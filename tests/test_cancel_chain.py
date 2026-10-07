"""取消与超时链路回归（2026-10-02 优化轮，不需要 ADS）。

覆盖用户取消沿调用链传播：面板 stop() → 后端 /chat/cancel → agent 不再
发起新请求/派发新工具 → toolserver 对排队作业跳过、对执行中作业打标记。
以及超时分类：排队超时（未执行）与执行超时（可能仍在收尾）分开报告，
队列满时返回忙碌而不是无限堆积，pump 有时间预算不占死界面线程。

运行::

    python tests/test_cancel_chain.py
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

ADDON = add_path("addon", "ads_agent")
BACKEND = add_path("backend")

TOKEN = "cancel-chain-token-" + "B2c3" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_cancel_")
_CFG = os.path.join(_TMP, "config.ini")
with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\nmodel = m\n\n"
        "[ads]\nhost = 127.0.0.1\nport = 8761\n"
        f"token = {TOKEN}\n\n"
        "[agent]\nsim_off_main_thread = true\n"
        "tool_queue_capacity = 3\npump_max_jobs = 2\n"
    )
os.environ["ADS_AGENT_CONFIG"] = _CFG

import authbridge  # noqa: E402

import toolserver  # noqa: E402

toolserver._LOG_DIR = os.path.join(_TMP, "logs")


def _reset_toolserver():
    """测试间清空队列与登记表，避免互相污染。"""
    while True:
        try:
            toolserver._jobs.get_nowait()
        except Exception:
            break
    with toolserver._jobs_lock:
        toolserver._active_jobs.clear()
    with toolserver._cancel_lock:
        toolserver._cancelled_ids.clear()


def _echo(args, ctx=None):
    return {"ok": True, "echo": args}


def _slow(args, ctx=None):
    time.sleep(0.3)
    return {"ok": True}


# ---------------------------------------------------------------------------
# toolserver：排队中取消
# ---------------------------------------------------------------------------

def test_queued_job_cancelled_before_pump_is_skipped():
    _reset_toolserver()
    out = {}

    def _submit():
        try:
            out["result"] = toolserver.submit("echo", {"x": 1}, timeout=5,
                                              client_id="job-cancel-1")
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)

    t = threading.Thread(target=_submit, daemon=True)
    t.start()
    time.sleep(0.2)                      # 让作业入队、但还没有 pump
    info = toolserver.request_cancel(["job-cancel-1"])
    eq(info["cancelled"], ["job-cancel-1"])
    toolserver.pump()                    # 主线程泵：应跳过执行
    t.join(timeout=3)
    ok("error" in out, f"取消的作业不应执行成功：{out}")
    contains(out.get("error", ""), "取消")
    ok("result" not in out)
    _reset_toolserver()


def _echo(args, ctx=None):
    return {"ok": True, "echo": args}


def _slow(args, ctx=None):
    time.sleep(0.3)
    return {"ok": True}


class _FakeAdsOps:
    """pump() 经 _get_ads_ops() 取 DISPATCH；测试里换成假模块，不 import 真身。"""
    DISPATCH = {"echo": _echo, "slow": _slow}


toolserver._get_ads_ops = lambda: _FakeAdsOps


def test_running_job_cancel_is_only_flagged():
    _reset_toolserver()
    # 造一个"正在执行"的作业记录（queued=False = 已开跑）；
    # 线程无法强杀，只能打标记等安全边界收尾
    jid = toolserver._job_begin("run_simulation", client_id="job-run-1")
    with toolserver._jobs_lock:
        toolserver._active_jobs[jid]["queued"] = False
    info = toolserver.request_cancel(["job-run-1"])
    eq(info["running"], ["job-run-1"], "已开跑的作业归入 running，如实说明")
    eq(info["cancelled"], [])
    jobs = toolserver.active_jobs()
    ok(any(j.get("cancel_requested") for j in jobs),
       "执行中的作业应带 cancel_requested 标记")
    toolserver._job_end(jid)
    _reset_toolserver()


def test_expired_job_is_skipped_as_queue_timeout():
    _reset_toolserver()
    out = {}

    def _submit():
        try:
            out["result"] = toolserver.submit("echo", {"x": 1}, timeout=0.2,
                                              client_id="job-exp-1")
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)

    t = threading.Thread(target=_submit, daemon=True)
    t.start()
    time.sleep(1.2)                      # 截止时间（下限 1s）已过，还没 pump
    toolserver.pump()
    t.join(timeout=3)
    ok("error" in out, f"过期作业不应执行：{out}")
    contains(out.get("error", ""), "排队超时")
    not_contains(out.get("error", ""), "执行超时", "排队超时不能和执行超时混为一谈")
    _reset_toolserver()


def test_queue_capacity_rejects_with_busy():
    _reset_toolserver()
    blocked = []

    def _submit(n):
        try:
            toolserver.submit("echo", {"n": n}, timeout=5, client_id=f"q-{n}")
        except toolserver.BusyError as e:
            blocked.append(str(e))

    threads = []
    for n in range(5):
        t = threading.Thread(target=_submit, args=(n,), daemon=True)
        threads.append(t)
        t.start()
        time.sleep(0.15)                 # 逐个入队，避免 check-put 竞争拉平结果
    for t in threads:
        t.join(timeout=3)
    # 容量 3：前 3 个入队，后 2 个 BusyError
    eq(len(blocked), 2, f"容量 3 应拒绝 2 个请求，实际拒绝 {len(blocked)}")
    contains(blocked[0], "队列已满")
    _reset_toolserver()


def test_pump_budget_limits_jobs_per_tick():
    _reset_toolserver()
    # 容量 3，但直接手动塞满队列绕过 submit 的容量检查
    done_events = []
    for i in range(5):
        ev = threading.Event()
        box = {}
        deadline = time.time() + 30
        toolserver._jobs.put((100 + i, "echo", {"i": i}, box, ev, deadline, ""))
        done_events.append(ev)
    toolserver.pump()                    # pump_max_jobs = 2
    remaining = toolserver._jobs.qsize()
    eq(remaining, 3, f"每次 pump 最多执行 2 个，应剩 3 个，实际剩 {remaining}")
    toolserver.pump()
    toolserver.pump()
    eq(toolserver._jobs.qsize(), 0)
    ok(all(ev.is_set() for ev in done_events))
    _reset_toolserver()


# ---------------------------------------------------------------------------
# agent：取消沿轮次传播
# ---------------------------------------------------------------------------

def test_turn_cancelled_before_run_never_calls_llm():
    import agent

    calls = []

    class _FakeLlm:
        @staticmethod
        def chat_stream(cfg, messages, tools=None, timeout=240, **kw):
            calls.append(1)
            return {"role": "assistant", "content": "hi"}, None

    real_llm = agent.llm
    agent.llm = _FakeLlm()
    try:
        turn = agent.Turn({"llm_model": "m", "max_tool_steps": 5}, [])
        turn.cancel()
        events = []
        turn.run(events.append)
        types = [e.get("type") for e in events]
        eq(calls, [], "已取消的轮次不能发起模型请求")
        ok("cancelled" in types, f"应发出 cancelled 事件：{types}")
        ok("done" not in types and "error" not in types,
           "取消不是完成也不是错误")
    finally:
        agent.llm = real_llm


def test_turn_cancelled_during_model_response_discards_tool_calls():
    """取消发生在模型响应期间：响应被丢弃，工具绝不派发。"""
    import agent

    tool_calls_made = []

    class _FakeLlm:
        @staticmethod
        def chat_stream(cfg, messages, tools=None, timeout=240, **kw):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "function": {
                        "name": "get_workspace_info", "arguments": "{}"}}]}, None

    class _FakeTools:
        AdsToolError = Exception

        @staticmethod
        def call(cfg, name, args):
            tool_calls_made.append(name)
            return {}

        @staticmethod
        def is_local(name):
            return False

    real_llm, real_tools = agent.llm, agent.tools_mod
    agent.llm = _FakeLlm()
    agent.tools_mod = _FakeTools()
    try:
        turn = agent.Turn({"llm_model": "m", "max_tool_steps": 5}, [])
        orig_stream = _FakeLlm.chat_stream

        def _cancel_mid_stream(*a, **kw):
            turn.cancel()                # 用户在模型响应期间点了停止
            return orig_stream(*a, **kw)

        _FakeLlm.chat_stream = staticmethod(_cancel_mid_stream)
        events = []
        turn.run(events.append)
        types = [e.get("type") for e in events]
        eq(tool_calls_made, [], "取消后模型给出的工具调用绝不能执行")
        ok("cancelled" in types, f"应发出 cancelled 事件：{types}")
    finally:
        agent.llm, agent.tools_mod = real_llm, real_tools


def test_turn_cancelled_between_tools_skips_dispatch():
    import agent

    executed = []

    class _FakeTools:
        AdsToolError = Exception
        AdsToolTimeout = type("AdsToolTimeout", (Exception,), {})
        AdsToolBusy = type("AdsToolBusy", (Exception,), {})

        @staticmethod
        def call(cfg, name, args):
            executed.append(name)
            return {"ok": True}
            yield  # pragma: no cover

        @staticmethod
        def is_local(name):
            return False

    responses = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "get_workspace_info",
                                                  "arguments": "{}"}}]},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c2", "function": {"name": "list_designs",
                                                  "arguments": "{}"}}]},
        {"role": "assistant", "content": "done-text"},
    ]
    idx = {"i": 0}

    def _fake_stream(cfg, messages, tools=None, timeout=240, **kw):
        r = responses[min(idx["i"], len(responses) - 1)]
        idx["i"] += 1
        if idx["i"] == 2:
            turn.cancel()                # 第一个工具执行完、第二个模型响应后取消
        return r, None

    class _FakeLlm:
        chat_stream = staticmethod(_fake_stream)

    real_llm, real_tools = agent.llm, agent.tools_mod
    agent.llm, agent.tools_mod = _FakeLlm(), _FakeTools()
    try:
        turn = agent.Turn({"llm_model": "m", "max_tool_steps": 10}, [])
        events = []
        turn.run(events.append)
        eq(executed, ["get_workspace_info"], "取消前派发的工具正常执行，取消后的跳过")
        types = [e.get("type") for e in events]
        ok("cancelled" in types)
        ok("tool_result" in types)
    finally:
        agent.llm, agent.tools_mod = real_llm, real_tools


# ---------------------------------------------------------------------------
# design_service：取消保留产物、不自动重跑
# ---------------------------------------------------------------------------

def test_design_cancel_before_sim_keeps_state_and_persists():
    import design_job as dj
    import design_service as dsvc

    root = tempfile.mkdtemp(prefix="ads_agent_cancel_job_")
    try:
        spec = dsvc.build_spec({
            "design": {"library": "AI_lib", "cell": "Amp"},
            "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                         "target": 15.0, "unit": "dB"}],
            "band": {"start": 2.3, "stop": 2.5, "unit": "GHz"},
        })
        cancel_event = threading.Event()
        cancel_event.set()               # 开始前就取消
        job = dsvc.run_design({}, spec, root, cancel_event=cancel_event)
        eq(job.stage, dj.STAGE_FAILED)
        contains(job.error, "取消")
        contains(job.error, "仿真未开始")
        eq(job.artifacts.get("dataset_path") or "", "", "没有仿真就没有数据集")
        # 已落盘可恢复
        loaded = dj.load_job(root, job.job_id)
        ok(loaded is not None, "取消状态必须已持久化")
        eq(loaded.stage, dj.STAGE_FAILED)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_design_cancel_after_sim_keeps_dataset_and_stops_evaluation():
    import design_job as dj
    import design_service as dsvc

    root = tempfile.mkdtemp(prefix="ads_agent_cancel_job2_")
    try:
        spec = dsvc.build_spec({
            "design": {"library": "AI_lib", "cell": "Amp"},
            "metrics": [{"kind": "min_in_band", "expr": "dB(S(2,1))",
                         "target": 15.0, "unit": "dB"}],
            "band": {"start": 2.3, "stop": 2.5, "unit": "GHz"},
        })
        cancel_event = threading.Event()

        sim_calls = []

        def _fake_call(cfg, name, args, job_id=""):
            sim_calls.append((name, job_id))
            if name == "run_simulation":
                # 仿真进行中用户取消 —— 仿真本身无法中断，跑完返回
                cancel_event.set()
                return {"status": "done", "dataset_path": "E:/x/data.ds",
                        "output_dir": "E:/x/sim1", "netlist_path": "E:/x/netlist"}
            raise AssertionError(f"取消后不应再调用 {name}")

        real_tools = dsvc.tools_mod

        class _FakeTools:
            AdsToolError = Exception
            call = staticmethod(_fake_call)

        dsvc.tools_mod = _FakeTools()
        try:
            job = dsvc.run_design({}, spec, root, cancel_event=cancel_event)
        finally:
            dsvc.tools_mod = real_tools

        sim_only = [c for c in sim_calls if c[0] in ("run_simulation", "read_traces")]
        eq([c[0] for c in sim_only], ["run_simulation"],
           "取消后不能再读数据/评估（get_workspace_info 是工作区核查，属正常）")
        eq(job.artifacts.get("dataset_path"), "E:/x/data.ds",
           "已生成的数据集必须保留")
        ok("ads_job_id" in (job.sim or {}), "仿真作业标识应记录在任务上（供取消转发）")
        contains(job.error, "取消")
        contains(job.error, "数据集")
        ok(all("pass" not in m for m in job.metrics),
           "未评估就没有指标结论：metrics 里只有定义，没有 pass 字段")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# tools.call：超时/忙碌分类
# ---------------------------------------------------------------------------

def test_tools_error_classes_are_distinct():
    import tools as tools_mod
    ok(issubclass(tools_mod.AdsToolTimeout, tools_mod.AdsToolError))
    ok(issubclass(tools_mod.AdsToolBusy, tools_mod.AdsToolError))
    e = tools_mod.AdsToolTimeout("timeout", job_id="abc")
    eq(e.job_id, "abc", "超时异常必须带作业标识，供查询后续状态")


if __name__ == "__main__":
    sys.exit(run(globals(), "取消与超时链路"))
