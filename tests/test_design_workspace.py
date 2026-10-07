"""工作区绑定与设计版本回归（2026-10-02 优化轮，不需要 ADS）。

用户场景：两个工作区里有同名的 library/cell（例如都在 AI_lib:Amp）。
切过工作区之后对旧结果页点"重新仿真 / 重新评估 / 打开原理图"，绝不能
操作到另一个工作区里的同名设计。

覆盖：
* 第一次执行把当前工作区记录为任务的绑定工作区，仿真记录设计版本
 （网表指纹）与工作区；
* 任务绑定工作区与 ADS 当前工作区不一致时，重新执行被拦截，错误信息
  给出两个路径与恢复方式，且**没有**产生任何仿真调用；
* 发布结果复用从"本轮没调用修改工具"升级为网表指纹验证：设计变了、
  工作区变了都不再复用旧数据集；
* server.workspace_mismatch 纯函数（open_schematic 入口的核对逻辑）；
* 任务文件损坏时从 .bak 恢复（持久化层配合）。

运行::

    python tests/test_design_workspace.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")

import agent  # noqa: E402
import design_job as dj  # noqa: E402
import design_service as dsvc  # noqa: E402
import server as server_mod  # noqa: E402

WS_A = "E:/ADS/Work_AI/AI_wrk"
WS_B = "E:/ADS/Work_Other/Other_wrk"

_SPEC = {
    "design": {"library": "AI_lib", "cell": "Amp"},
    "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                 "target": 15.0, "unit": "dB"}],
    "band": {"start": 2.3, "stop": 2.5, "unit": "GHz"},
}


class _FakeTools:
    """tools_mod 替身：get_workspace_info / run_simulation / read_traces。"""

    AdsToolError = Exception

    def __init__(self, workspace_path, sim_result=None, calls=None):
        self.workspace_path = workspace_path
        self.sim_result = sim_result or {
            "status": "done", "dataset_path": f"{WS_A}/sim/Amp.ds",
            "output_dir": f"{WS_A}/sim", "netlist_path": f"{WS_A}/sim/netlist",
            "design_version": {"netlist_sha": "sha_AAA", "netlist_chars": 100},
            "workspace": {"name": "AI_wrk", "path": workspace_path},
        }
        self.calls = calls if calls is not None else []

    def call(self, cfg, name, args, job_id=""):
        self.calls.append((name, dict(args) if isinstance(args, dict) else args))
        if name == "get_workspace_info":
            return {"workspace_open": True, "path": self.workspace_path,
                    "libraries": ["AI_lib"]}
        if name == "run_simulation":
            return dict(self.sim_result)
        if name == "read_traces":
            return {"variables": ["dB(S(2,1))"],
                    "traces": {"dB(S(2,1))": {
                        "x": [2.3e9, 2.4e9, 2.5e9], "y": [16.0, 18.0, 16.0],
                        "x_unit": "Hz", "y_unit": "dB", "y_name": "dB(S(2,1))",
                        "x_name": "freq"}}}
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


def test_first_run_records_workspace_and_design_version():
    root = tempfile.mkdtemp(prefix="ws_test1_")
    try:
        fake = _FakeTools(WS_A)
        spec = dsvc.build_spec(dict(_SPEC))
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, spec, root))
        eq(job.design.get("workspace"), WS_A,
           "第一次执行应把当前工作区记录为绑定工作区")
        eq(job.design_version.get("netlist_sha"), "sha_AAA")
        eq((job.sim or {}).get("workspace"), WS_A)
        ok(job.design_ref().startswith("AI_lib:Amp"))
        # 持久化里也在
        loaded = dj.load_job(root, job.job_id)
        eq(loaded.design.get("workspace"), WS_A)
        eq(loaded.design_version.get("netlist_sha"), "sha_AAA")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_resimulate_blocked_after_workspace_switch():
    """切到另一个工作区后重新仿真：必须拦截，且一个仿真都不能发。"""
    root = tempfile.mkdtemp(prefix="ws_test2_")
    try:
        fake_a = _FakeTools(WS_A)
        spec = dsvc.build_spec(dict(_SPEC))
        job = _with_fake_tools(fake_a, lambda: dsvc.run_design({}, spec, root))
        ok(job.is_finished())

        # 换到工作区 B（里面有同名 AI_lib:Amp）
        fake_b = _FakeTools(WS_B, calls=fake_a.calls)
        n_calls_before = len(fake_b.calls)
        job2 = _with_fake_tools(
            fake_b, lambda: dsvc.resimulate({}, root, job.job_id))
        eq(job2.stage, dj.STAGE_FAILED)
        contains(job2.error, "工作区不一致")
        contains(job2.error, WS_A)
        contains(job2.error, WS_B)
        contains(job2.error, "同名", "要说明为什么拦截：同名设计可能不是同一个")
        contains(job2.error, "恢复方式", "要给出可理解的恢复路径")
        sim_calls = [c for c in fake_b.calls[n_calls_before:]
                     if c[0] in ("run_simulation", "read_traces")]
        eq(sim_calls, [], "拦截时绝不能有仿真/读数调用")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_explicit_workspace_in_request_is_honoured():
    """显式给 design.workspace 时：与当前工作区一致才放行。"""
    root = tempfile.mkdtemp(prefix="ws_test3_")
    try:
        fake = _FakeTools(WS_A)
        spec = dsvc.build_spec(dict(_SPEC, design={
            "library": "AI_lib", "cell": "Amp", "workspace": WS_A}))
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, spec, root))
        ok(not job.error, f"一致时应放行：{job.error}")

        spec_b = dsvc.build_spec(dict(_SPEC, design={
            "library": "AI_lib", "cell": "Amp", "workspace": WS_B}))
        job_bad = _with_fake_tools(fake, lambda: dsvc.run_design({}, spec_b, root))
        eq(job_bad.stage, dj.STAGE_FAILED)
        contains(job_bad.error, "工作区不一致")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_windows_path_case_and_slash_do_not_trigger_mismatch():
    root = tempfile.mkdtemp(prefix="ws_test4_")
    try:
        fake = _FakeTools(WS_A)
        spec = dsvc.build_spec(dict(_SPEC, design={
            "library": "AI_lib", "cell": "Amp",
            "workspace": "e:\\ads\\work_ai\\ai_wrk\\"}))
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, spec, root))
        ok(not job.error, "同一工作区的大小写/分隔符差异不应触发拦截")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 发布复用的设计版本验证
# ---------------------------------------------------------------------------

def _turn_with_last_sim(fake_tools_cfg=None):
    turn = agent.Turn({"llm_model": "m", "max_tool_steps": 5}, [])
    turn._last_sim = {
        "library": "AI_lib", "cell": "Amp", "view": "schematic",
        "dataset_path": f"{WS_A}/sim/Amp.ds",
        "netlist_sha": "sha_AAA", "workspace": WS_A,
    }
    return turn


class _AgentFakeTools:
    AdsToolError = Exception

    def __init__(self, sha="sha_AAA", workspace=WS_A, fail=False):
        self.sha = sha
        self.workspace = workspace
        self.fail = fail
        self.called = []

    def call(self, cfg, name, args, job_id=""):
        self.called.append(name)
        if self.fail:
            raise RuntimeError("ADS offline")
        assert name == "design_fingerprint"
        return {"design_version": {"netlist_sha": self.sha},
                "workspace": {"path": self.workspace}}

    @staticmethod
    def is_local(name):
        return False


def test_reuse_rejected_when_design_changed():
    turn = _turn_with_last_sim()
    fake = _AgentFakeTools(sha="sha_BBB")
    real = agent.tools_mod
    agent.tools_mod = fake
    try:
        out = turn._reuse_recent_simulation({"design": {
            "library": "AI_lib", "cell": "Amp"}})
        eq(out.get("reuse_dataset"), None, "设计变了就不能复用旧数据集")
        eq(out.get("dataset_path"), None)
        contains(turn._reuse_note, "网表指纹")
        eq(turn._last_sim, None, "作废的缓存要清掉")
    finally:
        agent.tools_mod = real


def test_reuse_rejected_when_workspace_changed():
    turn = _turn_with_last_sim()
    fake = _AgentFakeTools(sha="sha_AAA", workspace=WS_B)
    real = agent.tools_mod
    agent.tools_mod = fake
    try:
        out = turn._reuse_recent_simulation({"design": {
            "library": "AI_lib", "cell": "Amp"}})
        eq(out.get("reuse_dataset"), None, "工作区变了就不能复用旧数据集")
        contains(turn._reuse_note, "工作区")
    finally:
        agent.tools_mod = real


def test_reuse_kept_when_fingerprint_matches():
    turn = _turn_with_last_sim()
    fake = _AgentFakeTools(sha="sha_AAA", workspace=WS_A)
    real = agent.tools_mod
    agent.tools_mod = fake
    try:
        out = turn._reuse_recent_simulation({"design": {
            "library": "AI_lib", "cell": "Amp"}})
        eq(out.get("reuse_dataset"), True, "指纹一致时正常复用")
        eq(out.get("dataset_path"), f"{WS_A}/sim/Amp.ds")
        eq(fake.called, ["design_fingerprint"])
    finally:
        agent.tools_mod = real


def test_reuse_falls_back_gracefully_when_fingerprint_unavailable():
    turn = _turn_with_last_sim()
    fake = _AgentFakeTools(fail=True)
    real = agent.tools_mod
    agent.tools_mod = fake
    try:
        out = turn._reuse_recent_simulation({"design": {
            "library": "AI_lib", "cell": "Amp"}})
        eq(out.get("reuse_dataset"), True, "指纹不可用时退回旧规则，不阻塞")
        contains(turn._reuse_note, "不可用", "退回时必须如实注明")
    finally:
        agent.tools_mod = real


# ---------------------------------------------------------------------------
# open_schematic 入口的核对（纯函数）与备份恢复
# ---------------------------------------------------------------------------

def test_workspace_mismatch_pure_function():
    ok(server_mod.workspace_mismatch(WS_A, WS_A) is None)
    ok(server_mod.workspace_mismatch(WS_A, "") is None, "没记录工作区时不拦")
    ok(server_mod.workspace_mismatch("", WS_A) is None, "拿不到当前工作区时不拦")
    msg = server_mod.workspace_mismatch(WS_A, WS_B)
    ok(msg is not None)
    contains(msg, WS_B)
    # Windows 大小写与斜杠差异不算不同工作区
    ok(server_mod.workspace_mismatch("E:/ADS/Work_AI/AI_wrk",
                                     "e:\\ads\\work_ai\\ai_wrk\\") is None)


def test_corrupt_job_file_recovered_from_bak():
    root = tempfile.mkdtemp(prefix="ws_test5_")
    try:
        fake = _FakeTools(WS_A)
        spec = dsvc.build_spec(dict(_SPEC))
        job = _with_fake_tools(fake, lambda: dsvc.run_design({}, spec, root))
        path = dj.job_path(root, job.job_id)
        ok(os.path.exists(f"{path}.bak"), "保存时应留下上一版备份")

        # 把主文件写坏（模拟写入中断 / 磁盘问题）
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"job_id": "dj_x", "stage": "si')   # 半截 JSON
        job2, info = dj.load_job_ex(root, job.job_id)
        ok(job2 is not None, "主文件损坏时应能从 .bak 恢复")
        eq(info.get("recovered_from_backup"), True)
        contains(info.get("error", ""), "损坏")
        ok(any("备份" in n.get("text", "") for n in job2.notes),
           "恢复来源要记录在任务笔记里")
        eq(job2.design_version.get("netlist_sha"), "sha_AAA")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recover_interrupted_marks_unconfirmed():
    root = tempfile.mkdtemp(prefix="ws_test6_")
    try:
        # 造一个停在 simulating 的任务（后端退出时留下的现场）
        job = dj.DesignJob(requirement="r",
                           band=dj.normalize_band({"start": 2.3, "stop": 2.5, "unit": "GHz"}),
                           metrics=[{"kind": "min_in_band", "expr": "dB(S(2,1))",
                                     "target": 15.0, "unit": "dB"}],
                           design={"library": "AI_lib", "cell": "Amp"})
        job.transition(dj.STAGE_PLANNED, "t")
        job.transition(dj.STAGE_SIMULATING, "t")
        job.set_sim(status="running", started_at=dj.utc_now())
        job.set_artifacts(netlist_path="E:/x/netlist")
        dj.save_job(root, job)

        recovered = dsvc.recover_interrupted(root)
        eq(recovered, [job.job_id])
        loaded = dj.load_job(root, job.job_id)
        eq(loaded.sim.get("status"), "interrupted")
        eq(loaded.stage, dj.STAGE_FAILED)
        contains(loaded.error, "中断")
        contains(loaded.error, "待确认")
        contains(loaded.error, "不能默认视为完成")
        eq(loaded.artifacts.get("netlist_path"), "E:/x/netlist",
           "产物必须原样保留")
        # 再跑一次恢复不应重复处理
        eq(dsvc.recover_interrupted(root), [])
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run(globals(), "工作区绑定与设计版本"))
