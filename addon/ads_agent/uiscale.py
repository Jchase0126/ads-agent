"""自适应 UI 缩放 + 圆角设计令牌（ADS Agent 面板）。

面板里不再散落硬编码像素，统一走这里：

* ``scale()`` —— 缩放系数：跟随宿主系统 / ADS 的默认字号（Windows 文本缩放、
  ADS 字体偏好会体现在 QApplication 字体上），也可由 ``config.ini`` 的
  ``[ui] scale`` 手动覆盖（如 ``1.25`` 放大、``0.9`` 紧凑）。
* ``px()``    —— 尺寸（间距 / 圆角 / 控件大小）按系数换算。
* ``fs()``    —— 字号（px）。
* ``font_css()`` / ``qfont()`` —— 在系统字体里挑一款现代、圆润且带中文的字体。
* ``R()`` / ``P()`` —— 圆角与内边距的语义令牌（xs → pill）。

宽度方向的自适应（气泡最大宽度、侧栏宽度、输入框高度、模型清单高度）由
panel.py 按"容器比例"计算，不写死像素；面板拖宽拖窄时布局都会跟着走。

注意：这里**不**按 logicalDpi 缩放 —— Qt 6 已经处理 HiDPI，再乘一次会重复
放大。字号缩放只跟随"逻辑字号"（pt），与屏幕 DPI 无关。
"""

from __future__ import annotations

import configparser
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.normpath(os.path.join(_HERE, "..", "..", "config.ini"))

# Qt / Windows 默认应用字号约 8.25~9pt，作为 1.0x 基准
_BASE_PT = 9.0
_AUTO_MIN, _AUTO_MAX = 1.0, 1.75      # 自动推导的取值范围
_MANUAL_MIN, _MANUAL_MAX = 0.75, 2.0  # 手动覆盖允许的取值范围

# 圆角令牌：整体偏圆润，刻度统一
_RADII = {"xs": 4, "sm": 8, "md": 10, "lg": 12, "xl": 16, "pill": 9999}

# 内边距 / 间距令牌
_PADS = {"xs": 2, "sm": 4, "md": 6, "lg": 8, "xl": 10, "xxl": 14}

# 字号令牌（px @1.0x）
_FONTS = {"micro": 11, "tiny": 12, "small": 13, "body": 14,
          "title": 16, "hero": 20, "logo": 34}

# 优先字体：圆润现代 + 中文字形完整；Qt 找不到时依次回退
_UI_FONT_PREFERENCE = [
    "Microsoft YaHei UI",     # Win10/11 中文界面首选，字形偏圆
    "Microsoft YaHei",
    "PingFang SC",            # macOS
    "HarmonyOS Sans SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "Segoe UI Variable Text",
    "Segoe UI",
    "System-ui",
    "Helvetica Neue",
    "Arial",
]
_MONO_FONT_PREFERENCE = [
    "Cascadia Mono", "JetBrains Mono", "Cascadia Code",
    "Consolas", "Menlo", "Monaco", "Courier New",
]

_cache: dict = {}


# ---------------------------------------------------------------------------
# config 读取
# ---------------------------------------------------------------------------

def _ui_setting(key: str, default: str = "") -> str:
    parser = configparser.ConfigParser()
    if os.path.exists(_CONFIG):
        try:
            parser.read(_CONFIG, encoding="utf-8")
        except Exception:  # noqa: BLE001 — 配置坏了也不该让界面挂掉
            return default
    try:
        raw = parser.get("ui", key).strip()
        return raw if raw else default
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


# ---------------------------------------------------------------------------
# 缩放系数
# ---------------------------------------------------------------------------

def _system_point_size() -> float:
    """宿主默认字号的 pt 值（反映 Windows 文本缩放 / ADS 字体偏好）。"""
    try:
        import qtcompat

        app = qtcompat.QtWidgets().QApplication.instance()
        if app is None:
            return _BASE_PT
        pt = app.font().pointSizeF()
        return float(pt) if pt and pt > 0 else _BASE_PT
    except Exception:  # noqa: BLE001
        return _BASE_PT


def scale() -> float:
    """全局缩放系数（带缓存）。"""
    if "scale" in _cache:
        return _cache["scale"]

    value = None
    raw = _ui_setting("scale")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is not None:
            value = max(_MANUAL_MIN, min(_MANUAL_MAX, value))
    if value is None:
        # 8.x pt 是 Qt/Windows 的默认值，别因此把界面缩小
        value = _system_point_size() / _BASE_PT
        if value < 1.0:
            value = 1.0
        value = max(_AUTO_MIN, min(_AUTO_MAX, value))
        # 量化到 0.05，避免窗口尺寸抖动
        value = round(value * 20) / 20.0

    _cache["scale"] = value
    return value


def px(value: float) -> int:
    """尺寸换算（间距 / 圆角 / 控件大小）。"""
    return int(round(value * scale()))


def fs(token: str) -> int:
    """语义字号 -> px（micro / tiny / small / body / title / hero / logo）。"""
    return px(_FONTS.get(token, _FONTS["body"]))


def R(token: str = "md") -> int:  # noqa: N802 — 短命名，样式表里好读
    """圆角半径令牌。"""
    return px(_RADII.get(token, _RADII["md"]))


def P(token: str = "md") -> int:  # noqa: N802
    """内边距 / 间距令牌。"""
    return px(_PADS.get(token, _PADS["md"]))


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------

def _available_families() -> set:
    if "families" in _cache:
        return _cache["families"]
    families: set = set()
    try:
        import qtcompat

        families = set(qtcompat.font_families())
    except Exception:  # noqa: BLE001
        families = set()
    _cache["families"] = families
    return families


def _pick(preference: list, fallback: str) -> str:
    override = _ui_setting("font_family").strip()
    if override:
        for name in (n.strip() for n in override.replace(";", ",").split(",")):
            if name:
                return name
    available = _available_families()
    for name in preference:
        if name in available:
            return name
    return fallback


def font_css() -> str:
    """样式表用的 font-family 声明（含回退链）。"""
    if "font_css" in _cache:
        return _cache["font_css"]
    picked = _pick(_UI_FONT_PREFERENCE, "Microsoft YaHei UI")
    chain = [picked] + [f for f in _UI_FONT_PREFERENCE if f != picked][:3]
    css = "font-family:" + ",".join(f'"{n}"' for n in chain) + ";"
    _cache["font_css"] = css
    return css


def mono_css() -> str:
    if "mono_css" in _cache:
        return _cache["mono_css"]
    picked = _pick(_MONO_FONT_PREFERENCE, "Consolas")
    chain = [picked, "Consolas", "monospace"]
    css = "font-family:" + ",".join(f'"{n}"' for n in chain) + ";"
    _cache["mono_css"] = css
    return css


def qfont(token: str = "body", bold: bool = False):
    """构造一个 QFont（用于 QListWidget 等不走样式表字号的控件）。"""
    import qtcompat

    QFont = qtcompat.QtGui().QFont

    font = QFont()
    font.setFamily(_pick(_UI_FONT_PREFERENCE, "Microsoft YaHei UI"))
    font.setPixelSize(fs(token))
    font.setBold(bold)
    return font


def refresh() -> None:
    """配置改过（scale / font）后调用，清掉缓存。"""
    _cache.clear()


_icon_assets = None


def icon_path(name: str, color: str = '#ffffff') -> str:
    """Cache tiny painted Qt assets for stylesheet indicators; no external files required."""
    import tempfile
    import qtcompat
    core, gui = qtcompat.QtCore(), qtcompat.QtGui()
    QPointF, Qt = core.QPointF, core.Qt
    QColor, QPainter, QPen, QPixmap = gui.QColor, gui.QPainter, gui.QPen, gui.QPixmap

    global _icon_assets
    if _icon_assets is None:
        _icon_assets = tempfile.TemporaryDirectory(prefix='ads_ui_icons_')
    path = os.path.join(_icon_assets.name, name + color.replace('#', '_') + '.png')
    if not os.path.exists(path):
        image = QPixmap(32, 32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        points = [(7, 16), (13, 22), (25, 10)] if name == 'check' else [(9, 13), (16, 20), (23, 13)]
        painter.drawLine(QPointF(*points[0]), QPointF(*points[1]))
        painter.drawLine(QPointF(*points[1]), QPointF(*points[2]))
        painter.end()
        image.save(path)
    return path.replace('\\', '/')


def indicator_css(pal: dict, owner: str = 'QCheckBox') -> str:
    check = icon_path('check')
    return (
        f"{owner}::indicator{{width:{px(16)}px;height:{px(16)}px;"
        f"border:1px solid {pal['input_border']};border-radius:{R('xs')}px;background:{pal['input_bg']};}}"
        f"{owner}::indicator:hover{{border-color:{pal['accent']};}}"
        f"{owner}::indicator:checked{{background:{pal['accent']};border-color:{pal['accent']};image:url(\"{check}\");}}"
        f"{owner}::indicator:disabled{{background:{pal['card_bg']};border-color:{pal['card_border']};}}"
    )


def field_css(pal: dict, combo: bool = False) -> str:
    selector = 'QComboBox' if combo else 'QLineEdit'
    css = (
        f"{selector}{{background:{pal['input_bg']};color:{pal['text']};"
        f"border:1px solid {pal['input_border']};border-radius:{R('sm')}px;"
        f"min-height:{px(18)}px;padding:{px(8)}px {px(10)}px;font-size:{fs('small')}px;{font_css()}}}"
        f"{selector}:hover{{border-color:{pal['subtle']};}}"
        f"{selector}:focus{{border-color:{pal['accent']};}}"
    )
    if combo:
        arrow = icon_path('chevron', pal['subtle'])
        css += (
            f"QComboBox::drop-down{{border:none;width:{px(28)}px;}}"
            f"QComboBox::down-arrow{{image:url(\"{arrow}\");width:{px(12)}px;height:{px(12)}px;}}"
            f"QComboBox QLineEdit{{background:transparent;color:{pal['text']};border:none;padding:0;margin:0;}}"
            f"QComboBox QAbstractItemView{{background:{pal['input_bg']};color:{pal['text']};"
            f"border:1px solid {pal['card_border']};padding:{px(4)}px;"
            f"selection-background-color:{pal['accent_soft']};selection-color:{pal['text']};}}"
        )
    return css


def action_css(pal: dict, primary: bool = False) -> str:
    bg = pal['accent'] if primary else pal['input_bg']
    fg = '#ffffff' if primary else pal['text']
    hover = pal['accent_hover'] if primary else pal['hover']
    return (
        f"QPushButton{{background:{bg};color:{fg};border:1px solid {pal['accent'] if primary else pal['card_border']};"
        f"border-radius:{R('sm')}px;padding:{px(8)}px {px(12)}px;"
        f"min-height:{px(18)}px;"
        f"font-size:{fs('small')}px;{font_css()}}}"
        f"QPushButton:hover{{background:{hover};}}"
        f"QPushButton:focus{{border-color:{pal['accent']};}}"
        f"QPushButton:disabled{{background:{pal['card_bg']};color:{pal['subtle']};}}"
    )


def dialog_css(pal: dict) -> str:
    return (
        f"QDialog{{background:{pal['panel_bg']};{font_css()}}}"
        f"QLabel{{color:{pal['text']};font-size:{fs('small')}px;{font_css()}}}"
        f"QTextEdit,QPlainTextEdit{{background:{pal['input_bg']};color:{pal['text']};"
        f"border:1px solid {pal['card_border']};padding:{px(8)}px;{mono_css()}}}"
        f"QComboBox::drop-down{{border:none;width:{px(24)}px;}}"
        f"QComboBox QAbstractItemView{{background:{pal['input_bg']};color:{pal['text']};"
        f"selection-background-color:{pal['accent_soft']};selection-color:{pal['text']};}}"
        f"QTableWidget{{background:{pal['input_bg']};alternate-background-color:{pal['card_bg']};"
        f"color:{pal['text']};border:1px solid {pal['card_border']};"
        f"gridline-color:{pal['card_border']};font-size:{fs('small')}px;{mono_css()}}}"
        f"QTableWidget::item{{padding:{px(6)}px;}}"
        f"QTableWidget::item:selected{{background:{pal['accent_soft']};color:{pal['text']};}}"
        f"QHeaderView::section{{background:{pal['card_bg']};color:{pal['subtle']};"
        f"border:none;border-bottom:1px solid {pal['card_border']};padding:{px(8)}px;{font_css()}}}"
        f"QScrollBar:vertical{{background:transparent;width:{px(10)}px;}}"
        f"QScrollBar::handle:vertical{{background:{pal['scroll']};border-radius:{px(5)}px;min-height:{px(24)}px;}}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        + field_css(pal) + field_css(pal, combo=True) + action_css(pal)
    )


def message_dialog(title: str, text: str, details: str = "", warning: bool = False,
                   parent=None, pal=None):
    """A themed native status/error dialog with optional technical details."""
    import qtcompat
    core, widgets = qtcompat.QtCore(), qtcompat.QtWidgets()
    Qt, QTimer = core.Qt, core.QTimer
    QMessageBox, QPushButton, QTextEdit = widgets.QMessageBox, widgets.QPushButton, widgets.QTextEdit

    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.button(QMessageBox.StandardButton.Ok).setText("知道了")
    if details:
        box.setDetailedText(details)
        def localize_details():
            for button in box.findChildren(QPushButton):
                if button is box.button(QMessageBox.StandardButton.Ok):
                    continue
                button.setText("查看详情")
                button.clicked.connect(lambda _=False, b=button: b.setText(
                    "收起详情" if any(edit.isVisible() for edit in box.findChildren(QTextEdit)) else "查看详情"))
        QTimer.singleShot(0, localize_details)
    box.setStyleSheet(dialog_css(pal or PALETTES['light']))
    return box


# Shared colors for the panel and its child dialogs.
PALETTES = {
    "light": {
        "panel_bg": "#ffffff", "chat_bg": "#ffffff",
        "header_bg": "#edf3fb", "sidebar_bg": "#f2f4f7",
        "text": "#172234", "subtle": "#65758b",
        "user_bubble": "#edf3ff", "user_bubble_to": "#edf3ff",
        "user_bubble_border": "#edf3ff", "user_text": "#203b66",
        "ai_bubble": "#ffffff", "ai_bubble_border": "#ffffff", "ai_text": "#172234",
        "card_bg": "#f4f6fa", "card_border": "#dfe5ee",
        "accent": "#246bdb", "accent_hover": "#1d5dbc", "accent_soft": "#edf3ff",
        "avatar_user": "#246bdb", "avatar_user_to": "#246bdb", "avatar_user_ring": "#edf3ff",
        "avatar_ai": "#edf3ff", "avatar_ai_ring": "#edf3ff",
        "input_bg": "#ffffff", "input_border": "#d6dfea",
        "chip_bg": "#ffffff", "chip_text": "#172234", "chip_hover": "#edf3ff",
        "hover": "#f0f3f8", "scroll": "#c5ceda", "error": "#c43d4b",
        "stop_bg": "#c43d4b", "stop_bg_hover": "#ad3040",
    },
    "dark": {
        "panel_bg": "#191919", "chat_bg": "#171717",
        "header_bg": "#202020", "sidebar_bg": "#1c1c1c",
        "text": "#e7e7e7", "subtle": "#a1a1a1",
        "user_bubble": "#22364f", "user_bubble_to": "#22364f",
        "user_bubble_border": "#22364f", "user_text": "#e0ebff",
        "ai_bubble": "#171717", "ai_bubble_border": "#171717", "ai_text": "#e7e7e7",
        "card_bg": "#222222", "card_border": "#343434",
        "accent": "#7d9ff0", "accent_hover": "#9bb5f5", "accent_soft": "#22364f",
        "avatar_user": "#7d9ff0", "avatar_user_to": "#7d9ff0", "avatar_user_ring": "#22364f",
        "avatar_ai": "#22364f", "avatar_ai_ring": "#22364f",
        "input_bg": "#242424", "input_border": "#383838",
        "chip_bg": "#242424", "chip_text": "#e7e7e7", "chip_hover": "#2c2c2c",
        "hover": "#2e2e2e", "scroll": "#484848", "error": "#ff8a92",
        "stop_bg": "#b84c59", "stop_bg_hover": "#a5404e",
    },
}
