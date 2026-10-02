# -*- coding: utf-8 -*-
"""scratch cell 方向学探测:R/C/L × angle 90/270 与 TermG 180 的默认标注 bbox。

临时 cell AI_annot_probe:放 8 个实例(不连线、不保存前备份——新建 cell 无可备份),
保存后读 bbox_annotation_only,再删除 cell 并只读确认。设计坐标:每件间隔 3 格。
"""
import json

import ads_ops

LIB, CELL = "BFR106_lib", "AI_annot_probe"
NAME = f"{LIB}:{CELL}:schematic"
CASES = [
    ("ads_rflib:R", "R90", 90, {"R": "50 Ohm"}),
    ("ads_rflib:R", "R270", 270, {"R": "50 Ohm"}),
    ("ads_rflib:C", "C90", 90, {"C": "1 pF"}),
    ("ads_rflib:C", "C270", 270, {"C": "1 pF"}),
    ("ads_rflib:L", "L90", 90, {"L": "1 nH"}),
    ("ads_rflib:L", "L270", 270, {"L": "1 nH"}),
    ("ads_simulation:TermG", "TG180", 180, {"Num": "1", "Z": "50 Ohm"}),
    ("ads_simulation:TermG", "TG0", 0, {"Num": "2", "Z": "50 Ohm"}),
    ("ads_simulation:Term", "T0", 0, {"Num": "1", "Z": "50 Ohm"}),
]

# 1) 建 cell(不存在则创建),用 APPEND 放置
ws = ads_ops._require_workspace()
ads_ops._ensure_cell_view(ws, LIB, CELL, "schematic")
import keysight.ads.de._pde.db as _pdb
db_uu = ads_ops._db_uu()
d = db_uu.open_design(NAME, mode=_pdb.DesignMode.WRITE)
try:
    for i, (master, name, ang, params) in enumerate(CASES):
        inst = d.add_instance(master, (3.0 * i, 0.0), name=name, angle=ang)
        for k, v in params.items():
            ads_ops._set_param(inst, k, v)
    d.save_design()
finally:
    ads_ops._close_design(d)

# 2) 只读重开,读符号 bbox 与标注 bbox
d2 = ads_ops._open_design(LIB, CELL, "schematic", write=False)
out = {}
try:
    for inst in list(d2.instances):
        nm = ads_ops._inst_name(inst)
        bb = inst.bbox
        sym = [float(bb.lower_left.x), float(bb.lower_left.y),
               float(bb.upper_right.x), float(bb.upper_right.y)]
        ab = None
        try:
            if getattr(inst, "has_ads_annotation", False):
                b2 = inst.bbox_annotation_only
                ab = [float(b2.lower_left.x), float(b2.lower_left.y),
                      float(b2.upper_right.x), float(b2.upper_right.y)]
        except Exception:
            ab = "ERR"
        out[nm] = {"sym": sym, "annot": ab}
finally:
    ads_ops._close_design(d2)

# 3) 删除 scratch cell 并确认
try:
    import keysight.ads.de as de
    lib = None
    for lf in de.libraries():
        if getattr(lf, "name", "") == LIB:
            lib = lf
            break
    if lib is not None:
        lib.delete_cell(CELL)
        gone = True
        try:
            ads_ops._open_design(LIB, CELL, "schematic", write=False)
            gone = False
        except Exception:
            pass
        out["_cleanup"] = {"deleted": True, "reopen_blocked": gone}
except Exception as e:
    out["_cleanup"] = {"error": f"{type(e).__name__}: {e}"}

print("===ANNOT_ORIENT_PROBE===")
print(json.dumps(out, ensure_ascii=False))
