# -*- coding: utf-8 -*-
"""从 schematic_rules.py 生成《原理图建图规则手册》。

手册不手写 —— 手写的文档必然与规则表脱节。这里直接读规则表，产出
``docs/原理图建图规则手册.md``：

* 阶段流水线：每个阶段能拦住什么、靠哪条规则；
* 规则总表：编号 / 名称 / 严重级 / 阶段 / 判定原语 / 代码位置 / 测试 / 提示词；
* 关联图：每条规则的关联规则与关系词汇（这张图就是"规则不是孤立条目"的证据）；
* 原语归属：同一个几何事实只由一份实现承载。

用法::

    python tools/gen_rule_handbook.py           # 只打印统计
    python tools/gen_rule_handbook.py --write   # 写 docs/原理图建图规则手册.md
"""

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "addon", "ads_agent"))

import schematic_rules as SR  # noqa: E402

OUT = os.path.join(ROOT, "docs", "原理图建图规则手册.md")

STAGE_ORDER = [SR.STAGE_ENTRY, SR.STAGE_LAYOUT, SR.STAGE_PLAN,
               SR.STAGE_WRITE, SR.STAGE_VERIFY, SR.STAGE_SIM]

SEV_MARK = {SR.BLOCK: "阻断", SR.WARN: "警告", SR.INFO: "提示"}


def _cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def stage_section():
    lines = [
        "## 2. 阶段流水线：每一步能拦住什么",
        "",
        "同一张规则表按阶段切开就是流水线视图。一个规则可以出现在多个阶段 ——",
        "这意味着它在更早的地方就被拦下，不必等到复核。",
        "",
        "| 阶段 | 名称 | 生效规则数 | 阻断规则 |",
        "| --- | --- | --- | --- |",
    ]
    for st in STAGE_ORDER:
        rules = SR.by_stage(st)
        blk = sorted(SR.blockers(st), key=lambda x: int(x.split("-")[1]))
        lines.append("| `%s` | %s | %d | %s |" % (
            st, SR.STAGE_LABEL[st], len(rules),
            "、".join(blk) if blk else "（无）"))
    lines += [
        "",
        "要点：",
        "",
        "* **入参阶段**只拦三件事：坐标尺度（R-19）、auto 用于非空设计（R-20）、",
        "  备份失败还硬要重建（R-21）——都是\"还没动设计就该拒绝\"的。",
        "* **规划阶段**（`plan_wire_routes`）与**写入阶段**（`_route_clear`）拦的是",
        "  同一批硬规则，但规划期用的是符号库静态引脚偏移，写入期才用实测引脚 ——",
        "  所以规划放行的线仍可能被写入期拒，这不是 bug，是 R-02 的豁免条件不同。",
        "* **复核阶段**规则最多：除了重查硬规则，还多出只有落盘后才能判的项",
        "  （R-12 无引脚 T 接、R-13 重复线段、R-14 平行出线、R-22 参数未落盘、",
        "  R-23 网表不等价、R-24 连线不同网）。",
        "* **仿真阶段**的规则在建图时**不拦**，只写进 `sim_readiness`。",
        "",
    ]
    return lines


def table_section():
    lines = [
        "## 3. 规则总表",
        "",
        "| 编号 | 名称 | 严重级 | 阶段 | 原语 | 代码位置 | 固化测试 | 提示词出处 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rid in sorted(SR.RULES, key=lambda x: int(x.split("-")[1])):
        r = SR.RULES[rid]
        lines.append("| %s | %s | %s | %s | %s | `%s` | %s | %s |" % (
            r.id, _cell(r.name), SEV_MARK[r.severity],
            "/".join(SR.STAGE_LABEL[s] for s in r.stages),
            SR.PRIMITIVE_LABEL[r.primitive],
            _cell(r.code), _cell(r.test) or "—", _cell(r.prompt) or "—"))
    lines += ["", "### 3.1 布局规则（只描述\"会怎么摆\"，不判违规）", "",
              "| 编号 | 名称 | 阶段 | 代码位置 | 说明 |",
              "| --- | --- | --- | --- | --- |"]
    for rid in sorted(SR.LAYOUT_RULES, key=lambda x: int(x.split("-")[1])):
        r = SR.LAYOUT_RULES[rid]
        lines.append("| %s | %s | %s | `%s` | %s |" % (
            r.id, _cell(r.name), "/".join(SR.STAGE_LABEL[s] for s in r.stages),
            _cell(r.code), _cell(r.message)))
    lines.append("")
    return lines


def relation_section():
    lines = [
        "## 4. 关联图：规则之间怎么互相牵连",
        "",
        "关系词汇（`schematic_rules.REL_*`）：",
        "",
        "| 词汇 | 含义 |",
        "| --- | --- |",
    ]
    meaning = {
        SR.REL_SAME_PRIMITIVE: "同一次判定产出的不同结论（如交叉/重叠/T 都是段关系）",
        SR.REL_SAME_ROOT: "根因相同，修一个通常修另一个",
        SR.REL_COMPENSATES: "一个宽松时另一个兜底（规划放过 → 复核拦住）",
        SR.REL_ESCALATES: "跨阶段升级（早阶段只提示、晚阶段阻断）",
        SR.REL_EXCLUDES: "互斥，两条不可能同时成立",
        SR.REL_DEPENDS: "前置依赖，前者失败则后者无法判定",
    }
    for k, v in meaning.items():
        lines.append("| %s | %s |" % (k, v))
    lines += ["", "### 4.1 每条规则牵连了谁", ""]
    g = SR.graph()
    for rid in sorted(g, key=lambda x: int(x.split("-")[1])):
        r = SR.ALL_RULES[rid]
        rows = g[rid]
        desc = "；".join("%s %s(%s)" % (rel, other, name) for rel, other, name in rows)
        lines.append("* **%s %s** — %s" % (r.id, r.name, desc))
    lines.append("")
    return lines


def chain_section():
    """几条真实故障链：把关联串成故事，比列表更能说明"为什么相关"。"""
    return [
        "## 5. 三条真实故障链（关联为什么有用）",
        "",
        "### 链 1：J310 穿体（2026-10-14）",
        "",
        "`R-02 → R-04 → R-31`（互相弥补 + 跨阶段升级）。规划期 `_segment_hits_box`",
        "在 `strict=True` 且有引脚表时，**只要有一端落在引脚上就整段豁免** —— 这根线",
        "确实从封装 L 引脚进来，于是规划期放行；但它停在 FET 核心中间，没落在",
        "gate/drain/source 任何引脚上，复核期 `_through_sym_pin` 用\"体内深度 ≤ 0.5\"",
        "兜住了它。这条链说明：**豁免不能只看一端**，所以 R-04 的贴边骑行判定和",
        "R-02 必须一起看，否则会把\"从引脚进去的短线\"误判成合法。",
        "",
        "### 链 2：规划说合法、落盘说短路",
        "",
        "`R-05/R-06/R-07 → R-08/R-09/R-10 → R-11/R-12`（同原语 + 前置依赖）。",
        "段关系的三种形态（交叉/重叠/T）乘上网络归属（异网/同网/未知）共 9 种组合，",
        "过去是在 `_geometry_report` 里用一棵 if/elif 树逐条写的；现在全部由",
        "`classify_wire_relation()` 一张表决定。**网络归属判不出来时（端点没落在任何",
        "引脚上）不是当\"没事\"，而是降级到 R-11/R-12** —— 这条依赖关系写进了表里。",
        "",
        "### 链 3：提示词承诺了代码没做的事",
        "",
        "`R-16 → R-02`（同原语）+ 提示词 `:405` 原文\"文字绝不被导线穿过\"。",
        "实际上 `_annotation_issues` 只把穿文字记成 **warning**，不阻断；",
        "R-17/R-18（标注悬空/互压）也是 INFO。现在提示词改成了\"R-16 只记 warning，",
        "仍需自己看一眼 annotation_issues\" —— 承诺与实现对齐。",
        "",
    ]


def primitive_section():
    lines = [
        "## 6. 判定原语：一个几何事实只有一份实现",
        "",
        "| 原语 | 承载规则 | 唯一实现 |",
        "| --- | --- | --- |",
    ]
    impl = {
        SR.PRIM_BOX: "`geometry_policy.segment_hits_box(a,b,box,strict,pins,eps)`",
        SR.PRIM_PIN: "`geometry_policy.segment_hits_pin(seg,pt)`",
        SR.PRIM_SEG: "`geometry_policy.segment_relation` / `segment_hits_route`",
        SR.PRIM_ORDER: "`_geometry_report` 步骤 6（折线顺序分析）",
        SR.PRIM_SCALE: "`_layout_scale_issue`",
    }
    for prim in (SR.PRIM_BOX, SR.PRIM_PIN, SR.PRIM_SEG, SR.PRIM_ORDER, SR.PRIM_SCALE):
        ids = sorted((r.id for r in SR.ALL_RULES.values() if r.primitive == prim),
                     key=lambda x: int(x.split("-")[1]))
        if not ids:
            continue
        lines.append("| %s | %s | %s |" % (
            SR.PRIMITIVE_LABEL[prim], "、".join(ids), impl[prim]))
    lines += [
        "",
        "**容差没有统一，这是有意的。** 三个调用点历史取值不同（规划/写入 1e-8、",
        "复核 1e-6），函数通过显式 `eps` 参数接住，不做暗中抹平 —— 悄悄统一会改变",
        "既有的判定结果，属于未经测量的行为变更。",
        "",
    ]
    return lines


def howto_section():
    return [
        "## 7. 怎么加/改一条规则",
        "",
        "1. 在 `addon/ads_agent/schematic_rules.py` 的 `RULES`（或 `LAYOUT_RULES`）里",
        "   加一条 `_R(...)`，填齐 12 个位置参数。`related` 至少要有一条 ——",
        "   `test_no_orphan_rule` 会拦住孤立条目。",
        "2. 报错点改成 `schematic_rules.format_violation(\"R-xx\", detail)`；",
        "   如果一个判定同时查多条规则，用 `format_stage_violation(stage, detail)`。",
        "3. 跑 `python tools/refresh_rule_locations.py --write` 刷新 `code` 行号。",
        "4. 跑 `python tests/test_schematic_rules.py` —— 20 项必须全绿。",
        "5. 如果是用户可见的阻断项，同步 `backend/agent.py` 与 `backend/tools.py`",
        "   的描述，把规则号写进去（手册第 3 节的\"提示词出处\"列就是这两处的坐标）。",
        "6. `python tools/gen_rule_handbook.py --write` 重生成本手册。",
        "",
        "守住规则表的四道闸门：",
        "",
        "* `audit_relations()` 返回空（关联闭合、双向、无孤立）；",
        "* `tests/test_schematic_rules.py` 20 项全绿；",
        "* 核心几何/布局 113 项测试全绿；",
        "* `tools/refresh_rule_locations.py` 无 `LookupError`（锚点没漂）。",
        "",
    ]


def build():
    n_blk = sum(1 for r in SR.ALL_RULES.values() if r.severity == SR.BLOCK)
    n_warn = sum(1 for r in SR.ALL_RULES.values() if r.severity == SR.WARN)
    n_info = sum(1 for r in SR.ALL_RULES.values() if r.severity == SR.INFO)
    n_rel = sum(len(r.related) for r in SR.ALL_RULES.values()) // 2
    head = [
        "# 原理图建图规则手册",
        "",
        "> 本文件由 `tools/gen_rule_handbook.py` 从 `addon/ads_agent/schematic_rules.py`",
        "> 生成，**不要手改** —— 改了会被下一次生成覆盖。要改规则请改规则表。",
        "",
        "## 1. 一句话结论",
        "",
        "建图规则现在有**唯一一份登记表**（`addon/ads_agent/schematic_rules.py`）：",
        "%d 条判定规则 + %d 条布局规则，其中阻断 %d、警告 %d、提示 %d，合计 %d 条关联关系。" %
        (len(SR.RULES), len(SR.LAYOUT_RULES), n_blk, n_warn, n_info, n_rel),
        "",
        "三条原先互相打架的判定（规划期 `plan_wire_routes`、写入期 `_route_clear`、",
        "复核期 `_geometry_report`）现在调用同一批原语；每个报错/问题文案带 `[R-xx 名称]`",
        "前缀，能凭前缀回查本手册。",
        "",
        "```",
        "build_schematic 阶段          规则表里的阶段常量",
        "  入参校验                    STAGE_ENTRY   → R-19/R-20/R-21",
        "  auto 布局                   STAGE_LAYOUT  → R-30..R-35（只描述摆法）",
        "  走线规划 plan_wire_routes   STAGE_PLAN    → 硬规则，静态引脚偏移",
        "  写入 _route_clear/_wire_*   STAGE_WRITE   → 硬规则，实测引脚",
        "  保存后复核 _geometry_report STAGE_VERIFY  → 硬规则 + 仅落盘可判的项",
        "  仿真前 _gate_problems       STAGE_SIM     → 建图时不拦，只写 sim_readiness",
        "```",
        "",
    ]
    out = head + stage_section() + table_section() + relation_section() \
        + chain_section() + primitive_section() + howto_section()
    return "\n".join(out) + "\n"


def main():
    text = build()
    if "--write" in sys.argv:
        io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
        print("已写入 %s（%d 行）" % (os.path.relpath(OUT, ROOT), text.count("\n")))
    else:
        print(text)


if __name__ == "__main__":
    main()
