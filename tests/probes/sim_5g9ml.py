"""重建后的 Wilkinson_5G9_ML 仿真回归：读带内 6 项指标与基线比对。"""
import json

import ads_ops

res = ads_ops.run_simulation({"library": "AI_lib", "cell": "Wilkinson_5G9_ML"})
print("sim keys:", sorted(res.keys()))
ds = res.get("dataset_path") or res.get("dataset")
print("status:", res.get("status"), "| dataset:", ds)
if not ds:
    print(json.dumps(res, default=str)[:800])
    raise SystemExit("没有数据集")

exprs = ["dB(S(1,1))", "dB(S(2,1))", "dB(S(3,1))", "dB(S(2,3))", "dB(S(2,2))", "dB(S(3,3))"]
tr = ads_ops.read_traces({"path": ds, "expressions": exprs, "max_points": 0})
traces = tr.get("traces") or {}

baseline = {
    "dB(S(1,1))": ("max_in_band", -28.8059, 5.73e9),   # 带内最大
    "dB(S(2,1))": ("min_in_band", -3.1815, 5.73e9),    # 带内最小(最差点)
    "dB(S(2,3))": ("max_in_band", -39.4842, 5.73e9),
    "dB(S(2,2))": ("max_in_band", -38.6265, 5.73e9),
}

def in_band(xs, ys, lo=5.725e9, hi=5.925e9, agg=max):
    pts = [(x, y) for x, y in zip(xs, ys) if lo <= x <= hi]
    x, y = agg(pts, key=lambda t: t[1])
    return x, y

# S(3,1) 与 S(2,1) 应几乎重合；S(3,3) 与 S(2,2) 同
for expr, xs in ((e, (traces.get(e) or {})) for e in exprs):
    xsv = xs.get("x") or []
    ysv = xs.get("y") or []
    if not xsv:
        print(f"{expr}: 无数据 keys={list(xs)[:5]}")
        continue
    if expr == "dB(S(1,1))":
        x, y = in_band(xsv, ysv, agg=max)
        ref = baseline[expr][1]
        print(f"{expr}: 带内最大 {y:.4f} dB @ {x:.3e}（基线 {ref}，差 {y-ref:+.4f}）")
    elif expr == "dB(S(2,1))":
        x, y = in_band(xsv, ysv, agg=min)
        ref = baseline["dB(S(2,1))"][1]
        print(f"{expr}: 带内最小 {y:.4f} dB @ {x:.3e}（基线 {ref}，差 {y-ref:+.4f}）")
    elif expr in ("dB(S(2,3))", "dB(S(2,2))"):
        x, y = in_band(xsv, ysv, agg=max)
        ref = baseline[expr][1]
        print(f"{expr}: 带内最大 {y:.4f} dB @ {x:.3e}（基线 {ref}，差 {y-ref:+.4f}）")
    elif expr == "dB(S(3,1))":
        y_at = min(zip(xsv, ysv), key=lambda t: abs(t[0] - 5.73e9))[1]
        print(f"{expr}: @5.73GHz {y_at:.4f} dB（应≈-3.18）")
    elif expr == "dB(S(3,3))":
        x, y = in_band(xsv, ysv, agg=max)
        print(f"{expr}: 带内最大 {y:.4f} dB（应≈-38.63）")
