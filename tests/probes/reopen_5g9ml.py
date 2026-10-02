"""通过主窗口菜单 File ▸ Recent Schematics 重开 Wilkinson_5G9_ML 原理图窗口。"""
from PySide6.QtWidgets import QApplication

app = QApplication.instance()
wins = [w for w in app.topLevelWidgets() if w.isVisible()]
main = None
for w in wins:
    if w.metaObject().className().startswith("Ads") or "ADS" in (w.windowTitle() or ""):
        mb = w.menuBar() if hasattr(w, "menuBar") else None
        if mb is not None:
            main = w
            break
if main is None:
    for w in wins:
        if hasattr(w, "menuBar") and w.menuBar().actions():
            main = w
            break
print("主窗口:", main.windowTitle() if main else None)
mb = main.menuBar()
file_menu = None
for a in mb.actions():
    if "file" in a.text().lower() or "文件" in a.text():
        file_menu = a
        break
print("File 菜单:", file_menu.text() if file_menu else None)
target = None
for a in file_menu.menu().actions():
    t = a.text()
    if t.replace("&", "").strip().lower().startswith("recent schematics"):
        for sub in a.menu().actions():
            print("  recent:", sub.text())
            if "Wilkinson_5G9_ML" in sub.text():
                target = sub
        break
if target is None:
    raise SystemExit("Recent 里没找到 Wilkinson_5G9_ML")
target.trigger()
print("已触发重开:", target.text())
