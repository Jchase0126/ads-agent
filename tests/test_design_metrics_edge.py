"""指标评估判定边界的回归（2026-10-02 优化轮，纯标准库）。

四类已由独立探针复现的问题（修复前都会给出错误的"达标"结论）：

1. 频段要求 1–3 GHz、数据只有 2–2.5 GHz —— 修复前带内指标照常判定达标；
   现在必须 blocked=band_coverage_insufficient / pass=None，
   显式 partial_band=True 时只评估覆盖区间并标注 eval_range。
2. 带内某点为 None，归一化丢弃后 min/max/flatness 仍标 exact=true 且达标；
   现在最值类指标 blocked=missing_points_in_band，mean/bandwidth 降级
   exact=False 并写明。
3. 请求 5 GHz 的值、数据只到 2 GHz —— 修复前就近取点照样判定；
   现在 blocked=out_of_range；范围内的最近点/插值区分 method 并降级 exact。
4. 曲线单位 V、指标单位 dB —— 修复前直接比较数值并把结果标成 dB；
   现在 blocked=unit_incompatible。

另覆盖：x/y 长度不一致、NaN/Inf、重复频点、非均匀扫频、0 Hz 起始频段、
阈值交点插值与端点截断、多区间不跨接、横轴单位不唯一、均值口径、
正常完整数据与旧评估结果兼容（test_design_metrics.py 全量通过）。

运行::

    python tests/test_design_metrics_edge.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")

import design_job as dj  # noqa: E402
import design_metrics as dm  # noqa: E402


def _payload(x, y, x_unit="GHz", y_unit="dB", **extra):
    p = {"x": list(x), "y": list(y), "x_name": "freq",
         "y_name": "dB(S(2,1))", "x_unit": x_unit, "y_unit": y_unit}
    p.update(extra)
    return p


def _sweep(lo, hi, n):
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _eval(payloads, metrics, band, options=None):
    traces = dm.normalize_traces(payloads)
    return dm.evaluate(traces, metrics, band, available=list(payloads),
                       options=options)


GAIN = {"id": "gain", "label": "带内增益", "kind": "min_in_band",
        "expr": "dB(S(2,1))", "target": 15.0, "unit": "dB"}


# ---------------------------------------------------------------------------
# 问题 1：频段覆盖不足
# ---------------------------------------------------------------------------

def test_band_partially_covered_is_blocked_not_judged():
    """频段 1–3 GHz、数据 2–2.5 GHz：修复前会只拿覆盖部分判达标。"""
    x = _sweep(2.0, 2.5, 101)
    y = [18.0] * len(x)                       # 覆盖部分"看起来"完美达标
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [GAIN],
                dj.normalize_band({"start": 1.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None, "覆盖不足时绝不能给出达标结论")
    eq(r["actual"], None, "没有覆盖到的部分没有实测值")
    eq(r["blocked_reason"], "band_coverage_insufficient")
    contains(r["note"], "没有覆盖频段")
    contains(r["note"], "低端")
    contains(r["note"], "高端")
    eq(out["summary"]["verdict"], "unknown")
    eq(r["data_quality"]["coverage"], "partial")


def test_band_partially_covered_with_explicit_partial_option_marks_range():
    x = _sweep(2.0, 2.5, 101)
    y = [18.0] * len(x)
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [GAIN],
                dj.normalize_band({"start": 1.0, "stop": 3.0, "unit": "GHz"}),
                options={"partial_band": True})
    r = out["results"][0]
    eq(r["pass"], True, "显式选项下按覆盖区间判定")
    eq(r["exact"], False, "部分覆盖的结论必须降级为非精确")
    contains(r["note"], "只评估覆盖区间")
    contains(r["data_quality"].get("eval_range", ""), "2", "结论应标明实际评估范围")


def test_band_start_uncovered_blocks_even_if_stop_covered():
    x = _sweep(2.4, 3.0, 121)
    y = [18.0] * len(x)
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None)
    eq(r["blocked_reason"], "band_coverage_insufficient")
    contains(r["note"], "低端", "只缺低端时也应点名低端")


def test_fully_covered_band_still_judged_normally():
    x = _sweep(2.0, 3.0, 1001)
    y = [18.0 - 5.0 * ((v - 2.4) / 0.6) ** 2 for v in x]
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True)
    eq(r["exact"], True)
    eq(r["blocked_reason"], "")
    eq(r["data_quality"]["coverage"], "full")


# ---------------------------------------------------------------------------
# 问题 2：带内读不出的点
# ---------------------------------------------------------------------------

def test_dropped_point_in_band_blocks_extremum_metrics():
    x = _sweep(2.0, 3.0, 1001)
    y = [18.0 - 5.0 * ((v - 2.4) / 0.6) ** 2 for v in x]
    y[350] = None                              # 带内（2.35 GHz 附近）一个点读不出
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None, "最值可能恰好落在读不出的点上，不能判达标")
    eq(r["blocked_reason"], "missing_points_in_band")
    contains(r["note"], "读不出数值")
    eq(r["data_quality"]["dropped_in_band"], 1)


def test_dropped_point_degrades_mean_and_bandwidth_but_still_judges():
    x = _sweep(2.0, 3.0, 1001)
    y = [18.0 - 5.0 * ((v - 2.4) / 0.6) ** 2 for v in x]
    y[100] = float("nan")                      # 带外 NaN（2.1 GHz）
    y[400] = None                              # 带内（2.4 GHz）
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "m", "kind": "mean_in_band", "expr": "dB(S(2,1))", "target": 10.0, "unit": "dB"},
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.01, "threshold": 13.0, "unit": "GHz"},
    ], dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    mean_r, bw_r = out["results"]
    ok(mean_r["actual"] is not None, "均值可在有效点上计算")
    eq(mean_r["pass"], True)
    eq(mean_r["exact"], False, "带内丢点后均值不是精确结论")
    contains(mean_r["note"], "有效点")
    ok(bw_r["actual"] is not None)
    eq(bw_r["exact"], False)


def test_quality_records_raw_valid_dropped_counts():
    p = _payload([1, "bad", 3, None, 5], [10, 20, None, 40, 50])
    trace = dm.normalize_trace("t", p)
    q = trace["quality"]
    eq(q["n_raw"], 5)
    eq(q["n_valid"], 2)
    eq(q["n_dropped"], 3)
    eq(q["drop_reasons"]["x_invalid"], 2, "x='bad' 与 x=None")
    eq(q["drop_reasons"]["y_invalid"], 1)
    ok(3 in q["dropped_x"], "丢弃点的横轴位置应记录")


def test_pair_length_mismatch_is_recorded():
    trace = dm.normalize_trace("t", {"x": [1, 2, 3, 4, 5], "y": [10, 20, 30]})
    q = trace["quality"]
    eq(q["pair_mismatch"], 2, "x/y 长度不一致必须记录")
    eq(q["n_valid"], 3, "按短的一侧配对")


def test_evaluation_blocked_on_mismatched_arrays():
    out = dm.evaluate(
        {"t": {"name": "t", "x": [1.0, 2.0], "y": [1.0], "y_unit": "dB",
               "quality": {}, "display": {}}},
        [{"id": "g", "kind": "min_in_band", "expr": "t", "target": 15.0, "unit": "dB"}],
        dj.normalize_band({"start": 1.0, "stop": 2.0, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None)
    eq(r["blocked_reason"], "empty_or_mismatched_data")


# ---------------------------------------------------------------------------
# 问题 3：value_at 目标频点超出数据范围
# ---------------------------------------------------------------------------

def test_value_at_out_of_range_is_blocked_not_nearest_judged():
    x = _sweep(2.0, 2.0 + 0.5 / 999, 1000)[:1000]  # 2.0–2.5 GHz
    x = _sweep(2.0, 2.5, 1000)
    y = [18.0] * len(x)
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 1.0,
         "unit": "dB", "at_hz": 5.0e9},
    ], dj.normalize_band({"start": 2.0, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None, "数据只到 2.5 GHz，5 GHz 不能就近取点判定")
    eq(r["actual"], None, "没有实测值")
    eq(r["blocked_reason"], "out_of_range")
    contains(r["note"], "超出数据范围")
    eq(r["at_hz"], None)


def test_value_at_below_range_is_blocked_too():
    x = _sweep(2.0, 3.0, 1001)
    out = _eval({"dB(S(2,1))": _payload(x, [18.0] * len(x))}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 1.0,
         "at_hz": 0.5e9},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    eq(out["results"][0]["blocked_reason"], "out_of_range")


def test_value_at_nearest_is_labeled_and_degraded():
    x = _sweep(2.0, 3.0, 3)                    # 2.0 / 2.5 / 3.0
    out = _eval({"dB(S(2,1))": _payload(x, [16.0, 15.0, 14.0])}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 14.5,
         "at_hz": 2.52e9},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True, "范围内的最近点近似（2.5 GHz 处 15.0 ≥ 14.5）仍可判定")
    eq(r["method"], "nearest（最近点近似）")
    eq(r["exact"], False, "最近点近似必须降级 exact")
    contains(r["note"], "最近采样点")


def test_value_at_exact_grid_hit_stays_exact():
    x = _sweep(2.0, 3.0, 1001)
    out = _eval({"dB(S(2,1))": _payload(x, [16.0] * len(x))}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 16.0,
         "at_hz": 2.4e9},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True)
    eq(r["method"], "grid_hit（频点精确采样）")
    eq(r["exact"], True)
    eq(r["note"], "")


def test_value_at_linear_interp_option():
    x = [2.0, 3.0]
    out = _eval({"dB(S(2,1))": _payload(x, [10.0, 20.0])}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 14.0,
         "at_hz": 2.4e9, "interp": "linear"},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    ok(abs(r["actual"] - 14.0) < 1e-9, f"2.4 GHz 线性插值应为 14，实际 {r['actual']}")
    eq(r["method"], "linear_interp（相邻点线性插值）")
    eq(r["exact"], False, "插值结果不是精确采样")


def test_value_at_configurable_tolerance():
    x = _sweep(2.0, 3.0, 3)
    spec = {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 16.0,
            "at_hz": 2.6e9}
    out = _eval({"dB(S(2,1))": _payload(x, [16.0, 15.0, 14.0])}, [spec],
                dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    ok("偏差" in out["results"][0]["note"], "默认 2% 容差下 0.1 GHz 偏差应提示")
    out2 = _eval({"dB(S(2,1))": _payload(x, [16.0, 15.0, 14.0])}, [dict(spec, tol=0.5e9)],
                 dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    not_contains(out2["results"][0]["note"], "偏差", "显式放大容差后不再提示")


# ---------------------------------------------------------------------------
# 问题 4：单位不可比
# ---------------------------------------------------------------------------

def test_voltage_curve_vs_db_metric_is_incompatible():
    x = _sweep(2.0, 3.0, 101)
    out = _eval({"dB(S(2,1))": _payload(x, [0.5] * len(x), y_unit="V")}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None, "V 和 dB 不能直接比较数值")
    eq(r["actual"], None)
    eq(r["blocked_reason"], "unit_incompatible")
    eq(r["unit"], "dB", "单位字段保持声明值，不把结果标成曲线单位")
    contains(r["note"], "单位不可比")


def test_same_unit_still_judges():
    x = _sweep(2.0, 3.0, 101)
    out = _eval({"dB(S(2,1))": _payload(x, [18.0] * len(x), y_unit="dB")}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    eq(out["results"][0]["pass"], True)


def test_metric_unit_adopted_when_curve_unit_missing():
    x = _sweep(2.0, 3.0, 101)
    out = _eval({"dB(S(2,1))": _payload(x, [18.0] * len(x), y_unit="")}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True, "曲线未声明单位时采用指标单位")
    eq(r["unit"], "dB")


def test_dbm_curve_vs_db_metric_is_incompatible():
    x = _sweep(2.0, 3.0, 101)
    out = _eval({"P": _payload(x, [-5.0] * len(x), y_unit="dBm")}, [
        dict(GAIN, expr="P"),
    ], dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    eq(out["results"][0]["blocked_reason"], "unit_incompatible", "dBm 与 dB 语义不同")


def test_voltage_prefix_mismatch_is_incompatible():
    x = _sweep(2.0, 3.0, 101)
    out = _eval({"V": _payload(x, [0.5] * len(x), y_unit="V")}, [
        dict(GAIN, expr="V", unit="mV"),
    ], dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    eq(out["results"][0]["blocked_reason"], "unit_incompatible", "V 与 mV 差 1000 倍")


def test_bandwidth_above_unit_field_is_not_a_value_unit():
    """bandwidth_above 的 unit 是带宽单位（GHz），不应和曲线 dB 比较。"""
    x = _sweep(2.0, 3.0, 1001)
    y = [16.0 - 60.0 * ((v - 2.4) / 0.6) ** 2 for v in x]
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.1, "threshold": 15.0, "unit": "GHz"},
    ], dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["blocked_reason"], "", "带宽单位不是纵轴单位，不应触发单位拦截")
    eq(r["pass"], True)
    eq(r["unit"], "GHz")


# ---------------------------------------------------------------------------
# 频段与采样边界
# ---------------------------------------------------------------------------

def test_zero_hz_start_band_is_a_real_band():
    """0 Hz 起始（DC–1 GHz）不能被当成"未指定频段"。"""
    x = _sweep(0.0, 1.0, 101)
    y = [10.0 + 5.0 * v for v in x]
    out = _eval({"S": _payload(x, y, y_unit="dB")}, [
        dict(GAIN, expr="S", target=10.0),
    ], dj.normalize_band({"start": 0.0, "stop": 1.0, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["blocked_reason"], "", f"0 Hz 起始是合法频段，不应拦截：{r['note']}")
    eq(r["pass"], True)
    eq(r["n_points"], 101)


def test_single_point_in_band_is_sparse_blocked():
    x = [2.0, 2.4, 3.0]
    out = _eval({"dB(S(2,1))": _payload(x, [10.0, 18.0, 10.0])}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None)
    eq(r["blocked_reason"], "in_band_sampling_sparse")


def test_few_points_degrade_exactness_but_still_judge():
    x = [2.0, 2.35, 2.4, 2.45, 3.0]           # 带内 3 点
    out = _eval({"dB(S(2,1))": _payload(x, [10.0, 18.0, 18.0, 18.0, 10.0])}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True)
    eq(r["exact"], False, "采样稀疏时结论降级")
    contains(r["note"], "较少")


def test_duplicate_and_nonuniform_sweep_are_recorded():
    x = [2.0, 2.4, 2.4, 2.5, 3.0]
    trace = dm.normalize_trace("t", _payload(x, [1, 2, 3, 4, 5]))
    q = trace["quality"]
    eq(q["duplicate_x"], 1)
    eq(q["non_uniform"], True, "步长 0.4/0/0.1/0.5 明显非均匀")
    out = _eval({"dB(S(2,1))": _payload(x, [16.0, 16.0, 16.0, 16.0, 16.0])}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    eq(out["results"][0]["pass"], True, "重复/非均匀只记录不改变判定")


# ---------------------------------------------------------------------------
# 带宽口径：连续区间 + 阈值交点插值
# ---------------------------------------------------------------------------

def test_bandwidth_uses_linear_crossing_interpolation():
    x = [2.0, 2.4, 2.8]
    y = [14.0, 16.0, 14.0]                     # 两侧交点都在中点
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.1, "threshold": 15.0, "unit": "GHz"},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    ok(abs(r["actual"] - 0.4) < 1e-9,
       f"交点插值后带宽应为 0.4 GHz，实际 {r['actual']}")


def test_bandwidth_does_not_bridge_disjoint_intervals():
    x = [2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7]
    y = [16.0, 16.0, 10.0, 10.0, 16.0, 16.0, 10.0, 10.0]
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.13, "threshold": 15.0, "unit": "GHz"},
    ], dj.normalize_band({"start": 2.0, "stop": 2.7, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True, "最宽连续区间（含交点插值）≈0.133 GHz ≥ 0.13")
    ok(abs(r["actual"] - 0.1333333) < 1e-3,
       f"两段交点插值后约 0.117/0.133 GHz，取最宽 0.133，实际 {r['actual']}")
    ok(len(r.get("intervals") or []) == 2, "应报告两个不连续区间")
    contains(r["note"], "不连续", "绝不把两段跨接成 0.7 GHz")


def test_bandwidth_clipped_at_data_edge_is_marked():
    x = _sweep(2.0, 2.5, 51)
    y = [16.0] * len(x)                        # 全程高于门限，顶到数据两端
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.4, "threshold": 15.0, "unit": "GHz"},
    ], dj.normalize_band({"start": 2.0, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], True)
    eq(r["exact"], False, "顶到数据端点的带宽可能被截断，必须降级")
    contains(r["note"], "截断")
    ok(r["intervals"][0]["clipped"] is True)


def test_bandwidth_missing_point_inside_band_splits_interval():
    x = _sweep(2.0, 3.0, 1001)
    y = [16.0] * len(x)
    y[500] = None                              # 2.5 GHz 处读不出 —— 带内
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.8, "threshold": 15.0, "unit": "GHz"},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], False, "缺失点打断连续区间后带宽不足")
    eq(r["exact"], False)
    ok(len(r.get("intervals") or []) == 2, "缺失点应把区间分成两段")
    contains(r["note"], "打断")


# ---------------------------------------------------------------------------
# 均值口径
# ---------------------------------------------------------------------------

def test_mean_default_is_sample_arithmetic_mean():
    x = [2.0, 2.5, 3.0]
    out = _eval({"dB(S(2,1))": _payload(x, [10.0, 20.0, 30.0])}, [
        {"id": "m", "kind": "mean_in_band", "expr": "dB(S(2,1))", "target": 15.0, "unit": "dB"},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    ok(abs(r["actual"] - 20.0) < 1e-9, f"默认按采样点算术平均，实际 {r['actual']}")
    contains(r["method"], "算术平均")


def test_mean_freq_weighted_option():
    x = [2.0, 2.5, 3.0]
    out = _eval({"dB(S(2,1))": _payload(x, [10.0, 20.0, 30.0])}, [
        {"id": "m", "kind": "mean_in_band", "expr": "dB(S(2,1))",
         "target": 15.0, "unit": "dB", "mean_method": "freq_weighted"},
    ], dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"}))
    r = out["results"][0]
    ok(abs(r["actual"] - 20.0) < 1e-9, f"对称梯形权重下仍应为 20，实际 {r['actual']}")
    contains(r["method"], "加权")


def test_mean_methods_differ_on_nonuniform_grid():
    x = [2.0, 2.05, 3.0]                       # 中点挤在低端
    payloads = {"dB(S(2,1))": _payload(x, [0.0, 100.0, 0.0])}
    traces = dm.normalize_traces(payloads)
    band = dj.normalize_band({"start": 2.0, "stop": 3.0, "unit": "GHz"})
    spec = {"id": "m", "kind": "mean_in_band", "expr": "dB(S(2,1))",
            "target": 1.0, "unit": "dB"}
    a = dm.evaluate(traces, [spec], band)["results"][0]
    b = dm.evaluate(traces, [dict(spec, mean_method="freq_weighted")], band)["results"][0]
    ok(abs(a["actual"] - b["actual"]) > 1.0,
       f"非均匀网格上两种口径必须可区分：sample={a['actual']} weighted={b['actual']}")


# ---------------------------------------------------------------------------
# 横轴单位不确定性
# ---------------------------------------------------------------------------

def test_ambiguous_x_unit_blocks_formal_verdict():
    """数据 [1,3000]、频段 2.3–2.5 GHz：GHz(2.3) 与 MHz(2300) 两种解释都落得进去。"""
    x = [1.0, 1200.0, 3000.0]
    out = _eval({"dB(S(2,1))": _payload(x, [18.0] * 3, x_unit="")}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], None, "横轴单位不唯一时不能确认达标")
    eq(r["blocked_reason"], "unit_ambiguous")
    contains(r["note"], "无法唯一确定")


def test_unambiguous_inferred_unit_still_judges():
    x = _sweep(2.0, 3.0, 101)                  # GHz 量级，MHz 解释落不进
    out = _eval({"dB(S(2,1))": _payload(x, [18.0] * 101, x_unit="")}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["blocked_reason"], "")
    eq(r["pass"], True)
    eq(r["assumed_unit"], True, "推断出的单位要如实标注")
    contains(r["note"], "按 GHz 解释")


def test_declared_x_unit_is_certain():
    x = _sweep(2.0, 3.0, 101)
    out = _eval({"dB(S(2,1))": _payload(x, [18.0] * 101, x_unit="GHz")}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    eq(out["results"][0]["assumed_unit"], False)


# ---------------------------------------------------------------------------
# 数据质量信息完整性
# ---------------------------------------------------------------------------

def test_quality_has_full_coverage_info():
    x = _sweep(2.0, 3.0, 1001)
    y = [16.0 - 60.0 * ((v - 2.4) / 0.6) ** 2 for v in x]
    trace = dm.normalize_trace("dB(S(2,1))", _payload(x, y), max_points=100)
    q = trace["quality"]
    eq(q["n_valid"], 1001)
    eq(q["n_dropped"], 0)
    eq(q["display_downsampled"], True)
    ok(q["display_n"] <= 100, f"显示点数应 ≤100，实际 {q['display_n']}")
    eq(q["display_method"], "minmax-bucket")
    ok(q["x_min"] == 2.0 and q["x_max"] == 3.0)
    ok(q["max_gap"] is not None and abs(q["max_gap"] - 0.001) < 1e-6,
       f"最大采样间隔应约 0.001 GHz，实际 {q['max_gap']}")
    eq(trace["source"], "")
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [GAIN],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    dq = out["results"][0]["data_quality"]
    eq(dq["n_used"], 201)
    ok(dq["max_gap_in_band"], "带内最大采样间隔应出现在质量信息里")
    contains(dq["max_gap_in_band"], "GHz")


def test_downsample_flag_does_not_affect_full_data_evaluation():
    """显示层降采样只影响画图；判定仍用完整数据。"""
    x = _sweep(2.0, 3.0, 2001)
    y = [16.0] * len(x)
    y[800] = 5.0                               # 带内一个深坑（2.4 GHz）
    out = _eval({"dB(S(2,1))": _payload(x, y)}, [dict(GAIN, target=10.0)],
                dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}))
    r = out["results"][0]
    eq(r["pass"], False, "坑在带内，带内最小值 5.0 < 10")


if __name__ == "__main__":
    sys.exit(run(globals(), "设计指标：判定边界与数据质量"))
