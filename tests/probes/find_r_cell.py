"""找 R 元件所在库 + 热重载 ads_ops 并重建 Wilkinson_5G9_ML（对称布局）。"""
import json

import ads_ops

# 热重载：拿到刚改过的对称布局代码（toolserver 旧闭包不管，这里直接调新模块）
import importlib
importlib.reload(ads_ops)
print("ads_ops 已重载:", getattr(ads_ops.auto_layout_positions, "__code__").co_firstlineno)

de = ads_ops._de()
r_master = None
for lib in ("ads_simulation", "ads_rflib", "ads_tlines", "ads_behavior"):
    try:
        if de.get_open_library(lib).cell("R") is not None:
            r_master = lib + ":R"
            break
    except Exception:  # noqa: BLE001
        pass
print("R master =", r_master)
if not r_master:
    raise SystemExit("找不到 R 的库")

args = {
    "library": "AI_lib",
    "cell": "Wilkinson_5G9_ML",
    "recreate": True,
    "layout": "auto",
    "instances": [
        {"master": "ads_tlines:MSUB", "name": "MSUB1",
         "params": {"H": "20 mil", "Er": "4.4", "T": "1.4 mil",
                    "TanD": "0.02", "Cond": "1.0E+50"}},
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2", "Z": "50 Ohm"}},
        {"master": "ads_simulation:TermG", "name": "P3",
         "params": {"Num": "3", "Z": "50 Ohm"}},
        {"master": "ads_tlines:MLIN", "name": "ML1",
         "params": {"Subst": "MSUB1", "W": "W50", "L": "L50"}},
        {"master": "ads_tlines:MLIN", "name": "MLa",
         "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
        {"master": "ads_tlines:MLIN", "name": "MLb",
         "params": {"Subst": "MSUB1", "W": "W70", "L": "L70"}},
        {"master": r_master, "name": "RISO1", "params": {"R": "Riso"}},
        {"master": "ads_simulation:S_Param", "name": "SP1",
         "params": {"Start": "5.4 GHz", "Stop": "6.4 GHz", "Step": "0.01 GHz"}},
    ],
    "var": {"name": "VAR1", "values": {
        "X": "1.0", "W50": "38.2 mil", "L50": "100 mil", "W70": "20.3 mil",
        "L70": "287 mil", "Riso": "100 Ohm", "Z0": "50 Ohm", "f0": "5.9 GHz"}},
    "connections": [
        {"a": ["P1", "1"], "b": ["ML1", "1"]},
        {"a": ["ML1", "2"], "b": ["MLa", "1"]},
        {"a": ["ML1", "2"], "b": ["MLb", "1"]},
        {"a": ["MLa", "2"], "b": ["P2", "1"]},
        {"a": ["MLb", "2"], "b": ["P3", "1"]},
        {"a": ["MLa", "2"], "b": ["RISO1", "2"]},
        {"a": ["MLb", "2"], "b": ["RISO1", "1"]},
    ],
}
res = ads_ops.build_schematic(args)
out = json.dumps(res, ensure_ascii=False, default=str)
print("RESULT:", out[:2500])
