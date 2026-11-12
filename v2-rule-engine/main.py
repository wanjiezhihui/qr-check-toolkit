"""二维码标签校验系统 —— 程序入口。

用法：
    python main.py                  正常启动界面
    python main.py --selftest       跑核心逻辑自检（不开界面）
    python main.py --ui-smoke       界面冒烟（造数→截图前用→自动退出）
    python main.py --demo           演示模式（临时库，自动扫几个样本）
    QRCheck.exe --watchdog PID DIR  看门狗进程（由主程序自动拉起，不用手动跑）
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path


def app_dir() -> Path:
    """单文件 exe 下 __file__ 指向临时解压目录，必须用可执行文件所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log_crash(base: Path, exc_info):
    try:
        p = base / "data" / "crash.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            traceback.print_exception(*exc_info, file=f)
    except Exception:
        pass


def setup_streams(base: Path):
    """windowed 模式下 sys.stdout/stderr 是 None，任何写入都会静默崩溃。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        try:
            p = base / "data" / "crash.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            sys.stderr = open(p, "a", encoding="utf-8", buffering=1)
        except Exception:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ==================================================================== 冒烟 / 演示
def ui_smoke(base: Path) -> int:
    import tkinter as tk
    from app.actions import enable_dpi_awareness
    from app.engine import Condition, Rule
    from app.ui_main import App

    enable_dpi_awareness()
    tmp = Path(tempfile.mkdtemp(prefix="qrsmoke_"))
    root = tk.Tk()
    app = App(root, tmp)

    def seed():
        db = app.db
        db.save_rule(Rule(
            part_no="3000000003", rule_name="3000000003 主标签",
            identify_type="PREFIX", identify_value="3000000003",
            on_pass="PASS", on_violation="BLOCK", on_dup="LOCK_CODE",
            conditions=[Condition(ctype="CONTAINS", operand="3000000003"),
                        Condition(ctype="CONTAINS", operand="8888"),
                        Condition(ctype="ENDS_WITH", operand="008")],
        ))
        db.save_rule(Rule(
            part_no="400004", rule_name="400004",
            identify_type="PREFIX", identify_value="400004",
            on_violation="WARN", on_dup="LOCK_CODE",
            conditions=[Condition(ctype="SEGMENT", operand="555555",
                                  op_param='{"sep":"/","idx":1,"cmp":"EQUALS"}')],
        ))
        app.svc.reload_rules()
        app._refresh_all()
        for code in ("3000000003-88888888-008", "400004/555555",
                     "3000000003-88888888-008", "UNKNOWN-CODE"):
            app._commit(code)
        root.after(900, root.destroy)

    root.after(500, seed)
    root.mainloop()
    print("UI_SMOKE_OK")
    return 0


def demo(base: Path) -> int:
    import tkinter as tk
    from app.actions import enable_dpi_awareness
    from app.engine import Condition, Rule
    from app.ui_main import App

    enable_dpi_awareness()
    tmp = Path(tempfile.mkdtemp(prefix="qrdemo_"))
    root = tk.Tk()
    app = App(root, tmp)

    def seed():
        db = app.db
        db.save_rule(Rule(
            part_no="3000000003", rule_name="3000000003 主标签",
            identify_type="PREFIX", identify_value="3000000003",
            on_pass="PASS", on_violation="BLOCK", on_dup="LOCK_CODE",
            conditions=[Condition(ctype="CONTAINS", operand="3000000003"),
                        Condition(ctype="CONTAINS", operand="8888"),
                        Condition(ctype="ENDS_WITH", operand="008")],
        ))
        db.save_rule(Rule(
            part_no="10000000001", rule_name="10000000001",
            identify_type="PREFIX", identify_value="10000000001",
            on_violation="BLOCK", on_dup="LOCK_CODE",
            conditions=[Condition(ctype="SEGMENT", operand="DM-A0000-000000000",
                                  op_param='{"sep":"%","idx":1,"cmp":"EQUALS"}')],
        ))
        app.svc.reload_rules()
        app._refresh_all()
        for code in (
            "3000000003-88888888-008",
            "10000000001%DM-A0000-000000000%1%1%1%261110%%202611101794290805404220443%280",
            "3000000003-99999-008",
            "3000000003-88888888-008",
            "ZZZ-NOT-REGISTERED",
        ):
            app._commit(code)

    root.after(800, seed)
    root.mainloop()
    return 0


# ==================================================================== 主入口
def main() -> int:
    base = app_dir()
    # windowed 版没有控制台，自检结果得落文件才看得到
    if "--selftest" in sys.argv:
        try:
            (base / "data").mkdir(parents=True, exist_ok=True)
            f = open(base / "data" / "selftest.log", "w", encoding="utf-8")
            sys.stdout = f
            sys.stderr = f
        except Exception:
            pass
    setup_streams(base)

    if "--version" in sys.argv:
        from app import __version__
        print(__version__)
        return 0

    if "--watchdog" in sys.argv:
        i = sys.argv.index("--watchdog")
        try:
            pid = int(sys.argv[i + 1])
            run_dir = sys.argv[i + 2]
        except Exception:
            return 2
        from app.actions import run_watchdog
        return run_watchdog(pid, run_dir)

    if "--selftest" in sys.argv:
        from app.selftest import run
        return run()

    if "--ui-smoke" in sys.argv:
        return ui_smoke(base)

    if "--demo" in sys.argv:
        return demo(base)

    import tkinter as tk
    from app.actions import enable_dpi_awareness
    from app.ui_main import App
    from app.ui_common import message

    enable_dpi_awareness()
    try:
        root = tk.Tk()
    except Exception:
        log_crash(base, sys.exc_info())
        return 1

    root.report_callback_exception = lambda *a: (
        log_crash(base, a), traceback.print_exception(*a))

    try:
        App(root, base)
    except Exception:
        log_crash(base, sys.exc_info())
        try:
            message(root, "启动失败",
                    "程序启动时出错了。\n\n详细信息已写入 data\\crash.log，"
                    "请把这个文件发给维护人员。", "error")
        except Exception:
            pass
        return 1

    try:
        root.mainloop()
    except Exception:
        log_crash(base, sys.exc_info())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
