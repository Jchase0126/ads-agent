"""曲线矩阵组装硬化与复核门禁的回归测试（阶段二）。

覆盖文档复现的三个缺陷与新增复核工具：
* 重复标签静默覆盖 → 必须拒绝；
* 幅值单位被最后一条记录覆盖 → 必须一致，否则拒绝；
* 相位单位随记录顺序丢失 → 组装结果与顺序无关；
另加非法下标、矩阵前缀错配、缺 2×2、幅相频点不一致等负例。
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, raises, run
add_path("backend")

import datasheet_curves as C
import datasheet_models as D


def _tmp() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="curves_test_")


DOC = "a1" * 32


def _values(freqs, value):
    return [[float(f), float(value)] for f in freqs]


def _record(kind="Y", comp="mag", unit="mS", labels=("Y11",), freqs=(100.0, 200.0),
            value=10.0, phase_unit=None, status="digitized_reviewed", origin=None,
            z0=50.0, z0_status="assumed", configuration="common_gate",
            conditions=None, doc_id=DOC, document_id=None):
    series = [{"label": f"{label}{comp}", "path_index": idx + 1,
               "values": _values(freqs, value)}
              for idx, label in enumerate(labels)]
    payload = {
        "document_id": document_id or doc_id, "sha256": doc_id, "page": 4, "figure": "Fig",
        "axes": {"x": {"unit": "MHz", "scale": "log10",
                       "cal_points": [{"at": 100, "value": 100.0},
                                      {"at": 1000, "value": 1000.0}]},
                 "y": {"unit": unit, "scale": "linear",
                       "cal_points": [{"at": 0, "value": 30.0}, {"at": 210, "value": 0.0}]}},
        "matrix_kind": kind, "component": comp, "phase_unit": phase_unit,
        "configuration": configuration,
        "port_order": {"port1": "source", "port2": "drain", "common": "gate"},
        "z0": {"ohm": z0, "status": z0_status},
        "conditions": conditions if conditions is not None else {"vds_v": 10.0, "id_a": 0.01},
        "sampling": {"mode": "linear", "count": len(freqs),
                     "start": freqs[0], "stop": freqs[-1]},
        "series": series,
    }
    import hashlib
    record_id = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return record_id, {**payload, "curve_id": record_id, "status": status,
                       "origin": origin or {"method": "test"}, "reliability": {}}


def _write(record):
    C._write(record["curve_id"], record)
    return record


def _full_pair(kind="Y", unit="mS", freqs=(100.0, 200.0), value=10.0, **over):
    """四个元素的 mag/phase 记录（完整 2×2）。"""
    elems = ("11", "21", "12", "22")
    mags = [_record(kind=kind, comp="mag", unit=unit, labels=(f"{kind}{e}",),
                    freqs=freqs, value=value, **over)[1] for e in elems]
    phases = [_record(kind=kind, comp="phase", unit=unit, labels=(f"{kind}{e}",),
                      freqs=freqs, value=-45.0, phase_unit="deg", **over)[1]
              for e in elems]
    return [_write(r) for r in mags + phases]


# ---------------------------------------------------------------------------
# assemble_matrix 硬化
# ---------------------------------------------------------------------------

def test_assemble_rejects_duplicate_labels():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        records = _full_pair()
        dup = _record(comp="mag", labels=("Y11",), value=9.0)[1]   # 又一条 Y11mag
        err = raises(C.CurveError, lambda: C.assemble_matrix(
            records + [dup], "Y", ("mag", "phase")))
        ok("重复" in str(err), f"应点名重复：{err}")


def test_assemble_rejects_magnitude_unit_mismatch():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        records = _full_pair()
        other_unit = _record(comp="mag", unit="S", labels=("Y11",), value=0.01)[1]
        err = raises(C.CurveError, lambda: C.assemble_matrix(
            records + [other_unit], "Y", ("mag", "phase")))
        ok("单位" in str(err), f"应点名单位不一致：{err}")


def test_assemble_is_order_independent_and_keeps_phase_unit():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        records = _full_pair()
        # 相位给成 rad：应被尊重，且不受记录顺序影响（mag 先/后都一样）
        rad = []
        for r in records:
            if r["component"] == "phase":
                r = dict(r, phase_unit="rad")
                rad.append(r)
            else:
                rad.append(r)
        f1, m1, u1 = C.assemble_matrix(records, "Y", ("mag", "phase"))
        f2, m2, u2 = C.assemble_matrix(list(reversed(records)), "Y", ("mag", "phase"))
        eq(f1, f2)
        ok(abs(m1[0][(1, 1)] - m2[0][(1, 1)]) < 1e-15)
        f3, m3, _u3 = C.assemble_matrix(rad, "Y", ("mag", "phase"))
        eq(f3, f1)
        # deg 的 -45° 与 rad 的 -45° 在数值上一致（都写成 -45 度值）
        ok(abs(m1[0][(1, 1)] - m3[0][(1, 1)]) > 1e-6,
           "rad 相位必须按弧度解释，不能被当成 deg 静默接受")
        eq(u1, "mS")


def test_assemble_rejects_illegal_index_and_prefix():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        bad_index = [_record(kind="S", comp="mag", unit="1", labels=("S13",))[1],
                     _record(kind="S", comp="phase", unit="1", labels=("S13",),
                             phase_unit="deg")[1]]
        err = raises(C.CurveError, lambda: C.assemble_matrix(
            bad_index, "S", ("mag", "phase")))
        ok("下标" in str(err) or "无法解析" in str(err), f"非法下标应拒绝：{err}")
        # 前缀错配：S 组装里混入 Y 标签
        mixed = [_record(kind="S", comp="mag", unit="1", labels=("Y11",))[1],
                 _record(kind="S", comp="phase", unit="1", labels=("Y11",),
                         phase_unit="deg")[1]]
        err = raises(C.CurveError, lambda: C.assemble_matrix(
            mixed, "S", ("mag", "phase")))
        ok("矩阵类型" in str(err) or "无法解析" in str(err), f"前缀错配应拒绝：{err}")


def test_assemble_requires_complete_2x2():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        partial = [r for r in _full_pair() if "Y21" not in r["series"][0]["label"]]
        err = raises(C.CurveError, lambda: C.assemble_matrix(
            partial, "Y", ("mag", "phase")))
        ok("2×2" in str(err) and "Y21" in str(err), f"应列出缺失元素：{err}")


def test_assemble_rejects_frequency_mismatch():
    """文档复现：幅值 [100,200] MHz、相位 [900,1000] MHz 必须报错。"""
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        mag = _record(comp="mag", labels=("Y11",), freqs=(100.0, 200.0))[1]
        phase = _record(comp="phase", labels=("Y11",), freqs=(900.0, 1000.0),
                        phase_unit="deg")[1]
        err = raises(C.CurveError, lambda: C.assemble_matrix(
            [mag, phase], "Y", ("mag", "phase")))
        ok("频点" in str(err), f"幅相频点不一致应拒绝：{err}")


def test_assemble_rejects_non_finite_and_empty():
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": _tmp()}):
        records = _full_pair()
        broken = [dict(r) for r in records]
        broken[0] = dict(broken[0], series=[dict(broken[0]["series"][0],
                                                  values=[[100.0, float("nan")]])])
        raises(C.CurveError, lambda: C.assemble_matrix(broken, "Y", ("mag", "phase")))
        empty = [dict(r) for r in records]
        empty[0] = dict(empty[0], series=[dict(empty[0]["series"][0], values=[])])
        raises(C.CurveError, lambda: C.assemble_matrix(empty, "Y", ("mag", "phase")))


# ---------------------------------------------------------------------------
# 单位换算与证据等级
# ---------------------------------------------------------------------------

def test_mag_scale_si():
    eq(C.mag_scale_si("S"), 1.0)
    eq(C.mag_scale_si("mS"), 1e-3)
    eq(C.mag_scale_si("mmhos"), 1e-3)
    eq(C.mag_scale_si("µS"), 1e-6)
    eq(C.mag_scale_si("1"), 1.0)
    eq(C.mag_scale_si("furlong"), None)


def test_curve_evidence_state():
    eq(C.curve_evidence_state({"origin": {"preset": "p"}}), "preset_adapter")
    eq(C.curve_evidence_state({"origin": {"method": "x"},
                               "status": "digitized_reviewed"}), "reviewed")
    eq(C.curve_evidence_state({"origin": {"method": "x"},
                               "status": "digitized_unreviewed"}), "unreviewed")


# ---------------------------------------------------------------------------
# review_datasheet_curves
# ---------------------------------------------------------------------------

def test_review_tool_promotes_and_reports_missing_evidence():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        records = _full_pair(status="digitized_unreviewed")
        ids = [r["curve_id"] for r in records]
        evidence = {"axes": True, "legend": True, "path": True, "bias": True, "ports": True}
        result = C.review_datasheet_curves({"curve_ids": ids, "evidence": evidence})
        eq(result["ok"], True)
        eq(sorted(result["reviewed"]), sorted(ids))
        eq(C.get_curve(ids[0])["status"], "digitized_reviewed")
        ok(C.get_curve(ids[0])["review_evidence"]["axes"])
        # 缺一项证据 → 保持未复核并列出缺失
        more = _full_pair(status="digitized_unreviewed", unit="mS",
                          freqs=(300.0, 400.0))
        more_ids = [r["curve_id"] for r in more]
        result = C.review_datasheet_curves({
            "curve_ids": more_ids,
            "evidence": {"axes": True, "legend": True, "path": True, "bias": False,
                         "ports": True}})
        eq(result["reviewed"], [])
        eq(len(result["failures"]), len(more_ids))
        ok(any("bias" in p for p in result["failures"][0]["problems"]))
        eq(C.get_curve(more_ids[0])["status"], "digitized_unreviewed")
        # 结构不完整（缺 port_order）也不能复核
        broken = _record(comp="mag", labels=("Y11",))[1]
        broken.pop("port_order")
        _write(broken)
        result = C.review_datasheet_curves({
            "curve_ids": [broken["curve_id"]], "evidence": evidence})
        eq(result["reviewed"], [])
        ok(any("端口顺序" in p for p in result["failures"][0]["problems"]))


def test_review_tool_reverifies_preset_sha_lock():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        # 文档 SHA 与适配器锁定值不符 → preset 曲线复核必须失败
        doc = "b2" * 32
        os.makedirs(os.path.join(D._root(), "documents"), exist_ok=True)
        with open(D._path("documents", doc), "w", encoding="utf-8") as stream:
            json.dump({"document_id": doc, "sha256": doc, "text": "",
                       "has_text_layer": True, "pages": []}, stream)
        rec = _record(origin={"method": "sample_adapter", "preset": "smmbfj310l_v1",
                              "sha256": doc}, doc_id=doc, document_id=doc)[1]
        _write(rec)
        result = C.review_datasheet_curves({
            "curve_ids": [rec["curve_id"]],
            "evidence": {"axes": True, "legend": True, "path": True, "bias": True,
                         "ports": True}})
        eq(result["reviewed"], [])
        ok(any("适配器" in p for p in result["failures"][0]["problems"]))
        # preset 曲线的证据等级本来就是 preset_adapter（可信回归适配器）
        eq(C.curve_evidence_state(rec), "preset_adapter")


def test_review_requires_evidence_object():
    folder = _tmp()
    with patch.dict(os.environ, {"ADS_AGENT_DATA_DIR": folder}):
        records = _full_pair(status="digitized_unreviewed")
        raises(C.CurveError, lambda: C.review_datasheet_curves(
            {"curve_ids": [records[0]["curve_id"]]}))
        raises(C.CurveError, lambda: C.review_datasheet_curves(
            {"curve_ids": [], "evidence": {}}))


if __name__ == "__main__":
    raise SystemExit(run(globals(), "曲线组装与复核回归"))
