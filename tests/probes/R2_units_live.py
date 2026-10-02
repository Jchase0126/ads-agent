"""R2c：ivar/dvar 对象属性全枚举。"""
import keysight.ads.dataset as dataset

DATASET = r"E:\ADS\Work_AI\AI_wrk\ads_agent_sim\Wilkinson_5G9_20260925_002328\Wilkinson_5G9.ds"
vb = dataset.open(DATASET)["SP1.SP"]
print("ivars_count =", vb.ivars_count, " dvars_count =", vb.dvars_count, " ivar_rows =", vb.ivar_rows)
for i in range(vb.ivars_count):
    iv = vb.ivars(i) if callable(vb.ivars) else vb.ivars[i]
    print(f"-- ivar[{i}] 类型: {type(iv).__name__}")
    print("   成员:", [n for n in dir(iv) if not n.startswith("_")])
    for attr in ("name", "unit", "units", "attrs", "value", "values", "dtype", "fmt", "str"):
        try:
            v = getattr(iv, attr)
            v = v() if callable(v) else v
            s = str(v)
            print(f"   .{attr} =", s[:80])
        except Exception as e:
            print(f"   .{attr} 异常:", type(e).__name__)
for i in range(min(vb.dvars_count, 2)):
    dv = vb.dvars(i) if callable(vb.dvars) else vb.dvars[i]
    print(f"-- dvar[{i}] 类型: {type(dv).__name__}")
    print("   成员:", [n for n in dir(dv) if not n.startswith("_")])
    for attr in ("name", "unit", "units", "attrs", "unit_string"):
        try:
            v = getattr(dv, attr)
            v = v() if callable(v) else v
            print(f"   .{attr} =", str(v)[:80])
        except Exception as e:
            print(f"   .{attr} 异常:", type(e).__name__)
df = vb.to_dataframe()
print("to_dataframe 列:", df.columns.tolist())
print("index:", df.index.name, df.index[:2].tolist())
