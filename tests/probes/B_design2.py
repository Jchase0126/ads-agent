import importlib, inspect
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu

ws = de.active_workspace()
lib = [l for l in ws.libraries if str(l.name) == "AI_lib"][0]
c = [x for x in lib.cells if str(x.name) == "Wilkinson_2G4"][0]
v = list(c.views)[0]
print("== View ==", type(v).__name__)
print("  name:", getattr(v, "name", None), "| view_type_name:", getattr(v, "view_type_name", None),
      "| lcv_name:", getattr(v, "lcv_name", None))
print("  成员:", [n for n in dir(v) if not n.startswith("_")][:40])

d = db_uu.open_design("AI_lib:Wilkinson_2G4:schematic", mode=db_uu.DesignMode.READ_ONLY)
print("\n== Design 全成员 ==")
allm = [n for n in dir(d) if not n.startswith("_")]
print(" ".join(allm))
print("\n含 create/add:", [n for n in allm if "create" in n or "add" in n])
print("含 save:", [n for n in allm if "save" in n])
print("含 wire/net/conn:", [n for n in allm if any(k in n for k in ("wire", "net", "conn"))])
for m in ("create_instance", "add_instance", "create_inst", "save_design", "save"):
    if hasattr(d, m):
        try: print(f"  {m}{inspect.signature(getattr(d,m))}")
        except Exception: print(f"  {m}")

print("\n== Point 类型在哪 ==")
for m in ("keysight.ads.de.db_uu._db_x", "keysight.ads.de._pde.db", "keysight.ads.de.db_uu"):
    try:
        mod = importlib.import_module(m)
        print(" ", m, "->", [n for n in dir(mod) if "Point" in n])
    except Exception as e:
        print(" ", m, "导入失败", e)

print("\n== 实例 ==")
insts = list(d.instances)
print("数量:", len(insts))
if insts:
    i0 = insts[0]
    print("类型:", type(i0).__name__)
    print("全成员:", " ".join([n for n in dir(i0) if not n.startswith("_")]))
