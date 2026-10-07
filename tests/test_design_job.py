"""DesignJob 状态机 / 持久化 / 编排服务的测试（不需要 ADS，纯标准库）。

覆盖第四轮需求里的「结构化 DesignJob」与「中途失败保留产物」：

* 状态流转是显式的：非法流转必须被拒绝，不能悄悄把 failed 改成 done；
* ``fail()`` 只改状态与错误信息 —— 原理图引用、产物路径、迭代记录全部保留；
* 判定（verdict）只来自评估器的结果，模型塞进来的 actual/pass 一律无效；
* 持久化是原子的，且能完整往返（含曲线数据、迭代记录）；
* 编排服务在"仿真失败 / 没产出数据集 / 没有数据集路径 / 任务文件丢失"
  各条失败路径上都要给出可操作的错误，且不编造任何实测值。

运行::

    python tests/test_design_job.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, raises, run  # noqa: E402

BACKEND = add_path("backend")

import design_job as dj  # noqa: E402
import design_service as svc  # noqa: E402
import tools as tools_mod  # noqa: E402

_TMPDIRS: list = []
_ORIGINAL_CALL = tools_mod.call


def _root() -> str:
    d = tempfile.mkdtemp(prefix="ads_agent_job_")
    _TMPDIRS.append(d)
    return d


def _teardown():
    tools_mod.call = _ORIGINAL_CALL
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

def test_legal_forward_transitions():
    job = dj.DesignJob(requirement="2.4 GHz 放大器")
    eq(job.stage, dj.STAGE_DRAFT)
    job.transition(dj.STAGE_PLANNED)
    job.transition(dj.STAGE_SIMULATING)
    job.transition(dj.STAGE_EVALUATED)
    job.transition(dj.STAGE_DONE)
    eq(job.stage, dj.STAGE_DONE)
    ok(job.is_finished())


def test_illegal_transitions_are_rejected():
    job = dj.DesignJob()
    raises(dj.IllegalTransition, lambda: job.transition(dj.STAGE_DONE),
           "draft 不能直接跳到 done")
    raises(dj.IllegalTransition, lambda: job.transition("不存在的阶段"))
    eq(job.stage, dj.STAGE_DRAFT, "被拒绝的流转不应改动状态")

    job.transition(dj.STAGE_PLANNED)
    job.transition(dj.STAGE_SIMULATING)
    raises(dj.IllegalTransition, lambda: job.transition(dj.STAGE_PLANNED),
           "simulating 不能直接回 planned")


def test_iteration_loop_is_a_legal_path():
    """未达标 -> 改参数 -> 再仿真，必须是一条正常路径而不是硬改状态。"""
    job = dj.DesignJob()
    job.transition(dj.STAGE_PLANNED)
    job.transition(dj.STAGE_BUILDING)
    job.transition(dj.STAGE_SIMULATING)
    job.transition(dj.STAGE_EVALUATED)
    job.transition(dj.STAGE_BUILDING)          # 调参
    job.transition(dj.STAGE_SIMULATING)        # 复测
    job.transition(dj.STAGE_EVALUATED)
    job.transition(dj.STAGE_DONE)
    eq(job.stage, dj.STAGE_DONE)


def test_failure_keeps_design_artifacts_and_iterations():
    """中途失败必须保留已经生成的原理图引用与仿真结果。"""
    job = dj.DesignJob(requirement="2.4 GHz 放大器")
    job.transition(dj.STAGE_PLANNED)
    job.set_design(library="AI_lib", cell="Amp24G", view="schematic")
    job.set_artifacts(output_dir="/ws/ads_agent_sim/Amp24G_1",
                      netlist_path="/ws/ads_agent_sim/Amp24G_1/netlist.ckt")
    job.add_iteration({"sim_status": "done"})
    job.transition(dj.STAGE_SIMULATING)

    job.fail("仿真失败：hpeesofsim 退出码 1")

    eq(job.stage, dj.STAGE_FAILED)
    contains(job.error, "hpeesofsim")
    eq(job.design_ref(), "AI_lib:Amp24G:schematic", "失败不该丢掉设计引用")
    contains(job.artifacts.get("output_dir", ""), "Amp24G_1", "失败不该丢掉输出目录")
    contains(job.artifacts.get("netlist_path", ""), "netlist.ckt")
    eq(len(job.iterations), 1, "失败不该丢掉迭代记录")


def test_fail_is_idempotent():
    job = dj.DesignJob()
    job.fail("第一次")
    job.fail("第二次")
    eq(job.stage, dj.STAGE_FAILED)
    eq(job.error, "第二次")


def test_artifacts_merge_instead_of_replace():
    job = dj.DesignJob()
    job.set_artifacts(output_dir="/a")
    job.set_artifacts(dataset_path="/a/x.ds")
    eq(job.artifacts.get("output_dir"), "/a")
    eq(job.artifacts.get("dataset_path"), "/a/x.ds")


def test_iterations_are_numbered():
    job = dj.DesignJob()
    job.add_iteration({})
    job.add_iteration({})
    eq([it["n"] for it in job.iterations], [1, 2])


# ---------------------------------------------------------------------------
# 判定（只看评估器）
# ---------------------------------------------------------------------------

def test_verdict_comes_only_from_metric_results():
    job = dj.DesignJob()
    eq(job.verdict(), "unknown", "没有指标时是 unknown，不是 fail")
    job.set_metrics([{"pass": True}, {"pass": True}])
    eq(job.verdict(), "pass")
    job.set_metrics([{"pass": True}, {"pass": False}])
    eq(job.verdict(), "partial")
    job.set_metrics([{"pass": False}, {"pass": None}])
    eq(job.verdict(), "fail", "有明确不达标项就是 fail")
    job.set_metrics([{"pass": None}])
    eq(job.verdict(), "unknown", "一项都判不出来时不能说未达标")


def test_summary_counts():
    job = dj.DesignJob()
    job.set_metrics([{"pass": True}, {"pass": False}, {"pass": None}])
    s = job.summary()
    eq((s["n_metrics"], s["n_passed"], s["n_failed"], s["n_unknown"]), (3, 1, 1, 1))
    eq(s["verdict"], "partial")


def test_design_ref_and_label():
    job = dj.DesignJob()
    eq(job.design_ref(), "?:?:schematic")
    eq(job.design_label(), "(未指定设计)")
    job.set_design(library="AI_lib", cell="Amp", view="schematic")
    eq(job.design_ref(), "AI_lib:Amp:schematic")
    contains(job.design_label(), "AI_lib")


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def test_save_and_load_round_trip():
    root = _root()
    job = dj.DesignJob(requirement="设计 2.4 GHz 放大器", title="Amp24G 评估")
    job.transition(dj.STAGE_PLANNED)
    job.set_design(library="AI_lib", cell="Amp24G")
    job.set_metrics([{"id": "g", "pass": False, "actual": 14.33, "at": "2.3 GHz"}])
    job.add_iteration({"sim_status": "done", "summary": {"verdict": "fail"}})
    job.set_artifacts(dataset_path="/ws/a.ds",
                      traces={"dB(S(2,1))": {"x": [1.0, 2.0], "y": [3.0, 4.0],
                                             "y_unit": "dB", "n_points": 2}})
    path = dj.save_job(root, job)
    ok(os.path.exists(path))

    back = dj.load_job(root, job.job_id)
    ok(back is not None, "应能读回来")
    eq(back.job_id, job.job_id)
    eq(back.requirement, job.requirement)
    eq(back.design_ref(), "AI_lib:Amp24G:schematic")
    eq(back.verdict(), "fail")
    eq(len(back.iterations), 1)
    eq(back.artifacts["traces"]["dB(S(2,1))"]["x"], [1.0, 2.0])
    eq(back.to_dict()["schema_version"], dj.SCHEMA_VERSION)


def test_save_is_atomic_and_leaves_no_temp_file():
    root = _root()
    dj.save_job(root, dj.DesignJob())
    leftovers = [f for f in os.listdir(dj.job_dir(root)) if f.endswith(".tmp")]
    eq(leftovers, [], f"残留了临时文件: {leftovers}")


def test_load_missing_or_corrupt_returns_none():
    root = _root()
    eq(dj.load_job(root, "dj_nope"), None)
    path = dj.job_path(root, "dj_broken")
    os.makedirs(dj.job_dir(root), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")
    eq(dj.load_job(root, "dj_broken"), None, "坏文件不能抛异常，要返回 None")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    eq(dj.load_job(root, "dj_broken"), None, "不是对象也不能抛异常")


def test_job_id_is_filesystem_safe():
    root = _root()
    job = dj.DesignJob(job_id="../../evil id!")
    path = dj.save_job(root, job)
    eq(os.path.dirname(os.path.abspath(path)), os.path.abspath(dj.job_dir(root)),
       "job_id 里的路径分隔符必须被清掉")


def test_list_and_delete():
    root = _root()
    a = dj.save_job(root, dj.DesignJob())
    b = dj.save_job(root, dj.DesignJob())
    ids = dj.list_job_ids(root)
    eq(len(ids), 2)
    ok(os.path.basename(a)[:-5] in ids and os.path.basename(b)[:-5] in ids)
    ok(dj.delete_job(root, os.path.basename(a)[:-5]))
    eq(len(dj.list_job_ids(root)), 1)
    eq(dj.delete_job(root, "dj_nope"), False)


def test_from_dict_tolerates_missing_fields():
    job = dj.DesignJob.from_dict({"job_id": "dj_x"})
    eq(job.stage, dj.STAGE_DRAFT)
    eq(job.metrics, [])
    eq(job.artifacts, {})
    job2 = dj.DesignJob.from_dict({"stage": "火星"})
    eq(job2.stage, dj.STAGE_DRAFT, "未知阶段要回落到 draft")


# ---------------------------------------------------------------------------
# 编排服务
# ---------------------------------------------------------------------------

_UNEXPECTED = object()


def _fake_call(sim=_UNEXPECTED, traces=_UNEXPECTED, record=None):
    """替身 tools.call：只回答被显式给定的工具，其余一律报"不该被调用"。

    注意签名带 job_id —— run_design 会对 run_simulation 传作业标识。
    verify_workspace 的 get_workspace_info 调用若落到这里会抛 AssertionError，
    服务层按"ADS 不可达"跳过工作区核查，不影响本文件的既有用例。
    """
    def fake(cfg, name, args, job_id=""):
        if record is not None:
            record.append((name, args))
        if name == "run_simulation":
            if sim is _UNEXPECTED:
                raise AssertionError("本次测试不应调用 run_simulation")
            if isinstance(sim, Exception):
                raise sim
            return sim
        if name == "read_traces":
            if traces is _UNEXPECTED:
                raise AssertionError("本次测试不应调用 read_traces")
            if isinstance(traces, Exception):
                raise traces
            return traces
        raise AssertionError(f"未预期的工具调用 {name}")
    return fake


def _good_sim(root):
    return {"dataset_path": os.path.join(root, "o", "Amp.ds"),
            "output_dir": os.path.join(root, "o"),
            "netlist_path": os.path.join(root, "o", "netlist.ckt"),
            "status": "done", "variables": ["dB(S(2,1))"], "audit": {"n_instances": 6}}


def _good_traces(root, gain=16.0, halfwidth=3.0):
    """2.0–3.0 GHz 的平坦增益曲线（默认带内几乎不变，所以容易达标）。

    halfwidth 调小就能造出"带内增益不足"的数据，用来测未达标路径。
    """
    xs = [2.0 + i * 0.001 for i in range(1001)]
    ys = [gain - 60 * ((x - 2.4) / halfwidth) ** 2 for x in xs]
    return {"path": os.path.join(root, "o", "Amp.ds"), "variables": ["dB(S(2,1))"],
            "traces": {"dB(S(2,1))": {"x": xs, "y": ys, "y_unit": "dB",
                                      "x_name": "freq", "n_points": len(xs)}}}


SPEC_ARGS = {
    "requirement": "设计一个 2.4 GHz 放大器，带内增益至少 15 dB",
    "design": {"library": "AI_lib", "cell": "Amp24G", "view": "schematic"},
    "band": {"start": 2.3, "stop": 2.5, "unit": "GHz"},
    "metrics": [{"id": "gain", "label": "带内增益", "kind": "min_in_band",
                 "expr": "dB(S(2,1))", "target": 15.0, "unit": "dB"}],
}


def test_build_spec_requires_design_and_metrics():
    raises(svc.DesignError, lambda: svc.build_spec({"metrics": SPEC_ARGS["metrics"]}),
           "缺设计引用应报错")
    raises(svc.DesignError, lambda: svc.build_spec({"design": SPEC_ARGS["design"]}),
           "缺指标定义应报错")
    e = raises(svc.DesignError, lambda: svc.build_spec(
        {"design": SPEC_ARGS["design"], "metrics": [{"kind": "min_in_band"}]}))
    contains(str(e), "expr", "没有 expr 的指标要被指出问题")


def test_build_spec_derives_traces_and_normalizes_band():
    spec = svc.build_spec(SPEC_ARGS)
    eq([t["expr"] for t in spec["traces"]], ["dB(S(2,1))"], "曲线应从指标自动去重")
    eq(spec["band"]["start_hz"], 2.3e9)
    eq(spec["design"]["view"], "schematic")
    eq(spec["simulate"], True)


def test_build_spec_rejects_invalid_band_for_band_metrics():
    args = json.loads(json.dumps(SPEC_ARGS))
    args["band"] = {"start": "2.3 GHz", "stop": "2.5 GHz"}
    error = raises(svc.DesignError, lambda: svc.build_spec(args))
    contains(str(error), '"unit": "GHz"')


def test_build_spec_allows_value_at_without_band():
    args = json.loads(json.dumps(SPEC_ARGS))
    args.pop("band")
    args["metrics"] = [{"kind": "value_at", "expr": "dB(S(2,1))",
                        "at_hz": 2.4e9, "target": 10}]
    eq(svc.build_spec(args)["band"], {})


def test_build_spec_strips_model_supplied_measurements():
    """模型塞进来的 actual / pass 不能进入任务（防编造数据）。"""
    args = json.loads(json.dumps(SPEC_ARGS))
    args["metrics"][0].update({"actual": 99.9, "pass": True})
    spec = svc.build_spec(args)
    eq(spec["metrics"][0].get("actual"), None, "模型给的实测值必须被清掉")
    eq(spec["metrics"][0].get("pass"), None, "模型给的判定必须被清掉")


def test_run_design_passes_when_metrics_are_met():
    root = _root()
    tools_mod.call = _fake_call(_good_sim(root), _good_traces(root))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)
    eq(job.stage, dj.STAGE_DONE)
    eq(job.verdict(), "pass")
    eq(job.metrics[0]["pass"], True)
    eq(job.metrics[0]["at"], "2.3 GHz", "应报告带内最低点所在的频率")
    ok(os.path.exists(dj.job_path(root, job.job_id)), "任务必须落盘")
    eq(len(job.iterations), 1)
    eq(job.sim["status"], "done")
    ok(job.artifacts.get("traces"), "结果页需要曲线数据")


def test_run_design_reports_partial_when_one_metric_fails():
    root = _root()
    args = json.loads(json.dumps(SPEC_ARGS))
    args["metrics"].append({"id": "gain20", "label": "增益≥20", "kind": "min_in_band",
                            "expr": "dB(S(2,1))", "target": 20.0, "unit": "dB"})
    tools_mod.call = _fake_call(_good_sim(root), _good_traces(root))
    job = svc.run_design({}, svc.build_spec(args), root)
    eq(job.stage, dj.STAGE_EVALUATED)
    eq(job.verdict(), "partial")
    eq(job.summary()["n_passed"], 1)
    eq(job.summary()["n_failed"], 1)


def test_run_design_reports_failure_when_gain_is_too_low():
    """带内增益不足时必须是 fail —— 而且实测值来自数据，不是模型说的。"""
    root = _root()
    tools_mod.call = _fake_call(_good_sim(root), _good_traces(root, gain=12.0))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)
    eq(job.verdict(), "fail")
    eq(job.metrics[0]["pass"], False)
    ok(job.metrics[0]["actual"] < 15.0)
    contains(job.metrics[0]["at"], "GHz", "未达标时必须给出对应频点")


def test_run_design_keeps_artifacts_when_simulation_fails():
    """仿真失败：状态是 failed，但报错里带的输出目录/网表要抢救回来。"""
    root = _root()
    out = os.path.join(root, "o")
    message = (f"仿真失败：ExecutionError: hpeesofsim 退出码 1\n"
               f"网表已保存到: {os.path.join(out, 'netlist.ckt')}\n"
               f"输出目录: {out}")
    tools_mod.call = _fake_call(RuntimeError(message))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)

    eq(job.stage, dj.STAGE_FAILED)
    contains(job.error, "hpeesofsim")
    eq(job.artifacts.get("output_dir"), out, "失败时也必须保留已经生成的输出目录")
    contains(job.artifacts.get("netlist_path", ""), "netlist.ckt",
             "失败时也必须保留已经生成的网表")
    eq(job.design_ref(), "AI_lib:Amp24G:schematic", "失败不该丢掉设计引用")
    ok(all(m.get("actual") is None and m.get("pass") is None for m in job.metrics),
       "仿真都没跑完，绝不能有实测值或判定")
    eq(job.verdict(), "unknown")
    ok(os.path.exists(dj.job_path(root, job.job_id)), "失败的任务也要落盘")


def test_run_design_fails_clearly_when_no_dataset():
    root = _root()
    sim = dict(_good_sim(root), dataset_path="", status="no_dataset",
               hint="检查网表里有没有仿真控制器")
    tools_mod.call = _fake_call(sim)
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)
    eq(job.stage, dj.STAGE_FAILED)
    contains(job.error, "没有产出 .ds")
    contains(job.error, "仿真控制器")


def test_run_design_fails_clearly_when_reading_traces_fails():
    root = _root()
    tools_mod.call = _fake_call(_good_sim(root), RuntimeError("数据集被占用"))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)
    eq(job.stage, dj.STAGE_FAILED)
    contains(job.error, "读取数据集失败")
    eq(job.artifacts.get("dataset_path", "").endswith("Amp.ds"), True,
       "读数据失败时数据集路径仍要留住，方便重试")


def test_run_design_without_dataset_path_fails_before_calling_ads():
    root = _root()
    calls = []
    tools_mod.call = _fake_call(record=calls)
    spec = svc.build_spec(dict(SPEC_ARGS, simulate=False, reuse_dataset=True))
    job = svc.run_design({}, spec, root)
    eq(job.stage, dj.STAGE_FAILED)
    contains(job.error, "没有可用的数据集路径")
    # get_workspace_info 是新增的工作区核查（允许）；不得有仿真/读数调用
    eq([n for n, _ in calls if n != "get_workspace_info"], [],
       "没有数据集时不该去打扰 ADS（除工作区核查外零调用）")


def test_reload_missing_job_raises():
    root = _root()
    raises(svc.DesignError, lambda: svc.reload_job({}, root, "dj_nope"))
    raises(svc.DesignError, lambda: svc.resimulate({}, root, "dj_nope"))


def test_resimulate_keeps_the_same_job_and_appends_iterations():
    root = _root()
    tools_mod.call = _fake_call(_good_sim(root), _good_traces(root))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)
    again = svc.resimulate({}, root, job.job_id)
    eq(again.job_id, job.job_id, "重新仿真应延续同一个任务（结果页还指向它）")
    eq(len(again.iterations), 2, "每轮都要留一条迭代记录")
    eq([it["n"] for it in again.iterations], [1, 2])


def test_reload_reuses_the_stored_dataset():
    root = _root()
    tools_mod.call = _fake_call(_good_sim(root), _good_traces(root))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)

    calls = []
    tools_mod.call = _fake_call(traces=_good_traces(root), record=calls)
    reloaded = svc.reload_job({}, root, job.job_id)
    names = [n for n, _ in calls if n != "get_workspace_info"]
    eq(names, ["read_traces"], "重新评估不应重新仿真（工作区核查除外）")
    eq(reloaded.verdict(), "pass")
    eq(reloaded.artifacts.get("dataset_path"), job.artifacts.get("dataset_path"))


def test_reruns_preserve_bandwidth_threshold_and_value_at_request():
    root = _root()
    args = json.loads(json.dumps(SPEC_ARGS))
    args["metrics"] = [
        {"id": "bw", "kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.15, "threshold": 25.0, "unit": "GHz"},
        {"id": "point", "kind": "value_at", "expr": "dB(S(2,1))",
         "target": 10.0, "at_hz": 2.4004e9, "unit": "dB"},
    ]
    traces = _good_traces(root)
    traces["traces"]["dB(S(2,1))"]["x_unit"] = "GHz"
    tools_mod.call = _fake_call(_good_sim(root), traces)
    first = svc.run_design({}, svc.build_spec(args), root)
    eq(first.metrics[0]["pass"], False)
    eq(first.metric_specs[0]["threshold"], 25.0)
    eq(first.metric_specs[1]["at_hz"], 2.4004e9)
    eq(first.metrics[1]["at_hz"], 2.4e9)

    reloaded = svc.reload_job({}, root, first.job_id)
    eq(reloaded.metrics[0]["pass"], False, "重新评估不能把带宽目标误作增益门限")
    eq(reloaded.metric_specs[1]["at_hz"], 2.4004e9)
    again = svc.resimulate({}, root, first.job_id)
    eq(again.metrics[0]["pass"], False, "重新仿真仍须使用原纵轴门限")
    eq(again.metric_specs[1]["at_hz"], 2.4004e9)
    eq(len(again.iterations), 3)


def test_legacy_result_without_original_conditions_is_unknown():
    root = _root()
    args = json.loads(json.dumps(SPEC_ARGS))
    args["metrics"] = [
        {"kind": "bandwidth_above", "expr": "dB(S(2,1))",
         "target": 0.15, "threshold": 25.0, "unit": "GHz"},
        {"kind": "value_at", "expr": "dB(S(2,1))",
         "target": 10.0, "at_hz": 2.4004e9, "unit": "dB"},
    ]
    traces = _good_traces(root)
    traces["traces"]["dB(S(2,1))"]["x_unit"] = "GHz"
    tools_mod.call = _fake_call(_good_sim(root), traces)
    job = svc.run_design({}, svc.build_spec(args), root)
    data = job.to_dict()
    data.pop("metric_specs")
    dj.save_job(root, data)
    old = svc.reload_job({}, root, job.job_id)
    eq(old.metrics[0]["pass"], None, "旧任务缺失纵轴门限时不能编造达标结论")
    eq(old.metrics[1]["pass"], None, "旧任务缺失原目标频点时不能把采样点当目标")


def test_iteration_records_the_metric_values_of_that_round():
    root = _root()
    tools_mod.call = _fake_call(_good_sim(root), _good_traces(root, gain=12.0))
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)
    it = job.iterations[0]
    eq(it["summary"]["verdict"], "fail")
    m = it["metrics"][0]
    eq(m["pass"], False)
    ok(m["actual"] is not None, "迭代记录里要留下那一轮的真实数值")
    eq(it["dataset_path"], job.artifacts["dataset_path"])


def test_traces_are_downsampled_but_metrics_use_full_data():
    """结果页存的是降采样曲线，判定必须基于完整数据。"""
    root = _root()
    xs = [2.0 + i * 0.0001 for i in range(10001)]      # 1 万个点
    ys = [16.0 - 60 * ((x - 2.4) / 0.6) ** 2 for x in xs]
    ys[5000] = -5.0                                    # 2.5 GHz 处一个深坑
    traces = {"path": "x", "variables": ["dB(S(2,1))"],
              "traces": {"dB(S(2,1))": {"x": xs, "y": ys, "y_unit": "dB",
                                        "n_points": len(xs)}}}
    calls = []
    tools_mod.call = _fake_call(_good_sim(root), traces, record=calls)
    job = svc.run_design({}, svc.build_spec(SPEC_ARGS), root)

    # 期望值直接从完整数组算出来（2.3–2.5 GHz 内的最小值就是那个坑）
    expected = min(y for x, y in zip(xs, ys) if 2.3 <= x <= 2.5)
    eq(expected, -5.0)
    eq(job.metrics[0]["actual"], expected, "判定必须基于完整数据")

    stored = job.artifacts["traces"]["dB(S(2,1))"]
    ok(len(stored["x"]) < 10001, "存进结果页的曲线应当是降采样过的")
    eq(len(stored["x"]), len(stored["y"]))
    eq(stored["n_points"], 10001, "原始点数要如实记录")
    ok(-5.0 in stored["y"], "降采样必须保住极值（2.5 GHz 的深坑）")
    eq(job.metrics[0]["exact"], True, "没被截断时判定是精确的")
    read_call = next(args for name, args in calls if name == "read_traces")
    eq(read_call["max_points"], 0, "指标评估应请求完整曲线")


if __name__ == "__main__":
    try:
        code = run(globals(), "设计任务：状态机 / 持久化 / 编排")
    finally:
        _teardown()
    sys.exit(code)
