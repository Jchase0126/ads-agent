"""上下文用量环 / 详情浮层 / 本次会话累计的 UI 回归测试。

对应契约 docs/context-usage-spec.md §4：
  (a) 完整字段 → 浮层与 tooltip 同时给出输入 / 输出 / 缓存命中率 / 本次会话；
  (b) 老后端缺字段 → 不丢事件、不出现 None/NaN、主环仍显示上下文占用；
  (c) 非法值（字符串 / 负数 / True / 越界命中率）→ 归一化为 None，绝不画错；
  (d) 两次 phase=="response" 的实测事件 → session.requests == 2 且命中率正确。

不联网、不读真实 projects.json：这里只用裸 ContextUsageIndicator 与一个
只实现被调方法的最小面板替身，不构造真正的 AgentPanelWidget。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TMP = tempfile.mkdtemp(prefix="ads_ctx_ui_")
os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url=http://127.0.0.1:1\n")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # pragma: no cover - 缺 Qt 时按环境不满足跳过
    print("SKIP: PySide6 未安装")
    raise SystemExit(2)

import panel  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ---------------------------------------------------------------- 测试道具
class _StubInput:
    def toPlainText(self):
        return ""


class _StubPanel:
    """只实现 _on_event / _sync_context_usage / _append_history 用到的那几个属性。"""

    def __init__(self):
        self._projects = {"A": {"history": []}, "B": {"history": []}}
        self.projects_data = {"active": "A", "projects": self._projects}
        self._context_usage = {}
        self._context_generated = {}
        self._model_contexts = {}
        self._context_budget_chars = 120000
        self._active_model = "m1"
        self._worker = None
        self._turn_project = None
        self.input = _StubInput()
        self.context_usage = panel.ContextUsageIndicator()

    # 面板里这几个方法都是普通 Python 方法，直接绑过来复用同一份实现。
    def _project(self, name):
        return self._projects.get(name)

    def _on_event(self, ev, project="A"):
        return panel.AgentPanelWidget._on_event(self, ev, project)

    def _sync_context_usage(self):
        return panel.AgentPanelWidget._sync_context_usage(self)

    def _append_history(self, project, message):
        return panel.AgentPanelWidget._append_history(self, project, message)


def _full_usage():
    turn = {"prompt_tokens": 60000, "completion_tokens": 2000,
            "cache_hit_tokens": 12000, "cache_miss_tokens": 4000,
            "reasoning_tokens": 300, "cache_hit_rate": 0.75}
    session = {"requests": 3, "prompt_tokens": 180000, "completion_tokens": 6000,
               "cache_hit_tokens": 36000, "cache_miss_tokens": 12000,
               "cache_hit_rate": 0.75}
    return {"unit": "tokens", "used": 62000, "max": 200000, "model": "gpt-5",
            "source": "official", "basis": "provider_usage",
            "prompt_tokens": 60000, "completion_tokens": 2000,
            "cache_hit_tokens": 12000, "cache_miss_tokens": 4000,
            "cache_hit_rate": 0.75, "reasoning_tokens": 300,
            "turn": turn, "session": session}


def _event(model="m1", prompt=1000, completion=100, hit=750, miss=250, phase="response",
           used=None):
    model_context = {"unit": "tokens", "used": used if used is not None else prompt,
                     "max": 128000, "source": "official", "basis": "provider_usage",
                     "prompt_tokens": prompt, "completion_tokens": completion,
                     "total_tokens": prompt + completion,
                     "cache_hit_tokens": hit, "cache_miss_tokens": miss}
    return {"type": "context_usage", "phase": phase, "model": model,
            "model_context": model_context}


# ------------------------------------------------------------------ (a)
def test_full_fields_popup_and_tooltip():
    host = QtWidgets.QWidget()
    host.resize(1000, 700)
    ind = panel.ContextUsageIndicator(host)
    host.show()
    ind.show()
    APP.processEvents()
    ind.set_usage(_full_usage())
    tip = ind.toolTip()
    eq(ind.accessibleDescription(), tip, "tooltip 与无障碍描述必须同步")
    for needle in ("62,000 / 200,000 tokens", "容量来源：官方文档",
                   "输入 60,000", "输出 2,000", "缓存命中率 75%",
                   "本次会话", "点击查看详情"):
        contains(tip, needle, f"完整字段的 tooltip 应含 {needle!r}")
    not_contains(tip, "None", "tooltip 不应出现 None")
    not_contains(tip, "nan", "tooltip 不应出现 nan")

    text = ind.details_text()
    for needle in ("上下文", "31.0%", "gpt-5",
                   "本回合", "输入 60,000", "输出 2,000",
                   "缓存命中 12,000 (75%)", "未命中 4,000", "推理 300",
                   "本次会话", "3 次请求", "缓存命中率 75%"):
        contains(text, needle, f"详情浮层应含 {needle!r}")
    not_contains(text, "None", "详情浮层不应出现 None")

    # 主环语义不变：弧长只由 used/max 决定，缓存命中不参与。
    eq(ind._usage["used"], 62000, "主环用量取自 used")
    eq(ind._usage["max"], 200000, "主环上限取自 max")

    img = ind.grab()          # 强制走 paintEvent（缓存弧不能影响主环绘制）
    ok(not img.isNull(), "用量环应能正常绘制")

    ind._show_popup()
    popup = ind._popup
    ok(popup is not None and popup.isVisible(), "点击/悬浮后详情浮层应可见")
    eq(len(popup._bars), 1, "有命中/未命中计数时浮层应画一条缓存命中色条")
    ind._hide_popup()
    ok(not popup.isVisible(), "收起后浮层应隐藏")


def test_estimated_values_are_prefixed_and_explained():
    usage = _full_usage()
    usage.update(estimated=True, basis="local_history")
    ind = panel.ContextUsageIndicator()
    ind.set_usage(usage)
    contains(ind.toolTip(), "≈", "估算值要带 ≈ 前缀")
    text = ind.details_text()
    contains(text, "≈", "浮层里的估算值也要带 ≈ 前缀")
    contains(text, "未含后端系统提示和完整工具结果", "local_history 必须给出估算口径说明")


# ------------------------------------------------------------------ (b)
def test_missing_fields_old_backend_payload():
    ind = panel.ContextUsageIndicator()
    ind.set_usage({"unit": "tokens", "used": 500, "max": 1000, "model": "旧模型",
                   "basis": "provider_usage"})
    tip = ind.toolTip()
    for needle in ("旧模型", "50.0%", "500 / 1,000 tokens"):
        contains(tip, needle, f"老后端事件仍要显示上下文（缺 {needle!r}）")
    not_contains(tip, "本回合", "没有实测字段时整段省略「本回合」")
    not_contains(tip, "本次会话", "没有 session 时整段省略「本次会话」")
    not_contains(tip, "None", "缺字段不能渲染成 None")
    eq(ind.accessibleDescription(), tip, "缺字段时 tooltip 也要同步无障碍描述")

    text = ind.details_text()
    contains(text, "旧模型", "浮层仍应显示模型")
    not_contains(text, "本回合", "浮层同样省略空分组")
    not_contains(text, "None", "浮层不应出现 None")

    # 完全没有用量时保持既有文案（test_ui_navigation 有精确断言）。
    ind.set_usage(None)
    eq(ind.toolTip(), "上下文字符预算未启用", "未知用量时保持既有提示")


# ------------------------------------------------------------------ (c)
def test_invalid_values_are_normalized_to_none():
    eq(panel._usage_int(True), None, "bool 不是有效计数")
    eq(panel._usage_int(-1), None, "负数不是有效计数")
    eq(panel._usage_int("80"), None, "字符串不是有效计数")
    eq(panel._usage_int(0), 0, "0 是有效计数")
    eq(panel._usage_rate(2.5), None, "命中率超出 0..1 作废")
    eq(panel._usage_rate(True), None, "bool 不是有效命中率")
    eq(panel._usage_rate("0.5"), None, "字符串不是有效命中率")
    eq(panel._usage_rate(0.75), 0.75, "合法命中率保留")
    eq(panel._normalize_usage_turn("oops"), None, "非 dict 的 turn 作废")
    eq(panel._normalize_usage_turn({"prompt_tokens": -2})["prompt_tokens"], None,
       "turn 内非法字段归一化为 None")

    stub = _StubPanel()
    ev = _event(phase="request")
    ev["model_context"].update({
        "prompt_tokens": "80", "completion_tokens": -5,
        "cache_hit_tokens": True, "cache_miss_tokens": 60, "cache_hit_rate": 2.5,
        "turn": {"prompt_tokens": "7", "completion_tokens": -1,
                 "cache_hit_tokens": True, "cache_miss_tokens": 60,
                 "reasoning_tokens": -2, "llm_seconds": -1.0}})
    stub._on_event(ev, "A")
    entry = stub._context_usage[("A", "m1")]
    eq(entry["prompt_tokens"], None, "字符串输入归一化为 None")
    eq(entry["completion_tokens"], None, "负数输出归一化为 None")
    eq(entry["cache_hit_tokens"], None, "True 不能当真计数")
    eq(entry["cache_miss_tokens"], 60, "合法计数保留")
    eq(entry["cache_hit_rate"], None, "越界命中率归一化为 None")
    eq(entry["turn"]["prompt_tokens"], None, "turn.prompt_tokens 非法 → None")
    eq(entry["turn"]["cache_miss_tokens"], 60, "turn 内合法字段保留")
    eq(entry["turn"]["llm_seconds"], None, "负数耗时 → None")

    stub._sync_context_usage()          # foreground（project == active）
    tip = stub.context_usage.toolTip()
    contains(tip, "0.8%", "上下文占用仍按合法的 used/max 显示")
    contains(tip, "1,000 / 128,000 tokens", "合法的 used/max 照常显示")
    contains(tip, "未命中 60", "合法字段照常展示")
    not_contains(tip, "None", "非法字段绝不能渲染成 None")
    not_contains(tip, "nan", "非法字段不能渲染成 nan")

    # 容量本身非法 → 整条事件丢弃，不写坏用量（也不会崩）。
    stub2 = _StubPanel()
    bad = _event()
    bad["model_context"]["max"] = True
    stub2._on_event(bad, "A")
    ok(("A", "m1") not in stub2._context_usage, "非法容量不得写入用量")


# ------------------------------------------------------------------ 4.4
def test_format_stats_appends_cache_hit_rate():
    stats = {"completion_tokens": 1000, "llm_seconds": 10.0, "model": "gpt-5",
             "cache_hit_tokens": 620, "cache_miss_tokens": 380}
    eq(panel._format_stats(stats, ""),
       "1.0k Tokens · 100.0 Token/秒 · gpt-5 · 缓存命中 62%",
       "有命中/未命中计数时追加命中率，保持 · 分隔与既有顺序")
    eq(panel._format_stats(dict(stats, requests=3), ""),
       "1.0k Tokens · 100.0 Token/秒 · gpt-5 · 缓存命中 62% · 3 次调用",
       "多次调用时再追加调用次数")
    eq(panel._format_stats({"completion_tokens": 1000, "llm_seconds": 10.0,
                            "model": "gpt-5"}, ""),
       "1.0k Tokens · 100.0 Token/秒 · gpt-5", "缺字段时完全不变")
    eq(panel._format_stats(dict(stats, cache_hit_tokens=True), ""),
       "1.0k Tokens · 100.0 Token/秒 · gpt-5", "非法命中计数不参与")
    eq(panel._format_stats(dict(stats, cache_hit_tokens=0, cache_miss_tokens=0), ""),
       "1.0k Tokens · 100.0 Token/秒 · gpt-5", "hit+miss==0 时不算命中率")


# ------------------------------------------------------------------ (d)
def test_session_accumulates_across_two_response_events():
    stub = _StubPanel()
    stub._on_event(_event(phase="request"), "A")
    ok(stub._context_usage[("A", "m1")].get("session") is None,
       "request 阶段不累计（避免同一回合算两次）")

    # 后端 request/streaming 阶段会带一个全 0 的回合累计：不能渲染成「输入 0」。
    zero = _event(phase="request")
    zero["turn"] = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                    "reasoning_tokens": 0, "total_tokens": 0,
                    "measured_requests": 0, "llm_seconds": 0.0}
    # request 阶段后端不会给实测值（顶层与 model_context 都是 None）。
    for target in (zero, zero["model_context"]):
        target.update(prompt_tokens=None, completion_tokens=None,
                      cache_hit_tokens=None, cache_miss_tokens=None,
                      cache_hit_rate=None, total_tokens=None)
    stub._on_event(zero, "A")
    not_contains(stub.context_usage.toolTip(), "本回合",
                 "全 0 的回合累计整段省略，不写成『输入 0』")

    stub._on_event(_event(hit=750, miss=250), "A")
    stub._on_event(_event(hit=600, miss=400), "A")
    session = stub._context_usage[("A", "m1")]["session"]
    eq(session["requests"], 2, "两次 response 事件 → 本次会话 2 次请求")
    eq(session["prompt_tokens"], 2000, "会话输入累加")
    eq(session["completion_tokens"], 200, "会话输出累加")
    eq(session["cache_hit_tokens"], 1350, "会话命中累加")
    eq(session["cache_miss_tokens"], 650, "会话未命中累加")
    rate = session["cache_hit_rate"]
    ok(rate is not None and abs(rate - 0.675) < 1e-9,
       f"会话命中率应为 1350/2000=67.5%，实际 {rate!r}")

    tip = stub.context_usage.toolTip()
    for needle in ("本次会话", "2 次请求", "缓存命中率 67.5%"):
        contains(tip, needle, f"指示器应实时显示本次会话（缺 {needle!r}）")
    contains(stub.context_usage.details_text(), "本次会话", "浮层应有本次会话分组")

    # 项目 / 模型各算各的：另一个模型从 0 开始，m1 不受影响。
    stub._on_event(_event(model="m2", hit=100, miss=900), "A")
    eq(stub._context_usage[("A", "m2")]["session"]["requests"], 1,
       "不同模型各自累计")
    eq(stub._context_usage[("A", "m1")]["session"]["requests"], 2,
       "另一个模型的回合不推进 m1 的会话计数")


def test_session_survives_history_append_but_measured_usage_does_not():
    session = {"requests": 2, "prompt_tokens": 2000, "completion_tokens": 200,
               "cache_hit_tokens": 1350, "cache_miss_tokens": 650,
               "cache_hit_rate": 0.675}
    stub = _StubPanel()
    stub._context_usage = {
        ("A", "m1"): {"used": 100, "max": 1000, "unit": "tokens", "session": dict(session)},
        ("A", "m2"): {"used": 200, "max": 1000, "unit": "tokens", "session": dict(session)},
        ("B", "m1"): {"used": 300, "max": 1000, "unit": "tokens", "session": dict(session)},
    }
    ok(stub._append_history("A", {"role": "user", "content": "新消息"}), "历史应追加成功")
    # 当前模型这一份原样保留；另一模型的实测用量随历史变更作废，但会话累计留着。
    kept_own = stub._context_usage[("A", "m1")]
    eq(kept_own["session"]["requests"], 2, "当前模型会话累计保留")
    eq(kept_own["used"], 100, "当前模型的实测用量不因自己的历史变更而丢")

    kept_other = stub._context_usage[("A", "m2")]
    eq(kept_other, {"session": dict(session)}, "另一模型只保留会话累计")

    eq(stub._context_usage[("B", "m1")]["used"], 300, "其它项目完全不受影响")


def test_popup_never_captures_pointer_or_focus_and_stays_on_screen():
    host = QtWidgets.QWidget()
    host.resize(1000, 700)
    host.show()
    ind = panel.ContextUsageIndicator(host)
    ind.set_usage(_full_usage())
    ind.move(0, 0)
    ind.show()
    APP.processEvents()
    ind._hide_popup()

    # 键盘 Enter 开关浮层（辅助功能要求，不依赖鼠标）。
    press = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_Return,
                            QtCore.Qt.KeyboardModifier.NoModifier)
    ind.keyPressEvent(press)
    popup = ind._popup
    ok(popup is not None and popup.isVisible(), "Enter 应打开详情浮层")
    ok(popup.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents),
       "浮层不能抢鼠标事件")
    eq(popup.focusPolicy(), QtCore.Qt.FocusPolicy.NoFocus, "浮层不能抢键盘焦点")
    ind.keyPressEvent(press)
    ok(not popup.isVisible(), "再次 Enter 应关闭浮层")

    # 悬浮提示事件也走浮层（不再弹超长系统 tooltip）。
    ok(ind.event(QtCore.QEvent(QtCore.QEvent.Type.ToolTip)), "ToolTip 事件由浮层接管")
    ok(popup.isVisible(), "悬浮应打开浮层")

    # 浮层必须是宿主窗口内的子控件，并收敛到宿主可视区。
    ok(not popup.isWindow(), "浮层不得创建顶层窗口")
    eq(popup.parentWidget(), host)
    ind.move(host.width() - ind.width(), host.height() - ind.height())
    ind._show_popup()
    ok(popup.x() >= 0 and popup.y() >= 0, "浮层左上不能越界")
    ok(popup.x() + popup.width() <= host.width(), "浮层右边不得越界")
    ok(popup.y() + popup.height() <= host.height(), "浮层下边不得越界")
    ind._hide_popup()
    ind.hide()


def test_hover_leave_and_refresh_timer_lifecycle():
    from PySide6.QtTest import QTest
    host = QtWidgets.QWidget()
    host.resize(1000, 700)
    ind = panel.ContextUsageIndicator(host)
    ind.move(400, 400)
    host.show()
    ind.show()
    APP.processEvents()
    ind._hide_popup()
    enter = QtGui.QEnterEvent(QtCore.QPointF(5, 5), QtCore.QPointF(5, 5), QtCore.QPointF(5, 5))
    APP.sendEvent(ind, enter)
    QTest.qWait(100)
    APP.sendEvent(ind, QtCore.QEvent(QtCore.QEvent.Type.Leave))
    QTest.qWait(350)
    ok(ind._popup is None or not ind._popup.isVisible(), "离开后不可迟到弹出")

    ind._show_popup()
    popup = ind._popup
    ok(not popup.isWindow(), "浮层不能是原生顶层窗口")
    old_label = popup._labels[0]
    ind.set_usage(_full_usage())
    # 重建内容时旧标签不得变成独立顶层窗口。
    ok(old_label.parentWidget() is popup, "刷新不得将标签脱离宿主")
    APP.sendEvent(ind, QtCore.QEvent(QtCore.QEvent.Type.Leave))
    ok(ind._leave_timer.isActive(), "离开后应开始关闭倒计时")
    QTest.qWait(80)
    ind.set_usage(dict(_full_usage(), used=63000))
    ok(ind._leave_timer.isActive(), "实时刷新不得取消关闭倒计时")
    QTest.qWait(250)
    ok(not popup.isVisible(), "离开且持续刷新时仍须关闭")
    host.close()
    APP.processEvents()


def test_first_mouse_click_opens_popup_and_host_hide_closes_it():
    from PySide6.QtTest import QTest
    host = QtWidgets.QWidget()
    host.resize(800, 600)
    ind = panel.ContextUsageIndicator(host)
    ind.move(300, 300)
    host.show()
    ind.show()
    APP.processEvents()
    ind._hide_popup()
    ind.clearFocus()
    QTest.mouseClick(ind, QtCore.Qt.MouseButton.LeftButton)
    ok(ind._popup is not None and ind._popup.isVisible(), "首次鼠标点击应打开浮层")
    host.hide()
    APP.processEvents()
    ok(not ind._popup.isVisible(), "宿主隐藏时浮层应关闭")
    host.close()


if __name__ == "__main__":
    raise SystemExit(run(globals(), "上下文用量 UI"))
