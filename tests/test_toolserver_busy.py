"""toolserver 忙碌状态准确性测试（不需要 ADS，纯标准库）。

覆盖第三轮评审第 3 项「忙碌状态准确性」：

``/health`` 过去只有一个"当前作业"槽位，而 ``pump()`` 的 ``finally`` 无条件清空它 ——
于是 ``ctx.defer()`` 把仿真转后台的**那一刻**，忙碌状态就被清掉了：整个仿真期间
``/health`` 都谎报空闲（面板/自检据此会以为 ADS 已经空下来）。多个作业同时在跑时，
一个结束也会把别人的状态一起清掉。

现在改为按作业登记：作业从**入队**起就在 ``_active_jobs`` 里，只有它**真正收尾**
（成功 / 失败 / 后台线程结束）才摘掉。本文件验证：

* 后台仿真期间 /health 持续报告运行中，作业结束后才回到空闲；
* 多个并发作业逐个记录，一个结束不影响其它；
* 请求已超时但后台线程还在跑的作业**仍算运行中**，并额外标注 ``timed_out``
  （不谎报空闲）；
* 处理器抛异常 / 未知工具 / 正常返回都能正确收尾，不会漏掉也不会卡住请求；
* 重复收尾只认第一次；
* HTTP 层 /execute 鉴权与 /health 字段完整。

运行::

    python tests/test_toolserver_busy.py
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

TOKEN = "busy-test-token-" + "A1b2" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_busy_")
_CFG = os.path.join(_TMP, "config.ini")
with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\nmodel = m\n\n"
        "[ads]\nhost = 127.0.0.1\nport = 8761\n"
        f"token = {TOKEN}\n\n"
        "[agent]\nsim_off_main_thread = true\nsim_timeout = 900\n"
    )
os.environ["ADS_AGENT_CONFIG"] = _CFG

import authbridge  # noqa: E402

import toolserver  # noqa: E402

# 工具服务日志写临时目录 —— 绝不能污染工程的 logs/
toolserver._LOG_DIR = os.path.join(_TMP, "logs")
toolserver._LOG_FILE = os.path.join(toolserver._LOG_DIR, "ads_toolserver.log")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

class _Pump:
    """替身 QTimer：持续把队列里的作业交给 pump()（真实环境由 Qt 定时器驱动）。"""

    def __init__(self):
        self._enabled = threading.Event()
        self._enabled.set()
        self._thread = None

    def start(self):
        if self._thread is None:
            def loop():
                while True:
                    if self._enabled.is_set():
                        try:
                            toolserver.pump()
                        except Exception:  # noqa: BLE001 — 别把测试线程打死
                            pass
                    time.sleep(0.005)
            self._thread = threading.Thread(target=loop, daemon=True, name="test-pump")
            self._thread.start()
        return self

    def pause(self):
        self._enabled.clear()

    def resume(self):
        self._enabled.set()


_PUMP = _Pump().start()


class _FakeOps:
    def __init__(self, dispatch):
        self.DISPATCH = dispatch


def _install(dispatch) -> None:
    toolserver._get_ads_ops = lambda: _FakeOps(dispatch)


def _reset() -> None:
    with toolserver._jobs_lock:
        toolserver._active_jobs.clear()
    while True:
        try:
            toolserver._jobs.get_nowait()
        except queue.Empty:
            break


def _wait(cond, msg="", timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待超时：{msg}")


def _deferring(sink):
    """处理器：登记 ctx 后立刻转后台（模拟 run_simulation 的主线程阶段）。"""
    def handler(args, ctx):
        sink.append(ctx)
        ctx.defer()
        return {"status": "running"}
    return handler


def _immediate(args, ctx):
    return {"ok": True}


def _submit_async(name, args=None, timeout=15.0):
    out: dict = {}

    def run_it():
        try:
            out["result"] = toolserver.submit(name, args or {}, timeout=timeout)
        except BaseException as e:  # noqa: BLE001
            out["error"] = e

    t = threading.Thread(target=run_it, daemon=True, name=f"submit-{name}")
    t.start()
    return t, out


_http = None


def _ensure_http():
    global _http
    if _http is None:
        _http = ThreadingHTTPServer(("127.0.0.1", 0), toolserver._Handler)
        _http.daemon_threads = True
        threading.Thread(target=_http.serve_forever, daemon=True).start()
    return _http


def _health():
    srv = _ensure_http()
    with _OPENER.open(f"http://127.0.0.1:{srv.server_address[1]}/health", timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post(path, body=None, token=None):
    srv = _ensure_http()
    headers = {"Content-Type": "application/json"}
    if token:
        headers[authbridge.header_name()] = token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}{path}", data=data,
        headers=headers, method="POST",
    )
    try:
        with _OPENER.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _log_text() -> str:
    try:
        with open(toolserver._LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _teardown():
    if _http is not None:
        _http.shutdown()
        _http.server_close()
    shutil.rmtree(_TMP, ignore_errors=True)


# ---------------------------------------------------------------------------
# 后台作业期间必须持续报告运行中
# ---------------------------------------------------------------------------

def test_job_stays_busy_until_it_settles():
    _reset()
    sink: list = []
    _install({"run_simulation": _deferring(sink)})

    t, out = _submit_async("run_simulation", {"library": "L", "cell": "C"})
    _wait(lambda: sink, "主线程阶段应被执行")

    jobs = toolserver.active_jobs()
    eq(len(jobs), 1, "后台仿真开始后 /health 应继续报告运行中（原先会谎报空闲）")
    eq(jobs[0]["name"], "run_simulation")
    eq(jobs[0]["timed_out"], False)
    ok(jobs[0]["elapsed_s"] >= 0)
    eq(out, {}, "此时请求还没结束（仿真在后台跑）")

    time.sleep(0.4)
    eq(len(toolserver.active_jobs()), 1, "忙碌状态在后台仿真期间自己消失了")

    sink[0].finish({"status": "done"})
    t.join(10)
    eq(out.get("result"), {"status": "done"})
    _wait(lambda: not toolserver.active_jobs(), "作业结束后应从忙碌表摘掉")


def test_job_counts_as_busy_before_the_main_thread_picks_it_up():
    _reset()
    _install({"x": _immediate})
    _PUMP.pause()                     # 模拟主线程正忙，还没轮到 pump
    try:
        t, out = _submit_async("x", timeout=10)
        time.sleep(0.2)
        eq(len(toolserver.active_jobs()), 1, "已入队但还没执行时也应算运行中")
    finally:
        _PUMP.resume()
    t.join(10)
    eq(out.get("result"), {"ok": True})
    _wait(lambda: not toolserver.active_jobs())


def test_concurrent_jobs_do_not_clear_each_other():
    _reset()
    sink: list = []
    _install({"run_simulation": _deferring(sink), "run_python": _deferring(sink)})

    t1, out1 = _submit_async("run_simulation", timeout=25)
    _wait(lambda: len(sink) == 1)
    t2, out2 = _submit_async("run_python", timeout=25)
    _wait(lambda: len(sink) == 2)
    eq(len(toolserver.active_jobs()), 2, "两个作业应同时被计为运行中")

    sink[0].finish({"n": 1})
    t1.join(10)
    eq(out1.get("result"), {"n": 1})
    jobs = toolserver.active_jobs()
    eq(len(jobs), 1, "一个作业结束把另一个作业的忙碌状态也清掉了")
    eq(jobs[0]["name"], "run_python", "留下的应是仍在跑的那个")

    sink[1].fail("boom")
    t2.join(10)
    ok(isinstance(out2.get("error"), RuntimeError), f"失败的作业应回错: {out2}")
    _wait(lambda: not toolserver.active_jobs())


def test_timeout_keeps_reporting_busy_and_marks_it():
    _reset()
    sink: list = []
    _install({"run_simulation": _deferring(sink)})

    t, out = _submit_async("run_simulation", timeout=0.5)
    _wait(lambda: sink, "主线程阶段应被执行")
    t.join(15)
    ok(isinstance(out.get("error"), TimeoutError), f"应当超时: {out}")

    jobs = toolserver.active_jobs()
    eq(len(jobs), 1,
       "超时后后台线程还在跑，不能谎报空闲（否则面板会以为 ADS 空了）")
    eq(jobs[0]["timed_out"], True, "应标注这次请求已经超时放弃")

    sink[0].finish({"late": True})
    _wait(lambda: not toolserver.active_jobs(), "后台真正结束后应摘掉")


def test_double_settle_only_counts_once():
    _reset()
    sink: list = []
    _install({"run_simulation": _deferring(sink)})

    t, out = _submit_async("run_simulation", timeout=15)
    _wait(lambda: sink)
    sink[0].finish({"first": 1})
    sink[0].fail("second")            # 迟到的收尾必须被忽略
    t.join(10)
    eq(out.get("result"), {"first": 1}, "第二次收尾覆盖了第一次")
    _wait(lambda: not toolserver.active_jobs())


# ---------------------------------------------------------------------------
# 各种收尾路径都不能漏掉 / 不能卡住
# ---------------------------------------------------------------------------

def test_handler_exception_clears_the_job_and_unblocks_the_request():
    _reset()

    def boom(args, ctx):
        raise ValueError("bad")

    _install({"x": boom})
    t, out = _submit_async("x", timeout=10)
    t.join(15)
    ok(isinstance(out.get("error"), RuntimeError), f"异常应回给调用方: {out}")
    contains(str(out["error"]), "ValueError")
    eq(toolserver.active_jobs(), [], "抛异常的作业应从忙碌表摘掉")


def test_handler_system_exit_does_not_stop_the_pump():
    _reset()

    def exit_now(args, ctx):
        raise SystemExit("script stopped")

    _install({"exit_now": exit_now, "next": _immediate})
    t, out = _submit_async("exit_now", timeout=5)
    t.join(7)
    ok(isinstance(out.get("error"), RuntimeError), f"SystemExit 应返回错误: {out}")
    contains(str(out["error"]), "SystemExit")
    eq(toolserver.active_jobs(), [])
    eq(toolserver.submit("next", {}, timeout=5), {"ok": True},
       "后续作业仍应执行")


def test_unknown_tool_clears_the_job():
    _reset()
    _install({})
    t, out = _submit_async("no_such_tool", timeout=10)
    t.join(15)
    ok(isinstance(out.get("error"), RuntimeError))
    contains(str(out["error"]), "未知工具")
    eq(toolserver.active_jobs(), [])


def test_synchronous_job_clears_immediately():
    _reset()
    _install({"run_python": _immediate})
    eq(toolserver.submit("run_python", {}, timeout=10), {"ok": True})
    eq(toolserver.active_jobs(), [], "同步作业结束后不应留在忙碌表里")


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def test_health_reports_every_field():
    _reset()
    status, data = _health()
    eq(status, 200)
    eq(data.get("status"), "ok")
    eq(data.get("auth_required"), True)
    for key in ("busy", "busy_count", "jobs", "timed_out_count",
                "elapsed_s", "sim_off_main_thread"):
        ok(key in data, f"/health 缺少字段 {key}")
    eq(data["busy"], None)
    eq(data["busy_count"], 0)
    eq(data["jobs"], [])
    eq(data["timed_out_count"], 0)


def test_health_stays_busy_for_the_whole_background_simulation():
    _reset()
    sink: list = []
    _install({"run_simulation": _deferring(sink)})

    t, _out = _submit_async("run_simulation", timeout=20)
    _wait(lambda: sink, "主线程阶段应被执行")

    status, data = _health()
    eq(status, 200)
    eq(data["busy"], "run_simulation", "/health 在后台仿真期间谎报空闲")
    eq(data["busy_count"], 1)
    eq(len(data["jobs"]), 1)
    eq(data["jobs"][0]["name"], "run_simulation")
    eq(data["jobs"][0]["timed_out"], False)

    sink[0].finish({"ok": True})
    t.join(10)
    _wait(lambda: _health()[1]["busy"] is None, "作业结束后 /health 应回到空闲")
    eq(_health()[1]["jobs"], [])


def test_health_tracks_multiple_jobs_independently():
    _reset()
    sink: list = []
    _install({"run_simulation": _deferring(sink), "run_python": _deferring(sink)})

    t1, _ = _submit_async("run_simulation", timeout=25)
    _wait(lambda: len(sink) == 1)
    t2, _ = _submit_async("run_python", timeout=25)
    _wait(lambda: len(sink) == 2)

    data = _health()[1]
    eq(data["busy_count"], 2)
    eq(sorted(j["name"] for j in data["jobs"]), ["run_python", "run_simulation"])

    sink[0].finish({"a": 1})
    t1.join(10)
    _wait(lambda: _health()[1]["busy_count"] == 1, "一个结束不该清掉另一个")
    data = _health()[1]
    eq(data["busy"], "run_python")

    sink[1].finish({"b": 2})
    t2.join(10)
    _wait(lambda: _health()[1]["busy_count"] == 0)
    eq(_health()[1]["busy"], None)


def test_execute_requires_the_token():
    _reset()
    _install({"run_python": _immediate})
    status, raw = _post("/execute", {"name": "run_python", "args": {}})
    eq(status, 401, "无令牌的 /execute 必须被拒绝（它等于在 ADS 里执行任意代码）")
    eq(json.loads(raw).get("error"), "unauthorized")
    not_contains(raw, TOKEN)


def test_unknown_post_path_is_gated_before_404():
    _reset()
    status, raw = _post("/nope", {})
    eq(status, 401, "未知 POST 路径也应先鉴权（否则可用 404 探测有哪些接口）")
    not_contains(raw, TOKEN)
    status, raw = _post("/nope", {}, token=authbridge.token())
    eq(status, 404, "鉴权通过后未知路径才返回 404")


def test_execute_runs_a_tool_end_to_end():
    _reset()
    _install({"run_python": _immediate})
    status, raw = _post("/execute",
                        {"name": "run_python", "args": {}, "timeout": 10},
                        token=authbridge.token())
    eq(status, 200, f"/execute 应正常返回: {raw[:200]}")
    eq(json.loads(raw), {"ok": True})
    eq(toolserver.active_jobs(), [], "请求结束后作业应已摘掉")


def test_token_never_appears_in_the_toolserver_log():
    _reset()
    _install({"run_python": _immediate})
    _post("/execute", {"name": "run_python", "args": {}})          # 制造拒绝日志
    _post("/execute", {"name": "run_python", "args": {}}, token=authbridge.token())

    log = _log_text()
    ok(bool(log), "工具服务应写文件日志")
    not_contains(log, TOKEN, "令牌被写进了工具服务日志")
    contains(log, "拒绝未授权请求", "拒绝请求应留痕")


def test_health_is_open_but_never_leaks_the_token():
    _reset()
    status, raw = _health()
    eq(status, 200, "/health 保持开放（供启动探测使用）")
    not_contains(raw, TOKEN, "/health 泄露了令牌")
    not_contains(raw, authbridge.mask(TOKEN), "/health 不应出现令牌指纹")


if __name__ == "__main__":
    try:
        code = run(globals(), "toolserver 忙碌状态")
    finally:
        _teardown()
    sys.exit(code)
