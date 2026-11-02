"""真实 HTTP 取消链测试（不需要 PySide6，纯标准库）。

目标：证明"后端聊天取消能传导到 ADS 工具作业取消"。搭建完整真实链路：

  agent(假 LLM，恰好一次 tool_call)
    → backend/tools.call(job_id=…, POST /execute)   ← 真实 tools.call 代码
    → 假 ADS 工具服务（记录 body 含 job_id，阻塞在事件上）
  测试在工具执行中 POST /chat/cancel {turn_id}
    → server.Turn.cancel() + Turn.propagate_cancel()
    → backend/tools.cancel_ads_jobs POST /cancel    ← 真实取消转发代码
    → 假 ADS 记录 job_ids 并释放 /execute

核心断言（job_id 贯穿轮次→工具→作业的直接证据）：
  /cancel 收到的 job_ids == /execute 时的 job_id。
其余断言：/chat/cancel 响应回执含该 job_id（顶层 cancelled/running/unknown
数组为准，兼容 jobs.ads）；SSE 流有界时间内结束（不等 socket 超时）；
最终事件如实（cancelled，且不出现 done）；模型只被调用一次。

起服务方式参照 tests/test_auth.py / tests/test_model_http.py：
临时 config.ini → ADS_AGENT_CONFIG 先于 import server；临时目录+随机端口；
假 ADS /health 返回满足 backend/tools._verify_toolserver 身份校验的 identity。

纪律：不依赖固定 sleep 制造竞态（事件+有界 deadline 轮询）；后台线程异常
必须判失败；不打印 token。若某断言失败，如实红——那是待修复缺陷，
lead 的接线工作应使其变绿（当前 agent/server 已接线，预期绿）。

运行::

    python tests/test_chat_cancel_http.py
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ne, ok, run  # noqa: E402

add_path("backend")
add_path("addon", "ads_agent")

# ---------------------------------------------------------------- 环境与假 ADS
_TMP = tempfile.mkdtemp(prefix="ads_agent_cancel_http_")
TOKEN = "cancel-http-token-" + "Q7wR" * 8

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_EXEC_BACKSTOP_S = 15.0   # /execute 阻塞兜底（取消链断掉时保证流程不悬挂）

# 假 ADS 服务记录（线程安全：handler 在 ThreadingHTTPServer 的线程里跑）
_LOCK = threading.Lock()
EXECUTES: list = []          # [{"name","args","timeout","job_id"}]
CANCELS: list = []           # [{"job_ids":[...]}]
_EXEC_SEEN = threading.Event()
_EXEC_RELEASE = threading.Event()   # 由 /cancel 处理器释放；红灯路径由测试兜底


def _fake_ads_health():
    """满足 backend/tools._verify_toolserver 的身份校验（同 test_model_http）。"""
    import instance
    import paths
    return {
        "status": "ok",
        "service": instance.SERVICE_TOOLSERVER,
        "protocol": paths.PROTOCOL_VERSION,
        "identity": {
            "install_id": paths.install_id(),
            "plugin_version": paths.PLUGIN_VERSION,
            "protocol": paths.PROTOCOL_VERSION,
            "pid": os.getpid(),
        },
    }


class _FakeAdsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静
        pass

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send_json(_fake_ads_health())
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except ValueError:
            body = {}
        if self.path.startswith("/execute"):
            with _LOCK:
                EXECUTES.append({
                    "name": body.get("name"),
                    "args": body.get("args"),
                    "timeout": body.get("timeout"),
                    "job_id": body.get("job_id"),
                })
            _EXEC_SEEN.set()
            # 模拟一个真实在跑的作业：阻塞到取消链释放（或兜底到点）
            released = _EXEC_RELEASE.wait(_EXEC_BACKSTOP_S)
            self._send_json({"result": {"ok": True,
                                        "released_by": "cancel" if released
                                        else "backstop"}})
            return
        if self.path.startswith("/cancel"):
            ids = body.get("job_ids")
            if not isinstance(ids, list):
                ids = [ids] if ids else []
            with _LOCK:
                CANCELS.append({"job_ids": [str(i) for i in ids]})
            _EXEC_RELEASE.set()
            # 顶层三数组为准（协议：{cancelled, running, unknown}）
            self._send_json({"ok": True, "cancelled": [str(i) for i in ids],
                             "running": [], "unknown": []})
            return
        self._send_json({"error": "not found"}, status=404)


_fake_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAdsHandler)
_fake_httpd.daemon_threads = True
threading.Thread(target=_fake_httpd.serve_forever, daemon=True).start()
_ADS_PORT = _fake_httpd.server_address[1]

_CFG = os.path.join(_TMP, "config.ini")
with open(_CFG, "w", encoding="utf-8") as f:
    f.write(
        "[llm]\nbase_url = http://127.0.0.1:1\nmodel = fake\n\n"
        f"[ads]\nhost = 127.0.0.1\nport = {_ADS_PORT}\ntoken = {TOKEN}\n\n"
        "[agent]\nsim_timeout = 60\n"
    )
os.environ["ADS_AGENT_CONFIG"] = _CFG
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    os.environ[_k] = ("," + _existing) if _existing else "127.0.0.1,localhost"

import adslog  # noqa: E402

adslog.LOG_DIR = os.path.join(_TMP, "logs")
adslog.LOG_FILE = os.path.join(adslog.LOG_DIR, "backend.log")
adslog.setup(echo_console=False)

import server  # noqa: E402
import agent  # noqa: E402

# ------------------------------------------------------------------ 假 LLM
LLM_CALLS: list = []


class _FakeLlm:
    """恰好一次回复：一个 get_workspace_info tool_call（走真实 HTTP 工具链）。"""

    @staticmethod
    def chat_stream(cfg, messages, tools=None, timeout=240, **kwargs):
        LLM_CALLS.append(1)
        if len(LLM_CALLS) == 1:
            msg = {"role": "assistant", "content": "", "complete": True,
                   "tool_calls": [{"id": "c1", "type": "function",
                                   "function": {"name": "get_workspace_info",
                                                "arguments": "{}"}}]}
        else:
            msg = {"role": "assistant",
                   "content": "取消后不应再调用模型", "complete": True}
        return msg, None


agent.llm = _FakeLlm  # run() 在调用点解析模块全局 —— 参照 test_llm_integrity

_backend_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_backend_httpd.daemon_threads = True
threading.Thread(target=_backend_httpd.serve_forever, daemon=True).start()
_BHOST, _BPORT = _backend_httpd.server_address[0], _backend_httpd.server_address[1]

# ------------------------------------------------------------- SSE 读流线程
EVENTS: list = []
_STREAM_DONE = threading.Event()


def _reader(resp):
    try:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue  # keepalive 注释
            if line.startswith("data:"):
                try:
                    EVENTS.append(json.loads(line[5:].strip()))
                except ValueError:
                    EVENTS.append({"type": "__unparsed", "raw": line})
    except Exception as e:  # noqa: BLE001 — 记录为事件，断言阶段判失败
        EVENTS.append({"type": "__reader_error", "error": repr(e)})
    finally:
        _STREAM_DONE.set()


def _wait_until(cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _cancel_post(payload):
    req = urllib.request.Request(
        f"http://{_BHOST}:{_BPORT}/chat/cancel",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 server.ads_auth.TOKEN_HEADER: TOKEN},
        method="POST")
    with _OPENER.open(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_chat_cancel_propagates_to_ads_job():
    del EVENTS[:]
    LLM_CALLS.clear()
    with _LOCK:
        EXECUTES.clear()
        CANCELS.clear()
    _EXEC_SEEN.clear(); _EXEC_RELEASE.clear()
    _STREAM_DONE.clear()
    eq(len(server._ACTIVE_TURNS), 0, "测试开始时不应有残留 turn")

    # 1. 发起聊天（SSE 流）
    conn = http.client.HTTPConnection(_BHOST, _BPORT, timeout=60)
    body = json.dumps({"messages": [{"role": "user", "content": "查一下工作区"}]})
    conn.request("POST", "/chat", body=body,
                 headers={"Content-Type": "application/json",
                          server.ads_auth.TOKEN_HEADER: TOKEN})
    resp = conn.getresponse()
    eq(resp.status, 200, "/chat 应接受 SSE 请求")
    threading.Thread(target=_reader, args=(resp,), daemon=True,
                     name="sse-reader").start()

    # 2. start 事件 → turn_id（有界）
    def _start_ev():
        return next((e for e in EVENTS if e.get("type") == "start"), None)
    ok(_wait_until(lambda: _start_ev() is not None, 15), "未收到 SSE start 事件")
    turn_id = (_start_ev() or {}).get("turn_id")
    ok(turn_id, "start 事件缺 turn_id")

    # 3. 工具真实派发到假 ADS（含 job_id）
    ok(_wait_until(_EXEC_SEEN.is_set, 15),
       "工具未派发到假 ADS 工具服务（/execute 未被调用）")

    # 4. 工具执行中发起取消
    t0 = time.monotonic()
    status, cancel_resp = _cancel_post({"turn_id": turn_id})
    eq(status, 200, "/chat/cancel 应返回 200")
    eq(cancel_resp.get("known"), True, "已注册的 turn 应 known=True")
    eq(cancel_resp.get("ok"), True)

    # 5. 等待取消传导到假 ADS（核心证据；有界，不靠固定 sleep）
    ok(_wait_until(lambda: bool(CANCELS), 8),
       "取消未传导到 ADS 工具服务（/cancel 未被调用）——待修复缺陷："
       "turn→工具作业取消链断开，lead 的接线工作应使其变绿")

    # 6. 等流结束（有界；无论红绿都先兜底释放，保证进程不悬挂）
    if not CANCELS:
        _EXEC_RELEASE.set()  # 红灯路径兜底：让 /execute 返回，流程可收尾
    stream_t = time.monotonic()
    ok(_wait_until(_STREAM_DONE.is_set, 30), "SSE 流未在有界时间内结束")
    elapsed = time.monotonic() - t0
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass

    # 7. turn 收尾后应从注册表摘除
    ok(_wait_until(lambda: turn_id not in server._ACTIVE_TURNS, 10),
       "turn 结束后未从 _ACTIVE_TURNS 摘除")

    # ----------------------------------------------------------- 断言阶段
    with _LOCK:
        executes = list(EXECUTES)
        cancels = list(CANCELS)

    eq(len(executes), 1, f"/execute 应恰好一次，实际 {len(executes)}")
    jid = executes[0].get("job_id") if executes else None
    ok(jid, "/execute body 缺 job_id（tools.call 未透传 job_id）")
    eq(executes[0].get("name"), "get_workspace_info")

    # 核心：/cancel 收到的 job_ids 与 /execute 一致 —— job_id 贯穿直接证据
    received = [i for c in cancels for i in c["job_ids"]]
    eq(received, [jid],
       "假 ADS /cancel 收到的 job_ids 应与 /execute 的 job_id 一致"
       "（job_id 贯穿 轮次→工具→作业）")

    # /chat/cancel 响应回执：顶层 cancelled/running/unknown 数组为准
    ids = []
    for key in ("cancelled", "running", "unknown"):
        v = cancel_resp.get(key)
        if isinstance(v, list):
            ids.extend(str(i) for i in v)
    if not ids:  # 兼容旧形态：jobs.ads.{cancelled,running,unknown}
        ads = (cancel_resp.get("jobs") or {}).get("ads") or {}
        for key in ("cancelled", "running", "unknown"):
            ids.extend(str(i) for i in (ads.get(key) or []))
    ok(jid in ids,
       f"/chat/cancel 回执应包含 job_id={jid}（顶层 cancelled/running/"
       f"unknown 或 jobs.ads），实际 {ids}")

    # 流尽快结束（取消链通时不等 socket 超时）
    ok(elapsed <= 30.0,
       f"取消后流应在有界时间内结束，实际 {elapsed:.1f}s")

    # 最终事件如实：cancelled，无 done
    types = [e.get("type") for e in EVENTS if isinstance(e, dict)]
    ok(any(t == "cancelled" for t in types),
       f"应推送 cancelled 事件，实际事件序列 {types}")
    ok(not any(t == "done" for t in types),
       f"取消后不应出现 done 事件，实际事件序列 {types}")
    ok(not any(t == "__reader_error" for t in types),
       f"SSE 读流线程出错：{[e for e in EVENTS if e.get('type') == '__reader_error']}")
    eq(LLM_CALLS, [1],
       f"取消后不应再调用模型（第二个工具结果会触发下一轮），实际 {len(LLM_CALLS)} 次")


def test_chat_cancel_unknown_turn_id_reports_known_false():
    # server.py:1052-1056 —— 未知/已结束 turn 如实说明（ok=False, known=False，
    # 不当错误处理）；本断言记录该现状语义
    status, data = _cancel_post({"turn_id": "no-such-turn-xyz"})
    eq(status, 200)
    eq(data.get("known"), False, "未知 turn_id 应 known=False")
    eq(data.get("ok"), False, "未知 turn_id 时 ok=False（如实说明，非错误）")
    ok(data.get("message"), "响应应带说明消息")


def _teardown():
    try:
        agent.llm = None  # 不残留假对象
    except Exception:  # noqa: BLE001
        pass
    _backend_httpd.shutdown()
    _backend_httpd.server_close()
    _fake_httpd.shutdown()
    _fake_httpd.server_close()
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run(globals())
    finally:
        _teardown()
    sys.exit(code)
