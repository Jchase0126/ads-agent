# -*- coding: utf-8 -*-
"""固定回归案例:共基放大器 CB_FM_Amp(用户截图对应设计)。

电气拓扑与参数 1:1 抄自 BFR106_lib:CB_FM_Amp 的保存网表
(generate_netlist() 2026-09-29 实测),引脚 label 为实测 inst_term 编号:
BFR106 1=c 2=b 3=e;C/L/R/V_DC/TermG 偏移见 ads_ops._AUTO_PIN_OFFS 注释。
连接对(生成树)保持与原设计一致,net 划分不得改变。

instances 只给名字/master/参数,不给坐标 —— layout="auto" 由
auto_layout_positions 落位;离线测试直接调该函数。
"""

INSTANCES = [
    {"master": "ads_simulation:TermG", "name": "PORT1",
     "params": {"Num": "1", "Z": "50 Ohm", "Noise": "yes"}},
    {"master": "ads_simulation:TermG", "name": "PORT2",
     "params": {"Num": "2", "Z": "50 Ohm", "Noise": "yes"}},
    {"master": "ads_rflib:C", "name": "CIN", "params": {"C": "82 pF"}},
    {"master": "ads_rflib:L", "name": "LIN", "params": {"L": "27 nH"}},
    {"master": "ads_rflib:C", "name": "CDCIN", "params": {"C": "10 nF"}},
    {"master": "Infineon_RF:BFR106", "name": "Q1", "params": {}},
    {"master": "ads_rflib:C", "name": "CDCOUT", "params": {"C": "10 nF"}},
    {"master": "ads_rflib:C", "name": "CPOUT", "params": {"C": "10.0 pF"}},
    {"master": "ads_rflib:L", "name": "LOUT", "params": {"L": "220 nH"}},
    {"master": "ads_rflib:L", "name": "LC", "params": {"L": "4.7 uH"}},
    {"master": "ads_rflib:R", "name": "RLOAD", "params": {"R": "510 Ohm"}},
    {"master": "ads_rflib:R", "name": "R1", "params": {"R": "6.2 kOhm"}},
    {"master": "ads_rflib:R", "name": "R2", "params": {"R": "3.3 kOhm"}},
    {"master": "ads_rflib:R", "name": "RE", "params": {"R": "240 Ohm"}},
    {"master": "ads_rflib:C", "name": "CB", "params": {"C": "10 nF"}},
    {"master": "ads_sources:V_DC", "name": "VCC", "params": {"Vdc": "5 V"}},
    {"master": "ads_rflib:GROUND", "name": "G1"},
    {"master": "ads_rflib:GROUND", "name": "G2"},
    {"master": "ads_rflib:GROUND", "name": "G3"},
    {"master": "ads_rflib:GROUND", "name": "G4"},
    {"master": "ads_rflib:GROUND", "name": "G5"},
    {"master": "ads_rflib:GROUND", "name": "G6"},
    {"master": "ads_rflib:GROUND", "name": "G7"},
    {"master": "ads_simulation:S_Param", "name": "SP1",
     "params": {"Start": "70 MHz", "Stop": "130 MHz", "Step": "0.5 MHz",
                "Sort": "LINEAR START STEP", "CalcS": "yes"}},
]

CONNECTIONS = [
    # N__17 输入 1(PORT1-CIN-LIN)
    {"a": ["PORT1", "1"], "b": ["CIN", "1"]},
    {"a": ["CIN", "1"], "b": ["LIN", "1"]},
    {"a": ["CIN", "2"], "b": ["G1", "1"]},
    # N__20 输入 2
    {"a": ["LIN", "2"], "b": ["CDCIN", "1"]},
    # N__12 发射极
    {"a": ["CDCIN", "2"], "b": ["Q1", "3"]},
    {"a": ["Q1", "3"], "b": ["RE", "1"]},
    {"a": ["RE", "2"], "b": ["G2", "1"]},
    # N__6 基极偏置(Q1.b-CB-R2-R1)
    {"a": ["Q1", "2"], "b": ["CB", "1"]},
    {"a": ["CB", "1"], "b": ["R2", "1"]},
    {"a": ["R2", "1"], "b": ["R1", "1"]},
    {"a": ["CB", "2"], "b": ["G4", "1"]},
    {"a": ["R2", "2"], "b": ["G5", "1"]},
    # N__13 集电极(Q1.c 三支:CDCOUT / LC / RLOAD)
    {"a": ["Q1", "1"], "b": ["CDCOUT", "1"]},
    {"a": ["Q1", "1"], "b": ["LC", "1"]},
    {"a": ["Q1", "1"], "b": ["RLOAD", "1"]},
    {"a": ["RLOAD", "2"], "b": ["G7", "1"]},
    # N__25 输出 1(CDCOUT-CPOUT-LOUT)
    {"a": ["CDCOUT", "2"], "b": ["CPOUT", "1"]},
    {"a": ["CPOUT", "1"], "b": ["LOUT", "1"]},
    {"a": ["CPOUT", "2"], "b": ["G6", "1"]},
    # N__28 输出 2
    {"a": ["LOUT", "2"], "b": ["PORT2", "1"]},
    # N__14 电源(R1.2-VCC-LC.2)
    {"a": ["R1", "2"], "b": ["VCC", "1"]},
    {"a": ["VCC", "1"], "b": ["LC", "2"]},
    # gnd
    {"a": ["VCC", "2"], "b": ["G3", "1"]},
]

# 网表逐元件对照(重构后在线验证用):元件 -> (节点元组)
# PORT1/PORT2 为 TermG,第二节点是全局地 0。
EXPECTED_NETLIST = {
    "Q1": ("N13", "N6", "N12"),        # c b e
    "VCC": ("N14", "0"),
    "R1": ("N6", "N14"),
    "R2": ("N6", "0"),
    "CB": ("N6", "0"),
    "RE": ("N12", "0"),
    "LC": ("N13", "N14"),
    "CIN": ("N17", "0"),
    "LIN": ("N17", "N20"),
    "CDCIN": ("N20", "N12"),
    "CDCOUT": ("N13", "N25"),
    "CPOUT": ("N25", "0"),
    "LOUT": ("N25", "N28"),
    "PORT1": ("N17", "0"),
    "PORT2": ("N28", "0"),
    "RLOAD": ("N13", "0"),
}
