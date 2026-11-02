"""FM 88-108 MHz 共源放大器多级候选综合（用已验证的 J310 小信号模型）。

背景（必须如实报告的硬约束）：
  器件在 88-108 MHz 的最大可用增益 MAG≈11 dB（K<1），**单级不可能达到
  全带 >15 dB**。因此按要求比较单级/两级，并由数值优化综合两级 LC 匹配。

方法：
  * 器件 2 端口来自 device_models 的已拟合参数（package_delay，共源）；
  * 每级拓扑：输入 L 匹配 + 器件 + 漏极并联 LC 谐振 + 串联耦合电容；
  * 用 ABCD 链计算整链 S21，scipy 优化 LC 值使全带最小 |S21| 最大；
  * 输出组件值供 ADS 建图（ads_rflib:L / ads_rflib:C）实机复算。

用法：
    python tools/design_fm_cs_multistage.py --model-record <path或model_id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

BAND = np.arange(88.0, 108.0001, 2.0)  # MHz
Z0 = 50.0


def _device_s(freqs_mhz, params, configuration="common_source"):
    import device_models as M

    y = M.circuit_y2p(np.asarray(freqs_mhz) * 1e6, params, configuration, True)
    return M.y_to_s(y, Z0)


def _abcd_series_impedance(z):
    out = np.zeros((len(z), 2, 2), complex)
    out[:, 0, 0] = 1.0
    out[:, 1, 1] = 1.0
    out[:, 0, 1] = z
    return out


def _abcd_shunt_admittance(y):
    out = np.zeros((len(y), 2, 2), complex)
    out[:, 0, 0] = 1.0
    out[:, 1, 1] = 1.0
    out[:, 1, 0] = y
    return out


def _abcd_series_cap(c):
    return _abcd_series_impedance(1.0 / (1j * 2 * np.pi * _f_hz() * c))


def _abcd_series_ind(l):
    return _abcd_series_impedance(1j * 2 * np.pi * _f_hz() * l)


def _abcd_shunt_cap(c):
    return _abcd_shunt_admittance(1j * 2 * np.pi * _f_hz() * c)


def _abcd_shunt_ind(l):
    return _abcd_shunt_admittance(1.0 / (1j * 2 * np.pi * _f_hz() * l))


_FREQS = None


def _f_hz():
    global _FREQS
    if _FREQS is None:
        _FREQS = BAND * 1e6
    return _FREQS


def _s_from_abcd(a):
    """ABCD 序列 -> S（Z0 系统）。"""
    denom = a[:, 0, 0] + a[:, 0, 1] / Z0 + a[:, 1, 0] * Z0 + a[:, 1, 1]
    s = np.zeros_like(a)
    s[:, 0, 0] = (a[:, 0, 0] + a[:, 0, 1] / Z0 - a[:, 1, 0] * Z0 - a[:, 1, 1]) / denom
    s[:, 0, 1] = 2 * (a[:, 0, 0] * a[:, 1, 1] - a[:, 0, 1] * a[:, 1, 0]) / denom
    s[:, 1, 0] = 2 / denom
    s[:, 1, 1] = (-a[:, 0, 0] + a[:, 0, 1] / Z0 - a[:, 1, 0] * Z0 + a[:, 1, 1]) / denom
    return s


def _abcd_from_s(s):
    out = np.zeros_like(s)
    out[:, 0, 0] = ((1 + s[:, 0, 0]) * (1 - s[:, 1, 1]) + s[:, 0, 1] * s[:, 1, 0]) / (
        2 * s[:, 1, 0])
    out[:, 0, 1] = Z0 * ((1 + s[:, 0, 0]) * (1 + s[:, 1, 1]) - s[:, 0, 1] * s[:, 1, 0]) / (
        2 * s[:, 1, 0])
    out[:, 1, 0] = ((1 - s[:, 0, 0]) * (1 - s[:, 1, 1]) - s[:, 0, 1] * s[:, 1, 0]) / (
        2 * s[:, 1, 0] * Z0)
    out[:, 1, 1] = ((1 - s[:, 0, 0]) * (1 + s[:, 1, 1]) + s[:, 0, 1] * s[:, 1, 0]) / (
        2 * s[:, 1, 0])
    return out


def _matmul_chain(mats):
    out = np.tile(np.eye(2, dtype=complex), (len(_f_hz()), 1, 1))
    for m in mats:
        out = out @ m
    return out


def build_generic_abcd(x, device_abcd, n_stages, elements_per_gap):
    """通用级联：每级之间给固定数量元件（series L / shunt C 交替）。

    x 分组（从源到负载）：
      gap0（输入匹配，2 元件）: shunt C, series L
      每级器件后：gap_i（2 元件）: series L, shunt C，最后再额外一个耦合电容
    变量顺序：C_a, L_a | (L_d_i, C_t_i, C_c_i) × n_stages
    """
    import numpy as _np
    idx = 0
    mats = []
    c_a, l_a = x[idx], x[idx + 1]
    idx += 2
    mats += [_abcd_shunt_cap(c_a), _abcd_series_ind(l_a)]
    for stage in range(n_stages):
        mats.append(device_abcd)
        l_d, c_t, c_c = x[idx], x[idx + 1], x[idx + 2]
        idx += 3
        mats += [_abcd_series_ind(l_d), _abcd_shunt_cap(c_t), _abcd_series_cap(c_c)]
    return _matmul_chain(mats), idx


GENERIC_DIMS = {"two_stage": 8, "three_stage": 11}


def optimize_generic(params, topology, seed=1, maxiter=300, popsize=22,
                     band_lo_mhz=20.0, band_hi_mhz=1000.0):
    """带稳定性窗口的通用级联优化。

    稳定性判据（在 band_lo..band_hi 上检查整链 S11/S22）：
      |S11|<1 且 |S22|<1 —— 端口无负阻。窗口远超 88-108 MHz 设计带，
      避免"带内好看、带外振荡"的假达标。
    """
    import numpy as _np
    from scipy.optimize import differential_evolution, minimize

    n_stages = 2 if topology == "two_stage" else 3
    dim = GENERIC_DIMS[topology]

    global BAND
    band_design = BAND
    band_check = _np.arange(band_lo_mhz, band_hi_mhz, 5.0)

    def build_abcd_on(freqs_mhz, x, device_abcd_map):
        global _FREQS
        _FREQS = freqs_mhz * 1e6
        # device abcd 需要在该频率重算 —— 由调用方传入映射函数
        out = _np.tile(_np.eye(2, dtype=complex), (len(freqs_mhz), 1, 1))
        idx = 0
        c_a, l_a = x[idx], x[idx + 1]
        idx += 2
        out = out @ _abcd_shunt_cap(c_a) @ _abcd_series_ind(l_a)
        for stage in range(n_stages):
            out = out @ device_abcd_map(freqs_mhz)
            l_d, c_t, c_c = x[idx], x[idx + 1], x[idx + 2]
            idx += 3
            out = out @ _abcd_series_ind(l_d) @ _abcd_shunt_cap(c_t) @ _abcd_series_cap(c_c)
        return out

    import device_models as M

    def device_abcd_at(freqs_mhz):
        y = M.circuit_y2p(_np.asarray(freqs_mhz) * 1e6, params, "common_source", True)
        return _abcd_from_s(M.y_to_s(y, Z0))

    dev_design = device_abcd_at(band_design)
    dev_check = device_abcd_at(band_check)

    bounds = [(0.1e-12, 400e-12), (10e-9, 800e-9)] + \
             [(10e-9, 800e-9), (0.1e-12, 400e-12), (10e-12, 10e-9)] * n_stages
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    assert len(bounds) == dim

    def stability_penalty(x):
        a = build_abcd_generic_freqs(x, band_check, dev_check, n_stages)
        s = _s_from_abcd(a)
        worst = max(np.max(np.abs(s[:, 0, 0])), np.max(np.abs(s[:, 1, 1])))
        return 0.0 if worst < 0.99 else 1.0 + 20.0 * (worst - 0.99)

    def build_abcd_generic_freqs(x, freqs_mhz, dev_abcd, stages):
        global _FREQS
        _FREQS = freqs_mhz * 1e6
        out = np.tile(np.eye(2, dtype=complex), (len(freqs_mhz), 1, 1))
        idx = 0
        out = out @ _abcd_shunt_cap(x[idx]) @ _abcd_series_ind(x[idx + 1])
        idx += 2
        for _ in range(stages):
            out = out @ dev_abcd
            out = out @ _abcd_series_ind(x[idx]) @ _abcd_shunt_cap(x[idx + 1]) \
                @ _abcd_series_cap(x[idx + 2])
            idx += 3
        return out

    def cost(x):
        a = build_abcd_generic_freqs(x, band_design, dev_design, n_stages)
        s = _s_from_abcd(a)
        mag = np.abs(s[:, 1, 0])
        if not np.all(np.isfinite(mag)) or np.any(mag > 1e4):
            return 1e4
        # 回波约束：两条端口反射都尽力压到 -10 dB 以下，压不下去也报实际值
        s11_worst = np.max(np.abs(s[:, 0, 0]))
        s22_worst = np.max(np.abs(s[:, 1, 1]))
        reflect_penalty = 3.0 * max(0.0, s11_worst - 0.316) + 3.0 * max(0.0, s22_worst - 0.316)
        return (-np.min(mag) + 0.06 * (np.max(mag) - np.min(mag))
                + 8.0 * stability_penalty(x) + reflect_penalty)

    result = differential_evolution(cost, bounds, seed=seed, maxiter=maxiter,
                                    popsize=popsize, tol=1e-10, polish=False)
    x = np.clip(result.x, lo, hi)
    best = minimize(lambda v: cost(np.clip(v, lo, hi)), x, method="Nelder-Mead",
                    options={"maxiter": 8000, "xatol": 1e-13, "fatol": 1e-13})
    x = np.clip(best.x, lo, hi)
    if cost(x) > result.fun:
        x = np.clip(result.x, lo, hi)

    a_design = build_abcd_generic_freqs(x, band_design, dev_design, n_stages)
    s_design = _s_from_abcd(a_design)
    a_check = build_abcd_generic_freqs(x, band_check, dev_check, n_stages)
    s_check = _s_from_abcd(a_check)
    names = ["c_in_F", "l_in_H"]
    for stage in range(1, n_stages + 1):
        names += [f"l_d{stage}_H", f"c_tank{stage}_F", f"c_c{stage}_F"]
    metrics = summarize_generic(x, s_design, s_check, names, topology, band_check)
    return x, metrics


def summarize_generic(x, s, s_check, names, topology, band_check):
    mag = np.abs(s[:, 1, 0])
    s11 = np.abs(s[:, 0, 0])
    s22 = np.abs(s[:, 1, 1])
    check_worst = max(float(np.max(np.abs(s_check[:, 0, 0]))),
                      float(np.max(np.abs(s_check[:, 1, 1]))))
    return {
        "topology": topology,
        "components": {n: float(v) for n, v in zip(names, x)},
        "frequencies_MHz": BAND.tolist(),
        "s21_db": [float(20 * np.log10(v)) for v in mag],
        "s21_min_db": float(20 * np.log10(np.min(mag))),
        "s21_max_db": float(20 * np.log10(np.max(mag))),
        "s11_max": float(np.max(s11)),
        "s22_max": float(np.max(s22)),
        "s11_max_db": float(20 * np.log10(np.max(s11))),
        "s22_max_db": float(20 * np.log10(np.max(s22))),
        "stability_window_MHz": [float(band_check[0]), float(band_check[-1])],
        "stability_check_worst_reflection": check_worst,
        "stable_50ohm_terminations": bool(check_worst < 1.0),
        "meets_15db": bool(np.min(mag) > 10 ** (15 / 20)),
    }
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-record", default="output/acceptance_j310/manifest.json")
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "fm_j310_design")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path(args.model_record).read_text(encoding="utf-8"))
    os.environ.setdefault("ADS_AGENT_DATA_DIR",
                         str(Path(args.model_record).parent / "data"))
    import device_models as M

    record = M.get_model_record(manifest["model_id"])
    kind = record["fit"]["selected"]
    params = record["fit"]["candidates"][kind]["parameters_fitted_SI"]
    report = {"model_id": manifest["model_id"], "candidate": kind,
              "bias": "VDS=10V ID=10mA (datasheet typical curves), TA=25C",
              "band_MHz": [float(BAND[0]), float(BAND[-1])], "z0_ohm": Z0,
              "note": "固定偏置小信号线性模型；DC 偏置电路、非线性与噪声未验证"}
    # 器件特性（如实报告单级上限）
    s_dev = _device_s(np.array([98.0]), params)
    m = s_dev[0]
    det = m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]
    K = (1 - abs(m[0, 0]) ** 2 - abs(m[1, 1]) ** 2 + abs(det) ** 2) / (2 * abs(m[0, 1] * m[1, 0]))
    msg = abs(m[1, 0] / m[0, 1])
    report["device_at_98MHz"] = {
        "K": float(K), "MSG_db": float(10 * np.log10(msg)),
        "single_stage_bound_db": float(10 * np.log10(msg * (K - np.sqrt(K ** 2 - 1))
                                                     if K > 1 else msg)),
        "conclusion": "K<1，MSG≈11 dB：单级共源不可能达到全带 >15 dB（硬上限）",
    }
    print("器件 98MHz:", json.dumps(report["device_at_98MHz"], ensure_ascii=False), flush=True)

    for topology in ("two_stage", "three_stage"):
        x, metrics = optimize_generic(params, topology)
        report.setdefault("candidates", {})[topology] = metrics
        print(f"{topology}: S21min={metrics['s21_min_db']:.2f} dB "
              f"S21max={metrics['s21_max_db']:.2f} dB "
              f"S11max={metrics['s11_max_db']:.1f} dB "
              f"S22max={metrics['s22_max_db']:.1f} dB "
              f"stable={metrics['stable_50ohm_terminations']} "
              f"meets15={metrics['meets_15db']}", flush=True)
        for k, v in metrics["components"].items():
            if k.endswith("_H"):
                print(f"    {k} = {v*1e9:.3f} nH", flush=True)
            else:
                print(f"    {k} = {v*1e12:.3f} pF", flush=True)
    (args.out / "design_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", args.out / "design_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
