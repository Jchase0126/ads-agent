# -*- coding: utf-8 -*-
"""BFR106 布局重构前置只读转储(2026-10-03) v2 —— 引脚/master 按 ads_ops 实测写法。"""
import json
import os

import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

OUT_DIR = r"D:\Antenna\ADS Agent\logs\bfr_layout_20261003"
if not os.path.isdir(OUT_DIR):
    os.makedirs(OUT_DIR)


def _pin_label(pin):
    term = getattr(pin, "inst_term", None)
    if term is None:
        return None
    try:
        return term.term_number if getattr(term, "is_numbered", True) else term.term_name
    except Exception:
        return None


def _net_name(pin):
    net = getattr(pin, "net", None)
    if net is None:
        return ""
    v = getattr(net, "name", None)
    if v:
        return str(v)
    s = str(net)
    return s.split('"')[1] if '"' in s else s


d = db_uu.open_design("BFR106_lib:FM_SC:schematic", mode=DesignMode.READ_ONLY)

out = {"instances": [], "wires": []}
for inst in d.instances:
    master = ""
    for attr in ("master_name", "cell_name", "master_cell_name"):
        v = getattr(inst, attr, None)
        if v:
            master = str(v)
            break
    name = ""
    for attr in ("inst_name", "name"):
        v = getattr(inst, attr, None)
        if v:
            name = str(v)
            break
    pins = []
    try:
        for p in list(inst.inst_pins):
            sp = getattr(p, "snap_point", None)
            pins.append({"label": str(_pin_label(p)),
                         "x": round(float(sp.x), 3), "y": round(float(sp.y), 3),
                         "net": _net_name(p)})
    except Exception as e:
        pins = [{"err": str(e)}]
    params = {}
    try:
        if getattr(inst, "is_var_instance", False):
            params = {str(k): str(vv) for k, vv in dict(inst.vars or {}).items()}
        else:
            for pp in inst.parameters:
                params[str(pp.name)] = str(pp.value)
    except Exception:
        pass
    try:
        bb = inst.bbox
        box = [round(float(bb.lower_left.x), 3), round(float(bb.lower_left.y), 3),
               round(float(bb.upper_right.x), 3), round(float(bb.upper_right.y), 3)]
    except Exception:
        box = None
    out["instances"].append({"name": name, "master": master,
                             "origin": [round(float(inst.origin.x), 3),
                                        round(float(inst.origin.y), 3)],
                             "params": params, "pins": pins, "bbox": box})

for sh in d.shapes:
    try:
        ol = sh.get_outline()
        pts = [[round(float(p.x), 3), round(float(p.y), 3)] for p in list(ol.points)]
    except Exception:
        continue
    out["wires"].append(pts)

with open(os.path.join(OUT_DIR, "FM_SC_before.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print("instances:", len(out["instances"]), "wires:", len(out["wires"]))

for inst in out["instances"]:
    pn = ",".join("%s@(%s,%s)%s" % (p.get("label"), p.get("x"), p.get("y"),
                                    "@" + p.get("net", "") if p.get("net") else "")
                  for p in inst["pins"] if "label" in p)
    print("%-8s %-24s org=(%s,%s) bbox=%s pins: %s" % (
        inst["name"], inst["master"], inst["origin"][0], inst["origin"][1],
        inst["bbox"], pn))

print("--- wires ---")
for i, w in enumerate(out["wires"]):
    print(i, w)
print("done")
