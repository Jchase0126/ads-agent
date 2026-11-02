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


# ---------------------------------------------------------------------------
# 预警分级（2026-10-12）：阈值随 max_tool_steps 比例缩放，两级各一次
# ---------------------------------------------------------------------------

def test_budget_warn_levels_scale_with_max_steps():
    eq(agent.budget_warn_levels(30), (6, 3), "默认 30 步：软 6 / 强 3")
    eq(agent.budget_warn_levels(60), (12, 6), "60 步：阈值翻倍，不再固定剩 4")
    eq(agent.budget_warn_levels(100), (20, 10), "100 步")
    eq(agent.budget_warn_levels(20), (4, 2), "20 步")
    eq(agent.budget_warn_levels(8), (3, 1), "8 步：小预算也从轻启用")
    ok(not (6 > 2 * agent.budget_warn_levels(6)[0]),
       "预算 <= 2×软阈值时两级都不启用（第一轮就喊很奇怪）")
    ok(not (5 > 2 * agent.budget_warn_levels(5)[0]), "5 步预算保持静默")


def _run_tool_only_turn(max_steps):
    """跑一轮「模型每步都发工具调用」的假对话，记录每次 LLM 请求现场。

    返回 (每次请求时的提醒消息快照列表, 面板事件)。提醒一旦注入会留在
    后续所有请求里 —— 断言看的是「首次出现」的请求序号，不是总数。
    """
    turn = agent.Turn({"max_tool_steps": max_steps}, [])
    events, snapshots = [], []

    def stream(cfg, messages, tools=None, **kw):
        snapshots.append({
            "soft": sum("开始收敛" in (m.get("content") or "")
                        for m in messages if m.get("role") == "user"),
            "hard": sum("立刻收敛" in (m.get("content") or "")
                        for m in messages if m.get("role") == "user"),
        })
        if tools is not None:
            return {"tool_calls": [{"id": "c", "function": {
                "name": "get_workspace_info", "arguments": "{}"}}]}, None
        return {"content": "总结收尾。"}, None

    with patch.object(agent.llm, "chat_stream", stream), \
         patch.object(agent.tools_mod, "call", return_value={"ok": True}), \
         patch.object(agent.tools_mod, "is_local", return_value=False):
        turn.run(events.append)
    return snapshots, events


def test_two_level_warns_fire_once_each_at_scaled_thresholds():
    # 10 步预算：软=3（step=7 步初注入，第 7 次请求可见）、强=1（step=9 步初）
    snapshots, events = _run_tool_only_turn(10)
    eq(len(snapshots), 11, "10 次工具步 + 1 次收尾总结")
    first_soft = next(i for i, s in enumerate(snapshots) if s["soft"])
    first_hard = next(i for i, s in enumerate(snapshots) if s["hard"])
    eq(first_soft, 6, "软提醒在 remaining=3 的步初注入，第 7 次请求即可见")
    eq(first_hard, 8, "强提醒在 remaining=1 的步初注入，第 9 次请求即可见")
    eq(snapshots[-1]["soft"], 1, "软提醒全程只注入一次")
    eq(snapshots[-1]["hard"], 1, "强提醒全程只注入一次")
    notices = [e for e in events if e["type"] == "notice" and "预算" in e.get("text", "")]
    eq(len(notices), 2, "面板收到软/强两条提醒")
    ok("还剩" in notices[0]["text"] and "仅剩" in notices[1]["text"],
       f"两级文案有别: {notices}")
    ok(events[-1]["type"] == "done", "步数用尽后正常走总结收尾")


def test_tiny_budget_stays_silent():
    snapshots, events = _run_tool_only_turn(5)
    eq(snapshots[-1]["soft"], 0, "5 步预算：无软提醒")
    eq(snapshots[-1]["hard"], 0, "5 步预算：无强提醒")
    ok(not any("预算" in e.get("text", "") for e in events if e["type"] == "notice"),
       "小预算不喊步数将尽")


if __name__ == "__main__":
    sys.exit(run(globals(), "轮次预算与重复失败"))
