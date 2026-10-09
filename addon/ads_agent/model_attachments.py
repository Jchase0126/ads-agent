"""模型压缩包附件：上传 worker + 附件卡片（PySide6）。

为什么单独一个模块：``panel.py`` 已经是 2700 多行的面板主体，附件这条链
（拖放、二进制上传、导入轮询、卡片渲染）自成一体，塞进去会让面板更难读。
本模块只依赖 Qt 与标准库，**不导入 panel**（panel 依赖本模块，反向 import
会成环），因此后端基址与鉴权头由 panel 在构造 worker 时显式传进来。

三条硬约束（与后端、与 ``backend/agent.py`` 的系统提示同一口径）：

* **上传 ≠ 导入**：上传只落盘并检查包结构，不解压、不挂库；导入必须由用户
  在卡片上点「解压并导入」。
* **上传不依赖 LLM 服务连通**：这里只用 HTTP，不碰任何模型服务。
* **ZIP 内容不进对话/LLM 请求**：只有文件名、大小、package_id 这类标识会
  出现在聊天消息里（见 panel 的 ``_attachment_manifest``）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import qtcompat

QtCore = qtcompat.QtCore()
QtGui = qtcompat.QtGui()
QtWidgets = qtcompat.QtWidgets()
Qt = QtCore.Qt
QObject, QThread, Signal, QSize = (QtCore.QObject, QtCore.QThread,
                                   QtCore.Signal, QtCore.QSize)
QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget = (
    QtWidgets.QHBoxLayout, QtWidgets.QLabel, QtWidgets.QPlainTextEdit,
    QtWidgets.QPushButton, QtWidgets.QToolButton, QtWidgets.QVBoxLayout,
    QtWidgets.QWidget)

import uiscale as U

# 后端 /models/packages 一次最多返回全部，但面板只渲染有限行 —— 与后端
# models_truncated 的口径一致：截断是**可见的**，不假装完整。
_MODELS_SHOWN = 20
_MODELS_VIEW_MAX_H = 240

# 状态 → 中文说明。与 backend/model_tools.py 的 STATE_LABELS 保持同一份口径，
# 这里只是**兜底**：卡片优先显示后端给的 state_label（口径以服务端为准）。
STATE_LABELS = {
    "saved": "已保存",
    "inspecting": "检查中",
    "pending_import": "待导入",
    "importing": "导入中",
    "pending_verify": "待验证",
    "ready": "已就绪",
    "failed": "失败",
    "cancelled": "已取消",
    "awaiting_user": "等待用户操作",
    # 面板侧自有的两个状态：资产不在当前工作区（重启核对后发现）、
    # 上传中。都不在后端的状态机里，所以只在这里定义文案。
    "missing": "资产已不可用",
    "uploading": "上传中",
}

# 轮询导入进度时会停在这个集合里：它们表示"后端还在动"。
POLL_ACTIVE_STATES = {"importing", "inspecting"}
# 轮询上限：1.6s × 1500 ≈ 40 分钟。到点后停止自动刷新并如实告诉用户，
# 而不是无限轮询一个可能已经死掉的任务。
POLL_INTERVAL_MS = 1600
POLL_MAX_TICKS = 1500

NO_WORKSPACE_HINT = (
    "当前没有打开 ADS 工作区，模型资产需要归属于一个工作区。"
    "请先在 ADS 主窗口中打开或新建 Workspace 再上传；"
    "聊天项目名称不会自动打开 ADS 工作区。"
)

# /models/packages 有返回上限（后端默认一页 50 个）。请求时显式要一页更大的
# 清单，但**不能假设**服务端一定照办 —— 响应里的 total/returned 才是判据。
PACKAGES_MAX_ITEMS = 1000


def packages_path(limit: int = PACKAGES_MAX_ITEMS) -> str:
    """GET 包清单的路径（带显式页大小；服务端忽略也不影响正确性）。"""
    return f"/models/packages?max_items={max(1, int(limit))}"


def listing_truncated(data: dict) -> bool:
    """这一页清单是否被"返回上限"截断了？

    截断时**不能**把"没出现在这一页里"当成"包没了"：那会把好端端的包误判成
    资产丢失，用户看到的提示就会让他去重传一个根本没丢的文件。
    """
    try:
        total = int(data.get("total"))
        returned = int(data.get("returned"))
    except (TypeError, ValueError):
        return False        # 服务端没给计数：按"清单是完整的"处理
    return returned < total


def workspace_mismatch_reason(recorded: str, current: str) -> str:
    """换工作区后的归属不匹配说明（与"资产被删掉"是两回事）。"""
    return (f"这个包上传到工作区「{recorded}」，而当前 ADS 打开的是「{current}」"
            "—— 模型包归属于上传时所在的工作区，不会跟着工作区切换搬过去。"
            "请回到原工作区操作，或在当前工作区重新上传这个 ZIP。")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def is_zip_path(path: str) -> bool:
    return str(path or "").lower().endswith(".zip")


def human_size(size) -> str:
    """字节数 → 人类可读（16.6 MB）。

    单位按 **SI 十进制** 定义：1 KB = 1000 B、1 MB = 1000 KB。全工程只有这一
    处把字节数画给人看，口径必须唯一 —— 否则同一份 16,600,000 字节的包在
    卡片上是 15.8 MB、在别处是 16.6 MB，用户只会以为两个地方说的不是同一个
    文件。拿不到大小就如实说"大小未知"。
    """
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "大小未知"
    if value < 0:
        return "大小未知"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"


def _fmt_freq(hz) -> str:
    try:
        value = float(hz)
    except (TypeError, ValueError):
        return ""
    for unit, div in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(value) >= div:
            return f"{value / div:.3g} {unit}"
    return f"{value:.0f} Hz"


def _fmt_range(start, stop) -> str:
    lo, hi = _fmt_freq(start), _fmt_freq(stop)
    return f"{lo}~{hi}" if lo and hi else (lo or hi)


def state_label(meta: dict) -> str:
    """状态文案：服务端给的 state_label 优先，本地只做兜底。"""
    meta = meta or {}
    return str(meta.get("state_label") or STATE_LABELS.get(
        str(meta.get("state") or ""), str(meta.get("state") or "状态未知")))


def _measure(text: str, font, width: int) -> int:
    """按给定宽度排出文本高度。

    刻意**不复用** panel._measure_text：panel 依赖本模块，反向 import 会成环。
    这里只抄它最核心的一段（QTextDocument 排版），并且带缓存 —— 拖动面板会
    反复重排，同一段文字没必要每帧重排一遍。
    """
    width = max(int(width), 1)
    key = (text, width, font.key())
    hit = _MEASURE_CACHE.get(key)
    if hit is not None:
        return hit
    QTextDocument, QTextOption = QtGui.QTextDocument, QtGui.QTextOption

    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    doc.setDefaultTextOption(opt)
    doc.setPlainText(text)
    doc.setTextWidth(width)
    height = int(doc.size().height())
    if len(_MEASURE_CACHE) > 400:
        _MEASURE_CACHE.clear()
    _MEASURE_CACHE[key] = height
    return height


_MEASURE_CACHE: dict = {}


# ---------------------------------------------------------------------------
# 拖放
# ---------------------------------------------------------------------------

def local_paths(mime) -> tuple:
    """从拖放数据里取本地文件路径，返回 (路径列表, 是否带任何文件)。"""
    if mime is None or not mime.hasUrls():
        return [], False
    out = []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if path:
            out.append(path)
    return out, bool(out)


class ZipDropFilter(QObject):
    """在若干控件上接管 ZIP 拖放。

    用事件过滤器而不是给每个控件写子类：输入框（QTextEdit 默认自己接受拖放，
    会把文件 URL 当文本插进去）、会话区（QListWidget 自带滚动/选择语义）都
    不该为附件改行为，过滤器只在"确实是 ZIP"时接管，其余事件照常放行。
    """

    def __init__(self, on_zip, on_other, parent=None):
        super().__init__(parent)
        self._on_zip = on_zip          # (paths:list[str]) -> None
        self._on_other = on_other      # (paths:list[str]) -> None

    def eventFilter(self, obj, event):  # noqa: N802 — Qt 命名
        QEvent = QtCore.QEvent

        kind = event.type()
        if kind not in (QEvent.Type.DragEnter, QEvent.Type.DragMove,
                        QEvent.Type.Drop):
            return False
        paths, _has_any = local_paths(event.mimeData())
        zips = [p for p in paths if is_zip_path(p)]
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            # 悬停阶段只决定"要不要接收光标"，不弹提示 —— 一次拖入会经过
            # 几十个 DragMove，提示会刷屏。真正的提示放在 drop。
            if zips:
                event.acceptProposedAction()
            else:
                event.ignore()
            return True
        if zips:
            event.acceptProposedAction()
            self._on_zip(zips)
        else:
            event.ignore()
            if paths:
                self._on_other(paths)
        return True


# ---------------------------------------------------------------------------
# workers（网络请求一律不在 Qt 主线程）
# ---------------------------------------------------------------------------

def _error_body(exc: urllib.error.HTTPError) -> tuple:
    """把 HTTPError 的响应体读成 (message, detail)。

    模型接口的错误信息**只在响应体里**（例如 409 no_workspace、413 过大、
    415 非 ZIP），只看状态码会丢掉用户真正需要知道的那句话。
    """
    raw = b""
    try:
        raw = exc.read() or b""
    except Exception:  # noqa: BLE001 — 读不到就按状态码给兜底文案
        pass
    detail = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                detail = parsed
        except (ValueError, UnicodeError):
            pass
    message = str(detail.get("error") or detail.get("message") or exc.reason or "")
    detail["http"] = exc.code
    return message or f"后端返回 HTTP {exc.code}", detail


class ModelApiWorker(QThread):
    """模型包的 JSON 接口调用（列表 / 详情 / 导入 / 取消）。

    与 panel.ConfigWorker 的区别：这里**保留 HTTP 状态码与响应体里的
    kind/message**。附件的失败提示必须给出真实原因（"没有打开工作区"和
    "文件过大"处置方式完全不同），笼统的"后端返回 HTTP 409"没有用。
    """

    result = Signal(dict)

    def __init__(self, path: str, payload: dict | None = None, base: str = "",
                 auth: dict | None = None, timeout: int = 60, parent=None):
        super().__init__(parent)
        self._path = path
        self._payload = payload            # None -> GET
        self._base = base
        self._auth = dict(auth or {})
        self._timeout = max(5, int(timeout))

    def run(self):
        url = self._base + self._path
        try:
            if self._payload is None:
                req = urllib.request.Request(url, headers=dict(self._auth))
            else:
                headers = {"Content-Type": "application/json"}
                headers.update(self._auth)
                req = urllib.request.Request(
                    url,
                    data=json.dumps(self._payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
            with _opener().open(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                data = {"data": data}
            self.result.emit({"ok": True, **data})
        except urllib.error.HTTPError as e:
            message, detail = _error_body(e)
            self.result.emit({"ok": False, "error": message, **detail})
        except urllib.error.URLError as e:
            self.result.emit({"ok": False, "error": f"后端未启动（{e.reason}）",
                              "backend_down": True})
        except Exception as e:  # noqa: BLE001
            self.result.emit({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _opener():
    """绕过 HTTP 代理访问回环后端（与 panel._LOOPBACK 同一理由）。

    企业网络常设 HTTP_PROXY，urllib 连 127.0.0.1 也会发给代理，
    于是附件上传/导入会误报"后端未启动"。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _CountingReader:
    """文件读取包装：边读边报进度。

    http.client 对文件对象是分块 read() 后 sendall 的，所以包一层就能拿到
    真实进度，且**整包不会进内存**（几十 MB 的 ZIP 是常态）。
    """

    def __init__(self, stream, total: int, worker: "UploadWorker"):
        self._stream = stream
        self._total = max(1, int(total))
        self._sent = 0
        self._worker = worker

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size if size and size > 0 else self._block)
        if chunk:
            self._sent += len(chunk)
            self._worker._emit_progress(self._sent, self._total)
        return chunk

    @property
    def _block(self) -> int:
        return 1 << 20


class UploadWorker(QThread):
    """把 ZIP 以**原始字节流**上传到 /models/upload。

    不做 Base64、不塞进 JSON：Base64 膨胀 33%，更关键的是它会让模型文件
    内容有机会进入对话历史与 LLM 请求 —— 那正是这套设计要避免的。
    """

    result = Signal(dict)
    progress = Signal(int, int)          # 已发送字节, 总字节

    def __init__(self, path: str, base: str = "", auth: dict | None = None,
                 session: str = "", chunk: int = 1 << 20, parent=None):
        super().__init__(parent)
        self._path = path
        self._base = base
        self._auth = dict(auth or {})
        self._session = session or ""
        self._chunk = max(64 * 1024, int(chunk))
        self._last_pct = -1

    # 供 _CountingReader 回调（跨线程只发信号，不碰 UI）
    def _emit_progress(self, sent: int, total: int):
        pct = int(sent * 100 / total)
        if pct != self._last_pct:       # 只在百分比变化时发，避免刷爆队列
            self._last_pct = pct
            self.progress.emit(sent, total)

    def _headers(self, filename: str, size: int) -> dict:
        # 文件名放请求头（JSON 通道会和内容混在一起）。中文名必须两个头都发：
        # ASCII 版给老服务端兜底，filename* 按 RFC 5987 携带 UTF-8。
        ascii_name = "".join(ch for ch in filename if ord(ch) < 128).strip()
        headers = {
            "Content-Type": "application/zip",
            "X-Ads-Filename": ascii_name or "model_package.zip",
            "X-Ads-Filename-Star": "UTF-8''" + urllib.parse.quote(filename, safe=""),
        }
        if self._session:
            headers["X-Ads-Session"] = self._session
        headers.update(self._auth)
        # 显式给 Content-Length：http.client 见到文件对象会自己分块 read()，
        # 但它算不出长度（文件对象没有 __len__），不给就会退化成 chunked 传输，
        # 而后端按 Content-Length 判定空包 / 超限。
        headers["Content-Length"] = str(size)
        return headers

    def run(self):
        filename = os.path.basename(self._path)
        try:
            size = os.path.getsize(self._path)
        except OSError as e:
            self.result.emit({"ok": False, "error": f"读取文件失败：{e}"})
            return
        if size <= 0:
            self.result.emit({"ok": False, "error": "文件是空的，没有可上传的内容"})
            return
        url = self._base + "/models/upload"
        stage = "preflight"
        try:
            # 使用已有的只读接口，兼容尚未重启的旧后端。鉴权或工作区检查
            # 必须发生在发送 ZIP 前，否则旧后端提前拒绝大请求体时，客户端
            # 只能看到连接中断，收不到真正的 401 / 409 错误。
            check = urllib.request.Request(
                self._base + "/models/packages", headers=dict(self._auth),
                method="GET")
            with _opener().open(check, timeout=60) as response:
                ready = json.loads(response.read().decode("utf-8"))
            if not isinstance(ready, dict) or not ready.get("workspace") \
                    or ready.get("ok") is False:
                detail = ready if isinstance(ready, dict) else {}
                self.result.emit({"ok": False, "stage": stage,
                                  "kind": detail.get("kind"),
                                  "error": detail.get("error") or
                                  "上传前检查未能确认 ADS 工作区，"
                                  "请确认 ADS 已打开 Workspace 后重试。"})
                return
            stage = "upload"
            # data 必须在 Request 构造时传入：urllib 的 data setter 在数据变化
            # 时会**删掉**已有的 Content-length 头，事后补就补不上了。
            with open(self._path, "rb") as stream:
                reader = _CountingReader(stream, size, self)
                req = urllib.request.Request(
                    url, data=reader,
                    headers=self._headers(filename, size),
                    method="POST",
                )
                with _opener().open(req, timeout=900) as resp:
                    raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                self.result.emit({"ok": False,
                                  "error": "后端返回的不是预期内容（模型包已上传，"
                                           "但回执无法解析）"})
                return
            if not isinstance(data, dict):
                data = {"data": data}
            self.result.emit({"ok": True, **data})
        except urllib.error.HTTPError as e:
            message, detail = _error_body(e)
            self.result.emit({"ok": False, "stage": stage,
                              "error": message, **detail})
        except urllib.error.URLError as e:
            reason = e.reason
            interrupted = isinstance(reason, (ConnectionAbortedError,
                                              ConnectionResetError,
                                              BrokenPipeError, TimeoutError))
            message = (f"上传连接中断（{reason}）" if interrupted
                       else f"无法连接后端（{reason}）")
            self.result.emit({"ok": False, "error": message,
                              "backend_down": True,
                              "connection_interrupted": interrupted})
        except Exception as e:  # noqa: BLE001
            self.result.emit({"ok": False,
                              "error": f"{type(e).__name__}: {e}",
                              "local_retryable": True})


# ---------------------------------------------------------------------------
# 附件卡片
# ---------------------------------------------------------------------------

def _models_text(meta: dict) -> str:
    """把型号索引渲染成纯文本（一个型号一段，含来源与缺失说明）。"""
    models = [m for m in (meta.get("models") or []) if isinstance(m, dict)]
    if not models:
        return ""
    lines = []
    for item in models[:_MODELS_SHOWN]:
        part = item.get("part") or "（型号未确定）"
        bits = [str(part)]
        library = item.get("library")
        if library:
            bits.append(f"库 {library}")
        ports = item.get("ports")
        if ports:
            bits.append(f"{ports} 端口")
        freq = _fmt_range(item.get("freq_start_hz"), item.get("freq_stop_hz"))
        if freq:
            bits.append(freq)
        z0 = item.get("reference_impedance_ohm")
        if z0:
            bits.append(f"{z0}Ω")
        bias = item.get("bias") or {}
        if isinstance(bias, dict) and bias:
            shown = "，".join(f"{k}={v}" for k, v in list(bias.items())[:3]
                             if v not in (None, ""))
            if shown:
                bits.append(f"工作点 {shown}")
        lines.append(" · ".join(bits))
    # 截断必须**可见**：后端自己也会截断（models_truncated），两处都要算上，
    # 不能让用户以为看到的就是全部型号。
    shown = len(lines)
    hidden = max(0, len(models) - shown) + int(meta.get("models_truncated") or 0)
    if hidden > 0:
        lines.append(f"—— 仅显示前 {shown} 个，另有 {hidden} 个未显示"
                     "（完整清单可问 Agent 或用 list_vendor_models 查询）")
    return "\n".join(lines)


# outcome → 中文。与契约 §3.1 的取值一一对应（只做展示，判断仍以后端为准）。
_OUTCOME_LABELS = {
    "opened": "已打开原生入口并定位到库/分类",
    # 本机 ADS 无 API 可弹窗口，但库/分类确实已在原生列表注册并定位到 ——
    # TDK 这类正常包的主路径，如实降级，绝不写成"已打开/已选中分类"。
    "located": "已定位到原生元件列表中的库/分类（本机 ADS 无法程序化打开窗口）",
    "loaded_only": "库已加载，但界面无法程序化打开",
    "not_attached": "库未挂接到当前工作区",
    "unsupported": "该包不是 Design Kit，没有原生元件面板",
    "workspace_mismatch": "当前 ADS 工作区与包记录不一致",
    "failed": "打开失败",
}
# 未知 outcome 的中文兜底：不把英文键名当标签（原文仍放括号里可追溯）。
_OUTCOME_FALLBACK = "已返回结果"


def _native_text(meta: dict) -> str:
    """把「在 ADS 元件列表中打开」的结果渲染成纯文本状态行。

    只**如实转述**后端 ``open_vendor_palette`` 的证据（boot.loaded /
    native.* / limits）：``loaded=="unknown"`` 或缺少证据时一律说"无法确认"，
    **绝不**把 unknown 说成成功 —— 这是原厂列表功能的验收底线（契约 §1/§5）。
    """
    info = (meta or {}).get("native_list")
    if not isinstance(info, dict) or not info:
        return ""

    lines = []
    outcome = str(info.get("outcome") or "")
    if outcome:
        # 已知 outcome 用中文标签；未知的走兜底（原文放括号里），
        # 绝不把英文键名直接当标签显示。
        label = _OUTCOME_LABELS.get(outcome)
        if label:
            lines.append(f"原生列表：{label}（{outcome}）")
        else:
            lines.append(f"原生列表：{_OUTCOME_FALLBACK}（{outcome}）")
    if info.get("ok") is False:
        err = str(info.get("error") or "").strip()
        if err:
            lines.append(f"打开失败：{err}")
    msg = str(info.get("message") or "").strip()
    if msg:
        lines.append(msg)

    boot = info.get("boot") or {}
    if isinstance(boot, dict) and boot:
        loaded = boot.get("loaded")
        if loaded is True:
            lines.append("库已加载启动配置（boot.ael 已生效）")
        elif loaded is False:
            lines.append("库未加载启动配置（boot.ael 缺失或未生效）")
        else:
            # 键缺失也按 unknown 处理：没有证据就不许说成功。
            lines.append("库是否加载启动配置：无法确认（未取得证据）")
        registered = boot.get("registered_components")
        # bool 是 int 的子类，要显式排除；缺失/非数字一律容错跳过。
        if isinstance(registered, int) and not isinstance(registered, bool):
            lines.append(f"已注册元件 {registered} 个")
        groups = boot.get("palette_groups") or []
        if isinstance(groups, (list, tuple)) and len(groups) > 0:
            names = []
            for g in groups:
                if isinstance(g, dict):
                    name = str(g.get("name") or g.get("label") or "").strip()
                else:
                    name = str(g).strip()
                if name:
                    names.append(name)
            sample = "、".join(names[:3])
            lines.append(f"原生分组 {len(groups)} 个"
                         + (f"（如 {sample}）" if sample else ""))

    native = info.get("native") or {}
    if isinstance(native, dict) and native:
        cl = native.get("component_library") or {}
        if isinstance(cl, dict) and cl:
            if cl.get("opened"):
                method = str(cl.get("method") or "").strip()
                lines.append("Component Library：已打开"
                             + (f"（{method}）" if method else ""))
            else:
                detail = str(cl.get("detail") or "").strip()
                lines.append("Component Library：未打开"
                             + (f"（{detail}）" if detail else ""))
        palette = native.get("palette") or {}
        if isinstance(palette, dict) and palette:
            method = str(palette.get("method") or "").strip()
            if palette.get("opened"):
                lines.append("Palette：已打开" + (f"（{method}）" if method else ""))
            else:
                lines.append("Palette：未打开" + (f"（{method}）" if method else ""))
        located = native.get("located") or {}
        if isinstance(located, dict) and located:
            target = " / ".join(
                str(x) for x in (located.get("library"), located.get("category"))
                if x) or "（未指定库/分类）"
            via = str(located.get("via") or "").strip()
            window = str(located.get("window") or "").strip()
            if via and window:
                how = f"（经 {via} 在 {window} 中定位）"
            elif via:
                how = f"（经 {via} 定位）"
            elif window:
                how = f"（在 {window} 中定位）"
            else:
                how = ""
            if located.get("found"):
                lines.append(f"已在原生列表中定位到：{target}{how}")
            else:
                lines.append(f"未在原生列表中定位到：{target}")

    for limit in (info.get("limits") or []):
        text = str(limit).strip()
        if text:
            lines.append(f"限制：{text}")
    return "\n".join(lines)


class AttachmentRow(QWidget):
    """一张附件卡片：折叠头（文件名 + 大小 + 状态 + 动作）+ 展开的详情。

    宽度自适应照 BubbleRow.reflow 的做法：所有控件宽度按容器比例算，
    高度按文本实际排版结果算，不写死像素。
    """

    def __init__(self, entry: dict, pal: dict, actions: dict, on_toggle=None,
                 parent=None):
        super().__init__(parent)
        self.entry = entry
        self._pal = pal
        self._actions = actions or {}
        self._on_toggle = on_toggle

        outer = QVBoxLayout(self)
        outer.setContentsMargins(U.P("xl"), U.P("xs"), U.P("xl"), U.P("xs"))
        outer.setSpacing(U.P("xs"))

        head = QHBoxLayout()
        head.setSpacing(U.P("sm"))
        self.toggle = QToolButton()
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(bool(entry.get("expanded")))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.clicked.connect(self._toggle)
        head.addWidget(self.toggle, 1)

        # 动作按钮按状态显示：一次只暴露当前**该做**的那一步，避免用户
        # 在"还没上传完"的时候点到"导入"。
        self.import_btn = self._button("解压并导入", "soft")
        self.open_btn = self._button("在 ADS 元件列表中打开", "primary")
        self.view_btn = self._button("查看模型", "soft")
        self.cancel_btn = self._button("取消导入", "soft")
        self.resume_btn = self._button("选择套件目录", "primary")
        self.retry_btn = self._button("重试", "primary")
        self.refresh_btn = self._button("刷新状态", "ghost")
        self.import_btn.clicked.connect(lambda: self._act("import"))
        self.open_btn.clicked.connect(lambda: self._act("open"))
        self.view_btn.clicked.connect(lambda: self._act("view"))
        self.cancel_btn.clicked.connect(lambda: self._act("cancel"))
        self.resume_btn.clicked.connect(lambda: self._act("resume"))
        self.retry_btn.clicked.connect(lambda: self._act("retry"))
        self.refresh_btn.clicked.connect(lambda: self._act("refresh"))
        for btn in (self.import_btn, self.open_btn, self.view_btn, self.cancel_btn,
                    self.resume_btn, self.retry_btn, self.refresh_btn):
            head.addWidget(btn)
        outer.addLayout(head)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, U.P("xs"), 0, 0)
        body.setSpacing(U.P("xs"))
        self._labels: list = []
        for _ in range(7):
            label = QLabel("")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setFont(U.qfont("tiny"))
            label.setVisible(False)
            body.addWidget(label)
            self._labels.append(label)
        self.models_view = QPlainTextEdit()
        self.models_view.setReadOnly(True)
        self.models_view.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.models_view.setFont(U.qfont("tiny"))
        self.models_view.setVisible(False)
        body.addWidget(self.models_view)
        outer.addWidget(self.body)

        self.setStyleSheet(
            f"QWidget{{background:{pal['card_bg']};"
            f" border:1px solid {pal['card_border']};"
            f" border-radius:{U.R('lg')}px;}}"
            f"QToolButton{{text-align:left; color:{pal['text']};"
            f" background:transparent; border:none; padding:{U.P('xs')}px;"
            f" font-size:{U.fs('small')}px;{U.font_css()}}}"
            f"QToolButton:hover{{color:{pal['accent']};}}"
            f"QPlainTextEdit{{background:transparent; color:{pal['text']};"
            f" border:none; font-size:{U.fs('tiny')}px;}}"
        )
        self.refresh()

    # ------------------------------------------------------------------ 控件
    def _button(self, text: str, kind: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(_button_css(pal=self._pal, kind=kind))
        btn.setVisible(False)
        return btn

    def _act(self, name: str):
        fn = self._actions.get(name)
        if fn is not None:
            fn(self.entry)

    def _toggle(self, checked: bool):
        self.entry["expanded"] = checked
        self.refresh()
        if self._on_toggle is not None:
            self._on_toggle(self.entry, checked)

    def set_progress(self, sent: int, total: int) -> None:
        """上传进度（只更新折叠头，不触发整表重排）。"""
        pct = int(sent * 100 / max(1, total))
        meta = self.entry.get("meta") or {}
        meta["upload_percent"] = pct
        self._head_text()

    # ------------------------------------------------------------------ 渲染
    def _meta(self) -> dict:
        return self.entry.get("meta") or {}

    def _filename(self) -> str:
        return str(self._meta().get("filename")
                   or self.entry.get("text") or "模型压缩包")

    def _state(self) -> str:
        return str(self._meta().get("state") or "")

    def _is_uploading(self) -> bool:
        return self._state() == "uploading"

    def _is_missing(self) -> bool:
        return bool(self._meta().get("missing"))

    def _failed(self) -> bool:
        return self._state() == "failed" or bool(self._meta().get("error"))

    def _importing(self) -> bool:
        return self._state() in ("importing", "inspecting")

    def _can_open(self) -> bool:
        """是否展示「在 ADS 元件列表中打开」。

        三个条件缺一不可：包是 Design Kit（或混合包）；库已真实挂接到工作区；
        状态已到待验证/已就绪。只认**运行时挂接结果**（``library_attach`` 的
        ``attached``/``libraries``），不拿静态扫描的 ``defined_libraries`` 充数 ——
        静态"定义了库"≠"已挂接"。
        """
        meta = self._meta()
        if str(meta.get("package_kind") or "") not in ("design_kit", "mixed"):
            return False
        if self._state() not in ("pending_verify", "ready"):
            return False
        attach = meta.get("library_attach") or {}
        if not isinstance(attach, dict):
            return False
        if attach.get("attached"):
            return True
        for key in ("libraries", "attached_libraries"):
            val = attach.get(key)
            if isinstance(val, (list, tuple)) and len(val) > 0:
                return True
        return False

    def _head_text(self):
        """折叠头一行：文件名 · 大小 · 状态 · 型号数（省略了多少）。"""
        meta = self._meta()
        bits = [self._filename(), human_size(meta.get("size_bytes"))]
        if self._is_uploading():
            bits.append(f"上传中 {int(meta.get('upload_percent') or 0)}%")
        elif self._is_missing():
            bits.append("资产已不可用")
        elif self._failed():
            # 失败优先于后端给的 state（后端可能还停在"已保存"，但用户需要
            # 在折叠状态下就看到"失败了"）
            bits.append("失败")
        else:
            bits.append(state_label(meta))
        if meta.get("model_count") and not self._failed():
            bits.append(f"{meta['model_count']} 个型号")
        arrow = "⌄" if self.toggle.isChecked() else "›"
        self.toggle.setText("📦  " + " · ".join(str(b) for b in bits) + f"  {arrow}")

    def _line(self, index: int, text: str, color: str | None = None) -> None:
        label = self._labels[index]
        if not text:
            label.setVisible(False)
            return
        label.setText(text)
        label.setStyleSheet(
            f"color:{color or self._pal['subtle']}; background:transparent;"
            f" font-size:{U.fs('tiny')}px;{U.font_css()}"
        )
        label.setVisible(True)

    def refresh(self):
        """按当前快照重画卡片（幂等；轮询与状态变化都走这里）。"""
        meta = self._meta()
        self._head_text()

        # ---- 动作按钮：哪一步现在该做，就只露哪一个 ----
        for btn in (self.import_btn, self.open_btn, self.view_btn, self.cancel_btn,
                    self.resume_btn, self.retry_btn, self.refresh_btn):
            btn.setVisible(False)
        if not self._is_missing():
            has_id = bool(meta.get("package_id"))
            if self._is_uploading():
                pass                      # 上传中：什么都不给点
            elif self._failed():
                self.retry_btn.setVisible(True)
                # 失败态仍然给"查看模型"：包多半已经在工作区里了（只是导入
                # 挂了），用户常需要先看清里面有什么再决定怎么办。
                self.view_btn.setVisible(has_id)
            elif self._importing():
                self.cancel_btn.setVisible(True)
                self.view_btn.setVisible(has_id)
            elif self._state() == "awaiting_user":
                detection = meta.get("detection") or {}
                roots = detection.get("kit_roots") or detection.get("kit_root_candidates") or []
                self.resume_btn.setVisible(
                    has_id and bool(detection.get("kit_root_ambiguous")) and len(roots) > 1)
                self.view_btn.setVisible(has_id)
            else:
                # 待导入 / 已就绪 / 待验证 / 已取消 —— 导入是幂等的，
                # 「解压并导入」在所有这些状态下都是有效且必要的下一步
                self.import_btn.setVisible(True)
                self.view_btn.setVisible(has_id)
            # 「在 ADS 元件列表中打开」：只在**库确实已挂接**且包是 Design Kit
            # 类的待验证/已就绪状态才出现 —— 其它状态下点它没有真实原生入口可进，
            # 露出来只会误导用户。
            self.open_btn.setVisible(self._can_open())
            if not self._importing() and not self._is_uploading():
                self.refresh_btn.setVisible(True)

        # ---- 详情区 ----
        self.body.setVisible(self.toggle.isChecked())
        if not self.toggle.isChecked():
            return
        pal = self._pal
        workspace = str(meta.get("workspace") or "（未记录，通常是上传时所在的工作区）")
        self._line(0, f"工作区：{workspace}")
        vendor = meta.get("vendor") or "包内未标注"
        version = meta.get("version") or "包内未标注"
        self._line(1, f"类型：{meta.get('package_kind_label') or '未识别'}"
                      f"　厂商：{vendor}　版本：{version}")
        if self._is_missing():
            self._line(2, f"资产已不可用：{meta.get('missing_reason') or ''}"
                          "　这条附件只保留引用，模型资产不在当前工作区里。",
                       pal["error"])
        elif self._is_uploading():
            self._line(2, f"状态：上传中 {int(meta.get('upload_percent') or 0)}%"
                          "（上传只保存文件并检查包结构，不解压、不挂库）")
        else:
            self._line(2, f"状态：{state_label(meta)}（{self._state() or '未知'}）"
                          + (f"　型号：{meta['model_count']} 个"
                             if meta.get("model_count") else ""))
        detection = meta.get("detection") or {}
        evidence = [str(x) for x in (detection.get("evidence") or []) if str(x).strip()]
        kits = [str(x) for x in (detection.get("kit_roots") or []) if str(x).strip()]
        if evidence or kits:
            self._line(3, "识别依据：" + "；".join(evidence[:3])
                       + (f"　套件根：{'、'.join(kits[:3])}" if kits else ""))
        else:
            self._line(3, "")
        message = str(meta.get("message") or "").strip()
        self._line(4, message)
        error = str(meta.get("error") or meta.get("last_error") or "").strip()
        self._line(5, f"失败原因：{error}" if error else "", pal["error"])
        # 原生列表打开结果的状态行（后端 POST /models/open 的结构化证据）。
        # 只如实转述，unknown 绝不说成成功。
        self._line(6, _native_text(meta), pal["text"])

        text = _models_text(meta)
        self.models_view.setPlainText(text)
        self.models_view.setVisible(bool(text))

    # ---------------------------------------------------------------- 自适应
    def reflow(self, avail_w: int, item):
        width = max(avail_w - U.P("xl") * 2, U.px(120))
        # 注意用 isHidden() 而不是 isVisible()：卡片挂在未 show 的列表项里，
        # isVisible() 会因为祖先不可见而恒为 False（Qt 的可见性是继承的）。
        # isHidden() 才是"我们自己有没有显式藏它"。
        # 头部：折叠头吃掉剩余宽度，动作按钮按各自 sizeHint 排。
        btns = [b for b in (self.import_btn, self.open_btn, self.view_btn,
                            self.cancel_btn, self.resume_btn, self.retry_btn,
                            self.refresh_btn) if not b.isHidden()]
        btn_w = sum(b.sizeHint().width() for b in btns) + U.P("sm") * len(btns)
        self.toggle.setFixedWidth(max(width - btn_w, U.px(60)))
        head_h = max(self.toggle.sizeHint().height(),
                     max((b.sizeHint().height() for b in btns), default=0))

        height = head_h + U.P("xs") * 2
        if not self.body.isHidden():
            content_w = max(width - U.P("sm") * 2, U.px(60))
            pad_v = U.P("xs")
            for label in self._labels:
                if label.isHidden():
                    continue
                text_h = _measure(label.text(), label.font(),
                                  max(content_w - U.P("md"), U.px(40)))
                label.setFixedWidth(content_w)
                label.setFixedHeight(text_h + pad_v)
                height += text_h + pad_v + U.P("xs")
            if not self.models_view.isHidden():
                text_h = _measure(self.models_view.toPlainText(),
                                  self.models_view.font(),
                                  max(content_w - U.P("md"), U.px(40)))
                self.models_view.setFixedWidth(content_w)
                # 上限封顶：8000 个型号的包不能把会话区撑到几千像素高
                self.models_view.setFixedHeight(
                    min(U.px(_MODELS_VIEW_MAX_H),
                        max(U.px(50), text_h + U.P("sm") * 2 + U.px(8)))
                )
                height += self.models_view.height() + U.P("xs")
        item.setSizeHint(QSize(avail_w, max(height, U.px(40))))


def _button_css(pal: dict, kind: str = "soft") -> str:
    """圆润小按钮（与 panel._button_css 同款，只是复制一份避免反向 import）。"""
    pad_v, pad_h = U.P("xs"), U.P("md")
    radius = U.R("pill") if kind != "ghost" else U.R("md")
    if kind == "primary":
        return (
            f"QPushButton{{background:{pal['accent']}; color:#ffffff; border:none;"
            f" border-radius:{radius}px; padding:{pad_v}px {pad_h}px;"
            f" font-size:{U.fs('tiny')}px;{U.font_css()}}}"
            f"QPushButton:hover{{background:{pal['accent_hover']};}}"
        )
    if kind == "soft":
        return (
            f"QPushButton{{background:{pal['chip_bg']}; color:{pal['chip_text']};"
            f" border:none; border-radius:{radius}px; padding:{pad_v}px {pad_h}px;"
            f" font-size:{U.fs('tiny')}px;{U.font_css()}}}"
            f"QPushButton:hover{{background:{pal['chip_hover']};}}"
        )
    return (
        f"QPushButton{{background:transparent; color:{pal['subtle']};"
        f" border:none; border-radius:{radius}px; padding:{pad_v}px {pad_h}px;"
        f" font-size:{U.fs('tiny')}px;{U.font_css()}}}"
        f"QPushButton:hover{{background:{pal['hover']};}}"
    )
