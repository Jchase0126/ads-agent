"""Datasheet evidence, SI conversion, persistence and passive template regression tests."""
import hashlib
import os
import sys
import tempfile
import zlib
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, raises, run
add_path("backend")
import datasheet_models as D
import pdf_extract


TEXT = "PART123 passive component datasheet\nCapacitance C 10 pF typical at 1 MHz\nResistance R 2 Ohm nominal\nInductance L 3 nH nominal"
DOC = hashlib.sha256(TEXT.encode()).hexdigest()


def _document():
    return D.save_document("part.pdf", DOC, {"text": TEXT, "has_text_layer": True,
                           "chars": len(TEXT), "page_count": 1}, 200)


def _parameters():
    return {"document_id": DOC, "part_number": "PART123", "device_kind": "capacitor", "parameters": [
        {"name": "capacitance", "value": 10, "unit": "pF", "value_kind": "typical",
         "category": "electrical_characteristic", "conditions": "at 1 MHz",
         "source_quote": "Capacitance C 10 pF typical at 1 MHz"},
        {"name": "resistance", "value": 2, "unit": "Ohm", "value_kind": "nominal",
         "category": "model_parameter", "source_quote": "Resistance R 2 Ohm nominal"},
    ]}


def test_document_persists_and_reads_beyond_chat_excerpt():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document()
        result = D.get_document({"document_id": DOC, "query": "Resistance"})
        ok("2 Ohm" in result["text"])
        eq(result["page_numbers_available"], False)
        eq(D.get_document({"document_id": DOC, "query": "missing"})["matched"], False)
        raises(D.DatasheetError, lambda: D.get_document({"document_id": "../../config"}))
        eq(len(os.listdir(os.path.join(folder, "datasheets", "documents"))), 1)


def test_parameters_have_verified_source_and_si_units():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document()
        result = D.save_parameters(_parameters())
        ok(abs(result["parameters"][0]["si_value"] - 1e-11) < 1e-25)
        eq(result["parameters"][0]["source_page"], None)
        eq(result["parameters"][0]["semantic_review"], "not_verified")
        eq(D.save_parameters(_parameters())["parameter_set_id"], result["parameter_set_id"])


def test_invented_values_quotes_units_and_model_numbers_rejected():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document()
        for key, bad in (("value", 999), ("source_quote", "Invented 10 pF"),
                         ("unit", "H"), ("value", float("nan")), ("value", True)):
            inputs = _parameters()
            inputs["parameters"][0][key] = bad
            raises(D.DatasheetError, lambda: D.save_parameters(inputs))
        inputs = _parameters()
        inputs["part_number"] = "WRONG999"
        raises(D.DatasheetError, lambda: D.save_parameters(inputs))


def test_passive_template_is_a_connected_new_design_and_not_a_simulation():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document()
        saved = D.save_parameters(_parameters())
        args = {"parameter_set_id": saved["parameter_set_id"], "library": "AI_lib", "cell": "PART123",
                "bindings": {"R": "resistance", "C": "capacitance"},
                "start_hz": 1e6, "stop_hz": 1e9, "step_hz": 1e6,
                "applicability": "Ideal series RC approximation; no validated wideband accuracy"}
        result = D.prepare_model(args)
        circuit = result["build_schematic_args"]
        eq(result["status"], "plan_generated")
        eq(circuit["connections"], [
            {"a": ["DS_PORT1", 1], "b": ["DS_R1", 1]},
            {"a": ["DS_R1", 2], "b": ["DS_C1", 1]},
            {"a": ["DS_C1", 2], "b": ["DS_PORT2", 1]}])
        ok(circuit["cell"] != "PART123")
        ok(D.prepare_model(args)["build_schematic_args"]["cell"] != circuit["cell"])
        eq(result["simulation_args"]["cell"], circuit["cell"])
        raises(D.DatasheetError, lambda: D.prepare_model({**args, "bindings": {"L": "capacitance"}}))
        raises(D.DatasheetError, lambda: D.prepare_model({**args, "step_hz": 0}))
        inputs = _parameters()
        inputs["parameters"][0]["category"] = "absolute_maximum"
        saved_max = D.save_parameters(inputs)
        raises(D.DatasheetError, lambda: D.prepare_model({**args, "parameter_set_id": saved_max["parameter_set_id"]}))


def test_pdf_array_spacing_and_bounded_decompression():
    content = b"BT [(PART123) -500 (Capacitance) -500 (10 pF typical)] TJ ET"
    pdf = b"%PDF-1.4\nstream\n" + zlib.compress(content) + b"\nendstream"
    result = pdf_extract.extract_text(pdf)
    ok("PART123 Capacitance 10 pF typical" in result["text"])
    with patch.object(pdf_extract, "_MAX_STREAM_BYTES", 64):
        bomb = b"%PDF-1.4\nstream\n" + zlib.compress(b"BT " + b"x" * 5000) + b"\nendstream"
        eq(pdf_extract.extract_text(bomb)["has_text_layer"], False)


def test_unparsed_font_encodings_block_parameters_even_with_readable_text():
    content = b"BT (PART123 passive component capacitance 10 pF typical) Tj ET"
    pdf = b"%PDF-1.4 /Encoding /Identity-H\nstream\n" + zlib.compress(content) + b"\nendstream"
    parsed = pdf_extract.extract_text(pdf)
    eq(parsed["has_text_layer"], True)
    eq(parsed["parameter_extraction_safe"], False)
    for mapping in (b"/Differences [1 /minus]", b"/ToUnicode 8 0 R", b"/Type /ObjStm"):
        hidden = b"%PDF-1.5\nstream\n" + zlib.compress(mapping) + b"\nendstream\n"
        hidden += b"stream\n" + zlib.compress(content) + b"\nendstream"
        eq(pdf_extract.extract_text(hidden)["parameter_extraction_safe"], False)
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        D.save_document("part.pdf", DOC, parsed, len(pdf))
        raises(D.DatasheetError, lambda: D.save_parameters(_parameters()))


def test_active_device_parameters_cannot_be_used_as_passive_model():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        _document()
        saved = D.save_parameters({**_parameters(), "device_kind": "jfet"})
        raises(D.DatasheetError, lambda: D.prepare_model({"parameter_set_id": saved["parameter_set_id"]}))


def test_tools_are_local_and_keep_template_in_llm_result():
    import agent
    import tools
    names = {t["function"]["name"] for t in tools.TOOLS}
    for name in D.HANDLERS:
        ok(name in names)
        ok(tools.is_local(name))
    result = {"ok": True, "build_schematic_args": {"instances": [{"name": "R1"}]}}
    eq(agent._summarize_tool_result("prepare_datasheet_model", result)["build_schematic_args"],
       result["build_schematic_args"])


if __name__ == "__main__":
    raise SystemExit(run(globals()))
