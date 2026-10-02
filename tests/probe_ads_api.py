"""用 ADS 自带 Python 探测 keysight.ads.de 真实 API，输出事实清单。

只读探测：只做 import / 签名 / 成员列举 / 打开设计读属性，不写任何东西。

用法:
  "E:\\ADS\\Programfile\\ADS2027\\tools\\python\\python.exe" tests\\probe_ads_api.py
"""

import inspect
import os
import sys

import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu

SEP = "=" * 70


def nm(x):
    try:
        return getattr(x, "__name__", type(x).__name__)
    except Exception:
        return str(x)


def sig(obj, label=""):
    try:
        return f"{label}{inspect.signature(obj)}"
    except Exception as e:
        return f"{label}<no signature: {type(e).__name__}: {e}>"


def members(obj, contain=""):
    try:
        out = [n for n in dir(obj) if not n.startswith("_") and contain in n]
        return ", ".join(out[:60])
    except Exception as e:
        return f"<fail {e}>"


print("\n" + SEP + "\n1) open_design / DesignMode —— 修 bugs_tools 的关键\n" + SEP)
print(sig(db_uu.open_design, "db_uu.open_design"))
# 找 DesignMode
DM = None
for modname in ("keysight.ads.de._pde.db", "keysight.ads.de.db_uu", "keysight.ads.de"):
    try:
        mod = __import__(modname, fromlist=["DesignMode"])
        cand = getattr(mod, "DesignMode", None)
        if cand is not None:
            print(f"DesignMode 来自 {modname}: {cand}")
            DM = cand
            break
    except Exception as e:
        print(f"  {modname} 导入失败: {type(e).__name__}: {e}")
if DM is not None:
    # pybind11 枚举不可下标，只能用 getattr
    print("成员(名=值):")
    for n in ("APPEND", "READ_ONLY", "WRITE", "READ_WRITE", "WRITE_ONLY"):
        v = getattr(DM, n, None)
        if v is not None:
            print(f"   {n} = {int(v)}   isinstance={type(v).__name__}")
    # 看看 open_design 的 mode 参数默认值是哪种类型
    try:
        doc = (db_uu.open_design.__doc__ or "")
        print("open_design.__doc__ 前 400 字:")
        print("   " + " | ".join(doc.splitlines())[:400])
    except Exception:
        pass

print("\n" + SEP + "\n2) Workspace —— 库/design 相关成员\n" + SEP)
ws = de.active_workspace() if de.workspace_is_open() else None
if ws is None:
    print("(没有打开的工作区，只列举类成员)")
    print("Workspace members:", members(de.Workspace) if hasattr(de, "Workspace") else "?")
else:
    print("workspace:", ws.path)
    print("Workspace 实例成员:", members(ws))
    print("含 lib 的成员:", members(ws, "libr"))
    print("含 design 的成员:", members(ws, "design"))
    print(sig(ws.design, "ws.design") if hasattr(ws, "design") else "(无 design)")

print("\n" + SEP + "\n3) Library / Cell —— 视图相关\n" + SEP)
if ws is not None:
    try:
        libs = list(ws.libraries or [])
        print("libraries:", [str(l.name) for l in libs][:12])
        ai = [l for l in libs if "AI_lib" in str(l.name)]
        target = ai[0] if ai else None
        if target is not None:
            print("目标库:", target.name, "| Library 成员:", members(target))
            cells = list(target.cells or [])
            for c in cells[:6]:
                print(f"  cell {c.name}  views={[str(getattr(v,'name',v)) for v in (c.views or [])]}")
                print(sig(c.create_view, f"     {c.name}.create_view") if hasattr(c, "create_view") else
                      f"     {c.name}: 无 create_view")
                print("     Cell 成员:", members(c, "view"))
    except Exception as e:
        print("枚举失败:", type(e).__name__, e)

print("\n" + SEP + "\n4) Design —— save / instances / 遍历\n" + SEP)
d = None
try:
    for attempt in (0, "READ_ONLY"):
        try:
            d = db_uu.open_design("AI_lib:Wilkinson_2G4:schematic", mode=attempt)
            print(f"open_design mode={attempt!r} 成功 -> {nm(d)}")
            break
        except Exception as e:
            print(f"open_design mode={attempt!r} 失败: {type(e).__name__}: {str(e)[:160]}")
except Exception as e:
    print("打开设计失败:", type(e).__name__, e)

if d is not None:
    print("Design 成员(含 save):", members(d, "save"))
    print("Design 成员(含 instance/itor/iter):", members(d, "inst"))
    print("Design 成员(含 net/wire/conn):", members(d, "net"))
    print("Design 成员(含 create):", members(d, "create"))
    try:
        insts = list(d.instances or [])
        print("instances:", len(insts), "->", [str(getattr(i, "instance_name", i)) for i in insts][:8])
        if insts:
            i0 = insts[0]
            print("首个实例类型:", nm(i0))
            print("实例成员:", members(i0))
            print("实例成员(含 origin/angle/loc/place):", members(i0, "orig"))
    except Exception as e:
        print("遍历实例失败:", type(e).__name__, e)

print("\n" + SEP + "\n5) 放置元件 / Pin / 连线相关\n" + SEP)
for modname, objname in (
    ("keysight.ads.de.db_uu", "ItemInfo"),
    ("keysight.ads.de.db_uu", "InstPin"),
    ("keysight.ads.de.db_uu", "InstTerm"),
    ("keysight.ads.de.db_uu", "PointF"),
    ("keysight.ads.de.db_uu", "Point"),
    ("keysight.ads.de.db_uu", "ScalarInst"),
    ("keysight.ads.de.db_uu", "ShapeIter"),
    ("keysight.ads.de._pde.db", "PointF"),
):
    try:
        mod = __import__(modname, fromlist=[objname])
        obj = getattr(mod, objname, None)
        if obj is None:
            print(f"{modname}.{objname}: 不存在")
            continue
        print(f"{modname}.{objname} = {obj}")
        try:
            print("   成员:", members(obj)[:600])
        except Exception as e:
            print("   成员列举失败:", e)
        for m in ("create_new_instance", "create_instance", "place"):
            f = getattr(obj, m, None)
            if callable(f):
                print("   " + sig(f, m))
    except Exception as e:
        print(f"{modname}.{objname}: 导入失败 {type(e).__name__}")

print("\n" + SEP + "\n6) 元件库：tlines / 端口 / S 参数控制器在哪\n" + SEP)
if ws is not None:
    for libname in ("ads_tlines", "ads_sources", "ads_simulation", "ads_rflib", "ads_datacmps"):
        try:
            lib = [l for l in (ws.libraries or []) if str(l.name) == libname]
            if not lib:
                print(f"{libname}: 未挂载")
                continue
            names = [str(c.name) for c in (lib[0].cells or [])]
            hit = [n for n in names if n.upper() in
                   ("TERM", "PORT", "SP", "S-PARAM", "TERM4", "MSUB", "MLIN", "MTEE", "VAR", "R", "MSTEP")]
            print(f"{libname}: {len(names)} cells; 关注={hit}")
            if libname == "ads_simulation":
                print("   控制器候选:", [n for n in names if "SP" in n.upper() or "NWA" in n.upper()][:12])
            if libname == "ads_sources":
                print("   端口候选:", [n for n in names if "TERM" in n.upper() or "PORT" in n.upper()][:12])
        except Exception as e:
            print(libname, "失败:", type(e).__name__, e)

print("\n" + SEP + "\n探测结束")
