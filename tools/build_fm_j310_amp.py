"""FM 88-108 MHz 两级 J310 共源放大器：ADS 实机建图 + 仿真 + 与数值设计对照。

拓扑（从源到负载）：
    Port1 → L_in → [封装 Rg/Lg + 器件1(共源) ] → L_d1 → [封装 + 器件2] → L_d2 → Port2
封装 R/L（拟合值）显式画出，与数值模型的"端子串联支路"一一对应；
源极经 Rs/Ls 接地。

用法（ADS 捆绑 Python）：
    python tools/build_fm_j310_amp.py --workspace E:\\ADS\\Work_AI\\FM_J310_wrk
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "addon" / "ads_agent"))

BAND = (88.0, 108.0)


def _fmt(value: float, unit: str) -> str:
    return f"{value:.12g} {unit}"


def build_instances(params, l_in, l_d1, l_d2, start_mhz=80.0,
                    stop_mhz=120.0, step_mhz=0.2):
    """两级共源：每枚器件带完整封装 R/L（拟合值），级间与输入输出为串联电感。"""
    inst = [
        {"master": "ads_simulation:TermG", "name": "PORT1", "params": {"Num": "1", "Z": "50 Ohm"}},
        {"master": "ads_rflib:L", "name": "L_IN", "params": {"L": _fmt(l_in * 1e9, "nH")}},
        {"master": "ads_simulation:TermG", "name": "PORT2", "params": {"Num": "2", "Z": "50 Ohm"}},
        {"master": "ads_rflib:GROUND", "name": "GND"},
        {"master": "ads_simulation:S_Param", "name": "SP1", "params": {
            "Start": f"{start_mhz:g} MHz", "Stop": f"{stop_mhz:g} MHz",
            "Step": f"{step_mhz:g} MHz",
            "CalcS": "yes", "CalcY": "yes", "CalcNoise": "No"}},
    ]
    for stage, drain_out in ((1, "N_D1"), (2, "N_D2")):
        core = {
            "G": _fmt(params["gm_S"] * 1e3, "mS"),
            "T": _fmt(params.get("tau_s", 0.0) * 1e12, "psec"),
            "F": "0 GHz",
            "Cgs": _fmt(params["Cgs_F"] * 1e12, "pF"),
            "Cdg": _fmt(params["Cgd_F"] * 1e12, "pF"),
            "Cds": _fmt(params["Cds_F"] * 1e12, "pF"),
            "Cdc": "0 pF", "Ggs": "0 S", "Ri": "0 Ohm",
            "Rds": _fmt(1.0 / params["gds_S"] if params["gds_S"] > 0 else 1e12, "Ohm"),
        }
        inst += [
            {"master": "ads_rflib:FET", "name": f"J310_{stage}", "params": core},
            {"master": "ads_rflib:R", "name": f"RG{stage}",
             "params": {"R": _fmt(params["Rg_Ohm"], "Ohm"), "Noise": "No"}},
            {"master": "ads_rflib:L", "name": f"LG{stage}",
             "params": {"L": _fmt(params["Lg_H"] * 1e9, "nH"), "Noise": "No"}},
            {"master": "ads_rflib:R", "name": f"RS{stage}",
             "params": {"R": _fmt(params["Rs_Ohm"], "Ohm"), "Noise": "No"}},
            {"master": "ads_rflib:L", "name": f"LS{stage}",
             "params": {"L": _fmt(params["Ls_H"] * 1e9, "nH"), "Noise": "No"}},
            {"master": "ads_rflib:R", "name": f"RD{stage}",
             "params": {"R": _fmt(params["Rd_Ohm"], "Ohm"), "Noise": "No"}},
            {"master": "ads_rflib:L", "name": f"LD{stage}",
             "params": {"L": _fmt(params["Ld_H"] * 1e9, "nH"), "Noise": "No"}},
        ]
    inst += [
        {"master": "ads_rflib:L", "name": "L_D1", "params": {"L": _fmt(l_d1 * 1e9, "nH")}},
        {"master": "ads_rflib:L", "name": "L_D2", "params": {"L": _fmt(l_d2 * 1e9, "nH")}},
    ]
    # ads_rflib:FET 原生符号引脚：gate=1, drain=2, source=3（运行时实测）
    conns = [
        {"a": ["PORT1", 1], "b": ["L_IN", 1]},
        {"a": ["L_IN", 2], "b": ["RG1", 1]},
        {"a": ["RG1", 2], "b": ["LG1", 1]},
        {"a": ["LG1", 2], "b": ["J310_1", 1]},          # gate
        {"a": ["J310_1", 3], "b": ["RS1", 1]},          # source
        {"a": ["RS1", 2], "b": ["LS1", 1]},
        {"a": ["LS1", 2], "b": ["GND", 1]},
        {"a": ["J310_1", 2], "b": ["RD1", 1]},          # drain
        {"a": ["RD1", 2], "b": ["LD1", 1]},
        {"a": ["LD1", 2], "b": ["L_D1", 1]},
        {"a": ["L_D1", 2], "b": ["RG2", 1]},            # 级间：到第二级栅极
        {"a": ["RG2", 2], "b": ["LG2", 1]},
        {"a": ["LG2", 2], "b": ["J310_2", 1]},
        {"a": ["J310_2", 3], "b": ["RS2", 1]},
        {"a": ["RS2", 2], "b": ["LS2", 1]},
        {"a": ["LS2", 2], "b": ["GND", 1]},
        {"a": ["J310_2", 2], "b": ["RD2", 1]},
        {"a": ["RD2", 2], "b": ["LD2", 1]},
        {"a": ["LD2", 2], "b": ["L_D2", 1]},
        {"a": ["L_D2", 2], "b": ["PORT2", 1]},
    ]
    return inst, conns


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--library", default="FM_J310_lib")
    parser.add_argument("--design", default=str(ROOT / "output/fm_j310_design/final_design.json"))
    parser.add_argument("--cell", default="FM_J310_CS_2STG")
    parser.add_argument("--start-mhz", type=float, default=80.0)
    parser.add_argument("--stop-mhz", type=float, default=120.0)
    parser.add_argument("--step-mhz", type=float, default=0.2)
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    report = {"started_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "design": design["topology"], "workspace": str(args.workspace)}
    os.environ.setdefault("ADS_AGENT_DATA_DIR", str(ROOT / "output/acceptance_j310/data"))
    import device_models as M
    rec = M.get_model_record(design["model_id"])
    params = rec["fit"]["candidates"][design["candidate"]]["parameters_fitted_SI"]
    l_in = design["components_H"]["l_in"]
    l_d1 = design["components_H"]["l_d1"]
    l_d2 = design["components_H"]["l_d2"]
    inst, conns = build_instances(params, l_in, l_d1, l_d2,
                                  args.start_mhz, args.stop_mhz, args.step_mhz)

    from keysight.ads import de
    import ads_ops

    workspace = args.workspace
    if de.workspace_is_open():
        print("[FAIL] 已有工作区打开；请在隔离进程里运行")
        return 2
    if workspace.exists():
        w = de.open_workspace(str(workspace))
        if args.library not in [l.name for l in w.libraries]:
            de.create_new_library(args.library, str(workspace / args.library))
            w.add_library(args.library, str(workspace / args.library), de.LibraryMode.NON_SHARED)
    else:
        w = de.create_workspace(str(workspace))
        w.open()
        de.create_new_library(args.library, str(workspace / args.library))
        w.add_library(args.library, str(workspace / args.library), de.LibraryMode.NON_SHARED)

    # 上一轮验收在本工作区留下的失败 cell（未通过建图门禁）：
    # 这是本脚本自建工作区里的验收产物，删除后重建干净版本。
    try:
        lib = next(l for l in w.libraries if l.name == args.library)
        if args.cell in [c.name for c in lib.cells]:
            lib.delete_cell(args.cell)
            print(f"[note] 已清理上一轮失败验收 cell {args.cell}", flush=True)
    except (StopIteration, AttributeError) as exc:
        print(f"[note] cell 清理跳过：{type(exc).__name__}", flush=True)

    spec = {"library": args.library, "cell": args.cell, "layout": "auto",
            "recreate": False, "instances": inst, "connections": conns}
    try:
        built = ads_ops.build_schematic(spec)
    except Exception as exc:  # noqa: BLE001
        print("[FAIL] build:", exc)
        report["build_error"] = f"{type(exc).__name__}: {exc}"
        (Path(args.design).parent / "ads_build_result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        de.close_workspace()
        return 1
    equiv = built.get("netlist_equivalence") or {}
    geom = built.get("geometry") or {}
    print(f"[ok ] build {args.cell}: instances={built.get('n_instances')} nets={built.get('n_nets')} "
          f"netlist_diffs={len(equiv.get('diffs') or [])} geom_problems={len(geom.get('problems') or [])}",
          flush=True)
    report["build"] = {"n_instances": built.get("n_instances"), "n_nets": built.get("n_nets"),
                       "netlist_equivalence_ok": bool(equiv.get("ok")),
                       "geometry_problems": geom.get("problems") or [],
                       "parameters_ok": built.get("parameters_ok")}

    sim = ads_ops.run_simulation({"library": args.library, "cell": args.cell})
    ds_path = sim["dataset_path"]
    print(f"[ok ] simulate: {ds_path}", flush=True)
    exprs = [f"{op}(S({i},{j}))" for i, j in ((1, 1), (2, 1), (1, 2), (2, 2))
             for op in ("real", "imag")]
    traces = ads_ops.read_traces({"path": ds_path, "expressions": exprs, "max_points": 0})
    tr = traces.get("traces") or {}

    def curve(expr):
        row = tr.get(expr) or {}
        return row.get("x") or [], row.get("y") or []

    freqs = curve("real(S(2,1))")[0]
    s21 = [complex(a, b) for a, b in zip(curve("real(S(2,1))")[1], curve("imag(S(2,1))")[1])]
    s11 = [complex(a, b) for a, b in zip(curve("real(S(1,1))")[1], curve("imag(S(1,1))")[1])]
    s22 = [complex(a, b) for a, b in zip(curve("real(S(2,2))")[1], curve("imag(S(2,2))")[1])]
    band = [(f, c, a, b) for f, c, a, b in zip(freqs, s21, s11, s22)
            if BAND[0] * 1e6 <= f <= BAND[1] * 1e6]
    covers_band = len(band) > 0 and band[0][0] <= BAND[0] * 1e6 + 1         and band[-1][0] >= BAND[1] * 1e6 - 1
    s21_db = [20 * __import__("math").log10(abs(c)) for _, c, _, _ in band]
    if not covers_band:
        # 非带内扫频（如宽带稳定性检查）：不算 15 dB 判定，只报最坏反射
        worst = max(max(abs(a) for _, _, a, _ in band or [(0, 0, 0, 0)]),
                    max(abs(b) for _, _, _, b in band or [(0, 0, 0, 0)]))
        report["ads"] = {
            "dataset_path": ds_path, "points_total": len(freqs),
            "points_band": len(band), "band_MHz": list(BAND),
            "sweep_MHz": [args.start_mhz, args.stop_mhz],
            "stable_50ohm_terminations": bool(worst < 1.0),
            "worst_reflection": float(worst),
        }
        print(f"[ok ] wideband {args.start_mhz:g}-{args.stop_mhz:g} MHz: "
              f"worst |S11|/|S22| = {worst:.3f} "
              f"({'稳定（无负阻）' if worst < 1 else '存在 |S|≥1：有源区'})", flush=True)
        report["cell"] = args.cell
        report["library"] = args.library
        report["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        out = Path(args.design).parent / (f"ads_build_result{'_stab' if args.cell.endswith('STAB') else ''}.json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        print("saved:", out, flush=True)
        de.close_workspace()
        return 0
    report["ads"] = {
        "dataset_path": ds_path, "points_total": len(freqs), "points_band": len(band),
        "band_MHz": list(BAND),
        "s21_min_db": min(s21_db), "s21_max_db": max(s21_db),
        "s11_max_db": 20 * __import__("math").log10(max(abs(a) for _, _, a, _ in band)),
        "s22_max_db": 20 * __import__("math").log10(max(abs(b) for _, _, _, b in band)),
        "s21_at_MHz": {str(int(f / 1e6)): round(20 * __import__("math").log10(abs(c)), 3)
                       for f, c, _, _ in band if int(f / 1e6) in (88, 98, 108)},
        "meets_15db": min(s21_db) > 15.0,
    }
    # 数值设计对照（同一组 S 参数模型独立重算）
    import numpy as np
    sys.path.insert(0, str(ROOT / "tools"))
    import design_fm_final as D
    freqs_mhz = [f / 1e6 for f, _, _, _ in band]
    s_model = D.build_two_stage((l_in, l_d1, l_d2), params, np.asarray(freqs_mhz))
    s21_model = s_model[:, 1, 0]
    diffs = [abs(abs(complex(*pair)) - abs(m)) for pair, m in zip(((c.real, c.imag) for _, c, _, _ in band), s21_model)]
    report["model_vs_ads"] = {
        "note": "ADS 仿真 |S21| 与同参数数值模型的逐点差（应为数值噪声级）",
        "max_abs_diff": float(max(diffs)),
        "s21_model_min_db": float(20 * np.log10(np.min(np.abs(s21_model)))),
    }
    print(f"[ok ] band {BAND[0]:.0f}-{BAND[1]:.0f} MHz: "
          f"S21 {report['ads']['s21_min_db']:.2f}..{report['ads']['s21_max_db']:.2f} dB  "
          f"S11max={report['ads']['s11_max_db']:.1f} dB  S22max={report['ads']['s22_max_db']:.1f} dB  "
          f"meets15={report['ads']['meets_15db']}", flush=True)
    print(f"      model-vs-ADS max|dS21|={report['model_vs_ads']['max_abs_diff']:.3e}", flush=True)
    report["cell"] = args.cell
    report["library"] = args.library
    report["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    out = Path(args.design).parent / "ads_build_result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("saved:", out, flush=True)
    de.close_workspace()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
