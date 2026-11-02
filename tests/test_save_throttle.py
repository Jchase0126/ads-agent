"""会话保存的合并写与活动行就地刷新（需要 PySide6，不需要 ADS）。

背景（实测）：一次长回合里每个 ``tool_call`` 事件、每段思考都会触发
``_save_projects``，而 ``project_store.save`` 是全量 ``json.dumps`` + ``fsync``
—— 1000 条条目 28ms、3000 条 71ms（见 docs 实施报告）。几十次叠加就把 GUI
线程占满；同一条路径上还会 ``chat.clear()`` 整表重建。

本测试锁住三件事：

1. 热路径（tool_call / 思考 / 附件轮询 / 任务状态轮询）**不**同步全量落盘，
   只登记"待保存"；
2. 落盘写的是**合并窗口边界上的最新快照** —— 多次热更新只写一次，且写入的
   内容是最新的，不存在旧快照覆盖新快照；
3. 必须落定的时机（切换/清空/回合结束/关闭面板）仍然立刻写，写失败照常提示。

运行：``python tests/test_save_throttle.py``
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

add_path("addon", "ads_agent")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能运行本测试：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

_TMP = tempfile.mkdtemp(prefix="ads_agent_save_")
os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url = http://127.0.0.1:1\nmodel = m\n\n[ads]\nport = 8761\n")

import panel  # noqa: E402
import project_store  # noqa: E402

_APP = None
_DIRS = [_TMP]


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _new_panel():
    """临时 projects.json 上的空面板；不连后端、不读真实会话。"""
    _app()
    d = tempfile.mkdtemp(prefix="ads_agent_save_panel_")
    _DIRS.append(d)
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


class _CountingStore:
    """替换 project_store.save，记录次数与每次写入的条目数。"""

    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    def __call__(self, path, data):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise OSError("磁盘不可写（测试注入）")
        entries = ((data.get("projects") or {}).get("A") or {}).get("entries") or []
        self.calls.append(len(entries))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


def _preseed_activity(w):
    """先把折叠行建出来（_ensure_activity 会走 _add_entry 的立即落盘与首条重建）。

    这一步是**必须落定**的时机，不属于热路径；为了让断言只看热路径，把
    计数在准备完成后清空。
    """
    return w._ensure_activity("A")


def test_hot_path_does_not_save_synchronously():
    w = _new_panel()
    counter = _CountingStore()
    original = project_store.save
    project_store.save = counter
    try:
        entry = _preseed_activity(w)
        counter.calls.clear()
        for i in range(20):
            entry["reasoning"].append(f"第 {i} 段")
            entry["text"] = f"思考 · {len(entry['reasoning'])} 段"
            w._refresh_activity(entry, "A")
        eq(counter.calls, [],
           "热路径不得同步全量落盘：20 次 tool_call/思考不能触发 20 次 JSON + fsync")
        ok(w._save_pending, "热路径应只登记待保存，交给合并写窗口")
        # 一次落定只写一次，且写的是最新快照
        w.flush_projects()
        eq(len(counter.calls), 1, "合并窗口内多次热更新只能落盘一次")
        eq(counter.calls[0], len(w.projects_data["projects"]["A"]["entries"]),
           "落盘内容必须是边界时刻的最新快照")
    finally:
        project_store.save = original
        w.close()


def test_merged_save_never_writes_a_stale_snapshot():
    w = _new_panel()
    counter = _CountingStore()
    original = project_store.save
    project_store.save = counter
    try:
        w._add_entry("user", "第一条")             # 立即落盘（必须落定的时机）
        counter.calls.clear()
        w._save_projects(immediate=False)          # 挂起一次
        w._add_entry("assistant", "第二条")        # 之后又有新内容（也是立即落盘）
        counter.calls.clear()
        w._save_projects(immediate=False)
        eq(counter.calls, [], "两次热更新都不该立刻落盘")
        w.flush_projects()
        eq(counter.calls[-1], 2,
           "旧快照不能覆盖新快照：落盘条目数必须是最新的 2 条")
        saved = json.load(open(w._PROJECTS_FILE, encoding="utf-8"))
        texts = [e["text"] for e in saved["projects"]["A"]["entries"]]
        eq(texts, ["第一条", "第二条"], "落盘内容必须是合并窗口后的完整会话")
    finally:
        project_store.save = original
        w.close()


def test_save_timer_writes_pending_state():
    w = _new_panel()
    counter = _CountingStore()
    original = project_store.save
    project_store.save = counter
    try:
        w._add_entry("user", "定时器用例")
        counter.calls.clear()
        w._save_projects(immediate=False)
        ok(w._save_timer.isActive(), "合并写应启动单发定时器")
        w._save_timer.timeout.emit()               # 直接触发槽位，不等真实时钟
        eq(len(counter.calls), 1, "定时器到点必须真的落盘")
        ok(not w._save_pending, "落盘后不应再有挂起的保存")
    finally:
        project_store.save = original
        w.close()


def test_immediate_sites_still_write_right_away():
    w = _new_panel()
    counter = _CountingStore()
    original = project_store.save
    project_store.save = counter
    try:
        w._add_entry("user", "切换项目前的消息")
        counter.calls.clear()
        w._save_projects(immediate=False)
        w._save_projects()                        # 必须落定的时机
        eq(len(counter.calls), 1,
           "切换/清空/回合结束这类时机必须立刻落盘，不能等合并窗口")
    finally:
        project_store.save = original
        w.close()


def test_save_failure_is_reported_not_swallowed():
    w = _new_panel()
    counter = _CountingStore(fail_after=0)
    original = project_store.save
    project_store.save = counter
    try:
        w._add_entry("user", "写盘会失败")
        w._save_projects()
        contains(w.status.text(), "会话保存失败",
                 "保存失败必须在状态栏提示，不能静默丢数据")
    finally:
        project_store.save = original
        w.close()


def test_close_flushes_pending_save():
    w = _new_panel()
    counter = _CountingStore()
    original = project_store.save
    project_store.save = counter
    try:
        w._add_entry("user", "关闭前的最后一条")
        counter.calls.clear()
        w._save_projects(immediate=False)
        eq(counter.calls, [], "此时还没落盘")
        w.close()                                  # closeEvent 必须落定
        eq(len(counter.calls), 1,
           "关闭面板前必须把挂起的会话写入落定，否则最后几条消息会丢")
    finally:
        project_store.save = original


def test_activity_refresh_updates_row_in_place_without_rebuild():
    w = _new_panel()
    rebuilds = {"n": 0}
    original = w._rebuild

    def counting_rebuild(*a, **kw):
        rebuilds["n"] += 1
        return original(*a, **kw)

    w._rebuild = counting_rebuild
    try:
        entry = _preseed_activity(w)
        rebuilds["n"] = 0                      # 首条消息的重建不算热路径
        entry["reasoning"].append("第一段")
        entry["text"] = "思考 · 1 段"
        w._refresh_activity(entry, "A")            # 行已渲染 -> 就地刷新
        eq(rebuilds["n"], 0,
           "活动行已在列表里时必须就地刷新，不得 chat.clear() 整表重建")
        entry["reasoning"].append("第二段")
        entry["text"] = "思考 · 2 段"
        w._refresh_activity(entry, "A")
        eq(rebuilds["n"], 0, "重复刷新也不该重建整表")
    finally:
        w._rebuild = original
        w.close()


def test_background_project_refresh_saves_but_does_not_repaint():
    w = _new_panel()
    counter = _CountingStore()
    original = project_store.save
    project_store.save = counter
    try:
        entry = _preseed_activity(w)
        counter.calls.clear()
        w._refresh_activity(entry, "不存在的项目")
        eq(counter.calls, [], "后台项目刷新也只登记待保存")
        ok(w._save_pending, "后台项目同样要落盘（稍后合并）")
    finally:
        project_store.save = original
        w.close()


def teardown():
    for d in _DIRS:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(run(globals(), "会话保存合并写 / 活动行就地刷新"))
    finally:
        teardown()
