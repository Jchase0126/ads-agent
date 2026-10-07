"""项目会话隔离测试（需要 PySide6，不需要 ADS —— 用 offscreen 平台跑真实面板控件）。

覆盖评审第 4 项「隔离项目会话」：

* 发送时**固定**所属项目；期间切换到别的项目，异步回复 / 工具事件 / 错误
  仍然写回原项目，不污染当前项目，也不丢内容；
* 状态栏只在"事件属于当前项目"时才更新；
* 回复在途时删掉该项目：后续事件被丢弃并计数，**绝不把项目复活**；
* 清空会话保持 ``entries`` / ``history`` 与 ``projects_data`` 的**同一对象引用**
  （否则清掉的对话会随着下一次写入"复活"）；
* 长耗时工具的耗时状态，以及"仿真期间界面会不会卡"的**如实描述**
  （串行模式必须明说会无响应，不能统一宣称不阻塞）。

用临时 projects.json 与临时 config.ini，**不碰仓库里的 projects.json / config.ini**。

运行（需要一个装了 PySide6 的解释器）::

    python tests/test_project_isolation.py
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

# 必须在导入 PySide6 之前：无显示环境下用 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能运行本测试：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

_TMP = tempfile.mkdtemp(prefix="ads_agent_proj_")
_TMPDIRS = [_TMP]

# 面板构造时会读 config.ini 取后端地址；指向临时配置，避免误连真后端
os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url = http://127.0.0.1:1\nmodel = m\n\n[ads]\nport = 8761\n")

import panel  # noqa: E402

import authbridge  # noqa: E402

_APP = None


class _StubWorker(QObject):
    """替身 ChatWorker：不发真实 HTTP，只把信号暴露给测试手动触发。"""

    event_received = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, messages, allow_python, model, parent=None):
        super().__init__(parent)
        self.messages = list(messages)
        self.allow_python = allow_python
        self.model = model
        self._running = False

    def isRunning(self):  # noqa: N802 — 与 QThread 接口一致
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


# 隔离面板与外部世界
panel.ChatWorker = _StubWorker
panel.AgentPanelWidget.reload_config = lambda self: None        # 不发起 /config 请求
panel.AgentPanelWidget._auto_revive = lambda self, retry=None: None  # 不真的拉后端
panel._backend_base = lambda: "http://127.0.0.1:1"


def _teardown():
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _new_panel(names=("A",), active=None):
    """建一个面板，projects.json 落在临时目录，并按需预置若干空项目。"""
    _app()
    d = tempfile.mkdtemp(prefix="ads_agent_proj_")
    _TMPDIRS.append(d)
    panel.AgentPanelWidget._PROJECTS_FILE = os.path.join(d, "projects.json")
    w = panel.AgentPanelWidget()
    names = list(names)
    w.projects_data = {
        "active": active or names[0],
        "projects": {n: {"entries": [], "history": []} for n in names},
    }
    w._apply_project(w.projects_data["active"], create=True)
    w._refresh_project_list()
    return w


def _entries(w, name):
    return w.projects_data["projects"][name]["entries"]


def _history(w, name):
    return w.projects_data["projects"][name]["history"]


def _texts(w, name):
    return [e["text"] for e in _entries(w, name)]


def _select(w, name):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem  # noqa: F401

    for i in range(w.project_list.count()):
        item = w.project_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == name:
            item.setSelected(True)
            return True
    return False


def _send(w, text="hello"):
    w.input.setPlainText(text)
    w._on_send()
    return w._worker


# ---------------------------------------------------------------------------
# 基本约定
# ---------------------------------------------------------------------------

def test_apply_project_does_not_create_missing_project():
    w = _new_panel(("A",), active="A")
    eq(w._apply_project("ghost"), False, "切换到一个不存在的项目应当失败")
    ok("ghost" not in w.projects_data["projects"], "不应隐式新建项目")
    eq(w.projects_data["active"], "A", "失败的切换不应改变当前项目")


def test_entries_and_history_are_live_references():
    w = _new_panel(("A",), active="A")
    ok(w.entries is _entries(w, "A"), "entries 应当是项目里那个 list 本身")
    ok(w.history is _history(w, "A"), "history 应当是项目里那个 list 本身")
    w._apply_project("A", create=True)
    ok(w.entries is _entries(w, "A"))


def test_add_entry_defaults_to_active_project():
    w = _new_panel(("A",), active="A")
    ok(w._add_entry("user", "x"), "写入当前项目应成功")
    eq(_texts(w, "A"), ["x"])


def test_append_history_rejects_deleted_project():
    w = _new_panel(("A",), active="A")
    eq(w._append_history("A", {"role": "user", "content": "x"}), True)
    eq(len(_history(w, "A")), 1)
    eq(w._append_history("ghost", {"role": "user", "content": "x"}), False,
       "已删除项目的历史必须被丢弃")
    ok("ghost" not in w.projects_data["projects"])


# ---------------------------------------------------------------------------
# 回复期间切换项目
# ---------------------------------------------------------------------------

def test_send_binds_the_project_at_send_time():
    w = _new_panel(("A", "B"), active="A")
    worker = _send(w, "hello")
    eq(w._turn_project, "A", "发送时就该固定本轮所属项目")
    eq(_texts(w, "A"), ["hello"], "用户消息应写进发送时所在的项目")
    eq(_texts(w, "B"), [], "不应写进别的项目")
    eq(worker.messages, [{"role": "user", "content": "hello"}], "应带上本轮历史")
    worker._running = False


def test_events_land_in_the_owning_project_after_switching():
    w = _new_panel(("A", "B"), active="A")
    worker = _send(w, "hello")

    # 用户在等待回复期间切到 B
    eq(w._apply_project("B"), True)
    eq(w.projects_data["active"], "B")
    ok(w.entries is _entries(w, "B"))

    # 之后到达的异步事件仍属于 A
    worker.event_received.emit({"type": "assistant", "text": "answer-for-A"})
    worker.event_received.emit({"type": "tool_call", "name": "run_simulation",
                                "arguments": {"library": "L", "cell": "C"}})
    worker.event_received.emit({"type": "tool_result", "name": "run_simulation",
                                "ok": True, "summary": "done"})
    worker.event_received.emit({"type": "notice", "text": "预算将尽"})
    worker.event_received.emit({"type": "error", "message": "boom"})

    a_texts = " | ".join(_texts(w, "A"))
    contains(a_texts, "answer-for-A", "回复没有写回原项目")
    a_acts = [e for e in _entries(w, "A") if e["kind"] == "activity"]
    eq(len(a_acts), 1, "工具事件应写回原项目的思考折叠条")
    eq(a_acts[0].get("tools"), 1, "工具调用计入原项目折叠条（详情只进后台日志）")
    contains(a_texts, "预算将尽", "提示事件没有写回原项目")
    contains(a_texts, "boom", "错误事件没有写回原项目")

    eq(_texts(w, "B"), [], "当前项目被别的项目的回复污染了")
    eq(w.entries, [], "前台列表被后台项目的事件污染了")

    # 历史也要写回原项目
    roles = [(m["role"], m["content"]) for m in _history(w, "A")]
    ok(("assistant", "answer-for-A") in roles, "assistant 消息没有写回原项目历史")
    eq(_history(w, "B"), [], "别的项目的历史被污染了")

    worker._running = False


def test_status_only_updated_by_foreground_project():
    w = _new_panel(("A", "B"), active="A")
    worker = _send(w, "hello")
    w._apply_project("B")
    w.status.setText("前台哨兵")

    worker.event_received.emit({"type": "status", "text": "后台项目在处理"})
    eq(w.status.text(), "前台哨兵", "后台项目的 status 事件改动了当前状态栏")
    worker.event_received.emit({"type": "done"})
    eq(w.status.text(), "前台哨兵", "后台项目的 done 事件改动了当前状态栏")

    worker.event_received.emit({"type": "assistant", "text": "n"})
    contains(w.status.text(), "项目「A」", "后台项目有新内容时应在状态栏提示")

    worker._running = False


def test_foreground_events_still_update_status():
    w = _new_panel(("A",), active="A")
    worker = _send(w, "hello")
    worker.event_received.emit({"type": "status", "text": "正在调用工具"})
    eq(w.status.text(), "正在调用工具")
    worker.event_received.emit({"type": "done"})
    eq(w.status.text(), "完成")
    worker._running = False


def test_tool_timer_ignores_background_project():
    w = _new_panel(("A", "B"), active="B")
    w._start_tool_timer("run_simulation", "A")
    eq(w._tool_name, "", "后台项目的工具调用不应改当前状态栏")
    w._start_tool_timer("run_simulation", "B")
    eq(w._tool_name, "run_simulation")
    w._stop_tool_timer()
    eq(w._tool_name, "")


def test_worker_done_reports_background_project():
    w = _new_panel(("A", "B"), active="A")
    worker = _send(w, "hello")
    w._apply_project("B")
    worker._running = False
    worker.finished.emit()          # -> _on_worker_done("A")

    eq(w.send.isEnabled(), True, "回复结束后应重新可用发送键")
    eq(w._turn_project, None, "回合结束后应清掉固定项目")
    contains(w.status.text(), "项目「A」", "应提示回复已完成的是别的项目")

    worker2 = _send(w, "second")
    eq(w._turn_project, "B", "下一次发送应固定到当前项目")
    worker2._running = False


def test_on_failed_writes_to_the_owning_project():
    w = _new_panel(("A", "B"), active="B")
    w._on_failed("connect boom", "A")
    contains(" | ".join(_texts(w, "A")), "connect boom", "错误没有写回原项目")
    eq(_texts(w, "B"), [], "错误污染了当前项目")


# ---------------------------------------------------------------------------
# 删除在途项目
# ---------------------------------------------------------------------------

def test_delete_project_mid_reply_drops_events_without_resurrecting():
    w = _new_panel(("A", "B", "C"), active="A")
    worker = _send(w, "hello")
    eq(w._turn_project, "A")

    ok(_select(w, "A"), "前置条件：项目列表里应能选中 A")
    w._delete_project()
    ok("A" not in w.projects_data["projects"], "A 应已被删除")
    ne(w.projects_data["active"], "A", "当前项目不应还是被删掉的 A")
    b_before = list(_texts(w, "B"))
    c_before = list(_texts(w, "C"))

    worker.event_received.emit({"type": "assistant", "text": "late answer"})
    worker.event_received.emit({"type": "tool_call", "name": "run_simulation",
                                "arguments": {"library": "L", "cell": "C"}})
    worker.event_received.emit({"type": "tool_result", "name": "run_simulation",
                                "ok": True, "summary": "late"})

    ok("A" not in w.projects_data["projects"],
       "已删除的项目被异步事件复活了 —— 会话隔离失效")
    ok(w._dropped_events >= 2, f"应记录被丢弃的事件数，实际 {w._dropped_events}")
    eq(_texts(w, "B"), b_before, "事件写进了别的项目")
    eq(_texts(w, "C"), c_before, "事件写进了别的项目")

    worker._running = False
    worker.finished.emit()
    contains(w.status.text(), "已丢弃", "应告知用户有内容因为项目被删而被丢弃")


def test_delete_project_warns_about_inflight_turn():
    w = _new_panel(("A", "B"), active="A")
    _send(w, "hello")
    ok(_select(w, "A"))
    w._delete_project()
    contains(w.status.text(), "在途", "删除有回复在途的项目时应给出提示")


def test_delete_last_project_is_refused():
    w = _new_panel(("A",), active="A")
    ok(_select(w, "A"))
    w._delete_project()
    ok("A" in w.projects_data["projects"], "至少应保留一个项目")
    contains(w.cfg_hint.text(), "至少保留一个项目")


def test_switch_during_reply_then_delete_keeps_other_project_clean():
    """切换 + 删除叠加：事件既不污染新项目，也不复活被删项目。"""
    w = _new_panel(("A", "B"), active="A")
    worker = _send(w, "hello")
    w._apply_project("B")
    ok(_select(w, "A"))
    w._delete_project()
    worker.event_received.emit({"type": "assistant", "text": "x"})
    eq(_texts(w, "B"), [], "新项目被污染了")
    ok("A" not in w.projects_data["projects"], "被删项目被复活了")
    eq(w.projects_data["active"], "B")
    worker._running = False


# ---------------------------------------------------------------------------
# 清空会话不能"复活"旧对话
# ---------------------------------------------------------------------------

def test_clear_chat_keeps_live_reference():
    w = _new_panel(("A",), active="A")
    w._add_entry("user", "m1", "A")
    live = _entries(w, "A")
    eq(len(live), 1)

    w._clear_chat()
    ok(w.entries is live, "清空后 entries 不再是项目里那个 list")
    eq(len(live), 0, "清空应作用于项目里那个 list")
    eq(len(_entries(w, "A")), 0)

    w._add_entry("user", "m2", "A")
    eq(len(live), 1, "清空后新消息写进了另一个 list（旧对话会复活）")
    eq(live[0]["text"], "m2")


def test_on_clear_clears_history_in_place():
    w = _new_panel(("A",), active="A")
    w._add_entry("user", "m1", "A")
    w._append_history("A", {"role": "user", "content": "m1"})
    live_entries = _entries(w, "A")
    live_history = _history(w, "A")

    w._on_clear()
    ok(w.history is live_history, "清空后 history 不再是项目里那个 list")
    eq(len(live_entries), 0)
    eq(len(live_history), 0)
    eq(len(_entries(w, "A")), 0)
    eq(len(_history(w, "A")), 0)


# ---------------------------------------------------------------------------
# 仿真期间的如实描述
# ---------------------------------------------------------------------------

def test_sim_status_is_honest_about_serial_mode():
    w = _new_panel(("A",), active="A")
    w._tool_name = "run_simulation"
    w._tool_t0 = time.perf_counter() - 10

    w._sim_off_main = False
    w._tick_tool_status()
    contains(w.status.text(), "会无响应",
             "串行模式下必须明说界面会无响应，不能统一宣称不阻塞")
    not_contains(w.status.text(), "后台线程", "串行模式不该说跑了后台线程")

    w._sim_off_main = True
    w._tick_tool_status()
    contains(w.status.text(), "后台线程")
    not_contains(w.status.text(), "会无响应")


def test_sim_status_shows_elapsed_time():
    w = _new_panel(("A",), active="A")
    w._tool_name = "run_simulation"
    w._tool_t0 = time.perf_counter() - 7
    w._tick_tool_status()
    contains(w.status.text(), "run_simulation")
    contains(w.status.text(), "7", "应显示已用时间，作为'ADS 还活着'的可见信号")


def test_config_loaded_reads_sim_flag_from_backend():
    w = _new_panel(("A",), active="A")
    w._on_config_loaded({
        "base_url": "http://127.0.0.1:1", "model": "m", "models": ["m"],
        "has_key": False, "api_key_hint": "",
        "sim_off_main_thread": False, "sim_timeout": 60,
    })
    eq(w._sim_off_main, False, "应采纳后端告知的仿真执行方式")
    w._on_config_loaded({"base_url": "http://x", "model": "m", "models": []})
    eq(w._sim_off_main, True, "后端未告知时按默认（后台线程）处理")


def test_short_tool_status_has_no_simulation_note():
    w = _new_panel(("A",), active="A")
    w._tool_name = "run_simulation"
    w._tool_t0 = time.perf_counter()          # 刚开始，还没到 5s
    w._tick_tool_status()
    not_contains(w.status.text(), "后台线程", "刚起步时不必解释仿真行为")


def test_tool_activity_collapses_and_restores():
    w = _new_panel(("A",), active="A")
    worker = _send(w, "run")
    for name in ("build_schematic", "run_simulation"):
        worker.event_received.emit({"type": "tool_call", "name": name,
                                    "arguments": {"cell": "Wilkinson_2G4_v2"}})
        worker.event_received.emit({"type": "tool_result", "name": name,
                                    "ok": True, "summary": "done"})
    activities = [e for e in _entries(w, "A") if e["kind"] == "activity"]
    eq(len(activities), 1, "一轮工具事件应合并为一个折叠条")
    eq(activities[0]["tools"], 2, "工具调用只累计次数")
    eq(activities[0]["events"], [], "工具详情不再写进面板（只进后台日志）")
    row = next(w.chat.itemWidget(w.chat.item(i)) for i in range(w.chat.count())
               if isinstance(w.chat.itemWidget(w.chat.item(i)), panel.ActivityRow))
    eq(row.details.isVisible(), False)
    row.toggle.click()
    eq(activities[0]["expanded"], True)
    w._rebuild()
    row = next(w.chat.itemWidget(w.chat.item(i)) for i in range(w.chat.count())
               if isinstance(w.chat.itemWidget(w.chat.item(i)), panel.ActivityRow))
    eq(row.toggle.isChecked(), True, "重建界面后应保留展开状态")
    worker.event_received.emit({"type": "done"})
    eq(activities[0]["complete"], True)
    worker._running = False


def test_input_clipboard_buttons():
    w = _new_panel(("A",), active="A")
    w.input.setPlainText("abc")
    w.input.selectAll()
    ok(w.copy_btn.isEnabled())
    w.copy_btn.click()
    eq(QApplication.clipboard().text(), "abc")
    w.cut_btn.click()
    eq(w.input.toPlainText(), "")
    w.paste_btn.click()
    eq(w.input.toPlainText(), "abc")


# ---------------------------------------------------------------------------
# 鉴权头
# ---------------------------------------------------------------------------

def test_panel_loopback_opener_bypasses_http_proxy():
    """面板访问后端走的是绕过代理的 opener（否则设了 HTTP_PROXY 就误报"后端未启动"）。

    行为验证在 tests/test_auth.py（真的把 HTTP_PROXY 指向死代理再打一次回环请求）；
    这里只做结构断言：opener 里不能有任何**生效**的代理配置。
    """
    import urllib.request

    ok(hasattr(panel, "_LOOPBACK"), "panel.py 缺少绕过代理的回环 opener")
    active = [h.proxies for h in panel._LOOPBACK.handlers
              if isinstance(h, urllib.request.ProxyHandler) and h.proxies]
    eq(active, [], "回环 opener 不应带任何生效的代理配置")


def test_panel_auth_header_uses_the_shared_token():
    header = panel._auth_header()
    name = authbridge.header_name()
    eq(list(header.keys()), [name], "面板应带上共享令牌请求头")
    eq(header[name], authbridge.token())
    ne(header[name], "", "面板应能拿到令牌")
    ne(header[name], "ads-agent-local-token", "面板不应再用公开默认令牌")


def test_panel_auth_header_survives_a_broken_bridge():
    """取不到令牌时不能抛异常把界面搞崩，要发出去让后端拒绝（401 有明确提示）。"""
    saved = panel.__dict__.get("__test_bridge_broken__")
    real_import = __import__

    def _boom(name, *a, **k):
        if name == "authbridge":
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    import builtins
    builtins.__import__ = _boom
    try:
        eq(panel._auth_header(), {}, "取不到令牌时应返回空头而不是抛异常")
    finally:
        builtins.__import__ = real_import
    _ = saved


if __name__ == "__main__":
    try:
        code = run(globals(), "项目会话隔离（PySide6 / offscreen）")
    finally:
        _teardown()
    sys.exit(code)
