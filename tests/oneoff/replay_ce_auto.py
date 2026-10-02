# -*- coding: utf-8 -*-
"""离线重放 auto_layout_positions 复原 CE_FM_Amp 的 16:40 自动布局(图1)。
连接清单从网表+日志前缀重建(星形 hub:端口/晶体管脚)。"""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'addon', 'ads_agent'))
import ads_ops

INSTANCES = [
    {"master": "Infineon_RF:Infineon_Include_RF", "name": "INC1"},
    {"master": "Infineon_RF:BFR106", "name": "Q1"},
    {"master": "ads_sources:V_DC", "name": "VCC", "params": {"Vdc": "5 V"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "1.5 kOhm"}},
    {"master": "ads_rflib:R", "name": "R2", "params": {"R": "620 Ohm"}},
    {"master": "ads_rflib:R", "name": "RE1", "params": {"R": "62 Ohm"}},
    {"master": "ads_rflib:C", "name": "CE", "params": {"C": "10 nF"}},
    {"master": "ads_rflib:L", "name": "LC", "params": {"L": "2.2 uH"}},
    {"master": "ads_rflib:C", "name": "CDCIN", "params": {"C": "1 nF"}},
    {"master": "ads_rflib:C", "name": "CDCOUT", "params": {"C": "1 nF"}},
    {"master": "ads_rflib:C", "name": "CIN", "params": {"C": "24 pF"}},
    {"master": "ads_rflib:L", "name": "LIN", "params": {"L": "100 nH"}},
    {"master": "ads_rflib:C", "name": "COUT", "params": {"C": "3.9 pF"}},
    {"master": "ads_rflib:L", "name": "LOUT", "params": {"L": "33 nH"}},
    {"master": "ads_rflib:R", "name": "RD", "params": {"R": "51 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "PORT1", "params": {"Num": "1", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "PORT2", "params": {"Num": "2", "Z": "50 Ohm"}},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "70 MHz", "Stop": "130 MHz", "Step": "0.5 MHz"}},
] + [{"master": "ads_rflib:GROUND", "name": f"G{i}"} for i in range(1, 8)]

C = lambda a, b: {"a": [a[0], str(a[1])], "b": [b[0], str(b[1])]}
CONNECTIONS = [
    C(("PORT1", 1), ("CIN", 1)), C(("CIN", 2), ("G1", 1)),
    C(("PORT1", 1), ("LIN", 1)), C(("LIN", 2), ("CDCIN", 1)),
    C(("CDCIN", 2), ("Q1", 2)),
    C(("Q1", 2), ("R1", 1)), C(("R1", 2), ("VCC", 1)),
    C(("Q1", 2), ("R2", 1)), C(("R2", 2), ("G2", 1)),
    C(("Q1", 3), ("RE1", 1)), C(("RE1", 2), ("G3", 1)),
    C(("Q1", 3), ("CE", 1)), C(("CE", 2), ("G4", 1)),
    C(("Q1", 1), ("CDCOUT", 1)), C(("Q1", 1), ("LC", 1)),
    C(("LC", 2), ("VCC", 1)), C(("VCC", 2), ("G5", 1)),
    C(("CDCOUT", 2), ("COUT", 1)), C(("COUT", 2), ("G6", 1)),
    C(("CDCOUT", 2), ("RD", 1)), C(("RD", 2), ("G7", 1)),
    C(("CDCOUT", 2), ("LOUT", 1)), C(("LOUT", 2), ("PORT2", 1)),
]

placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
print("=== 自动布局(图1) 重放结果 ===")
for n, p in sorted(placed.items(), key=lambda kv: (kv[1].get('y', 0), kv[1].get('x', 0))):
    print(f"{n:8s} x={p['x']:7.3f} y={p['y']:7.3f} angle={p.get('angle')}")
json.dump({"placed": placed}, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'probes', 'ce_auto_replay.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

# 规划导线
routes = ads_ops.plan_wire_routes(INSTANCES, CONNECTIONS, placed)
print()
print("=== 规划导线 ===")
for i in sorted(routes, key=int):
    row = routes[i]
    wp, tag = row[0], row[1]
    skip = row[2] if len(row) > 2 else []
    a, b = CONNECTIONS[int(i)]['a'], CONNECTIONS[int(i)]['b']
    wp_s = "BIND_ONLY" if wp is None else         [(round(x, 2), round(y, 2)) for x, y in wp]
    print("[%2d] %s.%s->%s.%s wp=%s skip=%s net=%s" % (
        int(i), a[0], a[1], b[0], b[1], wp_s, skip, tag))
open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  'probes', 'ce_auto_replay.json'), 'w',
     encoding='utf-8').write(json.dumps({"placed": placed},
                                        ensure_ascii=False, indent=1))
