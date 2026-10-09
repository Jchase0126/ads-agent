r"""在 ADS 进程内截图指定原理图窗口（Qt widget.grab），保存 PNG。

用法: python tests/probe_via_ads.py tests/probes/AB_grab.py -- D:\...\out.png
（直接改脚本尾部 OUT_PATH / TITLE_PAT 再跑更稳）
"""
import sys
from PySide6.QtWidgets import QApplication

TITLE_PAT = "Wilkinson_5G9"
OUT_PATH = r"D:\Antenna\ADS Agent\tests\preview\grab_5g9.png"

app = QApplication.instance()
if app is None:
    raise SystemExit("no QApplication")

found = None
for w in app.topLevelWidgets():
    try:
        if TITLE_PAT.lower() in (w.windowTitle() or "").lower() and w.isVisible():
            found = w
            break
    except Exception:  # noqa: BLE001
        continue

if found is None:
    titles = []
    for w in app.topLevelWidgets():
        try:
            if w.isVisible():
                titles.append(f"{type(w).__name__}: {w.windowTitle()!r}")
        except Exception:  # noqa: BLE001
            continue
    print("NO WINDOW with", TITLE_PAT)
    print("visible windows:", titles)
    raise SystemExit(1)

print("grab:", type(found).__name__, found.windowTitle(),
      found.width(), "x", found.height())
pix = found.grab()
ok = pix.save(OUT_PATH)
print("saved:", OUT_PATH, ok)
