"""只读探测 2：AddonLocale 语义与用户级注册文件位置线索。

不修改任何注册状态、不写文件。
"""

CODE = r'''
import json, os

out = {}
import keysight.ads.de.app as app

# AddonLocale 的正确读法（不是 IntEnum）
try:
    locs = {}
    for name in app.AddonLocale.__members__:
        v = app.AddonLocale[name]
        locs[name] = {"repr": repr(v), "name": getattr(v, "name", None),
                      "value": repr(getattr(v, "value", None))}
    out["AddonLocale"] = locs
except Exception as e:
    out["AddonLocale_err"] = f"{type(e).__name__}: {e}"

# 现有 addon 的 location
info = {}
for nm in ["ADS Agent", "Quantum Tools", "NexusLoader", "Python API Help Menus",
           "Electrothermal Floorplanner", "CoilSys"]:
    try:
        a = app.find_addon(nm)
    except Exception as e:
        info[nm] = f"err {type(e).__name__}: {e}"
        continue
    if a is None:
        info[nm] = None
        continue
    try:
        loc = a.location
        loc_s = getattr(loc, "name", repr(loc))
    except Exception as e:
        loc_s = f"err:{e}"
    info[nm] = {"enabled": a.enabled, "location": loc_s,
                "sync_location": repr(a.sync_location)[:200]}
out["addons"] = info

# app 模块里所有与 addon / 路径相关的符号
syms = sorted(n for n in dir(app) if ("addon" in n.lower() or "path" in n.lower()
                                      or "config" in n.lower()))
out["app_syms"] = syms

# _pde_app 原生模块：用户级列表相关符号
try:
    import _pde_app as native
    out["native_syms"] = sorted(n for n in dir(native)
                                if ("addon" in n.lower() or "path" in n.lower()
                                    or "file" in n.lower() or "config" in n.lower()))
except Exception as e:
    out["native_err"] = f"{type(e).__name__}: {e}"

# 用户级候选目录可写性
home = os.path.expanduser("~")
probes = {
    "HOME/hpeesof/config": os.path.join(home, "hpeesof", "config"),
    "HOME/hpeesof": os.path.join(home, "hpeesof"),
    "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
}
res = {}
for k, p in probes.items():
    res[k] = {"path": p, "exists": os.path.isdir(p),
              "writable": (os.access(p, os.W_OK) if os.path.isdir(p) else None)}
out["dirs"] = res

print(json.dumps(out, ensure_ascii=False, indent=1))
'''
