# -*- coding: utf-8 -*-
"""只读探测:CE 两个设计的实例位置 / 符号 bbox / 真实标注 bbox / 引脚网络 / 网表。

用法: python tests/probe_via_ads.py tests/probes/ce_annot_probe.py
"""
import json

OUT = {}
for lib_cell in (("BFR106_lib", "CE_FM_Amp_AGENT"),
                 ("BFR106_lib", "CE_FM_Amp")):
    lib, cell = lib_cell
    key = f"{lib}:{cell}"
    try:
        import importlib as _imp
        _db_uu = _imp.import_module("keysight.ads.de.db_uu")
        _pdb = _imp.import_module("keysight.ads.de._pde.db")
        mode = getattr(_pdb.DesignMode, "READ_ONLY",
                       getattr(_pdb.DesignMode, "ReadOnly", None))
        name = f"{lib}:{cell}:schematic"
        d = _db_uu.open_design(name, mode=mode)
        rows = []
        try:
            for inst in list(d.instances):
                def _bb(b):
                    try:
                        return [round(float(b.lower_left.x), 3),
                                round(float(b.lower_left.y), 3),
                                round(float(b.upper_right.x), 3),
                                round(float(b.upper_right.y), 3)]
                    except Exception:
                        return None
                try:
                    origin = (round(float(inst.origin.x), 3),
                              round(float(inst.origin.y), 3))
                except Exception:
                    origin = None
                try:
                    ang = int(inst.angle)
                except Exception:
                    ang = None
                pins = []
                for p in list(inst.inst_pins):
                    try:
                        net = p.net.name if p.net else ""
                    except Exception:
                        net = "?"
                    lbl = None
                    it = getattr(p, "inst_term", None)
                    if it is not None:
                        lbl = (getattr(it, "term_number", None)
                               if getattr(it, "is_numbered", False)
                               else getattr(it, "term_name", None))
                    try:
                        sp = p.snap_point
                        xy = (round(float(sp.x), 3), round(float(sp.y), 3))
                    except Exception:
                        xy = None
                    pins.append({"lbl": str(lbl), "net": str(net), "xy": xy})
                ab = None
                try:
                    if getattr(inst, "has_ads_annotation", False):
                        ab = _bb(inst.bbox_annotation_only)
                except Exception:
                    ab = "ERR"
                params = {}
                try:
                    for prm in list(inst.parameters):
                        params[str(prm.name)] = str(prm.value)
                except Exception:
                    pass
                rows.append({
                    "name": getattr(inst, "inst_name", None) or getattr(inst, "name", ""),
                    "master": getattr(inst, "master_name", ""),
                    "origin": origin, "angle": ang,
                    "bbox": _bb(inst.bbox) if hasattr(inst, "bbox") else None,
                    "annot_bbox": ab,
                    "params": params,
                    "pins": pins,
                })
            nl = d.generate_netlist()
        finally:
            closer = getattr(d, "close_design", None) or getattr(d, "close", None)
            if callable(closer):
                closer()
        OUT[key] = {"instances": rows, "netlist": nl}
    except Exception as e:
        OUT[key] = {"error": f"{type(e).__name__}: {e}"}

print("===CE_ANNOT_PROBE===")
print(json.dumps(OUT, ensure_ascii=False, default=str))
