"""找正确的"打开已有设计并追加修改"的方式。

已确认：WRITE 模式 = 打开一个空白可写设计，save 会把磁盘上的内容整个覆盖。
候选：DesignMode.APPEND、View.get_design(WRITE)、View.get_design(APPEND)。
"""
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
import keysight.ads.de._pde.db as pdb

LIB, CELL = "AI_lib", "AI_probe_k"
NAME = f"{LIB}:{CELL}:schematic"
MODE = pdb.DesignMode


def build():
    """用 WRITE 从零建一个 5 元件的图（这条路径已验证可靠）。"""
    d = db_uu.open_design(NAME, mode=MODE.WRITE)
    def put(m, x, y, n):
        return d.add_instance(de.CellviewRef.make(m), (float(x), float(y)), name=n)
    put("ads_simulation:Term:symbol", 0, 0, "Term1")
    put("ads_tlines:MLIN:symbol", 1500, 0, "ML1")
    put("ads_simulation:Term:symbol", 3000, 0, "Term2")
    put("ads_simulation:S_Param:symbol", 0, -2000, "SP1")
    put("ads_tlines:MSUB:symbol", 0, 2000, "MSUB1")
    d.save_design()
    try:
        d.close_design()
    except Exception:
        pass


def probe(tag, fn):
    try:
        d = fn()
        names = [i.name for i in list(d.instances)]
        print(f"  {tag:34} -> {len(names)} 个 {names}")
        try:
            d.close_design()
        except Exception:
            pass
        return len(names)
    except Exception as e:
        print(f"  {tag:34} -> 失败 {type(e).__name__}: {e}")
        return None


print("=== 0) 重建一个有 5 个元件的设计 ===")
build()
probe("READ_ONLY 复核", lambda: db_uu.open_design(NAME, mode=MODE.READ_ONLY))

print("\n=== 1) db_uu.open_design 各模式 ===")
probe("open_design(READ_ONLY)", lambda: db_uu.open_design(NAME, mode=MODE.READ_ONLY))
probe("open_design(WRITE)", lambda: db_uu.open_design(NAME, mode=MODE.WRITE))
probe("open_design(APPEND)", lambda: db_uu.open_design(NAME, mode=MODE.APPEND))

print("\n=== 2) View.get_design 各模式 ===")
lib = de.get_open_library(LIB)
v = lib.get_cell_if_exists(CELL).get_view_if_exists("schematic")
probe("view.get_design()", lambda: v.get_design())
probe("view.get_design(READ_ONLY)", lambda: v.get_design(MODE.READ_ONLY))
probe("view.get_design(WRITE)", lambda: v.get_design(MODE.WRITE))
probe("view.get_design(APPEND)", lambda: v.get_design(MODE.APPEND))

print("\n=== 3) DesignMode 成员全貌 ===")
print("  ", [(m, getattr(MODE, m).value if hasattr(getattr(MODE, m, None), "value") else None)
             for m in dir(MODE) if not m.startswith("_")])
