"""将 Wilkinson_2G4_v2 重排为信号流自动布局（电气拓扑不变）。

v2 是端口接地正确的"好"设计，但布局沿袭旧坐标：分支脚到水平支线的连线
是斜线。本脚本读取现有实例/参数/网络，用 build_schematic(layout="auto")
重建：主干一行、两臂分支、隔离电阻竖放行间、GND 落地脚正下方，
全部曼哈顿走线。网表按"网络成员集合"逐网核对，必须与重建前完全一致。

运行::

    python tests/repair_wilkinson_v2_flow_layout.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import config  # noqa: E402
import tools  # noqa: E402

LIBRARY, CELL = "AI_lib", "Wilkinson_2G4_v2"
PARAMS = {
    "MSUB": ("H", "Er", "Mur", "Cond", "T", "TanD"),
    "Term": ("Num", "Z"),
    "MLIN": ("Subst", "W", "L"),
    "MTEE": ("Subst", "W1", "W2", "W3"),
    "R": ("R",),
    "S_Param": ("Start", "Stop", "Step"),
    "GROUND": (),
}

EXPORT = '''
import json
d = odesign("AI_lib", "Wilkinson_2G4_v2")
try:
    rows = []
    for inst in d.instances:
        m = inst.master_cell
        rows.append({
            "name": inst.name,
            "master": f"{m.library.name}:{m.name}",
            "x": float(inst.origin.x), "y": float(inst.origin.y),
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

ANGLE = {"Orientation.R0": None, "Orientation.R90": 90,
         "Orientation.R180": 180, "Orientation.R270": 270}


def main():
    cfg = config.load()
    export = tools.call(cfg, "run_python", {"code": EXPORT})
    rows = json.loads(export["stdout"])
    if len(rows) != 18:
        raise RuntimeError(f"实例数量预期 18，当前 {len(rows)}；停止以避免覆盖其它设计")

    instances, variables = [], None
    by_name = {}
    for row in rows:
        by_name[row["name"]] = row
        short = row["master"].split(":", 1)[1]
        if short == "VAR":
            variables = {"name": row["name"], "values": row["vars"]}
            continue
        spec = {"master": row["master"], "name": row["name"]}
        angle = ANGLE.get(row["orient"])
        if angle is not None:
            spec["angle"] = angle
        keep = PARAMS.get(short, ())
        params = {k: row["params"][k] for k in keep if row["params"].get(k, "") != ""}
        if params:
            spec["params"] = params
        instances.append(spec)

    # 网络 -> 引脚列表；两脚网络直连，gnd! 网络按 PORT↔GND 配对
    by_net = {}
    for row in rows:
        for pin, net in row["pins"]:
            by_net.setdefault(net, []).append([row["name"], int(pin)])
    connections = []
    for net, pins in by_net.items():
        if net == "gnd!":
            ports = sorted(p for p in pins if p[0].startswith("PORT"))
            gnds = sorted(p for p in pins if p[0].startswith("GND"))
            if len(ports) != len(gnds):
                raise RuntimeError(f"gnd! 网络引脚异常: {pins}")
            connections += [{"a": p, "b": g} for p, g in zip(ports, gnds)]
            continue
        if len(pins) != 2:
            raise RuntimeError(f"网络 {net} 有 {len(pins)} 个引脚，停止自动重建")
        connections.append({"a": pins[0], "b": pins[1]})

    old_nets = {frozenset((x[0], str(x[1])) for x in pins)
                for pins in by_net.values()}

    spec = {"library": LIBRARY, "cell": CELL, "recreate": True, "layout": "auto",
            "instances": instances, "var": variables, "connections": connections}
    result = tools.call(cfg, "build_schematic", spec)
    print("BACKUP", result.get("backup", {}).get("path"))
    print("PLACED", len(result.get("placed", [])), "INSTANCES",
          result.get("n_instances"))
    conns = result.get("connections", [])
    bad = [c for c in conns if c.get("status") != "ok"]
    print("CONNECTIONS", len(conns), "bad:", bad)
    if result.get("problems"):
        raise RuntimeError(result["problems"])

    # 网表核对：每个网络的成员集合必须与重建前一致（网络名允许变）
    export2 = tools.call(cfg, "run_python", {"code": EXPORT})
    rows2 = json.loads(export2["stdout"])
    by_net2 = {}
    for row in rows2:
        for pin, net in row["pins"]:
            by_net2.setdefault(net, []).append([row["name"], int(pin)])
    new_nets = {frozenset((x[0], str(x[1])) for x in pins)
                for pins in by_net2.values()}
    if old_nets != new_nets:
        raise RuntimeError(f"网络成员不一致！\n仅旧有: {old_nets - new_nets}"
                           f"\n仅新有: {new_nets - old_nets}")
    print("NETLIST 逐网核对一致:", len(new_nets), "个网络")

    print("POSITIONS")
    for row in sorted(rows2, key=lambda r: (-r["y"], r["x"])):
        print(f"  {row['name']:8s} x={row['x']:7.2f} y={row['y']:7.2f}")


if __name__ == "__main__":
    main()
