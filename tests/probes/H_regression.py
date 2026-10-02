import importlib.util
spec = importlib.util.spec_from_file_location("ads_ops_new", r"D:/Antenna/ADS Agent/addon/ads_agent/ads_ops.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
env = m._python_env()

print("【回归 1】get_design_variables（对话里 3 次失败）")
for cell in ("Wilkinson_2G4", "Wilkinson_2G4_v2"):
    try:
        print("  ", cell, "->", str(m.get_design_variables({"library":"AI_lib","cell":cell}))[:150])
    except Exception as e:
        print("  ", cell, "-> 仍失败:", type(e).__name__, str(e)[:80])

print("\n【回归 2】helper 都在位")
need = {"odesign": "开设计", "put": "放元件", "wire": "连线", "connect": "按引脚连线",
        "save": "保存", "pins": "查引脚", "cells": "查库元件", "libs": "查库", "sig": "查签名"}
missing = [k for k in need if k not in env]
print("  缺失:", missing or "无")

print("\n【回归 3】元件位置（对话里 Term/SP 找错库）")
print("  ads_simulation 含 Term:", env["cells"]("ads_simulation", "Term")[:3])
print("  ads_simulation 含 S_Param:", env["cells"]("ads_simulation", "S_Param"))
print("  ads_tlines 含 MLIN/MTEE/MSUB:",
      env["cells"]("ads_tlines", "MLIN"), env["cells"]("ads_tlines", "MTEE")[:2],
      env["cells"]("ads_tlines", "MSUB")[:2])
print("  ads_datacmps 含 VAR:", env["cells"]("ads_datacmps", "VAR"))
print("  ads_rflib 含 R:", env["cells"]("ads_rflib", "R")[:3])

print("\n【回归 4】create_view / view 的正确用法")
import keysight.ads.de as de
ws = env["ws"]()
lib = [l for l in ws.libraries if str(l.name) == "AI_lib"][0]
c = [x for x in lib.cells if str(x.name) == "Wilkinson_2G4"][0]
print("  cell.view:", env["sig"](c, "view"), "| create_view:", env["sig"](c, "create_view"))
print("  get_view 存在?", hasattr(c, "get_view"), "（应为 False，所以用 view()）")

print("\n【回归 5】ws.design 参数个数")
import inspect
print("  ", str(inspect.signature(ws.design))[:90])
