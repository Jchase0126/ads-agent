"""流式 usage 捕获 / 分块合并 / stream_options 400 降级（不需要 ADS，不联网）。

覆盖 docs/context-usage-spec.md §2 的落地：

* ``_build_body(stream=True)`` 带 ``stream_options.include_usage``，非流式不带；
* 流内 usage 分块到达（先 ``total_tokens``、后 ``prompt_tokens_details``）时
  **合并**而不是整体替换，嵌套 details 也合并，provider 原始键名保持不变；
* 网关因 ``stream_options`` 回 HTTP 400 → 去掉该字段重试一次后成功
  （绝不能让整轮对话因此失败）；
* 服务端完全不给 usage → ``chat_stream`` 返回 ``None``（调用方估算兜底，
  不得伪造 0）。

全部走本地 127.0.0.1 的假 SSE 服务，不联网、不用真 API Key。

运行：``python tests/test_usage_capture.py``
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ok, run  # noqa: E402

add_path("backend")

import llm  # noqa: E402


class _FakeSseServer:
    """本地假 SSE 服务：按脚本吐块，记录每个请求体，可拒绝指定字段（回 400）。

    拒绝语义与真实网关一致：请求体里只要带了 ``reject_fields`` 中的字段就回
    400，逼出客户端的降级重试路径。
    """

    def __init__(self, chunks, reject_fields=()):
        self.chunks = list(chunks)
        self.reject_fields = tuple(reject_fields)
        self.bodies: list = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            def _reply(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                with outer._lock:
                    outer.bodies.append(body)
                for field in outer.reject_fields:
                    if field in body:
                        self._reply(400, {"error": {
                            "message": f"Unrecognized request argument supplied: {field}",
                            "type": "invalid_request_error",
                        }})
                        return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in outer.chunks:
                    self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    def cfg(self, **extra):
        cfg = {
            "llm_base_url": f"http://127.0.0.1:{self.port}",
            "llm_model": "fake-model",
            "llm_api_key": "test-key-not-real",
        }
        cfg.update(extra)
        return cfg


# ---------------------------------------------------------------------------
# 1. 请求体：流式必须索取 usage
# ---------------------------------------------------------------------------

def test_build_body_requests_usage_only_when_streaming():
    cfg = {"llm_model": "m", "llm_base_url": "http://127.0.0.1:1"}
    streamed = llm._build_body(cfg, [], None, stream=True)
    eq(streamed.get("stream_options"), {"include_usage": True},
       "流式请求必须用 stream_options.include_usage 索取 token 统计")
    eq(streamed.get("stream"), True, "仍然是流式请求")
    plain = llm._build_body(cfg, [], None, stream=False)
    ok("stream_options" not in plain, "非流式请求不需要 stream_options")


# ---------------------------------------------------------------------------
# 2. usage 分块合并（不是整体替换）
# ---------------------------------------------------------------------------

_MERGE_CHUNKS = [
    {"choices": [], "usage": {}},
    {"choices": [{"delta": {"content": "你"}}]},
    {"choices": [], "usage": {"total_tokens": 30}},
    {"choices": [{"delta": {"content": "好"}}],
     "usage": {"prompt_tokens": 20, "completion_tokens": 10}},
    {"choices": [], "usage": {"prompt_tokens_details": {"cached_tokens": 15}}},
    {"choices": [], "usage": {"prompt_tokens_details": {"cache_hit_tokens": 15},
                              "completion_tokens_details": {"reasoning_tokens": 4}}},
    {"choices": [], "usage": {"total_tokens": None}},  # 空值不得覆盖已有值
]


def test_stream_usage_is_merged_across_chunks():
    srv = _FakeSseServer(_MERGE_CHUNKS)
    try:
        got: list = []
        msg, usage = llm.chat_stream(srv.cfg(), [{"role": "user", "content": "hi"}],
                                     on_content=got.append)
    finally:
        srv.close()
    eq(msg.get("content"), "你好", "正文增量照常拼装")
    eq(got, ["你", "好"], "增量回调不受 usage 块影响")
    eq(usage, {
        "total_tokens": 30,
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 15, "cache_hit_tokens": 15},
        "completion_tokens_details": {"reasoning_tokens": 4},
    }, "分块 usage 合并；provider 原始键名保持不变（不改名、不裁剪）")
    eq(len(srv.bodies), 1, "正常路径只发一次请求")
    eq(srv.bodies[0].get("stream_options"), {"include_usage": True},
       "实际请求体带上了 stream_options")


# ---------------------------------------------------------------------------
# 3. stream_options 被网关拒绝（400）→ 去掉重试一次
# ---------------------------------------------------------------------------

_ONE_USAGE_CHUNKS = [
    {"choices": [{"delta": {"content": "ok"}}]},
    {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                              "total_tokens": 18}},
]


def test_stream_options_400_falls_back_without_the_field():
    srv = _FakeSseServer(_ONE_USAGE_CHUNKS, reject_fields=("stream_options",))
    try:
        msg, usage = llm.chat_stream(srv.cfg(), [{"role": "user", "content": "hi"}])
    finally:
        srv.close()
    eq(len(srv.bodies), 2, "带 stream_options 首次 400 后应去掉该字段重试一次")
    eq(srv.bodies[0].get("stream_options"), {"include_usage": True},
       "首次请求带 stream_options")
    ok("stream_options" not in srv.bodies[1], "重试请求已去掉 stream_options")
    eq(msg.get("content"), "ok", "降级后整轮对话照常完成（绝不能失败）")
    eq(usage.get("prompt_tokens"), 11, "降级后仍能拿到 provider usage")


# ---------------------------------------------------------------------------
# 4. thinking + stream_options 同时存在：逐个降级，仍不得失败
# ---------------------------------------------------------------------------

def test_thinking_and_stream_options_both_degrade_without_failing_the_turn():
    srv = _FakeSseServer(_ONE_USAGE_CHUNKS, reject_fields=("stream_options",))
    try:
        msg, usage = llm.chat_stream(srv.cfg(llm_thinking="on"),
                                     [{"role": "user", "content": "hi"}])
    finally:
        srv.close()
    eq(msg.get("content"), "ok", "两个可选字段都在时也不得让整轮对话失败")
    eq(usage.get("total_tokens"), 18, "降级重试后 usage 正常")
    ok(len(srv.bodies) >= 2, "至少降级重试过一次")
    last = srv.bodies[-1]
    ok("stream_options" not in last and "thinking" not in last,
       "最终请求体已去掉网关不认的可选字段")
    eq(last.get("temperature"), 0.3, "去掉 thinking 后恢复默认 temperature")


# ---------------------------------------------------------------------------
# 5. 服务端完全不给 usage
# ---------------------------------------------------------------------------

def test_missing_usage_returns_none():
    srv = _FakeSseServer([{"choices": [{"delta": {"content": "没有统计"}}]}])
    try:
        msg, usage = llm.chat_stream(srv.cfg(), [{"role": "user", "content": "hi"}])
    finally:
        srv.close()
    eq(usage, None, "服务端不给 usage 时如实返回 None，不得伪造 0")
    eq(msg.get("content"), "没有统计")
    eq(msg.get("complete"), True, "正常收到 [DONE]")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "流式 usage 捕获（llm.py）"))
