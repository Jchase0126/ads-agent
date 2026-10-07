# -*- coding: utf-8 -*-
"""CB_FM_Amp 验收门禁(离线)。基线见 docs/cb_amp_baseline_20260929.md。

四类问题的量化门禁,全部对 layout+走线计划的**规划几何**断言;
在线门禁(保存后磁盘几何+网表)由 tests/probes/AJ_build_cb_amp.py 在
实机执行,两道门禁互补。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "addon", "ads_agent"))
sys.path.insert(0, os.path.dirname(__file__))

import ads_ops
from cb_amp_case import INSTANCES, CONNECTIONS
from _harness import eq, ok


def _pin_xy(placed, name, lbl):
    p = placed[name]
    spec = next(s for s in INSTANCES if s["name"] == name)
    ang = p.get("angle")
    if ang is None:
        ang = int(spec.get("angle") or 0)
    o = ads_ops._AUTO_PIN_OFFS.get(ads_ops._master_short(spec["master"]),
                                   ads_ops._AUTO_PIN_OFFS_DEFAULT)
    dx, dy = ads_ops._rot_off(*o.get(str(lbl), (0.0, 0.0)), int(ang or 0))
    return (round(p["x"] + dx, 6), round(p["y"] + dy, 6))


def _layout_and_plan():
    placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
    plan = ads_ops.plan_wire_routes(INSTANCES, CONNECTIONS, placed)
    return placed, plan


def _segs(placed, plan):
    """全部连接的折线段列表 (conn_idx, a, b)。"""
    out = []
    for i, conn in enumerate(CONNECTIONS):
        wp, _tag = plan.get(i, ([], ""))
        pa = _pin_xy(placed, *conn["a"])
        pb = _pin_xy(placed, *conn["b"])
        pts = [pa] + list(wp) + [pb]
        for a, b in zip(pts, pts[1:]):
            if a != b:
                out.append((i, a, b))
    return out


def _net_of():
    """连接索引 -> 电气 net 键(并查集)。"""
    parent = {}

    def find(k):
        while parent.get(k, k) != k:
            parent[k] = parent.get(parent[k], parent[k])
            k = parent[k]
        return k

    for i, conn in enumerate(CONNECTIONS):
        ka = (str(conn["a"][0]), str(conn["a"][1]))
        kb = (str(conn["b"][0]), str(conn["b"][1]))
        ra, rb = find(ka), find(kb)
        if ra != rb:
            parent[rb] = ra
    return {i: find((str(c["a"][0]), str(c["a"][1])))
            for i, c in enumerate(CONNECTIONS)}


def test_topology_preserved_all_instances_placed():
    placed, _ = _layout_and_plan()
    eq(len(placed), len(INSTANCES), "全部 25 个实例都应有落位")


def test_signal_flow_single_row_no_detour():
    # 规则 1:输入链→Q1→输出链同在主干行顺向;偏置/去耦/负载/接地支路就近
    placed, _ = _layout_and_plan()
    row = placed["Q1"]["y"]
    # 骑线件(端口)按引入/引出引脚判行;串联件按出线脚判行(与 place_chain
    # 的落位语义一致)
    flow = ("PORT1", "CIN", "LIN", "CDCIN", "Q1", "CDCOUT", "CPOUT",
            "LOUT", "PORT2")
    # 主干行 = 流上各引入脚的众数行(Q1.b 在 origin、origin 可随符号
    # 微降,判据看"全链引入脚同一直线"而不是 Q1 的 origin)
    row = None
    ys = [_pin_xy(placed, n, "1")[1] for n in flow]
    row = max(set(ys), key=ys.count)
    for n in flow:
        pin = _pin_xy(placed, n, "1")
        eq(pin[1], row, f"{n}.1 应在主干行(真实信号流顺向摆放)")
    xs = [_pin_xy(placed, n, "1")[0] for n in flow]
    eq(xs, sorted(xs), "主干行应从左到右单调")
    # 支路就近:RE 贴 Q1.e、RLOAD 贴 Q1.c 母线 2 格内、偏置链贴 Q1.b
    qe = _pin_xy(placed, "Q1", "3")
    ok(abs(placed["RE"]["y"] - qe[1]) <= 1.5, "RE 应贴发射极就近放置")
    ok(abs(placed["RE"]["x"] - qe[0]) <= 2.0, "RE 应在发射极出线方向就近")
    qb = _pin_xy(placed, "Q1", "2")
    ok(max(abs(placed[n]["x"] - qb[0]) for n in ("CB", "R2", "R1")) <= 6.0,
       "偏置链应挂在基极附近")


def test_no_parallel_lines_same_node():
    # 规则 2:同一 net 不得有两条表达同一分流关系的平行水平段
    # (x 区间重叠、y 不同、非同一条线)
    placed, plan = _layout_and_plan()
    net_of = _net_of()
    hsegs = []
    for i, a, b in _segs(placed, plan):
        if abs(a[1] - b[1]) < 1e-9 and abs(a[0] - b[0]) > 1e-9:
            hsegs.append((net_of[i], min(a[0], b[0]), max(a[0], b[0]), a[1], i))
    for i in range(len(hsegs)):
        for j in range(i + 1, len(hsegs)):
            n1, x1a, x1b, y1, w1 = hsegs[i]
            n2, x2a, x2b, y2, w2 = hsegs[j]
            if n1 != n2 or abs(y1 - y2) < 1e-9:
                continue
            overlap = min(x1b, x2b) - max(x1a, x2a)
            ok(overlap <= 1e-9,
               f"net {n1} 平行水平段 y={y1}(conn{w1}) 与 y={y2}(conn{w2}) "
               f"x 重叠 {overlap:.2f} 格 —— 同一节点不得拉出两条平行线")


def test_route_efficiency_no_detour():
    # 规则 4:总绕行系数、拐弯数、逐线绕行上限(基线 165.43 格/1.263/41 拐)
    placed, plan = _layout_and_plan()
    total = lb_total = 0.0
    bends_total = 0
    for i, conn in enumerate(CONNECTIONS):
        wp, _tag = plan.get(i, ([], ""))
        pa = _pin_xy(placed, *conn["a"])
        pb = _pin_xy(placed, *conn["b"])
        pts = [pa] + list(wp) + [pb]
        L = sum(abs(a[0] - b[0]) + abs(a[1] - b[1])
                for a, b in zip(pts, pts[1:]))
        lb = abs(pa[0] - pb[0]) + abs(pa[1] - pb[1])
        total += L
        lb_total += lb
        nb = sum(1 for t in zip(pts, pts[1:], pts[2:])
                 if (t[0][0] == t[1][0]) != (t[1][0] == t[2][0]))
        bends_total += nb
        ok(nb <= 2, f"conn{i} {conn['a']}->{conn['b']} 拐弯 {nb} 次(>2)")
        ok(L <= lb * 1.001 + 1e-9,
           f"conn{i} {conn['a']}->{conn['b']} 绕行系数 "
           f"{L / lb if lb else 0:.3f}(S 形/大 C 形回绕)")
    ok(total <= 90.0, f"总长 {total:.1f} 格超过 90(基线 165.43)")
    ok(total / lb_total <= 1.05,
       f"总体绕行系数 {total / lb_total:.3f} 超过 1.05(基线 1.263)")
    ok(bends_total <= 12, f"拐弯总数 {bends_total} 超过 12(基线 41)")


def test_no_backtrack():
    # 规则 4:同轴折返 —— 同一条线里水平推进方向翻转
    placed, plan = _layout_and_plan()
    for i, conn in enumerate(CONNECTIONS):
        wp, _tag = plan.get(i, ([], ""))
        pa = _pin_xy(placed, *conn["a"])
        pb = _pin_xy(placed, *conn["b"])
        pts = [pa] + list(wp) + [pb]
        prev_dx = None
        for a, b in zip(pts, pts[1:]):
            if abs(a[1] - b[1]) > 1e-9:
                continue
            dx = 1 if b[0] > a[0] + 1e-9 else (-1 if b[0] < a[0] - 1e-9 else 0)
            if dx != 0:
                ok(prev_dx is None or dx == prev_dx,
                   f"conn{i} {conn['a']}->{conn['b']} 水平折返 {pts}")
                prev_dx = dx


def test_gnd_orientation_by_access_direction():
    # 规则 3:接地朝向按支路出线方向 —— GROUND 符号体方位实测建档
    # (0°体在右/90°体在上/180°体在左/270°体在下)。地脚引出线竖直向下
    # 的地应 270(正置),竖直向上的应 90(倒置)
    placed, _ = _layout_and_plan()
    for gname, comp, lbl in (("G1", "CIN", "2"), ("G2", "RE", "2"),
                             ("G4", "CB", "2"), ("G5", "R2", "2"),
                             ("G6", "CPOUT", "2"), ("G7", "RLOAD", "2"),
                             ("G3", "VCC", "2")):
        pin = _pin_xy(placed, comp, lbl)
        g = placed[gname]
        below = g["y"] < pin[1]
        want = 270 if below else 90
        eq(g.get("angle"), want,
           f"{gname} 挂 {comp}.{lbl} {'下方应正置 270' if below else '上方应倒置 90'}")
    # 下垂距离就近(≤1.5 格),不再定长 2~3 格
    for gname, comp, lbl in (("G1", "CIN", "2"), ("G2", "RE", "2"),
                             ("G4", "CB", "2"), ("G5", "R2", "2"),
                             ("G6", "CPOUT", "2"), ("G7", "RLOAD", "2")):
        pin = _pin_xy(placed, comp, lbl)
        ok(abs(placed[gname]["y"] - pin[1]) <= 1.5,
           f"{gname} 下垂 {abs(placed[gname]['y'] - pin[1]):.1f} 格超 1.5")


def test_same_line_left_right_of_junction():
    # 规则 2:同一节点分别向左右连接时两段线共线 —— 集电极节点 Q1.1 的
    # 三条出线(CDCOUT 向右 / LC 向上 / RLOAD 向下)不产生第二条水平线;
    # 输入链 PORT1-CIN-LIN 的水平段全在 y=Q1 行
    placed, plan = _layout_and_plan()
    net_of = _net_of()
    q1c = _net_of()[12]  # conn12: Q1.1-CDCOUT.1 的 net
    hlines = {}
    for i, a, b in _segs(placed, plan):
        if abs(a[1] - b[1]) < 1e-9 and net_of[i] == q1c:
            hlines.setdefault(round(a[1], 6), []).append(i)
    eq(len(hlines), 1,
       f"集电极 net 的水平段应共线一行,实际 {len(hlines)} 行: {hlines}")


def test_plan_metrics_documented():
    # 阶段产物记录:打印当前规划指标,人工核对基线文档
    placed, plan = _layout_and_plan()
    total = 0.0
    for i, conn in enumerate(CONNECTIONS):
        wp, _tag = plan.get(i, ([], ""))
        pa = _pin_xy(placed, *conn["a"])
        pb = _pin_xy(placed, *conn["b"])
        pts = [pa] + list(wp) + [pb]
        total += sum(abs(a[0] - b[0]) + abs(a[1] - b[1])
                     for a, b in zip(pts, pts[1:]))
    print(f"\n[CB_FM_Amp 规划指标] 总长 {total:.2f} 格(基线 165.43)")
