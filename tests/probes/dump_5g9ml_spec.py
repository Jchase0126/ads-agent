"""摸清 Wilkinson_5G9_ML 的完整可重放规格：全部实例（含 var 类）、参数、变量。"""
import ads_ops


def params_str(inst):
    out = {}
    try:
        for p in inst.parameters:
            out[str(p.name)] = str(p.value)
    except Exception:  # noqa: BLE001
        try:
            out = dict(inst.params)
        except Exception:  # noqa: BLE001
            out = {"<unavailable>": "?"}
    return out


d = ads_ops._open_design("AI_lib", "Wilkinson_5G9_ML", "schematic", write=False)

print("== all instances ==")
for inst in list(d.instances):
    try:
        iv = getattr(inst, "is_var_instance", None)
        print(ads_ops._inst_name(inst), "|", ads_ops._inst_master(inst), "| var:", iv)
        try:
            print("    params:", dict(inst.vars) if iv else params_str(inst))
        except Exception as e:  # noqa: BLE001
            print("    params ?", type(e).__name__, e)
    except Exception as e:  # noqa: BLE001
        print("??", type(e).__name__, e)

print("== design variables ==")
try:
    print(dict(d.design_variables))
except Exception as e:  # noqa: BLE001
    print("design_variables ?", type(e).__name__, e)
