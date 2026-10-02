"""枚举主窗口 File 菜单的全部条目（含子菜单前 15 项），找打开设计的入口。"""
from PySide6.QtWidgets import QApplication

app = QApplication.instance()
main = None
for w in app.topLevelWidgets():
    if w.isVisible() and hasattr(w, "menuBar") and w.menuBar() and w.menuBar().actions():
        if "ADS" in (w.windowTitle() or "") or w.metaObject().className().startswith("Ads"):
            main = w
            break
if main is None:
    for w in app.topLevelWidgets():
        if w.isVisible() and hasattr(w, "menuBar") and w.menuBar() and w.menuBar().actions():
            main = w
            break
mb = main.menuBar()
for a in mb.actions():
    if "&File" != a.text() and "File" not in a.text():
        continue
    for item in a.menu().actions():
        t = item.text().replace("&", "")
        if item.menu() is not None:
            subs = [s.text().replace("&", "") for s in item.menu().actions()][:15]
            print(f"[子菜单] {t}: {subs}")
        else:
            print(f"  {t}")
