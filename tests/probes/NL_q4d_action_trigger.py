# 只读探测 Q4d：定位 Component Library / Component Palette 菜单项对象并检查可触发性（不触发）
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _find_actions():
    from keysight.ads.de import app
    res = {}
    wt = app.WindowType.SCHEMATIC_WINDOW
    wins = app.find_windows_by_type(wt)
    if not wins:
        return "no schematic window"
    w = wins[0]
    impl = w.menubar._impl

    found = {}

    def walk(obj, depth=0):
        if depth > 5:
            return
        try:
            actions = obj.get_menu_actions()
        except Exception:  # noqa: BLE001
            return
        for a in actions:
            try:
                p = a.item_path()
            except Exception:  # noqa: BLE001
                p = "?"
            if "Component Library" in p or "Component Palette" in p:
                found[p] = a
            walk(a, depth + 1)

    walk(impl)
    for p, a in found.items():
        info = {"type": type(a).__name__}
        info["dir"] = [n for n in dir(a) if not n.startswith("_")]
        info["has_trigger"] = hasattr(a, "trigger")
        info["has_execute"] = hasattr(a, "execute")
        # Qt 侧可达性
        try:
            qw = a.qwidget if hasattr(a, "qwidget") else None
            info["qwidget"] = type(qw).__name__ if qw is not None else None
        except Exception as e:  # noqa: BLE001
            info["qwidget_err"] = repr(e)
        res[p] = info
    return res


safe("component_menu_actions", _find_actions)

print("PROBE_Q4D_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q4D_END")
