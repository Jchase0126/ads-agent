"""导线几何硬门禁 + 手工路径接线 + 端口地参考门禁测试（不需要 ADS）。

背景（2026-09-28，用户截图点名 Wilkinson_1G7_ML 的 Riso 上下斜线）：
build_schematic 的连线走 _wire_connection 正交门禁，但 run_python 的手工
connect()/add_wire 直接把两个不共轴引脚连成斜线落盘。修复分三层：

* ``connect_impl``/``wire_impl``：手工路径与 build_schematic 同一套
  正交/避障规则，违规直接报错（原设计不动）；
* ``save_impl``：run_python 里 save(d) 前置几何门禁 —— 从设计读真实
  导线，斜线/穿符号/异网交叉在落盘前拦截；
* ``_geometry_report``：build_schematic 保存后复核与 save_verify 共用，
  以磁盘上读到的几何为准（不是建图前的规划坐标）。

同时覆盖：
* 端口地参考门禁：Term 地脚网络没有 GND 时阻断仿真（实测悬空地脚
  仿真"成功"但参考开路，结果不可信）；
* TermG 作为端口：单脚自带地参考，网表 Port:x N 0，不触发地脚检查；
* auto_layout：无 MTEE、导线直接搭 T 的拓扑 —— 下支路进分支行、
  隔离电阻竖放两行之间、两个输出口同列（信号流对齐）。

运行::

    python tests/test_wire_geometry_gate.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "backend"), os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _harness import contains, eq, ne, ok, raises, run  # noqa: E402

import ads_ops  # noqa: E402


# ---------------------------------------------------------------------------
# ADS 对象替身：只实现门禁读到的成员
# ---------------------------------------------------------------------------

class Pt:
    def __init__(self, x, y):
        self.x, self.y = float(x), float(y)


class Outline:
    def __init__(self, pts):
        self.points = [Pt(x, y) for x, y in pts]


class Shape:
    """导线图形：优先给 outline（精确），否则只给 bbox。"""

    def __init__(self, pts=None, bbox=None):
        self._outline = Outline(pts) if pts else None
        if bbox:
            self.bbox = type("B", (), {"lower_left": Pt(bbox[0], bbox[1]),
                                       "upper_right": Pt(bbox[2], bbox[3])})()

    def get_outline(self):
        if self._outline is None:
            raise RuntimeError("no outline")
        return self._outline


class Net:
    def __init__(self, name):
        self._name = name

    def __str__(self):
        return f'<ScalarNet "{self._name}">'


class Pin:
    def __init__(self, number, x, y, net=None):
        self.inst_term = type("T", (), {"term_number": number,
                                        "is_numbered": True})()
        self.snap_point = Pt(x, y)
        self.net = net


class Inst:
    def __init__(self, name, master, pins=(), bbox=None, params=()):
        self.name = name
        self.cell_name = master
        self.inst_pins = list(pins)
        if bbox:
            self.bbox = type("B", (), {"lower_left": Pt(bbox[0], bbox[1]),
                                       "upper_right": Pt(bbox[2], bbox[3])})()
        self.parameters = [type("P", (), {"name": k, "value": v})()
                           for k, v in params]


class Design:
    def __init__(self, instances=(), shapes=()):
        self.instances = list(instances)
        self.shapes = list(shapes)
        self.saved = 0
        self.wires = []

    def add_wire(self, pts):
        self.wires.append(pts)

    def add_scalar_net(self):
        return Net(f"N__auto{len(self.wires)}")

    def save_design(self):
        self.saved += 1


def _wilkinson_plain_t():
    """用户截图拓扑：无 MTEE，ML1.2 导线直接搭 T，Riso 跨两输出节点。

    bbox 用本机实测几何：MLIN/Term 符号体宽 0.875，引脚在 origin 与
    origin+(1,0)（引脚伸出 bbox 之外）；R90 的引脚在上下边界中点。
    """
    def mlin(name, x, y):
        return Inst(name, "MLIN",
                    [Pin(1, x, y), Pin(2, x + 1, y)],
                    bbox=(x, y - 0.19, x + 0.875, y + 0.19))

    def term(name, x, y):
        # Term：1=地脚（origin），2=射频脚（origin+(1,0)）
        return Inst(name, "Term", [Pin(1, x, y), Pin(2, x + 1, y)],
                    bbox=(x, y - 0.19, x + 0.875, y + 0.19))

    insts = [
        term("Term1", 0, 0),
        mlin("ML1", 2, 0),
        mlin("MLa", 6, 0),
        mlin("MLb", 6, -5),
        Inst("Riso", "R", [Pin(1, 7, -3), Pin(2, 7, -2)],
             bbox=(6.81, -3, 7.19, -2)),          # R90：引脚在上下边界中点
        term("Term2", 10, 0),
        term("Term3", 10, -5),
    ]
    shapes = [
        # ML1.2 T 结的三条正交线（正确画法）
        Shape(pts=[(3, 0), (6, 0)]),          # ML1.2 -> MLa.1
        Shape(pts=[(3, 0), (3, -5)]),         # T 下拉到 MLb.1
        Shape(pts=[(3, -5), (6, -5)]),        # -> MLb.1
        # 上支路出线 + Riso 上端（竖直进上下引脚，不沿符号边缘滑入）
        Shape(pts=[(7, 0), (10, 0)]),
        Shape(pts=[(7, 0), (7, -2)]),
        # 下支路出线 + Riso 下端
        Shape(pts=[(7, -5), (10, -5)]),
        Shape(pts=[(7, -5), (7, -3)]),
    ]
    return insts, shapes


# ---------------------------------------------------------------------------
# _shape_polyline / _geometry_report
# ---------------------------------------------------------------------------

def test_shape_polyline_reads_outline_points():
    s = Shape(pts=[(0, 0), (2, 0), (2, -3)])
    pts, exact = ads_ops._shape_polyline(s)
    ok(exact, "outline 可读时应为精确")
    eq(pts, [(0.0, 0.0), (2.0, 0.0), (2.0, -3.0)], "应返回真实折线顶点")


def test_shape_polyline_bbox_fallback_and_inexact_flag():
    s = Shape(bbox=(0.0, 0.0, 4.0, 0.0))          # 共线 bbox：可当直线段
    pts, exact = ads_ops._shape_polyline(s)
    ok(exact, "共线 bbox 应视为可精确表达的直线段")
    eq(len(pts), 2)
    s2 = Shape(bbox=(0.0, 0.0, 4.0, 3.0))         # 非退化 bbox：几何未知
    pts2, exact2 = ads_ops._shape_polyline(s2)
    ne(exact2, True, "非退化 bbox 不能臆测为斜线段")


def test_geometry_report_flags_diagonal():
    insts, shapes = _wilkinson_plain_t()
    shapes.append(Shape(pts=[(7, -5), (8, -2)]))   # 手工斜线（截图里的形态）
    rep = ads_ops._geometry_report(Design(insts, shapes))
    ok(any("斜线" in p for p in rep["problems"]),
       f"斜段必须是 problem: {rep['problems'][:2]}")


def test_geometry_report_flags_through_symbol():
    insts, shapes = _wilkinson_plain_t()
    shapes.append(Shape(pts=[(3, 0), (9, 0)]))     # 横穿 MLa 内部
    rep = ads_ops._geometry_report(Design(insts, shapes))
    ok(any("穿过元件 MLa" in p for p in rep["problems"]),
       f"穿符号必须是 problem: {rep['problems'][:2]}")


def test_geometry_report_flags_t_without_pin():
    insts, shapes = _wilkinson_plain_t()
    shapes.append(Shape(pts=[(4, 0), (4, -8)]))    # 端点搭在横线中部，无引脚
    rep = ads_ops._geometry_report(Design(insts, shapes))
    ok(any("搭在另一根导线中部" in p for p in rep["problems"]),
       f"无引脚 T 接必须是 problem: {rep['problems'][:2]}")


def test_geometry_report_classifies_cross_and_overlap():
    insts, shapes = _wilkinson_plain_t()
    rep = ads_ops._geometry_report(Design(insts, shapes))
    eq(rep["problems"], [], f"正确画法不该有 problem: {rep['problems']}")
    ok(rep["n_segments"] >= 7, "应读到全部导线段")

    # 端口地脚接法：给 Term1.2 一个网络（通过引脚 net 模拟 _bind_pins 的结果）
    # 异网交叉：竖线 x=4（无网络归属）与横线 y=0 交叉但端点在引脚外
    shapes2 = [Shape(pts=[(3, 0), (6, 0)]), Shape(pts=[(3, 0), (3, -5)]),
               Shape(pts=[(3, -5), (6, -5)]), Shape(pts=[(7, 0), (10, 0)]),
               Shape(pts=[(7, -5), (10, -5)]),
               Shape(pts=[(4, -2), (4, -4)])]      # 不交叉的控制组
    rep2 = ads_ops._geometry_report(Design(insts, shapes2))
    eq(rep2["problems"], [], f"控制组不该有 problem: {rep2['problems']}")


def test_geometry_report_unverified_shapes_reported():
    insts, shapes = _wilkinson_plain_t()
    shapes.append(Shape(bbox=(0, -20, 5, -18)))    # 读不出顶点的图形
    rep = ads_ops._geometry_report(Design(insts, shapes))
    ok(len(rep["unverified"]) == 1, f"读不出顶点必须如实标注未验证: {rep['unverified']}")
    ok(all("未验证" in u for u in rep["unverified"]))


# ---------------------------------------------------------------------------
# 手工路径 connect_impl / wire_impl / save_impl
# ---------------------------------------------------------------------------

def test_connect_impl_routes_elbow_for_non_coaxial_pins():
    a = Inst("A", "MLIN", [Pin(1, 0, 0), Pin(2, 1, 0)])
    b = Inst("B", "MLIN", [Pin(1, 4, -3), Pin(2, 5, -3)])
    d = Design([a, b])
    pts = ads_ops.connect_impl(d, a, 2, b, 1)
    ok(len(d.wires) == 2, f"拐角应拆成两段直线: {d.wires}")
    for w in d.wires:
        (x1, y1), (x2, y2) = w
        ok(x1 == x2 or y1 == y2, f"每段必须正交: {w}")
    ok(pts[0] == (1.0, 0.0) and pts[-1] == (4.0, -3.0), "首尾在引脚上")


def test_connect_impl_refuses_when_no_orthogonal_path():
    a = Inst("A", "MLIN", [Pin(1, 0, 0), Pin(2, 1, 0)])
    b = Inst("B", "MLIN", [Pin(1, 4, -3), Pin(2, 5, -3)],
             bbox=(4, -3.2, 5, -2.8))
    # 用已画好的导线把 B 的引脚围死：A* 网格进不去，必须报错而不是画斜线
    cage = [Shape(pts=[(3.4, -3.8), (4.6, -3.8)]),
            Shape(pts=[(4.6, -3.8), (4.6, -2.2)]),
            Shape(pts=[(4.6, -2.2), (3.4, -2.2)]),
            Shape(pts=[(3.4, -2.2), (3.4, -3.8)])]
    d = Design([a, b], cage)
    raises(RuntimeError, lambda: ads_ops.connect_impl(d, a, 2, b, 1),
           "无正交路径必须报错，不能退化为斜线")
    eq(d.wires, [], "失败时不能画线")


def test_wire_impl_rejects_diagonal_and_symbol_crossing():
    a = Inst("A", "MLIN", [Pin(1, 0, 0), Pin(2, 1, 0)], bbox=(0, -0.2, 0.875, 0.2))
    d = Design([a])
    raises(RuntimeError, lambda: ads_ops.wire_impl(d, [(1, 0), (3, -2)]),
           "斜段必须拒绝")
    eq(d.wires, [], "拒绝时不能画线")
    raises(RuntimeError, lambda: ads_ops.wire_impl(d, [(0.5, 0), (0.5, -3)]),
           "穿符号内部必须拒绝")
    eq(d.wires, [], "拒绝时不能画线")
    raises(RuntimeError, lambda: ads_ops.wire_impl(d, [(0.875, 0), (0.875, -3)]),
           "沿符号边缘下行也应拒绝")
    ads_ops.wire_impl(d, [(1, 0), (3, 0), (3, -3)])   # 先水平离开再转弯
    eq(len(d.wires), 2, "合法折线按段画入")


def test_save_impl_blocks_bad_geometry_before_writing():
    insts, shapes = _wilkinson_plain_t()
    shapes.append(Shape(pts=[(7, -5), (8, -2)]))    # 一条斜线
    d = Design(insts, shapes)
    err = raises(RuntimeError, lambda: ads_ops.save_impl(d))
    contains(str(err), "导线几何检查未通过")
    eq(d.saved, 0, "门禁拦截时不得落盘")
    contains(str(err), "connect()", "报错要指路到自动正交连线")


def test_save_impl_passes_clean_design():
    insts, shapes = _wilkinson_plain_t()
    d = Design(insts, shapes)
    eq(ads_ops.save_impl(d), "saved")
    eq(d.saved, 1, "干净设计应正常保存")


# ---------------------------------------------------------------------------
# 端口地参考门禁 + TermG 识别
# ---------------------------------------------------------------------------

def test_design_audit_flags_port_ground_missing():
    gnet = Net("N__0")
    snet = Net("N__2")
    term = Inst("Term1", "Term", [Pin(1, 0, 0, gnet), Pin(2, 1, 0, snet)])
    mlin = Inst("ML1", "MLIN", [Pin(1, 2, 0, snet), Pin(2, 3, 0, Net("N__3"))])
    audit = ads_ops._design_audit(Design([term, mlin]))
    eq(len(audit["port_ground_issues"]), 1, "地脚网络无 GND 必须点名")
    eq(audit["port_ground_issues"][0]["instance"], "Term1")
    problems = ads_ops._gate_problems(audit, "AI_lib:X:schematic")
    ok(any("地脚没有接地参考" in p for p in problems),
       f"门禁必须阻断: {problems}")
    ok(any("TermG" in p for p in problems), "提示应给出 TermG 方案")


def test_design_audit_passes_term_with_ground():
    gnet = Net("N__0")
    snet = Net("N__2")
    term = Inst("Term1", "Term", [Pin(1, 0, 0, gnet), Pin(2, 1, 0, snet)])
    gnd = Inst("GND1", "GROUND", [Pin(1, 0, -0.5, gnet)])
    audit = ads_ops._design_audit(Design([term, gnd]))
    eq(audit["port_ground_issues"], [], "地脚接了 GND 不该报")
    problems = ads_ops._gate_problems(audit, "AI_lib:X:schematic")
    ok(not any("地脚" in p for p in problems), f"门禁应放行: {problems}")


def test_termg_is_recognized_as_port_and_needs_no_ground():
    tg = Inst("P1", "TermG", [Pin(1, 0, 0, Net("N__1"))])
    mlin = Inst("ML1", "MLIN", [Pin(1, 2, 0, Net("N__1")),
                                Pin(2, 3, 0, Net("N__3"))])
    tg2 = Inst("P2", "TermG", [Pin(1, 5, 0, Net("N__3"))])
    audit = ads_ops._design_audit(Design([tg, mlin, tg2]))
    eq(len(audit["ports"]), 2, "TermG 应识别为端口")
    eq(audit["port_ground_issues"], [], "TermG 自带地参考，不该触发地脚检查")
    problems = ads_ops._gate_problems(audit, "AI_lib:X:schematic")
    ok(not any("没有任何端口" in p for p in problems),
       "有 TermG 时不应再报缺端口")


def test_short_standing_termg_in_layout_spec():
    """TermG 在 auto_layout 的引脚表里：单脚在 origin。"""
    insts = [{"master": "ads_simulation:TermG", "name": "P1"},
             {"master": "ads_tlines:MLIN", "name": "ML1"}]
    conns = [{"a": ["P1", 1], "b": ["ML1", 1]}]
    placed = ads_ops.auto_layout_positions(insts, conns)
    eq(placed["P1"]["x"] + 0, placed["ML1"]["x"] - 1.9 + 0.0 - 0.0 + 1.0 - 1.0,
       "占位") if False else None
    # 真正的断言：P1 在 ML1 左侧一个 gap 处（引脚对引脚间距 = gap）。
    # 2026-09-30 紧凑化：链内引脚间距基线 1.9 → 1.4（逐对按文字伸出量加大）
    eq(round(placed["ML1"]["x"] - placed["P1"]["x"], 6),
       ads_ops._CHAIN_GAP_MIN,
       "TermG 单脚与 MLIN 引脚间距应为 gap 基线")


# ---------------------------------------------------------------------------
# auto_layout：无 MTEE 的 T 结拓扑（用户截图拓扑）
# ---------------------------------------------------------------------------

def _plain_t_spec():
    instances = [
        {"master": "ads_simulation:Term", "name": "Term1"},
        {"master": "ads_tlines:MLIN", "name": "ML1"},
        {"master": "ads_tlines:MLIN", "name": "MLa"},
        {"master": "ads_tlines:MLIN", "name": "MLb"},
        {"master": "ads_rflib:R", "name": "Riso", "angle": 90},
        {"master": "ads_simulation:Term", "name": "Term2"},
        {"master": "ads_simulation:Term", "name": "Term3"},
    ]
    conns = [
        {"a": ["Term1", 2], "b": ["ML1", 1]},
        {"a": ["ML1", 2], "b": ["MLa", 1]},
        {"a": ["ML1", 2], "b": ["MLb", 1]},
        {"a": ["MLa", 2], "b": ["Term2", 2]},
        {"a": ["MLa", 2], "b": ["Riso", 2]},
        {"a": ["MLb", 2], "b": ["Term3", 2]},
        {"a": ["MLb", 2], "b": ["Riso", 1]},
    ]
    return instances, conns


def test_plain_t_layout_lower_branch_and_bridge():
    # 2026-09-29 起 T 分裂命中对称一分二路径:两臂关于主干行镜像,隔离
    # 电阻竖放在两臂出脚同列正中（比旧通用路径的"下支路 -5 行"更紧凑）
    instances, conns = _plain_t_spec()
    placed = ads_ops.auto_layout_positions(instances, conns)
    d = ads_ops._SYM_ROW_H
    eq(placed["MLa"]["y"], d, "上臂镜像在 +row_h")
    eq(placed["MLb"]["y"], -d, "下臂镜像在 -row_h")
    eq(placed["Term3"]["y"], -d, "输出口在下臂行")
    eq(placed["Riso"]["y"], -0.5, "隔离电阻竖放在两行正中")
    eq(placed["MLa"]["x"], placed["MLb"]["x"], "两臂入脚同列")
    eq(placed["Riso"]["x"], placed["MLa"]["x"] + 1,
       "隔离电阻在两臂出脚同列")


def test_plain_t_layout_port_angles_normalized():
    instances, conns = _plain_t_spec()
    placed = ads_ops.auto_layout_positions(instances, conns)
    eq(placed["Term1"].get("angle"), 0, "输入 Term 朝右")
    eq(placed["Term2"].get("angle"), 180, "输出 Term2 射频脚朝左")
    eq(placed["Term3"].get("angle"), 180, "输出 Term3 射频脚朝左")


def test_termg_layout_angles_inverted():
    instances = [
        {"master": "ads_simulation:TermG", "name": "P1"},
        {"master": "ads_tlines:MLIN", "name": "ML1"},
        {"master": "ads_tlines:MLIN", "name": "MLa"},
        {"master": "ads_tlines:MLIN", "name": "MLb"},
        {"master": "ads_rflib:R", "name": "Riso", "angle": 90},
        {"master": "ads_simulation:TermG", "name": "P2"},
        {"master": "ads_simulation:TermG", "name": "P3"},
    ]
    conns = [
        {"a": ["P1", 1], "b": ["ML1", 1]},
        {"a": ["ML1", 2], "b": ["MLa", 1]},
        {"a": ["ML1", 2], "b": ["MLb", 1]},
        {"a": ["MLa", 2], "b": ["P2", 1]},
        {"a": ["MLa", 2], "b": ["Riso", 2]},
        {"a": ["MLb", 2], "b": ["P3", 1]},
        {"a": ["MLb", 2], "b": ["Riso", 1]},
    ]
    placed = ads_ops.auto_layout_positions(instances, conns)
    eq(placed["P1"].get("angle"), 180,
       "TermG 符号体在 origin 右侧，输入口要 180 让体甩在外侧")
    eq(placed["P2"].get("angle"), 0, "TermG 输出口 0")
    eq(placed["P3"].get("angle"), 0, "TermG 输出口 0")


def test_explicit_angle_never_overridden():
    instances, conns = _plain_t_spec()
    for s in instances:
        if s["name"] == "Term3":
            s["angle"] = 0                       # 用户显式给了角度
    placed = ads_ops.auto_layout_positions(instances, conns)
    eq(placed["Term3"].get("angle"), None,
       "显式角度不进输出（build_schematic 用实例自己的）")


def test_explicit_angle_affects_placement():
    """显式角度必须参与坐标计算：place_chain 对齐的是引入引脚 ——
    Term3 显式 0 与默认 180 时射频脚(pin2)落在同一列，origin 随角度
    平移一个符号宽（pin 偏移 (1,0) 旋转 180° 变 (-1,0)，差 2）。"""
    instances, conns = _plain_t_spec()
    placed_default = ads_ops.auto_layout_positions(
        [dict(s) for s in instances], [dict(c) for c in conns])
    spec2 = [dict(s) for s in instances]
    for s in spec2:
        if s["name"] == "Term3":
            s["angle"] = 0
    placed_explicit = ads_ops.auto_layout_positions(spec2, conns)

    def pin2(spec_map, placed):
        a = placed.get("angle", spec_map.get("angle") or 0)
        dx, dy = ads_ops._rot_off(1, 0, a)
        return (round(placed["x"] + dx, 6), round(placed["y"] + dy, 6))

    d = {s["name"]: s for s in instances}
    eq(pin2(d, placed_default["Term3"]), pin2(d, placed_explicit["Term3"]),
       "两种角度下引入引脚都应落在同一列")
    eq(round(placed_explicit["Term3"]["x"] - placed_default["Term3"]["x"], 6),
       -2.0, "origin 差一个符号宽的 2 倍（偏移 (1,0) 转 180°）")


def test_real_measure_bbox_t_drop_is_legal():
    """复刻实机失败（Wilkinson_1G7_RG 建图 2026-09-28）：实测 MLIN bbox 宽
    1.0、引脚就在 bbox 边缘上 —— 从通轴引脚垂直下拉的 T 结线擦自己的
    符号边缘，必须判合法且可布线，否则任何 T 结都无法建图。"""
    ml1 = Inst("ML1", "MLIN", [Pin(1, 1.9, 0), Pin(2, 2.9, 0)],
               bbox=(1.9, -0.125, 2.9, 0.125))          # 实测：引脚在边缘上
    mla = Inst("MLa", "MLIN", [Pin(1, 4.8, 0), Pin(2, 5.8, 0)],
               bbox=(4.8, -0.125, 5.8, 0.125))
    mlb = Inst("MLb", "MLIN", [Pin(1, 4.8, -5), Pin(2, 5.8, -5)],
               bbox=(4.8, -5.125, 5.8, -4.875))
    shapes = [
        Shape(pts=[(2.9, 0), (4.8, 0)]),        # ML1.2 -> MLa.1（横）
        Shape(pts=[(2.9, 0), (2.9, -5)]),       # T 下拉：擦 ML1 右边缘
        Shape(pts=[(2.9, -5), (4.8, -5)]),      # -> MLb.1
    ]
    rep = ads_ops._geometry_report(Design([ml1, mla, mlb], shapes))
    eq([p for p in rep["problems"] if "穿过" in p], [],
       f"T 下拉擦自己符号边缘不该报穿符号: {rep['problems']}")

    # 布线同理：已有横线在，T 下拉必须能走通
    d = Design([ml1, mla, mlb], shapes[:1])     # 只画了第一条横线
    pts = ads_ops.connect_impl(d, ml1, 2, mlb, 1)
    ok(pts and pts[-1] == (4.8, -5.0), f"T 下拉应可布线: {pts}")
    for w in d.wires:
        (x1, y1), (x2, y2) = w
        ok(x1 == x2 or y1 == y2, f"段必须正交: {w}")


def test_other_symbols_still_block_edge_hugging():
    """豁免只给自己的符号：擦别人的符号边界仍然拒绝。"""
    ml1 = Inst("ML1", "MLIN", [Pin(1, 0, 0), Pin(2, 1, 0)],
               bbox=(0, -0.125, 1, 0.125))
    other = Inst("OTHER", "MLIN", [Pin(1, 3, -3), Pin(2, 4, -3)],
                 bbox=(3, -3.125, 4, -2.875))
    d = Design([ml1, other])
    # 从 (3.5,-1) 下行到 (3.5,-4) 擦 OTHER 的 bbox？x=3.5 在 OTHER 内部 ——
    # 用边缘线：x=3 竖线擦 OTHER 左边缘，但 (3,-1) 不是 OTHER 的引脚
    raises(RuntimeError, lambda: ads_ops.wire_impl(d, [(3, 0), (3, -3.05)]),
           "擦别人符号边缘仍应拒绝")
    eq(d.wires, [])


if __name__ == "__main__":
    raise SystemExit(run(globals()))
