import inspect
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
import keysight.ads.de._pde.db as pdb

def sig(o, l=""):
    try: return l + str(inspect.signature(o))
    except Exception as e: return l + f"<{type(e).__name__}>"

def mem(o, contain="", n=70):
    try: return ", ".join([x for x in dir(o) if not x.startswith("_") and contain in x][:n])
    except Exception as e: return f"<{e}>"

print("== DesignMode 字符串取值 ==")
DM = pdb.DesignMode
for n in ("APPEND", "READ_ONLY", "WRITE"):
    v = getattr(DM, n)
    print(f"  {n}: int={int(v)} name={getattr(v,'name',None)!r} str={getattr(v,'str',None)!r}")

print("\n== 工作区 ==")
print("open:", de.workspace_is_open())
ws = de.active_workspace()
print("path:", ws.path)
print("libs:", list(ws.library_names)[:15])

print("\n== AI_lib 的 cell / view ==")
lib = [l for l in ws.libraries if str(l.name) == "AI_lib"]
lib = lib[0] if lib else None
print("AI_lib 找到:", lib is not None)
if lib:
    for c in list(lib.cells)[:8]:
        vs = [str(getattr(v, "name", v)) for v in (c.views or [])]
        print(f"  cell={c.name} views={vs}")
    c0 = list(lib.cells)[0]
    print("\nCell 成员(含 view):", mem(c0, "view"))
    print("create_view:", sig(c0.create_view, "create_view"))
    print("Cell 成员(含 create):", mem(c0, "create"))

print("\n== open_design 用 int / 枚举 / 字符串 ==")
for mode in (0, DM.READ_ONLY, "Read Only", "READ_ONLY", "Read-Only"):
    try:
        d = db_uu.open_design("AI_lib:Wilkinson_2G4:schematic", mode=mode)
        print(f"  mode={mode!r} -> OK ({type(d).__name__})")
        break
    except Exception as e:
        print(f"  mode={mode!r} -> {type(e).__name__}: {str(e)[:110]}")
