# -*- coding: utf-8 -*-
# 诊断 Q1.1->CDCOUT.1 (9.6,0)->(12.5,0) 为何无路 (2026-09-29)
import json, sys, importlib
sys.path.insert(0, r"D:\Antenna\ADS Agent\addon\ads_agent")
sys.path.insert(0, r"D:\Antenna\ADS Agent\tests")
import ads_ops, cb_amp_case
importlib.reload(ads_ops)
importlib.reload(cb_amp_case)
from cb_amp_case import INSTANCES, CONNECTIONS

placed = ads_ops.auto_layout_positions(INSTANCES, CONNECTIONS)
plan = ads_ops.plan_wire_routes(INSTANCES, CONNECTIONS, placed)
conns = []
for i, c in enumerate(CONNECTIONS):
    wp, tag = plan.get(i, ([], ""))
    c2 = dict(c, waypoints=[list(w) for w in wp], _net=tag)
    conns.append(c2)

de = ads_ops._de()
db_uu = ads_ops._db_uu()
de.active_workspace()
name = "BFR106_lib:CB_DIAG:schematic"
ads_ops._ensure_cell_view(de.active_workspace(), "BFR106_lib", "CB_DIAG", "schematic")
import keysight.ads.de._pde.db as _pdb
design = db_uu.open_design(name, mode=_pdb.DesignMode.WRITE)

log = []
try:
    for s in INSTANCES:
        p = placed[s["name"]]
        spec = dict(s, x=p["x"], y=p["y"])
        if spec.get("angle") is None and p.get("angle") is not None:
            spec["angle"] = p["angle"]
        kw = {"name": spec["name"]}
        if spec.get("angle") is not None:
            kw["angle"] = spec["angle"]
        design.add_instance(str(spec["master"]), (float(spec["x"]), float(spec["y"])), **kw)
    routed = ads_ops._design_wire_routes(design)
    fail_idx = None
    for idx, conn in enumerate(conns):
        try:
            ads_ops._wire_connection(design, conn, routed)
        except RuntimeError as e:
            fail_idx = idx
            log.append(f"[FAIL] conn{idx} {conn['a']}->{conn['b']}: {e}")
            break
    if fail_idx is None:
        log.append("全部连接成功!")
        raise SystemExit(0)
    conn = conns[fail_idx]
    ia = next(i for i in list(design.instances) if ads_ops._inst_name(i) == conn["a"][0])
    ib = next(i for i in list(design.instances) if ads_ops._inst_name(i) == conn["b"][0])
    pa = ads_ops._find_pin_on(ia, conn["a"][1])
    pb = ads_ops._find_pin_on(ib, conn["b"][1])
    x1, y1 = ads_ops._xy_point(pa.snap_point)
    x2, y2 = ads_ops._xy_point(pb.snap_point)
    boxes = ads_ops._symbol_boxes(design)
    pin_boxes = []
    for pt in ((x1, y1), (x2, y2)):
        pin_boxes.extend(ads_ops._box_owning_pins(pt, boxes))
    log.append(f"conn12 {conn['a']}->{conn['b']} pins ({x1},{y1})->({x2},{y2})")
    log.append(f"pin_boxes={pin_boxes}")
    direct = [(x1, y1), (x2, y2)]
    for nm, box in boxes:
        if ads_ops._segment_hits_box(direct[0], direct[1], box, strict=nm in pin_boxes):
            log.append(f"DIRECT 被 {nm} bbox {box} 挡(strict={nm in pin_boxes})")
    for old_route in routed:
        old_pts, old_net = old_route
        for c, d in zip(old_pts, old_pts[1:]):
            if ads_ops._segment_hits_route(direct[0], direct[1], c, d,
                                           {direct[0], direct[1]},
                                           {old_pts[0], old_pts[-1]}):
                log.append(f"DIRECT 与已有线 {old_pts} 段 {c}->{d} 冲突(net={old_net})")
    log.append(f"waypoints={conn.get('waypoints')}")
finally:
    design.close_design()
print("\n".join(log))
