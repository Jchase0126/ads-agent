"""toolserver pump 时间预算 / 原子入队 / 活跃上限 / 取消链 / 作业可查询 测试。

不需要 ADS、不需要 Qt 主线程（pump 由测试显式调用，行为完全确定）。覆盖：

* pump 软时间预算（``pump_time_budget_ms``）：预算用尽后一个 tick 只启动
  有限个作业，剩余留在队列；**正在执行的处理器绝不被打断**（跑完且结果
  正确）；
* 排队取消：request_cancel 后 pump 直接跳过，处理器不执行；
* 执行中取消：只打标记（cancel_requested 可查、ctx.cancelled() 可查），
  作业按处理器自己的收尾结束，终态历史/快照如实区分"请求取消"与
  "已中断"（不谎报）；
* 原子有界入队：多线程并发 submit 超容量，成功数=容量、BusyError 数=超出
  数、qsize 从未超容量；
* 活跃上限（``active_job_limit``）：deferred 作业占满后新 submit 拒绝，
  报错与"队列满"可区分；
* JobContext.set_phase：/health jobs[].phase 可见 + 同名阶段日志限频；
* 请求超时但作业仍执行/已完成：job_snapshot(client_id) 查询真实状态
  （超时后跑完的终态如实记 timed_out）；
* /health 新字段：gui_heartbeat_age_ms / heartbeat_stale / active_limit /
  cancelling_count / finished_recent；无 Qt 时 pump 兜底刷新心跳可测；
  有 Qt 事件循环时独立 QTimer 心跳路径可测（可选）。

运行::

    python tests/test_pump_budget.py
"""

import atexit
import contextlib
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
from _harness import add_path, contains, eq, not_contains, ok, run, skip  # noqa: E402

ADDON = add_path("addon", "ads_agent")

TOKEN = "pump-budget-token-" + "C3d4" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_pump_budget_")
_CFG = os.path.join(_TMP, "config.ini")


def _write_cfg(agent_extra: str = "") -> None:
    with open(_CFG, "w", encoding="utf-8") as _f:
        _f.write(
            "[llm]\nmodel = m\n\n"
            "[ads]\nhost = 127.0.0.1\nport = 8761\n"
            f"token = {TOKEN}\n\n"
            "[agent]\nsim_off_main_thread = true\n" + agent_extra
        )


_write_cfg()
os.environ["ADS_AGENT_CONFIG"] = _CFG

import authbridge  # noqa: E402

import toolserver  # noqa: E402

# 工具服务日志写临时目录 —— 绝不能污染工程的 logs/
toolserver._LOG_DIR = os.path.join(_TMP, "logs")
toolserver._LOG_FILE = os.path.join(toolserver._LOG_DIR, "ads_toolserver.log")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 后台线程安全网：任何未被 submit 捕获的后台线程异常都记到这里，
# 最后一个用例断言为空 —— 后台线程异常必须让用例失败，不许静默。
_BG_ERRORS: list = []


def _bg_excepthook(args) -> None:
    name = args.thread.name if args.thread is not None else "?"
    _BG_ERRORS.append(f"[{name}] {args.exc_type.__name__}: {args.exc_value}")


threading.excepthook = _bg_excepthook


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

def _set_agent(**kv) -> None:
    """重写 config.ini 的 [agent] 段（toolserver._setting 每次现读，立即生效）。"""
    _write_cfg("".join(f"{k} = {v}\n" for k, v in kv.items()))


def _reset() -> None:
    with toolserver._jobs_lock:
        toolserver._active_jobs.clear()
        toolserver._cancelled_ids.clear()
    while True:
        try:
            toolserver._jobs.get_nowait()
        except queue.Empty:
            break
    toolserver._finished_jobs.clear()
    toolserver._gui_heartbeat = 0.0
    toolserver._last_budget_log = 0.0


class _FakeOps:
    def __init__(self, dispatch):
        self.DISPATCH = dispatch


def _install(dispatch) -> None:
    toolserver._get_ads_ops = lambda **kwargs: _FakeOps(dispatch)


def _wait(cond, msg="", timeout=15.0, tick=None):
    """有界轮询等待（不用固定 sleep 断言；tick 用于驱动 Qt 事件循环）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tick is not None:
            tick()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待超时：{msg}")


_PUMP_INTERVAL_S = 0.01


@contextlib.contextmanager
def _pump_loop(interval=_PUMP_INTERVAL_S):
    """模拟 ADS 主线程里的 pump 定时器：独立线程反复调用 toolserver.pump()。

    真机上 pump 由 Qt 主线程的 QTimer 驱动；测试没有事件循环，所以由这个
    驱动线程代替 —— 被测语义（预算、取消、活跃上限）与调用线程无关。
    保证同一时刻只有一个 pump 在跑（与真机单线程语义一致）。
    """
    stop = threading.Event()
    lock = threading.Lock()
    errors: list = []

    def loop():
        while not stop.is_set():
            try:
                with lock:
                    toolserver.pump()
            except BaseException as e:  # noqa: BLE001 — 泵异常必须让用例失败
                errors.append(f"{type(e).__name__}: {e}")
                return
            stop.wait(interval)

    t = threading.Thread(target=loop, daemon=True, name="pump-driver")
    t.start()
    try:
        yield t
    finally:
        stop.set()
        t.join(5)
        eq(errors, [], "pump 驱动线程不得出现未捕获异常")


def _join_all(threads, timeout=15.0):
    """按总超时等待一组线程退出（避免逐个 join 累加成 N×timeout）。"""
    deadline = time.time() + timeout
    for t in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        t.join(remaining)
    return [t for t in threads if t.is_alive()]


def _deferring(sink):
    """处理器：登记 ctx 后立刻转后台（模拟 run_simulation 的主线程阶段）。"""
    def handler(args, ctx):
        sink.append(ctx)
        ctx.defer()
        return {"status": "running"}
    return handler


def _immediate(args, ctx):
    return {"ok": True}


def _submit_async(name, args=None, timeout=15.0, client_id=""):
    out: dict = {}

    def run_it():
        try:
            out["result"] = toolserver.submit(name, args or {}, timeout=timeout,
                                              client_id=client_id)
        except BaseException as e:  # noqa: BLE001 — submit 的异常本身就是被测行为
            out["error"] = e

    t = threading.Thread(target=run_it, daemon=True,
                         name=f"submit-{name}-{client_id or '-'}")
    t.start()
    return t, out


def _enqueue_direct(name, args, client_id=""):
    """绕过 submit 直接入队（测试 pump 预算用：不占提交线程，无阻塞等待）。"""
    done = threading.Event()
    box: dict = {}
    jid = toolserver._job_begin(name, client_id or f"direct-{name}")
    toolserver._jobs.put((jid, name, args, box, done,
                          time.time() + 120.0, client_id or f"direct-{name}"))
    return jid, box, done


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
    with _OPENER.open(f"http://127.0.0.1:{srv.server_address[1]}/health",
                      timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _log_text() -> str:
    try:
        with open(toolserver._LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _teardown():
    if _http is not None:
        try:
            _http.shutdown()
            _http.server_close()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(_TMP, ignore_errors=True)


atexit.register(_teardown)


# ---------------------------------------------------------------------------
# pump 软时间预算
# ---------------------------------------------------------------------------

def test_pump_time_budget_limits_tick_and_preserves_queue():
    _reset()
    _set_agent(pump_time_budget_ms="0.5", pump_max_jobs="16")
    started: list = []

    def slow(args, ctx):
        started.append("slow_one")
        # 执行中断言：本 tick 里绝不能有第二个作业被启动（预算只挡"下一个"）
        eq(toolserver._jobs.qsize(), 2, "慢处理器执行中，其余作业必须仍在排队")
        eq(len(started), 1, "慢处理器执行中不得有并发作业启动")
        time.sleep(0.12)
        return {"which": "slow", "ok": True}

    def fast(args, ctx):
        started.append("fast_one")
        return {"which": "fast", "ok": True}

    _install({"slow_one": slow, "fast_one": fast})

    j1, box1, d1 = _enqueue_direct("slow_one", {})
    j2, box2, d2 = _enqueue_direct("fast_one", {})
    j3, box3, d3 = _enqueue_direct("fast_one", {})

    t0 = time.perf_counter()
    toolserver.pump()          # 一个 tick
    tick_ms = (time.perf_counter() - t0) * 1000.0

    # 慢作业确实跑了约 120ms（没被预算打断），tick 在其后立刻截断
    ok(tick_ms >= 100, f"慢处理器必须完整执行（实测 {tick_ms:.0f}ms）")
    eq(started, ["slow_one"], "预算截断后本 tick 只能启动第一个作业")
    eq(box1.get("result"), {"which": "slow", "ok": True},
       "慢处理器必须跑完且结果正确（绝不能被预算中断）")
    ok(d1.is_set())
    eq(toolserver._jobs.qsize(), 2, "其余 2 个作业必须原样留在队列")
    ok(not d2.is_set() and not d3.is_set(), "队列中的作业不得被提前启动")
    aj = toolserver.active_jobs()
    eq([j["state"] for j in aj], ["queued", "queued"],
       "截断后正在跑的作业已收尾，剩余只有 2 条排队记录")

    # 放宽预算 → 剩余作业在后续 tick 正常排空，结果全部正确
    _set_agent(pump_time_budget_ms="5000")
    toolserver.pump()
    _wait(lambda: d2.is_set() and d3.is_set(), "放宽预算后剩余作业应执行完")
    eq(box2.get("result"), {"which": "fast", "ok": True})
    eq(box3.get("result"), {"which": "fast", "ok": True})
    eq(toolserver.active_jobs(), [], "全部收尾后不得残留活跃作业")
    states = [rec["state"] for rec in toolserver._finished_jobs]
    eq(sorted(states), ["succeeded", "succeeded", "succeeded"],
       "三个作业的终态历史都应是 succeeded")


def test_pump_budget_leaves_queued_jobs_for_next_tick():
    _reset()
    _set_agent(pump_time_budget_ms="0.001", pump_max_jobs="16")

    # 每个处理器都要一点时间：一个 tick 处理 1~2 个后预算必然用尽
    def almost_fast(args, ctx):
        time.sleep(0.02)
        return {"n": args.get("n")}

    _install({"batch": almost_fast})
    boxes = []
    for i in range(6):
        _, box, _ = _enqueue_direct("batch", {"n": i})
        boxes.append(box)

    toolserver.pump()  # 一个 tick：预算 1ms，最多消化 1~2 个
    done_now = sum(1 for b in boxes if "result" in b)
    ok(0 < done_now < 6,
       f"预算截断应部分执行（实际 {done_now}/6，0 或 6 都说明预算失效）")
    ok(toolserver._jobs.qsize() == 6 - done_now, "未执行的作业必须留在队列")

    _set_agent(pump_time_budget_ms="5000")
    for _ in range(10):
        toolserver.pump()
        if toolserver._jobs.qsize() == 0:
            break
    eq(toolserver._jobs.qsize(), 0, "放宽预算后必须能排空队列")
    eq([b["result"]["n"] for b in boxes], list(range(6)),
       "所有作业按序执行且结果正确")


# ---------------------------------------------------------------------------
# 取消链：排队跳过 / 执行中标记
# ---------------------------------------------------------------------------

def test_queued_cancel_skips_handler_execution():
    _reset()
    side: list = []

    def never(args, ctx):
        side.append(1)
        return {"ok": True}

    _install({"never_tool": never})

    t, out = _submit_async("never_tool", {}, timeout=5, client_id="qc-1")
    _wait(lambda: toolserver._jobs.qsize() >= 1, "作业应已入队（排队中）")

    info = toolserver.request_cancel(["qc-1"])
    eq(info.get("cancelled"), ["qc-1"], "排队中的取消应归入 cancelled")
    eq(info.get("running"), [])

    toolserver.pump()   # 取到即跳过
    t.join(5)
    ok(not t.is_alive(), "取消后的请求应及时返回")
    ok(isinstance(out.get("error"), RuntimeError), f"取消作业应报错返回: {out}")
    contains(str(out.get("error", "")), "取消")
    eq(side, [], "已取消的排队作业绝不能执行处理器（副作用标志为空）")

    snap = toolserver.job_snapshot("qc-1")
    eq(len(snap), 1, f"job_snapshot 应能查到该终态: {snap}")
    eq(snap[0]["state"], "cancelled")
    eq(snap[0]["queued"], True, "终态历史应如实记录'从未开跑'")
    eq(snap[0]["cancel_requested"], True)
    ok(toolserver.active_jobs() == [], "取消收尾后不得残留活跃作业")


def test_running_cancel_flags_job_and_finishes_honestly():
    _reset()
    started = threading.Event()
    release = threading.Event()

    def picky(args, ctx):
        started.set()
        release.wait(10)
        # 处理器在安全边界自查：必须能看到取消请求
        return {"ok": True, "saw_cancel": ctx.cancelled()}

    _install({"picky_tool": picky})

    with _pump_loop():
        t, out = _submit_async("picky_tool", {}, timeout=10, client_id="rc-1")
        _wait(lambda: started.is_set(), "处理器应已开跑")

        jobs = toolserver.active_jobs()
        eq(jobs[0]["cancel_requested"], False, "未请求取消前不得有标记")
        eq(jobs[0]["queued"], False, "开跑后 queued 必须为 False")
        eq(jobs[0]["phase"], "running", "开跑后 phase 应为 running")

        info = toolserver.request_cancel(["rc-1"])
        eq(info.get("running"), ["rc-1"], "执行中的取消只能'标记'，归入 running")
        eq(info.get("cancelled"), [])

        eq(toolserver.active_jobs()[0]["cancel_requested"], True,
           "cancel_requested 必须可通过 /health 的 jobs 查到")
        eq(toolserver.job_snapshot("rc-1")[0]["cancel_requested"], True,
           "快照里也必须查到取消请求")

        release.set()
        t.join(5)
    eq(out.get("result", {}).get("saw_cancel"), True,
       "ctx.cancelled() 必须在处理器里查到取消请求")
    eq(out.get("result", {}).get("ok"), True,
       "作业必须按处理器自己的收尾结束（不被强行打断）")

    snap = toolserver.job_snapshot("rc-1")
    eq(len(snap), 1)
    eq(snap[0]["state"], "succeeded",
       "处理器正常跑完就是 succeeded —— 绝不能谎报'已中断'")
    eq(snap[0]["cancel_requested"], True, "但取消请求必须如实留痕在终态历史里")


def test_ctx_cancelled_returns_precise_bool_and_thread_safe():
    _reset()
    sink: list = []
    _install({"bool_tool": _deferring(sink)})

    with _pump_loop():
        t, out = _submit_async("bool_tool", {}, timeout=10, client_id="bc-1")
        _wait(lambda: sink, "处理器应已转后台")
        ctx = sink[0]

        ok(ctx.cancelled() is False,
           "取消前必须返回精确 False（model_ops 按 is True 探测）")
        toolserver.request_cancel(["bc-1"])
        ok(ctx.cancelled() is True, "取消后必须返回精确 True")
        # 反复读 + 并发读都必须稳定（线程安全）
        ok(ctx.cancelled() is True)
        results = []

        def reader():
            results.append(ctx.cancelled())

        rs = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
        for r in rs:
            r.start()
        for r in rs:
            r.join(5)
        eq(results, [True] * 8, "并发读 cancelled() 必须全部一致")

        ctx.finish({"done": True})
        t.join(5)
    eq(out.get("result"), {"done": True})


# ---------------------------------------------------------------------------
# set_phase：快照可见 + 日志限频
# ---------------------------------------------------------------------------

def test_set_phase_updates_snapshot_and_limits_log():
    _reset()
    _install({"phase_tool": lambda args, ctx: None})  # 占位，下面换真正的处理器

    def phasy(args, ctx):
        ctx.set_phase("prepare")
        eq(toolserver.active_jobs()[0]["phase"], "prepare",
           "active_jobs 必须立刻可见新阶段")
        snap = toolserver.job_snapshot("pc-1")
        eq(snap[0]["phase"], "prepare", "job_snapshot 必须立刻可见新阶段")
        ctx.set_phase("prepare")
        ctx.set_phase("prepare")     # 同名阶段重复上报 → 日志限频
        ctx.set_phase("simulate")
        eq(toolserver.active_jobs()[0]["phase"], "simulate")
        return {"ok": True}

    toolserver._get_ads_ops = lambda **kwargs: _FakeOps({"phase_tool": phasy})

    t, out = _submit_async("phase_tool", {}, timeout=10, client_id="pc-1")
    with _pump_loop():
        t.join(10)
    eq(out.get("result"), {"ok": True})

    text = _log_text()
    eq(text.count("phase_tool 阶段: prepare"), 1,
       "同名阶段重复 set_phase 只记 1 条日志（限频）")
    eq(text.count("phase_tool 阶段: simulate"), 1, "换阶段应记 1 条日志")

    snap = toolserver.job_snapshot("pc-1")
    eq(len(snap), 1)
    eq(snap[0]["state"], "succeeded")
    eq(set(snap[0].keys()),
       {"id", "name", "state", "elapsed_s", "cancel_requested", "queued", "client_id"},
       "终态历史字段必须与契约一致")


# ---------------------------------------------------------------------------
# 原子有界入队 / 活跃上限
# ---------------------------------------------------------------------------

def test_atomic_enqueue_full_queue_never_oversells():
    _reset()
    _set_agent(tool_queue_capacity="4", active_job_limit="64",
               pump_time_budget_ms="8")
    _install({"noop": _immediate})   # 本用例不 pump，处理器不会真的执行

    CAP, N = 4, 16
    results: list = [None] * N

    def fire(i):
        try:
            results[i] = ("ok", toolserver.submit("noop", {"i": i}, timeout=5,
                                                  client_id=f"aq-{i}"))
        except BaseException as e:  # noqa: BLE001
            results[i] = ("err", e)

    max_q = {"v": 0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            q = toolserver._jobs.qsize()
            if q > max_q["v"]:
                max_q["v"] = q
            time.sleep(0.001)

    sm = threading.Thread(target=sampler, daemon=True, name="qsize-sampler")
    sm.start()
    ts = [threading.Thread(target=fire, args=(i,), daemon=True,
                           name=f"submitter-{i}") for i in range(N)]
    for x in ts:
        x.start()

    def _settled_count():
        return sum(1 for r in results if r is not None)

    # 有界等待：超容量的 12 个提交应被立刻拒绝；此刻 4 个成功入队的提交
    # 仍阻塞在 done.wait() 上（作业还没被 pump 执行）。绝不能等它们自己
    # 超时（那会把"仍在等待"变成 TimeoutError，测试就白测了）。
    _wait(lambda: _settled_count() >= N - CAP,
          "超容量的提交应被拒绝，而不是排队", timeout=5.0)

    busy = [r for r in results if r is not None and isinstance(r[1], toolserver.BusyError)]
    eq(len(busy), N - CAP, f"BusyError 数必须恰为超出数（{N - CAP}）")
    for r in busy:
        contains(str(r[1]), "队列已满", "队列满的报错必须说'队列已满'")
        not_contains(str(r[1]), "活跃", "本用例只应触发队列满，不是活跃上限")
    ok(max_q["v"] <= CAP, f"qsize 全程绝不能超过容量（峰值 {max_q['v']} > {CAP}）")
    eq(sum(1 for r in results if r is None), CAP,
       "恰有容量数的提交还在等待作业执行")
    eq(toolserver._jobs.qsize(), CAP, "队列里应恰有容量数个待执行作业")

    # 收尾：清队列，给等待者回填明确的清理错误（不是崩溃）
    drained = 0
    while True:
        try:
            _jid, _name, _args, box, done, _dl, _cid = toolserver._jobs.get_nowait()
        except queue.Empty:
            break
        box["error"] = "测试收尾：队列已清空"
        done.set()
        # 本用例从不 pump，作业记录不会被 pump 收尾；这里显式摘除，
        # 否则"活跃作业"会残留 4 条从未开跑的记录（真机上它们会被下一拍
        # pump 取走并收尾）。
        toolserver._job_end(_jid)
        drained += 1
    eq(drained, CAP)
    for x in ts:
        x.join(5)
    ok(all(not x.is_alive() for x in ts), "所有提交线程都应已退出")

    cleanup = [r for r in results
               if r is not None and isinstance(r[1], RuntimeError)
               and not isinstance(r[1], toolserver.BusyError)]
    eq(len(cleanup), CAP, "4 个成功入队的提交应以清理错误退出")
    for r in cleanup:
        contains(str(r[1]), "测试收尾")
    # 最终分类必须恰为：12 个队列满拒绝 + 4 个清理退出，不允许任何其它异常类型
    kinds = sorted("busy" if isinstance(r[1], toolserver.BusyError)
                   else "cleanup" if "测试收尾" in str(r[1])
                   else "unexpected"
                   for r in results if r is not None)
    eq(kinds, ["busy"] * (N - CAP) + ["cleanup"] * CAP,
       f"提交结果分类必须精确: {kinds}")
    eq(toolserver._jobs.qsize(), 0)
    eq(len(toolserver.active_jobs()), 0)
    stop.set()
    sm.join(2)


def test_active_job_limit_blocks_new_submit_distinct_from_queue_full():
    _reset()
    _set_agent(active_job_limit="2", tool_queue_capacity="64",
               pump_time_budget_ms="5000")
    sink: list = []
    _install({"sim": _deferring(sink)})

    t1, _o1 = _submit_async("sim", {}, timeout=10, client_id="al-1")
    t2, _o2 = _submit_async("sim", {}, timeout=10, client_id="al-2")
    with _pump_loop():
        _wait(lambda: len(sink) == 2, "两个作业都应被 pump 转后台（活跃）")

        jobs = toolserver.active_jobs()
        eq(len(jobs), 2)
        ok(all(j["queued"] is False for j in jobs), "deferred 作业必须计入活跃")

        e = None
        try:
            toolserver.submit("sim", {}, timeout=5, client_id="al-3")
        except toolserver.BusyError as exc:
            e = exc
        ok(e is not None, "活跃上限满时 submit 必须抛 BusyError")
        contains(str(e), "上限", "报错必须说明是活跃上限")
        contains(str(e), "2", "报错应带当前上限数值")
        not_contains(str(e), "队列已满", "活跃上限不得谎报成队列满")

        # 释放两个后台作业 → 活跃清零 → 新提交必须成功（证明之前挡的是活跃上限）
        sink[0].finish({"n": 1})
        sink[1].finish({"n": 2})
        t1.join(5)
        t2.join(5)
        eq(len(toolserver.active_jobs()), 0)

        sink2: list = []
        toolserver._get_ads_ops = lambda **kwargs: _FakeOps({"sim": _deferring(sink2)})
        t4, o4 = _submit_async("sim", {}, timeout=10, client_id="al-4")
        _wait(lambda: len(sink2) == 1, "限额腾出后新提交应成功入队并执行")
        sink2[0].finish({"final": True})
        t4.join(5)
    eq(o4.get("result"), {"final": True})


# ---------------------------------------------------------------------------
# 请求超时后作业仍可查询真实状态
# ---------------------------------------------------------------------------

def test_timeout_request_snapshot_reports_real_state():
    _reset()
    _set_agent(pump_time_budget_ms="5000")
    sink: list = []
    _install({"slowpoke": _deferring(sink)})

    with _pump_loop():
        t, out = _submit_async("slowpoke", {}, timeout=0.5, client_id="snap-1")
        _wait(lambda: sink, "处理器应已转后台（请求虽短，作业在跑）")

        t.join(8)   # done.wait(0.5+2) 后请求方拿到 TimeoutError
        ok(isinstance(out.get("error"), TimeoutError),
           f"短超时应拿到 TimeoutError: {out}")

        # 请求已经失败，但作业还在后台真实执行 —— 快照必须如实说 running
        snap = toolserver.job_snapshot("snap-1")
        eq(len(snap), 1, f"超时后作业仍应可查: {snap}")
        eq(snap[0]["state"], "running", "还在后台跑的作业必须报 running")
        eq(snap[0]["timed_out"], True, "并带 timed_out 标记（请求方已超时）")
        eq(len(toolserver.active_jobs()), 1)
        status, h = _health()
        eq(status, 200)
        eq(h["busy"], "slowpoke", "/health 必须继续报告该作业在运行")
        eq(h["timed_out_count"], 1)

        # 后台跑完后：终态如实记 timed_out（请求方拿到的就是超时，不谎报成功）
        sink[0].finish({"late": True})
        _wait(lambda: len(toolserver.active_jobs()) == 0, "作业应收尾")
    snap2 = toolserver.job_snapshot("snap-1")
    eq(len(snap2), 1)
    eq(snap2[0]["state"], "timed_out",
       "超时后跑完的作业终态必须如实记 timed_out，不谎报 succeeded")
    eq(snap2[0]["cancel_requested"], False)
    eq(toolserver.active_jobs(), [])


# ---------------------------------------------------------------------------
# /health 新字段 + GUI 心跳
# ---------------------------------------------------------------------------

def test_health_new_fields_and_heartbeat_semantics():
    _reset()
    _set_agent()   # 默认配置：active_job_limit=4、gui_heartbeat_ms=200

    status, h = _health()
    eq(status, 200)
    for key in ("gui_heartbeat_age_ms", "heartbeat_stale", "active_limit",
                "cancelling_count", "finished_recent"):
        ok(key in h, f"/health 缺少新字段 {key}")
    eq(h["gui_heartbeat_age_ms"], None,
       "从未刷新时 age 必须是 None（0 会被误读成'刚刚刷新'）")
    eq(h["heartbeat_stale"], False, "无心跳数据时不得报陈旧")
    eq(h["active_limit"], 4, "默认活跃上限应为 4")
    eq(h["finished_recent"], len(toolserver._finished_jobs))
    eq(h["cancelling_count"], 0)

    # pump 兜底刷新（无 Qt 事件循环也有心跳）
    toolserver.pump()
    _s2, h2 = _health()
    ok(h2["gui_heartbeat_age_ms"] is not None, "pump 执行后必须有心跳时间戳")
    ok(0 <= h2["gui_heartbeat_age_ms"] < 2000,
       f"pump 刚刷新过，age 应远小于陈旧阈值（实际 {h2['gui_heartbeat_age_ms']}ms）")
    eq(h2["heartbeat_stale"], False)

    # 心跳陈旧 → heartbeat_stale（只报"主线程疑似阻塞"，字段只给事实）
    toolserver._gui_heartbeat = time.monotonic() - 3600.0
    _s3, h3 = _health()
    eq(h3["heartbeat_stale"], True, "一小时没刷新必须报陈旧")
    ok(h3["gui_heartbeat_age_ms"] > 2000)
    toolserver._gui_heartbeat = 0.0

    # cancelling_count：执行中且 cancel_requested 才算"正在取消"；
    # 排队待跳过（queued=True）不计入
    j1 = toolserver._job_begin("cancel_probe", "hc-run")
    with toolserver._jobs_lock:
        toolserver._active_jobs[j1]["queued"] = False
        toolserver._active_jobs[j1]["cancel_requested"] = True
    j2 = toolserver._job_begin("cancel_probe", "hc-queued")
    with toolserver._jobs_lock:
        toolserver._active_jobs[j2]["queued"] = True
        toolserver._active_jobs[j2]["cancel_requested"] = True

    _s4, h4 = _health()
    eq(h4["cancel_requested_count"], 2, "带取消标记的活跃作业共 2 个")
    eq(h4["cancelling_count"], 1, "只有执行中的取消才算'正在取消'")
    run_job = [x for x in h4["jobs"] if x["id"] == j1][0]
    ok("phase" in run_job and "queued" in run_job,
       "jobs 条目必须带 phase 与 queued 字段")
    eq(run_job["phase"], "running")
    eq(run_job["queued"], False)
    toolserver._job_end(j1)
    toolserver._job_end(j2)


def test_heartbeat_timer_without_qt_returns_none():
    _reset()
    timer = toolserver.ensure_heartbeat_timer()
    eq(timer, None, "无 Qt 事件循环时必须返回 None 且不抛异常")
    eq(toolserver._heartbeat_timer, None, "不得残留定时器句柄")
    eq(toolserver._gui_heartbeat, 0.0, "无 Qt 时不得凭空产生心跳")


def test_heartbeat_timer_with_qt_event_loop():
    try:
        import qtcompat
        QtCore = qtcompat.QtCore()
    except Exception as e:  # noqa: BLE001
        skip(f"环境无可用 Qt 绑定：{type(e).__name__}: {e}")

    app = QtCore.QCoreApplication.instance()
    created = False
    if app is None:
        try:
            app = QtCore.QCoreApplication([])
        except Exception as e:  # noqa: BLE001
            skip(f"无法创建 QCoreApplication：{type(e).__name__}: {e}")
        created = True

    _set_agent(gui_heartbeat_ms="40")
    timer = None
    try:
        timer = toolserver.ensure_heartbeat_timer()
        ok(timer is not None, "存在 QCoreApplication 时应创建独立心跳定时器")
        ok(timer.isActive(), "心跳定时器应已启动")
        eq(timer.interval(), 40, "间隔必须来自 gui_heartbeat_ms 设置")
        again = toolserver.ensure_heartbeat_timer()
        ok(again is timer, "重复调用必须复用同一实例（防重入）")

        # 事件循环运转后心跳时间戳应被定时器刷新（有界轮询 + 驱动事件处理，
        # 不靠固定 sleep 猜测定时器什么时候触发）
        toolserver._gui_heartbeat = 0.0
        _wait(lambda: toolserver._gui_heartbeat > 0,
              "事件循环运转后 QTimer 必须刷新心跳时间戳", timeout=5.0,
              tick=QtCore.QCoreApplication.processEvents)
    finally:
        if timer is not None:
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:  # noqa: BLE001
                pass
            toolserver._heartbeat_timer = None
        if created:
            app.quit()


# ---------------------------------------------------------------------------
# 收尾：后台线程安全网
# ---------------------------------------------------------------------------

def test_no_unexpected_background_thread_exceptions():
    eq(_BG_ERRORS, [],
       f"后台线程不得有未捕获异常: {_BG_ERRORS}")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "toolserver pump 预算/原子入队/取消链/心跳"))
