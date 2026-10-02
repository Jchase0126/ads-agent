# -*- coding: utf-8 -*-
# 补抓:master 名 / pin label(inst_term) / GROUND bbox 朝向 (2026-09-29)
import json
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
de.active_workspace()
d = db_uu.open_design("BFR106_lib:CB_FM_Amp:schematic", mode="ReadOnly")

def master_of(inst):
    for a in ("master_name", "cell_name", "master_cell_name"):
        v = getattr(inst, a, None)
        if v:
            return str(v)
    return "?"

def pin_label(pin):
    t = getattr(pin, "inst_term", None)
    if t is None:
        return None
    try:
        return str(t.term_number if getattr(t, "is_numbered", True) else t.term_name)
    except Exception:
        return "?"

out = {"masters": {}, "pins": {}, "gnd_bbox": {}}
for inst in list(d.instances):
    name = str(inst.name)
    out["masters"][name] = master_of(inst)
    pl = []
    for p in list(inst.inst_pins):
        sp = p.snap_point
        pl.append({"label": pin_label(p), "snap": [float(sp.x), float(sp.y)]})
    out["pins"][name] = pl
    if master_of(inst).upper().startswith(("GROUND", "GND")):
        try:
            bb = inst.bbox
            out["gnd_bbox"][name] = [float(bb.lower_left.x), float(bb.lower_left.y),
                                     float(bb.upper_right.x), float(bb.upper_right.y)]
        except Exception as e:
            out["gnd_bbox"][name] = str(e)
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
