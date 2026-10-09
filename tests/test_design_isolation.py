"""设计结果页的项目隔离与重启恢复测试（需要 PySide6，不需要 ADS）。

覆盖第四轮需求里的「结果页与项目会话关联」：

* 结果页在**发送/发布时**就固定所属项目；期间切换项目，结果仍写回原项目，
  不污染当前项目；
* 结果在途时删掉该项目 -> 丢弃并计数，**绝不复活**已删除的项目；
* 项目里只存 ``job_id``（轻量、可 JSON 序列化），完整任务在
  ``design_jobs/<id>.json``；**重启 ADS 后**重新渲染仍能打开完整结果页；
* 任务文件丢失时给一句可操作的提示，而不是崩掉；
* "重新仿真 / 重新评估"回来时只更新**它所属项目**的那一条。

运行（需要一个装了 PySide6 的解释器）::

    python tests/test_design_isolation.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能运行本测试：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

_TMP = tempfile.mkdtemp(prefix="ads_agent_result_")
_TMPDIRS = [_TMP]

os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url = http://127.0.0.1:1\nmodel = m\n\n[ads]\nport = 8761\n")

import panel  # noqa: E402

import result_page as rp  # noqa: E402

_APP = None


class _StubWorker(QObject):
    event_received = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, messages, allow_python, model, parent=None):
        super().__init__(parent)
        self._running = False

    def isRunning(self):  # noqa: N802
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


panel.ChatWorker = _StubWorker
panel.AgentPanelWidget.reload_config = lambda self: None
panel.AgentPanelWidget._auto_revive = lambda self, retry=None: None
panel._backend_base = lambda: "http://127.0.0.1:1"


def _teardown():
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _job(job_id="dj_test_1", title="Amp24G 设计评估", passed=True, n=200):
    xs = [2.0 + i * 0.005 for i in range(n)]
    ys = [16.0 - 0.5 * ((x - 2.4) / 0.6) ** 2 for x in xs]
    return {
        "job_id": job_id, "title": title,
        "requirement": "设计一个 2.4 GHz 放大器，带内增益至少 15 dB",
        "design_ref": "AI_lib:Amp24G:schematic",
        "design": {"library": "AI_lib", "cell": "Amp24G", "view": "schematic"},
        "band": {"label": "2.3–2.5 GHz"},
        "stage": "done", "stage_label": "全部指标达标",
        "verdict": "pass" if passed else "fail",
        "summary": {"n_metrics": 1, "n_passed": 1 if passed else 0,
                    "n_failed": 0 if passed else 1, "n_unknown": 0},
        "iterations": [{"n": 1}],
        "metrics": [{"id": "gain", "label": "带内增益", "target": 15.0,
                     "actual": 15.9 if passed else 12.0, "unit": "dB",
                     "comparator": ">=", "pass": passed, "at": "2.3 GHz", "note": ""}],
        "artifacts": {
            "output_dir": "/ws/ads_agent_sim/Amp24G_1",
            "dataset_path": "/ws/ads_agent_sim/Amp24G_1/Amp24G.ds",
            "netlist_path": "/ws/ads_agent_sim/Amp24G_1/netlist.ckt",
            "traces": {"dB(S(2,1))": {"x": xs, "y": ys, "x_unit": "GHz", "y_unit": "dB",
                                      "n_points": n, "y_name": "dB(S(2,1))",
                                      "source": "/ws/ads_agent_sim/Amp24G_1/Amp24G.ds"}},
        },
        "sim": {"status": "done", "finished_at": "2026-09-24T17:00:05"},
        "error": "",
    }


def _new_panel(names=("A",), active=None, jobs_dir=None):
    _app()
    d = tempfile.mkdtemp(prefix="ads_agent_result_")
    _TMPDIRS.append(d)
    panel.AgentPanelWidget._PROJECTS_FILE = os.path.join(d, "projects.json")
    if jobs_dir is not None:
        panel.AgentPanelWidget._DESIGN_JOBS = jobs_dir
    w = panel.AgentPanelWidget()
    names = list(names)
    w.projects_data = {"active": active or names[0],
                       "projects": {n: {"entries": [], "history": []} for n in names}}
    w._apply_project(w.projects_data["active"], create=True)
    w._refresh_project_list()
    return w


def _entries(w, name):
    return w.projects_data["projects"][name]["entries"]


def _select(w, name):
    from PySide6.QtCore import Qt

    for i in range(w.project_list.count()):
        item = w.project_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == name:
            item.setSelected(True)
            return True
    return False


def _rows(w):
    """当前会话里渲染出来的控件列表。"""
    out = []
    for i in range(w.chat.count()):
        out.append(w.chat.itemWidget(w.chat.item(i)))
    return out


def _result_rows(w):
    return [r for r in _rows(w) if isinstance(r, rp.ResultPageRow)]


# ---------------------------------------------------------------------------
# 条目形态：只存 job_id，可 JSON 序列化
# ---------------------------------------------------------------------------

def test_result_entry_stores_only_the_job_id():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs1"))
    job = _job()
    ok(w._add_entry("result", job["title"], "A", payload=job))
    entry = _entries(w, "A")[0]
    eq(entry["kind"], "result")
    eq(entry["job_id"], job["job_id"])
    eq("job" in entry, False, "projects.json 里不该塞整份任务（含曲线，太大）")
    ok(json.dumps(entry, ensure_ascii=False), "条目必须可 JSON 序列化")
    ok(w._job_for(job["job_id"]) is job, "任务应缓存在内存里")


def test_result_entry_renders_a_result_page():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs2"))
    w._add_entry("result", "Amp24G 设计评估", "A", payload=_job())
    rows = _result_rows(w)
    eq(len(rows), 1, "应渲染出一个结果页控件")
    row = rows[0]
    eq(row.job()["job_id"], "dj_test_1")
    contains(row.verdict.text(), "达标")
    contains(row.design_ref.text(), "AI_lib:Amp24G:schematic")
    ok(row.chart.has_curves(), "结果页必须带真实曲线")
    eq(row.resim_btn.text(), "重新仿真")
    eq(row.open_btn.text(), "打开原理图")


# ---------------------------------------------------------------------------
# 项目隔离
# ---------------------------------------------------------------------------

def test_result_lands_in_the_owning_project_after_switching():
    w = _new_panel(("A", "B"), active="A", jobs_dir=os.path.join(_TMP, "jobs3"))
    job = _job()
    eq(w._apply_project("B"), True)                      # 用户切到 B
    ok(w._add_entry("result", job["title"], "A", payload=job),
       "结果应写回原项目 A")
    eq([e["kind"] for e in _entries(w, "A")], ["result"])
    eq(_entries(w, "B"), [], "当前项目 B 被别的项目的结果污染了")
    eq(_result_rows(w), [], "后台项目的结果不应渲染到前台")
    contains(w.status.text(), "项目「A」", "应在状态栏提示后台项目有新结果")


def test_design_result_event_is_routed_by_project():
    w = _new_panel(("A", "B"), active="B", jobs_dir=os.path.join(_TMP, "jobs4"))
    job = _job()
    w._on_event({"type": "design_result", "job": job}, "A")
    eq(len(_entries(w, "A")), 1)
    eq(_entries(w, "B"), [], "事件不该写进当前项目 B")
    eq(_result_rows(w), [])

    w._apply_project("A")                                # 切回 A 应能看到
    eq(len(_result_rows(w)), 1, "切回原项目后应渲染出结果页")


def test_result_for_a_deleted_project_is_dropped():
    w = _new_panel(("A", "B"), active="A", jobs_dir=os.path.join(_TMP, "jobs5"))
    ok(_select(w, "A"))
    w._delete_project()
    ok("A" not in w.projects_data["projects"])
    ok(not w._add_entry("result", "迟到的结果", "A", payload=_job()),
       "已删除项目的结果必须被丢弃")
    ok("A" not in w.projects_data["projects"], "已删除的项目被结果页复活了")
    eq(w._dropped_events, 1, "应记录被丢弃的条数")


def test_design_stage_event_only_touches_the_foreground():
    w = _new_panel(("A", "B"), active="B", jobs_dir=os.path.join(_TMP, "jobs6"))
    w.status.setText("前台哨兵")
    w._on_event({"type": "design_stage", "stage": "simulating", "text": "正在仿真…"}, "A")
    eq(w.status.text(), "前台哨兵", "后台项目的进度不该改当前状态栏")
    w._on_event({"type": "design_stage", "stage": "simulating", "text": "正在仿真…"}, "B")
    eq(w.status.text(), "正在仿真…")


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------

def test_result_page_survives_a_restart():
    """模拟重启 ADS：projects.json 里只有 job_id，任务文件在 design_jobs/。"""
    jobs_dir = os.path.join(_TMP, "jobs_restart")
    os.makedirs(jobs_dir, exist_ok=True)
    job = _job(job_id="dj_restart_1", title="重启后仍可打开的结果页")
    with open(os.path.join(jobs_dir, "dj_restart_1.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)

    # 全新面板（内存里没有任何缓存），projects.json 指向那个任务
    w = _new_panel(("A",), jobs_dir=jobs_dir)
    w.projects_data["projects"]["A"]["entries"] = [
        {"kind": "result", "text": job["title"], "job_id": "dj_restart_1"},
    ]
    w._apply_project("A")
    rows = _result_rows(w)
    eq(len(rows), 1, "重启后应能从项目列表重新打开结果页")
    eq(rows[0].job()["title"], "重启后仍可打开的结果页")
    ok(rows[0].chart.has_curves(), "重启后曲线也要还在")
    contains(rows[0].design_ref.text(), "Amp24G")
    contains(rows[0].footer.text(), "输出目录", "输出目录要一起恢复")


def test_missing_job_file_shows_a_helpful_note():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs_missing"))
    w.projects_data["projects"]["A"]["entries"] = [
        {"kind": "result", "text": "丢失的结果页", "job_id": "dj_gone"},
    ]
    w._apply_project("A")
    eq(_result_rows(w), [], "任务文件不在时不该造一个空结果页")
    texts = [r.label.text() for r in _rows(w) if isinstance(r, panel.BubbleRow)]
    ok(any("已丢失" in t for t in texts), f"应提示数据已丢失，实际: {texts}")


def test_persisted_entries_reload_after_restart():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs7"))
    w._add_entry("result", "标题", "A", payload=_job())
    w._save_projects()

    with open(w._PROJECTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    entries = data["projects"]["A"]["entries"]
    eq(len(entries), 1)
    eq(entries[0]["kind"], "result")
    eq(entries[0]["job_id"], "dj_test_1")


# ---------------------------------------------------------------------------
# 重新仿真 / 重新评估回来时只更新所属项目
# ---------------------------------------------------------------------------

def test_job_update_rewrites_only_the_owning_project():
    w = _new_panel(("A", "B"), active="A", jobs_dir=os.path.join(_TMP, "jobs8"))
    w._add_entry("result", "旧标题", "A", payload=_job())
    w._apply_project("B")

    updated = _job(title="新标题", passed=False)
    w._on_job_updated({"ok": True, "job": updated}, "dj_test_1")

    eq(_entries(w, "A")[0]["text"], "新标题", "所属项目的条目文本要更新")
    eq(_entries(w, "B"), [], "别的项目不该被改动")
    eq(_result_rows(w), [], "前台不是 A，不该重绘")
    ok(w._job_for("dj_test_1")["verdict"] == "fail", "内存缓存应换成新任务")
    contains(w.status.text(), "结果页已更新")

    w._apply_project("A")
    rows = _result_rows(w)
    eq(len(rows), 1)
    eq(rows[0].job()["title"], "新标题", "切回后应看到更新后的结果页")
    contains(rows[0].verdict.text(), "未达标")


def test_job_update_error_is_reported_without_touching_entries():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs9"))
    w._add_entry("result", "标题", "A", payload=_job())
    before = json.dumps(_entries(w, "A"), ensure_ascii=False)
    w._on_job_updated({"error": "找不到设计任务 dj_test_1"}, "dj_test_1")
    eq(json.dumps(_entries(w, "A"), ensure_ascii=False), before)
    contains(w.status.text(), "找不到设计任务")


def test_resimulate_action_posts_the_job_id():
    """结果页的"重新仿真"必须把 job_id 交给后端，而不是本地硬算。"""
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs10"))
    sent = []
    w._spawn_cfg_worker = lambda payload, path, cb, timeout=60: sent.append(
        (payload, path, timeout))
    job = _job()
    w._resimulate_job(job)
    eq(sent[0][1], "/design/resimulate")
    eq(sent[0][0], {"job_id": "dj_test_1"})
    ok(sent[0][2] > 60, "重新仿真要等一整个仿真，超时必须放宽")
    contains(w.status.text(), "正在重新仿真")

    w._refresh_job(job)
    eq(sent[1][1], "/design/reload", "重新评估走另一条接口（不重新仿真）")


def test_open_schematic_action_posts_the_design_ref():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs11"))
    sent = []
    w._spawn_cfg_worker = lambda payload, path, cb, timeout=60: sent.append(
        (payload, path))
    w._open_schematic(_job())
    eq(sent[0][1], "/design/open_schematic")
    # 2026-10-02 起带上 workspace（后端据此核对，防止跨工作区开错同名设计）
    eq(sent[0][0], {"library": "AI_lib", "cell": "Amp24G", "view": "schematic",
                    "workspace": ""})


def test_open_schematic_without_a_design_ref_is_refused():
    w = _new_panel(("A",), jobs_dir=os.path.join(_TMP, "jobs12"))
    sent = []
    w._spawn_cfg_worker = lambda payload, path, cb, timeout=60: sent.append(path)
    w._open_schematic({"job_id": "x", "design": {}})
    eq(sent, [], "没有设计引用时不该发请求")
    contains(w.status.text(), "没有记录完整的设计引用")


if __name__ == "__main__":
    try:
        code = run(globals(), "设计结果页：项目隔离与重启恢复")
    finally:
        _teardown()
    sys.exit(code)
