"""仿真来源绑定门禁回归（2026-10-14 A 阶段）。

审查 P1-1 的复现口径：来源此前可由调用方自填 —— `_with_simulation_evidence`
见到 `args["simulation"]` 非空就直接返回，而 `_dataset_provenance` 对
library/cell/dataset_path 只"有值才比"、网表指纹只查非空，于是

    simulation={"netlist_sha": "invented", "workspace": "Z:/different",
                "library": None, "cell": None, ...}

仍能拿到 `provenance.state == "verified"`。本文件用**真实文档 + 真实拟合**
构造可信记录，然后逐条打断，断言门禁确实拦住。

另外覆盖审查要求里此前没有独立用例的语义：
* 来源必需字段缺失 → unverified；
* 占位指纹（invented / netlist_only / 非 hex）→ unverified；
* 数据集在验证读取前后被替换 → unverified；
* 进程异常退出（0xC0000409 算完才崩）→ 只作诊断证据；
* 独立网表正演 → 只记 numeric_spec_validated，**绝不** validated；
* prepare_device_model 拒绝跨手册 / 跨型号参数集；
* 状态机区分 schematic_built / simulation_completed / numeric_spec_validated。

运行（需 numpy+scipy，**不要**用缺数值库的默认解释器）::

    "C:/Users/27244/.workbuddy/binaries/python/envs/default/Scripts/python.exe" \
        tests/test_sim_provenance.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _harness import contains, eq, ne, ok, raises, run  # noqa: E402

try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:  # pragma: no cover
    HAVE_NUMPY = False

import device_models as M  # noqa: E402
import sim_records  # noqa: E402

# 复用 test_pdf_modeling 的真实合成模型构造（同一套数值口径）
from test_pdf_modeling import _synthesize_model  # noqa: E402


def _ctx(tag: str):
    """造一个已建图、已仿真的完整可信场景。

    关键：**数据集轨迹由拟合参数正演生成**（`circuit_y2p`），不是随手编的 ——
    网表里放的是拟合值，所以"理想 ADS 数据集"也必须由同一组拟合值算出，
    否则数值等价会真的失败，测的就不是来源门禁而是数值分账了。

    `_synthesize_model` 自建数据根并返回它，模型/曲线/参数记录都在那个根下。
    """
    os.environ["ADS_AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="sp0_" + tag + "_")
    doc_id = (tag * 64)[:64]
    model_id, truth, freqs_mhz, root = _synthesize_model(doc_id, tau_s=0.0)
    os.environ["ADS_AGENT_DATA_DIR"] = root
    M.fit_device_model({"model_id": model_id, "candidates": ["intrinsic"]})
    M.build_device_model({"model_id": model_id, "library": "AI_lib",
                          "cell": "J310_SIMPROV", "start_hz": 1e8,
                          "stop_hz": 1e9, "step_hz": 5e6})
    record = M.get_model_record(model_id)
    build = record["build"]
    dataset = os.path.join(root, "sim.ds")
    with open(dataset, "wb") as stream:
        stream.write(b"!dataset placeholder\n" + b"x" * 4096)

    np = M._np()
    kind = record["fit"]["selected"]
    fitted = record["fit"]["candidates"][kind]["parameters_fitted_SI"]
    freqs_hz = np.asarray([f * 1e6 for f in freqs_mhz], dtype=float)
    y2p = M.circuit_y2p(freqs_hz, fitted, record["configuration"],
                        kind == "package_delay")
    s_actual = M.y_to_s(y2p, 50.0)
    traces = {}
    for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
        traces[f"real(S({i},{j}))"] = {
            "x": freqs_hz.tolist(),
            "y": s_actual[:, i - 1, j - 1].real.tolist()}
        traces[f"imag(S({i},{j}))"] = {
            "x": freqs_hz.tolist(),
            "y": s_actual[:, i - 1, j - 1].imag.tolist()}

    sim_id = sim_records.begin({
        "workspace": "E:/ADS/Work_AI/simprov_wrk",
        "library": build["library"], "cell": build["cell"],
        "view": "schematic", "build_fingerprint": build["fingerprint"],
        "netlist_sha": "deadbeefcafe0001"})
    sim_records.complete(sim_id, dataset_path=dataset,
                         netlist_sha="deadbeefcafe0001", process_exit=0,
                         dataset_ok=True, controller_trace_ok=True,
                         workspace="E:/ADS/Work_AI/simprov_wrk")
    return {"folder": root, "model_id": model_id, "dataset": dataset,
            "record": record, "simulation_id": sim_id, "traces": traces,
            "truth": truth, "freqs": freqs_mhz}


def _validate(ctx, **over):
    args = {"model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"], "simulation_id": ctx["simulation_id"],
            "thresholds": {"max_complex_rmse_S": 1e-3, "max_complex_rmse_Y": 1e-6}}
    args.update(over)
    return M.validate_device_model(args)


class _Root:
    """在 with 期间把 ADS_AGENT_DATA_DIR 指向该场景的临时数据根。"""

    def __init__(self, folder):
        self.folder = folder
        self.prev = None

    def __enter__(self):
        self.prev = os.environ.get("ADS_AGENT_DATA_DIR")
        os.environ["ADS_AGENT_DATA_DIR"] = self.folder
        return self

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("ADS_AGENT_DATA_DIR", None)
        else:
            os.environ["ADS_AGENT_DATA_DIR"] = self.prev
        return False


#: 参数归属测试用的最小真实文档/参数集（真实走 save → review → verify 流程）
DOC = "9" * 64
_PAGE2 = [
    "ELECTRICAL CHARACTERISTICS (TA = 25 C)",
    "Gate Source Cutoff Voltage (VDS = 10 Vdc, ID = 1.0 nAdc)",
    "VGS(off) −2.0 −6.5 Vdc",
    "Zero Gate Voltage Drain Current (VDS = 10 Vdc, VGS = 0)",
    "IDSS 24 60 mAdc",
]


def _make_doc(folder):
    """落一份 2 页手工行证据文档（电气特性表）。"""
    import datasheet_models as DM
    pages = [
        {"page": 1, "width": 612, "height": 792, "has_text": True, "chars": 40,
         "images": 0, "line_count": 1, "lines": [
             {"text": "PART999 N-Channel JFET", "bbox": [10, 10, 200, 24],
              "reliable": True}]},
        {"page": 2, "width": 612, "height": 792, "has_text": True,
         "chars": 320, "images": 0, "line_count": len(_PAGE2),
         "lines": [{"text": t, "bbox": [10, 10 + 20 * i, 400, 24 + 20 * i],
                    "reliable": True} for i, t in enumerate(_PAGE2)]},
    ]
    text = "\n".join([""] + [ln["text"] for pg in pages
                             for ln in pg["lines"]]).strip()
    parsed = {"engine": "pymupdf", "page_count": 2, "pages": pages, "lines": [],
              "text": text, "chars": len(text), "has_text_layer": True,
              "parameter_extraction_safe": True, "page_numbers_available": True,
              "unknown_glyph_lines": 0}
    with _Root(folder):
        DM.save_document("part999.pdf", DOC, parsed, 2048)
    return DOC


def _make_params():
    def item(name, value, unit, kind, quote, structured, symbol, row, mtm):
        return {"name": name, "value": value, "unit": unit, "value_kind": kind,
                "category": "electrical_characteristic",
                "conditions": quote, "conditions_structured": structured,
                "symbol": symbol, "source_page": 2, "source_quote": quote,
                "table": {"row_id": row, "column_id": f"c_{mtm}",
                          "header_path": ["ELECTRICAL CHARACTERISTICS", symbol],
                          "model_column": "PART999", "min_typ_max": mtm,
                          "footnote": "TA = 25 C"}}
    return {"document_id": DOC, "part_number": "PART999", "device_kind": "jfet",
            "parameters": [
                item("VGSoff_min", -2.0, "Vdc", "minimum",
                     "Gate Source Cutoff Voltage (VDS = 10 Vdc, ID = 1.0 nAdc) "
                     "VGS(off) −2.0 −6.5 Vdc",
                     {"vds_v": 10.0, "id_a": 1e-9},
                     "VGS(off)", "r_vgsoff", "min"),
                item("IDSS_max", 60.0, "mAdc", "maximum",
                     "Zero Gate Voltage Drain Current (VDS = 10 Vdc, VGS = 0) "
                     "IDSS 24 60 mAdc",
                     {"vds_v": 10.0}, "IDSS", "r_idss", "max"),
            ]}


# ---------------------------------------------------------------------------
# 1. 调用方自填来源（旧实现的最大漏洞）
# ---------------------------------------------------------------------------

def test_caller_supplied_simulation_is_rejected():
    """调用方在参数里塞 simulation 来源 → 直接报错，不得当证据用。"""
    ctx = _ctx("a")
    with _Root(ctx["folder"]):
        raises(M.ModelError, lambda: _validate(ctx, simulation={
            "library": "AI_lib", "cell": "J310_SIMPROV", "view": "schematic",
            "netlist_sha": "invented", "workspace": "Z:/different",
            "dataset_path": ctx["dataset"]}),
            "自填 simulation 来源必须被拒绝")
        rec = M.get_model_record(ctx["model_id"])
        ne((rec.get("validation") or {}).get("provenance", {}).get("state"),
           "verified", "拒绝的自填来源不得留下 verified 痕迹")


def test_forged_placeholder_fingerprint_never_verified():
    """伪来源（invented / netlist_only / 非 hex）不得 verified。"""
    ctx = _ctx("b")
    with _Root(ctx["folder"]):
        for forged in ("invented", "netlist_only", "NOTHEX!!", "-", "n/a"):
            sim_id = sim_records.begin({
                "workspace": "Z:/different", "library": "AI_lib",
                "cell": "J310_SIMPROV", "view": "schematic",
                "build_fingerprint": ctx["record"]["build"]["fingerprint"],
                "netlist_sha": forged})
            result = _validate(ctx, simulation_id=sim_id)
            eq(result["provenance"]["state"], "unverified", forged)
            ne(result["verdict"], "validated", f"伪指纹 {forged} 竟判 validated")


def test_missing_simulation_id_is_unverified():
    """没有可信执行记录 → unverified（不得仅凭 dataset_path 关联模型）。"""
    ctx = _ctx("c")
    with _Root(ctx["folder"]):
        result = _validate(ctx, simulation_id=None)
        eq(result["provenance"]["state"], "unverified")
        ok(result["provenance"]["problems"])
        ne(result["verdict"], "validated")


def test_unknown_simulation_id_is_unverified():
    """编一个不存在的 simulation_id → unverified，不放行。"""
    ctx = _ctx("d")
    with _Root(ctx["folder"]):
        result = _validate(ctx, simulation_id="0" * 32)
        eq(result["provenance"]["state"], "unverified")


def test_mismatched_workspace_library_cell_are_unverified():
    """工作区/库/cell 与建图记录不一致 → 逐项如实列出问题。"""
    ctx = _ctx("e")
    with _Root(ctx["folder"]):
        for bad in ({"workspace": "Z:/different"}, {"cell": "OTHER_CELL"},
                    {"library": "OTHER_LIB"}):
            sim_id = sim_records.begin({
                "workspace": "E:/ADS/Work_AI/simprov_wrk", "library": "AI_lib",
                "cell": "J310_SIMPROV", "view": "schematic",
                "build_fingerprint": ctx["record"]["build"]["fingerprint"],
                "netlist_sha": "deadbeefcafe0001"})
            sim_records.complete(sim_id, dataset_path=ctx["dataset"],
                                 netlist_sha="deadbeefcafe0001",
                                 process_exit=0, dataset_ok=True,
                                 controller_trace_ok=True)
            rec = sim_records.get(sim_id)
            rec.update(bad)
            sim_records._write(rec)
            result = _validate(ctx, simulation_id=sim_id)
            eq(result["provenance"]["state"], "unverified", str(bad))


def test_dataset_replaced_between_record_and_validate():
    """数据集文件在记录之后被替换 → SHA 不一致，unverified。"""
    ctx = _ctx("f")
    with _Root(ctx["folder"]):
        with open(ctx["dataset"], "wb") as stream:
            stream.write(b"!replaced by someone else\n")
        result = _validate(ctx)
        eq(result["provenance"]["state"], "unverified")
        ok(any("SHA" in p or "替换" in p
               for p in result["provenance"]["problems"]),
           f"未指出数据集被替换：{result['provenance']['problems']}")


def test_abnormal_process_exit_is_diagnostic_only():
    """0xC0000409（算完才崩）：产物可诊断，但不得计入端到端通过。"""
    ctx = _ctx("a7")
    with _Root(ctx["folder"]):
        sim_records.complete(ctx["simulation_id"], dataset_path=ctx["dataset"],
                             netlist_sha="deadbeefcafe0001",
                             process_exit="3221226505", dataset_ok=True,
                             controller_trace_ok=False,
                             workspace="E:/ADS/Work_AI/simprov_wrk")
        rec = sim_records.get(ctx["simulation_id"])
        eq(rec["integrity"]["process_exit"], "abnormal_after_dataset")
        eq(rec["diagnostic_only"], True)
        result = _validate(ctx)
        eq(result["provenance"]["state"], "unverified")
        ne(result["verdict"], "validated")


# ---------------------------------------------------------------------------
# 2. 正例：完整可信记录仍必须能 verified（防矫枉过正）
# ---------------------------------------------------------------------------

def test_trusted_record_still_verifies():
    """完整可信记录 → provenance verified（门禁不能收成全拒绝）。"""
    ctx = _ctx("b1")
    with _Root(ctx["folder"]):
        result = _validate(ctx)
        eq(result["provenance"]["state"], "verified",
           f"可信来源被误拒：{result['provenance']['problems']}")
        eq(result["simulation_id"], ctx["simulation_id"],
           "simulation_id 必须来自执行记录，不能是验证时现造的")
        sim_ev = result["provenance"]["evidence"]["simulation"]
        eq(sim_ev["source"], "backend_execution_record")
        eq(sim_ev["diagnostic_only"], False)


def test_validate_never_invents_simulation_id():
    """验证时不得现造 simulation_id（uuid 不等于来源）。"""
    ctx = _ctx("b2")
    with _Root(ctx["folder"]):
        result = _validate(ctx)
        ne(result["simulation_id"], None)
        eq(result["simulation_id"], ctx["simulation_id"])


def test_schematic_mode_distinguished_from_netlist_mode():
    """默认（schematic 模式）与 netlist 模式的结论必须可区分。"""
    ctx = _ctx("c2")
    with _Root(ctx["folder"]):
        result = _validate(ctx)
        eq(result["validation_mode"], "schematic")


# ---------------------------------------------------------------------------
# 3. 独立网表正演 ≠ 端到端验收
# ---------------------------------------------------------------------------

def test_netlist_equivalent_never_validated():
    """独立网表正演只能记 numeric_spec_validated，且 provenance 不 verified。"""
    ctx = _ctx("c1")
    with _Root(ctx["folder"]):
        result = _validate(ctx, validation_mode="netlist_equivalent")
        eq(result["validation_mode"], "netlist_equivalent")
        eq(result["provenance"]["state"], "unverified")
        ne(result["verdict"], "validated",
           "独立网表正演不得升格为 validated")
        eq(result["verdict"], "numeric_spec_validated")
        saved = M.get_model_record(ctx["model_id"])
        eq(saved["stage"], "numeric_spec_validated")
        ok(any("几何门禁" in r or "连通性" in r
               for r in result["verdict_reasons"]),
           f"未说明辅助证据边界：{result['verdict_reasons']}")


# ---------------------------------------------------------------------------
# 4. prepare_device_model 的参数集归属
# ---------------------------------------------------------------------------

def test_parameter_set_from_other_document_rejected():
    """别的手册的参数集（状态够新也不行）→ 拒绝。"""
    import datasheet_models as DM
    folder = tempfile.mkdtemp(prefix="spd_")
    with _Root(folder):
        _make_doc(folder)
        params = _make_params()
        pset_id = DM.save_parameters(params)["parameter_set_id"]
        DM.review_parameter_set({"parameter_set_id": pset_id})
        DM.verify_parameter_semantics({"parameter_set_id": pset_id})
        saved = DM._read("parameters", pset_id)
        eq(saved["status"], "semantic_verified", "前置状态应已是语义级")
        # 换成另一份资料（document_id 不一致）→ 必须拒绝
        saved["document_id"] = "e" * 64
        DM._write("parameters", pset_id, saved)
        err = raises(M.ModelError, lambda: M.prepare_device_model({
            "document_id": DOC, "part_number": "PART999",
            "device_kind": "jfet", "model_kind": "jfet_small_signal",
            "parameter_set_id": pset_id}), "跨手册参数集必须被拒绝")
        ok("另一份资料" in str(err) or "document" in str(err).lower(),
           f"应指出资料不符：{err}")


def test_parameter_set_other_part_number_rejected():
    """别的型号的参数集，且无共用声明证据 → 拒绝。"""
    import datasheet_models as DM
    folder = tempfile.mkdtemp(prefix="spe_")
    with _Root(folder):
        _make_doc(folder)
        pset_id = DM.save_parameters(_make_params())["parameter_set_id"]
        DM.review_parameter_set({"parameter_set_id": pset_id})
        DM.verify_parameter_semantics({"parameter_set_id": pset_id})
        saved = DM._read("parameters", pset_id)
        saved["part_number"] = "SOME_OTHER_PART"
        saved.get("shared_range_quote", None)
        DM._write("parameters", pset_id, saved)
        err = raises(M.ModelError, lambda: M.prepare_device_model({
            "document_id": DOC, "part_number": "PART999",
            "device_kind": "jfet", "model_kind": "jfet_small_signal",
            "parameter_set_id": pset_id}), "跨型号参数集必须被拒绝")
        ok("型号" in str(err), f"应指出型号不符：{err}")


# ---------------------------------------------------------------------------
# 5. 会话注入路径
# ---------------------------------------------------------------------------

def test_with_simulation_evidence_drops_caller_source():
    """_with_simulation_evidence 必须无条件丢弃调用方自填的 simulation。"""
    import agent as AG
    forged = {"library": "AI_lib", "cell": "X", "netlist_sha": "invented"}
    out = AG._with_simulation_evidence({"simulation": dict(forged)}, None)
    ok("simulation" not in out, "调用方自填的 simulation 竟被保留")
    ok(out.get("simulation_id") is None,
       "无可信执行记录时不得凭空造 simulation_id")


def test_with_simulation_evidence_injects_recorded_id():
    """会话缓存里有真实 simulation_id 时，注入它而不是让模型自填。"""
    import agent as AG
    sim_id = "a" * 32
    last = {"dataset_path": "E:/x/sim.ds", "simulation_id": sim_id}
    out = AG._with_simulation_evidence(
        {"dataset_path": "E:/x/sim.ds", "simulation": {"netlist_sha": "invented"}},
        last)
    ok("simulation" not in out)
    eq(out["simulation_id"], sim_id)


def test_with_simulation_evidence_ignores_other_dataset():
    """缓存数据集与本次不一致 → 不注入（防拿旧数据冒充）。"""
    import agent as AG
    last = {"dataset_path": "E:/x/other.ds", "simulation_id": "b" * 32}
    out = AG._with_simulation_evidence({"dataset_path": "E:/x/sim.ds"}, last)
    ok("simulation_id" not in out)


def _usage_sim(ctx, *, library, cell, dataset, netlist_sha="deadbeefcafe0002"):
    """为"封装元件 + 使用电路"造一条可信仿真执行记录。"""
    sim_id = sim_records.begin({
        "workspace": ctx["workspace"], "library": library, "cell": cell,
        "view": "schematic", "build_fingerprint": None,
        "netlist_sha": netlist_sha})
    sim_records.complete(sim_id, dataset_path=dataset, netlist_sha=netlist_sha,
                         process_exit=0, dataset_ok=True,
                         controller_trace_ok=True, workspace=ctx["workspace"])
    return sim_id


def _fake_ads(calls):
    def call_tool(name, args):
        calls.append((name, dict(args)))
        if name == "get_workspace_info":
            return {"workspace_open": True, "writable_libraries": ["AI_lib"],
                    "path": "E:/ws"}
        if name == "build_schematic":
            return {"ok": True}
        if name == "create_component_symbol":
            return {"ok": True, "symbol_view": "symbol", "symbol_created": True,
                    "pins": [{"num": 1, "terminal": "s"},
                             {"num": 2, "terminal": "d"},
                             {"num": 3, "terminal": "g"}]}
        raise AssertionError(f"unexpected tool {name}")
    return call_tool


def _publish_and_place(ctx, tag):
    """把源记录发布成修订，导入并放置到新工作区，返回复验所需材料。"""
    import device_library as DL
    import datasheet_curves
    import doc_assets
    root = DL.devices_root({}, tempfile.mkdtemp(prefix="rv_lib_" + tag + "_"))
    record = M.get_model_record(ctx["model_id"])
    # 合成模型没有真实上传字节；发布要求原文件可审计。补一份最小 PDF 资产，
    # 并把模型/曲线记录的 document_id 指向它的真实 SHA（否则资产按 SHA 找不到）。
    real_doc = doc_assets.store(b"%PDF-1.4 rv-" + tag.encode() + b" %")["sha256"]
    record["document_id"] = real_doc
    M._save(record)
    for pair in (record.get("curve_ids") or {}).values():
        for curve_id in (pair or {}).values():
            payload = datasheet_curves.get_curve(curve_id)
            payload["document_id"] = real_doc
            datasheet_curves._write(curve_id, payload)
    record = M.get_model_record(ctx["model_id"])
    build = record.get("build") or {}
    if build.get("pin_mapping"):
        # 建图记录里的参数来源仍指向旧文档，改为可审计的新文档
        record["build"]["parameters_source"] = {
            "kind": "digitized_curves", "document_id": real_doc,
            "note": "来自曲线记录的数字化数据，不是厂家模型"}
        M._save(record)
        record = M.get_model_record(ctx["model_id"])
    pkg = M.build_component_package(record, cell_name="J310RV" + tag)
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-RV" + tag,
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    workspace = tempfile.mkdtemp(prefix="rv_ws_" + tag + "_")
    ctx["workspace"] = workspace
    calls = []
    fake = _fake_ads(calls)
    imp = DL.import_revision(root, workspace, manifest, call_tool=fake)
    placed = DL.place_component(root, workspace, manifest, imp, call_tool=fake)
    return {"root": root, "manifest": manifest, "pub": pub,
            "workspace": workspace, "import": imp, "placed": placed}


def test_revision_revalidation_binds_real_usage_circuit():
    """封装元件在新工作区的复验：绑定真实使用电路，且不依赖源数据根。"""
    ctx = _ctx("a1b2")
    with _Root(ctx["folder"]):
        env = _publish_and_place(ctx, "A")
        sim_id = _usage_sim(ctx, library=env["import"]["library"],
                            cell=env["placed"]["cell"], dataset=ctx["dataset"])
        out = M.validate_device_revision({
            "revision_id": env["pub"]["revision_id"],
            "library_root": env["root"],
            "workspace": env["workspace"],
            "simulation_id": sim_id,
            "dataset_path": ctx["dataset"],
            "placement": {"library": env["import"]["library"],
                          "cell": env["placed"]["cell"],
                          "band_hz": [1e8, 1e9]},
            "traces": ctx["traces"],
            "thresholds": {"max_complex_rmse_S": 1e-2,
                           "max_complex_rmse_Y": 1e-4}})
        binding = out["binding"]
        eq(binding["scope"], "packaged_usage_circuit")
        eq(binding["revision_id"], env["pub"]["revision_id"])
        eq(binding["cell"], env["placed"]["cell"])
        eq(binding["problems"], [], "绑定应成立")
        eq(binding["archive_version"], 2)
        ok(out["provenance"]["state"] == "verified", "数据集来源应绑定")
        ne(out["verdict"], "validation_failed", "绑定成立时不应判失败")


def test_revision_revalidation_rejects_wrong_cell_and_band():
    """换个 cell、换频段、改参数都不能复用同一次仿真结论。"""
    ctx = _ctx("c3d4")
    with _Root(ctx["folder"]):
        env = _publish_and_place(ctx, "B")
        sim_id = _usage_sim(ctx, library=env["import"]["library"],
                            cell=env["placed"]["cell"], dataset=ctx["dataset"])
        common = {"revision_id": env["pub"]["revision_id"],
                  "library_root": env["root"], "workspace": env["workspace"],
                  "simulation_id": sim_id, "dataset_path": ctx["dataset"],
                  "traces": ctx["traces"],
                  "thresholds": {"max_complex_rmse_S": 1e-2,
                                 "max_complex_rmse_Y": 1e-4}}
        # 错 cell：声明一个没放置过的使用电路
        err_cell = raises(M.ModelError, lambda: M.validate_device_revision(
            dict(common, placement={"library": env["import"]["library"],
                                    "cell": "SOME_OTHER_USE",
                                    "band_hz": [1e8, 1e9]})))
        contains(str(err_cell), "使用电路", "cell 不一致必须拒绝")

        # 频段与放置登记不符
        err_band = raises(M.ModelError, lambda: M.validate_device_revision(
            dict(common, placement={"library": env["import"]["library"],
                                    "cell": env["placed"]["cell"],
                                    "band_hz": [2e8, 9e8]})))
        contains(str(err_band), "频段", "换频段不能被当成同一复验")

        # 自填 simulation 必须拒绝
        err = raises(M.ModelError, lambda: M.validate_device_revision(dict(
            common, simulation={"netlist_sha": "invented"})))
        contains(str(err), "simulation")

        # 未导入的修订不能复验
        err2 = raises(M.ModelError, lambda: M.validate_device_revision(dict(
            common, revision_id="rev_" + "0" * 16)))
        contains(str(err2), "revision")


def test_revision_revalidation_aware_of_identity_layers():
    """库 ID 与建模记录 ID 混用时给出可执行诊断。"""
    err = raises(M.ModelError, lambda: M.validate_device_revision({
        "revision_id": "rev_" + "1" * 16,
        "library_root": tempfile.mkdtemp(prefix="rv_root_"),
        "workspace": tempfile.mkdtemp(prefix="rv_ws_"),
        "simulation_id": "a" * 32, "dataset_path": "x.ds",
        "model_id": "mdl_" + "2" * 16}))
    ok("修订" in str(err) or "model_record_id" in str(err),
       f"应指出是哪一层身份问题：{err}")


if __name__ == "__main__":
    if not HAVE_NUMPY:
        print("本回归需要 numpy+scipy，请用装了数值库的解释器运行："
              "C:/Users/27244/.workbuddy/binaries/python/envs/default/Scripts/python.exe")
        raise SystemExit(2)
    raise SystemExit(run(dict(globals()), "仿真来源绑定门禁（A 阶段）"))
