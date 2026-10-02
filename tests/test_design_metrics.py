"""数据解析 + 指标计算的测试（不需要 ADS，纯标准库）。

覆盖第四轮需求里的「确定性的指标评估器」：

* 频段写法归一化（start/stop、start_ghz/stop_ghz、from/to…）；
* 曲线归一化（丢非数值点、记录原始点数与 truncated）；
* 保极值降采样（点数变少但极值不丢）；
* 横轴单位推断（声明优先；没声明时按"频段落在数据范围内"反推，并把推断写进 note）；
* 六种指标的实测值与对应频点；
* **数据不足一律 pass=None**（没有目标值 / 频段内没有点 / 表达式不存在 / 数据被截断）；
* 判定只看数据，不做任何猜测；同一输入两次评估结果完全一致。

运行::

    python tests/test_design_metrics.py
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")

import design_job as dj  # noqa: E402
import design_metrics as dm  # noqa: E402


# ---------------------------------------------------------------------------
# 合成数据：2.0–3.0 GHz，中心 2.4 GHz
# ---------------------------------------------------------------------------

def _sweep(unit="GHz", n=1001):
    lo, hi = (2.0, 3.0) if unit == "GHz" else (2.0e9, 3.0e9)
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _bell(x, peak, halfwidth, depth, unit="GHz"):
    center = 2.4 if unit == "GHz" else 2.4e9
    span = halfwidth if unit == "GHz" else halfwidth * 1e9
    return [peak - depth * ((v - center) / span) ** 2 for v in x]


def _payload(unit="GHz", n=1001, x_unit=None, truncated=False):
    x = _sweep(unit, n)
    return {
        "x": x,
        "y": _bell(x, 16.0, 0.6, 60.0, unit),
        "x_name": "freq",
        "y_name": "dB(S(2,1))",
        "x_unit": x_unit if x_unit is not None else unit,
        "y_unit": "dB",
        "n_points": n,
        "truncated": truncated,
    }


def _band(start=2.3, stop=2.5, unit="GHz"):
    return dj.normalize_band({"start": start, "stop": stop, "unit": unit})


GAIN = {"id": "gain", "label": "带内增益", "kind": "min_in_band",
        "expr": "dB(S(2,1))", "target": 15.0, "unit": "dB"}


# ---------------------------------------------------------------------------
# 频段归一化
# ---------------------------------------------------------------------------

def test_band_accepts_common_spellings():
    eq(dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"})["start_hz"], 2.3e9)
    eq(dj.normalize_band({"start_ghz": 2.3, "stop_ghz": 2.5})["start_hz"], 2.3e9)
    eq(dj.normalize_band({"from": 2.3, "to": 2.5, "units": "GHz"})["stop_hz"], 2.5e9)
    eq(dj.normalize_band([2.3, 2.5])["unit"], "Hz", "缺省单位按 Hz（数据集原始单位）")


def test_band_hz_fields_are_not_scaled_by_display_unit():
    band = dj.normalize_band({"start_hz": 2.3e9, "stop_hz": 2.5e9, "unit": "GHz"})
    eq((band["start_hz"], band["stop_hz"]), (2.3e9, 2.5e9))
    eq((band["start"], band["stop"]), (2.3, 2.5))
    mixed = dj.normalize_band({"start": 2.3, "stop_hz": 2.5e9, "unit": "GHz"})
    eq((mixed["start_hz"], mixed["stop_hz"]), (2.3e9, 2.5e9))
    suffixed = dj.normalize_band({"start_ghz": 2.3, "stop_mhz": 2500})
    eq((suffixed["start_hz"], suffixed["stop_hz"]), (2.3e9, 2.5e9))


def test_band_swaps_reversed_limits():
    band = dj.normalize_band({"start": 2.5, "stop": 2.3, "unit": "GHz"})
    eq(band["start_hz"], 2.3e9)
    eq(band["stop_hz"], 2.5e9)


def test_band_rejects_incomplete_input():
    eq(dj.normalize_band({}), {})
    eq(dj.normalize_band({"start": 2.3}), {})
    eq(dj.normalize_band({"start": "abc", "stop": 3, "unit": "GHz"}), {})
    eq(dj.normalize_band(None), {})


def test_band_label_is_readable():
    contains(dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"})["label"],
             "2.3–2.5 GHz")
    contains(dj.normalize_band({"start": 2300, "stop": 2500, "unit": "MHz"})["label"],
             "MHz")


# ---------------------------------------------------------------------------
# 曲线归一化与降采样
# ---------------------------------------------------------------------------

def test_normalize_trace_drops_non_numeric_points():
    trace = dm.normalize_trace("x", {"x": [1, "bad", 3, None], "y": [10, 20, 30, 40]})
    eq(trace["n_points"], 2)
    eq(trace["x"], [1.0, 3.0])
    eq(trace["y"], [10.0, 30.0])


def test_normalize_trace_keeps_units_and_source():
    trace = dm.normalize_trace("dB(S(2,1))", _payload())
    eq(trace["x_unit"], "GHz")
    eq(trace["y_unit"], "dB")
    eq(trace["y_name"], "dB(S(2,1))")
    eq(trace["truncated"], False)


def test_downsample_keeps_extremes():
    x = [float(i) for i in range(1000)]
    y = [0.0] * 1000
    y[137] = -50.0            # 一个尖坑
    y[861] = 90.0             # 一个尖峰
    dx, dy = dm.downsample(x, y, 100)
    ok(len(dx) <= 200, f"降采样后点数应明显减少，实际 {len(dx)}")
    eq(min(dy), -50.0, "降采样把最小值弄丢了")
    eq(max(dy), 90.0, "降采样把最大值弄丢了")
    eq(len(dx), len(dy), "x/y 长度必须一致")


def test_downsample_is_identity_when_small_enough():
    x, y = [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]
    eq(dm.downsample(x, y, 100), (x, y))


# ---------------------------------------------------------------------------
# 横轴单位推断
# ---------------------------------------------------------------------------

def test_declared_unit_wins():
    scale, name, note = dm.infer_x_scale([2.0, 3.0], _band(), "GHz")
    eq(scale, 1e9)
    eq(name, "GHz")
    eq(note, "", "单位已声明时不应有多余说明")


def test_undeclared_unit_is_inferred_from_the_band_and_reported():
    """数据是 Hz、频段写的是 GHz —— 必须推断出来，并**把推断写进 note**。"""
    x = _sweep("Hz")
    scale, name, note = dm.infer_x_scale(x, _band(), "")
    eq(scale, 1.0)
    eq(name, "Hz")
    contains(note, "未声明", "推断必须如实说明")
    contains(note, "Hz")


def test_undeclared_unit_infers_ghz_data():
    x = _sweep("GHz")
    scale, name, _ = dm.infer_x_scale(x, _band(), "")
    eq(scale, 1e9)
    eq(name, "GHz")


def test_mismatched_band_is_reported_not_hidden():
    x = [10.0, 20.0, 30.0]                  # 明显不是任何频率量级
    scale, name, note = dm.infer_x_scale(x, _band(), "")
    contains(note, "没有落在数据范围", "频段与数据对不上时必须提醒核对")


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

_DEFAULT_BAND = object()


def _eval(payloads, metrics, band=_DEFAULT_BAND):
    traces = dm.normalize_traces(payloads)
    return dm.evaluate(traces, metrics, _band() if band is _DEFAULT_BAND else band,
                       available=list(payloads))


def test_min_in_band_picks_the_worst_point_in_band():
    out = _eval({"dB(S(2,1))": _payload()}, [GAIN])
    r = out["results"][0]
    eq(r["pass"], False, "带内最低增益 14.33 dB 未达 15 dB")
    ok(abs(r["actual"] - 14.3333) < 0.01, f"实测值应为带内最小值，实际 {r['actual']}")
    eq(r["at"], "2.3 GHz", "对应频点应是带内最低点所在的频率")
    eq(r["n_points"], 201, "2.3–2.5 GHz 每 1 MHz 一个点")
    eq(r["exact"], True)
    eq(r["assumed_unit"], False)


def test_max_in_band_uses_the_worst_case_for_upper_bounds():
    x = _sweep()
    s11 = [-25.0 + 20.0 * ((v - 2.4) / 0.6) ** 2 for v in x]
    metrics = [{"id": "s11", "label": "带内 S11", "kind": "max_in_band",
                "expr": "dB(S(1,1))", "target": -10.0, "unit": "dB"}]
    out = _eval({"dB(S(1,1))": dict(_payload(), y=s11)}, metrics)
    r = out["results"][0]
    eq(r["pass"], True)
    ok(abs(r["actual"] - (-24.444)) < 0.01, f"应取带内最大值，实际 {r['actual']}")
    eq(r["at"], "2.3 GHz")


def test_mean_and_flatness_in_band():
    out = _eval({"dB(S(2,1))": _payload()}, [
        {"id": "m", "kind": "mean_in_band", "expr": "dB(S(2,1))", "target": 15.0},
        {"id": "f", "kind": "flatness_in_band", "expr": "dB(S(2,1))", "target": 1.0},
    ])
    mean_r, flat_r = out["results"]
    ok(14.4 < mean_r["actual"] < 16.0, f"带内均值应在 14.4–16 之间，实际 {mean_r['actual']}")
    eq(flat_r["pass"], False, "2.3–2.5 GHz 的起伏约 1.67 dB，超过 1 dB")
    ok(abs(flat_r["actual"] - 1.6667) < 0.02, f"起伏应约为 1.67，实际 {flat_r['actual']}")
    contains(flat_r["at_note"], "最低")
    contains(flat_r["at_note"], "最高")


def test_bandwidth_above_counts_contiguous_width():
    out = _eval({"dB(S(2,1))": _payload()}, [
        # threshold = 纵轴门限（15 dB）；target = 要求的带宽（0.15 GHz）
        {"id": "bw", "label": "增益 ≥15 dB 的带宽", "kind": "bandwidth_above",
         "expr": "dB(S(2,1))", "target": 0.15, "threshold": 15.0, "unit": "GHz"},
    ])
    r = out["results"][0]
    # 16 − 60·((f−2.4)/0.6)²  ≥ 15  ->  |f−2.4| ≤ 0.6·sqrt(1/60) = 0.07746
    ok(abs(r["actual"] - 0.15492) < 0.005, f"带宽应约 0.155 GHz，实际 {r['actual']}")
    eq(r["unit"], "GHz", "带宽单位应是频段单位，不是 dB")
    eq(r["pass"], True, "0.155 GHz ≥ 目标 0.15 GHz")


def test_value_at_reads_the_nearest_point():
    out = _eval({"dB(S(2,1))": _payload()}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 15.5,
         "at_hz": 2.4e9, "unit": "dB"},
    ])
    r = out["results"][0]
    ok(abs(r["actual"] - 16.0) < 0.01, f"2.4 GHz 处应为峰值 16 dB，实际 {r['actual']}")
    eq(r["at"], "2.4 GHz")
    eq(r["pass"], True)
    eq(r["note"], "", "频点能对上时不该有偏差提示")


def test_value_at_warns_when_the_nearest_point_is_far_away():
    # 只有 3 个点（2.0 / 2.5 / 3.0 GHz），目标频点落在点与点之间很远的位置
    out = _eval({"dB(S(2,1))": _payload(n=3)}, [
        {"id": "v", "kind": "value_at", "expr": "dB(S(2,1))", "target": 1.0,
         "at_hz": 2.756e9},
    ])
    r = out["results"][0]
    contains(r["note"], "偏差", "目标频点离数据点很远时应提示")
    eq(r["at"], "3 GHz", "应报告实际取到的频点")


def test_bandwidth_above_accepts_the_band_unit():
    """数据横轴是 Hz、频段写 GHz 时，带宽仍要以 GHz 报出来。"""
    out = _eval({"dB(S(2,1))": _payload("Hz")}, [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.15, "threshold": 15.0, "unit": "GHz"},
    ])
    r = out["results"][0]
    eq(r["unit"], "GHz")
    ok(abs(r["actual"] - 0.15492) < 0.005, f"单位换算后应约 0.155 GHz，实际 {r['actual']}")


# ---------------------------------------------------------------------------
# 数据不足 -> 一律 pass=None（绝不猜）
# ---------------------------------------------------------------------------

def test_missing_expression_yields_unknown_with_available_list():
    out = _eval({"dB(S(2,1))": _payload()},
                [dict(GAIN, expr="dB(S(3,3))")])
    r = out["results"][0]
    eq(r["pass"], None, "表达式不存在时不能给出判定")
    eq(r["actual"], None, "绝不能编造实测值")
    contains(r["note"], "dB(S(3,3))")
    contains(r["note"], "dB(S(2,1))", "应把可用变量列出来，方便模型改口径")


def test_band_without_points_yields_unknown():
    out = _eval({"dB(S(2,1))": _payload()}, [GAIN], band=_band(5.0, 6.0))
    r = out["results"][0]
    eq(r["pass"], None)
    eq(r["actual"], None)
    contains(r["note"], "没有数据点")


def test_missing_band_yields_unknown_for_band_metrics():
    out = _eval({"dB(S(2,1))": _payload()}, [GAIN], band={})
    r = out["results"][0]
    eq(r["pass"], None)
    contains(r["note"], "未指定频段")


def test_missing_target_yields_unknown():
    out = _eval({"dB(S(2,1))": _payload()}, [dict(GAIN, target=None)])
    r = out["results"][0]
    eq(r["actual"] is not None, True, "实测值仍应算出来")
    eq(r["pass"], None, "没有目标值时不能判定")
    contains(r["note"], "无法判定")


def test_truncated_data_is_marked_not_exact():
    out = _eval({"dB(S(2,1))": _payload(truncated=True)}, [GAIN])
    r = out["results"][0]
    eq(r["exact"], False)
    contains(r["note"], "降采样")
    contains(r["note"], "极值可靠", "要如实说明哪部分是可靠的")


def test_empty_traces_never_produce_a_number():
    out = dm.evaluate({}, [GAIN], _band(), available=[])
    r = out["results"][0]
    eq(r["actual"], None)
    eq(r["pass"], None)
    eq(out["summary"]["verdict"], "unknown")


def test_unsupported_kind_is_reported():
    out = _eval({"dB(S(2,1))": _payload()}, [dict(GAIN, kind="telepathy")])
    r = out["results"][0]
    eq(r["pass"], None)
    contains(r["note"], "不支持的指标类型")
    contains(r["note"], "min_in_band", "应列出可用类型")


# ---------------------------------------------------------------------------
# 判据 / 汇总 / 确定性
# ---------------------------------------------------------------------------

def test_comparator_variants():
    out = _eval({"dB(S(2,1))": _payload()}, [
        dict(GAIN, comparator=">"),
        dict(GAIN, comparator="<"),
        dict(GAIN, comparator=">="),
        dict(GAIN, comparator="<="),
    ])
    eq([r["pass"] for r in out["results"]], [False, True, False, True])
    eq(dm._judge(5, "==", 5), True)
    eq(dm._judge(5, "==", 6), False)
    eq(dm._judge(None, ">=", 1), None, "缺值时一律 None")
    eq(dm._judge(5, "??", 1), None, "未知判据一律 None")


def test_summary_verdicts():
    eq(dm.summarize([{"pass": True}, {"pass": True}])["verdict"], "pass")
    eq(dm.summarize([{"pass": True}, {"pass": False}])["verdict"], "partial")
    eq(dm.summarize([{"pass": False}, {"pass": None}])["verdict"], "fail")
    eq(dm.summarize([{"pass": None}])["verdict"], "unknown")
    eq(dm.summarize([])["verdict"], "unknown")
    eq(dm.summarize([{"pass": True}, {"pass": None}])["n_unknown"], 1)


def test_evaluation_is_deterministic():
    metrics = [GAIN, {"id": "s", "kind": "max_in_band", "expr": "dB(S(2,1))",
                      "target": 20.0, "unit": "dB"}]
    payloads = {"dB(S(2,1))": _payload()}
    a = _eval(payloads, metrics)
    b = _eval(payloads, metrics)
    eq(json.dumps(a, sort_keys=True, ensure_ascii=False),
       json.dumps(b, sort_keys=True, ensure_ascii=False),
       "同一份数据两次评估必须完全一致")


def test_nan_and_inf_are_treated_as_missing():
    payload = _payload(n=5)
    payload["y"][2] = float("nan")
    payload["y"][3] = float("inf")
    trace = dm.normalize_trace("dB(S(2,1))", payload)
    eq(trace["n_points"], 3, "NaN/Inf 不应进入曲线")
    ok(all(v == v for v in trace["y"]))


def test_format_freq_uses_the_declared_unit():
    eq(dm.format_freq(2.4e9, "GHz"), "2.4 GHz")
    eq(dm.format_freq(2400e6, "MHz"), "2400 MHz")
    eq(dm.format_freq(2.4e9, ""), "2.4 GHz")
    eq(dm.format_freq(None), "?")


def test_metric_id_falls_back_to_the_label():
    out = _eval({"dB(S(2,1))": _payload()},
                [{"label": "带内增益", "kind": "min_in_band",
                  "expr": "dB(S(2,1))", "target": 15.0}])
    ok(bool(out["results"][0]["id"]), "没有 id 时应从 label 生成")


if __name__ == "__main__":
    sys.exit(run(globals(), "设计指标：数据解析与计算"))
