"""SQLite 存储层。

关键设计：
1. `scans.uniq_key` 上建【部分唯一索引】，只对 is_valid=1 且 uniq_key 非空的行生效。
   这样被 BLOCK 的码（is_valid=0, uniq_key=NULL）不占用唯一性，重扫时仍按「首次」处理。
2. 验重范围靠 uniq_key 的构造方式实现：
   GLOBAL → uniq_key = norm
   PART   → uniq_key = part_no + SEP + norm
   NONE   → uniq_key = None（唯一索引允许多个 NULL，等于不参与判重）
3. 每条扫码立即 commit，断电不丢已扫记录。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .engine import Condition, Rule

SEP = "\x1f"          # uniq_key 内部拼接分隔符，不会出现在条码里


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ============================================================ 密码
def hash_password(pwd: str, salt: str) -> str:
    dk = hashlib.scrypt(pwd.encode("utf-8"), salt=salt.encode("utf-8"),
                        n=16384, r=8, p=1, dklen=32)
    return dk.hex()


def new_salt() -> str:
    return os.urandom(16).hex()


# ============================================================ 数据库
class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._init_schema()

    # ---------------------------------------------------------- 建表
    def _init_schema(self):
        with self._lock:
            c = self.conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS rules(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_no TEXT NOT NULL UNIQUE,
                    rule_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    identify_type TEXT NOT NULL DEFAULT 'PREFIX',
                    identify_value TEXT NOT NULL DEFAULT '',
                    identify_param TEXT NOT NULL DEFAULT '',
                    match_logic TEXT NOT NULL DEFAULT 'AND',
                    at_least_n INTEGER NOT NULL DEFAULT 1,
                    dup_scope TEXT NOT NULL DEFAULT 'GLOBAL',
                    on_pass TEXT NOT NULL DEFAULT 'PASS',
                    on_violation TEXT NOT NULL DEFAULT 'BLOCK',
                    on_dup TEXT NOT NULL DEFAULT 'LOCK_CODE',
                    on_unknown TEXT NOT NULL DEFAULT 'BLOCK',
                    escalation TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 100,
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_to TEXT NOT NULL DEFAULT '',
                    remark TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS conditions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    ctype TEXT NOT NULL,
                    operand TEXT NOT NULL DEFAULT '',
                    op_param TEXT NOT NULL DEFAULT '',
                    negate INTEGER NOT NULL DEFAULT 0,
                    required INTEGER NOT NULL DEFAULT 1,
                    fail_action TEXT NOT NULL DEFAULT '',
                    fail_msg TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_cond_rule ON conditions(rule_id, seq);

                CREATE TABLE IF NOT EXISTS scans(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seq INTEGER NOT NULL,
                    raw TEXT NOT NULL,
                    norm TEXT NOT NULL,
                    uniq_key TEXT,
                    part_no TEXT NOT NULL DEFAULT '',
                    rule_id INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL DEFAULT 'PASS',
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    is_dup INTEGER NOT NULL DEFAULT 0,
                    dup_count INTEGER NOT NULL DEFAULT 1,
                    matched_by TEXT NOT NULL DEFAULT 'NONE',
                    terminal TEXT NOT NULL DEFAULT '',
                    batch TEXT NOT NULL DEFAULT '',
                    operator TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    ts TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scan_norm ON scans(norm);
                CREATE INDEX IF NOT EXISTS idx_scan_ts ON scans(ts);
                CREATE INDEX IF NOT EXISTS idx_scan_part ON scans(part_no);
                CREATE INDEX IF NOT EXISTS idx_scan_batch ON scans(batch);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_uniq
                    ON scans(uniq_key) WHERE is_valid=1 AND uniq_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS locks(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    norm TEXT NOT NULL,
                    raw TEXT NOT NULL DEFAULT '',
                    part_no TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    locked_at TEXT NOT NULL,
                    locked_by TEXT NOT NULL DEFAULT '',
                    unlocked_at TEXT NOT NULL DEFAULT '',
                    unlocked_by TEXT NOT NULL DEFAULT '',
                    unlock_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_lock_norm ON locks(norm);

                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
                """
            )
            c.commit()

    # ---------------------------------------------------------- 规则
    def _row_to_rule(self, row: sqlite3.Row) -> Rule:
        return Rule(
            id=row["id"],
            part_no=row["part_no"],
            rule_name=row["rule_name"],
            enabled=bool(row["enabled"]),
            identify_type=row["identify_type"],
            identify_value=row["identify_value"],
            identify_param=row["identify_param"],
            match_logic=row["match_logic"],
            at_least_n=row["at_least_n"],
            dup_scope=row["dup_scope"],
            on_pass=row["on_pass"],
            on_violation=row["on_violation"],
            on_dup=row["on_dup"],
            on_unknown=row["on_unknown"],
            escalation=row["escalation"],
            priority=row["priority"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            remark=row["remark"],
            updated_at=row["updated_at"],
        )

    def _row_to_cond(self, row: sqlite3.Row) -> Condition:
        return Condition(
            id=row["id"],
            seq=row["seq"],
            ctype=row["ctype"],
            operand=row["operand"],
            op_param=row["op_param"],
            negate=bool(row["negate"]),
            required=bool(row["required"]),
            fail_action=row["fail_action"],
            fail_msg=row["fail_msg"],
        )

    def save_rule(self, rule: Rule) -> int:
        """整条规则保存（含条件全量替换）。返回 rule_id。"""
        with self._lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                exist = c.execute(
                    "SELECT id FROM rules WHERE part_no=?", (rule.part_no,)
                ).fetchone()
                data = (
                    rule.rule_name or rule.part_no,
                    int(rule.enabled),
                    rule.identify_type,
                    rule.identify_value,
                    rule.identify_param,
                    rule.match_logic,
                    int(rule.at_least_n),
                    rule.dup_scope,
                    rule.on_pass,
                    rule.on_violation,
                    rule.on_dup,
                    rule.on_unknown,
                    rule.escalation,
                    int(rule.priority),
                    rule.valid_from,
                    rule.valid_to,
                    rule.remark,
                    now_str(),
                )
                if exist:
                    rid = exist["id"]
                    c.execute(
                        """UPDATE rules SET rule_name=?,enabled=?,identify_type=?,identify_value=?,
                        identify_param=?,match_logic=?,at_least_n=?,dup_scope=?,on_pass=?,
                        on_violation=?,on_dup=?,on_unknown=?,escalation=?,priority=?,
                        valid_from=?,valid_to=?,remark=?,updated_at=? WHERE id=?""",
                        (*data, rid),
                    )
                    c.execute("DELETE FROM conditions WHERE rule_id=?", (rid,))
                else:
                    cur = c.execute(
                        """INSERT INTO rules(rule_name,enabled,identify_type,identify_value,
                        identify_param,match_logic,at_least_n,dup_scope,on_pass,on_violation,
                        on_dup,on_unknown,escalation,priority,valid_from,valid_to,remark,
                        updated_at,part_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (*data, rule.part_no),
                    )
                    rid = cur.lastrowid
                for i, cond in enumerate(rule.conditions):
                    c.execute(
                        """INSERT INTO conditions(rule_id,seq,ctype,operand,op_param,
                        negate,required,fail_action,fail_msg) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (rid, i, cond.ctype, cond.operand, cond.op_param,
                         int(cond.negate), int(cond.required),
                         cond.fail_action, cond.fail_msg),
                    )
                c.commit()
                return rid
            except Exception:
                c.rollback()
                raise

    def get_rule(self, rule_id: int) -> Optional[Rule]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            if not row:
                return None
            rule = self._row_to_rule(row)
            rule.conditions = [
                self._row_to_cond(r)
                for r in self.conn.execute(
                    "SELECT * FROM conditions WHERE rule_id=? ORDER BY seq,id", (rule_id,)
                ).fetchall()
            ]
            return rule

    def get_rule_by_part(self, part_no: str) -> Optional[Rule]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM rules WHERE part_no=?", (part_no,)
            ).fetchone()
            if not row:
                return None
            return self.get_rule(row["id"])

    def list_rules(self, only_enabled: bool = False) -> List[Rule]:
        with self._lock:
            sql = "SELECT * FROM rules"
            if only_enabled:
                sql += " WHERE enabled=1"
            sql += " ORDER BY priority, part_no"
            out = []
            for row in self.conn.execute(sql).fetchall():
                rule = self._row_to_rule(row)
                cnt = self.conn.execute(
                    "SELECT COUNT(*) FROM conditions WHERE rule_id=?", (rule.id,)
                ).fetchone()[0]
                rule.conditions = [Condition()] * cnt   # 只占位，供界面显示条数
                out.append(rule)
            return out

    def delete_rule(self, rule_id: int):
        with self._lock:
            self.conn.execute("DELETE FROM conditions WHERE rule_id=?", (rule_id,))
            self.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self.conn.commit()

    def set_enabled(self, rule_id: int, on: bool):
        with self._lock:
            self.conn.execute(
                "UPDATE rules SET enabled=?, updated_at=? WHERE id=?",
                (int(on), now_str(), rule_id),
            )
            self.conn.commit()

    def part_no_exists(self, part_no: str) -> Optional[dict]:
        """料号查重，给输入框实时提醒用。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT id, rule_name, updated_at FROM rules WHERE part_no=?", (part_no,)
            ).fetchone()
            return dict(row) if row else None

    def find_identify_conflict(self, identify_type: str, identify_value: str,
                               identify_param: str, exclude_id: int = 0) -> List[dict]:
        """找识别式可能冲突的规则。

        前缀互相包含 = 冲突（短前缀会把长前缀的码吸走）。
        """
        out = []
        if not identify_value:
            return out
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, part_no, identify_type, identify_value, identify_param "
                "FROM rules WHERE id<>?", (exclude_id,)
            ).fetchall()
        for r in rows:
            if r["identify_type"] != identify_type:
                continue
            ov = r["identify_value"] or ""
            if not ov:
                continue
            if identify_type == "PREFIX":
                if ov.startswith(identify_value) or identify_value.startswith(ov):
                    if ov != identify_value:
                        out.append(dict(r))
        return out

    def suggest_part_nos(self, prefix: str, limit: int = 20) -> List[str]:
        """料号联想：从规则表 + 历史扫码记录里取。"""
        with self._lock:
            out = []
            seen = set()
            for sql, args in (
                ("SELECT DISTINCT part_no FROM rules WHERE part_no LIKE ? ORDER BY part_no LIMIT ?",
                 (prefix + "%", limit)),
                ("SELECT DISTINCT part_no FROM scans WHERE part_no LIKE ? ORDER BY part_no LIMIT ?",
                 (prefix + "%", limit)),
            ):
                for r in self.conn.execute(sql, args).fetchall():
                    v = r[0]
                    if v and v not in seen:
                        seen.add(v)
                        out.append(v)
            return out[:limit]

    # ---------------------------------------------------------- 扫码记录
    def next_seq(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM scans"
            ).fetchone()[0]

    def find_valid(self, uniq_key: str) -> List[sqlite3.Row]:
        """找已占用唯一性的历史记录。"""
        if not uniq_key:
            return []
        with self._lock:
            return self.conn.execute(
                "SELECT seq, ts, terminal, raw FROM scans "
                "WHERE uniq_key=? AND is_valid=1 ORDER BY seq",
                (uniq_key,),
            ).fetchall()

    def insert_scan(self, **kw) -> int:
        cols = ",".join(kw.keys())
        marks = ",".join("?" * len(kw))
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO scans({cols}) VALUES({marks})", tuple(kw.values())
            )
            self.conn.commit()
            return cur.lastrowid

    def stats(self, batch: Optional[str] = None, part_no: Optional[str] = None) -> dict:
        where, args = [], []
        if batch:
            where.append("batch=?")
            args.append(batch)
        if part_no:
            where.append("part_no=?")
            args.append(part_no)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM scans {w}", args).fetchone()[0]
            valid = self.conn.execute(
                f"SELECT COUNT(*) FROM scans {w}{' AND' if w else 'WHERE'} is_valid=1", args
            ).fetchone()[0]
            uniq = self.conn.execute(
                f"SELECT COUNT(DISTINCT uniq_key) FROM scans {w}"
                f"{' AND' if w else 'WHERE'} is_valid=1 AND uniq_key IS NOT NULL",
                args,
            ).fetchone()[0]
            blocked = self.conn.execute(
                f"SELECT COUNT(*) FROM scans {w}{' AND' if w else 'WHERE'} action='BLOCK'", args
            ).fetchone()[0]
            warned = self.conn.execute(
                f"SELECT COUNT(*) FROM scans {w}{' AND' if w else 'WHERE'} action='WARN'", args
            ).fetchone()[0]
            dup_rows = self.conn.execute(
                f"SELECT COUNT(*) FROM scans {w}{' AND' if w else 'WHERE'} is_valid=1 AND is_dup=1",
                args,
            ).fetchone()[0]
            dup_codes = self.conn.execute(
                f"SELECT COUNT(DISTINCT norm) FROM scans {w}"
                f"{' AND' if w else 'WHERE'} is_valid=1 AND is_dup=1",
                args,
            ).fetchone()[0]
            locked = self.conn.execute(
                "SELECT COUNT(*) FROM locks WHERE unlocked_at=''"
            ).fetchone()[0]
        return {
            "total": total, "valid": valid, "unique": uniq,
            "dup_rows": dup_rows, "dup_codes": dup_codes,
            "blocked": blocked, "warned": warned, "locked": locked,
        }

    def recent(self, n: int = 100, batch: Optional[str] = None) -> List[sqlite3.Row]:
        where, args = ("WHERE batch=?", [batch]) if batch else ("", [])
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM scans {where} ORDER BY id DESC LIMIT ?", (*args, n)
            ).fetchall()

    def all_rows(self, batch: Optional[str] = None) -> List[sqlite3.Row]:
        where, args = ("WHERE batch=?", [batch]) if batch else ("", [])
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM scans {where} ORDER BY seq", args
            ).fetchall()

    def unique_codes(self, batch: Optional[str] = None) -> List[str]:
        where, args = ("AND batch=?", [batch]) if batch else ("", [])
        with self._lock:
            return [
                r[0]
                for r in self.conn.execute(
                    f"SELECT raw FROM scans WHERE is_valid=1 AND uniq_key IS NOT NULL {where} "
                    f"ORDER BY seq",
                    args,
                ).fetchall()
            ]

    def last_scan(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def delete_scan(self, scan_id: int) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT raw FROM scans WHERE id=?", (scan_id,)
            ).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
            # 重算受影响码的出现次序
            self.conn.execute(
                """UPDATE scans SET dup_count = (
                       SELECT COUNT(*) FROM scans s2
                       WHERE s2.norm = scans.norm AND s2.id <= scans.id AND s2.is_valid=1
                   ) WHERE is_valid=1"""
            )
            self.conn.commit()
            return row["raw"]

    def count_violations(self, rule_id: int, minutes: int) -> int:
        """统计窗口内违规次数，供升级策略用。"""
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM scans WHERE rule_id=? AND action IN "
                "('BLOCK','LOCK_CODE','LOCK_SCREEN') "
                "AND ts >= datetime('now','localtime',?)",
                (rule_id, f"-{int(minutes)} minutes"),
            ).fetchone()[0]

    # ---------------------------------------------------------- 锁定名单
    def lock(self, norm: str, raw: str, part_no: str, reason: str, by: str) -> bool:
        """锁定一个码。已锁定则不重复写入。"""
        with self._lock:
            got = self.conn.execute(
                "SELECT id FROM locks WHERE norm=? AND unlocked_at=''", (norm,)
            ).fetchone()
            if got:
                return False
            self.conn.execute(
                "INSERT INTO locks(norm,raw,part_no,reason,locked_at,locked_by) "
                "VALUES(?,?,?,?,?,?)",
                (norm, raw, part_no, reason, now_str(), by),
            )
            self.conn.commit()
            return True

    def is_locked(self, norm: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM locks WHERE norm=? AND unlocked_at='' "
                "ORDER BY id DESC LIMIT 1", (norm,)
            ).fetchone()

    def unlock(self, lock_id: int, by: str, reason: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE locks SET unlocked_at=?, unlocked_by=?, unlock_reason=? "
                "WHERE id=? AND unlocked_at=''",
                (now_str(), by, reason, lock_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_locks(self, only_active: bool = True) -> List[sqlite3.Row]:
        with self._lock:
            sql = "SELECT * FROM locks"
            if only_active:
                sql += " WHERE unlocked_at=''"
            sql += " ORDER BY id DESC"
            return self.conn.execute(sql).fetchall()

    # ---------------------------------------------------------- 审计
    def log_audit(self, actor: str, action: str, target: str = "", detail: str = ""):
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                (now_str(), actor, action, target, detail),
            )
            self.conn.commit()

    def list_audit(self, n: int = 200) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()

    # ---------------------------------------------------------- 设置
    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set(self, key: str, value: str):
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, "1" if default else "0")
        return v == "1"

    def set_bool(self, key: str, value: bool):
        self.set(key, "1" if value else "0")

    # ---------------------------------------------------------- 密码
    def has_password(self) -> bool:
        return bool(self.get("pwd_hash", ""))

    def set_password(self, pwd: str):
        salt = new_salt()
        self.set("pwd_salt", salt)
        self.set("pwd_hash", hash_password(pwd, salt))

    def check_password(self, pwd: str) -> bool:
        h = self.get("pwd_hash", "")
        s = self.get("pwd_salt", "")
        if not h or not s:
            return False
        try:
            return hash_password(pwd, s) == h
        except Exception:
            return False

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
