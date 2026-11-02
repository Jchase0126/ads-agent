"""FM 88-108 MHz 两级共源放大器：简化三电感拓扑的最终综合 + 理论上限核对。

拓扑（从源到负载，两枚器件级联）：
    Port1 → L_in(串联) → [器件1] → L_d1(串联) → [器件2] → L_d2(串联) → Port2
两个小电容器在上一轮优化里贴到"开路/短路"边界（0.1 pF / 10 nF），
等价于不存在，因此固定为三电感设计并以本脚本重新综合，
避免把"优化器边界伪影"带进 ADS 工程。

同时计算 Mason 单向增益 U —— 任意无耗嵌入能提供的增益上限，
用于回答"15 dB 目标在物理上是否有解"。
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

BAND = np.arange(88.0, 108.0001, 1.0)
Z0 = 50.0


def y_to_s_mats(y, z0):
    eye = np.eye(2)
    return np.array([(eye - z0 * m) @ np.linalg.inv(eye + z0 * m) for m in y])


def s_dev(freqs_mhz, params):
    import device_models as M
    y = M.circuit_y2p(np.asarray(freqs_mhz) * 1e6, params, "common_source", True)
    return y_to_s_mats(y, Z0)


def cascade_s(s_list):
    """级联若干二端口（S 参数序列，各自 (N,2,2)）。"""
    out = None
    for s in s_list:
        if out is None:
            out = s.copy()
            continue
        # S_total = S_A ⊗ S_B（先 A 后 B）
        n = len(out)
        st = np.zeros((n, 2, 2), complex)
        d = 1 - out[:, 1, 1] * s[:, 0, 0]
        st[:, 0, 0] = out[:, 0, 0] + out[:, 0, 1] * s[:, 0, 0] * out[:, 1, 0] / d
        st[:, 0, 1] = out[:, 0, 1] * s[:, 0, 1] / d
        st[:, 1, 0] = s[:, 1, 0] * out[:, 1, 0] / d
        st[:, 1, 1] = s[:, 1, 1] + s[:, 1, 0] * out[:, 1, 1] * s[:, 0, 1] / d
        out = st
    return out


def series_l(l_h, freqs_mhz):
    z = 1j * 2 * np.pi * np.asarray(freqs_mhz) * 1e6 * l_h
    s = np.zeros((len(freqs_mhz), 2, 2), complex)
    s[:, 0, 0] = z / (z + 2 * Z0)
    s[:, 0, 1] = 2 * Z0 / (z + 2 * Z0)
    s[:, 1, 0] = s[:, 0, 1]
    s[:, 1, 1] = s[:, 0, 0]
    return s


def mason_u(s):
    """Mason 单向增益（dB），任意无耗互易嵌入的增益上限。"""
    s11, s12, s21, s22 = s[:, 0, 0], s[:, 0, 1], s[:, 1, 0], s[:, 1, 1]
    det = s11 * s22 - s12 * s21
    k = (1 - np.abs(s11) ** 2 - np.abs(s22) ** 2 + np.abs(det) ** 2) / (2 * np.abs(s12 * s21))
    ratio = s21 / s12
    u = np.abs(ratio - 1) ** 2 / (2 * (k * np.abs(ratio) - ratio.real))
    return k, u


def build_two_stage(x, params, freqs_mhz):
    l_in, l_d1, l_d2 = x
    d = s_dev(freqs_mhz, params)
    return cascade_s([series_l(l_in, freqs_mhz), d, series_l(l_d1, freqs_mhz),
                      d, series_l(l_d2, freqs_mhz)])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    manifest = json.loads((ROOT / "output/acceptance_j310/manifest.json").read_text(encoding="utf-8"))
    import os
    os.environ.setdefault("ADS_AGENT_DATA_DIR", str(ROOT / "output/acceptance_j310/data"))
    import device_models as M
    rec = M.get_model_record(manifest["model_id"])
    kind = rec["fit"]["selected"]
    params = rec["fit"]["candidates"][kind]["parameters_fitted_SI"]

    d98 = s_dev(np.array([98.0]), params)[0]
    k, u = mason_u(np.array([d98]))[0] if False else (mason_u(d98[None])[0][0], mason_u(d98[None])[1][0])
    msg = abs(d98[1, 0] / d98[0, 1])
    print(f"器件@98MHz: K={k:.3f}  MSG={10*np.log10(msg):.2f} dB  "
          f"Mason U={10*np.log10(u):.2f} dB（任意无耗嵌入的增益上限）", flush=True)
    print(f"→ 15 dB 目标：U={10*np.log10(u):.2f} dB "
          f"{'可达（需反馈/嵌入）' if 10*np.log10(u) > 15 else '不可达'}", flush=True)

    # 三电感两级综合（含稳定性窗口检查）
    from scipy.optimize import differential_evolution, minimize
    chk = np.arange(20.0, 1000.0, 5.0)

    def cost(x):
        s = build_two_stage(x, params, BAND)
        mag = np.abs(s[:, 1, 0])
        if not np.all(np.isfinite(mag)):
            return 1e4
        s_chk = build_two_stage(x, params, chk)
        worst = max(np.max(np.abs(s_chk[:, 0, 0])), np.max(np.abs(s_chk[:, 1, 1])))
        pen = 0.0 if worst < 0.99 else 5.0 + 40.0 * (worst - 0.99)
        refl = 3.0 * max(0.0, np.max(np.abs(s[:, 0, 0])) - 0.316) \
            + 3.0 * max(0.0, np.max(np.abs(s[:, 1, 1])) - 0.316)
        return -np.min(mag) + 0.06 * (np.max(mag) - np.min(mag)) + pen + refl

    bounds = [(50e-9, 900e-9), (5e-9, 900e-9), (50e-9, 1200e-9)]
    res = differential_evolution(cost, bounds, seed=7, maxiter=400, popsize=25,
                                 tol=1e-11, polish=False)
    lo = np.array([b[0] for b in bounds]); hi = np.array([b[1] for b in bounds])
    best = minimize(lambda v: cost(np.clip(v, lo, hi)), res.x, method="Nelder-Mead",
                    options={"maxiter": 10000, "xatol": 1e-14, "fatol": 1e-14})
    x = np.clip(best.x, lo, hi)
    s = build_two_stage(x, params, BAND)
    s_chk = build_two_stage(x, params, chk)
    mag = np.abs(s[:, 1, 0])
    report = {
        "model_id": manifest["model_id"], "candidate": kind,
        "topology": "two_stage_common_source_three_inductor",
        "components_H": {"l_in": float(x[0]), "l_d1": float(x[1]), "l_d2": float(x[2])},
        "device_bound": {"K": float(k), "MSG_db": float(10 * np.log10(msg)),
                         "mason_U_db": float(10 * np.log10(u)),
                         "reaches_15db": bool(10 * np.log10(u) > 15)},
        "frequencies_MHz": BAND.tolist(),
        "s21_db": [float(20 * np.log10(v)) for v in mag],
        "s21_min_db": float(20 * np.log10(np.min(mag))),
        "s21_max_db": float(20 * np.log10(np.max(mag))),
        "s11_max_db": float(20 * np.log10(np.max(np.abs(s[:, 0, 0])))),
        "s22_max_db": float(20 * np.log10(np.max(np.abs(s[:, 1, 1])))),
        "stability_check": {
            "window_MHz": [20.0, 1000.0],
            "worst_reflection": float(max(np.max(np.abs(s_chk[:, 0, 0])),
                                          np.max(np.abs(s_chk[:, 1, 1])))),
            "stable_50ohm": bool(max(np.max(np.abs(s_chk[:, 0, 0])),
                                     np.max(np.abs(s_chk[:, 1, 1]))) < 1.0),
        },
        "meets_15db": bool(np.min(mag) > 10 ** (15 / 20)),
    }
    print(f"两级三电感: L_in={x[0]*1e9:.1f} nH  L_d1={x[1]*1e9:.1f} nH  "
          f"L_d2={x[2]*1e9:.1f} nH", flush=True)
    print(f"  S21: {report['s21_min_db']:.2f} .. {report['s21_max_db']:.2f} dB  "
          f"S11max={report['s11_max_db']:.1f} dB  S22max={report['s22_max_db']:.1f} dB  "
          f"stable={report['stability_check']['stable_50ohm']}  meets15={report['meets_15db']}",
          flush=True)
    out = ROOT / "output" / "fm_j310_design" / "final_design.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
