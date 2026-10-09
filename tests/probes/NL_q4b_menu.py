# 只读探测 Q4b：主窗口菜单动作枚举（找 Component Library / Palette 打开入口）+ boot filepath
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _menubar_introspect():
    from keysight.ads.de import app
    res = {}
    mw = app.main_window()
    mb = mw.menubar
    res["menubar_type"] = type(mb).__name__
    res["menubar_dir"] = [n for n in dir(mb) if not n.startswith("__")]
    impl = mb._impl
    res["impl_type"] = type(impl).__name__
    res["impl_has_get_menu_actions"] = hasattr(impl, "get_menu_actions")
    # 尝试反射遍历菜单路径
    paths = []

    def walk(obj, depth=0):
        if depth > 4:
            return
        try:
            actions = obj.get_menu_actions()
        except Exception:  # noqa: BLE001
            return
        for a in actions:
            try:
                paths.append(a.item_path())
            except Exception:  # noqa: BLE001
                pass
            walk(a, depth + 1)

    try:
        walk(impl)
    except Exception as e:  # noqa: BLE001
        res["walk_err"] = repr(e)
    res["total_paths"] = len(paths)
    hits = [p for p in paths
            if any(k in p.lower() for k in ["palette", "librar", "browser", "component", "design kit"])]
    res["hits"] = hits
    res["sample"] = paths[:30]
    return res


def _bootpath():
    from keysight.ads import ael
    res = {}
    for n in ["de_get_current_ads_ael_addon_boot_ael_filepath", "de_current_ads_ael_addon_is_loading"]:
        try:
            v = ael.decl(convert=False)
            val = getattr(v, n)
            res[n] = {"is_null": bool(val.is_null()), "type": val.ael_type_name(), "str": str(val)}
        except Exception as e:  # noqa: BLE001
            res[n] = "ERR: %r" % (e,)
    return res


safe("menubar", _menubar_introspect)
safe("bootpath", _bootpath)

print("PROBE_Q4B_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q4B_END")
