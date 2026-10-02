
import ads_ops
import importlib
importlib.reload(ads_ops)

inst = [
    {"master": "ads_simulation:TermG", "name": k}
    for k in ("P1", "P2", "P3", "P4")
]
inst += [
    {"master": "ads_tlines:MLIN", "name": k,
     "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}}
    for k in ("TLv1", "TLh1", "TLh2", "TLv2")
]
inst += [
    {"master": "ads_tlines:MSUB", "name": "MSUB1",
     "params": {"H": "20 mil", "Er": "4.4", "T": "1.4 mil", "TanD": "0.02", "Cond": "1.0E+50"}},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "5.4 GHz", "Stop": "6.4 GHz", "Step": "0.01 GHz"}},
]
conn = [
    {"a": ["P1", 1], "b": ["TLv1", 1]},
    {"a": ["TLv1", 2], "b": ["P3", 1]},
    {"a": ["P3", 1], "b": ["TLh1", 1]},
    {"a": ["TLh1", 2], "b": ["P2", 1]},
    {"a": ["P1", 1], "b": ["TLh2", 1]},
    {"a": ["TLh2", 2], "b": ["P4", 1]},
    {"a": ["P4", 1], "b": ["TLv2", 1]},
    {"a": ["TLv2", 2], "b": ["P2", 1]},
]
res = ads_ops.build_schematic({
    "library": "AI_lib", "cell": "_scratch_branched", "recreate": True,
    "layout": "auto", "instances": inst,
    "var": {"name": "VAR1", "values": {"X": "1.0"}},
    "connections": conn,
})
geo = res.get("geometry", {})
print("几何:", geo.get("n_segments"), "段, 问题", len(geo.get("problems", [])),
      "警告", len(geo.get("warnings", [])))
d = ads_ops._open_design("AI_lib", "_scratch_branched", "schematic", write=False)
import json
wires = []
for s in list(d.shapes):
    pts, exact = ads_ops._shape_polyline(s)
    if exact and len(pts) >= 2:
        wires.append([[round(x, 4), round(y, 4)] for x, y in pts])
json.dump(wires, open(r"D:/Antenna/ADS Agent/logs/branched_wires.json", "w"))
d.print_design(r"D:/Antenna/ADS Agent/logs/branched.pdf")
print("导线与渲染导出完成")
d2 = d
ads_ops._close_design(d2)
lib = ads_ops._de().get_open_library("AI_lib")
lib.delete_cell("_scratch_branched")
print("scratch 已删除")
