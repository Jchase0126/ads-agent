# -*- coding: utf-8 -*-
"""导出新布局的导线清单(设计坐标) + print_design 渲染 PDF。"""
import json
import os

import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

OUT_DIR = r"D:\Antenna\ADS Agent\logs\bfr_layout_20261003"
PDF = os.path.join(OUT_DIR, "FM_SC_after.pdf")
if os.path.exists(PDF):
    os.remove(PDF)          # print_design 拒绝覆盖已有文件

d = db_uu.open_design("BFR106_lib:FM_SC:schematic", mode=DesignMode.READ_ONLY)

wires = []
for sh in d.shapes:
    try:
        ol = sh.get_outline()
        pts = [[round(float(p.x), 3), round(float(p.y), 3)] for p in list(ol.points)]
    except Exception:
        continue
    if len(pts) >= 2:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        wires.append([min(xs), min(ys), max(xs), max(ys)])

with open(os.path.join(OUT_DIR, "FM_SC_after_wires.json"), "w", encoding="utf-8") as f:
    json.dump(wires, f)

d.print_design(PDF)
print("wires:", len(wires), " pdf:", os.path.exists(PDF))
print("done")
