"""在 ADS 主窗口里打开指定 cell 的原理图窗口（UIA 双击 Library 树），
并用 Qt widget.grab() 截图原理图窗口，保存 PNG。

用法:
  python tests/open_schematic_window.py <cell_name> <out_png>
"""
import sys
import time

CELL = sys.argv[1] if len(sys.argv) > 1 else "Wilkinson_5G9"
OUT = sys.argv[2] if len(sys.argv) > 2 else "ads_view.png"


def uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def close_get_started():
    try:
        for w in uia().windows():
            t = w.window_text() or ""
            if "get started" in t.lower():
                for b in w.descendants(control_type="Button"):
                    if "close" in (b.window_text() or "").lower():
                        b.invoke()
                        print("closed Get Started via", b.window_text())
                        return True
                w.close()
                print("closed Get Started (window.close)")
                return True
    except Exception as e:  # noqa: BLE001
        print("get-started close err:", e)
    return False


def open_cell_in_library(cell):
    """在主窗口 Library 树里找到 cell 并双击其 schematic 视图。"""
    main = None
    for w in uia().windows():
        t = w.window_text() or ""
        if "Advanced Design System" in t and "Main" in t:
            main = w
            break
    if main is None:
        raise SystemExit("main window not found")
    # Library 树：Document control_type 通常是 TreeView / 列出 TreeItem
    trees = main.descendants(control_type="Tree")
    if not trees:
        trees = main.descendants(control_type="DataGrid")
    print("trees found:", len(trees))
    target = None
    for tr in trees[:6]:
        try:
            items = tr.descendants(control_type="TreeItem")
        except Exception:  # noqa: BLE001
            continue
        names = [i.window_text() or "" for i in items]
        hit = [n for n in names if cell in n]
        if hit:
            print(f"tree with {len(items)} items, hits: {hit[:4]}")
            target = tr
            break
    if target is None:
        # 列出所有树名帮助定位
        for tr in trees[:6]:
            try:
                names = [i.window_text() or "" for i in tr.descendants(control_type="TreeItem")][:15]
                print("tree:", names)
            except Exception:  # noqa: BLE001
                pass
        raise SystemExit(f"cell {cell} not found in library trees")
    for it in target.descendants(control_type="TreeItem"):
        if cell in (it.window_text() or ""):
            it.double_click_input()
            print("double clicked:", it.window_text())
            time.sleep(1.5)
            return True
    raise SystemExit("tree item lost")


if __name__ == "__main__":
    close_get_started()
    open_cell_in_library(CELL)
    print("done")
