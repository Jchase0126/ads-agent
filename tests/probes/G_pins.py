import importlib.util
spec = importlib.util.spec_from_file_location("ads_ops_new", r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m._open_design("AI_lib", "Wilkinson_2G4", "schematic", write=False)

insts = list(d.instances)          # 只消费一次
print("实例数:", len(insts))
for i in insts[:6]:
    pins = list(i.inst_pins)       # 也是迭代器，同样只取一次
    print(f"  {getattr(i,'inst_name',None)!s:8} {type(i).__name__:12} pins={len(pins)}")
    if pins:
        sp = pins[0].snap_point
        print("     snap:", type(sp).__name__, "x/y:", getattr(sp,'x',None), getattr(sp,'y',None),
              "| 下标:", (lambda: (sp[0], sp[1]))() if hasattr(sp,'__getitem__') else "不支持")
        break
print("\n-- 关键点：d.instances / inst_pins 是不是一次性迭代器？")
print("  再次 list(d.instances) ->", len(list(d.instances)))
i0 = insts[0]
print("  同一实例再 list(inst_pins) ->", len(list(i0.inst_pins)), "(首次:", len(list(insts[0].inst_pins)), ")")
