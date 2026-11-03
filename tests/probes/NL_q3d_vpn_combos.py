# 只读探测 Q3d：穷举 deitem_get_visible_palette_name 参数形态 + 候选 palette getter
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
    win = app.current_window()

    combos = {
        "win_only": (win,),
        "win+cell": (win, "TDK_ACM12V"),
        "win+lcv": (win, "TDK_Component_Library_v2019.10:TDK_ACM12V"),
        "win+lib+cell": (win, "TDK_Component_Library_v2019.10", "TDK_ACM12V"),
        "win+lib+cell+view": (win, "TDK_Component_Library_v2019.10", "TDK_ACM12V", "schematic"),
    }
    for tag, args in combos.items():
        try:
            r = ael.call(convert=False).deitem_get_visible_palette_name(*args)
            res["vpn_" + tag] = {"is_null": bool(r.is_null()), "str": str(r)}
        except Exception as e:  # noqa: BLE001
            res["vpn_" + tag] = "ERR: %r" % (e,)

    # 候选 palette getter 名称存在性
    cands = ["de_get_palette_list", "de_get_palette_group_list", "de_palette_group_names",
             "de_get_visible_palettes", "de_get_active_palette", "de_get_window_palette",
             "de_get_current_palette", "de_get_palette_names", "de_list_palettes",
             "deitem_get_palette_name", "de_get_library_palettes", "de_get_palette_items"]
    ex = {}
    for n in cands:
        try:
            v = ael.decl(convert=False)
            val = getattr(v, n)
            ex[n] = {"is_null": bool(val.is_null()), "type": val.ael_type_name()}
        except Exception as e:  # noqa: BLE001
            ex[n] = "missing"
    res["candidate_getters"] = ex
    return res


safe("probe", _probe)

print("PROBE_Q3D_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q3D_END")
