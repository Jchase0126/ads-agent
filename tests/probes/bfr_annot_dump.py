# -*- coding: utf-8 -*-
"""转储 FM_SC 全部实例的真实标注框/默认标注位(2026-10-03 只读)。"""
import json
import os

import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

OUT_DIR = r"D:\Antenna\ADS Agent\logs\bfr_layout_20261003"

d = db_uu.open_design("BFR106_lib:FM_SC:schematic", mode=DesignMode.READ_ONLY)

rows = []
for inst in d.instances:
    name = getattr(inst, "inst_name", None) or getattr(inst, "name", "")
    master = ""
    for attr in ("master_name", "cell_name", "master_cell_name"):
        v = getattr(inst, attr, None)
        if v:
            master = str(v)
            break
    org = (round(float(inst.origin.x), 3), round(float(inst.origin.y), 3))
    cur = de_o = None
    try:
        if getattr(inst, "has_ads_annotation", False):
            b = inst.bbox_annotation_only
            cur = [round(float(b.lower_left.x), 3), round(float(b.lower_left.y), 3),
                   round(float(b.upper_right.x), 3), round(float(b.upper_right.y), 3)]
    except Exception as e:
        cur = ["err", str(e)]
    try:
        g = inst.get_default_annotation_origin()
        de_o = [round(float(g.x), 3), round(float(g.y), 3)]
    except Exception as e:
        de_o = ["err", str(e)]
    try:
        a = inst.get_annotation_origin()
        ao = [round(float(a.x), 3), round(float(a.y), 3)]
    except Exception:
        ao = None
    rows.append({"name": str(name), "master": master, "origin": list(org),
                 "cur_annot_box": cur, "default_annot_origin": de_o,
                 "cur_annot_origin": ao})

with open(os.path.join(OUT_DIR, "FM_SC_annot.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)

for r in rows:
    print("%-8s org=%s cur_box=%s def_org=%s cur_org=%s" % (
        r["name"], r["origin"], r["cur_annot_box"],
        r["default_annot_origin"], r["cur_annot_origin"]))
print("done", len(rows))
