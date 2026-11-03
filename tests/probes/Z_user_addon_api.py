"""只读探测（在 ADS 进程内执行）—— 核实官方用户级 Addon 注册接口的调用条件。

不修改任何注册状态、不写文件、不加载/卸载任何 addon。
"""

CODE = r'''
import json, os, inspect

out = {}

# 1) 本进程是否能访问官方接口
try:
    import keysight.ads.de.app as app
    out["app_import"] = "ok"
    out["app_file"] = getattr(app, "__file__", None)
except Exception as e:
    out["app_import"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)

names = ["Addon", "AddonLocale", "find_addon", "addon", "add_user_addon",
         "remove_user_addon", "enable_addon", "add_memory_addon"]
for n in names:
    out["has_" + n] = hasattr(app, n)

try:
    out["AddonLocale_members"] = {k: int(v) for k, v in list(app.AddonLocale.__members__.items())}
except Exception as e:
    out["AddonLocale_err"] = f"{type(e).__name__}: {e}"

try:
    out["Addon_init"] = str(inspect.signature(app.Addon.__init__))
except Exception as e:
    out["Addon_init_err"] = f"{type(e).__name__}: {e}"

for fn in ("add_user_addon", "remove_user_addon"):
    if hasattr(app, fn):
        f = getattr(app, fn)
        out[fn + "_doc"] = (getattr(f, "__doc__", "") or "").strip()[:300]

# 2) 已知 addon 的注册位置：sync_location 就是"引用它的那个 xml 文件"
probe_names = ["ADS Agent", "Python Exporter", "Quantum Tools", "NexusLoader",
               "SnP Utilities", "Memory Simulation Studio"]
info = {}
for nm in probe_names:
    try:
        a = app.find_addon(nm)
        if a is None:
            info[nm] = None
            continue
        try:
            loc = a.location
            loc_s = f"{loc.name}({int(loc)})"
        except Exception as e:
            loc_s = "err:" + str(e)
        info[nm] = {
            "root_directory": a.root_directory,
            "startup_file": a.startup_file,
            "raw_startup_file": a.raw_startup_file,
            "enabled": a.enabled,
            "location": loc_s,
            "sync_location": a.sync_location,
        }
    except Exception as e:
        info[nm] = {"error": f"{type(e).__name__}: {e}"}
out["addons"] = info

# 3) 用户级 addon 文件的候选路径是否存在
cands = {}
home = os.path.expanduser("~")
for label, p in [
    ("HOME/hpeesof/config/eesof_addons.xml", os.path.join(home, "hpeesof", "config", "eesof_addons.xml")),
    ("HOME/eesof_addons.xml", os.path.join(home, "eesof_addons.xml")),
    ("HPEESOF_DIR/config/eesof_addons.xml",
     os.path.join(os.environ.get("HPEESOF_DIR", ""), "config", "eesof_addons.xml")),
]:
    cands[label] = os.path.exists(p)
out["candidates"] = cands
out["HOME"] = home
out["HPEESOF_DIR"] = os.environ.get("HPEESOF_DIR", "")
out["USERPROFILE"] = os.environ.get("USERPROFILE", "")

# 4) _pde_app 原生模块（安装器要用的关键：它只在 ADS 进程内存在）
try:
    import _pde_app as native
    out["_pde_app"] = getattr(native, "__file__", "builtin/none")
    out["_pde_app_addon_syms"] = [n for n in dir(native) if "addon" in n.lower()][:40]
except Exception as e:
    out["_pde_app"] = f"{type(e).__name__}: {e}"

print(json.dumps(out, ensure_ascii=False, indent=1))
'''
