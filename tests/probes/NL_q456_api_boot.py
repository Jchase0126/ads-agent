# 只读探测 Q4/Q5/Q6：app 模块 API + _pde_app 接口 + 菜单动作 + boot 触发候选函数
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _app_dir():
    from keysight.ads.de import app
    names = [n for n in dir(app) if not n.startswith("__")]
    return names


def _pde_app_dir():
    import _pde_app
    top = [n for n in dir(_pde_app) if not n.startswith("__")]
    ui = [n for n in dir(_pde_app.ui) if not n.startswith("__")]
    return {"top": top, "ui": ui}


def _menu_actions():
    from keysight.ads.de import app
    res = {}
    try:
        mw = app.main_window()
        mb = mw.menubar
        items = mb.get_menu_actions()
        paths = []

        def walk(item, depth=0):
            if depth > 3:
                return
            for it in item.get_menu_actions():
                try:
                    paths.append(it.item_path())
                except Exception as e:  # noqa: BLE001
                    paths.append("ERR path: %r" % (e,))
                if hasattr(it, "get_menu_actions"):
                    walk(it, depth + 1)

        walk(mb)
        hits = [p for p in paths
                if any(k.lower() in p.lower() for k in ["palette", "librar", "browser", "component"])]
        res["total_paths"] = len(paths)
        res["hits"] = hits
    except Exception as e:  # noqa: BLE001
        res["ERR"] = repr(e)
    return res


def _boot_funcs():
    from keysight.ads import ael
    cands = [
        "de_boot_design_kit", "de_load_library", "dk_boot", "de_design_kit_boot",
        "de_load_design_kit", "de_boot_library", "de_reload_library", "de_refresh_library",
        "de_get_current_ads_ael_addon_boot_ael_filepath", "de_current_ads_ael_addon_is_loading",
        "de_open_library_read_only_mode", "de_add_library_to_workspace_read_only_mode",
        "de_close_library", "de_new_library", "de_get_open_libraries", "de_is_library_open",
        "de_get_path_to_open_library", "de_library_config_file_exists",
        "de_create_window", "de_get_window", "de_window_is_open",
        "de_bring_context_to_top_or_open_new_window", "de_show_context_in_new_window",
        "de_get_windows_with_same_library",
    ]
    res = {}
    for n in cands:
        try:
            v = ael.decl(convert=False)
            val = getattr(v, n)
            res[n] = {"is_null": bool(val.is_null()), "type": val.ael_type_name()}
        except Exception as e:  # noqa: BLE001
            res[n] = "missing"
    return res


safe("app_public_names", _app_dir)
safe("pde_app_names", _pde_app_dir)
safe("menu_actions", _menu_actions)
safe("boot_funcs", _boot_funcs)

print("PROBE_Q456_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q456_END")
