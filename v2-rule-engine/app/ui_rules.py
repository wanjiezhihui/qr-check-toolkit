"""规则管理、规则编辑、条件编辑、规则测试器。"""

from __future__ import annotations

import json
from pathlib import Path
from tkinter import filedialog, ttk
import tkinter as tk
from typing import List, Optional

from . import importer
from .engine import (
    ACTION_HINT, ACTION_LABEL, ACTIONS, COND_NEEDS_PARAM, COND_TYPES, SUB_CMP,
    Condition, Rule, eval_condition, identify_match, parse_escalation,
)
from .service import ScanService, ServiceConfig
from .storage import Database
from .ui_common import (
    BG, DIM, GOLD, GREEN, LINE, ORANGE, PANEL, PANEL2, PANEL3, RED, SILVER, TEXT,
    YELLOW, F_H1, F_MONO_SM, F_SM, F_TITLE, F_UI, F_XS, Modal, ScrollFrame,
    ask_text, ask_yes_no, mk_button, mk_entry, mk_label, message, section_title,
)

PARAM_FIELDS = {
    "SEGMENT": [("sep", "分隔符", "%"), ("idx", "第几段(从0算)", "1"),
                ("cmp", "比较方式", "EQUALS")],
    "DATE_VALID": [("sep", "分隔符", "%"), ("idx", "第几段(从0算)", "1"),
                   ("fmt", "日期格式", "YYMMDD"), ("min_offset", "最早(天)", "-30"),
                   ("max_offset", "最晚(天)", "365")],
    "DATE_MATCH": [("sep", "分隔符", "%"), ("idx_a", "段A(从0算)", "1"),
                   ("fmt_a", "格式A", "YYMMDD"), ("idx_b", "段B(从0算)", "3"),
                   ("fmt_b", "格式B", "YYYYMMDD")],
}


# ==================================================================== 条件编辑器
class ConditionEditor(Modal):
    def __init__(self, parent, cond: Optional[Condition] = None):
        super().__init__(parent, "编辑校验条件", (680, 520), (False, True))
        self.cond = cond or Condition(ctype="CONTAINS")
        self._pvars: dict = {}
        self._build()

    def _build(self):
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=22, pady=16)

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x")
        mk_label(row, "条件类型", bg=BG).pack(side="left")
        self.var_type = tk.StringVar(value=COND_TYPES.get(self.cond.ctype, "包含子串"))
        self.cb_type = ttk.Combobox(row, textvariable=self.var_type, width=22,
                                    state="readonly",
                                    values=[COND_TYPES[k] for k in COND_TYPES])
        self.cb_type.pack(side="left", padx=(8, 0))
        self.cb_type.bind("<<ComboboxSelected>>", lambda e: self._rebuild())

        self.lbl_warn = mk_label(body, "", fg=RED, bg=BG, font=F_SM)
        self.lbl_warn.pack(fill="x", pady=(8, 0))

        self.dyn = tk.Frame(body, bg=PANEL)
        self.dyn.pack(fill="x", pady=(10, 0))
        self._rebuild()

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=22, pady=(0, 18))
        mk_button(bar, "确定", self._ok, "gold").pack(side="right", padx=(8, 0))
        mk_button(bar, "取消", self.on_cancel, "ghost").pack(side="right")
        mk_button(bar, "验证这一条", self._test_one, "normal").pack(side="left")

    def _cur_type(self) -> str:
        label = self.var_type.get()
        for k, v in COND_TYPES.items():
            if v == label:
                return k
        return "CONTAINS"

    def _rebuild(self):
        for w in self.dyn.winfo_children():
            w.destroy()
        self._pvars = {}
        ct = self._cur_type()

        self.lbl_warn.configure(
            text=("⚠ 正则表达式区分大小写且部分字符有特殊含义，请确认后再用"
                  if ct == "REGEX" else ""))

        pad = tk.Frame(self.dyn, bg=PANEL)
        pad.pack(fill="x", padx=14, pady=12)

        if ct in PARAM_FIELDS:
            pk = tk.Frame(pad, bg=PANEL)
            pk.pack(fill="x", pady=(0, 8))
            old = self.cond.params()
            for i, (key, label, dflt) in enumerate(PARAM_FIELDS[ct]):
                cell = tk.Frame(pk, bg=PANEL)
                cell.grid(row=0, column=i, padx=(0, 12))
                mk_label(cell, label, bg=PANEL, font=F_XS).pack(anchor="w")
                v = tk.StringVar(value=str(old.get(key, dflt)))
                self._pvars[key] = v
                if key == "cmp":
                    ttk.Combobox(cell, textvariable=v, width=12, state="readonly",
                                 values=[SUB_CMP[k] for k in SUB_CMP]).pack()
                elif key in ("sep", "fmt", "fmt_a", "fmt_b"):
                    ttk.Combobox(cell, textvariable=v, width=12,
                                 values=(["%", "/", "-", "+", "*", "|", ","]
                                         if key == "sep"
                                         else ["YYMMDD", "YYYYMMDD", "YYYY-MM-DD"])
                                 ).pack()
                else:
                    mk_entry(cell, v, width=12, font=F_SM).pack(ipady=2)

        valrow = tk.Frame(pad, bg=PANEL)
        valrow.pack(fill="x")
        self.var_val = tk.StringVar(value=self.cond.operand)
        if ct in ("DATE_VALID", "DATE_MATCH"):
            mk_label(valrow, "不需要填（按分段解析日期）", bg=PANEL,
                     fg=DIM, font=F_SM).pack(side="left")
        else:
            mk_label(valrow, "条件值", bg=PANEL).pack(side="left")
            mk_entry(valrow, self.var_val, width=46, font=F_MONO_SM).pack(
                side="left", padx=(8, 0), ipady=3)
            if ct in ("CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH", "EQUALS"):
                mk_label(valrow, "按字面量比对，* + % 都是普通字符", bg=PANEL,
                         fg=DIM, font=F_XS).pack(side="left", padx=(10, 0))

        opt = tk.Frame(pad, bg=PANEL)
        opt.pack(fill="x", pady=(12, 0))
        self.var_req = tk.BooleanVar(value=self.cond.required)
        ttk.Checkbutton(opt, text="必须满足", variable=self.var_req).pack(side="left")
        self.var_neg = tk.BooleanVar(value=self.cond.negate)
        ttk.Checkbutton(opt, text="取反（不满足才算通过）",
                        variable=self.var_neg).pack(side="left", padx=(16, 0))

        opt2 = tk.Frame(pad, bg=PANEL)
        opt2.pack(fill="x", pady=(10, 0))
        mk_label(opt2, "不满足时动作", bg=PANEL, font=F_SM).pack(side="left")
        self.var_fa = tk.StringVar(
            value=ACTION_LABEL.get(self.cond.fail_action, "(继承规则级)"))
        ttk.Combobox(opt2, textvariable=self.var_fa, width=14, state="readonly",
                     values=["(继承规则级)"] + [ACTION_LABEL[a] for a in ACTIONS]
                     ).pack(side="left", padx=(6, 18))
        mk_label(opt2, "提示文案", bg=PANEL, font=F_SM).pack(side="left")
        self.var_fm = tk.StringVar(value=self.cond.fail_msg)
        mk_entry(opt2, self.var_fm, width=26, font=F_SM).pack(side="left", padx=(6, 0), ipady=2)
        mk_label(opt2, "（写给现场看的人话）", bg=PANEL, fg=DIM, font=F_XS).pack(side="left", padx=(6, 0))

    def _collect(self) -> Condition:
        ct = self._cur_type()
        c = Condition(ctype=ct)
        c.operand = self.var_val.get() if ct not in ("DATE_VALID", "DATE_MATCH") else ""
        kw = {}
        for key, var in self._pvars.items():
            raw = var.get().strip()
            if key == "cmp":
                for k, v in SUB_CMP.items():
                    if v == raw:
                        kw[key] = k
                        break
                else:
                    kw[key] = raw or "EQUALS"
            elif key in ("idx", "idx_a", "idx_b", "min_offset", "max_offset"):
                try:
                    kw[key] = int(raw)
                except Exception:
                    kw[key] = 0
            else:
                kw[key] = raw
        c.op_param = json.dumps(kw, ensure_ascii=False) if kw else ""
        c.required = bool(self.var_req.get())
        c.negate = bool(self.var_neg.get())
        fa = self.var_fa.get()
        c.fail_action = "" if fa.startswith("(") else next(
            (k for k, v in ACTION_LABEL.items() if v == fa), "")
        c.fail_msg = self.var_fm.get().strip()
        return c

    def _test_one(self):
        c = self._collect()
        sample = ask_text(self, "试一下", "贴一个二维码内容进来（可直接用扫码枪扫）")
        if not sample:
            return
        ok, detail = eval_condition(c, sample)
        message(self, "条件验证结果",
                f"{'通过' if ok else '不通过'}\n\n条件：{c.describe()}\n说明：{detail}",
                "info" if ok else "warn")

    def _ok(self):
        c = self._collect()
        ct = c.ctype
        if ct == "REGEX":
            import re
            try:
                re.compile(c.operand)
            except re.error as ex:
                message(self, "正则写错了", f"这个正则不合法：{ex}", "error")
                return
        if ct not in ("DATE_VALID", "DATE_MATCH") and not c.operand.strip():
            message(self, "条件值为空", "请填写要比对的内容", "warn")
            return
        if ct in COND_NEEDS_PARAM and not c.op_param:
            message(self, "缺少参数", "这类条件必须填分隔符/段序号等参数", "warn")
            return
        self.result = c
        self.on_cancel()


# ==================================================================== 规则编辑器
class RuleEditor(Modal):
    def __init__(self, parent, db: Database, rule: Optional[Rule] = None,
                 copy_from: Optional[Rule] = None):
        super().__init__(parent, "编辑校验规则", (900, 760), (True, True))
        self.db = db
        self.existing_id = rule.id if rule else 0
        self.conds: List[Condition] = list(rule.conditions) if rule else []
        src = rule or copy_from
        self.rule = src or Rule()
        if copy_from and not rule:
            self.existing_id = 0
            self.rule = Rule(**{**copy_from.__dict__, "id": 0,
                                "part_no": "", "rule_name": ""})
            self.rule.conditions = list(copy_from.conditions)
        self._build()

    # ------------------------------------------------------------ 布局
    def _build(self):
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=20, pady=14)

        top = tk.Frame(wrap, bg=PANEL)
        top.pack(fill="x")
        g = tk.Frame(top, bg=PANEL)
        g.pack(fill="x", padx=16, pady=14)

        mk_label(g, "料号", bg=PANEL, font=F_UI).grid(row=0, column=0, sticky="w")
        self.var_part = tk.StringVar(value=self.rule.part_no)
        self.cb_part = ttk.Combobox(g, textvariable=self.var_part, width=26)
        self.cb_part.grid(row=0, column=1, sticky="w", padx=(8, 16))
        self.var_part.trace_add("write", self._on_part_changed)

        mk_label(g, "规则名", bg=PANEL, font=F_UI).grid(row=0, column=2, sticky="w")
        self.var_name = tk.StringVar(value=self.rule.rule_name or self.rule.part_no)
        mk_entry(g, self.var_name, width=26).grid(row=0, column=3, sticky="w",
                                                  padx=(8, 0), ipady=3)

        self.lbl_hint = mk_label(g, "", fg=DIM, bg=PANEL, font=F_SM)
        self.lbl_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        g2 = tk.Frame(top, bg=PANEL)
        g2.pack(fill="x", padx=16, pady=(0, 14))

        mk_label(g2, "识别方式", bg=PANEL, font=F_SM).grid(row=0, column=0, sticky="w")
        self.var_itype = tk.StringVar(value={
            "PREFIX": "前缀", "REGEX": "正则", "SEGMENT": "分段", "EXACT": "完全相等"
        }.get(self.rule.identify_type, "前缀"))
        ttk.Combobox(g2, textvariable=self.var_itype, width=10, state="readonly",
                     values=["前缀", "正则", "分段", "完全相等"]).grid(
            row=0, column=1, padx=(6, 14))

        mk_label(g2, "识别值", bg=PANEL, font=F_SM).grid(row=0, column=2, sticky="w")
        self.var_ival = tk.StringVar(value=self.rule.identify_value)
        mk_entry(g2, self.var_ival, width=30, font=F_MONO_SM).grid(
            row=0, column=3, padx=(6, 14), ipady=3)

        mk_label(g2, "分段参数", bg=PANEL, font=F_SM).grid(row=0, column=4, sticky="w")
        self.var_iparam = tk.StringVar(
            value=self._kv_from_json(self.rule.identify_param))
        mk_entry(g2, self.var_iparam, width=20, font=F_MONO_SM).grid(
            row=0, column=5, padx=(6, 0), ipady=3)
        mk_label(g2, "如 sep=%|idx=1（仅分段方式用）", bg=PANEL, fg=DIM,
                 font=F_XS).grid(row=1, column=4, columnspan=2, sticky="w", pady=(3, 0))
        mk_label(g2, "识别式不支持「包含」，避免把别的料号吸进来",
                 bg=PANEL, fg=DIM, font=F_XS).grid(row=1, column=0, columnspan=4, sticky="w")

        # 动作
        act = tk.Frame(wrap, bg=PANEL)
        act.pack(fill="x", pady=(10, 0))
        a = tk.Frame(act, bg=PANEL)
        a.pack(fill="x", padx=16, pady=14)

        def action_row(parent, row, label, attr, tip):
            mk_label(parent, label, bg=PANEL, font=F_SM).grid(
                row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=ACTION_LABEL.get(getattr(self.rule, attr), "拦截"))
            cb = ttk.Combobox(parent, textvariable=var, width=12, state="readonly",
                              values=[ACTION_LABEL[x] for x in ACTIONS])
            cb.grid(row=row, column=1, sticky="w", padx=(8, 12), pady=3)
            mk_label(parent, tip, bg=PANEL, fg=DIM, font=F_XS).grid(
                row=row, column=2, sticky="w")
            return var

        self.var_on_pass = action_row(a, 0, "条件全过", "on_pass", "通常放行")
        self.var_on_viol = action_row(a, 1, "条件不符", "on_violation",
                                      "内容不对时怎么办（拦截 = 不占唯一性）")
        self.var_on_dup = action_row(a, 2, "判为重复", "on_dup", "这个码之前已经过了")

        r2 = tk.Frame(a, bg=PANEL)
        r2.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        mk_label(r2, "验重范围", bg=PANEL, font=F_SM).pack(side="left")
        self.var_scope = tk.StringVar(value={
            "GLOBAL": "全局（一码全网唯一）", "PART": "仅本料号内", "NONE": "不验重"
        }.get(self.rule.dup_scope, "全局（一码全网唯一）"))
        ttk.Combobox(r2, textvariable=self.var_scope, width=20, state="readonly",
                     values=["全局（一码全网唯一）", "仅本料号内", "不验重"]).pack(
            side="left", padx=(8, 18))

        mk_label(r2, "条件关系", bg=PANEL, font=F_SM).pack(side="left")
        self.var_logic = tk.StringVar(value={
            "AND": "全部满足", "OR": "任一满足", "AT_LEAST": "至少N条"
        }.get(self.rule.match_logic, "全部满足"))
        ttk.Combobox(r2, textvariable=self.var_logic, width=12, state="readonly",
                     values=["全部满足", "任一满足", "至少N条"]).pack(
            side="left", padx=(8, 8))
        self.var_n = tk.StringVar(value=str(self.rule.at_least_n))
        mk_entry(r2, self.var_n, width=5, font=F_SM).pack(side="left", ipady=2)

        mk_label(r2, "优先级", bg=PANEL, font=F_SM).pack(side="left", padx=(18, 0))
        self.var_prio = tk.StringVar(value=str(self.rule.priority))
        mk_entry(r2, self.var_prio, width=6, font=F_SM).pack(side="left", padx=(6, 0), ipady=2)
        mk_label(r2, "（数字小的先匹配）", bg=PANEL, fg=DIM, font=F_XS).pack(
            side="left", padx=(6, 0))

        # 条件区
        chead = tk.Frame(wrap, bg=BG)
        chead.pack(fill="x", pady=(14, 6))
        section_title(chead).pack(side="left")
        self.lbl_ccount = mk_label(chead, "校验条件（0 条）", bg=BG, font=F_H1)
        self.lbl_ccount.pack(side="left")
        bar = tk.Frame(chead, bg=BG)
        bar.pack(side="right")
        mk_button(bar, "＋ 添加条件", self._add_cond, "gold").pack(side="left", padx=3)
        mk_button(bar, "编辑", self._edit_cond, "normal").pack(side="left", padx=3)
        mk_button(bar, "删除", self._del_cond, "ghost").pack(side="left", padx=3)
        mk_button(bar, "↑", lambda: self._move(-1), "ghost").pack(side="left", padx=3)
        mk_button(bar, "↓", lambda: self._move(1), "ghost").pack(side="left", padx=3)

        cols = ("seq", "type", "desc", "req", "action", "msg")
        self.tv = ttk.Treeview(wrap, columns=cols, show="headings", height=9)
        for c, t, w, anc in (
            ("seq", "#", 36, "center"), ("type", "类型", 130, "w"),
            ("desc", "条件描述", 330, "w"), ("req", "必须", 46, "center"),
            ("action", "不满足动作", 92, "center"), ("msg", "提示文案", 190, "w"),
        ):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor=anc)
        self.tv.pack(fill="both", expand=True, pady=(0, 10))
        self.tv.bind("<Double-1>", lambda e: self._edit_cond())
        self.tv.tag_configure("odd", background=PANEL2)

        foot = tk.Frame(wrap, bg=BG)
        foot.pack(fill="x")
        self.var_remark = tk.StringVar(value=self.rule.remark)
        mk_label(foot, "备注", bg=BG, font=F_SM).pack(side="left")
        mk_entry(foot, self.var_remark, width=40, font=F_SM).pack(
            side="left", padx=(8, 0), ipady=3)
        mk_button(foot, "取消", self.on_cancel, "ghost").pack(side="right")
        mk_button(foot, "保存规则", self._save, "primary").pack(side="right", padx=(0, 8))
        mk_button(foot, "测试这条规则", self._test_rule, "normal").pack(
            side="right", padx=(0, 8))

        self._refresh_conds()
        self._on_part_changed()
        self.cb_part.focus_force()

    # ------------------------------------------------------------ 料号提醒
    def _on_part_changed(self, *_):
        part = self.var_part.get().strip()
        if not part:
            self.lbl_hint.configure(text="料号必填，建议与 ERP/MES 料号完全一致",
                                    fg=DIM)
            self.cb_part.configure(values=[])
            return
        sugg = self.db.suggest_part_nos(part[:4] if len(part) > 4 else part, 20)
        self.cb_part.configure(values=sugg)
        hit = self.db.part_no_exists(part)
        if hit and hit["id"] != self.existing_id:
            self.lbl_hint.configure(
                text=f"⚠ 料号 {part} 已有规则「{hit['rule_name']}」"
                     f"（{hit['updated_at'] or '未知时间'}），保存将覆盖原规则",
                fg=YELLOW)
            return
        bad = [c for c in part if c in " \t，。、；;"]
        if bad:
            self.lbl_hint.configure(
                text=f"⚠ 料号里有空格或中文标点（{' '.join(set(bad))}），"
                     f"可能是扫码枪带进来的，建议清掉", fg=ORANGE)
            return
        if len(part) > 40:
            self.lbl_hint.configure(text="⚠ 料号超过 40 个字符，确认是否贴错了",
                                    fg=ORANGE)
            return
        self.lbl_hint.configure(text=f"✓ 料号 {part} 可用", fg=GREEN)

    # ------------------------------------------------------------ 条件表
    def _refresh_conds(self):
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        for i, c in enumerate(self.conds):
            self.tv.insert("", "end", iid=str(i), values=(
                i + 1, COND_TYPES.get(c.ctype, c.ctype), c.describe(),
                "必须" if c.required else "可选",
                ACTION_LABEL.get(c.fail_action, "继承") if c.fail_action else "继承",
                c.fail_msg or "",
            ), tags=("odd",) if i % 2 else ())
        self.lbl_ccount.configure(text=f"校验条件（{len(self.conds)} 条）")

    def _sel_index(self) -> Optional[int]:
        sel = self.tv.selection()
        return int(sel[0]) if sel else None

    def _add_cond(self):
        dlg = ConditionEditor(self)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.conds.append(dlg.result)
            self._refresh_conds()

    def _edit_cond(self):
        i = self._sel_index()
        if i is None:
            message(self, "先选一条", "请在列表里点选一条条件", "warn")
            return
        dlg = ConditionEditor(self, self.conds[i])
        self.wait_window(dlg)
        if dlg.result is not None:
            self.conds[i] = dlg.result
            self._refresh_conds()

    def _del_cond(self):
        i = self._sel_index()
        if i is None:
            return
        del self.conds[i]
        self._refresh_conds()

    def _move(self, delta):
        i = self._sel_index()
        if i is None:
            return
        j = i + delta
        if 0 <= j < len(self.conds):
            self.conds[i], self.conds[j] = self.conds[j], self.conds[i]
            self._refresh_conds()
            self.tv.selection_set(str(j))

    # ------------------------------------------------------------ 保存
    def _collect(self) -> Optional[Rule]:
        part = self.var_part.get().strip()
        if not part:
            message(self, "料号不能为空", "规则以料号命名，请先填料号", "warn")
            return None
        ival = self.var_ival.get().strip()
        if not ival:
            message(self, "识别值不能为空",
                    "识别值决定这个料号的码怎么被认出来，不填会导致码无法路由", "warn")
            return None
        if not self.conds:
            if not ask_yes_no(self, "没有校验条件",
                              "这条规则没有任何校验条件，凡是识别命中的码都会直接按「条件全过」处理。"
                              "确定要这样保存吗？"):
                return None

        r = Rule()
        r.id = self.existing_id
        r.part_no = part
        r.rule_name = self.var_name.get().strip() or part
        r.enabled = self.rule.enabled
        r.identify_type = {"前缀": "PREFIX", "正则": "REGEX", "分段": "SEGMENT",
                           "完全相等": "EXACT"}[self.var_itype.get()]
        r.identify_value = ival
        r.identify_param = self._kv_to_json(self.var_iparam.get())
        r.match_logic = {"全部满足": "AND", "任一满足": "OR",
                         "至少N条": "AT_LEAST"}[self.var_logic.get()]
        try:
            r.at_least_n = max(1, int(self.var_n.get()))
        except Exception:
            r.at_least_n = 1
        r.dup_scope = {"全局（一码全网唯一）": "GLOBAL", "仅本料号内": "PART",
                       "不验重": "NONE"}[self.var_scope.get()]
        r.on_pass = next((k for k, v in ACTION_LABEL.items()
                          if v == self.var_on_pass.get()), "PASS")
        r.on_violation = next((k for k, v in ACTION_LABEL.items()
                               if v == self.var_on_viol.get()), "BLOCK")
        r.on_dup = next((k for k, v in ACTION_LABEL.items()
                         if v == self.var_on_dup.get()), "LOCK_CODE")
        r.on_unknown = self.rule.on_unknown
        r.escalation = self.rule.escalation
        try:
            r.priority = int(self.var_prio.get())
        except Exception:
            r.priority = 100
        r.remark = self.var_remark.get().strip()
        r.conditions = list(self.conds)
        return r

    def _save(self):
        r = self._collect()
        if r is None:
            return
        if r.identify_type == "REGEX":
            import re
            try:
                re.compile(r.identify_value)
            except re.error as ex:
                message(self, "识别正则写错了", str(ex), "error")
                return
        conf = self.db.find_identify_conflict(
            r.identify_type, r.identify_value, r.identify_param, r.id)
        if conf:
            names = "、".join(f"{c['part_no']}（{c['identify_value']}）" for c in conf[:4])
            if not ask_yes_no(
                    self, "识别式冲突",
                    f"这个识别式与已有规则重叠：\n\n{names}\n\n"
                    f"前缀互相包含时，短的那个会把长的那个的码先吸走，"
                    f"导致按错的规则判定。\n\n仍然要保存吗？"):
                return
        self.db.save_rule(r)
        self.db.log_audit("本机", "save_rule", r.part_no,
                          f"{len(r.conditions)} 条条件")
        self.result = r
        self.on_cancel()

    def _test_rule(self):
        r = self._collect()
        if r is None:
            return
        code = ask_text(self, "试一下这条规则", "贴一个二维码内容进来（可直接扫码枪扫）")
        if not code:
            return
        lines = [f"输入：{code}", f"识别：{r.describe_identify()}",
                 f"识别结果：{'命中' if identify_match(r, code) else '不命中'}", ""]
        allok = True
        for i, c in enumerate(r.conditions, 1):
            ok, detail = eval_condition(c, code)
            allok = allok and ok
            lines.append(f"  {'✓' if ok else '✗'} 条件{i} {c.describe()}")
            lines.append(f"      {detail}")
        expect = ACTION_LABEL.get(r.on_pass if allok else r.on_violation, "?")
        lines += ["", f"预期动作：{expect}"]
        message(self, "规则试算", "\n".join(lines), "info")

    @staticmethod
    def _kv_from_json(js: str) -> str:
        if not js:
            return ""
        try:
            d = json.loads(js)
            return "|".join(f"{k}={v}" for k, v in d.items()) if isinstance(d, dict) else ""
        except Exception:
            return ""

    @staticmethod
    def _kv_to_json(text: str) -> str:
        if not text.strip():
            return ""
        kw = {}
        for part in text.replace("；", "|").split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    if k in ("idx", "idx_a", "idx_b"):
                        try:
                            kw[k] = int(v)
                            continue
                        except Exception:
                            pass
                    kw[k] = v
        return json.dumps(kw, ensure_ascii=False) if kw else ""


# ==================================================================== 导入预检
class ImportPreview(Modal):
    def __init__(self, parent, plan: importer.ImportPlan):
        super().__init__(parent, "导入预检", (860, 620), (True, True))
        self.plan = plan
        self._build()

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=20, pady=(16, 6))
        c = self.plan.counts()
        txt = (f"新增 {c['insert']}　覆盖 {c['overwrite']}　"
               f"冲突 {c['conflict']}　格式错 {c['error']}")
        mk_label(top, txt, bg=BG, fg=TEXT, font=F_H1).pack(side="left")
        mk_label(top, "　冲突和格式错的规则不会被导入", bg=BG,
                 fg=DIM, font=F_SM).pack(side="left")

        cols = ("kind", "part", "row", "msg")
        tv = ttk.Treeview(self, columns=cols, show="headings", height=16)
        for col, t, w, anc in (("kind", "结果", 80, "center"),
                               ("part", "料号", 150, "w"),
                               ("row", "行号", 60, "center"),
                               ("msg", "说明", 520, "w")):
            tv.heading(col, text=t)
            tv.column(col, width=w, anchor=anc)
        tv.pack(fill="both", expand=True, padx=20)
        tv.tag_configure("insert", foreground=GREEN)
        tv.tag_configure("overwrite", foreground=YELLOW)
        tv.tag_configure("conflict", foreground=ORANGE)
        tv.tag_configure("error", foreground=RED)
        label = {"insert": "新增", "overwrite": "覆盖",
                 "conflict": "冲突", "error": "格式错"}
        for i in self.plan.issues:
            tv.insert("", "end", values=(label.get(i.kind, i.kind), i.part_no,
                                         i.rowno, i.msg), tags=(i.kind,))

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=14)
        mk_button(bar, "取消", self.on_cancel, "ghost").pack(side="right")
        n = len(self.plan.rules) - len({i.part_no for i in self.plan.issues
                                        if i.kind in ("error", "conflict")})
        mk_button(bar, f"确认导入 {max(0, n)} 条", self._ok, "primary").pack(
            side="right", padx=(0, 8))

    def _ok(self):
        self.result = True
        self.on_cancel()


# ==================================================================== 规则管理
class RuleManager(Modal):
    def __init__(self, parent, db: Database):
        super().__init__(parent, "校验规则管理", (1080, 720), (True, True))
        self.db = db
        self._build()
        self.refresh()

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=18, pady=(14, 8))
        section_title(top).pack(side="left")
        mk_label(top, "校验规则", bg=BG, font=F_H1).pack(side="left")
        self.lbl_cnt = mk_label(top, "", bg=BG, fg=DIM, font=F_SM)
        self.lbl_cnt.pack(side="left", padx=(12, 0))

        sf = tk.Frame(top, bg=BG)
        sf.pack(side="right")
        mk_label(sf, "搜索", bg=BG, font=F_SM).pack(side="left")
        self.var_q = tk.StringVar()
        self.var_q.trace_add("write", lambda *a: self.refresh())
        mk_entry(sf, self.var_q, width=22, font=F_SM).pack(side="left", padx=(6, 0), ipady=3)

        cols = ("on", "part", "name", "ident", "ncond", "viol", "dup", "prio", "upd")
        self.tv = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anc in (
            ("on", "启用", 50, "center"), ("part", "料号", 160, "w"),
            ("name", "规则名", 150, "w"), ("ident", "识别式", 200, "w"),
            ("ncond", "条件", 50, "center"), ("viol", "不符时", 80, "center"),
            ("dup", "重复时", 80, "center"), ("prio", "优先级", 60, "center"),
            ("upd", "更新时间", 130, "w"),
        ):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor=anc)
        self.tv.pack(fill="both", expand=True, padx=18)
        self.tv.tag_configure("off", foreground=DIM)
        self.tv.tag_configure("odd", background=PANEL2)
        self.tv.bind("<Double-1>", lambda e: self.edit())

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=18, pady=12)
        mk_button(bar, "新建规则", self.new, "gold").pack(side="left", padx=(0, 6))
        mk_button(bar, "编辑", self.edit, "normal").pack(side="left", padx=3)
        mk_button(bar, "复制新建", self.copy_new, "normal").pack(side="left", padx=3)
        mk_button(bar, "启用/停用", self.toggle, "normal").pack(side="left", padx=3)
        mk_button(bar, "删除", self.delete, "danger").pack(side="left", padx=3)
        mk_button(bar, "关闭", self.on_cancel, "ghost").pack(side="right")

        bar2 = tk.Frame(self, bg=PANEL2)
        bar2.pack(fill="x", padx=18, pady=(0, 14))
        mk_label(bar2, "　批量：", bg=PANEL2, fg=SILVER, font=F_SM).pack(side="left")
        mk_button(bar2, "导入 Excel/CSV", self.import_file, "normal").pack(
            side="left", padx=4, pady=8)
        mk_button(bar2, "导出当前规则", self.export_file, "normal").pack(side="left", padx=4)
        mk_button(bar2, "下载导入模板", self.save_template, "normal").pack(side="left", padx=4)
        mk_label(bar2, "　模板规则：一行一个条件，同料号多行自动合并；续行料号可留空",
                 bg=PANEL2, fg=DIM, font=F_XS).pack(side="left")

    # ------------------------------------------------------------ 列表
    def refresh(self):
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        q = self.var_q.get().strip().lower()
        rules = self.db.list_rules()
        shown = 0
        for i, r in enumerate(rules):
            if q and q not in r.part_no.lower() and q not in (r.rule_name or "").lower():
                continue
            tags = []
            if not r.enabled:
                tags.append("off")
            if i % 2:
                tags.append("odd")
            self.tv.insert("", "end", iid=str(r.id), values=(
                "✓" if r.enabled else "—",
                r.part_no, r.rule_name,
                r.describe_identify(),
                len(r.conditions),
                ACTION_LABEL.get(r.on_violation, r.on_violation),
                ACTION_LABEL.get(r.on_dup, r.on_dup),
                r.priority, r.updated_at,
            ), tags=tuple(tags))
            shown += 1
        self.lbl_cnt.configure(text=f"共 {len(rules)} 条，显示 {shown} 条")

    def _sel(self) -> Optional[int]:
        s = self.tv.selection()
        return int(s[0]) if s else None

    # ------------------------------------------------------------ 操作
    def new(self):
        dlg = RuleEditor(self, self.db)
        self.wait_window(dlg)
        self.refresh()

    def edit(self):
        rid = self._sel()
        if rid is None:
            message(self, "先选一条", "请在列表里点选一条规则", "warn")
            return
        rule = self.db.get_rule(rid)
        if not rule:
            return
        dlg = RuleEditor(self, self.db, rule=rule)
        self.wait_window(dlg)
        self.refresh()

    def copy_new(self):
        rid = self._sel()
        if rid is None:
            message(self, "先选一条", "请先选中要复制的那条规则", "warn")
            return
        rule = self.db.get_rule(rid)
        if not rule:
            return
        dlg = RuleEditor(self, self.db, rule=None, copy_from=rule)
        self.wait_window(dlg)
        self.refresh()

    def toggle(self):
        rid = self._sel()
        if rid is None:
            return
        r = self.db.get_rule(rid)
        if not r:
            return
        self.db.set_enabled(rid, not r.enabled)
        self.db.log_audit("本机", "toggle_rule", r.part_no,
                          "启用" if not r.enabled else "停用")
        self.refresh()

    def delete(self):
        rid = self._sel()
        if rid is None:
            return
        r = self.db.get_rule(rid)
        if not r:
            return
        if not ask_yes_no(self, "确认删除",
                          f"删除规则「{r.part_no} {r.rule_name}」？\n\n"
                          f"已扫的扫码记录不会被删除，但这条规则不再参与判定。"):
            return
        self.db.delete_rule(rid)
        self.db.log_audit("本机", "delete_rule", r.part_no, "")
        self.refresh()

    def import_file(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择规则表格",
            filetypes=[("表格文件", "*.xlsx *.csv *.tsv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            rows = importer.read_table(Path(path))
            plan = importer.build_plan(rows, self.db)
        except Exception as ex:
            message(self, "读文件失败", f"{ex}", "error")
            return
        if not plan.rules and not plan.issues:
            message(self, "没有内容", "文件里没有可用的规则数据", "warn")
            return
        prev = ImportPreview(self, plan)
        self.wait_window(prev)
        if prev.result:
            n = importer.apply_plan(plan, self.db, actor="本机")
            self.refresh()
            message(self, "导入完成",
                    f"成功导入 {n} 条规则。\n\n{plan.summary()}", "info")

    def export_file(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="导出规则", defaultextension=".csv",
            initialfile="规则导出.csv",
            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            importer.export_rules_csv(Path(path), self.db)
            message(self, "导出完成", f"已导出到：\n{path}\n\n"
                                      f"改完可以直接导入回来。", "info")
        except Exception as ex:
            message(self, "导出失败", str(ex), "error")

    def save_template(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="保存导入模板", defaultextension=".csv",
            initialfile="规则导入模板.csv",
            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            importer.write_template(Path(path))
            message(self, "模板已保存",
                    f"{path}\n\n用 Excel 打开即可填。表头别改，"
                    f"一行填一个条件，同料号的多行会自动合并成一条规则。", "info")
        except Exception as ex:
            message(self, "保存失败", str(ex), "error")


# ==================================================================== 测试器
class RuleTester(Modal):
    def __init__(self, parent, svc: ScanService):
        super().__init__(parent, "规则测试器", (820, 600), (True, True))
        self.svc = svc
        self._build()

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=20, pady=(16, 6))
        section_title(top).pack(side="left")
        mk_label(top, "规则测试器", bg=BG, font=F_H1).pack(side="left")
        mk_label(top, "　试算不会写入扫码记录", bg=BG, fg=DIM, font=F_SM).pack(side="left")

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=20, pady=8)
        mk_label(row, "二维码内容", bg=BG, font=F_SM).pack(side="left")
        self.ent = mk_entry(row, width=56, font=F_MONO_SM)
        self.ent.pack(side="left", padx=(8, 10), ipady=4, fill="x", expand=True)
        self.ent.bind("<Return>", lambda e: self.run_test())

        row2 = tk.Frame(self, bg=BG)
        row2.pack(fill="x", padx=20)
        mk_label(row2, "指定料号", bg=BG, font=F_SM).pack(side="left")
        self.var_part = tk.StringVar(value="(自动识别)")
        parts = ["(自动识别)"] + [r.part_no for r in self.svc.db.list_rules()]
        ttk.Combobox(row2, textvariable=self.var_part, width=26, state="readonly",
                     values=parts).pack(side="left", padx=(8, 12))
        mk_button(row2, "执行判定", self.run_test, "gold").pack(side="left")

        self.txt = tk.Text(self, bg=PANEL, fg=TEXT, font=F_MONO_SM, relief="flat",
                           wrap="word", padx=14, pady=12, insertbackground=GOLD)
        self.txt.pack(fill="both", expand=True, padx=20, pady=12)
        self.txt.tag_configure("ok", foreground=GREEN, font=("Microsoft YaHei UI", 10, "bold"))
        self.txt.tag_configure("bad", foreground=RED, font=("Microsoft YaHei UI", 10, "bold"))
        self.txt.tag_configure("warn", foreground=YELLOW, font=("Microsoft YaHei UI", 9, "bold"))
        self.txt.tag_configure("key", foreground=GOLD, font=("Consolas", 9, "bold"))
        self.txt.tag_configure("dim", foreground="#8A8A93")
        self.txt.tag_configure("lbl", foreground=SILVER,
                               font=("Microsoft YaHei UI", 9, "bold"))

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=20, pady=(0, 14))
        mk_button(bar, "关闭", self.on_cancel, "ghost").pack(side="right")
        mk_button(bar, "拿最近一条扫码试试", self._use_last, "normal").pack(side="left")
        self.ent.focus_force()

    def _use_last(self):
        last = self.svc.db.last_scan()
        if not last:
            message(self, "还没有记录", "先扫一次再来试", "warn")
            return
        self.ent.delete(0, "end")
        self.ent.insert(0, last["raw"])
        self.var_part.set("(自动识别)")

    def run_test(self):
        code = self.ent.get().strip()
        if not code:
            return
        part = None if self.var_part.get().startswith("(") else self.var_part.get()
        v = self.svc.evaluate(code, part)
        t = self.txt
        t.delete("1.0", "end")
        t.insert("end", "输入　", "lbl")
        t.insert("end", code + "\n")
        t.insert("end", "命中　", "lbl")
        if v.rule:
            t.insert("end", f"{v.rule.part_no}　", "key")
            t.insert("end", f"{v.rule.rule_name}\n")
            t.insert("end", "识别　", "lbl")
            t.insert("end", f"{v.rule.describe_identify()}　")
            t.insert("end", "（手选料号）\n\n" if v.matched_by == "MANUAL" else "（自动路由）\n\n")
        else:
            t.insert("end", "未匹配任何已启用规则\n\n", "warn")

        if v.cond_results:
            t.insert("end", "条件逐条\n", "lbl")
            for i, r in enumerate(v.cond_results, 1):
                t.insert("end", f"　{'✓' if r.ok else '✗'} ", "ok" if r.ok else "bad")
                t.insert("end", f"条件{i}　{r.cond.describe()}\n")
                t.insert("end", f"　　　　{r.detail}\n", "dim")
            t.insert("end", "\n")

        t.insert("end", "判定　", "lbl")
        tag = {"PASS": "ok", "WARN": "warn"}.get(v.action, "bad")
        t.insert("end", "▌ " + v.label() + "\n", tag)
        if v.is_dup:
            t.insert("end", "重复　", "lbl")
            t.insert("end", f"第 {v.dup_count} 次，首次 {v.first_ts}\n", "warn")
        if v.escalate_note:
            t.insert("end", "升级　", "lbl")
            t.insert("end", f"{v.escalate_note}\n", "warn")
        t.insert("end", "说明　", "lbl")
        t.insert("end", v.reason + "\n\n")
        t.insert("end", "※ 本次为试算，未写入记录。实际扫码还会叠加锁定名单与升级策略。\n", "dim")
