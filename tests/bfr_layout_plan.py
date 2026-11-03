# -*- coding: utf-8 -*-
"""BFR106 FM_SC 重排:离线布局校验(2026-10-03) v2。

实测建档(scratch _bfr_probe 2026-10-03):
- Q1@270 引脚: 1=c(+0.5,-0.5) 2=b(0,0) 3=e(-0.5,-0.5) —— E 左 C 右同水平线,B 朝上;
- Q1@270 文字框 rel [0.581,-0.375,1.169,0];
- R/C@180 文字框 rel R[-0.794,-0.731,-0.019,-0.15] C[-0.794,-0.788,-0.269,-0.206];
- GND 朝向归一化: 伙伴左->体向左, 伙伴上->体向下(正置)。
基极簇为轴向进入的狗腿支路(垂直顶入横放件 fat box 会被走线器拒绝)。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "tests"), os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ads_ops  # noqa: E402
import fm_sc_case as case  # noqa: E402

# ---- 新布局:实例 -> (x, y, angle, annot) ------------------------------
PLACED = {
    "PORT1":   (0.0, 0.0, 180, None),
    "CDC":     (1.5, 0.0, 0, None),
    "C1":      (3.1, 0.0, 270, None),
    "L1":      (4.0, 0.0, 0, None),
    "C2":      (5.6, 0.0, 270, None),
    "L2":      (6.5, 0.0, 0, None),
    "RE":      (8.05, 0.0, 270, None),
    "Q1":      (8.9, 0.5, 270, None),
    "CDCOUT":  (10.9, 0.0, 0, None),
    "C3":      (12.4, 0.0, 270, None),
    "RD":      (13.4, 0.0, 270, None),
    "L3":      (14.4, 0.0, 0, None),
    "C4":      (16.0, 0.0, 270, None),
    "L4":      (16.9, 0.0, 0, None),
    "PORT2":   (19.4, 0.0, 0, None),
    "R2":      (8.3, 0.9, 180, None),
    "CB":      (8.3, 1.3, 180, (-0.85, -0.25)),
    "R1":      (8.9, 1.6, 90, (-1.2, 0.0)),
    "LC":      (9.4, 2.6, 270, None),
    "VCC":     (10.4, 2.6, 0, None),
    "G1":      (3.1, -1.5, None, None),
    "G2":      (5.6, -1.5, None, None),
    "G3":      (8.05, -1.5, None, None),
    "G4":      (6.5, 0.9, None, None),
    "G5":      (6.5, 1.3, None, None),
    "G6":      (11.9, 2.6, None, None),
    "G7":      (13.4, -1.5, None, None),
    "G8":      (12.4, -1.5, None, None),
    "G9":      (16.0, -1.5, None, None),
    "INC1":    (0.0, -6.2, 0, None),
    "SP1":     (0.0, -8.4, 0, None),
    "VAR1":    (0.2, 12.137, 0, None),
}

# 连接 waypoint 注入(基极簇狗腿:轴向进入横放件)
WP = {
    ("Q1", "2", "R2", "1"): [[8.9, 0.9], [8.3, 0.9]],
    ("Q1", "2", "CB", "1"): [[8.9, 1.3], [8.3, 1.3]],
}

# 链式连接:搭接引脚(C1.1/C2.1/RE.1/C3.1/RD.1/C4.1/LC.1)一律作为连线
# 端点,任何连线的中段都不落第三引脚 —— 显式重建没有计划级去重,
# 中段引脚会因"与已画长线共线重叠"或"穿粗盒"而全线失败。
# 电气划分与 case.CONNECTIONS 逐网相同(下方 assert 校验)。
C = case.C
CONNECTIONS = [
    C(("PORT1", 1), ("CDC", 1)),
    C(("CDC", 2), ("C1", 1)),
    C(("C1", 1), ("L1", 1)),
    C(("C1", 2), ("G1", 1)),
    C(("L1", 2), ("C2", 1)),
    C(("C2", 1), ("L2", 1)),
    C(("C2", 2), ("G2", 1)),
    C(("L2", 2), ("RE", 1)),
    C(("RE", 1), ("Q1", 3)),
    C(("RE", 2), ("G3", 1)),
    C(("Q1", 2), ("R1", 1)),
    C(("Q1", 2), ("R2", 1)),
    C(("Q1", 2), ("CB", 1)),
    C(("R2", 2), ("G4", 1)),
    C(("CB", 2), ("G5", 1)),
    C(("Q1", 1), ("LC", 2)),
    C(("Q1", 1), ("CDCOUT", 1)),
    C(("R1", 2), ("LC", 1)),
    C(("LC", 1), ("VCC", 1)),
    C(("VCC", 2), ("G6", 1)),
    C(("CDCOUT", 2), ("C3", 1)),
    C(("C3", 1), ("RD", 1)),
    C(("RD", 1), ("L3", 1)),
    C(("C3", 2), ("G8", 1)),
    C(("RD", 2), ("G7", 1)),
    C(("L3", 2), ("C4", 1)),
    C(("C4", 1), ("L4", 1)),
    C(("C4", 2), ("G9", 1)),
    C(("L4", 2), ("PORT2", 1)),
]


def _assert_same_partition():
    """链式连接与原案例连接的电气划分必须逐网一致。"""
    def partition(conns):
        parent = {}
        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for c in conns:
            a = (str(c["a"][0]), str(c["a"][1]))
            b = (str(c["b"][0]), str(c["b"][1]))
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        groups = {}
        for k in parent:
            groups.setdefault(find(k), set()).add(k)
        return frozenset(frozenset(g) for g in groups.values())
    pa, pb = partition(case.CONNECTIONS), partition(CONNECTIONS)
    if pa != pb:
        raise SystemExit("连接划分不一致: 仅原案例有 %s / 仅链式有 %s" % (
            sorted(pa - pb), sorted(pb - pa)))
    print("连接划分一致: %d 个节点" % len(pa))


_assert_same_partition()

# 实测文字框 rel(origin),覆盖模型缺失/偏大的角度
MEASURED_REL = {
    ("BFR106", 270): (0.581, -0.375, 1.169, 0.0),
    ("R", 180): (-0.794, -0.731, -0.019, -0.15),
    ("C", 180): (-0.794, -0.788, -0.269, -0.206),
}

BODY_HALF = {"R": 0.069, "C": 0.125, "L": 0.062, "V_DC": 0.15}
BODY_LEN = 1.0


def _spec_of(name):
    if name == "VAR1":
        return {"master": "ads_datacmps:VAR", "name": "VAR1"}
    return next(s for s in case.INSTANCES if s["name"] == name)


def _ms(name):
    return ads_ops._master_short(_spec_of(name)["master"])


def pin_off(ms, label):
    table = ads_ops._AUTO_PIN_OFFS.get(ms, ads_ops._AUTO_PIN_OFFS_DEFAULT)
    return table.get(label, ads_ops._AUTO_PIN_OFFS_DEFAULT.get(label, (0.0, 0.0)))


def pin_xy(name, label):
    x, y, ang, _ = PLACED[name]
    dx, dy = pin_off(_ms(name), str(label))
    rx, ry = ads_ops._rot_off(dx, dy, ang or 0)
    return (x + rx, y + ry)


def text_zone(name):
    x, y, ang, annot = PLACED[name]
    ms = _ms(name)
    a = (ang or 0) % 360
    rel = MEASURED_REL.get((ms, a))
    if rel:
        ax, ay = annot or (0.0, 0.0)
        return (x + rel[0] + ax, y + rel[1] + ay,
                x + rel[2] + ax, y + rel[3] + ay)
    ax, ay = annot or (0.0, 0.0)
    return ads_ops._annot_text_zone(ms, x, y, ax, ay, ang or 0)


def body_box(name):
    x, y, ang, _ = PLACED[name]
    ms = _ms(name)
    if ms == "BFR106":
        return (8.4, 0.0, 9.4, 0.5)
    if ms in ("GROUND", "VAR", "S_PARAM", "Infineon_Include_RF"):
        return None
    half = BODY_HALF.get(ms, 0.1)
    a = (ang or 0) % 360
    if a == 90:      # 体向上:pin1 在下,身体 y..y+1
        return (x - half, y, x + half, y + BODY_LEN)
    if a == 270:     # 体向下:pin1 在上,身体 y-1..y
        return (x - half, y - BODY_LEN, x + half, y)
    if a == 180:
        return (x - BODY_LEN, y - half, x, y + half)
    return (x, y - half, x + BODY_LEN, y + half)


def overlap(a, b, pad=0.02):
    return (min(a[2], b[2]) - max(a[0], b[0]) > pad and
            min(a[3], b[3]) - max(a[1], b[1]) > pad)


def main():
    problems = []
    specs_with_xy = []
    for s in case.INSTANCES:
        row = dict(s)
        x, y, ang, _ = PLACED[s["name"]]
        row["x"], row["y"] = x, y
        if ang is not None:
            row["angle"] = ang
        specs_with_xy.append(row)
    gnd_angles = ads_ops._gnd_normalized_angles(specs_with_xy, case.CONNECTIONS)
    print("GND 角度:", gnd_angles)
    for n, a in gnd_angles.items():
        x, y, _, annot = PLACED[n]
        PLACED[n] = (x, y, a, annot)

    conns = []
    for c in CONNECTIONS:
        row = dict(c)
        key = (str(row["a"][0]), str(row["a"][1]), str(row["b"][0]), str(row["b"][1]))
        if key in WP:
            row["waypoints"] = WP[key]
            row["_skip_segs"] = [0]      # 首段与主干走廊重合,只绑网不画
        conns.append(row)
    # 注入电气节点标签(同网走廊豁免用)
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, c in enumerate(CONNECTIONS):
        a = (str(c["a"][0]), str(c["a"][1]))
        b = (str(c["b"][0]), str(c["b"][1]))
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for k in list(parent):
        groups.setdefault(find(k), []).append(k)
    node_of = {}
    for gid, (root, members) in enumerate(sorted(groups.items())):
        for m in members:
            node_of[m] = "N%d" % gid
    for row in conns:
        row["_net"] = node_of[(str(row["a"][0]), str(row["a"][1]))]

    placed = {n: {"x": v[0], "y": v[1], "angle": v[2]} for n, v in PLACED.items()}
    specs = [dict(s) for s in case.INSTANCES]
    routes = ads_ops.plan_wire_routes(specs, conns, placed)

    all_segs = []
    for i, conn in enumerate(conns):
        m = routes.get(i)
        if not m or m[0] is None:
            continue
        pa = pin_xy(conn["a"][0], conn["a"][1])
        pb = pin_xy(conn["b"][0], conn["b"][1])
        pts = [pa] + [tuple(w) for w in (m[0] or [])] + [pb]
        for p, q in zip(pts, pts[1:]):
            if abs(p[0] - q[0]) > 1e-6 or abs(p[1] - q[1]) > 1e-6:
                all_segs.append((p, q, i))
                if abs(p[0] - q[0]) > 1e-6 and abs(p[1] - q[1]) > 1e-6:
                    problems.append("斜线 c%d %s->%s" % (i, p, q))

    print("== 连接路径 ==")
    for i, conn in enumerate(conns):
        m = routes.get(i)
        pa = pin_xy(conn["a"][0], conn["a"][1])
        pb = pin_xy(conn["b"][0], conn["b"][1])
        print("  %-20s %-6s %s -> %s wp=%s" % (
            "%s.%s-%s.%s" % (conn["a"][0], conn["a"][1], conn["b"][0], conn["b"][1]),
            (m[1] if m else "?"), pa, pb, (m[0] if m else None)))

    names = list(PLACED)
    zones = {n: text_zone(n) for n in names}
    bodies = {n: body_box(n) for n in names}

    # 同网引脚点(用于 T 搭豁免):按连接对做并查集
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    pin_pt = {}
    for s in case.INSTANCES:
        if _ms(s["name"]) in ("GROUND", "VAR", "S_PARAM", "Infineon_Include_RF"):
            continue
        o = ads_ops._AUTO_PIN_OFFS.get(_ms(s["name"]),
                                       ads_ops._AUTO_PIN_OFFS_DEFAULT)
        for lbl in o:
            key = (s["name"], str(lbl))
            pin_pt[key] = pin_xy(s["name"], lbl)
            find(key)
    for c in case.CONNECTIONS:
        union((str(c["a"][0]), str(c["a"][1])), (str(c["b"][0]), str(c["b"][1])))

    def seg_box(p, q):
        return (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))

    def pin_on_seg(pt, p, q, tol=0.02):
        if abs(p[0] - q[0]) < 1e-6:
            return (abs(pt[0] - p[0]) < tol and
                    min(p[1], q[1]) - tol <= pt[1] <= max(p[1], q[1]) + tol)
        if abs(p[1] - q[1]) < 1e-6:
            return (abs(pt[1] - p[1]) < tol and
                    min(p[0], q[0]) - tol <= pt[0] <= max(p[0], q[0]) + tol)
        return False

    # 1) 导线 × 文字区
    for p, q, ci in all_segs:
        sb = seg_box(p, q)
        for n in names:
            z = zones[n]
            if not z:
                continue
            zx = (z[0] + 0.02, z[1] + 0.02, z[2] - 0.02, z[3] - 0.02)
            if overlap(sb, zx, pad=0.0):
                problems.append("线%s->%s[c%d] 穿 %s 文字区 %s" % (
                    p, q, ci, n, tuple(round(v, 2) for v in z)))

    # 2) 文字区互压 / 3) 文字区 × 符号体(区按 0.1 收缩消模型保守边)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if zones[a] and zones[b] and overlap(zones[a], zones[b]):
                problems.append("文字互压 %s × %s" % (a, b))
    for n in names:
        if not zones[n]:
            continue
        zs = (zones[n][0] + 0.1, zones[n][1] + 0.1, zones[n][2] - 0.1, zones[n][3] - 0.1)
        for m2 in names:
            bb = bodies[m2]
            if bb and overlap(zs, bb):
                problems.append("文字 %s 压符号体 %s" % (n, m2))

    # 4) 导线 × 符号体(端点引脚豁免 + 同网引脚 T 搭豁免)
    for p, q, ci in all_segs:
        sb = seg_box(p, q)
        conn = CONNECTIONS[ci]
        ep = [pin_xy(conn["a"][0], conn["a"][1]), pin_xy(conn["b"][0], conn["b"][1])]
        ep_keys = [(str(conn["a"][0]), str(conn["a"][1])),
                   (str(conn["b"][0]), str(conn["b"][1]))]
        for n in names:
            bb = bodies[n]
            if not bb or not overlap(sb, bb, pad=-0.01):
                continue
            if any(abs(px - min(max(px, bb[0]), bb[2])) < 0.02 and
                   abs(py - min(max(py, bb[1]), bb[3])) < 0.02 for px, py in ep):
                continue
            tap = False
            for key, pt in pin_pt.items():
                if find(key) != find(ep_keys[0]) and find(key) != find(ep_keys[1]):
                    continue
                if _ms(key[0]) != _ms(n):
                    continue
                if pin_on_seg(pt, p, q):
                    tap = True
                    break
            if tap:
                continue
            problems.append("线%s->%s[c%d] 穿符号体 %s" % (p, q, ci, n))

    print("== 指标 ==")
    rf = [n for n in names if n not in ("INC1", "SP1", "VAR1")]
    rx = [PLACED[n][0] for n in rf]
    ry = [PLACED[n][1] for n in rf]
    tot = sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q, _ in all_segs)
    print("主电路 bbox x[%.2f,%.2f] y[%.2f,%.2f]" % (min(rx), max(rx), min(ry), max(ry)))
    print("规划导线总长 %.2f 格, 段数 %d" % (tot, len(all_segs)))
    print("== 问题 %d ==" % len(problems))
    for pmsg in problems:
        print("  " + pmsg)

    if not problems and "--emit" in sys.argv:
        out = {
            "library": "BFR106_lib", "cell": "FM_SC", "view": "schematic",
            "recreate": True, "instances": [], "connections": conns,
            "var": case.VAR_SPEC,
        }
        for s in case.INSTANCES:
            x, y, ang, annot = PLACED[s["name"]]
            row = {"master": s["master"], "name": s["name"], "x": x, "y": y}
            if s.get("params"):
                row["params"] = s["params"]
            if ang is not None:
                row["angle"] = ang
            if annot:
                row["annot"] = list(annot)
            out["instances"].append(row)
        path = os.path.join(HERE, "bfr_rebuild_args.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("args ->", path)


if __name__ == "__main__":
    main()
