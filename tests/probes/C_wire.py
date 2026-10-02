import inspect
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu

d = db_uu.open_design("AI_lib:Wilkinson_2G4:schematic", mode=db_uu.DesignMode.READ_ONLY)

def sg(o, l):
    try: return l + str(inspect.signature(o))
    except Exception as e: return l + f"<{type(e).__name__}>"

print("== Design 关键方法签名 ==")
for m in ("add_instance", "add_wire", "add_net", "find_or_add_net", "add_scalar_net",
          "add_pin", "add_term", "add_var_instance", "save_design", "find_instance", "add_line"):
    if hasattr(d, m):
        print(" ", sg(getattr(d, m), m))

print("\n== PointF ==")
try:
    from keysight.ads.de._points import PointF
    print("  from keysight.ads.de._points import PointF -> OK", PointF)
except Exception as e:
    print("  导入失败:", type(e).__name__, e)

print("\n== ItemInfo ==")
for m in ("keysight.ads.de", "keysight.ads.de.db_uu", "keysight.ads.de._pde.db"):
    try:
        mod = __import__(m, fromlist=["ItemInfo"])
        obj = getattr(mod, "ItemInfo", None)
        print(f"  {m}.ItemInfo =", obj)
        if obj is not None:
            print("     成员:", [n for n in dir(obj) if not n.startswith("_")][:30])
            for f in ("create_new_instance", "create_instance"):
                if hasattr(obj, f): print("     ", sg(getattr(obj, f), f))
    except Exception as e:
        print("  ", m, "失败", e)

print("\n== 实例：移动 / 朝向 / 删除 ==")
insts = list(d.instances)
if insts:
    i0 = insts[0]
    for m in ("move", "orient", "delete_object", "origin", "vars", "parameters", "pin", "inst_pins"):
        a = getattr(i0, m, None)
        if a is None:
            print(f"  {m}: 无"); continue
        if callable(a):
            print(" ", sg(a, m))
        else:
            print(f"  {m}: {type(a).__name__} = {str(a)[:80]}")

print("\n== InstPin / InstTerm 取名字的正确方式 ==")
if insts:
    i0 = insts[0]
    try:
        terms = list(i0.inst_terms)
        print("  inst_terms:", len(terms))
        if terms:
            t = terms[0]
            print("   term_name:", getattr(t, "term_name", None), "| term_number:", getattr(t, "term_number", None),
                  "| is_numbered:", getattr(t, "is_numbered", None))
    except Exception as e:
        print("  inst_terms 失败:", type(e).__name__, e)
    try:
        pins = list(i0.inst_pins)
        print("  inst_pins:", len(pins))
        if pins:
            p = pins[0]
            print("   master_pin:", getattr(p, "master_pin", None), "| net:", getattr(p, "net", None),
                  "| snap_point:", getattr(p, "snap_point", None))
    except Exception as e:
        print("  inst_pins 失败:", type(e).__name__, e)

print("\n== nets ==")
try:
    print(" ", [str(n) for n in list(d.nets)][:10])
except Exception as e:
    print("  nets 失败:", e)
