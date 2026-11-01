# -*- coding: utf-8 -*-
"""RF 计算器面板 —— 实机 AppCAD 设计页 1:1 版式复刻（clean-room 重实现）。

法律边界（与 backend/rfcalc.py、THIRD-PARTY-NOTICES.txt 口径一致）：
本窗口是 Avago AppCAD 功能的 **clean-room 重实现** —— 未反汇编、未翻译、
未派生自 Avago 的任何代码或文案；所有图示均用 QPainter 矢量自绘（禁止贴图），
全部 UI 文案自拟（仅保留「Main Menu [F8]」「Calculate Z0 [F4]」等通用功能
短语与底部 Web 条英文以维持版式 1:1）。所有公式与数值口径只取自公开教科书
/论文，出处集中标注在 ``backend/rfcalc.py`` 的模块及各函数 docstring 里。

结构（对照实机截图 01/05/06/07/08/10/11/12/18/20/21/22/23/24）：
* A 黄底计算页 —— 传输线 7 页（微带/带状/圆同轴/方同轴/双线/CPW/板线槽线）
  + 反射 / 混频杂散 / 互调 3 个变体页：Times 22pt 标题、Main Menu [F8]、
  等轴结构图内嵌尺寸 QLineEdit、中列 εr/基片/Freq/Length Units、
  右列 Calculate 钮 + 结果行（标签=值框~90px+单位）、底部 Web 条。
* B 白底偏置页 —— BJT 分压 / FET 自偏 / FET 分压 / PAE：原理图嵌框 +
  QGroupBox 参数组 + 编号说明 + Bias Analysis 结果表。
* C 栅格页 —— NoiseCalc：级数 spin + Calculate [F4] + Stage1..N 图标栅格 +
  Stage Data / Stage Analysis 分区（引擎算不出的行整行灰显“-”）+
  底部 System Parameters / System Analysis（NF 黄底高亮）/ IM 盒（灰显）+
  左下 LED 状态。
* 侧栏 Outlook 风格：分组条 + 32px 自绘图标项，组展开记忆（Main Menu [F8]
  = 折叠/展开侧栏）。
* 实机「线过地板 Wire Over Groundplane」页引擎暂无对应模型，本版不建页
  （侧栏不含，见交付说明），不虚构公式。

引擎：``backend/rfcalc.py``（唯一实现，只读不改）。ADS 进程内经
``pathbridge.load_backend_module("rfcalc.py", "rfcalc")`` 加载；纯 PySide
测试环境兜底：把 ``backend/`` 插进 sys.path 后 ``import rfcalc``。
引擎承诺永不抛异常（坏输入返回 {"ok": False, "error": ...}）；UI 解析层
仍全部 try/except 兜底：A 页错误右列红字 ✗，C 页坏格 _warn 弹窗，绝不崩。

可测性：每个可编辑控件 setObjectName("<page>.<field>")；结果值框
"<page>.result.<key>"；保持 open_rfcalc_panel 单例契约与 closeEvent 隐藏。

独立预览 / 渲染取证：
    python rfcalc_panel.py                  # 弹出窗口（脱离 ADS 目检）
    python rfcalc_panel.py --snap OUTDIR    # offscreen 渲染每页 PNG
"""

import math
import os
import sys

# Qt 一律经 qtcompat 适配层导入（PySide2/6 双兼容，进程内只跟宿主走）。
try:
    import qtcompat  # noqa: F401
    import uiscale  # noqa: F401
except ImportError:  # pragma: no cover —— 包内导入兜底
    from . import qtcompat  # noqa: F401
    from . import uiscale  # noqa: F401

QtCore = qtcompat.QtCore()
QtGui = qtcompat.QtGui()
QtWidgets = qtcompat.QtWidgets()
Qt = QtCore.Qt

# AppCAD 4.00 实测版式模块（1:1 示意图规格 + chrome 常量），可选依赖：
# 缺失时回退到内置手绘 _Dg* 类。
try:
    import appcad_spec as _specmod  # noqa: F401
    import appcad_tl as _TL_SPEC
    import appcad_circ as _CIRC_SPEC
    import appcad_chrome as _CHROME
    SpecDiagram = _specmod.SpecDiagram
except ImportError:  # pragma: no cover —— 包内导入兜底
    try:
        from . import appcad_spec as _specmod  # noqa: F401
        from . import appcad_tl as _TL_SPEC
        from . import appcad_circ as _CIRC_SPEC
        from . import appcad_chrome as _CHROME
        SpecDiagram = _specmod.SpecDiagram
    except ImportError:
        _specmod = _TL_SPEC = _CIRC_SPEC = _CHROME = SpecDiagram = None
U = getattr(uiscale, "U", uiscale)
_PALETTE_SRC = getattr(U, "PALETTES", None)
_PAL = _PALETTE_SRC.get("light", {}) if isinstance(_PALETTE_SRC, dict) else {}

C0 = 299792458.0  # 光速 m/s（与引擎一致；仅用于 UI 侧派生电长度显示）

# ---------------------------------------------------------------------------
# 版式令牌（实机像素实测，SPEC-REPLICA-1 §1）
# ---------------------------------------------------------------------------

A_BG = "#FFFFC0"
B_BG = "#FFFFFF"
C_BG = "#F0F0F0"
GOLD = "#FFCC00"
SUBS = "#CCCC99"
SUBS_F = "#D8D8B0"
SUBS_S = "#BFBF8F"
GNDV = "#7B68EE"       # wire-over-gnd 地板色（本版未建页，保留令牌）
CABLE = "#F4F4E5"
LOADG = "#D2EFC5"
DIEMED = "#D9D9D9"
SQUARE = "#B9DFD8"
GRIDHEAD = "#F0F0F0"
ICONBG = "#DAECEE"
BADRED = "#FF9999"
HILITE = "#99CCFF"
YEL_HL = "#FFFF99"
GRIDLINE = "#A0A0A0"
WEBBLUE = "#0000CC"
LED_G = "#33FF00"
LED_R = "#FF0000"
_ERR = "#CC0000"
RO_BG = "#EFEFEF"

_LEN_ITEMS = ["mils", "mm", "cm", "µm", "inch"]
_LEN_ALIAS = {"mils": "mil", "mm": "mm", "cm": "cm", "µm": "um",
              "um": "um", "inch": "inch", "m": "m"}
_FREQ_ITEMS = ["GHz", "MHz"]
_TIME_ITEMS = ["ns", "us", "ms", "s"]
_TIME_SCALE = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}


# ---------------------------------------------------------------------------
# 引擎加载：pathbridge 优先（ADS 进程内），sys.path 兜底（纯 PySide 测试环境）
# ---------------------------------------------------------------------------

def _load_engine_module():
    try:
        from pathbridge import load_backend_module

        return load_backend_module("rfcalc.py", "rfcalc")
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        backend = os.path.abspath(os.path.join(here, "..", "..", "backend"))
        if backend not in sys.path:
            sys.path.insert(0, backend)
        import rfcalc  # noqa: F401

        return sys.modules["rfcalc"]


_ENGINE = None


def _E():
    """懒加载并缓存引擎模块（失败返回 None，由调用方给出 UI 提示）。"""
    global _ENGINE
    if _ENGINE is None:
        try:
            _ENGINE = _load_engine_module()
        except Exception:
            return None
    return _ENGINE


def _e():
    e = _E()
    if e is None:
        raise _InputError("引擎 backend/rfcalc.py 加载失败，无法计算")
    return e


# ---------------------------------------------------------------------------
# 数值 / 格式 / 弹窗钩子
# ---------------------------------------------------------------------------

class _InputError(ValueError):
    """UI 输入层防御：给用户看的中文提示，显示在页面错误行/状态栏，不崩。"""


def _warn(parent, title, text):
    """模块级弹窗钩子（测试可替换捕获）。QMessageBox.warning 包装。"""
    QtWidgets.QMessageBox.warning(parent, title, text)


def _f(le, name="数值"):
    txt = str(le.text()).strip() if le is not None else ""
    try:
        return float(txt)
    except (TypeError, ValueError):
        raise _InputError("%s：请输入数字（当前 %r）" % (name, txt or "空"))


def _fopt(le, default=0.0, name="数值"):
    txt = str(le.text()).strip() if le is not None else ""
    if txt == "":
        return default
    try:
        return float(txt)
    except (TypeError, ValueError):
        raise _InputError("%s：请输入数字（当前 %r）" % (name, txt))


def _fitem(it, name="数值"):
    txt = str(it.text()).strip() if it is not None else ""
    try:
        return float(txt)
    except (TypeError, ValueError):
        raise _InputError("%s：请输入数字（当前 %r）" % (name, txt or "空"))


def _g(v):
    """引擎值 → 显示串：float %g（6 位有效数字）、complex a±bj、None→-、inf→∞。"""
    if v is None:
        return "-"
    if isinstance(v, complex):
        sgn = "+" if v.imag >= 0 else "-"
        return "%g%s%gj" % (v.real, sgn, abs(v.imag))
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isinf(v):
        return "∞"
    if math.isnan(v):
        return "NaN"
    return "%g" % v


def _fmt2(v):
    try:
        return "%.2f" % float(v)
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# 图标（侧栏 32px / 栅格列头 24px）—— 全部 QPainter 矢量自绘，禁止贴图
# ---------------------------------------------------------------------------

_ICON_CACHE = {}


def _pix(kind, size):
    if kind in _ICON_CACHE:
        return _ICON_CACHE[kind]
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    s = float(size)
    k = s / 32.0  # 统一按 32 逻辑坐标绘制

    def poly(pts, fill, edge="#222222"):
        _pen(p, edge, 1.2)
        p.setBrush(QtGui.QColor(fill) if fill else Qt.NoBrush)
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(x * k, y * k) for x, y in pts]))

    def line(x1, y1, x2, y2, c="#222222", w=1.2):
        _pen(p, c, w)
        p.drawLine(QtCore.QLineF(x1 * k, y1 * k, x2 * k, y2 * k))

    def rect(x, y, w_, h_, fill):
        _pen(p, "#222222", 1.0)
        p.setBrush(QtGui.QColor(fill))
        p.drawRect(QtCore.QRectF(x * k, y * k, w_ * k, h_ * k))

    def circ(x, y, rr, fill):
        _pen(p, "#222222", 1.0)
        p.setBrush(QtGui.QColor(fill))
        p.drawEllipse(QtCore.QPointF(x * k, y * k), rr * k, rr * k)

    if kind == "microstrip":
        poly([(6, 12), (26, 12), (29, 7), (9, 7)], SUBS_F)
        rect(3, 12, 26, 12, SUBS)
        rect(8, 5, 12, 3, GOLD)
    elif kind == "stripline":
        rect(3, 6, 26, 4, GOLD)
        rect(3, 10, 26, 12, SUBS)
        rect(8, 15, 16, 3, GOLD)
        rect(3, 22, 26, 4, GOLD)
    elif kind == "coax_round":
        circ(16, 16, 12, GOLD)
        circ(16, 16, 8, DIEMED)
        circ(16, 16, 4, GOLD)
    elif kind == "coax_square":
        rect(4, 4, 24, 24, SQUARE)
        circ(16, 16, 7, GOLD)
    elif kind == "twin":
        line(12, 9, 26, 19, "#ABABAB", 8)
        line(6, 13, 20, 23, "#ABABAB", 8)
        circ(9, 11, 5, "#C8C8C8")
        circ(23, 21, 5, "#C8C8C8")
    elif kind == "wire_gnd":
        line(7, 21, 25, 15, "#ABABAB", 6)
        circ(8, 22, 4, "#C8C8C8")
        rect(3, 25, 26, 5, "#7B68EE")
    elif kind == "cpw":
        rect(4, 4, 7, 24, GOLD)
        rect(13, 4, 6, 24, GOLD)
        rect(21, 4, 7, 24, GOLD)
    elif kind == "trough":
        poly([(4, 6), (28, 6), (28, 26), (22, 26), (22, 12), (10, 12), (10, 26), (4, 26)],
             DIEMED)
        circ(9, 19, 3, GOLD)
        line(11, 17, 24, 12, "#F5C86A", 4)
    elif kind == "bjt":
        circ(15, 16, 9, "#FFFFFF")
        line(15, 10, 15, 22, "#222222", 2)
        line(15, 14, 7, 14)
        line(15, 18, 7, 18)
        line(7, 14, 7, 8)
        line(7, 18, 7, 24)
        line(18, 13, 24, 7)
        line(18, 19, 24, 25)
    elif kind == "fet":
        line(14, 9, 14, 23, "#222222", 2)
        line(14, 13, 7, 13)
        line(14, 19, 7, 19)
        line(18, 11, 18, 21, "#222222", 2)
        line(18, 13, 24, 13)
        line(18, 19, 24, 19)
    elif kind == "pae":
        poly([(7, 6), (7, 26), (26, 16)], HILITE)
        line(3, 16, 7, 16)
        line(26, 16, 30, 16)
    elif kind == "refl":
        rect(3, 12, 14, 8, CABLE)
        rect(19, 12, 9, 8, LOADG)
        line(26, 9, 26, 23, "#4a7a3a", 2)
        line(22, 16, 30, 16, "#4a7a3a", 2)
    elif kind == "grid":
        rect(3, 5, 26, 22, "#FFFFFF")
        for i in range(1, 4):
            line(3, 5 + i * 5.5, 29, 5 + i * 5.5, GRIDLINE)
            line(3 + i * 6.5, 5, 3 + i * 6.5, 27, GRIDLINE)
        rect(3, 5, 26, 5, GRIDHEAD)
    elif kind == "spur":
        circ(16, 16, 11, "#FFFFFF")
        line(9, 9, 23, 23, "#222222", 2)
        line(23, 9, 9, 23, "#222222", 2)
    elif kind == "im":
        line(3, 26, 14, 14, "#3355aa", 1.6)
        line(3, 26, 29, 6, "#aa3333", 1.6)
        line(3, 26, 29, 26, "#222222", 1.2)
    elif kind == "subs":
        rect(3, 18, 26, 5, SUBS)
        rect(5, 12, 22, 4, GOLD)
        rect(3, 6, 26, 4, DIEMED)
    p.end()
    _ICON_CACHE[kind] = pm
    return pm


def _icon(kind, size=32):
    return QtGui.QIcon(_pix(kind, size))


# ---------------------------------------------------------------------------
# 绘图原语（等轴 30° 平行投影）
# ---------------------------------------------------------------------------

def _pen(p, c="#000000", w=1.0):
    q = QtGui.QPen(QtGui.QColor(c))
    q.setWidthF(w)
    p.setPen(q)
    return q


def _poly(p, pts, fill, edge="#000000", pw=1.0, dash=None):
    _pen(p, edge, pw)
    if dash:
        q = p.pen()
        q.setDashPattern(dash)
        p.setPen(q)
    p.setBrush(QtGui.QColor(fill) if fill else Qt.NoBrush)
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(float(x), float(y)) for x, y in pts]))


def _line(p, x1, y1, x2, y2, c="#000000", pw=1.0, dash=None):
    _pen(p, c, pw)
    if dash:
        q = p.pen()
        q.setDashPattern(dash)
        p.setPen(q)
    p.drawLine(QtCore.QLineF(float(x1), float(y1), float(x2), float(y2)))


def _slab(p, x, y, w, h, dep, top=SUBS, front=SUBS_F, side=SUBS_S):
    """等轴长方体：front 矩形 + top 平行四边形 + side 平行四边形。"""
    dx, dy = dep * 0.80, -dep * 0.50
    _poly(p, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], front)
    _poly(p, [(x, y), (x + w, y), (x + w + dx, y + dy), (x + dx, y + dy)], top)
    _poly(p, [(x + w, y), (x + w + dx, y + dy), (x + w + dx, y + dy + h), (x + w, y + h)],
          side)


def _strip_top(p, x1, x2, y, dep, fill=GOLD):
    dx, dy = dep * 0.80, -dep * 0.50
    _poly(p, [(x1, y), (x2, y), (x2 + dx, y + dy), (x1 + dx, y + dy)], fill)


def _ground(p, x, y):
    """接地符号：竖线 + 逐级收窄的三横线。"""
    _line(p, x - 12, y, x + 12, y)
    _line(p, x - 8, y + 4, x + 8, y + 4, "#000000", 0.9)
    _line(p, x - 4, y + 8, x + 4, y + 8, "#000000", 0.8)


def _grad(p, x1, y1, x2, y2, c1, c2):
    """沿轴向的线性渐变画刷（导线/杆体的金属灰/金渐变）。"""
    g = QtGui.QLinearGradient(float(x1), float(y1), float(x2), float(y2))
    g.setColorAt(0.0, QtGui.QColor(c1))
    g.setColorAt(1.0, QtGui.QColor(c2))
    return QtGui.QBrush(g)


def _head(p, x, y, ux, uy, c="#000000", head=5.0, hw=4.5):
    """实心三角箭头头：尖在 (x,y)，方向单位向量 (ux,uy)。"""
    _poly(p, [(x, y),
              (x - head * ux + hw * uy, y - head * uy - hw * ux),
              (x - head * ux - hw * uy, y - head * uy + hw * ux)], c)


def _arrow(p, x1, y1, x2, y2, c="#000000", pw=1.0, head=5.0, hw=4.5):
    """单箭头：细线 + 实心三角头（头在 (x2,y2)）。"""
    _line(p, x1, y1, x2, y2, c, pw)
    dx, dy = float(x2) - x1, float(y2) - y1
    ln = math.hypot(dx, dy) or 1.0
    _head(p, x2, y2, dx / ln, dy / ln, c, head, hw)


def _cross(p, cx, cy, rx=5.5, ry=6.0, c="#000000"):
    """前端面中心黑十字 "+"。"""
    _line(p, cx - rx, cy, cx + rx, cy + 1, c, 1.5)
    _line(p, cx, cy - ry, cx, cy + ry, c, 1.5)


# ---------------------------------------------------------------------------
# 结构图基类：逻辑坐标绘制 + 内嵌尺寸 QLineEdit（AppCAD「图里改尺寸」）
# ---------------------------------------------------------------------------

class _Diagram(QtWidgets.QWidget):
    LW, LH = 440.0, 330.0   # 画布 = 原图 970×693 像素坐标（spec_v2 规格），1:1 无缩放
    OX, OY = 0.0, 0.0       # spec 坐标 → 本画布原点偏移（多数页直接用原图坐标）

    def __init__(self, name=None):
        super().__init__()
        self._name = name or getattr(self, "NAME", "")
        self.fields = {}
        self._geom = {}
        self._labels = []
        self.setFixedSize(int(self.LW), int(self.LH))
        self.setObjectName("diag." + self._name)

    def box(self, key, lx, ly, value="", w=54, h=18, ro=False, obj=None):
        le = QtWidgets.QLineEdit(self)
        le.setText(value)
        le.setAlignment(Qt.AlignRight)
        if ro:
            le.setReadOnly(True)
            le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
        le.setObjectName(obj or ("%s.%s" % (self._name, key)))
        self.fields[key] = le
        self._geom[key] = (lx, ly, w, h)
        return le

    def label(self, lx, ly, text, angle=0.0):
        self._labels.append((lx, ly, text, angle))

    def finalize(self):
        ox, oy = self.OX, self.OY
        for key, (lx, ly, w, h) in self._geom.items():
            self.fields[key].setGeometry(int(lx - ox) - 2, int(ly - oy) - 2,
                                         int(w) + 4, int(h) + 4)
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.translate(-self.OX, -self.OY)
        # draw() 内部调用的 label() 每次重绘都会重新注册；
        # 先存下构建期标签，清空收集本次绘制标签，合并去重后只画一份。
        base = self._labels
        self._labels = []
        try:
            self.draw(p)
        except Exception:
            pass
        dyn = self._labels
        self._labels = base
        seen = set()
        f = QtGui.QFont("Times New Roman", 11)
        f.setItalic(True)
        p.setFont(f)
        p.setPen(_pen(p))
        for lx, ly, txt, ang in base + dyn:
            k = (round(lx, 2), round(ly, 2), txt, round(ang, 2))
            if k in seen:
                continue
            seen.add(k)
            p.save()
            p.translate(lx, ly)
            if ang:
                p.rotate(ang)
            p.drawText(0, 0, txt)
            p.restore()
        p.end()

    def draw(self, p):
        pass


# --- 传输线 7 张结构图 -------------------------------------------------------

class _DgMicrostrip(_Diagram):
    NAME = "microstrip"

    def draw(self, p):
        # --- 基片（spec 05 §2：逐顶点） ---
        _poly(p, [(122, 202), (126, 200), (131, 198), (219, 198), (252, 201),
                  (252, 238), (194, 241), (122, 233)], SUBS)          # 近侧面
        _poly(p, [(214, 165), (231, 158), (241, 154), (326, 154), (326, 183),
                  (302, 197), (295, 201), (214, 201)], SUBS)          # 顶面右大块
        _poly(p, [(212, 166), (214, 165), (231, 158), (241, 154), (269, 154),
                  (269, 179), (212, 179)], SUBS)                      # 顶面左小条
        _poly(p, [(126, 201), (138, 196), (182, 178), (209, 167), (210, 167),
                  (210, 180), (209, 181), (169, 201)], SUBS)          # 顶面左侧块
        _poly(p, [(251, 183), (261, 178), (286, 166), (293, 163), (341, 163),
                  (341, 174), (338, 176), (285, 207), (261, 221), (254, 225),
                  (252, 226), (251, 226)], SUBS)                      # 右端斜面
        _poly(p, [(329, 155), (343, 155), (343, 173), (329, 173)], SUBS)  # 右端小三角
        # --- 金属（#FFCC00；audit cosmetic：导带收窄为窄条比例） ---
        _poly(p, [(174, 198), (178, 196), (262, 156), (272, 161), (188, 201)],
              GOLD)                                               # 导带窄条
        _poly(p, [(123, 229), (193, 229), (193, 232), (123, 232)], GOLD)  # 地板前沿左
        _poly(p, [(195, 229), (244, 229), (244, 232), (195, 232)], GOLD)  # 地板前沿右
        # --- 尺寸线（spec 05 §3；W 延长线止于结构上缘） ---
        _line(p, 174, 130, 174, 197)                       # W 延长线 x174
        _line(p, 188, 130, 188, 200)                       # W 延长线 x188
        _arrow(p, 158, 150, 172, 150)                      # 右向箭头（头在右）
        _arrow(p, 230, 150, 190, 150)                      # 左向箭头（头在左）
        _line(p, 194, 181, 194, 193)                       # 下指箭头杆
        _head(p, 194, 194, 0.0, 1.0)                       # 指向导带前缘
        _line(p, 195, 196, 195, 245)                       # 无头引出线接 T 框
        _line(p, 94, 185, 94, 197)                         # H 下箭头杆
        _head(p, 94, 198, 0.0, 1.0)
        _line(p, 80, 199, 113, 199)                        # H 上刻度线
        _line(p, 80, 234, 113, 234)                        # H 下刻度线
        _arrow(p, 94, 234, 94, 228)                        # H 上箭头（尖朝上）
        _arrow(p, 347, 215, 369, 204)                      # L 箭头A（头触线A）
        _line(p, 317, 150, 384, 213)                       # L 延长线A
        _arrow(p, 297, 246, 276, 257)                      # L 箭头B（头触线B）
        _line(p, 249, 235, 285, 268)                       # L 延长线B
        self.label(131, 157, "W")
        self.label(90, 183, "H")
        self.label(150, 258, "T")
        self.label(380, 197, "L")
        self.label(140, 218, "Ɛr")


class _DgStripline(_Diagram):
    NAME = "stripline"

    def draw(self, p):
        # --- 三明治结构（spec 07 §3，按 z 序） ---
        _poly(p, [(131, 205), (133, 204), (163, 193), (237, 166), (259, 158),
                  (262, 157), (376, 157), (376, 185), (375, 186), (358, 195),
                  (338, 205)], GOLD)                                  # 顶地板顶面
        _poly(p, [(274, 157), (379, 157), (379, 184), (337, 206), (331, 209),
                  (274, 209)], GOLD)                                  # 顶地板右端面
        _line(p, 262, 156, 380, 156)                                  # 右端描边 L 形
        _line(p, 380, 156, 380, 184)
        _line(p, 380, 184, 320, 184)
        _poly(p, [(127, 207), (272, 207), (272, 209), (127, 209)], GOLD)  # 顶板前面
        _poly(p, [(274, 211), (334, 182), (379, 161), (379, 168), (337, 190),
                  (301, 212), (274, 220)], "#CC9999")              # 右端上介质带（窄楔）
        _poly(p, [(304, 182), (379, 182), (379, 184), (337, 206), (314, 218),
                  (310, 220), (304, 220)], GOLD)                      # 中带右端断面
        _poly(p, [(127, 211), (272, 211), (272, 234), (127, 234)], "#CC9999")
        _poly(p, [(180, 219), (182, 218), (197, 212), (200, 211), (257, 211),
                  (254, 213), (241, 219), (232, 223), (225, 226), (222, 226)],
              "#FFCC99")                                              # 中带顶面透视
        _line(p, 223, 208, 260, 225, "#000000", 1.0, dash=[2, 2])     # 隐藏边虚线
        _poly(p, [(175, 219), (223, 219), (223, 228), (175, 228)], None)
        _poly(p, [(175, 221), (219, 221), (219, 227), (175, 227)], GOLD)  # 中带前面
        _poly(p, [(239, 220), (248, 216), (255, 213), (260, 211), (272, 211),
                  (272, 220)], "#CC9999")                             # 前面板补片1
        _poly(p, [(226, 226), (230, 224), (235, 222), (272, 222), (272, 226)],
              "#CC9999")                                              # 前面板补片2
        _poly(p, [(274, 222), (297, 222), (289, 226), (274, 226)], "#CC9999")
        _poly(p, [(127, 236), (172, 236), (172, 239), (127, 239)], GOLD)  # 底板前沿1
        _poly(p, [(174, 236), (219, 236), (219, 239), (174, 239)], GOLD)  # 底板前沿2
        _poly(p, [(221, 236), (272, 236), (272, 239), (221, 239)], GOLD)  # 底板前沿3
        _line(p, 126, 206, 272, 206)                                  # 前上缘描边
        _poly(p, [(340, 205), (355, 212), (366, 221), (366, 227)], None,
              pw=1.0)                                                 # 右端上斜缘
        _poly(p, [(379, 184), (337, 206), (314, 218), (310, 220), (304, 220)],
              None, pw=1.0)                                           # 中带断面底斜缘
        _line(p, 126, 241, 272, 241)                                  # 底板前底缘
        _line(p, 128, 205, 262, 157)                                  # 顶面左缘描边
        # --- 尺寸线（spec 07 §4） ---
        _line(p, 108, 185, 126, 203)                                  # L 左斜刻度
        _arrow(p, 139, 188, 114, 190)                                 # L 左箭头
        _line(p, 228, 130, 256, 153)                                  # L 右斜刻度
        _arrow(p, 214, 154, 239, 143)                                 # L 右箭头
        _line(p, 79, 211, 125, 211)                                   # H 上刻度线
        _arrow(p, 96, 196, 96, 208)                                   # H 下箭头
        _arrow(p, 96, 252, 96, 236)                                   # H 上箭头
        _line(p, 79, 235, 116, 235)                                   # H 下刻度线
        _line(p, 173, 240, 173, 265)                                  # W 延长线
        _line(p, 220, 240, 220, 265)
        _arrow(p, 153, 254, 171, 254)                                 # W 左箭头
        _arrow(p, 239, 254, 222, 254)                                 # W 右箭头
        _line(p, 223, 219, 368, 219)                                  # T 上刻度线
        _line(p, 223, 226, 368, 226)                                  # T 下刻度线
        _arrow(p, 355, 210, 355, 218)                                 # T 下箭头
        _arrow(p, 355, 235, 355, 227)                                 # T 上箭头
        self.label(168, 157, "L")
        self.label(91, 267, "H")
        self.label(243, 265, "W")
        self.label(375, 232, "T")
        self.label(134, 230, "Ɛr")


class _DgCoaxRound(_Diagram):
    NAME = "coax_round"

    def draw(self, p):
        # --- 黄色锥形管壳（spec 10 §3.4，闭合路径） ---
        path = QtGui.QPainterPath()
        path.moveTo(186, 178)
        path.lineTo(281, 156)
        path.cubicTo(288, 158, 294, 162, 299, 169)     # 尾帽弧
        path.lineTo(298, 180)                          # 帽右缘
        path.cubicTo(280, 193, 240, 215, 198, 233)     # 下边（斜率更陡→锥形收缩）
        path.quadTo(190, 228, 186, 219)                # 前端面下弧
        path.closeSubpath()
        p.setBrush(QtGui.QColor(GOLD))
        _pen(p)
        p.drawPath(path)
        # 水平切线（棱线，压黄体，右段兼作 D1 上延长线）
        _line(p, 186, 180, 312, 180)
        # --- 前端面：灰环 + 内导体黄圆 ---
        p.setBrush(QtGui.QColor("#CCCCCC"))
        _pen(p)
        p.drawEllipse(QtCore.QRectF(145, 181, 60, 59))     # 外椭圆 rx30 ry29.5
        p.setBrush(QtGui.QColor(GOLD))
        p.drawEllipse(QtCore.QRectF(161, 198, 28, 27))     # 内圆 r14
        # 隐藏虚线（前端环面上被剖切的壳壁圆）
        _line(p, 162, 193, 204, 199, "#000000", 1.0, dash=[1, 2])
        _line(p, 179, 226, 205, 221, "#000000", 1.0, dash=[1, 2])
        # --- 尺寸线（spec 10 §4） ---
        # L：尺寸线平行管轴，左右陡刻度线，双箭头；值框骑线
        _line(p, 141, 146, 161, 183)                       # 左刻度线
        _line(p, 261, 116, 280, 151)                       # 右刻度线
        _arrow(p, 190, 158, 152, 165)                      # 左箭头（尖触左刻度）
        _arrow(p, 243, 144, 269, 137)                      # 右箭头
        # D2：左侧竖直双箭头量内导体直径（43）；刻度线触前端面
        _line(p, 99, 197, 147, 197)                        # 上刻度线
        _line(p, 99, 226, 148, 226)                        # 下刻度线
        _line(p, 111, 176, 111, 189)                       # 上箭头杆
        _head(p, 112, 197, 0.0, 1.0)
        _head(p, 110, 226, 0.0, -1.0)                      # 下箭头（尖朝上）
        _line(p, 110, 233, 110, 246)
        # D1：右侧竖直双箭头量外导体直径（100）；上延长线=棱线右段
        _line(p, 299, 180, 312, 180)                       # 上延长刻度线
        _head(p, 306, 181, 0.0, -1.0)                      # 上箭头（尖朝上触棱线）
        _line(p, 306, 187, 306, 205)
        _line(p, 186, 244, 313, 244)                       # 下延长刻度线
        _line(p, 306, 226, 306, 235)                       # 下箭头杆
        _head(p, 307, 240, 0.0, 1.0)
        self.label(102, 268, "D2")
        self.label(298, 268, "D1")
        self.label(203, 137, "L")
        self.label(163, 241, "Ɛr")


class _DgCoaxSquare(_Diagram):
    NAME = "coax_square"

    def draw(self, p):
        # --- khaki 壳体（spec 11 §3：顶面 + 右侧面） ---
        _poly(p, [(147, 185), (254, 150), (298, 150), (210, 185)], SUBS, pw=2.0)
        _poly(p, [(210, 185), (298, 150), (299, 194), (296, 194), (212, 245)],
              SUBS, pw=2.0)
        # 水平折线 y185（左段=正面顶边，右段=D1 上延长线）+ 下延长线 y249
        _line(p, 147, 185, 333, 185)
        _line(p, 213, 249, 333, 249)
        # --- 薄荷绿正面（最上层） + 内导体黄圆 ---
        _poly(p, [(147, 185), (210, 185), (210, 247), (147, 247)], "#CCFFCC",
              pw=1.5)
        p.setBrush(QtGui.QColor(GOLD))
        _pen(p)
        p.drawEllipse(QtCore.QRectF(164, 203, 27, 27))
        # 两条隐藏虚线（与内圆相切、平行于轴向）
        _line(p, 156, 198, 210, 184, "#000000", 1.0, dash=[1, 2])
        _line(p, 156, 231, 210, 244, "#000000", 1.0, dash=[1, 2])
        # --- 尺寸线（spec 11 §4） ---
        _line(p, 127, 154, 147, 185)                       # L 左刻度线
        _line(p, 234, 116, 257, 150)                       # L 右刻度线
        _arrow(p, 165, 155, 141, 172)                      # L 左箭头
        _arrow(p, 232, 141, 244, 137)                      # L 右箭头
        # D2：左侧竖直双箭头（43），刻度线止于正面左缘附近
        _line(p, 90, 202, 137, 202)                        # 上刻度线
        _line(p, 100, 230, 144, 230)                       # 下刻度线
        _line(p, 112, 180, 112, 194)                       # 上箭头杆
        _head(p, 113, 199, 0.0, 1.0)
        _head(p, 113, 230, 0.0, -1.0)                      # 下箭头（尖朝上）
        _line(p, 111, 237, 111, 250)
        # D1：右侧竖直双箭头（100），上延长线 y185 / 下延长线 y249
        _head(p, 318, 186, 0.0, -1.0)
        _line(p, 318, 193, 318, 205)
        _line(p, 317, 225, 317, 238)
        _head(p, 317, 244, 0.0, 1.0)
        self.label(103, 276, "D2")
        self.label(309, 276, "D1")
        self.label(185, 135, "L")
        self.label(187, 246, "Ɛr")


class _DgTwinWire(_Diagram):
    NAME = "twin_wire"
    LW = 440.0

    def draw(self, p):
        # --- 两根平行圆导线（spec 08 §3：渐变体 + 白端面 + 黑十字） ---
        for x1, y1, x2, y2, capx, capy in ((70, 183, 225, 151, 231, 161),
                                           (222, 189, 370, 151, 377, 159)):
            path = QtGui.QPainterPath()
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)
            path.cubicTo(x2 + 12, y2 - 5, x2 + 16, y2 + 6, x2 + 12, y2 + 19)
            path.lineTo(x1 + 3, y1 + 27)
            path.closeSubpath()
            p.setBrush(_grad(p, x1, y1, capx, capy, "#BBBBBB", "#DFDFDF"))
            _pen(p)
            p.drawPath(path)
        # 前端面白椭圆 + 黑十字（后画、压线体轮廓）
        p.setBrush(QtGui.QColor("#FFFFFF"))
        _pen(p)
        p.drawEllipse(QtCore.QRectF(56, 186, 22, 24))
        _cross(p, 67, 198)
        p.drawEllipse(QtCore.QRectF(200, 186, 22, 24))
        _cross(p, 211, 198)
        # --- 尺寸线（spec 08 §4） ---
        _line(p, 41, 146, 55, 174)                         # L 左陡刻度线
        _arrow(p, 103, 159, 52, 159)                       # L 左箭头（水平指左）
        _line(p, 214, 111, 229, 139)                       # L 右陡刻度线
        _arrow(p, 170, 132, 217, 128)                      # L 右箭头
        _line(p, 66, 217, 66, 252)                         # S 延长刻度 x66
        _line(p, 210, 217, 210, 252)                       # S 延长刻度 x210
        _arrow(p, 115, 239, 67, 239)                       # S 左箭头（尖触 x66）
        _arrow(p, 164, 239, 207, 239)                      # S 右箭头（尖触 x210）
        _line(p, 225, 185, 389, 186)                       # D 上刻度线
        _line(p, 225, 211, 389, 211)                       # D 下刻度线
        _arrow(p, 375, 166, 375, 182)                      # D 下箭头（尖朝下）
        _arrow(p, 375, 228, 375, 214)                      # D 上箭头（尖朝上）
        self.label(129, 125, "L")
        self.label(134, 223, "S")
        self.label(369, 251, "D")
        self.label(264, 155, "Ɛr")


class _DgCpw(_Diagram):
    NAME = "cpw"

    def draw(self, p):
        # --- 基片（audit blocker 1：顶面三金带 + khaki gap，先面后带） ---
        # 顶面（khaki 整板）+ 前侧面 + 右端面
        _poly(p, [(111, 207), (256, 210), (360, 152), (215, 149)], SUBS)
        _poly(p, [(111, 207), (256, 210), (256, 239), (111, 235)], SUBS)
        _poly(p, [(256, 210), (360, 152), (360, 177), (256, 239)], "#C0B48F")
        # 三条金带（左地 / 中心 / 右地，沿纵深方向延伸；带间露出 khaki gap）
        _poly(p, [(113, 207), (144, 208), (248, 150), (217, 149)], GOLD)
        _poly(p, [(166, 208), (183, 209), (287, 151), (270, 150)], GOLD)
        _poly(p, [(186, 208), (215, 209), (319, 151), (290, 150)], GOLD)
        # 右端面三条细金带（金带远端断面投影）
        _poly(p, [(299, 152), (303, 152), (301, 178), (297, 178)], GOLD)
        _poly(p, [(322, 151), (326, 151), (324, 177), (320, 177)], GOLD)
        _poly(p, [(345, 150), (349, 150), (347, 176), (343, 176)], GOLD)
        # 整板外轮廓描边
        _poly(p, [(111, 207), (256, 210), (360, 152), (360, 177), (256, 239),
                  (111, 235)], None, pw=1.0)
        # --- 尺寸线（spec 06 §4；T 改到右端面上下缘） ---
        _line(p, 87, 216, 141, 160)                        # L 左延长线
        _arrow(p, 120, 178, 100, 185)                      # L 左箭头
        _line(p, 226, 110, 246, 141)                       # L 右延长线
        _arrow(p, 200, 136, 232, 121)                      # L 右箭头
        _line(p, 72, 207, 108, 207)                        # H 上刻度线
        _arrow(p, 88, 197, 88, 206)                        # H 上箭头
        _line(p, 72, 235, 108, 235)                        # H 下刻度线
        _arrow(p, 88, 245, 88, 236)                        # H 下箭头
        _line(p, 262, 174, 352, 174)                       # T 上刻度线（端面上缘）
        _arrow(p, 346, 190, 346, 176)                      # T 下箭头（尖朝上）
        _arrow(p, 346, 185, 346, 199)                      # T 上箭头（尖朝下）
        _line(p, 180, 215, 180, 305)                       # W 延长线 x180
        _line(p, 193, 215, 193, 305)                       # W 延长线 x193
        _arrow(p, 155, 268, 177, 268)                      # W 左箭头
        _arrow(p, 227, 268, 196, 268)                      # W 右箭头
        _line(p, 215, 217, 215, 305)                       # G 延长线
        _arrow(p, 155, 298, 188, 298)                      # G 左箭头
        _arrow(p, 233, 298, 215, 298)                      # G 右箭头
        self.label(166, 136, "L")
        self.label(82, 287, "H")
        self.label(360, 217, "T")
        self.label(237, 275, "W")
        self.label(241, 303, "G")
        self.label(117, 233, "Ɛr")


class _DgTrough(_Diagram):
    NAME = "trough"

    def draw(self, p):
        # --- 板线（spec 12 §3；audit blocker 3：先板后杆，杆身渐变不被槽壁遮挡） ---
        # 上/下平行板（暗顶面 + 前端面 + 右侧长面 + 远端条 + 槽内壁）
        _poly(p, [(88, 182), (269, 147), (336, 147), (334, 149), (318, 153),
                  (195, 181)], "#7F7F7F", pw=2.0)              # 顶面暗灰
        _poly(p, [(88, 182), (191, 182), (191, 245), (88, 245)], "#CCCCCC",
              pw=1.5)                                           # 前端面
        _poly(p, [(191, 184), (340, 184), (340, 192), (334, 195), (195, 245),
                  (191, 245)], "#CCCCCC")                       # 右侧长面
        _poly(p, [(318, 153), (336, 147), (340, 147), (340, 184), (296, 183),
                  (280, 162)], "#CCCCCC")                       # 远端浅灰条
        _poly(p, [(134, 200), (195, 181), (318, 153), (280, 162)], "#CCCCCC")
        # 槽内壁浅灰条（先画，杆体后画才可见）
        # 圆杆柱体（金渐变）：上边 (128,202)→(280,162)，下边 (131,228)→(292,183)
        body = QtGui.QPainterPath()
        body.moveTo(128, 202)
        body.lineTo(280, 162)
        body.lineTo(292, 183)
        body.lineTo(131, 228)
        body.closeSubpath()
        p.setBrush(_grad(p, 128, 202, 280, 162, "#FFCF6C", "#FFFDC9"))
        _pen(p, "#000000", 1.5)
        p.drawPath(body)
        # 远端帽（#FFFBC5 填充椭圆 + 左上弧虚线）
        p.setBrush(QtGui.QColor("#FFFBC5"))
        _pen(p)
        p.drawEllipse(QtCore.QRectF(273, 167, 24, 20))
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QtCore.QRectF(273, 167, 24, 20), 30 * 16, 120 * 16)
        # 杆前端帽（最后画，保持完整）
        p.setBrush(QtGui.QColor("#FFD06E"))
        _pen(p, "#000000", 1.5)
        p.drawEllipse(QtCore.QRectF(105, 201, 29, 28))         # 前端帽 r14.5
        # 隐藏棱虚线（前端面底角 → 右侧面）
        _line(p, 88, 243, 334, 193, "#000000", 1.0, dash=[1, 2])
        # --- 尺寸线（spec 12 §4） ---
        _line(p, 88, 166, 247, 129, "#000000", 2.0)        # L 尺寸线（无箭头，本页特例）
        _line(p, 251, 125, 261, 142)                       # L 右刻度线
        _line(p, 41, 199, 104, 199)                        # D2 上刻度线
        _line(p, 53, 179, 53, 192)                         # D2 上箭头杆
        _head(p, 54, 198, 0.0, 1.0)
        _line(p, 41, 229, 104, 229)                        # D2 下刻度线
        _head(p, 53, 229, 0.0, -1.0)                       # D2 下箭头（尖朝上）
        _line(p, 52, 236, 52, 248)
        _line(p, 340, 184, 378, 184)                       # D1 上刻度线
        _line(p, 358, 191, 358, 203)                       # D1 上箭头杆
        _head(p, 357, 185, 0.0, -1.0)
        _line(p, 358, 230, 358, 239)                       # D1 下箭头杆
        _head(p, 358, 244, 0.0, 1.0)
        _line(p, 211, 247, 379, 247)                       # D1 下刻度线
        self.label(155, 129, "L")
        self.label(44, 273, "D2")
        self.label(351, 273, "D1")
        self.label(163, 245, "Ɛr")


class _DgWireGnd(_Diagram):
    NAME = "wire_gnd"

    def draw(self, p):
        # --- 圆导线悬于地板上方（spec 09 §3） ---
        # 导线体：上边 (176,186)→(326,151)，下边 (180,210)→(324,171)，尾端圆帽
        wire = QtGui.QPainterPath()
        wire.moveTo(176, 186)
        wire.lineTo(326, 151)
        wire.cubicTo(338, 148, 346, 155, 344, 162)
        wire.cubicTo(341, 168, 330, 172, 324, 171)
        wire.lineTo(180, 210)
        wire.closeSubpath()
        p.setBrush(_grad(p, 170, 198, 330, 161, "#B9B9B9", "#DEDEDE"))
        _pen(p)
        p.drawPath(wire)
        # 前端面白椭圆 + 黑十字
        p.setBrush(QtGui.QColor("#FFFFFF"))
        _pen(p)
        p.drawEllipse(QtCore.QRectF(158, 186, 25, 25))
        _cross(p, 170, 198)
        # 地板：水平渐变四边形 + 顶/底 2px 边
        p.setBrush(_grad(p, 98, 221, 378, 221, "#8C81FF", "#6AC4FF"))
        _pen(p)
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(98, 242), QtCore.QPointF(240, 201),
                                       QtCore.QPointF(378, 202), QtCore.QPointF(240, 242)]))
        _line(p, 98, 242, 240, 201, "#000000", 2.0)
        _line(p, 240, 202, 378, 202, "#000000", 2.0)
        _line(p, 240, 242, 378, 202, "#000000", 1.0)
        _line(p, 98, 242, 240, 242, "#000000", 2.0)
        # 基准延长线（audit cosmetic：实线细黑）
        _line(p, 40, 199, 153, 199, "#000000", 1.0)
        _line(p, 40, 242, 240, 242, "#000000", 1.0)
        # --- 尺寸线（spec 09 §4） ---
        _line(p, 64, 177, 64, 189)                         # H 上箭头杆
        _head(p, 64, 196, 0.0, 1.0)
        _head(p, 64, 246, 0.0, -1.0)                       # H 下箭头（尖朝上）
        _line(p, 64, 252, 64, 269)
        _line(p, 144, 144, 158, 173)                       # L 左刻度线
        _line(p, 318, 110, 333, 139)                       # L 右刻度线
        _arrow(p, 209, 153, 153, 159)                      # L 左箭头
        _arrow(p, 310, 130, 323, 127)                      # L 右箭头
        _line(p, 158, 205, 158, 281)                       # D 延长线 x158
        _line(p, 181, 205, 181, 281)                       # D 延长线 x181
        _arrow(p, 138, 269, 157, 269)                      # D 左箭头
        _arrow(p, 206, 269, 182, 269)                      # D 右箭头
        self.label(60, 173, "H")
        self.label(231, 128, "L")
        self.label(164, 275, "D")
        self.label(358, 187, "Ɛr")


# ---------------------------------------------------------------------------
# A 模板骨架与公共小件
# ---------------------------------------------------------------------------

def _page_css(name, bg):
    css = ""
    try:
        css = U.dialog_css(_PAL) or ""
    except Exception:
        css = ""
    css += (
        " QWidget#%(n)spg { background: %(bg)s; }"
        " QWidget#%(n)spg QLineEdit { background: #FFFFFF; border: 1px solid #808080;"
        "   border-top-color: #484848; border-left-color: #484848; padding: 1px 2px; }"
        " QWidget#%(n)spg QLineEdit:read-only { background: #FFFFFF; color: #000000; }"
        " QWidget#%(n)spg QPushButton { background: #ECECEC; border: 1px solid #808080;"
        "   border-top-color: #FFFFFF; border-left-color: #FFFFFF; padding: 3px 12px; }"
        " QWidget#%(n)spg QPushButton:pressed { background: #DBDBDB; }"
        " QWidget#%(n)spg QComboBox { background: #FFFFFF; border: 1px solid #808080;"
        "   padding: 1px 4px; }"
        " QWidget#%(n)spg QGroupBox { border: 1px solid #A0A0A0; margin-top: 8px; }"
        " QWidget#%(n)spg QTableWidget { background: #FFFFFF; gridline-color: %(gl)s; }"
        " QLabel#pgtitle { font-family: 'Times New Roman'; font-size: 22pt;"
        "   font-weight: bold; color: #000000; }"
        % {"n": name, "bg": bg, "gl": GRIDLINE}
    )
    return css


def _ql(text, align_right=False, w=None):
    lab = QtWidgets.QLabel(text)
    if align_right:
        lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if w:
        lab.setFixedWidth(w)
    return lab


def _num_box(obj, value="", w=54, ro=False):
    le = QtWidgets.QLineEdit(value)
    le.setObjectName(obj)
    le.setFixedWidth(w)
    le.setAlignment(Qt.AlignRight)
    if ro:
        le.setReadOnly(True)
        le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
    return le


def _res_row(page, key, label, unit="", combo=None, combo_name=None, editable=False,
             box_w=90, box_h=22):
    """结果行：标签右对齐 + “=” + 值框(实测外宽 box_w/高 box_h) + 单位 / 单位下拉。"""
    row = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    lab = _ql(label, align_right=True)
    lab.setMinimumWidth(140)
    h.addWidget(lab)
    h.addWidget(_ql("="))
    le = QtWidgets.QLineEdit()
    le.setReadOnly(not editable)
    le.setFixedSize(box_w, box_h)
    le.setAlignment(Qt.AlignRight)
    le.setStyleSheet("QLineEdit{background:#FFFFFF;border:1px solid #696969;"
                     "padding:0px 2px;}")
    le.setObjectName("%s.result.%s" % (page.name, key))
    page.results[key] = le
    h.addWidget(le)
    if combo:
        cb = QtWidgets.QComboBox()
        cb.addItems(combo)
        cb.setFixedWidth(78)
        if combo_name:
            cb.setObjectName(combo_name)
            page.combos[combo_name] = cb
        h.addWidget(cb)
    elif unit:
        h.addWidget(_ql(unit))
    h.addStretch(1)
    return row


def _mid_row(widgets, label=None, lw=0):
    row = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(row)
    h.setContentsMargins(0, 2, 0, 2)
    h.setSpacing(6)
    if label is not None:
        lab = _ql(label)
        if lw:
            lab.setFixedWidth(lw)
        h.addWidget(lab)
    for it in widgets:
        h.addWidget(it)
    h.addStretch(1)
    return row


def _spec_chrome(page, name):
    """AppCAD 页面公共 chrome：菜单条、底带、链接条、窗框（实测坐标）。"""
    bar = QtWidgets.QLabel(page)
    bar.setStyleSheet("background:#F3F3F3;")
    bar.setGeometry(0, 0, _CHROME.PAGE_W, 29)
    bar.show()
    mf = QtGui.QFont("MS Sans Serif", 8)
    for tx, word in ((20, "File"), (47, "Calculate"), (113, "Select Parameters"),
                     (231, "Options"), (292, "Help")):
        mw = QtWidgets.QLabel(word, page)
        mw.setFont(mf)
        mw.setStyleSheet("background:transparent; color:#000000;")
        mw.setGeometry(tx, 11, 140, 16)
        mw.show()
    sep = QtWidgets.QFrame(page)
    sep.setFrameShape(QtWidgets.QFrame.HLine)
    sep.setStyleSheet("color:#696969;")
    sep.setGeometry(0, 29, _CHROME.PAGE_W, 1)
    sep.show()
    band = QtWidgets.QLabel(page)
    band.setStyleSheet("background:#F0F0F0;")
    band.setGeometry(*_CHROME.WEBBAR)
    band.show()
    wc = _CHROME.WEB_COLORS.get(name)
    if wc:
        wb = QtWidgets.QLabel(_CHROME.WEB_TEXT, page)
        wb.setAlignment(Qt.AlignCenter)
        wb.setFont(QtGui.QFont("MS Sans Serif", 8))
        wb.setStyleSheet("background:%s; color:#000000;" % wc)
        wb.setGeometry(*_CHROME.WEBLINK)
        wb.show()
    for fr in ((0, 0, 7, 662), (959, 0, 7, 662), (0, 662, 966, 7)):
        f = QtWidgets.QLabel(page)
        f.setStyleSheet("background:#000000;")
        f.setGeometry(*fr)
        f.lower()
        f.show()


def _spec_diagram(name):
    """按实测规格（AppCAD 4.00 截图逐像素测量）构建 1:1 示意图。

    规格数据在 appcad_tl.py / appcad_circ.py；缺模块或该页无规格时返回 None，
    调用方回退到内置手绘 _Dg* 类。
    """
    if SpecDiagram is None:
        return None
    spec = (_CIRC_SPEC.PAGES.get(name) if _CIRC_SPEC else None) or \
           (_TL_SPEC.PAGES.get(name) if _TL_SPEC else None)
    if not spec:
        return None
    try:
        dg = SpecDiagram(spec)
    except Exception:
        return None
    # 测试锚点：findChild(QLineEdit, "<页名>.<key>") 需要完整 objectName；
    # 只读框是计算结果，沿用旧约定 "<页名>.result.<key>"
    bykey = {bx.get("key"): bx for bx in spec.get("boxes", [])}
    # 与旧手绘 _Dg* 的 obj= 约定保持一致（测试锚点依赖）
    _RES_ALIAS = {"bjt_base": {"icc": "result.ic"},
                  "fet_self": {"rs": "result.rs", "rd": "result.rd",
                               "idc": "result.idc", "vdsc": "result.vdsc"}}
    alias = _RES_ALIAS.get(name, {})
    for k, le in dg.fields.items():
        if "." in k:
            le.setObjectName(k)
        elif k in alias:
            le.setObjectName("%s.%s" % (name, alias[k]))
        elif bykey.get(k, {}).get("ro"):
            le.setObjectName("%s.result.%s" % (name, k))
        else:
            le.setObjectName("%s.%s" % (name, k))
    return dg


def _spec_shell(win, name, title, diagram, mids_fn=None, buttons_fn=None,
                rows_fn=None, bg=A_BG):
    """实测版式外壳：966×669 绝对坐标画布，对齐 AppCAD 4.00 截图。

    VB6 窗体是固定坐标布局——这正是旧版流式布局错乱的根源，因此这里
    全部用 setGeometry/move 绝对定位，不使用任何布局管理器。
    """
    page = QtWidgets.QWidget()
    page.setObjectName(name + "pg")
    page.name = name
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = diagram
    page.setFixedSize(_CHROME.PAGE_W, _CHROME.PAGE_H)
    bgc = diagram.spec.get("bg") or bg
    page.setStyleSheet("background:%s;" % bgc)

    diagram.setParent(page)
    diagram.move(0, 0)
    diagram.show()

    _spec_chrome(page, name)

    # chrome：标题 + Main Menu[F8]（AppCAD 是 Times 粗体 18pt，基线 y≈72）
    tx, ttext = _CHROME.TITLES.get(name, (30, title))
    tf = QtGui.QFont("Times New Roman", 18)
    tf.setBold(True)
    lab = QtWidgets.QLabel(ttext, page)
    lab.setFont(tf)
    lab.setStyleSheet("background:transparent; color:#000000;")
    lab.setGeometry(tx, 46, 560, 30)
    mm = QtWidgets.QPushButton("Main Menu [F8]", page)
    mm.setObjectName(name + ".mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    mm.setGeometry(*_CHROME.MAINMENU)

    lay = _CHROME.TL_LAYOUT.get(name) or _CHROME.CIRC_LAYOUT.get(name) or {}
    btns = list(buttons_fn(page)) if buttons_fn else []
    brects = lay.get("buttons") or []
    for i, b in enumerate(btns):
        b.setParent(page)
        if i < len(brects):
            b.setGeometry(*brects[i])
            b.show()
        else:
            # 实机没有的附加按钮（如微带 Calculate W [F3]，实机仅经 F3 触发）：
            # 保留对象供测试与快捷键触发，但不在页面上显示
            b.setVisible(False)
    if btns:
        page._calc_btn = btns[0]
    err = QtWidgets.QLabel("", page)
    err.setObjectName(name + ".err")
    err.setStyleSheet("background:transparent; color:#CC0000;")
    err.setGeometry(435, 165, 520, 20)
    err.hide()
    page.err = err

    # 中列（介电常数/基片/频率/长度单位），锚在实测 substrate 组合框位置
    mids = list(mids_fn(page)) if mids_fn else []
    if mids:
        cx, cy, cw, ch = lay.get("combo", (78, 323, 231, 18))
        # 实机行距（相对 substrate 组合框 y）：εr -29 / combo 0 / freq +37 / units +68
        for i, w in enumerate(mids):
            w.setParent(page)
            my = (cy - 29, cy, cy + 37, cy + 68)[min(i, 3)]
            w.setGeometry(max(8, cx - 2), my, 300, 24)
            w.show()

    # 右列结果行：锚在实测首行框位，步进 res_dy
    rows = list(rows_fn(page)) if rows_fn else []
    rx = lay.get("res_x", 518)
    ry = lay.get("res_y", 252)
    dy = lay.get("res_dy", 27)
    for i, row in enumerate(rows):
        row.setParent(page)
        # 首行是 Z0 结果行（兼综合目标输入），实机在结果区上方 y≈ry-52；
        # 其余结果行自 ry-1 起步进 res_dy（参考图实测 251/278/305…）
        y0 = (ry - 52) if i == 0 else (ry - 1 + (i - 1) * dy)
        row.setGeometry(rx - 92, y0, 300, 24)
        # 标签列实测最宽 74px（"1.0 Wavelength ="），右对齐止于 x≈500
        labs = row.findChildren(QtWidgets.QLabel)
        if labs:
            labs[0].setFixedWidth(74)
        row.show()

    return page


def _a_shell(win, name, title, diagram, mids_fn=None, buttons_fn=None, rows_fn=None,
             bg=A_BG):
    """A 模板：标题 + Main Menu[F8]；左=结构图+中列；右=按钮+错误行+结果行。"""
    if SpecDiagram is not None and isinstance(diagram, SpecDiagram):
        page = _spec_shell(win, name, title, diagram, mids_fn, buttons_fn,
                           rows_fn, bg)
        win._pages[name] = page
        win._order.append(name)
        win._stack.addWidget(page)
        return page
    page = QtWidgets.QWidget()
    page.setObjectName(name + "pg")
    page.setStyleSheet(_page_css(name, bg))
    page.name = name
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = diagram

    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 2)
    v.setSpacing(4)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 22)
    tf.setBold(True)
    lab = QtWidgets.QLabel(title)
    lab.setObjectName("pgtitle")
    lab.setFont(tf)
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName(name + ".mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(lab)
    top.addStretch(1)
    top.addWidget(mm, 0, Qt.AlignTop)
    v.addLayout(top)

    body = QtWidgets.QHBoxLayout()
    body.setSpacing(6)
    left = QtWidgets.QVBoxLayout()
    if diagram is not None:
        left.addWidget(diagram)
    for it in (mids_fn(page) if mids_fn else []):
        left.addWidget(it)
    left.addStretch(1)
    right = QtWidgets.QVBoxLayout()
    right.setSpacing(5)
    for b in (buttons_fn(page) if buttons_fn else []):
        right.addWidget(b, 0, Qt.AlignLeft)
    err = QtWidgets.QLabel("")
    err.setObjectName(name + ".error")
    err.setStyleSheet("color:%s;font-weight:bold;" % _ERR)
    err.hide()
    page.err = err
    right.addWidget(err, 0, Qt.AlignLeft)
    for it in (rows_fn(page) if rows_fn else []):
        right.addWidget(it, 0, Qt.AlignLeft)
    right.addStretch(1)
    body.addLayout(left, 0)
    body.addStretch(1)                     # AppCAD 版式：右列贴窗口右缘
    body.addLayout(right, 0)
    v.addLayout(body, 1)
    win._pages[name] = page
    win._order.append(name)
    win._stack.addWidget(page)
    return page


def _calc_btn(page, text, fn, obj):
    b = QtWidgets.QPushButton(text)
    b.setObjectName(obj)
    b.clicked.connect(lambda: _run_calc(page, fn))
    return b


def _run_calc(page, fn, ok_msg=""):
    try:
        page.err.hide()
        msg = fn()
        if ok_msg or msg:
            page.window().status().showMessage("%s：%s" % (page.name, msg or ok_msg), 8000)
    except _InputError as exc:
        page.err.setText("✗ " + str(exc))
        page.err.show()
        page.window().status().showMessage("%s：失败——%s" % (page.name, exc))
    except Exception as exc:  # UI 解析层兜底，绝不崩
        page.err.setText("✗ 界面错误：%s: %s" % (type(exc).__name__, exc))
        page.err.show()
        page.window().status().showMessage(
            "%s：失败——%s: %s" % (page.name, type(exc).__name__, exc))


# --- 中列（Dielectric / 基片 / Freq / Length Units）--------------------------

def _er_mid(page):
    er = _num_box(page.name + ".er", "1")
    page.er_box = er
    combo = QtWidgets.QComboBox()
    combo.setObjectName(page.name + ".substrate")
    e = _E()
    names = ["Free Space"] + ([s["name"] for s in e.SUBSTRATES] if e else [])
    combo.addItems(names)
    combo.setFixedWidth(210)

    def pick(idx):
        if idx <= 0 or not e:
            return
        sub = e.SUBSTRATES[idx - 1]
        er.setText(_g(sub["er"]))
        w = page.window()
        if w is not None:
            w._last_sub = sub

    combo.currentIndexChanged.connect(pick)
    return [_mid_row([er], label="Dielectric:  εr ="),
            _mid_row([combo])]


def _freq_mid(page, default="1"):
    page.freq_box = _num_box(page.name + ".freq", default)
    fu = QtWidgets.QComboBox()
    fu.setObjectName(page.name + ".funit")
    fu.addItems(_FREQ_ITEMS)
    fu.setFixedWidth(96)
    page.combos[page.name + ".funit"] = fu
    un = QtWidgets.QComboBox()
    un.setObjectName(page.name + ".units")
    un.addItems(_LEN_ITEMS)
    un.setFixedWidth(96)
    page.combos[page.name + ".units"] = un
    page._ulen_cur = _LEN_ALIAS[_LEN_ITEMS[0]]
    page.ulen = page._ulen_cur
    # 页面装配方（mids_fn）先设 len_fields；此处仅在缺失时兜底初始化
    if not hasattr(page, "len_fields"):
        page.len_fields = []
    un.currentTextChanged.connect(lambda t: _on_unit_changed(page, t))
    return [_mid_row([page.freq_box, fu], label="Frequency:", lw=76),
            _mid_row([un], label="Length Units:", lw=76)]


def _on_unit_changed(page, text):
    newk = _LEN_ALIAS.get(text, "mil")
    old = getattr(page, "_ulen_cur", newk)
    e = _E()
    if e and old != newk:
        scale = e.UNITS["len"]
        for key in getattr(page, "len_fields", []):
            le = page.dg.fields.get(key) if page.dg is not None else None
            if le is None:
                continue
            try:
                si = e.to_si(le.text(), old, "len")
            except Exception:
                continue
            le.setText(_g(si / scale[newk]))
    page._ulen_cur = newk
    page.ulen = newk
    for lab in getattr(page, "unit_labels", []):
        lab.setText(text)


def _L(page, meters):
    return meters / _e().UNITS["len"][page.ulen]


def _flen(page, key):
    return _e().to_si(_f(page.dg.fields[key], key), page.ulen, "len")


def _fLopt(page, key):
    v = _fopt(page.dg.fields[key], 0.0, key)
    return _e().to_si(v, page.ulen, "len") if v > 0 else 0.0


def _ffreq(page):
    v = _fopt(page.freq_box, 0.0, "频率")
    if v <= 0:
        return 0.0
    fu = page.combos.get(page.name + ".funit")
    return _e().to_si(v, fu.currentText() if fu else "GHz", "freq")


def _setr(page, mapping):
    for k, v in mapping.items():
        le = page.results.get(k)
        if le is not None:
            le.setText(_g(v))


def _lam_rows(page, lam_g, L, eeff, delay=True):
    """电长度行族（λ/角度/长度/时延/1.0波长/Vp）—— 由引擎输出在 UI 侧派生。"""
    out = {}
    if not lam_g or lam_g <= 0:
        for k in ("elen_lambda", "elen_deg", "elen_len", "wavelength"):
            out[k] = None
        if delay:
            out["delay"] = None
        out["vp"] = (1.0 / math.sqrt(eeff)) if eeff else None
        return out
    out["wavelength"] = _L(page, lam_g)
    out["vp"] = 1.0 / math.sqrt(eeff) if eeff else None
    if not L or L <= 0:
        for k in ("elen_lambda", "elen_deg", "elen_len"):
            out[k] = None
        if delay:
            out["delay"] = None
        return out
    out["elen_lambda"] = L / lam_g
    unit = page.combos.get(page.name + ".degunit")
    mode = unit.currentText() if unit else "degrees"
    out["elen_deg"] = (math.degrees(2.0 * math.pi * L / lam_g) if mode == "degrees"
                       else 2.0 * math.pi * L / lam_g)
    out["elen_len"] = _L(page, L)
    if delay:
        tu = page.combos.get(page.name + ".timeunit")
        ts = _TIME_SCALE.get(tu.currentText(), 1e-9) if tu else 1e-9
        out["delay"] = (L * math.sqrt(eeff) / C0) / ts if eeff else None
    return out


# --- 传输线页公共装配 --------------------------------------------------------

def _tl_rows(page, res_spec, z0t=True):
    lay = (_CHROME.TL_LAYOUT.get(page.name) if _CHROME is not None else None) or {}
    rw = int(lay.get("res_w", 71)) + 2      # 参考图实测结果框外宽（含 1px 边框）
    rows = []
    for key, label, unit in res_spec:
        if key == "@deg":
            rows.append(_res_row(page, "elen_deg", label, combo=["degrees", "radians"],
                                 combo_name=page.name + ".degunit", box_w=rw, box_h=19))
        elif key == "@time":
            rows.append(_res_row(page, "delay", label, combo=list(_TIME_ITEMS),
                                 combo_name=page.name + ".timeunit", box_w=rw, box_h=19))
        elif key in ("elen_len", "wavelength"):
            tmp = _res_row(page, key, label, box_w=rw, box_h=19)
            ul = _ql(_LEN_ITEMS[0])
            page.unit_labels.append(ul)
            tmp.layout().insertWidget(3, ul)
            rows.append(tmp)
        elif z0t and key == "Z0":
            # AppCAD 的 Z0 框兼作综合目标输入（输入目标值按 F3），同框可编辑
            row = _res_row(page, key, label, unit, editable=True, box_w=73, box_h=24)
            page.z0t = page.results["Z0"]
            rows.append(row)
        else:
            rows.append(_res_row(page, key, label, unit, box_w=rw, box_h=19))
    return rows


def _generic_tl_page(win, name, title, dg, dim_keys, calc_fn, syn_fn, syn_label,
                     res_spec, z0t=True):
    def mids(page):
        page.len_fields = list(dim_keys)
        return _er_mid(page) + _freq_mid(page)

    def buttons(page):
        bs = [_calc_btn(page, "Calculate Z0 [F4]", lambda: calc_fn(page), name + ".calc")]
        if syn_fn is not None:
            bs.append(_calc_btn(page, syn_label, lambda: syn_fn(page), name + ".calc2"))
        return bs

    rows_fn = (lambda page: _tl_rows(page, res_spec, z0t=z0t))
    return _a_shell(win, name, title, dg, mids, buttons, rows_fn)


# --- 微带 --------------------------------------------------------------------

def _mk_microstrip(win):
    dg = _spec_diagram("microstrip")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgMicrostrip()
        dg.box("W", 175, 144, "2", w=34, h=15)
        dg.box("H", 70, 209, "6", w=46, h=15)
        dg.box("T", 167, 246, "2.1", w=52, h=15, ro=True)
        dg.box("L", 299, 225, "1000", w=46, h=15)
        dg.finalize()

    def mids(page):
        page.len_fields = ["W", "H", "L"]
        return _er_mid(page) + _freq_mid(page)

    def buttons(page):
        return [_calc_btn(page, "Calculate Z0 [F4]", lambda: _calc_ms(page),
                          "microstrip.calc"),
                _calc_btn(page, "Calculate W [F3]", lambda: _calc_ms_syn(page),
                          "microstrip.calc2")]

    def rows(page):
        lay = (_CHROME.TL_LAYOUT.get("microstrip") if _CHROME is not None else None) or {}
        rw = int(lay.get("res_w", 91)) + 2
        # AppCAD 的 Z0 框兼作 Calculate W [F3] 的目标输入，同框可编辑
        z0 = _res_row(page, "Z0", "Z0", "Ω", editable=True, box_w=73, box_h=24)
        page.z0t = page.results["Z0"]
        out = [z0, _res_row(page, "elen_lambda", "Elect Length", "λ",
                            box_w=rw, box_h=19)]
        out.append(_res_row(page, "elen_deg", "Elect Length",
                            combo=["degrees", "radians"],
                            combo_name="microstrip.degunit", box_w=rw, box_h=19))
        tmp = _res_row(page, "elen_len", "Elect Length", box_w=rw, box_h=19)
        ul = _ql(_LEN_ITEMS[0])
        page.unit_labels.append(ul)
        tmp.layout().insertWidget(3, ul)
        out.append(tmp)
        out.append(_res_row(page, "delay", "Delay", combo=list(_TIME_ITEMS),
                            combo_name="microstrip.timeunit", box_w=rw, box_h=19))
        tmp2 = _res_row(page, "wavelength", "1.0 Wavelength", box_w=rw, box_h=19)
        ul2 = _ql(_LEN_ITEMS[0])
        page.unit_labels.append(ul2)
        tmp2.layout().insertWidget(3, ul2)
        out.append(tmp2)
        out.append(_res_row(page, "vp", "Vp", "fraction of c", box_w=rw, box_h=19))
        out.append(_res_row(page, "eeff", "Ɛeff", box_w=rw, box_h=19))
        out.append(_res_row(page, "w_over_h", "W/H", box_w=rw, box_h=19))
        return out

    return _a_shell(win, "microstrip", "微带线 Microstrip", dg, mids, buttons, rows)


def _calc_ms(page):
    e = _e()
    w = _flen(page, "W")
    h = _flen(page, "H")
    er = _f(page.er_box, "εr")
    f = _ffreq(page)
    L = _fLopt(page, "L")
    r = e.microstrip_analysis(w, h, er, 0.0, f)
    if not r.get("ok"):
        raise _InputError(r.get("error", "引擎返回失败"))
    out = {"Z0": r.get("Z0"), "w_over_h": r.get("w_over_h"), "eeff": r.get("eeff")}
    out.update(_lam_rows(page, r.get("lambda_g"), L, r.get("eeff"), delay=True))
    _setr(page, out)
    return "Z0=%s Ω" % _g(r.get("Z0"))


def _calc_ms_syn(page):
    e = _e()
    zt = _f(page.z0t, "目标 Z0")
    h = _flen(page, "H")
    er = _f(page.er_box, "εr")
    f = _ffreq(page)
    r = e.microstrip_synthesis(zt, h, er, 0.0, f)
    if not r.get("ok"):
        raise _InputError(r.get("error", "引擎返回失败"))
    page.dg.fields["W"].setText(_g(_L(page, r["w"])))
    _calc_ms(page)
    return "综合 W=%s %s（Z0 残差 %s）" % (_g(_L(page, r["w"])), page.ulen,
                                         _g(r.get("Z0_resid")))


# --- 带状线 ------------------------------------------------------------------

def _mk_stripline(win):
    dg = _spec_diagram("stripline")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgStripline()
        dg.box("B", 72, 215, "7", w=46, h=15)
        dg.box("W", 179, 249, "8", w=37, h=15)
        dg.box("T", 329, 248, "0.01", w=44, h=15, ro=True)
        dg.box("L", 146, 160, "1000", w=46, h=15)
        dg.finalize()

    def calc(page):
        e = _e()
        w = _flen(page, "W")
        b = _flen(page, "B")
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        L = _fLopt(page, "L")
        r = e.stripline_analysis(w, b, er, 0.0, f)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        out = {"Z0": r.get("Z0"), "w_over_h": r.get("w_over_b"), "eeff": er}
        out.update(_lam_rows(page, r.get("lambda_g"), L, er, delay=False))
        _setr(page, out)
        return "Z0=%s Ω" % _g(r.get("Z0"))

    def syn(page):
        e = _e()
        zt = _f(page.z0t, "目标 Z0")
        b = _flen(page, "B")
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        r = e.stripline_synthesis(zt, b, er, 0.0, f)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        page.dg.fields["W"].setText(_g(_L(page, r["w"])))
        calc(page)
        return "综合 W=%s %s" % (_g(_L(page, r["w"])), page.ulen)

    return _generic_tl_page(
        win, "stripline", "带状线 Stripline", dg, ["W", "B", "L"], calc, syn,
        "Calculate W [F3]",
        [("Z0", "Z0", "Ω"), ("elen_lambda", "Elect Length", "λ"),
         ("@deg", "Elect Length", None),
         ("wavelength", "1.0 Wavelength", None), ("vp", "Vp", "fraction of c"),
         ("eeff", "Ɛeff", None), ("w_over_h", "W/H", None)])


# --- 圆同轴 ------------------------------------------------------------------

def _mk_coax_round(win):
    dg = _spec_diagram("coax_round")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgCoaxRound()
        # AppCAD 口径：D1 = 外导体内径（大）、D2 = 内导体外径（小）
        dg.box("D2", 90, 204, "43", w=46, h=15)
        dg.box("D1", 289, 206, "100", w=46, h=15)
        dg.box("L", 186, 140, "1000", w=47, h=15)
        dg.finalize()

    def calc(page):
        e = _e()
        d1 = _flen(page, "D1")   # 外导体内径（引擎 b*2）
        d2 = _flen(page, "D2")   # 内导体外径（引擎 a*2）
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        L = _fLopt(page, "L")
        r = e.coax_analysis(d2 / 2.0, d1 / 2.0, er, 0.0, f)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        out = {"Z0": r.get("Z0"), "d1d2": r.get("b_over_a"),
               "fc": (r.get("fc_te11") or 0.0) / 1e9}
        out.update(_lam_rows(page, r.get("lambda_g"), L, er, delay=True))
        _setr(page, out)
        return "Z0=%s Ω" % _g(r.get("Z0"))

    def syn(page):
        # 引擎口径：给定目标 Z0 与外导体内径 D1 → 内导体外径 D2（只回填 D2）
        e = _e()
        zt = _f(page.z0t, "目标 Z0")
        d1 = _flen(page, "D1")
        er = _f(page.er_box, "εr")
        r = e.coax_synthesis(zt, d1 / 2.0, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        page.dg.fields["D2"].setText(_g(_L(page, r["a"] * 2.0)))
        calc(page)
        return "综合 D2=%s %s" % (_g(_L(page, r["a"] * 2.0)), page.ulen)

    return _generic_tl_page(
        win, "coax_round", "同轴（圆）Coax (Round)", dg, ["D1", "D2", "L"], calc, syn,
        "Calculate D2 [F3]",
        [("Z0", "Z0", "Ω"), ("elen_lambda", "Elect Length", "λ"),
         ("@deg", "Elect Length", None), ("elen_len", "Elect Length", None),
         ("@time", "Delay", None), ("wavelength", "1.0 Wavelength", None),
         ("vp", "Vp", "fraction of c"), ("d1d2", "D1/D2", None),
         ("fc", "fc TE11", "GHz")])


# --- 方同轴 ------------------------------------------------------------------

def _mk_coax_square(win):
    dg = _spec_diagram("coax_square")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgCoaxSquare()
        # AppCAD 口径：D1 = 外方管边长（大）、D2 = 内导体外径（小）
        dg.box("D2", 87, 210, "43", w=46, h=15)
        dg.box("D1", 291, 210, "100", w=46, h=15)
        dg.box("L", 163, 143, "1000", w=47, h=15)
        dg.finalize()

    def calc(page):
        e = _e()
        s = _flen(page, "D1")    # 外方管边长（引擎 s）
        d2 = _flen(page, "D2")   # 内导体外径（引擎 a*2）
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        L = _fLopt(page, "L")
        r = e.sq_coax_analysis(d2 / 2.0, s, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        lam = (C0 / (f * math.sqrt(er))) if f > 0 and er > 0 else None
        out = {"Z0": r.get("Z0"), "d1d2": (s / d2) if d2 else None}
        out.update(_lam_rows(page, lam, L, er, delay=False))
        _setr(page, out)
        return "Z0=%s Ω" % _g(r.get("Z0"))

    def syn(page):
        # 给定目标 Z0 与外方管边长 D1 → 内导体外径 D2（只回填 D2）
        e = _e()
        zt = _f(page.z0t, "目标 Z0")
        s = _flen(page, "D1")
        er = _f(page.er_box, "εr")
        r = e.sq_coax_synthesis(zt, s, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        page.dg.fields["D2"].setText(_g(_L(page, r["a"] * 2.0)))
        calc(page)
        return "综合 D2=%s %s" % (_g(_L(page, r["a"] * 2.0)), page.ulen)

    def mids(page):
        page.len_fields = ["D1", "D2", "L"]
        return _er_mid(page) + _freq_mid(page)

    def buttons(page):
        return [_calc_btn(page, "Calculate Z0 [F2]", lambda: calc(page),
                          "coax_square.calc"),
                _calc_btn(page, "Calculate D2 [F3]", lambda: syn(page),
                          "coax_square.calc2")]

    def rows(page):
        out = [_ql("注：Z0 由引擎方同轴等效半径模型（b_eq = 0.539038·s，常数按 "
                   "AppCAD 4.00 实机定标）解出；结果行 D1/D2 为几何比 "
                   "s/(2a)=D1/D2，与引擎的等效外径比 b_eq/a 不同。")]
        out += _tl_rows(page, [("Z0", "Z0", "Ω"),
                               ("elen_lambda", "Elect Length", "λ"),
                               ("@deg", "Elect Length", None),
                               ("wavelength", "1.0 Wavelength", None),
                               ("vp", "Vp", "fraction of c"),
                               ("d1d2", "D1/D2", None)])
        return out

    return _a_shell(win, "coax_square", "同轴（方）Coax (Square)",
                    dg, mids, buttons, rows)


# --- 平行双线 ----------------------------------------------------------------

def _mk_twin(win):
    dg = _spec_diagram("parallel")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgTwinWire()
        dg.box("D", 345, 190, "15", w=47, h=15)
        dg.box("S", 116, 231, "100", w=47, h=15)
        dg.box("L", 116, 137, "1000", w=47, h=15)
        dg.finalize()

    def calc(page):
        e = _e()
        d = _flen(page, "D")   # 导线直径（引擎形参 d）
        s = _flen(page, "S")   # 中心距（引擎形参 D）
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        L = _fLopt(page, "L")
        r = e.twin_wire_analysis(d, s, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        lam = (C0 / (f * math.sqrt(er))) if f > 0 and er > 0 else None
        out = {"Z0": r.get("Z0"), "sd": r.get("D_over_d")}
        out.update(_lam_rows(page, lam, L, er, delay=False))
        _setr(page, out)
        return "Z0=%s Ω" % _g(r.get("Z0"))

    def syn(page):
        # 引擎口径：给定目标 Z0 与中心距 S → 导线直径 D（按钮文案保留实机 D2）
        e = _e()
        zt = _f(page.z0t, "目标 Z0")
        s = _flen(page, "S")
        er = _f(page.er_box, "εr")
        r = e.twin_wire_synthesis(zt, s, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        page.dg.fields["D"].setText(_g(_L(page, r["d"])))
        calc(page)
        return "综合 D=%s %s" % (_g(_L(page, r["d"])), page.ulen)

    return _generic_tl_page(
        win, "twin_wire", "平行双线 Parallel Wire Line", dg, ["D", "S", "L"], calc, syn,
        "Calculate D2 [F3]",
        [("Z0", "Z0", "Ω"), ("elen_lambda", "Elect Length", "λ"),
         ("@deg", "Elect Length", None), ("wavelength", "1.0 Wavelength", None),
         ("vp", "Vp", "fraction of c"), ("sd", "S/D", None)])


# --- 线过地板 ----------------------------------------------------------------

def _mk_wire_gnd(win):
    dg = _spec_diagram("wire_gnd")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgWireGnd()
        dg.box("H", 35, 211, "50", w=46, h=15)
        dg.box("D", 209, 260, "5", w=46, h=15)
        dg.box("L", 213, 135, "1000", w=47, h=15)
        dg.finalize()

    def calc(page):
        e = _e()
        h = _flen(page, "H")
        d = _flen(page, "D")
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        L = _fLopt(page, "L")
        r = e.wire_over_ground_analysis(d, h, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        lam = (C0 / (f * math.sqrt(er))) if f > 0 and er > 0 else None
        out = {"Z0": r.get("Z0"), "hd": r.get("twoh_over_d")}
        out.update(_lam_rows(page, lam, L, er, delay=False))
        _setr(page, out)
        return "Z0=%s Ω" % _g(r.get("Z0"))

    def syn(page):
        # Calculate H：给定目标 Z0 与线径 D，镜像法同式反解 h = (d/2)·cosh(Z0·√εr/60)
        zt = _f(page.z0t, "目标 Z0")
        d = _flen(page, "D")
        er = _f(page.er_box, "εr")
        if d <= 0:
            raise _InputError("线径 D 必须大于 0")
        h = (d / 2.0) * math.cosh(zt * math.sqrt(er) / 60.0)
        page.dg.fields["H"].setText(_g(_L(page, h)))
        calc(page)
        return "综合 H=%s %s" % (_g(_L(page, h)), page.ulen)

    return _generic_tl_page(
        win, "wire_gnd", "线过地板 Wire Over Groundplane", dg, ["H", "D", "L"],
        calc, syn, "Calculate H [F3]",
        [("Z0", "Z0", "Ω"), ("elen_lambda", "Elect Length", "λ"),
         ("@deg", "Elect Length", None),
         ("wavelength", "1.0 Wavelength", None), ("vp", "Vp", "fraction of c"),
         ("hd", "H/D", None)])


# --- CPW ---------------------------------------------------------------------

def _cpw_calc(page):
    e = _e()
    s = _flen(page, "W")
    g = _flen(page, "G")
    combo = page.combos.get("cpw.hmode")
    thick = combo is not None and combo.currentIndex() == 1
    h = 1e9 if thick else _flen(page, "H")
    er = _f(page.er_box, "εr")
    f = _ffreq(page)
    L = _fLopt(page, "L")
    r = e.cpw_analysis(s, g, h, er)
    if not r.get("ok"):
        raise _InputError(r.get("error", "引擎返回失败"))
    eeff = r.get("eeff")
    lam = (C0 / (f * math.sqrt(eeff))) if f > 0 and eeff else None
    out = {"Z0": r.get("Z0"), "eeff": eeff, "shape": None}
    out.update(_lam_rows(page, lam, L, eeff, delay=True))
    _setr(page, out)
    return "Z0=%s Ω" % _g(r.get("Z0"))


def _mk_cpw(win):
    dg = _spec_diagram("cpw")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgCpw()
        dg.box("L", 146, 146, "1000", w=46, h=15)
        dg.box("H", 52, 219, "25", w=46, h=15)
        dg.box("W", 267, 261, "10", w=40, h=15)
        dg.box("G", 266, 291, "5", w=40, h=15)
        dg.box("T", 324, 233, "0.1", w=47, h=15, ro=True)
        dg.finalize()

    def mids(page):
        page.len_fields = ["W", "G", "H", "L"]
        rows = _er_mid(page) + _freq_mid(page)
        # 实机 06 号截图标题右侧单选（With/No Groundplane）→ 语义映射：
        # 无限厚基片（引擎默认，h 不可编辑）/ 有限厚 h（可编辑，Ɛeff 修正）
        combo = QtWidgets.QComboBox()
        combo.setObjectName("cpw.hmode")
        combo.addItems(["基片：无限厚(默认)", "有限厚 h"])
        combo.setFixedWidth(180)
        page.combos["cpw.hmode"] = combo

        def mode_changed(idx):
            dg.fields["H"].setEnabled(idx == 1)

        combo.currentIndexChanged.connect(mode_changed)
        rows.insert(2, _mid_row([combo], label="基片:", lw=40))
        return rows

    def buttons(page):
        return [_calc_btn(page, "Calculate Z0 [F4]", lambda: _cpw_calc(page), "cpw.calc")]

    def rows(page):
        out = [_ql("注：本页为引擎的无底部地板标准 CPW 模型；实机 AppCAD 的 "
                   "With/No Groundplane 指底地板选项，引擎未建模。下方「基片高度"
                   "模式」仅切换基片高度对 Ɛeff 的修正（有限 h 可编辑 / 厚基片 h→∞）。")]
        out.append(_res_row(page, "Z0", "Z0", "Ω"))
        out.append(_res_row(page, "elen_lambda", "Elect Length", "λ"))
        out.append(_res_row(page, "elen_deg", "Elect Length", combo=["degrees", "radians"],
                            combo_name="cpw.degunit"))
        tmp = _res_row(page, "elen_len", "Elect Length")
        ul = _ql(_LEN_ITEMS[0])
        page.unit_labels.append(ul)
        tmp.layout().insertWidget(3, ul)
        out.append(tmp)
        out.append(_res_row(page, "delay", "Delay", combo=list(_TIME_ITEMS),
                            combo_name="cpw.timeunit"))
        tmp2 = _res_row(page, "wavelength", "1.0 Wavelength")
        ul2 = _ql(_LEN_ITEMS[0])
        page.unit_labels.append(ul2)
        tmp2.layout().insertWidget(3, ul2)
        out.append(tmp2)
        out.append(_res_row(page, "vp", "Vp", "fraction of c"))
        out.append(_res_row(page, "eeff", "Ɛeff"))
        tmp3 = _res_row(page, "shape", "Shape factor")
        page.results["shape"].setStyleSheet("background:%s;color:#777777;" % RO_BG)
        out.append(tmp3)
        return out

    return _a_shell(win, "cpw", "共面波导 Coplanar Waveguide（无底部地板模型）",
                    dg, mids, buttons, rows)


# --- 板线（槽线） ------------------------------------------------------------

def _mk_trough(win):
    dg = _spec_diagram("trough")
    if dg is None:  # 规格缺失时回退手绘
        dg = _DgTrough()
        # AppCAD 口径：D1 = 槽宽/板距（大，引擎 b）、D2 = 线径（小，引擎 d）
        dg.box("D2", 35, 204, "43", w=46, h=15)
        dg.box("D1", 342, 211, "100", w=46, h=15)
        dg.box("L", 136, 134, "1000", w=47, h=15)
        dg.finalize()

    def calc(page):
        e = _e()
        b = _flen(page, "D1")    # 槽宽（引擎 b）
        d = _flen(page, "D2")    # 线径（引擎 d）
        er = _f(page.er_box, "εr")
        f = _ffreq(page)
        L = _fLopt(page, "L")
        r = e.slabline_analysis(d, b, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        lam = (C0 / (f * math.sqrt(er))) if f > 0 and er > 0 else None
        dob = r.get("d_over_b")
        out = {"Z0": r.get("Z0"), "d1d2": (1.0 / dob) if dob else None}
        out.update(_lam_rows(page, lam, L, er, delay=False))
        _setr(page, out)
        return "Z0=%s Ω" % _g(r.get("Z0"))

    def syn(page):
        # 给定目标 Z0 与槽宽 D1 → 线径 D2（只回填 D2）
        e = _e()
        zt = _f(page.z0t, "目标 Z0")
        b = _flen(page, "D1")
        er = _f(page.er_box, "εr")
        r = e.slabline_synthesis(zt, b, er)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        page.dg.fields["D2"].setText(_g(_L(page, r["d"])))
        calc(page)
        return "综合 D2=%s %s" % (_g(_L(page, r["d"])), page.ulen)

    def mids(page):
        page.len_fields = ["D1", "D2", "L"]
        return _er_mid(page) + _freq_mid(page)

    def buttons(page):
        return [_calc_btn(page, "Calculate Z0 [F4]", lambda: calc(page),
                          "trough.calc"),
                _calc_btn(page, "Calculate D2 [F3]", lambda: syn(page),
                          "trough.calc2")]

    def rows(page):
        out = [_ql("注：结果行 D1/D2 为几何比（槽宽 D1 / 线径 D2）；Z0 由引擎"
                   "圆杆-平行板模型解出。")]
        out += _tl_rows(page, [("Z0", "Z0", "Ω"),
                               ("elen_lambda", "Elect Length", "λ"),
                               ("@deg", "Elect Length", None),
                               ("wavelength", "1.0 Wavelength", None),
                               ("vp", "Vp", "fraction of c"),
                               ("d1d2", "D1/D2", None)])
        return out

    return _a_shell(win, "trough", "板线（槽线）Trough Line",
                    dg, mids, buttons, rows)


# ---------------------------------------------------------------------------
# B 模板（白底偏置页）公共件
# ---------------------------------------------------------------------------

def _b_shell(win, name, title):
    page = QtWidgets.QWidget()
    page.setObjectName(name + "pg")
    page.setStyleSheet(_page_css(name, B_BG))
    page.name = name
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None
    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 2)
    v.setSpacing(4)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 20)
    tf.setBold(True)
    lab = QtWidgets.QLabel(title)
    lab.setObjectName("pgtitle")
    lab.setFont(tf)
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName(name + ".mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(lab)
    top.addStretch(1)
    top.addWidget(mm, 0, Qt.AlignTop)
    v.addLayout(top)
    body = QtWidgets.QHBoxLayout()
    body.setSpacing(10)
    left = QtWidgets.QVBoxLayout()
    right = QtWidgets.QVBoxLayout()
    right.setSpacing(6)
    body.addLayout(left, 1)
    body.addLayout(right, 1)
    v.addLayout(body, 1)
    page._left = left
    page._right = right
    page.err = QtWidgets.QLabel("")
    page.err.setObjectName(name + ".error")
    page.err.setStyleSheet("color:%s;font-weight:bold;" % _ERR)
    page.err.hide()
    win._pages[name] = page
    win._order.append(name)
    win._stack.addWidget(page)
    return page


def _grp(title):
    gb = QtWidgets.QGroupBox(title)
    lay = QtWidgets.QVBoxLayout(gb)
    lay.setContentsMargins(8, 4, 8, 6)
    lay.setSpacing(3)
    return gb, lay


def _grow(label, box, unit=""):
    row = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    lab = _ql(label, align_right=True)
    lab.setMinimumWidth(120)
    h.addWidget(lab)
    h.addWidget(box)
    if unit:
        h.addWidget(_ql(unit))
    h.addStretch(1)
    return row


def _btable(row_labels, obj, headers=None):
    ncol = 4 if headers is None else len(headers) + 1
    t = QtWidgets.QTableWidget(len(row_labels), ncol)
    t.setObjectName(obj)
    t.horizontalHeader().setVisible(False)
    t.verticalHeader().setVisible(False)
    t.setMinimumHeight(max(88, 24 * (len(row_labels) + 1)))
    for i, lab in enumerate(row_labels):
        it = QtWidgets.QTableWidgetItem(lab)
        it.setFlags(Qt.ItemIsEnabled)
        it.setBackground(QtGui.QColor(GRIDHEAD))
        t.setItem(i, 0, it)
        for c in range(1, ncol):
            cell = QtWidgets.QTableWidgetItem("-")
            cell.setFlags(Qt.ItemIsEnabled)
            cell.setBackground(QtGui.QColor("#F5F5F5"))
            cell.setForeground(QtGui.QColor("#909090"))
            cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(i, c, cell)
    return t



def _scan_table(headers, obj, height=110):
    """表头内置的扫描结果表（第 0 行为表头，其余为数据行）。"""
    t = QtWidgets.QTableWidget(1, len(headers))
    t.setObjectName(obj)
    t.horizontalHeader().setVisible(False)
    t.verticalHeader().setVisible(False)
    t.setMinimumHeight(height)
    t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    for c, head in enumerate(headers):
        it = QtWidgets.QTableWidgetItem(head)
        it.setFlags(Qt.ItemIsEnabled)
        it.setBackground(QtGui.QColor(GRIDHEAD))
        fnt = QtGui.QFont()
        fnt.setBold(True)
        it.setFont(fnt)
        t.setItem(0, c, it)
    return t


def _scan_fill(t, rows, fmt):
    """fmt[i] 是第 i 列的格式化函数；行数按 rows 补齐。"""
    t.setRowCount(len(rows) + 1)
    for r, row in enumerate(rows):
        for c, fn in enumerate(fmt):
            cell = t.item(r + 1, c)
            if cell is None:
                cell = QtWidgets.QTableWidgetItem("")
                cell.setFlags(Qt.ItemIsEnabled)
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(r + 1, c, cell)
            cell.setText(fn(row))


def _bfill(t, row, value, col=1, gray=False):
    cell = t.item(row, col)
    if cell is None:
        return
    cell.setText(_g(value) if not gray else "-")


def _instructions(lines):
    lab = QtWidgets.QLabel("\n".join(lines))
    lab.setWordWrap(True)
    lab.setStyleSheet("font-size:9pt;")
    return lab


# --- BJT 分压偏置 ------------------------------------------------------------

class _DgBjt(_Diagram):
    NAME = "bjt"

    def draw(self, p):
        # 顶部 Vcc 母线
        _line(p, 70, 52, 340, 52)
        # R1（基极上分压）支路 x=130
        _line(p, 130, 52, 130, 64)
        _poly(p, [(122, 64), (138, 64), (138, 96), (122, 96)], "#FFFFFF")
        _line(p, 130, 96, 130, 152)
        # R2（基极下分压）支路
        _poly(p, [(122, 168), (138, 168), (138, 200), (122, 200)], "#FFFFFF")
        _line(p, 130, 152, 130, 168)
        _line(p, 130, 200, 130, 228)
        _line(p, 118, 228, 142, 228)
        _line(p, 124, 234, 136, 234)
        _line(p, 127, 240, 133, 240)
        # RC（集电极）支路 x=240
        _line(p, 240, 52, 240, 64)
        _poly(p, [(232, 64), (248, 64), (248, 96), (232, 96)], "#FFFFFF")
        _line(p, 240, 96, 240, 120)
        # RE（发射极）支路
        _poly(p, [(232, 212), (248, 212), (248, 244), (232, 244)], "#FFFFFF")
        _line(p, 240, 196, 240, 212)
        _line(p, 240, 244, 240, 258)
        _line(p, 228, 258, 252, 258)
        _line(p, 234, 264, 246, 264)
        # BJT 符号
        p.setBrush(Qt.NoBrush)
        _pen(p)
        p.drawEllipse(QtCore.QRectF(191, 131, 48, 48))
        _line(p, 203, 140, 203, 170, "#000000", 2.0)
        _line(p, 130, 155, 203, 155)
        _line(p, 203, 146, 240, 120)
        _line(p, 203, 164, 240, 196)
        _poly(p, [(222, 178), (230, 186), (219, 184)], "#000000")
        self.label(26, 78, "R1")
        self.label(26, 186, "R2")
        self.label(204, 100, "RC")
        self.label(220, 208, "RE")


def _mk_bjt(win):
    dg = _DgBjt()
    dg.box("vcc", 318, 42, "2.7", obj="bjt.vcc")
    dg.box("ic", 318, 112, "5", obj="bjt.ic")
    dg.label(280, 44, "Vcc =")
    dg.label(288, 114, "Ic =")
    dg.label(282, 134, "mA")
    dg.box("vce", 318, 152, "2", obj="bjt.vce")
    dg.label(266, 154, "VCE =")
    dg.label(282, 174, "V")
    dg.box("rc", 268, 66, "", ro=True, obj="bjt.result.rc")
    dg.box("re", 256, 216, "", ro=True, obj="bjt.result.re")
    dg.box("r1", 58, 66, "", ro=True, obj="bjt.result.r1")
    dg.box("r2", 58, 176, "", ro=True, obj="bjt.result.r2")
    dg.box("ve", 344, 222, "", ro=True, obj="bjt.result.ve")
    dg.label(302, 224, "VE =")
    dg.finalize()

    page = _b_shell(win, "bjt", "BJT 电阻分压偏置 Voltage-Divider Bias")
    left = page._left
    left.addWidget(dg)
    left.addWidget(_instructions([
        "1. 输入 Vcc、Ic、VCE（图中白框，Ic 单位 mA）。",
        "2. 输入器件参数（β、VBE、VRE%、分压电流倍数）。",
        "3. 点击 Calculate [F4] 解出偏置电阻 R1/R2/RC/RE 并填入图中与分析表。",
        "注：实机 AppCAD 的 Base Bias Network 为 R_B 单偏置（基极经一只电阻接 Vcc、"
        "发射极直接接地、无 RE），拓扑与此页不同；本页按引擎的分压偏置模型"
        "（R1/R2 分压 + RE 稳定）计算，图示即引擎真实电路。"]))
    b = QtWidgets.QPushButton("Calculate [F4]")
    b.setObjectName("bjt.calc")

    def calc():
        e = _e()
        vcc = _f(dg.fields["vcc"], "Vcc")
        ic = _f(dg.fields["ic"], "Ic") / 1000.0
        vce = _f(dg.fields["vce"], "VCE")
        beta = _f(page.beta_box, "β")
        vbe = _fopt(page.vbe_box, 0.7, "VBE")
        vre_pct = _fopt(page.vre_box, 10, "VRE%")
        i2f = _fopt(page.i2f_box, 10, "I2 倍数")
        r = e.bjt_bias_divider(vcc, ic, vce, beta, vbe, vre_pct / 100.0 * vcc, i2f)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        dg.fields["rc"].setText(_g(r["rc"]))
        dg.fields["re"].setText(_g(r["re"]))
        dg.fields["r1"].setText(_g(r["r1"]))
        dg.fields["r2"].setText(_g(r["r2"]))
        dg.fields["ve"].setText(_g(r["ve"]))
        t = page.table
        _bfill(t, 0, r["vb"])
        _bfill(t, 1, r["ve"])
        _bfill(t, 2, r["ib"] * 1000.0)
        _bfill(t, 3, r["ie"] * 1000.0)
        _bfill(t, 4, r["i2"] * 1000.0)
        _bfill(t, 5, r["p_rc_w"] * 1000.0)
        _bfill(t, 6, r["p_re_w"] * 1000.0)
        return "RE=%s RC=%s R1=%s R2=%s" % (_g(r["re"]), _g(r["rc"]), _g(r["r1"]),
                                            _g(r["r2"]))

    b.clicked.connect(lambda: _run_calc(page, calc))
    left.addWidget(b)
    left.addStretch(1)

    gb1, l1 = _grp("器件参数 Transistor Parameters")
    page.beta_box = _num_box("bjt.beta", "50")
    page.vbe_box = _num_box("bjt.vbe", "0.7")
    page.vre_box = _num_box("bjt.vrepct", "10")
    page.i2f_box = _num_box("bjt.i2f", "10")
    l1.addWidget(_grow("β (hFE) =", page.beta_box))
    l1.addWidget(_grow("VBE =", page.vbe_box, "V"))
    l1.addWidget(_grow("VRE =", page.vre_box, "%Vcc"))
    l1.addWidget(_grow("I2 倍数 =", page.i2f_box, "×Ib"))
    page._right.addWidget(gb1)

    gb2, l2 = _grp("设计规则 Design Rules（引擎未建模，灰显）")
    for txt in ("Icbo / θjC / Tc(min·typ·max)", "∂hFE/∂T、∂VBE/∂T、∂Icbo/∂T"):
        lab = _ql(txt)
        lab.setStyleSheet("color:#909090;")
        l2.addWidget(lab)
    page._right.addWidget(gb2)

    page._right.addWidget(_ql("Bias Analysis（名义值；min/typ/max 列引擎未建模）："))
    page.table = _btable(["Vb (V)", "Ve (V)", "Ib (mA)", "Ie (mA)", "I2 (mA)",
                          "P(RC) (mW)", "P(RE) (mW)"], "bjt.table")
    page._right.addWidget(page.table)
    gb3, l3 = _grp("Stability Analysis（引擎未建模，灰显）")
    page.table2 = _btable(["Icbo", "Vbe", "hFE"], "bjt.table2",
                          headers=["Stability Factor", "Delta Ic (mA)"])
    l3.addWidget(page.table2)
    page._right.addWidget(gb3)
    page._right.addStretch(1)
    page.dg = dg
    return page


# --- BJT 固定偏置（Base Bias，RB 单电阻，AppCAD 原文拓扑） -------------------

class _DgBjtBase(_Diagram):
    NAME = "bjt_base"

    def draw(self, p):
        # --- Vcc 馈电：马蹄端子（开口朝左）+ 顶部水平走线（spec 20 §2.1） ---
        p.setBrush(Qt.NoBrush)
        _pen(p, "#000000", 2.0)
        p.drawArc(QtCore.QRectF(289, 110, 8, 8), 225 * 16, 270 * 16)
        _line(p, 74, 114, 290, 114, "#000000", 2.0)
        p.setBrush(QtGui.QColor("#000000"))
        _pen(p)
        p.drawRect(QtCore.QRectF(226, 112, 4, 4))          # 结点方块
        _line(p, 74, 114, 74, 244, "#000000", 2.0)         # 左侧垂直下引线
        # --- RB 基极偏置路径（水平锯齿，振幅 ±3px） ---
        _line(p, 74, 244, 145, 244, "#000000", 2.0)
        pts = [(145, 244)]
        for i in range(6):
            pts.append((148 + i * 3, 244 + (3 if i % 2 == 0 else -3)))
        pts.append((163, 244))
        _poly(p, pts, "#FFFFFF")
        _line(p, 163, 244, 218, 244, "#000000", 2.0)
        # --- BJT 符号（NPN 竖杠式，无外圈；发射极直接接地） ---
        _line(p, 219, 232, 219, 257, "#000000", 3.0)       # 基极竖杠
        _line(p, 220, 232, 228, 229)                       # 集电极斜线
        _line(p, 220, 257, 227, 259)                       # 发射极斜线
        _head(p, 228, 260, 0.45, 0.89)                     # NPN 箭头（指向右下）
        _line(p, 228, 261, 228, 299, "#000000", 2.0)       # 发射极下引线
        _line(p, 220, 300, 235, 300, "#000000", 2.0)       # 发射极地
        _line(p, 222, 302, 233, 302, "#000000", 1.4)
        _line(p, 224, 304, 231, 304, "#000000", 1.4)
        # --- 集电极支路：Vcc 结点 → RC 窄锯齿 → 集电极 ---
        _line(p, 228, 115, 228, 142, "#000000", 2.0)
        _zig_v(p, 228, 142, 159, 2.2, n=5)
        _line(p, 228, 159, 228, 229, "#000000", 2.0)
        _arrow(p, 228, 184, 228, 191)                      # IC 电流箭头（尖朝下）
        # --- 标注（spec §2.1 原文位置） ---
        self.label(86, 220, "RB =")
        self.label(178, 220, "[Ohms]")
        self.label(86, 154, "RC =")
        self.label(236, 194, "I C =")
        self.label(325, 194, "mA")
        self.label(307, 116, "Vcc =")
        self.label(306, 141, "Icc =")
        self.label(326, 162, "mA (typ. see Help)")
        self.label(251, 246, "VCE =")
        self.label(341, 246, "V")
        self.label(176, 274, "VBE")
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        # VCE 双弧箭头（跨 c-e）
        pa = QtGui.QPainterPath()
        pa.moveTo(256, 226)
        pa.quadTo(248, 218, 241, 216)
        p.drawPath(pa)
        _head(p, 239, 215, -0.93, -0.37)
        pb = QtGui.QPainterPath()
        pb.moveTo(254, 276)
        pb.quadTo(243, 272, 232, 260)
        p.drawPath(pb)
        _head(p, 231, 258, -0.53, -0.85)
        # VBE 弧形箭头（指向 b-e 结）
        pc = QtGui.QPainterPath()
        pc.moveTo(212, 278)
        pc.quadTo(200, 268, 193, 256)
        p.drawPath(pc)
        _head(p, 192, 253, -0.6, -0.8)

# --- 实机 AppCAD 绝对定位工具（spec 页右卡/底表重排） -------------------------


def _abs(page, w, x, y, ww, hh):
    """把控件挂到 page 并绝对定位（AppCAD 固定坐标窗体）。"""
    w.setParent(page)
    w.setMinimumSize(0, 0)
    w.setMaximumSize(16777215, 16777215)
    w.setGeometry(int(x), int(y), int(ww), int(hh))
    w.show()
    return w


def _abs_text(page, text, x, y, w, h, align="l"):
    """静态文本标签；(x,y,w,h) 为实测墨迹框，向外扩 2px 容纳行盒。"""
    lab = QtWidgets.QLabel(text, page)
    al = {"l": Qt.AlignLeft, "r": Qt.AlignRight,
          "c": Qt.AlignCenter}[align] | Qt.AlignVCenter
    lab.setAlignment(al)
    lab.setGeometry(int(x) - 2, int(y) - 2, int(w) + 4, int(h) + 4)
    lab.show()
    return lab


def _grey_abs(page, obj, val, x, y, w, h):
    """右卡灰参数框（引擎未建模，仅版式复刻；值左对齐与实机一致）。"""
    bx = _num_box(obj, val, w=w)
    bx.setStyleSheet("color:#777777;")
    bx.setToolTip("引擎未建模，仅版式复刻")
    bx.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return _abs(page, bx, x, y, w, h)


class _AbsCellDelegate(QtWidgets.QStyledItemDelegate):
    """VB6 3D 格子：每格上/左白高光 + 下/右灰阴影。
    固定格（行0/col0）阴影 #A0A0A0，数据格阴影 #F0F0F0（参考像素实测）。"""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        fixed = index.row() == 0 or index.column() == 0
        shadow = QtGui.QColor("#A0A0A0") if fixed else QtGui.QColor("#F0F0F0")
        rect = option.rect
        painter.save()
        painter.setPen(QtGui.QPen(shadow))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.drawLine(rect.topRight(), rect.bottomRight())
        painter.setPen(QtGui.QPen(QtGui.QColor("#FFFFFF")))
        painter.drawLine(rect.topLeft(), rect.topRight())
        painter.drawLine(rect.topLeft(), rect.bottomLeft())
        painter.restore()


def _abs_table(page, obj, x, y, w, h, nrow, ncol, col_ws, row_h):
    """绝对定位结果表：行0 整行灰、行标签列灰、值格白；cell 全建供 _bfill。"""
    t = QtWidgets.QTableWidget(nrow, ncol)
    t.setObjectName(obj)
    # VB6 风格外框：深灰 #696969 一圈（Qt 默认框色 #DFE5EE 太浅不可见）。
    # 内部格线由 _AbsCellDelegate 画 VB6 3D 格子（上/左白高光+下/右灰阴影），
    # 关掉 Qt 网格避免双线。
    t.setStyleSheet(
        "QTableWidget { border: 1px solid #696969;"
        " background: #FFFFFF; }")
    t.setShowGrid(False)
    t.setItemDelegate(_AbsCellDelegate(t))
    t.horizontalHeader().setVisible(False)
    t.verticalHeader().setVisible(False)
    t.setFocusPolicy(Qt.NoFocus)
    t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    t.verticalHeader().setDefaultSectionSize(row_h)
    # 样式默认 minimumSectionSize=17 会把 setRowHeight(16) 钳回 17，
    # 网格线随之偏 1px；先降最小节高再设行高。
    t.verticalHeader().setMinimumSectionSize(row_h)
    for c, cw in enumerate(col_ws):
        t.setColumnWidth(c, cw)
    _abs(page, t, x, y, w, h)
    # 行高必须在 _abs(setParent) 之后显式设：reparent 触发 StyleChange
    # 会把行重置为样式默认 17px，网格线随之偏移 1px。
    for r in range(nrow):
        t.setRowHeight(r, row_h)
        for c in range(ncol):
            it = QtWidgets.QTableWidgetItem("")
            it.setFlags(Qt.ItemIsEnabled)
            if r == 0 or c == 0:
                it.setBackground(QtGui.QColor(GRIDHEAD))
            else:
                it.setBackground(QtGui.QColor("#FFFFFF"))
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, c, it)
    return t


def _tset(t, r, c, text="", align=None, bg=None):
    """设置表格 cell 文本/对齐/底色（cell 由 _abs_table 全建）。"""
    it = t.item(r, c)
    if it is None:
        return
    it.setText(text)
    if align is not None:
        it.setTextAlignment(align)
    if bg is not None:
        it.setBackground(QtGui.QColor(bg))


def _style_title(page, name, title):
    """pgtitle 改为实机样式：Times New Roman 粗体 20pt 绝对定位。"""
    tx = 30
    if _CHROME is not None:
        tx = _CHROME.TITLES.get(name, (30, title))[0]
    lab = page.findChild(QtWidgets.QLabel, "pgtitle")
    if lab is None:
        lab = QtWidgets.QLabel(title, page)
        lab.setObjectName("pgtitle")
    lab.setStyleSheet("font-family:'Times New Roman'; font-size:20pt;"
                      " font-weight:bold;")
    lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    _abs(page, lab, tx, 40, 600, 26)
    return lab


def _mk_bjt_base(win):
    dg = _spec_diagram("bjt_base")
    if dg is None:
        dg = _DgBjtBase()
        dg.box("vcc", 340, 105, "2.7", w=48, h=17, obj="bjt_base.vcc")
        dg.box("icc", 339, 129, "", w=48, h=18, ro=True, obj="bjt_base.result.ic")
        dg.box("rc", 117, 143, "", w=57, h=18, obj="bjt_base.rc")
        dg.box("rb", 117, 208, "", w=57, h=18, obj="bjt_base.rb")
        dg.box("ic", 264, 183, "5", w=56, h=17, obj="bjt_base.ic")
        dg.box("vce", 292, 235, "2", w=42, h=17, obj="bjt_base.vce")
        dg.finalize()

    page = _b_shell(win, "bjt_base", "Base Bias Network (non-stabilized)")
    left = page._left
    is_spec = SpecDiagram is not None and isinstance(dg, SpecDiagram)
    if is_spec:
        dg.setParent(page)
        dg.move(0, 0)
        dg.show()
        page.setFixedSize(966, 669)   # 1:1 实测画布（AppCAD 4.00 截图尺寸）
        _spec_chrome(page, "bjt_base")
        # 实机 AppCAD 是固定坐标窗体：停用布局防回流，全部绝对定位
        lay = page.layout()
        if lay is not None:
            lay.setEnabled(False)
        _style_title(page, "bjt_base", "Base Bias Network (non-stabilized)")
        mm = page.findChild(QtWidgets.QPushButton, "bjt_base.mainmenu")
        if mm is not None and _CHROME is not None:
            mm.setGeometry(*_CHROME.MAINMENU)
        _abs(page, page.err, 54, 492, 420, 20)   # 红粗体状态行（表格下空白）
    else:
        left.addWidget(dg)
        left.addWidget(_instructions([
            "1. Enter values for Vcc, Vce, and Ic.",
            "2. Enter Transistor Parameters.",
            "3. Calculate bias resistors and analyze circuit.",
            "注：另有「BJT 电阻分压偏置」页（R1/R2 分压 + RE，引擎稳定偏置模型）。"]))
    b = QtWidgets.QPushButton("Calculate [F4]")
    b.setObjectName("bjt_base.calc")
    b2 = QtWidgets.QPushButton("Calculate RB [F3]")
    b2.setObjectName("bjt_base.calc2")

    def _analysis():
        e = _e()
        vcc = _f(dg.fields["vcc"], "Vcc")
        rb = _f(dg.fields["rb"], "RB")
        rc = _f(dg.fields["rc"], "RC")
        beta = _f(page.beta_box, "hFE")
        vbe = _fopt(page.vbe_box, 0.78, "VBE")
        r = e.bjt_base_bias_analysis(vcc, rb, rc, beta, vbe)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        dg.fields["icc"].setText(_g(r["ic"] * 1000.0))
        dg.fields["vce"].setText(_g(r["vce"]))
        # Collector Current 表：正文 3 行（hFE 50/80/150 对应），typ 行（row2）填引擎值
        t = page.table
        cell = t.item(2, 2)
        if cell is not None:
            cell.setText(_g(r["ic"] * 1000.0))
        _bfill(page.stab, 2, r["s_factor"])            # hFE 行 Stability Factor
        return "IC=%s mA  VCE=%s V  S=%s" % (_g(r["ic"] * 1000.0),
                                             _g(r["vce"]), _g(r["s_factor"]))

    def calc():
        return _analysis()

    def calc2():
        # 给定 Vcc / IC 目标 / RC / hFE / VBE → RB（引擎综合），回填后立即分析
        e = _e()
        vcc = _f(dg.fields["vcc"], "Vcc")
        ic = _f(dg.fields["ic"], "IC") / 1000.0
        rc = _f(dg.fields["rc"], "RC")
        beta = _f(page.beta_box, "hFE")
        vbe = _fopt(page.vbe_box, 0.78, "VBE")
        r = e.bjt_base_bias_synthesis(vcc, ic, rc, beta, vbe)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        dg.fields["rb"].setText(_g(r["rb"]))
        _analysis()
        return "RB=%s" % _g(r["rb"])

    b.clicked.connect(lambda: _run_calc(page, calc))
    b2.clicked.connect(lambda: _run_calc(page, calc2))
    if is_spec:
        # === 实机 AppCAD 固定坐标：按钮/右卡/底表全部绝对定位 ===
        _abs(page, b, 260, 333, 81, 23)      # Calculate [F4]
        _abs(page, b2, 346, 333, 90, 23)     # Calculate RB [F3]
        # -- Transistor Parameters --
        _abs_text(page, "Transistor Parameters", 462, 88, 101, 12)
        _abs_text(page, "hFE =", 465, 109, 28, 16, "r")
        _grey_abs(page, "bjt_base.hfemin", "50", 504, 109, 43, 16)
        _abs_text(page, "min", 552, 109, 14, 16)
        page.beta_box = _num_box("bjt_base.beta", "80")
        page.beta_box.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        _abs(page, page.beta_box, 582, 109, 43, 16)
        _abs_text(page, "typ", 631, 109, 12, 16)
        _grey_abs(page, "bjt_base.hfemax", "150", 661, 109, 43, 16)
        _abs_text(page, "max", 709, 109, 17, 16)
        _abs_text(page, "Icbo =", 468, 137, 28, 18, "r")
        _grey_abs(page, "bjt_base.icbo", "0.1", 503, 137, 55, 18)
        _abs_text(page, "uA", 565, 137, 11, 18)
        _abs_text(page, "θjc =", 615, 137, 24, 18, "r")
        _grey_abs(page, "bjt_base.tjc", "300", 646, 137, 46, 18)
        _abs_text(page, "deg/W", 704, 137, 27, 18)
        _abs_text(page, "Tc =", 475, 164, 20, 18, "r")
        _grey_abs(page, "bjt_base.tcmin", "-25", 501, 164, 43, 18)
        _abs_text(page, "min", 549, 164, 14, 18)
        _grey_abs(page, "bjt_base.tc", "25", 579, 164, 46, 18)
        _abs_text(page, "nom", 631, 164, 12, 18)
        _grey_abs(page, "bjt_base.tcmax", "65", 652, 164, 43, 18)
        _abs_text(page, "max °C", 702, 164, 31, 18)
        # -- Design Rules（引擎未建模，仅版式复刻）--
        _abs_text(page, "Design Rules", 535, 205, 62, 12)
        _abs_text(page, "VBE =", 535, 230, 83, 16, "r")
        page.vbe_box = _num_box("bjt_base.vbe", "0.78")
        page.vbe_box.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        _abs(page, page.vbe_box, 626, 230, 38, 16)
        _abs_text(page, "V", 673, 230, 6, 16)
        _abs_text(page, "ΔhFE/ΔT =", 535, 257, 83, 16, "r")
        _grey_abs(page, "bjt_base.dr_dhfe", "0.5", 626, 257, 38, 16)
        _abs_text(page, "%/°C", 674, 257, 22, 16)
        _abs_text(page, "ΔVbe/ΔT =", 535, 285, 83, 16, "r")
        _grey_abs(page, "bjt_base.dr_dvbe", "-2", 626, 285, 38, 16)
        _abs_text(page, "mV/°C", 674, 285, 29, 16)
        _abs_text(page, "ΔIcbo/ΔT factor =", 535, 312, 83, 16, "r")
        _grey_abs(page, "bjt_base.dr_dicbo", "2", 626, 312, 38, 16)
        _abs_text(page, "/10°C", 674, 312, 29, 16)
        # -- 底表左 Collector Current（表标题由 circ spec labels 绘制）--
        page.table = _abs_table(page, "bjt_base.table", 54, 391, 272, 66,
                                4, 4, (67, 66, 67, 70), 16)
        _tset(page.table, 0, 0, "hFE =", Qt.AlignRight | Qt.AlignVCenter)
        # -- 底表右 Stability Analysis（表标题同上）--
        page.stab = _abs_table(page, "bjt_base.stab", 433, 389, 294, 82,
                               5, 3, (67, 94, 131), 16)
        _tset(page.stab, 0, 1, "Stability Factor", Qt.AlignCenter)
        _tset(page.stab, 0, 2, "Delta Ic (mA): typ hFE, Tc", Qt.AlignCenter)
        for _r, _lab in ((1, "Icbo"), (2, "Vbe"), (3, "hFE")):
            _tset(page.stab, _r, 0, _lab, Qt.AlignCenter)
        _tset(page.stab, 4, 1, "Delta Ic, Total =",
              Qt.AlignLeft | Qt.AlignVCenter, "#BEBEBE")
        # -- 表下两组 --
        _abs_text(page, "Ic max/min ratio =", 55, 467, 83, 16)
        page.icratio = _num_box("bjt_base.icratio", "", w=49, ro=True)
        _abs(page, page.icratio, 144, 467, 49, 16)
        _abs_text(page, "Tj max =", 213, 467, 31, 16)
        page.tjmax = _num_box("bjt_base.tjmax", "", w=49, ro=True)
        _abs(page, page.tjmax, 261, 467, 49, 16)
        _abs_text(page, "°C", 316, 467, 12, 16)
        page.dg = dg
        return page
    left.addWidget(b)
    left.addWidget(b2)
    left.addStretch(1)

    gb1, l1 = _grp("Transistor Parameters（晶体管参数）")
    page.beta_box = _num_box("bjt_base.beta", "80")
    page.vbe_box = _num_box("bjt_base.vbe", "0.78")
    l1.addWidget(_grow("hFE (typ) =", page.beta_box))
    l1.addWidget(_grow("VBE =", page.vbe_box, "V"))

    def _grey_row(lay, lab, obj, val, unit=""):
        bx = _num_box(obj, val)
        bx.setStyleSheet("color:#777777;")
        bx.setToolTip("引擎未建模，仅版式复刻")
        lay.addWidget(_grow(lab, bx, unit))

    _grey_row(l1, "Icbo =", "bjt_base.icbo", "0.1", "µA")
    _grey_row(l1, "θjc =", "bjt_base.tjc", "300", "°C/W")
    _grey_row(l1, "Tc min =", "bjt_base.tcmin", "-25", "°C")
    _grey_row(l1, "Tc nom =", "bjt_base.tc", "25", "°C")
    _grey_row(l1, "Tc max =", "bjt_base.tcmax", "65", "°C")
    page._right.addWidget(gb1)

    gb2, l2 = _grp("Design Rules（引擎未建模，仅版式复刻）")
    for lab, key, val, unit in (("VBE =", "dr_vbe", "0.78", "V"),
                                ("ΔhFE/ΔT =", "dr_dhfe", "0.5", "%/°C"),
                                ("ΔVbe/ΔT =", "dr_dvbe", "-2", "mV/°C"),
                                ("ΔIcbo/ΔT factor =", "dr_dicbo", "2", "/10°C")):
        _grey_row(l2, lab, "bjt_base." + key, val, unit)
    page._right.addWidget(gb2)

    page._right.addWidget(_ql("Collector Current Ic (mA)："))
    t = QtWidgets.QTableWidget(4, 4)
    t.setObjectName("bjt_base.table")
    t.horizontalHeader().setVisible(False)
    t.verticalHeader().setVisible(False)
    t.setMinimumHeight(120)
    for c, head in enumerate(("hFE =", "", "", "")):
        it = QtWidgets.QTableWidgetItem(head)
        it.setFlags(Qt.ItemIsEnabled)
        it.setBackground(QtGui.QColor(GRIDHEAD))
        t.setItem(0, c, it)
    for r in range(1, 4):
        it = QtWidgets.QTableWidgetItem("")
        it.setFlags(Qt.ItemIsEnabled)
        it.setBackground(QtGui.QColor(GRIDHEAD))
        t.setItem(r, 0, it)
        for c in range(1, 4):
            cell = QtWidgets.QTableWidgetItem("")
            cell.setFlags(Qt.ItemIsEnabled)
            cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, c, cell)
    page.table = t
    page._right.addWidget(t)
    hq = QtWidgets.QWidget()
    hh = QtWidgets.QHBoxLayout(hq)
    hh.setContentsMargins(0, 0, 0, 0)
    hh.setSpacing(4)
    lab = _ql("Ic max/min ratio =", align_right=True)
    hh.addWidget(lab)
    page.icratio = _num_box("bjt_base.icratio", "", w=50, ro=True)
    hh.addWidget(page.icratio)
    hh.addSpacing(12)
    hh.addWidget(_ql("Tj max =", align_right=True))
    page.tjmax = _num_box("bjt_base.tjmax", "", w=50, ro=True)
    hh.addWidget(page.tjmax)
    hh.addWidget(_ql("°C"))
    hh.addStretch(1)
    page._right.addWidget(hq)

    gb3, l3 = _grp("Stability Analysis")
    page.stab = _btable(["Icbo", "Vbe", "hFE", "Delta Ic, Total ="],
                        "bjt_base.stab",
                        headers=["Stability Factor",
                                 "Delta Ic (mA): typ hFE, Tc"])
    # 末行灰底（spec §2.3 表 B r4）；首列加宽保证 "Delta Ic, Total =" 完整可见
    page.stab.setColumnWidth(0, 120)
    for c in range(page.stab.columnCount()):
        it = page.stab.item(3, c)
        if it is not None:
            it.setBackground(QtGui.QColor("#E8E8E8"))
            it.setText("" if c else it.text())
    l3.addWidget(page.stab)
    page._right.addWidget(gb3)
    page._right.addStretch(1)
    page.dg = dg
    return page


# --- FET 自偏 / 分压 ---------------------------------------------------------

def _zig_v(p, cx, y0, y1, w=3.5, n=6, fill="#FFFFFF"):
    """垂直锯齿电阻（峰谷左右交替），中心线 cx。"""
    pts = [(cx, y0)]
    dy = (y1 - y0) / n
    for i in range(n):
        pts.append((cx + (w if i % 2 == 0 else -w), y0 + dy * (i + 0.5)))
    pts.append((cx, y1))
    _poly(p, pts, fill)


def _gnd4(p, cx, y, w1=7.0):
    """AppCAD 地符号：逐级收窄横线（2~4 条）。"""
    _line(p, cx - w1, y, cx + w1, y, "#000000", 2.0)
    _line(p, cx - w1 + 1, y + 2, cx + w1 - 1, y + 2, "#000000", 1.2)
    _line(p, cx - w1 + 2, y + 5, cx + w1 - 2, y + 5, "#000000", 1.2)


class _DgFet(_Diagram):
    NAME = "fet"

    def __init__(self, divider=False):
        super().__init__()
        self._divider = divider
        self.setFixedSize(int(self.LW), 352)

    def draw(self, p):
        dv = self._divider
        # --- Vdd 端子（空心圆 r4）+ 水平引入线（spec 18 §1.1） ---
        p.setBrush(Qt.NoBrush)
        _pen(p)
        p.drawEllipse(QtCore.QPointF(247, 101), 4, 4)
        _line(p, 225, 102, 243, 102, "#000000", 2.0)
        # --- 垂直主干 x=225：Rd 顶结点 → Rd 锯齿 → 漏极结点 → 源极结点 ---
        _line(p, 225, 102, 225, 114, "#000000", 2.0)
        p.setBrush(QtGui.QColor("#000000"))
        _pen(p)
        p.drawRect(QtCore.QRectF(223, 110, 4, 4))
        _zig_v(p, 225.5, 114, 139)                     # Rd（垂直锯齿）
        _line(p, 225, 139, 225, 193, "#000000", 2.0)
        p.drawRect(QtCore.QRectF(224, 148, 3, 4))
        _arrow(p, 225, 167, 225, 177)                  # 电流方向箭头（尖朝下）
        p.drawRect(QtCore.QRectF(224, 193, 4, 4))      # 漏极结点
        _line(p, 225, 196, 225, 285, "#000000", 2.0)
        # 漏极输出线 + 端子圆
        _line(p, 223, 194, 323, 194, "#000000", 2.0)
        p.setBrush(Qt.NoBrush)
        _pen(p)
        p.drawEllipse(QtCore.QPointF(327, 194), 4, 4)
        # --- FET 符号（简化 MESFET，无外圈） ---
        _line(p, 216.5, 217, 216.5, 242, "#000000", 2.0)   # 沟道竖杠
        _line(p, 218, 225, 226, 225, "#000000", 3.0)       # 漏极横枝
        _line(p, 218, 237, 226, 237, "#000000", 3.0)       # 源极横枝
        # --- 源极：结点 → 旁路电容 → Rs → 地（spec §1.1 ①） ---
        p.setBrush(QtGui.QColor("#000000"))
        _pen(p)
        p.drawRect(QtCore.QRectF(224, 285, 3, 4))      # 源极结点
        _line(p, 225, 290, 225, 301, "#000000", 2.0)   # Rs 上引线
        _zig_v(p, 225, 302, 318, 4.0)                  # Rs
        _line(p, 225, 319, 225, 336, "#000000", 2.0)
        _gnd4(p, 225, 339, 5.5)                        # 地#3（Rs 地）
        _line(p, 197, 287, 227, 287, "#000000", 2.0)   # 源极向左引线
        _line(p, 191, 278, 191, 295, "#000000", 2.0)   # 电容板1
        _line(p, 197, 278, 197, 295, "#000000", 2.0)   # 电容板2
        _line(p, 171, 287, 190, 287, "#000000", 2.0)   # 电容左引线
        _line(p, 170, 288, 170, 301, "#000000", 2.0)   # 下引线
        _gnd4(p, 170, 303, 5.5)                        # 地#2（旁路电容地）
        # --- 栅极网络 ---
        p.setBrush(Qt.NoBrush)
        _pen(p)
        p.drawEllipse(QtCore.QPointF(56, 230), 4, 4)   # 栅极端子圆
        p.setBrush(QtGui.QColor("#000000"))
        _pen(p)
        p.drawRect(QtCore.QRectF(77, 228, 4, 4))       # 栅线结点
        if not dv:
            # 虚线框（RF 回地网络，不参与偏置计算）
            _line(p, 225, 241, 225, 241)               # 防御性空操作（保持 pen 状态）
            _pen(p, "#000000", 1.0)
            q = p.pen()
            q.setDashPattern([4, 1])
            p.setPen(q)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QtCore.QRectF(56, 240, 42, 47))
            _pen(p)
            # 框内电感（4 个左凸半圆波峰）
            for i in range(4):
                p.drawArc(QtCore.QRectF(65, 254 + i * 3.8, 7, 4.2),
                          90 * 16, 180 * 16)
            _line(p, 68, 244, 68, 254)
            _line(p, 68, 269, 68, 281)
            # IEC 矩形电阻
            p.setBrush(Qt.NoBrush)
            _pen(p)
            p.drawRect(QtCore.QRectF(82, 250, 5, 24))
            _line(p, 84, 245, 84, 250)
            _line(p, 84, 274, 84, 279)
            # 框下引地线 + 地#1
            _line(p, 78, 287, 78, 299, "#000000", 2.0)
            _gnd4(p, 78, 300, 7.0)
            self.label(89, 222, "Vg = 0")
        else:
            # 分压页：R1 上拉到 Vcc 母线、R2 下地（真实分压拓扑）
            _line(p, 78, 114, 78, 120, "#000000", 2.0)
            _zig_v(p, 78, 120, 145)                    # R1
            _line(p, 78, 145, 78, 227, "#000000", 2.0)
            _zig_v(p, 78, 255, 280)                    # R2
            _line(p, 78, 280, 78, 292, "#000000", 2.0)
            _gnd4(p, 78, 293, 5.5)
            self.label(24, 136, "R1")
            self.label(24, 272, "R2")
        # --- 标注文字与弧形箭头（spec §1.1 标注） ---
        self.label(265, 105, "Vdd =")
        self.label(270, 124, "Idd = Id")
        self.label(89, 134, "Rd =")
        self.label(178, 134, "[Ohms]")
        self.label(64, 180, "I d =")
        self.label(161, 168, "mA (target)")
        self.label(161, 194, "mA (calc)")
        self.label(107, 261, "Vgs =")
        self.label(185, 261, "V")
        self.label(251, 236, "Vds =")
        self.label(340, 222, "V (target)")
        self.label(341, 251, "V (calc)")
        self.label(237, 312, "Rs =")
        pf = QtGui.QFont("Times New Roman", 30)
        pf.setItalic(True)
        p.setFont(pf)
        p.drawText(84, 198, "{")                       # Id 大括号
        p.drawText(278, 250, "{")                      # Vds 大括号
        p.setFont(QtGui.QFont(self.font()))
        # Vgs 弧形箭头（指向栅极引线）
        path = QtGui.QPainterPath()
        path.moveTo(212, 266)
        path.quadTo(206, 246, 193, 242)
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        _head(p, 191, 241, -0.85, -0.53)
        # Vds 弧形箭头（指向漏极输出线下方）
        path2 = QtGui.QPainterPath()
        path2.moveTo(256, 258)
        path2.quadTo(250, 225, 241, 206)
        p.drawPath(path2)
        _head(p, 239, 202, -0.42, -0.91)

def _mk_fet(win, name, title, divider):
    dg = None if divider else _spec_diagram(name)
    if dg is None:
        dg = _DgFet(divider=divider)
        # 框坐标 = spec 18 §1.2；默认值取分压合法工作点（VDD=10/ID=10mA/VDS=5）
        dg.box("vdd", 300, 93, "10" if divider else "3", w=45, h=17, obj=name + ".vdd")
        dg.box("rd", 118, 121, "", w=57, h=19, ro=True, obj=name + ".result.rd")
        dg.box("id", 98, 156, "10" if divider else "20", w=57, h=17, obj=name + ".id")
        dg.box("idc", 98, 183, "", w=57, h=18, ro=True, obj=name + ".result.id")
        dg.box("vgs", 136, 247, "-0.5" if divider else "", w=43, h=18,
               obj=name + ".vgs")
        dg.box("vds", 299, 210, "5" if divider else "2", w=35, h=18, obj=name + ".vds")
        dg.box("vdsc", 292, 239, "", w=43, h=19, ro=True, obj=name + ".result.vds")
        dg.box("rs", 264, 300, "", w=57, h=18, ro=True, obj=name + ".result.rs")
        dg.label(307, 105, "=")   # （Vdd 标签已在 draw 内）
        if divider:
                dg.box("vg", 136, 275, "1.5", w=43, h=18, obj=name + ".vg")
                dg.box("i2", 136, 300, "1", w=43, h=18, obj=name + ".i2")
                dg.box("r1", 100, 126, "", w=57, h=18, ro=True, obj=name + ".result.r1")
                dg.box("r2", 100, 259, "", w=57, h=18, ro=True, obj=name + ".result.r2")
        dg.finalize()
    else:
        # 实机 spec 无图内 Rs 框（Rs 只在 Bias Analysis 表）→ 隐藏框兜底
        _rs = QtWidgets.QLineEdit(dg)
        _rs.setReadOnly(True)
        _rs.hide()
        _rs.setObjectName(name + ".result.rs")
        dg.fields["rs"] = _rs


    page = _b_shell(win, name, title)
    left = page._left
    is_spec = SpecDiagram is not None and isinstance(dg, SpecDiagram)
    if is_spec:
        dg.setParent(page)
        dg.move(0, 0)
        dg.show()
        page.setFixedSize(966, 669)   # 1:1 实测画布（AppCAD 4.00 截图尺寸）
        _spec_chrome(page, name if name != "fet_div" else "fet_self")
        # 实机 AppCAD 是固定坐标窗体：停用布局防回流，全部绝对定位
        lay = page.layout()
        if lay is not None:
            lay.setEnabled(False)
        _style_title(page, name, title)
        mm = page.findChild(QtWidgets.QPushButton, name + ".mainmenu")
        if mm is not None and _CHROME is not None:
            mm.setGeometry(*_CHROME.MAINMENU)
    else:
        left.addWidget(dg)
    if divider:
        lines = ["1. 输入 Vdd、目标 Id（mA）、VG 与目标 VDS。",
                 "2. 输入器件参数（VGS、VG、I2；Vp/Idss 本版引擎未建模）。",
                 "3. 点击 Calculate [F4] 解出 R1/R2/RS/RD 并填入图与分析表。"]
    else:
        lines = ["1. Enter values for Vdd, target Id, and target Vds.",
                 "   (Click Rd to toggle in/out or Menu Alt-S)",
                 "2. Enter Transistor Parameters. (%Idss/mA is a toggle)",
                 "3. [F4] to calculate bias resistors and analyze circuit."]
    if not is_spec:
        left.addWidget(_instructions(lines))
    else:
        # 指令 4 行由 circ spec labels 绘制；err 状态行绝对定位到表格下空白
        _abs(page, page.err, 41, 495, 420, 20)
    b = QtWidgets.QPushButton("Calculate [F4]")
    b.setObjectName(name + ".calc")

    def calc():
        e = _e()
        vdd = _f(dg.fields["vdd"], "Vdd")
        i_d = _f(dg.fields["id"], "Id") / 1000.0
        vgs_txt = dg.fields["vgs"].text().strip()
        vgs_note = ""
        if not vgs_txt:
            # 版式复刻：Vgs 框默认留空（spec 18）；未填时按典型值 -0.5 V 计算
            vgs = -0.5
            vgs_note = "（VGS 未填，按典型值 -0.5 V 计算）"
        else:
            vgs = _f(dg.fields["vgs"], "VGS")
        vds = _f(dg.fields["vds"], "VDS")
        if divider:
            vg = _f(dg.fields["vg"], "VG")
            i2 = _fopt(dg.fields["i2"], 1.0, "I2") / 1000.0
            r = e.fet_bias_divider(vdd, i_d, vgs, vds, vg, i2)
        else:
            r = e.fet_bias_self(vdd, i_d, vgs, vds)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        dg.fields["rs"].setText(_g(r["rs"]))
        dg.fields["rd"].setText(_g(r["rd"]))
        dg.fields["idc"].setText(_g(i_d * 1000.0))
        vact = vdd - i_d * (r["rs"] + r["rd"])
        dg.fields["vdsc"].setText(_g(vact))
        t = page.table
        _bfill(t, 0 if divider else 1, i_d * 1000.0)
        _bfill(t, 1 if divider else 2, i_d * (r["rs"] + r["rd"]) * 1000.0)
        if divider:
            dg.fields["r1"].setText(_g(r["r1"]))
            dg.fields["r2"].setText(_g(r["r2"]))
            _bfill(t, 2, r["vs"])
            _bfill(t, 3, r["p_rd_w"] * 1000.0)
            _bfill(t, 4, r["r1"])
            _bfill(t, 5, r["r2"])
        else:
            cell = t.item(3, 1)
            if cell is not None:
                cell.setText("")    # Tj 引擎未建模，保持空白
        return "RS=%s RD=%s VS=%s%s" % (_g(r["rs"]), _g(r["rd"]), _g(r["vs"]),
                                        vgs_note)

    b.clicked.connect(lambda: _run_calc(page, calc))
    if is_spec:
        # === 实机 AppCAD 固定坐标：右卡/按钮/底表全部绝对定位 ===
        _abs_text(page, "FET Parameters", 423, 64, 76, 12)
        _abs_text(page, "V'p =", 425, 88, 24, 18)
        _grey_abs(page, name + ".vpmin", "-0.65", 459, 88, 43, 18)
        _grey_abs(page, name + ".vptyp", "-0.5", 546, 88, 43, 18)
        _grey_abs(page, name + ".vpmax", "-0.35", 630, 88, 43, 18)
        _abs_text(page, "min", 510, 88, 16, 18)
        _abs_text(page, "typ", 596, 88, 14, 18)
        _abs_text(page, "max", 679, 88, 19, 18)
        _abs_text(page, "V", 703, 88, 8, 18)
        _abs_text(page, "V'p spec'd at =", 425, 115, 68, 18, "r")
        _grey_abs(page, name + ".vp10", "10", 499, 115, 42, 18)
        _abs_text(page, "% Idss", 551, 115, 30, 18)
        _abs_text(page, "ΔVp/ΔT =", 589, 115, 46, 18, "r")
        _grey_abs(page, name + ".dvpt", "0", 640, 115, 52, 18)
        _abs_text(page, "mV/°C", 695, 115, 26, 18)
        _abs_text(page, "Vp =", 425, 142, 23, 18)
        _grey_abs(page, name + ".vpmn", "", 458, 142, 46, 18)
        _grey_abs(page, name + ".vp", "", 545, 142, 46, 18)
        _grey_abs(page, name + ".vpmx", "", 629, 142, 46, 18)
        _abs_text(page, "min", 510, 142, 16, 18)
        _abs_text(page, "typ", 600, 142, 14, 18)
        _abs_text(page, "max", 685, 142, 19, 18)
        _abs_text(page, "V", 709, 142, 8, 18)
        _abs_text(page, "Idss =", 424, 185, 28, 18)
        _grey_abs(page, name + ".idssmin", "58", 458, 185, 52, 18)
        _grey_abs(page, name + ".idss", "65", 542, 185, 52, 18)
        _grey_abs(page, name + ".idssmax", "72", 624, 185, 52, 18)
        _abs_text(page, "min", 515, 185, 16, 18)
        _abs_text(page, "typ", 600, 185, 14, 18)
        _abs_text(page, "max", 680, 185, 19, 18)
        _abs_text(page, "mA", 705, 185, 15, 18)
        _abs_text(page, "ΔIdss/ΔT =", 424, 211, 56, 18, "r")
        _grey_abs(page, name + ".didsst", "-0.13", 487, 211, 43, 18)
        _abs_text(page, "% Idss/°C", 536, 211, 46, 18)
        _abs_text(page, "θJC =", 604, 211, 25, 18, "r")
        _grey_abs(page, name + ".tjc", "300", 635, 211, 46, 18)
        _abs_text(page, "°C/W", 685, 211, 29, 18)
        _abs_text(page, "Analysis Temperatures", 422, 259, 106, 12)
        _grey_abs(page, name + ".tcmin", "-25", 464, 278, 43, 18)
        _grey_abs(page, name + ".tc", "25", 542, 278, 46, 18)
        _grey_abs(page, name + ".tcmax", "65", 623, 278, 43, 18)
        _abs_text(page, "Tc =", 436, 278, 22, 18, "r")
        _abs_text(page, "min", 512, 278, 16, 18)
        _abs_text(page, "nom", 592, 278, 20, 18)
        _abs_text(page, "max °C", 672, 278, 33, 18)
        _abs(page, b, 413, 330, 81, 22)     # Calculate [F4]
        # 底表 Bias Analysis（表标题由 circ spec labels 绘制，勿加 QLabel）
        page.table = _abs_table(page, name + ".table", 41, 418, 331, 66,
                                4, 4, (67, 88, 88, 86), 16)
        _tset(page.table, 0, 0, "Tc =", Qt.AlignRight | Qt.AlignVCenter)
        for _r, _lab in ((1, "Id (mA) ="), (2, "Pd (mW) ="),
                         (3, "Tj (°C) =")):
            _tset(page.table, _r, 0, _lab, Qt.AlignLeft | Qt.AlignVCenter)
        page.dg = dg
        return page
    if divider:
        left.addWidget(b)
        left.addStretch(1)

    if not divider:
        # audit blocker 5：右列恢复真实输入框（预填原图默认值；引擎未建模 →
        # 灰字样式 + tooltip「引擎未建模，仅版式复刻」，仍可编辑）
        _TIP = "引擎未建模，仅版式复刻"

        def _grey(obj, val):
            bx = _num_box(obj, val)
            bx.setStyleSheet("color:#777777;")
            bx.setToolTip(_TIP)
            return bx

        gb1, l1 = _grp("FET Parameters")
        for lab, key, val, unit in (
                ("V'p min =", "vpmin", "-0.65", ""),
                ("V'p typ =", "vptyp", "-0.5", "V"),
                ("V'p max =", "vpmax", "-0.35", ""),
                ("V'p spec'd at =", "vp10", "10", ""),
                ("% Idss =", "idss_pct", "100", ""),
                ("ΔVp/T =", "dvpt", "0", "mV/°C"),
                ("Vp (typ，结果) =", "vp", "", "V"),
                ("Idss min =", "idssmin", "58", ""),
                ("Idss typ =", "idss", "65", "mA"),
                ("Idss max =", "idssmax", "72", ""),
                ("ΔIdss/T =", "didsst", "-0.13", "%/°C"),
                ("θjc =", "tjc", "300", "°C/W")):
            l1.addWidget(_grow(lab, _grey(name + "." + key, val), unit))
        page._right.addWidget(gb1)
        gb2, l2 = _grp("Analysis Temperatures")
        for lab, key, val, unit in (("Tc min =", "tcmin", "-25", ""),
                                    ("Tc nom =", "tc", "25", "°C"),
                                    ("Tc max =", "tcmax", "65", "")):
            l2.addWidget(_grow(lab, _grey(name + "." + key, val), unit))
        page._right.addWidget(gb2)
    else:
        gb1, l1 = _grp("器件参数 FET Parameters")
        lab = _ql("Vp / Idss / θjC / ΔVp·ΔIdss（min·typ·max）")
        lab.setStyleSheet("color:#909090;")
        l1.addWidget(lab)
        lab = _ql("（引擎未建模，灰显）")
        lab.setStyleSheet("color:#909090;")
        l1.addWidget(lab)
        page._right.addWidget(gb1)
        gb2, l2 = _grp("分析温度 Analysis Temperatures（引擎未建模，灰显）")
        lab = _ql("Tc(min·nom·max) 温度扫描 —— 本版只做名义偏置")
        lab.setStyleSheet("color:#909090;")
        l2.addWidget(lab)
        page._right.addWidget(gb2)
    page._right.addWidget(_ql("Bias Analysis（名义值）："))
    if divider:
        labels = ["VS (V)", "Id (mA)", "P(RD) (mW)", "P(RS) (mW)",
                  "R1 (Ω)", "R2 (Ω)"]
        page.table = _btable(labels, name + ".table")
    else:
        # spec §1.3：首行「Tc =」表头 + Id/Pd/Tj 三数据行；计算前数据格空白
        page.table = _btable(["Tc =", "Id (mA)", "Pd (mW)", "Tj (°C)"],
                             name + ".table")
        for r_ in range(page.table.rowCount()):
            for c_ in range(1, page.table.columnCount()):
                it = page.table.item(r_, c_)
                if it is not None:
                    it.setText("")
    page._right.addWidget(page.table)
    if not divider:
        # audit blocker 5：Calculate 小按钮（原图 81x22）放右列，不是整宽底条
        b.setFixedWidth(112)
        page._right.addWidget(b)
    page._right.addStretch(1)
    page.dg = dg
    return page


# --- PAE ---------------------------------------------------------------------

class _DgPae(_Diagram):
    NAME = "pae"
    LW = 430.0

    def draw(self, p):
        _line(p, 30, 160, 120, 160)
        _poly(p, [(130, 110), (130, 210), (250, 160)], HILITE)
        _line(p, 250, 160, 400, 160)
        self.label(150, 100, "PAE")
        _line(p, 60, 148, 60, 172)
        self.label(40, 138, "Pin")


def _mk_pae(win):
    dg = _DgPae()
    dg.box("pin", 30, 176, "-30", obj="pae.pin")
    dg.label(0, 178, "Pin =")
    dg.label(88, 178, "dBm")
    dg.box("pout", 300, 100, "40", obj="pae.pout")
    dg.label(252, 102, "Pout =")
    dg.label(360, 102, "dBm")
    dg.box("pdc", 300, 140, "10", obj="pae.pdc")
    dg.label(252, 142, "PDC =")
    dg.label(360, 142, "W")
    dg.box("gain", 300, 180, "", ro=True, obj="pae.result.gain")
    dg.label(252, 182, "Gain =")
    dg.label(360, 182, "dB")
    dg.finalize()

    page = _b_shell(win, "pae", "放大器效率 PAE")
    page.dg = dg
    left = page._left
    left.addWidget(dg)
    left.addWidget(_instructions([
        "1. 输入 Pin / Pout（dBm）与直流功耗 PDC（W）。",
        "2. 点击 Calculate [F4] 计算 PAE、漏极效率与增益。"]))
    b = QtWidgets.QPushButton("Calculate [F4]")
    b.setObjectName("pae.calc")

    def calc():
        e = _e()
        pin = _f(dg.fields["pin"], "Pin")
        pout = _f(dg.fields["pout"], "Pout")
        pdc = _f(dg.fields["pdc"], "PDC")
        r = e.amp_efficiency(pin, pout, pdc)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        dg.fields["gain"].setText(_g(r["gain_db"]))
        for i, key in enumerate(("pae_pct", "drain_eff_pct", "gain_db",
                                 "pout_w", "pin_w")):
            _bfill(page.table, i, r[key])
        return "PAE=%s%% 漏极效率=%s%%" % (_g(r["pae_pct"]), _g(r["drain_eff_pct"]))

    b.clicked.connect(lambda: _run_calc(page, calc))
    left.addWidget(b)
    left.addStretch(1)
    page._right.addWidget(_ql("Bias Analysis："))
    page.table = _btable(["PAE (%)", "漏极效率 (%)", "Gain (dB)", "Pout (W)", "Pin (W)"],
                         "pae.table")
    page._right.addWidget(page.table)
    page._right.addStretch(1)
    return page


# ---------------------------------------------------------------------------
# C 模板：NoiseCalc 栅格页
# ---------------------------------------------------------------------------

_NC_HEAD, _NC_ICON, _NC_TYPE = 0, 1, 2
_NC_NAME, _NC_NF, _NC_GAIN, _NC_OIP3, _NC_DNFT, _NC_DGT = 3, 4, 5, 6, 7, 8
_NC_SEC = 9
_NC_NFT, _NC_GT, _NC_PIN, _NC_POUT, _NC_D1, _NC_D2, _NC_D3 = 10, 11, 12, 13, 14, 15, 16

_NC_DEF_NF = [2, 1, 3, 2, 6, 3, 6]
_NC_DEF_GAIN = [3, 14, 4, 14, 12, 3, 50]
_NC_DEF_OIP3 = [20, 25, 30, 25, 20, 30, 25]
# clean-room 中性默认名（实机含 Avago/商标字样，本版一律自拟）
_NC_DEF_NAME = ["Duplexer", "LNA-1", "Image Filter", "LNA-2", "Mixer",
                "IF Filter", "LNA-3"]
_NC_TYPES = ["放大器", "滤波器", "混频器", "矩形"]
# 默认级联类型（对应默认名；spec 22 的链路：滤波-放大-滤波-放大-混频-滤波-用户器件）
_NC_DEF_KIND = ["滤波器", "放大器", "滤波器", "放大器", "混频器", "滤波器",
                "矩形"]


def _nc_icon(kind, size=30):
    """spec 22 栅格列头图标（30×30 逻辑，QPainter 自绘，clean-room；原图 30-34px）：
    滤波器=圆角方 + 3 条蓝波线；放大器=三角 + 器件字；混频器=圆 + X；矩形=用户器件。"""
    key = ("nc", kind, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    s = size / 30.0
    p.scale(s, s)
    if kind == "滤波器":
        p.setBrush(QtGui.QColor(ICONBG))
        _pen(p, "#656FAD", 1.6)
        p.drawRoundedRect(QtCore.QRectF(1, 3, 28, 24), 3, 3)
        qb = QtGui.QPen(QtGui.QColor("#4994D2"))
        qb.setWidthF(1.4)
        p.setPen(qb)
        for i, y in enumerate((10, 15, 20)):
            path = QtGui.QPainterPath()
            path.moveTo(5, y)
            path.cubicTo(9, y - 4, 12, y + 4, 15, y)
            path.cubicTo(18, y - 4, 21, y + 4, 25, y)
            p.strokePath(path, p.pen())
    elif kind == "混频器":
        p.setBrush(QtGui.QColor(ICONBG))
        _pen(p, "#656FAD", 1.6)
        p.drawEllipse(QtCore.QRectF(5, 6, 20, 20))
        _line(p, 10, 11, 20, 21, "#656FAD", 1.4)
        _line(p, 20, 11, 10, 21, "#656FAD", 1.4)
        _line(p, 15, 26, 15, 29, "#656FAD", 1.4)
    elif kind == "矩形":  # 用户器件（spec 22 S7：44x28 矩形 + 器件字）
        p.setBrush(QtGui.QColor(ICONBG))
        _pen(p, "#656FAD", 1.6)
        p.drawRect(QtCore.QRectF(1, 6, 28, 18))
        p.setPen(_pen(p, "#C0944A", 1.0))
        f = QtGui.QFont("Arial", 6)
        f.setBold(True)
        p.setFont(f)
        p.drawText(9, 19, "U1")
    else:  # 放大器
        _poly(p, [(3, 25), (3, 5), (27, 15)], ICONBG, "#656FAD", 1.6)
        p.setPen(_pen(p, "#C0944A", 1.0))
        f = QtGui.QFont("Arial", 6)
        f.setBold(True)
        p.setFont(f)
        p.drawText(6, 19, "A")
    p.end()
    _ICON_CACHE[key] = pm
    return QtGui.QIcon(pm)


class _Led(QtWidgets.QLabel):
    def __init__(self, name="led"):
        super().__init__()
        self.setObjectName(name)
        self.setFixedSize(16, 16)
        self._ok = True
        pm = QtGui.QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setBrush(QtGui.QColor(LED_G))
        _pen(p, "#333333", 1.0)
        p.drawEllipse(1, 1, 10, 10)
        p.end()
        self.setPixmap(pm)

    def set_state(self, ok, msg=""):
        self._ok = ok
        pm = QtGui.QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setBrush(QtGui.QColor(LED_G if ok else LED_R))
        _pen(p, "#333333", 1.0)
        p.drawEllipse(1, 1, 10, 10)
        p.end()
        self.setPixmap(pm)
        w = self.window()
        if w is not None:
            led_text = w.findChild(QtWidgets.QLabel, "noisecalc.ledtext")
            if led_text is not None:
                led_text.setText(msg or ("Normal" if ok else "错误"))
                led_text.setStyleSheet(
                    "color:%s;" % ("#000000" if ok else LED_R))


def _nc_item(text, editable=True, bg=None, bold=False, gray=False, icon=None):
    it = QtWidgets.QTableWidgetItem(text)
    flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
    if editable:
        flags |= Qt.ItemIsEditable
    it.setFlags(flags)
    if bg:
        it.setBackground(QtGui.QColor(bg))
    if gray:
        it.setForeground(QtGui.QColor("#909090"))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if icon is not None:
        it.setIcon(icon)
        it.setTextAlignment(Qt.AlignCenter)
    return it


def _nc_rebuild(page):
    n = page.stages.value()
    t = page.table
    page._type_combos = []
    cols = 2 + n
    t.clear()
    t.setColumnCount(cols)
    t.setRowCount(_NC_D3 + 1)
    t.verticalHeader().setVisible(False)
    t.setColumnWidth(0, 101)
    t.setColumnWidth(1, 57)
    for c in range(2, cols):
        t.setColumnWidth(c, 67)
    # 行 0：Stage 标题
    t.setItem(_NC_HEAD, 0, _nc_item("Stage Data", editable=False, bg=GRIDHEAD, bold=True))
    t.setItem(_NC_HEAD, 1, _nc_item("Units", editable=False, bg=GRIDHEAD, bold=True))
    for i in range(n):
        t.setItem(_NC_HEAD, 2 + i, _nc_item("Stage %d" % (i + 1), editable=False,
                                            bg=GRIDHEAD, bold=True))
        kind = _NC_DEF_KIND[i % len(_NC_DEF_KIND)]
        it = _nc_item(_NC_DEF_NAME[i % len(_NC_DEF_NAME)], editable=False, bg=ICONBG,
                      icon=_nc_icon(kind, 30))
        t.setItem(_NC_ICON, 2 + i, it)
        t.setRowHeight(_NC_ICON, 38)
        combo = QtWidgets.QComboBox()
        combo.addItems(_NC_TYPES)
        combo.setObjectName("noisecalc.stage%d.type" % (i + 1))
        combo.setCurrentIndex(_NC_TYPES.index(kind))

        def make_cb(idx, cell=2 + i):
            def cb():
                kind = _NC_TYPES[idx]
                t.item(_NC_ICON, cell).setIcon(_nc_icon(kind, 30))
            return cb

        combo.currentIndexChanged.connect(make_cb(combo.currentIndex()))
        page._type_combos.append(combo)
        t.setCellWidget(_NC_TYPE, 2 + i, combo)
    t.setItem(_NC_TYPE, 0, _nc_item("类型", editable=False, bg=GRIDHEAD))
    t.setItem(_NC_TYPE, 1, _nc_item("", editable=False, bg=GRIDHEAD))
    data_rows = [
        (_NC_NAME, "Stage Name:", "", True),
        (_NC_NF, "Noise Figure", "dB", True),
        (_NC_GAIN, "Gain", "dB", True),
        (_NC_OIP3, "Output IP3", "dBm", True),
        (_NC_DNFT, "dNF/dTemp", "dB/°C", False),
        (_NC_DGT, "dG/dT", "dB/°C", False),
    ]
    for row, lab, unit, editable in data_rows:
        t.setItem(row, 0, _nc_item(lab, editable=False, bg=GRIDHEAD))
        t.setItem(row, 1, _nc_item(unit, editable=False, bg=GRIDHEAD))
        for i in range(n):
            if editable:
                if row == _NC_NAME:
                    val = _NC_DEF_NAME[i % len(_NC_DEF_NAME)]
                elif row == _NC_NF:
                    val = _fmt2(_NC_DEF_NF[i % len(_NC_DEF_NF)])
                elif row == _NC_GAIN:
                    val = _fmt2(_NC_DEF_GAIN[i % len(_NC_DEF_GAIN)])
                else:
                    val = _fmt2(_NC_DEF_OIP3[i % len(_NC_DEF_OIP3)])
                t.setItem(row, 2 + i, _nc_item(val, editable=True))
            else:
                t.setItem(row, 2 + i, _nc_item("-", editable=False, gray=True,
                                               bg="#EDEDED"))
    t.setSpan(_NC_SEC, 0, 1, 2)
    t.setItem(_NC_SEC, 0, _nc_item("Stage Analysis", editable=False, bg=GRIDHEAD,
                                   bold=True))
    ana_rows = [
        (_NC_NFT, "NF (Temp cor)", "dB", True),
        (_NC_GT, "Gain (Temp cor)", "dB", True),
        (_NC_PIN, "Input Power", "dBm", True),
        (_NC_POUT, "Output Power", "dBm", True),
        (_NC_D1, "dNF/dNF", "dB/dB", False),
        (_NC_D2, "dNF/dGain", "dB/dB", False),
        (_NC_D3, "dIP3/dIP3", "dBm/dBm", False),
    ]
    for row, lab, unit, fillable in ana_rows:
        t.setItem(row, 0, _nc_item(lab, editable=False, bg=GRIDHEAD))
        t.setItem(row, 1, _nc_item(unit, editable=False, bg=GRIDHEAD))
        for i in range(n):
            if fillable:
                t.setItem(row, 2 + i, _nc_item("", editable=False))
            else:
                t.setItem(row, 2 + i, _nc_item("-", editable=False, gray=True,
                                               bg="#EDEDED"))
    # 行高：表头 18px、类型行 24px（combo 需要）、其余 16px（spec 22）
    for r in range(_NC_D3 + 1):
        t.setRowHeight(r, 18 if r == _NC_HEAD else (24 if r == _NC_TYPE else 16))


def _nc_cell(page, row, col):
    return page.table.item(row, col)


def _mk_noisecalc(win):
    page = QtWidgets.QWidget()
    page.setObjectName("noisecalcpg")
    page.setStyleSheet(_page_css("noisecalc", C_BG))
    page.name = "noisecalc"
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None
    page._type_combos = []
    win._pages["noisecalc"] = page
    win._order.append("noisecalc")
    win._stack.addWidget(page)

    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 4)
    v.setSpacing(6)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 22)
    tf.setBold(True)
    title = QtWidgets.QLabel("NoiseCalc")
    title.setFont(tf)
    top.addWidget(title)
    top.addSpacing(20)
    top.addWidget(_ql("Set Number of Stages ="))
    sp = QtWidgets.QSpinBox()
    sp.setObjectName("noisecalc.stages")
    sp.setRange(1, 10)
    sp.setValue(7)
    page.stages = sp
    top.addWidget(sp)
    calc_b = QtWidgets.QPushButton("Calculate [F4]")
    calc_b.setObjectName("noisecalc.calc")
    top.addWidget(calc_b)
    top.addStretch(1)
    clear_b = QtWidgets.QPushButton("Clear")
    clear_b.setObjectName("noisecalc.clear")
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName("noisecalc.mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(clear_b)
    top.addWidget(mm)
    v.addLayout(top)

    page.table = QtWidgets.QTableWidget()
    page.table.setObjectName("noisecalc.table")
    page.table.horizontalHeader().setVisible(False)
    page.table.verticalHeader().setVisible(False)
    page.table.setStyleSheet("QTableWidget { gridline-color: %s; background: #FFFFFF; }"
                             % GRIDLINE)
    v.addWidget(page.table, 1)
    sp.valueChanged.connect(lambda _v: _nc_rebuild(page))
    clear_b.clicked.connect(lambda: _nc_clear(page))

    boxes = QtWidgets.QHBoxLayout()
    gb1, l1 = _grp("Enter System Parameters")
    page.pin_box = _num_box("noisecalc.pin", "-50")
    page.tamb_box = _num_box("noisecalc.tamb", "25")
    page.bw_box = _num_box("noisecalc.bw", "1")
    page.snr_box = _num_box("noisecalc.snr", "10")
    page.reft_box = _num_box("noisecalc.reft", "25")
    page.nsrc_box = _num_box("noisecalc.nsrc", "290", ro=True)
    for lab, bx, unit in (("输入功率 Input Power", page.pin_box, "dBm"),
                          ("分析温度 Analysis Temp", page.tamb_box, "°C"),
                          ("噪声带宽 Noise BW", page.bw_box, "MHz"),
                          ("所需信噪比 S/N", page.snr_box, "dB"),
                          ("参考温度 Ref Temp", page.reft_box, "°C（显示用）"),
                          ("噪声源(参考) Noise Src", page.nsrc_box, "°K")):
        l1.addWidget(_grow(lab, bx, unit))
    gb2, l2 = _grp("System Analysis")
    res_spec = (("gain", "Gain"), ("nf", "Noise Figure"), ("ttemp", "Noise Temp"),
                ("snr", "SNR"), ("mds", "MDS"), ("sens", "Sensitivity"),
                ("floor", "Noise Floor"))
    for key, lab in res_spec:
        le = _num_box("noisecalc.result." + key, "", w=70, ro=True)
        page.results[key] = le
        row = _grow(lab + " =", le)
        l2.addWidget(row)
        if key == "nf":
            le.setStyleSheet("background:#FFFFC8;")
    units = {"gain": "dB", "nf": "dB", "ttemp": "°K", "snr": "dB", "mds": "dBm",
             "sens": "dBm", "floor": "dBm/Hz"}
    for i, (key, _lab) in enumerate(res_spec):
        row = l2.itemAt(i)
        w_ = row.widget() if row is not None else None
        if w_ is not None:
            hh = w_.layout()
            hh.insertWidget(hh.count() - 1, _ql(units[key]))
    gb3, l3 = _grp("IM (Intermod)")
    # audit：恢复 7 行版式（Input/Output IP3 + IM level ×3 + SFDR）；
    # 引擎算得出的（级联 IIP3/OIP3）计算后填值，其余灰字 "-"
    im_lab = _ql("级联 IM（IP3 由级联噪声引擎给出；IM level/SFDR 未建模）")
    im_lab.setStyleSheet("color:#909090;")
    l3.addWidget(im_lab)
    page.im_boxes = {}
    for key, txt in (("ip3in", "Input: IP3 ="), ("ip3out", "Output: IP3 ="),
                     ("imlin", "Input: IM level ="),
                     ("imlout", "Output: IM level ="),
                     ("imldbc", "IM level (dBc) ="), ("sfdr", "SFDR =")):
        rr = QtWidgets.QWidget()
        hh = QtWidgets.QHBoxLayout(rr)
        hh.setContentsMargins(0, 1, 0, 1)
        hh.setSpacing(4)
        lab = _ql(txt, align_right=True)
        lab.setStyleSheet("color:#909090;")
        hh.addWidget(lab)
        bx = _num_box("noisecalc.im." + key, "-", ro=True)
        bx.setStyleSheet("color:#909090; background:#F5F5F5;")
        hh.addWidget(bx)
        hh.addWidget(_ql("dBm" if key not in ("imldbc", "sfdr") else "dB"))
        hh.addStretch(1)
        page.im_boxes[key] = bx
        l3.addWidget(rr)
    boxes.addWidget(gb1)
    boxes.addWidget(gb2)
    boxes.addWidget(gb3)
    boxes.addStretch(1)
    v.addLayout(boxes)

    bottom = QtWidgets.QHBoxLayout()
    page.led = _Led("noisecalc.led")
    led_text = QtWidgets.QLabel("Normal")
    led_text.setObjectName("noisecalc.ledtext")
    err = QtWidgets.QLabel("")
    err.setObjectName("noisecalc.error")
    err.setStyleSheet("color:%s;font-weight:bold;" % LED_R)
    err.hide()
    page.err = err
    bottom.addWidget(page.led)
    bottom.addWidget(led_text)
    bottom.addWidget(err)
    bottom.addStretch(1)
    v.addLayout(bottom)

    def calc():
        e = _e()
        n = page.stages.value()
        stages = []
        for i in range(n):
            nf = _fitem(_nc_cell(page, _NC_NF, 2 + i), "第%d级 NF" % (i + 1))
            gain = _fitem(_nc_cell(page, _NC_GAIN, 2 + i), "第%d级 Gain" % (i + 1))
            oit = _nc_cell(page, _NC_OIP3, 2 + i)
            txt = str(oit.text()).strip() if oit is not None else ""
            oip3 = float(txt) if txt else None
            stages.append({"gain_db": gain, "nf_db": nf, "oip3_dbm": oip3})
        bw = _e().to_si(_f(page.bw_box, "带宽"), "MHz", "freq")
        ta = _f(page.tamb_box, "分析温度") + 273.15
        r = e.cascade_noise(stages, bw, ta)
        if not r.get("ok"):
            raise _InputError(r.get("error", "引擎返回失败"))
        pin = _f(page.pin_box, "输入功率")
        snr_req = _fopt(page.snr_box, 10.0, "S/N")
        ktb = r.get("kTb_dbm")
        nf_db = r.get("nf_db")
        mds = (ktb + nf_db) if ktb is not None and nf_db is not None else None
        page.results["gain"].setText(_fmt2(r.get("gain_db")))
        page.results["nf"].setText(_fmt2(nf_db))
        page.results["ttemp"].setText(_fmt2(r.get("tsys_k")))
        page.results["mds"].setText(_fmt2(mds))
        page.results["snr"].setText(_fmt2(pin - mds) if mds is not None else "-")
        page.results["sens"].setText(_fmt2(mds + snr_req) if mds is not None else "-")
        if ktb is not None and bw > 0:
            page.results["floor"].setText(_fmt2(ktb - 10.0 * math.log10(bw) + nf_db))
        else:
            page.results["floor"].setText("-")
        # IM 盒：引擎给出的级联 IIP3/OIP3 填值；IM level/SFDR 未建模保持灰 "-"
        imr = {"ip3in": r.get("iip3_dbm"), "ip3out": r.get("oip3_dbm"),
               "imlin": None, "imlout": None, "imldbc": None, "sfdr": None}
        for key, v in imr.items():
            bx = page.im_boxes.get(key)
            if bx is not None:
                bx.setText(_fmt2(v) if v is not None else "-")
        g_acc = 0.0
        t = page.table
        for i in range(n):
            gdB = _fitem(_nc_cell(page, _NC_GAIN, 2 + i), "gain")
            ndB = _fitem(_nc_cell(page, _NC_NF, 2 + i), "nf")
            t.setItem(_NC_NFT, 2 + i, _nc_item(_fmt2(ndB), editable=False))
            t.setItem(_NC_GT, 2 + i, _nc_item(_fmt2(gdB), editable=False))
            t.setItem(_NC_PIN, 2 + i, _nc_item(_fmt2(pin + g_acc), editable=False))
            t.setItem(_NC_POUT, 2 + i, _nc_item(_fmt2(pin + g_acc + gdB),
                                                editable=False))
            g_acc += gdB
        # 逐级灵敏度分析（spec 22 口径，clean-room 公式）：
        #   dNF/dNF_i  = (F_i / F_tot) / Π_{j<i} G_j（各级噪声贡献占比）
        #   dNF/dG_i   = -(F_after_i - 1) / (G_i · F_tot · Π_{j<i} G_j)
        #   dIP3/dIP3_i = term_i / Σterm，term_i = 1/(OIP3_lin_i · Π_{j>i} G_j)
        Fs = [10.0 ** (st["nf_db"] / 10.0) for st in stages]
        Gs = [10.0 ** (st["gain_db"] / 10.0) for st in stages]
        f_tot = 10.0 ** (nf_db / 10.0)
        dnf_bg = {0: "#B1B1FF", 1: "#A1A1FF", 2: "#ECECFF", 3: "#ECECFF"}
        dgn_bg = {0: "#E4F6E4", 1: "#F7FFF7", 2: "#F4FAF4", 3: "#F4FAF4"}
        terms = []
        for i in range(n):
            o = stages[i].get("oip3_dbm")
            tail = 1.0
            for j in range(i + 1, n):
                tail *= Gs[j]
            terms.append((1.0 / (10.0 ** (o / 10.0) * tail)) if o else None)
        tsum = sum(v for v in terms if v is not None)
        for i in range(n):
            pre = 1.0
            for j in range(i):
                pre *= Gs[j]
            c_nf = (Fs[i] / f_tot) / pre if f_tot else None
            t.setItem(_NC_D1, 2 + i, _nc_item(_fmt2(c_nf) if c_nf is not None
                                              else "-", editable=False,
                                              bg=dnf_bg.get(i)))
            fa = 1.0
            for j in range(i + 1, n):
                den = 1.0
                for k in range(i + 1, j):
                    den *= Gs[k]
                fa += (Fs[j] - 1.0) / den
            c_gn = -(fa - 1.0) / (Gs[i] * f_tot * pre) if f_tot else None
            t.setItem(_NC_D2, 2 + i, _nc_item(_fmt2(c_gn) if c_gn is not None
                                              else "-", editable=False,
                                              bg=dgn_bg.get(i)))
            if terms[i] is not None and tsum:
                pct = terms[i] / tsum
                over = pct >= 0.99
                t.setItem(_NC_D3, 2 + i, _nc_item(_fmt2(pct), editable=False,
                                                  bg="#FF9999" if over else None,
                                                  bold=over))
            else:
                t.setItem(_NC_D3, 2 + i, _nc_item("-", editable=False, gray=True,
                                                  bg="#EDEDED"))
        page.led.set_state(True, "Normal")
        page.err.hide()
        msg = "Gain=%s dB, NF=%s dB" % (_fmt2(r.get("gain_db")), _fmt2(nf_db))
        if r.get("warnings"):
            msg += "（⚠ %s）" % "; ".join(r["warnings"])
        return msg

    calc_b.clicked.connect(lambda: _nc_calc_wrap(page, calc))

    _nc_rebuild(page)
    return page


def _nc_calc_wrap(page, fn):
    """NoiseCalc 计算兜底：坏格/异常 → LED 红 + ✗ + 状态栏，绝不崩。"""
    try:
        msg = fn()
        page.window().status().showMessage("noisecalc：%s" % msg, 8000)
    except _InputError as exc:
        page.led.set_state(False, "错误")
        page.err.setText("✗ " + str(exc))
        page.err.show()
        page.window().status().showMessage("noisecalc：失败——%s" % exc)
    except Exception as exc:
        page.led.set_state(False, "错误")
        page.err.setText("✗ 界面错误：%s: %s" % (type(exc).__name__, exc))
        page.err.show()
        page.window().status().showMessage(
            "noisecalc：失败——%s: %s" % (type(exc).__name__, exc))


def _nc_clear(page):
    page.stages.setValue(1)
    _nc_rebuild(page)
    for key in ("gain", "nf", "ttemp", "snr", "mds", "sens", "floor"):
        if key in page.results:
            page.results[key].setText("")
    page.led.set_state(True, "Normal")
    page.err.hide()


# ---------------------------------------------------------------------------
# A 变体：反射计算器（Enter 驱动）
# ---------------------------------------------------------------------------

class _DgReflection(_Diagram):
    NAME = "reflection"
    LW = 660.0  # spec 21：右列 |Γ|/ZI/YI 框 x568..632，画布 1:1 直用原图坐标

    def draw(self, p):
        # 源支路：电阻符号（中心 228,154）+ Vs 电压源（圆 + 正弦）
        _zig_v(p, 228, 146, 162, 4.0, n=4)
        _line(p, 228, 146, 250, 146)                     # Zs 顶接左母线
        p.setBrush(QtGui.QColor("#FFFFFF"))
        _pen(p)
        p.drawEllipse(QtCore.QPointF(222, 192), 11, 11)
        # 明显正弦（~26px 宽，QPainterPath 贝塞尔）
        sine = QtGui.QPainterPath()
        sine.moveTo(210, 194)
        sine.cubicTo(214, 187, 218, 187, 222, 194)
        sine.cubicTo(226, 201, 230, 201, 234, 194)
        p.setFont(QtGui.QFont("Times New Roman", 11))
        p.drawText(218, 186, "Vs")
        p.setFont(QtGui.QFont(self.font()))
        _pen(p)
        p.strokePath(sine, p.pen())
        _line(p, 233, 192, 250, 192)                     # Vs 右缘接左母线
        # 连线：左右母线 + 四角空心节点 + 上下短接线
        _line(p, 250, 145, 250, 218)
        _line(p, 460, 145, 460, 218)
        _line(p, 250, 144, 276, 144)
        _line(p, 430, 144, 460, 144)
        _line(p, 250, 218, 276, 218)
        _line(p, 430, 218, 460, 218)
        p.setBrush(QtGui.QColor("#FFFFFF"))
        _pen(p)
        for cx, cy in ((250, 144), (460, 144), (250, 218), (460, 218)):
            p.drawEllipse(QtCore.QPointF(cx, cy), 4, 4)
        # 双电缆（上/下，灰填充黑描边）
        for y0 in (138, 212):
            _poly(p, [(276, y0), (430, y0), (430, y0 + 12), (276, y0 + 12)],
                  "#DCDCDD")
        self.label(327, 181, "Zo = Zs")
        # Load 三维盒（#B4CFCB 正面 + 顶/侧面同色系）
        _poly(p, [(466, 165), (500, 165), (500, 200), (466, 200)], "#B4CFCB")
        _poly(p, [(466, 165), (474, 158), (508, 158), (500, 165)], "#C6DCD8")
        _poly(p, [(500, 165), (508, 158), (508, 193), (500, 200)], "#9FB9B5")
        self.label(420, 181, "Load")
        # 大括号（罩住右列 |Γ|/ZI/YI 三行）
        p.setFont(QtGui.QFont("Times New Roman", 40))
        p.drawText(514, 224, "{")
        p.setFont(QtGui.QFont(self.font()))
        # 前向绿缎带箭头（尾部 V 形缺口 + 三角头）
        _poly(p, [(253, 252), (436, 252), (436, 244), (459, 269), (436, 293),
                  (436, 285), (253, 285), (265, 268)], "#D2EFC5")
        # 回波弧形箭头（从主体下方弯回，指向左）
        path = QtGui.QPainterPath()
        path.moveTo(415, 283)
        path.quadTo(385, 304, 372, 297)
        _pen(p, "#000000", 1.2)
        p.setBrush(Qt.NoBrush)
        p.strokePath(path, p.pen())
        _head(p, 369, 296, -0.97, -0.26, "#000000")


def _mk_reflection(win):
    dg = _DgReflection()
    dg.box("zs", 134, 145, "50", w=53, h=21, obj="refl.zs")
    dg.label(104, 158, "Zs =")
    dg.label(193, 160, "Ω")
    dg.label(190, 200, "Vs")
    # 右上反射参数列（|Γ| / Γ∠ / ZI / YI；gdeg 为引擎反射角辅助行）
    dg.box("gamma", 568, 152, "0.5", w=64, h=21, obj="refl.gamma")
    dg.label(526, 166, "|Γ| =")
    dg.box("gdeg", 568, 177, "", w=64, h=21, obj="refl.gdeg")
    dg.label(526, 191, "Γ∠°")
    dg.box("zl", 568, 202, "", w=64, h=21, obj="refl.zl")
    dg.label(526, 216, "ZI =")
    dg.label(638, 216, "Ω")
    dg.box("yl", 568, 227, "", w=64, h=21, obj="refl.yl")
    dg.label(526, 241, "YI =")
    dg.label(637, 241, "mho")
    # 功率行（绿箭头区，spec 21 原位）：Pi / Pt / Pr 任填两个回车算第三个
    dg.box("pt", 117, 262, "", w=64, h=21, obj="refl.pt")
    dg.label(87, 278, "Pi =")
    dg.box("pt2", 490, 259, "", w=64, h=21, obj="refl.pt2")
    dg.label(455, 276, "Pt =")
    dg.box("pr", 266, 304, "", w=64, h=21, obj="refl.pr")
    dg.label(228, 320, "Pr =")
    dg.finalize()

    def rows(page):
        out = []
        out.append(_ql("1.  Input source impedance, Zs"))
        out.append(_ql("2.  Input any parameter in the right column."))
        out.append(_ql("    Press Enter (with cursor in the entry field) to"))
        out.append(_ql("    calculate remaining reflection parameters."))
        out.append(_ql("3.  Input any of the three power levels. Press"))
        out.append(_ql("    Enter to calculate the other two powers."))
        # dBm/dBW 下拉（QComboBox 以子控件钉在画布 1:1 位置）
        pu = QtWidgets.QComboBox()
        pu.setObjectName("refl.punit")
        pu.addItems(["dBm", "dBW"])
        pu.setParent(dg)
        pu.setGeometry(186, 259, 67, 24)
        page.combos["refl.punit"] = pu
        page.pu2_lab = _ql("dBm")
        page.pu2_lab.setParent(dg)
        page.pu2_lab.setGeometry(558, 263, 34, 16)
        page.pu3_lab = _ql("dBm")
        page.pu3_lab.setParent(dg)
        page.pu3_lab.setGeometry(333, 307, 34, 16)

        def _pu_labels(*_):
            # dBm/dBW 切换时同步 Pt(Load)/Pr 的单位后缀
            t = pu.currentText()
            page.pu2_lab.setText(t)
            page.pu3_lab.setText(t)

        pu.currentIndexChanged.connect(_pu_labels)
        page.pt_box = dg.fields["pt"]
        page.pt2_box = dg.fields["pt2"]
        page.pr_box = dg.fields["pr"]
        out.append(_res_row(page, "vswr", "SwR", ": 1"))
        out.append(_res_row(page, "rl", "Return Loss, RL", "dB"))
        out.append(_res_row(page, "lmn", "Mismatch Loss, Lmn", "dB"))
        out.append(_res_row(page, "t_ratio", "Transmission Coefficient, T",
                            "(ratio)"))
        out.append(_res_row(page, "t_db", "Transmission Coefficient, T", "dB"))
        # 底部灰色附加小节（引擎附加工具；实机此页无，灰显区分）
        gb, l = _grp("附加工具（引擎扩展，实机此页无——灰显）")
        gb.setStyleSheet("color:#777777;")
        gbq = QtWidgets.QWidget()
        hq = QtWidgets.QHBoxLayout(gbq)
        hq.setContentsMargins(0, 0, 0, 0)
        hq.setSpacing(4)
        qzs = _num_box("refl.qw.zs", "25", w=60)
        qzl = _num_box("refl.qw.zl", "50", w=60)
        qout = _num_box("refl.result.zt", "", w=70, ro=True)
        page.results["zt"] = qout
        qb = QtWidgets.QPushButton("λ/4 变换")
        qb.setObjectName("refl.qwcalc")

        def qcalc():
            e = _e()
            r = e.quarter_wave_transform(_f(qzs, "Zs"), _f(qzl, "ZL"))
            if not r.get("ok"):
                raise _InputError(r.get("error", "引擎返回失败"))
            qout.setText(_g(r["z_t"]))
            return "Zt=%s Ω" % _g(r["z_t"])

        qb.clicked.connect(lambda: _run_calc(page, qcalc))
        hq.addWidget(_ql("Zs=", w=26))
        hq.addWidget(qzs)
        hq.addWidget(_ql("ZL=", w=26))
        hq.addWidget(qzl)
        hq.addWidget(qb)
        hq.addWidget(qout)
        hq.addWidget(_ql("Ω"))
        hq.addStretch(1)
        l.addWidget(gbq)
        gbl = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(gbl)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        lc_l = _num_box("refl.lc.l", "10", w=60)
        lc_c = _num_box("refl.lc.c", "10", w=60)
        lcout = _num_box("refl.result.f0", "", w=70, ro=True)
        page.results["f0"] = lcout
        lb = QtWidgets.QPushButton("LC 谐振")
        lb.setObjectName("refl.lccalc")

        def lcalc():
            e = _e()
            r = e.lc_resonance(_f(lc_l, "L") * 1e-9, _f(lc_c, "C") * 1e-12)
            if not r.get("ok"):
                raise _InputError(r.get("error", "引擎返回失败"))
            lcout.setText(_g(r["f0"] / 1e9))
            return "f0=%s GHz" % _g(r["f0"] / 1e9)

        lb.clicked.connect(lambda: _run_calc(page, lcalc))
        hl.addWidget(_ql("L=", w=20))
        hl.addWidget(lc_l)
        hl.addWidget(_ql("nH"))
        hl.addWidget(_ql("C=", w=20))
        hl.addWidget(lc_c)
        hl.addWidget(_ql("pF"))
        hl.addWidget(lb)
        hl.addWidget(lcout)
        hl.addWidget(_ql("GHz"))
        hl.addStretch(1)
        l.addWidget(gbl)
        out.append(gb)
        return out

    page = _a_shell(win, "reflection", "反射计算器 Reflection Calculator", dg,
                    mids_fn=None, buttons_fn=None, rows_fn=rows)

    # Enter 驱动：任一编辑框 editingFinished → 链式重算（实机此页无 Calculate 钮）
    src_map = {"refl.gamma": "gamma", "refl.zl": "zl", "refl.yl": "yl",
               "refl.zs": "zs", "refl.gdeg": "gamma"}

    def recalc(src):
        def _fn():
            e = _e()
            z0 = _fopt(dg.fields["zs"], 50.0, "Zs")
            if z0 <= 0:
                z0 = 50.0
            jx_b = dg.fields.get("jx")   # 旧版 jX 框已按 spec 21 移除；兼容取 0
            jx = _fopt(jx_b, 0.0, "jX") if jx_b is not None else 0.0
            if src == "gamma":
                mag = _f(dg.fields["gamma"], "|Γ|")
                deg = _fopt(dg.fields["gdeg"], 0.0, "Γ相位")
                r = e.refl_to_z(mag, deg, z0)
            elif src == "zl":
                r = e.refl_from_z(_f(dg.fields["zl"], "Zl"), jx, z0)
            elif src == "yl":
                y = _f(dg.fields["yl"], "Yl")
                if y == 0:
                    raise _InputError("Yl 不能为 0")
                r = e.refl_from_z(1.0 / y, 0.0, z0)
            else:
                r = e.refl_from_z(_f(dg.fields["zs"], "Zs"), jx, z0)
            if not r.get("ok"):
                raise _InputError(r.get("error", "引擎返回失败"))
            mag = r.get("gamma_mag")
            zc = r.get("gamma")
            _setr(page, {"vswr": r.get("vswr"), "rl": r.get("rl_db"),
                         "lmn": r.get("mismatch_loss_db")})
            tr = 1.0 - mag * mag if mag is not None else None
            _setr(page, {"t_ratio": tr,
                         "t_db": (10.0 * math.log10(tr)) if tr and tr > 0 else None})
            if src != "gamma":
                dg.fields["gamma"].setText(_g(mag))
                dg.fields["gdeg"].setText(_g(r.get("gamma_phase_deg")))
            if zc is not None:
                z = z0 * (1.0 + zc) / (1.0 - zc)
                if src != "zl":
                    dg.fields["zl"].setText(_g(z.real))
                if src != "yl" and z != 0:
                    dg.fields["yl"].setText(_g(1.0 / z))
            return "SwR=%s RL=%s dB" % (_g(r.get("vswr")), _g(r.get("rl_db")))
        return _fn

    for obj, src in src_map.items():
        le = page.findChild(QtWidgets.QLineEdit, obj)
        if le is not None:
            le.editingFinished.connect(_run_capture(page, recalc(src)))

    def pw_recalc(src):
        # 功率域（dBm）计算：Pi/Pt/Pr 任填两个回车算第三个；
        # dBm/dBW 单位经 refl.punit 换算（dBW = dBm + 30 dB）。
        def _fn():
            pu = page.combos.get("refl.punit")
            off = 30.0 if pu is not None and pu.currentText() == "dBW" else 0.0
            names = {"pt": "Pi", "pt2": "Pt (Load)", "pr": "Pr"}
            boxes = {"pt": page.pt_box, "pt2": page.pt2_box, "pr": page.pr_box}
            vals = {}
            for k, bx in boxes.items():
                v = _fopt(bx, None, names[k])
                if v is not None:
                    vals[k] = v + off          # 显示值 → dBm
            if len(vals) < 2:
                raise _InputError("任意填两个功率（Pi/Pt/Pr），回车算第三个")
            # 三个都填时：回车所在的框为重算对象；否则补算缺失的那个
            target = src if len(vals) == 3 else next(
                k for k in ("pt", "pt2", "pr") if k not in vals)

            def dbm_sum(a, b):
                return 10.0 * math.log10(10.0 ** (a / 10.0) + 10.0 ** (b / 10.0))

            def dbm_diff(a, b):
                return 10.0 * math.log10(10.0 ** (a / 10.0) - 10.0 ** (b / 10.0))
            if target == "pt":                 # Pi = Pt + Pr（功率域求和）
                v = dbm_sum(vals["pt2"], vals["pr"])
            elif target == "pt2":              # Pt = Pi − Pr，要求 Pr < Pi
                if vals["pr"] >= vals["pt"]:
                    raise _InputError("反射/透射功率必须小于入射功率")
                v = dbm_diff(vals["pt"], vals["pr"])
            else:                              # Pr = Pi − Pt，要求 Pt < Pi
                if vals["pt2"] >= vals["pt"]:
                    raise _InputError("反射/透射功率必须小于入射功率")
                v = dbm_diff(vals["pt"], vals["pt2"])
            boxes[target].setText(_g(v - off))  # dBm → 当前显示单位
            return "Pi=%s Pt=%s Pr=%s" % tuple(
                _g(boxes[k].text()) for k in ("pt", "pt2", "pr"))
        return _fn

    for obj, src in (("refl.pt", "pt"), ("refl.pr", "pr"), ("refl.pt2", "pt2")):
        le = page.findChild(QtWidgets.QLineEdit, obj)
        if le is not None:
            le.editingFinished.connect(_run_capture(page, pw_recalc(src)))
    return page


def _run_capture(page, fn):
    return lambda: _run_calc(page, fn)


# ---------------------------------------------------------------------------
# A 变体：混频器杂散
# ---------------------------------------------------------------------------

class _DgMixer(_Diagram):
    NAME = "spurs"
    LW = 580.0  # IF 频率框在 x489..562（spec 23），默认 440 会裁掉

    def draw(self, p):
        INK = "#1F1A17"
        # RF 混频环：圆心 (293,137) r23，环 2px 白底 + 内部对角 X（端点 ±r/√2）
        p.setBrush(QtGui.QColor("#FFFFFF"))
        _pen(p, INK, 2.0)
        p.drawEllipse(QtCore.QRectF(270, 114, 46, 46))
        d = 23.0 / math.sqrt(2.0)
        cx, cy = 293.0, 137.0
        _line(p, cx - d, cy - d, cx + d, cy + d, INK, 2.0)
        _line(p, cx + d, cy - d, cx - d, cy + d, INK, 2.0)
        # 连线：RF→环、环→IF、环→LO
        _line(p, 240, 137, 270, 137, INK, 2.0)
        _line(p, 316, 137, 370, 137, INK, 2.0)
        _line(p, 293, 160, 293, 190, INK, 2.0)
        self.label(227, 131, "RF")
        self.label(360, 131, "IF")
        # LO 本振圆：圆心 (293,213) r23 白底 + 正弦曲线
        p.setBrush(QtGui.QColor("#FFFFFF"))
        _pen(p, INK, 2.0)
        p.drawEllipse(QtCore.QRectF(270, 190, 46, 46))
        p.setFont(QtGui.QFont("Times New Roman", 12))
        p.drawText(283, 224, "~")
        p.setFont(QtGui.QFont(self.font()))
        self.label(328, 217, "LO")
        # 输入标签（黑体 12px，右对齐至编辑框左缘 -3px，spec 23）
        p.setFont(QtGui.QFont(self.font()))
        for text, x2, yb in (("Min RF Freq =", 128, 135), ("Max RF Freq =", 128, 162),
                             ("Min IF Freq =", 486, 132), ("Max IF Freq =", 486, 158),
                             ("Min LO Freq =", 262, 272), ("Max LO Freq =", 262, 300)):
            p.drawText(QtCore.QRectF(x2 - 110, yb - 15, 110, 17),
                       int(Qt.AlignRight) | int(Qt.AlignVCenter), text)


def _mk_spurs(win):
    dg = _DgMixer()
    # 频率输入框上画布（spec 23 原位坐标；objectName 保持 spurs.* 不变）
    dg.box("rfmin", 131, 119, "600", w=73, h=21, obj="spurs.rfmin")
    dg.box("rfmax", 131, 146, "1800", w=73, h=21, obj="spurs.rfmax")
    dg.box("ifmin", 489, 116, "200", w=73, h=21, obj="spurs.ifmin")
    dg.box("ifmax", 489, 142, "600", w=73, h=21, obj="spurs.ifmax")
    dg.box("lomin", 265, 257, "900", w=73, h=21, obj="spurs.lomin")
    dg.box("lomax", 265, 285, "1300", w=73, h=21, obj="spurs.lomax")
    dg.finalize()

    def mids(page):
        # 频率框已在图上；这里补 m/n 谐波次数、说明与配置组
        page.rfmin = dg.fields["rfmin"]
        page.rfmax = dg.fields["rfmax"]
        page.ifmin = dg.fields["ifmin"]
        page.ifmax = dg.fields["ifmax"]
        page.lomin = dg.fields["lomin"]
        page.lomax = dg.fields["lomax"]
        ms = QtWidgets.QSpinBox()
        ms.setObjectName("spurs.m")
        ms.setRange(1, 9)
        ms.setValue(3)
        ns = QtWidgets.QSpinBox()
        ns.setObjectName("spurs.n")
        ns.setRange(1, 9)
        ns.setValue(3)
        page.spin_m, page.spin_n = ms, ns
        rows = [
            _mid_row([ms], label="RF Harmonic (m) =", lw=120),
            _mid_row([ns], label="LO Harmonic (n) =", lw=120),
            _mid_row([_ql("Note: Frequency units consistent (MHz).")]),
        ]
        gb, l = _grp("System Configuration")
        rb1 = QtWidgets.QRadioButton("Variable RF - Variable LO")
        rb1.setObjectName("spurs.cfg.var")
        rb1.setChecked(True)
        rb2 = QtWidgets.QRadioButton("Variable RF - Fixed LO")
        rb2.setObjectName("spurs.cfg.fixed")

        def fixed(idx):
            page.lomax.setEnabled(idx == 1)
            if idx == 1:
                page.lomax.setText(page.lomin.text())

        page._cfg_group = QtWidgets.QButtonGroup(page)
        page._cfg_group.addButton(rb1)
        page._cfg_group.addButton(rb2)
        rb1.toggled.connect(lambda on: fixed(1 if on else 0))
        l.addWidget(rb1)
        l.addWidget(rb2)
        rows.append(gb)
        return rows

    def buttons(page):
        return [_calc_btn(page, "Calculate [F4]", lambda: _spurs_calc(page),
                          "spurs.calc")]

    def rows(page_):
        return []

    page = _a_shell(win, "spurs", "混频器杂散 Mixer Spur Finder", dg, mids, buttons, rows)

    # 结果表：右下区域
    t = QtWidgets.QTableWidget(0, 5)
    t.setObjectName("spurs.table")
    t.setHorizontalHeaderLabels(["m", "n", "阶数", "杂散频率 (MHz)", "带内"])
    t.horizontalHeader().setVisible(True)
    t.verticalHeader().setVisible(False)
    t.setMinimumWidth(360)
    t.setStyleSheet("QTableWidget { gridline-color: %s; background: #FFFFFF; }"
                    % GRIDLINE)
    page.table = t
    body = page.layout().itemAt(1).layout()
    rcol = body.itemAt(2).layout()
    # audit：Calculate [F4] 下移到 RF/IF 字段组之间（原图 (616,139)），顶部留空
    rcol.insertSpacing(0, 60)
    rcol.addWidget(_ql("杂散表（带内优先；引擎口径：f_sp=|m·fRF±n·fLO|，"
                       "带内判据 |f_sp−f_IF|≤BW/2；不含电平估计）"), 0, Qt.AlignLeft)
    rcol.addWidget(t, 1)
    return page


def _spurs_calc(page):
    e = _e()
    rmin = _f(page.rfmin, "Min RF")
    rmax = _f(page.rfmax, "Max RF")
    if rmin > rmax:
        rmin, rmax = rmax, rmin
    lomin = _f(page.lomin, "Min LO")
    fixed = page.findChild(QtWidgets.QRadioButton, "spurs.cfg.fixed")
    if fixed is not None and fixed.isChecked():
        lomax = lomin
        page.lomax.setText(_g(lomin))
    else:
        lomax = _f(page.lomax, "Max LO")
        if lomin > lomax:
            lomin, lomax = lomax, lomin
    ifmin = _f(page.ifmin, "Min IF")
    ifmax = _f(page.ifmax, "Max IF")
    if ifmin > ifmax:
        ifmin, ifmax = ifmax, ifmin
    f_if = (ifmin + ifmax) / 2.0 * 1e6
    bw = max(ifmax - ifmin, 0.0) * 1e6
    rfs = sorted({rmin, rmax})
    los = sorted({lomin, lomax})
    m_max = int(page.spin_m.value())
    n_max = int(page.spin_n.value())
    merged, seen = [], set()
    for frf in rfs:
        for flo in los:
            r = e.mixer_spurs(frf * 1e6, flo * 1e6, f_if, bw, m_max, n_max)
            if not r.get("ok"):
                raise _InputError(r.get("error", "引擎返回失败"))
            for row in r.get("rows", []):
                key = (row["m"], row["n"], round(row["f_spur"], 3))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(row)
    merged.sort(key=lambda r_: (not r_["inband"], r_["f_spur"], r_["order"]))
    t = page.table
    cap = min(len(merged), 300)
    t.setRowCount(cap)
    for i, row in enumerate(merged[:cap]):
        vals = (str(row["m"]), str(row["n"]), str(row["order"]),
                _fmt2(row["f_spur"] / 1e6), "✓" if row["inband"] else "")
        for c, v in enumerate(vals):
            it = QtWidgets.QTableWidgetItem(v)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if row["inband"]:
                it.setBackground(QtGui.QColor(YEL_HL))
            t.setItem(i, c, it)
    n_in = sum(1 for r_ in merged if r_["inband"])
    return "共 %d 个杂散点，带内 %d 个（显示前 %d 行）" % (len(merged), n_in, cap)


# ---------------------------------------------------------------------------
# A 变体：互调计算器（Pout-Pin 自绘图）
# ---------------------------------------------------------------------------

class _IMGraph(QtWidgets.QWidget):
    # spec 24：Pout-Pin 图固定逻辑画布，直接用原图像素坐标（原点平移到 (485,70)）
    OX = 485.0
    OY = 70.0
    PX_PER_DB = 188.0 / 60.0  # 截点(688,117)向下每 dB 的像素数（按 spec 曲线标定）

    def __init__(self):
        super().__init__()
        self.setObjectName("im.graph")
        self.setMinimumSize(265, 435)
        self._sig = None
        self._oip3 = None
        self._im = None

    def set_data(self, sig_out, oip3_out, im_out):
        self._sig, self._oip3, self._im = sig_out, oip3_out, im_out
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.translate(-self.OX, -self.OY)
        INK = "#1F1A17"
        _pen(p, INK, 2.0)
        # Y 轴 x500 y87..497 + 顶端箭头；轴标 Pout
        _line(p, 500, 497, 500, 87, INK, 2.0)
        _poly(p, [(500, 88), (494, 96), (506, 96)], INK)
        f = QtGui.QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.drawText(490, 86, "Pout")
        # X 轴 y353 x500..712 + 右端箭头；轴标 Pin
        _line(p, 500, 353, 712, 353, INK, 2.0)
        _poly(p, [(713, 353), (705, 349), (705, 357)], INK)
        p.drawText(725, 359, "Pin")
        p.setFont(QtGui.QFont(self.font()))
        # 基波 slope=1：(500,308)→截点(688,117)，越过截点延伸至 (694,111)
        _line(p, 500, 308, 694, 111, INK, 2.0)
        # 三阶 slope=3：(528,495)→截点(688,117)，延伸至 (694,103)
        _line(p, 528, 495, 694, 103, INK, 2.0)
        p.drawText(537, 195, "Fundamental")
        p.drawText(538, 208, "(slope = 1)")
        p.drawText(655, 230, "3rd Order")
        p.drawText(652, 243, "(slope = 3)")
        # 截点：红虚线 y=117（x502..686）+ 实心黑圆点 + 标注
        q = QtGui.QPen(QtGui.QColor("#FF0000"))
        q.setWidthF(1.0)
        q.setDashPattern([4, 4])
        p.setPen(q)
        p.drawLine(QtCore.QLineF(502, 117, 686, 117))
        _pen(p)
        p.setBrush(QtGui.QColor("#000000"))
        p.drawEllipse(QtCore.QPointF(690, 118), 3.5, 3.5)
        p.drawText(705, 108, "Intercept")
        p.drawText(713, 121, "Point")
        # P1dB：基波线上实心点 + 标注 + 黑虚线弧（弯向三阶线）
        p.drawEllipse(QtCore.QPointF(647, 162), 3.5, 3.5)
        p.drawText(610, 167, "P1dB")
        qa = QtGui.QPen(QtGui.QColor("#000000"))
        qa.setWidthF(1.0)
        qa.setDashPattern([4, 3])
        p.setPen(qa)
        p.setBrush(Qt.NoBrush)
        arc_path = QtGui.QPainterPath()
        arc_path.moveTo(632, 177)
        arc_path.quadTo(650, 172, 670, 167)
        p.strokePath(arc_path, p.pen())
        # 动态 Sig/IM 蓝虚线（set_data 的输出基准 dBm → 像素 y，锚定截点 y=117）
        oip3 = self._oip3 if self._oip3 is not None else 20.0
        qb = QtGui.QPen(QtGui.QColor("#0000FF"))
        qb.setWidthF(1.0)
        qb.setDashPattern([4, 4])
        p.setPen(qb)

        def py(dbm):
            # P=oip3 → 截点 y=117；向下每 dB 3.13px；钳在绘图区内
            return 117.0 + (oip3 - dbm) * self.PX_PER_DB

        if self._sig is not None:
            sy = min(max(py(self._sig), 95.0), 495.0)
            xs = 566.0
            p.drawLine(QtCore.QLineF(502, sy, 578, sy))
            p.drawLine(QtCore.QLineF(xs, sy - 9, xs, sy + 9))
        if self._im is not None:
            iy = min(max(py(self._im), 95.0), 495.0)
            xi = 565.0
            p.drawLine(QtCore.QLineF(502, iy, xi, iy))
            p.drawLine(QtCore.QLineF(xi, iy - 9, xi, iy + 9))
        p.end()


class _AmpGlyph(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("im.ampglyph")
        self.setFixedSize(120, 60)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        # audit blocker 4：浅青矩形放大器（#DAECEE 黑边）+ 进出箭头线
        x0, x1 = w * 0.3, w * 0.82
        y0, y1 = h * 0.16, h * 0.84
        _line(p, 4, h / 2, x0 - 4, h / 2)
        _head(p, x0, h / 2, 1.0, 0.0)
        p.setBrush(QtGui.QColor("#DAECEE"))
        _pen(p, "#000000", 1.4)
        p.drawRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
        _line(p, x1, h / 2, w - 10, h / 2)
        _head(p, w - 4, h / 2, 1.0, 0.0)
        p.end()


def _mk_intermod(win):
    page = QtWidgets.QWidget()
    page.setObjectName("intermodpg")
    page.setStyleSheet(_page_css("intermod", A_BG))
    page.name = "intermod"
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None
    page.err = QtWidgets.QLabel("")
    page.err.setObjectName("intermod.error")
    page.err.setStyleSheet("color:%s;font-weight:bold;" % _ERR)
    page.err.hide()
    win._pages["intermod"] = page
    win._order.append("intermod")
    win._stack.addWidget(page)

    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 2)
    v.setSpacing(4)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 22)
    tf.setBold(True)
    lab = QtWidgets.QLabel("互调计算器 Intermodulation Calculator")
    lab.setFont(tf)
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName("intermod.mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(lab)
    top.addStretch(1)
    top.addWidget(mm, 0, Qt.AlignTop)
    v.addLayout(top)

    body = QtWidgets.QHBoxLayout()
    left = QtWidgets.QVBoxLayout()
    # Sig1 → 放大器 → OIP3/Gain/IM1
    row1 = QtWidgets.QWidget()
    h1 = QtWidgets.QHBoxLayout(row1)
    h1.setContentsMargins(0, 0, 0, 0)
    h1.setSpacing(6)
    page.sig_box = _num_box("intermod.sig", "-30")
    h1.addWidget(_ql("Sig 1 =", align_right=True, w=46))
    h1.addWidget(page.sig_box)
    h1.addWidget(_ql("dBm"))
    h1.addWidget(_AmpGlyph())
    col2 = QtWidgets.QVBoxLayout()
    page.oip3_box = _num_box("intermod.oip3", "24.1")
    page.gain_box = _num_box("intermod.gain", "13.6")
    page.im1_box = _num_box("intermod.result.im1", "", ro=True)
    page.results["im1"] = page.im1_box
    for lab, bx, unit in (("OIP3 =", page.oip3_box, "dBm"),
                          ("Gain =", page.gain_box, "dB"),
                          ("IM 1 =", page.im1_box, "dBm")):
        rr = QtWidgets.QWidget()
        hh = QtWidgets.QHBoxLayout(rr)
        hh.setContentsMargins(0, 2, 0, 2)
        hh.addWidget(_ql(lab, align_right=True, w=46))
        hh.addWidget(bx)
        hh.addWidget(_ql(unit))
        hh.addStretch(1)
        col2.addWidget(rr)
    h1.addLayout(col2)
    h1.addStretch(1)
    left.addWidget(row1)

    def radios(title, name, labels, default_idx):
        # audit blocker 7：行内「标题 + Input/Output 单选」，不用竖排组框
        wdg = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(wdg)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(_ql(title))
        group = QtWidgets.QButtonGroup(page)
        btns = []
        for i, txt in enumerate(labels):
            rb = QtWidgets.QRadioButton(txt)
            rb.setObjectName("%s.%s" % (name, "in" if i == 0 else "out"))
            group.addButton(rb)
            rb.setChecked(i == default_idx)
            h.addWidget(rb)
            btns.append(rb)
        h.addStretch(1)
        return wdg, btns

    gb1, b1 = radios("1. IP3 基准", "intermod.ip3ref", ["Input", "Output"], 1)
    gb2, b2 = radios("2. 信号基准", "intermod.sigref", ["Input", "Output"], 0)
    gb3, b3 = radios("3. IM 基准", "intermod.imref", ["Input", "Output"], 1)
    left.addWidget(gb1)
    left.addWidget(gb2)
    left.addWidget(gb3)
    left.addWidget(_ql("Note: Sig 为每个单音的电平。"))
    gb4, b4 = radios("5. Calculate", "intermod.mode",
                     ["IP3 —— 给定 Psig 与 IM", "IM —— 给定 Psig 与 IP3",
                      "Psig —— 给定 IP3 与 IM"], 1)
    b4[0].setObjectName("intermod.calc.ip3")
    b4[1].setObjectName("intermod.mode.im")
    b4[2].setObjectName("intermod.mode.psig")
    left.addWidget(gb4)
    cb = QtWidgets.QPushButton("Calculate [F4]")
    cb.setObjectName("intermod.calc")
    left.addWidget(cb)
    left.addStretch(1)

    right = QtWidgets.QVBoxLayout()
    # audit blocker 4：图侧输入行（IP3/Sig/IM dBm + IM dBc），与主输入框联动
    side = QtWidgets.QWidget()
    sg = QtWidgets.QGridLayout(side)
    sg.setContentsMargins(0, 0, 0, 0)
    sg.setHorizontalSpacing(6)
    sg.setVerticalSpacing(3)

    def _sbox(obj, val=""):
        bx = _num_box(obj, val)
        return bx

    page.gip3_box = _sbox("intermod.gip3")
    page.gsig_box = _sbox("intermod.gsig")
    page.gim_box = _sbox("intermod.gim")
    page.gimdbc_box = _sbox("intermod.gimdbc")
    for col, lab in enumerate(("IP3 =", "Sig =", "IM =", "IM (dBc) =")):
        sg.addWidget(_ql(lab, align_right=True), 0, col * 2)
    sg.addWidget(page.gip3_box, 0, 1)
    sg.addWidget(_ql("dBm"), 0, 2)
    sg.addWidget(page.gsig_box, 0, 3)
    sg.addWidget(_ql("dBm"), 0, 4)
    sg.addWidget(page.gim_box, 0, 5)
    sg.addWidget(_ql("dBm"), 0, 6)
    sg.addWidget(page.gimdbc_box, 0, 7)
    sg.addWidget(_ql("dBc"), 0, 8)
    right.addWidget(side)
    graph = _IMGraph()
    page.graph = graph
    right.addWidget(graph, 1)
    gb5, b5 = radios("Graph Reference", "intermod.gref", ["Input", "Output"], 1)
    # audit blocker 7：Graph Reference 小组框叠放在图右下（原图 (638,291)-(740,332)）
    gb5.setParent(graph)
    gb5.setGeometry(150, 215, 155, 44)
    gb5.setStyleSheet("QGroupBox { background: #FFFFFF; border: 1px solid #A0A0A0;"
                      " margin-top: 8px; }")
    right.addStretch(1)
    body.addLayout(left, 0)
    body.addLayout(right, 1)
    v.addLayout(body)
    err_row = QtWidgets.QHBoxLayout()
    err_row.addWidget(page.err)
    err_row.addStretch(1)
    v.addLayout(err_row)

    def _is_out(btns):
        return btns[1].isChecked()

    def calc():
        e = _e()
        sig = _f(page.sig_box, "Sig")
        oip3 = _f(page.oip3_box, "OIP3")
        gain = _f(page.gain_box, "Gain")
        if gain <= 0:
            raise _InputError("Gain 必须大于 0")
        sig_out = sig + gain if not _is_out(b2) else sig
        oip3_out = oip3 + gain if not _is_out(b1) else oip3
        mode = 0 if b4[0].isChecked() else (1 if b4[1].isChecked() else 2)
        note = ""
        if mode == 0:
            # 引擎口径：P_IM3 = 3·P_out − 2·OIP3_out（用引擎 im_products 求值）
            r = e.im_products(1000.0, 1001.0, sig_out, None, oip3_out)
            if not r.get("ok"):
                raise _InputError(r.get("error", "引擎返回失败"))
            im_out = None
            for row in r.get("rows", []):
                if row.get("name") == "2f1−f2":
                    im_out = row.get("p_dbm")
            im_out = im_out if im_out is not None else (3 * sig_out - 2 * oip3_out)
        elif mode == 1:
            im_txt = page.im1_box.text().strip()
            im_out = float(im_txt) if im_txt else None
            if im_out is None:
                # IM 未填：退化为「由 Psig 与 IP3 计算 IM」（引擎口径），保持可算
                r = e.im_products(1000.0, 1001.0, sig_out, None, oip3_out)
                if not r.get("ok"):
                    raise _InputError(r.get("error", "引擎返回失败"))
                im_out = None
                for row in r.get("rows", []):
                    if row.get("name") == "2f1−f2":
                        im_out = row.get("p_dbm")
                im_out = (im_out if im_out is not None
                          else (3.0 * sig_out - 2.0 * oip3_out))
                note = "IM 未填，退化为由 IP3/Psig 计算 IM"
            else:
                oip3_out = (3.0 * sig_out - im_out) / 2.0
                oip3 = oip3_out - gain if not _is_out(b1) else oip3_out
                page.oip3_box.setText(_g(oip3))
        else:
            im_out = _f(page.im1_box, "IM") if page.im1_box.text().strip() else None
            if im_out is None:
                raise _InputError("Psig 模式需先填 IM 1（dBm）")
            sig_out = (2.0 * oip3_out + im_out) / 3.0
            sig = sig_out - gain if not _is_out(b2) else sig_out
            page.sig_box.setText(_g(sig))
        im_show = im_out - gain if (not _is_out(b3) and im_out is not None) else im_out
        page.im1_box.setText(_g(im_show))
        page.gim_box.setText(_g(im_show) if im_show is not None else "")
        page.gimdbc_box.setText(_g(im_show - sig_out)
                                if im_show is not None else "")
        graph.set_data(sig_out, oip3_out, im_out)
        return "IM1=%s dBm（%s 基准）%s" % (
            _g(im_show), "输入" if not _is_out(b3) else "输出", note)

    cb.clicked.connect(lambda: _run_calc(page, calc))

    def side_sync():
        # 图侧输入行 → 主输入框（IP3/Sig/IM 镜像），IM(dBc) → dBm 换算
        page.oip3_box.setText(page.gip3_box.text())
        page.sig_box.setText(page.gsig_box.text())
        dbc = page.gimdbc_box.text().strip()
        if dbc:
            try:
                gain = _fopt(page.gain_box, 0.0, "Gain") or 0.0
                sig = _fopt(page.sig_box, 0.0, "Sig")
                sig_out = sig + gain if not _is_out(b2) else sig
                if page.gim_box.text().strip() == "":
                    page.im1_box.setText(_g(sig_out + float(dbc)))
            except ValueError:
                pass
        else:
            page.im1_box.setText(page.gim_box.text())
        gref_redraw()

    for _bx in (page.gip3_box, page.gsig_box, page.gim_box, page.gimdbc_box):
        _bx.editingFinished.connect(side_sync)

    def gref_redraw():
        sig = _fopt(page.sig_box, None, "Sig")
        oip3 = _fopt(page.oip3_box, None, "OIP3")
        gain = _fopt(page.gain_box, 0.0, "Gain")
        if sig is not None and oip3 is not None:
            sig_out = sig + gain if not _is_out(b2) else sig
            oip3_out = oip3 + gain if not _is_out(b1) else oip3
            im_txt = page.im1_box.text().strip()
            im_out = float(im_txt) + gain if im_txt and not _is_out(b3) else \
                (float(im_txt) if im_txt else None)
            graph.set_data(sig_out, oip3_out, im_out)

    for le in (page.sig_box, page.oip3_box, page.gain_box, page.im1_box):
        le.editingFinished.connect(gref_redraw)
    return page


# ---------------------------------------------------------------------------
# 工具页：基片库（A 版黄底化）
# ---------------------------------------------------------------------------

def _mk_substrate(win):
    page = QtWidgets.QWidget()
    page.setObjectName("substratepg")
    page.setStyleSheet(_page_css("substrate", A_BG))
    page.name = "substrate"
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None
    page.err = QtWidgets.QLabel("")
    page.err.setObjectName("substrate.error")
    page.err.hide()
    win._pages["substrate"] = page
    win._order.append("substrate")
    win._stack.addWidget(page)
    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 2)
    v.setSpacing(6)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 22)
    tf.setBold(True)
    lab = QtWidgets.QLabel("基片库 Substrate Library")
    lab.setFont(tf)
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName("substrate.mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(lab)
    top.addStretch(1)
    top.addWidget(mm, 0, Qt.AlignTop)
    v.addLayout(top)
    e = _E()
    lst = QtWidgets.QListWidget()
    lst.setObjectName("substrate.list")
    if e:
        for s in e.SUBSTRATES:
            lst.addItem("%s — εr=%g, tanδ=%g" % (s["name"], s["er"], s["tand"]))
    else:
        lst.addItem("（引擎未加载，无法列出基片）")
    v.addWidget(lst, 1)
    note = _ql("选中基片后点击「应用到当前页」：把 εr 回填到当前计算页的 "
               "Dielectric εr 框（tanδ 本版黄底页未建模，仅记录）。")
    note.setWordWrap(True)
    v.addWidget(note)
    hb = QtWidgets.QHBoxLayout()
    b = QtWidgets.QPushButton("应用到当前页")
    b.setObjectName("substrate.apply")

    def apply_sub():
        idx = lst.currentRow()
        if idx < 0 or not e:
            raise _InputError("请先在列表中选择一个基片")
        sub = e.SUBSTRATES[idx]
        win._apply_substrate(sub)

    b.clicked.connect(lambda: _run_calc(page, apply_sub))
    hb.addWidget(b)
    hb.addStretch(1)
    v.addLayout(hb)
    v.addWidget(page.err)
    return page


# ---------------------------------------------------------------------------
# Reliability / 工程工具：thermal、comp_values、constants、complex_math、spcs
# （spec_misc v3；clean-room 复刻，引擎只调用 backend/rfcalc.py 既有函数）
# ---------------------------------------------------------------------------

class _FocusSpy(QtCore.QObject):
    """QLineEdit 聚焦监视：FocusIn 时回调框上挂的 on_focus 属性。"""

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.FocusIn:
            cb = obj.property("on_focus")
            if cb is not None:
                cb()
        return False


class _DgThermal(_Diagram):
    """左侧 3D 封装插画 + 右侧五层热阻剖面（spec_misc §2.5/§2.6，非交互示意）。"""
    NAME = "thermal"
    LW = 580.0
    LH = 360.0

    def draw(self, p):
        # ---- 左：3D 封装插画（AC 源 → IC → 负载电阻 → 地） ----
        p.setBrush(Qt.NoBrush)
        _pen(p)
        p.drawEllipse(QtCore.QRectF(66, 228, 30, 30))
        f = QtGui.QFont("Times New Roman", 13)
        f.setItalic(True)
        p.setFont(f)
        p.drawText(74, 249, "~")
        _line(p, 66, 262, 96, 262)
        _line(p, 72, 268, 90, 268)
        _line(p, 77, 274, 85, 274)
        _line(p, 96, 243, 122, 243)                       # 源 → 封装左引脚
        # 等距 IC 封装（顶/左/右三面）
        _poly(p, [(122, 230), (196, 206), (244, 222), (170, 247)], "#3D3D3D")
        _poly(p, [(122, 230), (170, 247), (170, 297), (122, 279)], "#8A8A8A")
        _poly(p, [(170, 247), (244, 222), (244, 271), (170, 297)], "#5A5A5A")
        for i in range(4):                                # 左右引脚
            y0 = 252 + i * 12
            _line(p, 122, y0 + 8, 108, y0 + 12)
            _line(p, 244 - i * 4, 231 + i * 12, 258 - i * 4, 235 + i * 12)
        # 气流虚线箭头（封装右上与下方）
        for (x1, y1, x2, y2) in ((150, 190, 172, 176), (206, 196, 228, 182),
                                 (150, 310, 172, 322), (206, 306, 228, 318)):
            _line(p, x1, y1, x2, y2, "#808080", 1.0, dash=[3, 2])
            _head(p, x2 + 3, y2 + (3 if y2 > y1 else -3), 0.7,
                  (1 if y2 > y1 else -1) * 0.5, "#808080")
        # 负载电阻 zigzag + 地
        _line(p, 244, 268, 262, 268)
        _zig_v(p, 262, 272, 300, 3.5)
        _line(p, 262, 304, 262, 312)
        _line(p, 254, 312, 270, 312)
        _line(p, 257, 317, 267, 317)
        # ---- 右：五层热阻剖面 ----
        _line(p, 430, 135, 468, 135, "#404040", 1.0, dash=[2, 2])
        _head(p, 474, 135, 1.0, 0.0, "#404040")
        # 行标签（右对齐 + →）
        rows = (("Semiconductor", 196), ("Lead frame", 214),
                ("Package material", 244), ("PCB", 292), ("Heatsink", 322))
        f2 = QtGui.QFont("Sans Serif", 10)
        p.setFont(f2)
        for txt, y in rows:
            p.drawText(QtCore.QRectF(330, y - 12, 86, 16),
                       Qt.AlignRight | Qt.AlignVCenter, txt)
            _line(p, 420, y - 4, 436, y - 4)
            _head(p, 442, y - 4, 1.0, 0.0)
        # 层结构（x 448..510）
        p.setBrush(QtGui.QColor("#FF7B00"))               # Semiconductor
        _pen(p)
        p.drawRect(QtCore.QRectF(448, 190, 24, 12))
        p.setBrush(QtGui.QColor("#FE6EAA"))               # Lead frame
        p.drawRect(QtCore.QRectF(448, 202, 52, 28))
        p.setBrush(QtGui.QColor("#F5F0D8"))               # Package material 楔形
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(478, 230),
                                       QtCore.QPointF(506, 230),
                                       QtCore.QPointF(510, 262),
                                       QtCore.QPointF(470, 262)]))
        p.setBrush(QtGui.QColor("#E8D44D"))               # 黄色薄层
        p.drawRect(QtCore.QRectF(448, 258, 62, 7))
        g = _grad(p, 448, 265, 448, 300, "#3FA8A0", "#7FD6CE")   # PCB 青绿渐变
        p.setBrush(g)
        p.drawRect(QtCore.QRectF(448, 265, 62, 35))
        p.setBrush(QtGui.QColor("#9FC4E8"))               # Heatsink 蓝斜纹
        p.drawRect(QtCore.QRectF(448, 300, 62, 12))
        for x in range(448, 512, 8):
            _line(p, x, 312, x + 6, 300, "#5B8FC9", 1.0)
        # 中央热流链（zigzag + 节点圆点）
        _line(p, 476, 202, 476, 214, "#202020", 1.0)
        _zig_v(p, 476, 214, 238, 3.0, n=5)
        p.setBrush(QtGui.QColor("#000000"))
        p.drawEllipse(QtCore.QRectF(474, 238, 5, 5))
        _line(p, 476, 243, 476, 258, "#202020", 1.0)
        _line(p, 476, 258, 500, 282, "#202020", 1.0)      # 一支进 PCB
        p.drawEllipse(QtCore.QRectF(498, 280, 5, 5))
        _zig_v(p, 476, 258, 296, 3.0, n=4)
        _line(p, 476, 296, 476, 306, "#202020", 1.0)
        self.label(330, 340, "θjc ↑        θca ↓")


def _mk_thermal(win):
    page = _b_shell(win, "thermal", "器件热设计 Device Thermal Calculator")
    dg = _DgThermal()
    page._left.addWidget(dg)
    # 左列输入（spec §2.2）
    grid = QtWidgets.QGridLayout()
    grid.setHorizontalSpacing(6)
    grid.setVerticalSpacing(5)
    for r_, (lab, key, val, unit) in enumerate((
            ("Vin =", "vin", "3", "V"), ("Iin =", "iin", "42", "mA"))):
        grid.addWidget(_ql(lab, align_right=True), r_, 0)
        dg.box(key, 0, 0, val, w=52, h=18, obj="thermal." + key)
        grid.addWidget(dg.fields[key], r_, 1)
        grid.addWidget(_ql(unit), r_, 2)
    page._left.addLayout(grid)
    row = QtWidgets.QHBoxLayout()
    row.addWidget(_ql("Pin =", align_right=True))
    dg.box("pin", 0, 0, "2.1", w=52, h=18, obj="thermal.pin")
    row.addWidget(dg.fields["pin"])
    row.addWidget(_ql("dBm"))
    row.addSpacing(18)
    row.addWidget(_ql("Pout =", align_right=True))
    dg.box("pout", 0, 0, "14.2", w=52, h=18, obj="thermal.pout")
    row.addWidget(dg.fields["pout"])
    row.addWidget(_ql("dBm"))
    row.addStretch(1)
    page._left.addLayout(row)
    page._left.addSpacing(8)
    page._left.addWidget(_instructions([
        "1. Input supply voltage and current.",
        "2. Input RF input and output power.",
        "3. Input thermal resistance for device (and PCB, optional).",
        "4. Input either of the three temperatures.",
        "5. Press Enter (with cursor in temperature input field) to",
        "    calculate remaining parameters.",
        "注：在 Tcase/Tj/Ta 任一框内回车即以该框为基准解出另两个温度。"]))
    page._left.addStretch(1)

    # 右列：温度/热阻 + 结果
    spy = _FocusSpy(win)
    page._tbasis = "tcase"
    page.basis_lab = _ql("◀ 计算基准（点入温度框即切换）")
    page.basis_lab.setStyleSheet("color:#1F5FBF;font-size:8pt;")

    def set_basis(key):
        page._tbasis = key
        page.basis_lab.setText({"tcase": "◀ 基准：Tcase", "tj": "◀ 基准：Tj",
                                "ta": "◀ 基准：Ta"}[key])

    def temp_row(lab, key, val, unit):
        hh = QtWidgets.QHBoxLayout()
        hh.setSpacing(4)
        hh.addWidget(_ql(lab, align_right=True))
        bx = _num_box("thermal." + key, val)
        bx.setProperty("on_focus", lambda k=key: set_basis(k))
        bx.installEventFilter(spy)
        bx.editingFinished.connect(lambda k=key: _run_calc(page, calc))
        hh.addWidget(bx)
        hh.addWidget(_ql(unit))
        hh.addStretch(1)
        return hh, bx

    for lab, key, val, unit in (("Tcase =", "tcase", "", "°C"),
                                ("Tj =", "tj", "", "°C"),
                                ("θjc =", "tjc", "220", "°C/W"),
                                ("θca =", "tca", "22", "°C/W"),
                                ("Ta =", "ta", "", "°C")):
        hh, bx = temp_row(lab, key, val, unit)
        page._right.addLayout(hh)
        dg.fields[key] = bx
    page._right.addWidget(page.basis_lab)
    page._right.addSpacing(10)
    for lab, key in (("Pdc =", "pdc"), ("ΣPrf =", "prf"), ("Pdiss =", "pdiss")):
        hh = QtWidgets.QHBoxLayout()
        hh.setSpacing(4)
        lab_w = _ql(lab, align_right=True)
        if key == "pdiss":
            lab_w.setStyleSheet("color:#909090;")        # 原图 Pdiss 标签灰字
        hh.addWidget(lab_w)
        bx = _num_box("thermal." + key, "")
        bx.setReadOnly(True)
        hh.addWidget(bx)
        dg.fields[key] = bx
        hh.addWidget(_ql("mW"))
        hh.addStretch(1)
        page._right.addLayout(hh)
    note = _ql("(Total power dissipated as heat)")
    note.setStyleSheet("color:#606060;font-size:8pt;")
    page._right.addWidget(note)
    b = QtWidgets.QPushButton("Calculate [F4]")
    b.setObjectName("thermal.calc")
    page._right.addWidget(b)
    page._right.addStretch(1)
    page.dg = dg

    def calc():
        e = _e()
        vin = _f(dg.fields["vin"], "Vin")
        iin = _f(dg.fields["iin"], "Iin")
        pin = _f(dg.fields["pin"], "Pin")
        pout = _f(dg.fields["pout"], "Pout")
        tjc = _fopt(dg.fields["tjc"], 220.0, "θjc")
        tca = _fopt(dg.fields["tca"], 22.0, "θca")
        basis = page._tbasis
        box = dg.fields[basis]
        if not box.text().strip():
            raise _InputError("请先填写基准温度 %s" %
                              {"tcase": "Tcase", "tj": "Tj", "ta": "Ta"}[basis])
        tv = float(box.text())
        kw = {"t_case": None, "t_j": None, "t_ambient": None}
        kw[{"tcase": "t_case", "tj": "t_j", "ta": "t_ambient"}[basis]] = tv
        try:
            r = e.thermal_analysis(vin, iin, pin, pout, tjc, tca, **kw)
        except Exception as exc:
            raise _InputError(str(exc))
        if not r.get("ok", True):
            raise _InputError(r.get("error", "热分析失败"))
        dg.fields["tj"].setText(_fmt2(r.get("t_j")))
        dg.fields["tcase"].setText(_fmt2(r.get("t_case")))
        dg.fields["ta"].setText(_fmt2(r.get("t_ambient")))
        dg.fields["pdc"].setText(_fmt2(r.get("pdc_mw")))
        dg.fields["prf"].setText(_fmt2(r.get("prf_mw")))
        dg.fields["pdiss"].setText(_fmt2(r.get("pdiss_mw")))
        for w in r.get("warnings") or []:
            page.err.setText("⚠ %s" % w)
        return "Pdiss=%s mW  Tj=%s°C  Ta=%s°C" % (
            _fmt2(r.get("pdiss_mw")), _fmt2(r.get("t_j")),
            _fmt2(r.get("t_ambient")))

    b.clicked.connect(lambda: _run_calc(page, calc))
    page.dg = dg
    return page


# --- 标准值计算器 ------------------------------------------------------------

def _cmv_icon(kind):
    """R/C/L 蓝色图标（spec §3.2：#004DC8 线条，面 #BEBEBE）。"""
    pm = QtGui.QPixmap(64, 32)
    pm.fill(QtGui.QColor("#BEBEBE"))
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    pen = _pen(p, "#004DC8", 2.0)
    if kind == "r":
        pts = [(8, 16)]
        x = 8
        for i in range(5):
            x += 4.5
            pts.append((x, 16 - (9 if i % 2 == 0 else -9)))
        pts += [(x + 4.5, 16), (56, 16)]
        for i in range(len(pts) - 1):
            p.drawLine(QtCore.QPointF(*pts[i]), QtCore.QPointF(*pts[i + 1]))
    elif kind == "c":
        _line(p, 10, 16, 28, 16, "#004DC8", 2.0)
        _line(p, 28, 5, 28, 27, "#004DC8", 2.0)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QtCore.QRectF(34, 5, 5, 22), 90 * 16, -180 * 16)
        _line(p, 39, 16, 54, 16, "#004DC8", 2.0)
    else:
        _line(p, 6, 16, 14, 16, "#004DC8", 2.0)
        for i in range(4):
            p.drawArc(QtCore.QRectF(14 + i * 9, 8, 9, 16),
                      0, -180 * 16)
        _line(p, 50, 16, 58, 16, "#004DC8", 2.0)
    p.end()
    return QtGui.QIcon(pm)


def _mk_comp_values(win):
    page = _b_shell(win, "comp_values", "标准值计算器 Standard Value Calculator")
    left = page._left
    left.addWidget(_ql("1. Enter raw value in any numeric format, e.g.:"))
    row = QtWidgets.QHBoxLayout()
    row.addWidget(_ql("Raw value =", align_right=True))
    raw = _num_box("comp_values.raw", "612E-5")
    raw.setMinimumWidth(170)
    row.addWidget(raw)
    row.addStretch(1)
    left.addLayout(row)
    ex = _ql("0.00612   .00612   |  6.1234e-3  612E-5  |  41543  41543.21  |"
             "  4.2e4  415E2")
    ex.setStyleSheet("color:#909090;font-size:8pt;")
    left.addWidget(ex)
    left.addSpacing(6)
    left.addWidget(_ql("2. Select component tolerance:"))
    gb, l = _grp("Tolerance")
    grp = QtWidgets.QButtonGroup(page)
    grp.setObjectName("comp_values.tol")
    tol_row = QtWidgets.QHBoxLayout()
    tol_row.setSpacing(14)
    tol_btns = {}
    for i, t in enumerate(("0.1%", "1%", "5%", "10%", "20%")):
        rb = QtWidgets.QRadioButton(t)
        rb.setObjectName("comp_values.tol.b%s" % t.replace(".", "").replace("%", ""))
        grp.addButton(rb)
        rb.setChecked(t == "5%")
        tol_row.addWidget(rb)
        tol_btns[t] = rb
    l.addLayout(tol_row)
    left.addWidget(gb)

    def tol_pct():
        for t, rb in tol_btns.items():
            if rb.isChecked():
                return {"0.1%": 0.1, "1%": 1.0, "5%": 5.0,
                        "10%": 10.0, "20%": 20.0}[t]
        return 5.0

    left.addWidget(_ql("3. Click type of component to calculate:"))
    irow = QtWidgets.QHBoxLayout()
    for kind, nm in (("r", "btn_r"), ("c", "btn_c"), ("l", "btn_l")):
        bt = QtWidgets.QPushButton()
        bt.setObjectName("comp_values." + nm)
        bt.setIcon(_cmv_icon(kind))
        bt.setFixedSize(72, 38)
        bt.setCheckable(True)
        irow.addWidget(bt)
    irow.addStretch(1)
    left.addLayout(irow)
    b = QtWidgets.QPushButton("Calculate")
    b.setObjectName("comp_values.calc")
    left.addWidget(b)
    left.addStretch(1)

    right = page._right
    for lab, key, unit in (("Calculated Standard Value =", "std", ""),
                           ("Absolute Error =", "abs_err", ""),
                           ("Percent Error =", "pct_err", "%")):
        hh = QtWidgets.QHBoxLayout()
        hh.setSpacing(4)
        hh.addWidget(_ql(lab, align_right=True))
        bx = _num_box("comp_values.result." + key, "")
        bx.setReadOnly(True)
        page.results[key] = bx
        hh.addWidget(bx)
        if key == "abs_err":
            nb = _ql("(raw number - standard value)")
            nb.setStyleSheet("color:#909090;font-size:8pt;")
            hh.addWidget(nb)
        elif unit:
            hh.addWidget(_ql(unit))
        hh.addStretch(1)
        right.addLayout(hh)
    right.addStretch(1)

    def calc():
        e = _e()
        v = _f(raw, "Raw value")
        try:
            r = e.standard_value(v, tol_pct())
        except Exception as exc:
            raise _InputError(str(exc))
        if not r.get("ok", True):
            raise _InputError(r.get("error", "标准值计算失败"))
        page.results["std"].setText(_fmt2(r.get("std")))
        page.results["abs_err"].setText(_fmt2(r.get("abs_err")))
        page.results["pct_err"].setText(_fmt2(r.get("pct_err")))
        return "std=%s（%s 系列）" % (_fmt2(r.get("std")),
                                      r.get("series", ""))

    b.clicked.connect(lambda: _run_calc(page, calc))
    return page


# --- 工程常数 ----------------------------------------------------------------

def _mk_constants(win):
    page = _b_shell(win, "constants", "工程常数 Engineering Constants")
    left = page._left
    head = _ql("Engineering Constant")
    head.setStyleSheet("background:#C8C8C8;color:#000000;padding:2px 6px;")
    head.setFixedHeight(20)
    left.addWidget(head)
    lst = QtWidgets.QListWidget()
    lst.setObjectName("constants.list")
    lst.setMinimumHeight(240)
    lst.setStyleSheet(
        " QListWidget { background:#FFFFFF; border:1px solid #646464;"
        "   font-size:9pt; }"
        " QListWidget::item { height:20px; padding-left:6px; }"
        " QListWidget::item:selected { background:#0078D7; color:#FFFFFF; }")
    left.addWidget(lst)
    right = page._right
    row = QtWidgets.QHBoxLayout()
    row.setSpacing(6)
    val = _num_box("constants.value", "")
    val.setReadOnly(True)
    row.addWidget(val, 1)
    unit = _ql("")
    unit.setObjectName("constants.unit")
    row.addWidget(unit)
    right.addLayout(row)
    cb = QtWidgets.QPushButton("Copy constant to clipboard")
    cb.setObjectName("constants.btn_copy")
    right.addWidget(cb)
    cm_lab = _ql("Comments:")
    right.addWidget(cm_lab)
    comments = _ql("")
    comments.setObjectName("constants.comments")
    comments.setWordWrap(True)
    comments.setStyleSheet("color:#303030;font-size:9pt;")
    right.addWidget(comments)
    right.addStretch(1)

    try:
        table = dict(_e().ENGINEERING_CONSTANTS)
    except Exception as exc:      # 引擎加载失败也要能建页（不崩）
        table = {}
        comments.setText("⚠ 引擎常数表加载失败：%s" % exc)
    keys = list(table.keys())
    for i, k in enumerate(keys):
        it = QtWidgets.QListWidgetItem(k)
        it.setBackground(QtGui.QColor("#DFEEFF" if i % 2 == 0 else "#FFFFFF"))
        lst.addItem(it)

    def show_row(r_):
        if r_ < 0 or r_ >= len(keys):
            return
        v, u, c = table[keys[r_]]
        val.setText(repr(v))
        unit.setText(u or "")
        comments.setText(c or "")

    lst.currentRowChanged.connect(show_row)
    if keys:
        lst.setCurrentRow(0)
    cb.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(
        val.text()))
    return page


# --- 复数计算器（RPN 栈） -----------------------------------------------------

class _CmOps(object):
    """complex_math 页的操作实现（cmath 栈机；clean-room）。"""

    @staticmethod
    def unary(name, x):
        import cmath
        fn = {
            "sin": cmath.sin, "asin": cmath.asin, "inv": lambda z: 1 / z,
            "cos": cmath.cos, "acos": cmath.acos, "sqr": cmath.sqrt,
            "tan": cmath.tan, "atan": cmath.atan,
            "sqr2": lambda z: z * z,
            "cot": lambda z: 1 / cmath.tan(z),
            "acot": lambda z: cmath.atan(1 / z),
            "abs": lambda z: complex(abs(z), 0.0),
            "conj": lambda z: z.conjugate(),
            "log10": cmath.log10, "ln": cmath.log,
            "exp_e": cmath.exp, "atanh": cmath.atanh,
            "sinh": cmath.sinh, "cosh": cmath.cosh,
            "tanh": cmath.tanh, "coth": lambda z: 1 / cmath.tanh(z),
        }[name]
        return fn(x)


def _mk_complex_math(win):
    import cmath
    import math
    page = QtWidgets.QWidget()
    page.setObjectName("complex_mathpg")
    page.setStyleSheet(_page_css("complex_math", "#DCE6F0"))
    page.name = "complex_math"
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None
    page._stack = [complex(0.25, 0.5)]
    page._undo = []
    page._regs = {}
    err = _ql("")
    err.setStyleSheet("color:%s;font-weight:bold;" % _ERR)
    err.hide()
    page.err = err
    win._pages["complex_math"] = page
    win._order.append("complex_math")
    win._stack.addWidget(page)

    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 2)
    v.setSpacing(4)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 20)
    tf.setBold(True)
    lab = QtWidgets.QLabel("复数计算器 Complex Math Calculator for RF Circuits")
    lab.setObjectName("pgtitle")
    lab.setFont(tf)
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName("complex_math.mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(lab)
    top.addStretch(1)
    top.addWidget(mm, 0, Qt.AlignTop)
    v.addLayout(top)

    body = QtWidgets.QHBoxLayout()
    body.setSpacing(10)
    v.addLayout(body, 1)

    # ---- 左：栈显示区 ----
    stack_gb = QtWidgets.QGroupBox("栈 Stack（x=1 为栈顶）")
    stack_gb.setObjectName("complex_math.stack")
    sv = QtWidgets.QGridLayout(stack_gb)
    sv.setHorizontalSpacing(6)
    sv.setVerticalSpacing(3)
    sv.addWidget(_ql("Rectangular:"), 0, 0)
    sv.addWidget(_ql("Real"), 0, 1)
    sv.addWidget(_ql("Imaginary"), 0, 3)
    re_rows, im_rows = {}, {}
    for lvl in range(7, 0, -1):
        r_ = 8 - lvl
        lab = _ql(("x 1:" if lvl == 1 else ("y 2:" if lvl == 2 else
                                            "%d:" % lvl)), align_right=True)
        f = QtGui.QFont("Times New Roman", 10)
        f.setItalic(True)
        lab.setFont(f)
        sv.addWidget(lab, r_, 0)
        re_bx = _num_box("cm_re%d" % lvl, "")
        re_bx.setAlignment(Qt.AlignRight)
        im_bx = _num_box("cm_im%d" % lvl, "")
        im_bx.setAlignment(Qt.AlignRight)
        if lvl == 1:
            re_bx.setText("0.250")
            im_bx.setText("0.500")
        else:
            re_bx.setPlaceholderText("0.000")
            im_bx.setPlaceholderText("0.000")
        sv.addWidget(re_bx, r_, 1)
        sv.addWidget(_ql("+ J"), r_, 2)
        sv.addWidget(im_bx, r_, 3)
        re_rows[lvl] = re_bx
        im_rows[lvl] = im_bx
    stack_gb.setMaximumWidth(620)
    body.addWidget(stack_gb, 0)

    # ---- 右上：参数区 ----
    par = QtWidgets.QGridLayout()
    par.setHorizontalSpacing(6)
    par.setVerticalSpacing(4)
    par.addWidget(_ql("Zo ="), 0, 0)
    z0 = _num_box("cm_z0", "50.0")
    par.addWidget(z0, 0, 1)
    par.addWidget(_ql("Ω"), 0, 2)
    par.addWidget(_ql("Yo ="), 0, 3)
    y0 = _num_box("cm_y0", "0.020")
    y0.setReadOnly(True)
    y0.setStyleSheet("color:#909090;")
    par.addWidget(y0, 0, 4)
    par.addWidget(_ql("S"), 0, 5)
    par.addWidget(_ql("Freq ="), 1, 0)
    fq = _num_box("cm_freq", "1000")
    par.addWidget(fq, 1, 1)
    unit = QtWidgets.QComboBox()
    unit.setObjectName("cm_unit")
    unit.addItems(["MHz", "GHz", "kHz", "Hz"])
    par.addWidget(unit, 1, 2, 1, 2)
    par.addWidget(_ql("Fix"), 1, 4)
    fix = QtWidgets.QSpinBox()
    fix.setObjectName("cm_fix")
    fix.setRange(0, 9)
    fix.setValue(3)
    par.addWidget(fix, 1, 5)
    par_w = QtWidgets.QWidget()
    par_w.setLayout(par)
    body.addWidget(par_w)

    # ---- 栈机核心 ----
    def fmt(z):
        return ("%.*f" % (fix.value(), z.real),
                "%.*f" % (fix.value(), z.imag))

    def refresh():
        for lvl in range(1, 8):
            if len(page._stack) >= lvl:
                z = page._stack[lvl - 1]
                re_t, im_t = fmt(z)
                re_rows[lvl].setText(re_t)
                im_rows[lvl].setText(im_t)
            else:
                re_rows[lvl].setText("")
                im_rows[lvl].setText("")

    def note(msg):
        win.status().showMessage(msg, 6000)

    def snap():
        page._undo.append(list(page._stack))
        del page._undo[:-8]

    def need(n):
        if len(page._stack) < n:
            raise _InputError("栈空（至少需要 %d 个元素）" % n)

    def parse_x():
        try:
            return complex(float(re_rows[1].text() or 0),
                           float(im_rows[1].text() or 0))
        except ValueError:
            raise _InputError("x 行不是合法数值")

    def zval():
        return _fopt(z0, 50.0, "Zo")

    def unary_op(name, label):
        def go():
            need(1)
            snap()
            try:
                page._stack[0] = _CmOps.unary(name, page._stack[0])
            except (ValueError, ZeroDivisionError) as exc:
                raise _InputError("%s：%s" % (label, exc))
            refresh()
            note(label)
        return go

    def binary_op(fn, label):
        def go():
            need(2)
            snap()
            y, x = page._stack[1], page._stack[0]
            try:
                res = fn(y, x)
            except (ValueError, ZeroDivisionError) as exc:
                raise _InputError("%s：%s" % (label, exc))
            page._stack[0:2] = [res]
            refresh()
            note(label)
        return go

    def push(v, label):
        snap()
        page._stack.insert(0, v)
        del page._stack[7:]
        refresh()
        note(label)

    def undo():
        if not page._undo:
            raise _InputError("无可撤销操作")
        page._stack = page._undo.pop()
        refresh()
        note("Undo")

    def clear_all():
        snap()
        page._stack = []
        refresh()

    def clear_x():
        need(1)
        snap()
        del page._stack[0]
        refresh()

    def swap():
        need(2)
        snap()
        page._stack[0], page._stack[1] = page._stack[1], page._stack[0]
        refresh()

    def drop():
        need(1)
        snap()
        del page._stack[0]
        refresh()

    def enter():
        need(1)
        snap()
        page._stack.insert(0, page._stack[0])
        del page._stack[7:]
        refresh()

    def r2p():
        need(1)
        snap()
        x = page._stack[0]
        page._stack[0] = complex(abs(x), math.degrees(cmath.phase(x)))
        refresh()

    def p2r():
        need(1)
        snap()
        x = page._stack[0]
        page._stack[0] = abs(x) * cmath.exp(1j * math.radians(x.imag))
        refresh()

    def eng_op(fn, label, keys):
        def go():
            need(1)
            snap()
            x = page._stack[0]
            try:
                r = fn(x.real, x.imag)
            except Exception as exc:
                raise _InputError("%s：%s" % (label, exc))
            if isinstance(r, dict) and r.get("ok") is False:
                raise _InputError("%s：%s" % (label, r.get("error", "失败")))
            page._stack[0] = complex(*[float(r[k]) for k in keys])
            refresh()
            note(label)
        return go

    def y_domain(ser_fn, ser_keys, par_fn, par_keys, label, parallel_first):
        """For x as Y 组：对 y = 1/x 执行同构操作，结果写回 x（以 y 域口径）。"""
        def go():
            need(1)
            snap()
            x = page._stack[0]
            y = 1 / x
            try:
                if parallel_first:      # Equivalent Parallel Circuit（先）
                    r = par_fn(y.real, y.imag)
                    keys = par_keys
                else:                   # Equivalent Series Circuit（先）
                    r = ser_fn(y.real, y.imag)
                    keys = ser_keys
            except Exception as exc:
                raise _InputError("%s：%s" % (label, exc))
            if isinstance(r, dict) and r.get("ok") is False:
                raise _InputError("%s：%s" % (label, r.get("error", "失败")))
            page._stack[0] = complex(*[float(r[k]) for k in keys])
            refresh()
            note(label + "（y=1/x）")
        return go

    # ---- 按钮装配 ----
    btns = QtWidgets.QHBoxLayout()
    btns.setSpacing(8)
    row2 = QtWidgets.QHBoxLayout()
    row2.setSpacing(8)

    colA = QtWidgets.QVBoxLayout()
    colA.setSpacing(5)
    a_items = (("Undo", "cm_undo", undo), ("Clear All", "cm_clear_all", clear_all),
               ("Clear x", "cm_clear_x", clear_x), ("Swap x<>y", "cm_swap", swap),
               ("Drop", "cm_drop", drop), ("Enter", "cm_enter", enter))
    for txt, nm, fn in a_items:
        bt = QtWidgets.QPushButton(txt)
        bt.setObjectName(nm)
        bt.setFixedHeight(22)
        bt.clicked.connect(lambda _=False, f=fn: _run_calc(page, f))
        colA.addWidget(bt)
    colA.addStretch(1)
    btns.addLayout(colA)

    mat = QtWidgets.QGridLayout()
    mat.setHorizontalSpacing(6)
    mat.setVerticalSpacing(5)
    b_rows = [
        [("SIN", "cm_sin", unary_op("sin", "SIN")),
         ("ASIN", "cm_asin", unary_op("asin", "ASIN")),
         ("1 / x", "cm_inv", unary_op("inv", "1/x")),
         ("", "cm_blank_r1c4", None)],
        [("COS", "cm_cos", unary_op("cos", "COS")),
         ("ACOS", "cm_acos", unary_op("acos", "ACOS")),
         ("SQR", "cm_sqr", unary_op("sqr", "SQR")),
         ("X", "cm_mul", binary_op(lambda y, x: y * x, "×"))],
        [("TAN", "cm_tan", unary_op("tan", "TAN")),
         ("ATAN", "cm_atan", unary_op("atan", "ATAN")),
         ("x ^ 2", "cm_sqr2", unary_op("sqr2", "x^2")),
         ("-", "cm_sub", binary_op(lambda y, x: y - x, "−"))],
        [("COT", "cm_cot", unary_op("cot", "COT")),
         ("ACOT", "cm_acot", unary_op("acot", "ACOT")),
         ("|x|", "cm_abs", unary_op("abs", "|x|")),
         ("+", "cm_add", binary_op(lambda y, x: y + x, "+"))],
        [("LOG [10]", "cm_log10", unary_op("log10", "LOG10")),
         ("y ^ x", "cm_pow_yx", binary_op(lambda y, x: y ** x, "y^x")),
         ("Conjugate", "cm_conj", unary_op("conj", "Conjugate")),
         ("e", "cm_e_exp", lambda: push(complex(math.e, 0), "e 压栈"))],
        [("LN [e]", "cm_ln", unary_op("ln", "LN")),
         ("e ^ x", "cm_exp_e", unary_op("exp_e", "e^x")),
         ("ATANH", "cm_atanh", unary_op("atanh", "ATANH")),
         ("π", "cm_pi", lambda: push(complex(cmath.pi, 0), "π 压栈"))],
        [("SINH", "cm_sinh", unary_op("sinh", "SINH")),
         ("COSH", "cm_cosh", unary_op("cosh", "COSH")),
         ("TANH", "cm_tanh", unary_op("tanh", "TANH")),
         ("COTH", "cm_coth", unary_op("coth", "COTH"))],
    ]
    for r_, row_items in enumerate(b_rows):
        for c_, (txt, nm, fn) in enumerate(row_items):
            bt = QtWidgets.QPushButton(txt)
            bt.setObjectName(nm)
            bt.setFixedHeight(22)
            if fn is None:
                bt.setEnabled(False)
            else:
                bt.clicked.connect(lambda _=False, f=fn: _run_calc(page, f))
            mat.addWidget(bt, r_, c_)
    btns.addLayout(mat)

    colC = QtWidgets.QVBoxLayout()
    colC.setSpacing(5)

    def z2gamma():
        need(1)
        snap()
        zo = zval()
        x = page._stack[0]
        if x == -zo:
            raise _InputError("Z→Γ：Z = -Zo，Γ 无定义")
        page._stack[0] = (x - zo) / (x + zo)
        refresh()
        note("Z→Γ")

    def gamma2z():
        need(1)
        snap()
        zo = zval()
        g = page._stack[0]
        if g == 1:
            raise _InputError("Γ→Z：Γ = 1，Z 无定义")
        page._stack[0] = zo * (1 + g) / (1 - g)
        refresh()
        note("Γ→Z")

    for txt, nm, fn in (("Z —> Γ", "cm_z2gamma", z2gamma),
                        ("Γ —> Z", "cm_gamma2z", gamma2z),
                        ("R to P (deg)", "cm_r2p", r2p),
                        ("P (deg) to R", "cm_p2r", p2r),
                        ("RCL Yo to x", "cm_rcl_yo",
                         lambda: push(_fopt(y0, 0.020, "Yo"), "Yo 入栈")),
                        ("RCL Zo to x", "cm_rcl_zo",
                         lambda: push(complex(zval(), 0), "Zo 入栈"))):
        bt = QtWidgets.QPushButton(txt)
        bt.setObjectName(nm)
        bt.setFixedHeight(22)
        bt.clicked.connect(lambda _=False, f=fn: _run_calc(page, f))
        colC.addWidget(bt)
    colC.addStretch(1)
    btns.addLayout(colC)

    sto = QtWidgets.QGridLayout()
    sto.setHorizontalSpacing(6)
    sto.setVerticalSpacing(5)
    letters = "abcdef"

    def mk_sto(ch):
        def go():
            need(1)
            page._regs[ch] = page._stack[0]
            note("STO %s：寄存器已存 x" % ch.upper())
        return go

    def mk_rcl(ch):
        def go():
            if ch not in page._regs:
                raise _InputError("RCL %s：寄存器为空" % ch.upper())
            push(page._regs[ch], "RCL %s" % ch.upper())
        return go

    for i, ch in enumerate(letters):
        bt = QtWidgets.QPushButton("STO " + ch.upper())
        bt.setObjectName("cm_sto_" + ch)
        bt.setFixedHeight(22)
        bt.clicked.connect(lambda _=False, f=mk_sto(ch): _run_calc(page, f))
        sto.addWidget(bt, i, 0)
        bt = QtWidgets.QPushButton("RCL " + ch.upper())
        bt.setObjectName("cm_rcl_" + ch)
        bt.setFixedHeight(22)
        bt.clicked.connect(lambda _=False, f=mk_rcl(ch): _run_calc(page, f))
        sto.addWidget(bt, i, 1)
    btns.addLayout(sto)
    btns.addStretch(1)

    zgrp = QtWidgets.QGroupBox("For x as Z")
    zv = QtWidgets.QVBoxLayout(zgrp)
    zv.setSpacing(4)
    for txt, nm, fn in (
            ("Normalize Z", "cm_norm_z",
             eng_op(lambda a, b: _e().z_normalize(a, b, zval()),
                    "Normalize Z", ("zn_re", "zn_im"))),
            ("Unnormalize Zn", "cm_unnorm_z",
             eng_op(lambda a, b: _e().z_unnormalize(a, b, zval()),
                    "Unnormalize Zn", ("z_re", "z_im"))),
            ("Equivalent Series Circuit", "cm_eq_series_z",
             eng_op(lambda a, b: _e().par_to_ser(a, b),
                    "Equivalent Series", ("r", "x"))),
            ("Equivalent Parallel Circuit", "cm_eq_parallel_z",
             eng_op(lambda a, b: _e().ser_to_par(a, b),
                    "Equivalent Parallel", ("rp", "xp")))):
        bt = QtWidgets.QPushButton(txt)
        bt.setObjectName(nm)
        bt.setMinimumHeight(20)
        bt.clicked.connect(lambda _=False, f=fn: _run_calc(page, f))
        zv.addWidget(bt)
    zv.addStretch(1)
    row2.addWidget(zgrp)

    ygrp = QtWidgets.QGroupBox("For x as Y")
    yv = QtWidgets.QVBoxLayout(ygrp)
    yv.setSpacing(4)
    for txt, nm, fn in (
            ("Normalize Y", "cm_norm_y",
             lambda: _run_calc(page, _y_norm())),
            ("Unnormalize Yn", "cm_unnorm_y",
             lambda: _run_calc(page, _y_unnorm())),
            ("Equivalent Parallel Circuit", "cm_eq_parallel_y",
             y_domain(lambda a, b: _e().ser_to_par(a, b), ("rp", "xp"),
                      lambda a, b: _e().par_to_ser(a, b), ("r", "x"),
                      "Equivalent Parallel", True)),
            ("Equivalent Series Circuit", "cm_eq_series_y",
             y_domain(lambda a, b: _e().par_to_ser(a, b), ("r", "x"),
                      lambda a, b: _e().ser_to_par(a, b), ("rp", "xp"),
                      "Equivalent Series", False))):
        bt = QtWidgets.QPushButton(txt)
        bt.setObjectName(nm)
        bt.setMinimumHeight(20)
        bt.clicked.connect(lambda _=False, f=fn: _run_calc(page, f))
        yv.addWidget(bt)
    yv.addStretch(1)
    row2.addWidget(ygrp)

    def _y_norm():
        def go():
            need(1)
            snap()
            yo = _fopt(y0, 0.020, "Yo")
            y = 1 / page._stack[0] / yo
            page._stack[0] = y
            refresh()
            note("Normalize Y（y=1/x ÷ Yo）")
        return go

    def _y_unnorm():
        def go():
            need(1)
            snap()
            yo = _fopt(y0, 0.020, "Yo")
            page._stack[0] = (1 / page._stack[0]) * yo
            refresh()
            note("Unnormalize Yn（y=1/x × Yo）")
        return go

    body2 = QtWidgets.QHBoxLayout()
    g1 = QtWidgets.QPushButton("Select Equivalent Circuit")
    g1.setObjectName("cm_select_equiv")
    g1.setFixedHeight(20)

    def select_equiv():
        page._equiv_ser = not getattr(page, "_equiv_ser", True)
        note("等效电路解释：%s" % ("串联" if page._equiv_ser else "并联"))

    g1.clicked.connect(select_equiv)
    body2.addWidget(g1)
    body2.addSpacing(8)
    cv_re = _num_box("cm_cv_re", "")
    cv_re.setPlaceholderText("Re")
    cv_im = _num_box("cm_cv_im", "")
    cv_im.setPlaceholderText("Im")

    def enter_cv(as_y):
        def go():
            re_t = cv_re.text().strip()
            im_t = cv_im.text().strip()
            if not re_t and not im_t:
                raise _InputError("请先填 Re/Im")
            z = complex(float(re_t or 0), float(im_t or 0))
            push(1 / z if as_y else z, "Enter %s" % ("Y" if as_y else "Z"))
        return go

    bz = QtWidgets.QPushButton("Enter Z from Circuit Values")
    bz.setObjectName("cm_enter_z_cv")
    by = QtWidgets.QPushButton("Enter Y from Circuit Values")
    by.setObjectName("cm_enter_y_cv")
    for bt, fn in ((bz, enter_cv(False)), (by, enter_cv(True))):
        bt.setFixedHeight(26)
        bt.clicked.connect(lambda _=False, f=fn: _run_calc(page, f))
        body2.addWidget(bt)
    body2.addSpacing(8)
    body2.addWidget(_ql("Re/Im:"))
    body2.addWidget(cv_re)
    body2.addWidget(cv_im)
    body2.addStretch(1)
    v.addLayout(body, 1)
    v.addLayout(btns)
    v.addLayout(row2)
    v.addLayout(body2)
    erow = QtWidgets.QHBoxLayout()
    erow.addWidget(err)
    erow.addStretch(1)
    v.addLayout(erow)

    for le in (re_rows[1], im_rows[1]):
        le.editingFinished.connect(lambda: _run_calc(page, _sync_x()))

    def _sync_x():
        def go():
            page._stack.insert(0, parse_x())
            del page._stack[7:]
            refresh()
        return go

    fix.valueChanged.connect(lambda _v: refresh())
    refresh()
    return page


# --- SPCS 彩蛋页 -------------------------------------------------------------

_BYLAWS = (
    "章程一：不得让器件在超过绝对最大额定值的电压下「试试看」。",
    "章程二：散热片不是可选项，是基本人权。",
    "章程三：焊接前请先读数据手册，哪怕只读典型值那一列。",
    "章程四：静电手环丑，但比更换整套板卡便宜。",
    "章程五：示波器探头地线严禁乱点。",
    "章程六：若已违反上述条款，请深呼吸，然后回到热设计页重新计算。",
)


class _SpcsFrame(QtWidgets.QFrame):
    """羊皮纸花边：#817800 双线边框 + 四角卷草纹 + 三行哥特风标题。"""

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pen = _pen(p, "#817800", 3.0)
        p.drawRoundedRect(10, 10, w - 20, h - 20, 14, 14)
        p.setPen(_pen(p, "#817800", 1.2))
        p.drawRoundedRect(18, 18, w - 36, h - 36, 10, 10)
        for cx, cy in ((0, 0), (w, 0), (0, h), (w, h)):
            p.drawArc(QtCore.QRectF(cx - 34, cy - 34, 68, 68),
                      0, 360 * 16)
        f = QtGui.QFont("Old English Text MT", 24)
        if not f.exactMatch():
            f = QtGui.QFont("Times New Roman", 24)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QtGui.QColor("#817800"))
        p.drawText(QtCore.QRect(0, 34, w, 40),
                   Qt.AlignHCenter, "Society for the Prevention")
        p.drawText(QtCore.QRect(0, 78, w, 40), Qt.AlignHCenter, "of Cruelty")
        p.drawText(QtCore.QRect(0, 122, w, 44), Qt.AlignHCenter,
                   "to Semiconductors")
        p.end()


def _mk_spcs(win):
    page = QtWidgets.QWidget()
    page.setObjectName("spcspg")
    page.name = "spcs"
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None
    err = _ql("")
    err.hide()
    page.err = err
    page.setStyleSheet("QWidget#spcspg { background:#F4F4E5; }")
    win._pages["spcs"] = page
    win._order.append("spcs")
    win._stack.addWidget(page)
    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(14, 14, 14, 14)
    frame = _SpcsFrame()
    fv = QtWidgets.QVBoxLayout(frame)
    fv.setContentsMargins(30, 180, 30, 24)
    fv.addSpacing(150)
    wel = _ql("欢迎 Welcome！这里是半导体保护协会（clean-room 幽默复刻页，"
              "文案自拟，与原应用无文案派生关系）。")
    wel.setWordWrap(True)
    fv.addWidget(wel)
    obj = _ql("协会宗旨：让每一颗芯片都在额定结温内体面地工作。")
    obj.setWordWrap(True)
    fv.addWidget(obj)
    fv.addSpacing(6)
    for line in _BYLAWS:
        lab = _ql(line)
        lab.setWordWrap(True)
        fv.addWidget(lab)
    fv.addStretch(1)
    hb = QtWidgets.QHBoxLayout()
    hb.addStretch(1)
    mb = QtWidgets.QPushButton("Menu [F8]")
    mb.setObjectName("spcs.menu")
    mb.clicked.connect(lambda: win.show_page(win._order[0]))
    hb.addWidget(mb)
    fv.addLayout(hb)
    v.addWidget(frame)
    v.addWidget(err)
    return page


# 左树分组：[(组键, 组名, [(页面键, 菜单显示名, 图标键), ...]), ...]
# 顺序对应 AppCAD 主菜单 Outlook 侧栏（Transmission Lines / Active Circuits /
# Signals-Systems / Reliability / Engnr Tools）。
# ---------------------------------------------------------------------------
# v3 新增 6 页：Everything S-Parameters / Lumped Element Balun /
#              RFIC 电压·电流偏置 / 检波器 大·小信号
# 依据 spec_v3（spec_cd.md §1–2、spec_ac2.md §1–4）：坐标 = AppCAD 原图
# 970×693 像素锚点，各页用 OX/OY 裁切到自己的画布。clean-room：引擎只调用
# backend/rfcalc.py 既有函数，不翻译任何 AppCAD 代码。
# ---------------------------------------------------------------------------

def _acsrc(p, cx, cy, r=12.0):
    """交流源：空心圆 + 蓝色正弦（spec_cd §B 符号规范）。"""
    _pen(p, "#000000", 1.0)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r))
    path = QtGui.QPainterPath()
    path.moveTo(cx - r * 0.62, cy)
    path.cubicTo(cx - r * 0.30, cy - r * 0.60, cx + r * 0.30, cy + r * 0.60,
                 cx + r * 0.62, cy)
    _pen(p, "#2244CC", 1.3)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def _cap_h(p, cx, y, w=16.0, gap=4.5):
    """串联电容（水平导线，两块竖直极板）。"""
    _line(p, cx - gap, y - w / 2.0, cx - gap, y + w / 2.0, "#000000", 1.8)
    _line(p, cx + gap, y - w / 2.0, cx + gap, y + w / 2.0, "#000000", 1.8)


def _cap_shunt(p, cx, y, w=18.0, gap=4.0):
    """并联电容（两块水平极板）。"""
    _line(p, cx - w / 2.0, y - gap, cx + w / 2.0, y - gap, "#000000", 1.8)
    _line(p, cx - w / 2.0, y + gap, cx + w / 2.0, y + gap, "#000000", 1.8)


def _coil_h(p, x0, x1, cy, n=3, r=10.0):
    """串联电感：n 个向上的半圆拱（spec_cd：三拱 34×20）。"""
    _pen(p, "#000000", 1.0)
    p.setBrush(Qt.NoBrush)
    step = (x1 - x0) / float(n)
    for i in range(n):
        p.drawArc(QtCore.QRectF(x0 + i * step, cy - r, step, 2 * r),
                  0, 180 * 16)


def _coil_v(p, cx, y0, y1, n=3, r=8.0):
    """并联电感：n 个向右的半圆凸起（竖直扼流圈）。"""
    _pen(p, "#000000", 1.0)
    p.setBrush(Qt.NoBrush)
    h = (y1 - y0) / float(n)
    for i in range(n):
        p.drawArc(QtCore.QRectF(cx - r, y0 + i * h, 2 * r, h),
                  90 * 16, -180 * 16)


def _diode(p, x0, x1, cy, h=15.0, fill="#33497A"):
    """二极管：三角形 + 阴极竖条（尖端朝 +x）。"""
    _poly(p, [(x0, cy - h), (x0, cy + h), (x1, cy)], fill)
    _line(p, x1, cy - h, x1, cy + h, "#000000", 2.0)


def _port_node(p, cx, cy, r=6.0, filled=False):
    """端口/节点符号：空心或实心小圆。"""
    _pen(p, "#000000", 1.0)
    p.setBrush(QtGui.QColor("#000000") if filled else Qt.NoBrush)
    p.drawEllipse(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r))
    p.setBrush(Qt.NoBrush)


def _xbox(p, x, y, w=24.0, h=34.0, txt="jX"):
    """负载电抗白矩形（spec_cd：白底 1px 黑框 24×34）。"""
    _poly(p, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], "#FFFFFF")
    f = QtGui.QFont("Times New Roman", 10)
    p.setFont(f)
    p.setPen(_pen(p))
    p.drawText(QtCore.QRectF(x, y, w, h), Qt.AlignCenter, txt)


# ---- 1) Everything S-Parameters（Smith 图，spec_cd §1）---------------------

class _DgSmith(_Diagram):
    """Smith 圆图：圆心原图 (576,460)、半径 186；r 圆族 + x 弧族 + 迹线。"""

    NAME = "sparam"
    LW, LH = 700.0, 400.0
    OX, OY = 214.0, 258.0          # 原图绘图区左上 (219,263) → 画布 (5,5)
    CX, CY, R = 576.0, 460.0, 186.0
    R_VALS = (0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
    X_VALS = (0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
    # 原图标签锚点（顶弧 / 底弧 / 实轴右行 / 实轴左行）
    R_LABEL_X = {0.2: 607, 0.4: 629, 0.6: 646, 0.8: 659, 1.0: 669,
                 1.5: 688, 2.0: 700, 3.0: 716, 5.0: 731, 10.0: 745}
    X_LABEL = ((0.2, 395, 375), (0.4, 437, 331), (0.6, 478, 312),
               (0.8, 522, 296), (1.0, 560, 292), (1.5, 645, 308),
               (2.0, 680, 330), (3.0, 718, 357), (5.0, 737, 392),
               (10.0, 753, 423))
    XL_LABEL = ((-0.2, 392, 551), (-0.4, 428, 598), (-0.6, 472, 626),
                (-0.8, 515, 643), (-1.0, 557, 647), (-1.5, 645, 631),
                (-2.0, 682, 609), (-3.0, 718, 574), (-5.0, 737, 531),
                (-10.0, 752, 497))
    AX_LABEL_X = {-0.2: 539, -0.4: 502, -0.6: 464, -0.8: 427, -1.0: 390}

    def __init__(self):
        super().__init__()
        self.traces = []        # [("S11", "#00A000", [(gr, gi), ...])]
        self.markers = []       # [("S11", gr, gi, "#00A000")]

    def _to_px(self, gr, gi):
        return (self.CX + self.R * gr, self.CY - self.R * gi)

    @staticmethod
    def _qt_deg(cx, cy, px, py):
        """点相对圆心的 Qt 角度（0=3 点钟，逆时针为正，单位度）。"""
        return math.degrees(math.atan2(-(py - cy), px - cx))

    def _arc_between(self, p, cx, cy, r, a0, a1):
        d = (a1 - a0) % 360.0
        if d > 180.0:
            d -= 360.0
        _pen(p, "#000000", 0.9)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r),
                  int(a0 * 16), int(d * 16))

    def draw(self, p):
        # 绘图区白底（原图 (219,263)-(914,654)）
        _poly(p, [(219, 263), (914, 263), (914, 654), (219, 654)], "#FFFFFF")
        cx, cy, r0 = self.CX, self.CY, self.R
        _pen(p, "#000000", 1.2)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QtCore.QRectF(cx - r0, cy - r0, 2 * r0, 2 * r0))
        _line(p, 390, cy, 762, cy, "#000000", 1.1)
        # r 圆族：圆心 (cx + r0·r/(1+r), cy)，半径 r0/(1+r)
        for rv in self.R_VALS:
            c = cx + r0 * rv / (1.0 + rv)
            rr = r0 / (1.0 + rv)
            _pen(p, "#000000", 0.9)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QtCore.QRectF(c - rr, cy - rr, 2 * rr, 2 * rr))
        # x 弧族：圆心 (cx+r0, cy ∓ r0/x)，半径 r0/|x|，从开路点到单位圆交点
        for xv in self.X_VALS:
            for sign in (1.0, -1.0):
                a = r0 / xv
                ccx, ccy = cx + r0, cy - sign * a
                gr = (xv * xv - 1.0) / (xv * xv + 1.0)
                gi = sign * 2.0 * xv / (xv * xv + 1.0)
                ex, ey = self._to_px(gr, gi)
                a0 = self._qt_deg(ccx, ccy, cx + r0, cy)
                a1 = self._qt_deg(ccx, ccy, ex, ey)
                self._arc_between(p, ccx, ccy, a, a0, a1)
        # 标签（原图 7px 黑字，无白底）
        f = QtGui.QFont("Times New Roman", 8)
        p.setFont(f)
        p.setPen(_pen(p))
        for xv in self.X_VALS:
            for (val, lx, ly) in self.X_LABEL:
                if val == xv:
                    p.drawText(QtCore.QRectF(lx - 8, ly - 8, 30, 14),
                               Qt.AlignLeft | Qt.AlignVCenter, "%g" % xv)
            for (val, lx, ly) in self.XL_LABEL:
                if abs(val) == xv:
                    p.drawText(QtCore.QRectF(lx - 12, ly - 8, 34, 14),
                               Qt.AlignLeft | Qt.AlignVCenter, "%g" % val)
        # 实轴刻度数字（右行在轴上方、左行在轴下方）
        for rv, lx in self.R_LABEL_X.items():
            p.drawText(QtCore.QRectF(lx - 8, cy - 17, 30, 13),
                       Qt.AlignLeft | Qt.AlignVCenter, "%g" % rv)
        for rv, lx in self.AX_LABEL_X.items():
            p.drawText(QtCore.QRectF(lx - 10, cy + 5, 32, 13),
                       Qt.AlignLeft | Qt.AlignVCenter, "%g" % rv)
        # 迹线 / 标记
        for _nm, color, pts in self.traces:
            if len(pts) < 2:
                continue
            _pen(p, color, 1.6)
            p.setBrush(Qt.NoBrush)
            path = QtGui.QPainterPath()
            x0, y0 = self._to_px(*pts[0])
            path.moveTo(x0, y0)
            for gr, gi in pts[1:]:
                x, y = self._to_px(gr, gi)
                path.lineTo(x, y)
            p.drawPath(path)
        for nm, gr, gi, color in self.markers:
            x, y = self._to_px(gr, gi)
            _pen(p, color, 1.2)
            p.setBrush(QtGui.QColor(color))
            p.drawEllipse(QtCore.QRectF(x - 3.0, y - 3.0, 6.0, 6.0))
            p.setBrush(Qt.NoBrush)
            p.setPen(_pen(p))
            p.drawText(QtCore.QRectF(x + 5, y - 6, 40, 13),
                       Qt.AlignLeft | Qt.AlignVCenter, nm)

def _mk_sparam(win):
    """Everything S-Parameters（spec_cd §1）：左树 + Smith 图 + 文件槽 + 结果。"""
    page = QtWidgets.QWidget()
    page.setObjectName("sparampg")
    page.setStyleSheet(_page_css("sparam", "#D4D0C8"))
    page.name = "sparam"
    page.results = {}
    page.combos = {}
    page.unit_labels = []
    page.dg = None

    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(10, 6, 10, 2)
    v.setSpacing(4)
    top = QtWidgets.QHBoxLayout()
    tf = QtGui.QFont("Times New Roman", 20)
    tf.setBold(True)
    lab = QtWidgets.QLabel("Everything S-Parameters")
    lab.setObjectName("pgtitle")
    lab.setFont(tf)
    mm = QtWidgets.QPushButton("Main Menu [F8]")
    mm.setObjectName("sparam.mainmenu")
    mm.clicked.connect(win.toggle_sidebar)
    top.addWidget(lab)
    top.addStretch(1)
    top.addWidget(mm, 0, Qt.AlignTop)
    v.addLayout(top)

    body = QtWidgets.QHBoxLayout()
    body.setSpacing(8)
    left = QtWidgets.QVBoxLayout()
    left.setSpacing(5)
    right = QtWidgets.QVBoxLayout()
    right.setSpacing(5)

    # ---- Display Type ----
    dt, dtl = _grp("Display Type")
    dt.setFixedWidth(196)
    page.dtype = QtWidgets.QComboBox()
    page.dtype.setObjectName("sparam.dtype")
    page.dtype.addItems(["Tabular Listing", "Smith Chart", "Rectangle Plot",
                         "Polar Plot", "Smith Chart: Gain/Noise Circles",
                         "View S-parameter file"])
    page.dtype.setCurrentIndex(1)                    # spec：默认 Smith Chart
    dtl.addWidget(page.dtype)
    left.addWidget(dt)

    # ---- Frequency [MHz] ----
    fg, fgl = _grp("Frequency [MHz]")
    fg.setFixedWidth(196)
    page.fmode = QtWidgets.QComboBox()
    page.fmode.setObjectName("sparam.fmode")
    page.fmode.addItems(["Start - Stop", "Center - Bwid"])
    fgl.addWidget(page.fmode)
    page.fstart = _num_box("sparam.fstart", "1000", 70)
    page.fstop = _num_box("sparam.fstop", "5000", 70)
    page.fstep = _num_box("sparam.fstep", "200", 70)
    fgl.addWidget(_grow("Start =", page.fstart))
    fgl.addWidget(_grow("Stop =", page.fstop))
    fgl.addWidget(_grow("Step =", page.fstep))
    page.auto_freq = QtWidgets.QCheckBox("Auto-set freqs from file")
    page.auto_freq.setObjectName("sparam.autofreq")
    page.auto_freq.setChecked(True)
    fgl.addWidget(page.auto_freq)
    left.addWidget(fg)

    calc_b = QtWidgets.QPushButton("Calculate [F4]")
    calc_b.setObjectName("sparam.calc")
    left.addWidget(calc_b, 0, Qt.AlignLeft)
    desel = QtWidgets.QPushButton("Deselect All")
    desel.setObjectName("sparam.deselect")
    left.addWidget(desel, 0, Qt.AlignLeft)

    # ---- 左树：Select Parameters to Display ----
    tg, tgl = _grp("Select Parameters to Display:")
    tg.setFixedWidth(196)
    page.tree = QtWidgets.QTreeWidget()
    page.tree.setObjectName("sparam.tree")
    page.tree.setHeaderHidden(True)
    page.tree.setFixedHeight(190)
    tree_def = (
        ("Differential Port Conversion",
         [("S22P-to-Differential 1-Port", True)]),
        ("Matching", [("Gamma Ms", False), ("Gamma Ml", False),
                      ("S11'", False), ("S22'", False)]),
        ("Noise", [("Gamma Opt", False)]),
        ("S Parameters", [("S11", True), ("S22", True),
                          ("S21", True), ("S12", True)]),
    )
    page._checks = []
    for gname, leaves in tree_def:
        g = QtWidgets.QTreeWidgetItem([gname])
        g.setFlags(Qt.ItemIsEnabled)
        page.tree.addTopLevelItem(g)
        for lname, on in leaves:
            it = QtWidgets.QTreeWidgetItem([lname])
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
            g.addChild(it)
            page._checks.append(it)
    page.tree.expandAll()
    tgl.addWidget(page.tree)
    left.addWidget(tg)
    left.addStretch(1)

    # ---- 右：文件槽 + Plot Options + Smith 图 ----
    slots = QtWidgets.QWidget()
    slots.setStyleSheet("background:#F5F5F2;")
    sl = QtWidgets.QVBoxLayout(slots)
    sl.setContentsMargins(6, 4, 6, 4)
    sl.setSpacing(3)
    hint = _ql("[Load S parameters from menu:  File Open]")
    hint.setStyleSheet("color:#606060;font-size:8pt;")
    sl.addWidget(hint)
    sg = QtWidgets.QGridLayout()
    sg.setHorizontalSpacing(10)
    sg.setVerticalSpacing(3)
    for i in range(6):
        le = QtWidgets.QLineEdit("")
        le.setReadOnly(True)
        le.setPlaceholderText("[no file loaded]")
        le.setObjectName("sparam.slot%d" % (i + 1))
        le.setFixedWidth(140)
        le.setStyleSheet("background:#FFFFFF;color:#909090;font-style:italic;")
        sg.addWidget(le, i // 3, i % 3)
    sl.addLayout(sg)
    slots.setFixedHeight(90)
    right.addWidget(slots, 0, Qt.AlignLeft)

    po, pol = _grp("Plot Options")
    prow = QtWidgets.QHBoxLayout()
    page.auto_y = QtWidgets.QCheckBox("Auto Scale Y Axis")
    page.auto_y.setObjectName("sparam.autoy")
    page.auto_y.setChecked(True)
    prow.addWidget(page.auto_y)
    page.manual_y = QtWidgets.QRadioButton("Manual Y Axis")
    page.manual_y.setObjectName("sparam.manualy")
    prow.addWidget(page.manual_y)
    page.ymax = _num_box("sparam.ymax", "20", 62)
    page.ymin = _num_box("sparam.ymin", "-20", 62)
    page.offset = _num_box("sparam.offset", "", 44)
    page.offset.setEnabled(False)
    prow.addWidget(_ql("Y Max ="))
    prow.addWidget(page.ymax)
    prow.addWidget(_ql("Y Min ="))
    prow.addWidget(page.ymin)
    prow.addWidget(_ql("Offset (dB) ="))
    prow.addWidget(page.offset)
    prow.addWidget(_ql("[S21 [dB] & S12]"))
    prow.addStretch(1)
    pol.addLayout(prow)
    right.addWidget(po, 0, Qt.AlignLeft)

    dg = _DgSmith()
    right.addWidget(dg, 0, Qt.AlignLeft)
    page.dg = dg

    page.err = QtWidgets.QLabel("")
    page.err.setObjectName("sparam.error")
    page.err.setStyleSheet("color:%s;font-weight:bold;" % _ERR)
    page.err.hide()
    right.addWidget(page.err, 0, Qt.AlignLeft)

    rows = QtWidgets.QHBoxLayout()
    for key, label, unit in (("k", "K =", ""), ("g0db", "G0 =", "dB"),
                             ("magdb", "MAG =", "dB")):
        rows.addWidget(_ql(label, align_right=True))
        le = QtWidgets.QLineEdit()
        le.setReadOnly(True)
        le.setFixedWidth(76)
        le.setAlignment(Qt.AlignRight)
        le.setObjectName("sparam.result." + key)
        le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
        page.results[key] = le
        rows.addWidget(le)
        if unit:
            rows.addWidget(_ql(unit))
        rows.addSpacing(10)
    rows.addStretch(1)
    right.addLayout(rows)
    right.addStretch(1)

    body.addLayout(left, 0)
    body.addLayout(right, 1)
    v.addLayout(body, 1)
    win._pages["sparam"] = page
    win._order.append("sparam")
    win._stack.addWidget(page)

    # 示例数据：载入 .s2p 前用一组固定 S 参数演示 Smith 迹线与 K/G/MAG
    # （引擎口径：stability_from_s / gain_from_s）
    page._s = (0.35, -0.28, 0.04, 0.01, 1.62, 0.55, 0.31, -0.22)

    def calc():
        e = _e()
        s = page._s
        fstart = _fopt(page.fstart, 1000.0, "Start")
        fstop = _fopt(page.fstop, 5000.0, "Stop")
        fstep = _fopt(page.fstep, 200.0, "Step")
        if fstop <= fstart:
            raise _InputError("Stop 必须大于 Start")
        if fstep <= 0:
            raise _InputError("Step 必须大于 0")
        # Smith 迹线：示例用 S11 沿一条 r=1.5 圆的小弧（随频率平移）
        dg.traces = [("S11", "#00A000",
                      [(0.42, -0.30), (0.30, -0.20), (0.18, -0.10),
                       (0.05, -0.02)])]
        dg.markers = [("S11", s[0], s[1], "#00A000"),
                      ("S22", s[6], s[7], "#CC6600")]
        st = e.stability_from_s(*s)
        gn = e.gain_from_s(*s)
        if isinstance(st, dict) and st.get("ok") is False:
            raise _InputError(st.get("error", "稳定性计算失败"))
        if isinstance(gn, dict) and gn.get("ok") is False:
            raise _InputError(gn.get("error", "增益计算失败"))
        page.results["k"].setText("%.4f" % st["k"])
        page.results["g0db"].setText("%.4f" % gn["g0_db"])
        page.results["magdb"].setText("%.4f" % gn["mag_db"])
        dg.update()
        n = int(round((fstop - fstart) / fstep)) + 1
        return "S 参数示例：%d 个频点  K=%.3f  MAG=%.2f dB" % (
            n, st["k"], gn["mag_db"])

    calc_b.clicked.connect(lambda: _run_calc(page, calc))
    desel.clicked.connect(lambda: [it.setCheckState(0, Qt.Unchecked)
                                   for it in page._checks])
    return page


# ---- 2) Lumped Element Balun（spec_cd §2）----------------------------------

class _DgBalun(_Diagram):
    """两方案巴伦拓扑：Sol1 六元件（顶轨 C-L-C / 底轨 C-L-C）、
    Sol2 四元件（顶轨 L-C / 底轨 C-L）。坐标 = 原图 970×693 像素锚点。"""

    NAME = "balun"
    LW, LH = 780.0, 480.0
    OX, OY = 30.0, 190.0

    def _src(self, p, x, y_top, y_bot, cy):
        """左侧 N0 竖条 + 源支路（Rg 水平锯齿 + AC 源 + 地）。"""
        _line(p, x, y_top, x, y_bot)
        _line(p, x, cy, 93, cy)
        _port_node(p, 93, cy, 2.5, filled=True)
        pts = [(57, cy + 28)]
        for i in range(6):
            pts.append((57 + 5.5 * (i + 1),
                        cy + 28 + (8 if i % 2 == 0 else -8)))
        pts.append((90, cy + 28))
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(QtGui.QPolygonF(
            [QtCore.QPointF(a, b) for a, b in pts]))
        _line(p, 57, cy + 28, 57, cy + 51)
        _acsrc(p, 67, cy + 52)
        _line(p, 67, cy + 64, 67, cy + 69)
        _gnd4(p, 67, cy + 69)

    def _zload(self, p, x, y_top, y_bot):
        """右侧 Zload 竖支路：R 竖锯齿 + jX 白矩形。"""
        _line(p, 490, y_top, x, y_top)
        _line(p, 490, y_bot, x, y_bot)
        _line(p, x, y_top, x, y_top + 10)
        _zig_v(p, x, y_top + 10, y_top + 38, w=8.0, n=5)
        mid = (y_top + y_bot) / 2.0
        _line(p, x, y_top + 38, x, mid - 17)
        _xbox(p, x - 12, mid - 17, 24, 34)
        _line(p, x, mid + 17, x, y_bot)

    def draw(self, p):
        f = QtGui.QFont("Times New Roman", 12)
        f.setBold(True)
        p.setFont(f)
        p.setPen(_pen(p))
        self.label(470, 80, "1. Enter balanced load as Zload = R + jX")
        self.label(470, 98, "2. Enter generator Rg and Frequency")
        self.label(470, 116,
                   "3. Optional: Enter circuit parasitic capacitance , Cp")
        p.drawText(QtCore.QPointF(38, 214), "Solution 1:   6-element Balun")
        p.drawText(QtCore.QPointF(38, 449), "Solution 2:   4-element Balun")
        f2 = QtGui.QFont("Times New Roman", 10)
        p.setFont(f2)
        p.drawText(QtCore.QPointF(362, 233), "- 90?")
        p.drawText(QtCore.QPointF(362, 349), "+ 90?")
        p.drawText(QtCore.QPointF(362, 467), "- 90?")
        p.drawText(QtCore.QPointF(362, 583), "+ 90?")
        p.drawText(QtCore.QPointF(560, 235), "Zload = R + jX")
        p.drawText(QtCore.QPointF(560, 466), "Zload = R + jX")
        p.drawText(QtCore.QPointF(684, 152), "MHz")
        p.drawText(QtCore.QPointF(283, 296), "Cp")
        p.drawText(QtCore.QPointF(288, 530), "Cp")
        # Cp 竖直虚线支路（Sol1 / Sol2）
        _line(p, 310, 258, 310, 342, "#000000", 1.0, dash=(4, 3))
        _cap_shunt(p, 310, 293)
        _line(p, 315, 492, 315, 576, "#000000", 1.0, dash=(4, 3))
        _cap_shunt(p, 315, 527)
        # ---- Solution 1（顶轨 y=258 / 底轨 y=342 / 左竖条 x=121）----
        self._src(p, 121, 258, 342, 296)
        # 顶轨：C1 并 → L1 串 → C2 并
        _line(p, 121, 258, 152, 258)
        _port_node(p, 152, 258, 2.5, filled=True)
        _line(p, 152, 258, 164, 258)
        _line(p, 152, 258, 152, 270)
        _cap_shunt(p, 152, 274)
        _line(p, 152, 280, 152, 290)
        _gnd4(p, 152, 290)
        _line(p, 164, 258, 183, 258)
        _coil_h(p, 183, 217, 258, n=3, r=10)
        _line(p, 217, 258, 245, 258)
        _port_node(p, 245, 258, 2.5, filled=True)
        _line(p, 245, 258, 245, 270)
        _cap_shunt(p, 245, 274)
        _line(p, 245, 280, 245, 290)
        _gnd4(p, 245, 290)
        _line(p, 245, 258, 390, 258)
        # 底轨：C3 串 → L2 并 → C4 串
        _line(p, 121, 342, 183, 342)
        _line(p, 183, 334, 183, 350, "#000000", 1.8)
        _line(p, 191, 334, 191, 350, "#000000", 1.8)
        _line(p, 191, 342, 200, 342)
        _port_node(p, 200, 342, 2.5, filled=True)
        _line(p, 200, 342, 200, 352)
        _coil_v(p, 200, 352, 382, n=3, r=7)
        _line(p, 200, 382, 200, 388)
        _gnd4(p, 200, 388)
        _line(p, 200, 342, 240, 342)
        _line(p, 240, 334, 240, 350, "#000000", 1.8)
        _line(p, 248, 334, 248, 350, "#000000", 1.8)
        _line(p, 248, 342, 390, 342)
        _port_node(p, 390, 258, 3.0)
        _port_node(p, 390, 342, 3.0)
        _port_node(p, 490, 258, 3.0)
        _port_node(p, 490, 342, 3.0)
        self._zload(p, 568, 258, 342)
        # ---- Solution 2（顶轨 y=492 / 底轨 y=576 / 左竖条 x=121）----
        self._src(p, 121, 492, 576, 530)
        _line(p, 121, 492, 183, 492)
        _coil_h(p, 183, 217, 492, n=3, r=10)
        _line(p, 217, 492, 250, 492)
        _port_node(p, 250, 492, 2.5, filled=True)
        _line(p, 250, 492, 250, 504)
        _cap_shunt(p, 250, 508)
        _line(p, 250, 514, 250, 522)
        _gnd4(p, 250, 522)
        _line(p, 250, 492, 390, 492)
        _line(p, 121, 576, 186, 576)
        _line(p, 186, 568, 186, 584, "#000000", 1.8)
        _line(p, 194, 568, 194, 584, "#000000", 1.8)
        _line(p, 194, 576, 250, 576)
        _port_node(p, 250, 576, 2.5, filled=True)
        _line(p, 250, 576, 250, 586)
        _coil_v(p, 250, 586, 618, n=3, r=7)
        _line(p, 250, 618, 250, 624)
        _gnd4(p, 250, 624)
        _line(p, 250, 576, 390, 576)
        _port_node(p, 390, 492, 3.0)
        _port_node(p, 390, 576, 3.0)
        _port_node(p, 490, 492, 3.0)
        _port_node(p, 490, 576, 3.0)
        self._zload(p, 568, 492, 576)


def _mk_balun(win):
    page = _b_shell(win, "balun", "集总元件巴伦 Lumped Element Balun Design")
    page.setStyleSheet(_page_css("balun", "#F2DFA6"))
    dg = _DgBalun()
    page._left.addWidget(dg)
    # 图内嵌框（spec_cd §B 控件 bbox）
    dg.box("f0", 608, 133, "1000", w=68, obj="balun.f0")
    dg.box("rg1", 55, 257, "50", w=36, obj="balun.rg1")
    dg.box("r1", 606, 257, "50", w=54, obj="balun.r1")
    dg.box("x1", 616, 299, "0", w=50, obj="balun.x1")
    dg.box("cp1", 298, 365, "0", w=47, obj="balun.cp1")
    dg.box("rg2", 55, 491, "", w=36, obj="balun.rg2")
    dg.box("r2", 606, 491, "", w=54, obj="balun.r2")
    dg.box("x2", 616, 533, "", w=50, obj="balun.x2")
    dg.box("cp2", 298, 599, "", w=47, obj="balun.cp2")
    dg.finalize()
    page.dg = dg
    page._left.addSpacing(4)
    page._left.addWidget(_instructions([
        "1. 输入平衡负载 Zload = R + jX。",
        "2. 输入源阻抗 Rg 与中心频率 f0。",
        "3. 可选：输入寄生电容 Cp。",
        "4. 点 Calculate [F4] 求两方案元件值。",
        "公式：Zi = √(Rg·R)、L = Zi/ω0、C = 1/(ω0·Zi)（ω0²LC = 1）。",
        "两方案元件值相同，区别仅在结构：6 元件 π/T 臂 vs 4 元件 L 节。"]))
    page._left.addStretch(1)

    b = QtWidgets.QPushButton("Calculate [F4]")
    b.setObjectName("balun.calc")
    page._right.addWidget(b, 0, Qt.AlignLeft)
    page._right.addSpacing(8)
    grid = QtWidgets.QGridLayout()
    grid.setHorizontalSpacing(6)
    grid.setVerticalSpacing(5)
    for c, head in enumerate(("", "Solution 1 (6 el)", "Solution 2 (4 el)")):
        lab = _ql(head)
        fnt = QtGui.QFont()
        fnt.setBold(True)
        lab.setFont(fnt)
        grid.addWidget(lab, 0, c)
    for i, (name, key, unit) in enumerate((("L (inductor)", "l", "nH"),
                                          ("C (capacitor)", "c", "pF"))):
        grid.addWidget(_ql(name, align_right=True), i + 1, 0)
        for j, sol in enumerate(("1", "2")):
            le = QtWidgets.QLineEdit()
            le.setReadOnly(True)
            le.setFixedWidth(96)
            le.setAlignment(Qt.AlignRight)
            le.setObjectName("balun.result.%s%s" % (key, sol))
            le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
            page.results["%s%s" % (key, sol)] = le
            grid.addWidget(le, i + 1, j + 1)
        grid.addWidget(_ql(unit), i + 1, 3)
    grid.addWidget(_ql("Zi =", align_right=True), 3, 0)
    le = QtWidgets.QLineEdit()
    le.setReadOnly(True)
    le.setFixedWidth(96)
    le.setAlignment(Qt.AlignRight)
    le.setObjectName("balun.result.zi")
    le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
    page.results["zi"] = le
    grid.addWidget(le, 3, 1)
    grid.addWidget(_ql("Ohm"), 3, 3)
    page._right.addLayout(grid)
    page._right.addSpacing(6)
    page._right.addWidget(page.err, 0, Qt.AlignLeft)
    page._right.addStretch(1)

    def _pick(a, b_, default, name):
        if a.text().strip():
            return _f(a, name)
        if b_.text().strip():
            return _f(b_, name)
        return default

    def calc():
        eng = _e()
        f0 = _f(dg.fields["f0"], "中心频率 f0")
        rg = _pick(dg.fields["rg1"], dg.fields["rg2"], 50.0, "Rg")
        rl = _pick(dg.fields["r1"], dg.fields["r2"], 50.0, "Zload R")
        xl = _pick(dg.fields["x1"], dg.fields["x2"], 0.0, "Zload X")
        cp = _pick(dg.fields["cp1"], dg.fields["cp2"], 0.0, "Cp")
        outs = []
        for sol in (6, 4):
            d = eng.balun_lumped_design(f0 * 1e6, rg, rl, xl, cp * 1e-12, sol)
            if isinstance(d, dict) and d.get("ok") is False:
                raise _InputError(d.get("error", "巴伦计算失败"))
            outs.append(d)
        for d, sfx in ((outs[0], "1"), (outs[1], "2")):
            page.results["l" + sfx].setText("%.4f" % (d["l_h"] * 1e9))
            page.results["c" + sfx].setText("%.4f" % (d["c_f"] * 1e12))
        page.results["zi"].setText("%.4f" % outs[0]["zi"])
        warns = outs[0].get("warnings") or []
        if warns:
            page.err.setText("⚠ " + warns[0])
            page.err.show()
        return "L=%.4f nH  C=%.4f pF  Zi=%.4f Ohm（两方案同值）" % (
            outs[0]["l_h"] * 1e9, outs[0]["c_f"] * 1e12, outs[0]["zi"])

    b.clicked.connect(lambda: _run_calc(page, calc))
    return page


# ---- 3) RFIC 电压偏置（spec_ac2 §1）---------------------------------------

class _DgRficV(_Diagram):
    """Voltage-Biased RFIC：传输链 Cin-AMP-Cout + 输出侧偏置 T（Cb/RFC）。"""

    NAME = "rfic_v"
    LW, LH = 720.0, 340.0
    OX, OY = 280.0, 80.0

    def draw(self, p):
        # 主传输链 y=205
        _line(p, 322, 205, 338, 205)
        _port_node(p, 322, 205, 3.0)
        _cap_h(p, 345, 205, 16, 4.5)
        _line(p, 350, 205, 452, 205)
        # 放大器矩形 + 内三角
        _poly(p, [(452, 176), (537, 176), (537, 235), (452, 235)], "#CFE6DE")
        _poly(p, [(462, 186), (523, 205), (462, 224)], "#CFE6DE")
        _line(p, 537, 205, 565, 205)
        _line(p, 489, 235, 489, 246)
        _gnd4(p, 489, 246)
        _port_node(p, 565, 205, 3.0, filled=True)
        _line(p, 565, 205, 596, 205)
        _cap_h(p, 604, 205, 16, 4.5)
        _line(p, 609, 205, 665, 205)
        _port_node(p, 665, 205, 3.0)
        _line(p, 665, 205, 675, 205)
        _line(p, 675, 205, 675, 236)
        # 左右端口源/负载
        _line(p, 322, 205, 322, 238)
        _port_node(p, 322, 244, 9.0)
        _gnd4(p, 322, 258)
        _port_node(p, 675, 244, 9.0)
        _gnd4(p, 675, 258)
        # 偏置支路：Vs 节点 → RFC → N2；Vs → Cb → 地
        _port_node(p, 567, 105, 3.0)
        _line(p, 567, 105, 567, 138)
        _coil_v(p, 567, 138, 176, n=3, r=7)
        _line(p, 567, 176, 567, 205)
        _line(p, 567, 105, 596, 122)
        _line(p, 596, 122, 596, 122)
        _line(p, 596, 116, 596, 132, "#000000", 1.8)
        _line(p, 604, 116, 604, 132, "#000000", 1.8)
        _line(p, 604, 122, 610, 122)
        _line(p, 610, 122, 610, 132)
        _gnd4(p, 610, 132)
        # 图形区标注
        self.label(299, 222, "Z0")
        self.label(686, 230, "Z0")
        self.label(356, 236, "Cin =")
        self.label(428, 270, "Gain =")
        self.label(527, 270, "dB")
        self.label(560, 234, "Cout =")
        self.label(334, 152, "Z0 =")
        self.label(424, 152, "Ω")
        self.label(540, 108, "Vs")
        self.label(598, 108, "Cb =")
        self.label(566, 178, "RFC =")


def _mk_rfic_v(win):
    page = _b_shell(win, "rfic_v", "电压偏置 RFIC Voltage-Biased Circuit")
    page.setStyleSheet(_page_css("rfic_v", "#ECECEC"))
    dg = _DgRficV()
    dg.box("cin", 394, 222, "", w=31, obj="rfic_v.cin")
    dg.box("cout", 601, 222, "", w=47, obj="rfic_v.cout")
    dg.box("gain", 474, 254, "17", w=48, obj="rfic_v.gain")
    dg.box("z0", 366, 136, "50", w=54, obj="rfic_v.z0")
    dg.box("cb", 632, 94, "", w=36, obj="rfic_v.cb")
    dg.box("rfc", 604, 164, "", w=46, obj="rfic_v.rfc")
    dg.finalize()
    page._left.addWidget(dg)
    frow = QtWidgets.QHBoxLayout()
    page.f1 = _num_box("rfic_v.f1", "834", 58)
    page.f2 = _num_box("rfic_v.f2", "1910", 58)
    frow.addWidget(_ql("F1 =", align_right=True))
    frow.addWidget(page.f1)
    frow.addWidget(_ql("MHz"))
    frow.addSpacing(12)
    frow.addWidget(_ql("F2 =", align_right=True))
    frow.addWidget(page.f2)
    frow.addWidget(_ql("MHz"))
    frow.addStretch(1)
    page._left.addLayout(frow)
    page._left.addWidget(_instructions([
        "Note: Click on Cin, Cout to toggle in/out or use Menu Alt-S",
        "填 F1/F2/Z0/Gain 后点 Calculate [F4]：Cin/Cout/RFC/Cb 留空则由"
        "引擎按 Design Rule 综合，填入则按填入值算损耗。"]))
    page._left.addStretch(1)
    page.dg = dg

    calc_b = QtWidgets.QPushButton("Calculate [F4]")
    calc_b.setObjectName("rfic_v.calc")
    page._right.addWidget(calc_b, 0, Qt.AlignLeft)
    rule, rl = _grp("Design Rule")
    page.rule = QtWidgets.QComboBox()
    page.rule.setObjectName("rfic_v.rule.loss")
    page.rule.addItems(["0.01 dB", "0.1 dB (default)", "0.25 dB", "0.5 dB"])
    page.rule.setCurrentIndex(1)
    rl.addWidget(page.rule)
    page._right.addWidget(rule, 0, Qt.AlignLeft)
    page._right.addSpacing(6)

    grid = QtWidgets.QGridLayout()
    grid.setHorizontalSpacing(8)
    grid.setVerticalSpacing(4)
    for c, head in enumerate(("", "Band 1 (F1)", "Band 2 (F2)")):
        lab = _ql(head)
        fnt = QtGui.QFont()
        fnt.setBold(True)
        lab.setFont(fnt)
        grid.addWidget(lab, 0, c)
    rows = (("Loss due to Caps =", "caps1", "caps2", "dB"),
            ("Loss due to RFC + Cb =", "rfc1", "rfc2", "dB"),
            ("Total Loss =", "total1", "total2", "dB"),
            ("Overall Gain =", "overall1", "overall2", "dB"),
            ("f-3dB (low freq) =", "f3db", None, ""))
    for i, (label, k1, k2, unit) in enumerate(rows):
        grid.addWidget(_ql(label, align_right=True), i + 1, 0)
        for j, key in enumerate((k1, k2)):
            if not key:
                continue
            le = QtWidgets.QLineEdit()
            le.setReadOnly(True)
            le.setFixedWidth(88)
            le.setAlignment(Qt.AlignRight)
            le.setObjectName("rfic_v.result." + key)
            le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
            page.results[key] = le
            grid.addWidget(le, i + 1, j + 1)
        if unit:
            grid.addWidget(_ql(unit), i + 1, 3)
    page._right.addLayout(grid)
    page._right.addSpacing(4)
    page._right.addWidget(page.err, 0, Qt.AlignLeft)
    page._right.addStretch(1)

    def _loss_rule():
        return float(page.rule.currentText().split()[0])

    def calc():
        eng = _e()
        f1 = _f(page.f1, "F1")
        f2 = _f(page.f2, "F2")
        z0 = _fopt(dg.fields["z0"], 50.0, "Z0")
        gain = _fopt(dg.fields["gain"], 17.0, "Gain")
        cin = _fopt(dg.fields["cin"], None, "Cin") if \
            dg.fields["cin"].text().strip() else None
        cout = _fopt(dg.fields["cout"], None, "Cout") if \
            dg.fields["cout"].text().strip() else None
        rfc = _fopt(dg.fields["rfc"], None, "RFC") if \
            dg.fields["rfc"].text().strip() else None
        cb = _fopt(dg.fields["cb"], None, "Cb") if \
            dg.fields["cb"].text().strip() else None
        if f1 <= 0 or f2 <= 0:
            raise _InputError("F1/F2 必须大于 0")
        if f2 <= f1:
            raise _InputError("双带要求 F2 > F1")
        d = eng.rfic_voltage_bias(f1 * 1e6, f2 * 1e6, z0, gain,
                                  cin, cout,
                                  (rfc * 1e-9) if rfc is not None else None,
                                  cb)
        if isinstance(d, dict) and d.get("ok") is False:
            raise _InputError(d.get("error", "RFIC 计算失败"))
        for band, sfx in ((d["band1"], "1"), (d["band2"], "2")):
            page.results["caps" + sfx].setText("%.2f" % band["loss_caps_db"])
            page.results["rfc" + sfx].setText("%.2f" % band["loss_rfc_db"])
            page.results["total" + sfx].setText("%.2f" % band["total_loss_db"])
            page.results["overall" + sfx].setText(
                "%.2f" % band["overall_gain_db"])
        page.results["f3db"].setText("%.2f" % (d["f3db_low_hz"] / 1e6))
        # 综合值回填图形框（空时才回填，尊重用户输入）
        if cin is None:
            dg.fields["cin"].setText("%.1f" % d["cin_pf"])
        if cout is None:
            dg.fields["cout"].setText("%.1f" % d["cout_pf"])
        if rfc is None:
            dg.fields["rfc"].setText("%.1f" % (d["rfc_h"] * 1e9))
        if cb is None:
            dg.fields["cb"].setText("%.1f" % d["cb_pf"])
        for w in (d.get("warnings") or []):
            page.err.setText("⚠ " + str(w))
            page.err.show()
        return "Band1 总损耗 %.2f dB / 总增益 %.2f dB；f-3dB=%.2f MHz" % (
            d["band1"]["total_loss_db"], d["band1"]["overall_gain_db"],
            d["f3db_low_hz"] / 1e6)

    calc_b.clicked.connect(lambda: _run_calc(page, calc))
    return page


# ---- 4) RFIC 电流偏置（spec_ac2 §2）---------------------------------------

class _DgRficI(_Diagram):
    """Current-Biased RFIC：传输链 + Rbias/Vs/Cb/RFC 偏置 + Id/Vd 标注。"""

    NAME = "rfic_i"
    LW, LH = 730.0, 330.0
    OX, OY = 225.0, 78.0

    def draw(self, p):
        # 主传输链 y=185
        _line(p, 283, 185, 330, 185)
        _port_node(p, 283, 185, 3.0)
        _cap_h(p, 336, 185, 16, 4.5)
        _line(p, 341, 185, 405, 185)
        _poly(p, [(405, 158), (490, 158), (490, 214), (405, 214)], "#CCDCF0")
        _poly(p, [(415, 168), (480, 185), (415, 202)], "#CCDCF0")
        _line(p, 490, 185, 520, 185)
        _line(p, 438, 214, 438, 226)
        _gnd4(p, 438, 226)
        _port_node(p, 520, 185, 3.0, filled=True)
        _line(p, 520, 185, 556, 185)
        _cap_h(p, 564, 185, 16, 4.5)
        _line(p, 569, 185, 665, 185)
        _port_node(p, 665, 185, 3.0)
        _line(p, 665, 185, 675, 185)
        _line(p, 675, 185, 675, 230)
        _line(p, 283, 185, 283, 230)
        _port_node(p, 283, 236, 9.0)
        _gnd4(p, 283, 252)
        _port_node(p, 675, 236, 9.0)
        _gnd4(p, 675, 252)
        # 电源侧：Rbias → B(612,100) → Cb → 地；B → RFC(520) → N2
        _zig_v(p, 515, 92, 108, w=0.0, n=1)   # 占位（实际用水平锯齿）
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        pts = [(498, 100)]
        for i in range(4):
            pts.append((498 + 8.5 * (i + 1), 100 + (7 if i % 2 == 0 else -7)))
        pts.append((532, 100))
        p.drawPolyline(QtGui.QPolygonF(
            [QtCore.QPointF(a, b) for a, b in pts]))
        _line(p, 532, 100, 606, 100)
        _port_node(p, 612, 100, 3.0, filled=True)
        _line(p, 612, 100, 612, 126)
        _cap_shunt(p, 612, 131)
        _line(p, 612, 137, 612, 145)
        _gnd4(p, 612, 145)
        _line(p, 612, 100, 520, 100)
        _line(p, 520, 100, 520, 116)
        _coil_v(p, 520, 116, 162, n=3, r=7)
        _line(p, 520, 162, 520, 185)
        # Id ↓ / Vd ↑ 标注
        _arrow(p, 551, 118, 551, 140)
        _arrow(p, 486, 212, 486, 196)
        self.label(545, 152, "Id")
        self.label(484, 228, "Vd")
        self.label(252, 222, "Z0")
        self.label(677, 216, "Z0")
        self.label(227, 154, "Z0 =")
        self.label(314, 154, "Ω")
        self.label(343, 142, "RFC =")
        self.label(431, 142, "nH")
        self.label(312, 216, "Cin =")
        self.label(560, 168, "Cout =")
        self.label(477, 100, "Rbias =")
        self.label(605, 100, "Ohm")
        self.label(630, 100, "Vs =")
        self.label(716, 100, "V")
        self.label(592, 142, "Cb =")


def _mk_rfic_i(win):
    page = _b_shell(win, "rfic_i",
                    "电流偏置 RFIC Current-Biased Circuit")
    page.setStyleSheet(_page_css("rfic_i", "#ECECEC"))
    dg = _DgRficI()
    dg.box("z0", 258, 139, "50", w=52, obj="rfic_i.z0")
    dg.box("rfc", 381, 128, "350", w=46, obj="rfic_i.rfc")
    dg.box("cin", 350, 203, "", w=50, obj="rfic_i.cin")
    dg.box("cout", 601, 174, "", w=49, obj="rfic_i.cout")
    dg.box("rbias", 521, 85, "", w=79, obj="rfic_i.rbias")
    dg.box("vs", 662, 85, "12", w=50, obj="rfic_i.vs")
    dg.box("cb", 623, 128, "", w=49, obj="rfic_i.cb")
    dg.box("vd_max", 543, 224, "6.6", w=47, obj="rfic_i.vd_max")
    dg.box("vd_typ", 543, 250, "5.5", w=47, obj="rfic_i.vd_typ")
    dg.box("vd_min", 543, 275, "4.4", w=47, obj="rfic_i.vd_min")
    dg.finalize()
    page._left.addWidget(dg)
    row = QtWidgets.QHBoxLayout()
    page.flow = _num_box("rfic_i.f_low", "450", 58)
    row.addWidget(_ql("F low =", align_right=True))
    row.addWidget(page.flow)
    row.addWidget(_ql("MHz"))
    row.addSpacing(14)
    row.addWidget(_ql("θjc =", align_right=True))
    page.tjc = _num_box("rfic_i.theta_jc", "125", 56)
    row.addWidget(page.tjc)
    row.addWidget(_ql("°C/W"))
    row.addSpacing(14)
    row.addWidget(_ql("∂Vd/∂T =", align_right=True))
    page.dvdt = _num_box("rfic_i.dvd_dt", "-8", 50)
    row.addWidget(page.dvdt)
    row.addWidget(_ql("mV/°C"))
    row.addStretch(1)
    page._left.addLayout(row)
    row2 = QtWidgets.QHBoxLayout()
    row2.addWidget(_ql("Vd =", align_right=True))
    row2.addWidget(_ql("{ V Max / V Typ / V Min }"))
    row2.addSpacing(14)
    row2.addWidget(_ql("Id Design Goal =", align_right=True))
    page.id_goal = _num_box("rfic_i.id_goal", "80", 44)
    row2.addWidget(page.id_goal)
    row2.addWidget(_ql("mA"))
    row2.addSpacing(14)
    row2.addWidget(_ql("Tcse =", align_right=True))
    page.t1 = _num_box("rfic_i.tcse_1", "-25", 38)
    page.t2 = _num_box("rfic_i.tcse_2", "25", 38)
    page.t3 = _num_box("rfic_i.tcse_3", "85", 38)
    for bx in (page.t1, page.t2, page.t3):
        row2.addWidget(bx)
    row2.addWidget(_ql("°C"))
    row2.addStretch(1)
    page._left.addLayout(row2)
    page._left.addWidget(_instructions([
        "Note: Click on Cin, Cout to toggle in/out or use Menu Alt-S",
        "Rbias 留空由引擎按 Id Design Goal 综合；填入则按填入值算。"]))
    page._left.addStretch(1)
    page.dg = dg

    calc_b = QtWidgets.QPushButton("Calculate [F4]")
    calc_b.setObjectName("rfic_i.calc")
    page._right.addWidget(calc_b, 0, Qt.AlignLeft)
    rule, rlg = _grp("Design Rule (loss per blocking cap)")
    page.rule = QtWidgets.QComboBox()
    page.rule.setObjectName("rfic_i.rule.loss")
    page.rule.addItems(["0.01 dB", "0.1 dB (default)", "0.25 dB", "0.5 dB"])
    page.rule.setCurrentIndex(1)
    rlg.addWidget(page.rule)
    page._right.addWidget(rule, 0, Qt.AlignLeft)
    page._right.addSpacing(6)

    ttl = _ql("Id and Thermal Analysis:")
    fnt = QtGui.QFont()
    fnt.setBold(True)
    ttl.setFont(fnt)
    page._right.addWidget(ttl, 0, Qt.AlignLeft)
    page.table = _btable(["Tcse =", "Tj =", "Pd =", "Id max =", "Id typ =",
                          "Id min ="], "rfic_i.table",
                         ["-25", "25", "85", "unit"])
    page.table.setFixedWidth(330)
    page._right.addWidget(page.table, 0, Qt.AlignLeft)
    brow = QtWidgets.QHBoxLayout()
    brow.addWidget(_ql("Bias loss =", align_right=True))
    le = QtWidgets.QLineEdit()
    le.setReadOnly(True)
    le.setFixedWidth(70)
    le.setAlignment(Qt.AlignRight)
    le.setObjectName("rfic_i.result.biasloss")
    le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
    page.results["biasloss"] = le
    brow.addWidget(le)
    brow.addWidget(_ql("dB"))
    brow.addSpacing(16)
    brow.addWidget(_ql("Pd of Rbias =", align_right=True))
    le2 = QtWidgets.QLineEdit()
    le2.setReadOnly(True)
    le2.setFixedWidth(76)
    le2.setAlignment(Qt.AlignRight)
    le2.setObjectName("rfic_i.result.pdrbias")
    le2.setStyleSheet("background:%s;color:#555555;" % RO_BG)
    page.results["pdrbias"] = le2
    brow.addWidget(le2)
    brow.addWidget(_ql("mW"))
    brow.addStretch(1)
    page._right.addLayout(brow)
    page._right.addWidget(page.err, 0, Qt.AlignLeft)
    page._right.addStretch(1)

    def calc():
        eng = _e()
        f_low = _f(page.flow, "F low")
        z0 = _f(dg.fields["z0"], "Z0")
        rfc = _f(dg.fields["rfc"], "RFC")
        vs = _f(dg.fields["vs"], "Vs")
        tjc = _f(page.tjc, "θjc")
        dvdt = _f(page.dvdt, "∂Vd/∂T")
        vmax = _f(dg.fields["vd_max"], "V Max")
        vtyp = _f(dg.fields["vd_typ"], "V Typ")
        vmin = _f(dg.fields["vd_min"], "V Min")
        idg = _f(page.id_goal, "Id Design Goal")
        ts = [_f(page.t1, "Tcse1"), _f(page.t2, "Tcse2"), _f(page.t3, "Tcse3")]
        if f_low <= 0:
            raise _InputError("F low 必须大于 0")
        rb = None
        if dg.fields["rbias"].text().strip():
            rb = _f(dg.fields["rbias"], "Rbias")
            if rb <= 0:
                raise _InputError("Rbias 必须大于 0")
        cbp = _f(dg.fields["cb"], "Cb") if \
            dg.fields["cb"].text().strip() else None
        d = eng.rfic_current_bias(f_low * 1e6, z0, rfc * 1e-9, vs, tjc, dvdt,
                                  vmax, vtyp, vmin, idg, ts, rb, cbp)
        if isinstance(d, dict) and d.get("ok") is False:
            raise _InputError(d.get("error", "RFIC 电流偏置计算失败"))
        rows = d["rows"]
        for c in range(3):
            page.table.item(0, c + 1).setText("%.0f" % rows[c]["tcse"])
            page.table.item(1, c + 1).setText("%.1f" % rows[c]["tj_c"])
            page.table.item(2, c + 1).setText("%.1f" % rows[c]["pd_mw"])
            page.table.item(3, c + 1).setText("%.1f" % rows[c]["id_max_ma"])
            page.table.item(4, c + 1).setText("%.1f" % rows[c]["id_ma"])
            page.table.item(5, c + 1).setText("%.1f" % rows[c]["id_min_ma"])
        page.table.item(1, 4).setText("°C")
        page.table.item(2, 4).setText("mW")
        for r_ in (3, 4, 5):
            page.table.item(r_, 4).setText("mA")
        page.results["biasloss"].setText("%.2f" % d["bias_loss_db"])
        page.results["pdrbias"].setText("%.1f" % d["pd_rbias_mw"])
        if rb is None and d.get("rbias_ohm"):
            dg.fields["rbias"].setText("%.1f" % d["rbias_ohm"])
        for w in (d.get("warnings") or []):
            page.err.setText("⚠ " + str(w))
            page.err.show()
        return "Rbias=%.1f Ω  Bias loss=%.2f dB  Pd(Rbias)=%.1f mW" % (
            d["rbias_ohm"], d["bias_loss_db"], d["pd_rbias_mw"])

    calc_b.clicked.connect(lambda: _run_calc(page, calc))
    return page


# ---- 5) 大信号检波器（spec_ac2 §3）----------------------------------------

class _DgDetLarge(_Diagram):
    """Large Signal Detector：输入段框 + Rm + 二极管 + C + RI + Rg + Vo。"""

    NAME = "det_large"
    LW, LH = 700.0, 330.0
    OX, OY = 380.0, 100.0

    def draw(self, p):
        # 输入段框 (412,140)-(533,240)
        _poly(p, [(412, 140), (533, 140), (533, 240), (412, 240)], None)
        _line(p, 412, 140, 412, 240)
        _line(p, 412, 155, 412, 148)
        # 源
        _port_node(p, 412, 158, 10.0)
        _line(p, 412, 168, 412, 236)
        _gnd4(p, 412, 240)
        # Rm 竖直跨接 x=462
        _zig_v(p, 462, 145, 175, w=8.0, n=4)
        _line(p, 462, 140, 462, 145)
        _line(p, 462, 175, 462, 240)
        # 二极管 + 竖直虚线参考面
        _line(p, 540, 118, 540, 250, "#000000", 1.0, dash=(4, 3))
        _line(p, 533, 140, 549, 140)
        _diode(p, 549, 583, 140, h=15)
        _line(p, 583, 140, 648, 140)
        # 输出竖线 x=648
        _line(p, 648, 140, 648, 240)
        _port_node(p, 648, 150, 2.5, filled=True)
        # C 并 + 地
        _line(p, 648, 190, 587, 190)
        _cap_shunt(p, 587, 200)
        _line(p, 587, 205, 587, 216)
        _gnd4(p, 587, 216)
        # RI 竖直跨接
        _zig_v(p, 656, 172, 214, w=8.0, n=4)
        _line(p, 656, 150, 656, 172)
        _line(p, 656, 214, 656, 236)
        _gnd4(p, 656, 240)
        # Rg 竖直 + 地（二极管下方）
        _zig_v(p, 535, 172, 206, w=7.0, n=4)
        _line(p, 535, 162, 535, 172)
        _line(p, 535, 206, 535, 214)
        _gnd4(p, 535, 218)
        # Vo 弧箭头
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QtCore.QRectF(598, 178, 18, 22), 0, 120 * 16)
        _head(p, 616, 186, 0.8, -0.6)
        # Pin ->
        self.label(393, 236, "Pin ->")
        _arrow(p, 440, 236, 452, 236)
        self.label(612, 196, "Vo")
        self.label(410, 128, "Z0 =")
        self.label(475, 128, "Ω")
        self.label(503, 172, "Rm =")
        self.label(565, 172, "Ω")
        self.label(517, 190, "Rg =")
        self.label(612, 190, "Ω")
        self.label(662, 166, "RI =")
        self.label(716, 166, "Ω")
        self.label(248, 182, "C")


def _mk_det_large(win):
    page = _b_shell(win, "det_large", "大信号检波器 Large Signal Detector")
    page.setStyleSheet(_page_css("det_large", "#FDF2F3"))
    dg = _DgDetLarge()
    dg.box("z0", 410, 112, "50", w=47, obj="det_large.z0")
    dg.box("rm", 503, 160, "68", w=43, obj="det_large.rm")
    dg.box("rg", 550, 177, "", w=44, obj="det_large.rg")
    dg.box("ri", 652, 175, "5000", w=48, obj="det_large.ri")
    dg.finalize()
    page._left.addWidget(dg)

    gp, gl = _grp("Diode Parameters:")
    gp.setFixedWidth(300)
    r1 = QtWidgets.QHBoxLayout()
    page.t = _num_box("det_large.t", "25", 68)
    page.rs = _num_box("det_large.rs", "9", 50)
    r1.addWidget(_ql("T =", align_right=True))
    r1.addWidget(page.t)
    r1.addWidget(_ql("°C"))
    r1.addSpacing(20)
    r1.addWidget(_ql("Rs =", align_right=True))
    r1.addWidget(page.rs)
    r1.addWidget(_ql("Ω"))
    r1.addStretch(1)
    gl.addLayout(r1)
    r2 = QtWidgets.QHBoxLayout()
    page.io = _num_box("det_large.io", "500", 68)
    page.eg = _num_box("det_large.eg", "0.69", 46)
    r2.addWidget(_ql("Io =", align_right=True))
    r2.addWidget(page.io)
    r2.addWidget(_ql("uA"))
    r2.addSpacing(20)
    r2.addWidget(_ql("EG =", align_right=True))
    r2.addWidget(page.eg)
    r2.addWidget(_ql("eV"))
    r2.addStretch(1)
    gl.addLayout(r2)
    r3 = QtWidgets.QHBoxLayout()
    page.iso = _num_box("det_large.iso", "0.03", 63)
    page.n = _num_box("det_large.n", "1.08", 46)
    r3.addWidget(_ql("Iso =", align_right=True))
    r3.addWidget(page.iso)
    r3.addWidget(_ql("uA"))
    r3.addSpacing(20)
    r3.addWidget(_ql("n =", align_right=True))
    r3.addWidget(page.n)
    r3.addStretch(1)
    gl.addLayout(r3)
    page._left.addWidget(gp, 0, Qt.AlignLeft)

    steps = QtWidgets.QHBoxLayout()
    steps.addWidget(_ql("1. Enter diode parameters and circuit values :"))
    steps.addStretch(1)
    page._left.addLayout(steps)
    pr = QtWidgets.QHBoxLayout()
    pr.addWidget(_ql("2. Enter Pin range:"))
    page.pstart = _num_box("det_large.pin_start", "20", 50)
    page.pstop = _num_box("det_large.pin_stop", "30", 53)
    page.pstep = _num_box("det_large.pin_step", "5", 50)
    pr.addWidget(_ql("Pin Start ="))
    pr.addWidget(page.pstart)
    pr.addWidget(_ql("Pin Stop ="))
    pr.addWidget(page.pstop)
    pr.addWidget(_ql("Pin Step ="))
    pr.addWidget(page.pstep)
    pr.addWidget(_ql("dBm"))
    pr.addStretch(1)
    page._left.addLayout(pr)
    page._left.addStretch(1)
    page.dg = dg

    calc_b = QtWidgets.QPushButton("Calculate [F4]")
    calc_b.setObjectName("det_large.calc")
    page._right.addWidget(calc_b, 0, Qt.AlignLeft)
    for key, label, unit in (("lambda", "Λ =", ""), ("is", "Is =", "uA"),
                             ("vd0", "Vd(0) =", "V"), ("rv", "Rv =", "Ω")):
        row = QtWidgets.QHBoxLayout()
        row.addWidget(_ql(label, align_right=True))
        le = QtWidgets.QLineEdit()
        le.setReadOnly(True)
        le.setFixedWidth(70)
        le.setAlignment(Qt.AlignRight)
        le.setObjectName("det_large.result." + key)
        le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
        page.results[key] = le
        row.addWidget(le)
        if unit:
            row.addWidget(_ql(unit))
        row.addStretch(1)
        page._right.addLayout(row)
    page._right.addSpacing(8)
    b1 = QtWidgets.QPushButton("Plot Vout vs. Pin")
    b1.setObjectName("det_large.plot_vout")
    b2 = QtWidgets.QPushButton("Plot Gamma vs. Pin")
    b2.setObjectName("det_large.plot_gamma")
    prow = QtWidgets.QHBoxLayout()
    prow.addWidget(b1)
    prow.addWidget(b2)
    prow.addStretch(1)
    page._right.addLayout(prow)
    page.table = _scan_table(["Pin (dBm)", "Vout (V)", "Sens (mV/uW)"],
                              "det_large.table", height=230)
    page.table.setFixedWidth(300)
    page._right.addWidget(page.table, 0, Qt.AlignLeft)
    page._right.addWidget(page.err, 0, Qt.AlignLeft)
    page._right.addStretch(1)

    def calc():
        eng = _e()
        t = _f(page.t, "T")
        rs = _f(page.rs, "Rs")
        io = _f(page.io, "Io")
        eg = _f(page.eg, "EG")
        iso = _f(page.iso, "Iso")
        n = _f(page.n, "n")
        z0 = _f(dg.fields["z0"], "Z0")
        rm = _f(dg.fields["rm"], "Rm")
        ri = _f(dg.fields["ri"], "RI")
        rg = _f(dg.fields["rg"], "Rg") if dg.fields["rg"].text().strip() \
            else None
        p0 = _f(page.pstart, "Pin Start")
        p1 = _f(page.pstop, "Pin Stop")
        dp = _f(page.pstep, "Pin Step")
        # AppCAD 页标签 ↔ 引擎形参语义交换：Io(偏置)=iso_ua、Iso(饱和)=io_ua
        d = eng.detector_large_signal(t, rs, iso, eg, io, n, z0, rm, ri,
                                      rg, p0, p1, dp)
        if isinstance(d, dict) and d.get("ok") is False:
            raise _InputError(d.get("error", "检波器计算失败"))
        page.results["lambda"].setText("%.4f" % d["lambda_per_v"])
        page.results["is"].setText("%.4f" % d["is_ua"])
        page.results["vd0"].setText("%.4E" % d["vd0_v"])
        page.results["rv"].setText("%.3f" % d["rv_ohm"])
        # 表格固定为 −20…20 dBm 内部扫描（AppCAD 4.00 实测：与输入 Pin 范围无关，
        # 输入的 Pin Start/Stop/Step 只驱动 "Plot Vout vs. Pin"）
        dscan = eng.detector_large_signal(t, rs, iso, eg, io, n, z0, rm, ri,
                                          rg, -20.0, 20.0, 5.0)
        rows = (dscan.get("pins_dbm") if isinstance(dscan, dict) else None)             or d["pins_dbm"]

        def _sens(row):
            pw = 10.0 ** (row["pin_dbm"] / 10.0) * 1e-3      # mW
            vout = row["pout_mv"] / 1e3
            return (vout / pw * 1e3) if pw > 0 else 0.0

        _scan_fill(page.table, rows, [
            lambda row: "%.1f" % row["pin_dbm"],
            lambda row: "%.4f" % (row["pout_mv"] / 1e3),
            lambda row: "%.3f" % _sens(row)])
        for w in (d.get("warnings") or []):
            page.err.setText("⚠ " + str(w))
            page.err.show()
        return "Λ=%.4f  Rv=%.3f Ω  Vd(0)=%.4E V" % (
            d["lambda_per_v"], d["rv_ohm"], d["vd0_v"])

    calc_b.clicked.connect(lambda: _run_calc(page, calc))
    return page


# ---- 6) 小信号检波器（spec_ac2 §4：原理图在左、Diode Parameters 在右）----

class _DgDetSmall(_Diagram):
    """Small Signal Detector：左侧原理图（与大字页镜像布局）。"""

    NAME = "det_small"
    LW, LH = 430.0, 250.0
    OX, OY = 20.0, 130.0

    def draw(self, p):
        # 输入段框 (132,158)-(325,232)
        _poly(p, [(132, 158), (325, 158), (325, 232), (132, 232)], None)
        _port_node(p, 132, 205, 11.0)
        _line(p, 132, 216, 132, 228)
        _gnd4(p, 132, 230)
        _line(p, 132, 158, 132, 194)
        # Rm 竖直跨接 x=199（原图无 Rm 文字标注）
        _zig_v(p, 199, 186, 216, w=7.0, n=4)
        _line(p, 199, 158, 199, 186)
        _line(p, 199, 216, 199, 232)
        # 二极管 D 跨接上边
        _line(p, 210, 158, 226, 158)
        _diode(p, 226, 258, 158, h=14)
        _line(p, 258, 158, 290, 158)
        # 输出竖线 x=290
        _line(p, 290, 158, 290, 232)
        _port_node(p, 290, 164, 2.5, filled=True)
        _line(p, 290, 196, 262, 196)
        _cap_shunt(p, 262, 204)
        _line(p, 262, 209, 262, 216)
        _gnd4(p, 262, 218)
        _zig_v(p, 297, 176, 214, w=7.0, n=4)
        _line(p, 297, 158, 297, 176)
        _line(p, 297, 214, 297, 228)
        _gnd4(p, 297, 230)
        _pen(p, "#000000", 1.0)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QtCore.QRectF(300, 196, 18, 22), 0, 120 * 16)
        _head(p, 318, 204, 0.8, -0.6)
        self.label(160, 244, "Pin ->")
        _arrow(p, 202, 240, 212, 240)
        self.label(310, 214, "Vo")
        self.label(248, 190, "C")
        self.label(29, 194, "Z0 =")
        self.label(124, 194, "Ω")
        self.label(336, 186, "RI =")
        self.label(396, 186, "Ω")


def _mk_det_small(win):
    page = _b_shell(win, "det_small", "小信号检波器 Small Signal Detector")
    page.setStyleSheet(_page_css("det_small", "#FDF2F3"))
    dg = _DgDetSmall()
    dg.box("z0", 60, 177, "50", w=45, obj="det_small.z0")
    dg.box("ri", 330, 200, "10000", w=48, obj="det_small.ri")
    dg.finalize()
    page._left.addWidget(dg)
    page._left.addWidget(_ql("1. Enter diode parameters and circuit values :"))
    pr = QtWidgets.QHBoxLayout()
    pr.addWidget(_ql("2. Enter Pin range:"))
    page.pstart = _num_box("det_small.pin_start", "50", 50)
    page.pstop = _num_box("det_small.pin_stop", "0", 50)
    page.pstep = _num_box("det_small.pin_step", "-2", 50)
    pr.addWidget(_ql("Pin Start ="))
    pr.addWidget(page.pstart)
    pr.addWidget(_ql("Pin Stop ="))
    pr.addWidget(page.pstop)
    pr.addWidget(_ql("Pin Step ="))
    pr.addWidget(page.pstep)
    pr.addWidget(_ql("dBm"))
    pr.addStretch(1)
    page._left.addLayout(pr)
    page._left.addStretch(1)
    page.dg = dg

    # 右列：Diode Parameters 组（镜像布局）+ 结果
    gp, gl = _grp("Diode Parameters:")
    gp.setFixedWidth(260)
    r1 = QtWidgets.QHBoxLayout()
    page.t = _num_box("det_small.t", "25", 54)
    page.rs = _num_box("det_small.rs", "6", 50)
    r1.addWidget(_ql("T =", align_right=True))
    r1.addWidget(page.t)
    r1.addWidget(_ql("°C"))
    r1.addSpacing(14)
    r1.addWidget(_ql("Rs =", align_right=True))
    r1.addWidget(page.rs)
    r1.addWidget(_ql("Ω"))
    r1.addStretch(1)
    gl.addLayout(r1)
    r2 = QtWidgets.QHBoxLayout()
    page.io = _num_box("det_small.io", "0", 54)
    page.eg = _num_box("det_small.eg", "0.35", 50)
    r2.addWidget(_ql("Io =", align_right=True))
    r2.addWidget(page.io)
    r2.addWidget(_ql("uA"))
    r2.addSpacing(14)
    r2.addWidget(_ql("EG =", align_right=True))
    r2.addWidget(page.eg)
    r2.addWidget(_ql("eV"))
    r2.addStretch(1)
    gl.addLayout(r2)
    r3 = QtWidgets.QHBoxLayout()
    page.iso = _num_box("det_small.iso", "0.06", 54)
    page.n = _num_box("det_small.n", "1.15", 50)
    r3.addWidget(_ql("Iso =", align_right=True))
    r3.addWidget(page.iso)
    r3.addWidget(_ql("uA"))
    r3.addSpacing(14)
    r3.addWidget(_ql("n =", align_right=True))
    r3.addWidget(page.n)
    r3.addStretch(1)
    gl.addLayout(r3)
    page._right.addWidget(gp, 0, Qt.AlignLeft)

    calc_b = QtWidgets.QPushButton("Calculate [F4]")
    calc_b.setObjectName("det_small.calc")
    page._right.addWidget(calc_b, 0, Qt.AlignLeft)
    for key, label, unit in (("lambda", "Λ =", ""), ("is", "Is =", "uA"),
                             ("vo0", "Vo(0) =", "V"), ("rv", "Rv =", "Ω")):
        row = QtWidgets.QHBoxLayout()
        row.addWidget(_ql(label, align_right=True))
        le = QtWidgets.QLineEdit()
        le.setReadOnly(True)
        le.setFixedWidth(72)
        le.setAlignment(Qt.AlignRight)
        le.setObjectName("det_small.result." + key)
        le.setStyleSheet("background:%s;color:#555555;" % RO_BG)
        page.results[key] = le
        row.addWidget(le)
        if unit:
            row.addWidget(_ql(unit))
        row.addStretch(1)
        page._right.addLayout(row)
    page._right.addSpacing(8)
    b1 = QtWidgets.QPushButton("Plot Vout vs. Pin")
    b1.setObjectName("det_small.plot_vout")
    b2 = QtWidgets.QPushButton("Plot Gamma vs. Pin")
    b2.setObjectName("det_small.plot_gamma")
    prow = QtWidgets.QHBoxLayout()
    prow.addWidget(b1)
    prow.addWidget(b2)
    prow.addStretch(1)
    page._right.addLayout(prow)
    page.table = _scan_table(["Pin (dBm)", "Vout (V)", "Sens (mV/uW)"],
                              "det_small.table", height=230)
    page.table.setFixedWidth(300)
    page._right.addWidget(page.table, 0, Qt.AlignLeft)
    page._right.addWidget(page.err, 0, Qt.AlignLeft)
    page._right.addStretch(1)

    def calc():
        eng = _e()
        t = _f(page.t, "T")
        rs = _f(page.rs, "Rs")
        io = _f(page.io, "Io")
        eg = _f(page.eg, "EG")
        iso = _f(page.iso, "Iso")
        n = _f(page.n, "n")
        z0 = _f(dg.fields["z0"], "Z0")
        ri = _f(dg.fields["ri"], "RI")
        p0 = _f(page.pstart, "Pin Start")
        p1 = _f(page.pstop, "Pin Stop")
        dp = _f(page.pstep, "Pin Step")
        # AppCAD 页标签 ↔ 引擎形参语义交换（Iso=饱和 → io_ua，Io=偏置 → iso_ua）
        d = eng.detector_small_signal(t, rs, iso, eg, io, n, z0, 68.0, ri,
                                      None, p0, p1, dp)
        if isinstance(d, dict) and d.get("ok") is False:
            raise _InputError(d.get("error", "检波器计算失败"))
        page.results["lambda"].setText("%.4f" % d["lambda_per_v"])
        page.results["is"].setText("%.4f" % d["is_ua"])
        page.results["vo0"].setText("%.4E" % d["vo0_v"])
        page.results["rv"].setText("%.3f" % d["rv_ohm"])
        # 表格固定为 −50…0 dBm 内部扫描（AppCAD 4.00 实测）
        dscan = eng.detector_small_signal(t, rs, iso, eg, io, n, z0, 68.0, ri,
                                          None, -50.0, 0.0, 2.0)
        rows = (dscan.get("pins_dbm") if isinstance(dscan, dict) else None)             or d["pins_dbm"]

        def _sens(row):
            pw = 10.0 ** (row["pin_dbm"] / 10.0) * 1e-3
            vout = row["pout_mv"] / 1e3
            return (vout / pw * 1e3) if pw > 0 else 0.0

        _scan_fill(page.table, rows, [
            lambda row: "%.1f" % row["pin_dbm"],
            lambda row: "%.6f" % (row["pout_mv"] / 1e3),
            lambda row: "%.3f" % _sens(row)])
        for w in (d.get("warnings") or []):
            page.err.setText("⚠ " + str(w))
            page.err.show()
        return "Λ=%.4f  Rv=%.3f Ω  Vo(0)=%.4E V" % (
            d["lambda_per_v"], d["rv_ohm"], d["vo0_v"])

    calc_b.clicked.connect(lambda: _run_calc(page, calc))
    return page


_GROUPS = [
    ("tl", "传输线", [
        ("microstrip", "微带线", "microstrip"),
        ("stripline", "带状线", "stripline"),
        ("twin_wire", "平行双线", "twin"),
        ("wire_gnd", "线过地板", "wire_gnd"),
        ("coax_round", "同轴(圆)", "coax_round"),
        ("coax_square", "同轴(方)", "coax_square"),
        ("cpw", "CPW", "cpw"),
        ("trough", "板线槽线", "trough"),
    ]),
    ("active", "有源电路", [
        ("rfic_v", "RFIC电压偏置", "pae"),
        ("rfic_i", "RFIC电流偏置", "pae"),
        ("det_large", "大信号检波器", "grid"),
        ("det_small", "小信号检波器", "grid"),
        ("bjt_base", "BJT固定偏置", "bjt"),
        ("bjt", "BJT分压偏置", "bjt"),
        ("fet_self", "FET自偏置", "fet"),
        ("fet_div", "FET分压偏置", "fet"),
        ("pae", "放大器效率", "pae"),
    ]),
    ("sig", "信号与系统", [
        ("sparam", "S参数(Smith)", "refl"),
        ("balun", "集总元件巴伦", "im"),
        ("reflection", "反射计算器", "refl"),
        ("noisecalc", "NoiseCalc", "grid"),
        ("spurs", "混频器杂散", "spur"),
        ("intermod", "互调计算器", "im"),
    ]),
    ("rel", "可靠性", [
        ("thermal", "器件热设计", "thermal"),
        ("spcs", "SPCS", "spcs"),
    ]),
    ("tools", "工程工具", [
        ("comp_values", "标准元件值", "subs"),
        ("constants", "工程常数", "subs"),
        ("complex_math", "复数计算器", "subs"),
        ("substrate", "基片库", "subs"),
    ]),
]


class _OutlookBar(QtWidgets.QWidget):
    """宽 ~115px：顶部=展开组的图标项（自绘 32px 缩略图），底部=分组条小钮。"""

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._expanded = _GROUPS[0][0]
        self.setObjectName("sidebar")
        self.setFixedWidth(118)
        self.setStyleSheet(
            " QWidget#sidebar { background: #E8E8E8; }"
            " QWidget#sidebar QPushButton { background: #FFFFFF;"
            "   border: 1px solid #808080; border-top-color: #FFFFFF;"
            "   border-left-color: #FFFFFF; font-size: 8pt; padding: 3px; }"
            " QWidget#sidebar QToolButton { border: 1px solid transparent;"
            "   font-size: 8pt; padding: 2px; }"
            " QWidget#sidebar QToolButton:hover { background: #DFE8F6; }"
            " QWidget#sidebar QToolButton:checked { background: #CCE0F5; }")
        self._items = {}
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._inner = None
        self.rebuild()

    def expand(self, key):
        self._expanded = key
        self.rebuild()

    def sync(self, page_name):
        for key, btn in self._items.items():
            btn.setChecked(key == page_name)

    def rebuild(self):
        # 重建 = 换内部容器（removeWidget 立即摘除旧容器；deleteLater 延迟销毁）
        if self._inner is not None:
            old = self._inner
            self.layout().removeWidget(old)
            old.hide()
            old.deleteLater()
        self._items = {}
        inner = QtWidgets.QWidget()
        inner.setObjectName("sidebar.inner")
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        for key, label, members in _GROUPS:
            if key == self._expanded:
                for page_key, text, icon_kind in members:
                    tb = QtWidgets.QToolButton()
                    tb.setText(text)
                    tb.setIcon(_icon(icon_kind, 32))
                    tb.setIconSize(QtCore.QSize(32, 32))
                    tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                    tb.setCheckable(True)
                    tb.setObjectName("sidebar.item." + page_key)
                    tb.clicked.connect(lambda _c=False, k=page_key:
                                       self._win.show_page(k))
                    lay.addWidget(tb)
                    self._items[page_key] = tb
        lay.addStretch(1)
        for key, label, _members in _GROUPS:
            b = QtWidgets.QPushButton(label)
            b.setObjectName("sidebar.group." + key)
            b.clicked.connect(lambda _c=False, k=key: self.expand(k))
            lay.addWidget(b)
        self._inner = inner
        self.layout().addWidget(inner)


# ---------------------------------------------------------------------------
# Web 条（白底凸起 + 蓝字，文案 1:1 英文保留）
# ---------------------------------------------------------------------------

class _WebBar(QtWidgets.QFrame):
    def __init__(self, win):
        super().__init__()
        self.setObjectName("webbar")
        self.setFixedHeight(24)
        self.setStyleSheet(
            " QFrame#webbar { background: #FFFFFF; border: 1px solid #808080;"
            "   border-top-color: #FFFFFF; border-left-color: #FFFFFF; }"
            " QFrame#webbar QLabel { color: %s; border: none; }" % WEBBLUE)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(2, 1, 2, 1)
        h.addStretch(1)
        self._lab = QtWidgets.QLabel("Click for Web: APPLICATION NOTES · MODELS · "
                                     "DESIGN TIPS · DATA SHEETS · S-PARAMETERS")
        h.addWidget(self._lab)
        h.addStretch(1)

    def mousePressEvent(self, ev):
        w = self.window()
        if w is not None:
            w.status().showMessage("离线版：无外部链接（clean-room 复刻，不含联网）")
        super().mousePressEvent(ev)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class RfCalcWindow(QtWidgets.QMainWindow):
    """RF 计算器主窗口：Outlook 侧栏 + 页栈 + Web 条 + 状态栏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        if parent is not None:
            self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle("RF 计算器（AppCAD 风格）")
        self.resize(1000, 700)
        self._pages = {}
        self._order = []
        self._last_sub = None

        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(0)
        self.sidebar = _OutlookBar(self)
        self._stack = QtWidgets.QStackedWidget()
        self._stack.setObjectName("pagestack")
        body.addWidget(self.sidebar)
        body.addWidget(self._stack, 1)
        v.addLayout(body, 1)
        v.addWidget(_WebBar(self))
        self.setCentralWidget(central)

        # 页面构建（顺序 = 侧栏分组顺序）
        _mk_microstrip(self)
        _mk_stripline(self)
        _mk_twin(self)
        _mk_wire_gnd(self)
        _mk_coax_round(self)
        _mk_coax_square(self)
        _mk_cpw(self)
        _mk_trough(self)
        _mk_rfic_v(self)
        _mk_rfic_i(self)
        _mk_det_large(self)
        _mk_det_small(self)
        _mk_bjt_base(self)
        _mk_bjt(self)
        _mk_fet(self, "fet_self", "FET 自偏置 FET Bias - Source Resistor (self bias)",
                divider=False)
        _mk_fet(self, "fet_div", "FET 分压偏置 FET Bias - Divider", divider=True)
        _mk_pae(self)
        _mk_sparam(self)
        _mk_balun(self)
        _mk_reflection(self)
        _mk_noisecalc(self)
        _mk_spurs(self)
        _mk_intermod(self)
        _mk_thermal(self)
        _mk_spcs(self)
        _mk_comp_values(self)
        _mk_constants(self)
        _mk_complex_math(self)
        _mk_substrate(self)

        self._shortcuts = []
        self._shortcut("F4", self._f4)
        self._shortcut("F3", self._f3)
        self._shortcut("F8", self.toggle_sidebar)
        self.show_page("microstrip")

    # -- 基础设施 ------------------------------------------------------------
    def status(self):
        return self.statusBar()

    def _shortcut(self, key, cb):
        cls = getattr(QtWidgets, "QShortcut", None) or getattr(QtGui, "QShortcut", None)
        if cls is None:
            return
        sc = cls(QtGui.QKeySequence(key), self)
        sc.activated.connect(cb)
        self._shortcuts.append(sc)

    def _cur_page(self):
        return self._stack.currentWidget()

    def _primary_button(self, page):
        return page.findChild(QtWidgets.QPushButton, page.name + ".calc")

    def _secondary_button(self, page):
        return page.findChild(QtWidgets.QPushButton, page.name + ".calc2")

    def _f4(self):
        b = self._primary_button(self._cur_page())
        if b is not None and b.isVisible():
            b.click()
        elif b is not None:
            b.click()

    def _f3(self):
        b = self._secondary_button(self._cur_page())
        if b is not None:
            b.click()

    def toggle_sidebar(self):
        self.sidebar.setVisible(not self.sidebar.isVisible())

    def show_page(self, name):
        page = self._pages.get(name)
        if page is None:
            return
        self._stack.setCurrentWidget(page)
        self.sidebar.sync(name)
        self.status().showMessage("RF 计算器 — %s" % name, 4000)

    def _apply_substrate(self, sub):
        self._last_sub = sub
        page = self._cur_page()
        er = getattr(page, "er_box", None)
        if er is not None:
            er.setText(_g(sub["er"]))
            self.status().showMessage("基片 %s：εr=%s 已回填 %s 页"
                                      % (sub["name"], _g(sub["er"]), page.name), 8000)
        else:
            self.status().showMessage("基片 %s 已记录（当前页无 εr 框）" % sub["name"],
                                      8000)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ---------------------------------------------------------------------------
# 公开 API：单例
# ---------------------------------------------------------------------------

_WINDOW = None


def open_rfcalc_panel(parent=None):
    """创建/复用 RF 计算器窗口；已存在则 show + raise + activate。失败返回 None。"""
    global _WINDOW
    if _WINDOW is not None:
        _WINDOW.show()
        _WINDOW.raise_()
        _WINDOW.activateWindow()
        return _WINDOW
    if _E() is None:
        _warn(parent, "RF 计算器", "引擎 backend/rfcalc.py 加载失败，无法打开窗口")
        return None
    try:
        win = RfCalcWindow(parent)
    except Exception as exc:  # UI 构建兜底
        _warn(parent, "RF 计算器", "窗口构建失败：%s: %s" % (type(exc).__name__, exc))
        return None
    _WINDOW = win
    win.show()
    return win


# ---------------------------------------------------------------------------
# 独立预览 / offscreen 渲染取证
# ---------------------------------------------------------------------------

def _snapshot_main(outdir):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication([])
    win = open_rfcalc_panel(None)
    if win is None:
        print("SNAP FAILED: window build failed")
        return 1
    win.resize(1000, 720)
    win.show()
    app.processEvents()
    app.processEvents()
    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)
    saved = []
    for name in win._order:
        page = win._pages[name]
        win._stack.setCurrentWidget(page)
        app.processEvents()
        app.processEvents()
        try:
            pm = page.grab()
            path = os.path.join(outdir, name + ".png")
            ok = pm.save(path)
            saved.append((name, path, ok))
        except Exception as exc:
            saved.append((name, "ERROR %s: %s" % (type(exc).__name__, exc), False))
    # 整窗取证一张（含 Outlook 侧栏 + Web 条）
    win.show_page("microstrip")
    app.processEvents()
    try:
        pm = win.grab()
        path = os.path.join(outdir, "_window_sidebar.png")
        saved.append(("window", path, pm.save(path)))
    except Exception as exc:
        saved.append(("window", "ERROR %s: %s" % (type(exc).__name__, exc), False))
    win.hide()
    for name, path, ok in saved:
        print(("OK  " if ok else "FAIL") + " " + name + " -> " + str(path))
    return 0 if all(ok for _n, _p, ok in saved) else 2


if __name__ == "__main__":
    if "--snap" in sys.argv:
        i = sys.argv.index("--snap")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "repl"
        sys.exit(_snapshot_main(out))
    app = QtWidgets.QApplication(sys.argv)
    w = open_rfcalc_panel(None)
    if w is not None:
        exec_fn = getattr(qtcompat, "dialog_exec", None)
        if exec_fn is not None:
            exec_fn(app)
        else:
            app.exec()
