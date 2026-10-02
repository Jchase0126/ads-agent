# -*- coding: utf-8 -*-
"""固定回归案例:共基放大器 FM_SC(2026-09-30 用户 document1.pdf 对应设计)。

电气拓扑与参数 1:1 抄自 BFR106_lib:FM_SC 保存网表
(ads_agent_sim/FM_SC_20260930_134312/netlist.ckt,layout=auto 当日产物,
用户以 PDF 点名"元件过于分散、接地支路偏长、存在大跨度折返")。
引脚 label 实测: BFR106 1=c(0.5,0.5) 2=b(0,0) 3=e(0.5,-0.5);
R/C/L/V_DC/TermG 偏移见 ads_ops._AUTO_PIN_OFFS。

拓扑:PORT1-CDC-[C1|L1]-[C2|L2]-Q1.e(共基输入),Q1.c-CDCOUT-[RD|C3|L3]-[C4|L4]-PORT2;
基极偏置 R1/R2/CB 挂 Q1.b,R1 上拉电源轨(VCC-LC);发射极 RE 就地接地。
EXPECTED_NETLIST:网表逐元件对照(在线重建后核对用)。
"""

INSTANCES = [
    {"master": "Infineon_RF:Infineon_Include_RF", "name": "INC1"},
    {"master": "Infineon_RF:BFR106", "name": "Q1", "params": {}},
    {"master": "ads_sources:V_DC", "name": "VCC",
     "params": {"Vdc": "5 V", "SaveCurrent": "1"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "R1_b", "Noise": "yes"}},
    {"master": "ads_rflib:R", "name": "R2", "params": {"R": "R2_b", "Noise": "yes"}},
    {"master": "ads_rflib:R", "name": "RE", "params": {"R": "RE_b", "Noise": "yes"}},
    {"master": "ads_rflib:R", "name": "RD", "params": {"R": "RD_b", "Noise": "yes"}},
    {"master": "ads_rflib:C", "name": "CB", "params": {"C": "CB_b"}},
    {"master": "ads_rflib:C", "name": "CDC", "params": {"C": "CDC_b"}},
    {"master": "ads_rflib:C", "name": "CDCOUT", "params": {"C": "CDCOUT_b"}},
    {"master": "ads_rflib:C", "name": "C1", "params": {"C": "C1_m"}},
    {"master": "ads_rflib:C", "name": "C2", "params": {"C": "C2_m"}},
    {"master": "ads_rflib:C", "name": "C3", "params": {"C": "C3_m"}},
    {"master": "ads_rflib:C", "name": "C4", "params": {"C": "C4_m"}},
    {"master": "ads_rflib:L", "name": "L1", "params": {"L": "L1_m", "Noise": "yes"}},
    {"master": "ads_rflib:L", "name": "L2", "params": {"L": "L2_m", "Noise": "yes"}},
    {"master": "ads_rflib:L", "name": "L3", "params": {"L": "L3_m", "Noise": "yes"}},
    {"master": "ads_rflib:L", "name": "L4", "params": {"L": "L4_m", "Noise": "yes"}},
    {"master": "ads_rflib:L", "name": "LC", "params": {"L": "LC_b", "Noise": "yes"}},
    {"master": "ads_simulation:TermG", "name": "PORT1",
     "params": {"Num": "1", "Z": "50 Ohm", "Noise": "yes"}},
    {"master": "ads_simulation:TermG", "name": "PORT2",
     "params": {"Num": "2", "Z": "50 Ohm", "Noise": "yes"}},
    {"master": "ads_rflib:GROUND", "name": "G1"},   # C1
    {"master": "ads_rflib:GROUND", "name": "G2"},   # C2
    {"master": "ads_rflib:GROUND", "name": "G3"},   # RE
    {"master": "ads_rflib:GROUND", "name": "G4"},   # R2
    {"master": "ads_rflib:GROUND", "name": "G5"},   # CB
    {"master": "ads_rflib:GROUND", "name": "G6"},   # VCC 负端
    {"master": "ads_rflib:GROUND", "name": "G7"},   # RD
    {"master": "ads_rflib:GROUND", "name": "G8"},   # C3
    {"master": "ads_rflib:GROUND", "name": "G9"},   # C4
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"CalcS": "yes", "Start": "70 MHz", "Stop": "130 MHz",
                "Step": "0.5 MHz"}},
]

VAR_SPEC = {"name": "VAR1", "values": {
    "X": "1.0", "C1_m": "56 pF", "C2_m": "240 pF", "C3_m": "3.3 pF",
    "C4_m": "15 pF", "CB_b": "10 nF", "CDC_b": "10 nF",
    "CDCOUT_b": "10 nF", "L1_m": "30 nH", "L2_m": "6.8 nH",
    "L3_m": "470 nH", "L4_m": "130 nH", "LC_b": "4.7 uH",
    "R1_b": "3.0 kOhm", "R2_b": "1.9 kOhm", "RD_b": "1.0 kOhm",
    "RE_b": "100 Ohm",
}}

C = lambda a, b: {"a": [a[0], str(a[1])], "b": [b[0], str(b[1])]}
CONNECTIONS = [
    # 输入链 N16(PORT1-CDC) / N27(CDC-L1-C1)
    C(("PORT1", 1), ("CDC", 1)),
    C(("CDC", 2), ("L1", 1)),
    C(("CDC", 2), ("C1", 1)),
    C(("C1", 2), ("G1", 1)),
    # N29(L1-L2-C2)
    C(("L1", 2), ("L2", 1)),
    C(("L1", 2), ("C2", 1)),
    C(("C2", 2), ("G2", 1)),
    # 发射极 N0(Q1.e-L2-RE)
    C(("L2", 2), ("Q1", 3)),
    C(("Q1", 3), ("RE", 1)),
    C(("RE", 2), ("G3", 1)),
    # 基极 N1(Q1.b-R1-R2-CB)
    C(("Q1", 2), ("R1", 1)),
    C(("Q1", 2), ("R2", 1)),
    C(("Q1", 2), ("CB", 1)),
    C(("R2", 2), ("G4", 1)),
    C(("CB", 2), ("G5", 1)),
    # 集电极 N36(Q1.c-CDCOUT-LC)
    C(("Q1", 1), ("CDCOUT", 1)),
    C(("Q1", 1), ("LC", 2)),
    # 电源 N35(R1-VCC-LC)+ 源负端地
    C(("R1", 2), ("VCC", 1)),
    C(("LC", 1), ("VCC", 1)),
    C(("VCC", 2), ("G6", 1)),
    # 输出 N31(CDCOUT-RD-C3-L3)
    C(("CDCOUT", 2), ("RD", 1)),
    C(("CDCOUT", 2), ("C3", 1)),
    C(("CDCOUT", 2), ("L3", 1)),
    C(("RD", 2), ("G7", 1)),
    C(("C3", 2), ("G8", 1)),
    # N33(L3-L4-C4)
    C(("L3", 2), ("L4", 1)),
    C(("L3", 2), ("C4", 1)),
    C(("C4", 2), ("G9", 1)),
    # 输出 N38
    C(("L4", 2), ("PORT2", 1)),
]

# 网表逐元件对照(在线重建后核对用):元件 -> (节点元组)
# PORT1/PORT2 为 TermG,第二节点是全局地 0(隐含,不进划分)。
EXPECTED_NETLIST = {
    "Q1": ("N36", "N1", "N0"),         # c b e
    "VCC": ("N35", "0"),
    "R1": ("N1", "N35"),
    "R2": ("N1", "0"),
    "RE": ("N0", "0"),
    "RD": ("N31", "0"),
    "CB": ("N1", "0"),
    "CDC": ("N16", "N27"),
    "CDCOUT": ("N36", "N31"),
    "C1": ("N27", "0"),
    "C2": ("N29", "0"),
    "C3": ("N31", "0"),
    "C4": ("N33", "0"),
    "L1": ("N27", "N29"),
    "L2": ("N29", "N0"),
    "L3": ("N31", "N33"),
    "L4": ("N33", "N38"),
    "LC": ("N35", "N36"),
    "PORT1": ("N16",),
    "PORT2": ("N38",),
}
