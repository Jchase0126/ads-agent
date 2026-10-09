# -*- coding: utf-8 -*-
"""重建后只读复核:Q1 引脚朝向断言 + 网表导出 + 实例位置清点。"""
import json
import os

import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

OUT_DIR = r"D:\Antenna\ADS Agent\logs\bfr_layout_20261003"

d = db_uu.open_design("BFR106_lib:FM_SC:schematic", mode=DesignMode.READ_ONLY)

rows = []
q1 = None
for inst in d.instances:
    name = getattr(inst, "inst_name", None) or getattr(inst, "name", "")
    org = (round(float(inst.origin.x), 3), round(float(inst.origin.y), 3))
    pins = []
    try:
        for p in inst.inst_pins:
            sp = p.snap_point
            net = ""
            try:
                net = str(p.net.name) if p.net is not None else ""
            except Exception:
                net = "?"
            pins.append({"n": str(p.inst_term.term_number),
                         "x": round(float(sp.x), 3), "y": round(float(sp.y), 3),
                         "net": net})
    except Exception:
        pass
    rows.append({"name": str(name), "origin": list(org), "pins": pins})
    if str(name) == "Q1":
        q1 = rows[-1]

with open(os.path.join(OUT_DIR, "FM_SC_after.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)

# 断言:E(3) 在 C(1) 左侧且同一水平线,B(2) 在上方
pe = next(p for p in q1["pins"] if p["n"] == "3")
pc = next(p for p in q1["pins"] if p["n"] == "1")
pb = next(p for p in q1["pins"] if p["n"] == "2")
ok_eleft = pe["x"] < pc["x"] - 0.4
ok_line = abs(pe["y"] - pc["y"]) < 0.01
ok_bup = pb["y"] > pe["y"] + 0.3
print("Q1 pins: E=%s C=%s B=%s" % (
    (pe["x"], pe["y"]), (pc["x"], pc["y"]), (pb["x"], pb["y"])))
print("E 在 C 左侧:", ok_eleft, " E/C 同水平线:", ok_line, " B 在上方:", ok_bup)

# 网络归属断言(与改前一致:1=c 接 CDCOUT/LC,2=b 接 R1/R2/CB,3=e 接 L2/RE)
def nets_of(pin_n):
    return sorted(p["net"] for p in q1["pins"] if p["n"] == pin_n)
print("pin1 nets:", nets_of("1"), " pin2 nets:", nets_of("2"), " pin3 nets:", nets_of("3"))

nl = str(d.generate_netlist())
with open(os.path.join(OUT_DIR, "FM_SC_after.ckt"), "w", encoding="utf-8") as f:
    f.write(nl)
print("netlist_chars:", len(nl), " instances:", len(rows))
print("done")
