"""按信号流自动布局重建 Wilkinson_5G9，并修正端口接地短路。

2026-09-24 用户截图反馈"布线太丑"：分支节点没对齐、R1 的连线斜穿全图、
VAR/控制器散在四角。本脚本用 build_schematic(layout="auto") 重建布局：
主干链一行从左到右、分支臂向下成行、隔离电阻竖放在行间、GROUND 落在
端口地脚正下方、VAR/MSUB/控制器自动排位，全部走线曼哈顿直角。

电气侧唯一改动（修 bug，不是布局）：原设计三个 Term 的地脚 pin1 与射频脚
pin2 被绑在同一个网络 —— 网表 Port:PORT1 N__0 N__0，端口自短路（对照已
验证的 v2：Port:PORT1 0 N__2）。重建后补 GND 实例把 pin1 接到 gnd!，
网表其余逐行不变（ML3 改 R270 只为让引脚配对保持原样：pin1=N__12 上端
进分支、pin2=N__15 下端出去）。

运行::

    python tests/repair_wilkinson_5g9_layout.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config  # noqa: E402
import tools  # noqa: E402

LIBRARY, CELL = "AI_lib", "Wilkinson_5G9"

SPEC = {
    "library": LIBRARY,
    "cell": CELL,
    "recreate": True,
    "layout": "auto",
    "instances": [
        {"master": "ads_simulation:Term", "name": "PORT1",
         "params": {"Num": "1", "Z": "Z0"}},
        {"master": "ads_tlines:MLIN", "name": "ML1",
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE1",
         "params": {"Subst": "MSub1", "W1": "W50", "W2": "W70", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML2",
         "params": {"Subst": "MSub1", "W": "W70", "L": "Lq"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE2",
         "params": {"Subst": "MSub1", "W1": "W70", "W2": "W50", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML4",
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_simulation:Term", "name": "PORT2", "angle": 180,
         "params": {"Num": "2", "Z": "Z0"}},
        {"master": "ads_tlines:MLIN", "name": "ML3", "angle": 270,
         "params": {"Subst": "MSub1", "W": "W70", "L": "Lq"}},
        {"master": "ads_tlines:MTEE", "name": "MTEE3",
         "params": {"Subst": "MSub1", "W1": "W70", "W2": "W50", "W3": "W50"}},
        {"master": "ads_tlines:MLIN", "name": "ML5",
         "params": {"Subst": "MSub1", "W": "W50", "L": "L50"}},
        {"master": "ads_simulation:Term", "name": "PORT3", "angle": 180,
         "params": {"Num": "3", "Z": "Z0"}},
        {"master": "ads_rflib:R", "name": "R1", "angle": 90,
         "params": {"R": "Riso"}},
        {"master": "ads_rflib:GROUND", "name": "GND1"},
        {"master": "ads_rflib:GROUND", "name": "GND2"},
        {"master": "ads_rflib:GROUND", "name": "GND3"},
        {"master": "ads_tlines:MSUB", "name": "MSub1",
         "params": {"H": "10.0 mil", "Er": "9.6", "Mur": "1",
                    "Cond": "1.0E+50", "T": "0 mil", "TanD": "0"}},
        {"master": "ads_simulation:S_Param", "name": "SP1",
         "params": {"Start": "4.5 GHz", "Stop": "7.5 GHz", "Step": "0.02 GHz"}},
    ],
    "var": {"name": "VAR1", "values": {
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
        {"a": ["MTEE2", 3], "b": ["ML3", 1]},
        {"a": ["ML3", 2], "b": ["MTEE3", 1]},
        {"a": ["MTEE3", 2], "b": ["ML5", 1]},
        {"a": ["ML5", 2], "b": ["PORT3", 2]},
        {"a": ["MTEE1", 3], "b": ["R1", 2]},
        {"a": ["R1", 1], "b": ["MTEE3", 3]},
        {"a": ["PORT1", 1], "b": ["GND1", 1]},
        {"a": ["PORT2", 1], "b": ["GND2", 1]},
        {"a": ["PORT3", 1], "b": ["GND3", 1]},
    ],
}

EXPORT = '''
import json
d = odesign("AI_lib", "Wilkinson_5G9")
try:
    rows = []
    for inst in d.instances:
        m = inst.master_cell
        pins = []
        for p in inst.inst_pins:
            sp = p.snap_point
            pins.append([str(p.inst_term.term_number),
                         round(float(sp.x), 4), round(float(sp.y), 4)])
        rows.append({"name": inst.name, "x": inst.origin.x, "y": inst.origin.y,
                     "pins": pins})
    print(json.dumps(rows, ensure_ascii=False))
finally:
    d.close_design()
'''


def main():
    cfg = config.load()
    result = tools.call(cfg, "build_schematic", SPEC)
    summary = {k: result.get(k) for k in
               ("design", "layout", "backup", "placed", "n_instances",
                "n_nets", "parameters_ok")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    conns = result.get("connections", [])
    bad = [c for c in conns if c.get("status") != "ok"]
    print(f"CONNECTIONS {len(conns)} ok={len(conns) - len(bad)} bad={bad}")
    if result.get("problems"):
        raise RuntimeError(result["problems"])

    # 重建后导出实际引脚坐标，核验所有连线横平竖直
    export = tools.call(cfg, "run_python", {"code": EXPORT})
    rows = {r["name"]: r for r in json.loads(export["stdout"])}
    print("POSITIONS")
    for name in sorted(rows, key=lambda n: (-rows[n]["y"], rows[n]["x"])):
        r = rows[name]
        print(f"  {name:8s} x={r['x']:7.2f} y={r['y']:7.2f} pins={r['pins']}")
    diagonals = 0
    for conn in SPEC["connections"]:
        a, b = conn["a"], conn["b"]
        pa = next(p for p in rows[a[0]]["pins"] if p[0] == str(a[1]))
        pb = next(p for p in rows[b[0]]["pins"] if p[0] == str(b[1]))
        if abs(pa[1] - pb[1]) > 1e-6 and abs(pa[2] - pb[2]) > 1e-6:
            diagonals += 1  # 会被 _ortho_points 加直角拐点，不再是斜线
    print(f"非共轴连线（自动拐直角）: {diagonals} 条")

    sim = tools.call(cfg, "run_simulation", {"library": LIBRARY, "cell": CELL})
    print("SIM", sim.get("status"), sim.get("dataset_path"))


if __name__ == "__main__":
    main()
