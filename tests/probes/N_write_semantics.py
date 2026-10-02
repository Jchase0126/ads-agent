"""搞清楚 open_design(WRITE) 的语义：返回的是磁盘上的设计，还是一个新的空设计？

关键问题：WRITE 打开后 save_design() 会不会把原设计覆盖成空？
"""
import keysight.ads.de.db_uu as db_uu
import keysight.ads.de._pde.db as pdb

NAME = "AI_lib:AI_probe_k:schematic"
RO, WR = pdb.DesignMode.READ_ONLY, pdb.DesignMode.WRITE


def count(tag, mode, close=True):
    d = db_uu.open_design(NAME, mode=mode)
    n = len(list(d.instances))
    names = [i.name for i in list(d.instances)]
    print(f"  {tag:28} -> {n} 个 {names}")
    if close:
        try:
            d.close_design()
        except Exception as e:
            print("      close 失败:", type(e).__name__, e)
    return n, d


print("=== 1) 先只读确认磁盘真相 ===")
count("READ_ONLY (close)", RO)

print("\n=== 2) WRITE 打开，不改动，直接数 ===")
n_w, d_w = count("WRITE (不 close)", WR, close=False)

print("\n=== 3) 同一个 WRITE 句柄再数一次 ===")
print("   ->", len(list(d_w.instances)), "个")

print("\n=== 4) WRITE 句柄不关，再开 READ_ONLY ===")
count("READ_ONLY (WRITE 句柄仍开着)", RO, close=True)

print("\n=== 5) 关掉 WRITE 句柄后再读 ===")
try:
    d_w.close_design()
    print("   WRITE 句柄已关")
except Exception as e:
    print("   close 失败:", type(e).__name__, e)
count("READ_ONLY (关之后)", RO)

print("\n=== 6) 危险测试：WRITE 打开后立刻 save（不改动）会不会清空 ===")
try:
    d2 = db_uu.open_design(NAME, mode=WR)
    before = len(list(d2.instances))
    print("   WRITE 打开时实例数:", before)
    d2.save_design()
    print("   已保存")
    try:
        d2.close_design()
    except Exception:
        pass
except Exception as e:
    print("   失败:", type(e).__name__, e)
count("保存后 READ_ONLY", RO)

print("\n=== 7) 不关闭多次 WRITE 打开 ===")
for i in range(3):
    d = db_uu.open_design(NAME, mode=WR)
    print(f"   第 {i+1} 次 WRITE: {len(list(d.instances))} 个")
