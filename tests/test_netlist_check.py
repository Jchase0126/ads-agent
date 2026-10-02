# -*- coding: utf-8 -*-
"""netlist_check 单元测试（R8，2026-09-30）。

覆盖：parse_netlist 的行筛选/折行/参数截断、partition 划分语义
（地/端口隐含地/悬空脚/无脚件）、compare 差异报告、破坏实验
（改一条连接必须报差异）、check_components 参数核对。
运行: python tests/test_netlist_check.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import netlist_check as nc  # noqa: E402

_results = []


def ok(cond, msg):
    _results.append((bool(cond), msg))
    if not cond:
        print("  FAIL", msg)


def eq(a, b, msg):
    ok(a == b, f"{msg} (got {a!r} want {b!r})")


NETLIST = '''
; Top Design: "lib:t:schematic"
Options ResourceUsage=yes EnableOptim=no
#include "E:/x/models/RF.net"
BFR106:Q1  N__18 N__8 N__12
V_Source:VCC  N__3 0 Type="V_DC" Vdc=5 V SaveCurrent=1
R:R1  N__8 N__3 R=1.5 kOhm Noise=yes
C:CIN  N__21 0 C=27 pF
L:LIN  N__21 N__16 L=110 nH Noise=yes \\
SweepVar="freq" SweepPlan="SP1_stim"
Port:PORT1  N__21 0 Num=1 Z=50 Ohm Noise=yes
S_Param:SP1 CalcS=yes \\
SweepVar="freq" SweepPlan="SP1_stim" OutputPlan="SP1_Output"
SweepPlan: SP1_stim Start=70 MHz Stop=130 MHz Step=0.5 MHz
'''


def test_parse():
    p = nc.parse_netlist(NETLIST)
    eq(p.get("Q1"), ("N__18", "N__8", "N__12"), "Q1 三节点")
    eq(p.get("VCC"), ("N__3", "0"), "VCC 两节点(第二=地)")
    eq(p.get("R1"), ("N__8", "N__3"), "R1 节点")
    # 折行: L:LIN 第二行是参数续行,节点只有前两个 token
    eq(p.get("LIN"), ("N__21", "N__16"), "LIN 折行参数不混入节点")
    # 控制行不入元件表
    ok("SP1" not in p, "S_Param 控制行不进元件表")
    ok("SP1_stim" not in p, "SweepPlan 不进元件表")
    ok("PORT1" in p, "Port 端口在元件表")
    # 参数 "=5 V" 不被当节点
    eq(len(p.get("VCC", ())), 2, "VCC 参数未混入节点")


def test_partition_plan():
    inst = [
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1"}},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2"}},
        {"master": "ads_rflib:R", "name": "R1", "params": {"R": "1 kOhm"}},
        {"master": "ads_rflib:C", "name": "C1", "params": {"C": "1 nF"}},
        {"master": "ads_rflib:GROUND", "name": "G1"},
        {"master": "ads_rflib:GROUND", "name": "G2"},
    ]
    conns = [
        {"a": ["P1", "1"], "b": ["R1", "1"]},
        {"a": ["R1", "2"], "b": ["C1", "1"]},
        {"a": ["C1", "2"], "b": ["G1", "1"]},
        {"a": ["P2", "1"], "b": ["C1", "1"]},
    ]
    part = nc.partition_from_plan(inst, conns)
    # R1.2 与 C1.1 与 P2.1 同块
    def blk(name, pin):
        for v in part.values():
            if (name, pin) in v:
                return v
        return frozenset()
    b = blk("R1", "2")
    ok(("C1", "1") in b and ("P2", "1") in b, "R1.2-C1.1-P2.1 同网络")
    # 两个地引脚 + C1.2 同块
    bg = blk("G1", "1")
    ok(("G2", "1") in bg and ("C1", "2") in bg, "地引脚与对地脚同块")
    # P1.1 与 R1.1 同块
    ok(("R1", "1") in blk("P1", "1"), "P1.1-R1.1 同网络")


def test_equivalence_and_sabotage():
    inst = [
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2", "Z": "50 Ohm"}},
        {"master": "ads_rflib:R", "name": "R1", "params": {"R": "1.5 kOhm"}},
        {"master": "ads_rflib:C", "name": "CIN", "params": {"C": "27 pF"}},
        {"master": "ads_rflib:GROUND", "name": "G1"},
    ]
    conns = [
        {"a": ["P1", "1"], "b": ["R1", "1"]},
        {"a": ["R1", "2"], "b": ["CIN", "1"]},
        {"a": ["CIN", "2"], "b": ["G1", "1"]},
        {"a": ["P2", "1"], "b": ["CIN", "1"]},
    ]
    nl = """
R:R1  N__2 N__4 R=1.5 kOhm
C:CIN  N__4 0 C=27 pF
Port:P1  N__2 0 Num=1 Z=50 Ohm Noise=yes
Port:P2  N__4 0 Num=2 Z=50 Ohm Noise=yes
"""
    ok_, diffs, parsed = nc.check_netlist_equivalence(nl, inst, conns)
    ok(ok_, f"一致网表应等价 (diffs={diffs[:3]})")
    eq(nc.check_components(nl, inst), [], "元件参数一致")
    # 破坏1: 改一条连接 -> 必须报差异
    bad = [dict(c) for c in conns]
    bad[0] = {"a": ["P1", "1"], "b": ["CIN", "1"]}
    ok2, diffs2, _ = nc.check_netlist_equivalence(nl, inst, bad)
    ok(not ok2, "改连接后必须不等价")
    ok(len(diffs2) > 0, "破坏实验产生差异报告")
    # 破坏2: 参数漂移
    inst_bad = [dict(s) for s in inst]
    for s in inst_bad:
        if s["name"] == "R1":
            s["params"] = {"R": "2 kOhm"}
    cd = nc.check_components(nl, inst_bad)
    ok(any("R1" in d for d in cd) and len(cd) == 1,
       f"参数漂移被检出且仅 R1: {cd}")


def test_compare_partitions_report():
    exp = {frozenset({("A", "1"), ("B", "1")})}
    act = {frozenset({("A", "1"), ("C", "1")})}
    diffs = nc.compare_partitions(exp, act)
    ok(any("缺失网络" in d for d in diffs),
       f"缺失网络有报告: {diffs}")
    ok(any("多出网络" in d for d in diffs), "多出网络有报告")


def main():
    for fn in (test_parse, test_partition_plan,
               test_equivalence_and_sabotage, test_compare_partitions_report):
        print("==", fn.__name__)
        fn()
    n_fail = sum(1 for c, _ in _results if not c)
    print(f"结果: {'PASS' if n_fail == 0 else 'FAIL'} "
          f"({len(_results) - n_fail} 项通过 / {n_fail} 项失败)")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
