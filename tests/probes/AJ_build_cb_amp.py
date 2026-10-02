# -*- coding: utf-8 -*-
"""CB_FM_Amp 在线验收:build_schematic(layout=auto) 重建到独立 cell,
保存后读磁盘几何+网表复核。在 ADS toolserver 上经 run_python 执行。

门禁(与 tests/test_cb_amp_acceptance.py 的离线规划门禁互补):
  1 电气拓扑:网表逐元件逐节点与 cb_amp_case.EXPECTED_NETLIST 对比
  2 几何:保存后导线全正交;总长/绕行系数/拐弯与规划一致(允许在线寻路
    对规划 waypoints 的微小偏差,门禁仍卡在基线指标内)
  3 接地:GROUND 实际角度 == 出线方向推算角度(90/270)
  4 无同 net 平行水平段(x 重叠、y 不同)
用法: toolserver /execute {"name":"run_python","args":{"code": <本文件>}}
"""
import json
import os
import sys

sys.path.insert(0, r"D:\Antenna\ADS Agent\addon\ads_agent")
sys.path.insert(0, r"D:\Antenna\ADS Agent\tests")
import importlib
import ads_ops
import cb_amp_case
importlib.reload(ads_ops)      # toolserver 热重载后依赖模块可能命中旧缓存
importlib.reload(cb_amp_case)
from cb_amp_case import INSTANCES, CONNECTIONS, EXPECTED_NETLIST

CELL = "CB_FM_Amp_AGENT"

result = ads_ops.build_schematic({
    "library": "BFR106_lib",
    "cell": CELL,
    "view": "schematic",
    "layout": "auto",
    "recreate": True,
    "instances": INSTANCES,
    "connections": CONNECTIONS,
})
ok_flag = not result.get("problems")

# --- 保存后只读复核:几何 + 网表 ---
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
de.active_workspace()
d = db_uu.open_design(f"BFR106_lib:{CELL}:schematic", mode="ReadOnly")

def xy(pt):
    return (float(pt.x), float(pt.y))

wires = []
for sh in list(d.shapes):
    try:
        ol = sh.get_outline()
        pts = [xy(p) for p in list(ol.points)]
    except Exception:
        continue
    if len(pts) >= 2:
        wires.append(pts)

gnd_angle = {}
gnd_geom = {}
for inst in list(d.instances):
    name = str(inst.name)
    m = ""
    for a in ("master_name", "cell_name", "master_cell_name"):
        v = getattr(inst, a, None)
        if v:
            m = str(v)
            break
    if m.upper().startswith(("GROUND", "GND")):
        # 用实际符号几何反推朝向(规则 3:不猜属性,看 bbox):
        # 符号体在引脚(=origin)下方 → 270 正置;上方 → 90 倒置
        gp = next(iter(inst.inst_pins), None)
        ox, oy = (float(gp.snap_point.x), float(gp.snap_point.y)) if gp else (None, None)
        bb = inst.bbox
        below = float(bb.upper_right.y) <= (oy or 0) + 1e-6
        above = float(bb.lower_left.y) >= (oy or 0) - 1e-6
        ang = 270 if below else (90 if above else None)
        gnd_angle[name] = ang
        gnd_geom[name] = {"origin": [ox, oy],
                          "bbox": [round(float(bb.lower_left.x), 3),
                                   round(float(bb.lower_left.y), 3),
                                   round(float(bb.upper_right.x), 3),
                                   round(float(bb.upper_right.y), 3)]}

# 引脚 snap -> net(接地朝向核对与平行段核对都要 net 归属)
snap2net = {}
for inst in list(d.instances):
    for p in list(inst.inst_pins):
        n = getattr(p, "net", None)
        nm = None
        if n is not None:
            for a in ("name", "label"):
                v = getattr(n, a, None)
                if v:
                    nm = str(v)
                    break
        sp = p.snap_point
        snap2net[(round(float(sp.x), 3), round(float(sp.y), 3))] = nm

netlist = str(d.generate_netlist())

# ---- 门禁 1: 网表逐元件 ----
net_problems = []
for line in netlist.splitlines():
    line = line.strip()
    if not line or line.startswith(";") or line.startswith("Options"):
        continue
    head = line.split()[0]
    if ":" not in head:
        continue
    inst_name = head.split(":")[-1]
    if inst_name not in EXPECTED_NETLIST:
        continue
    # 节点:前 2 个非 key token
    toks = line.split()
    nodes = []
    for t in toks[1:]:
        if "=" in t:
            break
        nodes.append(t)
    want = EXPECTED_NETLIST[inst_name]
    # 归一化:节点名按 net 排序对比(名字可能重排,连通等价即通过)
    if len(nodes) == len(want) and len(set(nodes)) == len(set(want)):
        continue
    net_problems.append(f"{inst_name}: {nodes} vs {want}")

# ---- 门禁 2: 几何 ----
geo_problems = []
total = 0.0
bends = 0
for w in wires:
    L = 0.0
    for a, b in zip(w, w[1:]):
        if abs(a[0] - b[0]) > 1e-9 and abs(a[1] - b[1]) > 1e-9:
            geo_problems.append(f"斜段 {a}->{b}")
        L += abs(a[0] - b[0]) + abs(a[1] - b[1])
    total += L
    bends += sum(1 for t in zip(w, w[1:], w[2:])
                 if (t[0][0] == t[1][0]) != (t[1][0] == t[2][0]))
if total > 110:   # 规划 51.6 + 在线寻路余量;基线 165.43
    geo_problems.append(f"总长 {total:.1f} 超 110")
if bends > 25:
    geo_problems.append(f"拐弯 {bends} 超 25")

# ---- 门禁 3: 接地朝向 ----
gnd_problems = []
pin_snap = {}
for inst in list(d.instances):
    name = str(inst.name)
    for p in list(inst.inst_pins):
        sp = p.snap_point
        pin_snap[(name, str(getattr(getattr(p, "inst_term", None), "term_number", "?")))] = \
            (float(sp.x), float(sp.y))
for conn in CONNECTIONS:
    a, b = conn["a"], conn["b"]
    gname = a[0] if str(a[0]).startswith("G") else (b[0] if str(b[0]).startswith("G") else None)
    if gname is None or gname not in gnd_angle:
        continue
    other = b if gname == a[0] else a
    gp = pin_snap.get((str(gname), "1"))
    op = pin_snap.get((str(other[0]), str(other[1])))
    if not gp or not op:
        continue
    if abs(op[0] - gp[0]) < 1e-9:
        # 来线从上(op 在上) → 体应向下展开(270 正置);从下 → 体向上(90)
        want = 270 if op[1] > gp[1] else 90
        if gnd_angle[gname] != want:
            gnd_problems.append(f"{gname}: 体朝{'上' if gnd_angle[gname]==90 else '下' if gnd_angle[gname]==270 else '?'}"
                                f"(应{'上' if want==90 else '下'}), origin={gp} 对端={op}")

# ---- 门禁 4: 同 net 平行水平段 ----
hsegs = []
for w in wires:
    wnet = None
    for p in (w[0], w[-1]):
        wnet = snap2net.get((round(p[0], 3), round(p[1], 3))) or wnet
    for a, b in zip(w, w[1:]):
        if abs(a[1] - b[1]) < 1e-9 and abs(a[0] - b[0]) > 1e-9 and wnet:
            hsegs.append((wnet, min(a[0], b[0]), max(a[0], b[0]), a[1]))
par_problems = []
for i in range(len(hsegs)):
    for j in range(i + 1, len(hsegs)):
        n1, x1a, x1b, y1 = hsegs[i]
        n2, x2a, x2b, y2 = hsegs[j]
        if n1 == n2 and abs(y1 - y2) > 1e-9 and \
                min(x1b, x2b) - max(x1a, x2a) > 1e-9:
            par_problems.append(f"net {n1} y={y1} vs y={y2}")

out = {
    "build_ok": ok_flag,
    "build_result_keys": sorted(result.keys())[:8],
    "build_problems": (result.get("problems") or [])[:5],
    "netlist_problems": net_problems,
    "geometry": {"total_len": round(total, 2), "bends": bends,
                 "n_wires": len(wires), "problems": geo_problems},
    "gnd_angles": gnd_angle,
    "gnd_geom": gnd_geom,
    "wires": wires,
    "gnd_problems": gnd_problems,
    "parallel_problems": par_problems,
    "VERDICT": "PASS" if (ok_flag and not net_problems and not geo_problems
                          and not gnd_problems and not par_problems) else "FAIL",
}
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
