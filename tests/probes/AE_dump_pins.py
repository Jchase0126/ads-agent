# -*- coding: utf-8 -*-
# 基线补全:引脚 label/snap/net + 实例角度/mirror + 网表 (2026-09-29)
import json
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
de.active_workspace()
d = db_uu.open_design("BFR106_lib:CB_FM_Amp:schematic", mode="ReadOnly")

def xy(pt):
    return [float(pt.x), float(pt.y)]

out = {"pins": {}, "angles": {}, "nets": {}}
for inst in list(d.instances):
    name = str(inst.name)
    ang = None
    for attr in ("angle", "orient", "rotation"):
        v = getattr(inst, attr, None)
        if v is not None and not callable(v):
            try:
                ang = int(v)
                break
            except Exception:
                pass
    try:
        ang = int(str(getattr(inst, "angle", 0) or 0))
    except Exception:
        pass
    mir = bool(getattr(inst, "mirror", False))
    out["angles"][name] = {"angle": ang, "mirror": mir}
    plist = []
    for p in list(inst.inst_pins):
        lbl = None
        for a in ("label", "name", "number"):
            v = getattr(p, a, None)
            if v is not None:
                lbl = str(v)
                break
        net = getattr(p, "net", None)
        netname = None
        if net is not None:
            for a in ("name", "label"):
                v = getattr(net, a, None)
                if v:
                    netname = str(v)
                    break
        plist.append({"label": lbl, "snap": xy(p.snap_point), "net": netname})
    out["pins"][name] = plist
    for p in plist:
        if p["net"]:
            out["nets"].setdefault(p["net"], []).append(f"{name}.{p['label']}")
try:
    out["netlist_head"] = str(d.generate_netlist())[:1500]
except Exception as e:
    out["netlist_err"] = str(e)
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
