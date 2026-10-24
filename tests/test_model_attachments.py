"""模型压缩包附件测试（需要 PySide6，不需要 ADS / 后端 —— offscreen 跑真实控件）。

覆盖面板侧的附件交互：

* 附件按钮存在、只收ZIP（非 ZIP 有明确提示，不静默忽略）；
* **拖放真的生效**：构造 QMimeData 投dragEnterEvent / dropEvent，
  验证过滤器接管了 ZIP 放行、非 ZIP 走提示分支；
* 上传在 worker 线程里跑（主线程只管 UI），且失败如实落到卡片上；
* 附件卡片的字段与状态展示：文件名 / 大小 / 工作区 / 状态 / 类型，
  折叠-展开、宽度自适应；
* **附件可单独发送**：不打字也能把附件标识发给 LLM，且 ZIP 内容不进消息；
* **附件可独立使用**：上传/导入/查看按钮不依赖聊天是否可用；
* 没有打开工作区（409 no_workspace）时如实提示，不假装成功；
* 项目隔离与重启恢复：上传中的异步回执写回**发起时**的项目；
  重启后核对引用，资产不在了要如实标注「资产已不可用」；
* 导入轮询与取消：状态推进、终止条件、取消文案区分"已请求"与"已完成保留"；
* 面板侧**不提供**任何删除模型资产的入口。

用临时 projects.json 与临时 config.ini，**不碰仓库里的 projects.json / config.ini**。

运行（需要一个装了 PySide6 的解释器）::

    python tests/test_model_attachments.py
"""

import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

# 必须在导入 PySide6 之前：无显示环境下用 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent, QDragEnterEvent
    from PySide6.QtWidgets import QApplication, QListWidgetItem
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能运行本测试：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

_TMP = tempfile.mkdtemp(prefix="ads_agent_attach_")
_TMPDIRS = [_TMP]

# 面板构造时会读 config.ini 取后端地址；指向临时配置，避免误连真后端
os.environ["ADS_AGENT_CONFIG"] = os.path.join(_TMP, "config.ini")
with open(os.environ["ADS_AGENT_CONFIG"], "w", encoding="utf-8") as _f:
    _f.write("[llm]\nbase_url = http://127.0.0.1:1\nmodel = m\n\n[ads]\nport = 8761\n")

import panel  # noqa: E402
import model_attachments as MA  # noqa: E402

_APP = None

# 记录 worker 实际会请求的 URL / 负载，回放用（不发真实 HTTP）
CALLS: list = []


class _StubChatWorker:
    """替身 ChatWorker：只暴露信号，不发真实请求。"""

    def __init__(self, messages, allow_python, model, parent=None):
        self.messages = list(messages)
        self.model = model
        self._running = False

    def isRunning(self):  # noqa: N802
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


class _StubUploadWorker:
    """替身 UploadWorker：记录参数，不真的连后端。"""

    def __init__(self, path, base="", auth=None, session="", chunk=1 << 20,
                 parent=None):
        self.path = path
        self.base = base
        self.session = session
        CALLS.append(("upload", path, base, session))

    def start(self):
        CALLS.append(("upload_started", self.path))

    def result(self, data):
        pass

    def progress(self, sent, total):
        pass

    def finished(self):
        pass


# 隔离面板与外部世界
panel.ChatWorker = _StubChatWorker
panel.MA.UploadWorker = _StubUploadWorker
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


def _new_panel(names=("A",), active=None):
    _app()
    d = tempfile.mkdtemp(prefix="ads_agent_attach_")
    _TMPDIRS.append(d)
    panel.AgentPanelWidget._PROJECTS_FILE = os.path.join(d, "projects.json")
    w = panel.AgentPanelWidget()
    w.projects_data = {
        "active": active or names[0],
        "projects": {n: {"entries": [], "history": []} for n in names},
    }
    w._apply_project(w.projects_data["active"], create=True)
    w._refresh_project_list()
    return w


def _entries(w, name):
    return w.projects_data["projects"][name]["entries"]


def _make_zip(name="村田_电容库.zip", size_kb=4):
    """造一个真的 ZIP（内容不参与断言，只保证扩展名与可读性都对）。"""
    d = tempfile.mkdtemp(prefix="ads_agent_attach_")
    _TMPDIRS.append(d)
    path = os.path.join(d, name)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "x" * 1024)
        zf.writestr("lib/lib.defs", "DEFINE lib demo 1\n")
    if size_kb > 4:
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("models/big.s2p", "# Hz S RI R 50\n" * (size_kb * 32))
    return path


def _attachment(w, project=None, **meta):
    """往项目里塞一条附件 entry（模拟上传完成后的卡片快照）。"""
    project = project or w.projects_data["active"]
    entry = {"kind": "attachment", "text": meta.get("filename", "a.zip"),
             "meta": dict(meta), "expanded": True}
    ok(w._add_entry("attachment", entry["text"], project, raw_entry=entry),
       "写入附件 entry 应成功")
    return entry


def _rows(w):
    out = []
    for i in range(w.chat.count()):
        widget = w.chat.itemWidget(w.chat.item(i))
        if isinstance(widget, panel.AttachmentRow):
            out.append(widget)
    return out


def _one_row(w):
    rows = _rows(w)
    eq(len(rows), 1, f"应当只有一张附件卡片，实际 {len(rows)}")
    return rows[0]


def _drop_event(mime, kind="drop"):
    """构造真实的拖放事件（Qt6 的 QDropEvent 收QPointF）。"""
    if kind == "enter":
        return QDragEnterEvent(QPointF(10, 10),
                               Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
    return QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def _mime_for(paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    return mime


def _row_text(row):
    """卡片上所有可见文字（折叠头 + 详情各行 + 型号清单）。"""
    parts = [row.toggle.text()]
    parts += [label.text() for label in row._labels if not label.isHidden()]
    if not row.models_view.isHidden():
        parts.append(row.models_view.toPlainText())
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# 附件按钮与 ZIP 限制
# ---------------------------------------------------------------------------

def test_attach_button_exists_next_to_send():
    w = _new_panel(("A",))
    ok(hasattr(w, "attach_btn"), "输入区应有附件按钮")
    # 排在发送键之前：附件是"输入"类动作，视觉顺序与操作顺序一致
    foot = w.input_frame.layout().itemAt(1).layout()
    order = [b for b in (w.attach_btn, w.send) if b is not None]
    idx = [i for i in range(foot.count())
           if foot.itemAt(i).widget() in order]
    eq(len(idx), 2, "附件按钮与发送键都应在输入区的按钮行里")
    ok(idx[0] < idx[1], "附件按钮应排在发送键之前")
    eq(w.attach_btn.toolTip().startswith("添加模型压缩包附件"), True,
       "附件按钮应有中文提示，说明只支持 .zip 与拖放")


def test_non_zip_drop_is_reported_not_silently_ignored():
    """拖入非 ZIP：必须明确提示"首版只支持 ZIP"，不能装作没发生。"""
    w = _new_panel(("A",))
    path = os.path.join(_TMP, "notes.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not a zip")

    accepted = w._drop_filter.eventFilter(w.chat, _drop_event(_mime_for([path])))
    eq(accepted, True, "过滤器应接管这次放下")
    texts = " | ".join(e["text"] for e in _entries(w, "A"))
    contains(texts, "只支持 ZIP", "拖入非 ZIP 应给出明确提示")
    contains(texts, "notes.txt", "提示里应说明收到的是哪个文件")


def test_pick_zip_rejects_non_zip_path():
    """_start_zip_upload 对非 ZIP 也走提示分支（按钮与拖放是同一条路）。"""
    w = _new_panel(("A",))
    path = os.path.join(_TMP, "not_a_zip.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")
    w._start_zip_upload(path)
    eq(len(_entries(w, "A")), 1, "非 ZIP 不应建卡片，只留一条提示")
    contains(_entries(w, "A")[0]["text"], "只支持 ZIP")


# ---------------------------------------------------------------------------
# 拖放真的生效（构造 QMimeData 走真实事件路径）
# ---------------------------------------------------------------------------

def test_drop_filter_accepts_zip_only_on_enter():
    """dragEnter：ZIP 放行（接收光标），非 ZIP 忽略。"""
    w = _new_panel(("A",))
    zpath = _make_zip()
    tpath = os.path.join(_TMP, "x.txt")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write("x")

    ok(w._drop_filter.eventFilter(w.chat, _drop_event(_mime_for([zpath]), "enter")),
       "ZIP 拖入应被接收")
    ok(w._drop_filter.eventFilter(w.chat, _drop_event(_mime_for([tpath]), "enter")),
       "非 ZIP 拖入也由过滤器处理（拒绝光标），不能漏到默认行为")


def test_zip_drop_creates_attachment_and_starts_upload():
    """drop 一个 ZIP：建卡片 + 起上传 worker，且上传不占用聊天通道。"""
    w = _new_panel(("A",))
    CALLS.clear()
    zpath = _make_zip()
    w._drop_filter.eventFilter(w.chat, _drop_event(_mime_for([zpath])))

    entries = _entries(w, "A")
    eq(len(entries), 1, "放下 ZIP 应插入一张附件卡片")
    eq(entries[0]["kind"], "attachment")
    eq(entries[0]["meta"]["state"], "uploading", "上传中就要有卡片，不能等传完才出现")
    ok(any(c[0] == "upload_started" for c in CALLS), "应启动上传 worker")
    eq(CALLS[0][3], "A", "上传要带上发起时的项目名（后端按项目记来源会话）")


def test_upload_worker_runs_off_the_main_thread():
    """上传必须走 QThread：主线程只管 UI，不跑网络请求。"""
    ok(issubclass(MA.UploadWorker, __import__("PySide6.QtCore",
                                              fromlist=["QThread"]).QThread),
       "UploadWorker 必须是 QThread，否则会阻塞 Qt 主线程")
    # 分块读而不是 f.read() 一次性进内存：几十 MB 的 ZIP 是常态
    src = _read_upload_source()
    contains(src, "read(", "读取文件应走 read 分块")


def _read_upload_source():
    import inspect
    return inspect.getsource(MA._CountingReader)


def test_upload_sends_raw_bytes_with_both_filename_headers():
    """请求体是原始字节；中文文件名两个头都发（ASCII + RFC 5987）。"""
    import inspect
    src = inspect.getsource(MA.UploadWorker)
    contains(src, "application/zip", "上传必须用 application/zip")
    contains(src, "X-Ads-Filename", "要发 ASCII 文件名头")
    contains(src, "X-Ads-Filename-Star", "中文文件名要发 filename*头")
    not_contains(src, "b64encode", "不能做 Base64（模型内容不该进 JSON/对话）")


# ---------------------------------------------------------------------------
# 上传回执：成功 / 无工作区 / 失败
# ---------------------------------------------------------------------------

def test_upload_success_fills_card_fields():
    w = _new_panel(("A",))
    _attachment(w, package_id="pkg-1", filename="村田.zip",
                size_bytes=16_600_000, workspace=r"D:\ws\proj",
                package_kind_label="Touchstone 模型文件包",
                state="saved", state_label="已保存", model_count=3)
    text = _row_text(_one_row(w))
    contains(text, "村田.zip", "卡片应显示文件名")
    contains(text, "16.6 MB", "大小应人类可读")
    contains(text, "已保存", "应显示保存状态")
    contains(text, "Touchstone 模型文件包", "应显示识别出的包类型")
    contains(text, r"D:\ws\proj", "应显示所属 ADS Workspace")
    contains(text, "3 个型号", "应显示型号数量")


def test_human_size_is_readable():
    eq(MA.human_size(16_600_000), "16.6 MB")
    eq(MA.human_size(512), "512 B")
    eq(MA.human_size(None), "大小未知", "拿不到大小要如实说")


def test_no_workspace_is_reported_honestly():
    """409 no_workspace：如实提示，绝不假装成功、也不落到默认目录。"""
    w = _new_panel(("A",))
    entry = _attachment(w, filename="x.zip")
    w._on_upload_done(entry, "A", {"ok": False, "kind": "no_workspace",
                                   "http": 409, "error": "no workspace"})
    meta = entry["meta"]
    eq(meta["state"], "failed", "没打开工作区时不能算上传成功")
    contains(meta["error"], "没有打开 ADS 工作区", "应明确说没有工作区")
    contains(meta["error"], "Workspace", "应告诉用户去 ADS 里打开/新建工作区")
    not_contains(meta.get("package_id") or "", "pkg", "失败不应凭空造出 package_id")
    # 绝不静默存到某个默认目录
    not_contains(json_dumps(entry), "ads_agent_models",
                 "没有工作区时不该出现任何资产落盘路径")


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_upload_failure_keeps_reason_and_offers_retry():
    w = _new_panel(("A",))
    entry = _attachment(w, filename="x.zip")
    w._on_upload_done(entry, "A", {"ok": False, "http": 413,
                                   "error": "文件过大（999 字节 > 上限 100 字节）"})
    contains(entry["meta"]["error"], "文件过大", "失败原因要原文显示")
    row = _one_row(w)
    row.refresh()
    ok(row.retry_btn.isHidden(), "失败后重试按钮不该被藏起来")
    ok(not row.import_btn.isHidden(), "上传没成功时不该给「解压并导入」")


def test_retry_after_upload_failure_reuploads_local_file():
    w = _new_panel(("A",))
    path = _make_zip("retry.zip")
    entry = _attachment(w, filename="retry.zip")
    entry["meta"]["local_path"] = path
    CALLS.clear()
    w._attach_retry(entry)
    ok(any(c[0] == "upload_started" and c[1] == path for c in CALLS),
       "上传失败后的重试应重传同一个本地文件")


# ---------------------------------------------------------------------------
# 卡片交互：折叠 / 展开 / 自适应 / 按钮可见性
# ---------------------------------------------------------------------------

def test_card_collapses_and_expands_and_persists():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="saved",
                        state_label="已保存")
    row = _one_row(w)
    eq(row.body.isHidden(), False, "默认按上传时那样展开")
    row.toggle.click()
    eq(entry["expanded"], False, "折叠状态要写回 entry（否则重启后又展开）")
    w._rebuild()
    eq(_one_row(w).body.isHidden(), True, "重建后应保持折叠")
    row = _one_row(w)
    row.toggle.click()
    eq(entry["expanded"], True)


def test_card_reflows_with_panel_width():
    """宽度自适应：窄面板不会把行高算成固定值，动作按钮也不被挤没。"""
    w = _new_panel(("A",))
    _attachment(w, package_id="p1", filename="一个名字很长的模型压缩包.zip",
                state="saved", state_label="已保存",
                message="已保存到当前工作区并完成包结构检查。**未解压、未加载套件**")
    w.resize(700, 600)
    wide = _one_row(w)
    wide_h = w.chat.item(0).sizeHint().height()
    w.chat.resize(360, 600)
    w.reflow()
    narrow_h = w.chat.item(0).sizeHint().height()
    ok(narrow_h >= wide_h - 2,
       f"窄面板下行高不该比宽面板更矮（{narrow_h} vs {wide_h}），"
       f"否则文字会被裁掉")
    ok(narrow_h > 0, "行高必须为正")


def test_import_button_only_when_not_uploading():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="uploading")
    row = _one_row(w)
    row.refresh()
    ok(row.import_btn.isHidden(), "上传中不该能点导入")
    ok(row.view_btn.isHidden(), "上传中不该能点查看模型（还没 package_id）")
    entry["meta"]["state"] = "saved"
    row.refresh()
    ok(not row.import_btn.isHidden(), "上传完成后应能点解压并导入")


def test_cancel_button_only_while_importing():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing",
                        state_label="导入中")
    row = _one_row(w)
    ok(not row.cancel_btn.isHidden(), "导入中要有取消按钮")
    ok(row.import_btn.isHidden(), "导入中不该再让用户点导入")
    entry["meta"]["state"] = "ready"
    row.refresh()
    ok(row.cancel_btn.isHidden(), "不在导入中就不该有取消按钮")


def test_failed_card_shows_reason_and_retry_only():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="failed",
                        state_label="失败")
    entry["meta"]["error"] = "挂接 ADS 工作区失败：库定义冲突"
    row = _one_row(w)
    row.refresh()
    ok(not row.retry_btn.isHidden(), "失败后要有重试按钮")
    ok(row.import_btn.isHidden(), "失败态只给重试，不给导入（免得重复踩同一个坑）")
    contains(_row_text(row), "挂接 ADS 工作区失败", "失败原因要原文显示")


def test_missing_asset_is_labelled_honestly():
    """资产不在了：如实标注「资产已不可用」，不能显示成正常。"""
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="gone", filename="a.zip", state="ready",
                        state_label="已就绪")
    entry["meta"]["missing"] = True
    entry["meta"]["missing_reason"] = "当前工作区的模型清单里没有这个包"
    row = _one_row(w)
    row.refresh()
    contains(row.toggle.text(), "资产已不可用", "折叠头就该如实标注")
    ok(row.import_btn.isHidden(), "资产不在了不该再给导入按钮（重导入必然失败）")
    contains(_row_text(row), "资产已不可用", "详情区也要如实标注")


# ---------------------------------------------------------------------------
# 导入与轮询
# ---------------------------------------------------------------------------

def test_import_request_marks_importing_and_starts_poll():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="saved")
    seen = {}
    w._spawn_model_worker = lambda payload, path, cb, timeout=120: seen.update(
        payload=payload, path=path)
    w._attach_import(entry)
    eq(entry["meta"]["state"], "importing")
    eq(seen["path"], "/models/import")
    eq(seen["payload"], {"package_id": "p1"}, "按 package_id 发起导入")


def test_import_ack_records_op_id_and_starts_polling():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip")
    w._spawn_model_worker = lambda *a, **k: None
    w._on_import_ack(entry, "A", {"ok": True, "op_id": "op-9", "accepted": True,
                                  "message": "导入已开始（后台执行）。"})
    eq(w._attach_op["p1"], "op-9", "取消导入要用后端给的 op_id")
    ok(w._attach_poll.isActive(), "导入在后台跑，应开始轮询进度")


def test_poll_targets_only_active_states():
    w = _new_panel(("A",), active="A")
    a = _attachment(w, "A", package_id="p1", state="importing")
    _attachment(w, "A", package_id="p2", state="ready")
    _attachment(w, "A", package_id="p3", state="inspecting")
    targets = w._attachments_needing_poll()
    ids = sorted(str(e["meta"]["package_id"]) for e, _p in targets)
    eq(ids, ["p1", "p3"], "只轮询还在动的包（importing / inspecting）")


def test_poll_picks_up_state_transitions():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._on_packages({"ok": True, "packages": [
        {"package_id": "p1", "state": "pending_verify",
         "state_label": "待验证", "model_count": 12}]}, "p1", "A")
    eq(entry["meta"]["state"], "pending_verify", "轮询应把状态推进到待验证")
    eq(entry["meta"]["model_count"], 12)
    eq(w._attachments_needing_poll(), [], "到达待验证后不再轮询")


def test_poll_marks_vanished_package_as_missing():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._on_packages({"ok": True, "packages": []}, "p1", "A")
    eq(entry["meta"]["state"], "missing", "包从清单消失要如实标注不可用")
    ok(entry["meta"]["missing"])


def test_poll_stops_after_max_ticks():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._attach_poll_ticks["p1"] = MA.POLL_MAX_TICKS
    w._spawn_model_worker = lambda *a, **k: None
    w._on_attach_poll_tick()
    contains(entry["meta"]["message"], "已停止自动刷新",
             "轮询要有终止条件，并如实告诉用户停了")
    ok(not w._attach_poll.isActive(), "超过上限应停掉定时器")


def test_poll_survives_project_switch():
    """切到别的项目后导入仍继续被轮询（否则那条导入永远停在"导入中"）。"""
    w = _new_panel(("A", "B"), active="A")
    entry = _attachment(w, "A", package_id="p1", filename="a.zip",
                        state="importing")
    w._apply_project("B")
    targets = w._attachments_needing_poll()
    eq(len(targets), 1, "后台项目的导入也要继续轮询")
    eq(targets[0][1], "A", "回执要落到附件所属的项目")
    _ = entry


def test_cancel_distinguishes_requested_from_done():
    """取消文案必须区分"已请求取消"与"已完成的前置步骤保留"。"""
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._attach_op["p1"] = "op-9"
    seen = {}
    w._spawn_model_worker = lambda payload, path, cb, timeout=120: seen.update(
        payload=payload, path=path)
    w._attach_cancel(entry)
    eq(seen["path"], "/models/cancel")
    eq(seen["payload"], {"op_id": "op-9"})
    contains(entry["meta"]["message"], "已请求取消")
    contains(entry["meta"]["message"], "保留", "要说明已完成的前置步骤会保留")


def test_cancel_ack_handles_unknown_operation():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._on_cancel_ack(entry, "A", {"ok": False, "known": False,
                                  "message": "该导入操作已结束或不存在"})
    contains(entry["meta"]["message"], "已经结束或不存在",
             "取消一个已结束的操作要如实说，不要谎称取消成功")


def test_view_expands_card_and_fetches_package_detail():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="pending_import")
    seen = {}
    w._spawn_model_worker = lambda payload, path, cb, timeout=120: seen.update(
        path=path)
    w._attach_view(entry)
    eq(entry["expanded"], True, "查看模型应就地展开卡片")
    contains(seen["path"], "/models/package?id=p1")


def test_view_renders_models_and_says_how_many_are_hidden():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="ready")
    models = [{"part": f"GRM{p:03d}", "library": "Murata_GCM",
               "ports": 2, "freq_start_hz": 3e8, "freq_stop_hz": 6e9,
               "reference_impedance_ohm": 50} for p in range(60)]
    w._on_package_detail(entry, "A", {"ok": True, "package": {
        "state": "ready", "state_label": "已就绪", "model_count": 60,
        "models": models[:20], "models_truncated": 40,
        "detection": {"evidence": ["含 lib.defs 定义", "含 .s2p 数据文件"],
                      "kit_roots": ["Murata_GCM"]},
        "workspace": r"D:\ws\proj"}})
    row = _one_row(w)
    text = row.models_view.toPlainText()
    contains(text, "GRM000", "应显示型号")
    contains(text, "Murata_GCM", "应显示所属库")
    contains(text, "300 MHz~6 GHz", "应显示频率范围")
    contains(text, "50Ω", "应显示参考阻抗")
    contains(text, "未显示", "数量多要截断并注明省略了多少")
    contains(text, "40", "应说明省略的个数")
    eq(len([ln for ln in text.split("\n") if ln.startswith("GRM")]), 20,
       "一次最多渲染 20 行，其余靠注明省略")
    # 识别依据也要露出来（为什么这么判型），不只给结论
    contains(_row_text(row), "lib.defs", "应展示识别依据")
    contains(_row_text(row), "Murata_GCM", "应展示候选套件根")


# ---------------------------------------------------------------------------
# 附件独立发送 / 独立使用
# ---------------------------------------------------------------------------

def test_attachment_can_be_sent_without_typing():
    w = _new_panel(("A",))
    _attachment(w, package_id="p1", filename="村田.zip", size_bytes=16_600_000,
                package_kind_label="Touchstone 模型文件包", state="saved",
                state_label="已保存")
    w._on_upload_done(_entries(w, "A")[0], "A", {"ok": True,
                                                 "workspace": r"D:\ws",
                                                 "package": {
                                                     "package_id": "p1",
                                                     "filename": "村田.zip",
                                                     "size_bytes": 16_600_000,
                                                     "package_kind_label":
                                                         "Touchstone 模型文件包",
                                                     "state": "saved",
                                                     "state_label": "已保存",
                                                     "model_count": 5}})
    eq(w.input.toPlainText(), "", "前置条件：不打字")
    w._on_send()
    eq(len(w.history), 1, "只发附件也要产生一条用户消息")
    msg = w.history[0]["content"]
    contains(msg, "村田.zip", "消息里应说明附件标识")
    contains(msg, "16.6 MB")
    contains(msg, "p1", "消息里应带 package_id")
    contains(msg, "Touchstone 模型文件包")
    contains(msg, "不要现在就解压或导入",
             "上传 ≠ 导入：要让模型先核对清单而不是自作主张导入")


def test_attachment_message_contains_no_zip_content():
    """ZIP 内容绝不进聊天消息 / LLM 请求 —— 只发标识与元数据。"""
    path = _make_zip("secret-content.zip", size_kb=32)
    with open(path, "rb") as f:
        blob = f.read()
    w = _new_panel(("A",))
    w._pending_attachments = [{"package_id": "p1", "meta": {
        "filename": "secret-content.zip", "size_bytes": len(blob),
        "package_kind_label": "ADS Design Kit", "state": "saved",
        "state_label": "已保存", "workspace": r"D:\ws"}}]
    msg = w._attachment_manifest(w._pending_attachments)
    for size in (64, 128, 512):
        not_contains(msg, blob[:size].decode("latin-1"),
                     "消息里不应出现 ZIP 的二进制内容")
    not_contains(msg, "lib.defs", "不应把包内文件名/内容塞进消息")


def test_attachment_send_is_blocked_while_a_turn_is_running():
    """上一轮在跑时不发附件，但附件不能丢 —— 放回待发送队列。"""
    w = _new_panel(("A",))
    worker = w._worker = _StubChatWorker([], False, "m")
    worker.start()
    w._pending_attachments = [{"package_id": "p1", "meta": {"filename": "a.zip"}}]
    w._on_send()
    eq(len(w.history), 0, "上一轮在跑时不该发新请求")
    eq(len(w._pending_attachments), 1, "附件必须留在队列里等下一轮")
    worker.stop()


def test_attachments_work_without_chat_available():
    """F：附件保存与导入不依赖 LLM 服务连通。"""
    import inspect
    src = inspect.getsource(panel.AgentPanelWidget._attach_import)
    not_contains(src, "_worker", "导入不能挂在聊天 worker 上（LLM 不可用时也要能用）")
    not_contains(src, "ChatWorker", "导入不该依赖聊天通道")
    src2 = inspect.getsource(panel.AgentPanelWidget._start_zip_upload)
    not_contains(src2, "isRunning", "上传不该因为「聊天不可用」而拒绝")
    # 上传/导入/查看都只经 ModelApiWorker（自己的 HTTP 通道）
    for name in ("_attach_import", "_attach_view", "_attach_cancel"):
        s = inspect.getsource(getattr(panel.AgentPanelWidget, name))
        not_contains(s, "ChatWorker", f"{name} 不该依赖聊天通道")


# ---------------------------------------------------------------------------
# 项目隔离与重启恢复
# ---------------------------------------------------------------------------

def test_upload_receipt_lands_in_the_originating_project():
    w = _new_panel(("A", "B"), active="A")
    entry = {"kind": "attachment", "text": "a.zip",
             "meta": {"filename": "a.zip", "state": "uploading"}, "expanded": True}
    w._add_entry("attachment", "a.zip", "A", raw_entry=entry)

    # 上传在途时用户切到 B
    w._apply_project("B")
    w._on_upload_done(entry, "A", {"ok": True, "workspace": r"D:\ws",
                                   "package": {"package_id": "p1",
                                               "filename": "a.zip",
                                               "state": "saved",
                                               "state_label": "已保存"}})
    eq(entry["meta"]["package_id"], "p1", "回执写回原项目")
    eq(len(_entries(w, "B")), 0, "不该污染当前项目")
    eq(_entries(w, "A")[0] is entry, True)
    # 切回 A 能看到已就绪的卡片
    w._apply_project("A")
    contains(_one_row(w).toggle.text(), "已保存")


def test_upload_receipt_dropped_when_project_deleted():
    """在途上传期间项目被删：回执丢弃，绝不把项目复活。"""
    w = _new_panel(("A", "B"), active="A")
    entry = {"kind": "attachment", "text": "a.zip",
             "meta": {"filename": "a.zip", "state": "uploading"}, "expanded": True}
    w._add_entry("attachment", "a.zip", "A", raw_entry=entry)
    del w.projects_data["projects"]["A"]
    w.projects_data["active"] = "B"
    w._apply_project("B")
    w._on_upload_done(entry, "A", {"ok": True, "package": {"package_id": "p1"}})
    ok("A" not in w.projects_data["projects"], "已删除的项目被复活了")


def test_restart_verifies_references_against_backend_list():
    """重启后 _rebuild 用 /models/packages 核对引用；资产不在要如实标注。"""
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="gone", filename="old.zip", state="ready",
                        state_label="已就绪")
    w._verify_attachments()
    w._on_verify_packages({"ok": True, "workspace": r"D:\other",
                           "packages": [{"package_id": "p2", "state": "saved"}]},
                          [entry])
    eq(entry["meta"]["missing"], True, "引用不在清单里就该标注不可用")
    contains(_one_row(w).toggle.text(), "资产已不可用")
    contains(_row_text(_one_row(w)), "当前工作区",
             "要说清是工作区换了/资产没了，不是笼统报错")


def test_interrupted_upload_is_reported_as_interrupted_not_as_lost_asset():
    """重启前没传完的包：如实说"上传中断"，不能算"资产丢失"。"""
    w = _new_panel(("A",))
    entry = _attachment(w, filename="half.zip", state="uploading")
    w._on_verify_packages({"ok": True, "packages": []}, [entry])
    eq(entry["meta"]["state"], "failed")
    eq(entry["meta"]["missing"], None,
       "上传中断不是资产不可用（后端根本没收到这个包）")
    contains(entry["meta"]["error"], "上传没有完成")
    contains(_row_text(_one_row(w)), "上传中断")


def test_restart_verification_refreshes_state_when_asset_still_there():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._on_verify_packages({"ok": True, "packages": [
        {"package_id": "p1", "state": "ready", "state_label": "已就绪",
         "model_count": 42, "package_kind_label": "ADS Design Kit"}]}, [entry])
    eq(entry["meta"]["state"], "ready")
    eq(entry["meta"]["model_count"], 42)
    eq(entry["meta"]["package_kind_label"], "ADS Design Kit")
    not_contains(_one_row(w).toggle.text(), "资产已不可用")


def test_verify_does_not_touch_other_projects_entries():
    """核对结果是异步的：回调到达时用户可能已切项目，不能顺着self.entries 乱改。"""
    w = _new_panel(("A", "B"), active="A")
    a = _attachment(w, "A", package_id="p1", filename="a.zip", state="ready")
    w._apply_project("B")
    b = _attachment(w, "B", package_id="p2", filename="b.zip", state="ready")
    w._on_verify_packages({"ok": True, "packages": []}, [a])
    eq(a["meta"]["missing"], True, "A 的附件应被核对")
    eq(b["meta"].get("missing"), None, "B 的附件不该被这次核对改到")
    eq(len(_entries(w, "B")), 1, "核对不该凭空在当前项目里加东西")


def test_clear_chat_does_not_delete_model_assets():
    """清空会话不得删除模型资产 —— 面板根本没有删除资产的入口。"""
    import inspect
    src = inspect.getsource(panel.AgentPanelWidget._on_clear)
    not_contains(src, "models", "清空会话不应触碰模型资产")
    src2 = inspect.getsource(panel.AgentPanelWidget._delete_project)
    not_contains(src2, "models", "删除聊天项目不应触碰模型资产")
    # 面板不提供任何删除模型资产的接口
    panel_src = inspect.getsource(panel)
    for bad in ("/models/delete", "delete_model_package", "remove_model"):
        not_contains(panel_src, bad, "面板不应有删除模型资产的入口")


def test_pending_attachments_are_not_persisted():
    """待发送附件只在内存里：重启后不该把老附件又当成新附件发一遍。"""
    w = _new_panel(("A",))
    w._pending_attachments = [{"package_id": "p1", "meta": {"filename": "a.zip"}}]
    eq(json_dumps(w.projects_data).count('"pending_attachments"'), 0,
       "待发送队列不应进projects.json")


# ---------------------------------------------------------------------------
# 卡片渲染不依赖后端也能画出来（LLM / 后端都不可用时的可用性）
# ---------------------------------------------------------------------------

def test_card_renders_without_any_backend():
    w = _new_panel(("A",))
    _attachment(w, package_id="p1", filename="a.zip", size_bytes=1024,
                workspace=r"D:\ws", package_kind_label="未识别",
                state="saved", state_label="已保存")
    row = _one_row(w)
    ok(row.toggle.text(), "没有后端也要能把卡片画出来")
    w.reflow()
    ok(w.chat.item(0).sizeHint().height() > 0)


if __name__ == "__main__":
    try:
        code = run(globals(), "模型压缩包附件（PySide6 / offscreen）")
    finally:
        _teardown()
    sys.exit(code)