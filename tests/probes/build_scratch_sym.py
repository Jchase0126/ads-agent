# -*- coding: utf-8 -*-
"""实机验证对称分支泛化：建两级/1分3 scratch cell，打印几何报告并渲染。"""
import json

import ads_ops

import importlib
importlib.reload(ads_ops)

VAR = {"name": "VAR1", "values": {
    "X": "1.0", "W50": "38.2 mil", "L50": "100 mil", "W70": "20.3 mil",
    "L70": "287 mil", "Riso": "100 Ohm", "Z0": "50 Ohm", "f0": "5.9 GHz"}}

CONN_2STAGE = [
    {"a": ["P1", 1], "b": ["ML1", 1]},
    {"a": ["ML1", 2], "b": ["ML2a", 1]},
    {"a": ["ML1", 2], "b": ["ML2b", 1]},
    {"a": ["ML2a", 2], "b": ["ML3a", 1]},
    {"a": ["ML2b", 2], "b": ["ML3b", 1]},
    {"a": ["ML3a", 2], "b": ["P2", 1]},
    {"a": ["ML3b", 2], "b": ["P3", 1]},
    {"a": ["ML2a", 2], "b": ["R1", 2]},
    {"a": ["ML2b", 2], "b": ["R1", 1]},
    {"a": ["ML3a", 2], "b": ["R2", 2]},
    {"a": ["ML3b", 2], "b": ["R2", 1]},
]
INST_2STAGE = [
    {"master": "ads_simulation:TermG", "name": "P1", "params": {"Num": "1", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "P2", "params": {"Num": "2", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "P3", "params": {"Num": "3", "Z": "50 Ohm"}},
    {"master": "ads_tlines:MLIN", "name": "ML1", "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}},
    {"master": "ads_tlines:MLIN", "name": "ML2a", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_tlines:MLIN", "name": "ML2b", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_tlines:MLIN", "name": "ML3a", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_tlines:MLIN", "name": "ML3b", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "Riso"}},
    {"master": "ads_rflib:R", "name": "R2", "params": {"R": "Riso"}},
    {"master": "ads_tlines:MSUB", "name": "MSUB1",
     "params": {"H": "20 mil", "Er": "4.4", "T": "1.4 mil", "TanD": "0.02", "Cond": "1.0E+50"}},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "5.4 GHz", "Stop": "6.4 GHz", "Step": "0.01 GHz"}},
]

CONN_3WAY = [
    {"a": ["P1", 1], "b": ["ML1", 1]},
    {"a": ["ML1", 2], "b": ["ML2a", 1]},
    {"a": ["ML1", 2], "b": ["ML2b", 1]},
    {"a": ["ML1", 2], "b": ["ML2c", 1]},
    {"a": ["ML2a", 2], "b": ["P2", 1]},
    {"a": ["ML2b", 2], "b": ["P3", 1]},
    {"a": ["ML2c", 2], "b": ["P4", 1]},
]
INST_3WAY = [
    {"master": "ads_simulation:TermG", "name": "P1", "params": {"Num": "1", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "P2", "params": {"Num": "2", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "P3", "params": {"Num": "3", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "P4", "params": {"Num": "4", "Z": "50 Ohm"}},
    {"master": "ads_tlines:MLIN", "name": "ML1", "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}},
    {"master": "ads_tlines:MLIN", "name": "ML2a", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_tlines:MLIN", "name": "ML2b", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_tlines:MLIN", "name": "ML2c", "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
    {"master": "ads_tlines:MSUB", "name": "MSUB1",
     "params": {"H": "20 mil", "Er": "4.4", "T": "1.4 mil", "TanD": "0.02", "Cond": "1.0E+50"}},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "5.4 GHz", "Stop": "6.4 GHz", "Step": "0.01 GHz"}},
]

for cell, inst, conn in (("_scratch_sym2stage", INST_2STAGE, CONN_2STAGE),
                         ("_scratch_sym3way", INST_3WAY, CONN_3WAY)):
    res = ads_ops.build_schematic({
        "library": "AI_lib", "cell": cell, "recreate": True,
        "layout": "auto", "instances": inst, "var": VAR, "connections": conn,
    })
    geo = res.get("geometry", {})
    print(cell, "->", "段数", geo.get("n_segments"),
          "问题", len(geo.get("problems", [])),
          "警告", len(geo.get("warnings", [])),
          "未验证", len(geo.get("unverified", [])),
          "| 连线", sum(1 for c in res.get("connections", []) if c.get("status") == "ok"),
          "/", len(res.get("connections", [])))

d2 = ads_ops._open_design("AI_lib", "_scratch_sym2stage", "schematic", write=False)
d2.print_design(r"D:/Antenna/ADS Agent/logs/scratch_2stage.pdf")
print("渲染 ok")
