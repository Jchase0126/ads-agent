"""Q：调查 ADS 2027 是否提供原生 Data Display 的 Python API（需要 ADS 环境）。

为什么要调查：需求希望"如果可行，再生成可保存、可打开的 ADS 原生曲线页面"。
但**本插件当前不生成原生页面** —— 因为在本机没有验证过 Keysight 是否暴露
这套 API，而"不要猜测 API 名称"是硬要求。所以这里做的是**发现**而不是猜测：

  1. 遍历 keysight 包里所有子模块，打印名字里带
     display / dds / plot / graph / chart 的候选；
  2. 打印 keysight.ads 下的全部子模块（人工扫一遍）；
  3. 打印 de.app.window 里带 window/view/display 的成员名；
  4. 对候选模块打印它们的公开成员名。

它**不调用**任何未经确认的方法，只做只读的枚举与打印。

用法（在仓库根目录）::

    python tests/probe_via_ads.py tests/probes/Q_data_display_api.py

把输出贴回来，就能据此决定：
  * 若确实存在可用的原生 API -> 实现"另存/打开 ADS 原生曲线页面"；
  * 若不存在或不可用   -> 明确记录"原生页面功能未验证/不可用"，
    以插件内结果页（result_page.py）为唯一交付物。
"""

import importlib
import pkgutil
import sys

KEYS = ("display", "dds", "plot", "graph", "chart")

print("=" * 70)
print("1) keysight 包里名字含", KEYS, "的子模块")
print("-" * 70)
try:
    import keysight  # noqa: F401

    found = []
    for mod in pkgutil.walk_packages(keysight.__path__, "keysight."):
        low = mod.name.lower()
        if any(k in low for k in KEYS):
            found.append(mod.name)
    for name in found or ["（没有找到候选模块）"]:
        print("   ", name)
except Exception as e:  # noqa: BLE001
    print("   遍历失败:", type(e).__name__, e)

print()
print("=" * 70)
print("2) keysight.ads 下的全部子模块（人工扫一眼有没有遗漏）")
print("-" * 70)
try:
    import keysight.ads as ads_pkg

    names = sorted(m.name for m in pkgutil.walk_packages(
        ads_pkg.__path__, "keysight.ads."))
    for name in names[:200]:
        print("   ", name)
    print(f"    （共 {len(names)} 个）")
except Exception as e:  # noqa: BLE001
    print("   遍历失败:", type(e).__name__, e)

print()
print("=" * 70)
print("3) keysight.ads.de.app.window 里与窗口/视图/显示有关的成员")
print("-" * 70)
try:
    window = importlib.import_module("keysight.ads.de.app.window")
    hits = [n for n in dir(window)
            if any(k in n.lower() for k in ("window", "view", "display", "plot", "page"))]
    for name in hits or ["（没有相关成员）"]:
        print("   ", name)
except Exception as e:  # noqa: BLE001
    print("   导入失败:", type(e).__name__, e)

print()
print("=" * 70)
print("4) 候选模块的公开成员")
print("-" * 70)
for name in found if "found" in dir() else []:
    try:
        mod = importlib.import_module(name)
    except Exception as e:  # noqa: BLE001
        print(f"   {name}: 导入失败 {type(e).__name__}: {e}")
        continue
    public = [n for n in dir(mod) if not n.startswith("_")]
    print(f"   {name}: {public[:40]}")

print()
print("=" * 70)
print("结论怎么用：")
print("  * 上面任一路径给出了可用的创建/保存页面对象 -> 告诉我名字，我实现原生页面；")
print("  * 都没有 -> 记录为「ADS 原生 Data Display Python API 未验证/不可用」，")
print("    结果页以插件内的 PySide6 图表为准（曲线数据来自真实 .ds）。")
print("  注意：不要在没跑过这个脚本的情况下凭记忆写 API 名。")
sys.stdout.flush()
