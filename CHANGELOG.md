# 更新记录

## 未发布 — 2026-10-30（ADS 长计算卡死修复）

修复"Agent 执行长计算时 ADS 界面卡死 / 未响应"的根因（审查见
[ADS未响应审查与优化方案](docs/ADS未响应审查与优化方案_2026-10-30.md)）。

- **新增 `run_compute` 工具：长计算移出 ADS 主线程**。NumPy/SciPy 优化、拟合、
  大规模搜索和数据文件处理在后端进程的**独立计算子进程**中执行；
  `backend/compute_worker.py` 是执行体，`backend/compute_manager.py` 负责排队、
  并发上限、执行预算、取消与进程树回收。子进程无管道（stdout/stderr 直接写
  受控日志，不会因管道写满而死锁）、Windows 隐藏窗口、崩溃不影响后端、后端重启
  后遗留作业标记为失败而不是伪造成功。
- **数据传递显式化**：`inputs` 显式传给计算进程，结果经 `result` 变量或
  `emit_result()` 返回；`run_python` 的常驻 `_PY_ENV` **不会**被计算进程继承
  （旧脚本必须显式传数据，不会静默带着空变量继续跑）。NDArray 自动转 JSON 列表，
  超过 256KB 的结果标为 `partial` 而不是悄悄截断成"完整成功"。
- **解释器选择策略**：默认用后端自带解释器，可用 `[agent] compute_python` 指定
  独立环境；启动前探测 `numpy`/`scipy`，缺依赖时返回明确的 `ComputeEnvError`
  （含解释器路径与缺失模块），**绝不静默切换到缺依赖的解释器**。
- **取消链路贯通轮次 → 作业 → worker**：`job_id` 在工具派发**前**生成并登记到
  轮次；停止按钮 → `/chat/cancel` → 后端 `Turn.propagate_cancel()` → 计算作业走
  `compute_manager.cancel()`（协作停止 → 宽限期 → 结束自有进程树），ADS 侧作业走
  工具服务 `/cancel`。取消幂等；"已请求取消"与"已退出"在界面和 API 里分开表述。
- **排队取消绝不执行**：排队中被取消的作业不进入处理器（测试直接断言 handler
  未执行），也不会留下 `client_id` 残留误伤后续作业。
- **`run_python` 的取消边界如实化**：执行前收到取消则一行用户代码都不跑
  （`cancel_boundary=before_exec`）；执行中被取消则明确返回 `after_exec` 边界与
  耗时，说明"此前的写入已发生、请以设计实际状态为准"，**不做跨线程注入异常式的
  强制中断**（那会留下半写设计）。超过 5s 会提示主线程被占用并引导改用
  `run_compute`。工具说明与系统提示同步加上"禁区"说明。
- **ADS 端调度：软时间预算 + 原子有界入队 + 活跃上限**。`pump()` 增加
  `pump_time_budget_ms`（默认 8ms）软预算，超预算只停止**启动下一个**作业、
  绝不抢占正在跑的同步处理器；排队容量检查与入队在同一把锁内完成（不再
  "先查 qsize 再 put"竞态）；排队容量（`tool_queue_capacity`）与活跃作业数
  （`active_job_limit`）分开限制。
- **GUI 心跳与 `/health`**：GUI 线程用独立 QTimer 刷新心跳（不依赖 pump），
  `/health` 新增 `gui_heartbeat_age_ms`、`heartbeat_stale`、`active_limit`、
  `cancelling_count`、`finished_recent`；心跳陈旧只报"疑似主线程阻塞"。
- **面板停止不再等 socket 超时**：停止时关闭正在读取的 SSE 连接，读取线程立即
  退出（此前要等 15s keepalive 或最坏 1800s）；新增 `cancel_requested` 即时事件
  的界面反馈"已请求取消…"，与终态的"已停止"明确区分。
- **仿真可取消了（hpeesofsim 子进程）**。`keysight.edatoolbox` 的
  `run_netlist()` 既没有 `timeout` 也没有 `cancel` 参数（底层是一句
  `subprocess.check_call`），插件改为在 `CircuitSimulator._execute` 处自己
  `Popen` hpeesofsim 并登记 pid：取消请求即终止、`sim_timeout` 到期即终止并
  标记 `timed_out`（不再只是"结束等待、仿真照跑"），子进程输出重定向到文件而
  非管道，大量 stdout/stderr 不会互锁。终止只针对本插件 spawn 的 pid 树
  （`taskkill /T /F /PID`），绝不按进程名批量杀 ADS 相关进程。被自己终止时
  返回 `status=cancelled|timed_out` + `partial=true`，不谎报成功。
  **这是插件接管子进程实现的能力，不是 SDK 支持的能力**；网表生成阶段仍在
  主线程、仍不可中断。
- **测试**：新增 `test_compute_worker.py`(13)、`test_compute_manager.py`(15)、
  `test_compute_turn_cancel.py`(7)、`test_sim_cancel.py`(7)、
  `test_pump_budget.py`(13)、`test_run_python_boundary.py`(9)、
  `test_panel_stop_latency.py`(6)、`test_chat_cancel_http.py`(2)、
  `test_submit_timeout_state.py`(1)、`test_layout_golden.py`(4)、
  `test_save_throttle.py`(8)、`test_health_compute.py`(4)；
  `tests/_harness.py` 现在会把**后台线程未捕获异常**判为失败（此前容量测试的
  后台 `TimeoutError` 泄漏仍显示 PASS）。
- **配置**：新增 `[agent] compute_*` 七项、`sim_cancel_grace_s` 与
  `tool_queue_capacity` / `pump_max_jobs` / `pump_time_budget_ms` /
  `active_job_limit` / `gui_heartbeat_ms`，默认值见
  [config.example.ini](config.example.ini)。
- **兼容性**：`run_python` 短脚本能力、常驻环境、备份额外校验与 ADS 版本门禁
  全部保留；`tools.call` 的 `job_id` 是可选参数，旧调用签名继续可用。
- **已知限制**：ADS 原生调用（建图、仿真网表生成、`run_python` 里对 ADS 数据库的
  写入）无法安全中断，取消只能在该调用返回后生效 —— 界面会如实显示这个边界，
  不承诺"任意时刻立即停止"。计算子进程隔离是**资源隔离**，不是安全沙箱。

### RF 计算器内核升级（2026-11-03）

`backend/rfcalc.py` 纯内核升级，**只增不改**（既有函数签名/返回键/单位口径
零变化；公式全部取自公开教科书/论文并在 docstring 标注出处，无任何代码
派生）：

- **新增计算能力**（8 个公开函数 + `CONDUCTORS` 电阻率表）：
  定阻衰减器 `attenuator_pi/t/bridged_t/lpad`（含 L 型固有输出失配的如实
  输出）、`vswr_convert`（VSWR↔|Γ|↔回波损耗↔失配损耗互算）、
  `skin_effect`（趋肤深度+表面电阻，Cu/Al/Au/Ag 可参数化）、
  `coax_te11_cutoff`（TE11 截止+单模适用上限口径说明）、
  `lc_resonator`（f0/特性阻抗/Q/带宽/SRF 与寄生告警）。对拍数值表见
  [docs/RF计算器与对拍清单.md](docs/RF计算器与对拍清单.md) 用例 24–28。
- **稳健性**：校验口径统一（NaN/±Inf 在 `_num`/`to_si` 即拒，错误信息带
  参数名与合法范围）；`_bisect_monotonic` 括号区间自适应（lo 减半/hi 加倍
  自动扩展，微带 500Ω@FR4 这类超出初括号的目标现在可解；真无解时报错附
  两端函数值）；同轴/方同轴/双线/线过地板综合对极端 Z0 的溢出改为明确
  CalcError；BJT 偏置负 VRE、级联噪声非 dict 条目、容差非数值等奇异输入
  路径全覆盖；"永不抛异常"契约经坏输入矩阵验证（25 个新增/修复调用点 ×
  11 类坏输入 = 275 次调用零异常）。
- **测试**：`tests/test_rfcalc.py` 63→74 项（新增衰减器金标准+ABCD/节点
  电学校验、VSWR 往返+与 refl_from_z 交叉一致、趋肤文献值与标度自检、
  TE11 与 coax_analysis 交叉一致、LC Q/SRF、二分扩展、奇异输入全
  ok:False），全部通过；全仓 `tests/run_tests.py` 无新增回归（其中
  `test_sim_provenance.py`/`test_model_attachments.py` 为既有环境性失败，
  已 stash 验证与本次改动无关）。

### RF 计算器界面与示意图升级（2026-11-03）

`addon/ads_agent/rfcalc_panel.py`（及 appcad_tl/circ/spec 规格模块）界面与
29→33 页示意图优化；布局数值全部来自本地重测量，文案为自撰：

- **默认窗口完整可见**：页面固定 966×669（规格版式），旧默认窗宽 1000px
  实际裁掉页面右缘 84px（预存问题）；默认尺寸改按 118px 侧栏 + 页宽 +
  Web 条/状态栏完整可见取值（1088×728），快照取证同步同宽。
- **Web 链接条截断修复**：各规格页底部 Web 文字超宽被两端裁切（预存），
  改为逐级降字号直至装入规格矩形（下限 6pt）。
- **示意图渲染质量**：矢量绘制统一开抗锯齿/cosmetic pen；逐页修正标签
  错行、度数符号、悬空箭头、标注脱框与互压（Smith/Balun/Thermal/检测器/
  RFIC/FET 分压与自偏/PAE/反射等页）；尺寸线贴边、长注释换行。
- **视觉令牌与交互**：新增 ACCENT/悬停/只读等调色板令牌并全线走
  `U()` 缩放；输入框焦点环、只读底色统一；侧栏重写为导航组件
  （↑↓/Home/End/←→ 键盘导航、三态样式、可访问名与状态提示、修复重建
  残留控件）；状态栏统一「当前页/计算完成/失败」口径；窗口快捷键提示
  常驻；移除界面文案中的违规参照词。
- **内核新能力接入界面**：新分组「扩展工具」+ 4 个新页 —— 衰减器设计
  （Pi/T/桥T/L 四拓扑电阻与失配）、VSWR/回损换算、趋肤效应、LC 谐振器
  （Q/带宽/SRF）；同轴页新增 TE11 截止/建议上限/单模判定只读行；
  示意图全部 QPainter 矢量自绘，objectName 契约与 F4/F3/F8 流程不变。
- **版式冲突修复**：反射计算页右栏结果标签与 Γ/T 列互压、互调页顶部
  输入行互压、巴伦页结果栅格与图内 Zload 输入框重叠（栅格下移至图区
  之下）——三处均为默认窗宽下可复现的真实重叠。
- **测试**：`tests/test_rfcalc_panel.py` 27→33 项、`test_ui_navigation.py`
  38 项、`test_qtcompat.py` 8 项全部通过；快照取证 33 页 + 整窗
  （`--snap` 输出与默认视图同宽）。

### RF 计算器八页版式对齐与 chrome 修正（2026-11-03，第二轮）

针对实机截图逐页复核后重做 8 页（cpw/trough/stripline/rfic_v/rfic_i/
det_large/bjt_base/fet_self）与页面公共 chrome。做法：离屏渲染 + 与参考截图
做像素对拍（`tmp/gen/pagecmp.py`：mismatch% + 控件审计 clipped/overlap），
逐页改到「零裁字、零互压」。

- **字体根因**：页面 QSS（HarmonyOS/13px）会覆盖子控件字体，导致 8pt 标签
  被换成更宽的字形后整词裁切（"mif"/"no"/"ma"）。规格页改为控件级
  `Microsoft Sans Serif 8pt`（TTF，与实机 MS Sans Serif 度量兼容），
  `_abs_text` 按 `horizontalAdvance` 自动扩宽并保持对齐锚点，从机制上消除裁字。
- **菜单与窗框**：按实测重排——菜单变体（4 项页 File/Calculate/Options/Help，
  5 项页多 Select Parameters）、文字 y=15、分隔线 y=30、主菜单按钮
  880,36,78,16；顶部改为灰条/白面板/压边三段，窗框改为左右 x0..5、x960..965
  自 y=9 起 + 底部高光边。
- **Web 链接条**：改为逐页实测配色与显隐（浅黄底/白底/深蓝底等 + 对应文字色，
  并加 1px 内凹边框）；参考图上为空白条的页（microstrip/cpw/stripline/parallel）
  不再绘制链接文字，重复的窗口级底条隐藏。
- **示意图黑块根因（新增 `_spec_clean`）**：逐像素矢量化把「细线墨迹闭环」
  误当实心多边形填黑，导致基片/正面被整块黑矩形盖住。按页删块/改描边/补回
  被吞掉的尺寸线，并重设标注字（L/H/T/W/G 13.5pt 粗、D1/D2 12pt、εr 14pt 斜体）。
- **电路三页改绝对坐标画布**：rfic_v/rfic_i/det_large 从左右 VBox 流式布局
  改为 966×669 绝对定位（新增 `_CanvasDg`/`_canvas_page`/`_abs_*` 基础设施）；
  rfic_i 的 Vd 三段改回竖排 + 手绘花括号，热分析表 298×104 关闭滚动条；
  det_large 重画检波器电路并补齐二极管参数卡与 Pin Start/Stop/Step 行；
  rfic_v 补回频率响应曲线、Design Rule 单选组与 Band1/Band2 双列结果。
- **cpw 选项如实化**：补回参考图的 With/No Groundplane 单选组，但引擎
  `cpw_analysis(s,g,h,er)` 未建模该选项，故控件置灰 + tooltip 说明，页面
  说明文字写明「引擎未建模」，不伪造功能。
- **对拍结果**（mismatch%，均 issues 为空）：cpw 12.56→10.24、trough
  10.52→8.81、stripline 9.12→7.61、rfic_v 11.12→6.15、rfic_i 13.52→8.51、
  det_large 9.83→5.14、bjt_base 7.53→7.61、fet_self 7.62→7.72。
- **测试**：`test_rfcalc.py` 74 项、`test_rfcalc_panel.py` 33 项、
  `test_ui_navigation.py` 38 项、`test_qtcompat.py` 8 项全部通过；
  快照 `tmp/snap_v2/`（33 页 + 整窗）。
- **已知差距**：Qt 8pt 文字比参考图 VB6 位图字宽约 15~25%，是剩余 mismatch
  的主要成分；microstrip 等未纳入本轮的页仍有同类「黑块」描摹问题；
  det_large 参考页本无 Pin 表格，因测试契约保留在空白区。


## 1.3.0 — 2026-10-30

- 新增 RF 计算器窗口（Tools ▸ ADS Agent ▸ "RF 计算器…"）：
  传输线（微带/带状线/同轴圆/方/平行双线/线过地板/CPW/板线）、级联噪声、
  混频器杂散、互调、BJT 固定基极偏置/分压偏置、FET 自偏/分压偏置、放大器
  效率与反射工具共 18 页。
- **版式参考网上资源**：按公开可见的射频计算页面版式逐页绘制
  （顶点坐标/渐变色/绘制顺序/尺寸线端点/输入框 bbox，规格存
  `appcad_shots/spec_v2/`），18 页示意图全部按参考版式重画；
  互调图补 P1dB 曲线、截点、Sig/IM 引导线与 IM dBc 框；NoiseCalc 补
  逐级 dNF/dG/dIP3 灵敏度行；按钮热键统一方括号 [F2]/[F3]/[F4]。
- 新增 **线过地板 Wire Over Groundplane** 页：引擎镜像法
  Z0=(60/√εr)·acosh(2h/d)（2h/d≫1 退化 ln(4h/d)），含 Calculate H 综合
  （同式反解 h）。
- 新增 **BJT 固定基极偏置** 页（RB 单电阻固定偏置拓扑）：
  Ib=(Vcc−VBE)/RB、Ic=β·Ib、Vce=Vcc−Ic·RC，稳定性系数 S=β+1，
  Vce≤0.3V 饱和告警；原分压偏置页保留并存。
- 窗口参考网上资源的射频计算页面版式：黄色计算页（结构图内嵌尺寸
  输入框、右列结果行、F3/F4 快捷键、底部 Web 条）、白底偏置页（原理图 +
  参数分组 + 编号说明 + 结果表）、NoiseCalc 栅格页（级数 + 逐级列头 +
  Stage Data/Analysis 分区 + 底部参数/结果盒 + 状态 LED）；Outlook 式
  分组侧栏（矢量自绘图标）。引擎未建模的列/盒保留版式并灰显注明。
- 新增引擎 `backend/rfcalc.py`：纯函数、纯标准库、SI 单位进出、永不抛异常
  （非法输入返回 `{"ok": False, "error": ...}`）；46 项金标准离线测试
  全绿。公式出处（Hammerstad-Jensen、Cohn、Wen、Friis、镜像法等）见
  源码 docstring。
- 方同轴等效半径常数按网上资源公开数值三点定标（D1/D2=2.326→55.1Ω、
  1.25→17.9Ω、Z0=50 反算 D2=46.853mil 全部复现），并修正首版把直径系数
  当半径的定标错误；综合增加内导体越界校验。
- 窗口与后端共享同一引擎实现（addon 经 pathbridge 按路径加载），无重复逻辑。
- 为后续 LLM 工具接入预留：引擎函数即未来工具实现，本次只发布 UI。
- 已知边界：本功能版式与功能参考网上资源，公式均出自公开文献，
  与任何专有软件无代码派生关系；数值对拍清单见
  [RF 计算器与对拍清单](docs/RF计算器与对拍清单.md)。

## 1.2.0 — 2026-10-09

- 新增原厂模型 ZIP 附件上传、检查、显式解压导入和 ADS Design Kit 挂接流程。
- 新增共享模型库：上传的模型包备份到 ADS 目录下的 `libraries`，可从任意工作区检索并导入。
- 新增厂商模型清单、料号搜索、只读浏览和导入验证工具。
- 仿真结果记录外部模型依赖指纹；模型内容变化或依赖无法确认时不复用旧结果。
- 完善上传错误恢复、工作区固定绑定、模型导入状态与进度反馈。
- 保留 ADS 2024–2027 兼容层和 1.1.2 启动及健康检查修复。

## 1.1.2 — 2026-10-08

- 修复 1.1.0 / 1.1.1 八处 Qt 模块获取函数误用，恢复面板与工具服务启动。
- 修复健康路径重复拼接导致的 401、后端离线误判与启动超时。
- 修复空 ADS 目录误报跨版本冲突，补齐实际目录身份信息。
- 工具服务关闭时停止定时器并释放端口，增加真实 Qt / HTTP 启动回归测试。

## 1.1.1 — 2026-10-08

- 优化模型设置：供应商侧栏、搜索、自定义名称与响应式双栏布局。
- 同一供应商 API 支持多个模型，共用连接参数；输入栏向上菜单可切换模型。
- 修复已有供应商地址、名称与密钥载入；密钥默认隐藏，新增供应商清空凭据。
- 精简重复提示，修复添加按钮文字裁切，统一浅色与深色主题。
- 同步结果页的响应式排版和数据窗口主题，避免窄窗口操作按钮裁切。
- 增加供应商配置、鉴权、模型切换和界面回归测试，保留 ADS 2024–2027 兼容层。

## 1.1.0 — 2026-10-07

- **跨版本适配（ADS 2024–2027，Windows x64）**：新增集中兼容层——
  版本识别（Windows 卸载表权威 + buildInfo 架构 + 目录名弱证据）、
  运行时能力检测（三态、只读探测）、按功能门禁与统一错误提示。
- **ADS 2024–2026 为实验性适配**：已完成官方文档研究与离线验证，
  **未实机验证**。读/聊天按能力检测开放；写/建图/仿真/任意代码按年份
  显式开启（`[compat] experimental_<年份>`），开启不代表验证通过。
- **Qt 绑定适配**：PySide6（2026+）/ PySide2（2024/2025）二选一，
  跟随宿主进程已加载绑定，绝不混用；差异 API（QFontDatabase、exec）shim。
- **多版本安装管理**：`--ads-dir` 可重复、`--all`；各版本分别注册互不覆盖；
  程序目录按版本隔离；安装登记表（ads_installs）迁移旧单值记录；
  卸载只动选定版本；32 位 Windows 明确拒绝。
- **实例身份加入 ADS 版本维度**：跨版本占用端口识别为
  `cross_version_conflict`，不误连。
- 门禁 fail-closed：能力缺失/门禁故障时拒绝执行，不宽泛兜底、不盲目重试。
- 新增 `docs/版本证据报告.md`、`docs/兼容性矩阵.md`、
  `tools/real_machine_check.py`（未来取得旧版实机后的验收脚本）。
- CI：Python 3.12 + 3.14 双版本矩阵（版本选择有官方/实机依据）。

## 1.0.1 — 2026-10-07

- 完善 ADS 2027 插件的安装、部署、迁移、升级与卸载。
- 分离程序与用户数据，增加实例身份校验和发布白名单。
- 修复流式工具调用返回。
- 建立 GitHub 测试与安装包发布流程。

详细说明见 [1.0.1 发布说明](docs/releases/1.0.1.md) 与
[1.1.0 发布说明](docs/releases/1.1.0.md)。
