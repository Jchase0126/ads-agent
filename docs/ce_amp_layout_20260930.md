# CE_FM_Amp 布局重构档案（2026-09-29/30）

设计：`BFR106_lib:CE_FM_Amp`（共射 FM 放大器，88–108MHz，5V）。
触发：用户两图对比任务——图 1 为 layout=auto 初版（16:40 构建），
图 2 为用户手工整理保存版（目标参考）。

## 电气拓扑（重构不变量，8 网）

N21 输入1: PORT1.1—CIN.1—LIN.1（CIN.2 下地）
N16 输入2: LIN.2—CDCIN.1
N8  基极: CDCIN.2—Q1.2(b)—R1.1—R2.1（R2.2 下地）
N12 发射极: Q1.3(e)—RE1.1—CE.1（RE1.2/CE.2 下地）
N18 集电极: Q1.1(c)—CDCOUT.1—LC.1
N3  电源: R1.2—VCC.(+)—LC.2（VCC.(−) 横地）
N25 输出: CDCOUT.2—COUT—RD—LOUT.1（COUT/RD 下地）
N30 输出2: LOUT.2—PORT2.1
引脚实测：BFR106 1=c(0.5,0.5) 2=b(0,0) 3=e(0.5,-0.5)。

## 初版（图1）四类问题与根因

1. 电源网串成竖直堆叠链（LC→VCC→G5→R1 同列），R1 被推到 y=6，
   基极馈线 6.5 格长绕行 —— 通用分支逻辑不认识"电源轨"形态。
2. 输出并联支路 COUT/RD 各占一整行（row_h=5），被推到 y=-5/-10。
3. 三端器件入口侧落差：place_chain 按出口脚对齐行线，输入链末端
   抬高 0.5 格再绕。
4. RE1/CE 散落多行。

## 重构（ads_ops auto_layout_positions 1.5 步，泛化非特例）

- rail bus：含 V_DC/I_DC、无端口、成员≥3 的网络 → 成员竖挂锚点行
  上方（锚点脚 +0.75），母线 = 最高锚点行 + 2.125，源横放母线右端，
  负端横地（自由端才横地）。
- shunt group：同节点全部"单件+地"并排下挂节点行（节点脚骑行线、
  地脚朝挂放侧），多锚组扫行线跨度内空槽（RD 落 COUT/LOUT 间窄槽），
  R 类列距 2.0（文字宽 1.75）。
- 三端器件入口脚对齐行线，出口脚高度成为后续行线。

## 结果（BFR106_lib:CE_FM_Amp_AGENT，recreate 重建）

28 段导线 0 problems；网表逐引脚等价 ok（16 元件 0 差异）；
S 参数与手工版逐位一致（Gmin 21.4434 / S11max -14.2932 /
S22max -12.2007 dB @ 88–108MHz）；metrics：总长 29.3、绕行 1.054、
0 拐弯、0 重复段、0 平行出线。手工版基线（总长 17.7 但含 2 处
VCC 穿体真违规）存 tests/probes/ce_before_geo.json。

## 回归

tests/ce_amp_case.py（固定案例，含 EXPECTED_NETLIST）+
tests/test_ce_amp_acceptance.py（133 项）+ tests/test_netlist_check.py
（18 项）+ tests/replay_ce_auto.py（图1 复原工具）。
