"""Tool schemas exposed to the LLM, and dispatch to the ADS-side tool server.

The addon inside ADS runs a localhost HTTP executor (addon/ads_agent/toolserver.py);
this module forwards named tool calls to it and returns JSON results.

令牌由 ads_auth.py 统一提供（每次调用现取，令牌轮换后无需重启后端）。
注意：令牌只放在请求头里，绝不写进日志或结果 —— 日志记的是工具名、参数和耗时。
"""

import json
import time
import urllib.error
import urllib.request

import ads_auth
import adslog
import instance
from config import ads_base_url

log = adslog.get("backend.tools")

# 本机回环请求必须**绕过 HTTP 代理**：企业网络 / 沙箱环境常设 HTTP_PROXY，
# urllib 会连 127.0.0.1 的请求也发给代理，于是"工具服务明明在跑"却报连不上。
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 工具服务身份校验结果缓存：{url: (monotonic 时间, verdict)}。
#: 每次工具调用都多打一个 /health 太浪费；但**不校验**就等于把带令牌的请求
#: 发给了端口上碰巧坐着的任何程序。折中：每个 url 最多 30s 校验一次，
#: 连接层面一出错立刻作废缓存（下次调用重新判定）。
_TOOLSERVER_VERDICT: dict[str, tuple[float, dict]] = {}
_VERDICT_TTL = 30.0


def _verify_toolserver(cfg: dict) -> None:
    """确认 ``[ads]`` 配的端口上确实是**本插件、本次安装**的工具服务。

    改造前这里只看"请求发得出去"，于是端口被别的程序（或另一份安装、
    另一个 ADS 实例）占用时，后端会一直发带令牌的 /execute，对方回 401/404，
    用户看到的是"无法连接 ADS 端工具服务"这种指向错误的提示。
    """
    url = ads_base_url(cfg)
    now = time.monotonic()
    cached = _TOOLSERVER_VERDICT.get(url)
    if cached and now - cached[0] < _VERDICT_TTL:
        verdict = cached[1]
    else:
        verdict = instance.evaluate(instance.probe(url, timeout=2.0), "toolserver")
        _TOOLSERVER_VERDICT[url] = (now, verdict)

    if verdict.get("usable"):
        return
    _TOOLSERVER_VERDICT.pop(url, None)  # 判定不可用就别缓存
    ident = verdict.get("identity") or {}
    reason = verdict.get("reason")
    # 顺序很重要：instance.evaluate 会给"另一份安装"也打上 conflict 标记，
    # 泛化的冲突分支必须先让位给具体的 reason，否则最该看到的那句话会被吞掉。
    if reason == "unreachable":
        raise AdsToolError(
            "无法连接 ADS 端工具服务 "
            f"({url})。请确认：1) ADS 2027 已启动；2) 已安装并启用 ADS Agent 插件"
            "（Tools > ADS Agent）；3) 面板打开过一次。"
        )
    if reason in ("foreign_install", "no_install_id"):
        raise AdsToolError(
            f"{url} 上是**另一份 ADS Agent 安装**（或无法确认归属）的工具服务："
            f"{verdict.get('detail')}（对方 install_id="
            f"{ident.get('install_id') or '未知'}，本机 install_id="
            f"{instance.paths.install_id()}）。请先退出那一个 ADS，"
            "或统一使用同一份安装。"
        )
    if reason == "protocol_mismatch":
        raise AdsToolError(
            f"{url} 上的 ADS 端插件协议版本为 {ident.get('protocol')}，"
            f"后端为 {instance.paths.PROTOCOL_VERSION}，两者不匹配（多半是插件与"
            "后端不是同一版本）。请用同一份安装重装/重启后再试。"
        )
    if verdict.get("conflict") or reason in ("foreign_service", "wrong_service"):
        raise AdsToolError(
            f"{url} 上的服务不是本插件：{verdict.get('detail')}。"
            "请检查 config.ini 的 [ads] port 是否与 ADS 端插件一致；"
            "若端口被别的程序占用，改成一个空闲端口后重启 ADS 与后端。"
        )
    raise AdsToolError(
        f"{url} 上的服务无法确认是本次安装的 ADS Agent 工具服务"
        f"（{verdict.get('detail') or reason}）。为避免把指令发给错误的实例，已停止调用。"
    )


# ---------------------------------------------------------------------------
# OpenAI function-calling schemas (what the LLM sees)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_workspace_info",
            "description": "获取当前 ADS 工作区状态：是否打开、工作区路径、已挂接的库（含可写库）。任何操作前先调用它了解环境。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_designs",
            "description": "列出可写库中的设计（cell）及其视图（schematic/layout/symbol 等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_cells": {"type": "integer", "description": "每个库最多返回的 cell 数，默认 50"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_design_variables",
            "description": "读取某个设计中 VAR 变量方程的当前值（按 VAR 实例分组）。修改前必须先读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string", "description": "库名"},
                    "cell": {"type": "string", "description": "设计（cell）名"},
                    "view": {"type": "string", "description": "视图名，默认 schematic"},
                },
                "required": ["library", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_design_variables",
            "description": "修改设计中 VAR 变量的值并保存。values 是 {变量名: 表达式或数值} 字典，值可以是带单位的 ADS 表达式（如 \"2.5 kOhm\"）。默认写到第一个 VAR 实例，可用 instance 指定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                    "view": {"type": "string", "description": "默认 schematic"},
                    "values": {
                        "type": "object",
                        "description": '{变量名: 值}，如 {"Rbias": "120 Ohm"}',
                    },
                    "instance": {"type": "string", "description": "目标 VAR 实例名，默认第一个"},
                },
                "required": ["library", "cell", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_schematic",
            "description": (
                "【建图首选】一次性完成：放元件 + 设参数 + 连线 + 保存 + 只读复核 + "
                "连通核对 + 导线几何复核。保存前失败不会写入；保存后复核失败会报错并提供备份，"
                "设计可能已落盘。失败后不要在该设计上继续追加修改。"
                "\n推荐 layout=\"auto\"（instances 全部不写坐标时缺省即 auto）："
                "工具按信号流自动布局并做紧凑化——端口链一行从左到右，相邻间距按"
                "符号与参数文字实际占用逐对计算（RLC 链 1.4、长参数器件留避让通道）；"
                "分支行距按上一行内容动态计算；接地支路就近挂所属节点旁成组"
                "（不独占整行）；共享 GND 按伙伴引脚就近挂置；偏置/供电围绕所服务"
                "器件组织；隔离电阻等竖放桥自动选文字干净且连线不穿字的落位；"
                "威尔金森/分配器类一分二~一分五自动对称；标注避让、GROUND 朝向、"
                "变量/控制器排位、端口朝向全部自动。给任何实例写了坐标则按 explicit "
                "尊重手工位置。电气拓扑不受布局影响。"
                "\n端口首选 ads_simulation:TermG：单引脚、自带地参考（网表第二节点即全局地），"
                "不需要接 GND。ads_simulation:Term 只用于确需独立参考端的场合，"
                "且其 1 号脚（地）必须接 GROUND，否则仿真门禁会拦截。"
                "手动布局时每个 instance 需要 master、name、x、y，"
                "可选 angle/mirror/params（参数名->值）；连线 connections 每项 "
                "{\"a\": [实例名, 引脚], \"b\": [实例名, 引脚]}；VAR 用 var 字段。"
                "\nlayout=auto 的返回体含 netlist_equivalence（磁盘网表与请求连接的"
                "逐引脚等价核对，ok=false 即拓扑被改）、annotation_issues（保存后"
                "按实测标注框做归属复核：文字距符号>1.2 格=悬空、文字互压，按实例"
                "点名；空=全部贴符号）与 geometry.metrics（导线总长/绕行系数/拐弯/"
                "重复段/平行出线，以及紧凑度：主电路与全图 bbox 面积/最长导线段/"
                "超长连接数/接地支路总长——验收紧凑布局直接读这里，不要只看"
                "detour_ratio≈1）。"
                "\n所有导线（含手工 waypoints）逐段强制正交：出现斜段、穿符号、"
                "无引脚 T 接、异网交叉/重叠时建图报错并保留备份，不会落盘斜线。"
                "\ncell 不存在会自动创建；recreate=true 表示清空重建（会先强制备份，"
                "备份失败则拒绝执行）。返回里有 sim_readiness.problems：距「能仿真」还缺什么。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                    "view": {"type": "string", "description": "默认 schematic"},
                    "recreate": {"type": "boolean",
                                  "description": "true = 清空重建（先备份，失败拒绝）；默认 false = 追加"},
                    "layout": {"type": "string",
                               "enum": ["auto", "explicit"],
                               "description": "auto = 按信号流自动布局（推荐；instances 全部"
                                              "不写坐标时缺省即 auto）；explicit = 用给定的 x/y"},
                    "allow_large_geometry": {"type": "boolean",
                                             "description": "确需超长导线时设 true；默认会拦截导线远大于元件符号的布局"},
                    "instances": {
                        "type": "array",
                        "description": "要放置的实例清单（layout=auto 时 x/y 可省）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "master": {"type": "string", "description": "如 ads_tlines:MLIN、ads_simulation:TermG（推荐端口）、ads_tlines:MSUB"},
                                "name": {"type": "string"},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "angle": {"type": "number", "description": "度；layout=auto 时端口角度自动规范，可省略"},
                                "mirror": {"type": "string"},
                                "params": {"type": "object",
                                           "description": '{参数名: 值}，如 {"Subst": "MSUB1", "W": "W50"}'},
                            },
                            "required": ["master", "name"],
                        },
                    },
                    "var": {
                        "type": "object",
                        "description": "可选：放一个 VAR 变量方程实例",
                        "properties": {
                            "name": {"type": "string", "description": "默认 VAR1"},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "values": {"type": "object",
                                       "description": '{变量名: 表达式}，如 {"Z0": "50 Ohm"}'},
                        },
                        "required": ["values"],
                    },
                    "connections": {
                        "type": "array",
                        "description": "要连的线：两两引脚之间按符号外框避障；端点不共轴时自动走正交线，避免穿元件及线段交叉重叠",
                        "items": {
                            "type": "object",
                            "properties": {
                                "a": {"type": "array", "items": {}, "description": "[实例名, 引脚号或名]"},
                                "b": {"type": "array", "items": {}, "description": "[实例名, 引脚号或名]"},
                                "waypoints": {"type": "array",
                                              "description": "可选：手工指定正交折点 [[x,y],...]；穿符号、交叉或重叠时拒绝建图",
                                              "items": {"type": "array", "items": {"type": "number"}}},
                            },
                            "required": ["a", "b"],
                        },
                    },
                },
                "required": ["library", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_connections",
            "description": (
                "只读体检一个设计：实例 / 仿真控制器 / 端口 / 网络数 / 悬空引脚 / "
                "基板引用 / 指定连线是否真的连通。problems 列出的是 run_simulation "
                "门禁会拦截的问题（如缺控制器、缺端口、基板引用断裂）；warnings 只是疑点。"
                "修改设计后、仿真前用它确认状态，比直接撞仿真报错省得多轮探索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                    "view": {"type": "string", "description": "默认 schematic"},
                    "connections": {
                        "type": "array",
                        "description": "可选：要核对的连线，格式同 build_schematic.connections",
                        "items": {
                            "type": "object",
                            "properties": {
                                "a": {"type": "array", "items": {}},
                                "b": {"type": "array", "items": {}},
                            },
                            "required": ["a", "b"],
                        },
                    },
                },
                "required": ["library", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_rf",
            "description": (
                "射频物理审查（原理图级 + Layout 级）。原理图级：按微带/共面模型核对"
                "基板与导体参数、工作频率、目标阻抗一致性、电长度；MTEE 按旋转后的实际"
                "引脚坐标判定 1/2/3 端并逐端核对 W1/W2/W3 与相接线宽（贯穿对默认 W1=W2，"
                "有意变宽须有过渡结构及理由）；检查 90° 直接相接无弯折元件。"
                "Layout 级：读取真实 Layout 铜皮，检查未切角直角弯折、同层重叠、"
                "间隙、接地层缺失，并与原理图实例名对应；没有 Layout 视图或为空时"
                "如实返回『未完成 Layout 验证』。LineCalc 在本环境不可程序化调用，"
                "闭式核对一律标注验证状态，绝不臆造 W/G/L。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                },
                "required": ["library", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_simulation",
            "description": "对指定设计生成网表并用 hpeesofsim 仿真，返回数据集路径与全部输出变量名。设计内需已放置仿真控制器（AC/S-param/DC 等）。耗时操作，请耐心等待。",
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                    "view": {"type": "string", "description": "默认 schematic"},
                },
                "required": ["library", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_dataset",
            "description": "读取 .ds 数据集中指定表达式的数值：返回统计量（最小/最大/首/末）与样例行，并导出 CSV。数值结论必须来自本工具，不得臆造。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ".ds 数据集路径（run_simulation 会返回）"},
                    "expressions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要读取的变量名列表，如 [\"AC1.S(1,1)\"]；留空则列出全部变量",
                    },
                    "max_rows": {"type": "integer", "description": "返回的样例行数，默认 5"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "在 ADS 进程内执行 Python 代码（可调用 keysight.ads.de 全部 API），返回 stdout。"
                "代码会展示给用户，请写清晰、带 print 输出。"
                "【重要】执行环境在多次调用之间是保持的：变量、import、已查到的对象都会留着，"
                "不要重复 import 或重复查找同一个对象。已为你预置：de（keysight.ads.de）、"
                "db_uu、os、json、inspect，以及辅助函数——ws() 取当前工作区、"
                "ls(obj) 列出成员（不传参则给出工作区与可用库的速览）、"
                "sig(obj, '方法名') 直接打印方法签名；建图/改图相关："
                "odesign(lib, cell, write=True)（自动先备份）、recreate、put、wire、connect、"
                "setp、params、save、save_verify(lib, cell)（保存后只读复核+导线几何复核）、"
                "backup(lib, cell)、audit(lib, cell)、pins、mkcell、cells、libs。"
                "【导线硬门禁】wire() 斜段/穿符号/与已有导线交叉重叠直接报错；"
                "connect() 自动走正交并避障（找不到合格路径报错，绝不画斜线）；"
                "save() 保存前跑几何检查，斜线/异网交叉在落盘前拦截。"
                "直接调 design.add_wire 会绕过门禁 —— 禁止，一律用 wire/connect。"
                "【建图/改图一律优先用 build_schematic 工具】只有它覆盖不到的操作才用本工具，"
                "且保存后必须 close 写句柄并调用 save_verify 复核。"
                "探索不熟悉的 API 时，务必用 ls/sig 一次看清（例如 "
                "print(sig(cell, 'create_view'))），并把多个问题合并到这一次调用里一起 print。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "完整 Python 代码"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_traces",
            "description": (
                "读取 .ds 数据集里的**完整曲线**（x/y 数组 + 单位 + 数据来源），"
                "用于画曲线和带内指标判定。read_dataset 只给统计量摘要；"
                "要判断「带内增益是否 ≥15 dB」这类问题必须用本工具拿到完整数据。"
                "返回的数值即实测值，禁止改写。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ".ds 数据集路径"},
                    "expressions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要读的表达式，如 [\"dB(S(2,1))\", \"dB(S(1,1))\"]",
                    },
                    "max_points": {
                        "type": "integer",
                        "description": "单条曲线的点数上限（默认 50000，超出则保极值降采样并标 truncated）",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_schematic",
            "description": (
                "在 ADS 中打开指定的原理图（library / cell / view）。"
                "用户说「打开看看 / 打开原理图」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                    "view": {"type": "string", "description": "默认 schematic"},
                },
                "required": ["library", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_design_result",
            "description": (
                "把一次设计评估发布成对话里的「设计结果页」。后端会用真实 .ds 数据"
                "算出每项指标的实测值与达标结论，并把结果页存进当前项目（重启后可重新打开）。"
                "\n\n【重要】你只提供**设计与指标定义**：要评估哪个设计、看哪些表达式、"
                "判据和目标值是什么。**绝对不要自己填写实测数值或达标结论** —— "
                "那些由确定性评估器从真实数据算出；你填了也会被忽略，并可能被判为编造。"
                "\n\n返回结果里带每项指标的 actual / pass，你必须原样引用，不得与之矛盾。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string", "description": "用户原始要求原文"},
                    "title": {"type": "string", "description": "结果页标题，留空自动生成"},
                    "design": {
                        "type": "object",
                        "description": "设计引用",
                        "properties": {
                            "library": {"type": "string"},
                            "cell": {"type": "string"},
                            "view": {"type": "string", "description": "默认 schematic"},
                            "workspace": {"type": "string"},
                        },
                        "required": ["library", "cell"],
                    },
                    "band": {
                        "type": "object",
                        "description": "评估频段（带内指标必须给）",
                        "properties": {
                            "start": {"type": "number"},
                            "stop": {"type": "number"},
                            "unit": {"type": "string", "description": "Hz / kHz / MHz / GHz，默认 GHz 建议显式写"},
                        },
                        "required": ["start", "stop", "unit"],
                    },
                    "metrics": {
                        "type": "array",
                        "description": "指标定义列表（每项的目标值与判据由用户要求决定）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string", "description": "中文名，如「带内增益」"},
                                "kind": {
                                    "type": "string",
                                    "enum": ["min_in_band", "max_in_band", "mean_in_band",
                                             "flatness_in_band", "bandwidth_above", "value_at"],
                                    "description": "判据类型：带内最小/最大/均值/起伏/连续带宽/指定频点",
                                },
                                "expr": {"type": "string", "description": "数据集表达式，如 dB(S(2,1))"},
                                "target": {"type": "number", "description": "目标值；bandwidth_above 时是**要求的带宽**（如 0.15，单位 GHz）"},
                                "comparator": {"type": "string", "description": ">= / <= / > / <，留空按 kind 取默认"},
                                "unit": {"type": "string", "description": "目标/实测的单位，如 dB；bandwidth_above 时给带宽单位（GHz）"},
                                "at_hz": {"type": "number", "description": "kind=value_at 时的目标频点（Hz）"},
                                "threshold": {"type": "number", "description": "kind=bandwidth_above 时的纵轴门限（如 15 表示增益 ≥15 dB）"},
                            },
                            "required": ["kind", "expr"],
                        },
                    },
                    "traces": {
                        "type": "array",
                        "description": "要画到结果页上的曲线（留空则按 metrics 里用到的表达式自动去重）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "expr": {"type": "string"},
                                "label": {"type": "string"},
                                "unit": {"type": "string"},
                            },
                            "required": ["expr"],
                        },
                    },
                    "simulate": {
                        "type": "boolean",
                        "description": "是否先跑仿真（默认 true）。false 表示复用已有数据集",
                    },
                    "reuse_dataset": {
                        "type": "boolean",
                        "description": "true = 不重新仿真，直接读 dataset_path 重算指标",
                    },
                    "dataset_path": {"type": "string", "description": "复用数据集时的 .ds 路径"},
                },
                "required": ["design", "metrics"],
            },
        },
    },
]

# 工具名 -> 派发到 ADS 端的等待秒数（run_simulation 用 config.ini 的 sim_timeout）
TIMEOUTS = {
    "get_workspace_info": 60,
    "list_designs": 120,
    "get_design_variables": 120,
    "set_design_variables": 180,
    # 一次建图包含 放置+连线+保存+只读复核+连通核对，给足时间
    "build_schematic": 300,
    "check_connections": 120,
    # 物理审查要读原理图全部实例参数 + Layout 全部图形，宽面板设计也不慢
    "audit_rf": 180,
    "read_dataset": 120,
    # 完整曲线可能上万点，序列化 + 传输比摘要慢，给宽一点
    "read_traces": 300,
    "open_schematic": 60,
    "run_python": 600,
    # 只读：打开设计 + 生成网表算指纹（结果复用的版本验证用），不给模型直接调用
    "design_fingerprint": 120,
}

# 这些工具**不**派发到 ADS 端，由后端自己处理（见 agent.py）。
# 放进 TOOLS 是为了让模型能调用；放进这里是为了让 tools.call() 明确拒绝，
# 避免误当成 ADS 工具发出去变成一句莫名其妙的"未知工具"。
LOCAL_TOOLS = {"publish_design_result"}
DEFAULT_SIM_TIMEOUT = 900
# ADS 端比后端多等一会儿：让"超时"由后端统一报出，而不是 ADS 端先回 504
# 结果后端还在傻等（两边用同一个数值时会出现这种竞态）。
ADS_TIMEOUT_MARGIN = 30


class AdsToolError(RuntimeError):
    pass


class AdsToolTimeout(AdsToolError):
    """执行超时：作业已开跑但没在时限内完成（可能仍在收尾）。"""

    def __init__(self, message: str, job_id: str = ""):
        super().__init__(message)
        self.job_id = job_id


class AdsToolBusy(AdsToolError):
    """ADS 端队列已满：调用方应稍后重试（不是故障，是忙碌）。"""

    def __init__(self, message: str, job_id: str = ""):
        super().__init__(message)
        self.job_id = job_id


def timeout_for(cfg: dict, name: str) -> int:
    """该工具在**后端侧**的等待上限（秒）。"""
    if name == "run_simulation":
        try:
            return max(1, int(cfg.get("sim_timeout") or DEFAULT_SIM_TIMEOUT))
        except (TypeError, ValueError):
            return DEFAULT_SIM_TIMEOUT
    return TIMEOUTS.get(name, 300)


def is_local(name: str) -> bool:
    """是否是后端本地处理的工具（不派发到 ADS 端）。"""
    return name in LOCAL_TOOLS


def describe_result(name: str, result) -> tuple:
    """把一次工具调用的结果翻译成 (日志级别, 结果标签)。

    过去的坑：HTTP 200 + {"ok": false} 会被记成"成功  run_python_ok=False"，
    排查时极易误读成调用成功。这里把三种结局分开说清：
      失败           —— ADS 端报错（有 error 字段）
      脚本执行失败   —— 请求本身成功，但 run_python 的代码抛了异常（ok=false）
      成功           —— 其余
    """
    if isinstance(result, dict) and "error" in result:
        return "warning", "失败"
    if isinstance(result, dict) and result.get("ok") is False:
        return "warning", "请求成功、脚本执行失败"
    return "info", "成功"


def call(cfg: dict, name: str, args: dict, job_id: str = "") -> dict:
    """Forward a tool call to the ADS-side executor, return its JSON result.

    每次调用生成唯一的 client 侧 job_id（调用方也可传入以便跨面板/后端追踪），
    超时/取消时可用它到 ADS 端 /cancel、/health 查询作业下场 —— 避免"超时后
    不知道作业死活，只能重试一遍"。
    """
    if is_local(name):
        raise AdsToolError(f"{name} 是后端本地工具，应由 agent 处理（不应派发到 ADS）")
    if name not in TIMEOUTS and name != "run_simulation":
        raise AdsToolError(f"未知工具: {name}")
    # 派发前先确认对端身份：带令牌的请求不能发给"碰巧占了那个端口"的服务
    _verify_toolserver(cfg)
    import uuid
    job_id = job_id or uuid.uuid4().hex
    url = ads_base_url(cfg) + "/execute"
    timeout = timeout_for(cfg, name)
    # 令牌现取（读盘），这样令牌轮换后正在运行的后端也能立刻跟上
    token = ads_auth.ensure_token()
    body = json.dumps(
        {"name": name, "args": args, "timeout": timeout + ADS_TIMEOUT_MARGIN,
         "job_id": job_id}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            ads_auth.TOKEN_HEADER: token,
        },
        method="POST",
    )
    _t0 = time.perf_counter()

    def _elapsed() -> str:
        return f"{int((time.perf_counter() - _t0) * 1000)}ms"

    log.info("-> %s  参数=%s  超时=%ss", name,
             adslog._clip(adslog.redact(args), 400), timeout)
    try:
        with _LOOPBACK.open(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        level, label = describe_result(name, result)
        detail = ""
        if isinstance(result, dict) and "error" in result:
            detail = adslog._clip(adslog.redact(result["error"]), 1500)
        elif label != "成功":
            # 脚本失败的原文在这里 —— 排查时最需要的就是这段
            detail = adslog._clip(adslog.redact(str(result.get("stdout", ""))), 1500)
        getattr(log, level)("<- %s  %s(%s)%s", name, label, _elapsed(),
                            f": {detail}" if detail else "")
        return result
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
            detail = payload.get("error", "")
        except Exception:
            payload, detail = {}, ""
        if e.code == 503:
            # 队列已满（忙碌）—— 与故障区分开，调用方可稍后重试
            log.warning("<- %s  队列已满(%s): %s", name, _elapsed(), detail)
            raise AdsToolBusy(f"ADS 工具队列已满：{detail or '请稍后重试'}", job_id) from e
        if e.code == 504:
            log.error("<- %s  执行超时(%s) job_id=%s: %s", name, _elapsed(), job_id, detail)
            raise AdsToolTimeout(
                f"工具 {name} 执行超时（>{timeout}s）。作业 job_id={job_id} 可能"
                f"仍在 ADS 端收尾（可在 ADS 端 /health 查询），请勿盲目重复提交。",
                job_id,
            ) from e
        log.error("<- %s  HTTP %s(%s): %s", name, e.code, _elapsed(), detail)
        raise AdsToolError(f"ADS 端返回 HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        log.error("<- %s  连接失败(%s): %s", name, _elapsed(), e.reason)
        raise AdsToolError(
            "无法连接 ADS 端工具服务 "
            f"({url})。请确认：1) ADS 2027 已启动；2) 已安装并启用 ADS Agent 插件"
            "（Tools > ADS Agent）；3) 面板打开过一次。"
        ) from e
    except TimeoutError as e:
        # 本端 socket 读超时：连接层面先放弃了，作业可能仍在 ADS 端执行
        log.error("<- %s  执行超时 >%ss job_id=%s", name, timeout, job_id)
        raise AdsToolTimeout(
            f"工具 {name} 执行超时（>{timeout}s）。作业 job_id={job_id} 可能仍在"
            f"ADS 端执行，请勿盲目重复提交。",
            job_id,
        ) from e
    except Exception as e:  # noqa: BLE001 — 任何异常都要留痕
        log.exception("<- %s  未预期异常: %s: %s", name, type(e).__name__, e)
        raise


def cancel_ads_jobs(cfg: dict, job_ids: list) -> dict:
    """请求 ADS 端取消作业（排队中的跳过、执行中的只打标记）。

    返回 {"cancelled": [...], "running": [...], "unknown": [...]}。
    网络失败不抛异常 —— 取消是尽力而为，失败只影响提示精度。
    """
    url = ads_base_url(cfg) + "/cancel"
    body = json.dumps({"job_ids": [str(j) for j in (job_ids or []) if j]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
        method="POST",
    )
    try:
        with _LOOPBACK.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("取消 ADS 作业失败（尽力而为）: %s: %s", type(e).__name__, e)
        return {"cancelled": [], "running": [], "unknown": [],
                "error": f"{type(e).__name__}: {e}"}
