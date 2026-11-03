# 只读探测 Q3c：用窗口/设计上下文查可见 palette 名称
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _probe():
    from keysight.ads import ael
    from keysight.ads.de import app
    res = {}

    # 当前窗口（Python 包装）
    try:
        win = app.current_window()
        res["current_window"] = None if win is None else {"title": win.title,
                                                          "type": int(win.window_type)}
    except Exception as e:  # noqa: BLE001
        res["current_window"] = "ERR: %r" % (e,)
        win = None

    # AEL 侧当前窗口实例
    ael_win = None
    try:
        ael_win = ael.call(convert=False).api_get_current_window()
        res["ael_current_window"] = {"is_null": bool(ael_win.is_null()), "str": str(ael_win)}
    except Exception as e:  # noqa: BLE001
        res["ael_current_window"] = "ERR: %r" % (e,)

    # 用各种上下文调用 deitem_get_visible_palette_name
    for tag, arg in [("ael_current_window", ael_win), ("win_py", win)]:
        if arg is None:
            res["vpn_with_" + tag] = "skipped (None)"
            continue
        try:
            r = ael.call(convert=False).deitem_get_visible_palette_name(arg)
            res["vpn_with_" + tag] = {"is_null": bool(r.is_null()), "str": str(r)}
        except Exception as e:  # noqa: BLE001
            res["vpn_with_" + tag] = "ERR: %r" % (e,)

    # 是否有窗口显示 TDK 库
    try:
        r = ael.call(convert=False).de_get_windows_with_same_library("TDK_Component_Library_v2019.10")
        res["windows_same_lib_TDK"] = {"is_null": bool(r.is_null()), "str": str(r)}
    except Exception as e:  # noqa: BLE001
        res["windows_same_lib_TDK"] = "ERR: %r" % (e,)

    return res


safe("probe", _probe)

print("PROBE_Q3C_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q3C_END")
