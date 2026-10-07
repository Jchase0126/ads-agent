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

import configparser
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.normpath(os.path.join(_HERE, "..", "..", "config.ini"))

# Qt / Windows 默认应用字号约 8.25~9pt，作为 1.0x 基准
_BASE_PT = 9.0
_AUTO_MIN, _AUTO_MAX = 1.0, 1.75      # 自动推导的取值范围
_MANUAL_MIN, _MANUAL_MAX = 0.75, 2.0  # 手动覆盖允许的取值范围

# 圆角令牌：整体偏圆润，刻度统一
_RADII = {"xs": 6, "sm": 9, "md": 12, "lg": 16, "xl": 22, "pill": 9999}

# 内边距 / 间距令牌
_PADS = {"xs": 2, "sm": 4, "md": 6, "lg": 8, "xl": 10, "xxl": 14}

# 字号令牌（px @1.0x）
_FONTS = {"micro": 10, "tiny": 11, "small": 12, "body": 13,
          "title": 15, "hero": 18, "logo": 34}

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
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
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
        from PySide6.QtGui import QFontDatabase

        families = set(QFontDatabase.families())
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
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamily(_pick(_UI_FONT_PREFERENCE, "Microsoft YaHei UI"))
    font.setPixelSize(fs(token))
    font.setBold(bold)
    return font


def refresh() -> None:
    """配置改过（scale / font）后调用，清掉缓存。"""
    _cache.clear()
