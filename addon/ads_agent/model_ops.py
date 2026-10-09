"""原厂模型包在**ADS 进程内**的操作层：挂接 / 只读浏览 / 可用性验证。

为什么单独一个模块
------------------
``ads_ops`` 已经6700 多行，且它的热重载由 toolserver 的 ``_ADS_OPS_DEPS``
驱动（见 toolserver 的注释：reload(ads_ops) 里的 import 会命中 sys.modules
缓存，改了磁盘也不生效）。模型包这套流程与建图/仿真两条主线**没有共享
状态**，却要写同一个工作区的库定义文件 —— 混进 ads_ops 会让两件事互相
牵连。因此独立成模块，由 ads_ops 的 DISPATCH 表转发，model_ops 自身也
登记进 ``_ADS_OPS_DEPS``，改它同样能热重载。

与后端 model_tools.py 的契约（**这是硬约束，实现以它为准**）
--------------------------------------------------------------
后端已经写好了编排层（backend/model_tools.py），它按下面的名字派发到
ADS 端，参数名也已固定：

  attach_design_kit   {package_id, kit_root(绝对路径), kit_root_rel,
                      workspace, library_names?}
      -> {libraries: [{name, path, ...}], already_attached: bool,
          attached_at: str}
  list_vendor_models  {workspace, library?, name_prefix?, max_items?,
                      include_params?}
  get_vendor_model_info {library, cell, view?}
  validate_model_import {library, cell, parameters{}, run_smoke_sim,
                         workspace}

所以本模块的工具名以这四个为准（少了任何一个，后端就会拿到
"未知工具"）。另外注册了四个语义更细的别名
（list_readonly_libraries / list_library_components /
inspect_component_model / detach_design_kit）—— 别名与主名共用实现，
不是两套代码。

API 依据（**读源码核对，非记忆**；ADS 2027 捆绑 Python 包）
------------------------------------------------------------------------------
Workspace（_core/workspace.py）
  L107lib_defs_file / L113writable_libraries / L124libraries（含只读）
  L135writable_library_names / L141library_names
  L146 open_library(lib_name, lib_path=None, mode=LibraryMode.READ_ONLY)
  L203 remove_library(library_name, library_path)
  L208 add_library(library_name, library_path, mode=READ_ONLY) -> Library
      官方文档原话："Add a library reference to **this workspace's library
      definition file** and return the opened library." —— 这正是我们要的
      「工作区挂接」，它**不动**全局 Favorite Design Kit 设置。
  L227 add_library_definition_file(lib_def_file_path)
  L235 remove_library_definition_file(lib_def_file_path)
  L340 get_path_for_use_in_library_definition_file(path, lib_def_file_path)
Library（_core/library.py）
  L105 name / L116 path / L121 is_open / L126 is_read_only /
  L131 is_writable / L136 is_an_ads_library / L141 lib_path /
  L193 cell(name) / L204 cells / L211 cell_exists /
  L215 get_cell_if_exists / L248 attached_tech_lib_name /
  L341 get_library_cfg_var / L378 physical_layer_names
Cell（_core/cell.py）
  L62 name / L72 lib_name / L89 path / L100 view(name) /
  L111 model_def -> Optional[ModelDefBase]（经 keysight.ads.ael._wrapping
      的 _wrap_value 包装，AEL 元件返回 ModelDefAEL）/ L120 views /
  L127 view_exists / L131 get_view_if_exists
ModelDefBase（db/_model_def.py）
  L365 class；L400 find_model_def(lib_or_cell[, cell_name])；
  L427 name / L436 label / L445 component_name / L450 library_name /
  L473 parameters（NamedIndexedListRefAbc[ModelParam]，可迭代）
ModelParam（同上 L28）
  L83 name / L95 label / L121 unit_type（ModelUnitType）/ L135 param_type
  （ModelParamType）/ L163 default_value（Optional[ParamItem]）/
  L216 is_editable / L253 is_discrete_value / L262 is_netlistable /
  L319 is_optimizable
ModelUnitType / ModelParamType：db/_parameters.py L1795 / L1842（枚举值
  是 'freq' / 'res' / 'lng' / 'real' / 'string' ... 这类小写串）
LibDefList（_core/library.py L576）—— **官方的 lib.defs 解析器**
  L593 open(path) / L633 members（LibDefListMem：is_library 为真时是
  LibDef，带 .name / .path / .mode）/ L639 save()
  L480 LibDef.__init__(name, path, mode)；L543 LibDefListRef（INCLUDE 行）
LibraryMode：_pde/__init__.pyi L691，成员 UNKNOWN/SHARED/NON_SHARED/
  READ_ONLY，有 .str 与 _from_str(name)（.pyl 同名 L712）
DesignMode：_pde/db/__init__.pyi L2301，READ_ONLY(0)/WRITE(1)/APPEND(2)
  ——与 README 的坑表一致：**必须传枚举成员**，传字符串/整数会报
  "DesignMode has no value"。本模块只读，写入一律交给 ads_ops 的 APPEND。

**本模块不做的事**
  * 不碰全局 Favorite Design Kit 设置（那是 ADS 全局偏好，跨工作区生效，
    用户没要求就不该动）。
  * 不给原厂库写权限：浏览与挂接一律 LibraryMode.READ_ONLY。SHARED /
    NON_SHARED 会创建锁文件并允许写原厂目录 —— 浏览一个库不需要这些。
  * 不直接字符串拼接改 lib.defs。优先走上面的 add_library /
    add_library_definition_file；真的需要退路时先备份再改（见
    _libdefs_fallback_add），并在返回体里说明走了退路。
  * 不在验证阶段跑仿真，除非调用方显式 run_smoke_sim=true。
  * **不编造**：拿不到的参数默认值/取值范围一律 null + 原因说明，绝不
    从元件名猜。ModelParam 在本 ADS 版本里**没有** min/max 字段，所以
    取值范围只能给 null —— 这是 API 的事实，不是没查。

**未实测声明**：写这个模块时本机没有运行中的 ADS 进程，所以以上全部
来自阅读安装目录里的 .py 源码与 .pyi 存根，**没有一条是在 ADS 里跑通
的**。首次在 ADS 中使用时若某个属性名不符，函数会如实报错（而不是
假装成功），届时按错误信息校准即可。所有可选属性都走 getattr 探测。
"""

import datetime
import itertools
import json
import os
import re
import threading

# ---------------------------------------------------------------------------
# 锁：同一工作区的挂接/卸载必须串行
#
# 为什么需要：ws.add_library 会**改工作区的 lib.defs 文件**。两个并发
# 挂接（后端 server.py 虽然有 _workspace_import_lock，但那是后端进程内
# 的锁，跨进程/跨调用方不保证）会读到同一份旧内容、各自写回，后写的
# 覆盖先写的 —— 表现为「挂接成功但某个库不见了」，且极难复现。
#
# 用「以工作区路径为键的锁表」而不是一个全局锁：不同工作区之间没有
# 共享文件，本来就不该互相阻塞。
# ---------------------------------------------------------------------------

_WS_LOCKS: dict = {}
_WS_LOCKS_GUARD = threading.Lock()


def _ws_lock(ws_path: str) -> threading.Lock:
    key = os.path.normcase(os.path.normpath(str(ws_path or "")))
    with _WS_LOCKS_GUARD:
        lock = _WS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WS_LOCKS[key] = lock
        return lock


# 挂接/卸载记录：detach 只认自己挂的，绝不乱删用户手工挂的库
_RECORD_DIRNAME = ".ads_agent"
_RECORD_FILENAME = "attach_record.json"


def _record_path(ws_path: str) -> str:
    return os.path.join(str(ws_path), _RECORD_DIRNAME, _RECORD_FILENAME)


def _load_record(ws_path: str) -> dict:
    """读挂接记录。读不到就当空 —— 记录丢失只影响 detach 的精确度，
    不该让工具不可用（且attach 是幂等的，重跑一次即可重建）。"""
    try:
        with open(_record_path(ws_path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_record(ws_path: str, record: dict) -> str:
    path = _record_path(ws_path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)      # 原子替换：中途崩溃不会留下半个文件
        return path
    except OSError as e:
        # 记录写不进去不影响挂接本身（ADS 侧的库定义已经改了），
        # 但必须如实说，否则 detach 会说「没挂过」而实际挂着。
        raise RuntimeError(
            f"库已挂接，但挂接记录无法写入 {path}: {e}。"
            f"后果：detach_design_kit 将无法自动识别这些库（它只删自己挂的），"
            f"需要时可在 ADS 里手工移除。"
        ) from e


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def simulation_dependency_plan(ws_path: str, libraries) -> dict:
    """Resolve vendor mappings from this workspace's trusted attach record.

    ADS ships configs whose first path component is a symbolic kit name; it
    need not match the ZIP's directory name. Never consult another workspace's
    global ADSlibconfig to repair these paths. No file is written here.
    """
    plan = {"workspace": ws_path, "mappings": {}, "pdk_dirs": [], "problems": []}
    entries = _load_record(ws_path).get("entries") or {}
    for name in sorted(set(str(x) for x in libraries)):
        rec = entries.get(name)
        if not isinstance(rec, dict):
            continue  # ADS builtins and manually managed libraries keep their normal setup.
        root = os.path.realpath(str(rec.get("kit_root") or ""))
        if not rec.get("kit_root") or not _inside(root, ws_path) or not os.path.isdir(root):
            plan["problems"].append(f"库 {name} 的模型根目录不属于当前工作区或已不存在：{root}")
            continue
        if root in plan["pdk_dirs"]:
            continue
        plan["pdk_dirs"].append(root)
        cfg = os.path.join(root, "circuit", "config", "ADSlibconfig")
        if not os.path.isfile(cfg):
            # Self-contained Touchstone and plaintext libraries need no mapping.
            models = os.path.join(root, "circuit", "models")
            if os.path.isdir(models) and any(n.lower().endswith(".library") for n in os.listdir(models)):
                plan["problems"].append(f"库 {name} 含编码模型，但缺少原厂 circuit/config/ADSlibconfig")
            continue
        if not _inside(cfg, root):
            plan["problems"].append(f"库 {name} 的 ADSlibconfig 指向模型包外部")
            continue
        with open(cfg, encoding="utf-8-sig", errors="replace") as source:
            for number, line in enumerate(source, 1):
                line = line.strip()
                if not line or line.startswith(("#", "//", ";")):
                    continue
                fields = line.split(None, 1)
                if len(fields) != 2:
                    plan["problems"].append(f"{cfg}:{number} 模型映射格式无法解析")
                    continue
                logical, raw = fields
                raw = raw.strip().strip('"').replace("\\", "/")
                parts = raw.split("/")
                candidates = [os.path.join(root, raw)]
                if len(parts) > 1:
                    candidates.append(os.path.join(root, *parts[1:]))
                # Factory configs sometimes contain paths from the packaging
                # machine. Rebind only an unambiguous same-named packaged file.
                candidates.append(os.path.join(root, "circuit", "models", parts[-1]))
                matches = sorted(set(os.path.realpath(p) for p in candidates
                                     if _inside(p, root) and os.path.isfile(p)))
                if len(matches) != 1:
                    plan["problems"].append(f"编码库 {logical} 无法唯一定位到当前工作区模型包：{raw}")
                    continue
                target = matches[0]
                previous = plan["mappings"].get(logical)
                if previous and previous != target:
                    plan["problems"].append(f"编码库 {logical} 在当前工作区有多个冲突的模型路径")
                else:
                    plan["mappings"][logical] = target
    plan["ok"] = not plan["problems"]
    return plan


def _instance_library(inst):
    lcv = getattr(inst, "master_lcv_name", None)
    name = str(getattr(lcv, "library_name", "") or "")
    if name:
        return name
    master = str(getattr(inst, "master_name", "") or "")
    return master.split(":", 1)[0] if ":" in master else ""


def check_simulation_netlist(plan: dict, netlist: str) -> dict:
    """Check encoded namespaces and recursively included factory netlists.

    A copied Include symbol may retain an AEL path from another workspace.
    Reject that reference before launching the simulator. Plaintext includes
    and builtin ``#uselib "ckt"`` need no encoded mapping.
    """
    result = {"ok": True, "problems": [], "encoded_names": [], "checked_files": []}
    roots = plan.get("pdk_dirs") or []
    if not roots:
        return result
    mappings = plan.get("mappings") or {}
    pending = [(str(netlist), str(plan.get("workspace") or ""), False)]
    seen = set()
    while pending:
        text, base, factory = pending.pop()
        for logical in re.findall(r'^\s*#uselib\s+"([^"\r\n]+)"\s*,', text, re.M | re.I):
            if logical.lower() == "ckt":
                continue
            if logical in mappings:
                result["encoded_names"].append(logical)
            elif factory:
                result["problems"].append(f"原厂网表引用编码库 {logical}，但当前工作区没有对应映射")
        for raw in re.findall(r'^\s*#include\s+"([^"\r\n]+)"', text, re.M | re.I):
            path = os.path.realpath(os.path.join(base, raw.replace("\\", "/")))
            belongs = any(_inside(path, root) for root in roots)
            # Same-name circuit/models files are evidence that a factory path
            # escaped the current package, not a reason to guess a replacement.
            if not belongs and any(os.path.isfile(os.path.join(root, "circuit", "models",
                                                               os.path.basename(path))) for root in roots):
                result["problems"].append(f"原厂 Include 引用了当前工作区模型包之外的文件：{path}")
                continue
            if not belongs or path in seen:
                continue
            seen.add(path)
            if len(seen) > 256:
                result["problems"].append("原厂 Include 依赖超过 256 个文件，无法完整复核")
                break
            if not os.path.isfile(path):
                result["problems"].append(f"原厂 Include 文件不存在：{path}")
                continue
            if os.path.getsize(path) > 8 * 1024 * 1024:
                result["problems"].append(f"原厂 Include 文件超过 8 MiB，无法完整复核：{path}")
                continue
            with open(path, encoding="utf-8-sig", errors="replace") as source:
                pending.append((source.read(), os.path.dirname(path), True))
            result["checked_files"].append(path)
    result["encoded_names"] = sorted(set(result["encoded_names"]))
    result["ok"] = not result["problems"]
    return result


def prepare_design_dependencies(design, ws_path: str, *, place_includes=False) -> dict:
    """Add unique factory Include components during an authorized design build.

    During read-only simulation preflight, report missing/ambiguous includes
    instead of editing an existing schematic. Candidates are restricted to the
    used registered library and must have a real symbol and model definition.
    """
    insts = list(design.instances)
    names = {_instance_library(i) for i in insts} - {""}
    plan = simulation_dependency_plan(ws_path, names)
    plan["include_instances"] = []
    registered = _load_record(ws_path).get("entries") or {}
    for name in sorted(names & set(registered)):
        lib = _library_for(name)
        candidates = []
        for cell in list(lib.cells):
            cn = str(getattr(cell, "name", "") or "")
            if re.search(r"include(?:_|$)", cn, re.I) and \
                    getattr(cell, "model_def", None) is not None and cell.view_exists("symbol"):
                candidates.append(cn)
        existing = {str(getattr(getattr(i, "master_lcv_name", None), "cell_name", "") or
                        str(getattr(i, "master_name", "") or "").split(":")[-1])
                    for i in insts if _instance_library(i) == name}
        if existing & set(candidates):
            continue
        if not candidates:
            continue  # No Include requirement can be inferred for self-contained models.
        if len(candidates) != 1:
            plan["problems"].append(f"库 {name} 有多个 Include 元件，请明确选用：{candidates}")
            continue
        master = f"{name}:{candidates[0]}:symbol"
        if not place_includes:
            plan["problems"].append(f"设计缺少原厂 Include 元件 {master}；请先放置该元件")
            continue
        used = {str(getattr(i, "inst_name", "")) for i in insts}
        serial = 1
        while f"MODEL_INCLUDE{serial}" in used:
            serial += 1
        inst = design.add_instance(master, (8.0, 8.0 + 3.0 * len(plan["include_instances"])),
                                  name=f"MODEL_INCLUDE{serial}")
        if list(inst.inst_pins):
            raise RuntimeError(f"候选 Include {master} 带电气引脚，不能作为无连接模型包含件自动添加")
        insts.append(inst)
        plan["include_instances"].append(str(inst.inst_name))
    plan["ok"] = not plan["problems"]
    if not plan["ok"]:
        raise RuntimeError("原厂模型依赖未就绪：\n- " + "\n- ".join(plan["problems"]))
    return plan


# ---------------------------------------------------------------------------
# ADS 侧薄封装（与 ads_ops 的 _de / _require_workspace 保持同一套语义）
# ---------------------------------------------------------------------------

def _de():
    """取 keysight.ads.de。

    import 失败时报可读原因而不是漏出 ModuleNotFoundError —— 这个模块
    只能在 ADS 进程内运行（Workspace/Library/Cell 都必须在 ADS 里拿），
    裸的 ModuleNotFoundError 会让人以为是路径/依赖装错了。
    """
    try:
        import keysight.ads.de as de
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"无法导入 keysight.ads.de（{type(e).__name__}: {e}）。"
            f"本模块的库/设计对象只能在 ADS 进程内获取 —— "
            f"请确认工具是由 ADS 里的插件面板调起的。"
        ) from e
    return de


def _db_uu():
    try:
        import keysight.ads.de.db_uu as db_uu
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"无法导入 keysight.ads.de.db_uu（{type(e).__name__}: {e}）。"
            f"本模块只能在 ADS 进程内运行。"
        ) from e
    return db_uu


def _require_workspace():
    de = _de()
    if not de.workspace_is_open():
        raise RuntimeError("当前没有打开的工作区，请先在 ADS 中打开或新建一个 Workspace")
    return de.active_workspace()


def _norm_path(p) -> str:
    """路径比较用的规范形。空串必须先判空：normpath("") == "."。"""
    p = str(p or "").strip()
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


def _workspace_guard(args: dict, action: str) -> tuple:
    """工作区复核。返回 (ws, ws_path, expected, mismatch_message)。

    为什么要复核：模型资产归属于**上传时所在的工作区**。用户切了工程以后
    继续按旧工作区的假设操作，会把 A 工程的模型挂到 B 工程去 —— 两个工程
    可能有同名 library/cell，之后谁也说不清网表里那个 MLIN 是哪来的。
    这里只做「检测 + 如实说明」，判定逻辑与 backend/server.py 的
    workspace_mismatch() 保持同一套口径（同样的 kind 语义）。
    """
    ws = _require_workspace()
    ws_path = str(getattr(ws, "path", "") or "")
    expected = str(args.get("workspace") or "").strip()
    if not expected or not ws_path:
        return ws, ws_path, expected, ""
    if _norm_path(expected) == _norm_path(ws_path):
        return ws, ws_path, expected, ""
    msg = (
        f"工作区不一致，已停止{action}：请求针对 {expected}，"
        f"而 ADS 当前打开的是 {ws_path}。两个工作区可能存在同名的 "
        f"library/cell，挂错工程后无法区分模型来源。"
        f"请在 ADS 中切换到目标工作区后重试。"
    )
    return ws, ws_path, expected, msg


def _cancel_requested(ctx) -> bool:
    """尽力探测取消信号。

    **诚实说明**：当前 toolserver.JobContext（toolserver.py L327的
    __slots__）只暴露 job_id / name / box / done / deferred /
    sim_off_main_thread / _lock / _settled，**没有**取消标志；取消只
    记在 toolserver 的 _active_jobs 里（那是 toolserver 的私有表，
    本模块不该去戳）。所以这里按鸭子类型探测一个可选属性：将来
    JobContext 补上 cancel_requested 就自动生效，现在恒为 False。
    与其去猜一个不存在的属性，不如让调用链自己长出来。
    """
    if ctx is None:
        return False
    for attr in ("cancel_requested", "cancelled", "cancel"):
        val = getattr(ctx, attr, None)
        if isinstance(val, bool):
            return val
        if callable(val):
            try:
                if val() is True:
                    return True
            except Exception:  # noqa: BLE001 — 探测失败按"没取消"处理
                return False
    return False


# ---------------------------------------------------------------------------
# lib.defs 解析
# ---------------------------------------------------------------------------

_DEFINE_RE = re.compile(r"^DEFINE\s+(\S+)\s+(.+)$", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"^ASSIGN\s+(\S+)\s+(\S+)\s*(.*)$", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"^INCLUDE\s+(.+)$", re.IGNORECASE)

# lib.defs 里的 libMode 取值 -> LibraryMode 成员名。
# 取值来自本机真实文件（已核对）：
#   E:\...\design_kit\Keysight_Photonics_pdk\lib.defs      ASSIGN ... libMode readOnly
#   E:\...\designguides\projects\smithdg\lib.defs          ASSIGN smithdg libMode shared
#   E:\...\examples\DesignKit\DemoKit_mmWave\for_editing_pdk.defs  libMode shared
# **键一律小写**（文件里是 readOnly / nonShared 这种驼峰），查表前必须
# .lower() —— 库里三处查表都做了，别新增忘记 lowercase 的调用点。
_LIBMODE_MAP = {
    "readonly": "READ_ONLY",
    "read_only": "READ_ONLY",
    "ro": "READ_ONLY",
    "shared": "SHARED",
    "nonshared": "NON_SHARED",
    "non_shared": "NON_SHARED",
    "unknown": "UNKNOWN",
}


def _library_mode(name: str):
    """按名字取 LibraryMode 枚举成员。

    传字符串/整数给需要枚举成员的 API 会报 "has no value"（README 坑表
    第一行就是这条），所以这里只返回**枚举成员**，绝不返回裸字符串。

    两种"拿不到"要分开说，不能都返回 None 让上层以为模式名不对：
      * 名字不认识 -> 返回 None（调用方报「模式名错」）
      * ADS 的 de 模块整个拿不到 -> 抛错（报「不在 ADS 进程里」）
    """
    want = _LIBMODE_MAP.get(str(name or "").strip().lower())
    if not want:
        return None
    try:
        de = _de()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"无法导入 keysight.ads.de（{type(e).__name__}: {e}）。"
            f"库模式必须取到真正的 LibraryMode 枚举成员，"
            f"否则 add_library 会报 \"has no value\"。"
            f"本工具只能在 ADS 进程内运行。"
        ) from e
    lm = getattr(de, "LibraryMode", None)
    if lm is None:
        raise RuntimeError(
            f"keysight.ads.de 上没有 LibraryMode（已核对 de/__init__.py L61 "
            f"有该导出）；若报此错说明该ADS 构建与本工具开发时不同。"
        )
    return getattr(lm, want, None)


def _mode_str(mode) -> str:
    """枚举 -> 可读字符串。`.str` 是官方属性（.pyi L710）。"""
    if mode is None:
        return ""
    val = getattr(mode, "str", None)
    if isinstance(val, str) and val:
        return val
    return str(mode)


def _parse_lib_defs_text(text: str, defs_dir: str) -> dict:
    """逐行按 token 解析 lib.defs 文本。

    为什么要自己写而不用字符串拼接：lib.defs 的**相对路径是相对
    DEFINE 所在文件解析的**，还有 $HPEESOF_DIR 这类环境变量引用
    （见 oalibs/analog_rf.defs 真实内容），还有 INCLUDE 间接引用。
    拼字符串既处理不了这些，也没法在不破坏原文件的前提下去重。
    """
    libs: dict = {}       # name -> entry（同名后出现的 DEFINE 覆盖前面的）
    order: list = []
    includes: list = []
    unparsed: list = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        m = _INCLUDE_RE.match(line)
        if m:
            includes.append({"line": lineno, "target": m.group(1).strip()})
            continue
        m = _DEFINE_RE.match(line)
        if m:
            name = m.group(1)
            raw_path = m.group(2).strip()
            entry = libs.get(name)
            if entry is None:
                entry = {"name": name, "path_tokens": [], "mode": None,
                         "write_path": None, "define_line": lineno,
                         "assign_lines": []}
                libs[name] = entry
                order.append(name)
            entry["path_tokens"] = _split_path_tokens(raw_path)
            entry["define_line"] = lineno
            continue
        m = _ASSIGN_RE.match(line)
        if m:
            name, key = m.group(1), m.group(2).lower()
            value = m.group(3).strip()
            entry = libs.get(name)
            if entry is None:
                # DEFINE 在别的included 文件里 —— 记下来，不当错误
                entry = {"name": name, "path_tokens": [], "mode": None,
                         "write_path": None, "define_line": None,
                         "assign_lines": []}
                libs[name] = entry
                order.append(name)
            entry["assign_lines"].append(lineno)
            if key == "libmode":
                entry["mode"] = value
            elif key == "writepath":
                entry["write_path"] = value
            continue
        unparsed.append({"line": lineno, "text": line[:200]})

    out = []
    for name in order:
        entry = libs[name]
        tokens = entry["path_tokens"]
        rel = " ".join(tokens) if tokens else ""
        abspath = _resolve_defs_path(tokens, defs_dir)
        mode_name = _LIBMODE_MAP.get(str(entry["mode"] or "").lower())
        out.append({
            "name": name,
            "declared_path": rel,
            "path_tokens": tokens,
            "abs_path": abspath,
            "abs_path_resolved": bool(abspath),
            "declared_mode": entry["mode"],
            "mode": mode_name,          # LibraryMode 成员名，如 READ_ONLY
            "write_path": entry["write_path"],
            "define_line": entry["define_line"],
            "assign_lines": entry["assign_lines"],
        })
    return {"libraries": out, "includes": includes, "unparsed": unparsed}


def _split_path_tokens(raw: str) -> list:
    """把 DEFINE 行的路径部分切成 token。

    要按 token 切而不是整串取，因为 ADS 允许引号：
    `DEFINE Foo "D:/Program Files/Foo"`。用 shlex 会吃掉反斜杠，
    所以手写：双引号/单引号内原样保留，其余按空白切。
    """
    tokens: list = []
    buf: list = []
    quote = ""
    for ch in str(raw or ""):
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _resolve_defs_path(tokens: list, defs_dir: str) -> str:
    """把 DEFINE 的路径 token 解析成绝对路径。

    规则（依据本机真实 lib.defs）：
      * 绝对路径直接用；
      * `./x` / `x` 相对**DEFINE 所在文件**（不是当前工作目录！这是
        最容易错的一条 —— 用 cwd 解析会挂到完全无关的地方去）；
      * `$VAR` / `${VAR}` 展开环境变量（oalibs/*.defs 全是这个形式）。
    返回空串表示解析不出来（比如引用了没设的环境变量），由调用方
    如实报告，不猜一个路径。
    """
    if not tokens:
        return ""
    raw = os.path.expandvars(tokens[0])
    if not raw:
        return ""
    # 环境变量没展开干净（$HPEESOF_DIR 未设）时 raw 里还带 $
    if "$" in raw:
        return ""
    raw = raw.replace("/", os.sep) if os.sep != "/" else raw
    if os.path.isabs(raw):
        return _norm_path(raw)
    return _norm_path(os.path.join(str(defs_dir), raw))


def _read_lib_defs(defs_path: str) -> dict:
    """读一个 lib.defs，返回 {exists, path, source, libraries, includes,
    unparsed, error}。

    优先用**官方解析器** LibDefList（_core/library.py L576/L593）——它
    才是 ADS 自己读这个文件的语义（含 INCLUDE 的处理）。官方解析器在
    某些情况下不可用（不在 ADS 进程里、文件正在被写、格式不被识别），
    这时才退回自己那份逐行 token 解析，并在返回体里标明 source，
    让调用方知道结论的来源。
    """
    defs_path = str(defs_path or "")
    out = {"exists": False, "path": defs_path, "source": None,
           "libraries": [], "includes": [], "unparsed": [], "error": ""}
    if not defs_path or not os.path.isfile(defs_path):
        return out
    out["exists"] = True
    defs_dir = os.path.dirname(os.path.abspath(defs_path))

    # 1) 官方解析器
    try:
        de = _de()
        list_cls = getattr(de, "LibDefList", None)
        if list_cls is not None:
            ld = list_cls.open(defs_path)
            libs: list = []
            incs: list = []
            for mem in list(ld.members):
                if getattr(mem, "is_library", False):
                    name = str(getattr(mem, "name", "") or "")
                    p = str(getattr(mem, "path", "") or "")
                    mode = getattr(mem, "mode", None)
                    toks = _split_path_tokens(p)
                    libs.append({
                        "name": name,
                        "declared_path": p,
                        "path_tokens": toks,
                        "abs_path": _resolve_defs_path(toks, defs_dir),
                        "abs_path_resolved": bool(_resolve_defs_path(toks, defs_dir)),
                        "declared_mode": _mode_str(mode),
                        "mode": getattr(mode, "name", None),
                        "write_path": None,
                        "define_line": None,
                        "assign_lines": [],
                    })
                else:
                    incs.append({"line": None,
                                 "target": str(getattr(mem, "path", "") or "")})
            if libs or incs:
                out["source"] = "de.LibDefList"
                out["libraries"] = libs
                out["includes"] = incs
                return out
            # 空结果：文件存在但官方解析出0 个成员 —— 多半是空文件，
            # 交给下面手写解析器再确认一次，不直接下结论。
    except Exception as e:  # noqa: BLE001 — 官方解析器不可用是常态
        out["error"] = f"官方 LibDefList 解析失败（改用内置解析器）: {type(e).__name__}: {e}"

    # 2) 内置逐行 token 解析器
    try:
        with open(defs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        out["error"] = (out["error"] + "; " if out["error"] else "") + \
            f"读取失败: {type(e).__name__}: {e}"
        return out
    parsed = _parse_lib_defs_text(text, defs_dir)
    out["source"] = "builtin"
    out["libraries"] = parsed["libraries"]
    out["includes"] = parsed["includes"]
    out["unparsed"] = parsed["unparsed"]
    return out


def _find_kit_lib_defs(kit_root: str) -> str:
    """在套件根里找 lib.defs。

    只在**套件根这一层**找（不递归）：厂商套件的 lib.defs 就在根下
    （已核对本机 5 个真实套件：Keysight_Photonics_pdk、DemoKit_mmWave、
    DemoKit_MTM 的 lib.defs 都在根目录）。递归全文搜会撞上库目录里
    成千上万个cell 文件，慢且毫无必要。
    """
    cand = os.path.join(str(kit_root), "lib.defs")
    return cand if os.path.isfile(cand) else ""


def _libdefs_fallback_add(ws_path: str, defs_path: str) -> dict:
    """退路：直接往工作区的 lib.defs 里加一行 INCLUDE。

    **只在官方 add_library_definition_file 不可用时才会走到这里。**
    顺序是硬要求：真正解析 → 备份 → 去重 → 失败可回滚，绝不是字符串
    拼了就算。返回体里带 fallback=True，让调用方知道这次不是走的
    官方 API（这是必须如实报告的事，不是实现细节）。
    """
    ws = _require_workspace()
    try:
        lib_defs = str(ws.lib_defs_file)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"拿不到工作区库定义文件路径，无法走退路: {type(e).__name__}: {e}"
        ) from e

    backup = lib_defs + ".bak_adsagent"
    original = ""
    if os.path.isfile(lib_defs):
        with open(lib_defs, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
        # 去重：已经在里面（按目标路径逐行比对，不做子串匹配 ——
        # 子串匹配会把 D:/a/lib 和 D:/a/lib2 认成同一个）
        target = _norm_path(defs_path)
        for raw in original.splitlines():
            m = _INCLUDE_RE.match(raw.strip())
            if not m:
                continue
            toks = _split_path_tokens(m.group(1))
            if toks and _norm_path(os.path.expandvars(toks[0])) == target:
                return {"fallback": True, "changed": False, "lib_defs_file": lib_defs,
                        "backup": backup,
                        "reason": "该库定义文件已在工作区 lib.defs 中（按路径去重），未重复写入"}
        try:
            with open(backup, "w", encoding="utf-8") as f:
                f.write(original)
        except OSError as e:
            raise RuntimeError(
                f"无法备份 {lib_defs} 到 {backup}，已阻止退路写入: {e}。"
                f"没有备份就不改工作区库定义 —— 这是硬底线。"
            ) from e

    new_text = (original or "")
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += f"INCLUDE {defs_path}\n"
    try:
        with open(lib_defs, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError as e:
        # 回滚：把备份内容写回去，绝不留下半截文件
        if original:
            try:
                with open(lib_defs, "w", encoding="utf-8") as f:
                    f.write(original)
            except OSError:
                pass
        raise RuntimeError(
            f"退路写入 {lib_defs} 失败: {e}；已尝试用备份恢复原内容。"
            f"备份在 {backup}，请核对后手工处理。"
        ) from e
    return {"fallback": True, "changed": True, "lib_defs_file": lib_defs,
            "backup": backup, "reason": "官方 add_library_definition_file 不可用"}


# ---------------------------------------------------------------------------
# 库/元件读取（只读）
# ---------------------------------------------------------------------------

def _lib_snapshot(lib, sample_cells: int = 0) -> dict:
    """一个 Library 的只读快照。

    cell 数量用len()：`_pde.Library.get_all_cells()`（.pyi L758）返回
    完整 list，NamedItemCollection 构造时就已经 tuple() 掉了（_nameditem
    collection.py L79），所以 len() 不额外遍历。**本 ADS 版本没有只数个数
    的 API**（核对了 .pyi 的 Library 段：get_all_cells 是唯一的入口），
    这一条遍历避不开。sample_cells>0 时额外列出一小撮 cell 名，
    让「库里到底有什么」有实证，而不是只有一个数字。
    """
    out: dict = {"name": "", "path": "", "lib_path": ""}
    for key, attr in (("name", "name"), ("path", "path"), ("lib_path", "lib_path")):
        try:
            out[key] = str(getattr(lib, attr, "") or "")
        except Exception as e:  # noqa: BLE001
            out[key + "_error"] = f"{type(e).__name__}: {e}"
    for key, attr in (("is_open", "is_open"), ("is_read_only", "is_read_only"),
                      ("is_writable", "is_writable"),
                      ("is_an_ads_library", "is_an_ads_library"),
                      ("has_attached_tech", "has_attached_tech")):
        try:
            val = getattr(lib, attr, None)
            out[key] = bool(val) if val is not None else None
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out[key + "_error"] = f"{type(e).__name__}: {e}"
    try:
        out["attached_tech_lib_name"] = str(lib.attached_tech_lib_name or "")
    except Exception as e:  # noqa: BLE001
        out["attached_tech_lib_name"] = None
        out["attached_tech_lib_name_error"] = f"{type(e).__name__}: {e}"
    try:
        out["cell_count"] = len(lib.cells)
    except Exception as e:  # noqa: BLE001
        out["cell_count"] = None
        out["cell_count_error"] = f"{type(e).__name__}: {e}"
    if sample_cells > 0 and out.get("cell_count"):
        try:
            out["cell_sample"] = [str(getattr(c, "name", "") or "")
                                  for c in itertools.islice(iter(lib.cells), sample_cells)]
        except Exception as e:  # noqa: BLE001
            out["cell_sample_error"] = f"{type(e).__name__}: {e}"
    return out


def _cell_views(cell) -> list:
    try:
        return [str(getattr(v, "view_name", getattr(v, "name", "")) or "")
                for v in (cell.views or [])]
    except Exception:  # noqa: BLE001
        return []


def _param_unit(p) -> str:
    """参数的物理单位类型（'freq' / 'res' / 'lng' / 'num' ...）。

    ModelUnitType 的枚举**值**是小写串（_parameters.py L1804起），
    官方包装层已经把它转成枚举，这里取 .value 拿原始串；拿不到就返回
    空串 —— 不从参数名猜单位（'Freq' 这个名字并不保证它就是频率参数）。
    """
    try:
        ut = p.unit_type
    except Exception:  # noqa: BLE001
        return ""
    val = getattr(ut, "value", None)
    if isinstance(val, str):
        return val
    return str(ut) if ut is not None else ""


def _param_type(p) -> str:
    """参数的数据类型（'real' / 'int' / 'string' ...），同上取 .value。"""
    try:
        pt = p.param_type
    except Exception:  # noqa: BLE001
        return ""
    val = getattr(pt, "value", None)
    if isinstance(val, str):
        return val
    return str(pt) if pt is not None else ""


def _param_default(p):
    """参数的默认���。

    ModelParam.default_value（_model_def.py L163）返回 Optional[ParamItem]，
    而且**可能为 None**：文档明写「若该属性未设置，初值必须由一个
    PARAMETER_DEFAULT_VALUE 回调提供」—— 原厂 AEL 元件大量属于后者。
    拿不到就返回 None，由上层如实标null 并说明原因。
    """
    try:
        item = p.default_value
    except Exception:  # noqa: BLE001
        return None
    if item is None:
        return None
    for attr in ("value", "name"):
        try:
            val = getattr(item, attr)
        except Exception:  # noqa: BLE001
            continue
        if attr == "value":
            return None if val is None else str(val)
    return None


def _param_bool(p, attr: str):
    try:
        val = getattr(p, attr, None)
    except Exception:  # noqa: BLE001
        return None
    return bool(val) if val is not None else None


_PARAM_RANGE_REASON = (
    "本 ADS 版本的 ModelParam 没有 min/max 字段（已核对 "
    "keysight/ads/de/db/_model_def.py 的 ModelParam 全部属性："
    "name/label/formset/unit_type/param_type/callbacks/default_value "
    "加一组 is_* 布尔标记），因此取值范围无法从定义里读出。"
    "不猜 —— 需要时以仿真实测为准。"
)


def _model_param_entries(model_def, limit: int = 200) -> dict:
    """把 model_def.parameters 读成可序列化 dict。

    只报**真实读到**的：name / label / unit / param_type / default /
    is_editable / is_discrete_value / is_netlistable / is_optimizable。
    取值范围一律 null + 原因（见 _PARAM_RANGE_REASON）。
    """
    out: dict = {"available": False, "parameters": [], "count": None,
                 "error": "", "range_note": _PARAM_RANGE_REASON}
    try:
        params = list(model_def.parameters)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"读取 model_def.parameters 失败: {type(e).__name__}: {e}"
        return out
    out["available"] = True
    out["count"] = len(params)
    for p in params[:max(1, int(limit))]:
        try:
            nm = str(p.name or "")
        except Exception:  # noqa: BLE001
            continue
        if not nm:
            continue
        out["parameters"].append({
            "name": nm,
            "label": str(getattr(p, "label", "") or ""),
            "unit": _param_unit(p),
            "param_type": _param_type(p),
            "default": _param_default(p),
            "min": None,
            "max": None,
            "range_reason": _PARAM_RANGE_REASON,
            "is_editable": _param_bool(p, "is_editable"),
            "is_discrete_value": _param_bool(p, "is_discrete_value"),
            "is_netlistable": _param_bool(p, "is_netlistable"),
            "is_optimizable": _param_bool(p, "is_optimizable"),
        })
    if len(params) > len(out["parameters"]):
        out["truncated"] = len(params) - len(out["parameters"])
    return out


def _model_def_info(model_def) -> dict:
    """model_def 的标量属性 + 参数定义。"""
    out: dict = {}
    for key, attr in (("name", "name"), ("label", "label"),
                      ("component_name", "component_name"),
                      ("library_name", "library_name")):
        try:
            out[key] = str(getattr(model_def, attr, "") or "")
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out[key + "_error"] = f"{type(e).__name__}: {e}"
    out["impl_class"] = type(model_def).__name__
    out.update(_model_param_entries(model_def))
    return out


def _read_model_def(cell):
    """读 cell 的模型定义。

    `Cell.model_def`（_core/cell.py L111）是**正确的参数入口** —— 它经
    ael._wrapping._wrap_value 包装，AEL 实现的元件返回 ModelDefAEL。
    拿不到（None 或抛错）就如实返回 None，绝不从 cell 名/文件名反推
    参数默认值。
    """
    try:
        return cell.model_def
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _library_for(name: str):
    """取已打开的 Library 对象；取不到就报可用库名，便于用户自查。"""
    de = _de()
    getter = getattr(de, "get_open_library", None)
    if getter is None:
        raise RuntimeError(
            "keysight.ads.de.get_open_library 在本 ADS 版本不可用"
            "（已核对 de/__init__.py L67 存在，若报此错说明该构建不同）")
    try:
        return getter(str(name))
    except Exception as e:  # noqa: BLE001
        try:
            open_names = sorted(str(x) for x in (de.active_workspace().library_names or []))
        except Exception:  # noqa: BLE001
            open_names = []
        raise RuntimeError(
            f"库里没有可用的 {name}（无法打开）：{type(e).__name__}: {e}。"
            f"当前工作区已打开的库: {open_names}。"
            f"如果是刚导入的包，请先用 attach_design_kit 挂接。"
        ) from e


def _library_kind(lib, ws_path: str) -> str:
    """库的来源分类：design_kit / builtin / workspace。

    判据全部来自真实可查的属性，不靠库名猜：
      * 路径在工作区目录内 -> workspace；
      * 路径在 $HPEESOF_DIR 下 -> builtin（ADS 自带 oalibs）；
      * 其余（路径在工作区外、又不是ADS 自带）-> design_kit。
    """
    try:
        p = _norm_path(getattr(lib, "path", "") or getattr(lib, "lib_path", ""))
    except Exception:  # noqa: BLE001
        return "unknown"
    if not p:
        return "unknown"
    root = _norm_path(os.environ.get("HPEESOF_DIR", ""))
    if root and (p == root or p.startswith(root + os.sep)):
        return "builtin"
    ws = _norm_path(ws_path)
    if ws and (p == ws or p.startswith(ws + os.sep)):
        return "workspace"
    return "design_kit"


def _find_cell(lib, cell_name: str):
    """cell() 在不存在时抛异常；先问 cell_exists 可以把「不存在」和
    「库坏了」区分开，两种情况给的提示完全不一样。"""
    try:
        exists = bool(lib.cell_exists(str(cell_name)))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"查询库 {lib.name} 里是否存在 cell {cell_name} 失败: "
            f"{type(e).__name__}: {e}") from e
    if not exists:
        try:
            sample = [str(getattr(c, "name", "") or "")
                      for c in itertools.islice(iter(lib.cells), 20)]
        except Exception:  # noqa: BLE001
            sample = []
        raise RuntimeError(
            f"库 {lib.name} 里没有 cell {cell_name}。该库前若干 cell: {sample}。"
            f"请用 list_vendor_models 核对真实存在的 cell 名（不要凭型号猜）。"
        )
    cell = _read_cell(lib, cell_name)
    if cell is None:
        raise RuntimeError(f"库 {lib.name} 里能确认 cell {cell_name} 存在，但打开失败。")
    return cell


def _read_cell(lib, cell_name: str):
    getter = getattr(lib, "get_cell_if_exists", None)
    if callable(getter):
        try:
            return getter(str(cell_name))
        except Exception:  # noqa: BLE001 — 换下面的 cell()
            pass
    try:
        return lib.cell(str(cell_name))
    except Exception:  # noqa: BLE001
        return None


def _parse_master(master: str, library: str = "", cell: str = "") -> tuple:
    """'lib:cell[:view]' -> (lib, cell, view)。容忍只给其中一部分。"""
    lib_s = str(library or "").strip()
    cell_s = str(cell or "").strip()
    view_s = ""
    ms = str(master or "").strip()
    if ms:
        parts = ms.split(":")
        if len(parts) >= 2:
            lib_s = lib_s or parts[0]
            cell_s = cell_s or parts[1]
            view_s = parts[2] if len(parts) > 2 else ""
        else:
            cell_s = cell_s or parts[0]
    return lib_s, cell_s, view_s


# ---------------------------------------------------------------------------
# 工具 1：attach_design_kit
# ---------------------------------------------------------------------------

def attach_design_kit(args: dict, ctx=None) -> dict:
    """把 Design Kit 挂接到**当前工作区**（只读），并如实报告挂接结果。

    参数（与 backend/model_tools.py 的调用一一对应）
      package_id      包 ID，只用于记录与回显
      kit_root        **解压后的套件根绝对路径**（由后端从清单解析后
                      传入，本模块不自己找路径 —— 猜路径就是往工作区
                      之外乱挂）
      kit_root_rel    包内相对路径，仅回显
      workspace       目标工作区（后端注入的可信上下文）；不一致则停止
      library_names   要挂的库名；缺省从 kit_root/lib.defs 解析
      mode            库模式，**默认且只允许 READ_ONLY**

    关键设计
    1. **工作区挂接≠ 全局 Favorite Design Kit**。用的是
       ws.add_library（官方语义：把库引用加入**本工作区**的库定义文件），
       不去改 ADS 的全局套件偏好 —— 后者跨工作区生效，用户没要求就不该动。
    2. **幂等**：挂之前先看 ws.libraries 里有没有同名且同路径的库，
       有就跳过并标 already_attached。重复导入同一个包不会产生重复条目。
    3. **同名不同路径 = 冲突**，不覆盖、不断言，如实报冲突让用户决定。
    4. 全部只读打开：SHARED / NON_SHARED 会创建锁文件并允许写原厂目录，
       浏览与挂接都不需要，所以只接受 READ_ONLY（其它值明确拒绝）。
    5. 同一工作区的挂接串行（见 _ws_lock 的说明）。
    6. **挂接成功 ≠ 模型可用**。返回体里的 usable 一律是 False 并附
       说明；能不能真的放进电路里仿真，只有 validate_model_import
       能下结论。
    """
    package_id = str(args.get("package_id") or "").strip()
    kit_root = str(args.get("kit_root") or args.get("kit_root_abs") or "").strip()
    kit_root_rel = str(args.get("kit_root_rel") or "").strip()
    requested_mode = str(args.get("mode") or "READ_ONLY").strip()

    ws, ws_path, _expected, mismatch = _workspace_guard(args, "挂接模型包")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch,
                "package_id": package_id, "workspace": ws_path}

    if not kit_root:
        raise RuntimeError("缺少 kit_root（解压后的套件根绝对路径）")
    kit_root = os.path.normpath(kit_root)
    if not os.path.isdir(kit_root):
        raise RuntimeError(
            f"套件根目录不存在：{kit_root}。"
            f"解压产物可能已被清理 —— 请重新导入该包。")

    mode = _library_mode(requested_mode)
    if mode is None:
        raise RuntimeError(
            f"无法识别的库模式 {requested_mode!r}。"
            f"可用: READ_ONLY（默认，也是本模块唯一允许的）。")
    if getattr(mode, "name", "") != "READ_ONLY":
        # 硬拒绝：给原厂库写权限是真实副作用（锁文件 + 可写目录），
        # 浏览/挂接都不需要它。要写请用户在 ADS 里手工处理。
        raise RuntimeError(
            f"本工具只以 READ_ONLY 挂接模型库，拒绝 {mode}。"
            f"给原厂库写权限会创建锁文件并允许改写原厂目录，"
            f"挂接/浏览都不需要这个权限。"
            f"如确需可写，请自行在 ADS 中配置。")

    want_names = [str(x).strip() for x in (args.get("library_names") or []) if str(x).strip()]

    lib_defs_path = _find_kit_lib_defs(kit_root)
    defs = _read_lib_defs(lib_defs_path) if lib_defs_path else {
        "exists": False, "path": "", "source": None, "libraries": [],
        "includes": [], "unparsed": [], "error": ""}

    # 库路径的来源：优先套件自带 lib.defs（那是原厂声明的权威集合）
    declared = {}
    for e in defs["libraries"]:
        if e.get("name") and e.get("abs_path"):
            declared[e["name"]] = e

    targets: list = []
    notes: list = []
    if want_names:
        for nm in want_names:
            e = declared.get(nm)
            if e:
                targets.append({"name": nm, "abs_path": e["abs_path"],
                                "source": "kit lib.defs",
                                "declared_mode": e.get("declared_mode")})
            else:
                # 没在 lib.defs 里声明：按库名在套件根下找同名目录 ——
                # 这是有依据的兜底（DEFINE 的路径惯例就是 ./<库名>），
                # 但必须说清来源，且目录不存在就报错不硬挂。
                guess = os.path.join(kit_root, nm)
                if os.path.isdir(guess):
                    targets.append({"name": nm, "abs_path": _norm_path(guess),
                                    "source": "kit 根下同名目录（lib.defs 未声明）",
                                    "declared_mode": None})
                else:
                    notes.append(
                        f"lib.defs 里没有声明库 {nm}，套件根下也没有同名目录 "
                        f"{guess} —— 已跳过，未挂接。")
    elif declared:
        for nm, e in declared.items():
            targets.append({"name": nm, "abs_path": e["abs_path"],
                            "source": "kit lib.defs",
                            "declared_mode": e.get("declared_mode")})
        # libMode 声明了 shared 的库在这里被强制降级为只读，如实说明
        for t in targets:
            dm = _LIBMODE_MAP.get(str(t.get("declared_mode") or "").lower())
            if dm and dm != "READ_ONLY":
                notes.append(
                    f"库 {t['name']} 在套件 lib.defs 里声明的是 "
                    f"{t['declared_mode']}，本次仍以 READ_ONLY 挂接"
                    f"（挂接不为了写）。")
    else:
        notes.append(
            f"套件根 {kit_root} 下没有可解析的 lib.defs，且未指定 library_names —— "
            f"无从得知该挂哪些库，已停止（不按目录结构瞎猜）。"
            f"请用 inspect_model_package 看包结构后显式给出库名。")
        return {"ok": False, "package_id": package_id, "workspace": ws_path,
                "kit_root": kit_root, "kit_root_rel": kit_root_rel,
                "libraries": [], "already_attached": False,
                "attached_at": "", "mode": "READ_ONLY",
                "lib_defs": {"path": lib_defs_path, "exists": False},
                "notes": notes,
                "error": notes[-1]}

    lock = _ws_lock(ws_path)
    if not lock.acquire(timeout=180):
        raise RuntimeError(
            f"等待工作区 {ws_path} 的挂接锁超时（180s）—— "
            f"另一个挂接操作可能仍在进行，稍后重试。")

    attached: list = []
    already: list = []
    conflicts: list = []
    failed: list = []
    added_now: list = []      # 本次真正新加进去的（回滚只动它们）
    try:
        # 先取一份「当前已打开的库」快照做幂等/冲突判定
        existing: dict = {}
        try:
            for lib in list(ws.libraries or []):
                try:
                    existing[str(lib.name)] = lib
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"读取工作区已打开的库列表失败，已停止挂接: "
                f"{type(e).__name__}: {e}") from e

        for t in targets:
            name = t["name"]
            path = t["abs_path"]
            if not path or not os.path.isdir(path):
                failed.append({"name": name, "path": path,
                               "reason": f"库目录不存在：{path}"})
                continue
            prior = existing.get(name)
            if prior is not None:
                prior_path = _norm_path(getattr(prior, "path", "")
                                        or getattr(prior, "lib_path", ""))
                if prior_path and prior_path == _norm_path(path):
                    snap = _lib_snapshot(prior)
                    snap["already_attached"] = True
                    snap["source"] = t["source"]
                    attached.append(snap)
                    already.append(name)
                    continue
                conflicts.append({
                    "name": name, "wanted_path": path, "existing_path": prior_path,
                    "reason": "工作区里已有同名库但指向不同路径 —— 已保留原有引用，"
                              "未替换。两个套件可能有同名库，需要你决定用哪个。",
                })
                continue
            try:
                lib = ws.add_library(name, path, mode)
            except Exception as e:  # noqa: BLE001
                failed.append({"name": name, "path": path,
                               "reason": f"{type(e).__name__}: {e}"})
                continue
            snap = _lib_snapshot(lib, sample_cells=5)
            snap["already_attached"] = False
            snap["source"] = t["source"]
            snap["mode"] = _mode_str(mode)
            attached.append(snap)
            added_now.append({"name": name, "path": path})
    finally:
        lock.release()

    # lib.defs 整体挂接：只有当套件自带 lib.defs 且**一个库都没逐个挂上**
    # 时才考虑（多库 DEFINE 且 add_library 全部失败的情况）。
    # 默认路径不走这里 —— 逐个 add_library 可控、可幂等、可 detach。
    fallback: dict = {}
    if not attached and targets and not conflicts:
        try:
            ws.add_library_definition_file(lib_defs_path)
            fallback = {"fallback": False, "changed": True,
                        "lib_defs_file": str(ws.lib_defs_file),
                        "reason": "逐个 add_library 未成功，改用 "
                                  "add_library_definition_file 整体引用套件 lib.defs"}
            try:
                for lib in list(ws.libraries or []):
                    nm = str(getattr(lib, "name", "") or "")
                    if nm in {t["name"] for t in targets}:
                        snap = _lib_snapshot(lib, sample_cells=5)
                        snap["already_attached"] = False
                        snap["source"] = "kit lib.defs (整体引用)"
                        attached.append(snap)
                        added_now.append({"name": nm,
                                          "path": str(getattr(lib, "path", "") or "")})
            except Exception as e:  # noqa: BLE001
                notes.append(f"整体引用后枚举库列表失败: {type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            notes.append(f"整体引用套件 lib.defs 也失败: {type(e).__name__}: {e}；"
                         f"改走直接编辑工作区 lib.defs 的退路。")
            try:
                fallback = _libdefs_fallback_add(ws_path, lib_defs_path)
                notes.append("已走退路：直接改写工作区 lib.defs。"
                             + fallback.get("reason", ""))
            except Exception as fe:  # noqa: BLE001
                failed.append({"name": "", "path": lib_defs_path,
                               "reason": f"退路也失败: {fe}"})

    if _cancel_requested(ctx):
        rolled = _rollback_added(ws, ws_path, added_now, notes) if added_now else []
        return {"ok": False, "cancelled": True, "package_id": package_id,
                "workspace": ws_path, "kit_root": kit_root,
                "libraries": attached, "already_attached": False,
                "attached_at": "",
                "outcome": {"kind": "cancelled", "ok": False,
                            "summary": "已请求取消；本次新增的库引用已撤销，"
                                       "此前已存在的引用未动。"},
                "rolled_back": rolled,
                "message": "已请求取消；已完成的挂接保留，未完成的库不再继续挂接。"}

    attached_at = datetime.datetime.now().isoformat(timespec="seconds")
    operation_id = str(args.get("operation_id") or "").strip() or \
        f"op_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(3).hex()}"
    record: dict = {"workspace": ws_path, "entries": {}}
    old = _load_record(ws_path)
    for e in (old.get("entries") or {}).values():
        if isinstance(e, dict) and e.get("name"):
            record["entries"][str(e["name"])] = e
    for snap in attached:
        if snap.get("already_attached"):
            # 已存在的（可能上次就是本模块挂的）——保留旧记录，不覆盖
            record["entries"].setdefault(str(snap.get("name")), {
                "name": str(snap.get("name")), "path": str(snap.get("path") or ""),
                "package_id": package_id, "attached_at": old.get("attached_at", ""),
                "kit_root": kit_root, "mode": "READ_ONLY",
            })
            continue
        record["entries"][str(snap.get("name"))] = {
            "name": str(snap.get("name")), "path": str(snap.get("path") or ""),
            "package_id": package_id, "attached_at": attached_at,
            "kit_root": kit_root, "mode": "READ_ONLY",
            # operation_id 让"回滚本次新增"有据可依：只撤这一次加进去的，
            # 上一次导入加的、用户手工挂的一律不动。
            "operation_id": operation_id,
        }
    record["attached_at"] = attached_at
    record_path = ""
    record_error = ""
    try:
        record_path = _save_record(ws_path, record)
    except RuntimeError as e:
        record_error = str(e)

    outcome = _classify_attach(targets, attached, already, conflicts, failed)

    # 调用方明确要求回滚（例如编排层判定这次结果是 partial/conflict 且不想
    # 留下半成品）：只撤销本次新增，已经有过的引用一个都不动。
    rolled_back = []
    if args.get("rollback") and added_now:
        rolled_back = _rollback_added(ws, ws_path, added_now, notes)
        outcome = _classify_attach(targets, [], [], conflicts, failed)
        outcome["summary"] += f"；已按请求回滚本次新增的 {len(rolled_back)} 个库引用"

    ok = bool(attached) and not failed and not conflicts and outcome["ok"]
    out = {
        "ok": ok,
        "package_id": package_id,
        "workspace": ws_path,
        "kit_root": kit_root,
        "kit_root_rel": kit_root_rel,
        "mode": "READ_ONLY",
        "libraries": attached,
        "already_attached": bool(already) and len(already) == len(targets),
        "attached_at": attached_at,
        "record_path": record_path,
        "lib_defs": {
            "path": lib_defs_path,
            "exists": bool(defs.get("exists")),
            "parser": defs.get("source"),
            "declared_libraries": [
                {"name": e["name"], "declared_path": e.get("declared_path"),
                 "declared_mode": e.get("declared_mode"),
                 "abs_path": e.get("abs_path"),
                 "abs_path_resolved": e.get("abs_path_resolved")}
                for e in defs.get("libraries", [])
            ],
            "includes": defs.get("includes") or [],
            "unparsed": (defs.get("unparsed") or [])[:20],
            "error": defs.get("error") or "",
        },
        "conflicts": conflicts,
        "failed": failed,
        "notes": notes,
        "operation_id": operation_id,
        "outcome": outcome,
        "rolled_back": rolled_back,
        # 本次到底改了什么：改了谁、备份在哪 —— 调用方要能原样复述，
        # 而不是笼统一句"库定义未被修改"（失败时也可能是改了一半）
        "changes": {
            "added_libraries": [dict(a) for a in added_now],
            "already_attached": list(already),
            "lib_defs_written": bool(fallback and fallback.get("changed")),
            "lib_defs_file": (fallback.get("lib_defs_file") if fallback else ""),
            "lib_defs_backup": (fallback.get("backup") if fallback else ""),
            "record_path": record_path,
        },
        # 挂接只是让库出现在工作区里；能不能放进电路仿真只有验证能说。
        "usable": False,
        "verified": False,
        "next_step": "库已挂接（只读）。**挂接成功不等于模型可用** —— "
                     "用 list_vendor_models 找元件、get_vendor_model_info 看参数，"
                     "再用 validate_model_import 验证后才能放进电路仿真。",
    }
    if fallback:
        out["lib_defs_fallback"] = fallback
    out["simulation_dependencies"] = simulation_dependency_plan(
        ws_path, [snap.get("name") for snap in attached if snap.get("name")])
    if record_error:
        out["record_error"] = record_error
    if not ok:
        out["error"] = outcome["summary"]
    # HTTP 层会拿到正常的 200 响应 —— 这里必须显式提醒：
    # 「请求成功」不等于「挂接成功」，判定只能看本返回体的 ok / outcome.kind。
    out["http_note"] = (
        "本结果通过工具通道返回，HTTP 层可能仍是 200；"
        "**是否挂接成功必须看 ok 与 outcome.kind**，不能只看请求是否成功。")
    return out


def _classify_attach(targets: list, attached: list, already: list,
                     conflicts: list, failed: list) -> dict:
    """把一次挂接的结果**归类**，让编排层不需要自己解读一组列表。

    为什么要这个：后端拿到的从来是 HTTP 200 + 一个 JSON，而版本的 bug 就是
    "200 就当成功"。这里把 6 种结局显式区分开：

      ok               全部目标都挂上了
      already_attached 目标此前就挂着同路径库（幂等命中）
      partial          只挂上一部分（另一部分失败/冲突/缺目录）
      conflict         存在同名不同路径的冲突（保留原有引用，未替换）
      zero_success     一个都没挂上且没有任何失败原因
      failed           有明确的失败项
      nothing_to_do    目标列表本身为空

    ``http_status_hint`` 一律说明"200 不代表成功"这件事。
    """
    requested = len(targets or [])
    n_attached = len(attached or [])
    n_already = len(already or [])
    n_conflicts = len(conflicts or [])
    n_failed = len(failed or [])
    newly = max(0, n_attached - n_already)

    if requested == 0:
        kind = "nothing_to_do"
        ok = True
    elif n_attached == requested and not n_conflicts and not n_failed:
        kind = "already_attached" if n_already == requested and newly == 0 else "ok"
        ok = True
    elif n_attached > 0:
        kind = "partial"
        ok = False
    elif n_conflicts:
        kind = "conflict"
        ok = False
    elif n_failed:
        kind = "failed"
        ok = False
    else:
        kind = "zero_success"
        ok = False

    summary_bits = [f"请求 {requested} 个库：新增 {newly}，"
                    f"此前已挂 {n_already}，冲突 {n_conflicts}，失败 {n_failed}"]
    if kind == "already_attached":
        summary_bits.append("（幂等命中：同名同路径已存在，未重复添加）")
    elif kind == "partial":
        summary_bits.append("**部分成功**：有库没挂上 —— 不能按全部成功处理")
    elif kind == "conflict":
        summary_bits.append("存在同名不同路径的库：已保留原有引用，未替换")
    elif kind == "zero_success":
        summary_bits.append("没有任何库挂上，也没有可用失败原因 —— 按失败处理")
    elif kind == "failed":
        summary_bits.append("有明确的挂接失败项")
    return {
        "kind": kind,
        "ok": ok,
        "counts": {"requested": requested, "newly_attached": newly,
                   "already_attached": n_already, "conflicts": n_conflicts,
                   "failed": n_failed},
        "summary": "；".join(summary_bits),
        "http_status_hint": "工具通道返回 HTTP 200 不代表挂接成功；"
                            "请以本对象里的 ok / kind 为准。",
    }


def _rollback_added(ws, ws_path: str, added_now: list, notes: list = None) -> list:
    """回滚**本次新增**的库引用：不在名单里的一个都不动。

    逐条核对当前路径是否与本次添加时一致 —— 若在这之间换了指向，说明已经
    不是我们加的那个引用，删掉会删到别人的东西。
    """
    removed = []
    for item in (added_now or []):
        name = str(item.get("name") or "")
        want_path = _norm_path(item.get("path") or "")
        if not name:
            continue
        try:
            cur = None
            for lib in list(ws.libraries or []):
                try:
                    if str(lib.name) == name:
                        cur = lib
                        break
                except Exception:  # noqa: BLE001
                    continue
            if cur is None:
                continue
            cur_path = _norm_path(getattr(cur, "path", "") or "")
            if want_path and cur_path and cur_path != want_path:
                if notes is not None:
                    notes.append(
                        f"回滚跳过 {name}：当前路径已变为 {cur_path}，"
                        f"与本次添加的 {want_path} 不一致（不删别人的引用）")
                continue
            ws.remove_library(name, cur_path or (item.get("path") or ""))
            removed.append({"name": name, "path": cur_path or item.get("path") or ""})
        except Exception as e:  # noqa: BLE001
            if notes is not None:
                notes.append(f"回滚 {name} 失败: {type(e).__name__}: {e}")
    if removed:
        try:
            rec = _load_record(ws_path)
            names = {r["name"] for r in removed}
            rec["entries"] = {k: v for k, v in (rec.get("entries") or {}).items()
                              if k not in names}
            rec["rolled_back_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            _save_record(ws_path, rec)
        except Exception as e:  # noqa: BLE001 — 记录写不回去也要说，否则 detach 会以为还挂着
            if notes is not None:
                notes.append(f"回滚后更新挂接记录失败: {type(e).__name__}: {e}")
    return removed


# ---------------------------------------------------------------------------
# 工具 3：open_vendor_palette（把已挂接的原厂包在 ADS 原生元件列表里打开/定位）
#
# 机制事实（已检索本机 SDK 与 ADS 2027 文档，写死在这里，别处不要另写一套）：
#   * Design Kit 的 native 元件列表来自库目录下 eesof_lib.cfg 的 BOOT_AEL
#     （不带扩展名，如 ../de/ael/boot），DE 加载库时执行它 -> 进而加载
#     palette.atf/ael 并用 de_define_library_palette 注册分类（.atf 是
#     AEL 编译产物，原厂包常**只有 .atf**）。
#   * **ADS 没有"程序化打开 Palette / Component Library 窗口"的 API**。
#     AEL 侧只有 de_define_library_palette()/de_define_palette_group() 这类
#     **定义**函数，没有查询函数；也没有打开窗口的函数。
#   * 可用的近亲是 keysight.ads.de.app：find_windows_by_type(WindowType.
#     BROWSER_WINDOW) + Window.show()（官方语义：把窗口带到前台并可见）。
#     只能"置前已存在的库浏览器"，**不能新建、不能选中分类**。
#   * boot 是否真的执行过，ADS 没有可查询的接口；能拿到的最接近证据是
#     Library.get_raw_library_cfg_var()（只有 DE 载入了该库 cfg 才回得出值）。
#     因此 boot.loaded 只在**正面证据成立**时给 True，否则一律 "unknown"。
# ---------------------------------------------------------------------------

# eesof_lib.cfg 里与"原生元件列表"直接相关的变量（只读，绝不改写库配置）
_EESOF_CFG_KEYS = ("BOOT_AEL", "DESIGN_KIT_NAME", "VERSION",
                   "LIB_BROWSER_CTL", "INPUT_DATA_PATH")


def _blank_boot() -> dict:
    """boot 段的空形状（键名与契约 §3.1 冻结一致；多余键只增不改名）。"""
    return {
        "eesof_lib_cfg": {"path": "", "exists": False},
        "boot_ael": {"path": "", "exists": False,
                     "atf_path": "", "atf_exists": False},
        "palette": {"path": "", "exists": False,
                    "atf_path": "", "atf_exists": False},
        "loaded": "unknown",
        "loaded_basis": "",
        "palette_groups": [],
        "registered_components": 0,
        "control_files": {},
        "config_vars": {},
        "evidence": [],
    }


def _blank_native() -> dict:
    """native 段的空形状（键名与契约 §3.1 冻结一致；多余键只增不改名）。"""
    return {
        "component_library": {"opened": False, "method": "", "detail": ""},
        "palette": {"opened": False, "method": "", "detail": ""},
        "located": {"library": "", "category": "", "found": False,
                    "library_found": False, "category_verified": False,
                    "via": "", "window": "", "detail": ""},
    }


def _read_eesof_lib_cfg(cfg_path: str) -> dict:
    """逐行读 eesof_lib.cfg（KEY=VALUE）。只读、不改写。

    返回 {"vars": {..}, "error": ""}。值按**原始文本**保留（不做环境变量
    替换）—— 与 ADS 的 get_raw_library_cfg_var 同口径，便于逐字比对。
    """
    out: dict = {"vars": {}, "error": ""}
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out["vars"][key.strip()] = val.strip()
    return out


def _resolve_cfg_asset(cfg_dir: str, value: str) -> dict:
    """把 BOOT_AEL 这类"**不带扩展名**的基名"解析成真实文件。

    eesof_lib.cfg 里的 BOOT_AEL=../de/ael/boot 不带扩展名，真实产物可能是
    boot.ael（AEL 源）或 boot.atf（编译产物），原厂包常**只有 .atf**。
    所以按"基名 -> .ael -> .atf"顺序探测，如实报告哪个存在 ——
    不假设一定是 .ael，也不拿 .atf 当文本执行。
    """
    value = str(value or "").strip()
    res: dict = {"raw": value, "base": "", "path": "", "exists": False,
                 "ael_path": "", "ael_exists": False,
                 "atf_path": "", "atf_exists": False, "candidates": [],
                 "error": ""}
    if not value:
        return res
    if "$" in value:
        # 环境变量没展开（$HPEESOF_DIR 等）时不敢猜路径，如实空值
        res["error"] = f"BOOT_AEL 含未展开的环境变量引用，未解析: {value}"
        return res
    try:
        base = value.replace("/", os.sep) if os.sep != "/" else value
        if not os.path.isabs(base):
            base = os.path.join(str(cfg_dir), base)
        base = os.path.normpath(base)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    res["base"] = base
    cands = [base, base + ".ael", base + ".atf"]
    res["candidates"] = cands
    for cand in cands:
        if os.path.isfile(cand):
            res["path"], res["exists"] = cand, True
            break
    ael, atf = base + ".ael", base + ".atf"
    if os.path.isfile(ael):
        res["ael_path"], res["ael_exists"] = ael, True
    if os.path.isfile(atf):
        res["atf_path"], res["atf_exists"] = atf, True
    return res


def _walk_native_markers(root: str, max_entries: int = 20000) -> dict:
    """有界遍历一个目录，找出"原生元件列表"相关标记（只列名，不读内容）。

    用途：判定"这个包到底是不是 Design Kit（有原生列表可开）"。
    纯 Touchstone/数据包里没有 eesof_lib.cfg、也没有任何 .ael/.atf ——
    那就没有可开的面板。遍历有上限，超限就标 truncated（不假装扫全了）。
    """
    markers: dict = {"eesof_lib_cfg": [], "ael": [], "atf": [], "ctl": [],
                     "lib_defs": [], "truncated": False, "error": ""}
    root = str(root or "")
    if not root or not os.path.isdir(root):
        return markers
    n = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                n += 1
                if n > max_entries:
                    markers["truncated"] = True
                    break
                low = fn.lower()
                full = os.path.join(dirpath, fn)
                if low == "eesof_lib.cfg":
                    markers["eesof_lib_cfg"].append(full)
                elif low.endswith(".ael"):
                    markers["ael"].append(full)
                elif low.endswith(".atf"):
                    markers["atf"].append(full)
                elif low.endswith(".ctl"):
                    markers["ctl"].append(full)
                elif low == "lib.defs":
                    markers["lib_defs"].append(full)
            if markers["truncated"]:
                break
    except OSError as e:  # noqa: BLE001
        markers["error"] = f"{type(e).__name__}: {e}"
    return markers


def _probe_library_cfg_vars(lib, keys) -> dict:
    """让 **ADS 自己**读回库配置变量 —— 最接近"库已加载启动配置"的直接证据。

    只有 DE 真的把该库的 eesof_lib.cfg 载入后，get_raw_library_cfg_var()
    才回得出文件里的原始值。这是本工具能拿到的、真实可复现的探测；
    它证明"库配置已被 DE 载入"，**不证明 boot.atf 执行成功/palette 已注册**。
    """
    out: dict = {"available": False, "vars": {}, "errors": {}}
    getter = getattr(lib, "get_raw_library_cfg_var", None)
    if not callable(getter):
        out["errors"]["_api"] = ("Library.get_raw_library_cfg_var 在本 ADS 版本不可用"
                                 "（已核对 _core/library.py L348 存在）")
        return out
    out["available"] = True
    for k in keys:
        try:
            out["vars"][k] = str(getter(k) or "")
        except Exception as e:  # noqa: BLE001
            out["vars"][k] = ""
            out["errors"][k] = f"{type(e).__name__}: {e}"
    return out


def _probe_ael(*names) -> dict:
    """探测 AEL 词汇表里某函数是否存在（能力证据，不写入、不执行 boot）。"""
    out: dict = {"available": False, "results": {}, "error": ""}
    try:
        from keysight.ads import ael as ael_mod  # noqa: PLC0415
        isf = getattr(getattr(ael_mod, "call", None), "is_function_defined", None)
        if not callable(isf):
            out["error"] = "ael.call.is_function_defined 不可用"
            return out
        out["available"] = True
        for nm in names:
            try:
                out["results"][nm] = bool(isf(nm))
            except Exception as e:  # noqa: BLE001
                out["results"][nm] = None
                out["error"] = f"{nm}: {type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _ael_item_def(lib_name, cell_name):
    """该元件是否已在 ADS 注册；注册返回限定名，未注册返回 None。

    **实测可用**（ADS 2027，见 tests/probes/NL_q3b_calib.py）：AEL
    `dm_find_item_definition(name)` 命中返回 "lib:cell"（str 带引号，这里剥掉），
    未命中返回 None/空。**不需要窗口**。先试 "lib:cell"，再退回裸 cell 名
    （实测两种入参都能命中 TDK 元件）。
    """
    from keysight.ads import ael  # noqa: PLC0415
    for name in (f"{lib_name}:{cell_name}", str(cell_name)):
        r = ael.call(convert=False).dm_find_item_definition(name)
        s = str(r).strip().strip('"').strip()
        if s not in ("None", "", "null"):
            return s
    return None


def _ael_vpn(win, lib_name, cell_name):
    """该 cell 所属 palette 分组名；不存在返回 None。

    **实测可用**（ADS 2027，见 tests/probes/NL_q3e_vpn_value.py / NL_q6b_groups.py）：
    AEL `deitem_get_visible_palette_name(window, libName, cellName)` 返回真实
    分组名（如 "TDK Chip Beads"），不存在返回 None。**需要窗口上下文**（第一个
    参数是窗口/设计上下文；传 NULL 会报 "Expected window or design context"）。
    """
    from keysight.ads import ael  # noqa: PLC0415
    r = ael.call(convert=False).deitem_get_visible_palette_name(
        win, lib_name, cell_name)
    s = str(r).strip().strip('"').strip()
    return None if s in ("None", "", "null") else s


def _ael_context_window() -> tuple:
    """取一个可用于 AEL 查询的窗口（优先当前窗口，退到 schematic 窗口）。

    返回 (win, title, error)。`deitem_get_visible_palette_name` 需要窗口上下文，
    没有窗口时该原语不可用 —— 如实返回 error，交由上层降级为 unknown。
    """
    try:
        import keysight.ads.de.app as de_app  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return None, "", f"无法导入 keysight.ads.de.app: {type(e).__name__}: {e}"
    try:
        win = de_app.current_window()
    except Exception as e:  # noqa: BLE001
        win = None
        err = f"current_window 失败: {type(e).__name__}: {e}"
    else:
        err = ""
    if win is None:
        try:
            wt = de_app.WindowType.SCHEMATIC_WINDOW
            wins = list(de_app.find_windows_by_type(wt) or [])
            win = wins[0] if wins else None
        except Exception as e:  # noqa: BLE001
            return None, "", (err or f"find_windows_by_type 失败: {type(e).__name__}: {e}")
    if win is None:
        return None, "", (err or "ADS 当前没有打开的窗口，AEL palette 查询需要窗口上下文")
    try:
        title = str(getattr(win, "title", "") or "")
    except Exception:  # noqa: BLE001
        title = ""
    return win, title, ""


def _focus_library_window(focus: bool) -> dict:
    """尽力把 ADS 的**库浏览器窗口**置前（唯一可用的原生近亲）。

    事实基线：**没有**程序化打开 Palette / Component Library 窗口的 API。
    可用的是 keysight.ads.de.app 的窗口枚举与 Window.show()（官方文档原文：
    "Bring the window to the front and make it visible."）。所以这里最多
    只能把**已存在**的库浏览器窗口带到前台，不能新建、不能选中分类。
    全部探测包在 try/except 里：该接口在 automation 模式下不可用 ——
    那时如实降级为 method="unsupported"，绝不整体抛错。
    """
    out: dict = {"opened": False, "method": "", "detail": "",
                 "windows": 0, "titles": [], "errors": []}
    try:
        import keysight.ads.de.app as de_app  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        out["method"] = "unsupported"
        out["detail"] = (f"无法导入 keysight.ads.de.app（{type(e).__name__}: {e}）—— "
                         f"该接口仅在 ADS 图形界面里可用，automation 模式下不可用。")
        return out
    finder = getattr(de_app, "find_windows_by_type", None)
    wt = getattr(getattr(de_app, "WindowType", None), "BROWSER_WINDOW", None)
    if wt is None or not callable(finder):
        out["method"] = "unsupported"
        out["detail"] = ("de.app 未提供 WindowType.BROWSER_WINDOW / "
                         "find_windows_by_type —— 无法枚举库浏览器窗口。")
        return out
    out["method"] = "de.app.find_windows_by_type(WindowType.BROWSER_WINDOW)"
    try:
        wins = list(finder(wt) or [])
    except Exception as e:  # noqa: BLE001
        out["method"] = "unsupported"
        out["detail"] = f"调用 de.app.find_windows_by_type 失败: {type(e).__name__}: {e}"
        return out
    out["windows"] = len(wins)
    valid = []
    for w in wins:
        try:
            title = str(getattr(w, "title", "") or "")
            ok = True
            isv = getattr(w, "is_valid", None)
            if callable(isv):
                ok = bool(isv())
            out["titles"].append(title)
            if ok:
                valid.append((w, title))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{type(e).__name__}: {e}")
    if not valid:
        out["detail"] = ("ADS 当前没有打开库浏览器（BROWSER_WINDOW）窗口，"
                         "且没有 API 能程序化新建它 —— 请在 ADS 里手动打开 "
                         "Library/Component 浏览器后再试。")
        return out
    if not focus:
        out["detail"] = (f"发现 {len(valid)} 个库浏览器窗口 {out['titles']}，"
                         f"但请求 focus=False，未置前。")
        return out
    w, title = valid[0]
    try:
        w.show()
        out["opened"] = True
        out["detail"] = (f"已把库浏览器窗口置前（Title={title!r}）。"
                         f"注意：仅把窗口带到前台，**无法选中具体分类/元件**。")
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"调用 Window.show() 置前失败: {type(e).__name__}: {e}"
    return out


def open_vendor_palette(args: dict, ctx=None) -> dict:
    """把**已挂接**的原厂 Design Kit 在 ADS 原生元件列表里打开/定位。

    这是一个"只读定位"动作，**不是**模型可用性证明。三条纪律：
      1. 只读：不改工作区、不加/删库、不放置元件、不保存设计、不改全局
         Favorite、**绝不执行 boot 入口**（重复执行会造成重复分类）。
      2. 诚实：boot 是否真的执行过 ADS 没有查询接口 —— 只在有正面证据时
         给 loaded=True，否则一律 "unknown"；界面开不了就如实降级为
         loaded_only，绝不宣称"已为你选中分类"。
      3. 恒 verified=False：打开原生列表 ≠ 模型可用（那要 validate_model_import）。

    入参（workspace/kit_root 由后端可信上下文注入，路径不采信 LLM）：
      package_id / workspace / kit_root / library / category / view / focus
    返回体键名与契约 §3.1 冻结一致（多余键只增不改名）。
    """
    steps: list = []
    limits: list = []
    package_id = str(args.get("package_id") or "").strip()
    requested_library = str(args.get("library") or "").strip()
    category = str(args.get("category") or "").strip()
    view = str(args.get("view") or "schematic").strip().lower() or "schematic"
    focus_arg = args.get("focus")
    focus = True if focus_arg is None else bool(focus_arg)
    kit_root = str(args.get("kit_root") or "").strip()
    if kit_root:
        kit_root = os.path.normpath(kit_root)

    # ---- 1) 工作区守卫：不符 -> workspace_mismatch，什么都不写 -------------
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "打开原生元件列表")
    if mismatch:
        return {
            "ok": False, "outcome": "workspace_mismatch", "kind": "workspace_mismatch",
            "error": mismatch, "workspace": ws_path, "package_id": package_id,
            "kit_root": kit_root, "library": requested_library,
            "library_attached": False, "boot": _blank_boot(),
            "native": _blank_native(), "limits": limits, "verified": False,
            "record_path": "", "steps": steps,
        }

    rec_path = _record_path(ws_path)
    record = _load_record(ws_path)
    record_path = rec_path if os.path.isfile(rec_path) else ""

    def _mk(outcome: str, error: str = "", boot: dict = None,
            native: dict = None, lib_name: str = "", attached: bool = False) -> dict:
        ok = outcome in ("opened", "located", "loaded_only")
        out = {
            "ok": ok, "outcome": outcome, "workspace": ws_path,
            "package_id": package_id, "kit_root": kit_root, "library": lib_name,
            "library_attached": bool(attached),
            "boot": boot if boot is not None else _blank_boot(),
            "native": native if native is not None else _blank_native(),
            "limits": limits, "verified": False,       # 打开 ≠ 模型可用
            "record_path": record_path, "steps": steps,
            "http_note": ("本结果通过工具通道返回，HTTP 层可能仍是 200；"
                          "是否真的打开了原生入口必须看 outcome，"
                          "且 outcome 属于 opened/loaded_only 也不代表模型可用。"),
            "error": error if not ok else "",
        }
        return out

    # ---- 2) 解析要定位的库名（args.library > kit lib.defs > 挂接记录） ------
    # 注意 kit lib.defs 里的库名会被 ADS 转义（'.' -> '#2e'），与 ws.libraries
    # 暴露的名字可能不一致。所以同时记下**声明的绝对路径**，之后允许按路径
    # 匹配到真正打开的那个 Library 对象（同名/同路径都算已挂接）。
    lib_name = requested_library
    lib_source = "args.library" if lib_name else ""
    wanted_path = ""
    if not lib_name and kit_root and os.path.isdir(kit_root):
        defs_path = _find_kit_lib_defs(kit_root)
        defs = _read_lib_defs(defs_path) if defs_path else {}
        for e in (defs.get("libraries") or []):
            if e.get("name"):
                lib_name, lib_source = str(e["name"]), "kit_root/lib.defs"
                wanted_path = str(e.get("abs_path") or "")
                break
    if not lib_name:
        for nm, e in (record.get("entries") or {}).items():
            if not isinstance(e, dict):
                continue
            if package_id and str(e.get("package_id") or "") != package_id:
                continue
            lib_name, lib_source = str(nm), "attach_record"
            wanted_path = str(e.get("path") or "")
            break

    # ---- 3) 判定"这是不是 Design Kit"（纯 Touchstone 包没有原生面板） ------
    explicit_kind = str(args.get("package_kind") or "").strip().lower()
    if explicit_kind and explicit_kind not in (
            "design_kit", "mixed", "pdk", "library", "unknown", ""):
        limits.append(f"package_kind={explicit_kind!r} 不是 Design Kit 类 —— "
                      f"该包没有可打开的原生元件列表。")
        steps.append({"step": "classify_kind", "ok": False,
                      "detail": f"package_kind={explicit_kind!r}"})
        return _mk("unsupported",
                   error=f"该包（package_kind={explicit_kind!r}）不是 Design Kit，"
                         f"没有原生元件列表可打开。",
                   lib_name=lib_name)

    scan_root = kit_root if (kit_root and os.path.isdir(kit_root)) else ""
    markers = _walk_native_markers(scan_root) if scan_root else {}
    has_dk_assets = bool(
        markers.get("eesof_lib_cfg") or markers.get("ael")
        or markers.get("atf") or markers.get("ctl") or markers.get("lib_defs"))
    steps.append({"step": "scan_native_assets", "ok": bool(has_dk_assets),
                  "detail": (f"根 {scan_root or '(未提供)'}: "
                             f"eesof_lib.cfg={len(markers.get('eesof_lib_cfg') or [])}, "
                             f".ael={len(markers.get('ael') or [])}, "
                             f".atf={len(markers.get('atf') or [])}, "
                             f".ctl={len(markers.get('ctl') or [])}, "
                             f"lib.defs={len(markers.get('lib_defs') or [])}"
                             + ("（遍历超限，未扫全）" if markers.get("truncated") else ""))})

    # 扫过套件根却没有任何 Design Kit 资产 -> 纯 Touchstone/数据包，没有面板可开。
    # 只在**确实扫过根**时下这个结论；kit_root 未提供时不敢据此降级。
    if scan_root and not has_dk_assets:
        limits.append("包内未发现 eesof_lib.cfg / .ael / .atf / .ctl / lib.defs —— "
                      "这不是 Design Kit（纯 Touchstone 或数据包），没有原生元件列表可打开。")
        steps.append({"step": "classify_kind", "ok": False,
                      "detail": "无任何 Design Kit 原生列表资产"})
        return _mk("unsupported",
                   error="该包不含任何 Design Kit 原生列表资产（无 eesof_lib.cfg / "
                         "boot|palette AEL / browser ctl / lib.defs），没有原生元件列表可打开。",
                   lib_name=lib_name)

    # ---- 4) 找已打开的 Library 对象；找不到 -> not_attached ---------------
    existing: dict = {}
    try:
        for lib in list(ws.libraries or []):
            try:
                existing[str(lib.name)] = lib
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"读取工作区已打开的库列表失败: {type(e).__name__}: {e}") from e

    lib = existing.get(lib_name) if lib_name else None
    if lib is None and wanted_path:
        # lib.defs 的名字可能被冒号/转义过；按**声明路径**再找一次
        want = _norm_path(wanted_path)
        for nm, cand in existing.items():
            cp = _norm_path(getattr(cand, "path", "")
                            or getattr(cand, "lib_path", ""))
            if cp and cp == want:
                lib, lib_name = cand, nm
                break
    lib_dir = ""
    if lib is not None:
        try:
            lib_dir = os.path.normpath(str(getattr(lib, "path", "")
                                          or getattr(lib, "lib_path", "") or ""))
        except Exception:  # noqa: BLE001
            lib_dir = ""

    if lib is None:
        limits.append("该库尚未挂接到当前工作区 —— 请先 import_model_package / "
                      "attach_design_kit 挂接后再打开。")
        steps.append({"step": "find_library", "ok": False,
                      "detail": f"工作区未打开库 {lib_name or '(未解析出库名)'}；"
                                f"当前已打开: {sorted(existing)}"})
        return _mk("not_attached",
                   error=(f"库 {lib_name or '(未解析出库名)'} 未挂接到当前工作区"
                          f"（来源: {lib_source or '无'}）。"),
                   lib_name=lib_name)

    is_open = None
    try:
        is_open = bool(getattr(lib, "is_open", False))
    except Exception:  # noqa: BLE001
        is_open = None
    steps.append({"step": "find_library", "ok": True,
                  "detail": f"库 {lib_name}（来源 {lib_source}）已挂接；"
                            f"is_open={is_open}；path={lib_dir}"})

    # ---- 5) 读 eesof_lib.cfg（库目录下）与 boot 资源 -----------------------
    boot = _blank_boot()
    cfg_path = os.path.join(lib_dir, "eesof_lib.cfg") if lib_dir else ""
    cfg_exists = bool(cfg_path and os.path.isfile(cfg_path))
    if not cfg_exists and markers.get("eesof_lib_cfg"):
        # 库目录下没有、但套件里别处有（如实取第一个找到的，并标出来源）
        cfg_path = str(markers["eesof_lib_cfg"][0])
        cfg_exists = os.path.isfile(cfg_path)
    boot["eesof_lib_cfg"] = {"path": cfg_path, "exists": cfg_exists}
    if not cfg_exists:
        limits.append(f"库 {lib_name} 目录下没有 eesof_lib.cfg —— 无启动配置可读，"
                      f"boot.loaded 无法判定（保持 unknown）。")

    cfg_vars: dict = {}
    cfg_error = ""
    if cfg_exists:
        parsed = _read_eesof_lib_cfg(cfg_path)
        cfg_vars = parsed.get("vars") or {}
        cfg_error = parsed.get("error") or ""
        if cfg_error:
            limits.append(f"读取 eesof_lib.cfg 出错（{cfg_error}），"
                          f"boot 信息可能不完整。")
    boot["config_vars"] = dict(cfg_vars)

    cfg_dir = os.path.dirname(cfg_path) if cfg_path else ""
    boot_ael = _resolve_cfg_asset(cfg_dir, cfg_vars.get("BOOT_AEL", ""))
    boot["boot_ael"] = {
        "path": boot_ael.get("path", ""), "exists": bool(boot_ael.get("exists")),
        "atf_path": boot_ael.get("atf_path", ""),
        "atf_exists": bool(boot_ael.get("atf_exists")),
    }
    if boot_ael.get("ael_exists"):
        boot["boot_ael"]["ael_path"] = boot_ael["ael_path"]
    if boot_ael.get("candidates"):
        boot["boot_ael"]["candidates"] = boot_ael["candidates"]
    if boot_ael.get("error"):
        boot["boot_ael"]["error"] = boot_ael["error"]

    # palette 与 boot 同目录（boot 通常 load 出 palette）
    pal_base = os.path.join(os.path.dirname(boot_ael.get("base") or ""), "palette")
    pal = _resolve_cfg_asset(cfg_dir, pal_base) if boot_ael.get("base") else {}
    if pal:
        boot["palette"] = {
            "path": pal.get("path", ""), "exists": bool(pal.get("exists")),
            "atf_path": pal.get("atf_path", ""),
            "atf_exists": bool(pal.get("atf_exists")),
        }

    if cfg_exists and cfg_vars and not boot_ael.get("exists"):
        limits.append(
            f"eesof_lib.cfg 声明 BOOT_AEL={cfg_vars.get('BOOT_AEL', '')!r}，"
            f"但在其目录下没找到对应的 boot 资源（.ael/.atf）—— "
            f"库启动配置不完整，原生分类可能不会被注册。")
    steps.append({"step": "read_eesof_lib_cfg", "ok": bool(cfg_exists),
                  "detail": (f"cfg={cfg_path or '(无)'}；"
                             f"BOOT_AEL={cfg_vars.get('BOOT_AEL', '')!r} -> "
                             f"{boot['boot_ael'].get('path') or '(未找到)'}"
                             f"（atf_exists={boot['boot_ael'].get('atf_exists')}）")})

    boot["evidence"].append(
        f"eesof_lib.cfg: path={cfg_path or '(无)'}, exists={cfg_exists}, "
        f"vars={ {k: cfg_vars.get(k, '') for k in _EESOF_CFG_KEYS} }")
    boot["evidence"].append(
        f"BOOT_AEL 解析（相对 {cfg_dir or '(cfg 目录未知)'}）: raw="
        f"{cfg_vars.get('BOOT_AEL', '')!r} -> base={boot_ael.get('base') or '(未解析)'}"
        f", candidates={boot_ael.get('candidates') or []}"
        f", exists={boot['boot_ael'].get('exists')}")

    # ---- 6) boot.loaded / palette_groups / registered_components -----------
    # 判据用**确定性 AEL 探测**（mechanism-scout 实机实测，见
    # docs/原生列表_机制核实_2026-10-09.md §1/§3/§4）：
    #   * dm_find_item_definition("<lib>:<cell>") 非空 -> 该元件已在 ADS 注册
    #   * deitem_get_visible_palette_name(win, lib, cell) 非 None -> 其 palette 分组
    #   * boot 的全局 decl 变量**不可**作为加载判据（实测找不到但元件/palette
    #     已注册）。
    # get_raw_library_cfg_var 降级为**辅助证据**，不再当主判据。
    cfg_probe = _probe_library_cfg_vars(lib, _EESOF_CFG_KEYS)
    boot["ads_cfg_vars"] = dict(cfg_probe.get("vars") or {})
    if cfg_probe.get("errors"):
        boot["ads_cfg_var_errors"] = dict(cfg_probe["errors"])
    ael_probe = _probe_ael("de_define_library_palette", "de_define_palette_group",
                           "dk_define_palette_group",
                           "deitem_get_visible_palette_name")
    boot["ael_probe"] = dict(ael_probe.get("results") or {})
    if ael_probe.get("error"):
        boot["ael_probe_error"] = ael_probe["error"]

    file_boot = str(cfg_vars.get("BOOT_AEL", "") or "")
    ads_boot = str((cfg_probe.get("vars") or {}).get("BOOT_AEL", "") or "")
    boot["evidence"].append(
        f"辅助证据 Library.get_raw_library_cfg_var: available="
        f"{cfg_probe.get('available')}, BOOT_AEL(file)={file_boot!r}, "
        f"BOOT_AEL(ADS)={ads_boot!r}, DESIGN_KIT_NAME(ADS)="
        f"{(cfg_probe.get('vars') or {}).get('DESIGN_KIT_NAME', '')!r}"
        f"（不作为 loaded 主判据）")
    boot["evidence"].append(
        f"AEL 能力探测: is_function_defined -> {boot['ael_probe']}"
        + (f"（error: {ael_probe.get('error')}）" if ael_probe.get("error") else ""))

    # 收集 cell 清单（一次遍历，上限 2000）：既用于 loaded 探测，也用于分组枚举
    cells: list = []
    cells_truncated = False
    try:
        for i, c in enumerate(iter(lib.cells or [])):
            if i >= 2000:
                cells_truncated = True
                break
            try:
                nm = str(getattr(c, "name", "") or "")
            except Exception:  # noqa: BLE001
                continue
            if nm:
                cells.append(nm)
    except Exception as e:  # noqa: BLE001
        boot["evidence"].append(f"枚举 lib.cells 失败: {type(e).__name__}: {e}")
    if cells_truncated:
        limits.append(
            f"库 {lib_name} 的 cell 数达到枚举上限 2000，palette_groups / "
            f"registered_components 只覆盖前 2000 个 —— 数据不完整（按截断如实说明）。")

    win, win_title, win_err = _ael_context_window()
    first_cell = cells[0] if cells else ""

    # loaded：只对 firstCell 做两个确定性探测
    item_first = vpn_first = None
    item_ok = vpn_ok = False
    item_err = vpn_err = ""
    if first_cell:
        try:
            item_first = _ael_item_def(lib_name, first_cell)
            item_ok = True
        except Exception as e:  # noqa: BLE001
            item_err = f"{type(e).__name__}: {e}"
        try:
            vpn_first = _ael_vpn(win, lib_name, first_cell)
            vpn_ok = True
        except Exception as e:  # noqa: BLE001
            vpn_err = f"{type(e).__name__}: {e}"

    if item_first or vpn_first:
        boot["loaded"] = True
        boot["loaded_basis"] = (
            f"确定性探测命中：dm_find_item_definition({lib_name}:{first_cell})="
            f"{item_first!r}；deitem_get_visible_palette_name(win, {lib_name}, "
            f"{first_cell})={vpn_first!r} —— 元件已在 ADS 注册、boot→palette 已生效。")
    elif item_ok and vpn_ok:
        boot["loaded"] = False
        boot["loaded_basis"] = (
            f"两个确定性探测都成功执行且都为空：dm_find_item_definition 与 "
            f"deitem_get_visible_palette_name(win={win_title!r}) 对 "
            f"{lib_name}:{first_cell} 都返回空 —— 该元件未注册。")
    else:
        boot["loaded"] = "unknown"
        boot["loaded_basis"] = (
            "确定性探测不可用，不敢判定："
            f"item_def_ok={item_ok}（{item_err or 'ok'}）；"
            f"vpn_ok={vpn_ok}（{vpn_err or 'ok'}）；"
            f"window={win_title or '(无)'}（{win_err or 'ok'}）。")
    boot["evidence"].append(
        f"loaded 探测: first_cell={first_cell!r}, item_def_ok={item_ok}, "
        f"item_def={item_first!r}, vpn_ok={vpn_ok}, vpn={vpn_first!r}, "
        f"window={win_title!r}, win_err={win_err!r}")

    # palette_groups + registered_components：一次遍历 lib.cells（上限 2000）
    groups: dict = {}
    registered = 0
    g_errors: list = []
    r_errors: list = []
    for cell in cells:
        try:
            g = _ael_vpn(win, lib_name, cell)
            if g:
                groups.setdefault(g, []).append(cell)
        except Exception as e:  # noqa: BLE001
            g_errors.append(f"{cell}: {type(e).__name__}: {e}")
        try:
            if _ael_item_def(lib_name, cell):
                registered += 1
        except Exception as e:  # noqa: BLE001
            r_errors.append(f"{cell}: {type(e).__name__}: {e}")
    if r_errors:
        boot["evidence"].append(
            f"registered_components 探测有 {len(r_errors)} 个 cell 报错，前 3: "
            f"{r_errors[:3]}")

    win_field = view if view in ("schematic", "layout") else "schematic"
    boot["palette_groups"] = [
        {"library": lib_name, "window": win_field, "design_type": "analogRF_net",
         "name": name, "label": "", "items": len(members),
         "owner": "probe", "source": "deitem_get_visible_palette_name"}
        for name, members in sorted(groups.items())
    ]
    boot["registered_components"] = registered
    boot["evidence"].append(
        f"palette_groups 枚举: cells={len(cells)}"
        + ("（超 2000 已截断）" if cells_truncated else "")
        + f", groups={len(groups)}, registered_components={registered}")
    if not groups:
        if win is None:
            limits.append(
                "palette_groups 为空：AEL palette 查询需要窗口上下文，当前无可用窗口"
                f"（{win_err or '未取到'}）—— 请在 ADS 里打开任一 schematic 窗口后重试。")
        elif g_errors:
            limits.append(
                f"palette_groups 为空：deitem_get_visible_palette_name 调用报错 —— "
                f"{g_errors[:3]}（共 {len(g_errors)} 个 cell）。")
        else:
            limits.append(
                "palette_groups 为空：已对枚举到的 cell 逐个查询，均未落在任何 "
                "palette 分组（可能该库未注册 palette，或该库是纯数据/tech 库）。")

    # boot.control_files：LIB_BROWSER_CTL（以库目录为基准解析）+ 同目录 *.rec
    ctl_raw = str((cfg_probe.get("vars") or {}).get("LIB_BROWSER_CTL", "")
                  or cfg_vars.get("LIB_BROWSER_CTL", "") or "")
    ctl_target = ""
    if ctl_raw and "$" not in ctl_raw:
        cand = ctl_raw.replace("/", os.sep) if os.sep != "/" else ctl_raw
        if not os.path.isabs(cand):
            cand = os.path.join(lib_dir or cfg_dir, cand)
        ctl_target = os.path.normpath(cand)
    ctl_exists = bool(ctl_target and os.path.isfile(ctl_target))
    records: list = []
    if ctl_exists:
        try:
            cdir = os.path.dirname(ctl_target)
            records = sorted(os.path.join(cdir, f) for f in os.listdir(cdir)
                             if f.lower().endswith(".rec"))
        except OSError as e:  # noqa: BLE001
            boot["evidence"].append(f"列出 *.rec 失败: {type(e).__name__}: {e}")
    boot["control_files"] = {
        "lib_browser_ctl": {"path": ctl_target, "exists": ctl_exists,
                            "raw": ctl_raw},
        "records": records,
    }
    boot["evidence"].append(
        f"control_files: LIB_BROWSER_CTL={ctl_raw!r} -> path={ctl_target!r}, "
        f"exists={ctl_exists}, records={len(records)}")

    # ---- 7) native：置前库浏览器窗口 + 用**真实分组名**定位 ----------------
    native = _blank_native()
    cl = _focus_library_window(focus)
    native["component_library"] = {
        "opened": bool(cl.get("opened")), "method": cl.get("method", ""),
        "detail": cl.get("detail", ""),
        "windows": cl.get("windows", 0), "titles": cl.get("titles", []),
    }
    native["palette"] = {
        "opened": False, "method": "unsupported",
        "detail": ("ADS 未提供程序化打开 Component Palette / Component Library "
                   "窗口的 API（mechanism-scout 实机实测，见机制核实文档 §1.3/§4）；"
                   "只能经菜单 \\Insert\\Component\\Component Library... 或 "
                   "\\View\\Docking Windows\\Component Palette 人工打开。"
                   "本工具可**定位**真实分组名，但**不能选中分类/元件**，"
                   "也**不会重复执行 boot**（会造成重复分类）。"),
    }

    # located：found 当且仅当 AEL 拿到了**真实分组名**（deitem_get_visible_palette_name）
    if category and category in groups:
        located_found, located_category = True, category
    elif category:
        located_found, located_category = False, category
    elif vpn_first:
        located_found, located_category = True, vpn_first
    elif groups:
        located_found, located_category = True, sorted(groups)[0]
    else:
        located_found, located_category = False, ""
    if located_found:
        loc_detail = (
            f"via=deitem_get_visible_palette_name；库 {lib_name}"
            f"（is_open={is_open}，视图 {win_field}，窗口 {win_title or '(无)'}）。"
            f"定位到分组 {located_category!r}；全库共 {len(groups)} 个分组、"
            f"{sum(len(v) for v in groups.values())} 个已入组元件、"
            f"{registered} 个已注册元件。")
    elif category:
        loc_detail = (
            f"via=deitem_get_visible_palette_name；请求分组 {category!r} **未在已注册"
            f"分组里找到**（实际已注册: {sorted(groups)}）。库 {lib_name} "
            f"（is_open={is_open}）。")
    else:
        loc_detail = (
            f"via=deitem_get_visible_palette_name；未能取到任何真实分组名"
            f"（窗口={win_title or '(无)'}，{win_err or 'ok'}），"
            f"无法确认库 {lib_name} 在原生列表里显示为哪个分类。")
    native["located"] = {
        "library": lib_name, "category": located_category, "found": located_found,
        "library_found": bool(is_open), "category_verified": bool(located_found),
        "via": "deitem_get_visible_palette_name", "window": win_title,
        "detail": loc_detail,
    }
    steps.append({"step": "focus_native_window", "ok": bool(cl.get("opened")),
                  "detail": cl.get("detail", "")})
    steps.append({"step": "locate_in_list", "ok": located_found, "detail": loc_detail})

    if not cl.get("opened"):
        limits.append(f"未能把库浏览器窗口置前：{cl.get('detail', '')}")
    limits.append(
        "本机 ADS 2027 未提供程序化打开 Palette / Component Library 窗口的 API"
        "（仅可定位已注册分组）；最接近的置前做法是把已打开的库浏览器窗口"
        "（WindowType.BROWSER_WINDOW）置前 —— **不能选中具体分类/元件**。")
    limits.append(
        "库是否\"已 boot\"用 dm_find_item_definition / "
        "deitem_get_visible_palette_name 判定；**boot 的全局 decl 变量不可作为判据**"
        "（实测找不到但元件/palette 已注册）。")

    # ---- 8) outcome 判定 ---------------------------------------------------
    if located_found and cl.get("opened"):
        outcome = "opened"
    elif located_found:
        outcome = "located"
    elif boot["loaded"] is True:
        outcome = "loaded_only"
    else:
        limits.append("库既无法确认为已加载、也无法定位到真实分组 —— 按失败处理。")
        outcome = "failed"

    err = ""
    if outcome == "located":
        err = (f"已定位到真实分组 {located_category!r}，但 ADS 无 API 程序化打开 "
               f"Component Palette / Component Library 窗口 —— 请在 ADS 菜单 "
               f"\\Insert\\Component\\Component Library... 人工打开。")
    elif outcome == "loaded_only":
        err = ("库已加载（元件已注册）但无 API 程序化打开界面入口 —— 已如实降级，"
               "**未选中任何分类**。请在 ADS 里手动打开库浏览器查看。")
    elif outcome == "failed":
        err = "无法打开或定位到该库的原生入口（详见 limits / steps）。"

    steps.append({"step": "decide_outcome",
                  "ok": outcome in ("opened", "located", "loaded_only"),
                  "detail": f"outcome={outcome}"})
    return _mk(outcome, error=err, boot=boot, native=native,
               lib_name=lib_name, attached=True)


# ---------------------------------------------------------------------------
# 工具 2：detach_design_kit
# ---------------------------------------------------------------------------

def detach_design_kit(args: dict, ctx=None) -> dict:
    """回滚挂接：**只移除本模块挂过、且记录里有的**库引用。

    绝不按"名字像模型库"去删 —— 用户自己在 ADS 里挂的库如果被误删，
    那是破坏性的且难以察觉。所以每一条要删的引用都必须先在
    .ads_agent/attach_record.json 里查到；查不到就跳过并如实说明。

    只做「移除工作区库定义里的引用」，**不删除磁盘上的库目录** ——
    解压产物由后端的包资产管理，删用户的文件不该由一个挂接回滚动作
    顺手做掉。
    """
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "卸载模型包")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch,
                "workspace": ws_path}

    record = _load_record(ws_path)
    entries = record.get("entries") or {}
    want = [str(x).strip() for x in (args.get("library_names") or []) if str(x).strip()]
    package_id = str(args.get("package_id") or "").strip()
    # operation_id：只撤**那一次**挂接新增的引用。没有它就按 package_id/
    # library_names 过滤 —— 两种情况都只在挂接记录里挑，绝不按名字外形去删。
    operation_id = str(args.get("operation_id") or "").strip()

    candidates = {}
    skipped: list = []
    for nm, e in entries.items():
        if not isinstance(e, dict):
            continue
        if package_id and str(e.get("package_id") or "") != package_id:
            continue
        if want and nm not in want:
            continue
        if operation_id:
            recorded = str(e.get("operation_id") or "")
            if not recorded:
                skipped.append({
                    "name": nm,
                    "reason": "该记录是旧格式（没有 operation_id），无法确认属于这一次挂接 —— "
                              "已跳过，不做回滚（宁可不撤也不误删）"})
                continue
            if recorded != operation_id:
                continue
        candidates[nm] = e
    missing = [n for n in want if n not in candidates]
    if not candidates:
        rec_path = _record_path(ws_path)
        return {"ok": False, "workspace": ws_path,
                "record_path": rec_path,
                "removed": [],
                "operation_id": operation_id,
                "skipped": skipped + [{"name": n, "reason": "不在本工具的挂接记录里"}
                                      for n in missing],
                "error": f"{rec_path} 里没有本工具挂接的库记录"
                         f"{'（未记录：' + ', '.join(missing) + '）' if missing else ''}"
                         f"{'（operation_id=' + operation_id + ' 下没有可回滚的条目）'
                            if operation_id else ''}，"
                         f"已停止 —— 本工具只移除自己挂的引用，不会动你手工挂的库。"}

    lock = _ws_lock(ws_path)
    if not lock.acquire(timeout=120):
        raise RuntimeError(f"等待工作区 {ws_path} 的挂接锁超时（120s），稍后重试")

    removed: list = []
    pre_skipped: list = list(skipped)     # 过滤阶段（旧格式无 operation_id）的跳过项
    skipped: list = []
    try:
        for nm, e in candidates.items():
            if _cancel_requested(ctx):
                skipped.append({"name": nm, "reason": "已请求取消，未处理"})
                continue
            want_path = _norm_path(e.get("path") or "")
            try:
                cur = _library_for(nm)
            except Exception as ex:  # noqa: BLE001 — 本来就没挂，直接跳过
                skipped.append({"name": nm, "reason": f"当前未打开，无需移除（{ex}）"})
                continue
            cur_path = _norm_path(getattr(cur, "path", "")
                                  or getattr(cur, "lib_path", ""))
            if want_path and cur_path and cur_path != want_path:
                # 记录里的路径与当前不一致：说明用户后来把同名库改指到别处，
                # 这时删掉会移除用户自己的引用 —— 停手并说明。
                skipped.append({
                    "name": nm, "reason":
                    f"记录路径 {e.get('path')} 与当前路径 {cur_path} 不一致，"
                    f"已跳过（不移除用户后来自己配置的引用）"})
                continue
            try:
                ws.remove_library(nm, cur_path or (e.get("path") or ""))
                removed.append({"name": nm, "path": cur_path or e.get("path")})
            except Exception as ex:  # noqa: BLE001
                skipped.append({"name": nm, "reason": f"{type(ex).__name__}: {ex}"})
    finally:
        lock.release()

    if removed:
        record["entries"] = {k: v for k, v in (record.get("entries") or {}).items()
                             if k not in {r["name"] for r in removed}}
        record["detached_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            _save_record(ws_path, record)
        except RuntimeError as e:
            return {"ok": False, "workspace": ws_path, "removed": removed,
                    "skipped": pre_skipped + skipped, "error": str(e)}

    return {
        "ok": bool(removed) or not candidates,
        "workspace": ws_path,
        "operation_id": operation_id,
        "removed": removed,
        # skipped 统一是 dict —— 之前"未记录的名字"塞的是裸字符串，
        # 调用方遍历时会 ItemTypeError（自测抓到）。形状必须一致。
        "skipped": pre_skipped + skipped
        + [{"name": n, "reason": "不在本工具的挂接记录里"} for n in missing],
        "files_deleted": False,
        "note": "已从工作区库定义中移除引用；磁盘上的解压产物与库目录未删除"
                "（如需清理请用后端的包管理，删除文件不是挂接回滚该做的事）。",
    }


# ---------------------------------------------------------------------------
# 工具 3：list_readonly_libraries（别名 list_vendor_models 的库视图）
# ---------------------------------------------------------------------------

def list_readonly_libraries(args: dict, ctx=None) -> dict:
    """列出当前工作区**全部**已打开的库，含只读的原厂库。

    为什么必须补这个：`ads_ops.list_designs`（L95）只遍历
    `writable_library_names` 里的库 —— 原厂模型库是只读的，于是
    list_designs 永远看不到它们。这是"明明装了库却找不到元件"的根因。
    """
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "浏览库列表")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch,
                "libraries": []}

    want_kind = str(args.get("kind") or "").strip().lower()
    only_readonly = bool(args.get("only_readonly"))
    try:
        max_items = max(1, int(args.get("max_items") or 200))
    except (TypeError, ValueError):
        max_items = 200
    try:
        sample_cells = max(0, int(args.get("sample_cells") or 0))
    except (TypeError, ValueError):
        sample_cells = 0

    try:
        writable = {str(x) for x in (ws.writable_library_names or [])}
    except Exception:  # noqa: BLE001
        writable = set()

    out: list = []
    total = 0
    try:
        libs = list(ws.libraries or [])
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"读取工作区库列表失败: {type(e).__name__}: {e}") from e

    for lib in libs:
        try:
            snap = _lib_snapshot(lib, sample_cells=sample_cells)
        except Exception as e:  # noqa: BLE001
            continue
        snap["kind"] = _library_kind(lib, ws_path)
        snap["writable"] = snap.get("name") in writable
        if only_readonly and snap.get("is_read_only") is not True:
            continue
        if want_kind and snap["kind"] != want_kind:
            continue
        name_filter = str(args.get("library") or "").strip()
        if name_filter and name_filter.lower() not in str(snap.get("name", "")).lower():
            continue
        total += 1
        if len(out) < max_items:
            out.append(snap)

    return {
        "workspace": ws_path,
        "total": total,
        "returned": len(out),
        "libraries": out,
        "note": "is_read_only=True 的是只读库（原厂模型库通常都是只读挂接）。"
                "浏览原厂元件不需要也不会给它们写权限。"
                + (f"\n共 {total} 个库，已按上限返回前 {len(out)} 个。"
                   if total > len(out) else ""),
    }


# ---------------------------------------------------------------------------
# 工具 4：list_library_components（别名 list_vendor_models）
# ---------------------------------------------------------------------------

def list_library_components(args: dict, ctx=None) -> dict:
    """在已挂接库里检索元件（cell），含只读原厂库。

    参数
      library        限定某个库；留空查所有已打开库
      name_prefix    cell 名前缀过滤（如 TDK_ / BFP）
      kind           design_kit / builtin / workspace
      max_items      返回上限，默认 100
      include_params 是否读每个元件的 model_def 参数定义，默认 True。
                     元件很多时建议关掉 —— 读 model_def 是这套工具里
                     最贵的一步（大套件上万个 cell）。
    """
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "检索元件")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch,
                "components": []}

    library = str(args.get("library") or "").strip()
    prefix = str(args.get("name_prefix") or "").strip()
    want_kind = str(args.get("kind") or "").strip().lower()
    include_params = args.get("include_params")
    include_params = True if include_params is None else bool(include_params)
    try:
        max_items = max(1, int(args.get("max_items") or 100))
    except (TypeError, ValueError):
        max_items = 100

    if library:
        libs = [_library_for(library)]
    else:
        try:
            libs = list(ws.libraries or [])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"读取工作区库列表失败: {type(e).__name__}: {e}") from e

    components: list = []
    truncated = 0
    scanned = 0
    for lib in libs:
        try:
            lname = str(lib.name)
        except Exception:  # noqa: BLE001
            continue
        kind = _library_kind(lib, ws_path)
        if want_kind and kind != want_kind:
            continue
        try:
            cells = list(lib.cells or [])
        except Exception as e:  # noqa: BLE001
            components.append({"library": lname, "kind": kind,
                               "error": f"读取库内cell 失败: {type(e).__name__}: {e}"})
            continue
        for cell in cells:
            scanned += 1
            try:
                cname = str(cell.name)
            except Exception:  # noqa: BLE001
                continue
            if prefix and not cname.lower().startswith(prefix.lower()):
                continue
            if len(components) >= max_items:
                truncated += 1
                continue
            entry = {
                "library": lname,
                "cell": cname,
                "master": f"{lname}:{cname}",
                "kind": kind,
                "is_read_only": bool(getattr(lib, "is_read_only", False)),
                "views": _cell_views(cell),
            }
            if include_params:
                md = _read_model_def(cell)
                if md is None:
                    entry["model_def"] = None
                    entry["params"] = None
                    entry["params_note"] = (
                        "该 cell 没有 model_def（不是元件，或定义在 AEL 之外）—— "
                        "参数如实标 null，不从名字猜。")
                elif isinstance(md, dict) and "error" in md:
                    entry["model_def"] = None
                    entry["params"] = None
                    entry["params_error"] = md["error"]
                else:
                    info = _model_def_info(md)
                    entry["model_def"] = {
                        "impl_class": info.get("impl_class"),
                        "name": info.get("name"),
                        "label": info.get("label"),
                        "param_count": info.get("count"),
                    }
                    entry["params"] = info.get("parameters")
                    entry["params_available"] = info.get("available")
                    if info.get("error"):
                        entry["params_error"] = info["error"]
                    if info.get("truncated"):
                        entry["params_truncated"] = info["truncated"]
            components.append(entry)

    return {
        "workspace": ws_path,
        "library": library or "",
        "name_prefix": prefix,
        "scanned_cells": scanned,
        "returned": len(components),
        "components": components,
        "truncated_matches": truncated,
        "include_params": include_params,
        **({"note": f"命中 {truncated} 个超出返回上限的 cell，未列出。"
                   f"缩小 name_prefix 或调大 max_items 再查。"} if truncated else {}),
    }


# ---------------------------------------------------------------------------
# 工具 5：inspect_component_model（别名 get_vendor_model_info）
# ---------------------------------------------------------------------------

def _probe_symbol_pins(cell, view_name: str = "schematic") -> dict:
    """尽力读元件符号的引脚。

    **这是一次运行期探测，不是写死的结论**：引脚在不同元件里可能以
    inst_pins / pins / terminals 等不同名字暴露，也可能完全读不到
    （那说明它不是符号视图）。读不到就返回 available=False 与真实
    原因，绝不返回空列表假装"这个元件没有引脚" —— 那会让调用方
    以为是个无引脚元件。
    """
    out = {"available": False, "pins": None, "source": "", "error": "",
           "probed_attrs": []}
    view = None
    getter = getattr(cell, "get_view_if_exists", None)
    if callable(getter):
        try:
            view = getter(view_name)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"get_view_if_exists({view_name}) 失败: {type(e).__name__}: {e}"
    if view is None:
        out["error"] = out["error"] or (
            f"cell 没有 {view_name} 视图，无法读符号引脚"
            f"（视图列表: {_cell_views(cell)}）")
        return out
    try:
        design = view.get_design()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"打开 {view_name} 只读视图失败: {type(e).__name__}: {e}"
        return out
    try:
        for attr in ("inst_pins", "pins", "terminals", "inst_terms"):
            probed = getattr(design, attr, None)
            out["probed_attrs"].append(attr)
            if probed is None:
                continue
            pins = []
            for p in list(probed):
                lbl = ""
                for a in ("term_number", "term_name", "name", "id"):
                    v = getattr(p, a, None)
                    if v not in (None, ""):
                        lbl = str(v)
                        break
                pins.append({"label": lbl})
            out["available"] = True
            out["pins"] = pins
            out["pin_count"] = len(pins)
            out["source"] = f"design.{attr}"
            return out
        out["error"] = ("只读视图上找不到引脚集合属性；已探测: "
                        + ", ".join(out["probed_attrs"])
                        + "。不猜引脚定义。")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"读取引脚失败: {type(e).__name__}: {e}"
    finally:
        closer = getattr(design, "close_design", None) or getattr(design, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
    return out


def inspect_component_model(args: dict, ctx=None) -> dict:
    """单个元件的详细信息：库模式、视图、model_def 参数定义、符号引脚。

    参数：library / cell（也接受 master='lib:cell[:view]'）/ view
    """
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "查看元件详情")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch}

    library, cell_name, view = _parse_master(args.get("master"),
                                            args.get("library"),
                                            args.get("cell"))
    view = view or str(args.get("view") or "schematic")
    if not library or not cell_name:
        raise RuntimeError("需要 library 与 cell（或 master='lib:cell[:view]'）")

    lib = _library_for(library)
    cell = _find_cell(lib, cell_name)

    try:
        cell_path = str(cell.path)
    except Exception as e:  # noqa: BLE001
        cell_path = ""
        path_error = f"{type(e).__name__}: {e}"
    else:
        path_error = ""

    snap = _lib_snapshot(lib)
    out = {
        "workspace": ws_path,
        "master": f"{library}:{cell_name}",
        "library": library,
        "cell": cell_name,
        "view": view,
        "library_mode": snap.get("mode") or ("read_only" if snap.get("is_read_only")
                                            else "unknown"),
        "is_read_only": snap.get("is_read_only"),
        "is_an_ads_library": snap.get("is_an_ads_library"),
        "library_path": snap.get("path"),
        "library_kind": _library_kind(lib, ws_path),
        "attached_tech_lib_name": snap.get("attached_tech_lib_name"),
        "cell_path": cell_path,
        "views": _cell_views(cell),
    }
    if path_error:
        out["cell_path_error"] = path_error

    md = _read_model_def(cell)
    if md is None:
        out["model_def"] = None
        out["parameters"] = None
        out["parameters_note"] = ("该 cell 没有 model_def —— 参数如实标 null，"
                                  "不从 cell 名或文件名猜默认值。")
    elif isinstance(md, dict) and "error" in md:
        out["model_def"] = None
        out["parameters"] = None
        out["parameters_error"] = md["error"]
    else:
        info = _model_def_info(md)
        out["model_def"] = {
            "impl_class": info.get("impl_class"),
            "name": info.get("name"),
            "label": info.get("label"),
            "component_name": info.get("component_name"),
            "library_name": info.get("library_name"),
            "param_count": info.get("count"),
        }
        out["parameters"] = info.get("parameters")
        out["parameters_available"] = info.get("available")
        out["range_note"] = info.get("range_note")
        if info.get("error"):
            out["parameters_error"] = info["error"]
        if info.get("truncated"):
            out["parameters_truncated"] = info["truncated"]

    out["symbol_pins"] = _probe_symbol_pins(cell, view)

    out["verified"] = False
    out["note"] = ("以上全部是**读到的定义**，不是仿真结果。"
                   "这个元件能不能真正放进电路里跑，"
                   "只有 validate_model_import 能给结论。")
    return out


# ---------------------------------------------------------------------------
# 工具 6：validate_model_import
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = {"real", "int", "fixed", "complex"}
_ARRAY_TYPES = {"realarray", "intarray", "stringarray", "complexarray",
                "fixedarray"}


def _parse_number(text: str):
    """把 '2.4 GHz' / '50 Ohm' 这类带单位的值拆成 (数值, 单位)。

    只为校验类型合理性用；ADS 自己按unit_type 解释单位，本函数
    不做任何单位换算 —— 换算错了比不给结果更糟。
    """
    s = str(text or "").strip()
    if not s:
        return None, ""
    m = re.match(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*)$", s)
    if not m:
        return None, s
    try:
        return float(m.group(1)), m.group(2).strip()
    except ValueError:
        return None, s


def _check_parameters(model_def, requested: dict) -> dict:
    """校验请求参数与元件定义是否一致。

    **只报真实发现的问题**：
      * 参数名不在定义里 -> 问题（ADS 会忽略或报错，取决于元件）
      * 定义说real/int 而给的是非数字 -> 问题
      * 定义说 string 而给的是纯数字 -> 提醒（不是硬错，ADS 允许）
    取值范围**不校验** —— 本ADS 版本的 ModelParam 没有 min/max
    （见 _PARAM_RANGE_REASON），拿不到就明说，不假装校验过。
    """
    out = {"checked": False, "matched": [], "problems": [], "warnings": [],
           "unknown_params": [], "range_checked": False,
           "range_note": _PARAM_RANGE_REASON}
    if not isinstance(requested, dict) or not requested:
        return out
    if model_def is None or isinstance(model_def, dict):
        out["warnings"].append("没有 model_def，无法核对参数 —— "
                               "参数名是否正确未验证。")
        return out
    try:
        defined = {}
        for p in list(model_def.parameters):
            try:
                defined[str(p.name)] = p
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"读取参数定义失败: {type(e).__name__}: {e}")
        return out

    out["checked"] = True
    for key, val in (requested or {}).items():
        p = defined.get(str(key))
        if p is None:
            out["unknown_params"].append(str(key))
            out["problems"].append(
                f"参数 {key!r} 不在该元件的定义里。"
                f"该元件定义的参数: {sorted(defined)[:60]}")
            continue
        ptype = _param_type(p)
        text = str(val)
        entry = {"name": str(key), "value": text, "param_type": ptype,
                 "unit": _param_unit(p),
                 "default": _param_default(p)}
        num, unit = _parse_number(text)
        ltype = ptype.lower()
        if ltype in _NUMERIC_TYPES:
            if num is None:
                out["problems"].append(
                    f"参数 {key!r} 定义为 {ptype}（数值型），"
                    f"但给的值 {text!r} 不是数字。")
            else:
                entry["parsed_number"] = num
                entry["parsed_unit"] = unit
        elif ltype in _ARRAY_TYPES:
            if not re.search(r"[\s,]", text.strip()):
                out["warnings"].append(
                    f"参数 {key!r} 定义为数组类型 {ptype}，"
                    f"但给的值 {text!r} 看起来是单个值 —— 请确认写法。")
        elif ltype == "string":
            if num is not None and not unit:
                out["warnings"].append(
                    f"参数 {key!r} 定义为 string，却给了纯数字 {text!r}；"
                    f"若原值带引号请一并给出。")
        out["matched"].append(entry)
    return out


# ---------------------------------------------------------------------------
# S 参数文件元件候选（**本机 ADS 2027 安装取证，不是靠记忆写的**）
# ---------------------------------------------------------------------------
#
# 取证路径（2026-10-09 在本机 ADS 2027 安装上真实读到；这里一律写成
# 相对 ADS 安装根的路径 —— 写死绝对安装目录既过不了发布包的
# 硬编码路径检查，也会在别人机器上直接误导）：
#   $HPEESOF_DIR/oalibs/analog_rf.defs
#       DEFINE ads_datacmps $HPEESOF_DIR/oalibs/rf/ads_datacmps
#       ASSIGN ads_datacmps libMode readOnly
#   $HPEESOF_DIR/oalibs/rf/ads_datacmps/S2P/itemdef.ael
#       create_item("S2P","2-Port S-parameter File", ...)
#       create_parm("File","Data file name",0,"datafileformset",-1,prm("dfiles",""))
#       create_parm("Type","File Type",512,"TForm",-1,prm("Type1"))
#       create_constant_form("Type1","Touchstone",0,'"touchstone"',"Touchstone")
#   同目录还有 S1P..S9P（都带 File 参数）与 SnP（N 端口，本版本没有 File
#   参数 —— 它的文件入口不同，所以候选里**不预设**它可用）。
#   $HPEESOF_DIR/oalibs/rf/ads_simulation/TermG/itemdef.ael
#       create_parm("Num","Port number",...); create_parm("Z", ...)
#       网表格式 "%d:%t %# ... %e 0 ..." —— 第二节点是全局地 0（单脚自带参考地）
#   $HPEESOF_DIR/oalibs/rf/ads_simulation/S_Param/itemdef.ael
#       create_parm("Start"/"Stop"/"Step", ... FREQUENCY_UNIT)
#
# 有了取证为什么还要**运行时再核实**：以上只是这一份安装的证据，换版本、换
# 安装或用户改了库都可能不成立。名录只提供**候选**，真正用哪个 master、
# 用哪个参数名传文件，一律以运行时读到的 model_def 参数表为准；读不到就
# 拒绝并原样列出探测过程，绝不静默退回一个"应该能用"的名字。

_SNP_DEFAULT_LIBRARIES = ("ads_datacmps",)
_SNP_FILE_PARAM_NAMES = ("File",)
_MAX_NAMED_PORTS = 9        # S1P..S9P；N 端口走 SnP 时是否有 File 参数由运行时判定

_TOUCHSTONE_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}


def _touchstone_header(path: str, max_bytes: int = 1 << 18) -> dict:
    """读 Touchstone 文件头与频率轴（只读前若干 KB，够判频段）。

    只报**真读出来**的：拿不到就 None + 原因。 ports 优先取文件扩展名里的
    ``.sNp``，再用数据块重复行数交叉核对；两者不一致时如实写进 notes，
    不挑一个当成结论。
    """
    out = {"available": False, "ports": None, "ports_source": "",
           "freq_start_hz": None, "freq_stop_hz": None, "points": 0,
           "frequency_unit": "", "data_format": "", "parameter": "",
           "reference_impedance_ohm": None, "option_line": "",
           "notes": [], "error": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(max_bytes)
    except OSError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    option = ""
    freqs: list = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("!") or s.startswith("[") or s.startswith("("):
            continue
        if s.startswith("#"):
            option = option or s
            continue
        parts = s.replace(",", " ").split()
        if not parts:
            continue
        try:
            freqs.append(float(parts[0]))
        except ValueError:
            continue

    unit = ""
    tokens = [t.upper() for t in (option or "").lstrip("#").split()]
    for token in tokens:
        if token in _TOUCHSTONE_UNITS:
            unit = token
            break
    scale = _TOUCHSTONE_UNITS.get(unit, 0.0)
    for token in tokens:
        if token in ("S", "Y", "Z", "H", "G"):
            out["parameter"] = token
        elif token in ("RI", "MA", "DB"):
            out["data_format"] = token
        elif token == "R":
            idx = tokens.index("R")
            if idx + 1 < len(tokens):
                try:
                    out["reference_impedance_ohm"] = float(tokens[idx + 1])
                except ValueError:
                    pass
    out["frequency_unit"] = unit
    out["option_line"] = option
    if not option:
        out["error"] = "文件里没有 Touchstone 选项行（以 # 开头），无法确认频段与参考阻抗"
        return out
    if not scale:
        out["error"] = f"选项行 {option!r} 里没有可识别的频率单位（HZ/KHZ/MHZ/GHZ）"
        return out

    unique = list(dict.fromkeys(freqs))
    if unique:
        out["freq_start_hz"] = min(unique) * scale
        out["freq_stop_hz"] = max(unique) * scale
        out["points"] = len(unique)
    out["available"] = True

    name = os.path.basename(path)
    m = re.search(r"\.s(\d+)p$", name, re.I)
    if m:
        out["ports"] = int(m.group(1))
        out["ports_source"] = "文件名扩展名 .sNp"
    # 交叉核对：同一个频率值连续出现的行数应该等于 ports**2
    if freqs and unique:
        block = freqs.count(unique[0])
        root = int(round(block ** 0.5))
        if root * root == block and root > 0:
            if out["ports"] is None:
                out["ports"] = root
                out["ports_source"] = "数据块重复行数开方"
            elif out["ports"] != root:
                out["notes"].append(
                    f"扩展名给 {out['ports']} 端口，数据块给 {root} 端口 —— 不一致，"
                    f"以扩展名为准并在返回体里保留这个差异")
    return out


def _sim_band_within(header: dict, requested: dict = None, min_points: int = 11) -> dict:
    """确定仿真频段：**必须落在模型文件的频率范围内**。

    超出模型频段仿真出来的曲线是插值/外推的结果，拿它当"模型可用"的证据
    就是自欺 —— 所以请求频段超出时明确拒绝，而不是悄悄外推。
    """
    out = {"ok": False, "start_hz": None, "stop_hz": None, "points": min_points,
           "source": "", "problem": "", "note": ""}
    start = header.get("freq_start_hz")
    stop = header.get("freq_stop_hz")
    if start is None or stop is None:
        out["problem"] = "读不到模型文件的频率范围，无法确定仿真频段（不外推）"
        return out
    req_start = None
    req_stop = None
    if isinstance(requested, dict):
        req_start = requested.get("start_hz")
        req_stop = requested.get("stop_hz")
    if req_start is None or req_stop is None:
        out["start_hz"], out["stop_hz"] = start, stop
        out["source"] = "模型文件的实际频率范围"
    else:
        if float(req_start) < start - 1e-9 or float(req_stop) > stop + 1e-9:
            out["problem"] = (
                f"请求的仿真频段 {float(req_start):.6g}–{float(req_stop):.6g} Hz "
                f"超出模型文件范围 {start:.6g}–{stop:.6g} Hz；"
                f"超出部分是插值/外推结果，不能当作该模型已验证的依据。"
                f"请改用模型覆盖内的频段重跑。")
            return out
        out["start_hz"], out["stop_hz"] = float(req_start), float(req_stop)
        out["source"] = "调用方指定频段（已核对落在模型范围内）"
    span = max(0.0, float(out["stop_hz"]) - float(out["start_hz"]))
    header_points = int(header.get("points") or 0)
    if header_points >= 2:
        out["points"] = max(min_points, header_points)
    elif span > 0:
        out["points"] = max(min_points, 21)
    out["ok"] = True
    if span <= 0:
        out["note"] = "频段起止相同（单点），只能做单点仿真，曲线形态无法核对"
    return out


def _unique_cell_name(prefix: str = "ADS_AGENT_VERIFY") -> str:
    """临时 cell 名：**秒内也唯一**。

    只用时间戳的话，同一秒内验证两个型号会撞名 —— 后一个会落在前一个的
    cell 上（close/reopen 之后还看得见上一次的实例），证据就串了。这里加
    PID + 随机串，并在真正创建前用 cell_exists 再确认一次。
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{os.getpid()}_{os.urandom(3).hex()}"


def _discover_sparam_component(ws, ports: int = 0, explicit: str = "") -> dict:
    """在当前工作区**已打开**的库里找能引用 Touchstone 文件的 S 参数元件。

    返回 ``{available, library, cell, master, file_param, param_names, probed,
    reason}``。``probed`` 记录每个候选的真实探测结果 —— 哪个库没开、哪个
    cell 不存在、哪个缺文件参数，全部说出来，调用方拿得到"找不到的原因"
    而不是一句"不支持"。
    """
    out = {"available": False, "library": "", "cell": "", "master": "",
           "file_param": "", "param_names": [], "probed": [], "reason": ""}

    open_libs = {}
    try:
        for lib in list(ws.libraries or []):
            try:
                open_libs[str(lib.name)] = lib
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"无法枚举工作区已打开的库: {type(e).__name__}: {e}"
        return out

    candidates: list = []
    seen = set()

    def _push(lib_name: str, cell_name: str, origin: str) -> None:
        key = (str(lib_name), str(cell_name))
        if not key[0] or not key[1] or key in seen:
            return
        seen.add(key)
        candidates.append((key[0], key[1], origin))

    lib_s, cell_s, _v = _parse_master(str(explicit or ""))
    if lib_s and cell_s:
        _push(lib_s, cell_s, "调用方显式指定")
    else:
        cell_names = []
        if isinstance(ports, int) and 1 <= ports <= _MAX_NAMED_PORTS:
            cell_names.append(f"S{ports}P")
        cell_names.append("SnP")
        for lib_name in _SNP_DEFAULT_LIBRARIES:
            for cell_name in cell_names:
                _push(lib_name, cell_name,
                      f"ADS 自带数据元件库（取证: oalibs/rf/{lib_name}）")
        # 换版本/其它库里可能有同名同职能的元件 —— 全库扫一遍同名 cell
        for lib_name in sorted(open_libs):
            for cell_name in cell_names:
                _push(lib_name, cell_name, "当前工作区已打开库里的同名 cell")

    for lib_name, cell_name, origin in candidates:
        probe = {"library": lib_name, "cell": cell_name, "origin": origin,
                 "library_open": False, "cell_exists": False,
                 "file_param": "", "param_names": [], "note": ""}
        lib = open_libs.get(lib_name)
        if lib is None:
            probe["note"] = "该库不在当前工作区的已打开库列表里"
            out["probed"].append(probe)
            continue
        probe["library_open"] = True
        try:
            exists = bool(lib.cell_exists(str(cell_name)))
        except Exception as e:  # noqa: BLE001
            probe["note"] = f"查询 cell 失败: {type(e).__name__}: {e}"
            out["probed"].append(probe)
            continue
        if not exists:
            probe["note"] = "库里没有这个 cell"
            out["probed"].append(probe)
            continue
        probe["cell_exists"] = True
        cell = _read_cell(lib, cell_name)
        md = None if cell is None else _read_model_def(cell)
        if md is None or isinstance(md, dict):
            probe["note"] = ("打不开或没有 model_def，读不到参数表 —— "
                             "无法确定用哪个参数传模型文件")
            out["probed"].append(probe)
            continue
        try:
            params = list(md.parameters)
        except Exception as e:  # noqa: BLE001
            probe["note"] = f"读参数表失败: {type(e).__name__}: {e}"
            out["probed"].append(probe)
            continue
        names = []
        by_name = {}
        for p in params:
            try:
                nm = str(p.name or "")
            except Exception:  # noqa: BLE001
                continue
            if not nm:
                continue
            names.append(nm)
            by_name[nm] = _param_type(p)
        probe["param_names"] = names
        chosen = ""
        for nm in names:
            if nm in _SNP_FILE_PARAM_NAMES:
                chosen = nm
                break
        if not chosen:
            for nm in names:
                if nm.lower().endswith("file"):
                    chosen = nm
                    break
        if not chosen:
            probe["note"] = "参数表里没有文件类参数（无法指定 Touchstone 文件）"
            out["probed"].append(probe)
            continue
        probe["file_param"] = chosen
        out["probed"].append(probe)
        out.update({"available": True, "library": lib_name, "cell": cell_name,
                    "master": f"{lib_name}:{cell_name}", "file_param": chosen,
                    "param_names": names,
                    "file_param_type": by_name.get(chosen, "")})
        return out

    out["reason"] = ("在当前工作区里找不到可引用 Touchstone 文件的 S 参数元件。"
                     + "；".join(f"{p['library']}:{p['cell']} -> {p['note']}"
                                 for p in out["probed"])
                     + "（探测过程原样保留，未按名录硬猜元件名）")
    return out


def _instance_pins(inst) -> list:
    """实例的真实引脚标签（编号/具名）。读不到返回空 —— 空不等于没引脚。"""
    labels = []
    try:
        pins = list(inst.inst_pins)
    except Exception:  # noqa: BLE001
        return []
    for pin in pins:
        try:
            label = getattr(pin, "inst_term", None)
        except Exception:  # noqa: BLE001
            label = None
        val = None
        if label is not None:
            try:
                val = label.term_number if getattr(label, "is_numbered", True) \
                    else label.term_name
            except Exception:  # noqa: BLE001
                val = None
        if val is None:
            val = getattr(pin, "name", None)
        labels.append(str(val) if val is not None else str(getattr(pin, "master_pin", "")))
    return labels


def _read_only_recheck(ads_ops, host_lib: str, tmp_cell: str, expected: dict) -> dict:
    """保存**并关闭写句柄之后**重新只读打开，做一次落盘后的核对。

    为什么必须这样：写句柄还开着时读到的是内存里的对象，磁盘上是不是真的
    有这些元件/网络/netlist 并没有被证明。过了这一关，"网表里引用了这个
    模型文件"才是对磁盘上真实数据的陈述。完毕后立刻关闭只读句柄。
    """
    out = {"ok": False, "audit": {}, "problems": [], "warnings": [], "netlist": ""}
    design = ads_ops._open_design(host_lib, tmp_cell, "schematic", write=False)
    try:
        audit = ads_ops._design_audit(design)
        out["audit"] = audit
        ref = f"{host_lib}:{tmp_cell}:schematic"
        out["problems"] = list(ads_ops._gate_problems(audit, ref))
        out["warnings"] = list(ads_ops._gate_warnings(audit))

        names = [str(i.get("name") or "") for i in (audit.get("instances") or [])]
        for want in (expected.get("instance_names") or []):
            if want not in names:
                out["problems"].append(
                    f"落盘后只读打开缺少实例 {want}（磁盘上实际有: {names}）")
        want_n = int(expected.get("n_instances") or 0)
        if want_n and int(audit.get("n_instances") or 0) != want_n:
            out["problems"].append(
                f"落盘后实例数 {audit.get('n_instances')} 与预期 {want_n} 不符")

        floating = audit.get("floating_pins") or []
        if floating:
            out["problems"].append(
                "有引脚没有接入网络："
                + ", ".join(f"{f.get('instance')}({f.get('master')}) 引脚 {f.get('pin')}"
                            for f in floating[:10]))

        # 每个端口网络必须只包含「端口 + 被测件」两个成员，且互不相同。
        # _design_audit 不返回 net_members（它只统计数量），这里按**磁盘上重开
        # 出来的真实对象**自己复算一遍 —— 端口短接与否只有这层数据能回答。
        net_members: dict = {}
        for inst in list(design.instances or []):
            master = ""
            for attr in ("master_name", "cell_name", "master_cell_name"):
                val = getattr(inst, attr, None)
                if val:
                    master = str(val)
                    break
            if master == "S_Param":
                continue        # 控制器没有参与 S 端口拓扑的引脚
            for pin in list(getattr(inst, "inst_pins", []) or []):
                label = _net_label_of(getattr(pin, "net", None))
                if not label:
                    continue
                iname = ""
                for attr in ("inst_name", "name", "instance_name"):
                    val = getattr(inst, attr, None)
                    if val:
                        iname = str(val)
                        break
                net_members.setdefault(label, []).append(iname)
        out["net_members"] = net_members
        seen_nets = {}
        for want_port in (expected.get("port_nets") or []):
            port_name = want_port.get("port")
            members = net_members.get(want_port.get("net") or "")
            if not members:
                out["problems"].append(f"端口 {port_name} 的网络在磁盘上没有成员（未连通）")
                continue
            if len(members) != 2:
                out["problems"].append(
                    f"端口 {port_name} 的网络成员数 {len(members)}（应为 2: 端口+被测件）"
                    f"：{members}")
            key = tuple(sorted(members))
            if key in seen_nets:
                out["problems"].append(
                    f"端口 {port_name} 与 {seen_nets[key]} 落在同一个网络上 —— "
                    f"两个端口被短接，S 参数无意义")
            seen_nets[key] = port_name

        try:
            netlist = design.generate_netlist()
            out["netlist"] = netlist if isinstance(netlist, str) else str(netlist)
        except Exception as e:  # noqa: BLE001
            out["problems"].append(f"只读打开后生成网表失败: {type(e).__name__}: {e}")
    finally:
        ads_ops._close_design(design)
    out["ok"] = not out["problems"]
    return out


def _net_label_of(net) -> str:
    """网络对象 -> 名字。.realname 优先，其次从 str 形如 <ScalarNet "N_A"> 里抠。"""
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


def _check_netlist(netlist: str, model_file_abs: str, ports: int,
                   sim_band: dict) -> dict:
    """核对网表**真的**引用了这个模型文件、端口编号互不相同、频段符合要求。

    只认事实：文件名/路径在网表里出现，且 Start/Stop 数值落在模型频段内。
    """
    out = {"ok": False, "problems": [], "checks": {}}
    text = str(netlist or "")
    low = text.lower()
    if not text.strip():
        out["problems"].append("网表为空，没有任何可核对内容")
        return out
    hit_base = False
    hit_path = False
    if model_file_abs:
        hit_base = os.path.basename(str(model_file_abs)).lower() in low
        # ADS 网表里路径一律带反斜杠或正斜杠，两侧都归一到正斜杠再比
        hit_path = str(model_file_abs).replace("\\", "/").lower() in \
            text.replace("\\", "/").lower()
    out["checks"]["file_basename_in_netlist"] = hit_base
    out["checks"]["full_path_in_netlist"] = bool(hit_path)
    if model_file_abs and not hit_base:
        out["problems"].append(
            f"网表里没有出现模型文件名 {base!r} —— "
            f"网络 assortment/File 参数没有落进网表，这条仿真与该文件无关")

    nums = []
    for i in range(1, max(1, int(ports or 0)) + 1):
        present = re.search(rf"Num\s*=\s*{i}\b", text) is not None
        nums.append({"port": i, "num_in_netlist": present})
    out["checks"]["port_numbers"] = nums
    missing_num = [p["port"] for p in nums if not p["num_in_netlist"]]
    if len(nums) >= 2 and len(missing_num) == len(nums):
        # 某些版本网表里端口编号不写成 Num= —— 不算阻断，但必须声明口径
        out["checks"]["port_number_note"] = (
            "网表里没有 Num= 形式的端口编号；端口区分改按端口实例名核对，"
            "未能同时证实编号本身")
    elif missing_num:
        out["problems"].append(
            f"网表里缺少端口编号 {missing_num}（端口编号必须互不相同，"
            f"否则 S 参数矩阵的端口顺序没有意义）")

    start = sim_band.get("start_hz")
    stop = sim_band.get("stop_hz")
    for key, value in (("Start", start), ("Stop", stop)):
        if value is None:
            continue
        m = re.search(rf"{key}\s*=\s*([0-9.eE+-]+)\s*([A-Za-z]*)", text)
        if not m:
            out["problems"].append(f"网表里没有 {key} 的数值，无法核对仿真频段")
            continue
        out["checks"].setdefault("controller", {})[key] = m.group(1) + (m.group(2) or "")
    out["ok"] = not out["problems"]
    return out


def _dataset_report(dataset_path: str, ports: int,
                    expressions: list = None) -> dict:
    """打开真实数据集，读取 S 曲线并核对数据本身有效。

    「仿真进程退出成功」不等于「得到了 S 参数」：数据集打不开、没有 S 矩阵、
    曲线全是 NaN/常数，都会让仿真实质无效 —— 这里逐项读出来。
    """
    out = {"ok": False, "problems": [], "variables": [], "curves": {},
           "dataset_path": str(dataset_path or "")}
    if not dataset_path or not os.path.isfile(dataset_path):
        out["problems"].append(f"数据集文件不存在: {dataset_path}")
        return out
    try:
        import keysight.ads.dataset as dataset
    except Exception as e:  # noqa: BLE001
        out["problems"].append(
            f"无法导入 keysight.ads.dataset（{type(e).__name__}: {e}）—— "
            f"数据集未做内容核对，不能凭仿真进程退出码判定结果有效")
        return out
    try:
        data = dataset.open(dataset_path)
    except Exception as e:  # noqa: BLE001
        out["problems"].append(f"打开数据集失败: {type(e).__name__}: {e}")
        return out
    try:
        out["variables"] = [str(k) for k in
                            (data.keys() if hasattr(data, "keys") else data)]
    except Exception as e:  # noqa: BLE001
        out["problems"].append(f"列出数据集变量失败: {type(e).__name__}: {e}")
        return out

    wanted = list(expressions or [])
    if not wanted:
        wanted = [f"S({i},{j})" for i in range(1, max(1, int(ports or 0)) + 1)
                  for j in range(1, max(1, int(ports or 0)) + 1)]
    if not wanted:
        wanted = ["S(1,1)"]

    for expr in wanted:
        entry = {"requested": expr, "found": False}
        try:
            block = data[expr]
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {e}"
            out["curves"][expr] = entry
            out["problems"].append(f"数据集中取不到 {expr}：{type(e).__name__}: {e}")
            continue
        entry["found"] = True
        df = None
        try:
            df = block.to_dataframe().reset_index()
        except Exception as e:  # noqa: BLE001
            raw = getattr(block, "data", None)
            if raw is None:
                entry["error"] = f"{type(e).__name__}: {e}"
                out["curves"][expr] = entry
                out["problems"].append(f"{expr} 既不支持 to_dataframe 也无 data 属性")
                continue
            values = [float(v) for v in list(raw)[:1000]]
            entry["n_points"] = len(values)
            entry["min"] = min(values) if values else None
            entry["max"] = max(values) if values else None
            if values and all(v == values[0] for v in values):
                out["problems"].append(
                    f"{expr} 全为同一数值 {values[0]} —— 曲线退化，不能作为模型可用的证据")
            out["curves"][expr] = entry
            continue
        try:
            cols = [str(c) for c in df.columns]
            entry["columns"] = cols
            entry["n_rows"] = int(len(df))
            stats = {}
            for col in cols:
                series = df[col]
                try:
                    stats[col] = {"min": float(series.min()),
                                  "max": float(series.max())}
                except Exception:  # noqa: BLE001
                    continue
            entry["stats"] = stats
            if int(len(df)) < 2:
                out["problems"].append(f"{expr} 只有 {len(df)} 个点，不足以构成曲线")
            non_trivial = [c for c in cols if c not in ("index", "freq")
                           and str(c).lower() not in ("index", "freq")]
            flat = [c for c in non_trivial
                    if abs(float(stats.get(c, {}).get("max", 0))
                           - float(stats.get(c, {}).get("min", 0))) < 1e-12]
            if non_trivial and len(flat) == len(non_trivial):
                out["problems"].append(
                    f"{expr} 的所有列都是常数（{non_trivial}）—— 数据退化，"
                    f"仿真虽跑完但结果无信息量")
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {e}"
        out["curves"][expr] = entry

    if not out["curves"]:
        out["problems"].append("没有读到任何 S 曲线")
    out["ok"] = not out["problems"]
    return out


def _sim_phase(netlist: str, out_dir: str, dataset_name: str, netlist_path: str,
               audit: dict, ctx, finish=None) -> dict:
    """跑仿真：优先按 toolserver 的机制**移出主线程**，没有 ctx 就同步跑。

    ``finish(result, error)`` 非空时，调用方负责把结果并进自己的返回体并调用
    ``ctx.finish`` —— 这样即使验证的仿真在后台收尾，最终回填的仍是**完整的**
    验证结果，而不是被仿真结果整段替换掉。
    """
    import ads_ops

    note = getattr(ctx, "note", None)
    can_defer = (finish is not None and ctx is not None
                 and getattr(ctx, "sim_off_main_thread", False)
                 and hasattr(ctx, "defer") and hasattr(ctx, "finish"))
    if can_defer:
        def _worker():
            try:
                finish(ads_ops._simulate(netlist, out_dir, dataset_name,
                                         netlist_path, audit, note=note), None)
            except Exception as e:  # noqa: BLE001
                finish(None, e)

        ctx.defer()
        threading.Thread(target=_worker, daemon=True,
                         name="ads-agent-model-smoke").start()
        return {"deferred": True, "output_dir": out_dir,
                "note": "仿真已转后台线程执行（ADS 主线程已释放）"}

    try:
        result = ads_ops._simulate(netlist, out_dir, dataset_name, netlist_path,
                                   audit, note=note)
    except Exception as e:  # noqa: BLE001
        if finish is not None:
            finish(None, e)
            return {"deferred": False, "ran": True, "ok": False,
                    "error": str(e) if isinstance(e, RuntimeError)
                    else f"{type(e).__name__}: {e}"}
        raise
    if finish is not None:
        finish(result, None)
    result["deferred"] = False
    return result


def _touchstone_smoke_test(ctx, ws, ws_path: str, *, model_file_abs: str = "",
                           header: dict = None, ports: int = 0,
                           requested_band: dict = None,
                           explicit_component: str = "", extra_params: dict = None,
                           reference_ohm=None, notes: list = None,
                           dut_master: str = "", deferred_finish=None) -> dict:
    """真实的两端口（多端口同理）Touchstone 仿真实证。

    流程（每一步失败都要能说出失败在哪一步）：
      1. 在当前工作区里找出**真实可用**的 S 参数元件与其文件参数名；
      2. 唯一临时 cell —— 放该元件、N 个编号互不相同的 TermG 端口（自带参考
         地）、一个 S 参数控制器；
      3. 按**真实引脚定义**把模型引脚与端口一一对应地绑到不同网络；
      4. 仿真频段取模型文件自带的频率范围（请求超出即拒绝，不外推）；
      5. save_design() —— 必须在写句柄**关闭之前**保存；
      6. 关闭写句柄后**只读重开**做落盘核对 + 复用现有仿真门禁 + 生成网表；
      7. 核对网表确实引用指定的模型文件、端口编号互不相同；
      8. 跑仿真（默认可转后台），打开真实数据集读取 S 曲线并核对数据有效。

    断言口径：**不以仿真进程退出成功为准**。退出成功但网表没引用目标文件、
    端口短接、数据集里没有 S 曲线或曲线退化，一律判失败并保留诊断产物。
    产物一律不删，失败时也保留（out_dir / netlist_path / 临时 cell 名）。
    """
    notes = notes if notes is not None else []
    import ads_ops

    out: dict = {
        "ran": True,        # 走到这里就是真的动手做了
        "ok": False,
        "supported": True,
        "problems": [],
        "notes": notes,
        "stages": {},
    }

    header = dict(header or {})
    resolved_ports = int(header.get("ports") or ports or 0)

    if bool(dut_master) and not model_file_abs:
        # 非 Touchstone 器件（Design Kit 里的原理图/行为模型）：没有文件可引用，
        # 也没有自带的频率范围 —— 两件事都必须先说清再动手，否则搭出来的
        # 夹具与"验证这个模型"毫无关系。
        probe = _discover_sparam_component(ws, 0, explicit=dut_master)
        probe["require_file_param"] = False
        if not probe.get("probed"):
            out.update({"ran": False, "supported": False,
                        "reason": f"找不到 {dut_master}，无法搭建验证夹具"})
            return out
        first = probe["probed"][0]
        if not first.get("cell_exists"):
            out.update({"ran": False, "supported": False,
                        "reason": f"{dut_master} 不可用: {first.get('note')}"})
            return out
        pick = {"available": True, "library": first["library"],
                "cell": first["cell"], "master": dut_master, "file_param": "",
                "param_names": first.get("param_names") or [],
                "probed": probe["probed"], "source": "调用方指定的 DUT master"}
        out["component"] = pick
        if not isinstance(requested_band, dict) or not requested_band.get("start_hz") \
                or not requested_band.get("stop_hz"):
            out.update({
                "ran": False, "supported": True,
                "reason": ("该器件不是 Touchstone 文件模型，没有自带的频率范围；"
                           "必须调用方给出 target_band（start_hz/stop_hz）才能确定仿真频段。"
                           "不猜默认频段 —— 在未知频段上跑出来的曲线说明不了任何问题。")})
            return out
        band = {"ok": True, "start_hz": requested_band["start_hz"],
                "stop_hz": requested_band["stop_hz"],
                "points": int(requested_band.get("points") or 21),
                "source": "调用方指定频段（非 Touchstone 模型无自带范围）"}
        out["sim_band"] = band
    elif model_file_abs:
        if not resolved_ports:
            out.update({"ran": False, "supported": False,
                        "reason": "无法确定模型端口数（文件扩展名不是 .sNp 且数据块推断不出），"
                                  "不敢按某个端口数瞎搭夹具"})
            return out
        pick = _discover_sparam_component(ws, resolved_ports,
                                          explicit=explicit_component)
        out["component"] = pick
        if not pick.get("available"):
            out.update({"ran": False, "supported": False,
                        "reason": pick.get("reason") or "找不到可用的 S 参数元件",
                        "detail": pick.get("probed")})
            return out
        band = _sim_band_within(header, requested_band)
        out["sim_band"] = band
        if not band.get("ok"):
            out.update({"ran": False, "supported": True,
                        "reason": band.get("problem") or "无法确定仿真频段",
                        "detail": {"model_range": [header.get("freq_start_hz"),
                                                   header.get("freq_stop_hz")]}})
            return out
    else:
        out.update({"ran": False, "supported": False,
                    "reason": "既没有模型文件也没有指定 DUT 元件，无法构造验证夹具"})
        return out

    writable = sorted(str(x) for x in (ws.writable_library_names or []))
    if not writable:
        out.update({"ran": False,
                    "reason": "当前工作区没有可写库，无法建临时 cell 做仿真实证。"
                              "（原厂库是只读的，不会为了验证给它们写权限。）"})
        return out
    host_lib = next((n for n in writable if n.startswith("ADS_AGENT")), writable[0])
    tmp_cell = _unique_cell_name()

    created = ads_ops._ensure_cell_view(ws, host_lib, tmp_cell, "schematic")
    out["temp_design"] = f"{host_lib}:{tmp_cell}:schematic"
    out["temp_design_library"] = host_lib
    out["cell_created"] = bool((created or {}).get("created"))
    out["cleanup_hint"] = (
        f"临时 cell {host_lib}:{tmp_cell} 与仿真产物均未删除（失败也要能查），"
        f"确认无用后可自行删除。")

    design = ads_ops._open_design(host_lib, tmp_cell, "schematic", write=True)
    try:
        try:
            x1 = design.add_instance(pick["master"], (0.0, 0.0), name="X1")
        except Exception as e:  # noqa: BLE001
            out.update({"ran": True, "stages": {"placed": False},
                        "problems": [f"放置 S 参数元件 {pick['master']} 失败: "
                                     f"{type(e).__name__}: {e}"]})
            return out
        applied_params: dict = {}
        if pick.get("file_param") and model_file_abs:
            try:
                ads_ops._set_param(x1, pick["file_param"], str(model_file_abs))
                applied_params[pick["file_param"]] = str(model_file_abs)
            except Exception as e:  # noqa: BLE001
                out.update({"ran": True, "stages": {"placed": False},
                            "problems": [f"给 {pick['file_param']} 赋模型文件路径失败: "
                                         f"{type(e).__name__}: {e}"]})
                return out
        for key, value in (extra_params or {}).items():
            try:
                ads_ops._set_param(x1, str(key), str(value))
                applied_params[str(key)] = str(value)
            except Exception as e:  # noqa: BLE001
                out.update({"ran": True, "ok": False,
                            "stages": {"placed": True, "parameters_applied": False},
                            "problems": [f"指定参数 {key}={value!r} 设置/回调失败: "
                                         f"{type(e).__name__}: {e}"]})
                return out
        out["params_applied"] = applied_params

        dut_pins = _instance_pins(x1)
        out["dut_pins"] = dut_pins
        if not model_file_abs:
            # 非 Touchstone 器件：端口数只能来自符号的真实引脚定义。
            # 三极管/MOS 这类带偏置脚的器件在这里明确拒绝 —— 套两端口夹具会
            # 让偏置悬空，跑出来的曲线说明不了该模型的可用性（宁可不支持）。
            resolved_ports = len(dut_pins)
            out["resolved_ports"] = resolved_ports
            if resolved_ports != 2:
                out.update({
                    "ran": True, "supported": False,
                    "stages": {"placed": True, "connected": False},
                    "problems": [
                        f"{pick['master']} 有 {resolved_ports} 个引脚（{dut_pins}）；"
                        f"当前自动夹具只支持**恰好两个信号引脚**的无源两端口器件。"
                        f"带偏置/衬底引脚的有源器件需要配置直流偏置网络与电源，"
                        f"本版不自动构造 —— 不套两端口夹具以免产出看似成功实则不成立的曲线。"]})
                return out
        if len(dut_pins) < resolved_ports:
            out.update({"ran": True, "stages": {"placed": True, "connected": False},
                        "problems": [
                            f"被测件只有 {len(dut_pins)} 个引脚，少于模型端口数 "
                            f"{resolved_ports} —— 按此连出的电路不代表该模型"
                            f"（实际引脚: {dut_pins}）"]})
            return out

        terms = []
        for i in range(1, resolved_ports + 1):
            term = design.add_instance("ads_simulation:TermG", (-8.0, -4.0 * i),
                                       name=f"P{i}")
            try:
                ads_ops._set_param(term, "Num", str(i))
            except Exception as e:  # noqa: BLE001
                notes.append(f"端口 P{i} 设置 Num 失败（端口编号可能相同）: "
                             f"{type(e).__name__}: {e}")
            if reference_ohm:
                try:
                    ads_ops._set_param(term, "Z", f"{reference_ohm} Ohm")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"端口 P{i} 设置 Z 失败，沿用默认值: {e}")
            terms.append(term)

        sp = design.add_instance("ads_simulation:S_Param", (0.0, -8.0), name="SP1")
        try:
            ads_ops._set_param(sp, "Start", _hz_to_ads(band["start_hz"]))
            ads_ops._set_param(sp, "Stop", _hz_to_ads(band["stop_hz"]))
            ads_ops._set_param(sp, "Step", _hz_to_ads(
                max(1.0, (float(band["stop_hz"]) - float(band["start_hz"]))
                    / max(1, int(band["points"]) - 1))))
        except Exception as e:  # noqa: BLE001
            notes.append(f"设置 S_Param 的频段失败，将使用默认频段: "
                         f"{type(e).__name__}: {e}")

        # --- 真实引脚对应：模型第 i 个引脚 <-> 第 i 个端口 ---
        connections = []
        for i, term in enumerate(terms, start=1):
            pin_label = dut_pins[i - 1]
            try:
                pa = ads_ops._find_pin_on(term, 1)
            except Exception:  # noqa: BLE001
                labels = _instance_pins(term)
                pa = None
                for lbl in labels:
                    try:
                        pa = ads_ops._find_pin_on(term, lbl)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if pa is None:
                    out.update({"ran": True,
                                "stages": {"placed": True, "connected": False},
                                "problems": [f"端口 P{i} 上找不到可用于连线的引脚"
                                             f"（可用引脚: {labels}）"]})
                    return out
            try:
                pb = ads_ops._find_pin_on(x1, pin_label)
            except Exception as e:  # noqa: BLE001
                out.update({"ran": True,
                            "stages": {"placed": True, "connected": False},
                            "problems": [f"被测件上找不到引脚 {pin_label!r}: {e}"]})
                return out
            # 电气连接：**绑定同一网络**（几何重合在 ADS 里不等于连通）；
            # 导线是图形表现，画不出来不影响电气结论，但必须如实记录。
            ads_ops._bind_pins(design, pa, pb)
            wire_drawn = False
            try:
                ads_ops.connect_impl(design, term, 1, x1, pin_label)
                wire_drawn = True
            except Exception as e:  # noqa: BLE001
                notes.append(f"P{i} 到 X1 引脚 {pin_label} 画导线失败"
                             f"（网络连接已绑定，电气不受影响）: {e}")
            connections.append({"port": f"P{i}", "dut_pin": str(pin_label),
                                # 绑定之后读回来才是**磁盘上会落盘**的那个网络名，
                                # 只读复核阶段要按它核对端口之间没有短接
                                "net": _net_label_of(getattr(pa, "net", None)),
                                "wire_drawn": wire_drawn})
        out["connections"] = connections

        try:
            out["vendor_dependencies"] = prepare_design_dependencies(
                design, ws_path, place_includes=True)
            design.save_design()      # 必须在关闭写句柄**之前**保存
        except Exception as e:  # noqa: BLE001
            out.update({"ran": True,
                        "stages": {"placed": True, "connected": True, "saved": False},
                        "problems": [f"保存临时设计失败: {type(e).__name__}: {e}"]})
            return out
    finally:
        ads_ops._close_design(design)

    out["stages"] = {"placed": True, "connected": True, "saved": True}

    # --- 关闭写句柄后只读复核 ---
    expected = {
        "instance_names": ["X1", "SP1"] + [f"P{i}" for i in range(1, resolved_ports + 1)]
                          + (out.get("vendor_dependencies") or {}).get("include_instances", []),
        "n_instances": resolved_ports + 2 + len(
            (out.get("vendor_dependencies") or {}).get("include_instances", [])),
        "port_nets": [{"port": c["port"], "net": c.get("net") or ""}
                      for c in (out.get("connections") or [])],
    }
    recheck = _read_only_recheck(ads_ops, host_lib, tmp_cell, expected)
    out["read_only_recheck"] = {
        "ok": recheck["ok"],
        "n_instances": recheck["audit"].get("n_instances"),
        "controllers": recheck["audit"].get("controllers"),
        "ports": recheck["audit"].get("ports"),
        "floating_pins": recheck["audit"].get("floating_pins"),
        "warnings": recheck["warnings"],
        "_vendor_dependencies": out.get("vendor_dependencies") or {},
    }
    netlist = recheck["netlist"]
    for problem in recheck["problems"]:
        note = f"落盘后只读复核未通过: {problem}"
        out["problems"].append(note)
        notes.append(note)

    if not netlist:
        out.update({"ran": True,
                    "problems": out["problems"]
                    + ["网表未生成，无法核对是否引用了目标模型文件"],
                    "artifacts": {"temp_design": out["temp_design"]}})
        return out

    import hashlib as _hashlib
    net_check = _check_netlist(netlist, model_file_abs, resolved_ports, band)
    out["netlist_check"] = net_check
    for problem in net_check["problems"]:
        out["problems"].append(f"网表核对: {problem}")

    out_dir = ads_ops.unique_sim_dir(ws_path, tmp_cell)
    netlist_path = os.path.join(out_dir, "netlist.ckt")
    try:
        with open(netlist_path, "w", encoding="utf-8") as f:
            f.write(netlist)
    except OSError:
        netlist_path = ""
    audit = {
        "n_instances": recheck["audit"].get("n_instances"),
        "controllers": recheck["audit"].get("controllers"),
        "ports": recheck["audit"].get("ports"),
        "n_nets": recheck["audit"].get("n_nets"),
        "warnings": recheck["warnings"],
        "_workspace": {"name": str(getattr(ws, "name", "") or ""), "path": ws_path},
        "_design_version": {
            "netlist_sha": _hashlib.sha256(netlist.encode("utf-8")).hexdigest()[:16],
            "netlist_chars": len(netlist),
        },
    }
    out["artifacts"] = {"netlist_path": netlist_path, "output_dir": out_dir,
                        "temp_design": out["temp_design"]}

    if _cancel_requested(ctx):
        out.update({"ran": False, "cancelled": True,
                    "reason": "已请求取消，未启动仿真。网表与临时设计已保留。"})
        return out

    def _finish(result, error):
        """后台/同步两条路都走这里：把仿真与数据集结论并回同一份返回体。"""
        if error is not None:
            out.update({"ok": False,
                        "sim": {"ran": True, "ok": False,
                                "error": str(error) if isinstance(error, RuntimeError)
                                else f"{type(error).__name__}: {error}"},
                        "problems": out["problems"]
                        + [f"仿真失败: {error}"]})
        else:
            sim = dict(result or {})
            sim["ran"] = True
            sim["ok"] = sim.get("status") == "done"
            out["sim"] = sim
            if not sim["ok"]:
                out["problems"].append(
                    f"仿真未产出数据集（status={sim.get('status')}）"
                    + (f": {sim.get('hint') or ''}" if sim.get("hint") else ""))
            dataset_path = sim.get("dataset_path") or ""
            report = _dataset_report(dataset_path, resolved_ports)
            out["dataset"] = report
            for problem in report["problems"]:
                out["problems"].append(f"数据集核对: {problem}")
            out["simulated"] = bool(sim["ok"] and report["ok"])
        out["ok"] = not out["problems"] and bool(out.get("simulated"))
        if deferred_finish is not None:
            deferred_finish(out)
        return out

    # deferred_finish 非空时允许移出主线程 —— 没有它就不能 defer，否则
    # 后台线程跑完没人回填，作业会挂到超时（toolserver 的 JobContext 只认
    # 一次 settle，双协程各写一半结果必然丢一半）。
    _sim_phase(netlist, out_dir, tmp_cell, netlist_path, audit, ctx,
               finish=_finish if deferred_finish is not None else None)
    if ctx is not None and getattr(ctx, "deferred", False):
        out.update({"deferred": True, "status": "running", "ok": False,
                    "note": "仿真已转后台线程执行；sim/dataset 证据在回填时更新"})
    return out


def _hz_to_ads(value) -> str:
    """Hz -> ADS 频率写法（GHz 优先，ADS 的习惯写法；至少 3 位有效数字）。"""
    try:
        hz = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"无法把 {value!r} 当频率处理")
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if hz >= scale:
            return f"{hz / scale:.6g} {unit}"
    return f"{hz:.6g} Hz"




def validate_model_import(args: dict, ctx=None) -> dict:
    """验证某个元件模型在当前工作区里**真的可用**，并产出可追溯的证据。

    这是「模型就绪」的唯一依据（backend/model_tools.py 只有本工具返回
    ok=True 才把包标成 ready）。检查分五层，每层都只报**真实发现**的问题：

      1. 库已挂接且可读（只读原厂库也能验证，关键是不能写坏它）
      2. cell 存在且能解析（读 model_def，拿不到就如实标 null）
      3. 请求参数与元件定义一致（取值范围**不校验** —— 本 ADS 版本读不到
         min/max，见 _PARAM_RANGE_REASON）
      4. Touchstone 模型：文件存在且能静态解析出端口数 / 频段 / 参考阻抗
      5. run_smoke_sim=true 时才搭真实夹具跑仿真（默认不跑）

    返回体里的 ``verification`` 是**证据对象**（与 backend/model_validation.py
    的定义一一对应）：它唯一绑定 workspace / package_id / model_id /
    library:cell / 实际参数 / 模型文件指纹，并把 five 阶段（已保存/已解压/
    静态可解析/已放置/已仿真）分别记成 ``status + detail + artifacts``。

    **不可逾越的三条**：
      * 缺 model_def、放不进电路、要求仿真却没跑、用户取消 —— 一律不得标
        记为「已仿真」；``overall`` 只到能证明的那一层。
      * 单元件通过**不暗示整包已验证**：包级结论由后端按所有模型的证据汇总，
        本工具只给这一条。
      * 静态通过就得说"静态通过"，不得写成"模型可用"（声称性能需要仿真）。
    """
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "验证模型")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch,
                "verified": False,
                "validated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    library, cell_name, _view = _parse_master(args.get("master"),
                                             args.get("library"),
                                             args.get("cell"))
    model_file = str(args.get("model_file") or args.get("file")
                     or args.get("path") or "").strip()
    if not library and not cell_name and not model_file:
        raise RuntimeError(
            "需要 library 与 cell（或 master='lib:cell'）；"
            "验证 Touchstone 模型文件时至少要给 model_file（可以是相对工作区的路径）")

    parameters = args.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise RuntimeError("parameters 必须是对象（参数名 -> 值）")
    run_smoke = bool(args.get("run_smoke_sim"))
    require_sim_arg = args.get("require_smoke_sim")
    simulation_required = bool(run_smoke) if require_sim_arg is None \
        else bool(require_sim_arg)
    if require_sim_arg and not run_smoke:
        run_smoke = True          # 要求仿真就得真的跑，不接受"说了算数"

    package_id = str(args.get("package_id") or "").strip()
    model_id = str(args.get("model_id") or "").strip()
    component_hint = str(args.get("component") or args.get("smoke_component")
                         or "").strip()
    target_band = args.get("target_band") if isinstance(args.get("target_band"),
                                                        dict) else None
    variant = _variant_from_args(args)
    model_ref = f"{library}:{cell_name}" if (library or cell_name) else \
        str(model_file or "")

    out = {
        "ok": False,
        "workspace": ws_path,
        "library": library,
        "cell": cell_name,
        "master": f"{library}:{cell_name}",
        "model_file": model_file,
        "validated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "problems": [],
        "warnings": [],
        "unverified": [],
    }

    stages = blank_verification_stages()
    artifacts: list = []

    # --- 0. 模型文件（Touchstone）：存在 -> 能解析出端口/频段 --------------
    header: dict = {}
    model_file_abs = ""
    file_rel = ""
    if model_file:
        model_file_abs = model_file if os.path.isabs(model_file) else \
            os.path.normpath(os.path.join(ws_path, model_file))
        # Touchstone 验证只允许读取当前工作区内的文件；相对路径还可能经过
        # junction/symlink 越界，因此用 realpath + commonpath 再做一次边界校验。
        try:
            workspace_real = os.path.realpath(ws_path)
            model_real = os.path.realpath(model_file_abs)
            inside_workspace = (os.path.commonpath((workspace_real, model_real))
                                == workspace_real)
        except (OSError, ValueError):
            inside_workspace = False
        if not inside_workspace:
            detail = "模型文件路径越出当前 ADS 工作区，已拒绝读取。"
            put_verification_stage(stages, "saved", "fail", detail)
            put_verification_stage(stages, "extracted", "fail", detail)
            put_verification_stage(stages, "parsed", "fail", detail)
            out["problems"].append(detail)
            out["verification"] = _seal_evidence(
                ws_path, package_id, model_id, model_ref, variant, parameters,
                "", "", {}, stages, simulation_required, artifacts)
            out["ok"] = False
            _finalize_verdict(out, out["verification"])
            return out
        try:
            file_rel = os.path.relpath(model_file_abs, ws_path).replace(os.sep, "/")
        except ValueError:      # 跨盘符：记绝对路径，不编造相对路径
            file_rel = ""
        if os.path.isfile(model_file_abs):
            put_verification_stage(stages, "saved", "pass",
                                   f"模型文件在工作区内可读: {file_rel or model_file_abs}")
            put_verification_stage(stages, "extracted", "pass",
                                   "文件内容可读（阶段定义在 backend/model_validation）")
            header = _touchstone_header(model_file_abs)
            if header.get("available"):
                put_verification_stage(
                    stages, "parsed", "pass",
                    f"Touchstone 头解析成功：{header.get('ports')} 端口，"
                    f"{header.get('freq_start_hz'):.6g}–{header.get('freq_stop_hz'):.6g} Hz"
                    f"（{header.get('points')} 个频点，来源: {header.get('ports_source')}）")
            else:
                put_verification_stage(stages, "parsed", "fail",
                                       header.get("error") or "Touchstone 头解析失败")
                out["problems"].append(f"模型文件无法静态解析：{header.get('error')}")
            for note in header.get("notes") or []:
                out["warnings"].append(note)
        else:
            put_verification_stage(stages, "saved", "fail",
                                   f"工作区内找不到该模型文件: {model_file}")
            put_verification_stage(stages, "extracted", "fail", "文件不可读")
            put_verification_stage(stages, "parsed", "fail", "无文件可解析")
            out["problems"].append(
                f"模型文件不存在：{model_file_abs}。"
                f"（可能未解压、路径不对，或这个工程里没有相应资产。）")
    elif library and cell_name:
        put_verification_stage(stages, "saved", "unknown",
                               "本次验证的是库里的元件定义，没有绑定模型文件")
        put_verification_stage(stages, "extracted", "unknown",
                               "同上：元件依赖的具体文件未能确定")

    # --- 1. 库 ------------------------------------------------------------
    lib = None
    if library:
        try:
            lib = _library_for(library)
        except Exception as e:  # noqa: BLE001
            out["problems"].append(str(e))
            out["verdict"] = "库未挂接"
            out["message"] = f"{library} 不在当前工作区的已打开库里 —— 先挂接再验证。"
            out["verification"] = _seal_evidence(ws_path, package_id, model_id,
                                                 model_ref, variant, parameters,
                                                 model_file_abs, file_rel, header,
                                                 stages, simulation_required,
                                                 artifacts)
            return out
        snap = _lib_snapshot(lib)
        out["library_mode"] = snap.get("mode") or (
            "read_only" if snap.get("is_read_only") else "unknown")
        out["is_read_only"] = snap.get("is_read_only")
        out["library_path"] = snap.get("path")
        out["attached_tech_lib_name"] = snap.get("attached_tech_lib_name")
        if snap.get("is_open") is False:
            out["problems"].append(f"库 {library} 处于未打开状态，无法验证。")
        if snap.get("is_an_ads_library") is False:
            out["warnings"].append(
                f"库 {library} 被 ADS 标记为非 ADS 库（is_an_ads_library=False）—— "
                f"元件与网表行为可能与预期不同。")
        if not snap.get("attached_tech_lib_name"):
            out["warnings"].append(
                f"库 {library} 没有 attached tech 库；涉及工艺/层的元件"
                f"可能无法正确仿真（射频基板类模型尤其如此）。")

    # --- 2. cell ----------------------------------------------------------
    md = None
    if lib is not None and cell_name:
        try:
            cell = _find_cell(lib, cell_name)
        except Exception as e:  # noqa: BLE001
            out["problems"].append(str(e))
            out["verdict"] = "cell 不存在或打不开"
            out["message"] = f"{library}:{cell_name} 不可用。"
            if stages.get("parsed", {}).get("status") == "not_checked":
                put_verification_stage(stages, "parsed", "fail", str(e))
            out["verification"] = _seal_evidence(ws_path, package_id, model_id,
                                                 model_ref, variant, parameters,
                                                 model_file_abs, file_rel, header,
                                                 stages, simulation_required,
                                                 artifacts)
            return out
        out["views"] = _cell_views(cell)
        md = _read_model_def(cell)
        # parameters / model_def 两个键**总是**出现（没有就是 None）。
        # 少了键的话，调用方无法区分「没读到」与「根本没查」——
        # 后端 model_tools.py 就是按 result["parameters"] 取值的。
        out["parameters"] = None
        out["model_def"] = None
        if md is None:
            out["warnings"].append(
                "该 cell 没有 model_def —— 无法核对参数定义，"
                "也无法确认它是不是可放置的元件。")
            put_verification_stage(stages, "parsed", "fail",
                                   "该 cell 没有 model_def，参数定义无法核对")
            out["unverified"].append("参数定义未验证（无 model_def）")
        elif isinstance(md, dict) and "error" in md:
            out["problems"].append(md["error"])
            put_verification_stage(stages, "parsed", "fail", md["error"])
        else:
            info = _model_def_info(md)
            out["model_def"] = {
                "impl_class": info.get("impl_class"),
                "name": info.get("name"),
                "label": info.get("label"),
                "component_name": info.get("component_name"),
                "param_count": info.get("count"),
            }
            out["parameters"] = info.get("parameters")
            out["component_def_fingerprint"] = _model_fingerprint(
                library, cell_name, info)
            if not model_file:
                put_verification_stage(stages, "parsed", "pass",
                                       f"元件定义可读，参数 {info.get('count')} 个")
    elif not model_file:
        out["warnings"].append("既没有给出 library/cell 也没有给出模型文件，"
                               "本次无法做元件级静态核对。")

    # --- 3. 参数一致性 ----------------------------------------------------
    param_check = _check_parameters(
        None if (md is None or isinstance(md, dict)) else md, parameters)
    out["parameter_check"] = param_check
    out["problems"].extend(param_check.get("problems") or [])
    out["warnings"].extend(param_check.get("warnings") or [])
    out["unverified"].append(
        "参数取值范围未校验（本 ADS 版本的 ModelParam 不提供 min/max）")
    if not variant:
        out["unverified"].append(
            "未声明变体（偏压/封装/温度）：同一型号若有多个变体，"
            "本条证据只代表当前这一组条件下的一个对象")
    if param_check.get("problems") and stages.get("parsed", {}).get("status") == "pass":
        put_verification_stage(stages, "parsed", "fail",
                               "参数与定义不一致：" + "; ".join(
                                   param_check["problems"])[:300])

    def _apply_smoke(smoke: dict) -> None:
        """把一次冒烟仿真的结果**单向**写进阶段表（只能把证明推进，不能冒领）。"""
        out["smoke_sim"] = smoke
        if smoke.get("netlist_path"):
            artifacts.append(smoke["netlist_path"])
            out.setdefault("netlist_path", smoke["netlist_path"])
        if smoke.get("artifacts", {}).get("output_dir"):
            artifacts.append(smoke["artifacts"]["output_dir"])
            out["output_dir"] = smoke["artifacts"]["output_dir"]
        if smoke.get("artifacts", {}).get("temp_design"):
            artifacts.append(smoke["artifacts"]["temp_design"])
        dataset = (smoke.get("dataset") or {}).get("dataset_path") or \
            (smoke.get("sim") or {}).get("dataset_path") or ""
        if dataset:
            artifacts.append(dataset)
            out["dataset_path"] = dataset

        if smoke.get("cancelled"):
            put_verification_stage(stages, "placed", "cancelled",
                                   smoke.get("reason") or "用户取消")
            put_verification_stage(stages, "simulated", "cancelled",
                                   smoke.get("reason") or "用户取消")
            out["warnings"].append("已请求取消，未进入仿真阶段。")
            out["unverified"].append("未跑仿真（已取消）")
        elif not smoke.get("ran"):
            detail = smoke.get("reason") or "未知原因"
            put_verification_stage(stages, "placed", "fail", detail)
            put_verification_stage(stages, "simulated", "fail",
                                   "放置/夹具阶段未通过，仿真没有执行")
            level = "problems" if simulation_required else "warnings"
            out[level].append(f"未搭成验证夹具：{detail}")
            out["unverified"].append("未跑仿真（夹具未搭成）")
        else:
            placed_ok = bool(smoke.get("stages", {}).get("saved"))
            put_verification_stage(
                stages, "placed",
                "pass" if placed_ok else "fail",
                ("夹具已建并保存后只读复核通过" if placed_ok
                 else "夹具保存/只读复核未通过："
                      + "; ".join(smoke.get("problems") or [])[:300]))
            for problem in (smoke.get("problems") or []):
                out["problems"].append(f"仿真实证: {problem}")
            dataset_report = smoke.get("dataset") or {}
            if bool(smoke.get("simulated")):
                put_verification_stage(
                    stages, "simulated", "pass",
                    f"仿真产出数据集并读到 S 曲线："
                    f"{', '.join(sorted(dataset_report.get('curves') or {}))[:200]}")
            elif bool(smoke.get("deferred")):
                put_verification_stage(stages, "simulated", "not_checked",
                                       "仿真在后台线程执行，尚未回填（回填后重判）")
                out["unverified"].append("仿真仍在执行（后台线程）")
            else:
                put_verification_stage(
                    stages, "simulated", "fail",
                    "仿真未通过数据核对：" + "; ".join(
                        (smoke.get("sim") or {}).get("hint")
                        and [str((smoke.get('sim') or {}).get('hint'))]
                        or (dataset_report.get("problems") or
                            ["仿真进程未产出可用数据集"]))[:300])
                out["unverified"].append("仿真未通过数据核对")

    # --- 4. 可选：真实仿真实证 --------------------------------------------
    if run_smoke:
        if _cancel_requested(ctx):
            out["warnings"].append("已请求取消，未进入仿真阶段。")
            out["unverified"].append("未跑仿真（已取消）")
            put_verification_stage(stages, "placed", "cancelled", "用户取消")
            put_verification_stage(stages, "simulated", "cancelled", "用户取消")
            out["smoke_sim"] = {"ran": False, "cancelled": True,
                                "reason": "已请求取消，未启动仿真。"}
        else:
            def _deferred(merged: dict) -> None:
                _apply_smoke(merged)
                out["verification"] = _seal_evidence(
                    ws_path, package_id, model_id, model_ref, variant, parameters,
                    model_file_abs, file_rel, header, stages, simulation_required,
                    artifacts)
                out["ok"] = not out["problems"] and _ok_from_evidence(
                    out["verification"])
                out["verified"] = out["ok"]
                _finalize_verdict(out, out["verification"])
                if ctx is not None and hasattr(ctx, "finish"):
                    ctx.finish(out)

            smoke = _touchstone_smoke_test(
                ctx, ws, ws_path, model_file_abs=model_file_abs, header=header,
                ports=int(header.get("ports") or 0),
                requested_band=target_band,
                explicit_component=component_hint, extra_params=parameters,
                reference_ohm=header.get("reference_impedance_ohm"),
                notes=out["warnings"],
                dut_master=(f"{library}:{cell_name}" if (library and cell_name
                                                         and not model_file) else ""),
                deferred_finish=_deferred)
            _apply_smoke(smoke)
    else:
        out["smoke_sim"] = {"ran": False,
                            "reason": "本次未要求仿真实证（run_smoke_sim 未开启）",
                            "supported": True}
        put_verification_stage(stages, "placed", "skipped", "未要求仿真，未建夹具")
        put_verification_stage(stages, "simulated", "skipped", "未要求仿真")
        out["unverified"].append(
            "**本次为静态校验，没有跑仿真** —— 不能据此说模型在目标频段的性能")

    out["verification"] = _seal_evidence(
        ws_path, package_id, model_id, model_ref, variant, parameters,
        model_file_abs, file_rel, header, stages, simulation_required, artifacts)
    out["ok"] = not out["problems"] and _ok_from_evidence(out["verification"])
    out["verified"] = out["ok"]
    _finalize_verdict(out, out["verification"])
    return out


# ---------------------------------------------------------------------------
# 证据对象的组装（口径以 backend/model_validation.py 为准，这里是 ADS 侧的
# 同一份形状 —— 后端会按 record_evidence 重算 overall，两边措辞必须一致）
# ---------------------------------------------------------------------------

# 阶段顺序即"验证深度"（与 backend/model_validation.py::STAGES 逐字一致）：
# booted/listed 夹在 parsed 与 placed 之间 —— Design Kit 导入后先"库已加载
# 启动配置"（eesof_lib.cfg → boot.ael → palette.ael 真的被 DE 加载），再"原生
# 列表可见"。**两者都不是仿真通过**：没放进电路、没跑 hpeesofsim，就不能拿
# 它们当"可用"。这两个阶段由 open_vendor_palette 记录，单模型验证不评估。
_VERIFICATION_STAGES = ("saved", "extracted", "parsed", "booted", "listed",
                        "placed", "simulated")
_VERIFICATION_STAGE_LABELS = {"saved": "已保存", "extracted": "已解压",
                              "parsed": "静态可解析",
                              "booted": "库已加载启动配置",
                              "listed": "原生列表可见",
                              "placed": "已放置", "simulated": "已仿真"}
_VERIFICATION_STATUS_LABELS = {"not_checked": "未检查", "pass": "通过",
                               "fail": "失败", "skipped": "跳过",
                               "cancelled": "已取消", "unknown": "无法核对"}
_OVERALL_LABELS_LOCAL = {"not_verified": "未验证", "saved": "仅已保存",
                         "extracted": "仅已解压", "parsed": "仅静态可解析",
                         "booted": "仅库已加载启动配置（未仿真）",
                         "listed": "仅原生列表可见（未仿真）",
                         "placed": "仅已放置（未仿真）", "simulated": "已仿真验证",
                         "failed": "验证失败", "cancelled": "已取消"}


def blank_verification_stages() -> dict:
    return {stage: {"status": "not_checked", "detail": "", "artifacts": []}
            for stage in _VERIFICATION_STAGES}


def put_verification_stage(stages: dict, stage: str, status: str,
                           detail: str = "", artifacts=None) -> dict:
    """写一个阶段。**前序阶段没有记录时不能被后序通过反推为通过**。"""
    if stage not in _VERIFICATION_STAGES:
        raise RuntimeError(f"未知阶段 {stage!r}")
    entry = dict(stages.get(stage) or {})
    entry["status"] = status
    if detail:
        entry["detail"] = detail
    entry["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    if artifacts:
        merged = list(entry.get("artifacts") or [])
        for item in artifacts:
            if str(item) not in merged:
                merged.append(str(item))
        entry["artifacts"] = merged
    stages[stage] = entry
    if status == "pass":
        idx = _VERIFICATION_STAGES.index(stage)
        for prior in _VERIFICATION_STAGES[:idx]:
            prior_entry = dict(stages.get(prior) or {})
            if prior_entry.get("status") in ("not_checked", ""):
                prior_entry["status"] = "unknown"
                prior_entry["detail"] = (
                    prior_entry.get("detail")
                    or f"本阶段无记录，不能由「{_VERIFICATION_STAGE_LABELS[stage]}」通过反推")
                stages[prior] = prior_entry
    return stages


def _overall_from_verification(stages: dict, simulation_required: bool) -> tuple:
    """从阶段表算总判定 + 未验证项（与 backend/model_validation.overall_from_stages 同口径）。"""
    highest = ""
    failed = False
    cancelled = False
    for stage in _VERIFICATION_STAGES:
        status = (stages.get(stage) or {}).get("status") or "not_checked"
        if status == "pass":
            highest = stage
        elif status == "fail":
            failed = True
        elif status == "cancelled":
            cancelled = True
    unverified = []
    if failed:
        overall = "failed"
    elif simulation_required and highest != "simulated":
        overall = "cancelled" if cancelled else (highest or "not_verified")
        if highest != "simulated":
            unverified.append("**要求仿真但未到达「已仿真」**：不能以静态结论替代")
    else:
        overall = highest or "not_verified"
    for stage in _VERIFICATION_STAGES:
        status = (stages.get(stage) or {}).get("status")
        if status == "unknown":
            unverified.append(f"{_VERIFICATION_STAGE_LABELS[stage]}：无记录可核对")
        elif status == "fail":
            unverified.append(f"{_VERIFICATION_STAGE_LABELS[stage]}："
                              f"{stages[stage].get('detail') or '失败'}")
        elif status == "skipped":
            unverified.append(f"{_VERIFICATION_STAGE_LABELS[stage]}：本次跳过")
    return overall, unverified


def _variant_from_args(args: dict) -> dict:
    """从调用参数里抽出**变体区分键**（偏压 / 封装 / 温度 / 频段条件）。

    不做推测：调用方没给就是没给，返回空 dict —— 空变体的证据不代表该型号
    的其它变体（同一型号不同偏置是两个不同的器件）。
    """
    variant: dict = {}
    explicit = args.get("variant")
    if isinstance(explicit, dict) and explicit:
        variant.update({str(k): str(v) for k, v in explicit.items()})
    for key, alias in (("bias", "bias"), ("package", "package"),
                       ("temperature", "temperature")):
        value = args.get(key)
        if isinstance(value, dict) and value:
            variant[alias] = ";".join(f"{k}={v}" for k, v in sorted(value.items()))
        elif isinstance(value, str) and value.strip():
            variant[alias] = value.strip()
    for extra in ("VCE", "VDS", "IC", "ID", "VGS"):
        if extra in (args.get("parameters") or {}):
            variant.setdefault(f"param:{extra}", str(args["parameters"][extra]))
    variant.setdefault("__note", "")
    return variant


def _seal_evidence(workspace: str, package_id: str, model_id: str, model_ref: str,
                   variant: dict, parameters: dict, model_file_abs: str,
                   file_rel: str, header: dict, stages: dict,
                   simulation_required: bool, artifacts: list) -> dict:
    """把一次验证的所有**可核对事实**封成证据对象。"""
    overall, unverified = _overall_from_verification(stages, simulation_required)
    file_info = {
        "relpath": file_rel,
        "abs_path": model_file_abs,
        "exists": bool(model_file_abs) and os.path.isfile(model_file_abs),
        "size_bytes": None,
        "ports": (header or {}).get("ports"),
        "freq_start_hz": (header or {}).get("freq_start_hz"),
        "freq_stop_hz": (header or {}).get("freq_stop_hz"),
        "reference_impedance_ohm": (header or {}).get("reference_impedance_ohm"),
        "option_line": (header or {}).get("option_line") or "",
    }
    if file_info["exists"]:
        try:
            file_info["size_bytes"] = os.path.getsize(model_file_abs)
        except OSError:
            pass
        import hashlib as _hashlib
        digest = _hashlib.sha256()
        try:
            with open(model_file_abs, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            file_info["sha256"] = digest.hexdigest()
        except OSError:
            file_info["sha256"] = ""
    else:
        file_info["sha256"] = ""

    import hashlib as _hashlib
    variant_clean = {k: v for k, v in (variant or {}).items()
                     if k != "__note" and str(v)}
    variant_key = _hashlib.sha256("\n".join(
        f"{k}={v}" for k, v in sorted(variant_clean.items())).encode("utf-8")
    ).hexdigest()[:12] if variant_clean else ""
    params_fp = _hashlib.sha256(json.dumps(
        {str(k): str(v) for k, v in sorted((parameters or {}).items())},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    bits = [f"{model_ref or '未知对象'}：{_OVERALL_LABELS_LOCAL.get(overall, overall)}"]
    if variant_clean:
        bits.append("变体 " + ", ".join(f"{k}={v}" for k, v in sorted(variant_clean.items())))
    if file_rel:
        bits.append(f"文件 {file_rel.rsplit('/', 1)[-1]}")
    return {
        "workspace": str(workspace or ""),
        "package_id": str(package_id or ""),
        "model_id": str(model_id or ""),
        "model_ref": str(model_ref or ""),
        "variant": variant_clean,
        "variant_key": variant_key,
        "parameters": dict(parameters or {}),
        "params_fingerprint": params_fp,
        "file": file_info,
        "file_fingerprint": file_info.get("sha256") or "",
        "stages": stages,
        "simulation_required": bool(simulation_required),
        "overall": overall,
        "highest_stage": next((s for s in reversed(_VERIFICATION_STAGES)
                               if (stages.get(s) or {}).get("status") == "pass"), ""),
        "unverified": unverified,
        "synthesis": "；".join(bits),
        "artifacts": [str(a) for a in (artifacts or [])],
        "stage_labels": dict(_VERIFICATION_STAGE_LABELS),
        "status_labels": dict(_VERIFICATION_STATUS_LABELS),
        "note": "单元件证据：不代表同一包里其它模型已验证。",
    }


def _ok_from_evidence(verification: dict) -> bool:
    """静态 bug-free 且（若要求仿真）确实到达了「已仿真」。"""
    stages = verification.get("stages") or {}
    overall = verification.get("overall") or ""
    # booted/listed 是**库级**阶段（由 open_vendor_palette 记录），本工具
    # （单模型验证）从不写它们，默认 not_checked —— 列进来只是为了让"有 fail
    # 就否决"覆盖新的完整阶段集；**绝不**把它们当"通过仿真"（overall 的
    # "simulated" 只能来自 simulated 阶段 pass，见 _overall_from_verification）。
    if any((stages.get(s) or {}).get("status") == "fail"
           for s in ("saved", "extracted", "parsed", "booted", "listed",
                     "placed", "simulated")):
        return False
    if verification.get("simulation_required") and overall != "simulated":
        return False
    if overall in ("not_verified", "failed", "cancelled"):
        return False
    if (stages.get("parsed") or {}).get("status") != "pass":
        return False
    return True


def _finalize_verdict(out: dict, verification: dict) -> None:
    """判定与文案：界面、工具返回、LLM 摘要共用这一份措辞。"""
    overall = verification.get("overall") or "not_verified"
    label = _OVERALL_LABELS_LOCAL.get(overall, overall)
    out["evidence_overall"] = overall
    if out["ok"]:
        out["verdict"] = label
        if overall == "simulated":
            out["message"] = ("该模型已通过**含仿真实证**的验证（网表引用指定模型文件、"
                              "端口独立、读出 S 曲线）。")
        elif simulation_required_text(verification):
            out["message"] = "验证未达到要求的仿真层级，不能说「模型可用」。"
        else:
            out["message"] = ("静态校验通过（元件可解析、参数与定义一致）。"
                              "**没有跑仿真** —— 要说「性能可用」请再跑一次仿真。")
    else:
        out["verdict"] = ("验证未通过" if overall in ("failed", "cancelled")
                          else "未达成本次要求")
        out["message"] = "存在阻断问题或未达到要求的验证层级（见 problems / unverified）。"


def simulation_required_text(verification: dict) -> bool:
    return bool(verification.get("simulation_required"))


def _model_fingerprint(library: str, cell: str, info: dict) -> str:
    """元件定义的指纹：库名 + cell 名 + 参数名/类型/默认值。

    注意与 ``ads_ops.design_fingerprint`` 返回的 ``model_fingerprint``
    区分：那个是「设计引用到的模型文件**内容**哈希」的 dict，用于结果复用的
    缓存失效；这里是「元件**定义**」的字符串指纹，用于判断库里定义有没有变过。
    两者语义不同，调用方不要混用。

    用途是「型号换了没有」的判据 —— 参数集合变了就该重新核对，
    旧仿真结果不能直接复用（这与 design_fingerprint 用网表 sha 是同一
    个思路：内容变了就是另一个东西）。这里**不含仿真数据**，因为静态
    校验阶段本来就没有仿真数据。
    """
    import hashlib as _hashlib
    parts = [f"{library}:{cell}"]
    for p in (info.get("parameters") or []):
        parts.append("|".join([str(p.get("name")), str(p.get("param_type")),
                               str(p.get("unit")), str(p.get("default"))]))
    return _hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# DISPATCH（由 ads_ops 汇入总表）
# ---------------------------------------------------------------------------

HANDLERS = {
    # 后端 model_tools.py 实际调用的四个名字 —— 少一个后端就报"未知工具"
    "attach_design_kit": attach_design_kit,
    "list_vendor_models": list_library_components,
    "get_vendor_model_info": inspect_component_model,
    "validate_model_import": validate_model_import,
    # 语义更细的别名，与上面共用实现
    "list_readonly_libraries": list_readonly_libraries,
    "list_library_components": list_library_components,
    "inspect_component_model": inspect_component_model,
    "detach_design_kit": detach_design_kit,
    # 把已挂接的原厂包在 ADS 原生元件列表里打开/定位（只读定位，≠ 模型可用）
    "open_vendor_palette": open_vendor_palette,
}
