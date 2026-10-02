# -*- coding: utf-8 -*-
# 查 CB_FM_Amp 各实例的完整 lib:cell (2026-09-29)
import json
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
de.active_workspace()
d = db_uu.open_design("BFR106_lib:CB_FM_Amp:schematic", mode="ReadOnly")
out = {}
for inst in list(d.instances):
    lib = ""
    try:
        lib = inst.master.library.name
    except Exception as e:
        try:
            # 退路:master 路径字符串
            lib = str(inst.master)[:80]
        except Exception:
            lib = "?"
    cell = ""
    for a in ("master_name", "cell_name", "master_cell_name"):
        v = getattr(inst, a, None)
        if v:
            cell = str(v)
            break
    out[str(inst.name)] = f"{lib}:{cell}"
print(json.dumps(out, ensure_ascii=False, indent=1))
