# -*- coding: utf-8 -*-
"""模型依赖指纹（ADS 端，纯标准库，可离线测试）。

为什么要有这个模块
------------------
网表指纹（``netlist_sha``）只覆盖**网表文本**。原厂模型的真实风险是：

    用户在同一个路径上把 ``GRM155.s2p`` 换成原厂新版 / 换偏压条件 /
    换温度条件下的变体 —— 网表一个字都不会变，但仿真结果完全不同。

只比网表就会把一份过期结论当成有效结果复用出去。所以仿真时必须额外
记录「这次到底吃了哪些模型文件、它们的内容哈希是什么」，复用前重新
核对一遍。

四态（调用方据此决定能不能复用，见 backend/model_gate.py）
----------------------------------------------------------
    none        设计不引用任何外部模型文件 —— 网表指纹已经覆盖全部输入，
                模型侧无需核对；
    complete    有依赖，且**每一项**都定位到盘上真实文件并算出了哈希；
    incomplete  有依赖但至少一项无法解析（相对路径找不到 / VAR 表达式
                求值不了 / 层次子图打不开 / 只能给套件级保守指纹）；
    missing     之前记录过的模型文件现在不在盘上了。

``incomplete`` / ``missing`` 一律按**保守**处理：不能确认"没变"就重跑。
宁可多跑一次，不能把错结果当对的复用 —— 一个假的"没变化"比明说
"不知道"危险得多。

两段式（重要，关系到 ADS 主线程）
--------------------------------
    collect_plan()   需要打开设计、遍历层次子图 —— 必须跑在 ADS 主线程；
                     产出"要哈希什么"的清单，**不做任何文件读取**。
    finalize()       只做文件读取与分块哈希 —— 不碰 DE 对象，可以安全地
                     放在后台线程（正常仿真就是这么接的）。

支持的模型文件
--------------
``.sNp``（N 为**任意**端口数，不止 1~4）与 ``.ts``（Touchstone）。
``.ts`` 的端口数无法从文件名得出（与 model_store 的口径一致），因此
端口数留 None，不猜。

Design Kit（套件级保守指纹）
----------------------------
Design Kit 元件（``.ael`` / ``.atf``）不把模型文件路径写在实例参数里，
无法定位到具体文件。此时可以给**整个套件目录**算一个保守指纹：
套件里任何文件变化都会让指纹变化 —— 能发现"套件动过"，但定位不到
具体元件，代价见 :func:`kit_fingerprint` 的 docstring（写明成本与
支持范围）。用了套件指纹，整体状态就是 ``incomplete``。
"""

import hashlib
import os
import re

# 模型文件后缀：.sNp 的 N 是**任意**端口数（.s8p/.s12p 都算），不是 1~4。
# 与 backend/model_store.py 的 _TOUCHSTONE_RE 保持同一口径。
MODEL_FILE_RE = re.compile(r"\.(s\d+p|ts)$", re.I)
_SNP_PORTS_RE = re.compile(r"\.s(\d+)p$", re.I)

# 参数名里出现这些词，且值是路径形态时，才当作"模型文件引用"。
# 刻意**不含** include/netlist —— 那是网表包含文件，不属于模型依赖，
# 混进来会让"无外部模型依赖"的判断失真。
_FILE_PARAM_RE = re.compile(
    r"(^|[^a-z])(file|filename|datafile|modelfile|model_file|"
    r"s\d*pfile|touchstone|snpfile|spath|modelpath)([^a-z]|$)", re.I)

# VAR 引用：$NAME / ${NAME}
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# 拼接式： "A" + "/" + "b.s2p"  /  A + '/b.s2p'
_CONCAT_SPLIT_RE = re.compile(r"\+")

STATE_NONE = "none"
STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_MISSING = "missing"

_HASH_CHUNK = 1 << 20          # 1 MiB：大文件分块读，绝不整体进内存
_VAR_DEPTH = 5                 # VAR 嵌套求值上限（防自引用死循环）
_MAX_SUBDESIGNS = 40           # 层次展开的子图上限（每个都要开设计，有成本）
_MAX_DEPTH = 3                 # 层次展开深度上限
_KIT_MAX_ROOTS = 2             # 一次最多给几个套件算保守指纹
_KIT_WALK_CAP = 20000          # 套件目录最多列举多少文件
_KIT_SAMPLE_FILES = 64         # 套件目录最多对多少个文件做内容哈希
_KIT_PRIORITY_NAMES = ("lib.defs", "ads.lib", "design_kit.cfg", "ads.lib.cfg")


# ---------------------------------------------------------------------------
# 值判断与 VAR 表达式
# ---------------------------------------------------------------------------

def is_model_file_value(value: str) -> bool:
    """这个字符串本身是不是模型文件名（``.sNp`` / ``.ts``，任意端口数）。"""
    return bool(MODEL_FILE_RE.search(str(value or "").strip().strip('"')))


def ports_of(value: str):
    """``.sNp`` 得出端口数；``.ts`` 端口数未知 -> None（不猜）。"""
    m = _SNP_PORTS_RE.search(str(value or "").strip().strip('"').lower())
    return int(m.group(1)) if m else None


def looks_like_model_param(key: str, value: str) -> bool:
    """这个 (参数名, 值) 组合是不是一条"模型文件引用"声明。

    两种情形认：
      1. 值本身就以模型文件后缀结尾（最常见的原厂 S 参数元件）；
      2. 参数名像文件参数，且值是路径形态（含分隔符或盘符）—— 这类值
         可能不带后缀（ADS 允许省略），交给 resolve 去按根搜索。

    值里有空白且没有分隔符的一律不认（"50 Ohm" 这种是数值，不是路径）。
    """
    text = str(value or "").strip().strip('"')
    if not text or len(text) > 512:
        return False
    if is_model_file_value(text):
        return True
    if not _FILE_PARAM_RE.search(str(key or "")):
        return False
    if "/" in text or "\\" in text or re.match(r"^[A-Za-z]:", text):
        return True
    return False


def expand_vars(value: str, var_table: dict | None, _depth: int = 0):
    """展开 VAR 表达式里的变量引用。

    支持三种写法（ADS 里都出现过）：

    * ``$MODEL_DIR/x.s2p`` 与 ``${MODEL_DIR}/x.s2p``
    * 拼接式 ``MODEL_DIR + "/x.s2p"``（ADS 的字符串拼接）
    * 整体就是变量名 ``File=$MODEL_FILE``

    返回 ``(展开后的字符串, 未解析的变量名列表)``。展开不了的变量**原样
    留在结果里**并返回名字 —— 调用方据此判 incomplete，绝不猜一个值。
    """
    text = str(value or "").strip()
    if not text:
        return "", []
    table = {str(k): str(v) for k, v in dict(var_table or {}).items()}
    if not table or _depth > _VAR_DEPTH:
        unresolved = [m for m in _VAR_REF_RE.findall(text) if m not in table] \
            if table else list(_VAR_REF_RE.findall(text))
        return text, unresolved

    unresolved: list = []

    def _lookup(name: str):
        if name in table:
            return _unquote(table[name])
        for key in table:
            if key.lower() == name.lower():
                return _unquote(table[key])
        return None

    def _sub(match):
        got = _lookup(match.group(1))
        if got is None:
            unresolved.append(match.group(1))
            return match.group(0)
        return got

    text = _VAR_REF_RE.sub(_sub, text)

    # 拼接式：分段求值，整段恰好是变量名的替换掉
    if _CONCAT_SPLIT_RE.search(text):
        parts = []
        for seg in _CONCAT_SPLIT_RE.split(text):
            seg = _unquote(seg)
            if not seg:
                continue
            got = _lookup(seg)
            if got is not None:
                parts.append(got)
            elif _VAR_REF_RE.search(seg):
                parts.append(seg)
            else:
                parts.append(seg)
        text = "".join(parts)

    if _VAR_REF_RE.search(text):
        # 可能是二次引用（变量的值里还有 $），再走一层
        deeper, more = expand_vars(text, table, _depth + 1)
        unresolved += more
        text = deeper
    # 去掉没解析出来的重复项，保持顺序
    seen, uniq = set(), []
    for name in unresolved:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    return text, uniq


def _unquote(value: str) -> str:
    """去掉一层配对引号。

    ADS 的路径表达式里两种引号都出现过：``MDIR + "/y.s2p"`` 与
    ``MDIR + '/y.s2p'``。只剥双引号会把单引号原样留在拼出来的路径里
    （``models'/y.s2p'``），于是文件明明存在却判定为"相对路径没解析出来"。
    """
    v = str(value or "").strip()
    for q in ('"', "'"):
        if len(v) >= 2 and v.startswith(q) and v.endswith(q):
            v = v[1:-1]
            break
    return v.strip()


def candidate_roots(workspace: str, extra_roots=(), library: str = "") -> list:
    """相对路径的候选根（按 ADS 的解析顺序试，第一个命中为准）。

    顺序（**不依赖进程 cwd** —— 后端与 ADS 的当前目录不是一回事，
    靠 cwd 解析会在换个启动方式时就静默错位）：

    1. 工作区根目录（ADS 里相对路径最常见的基准）；
    2. ``<工作区>/data``（部分套件把数据放在这里）；
    3. ``<工作区>/<库名>``（库内相对引用）；
    4. ``$HOME``（ADS 的 HOME 变量指向的用户目录）；
    5. 调用方显式给的搜索根（例如模型包解压目录）。

    ADS 的完整搜索规则与版本/设置有关，这里只列能**确定性复现**的几条；
    都没命中就如实记为"无法解析"，由调用方保守重跑 —— 好过猜一个路径
    然后按"没变"复用出错误结论。
    """
    roots: list = []
    ws = str(workspace or "").strip()
    if ws:
        roots.append(os.path.abspath(ws))
        roots.append(os.path.abspath(os.path.join(ws, "data")))
        if library:
            roots.append(os.path.abspath(os.path.join(ws, str(library))))
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if home:
        roots.append(os.path.abspath(home))
    for r in extra_roots or ():
        r = str(r or "").strip()
        if r:
            roots.append(os.path.abspath(r))
    seen, out = set(), []
    for r in roots:
        key = os.path.normcase(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def resolve_model_path(raw: str, workspace: str, var_table: dict | None = None,
                       extra_roots=(), library: str = ""):
    """把一条模型文件声明解析成盘上的真实路径。

    返回 dict：
        raw / expanded     原始值与 VAR 展开后的值；
        path               解析到的绝对路径（没找到为 ""）；
        resolved_by        absolute / workspace_relative / home_relative /
                           extra_root / ""（没解析出来）；
        unresolved_vars    展开不了的变量名；
        tried              试过的候选根（供人排查）；
        missing            绝对路径写对了但文件不存在（区别于"找不到"）。
    """
    expanded, unresolved = expand_vars(raw, var_table)
    text = expanded.strip().strip('"').strip()
    out = {
        "raw": str(raw or ""),
        "expanded": text,
        "path": "",
        "resolved_by": "",
        "unresolved_vars": unresolved,
        "tried": [],
        "missing": False,
    }
    if not text:
        out["resolved_by"] = ""
        return out

    if os.path.isabs(text):
        out["tried"].append(text)
        if os.path.isfile(text):
            out["path"] = os.path.abspath(text)
            out["resolved_by"] = "absolute"
        else:
            out["missing"] = True
        return out

    for root in candidate_roots(workspace, extra_roots, library):
        candidate = os.path.abspath(os.path.join(root, text))
        out["tried"].append(candidate)
        if os.path.isfile(candidate):
            out["path"] = candidate
            out["resolved_by"] = ("workspace_relative"
                                  if os.path.normcase(root) != os.path.normcase(
                                      os.path.abspath(os.environ.get("HOME")
                                                      or os.path.expanduser("~")))
                                  else "home_relative")
            if extra_roots and os.path.normcase(root) in {
                    os.path.normcase(os.path.abspath(str(r))) for r in extra_roots if r}:
                out["resolved_by"] = "extra_root"
            return out
    return out


# ---------------------------------------------------------------------------
# 分块哈希
# ---------------------------------------------------------------------------

def sha256_file(path: str, chunk: int = _HASH_CHUNK) -> dict:
    """分块读 + SHA-256。

    大文件（几十 MB 的多端口 S 参数）绝不整体进内存：按 chunk 逐块更新。
    同时带出 size 与 mtime_ns —— 大小/时间变了也可以作为"肯定变了"的
    快速判据（哈希才是判定的唯一依据，size/mtime 只用于展示与加速）。
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            size += len(block)
            digest.update(block)
    try:
        mtime_ns = int(os.stat(path).st_mtime_ns)
    except OSError:
        mtime_ns = None
    return {"sha256": digest.hexdigest(), "size": size, "mtime_ns": mtime_ns}


# ---------------------------------------------------------------------------
# 网表扫描（本次**实际**仿真网表的补充证据）
# ---------------------------------------------------------------------------

def scan_netlist_files(netlist_text: str) -> list:
    """从**本次仿真用的网表文本**里扫出模型文件引用。

    为什么还要扫网表：实例参数只覆盖"设计里写了什么"，而网表才是真正
    喂给 hpeesofsim 的东西 —— 层次展开、VAR 求值之后出现的路径只有网表里
    有。两边互为补充，重复的路径会合并（source 记为 both）。
    """
    out: list = []
    for token in re.findall(r'"([^"\r\n]+)"', str(netlist_text or "")):
        if is_model_file_value(token):
            out.append(token.strip())
    # 没加引号的裸路径（网表里也有这种写法）
    for token in re.findall(r"([\w./\\:$~-]+\.(?:s\d+p|ts))\b",
                            str(netlist_text or ""), re.I):
        out.append(token.strip())
    seen, uniq = set(), []
    for t in out:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


# ---------------------------------------------------------------------------
# 阶段一：收集"要哈希什么"（需要打开设计，跑在 ADS 主线程）
# ---------------------------------------------------------------------------

def _iter_model_params(instance: dict):
    """从一个实例字典里取出所有模型文件声明。"""
    name = str((instance or {}).get("name") or "")
    params = dict((instance or {}).get("params") or {})
    for key, value in params.items():
        if looks_like_model_param(key, value):
            yield name, str(key), str(value)


def collect_plan(instances: list, netlist_text: str = "", workspace: str = "",
                 var_table: dict | None = None, extra_roots=(),
                 library: str = "", library_roots: dict | None = None,
                 descend=None, allow_kit_fallback: bool = True,
                 ignored_cells=(), kit_libraries=()) -> dict:
    """阶段一：把设计引用到的模型文件解析成"待哈希清单"。

    ``instances``  : ``[{"name","master","params",...}]``（可用
                     ``ads_ops._rf_inventory`` 的输出，纯 dict，便于离线测试）
    ``var_table``  : 设计里的 VAR 变量表（``{名: 值}``）
    ``library_roots``: ``{库名: 盘上路径}`` —— 用于 Design Kit 套件级指纹
    ``descend``    : ``f(cell, library) -> {"instances": [...], "vars": {...},
                     "library": str} | None`` —— 层次展开回调（要开设计，
                     只能在主线程调）；返回 None 表示打不开（记为无法解析）

    返回的 plan 里**没有任何文件内容**，只有路径与"为什么没解析出来"。
    """
    var_table = {str(k): str(v) for k, v in dict(var_table or {}).items()}
    declared: dict = {}          # key -> 声明记录（去重合并来源）
    hierarchy = {"expanded": [], "failed": [], "truncated": False}
    notes: list = []

    def _add(raw, inst_name, param, origin, master="", lib=""):
        norm = str(raw or "").strip().strip('"')
        if not norm:
            return
        key = norm.lower()
        if key in declared:
            rec = declared[key]
            if origin not in rec["origins"]:
                rec["origins"].append(origin)
            if inst_name and inst_name not in rec["instances"]:
                rec["instances"].append(inst_name)
            return
        declared[key] = {
            "raw": norm, "param": param, "instances": [inst_name] if inst_name else [],
            "origins": [origin], "master": master, "library": lib,
        }

    # 1) 顶层实例参数
    scanned = 0
    for inst in list(instances or []):
        scanned += 1
        master = str((inst or {}).get("master") or "")
        for inst_name, key, value in _iter_model_params(inst):
            _add(value, inst_name, key, "instance_param", master, library)

    # 2) 层次展开（子图里引用模型是最容易漏的一类：顶层只有一个符号）
    visited: set = set()
    if callable(descend):
        pending: list = []
        for inst in list(instances or []):
            master = str((inst or {}).get("master") or "")
            if master and master not in ignored_cells:
                pending.append((master, library, 1))
        while pending:
            cell, lib, depth = pending.pop(0)
            if depth > _MAX_DEPTH or len(hierarchy["expanded"]) >= _MAX_SUBDESIGNS:
                hierarchy["truncated"] = True
                notes.append(
                    f"层次展开达到上限（最多 {_MAX_SUBDESIGNS} 个子图 / "
                    f"{_MAX_DEPTH} 层），更深的引用未检查 —— 按无法解析处理。")
                break
            visit_key = f"{lib}:{cell}".lower()
            if visit_key in visited:
                continue
            visited.add(visit_key)
            try:
                sub = descend(cell, lib)
            except Exception as e:  # noqa: BLE001 — 子图打不开要如实记账
                hierarchy["failed"].append(
                    {"cell": cell, "library": lib,
                     "reason": f"{type(e).__name__}: {e}"})
                continue
            if not sub:
                continue       # 不是层次元件（基本件），正常
            hierarchy["expanded"].append({"cell": cell, "library": lib,
                                          "depth": depth})
            sub_vars = {str(k): str(v)
                        for k, v in dict(sub.get("vars") or {}).items()}
            merged = dict(var_table)
            merged.update(sub_vars)
            sub_lib = str(sub.get("library") or lib or "")
            for sub_inst in list(sub.get("instances") or []):
                master = str((sub_inst or {}).get("master") or "")
                for inst_name, key, value in _iter_model_params(sub_inst):
                    _add(value, inst_name, key, "hierarchy", master, sub_lib)
                if master and master not in ignored_cells:
                    pending.append((master, sub_lib, depth + 1))
            # 子图自己的 VAR 也可能指向模型文件（路径型变量）
            for vname, vvalue in sub_vars.items():
                if looks_like_model_param(vname, vvalue):
                    _add(vvalue, "", f"VAR:{vname}", "hierarchy_var", "", sub_lib)

    # 3) 顶层 VAR 里的路径型变量
    for vname, vvalue in var_table.items():
        if looks_like_model_param(vname, vvalue):
            _add(vvalue, "", f"VAR:{vname}", "var", "", library)

    # 4) 本次实际仿真网表里出现的路径（互补证据）
    for token in scan_netlist_files(netlist_text):
        _add(token, "", "", "netlist")

    # 5) 解析成真实路径
    resolved, unresolved = [], []
    for rec in declared.values():
        info = resolve_model_path(rec["raw"], workspace, var_table,
                                  extra_roots, rec.get("library") or library)
        entry = {
            "raw": rec["raw"],
            "expanded": info["expanded"],
            "param": rec.get("param") or "",
            "instances": list(rec.get("instances") or []),
            "origins": list(rec.get("origins") or []),
            "master": rec.get("master") or "",
            "library": rec.get("library") or "",
            "path": info["path"],
            "resolved_by": info["resolved_by"],
            "ports": ports_of(rec["raw"]),
        }
        if info["path"]:
            resolved.append(entry)
        else:
            reason = "model_file_not_found"
            if info["missing"]:
                reason = "model_file_missing"
            if info["unresolved_vars"]:
                reason = "var_expression_unresolved"
                entry["unresolved_vars"] = info["unresolved_vars"]
            elif not info["path"] and not os.path.isabs(info["expanded"]):
                reason = "relative_path_unresolved"
            entry["reason"] = reason
            entry["tried"] = info["tried"][:8]
            unresolved.append(entry)

    # 6) Design Kit 套件级保守指纹
    #
    #    触发条件（**支持范围**就写在这里，不夸大）：
    #      * 元件能被归属到一个库（设计自身的库 / 层次子图带出的库 /
    #        实例字典里显式给的 library / 网表里 #load 出来的库名），且
    #      * 这个库在盘上有目录，且目录像 Design Kit（有 lib.defs / ads.lib
    #        / .atf / .ael）。
    #    **已知局限**：本版本 ADS 的 inst 只暴露 cell 名，拿不到"这个元件
    #    来自哪个库"，所以顶层直接放的套件元件若无法归属到库，就不在套件
    #    指纹的覆盖范围内 —— 这种情况既不假装"没依赖"，也不编一个指纹，
    #    只在 notes 里如实写明（调用方仍按"无法完整确认"处理）。
    kits: list = []
    if allow_kit_fallback and library_roots:
        libs_needing = sorted({str(r.get("library") or "")
                               for r in resolved + unresolved
                               if str(r.get("library") or "")}
                              | {str(x) for x in (kit_libraries or ()) if str(x)})
        for lib in libs_needing:
            root = (library_roots or {}).get(lib) or (library_roots or {}).get(
                lib.lower())
            if not root or not os.path.isdir(str(root)):
                continue
            if len(kits) >= _KIT_MAX_ROOTS:
                notes.append(
                    f"还有套件库（{lib}）需要保守指纹，但本次最多只处理 "
                    f"{_KIT_MAX_ROOTS} 个套件 —— 未处理的按无法解析计。")
                continue
            if not _looks_like_design_kit(root):
                continue
            kits.append({"library": lib, "root": os.path.abspath(str(root)),
                         "scope": "design_kit_suite"})

    return {
        "workspace": str(workspace or ""),
        "library": str(library or ""),
        "instances_scanned": scanned,
        "declared": sorted(declared.values(), key=lambda r: r["raw"].lower()),
        "resolved": resolved,
        "unresolved": unresolved,
        "kits": kits,
        "hierarchy": hierarchy,
        "notes": notes,
    }


def _looks_like_design_kit(root: str) -> bool:
    """目录像不像一个 Design Kit 套件根（有 lib.defs / ads.lib / .atf/.ael）。"""
    try:
        names = [n.lower() for n in os.listdir(root)]
    except OSError:
        return False
    if any(n in names for n in ("lib.defs", "ads.lib")):
        return True
    if any(n.endswith((".atf", ".ael")) for n in names):
        return True
    for sub in ("design_kit", "ads"):
        sub_path = os.path.join(root, sub)
        if os.path.isdir(sub_path):
            try:
                if any(n.lower() in ("lib.defs", "ads.lib")
                       for n in os.listdir(sub_path)):
                    return True
            except OSError:
                pass
    return False


# ---------------------------------------------------------------------------
# Design Kit 套件级保守指纹
# ---------------------------------------------------------------------------

def kit_fingerprint(root: str, walk_cap: int = _KIT_WALK_CAP,
                    sample: int = _KIT_SAMPLE_FILES) -> dict:
    """整个套件目录的**保守**指纹。

    支持范围与成本（必须写清楚，不能悄悄用）
    ------------------------------------------
    * 能发现：套件目录里**任何**文件的新增 / 删除 / 内容变化 / 大小变化 /
      修改时间变化 —— 所以它是**保守**的：套件里改一个无关文件也会让指纹
      变，结果是"多跑一次仿真"，不会漏掉真变化。
    * 不能发现：定位不到具体是哪个元件/哪个模型变了；套件外（例如全局
      Favorite Design Kit 里的同名库）的变化也不在视野内。
    * 成本：要遍历整个套件目录取 ``(相对路径, 大小, mtime_ns)``（万级文件
      约秒级，取决于磁盘），并额外对最多 ``sample`` 个文件做分块内容哈希。
      ``sample`` 取不到全部，所以**它不是完整性校验**，是"整体是否动过"的
      探测器；超过 ``walk_cap`` 的文件不列举，此时 ``truncated=True``。

    指纹 = sha256(清单文本) 与 sha256(抽样文件内容) 的组合，两者都在。
    """
    root = os.path.abspath(str(root or ""))
    entries: list = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            absolute = os.path.join(dirpath, name)
            try:
                st = os.stat(absolute)
            except OSError:
                continue
            rel = os.path.relpath(absolute, root).replace(os.sep, "/")
            entries.append((rel, int(st.st_size), int(getattr(st, "st_mtime_ns", 0))))
            if len(entries) >= walk_cap:
                truncated = True
                break
        if truncated:
            break
    entries.sort()

    manifest = "\n".join(f"{rel}|{size}|{mtime}" for rel, size, mtime in entries)
    manifest_sha = hashlib.sha256(manifest.encode("utf-8", "replace")).hexdigest()

    # 抽样：优先套件定义文件，其余按相对路径均匀取，保证确定性
    priority = [e for e in entries
                if os.path.basename(e[0]).lower() in _KIT_PRIORITY_NAMES]
    rest = [e for e in entries if e not in priority]
    if len(rest) > max(0, sample - len(priority)):
        stride = max(1, len(rest) // max(1, sample - len(priority)))
        rest = rest[::stride][:max(0, sample - len(priority))]
    chosen = (priority + rest)[:sample]

    content = hashlib.sha256()
    bytes_hashed = 0
    hashed: list = []
    for rel, _size, _mt in chosen:
        absolute = os.path.join(root, rel)
        try:
            info = sha256_file(absolute)
        except OSError:
            continue
        content.update(f"{rel}|{info['sha256']}\n".encode("utf-8", "replace"))
        bytes_hashed += int(info["size"])
        hashed.append(rel)

    return {
        "scope": "design_kit_suite",
        "root": root,
        "fingerprint": hashlib.sha256(
            (manifest_sha + content.hexdigest()).encode("utf-8")
        ).hexdigest()[:32],
        "manifest_sha256": manifest_sha[:32],
        "files_total": len(entries),
        "files_hashed": len(hashed),
        "bytes_hashed": bytes_hashed,
        "truncated": truncated,
        "sampled": hashed[:10],
        "cost_note": (
            f"套件级保守指纹：遍历 {len(entries)} 个文件"
            f"{'（已达上限，未列全）' if truncated else ''}，"
            f"对其中的 {len(hashed)} 个做了内容哈希。"
            "它能发现套件内任何文件变化，但定位不到具体元件；"
            "套件里改无关文件也会让它变化（保守，只会多跑仿真）。"
        ),
    }


# ---------------------------------------------------------------------------
# 阶段二：哈希（不碰 DE 对象，可后台执行）
# ---------------------------------------------------------------------------

def finalize(plan: dict) -> dict:
    """把 plan 变成最终证据：算哈希、判四态、汇总成一个指纹。

    返回结构（会被 run_simulation 原样带回后端并持久化）：::

        {
          "state": none|complete|incomplete|missing,
          "fingerprint": "…",           # 整体指纹（canonical 序列化后 sha256）
          "n_deps": 2,
          "deps":      [ {path, sha256, size, ports, instances, origins, …} ],
          "unresolved":[ {raw, reason, tried} ],
          "missing":   [ {raw, path, reason} ],
          "kits":      [ {library, root, fingerprint, cost_note, …} ],
          "hierarchy": {"expanded": [...], "failed": [...]},
          "notes":     [...],
          "model_fingerprint": {路径: sha256, "kit:<root>": 指纹}   # 兼容旧字段
        }
    """
    plan = plan or {}
    deps: list = []
    missing: list = []
    unresolved: list = []

    for entry in list(plan.get("resolved") or []):
        path = entry.get("path") or ""
        try:
            info = sha256_file(path)
        except OSError as e:
            rec = dict(entry)
            rec["reason"] = f"unreadable:{type(e).__name__}"
            unresolved.append(rec)
            continue
        rec = dict(entry)
        rec["sha256"] = info["sha256"]
        rec["size"] = info["size"]
        rec["mtime_ns"] = info["mtime_ns"]
        rec["package_id"] = _package_id_from_path(path)
        rec["variant"] = _variant_from_path(path)
        deps.append(rec)

    for entry in list(plan.get("unresolved") or []):
        rec = dict(entry)
        if rec.get("reason") == "model_file_missing":
            missing.append(rec)
        else:
            unresolved.append(rec)

    kits = []
    for kit in list(plan.get("kits") or []):
        try:
            info = kit_fingerprint(kit.get("root") or "")
        except OSError as e:
            unresolved.append({
                "raw": kit.get("root") or "", "param": "", "instances": [],
                "origins": ["design_kit"], "library": kit.get("library") or "",
                "reason": f"kit_unreadable:{type(e).__name__}",
            })
            continue
        merged = dict(kit)
        merged.update(info)
        kits.append(merged)

    # 兼容旧字段：路径 -> 内容哈希（旧结果复用逻辑吃这个）
    legacy = {}
    for d in deps:
        legacy[d.get("path") or ""] = d.get("sha256") or ""
    for k in kits:
        legacy[f"kit:{k.get('root') or ''}"] = k.get("fingerprint") or ""

    if not deps and not unresolved and not missing and not kits:
        state = STATE_NONE
    elif unresolved or missing or kits:
        # 套件指纹是"保守但不精确"，归 incomplete（能发现变化，但不是
        # 逐文件确认）；缺失与无法解析更是必须保守。
        state = STATE_MISSING if (missing and not unresolved and not kits) \
            else STATE_INCOMPLETE
    else:
        state = STATE_COMPLETE

    canonical = "\n".join(
        sorted(f"{d.get('path')}|{d.get('sha256')}|{d.get('size')}" for d in deps)
        + sorted(f"{k.get('root')}|{k.get('fingerprint')}" for k in kits)
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16] \
        if canonical else ""

    notes = list(plan.get("notes") or [])
    if kits:
        notes.append("本设计引用了 Design Kit 元件，模型依赖只能用套件级"
                     "保守指纹（见 kits 里的 cost_note），按无法完整解析处理。")
    if missing:
        notes.append(f"有 {len(missing)} 个模型文件在盘上找不到（依赖缺失）。")
    if unresolved:
        notes.append(f"有 {len(unresolved)} 项模型依赖无法解析到具体文件。")

    return {
        "state": state,
        "fingerprint": fingerprint,
        "n_deps": len(deps),
        "deps": deps,
        "unresolved": unresolved,
        "missing": missing,
        "kits": kits,
        "hierarchy": plan.get("hierarchy") or {},
        "instances_scanned": plan.get("instances_scanned", 0),
        "workspace": plan.get("workspace", ""),
        "notes": notes,
        "model_fingerprint": legacy,
    }


def _package_id_from_path(path: str) -> str:
    """从盘上路径反推 package_id（若它来自本插件导入的模型包）。

    布局是 ``<workspace>/ads_agent_models/extracted/<package_id>/…``
    （与 backend/model_store 一致）。认不出来就返回 "" —— 猜一个
    package_id 比说"不知道"危险。
    """
    parts = str(path or "").replace("\\", "/").split("/")
    for i in range(len(parts) - 2):
        if parts[i] == "ads_agent_models" and parts[i + 1] == "extracted":
            return parts[i + 2] if i + 2 < len(parts) else ""
    return ""


def _variant_from_path(path: str) -> str:
    """型号变体记号：文件名去掉目录与扩展名（含偏置/温度等原厂写法）。"""
    base = os.path.basename(str(path or ""))
    return os.path.splitext(base)[0]


# ---------------------------------------------------------------------------
# 汇总展示（给结果页 / LLM 看的人话）
# ---------------------------------------------------------------------------

def describe(evidence: dict) -> str:
    """把证据压成一句人话（结果页与对话里展示用）。"""
    ev = evidence or {}
    state = ev.get("state") or ""
    if state == STATE_NONE:
        return "本次仿真没有引用外部模型文件（网表指纹已覆盖全部输入）"
    if state == STATE_COMPLETE:
        return (f"引用 {ev.get('n_deps', 0)} 个模型文件，已全部解析并核对内容哈希"
                f"（指纹 {ev.get('fingerprint') or '—'}）")
    if state == STATE_MISSING:
        return (f"模型依赖缺失：{len(ev.get('missing') or [])} 个文件在盘上找不到，"
                f"结果不能复用")
    return (f"模型依赖无法完整确认（{len(ev.get('unresolved') or [])} 项未解析、"
            f"{len(ev.get('kits') or [])} 个套件用保守指纹），按保守策略处理")
