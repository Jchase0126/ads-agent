"""器件模型库回归：身份分层 / 发布纪律 / 检索 / 导入幂等 / 放置频段诚实性。

能失败于旧实现的点：

* 无 device_id/model_id/revision_id 分层时，同型号不同内容会互相覆盖 ——
  这里断言新内容=新 revision、同内容=复用；
* verdict != validated 曾能直接发布 —— 这里断言发布门禁与 candidate 标注；
* 导入每次随机建 cell / 名字被占用时覆盖 —— 这里断言幂等与换名；
* 使用电路曾会静默外推频段 —— 这里断言无验证结论时明确标注未验证。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import os as _os
_os.environ.setdefault("ADS_AGENT_DATA_DIR", tempfile.mkdtemp(prefix="devlib 数据 "))
_os.environ.setdefault("ADS_AGENT_LIBRARY_ROOT", tempfile.mkdtemp(prefix="devlib 库 "))

from _harness import run, eq, ok, ne, raises, contains

import device_library as DL
import device_models as DM
import model_store

LIB_ROOT = None


def _make_record(part="J310-TEST", vendor="Fairchild", validated=False,
                 verdict="validation_incomplete", fit_kind="package_delay",
                 model_kind="jfet_small_signal"):
    """造一条带拟合与建图计划的记录（走真实 build 代码路径）。"""
    import hashlib
    doc_id = hashlib.sha256(f"doc-{part}-{model_kind}".encode()).hexdigest()
    import doc_assets
    doc_bytes = f"%PDF-1.4\\n{part}\\n".encode("ascii")
    doc_id = doc_assets.store(doc_bytes)["sha256"]
    os.makedirs(os.path.join(DM.datasheet_models._root(), "documents"), exist_ok=True)
    DM.datasheet_models._write("documents", doc_id,
                               {"document_id": doc_id, "sha256": doc_id,
                                "filename": f"{part}.pdf"})
    params = {"gm_S": 0.014, "gds_S": 6.5e-5, "Cgs_F": 5e-12, "Cgd_F": 1.8e-12,
              "Cds_F": 5e-14, "Rg_Ohm": 1.0, "Rs_Ohm": 1.0, "Rd_Ohm": 1.0,
              "Lg_H": 5e-10, "Ls_H": 5e-10, "Ld_H": 5e-10, "tau_s": 3e-11}
    record_id = hashlib.sha256(json.dumps(
        {"document_id": doc_id, "part_number": part, "device_kind": "jfet",
         "model_kind": model_kind, "curve_ids": {},
         "configuration": "common_source"}, sort_keys=True).encode()).hexdigest()
    record = {"document_id": doc_id, "part_number": part, "device_kind": "jfet",
              "model_kind": model_kind, "curve_ids": {}, "model_id": record_id,
              "configuration": "common_source", "stage": "fitted",
              "curve_binding": {"z0": {}, "conditions": {"vds_v": 10.0, "id_a": 0.01},
                                "port_order": {}, "evidence": []},
              "parameter_set_id": None, "parameter_review": "semantic_verified",
              "unsupported": list(DM._UNSUPPORTED_ALWAYS),
              "fit": {"selected": fit_kind, "candidates": {fit_kind: {
                  "parameters_fitted_SI": dict(params)}}},
              "build": None}
    DM._save(record)
    if model_kind == "two_port_data":
        raise AssertionError("two_port 分支在专用测试里覆盖")
    import datasheet_curves
    mag_id = hashlib.sha256(f"{record_id}-mag".encode()).hexdigest()
    ph_id = hashlib.sha256(f"{record_id}-phase".encode()).hexdigest()
    for cid, comp, unit in ((mag_id, "mag", "S"), (ph_id, "phase", "deg")):
        datasheet_curves._write(cid, {"curve_id": cid, "document_id": doc_id,
            "sha256": doc_id, "page": 4, "figure": "fig",
            "axes": {"x": {"unit": "MHz", "scale": "linear", "cal_points": []},
                     "y": {"unit": unit, "scale": "linear", "cal_points": []}},
            "matrix_kind": "Y", "component": comp,
            "phase_unit": "deg" if comp == "phase" else None,
            "configuration": "common_source",
            "port_order": {"port1": "g", "port2": "d", "common": "s"},
            "z0": {"ohm": 50.0, "status": "assumed"},
            "conditions": {"vds_v": 10.0, "id_a": 0.01},
            "sampling": {"mode": "values", "count": 3, "start": 100, "stop": 1000},
            "series": [{"label": "a", "path_index": 0, "values": []}],
            "status": "reviewed", "origin": {"method": "test"},
            "reliability": {}})
    record["curve_ids"] = {"Y": {"mag": mag_id, "phase": ph_id}, "S": {}}
    DM._save(record)
    DM.build_device_model({"model_id": record_id, "library": "AI_lib",
                           "cell": "T", "start_hz": 1e8, "stop_hz": 1e9,
                           "step_hz": 5e7})
    record = DM.get_model_record(record_id)
    if validated:
        record["validation"] = {"verdict": "validated",
                                "validation_mode": "schematic",
                                "validated_at": "2026-10-19T00:00:00",
                                "simulation_id": "sim1",
                                "validation_scope_hz": {"S": [1e8, 1e9]}}
        record["stage"] = "validated"
        DM._save(record)
        return DM.get_model_record(record_id)
    record["validation"] = {"verdict": verdict, "validation_mode": "netlist_equivalent",
                            "validated_at": "2026-10-19T00:00:00",
                            "validation_scope_hz": {}}
    DM._save(record)
    return DM.get_model_record(record_id)


def _package(record):
    pkg = DM.build_component_package(record)
    check = DM.verify_component_plan(pkg["component_plan"], record)
    ok(check["ok"], f"元件计划应通过结构核对：{check.get('problems')}")
    return pkg


def test_identity_layers_are_stable_and_separated():
    d1 = DL.device_identity("Fairchild", "J310")
    d2 = DL.device_identity("fairchild", "j 310")     # 大小写/空格归一
    eq(d1, d2, "厂商大小写与型号分隔符归一后应是同一器件")
    ne(d1, DL.device_identity("Fairchild", "BRF106"), "不同型号绝不合并")
    ne(d1, DL.device_identity("NXP", "J310"), "不同厂商绝不合并")
    m1 = DL.model_identity(d1, "jfet_small_signal", "common_source",
                           {"vds_v": 10.0}, 50.0)
    m2 = DL.model_identity(d1, "jfet_small_signal", "common_source",
                           {"vds_v": 10.0}, 50.0)
    eq(m1, m2, "同形态同条件应是同一模型")
    ne(m1, DL.model_identity(d1, "jfet_small_signal", "common_source",
                             {"vds_v": 15.0}, 50.0), "不同偏置不是同一个模型")
    for value in (d1, m1):
        ok(value.startswith(("dev_", "mdl_")), "身份前缀区分层级")
        eq(len(value), 20, "ID 形如 xxx_<16hex>")


def test_archive_is_self_contained_without_source_data_root():
    """全新数据根、源目录不可见时，仅凭修订包也要能取回模型与证据。"""
    record = _make_record(part="J310-ARCH")
    pkg = _package(record)
    root = DL.devices_root({}, "")
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-ARCH",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    state = DL.revision_archive_state(root, manifest)
    eq(state["archive_version"], DL.ARCHIVE_VERSION)
    ok(state["self_contained"], f"新修订必须自带可复验证据：{state['problems']}")
    ok(state["has_model_record"], "必须归档源建模记录")
    ok(state["curve_records"], "必须归档曲线记录")
    ok(state["has_fit"], "必须归档拟合候选")
    eq(state["problems"], [])

    # 把源数据根整体移走（模拟验收数据根不可见），再读修订快照
    import datasheet_curves
    import datasheet_models
    saved_curve = datasheet_curves.get_curve
    saved_read = datasheet_models._read

    def _gone(*_a, **_k):
        raise AssertionError("复验不应再访问源数据根")

    datasheet_curves.get_curve = _gone
    datasheet_models._read = _gone
    try:
        base = os.path.join(root, pub["device_id"], pub["model_id"],
                            "revisions", pub["revision_id"])
        snapshot, problems = DL._load_snapshot(base, manifest)
        eq(problems, [], "快照读取不应访问源数据根")
        eq(snapshot["model_record"]["model_id"], record["model_id"])
        ok(snapshot["curves"], "曲线快照可读")
        eq(manifest["identity"]["source_record_id"], record["model_id"])
    finally:
        datasheet_curves.get_curve = saved_curve
        datasheet_models._read = saved_read

    # 篡改快照文件 → 严格校验必须报出来，不能当"文件完整"
    curve_entry = next(iter((DL._load_snapshot(
        os.path.join(root, pub["device_id"], pub["model_id"], "revisions",
                     pub["revision_id"]), manifest)[0]["curves"]).values()))
    ok(bool(curve_entry), "曲线快照非空")
    target = os.path.join(root, pub["device_id"], pub["model_id"], "revisions",
                          pub["revision_id"], "snapshot", "model_record.json")
    with open(target, "a", encoding="utf-8") as f:
        f.write(" ")
    problems = DL.verify_revision_files(root, manifest)
    ok(any("指纹不符" in p for p in problems), f"篡改后必须报指纹不符：{problems}")


def test_legacy_revision_without_archive_is_reported_incomplete():
    """旧格式（无 archive 版本）修订要如实标不完整，不能默认通过。"""
    record = _make_record(part="J310-LEGACY")
    pkg = _package(record)
    root = DL.devices_root({}, "")
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-LEGACY",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    legacy = dict(manifest)
    legacy.pop("archive", None)
    state = DL.revision_archive_state(root, legacy)
    eq(state["self_contained"], False, "无归档版本不得宣称自包含")
    ok(state["problems"], "必须说明为什么不能独立复验")


def test_publish_refuses_unverified_and_candidate_is_explicit():
    record = _make_record()
    pkg = _package(record)
    root = DL.devices_root({}, "")
    err = raises(DL.DeviceLibraryError, lambda: DL.publish_device_model(
        record, vendor="Fairchild", part_number="J310-TEST",
        component_plan=pkg["component_plan"], deps=pkg["deps"],
        library_root=root))
    contains(str(err), "verdict", "未验证模型不得直接发布")
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-TEST",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    eq(pub["status"], DL.STATUS_CANDIDATE, "未验证只能以 candidate 发布")
    ok(pub["status_reasons"], "candidate 必须带原因标注")
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    ok(manifest["manifest_sha256"], "清单必须有指纹")


def test_same_content_reuses_revision_new_content_new_revision():
    record = _make_record()
    pkg = _package(record)
    root = DL.devices_root({}, "")
    pub1 = DL.publish_device_model(record, vendor="Fairchild",
                                   part_number="J310-TEST",
                                   component_plan=pkg["component_plan"],
                                   deps=pkg["deps"], allow_unverified=True,
                                   library_root=root)
    ok(os.path.isfile(os.path.join(root, pub1["device_id"], pub1["model_id"],
                                   "revisions", pub1["revision_id"],
                                   "manifest.json")), "发布后清单落盘")
    pub2 = DL.publish_device_model(record, vendor="Fairchild",
                                   part_number="J310-TEST",
                                   component_plan=pkg["component_plan"],
                                   deps=pkg["deps"], allow_unverified=True,
                                   library_root=root)
    ok(pub2["reused"], "同内容重复发布应复用")
    eq(pub2["revision_id"], pub1["revision_id"], "同内容 → 同修订")
    # 模型内容变了（拟合参数变了）→ 新修订，旧修订保留
    record["fit"]["candidates"]["package_delay"]["parameters_fitted_SI"]["gm_S"] = 0.020
    DM._save(record)
    record = DM.get_model_record(record["model_id"])
    DM.build_device_model({"model_id": record["model_id"], "library": "AI_lib",
                           "cell": "T", "start_hz": 1e8, "stop_hz": 1e9,
                           "step_hz": 5e7})
    record = DM.get_model_record(record["model_id"])
    pkg3 = _package(record)
    pub3 = DL.publish_device_model(record, vendor="Fairchild",
                                   part_number="J310-TEST",
                                   component_plan=pkg3["component_plan"],
                                   deps=pkg3["deps"], allow_unverified=True,
                                   library_root=root)
    ne(pub3["revision_id"], pub1["revision_id"], "内容不同必须是新修订")
    pkg3 = _package(record)
    pub3 = DL.publish_device_model(record, vendor="Fairchild",
                                   part_number="J310-TEST",
                                   component_plan=pkg3["component_plan"],
                                   deps=pkg3["deps"], allow_unverified=True,
                                   library_root=root)
    ne(pub3["revision_id"], pub1["revision_id"], "内容不同必须是新修订")
    revisions = os.listdir(os.path.join(root, pub3["device_id"],
                                        pub3["model_id"], "revisions"))
    eq(len(revisions), 2, "旧修订保留不被覆盖")


def test_validated_record_publishes_as_published():
    record = _make_record(validated=True)
    pkg = _package(record)
    root = DL.devices_root({}, "")
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-TEST",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], library_root=root)
    eq(pub["status"], DL.STATUS_PUBLISHED, "validated 可发布为 published")
    entries = DL.search_registry(root, {"part": "J310-TEST"})["entries"]
    entry = next(e for e in entries if e["revision_id"] == pub["revision_id"])
    eq(entry["verdict"], "validated")
    eq(entry["validated_band_hz"], [1e8, 1e9], "已验证频段进索引")


def test_search_filters_and_band_coverage_requires_validation():
    record = _make_record(part="BF999-TEST")
    pkg = _package(record)
    root = DL.devices_root({}, "")
    DL.publish_device_model(record, vendor="Siemens", part_number="BF999-TEST",
                            aliases=["BF999"], component_plan=pkg["component_plan"],
                            deps=pkg["deps"], allow_unverified=True,
                            library_root=root)
    eq(DL.search_registry(root, {"query": "bf999"})["total"], 1, "别名可检索")
    eq(DL.search_registry(root, {"part": "BF999"})["total"], 0,
       "型号前缀不模糊匹配（BF999 ≠ BF999-TEST）")
    eq(DL.search_registry(root, {"part": "BF999-TEST"})["total"], 1)
    eq(DL.search_registry(root, {"part": "BF999-TEST", "status": "published"})["total"], 0,
       "candidate 不得计入 published 过滤")
    eq(DL.search_registry(root, {"part": "BF999-TEST", "verdict": "validated"})["total"], 0)
    # 频段过滤：无验证结论的模型不命中（不静默外推）
    eq(DL.search_registry(root, {"part": "BF999-TEST", "band_start_hz": 1e8,
                                 "band_stop_hz": 1e9})["total"],
       0, "无验证结论时频段过滤不得命中")
    eq(DL.search_registry(root, {"part": "BF999-TEST", "capability": "noise"})["total"], 0,
       "点名的 unsupported 能力不得命中")


def test_index_corruption_recovers_from_manifests():
    root = DL.devices_root({}, "")
    ok(DL.search_registry(root, {"part": "J310-TEST"})["total"] >= 1)
    os.remove(os.path.join(root, "index.json"))
    result = DL.search_registry(root, {"part": "J310-TEST"})
    ok(result["total"] >= 1, "索引删除后应自动由清单重建")
    # 索引被写坏（非法 JSON）也要能恢复
    with open(os.path.join(root, "index.json"), "w", encoding="utf-8") as s:
        s.write("{corrupt")
    ok(DL.search_registry(root, {"part": "J310-TEST"})["total"] >= 1,
       "索引损坏后应自动重建")


def _fake_ads(calls):
    def call_tool(name, args):
        calls.append((name, dict(args)))
        if name == "get_workspace_info":
            return {"workspace_open": True, "writable_libraries": ["AI_lib"],
                    "path": "E:/ws"}
        if name == "build_schematic":
            if str(args.get("cell", "")) == "TAKEN_J310":
                return {"error": "cell 已存在"}
            return {"ok": True}
        if name == "create_component_symbol":
            pins = [{"num": 1, "terminal": "s"}, {"num": 2, "terminal": "d"},
                    {"num": 3, "terminal": "g"}]
            return {"ok": True, "symbol_view": "symbol", "symbol_created": True,
                    "pins": pins}
        if name == "run_simulation":
            return {"ok": True, "status": "done", "dataset_path": "E:/ws/x.ds"}
        raise AssertionError(f"unexpected tool {name}")
    return call_tool


def test_import_is_idempotent_and_never_overwrites_taken_name():
    record = _make_record(part="J310-IMP")
    pkg = _package(record)
    root = DL.devices_root({}, "")
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-IMP",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    ws = tempfile.mkdtemp(prefix="devlib ws ")
    calls = []
    fake = _fake_ads(calls)
    imp1 = DL.import_revision(root, ws, manifest, call_tool=fake)
    ok(imp1["ok"], "首次导入成功")
    build_calls = [c for c in calls if c[0] == "build_schematic"]
    eq(len(build_calls), 1, "首次导入恰好建一次 cell")
    imp2 = DL.import_revision(root, ws, manifest, call_tool=fake)
    ok(imp2["reused"], "重复导入应复用")
    build_calls = [c for c in calls if c[0] == "build_schematic"]
    eq(len(build_calls), 1, "重复导入不再建 cell（幂等）")
    eq(imp2["cell"], imp1["cell"])
    # 记录了实际 cell 名与指纹
    imports = DL.load_imports(ws)
    eq(imports["revisions"][pub["revision_id"]]["manifest_sha256"],
       manifest["manifest_sha256"], "导入记录绑定清单指纹")


def test_import_renames_when_cell_name_taken():
    record = _make_record(part="J310-TAKE")
    pkg = _package(record)
    pkg["component_plan"]["cell"] = "TAKEN_J310"
    root = DL.devices_root({}, "")
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-TAKE",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    ws = tempfile.mkdtemp(prefix="devlib ws2 ")
    calls = []
    imp = DL.import_revision(root, ws, manifest, call_tool=_fake_ads(calls))
    ne(imp["cell"], "TAKEN_J310", "名字被占用时必须换名，不得覆盖")
    contains(imp["note"], "TAKEN_J310", "换名必须如实告知")
    eq([c[1]["cell"] for c in calls if c[0] == "build_schematic"][0],
       "TAKEN_J310", "第一次仍先尝试请求名")


def test_place_uses_validated_band_or_labels_unverified():
    root = DL.devices_root({}, "")
    # candidate（无验证结论）
    record = _make_record(part="J310-USE")
    pkg = _package(record)
    pub = DL.publish_device_model(record, vendor="Fairchild",
                                  part_number="J310-USE",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    requested = DL.usage_plan_from(manifest, "LIB", "CELL", [1e8, 1e9])
    eq(requested["requested_band_hz"], [1e8, 1e9])
    eq(requested["validated_band_hz"], None)
    eq(requested["validation_status"], "validation_incomplete")
    contains(requested["_band_note"], "未验证")
    ws = tempfile.mkdtemp(prefix="devlib ws3 ")
    calls = []
    fake = _fake_ads(calls)
    imp = DL.import_revision(root, ws, manifest, call_tool=fake)
    placed = DL.place_component(root, ws, manifest, imp, call_tool=fake)
    contains(placed["band_note"], "未验证", "无验证结论必须标注未验证")
    usage_calls = [c for c in calls if c[0] == "build_schematic"]
    usage = usage_calls[-1][1]
    masters = [i["master"] for i in usage["instances"]]
    ok(any(m.endswith(":" + imp["cell"]) for m in masters),
       "使用电路必须实例化导入的元件")
    ok("ads_simulation:TermG" in masters and "ads_simulation:S_Param" in masters,
       "使用电路含 TermG 与 S 参数控制器")
    # published（已验证）
    record2 = _make_record(part="J310-USE2", validated=True)
    pkg2 = _package(record2)
    pub2 = DL.publish_device_model(record2, vendor="Fairchild",
                                   part_number="J310-USE2",
                                   component_plan=pkg2["component_plan"],
                                   deps=pkg2["deps"], library_root=root)
    manifest2 = DL.load_manifest(root, pub2["device_id"], pub2["model_id"],
                                 pub2["revision_id"])
    ws2 = tempfile.mkdtemp(prefix="devlib ws4 ")
    calls2 = []
    imp2 = DL.import_revision(root, ws2, manifest2, call_tool=_fake_ads(calls2))
    placed2 = DL.place_component(root, ws2, manifest2, imp2,
                                 call_tool=_fake_ads(calls2))
    eq(placed2["band_hz"], [1e8, 1e9], "已验证模型使用其验证频段")
    contains(placed2["band_note"], "已验证", "频段来源要说明")


def test_two_port_package_copies_s2p_dependency():
    import hashlib
    doc_id = hashlib.sha256(b"two-port-doc").hexdigest()
    import doc_assets
    doc_id = doc_assets.store(b"%PDF-1.4\\ntwo-port-doc\\n")["sha256"]
    os.makedirs(os.path.join(DM.datasheet_models._root(), "documents"), exist_ok=True)
    DM.datasheet_models._write("documents", doc_id,
                               {"document_id": doc_id, "sha256": doc_id,
                                "filename": "tp.pdf"})
    record_id = hashlib.sha256(json.dumps(
        {"document_id": doc_id, "part_number": "TP-TEST", "device_kind": "jfet",
         "model_kind": "two_port_data", "curve_ids": {},
         "configuration": "common_source"}, sort_keys=True).encode()).hexdigest()
    model_dir = os.path.join(DM.datasheet_models._root(), "models", record_id)
    os.makedirs(model_dir, exist_ok=True)
    s2p = os.path.join(model_dir, "TP_TEST.s2p")
    with open(s2p, "w", encoding="ascii") as s:
        s.write("# MHz S RI R 50\n100 1 0 0 0 0 0 0 0\n")
    record = {"document_id": doc_id, "part_number": "TP-TEST", "device_kind": "jfet",
              "model_kind": "two_port_data", "curve_ids": {}, "model_id": record_id,
              "configuration": "common_source", "stage": "build_planned",
              "curve_binding": {"z0": {}, "conditions": {}, "port_order": {},
                                "evidence": []},
              "unsupported": list(DM._UNSUPPORTED_ALWAYS),
              "fit": None,
              "build": {"kind": "two_port_data", "z0": 50.0,
                        "s2p_path": s2p,
                        "s2p_sha256": model_store.sha256_file(s2p),
                        "plan": {"instances": [], "connections": []},
                        "parameters_source": {}}}
    DM._save(record)
    record = DM.get_model_record(record_id)
    pkg = DM.build_component_package(record)
    ok(pkg["deps"], "数据二端口必须有 S2P 依赖")
    eq(pkg["deps"][0]["sha256"], model_store.sha256_file(s2p))
    contains(pkg["component_plan"]["instances"][0]["params"]["File"],
             "{MODEL_DIR}", "S2P 路径用占位符，不写开发机绝对路径")
    anchors = pkg["component_plan"]["term_anchors"]
    eq([a["number"] for a in anchors], [1, 2, 3], "数据二端口要暴露 3 个层次引脚")
    root = DL.devices_root({}, "")
    pub = DL.publish_device_model(record, vendor="Test", part_number="TP-TEST",
                                  component_plan=pkg["component_plan"],
                                  deps=pkg["deps"], allow_unverified=True,
                                  library_root=root)
    manifest = DL.load_manifest(root, pub["device_id"], pub["model_id"],
                                pub["revision_id"])
    rev_dir = os.path.join(root, pub["device_id"], pub["model_id"], "revisions",
                           pub["revision_id"])
    stored = os.path.join(rev_dir, "model",
                          os.path.basename(pkg["deps"][0]["relpath"]))
    ok(os.path.isfile(stored), "S2P 依赖应随修订入库")
    eq(model_store.sha256_file(stored), pkg["deps"][0]["sha256"],
       "入库依赖指纹一致")
    problems = DL.verify_revision_files(root, manifest)
    eq(problems, [], "清单指纹核对通过")
    # 篡改依赖文件 → 导入前校验必须拒绝
    with open(stored, "a", encoding="ascii") as s:
        s.write("200 0 0 0 0 0 0 0 0\n")
    ws = tempfile.mkdtemp(prefix="devlib ws5 ")
    err = raises(DL.DeviceLibraryError, lambda: DL.import_revision(
        root, ws, manifest, call_tool=_fake_ads([])))
    contains(str(err), "指纹不符", "依赖被改动时导入必须拒绝")


def test_tool_summary_keeps_model_library_identifiers():
    """模型库工具的结果必须原样带出标识/引脚/能力 —— 折叠掉就无法继续调用。"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "backend"))
    import agent
    entries = [{"part": "SMMBFJ310L", "device_id": "dev_1", "model_id": "mdl_1",
                "revision_id": "rev_1", "status": "candidate",
                "validated_band_hz": [1e8, 1e9]} for _ in range(3)]
    summary = agent._summarize_tool_result(
        "search_model_library", {"ok": True, "models": entries})
    eq(len(summary["models"]), 3, "检索结果条目不能被折叠成占位文本")
    eq(summary["models"][0]["revision_id"], "rev_1")
    detail = agent._summarize_tool_result("get_model_revision", {
        "ok": True,
        "ads_library": {"pins": [{"num": 1, "terminal": "s"},
                                 {"num": 2, "terminal": "d"}]},
        "capabilities": {"unsupported": ["noise", "dc_operating_point"]}})
    eq(len(detail["ads_library"]["pins"]), 2, "引脚表必须可见")
    eq(detail["capabilities"]["unsupported"], ["noise", "dc_operating_point"])


def test_task_state_machine_tracks_record_stages():
    record = _make_record(part="J310-TASK")
    eq(DL.canonical_state(record), "packaged", "建图计划就绪 → packaged")
    build = record.pop("build")
    record["stage"] = "blocked_missing_evidence"
    eq(DL.canonical_state(record), "blocked")
    record["stage"] = "fit_failed_no_candidate_meets_rule"
    eq(DL.canonical_state(record), "failed")
    record["build"] = build
    record["stage"] = "validated"
    eq(DL.canonical_state(record), "validation_completed")
    eq(DL.canonical_state(None), "received")
    eq(DL.canonical_state(None, published=True), "published")
    fresh = _make_record(part="J310-TASK2")
    ok(DL.canonical_state(fresh) in DL.TASK_STATES)


if __name__ == "__main__":
    raise SystemExit(run(globals(), "器件模型库：身份/发布/检索/导入/放置"))
