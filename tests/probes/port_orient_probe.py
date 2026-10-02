# -*- coding: utf-8 -*-
"""补探:PORT1/TG 的 angle/mirror/pin 实值 + AI_annot_probe 的引脚(若还在)。"""
import json

import ads_ops

out = {}
for lib, cell in (("BFR106_lib", "CE_FM_Amp_AGENT"),
                  ("BFR106_lib", "CE_FM_Amp")):
    d = ads_ops._open_design(lib, cell, "schematic", write=False)
    rows = {}
    try:
        for inst in list(d.instances):
            nm = ads_ops._inst_name(inst)
            if nm not in ("PORT1", "PORT2"):
                continue
            try:
                ang = repr(inst.angle)
            except Exception as e:
                ang = f"ERR {e}"
            try:
                mir = repr(inst.mirror)
            except Exception as e:
                mir = f"ERR {e}"
            pins = []
            for p in list(inst.inst_pins):
                sp = p.snap_point
                pins.append((round(float(sp.x), 3), round(float(sp.y), 3)))
            bb = inst.bbox
            rows[nm] = {
                "origin": [round(float(inst.origin.x), 3),
                           round(float(inst.origin.y), 3)],
                "angle": ang, "mirror": mir,
                "bbox": [float(bb.lower_left.x), float(bb.lower_left.y),
                         float(bb.upper_right.x), float(bb.upper_right.y)],
                "pins": pins,
            }
    finally:
        ads_ops._close_design(d)
    out[f"{lib}:{cell}"] = rows

print("===PORT_ORIENT===")
print(json.dumps(out, ensure_ascii=False))
