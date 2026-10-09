"""兼容旧截图入口；唯一实现保留在 tests/probes/AB_grab.py。

支持直接运行以及 probe_via_ads 将脚本文本送入 ADS 执行。
"""
from pathlib import Path
import runpy

_entry = Path(globals().get("__file__", ""))
if _entry.name == "grab_design_window.py":
    _root = _entry.resolve().parent.parent
else:
    # ADS 的 run_python 执行文本时没有可靠的脚本 __file__。
    import panel
    _root = Path(panel.__file__).resolve().parents[2]

runpy.run_path(str(_root / "tests" / "probes" / "AB_grab.py"))
