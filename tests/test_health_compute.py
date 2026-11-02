"""后端 /health 的计算作业视图（真实 HTTP，不需要 ADS）。

为什么单独测：

* 用户要求 /health 能同时区分"HTTP 服务可达 / GUI 心跳新鲜度 / 排队数量 /
  执行中作业 / 取消中或等待回收的作业"。ADS 端那份在 toolserver 的 /health
  （见 test_pump_budget.py），**后端进程自己的**计算作业（run_compute）此前
  在任何地方都看不见 —— 它不经过 ADS GUI，也不进 ADS 端的作业表。
* /health 是探测入口：**统计失败绝不能 500**，否则插件会把"无法统计"误判成
  "后端挂了"。所以这里既测正常视图，也测没有管理器时的降级。

纪律：临时配置/数据目录、随机端口、有界轮询、后台线程异常必须判失败。

运行::

    python tests/test_health_compute.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ok, run  # noqa: E402

BACKEND = add_path("backend")

TOKEN = "health-test-token-" + "Q7wE" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_health_")
_CFG = os.path.join(_TMP, "config.ini")
_JOB_DIR = os.path.join(_TMP, "compute_jobs")

with open(_CFG, "w", encoding="utf-8") as f:
    f.write(
        "[llm]\n"
        "base_url = http://127.0.0.1:1\n"
        "model = test-model\n"
        "api_key = test-key-not-a-real-secret\n"
        "\n[ads]\n"
        "host = 127.0.0.1\n"
        "port = 8761\n"
        f"token = {TOKEN}\n"
        "\n[agent]\n"
        "sim_timeout = 120\n"
        f"compute_base_dir = {_JOB_DIR}\n"
        "compute_max_workers = 1\n"
        "compute_queue_capacity = 4\n"
        "compute_timeout_s = 120\n"
    )

os.environ["ADS_AGENT_CONFIG"] = _CFG
os.environ["ADS_AGENT_DATA_DIR"] = os.path.join(_TMP, "data")
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    os.environ[_k] = ("," + _existing) if _existing else "127.0.0.1,localhost"

import adslog  # noqa: E402

adslog.LOG_DIR = _TMP
adslog.LOG_FILE = os.path.join(_TMP, "backend.log")
adslog.setup(echo_console=False)

import agent  # noqa: E402
import compute_manager as cm  # noqa: E402
import server  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_httpd = None
_HOST = "127.0.0.1"
_PORT = 0

# 慢但协作可取消：每轮 20ms，便于稳定观测 running
_SLOW_CODE = """
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


def _ensure_server():
    global _httpd, _PORT
    import config as config_mod
    server.CFG.update(config_mod.load())
    if _httpd is None:
        _httpd = ThreadingHTTPServer((_HOST, 0), server.Handler)
        _httpd.daemon_threads = True
        _PORT = _httpd.server_address[1]
        threading.Thread(target=_httpd.serve_forever, daemon=True,
                         name="health-backend").start()


def _health():
    _ensure_server()
    req = urllib.request.Request(f"http://{_HOST}:{_PORT}/health")
    with _OPENER.open(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _wait_until(cond, msg="", timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{msg}")


def _set_manager(mgr):
    agent._COMPUTE_MANAGER = mgr


def _drop_manager():
    mgr = agent._COMPUTE_MANAGER
    agent._COMPUTE_MANAGER = None
    if mgr is not None:
        try:
            mgr.shutdown_all(grace_s=0.5)
        except Exception:  # noqa: BLE001
            pass
    return mgr


# ---------------------------------------------------------------------------
# 1. 没有计算管理器时：/health 仍 200，如实说"未启用"
# ---------------------------------------------------------------------------

def test_health_is_200_and_reports_compute_unavailable_without_manager():
    _drop_manager()
    status, data = _health()
    eq(status, 200, "/health 必须始终可达（探测入口）")
    eq(data.get("status"), "ok", "/health 的 status 约定")
    ok("compute" in data, "/health 必须带 compute 字段（否则看不到计算作业）")
    comp = data["compute"]
    eq(comp.get("available"), False, "没有计算作业时如实报未启用")
    ok(comp.get("reason"), f"必须给出原因：{comp}")
    # 不能因为"没有"就凭空造出 Manager（会建目录/起线程）
    eq(agent._COMPUTE_MANAGER, None, "/health 不得为了统计去创建管理器")


# ---------------------------------------------------------------------------
# 2. 有作业时：running / queued / jobs[] 如实可见
# ---------------------------------------------------------------------------

def test_health_shows_running_and_queued_compute_jobs():
    mgr = cm.ComputeManager(base_dir=_JOB_DIR, max_workers=1, queue_capacity=4)
    try:
        _set_manager(mgr)
        mgr.submit("h-run", _SLOW_CODE, timeout_s=300, label="health 用例")
        _wait_until(lambda: mgr.inspect("h-run")["state"] == "running",
                    "计算作业应先跑起来")
        mgr.submit("h-queued", "result = 1", timeout_s=300)

        def _seen():
            _, d = _health()
            c = d.get("compute") or {}
            return c.get("available") and c.get("running") == 1 \
                and c.get("queued") == 1

        _wait_until(_seen, "/health 应同时报出执行中与排队中的计算作业")
        _, data = _health()
        comp = data["compute"]
        eq(comp["available"], True, "有管理器时 available 必须为真")
        eq(comp["max_workers"], 1, "并发上限如实上报")
        eq(comp["queue_capacity"], 4, "排队容量如实上报")
        eq(comp["running"], 1, "执行中的计算作业数")
        eq(comp["queued"], 1, "排队中的计算作业数")
        ok(comp.get("python_executable"), "必须报出解释器，便于排查依赖问题")
        ids = {j.get("job_id") for j in comp.get("jobs") or []}
        ok("h-run" in ids, f"/health 的作业列表应包含执行中的作业：{comp}")
        run_item = [j for j in comp["jobs"] if j["job_id"] == "h-run"][0]
        eq(run_item["state"], "running", "作业状态如实上报")
        ok(run_item.get("worker_pid"), "执行中的作业必须带 worker_pid")
    finally:
        _drop_manager()
        shutil.rmtree(_JOB_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 取消中 / 已回收：/health 的字段随真实状态变化
# ---------------------------------------------------------------------------

def test_health_reflects_cancel_requested_and_reclaims():
    mgr = cm.ComputeManager(base_dir=_JOB_DIR, max_workers=1, queue_capacity=4,
                            cancel_grace_s=0.3)
    try:
        _set_manager(mgr)
        mgr.submit("h-cancel", _SLOW_CODE, timeout_s=300)
        _wait_until(lambda: mgr.inspect("h-cancel")["state"] == "running",
                    "计算作业应先跑起来")
        pid = mgr.inspect("h-cancel")["worker_pid"]
        receipt = mgr.cancel("h-cancel")
        ok(bool(receipt.get("ok")), f"取消请求应被接受：{receipt}")
        _wait_until(
            lambda: mgr.inspect("h-cancel")["state"] in cm.TERMINAL_STATES,
            "取消后作业应收尾")
        # 终态作业不再占用活跃视图，但状态可查（"HTTP 结束"≠"执行结束"的反面：
        # 真正结束之后仍能查到真实终态）
        _wait_until(
            lambda: all(j.get("job_id") != "h-cancel"
                        for j in (_health()[1].get("compute") or {}).get("jobs") or []),
            "终态作业不应继续出现在活跃列表")
        _, data = _health()
        comp = data["compute"]
        eq(comp["running"], 0, "回收后 running 归零")
        eq(comp["queued"], 0, "回收后 queued 归零")
        eq(comp["cancel_requested"], 0, "回收后 cancel_requested 归零")
        eq(mgr.inspect("h-cancel")["state"], "cancelled",
           "终态仍可查询，且状态如实")
        del pid
    finally:
        _drop_manager()
        shutil.rmtree(_JOB_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. 统计异常时降级而不是 500
# ---------------------------------------------------------------------------

def test_health_never_500s_when_snapshot_raises():
    class _Boom:
        def snapshot(self):
            raise RuntimeError("统计坏了（测试注入）")

    try:
        _set_manager(_Boom())
        status, data = _health()
        eq(status, 200, "统计失败时 /health 也必须 200（否则会被误判为后端挂了）")
        comp = data.get("compute") or {}
        eq(comp.get("available"), False, "统计失败应如实降级")
        ok("统计坏了" in str(comp.get("reason")),
           f"原因里要保留异常信息：{comp}")
    finally:
        _drop_manager()


def teardown():
    _drop_manager()
    if _httpd is not None:
        _httpd.shutdown()
        _httpd.server_close()
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run(globals(), "后端 /health 的计算作业视图（真实 HTTP）")
    finally:
        teardown()
    sys.exit(code)
