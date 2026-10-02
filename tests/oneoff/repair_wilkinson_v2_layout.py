"""将 Wilkinson_2G4_v2 的原理图从千单位间距改为可见的紧凑布局。

读取现有设计的实例、参数和网络；build_schematic 会在覆盖前自动备份。
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config  # noqa: E402
import tools  # noqa: E402

LIBRARY, CELL = "AI_lib", "Wilkinson_2G4_v2"
SCALE = 0.005
PARAMS = {
    "MSUB": ("H", "Er", "Mur", "Cond", "T", "TanD"),
    "Term": ("Num", "Z"),
    "MLIN": ("Subst", "W", "L"),
    "MTEE": ("Subst", "W1", "W2", "W3"),
    "R": ("R",),
    "S_Param": ("Start", "Stop", "Step"),
}

EXPORT = r'''
import json
d = odesign("AI_lib", "Wilkinson_2G4_v2")
try:
    rows = []
    for inst in d.instances:
        master = inst.master_cell
        rows.append({
            "name": inst.name,
            "master": f"{master.library.name}:{master.name}",
            "x": inst.origin.x, "y": inst.origin.y,
            "orient": str(inst.orient),
            "params": {p.name: str(p.value) for p in inst.parameters},
            "vars": dict(inst.vars) if getattr(inst, "is_var_instance", False) else {},
            "pins": [(str(p.inst_term.term_number), str(getattr(p.net, "name", "")))
                     for p in inst.inst_pins],
        })
    print(json.dumps(rows, ensure_ascii=False))
finally:
    d.close_design()
'''


def main():
    cfg = config.load()
    export = tools.call(cfg, "run_python", {"code": EXPORT})
    if not export.get("ok"):
        raise RuntimeError(export.get("stdout"))
    rows = json.loads(export["stdout"])
    if len(rows) != 18:
        raise RuntimeError(f"实例数量预期 18，当前 {len(rows)}；停止以避免覆盖其它设计")

    instances, variables, by_net = [], None, {}
    for row in rows:
        master_cell = row["master"].split(":", 1)[1]
        if master_cell == "VAR":
            variables = {"name": row["name"], "x": row["x"] * SCALE,
                         "y": row["y"] * SCALE, "values": row["vars"]}
        else:
            spec = {"name": row["name"], "master": row["master"],
                    "x": row["x"] * SCALE, "y": row["y"] * SCALE,
                    "params": {k: row["params"][k] for k in PARAMS.get(master_cell, ())
                               if row["params"].get(k, "") != ""}}
            if row["orient"] == "Orientation.R180":
                spec["angle"] = 180
            instances.append(spec)
        for pin, net in row["pins"]:
            by_net.setdefault(net, []).append([row["name"], int(pin)])

    connections = []
    for net, pins in by_net.items():
        if net == "gnd!":
            for port, ground in (("PORT1", "GND1"), ("PORT2", "GND2"),
                                 ("PORT3", "GND3")):
                connections.append({"a": [port, 1], "b": [ground, 1]})
            continue
        if len(pins) != 2:
            raise RuntimeError(f"网络 {net} 有 {len(pins)} 个引脚，停止自动重建")
        connections.append({"a": pins[0], "b": pins[1]})

    spec = {"library": LIBRARY, "cell": CELL, "recreate": True,
            "instances": instances, "var": variables, "connections": connections}
    result = tools.call(cfg, "build_schematic", spec)
    print("BACKUP", result.get("backup", {}).get("path"))
    print("PLACED", len(result.get("placed", [])), "INSTANCES", result.get("n_instances"))
    print("CONNECTIONS", len(result.get("connections", [])))
    print("PROBLEMS", result.get("problems", []))
    if result.get("problems"):
        raise RuntimeError("只读复核未通过")
    sim = tools.call(cfg, "run_simulation", {"library": LIBRARY, "cell": CELL})
    print("SIM", sim.get("status"), sim.get("dataset_path"))


if __name__ == "__main__":
    main()
