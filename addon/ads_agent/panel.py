"""ADS Agent chat panel — docked into the ADS main window as a side panel.

Provider settings: saved-model sidebar and add/edit form, with independent model
connections, API model discovery, and a pinned save-and-apply action.
Chat UI: bubble conversation with light/dark theme, centered welcome state
with suggestion chips, rounded input box with ↑ send button, Enter-to-send.

自适应 & 圆润 (见 uiscale.py):
  所有尺寸/字号/圆角走 uiscale 的令牌，缩放系数跟随宿主字号，可由
  config.ini [ui] scale 覆盖；气泡最大宽度、侧栏宽度、输入框高度、模型清单
  高度都按容器比例计算，面板拖宽拖窄时布局跟着变；圆角统一用 R() 令牌。

All HTTP (backend + SSE) runs in QThread workers; the ADS main thread only
paints widgets.
"""

from __future__ import annotations

import configparser
import json
import os
import threading
import time
import urllib.error
import urllib.request

# Qt 绑定经 qtcompat 选择（ADS 2024/2025 为 PySide2，2026 起为 PySide6，
# 官方证据见 backend/adscompat.py）；跟随宿主进程已加载的绑定，绝不混用。
import qtcompat

QtCore = qtcompat.QtCore()
QtGui = qtcompat.QtGui()
QtWidgets = qtcompat.QtWidgets()

Qt = QtCore.Qt
QThread = QtCore.QThread
Signal = QtCore.Signal
QTimer = QtCore.QTimer
QObject = QtCore.QObject
QRect = QtCore.QRect
QSize = QtCore.QSize
QPoint = QtCore.QPoint

QApplication = QtWidgets.QApplication
QAbstractItemView = QtWidgets.QAbstractItemView
QCheckBox = QtWidgets.QCheckBox
QComboBox = QtWidgets.QComboBox
QDockWidget = QtWidgets.QDockWidget
QFileDialog = QtWidgets.QFileDialog
QFormLayout = QtWidgets.QFormLayout
QFrame = QtWidgets.QFrame
QGridLayout = QtWidgets.QGridLayout
QHBoxLayout = QtWidgets.QHBoxLayout
QLabel = QtWidgets.QLabel
QLineEdit = QtWidgets.QLineEdit
QListWidget = QtWidgets.QListWidget
QListWidgetItem = QtWidgets.QListWidgetItem
QMainWindow = QtWidgets.QMainWindow
QMenu = QtWidgets.QMenu
QScrollArea = QtWidgets.QScrollArea
QStackedWidget = QtWidgets.QStackedWidget
QPlainTextEdit = QtWidgets.QPlainTextEdit
QPushButton = QtWidgets.QPushButton
QSizePolicy = QtWidgets.QSizePolicy
QToolButton = QtWidgets.QToolButton
QVBoxLayout = QtWidgets.QVBoxLayout
QWidget = QtWidgets.QWidget

import uiscale as U
import project_store
from mdplain import to_plain

from result_page import ResultPageRow

# 后端在本机回环上，请求必须**绕过 HTTP 代理**：企业网络常设 HTTP_PROXY，
# urllib 会连 127.0.0.1 的请求也发给代理，于是面板报"后端未启动"（其实是通的）。
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_HERE = os.path.dirname(os.path.abspath(__file__))

# 预设: (API 地址, 默认模型, 获取密钥链接)
PRESETS = {
    "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4.6",
                 "https://open.bigmodel.cn/usercenter/apikeys"),
    "DeepSeek": ("https://api.deepseek.com", "deepseek-chat",
                 "https://platform.deepseek.com/api_keys"),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o",
               "https://platform.openai.com/api-keys"),
    "Ollama (本机)": ("http://127.0.0.1:11434/v1", "", ""),
    "自定义": ("", "", ""),
}

SUGGESTIONS = [
    "当前工作区有哪些设计",
    "读取设计里的 VAR 变量",
    "仿真 S 参数并总结",
    "改偏置电阻再仿真对比",
]

# 配色：在原来的浅/深主题上补齐了"圆润风"需要的面板底色、悬浮色、滚动条色
PALETTES = U.PALETTES

# 气泡最大宽度占可视宽度的比例（窄面板也能占满、宽面板不至于拉太长）
_BUBBLE_RATIO = 0.78
# AI 气泡的宽度比例：上限跟随面板宽度。固定像素上限（旧值 720px）在宽
# 面板里会把长回复压在半幅以内、右侧留一大条空白；用户气泡仍保持紧凑。
_BUBBLE_AI_RATIO = 0.94
_BUBBLE_MIN, _BUBBLE_MAX = 200, 720


def _draw_icon(name: str, color: str, size: int = 16):
    """绘制线性矢量图标：16x16 网格、1.5px 圆头描边，2x 像素密度保证 HiDPI 清晰。

    文字符号（✂ ⧉ ▤ 🧹）在不同系统字体下粗细/缺字都不一致，改用手绘路径。
    """
    QPointF = QtCore.QPointF
    QRectF = QtCore.QRectF
    QColor = QtGui.QColor
    QIcon = QtGui.QIcon
    QPainter = QtGui.QPainter
    QPainterPath = QtGui.QPainterPath
    QPen = QtGui.QPen
    QPixmap = QtGui.QPixmap

    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # All paths use a 16x16 design grid, including larger brand/send icons.
    p.scale(size / 16.0, size / 16.0)
    pen = QPen(QColor(color))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    def line(x1, y1, x2, y2):
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    if name == "cut":            # 剪刀
        p.drawEllipse(QPointF(4.5, 4.5), 2.1, 2.1)
        p.drawEllipse(QPointF(4.5, 11.5), 2.1, 2.1)
        line(6.2, 5.9, 13.5, 13.0)
        line(6.2, 10.1, 13.5, 3.0)
    elif name == "copy":         # 两个错开的圆角矩形
        p.drawRoundedRect(QRectF(2.0, 2.0, 8.2, 8.2), 1.6, 1.6)
        p.drawRoundedRect(QRectF(5.8, 5.8, 8.2, 8.2), 1.6, 1.6)
    elif name == "paste":        # 带夹子的剪贴板
        p.drawRoundedRect(QRectF(3.3, 3.8, 9.4, 10.4), 1.6, 1.6)
        p.drawRoundedRect(QRectF(5.8, 1.8, 4.4, 3.0), 1.0, 1.0)
        line(6.0, 8.6, 10.0, 8.6)
        line(6.0, 11.4, 10.0, 11.4)
    elif name == "trash":        # 垃圾桶（清空会话）
        line(2.3, 4.2, 13.7, 4.2)
        body = QPainterPath()
        body.moveTo(3.3, 4.2)
        body.lineTo(4.1, 12.6)
        body.quadTo(4.25, 13.9, 5.5, 13.9)
        body.lineTo(10.5, 13.9)
        body.quadTo(11.75, 13.9, 11.9, 12.6)
        body.lineTo(12.7, 4.2)
        p.drawPath(body)
        lid = QPainterPath()
        lid.moveTo(6.0, 4.2)
        lid.lineTo(6.0, 3.1)
        lid.quadTo(6.0, 2.1, 7.1, 2.1)
        lid.lineTo(8.9, 2.1)
        lid.quadTo(10.0, 2.1, 10.0, 3.1)
        lid.lineTo(10.0, 4.2)
        p.drawPath(lid)
        line(6.6, 7.2, 6.6, 10.8)
        line(9.4, 7.2, 9.4, 10.8)
    elif name == "stop":         # 实心圆角方块（运行中：点击停止）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(3.6, 3.6, 8.8, 8.8), 1.6, 1.6)
    elif name == "send":         # 向上箭头
        line(8.0, 13.0, 8.0, 3.0)
        head = QPainterPath()
        head.moveTo(3.6, 7.4)
        head.lineTo(8.0, 3.0)
        head.lineTo(12.4, 7.4)
        p.drawPath(head)
    elif name == "chevron_up":
        line(4, 10, 8, 6)
        line(8, 6, 12, 10)
    elif name == "close":
        line(4, 4, 12, 12)
        line(12, 4, 4, 12)
    elif name == "menu":
        for y in (4, 8, 12):
            line(3, y, 13, y)
    elif name == "settings":
        p.drawEllipse(QPointF(8, 8), 4, 4)
        p.drawEllipse(QPointF(8, 8), 1.5, 1.5)
        for x1, y1, x2, y2 in ((8, 1, 8, 3), (8, 13, 8, 15),
                               (1, 8, 3, 8), (13, 8, 15, 8),
                               (3, 3, 4.5, 4.5), (11.5, 11.5, 13, 13),
                               (3, 13, 4.5, 11.5), (11.5, 4.5, 13, 3)):
            line(x1, y1, x2, y2)
    elif name == "sliders":
        for y, x in ((4, 6), (8, 10), (12, 5)):
            line(2, y, x - 1.5, y)
            line(x + 1.5, y, 14, y)
            p.drawEllipse(QPointF(x, y), 1.5, 1.5)
    elif name == "brand":
        # A compact symmetric A beneath two RF broadcast arcs.
        p.drawArc(QRectF(1.5, 1.5, 13, 13), 30 * 16, 120 * 16)
        p.drawArc(QRectF(4, 4, 8, 8), 30 * 16, 120 * 16)
        line(4.5, 13, 8, 6.5)
        line(8, 6.5, 11.5, 13)
        line(6, 10.2, 10, 10.2)
    elif name == "moon":
        path = QPainterPath()
        path.moveTo(10, 2)
        path.cubicTo(1, 1, 1, 14, 10, 14)
        path.cubicTo(13, 14, 15, 11, 14, 9)
        path.cubicTo(8, 11, 5, 5, 10, 2)
        p.drawPath(path)
    elif name == "eye":
        path = QPainterPath()
        path.moveTo(1, 8)
        path.quadTo(8, 0, 15, 8)
        path.quadTo(8, 16, 1, 8)
        p.drawPath(path)
        p.drawEllipse(QPointF(8, 8), 2, 2)
    elif name == "plus":
        line(8, 3, 8, 13)
        line(3, 8, 13, 8)
    elif name == "signal":
        wave = QPainterPath()
        wave.moveTo(1, 8)
        for x, y in ((4, 8), (6, 3), (8.5, 13), (11, 5), (13, 8), (15, 8)):
            wave.lineTo(x, y)
        p.drawPath(wave)
    elif name == "chevron":
        line(5, 6, 8, 9)
        line(8, 9, 11, 6)
    elif name == "back":
        line(3, 8, 13, 8)
        line(3, 8, 7, 4)
        line(3, 8, 7, 12)
    elif name == "collapse":
        for x in (3, 8):
            line(x, 4, x + 4, 8)
            line(x + 4, 8, x, 12)
    elif name == "sun":
        p.drawEllipse(QPointF(8, 8), 3, 3)
        for x1, y1, x2, y2 in ((8, 1, 8, 2.5), (8, 13.5, 8, 15),
                              (1, 8, 2.5, 8), (13.5, 8, 15, 8),
                              (3, 3, 4, 4), (12, 12, 13, 13),
                              (3, 13, 4, 12), (12, 4, 13, 3)):
            line(x1, y1, x2, y2)
    elif name == "chart":
        line(3, 2, 3, 13)
        line(3, 13, 14, 13)
        line(4, 10, 7, 6)
        line(7, 6, 10, 9)
        line(10, 9, 14, 3)
    elif name == "refresh":
        p.drawArc(QRectF(3, 3, 10, 10), 40 * 16, 285 * 16)
        line(13, 2, 13, 6)
        line(9, 6, 13, 6)
    elif name == "folder":
        path = QPainterPath()
        path.moveTo(2, 4)
        path.lineTo(6, 4)
        path.lineTo(7.5, 6)
        path.lineTo(14, 6)
        path.lineTo(14, 13)
        path.lineTo(2, 13)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "code":         # </>（允许执行 Python）
        left = QPainterPath()
        left.moveTo(5.8, 4.6)
        left.lineTo(2.4, 8.0)
        left.lineTo(5.8, 11.4)
        p.drawPath(left)
        right = QPainterPath()
        right.moveTo(10.2, 4.6)
        right.lineTo(13.6, 8.0)
        right.lineTo(10.2, 11.4)
        p.drawPath(right)
    p.end()
    return QIcon(pm)


def _scrollbar_css(pal: dict) -> str:
    """圆角细滚动条（浅/深主题通用）。"""
    w = U.px(9)
    r = max(w // 2, 2)
    return (
        f"QScrollBar:vertical{{background:transparent; width:{w}px;"
        f" margin:{U.P('xs')}px {U.P('xs')}px {U.P('xs')}px 0;}}"
        f"QScrollBar::handle:vertical{{background:{pal['scroll']};"
        f" border-radius:{r}px; min-height:{U.px(30)}px;}}"
        "QScrollBar::handle:vertical:hover{background:%s;}" % pal["subtle"]
        + "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        f"QScrollBar:horizontal{{height:0;}}"
    )


def _button_css(pal: dict, kind: str = "primary") -> str:
    if kind in ('primary', 'soft'):
        return U.action_css(pal, primary=kind == 'primary')
    return (
        f"QPushButton{{background:transparent;color:{pal['subtle']};border:1px solid transparent;"
        f"border-radius:{U.R('sm')}px;padding:{U.px(6)}px {U.px(8)}px;"
        f"font-size:{U.fs('small')}px;{U.font_css()}}}"
        f"QPushButton:hover{{background:{pal['hover']};color:{pal['text']};}}"
        f"QPushButton:focus{{border-color:{pal['accent']};}}"
    )


def _combo_css(pal: dict, radius_token: str = "md") -> str:
    return U.field_css(pal, combo=True)


def _lineedit_css(pal: dict, radius_token: str = "md") -> str:
    return U.field_css(pal)


def _setting(section, key, default):
    parser = configparser.ConfigParser()
    path = os.path.normpath(os.path.join(_HERE, "..", "..", "config.ini"))
    if os.path.exists(path):
        parser.read(path, encoding="utf-8")
    try:
        raw = parser.get(section, key).strip()
        return raw if raw else default
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def _backend_base() -> str:
    host = _setting("backend", "host", "127.0.0.1")
    port = _setting("backend", "port", "8760")
    return f"http://{host}:{port}"


def _auth_header() -> dict:
    """后端接口鉴权头。

    令牌由 backend/ads_auth.py 统一生成/读取，插件侧通过 authbridge 加载
    **同一份实现**，所以这里带上的值就是后端要校验的值。取不到时返回空头，
    让后端回 401，由界面给出可操作的提示（而不是静默失败）。
    """
    try:
        import authbridge

        return {authbridge.header_name(): authbridge.token()}
    except Exception:  # noqa: BLE001 — 令牌取不到也要把请求发出去，由后端拒绝
        return {}


def _unauthorized_hint() -> str:
    return (
        "后端拒绝了本次请求（回环令牌不匹配）。\n"
        "常见原因：后端与插件读到的 config.ini 不是同一个，或其中一方还是旧进程。\n"
        "处理：1) 重启后端（关掉后端窗口后重新运行 start_backend.bat，或重启 ADS）；\n"
        "      2) 确认 config.ini 的 [ads] token 没被手工改过；\n"
        "      3) 需要时运行 python backend/ads_auth.py --rotate 重新生成，然后重启两端。"
    )


# ---------------------------------------------------------------------------
# HTTP workers
# ---------------------------------------------------------------------------

class ChatWorker(QThread):
    event_received = Signal(dict)
    failed = Signal(str)

    def __init__(self, messages: list, allow_python: bool, model: str, parent=None):
        super().__init__(parent)
        self._messages = messages
        self._allow_python = allow_python
        self._model = model
        self._stop = threading.Event()
        # 轮次身份：停止按钮据此让**后端**取消本轮（不再发起新请求/新工具），
        # 而不只是本地退出读取循环
        import uuid as _uuid
        self.turn_id = _uuid.uuid4().hex

    def stop(self):
        self._stop.set()
        # 取消必须传播到后端：本地停读只会让 UI 线程不再收到事件，
        # 后端还在继续跑模型和工具。发完即忘 —— 面板不阻塞等待。
        threading.Thread(target=self._post_cancel, daemon=True).start()

    def _post_cancel(self):
        try:
            req = urllib.request.Request(
                _backend_base() + "/chat/cancel",
                data=json.dumps({"turn_id": self.turn_id}).encode("utf-8"),
                headers={"Content-Type": "application/json", **_auth_header()},
                method="POST",
            )
            with _LOOPBACK.open(req, timeout=10) as resp:
                resp.read()
        except Exception:  # noqa: BLE001 — 后端不在了就无需取消
            pass

    def run(self):
        url = _backend_base() + "/chat"
        body = json.dumps(
            {
                "messages": self._messages,
                "allow_python": self._allow_python,
                "model": self._model,
                "turn_id": self.turn_id,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        headers.update(_auth_header())
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with _LOOPBACK.open(req, timeout=1800) as resp:
                buffer = b""
                while not self._stop.is_set():
                    chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n\n" in buffer:
                        raw, buffer = buffer.split(b"\n\n", 1)
                        for line in raw.split(b"\n"):
                            line = line.strip()
                            if line.startswith(b"data: "):
                                try:
                                    ev = json.loads(line[6:].decode("utf-8"))
                                except json.JSONDecodeError:
                                    continue
                                self.event_received.emit(ev)
                                if ev.get("type") in ("done", "error", "cancelled"):
                                    return
        except urllib.error.HTTPError as e:
            # HTTPError 是 URLError 的子类，必须先单独处理，否则 401 会被
            # 报成"连不上后端"，把排查方向带偏。
            if e.code == 401:
                self.failed.emit(_unauthorized_hint())
            else:
                self.failed.emit(f"后端返回 HTTP {e.code}：{e.reason}")
        except urllib.error.URLError as e:
            self.failed.emit(
                f"无法连接后端 ({url})：{e.reason}\n请先运行 start_backend.bat 启动 Agent 后端。"
            )
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class ConfigWorker(QThread):
    """GET/POST against a backend endpoint without blocking the UI thread."""

    result = Signal(dict)

    def __init__(self, payload: dict | None, path: str = "/config", parent=None,
                 timeout: int = 60):
        super().__init__(parent)
        self._payload = payload  # None -> GET
        self._path = path
        # 设计闭环的接口可能要等一整个仿真（分钟级），所以超时可传
        self._timeout = max(5, int(timeout))

    def run(self):
        url = _backend_base() + self._path
        try:
            if self._payload is None:
                req = urllib.request.Request(url, headers=_auth_header())
            else:
                headers = {"Content-Type": "application/json"}
                headers.update(_auth_header())
                req = urllib.request.Request(
                    url,
                    data=json.dumps(self._payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
            with _LOOPBACK.open(req, timeout=self._timeout) as resp:
                self.result.emit(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if self._path == '/config/model':
                    try:
                        detail = json.loads(e.read().decode('utf-8'))
                    except (ValueError, UnicodeError):
                        detail = {}
                    if detail.get('error') == '模型配置不存在':
                        self.result.emit({'error': '此模型没有保存的连接参数，请重新添加。',
                                          'profile_missing': True})
                        return
                self.result.emit(
                    {"error": "后端版本过旧（缺少该接口），请关闭后端窗口后重新运行 start_backend.bat",
                     "backend_down": True}
                )
            elif e.code == 401:
                self.result.emit({"error": _unauthorized_hint(), "unauthorized": True})
            else:
                self.result.emit({"error": f"后端返回 HTTP {e.code}"})
        except urllib.error.URLError as e:
            self.result.emit({"error": f"后端未启动（{e.reason}）", "backend_down": True})
        except Exception as e:  # noqa: BLE001
            self.result.emit({"error": f"{type(e).__name__}: {e}"})


class _ReviveBridge(QObject):
    """Marshals the launcher callback (worker thread) to the UI thread."""

    done = Signal(bool, str)


# ---------------------------------------------------------------------------
# chat list with real bubble rows
# ---------------------------------------------------------------------------

def _format_stats(stats: dict, reply_text: str) -> str:
    """把后端 done 事件里的用量格式化成「1.1万 Tokens · 67.9 Token/秒 · 模型」。

    服务不返回 usage 时按文本长度估算（CJK 1 字≈1 token，其余 ~4 字符≈1 token），
    数值前加 ≈ 以示区别；速率的分母是本轮各次 LLM 调用耗时之和（不含工具/仿真时间）。
    """
    try:
        secs = float(stats.get("llm_seconds") or 0)
    except (TypeError, ValueError):
        secs = 0.0
    model = str(stats.get("model") or "").strip()
    n = stats.get("completion_tokens")
    approx = False
    try:
        n = int(n) if n is not None else None
    except (TypeError, ValueError):
        n = None
    if n is None:
        cjk = sum(1 for ch in reply_text if ord(ch) > 0x2E7F)
        n = max(1, int(round(cjk + (len(reply_text) - cjk) / 4)))
        approx = True
    if n >= 10000:
        tok = f"{n / 10000:.1f}万"
    elif n >= 1000:
        tok = f"{n / 1000:.1f}k"
    else:
        tok = str(n)
    parts = [f"{'≈' if approx else ''}{tok} Tokens"]
    if secs > 0:
        parts.append(f"{n / secs:.1f} Token/秒")
    if model:
        parts.append(model)
    return " · ".join(parts)


class ChatList(QListWidget):
    """Bubble conversation list; asks the panel to reflow rows on resize."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        panel = self.parent()
        while panel is not None and not isinstance(panel, AgentPanelWidget):
            panel = panel.parentWidget()
        if panel is not None:
            panel.reflow()


_MEASURE_CACHE: dict = {}


def _measure_text(text: str, font, text_w: int) -> tuple:
    """按 QLabel 的实际排版算出 (理想宽度, 换行后高度)。

    QLabel.heightForWidth() 在配了样式表 padding 时会少算一行，这里改用
    QTextDocument 自己排一遍，中文/中英混排的换行都能算准。
    结果带缓存：拖动面板时会反复重排，没必要每帧都重排一遍文本。

    wrap mode 用 WordWrap —— 这是 QLabel(wordWrap=True) 的**实际绘制**行为
    （实测：它并不按 WrapAtWordBoundaryOrAnywhere 排版，无空格长 token 会被
    直接横向裁掉而不是断行）。长 token 的可断行化由 _soft_break_text 负责。
    """
    key = (text, text_w, font.key())
    hit = _MEASURE_CACHE.get(key)
    if hit is not None:
        return hit

    QTextDocument = QtGui.QTextDocument
    QTextOption = QtGui.QTextOption

    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    doc.setDefaultTextOption(opt)
    doc.setPlainText(text)
    doc.setTextWidth(max(text_w, 1))
    result = (int(doc.idealWidth()), int(doc.size().height()))

    if len(_MEASURE_CACHE) > 800:
        _MEASURE_CACHE.clear()
    _MEASURE_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# 无空格长串的可断行化（QLabel 的 WordWrap 不会断无空格 token，只会横裁）
# ---------------------------------------------------------------------------

_ZWSP = "\u200b"  # 零宽空格：不可见，但给 WordWrap 一个断行机会
_SOFT_BREAK_AFTER = set("_\"',./\\:;(){}[]<>-=+*&%$#@!~`|^?")


def _soft_break_text(text: str, font, content_w: int) -> str:
    """有「不可断行段」超过 content_w 时，插入零宽空格制造断行机会。

    只处理真正超限的段：普通消息原样返回（复制出来的文本不受影响）；
    像 ``de_open_design"AI_lib",...`` 这类工具回显先在符号后插 ZWSP，
    若整段连符号都没有（超长纯字母数字），再按宽度每隔若干字符硬插。
    """
    import re

    QFontMetrics = QtGui.QFontMetrics

    fm = QFontMetrics(font)
    limit = max(content_w, 1)

    def _fix(word: str) -> str:
        if fm.horizontalAdvance(word) <= limit:
            return word
        broken = "".join(
            ch + (_ZWSP if ch in _SOFT_BREAK_AFTER else "") for ch in word
        )
        parts = broken.split(_ZWSP)
        fixed: list = []
        for part in parts:
            while fm.horizontalAdvance(part) > limit and len(part) > 1:
                # 二分找放得下的最长前缀
                lo, hi = 1, len(part)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fm.horizontalAdvance(part[:mid]) <= limit:
                        lo = mid
                    else:
                        hi = mid - 1
                fixed.append(part[:lo])
                part = part[lo:]
            fixed.append(part)
        return _ZWSP.join(fixed)

    return "".join(
        seg if seg.isspace() else _fix(seg)
        for seg in re.split(r"(\s+)", text)
    )


class BubbleRow(QWidget):
    """A restrained message row with a small vector assistant mark.

    宽度自适应：气泡最大宽度 = min(可视宽 * 比例, 可视宽 - 头像占位)；
    AI 气泡比例 _BUBBLE_AI_RATIO 随面板宽度伸缩，用户气泡另有 _BUBBLE_MAX
    固定上限保持紧凑；并按 _BUBBLE_MIN 夹紧下限。行高由文本实际排版高度
    算出，不写死像素，
    所以换面板宽度、换字号都不会截断文字或裁掉最后一行。
    """

    def __init__(self, kind: str, text: str, pal: dict, parent=None):
        super().__init__(parent)
        self.kind = kind
        # 气泡是纯文本，LLM 回复里的 Markdown 修饰符号会原样露出 —— 显示前清洗；
        # 只清 assistant（用户输入与面板自身生成的 note/hint 不含 Markdown）
        if kind == "assistant":
            text = to_plain(text)
        self._text = text
        row = QHBoxLayout(self)
        row.setContentsMargins(U.P("xl"), U.P("sm"), U.P("xl"), U.P("sm"))
        row.setSpacing(U.P("md"))

        self._avatar_size = U.px(26)
        self._gutter = (0 if kind == 'user' else self._avatar_size + U.P('md')) + U.P('xl') * 2

        if kind == "user":
            self.label = self._make_label(text, pal["user_bubble"], pal["user_text"])
            row.addStretch(1)
            row.addWidget(self.label)
        elif kind == "assistant":
            row.addWidget(self._make_assistant_mark(pal), 0, Qt.AlignmentFlag.AlignTop)
            self.label = self._make_label(text, pal["ai_bubble"], pal["ai_text"])
            row.addWidget(self.label)
            row.addStretch(1)
        elif kind == "stat":
            # 用量统计行：小字灰、透明底，左缘大致对齐 AI 气泡的文字起点
            self.label = QLabel(text)
            self.label.setWordWrap(True)
            self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            indent = self._avatar_size + U.P("md")
            self.label.setStyleSheet(
                f"color:{pal['subtle']}; background:transparent;"
                f" padding:0 0 0 {indent}px;"
                f" font-size:{U.fs('tiny')}px;{U.font_css()}"
            )
            row.addWidget(self.label, 1)
        else:  # tool / hint / note — full-width compact card
            color = pal["error"] if kind == "note" else pal["subtle"]
            self.label = QLabel(text)
            self.label.setWordWrap(True)
            self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.label.setStyleSheet(
                f"color:{color}; background:{pal['card_bg']};"
                f"border:1px solid {pal['card_border']}; border-radius:{U.R('md')}px;"
                f"padding:{U.P('sm')}px {U.P('lg')}px; font-size:{U.fs('tiny')}px;"
                f"{U.font_css()}"
            )
            row.addWidget(self.label, 1)
        # 纯文本格式：工具调用的 JSON、含 < > 的代码都按字面显示，也避免被当成富文本
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setFont(U.qfont("body" if kind in ("user", "assistant") else "tiny"))

    def set_text(self, text: str):
        """流式输出时就地更新气泡文本（只改文本，重排交给 reflow）。"""
        if self.kind == "assistant":
            text = to_plain(text)
        self._text = text
        self.label.setText(text)

    def _make_assistant_mark(self, pal: dict) -> QLabel:
        size = self._avatar_size
        avatar = QLabel()
        avatar.setFixedSize(size, size)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setFont(U.qfont("tiny", bold=True))
        avatar.setPixmap(_draw_icon('brand', pal['accent'], U.px(18)).pixmap(U.px(18), U.px(18)))
        avatar.setStyleSheet(
            f"background:{pal['accent_soft']};border:none;border-radius:{U.R('sm')}px;"
        )
        return avatar

    @staticmethod
    def _make_label(text: str, bg: str, fg: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        r = U.R("md")
        label.setStyleSheet(
            f"background:{bg}; color:{fg}; border:none;"
            f" border-radius:{r}px;"
            f"padding:{U.P('md')}px {U.P('xl')}px; font-size:{U.fs('body')}px;"
            f"{U.font_css()}"
        )
        return label

    def reflow(self, avail_w: int, item: QListWidgetItem):
        font = self.label.font()
        # 横向余量：实测 QLabel 在样式表 padding 下排版，换行点比
        # QTextDocument/QFontMetrics 量出来的宽 ~3px（内容 67px 时 65px 的
        # 文本都会被挤到下一行），宽度过紧最后一个字就被裁掉
        slack_w = U.px(3)
        slack_h = U.px(2)
        frame = U.px(2)          # 气泡 1px 描边 ×2（用户/AI 气泡都有描边）
        v_margin = U.P("sm")     # 与 __init__ 里 row 的纵向 contentsMargin 一致
        if self.kind in ("user", "assistant"):
            # 先按比例取，再保证既不超出「可视宽 - 头像占位」也不小于可读下限。
            # AI 气泡上限跟随面板宽度（固定像素上限在宽面板里会浪费半幅），
            # 用户气泡保持紧凑的固定上限；两种短回复都按内容收缩。
            room = max(avail_w - self._gutter, U.px(60))
            lo = min(U.px(_BUBBLE_MIN), room)
            if self.kind == "assistant":
                inner = max(lo, min(int(avail_w * _BUBBLE_AI_RATIO), room))
            else:
                hi = min(U.px(_BUBBLE_MAX), room)
                inner = max(lo, min(int(avail_w * _BUBBLE_RATIO), hi))
            pad_h, pad_v = U.P("xl") * 2, U.P("md") * 2
            content_limit = max(inner - pad_h - frame, U.px(40))
            # 无空格超长串（工具回显的 JSON/路径）会被 QLabel 横向裁掉：
            # 仅对这种文本插零宽空格制造断行机会（普通消息保持原样，便于复制）
            text = _soft_break_text(self._text, font, content_limit)
            if text != self.label.text():
                self.label.setText(text)
            ideal_w, _ = _measure_text(text, font, content_limit)
            width = min(inner, ideal_w + pad_h + slack_w + frame)
            width = max(width, U.px(56))
            # 定宽：让 QLabel 的排版宽度和我们量出来的一致
            # （只设 maxWidth 时 Qt 会按 sizeHint 给一个更窄的宽度，导致多出一两行被裁掉）
            self.label.setFixedWidth(width)
            _, h = _measure_text(text, font, max(width - pad_h - slack_w - frame, U.px(40)))
            # 定高：布局按 sizeHint 可能给出比测量值更矮的高度，把最后一行裁掉；
            # 宁可多一点气泡内边距，也不能再裁文字
            self.label.setFixedHeight(h + pad_v + slack_h + frame)
            min_h = self._avatar_size + U.P("sm")
            total = h + pad_v + frame + v_margin * 2 + slack_h
            item.setSizeHint(QSize(avail_w, max(total, min_h)))
        else:
            if self.kind == "stat":
                # 统计行：无卡片内边距，只需算上左缩进
                pad_h, pad_v = self._avatar_size + U.P("md") + U.P("lg"), 0
                min_row = U.px(16)
            else:
                pad_h, pad_v = U.P("lg") * 2, U.P("sm") * 2
                min_row = U.px(30)
            card_w = max(avail_w - U.P("xl") * 2, U.px(120))
            content_limit = max(card_w - pad_h - slack_w, U.px(60))
            text = _soft_break_text(self._text, font, content_limit)
            if text != self.label.text():
                self.label.setText(text)
            self.label.setFixedWidth(card_w)
            _, h = _measure_text(text, font, content_limit)
            self.label.setFixedHeight(h + pad_v + slack_h)
            total = h + pad_v + v_margin * 2 + slack_h
            item.setSizeHint(QSize(avail_w, max(total, min_row)))


class ActivityRow(QWidget):
    """One compact, expandable record of a turn's tool activity."""

    def __init__(self, entry: dict, pal: dict, on_change, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._on_change = on_change
        outer = QVBoxLayout(self)
        outer.setContentsMargins(U.P("xl"), U.P("xs"), U.P("xl"), U.P("xs"))
        outer.setSpacing(0)
        self.toggle = QToolButton()
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(bool(entry.get("expanded")))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle.setStyleSheet(
            f"QToolButton{{text-align:left; color:{pal['subtle']}; background:transparent;"
            f"border:none; border-radius:{U.R('sm')}px;"
            f"padding:{U.px(8)}px {U.px(10)}px; font-size:{U.fs('small')}px;}}"
            f"QToolButton:hover{{background:{pal['hover']};}}" + U.font_css()
        )
        outer.addWidget(self.toggle)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.details.setFont(U.qfont("tiny"))
        # 思考内容的侧边滚动条：常驻显示 + 加粗手柄，让"拖滑块上下翻思考"
        # 一眼可见、一抓就动（默认 as-needed 的细条几乎看不见，2026-09-29
        # 用户反馈想要侧边滑块翻内容）。
        self.details.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        w = U.px(10)
        r = max(w // 2, 2)
        # 注意：自身样式必须是「选择器规则」，不能裸声明后再拼 QScrollBar
        # 规则 —— 混用会让 Qt 整表解析失败并全部回退（实测踩坑）
        self.details.setStyleSheet(
            f"QPlainTextEdit{{color:{pal['text']}; background:{pal['card_bg']};"
            f"border:none; border-left:2px solid {pal['card_border']}; border-radius:{U.R('xs')}px;"
            f"padding:{U.P('sm')}px;{U.font_css()}}}"
            # 滚动条规则写在自身样式表里，优先级盖过从 chat 继承的窄条样式
            f"QScrollBar:vertical{{background:transparent; width:{w}px;"
            f" margin:0;}}"
            f"QScrollBar::handle:vertical{{background:{pal['subtle']};"
            f" border-radius:{r}px; min-height:{U.px(26)}px;}}"
            f"QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed{{"
            f"background:{pal['accent']};}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
            f"QScrollBar:horizontal{{height:0;}}"
        )
        self.details.setPlainText(self._body_text())
        self.details.setVisible(self.toggle.isChecked())
        outer.addWidget(self.details)
        self.toggle.toggled.connect(self._toggle)
        self.refresh()

    def _body_text(self) -> str:
        # 新条目显示思考内容；旧持久化条目（没有 reasoning 字段）回退到
        # 工具事件明细，保证升级后老会话还能展开看
        body = "\n\n".join(self.entry.get("reasoning") or self.entry.get("events") or [])
        return body or "（该模型没有返回思考内容）"

    def refresh(self):
        reasoning = self.entry.get("reasoning") or []
        legacy = self.entry.get("events") or []
        n_tools = int(self.entry.get("tools", 0) or 0)
        elapsed = int(self.entry.get("elapsed", 0))
        timing = f"持续了 {elapsed} 秒" if self.entry.get("complete") else f"已用 {elapsed} 秒"
        bits = [timing]
        if reasoning:
            bits.append(f"{len(reasoning)} 段思路")
        elif legacy:
            bits.append(f"{len(legacy)} 项记录")
        if n_tools:
            bits.append(f"{n_tools} 次工具")
        arrow = "⌄" if self.toggle.isChecked() else "›"
        self._summary_text = "思考 · " + " · ".join(bits) + f"  {arrow}"
        self.toggle.setText(self._summary_text)
        self.toggle.setToolTip(self._summary_text + " · 点击展开或收起")
        # setPlainText 重建文档会把内部滚动条归零 —— 流式思考每 ~120ms
        # 刷一次，用户向上翻看时会被反复弹回顶部（2026-09-29 用户反馈
        # "往下滑会回弹"）。先记录阅读位置，重设后恢复；本就在底部则
        # 继续贴底跟随。
        bar = self.details.verticalScrollBar()
        keep = bar.value() if self.details.isVisible() else None
        at_btm = keep is not None and keep >= bar.maximum() - U.px(8)
        self.details.setPlainText(self._body_text())
        if keep is not None:
            if at_btm:
                bar.setValue(bar.maximum())
            else:
                bar.setValue(min(keep, bar.maximum()))

    def live_append(self):
        """流式思考增量到位后就地刷新（不经由面板全量 rebuild）。

        滚动位置保持逻辑在 refresh()：在底部继续贴底，翻到上方则原位
        保持，不拉扯用户。
        """
        self.refresh()

    def _toggle(self, checked: bool):
        self.entry["expanded"] = checked
        self.details.setVisible(checked)
        self.refresh()
        self._on_change()

    def reflow(self, avail_w: int, item: QListWidgetItem):
        width = max(avail_w - U.P("xl") * 2, U.px(120))
        self.toggle.setFixedWidth(width)
        self.toggle.setText(self.toggle.fontMetrics().elidedText(
            self._summary_text, Qt.TextElideMode.ElideRight, max(1, width - U.px(24))))
        self.details.setFixedWidth(width)
        height = self.toggle.sizeHint().height() + U.P("xs") * 2
        if self.toggle.isChecked():
            # 展开高度按排版后的**像素高度**算：不能用「段落数 × 行高」——
            # 段落数只统计 \n 块，一个长段落折成五六行时高度不够，最后一截
            # 被裁掉并出现内部滚动条（实测踩过）。内边距含样式表 padding、
            # 1px 描边和 QPlainTextEdit 的默认文档边距；竖向滚动条现在常驻
            # 显示（约 10px），折行测量同样要给它留足宽度，宁可多留不能裁字。
            body = self._body_text()
            content_w = max(width - U.P("sm") * 2 - U.px(24), U.px(60))
            _, text_h = _measure_text(body, self.details.font(), content_w)
            self.details.setFixedHeight(
                min(U.px(300), max(U.px(70), text_h + U.P("sm") * 2 + U.px(8)))
            )
            height += self.details.height()
        item.setSizeHint(QSize(avail_w, height))


class _VerticalTabButton(QPushButton):
    """面板折叠后留在侧栏边缘的窄竖条标签：竖排文字，点击即展开面板。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(U.px(30), U.px(160))

    def paintEvent(self, event):  # noqa: N802 — 默认横排文字会被窄宽度裁掉
        QPainter = QtGui.QPainter
        QPalette = QtGui.QPalette
        QStyle = QtWidgets.QStyle
        QStyleOptionButton = QtWidgets.QStyleOptionButton

        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = ""  # 文字由我们旋转 90° 绘制
        painter = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_PushButton, opt, painter, self)
        painter.save()
        painter.setPen(self.palette().color(QPalette.ColorRole.ButtonText))
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(-90)
        painter.drawText(
            QRect(-self.height() // 2, -self.width() // 2, self.height(), self.width()),
            Qt.AlignmentFlag.AlignCenter,
            self.text(),
        )
        painter.restore()


class ChipButton(QPushButton):
    """建议卡片：允许被压缩到比 text 更窄（文字自动省略），避免把整行撑爆。"""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        s = super().minimumSizeHint()
        return QSize(U.px(60), s.height())


class FeedbackLabel(QLabel):
    """Keep empty request feedback out of the form's layout."""
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setVisible(bool(text))

    def setText(self, text):  # noqa: N802
        super().setText(text)
        self.setVisible(bool(text))

    def clear(self):
        self.setText('')


class WelcomeRow(QWidget):
    """Centered empty state: logo + greeting + clickable suggestion chips.

    自适应：建议卡片按可用宽度自动决定 1/2/3 列；行高由内容真实高度算出，
    不再写死 330px，所以面板高度变化时欢迎页始终垂直居中且不被裁切。
    """

    def __init__(self, on_suggest, pal: dict, parent=None):
        super().__init__(parent)
        self._pal = pal
        self._cols = 0
        col = QVBoxLayout(self)
        col.setContentsMargins(U.P("xl"), U.P("xxl"), U.P("xl"), U.P("xl"))
        col.setSpacing(U.P("md"))
        col.addStretch(1)

        self.logo = QLabel()
        mark_color = pal['panel_bg'] if pal == PALETTES['dark'] else '#ffffff'
        self.logo.setPixmap(_draw_icon('brand', mark_color, U.px(32)).pixmap(U.px(32), U.px(32)))
        self.logo.setFixedSize(U.px(52), U.px(52))
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo.setStyleSheet(
            f"background:{pal['accent']};border-radius:{U.R('lg')}px;"
        )
        col.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(U.px(10))

        self.greet = QLabel("开始你的射频设计")
        self.greet.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.greet.setFont(U.qfont("hero", bold=True))
        self.greet.setStyleSheet(
            f"font-size:{U.fs('hero')}px; font-weight:bold; color:{pal['text']};"
            "background:transparent;" + U.font_css()
        )
        col.addWidget(self.greet)

        self.sub = QLabel("读取设计、分析仿真，或一起优化电路。")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(
            f"font-size:{U.fs('small')}px; color:{pal['subtle']};"
            "background:transparent;" + U.font_css()
        )
        col.addWidget(self.sub)

        col.addSpacing(U.P("md"))

        # 建议卡片放进网格：列数随宽度变化（见 reflow）
        self.grid = QGridLayout()
        self.grid.setSpacing(U.P("md"))
        self._chips: list = []
        for index, tip in enumerate(SUGGESTIONS):
            chip = ChipButton(('查看工作区设计', '读取设计变量', '仿真 S 参数', '调整偏置并对比')[index])
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            chip.setStyleSheet(
                f"QPushButton{{background:{pal['chip_bg']}; color:{pal['chip_text']};"
                f"border:1px solid {pal['card_border']}; border-radius:{U.R('sm')}px;"
                f"text-align:left; padding:{U.px(12)}px {U.px(14)}px; font-size:{U.fs('small')}px;"
                f"{U.font_css()}}}"
                f"QPushButton:hover{{background:{pal['chip_hover']};}}"
                f"QPushButton:focus{{border-color:{pal['accent']};}}"
            )
            chip.setIcon(_draw_icon(('folder', 'code', 'chart', 'settings')[index], pal['accent']))
            chip.setIconSize(QSize(U.px(18), U.px(18)))
            chip.clicked.connect(lambda _, t=tip: on_suggest(t))
            chip.setToolTip(tip)
            self._chips.append(chip)
        col.addLayout(self.grid)
        col.addStretch(2)
        self._layout_chips(2)

    # ------------------------------------------------------------------ 自适应
    def _layout_chips(self, columns: int):
        """把建议卡片重新排成 columns 列（宽度不够时自动减列）。"""
        if columns == self._cols:
            return
        self._cols = columns
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self)
        for i, chip in enumerate(self._chips):
            self.grid.addWidget(chip, i // columns, i % columns)

    def reflow(self, avail_w: int, item: QListWidgetItem):
        # 卡片最小舒适宽度 -> 列数；窄面板自动降为 1 列
        usable = max(avail_w - U.P("xl") * 2, U.px(120))
        min_chip = U.px(200)
        columns = max(1, min(len(self._chips), usable // max(min_chip, 1)))
        self._layout_chips(columns)
        item.setSizeHint(QSize(avail_w, max(self.sizeHint().height(), U.px(240))))


# ---------------------------------------------------------------------------
# panel widget
# ---------------------------------------------------------------------------

class AgentPanelWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agentPanelRoot")
        # Paint our theme even when embedded in a differently themed ADS host.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(U.px(330))
        self.history: list = []  # [{role, content}] visible messages
        self.entries: list = []  # [{kind, text}] rendered conversation
        self.dark = False
        self._worker: ChatWorker | None = None
        self._stopping = False  # 用户点了停止键、等待读取线程退出的窗口期
        self._cfg_workers: list = []  # keep refs so running QThreads aren't GC'd
        self._loading_models = False
        self._reviving = False
        self._revive_bridge = None
        self._revive_retry = None
        self._side_wanted = True      # 用户是否想看到侧栏（窄面板会临时隐藏）
        self._compact_projects = False
        # 会话隔离：本轮对话固定的项目名（异步事件据此回写），
        # 以及因为项目被删而丢弃的事件计数
        self._turn_project: str | None = None
        self._dropped_events = 0
        # 长耗时工具的可见状态
        self._tool_name = ""
        self._tool_t0 = 0.0
        self._tool_timer = None
        self._activity_entry = None
        self._activity_t0 = 0.0
        # 思考流式增量的节流刷新：一次思考动辄几千个增量块，逐块重排重绘
        # 会把 GUI 线程打满（实测整面板冻结）——增量只写数据，重排重绘由
        # 这个单发定时器合并到 ~8Hz
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(120)
        self._flush_timer.timeout.connect(self._flush_reasoning)
        self._flush_target = None       # (entry, project)，待刷新的折叠行
        # 正文流式输出的节流刷新（数据层与渲染层分离，与思考增量同一原则）
        self._content_timer = QTimer(self)
        self._content_timer.setSingleShot(True)
        self._content_timer.setInterval(120)
        self._content_timer.timeout.connect(self._flush_content)
        self._stream_entry = None       # 正在流式输出的 assistant 条目
        self._stream_project = None
        self._sim_off_main = True     # 来自后端 /config，用于如实描述仿真期间的表现
        self._sim_timeout = 900       # 来自后端 /config，用于给设计接口留足超时
        # 设计结果页：job_id -> job 字典（内存缓存；磁盘上还有 design_jobs/<id>.json）
        self._jobs: dict = {}
        self._deleted_project = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(U.px(12), U.px(12), U.px(12), U.px(12))
        layout.setSpacing(U.P("md"))

        # ---- top row: projects + brand + theme + settings ----
        # The host title bar owns closing; avoid duplicate hide controls here.
        top = QHBoxLayout()
        top.setSpacing(U.P("sm"))
        self.side_btn = self._round_tool_button("☰", "展开/收起项目列表", checkable=True)
        self.side_btn.setChecked(True)
        self.side_btn.toggled.connect(self._on_side_toggle)
        top.addWidget(self.side_btn)

        # 品牌区：圆角 Logo + 标题/副标题（比单行文字有辨识度）
        self.logo = QLabel()
        self.logo.setFixedSize(U.px(34), U.px(34))
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.logo)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(0)
        self.title = QLabel("ADS Agent")
        self.subtitle = QLabel("射频设计智能助手")
        title_col.addWidget(self.title)
        title_col.addWidget(self.subtitle)
        top.addLayout(title_col, 1)

        self.theme_btn = self._round_tool_button("🌙", "深色 / 浅色主题")
        self.theme_btn.clicked.connect(self._toggle_theme)
        top.addWidget(self.theme_btn)
        self.settings_btn = self._round_tool_button("", "模型与连接设置", checkable=True)
        self.settings_btn.setFixedWidth(U.px(92))
        self.settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_btn.toggled.connect(self._toggle_settings)
        top.addWidget(self.settings_btn)
        layout.addLayout(top)

        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self.conversation_page = QWidget()
        conversation_layout = QVBoxLayout(self.conversation_page)
        conversation_layout.setContentsMargins(0, 0, 0, 0)
        self.pages.addWidget(self.conversation_page)

        # ---- body: project sidebar + chat column ---------------------------
        body = QHBoxLayout()
        body.setSpacing(U.P("md"))
        self._build_sidebar(body)

        self.chat_column = QWidget()
        self.chat_column.setObjectName('agentChatColumn')
        self.chat_column.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right = QVBoxLayout(self.chat_column)
        right.setContentsMargins(U.px(8), U.px(8), U.px(8), U.px(8))
        right.setSpacing(U.P("md"))
        context = QHBoxLayout()
        self.chat_heading = QLabel("对话")
        self.chat_heading.setFont(U.qfont('small', bold=True))
        context.addWidget(self.chat_heading)
        context.addStretch(1)
        self.active_project_label = QLabel()
        context.addWidget(self.active_project_label)
        right.addLayout(context)

        self.chat = ChatList()
        right.addWidget(self.chat, 1)

        self.status = QLabel("就绪")
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        body.addWidget(self.chat_column, 1)
        conversation_layout.addLayout(body, 1)

        # ---- rounded input box -----------------------------------------------
        self.input_frame = QFrame()
        self.input_frame.setObjectName("inputFrame")
        frame_layout = QVBoxLayout(self.input_frame)
        frame_layout.setContentsMargins(U.P("lg"), U.P("md"), U.P("md"), U.P("md"))
        frame_layout.setSpacing(U.P("xs"))

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("描述你的设计问题…")
        self.input.setToolTip("Enter 发送 · Shift+Enter 换行")
        self.input.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.input.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input.setMinimumHeight(U.px(44))
        self.input.setMaximumHeight(U.px(160))
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._fit_input_height)
        frame_layout.addWidget(self.input)

        foot = QHBoxLayout()
        foot.setSpacing(U.P("sm"))
        # 模型选择不放在这里：⚙设置 里已有完整的模型清单，输入栏保持干净
        self.allow_python = QCheckBox("Python")
        self.allow_python.setChecked(True)
        self.allow_python.setToolTip("允许 Agent 在 ADS 进程内执行任意 Python（run_python 工具）")
        foot.addWidget(self.allow_python)
        self.model_shortcut = QPushButton()
        self.model_shortcut.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.model_shortcut.setToolTip("当前模型 · 点击切换")
        self.model_shortcut.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_shortcut.clicked.connect(self._show_input_model_menu)
        foot.addWidget(self.model_shortcut)
        foot.addStretch(1)
        # 图标走 _draw_icon 矢量绘制（见 _apply_theme 的上色），不再用文字符号
        self.cut_btn = self._round_tool_button("", "剪切选中文字 (Ctrl+X)")
        self.copy_btn = self._round_tool_button("", "复制选中文字 (Ctrl+C)")
        self.paste_btn = self._round_tool_button("", "粘贴剪贴板内容 (Ctrl+V)")
        self.cut_btn.setProperty("icon_name", "cut")
        self.copy_btn.setProperty("icon_name", "copy")
        self.paste_btn.setProperty("icon_name", "paste")
        for button, action in ((self.cut_btn, self.input.cut),
                               (self.copy_btn, self.input.copy),
                               (self.paste_btn, self.input.paste)):
            button.clicked.connect(action)
            foot.addWidget(button)
        self.input.copyAvailable.connect(self._update_clipboard_buttons)
        QApplication.clipboard().dataChanged.connect(self._update_clipboard_buttons)
        self._update_clipboard_buttons()
        self.clear_btn = self._round_tool_button("", "清空会话")
        self.clear_btn.setProperty("icon_name", "trash")
        self.clear_btn.clicked.connect(self._on_clear)
        foot.addWidget(self.clear_btn)
        self.send = QToolButton()
        self.send.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.send.setAccessibleName("发送消息")
        self.send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send.setToolTip("发送 (Enter)")
        self.send.clicked.connect(self._on_send_clicked)
        foot.addWidget(self.send)
        frame_layout.addLayout(foot)
        right.addWidget(self.input_frame)
        self.compose_hint = QLabel("Enter 发送 · Shift+Enter 换行")
        self.compose_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.compose_hint)

        # settings box is created after theme so widgets exist for styling
        self._build_settings()
        self._apply_theme()
        self._load_projects_state()
        self.reload_config()
        self._fit_input_height()
        QTimer.singleShot(0, self._adapt_responsive)

    # ------------------------------------------------------------ 小控件工厂
    def _round_tool_button(self, text: str, tip: str, checkable: bool = False) -> QToolButton:
        """图标按钮：正方形 + 大圆角 + 悬浮底色，整体偏圆润。"""
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setAccessibleName(tip)
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setAutoRaise(True)
        btn.setFixedSize(U.px(32), U.px(32))
        btn.setStyleSheet(
            f"QToolButton{{border:1px solid transparent; border-radius:{U.R('sm')}px;"
            f" font-size:{U.fs('body')}px; padding:0;}}" + U.font_css()
        )
        return btn

    # -------------------------------------------------------------- 自适应
    def resizeEvent(self, e):  # noqa: N802 — 面板尺寸变化时重排
        super().resizeEvent(e)
        self._adapt_responsive()
        self.reflow()

    def _adapt_responsive(self):
        """按当前面板尺寸调整侧栏宽度 / 模型清单高度 / 发送键等。"""
        w = self.width()
        # 侧栏宽度：占面板 30%，夹在 [130, 220]（按缩放换算）
        side_w = max(U.px(130), min(int(w * 0.30), U.px(220))) if w else U.px(170)
        self.sidebar.setMaximumWidth(side_w)
        self.sidebar.setMinimumWidth(U.px(110) if w > U.px(560) else 0)
        # 面板太窄时临时收起侧栏，变宽后恢复用户的选择
        too_narrow = 0 < w < U.px(560)
        self.sidebar.setVisible(self._compact_projects if too_narrow else self._side_wanted)
        self.side_btn.blockSignals(True)
        self.side_btn.setChecked(self.sidebar.isVisible())
        self.side_btn.blockSignals(False)
        self.chat_column.setVisible(not (too_narrow and self._compact_projects))
        if too_narrow and self._compact_projects:
            self.sidebar.setMaximumWidth(w)
        self.logo.setVisible(w >= U.px(440))
        self.subtitle.setVisible(w >= U.px(400))
        # 模型清单高度跟着面板高度走，小面板也不会把设置区顶出屏幕
        rows = max(2, min(self.model_list.count(), 3))
        self.model_list.setFixedHeight(U.px(rows * 36 + 12))
        settings_compact = 0 < w < U.px(440)
        self.settings_columns.setDirection(
            QHBoxLayout.Direction.TopToBottom if settings_compact
            else QHBoxLayout.Direction.LeftToRight)
        if settings_compact:
            self.models_sidebar.setMinimumWidth(0)
            self.models_sidebar.setMaximumWidth(16777215)
        else:
            self.models_sidebar.setFixedWidth(max(U.px(160), min(U.px(250), int(w * .30))))
        self.models_sidebar.setMaximumHeight(U.px(152) if settings_compact else 16777215)
        self.model_sidebar_actions.setDirection(QHBoxLayout.Direction.TopToBottom)
        self.saved_model_list.setVisible(not settings_compact)
        self.saved_model_search.setVisible(not settings_compact)
        self.saved_model_selector.setVisible(settings_compact)
        self._reflow_saved_model_rows()
        self._update_project_context()
        # 窄面板：剪掉/复制/粘贴按钮让位（快捷键 Ctrl+X/C/V 仍可用）
        compact = 0 < w < U.px(760)
        for button in (self.cut_btn, self.copy_btn, self.paste_btn):
            button.setVisible(not compact)
        self.allow_python.setVisible(w > U.px(640))
        self._update_model_shortcut()
        # 宽度变了 -> 输入框的内容换行数也变了，重新贴合高度
        self._fit_input_height()

    def _on_side_toggle(self, on: bool):
        if self.width() < U.px(560):
            self._compact_projects = on
        else:
            self._side_wanted = on
        self.sidebar.setVisible(on)
        if self.width() < U.px(560):
            self.sidebar.setMaximumWidth(self.width())
            self.chat_column.setVisible(not on)

    def _fit_input_height(self):
        """输入框随内容长高（1 行矮、多行高，上限 160px）。

        QPlainTextEdit.document().size() 不参与可视宽度换行，这里按块自己算
        行数（含长段落自动折行），面板变窄/变宽时也能正确跟随。
        """
        fm = self.input.fontMetrics()
        avail = max(self.input.viewport().width() - U.P("md"), U.px(60))
        line_h = max(fm.lineSpacing(), 1)
        lines = 0
        block = self.input.document().begin()
        while block.isValid():
            rect = fm.boundingRect(
                QRect(0, 0, avail, 0), Qt.TextFlag.TextWrapAnywhere, block.text() or " "
            )
            lines += max(1, int(round(rect.height() / line_h)))
            block = block.next()
        h = lines * line_h + U.P("md")
        self.input.setFixedHeight(max(U.px(44), min(h, U.px(160))))

    # ---------------------------------------------------------------- projects
    # 会话与设计任务属于**用户数据**，位置由 backend/paths.py 统一决定
    # （%LOCALAPPDATA%\ADSAgent），不在代码目录旁边 —— 见 pathbridge 说明。
    # 用 property 而不是类常量：数据根目录可能被环境变量改掉，首次访问时
    # 才建目录也更稳。
    @property
    def _PROJECTS_FILE(self) -> str:
        return self._user_path("projects.json")

    @property
    def _DESIGN_JOBS(self) -> str:
        return self._user_path("design_jobs")

    @staticmethod
    def _user_path(name: str) -> str:
        try:
            import pathbridge

            p = pathbridge.load()
            p.ensure_data_dirs()
            root = p.data_root()
        except Exception:  # noqa: BLE001 — 拿不到就退回旧布局，不报错、不阻塞
            root = os.path.normpath(os.path.join(_HERE, "..", ".."))
        return os.path.normpath(os.path.join(root, name))

    def _build_sidebar(self, body: QHBoxLayout):
        """Collapsible project list (参考 AI 客户端的项目列表) — 圆角卡片。"""
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebarCard")
        self.sidebar.setMaximumWidth(U.px(170))
        sv = QVBoxLayout(self.sidebar)
        sv.setContentsMargins(U.P("sm"), U.P("sm"), U.P("sm"), U.P("sm"))
        sv.setSpacing(U.P("sm"))

        head = QHBoxLayout()
        head.setSpacing(U.P("xs"))
        self.proj_label = QLabel("项目")
        head.addWidget(self.proj_label)
        head.addStretch(1)
        self.proj_count = QLabel("1")
        head.addWidget(self.proj_count)
        sv.addLayout(head)
        self.proj_add_btn = QPushButton("新建项目")
        self.proj_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.proj_add_btn.setToolTip("创建项目，保存独立对话")
        self.proj_add_btn.clicked.connect(self._new_project)
        sv.addWidget(self.proj_add_btn)
        self.project_search = QLineEdit()
        self.project_search.setPlaceholderText("搜索项目")
        self.project_search.setClearButtonEnabled(True)
        self.project_search.textChanged.connect(self._filter_projects)
        sv.addWidget(self.project_search)

        self.project_list = QListWidget()
        self.project_list.setToolTip("每个项目保存独立的对话记录，点击切换")
        self.project_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.project_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.project_list.setSpacing(U.P("xs"))
        self.project_list.itemSelectionChanged.connect(self._on_project_selected)
        self.project_list.itemClicked.connect(lambda *_: self._on_project_selected())
        sv.addWidget(self.project_list, 1)
        self.proj_del_btn = QPushButton("删除选中项目")
        self.proj_del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.proj_del_btn.clicked.connect(self._delete_project)
        sv.addWidget(self.proj_del_btn)
        self.project_undo = QPushButton("撤销删除")
        self.project_undo.clicked.connect(self._undo_delete_project)
        self.project_undo.setVisible(False)
        sv.addWidget(self.project_undo)

        body.addWidget(self.sidebar)

    def _load_projects_state(self):
        """Load projects.json; fall back to a single default project."""
        self.projects_data = {"active": "", "projects": {}}
        data, restored = project_store.load(self._PROJECTS_FILE)
        if data is not None:
            self.projects_data = data
        if restored:
            self.status.setText("会话文件不可用，已从上一份备份恢复")

        projects = self.projects_data["projects"]
        active = self.projects_data.get("active", "")
        if active not in projects:
            active = next(iter(projects), "默认")
            if active not in projects:
                projects[active] = {"entries": [], "history": []}
            self.projects_data["active"] = active

        self._apply_project(active, create=True)
        self._refresh_project_list()

    def _save_projects(self):
        try:
            project_store.save(self._PROJECTS_FILE, self.projects_data)
        except OSError as e:
            self.status.setText(f"会话保存失败：{e}")

    def _project(self, name: str | None):
        """按名字取项目；已删除的项目返回 None（绝不隐式重建）。"""
        if not name:
            return None
        return self.projects_data["projects"].get(name)

    def _apply_project(self, name: str, create: bool = False) -> bool:
        """Point entries/history at the named project and re-render.

        create=False（默认）时不会新建项目：切换项目、事件回写走的都是这条路，
        这样"已删除的项目"不会被异步事件重新变出来。
        """
        projects = self.projects_data["projects"]
        if name not in projects:
            if not create:
                return False
            projects[name] = {"entries": [], "history": []}
        self.projects_data["active"] = name
        proj = projects[name]
        # live references：往这两个 list 里 append 就等于写进了 projects_data
        self.entries = proj.setdefault("entries", [])
        self.history = proj.setdefault("history", [])
        self._update_project_context()
        self._rebuild()
        return True

    def _append_history(self, project: str | None, message: dict) -> bool:
        """把一条 {role, content} 追加到指定项目的 history。

        项目已被删除时丢弃（返回 False）—— 回合开始时会固定项目名，
        用户中途删掉该项目的情况下，这些历史不该再写回去。
        """
        proj = self._project(project or self.projects_data.get("active"))
        if proj is None:
            return False
        proj.setdefault("history", []).append(message)
        return True

    def _refresh_project_list(self):
        active = self.projects_data.get("active", "")
        bold = U.qfont("small", bold=True)
        plain = U.qfont("small")
        self.project_list.blockSignals(True)
        try:
            self.project_list.clear()
            for name in self.projects_data["projects"]:
                item = QListWidgetItem(name)
                item.setIcon(_draw_icon('folder', self._pal()['subtle']))
                item.setToolTip(name)
                item.setData(Qt.ItemDataRole.UserRole, name)
                item.setFont(bold if name == active else plain)
                self.project_list.addItem(item)
                if name == active:
                    item.setSelected(True)
        finally:
            self.project_list.blockSignals(False)
        self.proj_count.setText(str(len(self.projects_data['projects'])))
        self.proj_del_btn.setEnabled(len(self.projects_data['projects']) > 1)
        self._filter_projects(self.project_search.text())

    def _filter_projects(self, text):
        for i in range(self.project_list.count()):
            item = self.project_list.item(i)
            item.setHidden(text.casefold() not in item.text().casefold())
        self.proj_del_btn.setEnabled(
            len(getattr(self, 'projects_data', {}).get('projects', {})) > 1 and
            any(not item.isHidden() for item in self.project_list.selectedItems()))

    def _update_project_context(self):
        name = getattr(self, 'projects_data', {}).get('active', '默认')
        self.active_project_label.setText(self.active_project_label.fontMetrics().elidedText(
            name, Qt.TextElideMode.ElideRight, max(U.px(60), self.chat_column.width() // 2)))
        self.active_project_label.setToolTip(f"当前项目：{name}")

    def _on_project_selected(self):
        items = self.project_list.selectedItems()
        if not items:
            return
        name = items[0].data(Qt.ItemDataRole.UserRole)
        if name and name != self.projects_data.get("active"):
            self._apply_project(name)
            self._refresh_project_list()
            self._save_projects()
            self.status.setText(f"已切换到项目：{name}")
        if self.width() < U.px(560) and self._compact_projects:
            self.side_btn.setChecked(False)

    def _project_name_dialog(self):
        QInputDialog = QtWidgets.QInputDialog

        dialog = QInputDialog(self)
        dialog.setWindowTitle("新建项目")
        dialog.setLabelText("项目名称\n每个项目保存独立的对话与设计结果。")
        dialog.setOkButtonText("创建项目")
        dialog.setCancelButtonText("取消")
        dialog.setStyleSheet(U.dialog_css(self._pal()))
        dialog.resize(U.px(360), U.px(180))
        return dialog

    def _new_project(self):
        dialog = self._project_name_dialog()
        ok = qtcompat.dialog_exec(dialog)
        name = dialog.textValue()
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.projects_data["projects"]:
            self.cfg_hint.setText(f"项目 {name} 已存在")
            self.status.setText(f"项目 {name} 已存在，请换一个名称")
            return
        self.projects_data["projects"][name] = {"entries": [], "history": []}
        self._apply_project(name, create=True)
        self._refresh_project_list()
        self._save_projects()
        self.status.setText(f"已创建项目：{name}")
        if self._compact_projects:
            self.side_btn.setChecked(False)

    def _delete_project(self):
        items = self.project_list.selectedItems()
        if not items:
            return
        name = items[0].data(Qt.ItemDataRole.UserRole)
        projects = self.projects_data["projects"]
        if len(projects) <= 1:
            self.cfg_hint.setText("至少保留一个项目")
            self.status.setText("至少保留一个项目")
            return
        self._deleted_project = (name, projects[name])
        del projects[name]
        self.project_undo.setVisible(True)
        pending = self._turn_project == name
        if self.projects_data.get("active") == name:
            self._apply_project(next(iter(projects)))
        self._refresh_project_list()
        self._save_projects()
        # 该项目的在途回合不会被"复活"：后续事件会写进一个不存在的项目，
        # 由 _add_entry / _append_history 丢弃并计数。
        self.status.setText(
            f"已删除项目：{name}"
            + ("（该项目仍有回复在途，其内容将被丢弃）" if pending else "")
        )

    def _undo_delete_project(self):
        if self._deleted_project is None:
            return
        name, project = self._deleted_project
        if name in self.projects_data['projects']:
            self.status.setText(f"已有同名项目：{name}，无法恢复")
            return
        self.projects_data['projects'][name] = project
        self._deleted_project = None
        self.project_undo.setVisible(False)
        self._apply_project(name)
        self._refresh_project_list()
        self._save_projects()
        self.status.setText(f"已恢复项目：{name}")

    # ---------------------------------------------------------------- theme
    def _pal(self) -> dict:
        return PALETTES["dark" if self.dark else "light"]

    def _toggle_theme(self):
        self.dark = not self.dark
        self.theme_btn.setText("☀" if self.dark else "🌙")
        self._apply_theme()

    def _apply_theme(self):
        pal = self._pal()
        base = U.font_css()

        # 面板整体：卡片浮在柔和底色上，圆角更立体
        self.setStyleSheet(
            f"QWidget#agentPanelRoot{{background:{pal['header_bg']};{base}}}"
            f"QLabel{{color:{pal['text']};{base}}}"
            f"QToolButton{{color:{pal['subtle']};{base}}}"
            f"QToolButton:hover{{background:{pal['hover']};}}"
            f"QToolButton:focus{{border:1px solid {pal['accent']};}}"
            f"QToolButton:checked{{background:{pal['accent_soft']}; color:{pal['accent']};}}"
        )
        self.chat_column.setStyleSheet(
            f"QWidget#agentChatColumn{{background:{pal['chat_bg']};"
            f"border:1px solid {pal['card_border']};border-radius:{U.R('lg')}px;}}"
        )

        # 会话区：圆角白卡 + 圆角细滚动条
        self.chat.setStyleSheet(
            f"QListWidget{{background:{pal['chat_bg']}; border:none;"
            f" border-radius:0; padding:{U.px(8)}px 0;}}"
            f"QListWidget::item{{border:none;}}"
            + _scrollbar_css(pal)
        )

        # 侧栏：圆角卡片，内部控件透明
        self.sidebar.setStyleSheet(
            f"QWidget#sidebarCard{{background:{pal['sidebar_bg']};"
            f"border:1px solid {pal['card_border']}; border-radius:{U.R('lg')}px;}}"
            f"QListWidget{{background:transparent; border:none; color:{pal['text']};"
            f" font-size:{U.fs('small')}px; padding:{U.P('xs')}px;}}"
            f"QListWidget::item{{border-radius:{U.R('sm')}px; padding:{U.P('sm')}px;}}"
            f"QListWidget::item:hover{{background:{pal['hover']};}}"
            f"QListWidget::item:selected{{background:{pal['accent_soft']};"
            f" color:{pal['accent']};}}"
            + _scrollbar_css(pal)
        )
        self.proj_label.setStyleSheet(
            f"color:{pal['text']}; font-size:{U.fs('small')}px;"
            "background:transparent;" + base
        )
        self.proj_count.setStyleSheet(f"color:{pal['subtle']};font-size:{U.fs('tiny')}px;")
        self.proj_add_btn.setStyleSheet(U.action_css(pal, primary=True))
        self.proj_add_btn.setIcon(_draw_icon('plus', '#ffffff'))
        self.proj_del_btn.setStyleSheet(_button_css(pal, 'ghost'))
        self.project_undo.setStyleSheet(_button_css(pal, 'soft'))
        self.project_search.setStyleSheet(_lineedit_css(pal))
        for hint in (self.active_project_label, self.compose_hint):
            hint.setStyleSheet(f"color:{pal['subtle']};font-size:{U.fs('tiny')}px;background:transparent;")
        self.logo.setStyleSheet(
            f"background:{pal['accent']};"
            f" border-radius:{U.R('md')}px;" + base
        )
        mark_color = pal['panel_bg'] if self.dark else '#ffffff'
        self.logo.setPixmap(_draw_icon('brand', mark_color, U.px(22)).pixmap(U.px(22), U.px(22)))
        self.title.setStyleSheet(
            f"color:{pal['text']}; font-size:{U.fs('title')}px; font-weight:bold;"
            "background:transparent;" + base
        )
        self.subtitle.setStyleSheet(
            f"color:{pal['subtle']}; font-size:{U.fs('tiny')}px;"
            "background:transparent;" + base
        )

        # 输入区：圆角容器 + 圆形发送键
        self.input_frame.setStyleSheet(
            f"QFrame#inputFrame{{background:{pal['input_bg']};"
            f"border:1px solid {pal['input_border']}; border-radius:{U.R('xl')}px;}}"
            f"QFrame#inputFrame[focused=\"true\"]{{border-color:{pal['accent']};}}"
        )
        # 同步控件字体：样式表只管画，fontMetrics() 拿到的是控件字体，两者必须一致
        self.input.setFont(U.qfont("body"))
        self.input.setStyleSheet(
            f"background:transparent; color:{pal['text']}; border:none;"
            f" font-size:{U.fs('body')}px;{base}"
        )
        _send = U.px(36)
        self.send.setFixedSize(_send, _send)
        self.send.setStyleSheet(
            f"QToolButton{{background:{pal['accent']}; color:#ffffff; border:none;padding:0;margin:0;"
            f" border-radius:{U.R('md')}px; font-size:{U.fs('title')}px;"
            " font-weight:bold;}"
            f"QToolButton:hover{{background:{pal['accent_hover']};}}"
            f"QToolButton:disabled{{background:{pal['card_border']}; color:{pal['subtle']};}}"
            # 运行中切换成红色停止键：属性选择器 + _set_run_state() 里的
            # unpolish/polish 触发重算，主题重刷也不会把运行态样式冲掉
            f"QToolButton[busy=\"true\"]{{background:{pal['stop_bg']};}}"
            f"QToolButton[busy=\"true\"]:hover{{background:{pal['stop_bg_hover']};}}"
        )
        # 矢量线条图标按主题着色（深/浅切换时重画）
        for button in (self.cut_btn, self.copy_btn, self.paste_btn, self.clear_btn):
            button.setIcon(_draw_icon(str(button.property("icon_name")), pal["subtle"]))
            button.setIconSize(QSize(U.px(16), U.px(16)))
        self.allow_python.setIcon(_draw_icon("code", pal["subtle"]))
        self.allow_python.setIconSize(QSize(U.px(15), U.px(15)))
        # 矢量线条图标按主题着色（深/浅切换时重画）；发送键随运行态换图标
        busy = self._worker is not None and self._worker.isRunning()
        send_color = '#ffffff' if busy or not self.dark else pal['panel_bg']
        self.send.setIcon(_draw_icon("stop" if busy else "send", send_color, size=U.px(18)))
        self.send.setIconSize(QSize(U.px(18), U.px(18)))
        self.model_combo.setStyleSheet(_combo_css(pal, radius_token="pill"))
        self.allow_python.setStyleSheet(
            f"QCheckBox{{color:{pal['subtle']}; font-size:{U.fs('tiny')}px;"
            f"spacing:{U.px(6)}px;background:transparent;}}" + U.indicator_css(pal) + base
        )
        self.status.setStyleSheet(
            f"color:{pal['subtle']}; font-size:{U.fs('tiny')}px;"
            "background:transparent;" + base
        )
        for button, name in ((self.side_btn, "menu"), (self.settings_btn, "sliders"),
                             (self.settings_close, "back"),
                             (self.theme_btn, "sun" if self.dark else "moon"), (self.key_eye, "eye")):
            button.setText("")
            button.setIcon(_draw_icon(name, pal['text'], U.px(16)))
            button.setIconSize(QSize(U.px(16), U.px(16)))
        self.settings_btn.setText("模型设置")
        self.settings_btn.setAccessibleName("模型设置：配置连接、模型和工具权限")
        self.settings_btn.setStyleSheet(
            f"QToolButton{{color:{pal['text']};background:{pal['card_bg']};"
            f"border:1px solid transparent;border-radius:{U.R('sm')}px;"
            f"padding:0;font-size:{U.fs('small')}px;{base}}}"
            f"QToolButton:hover{{background:{pal['hover']};}}"
            f"QToolButton:checked{{color:{pal['accent']};background:{pal['accent_soft']};}}"
            f"QToolButton:focus{{border-color:{pal['accent']};}}")
        self.settings_close.setText("返回对话")
        self.settings_close.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_close.setFixedWidth(U.px(112))
        self.model_shortcut.setStyleSheet(
            f"QPushButton{{background:{pal['card_bg']};color:{pal['subtle']};border:none;"
            f"border-radius:{U.R('sm')}px;padding:{U.px(6)}px {U.px(10)}px;font-size:{U.fs('small')}px;{base}}}"
            f"QPushButton:hover{{background:{pal['accent_soft']};color:{pal['accent']};}}")
        self.model_shortcut.setIcon(_draw_icon('chevron_up', pal['subtle']))
        self._style_settings(pal)
        _restyle_handle(pal)
        title_bar = getattr(self, '_dock_title_bar', None)
        if title_bar is not None:
            title_bar.restyle(pal)
        self._rebuild()
        if hasattr(self, 'projects_data'):
            self._refresh_project_list()

    # ------------------------------------------------------------ settings
    def _build_settings(self):
        self._profile_request_id = 0
        self._profile_loading = False
        self._editor_model = None
        self._provider_names = {}
        self._provider_groups = []
        self._editing_provider_models = []
        self.settings_page = QWidget()
        page_layout = QVBoxLayout(self.settings_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.settings_heading = QLabel("模型设置")
        self.settings_heading.setFont(U.qfont("title", bold=True))
        header.addWidget(self.settings_heading, 1)
        self.settings_close = self._round_tool_button("", "关闭设置，返回对话 (Esc)")
        self.settings_close.clicked.connect(lambda: self.settings_btn.setChecked(False))
        header.addWidget(self.settings_close)
        page_layout.addLayout(header)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_columns = QHBoxLayout()
        self.settings_columns.setSpacing(U.px(10))
        self.models_sidebar = QWidget()
        self.models_sidebar.setObjectName("modelsSidebar")
        side = QVBoxLayout(self.models_sidebar)
        side.setContentsMargins(U.px(10), U.px(14), U.px(10), U.px(12))
        side.setSpacing(U.px(10))
        self.saved_model_search = QLineEdit()
        self.saved_model_search.setPlaceholderText("搜索供应商 / 模型")
        self.saved_model_search.setAccessibleName("搜索已保存的供应商或模型")
        self.saved_model_search.setClearButtonEnabled(True)
        self.saved_model_search.textChanged.connect(self._filter_saved_models)
        side.addWidget(self.saved_model_search)
        self.saved_models_heading = QLabel("已有模型")
        self.saved_models_heading.setFont(U.qfont("small", bold=True))
        self.model_sidebar_actions = QHBoxLayout()
        self.model_sidebar_actions.setSpacing(U.px(10))
        self.model_sidebar_actions.addWidget(self.saved_models_heading)
        self.add_model_btn = QPushButton("添加供应商")
        self.add_model_btn.setToolTip("新增供应商与模型")
        self.add_model_btn.setAccessibleName("新增供应商与模型")
        self.add_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_model_btn.clicked.connect(self._new_model)
        self.model_sidebar_actions.addWidget(self.add_model_btn)
        side.addLayout(self.model_sidebar_actions)
        self.saved_model_selector = QComboBox()
        self.saved_model_selector.setPlaceholderText("选择已有模型")
        self.saved_model_selector.setAccessibleName("选择已有模型以编辑参数")
        self.saved_model_selector.activated.connect(
            lambda index: self._edit_saved_model(self.saved_model_selector.itemData(index)))
        side.addWidget(self.saved_model_selector)
        self.saved_model_list = QListWidget()
        self.saved_model_list.setFrameShape(QFrame.Shape.NoFrame)
        self.saved_model_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.saved_model_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.saved_model_list.setAccessibleName("已有模型，选择后编辑参数")
        self.saved_model_list.currentItemChanged.connect(
            lambda item, _: self._edit_saved_model(item.data(Qt.ItemDataRole.UserRole)) if item else None)
        self.saved_model_list.itemClicked.connect(
            lambda item: self._edit_saved_model(item.data(Qt.ItemDataRole.UserRole))
            if not self._profile_loading else None)
        side.addWidget(self.saved_model_list, 1)
        self.settings_columns.addWidget(self.models_sidebar)
        self.settings_columns.addWidget(self.settings_scroll, 1)
        page_layout.addLayout(self.settings_columns, 1)
        self.settings_box = QWidget()
        self.settings_box.setObjectName("settingsCard")
        sv = QVBoxLayout(self.settings_box)
        sv.setContentsMargins(U.px(18), U.px(18), U.px(18), U.px(18))
        sv.setSpacing(U.px(10))

        self.model_editor_heading = QLabel("模型参数")
        self.model_editor_heading.setFont(U.qfont("title", bold=True))
        sv.addWidget(self.model_editor_heading)
        self.model_editor_hint = QLabel("选择已有模型进行编辑，或添加新的模型。")
        self.model_editor_hint.setWordWrap(True)
        self.model_editor_hint.hide()
        self.connection_heading = QLabel("连接设置")
        sv.addWidget(self.connection_heading)

        form = QFormLayout()
        form.setHorizontalSpacing(U.P("md"))
        form.setVerticalSpacing(U.px(6))
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(PRESETS))
        self.preset_combo.currentTextChanged.connect(self._on_preset)
        form.addRow("服务类型", self.preset_combo)
        self.provider_name_edit = QLineEdit()
        self.provider_name_edit.setPlaceholderText("例如：我的 DeepSeek 服务")
        self.provider_name_edit.setAccessibleName("供应商名称，显示在左侧列表")
        form.addRow("供应商名称", self.provider_name_edit)
        self.provider_name_edit.textChanged.connect(self._update_provider_heading)

        key_row = QHBoxLayout()
        key_row.setSpacing(U.P("sm"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("输入此模型的 API 密钥")
        key_row.addWidget(self.api_key_edit, 1)
        self.key_eye = self._round_tool_button("👁", "显示/隐藏密钥", checkable=True)
        self.key_eye.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self.key_eye)
        self.get_key_label = QLabel("")
        self.get_key_label.setTextFormat(Qt.TextFormat.RichText)
        self.get_key_label.setOpenExternalLinks(True)
        self.test_btn = QPushButton("检测")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.setToolTip("检测连接，并获取可用模型")
        self.test_btn.clicked.connect(self._test_connection)
        key_row.addWidget(self.test_btn)
        form.addRow("API 密钥", key_row)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com")
        form.addRow("API 地址", self.base_url_edit)
        sv.addLayout(form)
        sv.addWidget(self.get_key_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(U.P("sm"))
        self.save_btn = QPushButton("保存并应用")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.clicked.connect(self._save_settings)
        self.cfg_hint = FeedbackLabel()
        self.cfg_hint.setWordWrap(True)
        btn_row.addStretch(1)
        sv.addWidget(self.cfg_hint)
        self.retry_profile_btn = QPushButton("重新载入参数")
        self.retry_profile_btn.clicked.connect(lambda: self._edit_saved_model(self._editor_model))
        self.retry_profile_btn.hide()
        sv.addWidget(self.retry_profile_btn)

        model_header = QHBoxLayout()
        model_header.setSpacing(U.P("sm"))
        self.model_label = QLabel("模型")
        model_header.addWidget(self.model_label)
        model_header.addStretch(1)
        # 在供应商下添加模型；对话模型由输入栏菜单切换。
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_combo.setMinimumContentsLength(6)
        self.model_combo.addItems(_model_choices())
        self._active_model = _default_model()
        self._set_model_combo_text(self._active_model)
        self.model_combo.setToolTip("选择或输入模型名称，再点击添加")
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.model_combo.setMaximumWidth(U.px(260))
        self.sync_btn = QPushButton("同步模型")
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_btn.setToolTip("从 API 地址拉取最新模型列表")
        self.sync_btn.clicked.connect(self._test_connection)
        model_header.addWidget(self.sync_btn)
        self.models_area = QFrame()
        self.models_area.setObjectName("modelSection")
        models_layout = QVBoxLayout(self.models_area)
        models_layout.setContentsMargins(U.px(12), U.px(12), U.px(12), U.px(12))
        models_layout.setSpacing(U.px(10))
        models_layout.addLayout(model_header)
        add_model_row = QHBoxLayout()
        add_model_row.setSpacing(U.px(6))
        add_model_row.addWidget(self.model_combo, 1)
        self.add_provider_model_btn = QPushButton("添加")
        self.add_provider_model_btn.setMinimumWidth(U.px(64))
        self.add_provider_model_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.add_provider_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_provider_model_btn.setToolTip("将输入的模型加入该供应商")
        self.add_provider_model_btn.clicked.connect(self._add_provider_model)
        add_model_row.addWidget(self.add_provider_model_btn)
        models_layout.addLayout(add_model_row)
        sv.addWidget(self.models_area)
        self.model_combo.setMaximumWidth(16777215)
        self.model_combo.currentTextChanged.connect(self._update_model_shortcut)
        self.model_combo.lineEdit().setPlaceholderText("输入模型名称，如 deepseek-flash")

        self.model_search = QLineEdit()
        self.model_search.setPlaceholderText("搜索模型…")
        self.model_search.setClearButtonEnabled(True)
        self.model_search.textChanged.connect(self._filter_models)
        models_layout.addWidget(self.model_search)

        self.model_list = QListWidget()
        self.model_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.model_list.setSpacing(U.P("xs"))
        self.model_list.setToolTip("选择该供应商的可用模型，保存后可在输入栏切换")
        self.model_list.itemChanged.connect(self._on_model_item_changed)
        self.model_list.setVisible(False)
        self.model_search.setVisible(False)
        models_layout.addWidget(self.model_list)

        self.model_list_hint = QLabel("勾选多个模型并保存，即可在输入栏切换；也可手动添加。")
        self.model_list_hint.setWordWrap(True)
        self.model_list_hint.hide()
        self.permission_heading = QLabel("工具权限")
        sv.addWidget(self.permission_heading)
        self.python_setting = QCheckBox("允许执行 ADS Python")
        self.python_setting.setChecked(self.allow_python.isChecked())
        self.python_setting.setToolTip("启用脚本工具，用于读取和修改 ADS 设计")
        self.python_setting.toggled.connect(self.allow_python.setChecked)
        self.allow_python.toggled.connect(self.python_setting.setChecked)
        sv.addWidget(self.python_setting)

        self.shared_connection_hint = QLabel("同一供应商的模型共用 API 地址和密钥。")
        self.shared_connection_hint.setWordWrap(True)
        sv.addWidget(self.shared_connection_hint)
        sv.addStretch(1)
        self.settings_scroll.setWidget(self.settings_box)
        footer = QHBoxLayout()
        self.settings_save_hint = QLabel("修改后保存并应用")
        self.settings_save_hint.setWordWrap(True)
        self.settings_save_hint.hide()
        footer.addStretch(1)
        self.save_btn.setMinimumWidth(U.px(140))
        self.save_btn.setMaximumWidth(U.px(190))
        footer.addWidget(self.save_btn)
        page_layout.addLayout(footer)
        self.pages.addWidget(self.settings_page)
        self.settings_page.installEventFilter(self)
        QKeySequence = QtGui.QKeySequence
        QShortcut = QtGui.QShortcut if hasattr(QtGui, "QShortcut") else QtWidgets.QShortcut
        self.settings_escape = QShortcut(QKeySequence("Esc"), self)
        self.settings_escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.settings_escape.activated.connect(lambda: self.settings_btn.setChecked(False))
        self.settings_escape.setEnabled(False)
        self._refresh_saved_models(_model_choices())

    def _style_settings(self, pal: dict):
        """设置区：模型侧栏、参数卡片与固定保存操作。"""
        base = U.font_css()
        self.settings_scroll.setStyleSheet(
            f"QScrollArea{{background:transparent; border:none;}}" + _scrollbar_css(pal)
        )
        self.settings_box.setStyleSheet(
            f"QWidget#settingsCard{{background:{pal['panel_bg']};border:1px solid {pal['card_border']};"
            f"border-radius:{U.R('md')}px;}}"
            f"QLabel{{color:{pal['text']}; font-size:{U.fs('small')}px;"
            " background:transparent;" + base + "}"
            f"QToolButton{{color:{pal['text']};}}"
        )
        self.preset_combo.setStyleSheet(_combo_css(pal))
        self.api_key_edit.setStyleSheet(_lineedit_css(pal))
        self.base_url_edit.setStyleSheet(_lineedit_css(pal))
        self.provider_name_edit.setStyleSheet(_lineedit_css(pal))
        self.model_search.setStyleSheet(_lineedit_css(pal, radius_token="pill"))
        self.saved_model_search.setStyleSheet(_lineedit_css(pal))
        self.models_area.setStyleSheet(
            f"QFrame#modelSection{{background:{pal['card_bg']};border:1px solid {pal['card_border']};"
            f"border-radius:{U.R('md')}px;}}")
        self.test_btn.setStyleSheet(_button_css(pal, "soft"))
        self.retry_profile_btn.setStyleSheet(_button_css(pal, "soft"))
        self.save_btn.setStyleSheet(_button_css(pal, "primary"))
        self.sync_btn.setStyleSheet(_button_css(pal, "soft"))
        self.sync_btn.setIcon(_draw_icon('refresh', pal['subtle']))
        self.models_sidebar.setStyleSheet(
            f"QWidget#modelsSidebar{{background:{pal['sidebar_bg']};"
            f"border:1px solid {pal['card_border']};border-radius:{U.R('md')}px;}}"
            f"QLabel{{color:{pal['text']};background:transparent;{base}}}")
        self.add_model_btn.setStyleSheet(_button_css(pal, "soft"))
        self.add_model_btn.setIcon(_draw_icon('plus', pal['accent']))
        self.add_model_btn.setIconSize(QSize(U.px(14), U.px(14)))
        self.add_provider_model_btn.setStyleSheet(_button_css(pal, "soft"))
        self.saved_model_selector.setStyleSheet(_combo_css(pal))
        self.saved_model_list.setStyleSheet(
            f"QListWidget{{background:transparent;border:none;color:{pal['text']};"
            f"font-size:{U.fs('small')}px;{base}}}"
            f"QListWidget::item{{padding:0;"
            f"margin-bottom:{U.px(4)}px;border:1px solid transparent;border-radius:{U.R('sm')}px;}}"
            f"QListWidget::item:hover{{background:{pal['hover']};}}"
            f"QListWidget::item:selected{{background:{pal['hover']};color:{pal['text']};"
            f"border-color:{pal['card_border']};}}" + _scrollbar_css(pal))
        for i in range(self.saved_model_list.count()):
            row = self.saved_model_list.itemWidget(self.saved_model_list.item(i))
            if row:
                for label in row.findChildren(QLabel):
                    if label.objectName() in ('providerLabel', 'modelLabel'):
                        color = pal['subtle'] if label.objectName() == 'modelLabel' else pal['text']
                        label.setStyleSheet(f"color:{color};background:transparent;")
        self.python_setting.setStyleSheet(
            f"QCheckBox{{color:{pal['text']};background:transparent;spacing:{U.px(8)}px;"
            f"font-size:{U.fs('small')}px;{base}}}" + U.indicator_css(pal))
        for section in (self.connection_heading, self.model_label, self.permission_heading):
            section.setStyleSheet(f"color:{pal['text']};font-size:{U.fs('small')}px;font-weight:bold;"
                                 f"padding-top:{U.px(8)}px;")
        for hint in (self.cfg_hint, self.model_list_hint, self.model_editor_hint, self.shared_connection_hint,
                     self.settings_save_hint):
            hint.setStyleSheet(
                f"color:{pal['subtle']}; font-size:{U.fs('tiny')}px;"
                "background:transparent;" + base
            )
        self.get_key_label.setStyleSheet(
            f"font-size:{U.fs('tiny')}px; background:transparent;" + base
        )
        self.model_list.setStyleSheet(
            f"QListWidget{{background:{pal['input_bg']};border:1px solid {pal['card_border']};"
            f" border-radius:{U.R('md')}px; color:{pal['text']};"
            f" font-size:{U.fs('small')}px; padding:{U.P('xs')}px;{base}}}"
            f"QListWidget::item{{border-radius:{U.R('sm')}px; padding:{U.px(6)}px;}}"
            f"QListWidget::item:hover{{background:{pal['hover']};}}"
            f"QListWidget::item:selected{{background:{pal['accent_soft']};}}"
            + U.indicator_css(pal, owner='QListWidget') + _scrollbar_css(pal)
        )

    def _update_provider_heading(self, name):
        self.model_editor_heading.setText(name.strip() or ("新增供应商" if self._editor_model is None else "供应商配置"))
        self.model_editor_heading.setWordWrap(True)

    def _filter_saved_models(self, text):
        query = text.strip().casefold()
        for i in range(self.saved_model_list.count()):
            item = self.saved_model_list.item(i)
            model = item.data(Qt.ItemDataRole.UserRole)
            provider = self._provider_names.get(model, "自定义供应商")
            members = item.data(Qt.ItemDataRole.UserRole + 1) or [model]
            item.setHidden(bool(query) and query not in (provider + ' ' + ' '.join(members)).casefold())

    def _ingest_provider_groups(self, data):
        if 'provider_groups' in data:
            self._provider_groups = data['provider_groups'] or []

    def _provider_models_for(self, model):
        return next((list(g['models']) for g in self._provider_groups if model in g['models']), [model] if model else [])

    def _refresh_saved_models(self, models, labels=None):
        self._provider_names.update(labels or {})
        self._saved_models = list(dict.fromkeys(str(m) for m in models if str(m).strip()))
        active = self.model_combo.currentText().strip()
        self.saved_model_list.blockSignals(True)
        self.saved_model_list.clear()
        self.saved_model_selector.blockSignals(True)
        self.saved_model_selector.clear()
        groups = [dict(g) for g in self._provider_groups if any(n in self._saved_models for n in g['models'])]
        grouped_names = {n for g in groups for n in g['models']}
        groups += [dict(model=n, models=[n]) for n in self._saved_models if n not in grouped_names]
        for group in groups:
            members = group['models']
            name = active if active in members else group['model']
            group['model'] = name
            self.saved_model_selector.addItem(self._provider_names.get(name, "自定义供应商") + f" · {len(members)} 个模型", name)
        self.saved_model_selector.setCurrentIndex(self.saved_model_selector.findData(active))
        self.saved_model_selector.blockSignals(False)
        for group in groups:
            name, members = group['model'], group['models']
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setData(Qt.ItemDataRole.UserRole + 1, members)
            provider = self._provider_names.get(name, "自定义供应商")
            item.setToolTip(provider + " · " + name + (" · 当前使用" if name == self._active_model else " · 编辑参数"))
            self.saved_model_list.addItem(item)
            row = QWidget()
            row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(U.px(8), U.px(8), U.px(8), U.px(8))
            row_layout.setSpacing(U.px(8))
            avatar = QLabel(provider[:1].upper())
            avatar.setObjectName('providerAvatar')
            avatar.setFixedSize(U.px(28), U.px(28))
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            colors = ('#4978c7', '#8562b1', '#258979', '#b47548', '#986377')
            color = colors[sum(ord(char) for char in provider) % len(colors)]
            avatar.setStyleSheet(f"background:{color};color:white;border-radius:{U.px(8)}px;font-weight:bold;")
            row_layout.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            texts = QVBoxLayout()
            texts.setSpacing(U.px(3))
            provider_label = QLabel(provider)
            provider_label.setObjectName('providerLabel')
            provider_label.setFont(U.qfont("small", bold=True))
            model_label = QLabel(f"{len(members)} 个模型")
            model_label.setObjectName('modelLabel')
            model_label.setFont(U.qfont("tiny"))
            for label in (provider_label, model_label):
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                text_color = self._pal()['subtle'] if label is model_label else self._pal()['text']
                label.setStyleSheet(f"color:{text_color};background:transparent;")
                texts.addWidget(label)
            row_layout.addLayout(texts, 1)
            if self._active_model in members:
                marker = QLabel('●')
                marker.setObjectName('activeMarker')
                marker.setToolTip('当前对话使用的模型')
                marker.setFixedWidth(U.px(8))
                marker.setStyleSheet(f"color:{self._pal()['accent']};background:transparent;font-size:{U.fs('tiny')}px;")
                row_layout.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)
            self.saved_model_list.setItemWidget(item, row)
            if name == active:
                self.saved_model_list.setCurrentItem(item)
        self.saved_model_list.blockSignals(False)
        self.saved_models_heading.setText(f"供应商 · {len(groups)}")
        self._reflow_saved_model_rows()
        self._filter_saved_models(self.saved_model_search.text())

    def _reflow_saved_model_rows(self):
        available = max(U.px(50), self.saved_model_list.viewport().width() - U.px(64))
        for i in range(self.saved_model_list.count()):
            item = self.saved_model_list.item(i)
            row = self.saved_model_list.itemWidget(item)
            if row:
                labels = [label for label in row.findChildren(QLabel)
                          if label.objectName() in ('providerLabel', 'modelLabel')]
                height = 0
                for label in labels:
                    label_height = max(label.fontMetrics().height(), label.heightForWidth(available))
                    label.setFixedHeight(label_height)
                    height += label_height
                item.setSizeHint(QSize(0, max(U.px(28), height) + U.px(24)))

    def _edit_saved_model(self, name):
        if not name:
            return
        self._set_model_combo_text(name)
        self.saved_model_selector.setCurrentIndex(self.saved_model_selector.findData(name))
        for i in range(self.saved_model_list.count()):
            if self.saved_model_list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                self.saved_model_list.blockSignals(True)
                self.saved_model_list.setCurrentRow(i)
                self.saved_model_list.blockSignals(False)
                break
        self.model_editor_heading.setText("模型参数")
        self.model_editor_hint.setText("编辑后点击保存并应用，即可使用该模型。")
        self._editor_model = name
        self._profile_request_id += 1
        request_id = self._profile_request_id
        self._reset_key_field()
        self.base_url_edit.clear()
        self.provider_name_edit.clear()
        self._set_editor_provider("")
        self._clear_discovered_models()
        self._set_profile_loading(True)
        self.retry_profile_btn.hide()
        self.cfg_hint.setText("正在载入模型参数…")
        self._spawn_cfg_worker({"model": name}, "/config/model",
                               lambda data: self._on_profile_loaded(data, name, request_id))

    def _set_profile_loading(self, loading):
        self._profile_loading = loading
        for control in (self.save_btn, self.test_btn, self.sync_btn, self.preset_combo,
                        self.base_url_edit, self.api_key_edit, self.model_combo, self.key_eye):
            control.setEnabled(not loading)
        self.provider_name_edit.setEnabled(not loading)

    def _reset_key_field(self):
        self.key_eye.setChecked(False)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.clear()
        self.api_key_edit.setPlaceholderText("输入此模型的 API 密钥")

    def _set_editor_provider(self, address):
        preset = next((name for name, values in PRESETS.items()
                       if values[0] == address and address), "自定义")
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(preset)
        self.preset_combo.blockSignals(False)
        self._update_get_key_link(preset)

    def _clear_discovered_models(self):
        self.model_list.blockSignals(True)
        self.model_list.clear()
        self.model_list.blockSignals(False)
        self.model_list.hide()
        self.model_search.clear()
        self.model_search.hide()
        self.model_list_hint.setText("勾选多个模型并保存，即可在输入栏切换；也可手动添加。")

    def _on_profile_loaded(self, data, name, request_id):
        if request_id != self._profile_request_id or self._editor_model != name:
            return
        self._set_profile_loading(False)
        if "error" in data or data.get("backend_down"):
            self.cfg_hint.setText(data.get("error") or "后端未运行，请启动后端再重新载入。")
            self.retry_profile_btn.show()
            self.save_btn.setEnabled(False)
            return
        self.base_url_edit.setText(data.get("base_url", ""))
        self._set_editor_provider(data.get("base_url", ""))
        self._reset_key_field()
        self.api_key_edit.setText(data.get("api_key", ""))
        provider = data.get("provider_name") or self.preset_combo.currentText()
        self.provider_name_edit.setText(provider)
        self._refresh_saved_models(self._saved_models, {name: provider})
        self._editing_provider_models = list(data.get('models') or self._provider_models_for(name))
        self._populate_model_list(self._editing_provider_models)
        self.cfg_hint.clear()
        self.settings_scroll.verticalScrollBar().setValue(0)

    def _new_model(self):
        self._profile_request_id += 1
        self._editor_model = None
        self._editing_provider_models = []
        self._set_profile_loading(False)
        self._reset_key_field()
        self.base_url_edit.clear()
        self.provider_name_edit.clear()
        self.retry_profile_btn.hide()
        self._set_editor_provider("")
        self._clear_discovered_models()
        self.saved_model_list.blockSignals(True)
        self.saved_model_list.setCurrentRow(-1)
        self.saved_model_list.blockSignals(False)
        self.saved_model_selector.setCurrentIndex(-1)
        self._set_model_combo_text("")
        self.model_editor_heading.setText("新增供应商")
        self.model_editor_hint.setText("填写供应商连接，再添加或同步此 API 下的模型。")
        self.cfg_hint.clear()
        self.provider_name_edit.setFocus()
        self.settings_scroll.verticalScrollBar().setValue(0)

    def _spawn_cfg_worker(self, payload: dict | None, path: str, callback,
                          timeout: int = 60) -> None:
        worker = ConfigWorker(payload, path=path, timeout=timeout)
        worker.result.connect(callback)
        worker.finished.connect(lambda: self._gc_worker(worker))
        self._cfg_workers.append(worker)
        worker.start()

    def _gc_worker(self, worker: ConfigWorker) -> None:
        if worker in self._cfg_workers:
            self._cfg_workers.remove(worker)

    def _set_model_combo_text(self, name: str):
        """设置模型下拉框文本，并把光标拉回开头。

        可编辑 QComboBox 设文本后光标停在末尾，QLineEdit 会滚动到尾部显示
        —— 长模型名就只能看到尾巴（"deepseek-chat" 显示成 "eepseek-chat"）。
        """
        self.model_combo.setCurrentText(name)
        le = self.model_combo.lineEdit()
        if le is not None:
            le.setCursorPosition(0)
            le.deselect()

    def reload_config(self):
        request_id = self._profile_request_id
        self._spawn_cfg_worker(None, "/config",
                               lambda data: self._on_config_loaded(data, request_id))

    def _on_config_loaded(self, data: dict, request_id=None):
        if request_id is not None and request_id != self._profile_request_id:
            if "error" not in data and not data.get("backend_down"):
                self._refresh_saved_models(self._saved_models + (data.get("models") or []), data.get('profile_labels'))
            return
        if data.get("backend_down"):
            self._auto_revive(retry=self.reload_config)
            return
        if "error" in data:
            self.cfg_hint.setText(f"读取配置失败：{data['error']}")
            return
        # 后端告知仿真是否跑在后台线程 —— 状态栏据此如实描述，不夸口
        self._sim_off_main = bool(data.get("sim_off_main_thread", True))
        try:
            self._sim_timeout = int(data.get("sim_timeout") or self._sim_timeout)
        except (TypeError, ValueError):
            pass
        self.base_url_edit.setText(data.get("base_url", ""))
        self._active_model = data.get("model") or self._active_model
        self._ingest_provider_groups(data)
        self._set_model_combo_text(data.get("model") or self.model_combo.currentText())
        self._refresh_saved_models(data.get("models") or [self.model_combo.currentText()], data.get('profile_labels'))
        self._update_model_shortcut()
        self._edit_saved_model(self._active_model)

    def _toggle_settings(self, checked: bool):
        self.key_eye.setChecked(False)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        if checked:
            self._settings_return_focus = QApplication.focusWidget()
        self.pages.setCurrentWidget(self.settings_page if checked else self.conversation_page)
        self.side_btn.setEnabled(not checked)
        self.settings_escape.setEnabled(checked)
        if checked:
            self.provider_name_edit.setFocus()
        else:
            target = getattr(self, "_settings_return_focus", None) or self.input
            target.setFocus()
            QTimer.singleShot(0, self.reflow)

    def _update_model_shortcut(self, *_):
        model = self._active_model or "选择模型"
        controls = U.px(110)
        if not self.allow_python.isHidden():
            controls += U.px(85)
        if not self.cut_btn.isHidden():
            controls += U.px(108)
        self.model_shortcut.setMaximumWidth(max(U.px(64), min(U.px(220), self.input_frame.width() - controls)))
        self.model_shortcut.setText(self.model_shortcut.fontMetrics().elidedText(
            model, Qt.TextElideMode.ElideRight, self.model_shortcut.maximumWidth() - U.px(42)))
        self.model_shortcut.setToolTip(f"当前模型：{model} · 点击切换")

    def _build_input_model_menu(self):
        menu = QMenu(self.model_shortcut)
        pal = self._pal()
        menu.setStyleSheet(
            f"QMenu{{background:{pal['card_bg']};color:{pal['text']};border:1px solid {pal['card_border']};"
            f"padding:{U.px(6)}px;border-radius:{U.R('md')}px;menu-scrollable:1;{U.font_css()}}}"
            f"QMenu::item{{padding:{U.px(9)}px {U.px(22)}px;border-radius:{U.R('sm')}px;}}"
            f"QMenu::item:selected{{background:{pal['hover']};}}")
        menu.addSection(self._provider_names.get(self._active_model, '当前供应商'))
        for model in self._provider_models_for(self._active_model):
            action = menu.addAction(model)
            action.setData(model)
            action.setCheckable(True)
            action.setChecked(model == self._active_model)
            action.triggered.connect(lambda checked=False, name=model: self._switch_input_model(name))
        menu.addSeparator()
        menu.addAction('管理供应商和模型…', lambda: self.settings_btn.setChecked(True))
        return menu

    def _show_input_model_menu(self):
        if getattr(self, '_input_model_menu', None):
            self._input_model_menu.deleteLater()
        menu = self._input_model_menu = self._build_input_model_menu()
        screen = self.model_shortcut.screen().availableGeometry()
        menu.setMaximumHeight(min(U.px(360), int(screen.height() * .65)))
        menu.ensurePolished()
        anchor = self.model_shortcut.mapToGlobal(QPoint(0, 0))
        size = menu.sizeHint()
        x = max(screen.left(), min(anchor.x(), screen.right() - size.width()))
        y = max(screen.top(), anchor.y() - min(size.height(), menu.maximumHeight()) - U.px(6))
        menu.popup(QPoint(x, y))

    def _switch_input_model(self, model):
        if model == self._active_model or model not in self._provider_models_for(self._active_model):
            return
        self.model_shortcut.setEnabled(False)
        self._spawn_cfg_worker({'model': model}, '/config',
                               lambda data: self._on_input_model_switched(data, model))

    def _on_input_model_switched(self, data, model):
        self.model_shortcut.setEnabled(True)
        if 'error' in data or data.get('backend_down'):
            self.status.setText(data.get('error') or '模型切换失败')
            return
        self._active_model = data.get('model') or model
        self._ingest_provider_groups(data)
        self._update_model_shortcut()
        self._refresh_saved_models(data.get('models') or self._saved_models, data.get('profile_labels'))
        self.status.setText('已切换模型：' + self._active_model)

    def _toggle_key_visibility(self, shown: bool):
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )

    def _update_get_key_link(self, preset: str):
        url = PRESETS.get(preset, ("", "", ""))[2]
        self.get_key_label.setText(
            f'<a href="{url}" style="color:{self._pal()["accent"]};">获取密钥</a>' if url else "")

    def _on_preset(self, name: str):
        base, model, _ = PRESETS.get(name, ("", "", ""))
        if base:
            if base != self.base_url_edit.text().strip():
                self._reset_key_field()
            self.base_url_edit.setText(base)
        if model and not self.model_combo.currentText().strip():
            self._set_model_combo_text(model)
        self._update_get_key_link(name)

    def _test_connection(self):
        """Probe {base_url}/models; on success render the checkable model list."""
        if self._profile_loading:
            return
        if not self.base_url_edit.text().strip():
            self.cfg_hint.setText("请先填写供应商的 API 地址。")
            return
        payload = {"base_url": self.base_url_edit.text().strip(),
                   "api_key": self.api_key_edit.text().strip()}
        self.test_btn.setEnabled(False)
        self.sync_btn.setEnabled(False)
        self.cfg_hint.setText("正在连接…")
        request_id = self._profile_request_id
        self._spawn_cfg_worker(payload, "/test_connection",
                               lambda data: self._on_test_result(data, request_id))

    def _on_test_result(self, data: dict, request_id=None):
        if request_id is not None and request_id != self._profile_request_id:
            return
        self.test_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)
        if data.get("backend_down"):
            self._auto_revive(retry=self._test_connection)
            return
        if data.get("ok"):
            models = data.get("models") or []
            current = self.model_combo.currentText().strip()
            # top combo: manual switching
            self.model_combo.clear()
            self.model_combo.addItems(models)
            self._set_model_combo_text(current)
            # checklist: tick to use
            self._populate_model_list(list(dict.fromkeys(self._editing_provider_models + models)))
            self.model_list_hint.setText(
                f"共 {data.get('count', len(models))} 个模型（{data.get('latency_ms', '?')} ms）。"
                "勾选多个模型，保存后在输入栏切换。"
            )
            self.cfg_hint.setText(
                f"连接成功（{data.get('latency_ms', '?')} ms），已同步 {len(models)} 个模型。"
            )
            return
        if data.get("reachable"):
            self.cfg_hint.setText(f"{data.get('error', '接口异常')}——地址可达，但没能取到模型列表。")
        else:
            self.cfg_hint.setText(f"连接失败：{data.get('error', '未知错误')}")

    # ------------------------------------------------- model checklist
    def _populate_model_list(self, models: list):
        self._loading_models = True
        try:
            self.model_list.clear()
            active = self.model_combo.currentText().strip()
            for name in models:
                item = QListWidgetItem(str(name))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if str(name) in self._editing_provider_models else Qt.CheckState.Unchecked
                )
                self.model_list.addItem(item)
        finally:
            self._loading_models = False
        has = bool(models)
        self.model_list.setVisible(has)
        self.model_search.setVisible(has)
        self._mark_active_model()
        self._filter_models(self.model_search.text())
        self._adapt_responsive()

    def _checked_models(self) -> list:
        return [
            self.model_list.item(i).text()
            for i in range(self.model_list.count())
            if self.model_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _on_model_item_changed(self, item: QListWidgetItem):
        if self._loading_models:
            return
        self._editing_provider_models = self._checked_models()
        self.cfg_hint.setText(f"已选择 {len(self._editing_provider_models)} 个模型，保存后可在输入栏切换。")

    def _add_provider_model(self):
        model = self.model_combo.currentText().strip()
        if not model:
            self.cfg_hint.setText('请输入模型名称')
            return
        selected = self._checked_models() if self.model_list.count() else self._editing_provider_models
        self._editing_provider_models = list(dict.fromkeys(selected + [model]))
        choices = [self.model_list.item(i).text() for i in range(self.model_list.count())]
        self._populate_model_list(list(dict.fromkeys(choices + [model])))
        self.cfg_hint.setText('已添加模型，保存后可在输入栏选择。')

    def _apply_model_selection(self, model: str, checked: list):
        payload = {"models": list(dict.fromkeys(self._saved_models + checked))}
        if model:
            payload["model"] = model
        if model not in self._saved_models:
            payload.update(base_url=self.base_url_edit.text().strip(),
                           api_key=self.api_key_edit.text().strip(),
                           provider_name=self.provider_name_edit.text().strip())
        self._reset_key_field()
        self.base_url_edit.clear()
        self._set_editor_provider("")
        self._profile_request_id += 1
        request_id = self._profile_request_id
        self._set_profile_loading(True)
        self.cfg_hint.setText("正在应用模型选择…")
        self._spawn_cfg_worker(payload, "/config",
                               lambda data, m=model: self._on_model_selection_applied(data, m, request_id))

    def _on_model_selection_applied(self, data: dict, model: str, request_id=None):
        same_editor = request_id is None or request_id == self._profile_request_id
        if same_editor:
            self._set_profile_loading(False)
        if data.get("backend_down"):
            if same_editor:
                self._auto_revive()
            return
        if "error" in data:
            if same_editor:
                self.cfg_hint.setText(f"应用失败：{data['error']}")
                self.save_btn.setEnabled(False)
            return
        self._active_model = data.get("model") or model
        self._update_model_shortcut()
        self._refresh_saved_models(data.get("models") or self._saved_models + [model], data.get('profile_labels'))
        if not same_editor:
            return
        self._mark_active_model()
        if model:
            self._edit_saved_model(model)
        else:
            self.cfg_hint.setText("已更新勾选的模型列表")

    def _mark_active_model(self):
        """Bold the checklist row matching the active model."""
        active = self._active_model
        bold = U.qfont("small", bold=True)
        plain = U.qfont("small")
        self._loading_models = True
        try:
            for i in range(self.model_list.count()):
                item = self.model_list.item(i)
                item.setFont(bold if item.text() == active else plain)
                item.setToolTip("当前使用" if item.text() == active else "勾选加入供应商的模型列表")
        finally:
            self._loading_models = False

    def _filter_models(self, text: str):
        text = (text or "").strip().lower()
        for i in range(self.model_list.count()):
            item = self.model_list.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _save_settings(self):
        if self._profile_loading:
            return
        model = self.model_combo.currentText().strip()
        if not model or not self.base_url_edit.text().strip():
            self.cfg_hint.setText("请填写模型名称和 API 地址。")
            return
        models = self._checked_models() if self.model_list.isVisible() else []
        if self.model_list.count():
            if not models:
                self.cfg_hint.setText('请至少勾选一个模型')
                return
            model = self._active_model if self._active_model in models else model if model in models else models[0]
        else:
            models = [model]
        payload = {
            "base_url": self.base_url_edit.text().strip(),
            "model": model,
            "models": list(dict.fromkeys(self._saved_models + models + [model])),
        }
        payload["api_key"] = self.api_key_edit.text().strip()
        payload["provider_name"] = self.provider_name_edit.text().strip() or self.preset_combo.currentText()
        payload['provider_models'] = models
        payload['provider_model'] = self._editor_model
        self.save_btn.setEnabled(False)
        self.cfg_hint.setText("保存中…")
        self._spawn_cfg_worker(payload, "/config",
                               lambda data, p=payload, r=self._profile_request_id:
                               self._on_config_saved(data, p, r))

    def _on_config_saved(self, data: dict, saved_profile=None, request_id=None):
        same_editor = request_id is None or request_id == self._profile_request_id
        self.save_btn.setEnabled(not self._profile_loading)
        if data.get("backend_down"):
            if same_editor:
                self._auto_revive(retry=self._save_settings)
            return
        if "error" in data:
            if same_editor:
                self.cfg_hint.setText(f"保存失败：{data['error']}")
            return
        self._active_model = data.get("model") or (saved_profile or {}).get("model") or self.model_combo.currentText().strip()
        self._ingest_provider_groups(data)
        self._update_model_shortcut()
        self._refresh_saved_models(data.get("models") or self._saved_models + [self._active_model],
                                   data.get('profile_labels') or {self._active_model: (saved_profile or {}).get('provider_name', '')})
        if not same_editor:
            return
        self._editor_model = self._active_model
        self._editing_provider_models = list((saved_profile or {}).get('provider_models') or self._provider_models_for(self._active_model))
        self.key_eye.setChecked(False)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cfg_hint.clear()
        self._update_provider_heading(self.provider_name_edit.text())
        self.model_editor_hint.setText("编辑后点击保存并应用，即可使用该模型。")

    # ------------------------------------------------------- self-healing
    def _auto_revive(self, retry=None):
        """Backend is down: auto-start it (hidden child) and optionally retry."""
        import backend_launcher

        if backend_launcher.backend_alive():
            if retry:
                QTimer.singleShot(0, retry)
            return
        if self._reviving:
            return
        self._reviving = True
        self._revive_retry = retry
        self.cfg_hint.setText("检测到后端未运行，正在自动启动…")
        self._revive_bridge = _ReviveBridge()
        self._revive_bridge.done.connect(self._on_revived)
        backend_launcher.ensure_backend_async(
            lambda ok, msg: self._revive_bridge.done.emit(ok, msg)
        )

    def _on_revived(self, ok: bool, msg: str):
        self._reviving = False
        if ok:
            retry, self._revive_retry = self._revive_retry, None
            self.cfg_hint.setText("后端已自动启动" + ("，正在重试…" if retry else ""))
            if retry:
                QTimer.singleShot(0, retry)
        else:
            self.cfg_hint.setText(f"{msg}（可手动运行 start_backend.bat 查看错误）")

    # ---------------------------------------------------------------- chat
    def _add_entry(self, kind: str, text: str, project: str | None = None,
                   payload: dict | None = None) -> bool:
        """把一条对话条目写进 `project`（默认当前项目）。

        一轮对话在**发送时**就固定了所属项目；如果用户在等待回复期间切到别的
        项目，事件仍然写回原项目，并且只在它仍处于前台时才渲染 ——
        既不会污染当前项目，也不会把回复丢掉。项目已被删除时直接丢弃，
        绝不重建。

        ``payload`` 用于结构化条目（目前是 ``kind="result"`` 的设计结果页）：
        entry 里只存 ``job_id``（轻量、可持久化），完整 job 从内存缓存或
        ``design_jobs/<job_id>.json`` 取 —— 重启 ADS 后仍能重新渲染。
        """
        active = self.projects_data.get("active")
        target = project or active
        proj = self._project(target)
        if proj is None:
            self._dropped_events += 1
            return False

        entries = proj.setdefault("entries", [])
        visible = target == active
        had_entries = bool(entries)
        entry = {"kind": kind, "text": text}
        if payload:
            job_id = str(payload.get("job_id") or "")
            if job_id:
                entry["job_id"] = job_id
                self._jobs[job_id] = payload
        entries.append(entry)

        if visible:
            self.entries = entries          # 保持前台别名指向同一个 list
            if not had_entries:
                # first message: drop the welcome row and render the conversation
                self._rebuild()
            else:
                row = self._make_row(entry)
                item = QListWidgetItem()
                item.setSizeHint(QSize(max(self.chat.viewport().width(), U.px(300)), U.px(46)))
                self.chat.addItem(item)
                self.chat.setItemWidget(item, row)
                self.reflow()
                self.chat.scrollToBottom()
        elif project is not None:
            # 后台项目收到内容：只在状态栏提示，不渲染到当前会话
            label = "设计结果页" if kind == "result" else "新消息"
            self.status.setText(f"项目「{target}」有{label}（切回该项目可查看）")
        self._save_projects()
        return True

    # ------------------------------------------------------- 设计结果页
    def _job_for(self, job_id: str | None):
        """取设计任务：先内存，再读 design_jobs/<id>.json（重启后走这条路）。"""
        if not job_id:
            return None
        job = self._jobs.get(job_id)
        if isinstance(job, dict):
            return job
        path = os.path.join(self._DESIGN_JOBS, f"{job_id}.json")
        try:
            with open(path, encoding="utf-8") as f:
                job = json.load(f)
        except (OSError, ValueError):
            return None
        if isinstance(job, dict):
            self._jobs[job_id] = job
            return job
        return None

    def _make_row(self, entry: dict):
        """按条目类型造控件（结果页是结构化控件，其余是气泡/卡片）。"""
        if entry.get("kind") == "activity":
            return ActivityRow(entry, self._pal(), self._activity_layout_changed)
        if entry.get("kind") == "result":
            job = self._job_for(entry.get("job_id"))
            if job is None:
                return BubbleRow(
                    "note",
                    f"设计结果页数据已丢失（job {entry.get('job_id') or '?'}）——"
                    f"design_jobs/ 下找不到该任务，可让 Agent 重新发布一次。",
                    self._pal(),
                )
            return ResultPageRow(
                job, self._pal(),
                on_open_schematic=self._open_schematic,
                on_resimulate=self._resimulate_job,
                on_refresh=self._refresh_job,
                on_export_full=self._export_full_data,
            )
        row = BubbleRow(entry["kind"], entry["text"], self._pal())
        row.entry = entry           # 流式刷新按 entry 身份找这一行
        return row

    def _clear_chat(self):
        # 原地清空：self.entries 与 projects_data 里那个 list 必须还是同一个对象，
        # 否则清空后新消息会写进 projects_data 里的旧 list，清掉的对话又回来了。
        del self.entries[:]
        self._rebuild()

    def _rebuild(self):
        pal = self._pal()
        self.chat.clear()
        if not self.entries:
            row = WelcomeRow(self._on_suggest, pal)
            item = QListWidgetItem()
            item.setSizeHint(QSize(max(self.chat.viewport().width(), U.px(300)), U.px(330)))
            self.chat.addItem(item)
            self.chat.setItemWidget(item, row)
            self.reflow()
            return
        for entry in self.entries:
            row = self._make_row(entry)
            item = QListWidgetItem()
            item.setSizeHint(QSize(max(self.chat.viewport().width(), U.px(300)), U.px(46)))
            self.chat.addItem(item)
            self.chat.setItemWidget(item, row)
        self.reflow()
        self.chat.scrollToBottom()

    def _on_suggest(self, text: str):
        self.input.setPlainText(text)
        self.input.setFocus()
        self._on_send()

    def reflow(self):
        """Recompute wrapped row heights (on resize / new rows)."""
        avail = max(self.chat.viewport().width() - U.P("xs"), U.px(240))
        for i in range(self.chat.count()):
            item = self.chat.item(i)
            widget = self.chat.itemWidget(item)
            if isinstance(widget, BubbleRow):
                widget.reflow(avail, item)
            elif isinstance(widget, WelcomeRow):
                widget.reflow(avail, item)
            elif isinstance(widget, ResultPageRow):
                widget.reflow(avail, item)
            elif isinstance(widget, ActivityRow):
                widget.reflow(avail, item)

    def _activity_layout_changed(self):
        self.reflow()
        self._save_projects()

    def _update_clipboard_buttons(self, *_):
        selected = bool(self.input.textCursor().hasSelection())
        self.cut_btn.setEnabled(selected)
        self.copy_btn.setEnabled(selected)
        data = QApplication.clipboard().mimeData()
        self.paste_btn.setEnabled(bool(data is not None and data.hasText()))

    def _on_clear(self):
        del self.history[:]
        self._clear_chat()
        self._save_projects()
        self.status.setText("会话已清空")

    def eventFilter(self, obj, event):  # noqa: N802 — Enter-to-send
        QEvent = QtCore.QEvent

        if obj is self.input and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.input_frame.setProperty('focused', event.type() == QEvent.Type.FocusIn)
            style = self.input_frame.style()
            style.unpolish(self.input_frame)
            style.polish(self.input_frame)
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _on_send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        if self._worker is not None and self._worker.isRunning():
            self._add_entry("note", "上一轮还在进行中；可点右下角 ■ 键停止后再发…")
            return
        # 固定本轮所属项目：之后所有异步事件都写回它，而不是"此刻屏幕上那一个"
        project = self.projects_data.get("active")
        self._turn_project = project
        self._activity_entry = None
        self._activity_t0 = 0.0
        self.input.clear()
        self.history.append({"role": "user", "content": text})
        self._add_entry("user", text, project)

        self._worker = ChatWorker(
            list(self.history), self.allow_python.isChecked(), self._active_model
        )
        # 用默认参数把项目名绑进回调 —— 用户中途切换项目不会改变这一轮的目标项目
        self._worker.event_received.connect(lambda ev, p=project: self._on_event(ev, p))
        self._worker.failed.connect(lambda msg, p=project: self._on_failed(msg, p))
        self._worker.finished.connect(lambda p=project: self._on_worker_done(p))
        self._worker.start()
        self._set_run_state(True)
        self.status.setText(f"Agent 工作中（模型: {self._active_model}）…")

    def _on_worker_done(self, project: str | None = None):
        self._finish_activity(project)
        stopped = self._stopping
        self._stopping = False
        self.send.setEnabled(True)
        self._set_run_state(False)
        self._stop_tool_timer()
        self._turn_project = None
        self.status.setText("就绪")
        if stopped:
            self._add_entry("note", "已手动停止本轮回复", project)
            self.status.setText("已停止本轮回复")
        if self._dropped_events:
            self.status.setText(
                f"就绪（已丢弃 {self._dropped_events} 条属于已删除项目的消息）"
            )
            self._dropped_events = 0
        elif project and project != self.projects_data.get("active"):
            self.status.setText(f"项目「{project}」的回复已完成（切回该项目可查看）")
        self._save_projects()

    def _on_send_clicked(self):
        """发送键的点击入口：空闲=发送，运行中=停止本轮（Enter 仍是发送语义）。"""
        if self._worker is not None and self._worker.isRunning():
            self._stop_run()
        else:
            self._on_send()

    def _stop_run(self):
        if self._worker is None or not self._worker.isRunning():
            return
        self._stopping = True
        # 停止 = /chat/cancel：后端立刻不再发起新的模型请求和工具派发；
        # 正在执行的 ADS 工具无法强杀，会在安全边界收尾（读线程随后退出）
        self.status.setText("正在停止后续步骤…")
        self.send.setEnabled(False)
        self._worker.stop()

    def _set_run_state(self, running: bool):
        """运行中把发送键换成红色停止方块，结束后换回向上箭头。"""
        self.send.setProperty("busy", running)
        self.send.setToolTip("停止生成" if running else "发送 (Enter)")
        self.send.setAccessibleName("停止生成" if running else "发送消息")
        color = '#ffffff' if running or not self.dark else self._pal()['panel_bg']
        self.send.setIcon(_draw_icon("stop" if running else "send", color, size=U.px(18)))
        # 属性选择器不会因 setProperty 自动重算，必须手动重新 polish
        style = self.send.style()
        style.unpolish(self.send)
        style.polish(self.send)

    # ------------------------------------------------- 长耗时工具的可见状态
    def _start_tool_timer(self, name: str, project: str | None) -> None:
        """工具执行期间在状态栏显示实时耗时。

        工具调用进行时后端是阻塞等待的，不会再推事件，所以"已经跑了多久"
        只能由界面自己算。这也是用户判断"ADS 还活着吗"的唯一可见信号。
        """
        if project != self.projects_data.get("active"):
            return
        self._tool_name = name
        self._tool_t0 = time.perf_counter()
        if self._tool_timer is None:
            self._tool_timer = QTimer(self)
            self._tool_timer.setInterval(1000)
            self._tool_timer.timeout.connect(self._tick_tool_status)
        self._tool_timer.start()
        self._tick_tool_status()

    def _stop_tool_timer(self) -> None:
        self._tool_name = ""
        if self._tool_timer is not None:
            self._tool_timer.stop()

    def _tick_tool_status(self) -> None:
        if not self._tool_name:
            return
        elapsed = time.perf_counter() - self._tool_t0
        text = f"执行 {self._tool_name} … 已用 {elapsed:.0f}s"
        if self._tool_name in ("run_simulation", "publish_design_result") and elapsed >= 5:
            # 如实说明，不承诺"界面不会卡"：取决于仿真是否跑在后台线程
            text += ("（仿真在后台线程运行，界面可继续操作）" if self._sim_off_main
                     else "（串行模式：仿真期间 ADS 界面会无响应）")
        self.status.setText(text)

    def _ensure_activity(self, project: str | None):
        """确保当前轮的「思考」折叠行存在，返回它；项目已被删除时返回 None。

        折叠行现在只放模型的思考内容（reasoning）；工具调用只累计次数，
        完整的参数与回显写进后台 logs/backend.log，不再刷给用户。
        """
        target = project or self.projects_data.get("active")
        proj = self._project(target)
        if proj is None:
            self._dropped_events += 1
            return None
        entries = proj.setdefault("entries", [])
        entry = self._activity_entry
        if entry is None or entry not in entries:
            if not self._add_entry("activity", "思考", project):
                return None
            entry = entries[-1]
            entry.update(events=[], reasoning=[], tools=0, elapsed=0,
                         complete=False, expanded=False)
            self._activity_entry = entry
            self._activity_t0 = time.perf_counter()
        return entry

    def _refresh_activity(self, entry: dict, project: str | None):
        """重算折叠行耗时并刷新渲染（tool_call 计数 / reasoning 追加共用）。"""
        entry["elapsed"] = max(0, int(time.perf_counter() - self._activity_t0))
        if (project or self.projects_data.get("active")) == self.projects_data.get("active"):
            self._rebuild()
        self._save_projects()

    def _append_reasoning(self, text: str, project: str | None):
        """追加一段模型思考内容（一段 = 一次 LLM 调用的 reasoning，整段到达）。"""
        text = (text or "").strip()
        if not text:
            return
        entry = self._ensure_activity(project)
        if entry is None:
            return
        entry["reasoning"].append(text)
        entry["text"] = f"思考 · {len(entry['reasoning'])} 段"
        self._refresh_activity(entry, project)

    def _append_content_delta(self, delta: str, project: str | None):
        """流式正文增量：只写数据层，渲染交给节流定时器（与思考增量同一原则）。

        已产生设计修改/工具执行的轮次正文也会照常流入 —— 最终 assistant
        事件会把这条气泡替换成完整内容，不会重复追加。
        """
        if not delta:
            return
        if self._stream_entry is None or self._stream_project != project:
            if not self._add_entry("assistant", "", project):
                return
            self._stream_entry = (self._project(project or
                                  self.projects_data.get("active")) or {}
                                  ).get("entries", [])[-1]
            self._stream_project = project
        self._stream_entry["text"] += delta
        if not self._content_timer.isActive():
            self._content_timer.start()

    def _flush_content(self):
        """把攒下的正文增量刷到气泡（节流 ~8Hz），贴底时跟随滚动。"""
        entry = self._stream_entry
        if entry is None:
            return
        if (self._stream_project or self.projects_data.get("active")) !=                 self.projects_data.get("active"):
            return
        bar = self.chat.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - U.px(24)
        for i in range(self.chat.count()):
            item = self.chat.item(i)
            wgt = self.chat.itemWidget(item)
            if getattr(wgt, "entry", None) is entry and hasattr(wgt, "set_text"):
                wgt.set_text(entry["text"])
                avail = max(self.chat.viewport().width() - U.P("xs"), U.px(240))
                wgt.reflow(avail, item)
                if at_bottom:
                    self.chat.scrollToBottom()
                return
        # 行还没渲染出来（首条增量可能早于行创建）：整体重建一次
        self._rebuild()
        self.chat.scrollToBottom()

    def _append_reasoning_delta(self, delta: str, first: bool, project: str | None):
        """流式思考增量：first=True 表示新开一段（一次新的 LLM 调用）。

        只改数据、调度节流刷新，不做任何重排重绘 —— 一次思考有几千个
        增量块，逐块刷新会把 GUI 线程打满，整个面板冻结（实测 bug）。
        """
        if not delta:
            return
        entry = self._ensure_activity(project)
        if entry is None:
            return
        reasoning = entry["reasoning"]
        if first or not reasoning:
            reasoning.append(delta)
        else:
            reasoning[-1] += delta
        entry["text"] = f"思考 · {len(reasoning)} 段"
        self._flush_target = (entry, project)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_reasoning(self):
        """把攒下的思考增量一次性刷到折叠行（节流 ~8Hz）。

        用户若已把聊天区滚离底部，不强行拽回底部；后台项目不刷（切回时
        _rebuild 会呈现完整内容）。不落盘 —— done/error 时的
        _finish_activity 会统一收尾并保存。
        """
        if self._flush_target is None:
            return
        entry, project = self._flush_target
        self._flush_target = None
        if entry is None or entry is not self._activity_entry:
            return
        entry["elapsed"] = max(0, int(time.perf_counter() - self._activity_t0))
        if (project or self.projects_data.get("active")) != self.projects_data.get("active"):
            return
        bar = self.chat.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - U.px(24)
        for i in range(self.chat.count()):
            item = self.chat.item(i)
            wgt = self.chat.itemWidget(item)
            if isinstance(wgt, ActivityRow) and wgt.entry is entry:
                wgt.live_append()
                avail = max(self.chat.viewport().width() - U.P("xs"), U.px(240))
                wgt.reflow(avail, item)
                if at_bottom:
                    self.chat.scrollToBottom()
                return
        self._rebuild()  # 首个增量到达时行可能还没渲染出来（本轮第一条消息）
        self.chat.scrollToBottom()

    def _finish_activity(self, project: str | None):
        entry = self._activity_entry
        if entry is None:
            return
        self._flush_timer.stop()   # 收尾即完整重建，挂起的节流刷新不再需要
        self._flush_target = None
        proj = self._project(project or self.projects_data.get("active"))
        if proj is not None and entry in proj.get("entries", []):
            entry["elapsed"] = max(0, int(time.perf_counter() - self._activity_t0))
            entry["complete"] = True
            if project == self.projects_data.get("active"):
                self._rebuild()
            self._save_projects()
        self._activity_entry = None
        self._activity_t0 = 0.0

    def _on_event(self, ev: dict, project: str | None = None):
        """处理一条 SSE 事件。

        `project` 是**发送这条消息时**所属的项目。用户中途切换项目时，
        事件依旧写回该项目（而不是现在显示的那个），也不会去改当前状态栏。
        """
        t = ev.get("type")
        foreground = project == self.projects_data.get("active")
        if t == "status":
            if foreground:
                self.status.setText(ev.get("text", ""))
        elif t == "assistant":
            text = ev.get("text", "")
            if text:
                if self._stream_entry is not None and self._stream_project == project:
                    # 正文已随 content_delta 流式进入同一条气泡：就地替换为
                    # 完整内容（防止重复追加），并触发一次最终渲染
                    self._stream_entry["text"] = text
                    self._append_history(project, {"role": "assistant", "content": text})
                    self._flush_content()
                else:
                    self._add_entry("assistant", text, project)
                    self._append_history(project, {"role": "assistant", "content": text})
                self._stream_entry = None
                self._stream_project = None
        elif t == "tool_call":
            entry = self._ensure_activity(project)
            if entry is not None:
                entry["tools"] = int(entry.get("tools", 0)) + 1
                self._refresh_activity(entry, project)
            self._start_tool_timer(ev.get("name", ""), project)
        elif t == "reasoning":
            self._append_reasoning(ev.get("text", ""), project)
        elif t == "reasoning_delta":
            self._append_reasoning_delta(ev.get("text", ""), bool(ev.get("first")), project)
        elif t == "content_delta":
            self._append_content_delta(ev.get("text", ""), project)
        elif t == "tool_result":
            self._stop_tool_timer()
        elif t == "notice":
            # 提示类（预算将尽 / 已达上限等）：中性色卡片，不是错误
            self._add_entry("hint", "ℹ️ " + ev.get("text", ""), project)
        elif t == "design_stage":
            # 设计闭环的进度（生成网表 / 仿真 / 读数据 / 评估）
            if foreground:
                self.status.setText(ev.get("text") or "设计流程进行中…")
        elif t == "design_result":
            self._stop_tool_timer()
            job = ev.get("job") or {}
            title = job.get("title") or "设计结果"
            added = self._add_entry("result", title, project, payload=job)
            if added and foreground:
                summary = job.get("summary") or {}
                if job.get("save_errors"):
                    # "算完了"和"存上了"是两回事：保存失败必须让用户看见
                    self.status.setText(
                        "结果已完成，但保存到磁盘失败（重启后可能找不回该结果页）")
                else:
                    self.status.setText(
                        "设计结果页已生成："
                        f"{summary.get('n_passed', 0)}/{summary.get('n_metrics', 0)} 项达标"
                    )
        elif t == "done":
            self._stop_tool_timer()
            self._finish_activity(project)
            stats = ev.get("stats")
            if stats:
                # 在回复下方挂一行「N Tokens · M Token/秒 · 模型」
                self._add_entry(
                    "stat", _format_stats(stats, ev.get("message") or ""), project
                )
            if foreground:
                self.status.setText("完成")
        elif t == "cancelled":
            # 后端已停止本轮后续步骤（停止按钮 → /chat/cancel 的回执）。
            # 文案必须如实：正在执行的操作会在安全边界收尾，不是"一切已中断"。
            self._stream_entry = None
            self._stream_project = None
            self._stop_tool_timer()
            self._finish_activity(project)
            self._add_entry("note", "⏹ " + ev.get(
                "message", "已停止后续步骤"), project)
            if foreground:
                self.status.setText("已停止后续步骤（正在执行的操作会自行收尾）")
        elif t == "error":
            self._stop_tool_timer()
            self._finish_activity(project)
            self._add_entry("note", f"错误：{ev.get('message', '')}", project)

    def _on_failed(self, message: str, project: str | None = None):
        self._stop_tool_timer()
        self._finish_activity(project)
        self._add_entry("note", f"连接失败：{message}", project)
        self._auto_revive()  # auto-start the backend so the next send works

    # ------------------------------------------------- 设计结果页的三个动作
    def _design_timeout(self) -> int:
        """设计接口的超时：一次仿真可能要跑满 sim_timeout，再留点余量。"""
        try:
            return int(self._sim_timeout) + 180
        except (TypeError, ValueError):
            return 1080

    def _open_schematic(self, job: dict) -> None:
        """在 ADS 中打开该设计的原理图（走主线程工具，不在这里碰 ADS API）。"""
        design = job.get("design") or {}
        if not design.get("library") or not design.get("cell"):
            self.status.setText("这条结果页没有记录完整的设计引用，无法打开原理图")
            return
        self.status.setText(f"正在打开 {job.get('design_ref')} …")
        # 带上任务记录的工作区：后端会核对，防止切过工作区后开错同名设计
        self._spawn_cfg_worker(
            {"library": design.get("library"), "cell": design.get("cell"),
             "view": design.get("view") or "schematic",
             "workspace": design.get("workspace") or ""},
            "/design/open_schematic",
            self._on_schematic_opened,
            timeout=120,
        )

    def _on_schematic_opened(self, data: dict) -> None:
        if data.get("error"):
            self.status.setText(f"打开原理图失败：{data['error']}")
            return
        note = (data.get("result") or {}).get("note") or "已请求在 ADS 中打开原理图"
        self.status.setText(note)

    def _resimulate_job(self, job: dict) -> None:
        """重新仿真（结果页的入口）：按原指标与设计引用重跑一遍。"""
        job_id = job.get("job_id")
        if not job_id:
            self.status.setText("这条结果页缺少 job_id，无法重新仿真")
            return
        self.status.setText(f"正在重新仿真 {job.get('design_ref')} …（可能要几分钟）")
        self._spawn_cfg_worker({"job_id": job_id}, "/design/resimulate",
                               lambda data: self._on_job_updated(data, job_id),
                               timeout=self._design_timeout())

    def _refresh_job(self, job: dict) -> None:
        """用已有 .ds 重新评估（不重新仿真），适合排查"数据集更新了但页面没变"。"""
        job_id = job.get("job_id")
        if not job_id:
            self.status.setText("这条结果页缺少 job_id，无法重新评估")
            return
        self.status.setText("正在用已有数据集重新评估…")
        self._spawn_cfg_worker({"job_id": job_id}, "/design/reload",
                               lambda data: self._on_job_updated(data, job_id),
                               timeout=600)

    def _export_full_data(self, job: dict, expr: str) -> None:
        """从原始 .ds 导出该表达式的完整数据（不是显示采样点）。

        后端重新读 .ds 组装 CSV，面板只负责选保存位置和写文件 ——
        完整点数可能上万，绝不加载进 Qt 表格。
        """
        job_id = job.get("job_id")
        if not job_id or not expr:
            self.status.setText("缺少任务或表达式信息，无法导出完整数据")
            return
        from urllib.parse import quote
        path = f"/design/export?id={quote(str(job_id))}&expr={quote(str(expr))}"
        safe = "".join(c for c in str(expr) if c.isalnum() or c in "_-()[],.") or "trace"
        default = f"{job_id}_{safe}.csv"
        target, _flt = QFileDialog.getSaveFileName(self, "导出完整数据", default,
                                                   "CSV (*.csv)")
        if not target:
            return
        self.status.setText("正在从原始数据集读取完整数据…（点数多时需要几秒）")

        def _done(data: dict):
            if data.get("error"):
                self.status.setText(f"导出完整数据失败：{data['error']}")
                return
            csv = data.get("csv") or ""
            try:
                # utf-8-sig：Excel 打开不乱码
                with open(target, "w", encoding="utf-8-sig") as f:
                    f.write(csv)
            except OSError as e:
                self.status.setText(f"写文件失败：{e}")
                return
            self.status.setText(
                f"已导出完整数据 {data.get('n_points', '?')} 点到 {os.path.basename(target)}")

        self._spawn_cfg_worker(None, path, _done, timeout=300)

    def _on_job_updated(self, data: dict, job_id: str) -> None:
        """设计任务更新后：写回**它所属的项目**，只在那个项目处于前台时重绘。"""
        if data.get("error"):
            self.status.setText(f"更新设计结果失败：{data['error']}")
            return
        job = data.get("job") or {}
        if not job.get("job_id"):
            self.status.setText("更新设计结果失败：后端没有返回任务数据")
            return

        self._jobs[job_id] = job
        title = job.get("title") or "设计结果"
        active = self.projects_data.get("active")
        touched_active = False
        owning_project = None
        for name, proj in (self.projects_data.get("projects") or {}).items():
            for entry in (proj.get("entries") or []):
                if entry.get("kind") == "result" and entry.get("job_id") == job_id:
                    entry["text"] = title
                    owning_project = name
                    if name == active:
                        touched_active = True
        if touched_active:
            self._rebuild()
        self._save_projects()

        summary = job.get("summary") or {}
        verdict = job.get("verdict")
        mark = {"pass": "全部达标", "partial": "部分达标",
                "fail": "未达标", "unknown": "无法判定"}.get(verdict, str(verdict))
        text = (f"{job.get('stage_label') or job.get('stage')}：{mark}"
                f"（{summary.get('n_passed', 0)}/{summary.get('n_metrics', 0)}）")
        if job.get("save_errors"):
            # data.get("save_ok")=False 的场景：结果在内存里更新了，但没写进磁盘
            text = "结果已完成，但保存到磁盘失败（本轮更新重启后将丢失）：" + text
        if touched_active:
            self.status.setText(text)
        else:
            # 状态提示显示**项目名**而不是 job_id —— 用户认得项目名
            where = f"项目「{owning_project}」" if owning_project else "结果页"
            self.status.setText(f"{where}的结果页已更新：" + text)


def _model_choices() -> list:
    parser = configparser.ConfigParser()
    path = os.path.normpath(os.path.join(_HERE, "..", "..", "config.ini"))
    if os.path.exists(path):
        parser.read(path, encoding="utf-8")
    models = []
    if parser.has_option("llm", "models"):
        models = [m.strip() for m in parser.get("llm", "models").split(",") if m.strip()]
    default = parser.get("llm", "model", fallback="glm-4.6").strip() or "glm-4.6"
    if default not in models:
        models.insert(0, default)
    return models


def _default_model() -> str:
    parser = configparser.ConfigParser()
    path = os.path.normpath(os.path.join(_HERE, "..", "..", "config.ini"))
    if os.path.exists(path):
        parser.read(path, encoding="utf-8")
    return parser.get("llm", "model", fallback="glm-4.6").strip() or "glm-4.6"


# ---------------------------------------------------------------------------
# docking into the ADS main window sidebar
# ---------------------------------------------------------------------------

_panel: QDockWidget | QMainWindow | None = None
_handle: QDockWidget | None = None      # 折叠后留在侧栏边缘的窄竖条
_handle_tab: _VerticalTabButton | None = None
DOCK_TITLE = "ADS Agent · 射频设计助手"
DOCK_OBJECT_NAME = "AdsAgentDockPanel"
HANDLE_OBJECT_NAME = "AdsAgentDockHandle"


class _DockTitleBar(QWidget):
    """Only gestures starting on the three-dot grip reach Qt's dock machinery."""

    def __init__(self, dock, pal):
        super().__init__(dock)
        self.setObjectName('agentDockTitleBar')
        self.setFixedHeight(U.px(32))
        self.setMinimumWidth(U.px(100))
        self.setMouseTracking(True)
        self._dragging = False
        self._grip_hover = False
        self.setAccessibleName('面板控制栏：仅左侧三圆点区域可拖动')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(U.px(12), U.px(3), U.px(8), U.px(3))
        layout.addSpacing(U.px(58))
        self.compat_label = QLabel(_compat_title_suffix(), self)
        self.compat_label.setVisible(bool(self.compat_label.text()))
        self.compat_label.setWordWrap(True)
        self.compat_label.setToolTip(self.compat_label.text())
        layout.addWidget(self.compat_label)
        layout.addStretch()
        self.collapse_btn = QToolButton(self)
        self.collapse_btn.setFixedSize(U.px(26), U.px(26))
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.setToolTip('折叠到侧栏，点击侧边 ADS Agent 标签可重新展开')
        self.collapse_btn.setAccessibleName('折叠 ADS Agent 到侧栏')
        self.collapse_btn.clicked.connect(dock.hide)
        layout.addWidget(self.collapse_btn)
        self.restyle(pal)

    def restyle(self, pal):
        self._pal = pal
        self.compat_label.setStyleSheet(
            f"color:{pal['subtle']};background:transparent;font-size:{U.fs('micro')}px;")
        self.setStyleSheet(
            f"QWidget#agentDockTitleBar{{background:{pal['header_bg']};}}"
        )
        self.collapse_btn.setIcon(_draw_icon('collapse', pal['subtle'], U.px(16)))
        self.collapse_btn.setIconSize(QSize(U.px(16), U.px(16)))
        self.collapse_btn.setStyleSheet(
            f"QToolButton{{border:none;background:transparent;padding:0;"
            f"border-radius:{U.R('sm')}px;}}"
            f"QToolButton:hover{{background:{pal['accent_soft']};}}"
            f"QToolButton:focus{{border:1px solid {pal['accent']};}}"
        )
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        QPointF = QtCore.QPointF
        QColor, QPainter = QtGui.QColor, QtGui.QPainter
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self._pal['header_bg']))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if self._grip_hover or self._dragging:
            painter.setBrush(QColor(self._pal['hover']))
            painter.drawRoundedRect(self._grip_rect(), U.px(6), U.px(6))
        # One drag surface, styled with macOS-inspired muted traffic lights.
        for x, color in ((17, '#ee8b86'), (33, '#e6bb65'), (49, '#7fbe98')):
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(U.px(x), self.height() / 2), U.px(4.5), U.px(4.5))
        painter.end()

    def mousePressEvent(self, event):
        self._dragging = (event.button() == Qt.MouseButton.LeftButton
                          and self._grip_rect().contains(event.pos()))
        self.update()
        event.ignore() if self._dragging else event.accept()

    def mouseMoveEvent(self, event):
        # Qt may take the mouse grab during a dock drag and handle its release.
        if self._dragging and not (event.buttons() & Qt.MouseButton.LeftButton):
            self._dragging = False
        self._update_grip_feedback(event.pos())
        event.ignore() if self._dragging else event.accept()

    def mouseReleaseEvent(self, event):
        was_dragging = self._dragging
        self._dragging = False
        self._update_grip_feedback(event.pos())
        event.ignore() if was_dragging else event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._grip_rect().contains(event.pos()):
            event.ignore()
        else:
            event.accept()

    def leaveEvent(self, event):
        self._grip_hover = False
        if not self._dragging:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def _update_grip_feedback(self, position):
        self._grip_hover = self._grip_rect().contains(position)
        self.setCursor(Qt.CursorShape.SizeAllCursor if self._grip_hover or self._dragging
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def _grip_rect(self):
        # A padded group target, not three tiny individual dot targets.
        return QRect(U.px(8), U.px(3), U.px(52), U.px(26))


def _install_dock_title_bar(dock, widget):
    bar = _DockTitleBar(dock, widget._pal())
    dock.setTitleBarWidget(bar)
    widget._dock_title_bar = bar
    return bar


def _restyle_handle(pal: dict) -> None:
    """边缘标签跟随面板主题换色（_apply_theme 会调过来）。"""
    if _handle_tab is not None:
        _handle_tab.setStyleSheet(
            f"QPushButton{{background:{pal['card_bg']}; color:{pal['accent']};"
            f" border:1px solid {pal['card_border']}; border-right:none;"
            f" border-top-left-radius:{U.R('md')}px;"
            f" border-bottom-left-radius:{U.R('md')}px;"
            f" font-size:{U.fs('small')}px; font-weight:bold;{U.font_css()}}}"
            f"QPushButton:hover{{background:{pal['accent_soft']};}}"
        )


def _sync_handle(visible: bool) -> None:
    """主 dock 隐藏（折叠/关闭）时显示边缘标签，重新显示时藏起来。"""
    if _handle is not None:
        _handle.setVisible(not visible)


def _expand_panel() -> None:
    """从折叠状态恢复：显示主面板、藏起边缘标签。"""
    if _panel is not None:
        _panel.setVisible(True)
        _panel.raise_()
    if _handle is not None:
        _handle.setVisible(False)


def _compat_title_suffix() -> str:
    """实验性 / 未知版本时给窗口标题加的后缀 —— 开启≠验证通过，必须可见。"""
    try:
        import capability

        snap = capability.snapshot()
        version = snap.get("ads_version") or {}
        year = version.get("year")
        if version.get("status") == "known" and year in (2024, 2025, 2026):
            return f"（实验性 · ADS {year} 未实机验证）"
        if version.get("status") != "known":
            return "（实验性 · ADS 版本未识别）"
    except Exception:  # noqa: BLE001 — 横幅拿不到不影响面板本身
        pass
    return ""


def open_panel():
    """Show (and dock on first use) the agent panel inside the ADS main window."""
    global _panel, _handle, _handle_tab
    import toolserver

    toolserver.ensure_started()  # belt & braces: menu handler normally did this

    if _panel is not None:
        _expand_panel()
        return _panel

    # main_pyside_widget 是 2027 实测接口；2024/2025 上可能不存在或行为不同。
    # 获取失败时退化为独立窗口，**不能**让整个面板打开动作崩溃。
    try:
        from keysight.ads.de.app import window as app_window

        main_win = app_window.main_pyside_widget()
    except Exception as e:  # noqa: BLE001
        print(f"[ADS Agent] 未获取到 ADS 主窗口（{type(e).__name__}: {e}），"
              "面板将使用独立窗口模式")
        main_win = None

    panel_widget = AgentPanelWidget()
    title = DOCK_TITLE + _compat_title_suffix()

    if isinstance(main_win, QMainWindow):
        dock = QDockWidget(title, main_win)
        dock.setObjectName(DOCK_OBJECT_NAME)
        dock.setWidget(panel_widget)
        _install_dock_title_bar(dock, panel_widget)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )
        main_win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        try:
            main_win.resizeDocks([dock], [U.px(430)], Qt.Orientation.Horizontal)
        except Exception:
            pass
        _panel = dock

        # 折叠标签：面板被折叠/关闭后留在右缘的窄竖条，点击即展开。
        # 空标题栏 + 无特性：这条标签本身不可拖动、不可关闭，只负责"展开"。
        handle = QDockWidget(main_win)
        handle.setObjectName(HANDLE_OBJECT_NAME)
        handle.setTitleBarWidget(QWidget(handle))
        handle.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        tab = _VerticalTabButton("◀ ADS Agent")
        tab.setToolTip("展开 ADS Agent 面板")
        tab.clicked.connect(_expand_panel)
        handle.setWidget(tab)
        handle.setFixedWidth(U.px(34))
        main_win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, handle)
        try:
            main_win.resizeDocks([handle], [U.px(34)], Qt.Orientation.Horizontal)
        except Exception:
            pass
        handle.hide()
        _handle, _handle_tab = handle, tab
        _restyle_handle(panel_widget._pal())

        # 自定义标题栏 » 是唯一的折叠入口，隐藏后显示侧边展开标签。
        dock.visibilityChanged.connect(_sync_handle)
    else:
        # fallback: standalone window (main window not found)
        win = QMainWindow()
        win.setWindowTitle(title)
        win.setCentralWidget(panel_widget)
        win.resize(U.px(470), U.px(740))
        win.show()
        _panel = win

    return _panel
