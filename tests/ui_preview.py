"""开发期 UI 预览脚本 —— 不启动 ADS，直接把面板渲染成图片看效果。

用法（需要装了 PySide6 的解释器）::

    python tests/ui_preview.py

产出图片在 tests/preview/ 下，覆盖：浅色/深色、宽/窄面板、放大字号、
设置区展开、有对话内容等场景，用来检查自适应与圆角效果。
"""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ADDON = os.path.join(_ROOT, "addon", "ads_agent")
_OUT = os.path.join(_HERE, "preview")
_PREVIEW_DATA = tempfile.TemporaryDirectory(prefix="ads_ui_preview_")

sys.path.insert(0, _ADDON)
# offscreen 插件拿不到系统字体（截图里会全是方框），windows 插件才能加载雅黑
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import panel as P  # noqa: E402
import uiscale as U  # noqa: E402

# 预览不要碰真实数据：每个场景用独立的项目文件，也不去连后端
P.AgentPanelWidget.reload_config = lambda self: None
_seq = [0]


def _make(scale=None, dark=False, entries=None, settings=False, w=520, h=820):
    U.refresh()
    if scale:
        U._cache["scale"] = scale
    _seq[0] += 1
    os.makedirs(_OUT, exist_ok=True)
    P.AgentPanelWidget._PROJECTS_FILE = os.path.join(_PREVIEW_DATA.name, f"projects_{_seq[0]}.json")

    widget = P.AgentPanelWidget()
    widget.dark = dark
    widget._apply_theme()
    widget._set_model_combo_text("deepseek-flash")
    widget._populate_model_list(["deepseek-flash", "deepseek-v4-pro"])
    widget.preset_combo.blockSignals(True)
    widget.preset_combo.setCurrentText("DeepSeek")
    widget.preset_combo.blockSignals(False)
    widget.base_url_edit.setText("https://api.deepseek.com")
    widget._update_get_key_link("DeepSeek")
    for kind, text in (entries or []):
        widget._add_entry(kind, text)
        if kind == "activity":
            widget.entries[-1].update(events=[
                '⚙ set_design_variables\n{"Rbias": "120 Ohm"}',
                '✅ set_design_variables → 已修改 1 个变量并保存 design',
                '⚙ run_simulation\n{"cell": "Wilkinson_2G4_v2"}',
                '✅ run_simulation → 仿真完成',
            ], elapsed=7, complete=True, expanded=False)
            widget._rebuild()
    if settings:
        widget.settings_btn.setChecked(True)
    widget.resize(w, h)
    # 真正 show() 才会激活布局（但不能真的弹窗），WA_DontShowOnScreen 正好
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()
    widget.ensurePolished()
    app.processEvents()
    widget.reflow()
    app.processEvents()
    return widget


def _shot(widget, name):
    os.makedirs(_OUT, exist_ok=True)
    path = os.path.join(_OUT, name + ".png")
    widget.grab().save(path)
    print("saved", path)


TALK = [
    ("user", "帮我把 Rbias 改成 120 Ohm，然后仿真 S 参数"),
    ("activity", "思考"),
    ("assistant", "已经把偏置电阻改成 120 Ohm 并跑完 S 参数仿真。增益从 12.4 dB 略降到 "
                  "11.8 dB，S11 在 2.4 GHz 处改善到 -18 dB，整体匹配更好但增益略有牺牲。"),
]


def main():
    global app
    app = QApplication(sys.argv)
    base = dict(entries=TALK)

    _shot(_make(**base), "01-light-talk")
    _shot(_make(dark=True, **base), "02-dark-talk")
    _shot(_make(w=360, h=760, **base), "03-narrow-talk")
    _shot(_make(scale=1.35, w=560, h=860, **base), "04-large-font")
    _shot(_make(scale=0.85, w=440, h=700, **base), "05-compact")
    _shot(_make(w=520, h=820), "06-welcome-light")
    _shot(_make(w=520, h=820, dark=True), "07-welcome-dark")
    _shot(_make(w=520, h=900, settings=True, entries=TALK), "08-settings")
    _shot(_make(w=330, h=760, settings=True), "09-settings-narrow")
    _shot(_make(w=330, h=760), "10-welcome-narrow")
    _shot(_make(w=330, h=520, dark=True, settings=True), "11-settings-dark-short")
    _shot(_make(scale=1.35, w=440, h=660, settings=True), "12-settings-large-font")
    # Include the host title bar to review the single remaining close control.
    from PySide6.QtWidgets import QDockWidget, QMainWindow
    host = QMainWindow()
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pal = P.PALETTES['light']
    host.setStyleSheet(
        f"QMainWindow, QDockWidget{{background:{pal['panel_bg']};color:{pal['text']};}}"
    )
    dock = QDockWidget(P.DOCK_TITLE, host)
    dock_widget = _make(w=600, h=760, **base)
    dock.setWidget(dock_widget)
    P._install_dock_title_bar(dock, dock_widget)
    host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    host.resize(600, 780)
    host.show()
    app.processEvents()
    _shot(dock, "23-custom-dock-header")
    dock_widget.dark = True
    dock_widget._apply_theme()
    host.resize(360, 780)
    app.processEvents()
    _shot(dock, "24-custom-dock-dark-narrow")
    host.close()
    print("scale at 1.0x ->", U.refresh() or U.scale())

    # 清掉预览过程产生的临时项目文件
    for name in os.listdir(_OUT):
        if name.startswith("_projects_") or name in ("_fn.json", "_p2.json"):
            os.remove(os.path.join(_OUT, name))


if __name__ == "__main__":
    main()
