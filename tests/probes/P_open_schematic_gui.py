"""P：查清"在 ADS 中打开原理图"到底能把窗口带到哪一步（需要 ADS 环境）。

背景：`open_schematic` 用的是**已实测**的 `db_uu.open_design(name, READ_ONLY)`
（与 run_simulation 同一个调用），所以"设计被打开到会话里"是确定的。
不确定的是"原理图窗口会不会自己弹到最前" —— 本插件目前只用 Qt 的
`windowTitle()` 做一次尽力而为的匹配（见 ads_ops._activate_window）。

这个脚本用来验证那个匹配到底有没有用，**不猜任何 Keysight API 名称**：

  1. 先打印当前所有顶层窗口标题；
  2. 打开指定设计；
  3. 再打印一次顶层窗口标题；
  4. 报告哪些标题里含 cell 名（也就是 _activate_window 会命中哪个）。

用法（在仓库根目录）::

    python tests/probe_via_ads.py tests/probes/P_open_schematic_gui.py

需要先在脚本里把 LIB / CELL / VIEW 改成你工作区里真实存在的设计。
"""

LIB, CELL, VIEW = "AI_lib", "Wilkinson_2G4_v2", "schematic"


def _titles():
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return ["(没有 QApplication 实例)"]
        out = []
        for w in app.topLevelWidgets():
            try:
                out.append(f"{type(w).__name__}: {w.windowTitle()!r}")
            except Exception as e:  # noqa: BLE001
                out.append(f"(读取标题失败 {type(e).__name__}: {e})")
        return sorted(out)
    except Exception as e:  # noqa: BLE001
        return [f"(Qt 不可用: {type(e).__name__}: {e})"]


print("=" * 70)
print("打开之前的顶层窗口：")
for line in _titles():
    print("   ", line)

print("-" * 70)
import keysight.ads.de.db_uu as db_uu  # noqa: E402

try:
    from keysight.ads.de.db_uu import DesignMode  # noqa: E402
except Exception:  # noqa: BLE001
    DesignMode = None

ref = f"{LIB}:{CELL}:{VIEW}"
print(f"尝试打开 {ref} …")
try:
    mode = DesignMode.READ_ONLY if DesignMode is not None else "ReadOnly"
    design = db_uu.open_design(ref, mode=mode)
    print("   open_design 成功，类型:", type(design).__name__)
except Exception as e:  # noqa: BLE001
    print("   open_design 失败:", type(e).__name__, e)
    raise SystemExit(1)

print("-" * 70)
print("打开之后的顶层窗口：")
after = _titles()
for line in after:
    print("   ", line)

hits = [t for t in after if CELL.lower() in t.lower()]
print("-" * 70)
print(f"标题里含 {CELL!r} 的窗口：{hits or '（一个都没有）'}")
print()
if hits:
    print("结论：ads_ops._activate_window 的标题匹配**能**找到原理图窗口。")
else:
    print("结论：标题匹配**找不到**原理图窗口 —— 也就是说面板上")
    print("      「在 ADS 中打开原理图」只能把设计打开到会话里，")
    print("      需要用户自己在 ADS 的 Library 树里双击该 cell。")
    print("      请把上面两份窗口标题贴回来，我据此改用可靠的定位方式。")
print()
print("（顺带确认：设计此刻是打开的，可以在 ADS 界面里找找它。）")
