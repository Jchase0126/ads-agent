"""参数语义绑定与表格证据的回归测试（阶段二）。

覆盖文档复现的两个缺陷：
* 结构化条件 conditions_structured 只做白名单，原文 10 V/10 mA 也接受 99 V/99 A；
* review 成功即写 reviewed（一键洗白），语义并未核实。
并覆盖表格证据（row/column/型号列/Min-Typ-Max/脚注）与 semantic_verified 流程。
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, contains, eq, ok, raises, run
add_path("backend")

import datasheet_models as D

DOC = "e1" * 32


def _tmp() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="param_semantics_test_")


PAGE2_LINES = [
    "ELECTRICAL CHARACTERISTICS (TA = 25 C)",
    "Gate Source Cutoff Voltage",
    "(VDS = 10 Vdc, ID = 1.0 nAdc)",
    "VGS(off) −4.0 −6.5 Vdc",
    "Zero−Gate−Voltage Drain Current",
    "(VDS = 10 Vdc, VGS = 0)",
    "IDSS 24 60 mAdc",
    "Forward Transfer Admittance",
    "(VDS = 10 Vdc, ID = 10 mAdc, f = 1.0 kHz)",
    "|Yfs| 8.0 − 18 mmhos",
]


def _document(folder: str) -> str:
    """构造 2 页手工行证据文档（电气特性表）。"""
    pages = [
        {"page": 1, "width": 612, "height": 792, "has_text": True, "chars": 40,
         "images": 0, "line_count": 1, "lines": [
             {"text": "PART999 N-Channel JFET", "bbox": [10, 10, 200, 24],
              "reliable": True}]},
        {"page": 2, "width": 612, "height": 792, "has_text": True, "chars": 320,
         "images": 0, "line_count": len(PAGE2_LINES),
         "lines": [{"text": text, "bbox": [10, 10 + 20 * idx, 400, 24 + 20 * idx],
                    "reliable": True} for idx, text in enumerate(PAGE2_LINES)]},
    ]
    text = "\n".join([""] + [ln["text"] for page in pages for ln in page["lines"]]).strip()
    parsed = {"engine": "pymupdf", "page_count": 2, "pages": pages, "lines": [],
              "text": text, "chars": len(text), "has_text_layer": True,
              "parameter_extraction_safe": True, "page_numbers_available": True,
              "unknown_glyph_lines": 0}
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        D.save_document("part999.pdf", DOC, parsed, 2048)
    return DOC


def _param(name, value, unit, kind, quote, conditions, structured, symbol,
           row_id, min_typ_max, model_column="PART999", **over):
    item = {"name": name, "value": value, "unit": unit, "value_kind": kind,
            "category": "electrical_characteristic", "conditions": conditions,
            "conditions_structured": structured, "symbol": symbol,
            "source_page": 2, "source_quote": quote,
            "table": {"row_id": row_id, "column_id": f"c_{min_typ_max}",
                      "header_path": ["ELECTRICAL CHARACTERISTICS", symbol],
                      "model_column": model_column, "min_typ_max": min_typ_max,
                      "footnote": "TA = 25 C"}}
    item.update(over)
    return item


def _params(**over) -> dict:
    return {"document_id": DOC, "part_number": "PART999", "device_kind": "jfet",
            "parameters": [
                _param("VGSoff_min", -4.0, "Vdc", "minimum", "VGS(off) −4.0 −6.5 Vdc",
                       "(VDS = 10 Vdc, ID = 1.0 nAdc)", {"vds_v": 10.0, "id_a": 1e-9},
                       "VGS(off)", "r_vgsoff", "min"),
                _param("IDSS_max", 60.0, "mAdc", "maximum", "IDSS 24 60 mAdc",
                       "(VDS = 10 Vdc, VGS = 0)", {"vds_v": 10.0},
                       "IDSS", "r_idss", "max"),
            ], **over}


# ---------------------------------------------------------------------------
# 结构化条件必须绑定原文数值+单位
# ---------------------------------------------------------------------------

def test_structured_condition_evidence_helpers():
    text = "VDS = 10 Vdc, ID = 10 mAdc, f = 1.0 kHz"
    eq(D.structured_condition_evidence("vds_v", 10.0, text)[0], True)
    eq(D.structured_condition_evidence("id_a", 0.01, text)[0], True)
    eq(D.structured_condition_evidence("frequency_hz", 1000.0, text)[0], True)
    ok_, reason = D.structured_condition_evidence("vds_v", 99.0, text)
    eq(ok_, False)
    ok("99" in reason and "10 Vdc" in reason, f"原因须给出原文候选：{reason}")
    eq(D.structured_condition_evidence("vds_v", None, text)[0], True)


def test_condition_field_swap_rejected_by_symbol_binding():
    """审查 P1 负例：VGS 的 -10 不能作为 vds_v 的证据（同页同数值不等于归属）。"""
    text = "VDS = 10 Vdc, VGS = -10 Vdc"
    eq(D.structured_condition_evidence("vgs_v", -10.0, text)[0], True,
       "符号绑定正确时通过")
    ok_, reason = D.structured_condition_evidence("vds_v", -10.0, text)
    eq(ok_, False, "VGS 的值充当 VDS 证据必须拒绝")
    contains(reason, "符号绑定值不符", "必须说明 VDS 自身绑定了不同数值")
    # 同符号数值不符：vds 期望 10，写 99
    ok_, reason = D.structured_condition_evidence("vds_v", 99.0, text)
    eq(ok_, False)
    contains(reason, "99", "同符号数值不符要点出原文绑定值")
    # 表格条件列（无等号）同样算符号绑定："VGS 10 Vdc"
    eq(D.structured_condition_evidence("vgs_v", 10.0, "VGS 10 Vdc measurement")[0],
       True, "无等号的条件列写法也应识别符号绑定")
    # 额定电流不能当测试电流：IDSS 是另一个符号
    ok_, _r = D.structured_condition_evidence("id_a", 0.06, "IDSS 60 mAdc")
    eq(ok_, False, "IDSS 的 60 mA 不得作为 id_a 的证据")


def test_save_rejects_structured_conditions_not_in_quote():
    """文档复现：原文 10 V / 1.0 nA，结构化写 99 V / 99 A 必须拒绝。"""
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        bad = _params()
        bad["parameters"][0]["conditions_structured"] = {"vds_v": 99.0, "id_a": 99.0}
        err = raises(D.DatasheetError, lambda: D.save_parameters(bad))
        ok("结构化测试条件" in str(err), f"应点名结构化条件：{err}")
        # 合法值仍然可以保存
        saved = D.save_parameters(_params())
        eq(saved["status"], "extracted_unreviewed")
        eq(saved["parameters"][0]["conditions_structured"]["vds_v"], 10.0)


def test_tampered_structured_conditions_fail_review():
    """文档复现：保存后把 conditions_structured 篡改成 99/99，复核必须失败。"""
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        saved = D.save_parameters(_params())
        record = D._read("parameters", saved["parameter_set_id"])
        record["parameters"][0]["conditions_structured"] = {"vds_v": 99.0, "id_a": 99.0}
        D._write("parameters", record["parameter_set_id"], record)
        review = D.review_parameter_set({"parameter_set_id": record["parameter_set_id"]})
        eq(review["review_passed"], False)
        ok("VGSoff_min" in review["failed_parameters"])
        problems = review["checks"][0]["problems"]
        ok(any("结构化测试条件与原文不符" in p for p in problems), f"{problems}")
        eq(D._read("parameters", record["parameter_set_id"])["status"], "requires_review")


def test_review_sets_evidence_checked_not_reviewed():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        saved = D.save_parameters(_params())
        review = D.review_parameter_set({"parameter_set_id": saved["parameter_set_id"]})
        eq(review["review_passed"], True)
        eq(review["status"], "evidence_checked")
        record = D._read("parameters", saved["parameter_set_id"])
        eq(record["status"], "evidence_checked")
        eq(record["semantic_review"], "evidence_checked")
        ok(record["parameters"][0]["semantic_review"] == "evidence_checked")
        ok("verify_parameter_semantics" in review["note"])


# ---------------------------------------------------------------------------
# 表格证据与 semantic_verified
# ---------------------------------------------------------------------------

def test_table_evidence_gates():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        # min_typ_max 与 value_kind 不匹配
        bad = _params()
        bad["parameters"][0]["table"]["min_typ_max"] = "max"
        err = raises(D.DatasheetError, lambda: D.save_parameters(bad))
        ok("min_typ_max" in str(err), f"列语义不符应拒绝：{err}")
        # value_kind=minimum 却没给 min_typ_max
        bad = _params()
        bad["parameters"][0]["table"].pop("min_typ_max")
        raises(D.DatasheetError, lambda: D.save_parameters(bad))
        # 型号列与目标型号不符且无共用范围声明
        bad = _params()
        bad["parameters"][0]["table"]["model_column"] = "PART888"
        err = raises(D.DatasheetError, lambda: D.save_parameters(bad))
        ok("shared_range_quote" in str(err), f"型号列归属应有证据：{err}")
        # 给了共用范围声明但原文里查不到 → 仍然拒绝
        bad = _params()
        bad["parameters"][0]["table"]["model_column"] = "PART888"
        bad["parameters"][0]["table"]["shared_range_quote"] = "PART888 shares the same range"
        raises(D.DatasheetError, lambda: D.save_parameters(bad))
        # symbol 不在引用行
        bad = _params()
        bad["parameters"][0]["symbol"] = "VGS(off)XX"
        err = raises(D.DatasheetError, lambda: D.save_parameters(bad))
        ok("符号" in str(err), f"符号应在引用中可查：{err}")
        # 未知 table 字段
        bad = _params()
        bad["parameters"][0]["table"]["mystery"] = "x"
        raises(D.DatasheetError, lambda: D.save_parameters(bad))
        # 合法表格证据可保存并记录型号列证据态
        saved = D.save_parameters(_params())
        eq(saved["parameters"][0]["model_column_evidence"], "matches_part")
        eq(saved["parameters"][0]["table"]["row_id"], "r_vgsoff")


def test_semantic_verification_flow():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        saved = D.save_parameters(_params())
        set_id = saved["parameter_set_id"]
        # 未复核状态不允许直接做语义复核
        raises(D.DatasheetError, lambda: D.verify_parameter_semantics(
            {"parameter_set_id": set_id}))
        D.review_parameter_set({"parameter_set_id": set_id})
        result = D.verify_parameter_semantics({"parameter_set_id": set_id})
        eq(result["status"], "semantic_verified")
        record = D._read("parameters", set_id)
        eq(record["status"], "semantic_verified")
        eq(record["parameters"][0]["semantic_review"], "semantic_verified")
        # 幂等
        eq(D.verify_parameter_semantics({"parameter_set_id": set_id})["status"],
           "semantic_verified")


def test_semantic_min_max_crosscheck_catches_swapped_columns():
    """两条记录各自列声明合法，但同一参数 min > max 时必须拒绝。"""
    params = [
        {"name": "IDSS_min", "value": 60.0, "unit": "mAdc",
         "value_kind": "minimum"},
        {"name": "IDSS_max", "value": 24.0, "unit": "mAdc",
         "value_kind": "maximum"},
    ]
    problems = D._min_max_crosscheck(params)
    eq(len(problems), 1)
    contains(problems[0], "minimum", "必须点明最小值大于最大值")
    eq(D._min_max_crosscheck([
        {"name": "IDSS_min", "value": 24.0, "unit": "mAdc", "value_kind": "minimum"},
        {"name": "IDSS_max", "value": 60.0, "unit": "mAdc", "value_kind": "maximum"},
    ]), [], "正常 Min/Max 顺序通过")


def test_semantic_verification_holds_back_without_table():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        payload = _params()
        for item in payload["parameters"]:
            item.pop("table")          # 没有表格证据 → 只能停在 evidence_checked
        saved = D.save_parameters(payload)
        set_id = saved["parameter_set_id"]
        D.review_parameter_set({"parameter_set_id": set_id})
        eq(D._read("parameters", set_id)["status"], "evidence_checked")
        result = D.verify_parameter_semantics({"parameter_set_id": set_id})
        eq(result["status"], "evidence_checked")          # 绝不降级、也绝不放行
        eq(result["failed_parameters"], ["VGSoff_min", "IDSS_max"])
        ok(any("表格证据" in p for p in result["checks"][0]["problems"]))


def test_semantic_holds_back_when_model_column_undeclared_range():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document(folder)
        payload = _params()
        # 型号列共用范围有声明（原文可查的整句写在页里）→ 允许保存
        page_text = "PART999 shares VGS(off) range with PART888"
        with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
            document = D._read("documents", DOC)
            document["pages"][1]["lines"].append(
                {"text": page_text, "bbox": [10, 400, 400, 414], "reliable": True})
            document["text"] = document["text"] + " " + page_text
            D._write("documents", DOC, document)
        for item in payload["parameters"]:
            item["table"]["model_column"] = "PART888"
            item["table"]["shared_range_quote"] = page_text
        saved = D.save_parameters(payload)
        set_id = saved["parameter_set_id"]
        eq(saved["parameters"][0]["model_column_evidence"], "shared_range_declared")
        D.review_parameter_set({"parameter_set_id": set_id})
        result = D.verify_parameter_semantics({"parameter_set_id": set_id})
        eq(result["status"], "semantic_verified")


def test_legacy_path_structured_conditions():
    """旧版纯文本路径同样要做结构化条件核对。"""
    folder = _tmp()
    text = ("PART123 passive datasheet\nCapacitance C 10 pF typical at 1 MHz\n"
            "VGS 10 Vdc measurement")
    doc = __import__("hashlib").sha256(text.encode()).hexdigest()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        D.save_document("legacy.pdf", doc, {"text": text, "has_text_layer": True,
                                            "chars": len(text), "page_count": 1}, 200)
        good = {"document_id": doc, "part_number": "PART123", "device_kind": "jfet",
                "parameters": [{"name": "vg", "value": 10.0, "unit": "Vdc",
                                "value_kind": "typical",
                                "category": "electrical_characteristic",
                                "conditions_structured": {"vgs_v": 10.0},
                                "source_quote": "VGS 10 Vdc measurement"}]}
        saved = D.save_parameters(good)
        eq(saved["parameters"][0]["conditions_structured"]["vgs_v"], 10.0)
        bad = {**good, "parameters": [dict(good["parameters"][0],
                                           conditions_structured={"vgs_v": 99.0})]}
        err = raises(D.DatasheetError, lambda: D.save_parameters(bad))
        ok("结构化测试条件" in str(err), f"legacy 路径也应核对：{err}")


def test_tools_registered():
    import tools
    names = {t["function"]["name"] for t in tools.TOOLS}
    for name in ("review_parameter_set", "verify_parameter_semantics",
                 "review_datasheet_curves"):
        ok(name in names, f"{name} 未注册进 TOOLS")
        ok(tools.is_local(name), f"{name} 应为后端本地工具")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "参数语义与表格证据回归"))
