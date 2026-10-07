"""ADS-side tool implementations, executed inside the ADS 2027 process.

All functions return JSON-serializable dicts; on failure they raise —
the toolserver converts exceptions into {"error": ...} for the backend.

**线程约定**：本模块的函数由 toolserver 在 Qt 主线程调用（keysight.ads.de 的
Design/Workspace 对象只能在那里用）。唯一例外是 run_simulation 的仿真阶段：
它被显式交给后台线程，见该函数的说明。每个处理器都接受第二个参数 ctx
（toolserver.JobContext），只有需要异步收尾的处理器会用它。

API surface verified against ADS 2027 (650) bundled docs/examples:
  de.active_workspace() / workspace_is_open() / open_workspace()
  db_uu.open_design("lib:cell:view", mode=DesignMode.APPEND)   # 写用 APPEND！
  DesignMode.WRITE 是空白覆盖，会把原设计清空，见 _design_mode 注释
  design.instances / find_instance / inst.is_var_instance / inst.vars
  design.generate_netlist() -> str
  edatoolbox.ads.CircuitSimulator().run_netlist(netlist, output_dir=...)
  dataset.open(path)["AC1.AC"].to_dataframe()

设计写入安全（2026-09-24 在 AI_lib:Wilkinson_2G4 上实测，见 tests/probes/S*.py）：
  * cell.path —— Cell 对象（lib.cells 迭代和 design.cell 上都有）直接给出
    cell 的物理目录（含 OA 的 % 转义），是写入前备份的定位正路；
  * InstPin.net —— 已连线设计的引脚返回 <ScalarNet "N_A">，design.nets
    可迭代：保存后的连通性核对以此为准；
  * 同一设计的一个句柄 close_design() 会让另一个句柄失效 —— 所以"保存后
    只读复核"必须先关写句柄、再重开只读，绝不能两个句柄并用；
  * **db_uu.Transaction 包裹的 add_instance 在 save_design() 后静默丢失**
    （会话里看得到，落盘重开就没了）—— 放置/赋值一律直接操作，不包
    Transaction。这是历史上"元件放进去又没了"的第二个根因（第一个是
    DesignMode.WRITE 的空白覆盖语义）；
  * **add_wire 端点精确压在引脚上也不会合并电气网络** —— 网表里两个引脚
    仍在各自独立的 N__x 节点上。连接必须再显式赋值 pin.net（_bind_pins）。
    这是历史上"看着连上了、仿真结果却是悬空"的根因。
"""

import datetime
import importlib
import math
import os
import re
import shutil
import threading
import time

import rf_audit  # 射频物理审查的纯计算层（同为本地模块，可离线测试）

# toolserver 热重载 ads_ops 时，依赖模块会命中 sys.modules 缓存拿旧代码；
# 这里在每次 ads_ops（重）加载时强制刷新，保证审查逻辑与磁盘一致。
importlib.reload(rf_audit)


def _de():
    import keysight.ads.de as de

    return de


def _db_uu():
    import keysight.ads.de.db_uu as db_uu

    return db_uu


def _require_workspace():
    de = _de()
    if not de.workspace_is_open():
        raise RuntimeError("当前没有打开的工作区，请先在 ADS 中打开或新建一个 Workspace")
    return de.active_workspace()


# ---------------------------------------------------------------------------
# workspace / designs
# ---------------------------------------------------------------------------

def get_workspace_info(args: dict, ctx=None) -> dict:
    de = _de()
    info = {"workspace_open": False, "ads_api": "keysight.ads.de"}
    try:
        info["ads_version"] = os.environ.get("HPEESOF_DIR", "")  # install root as hint
    except Exception:
        pass
    if de.workspace_is_open():
        ws = de.active_workspace()
        info.update(
            {
                "workspace_open": True,
                "path": str(ws.path),
                "libraries": list(ws.library_names or []),
                "writable_libraries": list(ws.writable_library_names or []),
            }
        )
    return info


def list_designs(args: dict, ctx=None) -> dict:
    max_cells = int(args.get("max_cells") or 50)
    ws = _require_workspace()
    designs = []
    for lib in list(ws.libraries or []):
        try:
            lib_name = lib.name
        except Exception:
            continue
        if lib_name not in list(ws.writable_library_names or []):
            continue
        entry = {"library": lib_name, "cells": []}
        try:
            for cell in list(lib.cells or [])[:max_cells]:
                views = []
                try:
                    views = [str(getattr(v, "view_name", getattr(v, "name", ""))) for v in (cell.views or [])]
                except Exception:
                    pass
                entry["cells"].append({"cell": cell.name, "views": views})
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        designs.append(entry)
    return {"designs": designs}


# ---------------------------------------------------------------------------
# VAR variables
# ---------------------------------------------------------------------------

def _design_mode(write: bool):
    """拿到 DesignMode 枚举成员。

    实测（ADS 2027，见 tests/probes/N_write_semantics.py 与 O_append.py）：

      * 传字符串 "READ_ONLY" / 整数 0 都会报错，必须给**枚举成员**
      * READ_ONLY(0) = 只读，拿到磁盘上的内容
      * WRITE(1)     = 打开一个**空白**的可写设计！save_design() 会把磁盘上
                       的原设计整个覆盖成空 —— 之前「元件放进去又没了 /
                       has no instances」就是这个造成的
      * APPEND(2)    = 打开磁盘上的现有设计并**追加**修改，save_design() 正确
                       落盘 —— 写操作一律用这个

    所以 write=True 取 APPEND，绝不能用 WRITE。
    """
    want = "APPEND" if write else "READ_ONLY"
    for mod_path in ("keysight.ads.de.db_uu", "keysight.ads.de._pde.db"):
        try:
            mod = importlib.import_module(mod_path)
            dm = getattr(mod, "DesignMode", None)
            if dm is None:
                continue
            member = getattr(dm, want, None)
            if member is not None:
                return member
        except Exception:  # noqa: BLE001
            continue
    # 兜底：枚举的 .str 取值（'ReadOnly' / 'Append'）也是被接受的
    return "Append" if write else "ReadOnly"


def _open_design(library: str, cell: str, view: str, write: bool):
    db_uu = _db_uu()
    name = f"{library}:{cell}:{view}"
    try:
        return db_uu.open_design(name, mode=_design_mode(write))
    except Exception as e:
        raise RuntimeError(f"无法打开设计 {name}: {e}")


def _close_design(design) -> None:
    for attr in ("close", "close_design"):
        closer = getattr(design, attr, None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
            return


def _inst_name(inst) -> str:
    """实例名。实测只有 .name / .inst_name 有值，.instance_name 恒为 None。"""
    for attr in ("inst_name", "name", "instance_name"):
        v = getattr(inst, attr, None)
        if v:
            return str(v)
    return ""


def _inst_master(inst) -> str:
    """实例的主控 cell 名（如 MLIN / S_Param / Term）。"""
    for attr in ("master_name", "cell_name", "master_cell_name"):
        v = getattr(inst, attr, None)
        if v:
            return str(v)
    return ""


def _var_instances(design) -> list:
    out = []
    try:
        instances = list(design.instances or [])
    except Exception as e:
        raise RuntimeError(f"无法遍历设计实例: {e}")
    for inst in instances:
        try:
            if getattr(inst, "is_var_instance", False):
                out.append(inst)
        except Exception:
            continue
    return out


# ADS 仿真控制器的主控 cell 名（ads_simulation 库里实测存在）
_SIM_CONTROLLERS = {
    "S_Param", "AC", "DC", "HarmonicBalance", "LSSP", "Tran", "Transient",
    "ParamSweep", "Envelope", "X_Param", "SP_NWA", "SP", "HB", "ACsim",
    "DCsim", "TRAN", "LSSPsim", "X_ParamSim",
}
# 端口 / 终端。TermG 是单脚自带地参考的端口（实测网表 Port:x N 0，第二节点
# 是全局地 0），2026-09-28 起作为接地参考端口的默认推荐；Term 保留给需要
# 独立参考端的场合。
_PORT_CELLS = {"Term", "TermG", "Port", "PORT", "TERM"}
# 接地符号主控名
_GND_MASTERS = {"GROUND", "GND"}


def _unquote(value: str) -> str:
    """ADS 字符串参数的值带一层双引号（如 Subst='"MSUB1"'），剥掉再比。"""
    v = str(value or "").strip()
    if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
    return v.strip()


def _pin_label(pin):
    """引脚的可用标签：编号（编号引脚）或名字（具名引脚）。取不到返回 None。"""
    term = getattr(pin, "inst_term", None)
    if term is None:
        return None
    try:
        return term.term_number if getattr(term, "is_numbered", True) else term.term_name
    except Exception:  # noqa: BLE001
        return None


def _net_label(net) -> str:
    """网络对象 -> 名字。优先属性，其次从 str 形如 <ScalarNet "N_A"> 里抠。"""
    if net is None:
        return ""
    for attr in ("name", "net_name"):
        try:
            v = getattr(net, attr, None)
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return str(v)
    m = re.search(r'"([^"]+)"', str(net))
    return m.group(1) if m else str(net)


# 这些控制器没有端口就无法构建激励（DC 单独跑不需要端口，不在此列）
_NEEDS_PORT_MASTERS = {"S_Param", "SP", "AC", "HarmonicBalance", "HB", "LSSP",
                       "Envelope", "X_Param", "Tran", "Transient", "ACsim"}


def _needs_port(master: str) -> bool:
    m = str(master or "")
    return m in _NEEDS_PORT_MASTERS or m.startswith("SP_NWA")


def _design_audit(design) -> dict:
    """仿真前的体检：实例数、控制器、端口、基板引用、网络连通。

    之前模型建完图直接 run_simulation，得到的只有
    "has no instances" / "No Simulation Component specified" /
    "Expected a substrate model" 三句底层报错，它根本不知道自己漏了什么。
    这里把缺的东西点名说出来，并给出门禁判定用的全部事实。
    """
    try:
        insts = list(design.instances or [])
    except Exception as e:  # noqa: BLE001
        return {"n_instances": 0, "error": f"{type(e).__name__}: {e}"}

    controllers, ports, others = [], [], []
    names = []
    subst_refs = []
    for inst in insts:
        entry = {"name": _inst_name(inst), "master": _inst_master(inst)}
        name, master = entry["name"], entry["master"]
        names.append(name)
        if master in _SIM_CONTROLLERS:
            controllers.append(entry)
        elif master in _PORT_CELLS:
            ports.append(entry)
        else:
            others.append(entry)
        # 基板引用：微带/共面等传输线的 Subst 参数指向一个基板实例名
        try:
            for p in list(inst.parameters):
                if str(getattr(p, "name", "")).lower() == "subst":
                    subst_refs.append((name, master, str(getattr(p, "value", ""))))
        except Exception:  # noqa: BLE001
            continue

    name_set = set(names)
    broken_subst = []
    for iname, master, raw in subst_refs:
        ref = _unquote(raw)
        if not ref or "(" in ref:      # 表达式不是实例引用，不核对
            continue
        if ref not in name_set:
            broken_subst.append({"instance": iname, "master": master, "ref": ref})

    # 网络连通：引脚 -> 网络；没有网络的引脚是悬空引脚
    pin_nets = {}
    net_members = {}      # 网络名 -> [(实例名, 主控, 引脚标签), ...]
    floating = []
    shorted_ports = []
    term_pin_nets = {}    # Term 实例名 -> {引脚号: 网络名}（地脚参考检查用）
    for inst in insts:
        master = _inst_master(inst)
        if master in _SIM_CONTROLLERS:
            continue                   # 控制器没有需要连接的引脚
        try:
            plist = list(inst.inst_pins)
        except Exception:  # noqa: BLE001
            continue
        if not plist:
            continue                   # 基板 / VAR 这类本来就没有引脚
        numbered_nets = {}
        for p in plist:
            try:
                net = _net_label(getattr(p, "net", None))
            except Exception:  # noqa: BLE001
                net = ""
            label = _pin_label(p)
            if net:
                pin_nets[net] = pin_nets.get(net, 0) + 1
                net_members.setdefault(net, []).append(
                    (_inst_name(inst), master, label))
                if label is not None:
                    numbered_nets[str(label)] = net
            else:
                floating.append({"instance": _inst_name(inst), "master": master,
                                 "pin": label})
        if master == "Term" and numbered_nets.get("1") and (
                numbered_nets["1"] == numbered_nets.get("2")):
            shorted_ports.append({"instance": _inst_name(inst),
                                  "net": numbered_nets["1"]})
        if master == "Term":
            term_pin_nets[_inst_name(inst)] = dict(numbered_nets)

    # 端口地参考：Term 的地脚（1 脚）网络里必须真的有接地符号。
    # 实测（Wilkinson_1G7_ML，2026-09-28）：地脚悬空时网表里
    # Port:Term1 N__0 N__2 的 N__0 是个只挂在端口上的孤立网络 —— 仿真
    # "成功"但端口参考开路，结果不可信。TermG 自带地参考，不在检查范围。
    port_ground_issues = []
    for iname, nn in term_pin_nets.items():
        gnet = nn.get("1")
        if not gnet:
            continue                   # 地脚完全没连，floating_pins 已报
        members = net_members.get(gnet, [])
        if gnet != "0" and not any(m[1] in _GND_MASTERS for m in members):
            port_ground_issues.append({"instance": iname, "net": gnet})

    nets = []
    try:
        for i, n in enumerate(design.nets or []):
            if i >= 400:               # 大设计只统计个概数，够诊断用
                break
            nets.append(_net_label(n) or str(n))
    except Exception:  # noqa: BLE001
        pass

    return {
        "n_instances": len(insts),
        "controllers": controllers,
        "ports": ports,
        "others": others,
        "instances": [{"name": _inst_name(i), "master": _inst_master(i)} for i in insts],
        "n_nets": len(nets),
        "nets": nets,
        "pin_nets": pin_nets,
        "floating_pins": floating[:40],
        "shorted_ports": shorted_ports,
        "port_ground_issues": port_ground_issues,
        "broken_substrate_refs": broken_subst,
    }


def _gate_problems(audit: dict, name: str) -> list:
    """仿真门禁：返回**阻断性**问题列表（空列表 = 允许仿真）。"""
    problems = []
    if audit.get("error"):
        return [f"无法读取设计 {name} 的实例: {audit['error']}"]

    if audit.get("n_instances", 0) == 0:
        lib, cell = library_cell(name)
        problems.append(
            f"设计 {name} 是空的（0 个实例），无法仿真。\n"
            f"最常见原因：之前是在只读设计上放元件 —— "
            f"add_instance 之后 save_design() 会抛 "
            f"\"Attempt to save a read-only design\"，元件全部丢失。\n"
            f"正确做法：d = odesign('{lib}', '{cell}', write=True)，"
            f"放完元件后 save(d)，再重开只读 print(len(list(d.instances))) 确认落盘。"
        )

    if not audit.get("controllers"):
        lib, cell = library_cell(name)
        problems.append(
            f"设计 {name} 里没有仿真控制器，hpeesofsim 会报 "
            f"\"No Simulation Component specified\"。\n"
            f"请先在写模式下放一个控制器，例如：\n"
            f"    d = odesign('{lib}', '{cell}', write=True)\n"
            f"    sp = put(d, 'ads_simulation:S_Param', 0, -10, name='SP1')\n"
            f"    setp(sp, 'Start', '2 GHz'); setp(sp, 'Stop', '3 GHz')\n"
            f"    setp(sp, 'Step', '0.01 GHz')   # parameters 是 list，用 setp\n"
            f"    save(d)\n"
            f"可用的控制器主控名（ads_simulation 库）："
            f"S_Param / AC / DC / HarmonicBalance / LSSP / Tran / ParamSweep。\n"
            f"{_audit_hint(audit, name)}"
        )

    broken = audit.get("broken_substrate_refs") or []
    if broken:
        lines = [f"  - {b['instance']}({b['master']}) 的 Subst 指向不存在的实例 "
                 f"{b['ref']!r}" for b in broken]
        problems.append(
            "基板引用缺失，hpeesofsim 会报 \"Expected a substrate model for "
            "parameter `Subst'\"：\n" + "\n".join(lines) +
            "\n请放置基板（ads_tlines:MSUB，如 MSUB1）并把传输线的 Subst 参数指到它：\n"
            "    msub = put(d, 'ads_tlines:MSUB', 0, 10, name='MSUB1')\n"
            "    setp(mlin, 'Subst', 'MSUB1')"
        )

    shorted = audit.get("shorted_ports") or []
    if shorted:
        ports = ", ".join(f"{p['instance']}({p['net']})" for p in shorted)
        problems.append(
            f"Term 信号脚与地脚接在同一网络：{ports}。"
            "默认朝向下 2 号脚是信号、1 号脚是地；请用 pins() 核对并修正。"
        )

    no_ground = audit.get("port_ground_issues") or []
    if no_ground:
        rows = ", ".join(f"{p['instance']}(地脚网络 {p['net']})" for p in no_ground)
        problems.append(
            f"端口地脚没有接地参考：{rows}。\n"
            "网表里这些端口的第二个节点是只挂在端口上的孤立网络 —— 仿真虽能跑完，"
            "但端口参考开路，S 参数结果不可信。\n"
            "二选一：给每个 Term 的 1 号脚接 ads_rflib:GROUND；"
            "或直接用单脚自带地参考的 ads_simulation:TermG（推荐，无需接 GND）。"
        )

    needs = [c for c in (audit.get("controllers") or []) if _needs_port(c["master"])]
    if needs and not audit.get("ports"):
        problems.append(
            f"设计 {name} 里有 {[c['name'] for c in needs]}，但没有任何端口/终端。"
            f"没有端口 hpeesofsim 无法构建激励。\n"
            f"请放端口并把端口引脚接入电路。接地参考端口首选 TermG（单脚自带地，"
            f"无需接 GND）：\n"
            f"    t1 = put(d, 'ads_simulation:TermG', -5, 0, name='P1')\n"
            f"    setp(t1, 'Num', '1'); setp(t1, 'Z', '50 Ohm')\n"
            f"    connect(d, t1, 1, 输入元件, 对应引脚)\n"
            f"确需独立参考端时用 ads_simulation:Term（双脚：默认朝向下 2=信号、"
            f"1=地，地脚必须接 GROUND）：\n"
            f"    t1 = put(d, 'ads_simulation:Term', -5, 0, name='Term1')\n"
            f"    connect(d, t1, 2, 输入元件, 对应引脚)  # 先用 pins(t1) 核对朝向"
        )
    return problems


def _gate_warnings(audit: dict) -> list:
    """非阻断的疑点（模型应该看到，但不阻止仿真）。"""
    warnings = []
    floating = audit.get("floating_pins") or []
    if floating:
        rows = [f"  - {f['instance']}({f['master']}) 引脚 {f['pin']}"
                for f in floating[:12]]
        more = f"\n  …共 {len(floating)} 个" if len(floating) > 12 else ""
        warnings.append(
            "以下引脚没有连接任何网络（悬空）。若是疏漏请补线；确认是故意悬空可忽略：\n"
            + "\n".join(rows) + more
        )
    return warnings


def library_cell(name: str):
    """'lib:cell:view' -> ('lib', 'cell')；解析不出就整体当 cell。"""
    parts = str(name or "").split(":")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", parts[0] if parts else ""


def _dump_vars(inst) -> dict:
    vars_map = inst.vars
    out = {}
    for key in list(vars_map):
        try:
            out[str(key)] = str(vars_map[key])
        except Exception:
            out[str(key)] = "<unreadable>"
    return out


def get_design_variables(args: dict, ctx=None) -> dict:
    library = args["library"]
    cell = args["cell"]
    view = args.get("view") or "schematic"
    design = _open_design(library, cell, view, write=False)
    try:
        insts = _var_instances(design)
        if not insts:
            return {
                "design": f"{library}:{cell}:{view}",
                "var_instances": {},
                "note": "设计中没有 VAR 变量方程实例",
            }
        result = {}
        for inst in insts:
            result[_inst_name(inst) or "VAR"] = _dump_vars(inst)
        return {"design": f"{library}:{cell}:{view}", "var_instances": result}
    finally:
        _close_design(design)


def set_design_variables(args: dict, ctx=None) -> dict:
    library = args["library"]
    cell = args["cell"]
    view = args.get("view") or "schematic"
    values = args.get("values") or {}
    target = args.get("instance")
    if not values:
        raise RuntimeError("values 为空，没有可修改的变量")

    ws = _require_workspace()
    # 写入前自动留存副本：改坏了至少有退路
    backup = _backup_design(ws, library, cell)

    design = _open_design(library, cell, view, write=True)
    inst_name = ""
    try:
        insts = _var_instances(design)
        if not insts:
            raise RuntimeError(f"设计 {library}:{cell} 中没有 VAR 实例，无法修改变量")
        inst = None
        if target:
            for candidate in insts:
                if _inst_name(candidate) == target:
                    inst = candidate
                    break
            if inst is None:
                raise RuntimeError(f"找不到 VAR 实例 {target}；现有: {[_inst_name(i) for i in insts]}")
        else:
            inst = insts[0]
        inst_name = _inst_name(inst)

        changed = {}
        # 注意：直接赋值即可，实测能正确落盘；不要包 db_uu.Transaction ——
        # 它包裹 add_instance 时 save 后会静默丢内容（见 build_schematic 注释）
        for key, value in values.items():
            old = None
            try:
                old = str(inst.vars[key])
            except Exception:
                pass
            inst.vars[str(key)] = str(value)
            changed[str(key)] = {"old": old, "new": str(value)}

        design.save_design()
    finally:
        # 先关写句柄再重开只读 —— 同一设计的两个句柄不能并用（实测会失效）
        _close_design(design)

    # 保存后只读复核：请求的每个值都必须真的落盘，否则视为本次修改失败
    mismatch = []
    d2 = _open_design(library, cell, view, write=False)
    try:
        on_disk = None
        for cand in _var_instances(d2):
            if _inst_name(cand) == inst_name:
                on_disk = _dump_vars(cand)
                break
        if on_disk is None:
            mismatch.append(f"保存后找不到 VAR 实例 {inst_name}，修改可能没有落盘")
        else:
            for key, value in values.items():
                got = on_disk.get(str(key))
                if got != str(value):
                    mismatch.append(
                        f"变量 {key}: 请求写入 {value!r}，但磁盘上是 {got!r}"
                    )
    finally:
        _close_design(d2)

    if mismatch:
        hint = f"（写入前副本: {backup['path']}）" if backup.get("backed_up") else ""
        raise RuntimeError(
            "保存后只读复核未通过，本次变量修改判定为失败：\n- "
            + "\n- ".join(mismatch)
            + f"\n设计 {library}:{cell}:{view} {hint}"
        )

    return {
        "design": f"{library}:{cell}:{view}",
        "instance": inst_name,
        "changed": changed,
        "verify": {"on_disk_ok": True},
        "backup": backup,
    }


# ---------------------------------------------------------------------------
# 设计写入安全：写入前自动留存副本
# ---------------------------------------------------------------------------

BACKUP_ROOT = "ads_agent_backups"      # <workspace>/ads_agent_backups/<lib>__<cell>__<时间戳>/
KEEP_BACKUPS = 8                       # 每个 cell 保留的最近备份数，超出自动清理
_BACKUP_SIZE_CAP = 200 * 1024 * 1024   # cell 目录超过这个大小就跳过备份（只提示，不硬来）


def _esc_oa(name: str) -> str:
    """OA 的 cell 物理目录名转义：大写字母前加 '%'（AI_lib 实测）。

    Wilkinson_2G4 -> %Wilkinson_2%G4；AI_probe_k -> %A%I_probe_k。
    只在 cell.path 拿不到时用来**猜**路径，且猜到后必须 isdir 验证。
    """
    return "".join(f"%{ch}" if ("A" <= ch <= "Z") else ch for ch in str(name))


def _unescape_oa(name: str) -> str:
    return str(name).replace("%", "")


def _cell_path(ws, library: str, cell: str) -> str:
    """cell 的物理目录。正路是 Cell.path（实测可用），失败再按转义规则猜。"""
    try:
        for lib in (ws.libraries or []):
            if str(lib.name) != str(library):
                continue
            for c in (lib.cells or []):
                if str(c.name) == str(cell):
                    p = getattr(c, "path", None)
                    if p:
                        return str(p)
    except Exception:  # noqa: BLE001
        pass
    try:
        ws_path = str(getattr(ws, "path", "") or "")
    except Exception:  # noqa: BLE001
        ws_path = ""
    if ws_path:
        lib_dir = os.path.join(ws_path, str(library))
        if os.path.isdir(lib_dir):
            direct = os.path.join(lib_dir, _esc_oa(str(cell)))
            if os.path.isdir(direct):
                return direct
            try:                       # 转义规则对不上就反解目录名扫一遍
                for d in os.listdir(lib_dir):
                    full = os.path.join(lib_dir, d)
                    if os.path.isdir(full) and _unescape_oa(d) == str(cell):
                        return full
            except OSError:
                pass
    return ""


def _backup_from_path(cell_path: str, library: str, cell: str, ws_path: str) -> dict:
    """把 cell 目录整体复制到 <ws>/ads_agent_backups/ 下，返回备份报告。

    这是"写入前自动留存副本"的落地：任何一次写打开（含 run_python 里的
    odesign(write=True)、recreate）都会先走这里。备份失败**不阻断普通写入**
    （VAR 改错可以手工改回），但会让 recreate 拒绝执行 —— 推倒重来不能没有
    退路。
    """
    if not cell_path or not os.path.isdir(cell_path):
        return {"backed_up": False, "path": "",
                "note": "找不到该 cell 的磁盘目录（新 cell 或尚未保存过），没有可备份的内容"}

    total = 0
    for root, _dirs, files in os.walk(cell_path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
        if total > _BACKUP_SIZE_CAP:
            break
    if total > _BACKUP_SIZE_CAP:
        return {"backed_up": False, "path": "",
                "note": (f"cell 目录超过 {_BACKUP_SIZE_CAP // (1024 * 1024)}MB，"
                         f"已跳过自动备份（不阻断写入）")}

    parent = os.path.join(str(ws_path), BACKUP_ROOT)
    os.makedirs(parent, exist_ok=True)
    base = f"{_safe_name(library)}__{_safe_name(cell)}__"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = ""
    for i in range(1, 200):
        cand = os.path.join(parent, base + stamp + ("" if i == 1 else f"_{i}"))
        try:
            os.makedirs(cand, exist_ok=False)      # 原子占位，绝不覆盖已有备份
            dest = cand
            break
        except FileExistsError:
            continue
    if not dest:
        return {"backed_up": False, "path": "", "note": "备份目录名连续占用，跳过本次备份"}

    copied = 0
    try:
        for root, _dirs, files in os.walk(cell_path):
            rel = os.path.relpath(root, cell_path)
            target = dest if rel == "." else os.path.join(dest, rel)
            os.makedirs(target, exist_ok=True)
            for f in files:
                shutil.copy2(os.path.join(root, f), os.path.join(target, f))
                copied += 1
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)    # 半份备份比没有更误导
        return {"backed_up": False, "path": "", "note": f"备份复制失败: {e}"}

    _prune_backups(parent, base)
    return {"backed_up": True, "path": dest, "files": copied, "note": ""}


def _prune_backups(parent: str, base_prefix: str) -> None:
    """只保留最近 KEEP_BACKUPS 份（目录名带时间戳，可直接排序）。"""
    try:
        entries = sorted(d for d in os.listdir(parent) if d.startswith(base_prefix))
        for d in entries[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(parent, d), ignore_errors=True)
    except OSError:
        pass


def _backup_design(ws, library: str, cell: str) -> dict:
    """备份一个 cell（任何异常都折算成 backed_up=False + note，绝不打断写入）。"""
    try:
        return _backup_from_path(_cell_path(ws, library, cell),
                                 library, cell, str(getattr(ws, "path", "")))
    except Exception as e:  # noqa: BLE001
        return {"backed_up": False, "path": "",
                "note": f"备份阶段异常: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 固定操作：建图 + 连线检查 + 只读复核（替代逐条 run_python 探索式建图）
# ---------------------------------------------------------------------------

def _xy_point(point):
    """点 -> (x, y)。PointF 的暴露方式在不同构建里不统一，这里都试一遍。"""
    if hasattr(point, "x") and hasattr(point, "y"):
        return (float(point.x), float(point.y))
    try:
        return (float(point[0]), float(point[1]))
    except Exception:  # noqa: BLE001
        pass
    for attr in ("get_x", "X"):
        if hasattr(point, attr):
            return (float(getattr(point, attr)), float(point.y))
    raise TypeError(f"无法从 {type(point).__name__} 取坐标（请用元组 (x, y)）")


def _set_param(inst, name, value):
    """设置实例参数。inst.parameters 是 **list[Param]**（不是 dict！）。

    字符串型参数（Subst、SweepVar…）的值**必须带双引号**，否则网表里出现
    Subst=MSUB1，hpeesofsim 会报
    "Expected a substrate model for parameter `Subst'"。
    按"原值是引号包着的"自动补引号。
    """
    value = str(value)
    for p in list(inst.parameters):
        if str(getattr(p, "name", "")) == str(name):
            try:
                old = str(getattr(p, "value", "") or "")
            except Exception:  # noqa: BLE001
                old = ""
            if old.startswith('"') and old.endswith('"') and len(old) >= 2:
                if not (value.startswith('"') and value.endswith('"')):
                    value = f'"{value}"'
            p.value = value
            return f"{getattr(inst, 'inst_name', '')}.{name} = {value}"
    raise KeyError(
        f"{getattr(inst, 'inst_name', inst)!r} 上没有参数 {name!r}；可用: "
        f"{[str(getattr(p, 'name', '')) for p in list(inst.parameters)][:30]}"
    )


def _find_pin_on(inst, label):
    """按编号（1、2）或名字（'P1'）找实例上的引脚。"""
    for p in list(inst.inst_pins):
        lbl = _pin_label(p)
        if str(lbl) == str(label) or str(getattr(p, "master_pin", "")) == str(label):
            return p
    avail = [_pin_label(p) for p in list(inst.inst_pins)]
    raise KeyError(f"实例 {_inst_name(inst)!r} 上找不到引脚 {label!r}；可用: {avail}")


def _bind_pins(design, pa, pb):
    """把两个引脚显式绑到同一网络。

    **几何重合不等于电气连通**（2026-09-24 实测）：add_wire 端点精确落在
    引脚上，网表里两个引脚仍在各自独立的 N__x 节点上 —— ADS 不会为 API
    画的线自动做连通提取。pin.net 可赋值，显式绑定后网表才真正共享节点。
    """
    old_net = getattr(pa, "net", None)
    net = getattr(pb, "net", None) or old_net
    if net is None:
        try:
            net = design.add_scalar_net()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"创建网络失败: {type(e).__name__}: {e}")
    try:
        # 两端已有各自的网络时，要把旧网络上的全部引脚一起迁移。
        # 只修改 pa/pb 会拆断旧网络中先前连好的其它元件。
        if old_net is not None and _net_label(old_net) != _net_label(net):
            old_name = _net_label(old_net)
            for inst in list(design.instances):
                for pin in list(inst.inst_pins):
                    current = getattr(pin, "net", None)
                    if current is old_net or (old_name and _net_label(current) == old_name):
                        pin.net = net
        pa.net = net
        pb.net = net
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"绑定引脚网络失败: {type(e).__name__}: {e}")


def _ortho_points(x1, y1, x2, y2):
    """两点间曼哈顿走线：共轴走直线，否则按主导方向加一个 90° 拐弯。

    ADS 的 add_wire 端点不对齐就画大斜线（Wilkinson_5G9 上 R1 的连线
    斜穿全图，2026-09-24 用户截图点名）；显式给拐点保证横平竖直。
    横向为主时先竖直离开源引脚、再水平进入目标引脚（目标引脚几乎都是
    水平朝向，这样最后一助手直进引脚）；纵向为主反之。
    """
    if abs(x2 - x1) < 1e-9 or abs(y2 - y1) < 1e-9:
        return [(x1, y1), (x2, y2)]
    if abs(x2 - x1) >= abs(y2 - y1):
        return [(x1, y1), (x1, y2), (x2, y2)]
    return [(x1, y1), (x2, y1), (x2, y2)]


def _symbol_boxes(design):
    """只取符号外框；缺少 bbox 的离线替身不参与避障。"""
    boxes = []
    for inst in list(design.instances):
        try:
            box = inst.bbox
            lo, hi = box.lower_left, box.upper_right
            rect = (float(lo.x), float(lo.y), float(hi.x), float(hi.y))
            if rect[2] > rect[0] and rect[3] > rect[1]:
                boxes.append((_inst_name(inst), rect))
        except Exception:  # noqa: BLE001
            continue
    return boxes


def _segment_hits_box(a, b, box, strict=False):
    """边界可触及（引脚常在边界）；strict=True 时只判内部穿越。

    strict 用于"引脚所属符号"：从引脚出发/进入的导线允许贴自己符号的
    边界（实测 MLIN bbox 宽 1.0、引脚就在 bbox 边缘上，垂直 T 下拉必然
    擦边 —— 误拒会让任何 T 结都无法布线），但穿过内部仍算违规。
    例外：段的一端在符号体**内部**时整段豁免 —— BJT 这类符号的引脚
    snap 点画在符号 bbox 内部（CB_FM_Amp 实测 BFR106 c 脚 (0.5,0.5) 而
    bbox 顶 0.65），引脚伸出段是物理必须,不豁免任何接入线都被拒。
    """
    left, bottom, right, top = box
    eps = 1e-8
    # 贴边骑行（2026-09-30 R13 修正判定范围）：段与**非引脚符号**的
    # 边线共线、且沿边线滑行长度 > 0.25 → 违规（渲染上导线与无关符号
    # 轮廓重叠）。引脚所属符号（strict=True）不判贴边 —— 从边界引脚
    # 出发的直线沿符号边缘滑行是 T 下拉/正交直通的必然形态（五路
    # 直通臂 x=出脚列 直线被旧判定误杀实测）。
    if not strict:
        if abs(a[0] - b[0]) < eps and                 (abs(a[0] - left) < 1e-6 or abs(a[0] - right) < 1e-6):
            if min(max(a[1], b[1]), top) -                     max(min(a[1], b[1]), bottom) > 0.25:
                return True
        if abs(a[1] - b[1]) < eps and                 (abs(a[1] - bottom) < 1e-6 or abs(a[1] - top) < 1e-6):
            if min(max(a[0], b[0]), right) -                     max(min(a[0], b[0]), left) > 0.25:
                return True
    if strict:
        for px, py in (a, b):
            if left - eps <= px <= right + eps and                     bottom - eps <= py <= top + eps and                     not (abs(px - left) < eps or abs(px - right) < eps
                         or abs(py - bottom) < eps or abs(py - top) < eps):
                return False   # 端点在体内:引脚伸出段
    if abs(a[1] - b[1]) < eps:                      # 水平段：看 y 在不在(严格)内部
        inside = (bottom + eps < a[1] < top - eps) if strict \
            else (bottom - eps <= a[1] <= top + eps)
        return inside and \
            max(min(a[0], b[0]), left) < min(max(a[0], b[0]), right) - eps
    inside = (left + eps < a[0] < right - eps) if strict \
        else (left - eps <= a[0] <= right + eps)
    return inside and \
        max(min(a[1], b[1]), bottom) < min(max(a[1], b[1]), top) - eps


def _segment_hits_route(a, b, old_a, old_b, endpoints, old_endpoints):
    """不同连接的线段不能交叉或重合；共同端点可以接成 T 形。"""
    eps = 1e-8
    horizontal = abs(a[1] - b[1]) < eps
    old_horizontal = abs(old_a[1] - old_b[1]) < eps
    if horizontal == old_horizontal:
        axis = 1 if horizontal else 0
        along = 0 if horizontal else 1
        if abs(a[axis] - old_a[axis]) > eps:
            return False
        lo = max(min(a[along], b[along]), min(old_a[along], old_b[along]))
        hi = min(max(a[along], b[along]), max(old_a[along], old_b[along]))
        if hi - lo > eps:
            return True
        if hi < lo - eps:
            return False
        point = (lo, a[1]) if horizontal else (a[0], lo)
    else:
        h1, h2 = (a, b) if horizontal else (old_a, old_b)
        v1, v2 = (old_a, old_b) if horizontal else (a, b)
        point = (v1[0], h1[1])
        if not (min(h1[0], h2[0]) - eps <= point[0] <= max(h1[0], h2[0]) + eps
                and min(v1[1], v2[1]) - eps <= point[1] <= max(v1[1], v2[1]) + eps):
            return False
    return not (point in endpoints and point in old_endpoints)


def _box_owning_pin(pt, boxes):
    """引脚点压在哪个符号边界上（用于该符号的严格内部豁免）。

    多个符号共享同一引脚点时（对称一分三的直通臂引脚与分叉引脚同点），
    返回**全部**归属符号 —— 只回第一个会把其余符号留在非豁免表里，
    出线擦边被误拒，直通臂的任何走线都找不到路。
    """
    eps = 1e-6
    x, y = pt
    owners = []
    for name, (left, bottom, right, top) in boxes:
        on_edge = ((abs(x - left) <= eps or abs(x - right) <= eps)
                   and bottom - eps <= y <= top + eps) or \
                  ((abs(y - bottom) <= eps or abs(y - top) <= eps)
                   and left - eps <= x <= right + eps)
        if on_edge:
            owners.append(name)
    return owners[0] if owners else None


def _box_owning_pins(pt, boxes):
    """同上，返回全部归属符号名列表。"""
    eps = 1e-6
    x, y = pt
    owners = []
    for name, (left, bottom, right, top) in boxes:
        on_edge = ((abs(x - left) <= eps or abs(x - right) <= eps)
                   and bottom - eps <= y <= top + eps) or \
                  ((abs(y - bottom) <= eps or abs(y - top) <= eps)
                   and left - eps <= x <= right + eps)
        if on_edge:
            owners.append(name)
    return owners


def _route_clear(points, boxes, routes, endpoints=None, pin_boxes=(), net=None,
                 tap_pins=()):
    """正交走线清空检查。routes 元素是 (折线, net 标签) 或裸折线(视作
    无 net);net 相同的两条线表达同一电气节点 —— 重叠是共享走廊段、
    交叠是 T 形分支,全部豁免(2026-09-29 用户规则 2:先按网络规划共享
    线段,不再各拉平行线)。

    tap_pins（2026-09-30 R20）：与本连线同电气节点的全部引脚点 ——
    线段恰好穿过其中某个引脚 = 合法 T 搭（T 落在已验证节点上），
    不算穿体。真实电容/电阻符号的顶板常高出引脚行 ~0.06 格，同网
    直连被误判穿体后 _route_around 各自绕行，画出重复竖线与平行
    车道（CE_FM_Amp_AGENT 输出行实测）。"""
    ends = endpoints if endpoints is not None else {points[0], points[-1]}
    exempt = set(pin_boxes)

    def _through_tap_pin(a, b):
        for (px, py) in tap_pins:
            on = (abs(a[0] - b[0]) < 1e-9 and abs(px - a[0]) < 1e-6
                  and min(a[1], b[1]) - 1e-6 <= py <= max(a[1], b[1]) + 1e-6)                 or (abs(a[1] - b[1]) < 1e-9 and abs(py - a[1]) < 1e-6
                    and min(a[0], b[0]) - 1e-6 <= px <= max(a[0], b[0]) + 1e-6)
            if on:
                return True
        return False

    for a, b in zip(points, points[1:]):
        if a == b:
            continue
        if abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8:
            return False
        for name, box in boxes:
            if _segment_hits_box(a, b, box, strict=name in exempt):
                if _through_tap_pin(a, b):
                    continue
                return False
        for old_route in routes:
            if isinstance(old_route, tuple):
                old_points, old_net = old_route
            else:
                old_points, old_net = old_route, None
            if net is not None and old_net == net:
                continue
            old_ends = {old_points[0], old_points[-1]}
            if any(_segment_hits_route(a, b, c, d, ends, old_ends)
                   for c, d in zip(old_points, old_points[1:])):
                return False
    return True


def _route_around(start, end, boxes, routes, pin_boxes=(), net=None,
                  tap_pins=()):
    """在符号外侧的正交网格上找最短清晰路径，优先少拐弯。

    pin_boxes：连线两端引脚所属的符号名 —— 这些符号对该连线的导线用
    "只判内部"的宽松规则（引脚压在符号边界上，出线擦边不可避免）。
    """
    import heapq

    direct = _ortho_points(*start, *end)
    if _route_clear(direct, boxes, routes, pin_boxes=pin_boxes, net=net,
                    tap_pins=tap_pins):
        return direct
    other = [start, (end[0], start[1]), end] if direct[1][0] == start[0] else \
        [start, (start[0], end[1]), end]
    if _route_clear(other, boxes, routes, pin_boxes=pin_boxes, net=net,
                    tap_pins=tap_pins):
        return other

    margin = 0.25
    xs = {start[0], end[0]}
    ys = {start[1], end[1]}
    for _, (left, bottom, right, top) in boxes:
        xs.update((left - margin, right + margin))
        ys.update((bottom - margin, top + margin))
    for old_route in routes:
        points = old_route[0] if isinstance(old_route, tuple) else old_route
        for x, y in points:
            xs.update((x - margin, x, x + margin))
            ys.update((y - margin, y, y + margin))
    xs, ys = sorted(xs), sorted(ys)
    grid = {(x, y) for x in xs for y in ys
            if not any(left < x < right and bottom < y < top
                       for _, (left, bottom, right, top) in boxes)}
    queue = [(0.0, 0, start, None, (start,))]
    best = {}
    while queue:
        cost, bends, point, direction, path = heapq.heappop(queue)
        state = (point, direction)
        if cost > best.get(state, float("inf")) + 1e-8:
            continue
        if point == end:
            turns = [path[0]]
            for a, b, c in zip(path, path[1:], path[2:]):
                if (a[0] == b[0]) != (b[0] == c[0]):
                    turns.append(b)
            return turns + [end]
        x, y = point
        neighbors = []
        ix, iy = xs.index(x), ys.index(y)
        for nx in (ix - 1, ix + 1):
            if 0 <= nx < len(xs):
                neighbors.append(((xs[nx], y), "h"))
        for ny in (iy - 1, iy + 1):
            if 0 <= ny < len(ys):
                neighbors.append(((x, ys[ny]), "v"))
        for neighbor, new_dir in neighbors:
            if neighbor not in grid or not _route_clear((point, neighbor), boxes, routes,
                                                        {start, end},
                                                        pin_boxes=pin_boxes,
                                                        net=net):
                continue
            turn = direction is not None and direction != new_dir
            next_cost = cost + abs(neighbor[0] - x) + abs(neighbor[1] - y) + (0.2 if turn else 0)
            next_state = (neighbor, new_dir)
            if next_cost + 1e-8 < best.get(next_state, float("inf")):
                best[next_state] = next_cost
                heapq.heappush(queue, (next_cost, bends + int(turn), neighbor,
                                       new_dir, path + (neighbor,)))
    raise RuntimeError(f"无法找到不穿元件、不交叉的正交走线：{start} -> {end}")


def _wire_connection(design, conn, routes=None) -> None:
    """按 {"a": [实例名, 引脚], "b": [实例名, 引脚]} 连接两个引脚。

    画线（给人看、给 GUI 看）+ 显式网络绑定（给网表用），缺一不可。
    端点不共轴时自动加 90° 拐点走曼哈顿线；conn 给 waypoints 时按给定
    折点走（自动布局之外的手工微调用）；conn["_net"] 是走线计划标注的
    电气网络 —— 同 net 的线段重叠=共享走廊段，避障时豁免。
    """
    if not isinstance(conn, dict) or not conn.get("a") or not conn.get("b"):
        raise RuntimeError(
            f"connections 每项需要 {{\"a\": [实例, 引脚], \"b\": [实例, 引脚]}}: {conn!r}")

    def inst(name):
        for i in list(design.instances):
            if _inst_name(i) == str(name):
                return i
        raise KeyError(f"找不到实例 {name!r}（它必须在本次 instances 里，或已存在于设计中）")

    pa = _find_pin_on(inst(conn["a"][0]), conn["a"][1])
    pb = _find_pin_on(inst(conn["b"][0]), conn["b"][1])
    x1, y1 = _xy_point(pa.snap_point)
    x2, y2 = _xy_point(pb.snap_point)
    if conn.get("_bind_only"):
        # 走线计划:该连接的导线已由同 net 前序连接画出(共享走廊),
        # 这里只做网络绑定 —— 重复画会构成"完全重合线段"(几何门禁)
        _bind_pins(design, pa, pb)
        return [(x1, y1), (x2, y2)]
    # 同网引脚点（R20）：T 搭合法性判定用 —— 本连线的电气节点上
    # 全部引脚（含自身两端）都是已验证节点；真实电容/电阻顶板高出
    # 引脚行 ~0.06 格，同网直连无此豁免会被误判穿体后各自绕行
    _my_net = _net_label(getattr(pa, "net", None))
    tap_pins = []
    if _my_net:
        for _i2 in list(design.instances):
            for _p2 in list(getattr(_i2, "inst_pins", []) or []):
                if _net_label(getattr(_p2, "net", None)) == _my_net:
                    _s2 = getattr(_p2, "snap_point", None)
                    if _s2 is not None:
                        _xy2 = _xy_point(_s2)
                        tap_pins.append((_xy2[0], _xy2[1]))
    boxes = _symbol_boxes(design)
    pin_boxes = []
    for pt in ((x1, y1), (x2, y2)):
        pin_boxes.extend(_box_owning_pins(pt, boxes))
    pin_boxes = list(dict.fromkeys(pin_boxes))
    net = conn.get("_net")
    existing = routes if routes is not None else []
    if conn.get("waypoints"):
        pts = [(x1, y1)] + [(float(w[0]), float(w[1]))
                            for w in conn["waypoints"]] + [(x2, y2)]
        if not _route_clear(pts, boxes, existing, pin_boxes=pin_boxes,
                            net=net, tap_pins=tap_pins):
            raise RuntimeError(f"指定折点使导线穿过元件或与已有导线交叉/重叠：{conn}")
    else:
        pts = _route_around((x1, y1), (x2, y2), boxes, existing,
                            pin_boxes=pin_boxes, net=net, tap_pins=tap_pins)
    # ADS 对多折点 wire 的显示并不稳定；逐段建立水平/垂直线，避免 GUI
    # 把首尾引脚直接画成一条跨图斜线。网络仍由下面的显式绑定保证。
    # _skip_segs:走线计划判定与同 net 前序连接重合的段(下标对 pts 的
    # 段序列)——跳过绘制,只保留网络绑定(重复段消除,2026-09-29)。
    skip = set(conn.get("_skip_segs") or [])
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        if abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8:
            raise RuntimeError(f"走线包含斜段: {a} -> {b}")
        if a != b and k not in skip:
            design.add_wire([a, b])
    _bind_pins(design, pa, pb)
    if routes is not None:
        routes.append((pts, net))


# ---------------------------------------------------------------------------
# 导线几何硬门禁：读取设计里的真实导线，逐段复核（build_schematic 保存后
# 复核与 run_python 手工路径 save() 前置检查共用）。
#
# 背景证据（Wilkinson_1G7_ML，2026-09-28 用户截图点名 R1 上下斜线）：
# build_schematic 的连线全部经 _wire_connection 正交门禁，但 run_python 的
# 手工 connect()/add_wire 直接把两个不共轴的引脚连成一条斜线，保存后的
# 设计里躺着 (7,-6)->(8.5,-2) 和 (7,-1)->(8.5,0) 两段斜线。门禁必须覆盖
# "保存前后都能读到的真实几何"，而不是只信建图前的规划坐标。
# ---------------------------------------------------------------------------

_GEOM_EPS = 1e-6


def _shape_polyline(shape):
    """shape 的折线顶点。返回 (顶点列表, 是否精确)。

    Outline.points 给真实顶点（含多折点）；拿不到时退化 bbox 只能表达
    "直线段"（共线 bbox），非退化 bbox 的形状几何未知（可能是正交折线、
    矩形或斜线），按精确=False 交给调用方如实标注，绝不臆测顶点。
    """
    try:
        ol = shape.get_outline()
        raw = getattr(ol, "points", None)
        if raw is None:
            raw = ol
        pts = [_xy_point(p) for p in list(raw)]
        if len(pts) >= 2:
            return pts, True
    except Exception:  # noqa: BLE001 — outline 读不出就退 bbox
        pass
    try:
        b = shape.bbox
        x1, y1 = float(b.lower_left.x), float(b.lower_left.y)
        x2, y2 = float(b.upper_right.x), float(b.upper_right.y)
    except Exception:  # noqa: BLE001
        return [], False
    if abs(x1 - x2) < _GEOM_EPS or abs(y1 - y2) < _GEOM_EPS:
        return [(x1, y1), (x2, y2)], True
    return [(x1, y1), (x2, y2)], False


def _design_wire_routes(design):
    """设计里已有导线的折线列表（避障用：追加布线不得压过旧线）。"""
    routes = []
    try:
        shapes = list(design.shapes)
    except Exception:  # noqa: BLE001
        return routes
    for s in shapes:
        pts, exact = _shape_polyline(s)
        if exact and len(pts) >= 2:
            routes.append((pts, None))
    return routes


def _seg_violation(a, b, c, d):
    """两段的关系。返回 None（无接触/仅端点相接）或 (kind, point)。

    kind: "overlap" 共线重叠；"t" 一段端点搭在另一段中部；"cross" 内部交叉。
    端点对端点相接（拐弯、链式相接）不算违规 —— 由调用方按网络归属分级。
    """
    eps = _GEOM_EPS
    h1, h2 = abs(a[1] - b[1]) < eps, abs(c[1] - d[1]) < eps
    if h1 and h2:
        if abs(a[1] - c[1]) > eps:
            return None
        lo = max(min(a[0], b[0]), min(c[0], d[0]))
        hi = min(max(a[0], b[0]), max(c[0], d[0]))
        return ("overlap", None) if hi - lo > eps else None
    if not h1 and not h2:
        if abs(a[0] - c[0]) > eps:
            return None
        lo = max(min(a[1], b[1]), min(c[1], d[1]))
        hi = min(max(a[1], b[1]), max(c[1], d[1]))
        return ("overlap", None) if hi - lo > eps else None
    ha, hb, va, vb = (a, b, c, d) if h1 else (c, d, a, b)
    pt = (va[0], ha[1])
    on_h = (min(ha[0], hb[0]) - eps <= pt[0] <= max(ha[0], hb[0]) + eps
            and min(ha[1], hb[1]) - eps <= pt[1] <= max(ha[1], hb[1]) + eps)
    on_v = (min(va[0], vb[0]) - eps <= pt[0] <= max(va[0], vb[0]) + eps
            and min(va[1], vb[1]) - eps <= pt[1] <= max(va[1], vb[1]) + eps)
    if not (on_h and on_v):
        return None
    ends_h = {ha, hb}
    ends_v = {va, vb}
    pt_r = (round(pt[0], 6), round(pt[1], 6))
    is_end_h = any((round(p[0], 6), round(p[1], 6)) == pt_r for p in ends_h)
    is_end_v = any((round(p[0], 6), round(p[1], 6)) == pt_r for p in ends_v)
    if is_end_h and is_end_v:
        return None                      # 端点对端点：链式相接/拐弯
    if is_end_h or is_end_v:
        return ("t", pt)                 # 一段端点搭在另一段中部
    return ("cross", pt)


def _annotation_issues(design, by_name: dict) -> list:
    """保存后标注归属复核（bbox_annotation_only 实测框，2026-09-30）。

    与 7.5 的规划期避让不同，这里以**磁盘上真实标注框**为准：
    * 文字框到所属符号盒的间隙 >1.2 格 → 归属不可判读（悬空）；
    * 两个实例的文字框互压（pad 0.02）→ 读图歧义。
    返回人类可读 issue 列表；空 = 全部文字贴符号且互不重叠。
    只报告不拦截 —— 电气与几何门禁另有 problems；文字质量由
    auto_layout 的归属规则在规划期保证，这里是渲染级兜底。
    """
    boxes, syms = {}, {}
    for inst in list(design.instances):
        name = _inst_name(inst)
        try:
            bb = inst.bbox
            syms[name] = (float(bb.lower_left.x), float(bb.lower_left.y),
                          float(bb.upper_right.x), float(bb.upper_right.y))
        except Exception:  # noqa: BLE001
            continue
        try:
            if not getattr(inst, "has_ads_annotation", False):
                continue
            ab = inst.bbox_annotation_only
            boxes[name] = (float(ab.lower_left.x), float(ab.lower_left.y),
                           float(ab.upper_right.x), float(ab.upper_right.y))
        except Exception:  # noqa: BLE001
            continue

    def _gap(a, b):
        dx = max(b[0] - a[2], a[0] - b[2], 0.0)
        dy = max(b[1] - a[3], a[1] - b[3], 0.0)
        return (dx * dx + dy * dy) ** 0.5

    issues = []
    for name, ab in sorted(boxes.items()):
        if name not in syms:
            continue
        g = _gap(ab, syms[name])
        if g > 1.2:
            issues.append(f"{name} 的标注文字距符号 {g:.2f} 格（>1.2），"
                          "读图无法判定归属")
    names = sorted(boxes)
    for i, na in enumerate(names):
        for nb in names[i + 1:]:
            a, b = boxes[na], boxes[nb]
            if not (a[2] < b[0] - 0.02 or b[2] < a[0] - 0.02
                    or a[3] < b[1] - 0.02 or b[3] < a[1] - 0.02):
                issues.append(f"{na} 与 {nb} 的标注文字互压")
    return issues


def _geometry_report(design) -> dict:
    """读取设计里的真实导线几何并逐段复核，返回可序列化报告。

    检查项（全部基于读取到的几何，不是建图前的规划坐标）：
      problems  —— 斜段；穿越符号内部；无引脚的 T 形搭接；异网交叉/共线重叠
      warnings  —— 同网交叉/重叠（电学无害、图面可疑）；无引脚的端点链
      unverified—— 顶点读不出的 shape（如实标注，不臆测）

    网络归属：导线端点压在引脚上取引脚网络；端点互相重合的导线经并查集
    共享网络。归属不明的交叉记入 warnings（"未验证电学影响"）。
    """
    report = {"problems": [], "warnings": [], "unverified": [], "n_segments": 0}
    try:
        shapes = list(design.shapes)
    except Exception as e:  # noqa: BLE001
        report["unverified"].append(f"无法枚举导线图形（{type(e).__name__}: {e}）——"
                                    "斜线/交叉未验证")
        return report

    segments = []
    for s in shapes:
        pts, exact = _shape_polyline(s)
        if not pts:
            continue
        if not exact:
            try:
                b = s.bbox
                report["unverified"].append(
                    f"一个图形 (bbox {b.lower_left.x:.3g},{b.lower_left.y:.3g} ~ "
                    f"{b.upper_right.x:.3g},{b.upper_right.y:.3g}) 读不出折线顶点，"
                    "其是否含斜段/交叉未验证")
            except Exception:  # noqa: BLE001
                report["unverified"].append("一个图形读不出几何，未验证")
            continue
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) < _GEOM_EPS and abs(a[1] - b[1]) < _GEOM_EPS:
                continue
            segments.append((a, b))
    report["n_segments"] = len(segments)

    # 1) 斜段：硬错误（用户要求：每一段导线严格沿 X 或 Y）
    for i, (a, b) in enumerate(segments):
        if abs(a[0] - b[0]) > _GEOM_EPS and abs(a[1] - b[1]) > _GEOM_EPS:
            report["problems"].append(
                f"第 {i + 1} 段导线是斜线: ({a[0]:.4g},{a[1]:.4g}) -> "
                f"({b[0]:.4g},{b[1]:.4g})")

    # 2) 引脚点表（网络归属 + 结点判定）
    pin_pts = {}
    for inst in list(design.instances):
        try:
            plist = list(inst.inst_pins)
        except Exception:  # noqa: BLE001
            continue
        for p in plist:
            sp = getattr(p, "snap_point", None)
            if sp is None:
                continue
            try:
                xy = _xy_point(sp)
            except Exception:  # noqa: BLE001
                continue
            key = (round(xy[0], 6), round(xy[1], 6))
            pin_pts.setdefault(key, []).append(
                (_inst_name(inst), _inst_master(inst), _pin_label(p),
                 _net_label(getattr(p, "net", None))))

    # 3) 并查集：端点重合的导线同分量；分量网络 = 分量内任一引脚的网络
    n = len(segments)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    by_pt = {}
    for i, (a, b) in enumerate(segments):
        for pt in (a, b):
            by_pt.setdefault((round(pt[0], 6), round(pt[1], 6)), []).append(i)
    for idxs in by_pt.values():
        for j in idxs[1:]:
            union(idxs[0], j)
    comp_net = {}
    for pt, idxs in by_pt.items():
        for entry in pin_pts.get(pt, ()):
            if entry[3]:
                for i in idxs:
                    comp_net.setdefault(find(i), entry[3])

    # 4) 穿符号：段落在某实例符号 bbox 内部（边界可触及 = 引脚在边上）。
    #    引脚所属的符号用"只判内部"的宽松规则 —— 从引脚出线擦自己的
    #    符号边缘不可避免（实测 MLIN bbox 宽 1.0，引脚就在 bbox 边缘上）。
    boxes = _symbol_boxes(design)

    def _seg_exempt(a, b):
        """两端点若是某实例的引脚且压在该实例符号边界上 → 该符号宽松判定。"""
        exempt = set()
        for pt in (a, b):
            key = (round(pt[0], 6), round(pt[1], 6))
            for entry in pin_pts.get(key, ()):
                for nm, bx in boxes:
                    if nm == entry[0] and _box_owning_pin(pt, [(nm, bx)]) == nm:
                        exempt.add(nm)
        return exempt

    # 各符号的引脚点（R20）：线段穿过某符号自身引脚 = T 搭在已验证
    # 节点上，不算穿体 —— 与走线层 tap 豁免同口径
    sym_pin_pts = {}
    for inst in list(design.instances):
        try:
            plist = list(inst.inst_pins)
        except Exception:  # noqa: BLE001
            continue
        nm = _inst_name(inst)
        for p in plist:
            sp = getattr(p, "snap_point", None)
            if sp is None:
                continue
            try:
                xy = _xy_point(sp)
            except Exception:  # noqa: BLE001
                continue
            sym_pin_pts.setdefault(nm, []).append((round(xy[0], 6),
                                                   round(xy[1], 6)))

    def _through_sym_pin(a, b, nm, box):
        """T 搭豁免（R20）：线段穿过符号**自身引脚**、且与符号内部的
        相交深度 <= 0.5 格 —— 引脚旁薄条擦过（真实 C 顶板高出引脚行
        ~0.06、C/R 竖放体宽 0.25~0.5），是"长线 + 引脚 T 搭"的合法
        形态；深穿（横穿整个符号体，如测试里 0.875 宽的 MLa）仍判
        穿体违规。"""
        for (px, py) in sym_pin_pts.get(nm, ()):
            on = (abs(a[0] - b[0]) < 1e-6 and abs(px - a[0]) < 1e-6
                  and min(a[1], b[1]) - 1e-6 <= py <= max(a[1], b[1]) + 1e-6)                 or (abs(a[1] - b[1]) < 1e-6 and abs(py - a[1]) < 1e-6
                    and min(a[0], b[0]) - 1e-6 <= px <= max(a[0], b[0]) + 1e-6)
            if not on:
                continue
            left, bottom, right, top = box
            if abs(a[0] - b[0]) < 1e-6:      # 竖线:内部相交长度 = y 向
                depth = min(top, max(a[1], b[1])) -                     max(bottom, min(a[1], b[1]))
            else:                            # 横线:内部相交长度 = x 向
                depth = min(right, max(a[0], b[0])) -                     max(left, min(a[0], b[0]))
            if depth <= 0.5:
                return True
        return False

    for i, (a, b) in enumerate(segments):
        if abs(a[0] - b[0]) > _GEOM_EPS and abs(a[1] - b[1]) > _GEOM_EPS:
            continue                       # 斜段已在上面单独报过
        exempt = _seg_exempt(a, b)
        for name, box in boxes:
            if _segment_hits_box(a, b, box, strict=name in exempt):
                if _through_sym_pin(a, b, name, box):
                    continue
                report["problems"].append(
                    f"导线 ({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) "
                    f"穿过元件 {name} 的符号内部")

    # 4.5) 导线穿文字：读真实标注 bbox（bbox_annotation_only，设计坐标）。
    #      文字避让由 auto_layout 的 7.5 步在规划级完成；这里是保存后
    #      用**真实标注位置**兜底复核——手工 move_annotation 挪到线上、
    #      或规划与实际路由分歧的绕行穿字，都逃不过这一层。警告级：
    #      不阻断保存，但在每次 build 结果里可见。
    for inst in list(design.instances):
        try:
            if not getattr(inst, "has_ads_annotation", False):
                continue
            ab = getattr(inst, "bbox_annotation_only", None)
            if ab is None:
                continue
            ax1, ay1 = float(ab.lower_left.x), float(ab.lower_left.y)
            ax2, ay2 = float(ab.upper_right.x), float(ab.upper_right.y)
            if ax2 - ax1 <= 1e-6 or ay2 - ay1 <= 1e-6:
                continue
            pad = 0.02
            box = (ax1 + pad, ay1 + pad, ax2 - pad, ay2 - pad)
            name = _inst_name(inst)
            for i, (a, b) in enumerate(segments):
                if _segment_hits_box(a, b, box, strict=False):
                    report["warnings"].append(
                        f"导线 ({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) "
                        f"穿过 {name} 的文字区（bbox {box[0]:.3g},{box[1]:.3g}"
                        f"~{box[2]:.3g},{box[3]:.3g}）")
        except Exception:  # noqa: BLE001 — 标注读不出就不查，不臆测
            continue

    # 5) 导线两两关系：T 形搭接 / 内部交叉 / 共线重叠，按网络归属分级
    for i in range(n):
        for j in range(i + 1, n):
            v = _seg_violation(segments[i][0], segments[i][1],
                               segments[j][0], segments[j][1])
            if v is None:
                continue
            kind, pt = v
            ni, nj = comp_net.get(find(i)), comp_net.get(find(j))
            same = ni is not None and ni == nj
            at_pin = pt is not None and (round(pt[0], 6), round(pt[1], 6)) in pin_pts
            where = (f"于 ({pt[0]:.4g},{pt[1]:.4g})" if pt is not None else "")
            if kind == "t":
                # 端点搭在中部：搭在引脚上 = 明确结点（引脚网络由 _bind_pins
                # 保证）；同网络的 T 形搭接 = 清晰 T 分支（2026-09-29 用户
                # 规则 2 的目标形态,两线引脚已被显式绑到同一标量网络,
                # 电气连通有保证）→ 警告级；无引脚且网络不明/异网才是问题
                if at_pin:
                    continue
                if same:
                    report["warnings"].append(
                        f"同一网络的导线 T 形分支{where}（电气已显式绑定,图面正常）")
                elif ni is None or nj is None:
                    report["problems"].append(
                        f"导线端点搭在另一根导线中部{where}，且该点没有元件引脚 —— "
                        "网络归属不明；请改为引脚到引脚连线")
                else:
                    report["problems"].append(
                        f"网络 {ni} 与 {nj} 的导线 T 形搭接{where}（短路风险）")
            elif kind == "cross":
                if same:
                    report["warnings"].append(
                        f"同一网络的两根导线交叉{where}（电学无影响，图面可疑）")
                elif ni is None or nj is None:
                    report["warnings"].append(
                        f"两根导线交叉{where}且网络归属不明 —— 电学影响未验证")
                else:
                    report["problems"].append(
                        f"网络 {ni} 与 {nj} 的导线交叉{where}（短路）")
            else:  # overlap
                if same:
                    report["warnings"].append(
                        f"同一网络的两根导线共线重叠{where}")
                elif ni is None or nj is None:
                    report["warnings"].append(
                        f"两根导线共线重叠{where}且网络归属不明 —— 未验证")
                else:
                    report["problems"].append(
                        f"网络 {ni} 与 {nj} 的导线共线重叠{where}")

    # 6) 图面质量指标（2026-09-29 用户验收清单）：
    #    重复重合线段 / 拐弯数（按连通折线合并）/ 平行出线 / 同轴折返 /
    #    绕行系数。拐弯与绕行基于"端点重合并查集分量"拼回完整折线再
    #    统计 —— ADS 把一条折线存成多根直段，逐段数会误报为零。
    dup = 0
    seen_seg = {}
    for i, (a, b) in enumerate(segments):
        key = tuple(sorted([(round(a[0], 6), round(a[1], 6)),
                            (round(b[0], 6), round(b[1], 6))]))
        if key in seen_seg:
            dup += 1
            report["problems"].append(
                f"重复重合线段：第 {seen_seg[key] + 1} 段与第 {i + 1} 段 "
                f"({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) 完全重合"
                " —— 同一段走廊被画了两次")
        else:
            seen_seg[key] = i
    # 折线合并：分量内按端点接成链，数拐弯
    import collections as _cl
    comp_adj = _cl.defaultdict(list)
    for i, (a, b) in enumerate(segments):
        r = find(i)
        ka = (round(a[0], 6), round(a[1], 6))
        kb = (round(b[0], 6), round(b[1], 6))
        comp_adj[r].append((ka, kb))
    bends_total = 0
    for r, segs_c in comp_adj.items():
        deg = _cl.Counter()
        for ka, kb in segs_c:
            deg[ka] += 1
            deg[kb] += 1
        ends_pts = [p for p, dg in deg.items() if dg == 1]
        start = ends_pts[0] if ends_pts else segs_c[0][0]
        nxt = dict(segs_c)
        for ka, kb in segs_c:
            nxt.setdefault(ka, kb)
        chain = [start]
        cur = start
        prev = None
        while True:
            cands = [q for ka, kb in segs_c for q in (ka, kb)
                     if q == cur and q != prev]
            step = next((q for q in cands if q not in chain
                         or (q == start and len(chain) > 2)), None)
            if step is None:
                break
            chain.append(step)
            prev, cur = cur, step
            if len(chain) > len(segs_c) + 1:
                break
        dirs = []
        for p1, p2 in zip(chain, chain[1:]):
            dirs.append((p2[0] - p1[0], p2[1] - p1[1]))
        dirs = [(1 if d[0] > 0 else -1 if d[0] < 0 else 0,
                 1 if d[1] > 0 else -1 if d[1] < 0 else 0) for d in dirs]
        bcnt = sum(1 for d1, d2 in zip(dirs, dirs[1:])
                   if d1[0] * d2[1] + d1[1] * d2[0] != 0
                   and (d1[0] != d2[0] or d1[1] != d2[1]))
        bends_total += bcnt
        # 同轴折返：折线里出现方向完全相反的连续段（S 形首段）
        for d1, d2 in zip(dirs, dirs[1:]):
            if d1[0] == -d2[0] and d1[1] == -d2[1]:
                report["warnings"].append(
                    f"导线折线存在同轴折返（方向反转）——可能是不必要的 "
                    f"S 形绕行，共 {len(chain) - 1} 段、{bcnt} 个拐弯")
                break
    # 平行出线：同一引脚点出发的两条同网水平/竖直段方向相同
    par_cnt = 0
    for pt, idxs in by_pt.items():
        if len(idxs) < 2:
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i2, j2 = idxs[x], idxs[y]
                if find(i2) != find(j2):
                    continue
                a1, b1 = segments[i2]
                a2, b2 = segments[j2]
                # 两个段都从 pt 出发、方向向量同向（含 0 长度跳过）
                for (p1, q1), (p2, q2) in (((a1, b1), (a2, b2)),):
                    d1 = (1 if q1[0] > p1[0] else -1 if q1[0] < p1[0] else 0,
                          1 if q1[1] > p1[1] else -1 if q1[1] < p1[1] else 0)
                    d2 = (1 if q2[0] > p2[0] else -1 if q2[0] < p2[0] else 0,
                          1 if q2[1] > p2[1] else -1 if q2[1] < p2[1] else 0)
                    if d1 == d2 and (d1[0] or d1[1])                             and min(abs(q1[0] - p1[0]) + abs(q1[1] - p1[1]),
                                    abs(q2[0] - p2[0]) + abs(q2[1] - p2[1])) > _GEOM_EPS:
                        # 段不能是首尾相接的同一条折线（端点互为对方终点）
                        if q1 == p2 or q2 == p1:
                            continue
                        # 共线重叠（一条包含另一条）= 同网共享走廊 ——
                        # 最长路径先画 + 短连接 bind_only 后仍会以
                        # "长段包含短段"的形态出现（R20 输出行实测），
                        # 不是两条平行车道
                        collinear = (abs(p1[1] - q1[1]) < 1e-9
                                     and abs(p2[1] - q2[1]) < 1e-9)                             or (abs(p1[0] - q1[0]) < 1e-9
                                and abs(p2[0] - q2[0]) < 1e-9)
                        if collinear:
                            continue
                        par_cnt += 1
                        report["problems"].append(
                            f"同一节点 ({pt[0]:.4g},{pt[1]:.4g}) 引出两条"
                            f"平行同向线段：({p1[0]:.4g},{p1[1]:.4g})->"
                            f"({q1[0]:.4g},{q1[1]:.4g}) 与 "
                            f"({p2[0]:.4g},{p2[1]:.4g})->({q2[0]:.4g},"
                            f"{q2[1]:.4g}) —— 同一分流关系不应画两条平行线")
    # 绕行系数：曼哈顿下界 = Σ 每分量端点对的最短曼哈顿距离（近似取
    # 分量引脚点的最小生成树长度下界）；分量实际长度 = Σ 段长
    total_len = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segments)
    lb_len = 0.0
    for r, segs_c in comp_adj.items():
        pts_c = sorted({p for ka, kb in segs_c for p in (ka, kb)})
        if len(pts_c) < 2:
            continue
        # Prim 最小生成树（曼哈顿），O(k^2) —— 分量都很小
        rest = pts_c[1:]
        cur_set = [pts_c[0]]
        while rest:
            best = min(((abs(p[0] - q[0]) + abs(p[1] - q[1]), q)
                        for p in cur_set for q in rest))
            lb_len += best[0]
            cur_set.append(best[1])
            rest.remove(best[1])
    report["metrics"] = {
        "total_wire_length": round(total_len, 4),
        "detour_ratio": round(total_len / lb_len, 4) if lb_len > 1e-9 else 1.0,
        "bends": bends_total,
        "duplicate_segments": dup,
        "parallel_exits": par_cnt,
    }

    # 7) 紧凑度指标（2026-09-30）：元件分散程度 —— 与"零几何问题"互补，
    #    验收必须同时回答元件是否更靠近、主电路是否减少不必要空白。
    #    主电路 = 非 annot 实例（VAR/MSUB/模型包含/仿真控制器/注释件除外，
    #    含 GND：接地支路是主电路的一部分）；全图 = 全部实例。
    #    bbox 按实例 origin 统计（前后对比同口径）。
    _c_annot = set(_AUTO_ANNOT_MASTERS) | {"INCLUDE"}
    main_xy, full_xy = [], []
    for inst in list(design.instances):
        try:
            o = inst.origin
            x, y = float(o.x), float(o.y)
        except Exception:  # noqa: BLE001 — 读不到 origin 的实例跳过
            continue
        full_xy.append((x, y))
        m = _master_short(_inst_master(inst)).upper()
        if m in _c_annot or "INCLUDE" in m:
            continue
        main_xy.append((x, y))

    def _bbox_of(pts):
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        return {"w": round(w, 4), "h": round(h, 4),
                "area": round(w * h, 4),
                "x": [round(min(xs), 4), round(max(xs), 4)],
                "y": [round(min(ys), 4), round(max(ys), 4)]}

    seg_lens = [abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in segments]
    # 接地支路连接长度：每个 GND 引脚上直连段的曼哈顿长度之和
    #（GND 引脚通常是一根短线；共享地的汇入水平段计入该段自身）
    gnd_branch_len = 0.0
    for pt, idxs in pin_pts.items():
        if not any(e[1].upper().endswith(("GROUND", "GND")) for e in idxs):
            continue
        for i in range(len(segments)):
            a, b = segments[i]
            if (round(a[0], 6), round(a[1], 6)) == pt or \
                    (round(b[0], 6), round(b[1], 6)) == pt:
                gnd_branch_len += abs(a[0] - b[0]) + abs(a[1] - b[1])
    report["metrics"].update({
        "main_bbox": _bbox_of(main_xy),
        "full_bbox": _bbox_of(full_xy),
        "longest_segment": round(max(seg_lens), 4) if seg_lens else 0.0,
        "long_segment_count": sum(1 for L in seg_lens if L > 8.0),
        "gnd_branch_length": round(gnd_branch_len, 4),
    })
    return report


def connect_impl(design, inst_a, pin_a, inst_b, pin_b):
    """手工路径的引脚连线：正交寻径 + 逐段画线 + 显式绑定网络。

    与 build_schematic 的 _wire_connection 同一套规则：端点不共轴自动加
    90° 拐点；找不到不穿元件、不与已有导线交叉/重叠的正交路径就报错，
    绝不退化为斜线。已有导线从设计里现读（追加布线也避让）。
    """
    pa = _find_pin_on(inst_a, pin_a)
    pb = _find_pin_on(inst_b, pin_b)
    x1, y1 = _xy_point(pa.snap_point)
    x2, y2 = _xy_point(pb.snap_point)
    boxes = _symbol_boxes(design)
    pin_boxes = []
    for pt in ((x1, y1), (x2, y2)):
        pin_boxes.extend(_box_owning_pins(pt, boxes))
    pin_boxes = list(dict.fromkeys(pin_boxes))
    _net_ab = _net_label(getattr(pa, "net", None))
    _tap = []
    if _net_ab:
        for _i3 in list(design.instances):
            for _p3 in list(getattr(_i3, "inst_pins", []) or []):
                if _net_label(getattr(_p3, "net", None)) == _net_ab:
                    _s3 = getattr(_p3, "snap_point", None)
                    if _s3 is not None:
                        _xy3 = _xy_point(_s3)
                        _tap.append((_xy3[0], _xy3[1]))
    pts = _route_around((x1, y1), (x2, y2), boxes,
                        _design_wire_routes(design), pin_boxes=pin_boxes,
                        tap_pins=_tap)
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8:
            raise RuntimeError(f"走线包含斜段: {a} -> {b}")
        if a != b:
            design.add_wire([a, b])
    _bind_pins(design, pa, pb)
    return pts


def wire_impl(design, points):
    """手工路径的画线：逐段验证正交 + 避障，不合格直接报错。

    手工折点是明确意图，不做静默改道：出现斜段、穿符号或与已有导线
    交叉/重叠时抛错（原设计未动），要自动寻径请用 connect()。
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 2:
        raise RuntimeError("wire 至少需要两个点")
    boxes = _symbol_boxes(design)
    routes = _design_wire_routes(design)
    ends = {pts[0], pts[-1]}
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8:
            raise RuntimeError(
                f"wire 折点含斜段: ({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g})。"
                "导线必须严格沿 X 或 Y；要自动正交连线请用 connect()")
    for a, b in zip(pts, pts[1:]):
        if a == b:
            continue
        if any(_segment_hits_box(a, b, box) for _, box in boxes):
            raise RuntimeError(
                f"wire 段 ({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) "
                "穿过元件符号内部")
        for old_route in routes:
            old = old_route[0] if isinstance(old_route, tuple) else old_route
            old_ends = {old[0], old[-1]}
            if any(_segment_hits_route(a, b, c, d, ends, old_ends)
                   for c, d in zip(old, old[1:])):
                raise RuntimeError(
                    f"wire 段 ({a[0]:.4g},{a[1]:.4g})->({b[0]:.4g},{b[1]:.4g}) "
                    "与已有导线交叉或重叠")
    for a, b in zip(pts, pts[1:]):
        if a != b:
            design.add_wire([a, b])
    return pts


def save_impl(design):
    """保存前先跑导线几何硬门禁，再 save_design()。

    斜线、穿符号、异网交叉在落盘**前**拦截 —— 磁盘上的原设计不受影响；
    失败时改用 connect()（自动正交寻径）或 build_schematic 重建。
    """
    geo = _geometry_report(design)
    if geo["problems"]:
        raise RuntimeError(
            "保存前导线几何检查未通过（未保存，原设计不受影响）：\n- "
            + "\n- ".join(geo["problems"])
            + "\n修复方式：删掉违规导线（shape.delete_object()），"
              "引脚连线改用 connect() —— 它自动走正交并避障。")
    for w in geo["warnings"]:
        print("[geometry] 疑点:", w)
    for u in geo["unverified"]:
        print("[geometry] 未验证:", u)
    try:
        design.save_design()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "read-only" in msg.lower() or "read only" in msg.lower():
            raise RuntimeError(
                "保存失败：这个设计是只读打开的，本次所有改动都不会写入磁盘。\n"
                "请用 d = odesign(lib, cell, write=True) 重新打开后再改并 save(d)。"
            ) from e
        raise
    return "saved"


def _layout_scale_issue(design, connections, limit: float = 200.0) -> str:
    """检查导线跨度是否远大于元件符号，避免全图缩放后符号不可见。"""
    by_name = {_inst_name(inst): inst for inst in list(design.instances)}
    sizes = []
    for inst in by_name.values():
        try:
            box = inst.bbox
            sizes.append(max(float(box.upper_right.x - box.lower_left.x),
                             float(box.upper_right.y - box.lower_left.y)))
        except Exception:  # noqa: BLE001
            continue
    sizes = sorted(s for s in sizes if s > 0)
    if not sizes:
        return ""
    symbol_size = sizes[len(sizes) // 2]
    for conn in connections:
        try:
            a, b = conn["a"], conn["b"]
            pa = _find_pin_on(by_name[str(a[0])], a[1])
            pb = _find_pin_on(by_name[str(b[0])], b[1])
            x1, y1 = _xy_point(pa.snap_point)
            x2, y2 = _xy_point(pb.snap_point)
            distance = math.hypot(x2 - x1, y2 - y1)
        except Exception:  # noqa: BLE001
            continue  # 无效连接由 _wire_connection 给出更准确的错误
        if distance > limit * symbol_size:
            return (f"{a} 到 {b} 的导线长 {distance:.3g}，而元件符号典型尺寸仅 "
                    f"{symbol_size:.3g}（比例 {distance / symbol_size:.0f}:1）。"
                    "整图缩放后元件会不可见；请把元件坐标间距缩小，"
                    "例如 ADS 标准符号宽约 1 时用 3~8 的间距。")
    return ""


def _verify_params(by_name: dict, instances: list) -> list:
    """对照构建清单核对落盘后的实例参数，返回不一致列表。"""
    bad = []
    for spec in instances:
        inst = by_name.get(str(spec.get("name")))
        if inst is None:
            continue
        have = {}
        try:
            for p in list(inst.parameters):
                have[str(getattr(p, "name", ""))] = str(getattr(p, "value", ""))
        except Exception:  # noqa: BLE001
            continue
        for k, v in (spec.get("params") or {}).items():
            got = have.get(str(k))
            if got is None or _unquote(got) != _unquote(str(v)):
                bad.append({"instance": spec.get("name"), "param": str(k),
                            "requested": str(v), "on_disk": got})
    return bad


def _verify_var_values(by_name: dict, var_spec: dict | None) -> list:
    """核对 build_schematic 新增的 VAR 方程确实保存到磁盘。"""
    if not var_spec:
        return []
    name = str(var_spec.get("name") or "VAR1")
    inst = by_name.get(name)
    if inst is None:
        return []  # 实例缺失由 build_schematic 的 missing 检查报告
    try:
        have = _dump_vars(inst)
    except Exception as e:  # noqa: BLE001
        return [{"instance": name, "param": "<VAR>", "requested": "可读取",
                 "on_disk": f"{type(e).__name__}: {e}"}]
    bad = []
    for key, value in (var_spec.get("values") or {}).items():
        got = have.get(str(key))
        if got != str(value):
            bad.append({"instance": name, "param": str(key),
                        "requested": str(value), "on_disk": got})
    return bad


def _check_connection_list(by_name: dict, connections) -> list:
    """核对落盘后的连通性：同一连接的两个引脚必须在同一个网络上。"""
    results = []
    for conn in (connections or []):
        a = conn.get("a") if isinstance(conn, dict) else None
        b = conn.get("b") if isinstance(conn, dict) else None
        entry = {"a": a, "b": b}
        try:
            ia = by_name.get(str(a[0])) if isinstance(a, (list, tuple)) else None
            ib = by_name.get(str(b[0])) if isinstance(b, (list, tuple)) else None
            if ia is None or ib is None:
                missing = a[0] if ia is None else b[0]
                raise KeyError(f"设计里找不到实例 {missing!r}")
            na = _net_label(getattr(_find_pin_on(ia, a[1]), "net", None))
            nb = _net_label(getattr(_find_pin_on(ib, b[1]), "net", None))
            if na and nb and na == nb:
                entry.update({"status": "ok", "net": na})
            elif not na and not nb:
                entry.update({"status": "failed",
                              "reason": "两个引脚都没有网络（连线没有生效）"})
            else:
                entry.update({"status": "failed",
                              "reason": f"引脚不在同一网络（{na or '无'} vs {nb or '无'}）"})
        except KeyError as e:
            entry.update({"status": "failed", "reason": str(e)})
        except Exception as e:  # noqa: BLE001
            entry.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# 信号流自动布局（build_schematic layout="auto"）
# ---------------------------------------------------------------------------

_AUTO_GND_MASTERS = {"GROUND", "GND"}
_AUTO_PORT_MASTERS = {"TERM", "TERMG"}
_AUTO_ANNOT_MASTERS = {
    "VAR", "MSUB", "S_PARAM", "SWEEP_PLAN", "SWEEPPLAN", "OUTPUT_PLAN",
    "OPTIM", "GOAL", "PARAMETER_SWEEP", "EQN", "MEAS_EQN", "NOTE", "TEXT",
    "NODESET", "DA_", "STIM",
}
# 无引脚、不参与信号连接的模型包含/选项件（如 Infineon_Include_RF，
# CB_FM_Amp 实测 2026-09-29）——按 master 名包含 INCLUDE 识别为注释件
_AUTO_INCLUDE_RE = re.compile(r"INCLUDE", re.IGNORECASE)
# angle=0 时标准符号的引脚偏移（实测：MLIN/R/Term 两端引脚在 origin 和
# origin+(1,0)；MTEE 第 3 脚（分支）在 origin+(0.5,-0.5)；TermG 单脚在
# origin 上、符号体在 origin 右侧（bbox 0~0.875），所以输入口要放 180°）
_AUTO_PIN_OFFS = {
    "MTEE": {"1": (0.0, 0.0), "2": (1.0, 0.0), "3": (0.5, -0.5)},
    "MCROS": {"1": (0.0, 0.0), "2": (1.0, 0.0), "3": (0.5, -0.5), "4": (0.5, 0.5)},
    "TERMG": {"1": (0.0, 0.0)},
    # 耦合线（实测 2026-09-29）：直通边 1→4 在 y=0，耦合边 2→3 在 y=-0.5；
    # 2/3 为离轴引脚，自动按分支脚处理
    "CLIN": {"1": (0.0, 0.0), "2": (0.0, -0.5),
             "3": (1.0, -0.5), "4": (1.0, 0.0)},
    # BFR106（Infineon RF BJT，CB_FM_Amp 实测 2026-09-29）：1=c(0.5,0.5)
    # 2=b(0,0) 3=e(0.5,-0.5)，三脚全在符号 bbox 边缘(体 0.5x1.0)，网表
    # 顺序 c b e 与实测一致。注意:ads_rflib 的 C/R/L/V_DC 在 angle=0 时
    # 全部 pin2=(1,0) 水平(实机 bbox 复核,CB_FM_Amp_AGENT 建档) ——
    # 不要从已有设计反推偏移:原设计的 angle 属性若读不到会显示成 0,
    # 旋转过的实例会被误认成 angle=0 几何(2026-09-29 实测踩过)。
    "BFR106": {"1": (0.5, 0.5), "2": (0.0, 0.0), "3": (0.5, -0.5)},
}

# 直流源 master（电源母线网络的判据之一；短名，大小写不敏感由 _master_short 保证）
_AUTO_DC_SRC_MASTERS = {"V_DC", "I_DC"}
# 母线距最高锚点行的距离 / 锚点引脚到首列的偏移 / 并联支路列距
#（2026-09-29 CE_FM_Amp 手工参考图实测：bus=锚点行+2.125，首列 +0.55，
#  输出并联件列距 1.0，接地支路元件上端骑在行线上、下端地符号压脚）
_RAIL_BUS_DY = 2.125
_RAIL_COL_DX = 0.55
_SHUNT_COL_STEP = 1.0

# 元件信号轴用哪两个引脚标签定义（默认 1→2）。CLIN 的 1→2 是耦合边，
# 直通边是 1→4 —— 轴认错边会把直通链判成分支，滤波器排不成一行。
_AUTO_AXIS_LABELS = {"CLIN": ("1", "4")}
_AUTO_PIN_OFFS_DEFAULT = {"1": (0.0, 0.0), "2": (1.0, 0.0)}

# 对称一分二（威尔金森 ML 类）专用排版常数 —— 只作用于对称分支，不影响
# 通用分支布局。row_h=4 的依据：上臂元件文字（上移后）/端口文字/电阻
# 文字（右下伸约 2.5 格）互不侵入，电阻文字底距下臂线仍有 >1 格；
# gap=1.4 的依据：端口文字左缘（origin-0.66）距臂元件文字 >0.6。
_SYM_ROW_H = 3.0
_SYM_GAP = 1.4

# 通用链路紧凑常数（2026-09-30 紧凑化轮）：
# * 链内相邻元件的引脚间距不再统一用 1.9 —— 基线 1.4（与对称分支
#   _SYM_GAP 同源，十轮压缩实测的文字不互压下限），再按"前件文字越过
#   出脚的伸出量 + 后件文字逆着链方向的伸出量"逐对加大；
# * 通用分支行距不再固定 5.0 —— 按上一行实际内容（符号盒/文字区/
#   骑线支路的地脚延伸）的向下深度逐行计算，MTEE 行自动保持 ~5 格，
#   纯 R/L/C 行收到 ~2.6 格。
_CHAIN_GAP_MIN = 1.4
_GAP_TEXT_MARGIN = 0.5
# 高文字块器件（MTEE/CLIN 类）邻接时的间距下限：两侧文字区之间要留出
# 标注避让通道（2026-09-30 MTEE 威尔金森案例实测，1.4 会让避让无解）
_CHAIN_GAP_TALL = 1.9
_ROW_GAP_MIN = 2.6
_ROW_GAP_MARGIN = 0.25


def _pin_text_overhang(master, angle, lbl, side):
    """元件文字区沿链方向（side=+1）或逆链方向（side=-1）越过指定引脚的
    伸出量（≥0）。用于相邻串联元件的逐对间距：间距下限 = 前件右伸出 +
    后件左伸出 + _GAP_TEXT_MARGIN。文字区模型与避让同源（_annot_text_zone）。
    """
    key = _master_short(str(master))
    zone = _annot_text_zone(key, 0.0, 0.0, 0.0, 0.0, angle)
    if zone is None:
        return 0.0
    o = _AUTO_PIN_OFFS.get(key, _AUTO_PIN_OFFS_DEFAULT)
    rx, _ry = _rot_off(*o.get(str(lbl), (0.0, 0.0)), int(angle or 0))
    return max(0.0, zone[2] - rx) if side > 0 else max(0.0, rx - zone[0])


def _master_short(master) -> str:
    return str(master).replace("/", ":").split(":")[-1].upper()


def text_wire_conflict_level(elem_angle, pt_a, pt_b):
    """文字区-导线冲突分级（单一事实来源，2026-09-30 收敛）。

    返回:
      "hard" —— 必须处理:竖直段穿任何文字、水平段穿**横放**文字
      "soft" —— 手工图常态可接受:水平段穿**竖放**元件文字
                （行线从参数文字间穿行不可避免，CE_FM_Amp R1/R2
                手工参考图即此形态）
      "none" —— 段不与文字走向相容（调用方仍需自己做 bbox 命中判断）

    本函数只做"段方向 × 元件朝向"的分级；是否命中 zone 由调用方
    先用 _segment_hits_box 判断。fuzz/harness/测试统一引用本函数，
    避免四处各写一套口径漂移。
    """
    horizontal_seg = abs(pt_a[1] - pt_b[1]) < 1e-9
    vertical_elem = (int(elem_angle or 0) % 180) == 90
    if horizontal_seg and vertical_elem:
        return "soft"
    return "hard"


def _rot_off(dx, dy, angle):
    """angle=0 的引脚偏移按实例角度旋转（ADS 逆时针，90°: (x,y)->(-y,x)）。"""
    a = int(angle or 0) % 360
    if a == 90:
        return (-dy, dx)
    if a == 180:
        return (-dx, -dy)
    if a == 270:
        return (dy, -dx)
    return (dx, dy)


def _annot_text_zone(key, x, y, ax=0.0, ay=0.0, angle=0):
    """实例文字块区域（相对 origin 的渲染实测常数，含 annot 偏移）。

    用于通用布局的标注避让：规划导线穿过谁的文字区，就把谁挪开。
    key 是 master 短名（MLIN/MTEE/TERM/TERMG/R/MSUB/VAR/S_PARAM/GROUND）。
    """
    x += ax
    y += ay
    a = int(angle or 0) % 360
    if key == "MLIN":
        if a % 180 == 90:
            # 竖放：名字行左缘 x+0.35（与横向模型 x+0.20 同源——名字
            # 行不随旋转移到更左），参数行右伸 x+2.3；旧值左缘 x+0.10
            # 把名字行也框进保守区，导致与端口文字的 0.13 格假互压
            # （P1 右缘 x+0.78 vs 区左缘 x+0.75，2026-09-29 渲染复核）
            return (x + 0.35, y - 2.30, x + 2.30, y + 0.40)
        return (x + 0.20, y - 1.25, x + 1.45, y - 0.15)
    if key == "CLIN":          # 名字 + 4 行参数全在 origin 正下方（实测 2026-09-29）
        return (x + 0.20, y - 2.30, x + 1.65, y - 0.15)
    if key == "MTEE":          # 名字 + 5 行参数全在 origin 正下方（硬坑 4）
        return (x + 0.20, y - 5.05, x + 1.45, y - 0.35)
    if key == "R":
        # 2026-09-30 scratch 实测（AI_annot_probe，bbox_annotation_only）:
        # angle 90（体向上,origin=下引脚）文字在体右侧上半 [0.15,0.42,0.93,1.0];
        # angle 270（体向下,origin=上引脚）文字贴 origin 右下 [0.15,-0.58,0.93,0]。
        # 旧模型不分支（y-2.6..+0.4）把 270 的文字区向下虚扩 2 格——RD 文字
        # 被幻影命中赶出支路 2.8 格的根因之一;90 的真实文字反而漏检。
        if a % 360 == 90:
            return (x + 0.10, y + 0.32, x + 1.02, y + 1.08)
        if a % 360 == 270:
            return (x + 0.10, y - 0.68, x + 1.02, y + 0.08)
        return (x + 0.10, y - 0.80, x + 1.20, y - 0.05)
    if key == "C":              # ads_rflib:C（横放实测 2026-09-29;竖放实测 09-30）
        if a % 360 == 90:
            return (x + 0.15, y + 0.32, x + 0.82, y + 1.08)
        if a % 360 == 270:
            return (x + 0.15, y - 0.68, x + 0.82, y + 0.08)
        return (x + 0.15, y - 0.85, x + 0.90, y - 0.15)
    if key == "L":              # ads_rflib:L（2026-09-30 scratch 实测;旧模型缺失
        # —— LOUT/LC 文字从不参与避让与互斥检查,输出支路 RD×LOUT 互压漏检）
        if a % 360 == 90:
            return (x + 0.07, y + 0.13, x + 0.73, y + 1.08)
        if a % 360 == 270:
            return (x + 0.07, y - 0.87, x + 0.73, y + 0.08)
        return (x + 0.15, y - 0.95, x + 0.90, y - 0.05)
    if key == "TERMG":
        # 实测（TG180/TG0 scratch）:angle 180 体在引脚左侧,文字默认引脚左下
        # [-0.67,-1.06,+0.11,-0.27];angle 0 体在右,文字右下 [0.21,-1.06,0.99,-0.27]。
        # 旧模型 180 框在引脚右侧 —— CE_FM_Amp_AGENT PORT1 文字被幻影命中
        # 连推两次 -1.2、悬到符号左 2.4 格的根因。
        if a == 180:
            return (x - 0.75, y - 1.12, x + 0.20, y - 0.20)
        return (x + 0.15, y - 1.12, x + 1.05, y - 0.20)
    if key == "TERM":
        if a == 180:   # 射频脚在 origin、地脚在 +x：文字偏右下（实测）
            return (x + 0.15, y - 1.10, x + 0.80, y - 0.20)
        return (x + 0.15, y - 1.12, x + 1.05, y - 0.20)   # 实测 T0 [0.21,-1.06,0.99,-0.27]
    if key == "BFR106":         # 晶体管名字+型号在 origin 下方（实测 CE_FM_Amp Q1
        # [0.21,-0.96,0.79,-0.58];仅横放建档,旋转件保守沿用,渲染复核兜底）
        return (x + 0.16, y - 1.00, x + 0.84, y - 0.58)
    if key == "V_DC":           # 实测 VCC [0.21,-0.84,0.83,-0.26]
        return (x + 0.15, y - 0.90, x + 0.90, y - 0.20)
    if key == "MSUB":
        return (x + 0.20, y - 2.65, x + 1.80, y + 0.55)
    if key == "VAR":
        # 渲染实测（2026-09-29，met harness 旧模型同源）：变量名文字
        # 在 [x+0.60, x+1.65]，值行向下生长；旧值左伸 x-0.05 会与
        # 输入行元件的竖放文字假互压（ML1 vs VAR1 0.5 格假重叠）。
        # 区高按行数收：1~8 行约 0.62 格/行 + 0.6 余量（8 行 5.6 封顶
        # 3.6 与旧模型一致），避免 1 变量的 VAR 定格在高位时深伸进
        # 信号区（ML1 vs VAR1 y 向 0.2 假重叠实测）
        return (x + 0.60, y - min(3.60, 0.62 * 8 + 0.60), x + 1.65, y + 0.10)
    if key == "S_PARAM":
        return (x - 0.05, y - 1.00, x + 2.70, y + 0.50)
    # GROUND 无标注文字（CE_FM_Amp 手工设计实机 dump：G1..G8 的
    # bbox_annotation_only 全为空）—— 旧模型 [x-0.15,y-0.4,..] 覆盖了
    # 引脚进入带，每根地线的进脚段都被判"穿字"、避让器乱挪不存在的
    # 文字（fuzz 种子1 实测 G1 文字被挪到主线上）。返回 None。
    return None


def _fallback_chain(rf, adj):
    """没有端口或端口间不通时：取全图最长简单链当主干，保证布局总能给出。"""
    best = []
    for start in rf:
        stack = [(start, (start,))]
        while stack:
            cur, path = stack.pop()
            if len(path) > len(best):
                best = list(path)
            for nxt in sorted(adj[cur], reverse=True):
                if nxt not in path:
                    stack.append((nxt, path + (nxt,)))
    return best


def auto_layout_positions(instances, connections, var_spec=None):
    """按信号流算坐标：主干链一行、分支向下成行、跨链件竖放在行间。

    输入是 build_schematic 的 instances/connections 清单（坐标可缺省），
    返回 {实例名: {"x":…, "y":…}}（含 VAR 实例名）。只算坐标，不改
    angle/mirror/params/连接 —— 网表与手工摆放完全一致。

    规则（功分器/放大链/滤波器这类 RF 链路图都收敛）：
    * 对称一分二优先：识别出 输入链→真 T 结→两条支链→双输出（威尔金森
      ML 类，导线直接搭 T）时，两支链镜像放主干行上下 ±row_h，隔离电阻
      竖放在两臂出脚同列正中 —— 关于主干行轴对称的经典画法；摆不下落回
      通用规则。分叉元件与上臂桥接元件的标注自动挪开避免线穿字；
    * 主干 = 端口(Term)间最短路径，放 y=0 行、从左到右（输入口朝右出线）；
    * 其余端口路径去掉已放前缀后作分支，挂在锚点引脚正下方一行
      （行距 5：4 行参数文本约 2.4 高，上下行文本不重叠），仍从左到右；
    * 不在任何端口路径上的元件（隔离电阻这类跨链件）竖放在上下两行正中，
      对齐上方引入引脚的列，两端由曼哈顿走线补直角；
    * GROUND 紧靠所接地脚，端口外侧稍作横向错位，避免地线沿符号边缘下行；
    * VAR/MSUB 排图左上、仿真控制器排左下，行高按文本行数算，避开信号区。
    相邻引脚间导线长 2：参数文本比符号宽（约 2.2~2.6），间距再小文本会
    压到下一个符号；行内 origin 间距 ≥3，文本互不重叠。
    """
    gap = _CHAIN_GAP_MIN   # 链内相邻引脚间距基线；place_chain 按前后件文字
    #                        伸出量逐对加大（_pin_text_overhang），简单
    #                        R/L/C 互连不再被统一 1.9 拉远

    specs = {str(s["name"]): s for s in instances}

    def kind(n):
        m = _master_short(specs[n]["master"])
        if m in _AUTO_GND_MASTERS:
            return "gnd"
        if m in _AUTO_ANNOT_MASTERS or _AUTO_INCLUDE_RE.search(m):
            return "annot"
        return "rf"

    rf = [n for n in specs if kind(n) == "rf"]
    rf_set = set(rf)

    placed = {}
    assigned = set()   # 已摆放的 rf（提前绑定：对称分支的闭包要先于主路径用到）
    row_of = {}

    # 端口朝向规范化（仅当调用方没给 angle）：主干首端口=in、链尾端口=out。
    # 必须在摆放**前**定下来 —— Term 的射频脚偏移 (1,0) 随角度旋转，先摆后改
    # 角度会让引脚坐标与导线端点错位。Term：输入 0 / 输出 180；TermG 相反
    # （实测其符号体在 origin 右侧、单脚在 origin 上，输入口要放 180 让
    # 符号体甩在电路外侧，否则第一段导线必然压符号）。
    resolved_angle = {}

    def _angle_of(n):
        if n in resolved_angle:
            return resolved_angle[n]
        if n in placed and placed[n].get("angle") is not None:
            return placed[n]["angle"]   # 对称分支的桥接件竖放角度（写在 placed）
        if n in specs:
            return specs[n].get("angle")
        return None   # var_spec 兜底创建的 VAR 等不在 specs 里

    def _normalize_port_angle(n, role):
        if specs[n].get("angle") is not None:
            return
        m = _master_short(specs[n]["master"])
        if m == "TERMG":
            resolved_angle[n] = 180 if role == "in" else 0
        elif m == "TERM":
            resolved_angle[n] = 0 if role == "in" else 180

    def offs(n):
        return _AUTO_PIN_OFFS.get(_master_short(specs[n]["master"]),
                                  _AUTO_PIN_OFFS_DEFAULT)

    def pin_abs(n, label):
        r = _rot_off(*offs(n).get(str(label), (0.0, 0.0)), _angle_of(n))
        return (placed[n]["x"] + r[0], placed[n]["y"] + r[1])

    # 分支脚：偏离元件通轴（1→2 引脚方向）的引脚，如 MTEE 的第 3 脚。
    # 注意按旋转后的几何算 —— 竖放 MLIN 的两个引脚都在轴上，不是分支脚。
    drop_pins = {}
    for n in rf:
        o = offs(n)
        la, lb = _AUTO_AXIS_LABELS.get(
            _master_short(specs[n]["master"]), ("1", "2"))
        p1 = o.get(la, (0.0, 0.0))
        p2 = o.get(lb, (1.0, 0.0))
        ax = _rot_off(p2[0] - p1[0], p2[1] - p1[1], _angle_of(n))
        labels = set()
        # 轴向量斜向的多端件（BFR106:1=c(0.5,0.5) 2=b(0,0) 3=e(0.5,-0.5)）
        # 没有"通轴"——三脚都是信号脚,共基/共射的主路径可能经任一脚,
        # 全部当 drop 会把主路径拦腰截断（CB_FM_Amp 主干被挤成输出链,
        # 2026-09-29 实测）。只有轴水平/竖直的多端件（MTEE 类）才有分支脚。
        if len(o) >= 3 and _master_short(specs[n]["master"]) \
                not in _AUTO_PIN_OFFS:
            # 未知多端器件（三端管类，引脚档案不在 _AUTO_PIN_OFFS）：
            # 默认两脚偏移会把第 3+ 脚判成"分支脚"，主信号路径在管子处
            # 被拦腰截断、链路散成两截互不相认的行（document1.pdf 大跨度
            # 折返形态的成因之一）。按 BFR106 同款处理：全部引脚可作
            # 信号通路；分支/桥接误识别由真 T 结判定与桥接守卫兜底。
            # （不用 _multi_pin —— 它在下方才定义，先引用会 UnboundLocalError）
            drop_pins[n] = set()
            continue
        if abs(ax[0]) > 1e-9 and abs(ax[1]) > 1e-9:
            drop_pins[n] = labels
            continue
        for l, (dx, dy) in o.items():
            rx, ry = _rot_off(dx, dy, _angle_of(n))
            if abs(ax[0] * ry - ax[1] * rx) > 1e-9:
                labels.add(l)
        drop_pins[n] = labels

    # 邻接：adj_side 只含"水平引脚"边（信号通路的内部边）；竖直引脚（MTEE
    # 分支脚这类，旋转后 dy≠0）只允许作分支/桥接的引入边。没有这条规则，
    # 隔离电阻（两端都是分支脚）会被误认成某条端口路径的一段，把真正的
    # λ/4 支线挤成桥接件（Wilkinson_5G9 实测踩过）。
    adj_side = {n: set() for n in rf}
    gnd_mates = {}  # rf 实例名 -> 连接的 GROUND 集合（挂靠判定用）
    adj_all = {n: set() for n in rf}
    gnd_partners = {}   # gnd 实例名 -> [(rf 实例名, rf 引脚标签)]
    pin_of = {}
    edge_drop = {}  # frozenset({a,b}) -> 是否分支脚引入边
    partner = {}    # gnd/annot 也记一下第一个连接对象
    # 引脚节点：被一条连线直接相连的两个引脚属同一节点（同一 (实例,引脚)
    # 出多条连线同理）。节点大小 >=3 即三分叉结 —— 没有 MTEE、用导线直接
    # 搭 T 的拓扑（Wilkinson_1G7_ML）靠它识别分支，见下方分支判定。
    node_parent = {}

    def _node_find(key):
        while node_parent.get(key, key) != key:
            node_parent[key] = node_parent.get(node_parent[key], node_parent[key])
            key = node_parent[key]
        return key

    def _node_union(a, b):
        ra, rb = _node_find(a), _node_find(b)
        if ra != rb:
            node_parent[rb] = ra

    for conn in connections or []:
        a, b = conn.get("a"), conn.get("b")
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            continue
        na, nb = str(a[0]), str(b[0])
        if na not in specs or nb not in specs or na == nb:
            continue
        partner.setdefault(na, (nb, str(b[1]), str(a[1])))
        partner.setdefault(nb, (na, str(a[1]), str(b[1])))
        # 两端点都先注册（代表自身也是节点成员，缺了会把 3 分叉数成 2）
        node_parent.setdefault((na, str(a[1])), (na, str(a[1])))
        node_parent.setdefault((nb, str(b[1])), (nb, str(b[1])))
        _node_union((na, str(a[1])), (nb, str(b[1])))
        if na not in rf_set or nb not in rf_set:
            # 接地对端单独记档:分支挂靠要判"单件支路的对端是不是地"
            #（adj_all 只收 rf-rf 边,查不到地）;gnd_partners 记下每个
            # 地符号的全部 rf 伙伴 —— 共享地的就近成组挂点要用
            if na in rf_set and kind(nb) == "gnd":
                gnd_mates.setdefault(na, set()).add(nb)
                gnd_partners.setdefault(nb, []).append((na, str(a[1])))
            elif nb in rf_set and kind(na) == "gnd":
                gnd_mates.setdefault(nb, set()).add(na)
                gnd_partners.setdefault(na, []).append((nb, str(b[1])))
            continue
        la, lb = str(a[1]), str(b[1])
        adj_all[na].add(nb)
        adj_all[nb].add(na)
        pin_of.setdefault((na, nb), la)
        pin_of.setdefault((nb, na), lb)
        if la in drop_pins.get(na, ()) or lb in drop_pins.get(nb, ()):
            edge_drop[frozenset((na, nb))] = True
        else:
            adj_side[na].add(nb)
            adj_side[nb].add(na)

    node_size = {}
    for key in node_parent:
        root = _node_find(key)
        node_size[root] = node_size.get(root, 0) + 1

    def _junction(n, label):
        """(实例, 引脚) 所在节点是否 >=3 分叉（真 T 结）。"""
        return node_size.get(_node_find((n, str(label))), 1) >= 3

    def _multi_pin(n):
        return len(offs(n)) >= 3

    def _axis_angle(n, entry, exit_, want_dir):
        """选 angle∈{0,90,180,270} 使 entry→exit 轴旋转后指向 want_dir(±1,0)。

        仅对两脚串联件调用;pin1 恒在 origin 时 entry 偏移为 0,轴=exit 偏移。
        R/C 默认轴 (0,-1)（竖放），水平链上须转 90°——此前不转，输出链引脚
        坐标错位是贴边绕行的根因之一（2026-09-29 基线实测）。"""
        if entry is None or exit_ is None:
            return 0
        o = offs(n)
        ax0 = (o[str(exit_)][0] - o[str(entry)][0],
               o[str(exit_)][1] - o[str(entry)][1])
        for ang in (0, 90, 180, 270):
            rx, ry = _rot_off(*ax0, ang)
            if abs(ry) < 1e-9 and rx * want_dir > 1e-9:
                return ang
        return 0

    def _shunt_angle(n, free_lbl, want_dy):
        """骑线件自由脚朝向 want_dy(±1)。

        自由脚不在 origin:转自由脚偏移到 (0,want)。
        自由脚在 origin(ads_rflib:R/C 的 pin1):转**另一脚**到反侧
        (0,-want) —— 体沿 want 方向延伸、自由脚留在近端。从 origin 脚
        本身算旋转恒为 0°,桥接/竖堆件会整体横放（fig4 R1 实测踩过）。"""
        o = offs(n)
        other = next((l for l in o if str(l) != str(free_lbl)), None)
        if other is None:
            return 0
        fx, fy = o.get(str(free_lbl), (0.0, 0.0))
        if abs(fx) < 1e-9 and abs(fy) < 1e-9:
            ox, oy = o[str(other)]
            for ang in (0, 90, 180, 270):
                rx, ry = _rot_off(ox, oy, ang)
                if abs(rx) < 1e-9 and ry * -want_dy > 1e-9:
                    return ang
            return 0
        for ang in (0, 90, 180, 270):
            rx, ry = _rot_off(fx, fy, ang)
            if abs(rx) < 1e-9 and ry * want_dy > 1e-9:
                return ang
        return 0

    def _stack_angle(n, entry, exit_):
        """竖堆串联件:angle 使 entry→exit 轴旋转后指向 +y(向上堆)。"""
        if entry is None or exit_ is None:
            return 0
        o = offs(n)
        ax0 = (o[str(exit_)][0] - o[str(entry)][0],
               o[str(exit_)][1] - o[str(entry)][1])
        for ang in (0, 90, 180, 270):
            rx, ry = _rot_off(*ax0, ang)
            if abs(rx) < 1e-9 and ry > 1e-9:
                return ang
        return 0

    def place_chain(chain, row_y, anchor, gap_x=None, direction=1,
                    shunt_up=None):
        """链沿 row_y 行排布(direction=1 向右 / -1 向左)。

        anchor=None 时 chain[0] 是源头，origin 直接放 cur_x；否则 chain[0]
        的引入引脚对齐 anchor 的引脚 x。链内每件按"链脚 label 集合"分类:
        两个不同链脚=串联件（angle 自动转到水平轴，链向 direction）；
        单一链脚=骑线 shunt 件（pin1 落在行线上，自由脚朝下/上由支路定）；
        三端及以上元件不自动转角度，按 entry/exit 偏移落位。
        返回链尾引脚沿排布方向侧的下一个 cur_x。"""
        g = gap if gap_x is None else float(gap_x)
        cur_x = float(pin_abs(anchor, pin_of.get((anchor, chain[0]), "1"))[0]) \
            if anchor is not None else 0.0
        if anchor is not None and direction < 0:
            # 向左排:链首件让出锚点脚左侧的连线走廊(水平线长 g);
            # 向右排保持 entry 引脚对齐锚点(竖直引入线)的旧语义
            cur_x += direction * g
        cur_row_y = row_y
        prev = None   # (实例名, 出脚 label, 出脚 x, 出脚是否竖直) —— 逐对间距用
        for i, n in enumerate(chain):
            nxt = chain[i + 1] if i + 1 < len(chain) else None
            entry = pin_of.get((n, chain[i - 1])) if i else \
                (pin_of.get((n, anchor)) if anchor is not None else None)
            exit_ = pin_of.get((n, nxt)) if nxt else None
            link_lbls = {str(l) for l in (entry, exit_) if l is not None}
            chain_set = set(chain) | {anchor}
            # 自由脚=非链脚且所在节点非分叉结（≥3 分叉的是分叉脚,如威尔金森
            # 输入链尾 ML1 的出脚,必须保持横放,转角会竖起整条输入链）
            free_lbls = [str(l) for l in offs(n)
                         if str(l) not in link_lbls and not _junction(n, l)] \
                if not _multi_pin(n) else []
            if specs[n].get("angle") is None and n not in resolved_angle:
                if len(link_lbls) == 2 and not _multi_pin(n):
                    resolved_angle[n] = _axis_angle(n, entry, exit_, direction)
                elif free_lbls:
                    # 骑线 shunt:自由脚朝向 —— 调用方指定(行挂锚点上方时
                    # 朝上,免得地脚插回主排)优先;否则默认朝下,自由脚
                    # 对端 rf 已放在上方则朝上
                    want = shunt_up if shunt_up is not None else -1
                    if shunt_up is None:
                        for m in adj_all[n]:
                            if m not in chain_set and m in placed \
                                    and kind(m) == "rf" and pin_abs(
                                        m, pin_of.get((m, n), "1"))[1] > row_y + 1e-6:
                                want = 1
                                break
                    resolved_angle[n] = _shunt_angle(n, free_lbls[0], want)
            eo = _rot_off(*offs(n).get(str(entry), (0.0, 0.0)), _angle_of(n)) \
                if entry else (0.0, 0.0)
            xo = _rot_off(*offs(n).get(str(exit_), (0.0, 0.0)), _angle_of(n)) \
                if exit_ else eo
            if i and prev is not None:
                # 逐对间距（2026-09-30 紧凑化）：基线 g 之上，按"前件文字
                # 区越过其出脚、沿链方向伸出的距离 + 余量"加大（纯几何
                # 口径，按 zone 绝对边界算 —— 出脚向后/向前出线都能正确
                # 处理）。简单 R/L/C 互连取基线 1.4，参数文字长的器件
                # （MTEE/CLIN）自动让出实际占用 —— 不再用统一 1.9 把所有
                # 元件拉远。只算前件（over_prev）：本件文字对再下一件的
                # 让位由 prev 携带的 zone 在下一轮体现；若把本件 over_me
                # 计进自己的入口推进，链尾端口的射频脚会被推离前件出脚
                # 列、破坏"脚对脚"对齐不变量（显式角度案例实测）。
                # 高文字块通道（MTEE 6 行类，zone 高 >2.4 会占满行间
                # 竖直空间）：与前件任一方是高块时，间距下限
                # 提到 1.9 —— 两侧文字区之间必须留出标注避让通道，
                # 否则 7.5 步避让全候选被堵（MTEE 威尔金森 MTEE2 实测：
                # gap 1.4 时 ML2 文字与 MTEE2 文字交叠 0.25、避让无解）。
                # CLIN 4 行（zone 高 2.15）在 1.4 下无避让死锁，不提。
                p_exit_x, p_zone, p_vert, p_n_prev = prev
                if p_zone is not None:
                    over_prev = max(0.0, p_zone[2] - p_exit_x) \
                        if direction > 0 else max(0.0, p_exit_x - p_zone[0])
                    # 只认横放高块：竖放件（竖放 MLIN zone 高 2.7）的
                    # 文字在自身 origin 右侧铺开、不占入口通道，其对
                    # 后件的让位由 over_prev（x 向伸出）覆盖
                    _p_tall = (p_zone[3] - p_zone[1]) > 2.4                         and int(_angle_of(p_n_prev) or 0) % 180 == 0
                else:
                    over_prev = 0.0
                    _p_tall = False
                _my_zone0 = _annot_text_zone(
                    _master_short(specs[n]["master"]), 0.0, 0.0,
                    0.0, 0.0, _angle_of(n))
                _me_tall = _my_zone0 is not None \
                    and (_my_zone0[3] - _my_zone0[1]) > 2.4 \
                    and int(_angle_of(n) or 0) % 180 == 0
                g_pair = max(g, over_prev + _GAP_TEXT_MARGIN,
                             _CHAIN_GAP_TALL if (_p_tall or _me_tall) else 0.0)
                cur_x = p_exit_x + direction * g_pair \
                    + (direction * 1.0 if p_vert else 0.0)
            if _multi_pin(n):
                # 三端件：入口脚对齐 cur_x 与行线；出口脚高度即后续行线
                # （CE 放大器：输入行齐基极、输出行齐集电极，入口侧不再
                # 有 0.5 格落差要绕；共基放大器输入行齐发射极同理。
                # pin1/pin2 共线的多端件（MTEE/CLIN）两行等高，行为不变）
                placed[n] = {"x": cur_x - eo[0], "y": cur_row_y - eo[1]}
                cur_row_y = placed[n]["y"] + xo[1]
            elif len(link_lbls) == 2:
                # 串联：entry 脚对齐 cur_x，exit（出线）脚对齐行线
                placed[n] = {"x": cur_x - eo[0], "y": cur_row_y - xo[1]}
            else:
                # 骑线 shunt:引入引脚落在行线上(CIN.1 骑 PORT1-LIN 线;
                # 链尾 Term 的射频脚对齐前件出脚列 —— 显式角度时 origin
                # 随脚平移,对齐的是脚不是 origin)
                placed[n] = {"x": cur_x - eo[0], "y": cur_row_y - eo[1]}
                xo = eo
            vertical = abs(xo[1]) > 1e-9 or abs(eo[1]) > 1e-9
            _pz = _annot_text_zone(_master_short(specs[n]["master"]),
                                   placed[n]["x"], placed[n]["y"],
                                   0.0, 0.0, _angle_of(n))
            prev = (placed[n]["x"] + xo[0], _pz, vertical, n)
        if prev is not None:
            # 返回值与旧语义一致:链尾出脚沿排布方向的下一可用列
            _px, _pz, _pv, _pn = prev
            return _px + direction * g + (direction * 1.0 if _pv else 0.0)
        return cur_x

    # 1) 主干：端口间"水平引脚边"图上的最短路径（并列时端口名序小的优先）
    ports = sorted(n for n in rf if _master_short(specs[n]["master"]) in _AUTO_PORT_MASTERS)
    cand = []
    for i in range(len(ports)):
        for j in range(i + 1, len(ports)):
            stack = [(ports[i], (ports[i],))]
            while stack:
                cur, path = stack.pop()
                if cur == ports[j]:
                    cand.append((len(path), tuple(sorted((ports[i], ports[j]))),
                                 list(path)))
                    continue
                for nx in sorted(adj_side[cur], reverse=True):
                    if nx not in path:
                        stack.append((nx, path + (nx,)))
    cand.sort(key=lambda c: (c[0], c[1], c[2]))
    main = cand[0][2] if cand else _fallback_chain(rf, adj_side if any(adj_side.values())
                                                   else adj_all)

    def _match_sym_split():
        """识别 输入链→真 T 结→k 条支链(k≥2)→k 路输出 的对称分裂。

        全部支链必须从分叉元件的**同一个引脚节点**出发（导线直接搭 T 的
        拓扑，Wilkinson_5G9_ML/1G7 类）；MTEE 分支脚不在 adj_side 路径上，
        天然不会进来。返回布局计划或 None。
        """
        if __import__("os").environ.get("SYM_MATCH_DEBUG"):
            print(f"[cand] main={main} 候选 {len(cand)} 条: "
                  f"{[(len(c[2]), c[2]) for c in cand]}")
        for c in cand[1:]:
            p2 = c[2]
            if p2[0] != main[0] or p2[-1] == main[-1]:
                continue
            k = 0
            while k < min(len(main), len(p2)) - 1 and main[k] == p2[k]:
                k += 1
            if k < 1:
                continue
            s = main[k - 1]
            lbl = pin_of.get((s, main[k]))
            if lbl is None or not _junction(s, lbl):
                continue
            prefix = main[:k]
            # 同一前缀的全部端口路径 = 全部支链（main 的后缀算第一条）。
            # 按路径长度升序认领、元素不重叠才算新臂 —— 经由隔离电阻的
            # 长绕行路径与已认领臂共享元件，必须跳过，否则臂会互相交叠。
            arms = [main[k:]]
            taken = set(arms[0])
            for c2 in sorted(cand, key=lambda c: len(c[2])):
                q = c2[2]
                if q[:k] != prefix:
                    continue
                suf = list(q[k:])
                if not suf or any(e in taken for e in suf):
                    continue
                arms.append(suf)
                taken.update(suf)
            if len(arms) < 2 or len(arms) > 5:
                if __import__("os").environ.get("SYM_MATCH_DEBUG"):
                    print(f"[match] 拒绝 {len(arms)} 臂 (前缀 {prefix})")
                continue
            if __import__("os").environ.get("SYM_MATCH_DEBUG"):
                print(f"[match] 命中 k={len(arms)} 前缀={prefix} 臂={arms}")
            return {"prefix": prefix, "split": s, "arms": arms}
        return None

    def _sym_rows(k):
        """k 条支链的行分配（关于主干行对称）：k=2 → ±D；k=3 → 0,±D
        （一条直通主干行）；k=4 → +D,+2D,-D,-2D（两两成对，让 (0,1)(2,3)
        式的桥都落在相邻行）；k=5 → 0,+D,+2D,-D,-2D。更多的臂收益与
        可读性都变差，交回通用布局。"""
        d = _SYM_ROW_H
        if k == 2:
            return [d, -d]
        if k == 3:
            return [0.0, d, -d]
        if k == 4:
            return [d, 2 * d, -d, -2 * d]
        # k=5（含直通臂）存在顺序依赖的未解缺陷（A2 落 -7.5、全部叠 x=0，
        # 测试文件内稳定复现、单进程独立跑不可复现，2026-09-29 记录待查）
        # —— 查明前 k=5 交回通用布局（功能正确，图面次之）。
        raise RuntimeError(f"{k} 路分支暂不做对称布局（k=5 待查）")

    def _place_sym_split(plan):
        """对称一分 k：输入链放主干行，k 条支链按 _sym_rows 镜像铺开，
        隔离电阻竖放在所跨两臂出脚同列正中 —— 关于主干行轴对称的经典
        画法。摆不下（桥接引脚不同列、跨行桥等）就抛错，由调用方落回
        通用布局。"""
        arms = plan["arms"]
        rows = _sym_rows(len(arms))
        used = set(plan["prefix"]) | {n for a in arms for n in a}
        bridges = []
        while True:
            found = False
            for n in [n for n in rf if n not in used]:
                nbrs = adj_all[n] & used
                if len(nbrs) >= 2 and adj_all[n] <= used and all(
                        any(nbr in a for a in arms) for nbr in nbrs):
                    bridges.append(n)
                    used.add(n)
                    found = True
                    break
            if not found:
                break
        # 前端 shunt 支路收编（2026-09-29 用户规则：优先把对称部分对称，
        # 前端不对称的支路像图四挂在输入端）。剩余 rf 必须全是"挂在已用
        # 节点上的死胡同支路"（无端口、单锚点、单元件）才收编；锚点还须
        # 在输入链（prefix）上且不在分叉出脚列 —— 出脚列向下是臂下拉线，
        # 支路骑线。任一不满足就回通用布局。多元件竖直链的竖放文字互压
        # 没有实测常数，同样不收。
        front_arms = []
        remaining = set(rf) - used
        if remaining:
            seen = set()
            for n0 in sorted(remaining):
                if n0 in seen:
                    continue
                block, stack = set(), [n0]
                while stack:
                    c = stack.pop()
                    if c in block:
                        continue
                    block.add(c)
                    stack.extend(m for m in adj_all[c]
                                 if m in remaining and m not in block)
                seen |= block
                anchors = {(n, m, str(pin_of.get((n, m), "1")),
                            str(pin_of.get((m, n), "1")))
                           for n in block for m in adj_all[n] if m in used}
                if (len(block) != 1 or len(anchors) != 1
                        or any(_master_short(specs[n]["master"])
                               in _AUTO_PORT_MASTERS for n in block)):
                    raise RuntimeError(
                        "对称布局盖不全（剩余件不是单元件单锚点支路）")
                n, m, lbl_n, lbl_m = next(iter(anchors))
                if m in plan["prefix"]:
                    up = False
                else:
                    arm_idx = next((i for i, a in enumerate(arms)
                                    if m in a), None)
                    if arm_idx is None:
                        raise RuntimeError(
                            "对称布局盖不全（支路锚点不在输入链或臂上）")
                    # 上臂支路向上挂、下臂向下挂 —— 都朝臂外侧延伸，
                    # 不侵入行间信号区（2026-09-29 用户图：臂出脚的
                    # Cout 上挂/下挂 + 倒置地）。
                    up = rows[arm_idx] > 0
                front_arms.append((n, m, lbl_n, lbl_m, up))
        for n in plan["prefix"]:
            assigned.add(n)
            row_of[n] = 0
        _normalize_port_angle(plan["prefix"][0], "in")
        place_chain(plan["prefix"], 0.0, None, _SYM_GAP)
        for arm, r in zip(arms, rows):
            for n in arm:
                assigned.add(n)
                row_of[n] = 0
            _normalize_port_angle(arm[-1], "out")
            place_chain(arm, r, plan["split"], _SYM_GAP)
        arm_of = {n: i for i, a in enumerate(arms) for n in a}
        for br in bridges:
            na = sorted(m for m in adj_all[br]
                        if m in used and arm_of.get(m) is not None)[0]
            nb = sorted(m for m in adj_all[br]
                        if m in used and arm_of.get(m) != arm_of[na])[0]
            ra, rb = rows[arm_of[na]], rows[arm_of[nb]]
            r_hi, r_lo = max(ra, rb), min(ra, rb)
            # 跨行桥：竖线会视觉短路夹在中间的臂 —— 交回通用布局
            if any(r_lo < r < r_hi for r in rows):
                raise RuntimeError("桥跨中间臂")
            # 电阻文字块高约 2.6 格、骑在符号中心上方 —— 两臂行距扣掉
            # 文字带后余量不足（<3.5 格）就放不下，交回通用布局
            if abs(ra - rb) < 3.5:
                raise RuntimeError("行距放不下竖放桥接件（含文字带）")
            xa = pin_abs(na, pin_of.get((na, br), "1"))[0]
            xb = pin_abs(nb, pin_of.get((nb, br), "1"))[0]
            if abs(xa - xb) > 1e-6:
                raise RuntimeError("两臂桥接引脚不同列")
            # 竖放角度按实测引脚偏移算：使"连上臂的脚"旋转后朝上、另一脚
            # 朝下（R/C 实测默认竖放 (0,-1)，与旧横放假设的 90/270 不同）。
            # 角度错了连线器会绕行出矩形框（2026-09-29 实机渲染实测）。
            # origin 让两脚关于两臂行中点对称。
            lbl_up = str(pin_of.get((br, na), "1"))
            lbl_lo = str(pin_of.get((br, nb), "1"))
            ang = _shunt_angle(br, lbl_lo, -1)
            ry_up = _rot_off(*offs(br).get(lbl_up, (0.0, 0.0)), ang)[1]
            ry_lo = _rot_off(*offs(br).get(lbl_lo, (0.0, 0.0)), ang)[1]
            br_y = (ra + rb) / 2.0 - (ry_up + ry_lo) / 2.0
            placed[br] = {"x": xa, "y": br_y, "angle": ang}
            placed[br]["annot"] = (0.0, 1.0)
            placed[br]["_annot_pinned"] = True   # 对称分支刻意位:免回收
            assigned.add(br)
            row_of[br] = 0
            # 标注避让：处于较高行的桥接元件文字挂在符号下方，桥接竖线
            # 向下必穿字 —— 移到符号上方（v6 MTEE 同款）；较低行竖线向上
            # 离开文字，不用动。
            if kind(na) == "rf":
                placed[na]["annot"] = (-0.6, 1.4)
                placed[na]["_annot_pinned"] = True
        # 前端/臂上支路：挂锚点引脚延伸方向竖放（图四画法，C=Cin 类
        # 输入去耦与臂出脚 Cout）。下挂锚点列与分叉出脚列同列时正下方
        # 是臂下拉线，骑线 —— 拒收回通用。
        if front_arms:
            split_lbl = str(pin_of.get((plan["split"], arms[0][0]), "2"))
            split_x = pin_abs(plan["split"], split_lbl)[0]
            for n, m, lbl_n, lbl_m, up in front_arms:
                axy = pin_abs(m, lbl_m)
                if not up and abs(axy[0] - split_x) < 1e-9:
                    raise RuntimeError("对称布局盖不全（支路锚点在分叉出脚列）")
                # 竖放角度按实测引脚偏移算：地脚（非锚点脚）旋转后必须朝
                # 挂放方向延伸（下挂向下/上挂向上），否则地符号会骑在
                # 符号体上（R/C 实测默认竖放，硬编码 90/270 已不适用）。
                free_lbl = next(str(l) for l in offs(n)
                                if str(l) != str(lbl_n))
                ang = _shunt_angle(n, free_lbl, 1.0 if up else -1.0)
                ao = _rot_off(*offs(n).get(str(lbl_n), (0.0, 0.0)), ang)
                sgn = 1.0 if up else -1.0
                placed[n] = {"x": round(axy[0] - ao[0], 4),
                             "y": round(axy[1] - ao[1] + sgn * 1.0, 4),
                             "angle": ang}
                assigned.add(n)
                row_of[n] = 0
        # 分叉元件：向下的分叉竖线从出脚正下走，必穿它脚下的参数文字
        # （实测横向符号文字最右到 origin+1.4，越过出脚 0.4）——
        # 左移让出出脚列。2026-09-29 planner 路径复核：竖放文字区
        # [x+0.1, x+2.3] 无论左右移都躲不开（左压端口文字、右撞隔离
        # 电阻走廊+其竖线、上移压上臂文字）——保守文字区模型下该
        # 形态无纯净位。维持 -0.75（渲染验收通过的形态：竖线擦的是
        # 文字区右缘的空白带，实际字形不与线相交，ml_sym_final.png
        # 视觉验收记录在案）；几何门禁按"竖放文字×竖线"已知形态
        # 处理，不静默。
        s = plan["split"]
        lbl = str(pin_of.get((s, arms[0][0]), "2"))
        if kind(s) == "rf" and offs(s).get(lbl, (0.0, 0.0)) == (1.0, 0.0) \
                and not _angle_of(s):
            # -0.85:P1 文字右缘 0.78 与竖放文字区左缘 x+0.1+annot 需
            # 分离(0.03 互压实测),-0.85 → 左缘 0.65,净距 0.13
            placed[s]["annot"] = (-0.85, 0.0)
            placed[s]["_annot_pinned"] = True

    sym_plan = _match_sym_split() if main and cand else None
    sym_rows_used = []
    if sym_plan:
        try:
            _place_sym_split(sym_plan)
            sym_rows_used = _sym_rows(len(sym_plan["arms"]))
        except Exception:  # noqa: BLE001 — 识别了但摆不下就落回通用布局
            placed.clear()
            resolved_angle.clear()
            assigned.clear()
            row_of.clear()
            sym_plan = None

    if not sym_plan:
        assigned = set(main)
        row_of = {n: 0 for n in main}

        if main:
            _normalize_port_angle(main[0], "in")
            if main[-1] != main[0]:
                _normalize_port_angle(main[-1], "out")

        place_chain(main, 0.0, None)

        # 2) 分支：每个未分配端口，在"水平边"连通块里找已放锚点（经分支脚引入），
        #    锚点引脚正下方一行放整条支链
        branch_no = {}
        branch_tails = {}   # 行号 -> 该行支链元素（跨链件列对齐时整体平移用）

        def side_path_in(drop_node, p, prev):
            """BFS 树里 drop_node -> p 的路径（drop_node 在前）。"""
            tail = []
            node = drop_node
            while node is not None:
                tail.append(node)
                node = prev[node]
            return tail

        def _extend_tail(tail, anchor):
            """从 tail[-1] 沿未放邻居单向延伸成完整支链（度≤2 的链）。

            只取最短路径会把 CB-R2-R1 这类偏置链拆成单件逐个下挂，一行一个
            （CB_FM_Amp 基线实测的散乱来源）；沿"水平边"延伸到链尾。
            出口脚换网络即断（偏置链尾的上拉 R1.2→N__14、馈电 LC.2→N__14:
            这些对端由竖堆挂靠/独立支路处理,不该串进同一条链）。"""
            tail = list(tail)
            while True:
                cur = tail[-1]
                in_lbl = pin_of.get(
                    (cur, tail[-2] if len(tail) > 1 else anchor))
                nxts = [m for m in sorted(adj_side[cur])
                        if m not in assigned and m not in tail]
                if len(nxts) != 1:
                    break
                out_lbl = pin_of.get((cur, nxts[0]))
                if in_lbl is not None and out_lbl is not None and                         _node_find((cur, str(in_lbl))) !=                         _node_find((cur, str(out_lbl))):
                    break
                tail.append(nxts[0])
            return tail

        def _link_lbls(n, prev_n, next_n):
            lbls = set()
            for m in (prev_n, next_n):
                if m is not None:
                    lbls.add(str(pin_of.get((n, m), "1")))
            return lbls

        def _rough_box(n):
            """已放件的保守占位矩形（布局阶段近似，含引脚伸出与 pad）。
            竖放件窄高、横放件扁宽、多端件按符号圈;用于支路挂靠的净空判定。"""
            p = placed[n]
            x, y = p["x"], p["y"]
            if kind(n) == "gnd":
                # 实测(CB_FM_Amp_AGENT bbox 建档):x±0.125、270°体在 y[-0.25,0]
                a = int(_angle_of(n) or 0) % 360
                if a == 90:
                    return (x - 0.15, y - 0.05, x + 0.15, y + 0.3)
                return (x - 0.15, y - 0.3, x + 0.15, y + 0.05)
            if _multi_pin(n):
                return (x - 0.5, y - 0.65, x + 0.7, y + 0.65)
            o = offs(n)
            la, lb = _AUTO_AXIS_LABELS.get(
                _master_short(specs[n]["master"]), ("1", "2"))
            ax = _rot_off(o.get(lb, (1.0, 0.0))[0] - o.get(la, (0.0, 0.0))[0],
                          o.get(lb, (1.0, 0.0))[1] - o.get(la, (0.0, 0.0))[1],
                          _angle_of(n))
            if abs(ax[1]) > abs(ax[0]):      # 竖放
                lo, hi = min(y, y + ax[1]), max(y, y + ax[1])
                return (x - 0.35, lo - 0.15, x + 0.35, hi + 0.15)
            lo, hi = min(x, x + ax[0]), max(x, x + ax[0])
            return (lo - 0.15, y - 0.45, hi + 0.15, y + 0.45)

        def _box_hit(n, box):
            b = _rough_box(n)
            return not (b[2] < box[0] or box[2] < b[0]
                        or b[3] < box[1] or box[3] < b[1])

        # 动态行距（2026-09-30 紧凑化）：通用分支行的 y 不再是 -5.0×行号，
        # 而是按上一行实际内容向下的最大深度逐行累计 —— MTEE 参数 6 行
        # （文字下探 5.05）的下一行自动保持 ~5.3，纯 R/L/C 行收到 ~2.6。
        _row_y_cache = {0: 0.0}

        def _row_depth(r):
            """行 r 上已放内容相对行线的向下最大深度（含文字区与
            骑线支路的地脚延伸；地符号此时尚未落位，按引脚最低点 +0.8
            预留）。"""
            ry = _row_y_cache[r]
            depth = 0.0
            for e, p in placed.items():
                if row_of.get(e) != r:
                    continue
                b = _rough_box(e)
                depth = max(depth, ry - b[1])
                zn = _annot_text_zone(
                    _master_short(specs[e]["master"]) if e in specs else "VAR",
                    p["x"], p["y"], *(p.get("annot") or (0.0, 0.0)),
                    _angle_of(e))
                if zn is not None:
                    depth = max(depth, ry - zn[1])
                if gnd_mates.get(e):
                    p_low = min(p["y"] + _rot_off(dx, dy, _angle_of(e))[1]
                                for dx, dy in offs(e).values())
                    depth = max(depth, ry - p_low + 0.8)
            return depth

        def _row_y(r):
            r = int(r)
            got = _row_y_cache.get(r)
            if got is not None:
                return got
            y = _row_y(r - 1) - max(_ROW_GAP_MIN,
                                    _row_depth(r - 1) + _ROW_GAP_MARGIN)
            _row_y_cache[r] = y
            return y

        def _anchor_free_dir(anchor, jlbl):
            """锚点件 jlbl 脚的水平出线方向:脚偏移带 x 符号用之,否则与链
            出口脚相反（BFR106 b 脚在 origin,约定与 c 脚链向相反）。"""
            jx = _rot_off(*offs(anchor).get(str(jlbl), (0.0, 0.0)),
                          _angle_of(anchor))[0]
            if abs(jx) > 1e-9:
                return 1 if jx > 0 else -1
            for l in offs(anchor):
                if str(l) != str(jlbl):
                    ox = _rot_off(*offs(anchor)[l], _angle_of(anchor))[0]
                    if abs(ox) > 1e-9:
                        return -1 if ox > 0 else 1
            return -1

        def _dir_occupied(anchor, jxy, d):
            """锚点脚出线方向 d 上 1.5 格处是否被已放件占位(横延会撞)。"""
            probe = (jxy[0] + d * 2.5 - 0.15, jxy[1] - 0.45,
                     jxy[0] + d * 2.5 + 1.15, jxy[1] + 0.45)
            if d < 0:
                probe = (jxy[0] + d * 2.5 - 1.15, jxy[1] - 0.45,
                         jxy[0] + d * 2.5 + 0.15, jxy[1] + 0.45)
            return any(_box_hit(m, probe) for m in placed if m != anchor)

        def _place_short_branch(tail, anchor, drop_node):
            """短支路就近挂靠（2026-09-29 用户规则:偏置/去耦/负载/接地支路
            就近放置）——锚点是多端件（三端管）、支路无端口时:
              单件+对端地 → 沿锚点脚出线方向横放延伸 1.5 格（方向被占则竖放
              就近下垂列）;多件竖堆链（LC-馈电-VCC 类）→ 锚点脚旁净空列
              向上堆叠,尾件地脚朝上倒置地;骑线偏置链（CB-R2-R1,全件单链脚）
              → 沿锚点空闲水平向同轴延伸,全部 pin1 骑在锚点引脚行延长线上。"""
            jlbl = pin_of.get((anchor, drop_node), "1")
            jxy = pin_abs(anchor, jlbl)
            tail_lbls = [_link_lbls(n, tail[i - 1] if i else drop_node,
                                    tail[i + 1] if i + 1 < len(tail) else None)
                         for i, n in enumerate(tail)]
            if len(tail) == 1 \
                    and gnd_mates.get(tail[0]) \
                    and all(m == anchor for m in adj_all[tail[0]]):
                # 单件支路:rf 邻只有锚点、且带接地脚（RE/RLOAD/RD 类）——
                # 沿锚点脚出线方向横放延伸,方向被占则竖放就近下垂。
                # （2026-09-30 紧凑化：不再要求锚点是三端器件 —— 两脚元件
                # 的输出/级间节点同样是接地支路的归属地，FM_SC 的 RD 曾因
                # 锚点是普通电容而退到节点下方 5 格的整行分支。
                #  只收**两脚**元件：三端件（λ/4 偏置线 MTL1 类）的 pin3
                #  还挂着去耦链，当"单件+地"收进竖堆会把它和整条去耦链
                #  一起拖成竖排（放大器链案例实测），必须回通用分支行）
                if len(offs(tail[0])) != 2:
                    return False
                n = tail[0]
                if True:
                    entry = pin_of.get((n, anchor), "1")
                    free = next(l for l in offs(n) if str(l) != str(entry))
                    d = _anchor_free_dir(anchor, jlbl)
                    if not _dir_occupied(anchor, jxy, d):
                        resolved_angle[n] = _axis_angle(n, entry, free, d)
                        eo = _rot_off(*offs(n).get(str(entry), (0.0, 0.0)),
                                      _angle_of(n))
                        placed[n] = {"x": jxy[0] + d * 1.5 - eo[0],
                                     "y": jxy[1] - eo[1]}
                    else:
                        # 方向被链占用:竖放就近列。侧向按锚点几何选 —— 三端
                        # 器件且锚点引脚是**最上引脚**（其上无其他引脚,如
                        # BJT 的集电极 c 在顶角）时优先上挂:支路与母线同 net
                        # 走廊共享成 T 分支;下挂则会撞发射极馈线走廊
                        # （CB_FM_Amp 实测 RLOAD 下挂所有列都被 y=-1 走廊
                        # 拦死,A* 绕出 C 形）。其余（发射极这类下方引脚、
                        # 两脚元件的行线引脚）就近下垂。
                        others_above = any(
                            pin_abs(anchor, l2)[1] > jxy[1] + 0.3
                            for l2 in offs(anchor) if str(l2) != str(jlbl))
                        if _multi_pin(anchor):
                            side = 1.0 if not others_above else -1.0
                        else:
                            side = -1.0
                        resolved_angle[n] = _shunt_angle(n, free, side)
                        eo = _rot_off(*offs(n).get(str(entry), (0.0, 0.0)),
                                      _angle_of(n))
                        # 列优先 ±1.0:与走线计划的拐点列重合,支路
                        # 一拐直进、不产生与母线平行的水平尾段。
                        # 两脚锚点只给出线方向一侧的列 —— 反侧的横进段
                        # 会从锚点出脚折回、穿过锚点自己的符号体
                        if _multi_pin(anchor):
                            col_cands = [jxy[0] + 1.0, jxy[0] - 1.0,
                                         jxy[0] + 1.5, jxy[0] - 1.5,
                                         jxy[0] + 2.5, jxy[0] - 2.5,
                                         jxy[0] + 3.5]
                        else:
                            col_cands = [jxy[0] + d * w
                                         for w in (1.0, 1.5, 2.0, 2.5,
                                                   3.0, 3.5)]
                        row_y = jxy[1] + side * 2.0

                        def _col_wire_hits(c):
                            """连接线走廊(横 jxy→c 于锚脚行 + 竖 c→行)
                            穿已放件或其他引脚连接的可能 L 路径即弃。"""
                            for seg in (((jxy[0], jxy[1]), (c, jxy[1])),
                                        ((c, jxy[1]), (c, row_y - eo[1]))):
                                for m in placed:
                                    if m != anchor and _segment_hits_box(
                                            seg[0], seg[1], _rough_box(m)):
                                        return True
                                # 锚点其他引脚→已放对端的两种 L 变体
                                for m2 in adj_all[anchor]:
                                    l2 = str(pin_of.get((anchor, m2), "1"))
                                    if l2 == str(jlbl) or m2 not in placed:
                                        continue
                                    ap2 = pin_abs(anchor, l2)
                                    pp2 = pin_abs(
                                        m2, pin_of.get((m2, anchor), "1"))
                                    for wp2 in ([(pp2[0], ap2[1])],
                                                [(ap2[0], pp2[1])]):
                                        opts = [ap2] + wp2 + [pp2]
                                        if any(_segment_hits_route(
                                                seg[0], seg[1], c2, d2,
                                                {jxy, (c, row_y - eo[1])},
                                                {opts[0], opts[-1]})
                                               for c2, d2 in
                                               zip(opts, opts[1:])):
                                            return True
                            return False

                        box_y = row_y + (eo[1] if side < 0 else 0.0)
                        box = (box_y - 1.3 if side < 0 else row_y - 0.3,
                               box_y + 0.3 if side < 0 else row_y + 1.3)
                        col_x = next(
                            (c for c in col_cands
                             if not any(_box_hit(m, (c - 0.35, box[0],
                                                     c + 0.35, box[1]))
                                        for m in placed if m != anchor)
                             and not _col_wire_hits(c)),
                            jxy[0] + 1.5)
                        placed[n] = {"x": col_x - eo[0],
                                     "y": jxy[1] + side * 2.0}
                    assigned.add(n)
                    row_of[n] = row_of.get(anchor, 0)
                    return True
            if len(tail) >= 2 and all(len(s) == 1 for s in tail_lbls):
                # 骑线链水平延伸:全部 pin1 骑在同一行线上（偏置链）。
                # 行侧按走廊净空选:锚点脚上/下 2 格的带状区哪侧撞已放件
                # 少就用哪侧 —— 三端管的基极脚下方常被发射极馈线占用,
                # 硬下挂会把连接线挤成跨网交叉（CB_FM_Amp conn7 实测:
                # 下挂必穿 CDCIN→Q1.3 的 L 线,上挂则连接线纯竖直 0 拐弯）。
                d = _anchor_free_dir(anchor, jlbl)
                width = 1.0 + 1.9 * len(tail)

                def _other_wires():
                    """锚点其他引脚→已放对端的"可能 L 路径"列表。
                    变体剔除:穿过锚点自身盒内部、压锚点其他引脚点的 L
                    不可能被走线计划采纳（blocked() 会拒）,不算数。"""
                    out_w = []
                    anchor_box = _rough_box(anchor)
                    others = {l: pin_abs(anchor, l) for l in offs(anchor)
                              if str(l) != str(jlbl)}
                    for m in adj_all[anchor]:
                        lbl = str(pin_of.get((anchor, m), "1"))
                        if lbl == str(jlbl) or m not in placed:
                            continue
                        ap = pin_abs(anchor, lbl)
                        pp = pin_abs(m, pin_of.get((m, anchor), "1"))
                        for wp in ([(pp[0], ap[1])], [(ap[0], pp[1])]):
                            pts = [ap] + wp + [pp]
                            bad = False
                            for s1, s2 in zip(pts, pts[1:]):
                                if _segment_hits_box(s1, s2, anchor_box,
                                                     strict=True):
                                    bad = True
                                    break
                                for q in others.values():
                                    qx = min(s1[0], s2[0]) - 1e-9 <= q[0]                                         <= max(s1[0], s2[0]) + 1e-9
                                    qy = min(s1[1], s2[1]) - 1e-9 <= q[1]                                         <= max(s1[1], s2[1]) + 1e-9
                                    on = (abs(s1[1] - s2[1]) < 1e-9
                                          and abs(q[1] - s1[1]) < 1e-9 and qx)                                          or (abs(s1[0] - s2[0]) < 1e-9
                                             and abs(q[0] - s1[0]) < 1e-9 and qy)
                                    if on and q != ap:
                                        bad = True
                                        break
                                if bad:
                                    break
                            if not bad:
                                out_w.append(pts)
                    return out_w

                head_x = jxy[0] + d * gap

                def _side_hits(side):
                    row_y = jxy[1] + side * 2.0
                    band = (min(head_x, jxy[0]) - 0.7, row_y - 0.7,
                            max(head_x, jxy[0]) + 0.7, row_y + 0.7)
                    n = sum(1 for m in placed if m != anchor
                            and _box_hit(m, band))
                    # 连接器两种 L 变体与锚点其他引脚的可能 L 路径交叉数
                    for wp in ([(jxy[0], row_y)], [(head_x, jxy[1])]):
                        cpts = [jxy] + wp + [(head_x, row_y)]
                        for s1, s2 in zip(cpts, cpts[1:]):
                            for opts in _other_wires():
                                if any(_segment_hits_route(
                                        s1, s2, c, d2,
                                        {jxy, (head_x, row_y)},
                                        {opts[0], opts[-1]})
                                       for c, d2 in zip(opts, opts[1:])):
                                    n += 1
                    return n

                down_hits = _side_hits(-1.0)
                up_hits = _side_hits(1.0)
                side = -1.0 if down_hits <= up_hits else 1.0
                row_y2 = jxy[1] + side * 2.0
                place_chain(tail, row_y2, anchor, direction=d,
                            shunt_up=(side > 0))
                for n in tail:
                    assigned.add(n)
                    row_of[n] = row_of.get(anchor, 0)
                return True
            # 多件竖堆链（LC-VCC 馈电类）:锚点脚上方净空列向上堆叠,
            # 尾件地脚朝上倒置地;自由脚对端 rf 未放时继续收进同列
            #（R1 的上拉 VCC 这类跨网络对端,由 _free_node_lbl 节点比对把关）
            # 守卫（2026-09-30）:竖堆是"接地馈电链"形态,链上至少一件带地;
            # 无地的三端单件（λ/4 偏置线类）交回通用分支行,否则会被连
            # 同去耦链一起拖成竖排（放大器链案例 MTL1+C3 实测）
            if not any(gnd_mates.get(m) for m in tail):
                return False
            def _free_node_lbl(chain_list, cur):
                i = chain_list.index(cur)
                in_lbl = pin_of.get(
                    (cur, chain_list[i - 1] if i else anchor))
                for l in offs(cur):
                    if str(l) != str(in_lbl):
                        return (cur, str(l))
                return (cur, str(in_lbl))
            tail = list(tail)
            while True:
                cur = tail[-1]
                grew = False
                for m in sorted(adj_all[cur]):
                    if m in assigned or m in tail or kind(m) != "rf":
                        continue
                    lbl_cur = str(pin_of.get((cur, m), "1"))
                    free_node = _free_node_lbl(tail, cur)
                    if _node_find((cur, lbl_cur)) != _node_find(free_node):
                        continue
                    tail.append(m)
                    grew = True
                    break
                if not grew:
                    break
            # 列优先贴锚点脚(+2.0):竖堆首件骑在 junction 母线上,离得越近
            # 分支线越短、越不容易与母线上的串联件形成平行出线;过远的列
            # 会被走线计划判成"同节点两条平行横线"（用户规则 2 明令禁止）
            col_cands = [jxy[0] + 2.0, jxy[0] + 3.5, jxy[0] + 4.5,
                         jxy[0] - 2.0, jxy[0] - 3.5, jxy[0] - 4.5]
            top_y = jxy[1] + 2.0 * len(tail) + 2.0
            col_x = next((c for c in col_cands
                          if not any(_box_hit(m, (c - 0.35, jxy[1] + 0.7,
                                                  c + 0.35, top_y))
                                     for m in placed if m != anchor)),
                         jxy[0] + 3.5)
            y = jxy[1] + 1.0
            for i, n in enumerate(tail):
                entry = pin_of.get((n, tail[i - 1])) if i else \
                    pin_of.get((n, anchor))
                exit_ = pin_of.get((n, tail[i + 1])) if i + 1 < len(tail) \
                    else None
                lbls = {str(l) for l in (entry, exit_) if l is not None}
                if specs[n].get("angle") is None and n not in resolved_angle \
                        and not _multi_pin(n):
                    if len(lbls) == 2:
                        # 竖堆串联件:entry 在下 exit 在上(LC 馈电类)
                        resolved_angle[n] = _stack_angle(n, entry, exit_)
                    else:
                        resolved_angle[n] = _shunt_angle(
                            n, next(l for l in offs(n)
                                    if str(l) not in lbls), 1)
                eo = _rot_off(*offs(n).get(str(entry), (0.0, 0.0)),
                              _angle_of(n)) if entry else (0.0, 0.0)
                placed[n] = {"x": col_x - eo[0], "y": y - eo[1]}
                no_ = _rot_off(*offs(n).get(str(exit_), (0.0, 0.0)),
                               _angle_of(n)) if exit_ else (0.0, 1.0)
                y = placed[n]["y"] + no_[1] + 2.0
                assigned.add(n)
                row_of[n] = row_of.get(anchor, 0)
            return True

        def _free_mate_unplaced(tail, anchor):
            """链上元件的非链脚是否连着未放的 rf 元件（如偏置链尾 R1 的上拉
            VCC 还没放）——是则推迟本轮，等对端落位后自由脚朝向才判得准。"""
            chain_set = set(tail) | {anchor}
            for n in tail:
                lbls = _link_lbls(
                    n, tail[tail.index(n) - 1] if tail.index(n) else None,
                    tail[tail.index(n) + 1]
                    if tail.index(n) + 1 < len(tail) else None)
                if len(lbls) == 2:
                    continue
                for m in adj_all[n]:
                    if m not in chain_set and kind(m) == "rf" \
                            and m not in assigned:
                        # 对端是桥接件且它的其他邻居都已放 → 它自己会以
                        # 已放区为锚,不算依赖（T 型功分器的隔离电阻:
                        # 不豁免会把下支路和桥一起拖进死锁推迟）
                        others = [x for x in adj_all[m] if x != n]
                        if others and all(x in assigned for x in others):
                            continue
                        return True
            return False

        # 1.5) 电源母线 + 同节点接地支路组（2026-09-29 CE_FM_Amp 用户手工
        #      参考图规则，泛化到任意多支路电路）：
        #      * 电源母线（rail bus）：net 含直流源(V_DC/I_DC)引脚、无端口、
        #        成员>=3、非源成员都是两脚串联件且各自另一脚的网络里有已放
        #        锚点 —— 各成员在自己锚点引脚行上方竖放（近端脚 +0.75），
        #        顶端齐平接一条水平母线（最高锚点行 +2.125），源横放母线
        #        右端、负端朝外接横地。替代旧的"LC-VCC-R1 竖直堆一列"
        #        （堆叠把 R1 推到 y=6，基极馈线被迫 6.5 格长绕行的根因）。
        #      * 接地支路组（shunt group）：同一节点上的全部"单件+地"元件
        #        （去耦电容/偏置下分压电阻/发射极电阻/负载电阻类）并排
        #        挂在节点行线一侧 —— 首列锚点引脚 +0.55（远离符号体方向），
        #        列距 1.0，元件节点脚骑在行线上（行线在该点顶点化），
        #        地脚朝挂放侧延伸、地符号随脚。向下优先；下方走廊被其他
        #        网络的行线截断时改向上挂（倒置地）。替代旧的"一条支路
        #        独占一行 row_h=5"（输出并联 COUT/RD 被推到 y=-5/-10 的根因）。
        def _node_groups():
            groups = {}
            for key in node_parent:
                groups.setdefault(_node_find(key), []).append(key)
            return groups

        def _pin_bound_box(n):
            """多端器件（三端管）的引脚包围盒 ±0.15 —— _rough_box 对多端件
            外扩 ±0.5/0.65 过肥，会把紧贴晶体管边缘的支路列整片误拒
            （CE_FM_Amp 的 R2 正下挂被拒实测）。两脚件仍用 _rough_box。"""
            if not _multi_pin(n):
                return _rough_box(n)
            xs, ys = [], []
            for l, (dx, dy) in offs(n).items():
                rx, ry = _rot_off(dx, dy, _angle_of(n))
                xs.append(placed[n]["x"] + rx)
                ys.append(placed[n]["y"] + ry)
            return (min(xs) - 0.15, min(ys) - 0.15,
                    max(xs) + 0.15, max(ys) + 0.15)

        def _corridor_clear(x_c, y_lo, y_hi, root, anchor, skip=None):
            """竖直走廊 x=x_c, y in [y_lo,y_hi]：不穿任何已放件（锚点按
            引脚包围盒、其余按粗占位盒；skip=正在试摆的元件，自身落位区
            不算障碍——先放置后查走廊会自锁），且不被其他网络的行线跨越。"""
            lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
            for m in placed:
                if m == skip:
                    continue
                if m == anchor:
                    b = _pin_bound_box(m)
                    if not (b[0] - 1e-9 <= x_c <= b[2] + 1e-9):
                        continue
                else:
                    b = _rough_box(m)
                if b[0] - 1e-9 < x_c < b[2] + 1e-9 \
                        and b[1] - 1e-9 < hi and lo < b[3] + 1e-9:
                    return False
            return not _row_wire_spans(root, lo, hi, x_c, x_c + 1e-6)

        def _away_dir(anchor, jlbl):
            """锚点引脚水平出线方向：远离符号体。引脚在符号左缘 → -1、
            右缘 → +1；中间按两侧净空选（右侧优先，与读序一致）。"""
            box = _pin_bound_box(anchor)
            jxy = pin_abs(anchor, jlbl)
            if jxy[0] <= box[0] + 1e-6:
                return -1
            if jxy[0] >= box[2] - 1e-6:
                return 1
            for d in (1, -1):
                probe = (jxy[0] + d * _RAIL_COL_DX - 0.35, jxy[1] - 0.45,
                         jxy[0] + d * _RAIL_COL_DX + 0.35, jxy[1] + 0.45)
                if not any(_box_hit(m, probe) for m in placed if m != anchor):
                    return d
            return 1

        def _placed_anchor_of(root, exclude=()):
            """节点上最合适的已放锚点引脚 (inst, lbl)：优先三端器件
            （晶体管的 b/c/e 是支路的天然枢纽），否则取最左已放件。"""
            cands = [k for k in _node_groups().get(root, [])
                     if k[0] in placed and k[0] not in exclude]
            if not cands:
                return None
            multi = [k for k in cands if _multi_pin(k[0])]
            if multi:
                return multi[0]
            return min(cands, key=lambda k: pin_abs(k[0], k[1])[0])

        def _row_wire_spans(root_skips, y_lo, y_hi, x1, x2):
            """其他网络的行线是否截断走廊 x∈[x1,x2]、y∈[y_lo,y_hi]：
            同一 net 的两个已放引脚 y 一致（<0.45 视作同行）、行高落在
            走廊 y 范围内（端点 ±0.05 豁免——走廊起点常压在自己网络的
            行线上）、x 跨越走廊区间，即存在截断行线。root_skips 是豁免
            网络（支路自身的网络与远端网络：立柱落回自己的行线是目标
            形态，不是交叉）。"""
            if not isinstance(root_skips, (set, frozenset, tuple, list)):
                root_skips = {root_skips}
            lo, hi = min(x1, x2), max(x1, x2)
            y_in_lo = min(y_lo, y_hi) + 0.05
            y_in_hi = max(y_lo, y_hi) - 0.05
            by_root = {}
            for key in node_parent:
                if key[0] not in placed:
                    continue
                by_root.setdefault(_node_find(key), []).append(key)
            for root2, keys in by_root.items():
                if root2 in root_skips:
                    continue
                pts = [pin_abs(k[0], k[1]) for k in keys]
                for i in range(len(pts)):
                    for j in range(i + 1, len(pts)):
                        if abs(pts[i][1] - pts[j][1]) < 0.45:
                            ry = (pts[i][1] + pts[j][1]) / 2.0
                            if not (y_in_lo < ry < y_in_hi):
                                continue
                            a, b = sorted((pts[i][0], pts[j][0]))
                            if a < lo < b or a < hi < b or (a >= lo and b <= hi
                                                            and a < b):
                                return True
            return False

        _rail_done = set()
        for root, members in _node_groups().items():
            src_pins = [k for k in members
                        if _master_short(specs[k[0]]["master"])
                        in _AUTO_DC_SRC_MASTERS]
            # 成员 >=2（源脚 + 至少一个轨成员）即触发 —— R12：射随器
            # 电源轨常只有一颗馈电电感；旧值 >=3 使单成员轨退回竖堆
            if len(members) < 2 or len(src_pins) != 1:
                continue
            if any(_master_short(specs[k[0]]["master"]) in _AUTO_PORT_MASTERS
                   for k in members):
                continue
            src_pin = src_pins[0]
            if src_pin[0] in assigned:
                continue
            elems = []
            ok = True
            for k in members:
                n = k[0]
                if n == src_pin[0]:
                    continue
                if n in assigned:
                    ok = False
                    break
                if n not in rf_set or _multi_pin(n):
                    continue
                other = next((str(l) for l in offs(n)
                              if str(l) != str(k[1])), None)
                if other is None:
                    continue
                far_root = _node_find((n, other))
                if far_root == root:
                    continue
                anchor = _placed_anchor_of(far_root, exclude=(n,))
                if anchor is None:
                    ok = False
                    break
                elems.append((n, str(k[1]), other, anchor))
            if not ok or not elems:
                # 成员数 >=1 即触发（R12：射随器电源轨常只有一颗馈电
                # 电感——单成员时母线=源-元件直连，竖挂+源顶置仍是
                # 经典画法；旧值 >=2 使单成员轨退回竖堆且落行下方）
                continue
            # 摆放：成员竖放（锚点侧脚 +0.75、母线侧脚 +1.75），列取
            # "锚点引脚行上方走廊净空"的方向；母线齐平于最高锚点行
            # + _RAIL_BUS_DY；源横放母线右端（+极压母线、-极朝外）。
            try:
                anchors_y = [pin_abs(a[0], a[1])[1] for a in
                             (e[3] for e in elems)]
                bus_y = max(anchors_y) + _RAIL_BUS_DY
                cols = []
                for n, rail_lbl, far_lbl, anchor in elems:
                    ax, ay = pin_abs(anchor[0], anchor[1])
                    far_root = _node_find((n, far_lbl))
                    col = None
                    for d in (1, -1):
                        c = round(ax + d * _RAIL_COL_DX, 4)
                        if _corridor_clear(c, ay, bus_y,
                                           {root, far_root}, anchor[0]):
                            col = c
                            break
                    if col is None:
                        raise RuntimeError("rail col blocked")
                    resolved_angle[n] = _shunt_angle(n, far_lbl, -1)
                    fo = _rot_off(*offs(n).get(str(far_lbl), (0.0, 0.0)),
                                  _angle_of(n))
                    placed[n] = {"x": round(col - fo[0], 4),
                                 "y": round(ay + 0.75 - fo[1], 4)}
                    assigned.add(n)
                    row_of[n] = 0
                    _rail_done.add(n)
                    cols.append(col)
                # 源本体摆位占用检查 + 右移重试（R7，2026-09-30）：
                # vccx=max(cols)+1.0 是盲位 —— 源符号 1.0 宽、文字右伸
                # 1.4，压到已放件时整个 rail pass 会带着坏坐标走下去
                vccx = None
                for off in (1.0, 1.5, 2.0, 2.5, 3.0, -0.5, -1.0):
                    cand = round(max(cols) + off, 4)
                    placed[src_pin[0]] = {"x": cand, "y": round(bus_y, 4),
                                          "angle": 0}
                    bsrc = _rough_box(src_pin[0])
                    if any(_box_hit(m, bsrc) for m in placed
                           if m != src_pin[0]):
                        placed.pop(src_pin[0], None)
                        continue
                    vccx = cand
                    break
                if vccx is None:
                    raise RuntimeError("rail src blocked")
                assigned.add(src_pin[0])
                row_of[src_pin[0]] = 0
                _rail_done.add(src_pin[0])
            except Exception:  # noqa: BLE001 — 摆不出就交回旧逻辑
                for n in list(_rail_done):
                    placed.pop(n, None)
                    assigned.discard(n)
                _rail_done.clear()
        # 接地支路组：按节点归组"单件+地"的未放元件
        shunt_by_node = {}
        for n in rf:
            if n in assigned or _multi_pin(n) or not gnd_mates.get(n):
                continue
            if _master_short(specs[n]["master"]) in _AUTO_PORT_MASTERS:
                continue
            gm = next(iter(gnd_mates[n]))
            # 一个地符号被多件共享也照常收编（2026-09-30 紧凑化：旧规则
            # 整组跳过、元件落到远端分支行；现在元件就近成组，共享的
            # 地符号由步骤 4 按全部伙伴引脚的中点挂置）—— 地脚标签从
            # partner 表取：partner[gm] = (n, n 的脚, gm 的脚)
            gp = partner.get(gm)
            g_lbl0 = str(gp[1]) if gp and gp[0] == n else None
            if g_lbl0 is None:
                continue
            node_lbl = next((str(l) for l in offs(n) if str(l) != g_lbl0),
                            None)
            if node_lbl is None:
                continue
            shunt_by_node.setdefault(_node_find((n, node_lbl)), []).append(n)
        for root, shunts in sorted(shunt_by_node.items(),
                                   key=lambda kv: str(kv[1][0])):
            anchor = _placed_anchor_of(root)
            if anchor is None:
                continue
            members = _node_groups().get(root, [])
            if len(members) < 2:
                continue
            try:
                ax, ay = pin_abs(anchor[0], anchor[1])
                # 节点行线跨度：已放引脚的 x 极值（决定支路列的活动范围，
                # 外扩 0.2 容纳锚点旁的近距列；单锚点时只按远离符号体方向）
                pins_x = [pin_abs(k[0], k[1])[0] for k in members
                          if k[0] in placed]
                lo_x = min(pins_x) if len(pins_x) > 1 else None
                hi_x = max(pins_x) if len(pins_x) > 1 else None
                d = _away_dir(anchor[0], anchor[1])
                # 挂放侧：向下优先；下方走廊被其他网络的行线截断则上挂
                side = -1.0
                span_probe = ax + d * (_RAIL_COL_DX + _SHUNT_COL_STEP
                                       * (len(shunts) - 1))
                if _row_wire_spans({root}, ay - 1.9, ay - 0.1,
                                   ax, span_probe):
                    side = 1.0
                for i, n in enumerate(sorted(shunts)):
                    if n in assigned:
                        continue   # 已被第一轮支路组或步骤 2 摆放
                    gm = next(iter(gnd_mates[n]))
                    gp = partner.get(gm)
                    g_lbl = str(gp[1]) if gp and gp[0] == n else None
                    if g_lbl is None:
                        continue
                    node_lbl = next(str(l) for l in offs(n)
                                    if str(l) != g_lbl)
                    col = None
                    # 候选列两个方向都试（锚点在节点行线端点时，远离符号体
                    # 的一侧可能整个落在行线跨度外，如基极节点在最右端）
                    # 列生成:先扫行线跨度内的**空槽**(与全部已放同组
                    # 列距 >=0.72 的位置,0.05 粒度)—— 手工图 RD 落在
                    # COUT 与 LOUT 之间的窄槽;没有空槽再按步进列外扩。
                    # 竖放 R 的参数文字右伸 ~1.75 格,列距不足会让邻列
                    # 地线穿文字(fuzz 例48 实测)。
                    _grp_cols = sorted({round(placed[m2]["x"], 4)
                                        for m2 in shunts if m2 in placed
                                        and m2 != n})
                    _ms = _master_short(specs[n]["master"])
                    _step = 2.0 if _ms == "R" else _SHUNT_COL_STEP
                    _extras = [k * 0.05 for k in range(-16, 17)]

                    def _zone_abs(name, x, y):
                        return _annot_text_zone(
                            _master_short(specs[name]["master"]),
                            x, y,
                            *(placed.get(name, {}).get("annot") or (0.0, 0.0)),
                            _angle_of(name))

                    def _try_place(c):
                        """试把 n 放到列 c:占位 -> 跨度界/符号盒/文字区/走廊
                        检查,全过返回 c,否则回滚返回 None。"""
                        if lo_x is not None and not (lo_x - 0.2 <= c
                                                     <= hi_x + 0.2):
                            return None
                        resolved_angle[n] = _shunt_angle(n, g_lbl, side)
                        no = _rot_off(
                            *offs(n).get(node_lbl, (0.0, 0.0)),
                            _angle_of(n))
                        placed[n] = {"x": round(c - no[0], 4),
                                     "y": round(ay - no[1], 4)}
                        box = _rough_box(n)
                        zn = None
                        hit = False
                        for m in placed:
                            if m == n:
                                continue
                            # 锚点按引脚包围盒查（真实符号体）——完全豁免
                            # 会让支路元件叠进晶体管身体里（CB 基极组实测）
                            b_m = _pin_bound_box(m)                                 if m == anchor[0] else _rough_box(m)
                            if not (b_m[2] < box[0] or box[2] < b_m[0]
                                    or b_m[3] < box[1] or box[3] < b_m[1]):
                                hit = True
                                break
                        if not hit and lo_x is not None:
                            # 跨节点邻列的文字区互斥 —— 仅限同节点行线
                            # 跨度内的候选（fuzz 例45 S1 实测:两支路列
                            # 距 0.7 时邻列地线穿文字）。发射极这类单锚
                            # 组（lo_x=None）不查 —— 查了会把 RE1 全拒
                            # 推到通用分支行远端（CE_FM_Amp_AGENT 实测）
                            zn = _zone_abs(n, placed[n]["x"], placed[n]["y"])
                            if zn is not None:
                                _grp = set(shunts)
                                for m2 in placed:
                                    # 锚点不再豁免（2026-09-30）：支路文字
                                    # 与锚点器件（三端管）文字互压同样要
                                    # 换列 —— CE_FM_Amp_AGENT 紧凑间距下
                                    # R2 文字压 Q1 名字实测；锚点符号盒
                                    # 仍按引脚包围盒查（上方 box 检查）
                                    if m2 == n or m2 in _grp:
                                        continue
                                    zm = _zone_abs(m2, placed[m2]["x"],
                                                   placed[m2]["y"])
                                    if zm is None:
                                        continue
                                    if not (zn[2] < zm[0] or zm[2] < zn[0]
                                            or zn[3] < zm[1]
                                            or zm[3] < zn[1]):
                                        hit = True
                                        break
                        if not hit and zn is not None:
                            # 默认文字区不得包含**离行引脚**（2026-09-30）:
                            # 引脚是未来导线的端点,区里含别行引脚=文字区
                            # 被别行行线深穿（CE 实测 R2 挤进基极-发射极
                            # 行距 0.5 的窄缝,文字只能跳到行另一侧）。
                            # 同行引脚(y≈ay)豁免 —— 行线擦文字上缘是
                            # 手工图常态,由避让的深穿口径统一处理。
                            for m2 in placed:
                                if m2 == n:
                                    continue
                                for lbl2 in offs(m2):
                                    px2, py2 = pin_abs(m2, str(lbl2))
                                    if abs(py2 - ay) <= 0.3:
                                        continue
                                    if (zn[0] - 0.15 <= px2 <= zn[2] + 0.15
                                            and zn[1] - 0.15 <= py2
                                            <= zn[3] + 0.15):
                                        hit = True
                                        break
                                if hit:
                                    break
                        clear = _corridor_clear(
                            c, ay, ay + side * 2.1, {root},
                            anchor[0], skip=n)
                        if not hit and clear:
                            return c
                        placed.pop(n, None)
                        resolved_angle.pop(n, None)
                        return None

                    def _scan_slots():
                        _slot_cands = []
                        if _grp_cols and lo_x is not None:
                            c0 = round(lo_x + 0.05, 2)
                            while c0 <= hi_x - 0.05:
                                if all(abs(c0 - gc) >= 0.72
                                       for gc in _grp_cols):
                                    _slot_cands.append(round(c0, 4))
                                c0 = round(c0 + 0.05, 2)
                        for dd in (d, -d):
                            # 加密网格:支路列要落进邻符号之间的窄槽
                            # (CE 手工图 RD 落 COUT 与 LOUT 间 0.75 槽),
                            # 0.05 粒度 × 足够范围;列距仍由 _step 分档
                            for extra in _extras:
                                if _slot_cands:
                                    if len(_slot_cands) <= i:
                                        break
                                    c = _slot_cands[i]
                                else:
                                    c = round(ax + dd * (_RAIL_COL_DX
                                                         + _step * i + extra),
                                              4)
                                got = _try_place(c)
                                if got is not None:
                                    return got
                            if _slot_cands and len(_slot_cands) <= i:
                                break
                        return None

                    def _shift_segment_and_rescan():
                        """跨度让位（2026-09-30 紧凑化泛化）：同组支路放不进
                        节点行线跨度时，把跨度端点**外侧的整段同行链**（沿
                        邻接收集的串联件、其骑线支路与端口）整体向外平移
                        腾出缺口，而不是只挪一个端点元件 —— 端点元件外侧常
                        被下游元件顶住（FM_SC 输出节点 RD 被 L3 顶住、只能
                        落到远端分支行的实测根因）。平移 0.05 步进、上限
                        2.0 格，每步对被移元件做符号盒 + 文字区碰撞校验；
                        地符号此时尚未落位、随宿主在步骤 4 自动跟随。
                        平移后重扫列。返回列或 None。"""
                        nonlocal lo_x, hi_x
                        if lo_x is None:
                            return None
                        resolved_angle[n] = _shunt_angle(n, g_lbl, side)
                        anchor_row = row_of.get(anchor[0], 0)

                        def _pin_xs(m2):
                            return [pin_abs(m2, str(l))[0] for l in offs(m2)]

                        def _collect_seg(direction):
                            """跨度外侧的同行链段：从跨度端点元件沿邻接走，
                            只收"全部引脚都在界外同侧"的已放件 —— 链在
                            与上游连接处自然截断（电源轨、上游链不会被
                            拖走）。"""
                            if direction == "hi":
                                bound = hi_x
                            else:
                                bound = lo_x
                            seed, seed_key = None, None
                            for k2 in members:
                                m2 = k2[0]
                                if (m2 not in placed or m2 == n
                                        or m2 in shunts or m2 == anchor[0]):
                                    continue
                                xs = _pin_xs(m2)
                                if direction == "hi":
                                    if min(xs) < bound - 1e-6:
                                        continue
                                    key = max(xs) - bound
                                else:
                                    if max(xs) > bound + 1e-6:
                                        continue
                                    key = bound - min(xs)
                                if seed is None or key < seed_key:
                                    seed, seed_key = m2, key
                            if seed is None:
                                return []
                            seg, stack = set(), [seed]
                            while stack:
                                cur = stack.pop()
                                if cur in seg:
                                    continue
                                seg.add(cur)
                                for m2 in sorted(adj_all[cur]):
                                    if (m2 not in placed or m2 in seg
                                            or m2 == n or m2 == anchor[0]):
                                        continue
                                    if row_of.get(m2) != anchor_row:
                                        continue
                                    xs = _pin_xs(m2)
                                    if direction == "hi" and                                             min(xs) >= bound - 1e-6:
                                        stack.append(m2)
                                    elif direction == "lo" and                                             max(xs) <= bound + 1e-6:
                                        stack.append(m2)
                            return seg

                        def _seg_clear(seg):
                            """平移后的碰撞校验：被移件的符号盒、文字区不得
                            撞任何未移动件（段内相对关系不变，不互查）。"""
                            for m2 in seg:
                                bx = _rough_box(m2)
                                for m3 in placed:
                                    if m3 in seg or m3 == n:
                                        continue
                                    b3 = (_pin_bound_box(m3)
                                          if m3 == anchor[0]
                                          else _rough_box(m3))
                                    if not (b3[2] < bx[0] or bx[2] < b3[0]
                                            or b3[3] < bx[1] or bx[3] < b3[1]):
                                        return False
                                zm = _zone_abs(m2, placed[m2]["x"],
                                               placed[m2]["y"])
                                if zm is None:
                                    continue
                                for m3 in placed:
                                    if m3 in seg or m3 == n:
                                        continue
                                    z3 = _zone_abs(m3, placed[m3]["x"],
                                                   placed[m3]["y"])
                                    if z3 is None:
                                        continue
                                    if not (zm[2] < z3[0] or z3[2] < zm[0]
                                            or zm[3] < z3[1] or z3[3] < zm[1]):
                                        return False
                            return True

                        for direction in ("hi", "lo"):
                            seg = _collect_seg(direction)
                            if not seg:
                                continue
                            snap = {m2: placed[m2]["x"] for m2 in seg}
                            for k in range(1, 41):
                                dx = 0.05 * k * (1 if direction == "hi" else -1)
                                for m2 in seg:
                                    placed[m2]["x"] = round(snap[m2] + dx, 4)
                                if not _seg_clear(seg):
                                    break
                                px = [pin_abs(k2[0], str(k2[1]))[0]
                                      for k2 in members if k2[0] in placed]
                                old_lo, old_hi = lo_x, hi_x
                                lo_x, hi_x = min(px), max(px)
                                got = _scan_slots()
                                if got is not None:
                                    return got
                                lo_x, hi_x = old_lo, old_hi
                            for m2 in seg:
                                placed[m2]["x"] = snap[m2]
                        return None

                    col = _scan_slots()
                    if col is None and lo_x is not None:
                        col = _shift_segment_and_rescan()
                    if col is None:
                        continue
                    assigned.add(n)
                    row_of[n] = row_of.get(anchor[0], 0)
            except Exception:  # noqa: BLE001 — 单件失败不影响其余
                continue
        pending = [n for n in ports if n not in assigned] + \
                  [n for n in rf if n not in assigned and n not in ports]
        for _round in range(2):   # 第二轮:自由脚对端已放齐后再放偏置类链
            deferred = []
            for p in pending:
                if p in assigned:
                    continue
                # 从 p 沿全边 BFS，第一次触达已放元件 = 锚点；路径上的元件 = 支链。
                # 只把**最短路径上**的元件当分支 —— 跨链桥（隔离电阻类）不在最短
                # 路径上，自然留给第 3 步竖放在行间（旧的"整块连通分量 + 出边计数"
                # 会把桥接件一起吞进分量，两条出边导致分支判定失败，Wilkinson_1G7
                # 这类无 MTEE、导线直接搭 T 的拓扑实测踩过）。
                prev = {p: None}
                queue = [p]
                anchor = drop_node = None
                hit = False
                while queue and not hit:
                    cur = queue.pop(0)
                    for nx in sorted(adj_all[cur]):
                        if nx in assigned:
                            anchor, drop_node = nx, cur
                            hit = True
                            break
                        if nx not in prev:
                            prev[nx] = cur
                            queue.append(nx)
                if anchor is None:
                    if _round == 1:
                        # 与已放区域完全不连通的孤立块：兜底放行间
                        placed[p] = {"x": 0.0, "y": _row_y(1)}
                        assigned.add(p)
                        row_of[p] = 1
                    continue
                # 到已放区域有 ≥2 条边的是跨链桥（隔离电阻类，两端都接已放元件），
                # 不能当分支 —— 否则会把真正的支线挤掉（Wilkinson_5G9 的 R1 实测）
                n_ext = sum(1 for m in adj_all[drop_node] if m in assigned)
                if n_ext >= 2:
                    continue
                if not edge_drop.get(frozenset((drop_node, anchor))):
                    # 非分支脚引入：只有锚点引脚所在节点是真 T 结（>=3 分叉）才算
                    # 分支 —— 否则跳过，交给第 3 步按跨链件处理
                    if not _junction(anchor, pin_of.get((anchor, drop_node), "1")):
                        continue
                tail = _extend_tail(side_path_in(drop_node, p, prev), anchor)
                # 短支路挂靠:锚点多端、支路无端口
                if len(tail) == 1 and not any(
                        _master_short(specs[n]["master"])
                        in _AUTO_PORT_MASTERS for n in tail):
                    # 单件死端支路（去耦/负载/分压类）：无论锚点是三端器件
                    # 还是普通两脚元件都优先就近挂靠，不再退到整行分支
                    if _place_short_branch(tail, anchor, drop_node):
                        continue
                if __import__("os").environ.get("AUTO_LAYOUT_DEBUG"):
                    print(f"[branch] p={p} anchor={anchor} drop={drop_node} "
                          f"tail={tail} round={_round}")
                # 自由脚对端未放齐时推迟一轮（等对端落位、朝向才判得准）；
                # 第二轮不再推迟 —— 环形拓扑（支线耦合器）会互相等待死锁，
                # 全部落到第 3 步桥接兜底摆成退化形态（P2/P4 同点重叠）
                if _round == 0 and _free_mate_unplaced(tail, anchor):
                    deferred.append(p)
                    continue
                row = row_of.get(anchor, 0) + 1
                branch_no[anchor] = branch_no.get(anchor, 0) + 1
                row += branch_no[anchor] - 1  # 同一锚点多条分支依次再往下一行
                # 行占位（后验）：链摆下后与已放件相撞则整链下移一行重摆
                #（支线耦合器 P2/P4 同点重叠的根因：不同锚点的支链共享
                # 分支行，互不知道对方已占用；先验带估对排布方向不敏感）
                row_y = _row_y(row)
                _snap = {n2: dict(placed[n2]) for n2 in placed}
                _snap_ang = dict(resolved_angle)
                while True:
                    place_chain(tail, row_y, anchor)
                    _crash = False
                    for n2 in tail:
                        if n2 not in placed:
                            continue
                        b2 = _rough_box(n2)
                        for m2 in placed:
                            if m2 in tail or m2 == anchor:
                                continue
                            bm = _rough_box(m2)
                            if not (b2[2] < bm[0] or bm[2] < b2[0]
                                    or b2[3] < bm[1] or bm[3] < b2[1]):
                                _crash = True
                                break
                        if _crash:
                            break
                    if not _crash or row >= 12:
                        break
                    placed.clear()
                    placed.update(_snap)
                    resolved_angle.clear()
                    resolved_angle.update(_snap_ang)
                    row += 1
                    for n2 in tail:
                        row_of[n2] = row
                    row_y = _row_y(row)
                branch_tails.setdefault(row, []).extend(tail)
                for n in tail:
                    assigned.add(n)
                _normalize_port_angle(tail[-1], "out")
            pending = deferred

        # 3) 桥接件/独立块（隔离电阻类，两端都是分支脚或不在端口路径上）：
        #    竖放件骑在上下两行正中、对齐上方引入引脚的列；横放链平铺在中线
        while True:
            rest = sorted(n for n in rf if n not in assigned)
            if not rest:
                break
            head = rest[0]
            chain = [head]
            seen = {head}
            cur = head
            while True:
                nxts = [m for m in sorted(adj_side[cur])
                        if m not in assigned and m not in seen]
                if len(nxts) != 1:
                    break
                cur = nxts[0]
                seen.add(cur)
                chain.append(cur)
            nbrs = sorted(adj_all[head], key=lambda m: row_of.get(m, 99))
            if not nbrs or nbrs[0] not in placed:
                placed[head] = {"x": 0.0, "y": _row_y(1)}
                assigned.add(head)
                row_of[head] = 1
                continue
            upper = nbrs[0]
            up_row = row_of.get(upper, 0)
            lo_row = max([row_of.get(m, up_row + 1) for m in nbrs[1:]] or [up_row + 1])
            up_pin = pin_abs(upper, pin_of.get((upper, head), "1"))
            mid_y = (_row_y(up_row) + _row_y(lo_row)) / 2.0
            if up_row == lo_row and len(nbrs) >= 2:
                # 同行双锚点（闭环环拓扑，如支线耦合器）：两锚点间的本行
                # 空档往往被同环元件占用 —— 桥放到锚点行下方 1.4 格的
                # "人行道"上、居中于两锚点，两向各一段 L 形短 stub 连接；
                # 若 stub 擦到某元件文字，7.5 步避让会自动挪开文字
                lower = next(m for m in nbrs[1:] if m in placed)
                pb = pin_abs(lower, pin_of.get((lower, head), "1"))
                for n2 in chain:
                    assigned.add(n2)
                    row_of[n2] = up_row
                width = len(chain) + (len(chain) - 1) * gap
                x0 = (up_pin[0] + pb[0]) / 2.0 - width / 2.0
                lane_y = _row_y(up_row) - 1.4
                place_chain(chain, lane_y, None, gap)
                dx = x0 - placed[head]["x"]
                if dx:
                    for n2 in chain:
                        placed[n2]["x"] = round(placed[n2]["x"] + dx, 4)
                continue
            for n in chain:
                assigned.add(n)
                row_of[n] = up_row + 1
            angle = int(_angle_of(head) or 0) % 180
            if len(chain) == 1 and angle == 90:  # 竖放单件（R90 等）
                lower = next((m for m in nbrs[1:] if m in placed), None)
                _lower_pt = pin_abs(lower, pin_of.get((lower, head), "1")) \
                    if lower else up_pin
                lower_x, lower_y = _lower_pt[0], _lower_pt[1]
                # 两端都接"信号通路引脚"（非分支脚）时，把下锚点所在支链整体
                # 平移，使两个锚点同列 —— 桥接件竖直直连两端、上下两个输出
                # 口同列，这是功分器的标准画法（Wilkinson_1G7_ML 实测拓扑）。
                # 任一端接分支脚（MTEE pin3 类）则不动：分支脚的列由支路下拉
                # 对齐决定，动了会把下拉线挤成横向长绕行（Wilkinson_5G9 拓扑）。
                if lower is not None and abs(lower_x - up_pin[0]) > 1e-9:
                    up_lbl = pin_of.get((upper, head), "1")
                    lo_lbl = pin_of.get((lower, head), "1")
                    up_is_drop = up_lbl in drop_pins.get(upper, ())
                    lo_is_drop = lo_lbl in drop_pins.get(lower, ())
                    if not up_is_drop and not lo_is_drop:
                        members = branch_tails.get(row_of.get(lower), [])
                        if members and all(m in placed for m in members):
                            dx = up_pin[0] - lower_x
                            for m in members:
                                placed[m]["x"] = round(placed[m]["x"] + dx, 4)
                            lower_x += dx
                # 放在两处连接点的中间，缩短最长的跨区导线；上下线各走一个直角。
                # 2026-09-30 布局-走线协同：竖放桥的 x 有自由度（只要在两
                # 锚点列之间，连线器给各一段 L 线）——行内压缩后中点可能
                # 恰好撞已放件的文字区（5G9 案例 R1 撞 MTEE2 六行参数列
                # 实测）。候选位按"文字/符号不撞优先、线长次之"选：中点、
                # 两侧锚点列、中间均分点，第一个干净位胜出；全撞保持中点。
                _bx_y = mid_y - 0.5
                _span = lower_x - up_pin[0]
                _bx_cands = [round((up_pin[0] + lower_x) / 2, 4),
                             round(up_pin[0], 4), round(lower_x, 4)]
                _bx_cands += [round(up_pin[0] + _span * f, 4)
                              for f in (0.25, 0.75, 0.4, 0.6, 0.15, 0.85)
                              if up_pin[0] < up_pin[0] + _span * f < lower_x]

                def _bridge_x_clear(c):
                    z = _annot_text_zone(
                        _master_short(specs[head]["master"]), c, _bx_y,
                        0.0, 0.0, _angle_of(head))
                    if z is not None:
                        for m2 in placed:
                            z2 = _annot_text_zone(
                                _master_short(specs[m2]["master"])
                                if m2 in specs else "VAR",
                                placed[m2]["x"], placed[m2]["y"],
                                *(placed[m2].get("annot") or (0.0, 0.0)),
                                _angle_of(m2))
                            if z2 is not None and not (
                                    z[2] < z2[0] or z2[2] < z[0]
                                    or z[3] < z2[1] or z2[3] < z[1]):
                                return False
                    box = (c - 0.35, min(_bx_y, _bx_y + 1.0) - 0.15,
                           c + 0.35, max(_bx_y, _bx_y + 1.0) + 0.15)
                    for m2 in placed:
                        b2 = _rough_box(m2)
                        if not (b2[2] < box[0] or box[2] < b2[0]
                                or b2[3] < box[1] or box[3] < b2[1]):
                            return False
                    # 布局-走线协同（2026-09-30）：本桥两段 L 连线的**横段**
                    # 不得穿任何已放件的文字区 —— 桥挪到锚点列会让横段
                    # 横贯整图，堵死邻件标注的全部避让位（MTEE 威尔金森
                    # 案例 R1 选上锚列后横贯线堵死 MTEE2 文字避让实测）。
                    # L 线两种变体（先横/先竖）的横段分别落在两端行，
                    # 全部检查；走线器两序等长时取先横 —— 按保守口径
                    # 任一变体穿字即拒。
                    _top_y = _bx_y + 1.0
                    _zones = []
                    for m2 in placed:
                        z2 = _annot_text_zone(
                            _master_short(specs[m2]["master"])
                            if m2 in specs else "VAR",
                            placed[m2]["x"], placed[m2]["y"],
                            *(placed[m2].get("annot") or (0.0, 0.0)),
                            _angle_of(m2))
                        if z2 is not None:
                            _zones.append(z2)
                    for ya_, yb_, xa_, xb_ in (
                            (up_pin[1], _top_y, up_pin[0], c),
                            (_bx_y, lower_y, c, lower_x)):
                        for hy_ in (ya_, yb_):
                            seg = (min(xa_, xb_), hy_, max(xa_, xb_), hy_)
                            if seg[0] >= seg[2]:
                                continue
                            for z2 in _zones:
                                if (seg[1] > z2[1] and seg[1] < z2[3]
                                        and seg[0] < z2[2] and seg[2] > z2[0]):
                                    return False
                    return True

                _bx = next((c for c in _bx_cands if _bridge_x_clear(c)),
                           round((up_pin[0] + lower_x) / 2, 4))
                placed[head] = {"x": _bx, "y": _bx_y}
                cur_x = up_pin[0] + gap
            else:
                cur_x = place_chain(chain, mid_y, None)
                # 桥接链锚到上方引脚的列：整体平移，让链头对齐 up_pin.x
                # 2026-09-30 布局-走线协同：单件竖放桥的 x 有自由度（在两
                # 锚点列之间，连线器给各一段 L 线）——盲目对齐上锚列会让
                # 桥的下行 L 线横贯整图、堵死邻件标注的全部避让位，且桥
                # 自身文字可能撞已放参数列（MTEE 威尔金森 R1 实测两症）。
                # 候选位（上锚列/中点/下锚列/均分点）按"符号+文字+连线
                # 横段都不撞已放件"选，全撞回退上锚列（旧行为）。
                _bx_y = mid_y
                _bx_default = up_pin[0]
                _lower2 = next((m for m in nbrs[1:] if m in placed), None)
                _lpt = pin_abs(_lower2, pin_of.get((_lower2, head), "1")) \
                    if _lower2 else up_pin
                lower_x, lower_y = _lpt[0], _lpt[1]
                _bx_cands = [_bx_default,
                             round((up_pin[0] + lower_x) / 2, 4),
                             round(lower_x, 4)]
                _span = lower_x - up_pin[0]
                _bx_cands += [round(up_pin[0] + _span * f, 4)
                              for f in (0.25, 0.75, 0.4, 0.6, 0.15, 0.85)
                              if up_pin[0] < up_pin[0] + _span * f < lower_x]
                if len(chain) == 1 and not _multi_pin(head) \
                        and specs[head].get("angle") is None \
                        and _lower2 is not None:
                    up_lbl = str(pin_of.get((head, upper), "1"))
                    _ang = _shunt_angle(head, next(
                        str(l) for l in offs(head) if str(l) != up_lbl), 1)


                    def _bridge_penalty(c):
                        """候选位罚分：0 = 符号/文字/连线横段全干净；
                        否则按"互压数+穿字数+撞盒数"计罚，横段总长做
                        次序 —— 全候选有罚时选罚最小（破坏最小）。"""
                        pen = 0.0
                        seg_len = 0.0
                        z = _annot_text_zone(
                            _master_short(specs[head]["master"]), c, _bx_y,
                            0.0, 0.0, _ang)
                        _zones = []
                        for m2 in placed:
                            z2 = _annot_text_zone(
                                _master_short(specs[m2]["master"])
                                if m2 in specs else "VAR",
                                placed[m2]["x"], placed[m2]["y"],
                                *(placed[m2].get("annot") or (0.0, 0.0)),
                                _angle_of(m2))
                            if z2 is not None:
                                _zones.append(z2)
                        if z is not None:
                            for z2 in _zones:
                                if not (z[2] < z2[0] - 0.02
                                        or z2[2] < z[0] - 0.02
                                        or z[3] < z2[1] - 0.02
                                        or z2[3] < z[1] - 0.02):
                                    pen += 1.0
                        box = (c - 0.35, min(_bx_y, _bx_y + 1.0) - 0.15,
                               c + 0.35, max(_bx_y, _bx_y + 1.0) + 0.15)
                        for m2 in placed:
                            b2 = _rough_box(m2)
                            if not (b2[2] < box[0] or box[2] < b2[0]
                                    or b2[3] < box[1] or box[3] < b2[1]):
                                pen += 1.0
                        # 两段 L 连线的横段（先横变体在桥端行、先竖变体
                        # 在锚点行）不得穿任何已放文字区
                        for ya_, yb_, xa_, xb_ in (
                                (up_pin[1], _bx_y + 1.0, up_pin[0], c),
                                (_bx_y, lower_y, c, lower_x)):
                            for hy_ in (ya_, yb_):
                                seg = (min(xa_, xb_), hy_,
                                       max(xa_, xb_), hy_)
                                if seg[0] >= seg[2]:
                                    continue
                                seg_len += seg[2] - seg[0]
                                for z2 in _zones:
                                    if (seg[1] > z2[1] and seg[1] < z2[3]
                                            and seg[0] < z2[2]
                                            and seg[2] > z2[0]):
                                        # 权重 2.0：横段穿字会把邻件标注
                                        # 的避让空间整个堵死（危害高于桥
                                        # 自身文字与参数列的保守互压）
                                        pen += 2.0
                        return pen + seg_len * 0.001

                    _bx = min(_bx_cands, key=_bridge_penalty)
                else:
                    _bx = _bx_default
                dx = _bx - placed[head]["x"]
                if dx:
                    for n in chain:
                        placed[n]["x"] += dx
                # 单件竖放桥的引脚定向：连接上锚点的引脚必须朝上 ——
                # place_chain 的 shunt 朝向只看"对端在上方"，不看连接
                # 映射，pin2 连上锚点却被转到下端时，连线被迫穿自己
                # 符号体（MTEE 威尔金森隔离电阻实测 (5.65,-0.5)->(5.65,-3.5)）
                if len(chain) == 1 and not _multi_pin(head) \
                        and specs[head].get("angle") is None:
                    up_lbl = str(pin_of.get((head, upper), "1"))
                    ry = _rot_off(*offs(head).get(up_lbl, (0.0, 0.0)),
                                  _angle_of(head))[1]
                    if ry < -1e-9:
                        old_ang = _angle_of(head)
                        new_ang = (int(old_ang or 0) + 180) % 360
                        resolved_angle[head] = new_ang
                        oy = _rot_off(0.0, 1.0, old_ang)[1] \
                            if old_ang is not None else 1.0
                        ny = _rot_off(0.0, 1.0, new_ang)[1]
                        placed[head]["y"] = round(
                            placed[head]["y"] + (oy - ny) * 1.0, 4)
                        placed[head]["angle"] = new_ang
                cur_x = up_pin[0] + gap

    # 3.9) 链尾跨网络件的朝向:angle 未定的两脚件,若某一引脚的 rf 对端
    # 全部已放且都偏离本件行线（偏置链尾上拉到电源节点这类跨网络延续,
    # 链内分类时对端还没放、看不到方向）,把该脚转向对端质心 —— 布线
    # 随之 1 拐弯直连,不再绕边（2026-09-29 CB_FM_Amp 实测:R1.2 朝下时
    # R1→VCC 拐 2 弯绕左边缘 19.2 格,朝上 1 弯 13.7 格）。
    node_members = {}
    for key in node_parent:
        node_members.setdefault(_node_find(key), []).append(key)
    for n in rf:
        if n not in placed or n in resolved_angle                 or specs[n].get("angle") is not None                 or placed[n].get("angle") is not None or _multi_pin(n):
            continue
        lbl_mates = {}
        for m in adj_all[n]:
            lbl_mates.setdefault(str(pin_of.get((n, m), "1")), []).append(m)
        labels = [str(l) for l in offs(n)]
        if len(labels) != 2 or not lbl_mates:
            continue
        row_y = placed[n]["y"]
        exits = []
        for lbl in labels:
            mates = [m for m in lbl_mates.get(lbl, []) if m in placed]
            if mates and all(
                    abs(pin_abs(m, pin_of.get((m, n), "1"))[1] - row_y) > 0.5
                    for m in mates):
                exits.append((lbl, mates))
        if len(exits) != 1:
            continue
        lbl, mates = exits[0]
        ys = [pin_abs(m, pin_of.get((m, n), "1"))[1] for m in mates]
        want = 1 if sum(ys) / len(ys) > row_y else -1
        resolved_angle[n] = _shunt_angle(n, lbl, want)

    # 4) GROUND：贴近地脚；Term 的地脚在符号边缘，向符号外侧错开再下行
    for n in specs:
        if kind(n) != "gnd":
            continue
        mates = [(m, l) for m, l in gnd_partners.get(n, []) if m in placed]
        if len(mates) >= 2:
            # 共享地（多件同节点共用一个地符号）：挂在全部伙伴地脚的
            # 下方中点 —— 各伙伴的地脚短线下探后水平汇入，比"整组退到
            # 远端分支行"或"只贴第一个伙伴"都近（2026-09-30 用户规则三）
            pts = [pin_abs(m, l) for m, l in mates]
            ys = [q[1] for q in pts]
            xs = [q[0] for q in pts]
            y_low = min(ys)
            if max(ys) - y_low <= 0.45 and max(xs) - min(xs) <= 6.0:
                placed[n] = {"x": round(sum(xs) / len(xs), 4),
                             "y": y_low - 0.5}
                resolved_angle[n] = 270
                continue
        p = partner.get(n)
        if p and p[0] in placed:
            xy = pin_abs(p[0], p[1])
            shift_x = 0.0
            if _master_short(specs[p[0]]["master"]) in _AUTO_PORT_MASTERS:
                other_pin = "2" if str(p[1]) == "1" else "1"
                other = pin_abs(p[0], other_pin)
                shift_x = -0.35 if other[0] > xy[0] else 0.35
            # 地的挂放方向沿所接引脚的延伸方向：竖放元件的上端脚（引脚在
            # origin 上方）只能向上接地（GND 挂脚上方、符号倒置 90°），
            # 其余（下端脚、水平元件的脚、端口地脚）沿用下挂 270°。
            # GROUND 角度实测建档（2026-09-29 渲染 gnd4.pdf 提取几何）：
            # 0° 接入线水平向右/符号体在右、90° 符号体在 origin 上方
            # （倒置接地，挂导线顶端）、180° 镜像向左、270° 符号体在
            # origin 下方（正置接地，挂导线底端）。
            op = placed[p[0]]
            pin_off = _rot_off(*offs(p[0]).get(str(p[1]), (0.0, 0.0)),
                               _angle_of(p[0]))
            if pin_off[1] > 1e-9:
                placed[n] = {"x": xy[0] + shift_x, "y": xy[1] + 0.5}
                resolved_angle[n] = 90
            elif abs(pin_off[1]) <= 1e-9 and abs(pin_off[0]) > 1e-9:
                # 水平引脚的侧向接地 —— 仅限"自由端"（该脚节点上除地外
                # 无其他 rf 引脚，如横放直流源负端、水平支路末端）。
                # 链上中段元件的水平脚侧挂会把地符号骑到行线上（fuzz
                # 实测 G1 文字被主线穿过）—— 中段脚仍走下方正置。
                root = _node_find((p[0], str(p[1])))
                free_end = not any(
                    k[0] != p[0] and kind(k[0]) == "rf"
                    for k in node_parent if _node_find(k) == root)
                if free_end:
                    sgn = 1 if pin_off[0] > 0 else -1
                    placed[n] = {"x": xy[0] + sgn * 0.5 + shift_x,
                                 "y": xy[1]}
                    resolved_angle[n] = 0 if sgn > 0 else 180
                else:
                    placed[n] = {"x": xy[0] + shift_x, "y": xy[1] - 0.5}
                    resolved_angle[n] = 270
            else:
                placed[n] = {"x": xy[0] + shift_x, "y": xy[1] - 0.5}
                resolved_angle[n] = 270
        else:
            placed[n] = {"x": 0.0, "y": -0.5}
            resolved_angle[n] = 270

    # 5) 注释件：VAR/MSUB 左上（行高按文本行数），控制器等左下
    if rf or placed:
        min_x = min(v["x"] for v in placed.values()) if placed else 0.0
        min_y = min(v["y"] for v in placed.values()) if placed else 0.0
    else:
        min_x = min_y = 0.0

    def annot_y(nlines):
        return round(1.6 + 0.62 * max(nlines - 1, 0), 2)

    var_count = len((var_spec or {}).get("values") or {})
    _ctrl_row = 0   # 左下控制器的行序号:同区多件(控制器+模型包含)逐行下移,
    #               否则 INC1 与 SP1 叠在同一点(CE_FM_Amp_AGENT 实测互压)
    for n in specs:
        k = kind(n)
        if k == "annot":
            m = _master_short(specs[n]["master"])
            nlines = len(specs[n].get("params") or {})
            if m == "VAR":
                placed[n] = {"x": min_x, "y": annot_y(max(nlines, var_count) + 1)}
            elif m == "MSUB":
                placed[n] = {"x": min_x + 4.5, "y": annot_y(nlines + 1)}
            else:
                placed[n] = {"x": min_x,
                             "y": round(min_y - 3.2 - 2.2 * _ctrl_row, 2)}
                _ctrl_row += 1

    # 5.4) var_spec 的 VAR 兜底位置在这里先落位 —— 必须赶在 5.5/5.6 的
    #      注解避让之前，否则对称布局下躲过 MSUB 抬升与 VAR 下移。
    if var_spec is not None:
        vname = str(var_spec.get("name") or "VAR1")
        placed.setdefault(vname, {"x": 0.0, "y": annot_y(var_count + 1)})

    # 5.5) 对称布局：MSUB 改放电路右侧空白列（最右元件再右 1.5、与上臂
    #      同高）——文字下垂落在端口右侧空区，单级/两级/多臂都不会撞文字
    #      或导线；原先"默认落点骑上臂线 → 整体抬升"的补丁不再需要。
    if sym_plan:
        row_max = max(abs(r) for r in sym_rows_used)
        x_right = max(placed[n]["x"] for n in rf) + 1.5
        for n in specs:
            if kind(n) == "annot" \
                    and _master_short(specs[n]["master"]) == "MSUB":
                placed[n] = {"x": round(x_right, 4), "y": round(row_max, 4)}

    # 5.6) 对称布局：VAR 文字块高度随变量数增长（实测 8 变量块高 3.9、
    #      底距 origin 3.58，每多 1 变量 +0.62）——定格规则：文字底必须
    #      保持在输入行文字顶 (≈-0.15) 之上 0.3 格，同时尽量贴近信号区；
    #      变量少时贴在 row_max+0.8，变量多时按文字高度抬高。
    if sym_plan:
        row_max = max(abs(r) for r in sym_rows_used)

        def _var_y(n_vars):
            base = row_max + 0.8
            tall = 0.15 + 3.58 + 0.62 * max(n_vars - 8, 0)
            return round(max(base, tall), 4)

        for n in specs:
            if kind(n) == "annot" \
                    and _master_short(specs[n]["master"]) == "VAR":
                placed[n]["y"] = _var_y(len(specs[n].get("params") or {}))
        # var_spec 路径创建的 VAR1 不在 specs 里，单独定格
        if var_spec is not None:
            vname = str(var_spec.get("name") or "VAR1")
            if vname in placed:
                placed[vname]["y"] = _var_y(len(var_spec.get("values") or {}))

    # 6) 整体平移到 x>=0，并给 var_spec 兜底位置
    if placed:
        min_x = min(v["x"] for v in placed.values())
        if min_x:
            for v in placed.values():
                v["x"] = round(v["x"] - min_x, 4)
                v["y"] = round(v["y"], 4)
    # 7.5) 通用标注避让：按每条连线的曼哈顿规划路径复算文字区冲突，
    #      冲突元件的标注在候选偏移（上移/左移…）里挑第一个既不被任何
    #      规划线穿过、也不与其他文字区互压的位置。对称分支已发的 annot
    #      一并复查（已避开的不会被动，幂等）。实机证据：MTEE 拓扑
    #      layout=auto 时 pin3 下拉竖线穿 MTEE 自己 6 行文字、隔离电阻
    #      下端长横线穿支路文字（2026-09-29 渲染实测 7 处）。
    def _zone_of(n, extra=(0.0, 0.0)):
        p = placed[n]
        old = p.get("annot") or (0.0, 0.0)
        key = _master_short(specs[n]["master"]) if n in specs else "VAR"
        angle = _angle_of(n) if n in specs else None
        return _annot_text_zone(key, p["x"], p["y"],
                                old[0] + extra[0], old[1] + extra[1], angle)

    def _hard_seg_hit(pt_a, pt_b, z, n):
        """7.5 避让的冲突判定：分级口径统一走 text_wire_conflict_level
        （单一事实来源）。返回 True = 硬冲突（必须避让）。

        2026-09-30 补深穿口径：硬冲突同样只认"深穿"——线在文字区
        内部的重合长度 >0.15 才挪字。竖线擦竖放文字区上/下缘的保守
        边带（交叠 ≤0.15，真实字形不相交：Q1.b→R1 竖线擦 R2 文字区
        上缘 0.08 实测）不算，否则骑行线支路的文字全被赶出符号。
        """
        if not _seg_hits(pt_a, pt_b, z):
            return False
        if text_wire_conflict_level(_angle_of(n), pt_a, pt_b) != "hard":
            return False
        if abs(pt_a[0] - pt_b[0]) < 1e-9:   # 竖线:区内重合 = y 向跨度
            depth = (min(max(pt_a[1], pt_b[1]), z[3])
                     - max(min(pt_a[1], pt_b[1]), z[1]))
        else:                               # 横线:区内重合 = x 向跨度
            depth = (min(max(pt_a[0], pt_b[0]), z[2])
                     - max(min(pt_a[0], pt_b[0]), z[0]))
        return depth > 0.15

    def _soft_seg_hit_deep(pt_a, pt_b, z, n):
        """软冲突（横线 × 竖放文字）的**深穿**判定：线要进入文字区内部
        超过 0.15 才算需要避让。行线擦文字上/下缘的 graze（穿透 <0.15）
        是手工图常态（CE 手工参考图 R1/R2 即此形态），不触发挪动 ——
        旧口径任何 soft 命中就全候选域找"完全无碰"落点，支路文字被赶出
        符号 2.8 格（CE_FM_Amp_AGENT RD 实测）。"""
        if not _seg_hits(pt_a, pt_b, z):
            return False
        if text_wire_conflict_level(_angle_of(n), pt_a, pt_b) != "soft":
            return False
        depth = min(pt_a[1] - z[1], z[3] - pt_a[1])
        return depth > 0.15

    # 规划线段：用 plan_wire_routes 的真实规划路径（2026-09-29 与走线
    # 计划统一口径 —— 旧的逐连接 _ortho_points 朴素 L 重放与布线层路径
    # 不同，避让决策会基于幽灵路径：真被采用的线没查、没被采用的线
    # 反而把文字挪走）。规划失败退回朴素重放兜底。
    plan_segs = []
    try:
        # 角度先合并再规划 —— 此处 placed 还没落 resolved_angle（第 7 步
        # 才写），竖放件会被按横放算符号框，规划/避让口径与最终布线层
        # 不一致（支线耦合器 TLv2 实测：内部规划全线落 y=0 兜底）
        _placed_for_plan = {n2: dict(p2) for n2, p2 in placed.items()}
        for _n2, _a2 in resolved_angle.items():
            if _n2 in _placed_for_plan and _a2 is not None:
                _placed_for_plan[_n2]["angle"] = _a2
        _routes = plan_wire_routes(instances, connections,
                                   _placed_for_plan)
    except Exception:  # noqa: BLE001
        _routes = None
    if _routes is not None:
        for conn_i, conn in enumerate(connections or []):
            if conn_i not in _routes or _routes[conn_i][0] is None:
                continue
            a, b = conn.get("a"), conn.get("b")
            if not (isinstance(a, (list, tuple))
                    and isinstance(b, (list, tuple))):
                continue
            na, nb = str(a[0]), str(b[0])
            if na not in placed or nb not in placed:
                continue
            pa = pin_abs(na, str(a[1]))
            pb = pin_abs(nb, str(b[1]))
            pts = [pa] + [tuple(w) for w in _routes[conn_i][0]] + [pb]
            for s1, s2 in zip(pts, pts[1:]):
                if s1 != s2:
                    plan_segs.append((s1, s2))
    else:
        for conn in connections or []:
            a, b = conn.get("a"), conn.get("b")
            if not (isinstance(a, (list, tuple))
                    and isinstance(b, (list, tuple))):
                continue
            na, nb = str(a[0]), str(b[0])
            if na not in placed or nb not in placed:
                continue
            pa = pin_abs(na, str(a[1]))
            pb = pin_abs(nb, str(b[1]))
            pts = _ortho_points(pa[0], pa[1], pb[0], pb[1])
            for s1, s2 in zip(pts, pts[1:]):
                if s1 != s2:
                    plan_segs.append((s1, s2))

    def _seg_hits(pt_a, pt_b, z, pad=0.05):
        if z is None:
            return False
        # 注意：不做"线端点压自己引脚就豁免"——MTEE 的 pin3 下拉竖线
        # 从自己引脚出发向下穿自己文字，恰恰是必须被检出并避让的情形
        (x1, y1), (x2, y2) = pt_a, pt_b
        if abs(y1 - y2) < 1e-9:
            return (z[1] - pad <= y1 <= z[3] + pad
                    and max(x1, x2) >= z[0] - pad and min(x1, x2) <= z[2] + pad)
        if abs(x1 - x2) < 1e-9:
            return (z[0] - pad <= x1 <= z[2] + pad
                    and max(y1, y2) >= z[1] - pad and min(y1, y2) <= z[3] + pad)
        return False

    def _zones_overlap(z1, z2, pad=0.02):
        # pad 0.02（2026-09-30 紧凑化）：zone 模型各含 ~0.05 保守边带，
        # 两个文字区的 zone 边缘间隙 0.03（如 RISO 竖放文字与左挪的
        # MTEE 参数列）在真实渲染中不重叠 —— 只把 zone 实质相交或
        # 间隙 <0.02 当互压，不再用 0.05 pad 把保守边带的擦边当互压
        # （MTEE 威尔金森案例 MTEE2 避让候选被 RISO zone 卡死实测）。
        return not (z1[2] < z2[0] - pad or z2[2] < z1[0] - pad
                    or z1[3] < z2[1] - pad or z2[3] < z1[1] - pad)

    names = [n for n in placed if _zone_of(n)]
    settled = set()
    for _pass in range(4):
        changed = False
        for n in names:
            if n in settled or placed[n].get("_annot_pinned"):
                continue
            z = _zone_of(n)
            # 触发口径（2026-09-30）：硬冲突必触发；软冲突须**深穿**才触发
            # （擦边 graze 不算，见 _soft_seg_hit_deep）。互压仍不触发：
            # 支路密集区的保守互压靠支路列间距解决，触发挪动会把元件
            # 推出组外（CE_FM_Amp_AGENT RE1 被推到右下远端实测）。
            if not any(_hard_seg_hit(a, b, z, n)
                       or _soft_seg_hit_deep(a, b, z, n)
                       for a, b in plan_segs):
                continue
            import os as _os
            _dbg = _os.environ.get("SYM_ANNOT_DEBUG")
            fixed = False
            if _dbg:
                hits = [(a, b) for a, b in plan_segs if _seg_hits(a, b, z)]
                print(f"[annot] {n} zone={tuple(round(v,2) for v in z)} "
                      f"被 {len(hits)} 条规划线穿过: {hits}")
            def _try(cand):
                # fixed/changed 必须 nonlocal —— 否则赋值落在 _try 局部,
                # 外层候选扫描看不到"已接受"、不 break,把整个候选域扫完
                # 且偏移逐次**叠加**(非相对同一基准):CE 的 RD +2.8 =
                # 1.4+1.4 两次叠加,2026-09-30 才定位的历史漂移真根因
                nonlocal fixed, changed
                p = placed[n]
                oldv = p.get("annot") or (0.0, 0.0)
                newv = (round(oldv[0] + cand[0], 4),
                        round(oldv[1] + cand[1], 4))
                # 归属约束(2026-09-30):挪动后的文字区必须仍与符号"搭着"——
                # x 或 y 至少一个方向投影重叠,另一方向到符号盒的间隙
                # ≤1.2(横)/2.2(纵)。纵向放宽到 2.2 是 MTEE 五行文字块
                # (高 4.7)历史验证位 (-1.2,-2.2) 的需要 —— 块顶仍朝符号
                # 下垂,读图归属不变;CE 的 RD(+2.8)/PORT1(-2.4) 是横向
                # 投影都脱开的真悬空,被此规则拦下。
                z2 = _zone_of(n, cand)
                box = (_rough_box_public(specs[n], placed[n])
                       if n in specs else None)
                if not _attached(z2, box):
                    return
                # 同侧规则(2026-09-30):竖放/横放 R/C/L 的文字不得越过
                # origin 行跑到符号体的对侧 —— 体向下的支路电阻文字跳到
                # 行上方,读图会把它认成别的元件(CE 实测 R2 挤进 0.5 行距
                # 的窄缝时文字被迫跨行)。MTEE/MLIN 等文字块在体侧方铺开,
                # 不受此限;RISO1 类体向上元件的上移(0,+1.0)是同侧,放行。
                if n in specs:
                    _k2 = _master_short(specs[n]["master"])
                    if _k2 in ("R", "C", "L"):
                        _a2 = int(_angle_of(n) or 0) % 360
                        if _a2 == 270 and newv[1] > 0.10:
                            return
                        if _a2 == 90 and newv[1] < -0.10:
                            return
                        if _a2 % 180 == 0 and newv[1] > 0.10:
                            return
                p["annot"] = newv
                settled.add(n)
                fixed = changed = True

            def _attached(z, box, pad=0.05, attach_h=1.2, attach_v=2.2):
                """文字区 z 与符号盒 box 是否保持归属可判读的搭接。"""
                if z is None or box is None:
                    return True
                x_ov = z[0] <= box[2] + pad and box[0] <= z[2] + pad
                y_ov = z[1] <= box[3] + pad and box[1] <= z[3] + pad
                if x_ov and y_ov:
                    return True
                if x_ov:   # 垂直方向脱开:查纵间隙
                    gap = max(box[1] - z[3], z[1] - box[3])
                    return gap <= attach_v
                if y_ov:   # 水平方向脱开:查横向间隙
                    gap = max(box[0] - z[2], z[0] - box[2])
                    return gap <= attach_h
                return False

            def _ok(off, hard_only):
                z2 = _zone_of(n, off)
                # hard_only=True: 任何线穿都拒。hard_only=False: 只豁免
                # "水平段 × 竖放文字"的软冲突,竖直段命中仍拒 —— 否则会
                # 接受被自己下拉线穿过的候选(ML1 实测)
                if any(_hard_seg_hit(a, b, z2, n) for a, b in plan_segs):
                    return False
                if hard_only and any(
                        _seg_hits(a, b, z2) for a, b in plan_segs):
                    return False
                if any(m != n and _zone_of(m)
                       and _zones_overlap(z2, _zone_of(m)) for m in names):
                    return False
                return True

            for hard_only in (True, False):
                for dy in (0.0, 1.4, 2.0, 3.4, -0.3, -2.2, 4.8):
                    for dx in (0.0, -1.2, -1.9, -2.6, -3.3, -0.7, -1.5):
                        off = (dx, dy)
                        if off == (0.0, 0.0):
                            continue
                        # 归属约束在 _try 里按"文字区仍与符号搭接"判定
                        # (方向化间隙上限),不再用平坦 |off| 和封顶 ——
                        # MTEE 五行块的历史验证位 (−1.2,−2.2) 需要纵向上限
                        if _ok(off, hard_only):
                            if _dbg:
                                print(f"[annot] {n} -> {off} "
                                      f"({'硬' if hard_only else '软'})")
                            _try(off)
                            break
                    if fixed:
                        break
                if fixed:
                    break
            # 所有候选都躲不开（扫掠型长线）：保持原位，由路由层处理
        if not changed:
            break

        # 收敛复查（2026-09-29）：前面轮次留下的 annot 偏移（幽灵路径
        # 时代 / 元件被后续 pass 挪动后）若已无必要 —— 默认位无线穿、
        # 无互压 —— 就收回默认位。避免"文字离符号很远"的漂移残留
        #（CE_FM_Amp_AGENT R1 实测：文字漂到符号左上 3 格外）。
        for n in names:
            p = placed[n]
            old = p.get("annot") or (0.0, 0.0)
            if old == (0.0, 0.0) or p.get("_annot_pinned"):
                continue
            z_default = _zone_of(n, (-old[0], -old[1]))
            # 软冲突豁免与避让一致:水平段穿竖放文字区不算阻回收的硬冲突
            _hard = [s for s in plan_segs if _hard_seg_hit(s[0], s[1],
                                                           z_default, n)]
            _soft = [s for s in plan_segs if _seg_hits(s[0], s[1],
                                                       z_default)]
            if _hard or any(abs(s[0][1] - s[1][1]) >= 1e-9
                            for s in _soft):
                continue
            if any(m != n and _zone_of(m)
                   and _zones_overlap(z_default, _zone_of(m)) for m in names):
                continue
            p["annot"] = (0.0, 0.0)

    # 7) 输出规范化后的端口角度（build_schematic 在实例未给 angle 时应用；
    #    角度已在摆放前参与引脚坐标计算，位置与朝向一致）
    for n, a in resolved_angle.items():
        if n in placed:
            placed[n]["angle"] = a
    return placed


def plan_wire_routes(instances, connections, placed):
    """网络级走线计划（2026-09-29 用户规则 2）:返回 {连接索引: 折点列表}。

    规则:
    * 先按电气网络规划节点与共享线段 —— 同一 junction 向左/向右的出线在
      同一直线上(共线),第三方向形成清晰的 T 形分支;同一节点不得拉出两条
      表达同一分流关系的平行线。实现:同 net 的拐点坐标吸附到该 net 已有
      走廊(水平 y / 竖直 x),重叠段表达同电位,由 _wire_connection 的
      同 net 豁免合并成一条线;
    * 每条连线直线或 1 个拐弯;两序(先横后竖/先竖后横)按
      "与同 net 走廊共线 > 不穿其他 net 的引脚点与符号占位 > 总长短"选择;
      两序都被拒时做 2 拐弯走廊偏移(±1/±2/±3),仍不行退回先横后竖,
      由在线 _route_around 兜底;
    * 符号占位用 _rough_box 保守矩形(与在线 inst.bbox 判定同规则:
      _segment_hits_box strict 豁免端点符号与引脚伸出段)。
    """
    specs = {str(s["name"]): s for s in instances}

    def pin_xy(name, lbl):
        p = placed[str(name)]
        ang = p.get("angle")
        if ang is None:
            ang = int(specs[str(name)].get("angle") or 0)
        o = _AUTO_PIN_OFFS.get(_master_short(specs[str(name)]["master"]),
                               _AUTO_PIN_OFFS_DEFAULT)
        dx, dy = _rot_off(*o.get(str(lbl), (0.0, 0.0)), int(ang or 0))
        return (round(p["x"] + dx, 6), round(p["y"] + dy, 6))

    # 电气 net:并查集(连接引脚对 union)
    parent = {}

    def find(k):
        while parent.get(k, k) != k:
            parent[k] = parent.get(parent[k], parent[k])
            k = parent[k]
        return k

    conns = []
    for i, conn in enumerate(connections or []):
        a, b = conn.get("a"), conn.get("b")
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            continue
        ka = (str(a[0]), str(a[1]))
        kb = (str(b[0]), str(b[1]))
        ra, rb = find(ka), find(kb)
        if ra != rb:
            parent[rb] = ra
        conns.append((i, ka, kb))

    def net_tag(k):
        r = find(k)
        return f"{r[0]}.{r[1]}"

    # 引脚点(带 net)与符号占位
    pin_pts = []
    for _i, ka, kb in conns:
        r = net_tag(ka)
        pin_pts.append((*pin_xy(*ka), r))
        pin_pts.append((*pin_xy(*kb), r))
    boxes = []
    pin_sym = set()
    for n, p in placed.items():
        if str(n) not in specs:
            continue
        m = _master_short(specs[str(n)]["master"])
        if m in _AUTO_GND_MASTERS:
            boxes.append((str(n), (p["x"] - 0.2, p["y"] - 0.5,
                                   p["x"] + 0.5, p["y"] + 0.2)))
            continue
        boxes.append((str(n), _rough_box_public(specs[str(n)], p)))

    hy = {}   # net tag -> 共享水平走廊 y 集合
    vx = {}   # net tag -> 共享竖直走廊 x 集合
    out = {}   # 连接索引 -> (折点列表, net 标签)
    planned = []   # 已规划折线 [(net 标签, pts)] —— 跨网交叉检查用

    def seg_hits_sym(seg, endpoints_syms):
        a, b = seg
        for n, box in boxes:
            strict = n in endpoints_syms
            if not _segment_hits_box(a, b, box, strict=strict):
                continue
            # 线段恰好穿过该符号自身引脚点（同网络）= 合法引脚搭接
            #（T 结打在引脚上）—— 五路直通臂的直线穿过 A2.1 引脚
            # 被粗占位盒拦截的误杀由此解除（2026-09-30 R13）
            if _seg_through_own_pin(a, b, n):
                continue
            return True
        return False

    def _seg_through_own_pin(a, b, n):
        spec = specs.get(n)
        if spec is None:
            return False
        p = placed.get(n)
        if p is None:
            return False
        o = _AUTO_PIN_OFFS.get(_master_short(spec["master"]),
                               _AUTO_PIN_OFFS_DEFAULT)
        ang = int(p.get("angle") or spec.get("angle") or 0)
        for _l, (dx, dy) in o.items():
            rx, ry = _rot_off(dx, dy, ang)
            px, py = p["x"] + rx, p["y"] + ry
            on = (abs(a[0] - b[0]) < 1e-9
                  and abs(px - a[0]) < 1e-6
                  and min(a[1], b[1]) - 1e-6 <= py <= max(a[1], b[1]) + 1e-6)                 or (abs(a[1] - b[1]) < 1e-9
                    and abs(py - a[1]) < 1e-6
                    and min(a[0], b[0]) - 1e-6 <= px <= max(a[0], b[0]) + 1e-6)
            if on:
                return True
        return False

    def plan_one(pa, pb, tag, end_syms):
        def segs(wp):
            pts = [pa] + list(wp) + [pb]
            return list(zip(pts, pts[1:]))

        def blocked(wp):
            all_pts = [pa] + list(wp) + [pb]
            last_k = len(all_pts) - 2
            for k, s in enumerate(zip(all_pts, all_pts[1:])):
                # 跨网交叉:不同电气网络的规划线不得交叉/重叠/触点
                #（同一节点拉出平行线或十字交叉都是用户明令禁止的;
                #  离线不查的话在线 _route_clear 会拒,布线整体失败）
                for otag, opts in planned:
                    if otag == tag:
                        continue
                    o_ends = {opts[0], opts[-1]}
                    if any(_segment_hits_route(s[0], s[1], c, d,
                                               {pa, pb}, o_ends)
                           for c, d in zip(opts, opts[1:])):
                        return True
                for (px, py, pn) in pin_pts:
                    if pn == tag:
                        continue
                    x1, y1 = s[0]
                    x2, y2 = s[1]
                    if abs(y1 - y2) < 1e-9:
                        if abs(py - y1) < 1e-9 and \
                                min(x1, x2) - 1e-9 <= px <= max(x1, x2) + 1e-9:
                            return True
                    elif abs(x1 - x2) < 1e-9:
                        if abs(px - x1) < 1e-9 and \
                                min(y1, y2) - 1e-9 <= py <= max(y1, y2) + 1e-9:
                            return True
                # 端点符号豁免只给"真正压在引脚上"的首末段 —— 中间段的
                # 途经点落在符号盒内部时不得触发引脚体内豁免（否则沿符号
                # 边绕行的折线会在体内布点、整段放行，2026-09-29 实测）
                syms = end_syms if k in (0, last_k) else set()
                if seg_hits_sym(s, syms):
                    return True
            return False

        # 共线直连也要过完整检查 —— 两脚件首尾同轴时，直线会穿过同轴
        # 第三个件的符号体（支线耦合器 TLh1.2→TLh2.1 直线穿 TLh2 体，
        # 2026-09-29 实测）；被挡则落到下方两序/偏移候选。
        if abs(pa[0] - pb[0]) < 1e-9 or abs(pa[1] - pb[1]) < 1e-9:
            if not blocked([]):
                return []

        def score(wp):
            s = 0
            for s1, s2 in segs(wp):
                if abs(s1[1] - s2[1]) < 1e-9 and \
                        round(s1[1], 6) in hy.get(tag, ()):
                    s -= 10
                if abs(s1[0] - s2[0]) < 1e-9 and \
                        round(s1[0], 6) in vx.get(tag, ()):
                    s -= 10
            length = sum(abs(s1[0] - s2[0]) + abs(s1[1] - s2[1])
                         for s1, s2 in segs(wp))
            return (s, length)

        cands = [[(pb[0], pa[1])], [(pa[0], pb[1])]]
        good = [c for c in cands if not blocked(c)]
        if good:
            return min(good, key=score)
        # 2 拐弯走廊偏移:端点邻域 + 符号边距列(障碍两侧的通道,
        # 与在线 _route_around 的 margin 网格同源 —— 目标列被 y 走廊
        # 挡死时,从障碍外侧下探再横进是唯一无交叉路径)
        xs_try = [pa[0] + off for off in (1.0, -1.0, 2.0, -2.0, 3.0, -3.0)]
        y0, y1 = sorted((pa[1], pb[1]))
        for _n, box in boxes:
            if box[1] < y1 and box[3] > y0:
                xs_try.extend((box[0] - 0.25, box[2] + 0.25))
        ys_try = [pa[1] + off for off in (1.0, -1.0, 2.0, -2.0, 3.0, -3.0)]
        for _n, box in boxes:
            if box[0] < max(pa[0], pb[0]) and box[2] > min(pa[0], pb[0]):
                ys_try.extend((box[1] - 0.25, box[3] + 0.25))
        for c in ([(pa[0], yk), (pb[0], yk)] for yk in ys_try):
            if not blocked(c):
                return c
        for c in ([(xk, pa[1]), (xk, pb[1])] for xk in xs_try):
            if not blocked(c):
                return c
        # 全候选被拒（环形/密集拓扑）—— 盲选 cands[0] 会横穿符号
        # （支线耦合器 3 项已知问题根源）。改为罚分选优：收集全部
        # 候选（含被拒的），按"跨网引脚命中数 + 符号穿越数 + 跨网
        # 规划线交叉数"升序、长度次序取最小 —— 退化路线至少是
        # 违规最少的，在线 _route_clear 仍会拒并在真几何上重寻。
        def _penalty(wp):
            pts_all = [pa] + list(wp) + [pb]
            pen = 0.0
            for s1, s2 in zip(pts_all, pts_all[1:]):
                for (px, py, pn2) in pin_pts:
                    if pn2 == tag:
                        continue
                    if abs(s1[1] - s2[1]) < 1e-9:
                        if abs(py - s1[1]) < 1e-9 and                                 min(s1[0], s2[0]) - 1e-9 <= px <=                                 max(s1[0], s2[0]) + 1e-9:
                            pen += 10
                    elif abs(s1[0] - s2[0]) < 1e-9:
                        if abs(px - s1[0]) < 1e-9 and                                 min(s1[1], s2[1]) - 1e-9 <= py <=                                 max(s1[1], s2[1]) + 1e-9:
                            pen += 10
                for n2, box in boxes:
                    strict = n2 in {ka[0], kb[0]}
                    if _segment_hits_box(s1, s2, box, strict=strict):
                        pen += 5 if not strict else 2
            pen += sum(abs(s1[0] - s2[0]) + abs(s1[1] - s2[1])
                       for s1, s2 in zip(pts_all, pts_all[1:])) * 0.01
            return pen

        all_c = []
        seen_c = set()
        for wp_c in (cands + [[(pb[0], yk)] and [(pa[0], yk), (pb[0], yk)]
                              for yk in ys_try]
                     + [[(xk, pa[1]), (xk, pb[1])] for xk in xs_try]):
            key = tuple(map(tuple, wp_c))
            if key not in seen_c:
                seen_c.add(key)
                all_c.append(wp_c)
        best = min(all_c, key=_penalty)
        return best

    for i, ka, kb in conns:
        tag = net_tag(ka)
        pa, pb = pin_xy(*ka), pin_xy(*kb)
        end_syms = {ka[0], kb[0]}
        wp = plan_one(pa, pb, tag, end_syms)
        pts = [pa] + [tuple(w) for w in wp] + [pb]
        planned.append((tag, pts))
        out[i] = ([tuple(w) for w in wp], tag, [])
        for s1, s2 in zip(pts, pts[1:]):
            if abs(s1[1] - s2[1]) < 1e-9:
                hy.setdefault(tag, set()).add(round(s1[1], 6))
            elif abs(s1[0] - s2[0]) < 1e-9:
                vx.setdefault(tag, set()).add(round(s1[0], 6))

    # T 形分支顶点化(2026-09-29 用户规则 2):同 net 后画的分支端点落在
    # 母线段中部时,把该点插入母线折点 —— ADS 导线在段中部搭接的电气
    # 连通未定义,顶点化后变成端点对端点的清晰 T(保存后门禁实测拒收
    # 段中部搭接,CB_FM_Amp_AGENT (11.6,0)/(12.25,0) 三处)。
    def _split_pass():
        changed = False
        items = list(out.items())
        for idx_a, (wp_a, tag_a, _sk) in items:
            if not wp_a:
                continue
            ka = conns_map[idx_a]
            pa = pin_xy(*ka[1])
            pb = pin_xy(*ka[2])
            pts_a = [pa] + list(wp_a) + [pb]
            # 其他同 net 折线的全部顶点(引脚端点+拐点)——拐点搭母线
            # 中部同样无定义(RLOAD 从母线右段下探的 T 形,实测 (12.25,0))
            for idx_b, (wp_b, tag_b, _sk2) in items:
                if idx_b == idx_a or tag_b != tag_a or wp_b is None:
                    continue
                kb = conns_map[idx_b]
                vb = [pin_xy(*kb[1])] + list(wp_b) + [pin_xy(*kb[2])]
                for e in vb:
                    for k in range(len(pts_a) - 1):
                        s1, s2 = pts_a[k], pts_a[k + 1]
                        if e in (s1, s2):
                            continue
                        on = ((abs(s1[1] - s2[1]) < 1e-9
                               and abs(e[1] - s1[1]) < 1e-9
                               and min(s1[0], s2[0]) < e[0] < max(s1[0], s2[0]))
                              or (abs(s1[0] - s2[0]) < 1e-9
                                  and abs(e[0] - s1[0]) < 1e-9
                                  and min(s1[1], s2[1]) < e[1] < max(s1[1], s2[1])))
                        if on:
                            pts_a.insert(k + 1, e)
                            changed = True
            out[idx_a] = (pts_a[1:-1], tag_a, _sk)
        return changed

    conns_map = {i: (i, ka, kb) for i, ka, kb in conns}

    for _ in range(3):   # 一个分支点可能引发链式再分裂
        if not _split_pass():
            break
    # 同 net 重合段消除(2026-09-29 CE_FM_Amp 实测):同一 hub 的多条
    # 连接各自从引脚画到 hub,共享的走廊段会被逐条 add_wire 画成
    # 完全重合的多根导线(几何门禁"重复重合线段"硬拦截)。按计划顺序
    # 消重:路径里与已画段完全重合的段记入 _skip_segs(绘制时跳过、
    # 网络绑定保留);整条全重合才标记 _bind_only。
    _drawn = []   # [(tag, pts)] 已确认要画的折线
    # 同网最长路径先画（R20）：手工参考图的形态是"一条长线 + 引脚上的
    # T 搭"—— 最长连接整条画出，短连接路径被包含 → 整条 bind_only。
    # 旧的按计划顺序画会先画短段、长段再叠上去（三根共线重叠线，
    # 输出行实测）。段数降序 = 折线优先。
    _conn_order = sorted(
        [i for i, ka, kb in conns if out[i][0] is not None],
        key=lambda i: -len(out[i][0] or []))
    _conns_map_by_i = {i: (i, ka, kb) for i, ka, kb in conns}
    for i in _conn_order:
        _ka, _kb = _conns_map_by_i[i][1], _conns_map_by_i[i][2]
        wp, tag, _skip0 = out[i]
        ka, kb = _ka, _kb
        pts = [pin_xy(*ka)] + list(wp) + [pin_xy(*kb)]

        def _seg_dup(seg):
            for _tag2, pts2 in _drawn:
                if tag != _tag2:
                    continue
                for c, d in zip(pts2, pts2[1:]):
                    if (abs(seg[0][0] - c[0]) < 1e-9
                            and abs(seg[0][1] - c[1]) < 1e-9
                            and abs(seg[1][0] - d[0]) < 1e-9
                            and abs(seg[1][1] - d[1]) < 1e-9) or \
                            (abs(seg[0][0] - d[0]) < 1e-9
                             and abs(seg[0][1] - d[1]) < 1e-9
                             and abs(seg[1][0] - c[0]) < 1e-9
                             and abs(seg[1][1] - c[1]) < 1e-9):
                        return True
                    # 包含关系:本段与已画段共线、且整段落在已画段内部
                    # —— 同一节点两同向出线的根因(Q1.c 的 T 下短段 vs
                    # 主线长段,2026-09-29 CE 实测),短段跳过、由长线
                    # 顶点化表达同一分流
                    (x1, y1), (x2, y2) = seg
                    if abs(y1 - y2) < 1e-9 and abs(c[1] - d[1]) < 1e-9 \
                            and abs(y1 - c[1]) < 1e-9:
                        lo1, hi1 = sorted((x1, x2))
                        lo2, hi2 = sorted((c[0], d[0]))
                        if lo2 - 1e-9 <= lo1 and hi1 <= hi2 + 1e-9:
                            return True
                    if abs(x1 - x2) < 1e-9 and abs(c[0] - d[0]) < 1e-9 \
                            and abs(x1 - c[0]) < 1e-9:
                        lo1, hi1 = sorted((y1, y2))
                        lo2, hi2 = sorted((c[1], d[1]))
                        if lo2 - 1e-9 <= lo1 and hi1 <= hi2 + 1e-9:
                            return True
            return False

        skip_flags = [_seg_dup((a, b)) for a, b in zip(pts, pts[1:])]
        if all(skip_flags):
            out[i] = (None, tag, [])   # 整条已被画过:仅绑定网络
        elif any(skip_flags):
            out[i] = ([tuple(w) for w in wp], tag, skip_flags)
        _drawn.append((tag, pts))

    return out


def _rough_box_public(spec, p):
    """placed 实例的保守占位矩形(布局/走线计划共用的符号 box 近似)。

    竖放件窄高、横放件扁宽、多端件（三端管）用旋转后的引脚包围盒
    （零内边距，引脚保持在盒边上 —— 外扩 ±0.5/0.65 会把引脚全部圈进
    "体内"，触发 _segment_hits_box 的引脚体内豁免，沿符号边缘滑行或
    穿体的走线计划全被放行；BFR106 实测三脚全在真 bbox 边缘）；
    GND 由调用方特判。在线真 bbox 由 _symbol_boxes 提供,两者规则一致
    (strict 豁免语义相同)。"""
    x, y = float(p["x"]), float(p["y"])
    m = _master_short(str(spec["master"]))
    o = _AUTO_PIN_OFFS.get(m, _AUTO_PIN_OFFS_DEFAULT)
    if len(o) >= 3:
        xs, ys = [], []
        for _l, (dx, dy) in o.items():
            rx, ry = _rot_off(dx, dy, int(p.get("angle") or
                                          spec.get("angle") or 0))
            xs.append(x + rx)
            ys.append(y + ry)
        return (min(xs), min(ys), max(xs), max(ys))
    la, lb = _AUTO_AXIS_LABELS.get(m, ("1", "2"))
    ax = _rot_off(o.get(lb, (1.0, 0.0))[0] - o.get(la, (0.0, 0.0))[0],
                  o.get(lb, (1.0, 0.0))[1] - o.get(la, (0.0, 0.0))[1],
                  int(p.get("angle") or spec.get("angle") or 0))
    if abs(ax[1]) > abs(ax[0]):
        lo, hi = min(y, y + ax[1]), max(y, y + ax[1])
        return (x - 0.35, lo - 0.15, x + 0.35, hi + 0.15)
    lo, hi = min(x, x + ax[0]), max(x, x + ax[0])
    return (lo - 0.15, y - 0.45, hi + 0.15, y + 0.45)


def _ensure_cell_view(ws, library: str, cell: str, view: str) -> dict:
    """确保 lib:cell:view 存在；不存在就创建（cell 也没有就先建 cell）。"""
    try:
        probe = _open_design(library, cell, view, write=False)
        _close_design(probe)
        return {"created": False}
    except Exception:  # noqa: BLE001 — 打不开就按"需要创建"处理
        pass
    de = _de()
    try:
        lib_obj = de.get_open_library(library)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"找不到库 {library}（无法创建 cell）：{e}")
    try:
        cell_obj = lib_obj.cell(str(cell))
    except Exception:  # noqa: BLE001
        cell_obj = None
    created_cell = False
    if cell_obj is None:
        cell_obj = lib_obj.create_cell(str(cell))
        created_cell = True
    try:
        cell_obj.create_view(str(view), str(view))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"创建 {library}:{cell}:{view} 失败"
            f"（cell {'新建' if created_cell else '已存在'}）: {e}")
    return {"created": True}


def _gnd_normalized_angles(instances, connections):
    """GND 朝向规范化表 {gnd_name: angle}：地的符号体必须沿所连导线的
    延伸方向展开 —— GND 在对端引脚下方 → 270（正置接地，横线在下）；
    上方 → 90（倒置接地，横线在上）；右侧 → 0；左侧 → 180。
    GROUND 角度实测建档（2026-09-29 渲染 gnd4.pdf 逐角度提取几何）：
    0° 接入线水平向右、90° 符号体在 origin 上方、180° 向左、270° 在
    origin 下方。模型乱给 angle 会画出横着的侧面地挂在垂直导线末端
    （用户三轮截图反馈）。无连线信息的 GND 取 270（最常见正置形）。"""
    specs = {str(s["name"]): s for s in instances}
    out = {}
    gnd_names = {n for n, s in specs.items()
                 if _master_short(str(s["master"])) in _AUTO_GND_MASTERS}
    if not gnd_names:
        return out
    for conn in connections or []:
        a, b = conn.get("a"), conn.get("b")
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            continue
        na, nb = str(a[0]), str(b[0])
        if na == nb:
            continue
        for gnd, other, lbl in ((na, nb, str(b[1])), (nb, na, str(a[1]))):
            if gnd not in gnd_names or other not in specs:
                continue
            os_ = specs[other]
            po = _rot_off(*_AUTO_PIN_OFFS.get(
                _master_short(str(os_["master"])),
                _AUTO_PIN_OFFS_DEFAULT).get(lbl, (0.0, 0.0)),
                int(os_.get("angle") or 0))
            px = float(os_["x"]) + po[0]
            py = float(os_["y"]) + po[1]
            dx = float(specs[gnd]["x"]) - px
            dy = float(specs[gnd]["y"]) - py
            if abs(dy) >= abs(dx):
                out[gnd] = 90 if dy > 1e-9 else 270
            else:
                out[gnd] = 0 if dx > 1e-9 else 180
            break
    for n in gnd_names:
        out.setdefault(n, 270)
    return out


def _netlist_gate(netlist_text: str, instances: list, connections: list,
                  design_ref: str, backup: dict, gen_error: str = ""):
    """保存后网表逐引脚等价核对。返回 (problems, netlist_equiv)。

    三种情形都必须拦下、不得当成功（2026-09-30 建图验收可信度轮）：
    * 网表没拿到 —— generate_netlist 抛异常（gen_error）或返回空：
      核对无从进行，"没核过"不等于"核过没问题"，按失败处理；
    * 核对过程本身抛异常：核对没跑完，同样不放行；
    * 划分比对 / 元件参数比对发现差异：原有失败路径。
    每条问题都带设计引用与写入前备份位置，拿到错误即可定位回滚点。
    """
    ctx = ("设计 %s；写入前备份: %s"
           % (design_ref, backup.get("path") or backup.get("note") or "（无）"))
    if not netlist_text:
        reason = gen_error or "generate_netlist 返回空网表"
        equiv = {"ok": None, "diffs": [reason], "n_components": 0}
        return ([f"网表未生成，逐引脚等价核对未完成，不能确认电气拓扑："
                 f"{reason}（{ctx}）"], equiv)
    import netlist_check as _nc
    try:
        _ok, _diffs, _parsed = _nc.check_netlist_equivalence(
            netlist_text, instances, connections)
        _cdiffs = _nc.check_components(netlist_text, instances)
    except Exception as e:  # noqa: BLE001 — 核对崩溃必须拦，不能静默当通过
        equiv = {"ok": None,
                 "diffs": [f"等价核对异常: {type(e).__name__}: {e}"],
                 "n_components": 0}
        return ([f"网表等价核对未完成（核对过程异常，不能当通过）："
                 f"{type(e).__name__}: {e}（{ctx}）"], equiv)
    equiv = {"ok": _ok and not _cdiffs, "diffs": _diffs,
             "component_diffs": _cdiffs, "n_components": len(_parsed)}
    problems = []
    if _diffs:
        problems.append("网表级电气等价失败（引脚划分比对）：\n- "
                        + "\n- ".join(_diffs[:12]) + f"（{ctx}）")
    if _cdiffs:
        problems.append("网表元件/参数核对失败：\n- "
                        + "\n- ".join(_cdiffs[:12]) + f"（{ctx}）")
    return problems, equiv


def build_schematic(args: dict, ctx=None) -> dict:
    """一次性建图：放元件 + 设参数 + 连线 + 保存 + 只读复核 + 连通核对。

    保存前失败不会落盘；保存后只读复核失败时磁盘可能已有本次改动，
    因此始终在写入前备份，并阻止继续追加修改。
    """
    library = str(args.get("library") or "").strip()
    cell = str(args.get("cell") or "").strip()
    view = str(args.get("view") or "schematic").strip() or "schematic"
    instances = args.get("instances") or []
    var_spec = args.get("var")
    connections = args.get("connections") or []
    recreate = bool(args.get("recreate"))
    layout_mode = str(args.get("layout") or "").strip().lower()
    if not layout_mode:
        # 缺省入口策略（2026-09-30）：调用方完全没给 layout 时——
        #   * 实例清单非空且**全部**未给坐标：自动升到 layout="auto"
        #     （建图指导与工具描述都优先推荐 auto；忘传 layout 不再
        #     报"缺坐标"然后逼模型自拟宽松坐标）；
        #   * 任一实例给了坐标：视为有意手工布置，按 explicit 走
        #     （部分坐标 + auto 混用会把手工位置覆盖掉，必须尊重）。
        #   * 只放 var 不放实例：explicit（VAR 有缺省位）。
        _any_xy = any(isinstance(s, dict) and (s.get("x") is not None
                                               or s.get("y") is not None)
                      for s in instances)
        layout_mode = "auto" if (instances and not _any_xy) else "explicit"
    name = f"{library}:{cell}:{view}"

    if not library or not cell:
        raise RuntimeError("必须给出 library 与 cell")
    for i, spec in enumerate(instances):
        if not isinstance(spec, dict) or not spec.get("master") or not spec.get("name"):
            raise RuntimeError(f"instances[{i}] 缺少 master 或 name: {spec!r}")

    # layout="auto"：坐标交给信号流自动布局（主干一行、分支向下、正交走线）；
    # 实例带不带 x/y 都行，带了也会被覆盖。电气拓扑不受影响。
    # 端口朝向也一并规范化（仅在调用方没给 angle 时）：Term 输入 0/输出 180，
    # TermG 相反（实测其符号体在 origin 右侧、引脚在 origin 上）。
    if layout_mode in ("auto", "flow"):
        # auto 规划按无镜像几何计算引脚；mirror 实例的引脚会翻转到规划
        # 位置之外（实测 rotate-then-flip）——图形规范化剥离（电气不变）
        instances = [{k: v for k, v in s.items() if k != "mirror"}
                     for s in instances]
        positions = auto_layout_positions(instances, connections, var_spec)
        layout_report = {"method": "auto（信号流布局）"}
        # 网络级走线计划（2026-09-29 用户规则 2）:先按电气网络规划共享
        # 线段再绘制 —— 同一节点左右出线共线、T 形分支、同 net 走廊豁免
        try:
            wire_plan = plan_wire_routes(instances, connections, positions)
        except Exception as plan_err:  # noqa: BLE001 — 计划失败回退在线寻路
            wire_plan = {}
            layout_report["fallback_reason"] = (
                f"网络级走线计划失败，已回退逐条在线寻路: "
                f"{type(plan_err).__name__}: {plan_err}")
            import sys as _sys
            print(f"[build_schematic] 走线计划失败,回退逐条寻路: {plan_err}",
                  file=_sys.stderr)
        new_connections = []
        for i, c in enumerate(connections):
            m = wire_plan.get(i)
            if m:
                if m[0] is None:
                    # 走线计划判定整条路径已被同 net 前序连接画出 ——
                    # 只做网络绑定,不再画线(重复段消除,2026-09-29)
                    c = dict(c, _bind_only=True, _net=m[1])
                else:
                    c = dict(c, waypoints=[list(w) for w in m[0]])
                    c["_net"] = m[1]
                    if len(m) > 2 and any(m[2]):
                        c["_skip_segs"] = list(m[2])
            new_connections.append(c)
        connections = new_connections
        new_instances = []
        for s in instances:
            pos = positions[str(s["name"])]
            spec = dict(s, x=pos["x"], y=pos["y"])
            if spec.get("angle") is None and pos.get("angle") is not None:
                spec["angle"] = pos["angle"]
            if pos.get("annot"):
                spec["annot"] = pos["annot"]
            new_instances.append(spec)
        instances = new_instances
        if var_spec is not None:
            vname = str(var_spec.get("name") or "VAR1")
            if vname in positions:
                var_spec = dict(var_spec, x=positions[vname]["x"],
                                y=positions[vname]["y"])

    for i, spec in enumerate(instances):
        for ax in ("x", "y"):
            if spec.get(ax) is None:
                raise RuntimeError(
                    f"instances[{i}]（{spec.get('name')}）缺少坐标 {ax}；"
                    "自己排坐标请给全 x/y，或用 layout=\"auto\" 让工具按信号流自动布局")

    # GND 朝向规范化（两轮用户反馈：横着的地 / 倒挂的正地）：按连线对端
    # 引脚相对 GND 的接入方向定角度（下 0°/上 180°/右 90°/左 270°），
    # 覆盖模型乱给的 angle，mirror 同剥（对地翻转无意义）。
    gnd_angles = _gnd_normalized_angles(instances, connections)
    if gnd_angles:
        instances = [dict(s, angle=gnd_angles[str(s["name"])], mirror=None)
                     if str(s["name"]) in gnd_angles else s
                     for s in instances]
    if var_spec is not None:
        if not isinstance(var_spec, dict):
            raise RuntimeError("var 必须是对象 {name?, x?, y?, values{}}")
        if not (var_spec.get("values") or {}):
            raise RuntimeError("var.values 为空：VAR 实例至少要有一个变量")
    if not instances and var_spec is None:
        raise RuntimeError("instances 与 var 至少给一个，否则没有可构建的内容")

    # 连接去重：LLM 偶尔会把同一对引脚连两次 —— 第二条同线会被避障
    # 逼成绕行，网表不变但图面多出一条奇怪的路
    _seen_conn = set()
    _uniq_conn = []
    for _c in connections:
        try:
            k1 = (str(_c["a"][0]), str(_c["a"][1]))
            k2 = (str(_c["b"][0]), str(_c["b"][1]))
        except Exception:
            _uniq_conn.append(_c)
            continue
        if (k1, k2) in _seen_conn or (k2, k1) in _seen_conn:
            continue
        _seen_conn.add((k1, k2))
        _uniq_conn.append(_c)
    connections = _uniq_conn

    ws = _require_workspace()

    # 0) cell/view 就位（不存在则创建）
    created = _ensure_cell_view(ws, library, cell, view)

    # 1) 写入前备份；recreate 是破坏性操作，备份失败就拒绝执行
    backup = _backup_design(ws, library, cell)
    if recreate and not backup.get("backed_up"):
        if "没有可备份" not in (backup.get("note") or ""):
            raise RuntimeError(
                "recreate=True 会清空原设计，但写入前备份失败："
                f"{backup.get('note')}\n为保住用户的设计，已拒绝重建。"
                f"请先手动备份，或去掉 recreate 用 APPEND 追加。"
            )

    # 2) 打开写句柄（recreate 走 WRITE 空白覆盖；默认 APPEND 追加）
    db_uu = _db_uu()
    if recreate:
        import keysight.ads.de._pde.db as _pdb

        design = db_uu.open_design(name, mode=_pdb.DesignMode.WRITE)
    else:
        design = _open_design(library, cell, view, write=True)

    placed = []
    n_before = 0
    try:
        n_before = len(list(design.instances))
        # layout="auto" 只为"从零铺一张图"设计：规划阶段看不到设计里已有
        # 的实例，增量追加时新元件会被排到与旧元件重叠的位置。已有内容
        # 且非 recreate 时明确拒绝，指明两条安全路径。
        if layout_mode in ("auto", "flow") and n_before > 0 and not recreate:
            raise RuntimeError(
                f"设计里已有 {n_before} 个实例，layout=\"auto\" 只支持全新铺图"
                f"（recreate=True，会先备份覆盖）或空设计；增量修改请显式给"
                f"每个实例 x/y 坐标（追加模式），或改用 recreate 重建整图")

        def _apply():
            out = []
            for spec in instances:
                kw = {"name": str(spec["name"])}
                if spec.get("angle") is not None:
                    kw["angle"] = spec["angle"]
                if spec.get("mirror"):
                    kw["mirror"] = spec["mirror"]
                inst = design.add_instance(
                    str(spec["master"]), (float(spec["x"]), float(spec["y"])), **kw)
                for k, v in (spec.get("params") or {}).items():
                    _set_param(inst, k, v)
                ann = spec.get("annot")
                if ann:
                    # 文字避让偏移（相对默认位，v6 MTEE 同款机制）：
                    # 竖线下穿的元件把标注挪开，否则线穿字。
                    inst.move_annotation((float(ann[0]), float(ann[1])))
                out.append(str(spec["name"]))
            if var_spec:
                vname = str(var_spec.get("name") or "VAR1")
                vinst = design.add_instance(
                    "ads_datacmps:VAR",
                    (float(0 if var_spec.get("x") is None else var_spec["x"]),
                     float(8 if var_spec.get("y") is None else var_spec["y"])),
                    name=vname)
                for k, v in (var_spec.get("values") or {}).items():
                    vinst.vars[str(k)] = str(v)
                out.append(vname)
            scale_issue = _layout_scale_issue(design, connections)
            if scale_issue and not args.get("allow_large_geometry"):
                raise RuntimeError("建图坐标尺度异常，已阻止保存：" + scale_issue)
            # 追加模式：设计里已有旧导线，新布线必须避让 —— 从设计里现读
            routed = _design_wire_routes(design)
            for conn in connections:
                try:
                    _wire_connection(design, conn, routed)
                except RuntimeError as wire_err:
                    # 走线计划的折点被在线真 bbox 拒(保守估计与实测符号
                    # 尺寸有出入) —— 舍弃折点、保留同 net 豁免标签,让
                    # _route_around 用真实几何在线重寻。仍失败才整体失败。
                    if "指定折点" not in str(wire_err):
                        raise
                    import sys as _sys2, os as _os2
                    if _os2.environ.get("WIRE_FALLBACK_DEBUG"):
                        print(f"[wire-fallback] {conn.get('a')}->{conn.get('b')} "
                              f"拒绝原因: {wire_err}", file=_sys2.stderr)
                    _wire_connection(
                        design,
                        {k: v for k, v in conn.items() if k != "waypoints"},
                        routed)
            return out

        # 注意：**不能用 db_uu.Transaction 包裹 add_instance** ——
        # 实测（2026-09-24，tests/probe_gate_acceptance.py + G 系列实验）
        # Transaction 包裹的放置在 save_design() 后不会持久化：
        # 会话里看得到、落盘后再打开就丢了，表面上每一步都"成功"。
        # 保存前的放置失败不会落盘；保存后仍须只读复核，失败时查看备份。
        placed = _apply()
        design.save_design()
    finally:
        # 先关写句柄再重开只读（同一设计的两个句柄不能并用，实测会失效）
        _close_design(design)

    # 3) 保存后只读复核：实例、参数、连通、导线几何，一切以磁盘为准
    d2 = _open_design(library, cell, view, write=False)
    _nl = ""
    _nl_err = ""
    try:
        audit = _design_audit(d2)
        by_name = {}
        for i in list(d2.instances):
            by_name.setdefault(_inst_name(i), i)
        missing = [n for n in placed if n not in by_name]
        params_bad = _verify_params(by_name, instances)
        params_bad.extend(_verify_var_values(by_name, var_spec))
        conn_results = _check_connection_list(by_name, connections)
        geo = _geometry_report(d2)
        annot_issues = _annotation_issues(d2, by_name)
        try:
            _nl = d2.generate_netlist()
        except Exception as _ne:  # noqa: BLE001
            _nl = ""
            _nl_err = f"网表生成失败: {_ne}"
    finally:
        _close_design(d2)

    problems = []
    if audit.get("error"):
        problems.append(f"保存后审计失败: {audit['error']}")
    if missing:
        problems.append(
            "保存后重开只读看不到这些实例: "
            + str(missing)
            + "。已知原因有二：① 把 add_instance 包进了 db_uu.Transaction ——"
            " 包裹的放置 save 后会静默丢失（本工具不用 Transaction，正常不会触发）；"
            "② 在只读句柄上改了没保存。"
        )
    if not audit.get("error") and audit.get("n_instances", 0) < n_before + len(placed):
        problems.append(
            f"实例数不符：保存前 {n_before}，期望至少 {n_before + len(placed)}，"
            f"磁盘上 {audit.get('n_instances', 0)}")
    if params_bad:
        rows = [f"  - {p['instance']}.{p['param']}: 请求 {p['requested']!r}，"
                f"磁盘 {p['on_disk']!r}" for p in params_bad]
        problems.append("参数复核失败：\n" + "\n".join(rows))
    conn_fail = [c for c in conn_results if c.get("status") == "failed"]
    if conn_fail:
        rows = [f"  - {c['a']} <-> {c['b']}: {c.get('reason', '')}" for c in conn_fail]
        problems.append("连线复核失败（引脚不在同一网络）：\n" + "\n".join(rows))
    # 网表级电气等价（2026-09-29 用户规则）：按"每个网络包含哪些元件
    # 引脚"的划分比对磁盘网表 vs 请求连接清单 —— 引脚级，能抓到
    # "网络数量相同但成员划错"与"元件参数值漂移"。网表没拿到或核对
    # 本身异常同样进 problems 拦截（2026-09-30），绝不静默放行。
    # （generate_netlist 在上面 try 内执行 —— 句柄关闭后调用会报
    #   invalid Design）
    nl_problems, netlist_equiv = _netlist_gate(
        _nl, instances, connections, name, backup, gen_error=_nl_err)
    problems.extend(nl_problems)
    if geo["problems"]:
        problems.append("保存后导线几何复核失败（以下以磁盘上读到的真实几何为准）：\n- "
                        + "\n- ".join(geo["problems"]))

    result = {
        "design": name,
        "created_cell": created.get("created", False),
        "recreate": recreate,
        "layout": layout_mode,
        "backup": backup,
        "placed": placed,
        "n_instances": audit.get("n_instances", 0),
        "n_nets": audit.get("n_nets", 0),
        "connections": conn_results,
        "netlist_equivalence": netlist_equiv,
        "parameters_ok": not params_bad,
        "geometry": {
            "n_segments": geo["n_segments"],
            "problems": geo["problems"],
            "warnings": geo["warnings"],
            "unverified": geo["unverified"],
            # 图面质量指标（2026-09-30）：总长/绕行系数/拐弯/重复段/
            # 平行出线 —— 与保存后几何复核同源，验收直接可读
            "metrics": geo.get("metrics"),
        },
        # 标注归属复核（2026-09-30，渲染级兜底）：文字悬空/互压按实例
        # 点名。不拦截 —— 拦截在电气与几何 problems；规划期规则失效时
        # 这里必须可见，不能让"文字漂走"静默通过验收。
        "annotation_issues": annot_issues,
        # 布局方法与回退原因（2026-10-02）：规划期发生回退必须可见，
        # 不能只在 stderr 里一闪而过
        "layout_report": (layout_report if layout_mode in ("auto", "flow")
                          else {"method": "manual（调用方给定坐标）"}),
        # 明确边界：原理图排布 ≠ 物理版图验证
        "layout_scope": ("原理图自动排布只保证图面可读性与电气正确；"
                         "未检查铜皮、层叠、过孔与接地结构，不能据此认为 "
                         "PCB 版图通过。射频物理审查用 audit_rf。"),
        "sim_readiness": {
            # 距离"能仿真"还缺什么（此刻不拦截，run_simulation 会再拦一次）
            "problems": _gate_problems(audit, name),
            "warnings": _gate_warnings(audit),
        },
    }
    if problems:
        result["problems"] = problems
        raise RuntimeError(
            f"建图后只读复核未通过（设计 {name}）—— 已停止。"
            "请先解决下面的问题，不要在该设计上继续追加修改"
            f"（写入前副本: {backup.get('path') or backup.get('note')}；"
            "恢复方式：可对照该副本核查本机改动内容，设计版本未经确认前"
            "不要自动重跑建图）：\n\n- "
            + "\n\n- ".join(problems)
        )
    return result


def check_connections(args: dict, ctx=None) -> dict:
    """只读体检：实例 / 控制器 / 端口 / 网络 / 悬空引脚 / 基板引用 / 指定连接。"""
    library = args["library"]
    cell = args["cell"]
    view = args.get("view") or "schematic"
    connections = args.get("connections") or []
    name = f"{library}:{cell}:{view}"
    _require_workspace()
    d = _open_design(library, cell, view, write=False)
    try:
        audit = _design_audit(d)
        by_name = {}
        for i in list(d.instances):
            by_name.setdefault(_inst_name(i), i)
        conn_results = _check_connection_list(by_name, connections)
    finally:
        _close_design(d)
    return {
        "design": name,
        "n_instances": audit.get("n_instances", 0),
        "controllers": [c["name"] for c in audit.get("controllers", [])],
        "ports": [p["name"] for p in audit.get("ports", [])],
        "n_nets": audit.get("n_nets", 0),
        "floating_pins": audit.get("floating_pins", []),
        "broken_substrate_refs": audit.get("broken_substrate_refs", []),
        "connections": conn_results,
        "problems": _gate_problems(audit, name),
        "warnings": _gate_warnings(audit),
        "note": "problems 是 run_simulation 门禁会拦截的问题；warnings 只是疑点。",
    }


# ---------------------------------------------------------------------------
# simulation / dataset
# ---------------------------------------------------------------------------

def _audit_hint(audit: dict, name: str) -> str:
    """把体检结果翻译成一句人（和模型）能直接照做的话。"""
    lines = [f"设计 {name} 当前有 {audit.get('n_instances', 0)} 个实例"]
    if audit.get("controllers"):
        lines.append(f"  仿真控制器: {[c['name'] for c in audit['controllers']]}")
    if audit.get("ports"):
        lines.append(f"  端口/终端: {[p['name'] for p in audit['ports']]}")
    others = [f"{o['name']}({o['master']})" for o in audit.get("others", [])]
    if others:
        lines.append(f"  其它元件: {others}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 仿真输出目录
# ---------------------------------------------------------------------------

SIM_ROOT = "ads_agent_sim"                 # <workspace>/ads_agent_sim/<cell>_<时间戳>[_N]/
UNIQUE_TRIES = 2000                        # 同一秒内最多顺延多少个后缀
_INVALID_PATH_CHARS = '<>:"/\\|?*'


def _safe_name(name: str, fallback: str = "design") -> str:
    """把 cell 名转成可用的目录名（Windows 不允许 <>:"/\\|?* 与控制字符）。"""
    cleaned = "".join(
        "_" if (ch in _INVALID_PATH_CHARS or ord(ch) < 32) else ch for ch in str(name)
    )
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def unique_sim_dir(workspace_path: str, cell: str, now=None, tries: int = UNIQUE_TRIES) -> str:
    """为**一次**仿真创建唯一输出目录，绝不覆盖已有结果。

    为什么不能只用秒级时间戳：``strftime("%Y%m%d_%H%M%S")`` 的粒度是一秒，
    同一秒内连续跑两次同一个设计（改个参数再跑一次是最常见的用法），
    甚至两次并发仿真，都会落到同一个目录名 —— 前一次的结果被就地覆盖。

    这里的定胜负手段是 ``os.makedirs(..., exist_ok=False)`` 的**原子性**：
    目录已存在就抛 FileExistsError，于是顺延 ``_2`` / ``_3`` …。
    多个进程同时抢同一个名字时，只有一个能建成，其余自动往后排，
    因此并发也是安全的（不依赖时间戳精度，也不依赖随机数）。

    返回创建成功的目录绝对路径。
    """
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    parent = os.path.join(str(workspace_path), SIM_ROOT)
    os.makedirs(parent, exist_ok=True)

    base = f"{_safe_name(cell)}_{stamp}"
    for i in range(1, max(1, int(tries)) + 1):
        candidate = os.path.join(parent, base if i == 1 else f"{base}_{i}")
        try:
            os.makedirs(candidate, exist_ok=False)     # 原子：已存在就抛
        except FileExistsError:
            continue
        return candidate

    raise RuntimeError(
        f"无法为 {cell} 创建唯一输出目录：{parent} 下 {base} 已占用 {tries} 个同名目录。"
        f"请清理该目录后重试。"
    )


def _simulate(netlist: str, output_dir: str, dataset_name: str, netlist_path: str,
              audit: dict, note=None) -> dict:
    """耗时的仿真阶段。

    这一阶段**不接触任何 keysight.ads.de 对象**（设计在调用前已经关闭），
    输入只有网表字符串，计算由 hpeesofsim 子进程完成 —— 所以它既可以跑在
    ADS 主线程，也可以跑在后台线程，后者才不会把界面冻住。
    """
    def _note(msg: str) -> None:
        if note is None:
            return
        try:
            note(msg)
        except Exception:  # noqa: BLE001 — 记进度失败不影响仿真
            pass

    _note(f"仿真开始（网表 {len(netlist)} 字符，输出 {output_dir}）")
    _t0 = time.perf_counter()

    from keysight.edatoolbox import ads as eda

    simulator = eda.CircuitSimulator()
    try:
        simulator.run_netlist(netlist, output_dir=output_dir, dataset_name=dataset_name)
    except Exception as e:  # noqa: BLE001
        detail = str(e)
        # ExecutionError 里才有 hpeesofsim 的真实输出，不带上就等于瞎猜
        for attr in ("stderr", "output", "stdout"):
            val = getattr(e, attr, None)
            if val:
                text = val.decode("utf-8", "replace") if isinstance(val, bytes) else str(val)
                detail += f"\n--- hpeesofsim {attr} ---\n{text.strip()}"
        _note(f"仿真失败，用时 {time.perf_counter() - _t0:.1f}s")
        raise RuntimeError(
            f"仿真失败：{type(e).__name__}: {detail}\n"
            f"网表已保存到: {netlist_path or '(写入失败)'}\n"
            f"输出目录: {output_dir}"
        )
    _note(f"仿真完成，用时 {time.perf_counter() - _t0:.1f}s")

    dataset_path = os.path.join(output_dir, f"{dataset_name}.ds")
    if not os.path.exists(dataset_path):
        # fall back: scan the output dir for a .ds artifact
        for fn in os.listdir(output_dir):
            if fn.endswith(".ds"):
                dataset_path = os.path.join(output_dir, fn)
                break

    result = {
        "dataset_path": dataset_path,
        "output_dir": output_dir,
        "netlist_path": netlist_path,
        "status": "done",
        "audit": {k: audit.get(k) for k in
                  ("n_instances", "controllers", "ports", "n_nets", "warnings")},
    }
    # 工作区与设计版本：结果页跨工作区防误操作、结果复用的版本验证都靠它
    if audit.get("_workspace"):
        result["workspace"] = audit["_workspace"]
    if audit.get("_design_version"):
        result["design_version"] = audit["_design_version"]
    if not os.path.exists(dataset_path):
        result["status"] = "no_dataset"
        result["hint"] = (
            "仿真进程没有产出 .ds 数据集。打开 netlist_path 检查是否有仿真控制器与端口，"
            "并查看输出目录下的 log 文件。"
        )
        try:
            result["dir_listing"] = sorted(os.listdir(output_dir))[:40]
        except Exception:  # noqa: BLE001
            pass
    try:
        import keysight.ads.dataset as dataset

        data = dataset.open(dataset_path)
        result["variables"] = [str(k) for k in (data.keys() if hasattr(data, "keys") else data)]
    except Exception as e:
        result["variables_error"] = f"{type(e).__name__}: {e}"
    return result


def _deferred_sim(ctx, netlist, output_dir, dataset_name, netlist_path, audit) -> None:
    """后台线程入口：跑仿真并把结果回填给等待中的请求。

    无论成功失败都必须回填 —— 否则 HTTP 线程会一直等到超时，
    用户看到的是"超时"而不是真正的报错原因。
    """
    try:
        ctx.finish(_simulate(netlist, output_dir, dataset_name, netlist_path,
                             audit, note=ctx.note))
    except Exception as e:  # noqa: BLE001
        ctx.fail(str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")


def run_simulation(args: dict, ctx=None) -> dict:
    """主线程阶段：体检 → 生成网表 → 网表落盘，然后决定仿真在哪跑。

    线程要求（ADS 2027 实测 + API 语义）：
      * ``keysight.ads.de`` 的 Workspace/Design 对象必须在 Qt 主线程使用，
        所以打开设计、体检、``generate_netlist()`` 全部留在主线程；
      * ``edatoolbox`` 的 ``run_netlist`` 只接收网表字符串、由 hpeesofsim
        子进程完成计算，此时设计已经关闭、不再触碰 DE 数据库 ——
        因此可以安全地移到后台线程，主线程立刻回到事件循环。

    回退：若某个 ADS 版本在这一点上有差异（例如 run_netlist 内部又回调 DE），
    把 config.ini 的 ``[agent] sim_off_main_thread`` 设为 ``false``，
    仿真会退回主线程串行执行 —— 代价是**界面在这段时间内会无响应**，
    这是 ADS API 的硬限制，不是本插件能绕开的。
    """
    library = args["library"]
    cell = args["cell"]
    view = args.get("view") or "schematic"
    name = f"{library}:{cell}:{view}"

    ws = _require_workspace()
    design = _open_design(library, cell, view, write=False)
    try:
        audit = _design_audit(design)

        # 仿真门禁：实例数 / 控制器 / 基板引用 / 端口 全部通过才生成网表。
        # 任何一项不过就停在这里，把缺的东西点名说清楚 —— 不让 hpeesofsim
        # 用一句底层报错把整轮探索烧掉。
        problems = _gate_problems(audit, name)
        if problems:
            raise RuntimeError(
                "仿真前检查未通过，已阻止本次仿真：\n\n- " + "\n\n- ".join(problems)
            )
        audit["warnings"] = _gate_warnings(audit)

        try:
            netlist = design.generate_netlist()
        except Exception as e:
            raise RuntimeError(
                f"网表生成失败：{type(e).__name__}: {e}\n{_audit_hint(audit, name)}"
            )
    finally:
        _close_design(design)

    netlist = netlist if isinstance(netlist, str) else str(netlist)

    # 设计版本 = 网表内容指纹：设计（含 VAR 值）任何变化都会体现在网表里。
    # 结果页跨工作区防误操作、"结果复用"的有效性验证都以它为准。
    import hashlib as _hashlib
    audit["_design_version"] = {
        "netlist_sha": _hashlib.sha256(netlist.encode("utf-8")).hexdigest()[:16],
        "netlist_chars": len(netlist),
    }
    try:
        audit["_workspace"] = {
            "name": str(getattr(ws, "name", "") or ""),
            "path": str(getattr(ws, "path", "") or ""),
        }
    except Exception:  # noqa: BLE001
        audit["_workspace"] = {}

    # 唯一目录（同一秒内重复/并发跑同一设计也不会覆盖上一次的结果）
    output_dir = unique_sim_dir(ws.path, cell)

    # 网表落盘：hpeesofsim 报错时能直接打开看，不用猜
    netlist_path = os.path.join(output_dir, "netlist.ckt")
    try:
        with open(netlist_path, "w", encoding="utf-8") as f:
            f.write(netlist)
    except Exception:  # noqa: BLE001
        netlist_path = ""

    dataset_name = f"{cell}"

    # 到这里设计已关闭、手上只有网表字符串 —— 可以放后台了
    if ctx is not None and getattr(ctx, "sim_off_main_thread", False):
        ctx.defer()          # 告诉 pump：这次作业由后台线程收尾
        threading.Thread(
            target=_deferred_sim,
            args=(ctx, netlist, output_dir, dataset_name, netlist_path, audit),
            daemon=True,
            name="ads-agent-sim",
        ).start()
        ctx.note("已转入后台线程执行（ADS 主线程已释放）")
        return {"status": "running", "note": "仿真已转后台执行"}

    # 直接调用（探测脚本 / 关闭了后台仿真）：主线程串行执行
    return _simulate(netlist, output_dir, dataset_name, netlist_path, audit,
                     note=getattr(ctx, "note", None))


def read_dataset(args: dict, ctx=None) -> dict:
    path = args["path"]
    expressions = args.get("expressions") or []
    max_rows = int(args.get("max_rows") or 5)

    import keysight.ads.dataset as dataset

    data = dataset.open(path)
    try:
        all_names = [str(k) for k in (data.keys() if hasattr(data, "keys") else data)]
    except Exception:
        all_names = []

    if not expressions:
        return {"path": path, "variables": all_names, "note": "未指定 expressions，仅列出变量名"}

    out = {"path": path, "expressions": {}}
    csv_dir = os.path.dirname(path)
    for expr in expressions:
        entry = {}
        try:
            block = data[expr]
            try:
                df = block.to_dataframe().reset_index()
            except Exception:
                df = None

            if df is not None:
                cols = [str(c) for c in df.columns]
                entry["columns"] = cols
                stats = {}
                for col in cols:
                    try:
                        series = df[col]
                        if hasattr(series, "min"):
                            stats[col] = {
                                "min": _num(series.min()),
                                "max": _num(series.max()),
                                "first": _num(series.iloc[0]) if len(series) else None,
                                "last": _num(series.iloc[-1]) if len(series) else None,
                            }
                    except Exception:
                        continue
                entry["stats"] = stats
                entry["n_rows"] = int(len(df))
                sample = df.head(max_rows)
                entry["sample_rows"] = [
                    {str(c): _num(sample.iloc[i][c]) for c in sample.columns}
                    for i in range(len(sample))
                ]
                csv_path = os.path.join(
                    csv_dir, f"{expr.replace(',', '_').replace('(', '_').replace(')', '_').replace(' ', '')}.csv"
                )
                try:
                    df.to_csv(csv_path, index=False)
                    entry["csv_path"] = csv_path
                except Exception:
                    pass
            else:
                raw = getattr(block, "data", None)
                if raw is None:
                    raise RuntimeError("该变量既不支持 to_dataframe 也无 data 属性")
                entry["values_head"] = [_num(v) for v in list(raw)[:max_rows]]
        except KeyError:
            entry["error"] = f"数据集中不存在变量 {expr}；可用: {all_names}"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out["expressions"][expr] = entry
    return out


# ---------------------------------------------------------------------------
# 打开原理图（结果页的"在 ADS 中打开原理图"）
# ---------------------------------------------------------------------------

def open_schematic(args: dict, ctx=None) -> dict:
    """核对磁盘设计，并尽力激活已有的 ADS 原理图窗口（主线程）。

    ``db_uu.open_design`` 仅打开数据库句柄，不会创建或刷新 GUI 原理图窗口。
    读取实例数后要立即关闭句柄，避免妨碍之后的写入。

    至于"把原理图窗口置到最前"：那是 GUI 层的事，本机**没有验证过**
    Keysight 是否提供对应的 Python API，所以这里只用 Qt 自己的接口做一次
    尽力而为的查找（按窗口标题匹配 cell 名），并把结果如实写在
    ``activated`` / ``note`` 里 —— 不假装窗口一定会弹到最前，也不猜 API 名。
    """
    library = args["library"]
    cell = args["cell"]
    view = args.get("view") or "schematic"
    ref = f"{library}:{cell}:{view}"

    ws = _require_workspace()
    design = _open_design(library, cell, view, write=False)
    try:
        n_instances = len(list(design.instances or []))
    finally:
        _close_design(design)

    info = {
        "opened": False,
        "design_ref": ref,
        "workspace": str(ws.path),
        "activated": False,
        "n_instances": n_instances,
        "note": "",
    }

    activated = _activate_window(cell)
    if activated:
        info["activated"] = True
        info["activated_window"] = activated
        info["note"] = (
            f"已找到 {ref} 的窗口并置前；磁盘上有 {n_instances} 个实例。"
            "如果窗口里仍是旧图，请关闭该原理图窗口，再从 Library 树重新打开。"
        )
    else:
        info["note"] = (
            f"磁盘上 {ref} 有 {n_instances} 个实例，但没有找到已打开的原理图窗口。"
            f"请在 ADS 的 Library 树里双击 {cell} 的 schematic 查看。"
        )
    return info


def _activate_window(cell: str):
    """尽力把标题含 cell 名的已打开窗口置前（只用 Qt 接口，纯 GUI 操作）。

    这是**尽力而为**：ADS 的窗口结构没有公开契约，找不到就返回 None，
    由调用方如实告诉用户"请手动打开"。绝不因此报错。
    """
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return None
        wanted = str(cell or "").lower()
        if not wanted:
            return None
        for widget in app.topLevelWidgets() + app.allWidgets():
            try:
                title = widget.windowTitle() or ""
            except Exception:  # noqa: BLE001
                continue
            if wanted in title.lower():
                try:
                    widget.raise_()
                    widget.activateWindow()
                except Exception:  # noqa: BLE001
                    return None
                return title
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# 曲线读取（结果页画图 + 指标判定）
# ---------------------------------------------------------------------------

# 由表达式语法直接推出来的纵轴单位 —— 这是**确定性的字符串规则**，
# 不是对 ADS API 的猜测。数据集自己给出单位时以数据集为准。
_UNIT_BY_PREFIX = (
    ("dbm(", "dBm"),
    ("db(", "dB"),
    ("phase(", "deg"),
    ("mag(", ""),
    ("abs(", ""),
    ("real(", ""),
    ("imag(", ""),
    ("nmse(", ""),
    ("v(", "V"),
    ("i(", "A"),
)


def _expr_unit(name: str) -> str:
    low = str(name or "").strip().lower()
    for prefix, unit in _UNIT_BY_PREFIX:
        if low.startswith(prefix):
            return unit
    return ""


def _series_unit(df, col: str) -> str:
    """尝试从数据本身拿到单位。

    只用**通用**属性（pandas Series.attrs / dtype 上的 unit 属性），
    不去猜 ADS 的私有 API；拿不到就返回空串，由上层如实标注"单位未声明"。
    """
    try:
        series = df[col]
    except Exception:  # noqa: BLE001
        return ""
    for getter in (
        lambda s: (s.attrs or {}).get("unit"),
        lambda s: (s.attrs or {}).get("units"),
        lambda s: getattr(s, "unit", None),
        lambda s: getattr(s.dtype, "unit", None),
    ):
        try:
            value = getter(series)
        except Exception:  # noqa: BLE001
            continue
        if value:
            return str(value)
    return ""


def _downsample_minmax(x: list, y: list, max_points: int):
    """保极值的等宽分桶降采样（画图用；极值可靠、频点定位有桶分辨率误差）。

    与 backend/design_metrics.downsample 是同一套规则 —— ADS 进程里不能
    import backend 的模块，所以这里保留一份等价实现（有测试对照两者一致）。
    """
    n = len(x)
    if max_points <= 0 or n <= max_points:
        return list(x), list(y)
    buckets = max(1, max_points // 2)
    width = n / buckets
    ox, oy = [], []
    for b in range(buckets):
        lo = int(b * width)
        hi = int((b + 1) * width)
        if hi <= lo:
            hi = min(lo + 1, n)
        if lo >= n:
            break
        chunk = list(range(lo, min(hi, n)))
        if not chunk:
            continue
        i_min = min(chunk, key=lambda i: y[i])
        i_max = max(chunk, key=lambda i: y[i])
        for i in (i_min, i_max) if i_min <= i_max else (i_max, i_min):
            if not ox or ox[-1] != x[i]:
                ox.append(x[i])
                oy.append(y[i])
    return ox, oy


def _trace_expression(expr: str, names: list[str]) -> tuple[str, str, str]:
    """将常用 ADS S 参数表达式映射到数据块及其复数列。"""
    text = str(expr or "").strip()
    match = re.fullmatch(
        r"(?:(?P<block>[\w.]+)\.)?(?P<op>dB|mag|abs|real|imag)"
        r"\(S\((?P<row>\d+)\s*,\s*(?P<col>\d+)\)\)",
        text, re.IGNORECASE)
    if not match:
        match = re.fullmatch(
            r"(?:(?P<block>[\w.]+)\.)?S\((?P<row>\d+)\s*,\s*(?P<col>\d+)\)",
            text, re.IGNORECASE)
    if not match:
        return text, "", ""
    block = match.group("block")
    if not block:
        candidates = [name for name in names if name.endswith(".SP")]
        if len(candidates) != 1:
            raise KeyError(f"无法唯一确定 S 参数数据块；可用: {names}")
        block = candidates[0]
    op = (match.groupdict().get("op") or "mag").lower()
    return block, f"S[{int(match.group('row'))},{int(match.group('col'))}]", op


def _trace_number(value, transform: str):
    if transform:
        try:
            v = complex(value)
            if transform == "db":
                magnitude = abs(v)
                return 20 * math.log10(magnitude) if magnitude > 0 else float("-inf")
            if transform in ("mag", "abs"):
                return abs(v)
            if transform == "real":
                return v.real
            if transform == "imag":
                return v.imag
        except (TypeError, ValueError, OverflowError):
            return str(value)
    return _num(value)


def read_traces(args: dict, ctx=None) -> dict:
    """读取 .ds 数据集里的**完整曲线**：x/y 数组 + 单位 + 数据来源。

    与 read_dataset 的区别：read_dataset 给的是统计量和样例行（给人看的摘要），
    这里给的是能画图、能算指标的完整序列。判定用的指标一律基于本工具返回的
    真实数组计算，模型不得改写。

    ``max_points`` 超过时做保极值降采样并标 ``truncated``；传 0 读取完整曲线。
    调用方必须据此
    把"判定是否精确"标出来，不能拿降采样数据当完整数据宣称精确。
    """
    path = args["path"]
    expressions = args.get("expressions") or []
    requested_max = args.get("max_points")
    max_points = 50000 if requested_max is None else int(requested_max)
    if max_points < 0:
        raise ValueError("max_points 不能小于 0")

    import keysight.ads.dataset as dataset

    data = dataset.open(path)
    try:
        all_names = [str(k) for k in (data.keys() if hasattr(data, "keys") else data)]
    except Exception:  # noqa: BLE001
        all_names = []

    out = {"path": path, "variables": all_names, "traces": {}}
    for expr in expressions:
        entry = {"source": path, "expr": expr}
        try:
            block_name, component, transform = _trace_expression(expr, all_names)
            block = data[block_name]
        except KeyError as e:
            entry["error"] = f"无法读取表达式 {expr}: {e}；可用数据块: {all_names}"
            out["traces"][expr] = entry
            continue
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {e}"
            out["traces"][expr] = entry
            continue

        try:
            df = block.to_dataframe().reset_index()
        except Exception as e:  # noqa: BLE001
            entry["error"] = (f"该变量无法转成表格（to_dataframe 失败）: "
                              f"{type(e).__name__}: {e}")
            out["traces"][expr] = entry
            continue

        try:
            cols = [str(c) for c in df.columns]
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"读取列名失败: {type(e).__name__}: {e}"
            out["traces"][expr] = entry
            continue
        if not cols:
            entry["error"] = "数据集为空（没有列）"
            out["traces"][expr] = entry
            continue

        # reset_index() 之后扫频变量在第一列；纵轴优先取与表达式同名的列
        x_col = cols[0]
        y_col = component or next((c for c in cols[1:] if c == expr), None) or cols[-1]
        if y_col not in cols:
            entry["error"] = f"数据块 {block_name} 没有列 {y_col}；可用列: {cols}"
            out["traces"][expr] = entry
            continue

        try:
            x = [_num(v) for v in df[x_col].tolist()]
            y = [_trace_number(v, transform) for v in df[y_col].tolist()]
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"读取数值失败: {type(e).__name__}: {e}"
            out["traces"][expr] = entry
            continue

        keep = [(a, b) for a, b in zip(x, y)
                if isinstance(a, float) and isinstance(b, float)]
        if not keep:
            entry["error"] = "该曲线没有可用的数值点（可能全是 NaN）"
            out["traces"][expr] = entry
            continue
        x = [a for a, _ in keep]
        y = [b for _, b in keep]

        entry.update({
            "x_name": x_col,
            "y_name": y_col,
            "x": x,
            "y": y,
            "n_points": len(x),
            "columns": cols,
            "x_unit": _series_unit(df, x_col),
            "y_unit": "dB" if transform == "db" else (_series_unit(df, y_col) or _expr_unit(expr)),
        })
        if max_points > 0 and len(x) > max_points:
            xs, ys = _downsample_minmax(x, y, max_points)
            entry["x"], entry["y"] = xs, ys
            entry["truncated"] = True
        out["traces"][expr] = entry
    return out


def _num(v):
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return str(v)
        return f
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# escape hatch
# ---------------------------------------------------------------------------

# 跨调用共享的执行环境（见 _python_env 注释）
_PY_ENV: dict | None = None


def _trim_traceback(tb_text: str) -> str:
    """只保留用户代码那几帧，去掉 exec/服务端自己的调用栈噪声。"""
    lines = tb_text.strip().splitlines()
    for i, line in enumerate(lines):
        # 注意要匹配 `File "<ads_agent>"`，只匹配 `<ads_agent>` 会命中外层
        # exec(...) 的源码行，多带两行噪声进来
        if 'File "<ads_agent>"' in line:
            return "[调用栈]\n" + "\n".join(lines[i:])
    return "\n".join(lines[-12:])


def install_env_helpers(env: dict) -> None:
    """把 run_python 的辅助函数（重新）绑定进执行环境。

    独立成模块级函数的原因：toolserver 热重载 ads_ops 时会**原样保留**
    _PY_ENV（用户的变量、import 都在里面），但旧环境里的闭包还是旧代码 ——
    实测（2026-09-28）旧 connect/wire 绕过新门禁照画斜线。重载后调本函数
    重绑全部助手：用户变量不丢，助手永远与磁盘上的 ads_ops 一致。
    """
    import inspect

    # 每次重绑都重取真实模块：执行环境里的 de/db_uu 可能被脚本里的
    # `import de` 污染（ADS 安装根目录下的 de 会被解析成同名命名空间
    # 包，实测发生过），重绑即自愈。
    try:
        import keysight.ads.de as de

        env["de"] = de
    except Exception as e:  # noqa: BLE001
        env.setdefault("de", None)
        env.setdefault("ADS_IMPORT_ERROR", f"{type(e).__name__}: {e}")
    try:
        import keysight.ads.de.db_uu as db_uu

        env["db_uu"] = db_uu
    except Exception:  # noqa: BLE001
        env.setdefault("db_uu", None)

    def ws():
        """当前工作区对象（未打开则返回 None）。"""
        de = env.get("de")
        if de is None:
            return None
        if not de.workspace_is_open():
            return None
        return de.active_workspace()

    def ls(obj=None, only_callable: bool = False, limit: int = 80):
        """不传参 -> 环境速览（工作区/库）；传对象 -> 列出成员名。"""
        if obj is None:
            w = ws()
            if w is None:
                return {"error": "当前没有打开的工作区"}
            try:
                return {
                    "workspace": str(w.path),
                    "libraries": list(w.library_names or []),
                    "writable": list(w.writable_library_names or []),
                }
            except Exception as e:  # noqa: BLE001
                return {"error": f"{type(e).__name__}: {e}"}
        names = [n for n in dir(obj) if not n.startswith("_")]
        if only_callable:
            names = [n for n in names if callable(getattr(obj, n, None))]
        return sorted(names)[:limit]

    def sig(obj=None, name: str | None = None) -> str:
        """sig(f) 或 sig(cell, "create_view") —— 直接拿到方法签名，别再靠试。"""
        target = getattr(obj, name) if (name and obj is not None) else obj
        try:
            return f"{name or getattr(target, '__name__', '')}{inspect.signature(target)}"
        except Exception as e:  # noqa: BLE001
            return f"<无法取签名: {type(e).__name__}: {e}>"

    # ---- 以下 helper 全部按 ADS 2027 实测 API 编写，见 tests/probes/ ----
    def dm(write: bool = False):
        return _design_mode(write)

    def odesign(lib, cell, view="schematic", write=False):
        """按正确方式打开设计（open_design 的 mode 必须给 DesignMode 枚举）。

        write=True 走 APPEND（追加），并且**先自动把该 cell 的磁盘目录备份**到
        <workspace>/ads_agent_backups/ —— 备份路径会 print 出来。
        如果要「从零重建」一个已有设计，显式传 recreate(lib, cell)。
        """
        if write:
            w = ws()
            if w is not None:
                info = _backup_design(w, str(lib), str(cell))
                if info.get("backed_up"):
                    print(f"[backup] 写入前已留存副本: {info['path']}")
                elif info.get("note"):
                    print(f"[backup] {info['note']}")
        db_uu = env.get("db_uu") or importlib.import_module("keysight.ads.de.db_uu")
        return db_uu.open_design(f"{lib}:{cell}:{view}", mode=_design_mode(write))

    def recreate(lib, cell, view="schematic"):
        """从零重建：拿到一个空白可写设计，save() 后原内容被替换。

        只在明确要「推倒重来」时用。误用会静默清空设计 —— 所以这里先强制
        备份，备份失败（磁盘上有内容却备不走）就拒绝，绝不裸跑。
        """
        w = ws()
        info = _backup_design(w, str(lib), str(cell)) if w is not None else {
            "backed_up": False, "path": "", "note": "没有打开的工作区"}
        if info.get("backed_up"):
            print(f"[backup] 重建前已留存副本: {info['path']}")
        elif "没有可备份" not in (info.get("note") or ""):
            raise RuntimeError(
                f"recreate 会清空 {lib}:{cell}，但写入前备份失败：{info.get('note')}\n"
                f"已拒绝重建。请先手动备份，或改用 odesign(lib, cell, write=True) 追加。"
            )
        db_uu = env.get("db_uu") or importlib.import_module("keysight.ads.de.db_uu")
        import keysight.ads.de._pde.db as pdb

        return db_uu.open_design(f"{lib}:{cell}:{view}", mode=pdb.DesignMode.WRITE)

    def backup(lib, cell):
        """手动给 cell 留一份副本，返回备份报告（path 在 backed_up=True 时有效）。"""
        w = ws()
        if w is None:
            return {"backed_up": False, "path": "", "note": "没有打开的工作区"}
        info = _backup_design(w, str(lib), str(cell))
        if info.get("backed_up"):
            print(f"[backup] 已留存副本: {info['path']}")
        else:
            print(f"[backup] 未备份: {info.get('note', '')}")
        return info

    def save_verify(lib, cell, view="schematic", expect_instances=None):
        """保存后的只读复核（**先 close 写句柄再调用**，两个句柄不能并用）。

        重开只读检查：设计非空、实例数与期望一致、导线几何（斜段/穿符号/
        交叉/重叠，读不到的项如实标"未验证"）；缺基板引用 / 悬空引脚等
        疑点会打印出来。复核不过就抛错 —— 此时不要继续画图，先修。
        """
        d = odesign(lib, cell, view, write=False)
        try:
            a = _design_audit(d)
            geo = _geometry_report(d)
        finally:
            d.close_design()
        n = a.get("n_instances", 0)
        problems = []
        if a.get("error"):
            problems.append(f"只读打开审计失败: {a['error']}")
        elif n == 0:
            problems.append("磁盘上的设计是空的（0 个实例）—— 修改没有落盘")
        if expect_instances is not None and n != int(expect_instances):
            problems.append(f"实例数不符：磁盘 {n}，期望 {expect_instances}")
        problems.extend(geo["problems"])
        for w in geo["warnings"]:
            print("[geometry] 疑点:", w)
        for u in geo["unverified"]:
            print("[geometry] 未验证:", u)
        for w in _gate_warnings(a):
            print("[verify] 疑点:", w)
        if problems:
            raise RuntimeError("保存后复核未通过：\n- " + "\n- ".join(problems))
        print(f"[verify] {lib}:{cell}:{view} 落盘正常：{n} 实例，"
              f"控制器 {[c['name'] for c in a.get('controllers', [])]}，"
              f"端口 {len(a.get('ports', []))}，网络 {a.get('n_nets', 0)}，"
              f"导线 {geo['n_segments']} 段（几何复核通过）")
        return a

    def put(design, master, x, y, name=None, angle=None, mirror=None):
        """放元件。master 用 '库:cell' / '库:cell:view'；原点用普通元组即可。"""
        kw = {}
        if name:
            kw["name"] = name
        if angle is not None:
            kw["angle"] = angle
        if mirror:
            kw["mirror"] = mirror
        return design.add_instance(master, (float(x), float(y)), **kw)

    def wire(design, points):
        """连线：wire(d, [(x1,y1), (x2,y2), ...])，坐标用元组。

        逐段硬门禁：斜段、穿符号、与已有导线交叉/重叠都会直接报错
        （原设计未动）——实测 add_wire 端点不共轴会画斜线，这里不再放行。
        实现委托给模块级 wire_impl：ads_ops 热重载后本闭包仍调到新实现
        （_PY_ENV 会被 toolserver 原样保留，闭包里的函数引用不能固化）。
        """
        import ads_ops
        return ads_ops.wire_impl(design, points)

    def connect(design, inst_a, pin_a, inst_b, pin_b):
        """连接两个实例的指定引脚：正交寻径 + 画线 + 显式绑定网络（缺一不可）。

        端点不共轴自动加 90° 拐点；找不到不穿元件、不与已有导线交叉的
        正交路径就报错 —— 绝不退化为斜线（Wilkinson_1G7_ML 的 Riso 斜线
        就是旧版 connect 直连两个不共轴引脚画出来的，2026-09-28 用户截图
        点名）。引脚可用编号（1、2）或名字（'P1'）。委托 connect_impl，
        热重载安全（同 wire）。
        """
        import ads_ops
        return ads_ops.connect_impl(design, inst_a, pin_a, inst_b, pin_b)

    def save(design):
        """保存：先过导线几何硬门禁（斜线/穿符号/异网交叉在落盘前拦截，
        磁盘原设计不受影响），再 save_design()。注意是 save_design()，
        Design 没有 .save()。

        只读设计上保存会抛 "Attempt to save a read-only design"，此时之前放的
        元件全部作废——这里把话说清楚，免得模型以为保存成功了。
        保存后请 close 写句柄，再调 save_verify(lib, cell) 复核真的落盘了。
        实现委托给模块级 save_impl：ads_ops 热重载后本闭包仍调到新实现。
        """
        import ads_ops
        return ads_ops.save_impl(design)

    def audit(lib, cell, view="schematic"):
        """仿真前自检：实例数 / 仿真控制器 / 端口。缺什么一眼看到。"""
        d = odesign(lib, cell, view, write=False)
        try:
            return _design_audit(d)
        finally:
            try:
                d.close_design()
            except Exception:  # noqa: BLE001
                pass

    def mkcell(lib, cell, view="schematic"):
        """新建 cell + schematic 视图，返回 (cell, view)。

        Library 是 create_cell(name)，Cell 是 create_view(view_name, view_type_name)。
        """
        de = env.get("de")
        L = de.get_open_library(lib)
        c = L.create_cell(cell)
        v = c.create_view(view, view)
        return c, v

    def setp(inst, name, value):
        """设置实例参数（详见 _set_param；自动给字符串型参数补双引号）。"""
        return _set_param(inst, name, value)

    def params(inst):
        """列出实例全部参数：[(名字, 当前值), ...]。不确定参数名就先调这个。"""
        out = []
        for p in list(inst.parameters):
            try:
                out.append((str(getattr(p, "name", "")), str(getattr(p, "value", ""))))
            except Exception as e:  # noqa: BLE001
                out.append((str(getattr(p, "name", "")), f"<{type(e).__name__}>"))
        return out

    def pins(inst):
        """列引脚。InstPin 没有 .name —— 名字要从 inst_term 的
        term_name（具名）或 term_number（编号）取；编号引脚读 term_name 会抛异常。"""
        out = []
        for p in list(inst.inst_pins):
            term = getattr(p, "inst_term", None)
            label = _pin_label(p)
            snap = getattr(p, "snap_point", None)
            try:
                xy = _xy_point(snap) if snap is not None else None
            except Exception:  # noqa: BLE001
                xy = None
            out.append({"label": label, "master_pin": str(getattr(p, "master_pin", "")),
                        "net": _net_label(getattr(p, "net", None)), "xy": xy})
        return out

    def libs():
        w = ws()
        if w is None:
            return {"error": "当前没有打开的工作区"}
        return {str(l.name): len(list(l.cells or [])) for l in (w.libraries or [])}

    def cells(lib, contains=None):
        w = ws()
        if w is None:
            return {"error": "当前没有打开的工作区"}
        for l in w.libraries or []:
            if str(l.name) == str(lib):
                names = [str(c.name) for c in (l.cells or [])]
                if contains:
                    return [n for n in names if str(contains).lower() in n.lower()]
                return names
        return {"error": f"工作区里没有库 {lib}", "已挂载": list(w.library_names or [])}

    # 常用类型：PointF 在 keysight.ads.de._points（不是 db_uu）
    try:
        env["PointF"] = importlib.import_module("keysight.ads.de._points").PointF
    except Exception:  # noqa: BLE001
        pass
    try:
        env["Orientation"] = importlib.import_module("keysight.ads.de._pde.db").Orientation
    except Exception:  # noqa: BLE001
        pass

    env.update({"ws": ws, "ls": ls, "sig": sig, "dm": dm, "odesign": odesign,
                "put": put, "wire": wire, "save": save, "pins": pins,
                "connect": connect, "libs": libs, "cells": cells,
                "audit": audit, "mkcell": mkcell, "setp": setp,
                "params": params, "recreate": recreate,
                "save_verify": save_verify, "backup": backup})


def _python_env() -> dict:
    """跨调用共享的 run_python 执行环境。

    之前每次调用都是全新的 globals：模型上一次查到的对象、导入的模块全丢，
    被迫在每一次调用里重复 `import` 和重新查找——这正是它把有限的步数消耗在
    「一个 dir() 一次调用」上的根本原因。这里保留一个常驻环境，并预置
    常用 ADS API 与辅助函数（见 install_env_helpers；ads_ops 热重载后
    toolserver 会对保留的环境重绑助手，见该函数的注释）。
    """
    global _PY_ENV
    if _PY_ENV is not None:
        return _PY_ENV

    import json as _json
    import math
    import traceback as _tb

    env = {
        "__name__": "__ads_agent__",
        "os": os,
        "json": _json,
        "math": math,
        "inspect": __import__("inspect"),
        "traceback": _tb,
    }

    # 预置常用 ADS API；导入失败也不影响其它能力（把原因留在变量里）
    try:
        import keysight.ads.de as de

        env["de"] = de
    except Exception as e:  # noqa: BLE001
        env["de"] = None
        env["ADS_IMPORT_ERROR"] = f"{type(e).__name__}: {e}"
    try:
        import keysight.ads.de.db_uu as db_uu

        env["db_uu"] = db_uu
    except Exception as e:  # noqa: BLE001
        env["db_uu"] = None

    install_env_helpers(env)
    _PY_ENV = env
    return env


def run_python(args: dict, ctx=None) -> dict:
    import io
    import traceback as _tb
    from contextlib import redirect_stdout

    code = args.get("code") or ""
    if not code.strip():
        raise RuntimeError("code 为空")
    buf = io.StringIO()
    env = _python_env()
    env["print"] = _make_print(buf)  # 每次指向新的缓冲
    try:
        with redirect_stdout(buf):
            exec(compile(code, "<ads_agent>", "exec"), env)  # noqa: S102 — by design
    except BaseException as e:  # 用户脚本的 SystemExit 也必须变成工具结果
        # 之前只回一句 "TypeError: ..."，模型看不到是哪一行、哪个参数，
        # 只能换写法重试；现在把用户代码的调用栈一起给它。
        buf.write(f"\n[异常] {type(e).__name__}: {e}\n")
        buf.write(_trim_traceback(_tb.format_exc()))
        return {"stdout": buf.getvalue(), "ok": False}
    out = buf.getvalue()
    if not out.strip():
        out = "(无输出；若需查看结果请 print 出来)"
    return {"stdout": out, "ok": True}


def _make_print(buf):
    import builtins

    def print(*a, **kw):  # noqa: A001 — shadow inside exec globals only
        builtins.print(*a, file=buf, **kw)

    return print


# ---------------------------------------------------------------------------
# RF 物理审查（需求→原理图→Layout→验证 闭环的检查入口）
#
# 两种布局的边界（系统提示与 docs/Layout 审查设计.md 同文）：
#   原理图布局 = 信号流/可读性/引脚方向/接地位置；导线长度只是绘图坐标。
#   ADS Layout = 实际铜皮的宽度/间隙/长度/拐角/接地回流/制造约束。
# 纯计算逻辑在 rf_audit.py（可离线测试）；这里只做 ADS 对象的读取适配。
# ---------------------------------------------------------------------------

def _inst_params(inst) -> dict:
    params = {}
    try:
        for p in list(inst.parameters):
            nm = str(getattr(p, "name", ""))
            if nm:
                params[nm] = str(getattr(p, "value", ""))
    except Exception:  # noqa: BLE001
        pass
    return params


def _rf_pin_entries(inst) -> list:
    out = []
    try:
        plist = list(inst.inst_pins)
    except Exception:  # noqa: BLE001
        return out
    for p in plist:
        try:
            net = _net_label(getattr(p, "net", None))
        except Exception:  # noqa: BLE001
            net = ""
        xy = None
        snap = getattr(p, "snap_point", None)
        if snap is not None:
            try:
                xy = _xy_point(snap)
            except Exception:  # noqa: BLE001
                xy = None
        out.append({"label": _pin_label(p), "net": net, "xy": xy})
    return out


def _rf_origin(inst):
    try:
        o = getattr(inst, "origin", None)
        return _xy_point(o) if o is not None else None
    except Exception:  # noqa: BLE001
        return None


def _rf_freq(entries: list) -> dict:
    """从控制器实例参数解析工作频率（S_Param 取 Start/Stop，AC 取 Freq）。"""
    start = stop = None
    notes = []
    for e in entries:
        if e["master"] not in _SIM_CONTROLLERS:
            continue
        p = e["params"]
        if "S_Param" in e["master"] or e["master"] in {"SP", "SP_NWA"}:
            f1, n1 = rf_audit.parse_freq_hz(p.get("Start"))
            f2, n2 = rf_audit.parse_freq_hz(p.get("Stop"))
            if f1 and f2:
                start, stop = min(f1, f2), max(f1, f2)
            notes += [n for n in (n1, n2) if n]
        elif e["master"] in {"AC", "HB", "HarmonicBalance"}:
            f1, n1 = rf_audit.parse_freq_hz(p.get("Freq"))
            if f1:
                start = stop = f1
            if n1:
                notes.append(n1)
    if start is None:
        notes = ["设计里没有找到可读的频率控制器，电长度无法计算"]
    out = {}
    if start is not None:
        out["start_hz"] = start
        out["stop_hz"] = stop
        out["center_hz"] = (start + stop) / 2
    if notes:
        out["notes"] = notes
    return out


def _rf_reference_ohm(entries: list):
    """端口参考阻抗：取第一个 Term 的 Z 参数数值（缺省视为 50）。"""
    for e in entries:
        if e["master"] in _PORT_CELLS:
            raw = (e.get("params") or {}).get("Z")
            if raw is None:
                continue
            m = re.search(r"[-+]?[0-9]*\.?[0-9]+", str(raw))
            if m:
                return float(m.group(0))
    return None


def _rf_inventory(design, library: str, cell: str) -> dict:
    """从原理图设计对象抽取审查所需的普通数据（纯 dict，喂给 rf_audit）。"""
    insts = []
    substrates = {}
    vars_table = {}
    for inst in list(design.instances or []):
        entry = {
            "name": _inst_name(inst),
            "master": _inst_master(inst),
            "params": _inst_params(inst),
            "pins": _rf_pin_entries(inst),
            "origin": _rf_origin(inst),
        }
        if entry["master"] in rf_audit._SUBSTRATE_MASTERS:
            substrates[entry["name"]] = {
                k: entry["params"].get(k)
                for k in ("H", "Er", "T", "TanD", "Rho", "Cond") if k in entry["params"]
            }
        try:
            if getattr(inst, "is_var_instance", False):
                for k, v in dict(getattr(inst, "vars", {}) or {}).items():
                    vars_table[str(k)] = str(v)
        except Exception:  # noqa: BLE001
            pass
        insts.append(entry)
    return {
        "design": f"{library}:{cell}:schematic",
        "instances": insts,
        "substrates": substrates,
        "vars": vars_table,
        "freq": _rf_freq(insts),
        "reference_ohm": _rf_reference_ohm(insts),
    }


def _outline_points(ol) -> list:
    """Outline → [(x, y), ...]。暴露方式在不同构建里不统一，逐个尝试。"""
    for attr in ("points", "vertices", "corners"):
        v = getattr(ol, attr, None)
        if v is not None:
            try:
                return [_xy_point(p) for p in list(v)]
            except Exception:  # noqa: BLE001
                pass
    try:
        return [_xy_point(p) for p in list(ol)]
    except Exception:  # noqa: BLE001
        return []


_LAYOUT_KIND = {"Polygon": "path", "Rect": "rect", "Dot": "dot"}


def _extract_layout(library: str, cell: str) -> dict:
    """读真实 Layout 图形。视图不存在/为空时如实返回，绝不编造几何。"""
    try:
        d = _open_design(library, cell, "layout", write=False)
    except Exception as e:  # noqa: BLE001
        return {"available": False,
                "reason": f"没有 layout 视图或打开失败（{type(e).__name__}: {e}）"}
    try:
        shapes = []
        for s in list(d.shapes):
            kind = _LAYOUT_KIND.get(type(s).__name__, "other")
            bbox = None
            try:
                b = s.bbox
                ll, ur = _xy_point(b.lower_left), _xy_point(b.upper_right)
                bbox = [ll[0], ll[1], ur[0], ur[1]]
            except Exception:  # noqa: BLE001
                pass
            outline = None
            if kind != "dot":
                try:
                    pts = _outline_points(s.get_outline())
                    outline = [[round(x, 4), round(y, 4)] for x, y in pts]
                except Exception:  # noqa: BLE001
                    outline = None
            width = None
            try:
                width = float(getattr(s, "width", None) or 0.0)
            except Exception:  # noqa: BLE001
                pass
            layer = str(getattr(s, "layer_id", "?"))
            shapes.append({"kind": kind, "layer": layer, "bbox": bbox,
                           "outline": outline, "width": width})
        try:
            insts = [{"name": _inst_name(i), "master": _inst_master(i)}
                     for i in list(d.instances or [])]
        except Exception:  # noqa: BLE001
            insts = []
        try:
            terms = [str(getattr(t, "name", "")) for t in (d.terms or [])]
        except Exception:  # noqa: BLE001
            terms = []
        return {"available": True,
                "empty": not shapes and not insts,
                "shapes": shapes, "instances": insts, "terms": terms}
    finally:
        _close_design(d)


def audit_rf(args: dict, ctx=None) -> dict:
    """射频物理审查入口：原理图级（传输线/MTEE/直角连接）+ Layout 级。

    返回的每条 finding 都带「验证状态」；Layout 拿不到时如实标注
    「未完成 Layout 验证」，LineCalc 不可程序化调用时绝不出示臆造的 W/G/L。
    """
    library = args["library"]
    cell = args["cell"]
    _require_workspace()

    d = _open_design(library, cell, "schematic", write=False)
    try:
        inv = _rf_inventory(d, library, cell)
        sch_names = [i["name"] for i in inv["instances"]]
    finally:
        _close_design(d)

    sch = rf_audit.audit_schematic(inv)
    lay_data = _extract_layout(library, cell)
    lay_data["schematic_names"] = sch_names
    lay = rf_audit.analyze_layout(lay_data)

    findings = list(sch["findings"]) + list(lay.get("findings") or [])
    pending = [f for f in findings
               if str(f.get("verification", "")).startswith("未")]
    summary = {
        "n_findings": len(findings),
        "n_error": sum(1 for f in findings if f.get("severity") == "error"),
        "n_warning": sum(1 for f in findings if f.get("severity") == "warning"),
        "n_info": sum(1 for f in findings if f.get("severity") == "info"),
        "n_pending": len(pending),
    }
    return {
        "design": f"{library}:{cell}",
        "principles": sch["principles"],
        "frequency": sch.get("frequency"),
        "tlines": sch.get("tlines", []),
        "layout_verdict": lay.get("verdict"),
        "findings": findings,
        "summary": summary,
        "linecalc": {
            "available": False,
            "evidence": ("keysight.ads Python API 无 LineCalc/合成接口（文档索引 0 命中）；"
                         "安装目录仅有 linecalc.exe 独立 GUI 程序"),
            "how_to": "需要精确合成时请用户在 ADS 里 Tools ▸ LineCalc ▸ Start LineCalc 手工操作",
        },
        "em": lay.get("em") or {
            "available": "emtools.create_empro_view 存在，但本设计暂无可用 EM 流程",
            "note": "EM 仿真自动化未实现，电路与 EM 对比未完成",
            "verified": False,
        },
        "note": ("原理图导线长度只是绘图坐标，不代表微带线物理长度；"
                 "Layout 未生成时本报告不含任何物理铜皮结论"),
    }


def design_fingerprint(args: dict, ctx=None) -> dict:
    """只读：返回当前工作区信息与指定设计的网表指纹（不做任何修改）。

    用途：结果页"结果复用"的版本验证 —— 发布结果时记录仿真时的网表指纹，
    复用前重算一次，不一致说明设计（参数/结构/VAR）已经变化，旧数据集
    不能再代表当前设计。同时带回工作区路径，防止跨工作区误操作同名设计。
    """
    library = args["library"]
    cell = args["cell"]
    view = args.get("view") or "schematic"
    name = f"{library}:{cell}:{view}"

    ws = _require_workspace()
    out = {
        "workspace": {
            "name": str(getattr(ws, "name", "") or ""),
            "path": str(getattr(ws, "path", "") or ""),
        },
    }
    design = _open_design(library, cell, view, write=False)
    try:
        try:
            netlist = design.generate_netlist()
        except Exception as e:
            # 指纹拿不到就明说 —— 绝不能返回一个空指纹让调用方误判"没变"
            out["error"] = f"网表生成失败：{type(e).__name__}: {e}"
            out["design_version"] = {}
            return out
    finally:
        _close_design(design)
    netlist = netlist if isinstance(netlist, str) else str(netlist)
    import hashlib as _hashlib
    out["design_version"] = {
        "netlist_sha": _hashlib.sha256(netlist.encode("utf-8")).hexdigest()[:16],
        "netlist_chars": len(netlist),
    }
    out["design_ref"] = name
    return out


DISPATCH = {
    "get_workspace_info": get_workspace_info,
    "list_designs": list_designs,
    "get_design_variables": get_design_variables,
    "set_design_variables": set_design_variables,
    "build_schematic": build_schematic,
    "check_connections": check_connections,
    "run_simulation": run_simulation,
    "read_dataset": read_dataset,
    "read_traces": read_traces,
    "open_schematic": open_schematic,
    "run_python": run_python,
    "audit_rf": audit_rf,
    "design_fingerprint": design_fingerprint,
}
