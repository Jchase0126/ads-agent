"""模型依赖指纹与评估门禁回归（2026-10-09，不需要 ADS）。

针对"模型被换掉但网表一个字没变"这类**必须保守**的情形，以及"模型有效
频段覆盖不到目标频段却报达标"这类**必须拦住**的情形：

一、指纹扫描（addon/ads_agent/model_deps.py）
    * 无外部模型依赖 -> none（网表指纹已覆盖全部输入）
    * 同路径替换内容 -> 哈希变化 -> 不复用
    * 工作区相对路径解析（不依赖进程 cwd）
    * 相对路径找不到 / VAR 表达式求值不了 -> incomplete（保守）
    * 模型文件消失 -> missing
    * 层次子图里引用模型（顶层只有一个符号）
    * 超过四端口的 .s8p / .s12p 与 .ts
    * 本次实际仿真网表里出现的路径（实例参数之外的补充证据）
    * Design Kit 套件级保守指纹：套件里改一个文件 -> 指纹变化
    * 大文件分块哈希 = 整体哈希
    * package_id 从导入目录反推

二、复用门禁（backend/model_gate.py）
    * 四态各自的复用结论；旧结果只有 model_fingerprint 字典时的兼容；
      当前拿不到证据 -> 不复用

三、覆盖门禁（backend/model_gate.py + design_metrics）
    * 频段覆盖 -> 放行；超出覆盖范围 -> pass=None（不报达标）
    * 条件未知（索引里查不到）-> pass=None
    * 参考阻抗不一致 / 偏压不匹配 -> pass=None

四、有界优化（backend/design_service.run_optimization）
    * 未经用户要求 -> 拒绝；离散选型不给 apply_candidate -> 拒绝
    * 迭代上限 / 取消 / 失败终止 / 最佳已验证结果

运行::

    python tests/test_model_deps_gate.py
"""

import hashlib
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

BACKEND = add_path("backend")
ADDON = add_path("addon", "ads_agent")

import model_deps  # noqa: E402  (ADS 端纯逻辑模块，只依赖标准库)
import model_gate  # noqa: E402
import design_job as dj  # noqa: E402
import design_metrics as dm  # noqa: E402
import design_service as dsvc  # noqa: E402


def _write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _s2p(path: str, tag: str = "v1") -> str:
    """写一个最小可用的 Touchstone 文件（内容不同 = 模型被换掉）。"""
    return _write(path, f"! model {tag}\n# GHz S RI R 50\n"
                        f"1.0 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8\n"
                        f"6.0 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8\n")


def _inst(name="X1", master="S2P", **params):
    return {"name": name, "master": master, "params": dict(params)}


def _scan(instances, netlist="", ws="", vars=None, **kw):
    plan = model_deps.collect_plan(instances, netlist_text=netlist,
                                   workspace=ws, var_table=vars, **kw)
    return model_deps.finalize(plan)


# ===========================================================================
# 一、指纹扫描
# ===========================================================================

def test_no_external_model_dependency_is_none():
    root = tempfile.mkdtemp(prefix="md_none_")
    try:
        ws = os.path.join(root, "wrk")
        os.makedirs(ws)
        ev = _scan([_inst("R1", "R", R="50 Ohm"), _inst("TERM1", "TermG", Z="50")],
                   ws=ws)
        eq(ev["state"], "none", "没有模型文件时必须是 none（不能假装完整）")
        eq(ev["fingerprint"], "")
        eq(ev["n_deps"], 0)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_same_path_content_replaced_changes_hash():
    """同一路径换内容：网表不会变，只有内容哈希能发现 —— 这是核心场景。"""
    root = tempfile.mkdtemp(prefix="md_replace_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "models", "GRM155.s2p"), "v1")
        inst = _inst("X1", "S2P", File=path)
        ev1 = _scan([inst], ws=ws)
        eq(ev1["state"], "complete")
        eq(ev1["n_deps"], 1)
        sha1 = ev1["deps"][0]["sha256"]

        _s2p(path, "v2 原厂新版")
        ev2 = _scan([inst], ws=ws)
        eq(ev2["state"], "complete")
        ne_sha = ev2["deps"][0]["sha256"]
        ok(sha1 != ne_sha, "内容换了哈希必须变")
        verdict = model_gate.compare_model_evidence(ev1, ev2)
        eq(verdict["reuse"], False, "模型内容变了就不能复用旧结果")
        eq(verdict["reason"], "model_content_changed")
        contains(verdict["note"], "模型内容已变化")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_unchanged_model_allows_reuse():
    root = tempfile.mkdtemp(prefix="md_same_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "models", "GRM155.s2p"))
        inst = _inst("X1", "S2P", File=path)
        ev1 = _scan([inst], ws=ws)
        ev2 = _scan([inst], ws=ws)
        verdict = model_gate.compare_model_evidence(ev1, ev2)
        eq(verdict["reuse"], True)
        eq(verdict["reason"], "model_deps_match")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_relative_path_resolved_against_workspace_not_cwd():
    """相对路径按工作区解析，**不**依赖进程当前目录（后端 cwd 不是工作区）。"""
    root = tempfile.mkdtemp(prefix="md_rel_")
    try:
        ws = os.path.join(root, "wrk")
        _s2p(os.path.join(ws, "models", "a.s2p"))
        ev = _scan([_inst("X1", "S2P", File="models/a.s2p")], ws=ws)
        eq(ev["state"], "complete")
        eq(ev["deps"][0]["resolved_by"], "workspace_relative",
           "相对路径必须按工作区根解析")
        ok(ev["deps"][0]["path"].endswith(os.path.join("models", "a.s2p")))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_relative_path_unresolved_is_incomplete():
    root = tempfile.mkdtemp(prefix="md_relmiss_")
    try:
        ws = os.path.join(root, "wrk")
        os.makedirs(ws)
        ev = _scan([_inst("X1", "S2P", File="models/nope.s2p")], ws=ws)
        eq(ev["state"], "incomplete")
        eq(len(ev["unresolved"]), 1)
        eq(ev["unresolved"][0]["reason"], "relative_path_unresolved")
        ok(ev["unresolved"][0]["tried"], "要把试过的候选根写出来供人排查")
        verdict = model_gate.compare_model_evidence(ev, ev)
        eq(verdict["reuse"], False, "依赖解析不完整 -> 保守不复用")
        eq(verdict["reason"], "model_deps_unresolved")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_model_file_disappears_is_missing():
    root = tempfile.mkdtemp(prefix="md_gone_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "models", "a.s2p"))
        inst = _inst("X1", "S2P", File=path)
        ev1 = _scan([inst], ws=ws)
        eq(ev1["state"], "complete")
        os.remove(path)
        ev2 = _scan([inst], ws=ws)
        eq(ev2["state"], "missing", "文件不见了必须报 missing，不是 none")
        eq(len(ev2["missing"]), 1)
        verdict = model_gate.compare_model_evidence(ev1, ev2)
        eq(verdict["reuse"], False)
        eq(verdict["reason"], "model_dependency_missing")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_var_expression_resolved():
    root = tempfile.mkdtemp(prefix="md_var_")
    try:
        ws = os.path.join(root, "wrk")
        _s2p(os.path.join(ws, "models", "x.s2p"))
        _s2p(os.path.join(ws, "models", "y.s2p"))
        ev = _scan([_inst("X1", "S2P", File="$MDIR/x.s2p"),
                    _inst("X2", "S2P", File="MDIR + '/y.s2p'")],
                   ws=ws, vars={"MDIR": '"models"'})
        eq(ev["state"], "complete", f"VAR 表达式应能求值: {ev.get('unresolved')}")
        eq(ev["n_deps"], 2)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_unknown_var_marks_unresolved():
    root = tempfile.mkdtemp(prefix="md_varbad_")
    try:
        ws = os.path.join(root, "wrk")
        os.makedirs(ws)
        ev = _scan([_inst("X1", "S2P", File="$NOPE/x.s2p")], ws=ws)
        eq(ev["state"], "incomplete")
        eq(ev["unresolved"][0]["reason"], "var_expression_unresolved")
        contains(str(ev["unresolved"][0].get("unresolved_vars")), "NOPE")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_hierarchical_reference_is_followed():
    """顶层只有一个符号、模型藏在子图里 —— 只看顶层会漏掉。"""
    root = tempfile.mkdtemp(prefix="md_hier_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "models", "q.s2p"))

        def descend(cell, lib):
            if cell != "AmpCell":
                return None
            return {"library": lib,
                    "instances": [_inst("Q1", "S2P", File=path)],
                    "vars": {}}

        ev = _scan([_inst("A1", "AmpCell")], ws=ws, descend=descend)
        eq(ev["state"], "complete")
        eq(ev["n_deps"], 1)
        contains(str(ev["deps"][0]["origins"]), "hierarchy")
        ok(any(h["cell"] == "AmpCell" for h in ev["hierarchy"]["expanded"]))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_hierarchy_open_failure_is_recorded_not_hidden():
    def descend(cell, lib):
        raise RuntimeError("子图打不开")

    ev = _scan([_inst("A1", "AmpCell")], ws="", descend=descend)
    eq(len(ev["hierarchy"]["failed"]), 1)
    contains(ev["hierarchy"]["failed"][0]["reason"], "子图打不开")


def test_more_than_four_ports_and_ts():
    """.s8p / .s12p / .ts 都要认：任意端口数，不只有 1~4。"""
    root = tempfile.mkdtemp(prefix="md_ports_")
    try:
        ws = os.path.join(root, "wrk")
        p8 = _write(os.path.join(ws, "m", "a.s8p"), "# GHz S RI R 50\n")
        p12 = _write(os.path.join(ws, "m", "b.s12p"), "# GHz S RI R 50\n")
        ts = _write(os.path.join(ws, "m", "c.ts"), "# GHz S RI R 50\n")
        ev = _scan([_inst("X1", "Snp", File=p8),
                    _inst("X2", "Snp", File=p12),
                    _inst("X3", "Snp", File=ts)], ws=ws)
        eq(ev["state"], "complete")
        eq(ev["n_deps"], 3)
        ports = {os.path.basename(d["path"]): d["ports"] for d in ev["deps"]}
        eq(ports["a.s8p"], 8)
        eq(ports["b.s12p"], 12)
        eq(ports["c.ts"], None, ".ts 的端口数无法从文件名得出 -> 留 None，不猜")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_netlist_scan_supplements_instance_params():
    """本次**实际仿真网表**里出现的路径也要算（层次展开后才有的引用）。"""
    root = tempfile.mkdtemp(prefix="md_net_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "models", "inside.s2p"))
        netlist = f'Snp:X1 1 2 File="{path}"\n'
        ev = _scan([_inst("A1", "AmpCell")], netlist=netlist, ws=ws)
        eq(ev["state"], "complete")
        eq(ev["n_deps"], 1)
        contains(str(ev["deps"][0]["origins"]), "netlist")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_design_kit_suite_fingerprint_detects_change():
    """套件级保守指纹：套件里改一个文件 -> 指纹变 -> 不复用。"""
    root = tempfile.mkdtemp(prefix="md_kit_")
    try:
        ws = os.path.join(root, "wrk")
        kit = os.path.join(root, "TDK_Kit")
        lib_dir = os.path.join(kit, "TDK_Lib")
        _write(os.path.join(kit, "lib.defs"), "DEFINE TDK_Lib TDK_Lib\n")
        _write(os.path.join(lib_dir, "ads.lib"), "lib\n")
        _write(os.path.join(lib_dir, "a.atf"), "model a\n")
        _write(os.path.join(lib_dir, "b.atf"), "model b\n")

        kwargs = dict(library_roots={"TDK_Lib": kit}, kit_libraries=["TDK_Lib"])
        ev1 = _scan([_inst("C1", "GRM155")], ws=ws, library="TDK_Lib", **kwargs)
        eq(ev1["state"], "incomplete", "套件级指纹是保守但不精确的 -> incomplete")
        eq(len(ev1["kits"]), 1)
        fp1 = ev1["kits"][0]["fingerprint"]
        ok(ev1["kits"][0]["cost_note"], "必须写明成本与范围")
        ok(ev1["kits"][0]["files_total"] >= 3)

        ev2 = _scan([_inst("C1", "GRM155")], ws=ws, library="TDK_Lib", **kwargs)
        eq(ev2["kits"][0]["fingerprint"], fp1, "没动套件时指纹必须稳定")
        comparison = model_gate.compare_model_evidence(ev1, ev2)
        eq(comparison["reuse"], False,
           "套件指纹一致只能确认文件未变；依赖证据不完整时仍不得复用")
        eq(comparison["reason"], "model_deps_unresolved")

        _write(os.path.join(lib_dir, "b.atf"), "model b (原厂改过)\n")
        ev3 = _scan([_inst("C1", "GRM155")], ws=ws, library="TDK_Lib", **kwargs)
        ok(ev3["kits"][0]["fingerprint"] != fp1, "套件内容变了必须发现")
        eq(model_gate.compare_model_evidence(ev1, ev3)["reuse"], False)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_chunked_hash_equals_whole_file_hash():
    root = tempfile.mkdtemp(prefix="md_big_")
    try:
        path = os.path.join(root, "big.s2p")
        chunk = "1.0 " + " ".join(["0.1"] * 8) + "\n"
        n_chunks = 120000               # 每块约 36 字节 -> 约 4.2 MB
        with open(path, "w", encoding="utf-8") as f:
            f.write("# GHz S RI R 50\n")
            for _ in range(n_chunks):   # 远超一个 chunk（默认 1 MB）
                f.write(chunk)
        with open(path, "rb") as f:
            whole = hashlib.sha256(f.read()).hexdigest()
        eq(model_deps.sha256_file(path)["sha256"], whole, "分块哈希必须等于整体哈希")
        ok(model_deps.sha256_file(path)["size"] > 2 * 1024 * 1024,
           f"样本要真的超过一个分块才有意义（实际 "
           f"{model_deps.sha256_file(path)['size']} 字节）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_package_id_recognized_from_import_dir():
    root = tempfile.mkdtemp(prefix="md_pkg_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "ads_agent_models", "extracted",
                                 "pkg_tdk_56", "models", "GRM155.s2p"))
        ev = _scan([_inst("X1", "S2P", File=path)], ws=ws)
        eq(ev["deps"][0]["package_id"], "pkg_tdk_56", "从导入目录反推 package_id")
        eq(ev["deps"][0]["variant"], "GRM155", "变体记号（含偏置/温度写法）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===========================================================================
# 二、复用门禁
# ===========================================================================

def test_old_result_without_evidence_is_not_reused():
    root = tempfile.mkdtemp(prefix="mg_old_")
    try:
        ws = os.path.join(root, "wrk")
        path = _s2p(os.path.join(ws, "a.s2p"))
        cur = _scan([_inst("X1", "S2P", File=path)], ws=ws)
        verdict = model_gate.compare_model_evidence(None, cur)
        eq(verdict["reuse"], False, "旧结果没有模型记录 -> 不复用")
        eq(verdict["reason"], "old_result_has_no_model_evidence")
        contains(verdict["note"], "重新仿真")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_old_legacy_hash_map_is_honored():
    """旧结果只有 {路径: 哈希} 字典时也按完整证据处理（兼容，不多跑）。"""
    legacy = {"/models/a.s2p": "deadbeef"}
    cur = {"state": "complete", "fingerprint": "f1", "n_deps": 1,
           "deps": [{"path": "/models/a.s2p", "sha256": "deadbeef"}],
           "unresolved": [], "missing": [], "kits": [],
           "model_fingerprint": legacy}
    eq(model_gate.compare_model_evidence(legacy, cur)["reuse"], True)
    changed = dict(cur)
    changed["deps"] = [{"path": "/models/a.s2p", "sha256": "0000"}]
    eq(model_gate.compare_model_evidence(legacy, changed)["reuse"], False)


def test_current_evidence_unavailable_is_not_reused():
    old = {"state": "complete", "fingerprint": "f", "n_deps": 1,
           "deps": [{"path": "/a.s2p", "sha256": "x"}], "unresolved": [],
           "missing": [], "kits": [], "model_fingerprint": {}}
    verdict = model_gate.compare_model_evidence(old, None)
    eq(verdict["reuse"], False)
    eq(verdict["reason"], "model_evidence_unavailable")


def test_none_state_needs_no_model_check():
    cur = {"state": "none", "fingerprint": "", "n_deps": 0, "deps": [],
           "unresolved": [], "missing": [], "kits": [], "model_fingerprint": {}}
    eq(model_gate.compare_model_evidence(None, cur)["reuse"], True,
       "没有外部模型依赖时，网表指纹已覆盖全部输入")


# ===========================================================================
# 三、覆盖门禁（频段 / 阻抗 / 偏压 / 温度）
# ===========================================================================

BAND = {"start_hz": 2.3e9, "stop_hz": 2.5e9}


def _cond(**kw):
    base = {"source": "model_index:pkg_1", "available": True,
            "part": "BFP181", "variant": "BFP181_VCE_2.0V_IC_10mA",
            "package_id": "pkg_1", "ports": 2,
            "freq_start_hz": 1e7, "freq_stop_hz": 6e9,
            "reference_impedance_ohm": 50.0, "bias": {"VCE": "2V", "IC": "10mA"}}
    base.update(kw)
    return base


def test_band_covered_passes_gate():
    gate = model_gate.evaluate_coverage(BAND, [_cond()], reference_ohm=50.0)
    eq(gate["state"], "covered")
    eq(gate["block"], False)


def test_band_out_of_model_range_blocks():
    """目标 2.3–2.5 GHz，模型只到 1.8 GHz —— 不能据外推曲线报达标。"""
    gate = model_gate.evaluate_coverage(BAND,
                                        [_cond(freq_stop_hz=1.8e9)],
                                        reference_ohm=50.0)
    eq(gate["state"], "out_of_range")
    eq(gate["block"], True)
    contains(gate["message"], "不覆盖目标频段")


def test_unknown_model_conditions_block():
    gate = model_gate.evaluate_coverage(BAND, [{
        "source": "unresolved", "available": False, "variant": "x",
        "freq_start_hz": None, "freq_stop_hz": None,
        "reason": "索引里没有这个文件"}])
    eq(gate["state"], "unknown")
    eq(gate["block"], True)
    contains(gate["message"], "需核实")


def test_impedance_mismatch_blocks():
    gate = model_gate.evaluate_coverage(BAND, [_cond(reference_impedance_ohm=75.0)],
                                        reference_ohm=50.0)
    eq(gate["state"], "impedance_mismatch")
    eq(gate["block"], True)


def test_bias_mismatch_blocks():
    gate = model_gate.evaluate_coverage(
        BAND, [_cond()], reference_ohm=50.0,
        required={"bias": {"VCE": "5V"}})
    eq(gate["state"], "condition_mismatch")
    eq(gate["block"], True)


def test_matching_bias_passes():
    gate = model_gate.evaluate_coverage(
        BAND, [_cond()], reference_ohm=50.0,
        required={"bias": {"VCE": "2V", "IC": "10mA"}})
    eq(gate["block"], False)


def _trace(x=None, y=None):
    return {"x": x or [2.3e9, 2.4e9, 2.5e9], "y": y or [16.0, 18.0, 16.0],
            "x_unit": "Hz", "y_unit": "dB", "quality": {}}


def test_metrics_blocked_by_model_gate():
    """门禁必须真的改判定：pass 不能是 True。"""
    traces = {"dB(S(2,1))": _trace()}
    spec = [{"kind": "min_in_band", "expr": "dB(S(2,1))", "target": 15.0,
             "unit": "dB"}]
    base = dm.evaluate(traces, spec, BAND)
    eq(base["results"][0]["pass"], True, "门禁没开时本来就达标（16 >= 15）")

    gate = model_gate.evaluate_coverage(
        BAND, [_cond(freq_stop_hz=1.8e9)], reference_ohm=50.0)
    gated = dm.evaluate(traces, spec, BAND, options={"model_gate": gate})
    eq(gated["results"][0]["pass"], None, "超出模型有效频段 -> 不能判达标")
    eq(gated["results"][0]["blocked_reason"], "model_out_of_range")
    eq(gated["summary"]["verdict"], "unknown")
    contains(gated["results"][0]["note"], "不覆盖目标频段")


def test_no_model_means_no_gate():
    gate = model_gate.evaluate_coverage(BAND, [])
    eq(gate["block"], False)
    gated = dm.evaluate({"dB(S(2,1))": _trace()},
                        [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                          "target": 15.0, "unit": "dB"}],
                        BAND, options={"model_gate": gate})
    eq(gated["results"][0]["pass"], True, "没有模型参与时不该被拦")


def test_model_conditions_persist_in_job():
    """端到端（替身 ADS）：仿真结果带 model_deps -> 门禁进入 job.sim。"""
    root = tempfile.mkdtemp(prefix="mg_job_")
    try:
        ws = tempfile.mkdtemp(prefix="mg_ws_")
        path = _s2p(os.path.join(ws, "models", "a.s2p"))
        deps = _scan([_inst("X1", "S2P", File=path)], ws=ws)
        fake = _FakeTools(ws, sim_extra={"model_deps": deps,
                                         "model_fingerprint": deps["model_fingerprint"]})
        spec = _spec()
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, spec, root))
        eq((job.sim or {}).get("model_deps", {}).get("state"), "complete")
        ok((job.sim or {}).get("model_conditions") is not None)
        gate = (job.sim or {}).get("model_gate") or {}
        # 没有导入过的模型包 -> 索引查不到 -> 条件未知 -> 不报达标
        eq(gate.get("state"), "unknown")
        eq(gate.get("block"), True)
        for m in job.metrics:
            eq(m.get("pass"), None, "条件未知时指标必须判 unknown")
            eq(m.get("blocked_reason"), "model_unknown")
        loaded = dj.load_job(root, job.job_id)
        eq((loaded.sim or {}).get("model_deps", {}).get("state"), "complete",
           "模型证据必须随任务持久化")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===========================================================================
# 四、有界优化
# ===========================================================================

def _spec(**over):
    spec = {
        "design": {"library": "AI_lib", "cell": "Amp"},
        "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                     "target": 15.0, "unit": "dB"}],
        "band": {"start": 2.3, "stop": 2.5, "unit": "GHz"},
        "simulate": True,
    }
    spec.update(over)
    return spec


class _FakeTools:
    """tools_mod 替身：get_workspace_info / run_simulation / read_traces /
    set_design_variables。"""

    AdsToolError = Exception

    def __init__(self, workspace, sim_result=None, sim_extra=None, calls=None,
                 trace_y=None):
        self.workspace = workspace
        self.sim_extra = dict(sim_extra or {})
        self.calls = calls if calls is not None else []
        self._sim_result = sim_result
        # 曲线纵轴可配：默认 16/18/16 dB 相对15 dB 目标是**达标**的。
        # 想验"没达标就不许说达标"必须把值压到目标以下，否则断言的是
        # 另一条路径（达标轮次应指过去），那样的测试是自欺。
        self._trace_y = list(trace_y) if trace_y else [16.0, 18.0, 16.0]

    def call(self, cfg, name, args, job_id=""):
        self.calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "get_workspace_info":
            return {"workspace_open": True, "path": self.workspace,
                    "libraries": ["AI_lib"]}
        if name == "run_simulation":
            out = {"status": "done",
                   "dataset_path": f"{self.workspace}/sim/Amp.ds",
                   "output_dir": f"{self.workspace}/sim",
                   "netlist_path": f"{self.workspace}/sim/netlist",
                   "design_version": {"netlist_sha": "sha_AAA"},
                   "workspace": {"name": "wrk", "path": self.workspace}}
            out.update(self.sim_extra)
            return out
        if name == "read_traces":
            return {"variables": ["dB(S(2,1))"],
                    "traces": {"dB(S(2,1))": {
                        "x": [2.3e9, 2.4e9, 2.5e9], "y": list(self._trace_y),
                        "x_unit": "Hz", "y_unit": "dB",
                        "x_name": "freq", "y_name": "dB(S(2,1))"}}}
        if name == "set_design_variables":
            return {"ok": True}
        raise AssertionError(f"未预期的工具调用 {name}")

    @staticmethod
    def is_local(name):
        return False


def _with_fake_tools(fake, fn):
    real = dsvc.tools_mod
    dsvc.tools_mod = fake
    try:
        return fn()
    finally:
        dsvc.tools_mod = real


def test_optimization_requires_user_request():
    root = tempfile.mkdtemp(prefix="opt_guard_")
    try:
        ws = tempfile.mkdtemp(prefix="opt_ws_")
        fake = _FakeTools(ws)
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, _spec(), root))
        try:
            _with_fake_tools(fake, lambda: dsvc.run_optimization(
                {}, root, job.job_id, {"kind": "continuous",
                                       "candidates": [{"variables": {"L": "2nH"}}]}))
        except dsvc.DesignError as e:
            contains(str(e), "明确要求")
        else:
            raise AssertionError("未经用户要求就跑优化，必须拒绝")
        eq(dj.load_job(root, job.job_id).optimization, {},
           "被拒绝的优化不能留下记录（导入模型绝不触发优化）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_discrete_selection_requires_real_swap():
    root = tempfile.mkdtemp(prefix="opt_disc_")
    try:
        ws = tempfile.mkdtemp(prefix="opt_ws_")
        fake = _FakeTools(ws)
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, _spec(), root))
        try:
            _with_fake_tools(fake, lambda: dsvc.run_optimization(
                {}, root, job.job_id,
                {"kind": "discrete", "requested_by_user": True,
                 "candidates": [{"part": "BFP181"}]}))
        except dsvc.DesignError as e:
            contains(str(e), "apply_candidate")
        else:
            raise AssertionError("离散选型不给替换回调必须拒绝（否则是假优化）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_optimization_is_bounded_and_records_best():
    root = tempfile.mkdtemp(prefix="opt_run_")
    try:
        ws = tempfile.mkdtemp(prefix="opt_ws_")
        calls = []
        # 全部轮次都不达标（目标 15 dB，实测最高 12 dB）
        fake = _FakeTools(ws, calls=calls, trace_y=[11.0, 12.0, 11.5])
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, _spec(), root))
        plan = {"kind": "continuous", "requested_by_user": True,
                "max_iterations": 2, "stop_on_first_pass": False,
                "candidates": [{"label": f"L={i}nH", "variables": {"L": f"{i} nH"}}
                               for i in range(1, 6)]}
        job = _with_fake_tools(fake, lambda: dsvc.run_optimization(
            {}, root, job.job_id, plan))
        opt = job.optimization
        eq(opt.get("kind"), "continuous")
        eq(len(opt.get("iterations") or []), 2, "必须受迭代上限约束")
        contains(opt.get("stopped_reason") or "", "迭代上限")
        best = opt.get("best") or {}
        ok(best.get("evidence", {}).get("dataset_path"), "最佳结果必须带仿真证据")
        ok(best.get("verified_pass") is False, "没达标就不能说达标")
        contains(opt.get("best_text") or "", "未达标")
        variable_calls = [c for c in calls if c[0] == "set_design_variables"]
        eq(len(variable_calls), 3,
           "两轮候选赋值后应恢复第一轮最佳候选；恢复不属于新的优化迭代")
        eq(variable_calls[-1][1]["values"], best["candidate"]["variables"],
           "结束时电路参数必须回到最佳已验证候选")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_optimization_cancel_keeps_done_rounds():
    root = tempfile.mkdtemp(prefix="opt_cancel_")
    try:
        ws = tempfile.mkdtemp(prefix="opt_ws_")
        fake = _FakeTools(ws)
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, _spec(), root))
        cancel = threading.Event()
        cancel.set()
        job = _with_fake_tools(fake, lambda: dsvc.run_optimization(
            {}, root, job.job_id,
            {"kind": "continuous", "requested_by_user": True,
             "candidates": [{"label": "L=1nH", "variables": {"L": "1 nH"}}]},
            cancel_event=cancel))
        contains(job.optimization.get("stopped_reason") or "", "取消")
        eq(job.optimization.get("status"), "cancelled")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run(globals(), "模型依赖指纹与评估门禁"))
