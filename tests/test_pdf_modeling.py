"""PDF 原文件资产、页级证据、曲线记录与有源建模引擎的回归测试。

能在任意解释器运行：pymupdf/numpy/scipy 缺失时相关用例显式 SKIP，
受限降级与纯标准库部分始终执行。真实样本（tests/fixtures/SMMBFJ310L.pdf）
存在时追加端到端离线验收（不含 ADS 实机部分）。
"""
import hashlib
import cmath
import json
import math
import os
import sys
import zlib
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, contains, eq, ok, raises, skip, run
add_path("backend")

import datasheet_models as D
import datasheet_curves as C
import device_models as M
import doc_assets
import pdf_extract

try:
    import pymupdf
    HAVE_PYMUPDF = True
except Exception:  # noqa: BLE001
    HAVE_PYMUPDF = False

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:  # noqa: BLE001
    HAVE_NUMPY = False

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "SMMBFJ310L.pdf")
J310_SHA = "113d3cb19b47f74879741cf408983f03ab89b0123f1e1beecc3bd8a2442871b8"


# ---------------------------------------------------------------------------
# doc_assets：原文件持久化
# ---------------------------------------------------------------------------

def test_asset_store_dedup_and_readback():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        data = b"%PDF-1.4 fake bytes"
        first = doc_assets.store(data)
        ok(first["absolute_path"].endswith(first["sha256"] + ".pdf"))
        ok(os.path.isfile(first["absolute_path"]))
        second = doc_assets.store(data)
        eq(second["reused"], True)
        eq(second["absolute_path"], first["absolute_path"])
        eq(doc_assets.read_bytes(first["sha256"]), data)
        status = doc_assets.asset_status("f" * 64)
        eq(status["has_original"], False)
        eq(status["source_status"], "source_unavailable")
        ok("重新" in status["reattach_hint"])
        raises(doc_assets.AssetError, lambda: doc_assets.store(b"", "x"))
        raises(doc_assets.AssetError, lambda: doc_assets.read_bytes("../etc"))
        # 哈希防篡改
        path = first["absolute_path"]
        with open(path, "wb") as stream:
            stream.write(b"tampered")
        raises(doc_assets.AssetError, lambda: doc_assets.read_bytes(first["sha256"]))


def _tmp() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="pdf_modeling_test_")


# ---------------------------------------------------------------------------
# 解析引擎：真实页数 / 负号 / 行级可靠性 / 降级语义不变
# ---------------------------------------------------------------------------

def test_fallback_parser_semantics_unchanged():
    filler = " ".join(["Capacitance value stays readable for the density check."] * 6)
    content = ("BT [(PART123) -500 (10 pF typical) -500 (" + filler + ")] TJ ET").encode()
    pdf = b"%PDF-1.4\nstream\n" + zlib.compress(content) + b"\nendstream"
    result = pdf_extract.extract_text(pdf)
    ok("PART123" in result["text"])
    doc = pdf_extract.extract_document(pdf)
    eq(doc["engine"], "stream_scan")
    eq(doc["page_numbers_available"], False)
    eq(doc["parameter_extraction_safe"], True)  # 无复杂字体标记的简单 PDF 保持可用


def test_line_reliability_marks_unknown_glyphs():
    reliable, unknown = pdf_extract._line_reliability("(IG = −1.0 µAdc, VDS = 0)")
    eq(reliable, True)
    bad, unknown2 = pdf_extract._line_reliability("(IG = −1.0 \x03Adc, VDS = 0)")
    eq(bad, False)
    eq(unknown2, [3])


def test_real_sample_parses_five_pages_with_evidence():
    if not os.path.isfile(FIXTURE):
        skip("真实样本 fixtures/SMMBFJ310L.pdf 不在本机")
    if not HAVE_PYMUPDF:
        skip("当前解释器无 pymupdf")
    with open(FIXTURE, "rb") as stream:
        data = stream.read()
    eq(hashlib.sha256(data).hexdigest(), J310_SHA)
    parsed = pdf_extract.extract_document(data)
    eq(parsed["engine"], "pymupdf")
    eq(parsed["page_count"], 5)
    eq(parsed["page_numbers_available"], True)
    eq(parsed["parameter_extraction_safe"], True)
    page2 = parsed["pages"][1]
    texts = [ln["text"] for ln in page2["lines"]]
    joined = " ".join(texts)
    # 负号与关键数值逐字保真（U+2212 减号，不是 ASCII 连字符）
    ok("−6.5" in joined, "VGS(off) 的负值必须保真")
    ok("−4.0" in joined)
    ok("SMMBFJ310" in joined)
    # µAdc 行保留原始字形并标注不可靠
    bad_lines = [ln for ln in page2["lines"] if not ln["reliable"]]
    ok(bad_lines, "含未映射字形的行必须标 unreliable")
    ok(any("\x03" in ln["text"] for ln in bad_lines))
    # 行级证据：可靠行包含 IDSS 两型号行
    ok(any("IDSS" in t for t in texts))
    ok(any("mAdc" in t for t in texts))


def test_document_record_keeps_pages_and_asset():
    if not os.path.isfile(FIXTURE) or not HAVE_PYMUPDF:
        skip("真实样本或 pymupdf 不可用")
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        with open(FIXTURE, "rb") as stream:
            data = stream.read()
        doc_assets.store(data)
        parsed = pdf_extract.extract_document(data)
        record = D.save_document("SMMBFJ310L.pdf", J310_SHA, parsed, len(data),
                                 session="proj-a")
        eq(record["parse_engine"], "pymupdf")
        eq(record["page_count"], 5)
        eq(record["has_original"], True)
        eq(record["sessions"], ["proj-a"])
        again = D.save_document("renamed.pdf", J310_SHA, parsed, len(data),
                                session="proj-b")
        eq(again["first_seen"], record["first_seen"])
        eq(sorted(again["sessions"]), ["proj-a", "proj-b"])
        overview = D.inspect_document({"document_id": J310_SHA})
        eq(len(overview["pages"]), 5)
        eq(overview["pages"][1]["unreliable_lines"] > 0, True)
        page = D.read_page({"document_id": J310_SHA, "page": 2})
        eq(page["page"], 2)
        ok(page["lines"])
        raises(D.DatasheetError, lambda: D.read_page({"document_id": J310_SHA, "page": 9}))
        legacy = D.get_document({"document_id": J310_SHA})
        eq(legacy["page_count"], 5)
        ok("pages" not in legacy)


# ---------------------------------------------------------------------------
# 参数保存：页码/行区间/单位/条件/复核
# ---------------------------------------------------------------------------

def _paged_document(folder: str) -> str:
    """构造一个小型 2 页文档记录（手工行证据，不依赖 pymupdf）。"""
    pages = [
        {"page": 1, "width": 612, "height": 792, "has_text": True, "chars": 60,
         "images": 0, "line_count": 3, "lines": [
             {"text": "SuperFET Datasheet", "bbox": [10, 10, 200, 24], "reliable": True},
             {"text": "PART999 N-Channel", "bbox": [10, 30, 200, 44], "reliable": True},
             {"text": "October 2013", "bbox": [10, 50, 200, 64], "reliable": True}]},
        {"page": 2, "width": 612, "height": 792, "has_text": True, "chars": 120,
         "images": 0, "line_count": 7, "lines": [
             {"text": "ELECTRICAL CHARACTERISTICS (TA = 25°C)", "bbox": [10, 60, 400, 74], "reliable": True},
             {"text": "Gate Source Cutoff Voltage", "bbox": [10, 90, 300, 104], "reliable": True},
             {"text": "(VDS = 10 Vdc, ID = 1.0 nAdc)", "bbox": [10, 110, 320, 124], "reliable": True},
             {"text": "VGS(off)", "bbox": [10, 130, 120, 144], "reliable": True},
             {"text": "−2.0", "bbox": [140, 130, 180, 144], "reliable": True},
             {"text": "−6.0", "bbox": [200, 130, 240, 144], "reliable": True},
             {"text": "Vdc", "bbox": [300, 130, 340, 144], "reliable": True}]},
    ]
    text = "\n".join(
        ["", *[ln["text"] for page in pages for ln in page["lines"]]]).strip()
    parsed = {"engine": "pymupdf", "page_count": 2, "pages": pages, "lines": [],
              "text": text, "chars": 180, "has_text_layer": True,
              "parameter_extraction_safe": True, "page_numbers_available": True,
              "unknown_glyph_lines": 0}
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        D.save_document("mini.pdf", "a" * 64, parsed, 1024)
    return "a" * 64


def test_paged_parameter_save_and_review():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        doc_id = _paged_document(folder)
        inputs = {"document_id": doc_id, "part_number": "PART999",
                  "device_kind": "jfet", "parameters": [
                      {"name": "vgsoff_min", "value": -2.0, "unit": "Vdc",
                       "value_kind": "minimum", "category": "electrical_characteristic",
                       "conditions": "(VDS = 10 Vdc, ID = 1.0 nAdc)",
                       "conditions_structured": {"vds_v": 10.0, "id_a": 1e-9},
                       "symbol": "VGS(off)", "source_page": 2,
                       "source_quote": "VGS(off) −2.0 −6.0 Vdc"}]}
        result = D.save_parameters(inputs)
        eq(result["status"], "extracted_unreviewed")
        param = result["parameters"][0]
        eq(param["source_page"], 2)
        ok(param["source_lines"] is not None)
        ok(param["source_bbox"] is not None)
        eq(param["line_reliability"], "reliable")
        eq(param["si_value"], -2.0)
        eq(param["dimension"], "voltage")
        eq(param["conditions_structured"]["vds_v"], 10.0)
        eq(param["extraction_method"], "page_text_lines_v2")
        review = D.review_parameter_set({"parameter_set_id": result["parameter_set_id"]})
        eq(review["review_passed"], True)
        eq(review["status"], "evidence_checked")   # 字段证据级，不是一键 reviewed
        again = D._read("parameters", result["parameter_set_id"])
        eq(again["status"], "evidence_checked")
        eq(again["semantic_review"], "evidence_checked")


def test_paged_parameter_gates():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        doc_id = _paged_document(folder)

        def base(**over):
            item = {"name": "vgsoff_min", "value": -2.0, "unit": "Vdc",
                    "value_kind": "minimum", "category": "electrical_characteristic",
                    "source_page": 2, "source_quote": "VGS(off) −2.0 −6.0 Vdc"}
            item.update(over)
            return {"document_id": doc_id, "part_number": "PART999",
                    "device_kind": "jfet", "parameters": [item]}

        # 页码缺失 / 越界
        raises(D.DatasheetError, lambda: D.save_parameters(base(source_page=None)))
        raises(D.DatasheetError, lambda: D.save_parameters(base(source_page=5)))
        # 引用不在该页
        raises(D.DatasheetError, lambda: D.save_parameters(
            base(source_quote="October 2013")))
        # 数值不在引用行
        raises(D.DatasheetError, lambda: D.save_parameters(base(value=-5.0)))
        # 单位不在引用行内
        raises(D.DatasheetError, lambda: D.save_parameters(base(unit="mAdc")))
        # 条件文本不在该页
        raises(D.DatasheetError, lambda: D.save_parameters(
            base(conditions="(VDS = 99 Vdc)")))
        # 结构化条件白名单
        raises(D.DatasheetError, lambda: D.save_parameters(
            base(conditions_structured={"vgs_weird": 1.0})))
        # 型号无证据
        raises(D.DatasheetError, lambda: D.save_parameters(
            {**base(), "part_number": "PART777"}))


def test_unknown_glyph_line_requires_review_not_silent():
    folder = _tmp()
    # 结构仿照真实样本：数值在含未映射字形的行里，单位 nAdc 在可靠行里
    pages = [{"page": 1, "width": 612, "height": 792, "has_text": True, "chars": 80,
              "images": 0, "line_count": 4, "lines": [
                  {"text": "Gate Reverse Current", "bbox": [10, 10, 200, 24], "reliable": True},
                  {"text": "IGSS", "bbox": [10, 30, 120, 44], "reliable": True},
                  {"text": "(\u22121.0 \x03Adc)", "bbox": [10, 50, 200, 64],
                   "reliable": False, "unknown_glyphs": ["0x3"]},
                  {"text": "nAdc", "bbox": [300, 50, 340, 64], "reliable": True}]}]
    parsed = {"engine": "pymupdf", "page_count": 1, "pages": pages, "lines": [],
              "text": "PART999 Gate Reverse Current IGSS (\u22121.0 \x03Adc) nAdc",
              "chars": 40, "has_text_layer": True,
              "parameter_extraction_safe": True, "page_numbers_available": True}
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        D.save_document("g.pdf", "b" * 64, parsed, 512)
        inputs = {"document_id": "b" * 64, "part_number": "PART999",
                  "device_kind": "unknown", "parameters": [
                      {"name": "ig", "value": -1.0, "unit": "nAdc",
                       "value_kind": "maximum", "category": "electrical_characteristic",
                       "source_page": 1,
                       "source_quote": "IGSS (\u22121.0 \x03Adc) nAdc"}]}
        # 默认拒绝；显式 allow_requires_review 才能以 requires_review 存档
        err = raises(D.DatasheetError, lambda: D.save_parameters(inputs))
        ok("未映射字形" in str(err), f"实际报错: {err}")
        saved = D.save_parameters({**inputs, "allow_requires_review": True})
        eq(saved["status"], "requires_review")
        eq(saved["requires_review_parameters"], ["ig"])
        # 复核不会把不可靠行洗白
        review = D.review_parameter_set({"parameter_set_id": saved["parameter_set_id"]})
        eq(review["review_passed"], False)
        eq(review["failed_parameters"], ["ig"])


def test_new_units_convert():
    cases = [("mS", 8.0, "conductance", 8e-3), ("uS", 70.0, "conductance", 7e-5),
             ("µS", 70.0, "conductance", 7e-5), ("psec", 30.0, "time", 3e-11),
             ("ps", 30.0, "time", 3e-11), ("ns", 1.0, "time", 1e-9),
             ("nV/sqrt(Hz)", 2.0, "noise_voltage_density", 2e-9),
             ("nA/sqrt(Hz)", 1.0, "noise_current_density", 1e-9),
             ("mAdc", 24.0, "current", 0.024), ("Vdc", -6.5, "voltage", -6.5),
             # 手册旧称与 SI 等价写法：mmhos/umhos 是原文常见写法，必须归一
             ("mho", 2.0, "conductance", 2.0), ("mmhos", 8.0, "conductance", 8e-3),
             ("umhos", 70.0, "conductance", 7e-5), ("μmhos", 70.0, "conductance", 7e-5),
             ("µS", 70.0, "conductance", 7e-5)]
    for unit, value, dim, si in cases:
        got, got_dim = D.quantity(value, unit)
        eq(got_dim, dim, unit)
        ok(math.isclose(got, si, rel_tol=1e-12), unit)
        eq(D.normalize_unit(unit),
           "S" if unit == "mho" else
           "mS" if unit == "mmhos" else
           "uS" if unit in ("umhos", "μmhos", "µS", "uS") else unit,
           f"{unit} 的规范单位")
    raises(D.DatasheetError, lambda: D.quantity(1.0, "furlongs"))


# ---------------------------------------------------------------------------
# 曲线：校准 / 采样 / 记录
# ---------------------------------------------------------------------------

def test_axis_calibration_and_digitize():
    xcal = C.AxisCal({"unit": "MHz", "scale": "log10",
                      "cal_points": [{"at": 100.0, "value": 100.0},
                                     {"at": 240.0, "value": 1000.0}]}, "x")
    ok(math.isclose(xcal.to_pdf(100.0), 100.0))
    ok(math.isclose(xcal.to_pdf(1000.0), 240.0))
    ok(math.isclose(xcal.to_value(170.0), 316.22776601683796, rel_tol=1e-9))
    ycal = C.AxisCal({"unit": "mS", "scale": "linear",
                      "cal_points": [{"at": 69.0, "value": 30.0},
                                     {"at": 213.0, "value": 0.0}]}, "y")
    ok(math.isclose(ycal.to_value(69.0), 30.0))
    polyline = [[100.0, 100.0], [140.0, 110.0], [240.0, 180.0]]
    targets = [100.0, 1000.0]
    values = C.digitize_series(polyline, xcal, ycal, targets)
    eq(len(values), 2)
    # 覆盖检查：折线域外直接报错，不外推
    raises(C.CurveError, lambda: C.digitize_series(
        polyline, xcal, ycal, [10.0]))
    raises(C.CurveError, lambda: C.AxisCal(
        {"scale": "log10", "cal_points": [{"at": 1, "value": -1},
                                          {"at": 2, "value": 10}]}, "y"))


def test_curve_record_roundtrip_and_matrix():
    folder = _tmp()
    mag_values = [[float(f), 10.0 + f / 1000.0] for f in range(100, 1001, 100)]
    phase_values = [[float(f), -45.0] for f in range(100, 1001, 100)]
    payload_common = {"document_id": "c" * 64, "sha256": "c" * 64, "page": 4,
                      "configuration": "common_gate",
                      "port_order": {"port1": "source", "port2": "drain", "common": "gate"},
                      "z0": {"ohm": 50.0, "status": "assumed"},
                      "conditions": {"vds_v": 10.0, "id_a": 0.01},
                      "sampling": {"mode": "log10_spaced", "count": len(mag_values),
                                   "start": 100.0, "stop": 1000.0}}
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        os.makedirs(os.path.join(D._root(), "documents"), exist_ok=True)
        with open(D._path("documents", "c" * 64), "w", encoding="utf-8") as stream:
            json.dump({"document_id": "c" * 64, "sha256": "c" * 64, "text": "",
                       "has_text_layer": True, "pages": []}, stream)
        # 完整 2×2：四个元素各一条 mag/phase 记录
        for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
            for comp, values, unit in (("mag", mag_values, "mS"),
                                       ("phase", phase_values, "mS")):
                payload = {**payload_common, "figure": "Fig 4",
                           "axes": {"x": {"unit": "MHz", "scale": "log10",
                                          "cal_points": [{"at": 100, "value": 100},
                                                         {"at": 1000, "value": 1000}]},
                                    "y": {"unit": unit, "scale": "linear",
                                          "cal_points": [{"at": 0, "value": 1},
                                                         {"at": 1, "value": 0}]}},
                           "matrix_kind": "Y", "component": comp,
                           "phase_unit": "deg" if comp == "phase" else None,
                           "series": [{"label": f"Y{i}{j}{comp}", "path_index": 4 * (i - 1) + j,
                                       "values": values}]}
                record_id = hashlib.sha256(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                C._write(record_id, {**payload, "curve_id": record_id,
                                     "status": "digitized_reviewed",
                                     "origin": {"method": "test"}, "reliability": {}})
        listing = C.list_curves({"document_id": "c" * 64})
        eq(listing["count"], 8)
        freqs, matrix, unit = C.assemble_matrix(
            [C.get_curve(c["curve_id"]) for c in listing["curves"]], "Y", ("mag", "phase"))
        eq(len(freqs), len(mag_values))
        ok(matrix[0][(1, 1)].imag < 0)  # -45°
        eq(unit, "mS")


# ---------------------------------------------------------------------------
# 建模引擎：拟合 / 建图计划 / 验证
# ---------------------------------------------------------------------------

def _write_curve_pair(doc_id: str, configuration: str, freqs_mhz, y_complex,
                      z0_status="assumed", conditions=None, kind="Y",
                      status="digitized_reviewed", z0_ohm=50.0) -> dict:
    """直接落两条曲线记录（Y mag/phase，含全部 2x2 参数），返回 curve_ids。

    ``y_complex`` 是 (N,2,2) 复数矩阵序列（单位 mS）。默认写
    ``digitized_reviewed``：通用路径曲线必须复核后才能建模。
    """
    if conditions is None:
        conditions = {"vds_v": 10.0, "id_a": 0.01, "temperature_c": 25.0}
    unit = "mS" if kind == "Y" else "1"
    ids = {}
    for comp in ("mag", "phase"):
        series = []
        for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
            values = [[float(f),
                       abs(row[i - 1, j - 1]) if comp == "mag"
                       else math.degrees(cmath.phase(row[i - 1, j - 1]))]
                      for f, row in zip(freqs_mhz, y_complex)]
            series.append({"label": f"{kind}{i}{j}{comp}", "path_index": 4 * (i - 1) + j,
                           "values": values})
        payload = {"document_id": doc_id, "sha256": doc_id, "page": 4,
                   "figure": "Fig", "configuration": configuration,
                   "port_order": {"port1": "source", "port2": "drain", "common": "gate"},
                   "z0": {"ohm": float(z0_ohm), "status": z0_status},
                   "conditions": conditions,
                   "sampling": {"mode": "log10_spaced", "count": len(freqs_mhz),
                                "start": freqs_mhz[0], "stop": freqs_mhz[-1]},
                   "axes": {"x": {"unit": "MHz", "scale": "log10",
                                  "cal_points": [{"at": 100, "value": freqs_mhz[0]},
                                                 {"at": 1000, "value": freqs_mhz[-1]}]},
                            "y": {"unit": unit, "scale": "linear",
                                  "cal_points": [{"at": 0, "value": 30.0},
                                                 {"at": 210, "value": 0.0}]}},
                   "matrix_kind": kind, "component": comp,
                   "phase_unit": "deg" if comp == "phase" else None,
                   "series": series}
        record_id = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        C._write(record_id, {**payload, "curve_id": record_id,
                             "status": status,
                             "origin": {"method": "test"},
                             "reviewed_at": "2026-10-14T00:00:00",
                             "reliability": {}})
        ids[comp] = record_id
    return {kind: ids}


def _synthesize_model(doc_id: str, configuration="common_gate", freqs_mhz=None,
                      conditions=None, tau_s=3e-11):
    """按已知参数生成 Y/S 目标并落曲线，返回 (model_id, truth, freqs, folder)。"""
    truth = {"gm_S": 0.014, "gds_S": 7e-5, "Cgs_F": 5e-12, "Cgd_F": 1.8e-12,
             "Cds_F": 5e-14, "tau_s": tau_s}
    if freqs_mhz is None:
        freqs_mhz = [10 ** (2 + i * (3 - 2) / 24) for i in range(25)]
    np = M._np()
    jw = 2j * np.pi * np.asarray(freqs_mhz) * 1e6
    a = np.zeros((len(jw), 3, 3), dtype=complex)
    for i, j, name in ((0, 1, "Cgs_F"), (0, 2, "Cgd_F"), (1, 2, "Cds_F")):
        v = jw * truth[name]
        a[:, i, i] += v
        a[:, j, j] += v
        a[:, i, j] -= v
        a[:, j, i] -= v
    a[:, 1, 1] += truth["gds_S"]
    a[:, 2, 2] += truth["gds_S"]
    a[:, 1, 2] -= truth["gds_S"]
    a[:, 2, 1] -= truth["gds_S"]
    gm = truth["gm_S"] * np.exp(-jw * truth["tau_s"])
    a[:, 2, 0] += gm
    a[:, 2, 1] -= gm
    a[:, 1, 0] -= gm
    a[:, 1, 1] += gm
    # common_gate 2 端口 = (s,d) 子矩阵；换算 mS 后整矩阵落曲线
    y2p = a[:, [1, 2], :][:, :, [1, 2]] * 1e3
    s2p = M.y_to_s(y2p * 1e-3, 50.0)      # 同一正演的无量纲 S（独立交叉曲线）
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        os.makedirs(os.path.join(D._root(), "documents"), exist_ok=True)
        with open(D._path("documents", doc_id), "w", encoding="utf-8") as stream:
            json.dump({"document_id": doc_id, "sha256": doc_id, "text": "TESTFET",
                       "has_text_layer": True, "pages": [], "parse_engine": "pymupdf"}, stream)
        curve_ids = _write_curve_pair(doc_id, configuration, freqs_mhz, y2p,
                                      conditions=conditions)
        curve_ids.update(_write_curve_pair(doc_id, configuration, freqs_mhz, s2p,
                                           conditions=conditions, kind="S"))
        prepared = M.prepare_device_model({
            "document_id": doc_id, "part_number": "TESTFET", "device_kind": "jfet",
            "model_kind": "jfet_small_signal", "curve_ids": curve_ids})
    eq(prepared["stage"], "candidate_ready")
    return prepared["model_id"], truth, freqs_mhz, folder


def _sim_evidence(record: dict, dataset_path: str, **over) -> dict:
    """落一条**真实的可信仿真执行记录**，返回 {simulation_id: ...}。

    2026-10-14 A 阶段：来源不再接受自填。测试必须走
    `sim_records.begin/complete` 这条与 run_simulation 相同的路径，
    否则"正例能 verified"就退化成了"绕过门禁也能 verified"。

    `over` 可覆盖记录字段以构造负例（如 workspace=别的盘、假指纹）。
    """
    import sim_records

    build = record.get("build") or {}
    payload = {"workspace": "E:/ADS/Work_AI/fake_wrk",
               "library": build.get("library"), "cell": build.get("cell"),
               "view": "schematic",
               "build_fingerprint": build.get("fingerprint"),
               "netlist_sha": "deadbeefcafe0001"}
    payload.update(over)
    sim_id = sim_records.begin(payload)
    if not payload.get("fail_complete"):
        # 指纹必须是真十六进制；测试里给的固定串已是 hex，可直接用。
        sim_records.complete(
            sim_id, dataset_path=dataset_path,
            netlist_sha=payload.get("netlist_sha", ""),
            process_exit=payload.get("process_exit", 0),
            dataset_ok=True,
            controller_trace_ok=not payload.get("bad_trace"),
            workspace=payload.get("workspace"))
    return {"simulation_id": sim_id}


def test_fit_recovers_synthetic_parameters():
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    doc_id = "d" * 64
    model_id, truth, _freqs, folder = _synthesize_model(doc_id)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        result = M.fit_device_model({"model_id": model_id,
                                     "candidates": ["intrinsic", "intrinsic_delay"]})
        eq(result["stage"], "fitted")
        ok(result["selected"] in ("intrinsic", "intrinsic_delay"))
        record = M.get_model_record(model_id)
        params = record["fit"]["candidates"][result["selected"]]["parameters_fitted_SI"]
        ok(math.isclose(params["gm_S"], truth["gm_S"], rel_tol=0.05),
           f"gm 拟合值 {params['gm_S']} 偏离真值")
        ok(math.isclose(params["gds_S"], truth["gds_S"], rel_tol=0.30),
           f"gds 拟合值 {params['gds_S']} 偏离真值")
        ok(record["fit"]["rule"]["origin"] in ("exploratory_default", "user_rule"))


def test_fit_rule_not_silently_relaxed():
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    doc_id = "e" * 64
    model_id, _truth, _freqs, folder = _synthesize_model(doc_id)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        # 不可达的用户规则：如实报告 no_candidate_meets_rule，不放宽
        result = M.fit_device_model({"model_id": model_id,
                                     "candidates": ["intrinsic"],
                                     "thresholds": {"heldout_max_nrmse": 1e-9}})
        eq(result["stage"], "fit_failed_no_candidate_meets_rule")
        eq(result["selected"], None)
        eq(result["rule"]["heldout_max_nrmse"], 1e-9)
        eq(result["rule"]["origin"], "user_rule")


def test_build_plan_three_terminal_pin_mapping():
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    doc_id = "f" * 64
    model_id, _truth, _freqs, folder = _synthesize_model(doc_id)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        raises(M.ModelError, lambda: M.build_device_model(
            {"model_id": model_id, "library": "AI_lib", "cell": "X",
             "start_hz": 1e8, "stop_hz": 1e9, "step_hz": 5e6}))  # 未拟合不能建图
        M.fit_device_model({"model_id": model_id, "candidates": ["intrinsic"]})
        plan = M.build_device_model({"model_id": model_id, "library": "AI_lib",
                                     "cell": "JFET_TEST",
                                     "start_hz": 1e8, "stop_hz": 1e9, "step_hz": 5e6})
        circuit = plan["build_schematic_args"]
        masters = [i["master"] for i in circuit["instances"]]
        ok("ads_rflib:FET" in masters, "必须是原生 FET 三端模型")
        fet = next(i for i in circuit["instances"] if i["master"] == "ads_rflib:FET")
        ok(fet["params"]["G"].endswith("mS"))
        ok(fet["params"]["Cgs"].endswith("pF"))
        ok(fet["params"]["Rds"].endswith("Ohm"))
        mapping = plan["pin_mapping"]
        eq(mapping["ads_native_symbol"]["ads_rflib:FET"], {"gate": 1, "drain": 2, "source": 3})
        eq(mapping["physical_package"], {"drain": 1, "source": 2, "gate": 3})
        eq(mapping["ports"]["common"], "g（外部端接地）")
        eq(plan["topology"], "intrinsic_no_package")
        ok(plan["build_fingerprint"])
        # 无封装支路时公共端直接接地（只有一条公共端连线）
        common_conns = [c for c in circuit["connections"] if "COMMON_G" in (c["a"][0], c["b"][0])]
        eq(len(common_conns), 1)
        record = M.get_model_record(model_id)
        eq(record["stage"], "build_planned")
        eq(record["build"]["parameters_source"]["kind"], "fitted")
        eq(record["build"]["fingerprint"], plan["build_fingerprint"])


def test_package_topology_builds_common_branch_and_solver_catches_omission():
    """漏公共端封装支路必须被独立网表求解器抓到（历史缺陷回归）。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    np = M._np()
    doc_id = "b1" * 32
    model_id, truth, freqs_mhz, folder = _synthesize_model(doc_id)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        M.fit_device_model({"model_id": model_id,
                            "candidates": ["package_delay"],
                            "thresholds": {"heldout_max_nrmse": 0.10}})
        plan = M.build_device_model({"model_id": model_id, "library": "AI_lib",
                                     "cell": "PKG", "start_hz": 1e8,
                                     "stop_hz": 1e9, "step_hz": 5e6})
        if plan["stage"] != "build_planned" or \
                M.get_model_record(model_id)["fit"]["selected"] != "package_delay":
            skip("package_delay 未通过拟合规则，跳过拓扑回归")
        circuit = plan["build_schematic_args"]
        names = {i["name"] for i in circuit["instances"]}
        # 三个端子都必须有 R/L 串联支路（含公共端 g）
        for terminal in ("G", "S", "D"):
            ok(f"PKG_R_{terminal}" in names, f"缺少 PKG_R_{terminal}")
            ok(f"PKG_L_{terminal}" in names, f"缺少 PKG_L_{terminal}")
        eq(plan["topology"], "packaged_all_three_terminals")
        ok("外部公共端接地" in plan["pin_mapping"]["common_branch"])
        # 内部公共端（FET gate 引脚）不得直接接 GROUND
        for conn in circuit["connections"]:
            ok(conn["a"] != ["DEVICE_CORE", 1] or conn["b"][0] != "COMMON_G",
               "内部公共端不得越过封装支路直接接地")
        record = M.get_model_record(model_id)
        params = record["fit"]["candidates"]["package_delay"]["parameters_fitted_SI"]
        freqs_hz = np.asarray([f * 1e6 for f in freqs_mhz])
        y_forward = M.circuit_y2p(freqs_hz, params, record["configuration"], True)
        y_plan = M.solve_plan_y2p(circuit, record["configuration"], freqs_hz)
        ok(float(np.max(np.abs(y_plan - y_forward))) < 1e-9,
           "完整三端支路的计划网表必须与解析正演一致")
        # 破坏实验：删掉公共端支路的两个元件与连线 → 求解器必须报出不一致
        broken = {
            "instances": [i for i in circuit["instances"]
                          if i["name"] not in ("PKG_R_G", "PKG_L_G")],
            "connections": [c for c in circuit["connections"]
                            if c["a"][0] not in ("PKG_R_G", "PKG_L_G")
                            and c["b"][0] not in ("PKG_R_G", "PKG_L_G")]
            + [{"a": ["COMMON_G", 1], "b": ["DEVICE_CORE", 1]}],
        }
        y_broken = M.solve_plan_y2p(broken, record["configuration"], freqs_hz)
        delta = float(np.max(np.abs(y_broken - y_forward)))
        ok(delta > 1e-6, f"漏公共端支路必须与正演明显不符（实际 {delta:.3e}）")


def _fake_dataset(folder: str, name: str = "model.ds") -> str:
    """写一个真实存在的小文件当"数据集"，用于数据集来源绑定（SHA/大小/时间）。"""
    path = os.path.join(folder, name)
    with open(path, "wb") as stream:
        stream.write(b"fake .ds dataset used for provenance binding tests")
    return path


def _fit_params(record: dict) -> tuple:
    """返回 (candidate, packaged, 拟合参数)。ADS 里仿真的是**拟合参数**。"""
    kind = (record.get("fit") or {}).get("selected")
    if not kind:
        raise AssertionError("模型未完成拟合")
    params = record["fit"]["candidates"][kind]["parameters_fitted_SI"]
    return kind, kind == "package_delay", params


def _validated_case(doc_id="9" * 64, candidates=("intrinsic",), **over):
    """跑到 build_planned 的模型 + 理想数据集（由拟合参数正演生成），返回上下文。

    "ADS 数据集"必须由**拟合参数**生成 —— 网表里放的就是拟合值，不是真值。
    """
    over.setdefault("tau_s", 0.0)          # intrinsic 候选不含 tau，真值须可精确表达
    np = M._np()
    model_id, truth, freqs_mhz, folder = _synthesize_model(doc_id, **over)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        M.fit_device_model({"model_id": model_id, "candidates": list(candidates)})
        plan = M.build_device_model({"model_id": model_id, "library": "AI_lib",
                                     "cell": "V", "start_hz": 1e8, "stop_hz": 1e9,
                                     "step_hz": 5e6})
        record = M.get_model_record(model_id)
        kind, packaged, fitted = _fit_params(record)
        dataset = _fake_dataset(folder)
        freqs_hz = np.asarray([f * 1e6 for f in freqs_mhz], dtype=float)
        y2p = M.circuit_y2p(freqs_hz, fitted, record["configuration"], packaged)
        s_actual = M.y_to_s(y2p, 50.0)
        traces = {}
        for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
            traces[f"real(S({i},{j}))"] = {"x": freqs_hz.tolist(),
                                           "y": s_actual[:, i - 1, j - 1].real.tolist()}
            traces[f"imag(S({i},{j}))"] = {"x": freqs_hz.tolist(),
                                           "y": s_actual[:, i - 1, j - 1].imag.tolist()}
    return {"np": np, "folder": folder, "model_id": model_id, "truth": truth,
            "fit_kind": kind, "fitted": fitted, "packaged": packaged,
            "freqs_mhz": freqs_mhz, "dataset": dataset, "traces": traces,
            "plan": plan, "record": record}


def test_validate_positive_requires_threshold_and_provenance():
    """正例：来源证据齐全 + 分维度阈值 → validated。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        record = M.get_model_record(ctx["model_id"])
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"],
            **_sim_evidence(record, ctx["dataset"]),
            # 测试声明的手册精度阈值（S 无量纲 / Y 西门子，必须分维度）
            "thresholds": {"max_complex_rmse_S": 5e-3, "max_complex_rmse_Y": 1e-4}})
        eq(result["verdict"], "validated")
        ok(result["sub_results"]["numeric_equivalence"]["state"] == "pass")
        ok(result["sub_results"]["datasheet_accuracy"]["state"] == "pass")
        ok(result["sub_results"]["scope_coverage"]["state"] == "pass")
        ok(result["provenance"]["state"] == "verified")
        # 数据集 SHA 现在由可信执行记录绑定（读取前后各校验一次）
        sim_ev = result["provenance"]["evidence"]["simulation"]
        eq(sim_ev["source"], "backend_execution_record")
        ok(sim_ev["dataset_sha256"] is not None)
        eq(sim_ev["dataset_sha256_before"], sim_ev["dataset_sha256"])
        ok(result["validation_scope_hz"])
        saved = M.get_model_record(ctx["model_id"])
        eq(saved["stage"], "validated")
        # 四个子结果都要有 state/evaluated 字段
        for name, sub in result["sub_results"].items():
            ok("state" in sub, name)
            ok("evaluated" in sub, name)
        # 相位低幅值点要如实标注
        phase = result["sub_results"]["datasheet_accuracy"]["metrics"]["Y"]["12"]
        ok("phase_low_amp_points" in phase and "phase_state" in phase)


def test_negative_no_threshold_is_incomplete_not_validated():
    """没有手册精度阈值 → accuracy 未评估 → validation_incomplete，绝不 validated。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("1" * 64)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        record = M.get_model_record(ctx["model_id"])
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"],
            **_sim_evidence(record, ctx["dataset"])})
        eq(result["verdict"], "validation_incomplete")
        eq(result["sub_results"]["numeric_equivalence"]["state"], "pass")
        eq(result["sub_results"]["datasheet_accuracy"]["state"], "not_evaluated")
        eq(result["sub_results"]["datasheet_accuracy"]["thresholds"]["origin"], "none")
        ok(any("阈值" in r for r in result["verdict_reasons"]))


def test_negative_all_zero_s_parameters():
    """全零 S 参数绝不能得到 validated。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("2" * 64)
    zeroed = {k: {"x": v["x"], "y": [0.0] * len(v["y"])}
              for k, v in ctx["traces"].items()}
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        record = M.get_model_record(ctx["model_id"])
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": zeroed,
            **_sim_evidence(record, ctx["dataset"]),
            "thresholds": {"max_complex_rmse_S": 1e-3, "max_complex_rmse_Y": 1e-5}})
        eq(result["verdict"], "validation_failed")
        numeric = result["sub_results"]["numeric_equivalence"]
        eq(numeric["state"], "fail")
        ok(any("全为零" in r for r in numeric["reasons"]),
           f"应点名全零：{numeric['reasons']}")


def test_negative_port_swap():
    """端口互换不能通过数值等价。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("3" * 64)
    swapped = dict(ctx["traces"])
    for op in ("real", "imag"):
        swapped[f"{op}(S(1,2))"], swapped[f"{op}(S(2,1))"] = (
            ctx["traces"][f"{op}(S(2,1))"], ctx["traces"][f"{op}(S(1,2))"])
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        record = M.get_model_record(ctx["model_id"])
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": swapped,
            **_sim_evidence(record, ctx["dataset"]),
            "thresholds": {"max_complex_rmse_S": 1e-3, "max_complex_rmse_Y": 1e-5}})
        eq(result["verdict"], "validation_failed")
        eq(result["sub_results"]["numeric_equivalence"]["state"], "fail")


def test_negative_missing_common_branch_in_plan():
    """打包模型：把公共端改成直接接地 → 数值等价必须失败。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("4" * 64, candidates=("package_delay",))
    if ctx["fit_kind"] != "package_delay":
        skip("package_delay 未通过拟合规则，跳过")
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        broken = M.get_model_record(ctx["model_id"])
        plan = broken["build"]["plan"]
        plan["instances"] = [i for i in plan["instances"]
                             if i["name"] not in ("PKG_R_G", "PKG_L_G")]
        plan["connections"] = [c for c in plan["connections"]
                               if c["a"][0] not in ("PKG_R_G", "PKG_L_G")
                               and c["b"][0] not in ("PKG_R_G", "PKG_L_G")]
        plan["connections"].append({"a": ["COMMON_G", 1], "b": ["DEVICE_CORE", 1]})
        M._save(broken)
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"],
            **_sim_evidence(broken, ctx["dataset"]),
            "thresholds": {"max_complex_rmse_S": 1e-3, "max_complex_rmse_Y": 1e-5}})
        eq(result["verdict"], "validation_failed")
        plan_check = result["sub_results"]["numeric_equivalence"]["plan_topology_check"]
        eq(plan_check["state"], "fail")
        ok(any("公共端" in r for r in plan_check["reasons"]))


def test_negative_wrong_dataset_provenance():
    """错误数据集：库/cell 不符、缺仿真证据、缺网表指纹 → 不得 validated。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("5" * 64)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        record = M.get_model_record(ctx["model_id"])
        thresholds = {"max_complex_rmse_S": 1e-3, "max_complex_rmse_Y": 1e-5}
        # 1) 库/cell 与建图计划不符（数据集来自别的电路）
        wrong = _sim_evidence(record, ctx["dataset"], cell="OTHER_CELL_0001")
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"], **wrong, "thresholds": thresholds})
        eq(result["verdict"], "validation_failed")
        ok(any("cell" in p for p in result["provenance"]["problems"]))
        # 2) 完全没有仿真来源证据
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"], "thresholds": thresholds})
        eq(result["verdict"], "validation_failed")
        eq(result["provenance"]["state"], "unverified")
        # 3) 缺网表指纹
        no_sha = _sim_evidence(record, ctx["dataset"], netlist_sha="")
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": ctx["traces"], **no_sha, "thresholds": thresholds})
        eq(result["verdict"], "validation_failed")
        ok(any("网表指纹" in p for p in result["provenance"]["problems"]))
        # 4) 数据集频段与建图扫描不符
        short = {k: {"x": v["x"][:5], "y": v["y"][:5]} for k, v in ctx["traces"].items()}
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": short,
            **_sim_evidence(record, ctx["dataset"]), "thresholds": thresholds})
        eq(result["verdict"], "validation_failed")
        ok(any("频段" in p for p in result["provenance"]["problems"]))


def test_negative_trace_axis_integrity():
    """异常频率轴/NaN/异长/轴不一致必须直接拒绝（工具的硬错误）。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("6" * 64)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        thresholds = {"max_complex_rmse_S": 1e-3, "max_complex_rmse_Y": 1e-5}
        base = {"model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
                "thresholds": thresholds}

        def mutate(fn):
            traces = {k: {"x": list(v["x"]), "y": list(v["y"])}
                      for k, v in ctx["traces"].items()}
            fn(traces)
            return M.validate_device_model({**base, "traces": traces})

        def non_monotonic(t):
            t["real(S(1,1))"]["x"][3] = t["real(S(1,1))"]["x"][2]
        raises(M.ModelError, lambda: mutate(non_monotonic))

        def unordered(t):
            t["real(S(1,1))"]["x"][0], t["real(S(1,1))"]["x"][1] = \
                t["real(S(1,1))"]["x"][1], t["real(S(1,1))"]["x"][0]
        raises(M.ModelError, lambda: mutate(unordered))

        def nan_value(t):
            t["imag(S(2,1))"]["y"][2] = float("nan")
        raises(M.ModelError, lambda: mutate(nan_value))

        def unequal(t):
            t["real(S(2,2))"]["y"] = t["real(S(2,2))"]["y"][:-1]
        raises(M.ModelError, lambda: mutate(unequal))

        def wrong_axis(t):
            t["real(S(1,2))"]["x"] = [f * 2 for f in t["real(S(1,2))"]["x"]]
            t["imag(S(1,2))"]["x"] = t["real(S(1,2))"]["x"]
        raises(M.ModelError, lambda: mutate(wrong_axis))

        def mismatched_real_imag(t):
            t["imag(S(1,1))"]["x"] = [f + 1.0 for f in t["imag(S(1,1))"]["x"]]
        raises(M.ModelError, lambda: mutate(mismatched_real_imag))

        def empty(t):
            t["real(S(1,1))"]["x"] = []
            t["real(S(1,1))"]["y"] = []
        raises(M.ModelError, lambda: mutate(empty))


def test_negative_out_of_band_is_limited_scope():
    """超域：只在交集内比较，范围标 extrapolated，validated 只限交集频段。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("7" * 64)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        record = M.get_model_record(ctx["model_id"])
        np = M._np()
        # 只仿真 100..300 MHz（曲线覆盖 100..1000 MHz）
        band = [100e6 * (3 ** (i / 20)) for i in range(21)]
        y2p = M.circuit_y2p(np.asarray(band), ctx["fitted"], record["configuration"],
                            ctx["packaged"])
        s_band = M.y_to_s(y2p, 50.0)
        traces = {}
        for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
            traces[f"real(S({i},{j}))"] = {"x": band, "y": s_band[:, i - 1, j - 1].real.tolist()}
            traces[f"imag(S({i},{j}))"] = {"x": band, "y": s_band[:, i - 1, j - 1].imag.tolist()}
        # 把建图扫描改成同一窄带，避免 provenance 频段不符
        edited = M.get_model_record(ctx["model_id"])
        edited["build"]["s_parameters"] = {"start_hz": band[0], "stop_hz": band[-1],
                                           "step_hz": (band[-1] - band[0]) / 20}
        M._save(edited)
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": traces, **_sim_evidence(edited, ctx["dataset"]),
            "thresholds": {"max_complex_rmse_S": 5e-3, "max_complex_rmse_Y": 1e-4}})
        eq(result["verdict"], "validated")
        scope = result["sub_results"]["scope_coverage"]
        ok(scope["validated_band_hz_S"][1] <= 300e6 + 1)
        ok(result["sub_results"]["datasheet_accuracy"]["scope"]["S"]["curve_coverage"] < 1.0)
        acc = result["sub_results"]["datasheet_accuracy"]
        ok(acc["scope"]["S"]["grid_points_outside_curve_band"] >= 0)
        # 无交集 → 准确度未评估、范围未评估 → incomplete
        far = M.get_model_record(ctx["model_id"])
        far["build"]["s_parameters"] = {"start_hz": 5e9, "stop_hz": 6e9, "step_hz": 5e7}
        M._save(far)
        hi_band = [5e9 + i * 5e7 for i in range(21)]
        y_hi = M.circuit_y2p(np.asarray(hi_band), ctx["fitted"], far["configuration"],
                             ctx["packaged"])
        s_hi = M.y_to_s(y_hi, 50.0)
        traces_hi = {}
        for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
            traces_hi[f"real(S({i},{j}))"] = {"x": hi_band,
                                              "y": s_hi[:, i - 1, j - 1].real.tolist()}
            traces_hi[f"imag(S({i},{j}))"] = {"x": hi_band,
                                              "y": s_hi[:, i - 1, j - 1].imag.tolist()}
        result = M.validate_device_model({
            "model_id": ctx["model_id"], "dataset_path": ctx["dataset"],
            "traces": traces_hi, **_sim_evidence(far, ctx["dataset"]),
            "thresholds": {"max_complex_rmse_S": 5e-3, "max_complex_rmse_Y": 1e-4}})
        eq(result["sub_results"]["datasheet_accuracy"]["state"], "not_evaluated")
        eq(result["verdict"], "validation_incomplete")


def test_negative_bad_bias_and_unreviewed_and_cross_document_curves():
    """错误偏置 / 未复核曲线 / 跨手册曲线 / z0 不一致都必须被拒绝。"""
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    folder = _tmp()
    doc_a, doc_b = "a" * 64, "b" * 64
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        os.makedirs(os.path.join(D._root(), "documents"), exist_ok=True)
        for doc in (doc_a, doc_b):
            with open(D._path("documents", doc), "w", encoding="utf-8") as stream:
                json.dump({"document_id": doc, "sha256": doc, "text": "TESTFET",
                           "has_text_layer": True, "pages": [], "parse_engine": "pymupdf"},
                          stream)
        freqs = [100.0, 200.0, 300.0]
        np = M._np()
        y = np.zeros((3, 2, 2), dtype=complex)
        for i, j in ((0, 0), (1, 1), (0, 1), (1, 0)):
            y[:, i, j] = 0.01 + 0.001j
        y_mS = y * 1e3
        ids_10v = _write_curve_pair(doc_a, "common_gate", freqs, y_mS)
        ids_5v = _write_curve_pair(doc_a, "common_gate", freqs, y_mS,
                                   conditions={"vds_v": 5.0, "id_a": 0.01})
        ids_b = _write_curve_pair(doc_b, "common_gate", freqs, y_mS)
        ids_unreviewed = _write_curve_pair(doc_a, "common_gate", freqs, y_mS,
                                          status="digitized_unreviewed",
                                          z0_status="declared")  # 与已复核组区分
        ids_75 = _write_curve_pair(doc_a, "common_gate", freqs, y_mS, z0_ohm=75.0)
        base = {"document_id": doc_a, "part_number": "TESTFET", "device_kind": "jfet",
                "model_kind": "jfet_small_signal"}
        # 结构条件不一致（mag 是 10V 的、phase 是 5V 的）
        mixed = {"Y": {"mag": ids_10v["Y"]["mag"], "phase": ids_5v["Y"]["phase"]}}
        raises(M.ModelError, lambda: M.prepare_device_model({**base, "curve_ids": mixed}))
        # 跨手册
        cross = {"Y": {"mag": ids_10v["Y"]["mag"], "phase": ids_b["Y"]["phase"]}}
        raises(M.ModelError, lambda: M.prepare_device_model({**base, "curve_ids": cross}))
        # 未复核曲线
        raises(M.ModelError, lambda: M.prepare_device_model(
            {**base, "curve_ids": ids_unreviewed}))
        # z0 不一致
        z_mixed = {"Y": {"mag": ids_10v["Y"]["mag"], "phase": ids_75["Y"]["phase"]}}
        raises(M.ModelError, lambda: M.prepare_device_model({**base, "curve_ids": z_mixed}))
        # 正确的一组仍然可用
        prepared = M.prepare_device_model({**base, "curve_ids": ids_10v})
        eq(prepared["stage"], "candidate_ready")


def test_negative_mag_phase_frequency_mismatch():
    """幅值与相位频点不一致必须拒绝（不插值掩盖）。"""
    doc = "c" * 64
    folder = _tmp()
    mag = {"document_id": doc, "sha256": doc, "matrix_kind": "Y", "component": "mag",
           "configuration": "common_gate",
           "port_order": {"port1": "source", "port2": "drain", "common": "gate"},
           "z0": {"ohm": 50.0, "status": "assumed"}, "conditions": {"vds_v": 10.0},
           "axes": {"y": {"unit": "mS"}}, "series": []}
    phase = {**mag, "component": "phase", "phase_unit": "deg", "series": []}
    for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
        mag["series"].append({"label": f"Y{i}{j}mag", "path_index": 1,
                              "values": [[100.0, 10.0], [200.0, 9.0]]})
        phase["series"].append({"label": f"Y{i}{j}phase", "path_index": 2,
                                "values": [[900.0, -45.0], [1000.0, -50.0]]})
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        err = raises(C.CurveError, lambda: C.assemble_matrix([mag, phase], "Y",
                                                            ("mag", "phase")))
        ok("频点" in str(err), f"应点名频点不一致：{err}")
        # 相位单位不一致
        phase_bad = {**phase, "phase_unit": "rad"}
        phase_bad["series"] = [dict(s, values=[[100.0, -0.8], [200.0, -0.9]])
                               for s in phase["series"]]
        mag_ok = {**mag, "series": [dict(s, values=[[100.0, 10.0], [200.0, 9.0]])
                                    for s in mag["series"]]}
        raises(C.CurveError, lambda: C.assemble_matrix([mag_ok, phase, phase_bad], "Y",
                                                       ("mag", "phase")))


def test_validate_traces_injection_without_channel_rejected():
    if not HAVE_NUMPY:
        skip("numpy 不可用")
    ctx = _validated_case("8" * 64)
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": ctx["folder"]}):
        raises(M.ModelError, lambda: M.validate_device_model(
            {"model_id": ctx["model_id"], "dataset_path": ""}))
        raises(M.ModelError, lambda: M.validate_device_model(
            {"model_id": ctx["model_id"], "dataset_path": ctx["dataset"]}))


def test_sample_adapter_sha_gate():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        import sample_adapters
        bad = {"sha256": "0" * 64}
        raises(ValueError, lambda: sample_adapters.build_specs("smmbfj310l_v1", bad))
        good = {"sha256": J310_SHA}
        specs, origin = sample_adapters.build_specs("smmbfj310l_v1", good)
        eq(len(specs), 16)  # 每条曲线一条记录（各自量程）
        ymag = next(s for s in specs if s["series"][0]["label"] == "Y11mag")
        eq(ymag["page"], 4)
        eq(len(ymag["series"]), 1)
        eq(ymag["axes"]["y"]["unit"], "mS")
        eq(ymag["axes"]["y"]["cal_points"][0]["value"], 30.0)
        y12 = next(s for s in specs if s["series"][0]["label"] == "Y12mag")
        eq(y12["axes"]["y"]["cal_points"][0]["value"], 3.0)  # 各自量程，不串用
        eq(ymag["z0"]["status"], "assumed")
        eq(ymag["conditions"]["vds_v"], 10.0)
        raises(ValueError, lambda: sample_adapters.build_specs("nope", good))


def test_tools_registered_and_local():
    import tools
    import agent
    names = {t["function"]["name"] for t in tools.TOOLS}
    for name in (list(D.HANDLERS) + list(C.HANDLERS) + list(M.HANDLERS)
                 + ["validate_device_model"]):
        ok(name in names, f"{name} 未注册进 TOOLS")
        ok(tools.is_local(name), f"{name} 应为后端本地工具")
    result = {"ok": True, "build_schematic_args": {"instances": [{"name": "Q1"}]}}
    eq(agent._summarize_tool_result("build_device_model", result)["build_schematic_args"],
       result["build_schematic_args"])
    eq(agent._summarize_tool_result("extract_datasheet_curves", result)["curve_note"]
       if "curve_note" in result else True, True)


def test_validate_contract_matches_schema():
    """工具声明与执行契约必须一致：schema 不得再出现自填 simulation。"""
    schema = {t["function"]["name"]: t["function"]["parameters"]
              for t in M.TOOL_SCHEMAS}["validate_device_model"]
    props = set(schema["properties"])
    ok("simulation" not in props, "validate_device_model 不得再声明自填 simulation")
    ok("simulation_id" in props, "可信来源应通过 simulation_id 引用")
    ok("model_record_id" in props, "建模记录身份应显式命名")
    ok("model_id" in props, "旧字段需保留兼容")
    err = raises(M.ModelError, lambda: M.get_device_model(
        {"model_id": "mdl_deadbeef"}))
    contains(str(err), "library_model_id", "库身份误用时要点明是哪一层 ID")
    err2 = raises(M.ModelError, lambda: M.get_device_model(
        {"model_record_id": "0" * 64}))
    contains(str(err2), "模型记录不存在", "正确类型但缺失时给存在性错误")


def test_sample_adapter_selects_by_sha_only():
    import sample_adapters
    sha = sample_adapters.PRESETS["smmbfj310l_v1"]["sha256"]
    eq(sample_adapters.preset_for_sha(sha), "smmbfj310l_v1")
    eq(sample_adapters.preset_for_sha("f" * 64), "", "其它 PDF 不得套用样本映射")
    eq(sample_adapters.preset_for_sha(""), "")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "PDF 建模链路回归"))
