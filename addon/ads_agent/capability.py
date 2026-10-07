"""运行时能力检测与工具门禁 —— 只读探测，统一三态，绝不掩盖不兼容。

与 :mod:`adscompat`（backend/，官方证据档案）的分工：

* **adscompat** 回答"官方文档/实机基线对这个年份说了什么"（静态证据）；
* **capability** 回答"当前这个 ADS 进程里接口到底在不在"（动态事实）。

两者的交集决定一个工具开不开：任何一边说"不行"，工具就不开放。
这也是"不能用宽泛异常捕获掩盖接口不兼容"的落点：

* 探测只捕 ``ImportError`` / ``AttributeError`` 这类**定位失败**，
  探测结果本身就是要拿到的信息，不是被掩盖的错误；
* 状态只有三态：``supported`` / ``unavailable`` / ``unknown``。
  "无法在不实际调用的前提下确认"一律 ``unknown``，门禁按不可用处理；
* 探测**只读**：只做 import、hasattr、callable 检查，绝不打开设计、
  绝不写库、绝不触发仿真。

线程约定：首次 ``detect()`` 在 Qt 主线程执行（toolserver 的 pump 里），
之后 HTTP 线程只读缓存快照。
"""

from __future__ import annotations

import configparser
import os
import sys
import threading
import time

import pathbridge

#: 能力三态
SUPPORTED = "supported"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"

_CACHE = {"snapshot": None, "at": 0.0}
_LOCK = threading.Lock()
#: 快照有效期（秒）。过期后下一次 pump 会重新探测（仍然只读）。
_TTL_SECONDS = 600.0


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def _probe_de() -> dict:
    """keysight.ads.de 及其关键符号。只 import + hasattr。"""
    out = {}
    try:
        import keysight.ads.de as de  # type: ignore
    except ImportError as e:
        return {"de_module": (UNAVAILABLE, f"keysight.ads.de 导入失败: {e}")}
    out["de_module"] = (SUPPORTED, "")
    for attr, key in (
        ("workspace_is_open", "de_workspace_is_open"),
        ("active_workspace", "de_active_workspace"),
    ):
        out[key] = ((SUPPORTED, "") if callable(getattr(de, attr, None))
                    else (UNAVAILABLE, f"keysight.ads.de 缺少 {attr}"))
    # 自动化模式查询 API：2025 起官方用 is_pde_app；2024 U2 及更早用
    # running_automation（官方发行说明记载的更名）。两者有其一即可。
    if callable(getattr(de, "is_pde_app", None)):
        out["automation_query"] = (SUPPORTED, "is_pde_app")
    elif callable(getattr(de, "running_automation", None)):
        out["automation_query"] = (SUPPORTED, "running_automation")
    else:
        out["automation_query"] = (UNKNOWN, "is_pde_app / running_automation 均不存在")
    # 私有模块的多路径探测（2027 实测它们的位置在不同构建可能漂移）
    out["design_mode_enum"] = _probe_design_mode(de)
    out["geometry_types"] = _probe_geometry_types(de)
    return out


def _probe_design_mode(de) -> tuple:
    """DesignMode 枚举来源：db_uu 或 de._pde.db；语义不由这里推断。"""
    try:
        import keysight.ads.de.db_uu as db_uu  # type: ignore
    except ImportError:
        db_uu = None
    if db_uu is not None and getattr(db_uu, "DesignMode", None) is not None:
        return SUPPORTED, "keysight.ads.de.db_uu.DesignMode"
    try:
        from keysight.ads.de._pde import db as pde_db  # type: ignore
    except ImportError:
        pde_db = None
    if pde_db is not None and getattr(pde_db, "DesignMode", None) is not None:
        return SUPPORTED, "keysight.ads.de._pde.db.DesignMode"
    return UNKNOWN, "DesignMode 枚举在 db_uu / _pde.db 均未找到"


def _probe_geometry_types(de) -> tuple:
    """Orientation / PointF（自动布局与标注依赖；缺失则相应能力禁用）。"""
    ok_orient = ok_point = False
    try:
        from keysight.ads.de._pde import db as pde_db  # type: ignore

        ok_orient = getattr(pde_db, "Orientation", None) is not None
    except ImportError:
        pass
    if not ok_orient and getattr(de, "_pde", None) is not None:
        ok_orient = getattr(getattr(de._pde, "db", None), "Orientation", None) is not None
    try:
        import keysight.ads.de._points as points  # type: ignore

        ok_point = getattr(points, "PointF", None) is not None
    except ImportError:
        pass
    if ok_orient and ok_point:
        return SUPPORTED, ""
    if not ok_orient and not ok_point:
        return UNKNOWN, "Orientation / PointF 均未找到（几何常量不作跨版本假设）"
    return UNKNOWN, ("部分几何类型缺失："
                     + ("Orientation" if not ok_orient else "PointF"))


def _probe_app() -> dict:
    """keysight.ads.de.app —— 插件契约与面板嵌入依赖的符号。"""
    out = {}
    try:
        import keysight.ads.de.app as app  # type: ignore
    except ImportError as e:
        out["de_app"] = (UNAVAILABLE, f"keysight.ads.de.app 导入失败: {e}")
        return out
    out["de_app"] = (SUPPORTED, "")
    needed = {
        "WindowType": "app.WindowType",
        "Menu": "app.Menu",
        "Action": "app.Action",
        "find_addon": "app.find_addon",
    }
    missing = [label for attr, label in needed.items()
               if getattr(app, attr, None) is None]
    if missing:
        out["addon_menu_api"] = (UNAVAILABLE, "缺少 " + ", ".join(missing))
    else:
        out["addon_menu_api"] = (SUPPORTED, "")
    try:
        from keysight.ads.de.app import window as app_window  # type: ignore

        has_host = hasattr(app_window, "main_pyside_widget")
    except ImportError:
        has_host = False
    out["panel_embedding"] = (
        (SUPPORTED, "") if has_host
        else (UNKNOWN, "keysight.ads.de.app.window.main_pyside_widget 不存在"
                       "（面板将退化为独立窗口，或不可用）")
    )
    return out


def _probe_dataset() -> dict:
    try:
        import keysight.ads.dataset as dataset  # type: ignore
    except ImportError as e:
        return {"dataset_module": (UNAVAILABLE, f"keysight.ads.dataset 导入失败: {e}")}
    if callable(getattr(dataset, "open", None)):
        return {"dataset_module": (SUPPORTED, "")}
    return {"dataset_module": (UNKNOWN, "keysight.ads.dataset.open 不存在")}


def _probe_simulator() -> dict:
    try:
        from keysight.edatoolbox import ads as eda  # type: ignore
    except ImportError as e:
        return {"circuit_simulator": (UNAVAILABLE,
                                      f"keysight.edatoolbox.ads 导入失败: {e}")}
    if getattr(eda, "CircuitSimulator", None) is not None:
        return {"circuit_simulator": (SUPPORTED, "")}
    return {"circuit_simulator": (UNKNOWN, "CircuitSimulator 类不存在")}


def _probe_qt() -> dict:
    import qtcompat

    try:
        ns = qtcompat.load()
    except qtcompat.QtBindingUnavailable as e:
        return {"qt_binding": (UNAVAILABLE, str(e))}
    return {"qt_binding": (SUPPORTED, ns.binding)}


def detect() -> dict:
    """完整能力快照。全部探测均为只读；返回 JSON 友好 dict。"""
    caps: dict = {}
    caps.update(_probe_qt())
    caps.update(_probe_de())
    caps.update(_probe_app())
    caps.update(_probe_dataset())
    caps.update(_probe_simulator())

    version = {"year": None, "update": "", "build": "", "status": "unknown"}
    try:
        compat = pathbridge.load_backend_module("adscompat.py", "adscompat")
        hpeesof = (os.environ.get("HPEESOF_DIR") or "").strip().strip('"')
        version = compat.detect_version(hpeesof) if hpeesof else version
    except Exception as e:  # noqa: BLE001 — 识别失败按未知处理，不吞掉原因
        version = {"year": None, "update": "", "build": "",
                   "status": "unknown", "error": f"{type(e).__name__}: {e}"}

    flags = read_compat_config()

    inside = False
    try:
        adslocate = pathbridge.load_backend_module("adslocate.py", "adslocate")
        inside = bool(adslocate.inside_ads())
    except Exception:  # noqa: BLE001
        inside = False

    snap = {
        "inside_ads": inside,
        "python": ".".join(str(p) for p in sys.version_info[:3]),
        "platform": sys.platform,
        "python_bitness": __import__("struct").calcsize("P") * 8,
        "ads_version": {k: v for k, v in version.items() if k != "profile"},
        "compat_flags": flags,
        "capabilities": {k: {"status": v[0], "reason": v[1]} for k, v in caps.items()},
    }
    # 逐工具门禁结果：后端据此过滤 LLM 工具列表（与 pump 派发前同一裁决）
    tools_decisions = {}
    for tool in TOOL_REQUIREMENTS:
        tools_decisions[tool] = tool_decision(snap, tool)
    snap["tools"] = tools_decisions
    return snap


# ---------------------------------------------------------------------------
# 工具 → 依赖能力
# ---------------------------------------------------------------------------

#: 每个工具依赖的能力清单。读工具依赖 DE 数据访问；数据集工具依赖
#: dataset 模块；仿真依赖 CircuitSimulator。缺任何一项即不可用。
TOOL_REQUIREMENTS = {
    "get_workspace_info": ("de_module", "de_workspace_is_open", "de_active_workspace"),
    "list_designs": ("de_module", "de_active_workspace"),
    "get_design_variables": ("de_module", "de_active_workspace", "design_mode_enum"),
    "set_design_variables": ("de_module", "de_active_workspace", "design_mode_enum"),
    "build_schematic": ("de_module", "de_active_workspace", "design_mode_enum"),
    "check_connections": ("de_module", "de_active_workspace", "design_mode_enum"),
    "audit_rf": ("de_module", "de_active_workspace", "design_mode_enum"),
    "open_schematic": ("de_module", "de_active_workspace", "design_mode_enum"),
    "design_fingerprint": ("de_module", "de_active_workspace", "design_mode_enum"),
    "read_dataset": ("dataset_module",),
    "read_traces": ("dataset_module",),
    "run_simulation": ("de_module", "circuit_simulator"),
    "run_python": ("de_module",),
}


def _cap_ok(snapshot: dict, name: str) -> tuple:
    entry = snapshot.get("capabilities", {}).get(name)
    if entry is None:
        return False, f"能力 {name} 未探测（unknown 按不可用处理）"
    if entry["status"] == SUPPORTED:
        return True, ""
    if entry["status"] == UNKNOWN:
        return False, entry["reason"] or f"能力 {name} 状态未知"
    return False, entry["reason"] or f"能力 {name} 不可用"


def tool_decision(snapshot: dict, tool: str) -> dict:
    """对快照裁决单个工具 → ``{"allowed", "code", "reason"}``。"""
    try:
        compat = pathbridge.load_backend_module("adscompat.py", "adscompat")
    except Exception as e:  # noqa: BLE001
        return {"allowed": False, "code": "capability_missing",
                "reason": f"兼容档案模块不可用: {type(e).__name__}: {e}"}

    # 不在依赖表里的工具（后端本地工具等）不经过 ADS 门禁
    if tool not in TOOL_REQUIREMENTS:
        return {"allowed": True, "code": "", "reason": ""}

    missing = []
    for cap in TOOL_REQUIREMENTS.get(tool, ()):
        ok, why = _cap_ok(snapshot, cap)
        if not ok:
            missing.append(why)
    capability_ok = not missing

    version = snapshot.get("ads_version") or {}
    flags = snapshot.get("compat_flags") or {}
    year = version.get("year")
    exp = False
    if isinstance(year, int):
        exp = bool(flags.get(compat.experimental_flag_key(year), False))
        if year in compat.VERIFIED_YEARS:
            exp = True
    decision = compat.gating_decision(
        tool=tool,
        year_status=str(version.get("status") or "unknown"),
        year=year,
        capability_ok=capability_ok,
        experimental_enabled=exp,
        allow_unknown=bool(flags.get("allow_unknown_version", False)),
    )
    if not decision.get("allowed") and missing:
        decision = dict(decision)
        decision["reason"] = decision["reason"] + "；缺失能力：" + "；".join(missing)
    return decision


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def read_compat_config() -> dict:
    """读 config.ini [compat] 节。缺省全关（保守）。"""
    defaults = {
        "allow_unknown_version": False,
        "experimental_2024": False,
        "experimental_2025": False,
        "experimental_2026": False,
    }
    try:
        paths = pathbridge.load()
        cfg_path = paths.config_path()
    except Exception:  # noqa: BLE001
        return defaults
    parser = configparser.ConfigParser()
    try:
        if cfg_path and os.path.exists(cfg_path):
            parser.read(cfg_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return defaults
    if not parser.has_section("compat"):
        return defaults

    def getb(key: str) -> bool:
        raw = parser.get("compat", key, fallback="").strip().lower()
        return raw in ("1", "true", "yes", "on")

    out = dict(defaults)
    out["allow_unknown_version"] = getb("allow_unknown_version")
    for year in (2024, 2025, 2026):
        key = f"experimental_{year}"
        out[key] = getb(key)
    return out


# ---------------------------------------------------------------------------
# 对外接口（toolserver / 面板用）
# ---------------------------------------------------------------------------

def snapshot(max_age: float = _TTL_SECONDS) -> dict:
    """带缓存的快照。``max_age`` 内复用；探测线程安全。"""
    with _LOCK:
        now = time.time()
        if _CACHE["snapshot"] is not None and now - _CACHE["at"] <= max_age:
            return _CACHE["snapshot"]
        snap = detect()
        _CACHE["snapshot"] = snap
        _CACHE["at"] = now
        return snap


def invalidate() -> None:
    """强制下次重新探测（例如用户改了 config.ini 之后）。"""
    with _LOCK:
        _CACHE["snapshot"] = None
        _CACHE["at"] = 0.0


def gate_tool(tool: str) -> dict:
    """toolserver pump 在派发前调用的门禁。返回 decision + 快照引用。"""
    snap = snapshot()
    decision = tool_decision(snap, tool)
    decision["compat_summary"] = summarize(snap)
    return decision


def summarize(snapshot_data: dict | None = None) -> str:
    """一行人话摘要（/health、日志、面板横幅用）。"""
    snap = snapshot_data or snapshot()
    version = snap.get("ads_version") or {}
    year = version.get("year")
    binding = (snap.get("capabilities", {}).get("qt_binding") or {}).get("reason") \
        or (snap.get("capabilities", {}).get("qt_binding") or {}).get("status")
    status = version.get("status") or "unknown"
    label = f"ADS {year}" if year else "ADS 未知版本"
    tag = {"verified": "已实机验证基线", "known": "已知版本", "unknown": "未知版本"}.get(
        "verified" if status == "known" and year in _verified_years() else
        ("known" if status == "known" else "unknown"), status)
    return f"{label} (Update={version.get('update') or '?'} build={version.get('build') or '?'}), Qt={binding}, {tag}"


def _verified_years() -> frozenset:
    try:
        compat = pathbridge.load_backend_module("adscompat.py", "adscompat")
        return compat.VERIFIED_YEARS
    except Exception:  # noqa: BLE001
        return frozenset()
