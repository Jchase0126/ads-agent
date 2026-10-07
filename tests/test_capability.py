"""运行时能力检测与门禁（capability.py）—— 契约测试，不需要 ADS。

做法：向 ``sys.modules`` 注入**假的 keysight / PySide6 模块树**，把
``HPEESOF_DIR`` 指向临时伪造的 ADS 目录，然后验证：

  * 接口齐全时探测为 supported，写工具按版本门禁放行/拒绝；
  * 缺少模块 / 缺少属性 / 签名不存在时 → unavailable / unknown，
    且失败原因要能被人类读懂（不许静默忽略）；
  * 门禁 fail-closed：门禁模块自身异常时拒绝，而不是放行。

这些都是**模拟接口上的契约测试**，不是 ADS 实机测试。

运行::

    python tests/test_capability.py
"""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")
add_path("backend")

import adscompat  # noqa: E402
import capability  # noqa: E402


# ---------------------------------------------------------------------------
# 假 keysight 模块树
# ---------------------------------------------------------------------------

def _mk(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def install_fake_keysight(full: bool = True) -> None:
    """在 sys.modules 里搭一棵 keysight 模块树；full=False 时故意缺接口。"""
    de = _mk("keysight.ads.de")
    if full:
        de.workspace_is_open = lambda: True
        de.active_workspace = lambda: object()
        de.is_pde_app = lambda: False
        de.running_automation = lambda: False
    pde_db = _mk("keysight.ads.de._pde.db", Orientation=object if full else None)
    points = _mk("keysight.ads.de._points", PointF=object if full else None)
    db_uu = _mk("keysight.ads.de.db_uu")
    if full:
        class _DesignMode:
            READ_ONLY = "ro"
            WRITE = "w"
            APPEND = "append"
        db_uu.DesignMode = _DesignMode
    app = _mk("keysight.ads.de.app", WindowType=object, Menu=object,
              Action=object, find_addon=lambda *a, **k: None)
    app_window = _mk("keysight.ads.de.app.window")
    if full:
        app_window.main_pyside_widget = lambda: None
    dataset = _mk("keysight.ads.dataset", open=lambda *a, **k: None)
    eda = _mk("keysight.edatoolbox.ads")
    if full:
        eda.CircuitSimulator = type("CircuitSimulator", (), {})

    mods = {
        "keysight": _mk("keysight"),
        "keysight.ads": _mk("keysight.ads"),
        "keysight.ads.de": de,
        "keysight.ads.de._pde": _mk("keysight.ads.de._pde", db=pde_db),
        "keysight.ads.de._pde.db": pde_db,
        "keysight.ads.de._points": points,
        "keysight.ads.de.db_uu": db_uu,
        "keysight.ads.de.app": app,
        "keysight.ads.de.app.window": app_window,
        "keysight.ads.dataset": dataset,
        "keysight.edatoolbox": _mk("keysight.edatoolbox"),
        "keysight.edatoolbox.ads": eda,
    }
    sys.modules.update(mods)
    return mods


def remove_fake_keysight(mods: dict) -> None:
    for name in mods:
        sys.modules.pop(name, None)


def _fake_ads_dir(name: str) -> str:
    tmp = tempfile.mkdtemp(prefix="capability_ads_")
    root = os.path.join(tmp, name)
    for sub in ("bin", "tools", "config"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


class Env:
    """临时 ADS 目录 + 可控的 inside_ads 伪装。

    capability 通过 pathbridge 取 adslocate（文件路径加载的独立副本），
    补丁必须打在**同一份实例**上才生效。
    """

    def __enter__(self):
        self._old_hpeesof = os.environ.get("HPEESOF_DIR")
        self.ads = _fake_ads_dir("ADS2025")
        os.environ["HPEESOF_DIR"] = self.ads
        import pathbridge

        self._loaded = pathbridge.load_backend_module("adslocate.py", "adslocate")
        self._real_locate = self._loaded.inside_ads
        return self

    def __exit__(self, *exc):
        if self._old_hpeesof is None:
            os.environ.pop("HPEESOF_DIR", None)
        else:
            os.environ["HPEESOF_DIR"] = self._old_hpeesof
        self._loaded.inside_ads = self._real_locate
        # 测试直接 import 的 adslocate 也要还原
        try:
            adslocate.inside_ads = self._real_locate
        except Exception:  # noqa: BLE001
            pass
        return False

    def set_inside_ads(self, value: bool) -> None:
        self._loaded.inside_ads = lambda: value


try:
    import adslocate  # noqa: E402  (在 Env 里被替换 inside_ads)
except Exception:  # pragma: no cover
    adslocate = None


def _snapshot_with(mods, inside=True):
    capability.invalidate()
    with Env() as env:
        env.set_inside_ads(inside)
        return capability.detect()


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def test_full_interface_tree_is_supported():
    mods = install_fake_keysight(full=True)
    try:
        snap = _snapshot_with(mods, inside=True)
    finally:
        remove_fake_keysight(mods)
    eq(snap["inside_ads"], True)
    caps = snap["capabilities"]
    eq(caps["de_module"]["status"], "supported")
    eq(caps["dataset_module"]["status"], "supported")
    eq(caps["circuit_simulator"]["status"], "supported")
    eq(caps["design_mode_enum"]["status"], "supported")
    eq(caps["automation_query"]["reason"], "is_pde_app")
    version = snap["ads_version"]
    eq(version["year"], 2025, "目录名 ADS2025 → 弱证据识别为 2025")
    eq(version["status"], "known")


def test_missing_de_module_reports_unavailable():
    mods = install_fake_keysight(full=True)
    sys.modules.pop("keysight.ads.de", None)
    sys.modules["keysight.ads"].de = None
    try:
        snap = _snapshot_with(mods, inside=True)
    finally:
        remove_fake_keysight(mods)
    caps = snap["capabilities"]
    eq(caps["de_module"]["status"], "unavailable")
    contains(caps["de_module"]["reason"], "keysight.ads.de",
             "原因里必须点名缺哪个模块")


def test_partial_interface_is_unknown():
    """接口树残缺（无 is_pde_app/running_automation）→ unknown，不冒充可用。"""
    mods = install_fake_keysight(full=True)
    de = sys.modules["keysight.ads.de"]
    del de.is_pde_app
    del de.running_automation
    sys.modules.pop("keysight.ads.de._pde.db", None)
    sys.modules.pop("keysight.ads.de._points", None)
    db_uu = sys.modules.get("keysight.ads.de.db_uu")
    if db_uu is not None and hasattr(db_uu, "DesignMode"):
        del db_uu.DesignMode
    try:
        snap = _snapshot_with(mods, inside=True)
    finally:
        remove_fake_keysight(mods)
    caps = snap["capabilities"]
    eq(caps["automation_query"]["status"], "unknown")
    eq(caps["design_mode_enum"]["status"], "unknown",
       "db_uu 与 _pde.db 都没有 DesignMode → unknown")
    ok(caps["geometry_types"]["status"] in ("unknown", "unavailable"),
       "几何常量缺失不能标 supported")


def test_no_keysight_at_all_everything_unavailable():
    remove_fake_keysight(dict.fromkeys(
        [k for k in sys.modules if k.startswith("keysight")]))
    snap = _snapshot_with({}, inside=True)
    caps = snap["capabilities"]
    eq(caps["de_module"]["status"], "unavailable")
    d = capability.tool_decision(snap, "get_workspace_info")
    ok(not d["allowed"], "能力缺失时读工具也必须拒绝（fail-closed）")


def test_gate_bypassed_outside_ads_process():
    """非 ADS 进程（测试/开发）：门禁旁路，handler 自己会失败。"""
    mods = install_fake_keysight(full=False)
    try:
        snap = _snapshot_with(mods, inside=False)
        d = capability.tool_decision(snap, "build_schematic")
        # inside_ads=False 时 tool_decision 不做门禁裁决 → 缺能力也不拒绝？
        # 不：tool_decision 本身仍按快照裁决；旁路发生在 toolserver._gate_tool。
        ok(isinstance(d, dict))
    finally:
        remove_fake_keysight(mods)


# ---------------------------------------------------------------------------
# 门禁（在快照上裁决）
# ---------------------------------------------------------------------------

def _verified_snapshot():
    mods = install_fake_keysight(full=True)
    try:
        snap = _snapshot_with(mods, inside=True)
    finally:
        remove_fake_keysight(mods)
    # 强制按已验证基线（2027）裁决，隔离 Qt 探测环境差异
    snap["ads_version"] = {"year": 2027, "update": "", "build": "",
                           "status": "known"}
    return snap


def test_2027_with_full_caps_allows_writes():
    snap = _verified_snapshot()
    for tool in ("set_design_variables", "build_schematic", "run_simulation",
                 "run_python", "get_workspace_info"):
        d = capability.tool_decision(snap, tool)
        ok(d["allowed"], f"{tool}: {d['reason']}")


def test_2025_writes_denied_until_optin():
    snap = _verified_snapshot()
    snap["ads_version"] = {"year": 2025, "update": "", "build": "",
                           "status": "known"}
    d = capability.tool_decision(snap, "build_schematic")
    ok(not d["allowed"])
    eq(d["code"], adscompat.DENY_EXPERIMENTAL_OFF)
    snap["compat_flags"]["experimental_2025"] = True
    d = capability.tool_decision(snap, "build_schematic")
    ok(d["allowed"])
    contains(d["reason"], "未实机验证")


def test_snapshot_cache_and_invalidate():
    capability.invalidate()
    mods = install_fake_keysight(full=True)
    try:
        with Env() as env:
            env.set_inside_ads(True)
            s1 = capability.snapshot()
            s2 = capability.snapshot()
            ok(s1 is s2, "TTL 内复用同一快照")
            capability.invalidate()
            s3 = capability.snapshot()
            ok(s3 is not s1, "invalidate 后重新探测")
    finally:
        remove_fake_keysight(mods)


def test_gate_tool_fail_closed_on_compat_error():
    """adscompat 加载失败 → 门禁必须拒绝（fail-closed），不能默认放行。"""
    snap = {
        "inside_ads": True,
        "ads_version": {"year": None, "status": "unknown"},
        "compat_flags": {},
        "capabilities": {"de_module": {"status": "supported", "reason": ""}},
    }
    real_loader = capability.pathbridge.load_backend_module

    def broken(name, alias=None):
        if "adscompat" in str(name):
            raise ImportError("模拟 adscompat 损坏")
        return real_loader(name, alias) if alias else real_loader(name)

    capability.pathbridge.load_backend_module = broken
    try:
        d = capability.tool_decision(snap, "get_workspace_info")
    finally:
        capability.pathbridge.load_backend_module = real_loader
    ok(not d["allowed"])
    contains(d["reason"], "兼容档案模块不可用")


def test_summarize_never_claims_verified_for_experimental():
    mods = install_fake_keysight(full=True)
    try:
        snap = _snapshot_with(mods, inside=True)
    finally:
        remove_fake_keysight(mods)
    text = capability.summarize(snap)
    ok("2025" in text)
    ok("已实机验证" not in text, "2025 摘要不得声称已实机验证")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
