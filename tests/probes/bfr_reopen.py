# -*- coding: utf-8 -*-
"""进程内触发 File ▸ Recent Schematics 打开 FM_SC 原理图窗口(不截图不 grab)。"""
from PySide6.QtWidgets import QApplication

app = QApplication.instance()
wins = [w for w in app.topLevelWidgets() if w.isVisible()]
main = None
for w in wins:
    if w.metaObject().className().startswith("Ads") or "ADS" in (w.windowTitle() or ""):
        if hasattr(w, "menuBar") and w.menuBar() is not None:
            main = w
            break
if main is None:
    for w in wins:
        if hasattr(w, "menuBar") and w.menuBar() and w.menuBar().actions():
            main = w
            break
if main is None:
    raise RuntimeError("找不到 ADS 主窗口")
print("主窗口:", main.windowTitle())

mb = main.menuBar()
file_menu = None
for a in mb.actions():
    if "file" in (a.text() or "").lower() or "文件" in (a.text() or ""):
        file_menu = a
        break
if file_menu is None:
    raise RuntimeError("找不到 File 菜单")
recent = None
for a in file_menu.menu().actions():
    t = (a.text() or "").replace("&", "").strip().lower()
    if t.startswith("recent schematics"):
        recent = a
        break
if recent is None:
    raise RuntimeError("找不到 Recent Schematics 子菜单")
target = None
names = []
for sub in recent.menu().actions():
    names.append(sub.text())
    if "FM_SC" in (sub.text() or ""):
        target = sub
print("recent 项:", names)
if target is None:
    raise RuntimeError("Recent Schematics 里没有 FM_SC")
target.trigger()
print("已触发重开:", target.text())
print("done")
