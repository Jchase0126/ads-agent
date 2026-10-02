"""导出设计当前全部 wire（设计坐标 bbox）到 JSON，供检查器拟合 PDF 映射。"""
import json

import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode

d = db_uu.open_design('AI_lib:Wilkinson_5G9:schematic', mode=DesignMode.READ_ONLY)
try:
    wires = []
    for s in d.shapes:
        b = s.bbox
        x1, y1 = b.lower_left.x, b.upper_right.y
        x2, y2 = b.upper_right.x, b.lower_left.y
        wires.append([round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)])
    print(json.dumps(wires))
finally:
    d.close_design()
