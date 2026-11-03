# -*- coding: utf-8 -*-
"""扫描 BFR106_lib 各设计的横放 R/C/L 实例,按引脚序判 0/180,转储真实文字框。

目的:确定 angle=180(体在 origin 左)时标注文字在 origin 哪一侧 —— 模型未
建档,基极簇 R2/CB 的避让方案依赖这个实测值。
"""
import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

CELLS = ["CB_FM_Amp", "CE_FM_Amp", "FM_V1", "FM_SE", "CB_DIAG",
         "CB_FM_Amp_v2", "CB_FM_Amp_AGENT", "CE_FM_Amp_AGENT"]

for cell in CELLS:
    try:
        d = db_uu.open_design("BFR106_lib:%s:schematic" % cell,
                              mode=DesignMode.READ_ONLY)
    except Exception as e:
        print(cell, "SKIP", e)
        continue
    found = 0
    for inst in d.instances:
        ms = ""
        for attr in ("master_name", "cell_name", "master_cell_name"):
            v = getattr(inst, attr, None)
            if v:
                ms = str(v)
                break
        if ms not in ("R", "C", "L", "V_DC"):
            continue
        try:
            pins = []
            for p in inst.inst_pins:
                sp = p.snap_point
                lbl = p.inst_term.term_number
                pins.append((str(lbl), round(float(sp.x), 3),
                             round(float(sp.y), 3)))
        except Exception:
            continue
        if len(pins) != 2:
            continue
        p1 = next((p for p in pins if p[0] == "1"), None)
        p2 = next((p for p in pins if p[0] == "2"), None)
        if not p1 or not p2 or abs(p1[2] - p2[2]) > 0.01:
            continue  # 只看水平
        side = "0" if p2[1] > p1[1] else "180"
        org = (round(float(inst.origin.x), 3), round(float(inst.origin.y), 3))
        box = None
        try:
            if getattr(inst, "has_ads_annotation", False):
                b = inst.bbox_annotation_only
                box = [round(float(b.lower_left.x), 3), round(float(b.lower_left.y), 3),
                       round(float(b.upper_right.x), 3), round(float(b.upper_right.y), 3)]
        except Exception:
            pass
        name = getattr(inst, "inst_name", None) or getattr(inst, "name", "")
        rel = None
        if box:
            rel = [round(box[0] - org[0], 3), round(box[1] - org[1], 3),
                   round(box[2] - org[0], 3), round(box[3] - org[1], 3)]
        print("%-16s %-6s %-4s org=%s pin1=%s pin2=%s rel_box=%s" % (
            cell, str(name), ms + "@" + side, org, p1[1:], p2[1:], rel))
        found += 1
    if not found:
        print(cell, "无水平 R/C/L")
print("done")
