"""Qt 绑定适配层 —— PySide6 / PySide2 二选一，绝不混用。

为什么需要它
------------
官方发行说明（docs/版本证据报告.md）确认：

* ADS 2024 / 2025 的 Python 插件使用 **PySide2**（2024 U2.0 与 2025 发行
  说明的 DDS Python Addon 条目）；
* ADS 2026 起 "**PySide2 has been upgraded to PySide6**"（2026 发行说明），
  2027 延续 PySide6（1.0.1 实机基线）。

因此本插件不能像旧版那样写死 ``from PySide6...``：在 2024/2025 的 ADS 进程
里那个 import 会直接失败，或更糟——把另一套 Qt 绑定拉进同一个进程。
本模块的规则：

1. **进程内已加载哪个绑定就用哪个**（查 ``sys.modules``）——ADS 主窗口早已
   初始化，跟着宿主走永远不会错；
2. 都没加载时，按官方档案指向的绑定顺序尝试（未知环境先 PySide6 后
   PySide2）；
3. **选定后缓存，终身不复选**：同一进程混用两套 Qt 绑定是禁止项；
4. 一个都没有时返回 ``None`` 并给出原因——调用方走降级路径（菜单提示 /
   纯文本诊断），**不能让整个插件加载崩溃**。

差异 shim（只 shim 实际用到的）：

* ``QFontDatabase.families()`` —— Qt5 里是实例方法，Qt6 里是静态方法；
* 对话框 ``exec`` —— PySide2 用 ``exec_``，PySide6 用 ``exec``。

其余控件（QDockWidget/QThread/Signal/QPainter 等）两类绑定签名一致，
直接透传，不做多余包装。
"""

from __future__ import annotations

import os
import sys

#: 选定后的绑定名（"PySide6" / "PySide2" / None）——进程内只允许设置一次。
_BINDING: str | None = None
_RESOLVED: bool = False
#: load() 失败时的原因（诊断页展示）。
_LAST_ERROR: str = ""

#: 已探测到的 Qt 模块缓存
_qt = {"QtCore": None, "QtGui": None, "QtWidgets": None}


class QtBindingUnavailable(ImportError):
    """ADS 进程内没有任何可用的 PySide 绑定。"""


def _loaded_binding() -> str | None:
    """ADS 已经加载进 sys.modules 的绑定（最可信的信号）。"""
    for name in ("PySide6", "PySide2"):
        mod = sys.modules.get(name)
        if mod is not None:
            return name
    return None


def _preferred_order() -> tuple:
    """尝试顺序：官方档案优先，无证据时 PySide6 在前（2026+ 的现实）。"""
    expected = None
    try:
        # 不强依赖 pathbridge/adscompat —— 拿不到就用默认顺序
        import pathbridge

        compat = pathbridge.load_backend_module("adscompat.py", "adscompat")
        hpeesof = (os.environ.get("HPEESOF_DIR") or "").strip()
        year = None
        if hpeesof and os.path.isdir(hpeesof):
            year = (compat.detect_version(hpeesof) or {}).get("year")
        if year is None:
            # 启动早期 HPEESOF_DIR 可能未设；ADS 进程内该变量通常已存在
            year = None
        expected = compat.expected_qt_binding(year)
    except Exception:  # noqa: BLE001 — 探测失败不阻塞绑定选择
        expected = None
    if expected == "PySide2":
        return ("PySide2", "PySide6")
    return ("PySide6", "PySide2")


def _import_binding(name: str) -> bool:
    """导入一个绑定的三个模块；只捕 ImportError/AttributeError，
    让"绑定坏了"与"绑定没有"都能被如实话报。"""
    try:
        QtCore = __import__(name + ".QtCore", fromlist=["QtCore"])
        QtGui = __import__(name + ".QtGui", fromlist=["QtGui"])
        QtWidgets = __import__(name + ".QtWidgets", fromlist=["QtWidgets"])
    except (ImportError, AttributeError) as e:
        global _LAST_ERROR
        _LAST_ERROR = f"{name} 导入失败: {type(e).__name__}: {e}"
        return False
    _qt["QtCore"] = QtCore
    _qt["QtGui"] = QtGui
    _qt["QtWidgets"] = QtWidgets
    return True


def binding_name() -> str | None:
    """当前选定的绑定名；未成功加载返回 None。"""
    return _BINDING


def last_error() -> str:
    """最近一次绑定选择失败的原因（诊断信息）。"""
    return _LAST_ERROR


def load(force_retry: bool = False):
    """选定并返回 Qt 命名空间；失败抛 :class:`QtBindingUnavailable`。

    返回一个简单命名空间：``QtCore`` / ``QtGui`` / ``QtWidgets`` / ``binding``。
    成功一次后终身缓存（``force_retry`` 仅供测试使用）。
    """
    global _BINDING, _RESOLVED
    if _RESOLVED and not force_retry:
        if _BINDING is None:
            raise QtBindingUnavailable(_LAST_ERROR or "没有可用的 PySide 绑定")
        return _Namespace(_qt, _BINDING)

    _RESOLVED = True
    already = _loaded_binding()
    order = (already,) if already else _preferred_order()
    for name in order:
        if name and _import_binding(name):
            _BINDING = name
            return _Namespace(_qt, name)
    _BINDING = None
    raise QtBindingUnavailable(
        _LAST_ERROR or "ADS 进程内既没有 PySide6 也没有 PySide2 可导入"
    )


def _get(section: str):
    """已加载命名空间里的模块；load() 未成功时抛错。"""
    mod = _qt.get(section)
    if mod is None:
        load()
    return _qt[section]


#: 公开取用函数：``qtcompat.QtWidgets()`` 等。load() 未成功时抛
#: :class:`QtBindingUnavailable`，调用方据此走降级路径。
def QtCore():
    return _get("QtCore")


def QtGui():
    return _get("QtGui")


def QtWidgets():
    return _get("QtWidgets")


class _Namespace:
    """load() 返回的只读命名空间。"""

    __slots__ = ("QtCore", "QtGui", "QtWidgets", "binding")

    def __init__(self, qt: dict, binding: str):
        self.QtCore = qt["QtCore"]
        self.QtGui = qt["QtGui"]
        self.QtWidgets = qt["QtWidgets"]
        self.binding = binding


# ---------------------------------------------------------------------------
# 差异 shim
# ---------------------------------------------------------------------------

def font_families():
    """跨版本的 ``QFontDatabase`` 字体族列表（Qt5 需实例化，Qt6 是静态）。"""
    QtGui = _get("QtGui")
    db = QtGui.QFontDatabase
    try:
        return list(db.families())
    except TypeError:
        return list(db().families())


def dialog_exec(dialog) -> int:
    """跨版本的对话框 exec（PySide2 只有 exec_，PySide6 用 exec）。"""
    fn = getattr(dialog, "exec", None)
    if fn is None:
        fn = getattr(dialog, "exec_", None)
    if fn is None:
        raise AttributeError("对话框既没有 exec 也没有 exec_")
    return fn()


def is_qt_object(obj) -> bool:
    """obj 是否为当前绑定下的 QObject（用于验证宿主窗口类型）。"""
    try:
        QtCore = _get("QtCore")
    except QtBindingUnavailable:
        return False
    return isinstance(obj, QtCore.QObject)
