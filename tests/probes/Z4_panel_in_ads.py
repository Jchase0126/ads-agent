"""只读探测 4：在 ADS 进程内核实「面板已加载并显示」。

不点任何按钮、不发任何请求，只回答：
  1. 插件模块在不在（__init__ / panel / toolserver）；
  2. 面板单例建出来了没有、是不是可见、多大；
  3. 面板是不是被嵌进了 ADS 主窗口（停靠成功）；
  4. 面板用的数据目录与配置是不是"分发版"的那一份。
"""

CODE = r'''
import json, os, sys

out = {}

try:
    import panel  # noqa: F401
except ImportError:
    _ts = sys.modules.get("toolserver")
    _f = getattr(_ts, "__file__", None) if _ts is not None else None
    if _f:
        _d = os.path.dirname(os.path.abspath(_f))
        if _d not in sys.path:
            sys.path.insert(0, _d)


def guard(label, fn):
    try:
        out[label] = fn()
    except Exception as e:
        out[label] = {"error": f"{type(e).__name__}: {e}"}


# ---- 1. 插件模块是否都在 ----
def modules_info():
    # 插件目录里的模块由 ADS 的 smart-package loader 加载，普通 import 名不一定
    # 等于文件名；backend/ 下的模块则统一登记成 ads_agent_shared_<stem>
    # （见 pathbridge.load_backend_module），所以按这两个约定分别查。
    addon_names = ["panel", "toolserver", "ads_ops", "pathbridge",
                   "backend_launcher", "registration", "uiscale"]
    shared_names = ["ads_agent_shared_paths", "ads_agent_shared_adslocate",
                    "ads_agent_shared_instance", "ads_agent_shared_config"]
    info = {n: (n in sys.modules) for n in addon_names}
    info["_shared"] = {n: (n in sys.modules) for n in shared_names}
    info["_loaded_ads_like"] = sorted(
        k for k in sys.modules if "ads_agent" in k.lower()
    )[:20]
    return info


guard("modules", modules_info)


# ---- 2. 面板单例 ----
def singleton_info():
    import panel
    p = getattr(panel, "_panel", None)
    info = {
        "created": p is not None,
        "handle_created": getattr(panel, "_handle", None) is not None,
    }
    if p is None:
        return info
    info["visible"] = bool(p.isVisible())
    info["size"] = [int(p.width()), int(p.height())]
    info["class"] = type(p).__name__
    return info


guard("panel", singleton_info)


# ---- 3. 是否嵌进 ADS 主窗口 ----
def dock_info():
    """``panel._panel`` 有两种形态：

    * **QDockWidget** —— 正常情况：已 dock 进 ADS 主窗口（找不到 ADS 主窗口时
      才会退回下面那种）；
    * **QMainWindow** —— fallback：独立窗口，说明 `main_pyside_widget()` 没拿到
      ADS 主窗口。用 ``is_window`` / 类型就能区分，别把 fallback 当成功。
    """
    import panel
    from PySide6.QtWidgets import QDockWidget, QApplication, QMainWindow

    p = getattr(panel, "_panel", None)
    if p is None:
        return {"error": "panel 单例还没建"}

    info = {
        "panel_class": type(p).__name__,
        "docked_into_ads": isinstance(p, QDockWidget),
        "is_window": bool(p.isWindow()),
        "visible": bool(p.isVisible()),
        "size": [int(p.width()), int(p.height())],
    }
    parent = p.parent()
    info["parent_class"] = type(parent).__name__ if parent is not None else None
    info["parent_is_qmainwindow"] = isinstance(parent, QMainWindow)
    if isinstance(p, QDockWidget):
        info["floating"] = bool(p.isFloating())
        info["allowed_areas_nonzero"] = bool(p.allowedAreas())
        feats = p.features()
        # PySide6 里 features() 是枚举标志位，int() 会 TypeError（踩过一次）
        try:
            info["features"] = int(feats.value)
        except Exception:  # noqa: BLE001
            info["features"] = str(feats)
    if isinstance(p, QMainWindow):
        info["fallback_reason"] = "main_pyside_widget() 没返回 QMainWindow"
    inner = p.widget() if isinstance(p, QDockWidget) else None
    info["inner_widget_class"] = type(inner).__name__ if inner is not None else None
    app = QApplication.instance()
    info["top_level_widgets"] = len(app.topLevelWidgets()) if app else 0
    info["qt_platform"] = app.platformName() if app else None
    info["dock_title"] = panel.DOCK_TITLE
    return info


guard("dock", dock_info)


# ---- 4. 面板侧路径 ----
def panel_paths():
    import panel
    import pathbridge
    p = pathbridge.load()
    win = getattr(panel, "_panel", None)
    # _panel 是 QDockWidget/QMainWindow 外壳，_PROJECTS_FILE / _user_path 长在
    # 里面的 AgentPanelWidget 上。
    inner = win.widget() if win is not None and hasattr(win, "widget") else win
    cls = type(inner)
    user_path = None
    if hasattr(cls, "_user_path"):
        user_path = cls._user_path("projects.json")
    return {
        "inner_class": cls.__name__,
        "projects_file": user_path,
        "config_path": p.config_path(),
        "data_root": p.data_root(),
        "app_root": p.app_root(),
        "logs_dir": p.logs_dir(),
        "design_jobs_dir": p.design_jobs_dir(),
    }


guard("panel_paths", panel_paths)

# ---- 5. 菜单/注册（在 ADS 内应当可用）----
def reg_info():
    import registration as reg
    return reg.describe()


guard("registration", reg_info)

print(json.dumps(out, ensure_ascii=False, indent=1))
'''
