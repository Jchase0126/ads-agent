# Layout 审查设计（需求 → 原理图 → Layout → 验证 → 反馈修改）

2026-09-28 落地。本文记录两种布局的边界、本环境实测的 ADS 2027 API 能力、
`audit_rf` 工具的检查项与输出格式、以及因环境限制仍未验证的项目。

## 1. 两种布局的边界（工程约定）

| | 原理图布局 | ADS Layout |
|---|---|---|
| 职责 | 信号流、可读性、引脚方向、接地位置 | 实际铜皮的宽度、间隙、长度、拐角、接地回流、制造约束 |
| 导线 | 绘图坐标，**长度绝不是微带线物理长度** | 真实几何，长度/拐角/间隙都可直接度量 |
| 审查 | `audit_rf` 原理图级（参数与连通几何） | `audit_rf` Layout 级（真实铜皮图形） |

Agent 系统提示（backend/agent.py「两种布局与射频物理审查」节）与工具
description 同文约束：涉及射频物理正确性时调 audit_rf，不得拿原理图
画面整齐与否代替物理审查。

## 2. 本环境实测能力矩阵（ADS 2027.650，2026-09-28 实测）

| 能力 | 结论 | 证据 |
|---|---|---|
| 打开/读取 Layout 视图 | 可用 | `odesign(lib, cell, view="layout", write=False)`；`Design.is_layout`；`Design.shapes` 逐图形给出 kind/layer_id/bbox/width/outline |
| 读 Layout 实例/端口 | 可用 | `Design.instances`（实例名与原理图同名可映射）、`Design.terms` |
| 原理图 → Layout 同步生成（S2L） | **不可用** | `keysight.ads` 文档索引 17409 条中无 S2L/synchronizer API（仅工作区偏好项 DSE_S2L_REPORT）；`db_uu.create_layout` 只能建空 Layout |
| 从零建 Layout 图形 | 可用（仅用于开发校验） | `db_uu.create_layout("lib:cell:layout")` + `add_path/add_rectangle/add_term/add_pin`（官方示例 ex_create_layout） |
| LineCalc | **不可程序化调用** | Python API 0 命中；`HPEESOF_DIR/bin/linecalc.exe` 为独立 GUI 程序，无文档化 CLI/脚本接口 |
| EM | **部分可用，仿真自动化未实现** | `keysight.ads.emtools` 有 `create_empro_view(empro_lcv, tool, layout_lcv, substrate_ls)`、`get_substrate_info(emsetup_lcv)` 等；但 EM 求解（FEM/Momentum）无自动化路径，电路 vs EM 对比未完成 |
| 弯折样式 | 模型存在 | `db.LineTypeInfo.corner_type`、`BendStyle.ADAPTIVE_MITERED/MITered/...`（切角尺寸应由线型/阻抗/基板/工艺决定，审查不写死比例） |

## 3. 工具入口

`audit_rf(library, cell)` —— 经 toolserver 派发到
`addon/ads_agent/ads_ops.audit_rf`，纯计算层在 `addon/ads_agent/rf_audit.py`
（不依赖 ADS，离线可测）。toolserver 对 ads_ops 及其依赖（`_ADS_OPS_DEPS`）
按 mtime 热重载；ads_ops 导入时亦强制 reload 依赖，双保险。

### 原理图级检查

1. **传输线参数**（要求 3）：遍历 MLIN/MLSC/CPWG 等，读 W/L/Subst 与
   MSUB 的 H/Er/T/TanD、控制器频率（S_Param Start/Stop、AC Freq）、
   Term 的 Z 作为参考阻抗；VAR 引用（W="W50"）解析成设计真实值后按
   Hammerstad 一阶闭式算 Z0/eps_eff/电长度。CPWG 不出闭式数值，只列
   待验证项。**全部标注「未过 LineCalc 验证」**。
2. **MTEE 方向与宽度**（要求 4）：按旋转后的实际引脚世界坐标（snap_point）
   判定贯穿对/分支脚（两两距离最远的一对为贯穿），逐端核对 W1/W2/W3 与
   相接传输线宽；贯穿对默认 W1=W2，宽度差呈「分支与一端同宽」的分裂/合并
   格局时降为 warning（疑似功率分配结的设计意图），仍要求确认；三宽全异
   才是 error；有意变宽必须有过渡结构（MSTEP）及理由。
3. **直角连接**：两条微带线 90° 直接相接且无 MBEND/MSBEND 时报警。

### Layout 级检查（真实铜皮）

1. 无 layout 视图或视图为空 → **如实返回「未完成 Layout 验证」**，指引
   GUI 手动 Generate/Update Layout；绝不编造几何。
2. 实例名 ↔ 原理图实例名对应（同步漂移点名）。
3. 未切角直角弯折（路径多边形 90° 顶点，定位到坐标）。
4. 同层铜皮重叠（bbox 交叠面积，网络归属未比对需人工确认）。
5. 最近间隙：只报数值不判罚（本环境无工艺规则库，限值不臆造）。
6. 接地层：只有单层导体时提示接地回流缺失。
7. EM：标注 emtools 可用性，EM 对比恒为「未验证」。

### finding 格式（要求 6）

```json
{"id": "rf-step-MTEE1", "severity": "error|warning|info",
 "element": "MTEE1 pin1/pin2", "location": "schematic|layout(x,y)",
 "actual": {"W1": "3.06 mm", "...": "..."},
 "problem": "...", "suggestion": "...",
 "verification": "已验证（原理图参数数据）|未过 LineCalc 验证|未完成 Layout 验证|未验证（…）"}
```

## 4. 实测输出示例（AI_lib:Wilkinson_5G9，2026-09-28 实机）

- 频率：4.5–7.5 GHz（S_Param），VAR 表 W50=3.06mm / W70=1.62mm /
  L50=6.96mm / Lq=7.04mm 全部解析成功。
- 闭式结果：W50 线 Z0≈8.6Ω（w/h=12，超出一阶公式最佳区间，结论需
  LineCalc 复核）、W70 线 ≈14.7Ω；电长度 ≈143–145°（设计 f0=5.9GHz 的
  λ/4 应约 90°）——这批数字本身就是给 Agent 的修改线索。
- findings：MTEE1/2/3 贯穿对宽度差（分裂格局 → warning，要求确认）、
  MTEE2 pin3(W50) ↔ ML3(W70) 真实突变（error）、Layout 视图缺失
  （未完成 Layout 验证）。

## 5. 已知限制（如实声明）

1. S2L 不可用：Layout 审查只能审「已经存在的」Layout；用户需先在
   GUI 生成，否则结论恒为「未完成 Layout 验证」。
2. LineCalc 不可程序化调用：所有 Z0/电长度为闭式模型（w/h≤10 内 ~2%，
   宽线误差更大），不能替代 LineCalc 合成；工具结果里永远带此标注。
3. EM 对比未实现：emtools 能建 EM 视图，但没有 EM 求解自动化；
   电路仿真与 EM 结果的对比一律标「未验证」。
4. 间隙/切角没有工艺规则库：间隙只报数值；切角建议指向 ADS 的
   BendStyle/CornerType 模型，不写死比例。
5. 原理图闭式核对只覆盖微带（MLIN）；CPWG/其他线型只列待验证项。

## 6. 测试

- 离线：tests/test_rf_audit.py（21 项，纯计算层，含诚实性断言：
  表达式不出数值、CPWG 不出闭式数、无 Layout 时结论必须是
  「未完成 Layout 验证」）。
- 全量：python tests/run_tests.py（16 个文件）。
- 实机：本文 §4 的输出即 2026-09-28 在 ADS 2027 实机上的真实审查结果；
  Layout 读取层用一次性 scratch Layout（AI_lib:_lay_probe，直角路径/
  重叠矩形/cond2 地层）验证后已删除。
