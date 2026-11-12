"""自检：不依赖界面，覆盖核心判定逻辑。

用法：python -m app.selftest
"""

from __future__ import annotations

import shutil
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

from . import importer
from .engine import (
    ACTION_RANK, Condition, Rule, combine_logic, eval_condition, normalize,
    parse_charset, parse_seg_date, route_rule, worse,
)
from .service import ScanService, ServiceConfig
from .storage import Database, hash_password, new_salt

STAT = {"pass": 0, "fail": 0}
FAILS: list = []


def chk(name: str, ok: bool, detail: str = ""):
    if ok:
        STAT["pass"] += 1
        print(f"  OK   {name}")
    else:
        STAT["fail"] += 1
        FAILS.append(f"{name}  {detail}")
        print(f"  FAIL {name}   {detail}")


def section(t: str):
    print(f"\n===== {t} =====")


# ==================================================== 真实样本（现场提供）
S1 = "10000000001%DM-A0000-000000000%1%1%1%261110%%202611101794290805404220443%280"
S2 = "20000000002C0126081604551"
S3 = "3000000003-88888888-008"
S4 = "400004/555555"
S5 = "ABCD01+555-0000*"
SAMPLES = [S1, S2, S3, S4, S5]


def cond(ctype, operand, param="", required=True, negate=False,
         fail_action="", fail_msg=""):
    return Condition(ctype=ctype, operand=operand, op_param=param,
                     required=required, negate=negate,
                     fail_action=fail_action, fail_msg=fail_msg)


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qrrule_"))
    try:
        _run(tmp)
    except Exception:
        STAT["fail"] += 1
        FAILS.append("未捕获异常")
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"通过 {STAT['pass']} 项，失败 {STAT['fail']} 项")
    if FAILS:
        print("失败清单：")
        for f in FAILS:
            print("  - " + f)
    return STAT["fail"]


def _run(tmp: Path):
    # -------------------------------------------------- 归一化
    section("归一化")
    chk("去首尾空白", normalize("  ABC  ") == "ABC")
    chk("忽略大小写", normalize("AbC", ignore_case=True) == "ABC")
    chk("全角转半角", normalize("Ａ１，", fullwidth=True) == "A1,")
    chk("全角空格", normalize("A　B", fullwidth=True) == "A B")
    chk("不传参保持原样", normalize("Ａb") == "Ａb")

    # -------------------------------------------------- 字符集
    section("字符集解析")
    cs = parse_charset("A-Z0-9%/+*-")
    chk("含大写区间", "A" in cs and "Z" in cs)
    chk("含数字区间", "0" in cs and "9" in cs)
    chk("含 %", "%" in cs)
    chk("含 /", "/" in cs)
    chk("含 -", "-" in cs)
    chk("含 *", "*" in cs)
    chk("不含小写", "a" not in cs)
    chk("不含中文", "中" not in cs)

    # -------------------------------------------------- 条件：字面量
    section("条件求值——特殊字符必须按字面量")
    chk("CONTAINS * 是星号不是通配",
        eval_condition(cond("CONTAINS", "*"), S5)[0] is True
        and eval_condition(cond("CONTAINS", "*"), "ABC")[0] is False)
    chk("CONTAINS + 是加号不是量词",
        eval_condition(cond("CONTAINS", "+"), S5)[0] is True
        and eval_condition(cond("CONTAINS", "+"), "A+B")[0] is True)
    chk("CONTAINS % 不触发解码",
        eval_condition(cond("CONTAINS", "%DM-A0000"), S1)[0] is True)
    chk("CONTAINS 不存在的串", eval_condition(cond("CONTAINS", "ZZZ"), S1)[0] is False)
    chk("NOT_CONTAINS 成立", eval_condition(cond("NOT_CONTAINS", "999"), S3)[0] is True)
    chk("NOT_CONTAINS 不成立", eval_condition(cond("NOT_CONTAINS", "8888"), S3)[0] is False)
    chk("STARTS_WITH", eval_condition(cond("STARTS_WITH", "3000000003"), S3)[0])
    chk("STARTS_WITH 失败", not eval_condition(cond("STARTS_WITH", "9999"), S3)[0])
    chk("ENDS_WITH 008", eval_condition(cond("ENDS_WITH", "008"), S3)[0])
    chk("EQUALS 全等", eval_condition(cond("EQUALS", S4), S4)[0])
    chk("EQUALS 差一位即失败", not eval_condition(cond("EQUALS", S4 + "1"), S4)[0])
    chk("REGEX 显式使用", eval_condition(cond("REGEX", r"^\d{10}"), S3)[0])
    chk("非法正则不崩", eval_condition(cond("REGEX", "(["), S3)[0] is False)
    chk("LENGTH 区间", eval_condition(cond("LENGTH", "20-30"), S2)[0])
    chk("LENGTH 区间外", not eval_condition(cond("LENGTH", "10-20"), S2)[0])
    chk("CHARSET 合法", eval_condition(cond("CHARSET", "A-Z0-9%/+-*"), S5)[0])
    chk("CHARSET 抓中文", not eval_condition(cond("CHARSET", "A-Z0-9%/+-*"), "ABCD01中")[0])
    chk("negate 取反", eval_condition(cond("CONTAINS", "8888", negate=True), S3)[0] is False)

    # -------------------------------------------------- 条件：分段
    section("条件求值——分段（空段必须保留）")
    parts = S1.split("%")
    chk("S1 共 9 段", len(parts) == 9, f"实际 {len(parts)}: {parts}")
    chk("S1 第 7 段是空串", parts[6] == "", f"实际 {parts[6]!r}")
    chk("SEGMENT 第1段等值",
        eval_condition(cond("SEGMENT", "DM-A0000-000000000", '{"sep":"%","idx":1,"cmp":"EQUALS"}'), S1)[0])
    chk("SEGMENT 第1段不符",
        not eval_condition(cond("SEGMENT", "XXX", '{"sep":"%","idx":1,"cmp":"EQUALS"}'), S1)[0])
    chk("SEGMENT 取空段成功",
        eval_condition(cond("SEGMENT", "", '{"sep":"%","idx":6,"cmp":"EQUALS"}'), S1)[0])
    chk("SEGMENT 越界要失败不崩",
        eval_condition(cond("SEGMENT", "x", '{"sep":"%","idx":20,"cmp":"EQUALS"}'), S1)[0] is False)
    chk("SEGMENT 斜杠分隔",
        eval_condition(cond("SEGMENT", "555555", '{"sep":"/","idx":1,"cmp":"EQUALS"}'), S4)[0])
    chk("SEGMENT 横杠分隔共3段", len(S3.split("-")) == 3)

    # -------------------------------------------------- 条件：日期
    section("条件求值——日期")
    today = date.today()
    yymmdd = today.strftime("%y%m%d")
    yyyymmdd = today.strftime("%Y%m%d")
    chk("parse_seg_date YYMMDD", parse_seg_date(yymmdd, "YYMMDD") == today)
    chk("parse_seg_date YYYYMMDD", parse_seg_date(yyyymmdd, "YYYYMMDD") == today)
    chk("parse_seg_date 非法返回 None", parse_seg_date("999999", "YYMMDD") is None)
    code_now = f"X%{yymmdd}%Y"
    chk("DATE_VALID 今天通过",
        eval_condition(cond("DATE_VALID", "", f'{{"sep":"%","idx":1,"fmt":"YYMMDD"}}'), code_now)[0])
    old = (today - timedelta(days=800)).strftime("%y%m%d")
    code_old = f"X%{old}%Y"
    chk("DATE_VALID 八百天前超期",
        not eval_condition(cond("DATE_VALID", "", f'{{"sep":"%","idx":1,"fmt":"YYMMDD"}}'), code_old)[0])
    code_match = f"A%{yymmdd}%B%{yyyymmdd}%C"
    chk("DATE_MATCH 两处一致",
        eval_condition(cond("DATE_MATCH", "",
                            '{"sep":"%","idx_a":1,"fmt_a":"YYMMDD","idx_b":3,"fmt_b":"YYYYMMDD"}'),
                       code_match)[0])
    other = (today - timedelta(days=3)).strftime("%Y%m%d")
    code_bad = f"A%{yymmdd}%B%{other}%C"
    chk("DATE_MATCH 两处不一致要抓出来",
        not eval_condition(cond("DATE_MATCH", "",
                                '{"sep":"%","idx_a":1,"fmt_a":"YYMMDD","idx_b":3,"fmt_b":"YYYYMMDD"}'),
                           code_bad)[0])

    # -------------------------------------------------- 动作严重度
    section("动作严重度")
    chk("锁屏最严重", worse("BLOCK", "LOCK_SCREEN") == "LOCK_SCREEN")
    chk("顺序无关", worse("LOCK_SCREEN", "BLOCK") == "LOCK_SCREEN")
    chk("PASS 最轻", worse("PASS", "WARN") == "WARN")
    chk("等级递增", ACTION_RANK["PASS"] < ACTION_RANK["BLOCK"] < ACTION_RANK["LOCK_SCREEN"])

    # -------------------------------------------------- 条件组合
    section("条件组合逻辑")
    r = Rule(part_no="T", match_logic="AND")
    rs = [type("X", (), {"cond": cond("CONTAINS", "A"), "ok": True, "detail": ""})(),
          type("X", (), {"cond": cond("CONTAINS", "B"), "ok": False, "detail": "缺 B"})()]
    ok, msg = combine_logic(r, rs)
    chk("AND 有一条失败即失败", not ok, msg)
    r.match_logic = "OR"
    chk("OR 有一条通过即通过", combine_logic(r, rs)[0])
    r.match_logic = "AT_LEAST"
    r.at_least_n = 2
    chk("AT_LEAST 不满足", not combine_logic(r, rs)[0])
    r.at_least_n = 1
    chk("AT_LEAST 满足", combine_logic(r, rs)[0])
    chk("无条件是直接通过", combine_logic(Rule(), [])[0])

    # -------------------------------------------------- 路由
    section("料号路由")
    r1 = Rule(part_no="A", identify_type="PREFIX", identify_value="3000", priority=100)
    r2 = Rule(part_no="B", identify_type="PREFIX", identify_value="3000000003", priority=50)
    got = route_rule([r1, r2], S3)
    chk("优先级小的先命中", got is not None and got.part_no == "B",
        f"实际 {got.part_no if got else None}")
    r3 = Rule(part_no="C", identify_type="PREFIX", identify_value="400004")
    chk("S4 路由到 C", route_rule([r1, r2, r3], S4).part_no == "C")
    r4 = Rule(part_no="D", identify_type="SEGMENT", identify_value="DM-A0000-000000000",
              identify_param='{"sep":"%","idx":1}')
    chk("S1 按分段识别", route_rule([r4], S1).part_no == "D")
    chk("无规则返回 None", route_rule([r1], "ZZZZ") is None)

    # -------------------------------------------------- 存储与判定
    section("存储层与判定链")
    db = Database(tmp / "t1.db")
    chk("建库成功", db.conn is not None)

    # 未匹配 → 默认拦截
    svc = ScanService(db, ServiceConfig(terminal="T1", batch="B1"))
    v = svc.scan("NOSUCHCODE")
    chk("未匹配规则默认拦截", v.action == "BLOCK", v.action)
    chk("拦截不占唯一性", db.stats()["unique"] == 0, str(db.stats()))
    v2 = svc.scan("NOSUCHCODE")
    chk("被拦截的码重扫仍是拦截不是重复", v2.action == "BLOCK" and not v2.is_dup,
        f"{v2.action} dup={v2.is_dup}")

    # 建规则
    rule = Rule(part_no="3000000003", rule_name="3000000003 主标签",
                identify_type="PREFIX", identify_value="3000000003",
                on_pass="PASS", on_violation="BLOCK", on_dup="LOCK_CODE",
                conditions=[
                    cond("CONTAINS", "3000000003"),
                    cond("CONTAINS", "8888"),
                    cond("ENDS_WITH", "008"),
                ])
    db.save_rule(rule)
    svc.reload_rules()
    chk("规则已载入", len(svc.rules) == 1)

    v = svc.scan(S3)
    chk("合规码放行", v.action == "PASS", f"{v.action} / {v.reason}")
    chk("命中方式为自动路由", v.matched_by == "AUTO", v.matched_by)
    chk("唯一码计数为 1", db.stats()["unique"] == 1, str(db.stats()))

    v = svc.scan(S3)
    chk("重复码触发锁码", v.action == "LOCK_CODE", v.action)
    chk("重复次数为 2", v.dup_count == 2, str(v.dup_count))
    chk("重复记录不占唯一性", db.stats()["unique"] == 1, str(db.stats()))
    chk("重复记录了 dup_rows", db.stats()["dup_rows"] == 1, str(db.stats()))
    chk("锁码写入锁定名单", db.is_locked(normalize(S3)) is not None)

    v = svc.scan(S3)
    chk("已锁定的码直接拒", "已被锁定" in v.reason, v.reason)

    # 内容不合规
    svc2_db = tmp / "t2.db"
    db2 = Database(svc2_db)
    db2.save_rule(Rule(part_no="P", identify_type="PREFIX", identify_value="P",
                       on_violation="BLOCK", on_dup="BLOCK",
                       conditions=[cond("CONTAINS", "AAA", fail_msg="必须含 AAA")]))
    s2 = ScanService(db2, ServiceConfig(terminal="T2"))
    v = s2.scan("PBBB")
    chk("条件不满足被拦截", v.action == "BLOCK", v.action)
    chk("拦截原因带条件提示", "必须含 AAA" in v.reason, v.reason)

    # 条件级动作覆盖
    db3 = Database(tmp / "t3.db")
    db3.save_rule(Rule(part_no="Q", identify_type="PREFIX", identify_value="Q",
                       on_violation="BLOCK", on_dup="BLOCK",
                       conditions=[cond("CONTAINS", "MMM", fail_action="WARN")]))
    s3 = ScanService(db3, ServiceConfig(terminal="T3"))
    v = s3.scan("QTTT")
    chk("条件级 WARN 覆盖规则级 BLOCK", v.action == "WARN", v.action)
    chk("WARN 计入有效记录", db3.stats()["valid"] == 1, str(db3.stats()))

    # -------------------------------------------------- 验重范围
    section("验重三种范围")
    for scope, expect in (("GLOBAL", True), ("PART", True), ("NONE", False)):
        d = Database(tmp / f"scope_{scope}.db")
        d.save_rule(Rule(part_no=f"X{scope}", identify_type="PREFIX",
                         identify_value=f"X{scope}", dup_scope=scope,
                         on_dup="LOCK_CODE"))
        s = ScanService(d, ServiceConfig(terminal="TS"))
        s.scan(f"X{scope}-CODE")
        v = s.scan(f"X{scope}-CODE")
        chk(f"{scope} 范围判重={expect}", v.is_dup == expect,
            f"action={v.action} is_dup={v.is_dup} reason={v.reason}")
        d.close()

    # PART 范围：不同料号同码不判重
    d = Database(tmp / "scope_part2.db")
    d.save_rule(Rule(part_no="PA", identify_type="PREFIX", identify_value="PA",
                     dup_scope="PART", on_dup="LOCK_CODE"))
    d.save_rule(Rule(part_no="PB", identify_type="PREFIX", identify_value="PB",
                     dup_scope="PART", on_dup="LOCK_CODE"))
    s = ScanService(d, ServiceConfig(terminal="TP"))
    s.scan("PA-123")
    v = s.scan("PB-123")
    chk("PART 范围不同料号同码不判重", not v.is_dup, f"is_dup={v.is_dup} {v.reason}")
    d.close()

    # -------------------------------------------------- 手选模式
    section("手选料号模式")
    d = Database(tmp / "manual.db")
    d.save_rule(Rule(part_no="M1", identify_type="PREFIX", identify_value="M1",
                     on_dup="BLOCK", conditions=[cond("LENGTH", "3-30")]))
    s = ScanService(d, ServiceConfig(terminal="TM"))
    v = s.scan("M1-XYZ", part_no="M1")
    chk("手选命中", v.matched_by == "MANUAL" and v.action == "PASS", f"{v.matched_by}/{v.action}")
    v = s.scan("M9-XYZ", part_no="M1")
    chk("手选但识别式不符 → 提醒混料", v.action == "WARN" and "混料" in v.reason, v.reason)
    d.close()

    # -------------------------------------------------- 升级策略
    section("升级策略")
    d = Database(tmp / "esc.db")
    d.save_rule(Rule(part_no="E", identify_type="PREFIX", identify_value="E",
                     on_violation="BLOCK", on_dup="BLOCK",
                     escalation='{"count":2,"minutes":480,"action":"LOCK_SCREEN"}',
                     conditions=[cond("CONTAINS", "GOOD")]))
    s = ScanService(d, ServiceConfig(terminal="TE"))
    v1 = s.scan("E-BAD-1")
    chk("首次违规是拦截", v1.action == "BLOCK", v1.action)
    v2 = s.scan("E-BAD-2")
    chk("累计第二次升级为锁屏", v2.action == "LOCK_SCREEN", f"{v2.action} / {v2.escalate_note}")
    d.close()

    # -------------------------------------------------- 状态与导出
    section("统计与导出")
    st = db.stats()
    chk("统计字段齐全",
        all(k in st for k in ("total", "valid", "unique", "dup_rows",
                              "dup_codes", "blocked", "warned", "locked")), str(st))

    csv_p = tmp / "out.csv"
    n = svc.export_csv(csv_p)
    chk("导出 CSV 有内容", csv_p.exists() and n > 0, f"{n} 行")
    chk("CSV 带 BOM 可被 Excel 直开", csv_p.read_bytes()[:3] == b"\xef\xbb\xbf")

    txt_p = tmp / "out.txt"
    m = svc.export_txt(txt_p)
    chk("导出 TXT", txt_p.exists() and m >= 1, f"{m} 行")

    rep_p = tmp / "report.txt"
    text = svc.export_report(rep_p)
    chk("导出报告含统计", "扫码总次数" in text and "按料号" in text)
    chk("报告文件已落盘", rep_p.exists() and rep_p.stat().st_size > 100)

    # -------------------------------------------------- 导入
    section("批量导入")
    tpl = tmp / "tpl.csv"
    importer.write_template(tpl)
    chk("模板已生成", tpl.exists() and tpl.stat().st_size > 200)
    rows = importer.read_table(tpl)
    chk("模板可读回", len(rows) >= 5, f"{len(rows)} 行")

    dimp = Database(tmp / "imp.db")
    plan = importer.build_plan(rows, dimp)
    cnt = plan.counts()
    chk("模板无格式错", cnt["error"] == 0, str(plan.issues[:3]))
    chk("同料号多行合并成一条规则",
        any(r.part_no == "3000000003" and len(r.conditions) == 3 for r in plan.rules),
        str([(r.part_no, len(r.conditions)) for r in plan.rules]))
    n = importer.apply_plan(plan, dimp, actor="selftest")
    chk("导入落库成功", n >= 4, f"{n} 条")
    got = dimp.get_rule_by_part("3000000003")
    chk("导回后条件数正确", got is not None and len(got.conditions) == 3,
        str(len(got.conditions) if got else None))

    s_imp = ScanService(dimp, ServiceConfig(terminal="TI"))
    v = s_imp.scan(S3)
    chk("导入的规则能正确判定真实样本", v.action == "PASS", f"{v.action} / {v.reason}")

    # 冲突检测
    conf = dimp.find_identify_conflict("PREFIX", "3000000", "", 0)
    chk("前缀互相包含判为冲突", len(conf) >= 1, str([c["part_no"] for c in conf]))

    # 导出再导入（往返）
    back = tmp / "back.csv"
    importer.export_rules_csv(back, dimp)
    chk("规则可导出为 CSV", back.exists() and back.stat().st_size > 100)
    rows2 = importer.read_table(back)
    plan2 = importer.build_plan(rows2, Database(tmp / "imp2.db"))
    chk("导出再导入无格式错", plan2.counts()["error"] == 0, str(plan2.issues[:3]))
    total_conds = sum(len(r.conditions) for r in plan2.rules)
    chk("往返后条件总量不减少", total_conds >= 7, f"{total_conds} 条条件")

    # -------------------------------------------------- 密码
    section("解锁密码")
    salt = new_salt()
    h1 = hash_password("abc123", salt)
    h2 = hash_password("abc123", salt)
    h3 = hash_password("abc124", salt)
    chk("同密码同盐哈希一致", h1 == h2)
    chk("不同密码哈希不同", h1 != h3)
    chk("哈希不含明文", "abc123" not in h1)
    d = Database(tmp / "pwd.db")
    d.set_password("Test@2026")
    chk("密码校验通过", d.check_password("Test@2026"))
    chk("错误密码拒绝", not d.check_password("test@2026"))
    d.close()

    # -------------------------------------------------- 真实样本全链路
    section("现场提供的 5 个真实样本")
    d = Database(tmp / "real.db")
    d.save_rule(Rule(part_no="10000000001", identify_type="PREFIX",
                     identify_value="10000000001", on_dup="LOCK_CODE",
                     conditions=[cond("CONTAINS", "10000000001")]))
    d.save_rule(Rule(part_no="20000000002", identify_type="PREFIX",
                     identify_value="20000000002", on_dup="LOCK_CODE",
                     conditions=[cond("LENGTH", "25-25")]))
    d.save_rule(Rule(part_no="3000000003", identify_type="PREFIX",
                     identify_value="3000000003", on_dup="LOCK_CODE",
                     conditions=[cond("CONTAINS", "8888")]))
    d.save_rule(Rule(part_no="400004", identify_type="PREFIX",
                     identify_value="400004", on_dup="LOCK_CODE",
                     conditions=[cond("SEGMENT", "555555",
                                      '{"sep":"/","idx":1,"cmp":"EQUALS"}')]))
    d.save_rule(Rule(part_no="ABCD01", identify_type="PREFIX",
                     identify_value="ABCD01", on_dup="LOCK_CODE",
                     conditions=[cond("CHARSET", "A-Z0-9+/\\-*")]))
    s = ScanService(d, ServiceConfig(terminal="REAL"))
    for i, code in enumerate(SAMPLES, 1):
        v = s.scan(code)
        chk(f"样本{i} 放行（{code[:24]}…）", v.action == "PASS",
            f"{v.action} / {v.reason}")
    chk("5 个样本全部唯一", d.stats()["unique"] == 5, str(d.stats()))
    v = s.scan(S1)
    chk("样本1 重扫判重复", v.is_dup and v.action == "LOCK_CODE", v.action)
    d.close()


if __name__ == "__main__":
    import sys
    sys.exit(run())
