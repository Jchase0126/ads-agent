"""设计结果页（PySide6）—— 真实曲线 + 指标判定 + 设计引用 + 重新仿真入口。

这一页是"设计闭环"的交付物，不是截图：曲线由 ``.ds`` 数据集里的真实数组
用 QPainter 画出来，指标表里的实测值来自确定性评估器（``backend/design_metrics``），
判定与频点一一对应，并且数据点可以逐点查看。

内容（对应需求里的五项）：
1. 设计引用：工作区 / 库 / cell / view + 「在 ADS 中打开原理图」按钮
2. 曲线：横纵轴名称、单位、数据来源（.ds 路径）都标在图上
3. 指标表：目标值 / 实测值 / 判定 / 对应频点
4. 仿真状态、错误信息、输出目录、重新仿真入口
5. 页面本身只是 ``job`` 字典的渲染 —— 面板把 job 存进项目，重启后重新渲染即可

刻意**不**依赖 pyqtgraph / matplotlib：本插件跑在 ADS 进程里，多一个第三方
依赖就多一层装不上的风险；曲线用 QPainter 自绘，离线平台（offscreen）也能测。
"""

from PySide6.QtCore import Qt, QRect, QSize, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import uiscale as U

# 曲线配色：在浅色与深色卡片上都能看清
CURVE_COLORS = ("#1a73e8", "#e8710a", "#0f9d76", "#d93025", "#7b1fa2", "#00838f")

VERDICT_STYLE = {
    "pass": ("全部指标达标", "#16856b"),
    "partial": ("部分指标达标", "#b47719"),
    "fail": ("指标未达标", "#c43d4b"),
    "unknown": ("等待指标评估", "#65758b"),
}

STAGE_STYLE = {
    "failed": "#d93025",
    "done": "#0f9d76",
    "evaluated": "#1a73e8",
    "simulating": "#e8710a",
}


def _fmt(v, digits: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return "NaN"
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    return f"{f:.{digits}g}"


def _hz(value) -> str:
    """频率数值 -> 人能一眼读懂的单位（展示用，不做任何判定）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(v) >= scale:
            return f"{v / scale:g}{unit}"
    return f"{v:g}Hz"


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list:
    """给坐标轴挑一组整齐的刻度值。"""
    if not (hi > lo):
        return [lo]
    raw = (hi - lo) / max(1, count)
    mag = 10 ** int(_floor_log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw:
            break
    else:
        step = mag * 10
    start = step * int(lo / step)
    out = []
    v = start
    while v <= hi + step * 0.001 and len(out) < 40:
        if v >= lo - step * 0.001:
            out.append(v)
        v += step
    return out or [lo, hi]


def _floor_log10(v: float) -> float:
    import math

    if v <= 0:
        return 0.0
    return math.floor(math.log10(v))


# ---------------------------------------------------------------------------
# 曲线图
# ---------------------------------------------------------------------------

class CurveChart(QWidget):
    """自绘多曲线图：坐标轴 + 刻度 + 单位 + 图例 + 悬停查点。

    数据来源标注在图下方，横纵轴都带名称与单位 —— 没有单位时明确写
    "单位未声明"，而不是留空让人误以为是已知的。
    """

    def __init__(self, traces: dict, pal: dict, parent=None):
        super().__init__(parent)
        self._pal = pal
        self._traces = []
        self._hover = None
        self.setMinimumHeight(U.px(210))
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_traces(traces)

    # -- 数据 ---------------------------------------------------------------
    def set_traces(self, traces: dict) -> None:
        self._traces = []
        for i, (name, t) in enumerate((traces or {}).items()):
            xs = [float(v) for v in (t.get("x") or []) if isinstance(v, (int, float))]
            ys = [float(v) for v in (t.get("y") or []) if isinstance(v, (int, float))]
            n = min(len(xs), len(ys))
            if n < 2:
                continue
            self._traces.append({
                "name": name,
                "label": t.get("y_name") or name,
                "x": xs[:n],
                "y": ys[:n],
                "x_unit": t.get("x_unit") or "",
                "x_name": t.get("x_name") or "freq",
                "y_unit": t.get("y_unit") or "",
                "color": CURVE_COLORS[i % len(CURVE_COLORS)],
                "source": t.get("source") or "",
                "n_points": t.get("n_points") or n,
            })
        self._hover = None
        self.setFixedHeight(U.px(230 + len(self._traces) * 18))
        self.update()

    def has_curves(self) -> bool:
        return bool(self._traces)

    # -- 几何 ---------------------------------------------------------------
    def _plot_rect(self) -> QRect:
        top = U.px(14 + len(self._traces) * 18)
        return QRect(
            U.px(56), top,
            max(self.width() - U.px(56) - U.px(14), U.px(40)),
            max(self.height() - top - U.px(38), U.px(40)),
        )

    def _ranges(self):
        xs = [v for t in self._traces for v in t["x"]]
        ys = [v for t in self._traces for v in t["y"]]
        if not xs or not ys:
            return None
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 <= x0:
            x1 = x0 + 1
        if y1 <= y0:
            pad = abs(y0) * 0.1 or 1.0
            y0, y1 = y0 - pad, y1 + pad
        else:
            pad = (y1 - y0) * 0.08
            y0, y1 = y0 - pad, y1 + pad
        return x0, x1, y0, y1

    def _map(self, rect: QRect, rng, xv, yv) -> QPointF:
        x0, x1, y0, y1 = rng
        px = rect.left() + (xv - x0) / (x1 - x0) * rect.width()
        py = rect.bottom() - (yv - y0) / (y1 - y0) * rect.height()
        return QPointF(px, py)

    # -- 绘制 ---------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        pal = self._pal
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self._plot_rect()

        painter.fillRect(self.rect(), QColor(pal["card_bg"]))
        painter.setPen(QPen(QColor(pal["card_border"]), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1),
                                U.R("md"), U.R("md"))

        rng = self._ranges()
        if rng is None:
            painter.setPen(QColor(pal["subtle"]))
            painter.setFont(U.qfont("small"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "暂无曲线数据")
            painter.end()
            return

        x0, x1, y0, y1 = rng
        x_unit = self._traces[0]["x_unit"]
        x_name = self._traces[0]["x_name"]
        x_label = f"{x_name}" + (f" ({x_unit})" if x_unit else "（单位未声明）")

        # 网格 + 刻度
        painter.setFont(U.qfont("micro"))
        for tick in _nice_ticks(x0, x1, max(2, rect.width() // U.px(65))):
            p = self._map(rect, rng, tick, y0)
            painter.setPen(QPen(QColor(pal["card_border"]), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(p.x(), rect.top()), QPointF(p.x(), rect.bottom()))
            painter.setPen(QColor(pal["subtle"]))
            painter.drawText(QRect(int(p.x()) - U.px(34), rect.bottom() + U.px(2),
                                   U.px(68), U.px(16)),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             _fmt(tick))
        for tick in _nice_ticks(y0, y1):
            p = self._map(rect, rng, x0, tick)
            painter.setPen(QPen(QColor(pal["card_border"]), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(rect.left(), p.y()), QPointF(rect.right(), p.y()))
            painter.setPen(QColor(pal["subtle"]))
            painter.drawText(QRect(0, int(p.y()) - U.px(8), U.px(52), U.px(16)),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             _fmt(tick))

        painter.setPen(QPen(QColor(pal["subtle"]), 1))
        painter.drawRect(rect)

        # 曲线
        painter.setClipRect(rect)
        for t in self._traces:
            poly = QPolygonF([self._map(rect, rng, xv, yv) for xv, yv in zip(t["x"], t["y"])])
            painter.setPen(QPen(QColor(t["color"]), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(poly)
        painter.setClipping(False)

        # 悬停：竖线 + 最近点
        if self._hover is not None:
            hx = self._hover
            painter.setPen(QPen(QColor(pal["subtle"]), 1, Qt.PenStyle.DashLine))
            p = self._map(rect, rng, hx, y0)
            painter.drawLine(QPointF(p.x(), rect.top()), QPointF(p.x(), rect.bottom()))
            painter.setFont(U.qfont("micro"))
            lines = []
            for t in self._traces:
                j = min(range(len(t["x"])), key=lambda i: abs(t["x"][i] - hx))
                pt = self._map(rect, rng, t["x"][j], t["y"][j])
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(t["color"])))
                painter.drawEllipse(pt, U.px(3), U.px(3))
                lines.append((t, t["x"][j], t["y"][j]))
            if lines:
                text = "\n".join(
                    f"{t['label']}: {_fmt(xv)} → {_fmt(yv)} {t['y_unit']}".strip()
                    for t, xv, yv in lines
                )
                painter.setPen(QColor(pal["text"]))
                metrics = painter.fontMetrics()
                text = "\n".join(metrics.elidedText(ln, Qt.TextElideMode.ElideRight,
                    max(1, rect.width() - U.px(16))) for ln in text.splitlines())
                w = min(rect.width(), max(metrics.horizontalAdvance(ln) for ln in text.splitlines()) + U.px(12))
                h = metrics.height() * len(lines) + U.px(8)
                bx = max(rect.left(), min(p.x() + U.px(6), rect.right() - w - 2))
                by = rect.top() + U.px(4)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(pal["card_bg"])))
                painter.drawRoundedRect(QRect(int(bx), int(by), int(w), int(h)),
                                        U.R("xs"), U.R("xs"))
                painter.setPen(QPen(QColor(pal["card_border"]), 1))
                painter.drawRoundedRect(QRect(int(bx), int(by), int(w), int(h)),
                                        U.R("xs"), U.R("xs"))
                painter.setPen(QColor(pal["text"]))
                painter.drawText(QRect(int(bx) + U.px(6), int(by) + U.px(4),
                                       int(w), int(h)), text)

        # One legend entry per line, outside the plotting area.
        painter.setFont(U.qfont("micro"))
        lx = rect.left()
        for i, t in enumerate(self._traces):
            label = f"{t['label']}" + (f" ({t['y_unit']})" if t["y_unit"] else "")
            label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight,
                                                     max(1, rect.width() - U.px(18)))
            y = U.px(8 + i * 18)
            painter.setPen(QPen(QColor(t["color"]), 2))
            painter.drawLine(QPointF(lx, y + U.px(7)), QPointF(lx + U.px(10), y + U.px(7)))
            painter.setPen(QColor(pal["subtle"]))
            painter.drawText(QRect(int(lx) + U.px(16), y, rect.width() - U.px(16), U.px(16)),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             label)

        # 轴标签
        painter.setPen(QColor(pal["subtle"]))
        painter.drawText(QRect(rect.left(), rect.bottom() + U.px(18),
                               rect.width(), U.px(16)),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                         x_label)
        painter.save()
        painter.translate(U.px(12), rect.center().y())
        painter.rotate(-90)
        painter.drawText(QRect(-rect.height() // 2, -U.px(8), rect.height(), U.px(16)),
                         Qt.AlignmentFlag.AlignCenter, "数值" +
                         (f" ({self._traces[0]['y_unit']})" if self._traces[0]['y_unit'] else "（单位未声明）"))
        painter.restore()
        painter.end()

    # -- 交互 ---------------------------------------------------------------
    def mouseMoveEvent(self, event):  # noqa: N802
        rng = self._ranges()
        if rng is None:
            return
        rect = self._plot_rect()
        if rect.width() <= 0:
            return
        x0, x1 = rng[0], rng[1]
        ratio = (event.position().x() - rect.left()) / rect.width()
        if 0.0 <= ratio <= 1.0:
            self._hover = x0 + ratio * (x1 - x0)
            self.update()

    def leaveEvent(self, event):  # noqa: N802
        self._hover = None
        self.update()

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return U.px(230)


# ---------------------------------------------------------------------------
# 数据点查看
# ---------------------------------------------------------------------------

class PointsDialog(QDialog):
    """逐点检查曲线数据（可复制/导出），并明确标注这是显示点还是完整数据。

    结果页持久化的是**显示采样序列**（默认保极值降采样到 ~600 点）——
    指标计算用的是完整数据，但这个对话框拿到的是显示数组。为了不让用户
    把显示点当成完整原始数据，顶部标识、表头提示和 CSV 头部都会写明；
    需要完整数据时走「导出完整数据」（后端从原始 .ds 重读）。
    """

    def __init__(self, job: dict, parent=None, on_export_full=None, pal=None):
        super().__init__(parent)
        inherited = getattr(parent, '_pal', None)
        if callable(inherited):
            inherited = inherited()
        self._pal = pal or inherited or U.PALETTES['light']
        self.setStyleSheet(U.dialog_css(self._pal))
        self.setWindowTitle("曲线数据点")
        self.resize(U.px(560), U.px(460))
        traces = (job.get("artifacts") or {}).get("traces") or {}
        self._traces = traces
        self._job = job or {}
        self._on_export_full = on_export_full

        lay = QVBoxLayout(self)
        lay.setContentsMargins(U.px(18), U.px(18), U.px(18), U.px(18))
        lay.setSpacing(U.px(12))
        heading = QLabel("曲线数据")
        heading.setStyleSheet(f"font-size:{U.fs('hero')}px;font-weight:bold;")
        lay.addWidget(heading)
        top = QHBoxLayout()
        top.addWidget(QLabel("曲线："))
        self.picker = QComboBox()
        for name, t in traces.items():
            n_full = int(t.get("n_points") or len(t.get("x") or []))
            n_disp = int(t.get("n_display") or n_full)
            extra = f"，显示 {n_disp}/{n_full}" if n_disp < n_full else ""
            self.picker.addItem(f"{t.get('y_name') or name}  ({n_full} 点{extra})", name)
        self.picker.currentIndexChanged.connect(self._reload)
        top.addWidget(self.picker, 1)
        lay.addLayout(top)

        self.source = QLabel("")
        self.source.setWordWrap(True)
        self.source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source.setStyleSheet(f"color:{self._pal['subtle']}; font-size:{U.fs('tiny')}px;")
        lay.addWidget(self.source)

        self.badge = QLabel("")
        self.badge.setWordWrap(True)
        self.badge.setStyleSheet(f"color:{self._pal['accent']};background:{self._pal['accent_soft']};"
                                f"padding:{U.px(10)}px;border-radius:{U.R('sm')}px;font-size:{U.fs('tiny')}px;")
        lay.addWidget(self.badge)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["横轴", "纵轴"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(U.px(34))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lay.addWidget(self.table, 1)

        row = QGridLayout()
        self.actions = row
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{self._pal['subtle']}; font-size:{U.fs('tiny')}px;")
        lay.addWidget(self.hint)
        self.action_buttons = []
        if callable(self._on_export_full):
            self.export_btn = QPushButton("导出完整数据…")
            self.export_btn.setToolTip("从原始 .ds 数据集重新读取该表达式的全部点并另存为 CSV")
            self.export_btn.clicked.connect(self._export_full)
            self.export_btn.setStyleSheet(U.action_css(self._pal, primary=True))
            self.action_buttons.append(self.export_btn)
        copy = QPushButton("复制为 CSV")
        copy.setToolTip("复制的是当前表格里的点（可能是显示采样点，见上方标识）")
        copy.clicked.connect(self._copy)
        self.action_buttons.append(copy)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        self.action_buttons.append(close)
        for button in self.action_buttons:
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addLayout(row)

        self._reload()
        self._layout_actions()

    def _layout_actions(self):
        columns = 1 if self.width() < U.px(440) else len(self.action_buttons)
        while self.actions.count():
            self.actions.takeAt(0)
        for i, button in enumerate(self.action_buttons):
            self.actions.addWidget(button, i // columns, i % columns)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, 'actions'):
            self._layout_actions()

    def _display_state(self, t: dict) -> tuple:
        """返回 (是否显示采样点, 标识文案)。"""
        xs = t.get("x") or []
        n_full = int(t.get("n_points") or len(xs))
        n_disp = int(t.get("n_display") or len(xs))
        method = str(t.get("display_method") or "none")
        quality = t.get("quality") or {}
        raw = int(t.get("n_points_raw") or quality.get("n_raw") or n_full)
        if method not in ("none", "") and n_disp < n_full:
            label = (f"当前为显示采样点：{n_disp} / {n_full} 点"
                     f"（降采样算法 {method}；原始数据集 {raw} 点）")
            return True, label
        if n_full < raw:
            return False, (f"当前为完整有效数据：{n_full} 点"
                           f"（数据集原始 {raw} 点，其中 {raw - n_full} 点无效已剔除）")
        return False, f"当前为完整数据：{n_full} 点（未降采样）"

    def _copy(self):
        """把当前曲线的数据点复制成 CSV（可直接粘到 Excel 或写进报告）。"""
        from PySide6.QtWidgets import QApplication

        name = self.picker.currentData()
        t = self._traces.get(name) or {}
        xs, ys = t.get("x") or [], t.get("y") or []
        is_display, label = self._display_state(t)
        lines = [f"# {t.get('y_name') or name}",
                 f"# {label}",
                 f"# 来源: {t.get('source') or job_source(self._traces) or '未知'}"]
        lines.append(f"{t.get('x_name') or 'x'}({t.get('x_unit') or '未声明'}),"
                     f"{t.get('y_name') or 'y'}({t.get('y_unit') or '未声明'})")
        lines += [f"{_fmt(xv, 8)},{_fmt(yv, 8)}" for xv, yv in zip(xs, ys)]
        QApplication.clipboard().setText("\n".join(lines))
        self.hint.setText(f"已复制 {len(xs)} 行到剪贴板"
                          + ("（显示采样点）" if is_display else "（完整数据）"))

    def _export_full(self):
        name = self.picker.currentData()
        if not name:
            return
        try:
            self._on_export_full(self._job, name)
        except Exception:  # noqa: BLE001 — 回调异常不影响对话框
            pass

    def _reload(self):
        name = self.picker.currentData()
        if name is None:
            self.table.setRowCount(0)
            self.badge.setText("暂无可查看的曲线数据，请先完成仿真。")
            self.source.setText("尚无数据来源")
            self.hint.setText("共 0 行")
            for button in self.action_buttons[:-1]:
                button.setEnabled(False)
            return
        t = self._traces.get(name) or {}
        xs, ys = t.get("x") or [], t.get("y") or []
        self.table.setRowCount(len(xs))
        for i, (xv, yv) in enumerate(zip(xs, ys)):
            for column, value in enumerate((xv, yv)):
                item = QTableWidgetItem(_fmt(value, 6))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, column, item)
        x_unit = t.get("x_unit") or "（未声明）"
        y_unit = t.get("y_unit") or "（未声明）"
        self.table.setHorizontalHeaderLabels([f"横轴 ({x_unit})", f"纵轴 ({y_unit})"])
        is_display, label = self._display_state(t)
        self.badge.setText(("⚠ " if is_display else "✔ ") + label)
        quality = t.get("quality") or {}
        dropped = quality.get("n_dropped")
        extra = ""
        if dropped:
            extra += f"    读不出丢弃 {dropped} 点"
        if quality.get("max_gap") is not None:
            extra += f"    最大采样间隔 {quality['max_gap']:.4g}（横轴单位）"
        self.source.setText(
            f"数据来源：{t.get('source') or job_source(self._traces) or '未知'}"
            f"    原始点数 {t.get('n_points_raw') or quality.get('n_raw') or len(xs)}"
            + extra
            + ("    （原始曲线曾因点数超限保极值降采样记录）" if t.get("truncated") else "")
        )
        self.hint.setText(f"共 {len(xs)} 行")


def job_source(traces: dict) -> str:
    for t in (traces or {}).values():
        if isinstance(t, dict) and t.get("source"):
            return str(t["source"])
    return ""


# ---------------------------------------------------------------------------
# 结果页条目
# ---------------------------------------------------------------------------

class ResultPageRow(QWidget):
    """一条 ``kind == 'result'`` 的对话条目：完整的设计结果页。"""

    def __init__(self, job: dict, pal: dict, on_open_schematic=None,
                 on_resimulate=None, on_refresh=None, parent=None,
                 on_export_full=None):
        super().__init__(parent)
        self._job = job or {}
        self._pal = pal
        self._on_open = on_open_schematic
        self._on_resim = on_resimulate
        self._on_refresh = on_refresh
        self._on_export_full = on_export_full

        outer = QVBoxLayout(self)
        outer.setContentsMargins(U.P("xl"), U.P("sm"), U.P("xl"), U.P("sm"))
        outer.setSpacing(U.P("sm"))

        self.card = QFrame()
        self.card.setObjectName("designResultCard")
        outer.addWidget(self.card)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(U.px(14), U.px(14), U.px(14), U.px(14))
        lay.setSpacing(U.px(12))
        self._responsive_rows = []

        self._build_header(lay)
        self._build_design_ref(lay)
        self._build_model_conditions(lay)
        self._build_metrics(lay)
        self._build_chart(lay)
        self._build_footer(lay)
        self._apply_style()

    # -- 构建 ---------------------------------------------------------------
    def _build_header(self, lay):
        row = QHBoxLayout()
        self._responsive_rows.append(row)
        row.setSpacing(U.P("sm"))
        self.title = QLabel(self._job.get("title") or "设计结果")
        self.title.setWordWrap(True)
        f = U.qfont("body", bold=True)
        self.title.setFont(f)
        row.addWidget(self.title, 1)

        verdict = self._job.get("verdict") or "unknown"
        text, color = VERDICT_STYLE.get(verdict, VERDICT_STYLE["unknown"])
        self.verdict = QLabel(text)
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet(
            f"color:{color}; background:{self._pal['card_bg']};"
            f"border:none; border-left:3px solid {color}; border-radius:{U.R('xs')}px;"
            f"padding:{U.px(8)}px {U.px(10)}px; font-size:{U.fs('small')}px;"
        )
        row.addWidget(self.verdict, 0)
        lay.addLayout(row)

        req = self._job.get("requirement") or ""
        if req:
            self.req = QLabel(f"要求：{req}")
            self.req.setWordWrap(True)
            self.req.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.req.setStyleSheet(
                f"color:{self._pal['subtle']}; font-size:{U.fs('tiny')}px;"
            )
            lay.addWidget(self.req)

        band = (self._job.get("band") or {}).get("label") or ""
        stage = self._job.get("stage_label") or self._job.get("stage") or ""
        summary = self._job.get("summary") or {}
        bits = [f"阶段：{stage}"]
        if band:
            bits.append(f"评估频段：{band}")
        if summary:
            bits.append(f"指标 {summary.get('n_passed', 0)}/{summary.get('n_metrics', 0)} 达标")
        if (self._job.get("iterations") or []):
            bits.append(f"第 {len(self._job['iterations'])} 次迭代")
        self.meta = QLabel("　·　".join(bits))
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet(
            f"color:{self._pal['subtle']}; font-size:{U.fs('micro')}px;"
        )
        lay.addWidget(self.meta)

    def _build_design_ref(self, lay):
        d = self._job.get("design") or {}
        row = QHBoxLayout()
        self._responsive_rows.append(row)
        row.setSpacing(U.P("sm"))
        ref = self._job.get("design_ref") or "?"
        parts = [
            f"工作区：{d.get('workspace') or '（当前打开的工作区）'}",
            f"库：{d.get('library') or '?'}",
            f"cell：{d.get('cell') or '?'}",
            f"视图：{d.get('view') or 'schematic'}",
        ]
        self.design_ref = QLabel(f"{ref}\n" + "\n".join(parts))
        self.design_ref.setTextFormat(Qt.TextFormat.PlainText)
        self.design_ref.setWordWrap(True)
        self.design_ref.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.design_ref.setStyleSheet(
            f"color:{self._pal['text']}; background:{self._pal['card_bg']};"
            "border:none;"
            f"border-radius:{U.R('md')}px; padding:{U.P('sm')}px {U.P('md')}px;"
            f"font-size:{U.fs('tiny')}px;"
        )
        row.addWidget(self.design_ref, 1)

        self.open_btn = QPushButton("打开原理图")
        self.open_btn.setToolTip("在 ADS 中打开此设计的原理图")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self._emit_open)
        row.addWidget(self.open_btn, 0)
        lay.addLayout(row)

    def _build_model_conditions(self, lay):
        """模型来源与有效条件（原厂模型必须让用户看见"数据从哪来、在什么条件下有效"）。

        为什么专门一块：指标表里"达标"两个字看不出背后是不是拿超出模型有效
        频段的插值/外推曲线判的。这里把模型来源、有效频段、参考阻抗、偏压
        写清楚，门禁没过时用警示色标出"未判达标的原因"。
        """
        sim = self._job.get("sim") or {}
        deps = sim.get("model_deps") or {}
        conditions = sim.get("model_conditions") or []
        gate = sim.get("model_gate") or {}
        if not deps and not conditions and not gate:
            return

        heading = QLabel("模型来源与有效条件")
        heading.setFont(U.qfont("small", bold=True))
        heading.setStyleSheet(f"color:{self._pal['text']};")
        lay.addWidget(heading)

        lines = []
        state = str(deps.get("state") or "")
        state_text = {
            "none": "本次仿真没有引用外部模型文件",
            "complete": f"已核对 {deps.get('n_deps', 0)} 个模型文件的内容哈希",
            "incomplete": "模型依赖无法完整确认（按保守策略处理）",
            "missing": "模型依赖缺失（有文件在盘上找不到）",
        }.get(state, "")
        if state_text:
            lines.append((f"模型依赖：{state_text}", self._pal['subtle']))
        for cond in conditions:
            part = cond.get("part") or cond.get("variant") or "（未识别型号）"
            source = cond.get("source") or "来源未知"
            if not cond.get("available"):
                lines.append((f"{part}（{source}）：有效条件未知 —— "
                              f"{cond.get('reason') or '索引里没有可引用数据'}",
                              self._pal['subtle']))
                continue
            bits = []
            if cond.get("freq_start_hz") and cond.get("freq_stop_hz"):
                bits.append("有效频段 %s–%s" % (_hz(cond["freq_start_hz"]),
                                                _hz(cond["freq_stop_hz"])))
            else:
                bits.append("有效频段未知")
            if cond.get("reference_impedance_ohm"):
                bits.append("Z0 %gΩ" % cond["reference_impedance_ohm"])
            if cond.get("bias"):
                bits.append("偏压 " + ", ".join(
                    f"{k}={v}" for k, v in dict(cond["bias"]).items()))
            if cond.get("package_id"):
                bits.append(f"包 {cond['package_id']}")
            lines.append((f"{part}（{source}）：" + "，".join(bits),
                          self._pal['subtle']))
        if gate:
            color = self._pal['error'] if gate.get("block") else self._pal['subtle']
            lines.append((str(gate.get("message") or ""), color))

        for text, color in lines:
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet(
                f"color:{color}; background:{self._pal['card_bg']};"
                f"border:none; border-radius:{U.R('sm')}px;"
                f"padding:{U.px(6)}px {U.P('sm')}px; font-size:{U.fs('micro')}px;")
            lay.addWidget(label)

    def _build_metrics(self, lay):
        heading = QLabel("指标评估")
        heading.setFont(U.qfont("small", bold=True))
        heading.setStyleSheet(f"color:{self._pal['text']};")
        lay.addWidget(heading)
        self.metrics_grid = QGridLayout()
        self.metrics_grid.setSpacing(U.px(8))
        self.metric_cards = []
        self._metric_columns = 0
        metrics = self._job.get("metrics") or []
        if not metrics:
            empty = QLabel("尚未定义指标")
            empty.setStyleSheet(f"color:{self._pal['subtle']};")
            lay.addWidget(empty)
        for m in metrics:
            card = QFrame()
            card.setObjectName("metricCard")
            card.setStyleSheet(
                f"QFrame#metricCard{{background:{self._pal['card_bg']};"
                f"border:none;border-radius:{U.R('md')}px;}}")
            inner = QVBoxLayout(card)
            inner.setContentsMargins(U.px(12), U.px(10), U.px(12), U.px(10))
            inner.setSpacing(U.px(6))
            passed = m.get("pass")
            mark = "达标" if passed is True else ("未达标" if passed is False else "无法判定")
            dark = self._pal == U.PALETTES['dark']
            color = ('#61cbb0' if dark else '#16856b') if passed is True else (self._pal['error'] if passed is False else self._pal['subtle'])
            unit = m.get("unit") or ""
            badge = QLabel(mark)
            badge_bg = ('#193c35' if dark else '#e8f5f0') if passed is True else (
                ('#442832' if dark else '#fff0f1') if passed is False else self._pal['accent_soft'])
            badge.setStyleSheet(f"color:{color};background:{badge_bg};padding:{U.px(4)}px {U.px(8)}px;"
                               f"border-radius:{U.R('xs')}px;font-size:{U.fs('tiny')}px;")
            lines = [
                (m.get("label") or m.get("id") or "未命名指标", self._pal['text'], "small", True),
                (f"{_fmt(m.get('actual'))} {unit}".strip(), self._pal['text'], "hero", True),
                (f"目标  {_fmt(m.get('target'))} {unit}".strip(), self._pal['subtle'], "small", False),
                (f"频点  {m.get('at') or '—'}", self._pal['subtle'], "tiny", False),
            ]
            if m.get('note'):
                lines.append((str(m['note']), self._pal['subtle'], "tiny", False))
            for text, fg, token, bold in lines:
                label = QLabel(text)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                label.setFont(U.qfont(token, bold=bold))
                label.setStyleSheet(f"color:{fg};font-size:{U.fs(token)}px;" +
                                   ("font-weight:bold;" if bold else ""))
                inner.addWidget(label)
            inner.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
            self.metric_cards.append(card)
        lay.addLayout(self.metrics_grid)
        self._layout_metrics(1)

    def _layout_metrics(self, columns):
        if self._metric_columns == columns:
            return
        self._metric_columns = columns
        while self.metrics_grid.count():
            self.metrics_grid.takeAt(0)
        for i, card in enumerate(self.metric_cards):
            self.metrics_grid.addWidget(card, i // columns, i % columns)
        self.metrics_grid.setColumnStretch(0, 1)
        self.metrics_grid.setColumnStretch(1, 1 if columns == 2 else 0)

    def _build_chart(self, lay):
        traces = (self._job.get("artifacts") or {}).get("traces") or {}
        self.chart = CurveChart(traces, self._pal)
        lay.addWidget(self.chart)
        src = job_source(traces)
        self.chart_src = QLabel(
            f"数据来源：{src or '（无数据集）'}"
            + (f"　·　{len(traces)} 条曲线" if traces else "")
        )
        self.chart_src.setWordWrap(True)
        self.chart_src.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.chart_src.setStyleSheet(
            f"color:{self._pal['subtle']}; font-size:{U.fs('micro')}px;"
        )
        lay.addWidget(self.chart_src)

    def _build_footer(self, lay):
        sim = self._job.get("sim") or {}
        art = self._job.get("artifacts") or {}
        lines = [f"仿真状态：{sim.get('status') or '—'}"]
        if sim.get("finished_at"):
            lines.append(f"结束时间：{sim['finished_at']}")
        if art.get("output_dir"):
            lines.append(f"输出目录：{art['output_dir']}")
        if art.get("netlist_path"):
            lines.append(f"网表：{art['netlist_path']}")
        if art.get("dataset_path"):
            lines.append(f"数据集：{art['dataset_path']}")
        err = self._job.get("error") or sim.get("error") or ""
        self.footer = QLabel("\n".join(lines))
        self.footer.setWordWrap(True)
        self.footer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.footer.setStyleSheet(
            f"color:{self._pal['subtle']}; font-size:{U.fs('micro')}px;"
        )
        lay.addWidget(self.footer)

        if err:
            self.err = QLabel(f"错误：{err}")
            self.err.setWordWrap(True)
            self.err.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.err.setStyleSheet(
                f"color:{self._pal['error']}; font-size:{U.fs('micro')}px;"
            )
            lay.addWidget(self.err)

        row = QGridLayout()
        self.footer_actions = row
        self._action_layout_state = None
        row.setSpacing(U.P("sm"))
        self.points_btn = QPushButton("查看数据点")
        self.points_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.points_btn.clicked.connect(self._show_points)

        self.refresh_btn = QPushButton("重新评估")
        self.refresh_btn.setToolTip("不重新仿真，只重新读取 .ds 并重算指标")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(lambda: self._emit(self._on_refresh))

        self.resim_btn = QPushButton("重新仿真")
        self.resim_btn.setToolTip("按当前指标与设计引用重新生成网表并仿真")
        self.resim_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resim_btn.clicked.connect(lambda: self._emit(self._on_resim))
        lay.addLayout(row)
        self._layout_actions(True, U.px(330))

    def _layout_actions(self, narrow, width):
        state = (narrow, width < U.px(280))
        if state == self._action_layout_state:
            return
        self._action_layout_state = state
        while self.footer_actions.count():
            self.footer_actions.takeAt(0)
        if narrow:
            columns = 1 if state[1] else 2
            self.footer_actions.addWidget(self.points_btn, 0, 0, 1, columns)
            self.footer_actions.addWidget(self.refresh_btn, 1, 0)
            self.footer_actions.addWidget(self.resim_btn, 2 if columns == 1 else 1, 0 if columns == 1 else 1)
        else:
            for column, button in enumerate((self.points_btn, self.refresh_btn, self.resim_btn)):
                self.footer_actions.addWidget(button, 0, column)

    def _apply_style(self):
        pal = self._pal
        self.card.setStyleSheet(
            f"QFrame#designResultCard{{background:{pal['chat_bg']};"
            f"border:1px solid {pal['card_border']}; border-radius:{U.R('lg')}px;}}"
            f"QLabel{{background:transparent;}}"
            + U.font_css()
        )
        self.title.setStyleSheet(f"color:{pal['text']}; font-size:{U.fs('title')}px;font-weight:bold;")
        for btn in (self.open_btn, self.points_btn, self.refresh_btn, self.resim_btn):
            btn.setStyleSheet(U.action_css(pal, primary=btn is self.points_btn))
        for label in self.card.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.points_btn.setEnabled(self.chart.has_curves())

    # -- 行为 ---------------------------------------------------------------
    def _emit(self, callback):
        if callback:
            callback(self._job)

    def _emit_open(self):
        self._emit(self._on_open)

    def _show_points(self):
        dlg = PointsDialog(self._job, self,
                           on_export_full=self._on_export_full, pal=self._pal)
        dlg.exec()

    def set_busy(self, busy: bool, text: str = ""):
        for btn in (self.resim_btn, self.refresh_btn):
            btn.setEnabled(not busy)
        if text:
            self.meta.setText(text)

    def job(self) -> dict:
        return self._job

    def reflow(self, avail_w: int, item):
        """按可用宽度给出真实高度（内容很长，不能写死）。"""
        w = max(avail_w - U.P("xl") * 2, U.px(160))
        narrow = w < U.px(520)
        for row in self._responsive_rows:
            row.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        self.card.setFixedWidth(w)
        self.chart.setFixedWidth(max(U.px(100), w - U.px(28)))
        self._layout_metrics(1 if narrow else 2)
        self._layout_actions(narrow, w)
        self.card.layout().activate()
        h = self.card.layout().heightForWidth(w) + U.P('sm') * 2
        if h < 0:
            h = self.sizeHint().height()
        item.setSizeHint(QSize(avail_w, max(h, U.px(260))))
