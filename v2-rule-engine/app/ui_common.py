"""界面公共资源：配色、字体、通用控件。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 配色（Titanium）
BG = "#1C1C1E"
PANEL = "#26262A"
PANEL2 = "#2E2E33"
PANEL3 = "#3A3A40"
LINE = "#3F3F46"

TEXT = "#F1EFE8"
SILVER = "#A1A1AA"
DIM = "#71717A"
GOLD = "#F59E0B"

GREEN = "#3FB950"
YELLOW = "#D29922"
ORANGE = "#D85A30"
RED = "#E24B4A"
DEEPRED = "#A32D2D"
CRIMSON = "#791F1F"

ACTION_BG = {
    "PASS": "#16351E",
    "WARN": "#3A2E12",
    "BLOCK": "#3A1A1A",
    "LOCK_CODE": "#3A1618",
    "LOCK_SCREEN": "#2A0F10",
}
ACTION_FG = {
    "PASS": GREEN,
    "WARN": GOLD,
    "BLOCK": RED,
    "LOCK_CODE": "#F09595",
    "LOCK_SCREEN": "#F09595",
}

# ---------------------------------------------------------------- 字体
F_TITLE = ("Microsoft YaHei UI", 14, "bold")
F_H1 = ("Microsoft YaHei UI", 12, "bold")
F_UI = ("Microsoft YaHei UI", 10)
F_SM = ("Microsoft YaHei UI", 9)
F_XS = ("Microsoft YaHei UI", 8)
F_STATE = ("Microsoft YaHei UI", 30, "bold")
F_MONO = ("Consolas", 11)
F_MONO_SM = ("Consolas", 9)


def style_ttk(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure("Treeview",
                 background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                 rowheight=23, borderwidth=0, font=F_SM)
    st.configure("Treeview.Heading",
                 background=PANEL3, foreground=SILVER, relief="flat",
                 font=("Microsoft YaHei UI", 9, "bold"))
    st.map("Treeview.Heading", background=[("active", PANEL3)])
    st.map("Treeview",
           background=[("selected", PANEL3)],
           foreground=[("selected", GOLD)])
    st.configure("TCombobox",
                 fieldbackground=PANEL3, background=PANEL3, foreground=TEXT,
                 arrowcolor=SILVER, borderwidth=0, padding=3)
    st.map("TCombobox",
           fieldbackground=[("readonly", PANEL3), ("disabled", PANEL2)],
           foreground=[("readonly", TEXT)],
           selectbackground=[("readonly", PANEL3)],
           selectforeground=[("readonly", TEXT)])
    st.configure("TSpinbox", fieldbackground=PANEL3, foreground=TEXT,
                 background=PANEL3, arrowcolor=SILVER, borderwidth=0)
    st.configure("TCheckbutton", background=PANEL, foreground=SILVER, font=F_SM)
    st.map("TCheckbutton", background=[("active", PANEL)],
           foreground=[("active", GOLD)])
    st.configure("TRadiobutton", background=PANEL, foreground=SILVER, font=F_UI)
    st.map("TRadiobutton", background=[("active", PANEL)],
           foreground=[("active", GOLD)])
    st.configure("TNotebook", background=BG, borderwidth=0)
    st.configure("TNotebook.Tab", background=PANEL2, foreground=SILVER,
                 padding=(14, 6), font=F_SM, borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", PANEL3)],
           foreground=[("selected", GOLD)])
    st.configure("Vertical.TScrollbar", background=PANEL3, troughcolor=PANEL,
                 borderwidth=0, arrowcolor=SILVER)
    return st


def mk_button(parent, text, command, kind="normal", width=None, font=F_UI):
    """统一风格的按钮。kind: normal / primary / danger / ghost"""
    palette = {
        "normal": (PANEL3, TEXT, PANEL2),
        "primary": (DEEPRED, "#FFFFFF", CRIMSON),
        "danger": (CRIMSON, "#FFFFFF", DEEPRED),
        "ghost": (PANEL, SILVER, PANEL2),
        "gold": (GOLD, "#1C1C1E", "#D29922"),
    }[kind]
    b = tk.Button(
        parent, text=text, command=command,
        bg=palette[0], fg=palette[1], activebackground=palette[2],
        activeforeground=palette[1], relief="flat", bd=0,
        font=font, padx=14, pady=6, cursor="hand2",
        highlightthickness=0,
    )
    if width:
        b.configure(width=width)
    return b


def mk_entry(parent, textvariable=None, width=30, show=None, font=F_UI, justify="left"):
    e = tk.Entry(
        parent, textvariable=textvariable, width=width, show=show or "",
        bg=PANEL3, fg=TEXT, insertbackground=GOLD, relief="flat",
        font=font, justify=justify, highlightthickness=1,
        highlightbackground=LINE, highlightcolor=GOLD,
    )
    return e


def mk_label(parent, text="", fg=SILVER, bg=PANEL, font=F_UI, **kw):
    return tk.Label(parent, text=text, fg=fg, bg=bg, font=font, **kw)


def section_title(parent, text="", bg=BG):
    """金色竖条 + 可选标题。text 留空时只返回竖条，供调用方自己接文字。"""
    f = tk.Frame(parent, bg=bg)
    tk.Frame(f, bg=GOLD, width=3, height=14).pack(side="left", padx=(0, 7))
    if text:
        tk.Label(f, text=text, bg=bg, fg=TEXT, font=F_H1).pack(side="left")
    return f


class ScrollFrame(tk.Frame):
    """可滚动的容器，条件列表用。"""

    def __init__(self, parent, bg=PANEL, height=220):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, height=height)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()


class Modal(tk.Toplevel):
    """模态窗口基类：居中、置顶、Esc 关闭。"""

    def __init__(self, parent, title, size=(760, 560), resizable=(False, False)):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.geometry(self._center(parent, size))
        self.resizable(*resizable)
        self.transient(parent)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.bind("<Escape>", lambda e: self.on_cancel())
        self.after(60, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    @staticmethod
    def _center(parent, size):
        w, h = size
        try:
            parent.update_idletasks()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
        except Exception:
            x = y = 120
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        x = max(0, min(x, sw - w - 20))
        y = max(0, min(y, sh - h - 60))
        return f"{w}x{h}+{x}+{y}"

    def on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def message(parent, title, text, kind="info"):
    """统一风格的轻提示。"""
    from tkinter import messagebox
    fn = {"info": messagebox.showinfo, "warn": messagebox.showwarning,
          "error": messagebox.showerror}.get(kind, messagebox.showinfo)
    return fn(title, text, parent=parent)


def ask_yes_no(parent, title, text) -> bool:
    from tkinter import messagebox
    return bool(messagebox.askyesno(title, text, parent=parent))


def ask_text(parent, title, prompt, show=None) -> str:
    """简易密码/理由输入框。返回空串表示取消。"""
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(bg=BG)
    top.transient(parent)
    top.resizable(False, False)
    top.geometry(Modal._center(parent, (420, 190)))
    tk.Label(top, text=prompt, bg=BG, fg=TEXT, font=F_UI,
             wraplength=380, justify="left").pack(padx=20, pady=(20, 8), anchor="w")
    ent = mk_entry(top, width=34, show=show)
    ent.pack(padx=20, fill="x", ipady=4)
    out = {"v": ""}

    def ok():
        out["v"] = ent.get()
        top.destroy()

    bar = tk.Frame(top, bg=BG)
    bar.pack(padx=20, pady=16, anchor="e")
    mk_button(bar, "确定", ok, "gold").pack(side="right", padx=(8, 0))
    mk_button(bar, "取消", top.destroy, "ghost").pack(side="right")
    ent.bind("<Return>", lambda e: ok())
    top.grab_set()
    ent.focus_force()
    parent.wait_window(top)
    return out["v"]
