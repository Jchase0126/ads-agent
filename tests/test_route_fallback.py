# -*- coding: utf-8 -*-
"""折点被拒→延后重寻 + 细化网格 的回归（2026-10-12 FM_MURATA 实测停摆）。

背景（实机日志 2026-10-09 17:21 与离线复现）：含村田模型库元件的 FM 放大器
整图构建在最后一刻失败 —— R1->CB 的计划折点用保守符号盒算出，被在线真
bbox 复核拒绝（"指定折点使导线穿过元件或与已有导线交叉/重叠"）；当时立即
改走在线寻路，兜底绕行线横封 Q1.c 周围走廊，随后 Q1->LC 的直连
(11.4,1.0)->(11.95,1.75) 找不到任何正交路径，整图构建失败、设计没有落盘。

修复（addon/ads_agent/ads_ops.py）：
  * _wire_all_connections：折点被拒的连接延后到其余连接画完再重寻；
  * _route_around：粗网格无解时用相邻网格线中点细化重试；
  * plan_wire_routes._penalty：补上注释承诺的"跨网规划线交叉"罚分项。

断言里的几何（符号盒/引脚偏移/实例坐标）是 2026-10-12 在线实测值：
FC_SC 建档 + MURATA_TEST 符号实测（见 logs/probe_*_geometry.py）。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "backend"), os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _harness import contains, eq, ok, raises, run  # noqa: E402

import ads_ops  # noqa: E402


# ---------------------------------------------------------------------------
# 与 FM_MURATA 实机调用 1:1 的输入（2026-10-09 17:21 日志参数）
# ---------------------------------------------------------------------------

PN_10N = "GCM155L81E103KA01"
INSTANCES = [
    {"master": "Infineon_RF:Infineon_Include_RF", "name": "INC1"},
    {"master": "muRataLibWeb_L:muRataLibWeb_L_Include", "name": "INCL"},
    {"master": "muRataLibWeb_C:muRataLibWeb_C_Include", "name": "INCC"},
    {"master": "Infineon_RF:BFR106", "name": "Q1"},
    {"master": "ads_sources:V_DC", "name": "VCC", "params": {"Vdc": "5 V"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "R1_b"}},
    {"master": "ads_rflib:R", "name": "R2", "params": {"R": "R2_b"}},
    {"master": "ads_rflib:R", "name": "RE", "params": {"R": "RE_b"}},
    {"master": "ads_rflib:R", "name": "RD", "params": {"R": "RD_b"}},
    {"master": "muRataLibWeb_C:GCM15", "name": "CB", "params": {"PartNumber": PN_10N}},
    {"master": "muRataLibWeb_C:GCM15", "name": "CDC", "params": {"PartNumber": PN_10N}},
    {"master": "muRataLibWeb_C:GCM15", "name": "CDCOUT", "params": {"PartNumber": PN_10N}},
    {"master": "muRataLibWeb_C:GJM15_T", "name": "C1", "params": {"PartNumber": "GJM1555C1H560WB01"}},
    {"master": "muRataLibWeb_C:GJM15", "name": "C2", "params": {"PartNumber": "GJM1555C1H241WB01"}},
    {"master": "muRataLibWeb_C:GJM15", "name": "C3", "params": {"PartNumber": "GJM1555C1H3R3WB01"}},
    {"master": "muRataLibWeb_C:GJM15", "name": "C4", "params": {"PartNumber": "GJM1555C1H150WB01"}},
    {"master": "muRataLibWeb_L:LQG15HH_02", "name": "L1", "params": {"PartNumber": "LQG15HH30NG02"}},
    {"master": "muRataLibWeb_L:LQG15HH_02", "name": "L2", "params": {"PartNumber": "LQG15HH6N8G02"}},
    {"master": "muRataLibWeb_L:LQG15HH_02", "name": "L3", "params": {"PartNumber": "LQG15HH471NG02"}},
    {"master": "muRataLibWeb_L:LQG15HH_02", "name": "L4", "params": {"PartNumber": "LQG15HH131NG02"}},
    {"master": "muRataLibWeb_L:LQG15HH_02", "name": "LC", "params": {"PartNumber": "LQG15HN4K7M02"}},
    {"master": "ads_simulation:TermG", "name": "PORT1", "params": {"Num": "1", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "PORT2", "params": {"Num": "2", "Z": "50 Ohm"}},
] + [{"master": "ads_rflib:GROUND", "name": f"G{i}"} for i in range(1, 10)] + [
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "70 MHz", "Stop": "130 MHz", "Step": "0.5 MHz"}},
]

VAR_SPEC = {"name": "VAR1", "values": {
    "R1_b": "3.0 kOhm", "R2_b": "1.9 kOhm", "RD_b": "1.0 kOhm", "RE_b": "100 Ohm",
}}

C = lambda a, b: {"a": [a[0], str(a[1])], "b": [b[0], str(b[1])]}
CONNECTIONS = [
    C(("PORT1", 1), ("CDC", 1)), C(("CDC", 2), ("C1", 1)),
    C(("C1", 1), ("L1", 1)), C(("C1", 2), ("G1", 1)),
    C(("L1", 2), ("C2", 1)), C(("C2", 1), ("L2", 1)),
    C(("C2", 2), ("G2", 1)), C(("L2", 2), ("Q1", 3)),
    C(("Q1", 3), ("RE", 1)), C(("RE", 2), ("G3", 1)),
    C(("Q1", 2), ("R1", 1)), C(("R1", 1), ("R2", 1)),
    C(("R1", 1), ("CB", 1)), C(("R2", 2), ("G4", 1)),
    C(("CB", 2), ("G5", 1)), C(("R1", 2), ("VCC", 1)),
    C(("Q1", 1), ("LC", 2)), C(("Q1", 1), ("CDCOUT", 1)),
    C(("LC", 1), ("VCC", 1)), C(("VCC", 2), ("G6", 1)),
    C(("CDCOUT", 2), ("C3", 1)), C(("CDCOUT", 2), ("L3", 1)),
    C(("CDCOUT", 2), ("RD", 1)), C(("C3", 2), ("G7", 1)),
    C(("RD", 2), ("G8", 1)), C(("L3", 2), ("C4", 1)),
    C(("L3", 2), ("L4", 1)), C(("C4", 2), ("G9", 1)),
    C(("L4", 2), ("PORT2", 1)),
]

#: master 短名 -> R0 基准符号盒（2026-10-12 在线实测；GROUND 由 FM_SC 的
#: R180/R270 盒反解、(0,-0.125,0.25,0.125) 两处互证）
R0_BOX = {
    "BFR106": (0.0, -0.5, 0.5, 0.5),
    "R": (0.0, -0.069, 1.0, 0.069),
    "C": (0.0, -0.125, 1.0, 0.125),
    "L": (0.0, -0.037, 1.0, 0.087),
    "GJM15_T": (0.0, -0.125, 1.0, 0.294),   # 村田高顶盖电容（顶板高出引脚 0.294）
    "V_DC": (0.0, -0.175, 1.0, 0.125),
    "TERMG": (0.0, -0.188, 0.875, 0.188),
    "GROUND": (0.0, -0.125, 0.25, 0.125),
    "LQG15HH_02": (0.0, -0.037, 1.0, 0.087),
    "GJM15": (0.0, -0.125, 1.0, 0.125),
    "GCM15": (0.0, -0.125, 1.0, 0.125),
    "MURATALIBWEB_L_INCLUDE": (0.0, -0.469, 2.663, 0.0),
    "MURATALIBWEB_C_INCLUDE": (0.0, -0.469, 2.662, 0.0),
    "INFINEON_INCLUDE_RF": (0.0, -0.469, 2.662, 0.0),
    "S_PARAM": (0.0, -0.469, 2.575, 0.0),
}


def _rot_box(rect, angle):
    a, b, c, d = rect
    ang = int(angle or 0) % 360
    if ang == 0:
        return (a, b, c, d)
    if ang == 90:
        return (-d, a, -b, c)
    if ang == 180:
        return (-c, -d, -a, -b)
    return (b, -c, d, -a)  # 270


class _Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _FakePin:
    def __init__(self, label, x, y):
        self.master_pin = str(label)
        self.inst_term = type("T", (), {"term_number": label})()
        self.snap_point = _Pt(x, y)
        self.net = None


class _FakeInst:
    def __init__(self, name, master, x, y, angle):
        self.name = name
        self.inst_pins = []
        short = ads_ops._master_short(master)
        offs = ads_ops._AUTO_PIN_OFFS.get(short, ads_ops._AUTO_PIN_OFFS_DEFAULT)
        for lbl, (dx, dy) in offs.items():
            rx, ry = ads_ops._rot_off(dx, dy, int(angle or 0))
            self.inst_pins.append(_FakePin(lbl, x + rx, y + ry))
        rect = _rot_box(R0_BOX[short], angle)
        self.bbox = type("B", (), {
            "lower_left": _Pt(x + rect[0], y + rect[1]),
            "upper_right": _Pt(x + rect[2], y + rect[3])})()


class _FakeDesign:
    def __init__(self):
        self.instances = []
        self.wires = []
        self._net_seq = 0

    def add_wire(self, points):
        self.wires.append([tuple(p) for p in points])

    def add_scalar_net(self):
        self._net_seq += 1
        return f"N__{self._net_seq}"


def _build_fm_murata():
    """按 build_schematic 的 layout=auto 管线复建场景（布局计划在线重放）。"""
    instances = [dict(s) for s in INSTANCES]
    connections = [dict(c) for c in CONNECTIONS]
    positions = ads_ops.auto_layout_positions(instances, connections, VAR_SPEC)
    wire_plan = ads_ops.plan_wire_routes(instances, connections, positions)
    conns = []
    for i, c in enumerate(connections):
        m = wire_plan.get(i)
        if m:
            if m[0] is None:
                c = dict(c, _bind_only=True, _net=m[1])
            else:
                c = dict(c, waypoints=[list(w) for w in m[0]])
                c["_net"] = m[1]
                if len(m) > 2 and any(m[2]):
                    c["_skip_segs"] = list(m[2])
        conns.append(c)
    d = _FakeDesign()
    for s in instances:
        pos = positions[str(s["name"])]
        d.instances.append(_FakeInst(s["name"], s["master"],
                                     pos["x"], pos["y"], pos.get("angle") or 0))
    return d, conns


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_fm_murata_full_build_routes_without_failure():
    """2026-10-09 实机停摆场景：全部连接必须在真几何上走通。"""
    d, conns = _build_fm_murata()
    routed = []
    ads_ops._wire_all_connections(d, conns, routed)   # 修复前在此抛 RuntimeError
    # _bind_only 的连接（路径已被同网长线完全覆盖）只做网络绑定、不画线，
    # 也不进 routed —— 应按画线连接数核对
    drawable = [c for c in conns if not c.get("_bind_only")]
    eq(len(routed), len(drawable), "每条需要画线的连接都应完成走线")
    ok(len(d.wires) > 0, "导线应真实落入设计")

    # 全部段正交、无零长
    for w in d.wires:
        eq(len(w), 2, "每根导线应是单段直线")
        (x1, y1), (x2, y2) = w
        ok(abs(x1 - x2) < 1e-9 or abs(y1 - y2) < 1e-9,
           f"段必须水平或竖直: {w}")
        ok(abs(x1 - x2) > 1e-9 or abs(y1 - y2) > 1e-9, f"不允许零长段: {w}")

    # 关键回归点：Q1.c -> LC.2 的最短直角路径 (11.4,1.0)->(11.95,1.0)->(11.95,1.75)
    # 在失败现场被绕行线堵死；修复后应作为直连成功。
    found = [pts for pts, _net in routed
             if pts[0] == (11.4, 1.0) and pts[-1] == (11.95, 1.75)]
    ok(found, "Q1.1->LC.2 的连接应有走线")
    eq(len(found[0]), 3, f"该连接应为两点一拐的直连: {found[0]}")
    eq(found[0][1], (11.95, 1.0), "拐点位置应是 (11.95,1.0)")


def test_fm_murata_drawn_geometry_has_no_cross_net_conflicts():
    """画出的导线两两复核：不同电气网络之间不得交叉/重叠/T 搭。"""
    d, conns = _build_fm_murata()
    routed = []
    ads_ops._wire_all_connections(d, conns, routed)
    seg_net = []
    for pts, net in routed:
        for a, b in zip(pts, pts[1:]):
            seg_net.append((a, b, net))
    for i in range(len(seg_net)):
        for j in range(i + 1, len(seg_net)):
            a, b, ni = seg_net[i]
            c, dd, nj = seg_net[j]
            if ni is not None and ni == nj:
                continue
            hit = ads_ops._segment_hits_route(
                a, b, c, dd, {a, b}, {c, dd})
            ok(not hit, f"跨网相交: net={ni} {a}-{b} × net={nj} {c}-{dd}")


def test_wire_all_connections_defers_rejected_waypoints():
    """折点被拒的连接必须延后重试（而不是当场兜底），且重试时不带折点。"""
    calls = []

    class _D(_FakeDesign):
        def add_wire(self, points):
            calls.append(("wire", tuple(map(tuple, points))))

    real = ads_ops._wire_connection

    def fake_wire(design, conn, routes=None):
        calls.append(("wire_connection", conn.get("a"), conn.get("b"),
                      "waypoints" in conn, "_skip_segs" in conn))
        if "waypoints" in conn:
            raise RuntimeError(f"指定折点使导线穿过元件或与已有导线交叉/重叠：{conn}")
        return real(design, conn, routes)

    saved = ads_ops._wire_connection
    ads_ops._wire_connection = fake_wire
    try:
        d = _FakeDesign()
        d.instances = [
            _FakeInst("A", "ads_rflib:R", 0, 0, 0),
            _FakeInst("B", "ads_rflib:R", 4, 4, 0),
            _FakeInst("C", "ads_rflib:R", 8, 0, 0),
        ]
        conns = [
            {"a": ["A", 1], "b": ["B", 1], "waypoints": [[1, 1]],
             "_skip_segs": [False]},
            {"a": ["B", 2], "b": ["C", 1]},
        ]
        ads_ops._wire_all_connections(d, conns, [])
    finally:
        ads_ops._wire_connection = saved

    order = [c for c in calls if c[0] == "wire_connection"]
    # 第一次 A->B（带折点）被拒 → 延后；B->C 先画；A->B 最后重试（不带折点/_skip_segs）
    eq(len(order), 3, f"应恰好三次走线尝试: {order}")
    eq(order[0][1], ["A", 1], "第一次尝试是 A->B（带折点）")
    eq(order[1][1], ["B", 2], "第二次是其余连接 B->C")
    eq(order[2][1], ["A", 1], "A->B 延后到末尾重试")
    eq(order[2][3], False, "重试时不带 waypoints")
    eq(order[2][4], False, "重试时丢弃陈旧 _skip_segs")


def test_route_around_refines_grid_through_tight_gap():
    """围墙缺口恰为网格步长 0.25 时，必须用缝隙中点穿越。

    实测场景（本用例可离线复现）：矩形围墙只留 y∈(0.1, 0.35) 的缺口，
    缺口两端又正好是墙线的端点 —— 粗网格的行集合里没有任何一行严格
    落在缺口内（0.1/0.35 两行贴着墙端点接线，被判相交），粗网格整片
    搜死。补插相邻网格线中点后唯一通路是 y=0.225。
    """
    ring = [
        [(-3.0, 3.0), (5.0, 3.0)], [(-3.0, -3.0), (5.0, -3.0)],
        [(-3.0, -3.0), (-3.0, 3.0)],
        [(5.0, -3.0), (5.0, 0.1)], [(5.0, 0.35), (5.0, 3.0)],
    ]
    path = ads_ops._route_around((0.0, 0.0), (6.0, 0.0), [], ring)
    ok(ads_ops._route_clear(path, [], ring), f"路径必须不触碰围墙: {path}")
    eq([round(y, 6) for _, y in path], [0.0, 0.225, 0.225, 0.0],
       f"必须在缝隙中点穿越: {path}")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
