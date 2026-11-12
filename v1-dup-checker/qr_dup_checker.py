#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二维码标签验重工具  v1.0
=====================================================================
场景
    现场已打印数百张二维码标签（多卷）。用 USB 扫码枪（HID 键盘模式）
    逐张扫一遍，工具实时判断该码是否已经扫过，重复立刻声光报警，
    所有记录即时写入本地 SQLite 数据库，随时一键导出 CSV / TXT / 汇总报告。

依赖
    Python 3.9+ 标准库（tkinter / sqlite3 / winsound / wave / csv），
    无第三方包、无网络、数据不出本机。

运行
    python qr_dup_checker.py              正常启动
    python qr_dup_checker.py --ui-smoke   界面冒烟测试（1.5 秒后自动关闭）
    python qr_dup_checker.py --selftest   无界面逻辑自检（不弹窗）

快捷键
    F2 撤销上一条   F3 手动补录   F4 导出CSV   F5 导出TXT   F6 汇总报告
    F7 改批次名     F8 窗口置顶   F9 声音开关  Esc 退出

作者：星海听澜
"""

from __future__ import annotations

import csv
import math
import os
import queue
import sqlite3
import struct
import sys
import threading
import time
import traceback
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

try:
    import winsound  # Windows 专用
    HAS_WINSOUND = True
except Exception:
    HAS_WINSOUND = False

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    HAS_TK = True
except Exception:
    HAS_TK = False

try:
    import serial  # pyserial，串口扫码枪用
    from serial.tools import list_ports
    HAS_SERIAL = True
    try:
        SERIAL_VER = serial.VERSION
    except Exception:
        SERIAL_VER = "?"
except Exception:
    serial = None
    list_ports = None
    HAS_SERIAL = False
    SERIAL_VER = "-"

APP_VERSION = "1.1"
APP_TITLE = "二维码标签验重工具"

if getattr(sys, "frozen", False):
    # PyInstaller 单文件模式下 __file__ 指向临时解压目录，
    # 数据必须落在 exe 所在目录，否则退出即丢。
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
EXPORT_DIR = DATA_DIR / "export"
DB_PATH = DATA_DIR / "qr_records.db"

# ---------------------------------------------------------------- 配色（Titanium）
BG      = "#1C1C1E"
PANEL   = "#242428"
PANEL2  = "#2C2C30"
PANEL3  = "#34343A"
BORDER  = "#3F3F46"
SILVER  = "#A1A1AA"
TEXT    = "#E4E4E7"
GOLD    = "#F59E0B"
RED     = "#EF4444"
RED_DK  = "#7F1D1D"
RED_BG  = "#3B1212"
GREEN   = "#22C55E"
GREEN_DK= "#14532D"

F_UI    = ("Microsoft YaHei UI", 10)
F_UI_B  = ("Microsoft YaHei UI", 10, "bold")
F_SM    = ("Microsoft YaHei UI", 9)
F_TITLE = ("Microsoft YaHei UI", 13, "bold")
F_NUM   = ("Consolas", 26, "bold")
F_CODE  = ("Consolas", 20, "bold")
F_STATE = ("Microsoft YaHei UI", 26, "bold")

MAX_CODE_LEN = 4096      # 超过视为脏数据
RECENT_KEEP = 300        # 最近扫描流保留条数
DUP_LIST_KEEP = 500


# =================================================================== 工具函数
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize(raw: str, ignore_case: bool = False) -> str:
    """验重归一化：去首尾空白；可选忽略大小写。"""
    s = (raw or "").strip()
    return s.upper() if ignore_case else s


def human_len(s: str) -> int:
    """按显示宽度估算长度（中文算 2）。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def clip(s: str, width: int) -> str:
    """按显示宽度截断。"""
    if human_len(s) <= width:
        return s
    out, w = [], 0
    for c in s:
        cw = 2 if ord(c) > 0x2E80 else 1
        if w + cw > width - 1:
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


# =================================================================== 数据模型
@dataclass
class AddResult:
    seq: int            # 全局第几张
    is_dup: bool
    dup_index: int      # 该码第几次出现（1 = 首次）
    first_ts: str       # 首次出现时间
    first_seq: int      # 首次出现时的序号
    first_batch: str    # 首次出现所属批次
    raw: str
    batch: str
    ts: str


# =================================================================== 存储层
class Store:
    """SQLite 存储。每条扫码立即 commit，断电/崩溃不丢已扫数据。

    验重归一化采用双列：norm（精确）与 norm_up（大写）。
    由 self.ignore_case 决定比对用哪一列，避免中途切换模式导致口径分裂。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ignore_case = False
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scans(
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                seq       INTEGER NOT NULL,
                norm      TEXT    NOT NULL,
                norm_up   TEXT    NOT NULL DEFAULT '',
                raw       TEXT    NOT NULL,
                batch     TEXT    NOT NULL DEFAULT '',
                ts        TEXT    NOT NULL,
                dup_index INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(scans)")]
        if "norm_up" not in cols:                     # 兼容旧库
            self.conn.execute("ALTER TABLE scans ADD COLUMN norm_up TEXT NOT NULL DEFAULT ''")
            self.conn.execute("UPDATE scans SET norm_up = UPPER(norm)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_norm    ON scans(norm)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_norm_up ON scans(norm_up)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_batch   ON scans(batch)")
        self.conn.commit()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 归一化
    @property
    def kcol(self) -> str:
        """当前比对使用的列。"""
        return "norm_up" if self.ignore_case else "norm"

    def key(self, raw: str) -> str:
        s = (raw or "").strip()
        return s.upper() if self.ignore_case else s

    def set_mode(self, ignore_case: bool):
        self.ignore_case = bool(ignore_case)

    # ---------------------------------------------------------------- 写入
    def add(self, raw: str, batch: str) -> AddResult:
        s = (raw or "").strip()
        n_exact, n_up = s, s.upper()
        key = n_up if self.ignore_case else n_exact
        k = self.kcol
        ts = now_str()
        with self._lock:
            prev = self.conn.execute(
                f"SELECT seq, ts, batch FROM scans WHERE {k}=? ORDER BY seq LIMIT 1",
                (key,),
            ).fetchone()
            cnt = self.conn.execute(
                f"SELECT COUNT(*) FROM scans WHERE {k}=?", (key,)
            ).fetchone()[0]
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM scans"
            ).fetchone()[0]
            self.conn.execute(
                "INSERT INTO scans(seq,norm,norm_up,raw,batch,ts,dup_index) "
                "VALUES(?,?,?,?,?,?,?)",
                (seq, n_exact, n_up, raw, batch, ts, cnt + 1),
            )
            self.conn.commit()
        return AddResult(
            seq=seq,
            is_dup=cnt > 0,
            dup_index=cnt + 1,
            first_ts=prev[1] if prev else ts,
            first_seq=prev[0] if prev else seq,
            first_batch=prev[2] if prev else batch,
            raw=raw,
            batch=batch,
            ts=ts,
        )

    # ---------------------------------------------------------------- 撤销
    def undo_last(self) -> Optional[Tuple[int, str]]:
        k = self.kcol
        with self._lock:
            row = self.conn.execute(
                f"SELECT id, seq, {k}, raw FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM scans WHERE id=?", (row[0],))
            # 重算受影响码的出现次序
            self.conn.execute(
                f"""
                UPDATE scans SET dup_index = (
                    SELECT COUNT(*) FROM scans s2
                    WHERE s2.{k} = scans.{k} AND s2.seq <= scans.seq
                ) WHERE {k} = ?
                """,
                (row[2],),
            )
            self.conn.commit()
        return row[1], row[3]

    # ---------------------------------------------------------------- 统计
    def stats(self, batch: Optional[str] = None) -> dict:
        k = self.kcol
        where, args = ("WHERE batch=?", (batch,)) if batch else ("", ())
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM scans {where}", args
        ).fetchone()[0]
        uniq = self.conn.execute(
            f"SELECT COUNT(DISTINCT {k}) FROM scans {where}", args
        ).fetchone()[0]
        dup_codes = self.conn.execute(
            f"""SELECT COUNT(*) FROM (
                    SELECT {k} FROM scans {where}
                    GROUP BY {k} HAVING COUNT(*) > 1)""",
            args,
        ).fetchone()[0]
        return {
            "total": total,
            "unique": uniq,
            "dup_rows": total - uniq,
            "dup_codes": dup_codes,
        }

    def batches(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT batch FROM scans WHERE batch<>'' ORDER BY batch"
        ).fetchall()
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- 查询
    def dup_groups(self, batch: Optional[str] = None, limit: int = DUP_LIST_KEEP) -> List[tuple]:
        k = self.kcol
        where, args = ("WHERE batch=?", (batch,)) if batch else ("", ())
        return self.conn.execute(
            f"""
            SELECT {k} AS key, MIN(raw) AS raw, MIN(ts) AS first_ts,
                   MIN(seq) AS first_seq, COUNT(*) AS cnt
            FROM scans {where}
            GROUP BY {k} HAVING COUNT(*) > 1
            ORDER BY first_seq DESC LIMIT ?
            """,
            (*args, limit),
        ).fetchall()

    def dup_detail(self, key: str) -> List[tuple]:
        k = self.kcol
        return self.conn.execute(
            f"SELECT seq, ts, batch FROM scans WHERE {k}=? ORDER BY seq", (key,)
        ).fetchall()

    def count_key(self, key: str) -> int:
        k = self.kcol
        return self.conn.execute(
            f"SELECT COUNT(*) FROM scans WHERE {k}=?", (key,)
        ).fetchone()[0]

    def first_of_key(self, key: str) -> Optional[tuple]:
        k = self.kcol
        return self.conn.execute(
            f"SELECT seq, ts, batch FROM scans WHERE {k}=? ORDER BY seq LIMIT 1", (key,)
        ).fetchone()

    def recent(self, n: int = RECENT_KEEP, batch: Optional[str] = None) -> List[tuple]:
        where, args = ("WHERE batch=?", (batch,)) if batch else ("", ())
        return self.conn.execute(
            f"SELECT seq, ts, raw, dup_index, batch FROM scans {where} "
            f"ORDER BY seq DESC LIMIT ?",
            (*args, n),
        ).fetchall()

    def all_rows(self, batch: Optional[str] = None) -> List[tuple]:
        where, args = ("WHERE batch=?", (batch,)) if batch else ("", ())
        return self.conn.execute(
            f"SELECT seq, raw, ts, dup_index, batch FROM scans {where} ORDER BY seq",
            args,
        ).fetchall()

    def unique_codes(self, batch: Optional[str] = None) -> List[str]:
        """批次内去重后的二维码原串，按首次出现顺序。"""
        k = self.kcol
        if batch:
            sql = f"""
                SELECT s.raw FROM scans s
                WHERE s.batch = ?
                  AND s.id = (SELECT MIN(s2.id) FROM scans s2
                              WHERE s2.{k} = s.{k} AND s2.batch = ?)
                ORDER BY s.seq
            """
            args = (batch, batch)
        else:
            sql = f"""
                SELECT s.raw FROM scans s
                WHERE s.id = (SELECT MIN(s2.id) FROM scans s2 WHERE s2.{k} = s.{k})
                ORDER BY s.seq
            """
            args = ()
        return [r[0] for r in self.conn.execute(sql, args).fetchall()]

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass


# =================================================================== 导出
def export_csv(store: Store, path: Path | str, batch: Optional[str] = None) -> int:
    rows = store.all_rows(batch)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["序号", "二维码内容", "扫描时间", "该码第几次出现", "状态", "批次"])
        for seq, raw, ts, dup_index, b in rows:
            w.writerow([seq, raw, ts, dup_index, "首次" if dup_index == 1 else "重复", b])
    return len(rows)


def export_txt(store: Store, path: Path | str, batch: Optional[str] = None) -> int:
    codes = store.unique_codes(batch)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        for c in codes:
            f.write(c + "\n")
    return len(codes)


def export_report(store: Store, path: Path | str, batch: Optional[str] = None) -> int:
    s = store.stats(batch)
    g = store.stats(None)
    lines: List[str] = []
    lines.append("=" * 66)
    lines.append(f"二维码标签验重 · 汇总报告")
    lines.append("=" * 66)
    lines.append(f"生成时间   : {now_str()}")
    lines.append(f"数据库文件 : {store.path}")
    lines.append(f"统计范围   : {'批次「' + batch + '」' if batch else '全部记录'}")
    lines.append("")
    lines.append("-" * 66)
    lines.append("一、统计")
    lines.append("-" * 66)
    lines.append(f"总扫描次数        : {s['total']}")
    lines.append(f"唯一二维码数量    : {s['unique']}")
    lines.append(f"重复扫描次数      : {s['dup_rows']}")
    lines.append(f"出现重复的码数量  : {s['dup_codes']}")
    if s["total"]:
        rate = s["unique"] / s["total"] * 100
        lines.append(f"不重复占比        : {rate:.2f}%  ({s['unique']}/{s['total']})")
    lines.append("")
    lines.append(f"（全库累计：总扫 {g['total']} / 唯一 {g['unique']} / 重复 {g['dup_rows']}）")
    lines.append("")

    groups = store.dup_groups(batch)
    lines.append("-" * 66)
    lines.append(f"二、重复明细（共 {len(groups)} 个码出现重复）")
    lines.append("-" * 66)
    if not groups:
        lines.append("无重复。")
    else:
        for i, (norm, raw, first_ts, first_seq, cnt) in enumerate(groups, 1):
            lines.append(f"[{i}] {raw}")
            lines.append(f"    出现次数 : {cnt}")
            det = store.dup_detail(norm)
            for j, (seq, ts, b) in enumerate(det, 1):
                tag = "首次" if j == 1 else f"重复{j-1}"
                lines.append(f"      - 第{seq}张  {ts}  [{tag}]  {b}")
            lines.append("")
    lines.append("=" * 66)
    lines.append("报告结束")
    lines.append("=" * 66)
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))
    return len(groups)


# =================================================================== 提示音
def _synth_wav(path: Path, segments: Sequence[Tuple[float, int, float]], rate: int = 22050) -> Path:
    """合成 16bit PCM 单声道 wav。segments = [(频率Hz, 毫秒, 振幅0~1)]，频率 0 = 静音。"""
    frames = bytearray()
    fade = max(1, int(rate * 0.004))
    for freq, ms, amp in segments:
        n = int(rate * ms / 1000)
        if freq <= 0:
            frames += b"\x00\x00" * n
            continue
        step = 2.0 * math.pi * freq / rate
        for i in range(n):
            env = 1.0
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = (n - i) / fade
            v = 1.0 if math.sin(step * i) >= 0 else -1.0
            s = int(amp * env * 32767 * v * 0.72)
            s = max(-32768, min(32767, s))
            frames += struct.pack("<h", s)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class Sound:
    """声音报警。走系统音频输出（可外接音箱），比主板蜂鸣器响得多。"""

    def __init__(self, data_dir: Path):
        self.enabled = True
        self.paths: dict[str, Path] = {}
        self._q: List[Optional[str]] = []
        self._cv = threading.Condition()
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.paths["ok"] = self._ensure(
                data_dir / "snd_ok.wav", [(1046, 65, 0.28)]
            )
            self.paths["dup"] = self._ensure(
                data_dir / "snd_dup.wav",
                [(1568, 95, 0.60), (0, 55, 0), (1568, 95, 0.60), (0, 55, 0), (1568, 170, 0.60)],
            )
            self.paths["err"] = self._ensure(
                data_dir / "snd_err.wav", [(300, 240, 0.45)]
            )
        except Exception:
            self.paths = {}
        threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def _ensure(path: Path, segs) -> Path:
        if not path.exists() or path.stat().st_size < 100:
            _synth_wav(path, segs)
        return path

    def _worker(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                kind = self._q.pop(0)
            if kind is None:
                return
            p = self.paths.get(kind)
            if not p:
                continue
            try:
                if HAS_WINSOUND:
                    winsound.PlaySound(str(p), winsound.SND_FILENAME)  # 同步，串行不打架
            except Exception:
                pass

    def play(self, kind: str):
        if not self.enabled or kind not in self.paths:
            return
        with self._cv:
            if len(self._q) >= 3:      # 防堆积
                return
            self._q.append(kind)
            self._cv.notify()


# =================================================================== 键盘楔子
class Wedge:
    """扫码枪 = HID 键盘。逐字符累积，遇回车/Tab 视为一码结束。纯逻辑，可单测。"""

    def __init__(self, timeout: float = 5.0):
        self.buf: List[str] = []
        self.timeout = timeout
        self._t = 0.0

    def feed(self, ch: str):
        if not ch:
            return
        self.buf.append(ch)
        self._t = time.monotonic()

    def finish(self) -> str:
        s = "".join(self.buf)
        self.buf = []
        self._t = 0.0
        return s

    def reset(self):
        self.buf = []
        self._t = 0.0

    def expired(self) -> bool:
        return bool(self.buf) and self._t > 0 and (time.monotonic() - self._t) > self.timeout

    @property
    def pending(self) -> str:
        return "".join(self.buf)


# =================================================================== 串口支持
class LineSplitter:
    """串口字节流 -> 一条条扫码内容。遇 \\r 或 \\n 判定一码结束。

    纯逻辑，不依赖硬件，可单测。
    """

    def __init__(self, max_len: int = 8192):
        self.buf = bytearray()
        self.max_len = max_len
        self._dropping = False   # 超长段整段丢弃，直到下一个分隔符

    @staticmethod
    def _decode(raw: bytes) -> str:
        """扫码枪一般输出 ASCII；带中文标签时按 UTF-8 优先、GBK 兜底。

        GBK 中文的字节序列有时恰好是合法 UTF-8（会解成希伯来/西里尔字母），
        所以用「解出来是不是落在非常用区」做一个启发式回退。
        """
        try:
            s = raw.decode("utf-8")
            if any("\u0400" <= ch <= "\u07FF" for ch in s):
                try:
                    return raw.decode("gbk")
                except Exception:
                    return s
            return s
        except Exception:
            pass
        try:
            return raw.decode("gbk")
        except Exception:
            return raw.decode("latin-1", "ignore")

    def feed(self, data: bytes) -> List[str]:
        out: List[str] = []
        for b in data:
            if b in (0x0D, 0x0A):
                if self.buf and not self._dropping:
                    out.append(self._decode(bytes(self.buf)))
                self.buf = bytearray()
                self._dropping = False
            else:
                if self._dropping:
                    continue
                self.buf.append(b)
                if len(self.buf) > self.max_len:
                    self.buf = bytearray()
                    self._dropping = True
        return out

    def reset(self):
        self.buf = bytearray()
        self._dropping = False


class SerialReader(threading.Thread):
    """后台读取串口。读到完整码后回调 on_code（在子线程执行，调用方自行转主线程）。"""

    def __init__(self, port: str, baudrate: int = 9600, on_code=None, on_error=None):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.on_code = on_code or (lambda c: None)
        self.on_error = on_error or (lambda e: None)
        self.ser = None
        self._stop = False
        self.connected = False

    def open(self) -> Tuple[bool, str]:
        if not HAS_SERIAL:
            return False, "未安装 pyserial"
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                write_timeout=0.5,
            )
            self.connected = True
            return True, "ok"
        except Exception as ex:
            self.connected = False
            return False, f"{type(ex).__name__}: {ex}"

    def run(self):
        if not self.connected:
            return
        splitter = LineSplitter()
        while not self._stop:
            try:
                data = self.ser.read(256)
                if data:
                    for code in splitter.feed(data):
                        if code.strip():
                            self.on_code(code)
            except Exception as ex:
                if not self._stop:
                    self.connected = False
                    self.on_error(f"{type(ex).__name__}: {ex}")
                break

    def stop(self):
        self._stop = True
        self.connected = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass


def list_serial_ports() -> List[Tuple[str, str]]:
    """返回 [(device, description)]。"""
    if not HAS_SERIAL:
        return []
    try:
        return [(p.device, p.description or "") for p in list_ports.comports()]
    except Exception:
        return []


# =================================================================== 界面
class App:
    def __init__(self, root, db_path: Optional[Path] = None):
        self.root = root
        self.store = Store(db_path or DB_PATH)
        self.sound = Sound(DATA_DIR)
        self.wedge = Wedge()
        self.batch = datetime.now().strftime("批次_%Y%m%d")
        self.ignore_case = False
        self.topmost = False
        self.last_result: Optional[AddResult] = None
        self._flash_job = None
        self._active = True
        self._repeat_mode = False
        self.serial_reader: Optional[SerialReader] = None
        self.serial_q: "queue.Queue" = queue.Queue()

        root.title(f"{APP_TITLE}  v{APP_VERSION}")
        root.configure(bg=BG)
        self._fit_window()
        root.minsize(min(1000, root.winfo_screenwidth() - 60), 620)
        self._setup_style()
        self._build_ui()
        self._bind_keys()
        self._load_history()
        self._refresh_stats()
        self._tick()
        self._pump_serial()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, lambda: root.focus_force())

    # ---------------------------------------------------------- 尺寸自适应
    def _fit_window(self):
        """按实际屏幕可用区域定窗口，避免高分屏缩放下被系统压缩。"""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1320, int(sw * 0.94))
        h = min(880, int(sh * 0.90))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 10)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---------------------------------------------------------- 样式
    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL, relief="flat")
        st.configure("TLabel", background=BG, foreground=TEXT, font=F_UI)
        st.configure("Dim.TLabel", background=BG, foreground=SILVER, font=F_SM)
        st.configure(
            "TButton",
            background=PANEL3,
            foreground=TEXT,
            font=F_UI,
            borderwidth=0,
            focusthickness=0,
            padding=(12, 7),
        )
        st.map(
            "TButton",
            background=[("active", "#88884D"), ("pressed", "#52525B")],
            foreground=[("active", GOLD)],
        )
        st.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=26,
            borderwidth=0,
            font=F_SM,
        )
        st.configure(
            "Treeview.Heading",
            background=PANEL2,
            foreground=SILVER,
            font=F_SM,
            relief="flat",
            borderwidth=0,
        )
        st.map(
            "Treeview",
            background=[("selected", "#3D3D45")],
            foreground=[("selected", TEXT)],
        )
        st.map("Treeview.Heading", background=[("active", PANEL3)])
        st.configure(
            "Vertical.TScrollbar",
            background=PANEL2,
            troughcolor=PANEL,
            borderwidth=0,
            arrowcolor=SILVER,
        )
        st.configure("TCheckbutton", background=PANEL, foreground=SILVER, font=F_SM)
        st.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", GOLD)])
        st.configure(
            "TCombobox",
            fieldbackground=PANEL3,
            background=PANEL3,
            foreground=TEXT,
            arrowcolor=SILVER,
            borderwidth=0,
        )
        st.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL3)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", PANEL3)],
            selectforeground=[("readonly", TEXT)],
        )

    # ---------------------------------------------------------- 布局
    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ===== 顶部 Banner
        self.banner = tk.Frame(root, bg=PANEL2, height=64)
        self.banner.grid(row=0, column=0, sticky="ew")
        self.banner.grid_propagate(False)
        self.banner.columnconfigure(1, weight=1)
        self.lbl_dot = tk.Label(
            self.banner, text="●", bg=PANEL2, fg=GREEN, font=("Segoe UI", 16, "bold")
        )
        self.lbl_dot.grid(row=0, column=0, padx=(16, 8), pady=14)
        self.lbl_banner = tk.Label(
            self.banner,
            text="监听中 · 等待扫码",
            bg=PANEL2,
            fg=TEXT,
            font=F_TITLE,
            anchor="w",
        )
        self.lbl_banner.grid(row=0, column=1, sticky="ew")
        self.lbl_batch = tk.Label(
            self.banner,
            text="",
            bg=PANEL2,
            fg=GOLD,
            font=F_UI_B,
            anchor="e",
        )
        self.lbl_batch.grid(row=0, column=2, padx=(8, 18))
        self.lbl_focus = tk.Label(
            self.banner, text="", bg=PANEL2, fg=RED, font=F_UI_B, anchor="e"
        )
        self.lbl_focus.grid(row=0, column=3, padx=(0, 18))

        # ===== 主体（左：当前码 / 右：重复清单）
        body = tk.Frame(root, bg=BG)
        body.grid(row=2, column=0, sticky="nsew", padx=14, pady=(12, 0))
        body.columnconfigure(0, weight=58, uniform="col")
        body.columnconfigure(1, weight=42, uniform="col")
        body.rowconfigure(0, weight=1)

        # --- 左
        left = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=0)
        left.rowconfigure(2, weight=1)

        tk.Label(
            left, text="当前扫描内容", bg=PANEL, fg=SILVER, font=F_SM, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))

        self.txt_code = tk.Text(
            left,
            height=3,
            bg=PANEL,
            fg=TEXT,
            font=F_CODE,
            relief="flat",
            highlightthickness=0,
            wrap="char",
            borderwidth=0,
            padx=2,
            pady=2,
        )
        self.txt_code.grid(row=1, column=0, sticky="nsew", padx=16)
        self.txt_code.configure(state="disabled")

        self.lbl_state = tk.Label(
            left,
            text="等 待 扫 码",
            bg=PANEL2,
            fg=SILVER,
            font=F_STATE,
            anchor="center",
        )
        self.lbl_state.grid(row=2, column=0, sticky="nsew", padx=16, pady=(10, 4))

        self.lbl_state_sub = tk.Label(
            left, text="", bg=PANEL, fg=SILVER, font=F_UI, anchor="center"
        )
        self.lbl_state_sub.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 4))

        # 统计三块
        stats = tk.Frame(left, bg=PANEL)
        stats.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 14))
        for i in range(3):
            stats.columnconfigure(i, weight=1, uniform="s")
        self.num_total = self._stat_block(stats, 0, "总扫描", TEXT)
        self.num_uniq = self._stat_block(stats, 1, "唯一码", GREEN)
        self.num_dup = self._stat_block(stats, 2, "重复", RED)

        # --- 右（重复清单）
        right = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        head = tk.Frame(right, bg=PANEL)
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        head.columnconfigure(0, weight=1)
        tk.Label(
            head, text="重复清单", bg=PANEL, fg=RED, font=F_UI_B, anchor="w"
        ).grid(row=0, column=0, sticky="w")
        self.lbl_dup_count = tk.Label(
            head, text="0", bg=PANEL, fg=SILVER, font=F_SM, anchor="e"
        )
        self.lbl_dup_count.grid(row=0, column=1, sticky="e")

        wrap = tk.Frame(right, bg=PANEL)
        wrap.grid(row=1, column=0, sticky="nsew", padx=(14, 6), pady=(0, 12))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        cols = ("cnt", "ts", "code")
        self.tv_dup = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        self.tv_dup.heading("cnt", text="次数")
        self.tv_dup.heading("ts", text="首次时间")
        self.tv_dup.heading("code", text="二维码内容")
        self.tv_dup.column("cnt", width=52, anchor="center", stretch=False)
        self.tv_dup.column("ts", width=140, anchor="w", stretch=False)
        self.tv_dup.column("code", width=260, anchor="w", stretch=True)
        self.tv_dup.grid(row=0, column=0, sticky="nsew")
        sb1 = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_dup.yview)
        sb1.grid(row=0, column=1, sticky="ns")
        self.tv_dup.configure(yscrollcommand=sb1.set)
        self.tv_dup.tag_configure("dup", foreground="#FCA5A5")

        # ===== 最近扫描流
        flow = tk.Frame(root, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        flow.grid(row=3, column=0, sticky="ew", padx=14, pady=(10, 0))
        flow.columnconfigure(0, weight=1)
        tk.Label(
            flow, text="最近扫描流", bg=PANEL, fg=SILVER, font=F_SM, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(8, 2))
        fwrap = tk.Frame(flow, bg=PANEL)
        fwrap.grid(row=1, column=0, sticky="ew", padx=(14, 6), pady=(0, 10))
        fwrap.columnconfigure(0, weight=1)
        fcols = ("seq", "state", "code", "ts")
        self.tv_recent = ttk.Treeview(
            fwrap, columns=fcols, show="headings", height=5, selectmode="browse"
        )
        self.tv_recent.heading("seq", text="序号")
        self.tv_recent.heading("state", text="状态")
        self.tv_recent.heading("code", text="二维码内容")
        self.tv_recent.heading("ts", text="时间")
        self.tv_recent.column("seq", width=70, anchor="center", stretch=False)
        self.tv_recent.column("state", width=80, anchor="center", stretch=False)
        self.tv_recent.column("code", width=620, anchor="w", stretch=True)
        self.tv_recent.column("ts", width=150, anchor="w", stretch=False)
        self.tv_recent.grid(row=0, column=0, sticky="ew")
        sb2 = ttk.Scrollbar(fwrap, orient="vertical", command=self.tv_recent.yview)
        sb2.grid(row=0, column=1, sticky="ns")
        self.tv_recent.configure(yscrollcommand=sb2.set)
        self.tv_recent.tag_configure("dup", foreground="#FCA5A5")
        self.tv_recent.tag_configure("ok", foreground=TEXT)

        # ===== 按钮栏
        bar = tk.Frame(root, bg=BG)
        bar.grid(row=4, column=0, sticky="ew", padx=14, pady=(12, 6))
        btns = [
            ("F2 撤销上一条", self.on_undo),
            ("F3 手动补录", self.on_manual),
            ("F4 导出 CSV", lambda: self.on_export("csv")),
            ("F5 导出 TXT(唯一码)", lambda: self.on_export("txt")),
            ("F6 汇总报告", lambda: self.on_export("report")),
            ("F7 批次名", self.on_rename_batch),
            ("F8 置顶", self.on_toggle_topmost),
            ("F9 声音", self.on_toggle_sound),
            ("Esc 退出", self.on_close),
        ]
        for txt, cb in btns:
            ttk.Button(bar, text=txt, command=cb).pack(side="left", padx=(0, 8))

        self.var_case = tk.BooleanVar(value=False)
        self.chk_case = ttk.Checkbutton(
            bar,
            text="忽略大小写",
            variable=self.var_case,
            command=self.on_toggle_case,
        )
        self.chk_case.pack(side="right")

        # ===== 状态栏
        sb = tk.Frame(root, bg=PANEL2)
        sb.grid(row=5, column=0, sticky="ew")
        sb.columnconfigure(0, weight=1)
        self.status = tk.Label(
            sb,
            text="就绪 · 扫码枪回车后缀已开启、输入法处于英文状态",
            bg=PANEL2,
            fg=SILVER,
            font=F_SM,
            anchor="w",
            padx=14,
            pady=5,
        )
        self.status.grid(row=0, column=0, sticky="ew")
        self.lbl_source = tk.Label(
            sb,
            text="输入源：键盘（HID）　F10 切换",
            bg=PANEL2,
            fg=GOLD,
            font=F_SM,
            anchor="e",
            padx=14,
            pady=5,
            cursor="hand2",
        )
        self.lbl_source.grid(row=0, column=1, sticky="e")
        self.lbl_source.bind("<Button-1>", lambda e: self.on_input_source())

        self._sync_batch_label()

    def _stat_block(self, parent, col, title, color):
        f = tk.Frame(parent, bg=PANEL2)
        f.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
        num = tk.Label(f, text="0", bg=PANEL2, fg=color, font=F_NUM)
        num.pack(pady=(6, 0))
        tk.Label(f, text=title, bg=PANEL2, fg=SILVER, font=F_SM).pack(pady=(0, 6))
        return num

    # ---------------------------------------------------------- 按键
    def _bind_keys(self):
        root = self.root
        root.bind_all("<KeyPress>", self._on_key)
        root.bind("<F2>", lambda e: (self.on_undo(), "break")[1])
        root.bind("<F3>", lambda e: (self.on_manual(), "break")[1])
        root.bind("<F4>", lambda e: (self.on_export("csv"), "break")[1])
        root.bind("<F5>", lambda e: (self.on_export("txt"), "break")[1])
        root.bind("<F6>", lambda e: (self.on_export("report"), "break")[1])
        root.bind("<F7>", lambda e: (self.on_rename_batch(), "break")[1])
        root.bind("<F8>", lambda e: (self.on_toggle_topmost(), "break")[1])
        root.bind("<F9>", lambda e: (self.on_toggle_sound(), "break")[1])
        root.bind("<F10>", lambda e: (self.on_input_source(), "break")[1])
        root.bind("<Escape>", lambda e: (self.on_close(), "break")[1])

    IGNORED_KEYS = {
        "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Caps_Lock", "Num_Lock", "Scroll_Lock", "Insert", "Delete", "BackSpace",
        "Super_L", "Super_R", "Menu", "Win_L", "Win_R", "Tab",
    }

    def _on_key(self, e):
        # 功能键/修饰键放行给具体绑定
        if e.keysym.startswith("F") and e.keysym[1:].isdigit():
            return None
        if e.keysym in ("Escape", "Tab", "BackSpace"):
            return None
        if e.state & 0x4 or e.state & 0x8:   # Ctrl / Alt
            return None
        if e.keysym in self.IGNORED_KEYS:
            return None

        ch = e.char
        if ch in ("\r", "\n"):
            code = self.wedge.finish()
            if code.strip():
                self._commit(code)
            return "break"
        if ch and ch.isprintable():
            self.wedge.feed(ch)
            return "break"
        return None

    # ---------------------------------------------------------- 核心处理
    def _commit(self, code: str):
        raw = code.strip()
        if not raw:
            return
        if len(raw) > MAX_CODE_LEN:
            self.sound.play("err")
            self._set_state("无 效 码", "内容超长，已忽略", RED, RED_BG)
            return

        res = self.store.add(raw, self.batch)
        self.last_result = res

        if res.is_dup:
            self.sound.play("dup")
            has_prev = f"{res.first_ts} · 第 {res.first_seq} 张"
            if res.first_batch and res.first_batch != self.batch:
                has_prev += f" · {res.first_batch}"
            self._set_state(
                "重 复 ！",
                f"该码第 {res.dup_index} 次出现　首次：{has_prev}",
                RED,
                RED_BG,
            )
            self._banner_warn(
                f"重复码：{clip(raw, 46)}　首次出现于 {has_prev}"
            )
            self._flash()
            self._upsert_dup(raw)
        else:
            self.sound.play("ok")
            self._set_state(
                "首 次 通 过", f"第 {res.seq} 张　{res.ts}", GREEN, GREEN_DK
            )
            self._banner_listen()

        self._show_code(raw)
        self._push_recent(res)
        self._refresh_stats()

    def _show_code(self, code: str):
        self.txt_code.configure(state="normal")
        self.txt_code.delete("1.0", "end")
        self.txt_code.insert("1.0", code)
        self.txt_code.configure(state="disabled")

    def _set_state(self, main: str, sub: str, fg: str, bg: str):
        self.lbl_state.configure(text=main, fg=fg, bg=bg)
        self.lbl_state_sub.configure(text=sub, fg=fg)

    def _banner_warn(self, text: str):
        self.lbl_dot.configure(fg=RED)
        self.banner.configure(bg=RED_BG)
        for w in (self.lbl_dot, self.lbl_banner, self.lbl_batch, self.lbl_focus):
            w.configure(bg=RED_BG)
        self.lbl_banner.configure(text=text, fg="#FECACA")

    def _banner_listen(self):
        self.lbl_dot.configure(fg=GREEN)
        self.banner.configure(bg=PANEL2)
        for w in (self.lbl_dot, self.lbl_banner, self.lbl_batch, self.lbl_focus):
            w.configure(bg=PANEL2)
        self.lbl_banner.configure(text="监听中 · 等待扫码", fg=TEXT)

    def _flash(self, times: int = 3):
        if self._flash_job:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None
        seq = [RED_BG if i % 2 == 0 else PANEL2 for i in range(times * 2)]

        def step(i):
            if i >= len(seq):
                self.lbl_state.configure(bg=RED_BG)
                self._flash_job = None
                return
            try:
                self.lbl_state.configure(bg=seq[i])
            except Exception:
                return
            self._flash_job = self.root.after(120, lambda: step(i + 1))

        step(0)

    def _upsert_dup(self, raw: str):
        key = self.store.key(raw)
        cnt = self.store.count_key(key)
        first = self.store.first_of_key(key)
        vals = (cnt, first[1] if first else "", clip(raw, 60))
        if self.tv_dup.exists(key):
            self.tv_dup.item(key, values=vals)
        else:
            self.tv_dup.insert("", 0, iid=key, values=vals, tags=("dup",))
            kids = self.tv_dup.get_children()
            if len(kids) > DUP_LIST_KEEP:
                self.tv_dup.delete(*kids[DUP_LIST_KEEP:])
        self.lbl_dup_count.configure(text=str(len(self.tv_dup.get_children())))

    def _push_recent(self, res: AddResult):
        self.tv_recent.insert(
            "",
            0,
            values=(
                res.seq,
                "重复" if res.is_dup else "首次",
                clip(res.raw, 90),
                res.ts,
            ),
            tags=("dup" if res.is_dup else "ok",),
        )
        kids = self.tv_recent.get_children()
        if len(kids) > RECENT_KEEP:
            self.tv_recent.delete(*kids[RECENT_KEEP:])

    def _refresh_stats(self):
        s = self.store.stats(self.batch)
        self.num_total.configure(text=str(s["total"]))
        self.num_uniq.configure(text=str(s["unique"]))
        self.num_dup.configure(text=str(s["dup_rows"]))

    def _load_history(self):
        for seq, ts, raw, dup_index, b in self.store.recent(RECENT_KEEP, self.batch):
            self.tv_recent.insert(
                "",
                "end",
                values=(seq, "重复" if dup_index > 1 else "首次", clip(raw, 90), ts),
                tags=("dup" if dup_index > 1 else "ok",),
            )
        for key, raw, first_ts, first_seq, cnt in self.store.dup_groups(self.batch):
            self.tv_dup.insert(
                "", "end", iid=key, values=(cnt, first_ts, clip(raw, 60)), tags=("dup",)
            )
        self.lbl_dup_count.configure(text=str(len(self.tv_dup.get_children())))

    # ---------------------------------------------------------- 定时任务
    def _tick(self):
        if not self._active:
            return
        try:
            # 焦点检测：焦点不在本程序时扫码无效，必须提示
            focused = self.root.focus_displayof() is not None
            if focused != getattr(self, "_focus_prev", None):
                self._focus_prev = focused
                self.lbl_focus.configure(
                    text="" if focused else "⚠ 窗口未激活 · 扫码无效"
                )
            # 缓冲超时保护（防止半截码卡死）
            if self.wedge.expired():
                lost = self.wedge.pending
                self.wedge.reset()
                self.status.configure(
                    text=f"⚠ 输入超时已重置（残留 {len(lost)} 字符：{clip(lost, 40)}）"
                )
        except Exception:
            pass
        self.root.after(250, self._tick)

    def _sync_batch_label(self):
        self.lbl_batch.configure(text=f"批次：{self.batch}")

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception as ex:
            messagebox.showerror("出错了", f"{type(ex).__name__}: {ex}")

    # ---------------------------------------------------------- 动作
    def on_undo(self):
        def _go():
            r = self.store.undo_last()
            if not r:
                self.status.configure(text="没有可撤销的记录")
                return
            seq, raw = r
            kids = self.tv_recent.get_children()
            if kids:
                self.tv_recent.delete(kids[0])
            # 清掉已不重复的码
            key = self.store.key(raw)
            cnt = self.store.count_key(key)
            if cnt <= 1 and self.tv_dup.exists(key):
                self.tv_dup.delete(key)
            elif cnt > 1 and self.tv_dup.exists(key):
                first = self.store.first_of_key(key)
                self.tv_dup.item(key, values=(cnt, first[1] if first else "", clip(raw, 60)))
            self.lbl_dup_count.configure(text=str(len(self.tv_dup.get_children())))
            self._show_code("")
            self._set_state("已 撤 销", f"删除第 {seq} 张：{clip(raw, 40)}", GOLD, PANEL2)
            self._refresh_stats()
            self.status.configure(text=f"已撤销第 {seq} 张")

        self._safe(_go)

    def on_manual(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("手动补录")
        dlg.configure(bg=BG)
        dlg.geometry("560x230")
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(
            dlg, text="扫码枪不便时，手动粘贴/输入二维码内容", bg=BG, fg=SILVER, font=F_UI
        ).pack(anchor="w", padx=18, pady=(16, 6))
        var = tk.StringVar()
        ent = tk.Entry(
            dlg, textvariable=var, bg=PANEL2, fg=TEXT, insertbackground=GOLD,
            relief="flat", font=("Consolas", 14), width=52
        )
        ent.pack(padx=18, fill="x", ipady=6)
        ent.focus_set()

        def ok(_=None):
            v = var.get().strip()
            dlg.destroy()
            if v:
                self._commit(v)
                self.root.focus_force()

        ent.bind("<Return>", ok)
        row = tk.Frame(dlg, bg=BG)
        row.pack(pady=16)
        ttk.Button(row, text="确定录入", command=ok).pack(side="left", padx=6)
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)

    def on_export(self, kind: str):
        def _go():
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            sub = {"csv": "CSV 表格", "txt": "TXT 唯一码", "report": "汇总报告"}[kind]
            ext = {"csv": ".csv", "txt": ".txt", "report": ".txt"}[kind]
            init = EXPORT_DIR / f"二维码验重_{self.batch}_{kind}_{stamp()}{ext}"
            path = filedialog.asksaveasfilename(
                title=f"导出{sub}",
                initialdir=str(EXPORT_DIR),
                initialfile=init.name,
                defaultextension=ext,
                filetypes=[(sub, "*" + ext), ("所有文件", "*.*")],
            )
            if not path:
                return
            p = Path(path)
            if kind == "csv":
                n = export_csv(self.store, p, self.batch)
                msg = f"已导出 {n} 条记录（批次「{self.batch}」）"
            elif kind == "txt":
                n = export_txt(self.store, p, self.batch)
                msg = f"已导出 {n} 个唯一二维码"
            else:
                n = export_report(self.store, p, self.batch)
                msg = f"报告已生成，含 {n} 个重复码明细"
            self.status.configure(text=f"{msg} → {p}")
            self.root.focus_force()

        self._safe(_go)

    def on_rename_batch(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("设置批次名")
        dlg.configure(bg=BG)
        dlg.geometry("460x180")
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(
            dlg, text="建议按卷/按天命名，例如：卷1 / 2026-11-11-A", bg=BG, fg=SILVER, font=F_UI
        ).pack(anchor="w", padx=18, pady=(16, 6))
        var = tk.StringVar(value=self.batch)
        ent = tk.Entry(
            dlg, textvariable=var, bg=PANEL2, fg=TEXT, insertbackground=GOLD,
            relief="flat", font=F_UI_B, width=40
        )
        ent.pack(padx=18, fill="x", ipady=6)
        ent.focus_set()
        ent.select_range(0, "end")

        def ok(_=None):
            v = var.get().strip()
            dlg.destroy()
            if v:
                self.batch = v
                self._sync_batch_label()
                self.tv_recent.delete(*self.tv_recent.get_children())
                self.tv_dup.delete(*self.tv_dup.get_children())
                self.lbl_dup_count.configure(text="0")
                self._load_history()
                self._refresh_stats()
                self.status.configure(text=f"批次已切换为「{v}」（验重仍跨批次全局生效）")
            self.root.focus_force()

        ent.bind("<Return>", ok)
        row = tk.Frame(dlg, bg=BG)
        row.pack(pady=16)
        ttk.Button(row, text="确定", command=ok).pack(side="left", padx=6)
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)

    def on_toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)
        self.status.configure(text=f"窗口置顶：{'开' if self.topmost else '关'}")
        self.root.focus_force()

    def on_toggle_sound(self):
        self.sound.enabled = not self.sound.enabled
        self.status.configure(text=f"声音报警：{'开' if self.sound.enabled else '关'}")
        self.root.focus_force()

    def on_toggle_case(self):
        self.ignore_case = bool(self.chk_case.instate(["selected"]))
        self.store.set_mode(self.ignore_case)
        self.tv_recent.delete(*self.tv_recent.get_children())
        self.tv_dup.delete(*self.tv_dup.get_children())
        self._load_history()
        self._refresh_stats()
        self.status.configure(
            text=f"忽略大小写：{'开' if self.ignore_case else '关'}（历史记录已按新口径重新判定）"
        )
        self.root.focus_force()

    # ---------------------------------------------------------- 输入源（键盘 / 串口）
    def _serial_code_in(self, code: str):
        """串口线程回调：只投递，不碰控件。"""
        self.serial_q.put(("code", code))

    def _serial_err_in(self, msg: str):
        self.serial_q.put(("error", msg))

    def _pump_serial(self):
        if not self._active:
            return
        try:
            while True:
                kind, payload = self.serial_q.get_nowait()
                if kind == "code":
                    self._commit(payload)
                elif kind == "error":
                    self.lbl_source.configure(
                        text="输入源：串口已断开　F10 重连", fg=RED
                    )
                    self.status.configure(text=f"串口错误：{payload}")
                    self.serial_reader = None
        except queue.Empty:
            pass
        self.root.after(60, self._pump_serial)

    def _stop_serial(self):
        if self.serial_reader:
            self.serial_reader.stop()
            self.serial_reader = None

    def _start_serial(self, port: str, baud: int) -> bool:
        self._stop_serial()
        rd = SerialReader(
            port, baud, on_code=self._serial_code_in, on_error=self._serial_err_in
        )
        ok, msg = rd.open()
        if not ok:
            messagebox.showerror(
                "串口打开失败",
                f"{port} @ {baud}\n\n{msg}\n\n常见原因：\n"
                f"· 端口被别的程序占用（扫码枪厂商配置工具最常见）\n"
                f"· 波特率不匹配（扫码枪出厂多为 9600）\n"
                f"· 线没插好 / USB 转串口驱动没装\n"
                f"· 该 COM 口根本不是扫码枪（本机现有："
                f"{', '.join(d for d, _ in list_serial_ports()) or '无'}）",
            )
            return False
        self.serial_reader = rd
        rd.start()
        self.lbl_source.configure(text=f"输入源：串口 {port} @ {baud}　F10 切换", fg=GOLD)
        self.status.configure(text=f"串口已连接 {port} @ {baud}，请扫一张测试是否出结果")
        return True

    def on_input_source(self):
        ports = list_serial_ports()
        dlg = tk.Toplevel(self.root)
        dlg.title("输入源设置 —— 先确认扫码枪是哪种接法")
        dlg.configure(bg=BG)
        dlg.geometry("700x520")
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(
            dlg,
            text="扫码枪有两种接法，先确认你手上这支属于哪一种：",
            bg=BG, fg=TEXT, font=F_UI_B,
        ).pack(anchor="w", padx=20, pady=(18, 8))

        tip = (
            "① USB 键盘模式（HID 键盘）—— 绝大多数枪都是这种\n"
            "   插上电脑就被当成一个键盘，扫一次等于打一串字再加回车。\n"
            "   设备管理器里出现在「键盘」类别下，不占用 COM 口。\n"
            "   → 选「键盘模式」，什么都不用配。\n\n"
            "② 串口模式（COM 口）—— USB 虚拟串口 或 RS232 转 USB\n"
            "   程序必须主动打开 COM 口读字节流，键盘监听收不到任何东西。\n"
            "   设备管理器里出现在「端口 (COM 和 LPT)」下。\n"
            "   → 选「串口模式」，并指定端口与波特率（出厂多为 9600-8-N-1）。\n\n"
            "※ 不确定？插上枪打开「设备管理器」看一眼：\n"
            "   多出来的是键盘 → 选①；多出来的是 COMx → 选②。"
        )
        tk.Label(
            dlg, text=tip, bg=BG, fg=SILVER, font=F_SM, justify="left"
        ).pack(anchor="w", padx=20)

        mode = tk.StringVar(value="serial" if self.serial_reader else "hid")
        f1 = tk.Frame(dlg, bg=BG)
        f1.pack(anchor="w", padx=20, pady=(16, 0))
        for text, val in (("键盘模式（HID）", "hid"), ("串口模式（COM）", "serial")):
            tk.Radiobutton(
                f1, text=text, variable=mode, value=val,
                bg=BG, fg=TEXT, selectcolor=PANEL3,
                activebackground=BG, activeforeground=GOLD,
                font=F_UI, highlightthickness=0, bd=0,
            ).pack(side="left", padx=(0, 24))

        f2 = tk.Frame(dlg, bg=BG)
        f2.pack(anchor="w", padx=20, pady=14, fill="x")
        tk.Label(f2, text="端口：", bg=BG, fg=SILVER, font=F_UI).pack(side="left")

        port_items = [f"{d}   {desc}" for d, desc in ports] or ["（未检测到串口）"]
        cur = ""
        if self.serial_reader:
            cur = next(
                (x for x in port_items if x.startswith(self.serial_reader.port)), ""
            )
        port_var = tk.StringVar(value=cur or port_items[0])
        cb_port = ttk.Combobox(
            f2, textvariable=port_var, values=port_items, width=34, state="readonly"
        )
        cb_port.pack(side="left", padx=(6, 16))

        tk.Label(f2, text="波特率：", bg=BG, fg=SILVER, font=F_UI).pack(side="left")
        baud_var = tk.StringVar(
            value=str(self.serial_reader.baudrate) if self.serial_reader else "9600"
        )
        ttk.Combobox(
            f2, textvariable=baud_var, width=9, state="readonly",
            values=["9600", "19200", "38400", "57600", "115200"],
        ).pack(side="left", padx=6)

        lbl_ports = tk.Label(
            dlg,
            text="本机检测到的串口：" + (", ".join(d for d, _ in ports) if ports else "无"),
            bg=BG, fg=SILVER, font=F_SM,
        )
        lbl_ports.pack(anchor="w", padx=20)

        def refresh():
            ps = list_serial_ports()
            items = [f"{d}   {desc}" for d, desc in ps] or ["（未检测到串口）"]
            cb_port.configure(values=items)
            if port_var.get() not in items:
                port_var.set(items[0])
            lbl_ports.configure(
                text="本机检测到的串口：" + (", ".join(d for d, _ in ps) if ps else "无")
            )

        def apply():
            if mode.get() == "hid":
                self._stop_serial()
                self.lbl_source.configure(
                    text="输入源：键盘（HID）　F10 切换", fg=GOLD
                )
                self.status.configure(text="已切换为键盘模式（扫码枪走 USB 键盘）")
                dlg.destroy()
                self.root.focus_force()
                return
            txt = port_var.get()
            if not txt or txt.startswith("（"):
                messagebox.showwarning(
                    "没有可用串口",
                    "未检测到串口设备。\n\n若你确认枪是串口式的，请检查：\n"
                    "· USB 转串口驱动是否装好\n· 设备管理器里是否能看到 COM 口\n· 换一根数据线试试",
                )
                return
            port = txt.split()[0]
            if self._start_serial(port, int(baud_var.get())):
                dlg.destroy()
                self.root.focus_force()

        row = tk.Frame(dlg, bg=BG)
        row.pack(pady=20)
        ttk.Button(row, text="确定", command=apply).pack(side="left", padx=6)
        ttk.Button(row, text="刷新端口", command=refresh).pack(side="left", padx=6)
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)

    def on_close(self):
        s = self.store.stats(self.batch)
        if s["total"] > 0:
            if not messagebox.askokcancel(
                "确认退出",
                f"当前批次「{self.batch}」已记录 {s['total']} 条，"
                f"其中唯一 {s['unique']}、重复 {s['dup_rows']}。\n\n"
                f"数据已实时保存到：\n{DB_PATH}\n\n确定退出？",
            ):
                return
        self._active = False
        self._stop_serial()
        self.store.close()
        self.root.destroy()


# =================================================================== 自检
def selftest() -> int:
    import tempfile

    out: List[str] = []
    fails = 0

    def chk(name, cond, extra=""):
        nonlocal fails
        if not cond:
            fails += 1
        out.append(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))

    tmp = Path(tempfile.mkdtemp(prefix="qrchk_"))
    db = tmp / "t.db"
    st = Store(db)

    chk("T01 normalize strip", normalize("  ABC ") == "ABC")
    chk("T02 normalize ignore_case", normalize("abc", True) == "ABC")
    chk("T03 normalize default case", normalize("abc") == "abc")

    r1 = st.add("SN0001", "B1")
    chk("T04 first scan not dup", (not r1.is_dup) and r1.seq == 1 and r1.dup_index == 1)
    r2 = st.add("SN0002", "B1")
    chk("T05 second distinct not dup", (not r2.is_dup) and r2.seq == 2)
    r3 = st.add("SN0001", "B1")
    chk(
        "T06 repeat detected",
        r3.is_dup and r3.dup_index == 2 and r3.first_seq == 1,
        f"dup_index={r3.dup_index} first_seq={r3.first_seq}",
    )
    r4 = st.add("SN0001", "B1")
    chk("T07 third occurrence", r4.is_dup and r4.dup_index == 3)

    s = st.stats(None)
    chk(
        "T08 stats",
        s["total"] == 4 and s["unique"] == 2 and s["dup_rows"] == 2 and s["dup_codes"] == 1,
        str(s),
    )
    s1 = st.stats("B1")
    chk("T09 stats by batch", s1["total"] == 4)

    # 跨批次重复
    r5 = st.add("SN0002", "B2")
    chk(
        "T10 cross-batch dup detected",
        r5.is_dup and r5.first_batch == "B1",
        f"first_batch={r5.first_batch}",
    )

    # 撤销
    u = st.undo_last()
    chk("T11 undo returns last", u is not None and u[1] == "SN0002")
    s = st.stats(None)
    chk("T12 undo removed row", s["total"] == 4, str(s))

    st.add("SN0001", "B1")
    u = st.undo_last()
    s = st.stats(None)
    di = st.conn.execute(
        "SELECT seq, dup_index FROM scans WHERE norm='SN0001' ORDER BY seq"
    ).fetchall()
    chk(
        "T13 undo recalcs dup_index",
        s["total"] == 4 and di == [(1, 1), (3, 2), (4, 3)],
        str(di),
    )

    # 查询
    groups = st.dup_groups(None)
    chk("T14 dup_groups", len(groups) == 1 and groups[0][4] == 3, str(groups))
    uniq = st.unique_codes(None)
    chk("T15 unique_codes order", uniq == ["SN0001", "SN0002"], str(uniq))
    det = st.dup_detail("SN0001")
    chk("T16 dup_detail", len(det) == 3, str(det))

    # 导出
    c = tmp / "o.csv"
    n = export_csv(st, c, None)
    txt = c.read_text(encoding="utf-8-sig")
    chk("T17 export_csv rows", n == 4 and txt.count("\n") >= 5, f"n={n}")
    chk("T18 export_csv header", "二维码内容" in txt and "状态" in txt)
    chk("T19 export_csv marks dup", "重复" in txt and "首次" in txt)

    t = tmp / "o.txt"
    n = export_txt(st, t, None)
    lines = [x for x in t.read_text(encoding="utf-8").splitlines() if x]
    chk("T20 export_txt uniques", n == 2 and lines == ["SN0001", "SN0002"], str(lines))

    rp = tmp / "o_report.txt"
    n = export_report(st, rp, None)
    body = rp.read_text(encoding="utf-8")
    chk("T21 export_report", n == 1 and "重复明细" in body and "SN0001" in body)

    # 键盘楔子
    w = Wedge()
    for ch in "ABC123":
        w.feed(ch)
    chk("T22 wedge pending", w.pending == "ABC123")
    chk("T23 wedge finish", w.finish() == "ABC123" and w.pending == "")
    chk("T24 wedge empty finish", w.finish() == "")
    w2 = Wedge(timeout=0.0)
    w2.feed("X")
    time.sleep(0.01)
    chk("T25 wedge expired", w2.expired())

    # 大小写验重模式切换
    st2 = Store(tmp / "t2.db")
    st2.add("abc", "B")
    st2.set_mode(True)
    r = st2.add("ABC", "B")
    chk("T26 ignore_case dedup", r.is_dup and r.dup_index == 2, f"dup={r.is_dup} idx={r.dup_index}")
    st2.set_mode(False)
    r = st2.add("ABC", "B")
    chk("T27 exact-mode repeat of ABC", r.is_dup and r.dup_index == 2, f"dup={r.is_dup} idx={r.dup_index}")
    st6 = Store(tmp / "t6.db")
    st6.add("abc", "B")
    r = st6.add("ABC", "B")
    chk("T27b case-sensitive keeps distinct", not r.is_dup, f"dup={r.is_dup}")
    s2 = st2.stats(None)
    chk("T28 exact-mode uniques", s2["total"] == 3 and s2["unique"] == 2, str(s2))
    st2.set_mode(True)
    s2 = st2.stats(None)
    chk("T29 ignore-case uniques", s2["total"] == 3 and s2["unique"] == 1, str(s2))

    # 批次内唯一码（跨批次场景）
    st5 = Store(tmp / "t5.db")
    st5.add("X1", "B1")
    st5.add("X2", "B1")
    st5.add("X1", "B2")
    st5.add("X3", "B2")
    chk("T30 uniques per batch", st5.unique_codes("B2") == ["X1", "X3"], str(st5.unique_codes("B2")))
    chk("T31 dup across batch counted", st5.stats(None)["unique"] == 3)

    # 持久化：重开同一库
    st.close()
    st3 = Store(db)
    s = st3.stats(None)
    chk("T32 persistence across reopen", s["total"] == 4, str(s))
    st3.close()

    # 音频合成
    try:
        p = Path(tempfile.mkdtemp()) / "s.wav"
        _synth_wav(p, [(1000, 50, 0.3), (0, 20, 0)])
        chk("T33 wav synth", p.exists() and p.stat().st_size > 1000, f"{p.stat().st_size}B")
    except Exception as ex:
        chk("T33 wav synth", False, repr(ex))

    # 串口行切分
    sp = LineSplitter()
    chk("T34 splitter single", sp.feed(b"ABC123\r\n") == ["ABC123"])
    chk("T35 splitter partial keeps buffer", sp.feed(b"XYZ") == [] and bytes(sp.buf) == b"XYZ")
    chk("T36 splitter completes across chunks", sp.feed(b"789\n") == ["XYZ789"])
    chk("T37 splitter skips empty", sp.feed(b"\r\n\r\n") == [])
    sp2 = LineSplitter()
    chk("T38 splitter multi lines", sp2.feed(b"A\rB\nC\r\n") == ["A", "B", "C"])
    sp3 = LineSplitter()
    chk("T39 splitter utf8 chinese", sp3.feed("中文码\r".encode("utf-8")) == ["中文码"])
    sp4 = LineSplitter()
    chk("T40 splitter gbk chinese", sp4.feed("中文码\r".encode("gbk")) == ["中文码"])
    sp5 = LineSplitter(max_len=8)
    chk("T41 splitter drops overlong", sp5.feed(b"12345678901234567890\r") == [])
    chk("T42 serial module available", HAS_SERIAL, f"pyserial={SERIAL_VER}")
    chk("T43 list_serial_ports callable", isinstance(list_serial_ports(), list))

    report = "\n".join(out)
    print(report)
    (tmp / "selftest_result.txt").write_text(report, encoding="utf-8")
    print(f"\n=== {'ALL PASS' if fails == 0 else str(fails) + ' FAILED'} ({len(out)} checks) ===")

    # 清理临时目录（只清自己建的临时目录）
    try:
        for f in tmp.rglob("*"):
            if f.is_file():
                f.unlink()
        for d in sorted([x for x in tmp.rglob("*") if x.is_dir()], reverse=True):
            d.rmdir()
        tmp.rmdir()
    except Exception:
        pass
    return 0 if fails == 0 else 1


def ui_smoke() -> int:
    import tempfile

    if not HAS_TK:
        print("tkinter not available")
        return 1
    enable_dpi_awareness()
    root = tk.Tk()
    tmpdb = Path(tempfile.mkdtemp(prefix="qrsmoke_")) / "smoke.db"
    app = App(root, tmpdb)

    def feed_demo():
        for code in ("SN-A-001", "SN-A-002", "SN-A-001"):
            app._commit(code)
        root.after(600, root.destroy)

    root.after(500, feed_demo)
    root.mainloop()
    app.store.close()
    print("UI_SMOKE_OK")
    return 0


def demo() -> int:
    """演示模式：用临时库跑一遍，不污染真实数据。用于培训操作员看报警效果。"""
    import tempfile

    if not HAS_TK:
        print("tkinter not available")
        return 1
    enable_dpi_awareness()
    tmpdb = Path(tempfile.mkdtemp(prefix="qrdemo_")) / "demo.db"
    root = tk.Tk()
    app = App(root, tmpdb)
    demo_codes = [
        "SN-2026-A0001", "SN-2026-A0002", "SN-2026-A0003", "SN-2026-A0004",
        "SN-2026-A0002", "SN-2026-A0005", "SN-2026-A0003",
    ]

    def play():
        for c in demo_codes:
            app._commit(c)

    root.after(900, play)
    root.mainloop()
    return 0


def log_exception(text: str, tag: str = "ERROR"):
    """把异常写进 crash.log，现场电脑出问题时至少有据可查。"""
    try:
        with open(APP_DIR / "crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {now_str()} [{tag}] =====\n{text}\n")
    except Exception:
        pass


def install_crash_logger():
    import traceback

    def hook(t, v, tb):
        log_exception("".join(traceback.format_exception(t, v, tb)), "main")

    sys.excepthook = hook

    def thook(args):
        log_exception(
            "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            ),
            f"thread:{getattr(args.thread, 'name', '?')}",
        )

    try:
        threading.excepthook = thook
    except Exception:
        pass


def fix_std_streams():
    """PyInstaller --windowed 下 sys.stdout/stderr 为 None，
    任何一次写入都会抛异常且完全无声。重定向到空设备。"""
    if sys.stdout is None:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            pass
    if sys.stderr is None:
        try:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            pass


def enable_dpi_awareness():
    """让 Tk 以物理像素渲染，高分屏下文字不模糊、窗口不被系统压缩。"""
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    fix_std_streams()
    install_crash_logger()

    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--ui-smoke" in sys.argv:
        sys.exit(ui_smoke())
    if "--demo" in sys.argv:
        sys.exit(demo())
    if not HAS_TK:
        print("缺少 tkinter，无法启动图形界面。")
        sys.exit(1)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        enable_dpi_awareness()
        root = tk.Tk()
        app = App(root)
        root.report_callback_exception = lambda t, v, tb: (
            log_exception("".join(traceback.format_exception(t, v, tb)), "tk-callback"),
            messagebox.showerror(
                "程序出错",
                f"{v}\n\n错误已记录到：\n{APP_DIR / 'crash.log'}\n\n"
                f"数据已实时保存，不会丢。请把这个文件发给开发。",
            ),
        )
        root.mainloop()
    except Exception as ex:
        import traceback as _tb

        log_exception(_tb.format_exc(), "startup")
        try:
            messagebox.showerror(
                "启动失败",
                f"{type(ex).__name__}: {ex}\n\n错误日志：\n{APP_DIR / 'crash.log'}",
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
