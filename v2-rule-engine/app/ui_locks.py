"""锁定名单：查看与解锁。"""

from __future__ import annotations

from tkinter import ttk
import tkinter as tk

from .storage import Database, now_str
from .ui_common import (
    BG, DIM, GREEN, GOLD, PANEL, PANEL2, RED, SILVER, TEXT, YELLOW,
    F_H1, F_SM, F_XS, Modal, ask_text, ask_yes_no, mk_button, mk_label,
    message, section_title,
)


class LockList(Modal):
    def __init__(self, parent, db: Database):
        super().__init__(parent, "锁定名单", (960, 620), (True, True))
        self.db = db
        self._build()
        self.refresh()

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=18, pady=(14, 8))
        section_title(top).pack(side="left")
        mk_label(top, "锁定名单", bg=BG, font=F_H1).pack(side="left")
        self.lbl_cnt = mk_label(top, "", bg=BG, fg=DIM, font=F_SM)
        self.lbl_cnt.pack(side="left", padx=(12, 0))

        cols = ("part", "code", "time", "by", "reason")
        self.tv = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anc in (("part", "料号", 140, "w"), ("code", "二维码内容", 340, "w"),
                             ("time", "锁定时间", 140, "w"),
                             ("by", "锁定终端", 120, "w"), ("reason", "原因", 200, "w")):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor=anc)
        self.tv.pack(fill="both", expand=True, padx=18)
        self.tv.tag_configure("odd", background=PANEL2)

        tip = mk_label(self,
                       "解锁需要密码 + 填写理由，两者都会记入审计日志。"
                       "解锁后该码仍占用唯一性，重扫仍会判为重复。",
                       bg=BG, fg=DIM, font=F_XS)
        tip.pack(fill="x", padx=18, pady=(8, 0))

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=18, pady=12)
        mk_button(bar, "解锁选中", self.unlock, "primary").pack(side="left", padx=(0, 6))
        mk_button(bar, "查看已解锁", self.show_unlocked, "normal").pack(side="left", padx=3)
        mk_button(bar, "刷新", self.refresh, "ghost").pack(side="left", padx=3)
        mk_button(bar, "关闭", self.on_cancel, "ghost").pack(side="right")
        self._only_active = True

    def refresh(self):
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        rows = self.db.list_locks(only_active=self._only_active)
        for i, r in enumerate(rows):
            self.tv.insert("", "end", iid=str(r["id"]), values=(
                r["part_no"] or "—", r["raw"][:90] or "—", r["locked_at"],
                r["locked_by"] or "—", (r["reason"] or "—")[:50],
            ), tags=("odd",) if i % 2 else ())
        scope = "当前锁定" if self._only_active else "全部记录"
        self.lbl_cnt.configure(text=f"{scope} {len(rows)} 条")

    def show_unlocked(self):
        self._only_active = not self._only_active
        self.refresh()

    def unlock(self):
        sel = self.tv.selection()
        if not sel:
            message(self, "先选一条", "请在列表里点选要解锁的码", "warn")
            return
        if self._only_active is False:
            message(self, "已解锁的不用再解", "这是历史记录，切换到「当前锁定」再操作", "warn")
            return
        if not self.db.has_password():
            message(self, "没有解锁密码", "请先在主界面「设置」里设定解锁密码", "warn")
            return
        pwd = ask_text(self, "解锁验证", "请输入解锁密码：", show="●")
        if not pwd:
            return
        if not self.db.check_password(pwd):
            message(self, "密码不对", "解锁失败，已记录本次尝试", "error")
            self.db.log_audit("未知", "unlock_fail", sel[0], "密码错误")
            return
        reason = ask_text(self, "解锁理由", "为什么要解这个码？\n（会写进审计日志，必填）")
        if not reason or not reason.strip():
            message(self, "理由必填", "解锁必须留下理由，方便事后追溯", "warn")
            return
        ok = self.db.unlock(int(sel[0]), "管理员", reason.strip())
        if ok:
            self.db.log_audit("管理员", "unlock_code", sel[0], reason.strip())
            message(self, "已解锁", f"解锁成功\n\n{now_str()}\n理由：{reason.strip()}", "info")
            self.refresh()
        else:
            message(self, "解锁失败", "这条记录可能已被别人解锁", "warn")
