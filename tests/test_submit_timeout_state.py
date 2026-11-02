"""submit 超时后作业真实状态的显式现状测试（不需要 ADS，纯标准库）。

背景（审计 2026-10-30，交付 2）：toolserver.submit() 的 HTTP 等待超时
（``done.wait(timeout+2.0)`` 到点 → ``_job_mark_timeout`` + ``TimeoutError``）
**不等于作业停止执行** —— pump 已经把作业交给处理器，后台线程无法被强杀。
本文件记录**当前**行为，作为后续改造的基线（改造后这里不得变红；若语义
被有意更改，应同步更新断言并在审计报告登记）：

1. submit 抛 TimeoutError 后，处理器线程仍在运行（作业仍在真实执行）；
2. 此窗口内 ``active_jobs()`` 如实报告该作业（timed_out=True），
   ``/health`` 的 busy / timed_out_count 与之一致 —— 不谎报空闲；
3. 处理器真正完成后，作业记录从 ``_active_jobs`` 被**摘除**
   （当前语义：摘除 = 真实收尾，没有独立的 "succeeded" 终态记录；
   ``/health`` 回到空闲）。结果只写进 submit 早已丢弃的 box，调用方
   已拿不到 —— 这就是"HTTP 请求结束 ≠ 执行结束"的现状证据。

纪律：临时数据目录；不依赖固定 sleep 制造竞态 —— 全部用事件 + 有界
deadline 轮询；后台线程（submit/pump）异常必须导致失败。

运行::

    python tests/test_submit_timeout_state.py
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

TOKEN = "submit-timeout-token-" + "K3mP" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_submit_timeout_")
_CFG = os.path.join(_TMP, "config.ini")
with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\nmodel = m\n\n"
        "[ads]\nhost = 127.0.0.1\nport = 8761\n"
        f"token = {TOKEN}\n\n"
        "[agent]\nsim_timeout = 60\n"
    )
os.environ["ADS_AGENT_CONFIG"] = _CFG
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    os.environ[_k] = ("," + _existing) if _existing else "127.0.0.1,localhost"

import authbridge  # noqa: E402,F401  — /execute 鉴权依赖
import toolserver  # noqa: E402

# 工具服务日志写临时目录 —— 绝不污染工程 logs/
toolserver._LOG_DIR = os.path.join(_TMP, "logs")
toolserver._LOG_FILE = os.path.join(toolserver._LOG_DIR, "ads_toolserver.log")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 处理器阻塞兜底：即使测试流程意外没释放，handler 也必须在有限时间内返回，
# 保证任何路径下测试进程都不会悬挂（这是兜底 deadline，不是竞态制造）。
_BACKSTOP_S = 25.0
_SUBMIT_TIMEOUT_S = 0.8        # submit 的 HTTP 等待预算（故意很短）

_ENTERED = threading.Event()   # 处理器已开始执行
_RELEASE = threading.Event()   # 测试释放处理器
_FINISHED = threading.Event()  # 处理器已返回
_HANDLER_RESULT: dict = {}


def _blocking_handler(args, ctx):
    """受控阻塞处理器：进入即登记，直到测试释放（或兜底到点）才完成。"""
    _ENTERED.set()
    released = _RELEASE.wait(_BACKSTOP_S)
    _FINISHED.set()
    _HANDLER_RESULT["released_by"] = "release_event" if released else "backstop"
    return {"ok": True}


class _FakeOps:
    def __init__(self, dispatch):
        self.DISPATCH = dispatch


toolserver._get_ads_ops = lambda **kwargs: _FakeOps(
    {"blocking_probe": _blocking_handler})

# 后台线程异常必须判失败：pump 线程的任何异常都记录在案，测试末尾断言为空
_PUMP_ERRORS: list = []
_PUMP_STOP = threading.Event()


def _pump_loop():
    while not _PUMP_STOP.is_set():
        try:
            toolserver.pump()
        except BaseException as e:  # noqa: BLE001 — 记录后退出，测试末尾判失败
            _PUMP_ERRORS.append(e)
            return
        time.sleep(0.005)


_PUMP_THREAD = threading.Thread(target=_pump_loop, daemon=True, name="test-pump")
_PUMP_THREAD.start()


def _reset() -> None:
    with toolserver._jobs_lock:
        toolserver._active_jobs.clear()
    while True:
        try:
            toolserver._jobs.get_nowait()
        except queue.Empty:
            break


def _wait_until(cond, timeout, msg=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _submit_async(name, args=None, timeout=_SUBMIT_TIMEOUT_S):
    """后台线程提交；异常（含 TimeoutError）捕获进 out dict，测试必须核验。"""
    out: dict = {}

    def run_it():
        try:
            out["result"] = toolserver.submit(name, args or {}, timeout=timeout)
        except BaseException as e:  # noqa: BLE001
            out["error"] = e

    t = threading.Thread(target=run_it, daemon=True, name="submit-blocking_probe")
    t.start()
    return t, out


_httpd = None


def _ensure_http():
    global _httpd
    if _httpd is None:
        _httpd = ThreadingHTTPServer(("127.0.0.1", 0), toolserver._Handler)
        _httpd.daemon_threads = True
        threading.Thread(target=_httpd.serve_forever, daemon=True).start()
    return _httpd


def _health():
    srv = _ensure_http()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/health")
    with _OPENER.open(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def test_submit_timeout_keeps_executing_and_reports_true_state():
    _reset()
    _ENTERED.clear(); _RELEASE.clear(); _FINISHED.clear()
    _HANDLER_RESULT.clear()

    t, out = _submit_async("blocking_probe")

    # 0. 处理器确实开始执行（作业从"排队"进入"执行中"）
    ok(_wait_until(_ENTERED.is_set, 10, "处理器未开始执行"),
       "pump 应取走作业并进入处理器")

    # 1. submit 等待超时（timeout+2s 宽限），抛 TimeoutError
    ok(_wait_until(lambda: "error" in out, _SUBMIT_TIMEOUT_S + 6.0,
                   "submit 未在预算内抛出 TimeoutError"),
       "submit 应在 done.wait(timeout+2.0) 到点后抛 TimeoutError")
    t.join(timeout=5)
    ok(not t.is_alive(), "submit 线程应已结束（异常已被捕获，未悬挂）")
    err = out.get("error")
    ok(isinstance(err, TimeoutError),
       f"应为 TimeoutError，实际 {type(err).__name__}: {err}")

    # 2. TimeoutError 后处理器仍在运行 —— 作业仍在真实执行
    ok(not _FINISHED.is_set(), "submit 超时后处理器不应已结束（作业仍在执行）")

    # 3. 窗口内 active_jobs() 如实报告：作业在跑 + 已打 timed_out 标记
    ok(_wait_until(lambda: any(j.get("timed_out") for j in toolserver.active_jobs()),
                   5, "active_jobs 未出现 timed_out 标记"),
       "_job_mark_timeout 应在超时窗口内把作业标记为 timed_out")
    jobs = toolserver.active_jobs()
    eq(len(jobs), 1, "窗口内应恰好有 1 个在跑作业")
    eq(jobs[0]["name"], "blocking_probe")
    eq(jobs[0]["timed_out"], True)
    # active_jobs() 公共视图不含 client_id；此处按任务允许用内部态核对一次
    # （注明：client_id 记录在 _active_jobs[jid]["client_id]，/health 不暴露）
    with toolserver._jobs_lock:
        recs = list(toolserver._active_jobs.values())
    eq(len(recs), 1)
    eq(recs[0]["client_id"], "", "submit 未传 client_id 时应如实记录为空")

    # 4. /health 展示语义：busy=True 且 timed_out_count 与真实执行并存
    def _health_marked():
        h = _health()
        return bool(h.get("busy")) and int(h.get("timed_out_count") or 0) >= 1
    ok(_wait_until(_health_marked, 5, "/health 未如实报告 timed_out 作业"),
       "/health 应报告 busy=True + timed_out_count>=1（标记与真实执行并存）")

    # 5. 释放处理器 → 作业真实完成 → 记录被摘除（当前收尾语义）
    _RELEASE.set()
    ok(_wait_until(_FINISHED.is_set, 10, "处理器未在释放后完成"),
       "释放后处理器应真实完成")
    ok(_wait_until(lambda: toolserver.active_jobs() == [], 10,
                   "作业完成后记录未被摘除"),
       "真实收尾后 _active_jobs 应摘除该作业（当前语义：摘除=收尾，"
       "无独立 succeeded 终态记录 —— 结果只留在已丢弃的 box 里）")

    # 6. /health 回到空闲
    ok(_wait_until(lambda: not bool(_health().get("busy")), 5,
                   "作业收尾后 /health 仍报 busy"),
       "全部作业收尾后 /health 应回到空闲")
    eq(_HANDLER_RESULT.get("released_by"), "release_event")

    # 7. 后台线程零异常（submit 线程的 TimeoutError 是被捕获的预期结果）
    eq(_PUMP_ERRORS, [], "pump 线程不得有未处理异常")


def _teardown():
    _PUMP_STOP.set()
    _PUMP_THREAD.join(timeout=3)
    if _httpd is not None:
        _httpd.shutdown()
        _httpd.server_close()
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run(globals())
    finally:
        _teardown()
    sys.exit(code)
