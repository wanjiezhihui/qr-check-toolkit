"""设置：终端身份、识别口径、声音、看门狗、解锁密码、锁屏测试。"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .actions import LockScreen
from .service import ServiceConfig
from .storage import Database
from .ui_common import (
    BG, DIM, GOLD, GREEN, ORANGE, PANEL, PANEL2, PANEL3, RED, SILVER, TEXT, YELLOW,
    F_H1, F_SM, F_XS, Modal, ask_text, ask_yes_no, mk_button, mk_entry, mk_label,
    message, section_title,
)


class SettingsDialog(Modal):
    def __init__(self, parent, db: Database, cfg: ServiceConfig):
        super().__init__(parent, "设置", (720, 700), (False, True))
        self.db = db
        self.cfg = cfg
        self.result = False
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=22, pady=16)

        # ---- 终端身份
        section_title(wrap).pack(anchor="w", pady=(0, 8))
        mk_label(wrap, "终端与批次", bg=BG, font=F_H1).pack(anchor="w", pady=(0, 8))

        box = tk.Frame(wrap, bg=PANEL)
        box.pack(fill="x")
        g = tk.Frame(box, bg=PANEL)
        g.pack(fill="x", padx=16, pady=14)

        mk_label(g, "终端名称", bg=PANEL).grid(row=0, column=0, sticky="w")
        self.var_term = tk.StringVar(value=self.db.get("terminal", self.cfg.terminal))
        mk_entry(g, self.var_term, width=28).grid(row=0, column=1, sticky="w",
                                                  padx=(8, 16), ipady=3)
        mk_label(g, "当前批次/卷号", bg=PANEL).grid(row=0, column=2, sticky="w")
        self.var_batch = tk.StringVar(value=self.cfg.batch)
        mk_entry(g, self.var_batch, width=18).grid(row=0, column=3, sticky="w",
                                                   padx=(8, 0), ipady=3)
        mk_label(g, "终端名会写进每条扫码记录，报表按它区分车间",
                 bg=PANEL, fg=DIM, font=F_XS).grid(row=1, column=0, columnspan=4,
                                                   sticky="w", pady=(6, 0))

        # ---- 识别口径
        mk_label(wrap, "验重比对口径", bg=BG, font=F_H1).pack(anchor="w", pady=(16, 8))
        box2 = tk.Frame(wrap, bg=PANEL)
        box2.pack(fill="x")
        g2 = tk.Frame(box2, bg=PANEL)
        g2.pack(fill="x", padx=16, pady=14)

        self.var_case = tk.BooleanVar(value=self.db.get_bool("ignore_case", False))
        ttk.Checkbutton(g2, text="忽略大小写（ABC 与 abc 视为同一个码）",
                        variable=self.var_case).pack(anchor="w")
        self.var_full = tk.BooleanVar(value=self.db.get_bool("fullwidth", False))
        ttk.Checkbutton(g2, text="全角转半角（Ａ１ 与 A1 视为同一个码）",
                        variable=self.var_full).pack(anchor="w", pady=(4, 0))
        mk_label(g2, "只影响「判重」，不影响内容校验——校验永远按标签原样比对。",
                 bg=PANEL, fg=DIM, font=F_XS).pack(anchor="w", pady=(6, 0))

        # ---- 报警与守护
        mk_label(wrap, "报警与守护", bg=BG, font=F_H1).pack(anchor="w", pady=(16, 8))
        box3 = tk.Frame(wrap, bg=PANEL)
        box3.pack(fill="x")
        g3 = tk.Frame(box3, bg=PANEL)
        g3.pack(fill="x", padx=16, pady=14)
        self.var_sound = tk.BooleanVar(value=self.db.get_bool("sound_on", True))
        ttk.Checkbutton(g3, text="开启报警声音", variable=self.var_sound).pack(anchor="w")
        self.var_wd = tk.BooleanVar(value=self.db.get_bool("watchdog_on", False))
        ttk.Checkbutton(g3, text="开启看门狗（主程序被强杀后 3 秒内自动重启并保持锁定态）",
                        variable=self.var_wd).pack(anchor="w", pady=(4, 0))
        mk_label(g3, "⚠ 看门狗挡的是「任务管理器结束进程」。Ctrl+Alt+Del、Win+L、"
                     "注销仍拦不住——那是 Windows 保留给系统的。",
                 bg=PANEL, fg=ORANGE, font=F_XS, wraplength=620,
                 justify="left").pack(anchor="w", pady=(8, 0))

        # ---- 密码与锁屏
        mk_label(wrap, "解锁密码与锁屏", bg=BG, font=F_H1).pack(anchor="w", pady=(16, 8))
        box4 = tk.Frame(wrap, bg=PANEL)
        box4.pack(fill="x")
        g4 = tk.Frame(box4, bg=PANEL)
        g4.pack(fill="x", padx=16, pady=14)
        has = self.db.has_password()
        self.lbl_pwd = mk_label(g4, "已设置解锁密码" if has else "尚未设置解锁密码（锁屏/锁码会解不开）",
                                bg=PANEL, fg=GREEN if has else RED, font=F_SM)
        self.lbl_pwd.pack(anchor="w")
        row = tk.Frame(g4, bg=PANEL)
        row.pack(anchor="w", pady=(10, 0))
        mk_button(row, "修改解锁密码" if has else "设置解锁密码",
                  self._set_pwd, "gold").pack(side="left", padx=(0, 8))
        mk_button(row, "测试锁屏", self._test_lock, "normal").pack(side="left")
        mk_label(g4, "密码只存 scrypt 哈希，不存明文。忘了只能删库重来。",
                 bg=PANEL, fg=DIM, font=F_XS).pack(anchor="w", pady=(8, 0))

        # ---- 数据位置
        mk_label(wrap, "数据位置", bg=BG, font=F_H1).pack(anchor="w", pady=(16, 6))
        st = self.db.stats()
        mk_label(wrap, f"{self.db.path}　（{st['total']} 条记录）",
                 bg=BG, fg=DIM, font=F_XS).pack(anchor="w")

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=22, pady=(0, 16))
        mk_button(bar, "保存", self._save, "primary").pack(side="right", padx=(8, 0))
        mk_button(bar, "取消", self.on_cancel, "ghost").pack(side="right")

    # ------------------------------------------------------------ 操作
    def _set_pwd(self):
        p1 = ask_text(self, "设置解锁密码", "新密码（至少 4 位）：", show="●")
        if not p1:
            return
        if len(p1) < 4:
            message(self, "太短了", "至少 4 位", "warn")
            return
        p2 = ask_text(self, "确认", "再输一次：", show="●")
        if p1 != p2:
            message(self, "两次不一致", "请重新设置", "warn")
            return
        self.db.set_password(p1)
        self.db.log_audit("管理员", "set_password", "", "")
        self.lbl_pwd.configure(text="已设置解锁密码", fg=GREEN)
        message(self, "已保存", "解锁密码已更新。", "info")

    def _test_lock(self):
        if not self.db.has_password():
            message(self, "先设密码", "没有密码的话锁上就解不开了", "warn")
            return
        if not ask_yes_no(self, "测试锁屏",
                          "将立即全屏锁定，需要输入解锁密码才能退回。\n\n"
                          "在锁定界面上，Alt+F4 / Alt+Tab / Win 键 / Ctrl+Esc 都会被吞掉。\n"
                          "Ctrl+Alt+Del 仍可呼出系统菜单——这是 Windows 保留的，程序拦不住。\n\n"
                          "现在开始吗？"):
            return
        self._show_lock("这是一次锁屏测试")

    def _show_lock(self, reason):
        cfg = ServiceConfig(terminal=self.var_term.get() or "本机")
        self._locker = LockScreen(self, reason=reason,
                                  check_pwd=self.db.check_password,
                                  on_done=lambda ok: None)
        self._locker.show()

    def _save(self):
        self.db.set("terminal", self.var_term.get().strip() or "本机")
        self.db.set_bool("ignore_case", bool(self.var_case.get()))
        self.db.set_bool("fullwidth", bool(self.var_full.get()))
        self.db.set_bool("sound_on", bool(self.var_sound.get()))
        self.db.set_bool("watchdog_on", bool(self.var_wd.get()))
        self.cfg.batch = self.var_batch.get().strip()
        self.db.set("batch", self.cfg.batch)
        self.db.log_audit("本机", "save_settings", "", "")
        self.result = True
        self.on_cancel()
