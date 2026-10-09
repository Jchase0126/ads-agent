"""ADS Agent — an AI assistant addon for Keysight PathWave ADS 2027.

Addon contract (loaded by keysight.ads.de.app):
  setup_addon(addon)            optional, no UI allowed here
  shutdown_addon(addon)         optional cleanup
  generate_menu(addon, win_def) add Tools > ADS Agent menu

IMPORTANT: ADS's smart-package loader executes this module WITHOUT injecting
``__file__`` (its ModuleSpec is built with has_location=False), so this file
must never touch ``__file__``. The addon directory is resolved from
``addon.root_directory`` / the eesof_addons.xml registration instead — the
same approach the shipping addons use.
"""

import os
import re
import sys

_ADDON_NAME = "ADS Agent"
_plugin_dir: str | None = None


# ---------------------------------------------------------------------------
# path resolution (no __file__ allowed here)
# ---------------------------------------------------------------------------

def _resolve_plugin_dir() -> str | None:
    """Locate this addon's directory via the addon registry, not __file__."""
    global _plugin_dir
    if _plugin_dir:
        return _plugin_dir

    candidates: list[str] = []

    # 1. the Addon object registered in this session
    try:
        import keysight.ads.de.app as app

        record = app.find_addon(_ADDON_NAME)
        if record is not None:
            candidates.append(record.root_directory)
    except Exception:
        pass

    # 2. the installation-level registration file
    #    ADS 目录**不能**在这里写死默认值：每台机器的安装位置都不一样。
    #    优先用环境变量；没有就让 backend/adslocate.py 去探测（卸载表 + 扫描），
    #    仍然找不到时这一步自然跳过 —— 由 setup_addon(addon) 里的
    #    addon.root_directory 兜底，绝不因此让 ADS 启动失败。
    try:
        import pathbridge

        adslocate = pathbridge.load_backend_module("adslocate.py", "adslocate")
        hpeesof = (os.environ.get("HPEESOF_DIR") or "").strip()
        xmls = []
        if hpeesof:
            xmls.append(os.path.join(hpeesof, "config", "eesof_addons.xml"))
        for item in adslocate.detect_ads_dirs(scan=False):
            xmls.append(os.path.join(item["dir"], "config", "eesof_addons.xml"))
        xml = next((p for p in xmls if p and os.path.exists(p)), "")
        if xml:
            text = open(xml, encoding="utf-8", errors="replace").read()
            m = re.search(
                r'<Addon\s+Name="%s"\s+FilePath="([^"]+)"' % _ADDON_NAME, text
            )
            if m:
                candidates.append(m.group(1))
    except Exception:
        pass

    for cand in candidates:
        if not cand:
            continue
        cand = os.path.normpath(cand)
        if cand.lower().endswith("__init__.py"):
            cand = os.path.dirname(cand)
        if os.path.isdir(cand):
            _plugin_dir = cand
            return _plugin_dir
    return None


def _ensure_sys_path() -> str | None:
    d = _resolve_plugin_dir()
    if d and d not in sys.path:
        sys.path.insert(0, d)
    return d


def _shared_paths():
    """共享路径模块（%LOCALAPPDATA%\\ADSAgent 等），拿不到返回 None。"""
    try:
        _ensure_sys_path()
        import pathbridge

        return pathbridge.load()
    except Exception:  # noqa: BLE001
        return None


def _config_path() -> str:
    p = _shared_paths()
    if p is not None:
        try:
            return p.config_path()
        except Exception:  # noqa: BLE001
            pass
    # 兜底：旧布局（配置文件就在代码目录旁边）
    d = _ensure_sys_path() or ""
    return os.path.normpath(os.path.join(d, "..", "..", "config.ini"))


def prepare_data() -> tuple[bool, str]:
    """准备好用户数据：建目录 → 从旧布局迁移 → 首启生成干净配置。

    必须在拉起后端**之前**跑：后端会往 数据目录/logs 与 config.ini 里写东西，
    目录都没建好就启动，等于把改造前"日志跟代码混在一起"的老毛病再犯一遍。
    幂等且在任意 ADS 会话里都安全。
    """
    p = _shared_paths()
    if p is None:
        return False, "无法加载共享路径模块 backend/paths.py"
    try:
        p.ensure_data_dirs()
        report: list = []
        p.migrate_from_legacy(report)
        info = p.init_first_run()
        p.touch_install_state(app_root=p.app_root())
        tail = ("；" + "；".join(report)) if report else ""
        return True, f"数据目录={p.data_root()}  配置={info['config']}{tail}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _ui_setting(key: str, default: str) -> str:
    import configparser

    parser = configparser.ConfigParser()
    path = _config_path()
    if path and os.path.exists(path):
        parser.read(path, encoding="utf-8")
    try:
        raw = parser.get("ui", key).strip()
        return raw if raw else default
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


# ---------------------------------------------------------------------------
# addon contract
# ---------------------------------------------------------------------------

def setup_addon(addon) -> None:
    global _plugin_dir
    # No UI directly here per the addon contract — but we may schedule one:
    # a zero-UI QTimer.singleShot now, with retries once the event loop and
    # main window are up, gives the "panel is already open when ADS starts"
    # experience. Everything is guarded so ADS startup can never break.
    try:
        root = getattr(addon, "root_directory", None)
        if root:
            _plugin_dir = os.path.normpath(root)
        _ensure_sys_path()

        # 先把用户数据安顿好（目录 / 旧布局迁移 / 首启配置），再谈后端与面板
        ok, detail = prepare_data()
        print(f"[ADS Agent] 数据准备{'完成' if ok else '失败'}：{detail}")

        # Auto-start the backend with ADS (hidden child, reused if already up)
        import backend_launcher

        backend_launcher.ensure_backend_async(
            lambda ok, msg: print(f"[ADS Agent] {msg}")
        )

        if _ui_setting("auto_open", "true").lower() in ("1", "true", "yes", "on"):
            delay = int(_ui_setting("auto_open_delay_ms", "3000") or 3000)
            from PySide6.QtCore import QTimer

            QTimer.singleShot(max(delay, 0), lambda: _auto_open(retries=15))
    except Exception as e:  # noqa: BLE001 — never break ADS startup
        print(f"[ADS Agent] auto-open 未调度: {type(e).__name__}: {e}")


def _auto_open(retries: int) -> None:
    """Open the panel once ADS's main window exists; retry while it is not ready."""
    try:
        _ensure_sys_path()
        import toolserver

        toolserver.ensure_started()
        import panel

        panel.open_panel()
        print("[ADS Agent] 面板已自动打开")
    except Exception as e:  # noqa: BLE001
        if retries > 0:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(2000, lambda: _auto_open(retries - 1))
        else:
            print(
                f"[ADS Agent] 自动打开面板失败（可从 Tools > ADS Agent 菜单打开）: "
                f"{type(e).__name__}: {e}"
            )


def shutdown_addon(addon) -> None:
    try:
        import toolserver

        toolserver.shutdown()
    except Exception:
        pass
    try:
        import backend_launcher

        backend_launcher.shutdown_backend()  # stop the child we spawned
    except Exception:
        pass


def _remember_root(addon) -> None:
    global _plugin_dir
    root = getattr(addon, "root_directory", None)
    if root and not _plugin_dir:
        _plugin_dir = os.path.normpath(root)


def generate_menu(addon, win_def) -> None:
    import keysight.ads.de.app as app

    if win_def.window_type != app.WindowType.MAIN_WINDOW:
        return

    try:
        _remember_root(addon)
    except Exception:
        pass
    _ensure_sys_path()

    menubar = win_def.menubar
    if menubar is None:
        return
    tools_menu = menubar.find_menu("Tools")
    if tools_menu is None:
        return

    if tools_menu.find_menu("ADS Agent") is None:
        menu = app.Menu("ADS Agent")
        menu.add_action(
            app.Action("打开对话面板…", lambda action, window: _open_panel(), None)
        )
        menu.add_action(
            app.Action(
                "启动/重启工具服务",
                lambda action, window: _ensure_server(),
                None,
            )
        )
        menu.add_action(
            app.Action(
                "工具服务状态…",
                lambda action, window: _show_server_status(),
                None,
            )
        )
        tools_menu.add_menu(menu)

    # Arm the tool server + main-thread pump timer now (we are on the main
    # thread during window construction, so creating a QTimer is safe).
    try:
        import toolserver

        toolserver.ensure_started()
    except Exception as e:  # noqa: BLE001 — never break ADS startup
        print(f"[ADS Agent] toolserver 未启动: {type(e).__name__}: {e}")


def _open_panel() -> None:
    try:
        _ensure_sys_path()
        import panel

        panel.open_panel()
    except Exception as e:  # noqa: BLE001
        _show_message("ADS Agent", "无法打开对话面板", f"{type(e).__name__}: {e}", warning=True)


def _show_message(title: str, text: str, details: str = "", warning: bool = False) -> None:
    from PySide6.QtWidgets import QMessageBox
    try:
        import sys
        import uiscale as U

        loaded = sys.modules.get('panel')
        host = getattr(loaded, '_panel', None)
        widget = host.widget() if host is not None and hasattr(host, 'widget') else None
        if widget is None and host is not None and hasattr(host, 'centralWidget'):
            widget = host.centralWidget()
        pal = widget._pal() if widget is not None else U.PALETTES['light']
        U.message_dialog(title, text, details, warning, parent=host, pal=pal).exec()
    except Exception:  # status reporting must survive a broken UI module
        QMessageBox.warning(None, title, text + (f"\n{details}" if details else ""))


def _ensure_server() -> str:
    _ensure_sys_path()
    import toolserver

    return toolserver.ensure_started()


def _show_server_status() -> None:
    try:
        url = _ensure_server()
        import urllib.request

        with urllib.request.urlopen(url + "/health", timeout=3) as resp:
            body = resp.read().decode("utf-8")
        _show_message("ADS Agent 工具服务", "工具服务正在运行，可以连接 ADS。", f"服务地址：{url}\n{body}")
    except Exception as e:  # noqa: BLE001
        _show_message("ADS Agent 工具服务", "工具服务未运行或连接异常。",
                      f"{type(e).__name__}: {e}", warning=True)
