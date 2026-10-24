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

API 依据（**读源码核对，非记忆**；ADS 2027 安装根 E:\\ADS\\Programfile\\ADS2027）
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
        return {"ok": False, "cancelled": True, "package_id": package_id,
                "workspace": ws_path, "kit_root": kit_root,
                "libraries": attached, "already_attached": False,
                "attached_at": "",
                "message": "已请求取消；已完成的挂接保留，未完成的库不再继续挂接。"}

    attached_at = datetime.datetime.now().isoformat(timespec="seconds")
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
        }
    record["attached_at"] = attached_at
    record_path = ""
    record_error = ""
    try:
        record_path = _save_record(ws_path, record)
    except RuntimeError as e:
        record_error = str(e)

    ok = bool(attached) and not failed
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
        # 挂接只是让库出现在工作区里；能不能放进电路仿真只有验证能说。
        "usable": False,
        "verified": False,
        "next_step": "库已挂接（只读）。**挂接成功不等于模型可用** —— "
                     "用 list_vendor_models 找元件、get_vendor_model_info 看参数，"
                     "再用 validate_model_import 验证后才能放进电路仿真。",
    }
    if fallback:
        out["lib_defs_fallback"] = fallback
    if record_error:
        out["record_error"] = record_error
    if not ok:
        out["error"] = ("没有成功挂接任何库。"
                       + ("；失败: " + "; ".join(f["reason"] for f in failed)
                          if failed else "")
                       + ("；冲突: " + "; ".join(c["name"] for c in conflicts)
                          if conflicts else ""))
    return out


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

    candidates = {}
    for nm, e in entries.items():
        if not isinstance(e, dict):
            continue
        if package_id and str(e.get("package_id") or "") != package_id:
            continue
        if want and nm not in want:
            continue
        candidates[nm] = e
    if want:
        missing = [n for n in want if n not in candidates]
    else:
        missing = []
    if not candidates:
        rec_path = _record_path(ws_path)
        return {"ok": False, "workspace": ws_path,
                "record_path": rec_path,
                "removed": [],
                "skipped": [{"name": n, "reason": "不在本工具的挂接记录里"}
                            for n in (missing or want)],
                "error": f"{rec_path} 里没有本工具挂接的库记录"
                         f"{'（未记录：' + ', '.join(missing) + '）' if missing else ''}，"
                         f"已停止 —— 本工具只移除自己挂的引用，不会动你手工挂的库。"}

    lock = _ws_lock(ws_path)
    if not lock.acquire(timeout=120):
        raise RuntimeError(f"等待工作区 {ws_path} 的挂接锁超时（120s），稍后重试")

    removed: list = []
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
                    "skipped": skipped, "error": str(e)}

    return {
        "ok": bool(removed) or not candidates,
        "workspace": ws_path,
        "removed": removed,
        # skipped 统一是 dict —— 之前"未记录的名字"塞的是裸字符串，
        # 调用方遍历时会ItemTypeError（自测抓到）。形状必须一致。
        "skipped": skipped + [{"name": n, "reason": "不在本工具的挂接记录里"}
                              for n in missing],
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


def _smoke_sim(ctx, ws, ws_path: str, library: str, cell_name: str,
               parameters: dict, notes: list) -> dict:
    """最小基础仿真实证（只在 run_smoke_sim=true 时才走）。

    做法：另起一个**临时 cell**，放该元件 +两个接地端口 + 一个 S 参数
    控制器，生成网表后交给 ads_ops._simulate 跑。刻意不复用用户的
    原理图，也**不删除任何产物** —— 产物路径原样返回，让用户能自己
    打开网表核对（"验证过了"应该是可复核的，不是一句结论）。

    诚实边界：这只证明「网表能生成且hpeesofsim 接受这个元件」，**不证明
    模型在目标频段的精度**。原厂模型常常要求特定偏压/温度/频段才能得到
    有意义的 S 参数，随手跑出来的曲线只能当"能被调用"的证据。
    """
    import ads_ops

    writable = sorted(str(x) for x in (ws.writable_library_names or []))
    if not writable:
        return {"ran": False,
                "reason": "当前工作区没有可写库，无法建临时 cell 做仿真实证。"
                          "（原厂库是只读的，不会为了验证去给它们写权限。）"}
    # 临时 cell 建在**哪个**库里必须说清楚：这是往用户的库里写了一个
    # cell，属于真实副作用。优先用本插件自己的库（ADS_AGENT_*），
    # 没有就挑第一个可写库并在返回体里点名 —— 让用户知道去哪儿清理。
    host_lib = next((n for n in writable if n.startswith("ADS_AGENT")), writable[0])
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_cell = f"ADS_AGENT_VERIFY_{stamp}"

    created = ads_ops._ensure_cell_view(ws, host_lib, tmp_cell, "schematic")
    design = ads_ops._open_design(host_lib, tmp_cell, "schematic", write=True)
    try:
        master = f"{library}:{cell_name}"
        design.add_instance(master, (0.0, 0.0), name="X1")
        # TermG 单脚自带地（README坑表：ads_simulation:TermG），不用再放 GND
        design.add_instance("ads_simulation:TermG", (-40.0, 0.0), name="T1")
        design.add_instance("ads_simulation:TermG", (40.0, 0.0), name="T2")
        design.add_instance("ads_simulation:S_Param", (0.0, -40.0), name="SP1")
        # 请求的 parameters 是给**元件**的，不是给控制器的 —— 设到 X1 上
        x1 = _find_inst_by_name(design, "X1")
        applied: dict = {}
        for k, v in (parameters or {}).items():
            try:
                applied[k] = ads_ops._set_param(x1, k, v)
            except Exception as e:  # noqa: BLE001
                notes.append(f"临时仿真里设置 {k}={v!r} 失败: {type(e).__name__}: {e}")
        audit = ads_ops._design_audit(design)
        netlist = design.generate_netlist()
    except Exception as e:  # noqa: BLE001
        try:
            design.save_design()
        except Exception:  # noqa: BLE001
            pass
        return {"ran": False, "reason": f"{type(e).__name__}: {e}",
                "temp_design": f"{host_lib}:{tmp_cell}:schematic",
                "cell_created": bool(created.get("created"))}
    finally:
        ads_ops._close_design(design)

    try:
        design.save_design()
    except Exception as e:  # noqa: BLE001
        notes.append(f"临时设计保存失败（不影响网表生成）: {type(e).__name__}: {e}")

    import hashlib as _hashlib
    audit["_workspace"] = {"name": str(getattr(ws, "name", "") or ""), "path": ws_path}
    audit["_design_version"] = {
        "netlist_sha": _hashlib.sha256(netlist.encode("utf-8")).hexdigest()[:16],
        "netlist_chars": len(netlist),
    }
    # 逐个核对临时设计里的参数确实落到了网表上 —— _set_param 只在实例
    # 上赋值成功过，不等于 netlist 里就是那个值。
    netlist_applied = {
        k: (str(v) in netlist or str(v).strip('"') in netlist)
        for k, v in (parameters or {}).items()
    }
    out_dir = ads_ops.unique_sim_dir(ws_path, tmp_cell)
    netlist_path = os.path.join(out_dir, "netlist.ckt")
    try:
        with open(netlist_path, "w", encoding="utf-8") as f:
            f.write(netlist)
    except OSError:
        netlist_path = ""
    if _cancel_requested(ctx):
        return {"ran": False, "cancelled": True,
                "reason": "已请求取消，未启动仿真。网表已生成但未运行。",
                "netlist_path": netlist_path, "output_dir": out_dir,
                "temp_design": f"{host_lib}:{tmp_cell}:schematic"}
    try:
        result = ads_ops._simulate(netlist, out_dir, tmp_cell, netlist_path,
                                   audit, note=getattr(ctx, "note", None))
    except Exception as e:  # noqa: BLE001
        return {"ran": True, "ok": False, "error": str(e),
                "netlist_path": netlist_path, "output_dir": out_dir,
                "temp_design": f"{host_lib}:{tmp_cell}:schematic",
                "meaning": "网表已生成但仿真进程失败 —— 模型本身是否可用仍未定论，"
                           "请看 netlist_path 与 output_dir 里的日志。"}
    result.update({
        "ran": True,
        "ok": result.get("status") == "done",
        "temp_design": f"{host_lib}:{tmp_cell}:schematic",
        "temp_design_library": host_lib,
        "netlist_path": netlist_path,
        "output_dir": out_dir,
        "params_set": applied,
        # 参数没进网表就必须说出来：_set_param 返回成功但网表里找不到值，
        # 说明元件把它网表化成别的东西了（比如写进 include 或被回调改写）。
        "params_in_netlist": netlist_applied,
        "params_not_in_netlist": [k for k, okv in netlist_applied.items() if not okv],
        "meaning": "只证明网表能生成且 hpeesofsim 接受这个元件 —— "
                   "不证明模型在目标频段的精度（偏压/温度/频段条件未核对）。",
        "not_deleted": "临时设计与仿真产物均已保留，未删除任何用户数据。"
                      f"临时 cell 建在 {host_lib} 库里（ADS_AGENT_VERIFY_* 前缀），"
                      f"确认无用后可自行删除。",
    })
    return result


def _first_inst(design):
    for i in list(design.instances or []):
        return i
    return None


def _find_inst_by_name(design, name: str):
    finder = getattr(design, "find_instance", None)
    if callable(finder):
        try:
            got = finder(name)
            if got is not None:
                return got
        except Exception:  # noqa: BLE001
            pass
    for i in list(design.instances or []):
        for attr in ("inst_name", "name", "instance_name"):
            if str(getattr(i, attr, "") or "") == str(name):
                return i
    return _first_inst(design)


def validate_model_import(args: dict, ctx=None) -> dict:
    """验证某个元件模型在当前工作区里**真的可用**。

    这是「模型就绪」的唯一依据（backend/model_tools.py 只有本工具返回
    ok=True 才把包标成 ready）。分四层检查，每层都只报**真实发现**的问题：

      1. 库已挂接且可读（是否只读、是否 ADS 库、tech 库关系）
      2. cell 存在且能解析（读 model_def，拿不到就如实标 null）
      3. 请求参数与元件定义一致（名字存在？类型对得上？取值范围**不校验**
         —— 本 ADS 版本读不到 min/max，见 _PARAM_RANGE_REASON）
      4. run_smoke_sim=true 时才跑最小基础仿真（默认不跑：用户没要求
         就不该自动开始耗时仿真）

    返回体里的 verified 只表示"静态检查全部通过"，并明确列出
    **未验证项** —— 尤其是"没有跑仿真"这件事必须显眼，不能让调用方
    把静态通过当成仿真实证。
    """
    ws, ws_path, _expected, mismatch = _workspace_guard(args, "验证模型")
    if mismatch:
        return {"ok": False, "kind": "workspace_mismatch", "error": mismatch,
                "validated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    library, cell_name, _view = _parse_master(args.get("master"),
                                             args.get("library"),
                                             args.get("cell"))
    if not library or not cell_name:
        raise RuntimeError("需要 library 与 cell（或 master='lib:cell'）")
    parameters = args.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise RuntimeError("parameters 必须是对象（参数名 -> 值）")
    run_smoke = bool(args.get("run_smoke_sim"))

    out = {
        "ok": False,
        "workspace": ws_path,
        "library": library,
        "cell": cell_name,
        "master": f"{library}:{cell_name}",
        "validated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "problems": [],
        "warnings": [],
        "unverified": [],
    }

    # --- 1. 库---
    try:
        lib = _library_for(library)
    except Exception as e:  # noqa: BLE001
        out["problems"].append(str(e))
        out["verdict"] = "库未挂接"
        out["message"] = f"{library} 不在当前工作区的已打开库里 —— 先挂接再验证。"
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

    # --- 2. cell---
    try:
        cell = _find_cell(lib, cell_name)
    except Exception as e:  # noqa: BLE001
        out["problems"].append(str(e))
        out["verdict"] = "cell 不存在或打不开"
        out["message"] = f"{library}:{cell_name} 不可用。"
        return out
    out["views"] = _cell_views(cell)
    md = _read_model_def(cell)
    # parameters / model_def 两个键**总是**出现（没有就是 None）。
    # 少了键的话，调用方无法区分「没读到」与「根本没查」——
    # 后端 model_tools.py 就是按result["parameters"] 取值的。
    out["parameters"] = None
    out["model_def"] = None
    if md is None:
        out["warnings"].append(
            "该 cell 没有 model_def —— 无法核对参数定义，"
            "也无法确认它是不是可放置的元件。")
        out["unverified"].append("参数定义未验证（无 model_def）")
    elif isinstance(md, dict) and "error" in md:
        out["problems"].append(md["error"])
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
        # 键名刻意**不叫** model_fingerprint：ads_ops.design_fingerprint 里的
        # 同名字段是「设计引用到的模型文件内容哈希（路径→sha）」，是给结果
        # 复用做缓存失效判据的 dict。而这里算的是「这个元件的定义指纹」，
        # 用来判断"库里的元件定义有没有变过"，是个字符串。
        # 两个语义完全不同的东西共用一个名字，跨模块读起来必然出错 ——
        # 所以按语义拆开：component_def_fingerprint。
        out["component_def_fingerprint"] = _model_fingerprint(library, cell_name, info)

    # --- 3. 参数一致性 ---
    param_check = _check_parameters(
        None if (md is None or isinstance(md, dict)) else md, parameters)
    out["parameter_check"] = param_check
    out["problems"].extend(param_check.get("problems") or [])
    out["warnings"].extend(param_check.get("warnings") or [])
    out["unverified"].append(
        "参数取值范围未校验（本 ADS 版本的 ModelParam 不提供 min/max）")

    # --- 4. 可选：最小基础仿真 ---
    if run_smoke:
        if _cancel_requested(ctx):
            out["warnings"].append("已请求取消，未进入仿真阶段。")
            out["unverified"].append("未跑仿真（已取消）")
        else:
            smoke = _smoke_sim(ctx, ws, ws_path, library, cell_name,
                               parameters, out["warnings"])
            out["smoke_sim"] = smoke
            if smoke.get("cancelled"):
                out["unverified"].append("未跑仿真（已取消）")
            elif not smoke.get("ran"):
                out["warnings"].append(f"未跑仿真：{smoke.get('reason') or '未知原因'}")
                out["unverified"].append("未跑仿真")
            elif not smoke.get("ok"):
                out["problems"].append(f"基础仿真失败：{smoke.get('error')}")
                out["dataset_path"] = smoke.get("dataset_path") or ""
            else:
                out["dataset_path"] = smoke.get("dataset_path") or ""
                out["output_dir"] = smoke.get("output_dir") or ""
                out["netlist_path"] = smoke.get("netlist_path") or ""
    else:
        out["smoke_sim"] = {"ran": False,
                            "reason": "本次未要求仿真实证（run_smoke_sim 未开启）"}
        out["unverified"].append(
            "**本次为静态校验，没有跑仿真** —— 不能据此说模型在目标频段的性能")

    out["ok"] = not out["problems"]
    out["verified"] = out["ok"]
    if out["ok"]:
        out["verdict"] = ("静态校验通过" + ("（含基础仿真）" if run_smoke
                                            and (out.get("smoke_sim") or {}).get("ran")
                                            and (out.get("smoke_sim") or {}).get("ok")
                                            else ""))
        out["message"] = ("该元件可被解析、参数与定义一致。"
                          + ("" if run_smoke else "本次为静态校验，未跑仿真 —— "
                                                  "要说「性能可用」请再跑一次仿真。"))
    else:
        out["verdict"] = "验证未通过"
        out["message"] = "存在阻断问题，该元件不能直接用于仿真（见 problems）。"
    return out


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
}