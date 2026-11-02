"""跨模块集成：轮次取消 → 计算作业取消 → worker 回收（不需要 ADS）。

这是本次修复最关键的链路，单独用**真实组件**验证：

    Turn.cancel() → /chat/cancel 语义 → turn.propagate_cancel()
        → compute_manager.cancel(job_id) → stop 标志 → terminate → 回收
        → Turn 里登记表如实反映"已请求取消"与"已退出"的区别

为什么不能只测 compute_manager：manager 自己取消自己，测不出"轮次身份是否
贯穿到作业"。这里走 agent.Turn 的真实登记表（register_job / current_jobs /
propagate_cancel）与真实的 _run_compute_tool，只把 LLM 与 ADS 工具服务换成
本地替身。

纪律：临时数据目录；事件 + 有界轮询；后台线程异常必须判失败。

运行::

    python tests/test_compute_turn_cancel.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run, skip  # noqa: E402

BACKEND = add_path("backend")
ADDON = add_path("addon", "ads_agent")

# 后端与插件共用同一份 config：先指到临时目录，再 import agent
_TMP = tempfile.mkdtemp(prefix="ads_agent_turn_cancel_")
_CFG = os.path.join(_TMP, "config.ini")
_JOB_DIR = os.path.join(_TMP, "compute_jobs")
with open(_CFG, "w", encoding="utf-8") as f:
    f.write(
        "[llm]\nmodel = m\n\n"
        "[ads]\nhost = 127.0.0.1\nport = 8761\n"
        "token = turn-cancel-token-0123456789abcdef0123456789abcdef\n\n"
        "[agent]\n"
        "compute_max_workers = 1\n"
        "compute_queue_capacity = 4\n"
        "compute_timeout_s = 120\n"
        f"compute_base_dir = {_JOB_DIR}\n"
    )
os.environ["ADS_AGENT_CONFIG"] = _CFG
os.environ["ADS_AGENT_DATA_DIR"] = os.path.join(_TMP, "data")

import agent  # noqa: E402
import config as config_mod  # noqa: E402
import compute_manager as cm  # noqa: E402


def _have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _pid_alive(pid) -> bool:
    """只读探测（Windows 上绝不能用 os.kill(pid, 0)：那会真杀进程）。"""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259     # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def wait_until(cond, msg="", timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{msg}")


_CFG_DICT = config_mod.load()

# 长计算：协作可取消（每轮 20ms，别把测试跑成几十分钟）
_LONG_CODE = """
import time
acc = 0.0
for i in range(3000):
    if check_cancel():
        progress('cancelled')
        break
    acc += i
    time.sleep(0.02)
result = {'acc': acc}
"""


def _new_turn(**kwargs):
    cfg = dict(_CFG_DICT)
    return agent.Turn(cfg, [], **kwargs)


def _reset_manager():
    mgr = agent._get_compute_manager(_CFG_DICT)
    ok(mgr is not None, "计算管理器必须可用（compute_manager 已随包）")
    return mgr


# ---------------------------------------------------------------------------
# 1. 身份贯穿：job_id 派发前登记，取消能找到它
# ---------------------------------------------------------------------------

def test_job_id_is_registered_before_dispatch_and_cancel_propagates():
    mgr = _reset_manager()
    turn = _new_turn()
    job_id = "turn-job-1"
    turn.register_job(job_id, "run_compute", "compute")
    jobs = turn.current_jobs()
    eq(len(jobs), 1, "派发前就要有登记（不能等异常才知道 job_id）")
    eq(jobs[0]["job_id"], job_id, "登记表必须能按 job_id 找到作业")
    eq(jobs[0]["kind"], "compute", "作业类型要区分计算/ADS")
    # 让 manager 真的有一个作业在跑（这样 propagate_cancel 才有真实回执）
    mgr.submit(job_id, _SLOW_TAIL, timeout_s=120)
    wait_until(lambda: mgr.inspect(job_id)["state"] == "running", "作业应开跑")
    turn.cancel()
    info = turn.propagate_cancel()
    ok("compute" in info, f"取消传播必须带上计算作业回执：{info}")
    receipt = info["compute"].get(job_id) or {}
    eq(receipt.get("ok"), True, f"计算作业取消必须受理：{receipt}")
    final = mgr.wait(job_id, timeout_s=40)
    ok(final["state"] in ("cancelled", "succeeded"), f"最终状态：{final['state']}")
    # 作业未确认终态前不得清掉登记（"取消成功"≠"已退出"由 state 表达）
    turn.clear_job(job_id)
    eq(turn.current_jobs(), [], "确认终态后登记表才清空")


_SLOW_TAIL = """
import time
acc = 0.0
for i in range(3000):
    if check_cancel():
        progress('cancelled')
        break
    acc += i
    time.sleep(0.02)
result = {'acc': acc}
"""


# ---------------------------------------------------------------------------
# 2. 真实调用链：_run_compute_tool 跑真实 worker，取消后如实 cancelled
# ---------------------------------------------------------------------------

def test_run_compute_tool_executes_on_worker_and_reports_partial():
    if not _have_numpy():
        skip("缺 numpy/scipy")
    mgr = _reset_manager()
    turn = _new_turn()
    code = ("import numpy as np\n"
            "from scipy.optimize import minimize\n"
            "n = int(inputs['n'])\n"
            "rng = np.random.default_rng(7)\n"
            "a = rng.standard_normal((n, n)); b = rng.standard_normal(n)\n"
            "f = lambda x: float((a @ x - b) @ (a @ x - b))\n"
            "res = minimize(f, np.zeros(n), method='L-BFGS-B',"
            " options={'maxiter': 40})\n"
            "result = {'fun': float(res.fun)}\n")
    job_id = "turn-compute-ok"
    turn.register_job(job_id, "run_compute", "compute")
    out = turn._run_compute_tool({"code": code, "inputs": {"n": 20},
                                  "timeout_s": 120}, job_id)
    eq(out.get("job_id"), job_id, "结果必须带回同一个 job_id")
    eq(out.get("ok"), True, f"计算应成功：{out.get('error')}")
    eq(out.get("state"), "succeeded", "状态")
    eq(out.get("partial"), False, "完整结果不得标 partial")
    ok(isinstance(out.get("result"), dict), "必须交出结构化结果")
    ok("fun" in out["result"], "结果里要有目标函数值")
    turn.clear_job(job_id)


def test_cancelled_turn_yields_cancelled_partial_result_not_success():
    mgr = _reset_manager()
    turn = _new_turn()
    job_id = "turn-compute-cancel"
    out = {}

    def _run():
        turn.register_job(job_id, "run_compute", "compute")
        try:
            out.update(turn._run_compute_tool(
                {"code": _LONG_CODE, "inputs": {}, "timeout_s": 120}, job_id))
        except Exception as e:  # noqa: BLE001
            out["raised"] = f"{type(e).__name__}: {e}"
        finally:
            turn.clear_job(job_id)

    t = threading.Thread(target=_run, daemon=True, name="turn-compute-cancel")
    t.start()
    wait_until(lambda: mgr.inspect(job_id) is not None
               and mgr.inspect(job_id)["state"] == "running",
               "计算作业应先跑起来", timeout=30)
    pid = mgr.inspect(job_id)["worker_pid"]
    turn.cancel()
    turn.propagate_cancel()
    t.join(timeout=90)
    ok(not t.is_alive(), "取消后工具调用必须尽快返回，不能挂到超时")
    eq(out.get("raised"), None, f"取消不应抛出未处理异常：{out.get('raised')}")
    ok(out.get("ok") in (False, None),
       f"被取消的计算绝不能报成功：{out}")
    ok(out.get("state") in ("cancelled", "timed_out"),
       f"最终状态要如实：{out.get('state')}")
    if out.get("result") is not None:
        eq(out.get("partial"), True, "中途取消交回的结果必须标 partial")
    wait_until(lambda: not _pid_alive(pid), "取消后 worker 必须被回收", timeout=30)


# ---------------------------------------------------------------------------
# 3. 超时后查询仍能看到真实状态，且不自动重做
# ---------------------------------------------------------------------------

def test_timed_out_compute_is_reported_and_not_retried():
    if not _have_numpy():
        skip("缺 numpy")
    mgr = _reset_manager()
    with mgr._lock:
        before_ids = set(mgr._records)      # 调用前已存在的作业集合
    turn = _new_turn()
    job_id = "turn-compute-timeout"
    code = ("import numpy as np\n"
            "a = np.ones((1200, 1200))\n"
            "for _ in range(100000):\n"
            "    b = a @ a\n"
            "result = float(b[0, 0])\n")
    turn.register_job(job_id, "run_compute", "compute")
    out = turn._run_compute_tool({"code": code, "inputs": {}, "timeout_s": 2},
                                 job_id)
    eq(out.get("state"), "timed_out", f"超出执行预算必须 timed_out：{out}")
    ok(out.get("ok") in (False, None), "超时不得报成功")
    rec = mgr.inspect(job_id)
    ok(rec is not None, "超时后仍必须能查询到该作业的真实状态")
    eq(rec["state"], "timed_out", "查询到的状态与调用方一致")
    # 不许自动重做：管理器中新增的作业只能是这一个，且没有第二个同名作业
    with mgr._lock:
        after_ids = set(mgr._records)
    eq(after_ids - before_ids, {job_id},
       f"超时后不得偷偷再提交一遍：新增作业 {after_ids - before_ids}")
    eq([j for j in after_ids if j.startswith(job_id)], [job_id],
       "同一个逻辑作业不得留下重复的 job_id")
    turn.clear_job(job_id)


# ---------------------------------------------------------------------------
# 4. 队列满：明确 busy，不改状态、不重试
# ---------------------------------------------------------------------------

def test_queue_full_surfaces_as_busy_without_side_effects():
    if not _have_numpy():
        skip("缺 numpy")
    mgr = _reset_manager()
    # 用光唯一的并发槽 + 占满队列
    occupy = "result = 1"
    mgr.submit("occupy-run", "import time\nfor _ in range(600): time.sleep(0.05)\n"
                             "result = 1", timeout_s=300)
    wait_until(lambda: mgr.inspect("occupy-run")["state"] == "running",
               "占位作业")
    queued_ids = []
    for i in range(4):     # capacity = 4
        jid = f"occupy-q{i}"
        mgr.submit(jid, occupy, timeout_s=300)
        queued_ids.append(jid)
    turn = _new_turn()
    job_id = "turn-compute-busy"
    turn.register_job(job_id, "run_compute", "compute")
    out = turn._run_compute_tool({"code": "result = 1", "inputs": {},
                                  "timeout_s": 10}, job_id)
    eq(out.get("kind"), "busy", f"队列满必须给出 busy：{out}")
    eq(out.get("state"), "submit_failed", "未受理就不得假装有状态")
    contains(str(out.get("error")), "排队", "文案要说明排队满")
    turn.clear_job(job_id)
    for jid in queued_ids:
        mgr.cancel(jid)
    mgr.cancel("occupy-run")


# ---------------------------------------------------------------------------
# 5. 普通 ADS 工具也走同一条登记/取消链（不接 worker）
# ---------------------------------------------------------------------------

def test_ads_tool_job_is_also_registered_and_cancelled_via_cancel_api():
    calls = {}

    class _FakeTools:
        AdsToolError = agent.tools_mod.AdsToolError
        AdsToolTimeout = agent.tools_mod.AdsToolTimeout
        AdsToolBusy = agent.tools_mod.AdsToolBusy

        @staticmethod
        def cancel_ads_jobs(cfg, job_ids):
            calls["cancel"] = list(job_ids)
            return {"cancelled": [], "running": list(job_ids), "unknown": []}

    real = agent.tools_mod
    agent.tools_mod = _FakeTools()
    try:
        turn = _new_turn()
        turn.register_job("ads-job-9", "run_python", "ads")
        info = turn.propagate_cancel()
        eq(calls.get("cancel"), ["ads-job-9"],
           "普通 ADS 工具作业必须通过 /cancel 取消（job_id 一致）")
        ok("ads" in info, f"回执里要能区分 ADS 与计算：{info}")
    finally:
        agent.tools_mod = real


def test_propagate_cancel_is_idempotent():
    calls = []

    class _FakeTools:
        AdsToolError = agent.tools_mod.AdsToolError
        AdsToolTimeout = agent.tools_mod.AdsToolTimeout
        AdsToolBusy = agent.tools_mod.AdsToolBusy

        @staticmethod
        def cancel_ads_jobs(cfg, job_ids):
            calls.append(list(job_ids))
            return {"cancelled": [], "running": list(job_ids), "unknown": []}

    real = agent.tools_mod
    agent.tools_mod = _FakeTools()
    try:
        turn = _new_turn()
        turn.register_job("ads-j", "get_workspace_info", "ads")
        turn.cancel()
        turn.propagate_cancel()
        turn.propagate_cancel()     # 幂等：用户连点两次"停止"
        eq(len(calls), 2, "重复取消不得崩溃，也不得清空登记表")
        eq(turn.current_jobs()[0]["job_id"], "ads-j",
           "取消成功不等于已退出：登记必须还在")
    finally:
        agent.tools_mod = real


if __name__ == "__main__":
    try:
        raise SystemExit(run(globals(), "轮次取消 → 计算作业取消 → worker 回收"))
    finally:
        mgr = agent._COMPUTE_MANAGER
        if mgr is not None:
            try:
                mgr.shutdown_all(grace_s=0.5)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(_TMP, ignore_errors=True)
