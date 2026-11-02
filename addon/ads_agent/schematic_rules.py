# -*- coding: utf-8 -*-
"""原理图建图规则的**单一登记处**（纯数据 + 纯函数，不 import 任何 ADS 模块）。

为什么有这个模块
================
在此之前，同一个"规则"在工程里以四种互不引用的形态存在：

* **规划期** ``plan_wire_routes.blocked()``（ads_ops.py:5167）自己写一遍判定；
* **写入期** ``_route_clear()``（ads_ops.py:1242）再写一遍；
* **复核期** ``_geometry_report()``（ads_ops.py:1638）写第三遍，还自带一套
  按网络归属的 problems/warnings 分级（:1834-1882）；
* **提示词/schema**（backend/agent.py、backend/tools.py）用自然语言描述第四遍。

四份实现之间没有指针，于是出现三类长期缺陷：

1. 规划说合法、落盘复核说穿体（J310，2026-10-14）；
2. 同一条规则在两个阶段严重级不同（``parallel_escape``、``tap_no_pin``
   在 ``geometry_policy.py`` 里被列为 SOFT，在 live 复核里是阻断 problems）；
3. 提示词声称"文字绝不被导线穿过"，而代码只报告不拦截。

本模块建立**唯一一份规则表**，每条规则带稳定编号 ``R-xx``，并显式声明
它的判定原语、生效阶段、严重级、代码位置、测试、提示词出处与**关联规则**
（``related`` 字段）。关联是这张表的核心价值 —— 规则不再是孤立条目：
每条规则的失效会影响哪些其它规则、它和谁是同一个原语的两种表现、
在哪两个阶段之间发生了"升级/降级"，都写在表里。

设计原则：统一实现，但不统一容差
================================
三个判定原语（段×符号盒、段×引脚点、段×已存导线）统一到
``geometry_policy.py`` 的**同一份实现**；但**容差不做暗合并** ——
每个调用点通过显式 ``eps`` 参数传入自己历史上的取值
（``_segment_hits_box``/``_segment_hits_route`` 用 1e-8，
``_seg_violation`` 用 1e-6）。“悄悄把三套容差统一成 1e-6”会改变
既有判定结果，属于未经测量的行为变更，本模块不这么做。

阶段
----
============================  ==========================================
``STAGE_ENTRY``               build_schematic 入参校验（尚未动设计）
``STAGE_LAYOUT``              auto_layout_positions 布局
``STAGE_PLAN``                plan_wire_routes 规划折点
``STAGE_WRITE``               _route_clear / _wire_connection 实际绘制
``STAGE_VERIFY``              保存后只读重开复核（图已落盘）
``STAGE_SIM``                 run_simulation 前仿真门禁
============================  ==========================================

严重级
------
``BLOCK``  进 problems / raise，阻断写入或阻断仿真
``WARN``   进 warnings，可见但不阻断
``INFO``   只进独立的 issue 列表（annotation_issues 等），不阻断
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 阶段与严重级
# ---------------------------------------------------------------------------

STAGE_ENTRY = "entry"
STAGE_LAYOUT = "layout"
STAGE_PLAN = "plan"
STAGE_WRITE = "write"
STAGE_VERIFY = "verify"
STAGE_SIM = "sim"

ALL_STAGES = (STAGE_ENTRY, STAGE_LAYOUT, STAGE_PLAN, STAGE_WRITE,
              STAGE_VERIFY, STAGE_SIM)

STAGE_LABEL = {
    STAGE_ENTRY: "入参",
    STAGE_LAYOUT: "布局",
    STAGE_PLAN: "规划",
    STAGE_WRITE: "写入",
    STAGE_VERIFY: "复核",
    STAGE_SIM: "仿真前",
}

BLOCK = "block"
WARN = "warn"
INFO = "info"

SEVERITY_LABEL = {BLOCK: "阻断", WARN: "警告", INFO: "提示"}

# ---------------------------------------------------------------------------
# 判定原语（唯一三份实现，全在 geometry_policy.py）
# ---------------------------------------------------------------------------

PRIM_NONE = "none"          # 非几何规则（入参/网络/网表/仿真前置）
PRIM_BOX = "box"            # 段 × 符号盒        geometry_policy.segment_hits_box
PRIM_PIN = "pin"            # 段 × 引脚点        geometry_policy.segment_through_point
PRIM_SEG = "seg"            # 段 × 已存导线      geometry_policy.segment_relation
PRIM_ORDER = "order"        # 折线自身形态       （拐弯/折返，逐段方向序列）
PRIM_SCALE = "scale"        # 坐标尺度           （median 符号长边）

PRIMITIVE_LABEL = {
    PRIM_NONE: "非几何",
    PRIM_BOX: "段×符号盒",
    PRIM_PIN: "段×引脚点",
    PRIM_SEG: "段×已存导线",
    PRIM_ORDER: "折线自身形态",
    PRIM_SCALE: "坐标尺度",
}

# 关联关系词汇
REL_SAME_PRIMITIVE = "同原语"     # 同一次判定产出的不同结论
REL_SAME_ROOT = "共因"            # 根因相同，修一个通常修另一个
REL_COMPENSATES = "互相弥补"      # 一个宽松时另一个兜底
REL_ESCALATES = "跨阶段升级"      # 规划放行→复核拦截
REL_EXCLUDES = "互斥"             # 两条不可能同时成立
REL_DEPENDS = "前置依赖"          # 前者失败则后者无法判定


@dataclass(frozen=True)
class Rule:
    """一条规则的全部元数据。

    ``message`` 是**报错文案前缀**（与既有 live 文案保持子串兼容 ——
    调用方在本规则文案前加 ``[R-xx 名称]`` 不会破坏既有断言）。
    """

    id: str
    name: str
    kind: str                  # 机器可读的规则名（进 violations 的 rule 字段）
    severity: str              # 见 BLOCK/WARN/INFO
    stages: tuple              # 在哪些阶段生效
    primitive: str             # 判定原语
    code: str                  # 代码位置（文件:行号）
    message: str               # 报错文案核心片段
    fix: str                   # 修复建议
    test: str                  # 固化这条规则的测试
    prompt: str                # 提示词/schema 里对应的说法（空 = 未写）
    related: tuple = ()        # ((规则号, 关联词汇), ...)

    def label(self) -> str:
        return f"[{self.id} {self.name}]"

    def format(self, detail: str) -> str:
        """统一报错格式：``[R-xx 名称] 具体细节``。"""
        return f"{self.label()} {detail}"


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

_R = Rule

RULES = {r.id: r for r in (

    # -----------------------------------------------------------------------
    # A 族：正交性
    # -----------------------------------------------------------------------
    _R("R-01", "斜段", "diagonal", BLOCK,
       (STAGE_PLAN, STAGE_WRITE, STAGE_VERIFY),
       PRIM_NONE,
       "ads_ops.py:1184 / ads_ops.py:1369 / ads_ops.py:2011 / ads_ops.py:2034 / "
       "ads_ops.py:1582",
       "不是水平或竖直",
       "把该段拆成水平+竖直两段；自动走线用 connect() 或 build_schematic "
       "layout=auto，不要手写斜折点",
       "tests/test_wire_geometry_gate.py",
       "backend/agent.py:405 「导线必须严格沿 X 或 Y」",
       ((("R-02"), REL_COMPENSATES),)),

    # -----------------------------------------------------------------------
    # B 族：符号体（段 × 盒）
    # -----------------------------------------------------------------------
    _R("R-02", "穿符号体", "symbol_body", BLOCK,
       (STAGE_PLAN, STAGE_WRITE, STAGE_VERIFY),
       PRIM_BOX,
       "geometry_policy.py:260 / ads_ops.py:1671 / "
       "ads_ops.py:1705（strict+pins 时端点命中引脚即整段豁免，复核期由 _through_sym_pin 用体内深度 ≤0.5 兜住）",
       "穿过元件",
       "改走符号外侧通道；引脚处只允许「体内深度 <= 0.5」的局部逃逸段",
       "tests/test_wire_geometry_gate.py / tests/test_three_terminal_layout.py",
       "backend/agent.py:405 「不得穿过元件符号」",
       ((("R-03"), REL_SAME_PRIMITIVE),
        (("R-04"), REL_SAME_PRIMITIVE),
        (("R-12"), REL_COMPENSATES),
        (("R-31"), REL_ESCALATES),
         (("R-01"), REL_COMPENSATES),
         (("R-16"), REL_SAME_PRIMITIVE),
         (("R-34"), REL_COMPENSATES))),

    _R("R-03", "经过其他网络引脚", "foreign_pin", BLOCK,
       (STAGE_PLAN, STAGE_WRITE),
       PRIM_PIN,
"ads_ops.py:5100",
       "经过引脚点",
       "给该网络单独留一条通道，不要从别的网络的引脚上借道",
       "tests/test_route_fallback.py",
       "backend/agent.py:434 「导线不得经过其他网络的引脚」",
       ((("R-02"), REL_SAME_PRIMITIVE),
        (("R-11"), REL_SAME_PRIMITIVE),
        (("R-12"), REL_EXCLUDES))),

    _R("R-04", "贴边骑行", "edge_ride", BLOCK,
       (STAGE_WRITE, STAGE_VERIFY),
       PRIM_BOX,
"geometry_policy.py:303（非 strict 分支，贴边滑行 > EDGE_RIDE_MAX=0.25 格判违规）",
       "贴符号轮廓滑行",
       "外移 0.25 格以上，不要贴着无关符号的边线走",
       "tests/test_wire_geometry_gate.py",
       "",
       ((("R-02"), REL_SAME_PRIMITIVE),
        (("R-02"), REL_COMPENSATES))),

    # -----------------------------------------------------------------------
    # C 族：导线间关系（段 × 段，按网络归属分级）
    # -----------------------------------------------------------------------
    _R("R-05", "异网交叉", "cross_net_cross", BLOCK,
       (STAGE_PLAN, STAGE_WRITE, STAGE_VERIFY),
       PRIM_SEG,
"ads_ops.py:1767",
       "的导线交叉",
       "改走不同通道；异网交叉在原理图上是短路",
       "tests/test_wire_geometry_gate.py",
       "backend/tools.py:205 「与已有导线交叉或重叠时拒绝建图」",
       ((("R-06"), REL_SAME_PRIMITIVE),
        (("R-07"), REL_SAME_PRIMITIVE),
        (("R-08"), REL_EXCLUDES),
        (("R-11"), REL_DEPENDS))),

    _R("R-06", "异网共线重叠", "cross_net_overlap", BLOCK,
       (STAGE_PLAN, STAGE_WRITE, STAGE_VERIFY),
       PRIM_SEG,
"ads_ops.py:1772",
       "的导线共线重叠",
       "两条不同网络的线不能共用同一段走廊",
       "tests/test_wire_geometry_gate.py",
       "backend/tools.py:205",
       ((("R-05"), REL_SAME_PRIMITIVE),
        (("R-07"), REL_SAME_PRIMITIVE),
        (("R-09"), REL_EXCLUDES),
        (("R-13"), REL_SAME_ROOT))),

    _R("R-07", "异网 T 形搭接", "cross_net_t", BLOCK,
       (STAGE_WRITE, STAGE_VERIFY),
       PRIM_SEG,
"ads_ops.py:1758",
       "的导线 T 形搭接",
       "端点必须落在引脚上；异网 T 搭等于把两个网络短在一起",
       "tests/test_wire_geometry_gate.py",
       "backend/agent.py:434",
       ((("R-05"), REL_SAME_PRIMITIVE),
        (("R-06"), REL_SAME_PRIMITIVE),
        (("R-12"), REL_COMPENSATES),
        (("R-10"), REL_EXCLUDES))),

    _R("R-08", "同网交叉", "same_net_cross", WARN,
       (STAGE_WRITE, STAGE_VERIFY),
       PRIM_SEG,
"ads_ops.py:1769",
       "同一网络的两根导线交叉",
       "同网交叉电学无害，但图上容易误读成异网交叉；尽量改成 T 分支",
       "tests/test_wire_geometry_gate.py",
       "backend/agent.py:405",
       ((("R-05"), REL_EXCLUDES),
        (("R-09"), REL_SAME_PRIMITIVE),
        (("R-10"), REL_SAME_PRIMITIVE))),

    _R("R-09", "同网共线重叠", "same_net_overlap", WARN,
       (STAGE_WRITE, STAGE_VERIFY),
       PRIM_SEG,
"ads_ops.py:1774",
       "同一网络的两根导线共线重叠",
       "同网共享走廊是合法形态；若重复画了两次由 R-13 拦",
       "tests/test_wire_geometry_gate.py",
       "",
       ((("R-06"), REL_EXCLUDES),
        (("R-08"), REL_SAME_PRIMITIVE),
        (("R-13"), REL_SAME_ROOT))),

    _R("R-10", "同网 T 形分支", "same_net_t", WARN,
       (STAGE_WRITE, STAGE_VERIFY),
       PRIM_SEG,
"ads_ops.py:1761",
       "同一网络的导线 T 形分支",
       "这是目标形态（清晰 T 分支），保持即可",
       "tests/test_wire_geometry_gate.py",
       "backend/agent.py:434",
       ((("R-07"), REL_EXCLUDES),
        (("R-08"), REL_SAME_PRIMITIVE),
        (("R-11"), REL_DEPENDS),
         (("R-14"), REL_COMPENSATES))),

    _R("R-11", "网络归属不明", "unknown_net", WARN,
       (STAGE_VERIFY,),
       PRIM_SEG,
"ads_ops.py:1776（交叉与共线重叠共用本规则，措辞按关系分）",
       "网络归属不明",
       "端点没有压在引脚上的导线无法判定归属；改成引脚到引脚连线",
       "tests/test_wire_geometry_gate.py",
       "",
       ((("R-12"), REL_SAME_ROOT),
        (("R-05"), REL_DEPENDS),
        (("R-10"), REL_DEPENDS),
         (("R-03"), REL_SAME_PRIMITIVE))),

    _R("R-12", "无引脚 T 搭", "tap_no_pin", BLOCK,
       (STAGE_VERIFY,),
       PRIM_SEG,
"ads_ops.py:1764（kind==t 且 at_pin 为假：端点落在导线中部、该点无引脚）",
       "且该点没有元件引脚",
       "把 T 结移到引脚上，或补一颗 MTEE/结点器件",
       "tests/test_wire_geometry_gate.py",
       "",
       ((("R-11"), REL_SAME_ROOT),
        (("R-03"), REL_SAME_PRIMITIVE),
        (("R-02"), REL_COMPENSATES),
        (("R-07"), REL_EXCLUDES))),

    # -----------------------------------------------------------------------
    # D 族：图面质量
    # -----------------------------------------------------------------------
    _R("R-13", "重复重合线段", "duplicate_segment", BLOCK,
       (STAGE_VERIFY,),
       PRIM_SEG,
"ads_ops.py:1805",
       "完全重合",
       "同一段走廊被画了两次；删掉重复的 shape",
       "tests/test_wire_geometry_gate.py",
       "",
       ((("R-09"), REL_SAME_ROOT),
        (("R-06"), REL_SAME_ROOT))),

    _R("R-14", "平行出线", "parallel_escape", BLOCK,
       (STAGE_VERIFY,),
       PRIM_ORDER,
"ads_ops.py:1897",
       "平行同向线段",
       "同一分流关系只画一条线，其余在引脚处 T 搭",
       "tests/test_wire_geometry_gate.py",
       "backend/agent.py:434",
       ((("R-15"), REL_SAME_PRIMITIVE),
        (("R-10"), REL_COMPENSATES))),

    _R("R-15", "同轴折返", "u_turn", WARN,
       (STAGE_VERIFY,),
       PRIM_ORDER,
"ads_ops.py:1857",
       "同轴折返",
       "折线出现方向反转，通常是不必要的 S 形绕行",
       "tests/test_cb_amp_acceptance.py",
       "",
       ((("R-14"), REL_SAME_PRIMITIVE),
        (("R-16"), REL_SAME_PRIMITIVE))),

    _R("R-16", "导线穿文字", "wire_through_text", WARN,
       (STAGE_VERIFY,),
       PRIM_BOX,
"ads_ops.py:1729",
       "的文字区",
       "挪文字或挪线；规划期由 auto_layout 7.5 步避让，复核期兜底",
       "tests/test_auto_layout.py",
       "backend/agent.py:405 「文字绝不被导线穿过」（提示词说绝对，代码只警告）",
       ((("R-02"), REL_SAME_PRIMITIVE),
        (("R-15"), REL_SAME_PRIMITIVE),
        (("R-17"), REL_SAME_ROOT))),

    # -----------------------------------------------------------------------
    # E 族：标注归属（只报告不拦截）
    # -----------------------------------------------------------------------
    _R("R-17", "标注悬空", "annotation_gap", INFO,
       (STAGE_VERIFY,),
       PRIM_NONE,
"ads_ops.py:1525",
       "的标注文字距符号",
       "文字必须贴着所属符号；auto_layout 的注释件落位规则负责规划期",
       "tests/test_auto_layout.py",
       "",
       ((("R-18"), REL_SAME_PRIMITIVE),
        (("R-16"), REL_SAME_ROOT),
         (("R-33"), REL_DEPENDS))),

    _R("R-18", "标注互压", "annotation_overlap", INFO,
       (STAGE_VERIFY,),
       PRIM_NONE,
"ads_ops.py:1533",
       "的标注文字互压",
       "拉开文字位置；标注避让在 auto_layout 里最多 4 轮",
       "tests/test_auto_layout.py",
       "",
       ((("R-17"), REL_SAME_PRIMITIVE),
         (("R-33"), REL_DEPENDS))),

    # -----------------------------------------------------------------------
    # F 族：入参与尺度
    # -----------------------------------------------------------------------
    _R("R-19", "坐标尺度异常", "layout_scale", BLOCK,
       (STAGE_ENTRY, STAGE_WRITE),
       PRIM_SCALE,
"ads_ops.py:2090（>200 × 符号中位长边；无有效 bbox 时放行）",
       "建图坐标尺度异常",
       "把坐标改成「符号宽约 1、间距 3~8」的示意图尺度，不要用物理尺寸",
       "tests/test_build_gate.py",
       "backend/agent.py:411-413；backend/tools.py:222（两处口径相反）",
       ((("R-20"), REL_DEPENDS),)),

    _R("R-20", "auto 只支持空图", "auto_needs_empty", BLOCK,
       (STAGE_ENTRY,),
       PRIM_NONE,
"ads_ops.py:5875",
       "layout=\"auto\" 只支持全新铺图",
       "增量修改请显式给每个实例 x/y，或改用 recreate 重建整图",
       "tests/test_build_gate.py",
       "backend/agent.py:372（提示词说「增量构建是安全的」，与代码冲突）",
       ((("R-19"), REL_DEPENDS),
        (("R-21"), REL_DEPENDS))),

    _R("R-21", "备份失败拒写", "backup_failed", BLOCK,
       (STAGE_ENTRY,),
       PRIM_NONE,
"ads_ops.py:869 / ads_ops.py:5851（recreate 且备份失败则拒写；note 含「没有可备份」视为可放行）",
       "备份失败",
       "清理 ads_agent_backups（保留 8 份、单份上限 200MB）或检查写权限",
       "tests/test_build_gate.py",
       "",
       ((("R-20"), REL_DEPENDS),)),

    # -----------------------------------------------------------------------
    # G 族：保存后复核（电气/参数）
    # -----------------------------------------------------------------------
    _R("R-22", "参数未落盘", "param_persist", BLOCK,
       (STAGE_VERIFY,),
       PRIM_NONE,
"ads_ops.py:2151 / ads_ops.py:2172",
       "参数未落盘",
       "参数名拼写、单位与器件支持范围三选一有错；读回去比对",
       "tests/test_build_gate.py",
       "backend/agent.py:462 RLC 取值规范",
       ((("R-23"), REL_DEPENDS),)),

    _R("R-23", "网表不等价", "netlist_equivalence", BLOCK,
       (STAGE_VERIFY,),
       PRIM_NONE,
"ads_ops.py:5656 / ads_ops.py:5964",
       "网表",
       "逐引脚比对失败说明画线没绑网络（几何重合 ≠ 电气连通），"
       "用 connect() 而不是 add_wire()",
       "tests/test_build_gate.py",
       "",
       ((("R-24"), REL_SAME_ROOT),
         (("R-22"), REL_DEPENDS))),

    _R("R-24", "连线与预期不同网", "connection_same_net", BLOCK,
       (STAGE_VERIFY,),
       PRIM_NONE,
"ads_ops.py:2194 / ads_ops.py:5986",
       "连线",
       "连接列表里两条本应独立的连线被并到同一网络（多为误绑）",
       "tests/test_build_gate.py",
       "",
       ((("R-23"), REL_SAME_ROOT),)),

    # -----------------------------------------------------------------------
    # H 族：仿真前门禁（建图时不拦，run_simulation 才拦）
    # -----------------------------------------------------------------------
    _R("R-25", "缺仿真控制器", "no_controller", BLOCK,
       (STAGE_SIM,),
       PRIM_NONE,
"ads_ops.py:517 / ads_ops.py:538",
       "没有仿真控制器",
       "加 SP1（S_Param）或 HB1 等控制器",
       "tests/test_build_gate.py",
       "backend/agent.py:523-526 SP1.SP 规则",
       ((("R-26"), REL_SAME_PRIMITIVE),
        (("R-29"), REL_DEPENDS))),

    _R("R-26", "基板引用断裂", "broken_substrate", BLOCK,
       (STAGE_SIM,),
       PRIM_NONE,
"ads_ops.py:557",
       "基板引用缺失",
       "Subst 参数指向一个不存在的 MSUB 实例",
       "tests/test_build_gate.py",
       "",
       ((("R-25"), REL_SAME_PRIMITIVE),)),

    _R("R-27", "端口自短路", "shorted_port", BLOCK,
       (STAGE_SIM,),
       PRIM_NONE,
"ads_ops.py:444 / ads_ops.py:569",
       "同一网络",
       "Term 的 1 脚和 2 脚接到同一网络，端口被短路",
       "tests/test_build_gate.py",
       "",
       ((("R-28"), REL_SAME_PRIMITIVE),)),

    _R("R-28", "端口地脚无参考", "port_ground", BLOCK,
       (STAGE_SIM,),
       PRIM_NONE,
"ads_ops.py:483 / ads_ops.py:578",
       "接地",
       "Term 地脚所在网络里没有 GND 成员，仿真会「成功」但参考开路",
       "tests/test_build_gate.py",
       "backend/agent.py:386-393 接地布局补充规则",
       ((("R-27"), REL_SAME_PRIMITIVE),
        (("R-32"), REL_DEPENDS))),

    _R("R-29", "悬空引脚", "floating_pins", WARN,
       (STAGE_SIM,),
       PRIM_NONE,
"ads_ops.py:603",
       "悬空",
       "未连接的引脚；多数无害，但关键支路悬空会导致仿真结果无意义",
       "tests/test_build_gate.py",
       "",
       ((("R-25"), REL_DEPENDS),)),
)}

# ---------------------------------------------------------------------------
# 布局规则（auto_layout_positions）—— 它们不是"违规判定"，而是"摆放承诺"。
# 单独一张表，但它们与上面的几何规则有明确的前置/补偿关系。
# ---------------------------------------------------------------------------

LAYOUT_RULES = {r.id: r for r in (
    _R("R-30", "主干选择", "main_chain", INFO,
       (STAGE_LAYOUT,), PRIM_NONE,
"ads_ops.py:2902 / ads_ops.py:2903（枚举全部简单路径后按 (长度, 端口名对, 路径) 排序取首条）",
       "主干",
       "端口改名会改变主干选择；主干不是最短路，是排序后的第一条",
       "tests/test_auto_layout.py",
       "",
       ((("R-31"), REL_DEPENDS), (("R-33"), REL_DEPENDS),
         (("R-35"), REL_DEPENDS))),

    _R("R-31", "端口朝向", "port_angle", INFO,
       (STAGE_LAYOUT,), PRIM_NONE,
"ads_ops.py:2592 / ads_ops.py:5552",
       "端口朝向",
       "TERM 输入 0/输出 180，TERMG 相反；GND 由连线方向反推（上下左右→270/90/0/180）",
       "tests/test_auto_layout.py:762-796",
       "backend/agent.py:347-354 端口首选 TermG",
       ((("R-30"), REL_DEPENDS), (("R-32"), REL_DEPENDS),
        (("R-02"), REL_COMPENSATES))),

    _R("R-32", "接地就近", "gnd_place", INFO,
       (STAGE_LAYOUT,), PRIM_NONE,
"ads_ops.py:4549（共享地需 y 差 ≤0.45 且 x 跨度 ≤6.0 才合并）",
       "接地",
       "沿引脚实际出线方向紧靠；多伙伴共享地挂下方中点 y_low-0.5",
       "tests/test_auto_layout.py",
       "backend/agent.py:386-393",
       ((("R-28"), REL_DEPENDS), (("R-31"), REL_DEPENDS))),

    _R("R-33", "注释件落位", "annot_place", INFO,
       (STAGE_LAYOUT,), PRIM_NONE,
"ads_ops.py:4607（annot_y(n) = 1.6 + 0.62*(n-1)）",
       "注释件",
       "VAR/MSUB/控制器统一排在主图上方/下方固定行",
       "tests/test_auto_layout.py",
       "",
       ((("R-30"), REL_DEPENDS), (("R-17"), REL_DEPENDS),
        (("R-18"), REL_DEPENDS))),

    _R("R-34", "行距", "row_gap", INFO,
       (STAGE_LAYOUT,), PRIM_NONE,
       "ads_ops.py:3262 / "
       "ads_ops.py:3234（max(2.6, 上一行深度+0.25)；docstring 仍写 5，已过期）",
       "行距",
       "行距按上一行实际深度动态算，不是固定 5",
       "tests/test_auto_layout.py",
       "",
       ((("R-35"), REL_SAME_PRIMITIVE), (("R-02"), REL_COMPENSATES))),

    _R("R-35", "对称一分三", "sym_split", INFO,
       (STAGE_LAYOUT,), PRIM_NONE,
"ads_ops.py:2906 / ads_ops.py:2953（k=5 直接 raise）",
       "对称分支",
       "对称拓扑拆成上下臂；臂数超过 4 直接失败回退到非对称摆放",
       "tests/test_auto_layout.py",
       "",
       ((("R-34"), REL_SAME_PRIMITIVE), (("R-30"), REL_DEPENDS))),
)}

ALL_RULES = {**RULES, **LAYOUT_RULES}

# ---------------------------------------------------------------------------
# 查询与分类
# ---------------------------------------------------------------------------


def rule(rid):
    """按编号取规则；未知编号返回 None（调用方自行决定如何降级）。"""
    return ALL_RULES.get(rid)


def by_kind(kind):
    """按机器可读名取规则（violations 里的 rule 字段用它查回来）。"""
    for r in ALL_RULES.values():
        if r.kind == kind:
            return r
    return None


def by_stage(stage):
    """该阶段生效的全部规则，按严重级排序（阻断优先）。"""
    order = {BLOCK: 0, WARN: 1, INFO: 2}
    return sorted((r for r in ALL_RULES.values() if stage in r.stages),
                  key=lambda r: (order[r.severity], r.id))


def blockers(stage):
    """该阶段会阻断的规则号集合 —— 用来回答「哪一步能拦住我」。"""
    return {r.id for r in ALL_RULES.values()
            if stage in r.stages and r.severity == BLOCK}


def related(rid):
    """一条规则的关联规则（解开成 Rule 对象）。"""
    r = rule(rid)
    if r is None:
        return []
    out = []
    for other, relation in r.related:
        o = rule(other)
        if o is not None:
            out.append((relation, o))
    return out


# 段×段的关系 → 规则号。这是三套判定共享的**唯一**分级入口。
_SEG_RULE = {
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


def classify_wire_relation(kind, net_i, net_j, at_pin=False):
    """把「两段关系 + 网络归属」判成规则号。

    这是过去散在 ``_geometry_report:1846-1882`` 的 if/elif 树的**唯一**
    替代品 —— 规划、写入、复核三处都调它，分级不再各写一套。

    ``kind``: ``"cross"`` / ``"overlap"`` / ``"t"``
    ``net_i``/``net_j``: 两段的网络标签，``None`` 表示归属不明
    ``at_pin``: 交点是否正好落在某个元件引脚上
    """
    if kind == "t" and at_pin:
        return None                     # 端点搭在引脚上 = 明确结点，不算违规
    if net_i is not None and net_j is not None:
        attribution = "same" if net_i == net_j else "different"
    else:
        attribution = "unknown"
    return _SEG_RULE.get((kind, attribution))


def severity_of(kind_or_rid, stage=None):
    """规则严重级。给了 stage 时返回该阶段的严重级（None = 该阶段不生效）。"""
    r = rule(kind_or_rid) or by_kind(kind_or_rid)
    if r is None:
        return None
    if stage is not None and stage not in r.stages:
        return None
    return r.severity


def stage_hint(stage):
    """该阶段全部阻断规则号的紧凑列表（按编号数值排序）。"""
    ids = sorted(blockers(stage), key=lambda x: int(x.split("-")[1]))
    return "/".join(ids)


def format_stage_violation(stage, detail):
    """聚合型报错的统一文案：``[阶段: 规则 R-01/R-02/...] 具体细节``。

    ``_route_clear``/``_route_around`` 这类"一处判定里同时查了多条规则"
    的报错无法归到单条规则号上 —— 用该阶段的阻断规则清单代替，读者仍能
    顺着编号回查到本表，知道自己撞的是哪一族规则。
    """
    ids = stage_hint(stage)
    if not ids:
        return detail
    return f"[{STAGE_LABEL.get(stage, stage)}: 规则 {ids}] {detail}"


def format_violation(kind_or_rid, detail):
    """把 ``(规则号或 kind, detail)`` 渲染成统一文案；未知则原样返回。

    统一文案形如 ``[R-02 穿符号体] 具体细节`` —— 任何一条报错都能凭前缀
    回查到本表，从而看到它的阶段、严重级、关联规则与修复建议。
    """
    r = rule(kind_or_rid) or by_kind(kind_or_rid)
    if r is None:
        return detail
    return r.format(detail)


# ---------------------------------------------------------------------------
# 自检：规则表的关联必须闭合
# ---------------------------------------------------------------------------


def audit_relations():
    """检查规则表自身的一致性。返回问题列表（空 = 通过）。

    检查项：
      * ``related`` 指向的规则号必须存在；
      * 不许自己关联自己；
      * 关联必须**双向**登记（A 记着 B，B 也要记着 A）；
      * 不许有**孤立规则**（``related`` 为空）；
      * 严重级、原语、阶段必须是已知枚举值。

    这个函数存在的意义：规则表的价值在于**关联**，关联一旦指向不存在的
    编号，表就退化回一堆孤立条目 —— 必须有机器检查守住。
    """
    issues = []
    for rid, r in ALL_RULES.items():
        if r.id != rid:
            issues.append(f"{rid}: 表键与 id 不一致（{r.id}）")
        if r.severity not in SEVERITY_LABEL:
            issues.append(f"{rid}: 未知严重级 {r.severity}")
        if r.primitive not in PRIMITIVE_LABEL:
            issues.append(f"{rid}: 未知判定原语 {r.primitive}")
        for st in r.stages:
            if st not in STAGE_LABEL:
                issues.append(f"{rid}: 未知阶段 {st}")
        if not r.related:
            issues.append(f"{rid}: 没有任何关联（孤立条目）")
        for other, relation in r.related:
            if other == rid:
                issues.append(f"{rid}: 关联到自己")
            elif other not in ALL_RULES:
                issues.append(f"{rid}: 关联到不存在的规则 {other}")
            elif rid not in [o for o, _ in ALL_RULES[other].related]:
                issues.append(f"{rid} -> {other}: 目标没有反向登记")
    return issues


def graph():
    """返回 ``{rid: [(relation, other_rid, other_name), ...]}``，供文档生成用。"""
    out = {}
    for rid, r in ALL_RULES.items():
        rows = []
        for other, relation in r.related:
            target = ALL_RULES.get(other)
            rows.append((relation, other, target.name if target else "?"))
        out[rid] = rows
    return out
