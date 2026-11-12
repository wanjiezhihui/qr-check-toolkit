"""规则批量导入：CSV / TSV / XLSX（纯标准库解析）。

模板设计：**一行一个条件，同一料号多行自动合并成一条规则**。
比「一行一规则 + 条件1..N 宽表」更符合 Excel 使用习惯，且不受条件数量限制。

同料号多行时：规则级字段（识别方式/动作/验重范围…）取该组第一个非空值，条件列每行各贡献一条。
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .engine import ACTIONS, ACTION_LABEL, COND_TYPES, Condition, Rule
from .storage import Database

# ============================================================ 模板列
COLUMNS = [
    "料号", "规则名", "识别方式", "识别值", "识别参数",
    "条件关系", "至少满足", "验重范围",
    "校验动作", "判重动作", "未匹配动作", "优先级",
    "条件类型", "条件值", "条件参数", "是否必填", "取反", "失败动作", "失败提示",
    "备注",
]

# 中文 → 内部代码 映射
IDENT_MAP = {"前缀": "PREFIX", "PREFIX": "PREFIX", "正则": "REGEX", "REGEX": "REGEX",
             "分段": "SEGMENT", "SEGMENT": "SEGMENT", "完全相等": "EXACT", "EXACT": "EXACT"}
LOGIC_MAP = {"全部满足": "AND", "AND": "AND", "任一满足": "OR", "OR": "OR",
             "至少N条": "AT_LEAST", "AT_LEAST": "AT_LEAST"}
SCOPE_MAP = {"全局": "GLOBAL", "GLOBAL": "GLOBAL", "料号内": "PART", "PART": "PART",
             "不验重": "NONE", "NONE": "NONE"}
ACTION_MAP = {v: v for v in ACTIONS}
ACTION_MAP.update({ACTION_LABEL[v]: v for v in ACTIONS})
COND_MAP = {k: k for k in COND_TYPES}
COND_MAP.update({v: k for k, v in COND_TYPES.items()})

PARAM_KEYS = {
    "SEGMENT": (("sep", "分隔符"), ("idx", "段序号"), ("cmp", "比较方式")),
    "DATE_VALID": (("sep", "分隔符"), ("idx", "段序号"), ("fmt", "日期格式"),
                   ("min_offset", "最早偏移天"), ("max_offset", "最晚偏移天")),
    "DATE_MATCH": (("sep", "分隔符"), ("idx_a", "段A"), ("fmt_a", "格式A"),
                   ("idx_b", "段B"), ("fmt_b", "格式B")),
}


def parse_kv(text: str) -> dict:
    """`sep=%|idx=1|cmp=EQUALS` → dict。值里可以含 =，只按第一个 = 切。"""
    out = {}
    for part in (text or "").replace("；", "|").replace(";", "|").split("|"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def params_to_json(kv: dict) -> str:
    if not kv:
        return ""
    fixed = {}
    for k, v in kv.items():
        if k in ("idx", "idx_a", "idx_b", "min_offset", "max_offset"):
            try:
                fixed[k] = int(v)
                continue
            except Exception:
                pass
        fixed[k] = v
    return json.dumps(fixed, ensure_ascii=False)


# ============================================================ 读文件
def read_table(path: Path) -> List[List[str]]:
    """读成二维字符串表。支持 csv/tsv/xlsx。"""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _read_xlsx(path)
    if suffix in (".xls",):
        raise ValueError("不支持旧版 .xls，请在 Excel 里另存为 .xlsx 或 .csv")
    return _read_delimited(path)


def _read_delimited(path: Path) -> List[List[str]]:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        raise ValueError("无法识别文件编码")
    sample = text[:4096]
    delim = "\t" if (sample.count("\t") > sample.count(",")) else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    return [[(c or "").strip() for c in row] for row in reader]


def _read_xlsx(path: Path) -> List[List[str]]:
    """标准库解析 xlsx：zipfile + ElementTree。"""
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        sheet = None
        for n in sorted(names):
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                sheet = n
                break
        if not sheet:
            raise ValueError("xlsx 里找不到工作表")
        root = ET.fromstring(z.read(sheet))
        table: List[List[str]] = []
        for row in root.iter(f"{NS}row"):
            cells: dict = {}
            max_idx = -1
            for c in row.findall(f"{NS}c"):
                ref = c.get("r", "")
                letters = "".join(ch for ch in ref if ch.isalpha())
                idx = _col_index(letters)
                t = c.get("t", "")
                v = c.find(f"{NS}v")
                if t == "s" and v is not None and v.text is not None:
                    try:
                        val = shared[int(v.text)]
                    except Exception:
                        val = ""
                elif t == "inlineStr":
                    node = c.find(f"{NS}is")
                    val = "".join(x.text or "" for x in node.iter(f"{NS}t")) if node is not None else ""
                else:
                    val = v.text if (v is not None and v.text is not None) else ""
                cells[idx] = str(val).strip()
                max_idx = max(max_idx, idx)
            if max_idx < 0:
                table.append([])
            else:
                table.append([cells.get(i, "") for i in range(max_idx + 1)])
        return table


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


# ============================================================ 解析结果
@dataclass
class ImportIssue:
    rowno: int
    part_no: str
    kind: str        # insert / overwrite / conflict / error
    msg: str


@dataclass
class ImportPlan:
    rules: List[Rule] = field(default_factory=list)
    issues: List[ImportIssue] = field(default_factory=list)

    def counts(self) -> dict:
        c = {"insert": 0, "overwrite": 0, "conflict": 0, "error": 0}
        for i in self.issues:
            if i.kind in c:
                c[i.kind] += 1
        return c

    def summary(self) -> str:
        c = self.counts()
        return (f"新增 {c['insert']} 条｜覆盖 {c['overwrite']} 条｜"
                f"冲突 {c['conflict']} 条｜格式错 {c['error']} 条")


def build_plan(rows: List[List[str]], db: Database) -> ImportPlan:
    """把二维表解析成导入计划，不落库。"""
    plan = ImportPlan()
    if not rows:
        plan.issues.append(ImportIssue(0, "", "error", "文件是空的"))
        return plan

    header = [h.strip() for h in rows[0]]
    col = {}
    for i, h in enumerate(header):
        if h in COLUMNS:
            col[h] = i
    missing = [c for c in ("料号",) if c not in col]
    if missing:
        plan.issues.append(
            ImportIssue(1, "", "error",
                        f"表头缺少必需列：{'、'.join(missing)}。请先导出模板对照")
        )
        return plan

    def cell(row: List[str], name: str) -> str:
        i = col.get(name)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    groups: dict = {}
    order: List[str] = []
    last_part = ""
    for idx, row in enumerate(rows[1:], start=2):
        if not any((c or "").strip() for c in row):
            continue
        part = cell(row, "料号")
        ctype_raw = cell(row, "条件类型")
        # 续行：料号留空但填了条件 → 归到上一条料号（这是「一行一条件」模板的核心用法）
        if not part:
            if ctype_raw and last_part:
                part = last_part
            else:
                plan.issues.append(
                    ImportIssue(idx, "", "error", "料号为空且无法归属到上一行，已跳过"))
                continue
        last_part = part

        if part not in groups:
            groups[part] = {"rowno": idx, "head": list(row), "conds": []}
            order.append(part)
        else:
            g = groups[part]
            merged = list(g["head"])
            for i, val in enumerate(row):
                if i >= len(merged):
                    merged.append(val)
                elif val and not merged[i]:
                    merged[i] = val
            g["head"] = merged
        if ctype_raw:
            groups[part]["conds"].append((idx, row))

    existing = {r.part_no: r.id for r in db.list_rules()}

    for part in order:
        g = groups[part]
        head = g["head"]
        rowno = g["rowno"]

        def h(name: str) -> str:
            return cell(head, name)

        rule = Rule(part_no=part, rule_name=h("规则名") or part)

        it = IDENT_MAP.get(h("识别方式") or "前缀")
        if it is None:
            plan.issues.append(ImportIssue(rowno, part, "error",
                                           f"识别方式「{h('识别方式')}」无法识别"))
            continue
        rule.identify_type = it
        rule.identify_value = h("识别值")
        rule.identify_param = params_to_json(parse_kv(h("识别参数")))
        if not rule.identify_value:
            plan.issues.append(ImportIssue(rowno, part, "error", "识别值为空，无法路由"))
            continue

        logic = LOGIC_MAP.get(h("条件关系") or "全部满足")
        rule.match_logic = logic or "AND"
        try:
            rule.at_least_n = int(h("至少满足") or 1)
        except Exception:
            rule.at_least_n = 1
        rule.dup_scope = SCOPE_MAP.get(h("验重范围") or "全局", "GLOBAL")
        rule.on_pass = "PASS"
        rule.on_violation = ACTION_MAP.get(h("校验动作") or "拦截", "BLOCK")
        rule.on_dup = ACTION_MAP.get(h("判重动作") or "锁码", "LOCK_CODE")
        rule.on_unknown = ACTION_MAP.get(h("未匹配动作") or "拦截", "BLOCK")
        try:
            rule.priority = int(h("优先级") or 100)
        except Exception:
            rule.priority = 100
        rule.remark = h("备注")

        # 条件
        for crowno, crow in g["conds"]:
            ct_raw = cell(crow, "条件类型")
            ct = COND_MAP.get(ct_raw)
            if ct is None:
                plan.issues.append(ImportIssue(crowno, part, "error",
                                               f"条件类型「{ct_raw}」无法识别"))
                continue
            cond = Condition(ctype=ct, operand=cell(crow, "条件值"))
            cond.op_param = params_to_json(parse_kv(cell(crow, "条件参数")))
            cond.required = (cell(crow, "是否必填") or "是") not in ("否", "0", "false", "FALSE")
            cond.negate = (cell(crow, "取反") or "否") in ("是", "1", "true", "TRUE")
            fa = cell(crow, "失败动作")
            cond.fail_action = ACTION_MAP.get(fa, "") if fa else ""
            cond.fail_msg = cell(crow, "失败提示")
            if ct in ("SEGMENT", "DATE_VALID", "DATE_MATCH") and not cond.op_param:
                plan.issues.append(ImportIssue(
                    crowno, part, "error",
                    f"条件类型「{COND_TYPES[ct]}」必须填条件参数，如 sep=%|idx=1"))
                continue
            if not cond.operand and ct not in ("DATE_VALID", "DATE_MATCH"):
                plan.issues.append(ImportIssue(crowno, part, "error", "条件值为空"))
                continue
            rule.conditions.append(cond)

        if not rule.conditions:
            plan.issues.append(ImportIssue(rowno, part, "error", "该料号没有任何有效条件"))
            continue

        # 冲突检查
        conflicts = db.find_identify_conflict(
            rule.identify_type, rule.identify_value, rule.identify_param, 0)
        if conflicts:
            names = "、".join(c["part_no"] for c in conflicts[:3])
            plan.issues.append(ImportIssue(
                rowno, part, "conflict",
                f"识别式与已有料号 {names} 重叠（前缀互相包含），可能误吸别料号的码"))
            continue

        if part in existing:
            plan.issues.append(ImportIssue(rowno, part, "overwrite",
                                           f"已有规则将被覆盖（{len(rule.conditions)} 条条件）"))
        else:
            plan.issues.append(ImportIssue(rowno, part, "insert",
                                           f"新增（{len(rule.conditions)} 条条件）"))
        plan.rules.append(rule)

    return plan


def apply_plan(plan: ImportPlan, db: Database, actor: str = "") -> int:
    """只导入没有 error/conflict 的规则。返回成功条数。"""
    blocked = {i.part_no for i in plan.issues if i.kind in ("error", "conflict")}
    n = 0
    for rule in plan.rules:
        if rule.part_no in blocked:
            continue
        db.save_rule(rule)
        n += 1
    db.log_audit(actor, "import_rules", f"{n} 条规则", plan.summary())
    return n


# ============================================================ 导出
def write_template(path: Path):
    """写一份带示例的导入模板。"""
    rows = [
        COLUMNS,
        ["3000000003", "3000000003 主标签", "前缀", "3000000003", "", "全部满足", 1, "全局",
         "拦截", "锁码", "拦截", 100,
         "包含子串", "3000000003", "", "是", "否", "", "料号段不符，请确认是否混料", "",
         ],
        ["", "", "", "", "", "", "", "", "", "", "", "",
         "包含子串", "8888", "", "是", "否", "", "", ""],
        ["", "", "", "", "", "", "", "", "", "", "", "",
         "结尾是", "008", "", "是", "否", "提醒", "结尾流水号不是 008", ""],
        ["400004", "400004", "前缀", "400004", "", "全部满足", 1, "全局",
         "拦截", "锁码", "拦截", 100,
         "分段取值比对", "555555", "sep=/|idx=1|cmp=EQUALS", "是", "否", "",
         "分隔符后半段不符", ""],
        ["10000000001", "10000000001", "前缀", "10000000001", "", "全部满足", 1, "全局",
         "拦截", "锁码", "拦截", 100,
         "分段取值比对", "DM-A0000-000000000", "sep=%|idx=1|cmp=EQUALS", "是", "否", "",
         "第2段规格不符", ""],
        ["", "", "", "", "", "", "", "", "", "", "", "",
         "两段日期一致", "", "sep=%|idx_a=5|fmt_a=YYMMDD|idx_b=7|fmt_b=YYYYMMDD",
         "是", "否", "", "两处日期不一致，疑似串号", "语义待确认"],
        ["20000000002", "20000000002", "前缀", "20000000002", "", "全部满足", 1, "全局",
         "拦截", "锁码", "拦截", 100,
         "长度在区间", "24-24", "", "是", "否", "", "长度不是 24，疑似漏字符", ""],
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)


def export_rules_csv(path: Path, db: Database):
    """把现有规则导成同格式 CSV（方便改完再导回）。"""
    rows = [COLUMNS]
    for r in db.list_rules():
        full = db.get_rule(r.id)
        if not full:
            continue
        base = [
            full.part_no, full.rule_name, _rev(IDENT_MAP, full.identify_type),
            full.identify_value, _kv_from_json(full.identify_param),
            _rev(LOGIC_MAP, full.match_logic), full.at_least_n,
            _rev(SCOPE_MAP, full.dup_scope),
            ACTION_LABEL.get(full.on_violation, full.on_violation),
            ACTION_LABEL.get(full.on_dup, full.on_dup),
            ACTION_LABEL.get(full.on_unknown, full.on_unknown),
            full.priority,
        ]
        if not full.conditions:
            rows.append(base + [""] * 8 + [full.remark])
            continue
        for i, c in enumerate(full.conditions):
            head = base if i == 0 else [""] * len(base)
            rows.append(head + [
                COND_TYPES.get(c.ctype, c.ctype), c.operand, _kv_from_json(c.op_param),
                "是" if c.required else "否", "是" if c.negate else "否",
                ACTION_LABEL.get(c.fail_action, "") if c.fail_action else "",
                c.fail_msg, (full.remark if i == 0 else ""),
            ])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)


def _rev(mapping: dict, value: str) -> str:
    """内部代码 → 中文（优先）。"""
    for k, v in mapping.items():
        if v == value and not k.isascii():
            return k
    return value


def _kv_from_json(js: str) -> str:
    if not js:
        return ""
    try:
        d = json.loads(js)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    return "|".join(f"{k}={v}" for k, v in d.items())
