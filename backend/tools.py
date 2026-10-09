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
            "name": "list_shared_model_packages",
            "description": (
                "查询 ADS Agent 统一 libraries 目录中已备份的原厂模型 ZIP。"
                "用户提到其它 Workspace 上传的模型、libraries 中的库或跨工程导入时先调用。"
                "返回 package_id、厂家、版本和包类型；选择时使用返回的真实 package_id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "按文件名、厂家、版本、型号或 package_id 搜索"},
                    "package_kind": {"type": "string", "enum": ["", "touchstone", "design_kit", "mixed", "unknown"]},
                    "max_items": {"type": "integer", "description": "最多返回条数，默认 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_shared_models",
            "description": (
                "在统一 libraries 备份的 ZIP 中按具体型号、cell 或库名检索真实文件证据。"
                "无需先解压到工作区。返回所属 package_id 和包内文件证据，"
                "用户明确要求使用时再调用 import_shared_model_package。"
                "检索命中不代表模型已加载或已通过仿真；候选不唯一时先确认型号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "实际料号、cell 或库名"},
                    "max_items": {"type": "integer", "description": "最多返回条数，默认 50"},
                    "package_kind": {"type": "string", "enum": ["", "touchstone", "design_kit", "mixed", "unknown"]},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_shared_model_package",
            "description": (
                "把统一 libraries 目录中备份的模型包复制到当前 ADS Workspace，"
                "然后执行安全解压、索引及只读库挂接。用户明确要求从 libraries 导入/使用时调用。"
                "必须先用 list_shared_model_packages 或 search_shared_models 得到真实 package_id。"
                "目标 Workspace 由系统绑定，不能传路径。导入成功仍需验证模型后才可称可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_id": {"type": "string", "description": "共享库查询返回的 package_id"},
                    "kit_root": {"type": "string", "description": "多候选套件根时从 inspect_model_package 返回值中选"},
                    "vendor_filter": {"type": "string", "description": "可选厂家库过滤"},
                },
                "required": ["package_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_model_packages",
            "description": (
                "列出当前 ADS Workspace 里已保存的**原厂模型压缩包资产**"
                "（用户通过聊天附件上传的 ZIP）。返回每个包的 package_id、原始文件名、"
                "识别出的包类型（touchstone / design_kit / mixed / unknown）、供应商、版本、"
                "处理状态、型号数量。\n"
                "**package_id 是唯一稳定的句柄**（形如 pkg_xxxxxxxxxxxxxxxx），"
                "后续所有模型工具都用它，不要用文件名指代。\n"
                "用户说「我之前上传的村田/TDK 模型」「上次那个包」时先调它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_kind": {
                        "type": "string",
                        "enum": ["", "touchstone", "design_kit", "mixed", "unknown"],
                        "description": "按包类型过滤，留空返回全部",
                    },
                    "state": {
                        "type": "string",
                        "description": "按状态过滤，如 ready / pending_import / failed；留空返回全部",
                    },
                    "max_items": {"type": "integer", "description": "最多返回条数，默认 50"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_model_package",
            "description": (
                "查看某个模型包的详细信息：包结构识别依据（evidence）、套件根目录候选、"
                "内含文件类型统计、型号索引、库挂接信息、验证结果与最近错误。\n"
                "**不解压、不加载、不执行包内任何脚本** —— 只读清单与索引。"
                "用户问「这个包里有什么」「能不能用」时先调它再决定是否导入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_id": {"type": "string", "description": "包 ID，如 pkg_ab12cd34ef567890"},
                    "include_models": {
                        "type": "boolean",
                        "description": "是否返回完整型号索引，默认 true",
                    },
                    "max_models": {"type": "integer", "description": "型号索引返回上限，默认 200"},
                },
                "required": ["package_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_model_package",
            "description": (
                "把已保存的模型包**按需**解压并导入当前 ADS Workspace："
                "安全解压 → 识别套件根目录 → 通过官方 API 挂接到本工作区 → 建模型索引。\n"
                "**只有用户明确要求导入/解压/使用某个包时才调用**（如「解压刚才那个包」"
                "「导入之前上传的 TDK 模型」）。上传本身不会自动导入。\n"
                "Design Kit 默认以**只读**方式挂接到目标工作区（不会去改全局 Favorite "
                "Design Kit 设置，也不会给原厂库写权限）。\n"
                "同一个包重复导入是幂等的，不会产生重复挂接。\n"
                "导入完成**不等于**模型可用 —— 必须再用 validate_model_import 验证。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_id": {"type": "string", "description": "包 ID"},
                    "kit_root": {
                        "type": "string",
                        "description": "可选：套件根目录的包内相对路径。识别出多个候选时必须先"
                                       "inspect_model_package 看 candidates，再由你或用户选定，"
                                       "不要武断选第一个",
                    },
                    "vendor_filter": {
                        "type": "string",
                        "description": "可选：只导入属于该供应商的库（如 tdk / murata / infineon）",
                    },
                },
                "required": ["package_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_vendor_models",
            "description": (
                "在**已挂接到当前工作区的**原厂模型库里检索可用元件（cell），"
                "包含只读的原厂库 —— 这弥补了 list_designs 只能看到可写库的限制。\n"
                "可按库名、名称前缀、类型（design_kit / builtin / workspace）过滤。\n"
                "返回元件的库:cell 引用、视图列表与元件模型定义（model_def）里真实读到的"
                "参数定义 —— 拿不到参数就如实标 null，**不要根据元件名猜参数默认值**。"
                "\n选型时**必须先调用本工具拿到真实的库名与 cell 名**，"
                "绝不编造库名、cell 名或参数值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string", "description": "限定某个库；留空查所有已挂接库"},
                    "name_prefix": {"type": "string", "description": "cell 名前缀过滤，如 TDK_ / BFP"},
                    "max_items": {"type": "integer", "description": "最多返回条数，默认 100"},
                    "include_params": {
                        "type": "boolean",
                        "description": "是否读取每个元件的 model_def 参数定义，默认 true；"
                                       "元件很多时建议关掉以免响应过大",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vendor_model_info",
            "description": (
                "查看**单个**元件的详细信息：所属库与库模式（只读/可写）、cell、视图、"
                "model_def 的参数定义（名称/类型/默认值/取值范围/单位）、端口定义、"
                "以及该元件在模型索引里的频率范围与参考阻抗（仅当文件里真实声明时才给值）。\n"
                "用于回答「这个型号有哪些参数可设」「参考阻抗是多少」这类问题。"
                "\n**频率范围覆盖不到目标频段时要明确提示，不静默外推**；"
                "**固定型号的 S 参数模型不能当连续电容/电感值修改** —— "
                "外围连续参数优化与原厂型号离散选型是两件事，不要混为一谈。"
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
            "name": "validate_model_import",
            "description": (
                "验证某个元件模型在当前工作区里**真的可用**：库是否已挂接、"
                "cell 是否存在且可解析、请求参数是否与元件定义一致、"
                "端口定义与数量、以及当前工作区与模型包所属工作区是否一致。\n"
                "**模型与文件指纹的分阶段证据会写入工作区台账**；单个元件验证不代表整包"
                "可用，整包只有在完整索引中的每个模型都达到仿真验证后才会标记就绪。\n"
                "run_smoke_sim=true 时会额外做一次最小基础仿真（建临时 cell、放元件、"
                "跑仿真）来实证；默认 false，**用户没要求验证就不要自动跑耗时仿真**。"
                "\n型号切换时用它核对端口定义、参数与连接方式是否匹配。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "cell": {"type": "string"},
                    "master": {"type": "string"},
                    "model_file": {"type": "string", "description": "Touchstone 文件路径；可为当前工作区相对路径"},
                    "package_id": {"type": "string"},
                    "model_id": {"type": "string"},
                    "component": {"type": "string"},
                    "variant": {"type": "object"},
                    "parameters": {
                        "type": "object",
                        "description": "要放置的元件参数，如 {\"Freq\": \"2.4 GHz\"}；留空只做静态校验",
                    },
                    "run_smoke_sim": {
                        "type": "boolean",
                        "description": "是否额外做一次最小基础仿真实证，默认 false",
                    },
                    "require_smoke_sim": {"type": "boolean"},
                    "target_band": {"type": "object"},
                    "bias": {"type": "object"},
                    "package": {"type": "string"},
                    "temperature": {"type": "string"},
                },
                "anyOf": [
                    {"required": ["library", "cell"]},
                    {"required": ["model_file"]},
                    {"required": ["master"]},
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_vendor_palette",
            "description": (
                "把**已导入并挂接**的原厂元件包在 ADS **原生元件列表**里打开/定位："
                "让用户在 ADS 真正自带的 Palette / Component Library 里看到该包的真实"
                "分类与元件，再点击放置并选真实料号。\n"
                "**仅当用户明确要求在原生元件列表中打开时调用**"
                "（如「在 ADS 元件列表里打开它」「把 TDK 分类调出来」）。"
                "只是查看包内容请用 inspect_model_package，不要用本工具。\n"
                "本工具只接受 package_id（及可选的 library / category / view），"
                "**不接受任何文件路径** —— 目标工作区与套件根目录一律由后端从可信"
                "上下文解析，你不要也不要尝试传路径。\n"
                "返回体里的 **limits 要如实转述**：本机 ADS 可能没有程序化打开 Palette "
                "窗口的 API，此时 outcome 会是 loaded_only（库已加载但界面无法代开），"
                "**不得改口说「已为你选中分类」**。打开原生列表也**不代表模型可用** —— "
                "仍需 validate_model_import 验证后才能放进电路仿真。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_id": {
                        "type": "string",
                        "description": "包 ID（list_model_packages 返回的稳定句柄）",
                    },
                    "library": {
                        "type": "string",
                        "description": "可选：要定位的库名；留空取该包第一个已挂接库",
                    },
                    "category": {
                        "type": "string",
                        "description": "可选：期望定位的 palette 分类/组名",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["schematic", "layout"],
                        "description": "可选：目标视图，默认 schematic",
                    },
                },
                "required": ["package_id"],
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
    {
        "type": "function",
        "function": {
            "name": "run_design_optimization",
            "description": (
                "仅在用户明确要求优化时，对已有设计任务执行有界的真实仿真优化。"
                "每个候选都实际应用、重新仿真并由评估器判定；自动限制轮数并保存证据。"
                "连续参数优化只允许给变量值；型号离散选型必须提供真实元件替换信息，"
                "若当前没有可安全替换的工作流，应先说明无法自动替换，不要假装已优化。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "已有设计结果页返回的 job_id"},
                    "kind": {"type": "string", "enum": ["continuous", "discrete"]},
                    "candidates": {"type": "array", "items": {"type": "object"}},
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 12},
                    "stop_on_first_pass": {"type": "boolean"},
                    "on_error": {"type": "string", "enum": ["stop", "continue"]},
                    "model_conditions": {"type": "object"},
                    "reference_ohm": {"type": "number"},
                },
                "required": ["job_id", "kind", "candidates", "max_iterations"],
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
    # ---- 原厂模型包 ----
    # 模型包工具的等待上限要按"最坏情况"给：解压 40MB 级 Design Kit + 建索引
    # 是几十秒级，挂接要进 ADS 工作区；只读库浏览在大套件里上万个 cell 也偏慢。
    "list_model_packages": 60,
    "inspect_model_package": 120,
    # 挂接要进 ADS 工作区改 lib.defs。编排层按阶段拆开派发（而不是一个大
    # import_model_package），这样取消能在阶段边界生效 —— 对应 ADS 侧
    # model_ops.attach_design_kit。这三个名字漏登记过一次，后端会把它们判成
    # "未知工具"，表现为 Design Kit 挂接 100% 失败且报错误导。
    "attach_design_kit": 900,
    "detach_design_kit": 180,
    "list_readonly_libraries": 120,
    "list_library_components": 300,
    "inspect_component_model": 180,
    # 解压 + 挂接 + 建索引合并在一次调用里，给足预算（120s 在 40MB 包上会超时）
    "import_model_package": 900,
    "import_shared_model_package": 900,
    "list_shared_model_packages": 60,
    "search_shared_models": 120,
    # 上万个 cell 的原厂库：翻库本身不慢，但读取 model_def 参数定义很贵
    "list_vendor_models": 300,
    "get_vendor_model_info": 180,
    "validate_model_import": 300,
    # 打开/定位原生元件列表：ADS 端要读 eesof_lib_cfg → boot.ael → palette.ael，
    # 首批加载大套件可能触发 aelcomp 重编，给足预算（>240）。
    "open_vendor_palette": 240,
}

# 这些工具**不**派发到 ADS 端，由后端自己处理（见 agent.py）。
# 放进 TOOLS 是为了让模型能调用；放进这里是为了让 tools.call() 明确拒绝，
# 避免误当成 ADS 工具发出去变成一句莫名其妙的"未知工具"。
LOCAL_TOOLS = {"publish_design_result", "run_design_optimization"}

# 原厂模型包工具：由后端读取 Workspace 的模型清单来回答（纯读本地清单，
# 不必把 ADS 叫起来），只有真正要动 ADS 库的导入/验证才派发到 ADS 端。
# 目标 Workspace 一律由**可信应用上下文**（ADS 当前打开的工作区）绑定，
# 绝不接受 LLM 传入的任意路径 —— 否则模型一句"导入到 C:\..."就能写到别处。
MODEL_LOCAL_TOOLS = {
    "list_model_packages",
    "list_shared_model_packages",
    "search_shared_models",
    "inspect_model_package",
}
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


def is_model_local(name: str) -> bool:
    """是否是模型包的本地只读工具（读清单即可回答，不必叫起 ADS）。"""
    return name in MODEL_LOCAL_TOOLS


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
