"""Agent loop: LLM <-> tools, emitting progress events for the chat panel.

The panel receives a stream of JSON events (SSE):
  status       — textual progress note
  reasoning    — 模型深度思考内容 {text}（面板折叠显示，工具细节只写日志不再下发）
  tool_call    — the agent invoked a tool {name, arguments}（面板只用来计数与计时）
  tool_result  — tool finished {name, ok, summary}
  assistant    — a final / intermediate assistant text message
  done         — turn complete {message}
  error        — turn failed {message}
"""

from __future__ import annotations

import json
import time
import traceback

import adslog
import llm
import tools as tools_mod
from tools import TOOLS

log = adslog.get("backend.agent")

MAX_SUMMARY_CHARS = 1200
DEFAULT_MAX_STEPS = 30
# 剩余步数降到这个数时提醒模型（以及面板）抓紧收敛
LOW_BUDGET = 4

BUDGET_HINT = """\
【步数提醒】本轮的预算只剩 {remaining} 步了。请立刻收敛：
- 不要再做新的探索性调用；
- 基于已有结果直接给出结论，或把「卡在哪、下一步需要什么」写清楚；
- 若还有必须执行的关键动作，把它们合并成尽量少的调用一次做完。
"""

FINALIZE_HINT = """\
【已到单轮工具调用上限】不能再调用任何工具了。请只用文字总结：
1) 已经确认的事实（带真实数值/路径/报错原文）；
2) 任务当前卡在哪一步、原因是什么；
3) 下一步建议（具体到「再发一条什么指令」或「需要用户先做什么」）。
不要再输出任何工具调用或代码块占位，直接给结论。
"""


# ---------------------------------------------------------------------------
# 工具结果结构化摘要（给模型的消息）：绝不做字符串中截断
# ---------------------------------------------------------------------------
_TOOL_RESULT_KEEP = ("ok", "status", "error", "job_id", "kind", "design_ref",
                     "stage", "stage_label", "verdict", "summary", "metrics",
                     "artifacts", "sim", "reuse_note", "note", "hint",
                     "workspace", "design_version", "dataset_path",
                     "netlist_path", "output_dir", "path", "variables")
_TOOL_LIST_CAP = 50
_TOOL_STR_CAP = 800


def _clip_val(v, list_cap=_TOOL_LIST_CAP, str_cap=_TOOL_STR_CAP):
    if isinstance(v, str):
        return v if len(v) <= str_cap else v[:str_cap] + " …(截断)"
    if isinstance(v, list):
        clipped = [_clip_val(x, list_cap, str_cap) for x in v[:list_cap]]
        if len(v) > list_cap:
            clipped.append(f"…(其余 {len(v) - list_cap} 项省略)")
        return clipped
    if isinstance(v, dict):
        return {k: _clip_val(x, list_cap, str_cap)
                for k, x in list(v.items())[:list_cap]}
    return v


def _summarize_tool_result(name: str, result) -> dict:
    """把工具结果整理成模型可读的结构化摘要。

    保留成功状态、错误、关键数值与产物引用；数组/嵌套按上限裁剪并**注明
    省略数量**，绝不把 JSON 从中间切掉一半。模型需要完整数据时应通过
    指定表达式/参数去读取，而不是指望消息里塞着上万行点。
    """
    if not isinstance(result, dict):
        return {"tool": name, "ok": True,
                "text": _clip_val(str(result), str_cap=2000)}
    out = {"tool": name}
    for k in _TOOL_RESULT_KEEP:
        if k in result:
            out[k] = _clip_val(result[k])
    for k, v in result.items():
        if k in out:
            continue
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = _clip_val(v)
        else:
            # 列表/字典且不在保留名单：大概率是大数组，只留键名提示
            out[k] = f"（{type(v).__name__}，共 {len(v)} 项，已省略；需要时明确指定重新读取）"
    return out

SYSTEM_PROMPT = """\
你是内嵌于 Keysight PathWave ADS 2027 的射频设计智能助手（ADS Agent）。

你可以通过一组工具直接操作当前打开的 ADS 工作区：查看工作区与设计、
读取和修改 VAR 变量、运行电路仿真、读取仿真数据集，以及执行任意
keysight.ads.de Python 代码。

工作准则：
1. 数据优先：一切数值结论（增益、S 参数、带宽、驻波等）必须来自
   read_traces 或 read_dataset 返回的真实数据，禁止凭经验编造数值。
2. 先读后改：修改任何 VAR 之前，先用 get_design_variables 读取当前值
   并向用户展示，再修改；修改后复述改动内容。
3. 仿真条件：run_simulation 要求设计中已放置仿真控制器（AC / S-Param /
   DC / HB 等）。若没有，先向用户说明需要哪个控制器，或用 run_python
   放置（放置控制器属于结构修改，需先征得用户同意）。
4. run_python 慎用：仅用于预置工具覆盖不到的操作；不修改、不删除用户
   未授权的工程文件；输出写到带时间戳的新目录，绝不覆盖已有结果。
5. 逐步推进：复杂任务（如"把增益调到 20dB"）先建立基线（读变量→仿真→
   读数据），再小步修改参数并复测，每步汇报数值变化。
6. 工程解读与设计思路：给出数值后解释其工程含义（如 S11<-10dB 表示匹配
   良好、dB(S21) 即增益）和下一步建议。回复要像同事讨论：先说结论和设计
   思路（为什么选这个拓扑/参数、依据是什么），再列数值，不要只报工具调用
   流水账；用户需求含糊或互相矛盾时，先问清楚再动手。
7. 用中文回复；回复简洁、面向工程师。聊天面板只显示纯文本：不要用任何
   Markdown 修饰（**加粗**、*斜体*、# 标题、反引号、表格、[链接](…)）；
   列表用短横线或数字编号，代码、文件名、参数名直接写纯文本。

步数预算与探索纪律（很重要）：
8. 单轮的工具调用步数是有限的。**不要用「一个问题一次调用」的方式逐个
   试探 API** —— 这是最常见的浪费。探索 API 时必须把多个问题合并到同
   一次 run_python 里，一次跑完、一次打印。
9. run_python 的执行环境**跨调用保持**（变量/import 都留着），并且预置了
   de、db_uu、ws()、ls(obj)、sig(obj, "方法名")。不确定某个 API 怎么用，
   先用 sig() 打印签名、用 ls() 列成员，再把多个问题一起 print —— 一次调用
   通常就能把 API 摸清，不需要逐个试。
     print(sig(cell, "create_view"))       # 看清参数表
     print(ls(cell))                       # 看清有哪些成员
     for t in ("schematic", "layout"):     # 候选写法在同一次调用里都试一遍
         try:
             print(t, "->", cell.create_view(t))
         except Exception as e:
             print(t, "-> 失败:", type(e).__name__, e)
10. 遇到「缺少位置参数」这类报错，不要逐个方法试：先用
   sig(obj, "方法名") 把签名打印出来，再按签名写调用。
11. 同一个目标连续失败 2 次就停下来：把现状、报错原文和你的判断告诉
   用户，并说明需要用户补充什么信息，不要继续换写法硬试。
12. 需要多次探索才能完成的任务（如「建原理图并放元件」），先说明你打算
   分几步做，再开始；每一步都让结果可复用（打印出下一步需要的名称/路径），
   避免重复探索。

## ADS 2027 API 速查（已在本机实测，直接照用，不要再逐个试）

打开设计（**最容易错，也最致命**）：
    d = odesign("AI_lib", "Wilkinson_2G4", write=False)   # 只读，看内容用这个
    d = odesign("AI_lib", "Wilkinson_2G4", write=True)    # 追加修改，用这个
    d = recreate("AI_lib", "Wilkinson_2G4")               # 从零重建（会清空原设计！）

DesignMode 有三个值，**别用错**：
    READ_ONLY(0)  只读，拿到磁盘上的内容
    WRITE(1)      打开一个**空白**设计！save(d) 会把磁盘上的原设计整个覆盖成空。
                  曾导致「元件放进去看着都成功，最后设计却是空的」
    APPEND(2)     打开磁盘上的现有设计并**追加**修改，save(d) 正确落盘
mode 必须给枚举成员，传字符串 "READ_ONLY" 或整数 0 都会报
"DesignMode has no value"。odesign(write=True) 内部已经用 APPEND，直接用即可。

放元件 / 连线 / 保存：
    i = put(d, "ads_tlines:MLIN", 0, 0, name="ML1", angle=90)  # 原点是普通元组
    wire(d, [(0, 0), (5, 0), (5, 3)])             # 原理图坐标，勿写千单位间距
    connect(d, ML1, 2, MTEE1, 1)                    # 按引脚连线（编号或名字都行）
    v = put(d, "ads_datacmps:VAR", 0, 8, name="VAR1"); v.vars["Z"] = "70.7 Ohm"
    save(d)                    # 是 design.save_design()，没有 design.save()
    print(pins(ML1))           # 看引脚编号与坐标，不确定就用它先查

改实例位置/朝向/删除（origin 只读，不能赋值）：
    inst.move((5, 0))          # 移动
    inst.orient = Orientation.R90   # R0/R90/R180/R270/MX/MY...
    inst.delete_object()       # 是 delete_object()，没有 delete()

建视图：cell.create_view(view_name, view_type_name) —— 两个参数，如
    cell.create_view("schematic", "schematic")

常用元件所在库（**Term/TermG 都在 ads_simulation**）：
  ads_simulation: TermG（推荐端口，单脚自带地参考）、Term、S_Param、SP_NWA_*
  ads_tlines:     MLIN、MTEE、MSUB
  ads_datacmps:   VAR
  ads_rflib:      R、C、L
  不确定就先 print(cells("ads_simulation", "Term")) 查

端口选择（2026-09-28 实测定案）：
  接地参考端口一律首选 ads_simulation:TermG —— 单引脚（编号 1，在 origin 上），
  参数 Num/Z/Noise，网表输出 Port:x N 0（第二节点是全局地），**不需要接 GND**。
  布局朝向与 Term 相反（符号体在 origin 右侧）：输入口 angle=180、输出口 0，
  layout="auto" 会自动规范，手工给坐标时注意。
  ads_simulation:Term 只用于确需独立参考端的场合（如差分/奇偶模分析）：
  双脚（1=地、2=射频信号，默认朝向下），1 号脚必须接 ads_rflib:GROUND，
  否则端口参考开路 —— 仿真"能跑完"但 S 参数不可信，run_simulation 门禁会拦截。

取引脚名：InstPin 没有 .name —— 用 pins(inst) 或
  term.term_number if term.is_numbered else term.term_name
其它坑：ShapeIter / inst_pins / inst_terms 都是迭代器，不能下标，要先 list()
（且同一实例多次取要重新 list）；inst.vars 只对 VAR 实例有效（对普通实例读会
抛 AssertionError）；PointF 在 keysight.ads.de._points（不在 db_uu）；
取视图用 cell.view("schematic") 或 get_view_if_exists()，没有 get_view；
ws.design(名字, mode) 只有两个参数，四个参数会报 TypeError。

排查工具：print(libs()) 看挂载了哪些库，print(sig(x, "方法")) 看签名，
print(ls(obj)) 看成员。全部塞进**一次** run_python 里 print 完。

## 建图 → 仿真 的标准流程（本机已反复验证）

**首选 build_schematic 一次做完**（放元件 + 设参数 + 连线 + 保存 + 只读复核 +
连通核对）。它在保存前失败时原设计保持不变；若保存后只读复核失败，
设计可能已经落盘，必须检查自动备份。**失败后不要在同一设计上继续追加修改**：先读报错，
缺什么补什么（分多次 build_schematic 增量构建是安全的，追加模式）。

    build_schematic(library="AI_lib", cell="MyCell", layout="auto", instances=[
        {"master": "ads_tlines:MSUB", "name": "MSUB1",
         "params": {"H": "2.6 mm", "Er": "4.4"}},
        {"master": "ads_tlines:MLIN", "name": "ML1",
         "params": {"Subst": "MSUB1"}},
        {"master": "ads_simulation:TermG", "name": "PORT1",
         "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_simulation:S_Param", "name": "SP1",
         "params": {"Start": "2 GHz", "Stop": "3 GHz", "Step": "0.01 GHz"}},
    ], connections=[{"a": ["PORT1", 1], "b": ["ML1", 1]}],
       var={"name": "VAR1", "values": {"Z0": "50 Ohm"}})

**布局永远优先 layout="auto"，不要自己编造 x/y 坐标或 waypoints**。调用
build_schematic 时 instances 完全不写坐标时甚至会自动按 auto 处理（给了
任何坐标则尊重手工位置）。工具按信号流自动排：端口链一行从左到右，相邻
元件间距按符号与参数文字的实际占用逐对计算（简单 R/L/C 链间距 1.4 紧凑
排布，MTEE/CLIN 等长参数器件自动留出避让通道）；分支行距按上一行内容
（符号+文字+接地支路）动态计算，不再固定 5 格；接地支路（去耦/分压/
负载/发射极电阻）就近挂在所属节点旁成组，不再独占整行分支；共享同一
GND 符号的多元件按伙伴引脚就近挂置；偏置/供电围绕所服务器件组织（电源
母线在上、偏置链选净空侧）；竖放桥接件（隔离电阻）自动选文字干净且连线
横段不穿字的落位。威尔金森/分配器类一分二自动镜像对称、一分三~一分五
对称铺开、两级级联一分四同样对称；标注避让自动完成（文字绝不被导线
穿过）；GROUND 按接入方向自动定朝向、VAR/MSUB/控制器自动排位（VAR 多
变量时自动抬高）、端口朝向自动规范。导线横平竖直、绕开元件符号，不得
穿符号内部、沿符号边缘重叠、与别的导线交叉或共线重叠；手工折点不满足
这些条件时建图会报错。
instances 不必写 x/y 和端口 angle——显式给了 angle 则尊重原值；电气拓扑
（参数/连线）不受布局影响。手动布局时：ADS 标准符号宽约 1 个坐标单位，
相邻元件间距 3~8；不要把物理尺寸（如 1000 mil）当坐标间距（千单位布局会被
拦截），也不要为绕过报错设 allow_large_geometry。

端口和仿真控制器**都在 ads_simulation**；微带线必须指向基板实例
（MLIN/MTEE 的 Subst 参数 = MSUB 实例名，缺了 hpeesofsim 必报
"Expected a substrate model"）。**接地参考端口首选 TermG**（单脚自带地，
引脚编号 1，无需 GND）；用 Term 时引脚语义先 pins() 确认（实测默认朝向下
1=地、2=射频信号），1 号脚不接 GROUND 会被仿真门禁拦截——端口参考开路时
仿真不出错但结果全错。

连线由 build_schematic 的 connections 完成时是**画线 + 显式网络绑定**双保险
（实测 add_wire 即使端点精确压在引脚上，网表里也不会自动合并网络），并会在
保存后核对两个引脚是否真的在同一网络。用 run_python 手工连线时同理：
connect(d, a, 1, b, 2) 已经包含绑定；不要只调 add_wire。

只有 build_schematic 覆盖不到的操作才用 run_python 手工建图，且必须遵守：
1) 写打开只能用 odesign(lib, cell, write=True)（APPEND 追加；它会**自动先
   备份**该设计到 <workspace>/ads_agent_backups/）。只读设计上放元件不报错，
   但 save 会抛 "Attempt to save a read-only design"，元件**全部丢失**。
2) **绝不用 db_uu.Transaction 包裹 add_instance/wire** —— 实测包住的放置
   在 save_design() 后会**静默丢失**（会话里看得到，重开就没了）。
   直接 put/wire 赋值即可，持久化是完整的。
3) 连线一律用 connect(d, a, 1, b, 2)（自动正交寻径 + 显式网络绑定），
   画原始线用 wire(d, [(x1,y1), ...]) —— 两者都有硬门禁：斜段、穿元件
   符号、与已有导线交叉/重叠会直接报错（实测 add_wire 端点不共轴会画
   斜线，Wilkinson_1G7_ML 的 Riso 斜线就是这么来的）。不要直接调
   design.add_wire 绕过门禁。找不到合格正交路径时报错就停下调整布局，
   不要退化为斜线。
4) 改完必须 save(d)（保存前自动跑几何门禁，违规不落盘），然后
   **close 写句柄**，再 save_verify("AI_lib", "MyCell") 复核落盘 ——
   同一设计的两个句柄不能并用（实测第二个会失效）。不复核就往下走 = 赌。
5) 失败就停：同一设计连续失败 2 次，把现状和报错告诉用户，不要继续堆修改。

仿真前想自检就调 check_connections(library, cell)：它点名 problems
（缺控制器 / 缺端口 / 基板引用断裂）和 warnings（悬空引脚）。
run_simulation 自带门禁（控制器、端口、基板引用、网络连通），缺什么会被
**点名拦截**；被拦就按提示补齐，不要盲撞仿真器。

设参数（**容易写错**）：
    setp(inst, "Start", "2 GHz")     # inst.parameters 是 list[Param]，不是 dict！
    print(params(inst))              # 先列出所有参数名和当前值再改
    var.vars["Z0"] = "50 Ohm"        # 只有 VAR 实例有 .vars；普通实例读会 AssertionError
    setp(mlin, "Subst", "MSUB1")     # 微带线 MLIN/MTEE 必须指向基板实例名

常见仿真报错的真实含义：
  "No Simulation Component specified"  -> 原理图里没放仿真控制器
  "has no instances"                   -> 设计是空的（多半是只读模式下改动没落盘）
  "Expected a substrate model for parameter `Subst'"
                                      -> 用了微带线但没放 MSUB 并设 Subst

## RLC 取值规范：优化收敛后必须吸附到常用值

仿真优化得到的连续值（C=2.37pF、L=8.9nH 这类）只是数学解，**不能直接交付**——
真实元件按 IEC 60063 E 系列和厂商固定档位生产。取值规则：

标准 E 系列（同一序列跨十年适用：2.2 档 = 22pF / 2.2nH / 2.2kOhm）：
  E12（±10%，默认首选，最便宜最好买）:
      1.0 1.2 1.5 1.8 2.2 2.7 3.3 3.9 4.7 5.6 6.8 8.2
  E24（±5%，E12 吸附后指标恶化时才用，再加 12 档）:
      追加 1.1 1.3 1.6 2.0 2.4 3.0 3.6 4.3 5.1 6.2 7.5 9.1
  E6（±20%，大容值电解/普通陶瓷）: 1.0 1.5 2.2 3.3 4.7 6.8
  E96（±1%）基本只用于精密电阻（偏置电流设置点、精密分压），电容电感极少。

射频件的实际商品档（与理想 E 系列有出入，**按这个取**）：
  电容（Murata GJM / ATC 0402 高 Q 系，约 0.2~20pF）：
    10pF 以下按 **0.1pF 步进**（不是 E24；0.5pF 以下有 0.05pF 档），公差
    ±0.05/±0.1/±0.25pF；10pF 以上走 E12/E24。
    隔直/退耦用大值标准档：100pF（VHF 以上隔直）、1nF、0.01uF、0.1uF、10uF。
  电感（Coilcraft 0402HP/0402CS 系）：1.0~220nH。
    10nH 以下有 E24 细档；10nH 以上 E12/E24，到约 100nH 为止。
    **小于 1nH 的值不要取**——商品极少，改用微带短短线/高阻线实现。
  电阻：偏置/负载/隔离 E24 起步；50Ω 负载与隔离电阻本身就是标准档。

优化流程中的用法：
1. 探索期允许连续值自由调；**收敛后把每个 C/L/R 吸附（snap）到最近的
   常用档，然后重新仿真验证**。吸附后不达标就换相邻档再测；相邻两档都
   不行说明该位敏感，改用：两档并联/串联拼值、调拓扑、或微带线替代。
2. 先粗后细：先吸 E12（电容 10pF 以下则 0.1pF 档），指标恶化超预算
   （如 S11 恶化超过 1dB）才退 E24。
3. 敏感度分级：匹配网络里的 C/L 吸附后必须复测；隔直、退耦、馈电电感
   只要"大到足够"（隔直 XC 远小于 50Ω、退耦呈低阻），直接取比计算值
   大一档的常用值即可，不参与精细优化。
4. 微带线 W/L 不属于元件值，连续可制造，圆整到 0.01mm 即可。
5. 汇报时给吸附对照：优化值 → 取用值 → 复测指标，如
   "C1 优化 2.37pF → 取 2.2pF，S11 由 -14.2dB 变 -13.1dB，仍达标"。

## 两种布局与射频物理审查（audit_rf）

原理图布局只负责信号流、可读性、引脚方向、接地位置；**原理图导线长度是
绘图坐标，绝不是微带线的物理长度**。实际铜皮的宽度、间隙、长度、拐角、
接地回流与制造约束以 ADS Layout 为准。涉及射频物理正确性时调用：
    audit_rf(library="AI_lib", cell="MyCell")
返回逐条 findings，每条带 设计引用/元件及引脚/位置/实际参数/问题/建议/
验证状态。使用要点：
- MTEE 的 1/2/3 端按旋转后的实际引脚坐标几何判定（不要按屏幕左右猜）；
  贯穿对默认要求 W1=W2，有意的宽度变化必须加过渡结构（MSTEP）并说明理由。
- 微带 Z0/电长度用闭式模型核对（约 2% 精度）。**LineCalc 在本环境不可
  程序化调用**——需要精确合成时引导用户手工打开 LineCalc，绝不臆造 W/L/G。
- 设计没有 Layout 视图或 Layout 为空时，结果会如实标注
  「未完成 Layout 验证」：引导用户在 GUI 用 Edit ▸ Generate/Update
  Layout 手动生成后再审，**不要拿示意图或推测冒充真实 Layout**。
- 本环境无法自动跑 EM：电路仿真与 EM 结果对比只能标记「未验证」。

## 设计闭环：把"指标要求"变成可检查的结果页

用户给的往往是**指标要求**（例如「设计一个 2.4 GHz 放大器，带内增益 ≥15 dB，
S11 低于 −10 dB」）。标准流程：

1. **先整理成可检查的任务**，并用文字复述给用户（目标值 / 判据 / 表达式 / 单位）：
   - 增益     -> 表达式 `dB(S(2,1))`，判据 `>=`，单位 dB
   - 回波损耗 -> 表达式 `dB(S(1,1))`，判据 `<=`，单位 dB
   S 参数数据集顶层变量通常是 `SP1.SP`，矩阵元素在其中的 `S[2,1]` 等列。
   读取完整曲线时用 `read_traces` 的 `dB(S(2,1))` 形式，工具会从矩阵取值；
   不要把该表达式直接当作 `read_dataset` 的顶层变量，也不要因顶层列表只含
   `SP1.SP` 就认定仿真没有 S 参数。
2. **设计或修改原理图**：按上面「建图 → 仿真 的标准流程」做；改完必须
   `save(d)` 并重开只读核实落盘。
3. **发布结果页**：调用 `publish_design_result`，只给设计引用、频段、指标定义。
   若刚用 `run_simulation` 得到该设计的数据集，设置 `reuse_dataset=true` 并
   传 `dataset_path`，避免重复跑同一设计；设计有任何改动后必须重新仿真。
   - **绝对不要自己填实测数值或达标结论**：实测值由确定性评估器从真实 `.ds`
     数据算出。你填了也不会被采用，并且属于编造数据。
   - 工具返回的 `actual` / `pass` / `at` 就是最终结论，必须**原样引用**。
   - `verdict` 为 fail/partial 时，说清差多少、在哪个频点，再提出下一轮改哪个参数；
     unknown 时说明哪个表达式无法读取或缺少哪个判据，不能编造差值。
   - 一轮里想迭代多次就多次调用（同一 job 会自动追加迭代记录）。
4. **不要替结果页写内容**：设计引用、"打开原理图"按钮、真实曲线（含横纵轴与
   单位）、每项指标的目标/实测/判定/频点、仿真状态、输出目录、重新仿真入口
   都是自动生成的。
5. 调用之后不要再说"应该能达到 / 预计达标"；要么引用评估结果，
   要么明确说"尚未验证"。
"""


class Turn:
    """One user turn: run the tool loop, yield events until done/error.

    取消（2026-10-02）：每轮有 turn_id 和 cancel_event。``cancel()`` 由
    /chat/cancel 端点调用，取消沿调用链传播：
      * 取消后不再发起新的模型请求（当前流式请求无法中断，返回后丢弃）；
      * 取消后不再派发新的工具（正在执行的 ADS 工具无法强杀，由
        toolserver 的 /cancel 对排队作业跳过、对执行中作业打标记）；
      * 设计任务（publish_design_result）在阶段边界检查取消，已完成的
        仿真产物保留，不继续读取/评估。
    """

    def __init__(self, cfg: dict, history: list, allow_python: bool = True,
                 model: str | None = None, turn_id: str | None = None,
                 cancel_event=None):
        import threading
        import uuid
        self.cfg = cfg
        self.history = history  # panel-visible [{role, content}] messages
        self.allow_python = allow_python
        self.max_steps = max(1, int(cfg.get("max_tool_steps", DEFAULT_MAX_STEPS)))
        self.model = (model or "").strip() or None
        self.turn_id = turn_id or uuid.uuid4().hex
        self.cancel_event = cancel_event or threading.Event()
        # 每轮汇总用：调用数 / 失败数 / 其中脚本失败数 / 仿真成败
        self.stats = {"calls": 0, "failed": 0, "script_failed": 0,
                      "sim_ok": 0, "sim_failed": 0}
        self._last_sim = None
        # 每轮 token 用量 + LLM 耗时（面板据此显示「N Tokens · M Token/秒 · 模型」）
        self._turn_usage = {"completion": 0, "prompt": 0, "llm_s": 0.0, "known": False}
        # 本轮是否收到过思考内容（用于排查「模型没开思考」类问题）
        self._got_reasoning = False

    def cancel(self) -> None:
        """请求取消本轮：不再发起新的模型请求和工具派发。"""
        self.cancel_event.set()

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def _apply_context_budget(self, messages: list) -> list:
        """长会话的上下文预算：保留系统提示、原始需求与最近消息，压缩较早历史。

        预算来自 config.ini [agent] context_budget_chars（字符数，默认 12 万）。
        保留优先级：1) 系统提示；2) 第一条 user（用户的原始需求/设计引用）；
        3) 从最新往前装到预算为止。被压缩的条数以一条 user 标记注明 ——
        模型知道历史被压缩，不会假装记得。
        """
        budget = int(self.cfg.get("context_budget_chars") or 120000)
        if budget <= 0:
            return messages

        def _size(msgs: list) -> int:
            return sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or "")) + 24
                       for m in msgs)

        if _size(messages) <= budget:
            return messages

        head, head_ids = [], set()
        idx = 0
        if messages and messages[0].get("role") == "system":
            head.append(messages[0])
            head_ids.add(id(messages[0]))
            idx = 1
        first_user = next((m for m in messages[idx:] if m.get("role") == "user"), None)
        if first_user is not None:
            head.append(first_user)
            head_ids.add(id(first_user))

        tail, remaining = [], budget - _size(head)
        for m in reversed(messages):
            if id(m) in head_ids:
                continue
            s = _size([m])
            if remaining - s < 0:
                break
            tail.insert(0, m)
            remaining -= s

        n_dropped = len(messages) - len(head) - len(tail)
        if n_dropped <= 0:
            return messages
        marker = {
            "role": "user",
            "content": (f"【上下文压缩】为控制长度，中间 {n_dropped} 条较早的消息已省略；"
                        "用户的原始需求和最近的操作/结果都完整保留，请基于它们继续。"),
        }
        return head + [marker] + tail

    def _reasoning_cb(self, emit, flags: dict):
        """流式思考增量 → reasoning_delta 事件；每步第一个增量带 first=True。"""

        def on_reasoning(delta: str):
            first = not flags["started"]
            flags["started"] = True
            self._got_reasoning = True
            emit({"type": "reasoning_delta", "text": delta, "first": first})

        return on_reasoning

    def _accum_usage(self, usage: dict | None, elapsed_s: float):
        self._turn_usage["llm_s"] += max(float(elapsed_s), 0.0)
        if usage:
            try:
                self._turn_usage["completion"] += int(usage.get("completion_tokens") or 0)
                self._turn_usage["prompt"] += int(usage.get("prompt_tokens") or 0)
                self._turn_usage["known"] = True
            except (TypeError, ValueError):
                pass

    def _stats_payload(self, model: str | None) -> dict:
        u = self._turn_usage
        return {
            # 服务不返回 usage 时为 None，面板按文本长度估算
            "completion_tokens": u["completion"] if u["known"] else None,
            "prompt_tokens": u["prompt"] if u["known"] else None,
            "llm_seconds": round(u["llm_s"], 2),
            "model": model or "",
        }

    def _tool_schemas(self, call_cfg: dict) -> list:
        """本回合可用的工具列表（按 ADS 端门禁过滤；拿不到就回全量）。"""
        fn = getattr(tools_mod, "available_tools", None)
        if callable(fn):
            try:
                schemas = fn(call_cfg)
                if isinstance(schemas, list) and schemas:
                    return schemas
            except Exception as e:  # noqa: BLE001 — 过滤失败回退全量列表
                log.debug("工具过滤失败，回退全量: %s: %s", type(e).__name__, e)
        return TOOLS

    def _system_prompt(self, call_cfg: dict) -> str:
        """系统提示词 = 基线提示 + （非 2027 环境时的）兼容性附注。

        基线提示里的 API 速查全部来自 2027 实机实测；其他版本上这些结论
        不应被当作事实使用，所以必须由附注明确声明。
        """
        prompt = SYSTEM_PROMPT
        try:
            snap = tools_mod.compat_snapshot(call_cfg)
        except Exception:  # noqa: BLE001 — 快照失败不阻塞对话
            snap = {}
        if not snap.get("available"):
            return prompt
        version = snap.get("ads_version") or {}
        year = version.get("year")
        status = version.get("status")
        caps = snap.get("capabilities") or {}
        binding = (caps.get("qt_binding") or {})
        qt_line = f"Qt 绑定: {binding.get('reason') or binding.get('status')}"
        if status == "known" and year == 2027:
            return prompt
        if status != "known" or not year:
            return (prompt
                    + "\n\n## 兼容性附注（重要）\n"
                    + "当前 ADS 版本无法确认（buildInfo.xml 识别失败）。未配备份证据："
                      "API 速查与实测结论按『参考』对待，不要当作已验证事实；"
                      "写/建图/仿真类工具默认被禁用，若调用被拒请直接告知用户原因。\n")
        return (prompt
                + f"\n\n## 兼容性附注（ADS {year}，实验性）\n"
                + f"当前环境是 ADS {year}（Update={version.get('update') or '?'} "
                  f"build={version.get('build') or '?'}，{qt_line}）。"
                  "该版本仅完成官方文档与离线验证，**未实机验证**：\n"
                  "- 底部的 API 速查来自 ADS 2027 实机实测，在本版本上不保证成立；"
                  "不确定的 API 先用 sig()/ls() 探测，不要直接照抄。\n"
                  "- 被兼容门禁拒绝的工具请直接向用户说明原因，不要换写法绕过。\n"
                  "- 引用任何『已实测』结论前先注明它来自 ADS 2027。\n")

    def run(self, emit):
        call_cfg = dict(self.cfg)
        if self.model:
            call_cfg["llm_model"] = self.model
        messages = [{"role": "system", "content": self._system_prompt(call_cfg)}]
        messages += self.history
        warned = False

        log.info("==== 新一轮 ==== model=%s max_steps=%s 历史消息=%d 条 allow_python=%s",
                 call_cfg.get("llm_model"), self.max_steps, len(self.history), self.allow_python)
        _t0 = time.perf_counter()

        try:
            for step in range(1, self.max_steps + 1):
                if self.cancelled():
                    log.info("轮次 %s 已取消（第 %d 步前停止）", self.turn_id, step)
                    emit({"type": "cancelled", "turn_id": self.turn_id,
                          "message": "已停止：不再发起新的模型请求和工具执行。"
                                     "正在执行的操作会在安全边界收尾。"})
                    return
                remaining = self.max_steps - step
                emit({"type": "status", "text": f"正在思考…（第 {step}/{self.max_steps} 步）"})

                # 预算将尽：提醒模型收敛（tool 消息之后追加一条 user 提示是安全的）
                # 预算本身就很小时不提醒，否则第一轮就喊「只剩 N 步」很奇怪
                if remaining <= LOW_BUDGET and self.max_steps > 2 * LOW_BUDGET and not warned:
                    warned = True
                    messages.append(
                        {"role": "user", "content": BUDGET_HINT.format(remaining=remaining)}
                    )
                    emit(
                        {
                            "type": "notice",
                            "text": f"工具调用预算仅剩 {remaining} 步，已提醒 Agent 收敛并给出结论。",
                        }
                    )

                _tllm = time.perf_counter()
                messages = self._apply_context_budget(messages)
                content_sink = {"buf": ""}

                def _on_content(delta: str):
                    content_sink["buf"] += delta
                    emit({"type": "content_delta", "text": delta})

                msg, usage = llm.chat_stream(
                    call_cfg, messages, tools=self._tool_schemas(call_cfg),
                    on_reasoning=self._reasoning_cb(emit, {"started": False}),
                    on_content=_on_content,
                )
                _ms = int((time.perf_counter() - _tllm) * 1000)
                self._accum_usage(usage, _ms / 1000)

                # 模型流式请求无法中途打断；若取消发生在等待期间，直接丢弃
                if self.cancelled():
                    log.info("轮次 %s 已取消（模型响应返回后丢弃）", self.turn_id)
                    emit({"type": "cancelled", "turn_id": self.turn_id,
                          "message": "已停止：模型响应已丢弃，未执行任何操作。"})
                    return

                # 思考内容已随流式增量实时下发（reasoning_delta），不回填进
                # messages —— 带回去部分服务会报错，思考也不该进对话历史
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    content = msg.get("content") or ""
                    log.info("第 %d/%d 步 LLM 回复(%dms)，无工具调用，本轮结束（总计 %dms）",
                             step, self.max_steps, _ms,
                             int((time.perf_counter() - _t0) * 1000))
                    messages.append({"role": "assistant", "content": content})
                    emit({"type": "assistant", "text": content})
                    emit({"type": "done", "message": content,
                          "stats": self._stats_payload(call_cfg.get("llm_model"))})
                    return

                # 流异常中断的响应里工具参数很可能没拼完 —— 绝不能进 ADS 执行
                if msg.get("complete") is False:
                    log.warning("轮次 %s 模型流中断（带 %d 个工具调用），已阻止执行",
                                self.turn_id, len(tool_calls))
                    emit({"type": "notice",
                          "text": "模型响应流异常中断（未收到结束标志），"
                                  "其中的工具调用不完整，已阻止执行；请重试。"})
                    content = msg.get("content") or ""
                    if content:
                        messages.append({"role": "assistant", "content": content})
                        emit({"type": "assistant", "text": content})
                    emit({"type": "done", "message": content,
                          "stats": self._stats_payload(call_cfg.get("llm_model"))})
                    return

                log.info("第 %d/%d 步 LLM 请求 %d 个工具(%dms): %s",
                         step, self.max_steps, len(tool_calls), _ms,
                         ", ".join(tc.get("function", {}).get("name", "?") for tc in tool_calls))
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for tc in tool_calls:
                    self._run_tool_call(messages, tc, emit)

            # 预算用尽：不再直接报错，先要一份「只基于已有信息」的总结
            log.warning("步数用尽（%d/%d），进入总结收尾", self.max_steps, self.max_steps)
            self._finalize(call_cfg, messages, emit)
        except llm.LLMError as e:
            log.error("LLM 调用失败: %s", e)
            emit({"type": "error", "message": str(e)})
        except tools_mod.AdsToolError as e:
            log.error("ADS 工具执行失败: %s", e)
            emit({"type": "error", "message": f"ADS 工具执行失败：{e}"})
        except Exception as e:  # noqa: BLE001 — report anything to the panel
            traceback.print_exc()
            log.exception("本轮未预期异常: %s: %s", type(e).__name__, e)
            emit({"type": "error", "message": f"内部错误：{type(e).__name__}: {e}"})
        finally:
            self._log_turn_summary()

    def _log_turn_summary(self):
        """每轮收尾的调用汇总：一轮多少调用、多少失败、其中多少脚本失败。

        过去 logs 里大量「成功 + run_python_ok=False」混在一起，排查时数不清
        一轮到底烧了多少调用、败在哪类 —— 这里一口气给结论。
        """
        st = self.stats
        if not st["calls"]:
            return
        log.info("本轮汇总: 工具调用 %d 次（失败 %d，其中脚本执行失败 %d）；"
                 "仿真 成功 %d / 失败 %d",
                 st["calls"], st["failed"], st["script_failed"],
                 st["sim_ok"], st["sim_failed"])

    def _finalize(self, call_cfg: dict, messages: list, emit):
        """步数用尽：不再带工具地问最后一轮，把已有信息整理成结论。

        这样即使用户的任务比预算更大，也能拿到「做了什么 / 卡在哪 / 下一步」，
        而不是一句干巴巴的上限报错。总结成功就正常结束（附一条提示）。
        """
        reason = (
            f"已达单轮工具调用步数上限（{self.max_steps}）。"
            f"可提高 config.ini 的 [agent] max_tool_steps，或把任务拆成更小的几步。"
        )
        emit({"type": "status", "text": "步数用尽，正在整理已有结果…"})
        emit({"type": "notice", "text": reason})
        try:
            _tllm = time.perf_counter()
            messages = self._apply_context_budget(messages)
            msg, usage = llm.chat_stream(
                call_cfg, messages + [{"role": "user", "content": FINALIZE_HINT}],
                tools=None, on_reasoning=self._reasoning_cb(emit, {"started": False}),
            )
            self._accum_usage(usage, time.perf_counter() - _tllm)
            content = (msg.get("content") or "").strip()
            log.info("总结轮完成，产出 %d 字", len(content))
        except Exception as e:  # noqa: BLE001 — 总结失败也不该把整轮变成报错
            log.warning("总结轮失败: %s: %s", type(e).__name__, e)
            content = ""

        if content:
            emit({"type": "assistant", "text": content})
            emit({"type": "done", "message": content,
                  "stats": self._stats_payload(call_cfg.get("llm_model"))})
        else:
            emit({"type": "error", "message": reason})

    def _run_tool_call(self, messages, tc, emit):
        name = tc.get("function", {}).get("name", "")
        raw_args = tc.get("function", {}).get("arguments") or "{}"

        # 取消检查：已取消就不再派发任何工具。协议上 tool 消息必须回应
        # assistant 的 tool_call，所以补一条"未执行"的 tool 消息保持序列完整。
        if self.cancelled():
            emit({"type": "tool_result", "name": name, "ok": False,
                  "summary": json.dumps({"error": "已取消：用户停止了本轮，工具未执行。"},
                                        ensure_ascii=False)})
            messages.append({
                "role": "tool", "tool_call_id": tc.get("id", ""),
                "content": json.dumps({"error": "cancelled_by_user"},
                                      ensure_ascii=False),
            })
            return

        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as e:
            # 参数没拼完/写坏了还照执行 = 拿空参数去改设计 —— 过去这会静默
            # 变成 args={} 发给 build_schematic，属于最危险的静默失败
            log.warning("工具 %s 的参数不是合法 JSON，已阻止执行: %s", name, e)
            summary = {"error": f"工具 {name} 的参数不是合法 JSON（{e}），已阻止执行；"
                                "通常是模型响应被截断，请重试。"}
            self._emit_tool_message(messages, tc, name, summary, emit, ok=False)
            self.stats["calls"] += 1
            self.stats["failed"] += 1
            return
        if not isinstance(args, dict):
            summary = {"error": f"工具 {name} 的参数必须是 JSON 对象，已阻止执行。"}
            self._emit_tool_message(messages, tc, name, summary, emit, ok=False)
            self.stats["calls"] += 1
            self.stats["failed"] += 1
            return

        if name == "publish_design_result":
            args = self._reuse_recent_simulation(args)

        if tools_mod.is_local(name):
            emit({"type": "tool_call", "name": name, "arguments": args})
            log.debug("工具 %s 参数: %s", name, json.dumps(args, ensure_ascii=False)[:2000])
            result = self._run_local_tool(name, args, emit)
            ok = not bool(result.get("error"))
        elif name == "run_python" and not self.allow_python:
            result = {"error": "用户已关闭「执行任意 Python」权限，请改用预置工具完成。"}
            ok = False
        else:
            emit({"type": "tool_call", "name": name, "arguments": args})
            log.debug("工具 %s 参数: %s", name, json.dumps(args, ensure_ascii=False)[:2000])
            try:
                result = tools_mod.call(self.cfg, name, args)
                # run_python 内部报错会用 {"ok": False}，不能只看有没有 error 字段，
                # 否则面板会把一次失败的 Python 执行标成 ✅，看不出到底失败没有。
                if "error" in result:
                    ok = False
                else:
                    ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
            except tools_mod.AdsToolTimeout as e:
                result = {"error": str(e), "kind": "exec_timeout",
                          "job_id": getattr(e, "job_id", "")}
                ok = False
            except tools_mod.AdsToolBusy as e:
                result = {"error": str(e), "kind": "busy",
                          "job_id": getattr(e, "job_id", "")}
                ok = False
            except tools_mod.AdsToolError as e:
                result = {"error": str(e)}
                ok = False

        summary = json.dumps(result, ensure_ascii=False, default=str)
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS] + " …(截断)"
        # 完整工具回显只进后台日志（logs/backend.log），面板折叠区留给思考内容
        log.debug("工具 %s 结果(ok=%s): %s", name, ok, summary[:4000])
        emit({"type": "tool_result", "name": name, "ok": ok, "summary": summary})

        # 每轮汇总的计数（与 tools.describe_result 同一套判定）
        st = self.stats
        st["calls"] += 1
        if not ok:
            st["failed"] += 1
        if (name == "run_python" and isinstance(result, dict)
                and "error" not in result and result.get("ok") is False):
            st["script_failed"] += 1
        if name == "run_simulation":
            st["sim_ok" if ok else "sim_failed"] += 1
            if (ok and isinstance(result, dict) and result.get("status") == "done"
                    and result.get("dataset_path")):
                self._last_sim = {
                    "library": args.get("library"), "cell": args.get("cell"),
                    "view": args.get("view") or "schematic",
                    "dataset_path": result["dataset_path"],
                    # 设计版本与工作区：发布复用前要验证"设计还是那一版"
                    "netlist_sha": (result.get("design_version") or {}).get("netlist_sha", ""),
                    "workspace": (result.get("workspace") or {}).get("path", ""),
                }
        elif name in {"build_schematic", "set_design_variables", "run_python"}:
            # 任意 Python 即使返回失败也可能已经部分修改设计。
            self._last_sim = None

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                # 结构化摘要：保留状态/错误/关键数值/产物引用，丢掉大数组。
                # 过去直接 [:60000] 按字符截断 JSON —— 会切出非法 JSON，
                # 模型拿到的是半行字符串而不是信息。
                "content": json.dumps(
                    _summarize_tool_result(name, result),
                    ensure_ascii=False, default=str),
            }
        )

    def _emit_tool_message(self, messages, tc, name, payload, emit, ok=False):
        """补一条 tool_result 事件与 tool 消息（用于"参数无效未执行"等场景）。"""
        text = json.dumps(payload, ensure_ascii=False)
        emit({"type": "tool_result", "name": name, "ok": ok, "summary": text})
        messages.append({
            "role": "tool", "tool_call_id": tc.get("id", ""),
            "content": text,
        })

    def _reuse_recent_simulation(self, args: dict) -> dict:
        """同一轮中设计未变时，发布结果复用刚产生的数据集。

        "设计未变"的判定从"本轮没有调用修改工具"升级为**设计版本验证**：
        用户完全可能在 ADS 里手工改了设计（插件看不到），此时旧数据集已经
        不能代表当前设计。验证方式：对比仿真时记录的网表指纹与当前指纹
        （design_fingerprint，只读）。指纹不可用（旧工具端 / ADS 离线）时
        退回旧行为并如实注明，不阻塞发布。
        """
        import os as _os
        sim = self._last_sim
        design = args.get("design") or {}
        if (not sim or "simulate" in args or "reuse_dataset" in args
                or args.get("dataset_path")):
            return args
        if (design.get("library"), design.get("cell"),
                design.get("view") or "schematic") != (
                sim["library"], sim["cell"], sim["view"]):
            return args
        self._reuse_note = ""
        if sim.get("netlist_sha"):
            try:
                fp = tools_mod.call(self.cfg, "design_fingerprint", {
                    "library": sim["library"], "cell": sim["cell"],
                    "view": sim["view"] or "schematic"})
                cur_sha = (fp.get("design_version") or {}).get("netlist_sha") or ""
                if cur_sha and cur_sha != sim["netlist_sha"]:
                    self._last_sim = None      # 设计已变，缓存数据集作废
                    self._reuse_note = (
                        f"检测到设计已变化（网表指纹 {sim['netlist_sha']} → "
                        f"{cur_sha}），已放弃复用旧数据集，将重新仿真。"
                    )
                    return args
                cur_ws = _os.path.normcase(_os.path.normpath(
                    str((fp.get("workspace") or {}).get("path") or "")))
                sim_ws = _os.path.normcase(_os.path.normpath(
                    str(sim.get("workspace") or "")))
                if cur_ws and sim_ws and cur_ws != sim_ws:
                    self._last_sim = None
                    self._reuse_note = (
                        "检测到当前工作区与仿真时不同，已放弃复用旧数据集，"
                        "将重新仿真。"
                    )
                    return args
            except Exception:  # noqa: BLE001
                self._reuse_note = ("设计版本指纹本次不可用，"
                                    "按「本轮未调用修改工具」的旧规则复用。")
        updated = dict(args)
        updated.update({"reuse_dataset": True, "dataset_path": sim["dataset_path"]})
        return updated

    # ---------------------------------------------------------- 本地工具
    def _run_local_tool(self, name: str, args: dict, emit) -> dict:
        """后端自己执行的工具（不派发到 ADS 端）。"""
        if name == "publish_design_result":
            return self._publish_design_result(args, emit)
        return {"error": f"未知的本地工具: {name}"}

    def _publish_design_result(self, args: dict, emit) -> dict:
        """跑仿真 → 读真实曲线 → 确定性评估 → 发布结果页。

        模型在这里只能给"设计与指标定义"，实测值与达标结论全部由
        design_metrics 从真实 .ds 数据算出；返回给模型的结果就是评估结果，
        所以模型无法编造数值、也无法宣称达标。
        """
        import design_service as design_svc

        try:
            spec = design_svc.build_spec(args)
        except design_svc.DesignError as e:
            return {"error": str(e)}

        root = design_svc.project_root_of(self.cfg)

        def on_step(stage, text=""):
            emit({"type": "design_stage", "stage": stage, "text": text or ""})

        try:
            job = design_svc.run_design(self.cfg, spec, root, on_step=on_step,
                                        cancel_event=self.cancel_event)
        except Exception as e:  # noqa: BLE001 — 编排层崩了也要让模型知道
            log.exception("设计评估失败: %s: %s", type(e).__name__, e)
            return {"error": f"设计评估失败：{type(e).__name__}: {e}"}

        if self.cancelled():
            return {"error": "已取消：用户停止了本轮。"
                             "已完成的仿真产物与备份保留在任务记录中。"}

        payload = job.to_dict()
        emit({"type": "design_result", "job": payload})

        reuse_note = getattr(self, "_reuse_note", "")
        self._reuse_note = ""
        metrics = [
            {k: m.get(k) for k in
             ("id", "label", "kind", "expr", "actual", "target", "comparator",
              "unit", "at", "pass", "note", "n_points", "exact")}
            for m in payload.get("metrics", [])
        ]
        sim = payload.get("sim") or {}
        artifacts = {k: v for k, v in (payload.get("artifacts") or {}).items()
                     if k != "traces"}          # traces 太大，不进模型上下文
        result = {
            "job_id": payload["job_id"],
            "design_ref": payload["design_ref"],
            "stage": payload["stage"],
            "stage_label": payload.get("stage_label", ""),
            "verdict": payload["verdict"],
            "summary": payload["summary"],
            "metrics": metrics,
            "artifacts": artifacts,
            "sim": {k: sim.get(k) for k in ("status", "error", "finished_at")},
            "design_version": payload.get("design_version") or {},
            **({"error": payload["error"]} if payload.get("error") else {}),
        }
        if reuse_note:
            result["reuse_note"] = reuse_note
        result["note"] = (
            "结果页已生成并存入当前项目（重启 ADS 后可重新打开）。"
            "上面的 actual / pass / at 是确定性评估器从真实 .ds 数据算出的，"
            "你必须原样引用：不得改写数值，不得把 pass=false 说成达标，"
            "不得补充任何评估器没有给出的实测数字。"
            "verdict 为 fail/partial 时说明差距；unknown 时说明缺失的数据或判据。"
        )
        return result
