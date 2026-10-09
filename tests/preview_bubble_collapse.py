"""验证脚本：气泡裁切修复 + 折叠到侧栏标签。

无头渲染（不启动 ADS），产出图片在 tests/preview/ 下：
  bubble-fix-*.png   —— 长 token / 中文短句 / 长回复的气泡应完整显示
  collapse-*.png     —— 折叠后主窗口右缘应只剩一条竖排标签
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ADDON = os.path.join(_ROOT, "addon", "ads_agent")
_OUT = os.path.join(_HERE, "preview")

sys.path.insert(0, _ADDON)
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow  # noqa: E402

import panel as P  # noqa: E402
import uiscale as U  # noqa: E402

P.AgentPanelWidget.reload_config = lambda self: None


def _finish(widget):
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    widget.ensurePolished()
    app.processEvents()
    widget.reflow()
    app.processEvents()


def _shot(widget, name):
    os.makedirs(_OUT, exist_ok=True)
    path = os.path.join(_OUT, name + ".png")
    widget.grab().save(path)
    print("saved", path)


def _painted_lines(label):
    """抓取 label 本体，数实际绘制的文字行数（按墨水行聚类）。"""
    from PySide6.QtGui import QImage
    img = label.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    bg = img.pixelColor(w // 2, 1)
    ink_ys = set()
    for y in range(h):
        for x in range(w):
            c = img.pixelColor(x, y)
            if abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue()) > 120:
                ink_ys.add(y)
    ys = sorted(ink_ys)
    lines = 0
    prev = -10
    for y in ys:
        if y - prev > 2:
            lines += 1
        prev = y
    return lines


def bubble_case(name, entries, w=430, h=700, dark=False):
    P.AgentPanelWidget._PROJECTS_FILE = os.path.join(_OUT, f"_pv_{name}.json")
    widget = P.AgentPanelWidget()
    widget.dark = dark
    widget._apply_theme()
    for kind, text in entries:
        widget._add_entry(kind, text)
    widget.resize(w, h)
    _finish(widget)
    for i in range(widget.chat.count()):
        _it = widget.chat.item(i)
        _row = widget.chat.itemWidget(_it)
        if isinstance(_row, P.BubbleRow):
            print(f"  row{i} {_row.kind} label={_row.label.width()}x{_row.label.height()}"
                  f" item_h={_it.sizeHint().height()} text={_row._text[:14]!r}")
    _shot(widget, name)
    # 校验：label 实际绘制的行数必须 <= 固定高度能容纳的行数（否则有文字被裁）
    problems = []
    for i in range(widget.chat.count()):
        item = widget.chat.item(i)
        row = widget.chat.itemWidget(item)
        if isinstance(row, P.BubbleRow):
            painted = _painted_lines(row.label)
            line_h = max(row.label.fontMetrics().lineSpacing(), 1)
            fits = max(1, row.label.height() // line_h)
            if painted > fits:
                problems.append(f"row {i} ({row.kind}): 绘了 {painted} 行，只容 {fits} 行")
    if problems:
        print("  !! CLIPPED:", *problems, sep="\n     ")
    else:
        print("  ok: 所有气泡文字完整绘制，无裁切")
    widget.close()
    return not problems


def main():
    global app
    app = QApplication(sys.argv)
    os.makedirs(_OUT, exist_ok=True)

    ok = True
    # 1) 截图同款问题：长无空格 token（工具回显）+ 中文短句
    ok &= bubble_case("bubble-fix-narrow", [
        ("user", 'de_open_design"AI_lib","Wilkinson_2G4_v2","schematic"'),
        ("assistant", "2 个设计已打开，当前激活的是 Wilkinson_2G4_v2 的 schematic。"
                      "需要我继续读它的 VAR 变量吗？"),
        ("user", "你能做什么"),
        ("assistant", "我可以列出工作区设计、读取/修改变量、跑仿真并对比结果。"),
        ("stat", P._format_stats({"completion_tokens": 11300, "llm_seconds": 166.4,
                                  "model": "deepseek-chat"}, "")),
    ], w=340)
    # 2) 正常宽度 + 深色（含 usage 缺失时的 ≈ 估算统计行）
    ok &= bubble_case("bubble-fix-dark", [
        ("user", 'de_open_design"AI_lib","Wilkinson_2G4_v2","schematic" 然后跑仿真'),
        ("assistant", "已打开设计。S 参数仿真完成：S11 = -18.2 dB @ 2.4 GHz，"
                      "隔离度 S23 = -21 dB，两支路幅度不平衡 < 0.3 dB，整体达标。"),
        ("stat", P._format_stats({"completion_tokens": None, "llm_seconds": 6.2,
                                  "model": "glm-4.6"},
                                 "已打开设计。S 参数仿真完成：S11 = -18.2 dB @ 2.4 GHz，"
                                 "隔离度 S23 = -21 dB，两支路幅度不平衡 < 0.3 dB，整体达标。")),
    ], w=430, dark=True)

    # 3) 折叠标签：主窗口 + dock，折叠后应只剩右缘竖条
    win = QMainWindow()
    win.resize(900, 620)
    win.setCentralWidget(P.ChipButton("central"))
    P.AgentPanelWidget._PROJECTS_FILE = os.path.join(_OUT, "_pv_collapse.json")
    widget = P.AgentPanelWidget()

    dock = QDockWidget(P.DOCK_TITLE, win)
    dock.setObjectName(P.DOCK_OBJECT_NAME)
    dock.setWidget(widget)
    bar = P._install_dock_title_bar(dock, widget)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    handle = QDockWidget(win)
    handle.setObjectName(P.HANDLE_OBJECT_NAME)
    from PySide6.QtWidgets import QWidget
    handle.setTitleBarWidget(QWidget(handle))
    handle.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
    tab = P._VerticalTabButton("◀ ADS Agent")
    handle.setWidget(tab)
    handle.setFixedWidth(U.px(34))
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, handle)
    P._handle, P._handle_tab = handle, tab
    P._panel = dock
    P._restyle_handle(widget._pal())
    dock.visibilityChanged.connect(P._sync_handle)
    tab.clicked.connect(P._expand_panel)

    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    app.processEvents()
    _shot(win, "collapse-1-expanded")

    bar.collapse_btn.click()  # 自定义标题栏 » 是唯一收起入口。
    app.processEvents()
    _shot(win, "collapse-2-collapsed")
    print("  collapsed: dock visible =", dock.isVisible(),
          "| handle visible =", handle.isVisible())

    P._expand_panel()  # 点标签展开
    app.processEvents()
    _shot(win, "collapse-3-reexpanded")
    print("  expanded:  dock visible =", dock.isVisible(),
          "| handle visible =", handle.isVisible())

    assert not dock.isVisible() or handle.isVisible() is False
    print("ALL OK" if ok else "SOME ROWS STILL CLIPPED")
    for f in os.listdir(_OUT):
        if f.startswith("_pv_"):
            os.remove(os.path.join(_OUT, f))


if __name__ == "__main__":
    main()
