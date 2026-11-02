"""独立计算作业管理器（backend/compute_manager.py）的行为测试。

覆盖的是契约里最容易骗人的几件事：

- 排队容量满必须**原子拒绝**（不超卖、不丢单）；
- max_workers 必须真的生效（并发上限不是装饰）；
- 排队中取消 → **绝不 spawn**（一个字节的用户代码都不跑）；
- 运行中取消 → 先如实分阶段（cancel_requested），进程退出后才是 cancelled；
- 取消/超时后**没有孤儿进程**（只回收自己的 pid 树）；
- 等待超时 ≠ 作业结束：超时后 inspect 仍能看到真实状态；
- 重复 submit 幂等（不重复建 worker）；
- worker 崩溃 / 后端重启都不带崩管理器，且状态可解释。

纪律：临时数据目录；事件 + 有界轮询（不用固定 sleep 制造竞态）；
后台线程异常必须判失败。

运行::

    python tests/test_compute_manager.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run, skip  # noqa: E402

BACKEND = add_path("backend")

import compute_manager as cm  # noqa: E402


def _have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _pid_alive(pid) -> bool:
    """进程是否仍存活。

    注意：**不能用 os.kill(pid, 0)** —— 在 Windows 上 os.kill 的第二个参数
    不是信号而是退出码，os.kill(pid, 0) 会直接 TerminateProcess 把被测进程
    杀掉（既破坏被测对象，又永远测不出"是否已回收"）。这里用 OpenProcess +
    GetExitCodeProcess 做只读探测。
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, int(pid))
        if not handle:
            return False          # 打不开 = 已退出（或已不是我们的进程）
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)      # POSIX 下 sig 0 才是纯探测
    except OSError:
        return False
    return True


def wait_until(cond, msg="", timeout=20.0):
    """有界轮询（不用固定 sleep 当断言）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{msg}")


class Manager:
    """临时 base_dir 的 ComputeManager（测试结束必定 shutdown_all）。"""

    def __init__(self, **kwargs):
        self.base = tempfile.mkdtemp(prefix="ads_agent_cm_")
        kwargs.setdefault("probe_cache_s", 3600)
        self.mgr = cm.ComputeManager(base_dir=self.base, **kwargs)

    def __enter__(self):
        return self.mgr

    def __exit__(self, *exc):
        try:
            self.mgr.shutdown_all(grace_s=0.5)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.base, ignore_errors=True)


# 慢速但**协作**的计算：每轮 20ms，便于观测 running 阶段
_SLOW_CODE = """
import time
acc = 0.0
for i in range(600):
    if check_cancel():
        progress('cancelled at %d' % i)
        break
    acc += i * 0.5
    time.sleep(0.02)
result = {'acc': acc}
"""

# 非协作的热循环：不调 check_cancel()，只能靠 terminate 回收
_HOT_CODE = """
import numpy as np
a = np.ones((1200, 1200))
for _ in range(100000):
    b = a @ a
result = float(b[0, 0])
"""


# ---------------------------------------------------------------------------
# 1. 正常完成：record 契约字段齐全
# ---------------------------------------------------------------------------

def test_submit_wait_returns_complete_record_contract():
    with Manager(max_workers=1, queue_capacity=4) as mgr:
        rec = mgr.submit("j-ok", "result = {'v': inputs['v'] + 1}", inputs={"v": 41},
                         timeout_s=60, workspace="ws-1", label="契约用例")
        for key in ("job_id", "state", "phase", "workspace", "submit_ts",
                    "start_ts", "end_ts", "timeout_s", "worker_pid",
                    "exit_kind", "error_kind", "stdout_tail", "result", "partial"):
            ok(key in rec, f"record 必须包含契约字段 {key}")
        final = mgr.wait("j-ok", timeout_s=60)
        eq(final["state"], "succeeded", f"作业应成功：{final.get('error')}")
        eq(final["result"], {"v": 42}, "结果值")
        eq(final["partial"], False, "完整结果不得标 partial")
        eq(final["exit_kind"], "natural", "worker 自己写完结果退出")
        eq(final["workspace"], "ws-1", "workspace 身份要跟着作业走")
        ok(final["elapsed_s"] is not None and final["elapsed_s"] >= 0,
           "必须记录耗时")
        # 终态仍可查询（"HTTP 请求结束 ≠ 执行结束"）
        again = mgr.inspect("j-ok")
        eq(again["state"], "succeeded", "终态作业必须仍可 inspect")


# ---------------------------------------------------------------------------
# 2. 幂等：同 job_id 不重复建 worker
# ---------------------------------------------------------------------------

def test_submit_is_idempotent_and_does_not_spawn_twice():
    with Manager(max_workers=2) as mgr:
        first = mgr.submit("j-idem", "result = 1", timeout_s=60)
        second = mgr.submit("j-idem", "result = 999", timeout_s=60)
        eq(second["job_id"], first["job_id"], "同 job_id 提交必须幂等返回同一条记录")
        eq(second["submit_ts"], first["submit_ts"], "幂等提交不得重置记录")
        final = mgr.wait("j-idem", timeout_s=60)
        eq(final["state"], "succeeded", "作业成功")
        eq(final["result"], 1, "第二次提交的 code 不得覆盖第一次（不重复执行）")


# ---------------------------------------------------------------------------
# 3. 排队容量：原子拒绝、不超卖
# ---------------------------------------------------------------------------

def test_queue_capacity_rejects_atomically_without_oversell():
    if not _have_numpy():
        skip("缺 numpy，无法构造占满 worker 的作业")
    with Manager(max_workers=1, queue_capacity=2) as mgr:
        # 先占住唯一的 worker（非协作热循环，必须靠取消才能停下）
        mgr.submit("j-busy", _HOT_CODE, timeout_s=300)
        wait_until(lambda: mgr.inspect("j-busy")["state"] == "running",
                   "占位作业应先跑起来")
        mgr.submit("j-q1", "result = 1", timeout_s=300)
        mgr.submit("j-q2", "result = 2", timeout_s=300)
        # 容量 2 已满：第三次必须立刻拒绝
        err = None
        try:
            mgr.submit("j-q3", "result = 3", timeout_s=300)
        except cm.ComputeQueueFull as e:
            err = e
        ok(err is not None, "排队满必须抛 ComputeQueueFull")
        contains(str(err), "排队已满", "错误文案要说明是排队满")
        with mgr._lock:
            eq(mgr._pending_count, 2, "超卖的提交不得进入排队记账")
            ok("j-q3" not in mgr._records, "被拒绝的作业不得留下记录")
        # 清掉占位作业，避免污染后续
        mgr.cancel("j-busy")
        mgr.cancel("j-q1")
        mgr.cancel("j-q2")


def test_concurrency_limit_is_actually_enforced():
    with Manager(max_workers=2, queue_capacity=6) as mgr:
        for i in range(4):
            mgr.submit(f"j-c{i}", _SLOW_CODE, timeout_s=120)
        # 最多 2 个同时 running，其余 queued
        wait_until(lambda: sum(1 for i in range(4)
                               if mgr.inspect(f"j-c{i}")["state"] == "running") == 2,
                   "应有且仅有 2 个作业同时 running")
        states = [mgr.inspect(f"j-c{i}")["state"] for i in range(4)]
        eq(states.count("running"), 2, f"并发上限必须生效：{states}")
        eq(states.count("queued"), 2, f"其余必须排队：{states}")
        for i in range(4):
            mgr.cancel(f"j-c{i}")


# ---------------------------------------------------------------------------
# 4. 排队中取消：绝不 spawn
# ---------------------------------------------------------------------------

def test_queued_cancel_never_spawns_worker():
    if not _have_numpy():
        skip("缺 numpy")
    ctx = Manager(max_workers=1, queue_capacity=4)
    with ctx as mgr:
        mgr.submit("j-head", _HOT_CODE, timeout_s=300)
        wait_until(lambda: mgr.inspect("j-head")["state"] == "running",
                   "占位作业应先跑起来")
        marker = os.path.join(ctx.base, "queued_ran.txt")
        code = (f"open({marker!r}, 'w').write('ran')\n"
                "result = 1\n")
        mgr.submit("j-queued", code, timeout_s=300)
        eq(mgr.inspect("j-queued")["state"], "queued", "作业应还在排队")
        receipt = mgr.cancel("j-queued")
        eq(receipt["ok"], True, "取消受理")
        eq(receipt["state"], "cancelled", "排队取消直接落终态")
        eq(receipt["reason"], "queued_cancelled_before_spawn", "取消原因要如实")
        eq(mgr.inspect("j-queued")["worker_pid"], None, "排队取消绝不能留下 worker pid")
        # 给派发线程足够机会（若逻辑有 bug 它会去跑）
        time.sleep(0.3)
        ok(not os.path.exists(marker), "排队取消后用户代码绝不能被执行")
        mgr.cancel("j-head")


# ---------------------------------------------------------------------------
# 5. 运行中取消：两阶段状态 + 无孤儿进程
# ---------------------------------------------------------------------------

def test_running_cancel_is_two_phase_and_reaps_process():
    with Manager(max_workers=1, queue_capacity=4, cancel_grace_s=0.5) as mgr:
        mgr.submit("j-run", _SLOW_CODE, timeout_s=120)
        wait_until(lambda: mgr.inspect("j-run")["state"] == "running",
                   "作业应先跑起来")
        pid = mgr.inspect("j-run")["worker_pid"]
        ok(pid and pid != os.getpid(), "worker_pid 必须是子进程")
        ok(_pid_alive(pid), "worker 应真实存活")
        receipt = mgr.cancel("j-run")
        eq(receipt["ok"], True, "取消受理")
        ok(receipt["state"] in ("cancelled", "cancel_requested"),
           f"取消回执状态要如实：{receipt}")
        final = mgr.wait("j-run", timeout_s=30)
        ok(final["state"] in ("cancelled", "succeeded"),
           f"最终状态：{final['state']}")
        if final["state"] == "cancelled":
            ok(final["exit_kind"] in ("natural", "terminated"),
               f"退出方式要如实：{final['exit_kind']}")
        wait_until(lambda: not _pid_alive(pid), "取消后不得留下孤儿 worker 进程",
                   timeout=20)


def test_non_cooperative_hot_loop_is_terminated_and_reaped():
    if not _have_numpy():
        skip("缺 numpy")
    with Manager(max_workers=1, queue_capacity=4, cancel_grace_s=0.3) as mgr:
        mgr.submit("j-hot", _HOT_CODE, timeout_s=300)
        wait_until(lambda: mgr.inspect("j-hot")["state"] == "running",
                   "热循环作业应先跑起来")
        pid = mgr.inspect("j-hot")["worker_pid"]
        receipt = mgr.cancel("j-hot")
        eq(receipt["ok"], True, "取消受理")
        final = mgr.wait("j-hot", timeout_s=40)
        eq(final["state"], "cancelled", f"最终必须 cancelled：{final}")
        ok(final["exit_kind"] in ("terminated", "natural"),
           f"退出方式：{final['exit_kind']}")
        wait_until(lambda: not _pid_alive(pid), "非协作作业也必须被回收",
                   timeout=20)


def test_cancel_is_idempotent_and_honest_when_already_terminal():
    with Manager(max_workers=1) as mgr:
        mgr.submit("j-done", "result = 1", timeout_s=60)
        mgr.wait("j-done", timeout_s=60)
        receipt = mgr.cancel("j-done")
        eq(receipt["ok"], False, "终态作业取消必须如实返回 ok=False")
        eq(receipt["reason"], "already_terminal", "原因")
        eq(receipt["state"], "succeeded", "不得改已有终态")
        unknown = mgr.cancel("does-not-exist")
        eq(unknown["ok"], False, "未知作业")
        eq(unknown["reason"], "unknown_job", "原因")


# ---------------------------------------------------------------------------
# 6. 执行预算：超时被终止回收，状态如实
# ---------------------------------------------------------------------------

def test_execution_budget_timeout_is_reported_and_reaped():
    if not _have_numpy():
        skip("缺 numpy")
    with Manager(max_workers=1, queue_capacity=2) as mgr:
        mgr.submit("j-to", _HOT_CODE, timeout_s=2.0)
        final = mgr.wait("j-to", timeout_s=60)
        eq(final["state"], "timed_out", f"超出执行预算必须 timed_out：{final}")
        ok(final["error_kind"] in ("timeout", "timeout_manager_backstop"),
           f"错误类别：{final['error_kind']}")
        contains(str(final.get("error")), "预算", "错误文案要说明预算到期")
        pid = final["worker_pid"]
        wait_until(lambda: not _pid_alive(pid), "超时后不得留下孤儿进程",
                   timeout=20)


# ---------------------------------------------------------------------------
# 7. 等待超时 ≠ 作业结束
# ---------------------------------------------------------------------------

def test_wait_timeout_does_not_mean_job_finished():
    with Manager(max_workers=1) as mgr:
        mgr.submit("j-wait", _SLOW_CODE, timeout_s=120)
        rec = mgr.wait("j-wait", timeout_s=0.3)   # 只等 0.3s
        ok(rec["state"] in ("queued", "running", "cancel_requested"),
           f"等待超时后作业必须仍是非终态：{rec}")
        ok(rec["state"] != "succeeded", "绝不能把等待超时当成完成")
        # 作业真实状态仍可查询（这就是"HTTP 请求结束 ≠ 执行结束"）
        again = mgr.inspect("j-wait")
        ok(again["state"] in ("running", "cancel_requested"), "真实状态可查询")
        mgr.cancel("j-wait")


# ---------------------------------------------------------------------------
# 8. 崩溃、崩溃后可用、后端重启清扫
# ---------------------------------------------------------------------------

def test_worker_crash_is_failed_and_manager_survives():
    with Manager(max_workers=1, queue_capacity=2) as mgr:
        mgr.submit("j-crash", "import os\nos._exit(7)\n", timeout_s=60)
        final = mgr.wait("j-crash", timeout_s=60)
        eq(final["state"], "failed", f"硬退出的作业必须 failed：{final}")
        eq(final["exit_kind"], "crashed", "退出方式 crashed")
        contains(str(final.get("error")), "rc=7", "错误文案要带退出码")
        # 管理器必须还能干活
        mgr.submit("j-after", "result = 'alive'", timeout_s=60)
        good = mgr.wait("j-after", timeout_s=60)
        eq(good["state"], "succeeded", "崩溃后管理器必须仍可派发新作业")
        eq(good["result"], "alive", "结果正确")


def test_restart_sweeps_stale_jobs_as_failed():
    base = tempfile.mkdtemp(prefix="ads_agent_cm_restart_")
    try:
        # 手工造一个"上次后端退出时留下"的作业目录：有 request、无终态标记
        jdir = os.path.join(base, "stale-job")
        os.makedirs(jdir)
        with open(os.path.join(jdir, "request.json"), "w", encoding="utf-8") as f:
            json.dump({"job_id": "stale-job", "code": "result = 1",
                       "inputs": None, "timeout_s": 60,
                       "created_ts": time.time()}, f)
        mgr = cm.ComputeManager(base_dir=base, max_workers=1)
        try:
            rec = mgr.inspect("stale-job")
            ok(rec is not None, "遗留作业必须仍有记录（可查询、可解释）")
            eq(rec["state"], "failed", "遗留作业必须标 failed")
            eq(rec["error_kind"], "backend_restart", "错误类别要能区分重启")
            contains(str(rec.get("error")), "重启", "文案要说明是后端重启")
        finally:
            mgr.shutdown_all(grace_s=0.2)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. 依赖探测：缺依赖明确报错，绝不静默换解释器
# ---------------------------------------------------------------------------

def test_missing_dependency_raises_env_error_with_interpreter():
    with Manager(max_workers=1) as mgr:
        real = cm._probe_dependencies
        cm._probe_dependencies = lambda exe, *a, **kw: ["numpy", "scipy"]
        try:
            with Manager(max_workers=1) as mgr2:
                err = None
                try:
                    mgr2.submit("j-env", "result = 1", timeout_s=10)
                except cm.ComputeEnvError as e:
                    err = e
                ok(err is not None, "缺依赖必须抛 ComputeEnvError")
                contains(str(err), "numpy", "错误里要列出缺失依赖")
                contains(str(err), "解释器", "错误里要说清用的是哪个解释器")
                eq(mgr2.inspect("j-env"), None, "缺依赖的作业不得留下记录")
        finally:
            cm._probe_dependencies = real


# ---------------------------------------------------------------------------
# 10. 关停：排队取消、运行回收
# ---------------------------------------------------------------------------

def test_shutdown_all_cancels_queue_and_reaps_running():
    base = tempfile.mkdtemp(prefix="ads_agent_cm_shutdown_")
    try:
        mgr = cm.ComputeManager(base_dir=base, max_workers=1, queue_capacity=4,
                                cancel_grace_s=0.3)
        mgr.submit("j-run", _SLOW_CODE, timeout_s=300)
        wait_until(lambda: mgr.inspect("j-run")["state"] == "running",
                   "运行作业")
        pids = [mgr.inspect("j-run")["worker_pid"]]
        mgr.submit("j-q", "result = 1", timeout_s=300)
        mgr.shutdown_all(grace_s=0.3)
        eq(mgr.inspect("j-q")["state"], "cancelled", "关停时排队作业标 cancelled")
        ok(mgr.inspect("j-run")["state"] in ("cancelled", "failed"),
           f"关停时运行作业要被收回：{mgr.inspect('j-run')['state']}")
        for pid in pids:
            wait_until(lambda: not _pid_alive(pid), "关停后不得留下 worker", timeout=20)
        # 关停后不再接受新作业
        err = None
        try:
            mgr.submit("j-new", "result = 1", timeout_s=10)
        except cm.ComputeError as e:
            err = e
        ok(err is not None, "关停后必须拒绝新作业")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# 11. snapshot()：/health 用的进程级汇总（不读磁盘、不改状态）
# ---------------------------------------------------------------------------

def test_snapshot_reports_counts_for_health_without_disk_io():
    with Manager(max_workers=1, queue_capacity=4) as mgr:
        snap = mgr.snapshot()
        eq(snap["max_workers"], 1, "max_workers 如实上报")
        eq(snap["queue_capacity"], 4, "排队容量如实上报")
        eq(snap["running"], 0, "空闲时没有运行中的作业")
        eq(snap["queued"], 0, "空闲时队列为空")
        eq(snap["cancel_requested"], 0, "空闲时没有取消中的作业")
        eq(snap["jobs"], [], "空闲时作业列表为空")
        ok(bool(snap["python_executable"]), "必须报出解释器路径，便于排查依赖问题")

        # 一个跑起来 + 一个排队的：/health 必须能同时看见两种
        mgr.submit("j-snap-run", _SLOW_CODE, timeout_s=300, label="快照用")
        wait_until(lambda: mgr.inspect("j-snap-run")["worker_pid"],
                   "作业进入运行态")
        mgr.submit("j-snap-q", "result = 1", timeout_s=300)
        wait_until(lambda: mgr.snapshot()["queued"] >= 1,
                   "排队作业应计入 queued")
        snap = mgr.snapshot()
        eq(snap["running"], 1, "运行中的作业数")
        eq(snap["queued"], 1, "排队中的作业数")
        states = {j["state"] for j in snap["jobs"]}
        ok("running" in states, f"作业列表必须包含运行中的作业：{snap['jobs']}")
        run_item = [j for j in snap["jobs"] if j["state"] == "running"][0]
        eq(run_item["job_id"], "j-snap-run", "作业 id 如实上报")
        ok(run_item.get("worker_pid"), "运行中作业必须带 worker_pid")
        ok(run_item.get("elapsed_s") is not None, "运行中作业应能报已用时长")

        # 排队中取消 → cancel_requested 计数看得见；终态后从作业列表消失
        mgr.cancel("j-snap-q")
        snap = mgr.snapshot()
        eq(snap["queued"], 0, "取消排队作业后 queued 归零")
        ok(not [j for j in snap["jobs"] if j["job_id"] == "j-snap-q"],
           f"已取消的排队作业不应再出现在活跃列表：{snap['jobs']}")

        # 请求取消运行中的作业 → 先如实进 cancel_requested
        mgr.cancel("j-snap-run")
        wait_until(lambda: mgr.inspect("j-snap-run")["state"] in
                   cm.TERMINAL_STATES, "运行作业收尾")
        snap = mgr.snapshot()
        eq(snap["running"], 0, "收尾后 running 归零")
        eq(snap["jobs"], [], "终态作业不再占用活跃列表")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "独立计算作业管理器（进程生命周期）"))
