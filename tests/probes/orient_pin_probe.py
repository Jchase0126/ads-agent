# -*- coding: utf-8 -*-
"""补测 AI_annot_probe:各实例 pin snap_point + 变换属性名,然后完成删除。"""
import json

import ads_ops

LIB, CELL = "BFR106_lib", "AI_annot_probe"
out = {}
try:
    d = ads_ops._open_design(LIB, CELL, "schematic", write=False)
    try:
        for inst in list(d.instances):
            nm = ads_ops._inst_name(inst)
            pins = []
            for p in list(inst.inst_pins):
                sp = p.snap_point
                pins.append([round(float(sp.x), 3), round(float(sp.y), 3)])
            attrs = [a for a in dir(inst) if not a.startswith("_")
                     and any(k in a.lower() for k in ("angle", "orient", "mirror",
                                                      "reflect", "transform",
                                                      "origin"))]
            attr_vals = {}
            for a in attrs:
                try:
                    attr_vals[a] = repr(getattr(inst, a))[:80]
                except Exception as e:
                    attr_vals[a] = f"ERR {e}"
            out[nm] = {"pins": pins, "attrs": attr_vals}
    finally:
        ads_ops._close_design(d)
except Exception as e:
    out["_open_error"] = f"{type(e).__name__}: {e}"

# 删除 scratch cell(用 db_uu 的库接口)
cleanup = {}
try:
    db_uu = ads_ops._db_uu()
    lib_names = [a for a in dir(db_uu) if not a.startswith("_")]
    cleanup["db_uu_attrs_sample"] = lib_names[:30]
except Exception as e:
    cleanup["err"] = str(e)
out["_cleanup_probe"] = cleanup

print("===ORIENT2===")
print(json.dumps(out, ensure_ascii=False))
