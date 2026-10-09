# -*- coding: utf-8 -*-
"""转储 _bfr_probe 实测:Q1@270 引脚、R2/CB@180 文字框、GND 符号盒。"""
import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

d = db_uu.open_design("BFR106_lib:_bfr_probe:schematic", mode=DesignMode.READ_ONLY)

for inst in d.instances:
    name = getattr(inst, "inst_name", None) or getattr(inst, "name", "")
    org = (round(float(inst.origin.x), 3), round(float(inst.origin.y), 3))
    pins = []
    try:
        for p in inst.inst_pins:
            sp = p.snap_point
            pins.append((str(p.inst_term.term_number),
                         round(float(sp.x), 3), round(float(sp.y), 3)))
    except Exception:
        pass
    box = None
    try:
        bb = inst.bbox
        box = [round(float(bb.lower_left.x), 3), round(float(bb.lower_left.y), 3),
               round(float(bb.upper_right.x), 3), round(float(bb.upper_right.y), 3)]
    except Exception:
        pass
    annot = None
    try:
        if getattr(inst, "has_ads_annotation", False):
            b = inst.bbox_annotation_only
            annot = [round(float(b.lower_left.x) - org[0], 3),
                     round(float(b.lower_left.y) - org[1], 3),
                     round(float(b.upper_right.x) - org[0], 3),
                     round(float(b.upper_right.y) - org[1], 3)]
    except Exception:
        pass
    print("%-4s org=%s pins=%s bbox_rel=%s annot_rel=%s" % (
        name, org, pins,
        [round(box[0] - org[0], 3), round(box[1] - org[1], 3),
         round(box[2] - org[0], 3), round(box[3] - org[1], 3)] if box else None,
        annot))
print("done")
