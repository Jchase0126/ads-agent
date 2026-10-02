# -*- coding: utf-8 -*-
"""FM_SC 案例离线重放:当前 auto_layout_positions + plan_wire_routes 的落位
与图面指标,用于定位 document1.pdf 分散形态的成因(2026-09-30)。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (os.path.join(ROOT, "tests"), os.path.join(ROOT, "backend"),
          os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ads_ops  # noqa: E402
import fm_sc_case as case  # noqa: E402


def rough_bbox(placed, names):
    xs, ys = [], []
    for n in names:
        p = placed[n]
        b = ads_ops._rough_box_public(
            {"master": "ads_rflib:R", "angle": p.get("angle")}, p) \
            if False else None
        xs.append(p["x"]); ys.append(p["y"])
    return min(xs), min(ys), max(xs), max(ys)


def main():
    inst = [dict(s) for s in case.INSTANCES]
    placed = ads_ops.auto_layout_positions(inst, case.CONNECTIONS,
                                           dict(case.VAR_SPEC))
    specs = {s["name"]: s for s in case.INSTANCES}

    def kind(n):
        m = ads_ops._master_short(specs[n]["master"])
        if m in ads_ops._AUTO_GND_MASTERS:
            return "gnd"
        if m in ads_ops._AUTO_ANNOT_MASTERS or "INCLUDE" in m.upper():
            return "annot"
        return "rf"

    specs["VAR1"] = {"master": "ads_datacmps:VAR"}
    rf = [n for n in placed if kind(n) == "rf"]
    gnd = [n for n in placed if kind(n) == "gnd"]
    ann = [n for n in placed if kind(n) == "annot"]

    print("== 实例坐标(按 x 排序) ==")
    for n in sorted(placed, key=lambda k: (placed[k]["y"], placed[k]["x"])):
        p = placed[n]
        print(f"  {n:8s} {kind(n):5s} x={p['x']:8.3f} y={p['y']:8.3f} "
              f"angle={p.get('angle')} annot={p.get('annot')}")

    for label, names in (("主电路(rf)", rf), ("rf+gnd", rf + gnd),
                         ("全图(含 VAR/控制器)", list(placed))):
        x0, y0 = min(placed[n]["x"] for n in names), min(placed[n]["y"] for n in names)
        x1, y1 = max(placed[n]["x"] for n in names), max(placed[n]["y"] for n in names)
        print(f"bbox {label:22s} w={x1-x0:7.2f} h={y1-y0:7.2f} "
              f"area={(x1-x0)*(y1-y0):8.1f}  x[{x0:.2f},{x1:.2f}] y[{y0:.2f},{y1:.2f}]")

    routes = ads_ops.plan_wire_routes(inst, case.CONNECTIONS, placed)
    tot = 0.0
    longest = 0.0
    longest_info = None
    for i, conn in enumerate(case.CONNECTIONS):
        m = routes.get(i)
        if not m or m[0] is None:
            continue
        pts = ([ (placed[str(conn['a'][0])]['x'], placed[str(conn['a'][0])]['y']) ]
               + [tuple(w) for w in m[0]]
               + [ (placed[str(conn['b'][0])]['x'], placed[str(conn['b'][0])]['y']) ])
        # 用真实引脚坐标重算(粗略:上面用 origin 不准,这里仅示意)
        L = sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b in zip(pts, pts[1:]))
        tot += L
        if L > longest:
            longest, longest_info = L, (conn["a"], conn["b"], len(m[0]))
    print(f"规划导线总长≈{tot:.2f}  最长单连接≈{longest:.2f}  {longest_info}")


if __name__ == "__main__":
    main()
