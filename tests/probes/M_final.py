"""最终验收：补 MSUB 基板 -> 保存 -> 自检 -> 网表。

之后在外面调 run_simulation 应该能真正产出 .ds 数据集。
"""
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
import keysight.ads.de._pde.db as pdb
import importlib.util

SPEC = r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py"
spec = importlib.util.spec_from_file_location("ads_ops_probe", SPEC)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

LIB, CELL = "AI_lib", "AI_probe_k"
NAME = f"{LIB}:{CELL}:schematic"


def setp(inst, name, value):
    for p in list(inst.parameters):
        if str(p.name) == name:
            p.value = str(value)
            return f"{name}={value}"
    raise KeyError(f"{inst.name} 没有参数 {name}")


d = db_uu.open_design(NAME, mode=pdb.DesignMode.WRITE)
print("实例:", [i.name for i in list(d.instances)])

# 已有 MSUB 就不重复放
names = {i.name for i in list(d.instances)}
if "MSUB1" not in names:
    msub = d.add_instance(de.CellviewRef.make("ads_tlines:MSUB:symbol"),
                          (0.0, 2000.0), name="MSUB1")
    print("已放 MSUB1")
else:
    msub = d.find_instance("MSUB1")
    print("MSUB1 已存在")

print("基板:", setp(msub, "H", "1.6 mm"), setp(msub, "Er", "4.4"),
      setp(msub, "T", "0.035 mm"), setp(msub, "TanD", "0.02"),
      setp(msub, "Cond", "5.8E7"))

ml = d.find_instance("ML1")
print("ML1 Subst:", setp(ml, "Subst", "MSUB1"))

d.save_design()
print("SAVED")

print("\n--- 自检 ---")
d2 = db_uu.open_design(NAME, mode=pdb.DesignMode.READ_ONLY)
print("实例:", [(i.name, i.cell_name) for i in list(d2.instances)])
try:
    nl = str(d2.generate_netlist())
    print("网表长度:", len(nl))
    print(nl[:700])
except Exception as e:
    print("网表失败:", type(e).__name__, e)
