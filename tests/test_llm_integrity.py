"""LLM 流完整性与上下文管理回归（2026-10-02 优化轮，不需要 ADS）。

覆盖：
* 正常结束标志（[DONE]）识别：message.complete 如实标注；
* 流异常中断（无 [DONE]）：带工具调用的残缺响应**绝不执行**（抛
  StreamIncomplete），纯文本残缺响应打 complete=False 交给调用方；
* 临时故障（限流/5xx/连接抖动）在流开始前有限重试；
* 工具参数不是合法 JSON 时拒绝执行（过去会静默变成空参数去改设计）；
* 工具结果结构化摘要：保留状态/错误/关键数值/产物引用，数组裁剪并注明
  数量，绝不做字符串中截断；
* 上下文预算：长历史压缩后仍保留系统提示、原始需求与最近消息；
* 本机免密服务：127.0.0.1 等本地地址不强制要求 API Key。

运行::

    python tests/test_llm_integrity.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, raises, run  # noqa: E402

BACKEND = add_path("backend")

import agent  # noqa: E402
import llm as llm_mod  # noqa: E402


class _FakeResp:
    def __init__(self, lines):
        self._lines = [l.encode("utf-8") for l in lines]
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


def _sse(*chunks):
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps(c))
    return lines


def _content_chunk(text):
    return {"choices": [{"delta": {"content": text}}]}


def _tool_chunk(name, args_part, index=0, with_id=False):
    delta = {"tool_calls": [{"index": index, "function": {"name": name, "arguments": args_part}}]}
    if with_id:
        delta["tool_calls"][0]["id"] = f"call_{index}"
    return {"choices": [{"delta": delta}]}


def _patch_urlopen(fn):
    real = llm_mod.urllib.request.urlopen
    llm_mod.urllib.request.urlopen = fn
    return real


CFG = {"llm_base_url": "http://127.0.0.1:1/v1", "llm_model": "m",
       "llm_api_key": "k", "llm_thinking": "off"}


def test_stream_complete_marks_done_and_assembles_content():
    lines = _sse(_content_chunk("你"), _content_chunk("好"),
                 {"choices": [{"delta": {}}]}) + ["data: [DONE]"]
    real = _patch_urlopen(lambda req, timeout=None: _FakeResp(lines))
    got = []
    try:
        msg, usage = llm_mod.chat_stream(CFG, [], on_content=got.append)
    finally:
        llm_mod.urllib.request.urlopen = real
    eq(msg["content"], "你好")
    eq(msg["complete"], True, "收到 [DONE] 必须标记 complete=True")
    eq(got, ["你", "好"], "正文增量回调要收到每个增量")
    ok(msg.get("tool_calls") is None)


def test_stream_without_done_with_tool_calls_is_blocked():
    """没有结束标志且带工具调用：抛 StreamIncomplete，绝不给执行。"""
    lines = _sse(_tool_chunk("build_schematic", '{"library":'))  # 参数被掐断
    real = _patch_urlopen(lambda req, timeout=None: _FakeResp(lines))
    try:
        raises(llm_mod.StreamIncomplete,
               lambda: llm_mod.chat_stream(CFG, []))
    finally:
        llm_mod.urllib.request.urlopen = real


def test_complete_stream_preserves_interleaved_tool_calls():
    lines = _sse(
        _tool_chunk("list_designs", '{"library":', index=1, with_id=True),
        _tool_chunk("get_workspace_info", "{", index=0, with_id=True),
        _tool_chunk("", '"AI_lib"}', index=1),
        _tool_chunk("", "}", index=0),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"completion_tokens": 12}},
    ) + ["data: [DONE]"]
    resp = _FakeResp(lines)
    real = _patch_urlopen(lambda req, timeout=None: resp)
    try:
        msg, usage = llm_mod.chat_stream(CFG, [])
    finally:
        llm_mod.urllib.request.urlopen = real
    eq(msg["complete"], True)
    eq(msg["tool_calls"], [
        {"id": "call_0", "type": "function", "function": {
            "name": "get_workspace_info", "arguments": "{}"}},
        {"id": "call_1", "type": "function", "function": {
            "name": "list_designs", "arguments": '{"library":"AI_lib"}'}},
    ])
    eq(usage, {"completion_tokens": 12})
    ok(resp.closed)


def test_stream_transport_failure_preserves_partial_calls():
    class BrokenResp(_FakeResp):
        def __iter__(self):
            yield from self._lines
            raise ConnectionError("connection lost")

    resp = BrokenResp(_sse(_tool_chunk("run_simulation", '{"cell":', with_id=True)))
    real = _patch_urlopen(lambda req, timeout=None: resp)
    try:
        try:
            llm_mod.chat_stream(CFG, [])
            raise AssertionError("中断流必须抛异常")
        except llm_mod.StreamIncomplete as exc:
            eq(exc.partial_message["complete"], False)
            eq(exc.partial_message["tool_calls"][0]["function"]["arguments"], '{"cell":')
    finally:
        llm_mod.urllib.request.urlopen = real
    ok(resp.closed)


def test_real_stream_client_reaches_agent_dispatch_and_followup():
    requests, executed = [], []

    def fake_open(req, timeout=None):
        body = json.loads(req.data)
        requests.append(body)
        if len(requests) == 1:
            return _FakeResp(_sse(
                _tool_chunk("get_workspace_info", "{", with_id=True),
                _tool_chunk("", "}"),
            ) + ["data: [DONE]"])
        return _FakeResp(_sse(_content_chunk("已读取工作区")) + ["data: [DONE]"])

    real_open = _patch_urlopen(fake_open)
    real_call = agent.tools_mod.call

    def fake_call(cfg, name, args, **kwargs):
        executed.append((name, args))
        return {"workspace": "test_workspace"}

    agent.tools_mod.call = fake_call
    try:
        events = []
        agent.Turn(dict(CFG, max_tool_steps=3), [
            {"role": "user", "content": "查看工作区"},
        ]).run(events.append)
    finally:
        llm_mod.urllib.request.urlopen = real_open
        agent.tools_mod.call = real_call
    eq(executed, [("get_workspace_info", {})])
    eq(len(requests), 2)
    tool_msgs = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    eq(tool_msgs[0]["tool_call_id"], "call_0")
    contains(tool_msgs[0]["content"], "test_workspace")
    eq(events[-1]["type"], "done")
    eq(events[-1]["message"], "已读取工作区")


def test_stream_without_done_text_only_flags_incomplete():
    lines = _sse(_content_chunk("回复写到一半就断了"))
    real = _patch_urlopen(lambda req, timeout=None: _FakeResp(lines))
    try:
        msg, _u = llm_mod.chat_stream(CFG, [])
    finally:
        llm_mod.urllib.request.urlopen = real
    eq(msg["complete"], False, "没有结束标志要如实标注")
    contains(msg["content"], "一半")


def test_transient_error_retries_before_any_data():
    """限流在流开始前发生：有限重试后成功。"""
    import urllib.error
    attempts = {"n": 0}

    def _flaky_open(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        return _FakeResp(_sse(_content_chunk("ok")) + ["data: [DONE]"])

    real = _patch_urlopen(_flaky_open)
    try:
        msg, _u = llm_mod.chat_stream(CFG, [])
        eq(msg["content"], "ok", "重试后应拿到完整响应")
        eq(attempts["n"], 2, "第一次 503 + 第二次成功")
    finally:
        llm_mod.urllib.request.urlopen = real


def test_local_service_does_not_require_api_key():
    ok(llm_mod.local_service("http://127.0.0.1:11434/v1"))
    ok(llm_mod.local_service("http://localhost:8080"))
    ok(not llm_mod.local_service("https://api.deepseek.com"))
    cfg_local = dict(CFG, llm_base_url="http://127.0.0.1:11434/v1", llm_api_key="")
    lines = _sse(_content_chunk("hi")) + ["data: [DONE]"]
    real = _patch_urlopen(lambda req, timeout=None: _FakeResp(lines))
    try:
        # 本地免密：不抛"未配置 API Key"，且请求头不带 Authorization
        seen = {}
        real_open = llm_mod.urllib.request.urlopen

        def _capture(req, timeout=None):
            seen["auth"] = req.headers.get("Authorization")
            return _FakeResp(lines)

        llm_mod.urllib.request.urlopen = _capture
        try:
            msg, _u = llm_mod.chat_stream(cfg_local, [])
        finally:
            llm_mod.urllib.request.urlopen = real_open
        eq(msg["content"], "hi")
        eq(seen.get("auth"), None, "本地免密请求不应带 Authorization 头")
    finally:
        llm_mod.urllib.request.urlopen = real


def test_remote_without_key_still_raises():
    cfg_remote = dict(CFG, llm_api_key="")
    raises(llm_mod.LLMError,
           lambda: llm_mod.chat_stream(cfg_remote, []), "远程服务缺密钥必须报错")


# ---------------------------------------------------------------------------
# agent：残缺参数拦截 / 结构化摘要 / 上下文预算
# ---------------------------------------------------------------------------

def test_malformed_tool_arguments_are_not_executed():
    executed = []

    class _FakeTools:
        AdsToolError = Exception
        AdsToolTimeout = type("T", (Exception,), {})
        AdsToolBusy = type("B", (Exception,), {})

        @staticmethod
        def call(cfg, name, args):
            executed.append((name, args))
            return {"ok": True}

        @staticmethod
        def is_local(name):
            return False

    real = agent.tools_mod
    agent.tools_mod = _FakeTools()
    try:
        turn = agent.Turn({"llm_model": "m"}, [])
        messages = []
        events = []
        tc = {"id": "c1", "function": {"name": "build_schematic",
                                       "arguments": '{"library": "AI_lib',  # 半截 JSON
                                       }}
        turn._run_tool_call(messages, tc, events.append)
        eq(executed, [], "参数不合法绝不能执行工具")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        eq(len(tool_msgs), 1, "协议上 tool 消息必须补上")
        contains(tool_msgs[0]["content"], "阻止执行")
        types = [e.get("type") for e in events]
        ok("tool_result" in types)
    finally:
        agent.tools_mod = real


def test_tool_result_summary_is_structured_and_valid_json():
    big = {"ok": True, "status": "done", "dataset_path": "E:/x/A.ds",
           "variables": [f"v{i}" for i in range(200)],
           "traces": {"dB(S(2,1))": {"x": list(range(5000)), "y": [1.0] * 5000}},
           "note": "x" * 5000}
    out = agent._summarize_tool_result("read_traces", big)
    text = json.dumps(out, ensure_ascii=False)          # 必须能整体序列化
    ok(isinstance(json.loads(text), dict), "摘要是合法 JSON")
    eq(out["status"], "done")
    eq(out["dataset_path"], "E:/x/A.ds")
    ok(len(out["variables"]) <= agent._TOOL_LIST_CAP + 1, "列表裁剪并注明")
    contains(out["variables"][-1], "省略")
    ok(len(out["note"]) < 1200, "长字符串截断但有界")
    contains(out["note"], "截断")
    ok("traces" not in out or isinstance(out["traces"], str),
       "大数组一律收拢为提示，不整段塞给模型")


def test_context_budget_keeps_system_requirement_and_tail():
    turn = agent.Turn({"llm_model": "m", "context_budget_chars": 3000}, [])
    filler = [{"role": "user" if i % 2 == 0 else "assistant",
               "content": "f" * 200} for i in range(40)]
    messages = ([{"role": "system", "content": "SYS" * 50}]
                + [{"role": "user", "content": "需求：设计 2.4G 放大器，S11 <= -10 dB"}]
                + filler
                + [{"role": "assistant", "content": "最近的消息" * 20}])
    out = turn._apply_context_budget(messages)
    eq(out[0]["role"], "system", "系统提示必须保留")
    contains(out[1]["content"], "2.4G", "原始需求必须保留")
    eq(out[-1]["content"], messages[-1]["content"], "最近消息必须保留")
    contains(out[2]["content"], "压缩", "被压缩的事实必须注明")
    ok(len(out) < len(messages), "长历史确实被压缩了")
    # 短历史原样返回（同一对象）
    short = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    eq(turn._apply_context_budget(short), short)


def test_agent_blocks_tool_calls_from_incomplete_stream():
    """流中断响应里的工具调用：不执行，给 notice，正常收尾。"""
    class _FakeLlm:
        @staticmethod
        def chat_stream(cfg, messages, tools=None, timeout=240, **kw):
            return {"role": "assistant", "content": "", "complete": False,
                    "tool_calls": [{"id": "c1", "function": {
                        "name": "run_simulation", "arguments": "{}"}}]}, None

    executed = []
    real_llm, real_tools = agent.llm, agent.tools_mod
    agent.llm = _FakeLlm()

    class _FakeTools:
        AdsToolError = Exception

        @staticmethod
        def call(cfg, name, args):
            executed.append(name)
            return {}

        @staticmethod
        def is_local(name):
            return False

    agent.tools_mod = _FakeTools()
    try:
        turn = agent.Turn({"llm_model": "m"}, [])
        events = []
        turn.run(events.append)
        eq(executed, [], "残缺响应里的工具调用绝不能执行")
        types = [e.get("type") for e in events]
        ok("notice" in types, "要告诉用户为什么没执行")
        ok("done" in types, "仍要正常收尾，不悬挂")
    finally:
        agent.llm, agent.tools_mod = real_llm, real_tools


def test_content_deltas_are_forwarded_without_duplicate_final():
    """正文增量随 content_delta 下发；最终 assistant 事件给完整文本。"""
    full = "完整回复内容"

    class _FakeLlm:
        @staticmethod
        def chat_stream(cfg, messages, tools=None, timeout=240, **kw):
            parts = ["完整", "回复", "内容"]
            on_content = kw.get("on_content")
            for p in parts:
                if on_content:
                    on_content(p)
            return {"role": "assistant", "content": full, "complete": True}, None

    real_llm = agent.llm
    agent.llm = _FakeLlm()
    try:
        turn = agent.Turn({"llm_model": "m", "max_tool_steps": 5}, [])
        events = []
        turn.run(events.append)
        deltas = [e for e in events if e.get("type") == "content_delta"]
        finals = [e for e in events if e.get("type") == "assistant"]
        eq(len(deltas), 3, "正文增量要下发")
        eq("".join(d["text"] for d in deltas), full)
        eq(len(finals), 1, "最终完整文本只发一次")
        eq(finals[0]["text"], full)
    finally:
        agent.llm = real_llm


if __name__ == "__main__":
    sys.exit(run(globals(), "LLM 流完整性与上下文管理"))
