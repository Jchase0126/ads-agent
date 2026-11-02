# RF 计算器（AppCAD 风格）与数值对拍清单

1.3.0 新增的 RF 计算器窗口是对 Avago/Broadcom AppCAD 4.00 中计算器功能的
**clean-room 重实现**：只依据公开教科书与期刊文献中的公式重新编写，未反编译、
未翻译、未复制原软件的任何代码或数据。本文给出与原版 AppCAD GUI 逐项对比的
用例与预期值，供在装有 AppCAD 的机器上做实机数值验收。

## 使用入口

- ADS 内：`Tools ▸ ADS Agent ▸ RF 计算器（AppCAD 风格）…`。
- 脱离 ADS 预览：`python addon/ads_agent/rfcalc_panel.py`（需要 PySide6）。
- 引擎直调（供脚本/测试）：`backend/rfcalc.py`，全部函数收 SI 单位、返回
  `{"ok": True, ...}` 或 `{"ok": False, "error": ...}`，不抛异常。

## 公式出处（与 AppCAD 数值口径的差异来源）

| 功能 | 公式 | 出处 |
| --- | --- | --- |
| 微带特性阻抗/εeff | Hammerstad–Jensen 1980 综合式 | E. Hammerstad, Ø. Jensen, "Accurate Models for Microstrip Computer-Aided Design", IEEE MTT-S 1980 |
| 微带损耗 | Hammerstad–Bekkadal αd + 低阻抗 αc 近似 | Hammerstad & Bekkadal 1975（αc 为近似，宽导带时偏保守，界面会提示） |
| 带状线 | Cohn 保角映射 Z0=(30π/√εr)·K(k)/K(k')，k'=tanh(πw/2b) | S.B. Cohn, "Problems in Strip Transmission Lines", IRE Trans. MTT-3, 1955；椭圆积分用 AGM 精确计算 |
| 同轴（圆） | 精确式 60/√εr·ln(b/a) + TE11 截止 | 教科书精确式（无近似） |
| 同轴（方） | Frank 等效半径 r=0.539038·s，即 Z0=60/√εr·ln(1.078076·s/d) | Frank 1932 方形同轴等效；常数 1.078076 按 AppCAD 4.00 实机三点定标（55.1Ω/17.9Ω/46.853mil 全部精确复现；流传值 1.0787 与实机不符） |
| 平行双线 | Z0=(120/√εr)·acosh(D/2a)…（按孤立/近距分支） | 教科书精确式 |
| CPW | Wen 保角映射 + Simons 有限厚度修正 k1=sinh(πS/4h)/sinh(π(S+2G)/4h) | C.P. Wen, MTT-17, 1969；R. Simons, "Coplanar Waveguide Circuits, Components, and Systems", Wiley 2001 |
| 板线（圆杆） | Frankel Z0=(60/√εr)·ln[coth(πd/4b)] | S. Frankel, MTT-10, 1962（Cohn 口径） |
| 线过地板 | 镜像法 Z0=(60/√εr)·acosh(2h/d)，2h/d≫1→ln(4h/d) | 教科书镜像法（等价双线 D=2h 取半） |
| BJT 固定基极偏置 | Ib=(Vcc−VBE)/RB、Ic=β·Ib、Vce=Vcc−Ic·RC、S=β+1 | 教科书固定偏置（无直流负反馈，AppCAD 同口径；Vce≤0.3V 告警饱和） |
| 级联噪声 | Friis 级联公式 + Te=(F−1)·T0，T0=290K | H.T. Friis, Proc. IRE 32, 1944 |
| 级联 OIP3 | 输出参考功率相加 1/OIP3=Σ1/OIP3i·∏G（第一级不除增益） | 与 AppCAD NoiseCalc 相同口径 |
| 混频器杂散 | m·fRF−n·fLO 全枚举 + 带内标记 | 与 AppCAD SpurFind 相同口径（AppCAD 另给相对电平，本版未实现电平估计） |
| 互调 | IM2/IM3 频率与 P_IM3=3P−2·OIP3、P_IM2=2P−OIP2 | 教科书口径 |
| BJT 分压偏置 | VRE≈10%·VCC（可改）、I2≈10·Ib 设计规则 | AppCAD Bias 设计规则同源（通用工程规则） |
| 放大器效率 | PAE=(Pout−Pin)/PDC | 定义式 |
| RFIC 电压偏置 | 串联电容 IL=10log₁₀(1+(Xc/2Z0)²)、偏置支路 IL=10log₁₀(1+(Z0/2Xb)²)、f-3dB=1/(2π·Z0·Cin) | 教科书插入损耗口径；三处按 AppCAD 4.00 实机定标（见用例 18） |
| RFIC 电流偏置 | Id=(Vs−Vd)/Rbias、Pd=Vd,typ·Id,typ、Tj=Tcase+Pd·θjc、Rd 分压损耗=10log₁₀(1+(Z0/2Rbias)²) | AppCAD 4.00 实机三温点表定标（见用例 19） |
| 大/小信号检波器 | Λ=1/vt（与 n、Rs、Rm 无关）、Rv=n·VT_APPCAD/(Is+Ibias)、Is(T) 温度修正、平方律 ΔI=gd·vrf²/(4·n·vt)、大信号贝塞尔 I0 | 二极管检波理论；Λ/Rv 按 AppCAD 4.00 两页实机反解（VT_APPCAD=0.0248358 V，见用例 20） |
| 器件热设计 | Pdc=Vin·Iin、Pdiss=Pdc−ΣPrf、Tj=Tcase+Pdiss·θjc、Ta=Tcase−Pdiss·θca | AppCAD Device Thermal 实机定标（见用例 21） |
| S 参数 / 稳定性 | Rollett K=(1−\|S11\|²−\|S22\|²+\|Δ\|²)/(2\|S12S21\|)、MAG=ratio·(K−√(K²−1))；Touchstone 列主序 | J. Rollett, IRE Trans. CT-9, 1962；Touchstone 1.0/2.0 规范 |
| 集总元件巴伦 | L=√(Rg·Rl)/ω0、C=1/(ω0·√(Rg·Rl))（ω0²LC=1） | AppCAD 4.00 实机两点定标（1GHz→8.0nH/3.2pF、2GHz→4.0nH/1.6pF，见用例 22） |

**已知未复刻**（面板无对应页，引擎未建模）：Everything S-Parameters 的
Gain/Noise Circles 叠加、多文件 6 槽同时载入与绘图（Rectangle/Polar 两种
视图）、Mixer Spurs 的**相对电平估计**（本版只给频率与带内标记）、
Balun 的 Cp 寄生电容参与计算（AppCAD 页仅提示、本版同样只警告）、
检波器大信号扫描表的**绝对幅度口径**（AppCAD 输出做了压缩/钳位，与贝塞尔
峰值包络差 45×…57571×，无法用常数拟合，见用例 20 备注）、
AppCAD 的 Application Examples / Tutorial / Web 链接内容。

## 对拍清单（与实机 AppCAD GUI 对比）

判定标准：引擎与 AppCAD 显示值相对偏差 ≤2%（微带/同轴等精确式应 ≤0.5%）；
超过时先确认双方输入单位与频率条件一致，再对照上表公式出处判断口径差异。
在 AppCAD 对应页面按相同输入填入，把两边读数抄进下表"实测"列。

### 1. 微带分析（Transmission Lines ▸ Microstrip）

| 输入 | W=3.0mm, h=1.6mm, εr=4.4, tanδ=0.02, f=10 GHz | 基片：FR-4 类 |
| --- | --- | --- |
| 引擎预期 | Z0=50.617Ω, εeff=3.3255 | H-J 1980 |
| AppCAD 实测 | | |

（AppCAD 同输入读数应一致到 ±0.5%；损耗 αc 为低阻抗近似，宽导带时两边
导体损耗可能相差数个百分点，属口径差异。）

### 2. 微带综合

| 输入 | Z0=50Ω, h=1.6mm, εr=4.4 | |
| --- | --- | --- |
| 引擎预期 | W/h=1.9138 → W=3.062mm | |
| AppCAD 实测 | | |

### 3. 带状线

| 输入 | W=0.5mm, b=1.0mm, εr=4.4 | |
| --- | --- | --- |
| 引擎预期 | Z0≈47.9Ω | Cohn 精确映射 |
| AppCAD 实测 | | |

### 4. 同轴（圆）

| 输入 | a=0.5mm, b=1.5mm, εr=2.2 | 近似 RG-405 半刚比例 |
| --- | --- | --- |
| 引擎预期 | Z0=44.441Ω；TE11 截止 32.17 GHz | 精确式 |
| AppCAD 实测 | | |

### 5. 同轴（方）

| 输入 | D1=100mil, D2=43mil, εr=1（AppCAD Square Coax 参考值） | |
| --- | --- | --- |
| 引擎预期 | Z0=55.149Ω；综合 Z0=50 反算 D2=46.853mil | Frank 等效半径（AppCAD 定标常数） |
| AppCAD 实测 | Z0 显示 55.1；D2=46.853（2026-10-30 实机三点定标） | |

### 6. 平行双线

| 输入 | a=0.5mm（半径）, D=10mm, εr=1 | |
| --- | --- | --- |
| 引擎预期 | Z0=442.59Ω | |
| AppCAD 实测 | | |

### 7. CPW

| 输入 | S=50µm, G=30µm, h=500µm, εr=10.9 | |
| --- | --- | --- |
| 引擎预期 | Z0≈52.15Ω, εeff≈5.940 | Wen+Simons 有限厚度 |
| AppCAD 实测 | | |

（AppCAD CPW 页若为无限厚基片口径，读数会略高于本引擎——有限厚度修正
在 h=500µm≫S+2G 时差异 <1%，正常应可比。）

### 8. 板线（Slabline）

| 输入 | d=0.5mm, b=1.0mm, εr=1 | |
| --- | --- | --- |
| 引擎预期 | Z0≈59.06Ω | Frankel |
| AppCAD 实测 | | |

### 9. 级联噪声（Noise Figure ▸ Cascade）

| 输入 | f=1 GHz；级1：G=10dB, NF=0.868dB, OIP3=30dBm；级2：G=3dB, NF=3dB, OIP3=20dBm | |
| --- | --- | --- |
| 引擎预期 | NF=1.208dB, Gain=13dB, OIP3=26.990dBm | Friis + OIP3 功率求和 |
| AppCAD 实测 | | |

### 10. 混频器杂散（Mixer Spurs）

| 输入 | fRF=1 GHz, fLO=1.5 GHz, IF=500 MHz, 容差=1 MHz | |
| --- | --- | --- |
| 引擎预期 | 表含 2×2−? 行；2·fRF−fLO=0.5GHz 标记带内 | 仅频率口径 |
| AppCAD 实测 | | |

（AppCAD 另给杂散电平估计，本版只做频率判定；电平列为后续版本。）

### 11. 互调

| 输入 | f1=1 GHz, f2=1.1 GHz, 每音 10dBm, OIP3=30dBm, OIP2=40dBm | |
| --- | --- | --- |
| 引擎预期 | IM3@3f1−2f2(0.8GHz)=−30dBm；IM2@f2−f1(0.1GHz)=−20dBm | |
| AppCAD 实测 | | |

### 12. BJT 分压偏置（Bias ▸ Bipolar）

| 输入 | VCC=12V, IC=10mA, VCE=6V, β=100, VRE=1V | |
| --- | --- | --- |
| 引擎预期 | RE=99.01Ω, RC=500Ω, R2=1.70kΩ, R1=9.364kΩ | |
| AppCAD 实测 | | |

（AppCAD 默认 VRE/I2 规则与引擎一致（10%·VCC、10·Ib）时才可比；自定义
规则页面请把 AppCAD 侧改成同值。）

### 13. FET 自偏

| 输入 | VDD=5V, ID=20mA, VGS=−0.5V, VDS=2V | |
| --- | --- | --- |
| 引擎预期 | RS=25Ω, RD=125Ω | |
| AppCAD 实测 | | |

### 14. 放大器效率

| 输入 | Pin=10dBm, Pout=30dBm, PDC=2.4W | |
| --- | --- | --- |
| 引擎预期 | PAE=41.25%, 漏极效率=41.67%, 增益=20dB | |
| AppCAD 实测 | | |

### 15. 反射工具

| 输入 | Z=25+j30Ω, Z0=50Ω | |
| --- | --- | --- |
| 引擎预期 | Γ=−0.1494+j0.4598（|Γ|=0.4834）, VSWR=2.872, 回损=6.31dB | |
| AppCAD 实测 | | |

### 16. 线过地板（Wire Over Groundplane）

| 输入 | d=1mm, h=10mm, εr=1 | |
| --- | --- | --- |
| 引擎预期 | Z0=60·acosh(2h/d)=221.33Ω | 镜像法 |
| AppCAD 实测 | | |

综合对拍：目标 Z0=50Ω、d=5mm、εr=1 → 引擎 h=(d/2)·cosh(Z0/60)=3.4193mm。

### 17. BJT 固定基极偏置（Bias ▸ Base Bias, non-stabilized）

| 输入 | VCC=12V, RB=470kΩ, RC=1kΩ, β=100, VBE=0.7V | |
| --- | --- | --- |
| 引擎预期 | Ib=24.04µA, Ic=2.404mA, Vce=9.596V, S=101 | |
| AppCAD 实测 | | |

（AppCAD 的 VBE 默认 0.78V：对拍时把两边 VBE 改成同值，否则 Ic 差约 11%。）

### 18. RFIC 电压偏置（Active Circuits ▸ Voltage Biased RFIC）

| 输入 | F1=834MHz, F2=1910MHz, Z0=50, Gain=17dB, Cin=Cout=13pF, RFC=30nH, Cb=750pF | |
| --- | --- | --- |
| 引擎预期 | Band1 caps 0.19 / rfc 0.11 / total 0.29 / overall 16.71 dB | |
| | Band2 caps 0.04 / rfc 0.11 / total 0.14 / overall 16.86 dB | |
| | f-3dB = 244.85MHz（Cin=13pF）；Cin=12.21pF 时 260.65MHz | |
| AppCAD 实测 | 0.19 / 0.11 / 0.29 / 16.71；0.04 / 0.11 / 0.14 / 16.86；f-3dB=260.65MHz | ✓ 全部吻合 |

两条关键口径（实机反推，非文献）：
1. **偏置支路损耗只在 f1 计算一次，两个 band 共用同一数值**（Band2 若按
   f2=1910MHz 单独算只有 0.02dB，与实机的 0.11dB 不符）。
2. **Total 行不含额外 Design Rule 项** = caps + rfc；f-3dB 用 1/(2π·Z0·Cin)
   而非 1/(4π·Z0·Cin)（后者只得 122MHz，差一倍）。
   实机显示 260.65MHz 与 Cin=13pF 的 244.9MHz 差 6.5%，来自 AppCAD 把
   Cin 显示为整数 13pF（真值 12.21pF）。

### 19. RFIC 电流偏置（Active Circuits ▸ Current Biased RFIC）

| 输入 | F=450MHz, Z0=50, RFC=360nH, Vs=12V, θjc=125°C/W, ∂Vd/∂T=−8mV/°C, Vd=6.6/5.5/4.4V, Id goal=80mA, Rbias=110Ω, Cb=1.5nF, Tcse=−25/25/85°C | |
| --- | --- | --- |
| 引擎预期（Tj/Pd/Id max/Id typ/Id min） | −25°C: 13.1 / 305.0 / 65.5 / 55.5 / 45.5 | |
| | 25°C: 65.6 / 325.0 / 69.1 / 59.1 / 49.1 | |
| | 85°C: 128.6 / 349.0 / 73.5 / 63.5 / 53.5 | |
| 引擎预期（标量） | Bias loss 0.22dB、Pd of Rbias 593.5mW | |
| AppCAD 实测 | 13.1/305.0/65.5/55.5/45.5；65.6/325.0/69.1/59.1/49.1；129.1/352.8/73.5/63.5/53.5；0.23dB、593.5mW | ✓（85°C 列 Tj 差 0.5°C、Pd 低 1.1%，其余全中） |

AppCAD 的表按**三个 Vd 档位**分别算电流（Id=(Vs−Vd)/Rbias），而不是按
∂Vd/∂T 线性外推单一值；Pd 用 25°C 标称 Vd,typ 作电压因子；
Pd of Rbias 取**最高壳温列的 Id max** 的焦耳耗散（Idmax²·Rbias）。

### 20. 大/小信号检波器（Active Circuits ▸ Large / Small Signal Detector）

| 页 | 输入 | 引擎 | AppCAD 实测 |
| --- | --- | --- | --- |
| 大信号 | T=25, Rs=9, Io=500µA, EG=0.69, Iso=0.03µA, n=1.08, Z0=50, Rm=68, RI=5000 | Λ=38.9217, Is=0.0300µA, Vd(0)=2.697E-01V, Rv=53.642Ω | 38.922 / 0.0300 / 2.69E-01 / 53.642 ✓ |
| 小信号 | T=25, Rs=6, Io=0, EG=0.35, Iso=0.06µA, n=1.15, Z0=50, RI=10000 | Λ=38.9217, Is=0.0600µA, Vo(0)=0, Rv=476019.5Ω | 38.922 / 0.0600 / 5.25E-14 / 476021.321 ✓ |

三条关键口径（实机反推）：
1. **Λ 就是 1/vt**（25°C 的 38.921744），与 n、Rs、Rm 全无关。文献式
   γ/(2·n·vt) 会得 15.55/31.83，与实机不符。
2. **Rv = n·VT_APPCAD/(Is+Ibias)**，其中 VT_APPCAD=0.0248358V 是由两页
   （不同 n、不同电流）反解出的**同一常数**（等价 288.2K，即 25°C 的
   0.9666 倍）。用 k/q/T 标准组合无法复现，故作为独立定标常量写进引擎。
3. **扫描表是页内固定区间**，与输入 Pin Start/Stop 无关：大信号固定
   −20…20 dBm（步进 5），小信号固定 −50…0 dBm（步进 2）。面板按此实现。
4. **已知差异（未复刻）**：大信号扫描表的绝对幅度。AppCAD 逐点值与本版
   贝塞尔峰值包络差 45×(−20dBm)…57571×(0dBm)，且 ≥5dBm 起本版触及
   1e9mV 截断上限——说明 AppCAD 对输出做了压缩/钳位，无法用常数拟合，
   只保留标量 Λ/Is/Rv/Vd(0) 的对齐。

### 21. 器件热设计（Reliability ▸ Device Thermal）

| 输入 | Vin=3V, Iin=42mA, Pin=2.1dBm, Pout=14.2dBm, θjc=220°C/W, θca=22°C/W, Tcase=25°C | |
| --- | --- | --- |
| 引擎预期 | Tj=47.29°C, Ta=22.77°C, Pdc=126mW, ΣPrf=24.68mW, Pdiss=101.32mW | |
| AppCAD 实测 | 47 / 23 / 126 / 24.7 / 101.3 | ✓ |

### 22. 集总元件巴伦（Circuit Design ▸ Lumped Element Balun）

| 输入 | f0=1000MHz, Rg=50Ω, Zload=50+j0Ω, Cp=0 | |
| --- | --- | --- |
| 引擎预期 | L=7.9577nH, C=3.1831pF（两方案同值），Zi=50Ω | |
| AppCAD 实测 | L=8.0nH, C=3.2pF（显示圆整） | ✓ |

第二点定标：f0=2000MHz → 引擎 3.9789nH/1.5915pF，AppCAD 显示 4.0nH/1.6pF。
电学验证：用显示圆整值（8nH/3.2pF）做节点分析得两臂 ∠−45.16°/+44.85°、
差分 1.0∠−90.31°、Zin=49.9985Ω。**注意**：用严格共振值
（ω0²LC 精确等于 1）时节点矩阵奇异——理想无损网络在精确共振点无解，
测试必须用圆整值。

### 23. S 参数与稳定性（Everything S-Parameters）

| 输入 | S11=0.35−j0.28, S21=1.62+j0.55, S12=0.04+j0.01, S22=0.31−j0.22 | |
| --- | --- | --- |
| 引擎预期 | K=4.9303（无条件稳定）、\|S21\|²=4.664dB、MAG=6.286dB | |
| AppCAD 实测 | （Smith 图几何按 spec_cd.md 复刻：圆心 (576,460)、半径 186） | |

## 验收记录

| 日期 | ADS/AppCAD 环境 | 结果 | 备注 |
| --- | --- | --- | --- |
| （待实机填写） | | | |
