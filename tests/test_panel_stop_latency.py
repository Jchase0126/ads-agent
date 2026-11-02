"""面板「停止」链路的行为测试（需要 PySide6，不需要 ADS）。

覆盖本次修复的第三块：**用户按下停止后，面板不能靠 socket 超时来"发现"停止**。

背景（真实审查证据）：面板停止只做了两件事 —— 置本地 `_stop` 标志、异步 POST
`/chat/cancel`。读取线程此时阻塞在 `resp.read1()` 里，只有后端发来下一个
keepalive（15s）或连接关闭，才能看到 `_stop`；最坏情况是等满 1800s 的 socket
超时。用户在界面上看到的就是"按了停止没反应"。

本测试用一个**真实的本地 SSE 服务器**验证：

1. 服务器发完一条事件后**长时间不再说话**（模拟后端正在跑一个长工具），
   此期间 `worker.stop()` 必须让读取线程在 1s 量级内退出，而不是等 keepalive；
2. `stop()` 必须把 `turn_id` 真正发给 `/chat/cancel`（停止要传到后端，不能只
   停本地读取）；
3. 面板收到后端新增的即时事件 `cancel_requested` 时，必须如实显示
   "已请求取消"，而不是"已停止"（请求取消 ≠ 作业已退出）；
4. `_stop_followup` 在 1s 后把状态从"正在停止"推进为"已请求取消…"，
   并且**不会**谎称已经停止。

运行：``python tests/test_panel_stop_latency.py``
"""

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
from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QObject, Signal
    from PySide6.QtWidgets import QApplication
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能运行本测试：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

_TMP = tempfile.mkdtemp(prefix="ads_agent_stop_")
_TMPDIRS = [_TMP]

# 面板构造时会读 config.ini 取后端地址；指向临时配置，避免误连真后端
os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url = http://127.0.0.1:1\nmodel = m\n\n[ads]\nport = 8761\n")

import panel  # noqa: E402

_APP = None
_SERVERS = []


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


# ---------------------------------------------------------------------------
# 一个"说一句就闭嘴"的 SSE 后端：模拟正在跑长工具的后端
# ---------------------------------------------------------------------------

class _SseServer:
    """真实 HTTP 服务器：先吐一条事件，然后安静 hold_for 秒再关闭连接。"""

    def __init__(self, hold_for=6.0, events=None):
        self.hold_for = hold_for
        self.events = list(events if events is not None else [
            {"type": "content_delta", "text": "正在算…"},
        ])
        self.cancel_bodies = []
        self.chat_started = threading.Event()
        self._release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: D401 — 静音
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(body.decode("utf-8"))
                except ValueError:
                    payload = {}
                if self.path == "/chat/cancel":
                    outer.cancel_bodies.append(payload)
                    outer._release.set()          # 收到取消就让连接尽快断
                    out = b'{"ok": true, "known": true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                if self.path != "/chat":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for ev in outer.events:
                    self.wfile.write(b"data: " + json.dumps(ev).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                outer.chat_started.set()
                # 关键：长时间不再说话。没有修复时读取线程只能等 keepalive/超时。
                outer._release.wait(outer.hold_for)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _SERVERS.append(self)

    def close(self):
        self._release.set()
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


def _teardown():
    for s in list(_SERVERS):
        s.close()
    _SERVERS.clear()
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 用例 1/2：停止必须让读取线程立刻退出，并把 turn_id 发到 /chat/cancel
# ---------------------------------------------------------------------------

def test_stop_releases_the_reading_thread_without_waiting_for_keepalive():
    srv = _SseServer(hold_for=6.0)
    panel._backend_base = lambda: f"http://127.0.0.1:{srv.port}"
    _app()
    worker = panel.ChatWorker([{"role": "user", "content": "hi"}], False, "m")
    try:
        worker.start()
        ok(srv.chat_started.wait(10.0), "读取线程应真正连上 /chat")

        t0 = time.time()
        worker.stop()
        ok(worker.wait(4000), "停止后读取线程必须在 4s 内退出（不能等 keepalive/超时）")
        elapsed = time.time() - t0
        ok(elapsed < 2.5, f"停止到线程退出应远小于 keepalive（实测 {elapsed:.2f}s）")
    finally:
        worker.stop()
        worker.wait(3000)
        srv.close()


def test_stop_posts_turn_id_to_chat_cancel():
    srv = _SseServer(hold_for=6.0)
    panel._backend_base = lambda: f"http://127.0.0.1:{srv.port}"
    _app()
    worker = panel.ChatWorker([{"role": "user", "content": "hi"}], False, "m")
    try:
        worker.start()
        ok(srv.chat_started.wait(10.0), "读取线程应真正连上 /chat")
        worker.stop()
        ok(worker.wait(4000), "停止后读取线程应退出")
        deadline = time.time() + 5.0
        while time.time() < deadline and not srv.cancel_bodies:
            time.sleep(0.02)
        eq(len(srv.cancel_bodies), 1, "停止必须向后端发一次 /chat/cancel")
        eq(srv.cancel_bodies[0].get("turn_id"), worker.turn_id,
           "/chat/cancel 必须带本轮 turn_id（否则后端不知道停哪一轮）")
    finally:
        worker.stop()
        worker.wait(3000)
        srv.close()


# ---------------------------------------------------------------------------
# 用例 3/4：面板必须区分"已请求取消"与"已退出"
# ---------------------------------------------------------------------------

def _new_panel():
    _app()
    d = tempfile.mkdtemp(prefix="ads_agent_stop_panel_")
    _TMPDIRS.append(d)
    panel.AgentPanelWidget._PROJECTS_FILE = os.path.join(d, "projects.json")
    panel.AgentPanelWidget.reload_config = lambda self: None
    panel.AgentPanelWidget._auto_revive = lambda self, retry=None: None
    w = panel.AgentPanelWidget()
    w.projects_data = {
        "active": "A",
        "projects": {"A": {"entries": [], "history": []}},
    }
    w._apply_project("A", create=True)
    return w


def _notes(w, name="A"):
    return [e["text"] for e in w.projects_data["projects"][name]["entries"]
            if e.get("kind") == "note" or str(e.get("kind", "")).startswith("note")]


def _all_texts(w, name="A"):
    return [e.get("text", "") for e in w.projects_data["projects"][name]["entries"]]


def test_cancel_requested_event_reports_request_not_completion():
    w = _new_panel()
    w._on_event({
        "type": "cancel_requested",
        "turn_id": "t1",
        "jobs": {
            "ads": {"cancelled": ["j-queued"], "running": ["j-running"]},
            "compute": {"c1": {"ok": True, "state": "cancelled"}},
        },
    }, "A")
    texts = " ".join(_all_texts(w))
    contains(texts, "已请求取消", "收到取消回执应显示『已请求取消』")
    contains(texts, "1 个排队作业已取消", "排队取消应如实说明『未执行』")
    contains(texts, "仍在执行", "执行中的 ADS 作业必须说明仍在执行，不能谎称已停止")
    not_contains(texts, "已停止后续步骤", "取消受理阶段不得宣称已经停止")


def test_cancelled_event_still_says_operations_finish_at_safe_boundary():
    w = _new_panel()
    # 用后端 agent.py 实际发出的那句话（不是测试自造文案）
    w._on_event({"type": "cancelled", "turn_id": "t1", "message":
                 "已停止：不再发起新的模型请求和工具执行。正在执行的操作会在安全边界收尾。"}, "A")
    texts = " ".join(_all_texts(w))
    contains(texts, "已停止", "终态事件应显示已停止")
    contains(texts, "安全", "终态文案要说明正在执行的操作会在安全边界收尾")


def test_stop_followup_escalates_status_without_claiming_done():
    w = _new_panel()

    class _Running:
        def isRunning(self):  # noqa: N802
            return True

        def stop(self):
            pass

    w._worker = _Running()
    w._stopping = True
    w.status.setText("正在停止后续步骤…")
    w._stop_followup()
    contains(w.status.text(), "已请求取消",
             "1s 后仍未见终态时应推进为『已请求取消』")
    not_contains(w.status.text(), "已停止本轮", "未收到终态前不得显示已停止")
    w._stopping = False


def test_backend_base_probe_and_socket_release_are_idempotent():
    srv = _SseServer(hold_for=1.0)
    panel._backend_base = lambda: f"http://127.0.0.1:{srv.port}"
    _app()
    worker = panel.ChatWorker([{"role": "user", "content": "hi"}], False, "m")
    try:
        # 没有响应对象时重复调用不能炸
        worker._release_socket()
        worker._release_socket()
        worker.start()
        ok(srv.chat_started.wait(10.0), "读取线程应真正连上 /chat")
        worker._release_socket()
        ok(worker.wait(4000), "主动关 socket 后读取线程应退出")
        # 再关一次（幂等）
        worker._release_socket()
    finally:
        worker.stop()
        worker.wait(3000)
        srv.close()


if __name__ == "__main__":
    try:
        sys.exit(run(globals(), "面板停止链路（读线程退出 / turn_id 传播 / 取消两阶段文案）"))
    finally:
        _teardown()
