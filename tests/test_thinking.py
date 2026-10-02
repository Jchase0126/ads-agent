"""深度思考（reasoning）链路测试（不需要 ADS；面板段需要 PySide6）。

覆盖「接入模型深度思考 + 流式交互」改动：

* llm.thinking_requested：auto（智谱官方接口才加参）/ on / off 三态门控
* llm.chat_stream：SSE 解析、reasoning 增量回调、400 自动去参降级重试、
  tool_calls 增量拼装、usage 取收尾块；chat() 为其便捷封装
* agent.Turn：思考增量实时发 reasoning_delta（每步首个带 first=True），
  无思考则无事件；思考不回填 messages
* 面板：增量驱动「思考」折叠行实时生长（原文保留），tool_call 只计数；
  旧条目（只有 events）回退显示

运行::

    python tests/test_thinking.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")
ADDON = add_path("addon", "ads_agent")

import llm  # noqa: E402

# ---------------- thinking_requested 门控 ----------------


def test_thinking_gate():
    ok(llm.thinking_requested({"llm_base_url": "https://open.bigmodel.cn/api/paas/v4"}),
       "auto + 智谱官方接口 → 开")
    ok(not llm.thinking_requested({"llm_base_url": "http://127.0.0.1:8760"}),
       "auto + 其它网关 → 不加参数（DeepSeek 等默认就回思考，无需参数）")
    ok(llm.thinking_requested({"llm_base_url": "http://127.0.0.1:8760",
                               "llm_thinking": "on"}), "on → 强制开")
    ok(not llm.thinking_requested({"llm_base_url": "https://open.bigmodel.cn/api/paas/v4",
                                   "llm_thinking": "off"}), "off → 强制关")


# ---------------- chat_stream：SSE 解析 / 降级 / 拼装 ----------------

_SEEN_BODIES: list = []
_SSE = (
    'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"先读工作区"}}]}\n\n'
    'data: {"choices":[{"delta":{"reasoning_content":"再列设计","content":"好"}}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":3,"completion_tokens":4}}\n\n'
    "data: [DONE]\n\n"
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _SEEN_BODIES.append(json.loads(raw))
        if "thinking" in _SEEN_BODIES[-1]:
            # 语义：该网关不认识 thinking 字段，见了就回 400
            self.send_response(400)
            self.end_headers()
            return
        data = _SSE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


def _serve():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def test_chat_stream_reasoning_fallback_and_assembly():
    _SEEN_BODIES.clear()
    httpd = _serve()
    try:
        cfg = {"llm_api_key": "k", "llm_base_url": f"http://127.0.0.1:{httpd.server_address[1]}",
               "llm_model": "deepseek-flash", "llm_thinking": "on"}
        got: list = []
        msg, usage = llm.chat_stream(cfg, [{"role": "user", "content": "hi"}],
                                     on_reasoning=got.append)
        eq(got, ["先读工作区", "再列设计"], "思考增量逐块回调")
        eq(msg.get("content"), "好", "正文拼装完整")
        ok("reasoning_content" not in msg, "思考不拼进 message")
        eq(usage.get("completion_tokens"), 4, "usage 取收尾块")
        eq(len(_SEEN_BODIES), 2, "400 后降级重试了一次")
        eq(_SEEN_BODIES[0].get("thinking"), {"type": "enabled"}, "首次请求带 thinking")
        ok("thinking" not in _SEEN_BODIES[1], "重试请求已去掉 thinking")
        ok("temperature" not in _SEEN_BODIES[0], "思考模式不带 temperature（GLM 约束）")
        eq(_SEEN_BODIES[1].get("temperature"), 0.3, "降级后恢复 temperature")
        eq(_SEEN_BODIES[0].get("stream"), True, "走流式传输")
    finally:
        httpd.shutdown()


def test_chat_wrapper_and_tool_call_assembly():
    _SEEN_BODIES.clear()
    httpd = _serve()
    try:
        cfg = {"llm_api_key": "k", "llm_base_url": f"http://127.0.0.1:{httpd.server_address[1]}",
               "llm_model": "deepseek-flash", "llm_thinking": "off"}
        msg, usage = llm.chat(cfg, [{"role": "user", "content": "hi"}])
        eq(msg.get("content"), "好", "chat() 封装返回拼装 message")
        eq(usage.get("prompt_tokens"), 3, "usage 正常")
        ok("thinking" not in _SEEN_BODIES[0], "thinking=off 时不带参数")
    finally:
        httpd.shutdown()


# ---------------- Turn：reasoning_delta 实时下发 ----------------


def test_turn_streams_reasoning_deltas():
    import agent

    captured = []

    def fake_stream(cfg, messages, tools=None, timeout=240,
                    on_reasoning=None, on_content=None):
        captured.append(json.loads(json.dumps(messages[-1])))
        if on_reasoning:
            on_reasoning("第一步先")
            on_reasoning("确认工作区，再列库。")
        return {"role": "assistant", "content": "结论：AI_lib 已挂接。"}, {
            "prompt_tokens": 5, "completion_tokens": 6}

    orig = agent.llm.chat_stream
    agent.llm.chat_stream = fake_stream
    try:
        events = []
        agent.Turn({"llm_api_key": "k", "llm_base_url": "http://127.0.0.1:1",
                    "llm_model": "m", "max_tool_steps": 3},
                   history=[], allow_python=False).run(events.append)
    finally:
        agent.llm.chat_stream = orig

    deltas = [(e["text"], e["first"]) for e in events if e["type"] == "reasoning_delta"]
    eq(deltas, [("第一步先", True), ("确认工作区，再列库。", False)],
       "每步首个增量带 first=True")
    ok("assistant" in [e["type"] for e in events], "正文事件照常")
    ok("done" in [e["type"] for e in events], "正常收尾")
    eq(captured[0].get("role"), "system", "首轮从系统提示开始")


def test_turn_without_reasoning_emits_nothing():
    import agent

    def fake_stream(cfg, messages, tools=None, timeout=240,
                    on_reasoning=None, on_content=None):
        return {"role": "assistant", "content": "plain"}, None

    orig = agent.llm.chat_stream
    agent.llm.chat_stream = fake_stream
    try:
        events = []
        agent.Turn({"llm_api_key": "k", "llm_base_url": "http://127.0.0.1:1",
                    "llm_model": "m", "max_tool_steps": 2}, history=[],
                   allow_python=False).run(events.append)
    finally:
        agent.llm.chat_stream = orig

    ok(not [e for e in events if e["type"] == "reasoning_delta"],
       "无思考内容时不发 reasoning_delta")
    ok("done" in [e["type"] for e in events], "轮次正常结束")


# ---------------- 面板：增量驱动折叠行 ----------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TMPDIRS: list = []

try:
    from PySide6.QtWidgets import QApplication  # noqa: E402
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能跑面板段：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

_TMP = tempfile.mkdtemp(prefix="ads_agent_think_")
_TMPDIRS.append(_TMP)
os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url = http://127.0.0.1:1\nmodel = m\n\n[ads]\nport = 8761\n")

import panel  # noqa: E402

_APP = None


class _StubWorker:
    """不发真实 HTTP 的 ChatWorker 替身（面板只走 _on_event 手动驱动）。"""

    def __init__(self, *args, **kwargs):
        pass

    isRunning = lambda self: False  # noqa: E731
    start = lambda self: None  # noqa: E731
    stop = lambda self: None  # noqa: E731


panel.ChatWorker = _StubWorker
panel.AgentPanelWidget.reload_config = lambda self: None
panel.AgentPanelWidget._auto_revive = lambda self, retry=None: None
panel._backend_base = lambda: "http://127.0.0.1:1"


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _new_panel():
    _app()
    d = tempfile.mkdtemp(prefix="ads_agent_think_p_")
    _TMPDIRS.append(d)
    panel.AgentPanelWidget._PROJECTS_FILE = os.path.join(d, "projects.json")
    w = panel.AgentPanelWidget()
    w.projects_data = {"active": "A", "projects": {"A": {"entries": [], "history": []}}}
    w._apply_project("A", create=True)
    return w


def _rows(w):
    return [w.chat.itemWidget(w.chat.item(i)) for i in range(w.chat.count())]


def test_panel_streams_reasoning_into_fold():
    w = _new_panel()
    w._on_event({"type": "tool_call", "name": "get_workspace_info", "arguments": {}}, None)
    w._on_event({"type": "reasoning_delta", "text": "先看工作区，**再**", "first": True}, None)
    w._on_event({"type": "tool_call", "name": "list_designs", "arguments": {}}, None)
    w._on_event({"type": "reasoning_delta", "text": "列设计", "first": False}, None)
    w._on_event({"type": "tool_result", "name": "list_designs", "ok": True,
                 "summary": "{\"designs\": []}"}, None)
    w._flush_reasoning()

    entry = w._activity_entry
    eq(entry["tools"], 2, "工具调用只累计次数")
    eq(entry["reasoning"], ["先看工作区，**再**列设计"], "增量按序拼接、原文保留")
    eq(entry["events"], [], "工具详情不再写进面板条目")

    rows = [r for r in _rows(w) if isinstance(r, panel.ActivityRow)]
    eq(len(rows), 1, "渲染一行思考折叠行")
    contains(rows[0].toggle.text(), "1 段思路", "标题含思路段数")
    contains(rows[0].toggle.text(), "2 次工具", "标题含工具次数")
    contains(rows[0].details.toPlainText(), "先看工作区，**再**列设计", "展开可见思考全文")

    # 第二步：新开一段
    w._on_event({"type": "reasoning_delta", "text": "第二步：读 VAR", "first": True}, None)
    w._flush_reasoning()
    eq(len(entry["reasoning"]), 2, "first=True 新开一段")
    rows2 = [r for r in _rows(w) if isinstance(r, panel.ActivityRow)]
    contains(rows2[0].toggle.text(), "2 段思路", "段数实时更新")

    w._on_event({"type": "done", "message": "完成", "stats": None}, None)


def test_reasoning_deltas_are_throttled():
    """回归：流式增量只写数据，重排合并到定时器 —— 逐块重排重绘会把
    GUI 线程打满（一次思考 4000+ 块，实测整个面板冻结、输入框点不动）。"""
    w = _new_panel()
    calls = {"n": 0}
    orig = panel.ActivityRow.live_append

    def counting_live_append(self):
        calls["n"] += 1
        orig(self)

    panel.ActivityRow.live_append = counting_live_append
    try:
        for k in range(30):
            w._on_event({"type": "reasoning_delta", "text": f"第{k}句。",
                         "first": k == 0}, None)
        eq(calls["n"], 0, "增量到达时不逐块重排")
        w._flush_reasoning()
        eq(calls["n"], 1, "一次 flush 只重排一次")
        entry = w._activity_entry
        eq(entry["reasoning"], ["".join(f"第{k}句。" for k in range(30))],
           "数据层完整无丢失")

        # 轮次收尾后挂起的 flush 不应再落到新内容上
        w._on_event({"type": "done", "message": "完成", "stats": None}, None)
        w._flush_reasoning()
        eq(calls["n"], 1, "收尾后 flush 空转")
    finally:
        panel.ActivityRow.live_append = orig


def test_panel_full_reasoning_event_still_works():
    w = _new_panel()
    w._on_event({"type": "reasoning", "text": "整段思考（兼容旧事件）"}, None)
    entry = w._activity_entry
    eq(entry["reasoning"], ["整段思考（兼容旧事件）"], "整段 reasoning 事件仍被接受")


def test_activity_row_expanded_height_fits_wrapped_text():
    """回归：展开高度必须按折行后的像素高度算（旧实现按段落数×行高，
    长段落折行后最后一截被裁掉并出现内部滚动条）。"""
    w = _new_panel()
    long_para = "这是一段足够长的思考，会被折行成好几行显示。" * 12
    w._on_event({"type": "reasoning_delta", "text": long_para, "first": True}, None)
    entry = w._activity_entry
    entry["expanded"] = True
    w._rebuild()

    rows = [r for r in _rows(w) if isinstance(r, panel.ActivityRow)]
    eq(len(rows), 1)
    row = rows[0]
    ok(row.toggle.isChecked(), "expanded=True 时展开")
    avail = 460
    idx = next(i for i in range(w.chat.count())
               if w.chat.itemWidget(w.chat.item(i)) is row)
    row.reflow(avail, w.chat.item(idx))

    width = max(avail - panel.U.P("xl") * 2, panel.U.px(120))
    content_w = max(width - panel.U.P("sm") * 2 - panel.U.px(12), panel.U.px(60))
    _, text_h = panel._measure_text(row._body_text(), row.details.font(), content_w)
    ok(row.details.height() >= text_h + panel.U.P("sm"),
       f"展开高度要容纳全部折行文本（需要≈{text_h + panel.U.P('sm')}，"
       f"实际 {row.details.height()}）")
    ok(w.chat.item(idx).sizeHint().height() > row.details.height(),
       "列表项高度包含折叠行")


def test_activity_details_scrollbar_always_on_and_keeps_position():
    """回归：思考内容框的竖向滚动条常驻可拖（2026-09-29 用户要的侧边滑块
    = 上下翻思考内容，不是调框高）；拖到中间后流式刷新不打断阅读位置。"""
    from PySide6.QtCore import Qt as _Qt
    w = _new_panel()
    long_para = "这是一段足够长的思考，会被折行成好几行显示。" * 40
    w._on_event({"type": "reasoning_delta", "text": long_para, "first": True}, None)
    entry = w._activity_entry
    entry["expanded"] = True
    w._rebuild()

    rows = [r for r in _rows(w) if isinstance(r, panel.ActivityRow)]
    eq(len(rows), 1)
    row = rows[0]
    ok(row.details.verticalScrollBarPolicy()
       is _Qt.ScrollBarPolicy.ScrollBarAlwaysOn, "竖向滚动条常驻显示")
    bar = row.details.verticalScrollBar()
    ok(bar.maximum() > 0, "内容超高时滚动条有可拖范围")
    # 拖到中间 -> 流式刷新（refresh 全文重设）后位置保持、不弹回
    bar.setValue(bar.maximum() // 2)
    row.refresh()
    keep = bar.value()
    ok(abs(keep - bar.maximum() // 2) <= panel.U.px(80),
       f"刷新后阅读位置保持（拖至 {bar.maximum() // 2}，刷新后 {keep}）")


def test_panel_legacy_activity_entry():
    w = _new_panel()
    legacy = {"kind": "activity", "text": "思考 · 2 项",
              "events": ["⚙ run_python\n{...}", "✅ run_python → {...}"],
              "elapsed": 9, "complete": True, "expanded": False}
    w._activity_entry = legacy
    w.projects_data["projects"]["A"]["entries"].append(legacy)
    w._rebuild()
    rows = [r for r in _rows(w) if isinstance(r, panel.ActivityRow)]
    eq(len(rows), 1, "旧条目仍渲染")
    contains(rows[0].toggle.text(), "2 项记录", "旧条目标题回退到事件计数")
    contains(rows[0].details.toPlainText(), "⚙ run_python", "旧条目展开仍是工具明细")


if __name__ == "__main__":
    try:
        raise SystemExit(run(globals()))
    finally:
        for d in _TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
