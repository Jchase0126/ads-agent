"""插件注册状态的查询与（在 ADS 内）官方 API 操作。

## 为什么安装器不走官方 API

``keysight.ads.de.app.add_user_addon()`` / ``remove_user_addon()`` 确实是官方的
用户级注册接口（``<ADS>\\tools\\python\\packages\\keysight\\ads\\de\\app\\addon.py``），
但它的实现是 ``_pde_app.add_user_addon(addon._impl)`` —— ``_pde_app`` 是**编译进
ADS 主程序的**模块，只有 ADS 自己的进程里才有。

实测（本机 ADS 2027，2026-10-07）：

* ADS 进程内  ``import keysight.ads.de.app`` → OK，``add_user_addon`` 可用；
* 用 ADS 自带解释器 ``<ADS>\\tools\\python\\python.exe`` 在外部跑同一个 import
  → ``ImportError: cannot import name 'WorkspaceItemType' from '_pde_app.ui'``，
  ``dir(_pde_app)`` 里没有任何 addon 符号。

也就是说：**外部进程（安装器 / 卸载器 / 自检）无法调用它**。硬性要求安装步骤
"不依赖 ADS 正在运行、不在 ADS 进程里"，所以安装注册只能写 ADS 启动时会读的
``eesof_addons.xml``（结构化解析 + 备份 + 原子写 + 失败回滚，见 install_addon.py）。

## 那这个模块干嘛用

在**条件成立的地方**照用官方 API：本模块跑在 ADS 进程里，负责

* 查询当前注册来源（``AddonLocale``：INSTALLATION / USER / SITE / MEMORY）；
* 在用户明确要求时，用 ``add_user_addon`` / ``remove_user_addon`` 增删用户级注册
  （ADS 菜单 ▸ Tools ▸ ADS Agent ▸ 注册状态…）。

安装器不自动替用户做这件事 —— 注册方式由用户决定，不悄悄改两处。
"""

from __future__ import annotations

import os


def official_api():
    """返回 ``keysight.ads.de.app`` 模块；不在 ADS 进程里则返回 ``None``。"""
    try:
        import keysight.ads.de.app as app  # type: ignore

        if hasattr(app, "find_addon") and hasattr(app, "add_user_addon"):
            return app
    except Exception:  # noqa: BLE001
        pass
    return None


def api_available() -> bool:
    return official_api() is not None


ADDON_NAME = "ADS Agent"


def find_record(root_directory: str | None = None):
    """查当前会话里本插件的注册记录。找不到返回 None。"""
    app = official_api()
    if app is None:
        return None
    name = ADDON_NAME
    if root_directory:
        root_directory = os.path.normpath(root_directory)
    try:
        record = app.find_addon(name)
    except Exception:  # noqa: BLE001
        return None
    if record is None:
        return None
    if root_directory:
        try:
            if os.path.normcase(record.root_directory) != os.path.normcase(root_directory):
                return None
        except Exception:  # noqa: BLE001
            pass
    return record


def describe(root_directory: str | None = None) -> dict:
    """注册状态。返回 dict，界面直接照着渲染。

    拿不到官方 API 时 ``available=False`` 并说明原因 —— 不假装"未注册"。
    """
    app = official_api()
    if app is None:
        return {
            "available": False,
            "detail": ("官方注册接口不可用：当前不在 ADS 进程内"
                       "（keysight.ads.de.app 依赖 ADS 内置的 _pde_app 模块）。"
                       "注册状态请用 安装目录下 install_addon.py --status 查看。"),
        }

    record = find_record(root_directory)
    if record is None:
        return {
            "available": True,
            "registered": False,
            "detail": f"当前 ADS 会话里没有加载名为 “{ADDON_NAME}” 的插件。",
        }

    location, location_name = "", "未知"
    try:
        loc = record.location
        location_name = getattr(loc, "name", str(loc)) or "未知"
        location = location_name
    except Exception:  # noqa: BLE001
        pass

    try:
        enabled = bool(record.enabled)
    except Exception:  # noqa: BLE001
        enabled = False

    return {
        "available": True,
        "registered": True,
        "name": getattr(record, "name", ADDON_NAME) or ADDON_NAME,
        "enabled": enabled,
        "location": location,
        "root_directory": getattr(record, "root_directory", ""),
        "startup_file": getattr(record, "startup_file", ""),
        "raw_startup_file": getattr(record, "raw_startup_file", ""),
        "detail": "",
    }


def locale_help() -> str:
    """把四种注册位置翻成人话，界面/卸载提示都用它。"""
    return (
        "INSTALLATION —— 写在 ADS 安装目录的 config\\eesof_addons.xml（可能需要管理员权限）；\n"
        "USER         —— 写在用户目录的用户级 addons 配置（每人一份，不需要管理员权限）；\n"
        "SITE         —— 站点级配置；\n"
        "MEMORY       —— 只在当前会话内存里，关掉 ADS 就没了。"
    )


def register_user_level(root_directory: str, enabled: bool = True) -> tuple[bool, str]:
    """用官方接口把本插件登记到**用户级**（必须在 ADS 进程内调用）。"""
    app = official_api()
    if app is None:
        return False, "不在 ADS 进程内，无法调用官方注册接口"
    root = os.path.normpath(root_directory or "")
    start = os.path.join(root, "__init__.py")
    if not os.path.isfile(start):
        return False, f"该目录下没有 __init__.py：{root}"
    try:
        addon = app.Addon(ADDON_NAME, start, enabled=enabled)
        app.add_user_addon(addon)
    except Exception as e:  # noqa: BLE001
        return False, f"add_user_addon 失败：{type(e).__name__}: {e}"
    return True, f"已用官方接口登记为用户级插件：{start}"


def unregister_user_level() -> tuple[bool, str]:
    """用官方接口移除用户级注册（必须在 ADS 进程内调用）。"""
    app = official_api()
    if app is None:
        return False, "不在 ADS 进程内，无法调用官方注册接口"
    record = find_record()
    if record is None:
        return False, f"当前会话里没有名为 “{ADDON_NAME}” 的插件"
    try:
        location_name = getattr(record.location, "name", "")
    except Exception:  # noqa: BLE001
        location_name = ""
    if location_name != "USER":
        return False, (f"当前注册位置是 {location_name or '未知'}，不是 USER —— "
                       "安装级注册请用 uninstall_addon.bat（它会改 eesof_addons.xml）")
    try:
        app.remove_user_addon(record)
    except Exception as e:  # noqa: BLE001
        return False, f"remove_user_addon 失败：{type(e).__name__}: {e}"
    return True, "已用官方接口移除用户级注册（下次启动 ADS 生效）"


def menu_text() -> str:
    """给菜单/状态框用的一段人话。"""
    info = describe()
    if not info.get("available"):
        return info["detail"]
    if not info.get("registered"):
        return info["detail"]
    lines = [
        f"名称：{info.get('name')}",
        f"位置：{info.get('location')}",
        f"目录：{info.get('root_directory')}",
        f"启用：{'是' if info.get('enabled') else '否'}",
    ]
    return "\n".join(lines)
