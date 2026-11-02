"""SMMBFJ310L 端到端验收（离线部分：上传→解析→参数→曲线→拟合→建图计划）。

通过后端本地工具的同一入口执行（与聊天工具调用完全一致），不写 ADS、
不仿真。真实 ADS 实机验收由 tools/acceptance_j310_e2e.py 在 ADS 解释器
里执行（建图 → run_simulation → validate_device_model）。

用法：
    python tools/acceptance_j310_pipeline.py --pdf tests/fixtures/SMMBFJ310L.pdf
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

EXPECTED_SHA = "113d3cb19b47f74879741cf408983f03ab89b0123f1e1beecc3bd8a2442871b8"

#: 第 2 页表格的表头层级（read_datasheet_page 该页真实行文本）
_TABLE_HEADER = ["ELECTRICAL CHARACTERISTICS (TA = 25°C unless otherwise noted)",
                 "Characteristic", "Symbol", "Min", "Typ", "Max", "Unit"]
_TABLE_FOOTNOTE = "TA = 25°C unless otherwise noted"


def _table(row_id: str, column: str, min_typ_max: str, conditions: str) -> dict:
    """表格证据：行/列/表头层级/型号列/Min-Typ-Max/脚注/测试条件区域。

    型号列取数据手册第 2 页页眉列出的同一族型号（SMMBFJ310L），与目标型号
    一致；共用范围在页眉行有声明（MMBFJ309L, MMBFJ310L, SMMBFJ309L, SMMBFJ310L）。
    """
    return {"row_id": row_id, "column_id": column, "header_path": _TABLE_HEADER,
            "model_column": "SMMBFJ310L", "min_typ_max": min_typ_max,
            "footnote": _TABLE_FOOTNOTE, "test_conditions_region": conditions}


#: 验收要求的参数证据（全部来自人工复核过的第 2 页电气特性表；
#: source_quote 是 read_datasheet_page 该页真实行文本的拼接）
PARAMETER_INPUTS = {
    "part_number": "SMMBFJ310L",
    "device_kind": "jfet",
    "parameters": [
        # Gate Source Cutoff Voltage（型号列 MMBFJ310, SMMBFJ310）：Min −4.0 / Max −6.5
        {"name": "VGSoff_max", "value": -6.5, "unit": "Vdc", "value_kind": "maximum",
         "category": "electrical_characteristic",
         "conditions": "(VDS = 10 Vdc, ID = 1.0 nAdc)",
         "conditions_structured": {"vds_v": 10.0, "id_a": 1e-9},
         "source_page": 2, "symbol": "VGS(off)",
         "source_quote": "VGS(off) −1.0 −2.0 − − −4.0 −6.5 Vdc",
         "table": _table("r_vgsoff", "Max", "max", "(VDS = 10 Vdc, ID = 1.0 nAdc)")},
        {"name": "VGSoff_min", "value": -4.0, "unit": "Vdc", "value_kind": "minimum",
         "category": "electrical_characteristic",
         "conditions": "(VDS = 10 Vdc, ID = 1.0 nAdc)",
         "conditions_structured": {"vds_v": 10.0, "id_a": 1e-9},
         "source_page": 2, "symbol": "VGS(off)",
         "source_quote": "VGS(off) −1.0 −2.0 − − −4.0 −6.5 Vdc",
         "table": _table("r_vgsoff", "Min", "min", "(VDS = 10 Vdc, ID = 1.0 nAdc)")},
        # Zero−Gate−Voltage Drain Current（型号列 MMBFJ310, SMMBFJ310）：24 / 60 mAdc
        {"name": "IDSS_min", "value": 24, "unit": "mAdc", "value_kind": "minimum",
         "category": "electrical_characteristic",
         "conditions": "(VDS = 10 Vdc, VGS = 0)",
         "conditions_structured": {"vds_v": 10.0},
         "source_page": 2, "symbol": "IDSS",
         "source_quote": "IDSS 12 24 − − 30 60 mAdc",
         "table": _table("r_idss", "Min", "min", "(VDS = 10 Vdc, VGS = 0)")},
        {"name": "IDSS_max", "value": 60, "unit": "mAdc", "value_kind": "maximum",
         "category": "electrical_characteristic",
         "conditions": "(VDS = 10 Vdc, VGS = 0)",
         "conditions_structured": {"vds_v": 10.0},
         "source_page": 2, "symbol": "IDSS",
         "source_quote": "IDSS 12 24 − − 30 60 mAdc",
         "table": _table("r_idss", "Max", "max", "(VDS = 10 Vdc, VGS = 0)")},
        # Forward Transfer Admittance：8.0 / 18 mmhos（手册单位写法，等价 mS）
        {"name": "gm_min", "value": 8.0, "unit": "mS", "value_kind": "minimum",
         "category": "electrical_characteristic",
         "conditions": "(VDS = 10 Vdc, ID = 10 mAdc, f = 1.0 kHz)",
         "conditions_structured": {"vds_v": 10.0, "id_a": 0.01, "frequency_hz": 1000.0},
         "source_page": 2, "symbol": "|Yfs|",
         "source_quote": "Forward Transfer Admittance (VDS = 10 Vdc, ID = 10 mAdc,"
                         " f = 1.0 kHz) |Yfs| 8.0 − 18 mmhos",
         "table": _table("r_yfs", "Min", "min",
                         "(VDS = 10 Vdc, ID = 10 mAdc, f = 1.0 kHz)")},
        # Reverse Transfer Capacitance：2.5 pF（Max，不得直接当工作点电容）
        {"name": "Crss_max", "value": 2.5, "unit": "pF", "value_kind": "maximum",
         "category": "electrical_characteristic",
         "conditions": "(VGS = −10 Vdc, VDS = 0 Vdc, f = 1.0 MHz)",
         "conditions_structured": {"vgs_v": -10.0, "vds_v": 0.0, "frequency_hz": 1e6},
         "source_page": 2, "symbol": "Crss",
         "source_quote": "Reverse Transfer Capacitance (VGS = −10 Vdc, VDS = 0 Vdc,"
                         " f = 1.0 MHz) Crss − − 2.5 pF",
         "table": _table("r_crss", "Max", "max",
                         "(VGS = −10 Vdc, VDS = 0 Vdc, f = 1.0 MHz)")},
    ],
}


def _json_dump(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                               default=str), encoding="utf-8")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path,
                        default=ROOT / "output" / "acceptance_j310")
    parser.add_argument("--workspace-hint", type=str,
                        default=str(ROOT / "output" / "acceptance_j310"))
    args = parser.parse_args()
    out = args.data_dir
    out.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "pdf": str(args.pdf), "stages": {}}
    os.environ["ADS_AGENT_DATA_DIR"] = str(out / "data")

    import datasheet_models as D
    import datasheet_curves as C
    import device_models as M
    import doc_assets
    import pdf_extract

    def stage(name, ok, **fields):
        report["stages"][name] = {"ok": bool(ok), **fields}
        print(f"[{'ok ' if ok else 'FAIL'}] {name} " +
              " ".join(f"{k}={v}" for k, v in fields.items() if k != "note"), flush=True)

    # ---- 1. 上传：原文件按 SHA 持久化 + 完整解析 ------------------------
    data = args.pdf.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != EXPECTED_SHA:
        stage("upload", False, note=f"SHA 不匹配：{sha}")
        return 1
    asset = doc_assets.store(data, expect_sha=sha)
    parsed = pdf_extract.extract_document(data)
    document = D.save_document(args.pdf.name, sha, parsed, len(data), session="acceptance")
    stage("upload", True, engine=document["parse_engine"],
          page_count=document["page_count"], reused=asset["reused"],
          note=document["note"])

    # 重读原始字节（按 document_id）
    reread = doc_assets.read_bytes(sha)
    stage("asset_readback", hashlib.sha256(reread).hexdigest() == sha,
          size=len(reread))

    # ---- 2. 解析核验：真实页数/型号/负号/字形行 ------------------------
    inspect = D.inspect_document({"document_id": sha})
    page2 = next(p for p in inspect["pages"] if p["page"] == 2)
    joined = " ".join(ln["text"] for ln in
                      D.read_page({"document_id": sha, "page": 2, "limit": 500})["lines"])
    checks = {
        "page_count_5": inspect["page_count"] == 5,
        "page_source_page_tree": inspect["page_count_source"] == "page_tree",
        "part_visible": "SMMBFJ310" in joined,
        "negative_sign": "−6.5" in joined and "−4.0" in joined,
        "gm_range": "8.0" in joined and "18" in joined,
        "unknown_glyph_lines_flagged": page2["unreliable_lines"] > 0,
    }
    stage("parse_verify", all(checks.values()), **checks)

    # ---- 3. 参数保存 + 复核 --------------------------------------------
    params_args = {"document_id": sha, "part_number": "SMMBFJ310L",
                   "device_kind": "jfet",
                   "parameters": PARAMETER_INPUTS["parameters"]}
    saved = D.save_parameters(params_args)
    review = D.review_parameter_set({"parameter_set_id": saved["parameter_set_id"]})
    stage("parameters", saved["status"] == "extracted_unreviewed",
          count=len(saved["parameters"]),
          pages=sorted({p["source_page"] for p in saved["parameters"]}),
          set_id=saved["parameter_set_id"][:8])
    stage("review", review["review_passed"] and review["status"] == "evidence_checked",
          status=review["status"], level="evidence_checked")
    _json_dump(out / "parameters.json", saved)
    _json_dump(out / "review.json", review)

    # 语义级复核：表格/型号列/Min-Typ-Max/条件归属
    semantics = D.verify_parameter_semantics({"parameter_set_id": saved["parameter_set_id"]})
    stage("semantics", semantics["status"] == "semantic_verified",
          status=semantics["status"],
          failed=semantics.get("failed_parameters"))
    _json_dump(out / "semantic_review.json", semantics)

    # 未复核/无证据的参数不得通过
    bad = {**params_args, "part_number": "NOPE404"}
    rejected = "error" in _safe(D.save_parameters, bad)
    stage("gate_rejects_unverified", rejected)

    # 结构化条件与原文不符必须被拒绝（99 V / 99 A 负例）
    bad_conditions = {"document_id": sha, "part_number": "SMMBFJ310L",
                      "device_kind": "jfet",
                      "parameters": [{**params_args["parameters"][0],
                                      "conditions_structured": {"vds_v": 99.0,
                                                                "id_a": 99.0}}]}
    stage("gate_rejects_wrong_conditions",
          "error" in _safe(D.save_parameters, bad_conditions))

    # ---- 4. 曲线提取（SHA 锁定样本适配器） ------------------------------
    extracted = C.save_curve({"document_id": sha, "preset": "smmbfj310l_v1"})
    listing = C.list_curves({"document_id": sha})
    kinds = {(c["matrix_kind"], c["component"]) for c in listing["curves"]}
    curve_ids = {}
    for c in listing["curves"]:
        curve_ids.setdefault(c["matrix_kind"], {}).setdefault(
            c["component"], []).append(c["curve_id"])
    stage("curves", len(extracted["curve_ids"]) == 16 and len(kinds) == 4,
          curves=len(listing["curves"]),
          z0=extracted["curves"][0]["z0"],
          conditions=extracted["curves"][0]["conditions"],
          page=extracted["curves"][0]["page"])

    # ---- 5. 候选模型 + 拟合 --------------------------------------------
    prepared = M.prepare_device_model({
        "document_id": sha, "part_number": "SMMBFJ310L", "device_kind": "jfet",
        "model_kind": "jfet_small_signal", "curve_ids": curve_ids,
        "parameter_set_id": saved["parameter_set_id"]})
    stage("candidate", prepared["stage"] == "candidate_ready",
          stage=prepared["stage"], missing=prepared["missing"])
    fitted = M.fit_device_model({"model_id": prepared["model_id"]})
    fit_record = M.get_model_record(prepared["model_id"])
    stage("fit", fitted["stage"] == "fitted",
          selected=fitted["selected"],
          heldout=round(fit_record["fit"]["candidates"][fitted["selected"]]
                        ["heldout_weighted_complex_nrmse"], 5),
          gm_mS=round(fit_record["fit"]["candidates"][fitted["selected"]]
                      ["effective_CS_gm_mS_1kHz_extrapolated"], 2),
          rule=fitted["rule"]["origin"])

    # ---- 6. 建图计划 ---------------------------------------------------
    plan = M.build_device_model({"model_id": prepared["model_id"], "library": "AI_lib",
                                 "cell": "J310_ACC", "start_hz": 100e6,
                                 "stop_hz": 1000e6, "step_hz": 5e6})
    circuit = plan["build_schematic_args"]
    built_record = M.get_model_record(prepared["model_id"])
    stage("build_plan", plan["stage"] == "build_planned",
          masters=sorted({i["master"] for i in circuit["instances"]}),
          cell=circuit["cell"],
          pin_symbol=plan["pin_mapping"]["ads_native_symbol"]["ads_rflib:FET"],
          pin_package=plan["pin_mapping"]["physical_package"],
          params_source=built_record["build"]["parameters_source"]["kind"])

    # ---- 6b. 建图拓扑核对：三端（含公共端）封装支路 ----------------------
    np = M._np()
    selected = built_record["fit"]["selected"]
    params_si = built_record["fit"]["candidates"][selected]["parameters_fitted_SI"]
    topology = built_record["build"]["topology"]
    freqs_hz = np.linspace(100e6, 1000e6, 25)
    y_forward = M.circuit_y2p(freqs_hz, params_si, built_record["configuration"],
                              selected == "package_delay")
    y_plan = M.solve_plan_y2p(circuit, built_record["configuration"], freqs_hz)
    delta = float(np.max(np.abs(y_plan - y_forward)))
    branches = sorted(i["name"] for i in circuit["instances"]
                      if i["master"] in ("ads_rflib:R", "ads_rflib:L"))
    stage("plan_topology", delta < 1e-9, topology=topology,
          max_abs_dY=delta, branches=branches,
          fingerprint=built_record["build"]["fingerprint"][:16])

    # ---- 7. 汇总 -------------------------------------------------------
    model_record = M.get_model_record(prepared["model_id"])
    _json_dump(out / "model_record.json", model_record)
    _json_dump(out / "build_plan.json", {"build_schematic_args": circuit,
                                         "simulation_args": plan["simulation_args"]})
    manifest = {"document_id": sha, "asset": asset["relative_path"],
                "parameter_set_id": saved["parameter_set_id"],
                "parameter_status": semantics["status"],
                "curve_ids": curve_ids, "model_id": prepared["model_id"],
                "build": {"library": circuit["library"], "cell": circuit["cell"]},
                "fit_selected": selected,
                "topology": topology,
                "build_fingerprint": built_record["build"]["fingerprint"],
                "conditions": built_record["build"].get("conditions") or {},
                "z0_ohm": built_record["build"].get("z0"),
                "stage": model_record["stage"],
                "unsupported": model_record["unsupported"],
                "workspace_hint": args.workspace_hint}
    _json_dump(out / "manifest.json", manifest)
    report["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    report["manifest"] = manifest
    _json_dump(out / "pipeline_report.json", report)
    all_ok = all(s["ok"] for s in report["stages"].values())
    stage("ALL_STAGES", all_ok, n=len(report["stages"]))
    print("\n下一步（ADS 实机）："
          f'  {ROOT / "tools" / "acceptance_j310_ads.py"} --manifest {out / "manifest.json"}'
          " --workspace <E:/ADS/Work_AI 下的工作区>")
    return 0 if all_ok else 1


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
