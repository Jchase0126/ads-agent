"""端到端：写模式建图 -> 保存 -> 重开 -> 生成网表 -> 仿真。

如果能跑通，说明链路本身没问题，之前的失败纯粹是"写操作没落盘"导致设计为空。
"""
import os
import traceback

import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
import keysight.ads.de._pde.db as pdb

LIB = "AI_lib"
CELL = "AI_probe_k"
VIEW = "schematic"
NAME = f"{LIB}:{CELL}:{VIEW}"

lib = de.get_open_library(LIB)
existing = [c.name for c in lib.cells]
print("现有 cell:", existing)

# 若已存在先删掉，保证干净
if CELL in existing:
    try:
        lib.delete_cell(CELL)
        print("已删除旧", CELL)
    except Exception as e:
        print("删除失败:", type(e).__name__, e)

print("\n=== 1) 建 cell + view ===")
import inspect
print("  create_cell sig:", inspect.signature(lib.create_cell))
cell = lib.create_cell(CELL)
print("cell:", cell)
print("  create_view sig:", inspect.signature(cell.create_view))
view = cell.create_view(VIEW, "schematic")
print("view:", view, "path:", getattr(view, "path", None))

print("\n=== 2) WRITE 模式打开 ===")
d = db_uu.open_design(NAME, mode=pdb.DesignMode.WRITE)
print("design:", d, "实例数:", len(list(d.instances)))

print("\n=== 3) 放元件 ===")
def put(libname, cellname, viewname, origin, name=None):
    ref = de.CellviewRef.make(f"{libname}:{cellname}:{viewname}")
    kw = {}
    if name:
        kw["name"] = name
    return d.add_instance(ref, tuple(origin), **kw)

try:
    t1 = put("ads_simulation", "Term", "symbol", (0, 0), "Term1")
    print("  Term1:", t1.name)
except Exception as e:
    print("  Term1 失败:", type(e).__name__, e)
    traceback.print_exc()

try:
    ml = put("ads_tlines", "MLIN", "symbol", (-10, 0), "ML1")
    print("  ML1:", ml.name)
except Exception as e:
    print("  ML1 失败:", type(e).__name__, e)

try:
    t2 = put("ads_simulation", "Term", "symbol", (-20, 0), "Term2")
    print("  Term2:", t2.name)
except Exception as e:
    print("  Term2 失败:", type(e).__name__, e)

print("\n=== 4) 保存 ===")
try:
    d.save_design()
    print("  save_design OK")
except Exception as e:
    print("  save 失败:", type(e).__name__, e)
    traceback.print_exc()

print("\n=== 5) 重新 READ_ONLY 打开，数实例 ===")
d2 = db_uu.open_design(NAME, mode=pdb.DesignMode.READ_ONLY)
insts = list(d2.instances)
print("  实例数:", len(insts))
for i in insts:
    print("    -", i.name, type(i).__name__)

print("\n=== 6) 生成网表 ===")
try:
    nl = d2.generate_netlist()
    print("  网表 OK:\n", str(nl)[:600])
except Exception as e:
    print("  网表失败:", type(e).__name__, e)
