"""Q3：DDSFile / Page 建页方法签名 + is_dds_app 状态（只枚举）。"""
import inspect
import keysight.ads.dds as dds

print("is_dds_app:", getattr(dds, "is_dds_app", lambda: "?")())
print("running_automation:", getattr(dds, "running_automation", lambda: "?")())
print("product_version:", getattr(dds, "product_version", lambda: "?")())
print()
for cls_name in ("DDSFile", "Page", "RectPlot", "Plot", "Window", "Trace"):
    cls = getattr(dds, cls_name, None)
    if cls is None:
        continue
    print(f"== {cls_name}")
    for n in dir(cls):
        if n.startswith("_") or n in ("Iter",):
            continue
        v = getattr(cls, n)
        if inspect.isfunction(v) or inspect.ismethod(v):
            try:
                sig = str(inspect.signature(v))[:140]
            except Exception:
                sig = "(?)"
            if any(k in n for k in ("add", "new", "save", "open", "close", "trace", "plot", "page", "title", "name", "path")):
                print(f"   .{n}{sig}")
    print()
