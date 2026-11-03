# 只读探测 Q3e：取 deitem_get_visible_palette_name(win, lib, cell) 的真实返回串
import json

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _call(*args):
    from keysight.ads import ael
    r = ael.call(convert=False).deitem_get_visible_palette_name(*args)
    try:
        return {"str": str(r), "repr": repr(r)}
    except Exception as e:  # noqa: BLE001
        return {"raw_err": repr(e)}


def _probe():
    from keysight.ads.de import app
    win = app.current_window()
    res = {}
    cases = []
    # 逐个 TDK 元件（覆盖 strings 里出现的多个分组）
    tdk_cells = [
        "TDK_ACM12V", "TDK_ALT3232M_151_T001", "TDK_SGNE04", "TDK_MMZ0402",
        "TDK_Netlist_Include",
    ]
    for c in tdk_cells:
        cases.append(("TDK_Component_Library_v2019.10", c, None))
    # 对照组：不存在的元件
    cases.append(("TDK_Component_Library_v2019.10", "NO_SUCH_CELL_XYZ", None))
    # 对照：内置库
    cases.append(("ads_rflib", "R", None))
    for lib, cell, view in cases:
        key = f"{lib}::{cell}"
        args = [win, lib, cell]
        if view:
            args.append(view)
        try:
            r = _call(*args)
        except Exception as e:  # noqa: BLE001
            r = {"ERR": repr(e)}
        res[key] = r
    return res


safe("vpn", _probe)

print("PROBE_Q3E_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q3E_END")
