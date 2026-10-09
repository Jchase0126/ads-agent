"""确定性指标评估器（纯标准库，不依赖 ADS / Qt / numpy）。

职责边界（很重要）
------------------
**只有本模块能给出"实测值"和"是否达标"。** 模型可以提出设计、提出调参方案，
也可以解释结果，但它给的数值一律不作为实测值 —— 评估器的输入只有从
``.ds`` 数据集读出来的真实数组，输出是"目标 / 实测 / 判定 / 对应频点"。
数据不够、单位对不上、频段里没有点时，结论是 ``pass=None`` + 一句说明，
**绝不猜一个数**。

判定边界（2026-10-02 强化，四种此前会给出错误"达标"的情形）
------------------------------------------------------------
1. 频段覆盖不足：要求 1–3 GHz 但数据只有 2–2.5 GHz —— 带内指标一律
   ``pass=None``（blocked_reason=band_coverage_insufficient），除非调用方
   显式给 ``options={"partial_band": True}``，此时只评估已覆盖区间并在
   结果里标注 eval_range，exact=False。
2. 带内有点读不出来（None/NaN/Inf 被丢弃）：对 min/max/flatness 这类
   "最值"指标，丢点意味着真正的最值可能就在丢的那个点上 —— 一律
   ``pass=None``（blocked_reason=missing_points_in_band）；mean/bandwidth
   可以在有效点上计算，但 exact=False 并写明。
3. value_at 目标频点超出数据范围：不再"就近取点然后照样判定"，
   直接 ``pass=None``（blocked_reason=out_of_range）。范围内的最近点近似
   照常判定，但 exact=False 并标注 method=nearest / linear。
4. 单位不可比：曲线单位 V、指标单位 dB 这类直接比较没有物理意义 ——
   ``pass=None``（blocked_reason=unit_incompatible）。横轴单位无法唯一
   推断（只能靠量级猜）时同样 ``pass=None``（unit_ambiguous）。

数据质量（quality）
-------------------
每条曲线的归一化结果带 ``quality``：原始点数 / 有效点数 / 丢弃点数 /
丢弃原因（x 非法、y 非法、x/y 长度不一致）/ 丢弃点位置 / 实际覆盖范围 /
最大采样间隔 / 重复频点数 / 是否非均匀扫频 / 显示层是否降采样。
每条指标结果带 ``data_quality``（本次判定用到的点数、覆盖结论、带内最大间隔）。

指标口径（明确化）
------------------
    mean_in_band   默认按**采样点算术平均**（result.method=sample_mean）；
                   指标定义里给 "mean_method": "freq_weighted" 时按频率区间
                   加权（梯形权重，method=freq_weighted_mean）。
    bandwidth_above 连续区间 = 相邻有效点都满足 y ≥ threshold 的区段，区间
                   两端与相邻不满足点做**线性插值**求阈值交点；数据端点处
                   不外推（clipped 标注）；带内缺失点把区间打断（保守）；
                   多段不连续区间只取最宽一段（intervals 列表全量给出，
                   绝不把缺失区间跨接成长带宽）。

单位处理
--------
频段用 Hz 保存（``design_job.normalize_band``），数据集的横轴单位可能缺失，
所以这里先按 trace 声明的 ``x_unit`` 换算；没有声明时用"声明的频段落在
数据范围内"反推，并把这次推断**写进 note**；推不唯一（多个单位解释都落得进
数据范围，最后只能按量级猜）时判 ``unit_ambiguous``，不确认达标。
"""

from __future__ import annotations

import math

from design_job import FREQ_UNITS, canonical_freq_unit, freq_scale

DEFAULT_COMPARATOR = {
    "min_in_band": ">=",
    "max_in_band": "<=",
    "mean_in_band": ">=",
    "flatness_in_band": "<=",
    "bandwidth_above": ">=",
    "value_at": ">=",
}

NEEDS_BAND = {"min_in_band", "max_in_band", "mean_in_band",
              "flatness_in_band", "bandwidth_above"}

KINDS = tuple(DEFAULT_COMPARATOR)

# 单位兼容类：只有同类（且换算后同量级前缀）的曲线/指标单位才允许直接比较。
_UNIT_CLASSES = {
    "log_ratio": ("db", "dbc", "dbi", "dbv", "dbuv"),
    "log_power": ("dbm", "dbw"),
    "voltage": ("v", "mv", "uv", "kv"),
    "power": ("w", "mw", "uw", "kw"),
    "impedance": ("ohm", "kohm", "mohm", "ohms"),
    "dimensionless": ("", "-", "ratio", "lin", "linear", "none"),
    "frequency": ("hz", "khz", "mhz", "ghz", "thz"),
}
# 同类但前缀不同（V 与 mV）也不可比：差固定倍数，直接比较会得出错误结论。
_EXACT_UNIT_ALIASES = {
    "db": "db", "dbc": "dbc", "dbi": "dbi", "dbv": "dbv", "dbuv": "dbuv",
    "dbm": "dbm", "dbw": "dbw",
    "v": "v", "mv": "mv", "uv": "uv", "kv": "kv",
    "w": "w", "mw": "mw", "uw": "uw", "kw": "kw",
    "ohm": "ohm", "ohms": "ohm", "kohm": "kohm", "mohm": "mohm",
    "": "", "-": "", "ratio": "", "lin": "", "linear": "", "none": "",
}


def _unit_key(unit: str) -> str:
    return str(unit or "").strip().lower()


def units_compatible(y_unit: str, metric_unit: str) -> bool:
    """曲线纵轴单位与指标单位是否可直接比较。

    任一方未声明（空）视为兼容（采用另一方）；同类但前缀不同（V 与 mV）
    不兼容 —— 差 1000 倍，比较结论一定是错的。
    """
    a, b = _unit_key(y_unit), _unit_key(metric_unit)
    if not a or not b:
        return True
    return _EXACT_UNIT_ALIASES.get(a, a) == _EXACT_UNIT_ALIASES.get(b, b)


def _unit_hint(unit: str) -> str:
    key = _unit_key(unit)
    for cls, members in _UNIT_CLASSES.items():
        if key in members:
            return cls
    return "unknown"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _to_float(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _fmt(v) -> str:
    f = _to_float(v)
    if f is None:
        return "?"
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    return f"{f:g}"


def _slug(text: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(text or ""))
    return out.strip("_") or "metric"


def format_freq(hz, unit: str = "") -> str:
    """把 Hz 显示成人类可读的形式（优先用调用方声明的单位）。"""
    f = _to_float(hz)
    if f is None:
        return "?"
    if unit:
        scale = freq_scale(unit)
        if scale:
            return f"{_fmt(f / scale)} {canonical_freq_unit(unit)}"
    for name, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3), ("Hz", 1.0)):
        if abs(f) >= scale:
            return f"{_fmt(f / scale)} {name}"
    return f"{_fmt(f)} Hz"


def downsample(x: list, y: list, max_points: int) -> tuple:
    """保极值的等宽分桶降采样（**只用于画图**；判定始终用完整数据）。

    每个桶里取出最小与最大两点（按 x 顺序输出），所以曲线的尖峰与凹陷
    不会被抹掉 —— 但频点定位会退化到桶分辨率，调用方需把 exact 标成 False。
    """
    n = len(x)
    if max_points <= 0 or n <= max_points:
        return list(x), list(y)
    buckets = max(1, max_points // 2)
    width = n / buckets
    ox, oy = [], []
    for b in range(buckets):
        lo = int(b * width)
        hi = int((b + 1) * width)
        if hi <= lo:
            hi = min(lo + 1, n)
        if lo >= n:
            break
        chunk = list(range(lo, min(hi, n)))
        if not chunk:
            continue
        i_min = min(chunk, key=lambda i: y[i])
        i_max = max(chunk, key=lambda i: y[i])
        for i in (i_min, i_max) if i_min <= i_max else (i_max, i_min):
            if not ox or ox[-1] != x[i]:
                ox.append(x[i])
                oy.append(y[i])
    return ox, oy


# ---------------------------------------------------------------------------
# trace 归一化 + 数据质量
# ---------------------------------------------------------------------------

def _compute_quality(raw_x: list, raw_y: list, kept_x: list, kept_y: list,
                     dropped_x: list, reasons: dict) -> dict:
    """从归一化前后的序列统计数据质量（单位 = 数据横轴原始单位）。"""
    n_raw = len(raw_x) if len(raw_x) == len(raw_y) else -1  # -1: 长度不一致
    q = {
        "n_raw": max(n_raw, len(raw_x)),
        "n_valid": len(kept_x),
        "n_dropped": len(raw_x) - len(kept_x) if len(raw_x) >= len(kept_x) else 0,
        "pair_mismatch": abs(len(raw_x) - len(raw_y)) if len(raw_x) != len(raw_y) else 0,
        "drop_reasons": reasons,
        "dropped_x": dropped_x[:50],        # 只留前 50 个，够定位问题
        "x_min": kept_x[0] if kept_x else None,
        "x_max": kept_x[-1] if kept_x else None,
        "max_gap": None,
        "duplicate_x": 0,
        "non_uniform": False,
    }
    if len(kept_x) >= 2:
        steps = [kept_x[i + 1] - kept_x[i] for i in range(len(kept_x) - 1)]
        positive = [s for s in steps if s > 0]
        q["max_gap"] = max(steps) if steps else None
        q["duplicate_x"] = sum(1 for s in steps if s == 0)
        if positive and len(positive) >= 2:
            ratio = max(positive) / min(positive) if min(positive) > 0 else 1.0
            q["non_uniform"] = ratio > 1.5
            q["min_step"] = min(positive)
            q["max_step"] = max(positive)
    return q


def normalize_trace(name: str, payload: dict, max_points: int = 0) -> dict:
    """把 ``read_traces`` 返回的一条曲线整理成统一结构。

    输入::

        {"x": [...], "y": [...], "x_name": "freq", "y_name": "dB(S(2,1))",
         "x_unit": "GHz", "y_unit": "dB", "truncated": false}

    输出带 ``n_points`` / ``truncated`` / ``quality``（数据质量）/
    ``display``（降采样后的画图序列）。**评估用 x/y（完整有效点），
    画图用 display —— 两者在本结构里始终分得开。**
    """
    payload = payload or {}
    raw_x = list(payload.get("x") or [])
    raw_y = list(payload.get("y") or [])

    reasons = {"x_invalid": 0, "y_invalid": 0}
    dropped_x = []
    keep_x, keep_y = [], []
    if len(raw_x) != len(raw_y):
        # 长度不一致：按短的一侧配对，多出来的算丢弃（原因记 pair_mismatch）
        n = min(len(raw_x), len(raw_y))
    else:
        n = len(raw_x)
    for i in range(n):
        a, b = _to_float(raw_x[i]), _to_float(raw_y[i])
        if a is None:
            reasons["x_invalid"] += 1
            dropped_x.append(raw_x[i])
            continue
        if b is None:
            reasons["y_invalid"] += 1
            dropped_x.append(a)
            continue
        keep_x.append(a)
        keep_y.append(b)
    if len(raw_x) != len(raw_y):
        reasons["pair_mismatch"] = abs(len(raw_x) - len(raw_y))

    x, y = keep_x, keep_y
    quality = _compute_quality(raw_x, raw_y, x, y, dropped_x, reasons)

    display_downsampled = False
    display = {"x": x, "y": y, "n_points": len(x), "method": "none"}
    if max_points and len(x) > max_points:
        dx, dy = downsample(x, y, max_points)
        display = {"x": dx, "y": dy, "n_points": len(dx), "method": "minmax-bucket"}
        display_downsampled = True
    quality["display_downsampled"] = display_downsampled
    quality["display_n"] = display["n_points"]
    quality["display_method"] = display["method"]

    trace = {
        "name": name,
        "x_name": payload.get("x_name") or "x",
        "y_name": payload.get("y_name") or name,
        "x_unit": (canonical_freq_unit(payload["x_unit"], payload["x_unit"])
                   if payload.get("x_unit") else ""),
        "y_unit": payload.get("y_unit") or "",
        "x": x,
        "y": y,
        "n_points": len(x),
        "n_points_raw": int(payload.get("n_points") or len(raw_x)),
        "truncated": bool(payload.get("truncated")),
        "source": payload.get("source") or "",
        "quality": quality,
        "display": display,
    }
    return trace


def normalize_traces(raw: dict, max_points: int = 0) -> dict:
    return {name: normalize_trace(name, payload, max_points)
            for name, payload in (raw or {}).items()}


# ---------------------------------------------------------------------------
# 横轴单位推断
# ---------------------------------------------------------------------------

def _infer_candidates(x: list, refs: list):
    """给出"能把所有参考频率落进数据范围"的单位解释列表。"""
    if not x or not refs:
        return []
    lo, hi = min(x), max(x)
    ok = []
    for name, scale in (("Hz", 1.0), ("kHz", 1e3), ("MHz", 1e6), ("GHz", 1e9)):
        if all(lo <= r / scale <= hi for r in refs):
            ok.append((name, scale))
    return ok


def infer_x_scale(x: list, band: dict, x_unit: str):
    """返回 ``(scale, unit_name, note)``；scale 把 Hz 换算成数据横轴的单位。

    兼容旧签名。需要区分"推断是否唯一"时用 ``infer_x_scale_ex``。
    """
    scale, name, note, _ = infer_x_scale_ex(x, band, x_unit)
    return scale, name, note


def infer_x_scale_ex(x: list, band: dict, x_unit: str, extra_refs=None):
    """返回 ``(scale, unit_name, note, certain)``。

    certain=False 表示单位只能靠量级猜（多个解释都行 / 都不行取最近的），
    调用方**不得**据此给正式达标结论。
    """
    if x_unit:
        scale = freq_scale(x_unit)
        if scale:
            return scale, canonical_freq_unit(x_unit), "", True

    refs = []
    if band and band.get("start_hz") is not None:
        refs = [band["start_hz"], band["stop_hz"]]
    if extra_refs:
        refs = refs + [r for r in extra_refs if r is not None]
    if not refs or not x:
        return 1.0, "Hz", "数据未声明横轴单位，按 Hz 解释", False

    lo, hi = min(x), max(x)
    candidates = _infer_candidates(x, refs)
    if len(candidates) == 1:
        name, scale = candidates[0]
        return scale, name, (
            f"数据未声明横轴单位，按 {name} 解释"
            f"（依据：声明的频段 {band.get('label', '')} 落在数据范围 "
            f"{_fmt(lo)}–{_fmt(hi)} 内）"
        ), True
    if len(candidates) > 1:
        name, scale = candidates[-1]
        names = " / ".join(n for n, _ in candidates)
        return scale, name, (
            f"数据未声明横轴单位，且 {names} 多种解释都能把频段落进数据范围 "
            f"{_fmt(lo)}–{_fmt(hi)} —— 无法唯一确定，不能给出正式判定"
        ), False
    # 所有解释都落不进：沿用旧行为按"离最近点解释"猜一个，但明确不确定
    name, scale = min(
        (("Hz", 1.0), ("kHz", 1e3), ("MHz", 1e6), ("GHz", 1e9)),
        key=lambda kv: abs(refs[0] / kv[1] - lo),
    )
    return scale, name, (
        f"数据未声明横轴单位，且声明的频段没有落在数据范围 "
        f"{_fmt(lo)}–{_fmt(hi)} 内 —— 已按 {name} 解释，请核对频段设置"
    ), False


def _band_indices(x: list, band: dict, scale: float):
    if not band or band.get("start_hz") is None:
        return []
    lo = band["start_hz"] / scale
    hi = band["stop_hz"] / scale
    return [i for i, v in enumerate(x) if lo <= v <= hi]


# ---------------------------------------------------------------------------
# 频段覆盖核查
# ---------------------------------------------------------------------------

def _band_coverage(x: list, idx: list, band: dict, scale: float, quality: dict):
    """核查数据对频段的覆盖情况。

    返回 dict：
      start_covered / stop_covered  频段两端是否被数据覆盖（端点容差 = 带内
                                    中位步长的一半，采样点离端点半个步长内
                                    视为覆盖到端）；
      n_in_band                     带内有效点数；
      dropped_in_band               带内被丢弃（读不出值）的点数；
      max_gap                       带内最大采样间隔（数据横轴单位）；
      eval_start_hz / eval_stop_hz  实际有数据覆盖的区间（Hz）。
    """
    out = {
        "start_covered": False, "stop_covered": False,
        "n_in_band": len(idx), "dropped_in_band": 0,
        "max_gap": None, "eval_start_hz": None, "eval_stop_hz": None,
    }
    if not x:
        return out
    lo_hz, hi_hz = band["start_hz"], band["stop_hz"]
    in_band = [x[i] for i in idx]
    dropped_x = (quality or {}).get("dropped_x") or []
    out["dropped_in_band"] = sum(
        1 for v in dropped_x
        if isinstance(v, (int, float)) and lo_hz / scale <= v <= hi_hz / scale
    )
    if in_band:
        steps = sorted(b - a for a, b in zip(in_band, in_band[1:]) if b > a)
        median_step = steps[len(steps) // 2] if steps else 0.0
        tol = max(median_step * 0.5, abs(hi_hz - lo_hz) / scale * 1e-9)
        out["start_covered"] = (x[0] * scale) <= lo_hz + tol * scale
        out["stop_covered"] = (x[-1] * scale) >= hi_hz - tol * scale
        out["eval_start_hz"] = max(lo_hz, min(in_band) * scale)
        out["eval_stop_hz"] = min(hi_hz, max(in_band) * scale)
        if len(in_band) >= 2:
            out["max_gap"] = max(b - a for a, b in zip(in_band, in_band[1:]))
    return out


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------

def evaluate(traces: dict, metrics: list, band: dict | None = None,
             available: list | None = None, options: dict | None = None) -> dict:
    """按指标定义评估真实数据。

    ``traces``  : ``normalize_traces()`` 的输出（含完整 ``x`` / ``y``）
    ``metrics`` : ``[{id,label,kind,expr,target,unit,comparator,...}]``
    ``band``    : ``design_job.normalize_band()`` 的输出
    ``options`` : {"partial_band": bool} —— 频段覆盖不足时是否只评估已覆盖
                  部分（默认 False = 无法判定）。partial=True 时结论会标注
                  实际评估范围，exact=False。

    返回 ``{"results": [...], "summary": {...}, "band": {...}, "notes": [...]}``。
    """
    band = band or {}
    notes: list = []
    options = options or {}
    results = [_eval_one(traces, spec or {}, band, notes, available, options)
               for spec in (metrics or [])]

    # 模型条件门禁：**在给出判定之前**拦一道。模型的有效频段覆盖不到目标
    # 频段时，曲线在该区间是 ADS 的插值/外推结果，据此报达标没有依据；
    # 频段或工作条件查不到时同理 —— 一律 unknown（pass=None）+ 写明原因。
    gate = options.get("model_gate")
    if gate and gate.get("block"):
        reason = "model_" + str(gate.get("state") or "unknown")
        for r in results:
            if r.get("pass") is not None:
                r["pass"] = None
                r["blocked_reason"] = reason
                r["exact"] = False
            r["note"] = (r.get("note", "") + "；" if r.get("note") else "") \
                + str(gate.get("message") or "模型条件门禁未通过")
            r["model_gate"] = {"state": gate.get("state"), "reason": reason}
        notes.append(str(gate.get("message") or ""))

    return {
        "results": results,
        "band": band,
        "notes": notes,
        "summary": summarize(results),
    }


def summarize(results: list) -> dict:
    """汇总判定。

    规则（刻意区分"没判定"与"判定为否"）：
      * 一项都判不了（全 None）-> unknown —— 不能说"未达标"，因为压根没算出来；
      * 全部 True            -> pass
      * 一个 True 都没有     -> fail（哪怕还有若干项判不了，也已经明确不达标）
      * 其余                 -> partial
    """
    checks = [r for r in results if isinstance(r, dict)]
    n_pass = sum(1 for r in checks if r.get("pass") is True)
    n_fail = sum(1 for r in checks if r.get("pass") is False)
    n_unknown = sum(1 for r in checks if r.get("pass") is None)

    if not checks or n_unknown == len(checks):
        verdict = "unknown"
    elif n_fail == 0 and n_unknown == 0:
        verdict = "pass"
    elif n_pass == 0:
        verdict = "fail"
    else:
        verdict = "partial"

    return {
        "n_metrics": len(checks),
        "n_passed": n_pass,
        "n_failed": n_fail,
        "n_unknown": n_unknown,
        "verdict": verdict,
    }


def _blank(spec: dict, kind: str, trace, note: str) -> dict:
    return {
        "id": spec.get("id") or _slug(spec.get("label") or spec.get("expr") or kind),
        "label": spec.get("label") or spec.get("expr") or kind,
        "kind": kind,
        "expr": spec.get("expr") or "",
        "target": _to_float(spec.get("target")),
        "comparator": spec.get("comparator") or DEFAULT_COMPARATOR.get(kind, ">="),
        "unit": spec.get("unit") or (trace or {}).get("y_unit") or "",
        "actual": None,
        "at_hz": None,
        "at": "",
        "pass": None,
        "n_points": 0,
        "exact": False,
        "assumed_unit": False,
        "blocked_reason": "",
        "method": "",
        "data_quality": {},
        "note": note,
    }


def _block(result: dict, reason: str, extra: str = "") -> dict:
    """把结果标记为"无法判定"（pass=None），带上机器可读原因。"""
    result["pass"] = None
    result["blocked_reason"] = reason
    if extra:
        result["note"] = (result["note"] + "；" if result["note"] else "") + extra
    return result


def _eval_one(traces: dict, spec: dict, band: dict, notes: list,
              available: list | None, options: dict) -> dict:
    kind = spec.get("kind") or "min_in_band"
    expr = spec.get("expr") or ""

    if kind not in DEFAULT_COMPARATOR:
        return _blank(spec, kind, None,
                      f"不支持的指标类型 {kind}；可用: {', '.join(KINDS)}")

    trace = (traces or {}).get(expr)
    if trace is None:
        names = ", ".join(sorted((available or list((traces or {}).keys()))))[:200]
        blank = _blank(spec, kind, None,
                       f"数据集中没有 {expr or '(未指定表达式)'} 的数据；可用: {names or '(无)'}")
        blank["blocked_reason"] = "expression_missing"
        return blank

    result = _blank(spec, kind, trace, "")
    x, y = trace.get("x") or [], trace.get("y") or []
    quality = trace.get("quality") or {}
    if not x or not y or len(x) != len(y):
        result["note"] = "该曲线的数据为空或长度不一致"
        return _block(result, "empty_or_mismatched_data")

    # ---- 单位兼容性（对所有指标类型生效）--------------------------------
    # bandwidth_above 的 unit 字段是**带宽输出单位**（GHz 等），与曲线纵轴
    # 无关，不能拿来和 y_unit 比；其余类型的 target/actual 才共用这个单位。
    y_unit = trace.get("y_unit") or ""
    m_unit = spec.get("unit") or ""
    if kind != "bandwidth_above" and not units_compatible(y_unit, m_unit):
        result["note"] = (
            f"单位不可比：曲线纵轴单位是 {y_unit or '(未声明)'}，"
            f"指标单位是 {m_unit or '(未声明)'}，直接比较数值没有物理意义。"
            f"请统一口径（如把表达式改成 dB(...) 或把指标单位改成 {y_unit}）。"
        )
        result["unit"] = m_unit or y_unit
        return _block(result, "unit_incompatible")
    result["unit"] = m_unit or y_unit if kind != "bandwidth_above" else (m_unit or "Hz")
    # exact 的基准：完整数据（未截断）才可能是精确结论；后面按近似情况逐级降级
    result["exact"] = not trace.get("truncated")
    if trace.get("truncated"):
        extra = (f"数据集点数 {trace.get('n_points_raw')} 超过上限，已按保极值降采样；"
                 f"极值可靠，频点定位有桶分辨率误差")
        result["note"] = (result["note"] + "；" if result["note"] else "") + extra

    # ---- 频段类指标：0 Hz 起始也算"已指定频段"（start_hz=0 是合法频段）----
    if kind in NEEDS_BAND and band.get("start_hz") is None:
        result["note"] = "未指定频段，无法做带内判定"
        return _block(result, "band_not_specified")

    extra_refs = []
    if kind == "value_at":
        t = _to_float(spec.get("at_hz"))
        if t is None:
            t = _to_float(spec.get("at"))
            if t is not None:
                u = spec.get("at_unit") or band.get("unit") or ""
                t = t * (freq_scale(u) or 1.0)
        if t is not None:
            extra_refs = [t]

    scale, unit_name, unit_note, certain = infer_x_scale_ex(
        x, band if kind in NEEDS_BAND else {}, trace.get("x_unit", ""),
        extra_refs=extra_refs,
    )
    if unit_note:
        result["assumed_unit"] = True
        result["note"] = unit_note
        if unit_note not in notes:
            notes.append(unit_note)
        if not certain:
            # 单位只能靠猜：不能确认数据横轴含义，判定必然不可信
            result["x_unit"] = unit_name
            return _block(result, "unit_ambiguous",
                          "横轴单位无法唯一确定，已停止判定；"
                          "请让数据集输出带单位的横轴或核对扫频设置后重试")
    result["x_unit"] = unit_name

    # value_at 的目标频点（上面已算过一次，这里复用）
    target_hz = None
    if kind == "value_at":
        target_hz = extra_refs[0] if extra_refs else None

    result["n_points"] = 0
    result["data_quality"] = {}
    dq = result["data_quality"]

    if kind in NEEDS_BAND:
        idx = _band_indices(x, band, scale)
        coverage = _band_coverage(x, idx, band, scale, quality)
        dq.update({
            "coverage": ("full" if (coverage["start_covered"] and coverage["stop_covered"])
                         else "partial" if idx else "none"),
            "n_in_band": coverage["n_in_band"],
            "dropped_in_band": coverage["dropped_in_band"],
            "band_start_covered": coverage["start_covered"],
            "band_stop_covered": coverage["stop_covered"],
            "max_gap_in_band": (format_freq(coverage["max_gap"] * scale, unit_name)
                                if coverage["max_gap"] is not None else None),
        })
        if not idx:
            result["note"] = (
                (result["note"] + "；" if result["note"] else "")
                + f"频段 {band.get('label', '')} 内没有数据点"
                  f"（数据范围 {format_freq(min(x), unit_name)}–"
                  f"{format_freq(max(x), unit_name)}）"
            )
            return _block(result, "band_coverage_insufficient")
        if not (coverage["start_covered"] and coverage["stop_covered"]):
            missing = []
            if not coverage["start_covered"]:
                missing.append(f"低端 {format_freq(band['start_hz'], unit_name)}")
            if not coverage["stop_covered"]:
                missing.append(f"高端 {format_freq(band['stop_hz'], unit_name)}")
            detail = (f"数据没有覆盖频段{'和'.join(missing)}"
                      f"（数据范围 {format_freq(min(x) * scale, unit_name)}–"
                      f"{format_freq(max(x) * scale, unit_name)}，"
                      f"实际覆盖 {format_freq(coverage['eval_start_hz'], unit_name)}–"
                      f"{format_freq(coverage['eval_stop_hz'], unit_name)}）")
            if not options.get("partial_band"):
                result["note"] = ((result["note"] + "；") if result["note"] else "") + detail
                return _block(result, "band_coverage_insufficient",
                              "无法确认整个频段的指标，不能给出达标结论")
            # 显式允许只评估已覆盖部分：结论只对覆盖区间负责
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                detail + "；已按显式选项只评估覆盖区间"
            result["exact"] = False
            dq["eval_range"] = (f"{format_freq(coverage['eval_start_hz'], unit_name)}"
                                f"–{format_freq(coverage['eval_stop_hz'], unit_name)}")
        if coverage["dropped_in_band"]:
            # 最值类指标：丢的点可能正是最值所在 —— 不能装作没看见
            detail = (f"频段内有 {coverage['dropped_in_band']} 个点读不出数值"
                      f"（已在归一化时丢弃），无法确认最值")
            if kind in ("min_in_band", "max_in_band", "flatness_in_band"):
                result["note"] = ((result["note"] + "；") if result["note"] else "") + detail
                return _block(result, "missing_points_in_band")
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                detail + "；以下结果按有效点计算"
            result["exact"] = False
        if len(idx) < 2:
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                f"频段内只有 {len(idx)} 个采样点，不足以可靠判定（请检查扫频设置）"
            return _block(result, "in_band_sampling_sparse")
        if len(idx) < 5:
            result["exact"] = False
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                f"频段内采样点较少（{len(idx)} 个），结论精度受采样限制"
        dq["n_used"] = len(idx)
        result["n_points"] = len(idx)
        ys = [y[i] for i in idx]
        xs = [x[i] for i in idx]
    else:
        dq["n_used"] = len(x)
        result["n_points"] = len(x)
        ys, xs = list(y), list(x)

    if kind == "min_in_band":
        j = min(range(len(ys)), key=lambda i: ys[i])
        result["actual"] = ys[j]
        result["at_hz"] = xs[j] * scale
        result["method"] = "sampled_min"
    elif kind == "max_in_band":
        j = max(range(len(ys)), key=lambda i: ys[i])
        result["actual"] = ys[j]
        result["at_hz"] = xs[j] * scale
        result["method"] = "sampled_max"
    elif kind == "mean_in_band":
        method = str(spec.get("mean_method") or options.get("mean_method")
                     or "sample").strip().lower()
        if method in ("freq_weighted", "weighted", "freq"):
            # 梯形权重：内部点 w=(x[i+1]-x[i-1])/2，两端点各占半个邻区间
            n = len(xs)
            weights = []
            for i in range(n):
                if i == 0:
                    w = (xs[1] - xs[0]) / 2 if n > 1 else 1.0
                elif i == n - 1:
                    w = (xs[n - 1] - xs[n - 2]) / 2 if n > 1 else 1.0
                else:
                    w = (xs[i + 1] - xs[i - 1]) / 2
                weights.append(max(w, 0.0))
            wsum = sum(weights)
            result["actual"] = sum(v * w for v, w in zip(ys, weights)) / wsum if wsum else sum(ys) / len(ys)
            result["method"] = "freq_weighted_mean（按频率区间加权）"
        else:
            result["actual"] = sum(ys) / len(ys)
            result["method"] = "sample_mean（按采样点算术平均）"
        result["at_hz"] = sum(xs) / len(xs) * scale
        result["at_note"] = "频段中心"
    elif kind == "flatness_in_band":
        j_min = min(range(len(ys)), key=lambda i: ys[i])
        j_max = max(range(len(ys)), key=lambda i: ys[i])
        result["actual"] = ys[j_max] - ys[j_min]
        result["at_hz"] = xs[j_min] * scale
        result["method"] = "sampled_max_min"
        result["at_note"] = (f"最低 {_fmt(ys[j_min])} @ {format_freq(xs[j_min]*scale, unit_name)}"
                             f" / 最高 {_fmt(ys[j_max])} @ {format_freq(xs[j_max]*scale, unit_name)}")
    elif kind == "bandwidth_above":
        threshold = _to_float(spec.get("threshold"))
        if threshold is None:
            result["note"] = (result["note"] + "；" if result["note"] else "") + \
                "bandwidth_above 需要纵轴门限 threshold"
            return _block(result, "threshold_missing")
        pairs = sorted(zip(xs, ys))
        # 带内被丢弃的点：横在两个有效点之间时必须把连续区间打断 ——
        # 缺失处可能低于门限，跨接过去会把带宽算大（保守取小）。
        dropped_in_range = sorted(
            v for v in ((quality.get("dropped_x") or []) if isinstance(quality, dict) else [])
            if isinstance(v, (int, float)) and pairs and pairs[0][0] <= v <= pairs[-1][0]
        )
        best, intervals, clipped = 0.0, [], False
        split_by_missing = [False]

        def _flush(seg: list) -> None:
            nonlocal best, clipped
            if not seg:
                return
            w, lo_w, hi_w, clip = _run_width(seg, pairs, threshold)
            intervals.append({"width": w, "start_hz": lo_w * scale,
                              "stop_hz": hi_w * scale, "clipped": clip})
            clipped = clipped or clip
            if w > best:
                best = w

        run = []
        gi = 0
        for px, py in pairs + [(None, None)]:
            if py is not None and py >= threshold:
                while gi < len(dropped_in_range) and run and dropped_in_range[gi] <= run[-1][0]:
                    gi += 1
                if (run and gi < len(dropped_in_range)
                        and run[-1][0] < dropped_in_range[gi] < px):
                    _flush(run)
                    run = []
                    split_by_missing[0] = True
                run.append((px, py))
                continue
            _flush(run)
            run = []
        best = max(best, 0.0)
        # 带宽的物理含义是频率：报成频段声明单位，绝不报成 dB 或原始纵轴单位。
        band_unit = band.get("unit") or unit_name or "Hz"
        band_scale = freq_scale(band_unit) or 1.0
        result["actual"] = best * scale / band_scale
        result["unit"] = canonical_freq_unit(band_unit) or "Hz"
        result["at_hz"] = None
        result["method"] = "threshold_crossing_linear_interp（连续区间+阈值交点插值）"
        result["at_note"] = f"判据 y ≥ {_fmt(threshold)}，取最宽连续区间"
        result["n_points"] = sum(1 for _, py in pairs if py >= threshold)
        result["intervals"] = [
            {k: (format_freq(v, result["unit"]) if k in ("start_hz", "stop_hz") else v)
             for k, v in it.items()}
            for it in intervals
        ]
        if len(intervals) > 1:
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                f"满足门限的有 {len(intervals)} 个不连续区间，取最宽一段，未跨接"
        if split_by_missing[0]:
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                "带内有读不出的点，连续区间已在其处打断（保守口径）"
        if clipped:
            result["exact"] = False
            result["note"] = ((result["note"] + "；") if result["note"] else "") + \
                "最宽区间顶到数据端点，带宽可能被数据范围截断"
    elif kind == "value_at":
        if target_hz is None:
            result["note"] = (result["note"] + "；" if result["note"] else "") + \
                "value_at 需要 at_hz（Hz）或 at + at_unit"
            return _block(result, "target_freq_missing")
        want = target_hz / scale
        lo_x, hi_x = min(x), max(x)
        if want < lo_x - 1e-12 or want > hi_x + 1e-12:
            result["note"] = (
                (result["note"] + "；" if result["note"] else "")
                + f"目标频点 {format_freq(target_hz, unit_name)} 超出数据范围 "
                  f"{format_freq(lo_x * scale, unit_name)}–"
                  f"{format_freq(hi_x * scale, unit_name)}，不能就近取点下结论"
            )
            result["at_hz"] = None
            return _block(result, "out_of_range")
        j = min(range(len(x)), key=lambda i: abs(x[i] - want))
        delta = abs(x[j] - want)
        step = (hi_x - lo_x) / max(1, len(x) - 1)
        grid_eps = max(step * 0.01, abs(want) * 1e-12, 1e-15)
        # 可配置的最近点容差：指标定义给 tol（Hz）时优先，否则 2% 相对默认
        tol = _to_float(spec.get("tol"))
        if tol is None:
            tol = abs(want) * 0.02 if want else 0.02
        if delta <= grid_eps:
            result["actual"] = y[j]
            result["method"] = "grid_hit（频点精确采样）"
        elif str(spec.get("interp") or options.get("interp") or "").lower() in ("linear", "interp"):
            result["actual"] = _interp_y(x, y, want)
            result["method"] = "linear_interp（相邻点线性插值）"
            result["exact"] = False
        else:
            result["actual"] = y[j]
            result["method"] = "nearest（最近点近似）"
            result["exact"] = False
        result["at_hz"] = x[j] * scale
        result["n_points"] = 1
        result["at_note"] = f"目标频点 {format_freq(target_hz, unit_name)}"
        if delta > tol:
            result["note"] = (result["note"] + "；" if result["note"] else "") + \
                f"最近的频点是 {format_freq(result['at_hz'], unit_name)}，与目标频点有偏差"
        elif delta > grid_eps and result["method"].startswith("nearest"):
            result["note"] = (result["note"] + "；" if result["note"] else "") + \
                f"取最近采样点（相距 {format_freq(delta * scale, unit_name)}）"

    result["at"] = format_freq(result["at_hz"], unit_name) if result["at_hz"] is not None else ""
    result["pass"] = _judge(result["actual"], result["comparator"], result["target"])
    if result["pass"] is None and not result["blocked_reason"] and not result["note"]:
        result["note"] = "缺少目标值或实测值，无法判定"
    if result["pass"] is None and not result["blocked_reason"] and result["note"]:
        result["blocked_reason"] = "missing_target_or_value"
    return result


def _interp_y(x: list, y: list, want: float):
    """want 处的线性插值；want 落在两点之间时按比例取值。"""
    j = min(range(len(x)), key=lambda i: abs(x[i] - want))
    if abs(x[j] - want) <= 1e-15:
        return y[j]
    if j > 0 and x[j] > want:
        x0, x1, y0, y1 = x[j - 1], x[j], y[j - 1], y[j]
    elif j < len(x) - 1:
        x0, x1, y0, y1 = x[j], x[j + 1], y[j], y[j + 1]
    else:
        return y[j]
    if x1 == x0:
        return y[j]
    t = (want - x0) / (x1 - x0)
    return y0 + (y1 - y0) * t


def _run_width(run: list, pairs: list, threshold: float):
    """一段连续满足阈值的宽度；两端与相邻不满足点做线性插值求交点。

    返回 (width, start_x, stop_x, clipped)。数据端点处不外推 —— 宽度顶到
    数据边界时 clipped=True，由调用方标注"可能被截断"。
    """
    xs = [p[0] for p in pairs]
    lo_x, hi_x = run[0][0], run[-1][0]
    clip_lo = clip_hi = False
    i0 = xs.index(lo_x) if lo_x in xs else 0
    i1 = xs.index(hi_x) if hi_x in xs else len(xs) - 1
    if i0 > 0:
        lo_x = _crossing(xs[i0 - 1], xs[i0], pairs[i0 - 1][1], pairs[i0][1], threshold)
    else:
        clip_lo = True
    if i1 < len(xs) - 1:
        hi_x = _crossing(xs[i1], xs[i1 + 1], pairs[i1][1], pairs[i1 + 1][1], threshold)
    else:
        clip_hi = True
    width = max(hi_x - lo_x, 0.0)
    return width, lo_x, hi_x, (clip_lo or clip_hi)


def _crossing(x0, x1, y0, y1, threshold: float) -> float:
    """y 在 [x0,x1] 上线性穿过 threshold 的横坐标；不穿则取靠 threshold 近的一端。"""
    if y1 == y0:
        return x0 if abs(y0 - threshold) <= abs(y1 - threshold) else x1
    if (y0 - threshold) * (y1 - threshold) > 0:
        return x0 if abs(y0 - threshold) <= abs(y1 - threshold) else x1
    t = (threshold - y0) / (y1 - y0)
    return x0 + (x1 - x0) * t


def _judge(actual, comparator: str, target):
    """按判据比较；任一侧缺失时返回 None（**不猜**）。"""
    if actual is None or target is None:
        return None
    try:
        if comparator in (">=", "≥", "gte"):
            return bool(actual >= target)
        if comparator in ("<=", "≤", "lte"):
            return bool(actual <= target)
        if comparator in (">", "gt"):
            return bool(actual > target)
        if comparator in ("<", "lt"):
            return bool(actual < target)
        if comparator in ("==", "eq"):
            return bool(abs(actual - target) <= 1e-12)
    except TypeError:
        return None
    return None
