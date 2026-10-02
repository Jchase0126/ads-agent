import importlib.util
PATH = r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py"
spec = importlib.util.spec_from_file_location("ads_ops_new", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

print("== 1) _design_mode 取值 ==")
print("  read ->", repr(m._design_mode(False)))
print("  write->", repr(m._design_mode(True)))

print("\n== 2) 旧写法复现（应当失败）==")
import keysight.ads.de.db_uu as db_uu
try:
    db_uu.open_design("AI_lib:Wilkinson_2G4:schematic", mode="READ_ONLY")
    print("  旧写法竟然成功了？")
except Exception as e:
    print("  旧写法失败（预期）:", type(e).__name__ + ":", str(e)[:90])

print("\n== 3) 新 _open_design 只读 ==")
d = m._open_design("AI_lib", "Wilkinson_2G4", "schematic", write=False)
print("  OK ->", type(d).__name__, "| instances:", len(list(d.instances)))

print("\n== 4) 预置环境 helper 是否可用 ==")
env = m._python_env()
print("  helper:", sorted(k for k in env if not k.startswith("_")))
print("  libs():", list(env["libs"]())[:8])
print("  cells('ads_simulation','Term'):", env["cells"]("ads_simulation", "Term"))
print("  sig(d,'save_design'):", env["sig"](d, "save_design"))
i0 = list(d.instances)[0]
print("  pins(首个实例):", env["pins"](i0)[:3])

print("\n== 5) get_design_variables（之前必挂的工具）==")
print(" ", str(m.get_design_variables({"library": "AI_lib", "cell": "Wilkinson_2G4"}))[:300])
