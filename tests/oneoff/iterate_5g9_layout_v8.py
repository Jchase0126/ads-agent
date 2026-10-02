"""Wilkinson_5G9 布局迭代 v8：顶部收紧（2026-09-27）。

在 v7 基础上（电气仍零变化）：
  VAR1 6.562→5.762、MSub1 5.319→4.519（各 -0.8）：顶部空白收紧，
  MSUB 文字底 2.80→2.00（与 MTEE 文字顶 1.45 间隙 0.55），
  设计总高顶从 6.65 降到 5.85。

运行:: python tests/iterate_5g9_layout_v8.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from call_tool import call  # noqa: E402

LIBRARY, CELL = "AI_lib", "Wilkinson_5G9"

SPEC = {
    "library": LIBRARY,
    "cell": CELL,
    "recreate": True,
    "instances": [
        # 主干 y=0：输入在左，信号从左到右
        {"master": "ads_simulation:Term", "name": "PORT1", "x": 0, "y": 0,
         "params": {"Num": "1", "Z": "Z0"}},
        {"master": "ads_tlines:MLIN", "name": "ML1", "x": 2.5, "y": 0,
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE1", "x": 5, "y": 0,
         "params": {"Subst": "MSub1", "W1": "W50", "W2": "W70", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML2", "x": 7.5, "y": 0,
         "params": {"Subst": "MSub1", "W": "W70", "L": "Lq"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE2", "x": 10, "y": 0,
         "params": {"Subst": "MSub1", "W1": "W70", "W2": "W50", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML4", "x": 12.5, "y": 0,
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_simulation:Term", "name": "PORT2", "x": 15.5, "y": 0, "angle": 180,
         "params": {"Num": "2", "Z": "Z0"}},
        # 下支路 y=-4：从 MTEE2 分叉竖直落下，从左到右到 PORT3
        {"master": "ads_tlines:MLIN", "name": "ML3", "x": 10.5, "y": -4,
         "params": {"Subst": "MSub1", "W": "W70", "L": "Lq"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE3", "x": 12, "y": -4,
         "params": {"Subst": "MSub1", "W1": "W70", "W2": "W50", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML5", "x": 13.5, "y": -4,
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_simulation:Term", "name": "PORT3", "x": 16.5, "y": -4, "angle": 180,
         "params": {"Num": "3", "Z": "Z0"}},
        # 隔离电阻：两连接结点中点 x=10.5，行间竖放
        {"master": "ads_rflib:R", "name": "R1", "x": 9, "y": -3, "angle": 90,
         "params": {"R": "Riso"}},
        # 端口落地
        {"master": "ads_rflib:GROUND", "name": "GND1", "x": 0, "y": -2},
        {"master": "ads_rflib:GROUND", "name": "GND2", "x": 15.5, "y": -2},
        {"master": "ads_rflib:GROUND", "name": "GND3", "x": 16.5, "y": -6},
        # 非信号件
        {"master": "ads_tlines:MSUB", "name": "MSub1", "x": 4.5, "y": 4.519,
         "params": {"H": "10.0 mil", "Er": "9.6", "Mur": "1",
                    "Cond": "1.0E+50", "T": "0 mil", "TanD": "0"}},
        {"master": "ads_simulation:S_Param", "name": "SP1", "x": 0, "y": -8.2,
         "params": {"Start": "4.5 GHz", "Stop": "7.5 GHz", "Step": "0.02 GHz"}},
    ],
    "var": {"name": "VAR1", "x": 0, "y": 5.762, "values": {
        "X": "1.0", "f0": "5.9 GHz", "Z0": "50 Ohm", "W50": "3.06 mm",
        "W70": "1.62 mm", "Lq": "7.04 mm", "L50": "6.96 mm",
        "Riso": "100 Ohm"}},
    "connections": [
        {"a": ["PORT1", 2], "b": ["ML1", 1]},
        {"a": ["ML1", 2], "b": ["MTEE1", 1]},
        {"a": ["MTEE1", 2], "b": ["ML2", 1]},
        {"a": ["ML2", 2], "b": ["MTEE2", 1]},
        {"a": ["MTEE2", 2], "b": ["ML4", 1]},
        {"a": ["ML4", 2], "b": ["PORT2", 2]},
        # 分支：MTEE2.pin3 竖直落到下支路行
        {"a": ["MTEE2", 3], "b": ["ML3", 1]},
        {"a": ["ML3", 2], "b": ["MTEE3", 1]},
        {"a": ["MTEE3", 2], "b": ["ML5", 1]},
        {"a": ["ML5", 2], "b": ["PORT3", 2]},
        # 隔离电阻：上端经 (6.5,-2) 水平引入，下端经 (10.5,-4.5) 沿支路行下方接 pin3
        {"a": ["MTEE1", 3], "b": ["R1", 2], "waypoints": [[5.5, -2]]},
        {"a": ["R1", 1], "b": ["MTEE3", 3], "waypoints": [[9, -4.5]]},
        {"a": ["PORT1", 1], "b": ["GND1", 1]},
        {"a": ["PORT2", 1], "b": ["GND2", 1]},
        {"a": ["PORT3", 1], "b": ["GND3", 1]},
    ],
}

# recreate 重放后 annotation 回默认位置，需重打偏移（相对元件，随元件移动生效）
ANNOTATION_PATCH = """
import keysight.ads.de.db_uu as db_uu
from keysight.ads.de.db_uu import DesignMode
d = db_uu.open_design('AI_lib:Wilkinson_5G9:schematic', mode=DesignMode.APPEND)
try:
    moved = []
    for inst in list(d.instances):
        if inst.name in ('MTEE1', 'MTEE2', 'MTEE3'):
            inst.move_annotation((0.0, 2.0)); moved.append(inst.name)
        elif inst.name == 'ML3':
            inst.move_annotation((0.0, 1.4)); moved.append(inst.name)
    d.save_design()
    print('annotation moved:', moved)
finally:
    d.close_design()
"""


def main():
    result = call("build_schematic", SPEC, timeout=300)
    summary = {k: result.get(k) for k in
               ("design", "backup", "n_instances", "n_nets",
                "parameters_ok", "problems")}
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    conns = result.get("connections", [])
    bad = [c for c in conns if c.get("status") != "ok"]
    print(f"CONNECTIONS {len(conns)} ok={len(conns) - len(bad)} bad={bad}")
    if result.get("problems"):
        raise SystemExit("PROBLEMS: " + str(result["problems"]))
    patch = call("run_python", {"code": ANNOTATION_PATCH}, timeout=180)
    print("PATCH:", str(patch.get("stdout", patch))[:200])


if __name__ == "__main__":
    main()
