# -*- coding: utf-8 -*-
# 回归案例基线导出:CB_FM_Amp 实例/引脚/导线/网络 (2026-09-29)
import json
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
ws = de.active_workspace()
d = db_uu.open_design("BFR106_lib:CB_FM_Amp:schematic", mode="ReadOnly")
out = {"instances": [], "wires": [], "nets": []}
for inst in list(d.instances):
    try:
        master = "%s:%s" % (inst.master.library.name, inst.master.name)
    except Exception as e:
        master = "?%s" % e
    x, y = float(inst.origin.x), float(inst.origin.y)
    try:
        ang = int(inst.angle)
    except Exception:
        ang = 0
    try:
        mir = bool(inst.mirror)
    except Exception:
        mir = False
    params = {}
    try:
        for p in inst.parameters:
            params[str(p.name)] = str(p.value)
    except Exception:
        pass
    pins = []
    try:
        for pin in inst.pins:
            sp = pin.snap_point
            lbl = getattr(pin, "label", None) or getattr(pin, "name", "?")
            pins.append({"label": str(lbl), "x": float(sp.x), "y": float(sp.y)})
    except Exception as e:
        pins = [{"err": str(e)}]
    try:
        bb = inst.bbox
        box = [float(bb.lower_left.x), float(bb.lower_left.y),
               float(bb.upper_right.x), float(bb.upper_right.y)]
    except Exception:
        box = None
    out["instances"].append({"name": inst.name, "master": master,
                             "origin": [x, y], "angle": ang, "mirror": mir,
                             "params": params, "pins": pins, "bbox": box})
for sh in list(d.shapes):
    try:
        ol = sh.get_outline()
        pts = [[float(p.x), float(p.y)] for p in list(ol.points)]
    except Exception:
        try:
            bb = sh.bbox
            pts = [[float(bb.lower_left.x), float(bb.lower_left.y)],
                   [float(bb.upper_right.x), float(bb.upper_right.y)]]
        except Exception:
            continue
    out["wires"].append(pts)
try:
    for net in list(d.nets):
        out["nets"].append({"name": str(net.name),
                            "nodes": [str(nd) for nd in list(net.nodes)]})
except Exception as e:
    out["nets_err"] = str(e)
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
