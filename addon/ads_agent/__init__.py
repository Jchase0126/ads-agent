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
    try:
        hpeesof = os.environ.get("HPEESOF_DIR", r"E:\ADS\Programfile\ADS2027")
        xml = os.path.join(hpeesof, "config", "eesof_addons.xml")
        if os.path.exists(xml):
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


def _config_path() -> str:
    d = _ensure_sys_path() or ""
    return os.path.normpath(os.path.join(d, "..", "..", "config.ini"))


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
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "ADS Agent", f"无法打开面板：\n{type(e).__name__}: {e}")


def _ensure_server() -> str:
    _ensure_sys_path()
    import toolserver

    return toolserver.ensure_started()


def _show_server_status() -> None:
    from PySide6.QtWidgets import QMessageBox

    try:
        url = _ensure_server()
        import urllib.request

        with urllib.request.urlopen(url + "/health", timeout=3) as resp:
            body = resp.read().decode("utf-8")
        QMessageBox.information(None, "ADS Agent 工具服务", f"运行中：{url}\n{body}")
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(
            None,
            "ADS Agent 工具服务",
            f"未运行或异常：{type(e).__name__}: {e}",
        )
