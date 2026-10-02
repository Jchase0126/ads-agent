"""诊断：为什么仿真跑不起来。

关注四件事：
  1. 运行中的 ADS 插件是否已加载修复后的 ads_ops.py（DesignMode）
  2. 设计里到底有没有实例（"has no instances" 的根因）
  3. design.instances 是不是一次性迭代器
  4. generate_netlist / 仿真控制器是否存在
"""
import importlib.util
import json
import traceback

import keysight.ads.de as de
import keysight.ads.de.db_uu as db_uu

SPEC = r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py"
spec = importlib.util.spec_from_file_location("ads_ops_probe", SPEC)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

print("=== 0) 工作区 / 库 ===")
ws = de.active_workspace()
print("workspace:", ws.path)
print("libs:", list(ws.library_names or []))
print("writable:", list(ws.writable_library_names or []))
print("AI_lib read_only:", de.library_is_read_only("AI_lib"))

print("\n=== 1) 修复后的 _design_mode ===")
print("  read ->", repr(ops._design_mode(False)))
print("  write->", repr(ops._design_mode(True)))

CELLS = ["Wilkinson_2G4", "Wilkinson_2G4_v2"]

print("\n=== 2) 各 cell 的实例数（READ_ONLY 打开）===")
for cn in CELLS:
    try:
        d = ops._open_design("AI_lib", cn, "schematic", write=False)
    except Exception as e:
        print(f"  {cn}: 打开失败 {type(e).__name__}: {e}")
        continue
    try:
        a = list(d.instances)
        b = list(d.instances)          # 再取一次：判断是否一次性迭代器
        print(f"  {cn}: 第一次 {len(a)} 个, 第二次 {len(b)} 个  "
              f"({'一次性迭代器!' if len(a) != len(b) else '可重复迭代'})")
        for i in a[:12]:
            print("      -", getattr(i, "instance_name", None),
                  type(i).__name__,
                  getattr(i, "master_name", None) or getattr(i, "cell_name", None))
    except Exception as e:
        print(f"  {cn}: instances err {type(e).__name__}: {e}")
    try:
        ops._close_design(d)
    except Exception:
        pass

print("\n=== 3) VAR 变量（走修复后的 get_design_variables）===")
for cn in CELLS:
    try:
        r = ops.get_design_variables({"library": "AI_lib", "cell": cn})
        print(f"  {cn}:", json.dumps(r, ensure_ascii=False)[:400])
    except Exception as e:
        print(f"  {cn}: 失败 {type(e).__name__}: {e}")

print("\n=== 4) generate_netlist ===")
for cn in CELLS:
    try:
        d = ops._open_design("AI_lib", cn, "schematic", write=False)
    except Exception as e:
        print(f"  {cn}: 打开失败 {e}")
        continue
    try:
        nl = d.generate_netlist()
        txt = nl if isinstance(nl, str) else str(nl)
        print(f"  {cn}: 网表生成成功, {len(txt)} 字符")
        print("  前 800 字符:\n" + txt[:800])
    except Exception as e:
        print(f"  {cn}: 网表失败 {type(e).__name__}: {e}")
    ops._close_design(d)

print("\n=== 5) 仿真控制器 API 探查 ===")
try:
    from keysight.edatoolbox import ads as eda
    print("  eda.CircuitSimulator:", eda.CircuitSimulator)
    import inspect
    try:
        print("  run_netlist sig:", inspect.signature(eda.CircuitSimulator.run_netlist))
    except Exception as e:
        print("  run_netlist sig err:", e)
except Exception as e:
    print("  edatoolbox 导入失败:", type(e).__name__, e)

print("\n=== 6) db_uu.open_design WRITE 落盘验证 ===")
try:
    d = ops._open_design("AI_lib", "Wilkinson_2G4", "schematic", write=True)
    print("  WRITE 打开成功:", d)
    before = len(list(d.instances))
    print("  当前实例数:", before)
    print("  hasattr save_design:", hasattr(d, "save_design"))
    ops._close_design(d)
except Exception as e:
    print("  WRITE 打开失败:", type(e).__name__, e)
    traceback.print_exc()
