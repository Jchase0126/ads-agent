import inspect
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu

print("== add_instance doc ==")
d = db_uu.open_design("AI_lib:Wilkinson_2G4:schematic", mode=db_uu.DesignMode.READ_ONLY)
doc = (d.add_instance.__doc__ or "")
print(" | ".join(doc.splitlines())[:900])

print("\n== ItemInfo 怎么拿 ==")
try:
    print("ItemInfo.__init__:", inspect.signature(de.ItemInfo.__init__))
    print("de.ItemInfo doc:", " | ".join((de.ItemInfo.__doc__ or "").splitlines())[:300])
except Exception as e:
    print("失败:", e)
for helper in ("get_item_info", "item_info", "find_item", "component_info"):
    for mod in (de, db_uu):
        f = getattr(mod, helper, None)
        if f: print(f"  {mod.__name__}.{helper}:", inspect.signature(f))

print("\n== Orientation ==")
try:
    from keysight.ads.de._pde.db import Orientation
    print(" ", [n for n in dir(Orientation) if not n.startswith("_")])
except Exception as e:
    print("  失败:", e)

print("\n== ws.design / cell.view ==")
ws = de.active_workspace()
print("ws.design:", inspect.signature(ws.design))
lib = [l for l in ws.libraries if str(l.name) == "AI_lib"][0]
c = [x for x in lib.cells if str(x.name) == "Wilkinson_2G4"][0]
for m in ("view", "get_view_if_exists", "view_exists", "create_view"):
    if hasattr(c, m): print(f"  cell.{m}:", inspect.signature(getattr(c, m)))

print("\n== 端口 / 控制器 在哪个库 ==")
for libname in ("ads_sources", "ads_simulation", "ads_tlines", "ads_rflib", "ads_datacmps"):
    ls = [l for l in ws.libraries if str(l.name) == libname]
    if not ls:
        print(f"{libname}: 未挂载"); continue
    names = [str(x.name) for x in ls[0].cells]
    print(f"\n{libname} ({len(names)}):")
    for kw in ("Term", "PORT", "SP", "NWA", "MSUB", "MLIN", "MTEE", "VAR", "R", "S_Param", "SParam"):
        hit = [n for n in names if kw.lower() in n.lower()][:8]
        if hit: print(f"   {kw}: {hit}")
