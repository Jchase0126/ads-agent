"""Budget finalization and repeated-failure regression; no ADS/network calls."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, run
add_path("backend")
import agent


def test_budget_exhaustion_summarizes_without_tools_and_keeps_results():
    turn = agent.Turn({"max_tool_steps": 1}, [])
    requests, events = [], []
    def stream(cfg, messages, tools=None, **kw):
        requests.append(tools)
        if tools is not None:
            return {"tool_calls": [{"id": "one", "function": {
                "name": "get_workspace_info", "arguments": "{}"}}]}, None
        ok(any(m.get("role") == "tool" for m in messages))
        return {"content": "已读取工作区，仿真尚未执行。"}, None
    with patch.object(agent.llm, "chat_stream", stream), \
         patch.object(agent.tools_mod, "call", return_value={"ok": True}), \
         patch.object(agent.tools_mod, "is_local", return_value=False):
        turn.run(events.append)
    eq(len(requests), 2)
    eq(requests[-1], None)
    eq(events[-1]["type"], "done")
    ok("仿真尚未执行" in events[-1]["message"])
    ok(not any(e["type"] == "error" for e in events))


def test_cancel_at_final_tool_does_not_start_summary_request():
    turn = agent.Turn({"max_tool_steps": 1}, [])
    def stream(*a, **kw):
        return {"tool_calls": [{"id": "one", "function": {
            "name": "get_workspace_info", "arguments": "{}"}}]}, None
    def call(*a):
        turn.cancel()
        return {"ok": True}
    events = []
    with patch.object(agent.llm, "chat_stream", side_effect=stream) as llm, \
         patch.object(agent.tools_mod, "call", call), \
         patch.object(agent.tools_mod, "is_local", return_value=False):
        turn.run(events.append)
        eq(llm.call_count, 1)
    eq(events[-1]["type"], "cancelled")


def test_cancel_during_summary_discards_response_even_on_error():
    for fail in (False, True):
        turn = agent.Turn({"max_tool_steps": 1}, [])
        def stream(*a, **kw):
            turn.cancel()
            if fail:
                raise RuntimeError("interrupted")
            return {"content": "discard this"}, None
        events = []
        with patch.object(agent.llm, "chat_stream", stream):
            turn._finalize({}, [], events.append)
        eq(events[-1]["type"], "cancelled")
        ok(not any(e["type"] in ("assistant", "done", "error") for e in events))


def test_failed_or_incomplete_summary_returns_honest_fallback():
    for response in (RuntimeError("offline"), {"content": "partial", "complete": False}):
        turn = agent.Turn({"max_tool_steps": 1}, [])
        events = []
        with patch.object(agent.llm, "chat_stream", side_effect=(
                response if isinstance(response, Exception) else lambda *a, **kw: (response, None))):
            turn._finalize({}, [], events.append)
        eq(events[-1]["type"], "done")
        ok("不能据此认定任务成功" in events[-1]["message"])
        ok("partial" not in events[-1]["message"])


def test_identical_failed_calls_stop_after_three_and_answer_entire_batch():
    turn = agent.Turn({"max_tool_steps": 10}, [])
    events, requests = [], []
    def stream(cfg, messages, tools=None, **kw):
        requests.append(tools)
        if tools is None:
            ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
            eq(len(ids), 5)  # fourth call blocked, fifth in same batch also answered
            return {"content": "重复失败，已停止。"}, None
        count = len(requests)
        calls = [{"id": str(count), "function": {
            "name": "get_workspace_info", "arguments": "{}"}}]
        if count == 4:
            calls.append({"id": "extra", "function": {
                "name": "list_designs", "arguments": "{}"}})
        return {"tool_calls": calls}, None
    with patch.object(agent.llm, "chat_stream", stream), \
         patch.object(agent.tools_mod, "call", return_value={"error": "same failure"}) as tool, \
         patch.object(agent.tools_mod, "is_local", return_value=False):
        turn.run(events.append)
        eq(tool.call_count, 3)
    eq(events[-1]["type"], "done")
    ok("失败 3 次" in turn._halt_reason)


def test_changed_arguments_and_success_allow_further_calls():
    turn = agent.Turn({}, [])
    events, messages = [], []
    with patch.object(agent.tools_mod, "is_local", return_value=False), \
         patch.object(agent.tools_mod, "call", side_effect=[
             {"error": "failure"}, {"error": "failure"}, {"ok": True},
             {"error": "failure"}, {"error": "failure"}, {"error": "failure"},
             {"ok": True}]) as tool:
        for i, arg in enumerate(("{}", "{}", "{}", "{}", "{}", "{}", '{"cell":"new"}')):
            turn._run_tool_call(messages, {"id": str(i), "function": {
                "name": "get_workspace_info", "arguments": arg}}, events.append)
        eq(tool.call_count, 7)
    eq(turn._halt_reason, "")


if __name__ == "__main__":
    sys.exit(run(globals(), "轮次预算与重复失败"))
