"""验收：写模式建图 -> 接线 -> 设扫描 -> 保存 -> 自检 -> 生成网表。

跑完这个之后在外面调 run_simulation 应当能真正产出 .ds。
"""
import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu
import keysight.ads.de._pde.db as pdb

LIB, CELL = "AI_lib", "AI_probe_k"
NAME = f"{LIB}:{CELL}:schematic"

def setp(inst, name, value):
    """设置实例参数。parameters 是 list[Param]，只能用名字找再改 .value。"""
    for p in list(inst.parameters):
        if str(p.name) == name:
            p.value = str(value)
            return f"{name}={value}"
    raise KeyError(f"{inst.name} 没有参数 {name}；可用: {[str(p.name) for p in list(inst.parameters)][:20]}")


d = db_uu.open_design(NAME, mode=pdb.DesignMode.WRITE)
print("打开(WRITE) 实例数:", len(list(d.instances)))

# 清掉旧的，重建
for i in list(d.instances):
    try:
        i.delete_object()
    except Exception as e:
        print("  删除失败", i.name, e)

def put(master, x, y, name):
    return d.add_instance(de.CellviewRef.make(master), (float(x), float(y)), name=name)

t1 = put("ads_simulation:Term:symbol", 0.0, 0.0, "Term1")
ml = put("ads_tlines:MLIN:symbol", 1500.0, 0.0, "ML1")
t2 = put("ads_simulation:Term:symbol", 3000.0, 0.0, "Term2")
sp = put("ads_simulation:S_Param:symbol", 0.0, -2000.0, "SP1")
print("放置:", [i.name for i in list(d.instances)])

print("设置 MLIN:", setp(ml, "W", "3.06 mm"), setp(ml, "L", "17.3 mm"))
print("设置 SP1 :", setp(sp, "Start", "2.0 GHz"), setp(sp, "Stop", "3.0 GHz"),
      setp(sp, "Step", "0.01 GHz"))

# 接线：Term1(1) - ML1(1)；ML1(2) - Term2(1)
try:
    d.add_wire([(0.0, 0.0), (1500.0, 0.0)])
    d.add_wire([(1500.0, 0.0), (3000.0, 0.0)])
    print("连线完成")
except Exception as e:
    print("连线失败:", type(e).__name__, e)

d.save_design()
print("SAVED")

print("\n--- 重开只读自检 ---")
d2 = db_uu.open_design(NAME, mode=pdb.DesignMode.READ_ONLY)
insts = list(d2.instances)
print("实例数:", len(insts))
for i in insts:
    print("   ", i.name, "/", i.cell_name)

print("\n--- 网表 ---")
try:
    nl = d2.generate_netlist()
    print(str(nl)[:900])
except Exception as e:
    print("网表失败:", type(e).__name__, e)
