# -*- coding: utf-8 -*-
"""实机探针：MTEE 威尔金森走 layout=auto，导出 wires.json + 实例/引脚 + 渲染 PDF。"""
import json

import ads_ops

import importlib
importlib.reload(ads_ops)

INST = [
    {"master": "ads_simulation:Term", "name": "PORT1", "params": {"Num": "1", "Z": "Z0"}},
    {"master": "ads_tlines:MLIN", "name": "ML1", "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}},
    {"master": "ads_tlines:MTEE", "name": "MTEE1", "params": {"Subst": "MSUB1", "W1": "W50", "W2": "W70", "W3": "W50"}},
    {"master": "ads_tlines:MLIN", "name": "ML2", "params": {"Subst": "MSUB1", "W": "W70", "L": "Lq"}},
    {"master": "ads_tlines:MTEE", "name": "MTEE2", "params": {"Subst": "MSUB1", "W1": "W70", "W2": "W50", "W3": "W50"}},
    {"master": "ads_tlines:MLIN", "name": "ML4", "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}},
    {"master": "ads_simulation:Term", "name": "PORT2", "params": {"Num": "2", "Z": "Z0"}},
    {"master": "ads_tlines:MLIN", "name": "ML3", "params": {"Subst": "MSUB1", "W": "W70", "L": "Lq"}},
    {"master": "ads_tlines:MTEE", "name": "MTEE3", "params": {"Subst": "MSUB1", "W1": "W70", "W2": "W50", "W3": "W50"}},
    {"master": "ads_tlines:MLIN", "name": "ML5", "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}},
    {"master": "ads_simulation:Term", "name": "PORT3", "params": {"Num": "3", "Z": "Z0"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "Riso"}},
    {"master": "ads_rflib:GROUND", "name": "GND1"},
    {"master": "ads_rflib:GROUND", "name": "GND2"},
    {"master": "ads_rflib:GROUND", "name": "GND3"},
    {"master": "ads_tlines:MSUB", "name": "MSub1",
     "params": {"H": "10.0 mil", "Er": "9.6", "Mur": "1", "Cond": "1.0E+50", "T": "0 mil", "TanD": "0"}},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "4.5 GHz", "Stop": "7.5 GHz", "Step": "0.02 GHz"}},
]
VAR = {"name": "VAR1", "values": {
    "X": "1.0", "f0": "5.9 GHz", "Z0": "50 Ohm", "W50": "3.06 mm",
    "W70": "1.62 mm", "Lq": "7.04 mm", "L50": "6.96 mm", "Riso": "100 Ohm"}}
CONN = [
    {"a": ["PORT1", 2], "b": ["ML1", 1]},
    {"a": ["ML1", 2], "b": ["MTEE1", 1]},
    {"a": ["MTEE1", 2], "b": ["ML2", 1]},
    {"a": ["ML2", 2], "b": ["MTEE2", 1]},
    {"a": ["MTEE2", 2], "b": ["ML4", 1]},
    {"a": ["ML4", 2], "b": ["PORT2", 2]},
    {"a": ["MTEE2", 3], "b": ["ML3", 1]},
    {"a": ["ML3", 2], "b": ["MTEE3", 1]},
    {"a": ["MTEE3", 2], "b": ["ML5", 1]},
    {"a": ["ML5", 2], "b": ["PORT3", 2]},
    {"a": ["MTEE1", 3], "b": ["R1", 2]},
    {"a": ["R1", 1], "b": ["MTEE3", 3]},
    {"a": ["PORT1", 1], "b": ["GND1", 1]},
    {"a": ["PORT2", 1], "b": ["GND2", 1]},
    {"a": ["PORT3", 1], "b": ["GND3", 1]},
]

res = ads_ops.build_schematic({
    "library": "AI_lib", "cell": "_scratch_mtee", "recreate": True,
    "layout": "auto", "instances": INST, "var": VAR, "connections": CONN,
})
geo = res.get("geometry", {})
print("几何:", geo.get("n_segments"), "段, 问题", len(geo.get("problems", [])),
      "警告", len(geo.get("warnings", [])), "未验证", len(geo.get("unverified", [])))
for p in geo.get("problems", [])[:6]:
    print("  P:", p)
for w in geo.get("warnings", [])[:6]:
    print("  W:", w)
print("连线:", sum(1 for c in res.get("connections", []) if c.get("status") == "ok"),
      "/", len(res.get("connections", [])))

d = ads_ops._open_design("AI_lib", "_scratch_mtee", "schematic", write=False)
wires = []
for s in list(d.shapes):
    pts, exact = ads_ops._shape_polyline(s)
    if exact and len(pts) >= 2:
        wires.append([[round(x, 4), round(y, 4)] for x, y in pts])
json.dump(wires, open(r"D:/Antenna/ADS Agent/logs/mtee_wires.json", "w"))
print("wires.json:", len(wires), "条")

print("== 实例 bbox ==")
for inst in list(d.instances):
    try:
        b = inst.bbox
        lo, hi = b.lower_left, b.upper_right
        print(ads_ops._inst_name(inst), "|", ads_ops._inst_master(inst), "|",
              (round(float(lo.x), 3), round(float(lo.y), 3),
               round(float(hi.x), 3), round(float(hi.y), 3)))
        for p in list(getattr(inst, "inst_pins", []) or []):
            sp = ads_ops._xy_point(p.snap_point)
            print("    pin", ads_ops._pin_label(p), "@",
                  (round(sp[0], 3), round(sp[1], 3)))
    except Exception as e:  # noqa: BLE001
        print("??", type(e).__name__, e)

d.print_design(r"D:/Antenna/ADS Agent/logs/mtee_auto.pdf")
print("渲染 ok")
