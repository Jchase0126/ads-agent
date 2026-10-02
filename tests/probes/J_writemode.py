"""核心验证：db_uu.open_design 在 WRITE 模式下拿到的是不是同一个设计。

如果 WRITE 打开返回空设计，那么所有"写入"都是在一个空壳上做的，
再 save_design() 会把原设计覆盖成空 —— 这足以解释"元件丢了 / 无法仿真"。
"""
import importlib.util

import keysight.ads.de.db_uu as db_uu
import keysight.ads.de as de

SPEC = r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py"
spec = importlib.util.spec_from_file_location("ads_ops_probe", SPEC)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

NAME = "AI_lib:Wilkinson_2G4:schematic"
RO = ops._design_mode(False)
WR = ops._design_mode(True)


def snap(tag, d):
    insts = list(d.instances)
    print(f"  [{tag}] 实例数={len(insts)}  设计对象={d!r}")
    for i in insts[:10]:
        print("      -", repr(getattr(i, "name", None)),
              repr(getattr(i, "instance_name", None)),
              type(i).__name__,
              getattr(i, "master_name", None) or getattr(i, "cell_name", None))
    return len(insts)


print("=== A) 全新进程状态：先 WRITE 打开 ===")
try:
    dw = db_uu.open_design(NAME, mode=WR)
    n_w = snap("WRITE", dw)
except Exception as e:
    print("  WRITE 打开失败:", type(e).__name__, e)
    n_w = None

print("\n=== B) 再 READ_ONLY 打开 ===")
try:
    dr = db_uu.open_design(NAME, mode=RO)
    n_r = snap("READ_ONLY", dr)
except Exception as e:
    print("  READ_ONLY 打开失败:", type(e).__name__, e)
    n_r = None

print(f"\n  >> WRITE={n_w}  READ_ONLY={n_r}  {'不一致！' if n_w != n_r else '一致'}")

print("\n=== C) 磁盘真相：走 Cell/View 路径 ===")
lib = de.get_open_library("AI_lib")
for cn in ("Wilkinson_2G4", "Wilkinson_2G4_v2"):
    c = lib.get_cell_if_exists(cn)
    if c is None:
        print(f"  {cn}: 不存在")
        continue
    v = c.get_view_if_exists("schematic")
    if v is None:
        print(f"  {cn}: 无 schematic 视图")
        continue
    print(f"  {cn}: 视图 {v}")
    try:
        dd = v.get_design()
        print("      默认(READ_ONLY) 实例数:", len(list(dd.instances)))
    except Exception as e:
        print("      默认打开失败:", type(e).__name__, e)
    try:
        import keysight.ads.de._pde.db as pdb
        dd2 = v.get_design(pdb.DesignMode.WRITE)
        print("      WRITE         实例数:", len(list(dd2.instances)))
    except Exception as e:
        print("      WRITE 打开失败:", type(e).__name__, e)

print("\n=== D) 磁盘文件 ===")
import os
for cn in ("Wilkinson_2G4", "Wilkinson_2G4_v2"):
    p = os.path.join(r"E:\ADS\Work_AI\AI_wrk", "AI_lib", cn, "schematic")
    print(f"  {cn}: exists={os.path.exists(p)}")
    if os.path.exists(p):
        print("      ", os.listdir(p))

print("\n=== E) 实例属性名探查（name vs instance_name）===")
try:
    d = db_uu.open_design(NAME, mode=RO)
    for i in list(d.instances)[:3]:
        print("  ", type(i).__name__, "| name=", repr(getattr(i, "name", "N/A")),
              "| instance_name=", repr(getattr(i, "instance_name", "N/A")),
              "| is_var_instance=", repr(getattr(i, "is_var_instance", "N/A")))
        print("     成员含 var/param 的:", [m for m in dir(i) if "var" in m.lower() or "param" in m.lower()])
except Exception as e:
    print("  失败:", type(e).__name__, e)
