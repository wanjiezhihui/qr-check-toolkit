"""业务编排层：把引擎的纯判定和存储的落库串起来。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .engine import (
    ACTION_LABEL, ACTION_RANK, LOCKING_ACTIONS, Condition, CondResult, Rule, Verdict,
    combine_logic, eval_condition, identify_match, normalize, parse_escalation,
    route_rule, rule_in_window, worse,
)
from .storage import SEP, Database, now_str

UNKNOWN_DEFAULT = "BLOCK"


@dataclass
class ServiceConfig:
    terminal: str = "本机"
    batch: str = ""
    operator: str = ""
    ignore_case: bool = False
    fullwidth: bool = False


class ScanService:
    def __init__(self, db: Database, cfg: Optional[ServiceConfig] = None):
        self.db = db
        self.cfg = cfg or ServiceConfig()
        self._rules: List[Rule] = []
        self.reload_rules()

    # ---------------------------------------------------------- 规则缓存
    def reload_rules(self):
        rows = self.db.list_rules(only_enabled=True)
        self._rules = [r for r in (self.db.get_rule(x.id) for x in rows) if r]
        return len(self._rules)

    @property
    def rules(self) -> List[Rule]:
        return self._rules

    # ---------------------------------------------------------- 唯一键
    def uniq_key_for(self, rule: Optional[Rule], norm: str) -> Optional[str]:
        if rule is None or rule.dup_scope == "NONE":
            return None
        if rule.dup_scope == "PART":
            return rule.part_no + SEP + norm
        return norm

    # ---------------------------------------------------------- 纯判定
    def evaluate(self, code: str, part_no: Optional[str] = None) -> Verdict:
        """只判定，不落库。规则测试器直接用这个。"""
        code = (code or "").strip()
        norm = normalize(code, self.cfg.ignore_case, self.cfg.fullwidth)
        v = Verdict(action="PASS", code=code, norm=norm)

        # ---- 1. 找规则
        rule: Optional[Rule] = None
        matched_by = "NONE"
        if part_no:
            rule = self.db.get_rule_by_part(part_no)
            matched_by = "MANUAL"
        else:
            pool = [r for r in self._rules if rule_in_window(r, now_str())]
            rule = route_rule(pool, code)
            matched_by = "AUTO" if rule else "NONE"

        # ---- 2. 无规则 → 未建规则处置
        if rule is None:
            unknown = self.cfg_action("unknown_action", UNKNOWN_DEFAULT)
            v.action = unknown
            v.matched_by = "NONE"
            v.reason = f"未匹配任何已启用规则（料号未建规则），按「{ACTION_LABEL.get(unknown, unknown)}」处置"
            self._write_record(v, rule, is_dup=False, dup_count=0,
                               first_ts="", first_terminal="", reason=v.reason)
            return v

        v.rule = rule
        v.matched_by = matched_by
        pre_reasons: List[str] = []

        # 手选模式下，识别式不匹配 → 疑似混料，但不阻断
        if matched_by == "MANUAL" and rule.identify_value:
            if not identify_match(rule, code):
                pre_reasons.append(
                    f"该码不符合料号 {rule.part_no} 的识别式（{rule.describe_identify()}），疑似混料"
                )

        # ---- 3. 已锁定？
        locked = self.db.is_locked(norm)
        if locked:
            v.action = "LOCK_CODE"
            v.reason = f"该码已被锁定（{locked['locked_at']}，原因：{locked['reason'] or '未填写'}），需密码解锁"
            self._write_record(v, rule, is_dup=False, dup_count=0,
                               first_ts=locked["locked_at"], first_terminal="",
                               reason=v.reason)
            return v

        # ---- 4. 条件校验
        results: List[CondResult] = []
        for cond in rule.conditions:
            ok, detail = eval_condition(cond, code)
            results.append(CondResult(cond=cond, ok=ok, detail=detail))
        v.cond_results = results
        logic_ok, logic_msg = combine_logic(rule, results)

        action = rule.on_pass if logic_ok else rule.on_violation
        reasons = list(pre_reasons)
        if not logic_ok:
            # 条件级 fail_action 是【覆盖】规则级动作，不是取更严。
            # 多条失败条件都配了动作时，取其中更严重的那个。
            # 注意：不允许覆盖成 PASS（那等于让违规码混过去）。
            override = None
            for r in results:
                if not r.ok and r.cond.fail_action in ACTION_LABEL \
                        and r.cond.fail_action != "PASS":
                    override = (r.cond.fail_action if override is None
                                else worse(override, r.cond.fail_action))
            if override:
                action = override
            reasons.append(logic_msg)
        elif pre_reasons:
            action = worse(action, "WARN")

        # ---- 5. 验重
        key = self.uniq_key_for(rule, norm)
        prev = self.db.find_valid(key) if key else []
        v.is_dup = len(prev) > 0
        v.dup_count = len(prev) + 1
        if prev:
            v.first_ts = prev[0]["ts"]
            v.first_terminal = prev[0]["terminal"] or ""
            reasons.append(
                f"重复：首次于 {v.first_ts}"
                + (f"（终端 {v.first_terminal}）" if v.first_terminal else "")
                + f"，这是第 {v.dup_count} 次"
            )
            action = worse(action, rule.on_dup)

        # ---- 6. 升级策略
        esc = parse_escalation(rule)
        if esc and action in ("BLOCK", "LOCK_CODE", "LOCK_SCREEN"):
            cnt = self.db.count_violations(rule.id, int(esc.get("minutes", 480)))
            if cnt + 1 >= int(esc["count"]):
                up = esc["action"]
                if ACTION_RANK.get(up, 0) > ACTION_RANK.get(action, 0):
                    v.escalate_note = (
                        f"本班次已累计 {cnt + 1} 次违规，动作由"
                        f"「{ACTION_LABEL.get(action)}」升级为「{ACTION_LABEL.get(up)}」"
                    )
                    action = up

        if action == "PASS" and not reasons:
            reasons.append("全部条件通过" + ("，且未重复" if key else ""))

        v.action = action
        v.reason = "；".join(reasons)

        # ---- 7. 落库
        self._write_record(v, rule, is_dup=v.is_dup, dup_count=v.dup_count,
                           first_ts=v.first_ts, first_terminal=v.first_terminal,
                           reason=v.reason)
        return v

    def cfg_action(self, key: str, default: str) -> str:
        got = self.db.get(key, default)
        return got if got in ACTION_LABEL else default

    # ---------------------------------------------------------- 落库
    def _write_record(self, v: Verdict, rule: Optional[Rule], is_dup: bool,
                      dup_count: int, first_ts: str, first_terminal: str,
                      reason: str) -> int:
        action = v.action
        # 唯一键占用规则：
        #   BLOCK            → 不占位、不算有效记录（被拦的码重扫仍按「首次」处理）
        #   key 未被占用      → 占位（这是该码的第一次有效通过）
        #   key 已被占用/无key → 不占位（重复、锁定、锁屏、未识别等情况）
        key = self.uniq_key_for(rule, v.norm) if rule else None
        if action == "BLOCK":
            is_valid, uniq_key = 0, None
        elif key and not self.db.find_valid(key):
            is_valid, uniq_key = 1, key
        else:
            is_valid, uniq_key = 1, None

        kw = dict(
            seq=self.db.next_seq(),
            raw=v.code,
            norm=v.norm,
            uniq_key=uniq_key,
            part_no=(rule.part_no if rule else ""),
            rule_id=(rule.id if rule else 0),
            action=action,
            is_valid=is_valid,
            is_dup=(1 if is_dup else 0),
            dup_count=max(1, dup_count),
            matched_by=v.matched_by,
            terminal=self.cfg.terminal,
            batch=self.cfg.batch,
            operator=self.cfg.operator,
            reason=reason,
            ts=now_str(),
        )
        try:
            scan_id = self.db.insert_scan(**kw)
        except Exception:
            # 并发兜底：唯一索引冲突说明别人刚占了位，改按重复记录落库
            kw["uniq_key"] = None
            kw["is_dup"] = 1
            scan_id = self.db.insert_scan(**kw)

        # 锁定动作 → 写入锁定名单
        if action in LOCKING_ACTIONS:
            self.db.lock(
                norm=v.norm, raw=v.code, part_no=(rule.part_no if rule else ""),
                reason=(v.reason[:200] if action == "LOCK_CODE" else "触发锁屏策略")[:200],
                by=self.cfg.terminal,
            )
        return scan_id

    def scan(self, code: str, part_no: Optional[str] = None) -> Verdict:
        return self.evaluate(code, part_no)

    # ---------------------------------------------------------- 撤销
    def undo_last(self) -> Optional[str]:
        last = self.db.last_scan()
        if not last:
            return None
        return self.db.delete_scan(last["id"])

    # ---------------------------------------------------------- 导出
    def export_csv(self, path: Path, batch: Optional[str] = None) -> int:
        rows = self.db.all_rows(batch)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["序号", "二维码原文", "料号", "判定", "是否重复", "第几次",
                        "命中方式", "终端", "批次", "时间", "说明"])
            for r in rows:
                w.writerow([
                    r["seq"], r["raw"], r["part_no"],
                    ACTION_LABEL.get(r["action"], r["action"]),
                    "是" if r["is_dup"] else "否",
                    r["dup_count"], r["matched_by"], r["terminal"],
                    r["batch"], r["ts"], r["reason"],
                ])
        return len(rows)

    def export_txt(self, path: Path, batch: Optional[str] = None) -> int:
        codes = self.db.unique_codes(batch)
        with open(path, "w", encoding="utf-8") as f:
            for c in codes:
                f.write(c + "\n")
        return len(codes)

    def export_report(self, path: Path, batch: Optional[str] = None) -> str:
        st = self.db.stats(batch)
        lines = [
            "二维码标签校验报告",
            "=" * 56,
            f"生成时间：{now_str()}",
            f"终端：{self.cfg.terminal}",
            f"批次：{batch or self.cfg.batch or '（全部）'}",
            "",
            "一、总体",
            f"  扫码总次数         {st['total']}",
            f"  有效记录           {st['valid']}",
            f"  唯一码数量         {st['unique']}",
            f"  重复扫码次数       {st['dup_rows']}",
            f"  涉及重复的码数     {st['dup_codes']}",
            f"  被拦截次数         {st['blocked']}",
            f"  提醒次数           {st['warned']}",
            f"  当前锁定码数       {st['locked']}",
            "",
            "二、按料号",
        ]
        rows = self.db.all_rows(batch)
        agg: dict = {}
        for r in rows:
            k = r["part_no"] or "(未识别)"
            a = agg.setdefault(k, {"n": 0, "ok": 0, "dup": 0, "bad": 0})
            a["n"] += 1
            if r["action"] == "BLOCK":
                a["bad"] += 1
            elif r["is_dup"]:
                a["dup"] += 1
            else:
                a["ok"] += 1
        for k in sorted(agg):
            a = agg[k]
            lines.append(f"  {k:<22} 共 {a['n']:>6}  通过 {a['ok']:>6}  "
                         f"重复 {a['dup']:>4}  拦截 {a['bad']:>4}")
        lines += ["", "三、问题清单"]
        bad = [r for r in rows if r["action"] == "BLOCK" or r["is_dup"]]
        if not bad:
            lines.append("  无")
        else:
            for r in bad[:500]:
                lines.append(
                    f"  [{r['seq']:>6}] {r['ts']}  {ACTION_LABEL.get(r['action'], r['action'])}  "
                    f"{r['raw']}  ← {r['reason'][:60]}"
                )
            if len(bad) > 500:
                lines.append(f"  …… 其余 {len(bad) - 500} 条见 CSV 明细")
        text = "\n".join(lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return text
