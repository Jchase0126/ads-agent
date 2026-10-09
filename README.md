# ADS Agent —— ADS 2027 内置 AI 助手插件

[下载安装包](https://github.com/Jchase0126/ads-agent/releases) · [安装说明](docs/安装说明.md) · [版本迭代与发布](docs/版本迭代与发布.md) · [更新记录](CHANGELOG.md)

源码、测试和构建脚本保存在本仓库；带版本号的安装 ZIP 在 GitHub Releases 下载。
当前支持 Windows + ADS 2027，提供 ZIP 与 BAT 安装入口。图形 EXE/MSI 安装程序和插件内自动更新尚未实现。
仓库当前为私有，下载需要访问权限；尚未选择开源许可证。

原理图自动建图与仿真的故障复盘及操作准则见 [原理图建图与仿真经验总结](docs/原理图建图与仿真经验总结.md)。

## 安装 / 升级 / 卸载（完整说明见 docs/安装说明.md）

分发包是一个带版本号的 ZIP（``ADSAgent-<版本>.zip``，由 ``tools/build_release.py``
生成）。解压后：

```
安装 ADS Agent.bat      自动识别 ADS 2027 → 注册 → 重启 ADS → 面板内配 API 即可用
卸载 ADS Agent.bat      只移除本插件的注册与程序文件，默认保留用户数据
环境自检.bat            解释器 / ADS 目录 / 数据与程序目录 / 令牌 / 注册 / 实例身份
```

**不需要另外安装 Python、Anaconda 或 PySide6** —— 安装与运行都优先使用
**ADS 2027 自带的解释器**（``<ADS>\tools\python\python.exe``），PySide6 与
``keysight.*`` 也是 ADS 自己提供的。

装好后**程序文件与用户数据是分开的**（这是升级不丢配置、卸载不误删的前提）：

| 类型 | 位置 | 卸载时 |
|---|---|---|
| 程序文件 | ``%LOCALAPPDATA%\Programs\ADSAgent``（默认） | 可选删除 |
| 配置 / 令牌 / 会话 / 设计任务 / 日志 | ``%LOCALAPPDATA%\ADSAgent`` | **默认保留** |
| ADS 注册记录 | ``<ADS>\config\eesof_addons.xml`` 里的一条 | 被移除 |

路径解析统一在 ``backend/paths.py``（后端、面板、工具服务、启动器、鉴权、自检共用）。
从旧版本（数据放在代码目录旁边）升级时，旧数据会被**复制**到新位置，旧文件
**原样保留不删除**，且不会覆盖新位置已有的有效数据。

仿真产物（网表与 .ds 数据集）仍然写在用户的 ADS 工作区
``<工作区>/ads_agent_sim/`` 下 —— 那是用户自己的工作区，不属于插件数据。

在 Keysight PathWave ADS 2027 内部运行的 AI Agent：聊天面板（PySide6，官方 add-on 机制）
+ 本机后端（LLM 对话循环）+ 回环工具服务（在 ADS 进程内执行操作）。

```
┌─────────────────────────── ADS 2027 进程 ───────────────────────────┐
│  启动即自动：拉起后端 + 打开右侧 Agent 面板                             │
│  Tools ▸ ADS Agent ▸ 打开对话面板                                      │
│  ┌──────────────────────┐   ┌─────────────────────────────────────┐  │
│  │ PySide6 聊天面板 (UI) │   │ 工具执行服务 toolserver (localhost)   │  │
│  └──────────┬───────────┘   │  keysight.ads.de / dataset / 仿真    │  │
│             │ HTTP/SSE      └──────────────▲──────────────────────┘  │
└─────────────┼──────────────────────────────┼─────────────────────────┘
              ▼                              │ 回环执行工具
┌─────────────────────────────────────────────┴─────────────────────────┐
│ 后端 (ADS 捆绑 Python 隐藏子进程，自动启动)：LLM 对话循环 + 函数调用       │
│ 日志: logs/backend.log（手动备用: start_backend.bat）                    │
└───────────────────────────────────────────────────────────────────────┘
```

对话区为现代气泡式 UI：居中欢迎页带可点击的建议问题、你/AI 头像气泡、
工具调用以紧凑卡片展示、底部圆角输入框（**Enter 发送**，Shift+Enter 换行，
↑ 圆形发送键，**模型选择就在输入框左下角**），右上角 🌙 可切换深色/浅色主题。

**自适应与圆润**（`addon/ads_agent/uiscale.py`）：全部尺寸、字号、圆角走统一
设计令牌，缩放系数跟随系统/ADS 字号，也可用 `config.ini [ui] scale` 手动覆盖；
气泡宽度按面板宽度取比例（不写死像素）、输入框随内容长高、侧栏/模型清单高度
随面板变化、面板过窄时自动收起侧栏、建议卡片自动 1~3 列排布。字号与图形
（圆形头像、圆角卡片、圆角滚动条）都按同一系数缩放，不会有"字变大了但按钮
没变"的错位。

**项目列表**（左侧 ☰ 展开/收起，组头 ▾ 可再折叠）：每个项目保存**独立的
对话记录**，点击切换、➕ 新建、选中后 🗑 删除；对话实时存入 `projects.json`，
重启 ADS 后继续上次进度。会话写入采用原子替换，上一份有效文件保存在
`projects.json.bak`；主文件损坏时启动会从备份恢复并在状态栏提示。

## 原厂模型压缩包：上传 → 按需导入 → 选型 → 仿真

用户可以把原厂模型包（村田 / TDK / Infineon 等）当**附件**发进聊天面板。
原始 ZIP 存进当前 ADS Workspace 的 `ads_agent_models/`，**上传只保存并检查包
结构，不自动解压、不加载套件、不执行包内脚本**；真正导入由用户按需触发
（点附件卡片上的「解压并导入」，或明确说「导入之前上传的 TDK 模型」）。

### 怎么用

1. **上传**：先在 ADS 主窗口中打开目标 Workspace，再点输入框旁的 📎 选
   `.zip`，或拖入 ZIP、复制文件后按 `Ctrl+V`。输入栏会显示可移除的文件卡片，
   可以只发附件、不打字。上传前先检查鉴权与工作区；未打开 Workspace 时会
   直接提示，不发送 ZIP 内容。聊天项目名称与 ADS Workspace 是独立的。
   附件保存**不依赖 LLM 服务连通**，模型 ZIP 保存到当前工作区并备份到统一 `libraries` 目录。
   未配置时，首次确定的 `libraries` 位于 ADS Workspace 的父目录，与工作区同级，并持久化该位置；之后切换到其他路径的工作区仍使用同一目录。可通过 `[models] library_root` 或 `ADS_AGENT_LIBRARY_ROOT` 更改位置。
2. **看识别结果**：附件卡片显示文件名、大小、所属工作区、状态与识别出的包
   类型（Touchstone 模型包 / ADS Design Kit / 混合包 / 未识别）。点「查看模型」
   可展开识别依据与型号索引。
3. **按需导入**：点「解压并导入」。导入是后台任务，卡片轮询显示进度
   （检查中 → 待导入 → 导入中 → 待验证）；导入中可以「取消导入」。
4. **选型与验证**：直接对 Agent 说「列出 TDK 包里的电容」「用 GRM155R71C104KA88D
   建个 2.4G 匹配网络」。Agent 会先查真实存在的库名/cell，再验证可用性，
   然后建图、仿真、评估。
5. **进入优化工作流**：验证通过的模型与普通元件一样参与
   `publish_design_result` 与后续迭代优化。

### 模型工具

| 工具 | 能力 |
|---|---|
| `list_model_packages` | 列出当前工作区的模型包（只读清单，不解压） |
| `list_shared_model_packages` | 搜索 ADS `libraries` 统一备份目录中的模型包 |
| `search_shared_models` | 在共享 ZIP 中按具体型号、cell 或库名查询包内文件证据 |
| `inspect_model_package` | 识别依据、候选套件根、文件统计、型号索引 |
| `import_model_package` | 安全解压 → 建索引 → 挂接到**本工作区**（幂等） |
| `import_shared_model_package` | 从统一备份复制到当前工作区，再安全解压并只读挂接 |
| `list_vendor_models` | 在已挂接库（含**只读**原厂库）里检索元件 |
| `get_vendor_model_info` | 单个元件的 model_def 参数定义与模型元数据 |
| `validate_model_import` | 验证"真的可用" —— **就绪状态的唯一依据** |

### 状态与"就绪"的含义

`saved` 已保存 → `inspecting` 检查中 → `pending_import` 待导入 →
`importing` 导入中 → `pending_verify` 待验证 → `ready` 已就绪；
另有 `failed` / `cancelled` / `awaiting_user`。

**解压结束只标到"待验证"，绝不会标"已就绪"** —— 只有
`validate_model_import` 通过才会写 `ready`。界面上"已导入"和"模型可用"是
两件事：挂接成功只说明库在库里了，元件能不能真正放进电路仿真要另外验证。

### 包类型与支持范围

| 类型 | 判定 | 处理方式 |
|---|---|---|
| Touchstone 文件包 | 含 `.s1p/.s2p/.sNp/.ts` | 不按 Design Kit 安装；通过 ADS 的 S 参数元件引用文件路径 |
| ADS Design Kit | `lib.defs` + 库结构（`.oalib`/`circuit/ael`/`design_kit/` 等） | 保持原厂内部目录结构解压，按官方 API 挂接到当前工作区 |
| 混合包 / 未知包 / 其它仿真器模型 | 其它 | 如实显示检测结果与支持范围，**不硬套** Design Kit 导入 |

#### Design Kit 仿真依赖

挂接 `lib.defs` 中的库只证明 ADS 能看到库和 cell，不代表模型已进入仿真网表。不同厂商的 Design Kit 可能还要求专用 Netlist Include 元件、`models.net` / 编码模型库、AEL 启动定义、Verilog-A、数据文件或 `ADSlibconfig` 路径映射。验证前要检查这些依赖，并在隔离测试电路中放入必需的 include 元件；生成网表后核对实际的 `#include`、`#uselib`、`#load` 等引用。仿真调用若需要 PDK 根目录，也必须显式传入并验证路径映射。只有 ADS 产生数据集且读到预期曲线，才算实机仿真通过。

TDK v2019.10 实测中，漏放 `TDK_Netlist_Include` 会导致模型未定义；加上它后，仍需给仿真器提供可解析的 `TDK_Encoded_Lib` 映射。该要求是这套 TDK Kit 的实测结果；对其它厂商必须从其包内结构和生成网表中重新确认，不能照搬 TDK 元件名或路径。

首版**只支持 ZIP**；`.tar/.rar/.7z` 明确提示暂不支持。

### 资产目录

```
<工作区>/ads_agent_models/
├── archives/<package_id>/<原始文件名>.zip   # 原始 ZIP（永不与解压内容混放）
├── extracted/<package_id>/<原厂内部结构>   # 解压产物（保持原厂目录结构）
├── .tmp/                                   # 解压暂存，完整检查通过才提交
└── manifest.json（+ .bak）                  # 资产清单（原子写入 + 跨进程锁）
```

- `package_id = "pkg_" + ZIP 的 SHA-256 前 16 位` —— 稳定、不依赖文件名；
- 同工作区**相同内容**重复上传 → 复用已有资产（`ref_count` +1），不重复占空间；
- **同名但内容不同** → 各存各的，不静默覆盖；
- 清单里的路径一律**相对工作区**保存，工程迁移后用 `relocate()` 重新定位，
  `verify_references()` 核对引用是否还指向真实文件；
- **聊天项目只保存附件引用。删除聊天项目或清空会话不会删除模型资产**
  （资产层刻意不提供删除入口）。

### 安全

压缩包按"不可信输入"处理：检查真实 ZIP 格式与损坏情况；拒绝路径穿越
（`../`）、绝对路径、盘符路径、UNC；拒绝符号链接条目；限制文件数量、
累计解压大小与压缩比（zip bomb）；**包内脚本一律只落盘不执行**；包内
README / 文件名 / 说明**只当数据**，不作为对模型的指令。

解压走"临时目录 → 完整检查 → 提交"，失败时**保留原始 ZIP**、清理本次
临时产物。同一工作区的导入串行化，同包重复导入幂等（不会产生重复挂接）。

上传通道是**独立的二进制接口**（`POST /models/upload`），不用 Base64、
不走 JSON：文件内容不进聊天历史、不进 LLM 请求，LLM 只拿到 `package_id`
与元数据。真实原厂包 14~41 MB 起，JSON 接口统一限 8 MB，所以必须走单独通道。

### 工作区绑定与只读约束

- 目标工作区**只认 ADS 当前打开的那个**，请求里带的路径不采信 —— 否则模型
  一句"导入到 D:\\…"就能写到工作区之外；
- 每次 ADS 写入/操作前复核工作区，切换工程后停止依赖当前工程的操作并说明原因；
- 原厂库以 **`LibraryMode.READ_ONLY`** 挂接，只为浏览与引用，**不给原厂库写
  权限**；挂接限定在目标工作区，**不动全局 Favorite Design Kit 设置**；
  修改库定义文件时用官方 API，不做字符串拼接（退路才改文件，且备份+去重+可回滚）；
- 重复导入前会核对工作区里是否已有同名同路径库 → 幂等，不产生重复挂接；
- 用户在 ADS 里手工 Unzip 过套件后，重新扫描能识别并验证已导入的套件，不会重复安装。

### 模型选择的硬约束

- LLM **必须先查真实模型**再选型，绝不编造库名、cell、参数或型号；
- 供应商名字不足以唯一确定型号时，列出候选项让用户选；
- **固定型号的 S 参数模型不能当连续电容/电感值修改** —— 外围连续参数优化
  与原厂型号离散选型分别处理；
- 模型的频率范围覆盖不到目标频段时**明确提示，不静默外推并报告达标**；
  偏压/温度只用具有对应条件的模型，不自行推断；
- 型号切换时核对端口定义、参数与连接方式；
- 用户没提优化任务时，不因为导入了模型就自动开始优化。

### 结果追溯与缓存失效

每次仿真记录实际使用的模型包 ID、ZIP 哈希、模型文件哈希与元件定义指纹。
**网表指纹不足以代表模型版本**：同一个 `.s2p` 路径下的文件内容被原厂换掉时，
网表一个字都不会变，但仿真结果完全不同。因此结果复用除了比网表指纹，还要
比**模型依赖指纹**；拿不到当前模型指纹时按保守策略重新仿真，不给过期结论。

### 已知限制（未实机验证的部分）

- Design Kit 的**实机挂接**（`Workspace.add_library` / `add_library_definition_file`）
  与元件放置、基础仿真验证，需要在真实运行的 ADS 2027 里执行；相关代码依据
  本机 ADS 2027 安装目录的 Python 包源码核对（`tools/python/packages/keysight/
  ads/de/_core/workspace.py`），**尚未在 ADS 实机上跑通端到端**；
- 用户亲手 Unzip 的套件"重新扫描识别"路径已实现识别逻辑，但重复安装的
  判定同样待实机确认；
- 面板附件交互（📎 按钮、拖入 ZIP、卡片操作、轮询、恢复）已完成实现，
  **PySide6 界面行为待实机验证**。

## 目录结构

```
ADS Agent/
├── config.ini               # 共享配置（LLM Key、端口；[ads] token 首次启动自动生成）
├── install_addon.bat        # 注册插件到 ADS（写入 eesof_addons.xml，自动备份）
├── uninstall_addon.bat      # 卸载插件
├── start_backend.bat        # 启动后端
├── check_env.py             # 环境自检（含令牌状态、两端一致性、回归测试）
├── design_jobs/             # 设计任务（结果页）的持久化记录 <job_id>.json
├── backend/                 # 后端：agent.py 对话循环 / llm.py LLM 客户端 /
│                            #       tools.py 工具派发 / server.py SSE 服务 /
│                            #       design_job.py 任务与状态机 /
│                            #       design_metrics.py 确定性指标评估 /
│                            #       design_service.py 仿真→读数→评估 编排 /
│                            #       model_store.py 模型包资产（保存/清单/安全解压/识别/索引）/ │
│                            #       model_tools.py 模型工具编排（工作区绑定/导入流水线）/ │
│                            #       ads_auth.py 回环令牌（唯一来源）/ adslog.py 文件日志
├── tests/                   # test_auth.py 接口鉴权 / test_token_migration.py 令牌迁移 /
│                            # test_config_write_race.py 配置并发写入 /
│                            # test_sim_output_dir.py 仿真目录唯一性 /
│                            # test_toolserver_busy.py 忙碌状态 /
│                            # test_design_metrics.py 指标计算 /
│                            # test_design_job.py 任务状态机与编排 /
│                            # test_project_isolation.py 会话隔离 /
│                            # test_design_isolation.py 结果页隔离与重启恢复 /
│                            # test_model_store.py 模型包资产（去重/识别/越界/并发/迁移）/
│                            # test_model_http.py 模型包上传与清单 HTTP 接口 /
│                            # test_model_ops.py + test_model_ops_flow.py ADS 侧挂接纯逻辑
└── addon/ads_agent/         # ADS 端插件：__init__.py 菜单注册 / panel.py 面板 /
                             # toolserver.py 工具服务 / ads_ops.py 工具实现 /
                             # model_ops.py 原厂模型挂接/只读库浏览/验证 /
                             # model_attachments.py 附件卡片/上传 worker/拖放过滤 /
                             # result_page.py 设计结果页（自绘曲线） /
                             # authbridge.py 加载共享令牌 / uiscale.py 自适应缩放与圆角设计令牌
```

想改 UI 外观时，装个 `pip install PySide6`，然后 `python tests/ui_preview.py`，
会在 `tests/preview/` 生成浅色/深色、宽/窄面板、放大字号等场景的截图。

## 首次使用（三步）

**1. 安装插件**：双击 `安装 ADS Agent.bat`（会备份并修改
`%HPEESOF_DIR%\config\eesof_addons.xml`），然后**重启 ADS 2027**。

自动识别失败时显式指定：`install_addon.py --ads-dir "你的 ADS 安装目录"`；
先看探测结果：`install_addon.py --detect`。安装器与启动器都优先用 ADS 自带
Python，本机没有装 Python 也能装。

**2. 一切自动**：ADS 启动时插件会**自动拉起后端**（隐藏进程，复用已在运行的
实例，日志见 `logs\backend.log`）并自动打开面板。`start_backend.bat` 仅作
手动备用。

**3. 在面板里配置 API（推荐，无需改配置文件）**：
1. 启动 ADS 2027 —— **Agent 面板自动出现在右侧边栏**（可拖动、可浮动，
   关闭后从菜单 Tools ▸ ADS Agent ▸ 打开对话面板… 再次打开）；
2. 点面板顶部 **⚙ 设置**：选预设（DeepSeek / 智谱 GLM / OpenAI / Ollama，
   选完可直接点「获取密钥」跳转申请页），填入 API 密钥（👁 可切换明文显示）；
3. 点 **检测**（或模型区的 **⟳ 同步模型**）——检测 API 连通性，成功后自动
   拉取该地址的**可用模型清单**；失败会明确提示原因：后端未启动 / 密钥无效 /
   地址连不上 / 服务无模型列表接口；
4. 在「模型」清单中**勾选某个模型 = 立即切换使用它**（对话马上用新模型，
   勾选集合自动保存）；清单顶部有搜索框可快速过滤；加粗行为当前使用中的模型；
5. 直接对话，例如：
   - 「当前工作区里有哪些设计？」
   - 「读取 RFamp 的 VAR 变量」
   - 「把 Rbias 改成 120 Ohm，然后仿真 S 参数，总结增益和 S11」

> 也可以继续用 `config.ini` 的 `[llm]` 段配置 base_url / api_key（面板设置会写回该文件）。

## Agent 能做的事（预置工具）

| 工具 | 能力 |
|---|---|
| `get_workspace_info` | 工作区路径、已挂接库 |
| `list_designs` | 可写库中的设计（cell）与视图 |
| `get_design_variables` | 读取 VAR 变量方程当前值 |
| `set_design_variables` | 修改 VAR 值、保存并只读复核 |
| `run_simulation` | 生成网表 → hpeesofsim 仿真 → 返回数据集 |
| `read_dataset` | 读取 .ds 数值（统计量+样例+CSV 导出） |
| `read_traces` | 读取 .ds 曲线（x/y 数组 + 单位 + 来源）；对话工具默认最多返回 50000 点，设计指标评估请求完整曲线 |
| `open_schematic` | 核对原理图实例并尽力激活已打开的 ADS 窗口 |
| `audit_rf` | 射频物理审查：传输线参数（基板/频率/Z0 闭式核对/电长度）、MTEE 旋转后引脚角色与 W1/W2/W3 宽度一致性、90° 直角连接；读取真实 Layout 铜皮查直角弯折/重叠/间隙/接地（无 Layout 时如实标注「未完成 Layout 验证」）。详见 docs/Layout 审查设计.md |
| `publish_design_result` | **后端本地工具**：跑仿真 → 读真实曲线 → 确定性评估 → 生成结果页 |
| `run_python` | 兜底：在 ADS 进程内执行任意 keysight.ads.de 代码（面板可关） |

设计指标使用完整曲线计算，结果页只保存最多 600 个**显示点**（保极值降采样，
画图用）。查看数据点时会明确标注"当前为显示采样点 / 当前为完整数据"及
降采样算法与点数对比；需要完整数据用结果页的「导出完整数据」——后端从原始
.ds 重新读取并直接导出 CSV，不经过显示点。每条曲线还记录数据质量
（原始/有效/丢弃点数、丢弃原因、覆盖范围、最大采样间隔）。极大数据集会增加
本机内存占用和工具响应时间。

运行 `run_simulation` 前设计里需要已有仿真控制器（AC / S-Param / DC / HB…）。

### 原理图自动布局（layout="auto"）

`build_schematic` 的 `layout="auto"` 按信号流自动排布坐标，已覆盖并实测的
拓扑：

- 一分二威尔金森（对称经典画法：两臂 ±3 行镜像、隔离电阻竖放在两臂出脚
  同列正中、M subscript/变量/控制器自动排位）；
- 前端带并联支路的对称分配器（输入去耦/稳定电容 C=Cin 挂输入端：对称
  部分保持镜像对称，不对称支路竖挂端口引脚正下——单元件、单锚点、锚点
  在输入链上且不在分叉出脚列时收编，否则诚实回退通用布局）；
- 一分三~一分五对称分配器（一分三含直通臂；带隔离电阻的 1分4 会诚实
  回退通用布局——单结隔离本应级联实现）；
- 两级级联一分四（双分叉、双隔离电阻全部对称）；
- 两节耦合线带通滤波器（CLIN：直通边 1→4 为元件轴，主干对齐一行，
  耦合边 2/3 交叉互连自动走正交线）；
- MTEE 类功分器与放大器链（通用路径：主链一行、分支下挂、标注自动
  避让——文字绝不被导线穿过）；
- 共射/共基晶体管放大器（BFR106 三端管：输入行直线接基极/发射极无
  落差绕行；**电源母线**——含直流源的网络自动改为"成员竖挂锚点行
  上方、母线 T 分支、源横放右端、负端横地"；**接地支路组**——同节点
  全部"单件+地"并排下挂节点行，多锚组自动找已放符号之间的空槽；
  下方走廊被其他网络行线截断时自动改上挂倒置地）。

保存后复核返回体含 `netlist_equivalence`（磁盘网表与请求连接的
**逐引脚等价**核对——按"每个网络包含哪些元件引脚"的划分比对，
网络重编号不影响，ok=false 即拓扑被改）与 `geometry.metrics`
（导线总长/绕行系数/拐弯/重复段/平行出线，以及**紧凑度**：主电路
与全图 bbox 宽高面积、最长导线段、超长连接数、接地支路总长——
验收紧凑布局直接读这里，detour_ratio≈1 不等于紧凑）。

**紧凑化口径（2026-09-30）**：链内相邻间距按符号与参数文字的实际
占用逐对计算——简单 R/L/C 链基线 1.4（原统一 1.9），MTEE 等 6 行
参数器件邻接时自动留 1.9 避让通道；通用分支行距按上一行实际内容
（符号+文字+接地支路深度）动态计算，不再固定 5 格；接地支路（两脚
单件+地）优先就近挂所属节点旁成组，不再独占整行分支；共享同一
GND 符号的多元件按伙伴引脚中点就近挂置；竖放桥接件（隔离电阻）的
列位按"自身文字不撞已放参数列、连线横段不穿别件文字"选位；支路
放不进节点跨度时整段平移下游链腾位（不再把支路赶到远端分支行）。
`instances` 全部不写坐标时缺省即按 auto 处理；给了任何坐标则尊重
手工位置（explicit）。已知形态豁免（诚实计数，非静默）：竖放文字
被行线擦边（graze ≤0.15，字体不满铺 zone）、同网共线重叠（共享走廊
/T 分支的目标形态）。

布局回归一键验证：`python tests/layout_regression.py`（七拓扑案例间隙
模型 + 结构测试 + fuzz 冒烟，fuzz 含三端管放大器与双支路同 host 拓扑）。
FM_SC 共基放大器案例（含改名/连接顺序/共享地变体）在
tests/fm_sc_case.py + tests/test_auto_layout.py 固化。

接地位置遵循用户规则：GROUND 沿接地引脚实际出线方向紧靠放置，优先同轴短直连接；S2P 的 Ref 向下时就在其正下方就近接地，避免多余横折。地符号和地线须避开实例名、器件类型与参数文字，优先调整文字而非拉长地线。该规则适用于所有厂商模型。

约束与行为：导线全部横平竖直；文字/导线冲突由内置模型自动避让；连接重复
自动去重；GROUND 朝向按接入方向规范化（实测建档：挂导线底端 270° 正置、
顶端 90° 倒置、右侧 0°、左侧 180°——地符号永远沿所连导线方向展开，模型
乱给的角度会被覆盖）；`layout="auto"` 只用于全新
铺图（recreate 或空设计），增量修改请显式给坐标。多变量 VAR 会按文字
高度自动抬高。布局回归由 5 个拓扑案例的
间隙模型与渲染级复核覆盖（tests/iterate_ml_sym_metrics.py、
tests/render_check_sym.py）。

## 设计闭环：输入指标 → 设计仿真 → 结果页

用户可以直接给**指标要求**，例如：

> 设计一个 2.4 GHz 放大器，带内增益至少 15 dB，S11 低于 −10 dB

插件会把它整理成可检查的任务、跑设计仿真、最后在当前项目里生成一张
**可再次打开的设计结果页**。流程与职责划分：

```
用户要求
  │  ① 整理成可检查的任务：设计引用 + 频段 + 指标定义（目标值/判据/表达式/单位）
  ▼
DesignJob(planned)           ← backend/design_job.py（状态机 + 持久化）
  │  ② 建图 / 改图（复用现有 ADS 工具：odesign / put / setp / save…）
  ▼
DesignJob(building) → (simulating)   ← run_simulation（主线程出网表 + 后台线程仿真）
  │  ③ 读**真实**曲线：read_traces（完整数组 + 单位 + .ds 来源）
  ▼
DesignJob(evaluated)         ← backend/design_metrics.py（确定性评估）
  │  ④ 目标 / 实测 / 判定 / 频点
  ▼
DesignJob(done | evaluated)  →  design_jobs/<job_id>.json
  │  ⑤ 面板渲染
  ▼
结果页（addon/ads_agent/result_page.py）
```

### 结果页包含什么

| 内容 | 说明 |
|---|---|
| 设计引用 | 工作区 / 库 / cell / view，以及**「在 ADS 中打开原理图」按钮** |
| 曲线 | 由真实 `.ds` 数据自绘（QPainter）：横纵轴名称与单位、数据来源路径、图例、悬停查点 |
| 指标表 | 每项：目标值 / **实测值** / 判定（达标·未达标·无法判定） / **对应频点**，附数据说明 |
| 仿真状态 | `done` / `failed` / `reused` / `no_dataset`、结束时间、**输出目录**、网表与数据集路径 |
| 错误信息 | 仿真或读数据失败时的原文（失败时页面仍然生成，产物路径一并保留） |
| 数据点检查 | 「查看数据点」逐点列出原始数值（带单位与来源），可一键复制成 CSV |
| 重新仿真 | 「重新仿真」按原指标重跑；「重新评估」只重读已有 `.ds` 重算指标 |
| 重启后可打开 | 项目里只存 `job_id`，完整任务在 `design_jobs/<job_id>.json`；重启 ADS 后从项目列表点开即可 |

### 谁说了算：数据 vs 模型

- 模型只能提供**设计与指标定义**（要评估哪个设计、看哪些表达式、判据与目标值）。
- 实测值、达标结论、对应频点**全部**由 `design_metrics` 从真实数据算出。
- 模型即使把 `actual` / `pass` 填进参数也会被丢掉（`design_service.METRIC_KEYS` 白名单）。
- 数据不足时结论是 **`无法判定`**，绝不猜：表达式不存在、频段内没有数据点、
  没给目标值、数据被截断 —— 都会在结果页的"说明"列里写清楚原因。

### 指标类型

| kind | 含义 | 默认判据 |
|---|---|---|
| `min_in_band` | 带内最小值（带内增益 ≥ 15 dB） | `>=` |
| `max_in_band` | 带内最大值（带内 S11 ≤ −10 dB） | `<=` |
| `mean_in_band` | 带内均值 | `>=` |
| `flatness_in_band` | 带内起伏（max−min） | `<=` |
| `bandwidth_above` | y ≥ threshold 的连续带宽（target 是**要求的带宽**） | `>=` |
| `value_at` | 指定频点上的值（`at_hz`） | `>=` |

### 状态流转

`draft → planned → building → simulating → evaluated → done`，任何一步失败进
`failed`（产物保留）。未达标时从 `evaluated` 回到 `building` / `simulating`
再迭代 —— 这是一条**正常路径**，每次迭代都追加一条记录。非法流转会被
`IllegalTransition` 拒绝，不会悄悄把 failed 改成 done。

## 安全设计

- **可操作接口一律要令牌**：后端 `/chat` `/config` `/tools` `/logs`
  `/test_connection` 与 ADS 端 `/execute` 都校验请求头 `X-Ads-Agent-Token`；
  只有 `/health` 开放（插件启动探测、`check_env.py` 靠它判断服务是否活着）。
  `/chat` 配合 `allow_python` 等于"在 ADS 进程里执行任意代码"，所以这不是可选项。
- **令牌不是写死的**：首次启动 / 安装时**随机生成**并写进 `config.ini [ads] token`。
  后端与 ADS 插件读的是**同一份实现**（`backend/ads_auth.py`；插件侧由
  `addon/ads_agent/authbridge.py` 按文件路径加载同一个文件），所以不会出现
  "两端各自生成一个令牌、结果全部 401"的情况。检测到旧的公开默认值
  `ads-agent-local-token` 会自动轮换；想手动换：
  `python backend/ads_auth.py --rotate`，然后重启后端与 ADS。
  自查令牌状态：`python backend/ads_auth.py`（只显示脱敏指纹）。
- **令牌不落日志、不回显**：日志只记工具名 / 参数 / 耗时，以及"哪个请求被拒绝了"；
  鉴权失败的响应体永远是 `{"error": "unauthorized"}`。需要展示时只用 `mask()`。
- **config.ini 只有一个写入通道**：令牌的生成/轮换与 LLM 设置的保存都是
  "读整个文件 → 改几行 → 写回"，两者共用 `backend/ads_auth.py` 提供的
  `edit_config()`（跨进程排他锁 + 原子替换），所以不会出现"刚轮换的令牌被一次
  保存 API 设置的操作还原成公开默认值"这类互相覆盖。详见下面「配置写入协调」。
- 工具服务只绑定 `127.0.0.1`。
- **仿真期间界面会不会卡，取决于模式（如实报告，不夸口）**：
  ADS 的 DE 数据库 API（打开设计、生成网表）**必须**在 Qt 主线程调用，
  而 `edatoolbox` 的仿真只吃一个网表字符串、不碰 DE，所以被放到了后台线程。
  长耗时期间状态栏会显示实时耗时（这也是"ADS 还活着吗"的可见信号），并按
  `config.ini [agent] sim_off_main_thread` 的实际值给出说明：为 `false` 时
  直接写「串行模式：仿真期间 ADS 界面会无响应」，不会假称不阻塞。
  该开关的实机验证结论见下面「实机验证结论」及 docs/R1_实机验证记录_2026-09-26.md。
- 仿真输出写到 `工作区/ads_agent_sim/<cell>_<时间戳>[_N]/`，每次仿真一个**新目录**，
  永不覆盖已有结果（见下面「仿真输出目录」）；
- `run_python` 的代码与输出会完整显示在面板里，可随时取消勾选关闭；
- `install_addon.bat` 修改 `eesof_addons.xml` 前自动备份（`.bak_adsagent`）。

## 配置写入协调

`config.ini` 有两个写入方，都是"读整个文件 → 改几行 → 写回"：

| 写入方 | 改哪一段 | 入口 |
|---|---|---|
| 令牌生成 / 轮换 | `[ads] token` | `backend/ads_auth.py` |
| 保存 LLM 设置 | `[llm]` | `backend/config.py` → `update_llm_settings()` |

两者都走 `ads_auth.edit_config(mutate)` —— **同一把跨进程锁 + 原子替换**：

- 锁文件 `<config 目录>/.ads_agent_config.lock`（`O_CREAT|O_EXCL`，崩溃残留会自动清理），
  同线程可重入（`ensure_token()` 持锁时会嵌套写令牌，不能把自己锁死）；
- 写入用"临时文件 + `fsync` + `os.replace`"，读方永远看不到写了一半的文件；
- **Windows 上 `os.replace` 与 `open` 会互相打断**：目标文件被打开时替换会被拒
  （`PermissionError`），替换的一瞬间读也会被拒。所以两边都带重试：
  写入重试到成功（实测 3 个线程持续读的情况下平均重试 2 次、最坏 22 次），
  读取读不到时**退回上次成功读到的值**而不是返回空 —— 空令牌会让 `check_token`
  一律拒绝，表现为"随机 401"；
- `_read_lines()` 读不到时**抛异常**而不是返回空列表 —— 拿到空列表会写出一个
  "只有新内容"的配置文件，把用户的设置和令牌全抹掉；读不到就失败更安全；
- 所以并发保存不会丢字段：一个进程在写 `[ads] token` 时，另一个保存 `[llm] model`
  会等它写完再基于**最新内容**改，而不是按自己的旧快照整文件覆盖。

诊断：`python check_env.py` 会报告令牌状态与是否有残留锁文件。
极端情况（锁被占用超过 8s，例如某个进程卡住）会退化为无锁写入并在
`update_llm_settings()` 的返回里带 `config_locked: false` —— 宁可保存成功，
也不让用户的"保存"按钮白按。

## 仿真输出目录

每次 `run_simulation` 都会新建一个**唯一**目录：

```
<工作区>/ads_agent_sim/<cell>_<YYYYmmdd_HHMMSS>[_2|_3|…]/
    ├── netlist.ckt        # 生成的网表（hpeesofsim 报错时直接打开看）
    └── <cell>.ds          # 仿真数据集
```

命名不是只靠秒级时间戳：`<cell>_<时间戳>` 撞名时靠
`os.makedirs(..., exist_ok=False)` 的**原子性**顺延 `_2` / `_3` …，
所以"改个参数立刻再跑一次"或"两个仿真同时跑同一个设计"都不会覆盖上一次的
`.ds` / 网表 / 日志。cell 名里的非法字符（`<>:"/\|?*`）会被替换成 `_`，
不会造出子目录或跑到 `ads_agent_sim` 之外。

## 会话隔离（多项目并行）

左侧项目列表里每个项目保存**独立的对话记录**。一轮对话在**按下发送时**就固定了
所属项目，之后所有异步内容都写回那个项目：

- 等待回复期间切到别的项目 —— 回复、工具卡片、错误都会落回**原项目**，
  当前项目不会被污染，内容也不会丢（状态栏会提示「项目「X」有新消息」）；
- 期间**删掉**该项目 —— 后续事件被丢弃并在状态栏计数提示，
  不会把已删除的项目"复活"；
- 清空会话是**原地清空**，不会出现"清掉的消息在下一条消息后集体复活"。
- 会话保存先写临时文件再替换，失败时保留原文件；主文件不可读时尝试
  `projects.json.bak`。保存失败会显示在状态栏。

## 测试

测试分四层，入口统一在 `tests/run_tests.py`（`pytest` 不是必需的，直接运行即可）：

```
python tests/run_tests.py --list         # 查看分层清单（logic / http / qt / ads）
python tests/run_tests.py                # 跑 logic + http + qt 全部（报告 通过/失败/超时/跳过）
python tests/run_tests.py --stdlib       # 只跑纯逻辑层（任何解释器都能跑）
```

分层说明：
- logic —— 纯逻辑（不需要 PySide6 / 网络）：指标评估与判定边界、任务状态机与
  持久化、取消链路、工作区绑定、LLM 流完整性、布局与几何门禁等；
- http —— 起真实本地服务的集成测试：后端鉴权、toolserver 忙碌状态；
- qt —— PySide6 offscreen 跑真实控件：项目隔离、结果页数据标识、气泡清洗、
  思考折叠行；
- ads —— ADS 实机验收，本脚本不跑：`tests/test_loopback.py`（toolserver 回环
  冒烟，用 ADS 内置解释器跑）、`tests/probe_via_ads.py`（进程内探针入口）、
  `tests/layout_regression.py`（布局回归编排）。

报告如实区分 通过 / 失败 / 超时 / 跳过 —— 有跳过时会明确列出"未执行"的文件，
**跳过不计入通过**；单个文件超时（默认 240s）按失败计，不会拖死整个套件。
一次性脚本（历史布局迭代、修复脚本）在 `tests/oneoff/`，归档的过期实现在
`attic/`（见其 README）。

也可以交给环境自检一起跑：`python check_env.py`（会顺带做全量语法编译检查）。

## 取消、超时与身份（2026-10-02 优化轮）

每一轮对话有 turn_id，每个 ADS 工具作业有 job_id，停止按钮的取消沿调用链传播：
面板 stop() → 后端 POST /chat/cancel → agent 不再发起新的模型请求、不再派发新工具
→ ADS 端 /cancel 对**排队中**的作业直接跳过、对**已开跑**的作业打 cancel_requested
标记（线程无法强杀，由 /health 如实报告"正在收尾"）。设计任务（重新仿真/重新评估）
在阶段边界检查取消：已完成的仿真产物与备份一律保留，界面文案区分
"已停止后续步骤"与"当前仿真仍在收尾"。

超时分类（错误信息里明确区分，不再都叫"超时"）：
- 排队超时 —— 截止时间到了还没轮到执行，作业未执行即返回；
- 执行超时 —— 已开跑但没在时限内完成（504，带回 job_id，可到 /health 查它是否仍在收尾）；
- 队列已满 —— 503 忙碌（容量 config.ini `[agent] tool_queue_capacity`，默认 64）；
- 网络断开 / 仿真失败 —— 各自独立报错。

其它相关行为：
- 设计接口支持 request_id 幂等：重复点击/客户端重试返回原响应（idempotent_replayed=true），
  不会重复建图或重复仿真；同一 job 与同一设计引用的并发操作串行执行；
- 任务记录并核对工作区：切换工作区后对旧结果页"重新仿真 / 打开原理图"会被拦截并
  给出恢复路径（防误操作另一个工作区里的同名设计）；结果复用从"本轮没调用修改工具"
  升级为网表指纹验证，设计被手工改过就不再复用旧数据集；
- 任务文件 schema v2：关键阶段检查点落盘、保存前自动留 .bak（损坏可恢复）、
  启动时把中断任务标记为"上次运行中断，待确认"（不宣称完成、不自动重跑）、
  保存失败在接口与界面分别报告"计算已完成 / 结果未成功保存"；
- Qt 主线程的 pump() 每次最多执行 `pump_max_jobs`（默认 16）个排队作业，
  大量积压不会占死 ADS 界面线程；
- 长会话上下文预算：超过 `context_budget_chars`（默认 120000 字符）压缩较早历史，
  系统提示、原始需求与最近消息始终保留。

需要 ADS 才能验证的部分已于 2026-09-26 在真 ADS 上逐项验证，见下面「实机验证结论」。

## 忙碌状态与 `/health`

ADS 端工具服务的 `/health`（`http://127.0.0.1:8761/health`，无需令牌）如实报告
**所有仍在运行的作业**：

| 字段 | 含义 |
|---|---|
| `busy` | 第一个仍在运行的作业名，空闲时为 `null` |
| `busy_count` | 仍在运行的作业数（多个后台仿真可以并存） |
| `jobs` | `[{name, elapsed_s, timed_out, cancel_requested}]`，按开始顺序 |
| `timed_out_count` | 请求已超时、但后台线程还在跑的作业数 |
| `cancel_requested_count` | 已请求取消、但线程无法强杀仍在收尾的作业数 |
| `queued` / `queue_capacity` | 当前排队数 / 队列容量（满了 /execute 返回 503） |
| `sim_off_main_thread` | 当前仿真是否跑在后台线程 |

关键点：作业从**入队**起就计入 `busy`，只有它**真正收尾**（成功 / 失败 /
后台线程结束）才摘掉 —— 仿真转后台的那一刻不会清空状态，多个作业也不会互相
清掉对方的记录。请求超时（504）后后台线程无法被强制终止，所以它**仍算运行中**
并额外带 `timed_out` 标记：面板不会因此以为 ADS 已经空下来。

排查时可以直接看：

```
http://127.0.0.1:8761/health
```

后端自己的 `/health` 报告的是模型、API Key 是否配置、`max_tool_steps`、
`sim_timeout`、`sim_off_main_thread` 等，供启动探测使用。

## ADS 2027 API 实测结论

`backend/agent.py` 的系统提示里带一份「ADS 2027 API 速查（已实测）」，
是直接用 `tests/probe_via_ads.py` 在 ADS 进程内部探测得到的，不是猜的。
几个最容易踩的坑：

| 坑 | 正确写法 |
|---|---|
| `open_design(mode="READ_ONLY")` | 必须给 `DesignMode` 枚举（字符串 `"READ_ONLY"`、整数 `0` 都报错） |
| `design.save()` | 是 `design.save_design()` |
| `inst.delete()` | 是 `inst.delete_object()` |
| `inst.origin = ...` | origin 只读，用 `inst.move((dx, dy))`；朝向用 `inst.orient = Orientation.R90` |
| `ItemInfo.create_new_instance(angle=)` | 用 `design.add_instance(master, (x, y), name=, angle=)` |
| `db_uu.PointF` | 在 `keysight.ads.de._points`；但原点/坐标直接传元组即可 |
| `InstPin.name` | 不存在；用 `inst_term.term_number`（编号）或 `term_name`（具名） |
| `Cell.get_view` | 用 `cell.view(name)` / `get_view_if_exists(name)` |
| `Term` 在 `ads_sources` | **`Term` 和 `S_Param` 都在 `ads_simulation`；`GROUND` 在 `ads_rflib`；接地参考端口首选 `ads_simulation:TermG`（单脚自带地）** |
| `shapes[i]` / `inst_pins[i]` | 都是迭代器，先 `list()` |

修改或复核这些结论：编辑 `tests/probes/*.py`，然后
`python tests/probe_via_ads.py tests/probes/H_regression.py`（在 ADS 内部执行）。

设计闭环相关的三个探测脚本（都是**只读枚举**，不猜 API 名，结论已写入「实机验证结论」）：

| 脚本 | 查什么 |
|---|---|
| `tests/probes/P_open_schematic_gui.py` | 打开设计后原理图窗口的标题，验证「在 ADS 中打开原理图」的置前匹配是否有效 |
| `tests/probes/Q_data_display_api.py` | ADS 是否暴露原生 Data Display 的 Python API（遍历模块名，不猜） |
| `tests/probes/R_dataset_units.py` | `.ds` 数据集是否提供横轴/纵轴单位 |

## 排错：日志在哪里

出了问题时按顺序看这两份日志（**都是程序自己写的，跟怎么启动无关**）：

| 日志 | 内容 | 位置 |
|---|---|---|
| 后端日志 | 每轮对话的模型/步数/耗时、每次工具调用的参数与结果、所有异常堆栈 | `%LOCALAPPDATA%\ADSAgent\logs\backend.log`（滚动保留 3 个备份） |
| ADS 端日志 | 工具服务收到的请求、每条工具执行结果、`run_python` 内部报错 | `%LOCALAPPDATA%\ADSAgent\logs\ads_toolserver.log` |

（日志位置由 `backend/paths.py` 统一决定；设 `ADS_AGENT_DATA_DIR` 可整体改到别处。）

不开文件也行，后端在跑的时候直接看最近 N 行：

```
http://127.0.0.1:8760/logs?n=200
```

后端启动后会在日志里打印一行使用中的参数（Python 版本、配置路径、模型、
`max_tool_steps`、ADS 工具服务地址），确认配置有没有生效就看这一行。

## 常见问题

**面板提示「后端未启动」** —— 插件会**自动拉起后端并重试**，无需手动干预；
若自动启动持续失败：

- 双击 `start_backend.bat` 手动启动（保持窗口开着，日志同时在 `logs\backend.log`）；
- 或用 `python tests/restart_backend.py` 以隐藏进程方式重启它；
- 运行 `python check_env.py` 自检。

> **不推荐**用 `python backend/server.py` 之外的方式长期挂着后端时依赖打印输出：
> ADS 是 GUI 进程，没有控制台，`print`/`traceback` 都会丢。后端现在自己写文件日志。

**面板提示「后端拒绝了本次请求（回环令牌不匹配）」** —— 两端读到的令牌不同。
最常见的原因：后端与插件读的不是同一个 `config.ini`（换了安装目录），
或其中一方还是旧进程。处理：

1. `python check_env.py` —— 看「回环令牌状态」是否显示"两端一致"；
2. 重启后端（`start_backend.bat`）与 ADS；
3. 仍不行就 `python backend/ads_auth.py --rotate` 换一个令牌，再重启两端。

**工具调用报「无法连接 ADS 端工具服务」** —— 确认 ① ADS 2027 已启动；
② 已运行 `install_addon.bat` 并重启 ADS；③ 打开过一次对话面板
（面板会拉起工具服务）；或菜单 Tools ▸ ADS Agent ▸ 启动/重启工具服务。

**改了 config.ini 不生效** —— 重启后端，重新打开面板。

**保存设置/轮换令牌后配置像被"改回去"了** —— 正常情况下两端写入共用一把锁
（见「配置写入协调」），不会互相覆盖。若确实遇到：

1. `python check_env.py` —— 看「回环令牌状态」是否"两端一致"、
   「配置写入锁状态」是否有残留锁文件（超过 15s 未释放会被自动清理，
   也可以直接删掉 `<config 目录>/.ads_agent_config.lock`）；
2. 确认没有两个后端实例在跑（两个实例各持一份内存配置，也会互相覆盖）；
3. 用 `python backend/ads_auth.py --rotate` 重新生成令牌，再重启两端。

**想改单轮最大工具步数 / 仿真超时** —— config.ini `[agent]` 段。`max_tool_steps`
是**单轮对话允许的 LLM 来回轮数**（不是工具调用次数），默认 30。

**面板提示「已达单轮工具调用步数上限」** —— 这一轮的来回次数用完了，常见于需要
探索 ADS API 的任务（新建视图、放元件、反复调参复测）。处理顺序：

1. **调大预算**：config.ini `[agent] max_tool_steps = 50`，然后在面板点一次
   **⚙ 设置 ▸ 保存并应用**（会自动重载配置，**不用重启后端**）；或重启后端。
   生效后可访问 `http://127.0.0.1:8760/health` 看当前预算值。
2. **拆小任务**：先「建好 schematic 视图」，再「往里放元件」，一次一件。
3. **看阶段性结论**：触顶时 Agent 不会再直接失败，它会用不带工具的一轮给出
   「已完成什么 / 卡在哪 / 下一步怎么办」；按它的下一步再发一条消息即可接着做。
4. **治根**：反复触顶通常是 Agent 在**逐个试探 API**（一次 `dir()`、一次试签名、
   再试一种写法）。直接提示它「把 `dir` / `help` / `inspect.signature` 的结果合并到
   一次 `run_python` 里一次打印」，或直接告诉它正确的调用方式，步数立刻省下来。

> 后端已内置这条纪律（系统提示第 8~11 条）与预算提醒：剩余 4 步时面板会出现
> 一条 ℹ️ 提示，让 Agent 收敛。

**不想要开机自动弹面板 / 改弹出时机** —— config.ini `[ui]` 段的
`auto_open` 与 `auto_open_delay_ms`。

**界面太大 / 太小、想换字体** —— config.ini `[ui]` 段：`scale`（如 `1.15`
放大、`0.9` 紧凑，留空则跟随系统字号自动适配），`font_family`（留空自动挑选
圆润且支持中文的字体）。改完重开面板即可生效。

**面板误关/想浮动成独立窗口** —— 拖动 Dock 标题栏即可浮出，拖回边缘停靠；
关闭后从 Tools ▸ ADS Agent 菜单再打开。

**卸载** —— 双击 `卸载 ADS Agent.bat` 后重启 ADS。默认只移除注册，用户数据保留在
`%LOCALAPPDATA%\ADSAgent`；要连数据一起删：`install_addon.py --remove --purge-data`。

**没找到 ADS / 找到多个** —— `install_addon.py --detect` 列出候选，然后用
`--ads-dir` 指定；确认用第一个就加 `--accept-default`。

**端口被占 / 提示"服务无法确认归属"** —— 见下面「实例身份」一节。

## 注册为什么是写 XML，而不是用官方的 `add_user_addon`

`keysight.ads.de.app.add_user_addon()` / `remove_user_addon()` 确实是官方的
**用户级**注册接口，但它的实现是 `_pde_app.add_user_addon(...)`，而 `_pde_app`
是**编译进 ADS 主程序**的模块 —— 只有 ADS 自己的进程里才有。实测（本机 ADS 2027）：

* ADS 进程内：`import keysight.ads.de.app` 正常，`add_user_addon` 可用；
* 用 ADS 自带解释器在外部跑同一个 import：`ImportError: cannot import name
  'WorkspaceItemType' from '_pde_app.ui'`，`dir(_pde_app)` 里没有任何 addon 符号
  （外部解析到的是一个只有 `.pyi` 的空壳包）。

安装器必须能在"ADS 没运行"时工作，所以注册只能写成 ADS 启动时会读的
`eesof_addons.xml`（结构化解析 + 备份 + 原子写 + 失败回滚，写入后逐条比对
其它插件没被动过，见 `install_addon.py` 与 `tests/test_registration.py`）。

官方接口则在**条件成立的地方**照用：ADS 菜单 ▸ Tools ▸ ADS Agent ▸ 注册状态…
会走 `addon/ads_agent/registration.py` 调 `find_addon` / `add_user_addon` /
`remove_user_addon`。

## 实例身份：不以 `/health` 200 作为复用依据

后端与 ADS 端工具服务的 `/health` 都会带上 `identity`
（`install_id` / `plugin_version` / `protocol` / `app_root` / `data_root`）。
启动器（`addon/ads_agent/backend_launcher.py`）与自检（`check_env.py`）判断
"能不能复用"时必须同时满足三条：

1. **身份** —— `service` 是本服务的名字，且 `protocol` 与本端一致；
2. **归属** —— `identity.install_id` 是这份安装的 ID（存在 `安装状态`里）；
3. **存活** —— 登记文件里的 pid 还活着。

任一条不满足就**不连**，并给出可执行的提示（端口被谁占、是不是旧版本、
要不要先停掉）。首版只支持单实例：同一次安装出现了两个不同端口的后端会被
检测为"多开冲突"并写进日志。详见 `backend/instance.py`。

## 实现依据（ADS 2027 官方机制）

- 插件注册：`<HPEESOF_DIR>\config\eesof_addons.xml` + `keysight.ads.de.app`
  的 `Addon`/`Menu`/`Action`（官方 add-on 契约 `setup_addon` / `generate_menu`）；
- 设计操作 API：`de.active_workspace`、`db_uu.open_design`、实例 `vars`
  集合（官方示例 *Working with VAR* / *Create, Simulate, and Plot*）；
- 仿真：`edatoolbox.ads.CircuitSimulator().run_netlist()`；数据：
  `keysight.ads.dataset.open(...)`。

## 排错：仿真跑不起来时的排查顺序

`run_simulation` 会在仿真前做一次体检（实例数 / 仿真控制器 / 端口），
缺什么就直接说缺什么。常见报错与真实原因：

| 报错 | 真实原因 |
| --- | --- |
| `has no instances` | 设计是空的。多半是在只读设计上放元件，`save_design()` 抛 `Attempt to save a read-only design`，改动全丢 |
| `No Simulation Component specified` | 原理图里没放仿真控制器（`ads_simulation:S_Param` / `AC` / `HarmonicBalance` …） |
| `Expected a substrate model for parameter 'Subst'` | 用了微带线（MLIN/MTEE）但没放 `ads_tlines:MSUB`，或 `Subst` 值没加双引号 |
| `Attempt to save a read-only design` | 用 `write=False` 打开了设计却要保存 |

**ADS 2027 DesignMode 三个值的含义（实测，最容易踩）**：

| 枚举 | 值 | 语义 |
| --- | --- | --- |
| `READ_ONLY` | 0 | 只读，拿到磁盘上的内容 |
| `WRITE` | 1 | 打开一个**空白**可写设计，`save_design()` 会把磁盘上的原设计**整个覆盖成空** |
| `APPEND` | 2 | 打开磁盘上的现有设计并**追加**修改，保存正确落盘 |

修改已有设计一律用 `APPEND`（`odesign(lib, cell, write=True)` 内部已是
`APPEND`）；只有明确要推倒重来才用 `recreate(...)`。mode 必须传枚举成员，
传字符串 `"READ_ONLY"` 或整数 `0` 都会报 `DesignMode has no value`。

排障脚本见 `tests/probes/`（`N_write_semantics.py`、`O_append.py` 记录了
上面这张表的验证过程）。

## 实机验证结论（2026-09-26 已全部在真 ADS 上验证）

自动化测试覆盖不到、必须在真 ADS 里确认的事项已逐项验证完毕，
逐项方法与数据见 docs/R1_实机验证记录_2026-09-26.md，复现脚本在
tests/probes/（P/Q/Q2/Q3/Q4/R2/T/U/V/W/X/Y 系列）。结论摘要：

1. **仿真脱离主线程：安全，已实证。** 并行与串行模式各跑一次
   60001 点 S 参数仿真，两种模式 50000 点 S(2,1) 逐点相对差为 0；
   并行期间 ADS 主线程 ping 延迟 0.01~0.07s（界面保持响应），
   串行期间 ping 被阻塞 1.54s（与面板"串行模式会无响应"的提示一致）；
   仿真窗口内 toolserver 日志无 DE 异常。`sim_off_main_thread`
   默认 true，可放心保持。

2. **令牌迁移：收敛，已实证。** token 复位为公开默认值后按"先后端、
   再 ADS"重启，config.ini 自动轮换为随机值，后端日志打印脱敏指纹，
   `python check_env.py` 报两端一致、无残留锁，迁移后真实对话链路可用。

3. **真实 SSE 链路多项目并行：通过。** offscreen 真实面板 + 真后端 +
   真实 LLM 的实测（tests/probes/X_sse_multiproject.py）：项目 A 的
   长任务在途时切到项目 B，B 视图干净；A 的回复完整写回 A；B 随后
   正常对话；双向零污染，projects.json 落盘正确。

4. **真两进程同时写 config.ini：安全，已实证。** 后端被 ADS 拉起
   （读 legacy 令牌并轮换）的同一窗口内连续 10 次 POST /config
   （等价面板「保存并应用」）全部成功；config.ini 注释、[agent]、[ui]
   各段完整保留，configparser 可正常解析，check_env 两端一致、无锁残留。

5. **/health 长仿真忙碌状态：正确。** 忙碌期间 busy 恒为
   run_simulation、elapsed_s 单调递增；超时路径返回 504 后 /health
   继续报告该作业并标记 timed_out=true，直到后台线程真正结束。

### 设计闭环：已在 ADS 里跑完整遍

1. **端到端评估：通过。** 对 Wilkinson_2G4_v2 发"评估 2.3-2.5 GHz
   带内插损"：job 完整走 planned→simulating→evaluated，指标实测值与
   read_traces 独立读数逐位一致（-3.372438195381779 dB @2.48 GHz），
   判定流程正确（该设计两路输出各 3dB 分路损耗，达不到 1dB 目标，
   如实判 fail）。注意 band 传参用 {"start": 2.3, "stop": 2.5,
   "unit": "GHz"}，不要把单位拼进数字字符串。

2. **打开原理图：设计入会话可用；窗口自动置前不可用。** ADS 2027 的
   原理图窗口标题不含 cell 名，Qt 标题匹配命中不了（P 探针实证），
   面板如实提示"请在 Library 树里双击该 cell"——这是正确行为，
   维持现状。

3. **曲线一致性：通过（程序化等价）。** read_traces 与
   keysight.ads.dataset 直读同一 .ds 同一表达式：101 点横轴、幅度、
   dB 值差异全为 0。与原生 Data Display 的肉眼对照步骤同前
   （打开 .ds → 画同表达式 → 对照极值）。

4. **重启恢复：通过。** 重启 ADS 与后端后 projects.json、
   design_jobs/*.json、/design/job 接口数据完好；曲线以 display 序列
   （101 点 + 单位 + truncated 标记）持久化在 artifacts.traces；
   /design/reload 从 dataset_path 现场重算指标，结果逐位一致。

5. **失败也要留住产物：通过。** 故意制造网表级错误（VAR 删除 Riso）：
   job 停在 failed，error 点名 "Undefined parameter `Riso' used by
   `R1.R'"，输出目录保留 netlist.ckt 与全部仿真产物，指标全部 unknown，
   job 文件留存。

6. **ADS 原生 Data Display 的 Python API：可用（端到端实证）。**
   keysight.ads.dds 完整存在，实测最小链路全部走通：
   new_dds_file(path) → add_dataset_alias(别名, .ds 路径) →
   new_page → page.add_plot(traces=["dB(S(2,1))"], title=...) →
   plot.add_line_marker → save(name=, path=) → close，
   临时目录成功落盘 .dds（验证后已删）。注意 save 必须带 name。
   后续可按这些实测名字实现"生成原生 .dds 曲线页面"。
   探针：tests/probes/Q_data_display_api.py（枚举）、
   Q2_dds_deep.py / Q3_dds_sig.py（API 清单）、Q4_dds_e2e.py（实证）。

7. **数据集横轴单位：部分可用。** 数据集里没有量纲单位字段
   （Variable 上只有 name/attrs/dtype/flags 等），但 attrs 标志串
   可读出轴类型（如 freq 轴为 "frequency type=real indep=yes"）。
   因此横轴单位维持**推断**并显式标注：按"声明的频段落在数据范围内"
   反推量级，指标的 note 与结果页「（单位未声明）」照旧；R2 落地时
   轴类型可改用 attrs 真实判定。探针：tests/probes/R2_units_live.py。

   2026-09-26 验证中发现的问题记录在
   docs/R1_实机验证记录_2026-09-26.md 第四节。其中 `run_python` 的
   `SystemExit` 收尾已于 2026-09-27 修复并完成 ADS 实机复验；同轮还验证了
   临时设计建图、60001 点完整曲线评估和端口短路门禁。详见
   [R2 实机复验记录](docs/R2_实机复验记录_2026-09-27.md)。

## 改代码后要不要重启

- 改 `addon/ads_agent/ads_ops.py` —— **不用重启 ADS**，toolserver 会按文件
  mtime 自动热重载（`run_python` 的常驻环境会保留）；
- 改 `addon/ads_agent/toolserver.py` / `panel.py` / `result_page.py` /
  `uiscale.py` / `authbridge.py` —— 需要重启 ADS；
- 改 `backend/*.py`（含 `ads_auth.py`、`design_*.py`）—— 需要重启后端
  （`start_backend.bat`）；改 `agent.py` 的系统提示也要重启后端才生效；
- 改 `config.ini [ads] token` —— 两端都要重启（校验是每次请求现读盘的，
  但已建立的连接与面板缓存另说）。
