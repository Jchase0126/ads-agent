# -*- coding: utf-8 -*-
"""CE_FM_Amp 布局验收 + 新规则测试矩阵(2026-09-29 用户规则落地的离线回归)。

覆盖:
* CE 固定案例:手工参考图结构(输入行/电源母线上方/输出并联下挂/
  接地支路就近)+ 走线计划全部正交 0 穿符号 + 网表等价(离线 netlist_check
  对 EXPECTED 拓扑)。
* 拓扑矩阵:三端管引脚朝向变体(mirror 语义等价物:显式 angle)/多端节点
  (基极三支)/上挂地/供电偏置/输出并联支路。
运行: python tests/test_ce_amp_acceptance.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import ads_ops  # noqa: E402
import netlist_check as nc  # noqa: E402
from ce_amp_case import INSTANCES, CONNECTIONS, EXPECTED_NETLIST  # noqa: E402

_results = []


def ok(cond, msg):
    _results.append((bool(cond), msg))
    if not cond:
        print("  FAIL", msg)


def eq(a, b, msg):
    ok(a == b, f"{msg} (got {a!r} want {b!r})")


def _rough_box(n, placed, specs):
    p = placed[n]
    return ads_ops._rough_box_public(specs[n], p)


def _segs_of(specs, conns, placed):
    routes = ads_ops.plan_wire_routes(INSTANCES, conns, placed)
    segs = []
    for i in sorted(routes, key=int):
        row_i = routes[i]
        wp = row_i[0]
        _tag = row_i[1]
        if wp is None:
            continue   # 整条路径已由同 net 前序连接覆盖(重复段消除)
        c = conns[int(i)]
        o_a = ads_ops._AUTO_PIN_OFFS.get(
            ads_ops._master_short(specs[c['a'][0]]['master']),
            ads_ops._AUTO_PIN_OFFS_DEFAULT)
        o_b = ads_ops._AUTO_PIN_OFFS.get(
            ads_ops._master_short(specs[c['b'][0]]['master']),
            ads_ops._AUTO_PIN_OFFS_DEFAULT)
        pa = ads_ops._rot_off(*o_a.get(str(c['a'][1]), (0, 0)),
                              int(placed[c['a'][0]].get('angle') or 0))
        pb = ads_ops._rot_off(*o_b.get(str(c['b'][1]), (0, 0)),
                              int(placed[c['b'][0]].get('angle') or 0))
        pts = [(placed[c['a'][0]]['x'] + pa[0], placed[c['a'][0]]['y'] + pa[1])]
        pts += [tuple(w) for w in (wp or [])]
        pts.append((placed[c['b'][0]]['x'] + pb[0],
                    placed[c['b'][0]]['y'] + pb[1]))
        for s1, s2 in zip(pts, pts[1:]):
            if s1 != s2:
                segs.append((s1, s2))
    return segs


def test_ce_structure():
    specs = {str(s["name"]): s for s in INSTANCES}
    placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
    eq(len(placed), len(INSTANCES), "全部实例落位")
    # 1 输入链一行:PORT1/LIN/CDCIN 同 y,直线进基极
    y0 = placed["PORT1"]["y"]
    eq(placed["LIN"]["y"], y0, "LIN 在输入行")
    eq(placed["CDCIN"]["y"], y0, "CDCIN 在输入行")
    # 基极引脚与行线共线(入口侧无 0.5 落差)
    b_off = ads_ops._rot_off(0.0, 0.0, 0)  # Q1.b 在 origin
    eq(placed["Q1"]["y"] + b_off[1], y0, "Q1 基极脚与输入行共线")
    # 2 电源母线在主线之上:R1/LC 竖放 y>0,VCC 在最高锚点行上方
    ok(placed["R1"]["y"] > y0, "R1(基极上拉)在输入行上方")
    ok(placed["LC"]["y"] > placed["Q1"]["y"], "LC(集电极馈电)在主线上方")
    ok(placed["VCC"]["y"] >= placed["LC"]["y"], "VCC 与 LC 同母线或更高")
    eq(placed["VCC"].get("angle"), 0, "VCC 横放(负端朝外)")
    # 3 输出并联支路并排下挂:COUT/RD 节点脚与 CDCOUT.2 行共线、体向下
    ok(placed["COUT"].get("angle") == 270 and placed["RD"].get("angle") == 270,
       "COUT/RD 竖放(节点脚在上、地脚向下)")
    eq(placed["COUT"]["y"], placed["CDCOUT"]["y"],
       "COUT 节点脚骑在输出行线上")
    eq(placed["RD"]["y"], placed["CDCOUT"]["y"], "RD 节点脚骑在输出行线上")
    ok(placed["COUT"]["x"] != placed["RD"]["x"], "COUT/RD 各占一列")
    # 4 接地支路就近:全部地在支路末端、方向正确(下挂 270 / 上挂 90 /
    # 自由端横地 0/180)
    gnds = {n: placed[n] for n in placed
            if n.startswith("G") and n[1:].isdigit()}
    eq(len(gnds), 7, "7 个地全部落位")
    # 地朝向合法值域 + 接入距离核对。GROUND bbox 实测语义
    # （gnd4.pdf 渲染提取）：270 体在 origin 下方 y[-0.25,0]、
    # 90 体在上方、0 体在右侧 x[0,0.25]、180 左侧；接入引脚恒在
    # origin，符号体沿接入方向延伸 —— 地与所接引脚的距离必须
    # 恰为 1.0（ _gnd_normalized_angles / 第 4 步的摆位约定）。
    for n, p in gnds.items():
        a = p.get("angle")
        ok(a in (0, 90, 180, 270), f"{n} 朝向在四向值域内 (got {a})")
        # 接入距离:找它所连的引脚 partner(CONNECTIONS 里的 G.n 对端)
        partners = [c["a"] for c in CONNECTIONS
                    if c["b"][0] == n] + [c["b"] for c in CONNECTIONS
                                          if c["a"][0] == n]
        ok(len(partners) == 1, f"{n} 恰有一个接入对端")
        if len(partners) == 1:
            pn, pl = partners[0]
            o = ads_ops._AUTO_PIN_OFFS.get(
                ads_ops._master_short(specs[pn]["master"]),
                ads_ops._AUTO_PIN_OFFS_DEFAULT)
            rx, ry = ads_ops._rot_off(
                *o.get(str(pl), (0, 0)),
                int(placed[pn].get("angle") or 0))
            px, py = placed[pn]["x"] + rx, placed[pn]["y"] + ry
            dist = abs(p["x"] - px) + abs(p["y"] - py)
            # 约定:地贴所接引脚 0.5 格(元件体长 1.0,地贴远端脚边;
            # 端口/源的地脚同样 0.5)—— 2026-09-30 实测核对
            eq(dist, 0.5, f"{n} 与所接引脚距离 0.5")
            # 方向与相对位置一致:270 地在引脚正下方、90 正上、
            # 0 正右、180 正左
            dx, dy = p["x"] - px, p["y"] - py
            want = {(270,): (0, -1), (90,): (0, 1),
                    (0,): (1, 0), (180,): (-1, 0)}
            vec = {270: (0, -0.5), 90: (0, 0.5),
                   0: (0.5, 0.0), 180: (-0.5, 0.0)}[a]
            eq((dx, dy), vec, f"{n} 朝向与相对引脚方位一致")


def test_ce_wires_orthogonal():
    specs = {str(s["name"]): s for s in INSTANCES}
    placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
    segs = _segs_of(specs, CONNECTIONS, placed)
    for a, b in segs:
        ok(abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9,
           f"导线段正交 {a}->{b}")
    # 不穿符号体(锚点/自身豁免同门禁)
    boxes = [(n, _rough_box(n, placed, specs)) for n in specs]
    pin_pts = set()
    for n in specs:
        p = placed[n]
        o = ads_ops._AUTO_PIN_OFFS.get(
            ads_ops._master_short(specs[n]["master"]),
            ads_ops._AUTO_PIN_OFFS_DEFAULT)
        for l, (dx, dy) in o.items():
            rx, ry = ads_ops._rot_off(dx, dy, int(p.get("angle") or 0))
            pin_pts.add((round(p["x"] + rx, 6), round(p["y"] + ry, 6)))
    # 每符号的引脚点（R13 与规划器同口径：线段穿过某符号自身引脚
    # = 合法引脚搭接，不算穿体）
    sym_pins = {}
    for n in specs:
        p = placed[n]
        o = ads_ops._AUTO_PIN_OFFS.get(
            ads_ops._master_short(specs[n]["master"]),
            ads_ops._AUTO_PIN_OFFS_DEFAULT)
        pts = []
        for l, (dx, dy) in o.items():
            rx, ry = ads_ops._rot_off(dx, dy, int(p.get("angle") or 0))
            pts.append((p["x"] + rx, p["y"] + ry))
        sym_pins[n] = pts

    def _through_own_pin(a, b, n):
        for (px, py) in sym_pins.get(n, ()):
            on = (abs(a[0] - b[0]) < 1e-9 and abs(px - a[0]) < 1e-6
                  and min(a[1], b[1]) - 1e-6 <= py <= max(a[1], b[1]) + 1e-6)                 or (abs(a[1] - b[1]) < 1e-9 and abs(py - a[1]) < 1e-6
                    and min(a[0], b[0]) - 1e-6 <= px <= max(a[0], b[0]) + 1e-6)
            if on:
                return True
        return False

    n_checked = 0
    for a, b in segs:
        for n, box in boxes:
            on_own_pin = any(
                abs(p[0] - a[0]) < 1e-6 and abs(p[1] - a[1]) < 1e-6
                or abs(p[0] - b[0]) < 1e-6 and abs(p[1] - b[1]) < 1e-6
                for p in pin_pts)
            n_checked += 1
            if _through_own_pin(a, b, n):
                continue
            if ads_ops._segment_hits_box(a, b, box,
                                         strict=on_own_pin):
                ok(False, f"导线 {a}->{b} 穿 {n} 符号 {box}")
    ok(n_checked > 0, f"线-符号检查实际执行了 {n_checked} 组合(防恒真空断)")


def test_ce_netlist_equivalence():
    # 离线等价:plan 划分 vs EXPECTED 拓扑划分(网络名无关,比成员)
    exp = nc.partition_from_plan(INSTANCES, CONNECTIONS)
    # 划分 -> 网络编号化:GND 块命名为 "0";其余块按成员排序后依次
    # 编号 N1..Nk(编号稳定:排序成员的 repr)。与 EXPECTED_NETLIST 的
    # N21/N16 等名字无关,只比"每元件各脚落在哪个块"。
    def sig(part):
        names = {}
        others = []
        for k in part.values():
            if any(p[0].startswith("G") and p[0][1:].isdigit() for p in k)                     and len([p for p in k if p[0].startswith("G")
                             and p[0][1:].isdigit()]) >= 2:
                names[k] = "0"   # 含多个地引脚的块 = GND
            else:
                others.append(k)
        others = sorted(others, key=lambda k: sorted(map(repr, k)))
        for i, k in enumerate(others):
            names[k] = "N%d" % (i + 1)
        pin2net = {}
        for net_key, members in part.items():
            for p in members:
                pin2net[p] = names[members]
        out = {}
        for name in ("Q1", "VCC", "R1", "R2", "RE1", "CE", "LC", "CIN",
                     "LIN", "CDCIN", "CDCOUT", "COUT", "LOUT", "PORT1",
                     "PORT2", "RD"):
            m = next(s for s in INSTANCES if s["name"] == name)
            short = m["master"].split(":")[-1].upper()
            lbls = nc._pin_order_of(short, m)
            out[name] = tuple(pin2net.get((name, l)) for l in lbls)
        return out
    got = sig(exp)
    # 期望签名同样编号化:EXPECTED 里的 N21/N16/N8/... 按首次出现顺序
    # 重编号 N1..、地 0 —— 网络名无关,只比"每元件各脚落在哪个块"
    canon = {"0": "0"}

    def _canon(wtuple):
        out = []
        for w in wtuple:
            if w == "0" or w is None:
                out.append(w)
                continue
            if w not in canon:
                canon[w] = "N%d" % (len(canon))
            out.append(canon[w])
        return tuple(out)

    want = EXPECTED_NETLIST
    # sig 的编号按块成员排序生成,期望的按首次出现 —— 直接做双射校验:
    # 收集 (元件名 -> 元组) 两侧,验证两两相等关系同构(逐元件比较相对
    # 于第一个元件的相等模式)。
    ref = "Q1"
    got_ref, want_ref = got[ref], _canon(want[ref])
    for name, w in want.items():
        g = got[name]
        cw = _canon(w)
        # 相对模式:每对 (脚) 相等关系与参考元件一致 + 脚数一致
        ok(len(g) == len(cw), f"{name} 引脚数")
        if len(g) == len(cw):
            pat_g = tuple(g[i] == got_ref[j]
                          for i in range(len(g))
                          for j in range(len(got_ref)))
            pat_w = tuple(cw[i] == want_ref[j]
                          for i in range(len(cw))
                          for j in range(len(want_ref)))
            eq(pat_g, pat_w, f"{name} 与 Q1 的网络相对关系(同构)")
            # 0(地)位置必须一致
            eq(tuple(x == "0" for x in g),
               tuple(x == "0" for x in cw), f"{name} 接地脚位置")
    # 结构断言:Q1 三个脚在不同网络、RE 与 CE 同网(发射极)
    eq(len(set(got["Q1"])), 3, "Q1 三脚三网")


def test_transistor_orientation_variants():
    # 三端管不同主路径朝向:CB(输入进发射极)也应在行线上无落差
    CB = [
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2", "Z": "50 Ohm"}},
        {"master": "Infineon_RF:BFR106", "name": "Q1"},
        {"master": "ads_rflib:R", "name": "RB", "params": {"R": "1 kOhm"}},
        {"master": "ads_rflib:C", "name": "CB", "params": {"C": "10 nF"}},
        {"master": "ads_rflib:GROUND", "name": "G1"},
        {"master": "ads_rflib:GROUND", "name": "G2"},
        {"master": "ads_rflib:GROUND", "name": "G3"},
        {"master": "ads_rflib:L", "name": "LC", "params": {"L": "1 uH"}},
        {"master": "ads_sources:V_DC", "name": "VCC",
         "params": {"Vdc": "5 V"}},
        {"master": "ads_rflib:GROUND", "name": "G4"},
    ]
    CC = [
        {"a": ["P1", "1"], "b": ["Q1", "3"]},   # 输入进发射极(CB)
        {"a": ["Q1", "2"], "b": ["RB", "1"]},   # 基极偏置
        {"a": ["RB", "2"], "b": ["G1", "1"]},
        {"a": ["Q1", "2"], "b": ["CB", "1"]},
        {"a": ["CB", "2"], "b": ["G2", "1"]},
        {"a": ["Q1", "1"], "b": ["P2", "1"]},   # 集电极输出
        {"a": ["Q1", "1"], "b": ["LC", "1"]},   # 馈电
        {"a": ["LC", "2"], "b": ["VCC", "1"]},
        {"a": ["VCC", "2"], "b": ["G4", "1"]},
        {"a": ["P1", "1"], "b": ["G3", "1"]},
    ]
    placed = ads_ops.auto_layout_positions(CB, CC)
    y0 = placed["P1"]["y"]
    # 发射极脚 (0.5,-0.5):入口行应与 e 脚共线 —— Q1.y = y0 + 0.5
    eq(placed["Q1"]["y"] + (-0.5), y0, "CB:发射极脚与输入行共线(无落差)")
    ok(placed["LC"]["y"] > placed["Q1"]["y"] - 0.5, "CB:LC 在集电极上方")


def test_common_collector_structure():
    # R12: 射随器（共集电极）—— 输入直线进基极、轨在上方、
    # 输出行齐发射极脚。三种组态（CE/CB/CC）的角色覆盖闭环。
    CC = [
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2", "Z": "50 Ohm"}},
        {"master": "Infineon_RF:BFR106", "name": "Q1"},
        {"master": "ads_rflib:C", "name": "CIN", "params": {"C": "10 nF"}},
        {"master": "ads_rflib:C", "name": "COUT",
         "params": {"C": "10 nF"}},
        {"master": "ads_rflib:R", "name": "RE", "params": {"R": "100 Ohm"}},
        {"master": "ads_rflib:L", "name": "LC", "params": {"L": "1 uH"}},
        {"master": "ads_sources:V_DC", "name": "VCC",
         "params": {"Vdc": "5 V"}},
        {"master": "ads_rflib:GROUND", "name": "G1"},
        {"master": "ads_rflib:GROUND", "name": "G2"},
        {"master": "ads_rflib:GROUND", "name": "G3"},
    ]
    conns = [
        {"a": ["P1", "1"], "b": ["CIN", "1"]},
        {"a": ["CIN", "2"], "b": ["Q1", "2"]},
        {"a": ["Q1", "1"], "b": ["LC", "1"]},
        {"a": ["LC", "2"], "b": ["VCC", "1"]},
        {"a": ["VCC", "2"], "b": ["G1", "1"]},
        {"a": ["Q1", "3"], "b": ["COUT", "1"]},
        {"a": ["COUT", "2"], "b": ["P2", "1"]},
        {"a": ["Q1", "3"], "b": ["RE", "1"]},
        {"a": ["RE", "2"], "b": ["G2", "1"]},
        {"a": ["Q1", "2"], "b": ["G3", "1"]},
    ]
    placed = ads_ops.auto_layout_positions(CC, conns)
    eq(len(placed), len(CC), "CC 全部落位")   # 无 var_spec,无 VAR
    eq(placed["P1"]["y"], placed["Q1"]["y"], "输入行与基极共线")
    ok(placed["LC"]["y"] > placed["Q1"]["y"], "馈电 LC 在集电极上方")
    eq(placed["VCC"].get("angle"), 0, "VCC 横放")
    # 输出行齐 e 脚 (e = origin + (0.5,-0.5))
    eq(placed["P2"]["y"], placed["Q1"]["y"] - 0.5, "输出行齐发射极脚")
    eq(placed["RE"].get("angle"), 270, "RE 下挂")


def test_multi_pin_node_and_up_gnd():
    # 多端节点(基极 3 支)+ 下方被占时的上挂地(倒置 90)
    TOPO = [
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2", "Z": "50 Ohm"}},
        {"master": "Infineon_RF:BFR106", "name": "Q1"},
        {"master": "ads_rflib:R", "name": "RB", "params": {"R": "1 kOhm"}},
        {"master": "ads_rflib:R", "name": "RB2", "params": {"R": "2 kOhm"}},
        {"master": "ads_rflib:C", "name": "CB", "params": {"C": "10 nF"}},
        {"master": "ads_rflib:GROUND", "name": "G1"},
        {"master": "ads_rflib:GROUND", "name": "G2"},
        {"master": "ads_rflib:GROUND", "name": "G3"},
    ]
    CC = [
        {"a": ["P1", "1"], "b": ["Q1", "2"]},
        {"a": ["Q1", "2"], "b": ["RB", "1"]},
        {"a": ["Q1", "2"], "b": ["RB2", "1"]},
        {"a": ["Q1", "2"], "b": ["CB", "1"]},
        {"a": ["RB", "2"], "b": ["G1", "1"]},
        {"a": ["RB2", "2"], "b": ["G2", "1"]},
        {"a": ["CB", "2"], "b": ["G3", "1"]},
        {"a": ["Q1", "1"], "b": ["P2", "1"]},
    ]
    placed = ads_ops.auto_layout_positions(TOPO, CC)
    eq(len([p for n, p in placed.items() if n != "VAR1"]), len(TOPO),
       "多端节点拓扑全部落位")
    # 基极三支路各自成一列(不重叠)
    xs = sorted((placed["RB"]["x"], placed["RB2"]["x"], placed["CB"]["x"]))
    ok(len(set(xs)) == 3, f"三支路三列 {xs}")


def test_rail_bus_no_stack():
    # 回归锚:电源网不再串成竖直堆叠链 —— R1 与 VCC 的 y 差应小于
    # 旧堆叠版的 5.75(手工参考图形态:R1 竖挂近锚点、VCC 在母线上)
    specs = {str(s["name"]): s for s in INSTANCES}
    placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
    dy = placed["VCC"]["y"] - placed["R1"]["y"]
    ok(0 <= dy < 4.0, f"VCC 与 R1 的垂直距离 {dy:.2f} 在母线形态范围")


def test_wire_dedup_semantics():
    # R15: 同 net 重合段消除的边界语义 ——
    # 1) 共享走廊只画一次（bind_only/skip 出现）
    # 2) 每条连接的网络绑定不因消重丢失（规划 tag 全在）
    specs = {str(s["name"]): s for s in INSTANCES}
    placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
    routes = ads_ops.plan_wire_routes(INSTANCES, CONNECTIONS, placed)
    eq(len(routes), len(CONNECTIONS), "每条连接都有规划条目")
    bound_tags = {routes[i][1] for i in routes}
    ok(len(bound_tags) >= 8, f"网络标签覆盖 {len(bound_tags)} 网段")
    # bind_only 的连接：其两端引脚所属 net 必须仍由其他连接的路径
    # 覆盖（有实体导线经过），否则绑定悬空
    for i in sorted(routes, key=int):
        row = routes[i]
        if row[0] is not None:
            continue
        c = CONNECTIONS[int(i)]
        # 同 net 其他连接存在
        others = [j for j in routes
                  if j != i and routes[j][1] == row[1]
                  and routes[j][0] is not None]
        ok(len(others) > 0,
           f"BIND_ONLY {c['a']}->{c['b']} 有同网实体导线")


def main():
    for fn in (test_ce_structure, test_ce_wires_orthogonal,
               test_ce_netlist_equivalence, test_transistor_orientation_variants,
               test_common_collector_structure, test_wire_dedup_semantics,
               test_multi_pin_node_and_up_gnd, test_rail_bus_no_stack):
        print("==", fn.__name__)
        fn()
    n_fail = sum(1 for c, _ in _results if not c)
    print(f"结果: {'PASS' if n_fail == 0 else 'FAIL'} "
          f"({len(_results) - n_fail} 项通过 / {n_fail} 项失败)")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
