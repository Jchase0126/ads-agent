import importlib.util
spec = importlib.util.spec_from_file_location("ads_ops_new", r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m._open_design("AI_lib", "Wilkinson_2G4", "schematic", write=False)
env = m._python_env()
for inst in list(d.instances):
    ps = env["pins"](inst)
    if ps:
        print("实例:", getattr(inst, "inst_name", None), type(inst).__name__)
        for p in ps[:4]: print("   ", p)
        p0 = list(inst.inst_pins)[0]
        sp = p0.snap_point
        print("snap_point 类型:", type(sp).__name__)
        print("  有 x/y?", hasattr(sp, "x"), hasattr(sp, "y"),
              "| x,y =", getattr(sp, "x", None), getattr(sp, "y", None))
        print("  可下标?", end=" ")
        try: print(sp[0], sp[1])
        except Exception as e: print("否", type(e).__name__)
        break
else:
    print("所有实例都没有引脚（都是 VAR 之类？）",
          [getattr(i, "inst_name", None) for i in d.instances][:8])
