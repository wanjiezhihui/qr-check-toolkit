"""规则引擎：条件求值、料号路由、动作判定。

设计要点：
1. 所有内容匹配走【字面量】，`*` `+` `%` 一律当普通字符，绝不进正则语义。
   需要正则必须显式选 REGEX 类型。
2. 内容校验与验重使用【两套归一化口径】：内容按原样比对，验重走归一化键。
3. 识别式禁用「包含」，只允许 PREFIX / REGEX / SEGMENT，避免宽匹配误吸别料号的码。
4. 多个动作同时命中时，取严重度最高的那个。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Tuple

# ============================================================ 动作定义
ACTIONS = ["PASS", "WARN", "BLOCK", "LOCK_CODE", "LOCK_SCREEN"]

ACTION_RANK = {"PASS": 0, "WARN": 1, "BLOCK": 2, "LOCK_CODE": 3, "LOCK_SCREEN": 4}

ACTION_LABEL = {
    "PASS": "放行",
    "WARN": "提醒",
    "BLOCK": "拦截",
    "LOCK_CODE": "锁码",
    "LOCK_SCREEN": "锁屏",
}

ACTION_HINT = {
    "PASS": "内容合规，正常记录",
    "WARN": "提示异常，按确认键后继续",
    "BLOCK": "拒绝通过，不占唯一性，须重扫",
    "LOCK_CODE": "该码进入锁定名单，需密码解锁",
    "LOCK_SCREEN": "锁定操作界面，需密码解锁",
}

# 需要密码解锁的动作
LOCKING_ACTIONS = {"LOCK_CODE", "LOCK_SCREEN"}


def worse(a: str, b: str) -> str:
    """返回两个动作中更严重的那个。"""
    return a if ACTION_RANK.get(a, 0) >= ACTION_RANK.get(b, 0) else b


# ============================================================ 条件类型
COND_TYPES = {
    "CONTAINS": "包含子串",
    "NOT_CONTAINS": "不包含子串",
    "STARTS_WITH": "开头是",
    "ENDS_WITH": "结尾是",
    "EQUALS": "完全等于",
    "REGEX": "正则匹配（危险）",
    "LENGTH": "长度在区间",
    "CHARSET": "只允许这些字符",
    "SEGMENT": "分段取值比对",
    "DATE_VALID": "分段日期有效",
    "DATE_MATCH": "两段日期一致",
}

# 需要额外参数的复杂条件
COND_NEEDS_PARAM = {"SEGMENT", "DATE_VALID", "DATE_MATCH"}

# 子比较方式（SEGMENT 内部用）
SUB_CMP = {
    "EQUALS": "等于",
    "CONTAINS": "包含",
    "STARTS_WITH": "开头是",
    "ENDS_WITH": "结尾是",
    "REGEX": "正则匹配",
}


# ============================================================ 归一化
def to_halfwidth(s: str) -> str:
    """全角转半角（含全角空格）。"""
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize(raw: str, ignore_case: bool = False, fullwidth: bool = False) -> str:
    """验重归一化键。注意：内容校验【不能】用这个结果，必须用原样。"""
    s = (raw or "").strip()
    if fullwidth:
        s = to_halfwidth(s)
    if ignore_case:
        s = s.upper()
    return s


# ============================================================ 数据结构
@dataclass
class Condition:
    ctype: str = "CONTAINS"
    operand: str = ""
    op_param: str = ""          # JSON 字符串，复杂条件用
    negate: bool = False        # 取反
    required: bool = True       # 必须 / 可选（可选只计入 AT_LEAST 统计）
    fail_action: str = ""       # 该条不满足时的动作，空则继承规则级
    fail_msg: str = ""          # 现场提示文案
    seq: int = 0
    id: int = 0

    def params(self) -> dict:
        if not self.op_param:
            return {}
        try:
            got = json.loads(self.op_param)
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}

    def describe(self) -> str:
        """人话描述，界面和报表用。"""
        base = COND_TYPES.get(self.ctype, self.ctype)
        p = self.params()
        if self.ctype == "SEGMENT":
            seg = f"按 {p.get('sep', '%')!r} 切分第 {int(p.get('idx', 0)) + 1} 段"
            cmp_ = SUB_CMP.get(p.get("cmp", "EQUALS"), "等于")
            body = f"{seg} {cmp_} {self.operand!r}"
        elif self.ctype == "DATE_VALID":
            seg = f"按 {p.get('sep', '%')!r} 切分第 {int(p.get('idx', 0)) + 1} 段"
            body = f"{seg} 按 {p.get('fmt', 'YYMMDD')} 解析并在有效期内"
        elif self.ctype == "DATE_MATCH":
            body = (
                f"第 {int(p.get('idx_a', 0)) + 1} 段({p.get('fmt_a', 'YYMMDD')})"
                f" 与第 {int(p.get('idx_b', 0)) + 1} 段({p.get('fmt_b', 'YYYYMMDD')}) 日期一致"
            )
        elif self.ctype == "LENGTH":
            body = f"长度 {self.operand}"
        elif self.ctype == "CHARSET":
            body = f"只含 {self.operand}"
        else:
            body = f"{base} {self.operand!r}"
        return ("非 " + body) if self.negate else body


@dataclass
class Rule:
    part_no: str = ""
    rule_name: str = ""
    enabled: bool = True
    identify_type: str = "PREFIX"      # PREFIX / REGEX / SEGMENT
    identify_value: str = ""
    identify_param: str = ""           # SEGMENT 识别时用 JSON
    match_logic: str = "AND"           # AND / OR / AT_LEAST
    at_least_n: int = 1
    dup_scope: str = "GLOBAL"          # GLOBAL / PART / NONE
    on_pass: str = "PASS"
    on_violation: str = "BLOCK"
    on_dup: str = "LOCK_CODE"
    on_unknown: str = "BLOCK"
    escalation: str = ""               # JSON: {"count":3,"minutes":480,"action":"LOCK_SCREEN"}
    priority: int = 100
    valid_from: str = ""
    valid_to: str = ""
    remark: str = ""
    conditions: List[Condition] = field(default_factory=list)
    id: int = 0
    updated_at: str = ""

    def ident_params(self) -> dict:
        if not self.identify_param:
            return {}
        try:
            got = json.loads(self.identify_param)
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}

    def describe_identify(self) -> str:
        if self.identify_type == "PREFIX":
            return f"前缀 = {self.identify_value!r}"
        if self.identify_type == "REGEX":
            return f"正则 {self.identify_value}"
        if self.identify_type == "SEGMENT":
            p = self.ident_params()
            return (
                f"按 {p.get('sep', '%')!r} 切分第 {int(p.get('idx', 0)) + 1} 段"
                f" = {self.identify_value!r}"
            )
        return "(未设置)"


@dataclass
class CondResult:
    cond: Condition
    ok: bool
    detail: str


@dataclass
class Verdict:
    action: str = "BLOCK"
    code: str = ""
    norm: str = ""
    rule: Optional[Rule] = None
    matched_by: str = "NONE"           # AUTO / MANUAL / NONE
    is_dup: bool = False
    dup_count: int = 0
    first_ts: str = ""
    first_terminal: str = ""
    cond_results: List[CondResult] = field(default_factory=list)
    reason: str = ""
    escalate_note: str = ""

    def label(self) -> str:
        return ACTION_LABEL.get(self.action, self.action)

    def passed(self) -> bool:
        return self.action in ("PASS", "WARN")


# ============================================================ 字符集解析
def parse_charset(spec: str) -> set:
    """把 `A-Z0-9%/+*-` 这类描述解析成字符集合。

    末尾或无法构成区间的 `-` 按字面量处理。
    """
    chars = set()
    i = 0
    n = len(spec)
    while i < n:
        if i + 2 < n and spec[i + 1] == "-":
            a, b = spec[i], spec[i + 2]
            if ord(a) <= ord(b):
                for c in range(ord(a), ord(b) + 1):
                    chars.add(chr(c))
                i += 3
                continue
        chars.add(spec[i])
        i += 1
    return chars


# ============================================================ 日期解析
def parse_seg_date(text: str, fmt: str) -> Optional[date]:
    s = (text or "").strip()
    try:
        if fmt == "YYMMDD" and len(s) == 6 and s.isdigit():
            return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))
        if fmt == "YYYYMMDD" and len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if fmt == "YYYY-MM-DD" and len(s) == 10 and s[4] == "-" and s[7] == "-":
            return date(int(s[:4]), int(s[5:7]), int(s[8:10]))
        if fmt == "YY-MMDD" and len(s) == 7 and s[2] == "-":
            return date(2000 + int(s[:2]), int(s[3:5]), int(s[5:7]))
    except Exception:
        return None
    return None


# ============================================================ 条件求值
def _sub_compare(text: str, value: str, cmp_: str) -> bool:
    if cmp_ == "EQUALS":
        return text == value
    if cmp_ == "CONTAINS":
        return value in text
    if cmp_ == "STARTS_WITH":
        return text.startswith(value)
    if cmp_ == "ENDS_WITH":
        return text.endswith(value)
    if cmp_ == "REGEX":
        try:
            return re.search(value, text) is not None
        except re.error:
            return False
    return False


def eval_condition(cond: Condition, code: str, today: Optional[date] = None) -> Tuple[bool, str]:
    """求值单条条件。返回 (是否通过, 说明)。

    code 必须是【扫码原文】，不做归一化——条件里写的字符要和标签上的字符逐字对上。
    """
    today = today or date.today()
    t = cond.ctype
    v = cond.operand
    p = cond.params()
    ok = False
    detail = ""

    if t == "CONTAINS":
        ok = v in code
        detail = f"未包含 {v!r}" if not ok else "已包含"
    elif t == "NOT_CONTAINS":
        ok = v not in code
        detail = f"不应包含 {v!r}" if not ok else "未出现"
    elif t == "STARTS_WITH":
        ok = code.startswith(v)
        detail = f"开头不是 {v!r}" if not ok else "开头匹配"
    elif t == "ENDS_WITH":
        ok = code.endswith(v)
        detail = f"结尾不是 {v!r}" if not ok else "结尾匹配"
    elif t == "EQUALS":
        ok = code == v
        detail = "与期望值不等" if not ok else "完全一致"
    elif t == "REGEX":
        try:
            ok = re.search(v, code) is not None
            detail = "正则未命中" if not ok else "正则命中"
        except re.error as ex:
            ok = False
            detail = f"正则本身有误：{ex}"
    elif t == "LENGTH":
        raw = (v or "").replace(" ", "")
        try:
            if "-" in raw:
                lo, hi = raw.split("-", 1)
                lo, hi = int(lo), int(hi)
            else:
                lo = hi = int(raw)
            n = len(code)
            ok = lo <= n <= hi
            detail = f"长度 {n}，要求 {lo}-{hi}"
        except Exception:
            ok = False
            detail = f"长度参数无法解析：{v!r}"
    elif t == "CHARSET":
        allowed = parse_charset(v or "")
        bad = [ch for ch in code if ch not in allowed]
        ok = not bad
        detail = ("含非法字符 " + repr("".join(sorted(set(bad)))[:12])) if bad else "字符全部合法"
    elif t == "SEGMENT":
        sep = p.get("sep", "%") or "%"
        idx = int(p.get("idx", 0))
        cmp_ = p.get("cmp", "EQUALS")
        parts = code.split(sep)          # 保留空段：'a%%b' -> ['a','','b']
        if idx >= len(parts) or idx < 0:
            ok = False
            detail = f"按 {sep!r} 只有 {len(parts)} 段，取不到第 {idx + 1} 段"
        else:
            got = parts[idx]
            ok = _sub_compare(got, v, cmp_)
            detail = f"第 {idx + 1} 段为 {got!r}" + ("" if ok else "，不符")
    elif t == "DATE_VALID":
        sep = p.get("sep", "%") or "%"
        idx = int(p.get("idx", 0))
        fmt = p.get("fmt", "YYMMDD")
        lo_off = int(p.get("min_offset", -30))
        hi_off = int(p.get("max_offset", 365))
        parts = code.split(sep)
        if idx >= len(parts) or idx < 0:
            ok = False
            detail = f"只有 {len(parts)} 段，取不到第 {idx + 1} 段"
        else:
            d = parse_seg_date(parts[idx], fmt)
            if d is None:
                ok = False
                detail = f"第 {idx + 1} 段 {parts[idx]!r} 不是合法 {fmt} 日期"
            else:
                lo = today + timedelta(days=lo_off)
                hi = today + timedelta(days=hi_off)
                ok = lo <= d <= hi
                detail = f"日期 {d.isoformat()}，允许范围 {lo.isoformat()}~{hi.isoformat()}"
    elif t == "DATE_MATCH":
        sep = p.get("sep", "%") or "%"
        ia, ib = int(p.get("idx_a", 0)), int(p.get("idx_b", 0))
        fa, fb = p.get("fmt_a", "YYMMDD"), p.get("fmt_b", "YYYYMMDD")
        parts = code.split(sep)
        if max(ia, ib) >= len(parts):
            ok = False
            detail = f"只有 {len(parts)} 段，取不到第 {max(ia, ib) + 1} 段"
        else:
            da = parse_seg_date(parts[ia], fa)
            db = parse_seg_date(parts[ib], fb)
            if da is None or db is None:
                ok = False
                detail = f"日期解析失败：{parts[ia]!r} / {parts[ib]!r}"
            else:
                ok = da == db
                detail = f"{da.isoformat()} vs {db.isoformat()}" + ("" if ok else "，不一致")
    else:
        ok = False
        detail = f"未知条件类型 {t}"

    if cond.negate:
        ok = not ok
        detail = ("取反后不成立：" + detail) if not ok else detail
    return ok, detail


# ============================================================ 规则路由
def identify_match(rule: Rule, code: str) -> bool:
    """判断这个码是否属于该规则的料号。"""
    it = rule.identify_type
    iv = rule.identify_value or ""
    if not iv:
        return False
    if it == "PREFIX":
        return code.startswith(iv)
    if it == "REGEX":
        try:
            return re.search(iv, code) is not None
        except re.error:
            return False
    if it == "SEGMENT":
        p = rule.ident_params()
        sep = p.get("sep", "%") or "%"
        idx = int(p.get("idx", 0))
        parts = code.split(sep)
        if idx < 0 or idx >= len(parts):
            return False
        return parts[idx] == iv
    if it == "EXACT":
        return code == iv
    return False


def route_rule(rules: List[Rule], code: str) -> Optional[Rule]:
    """按优先级顺序找第一条识别命中的规则。"""
    for r in sorted(rules, key=lambda x: (x.priority, x.id)):
        if identify_match(r, code):
            return r
    return None


# ============================================================ 条件组合
def combine_logic(rule: Rule, results: List[CondResult]) -> Tuple[bool, str]:
    """按规则的逻辑关系组合条件结果。"""
    if not results:
        return True, "无校验条件，直接通过"

    must = [r for r in results if r.cond.required]
    optional = [r for r in results if not r.cond.required]

    logic = rule.match_logic
    if logic == "AND":
        bad = [r for r in must if not r.ok]
        if bad:
            return False, "；".join((r.cond.fail_msg or r.detail) for r in bad[:3])
        return True, "全部必填条件通过"

    if logic == "OR":
        pool = must + optional
        if any(r.ok for r in pool):
            return True, "至少一条通过"
        return False, "没有任何一条条件通过"

    if logic == "AT_LEAST":
        need = max(1, int(rule.at_least_n))
        pool = [r for r in results if r.ok]
        if len(pool) >= need:
            return True, f"满足 {len(pool)} 条（要求 {need} 条）"
        return False, f"仅满足 {len(pool)} 条，要求至少 {need} 条"

    return True, ""


# ============================================================ 升级策略
def parse_escalation(rule: Rule) -> Optional[dict]:
    if not rule.escalation:
        return None
    try:
        got = json.loads(rule.escalation)
        if not isinstance(got, dict):
            return None
        if int(got.get("count", 0)) <= 0:
            return None
        if got.get("action") not in ACTIONS:
            return None
        return got
    except Exception:
        return None


# ============================================================ 时间窗
def rule_in_window(rule: Rule, now_ts: str) -> bool:
    """规则生效时间窗判断。空值表示不限。"""
    vf = (rule.valid_from or "").strip()
    vt = (rule.valid_to or "").strip()
    d = now_ts[:10]
    if vf and d < vf:
        return False
    if vt and d > vt:
        return False
    return True
