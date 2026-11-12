"""锁屏（A 档）与看门狗（B 档）。

能力边界（必须如实告知，已在方案文档写明）：
  能拦住：Alt+F4 / Alt+Tab / Alt+Esc / Win 键 / Ctrl+Esc / PrintScreen / Ctrl+Shift+Esc
  拦不住：Ctrl+Alt+Del、Win+L、UAC 安全桌面、注销重启断电
  真锁死需要 C 档 Windows 受控访问（Assigned Access）

安全守则（本模块每一条都为此）：
  1. 钩子回调里**任何异常都放行**，绝不吞掉无法识别的按键。
  2. 只有锁屏窗口存活期间钩子才生效，窗口一关立刻解钩子。
  3. 回调对象保存在实例上，防止被 GC 回收（否则键盘会直接废掉）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

IS_WIN = sys.platform == "win32"

# ============================================================ DPI
def enable_dpi_awareness():
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ============================================================ 键盘钩子
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012

VK_TAB = 0x09
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_SNAPSHOT = 0x2C      # PrintScreen
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_F4 = 0x73


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KeyboardBlocker:
    """低级键盘钩子。只吞特定组合键，其余全部放行。"""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook = None
        self._proc = None          # 必须持有引用，防 GC
        self._active = False
        self._swallowed: list = []
        self._started = threading.Event()

    # ---------------------------------------------------------- 生命周期
    def start(self) -> bool:
        if not IS_WIN:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._active = True
        self._started.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="kb-hook")
        self._thread.start()
        self._started.wait(2.0)
        return self._hook is not None

    def stop(self):
        """解钩子。这一步失败会导致键盘持续被吞，所以尽力而为并兜底。"""
        self._active = False
        try:
            if self._hook:
                ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None
        try:
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        self._thread = None
        self._thread_id = None

    @property
    def active(self) -> bool:
        return self._active

    # ---------------------------------------------------------- 线程内
    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            self._proc = HOOKPROC(self._callback)
            hmod = kernel32.GetModuleHandleW(None)
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, hmod, 0)
            self._thread_id = kernel32.GetCurrentThreadId()
            self._started.set()
            if not self._hook:
                return
            msg = wintypes.MSG()
            while self._active:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got == 0 or got == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            try:
                if self._hook:
                    user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
            self._started.set()

    # ---------------------------------------------------------- 回调
    def _callback(self, nCode, wParam, lParam):
        user32 = ctypes.windll.user32
        try:
            if nCode != 0 or not self._active:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)
            if wParam not in (WM_KEYDOWN, WM_SYSKEYDOWN):
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(kb.vkCode)

            alt = bool(user32.GetAsyncKeyState(VK_MENU) & 0x8000)
            ctrl = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
            shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)

            swallow = False
            if vk in (VK_LWIN, VK_RWIN):
                swallow = True
            elif vk == VK_SNAPSHOT:
                swallow = True
            elif alt and vk in (VK_TAB, VK_ESCAPE, VK_F4, VK_SPACE):
                swallow = True
            elif ctrl and vk == VK_ESCAPE:
                swallow = True
            elif ctrl and shift and vk == VK_ESCAPE:      # 任务管理器
                swallow = True

            if swallow:
                if len(self._swallowed) < 50:
                    self._swallowed.append(vk)
                return 1
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        except Exception:
            # 任何异常都必须放行，否则键盘会整个失效
            try:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)
            except Exception:
                return 0


# ============================================================ 锁屏
class LockScreen:
    """全屏遮罩 + 吞键 + 密码解锁。

    用法：
        ls = LockScreen(root, reason="内容严重违规", check_pwd=fn, on_done=cb)
        ls.show()
    """

    def __init__(self, root, reason: str, check_pwd: Callable[[str], bool],
                 on_done: Optional[Callable[[bool], None]] = None,
                 title: str = "操作已锁定"):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.reason = reason
        self.check_pwd = check_pwd
        self.on_done = on_done
        self.title_text = title
        self.blocker = KeyboardBlocker()
        self.top = None
        self._keep_top_job = None
        self._fails = 0

    def show(self):
        tk = self.tk
        self.top = tk.Toplevel(self.root)
        self.top.title(self.title_text)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        sw = self.top.winfo_screenwidth()
        sh = self.top.winfo_screenheight()
        self.top.geometry(f"{sw}x{sh}+0+0")
        self.top.configure(bg="#141416")
        try:
            self.top.grab_set()
        except Exception:
            pass

        frame = tk.Frame(self.top, bg="#141416")
        frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(frame, text="⚠", bg="#141416", fg="#E24B4A",
                 font=("Microsoft YaHei UI", 52)).pack(pady=(0, 10))
        tk.Label(frame, text=self.title_text, bg="#141416", fg="#E24B4A",
                 font=("Microsoft YaHei UI", 36, "bold")).pack()
        tk.Label(frame, text=self.reason[:300], bg="#141416", fg="#D3D1C7",
                 font=("Microsoft YaHei UI", 13), wraplength=880,
                 justify="center").pack(pady=(18, 32))

        tk.Label(frame, text="请输入解锁密码", bg="#141416", fg="#A1A1AA",
                 font=("Microsoft YaHei UI", 12)).pack()
        self.ent = tk.Entry(frame, show="●", width=26, justify="center",
                            font=("Consolas", 18), bg="#26262A", fg="#F1EFE8",
                            insertbackground="#F1EFE8", relief="flat")
        self.ent.pack(pady=(10, 8), ipady=8)
        self.ent.focus_force()
        self.ent.bind("<Return>", lambda e: self._try_unlock())

        self.msg = tk.Label(frame, text="", bg="#141416", fg="#BA7517",
                            font=("Microsoft YaHei UI", 12))
        self.msg.pack(pady=(2, 14))

        tk.Button(frame, text="解 锁", command=self._try_unlock,
                  bg="#A32D2D", fg="#FFFFFF", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 14, "bold"),
                  activebackground="#791F1F", activeforeground="#FFFFFF",
                  padx=52, pady=11, cursor="hand2").pack()

        tk.Label(frame, text="本界面无法关闭，需正确密码解除",
                 bg="#141416", fg="#71717A",
                 font=("Microsoft YaHei UI", 11)).pack(pady=(26, 0))

        self.blocker.start()
        self._keep_top()
        try:
            self.top.focus_force()
            self.top.lift()
        except Exception:
            pass

    def _keep_top(self):
        """每 800ms 抢回焦点与置顶，防止被其他窗口盖住。"""
        if self.top is None:
            return
        try:
            if self.top.winfo_exists():
                self.top.attributes("-topmost", True)
                self.top.lift()
        except Exception:
            pass
        try:
            self._keep_top_job = self.top.after(800, self._keep_top)
        except Exception:
            self._keep_top_job = None

    def _try_unlock(self):
        pwd = self.ent.get()
        if not pwd:
            return
        ok = False
        try:
            ok = bool(self.check_pwd(pwd))
        except Exception:
            ok = False
        if ok:
            self.close(True)
        else:
            self._fails += 1
            self.ent.delete(0, "end")
            self.msg.configure(text=f"密码不正确（已尝试 {self._fails} 次）")
            try:
                self.top.bell()
            except Exception:
                pass

    def close(self, unlocked: bool = False):
        # 先解钩子，再关窗口——顺序不能反，否则中间有窗口不在但键还被吞的空档
        self.blocker.stop()
        try:
            if self._keep_top_job and self.top:
                self.top.after_cancel(self._keep_top_job)
        except Exception:
            pass
        self._keep_top_job = None
        try:
            if self.top:
                self.top.grab_release()
                self.top.destroy()
        except Exception:
            pass
        self.top = None
        if self.on_done:
            try:
                self.on_done(unlocked)
            except Exception:
                pass


# ============================================================ 看门狗
def pid_alive(pid: int) -> bool:
    if not IS_WIN or pid <= 0:
        return False
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == STILL_ACTIVE
    except Exception:
        return False


def self_cmd() -> list:
    """拿到重新拉起自己的命令行。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(sys.argv[0])]


class WatchdogHandle:
    """在主程序侧启动看门狗，并维护 graceful 标记。"""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file = self.run_dir / "watchdog.pid"
        self.flag = self.run_dir / "graceful.flag"
        self.proc = None

    def clear_flag(self):
        try:
            if self.flag.exists():
                self.flag.unlink()
        except Exception:
            pass

    def mark_graceful(self):
        try:
            self.flag.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

    def _running(self) -> bool:
        try:
            if not self.pid_file.exists():
                return False
            pid = int(self.pid_file.read_text(encoding="utf-8").strip() or 0)
            return pid_alive(pid)
        except Exception:
            return False

    def start(self) -> bool:
        if not IS_WIN:
            return False
        if self._running():
            return True
        try:
            CREATE_NO_WINDOW = 0x08000000
            cmd = self_cmd() + ["--watchdog", str(os.getpid()), str(self.run_dir)]
            self.proc = subprocess.Popen(
                cmd, creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            self.pid_file.write_text(str(self.proc.pid), encoding="utf-8")
            return True
        except Exception:
            return False

    def stop(self):
        """主程序正常退出前调用：写 graceful 标记，看门狗会自己退。"""
        self.mark_graceful()


def run_watchdog(main_pid: int, run_dir: str) -> int:
    """看门狗主循环（独立进程）。主进程被杀 → 3 秒内拉起。"""
    rd = Path(run_dir)
    rd.mkdir(parents=True, exist_ok=True)
    graceful = rd / "graceful.flag"
    pid_file = rd / "watchdog.pid"
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    cmd = self_cmd()
    cur = main_pid
    while True:
        try:
            if graceful.exists():
                try:
                    graceful.unlink()
                except Exception:
                    pass
                return 0
            if not pid_alive(cur):
                if graceful.exists():
                    try:
                        graceful.unlink()
                    except Exception:
                        pass
                    return 0
                CREATE_NO_WINDOW = 0x08000000
                p = subprocess.Popen(
                    cmd, creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
                cur = p.pid
        except Exception:
            pass
        time.sleep(2)
