# -*- coding: utf-8 -*-
"""AppCAD 1:1 spec renderer.

A *page spec* is plain measured data (pixels lifted off the real AppCAD 4.00
screenshots in ``tmp/appcad/pages``) plus a small renderer.  Nothing here
guesses geometry: every polygon, label and input box comes from measurement.

Schema
------
{
  "name":   "microstrip",
  "size":   [966, 669],            # reference canvas, 1 spec unit = 1 px
  "bg":     "#FFFFC0",
  "shapes": [                      # painted in order (z = list order)
    {"k":"poly",    "pts":[[x,y],...], "fill":"#CCCC99", "edge":"#000000", "w":1},
    {"k":"line",    "pts":[[x,y],[x,y]], "edge":"#000000", "w":1, "dash":[4,2]},
    {"k":"rect",    "xywh":[x,y,w,h],  "fill":..., "edge":..., "w":1},
    {"k":"ellipse", "xywh":[x,y,w,h],  "fill":..., "edge":..., "w":1},
    {"k":"arc",     "xywh":[x,y,w,h], "start":deg, "span":deg, "edge":..., "w":1},
  ],
  "labels": [{"xy":[x,y], "text":"W =", "font":"MS Sans Serif", "pt":8,
              "bold":false, "italic":false, "color":"#000000",
              "align":"left"|"right"|"center", "baseline":true}],
  "boxes":  [{"key":"W", "xywh":[x,y,w,h], "value":"12", "ro":false,
              "align":"right"|"left"|"center"}],
}

``labels[].xy`` is the *baseline left* point by default (``baseline`` true),
which is how text is measured on the reference bitmaps.
"""
from __future__ import annotations

try:
    from . import qtcompat  # noqa: F401
except ImportError:  # 脚本/测试直接运行
    import qtcompat  # noqa: F401

QtCore = qtcompat.QtCore()
QtGui = qtcompat.QtGui()
QtWidgets = qtcompat.QtWidgets()

QPointF = QtCore.QPointF
QRectF = QtCore.QRectF


def _color(spec, default=None):
    if not spec:
        return QtCore.Qt.transparent if default is None else QtGui.QColor(default)
    return QtGui.QColor(spec)


def font_for(label, zoom=1.0):
    f = QtGui.QFont(label.get("font", "MS Sans Serif"))
    f.setPointSizeF(float(label.get("pt", 8)) * zoom)
    f.setBold(bool(label.get("bold", False)))
    f.setItalic(bool(label.get("italic", False)))
    # Windows 显示缩放下 PreferDefault 会退化成灰度点阵→糊；强制抗锯齿光栅。
    f.setStyleStrategy(QtGui.QFont.PreferAntialias)
    return f


def paint_spec(p: QtGui.QPainter, spec: dict, zoom: float = 1.0,
               skip_boxes: bool = True):
    """Paint shapes+labels of *spec*.  Input boxes are real widgets, so the
    white well of a box is only painted when ``skip_boxes`` is False (used by
    the off-screen verification renderer)."""
    p.save()
    p.scale(zoom, zoom)
    p.setRenderHint(QtGui.QPainter.Antialiasing, False)
    for sh in spec.get("shapes", []):
        k = sh["k"]
        pen = QtGui.QPen(_color(sh.get("edge", "#000000")))
        pen.setWidthF(float(sh.get("w", 1)))
        if sh.get("dash"):
            pen.setDashPattern(sh["dash"])
        if not sh.get("edge"):
            pen = QtCore.Qt.NoPen
        p.setPen(pen)
        fill = sh.get("fill")
        p.setBrush(QtGui.QBrush(_color(fill)) if fill else QtCore.Qt.NoBrush)
        if k in ("poly", "line"):
            pts = [QPointF(float(x), float(y)) for x, y in sh["pts"]]
            if k == "poly":
                p.drawPolygon(pts)
            else:
                for a, b in zip(pts, pts[1:]):
                    p.drawLine(a, b)
        elif k == "rect":
            p.drawRect(QRectF(*[float(v) for v in sh["xywh"]]))
        elif k == "ellipse":
            p.drawEllipse(QRectF(*[float(v) for v in sh["xywh"]]))
        elif k == "arc":
            p.drawArc(QRectF(*[float(v) for v in sh["xywh"]]),
                      int(sh.get("start", 0) * 16), int(sh.get("span", 90) * 16))
    # 形状保持 Antialiasing=False（矢量碎片按实机像素硬边），文字单独开
    # TextAntialiasing：Windows 显示缩放下亚像素定位+抗锯齿，避免字形发糊。
    p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
    for lb in spec.get("labels", []):
        p.setPen(QtGui.QPen(_color(lb.get("color", "#000000"))))
        p.setFont(font_for(lb))
        # 坐标整数化：半像素偏移会在光栅化时把字形劈成两列灰像素（糊/重影）
        x, y = (float(v) for v in lb["xy"])
        text = lb["text"]
        fm = QtGui.QFontMetricsF(p.font())
        w = fm.horizontalAdvance(text)
        align = lb.get("align", "left")
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2.0
        if lb.get("baseline", True):
            p.drawText(QPointF(round(x), round(y)), text)
        else:
            p.drawText(QRectF(round(x), round(y), w + 4, fm.height() + 2),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop, text)
    if not skip_boxes:
        for bx in spec.get("boxes", []):
            x, y, w, h = (float(v) for v in bx["xywh"])
            p.setPen(QtGui.QPen(QtGui.QColor("#646464")))
            p.setBrush(QtGui.QColor("#FFFFFF"))
            p.drawRect(QRectF(x - 1, y - 1, w + 2, h + 2))
    p.restore()


class SpecDiagram(QtWidgets.QWidget):
    """A QWidget that reproduces one measured AppCAD drawing 1:1.

    ``fields`` maps box key -> QLineEdit so the calculator pages can keep
    using their existing field names.
    """

    def __init__(self, spec: dict, parent=None, zoom: float = 1.0):
        super().__init__(parent)
        self.spec = spec
        self.zoom = zoom
        w, h = spec.get("size", [966, 669])
        self._lw, self._lh = float(w), float(h)
        self.fields: dict[str, QtWidgets.QLineEdit] = {}
        # 与手绘 _Diagram 同名接口：标注列表一次性来自规格，重绘不会累积
        self._labels = list(spec.get("labels", []))
        self.setAttribute(QtCore.Qt.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setFixedSize(int(round(self._lw * zoom)), int(round(self._lh * zoom)))
        for bx in spec.get("boxes", []):
            key = bx.get("key")
            if not key:
                continue
            e = QtWidgets.QLineEdit(self)
            e.setText(str(bx.get("value", "")))
            e.setReadOnly(bool(bx.get("ro", False)))
            e.setAlignment({"right": QtCore.Qt.AlignRight,
                            "center": QtCore.Qt.AlignCenter}.get(
                                bx.get("align", "right"), QtCore.Qt.AlignRight))
            f = QtGui.QFont("MS Sans Serif")
            f.setPointSizeF(8.0 * zoom)
            e.setFont(f)
            e.setStyleSheet(
                "QLineEdit{background:#FFFFFF;border:1px solid #646464;"
                "padding:0px 2px;border-radius:0px;}"
                "QLineEdit:read-only{background:#EFEFEF;}")
            x, y, w, h = (float(v) for v in bx["xywh"])
            e.setGeometry(int(round((x - 1) * zoom)), int(round((y - 1) * zoom)),
                          int(round((w + 2) * zoom)), int(round((h + 2) * zoom)))
            self.fields[key] = e

    def paintEvent(self, ev):
        # 高 DPI：先按 devicePixelRatio 渲染到高分辨率 QImage 再落回控件，
        # 保证 Windows 显示缩放（125%/150%）下文字与矢量都以设备像素光栅化，
        # 不经任何中间 1x 位图放大（那是「糊」的来源之一）。
        dpr = float(self.devicePixelRatioF())
        img = QtGui.QImage(int(round(self.width() * dpr)),
                           int(round(self.height() * dpr)),
                           QtGui.QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(QtGui.QColor(self.spec.get("bg", "#FFFFFF")))
        p = QtGui.QPainter(img)
        paint_spec(p, self.spec, self.zoom)
        p.end()
        q = QtGui.QPainter(self)
        q.drawImage(self.rect(), img)
        q.end()


class SpecPage(SpecDiagram):
    """A whole AppCAD page as an absolutely positioned 966x669 canvas.

    AppCAD's forms are fixed-position VB6 layouts, so Qt layout managers are
    the wrong tool here: they are exactly why earlier attempts drifted.  Every
    control is instead placed at its measured pixel rectangle with
    :meth:`place`, and the measured drawing sits underneath everything.
    """

    def __init__(self, spec: dict, parent=None, zoom: float = 1.0):
        super().__init__(spec, parent, zoom)
        self._placed = []

    def place(self, w, x, y, width=None, height=None):
        """Put *w* at design coordinates (x, y); size is measured too unless
        omitted, in which case the widget's own sizeHint is used."""
        w.setParent(self)
        hint = w.sizeHint()
        width = hint.width() if width is None else width
        height = hint.height() if height is None else height
        w.setGeometry(int(round(x * self.zoom)), int(round(y * self.zoom)),
                      int(round(width * self.zoom)), int(round(height * self.zoom)))
        w.show()
        self._placed.append(w)
        return w
