"""主界面：扫码判定。"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
import tkinter as tk
from typing import Optional

try:
    import winsound
except Exception:
    winsound = None

from . import APP_TITLE, __version__
from .actions import LockScreen, WatchdogHandle, enable_dpi_awareness
from .engine import ACTION_LABEL, Rule, normalize
from .service import ScanService, ServiceConfig
from .storage import Database, now_str
from .ui_common import (
    ACTION_BG, ACTION_FG, BG, DIM, GOLD, GREEN, LINE, ORANGE, PANEL, PANEL2, PANEL3,
    RED, SILVER, TEXT, YELLOW, F_H1, F_MONO, F_MONO_SM, F_SM, F_STATE, F_TITLE,
    F_UI, F_XS, ask_text, ask_yes_no, message, mk_button, mk_entry, mk_label,
    style_ttk,
)
from .ui_rules import RuleManager, RuleTester

BEEP = {
    "PASS": [(880, 55)],
    "WARN": [(880, 70), (0, 60), (880, 70)],
    "BLOCK": [(520, 110), (0, 45), (520, 110), (0, 45), (520, 110)],
    "LOCK_CODE": [(380, 160), (0, 60), (380, 160), (0, 60), (380, 220)],
    "LOCK_SCREEN": [(300, 220), (0, 60), (300, 220), (0, 60), (300, 300)],
}


class App:
    def __init__(self, root, base_dir: Path):
        self.root = root
        self.base = Path(base_dir)
        self.data_dir = self.base / "data"
        self.export_dir = self.data_dir / "exports"
        self.run_dir = self.data_dir / "run"
        for d in (self.data_dir, self.export_dir, self.run_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.data_dir / "qr_rules.db")
        self.cfg = ServiceConfig(
            terminal=self.db.get("terminal", self._default_terminal()),
            batch=self.db.get("batch", ""),
            operator=self.db.get("operator", ""),
            ignore_case=self.db.get_bool("ignore_case", False),
            fullwidth=self.db.get_bool("fullwidth", False),
        )
        self.db.set("terminal", self.cfg.terminal)
        self.svc = ScanService(self.db, self.cfg)

        self._buf = ""
        self._capture = True
        self.sound_on = self.db.get_bool("sound_on", True)
        self._lock: Optional[LockScreen] = None
        self._wd = WatchdogHandle(self.run_dir)
        self._last_action = "PASS"

        enable_dpi_awareness()
        style_ttk(root)
        self._build()
        self._bind_keys()
        self._refresh_all()
        self._tick()

        if self.db.get_bool("watchdog_on", False):
            self._wd.clear_flag()
            self._wd.start()

    # ---------------------------------------------------------- 环境
    @staticmethod
    def _default_terminal() -> str:
        import os
        import socket
        try:
            return socket.gethostname() or os.environ.get("COMPUTERNAME", "本机")
        except Exception:
            return "本机"

    # ---------------------------------------------------------- 布局
    def _build(self):
        r = self.root
        r.title(f"{APP_TITLE}  v{__version__}")
        r.configure(bg=BG)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w = min(1440, max(1120, int(sw * 0.84)))
        h = min(920, max(720, int(sh * 0.84)))
        r.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 24)}")
        r.minsize(1080, 690)
        r.columnconfigure(0, weight=1)
        r.rowconfigure(2, weight=1)
        r.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_footer()

    def _build_header(self):
        head = tk.Frame(self.root, bg=PANEL2)
        head.grid(row=0, column=0, sticky="ew")
        inner = tk.Frame(head, bg=PANEL2)
        inner.pack(fill="x", padx=16, pady=9)

        tk.Frame(inner, bg=GOLD, width=4, height=22).pack(side="left", padx=(0, 10))
        tk.Label(inner, text=APP_TITLE, bg=PANEL2, fg=TEXT,
                 font=F_TITLE).pack(side="left")
        tk.Label(inner, text=f"v{__version__}", bg=PANEL2, fg=DIM,
                 font=F_XS).pack(side="left", padx=(8, 22))

        mk_label(inner, "料号", bg=PANEL2, font=F_SM).pack(side="left")
        self.var_part = tk.StringVar(value="▶ 自动识别")
        self.cb_part = ttk.Combobox(inner, textvariable=self.var_part, width=24,
                                    state="readonly")
        self.cb_part.pack(side="left", padx=(6, 14))
        self.cb_part.bind("<<ComboboxSelected>>", self._on_part_change)

        mk_label(inner, "批次/卷号", bg=PANEL2, font=F_SM).pack(side="left")
        self.var_batch = tk.StringVar(value=self.cfg.batch)
        e = mk_entry(inner, self.var_batch, width=16, font=F_SM)
        e.pack(side="left", padx=(6, 14), ipady=3)
        self.var_batch.trace_add("write", self._on_batch_change)

        self.lbl_term = mk_label(inner, f"终端 {self.cfg.terminal}", bg=PANEL2,
                                 fg=DIM, font=F_XS)
        self.lbl_term.pack(side="right")

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=PANEL)
        bar.grid(row=1, column=0, sticky="ew")
        inner = tk.Frame(bar, bg=PANEL)
        inner.pack(fill="x", padx=16, pady=7)

        mk_button(inner, "规则管理  F3", self.open_rules, "gold").pack(side="left", padx=(0, 6))
        mk_button(inner, "规则测试器  F8", self.open_tester, "normal").pack(side="left", padx=3)
        tk.Frame(inner, bg=LINE, width=1, height=20).pack(side="left", padx=10)
        mk_button(inner, "导出明细 F4", self.export_csv, "normal").pack(side="left", padx=3)
        mk_button(inner, "导出唯一码 F5", self.export_txt, "normal").pack(side="left", padx=3)
        mk_button(inner, "生成报告 F6", self.export_report, "normal").pack(side="left", padx=3)
        tk.Frame(inner, bg=LINE, width=1, height=20).pack(side="left", padx=10)
        mk_button(inner, "撤销上一条 F2", self.undo, "ghost").pack(side="left", padx=3)
        mk_button(inner, "锁定名单", self.open_locks, "ghost").pack(side="left", padx=3)

        self.btn_sound = mk_button(inner, "声音 开", self.toggle_sound, "ghost")
        self.btn_sound.pack(side="right", padx=3)
        mk_button(inner, "设置  F10", self.open_settings, "ghost").pack(side="right", padx=3)

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.grid(row=2, column=0, sticky="nsew", padx=14, pady=10)
        body.columnconfigure(0, weight=5, uniform="c")
        body.columnconfigure(1, weight=6, uniform="c")
        body.rowconfigure(0, weight=1)

        # ---------------- 左：当前扫描
        left = tk.Frame(body, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        left.rowconfigure(4, weight=0)

        hdr = tk.Frame(left, bg=PANEL)
        hdr.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        mk_label(hdr, "当前扫描内容", bg=PANEL, fg=SILVER, font=F_SM).pack(side="left")
        self.lbl_hint = mk_label(hdr, "", bg=PANEL, fg=DIM, font=F_XS)
        self.lbl_hint.pack(side="right")

        self.txt_code = tk.Text(left, height=3, bg=PANEL3, fg=TEXT, font=F_MONO,
                                relief="flat", wrap="char", padx=10, pady=8,
                                insertbackground=GOLD, highlightthickness=0)
        self.txt_code.grid(row=1, column=0, sticky="ew", padx=14)
        self.txt_code.configure(state="disabled")

        self.state_box = tk.Frame(left, bg=PANEL2)
        self.state_box.grid(row=2, column=0, sticky="nsew", padx=14, pady=(10, 6))
        self.state_box.columnconfigure(0, weight=1)
        self.state_box.rowconfigure(0, weight=1)

        self.lbl_state = tk.Label(self.state_box, text="等 待 扫 码", bg=PANEL2,
                                  fg=SILVER, font=F_STATE, anchor="center")
        self.lbl_state.grid(row=0, column=0, sticky="nsew")

        self.lbl_state_sub = tk.Label(self.state_box, text="", bg=PANEL2,
                                      fg=DIM, font=F_SM, anchor="center")
        self.lbl_state_sub.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        self.lbl_reason = tk.Label(left, text="扫码枪扫一张标签，或手动输入后回车",
                                   bg=PANEL, fg=DIM, font=F_SM, anchor="center",
                                   wraplength=430, justify="center")
        self.lbl_reason.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 6))

        stat = tk.Frame(left, bg=PANEL2)
        stat.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 12))
        self.stat_vals = {}
        for i, (key, label, color) in enumerate((
            ("total", "扫码总数", TEXT),
            ("unique", "唯一码", GREEN),
            ("dup_rows", "重复次数", YELLOW),
            ("blocked", "被拦截", RED),
        )):
            stat.columnconfigure(i, weight=1)
            cell = tk.Frame(stat, bg=PANEL2)
            cell.grid(row=0, column=i, sticky="nsew", pady=8)
            v = tk.Label(cell, text="0", bg=PANEL2, fg=color,
                         font=("Microsoft YaHei UI", 18, "bold"))
            v.pack()
            mk_label(cell, label, bg=PANEL2, fg=DIM, font=F_XS).pack()
            self.stat_vals[key] = v

        # ---------------- 右：最近记录
        right = tk.Frame(body, bg=PANEL)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        rh = tk.Frame(right, bg=PANEL)
        rh.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        mk_label(rh, "最近扫码", bg=PANEL, fg=SILVER, font=F_SM).pack(side="left")
        self.lbl_rule_cnt = mk_label(rh, "", bg=PANEL, fg=DIM, font=F_XS)
        self.lbl_rule_cnt.pack(side="right")

        cols = ("seq", "time", "part", "act", "code")
        self.tv = ttk.Treeview(right, columns=cols, show="headings", height=14)
        for c, t, w, anc in (("seq", "#", 52, "center"), ("time", "时间", 78, "center"),
                             ("part", "料号", 128, "w"), ("act", "判定", 62, "center"),
                             ("code", "二维码内容", 330, "w")):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor=anc)
        self.tv.grid(row=1, column=0, sticky="nsew", padx=14)
        for a in ("PASS", "WARN", "BLOCK", "LOCK_CODE", "LOCK_SCREEN"):
            self.tv.tag_configure(a, foreground=ACTION_FG[a])
        self.tv.bind("<Double-1>", self._show_row_detail)

    def _build_footer(self):
        foot = tk.Frame(self.root, bg=PANEL2)
        foot.grid(row=3, column=0, sticky="ew")
        self.status = tk.Label(foot, text="就绪", bg=PANEL2, fg=SILVER, font=F_XS,
                               anchor="w", padx=14, pady=5)
        self.status.pack(side="left")
        self.lbl_clock = tk.Label(foot, text="", bg=PANEL2, fg=DIM, font=F_XS,
                                  anchor="e", padx=14, pady=5)
        self.lbl_clock.pack(side="right")

    # ---------------------------------------------------------- 键盘
    def _bind_keys(self):
        self.root.bind_all("<Key>", self._on_key)
        self.root.bind("<F2>", lambda e: self.undo())
        self.root.bind("<F3>", lambda e: self.open_rules())
        self.root.bind("<F4>", lambda e: self.export_csv())
        self.root.bind("<F5>", lambda e: self.export_txt())
        self.root.bind("<F6>", lambda e: self.export_report())
        self.root.bind("<F8>", lambda e: self.open_tester())
        self.root.bind("<F9>", lambda e: self.toggle_sound())
        self.root.bind("<F10>", lambda e: self.open_settings())
        self.root.bind("<Escape>", lambda e: self._clear_current())

    def _on_key(self, event):
        if not self._capture:
            return None
        try:
            w = self.root.focus_get()
        except Exception:
            w = None
        if w is not None and isinstance(
                w, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text, ttk.Spinbox)):
            return None          # 输入框自己处理，别抢
        ks = event.keysym
        if ks in ("Return", "KP_Enter"):
            code = self._buf
            self._buf = ""
            if code:
                self._commit(code)
            return "break"
        ch = event.char
        if not ch or not ch.isprintable():
            return None          # 功能键放行，交给各自的绑定
        self._buf += ch
        if len(self._buf) > 4096:
            self._buf = ""
        return "break"

    # ---------------------------------------------------------- 判定
    def _commit(self, code: str):
        code = (code or "").strip()
        if not code:
            return
        part = None
        if not self.var_part.get().startswith("▶"):
            part = self.var_part.get()
        try:
            v = self.svc.scan(code, part)
        except Exception as ex:
            message(self.root, "判定出错", str(ex), "error")
            return
        self._apply(v)
        self._refresh_stats()
        self._refresh_recent()

    def _apply(self, v):
        self._last_action = v.action
        self.txt_code.configure(state="normal")
        self.txt_code.delete("1.0", "end")
        self.txt_code.insert("1.0", v.code)
        self.txt_code.configure(state="disabled")

        self.lbl_state.configure(
            text=v.label(),
            bg=ACTION_BG.get(v.action, PANEL2),
            fg=ACTION_FG.get(v.action, TEXT),
        )
        self.state_box.configure(bg=ACTION_BG.get(v.action, PANEL2))
        part_txt = (v.rule.part_no if v.rule else "未识别料号")
        self.lbl_state_sub.configure(
            text=f"{part_txt}　·　{v.code[:46]}",
            bg=ACTION_BG.get(v.action, PANEL2),
            fg=ACTION_FG.get(v.action, DIM),
        )
        self.lbl_reason.configure(
            text=v.reason or "",
            fg=ACTION_FG.get(v.action, SILVER),
        )
        self._beep(v.action)
        self.status.configure(
            text=f"{now_str()}　{v.label()}　{v.code[:60]}", fg=ACTION_FG.get(v.action, SILVER))

        if v.action == "LOCK_SCREEN":
            self.root.after(120, lambda: self._do_lock(v.reason))
        elif v.action == "LOCK_CODE":
            self._flash_window("#3A1618")

    def _flash_window(self, color):
        try:
            self.root.configure(bg=color)
            self.root.after(220, lambda: self.root.configure(bg=BG))
        except Exception:
            pass

    def _beep(self, action):
        if not self.sound_on:
            return
        pattern = BEEP.get(action, BEEP["PASS"])

        def play():
            try:
                if winsound is None:
                    return
                for freq, dur in pattern:
                    if freq:
                        winsound.Beep(freq, dur)
                    else:
                        time.sleep(dur / 1000.0)
            except Exception:
                pass

        threading.Thread(target=play, daemon=True).start()

    # ---------------------------------------------------------- 锁屏
    def _do_lock(self, reason: str):
        if self._lock is not None:
            return
        if not self.db.has_password():
            message(self.root, "还没设解锁密码",
                    "锁屏必须能解开，否则只能重启电脑。\n\n请先在「设置」里设定解锁密码。",
                    "warn")
            self._open_set_password()
            return
        self._capture = False
        self._lock = LockScreen(
            self.root,
            reason=f"二维码校验触发锁屏\n\n{reason[:260]}",
            check_pwd=self.db.check_password,
            on_done=self._on_unlocked,
        )
        self._lock.show()

    def _on_unlocked(self, ok: bool):
        self._lock = None
        self._capture = True
        if ok:
            self.db.log_audit("管理员", "unlock_screen", "", "锁屏解除")
            self.status.configure(text=f"{now_str()}　锁屏已解除", fg=GREEN)
        try:
            self.root.focus_force()
        except Exception:
            pass

    # ---------------------------------------------------------- 刷新
    def _refresh_all(self):
        self._refresh_parts()
        self._refresh_stats()
        self._refresh_recent()
        self.lbl_rule_cnt.configure(text=f"已启用规则 {len(self.svc.rules)} 条")
        self.btn_sound.configure(text=f"声音 {'开' if self.sound_on else '关'}")
        if not self.svc.rules:
            self.lbl_hint.configure(text="⚠ 还没有任何规则，所有码都会被拦截")
            self.lbl_hint.configure(fg=ORANGE)
        else:
            self.lbl_hint.configure(text="", fg=DIM)

    def _refresh_parts(self):
        cur = self.var_part.get()
        vals = ["▶ 自动识别"] + [r.part_no for r in self.db.list_rules(only_enabled=True)]
        self.cb_part.configure(values=vals)
        if cur not in vals:
            self.var_part.set("▶ 自动识别")

    def _refresh_stats(self):
        st = self.db.stats(self.cfg.batch or None)
        for k, lbl in self.stat_vals.items():
            lbl.configure(text=str(st.get(k, 0)))

    def _refresh_recent(self):
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        for row in self.db.recent(120, self.cfg.batch or None):
            self.tv.insert("", "end", iid=str(row["id"]), values=(
                row["seq"], row["ts"][11:19], row["part_no"] or "—",
                ACTION_LABEL.get(row["action"], row["action"]),
                row["raw"][:110],
            ), tags=(row["action"],))

    def _tick(self):
        self.lbl_clock.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            self.root.after(1000, self._tick)
        except Exception:
            pass

    # ---------------------------------------------------------- 交互
    def _on_part_change(self, _e=None):
        sel = self.var_part.get()
        if sel.startswith("▶"):
            self.status.configure(text="料号：自动识别（按各规则的识别式路由）", fg=SILVER)
        else:
            self.status.configure(text=f"料号已锁定：{sel}（只按这条规则校验）", fg=GOLD)
        self._focus_back()

    def _on_batch_change(self, *_):
        self.cfg.batch = self.var_batch.get().strip()
        self.db.set("batch", self.cfg.batch)
        self._refresh_stats()
        self._refresh_recent()

    def _focus_back(self):
        try:
            self.root.focus_set()
        except Exception:
            pass

    def _clear_current(self):
        self._buf = ""
        self.txt_code.configure(state="normal")
        self.txt_code.delete("1.0", "end")
        self.txt_code.configure(state="disabled")
        self.lbl_state.configure(text="等 待 扫 码", bg=PANEL2, fg=SILVER)
        self.lbl_state_sub.configure(text="", bg=PANEL2, fg=DIM)
        self.state_box.configure(bg=PANEL2)
        self.lbl_reason.configure(text="扫码枪扫一张标签，或手动输入后回车")
        self._focus_back()

    def _show_row_detail(self, _e=None):
        sel = self.tv.selection()
        if not sel:
            return
        row = self.db.conn.execute(
            "SELECT * FROM scans WHERE id=?", (int(sel[0]),)).fetchone()
        if not row:
            return
        mapping = {"AUTO": "自动路由", "MANUAL": "手选料号", "NONE": "未命中"}
        mb = mapping.get(row["matched_by"], row["matched_by"] or "—")
        dup = f"是，第 {row['dup_count']} 次" if row["is_dup"] else "否"
        txt = (
            f"二维码原文：\n{row['raw']}\n\n"
            f"料号：{row['part_no'] or '—'}\n"
            f"判定：{ACTION_LABEL.get(row['action'], row['action'])}\n"
            f"是否重复：{dup}\n"
            f"命中方式：{mb}\n"
            f"终端：{row['terminal']}\n"
            f"批次：{row['batch'] or '—'}\n"
            f"时间：{row['ts']}\n\n"
            f"说明：\n{row['reason']}"
        )
        message(self.root, "扫码详情", txt, "info")

    # ---------------------------------------------------------- 功能
    def open_rules(self):
        self._capture = False
        try:
            dlg = RuleManager(self.root, self.db)
            self.wait_window(dlg)
        finally:
            self._capture = True
        self.svc.reload_rules()
        self._refresh_all()
        self._focus_back()

    def open_tester(self):
        self._capture = False
        try:
            dlg = RuleTester(self.root, self.svc)
            self.wait_window(dlg)
        finally:
            self._capture = True
        self._focus_back()

    def open_locks(self):
        from .ui_locks import LockList
        self._capture = False
        try:
            dlg = LockList(self.root, self.db)
            self.wait_window(dlg)
        finally:
            self._capture = True
        self._focus_back()

    def open_settings(self):
        from .ui_settings import SettingsDialog
        self._capture = False
        try:
            dlg = SettingsDialog(self.root, self.db, self.cfg)
            self.wait_window(dlg)
            if dlg.result:
                self.cfg.terminal = self.db.get("terminal", self.cfg.terminal)
                self.cfg.ignore_case = self.db.get_bool("ignore_case", False)
                self.cfg.fullwidth = self.db.get_bool("fullwidth", False)
                self.sound_on = self.db.get_bool("sound_on", True)
                self.lbl_term.configure(text=f"终端 {self.cfg.terminal}")
                self.btn_sound.configure(text=f"声音 {'开' if self.sound_on else '关'}")
                if self.db.get_bool("watchdog_on", False):
                    self._wd.clear_flag()
                    self._wd.start()
                else:
                    self._wd.stop()
        finally:
            self._capture = True
        self._focus_back()

    def _open_set_password(self):
        p1 = ask_text(self.root, "设置解锁密码",
                      "设定解锁密码（锁屏/锁码时用），至少 4 位：", show="●")
        if not p1:
            return
        if len(p1) < 4:
            message(self.root, "太短了", "解锁密码至少 4 位", "warn")
            return
        p2 = ask_text(self.root, "确认密码", "再输一次：", show="●")
        if p1 != p2:
            message(self.root, "两次不一致", "请重新设置", "warn")
            return
        self.db.set_password(p1)
        self.db.log_audit("管理员", "set_password", "", "")
        message(self.root, "已设置", "解锁密码已保存（只存哈希，不存明文）。\n"
                                    "忘了只能删库重来，请记好。", "info")

    def toggle_sound(self):
        self.sound_on = not self.sound_on
        self.db.set_bool("sound_on", self.sound_on)
        self.btn_sound.configure(text=f"声音 {'开' if self.sound_on else '关'}")
        self._focus_back()

    def undo(self):
        raw = self.svc.undo_last()
        if raw is None:
            self.status.configure(text="没有可撤销的记录", fg=YELLOW)
            return
        self._refresh_stats()
        self._refresh_recent()
        self._clear_current()
        self.status.configure(text=f"已撤销：{raw[:60]}　（请把那张标签重新扫一次）", fg=YELLOW)

    # ---------------------------------------------------------- 导出
    def _save_path(self, ext: str, name: str) -> Optional[Path]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            parent=self.root, title="保存",
            initialdir=str(self.export_dir),
            initialfile=f"{name}_{stamp}.{ext}",
            defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} 文件", f"*.{ext}")])
        return Path(path) if path else None

    def export_csv(self):
        p = self._save_path("csv", "扫码明细")
        if not p:
            return
        try:
            n = self.svc.export_csv(p, self.cfg.batch or None)
            self.status.configure(text=f"已导出 {n} 条明细 → {p.name}", fg=GREEN)
            message(self.root, "导出完成", f"{n} 条记录\n\n{p}", "info")
        except Exception as ex:
            message(self.root, "导出失败", str(ex), "error")
        self._focus_back()

    def export_txt(self):
        p = self._save_path("txt", "唯一码")
        if not p:
            return
        try:
            n = self.svc.export_txt(p, self.cfg.batch or None)
            self.status.configure(text=f"已导出 {n} 个唯一码 → {p.name}", fg=GREEN)
            message(self.root, "导出完成", f"{n} 个唯一码，一行一个\n\n{p}", "info")
        except Exception as ex:
            message(self.root, "导出失败", str(ex), "error")
        self._focus_back()

    def export_report(self):
        p = self._save_path("txt", "校验报告")
        if not p:
            return
        try:
            self.svc.export_report(p, self.cfg.batch or None)
            self.status.configure(text=f"报告已生成 → {p.name}", fg=GREEN)
            message(self.root, "报告已生成", str(p), "info")
        except Exception as ex:
            message(self.root, "生成失败", str(ex), "error")
        self._focus_back()

    # ---------------------------------------------------------- 关闭
    def on_close(self):
        running = self.db.stats().get("total", 0)
        if self._last_action == "LOCK_SCREEN" and self._lock is not None:
            return
        if running and not ask_yes_no(
                self.root, "确认退出",
                f"本次已扫 {running} 条记录，数据已经实时落库、退出不会丢。\n\n确定退出吗？"):
            return
        if self.db.get_bool("watchdog_on", False):
            self._wd.stop()
        self.svc.reload_rules()
        self.db.log_audit("本机", "app_close", "", f"本次 {running} 条")
        try:
            self.db.close()
        except Exception:
            pass
        self.root.destroy()
