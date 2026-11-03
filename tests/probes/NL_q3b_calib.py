# 只读探测 Q3b：校准 AEL 词汇 + 尝试 deitem_get_visible_palette_name 签名
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _calib():
    from keysight.ads import ael
    names = ["create_item", "de_version_number_int", "designKitRecord", "SCHEMATIC_WINDOW",
             "LAYOUT_WINDOW", "SCHEM_WIN", "LAYOUT_WIN", "TRUE", "NULL", "analogRFnet"]
    res = {}
    for n in names:
        try:
            v = ael.decl(convert=False)
            val = getattr(v, n)
            res[n] = {"is_null": bool(val.is_null()), "type": val.ael_type_name()}
        except Exception as e:  # noqa: BLE001
            res[n] = "ERR: %r" % (e,)
    return res


safe("calibration", _calib)


def _call0():
    from keysight.ads import ael
    fn = ael.decl(convert=False).deitem_get_visible_palette_name
    try:
        r = fn()
        return {"ok": True, "repr": repr(r), "str": str(r)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": repr(e)}


safe("visible_palette_0args", _call0)


def _find_item():
    from keysight.ads import ael
    res = {}
    for name in ["TDK_ACM12V", "TDK_ALT3232M_151_T001", "demo_mmWave_Res"]:
        try:
            r = ael.call(convert=False).dm_find_item_definition(name)
            res[name] = {"is_null": bool(r.is_null()), "str": str(r)}
        except Exception as e:  # noqa: BLE001
            res[name] = "ERR: %r" % (e,)
    return res


safe("dm_find_item_definition", _find_item)

print("PROBE_Q3B_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q3B_END")
