"""Qt 绑定适配（qtcompat.py）—— 用假 PySide2/PySide6 模块做契约测试。

核心契约：
  * 进程内**已加载**的绑定优先（跟着宿主走，绝不引入第二套 Qt）；
  * 选定后终身缓存，同进程内不允许混用两种绑定；
  * 差异 shim：QFontDatabase.families（Qt5 实例 / Qt6 静态）、dialog exec_；
  * 两个绑定都没有 → 明确报错（QtBindingUnavailable），调用方走降级。

运行::

    python tests/test_qtcompat.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, raises, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

import qtcompat  # noqa: E402


def _mk(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class _FakeQtModule:
    """行为像 Qt 模块的最小对象（属性访问透传）。"""

    def __init__(self, name: str, attrs: dict):
        self._name = name
        self.__dict__.update(attrs)

    def __getattr__(self, item):
        raise AttributeError(f"{self._name} 没有 {item}")


def install_fake_binding(name: str, *, loaded: bool, families_static: bool) -> dict:
    """搭一棵假的 PySideX 模块树。返回（模块名 -> 模块）以便清理。"""
    class QFontDatabase:
        @staticmethod
        def families():  # Qt6 形态
            return ["Arial", "Microsoft YaHei"]

    if not families_static:
        # Qt5/PySide2 形态：需要实例化
        QFontDatabase.families = None  # 静态属性不存在

        class QFontDatabaseInst(QFontDatabase):
            def __init__(self):
                pass

            def families(self):
                return ["SimSun"]

        QFontDatabase = QFontDatabaseInst

    QtCore = _mk(f"{name}.QtCore", QTimer=lambda: None, Qt=type("Qt", (), {}))
    QtGui = _mk(f"{name}.QtGui", QFontDatabase=QFontDatabase, QColor=object)
    QtWidgets = _mk(f"{name}.QtWidgets", QApplication=object, QMessageBox=object)

    class _Dialog:
        if name == "PySide2":
            def exec_(self):
                return 0
        else:
            def exec(self):
                return 0

    QtWidgets.QDialog = _Dialog

    mods = {
        name: _mk(name),
        f"{name}.QtCore": QtCore,
        f"{name}.QtGui": QtGui,
        f"{name}.QtWidgets": QtWidgets,
    }
    if loaded:
        sys.modules.update(mods)  # 模拟"ADS 已经加载了这套绑定"
    return mods


def cleanup(mods: dict) -> None:
    for name in mods:
        sys.modules.pop(name, None)


def _reset():
    """重置 qtcompat 的单次选择缓存（仅测试可做）。"""
    qtcompat._BINDING = None
    qtcompat._RESOLVED = False
    qtcompat._qt.update({"QtCore": None, "QtGui": None, "QtWidgets": None})
    qtcompat._LAST_ERROR = ""


def test_loaded_pyside2_wins_over_pyside6():
    """宿主已加载 PySide2（ADS 2024/2025）→ 必须用 PySide2，不得再拉 PySide6。"""
    _reset()
    m2 = install_fake_binding("PySide2", loaded=True, families_static=False)
    try:
        ns = qtcompat.load()
        eq(ns.binding, "PySide2")
        eq(qtcompat.binding_name(), "PySide2")
    finally:
        cleanup(m2)
        _reset()


def test_loaded_pyside6_wins():
    _reset()
    m6 = install_fake_binding("PySide6", loaded=True, families_static=True)
    try:
        ns = qtcompat.load()
        eq(ns.binding, "PySide6")
    finally:
        cleanup(m6)
        _reset()


def test_order_by_profile_pyside2_first_when_nothing_loaded():
    """两套都没加载：HPEESOF_DIR 指向 2024/2025 → 官方档案说 PySide2 优先。"""
    _reset()
    import tempfile
    tmp = tempfile.mkdtemp(prefix="qtcompat_ads_")
    target = os.path.join(tmp, "ADS2025")
    for sub in ("bin", "tools", "config"):
        os.makedirs(os.path.join(target, sub), exist_ok=True)
    old = os.environ.get("HPEESOF_DIR")
    os.environ["HPEESOF_DIR"] = target
    try:
        eq(qtcompat._preferred_order(), ("PySide2", "PySide6"),
           "ADS 2024/2025 目录 → PySide2 优先（官方档案）")
    finally:
        if old is None:
            os.environ.pop("HPEESOF_DIR", None)
        else:
            os.environ["HPEESOF_DIR"] = old
    # 未知目录 → PySide6 优先（2026+ 的现实）
    os.environ["HPEESOF_DIR"] = os.path.join(tempfile.mkdtemp(prefix="qtcompat_x_"), "nope")
    try:
        eq(qtcompat._preferred_order(), ("PySide6", "PySide2"))
    finally:
        os.environ.pop("HPEESOF_DIR", None)


def test_binding_cached_forever():
    """选定后不再重选 —— 混用两套 Qt 绑定是禁止项。"""
    _reset()
    m6 = install_fake_binding("PySide6", loaded=True, families_static=True)
    m2 = install_fake_binding("PySide2", loaded=True, families_static=False)
    try:
        ns1 = qtcompat.load()
        ns2 = qtcompat.load()
        eq(ns1.binding, "PySide6")
        ok(ns1 is ns2 or ns1.binding == ns2.binding, "同一绑定")
    finally:
        cleanup(m6)
        cleanup(m2)
        _reset()


def test_no_binding_raises_with_reason():
    _reset()
    # 伪造导入失败：preferred order 里的 import 都应失败
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def broken_import(name, *a, **k):
        if name.startswith("PySide"):
            raise ImportError(f"模拟缺绑定: {name}")
        return real_import(name, *a, **k)

    import builtins
    old = builtins.__import__
    builtins.__import__ = broken_import
    try:
        try:
            qtcompat.load()
            raised = False
        except qtcompat.QtBindingUnavailable as e:
            raised = True
            ok(str(e), "异常里必须带原因")
    finally:
        builtins.__import__ = old
        _reset()
    ok(raised, "两个绑定都不可用时必须抛 QtBindingUnavailable")


def test_font_families_shim_qt5():
    """PySide2：QFontDatabase 需要实例化 —— shim 必须处理。"""
    _reset()
    m2 = install_fake_binding("PySide2", loaded=True, families_static=False)
    try:
        families = qtcompat.font_families()
        ok("SimSun" in families, f"Qt5 形态应走实例方法: {families}")
    finally:
        cleanup(m2)
        _reset()


def test_font_families_shim_qt6():
    _reset()
    m6 = install_fake_binding("PySide6", loaded=True, families_static=True)
    try:
        families = qtcompat.font_families()
        ok("Microsoft YaHei" in families, f"Qt6 形态应走静态方法: {families}")
    finally:
        cleanup(m6)
        _reset()


def test_dialog_exec_shim():
    _reset()
    m2 = install_fake_binding("PySide2", loaded=True, families_static=False)
    try:
        dlg = qtcompat.QtWidgets().QDialog()
        eq(qtcompat.dialog_exec(dlg), 0, "PySide2 对话框走 exec_")
    finally:
        cleanup(m2)
        _reset()
    _reset()
    m6 = install_fake_binding("PySide6", loaded=True, families_static=True)
    try:
        dlg = qtcompat.QtWidgets().QDialog()
        eq(qtcompat.dialog_exec(dlg), 0, "PySide6 对话框走 exec")
    finally:
        cleanup(m6)
        _reset()


if __name__ == "__main__":
    raise SystemExit(run(globals()))
