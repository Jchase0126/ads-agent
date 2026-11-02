# -*- coding: utf-8 -*-
"""规则登记表自检 —— 守住"规则之间必须有关联"这件事。

背景（2026-10-30）：建图规则过去散在四处 —— 提示词（backend/agent.py）、
工具 schema（backend/tools.py）、ads_ops.py 里三套各自实现的几何判定、
以及一个从未被 import 的 geometry_policy.py。同一个概念（例如"导线不许穿
符号体"）在四处各写一遍、口径还不同，改一处不会带动其余三处。

现在规则集中在 ``addon/ads_agent/schematic_rules.py``：每条规则有编号、
阶段、严重级、判定原语、代码位置、修复建议、覆盖测试、提示词出处，以及
**与其他规则的关联**。这个文件就是那份关联的机器守卫：

* 关联必须指向真实存在的规则号（``audit_relations`` 为空）；
* 每条规则至少有一条关联（不允许孤立条目 —— 用户原话）；
* 阻断级规则必须能追溯到代码位置、并且有测试或提示词出处；
* 关系必须对称（A 说和 B 互相弥补，B 也必须记着 A）；
* 判定原语必须唯一 —— 同一个几何事实不能有两套判定。

运行::

    python tests/test_schematic_rules.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "backend"), os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _harness import eq, ok, run  # noqa: E402

import schematic_rules as SR  # noqa: E402


# ---------------------------------------------------------------------------
# 关联闭合性
# ---------------------------------------------------------------------------

def test_relations_are_closed():
    """关联指向的规则号必须存在，且不能指向自己。"""
    eq(SR.audit_relations(), [], "规则表的关联关系必须闭合（空列表 = 全部有效）")


def test_no_orphan_rule():
    """每条规则至少有一条关联 —— 不允许孤立条目。

    这是用户对本次整合提出的核心要求：规则不是一张清单，而是一张网。
    """
    orphans = [r.id for r in SR.ALL_RULES.values() if not r.related]
    eq(orphans, [], "以下规则没有任何关联，属于孤立条目")


def test_relations_are_symmetric():
    """A 说与 B 有某种关系，B 也必须记着 A。"""
    missing = []
    for r in SR.ALL_RULES.values():
        for other, _rel in r.related:
            target = SR.ALL_RULES[other]
            names = [o for o, _ in target.related]
            if r.id not in names:
                missing.append(f"{r.id} -> {other} 但 {other} 没记着 {r.id}")
    eq(missing, [], "关联必须双向登记")


def test_relation_vocabulary_is_known():
    """关联词汇必须是 REL_* 常量之一。"""
    known = {
        SR.REL_SAME_PRIMITIVE, SR.REL_SAME_ROOT, SR.REL_COMPENSATES,
        SR.REL_ESCALATES, SR.REL_EXCLUDES, SR.REL_DEPENDS,
    }
    bad = []
    for r in SR.ALL_RULES.values():
        for other, rel in r.related:
            if rel not in known:
                bad.append(f"{r.id} -> {other}: {rel}")
    eq(bad, [], "关联词汇必须取自 REL_* 常量")


# ---------------------------------------------------------------------------
# 元数据完整性
# ---------------------------------------------------------------------------

def test_rule_ids_are_unique_and_numbered():
    """编号唯一且形如 R-两位数字。"""
    ids = [r.id for r in SR.ALL_RULES.values()]
    eq(len(ids), len(set(ids)), "规则号必须唯一")
    bad = [i for i in ids if not (i.startswith("R-") and len(i) == 4 and i[2:].isdigit())]
    eq(bad, [], "规则号必须形如 R-01")


def test_every_rule_has_stage_and_severity():
    """阶段与严重级必须是已知枚举。"""
    bad = []
    for r in SR.ALL_RULES.values():
        if r.severity not in SR.SEVERITY_LABEL:
            bad.append(f"{r.id} severity={r.severity}")
        if r.primitive not in SR.PRIMITIVE_LABEL:
            bad.append(f"{r.id} primitive={r.primitive}")
        if not r.stages or any(s not in SR.STAGE_LABEL for s in r.stages):
            bad.append(f"{r.id} stages={r.stages}")
    eq(bad, [], "阶段/严重级/原语必须取自常量表")


def test_blocker_has_code_location():
    """阻断级规则必须写清代码位置 —— 否则报错文案指向不了实现。"""
    bad = [r.id for r in SR.ALL_RULES.values()
           if r.severity == SR.BLOCK and not r.code.strip()]
    eq(bad, [], "阻断规则必须有 code 定位串")


def test_blocker_has_test_or_prompt():
    """阻断级规则要么有测试覆盖，要么在提示词/schema 里有出处。"""
    bad = [r.id for r in SR.ALL_RULES.values()
           if r.severity == SR.BLOCK and not (r.test.strip() or r.prompt.strip())]
    eq(bad, [], "阻断规则必须有测试或提示词出处")


def test_every_rule_has_message_and_fix():
    """每条规则都要有一句人话与修复建议 —— 报错文案直接取 message。"""
    bad = [r.id for r in SR.ALL_RULES.values() if not r.message.strip() or not r.fix.strip()]
    eq(bad, [], "规则必须有 message 与 fix")


# ---------------------------------------------------------------------------
# 判定原语唯一性
# ---------------------------------------------------------------------------

def test_one_primitive_per_geometry_fact():
    """同一几何事实（段×盒/段×引脚/段×段）只能由一个原语承载。

    过去 ``_segment_hits_box`` 在 ads_ops.py 有两套（规划期/复核期各一份），
    ``segment_relation`` 也有两套（policy 与 ``_seg_violation``）。这里把
    "盒判定"相关规则的原语钉死为 PRIM_BOX，"段关系"钉死为 PRIM_SEG，防止
    以后又分叉出一套。
    """
    box_rules = {"R-02", "R-04", "R-16"}
    seg_rules = {"R-05", "R-06", "R-07", "R-08", "R-09", "R-10", "R-11", "R-12", "R-13"}
    bad = []
    for rid in box_rules:
        if SR.RULES[rid].primitive != SR.PRIM_BOX:
            bad.append(f"{rid} 应为 PRIM_BOX，实际 {SR.RULES[rid].primitive}")
    for rid in seg_rules:
        if SR.RULES[rid].primitive != SR.PRIM_SEG:
            bad.append(f"{rid} 应为 PRIM_SEG，实际 {SR.RULES[rid].primitive}")
    eq(bad, [], "判定原语必须唯一")


# ---------------------------------------------------------------------------
# 分级查表（过去是 _geometry_report 里的 if/elif 树）
# ---------------------------------------------------------------------------

def test_classify_covers_all_relations():
    """段关系 × 网络归属的 9 种组合都必须能查到规则号。"""
    cases = {
        ("cross", "different"): "R-05",
        ("overlap", "different"): "R-06",
        ("t", "different"): "R-07",
        ("cross", "same"): "R-08",
        ("overlap", "same"): "R-09",
        ("t", "same"): "R-10",
        ("cross", "unknown"): "R-11",
        ("overlap", "unknown"): "R-11",
        ("t", "unknown"): "R-12",
    }
    for (kind, net_kind), want in cases.items():
        if net_kind == "different":
            got = SR.classify_wire_relation(kind, "N1", "N2")
        elif net_kind == "same":
            got = SR.classify_wire_relation(kind, "N1", "N1")
        else:
            got = SR.classify_wire_relation(kind, None, None)
        eq(got, want, f"{kind}/{net_kind} 应归到 {want}")


def test_t_at_pin_is_not_a_violation():
    """T 形搭接点正好落在引脚上 = 明确结点，不产生任何规则。"""
    eq(SR.classify_wire_relation("t", "N1", "N2", at_pin=True), None,
       "T 接点有引脚时不应报违规")


def test_same_net_relations_are_warnings():
    """同网交叉/重叠/T 只警告 —— 电学上无影响，别阻断建图。"""
    for rid in ("R-08", "R-09", "R-10"):
        eq(SR.severity_of(rid), SR.WARN, f"{rid} 应为 warning")


def test_cross_net_relations_block():
    """异网交叉/重叠/T 阻断 —— 这是真实的短路。"""
    for rid in ("R-05", "R-06", "R-07"):
        eq(SR.severity_of(rid), SR.BLOCK, f"{rid} 应为 block")


# ---------------------------------------------------------------------------
# 文案前缀（报错/问题文案必须带规则号，才能反查规则表）
# ---------------------------------------------------------------------------

def test_format_violation_prefixes_rule_id():
    """``format_violation`` 必须产出 ``[R-xx 名称]`` 前缀。"""
    text = SR.format_violation("R-02", "导线穿过元件 MLa 的符号内部")
    ok(text.startswith("[R-02"), f"前缀应为 [R-02 …]，实际 {text!r}")
    ok("穿过元件 MLa" in text, "原始细节必须保留（既有测试按子串断言）")


def test_format_unknown_rule_returns_detail():
    """未知规则号不得吞掉原始细节。"""
    eq(SR.format_violation("R-99", "原始细节"), "原始细节", "未知规则号应原样返回")


def test_format_stage_violation_lists_stage_blockers():
    """阶段级文案必须列出该阶段的阻断规则号。"""
    text = SR.format_stage_violation(SR.STAGE_WRITE, "无法找到正交走线")
    for rid in SR.blockers(SR.STAGE_WRITE):
        ok(rid in text, f"阶段文案应含 {rid}，实际 {text!r}")
    ok("无法找到正交走线" in text, "原始细节必须保留")


# ---------------------------------------------------------------------------
# 阶段视图
# ---------------------------------------------------------------------------

def test_blockers_shrink_along_pipeline():
    """阶段越晚，能拦的阻断规则不会变少（entry → verify）。"""
    eq(sorted(SR.blockers(SR.STAGE_ENTRY)),
       sorted({"R-19", "R-20", "R-21"}), "entry 阶段阻断项")
    ok("R-01" in SR.blockers(SR.STAGE_WRITE), "write 阶段要拦斜段")
    ok("R-23" in SR.blockers(SR.STAGE_VERIFY), "verify 阶段要拦网表不等价")


def test_layout_rules_are_advisory():
    """布局规则全是 info —— 它们描述"会怎么摆"，不是"违规"。"""
    bad = [r.id for r in SR.LAYOUT_RULES.values() if r.severity != SR.INFO]
    eq(bad, [], "布局规则不应阻断")


# ---------------------------------------------------------------------------
# 代码位置可追溯
# ---------------------------------------------------------------------------

def test_code_locations_point_at_real_files():
    """``code`` 里提到的文件必须存在，行号必须为正。"""
    import re
    missing = []
    for r in SR.ALL_RULES.values():
        for part in r.code.split(" / "):
            m = re.match(r"([\w.]+):(\d+)", part.strip())
            if not m:
                continue
            name, line = m.group(1), int(m.group(2))
            path = os.path.join(ROOT, "addon", "ads_agent", name)
            if not os.path.exists(path):
                missing.append(f"{r.id}: 找不到 {name}")
            elif line < 1:
                missing.append(f"{r.id}: {name}:{line} 行号非法")
    eq(missing, [], "code 定位必须指向真实文件")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
