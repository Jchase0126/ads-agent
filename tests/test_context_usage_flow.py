"""上下文用量全链路：provider usage → model_context → agent 事件 → 面板归一化。

链路（docs/context-usage-spec.md §1/§3/§4）::

    假 emit（list.append）
      → Turn._accum_usage / Turn._emit_context_usage（真后端方法，不联网）
      → AgentPanelWidget._on_event（unbound + 轻量替身 self，不建 QApplication）
      → _sync_context_usage 喂给指示器的 usage 字典

全程不请求 LLM、不导入 ADS、不创建任何 Qt 控件。面板方法只做“归一化 +
把 usage 交给指示器”，浮层/tooltip 的真实渲染由 qt 层的
test_context_usage_ui.py 覆盖。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, run, not_contains, contains  # noqa: E402

add_path("backend")
import agent  # noqa: E402
import model_context as MC  # noqa: E402

add_path("addon/ads_agent")
import panel  # noqa: E402  —— 模块级 import 只定义类，不建 QApplication

PROJECT = "flow-project"
OTHER_PROJECT = "other-project"
MODEL = "deepseek-flash"
CFG = {"llm_model": MODEL, "llm_base_url": "https://api.deepseek.com",
       "max_tool_steps": 1}

# DeepSeek 原生形状：缓存命中/未命中是顶层字段
DEEPSEEK_USAGE = {
    "prompt_tokens": 8120, "completion_tokens": 1234, "total_tokens": 9354,
    "prompt_cache_hit_tokens": 6000, "prompt_cache_miss_tokens": 2120,
}
# OpenAI/GLM/StepFun 形状：缓存命中藏在 prompt_tokens_details 里
OPENAI_USAGE = {
    "prompt_tokens": 4000, "completion_tokens": 500,
    "prompt_tokens_details": {"cached_tokens": 3000},
}
# 没有缓存字段的普通服务
PLAIN_USAGE = {"prompt_tokens": 100, "completion_tokens": 20}


def _approx(got, want, msg="", tol=1e-9):
    ok(isinstance(got, float), f"{msg}：期望 float，实际 {type(got).__name__} {got!r}")
    ok(abs(got - want) <= tol, f"{msg}：实际 {got!r}，期望 {want!r}")


def _response_event(usage, cfg=None, elapsed=1.5):
    """用真 Turn 方法产生一条 phase=response 的 context_usage 事件。"""
    cfg = dict(cfg or CFG)
    turn = agent.Turn(cfg, [])
    messages = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}]
    events = []
    turn._accum_usage(usage, elapsed)
    turn._emit_context_usage(events.append, messages, cfg, "response", usage)
    ok(events and events[-1].get("type") == "context_usage", "没有产生 context_usage 事件")
    return turn, events[-1]


class _RecordingIndicator:
    """ContextUsageIndicator 的轻量替身：只记录 set_usage 收到的 usage。"""

    def __init__(self):
        self.usages = []

    def set_usage(self, usage):
        self.usages.append(usage)


def _panel_stub(model_metadata=None):
    """不建 QApplication 的面板替身：绑定真 _on_event / _sync_context_usage。"""
    fake = types.SimpleNamespace()
    fake._context_usage = {}
    fake.projects_data = {"active": PROJECT,
                          "projects": {PROJECT: {"history": []},
                                       OTHER_PROJECT: {"history": []}}}
    fake._active_model = MODEL
    fake._model_contexts = {MODEL: dict(model_metadata)} if model_metadata else {}
    fake._context_generated = {}
    fake._context_budget_chars = 0
    fake.input = types.SimpleNamespace(toPlainText=lambda: "")
    fake.context_usage = _RecordingIndicator()
    fake._sync_context_usage = lambda: panel.AgentPanelWidget._sync_context_usage(fake)
    return fake


def _feed(fake, event, project=PROJECT):
    panel.AgentPanelWidget._on_event(fake, event, project)
    stored = fake._context_usage.get((project, event.get("model") or MODEL))
    ok(stored is not None, "面板没有保存归一化后的用量")
    return stored


# ---------------------------------------------------------------------------
# 1. DeepSeek 形状：prompt_cache_hit_tokens / prompt_cache_miss_tokens 全链路
# ---------------------------------------------------------------------------
def test_deepseek_cache_shape_end_to_end():
    turn, ev = _response_event(DEEPSEEK_USAGE)
    ctx = ev["model_context"]

    # model_context.usage()：老字段口径不变，新字段来自 provider 实测
    eq(ctx["unit"], "tokens", "DeepSeek 官方模型容量应按 token 计")
    eq(ctx["basis"], "provider_usage", "有实测 usage 时 basis 必须是 provider_usage")
    eq(ctx["used"], 8120 + 1234, "used 仍是最近一次请求的输入 + 输出")
    eq((ctx["prompt_tokens"], ctx["completion_tokens"]), (8120, 1234))
    eq(ctx["total_tokens"], 9354, "total_tokens 应取 provider 上报值")
    eq(ctx["cache_hit_tokens"], 6000, "DeepSeek 缓存命中取 prompt_cache_hit_tokens")
    eq(ctx["cache_miss_tokens"], 2120, "DeepSeek 缓存未命中取 prompt_cache_miss_tokens")
    eq(ctx["reasoning_tokens"], None, "服务没报推理 tokens 时必须 None，不能伪造 0")
    _approx(ctx["cache_hit_rate"], 6000 / 8120, "cache_hit_rate = hit/(hit+miss)")

    # 事件顶层平铺（契约 3.2）
    eq(ev["phase"], "response")
    eq(ev["model"], MODEL)
    for key, want in (("cache_hit_tokens", 6000), ("cache_miss_tokens", 2120),
                      ("reasoning_tokens", None), ("total_tokens", 9354)):
        eq(ev.get(key), want, f"事件顶层 {key} 不符")
    _approx(ev["cache_hit_rate"], 6000 / 8120, "事件顶层 cache_hit_rate")

    # 回合累计（契约 3.1/3.2）
    turn_ev = ev["turn"]
    eq(turn_ev["requests"], 1)
    eq(turn_ev["prompt_tokens"], 8120)
    eq(turn_ev["completion_tokens"], 1234)
    eq(turn_ev["cache_hit_tokens"], 6000)
    eq(turn_ev["cache_miss_tokens"], 2120)
    eq(turn_ev["reasoning_tokens"], 0, "没有推理用量时回合累计为 0（累计器语义）")
    eq(turn_ev["measured_requests"], 1)
    ok(abs(turn_ev["llm_seconds"] - 1.5) < 1e-9, "turn.llm_seconds 应累计本回合 LLM 耗时")

    # 面板：_on_event 归一化 → _context_usage[(project, model)]
    fake = _panel_stub()
    stored = _feed(fake, ev)
    eq(stored["cache_hit_tokens"], 6000, "面板丢了 cache_hit_tokens")
    eq(stored["cache_miss_tokens"], 2120, "面板丢了 cache_miss_tokens")
    _approx(stored["cache_hit_rate"], 6000 / 8120, "面板丢了 cache_hit_rate")
    eq(stored["prompt_tokens"], 8120)
    eq(stored["completion_tokens"], 1234)
    eq(stored["total_tokens"], 9354)

    # 指示器收到的 usage 字典
    ok(fake.context_usage.usages, "_sync_context_usage 没有把 usage 交给指示器")
    shown = fake.context_usage.usages[-1]
    eq(shown["model"], MODEL)
    eq(shown["unit"], "tokens")
    eq(shown["cache_hit_tokens"], 6000)
    eq(shown["prompt_tokens"], 8120)
    _approx(shown["cache_hit_rate"], 6000 / 8120)
    ok(stored is not shown, "传给指示器的应是副本，不能与面板缓存同一对象")


# ---------------------------------------------------------------------------
# 2. OpenAI / GLM 形状：prompt_tokens_details.cached_tokens（miss 由 prompt 推导）
# ---------------------------------------------------------------------------
def test_openai_cached_tokens_shape_end_to_end():
    turn, ev = _response_event(OPENAI_USAGE)
    ctx = ev["model_context"]
    eq(ctx["cache_hit_tokens"], 3000, "OpenAI 形状应读 prompt_tokens_details.cached_tokens")
    eq(ctx["cache_miss_tokens"], 1000, "未上报 miss 时按 prompt_tokens - hit 推导")
    _approx(ctx["cache_hit_rate"], 0.75)
    eq(ctx["total_tokens"], 4500, "缺 total_tokens 时用 prompt+completion 补")
    eq(ev["cache_hit_tokens"], 3000)
    eq(ev["cache_miss_tokens"], 1000)

    fake = _panel_stub()
    stored = _feed(fake, ev)
    eq((stored["cache_hit_tokens"], stored["cache_miss_tokens"]), (3000, 1000))
    _approx(fake.context_usage.usages[-1]["cache_hit_rate"], 0.75)


def test_reasoning_tokens_from_completion_details_flow():
    usage = {"prompt_tokens": 200, "completion_tokens": 100,
             "completion_tokens_details": {"reasoning_tokens": 64},
             "prompt_tokens_details": {"cached_tokens": 50}}
    _turn, ev = _response_event(usage)
    eq(ev["model_context"]["reasoning_tokens"], 64, "推理 tokens 应读 completion_tokens_details")
    eq(ev["reasoning_tokens"], 64)
    eq(ev["turn"]["reasoning_tokens"], 64)


# ---------------------------------------------------------------------------
# 3. 没有缓存字段：一律 None，不能用 0 冒充实测
# ---------------------------------------------------------------------------
def test_no_cache_fields_are_none_not_zero():
    _turn, ev = _response_event(PLAIN_USAGE)
    ctx = ev["model_context"]
    eq(ctx["basis"], "provider_usage", "只有 prompt/completion 也是实测路径")
    eq(ctx["used"], 120)
    for key in ("cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens"):
        eq(ctx[key], None, f"服务没上报 {key} 时必须 None（不得用 0 冒充）")
    eq(ctx["cache_hit_rate"], None, "没有缓存分母时命中率必须 None")
    eq(ctx["total_tokens"], 120)
    for key in ("cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens"):
        eq(ev[key], None, f"事件顶层 {key} 应为 None")

    fake = _panel_stub()
    stored = _feed(fake, ev)
    for key in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_rate"):
        ok(stored[key] is None, f"面板归一化后 {key} 应为 None，实际 {stored[key]!r}")
        ok(not isinstance(stored[key], str), f"{key} 不能被渲染成字符串")
    shown = fake.context_usage.usages[-1]
    for key in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_rate"):
        eq(shown[key], None, f"指示器 usage 的 {key} 应为 None")


# ---------------------------------------------------------------------------
# 4. 老后端事件（没有 cache_*/turn 等新字段）不崩、不把缺字段当错
# ---------------------------------------------------------------------------
def _legacy_token_event():
    """旧版后端能发出的 context_usage 事件（只有 model_context + prompt/completion）。

    故意把 model_context 里的新字段去掉：老后端返回的 usage() 没有
    cache_*/total_tokens/reasoning_tokens，面板不能因此丢弃整条事件。
    """
    cfg = dict(CFG)
    ctx = MC.usage(cfg, [{"role": "user", "content": "hi"}],
                   reported=DEEPSEEK_USAGE)
    for key in ("total_tokens", "cache_hit_tokens", "cache_miss_tokens",
                "reasoning_tokens", "cache_hit_rate"):
        ctx.pop(key, None)
    return {"type": "context_usage", "used": 9354, "request_used": 40,
            "generated_chars": 0, "max": 120000, "unit": "chars",
            "estimated": False, "basis": "context_budget", "phase": "response",
            "model": MODEL, "model_context": ctx,
            "prompt_tokens": 8120, "completion_tokens": 1234}


def test_legacy_backend_event_does_not_crash_or_fake_values():
    ev = _legacy_token_event()
    ok("turn" not in ev and "cache_hit_tokens" not in ev, "老事件本就不该带新字段")
    fake = _panel_stub()
    stored = _feed(fake, ev)  # 不抛异常
    # 老事件没有顶层新字段：归一化只能得到 None，不能编造数值
    for key in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_rate",
                "reasoning_tokens", "total_tokens"):
        eq(stored.get(key), None, f"老事件缺 {key} 时归一化应为 None")
    ok(fake.context_usage.usages, "老事件也必须刷新指示器")
    shown = fake.context_usage.usages[-1]
    for key in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_rate"):
        eq(shown.get(key), None, f"老事件指示器 {key} 应为 None")
        ok(not isinstance(shown.get(key), str), f"老事件 {key} 不能被渲染成字符串")


def test_legacy_char_budget_event_without_model_context():
    ev = {"type": "context_usage", "used": 500, "max": 120000, "unit": "chars",
          "phase": "response", "model": MODEL, "estimated": True,
          "basis": "context_budget"}
    fake = _panel_stub()
    panel.AgentPanelWidget._on_event(fake, ev, PROJECT)  # 不抛异常
    eq(fake._context_budget_chars, 120000, "老字符预算事件应更新本地预算")
    ok(fake.context_usage.usages, "字符预算事件也必须刷新指示器")
    eq(fake.context_usage.usages[-1]["unit"], "chars")


# ---------------------------------------------------------------------------
# 5. 会话累计：model_context.add_usage 跨两次 response 正确
# ---------------------------------------------------------------------------
def test_add_usage_session_accumulates_across_two_responses():
    first = MC.add_usage(None, DEEPSEEK_USAGE)
    eq(first["requests"], 1)
    eq(first["measured_requests"], 1)
    eq(first["estimated_requests"], 0)
    eq(first["prompt_tokens"], 8120)
    eq(first["cache_hit_tokens"], 6000)
    _approx(first["cache_hit_rate"], 6000 / 8120)

    second = MC.add_usage(first, OPENAI_USAGE)
    eq(second["requests"], 2, "第二次 response 后 requests 应为 2")
    eq(second["measured_requests"], 2)
    eq(second["estimated_requests"], 0)
    eq(second["prompt_tokens"], 8120 + 4000)
    eq(second["completion_tokens"], 1234 + 500)
    eq(second["cache_hit_tokens"], 6000 + 3000)
    eq(second["cache_miss_tokens"], 2120 + 1000)
    eq(second["total_tokens"], 9354 + 4500)
    _approx(second["cache_hit_rate"], 9000 / (9000 + 3120))
    # 纯函数：不修改上一次结果
    eq(first["requests"], 1, "add_usage 不得修改入参 total")
    eq(first["cache_hit_tokens"], 6000, "add_usage 不得修改入参 total")
    eq(DEEPSEEK_USAGE["prompt_tokens"], 8120, "add_usage 不得修改 provider 原始 usage")

    # 无缓存字段的调用也要计数（实测但无缓存）
    third = MC.add_usage(second, PLAIN_USAGE)
    eq(third["requests"], 3)
    eq(third["measured_requests"], 3)
    eq(third["cache_hit_tokens"], 9000, "缺缓存字段的调用按 0 累加，不影响已有总数")
    _approx(third["cache_hit_rate"], 9000 / 12120)


def test_panel_session_accumulates_two_response_events():
    fake = _panel_stub()
    for usage in (DEEPSEEK_USAGE, OPENAI_USAGE):
        _turn, ev = _response_event(usage)
        panel.AgentPanelWidget._on_event(fake, ev, PROJECT)
    stored = fake._context_usage[(PROJECT, MODEL)]
    session = stored.get("session")
    ok(isinstance(session, dict), "面板应在 _context_usage[(project, model)]['session'] 累计本次会话")
    eq(session["requests"], 2, "面板会话累计应跨两条 response 事件累加")
    eq(session["measured_requests"], 2)
    eq(session["prompt_tokens"], 8120 + 4000)
    eq(session["cache_hit_tokens"], 6000 + 3000)
    _approx(session["cache_hit_rate"], 9000 / 12120)

    # 另一个项目/模型各算各的，不受影响
    fake2 = _panel_stub()
    _turn, ev = _response_event(DEEPSEEK_USAGE)
    panel.AgentPanelWidget._on_event(fake2, ev, OTHER_PROJECT)
    eq(fake2._context_usage[(OTHER_PROJECT, MODEL)]["session"]["requests"], 1,
       "切换项目后会话累计必须从 1 开始")


def test_panel_streaming_event_keeps_session_and_ignores_dirty_fields():
    """契约 §4.2：非 response 阶段不重置会话；§4.1：非法新字段只置 None，不丢事件。"""
    fake = _panel_stub()
    _turn, ev = _response_event(DEEPSEEK_USAGE)
    panel.AgentPanelWidget._on_event(fake, ev, PROJECT)
    eq(fake._context_usage[(PROJECT, MODEL)]["session"]["requests"], 1)

    # 随后的 request/streaming 事件：没有实测值，不得把会话计数再加一次
    turn = agent.Turn(dict(CFG), [])
    for phase in ("request", "streaming"):
        events = []
        turn._emit_context_usage(events.append, [{"role": "user", "content": "hi"}],
                                 dict(CFG), phase)
        panel.AgentPanelWidget._on_event(fake, events[-1], PROJECT)
    eq(fake._context_usage[(PROJECT, MODEL)]["session"]["requests"], 1,
       "request/streaming 事件不能重复累计会话")

    # 新字段被污染成字符串 / 负数 / bool：只置 None，old 字段照常生效
    _turn, dirty = _response_event(DEEPSEEK_USAGE)
    dirty["cache_hit_tokens"] = "6000"
    dirty["cache_miss_tokens"] = -5
    dirty["reasoning_tokens"] = True
    dirty["cache_hit_rate"] = 1.5
    panel.AgentPanelWidget._on_event(fake, dirty, PROJECT)
    stored = fake._context_usage[(PROJECT, MODEL)]
    eq(stored["cache_hit_tokens"], None)
    eq(stored["cache_miss_tokens"], None)
    eq(stored["reasoning_tokens"], None)
    eq(stored["cache_hit_rate"], None)
    eq(stored["prompt_tokens"], 8120, "新字段非法不能让整条事件被丢弃")
    ok(stored["used"] is not None, "上下文占用仍要更新")


# ---------------------------------------------------------------------------
# 6. request / streaming 阶段：还没有实测缓存值（契约 3.2）
# ---------------------------------------------------------------------------
def test_request_and_streaming_phases_have_no_measured_cache_fields():
    cfg = dict(CFG)
    turn = agent.Turn(cfg, [])
    messages = [{"role": "user", "content": "hi"}]
    for phase in ("request", "streaming"):
        events = []
        turn._emit_context_usage(events.append, messages, cfg, phase)
        ev = events[-1]
        eq(ev["phase"], phase)
        for key in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_rate",
                    "reasoning_tokens", "total_tokens"):
            eq(ev.get(key), None, f"{phase} 阶段还没有实测值，{key} 必须 None")
        eq(ev["turn"]["requests"], 0, f"{phase} 阶段尚未发生 LLM 调用")
        # model_context 老字段仍是估算 / 预估值，不能被当成实测
        equal = (ev["model_context"].get("basis") == "token_estimate")
        ok(equal, f"{phase} 阶段 model_context 应走估算路径，实际 {ev['model_context'].get('basis')!r}")


# ---------------------------------------------------------------------------
# 7. done 事件 stats 的回合累计（契约 3.3）
# ---------------------------------------------------------------------------
def test_stats_payload_reports_turn_cache_totals():
    turn, _ev = _response_event(DEEPSEEK_USAGE)
    stats = turn._stats_payload(MODEL)
    eq(stats["cache_hit_tokens"], 6000)
    eq(stats["cache_miss_tokens"], 2120)
    eq(stats["reasoning_tokens"], 0)
    eq(stats["requests"], 1)
    eq(stats["prompt_tokens"], 8120)
    eq(stats["completion_tokens"], 1234)

    # 服务完全不返回 usage：不伪造 0
    empty = agent.Turn(dict(CFG), [])
    empty._accum_usage(None, 0.2)
    stats = empty._stats_payload(MODEL)
    for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens",
                "cache_miss_tokens", "reasoning_tokens", "requests"):
        eq(stats[key], None, f"无 usage 时 stats['{key}'] 必须 None，不能是 0")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "上下文用量全链路"))
