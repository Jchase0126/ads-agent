# -*- coding: utf-8 -*-
"""渲染 CE_FM_Amp_AGENT 为 PDF + 导出设计坐标导线段（映射拟合用）。只读。"""
import json

import ads_ops

d = ads_ops._open_design("BFR106_lib", "CE_FM_Amp_AGENT", "schematic", write=False)
pdf = r"D:/Antenna/ADS Agent/logs/ce_agent_before.pdf"
try:
    d.print_design(pdf)
    routes = ads_ops._design_wire_routes(d)
    wires = [[list(p) for p in pts] for pts, _exact in routes]
finally:
    ads_ops._close_design(d)
with open(r"D:/Antenna/ADS Agent/logs/ce_agent_before_wires.json", "w",
          encoding="utf-8") as f:
    json.dump(wires, f)
print("ok", pdf, len(wires), "wires")
