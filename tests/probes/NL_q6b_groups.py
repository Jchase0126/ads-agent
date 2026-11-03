# 只读探测 Q6b：TDK boot/palette 文件存在性 + 用 deitem_get_visible_palette_name 枚举全部分组
import json
import os

out = {}


def safe(label, fn):
    try:
        out[label] = fn()
    except Exception as e:  # noqa: BLE001
        out[label] = "ERR: %r" % (e,)


def _files():
    from keysight.ads import de
    ws = de.active_workspace()
    lib = ws.libraries["TDK_Component_Library_v2019.10"]
    libdir = str(lib.path)
    root = os.path.dirname(libdir)  # DESIGN_KIT_ROOT ../  -> 包的顶层
    res = {"libdir": libdir, "kit_root": root}
    boot_cfg = os.path.join(libdir, "eesof_lib.cfg")
    res["eesof_lib_cfg"] = {"path": boot_cfg, "exists": os.path.exists(boot_cfg)}
    boot_variants = [os.path.join(root, "de/ael/boot"),
                     os.path.join(root, "de/ael/boot.ael"),
                     os.path.join(root, "de/ael/boot.atf")]
    pal_variants = [os.path.join(root, "de/ael/palette.ael"),
                    os.path.join(root, "de/ael/palette.atf")]
    res["boot_ael"] = {p: os.path.exists(p) for p in boot_variants}
    res["palette_ael"] = {p: os.path.exists(p) for p in pal_variants}
    ael_dir = os.path.join(root, "circuit/ael")
    res["circuit_ael_dir"] = {"path": ael_dir, "exists": os.path.isdir(ael_dir)}
    if os.path.isdir(ael_dir):
        files = [f for f in os.listdir(ael_dir) if f.lower().endswith(".atf")]
        res["circuit_ael_count"] = len(files)
        res["sample_files"] = files[:5]
    return res


def _groups():
    from keysight.ads import de
    from keysight.ads.de import app
    from keysight.ads import ael
    ws = de.active_workspace()
    libname = "TDK_Component_Library_v2019.10"
    lib = ws.libraries[libname]
    root = os.path.dirname(str(lib.path))
    ael_dir = os.path.join(root, "circuit/ael")
    cells = sorted(os.path.splitext(f)[0] for f in os.listdir(ael_dir)
                   if f.lower().endswith(".atf"))
    win = app.current_window()
    groups = {}
    none_cells = []
    errs = []
    n = 0
    for c in cells:
        try:
            r = ael.call(convert=False).deitem_get_visible_palette_name(win, libname, c)
            s = str(r)
            n += 1
            if s in ("None", "", "null"):
                none_cells.append(c)
            else:
                groups.setdefault(s, []).append(c)
        except Exception as e:  # noqa: BLE001
            errs.append([c, repr(e)])
    return {
        "queried": len(cells),
        "resolved": n,
        "group_count": len(groups),
        "groups": {k: {"items": len(v), "sample": v[:4]} for k, v in groups.items()},
        "none_cells": none_cells[:20],
        "errors": errs[:10],
    }


safe("files", _files)
safe("groups", _groups)

print("PROBE_Q6B_BEGIN")
print(json.dumps(out, indent=2, default=str))
print("PROBE_Q6B_END")
