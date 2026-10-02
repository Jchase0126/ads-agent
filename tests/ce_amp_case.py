# -*- coding: utf-8 -*-
"""固定回归案例:共射放大器 CE_FM_Amp(2026-09-29 用户截图对应设计)。

电气拓扑与参数 1:1 抄自 BFR106_lib:CE_FM_Amp 保存网表(generate_netlist()
2026-09-29 实测,用户手工整理布局后)。引脚 label 为实测 inst_term 编号:
BFR106 1=c(0.5,0.5) 2=b(0,0) 3=e(0.5,-0.5);C/L/R/V_DC/TermG 偏移见
ads_ops._AUTO_PIN_OFFS 注释。

instances 只给名字/master/参数,不给坐标 —— layout="auto" 由
auto_layout_positions 落位。目标是复现用户手工参考图的分区结构:
输入链一行进基极;电源母线(母线=T 分支)上方;输出并联支路(COUT/RD)
并排下挂集电极-输出行;全部接地支路就近展开。

EXPECTED_NETLIST:网表逐元件对照(重构后在线验证用)。
"""

INSTANCES = [
    {"master": "Infineon_RF:Infineon_Include_RF", "name": "INC1"},
    {"master": "ads_simulation:TermG", "name": "PORT1",
     "params": {"Num": "1", "Z": "50 Ohm"}},
    {"master": "ads_simulation:TermG", "name": "PORT2",
     "params": {"Num": "2", "Z": "50 Ohm"}},
    {"master": "ads_rflib:C", "name": "CIN", "params": {"C": "27 pF"}},
    {"master": "ads_rflib:L", "name": "LIN", "params": {"L": "110 nH"}},
    {"master": "ads_rflib:C", "name": "CDCIN", "params": {"C": "1 nF"}},
    {"master": "Infineon_RF:BFR106", "name": "Q1", "params": {}},
    {"master": "ads_rflib:C", "name": "CDCOUT", "params": {"C": "1 nF"}},
    {"master": "ads_rflib:C", "name": "COUT", "params": {"C": "3.9 pF"}},
    {"master": "ads_rflib:L", "name": "LOUT", "params": {"L": "33 nH"}},
    {"master": "ads_rflib:L", "name": "LC", "params": {"L": "2.2 uH"}},
    {"master": "ads_rflib:R", "name": "RD", "params": {"R": "51 Ohm"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "1.5 kOhm"}},
    {"master": "ads_rflib:R", "name": "R2", "params": {"R": "620 Ohm"}},
    {"master": "ads_rflib:R", "name": "RE1", "params": {"R": "62 Ohm"}},
    {"master": "ads_rflib:C", "name": "CE", "params": {"C": "10 nF"}},
    {"master": "ads_sources:V_DC", "name": "VCC", "params": {"Vdc": "5 V"}},
    {"master": "ads_rflib:GROUND", "name": "G1"},
    {"master": "ads_rflib:GROUND", "name": "G2"},
    {"master": "ads_rflib:GROUND", "name": "G3"},
    {"master": "ads_rflib:GROUND", "name": "G4"},
    {"master": "ads_rflib:GROUND", "name": "G5"},
    {"master": "ads_rflib:GROUND", "name": "G6"},
    {"master": "ads_rflib:GROUND", "name": "G7"},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "70 MHz", "Stop": "130 MHz", "Step": "0.5 MHz"}},
]

C = lambda a, b: {"a": [a[0], str(a[1])], "b": [b[0], str(b[1])]}
CONNECTIONS = [
    # 输入链 N21(PORT1-CIN) / N16(LIN-CDCIN)
    C(("PORT1", 1), ("CIN", 1)),
    C(("PORT1", 1), ("LIN", 1)),
    C(("CIN", 2), ("G1", 1)),
    C(("LIN", 2), ("CDCIN", 1)),
    # 基极 N8(Q1.b-CDCIN.2-R1-R2)
    C(("CDCIN", 2), ("Q1", 2)),
    C(("Q1", 2), ("R1", 1)),
    C(("Q1", 2), ("R2", 1)),
    C(("R2", 2), ("G2", 1)),
    # 发射极 N12(Q1.e-RE1-CE)
    C(("Q1", 3), ("RE1", 1)),
    C(("RE1", 2), ("G3", 1)),
    C(("Q1", 3), ("CE", 1)),
    C(("CE", 2), ("G4", 1)),
    # 集电极 N18(Q1.c-CDCOUT-LC.1)
    C(("Q1", 1), ("CDCOUT", 1)),
    C(("Q1", 1), ("LC", 1)),
    # 电源 N3(R1.2-VCC-LC.2)+ 源负端地
    C(("R1", 2), ("VCC", 1)),
    C(("LC", 2), ("VCC", 1)),
    C(("VCC", 2), ("G5", 1)),
    # 输出 N25(CDCOUT.2-COUT-RD-LOUT)
    C(("CDCOUT", 2), ("COUT", 1)),
    C(("CDCOUT", 2), ("RD", 1)),
    C(("COUT", 2), ("G6", 1)),
    C(("RD", 2), ("G7", 1)),
    C(("CDCOUT", 2), ("LOUT", 1)),
    # 输出 2 N30
    C(("LOUT", 2), ("PORT2", 1)),
]

# 网表逐元件对照(重构后在线验证用):元件 -> (节点元组)
# PORT1/PORT2 为 TermG,第二节点是全局地 0(隐含,不进划分)。
EXPECTED_NETLIST = {
    "Q1": ("N18", "N8", "N12"),        # c b e
    "VCC": ("N3", "0"),
    "R1": ("N8", "N3"),
    "R2": ("N8", "0"),
    "RE1": ("N12", "0"),
    "CE": ("N12", "0"),
    "LC": ("N18", "N3"),
    "CIN": ("N21", "0"),
    "LIN": ("N21", "N16"),
    "CDCIN": ("N16", "N8"),
    "CDCOUT": ("N18", "N25"),
    "COUT": ("N25", "0"),
    "LOUT": ("N25", "N30"),
    "PORT1": ("N21",),          # TermG 第二节点是隐含地参考,不进划分
    "PORT2": ("N30",),
    "RD": ("N25", "0"),
}
