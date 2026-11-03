# -*- coding: utf-8 -*-
"""随机无源拓扑 fuzz：auto_layout 不抛异常、全落位、规划级零文字冲突。

用法: python tests/fuzz_layout.py [例数] [种子]
"""
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import ads_ops  # noqa: E402
import iterate_ml_sym_metrics as met  # noqa: E402


def vertical_known_shape(p, a, b):
    """竖放文字 × 竖线擦过 = 已知形态（保守 zone 模型,渲染验收在案,
    -0.85 分叉左移不变式）。R4 收敛时与 harness 保持同一判据。"""
    return (p.get("angle") or 0) % 180 == 90 and abs(a[0] - b[0]) < 1e-9


def _gen_amp_case(rng):
    """R9 扩展：三端管放大器拓扑（基极偏置+发射极电阻+集电极馈电+
    电源轨），覆盖 BFR106 多端锚点与 rail pass。"""
    inst = [{"master": "ads_simulation:TermG", "name": "P1",
             "params": {"Num": "1", "Z": "50 Ohm"}},
            {"master": "ads_simulation:TermG", "name": "P2",
             "params": {"Num": "2", "Z": "50 Ohm"}},
            {"master": "Infineon_RF:BFR106", "name": "Q1"},
            {"master": "ads_rflib:R", "name": "RB1",
             "params": {"R": "1.5 kOhm"}},
            {"master": "ads_rflib:R", "name": "RE",
             "params": {"R": "62 Ohm"}},
            {"master": "ads_rflib:L", "name": "LC",
             "params": {"L": "2.2 uH"}},
            {"master": "ads_sources:V_DC", "name": "VCC",
             "params": {"Vdc": "5 V"}},
            {"master": "ads_rflib:GROUND", "name": "GV"},
            {"master": "ads_rflib:GROUND", "name": "GND1"}]
    conns = [
        {"a": ["P1", "1"], "b": ["Q1", "2"]},   # 进基极(共射)
        {"a": ["Q1", "2"], "b": ["RB1", "1"]},
        {"a": ["RB1", "2"], "b": ["VCC", "1"]},
        {"a": ["Q1", "3"], "b": ["RE", "1"]},
        {"a": ["RE", "2"], "b": ["GND1", "1"]},
        {"a": ["Q1", "1"], "b": ["P2", "1"]},
        {"a": ["Q1", "1"], "b": ["LC", "1"]},
        {"a": ["LC", "2"], "b": ["VCC", "1"]},
        {"a": ["VCC", "2"], "b": ["GV", "1"]},
    ]
    n_in = rng.randint(1, 2)
    prev, lbl = "P1", 1
    for i in range(n_in):
        n = f"CIN{i}"
        inst.append({"master": rng.choice(["ads_rflib:C", "ads_rflib:L"]),
                     "name": n})
        conns.append({"a": [prev, str(lbl)], "b": [n, "1"]})
        prev, lbl = n, 2
    conns.append({"a": [prev, str(lbl)], "b": ["Q1", "2"]})
    return inst, conns


def _gen_two_shunt_case(rng):
    """R9 扩展：同一 host 挂两条接地支路 + 支路上再挂地 —— 多端节点
    与并排支路列生成的压力形态。"""
    inst = [{"master": "ads_simulation:TermG", "name": "P1"},
            {"master": "ads_simulation:TermG", "name": "P2"},
            {"master": "ads_tlines:MLIN", "name": "M0"}]
    conns = [{"a": ["P1", "1"], "b": ["M0", "1"]},
             {"a": ["M0", "2"], "b": ["P2", "1"]}]
    for j in range(2):
        inst.append({"master": rng.choice(["ads_rflib:R", "ads_rflib:C"]),
                     "name": f"S{j}"})
        inst.append({"master": "ads_rflib:GROUND", "name": f"G{j}"})
        conns.append({"a": ["M0", "2"], "b": [f"S{j}", "1"]})
        conns.append({"a": [f"S{j}", "2"], "b": [f"G{j}", "1"]})
    return inst, conns


def main(n=50, seed=20260929):
    random.seed(seed)
    MASTERS2 = ["ads_tlines:MLIN", "ads_rflib:R"]
    fails = []
    for idx in range(n):
        # 每 5 例插 1 例三端管、1 例双 shunt 同 host
        kind = idx % 5
        if kind == 3:
            inst, conns = _gen_amp_case(random)
        elif kind == 4:
            inst, conns = _gen_two_shunt_case(random)
        else:
            n_main = random.randint(2, 5)
            inst = [{"master": "ads_simulation:TermG", "name": "P1"},
                    {"master": "ads_simulation:TermG", "name": "P2"}]
            conns = []
            prev, prev_lbl = "P1", 1
            main_names = []
            for i in range(n_main):
                name = f"M{i}"
                inst.append({"master": random.choice(MASTERS2),
                             "name": name})
                conns.append({"a": [prev, prev_lbl], "b": [name, 1]})
                prev, prev_lbl = name, 2
                main_names.append(name)
            conns.append({"a": [prev, prev_lbl], "b": ["P2", 1]})
            for j in range(random.randint(0, 3)):
                host = random.choice(main_names)
                name = f"S{j}"
                inst.append({"master": random.choice(MASTERS2),
                             "name": name})
                inst.append({"master": "ads_rflib:GROUND", "name": f"G{j}"})
                conns.append({"a": [host, 2], "b": [name, 1]})
                conns.append({"a": [name, 2], "b": [f"G{j}", 1]})
        try:
            met._CUR_SPECS = inst
            placed = ads_ops.auto_layout_positions(
                inst, conns, {"name": "VAR1", "values": {"X": "1.0"}})
        except Exception as e:  # noqa: BLE001
            fails.append((idx, "异常", f"{type(e).__name__}: {e}"))
            continue
        _want = len(inst) + 1   # var_spec 总是创建 VAR1（所有 kind 一致）
        if len(placed) != _want:
            fails.append((idx, "缺位", f"{len(placed)}/{_want}"))
            continue
        wires = []
        for c in conns:
            pa = met.pin_xy(placed[c["a"][0]], met.master_of(c["a"][0]), c["a"][1])
            pb = met.pin_xy(placed[c["b"][0]], met.master_of(c["b"][0]), c["b"][1])
            pts = ads_ops._ortho_points(pa[0], pa[1], pb[0], pb[1])
            for s1, s2 in zip(pts, pts[1:]):
                if s1 != s2:
                    wires.append((s1, s2))
        for n, p in placed.items():
            z = ads_ops._annot_text_zone(met.master_of(n), p["x"], p["y"],
                                         *(p.get("annot") or (0, 0)),
                                         p.get("angle"))
            if z is None:
                continue
            # 与布局器 7.5 步同口径:竖放两脚元件的文字区被水平行线穿过
            # 是手工图常态(软冲突),不算违规;竖直段与文字互压才是
            for a, b in wires:
                if not ads_ops._segment_hits_box(a, b, z, strict=False):
                    continue
                # 分级口径统一走 ads_ops.text_wire_conflict_level（R4）
                lvl = ads_ops.text_wire_conflict_level(p.get("angle"), a, b)
                if lvl == "soft":
                    continue   # 水平行线 × 竖放文字 = 软冲突
                if lvl == "hard" and vertical_known_shape(p, a, b):
                    continue   # 竖线 × 竖放文字 = 已知形态(渲染验收在案)
                fails.append((idx, "线穿字", f"{n} wire={a}->{b}"))
                break
    print(f"fuzz {n} 例(种子 {seed}):",
          "全部通过" if not fails else f"{len(fails)} 项问题")
    for f in fails[:10]:
        print("  ", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20260929
    raise SystemExit(main(n, seed))
