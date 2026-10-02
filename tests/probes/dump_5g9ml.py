"""只读 dump AI_lib:Wilkinson_5G9_ML 的实例、引脚、导线与几何报告。"""
import json

import ads_ops

d = ads_ops._open_design("AI_lib", "Wilkinson_5G9_ML", "schematic", write=False)

print("== instances ==")
for inst in list(d.instances):
    try:
        b = inst.bbox
        lo, hi = b.lower_left, b.upper_right
        bbox = (round(float(lo.x), 3), round(float(lo.y), 3),
                round(float(hi.x), 3), round(float(hi.y), 3))
    except Exception as e:  # noqa: BLE001
        bbox = str(e)
    print(ads_ops._inst_name(inst), "|", ads_ops._inst_master(inst), "| bbox", bbox)
    for p in list(getattr(inst, "inst_pins", []) or []):
        try:
            sp = ads_ops._xy_point(p.snap_point)
            print("    pin", ads_ops._pin_label(p), "@",
                  (round(sp[0], 3), round(sp[1], 3)),
                  "net", ads_ops._net_label(getattr(p, "net", None)))
        except Exception as e:  # noqa: BLE001
            print("    pin ?", e)

print("== shapes ==")
for s in list(d.shapes):
    pts, exact = ads_ops._shape_polyline(s)
    if pts:
        print("  ", [(round(x, 3), round(y, 3)) for x, y in pts],
              "" if exact else "(bbox近似)")

print("== geometry report ==")
rep = ads_ops._geometry_report(d)
print(json.dumps(rep, ensure_ascii=False, indent=1))

try:
    nl = d.generate_netlist()
    txt = nl if isinstance(nl, str) else str(nl)
    print("== netlist ==")
    print(txt)
except Exception as e:  # noqa: BLE001
    print("netlist 失败:", e)
