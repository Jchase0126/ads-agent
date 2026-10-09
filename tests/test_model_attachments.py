"""模型压缩包附件测试（需要 PySide6，不需要 ADS —— offscreen/windows 跑真实控件）。

覆盖面板侧的附件交互：

* 附件按钮存在、只收ZIP（非 ZIP 有明确提示，不静默忽略）；
* **拖放真的生效**：构造 QMimeData 投 dragEnterEvent / dropEvent，
  验证过滤器接管了 ZIP 放行、非 ZIP 走提示分支；
* 上传在 worker 线程里跑（主线程只管 UI），且失败如实落到卡片上；
* 附件卡片的字段与状态展示：文件名 / 大小 / 工作区 / 状态 / 类型，
  折叠-展开、宽度自适应；
* **附件可单独发送**：不打字也能把附件标识发给 LLM，且 ZIP 内容不进消息；
* **附件可独立使用**：上传/导入/查看走自己的 HTTP 通道 —— 用**实际发出的
  请求**证明（真起一个回环 HTTP 服务收请求），不做源码子串断言；
* 上传失败显示原因并可重试，重试**回写原聊天项目**且就地重传同一张卡片；
* 中断上传与资产丢失分别提示（不是一回事）；
* 同一个包被多个聊天引用时，状态刷新覆盖每一张卡片；轮询按包去重、
  在途请求不重复发；清单被返回上限截断时**不**把好包误判成丢失；
* 切换工作区显示归属不匹配，并且不误发起导入；
* 没有打开工作区（409 no_workspace）时如实提示，不假装成功；
* 项目隔离与重启恢复：上传中的异步回执写回**发起时**的项目；
  项目被删除后异步回执不污染其他项目；待发送附件按聊天项目隔离；
* 导入轮询与取消：状态推进、终止条件、取消文案区分"已请求"与"已完成保留"；
* 面板侧**不提供**任何删除模型资产的入口。

测试基础设施的几条硬规矩（踩过的坑都写在对应的注释里）：

* Qt 事件**不接管** QMimeData 的所有权 —— 不持有引用的话，
  ``event.mimeData()`` 拿回来的是一个已经没有 Python 包装的 QObject；
* Qt 6.11 的 ``QDragEnterEvent`` 只收 ``QPoint``（不是 QPointF）；
* 替身替换必须**限定作用域并可靠恢复**，且替身要带真实消费方依赖的 Qt 信号；
  检查真实 worker 的类行为时不能在替身生效的作用域里做；
* 大小单位全工程只有一处定义（SI 十进制：1 MB = 1000 KB），口径不再分裂；
* 布局断言按**当前输入栏结构**取布局，不写死索引。

用临时 projects.json 与临时 config.ini，**不碰仓库里的 projects.json / config.ini**。

运行（需要一个装了 PySide6 的解释器）::

    python tests/test_model_attachments.py
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

# 必须在导入 PySide6 之前：无显示环境下用 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import (QMimeData, QObject, QPoint, QPointF, Qt, QThread,
                                QUrl, Signal)
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
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

# Qt 的拖放事件只借用 QMimeData 指针、不接管所有权：Python 侧一回收，
# event.mimeData() 拿到的就退化成一个光秃秃的 QObject（连 hasUrls 都没有）。
# 所以每份 mime 都必须被这个人持有到测试结束。
_MIME_BAG: list = []


# ---------------------------------------------------------------------------
# 替身：带真实消费方依赖的 Qt 信号，且替换可限定作用域、可恢复
# ---------------------------------------------------------------------------

class _StubChatWorker(QObject):
    """替身 ChatWorker：只暴露信号，不发真实请求。

    面板会 ``connect`` 到 event_received / failed / finished —— 真实 ChatWorker
    是 QObject + Signal，替身也必须如此，否则一发消息就 AttributeError。
    """

    event_received = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, messages, allow_python, model, parent=None):
        super().__init__(parent)
        self.messages = list(messages)
        self.model = model
        self._running = False

    def isRunning(self):  # noqa: N802
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False


class _StubUploadWorker(QObject):
    """替身 UploadWorker：记录参数，不真的连后端（连信号一起替）。"""

    result = Signal(dict)
    progress = Signal(int, int)
    finished = Signal()

    def __init__(self, path, base="", auth=None, session="", chunk=1 << 20,
                 parent=None):
        super().__init__(parent)
        self.path = path
        self.base = base
        self.session = session
        CALLS.append(("upload", path, base, session))

    def start(self):
        CALLS.append(("upload_started", self.path))


_REAL: dict = {}


def _install_stubs():
    """把面板与外部世界隔开：所有替身都记住原件，_teardown 时逐个还原。"""
    _REAL["ChatWorker"] = panel.ChatWorker
    _REAL["UploadWorker"] = MA.UploadWorker
    _REAL["reload_config"] = panel.AgentPanelWidget.reload_config
    _REAL["auto_revive"] = panel.AgentPanelWidget._auto_revive
    _REAL["backend_base"] = panel._backend_base
    panel.ChatWorker = _StubChatWorker
    MA.UploadWorker = _StubUploadWorker
    panel.AgentPanelWidget.reload_config = lambda self: None
    panel.AgentPanelWidget._auto_revive = lambda self, retry=None: None
    panel._backend_base = lambda: "http://127.0.0.1:1"


def _restore_stubs():
    for key, value in _REAL.items():
        if key == "ChatWorker":
            panel.ChatWorker = value
        elif key == "UploadWorker":
            MA.UploadWorker = value
        elif key == "reload_config":
            panel.AgentPanelWidget.reload_config = value
        elif key == "auto_revive":
            panel.AgentPanelWidget._auto_revive = value
        elif key == "backend_base":
            panel._backend_base = value


@contextlib.contextmanager
def _real_upload_worker():
    """让**真实** UploadWorker 上场（检查它的类行为 / 发真实请求时用）。

    替身默认全局生效；要断言"真实 worker 是 QThread""真实请求是原始字节"
    就必须临时换回原件 —— 在替身作用域里检查真实类只会检查到替身自己。
    """
    saved = MA.UploadWorker
    MA.UploadWorker = _REAL.get("UploadWorker", MA.UploadWorker)
    try:
        yield
    finally:
        MA.UploadWorker = saved


@contextlib.contextmanager
def _backend_at(url: str):
    """把后端基址临时指向某个真实服务（发真实请求时用）。"""
    saved = panel._backend_base
    panel._backend_base = lambda: url
    try:
        yield
    finally:
        panel._backend_base = saved


def _teardown():
    # QApplication 必须活着完成 QWidget 的析构。面板的剪贴板回调和 Qt
    # 定时器会持有引用，留到解释器退出再释放在 Windows/PySide6 上会崩溃。
    if _APP is not None:
        from PySide6.QtCore import QCoreApplication, QEvent, QTimer
        _APP.processEvents()
        for widget in _APP.topLevelWidgets():
            if isinstance(widget, panel.AgentPanelWidget):
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                for worker in list(widget._attach_workers):
                    if isinstance(worker, QThread):
                        worker.wait(5000)
                widget.close()
                widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _restore_stubs()
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
    # 构造器的 singleShot 工作区恢复检查在空会话时完成，避免稍后处理事件
    # 时对其它测试已写入的附件发起意外 HTTP 请求。
    _APP.processEvents()
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
    """构造真实的拖放事件。

    Qt 6.11 的 ``QDragEnterEvent`` 只收 ``QPoint``（QPointF 会直接 TypeError）；
    ``QDropEvent`` 收 QPointF —— 两个构造签名并不一致，别照抄。
    """
    if kind == "enter":
        return QDragEnterEvent(QPoint(10, 10),
                               Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
    return QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def _mime_for(paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    _MIME_BAG.append(mime)      # 必须持有：事件不接管所有权
    return mime


def _row_text(row):
    """卡片上所有可见文字（折叠头 + 详情各行 + 型号清单）。"""
    parts = [row.toggle.text()]
    parts += [label.text() for label in row._labels if not label.isHidden()]
    if not row.models_view.isHidden():
        parts.append(row.models_view.toPlainText())
    return " | ".join(parts)


def _foot_layout(w):
    """找输入栏里装着按钮的那一排。

    输入栏现在是"状态行 + 输入框 + 按钮行"三层结构，写死 itemAt(1) 会取到
    输入框（没有 layout）。按"哪一层里有附件按钮"来找，结构再变也不会错位。
    """
    outer = w.input_frame.layout()
    for i in range(outer.count()):
        sub = outer.itemAt(i).layout()
        if sub is None:
            continue
        for j in range(sub.count()):
            if sub.itemAt(j).widget() is w.attach_btn:
                return sub
    return None


def _select_project(w, name):
    for i in range(w.project_list.count()):
        item = w.project_list.item(i)
        item.setSelected(item.data(Qt.ItemDataRole.UserRole) == name)


# ---------------------------------------------------------------------------
# 真起一个回环 HTTP 服务：用"实际收到的请求"证明行为
# ---------------------------------------------------------------------------

class _RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 别把请求日志喷进测试输出
        pass

    def _record(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        self.server.calls.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        return body

    def _reply(self):
        key = self.path.split("?")[0]
        response = self.server.responses.get(key, {"ok": True})
        status, body = response if isinstance(response, tuple) else (200, response)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 — http.server 的命名
        self._record()
        self._reply()

    def do_POST(self):  # noqa: N802
        self._record()
        self._reply()


class _LocalBackend:
    """记录面板**实际发出**的请求的本地后端。

    用它替代"看源码里有没有某个字符串"：附件是不是走自己的 HTTP 通道、
    上传是不是原始字节、没有 LLM 时还能不能用 —— 只有真收到请求才算数。
    """

    def __init__(self, responses: dict | None = None):
        self.calls: list = []
        self.responses = {"/models/packages": {"ok": True, "workspace": r"D:\ws",
                                               "packages": [], "total": 0}}
        self.responses.update(responses or {})
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self._srv.calls = self.calls
        self._srv.responses = self.responses
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def paths(self) -> list:
        return [c["path"] for c in self.calls]

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


def _wait_workers(w, timeout_ms=15000):
    """等附件 worker 跑完，并把 Qt 事件队列放空（信号才能送回主线程）。"""
    for worker in list(w._attach_workers):
        try:
            worker.wait(timeout_ms)
        except (AttributeError, RuntimeError):
            pass
    _app().processEvents()


# ---------------------------------------------------------------------------
# 附件按钮与 ZIP 限制
# ---------------------------------------------------------------------------

def test_attach_button_exists_next_to_send():
    w = _new_panel(("A",))
    ok(hasattr(w, "attach_btn"), "输入区应有附件按钮")
    # 排在发送键之前：附件是"输入"类动作，视觉顺序与操作顺序一致
    foot = _foot_layout(w)
    ok(foot is not None, "应能在输入栏里找到按钮行")
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


def test_pasting_zip_creates_removable_composer_card_without_path_text():
    """Ctrl+V 文件 URL 应上传 ZIP，并在输入栏显示可移除的附件缩略卡。"""
    w = _new_panel(("A",))
    CALLS.clear()
    zpath = _make_zip("Inductor_LQG15HH_02.zip")
    mime = _mime_for([zpath])

    w.input.insertFromMimeData(mime)

    eq(w.input.toPlainText(), "", "本地 ZIP 路径不能插进聊天正文")
    entries = _entries(w, "A")
    eq(len(entries), 1, "粘贴 ZIP 应创建会话附件卡片")
    eq(entries[0]["meta"]["state"], "uploading")
    ok(not w.composer_attachment_strip.isHidden(), "输入栏应显示 ZIP 预览卡片")
    eq(w.composer_attachment_layout.count(), 2,
       "应有一张文件卡片和末尾弹性占位")
    ok(any(c[0] == "upload_started" for c in CALLS), "粘贴 ZIP 应启动上传")

    w.input.setPlainText("请使用这个模型")
    w._on_send()
    eq(len(w.history), 0, "上传未完成时不能把消息单独发出")
    eq(w.input.toPlainText(), "请使用这个模型", "等待上传时应保留用户输入")
    contains(w.status.text(), "还在上传")
    w.input.clear()

    remove = next(btn for btn in w.composer_attachment_strip.findChildren(
        panel.QToolButton) if btn.accessibleName() == "移除待发送附件")
    remove.click()
    ok(w.composer_attachment_strip.isHidden(), "移除后输入栏卡片应隐藏")
    eq(w.input.toPlainText(), "", "移除附件不应把路径写回正文")


def test_pasting_plain_text_still_uses_normal_text_paste():
    w = _new_panel(("A",))
    mime = QMimeData()
    mime.setText("请检查这个电路")
    w.input.insertFromMimeData(mime)
    eq(w.input.toPlainText(), "请检查这个电路", "普通文本粘贴行为需保留")


def test_pasting_file_uri_text_uploads_zip_instead_of_pasting_path():
    """兼容剪贴板只提供 file:/// 文本的 Windows 文件复制结果。"""
    w = _new_panel(("A",))
    zpath = _make_zip("Inductor_LQG15HH_02.zip")
    mime = QMimeData()
    mime.setText(QUrl.fromLocalFile(zpath).toString())

    w.input.insertFromMimeData(mime)

    eq(w.input.toPlainText(), "", "file URL 不应作为正文文本插入")
    eq(len(_entries(w, "A")), 1, "file URL 应创建 ZIP 附件")
    ok(not w.composer_attachment_strip.isHidden(), "file URL 应显示附件卡片")


def test_upload_worker_runs_off_the_main_thread():
    """上传必须走 QThread：主线程只管 UI，不跑网络请求。

    注意用 ``_real_upload_worker()`` 把替身换回原件 —— 在替身作用域里断言
    "是 QThread" 只会检查到替身自己。
    """
    with _real_upload_worker():
        ok(issubclass(MA.UploadWorker, QThread),
           "UploadWorker 必须是 QThread，否则会阻塞 Qt 主线程")


def test_upload_worker_labels_connection_abort_as_interruption():
    """WinError 10053 应报告为上传连接中断，仍触发后端恢复。"""
    import urllib.error

    class _AbortingOpener:
        def open(self, request, **kwargs):
            if request.get_method() == "GET":
                import io
                return io.BytesIO(json.dumps({"workspace": r"D:\ws"}).encode())
            raise urllib.error.URLError(ConnectionAbortedError(10053, "aborted"))

    path = _make_zip("aborted.zip")
    previous = MA._opener
    MA._opener = lambda: _AbortingOpener()
    try:
        with _real_upload_worker():
            worker = MA.UploadWorker(path, base="http://127.0.0.1:1")
            results = []
            worker.result.connect(results.append)
            worker.run()
    finally:
        MA._opener = previous

    eq(len(results), 1)
    ok(results[0].get("backend_down"), "连接中断仍需检测/恢复后端")
    ok(results[0].get("connection_interrupted"))
    contains(results[0].get("error", ""), "上传连接中断")


def test_upload_preflight_rejects_missing_workspace_or_auth_before_sending_bytes():
    """旧后端也能通过只读检查明确拒绝，无需先发送 ZIP 再等待连接中断。"""
    for status, detail in (
            (409, {"error": "请先打开 ADS 工作区", "kind": "no_workspace"}),
            (401, {"error": "unauthorized"})):
        srv = _LocalBackend({"/models/packages": (status, detail)})
        try:
            with _real_upload_worker():
                worker = MA.UploadWorker(_make_zip("preflight.zip", size_kb=2048),
                                         base=srv.base, auth={"X-Ads-Agent-Token": "test"})
                results, progress = [], []
                worker.result.connect(results.append)
                worker.progress.connect(lambda sent, total: progress.append(sent))
                worker.run()
            eq(len(results), 1)
            eq(results[0].get("http"), status)
            eq(results[0].get("stage"), "preflight")
            eq([c["method"] for c in srv.calls], ["GET"], "拒绝后不能发送 ZIP POST")
            eq(progress, [], "拒绝前不能读取/发送 ZIP 内容")
            ok(not results[0].get("backend_down"), "业务拒绝不能触发后端恢复")
            if status == 409:
                eq(results[0].get("kind"), "no_workspace")
        finally:
            srv.close()


def test_upload_posts_raw_bytes_via_its_own_thread():
    """真起一个后端收请求：原始字节 + 中文文件名两个头 + 分块读出来仍完整。"""
    srv = _LocalBackend(responses={"/models/upload": {
        "ok": True, "workspace": r"D:\ws",
        "package": {"package_id": "p1", "filename": "村田_电容库.zip",
                    "state": "saved", "state_label": "已保存"}}})
    try:
        w = _new_panel(("A",))
        path = _make_zip("村田_电容库.zip", size_kb=32)
        with open(path, "rb") as f:
            blob = f.read()
        with _backend_at(srv.base), _real_upload_worker():
            w._start_zip_upload(path)
            _wait_workers(w)
        uploads = [c for c in srv.calls if c["path"].startswith("/models/upload")]
        eq(len(uploads), 1, "上传请求应真的发出去（且只发一次）")
        call = uploads[0]
        eq(call["method"], "POST")
        eq(call["headers"].get("Content-Type"), "application/zip",
           "上传必须用 application/zip")
        ok(call["headers"].get("X-Ads-Filename"), "要发 ASCII 文件名头")
        contains(call["headers"].get("X-Ads-Filename-Star", ""), "UTF-8''",
                 "中文文件名要按 RFC 5987 发 filename* 头")
        eq(call["body"], blob,
           "请求体必须是原始 ZIP 字节（不做 Base64，分块读出来还要完整）")
    finally:
        srv.close()


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
    # 全工程唯一的大小口径：SI 十进制（1 MB = 1000 KB）
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
    import json as _json
    return _json.dumps(obj, ensure_ascii=False)


def test_upload_failure_keeps_reason_and_offers_retry():
    w = _new_panel(("A",))
    entry = _attachment(w, filename="x.zip")
    w._on_upload_done(entry, "A", {"ok": False, "http": 413,
                                   "error": "文件过大（999 字节 > 上限 100 字节）"})
    contains(entry["meta"]["error"], "文件过大", "失败原因要原文显示")
    row = _one_row(w)
    row.refresh()
    ok(not row.retry_btn.isHidden(), "失败后必须给出重试按钮")
    ok(row.import_btn.isHidden(), "上传没成功时不该给「解压并导入」")


def test_interrupted_upload_recovers_backend_and_retries_once():
    """连接中断时自动恢复并重传一次，连续失败后留给用户手动重试。"""
    w = _new_panel(("A",))
    path = _make_zip("recover.zip")
    CALLS.clear()
    w._start_zip_upload(path)
    entry = _entries(w, "A")[0]
    retries = []
    w._auto_revive = lambda retry=None: retries.append(retry)

    failure = {"ok": False, "backend_down": True,
               "connection_interrupted": True,
               "error": "上传连接中断（[WinError 10053]）"}
    w._on_upload_done(entry, "A", failure)

    eq(len(retries), 1)
    ok(callable(retries[0]), "恢复后应自动重试上传")
    contains(w.status.text(), "自动重试")
    retries[0]()
    eq(entry["meta"]["state"], "uploading", "自动重试应复用原卡片")
    eq(len([c for c in CALLS if c[0] == "upload_started"]), 2,
       "自动重试只能重传一次")
    eq(entry["meta"]["automatic_retry_count"], 1)

    w._on_upload_done(entry, "A", failure)
    eq(len(retries), 2)
    ok(retries[1] is None, "第二次失败不能再自动循环重试")
    contains(w.status.text(), "连接仍未恢复")


def test_retry_after_upload_failure_reuploads_local_file():
    w = _new_panel(("A",))
    path = _make_zip("retry.zip")
    entry = _attachment(w, filename="retry.zip")
    entry["meta"]["local_path"] = path
    CALLS.clear()
    w._attach_retry(entry)
    ok(any(c[0] == "upload_started" and c[1] == path for c in CALLS),
       "上传失败后的重试应重传同一个本地文件")


def test_retry_writes_back_to_the_originating_chat():
    """重试必须回写**这张卡片所在的聊天**，不是此刻屏幕上那一个。"""
    w = _new_panel(("A", "B"), active="A")
    path = _make_zip("retry-back.zip")
    entry = _attachment(w, "A", filename="retry-back.zip", state="failed",
                        state_label="上传失败", error="后端未启动（连接被拒）")
    entry["meta"]["local_path"] = path
    w._apply_project("B")            # 上传失败后用户切到了 B
    CALLS.clear()
    w._attach_retry(entry)
    ok(any(c[0] == "upload_started" and c[1] == path for c in CALLS),
       "重试应重传同一个本地文件")
    eq(entry["meta"]["state"], "uploading", "原卡片就地回到上传中")
    eq(len(_entries(w, "B")), 0, "不该在当前项目里新开一张卡片")
    eq(len(_entries(w, "A")), 1, "也不该在原项目里叠出第二张卡片")


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


def test_poll_deduplicates_across_chats():
    """同一个包被两个聊天引用：一拍只发**一次**清单请求。"""
    w = _new_panel(("A", "B"), active="A")
    _attachment(w, "A", package_id="p1", filename="a.zip", state="importing")
    _attachment(w, "B", package_id="p1", filename="a.zip", state="importing")
    calls = []
    w._spawn_model_worker = lambda payload, path, cb, timeout=120: calls.append(path)
    w._on_attach_poll_tick()
    eq(len(calls), 1, f"同一个包只该发一次请求，实际发了 {len(calls)} 次")


def test_poll_skips_while_a_request_is_in_flight():
    """上一次清单请求还没回来时不重复发（1.6s 一拍，慢后端会积压）。"""
    w = _new_panel(("A",))
    _attachment(w, package_id="p1", filename="a.zip", state="importing")
    calls = []
    w._spawn_model_worker = lambda payload, path, cb, timeout=120: calls.append(path)
    w._on_attach_poll_tick()
    eq(len(calls), 1)
    w._on_attach_poll_tick()
    eq(len(calls), 1, "在途请求未回执时不应再发")
    w._on_packages({"ok": True, "packages": [
        {"package_id": "p1", "state": "importing"}]}, "p1")   # 回执到达
    w._on_attach_poll_tick()
    eq(len(calls), 2, "回执到达后可以再发下一次")


def test_poll_refreshes_every_chat_that_references_the_package():
    """同一包在多个聊天中被引用：每张卡片都要刷新，不能只刷第一个。"""
    w = _new_panel(("A", "B"), active="A")
    a = _attachment(w, "A", package_id="p1", filename="a.zip", state="importing")
    b = _attachment(w, "B", package_id="p1", filename="a.zip", state="importing")
    w._spawn_model_worker = lambda *a, **k: None
    w._on_packages({"ok": True, "packages": [
        {"package_id": "p1", "state": "ready", "state_label": "已就绪",
         "model_count": 7}]}, "p1")
    eq(a["meta"]["state"], "ready", "A 里那张卡片要跟上")
    eq(b["meta"]["state"], "ready", "B 里那张卡片也要跟上")
    eq(b["meta"]["model_count"], 7)


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


def test_pagination_does_not_misjudge_a_live_package_as_lost():
    """/models/packages 有返回上限：没出现在这一页里 ≠ 包没了。"""
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="ready")
    trunc = {"ok": True, "workspace": r"D:\ws", "total": 120, "returned": 50,
             "packages": [{"package_id": "p2", "state": "saved"}]}
    w._on_packages(trunc, "p1")
    eq(entry["meta"].get("missing"), None,
       "清单被截断时不能把没在这一页里的包判成丢失")
    contains(entry["meta"]["message"], "无法确认", "要如实说没确认到")
    w._on_verify_packages(trunc, [entry])
    eq(entry["meta"].get("missing"), None,
       "重启核对走同一条判据，截断时也不能误判")


def test_workspace_switch_reports_ownership_mismatch_and_blocks_import():
    """换了工作区：说清归属不匹配（两个工作区都点名），且不发起导入。"""
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="ready",
                        workspace=r"D:\ws\old")
    w._on_verify_packages({"ok": True, "workspace": r"D:\ws\new",
                           "packages": []}, [entry])
    eq(entry["meta"]["missing"], True, "不在当前工作区就要如实标注")
    contains(entry["meta"]["missing_reason"], r"D:\ws\old", "要说清原来属于哪个工作区")
    contains(entry["meta"]["missing_reason"], r"D:\ws\new", "要说清现在打开的是哪个")
    contains(_row_text(_one_row(w)), "工作区", "卡片上也要说清归属")
    w._attach_import(entry)
    eq(entry["meta"]["state"], "ready", "归属不匹配时不该发起导入")
    contains(w.status.text(), "工作区", "要告诉用户为什么没导入")


def test_poll_stops_after_max_ticks():
    w = _new_panel(("A",))
    entry = _attachment(w, package_id="p1", filename="a.zip", state="importing")
    w._attach_poll_ticks["p1"] = MA.POLL_MAX_TICKS
    w._spawn_model_worker = lambda *a, **k: None
    w._attach_poll.start()          # 先真的跑起来，才能验证"到点会停"
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
    w._pending_attachments["A"] = [{"package_id": "p1", "meta": {
        "filename": "secret-content.zip", "size_bytes": len(blob),
        "package_kind_label": "ADS Design Kit", "state": "saved",
        "state_label": "已保存", "workspace": r"D:\ws"}}]
    msg = w._attachment_manifest(w._pending_attachments["A"])
    for size in (64, 128, 512):
        not_contains(msg, blob[:size].decode("latin-1"),
                     "消息里不应出现 ZIP 的二进制内容")
    not_contains(msg, "lib.defs", "不应把包内文件名/内容塞进消息")


def test_attachment_send_is_blocked_while_a_turn_is_running():
    """上一轮在跑时不发附件，但附件不能丢 —— 放回待发送队列。"""
    w = _new_panel(("A",))
    worker = w._worker = _StubChatWorker([], False, "m")
    worker.start()
    w._pending_attachments["A"] = [{"package_id": "p1",
                                    "meta": {"filename": "a.zip"}}]
    w._on_send()
    eq(len(w.history), 0, "上一轮在跑时不该发新请求")
    eq(len(w._pending_attachments["A"]), 1, "附件必须留在队列里等下一轮")
    worker.stop()


def test_pending_attachments_are_scoped_per_chat():
    """A 项目的待发送附件不能因为在 B 里按了发送就被带过去。"""
    w = _new_panel(("A", "B"), active="A")
    w._pending_attachments["A"] = [{"package_id": "pA",
                                    "meta": {"filename": "a.zip"}}]
    w._apply_project("B")
    eq(w._take_pending_attachments(), [], "B 不该拿到 A 的待发送附件")
    eq(len(w._pending_attachments["A"]), 1, "A 的附件要留在 A 名下")
    w.input.setPlainText("在 B 里问个问题")
    w._on_send()
    not_contains(w.history[-1]["content"], "a.zip",
                 "B 的这一发不该带上 A 的附件")


def test_pending_attachments_dropped_with_deleted_project():
    """项目删掉后，它没发出去的附件不该被下一次发送带上。"""
    w = _new_panel(("A", "B"), active="A")
    w._pending_attachments["A"] = [{"package_id": "pA",
                                    "meta": {"filename": "a.zip"}}]
    _select_project(w, "A")
    w._delete_project()
    ok("A" not in w._pending_attachments, "已删除项目的待发送附件应一并丢弃")
    eq(w.projects_data.get("active"), "B")
    w.input.setPlainText("继续")
    w._on_send()
    not_contains(w.history[-1]["content"], "a.zip",
                 "删除项目后不该把它没发出的附件带进别的对话")


def test_attachment_actions_do_not_depend_on_the_chat_channel():
    """聊天通道完全不可用时，上传/导入/查看仍然各自发出 HTTP 请求。

    用**实际收到的请求**证明，不看源码里有没有某个字符串：把后端基址指到
    一个真起在回环上的服务，再让 ChatWorker 一启动就抛错。
    """
    srv = _LocalBackend(responses={
        "/models/import": {"ok": True, "op_id": "op-1", "accepted": True,
                           "message": "导入已开始（后台执行）。"},
        "/models/package": {"ok": True, "package": {
            "package_id": "p1", "state": "ready", "state_label": "已就绪",
            "model_count": 3, "models": []}},
    })
    try:
        w = _new_panel(("A",))
        entry = _attachment(w, package_id="p1", filename="a.zip", state="saved")

        class _DeadChat(_StubChatWorker):
            """LLM 侧完全不可用：一发就炸。"""

            started = False

            def start(self):
                _DeadChat.started = True
                raise RuntimeError("LLM 服务不可达")

        w._worker = _DeadChat([], False, "m")
        with _backend_at(srv.base):
            w._attach_import(entry)
            _wait_workers(w)
            w._attach_view(entry)
            _wait_workers(w)
        paths = srv.paths()
        ok(any(p.startswith("/models/import") for p in paths),
           f"导入请求应独立于聊天通道发出，实际收到：{paths}")
        ok(any(p.startswith("/models/package") for p in paths),
           f"查看请求应独立发出，实际收到：{paths}")
        eq(_DeadChat.started, False, "这两步压根没碰聊天通道")
    finally:
        srv.close()


def test_upload_works_without_the_chat_channel():
    """上传同样不依赖聊天：LLM 不可用时 ZIP 照样发出去。"""
    srv = _LocalBackend(responses={"/models/upload": {
        "ok": True, "workspace": r"D:\ws",
        "package": {"package_id": "p1", "filename": "a.zip", "state": "saved",
                    "state_label": "已保存"}}})
    try:
        w = _new_panel(("A",))
        w._worker = _StubChatWorker([], False, "m")   # 聊天通道存在但没在用
        path = _make_zip("no-llm.zip")
        with _backend_at(srv.base), _real_upload_worker():
            w._start_zip_upload(path)
            _wait_workers(w)
        ok(any(p.startswith("/models/upload") for p in srv.paths()),
           "上传请求应独立于聊天通道发出")
    finally:
        srv.close()


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


def test_async_receipt_does_not_leak_into_other_projects():
    """项目删除后到达的异步回执：既不复活它，也不往别的项目里塞东西。"""
    w = _new_panel(("A", "B"), active="A")
    entry = {"kind": "attachment", "text": "a.zip",
             "meta": {"filename": "a.zip", "state": "importing",
                      "package_id": "p1"}, "expanded": True}
    w._add_entry("attachment", "a.zip", "A", raw_entry=entry)
    del w.projects_data["projects"]["A"]
    w.projects_data["active"] = "B"
    w._apply_project("B")
    before = len(_entries(w, "B"))
    w._on_packages({"ok": True, "packages": [
        {"package_id": "p1", "state": "ready", "state_label": "已就绪"}]}, "p1")
    w._on_import_ack(entry, "A", {"ok": True, "op_id": "op-1", "accepted": True})
    ok("A" not in w.projects_data["projects"], "不该复活已删除的项目")
    eq(len(_entries(w, "B")), before, "不该往别的项目里凭空加内容")


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
    eq(entry["meta"].get("missing"), None,
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
    w._pending_attachments["A"] = [{"package_id": "p1",
                                    "meta": {"filename": "a.zip"}}]
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


# 替身在模块加载时装好（pytest 直接收集时也一样生效），_teardown 时逐个还原
_install_stubs()


if __name__ == "__main__":
    try:
        code = run(globals(), "模型压缩包附件（PySide6 / 真实控件）")
    finally:
        _teardown()
    sys.exit(code)
