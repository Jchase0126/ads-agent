# 只读探测 Q4c：各窗口 menubar 中查找 Component Library / Palette 打开入口
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _all_win_menus():
    from keysight.ads.de import app
    res = {}
    for wt_name in ["MAIN_WINDOW", "SCHEMATIC_WINDOW", "LAYOUT_WINDOW"]:
        wt = getattr(app.WindowType, wt_name)
        for i, w in enumerate(app.find_windows_by_type(wt)):
            paths = []
            try:
                impl = w.menubar._impl

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

                walk(impl)
            except Exception as e:  # noqa: BLE001
                res[f"{wt_name}#{i}:err"] = repr(e)
                continue
            hits = [p for p in paths
                    if any(k in p.lower() for k in ["palette", "librar", "browser", "component"])]
            res[f"{wt_name}#{i}"] = {"total": len(paths), "hits": hits}
    return res


safe("window_menus", _all_win_menus)

print("PROBE_Q4C_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q4C_END")
