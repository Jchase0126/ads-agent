# -*- coding: utf-8 -*-
"""FM_SC_after.pdf 确定性映射渲染审计(2026-10-03)。

映射不再依赖笔画分组启发式:主信号链 y=0 在 PDF 上是唯一 8 笔画组,
设计跨度 x[0,19.4] -> PDF [min_x,max_x] 解 K/X0,Y0=278(已人工核对),
再复用 check_render_geometry 的 extract/seg_rect_hit 做全套检查。
仅保留能对上设计真实导线的线段参与线-字核对(符号自体边框剔除)。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check_render_geometry as crg  # noqa: E402
import pymupdf  # noqa: E402

PDF = os.path.join(HERE, "..", "logs", "bfr_layout_20261003", "FM_SC_after.pdf")
WIRES = os.path.join(HERE, "..", "logs", "bfr_layout_20261003",
                     "FM_SC_after_wires.json")

page = pymupdf.open(PDF)[0]
strokes = []
for d in page.get_drawings():
    for item in d["items"]:
        if item[0] == "l":
            p1, p2 = item[1], item[2]
            if abs(p1.y - p2.y) < 0.3 and abs(p1.x - p2.x) > 20:
                strokes.append((p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
from collections import defaultdict  # noqa: E402
g = defaultdict(list)
for y, a, b in strokes:
    g[round(y)].append((a, b, y))
main_group = max(g.values(), key=len)
xs0 = min(t[0] for t in main_group)
xs1 = max(t[1] for t in main_group)
K = (xs1 - xs0) / 19.4
X0 = xs0
Y0 = main_group[0][2]
print("mapping: K=%.4f X0=%.2f Y0=%.2f (主链组 %d 笔画)" % (
    K, X0, Y0, len(main_group)))

crg.K, crg.X0, crg.Y0 = K, X0, Y0
wires, diagonal, texts = crg.extract(PDF)

wires_des = json.load(open(WIRES, encoding="utf-8"))


def on_design_wire(seg):
    (x1, y1), (x2, y2) = seg
    for x0, ya, x1d, yb in wires_des:
        if abs(ya - yb) < 1e-6:  # 水平
            if (abs(y1 - ya) < 0.06 and abs(y2 - ya) < 0.06 and
                    min(x1, x2) >= min(x0, x1d) - 0.08 and
                    max(x1, x2) <= max(x0, x1d) + 0.08):
                return True
        else:
            if (abs(x1 - x0) < 0.06 and abs(x2 - x0) < 0.06 and
                    min(y1, y2) >= min(ya, yb) - 0.08 and
                    max(y1, y2) <= max(ya, yb) + 0.08):
                return True
    return False


real = [w for w in wires if on_design_wire(w)]
dropped = len(wires) - len(real)
print("解码线段 %d, 对上设计导线 %d, 剔除符号自体线 %d" % (
    len(wires), len(real), dropped))
res = [w for w in wires if not on_design_wire(w)]
for (a, b) in res[:12]:
    print("  剔除示例: (%.2f,%.2f)->(%.2f,%.2f) len=%.2f" % (
        a[0], a[1], b[0], b[1], abs(a[0] - b[0]) + abs(a[1] - b[1])))

bad = 0
for a, b, c in diagonal:
    if c not in ((0, 0, 0), None):
        continue
    print("  [斜线] (%.2f,%.2f)->(%.2f,%.2f)" % (a[0], a[1], b[0], b[1]))
    bad += 1

hits = []
for w in real:
    for t in texts:
        if crg.seg_rect_hit(w, t):
            hits.append(("CROSS", w, t))
for kind, w, t in hits:
    (x1, y1), (x2, y2) = w
    print("  [%s] 线(%.2f,%.2f)-(%.2f,%.2f) vs 文字[%s] x[%.2f,%.2f] y[%.2f,%.2f]" % (
        kind, x1, y1, x2, y2, t[4], t[0], t[2], t[1], t[3]))
    bad += 1

# 文字-文字重叠
for i in range(len(texts)):
    for j in range(i + 1, len(texts)):
        a, b = texts[i], texts[j]
        ox = min(a[2], b[2]) - max(a[0], b[0])
        oy = min(a[3], b[3]) - max(a[1], b[1])
        if ox > 0.02 and oy > 0.02:
            print("  [文字重叠] [%s] 与 [%s] (交叠 %.2fx%.2f) @x[%.2f,%.2f] y[%.2f,%.2f]" % (
                a[4], b[4], ox, oy, min(a[0], b[0]), max(a[2], b[2]),
                min(a[1], b[1]), max(a[3], b[3])))
            bad += 1

print("RESULT:", "FAIL" if bad else "PASS", "(%d problems)" % bad)
sys.exit(1 if bad else 0)
