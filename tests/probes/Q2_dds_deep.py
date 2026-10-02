"""Q2：深挖 keysight.ads.dds 与 de.app.dds 的公开 API（只枚举，不调用）。"""
import inspect

def dump(mod_name, depth=0):
    print("=" * 70)
    print(f"模块 {mod_name}")
    print("-" * 70)
    try:
        mod = __import__(mod_name, fromlist=["*"])
    except Exception as e:
        print("  导入失败:", type(e).__name__, e)
        return None
    names = [n for n in dir(mod) if not n.startswith("_")]
    print("  公开成员:", names)
    return mod

dds = dump("keysight.ads.dds")
app_dds = dump("keysight.ads.de.app.dds")
ddp = dump("keysight.ads.dds.core.ddpage")
ddw = dump("keysight.ads.dds.core.ddwin")
ddl = dump("keysight.ads.dds.core.ddplot")

print("=" * 70)
print("重点对象的 docstring / 签名")
print("-" * 70)
for label, obj in [("dds.app?" , dds), ("app.dds", app_dds), ("ddpage", ddp), ("ddwin", ddw), ("ddplot", ddl)]:
    if obj is None:
        continue
    for n in dir(obj):
        if n.startswith("_"):
            continue
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if inspect.isclass(v) or inspect.isfunction(v):
            doc = (inspect.getdoc(v) or "").strip().splitlines()
            head = doc[0] if doc else ""
            try:
                sig = str(inspect.signature(v))[:120]
            except Exception:
                sig = ""
            print(f"  {label}.{n}{sig}")
            if head:
                print(f"      {head[:150]}")
