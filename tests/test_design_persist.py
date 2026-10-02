"""任务持久化与并发保护回归（2026-10-02 优化轮，不需要 ADS）。

覆盖：
* 磁盘写入失败必须上账（save_errors）并在接口响应里可见（save_ok=false），
  "计算已完成"与"结果未成功保存"分开报告；
* 关键阶段检查点：仿真进行中任务文件已带 stage=simulating 与作业标识；
* 同一设计引用并发写入/仿真被串行化（两个项目请求改同一设计不互相覆盖）；
* 同一任务并发的 resimulate/reload 串行执行；
* request_id 幂等缓存：重复请求返回原响应；
* schema v1 -> v2 迁移：旧任务文件读取后补默认值并记录迁移说明。

运行::

    python tests/test_design_persist.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")

import design_job as dj  # noqa: E402
import design_service as dsvc  # noqa: E402
import server as server_mod  # noqa: E402

SPEC = {
    "design": {"library": "AI_lib", "cell": "Amp"},
    "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                 "target": 15.0, "unit": "dB"}],
    "band": {"start": 2.3, "stop": 2.5, "unit": "GHz"},
}


class _FakeTools:
    AdsToolError = Exception

    def __init__(self, workspace="E:/ws/A", slow_read=0.0, on_sim=None):
        self.workspace = workspace
        self.slow_read = slow_read
        self.on_sim = on_sim
        self.calls = []

    def call(self, cfg, name, args, job_id=""):
        self.calls.append(name)
        if name == "get_workspace_info":
            return {"workspace_open": True, "path": self.workspace}
        if name == "run_simulation":
            if self.on_sim is not None:
                self.on_sim()
            return {"status": "done", "dataset_path": "E:/ws/sim/A.ds",
                    "output_dir": "E:/ws/sim", "netlist_path": "E:/ws/sim/netlist",
                    "design_version": {"netlist_sha": "shaX"},
                    "workspace": {"name": "A", "path": self.workspace}}
        if name == "read_traces":
            if self.slow_read:
                time.sleep(self.slow_read)
            return {"variables": ["dB(S(2,1))"],
                    "traces": {"dB(S(2,1))": {
                        "x": [2.3e9, 2.4e9, 2.5e9], "y": [16.0, 18.0, 16.0],
                        "x_unit": "Hz", "y_unit": "dB"}}}
        raise AssertionError(f"未预期的工具调用 {name}")

    @staticmethod
    def is_local(name):
        return False


def _with(fake, fn):
    real = dsvc.tools_mod
    dsvc.tools_mod = fake
    try:
        return fn()
    finally:
        dsvc.tools_mod = real


# ---------------------------------------------------------------------------
# 保存失败反馈
# ---------------------------------------------------------------------------

def test_save_failure_is_recorded_on_the_job_and_reported():
    root = tempfile.mkdtemp(prefix="persist1_")
    try:
        fake = _FakeTools()
        spec = dsvc.build_spec(dict(SPEC))
        real_save = dj.save_job
        failures = []

        def _broken_save(r, job):
            failures.append(1)
            raise OSError("磁盘已满（模拟）")

        dj.save_job = _broken_save
        try:
            job = _with(fake, lambda: dsvc.run_design({}, spec, root))
        finally:
            dj.save_job = real_save

        ok(job.is_finished(), "计算本身完成了：指标已评估")
        eq(job.stage, dj.STAGE_DONE)
        ok(len(job.save_errors) >= 1, "保存失败必须记到 job.save_errors")
        contains(job.save_errors[-1]["error"], "OSError")
        eq(len(failures) >= 1, True)
        # server 响应契约：save_ok=false 且带错误明细
        resp = {"save_ok": not job.save_errors}
        eq(resp["save_ok"], False, "接口必须报告'结果未成功保存'")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_checkpoint_during_simulation_has_stage_and_job_id():
    root = tempfile.mkdtemp(prefix="persist2_")
    try:
        observed = {}

        def _during_sim():
            # 仿真进行中：磁盘上的任务文件应已反映 simulating 状态与作业标识
            ids = dj.list_job_ids(root)
            if not ids:
                observed["file"] = False
                return
            with open(dj.job_path(root, ids[0]), encoding="utf-8") as f:
                data = json.load(f)
            observed["stage"] = data.get("stage")
            observed["ads_job_id"] = (data.get("sim") or {}).get("ads_job_id") or ""

        fake = _FakeTools(on_sim=_during_sim)
        spec = dsvc.build_spec(dict(SPEC))
        _with(fake, lambda: dsvc.run_design({}, spec, root))
        eq(observed.get("stage"), "simulating",
           "仿真进行中检查点必须已落盘")
        ok(observed.get("ads_job_id"), "仿真作业标识必须已落盘（供取消转发）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 并发保护
# ---------------------------------------------------------------------------

def test_same_design_concurrent_runs_are_serialized():
    """两个项目同时请求同一设计：串行执行，不互相覆盖。"""
    root = tempfile.mkdtemp(prefix="persist3_")
    try:
        fake = _FakeTools(slow_read=0.15)
        state = {"cur": 0, "max": 0}
        lock = threading.Lock()

        orig_read = fake.call

        def _tracking_call(cfg, name, args, job_id=""):
            if name == "read_traces":
                with lock:
                    state["cur"] += 1
                    state["max"] = max(state["max"], state["cur"])
                try:
                    return orig_read(cfg, name, args, job_id)
                finally:
                    with lock:
                        state["cur"] -= 1
            return orig_read(cfg, name, args, job_id)

        fake.call = _tracking_call

        # 注意：monkeypatch 必须在线程外统一设置/恢复 —— 两个线程共用
        # dsvc.tools_mod，线程内各自的 finally 会把替身提前撤掉
        real = dsvc.tools_mod
        dsvc.tools_mod = fake
        try:
            spec = dsvc.build_spec(dict(SPEC))
            results = []
            t1 = threading.Thread(target=lambda: results.append(
                dsvc.run_design({}, spec, root)))
            t2 = threading.Thread(target=lambda: results.append(
                dsvc.run_design({}, spec, root)))
            t1.start(); t2.start(); t1.join(20); t2.join(20)
        finally:
            dsvc.tools_mod = real
        eq(len(results), 2)
        ok(all(r.stage in (dj.STAGE_DONE, dj.STAGE_EVALUATED) for r in results),
           "两个都应完整跑完（串行，不是互相打断）")
        eq(state["max"], 1, f"同一设计的仿真/读数必须串行，观测到并发 {state['max']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_same_job_resimulate_and_reload_serialize():
    """同一 job_id 的重新仿真与重新评估并发：串行，不基于旧快照互相覆盖。"""
    root = tempfile.mkdtemp(prefix="persist4_")
    try:
        fake = _FakeTools()
        spec = dsvc.build_spec(dict(SPEC))
        job = _with(fake, lambda: dsvc.run_design({}, spec, root))

        overlap = {"cur": 0, "max": 0}
        lock = threading.Lock()
        orig = dsvc.tools_mod

        class _SlowTools(_FakeTools):
            def call(self, cfg, name, args, job_id=""):
                if name in ("run_simulation", "read_traces"):
                    with lock:
                        overlap["cur"] += 1
                        overlap["max"] = max(overlap["max"], overlap["cur"])
                    try:
                        time.sleep(0.1)
                    finally:
                        with lock:
                            overlap["cur"] -= 1
                return super().call(cfg, name, args, job_id)

        dsvc.tools_mod = _SlowTools()
        try:
            outs = []
            t1 = threading.Thread(target=lambda: outs.append(
                dsvc.resimulate({}, root, job.job_id)))
            t2 = threading.Thread(target=lambda: outs.append(
                dsvc.reload_job({}, root, job.job_id)))
            t1.start(); t2.start(); t1.join(30); t2.join(30)
        finally:
            dsvc.tools_mod = orig
        eq(len(outs), 2)
        eq(overlap["max"], 1, f"job 级串行被破坏，观测到并发 {overlap['max']}")
        ok(all(o.job_id == job.job_id for o in outs))
        ok(all(o.is_finished() for o in outs))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_idempotency_cache_roundtrip():
    resp = {"ok": True, "job": {"job_id": "dj_1"}}
    server_mod._idem_store("req-1", resp)
    hit = server_mod._idem_lookup("req-1")
    eq(hit, resp)
    eq(server_mod._idem_lookup("req-2"), None, "未提交过的 request_id 不能命中")
    # 命中后再次取仍然一致（重复点击返回同一响应）
    eq(server_mod._idem_lookup("req-1"), resp)


def test_idempotency_cache_ttl_and_capacity():
    # TTL 过期
    server_mod._IDEM_CACHE["req-old"] = (time.time() - 25 * 3600, {"ok": True})
    eq(server_mod._idem_lookup("req-old"), None, "过期缓存不能命中")
    # 容量上限：超过 _IDEM_MAX 时淘汰最旧的
    server_mod._IDEM_CACHE.clear()
    for i in range(server_mod._IDEM_MAX + 120):
        server_mod._idem_store(f"req-{i}", {"i": i})
    ok(len(server_mod._IDEM_CACHE) <= server_mod._IDEM_MAX,
       f"缓存不能无限增长：{len(server_mod._IDEM_CACHE)}")
    server_mod._IDEM_CACHE.clear()


# ---------------------------------------------------------------------------
# schema 迁移
# ---------------------------------------------------------------------------

def test_v1_job_file_migrates_to_v2():
    root = tempfile.mkdtemp(prefix="persist5_")
    try:
        # 手工写一个 v1 时代的任务文件：没有 save_errors/timing/eval_options
        v1 = {
            "job_id": "dj_v1old",
            "requirement": "老任务",
            "band": {"start_hz": 2.3e9, "stop_hz": 2.5e9, "unit": "GHz"},
            "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                         "target": 15.0, "pass": True, "actual": 16.0}],
            "metric_specs": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                              "target": 15.0}],
            "design": {"library": "AI_lib", "cell": "Amp"},
            "stage": "done",
            "iterations": [],
            "artifacts": {"dataset_path": "E:/x/A.ds"},
            "sim": {"status": "done"},
            "error": "",
            "notes": [],
            "created_at": "2026-09-01T10:00:00",
            "updated_at": "2026-09-01T10:30:00",
            # 注意：没有 schema_version（v1 时代未写或值为 1）
        }
        os.makedirs(dj.job_dir(root), exist_ok=True)
        with open(dj.job_path(root, "dj_v1old"), "w", encoding="utf-8") as f:
            json.dump(v1, f)
        job = dj.load_job(root, "dj_v1old")
        ok(job is not None)
        eq(job.design_version, {}, "新字段以默认值兜底")
        eq(job.save_errors, [])
        ok(any("迁移" in n.get("text", "") for n in job.notes),
           "迁移要留下说明")
        # 再保存即升级为 v2
        dj.save_job(root, job)
        with open(dj.job_path(root, "dj_v1old"), encoding="utf-8") as f:
            data = json.load(f)
        eq(data.get("schema_version"), dj.SCHEMA_VERSION)
        # v1 时代把评估结果混进 metrics 的旧格式仍能读出判定
        eq(job.verdict(), "pass")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_bak_files_are_not_listed_as_jobs():
    root = tempfile.mkdtemp(prefix="persist6_")
    try:
        job = dj.DesignJob(job_id="dj_only")
        dj.save_job(root, job)
        dj.save_job(root, job)          # 第二次保存产生 .bak
        ids = dj.list_job_ids(root)
        eq(ids, ["dj_only"], ".bak 不能被当成任务列出")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run(globals(), "任务持久化与并发保护"))
