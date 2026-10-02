"""Wilkinson_5G9 布局迭代 v2：显式坐标按信号流重排（2026-09-27）。

对现状（auto_layout 等距两行）的三处结构调整，电气连接/参数/元件完全不变：
  1. 隔离电阻 R1 从 MTEE1 正下方 (6.5,-3) 移到两连接结点中间 (9.5,-3)
     （MTEE1.pin3 x=6.5 与 MTEE3.pin3 x=14.5 的中点附近），
     原 9.5 格的底部横穿线缩短为上 3 格 + 下 5 格两段，且不再横跨全图。
  2. 下支路整体左移收紧：MTEE3 15.5→14、ML5 18.5→15.5、PORT3 22.5→19、
     GND3 22.5→19；ML3 改横放 (12.5,-5)，λ/4 线与支路流向一致。
  3. 主干行不动（PORT1→ML1→MTEE1→ML2→MTEE2→ML4→PORT2 本已对齐短直连）。

运行:: python tests/iterate_5g9_layout_v2.py
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
        {"master": "ads_tlines:MLIN", "name": "ML1", "x": 3, "y": 0,
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE1", "x": 6, "y": 0,
         "params": {"Subst": "MSub1", "W1": "W50", "W2": "W70", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML2", "x": 9, "y": 0,
         "params": {"Subst": "MSub1", "W": "W70", "L": "Lq"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE2", "x": 12, "y": 0,
         "params": {"Subst": "MSub1", "W1": "W70", "W2": "W50", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML4", "x": 15, "y": 0,
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_simulation:Term", "name": "PORT2", "x": 19, "y": 0, "angle": 180,
         "params": {"Num": "2", "Z": "Z0"}},
        # 下支路 y=-5：从 MTEE2 分叉竖直落下，从左到右到 PORT3
        {"master": "ads_tlines:MLIN", "name": "ML3", "x": 12.5, "y": -5,
         "params": {"Subst": "MSub1", "W": "W70", "L": "Lq"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE3", "x": 14, "y": -5,
         "params": {"Subst": "MSub1", "W1": "W70", "W2": "W50", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML5", "x": 15.5, "y": -5,
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_simulation:Term", "name": "PORT3", "x": 19, "y": -5, "angle": 180,
         "params": {"Num": "3", "Z": "Z0"}},
        # 隔离电阻：两连接结点（MTEE1.pin3 与 MTEE3.pin3）之间，行间竖放
        {"master": "ads_rflib:R", "name": "R1", "x": 9.5, "y": -3, "angle": 90,
         "params": {"R": "Riso"}},
        # 端口落地
        {"master": "ads_rflib:GROUND", "name": "GND1", "x": 0, "y": -2},
        {"master": "ads_rflib:GROUND", "name": "GND2", "x": 19, "y": -2},
        {"master": "ads_rflib:GROUND", "name": "GND3", "x": 19, "y": -7},
        # 非信号件
        {"master": "ads_tlines:MSUB", "name": "MSub1", "x": 4.5, "y": 5.319,
         "params": {"H": "10.0 mil", "Er": "9.6", "Mur": "1",
                    "Cond": "1.0E+50", "T": "0 mil", "TanD": "0"}},
        {"master": "ads_simulation:S_Param", "name": "SP1", "x": 0, "y": -10.2,
         "params": {"Start": "4.5 GHz", "Stop": "7.5 GHz", "Step": "0.02 GHz"}},
    ],
    "var": {"name": "VAR1", "x": 0, "y": 6.562, "values": {
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
        # 隔离电阻：上端经 (6.5,-2) 水平引入，下端经 (9.5,-5.5) 沿支路行下方接 pin3
        {"a": ["MTEE1", 3], "b": ["R1", 2], "waypoints": [[6.5, -2]]},
        {"a": ["R1", 1], "b": ["MTEE3", 3], "waypoints": [[9.5, -5.5]]},
        {"a": ["PORT1", 1], "b": ["GND1", 1]},
        {"a": ["PORT2", 1], "b": ["GND2", 1]},
        {"a": ["PORT3", 1], "b": ["GND3", 1]},
    ],
}


def main():
    result = call("build_schematic", SPEC, timeout=300)
    summary = {k: result.get(k) for k in
               ("design", "backup", "n_instances", "n_nets",
                "parameters_ok", "problems", "warnings")}
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    conns = result.get("connections", [])
    bad = [c for c in conns if c.get("status") != "ok"]
    print(f"CONNECTIONS {len(conns)} ok={len(conns) - len(bad)} bad={bad}")
    if result.get("problems"):
        raise SystemExit("PROBLEMS: " + str(result["problems"]))


if __name__ == "__main__":
    main()
