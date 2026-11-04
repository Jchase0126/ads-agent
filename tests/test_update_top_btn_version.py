"""ADS 面板顶栏绿色按键「常驻显示当前版本」独立验证（集群成员 B / verify-ui）。

契约（唯一权威）：docs/顶栏绿色按键显示当前版本_接口契约_2026-11-04.md
  - §2 状态机表：初始 / checking / update_available / up_to_date / error / preparing / applied
  - §2 补充规则 2（点击分流）、3（样式不动）、5（setAccessibleName 同步）
  - §4.2 必增用例 1-8
  - 额外 3 条：preparing 禁用态、objectName/样式表未变、双面板实例状态不串

跑法：python tests/test_update_top_btn_version.py

本文件独立于 tests/test_update_ui.py：不 import 它、不依赖它的改名/增删。
每条断言的说明里都写明对应契约条款；失败时先判断是实现违规还是契约理解
分歧 —— 分歧必须回报 lead，不得在本文件里放宽断言。
"""

import contextlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    print("跳过：本机没有可用的 PySide6，无法构造面板控件。")
    sys.exit(2)

import panel  # noqa: E402
import pathbridge  # noqa: E402

APP = QApplication.instance() or QApplication([])

# 面板初始化会连后端读配置；启动时的静默更新检查会起真线程 —— 都与本文件无关，掐断。
# （与 tests/test_update_ui.py 独立做同样的隔离，不共享其代码。）
panel.AgentPanelWidget.reload_config = lambda self: None
panel.AgentPanelWidget._maybe_check_updates_on_start = lambda self: None


# ---------------------------------------------------------------- 基建
def make_panel(width=485, height=760):
    widget = panel.AgentPanelWidget()
    widget.resize(width, height)
    widget.show()
    APP.processEvents()
    return widget


def close(widget):
    try:
        widget.close()
    except BaseException:  # noqa: BLE001 - 清理失败不该影响结论
        pass
    try:
        APP.processEvents()
    except BaseException:  # noqa: BLE001
        pass


def local_plugin_version():
    """契约§3：本地版本只认 pathbridge.load().PLUGIN_VERSION，去掉可能的 v 前缀。"""
    try:
        raw = str(getattr(pathbridge.load(), "PLUGIN_VERSION", "") or "").strip()
    except BaseException:  # noqa: BLE001
        return ""
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    return raw


def expected_idle_text():
    """契约§2 初始/error 行：v{V}；V 取不到时兜底「检查更新」。"""
    version = local_plugin_version()
    return f"v{version}" if version else "检查更新"


@contextlib.contextmanager
def patched_panel_methods(*pairs):
    """临时替换 AgentPanelWidget 的方法，退出时恢复（测试之间不串补丁）。"""
    originals = []
    for name, replacement in pairs:
        originals.append((name, getattr(panel.AgentPanelWidget, name)))
        setattr(panel.AgentPanelWidget, name, replacement)
    try:
        yield
    finally:
        for name, original in reversed(originals):
            setattr(panel.AgentPanelWidget, name, original)


AVAILABLE = {
    "status": "update_available",
    "current_version": "1.3.1",
    "latest_version": "1.4.0",
    "message": "发现新的稳定版本。",
    "asset_name": "ADSAgent-1.4.0.zip",
}
UP_TO_DATE = {
    "status": "up_to_date",
    "current_version": "1.4.0",
    "latest_version": "1.4.0",
    "message": "当前版本不低于最新稳定版本，不会回退。",
}
OFFLINE = {"error": "后端未启动（[WinError 10061] 由于目标计算机积极拒绝，无法连接。）",
           "backend_down": True}


# ---------------------------------------------------------------- §4.2-1
def test_initial_button_visible_with_local_version():
    """§4.2-1 + §2「初始（未检查）」+ 补充规则 5：常驻可见、文案 v{V}、tooltip、读屏语义。"""
    widget = make_panel()
    try:
        btn = widget.update_top_btn
        expect = expected_idle_text()
        ok(btn.isVisible(),
           "§2 初始：按钮必须常驻可见（本波之后不得因「没有新版本」而隐藏）")
        eq(btn.text(), expect,
           "§2 初始：文案必须是 v{V}，V 取自 pathbridge.load().PLUGIN_VERSION")
        if expect != "检查更新":
            contains(btn.toolTip(), f"当前版本 v{expect[1:]}",
                     "§2 初始：tooltip 必须含「当前版本 v{V}」")
        contains(btn.toolTip(), "点击检查更新",
                 "§2 初始：tooltip 必须含「点击检查更新」")
        ok(btn.isEnabled(), "§2 初始：按钮可点")
        not_contains(btn.accessibleName(), "发现新版本",
                     "§2 补充规则 5：setAccessibleName 必须同步为当前语义，"
                     "不能再让读屏用户听到「发现新版本」")
        ok(btn.accessibleName().strip() != "",
           "§2 补充规则 5：setAccessibleName 不得为空")
    finally:
        close(widget)


# ---------------------------------------------------------------- §4.2-2
def test_up_to_date_uses_data_current_version():
    """§4.2-2 + §2「up_to_date」：数据里的 current_version 优先于本地常量。"""
    widget = make_panel()
    try:
        btn = widget.update_top_btn
        local = local_plugin_version()
        widget._on_update_checked(dict(UP_TO_DATE))  # current_version=1.4.0，本地一般 1.3.5
        eq(btn.text(), "v1.4.0",
           "§2 up_to_date：文案取数据 current_version（后端权威），不是本地 PLUGIN_VERSION")
        if local and local != "1.4.0":
            not_contains(btn.text(), local,
                         "§2 up_to_date：不得把本地版本号当作显示值")
        ok(btn.isVisible(), "§2 up_to_date：仍然可见，不得收起")
        contains(btn.toolTip(), "已是最新版本",
                 "§2 up_to_date：tooltip 必须含「已是最新版本」")
        ok(btn.isEnabled(), "§2 up_to_date：可点（点击应重新检查，见 T4）")
        not_contains(btn.text(), "一键更新",
                     "§2 up_to_date：无可用更新时不得显示「一键更新」")
    finally:
        close(widget)


# ---------------------------------------------------------------- §4.2-3
def test_update_available_click_triggers_prepare_chain():
    """§4.2-3 + §2「update_available」：文案带 latest，点击走 prepare（一键链路）。"""
    widget = make_panel()
    calls = {"prepare": 0, "check": 0}
    try:
        btn = widget.update_top_btn
        widget._on_update_checked(dict(AVAILABLE))
        eq(btn.text(), "一键更新 v1.4.0",
           "§2 update_available：文案必须是「一键更新 v{latest}」")
        ok(btn.isVisible(), "§2 update_available：按钮可见")
        contains(btn.toolTip(), "有新版本 v1.4.0",
                 "§2 update_available：tooltip 必须含「有新版本 v{latest}」")
        contains(btn.toolTip(), "点击后自动下载",
                 "§2 update_available：tooltip 必须含「点击后自动下载、校验并更新」语义")

        def fake_prepare(self):
            calls["prepare"] += 1
            self._update_preparing = True

        def fake_check(self, silent=False, on_available=None):
            calls["check"] += 1

        with patched_panel_methods(("_prepare_update", fake_prepare),
                                   ("_check_updates", fake_check)):
            btn.click()
            APP.processEvents()

        eq(calls["prepare"], 1,
           "§4.2-3：有更新时点击必须走 prepare 链路（_prepare_update 恰 1 次）")
        eq(calls["check"], 0,
           "§2 补充规则 2：有可用更新时点击不得改成重新检查")
        ok(not btn.isEnabled(), "§2 preparing：下载中按钮必须禁用")
    finally:
        close(widget)


# ---------------------------------------------------------------- §4.2-4
def test_up_to_date_and_initial_click_recheck_without_prepare():
    """§4.2-4 + §2 补充规则 2：初始/up_to_date 点击 → 非静默重新检查，绝不 prepare。"""
    widget = make_panel()
    calls = {"prepare": 0, "check": 0, "silent": []}
    try:
        btn = widget.update_top_btn

        def fake_prepare(self):
            calls["prepare"] += 1

        def fake_check(self, silent=False, on_available=None):
            calls["check"] += 1
            calls["silent"].append(silent)

        with patched_panel_methods(("_prepare_update", fake_prepare),
                                   ("_check_updates", fake_check)):
            # 4a 初始（未检查）点击：契约§2 补充规则 2「其它状态 → 重新检查」
            btn.click()
            APP.processEvents()
            eq(calls["check"], 1, "§2 补充规则 2：初始状态点击必须重新检查")
            eq(calls["silent"], [False],
               "§2 补充规则 2：点击必须走非静默检查 _check_updates(silent=False)")

            # 4b up_to_date 点击
            widget._on_update_checked(dict(UP_TO_DATE))
            btn.click()
            APP.processEvents()
            eq(calls["check"], 2, "§4.2-4：up_to_date 时点击必须触发重新检查")
            eq(calls["prepare"], 0, "§4.2-4：up_to_date 时点击绝不能触发 prepare")
            eq(calls["silent"], [False, False],
               "§4.2-4：重新检查必须是非静默的（用户主动点击要看结果）")

        ok(btn.isVisible(), "§4.2-4：点击后按钮仍可见")
    finally:
        close(widget)


# ---------------------------------------------------------------- §4.2-5
def test_error_and_offline_keep_version_and_recheck():
    """§4.2-5 + §2「error / 离线」：文案仍 v{V}、可点、点击触发重新检查。"""
    widget = make_panel()
    calls = {"prepare": 0, "check": 0}
    try:
        btn = widget.update_top_btn
        expect = expected_idle_text()

        # 5a 后端检查失败（status=error + message）
        widget._on_update_checked({
            "status": "error",
            # 后端回报的 current_version 应等于它自己读到的 PLUGIN_VERSION，
            # 这里动态取本地版本，避免版本号升级后夹具过期（v1.3.6 发布教训）。
            "current_version": local_plugin_version() or "1.3.5",
            "latest_version": "",
            "message": "后端未启动",
        })
        eq(btn.text(), expect, "§2 error：文案仍是本地当前版本 v{V}")
        ok(btn.isVisible(), "§2 error：仍然可见（绝不因失败而隐藏）")
        ok(btn.isEnabled(), "§2 error：按钮可点")
        contains(btn.toolTip(), "检查更新失败",
                 "§2 error：tooltip 必须含「检查更新失败」")
        not_contains(btn.text(), "一键更新",
                     "§2 error：没有可用更新时不得显示「一键更新」")

        # 5b 离线（后端未启动）
        widget._on_update_checked(dict(OFFLINE))
        eq(btn.text(), expect, "§2 error/离线：文案仍是 v{V}")
        ok(btn.isVisible(), "§2 error/离线：仍然可见")
        ok(btn.isEnabled(), "§2 error/离线：可点")

        def fake_check(self, silent=False, on_available=None):
            calls["check"] += 1

        def fake_prepare(self):
            calls["prepare"] += 1

        with patched_panel_methods(("_check_updates", fake_check),
                                   ("_prepare_update", fake_prepare)):
            btn.click()
            APP.processEvents()

        eq(calls["check"], 1, "§4.2-5：error/离线时点击必须触发重新检查")
        eq(calls["prepare"], 0, "§4.2-5：error/离线时点击绝不能触发 prepare")
    finally:
        close(widget)


# ---------------------------------------------------------------- §4.2-6
def test_local_version_unavailable_falls_back_to_check_updates():
    """§4.2-6 + §2 初始兜底 + 补充规则 4：V 取不到 → 「检查更新」且 never-raise。"""
    original_load = pathbridge.load
    original_paths = pathbridge.paths
    fake_paths = types.SimpleNamespace(config_path=lambda: os.devnull)

    def raising_load():
        raise RuntimeError("模拟 pathbridge.load 失败")

    # 6a patch pathbridge.load 抛异常
    try:
        pathbridge.load = raising_load
        pathbridge.paths = lambda create=False: fake_paths
        widget = make_panel()
        try:
            btn = widget.update_top_btn
            eq(btn.text(), "检查更新",
               "§4.2-6：本地版本取不到时文案必须兜底为「检查更新」（不得 hardcode 1.3.5）")
            ok(btn.isVisible(), "§4.2-6：兜底时按钮仍可见")
            ok(btn.isEnabled(), "§4.2-6：兜底时按钮可点")
            contains(btn.toolTip(), "点击检查更新",
                     "§4.2-6：兜底时 tooltip 仍应说明点击可检查")
            # never-raise：后续状态更新也不得因版本取不到而抛
            widget._on_update_checked({"status": "up_to_date",
                                       "current_version": "",
                                       "latest_version": "",
                                       "message": "已是最新"})
            ok(btn.isVisible(), "§4.2-6：V 为空时 up_to_date 仍不得隐藏按钮")
            ok(btn.text().strip() != "", "§4.2-6：V 为空时文案不得为空")
        finally:
            close(widget)

        # 6b patch pathbridge.load 返回无 PLUGIN_VERSION 属性的对象
        pathbridge.load = lambda: types.SimpleNamespace()
        widget = make_panel()
        try:
            eq(widget.update_top_btn.text(), "检查更新",
               "§4.2-6：PLUGIN_VERSION 属性缺失时文案同样兜底为「检查更新」")
            ok(widget.update_top_btn.isVisible(), "§4.2-6：属性缺失时按钮仍可见")
        finally:
            close(widget)
    finally:
        pathbridge.load = original_load
        pathbridge.paths = original_paths


# ---------------------------------------------------------------- §4.2-7
def test_applied_shows_applied_version():
    """§4.2-7 + §2「applied」：更新完成显示 v{applied_version}，仍可见可点。"""
    widget = make_panel()
    try:
        btn = widget.update_top_btn

        def fake_restart(self):
            return None

        with patched_panel_methods(("_restart_backend_async", fake_restart)):
            widget._on_update_applied({
                "status": "applied",
                "applied_version": "1.4.0",
                "message": "已应用更新",
                "files": 3,
            })
            APP.processEvents()

        eq(btn.text(), "v1.4.0",
           "§2 applied：文案必须变成 v{applied_version}")
        ok(btn.isVisible(), "§2 applied：更新完成后按钮仍常驻可见（不得收起）")
        ok(btn.isEnabled(), "§2 applied：更新完成后可点")
        contains(btn.toolTip(), "已更新到 v1.4.0",
                 "§2 applied：tooltip 必须含「已更新到 v{...}」")
    finally:
        close(widget)


# ---------------------------------------------------------------- §4.2-8
def test_garbage_payloads_never_raise_keep_button():
    """§4.2-8 + 补充规则 4：垃圾数据 never-raise，按钮仍可见且文案非空。"""
    widget = make_panel()
    try:
        btn = widget.update_top_btn
        for payload in ({}, {"status": 123}, None):
            widget._on_update_checked(payload)  # 抛异常即本用例失败 = never-raise 被破坏
            APP.processEvents()
            ok(btn.isVisible(),
               f"§4.2-8：垃圾数据 {payload!r} 后按钮仍必须可见")
            ok(btn.text().strip() != "",
               f"§4.2-8：垃圾数据 {payload!r} 后文案不得为空")
    finally:
        close(widget)


# ---------------------------------------------------------------- 额外 1：preparing 禁用态
def test_preparing_disables_button_and_shows_downloading():
    """§2「preparing（下载中）」：可见但禁用、文案「正在下载更新…」、期间不再响应点击。"""
    widget = make_panel()
    calls = {"prepare": 0}
    try:
        btn = widget.update_top_btn
        widget._on_update_checked(dict(AVAILABLE))

        def fake_prepare(self):
            calls["prepare"] += 1
            self._update_preparing = True

        with patched_panel_methods(("_prepare_update", fake_prepare)):
            btn.click()
            APP.processEvents()
            ok(not btn.isEnabled(), "§2 preparing：下载中按钮必须禁用")
            eq(btn.text(), "正在下载更新…", "§2 preparing：文案沿用「正在下载更新…」")
            ok(btn.isVisible(), "§2 preparing：下载中按钮仍可见（不隐藏）")
            # §2 补充规则 2：preparing 期间不响应点击
            btn.click()
            APP.processEvents()
            eq(calls["prepare"], 1,
               "§2 补充规则 2：preparing 期间再次点击不得重复触发 prepare")
    finally:
        close(widget)


# ---------------------------------------------------------------- 额外 2：身份/样式不被改
def test_button_identity_and_dual_state_stylesheet():
    """§2 补充规则 3（2026-11-04 用户反馈修订）：objectName 仍是 updateTopBtn；
    样式改为**双态**——默认扁平文字态（透明背景、无绿色，与品牌字观感一致），
    发现新版本切绿色胶囊态（#22C55E + border-radius 圆弧），回到无更新态恢复扁平。
    （旧断言「样式表恒为绿色不变」已按用户需求修订，非放宽安全断言。）"""
    widget = make_panel()
    try:
        btn = widget.update_top_btn
        eq(btn.objectName(), "updateTopBtn",
           "§5：按钮 objectName 必须仍是 updateTopBtn（样式表按它选择）")
        flat = btn.styleSheet()
        not_contains(flat, "#22C55E",
                     "双态样式：默认（未发现新版本）必须是扁平文字态，不得带绿色")
        contains(flat, "transparent",
                 "双态样式：默认态背景必须透明（与「ADS Agent」品牌字观感一致）")
        widget._on_update_checked(dict(AVAILABLE))
        green = btn.styleSheet()
        ok(green != flat,
           "双态样式：发现新版本后样式必须切换（不再与默认态相同）")
        contains(green, "#22C55E",
                 "发现新版本：必须切换为绿色胶囊态（#22C55E 主色不得改动）")
        contains(green, "border-radius",
                 "发现新版本：绿色胶囊必须有圆角（border-radius），四周带弧形")
        widget._on_update_checked(dict(UP_TO_DATE))
        eq(btn.styleSheet(), flat,
           "回到无更新态：必须恢复扁平文字态样式（绿色只在有可用更新时出现）")
    finally:
        close(widget)


# ---------------------------------------------------------------- 额外 3：双面板不串状态
def test_two_panels_do_not_share_state():
    """状态标记必须是实例级：一个面板的检查结果不得改写另一个面板的按钮。"""
    first = make_panel()
    second = make_panel()
    try:
        first._on_update_checked(dict(AVAILABLE))
        second._on_update_checked(dict(UP_TO_DATE))
        eq(first.update_top_btn.text(), "一键更新 v1.4.0",
           "第一个面板发现新版本后应显示「一键更新 v1.4.0」")
        eq(second.update_top_btn.text(), "v1.4.0",
           "第二个面板只在 up_to_date 状态，不得被第一个面板串成「一键更新」")
        first._on_update_checked(dict(UP_TO_DATE))
        eq(second.update_top_btn.text(), "v1.4.0",
           "第一个面板再次检查后，第二个面板文案不得被连带改写")
        ok(second.update_top_btn.isVisible(),
           "第二个面板按钮应保持常驻可见")
    finally:
        close(first)
        close(second)


# ---------------------------------------------------------------- 额外 4：prepare 复检为最新
def test_prepare_up_to_date_restores_clickable_version_button():
    """§2「up_to_date」行：prepare 期间后端复检为「已是最新」时，按钮必须复位成
    v{current} 并恢复可点；再点击应触发重新检查，而不是重复 prepare。

    可达性依据：backend/update_service.py:346 `_prepare_locked()` 在 :350 重新
    `_check()`；:351-352 一旦 `status != "update_available"` 就直接返回，而
    `_check()` 可以返回 up_to_date（backend/update_service.py:142）。因此用户点了
    「一键更新」之后后端复检说「其实已是最新」是真实可达的状态，不是假想分支。
    """
    widget = make_panel()
    calls = {"prepare": 0, "check": 0}
    try:
        btn = widget.update_top_btn
        widget._on_update_checked(dict(AVAILABLE))

        def fake_prepare(self):
            calls["prepare"] += 1
            self._update_preparing = True

        def fake_check(self, silent=False, on_available=None):
            calls["check"] += 1

        with patched_panel_methods(("_prepare_update", fake_prepare)):
            btn.click()          # _apply_update_now：禁用 + 「正在下载更新…」
            APP.processEvents()
            ok(not btn.isEnabled(),
               "前置条件：点击「一键更新」后应先进入 preparing 禁用态")

            widget._on_update_prepared({
                "status": "up_to_date",
                "current_version": "1.4.0",
                "latest_version": "1.4.0",
                "message": "当前版本不低于最新稳定版本，不会回退。",
            })
            APP.processEvents()

            local = local_plugin_version()
            acceptable = {"v1.4.0"}          # §3：数据 current_version 优先
            if local:
                acceptable.add(f"v{local}")  # 允许实现回退到本地版本
            ok(btn.text().strip() in acceptable,
               "§2 up_to_date：复检为最新后文案必须回到当前版本号，"
               f"实际 {btn.text()!r}（可接受 {sorted(acceptable)}）")
            ok(btn.isEnabled(),
               "§2 up_to_date：复检为最新后按钮必须恢复可点（严禁卡在禁用态）")
            contains(btn.toolTip(), "当前版本",
                     "§2 up_to_date：tooltip 必须回到「当前版本」口径")
            not_contains(btn.text(), "正在下载",
                         "§2 up_to_date：不得继续显示「正在下载更新…」")

            # 模拟真实时序：worker.finished → _gc_prepare_worker 已把 _update_preparing 复位
            widget._update_preparing = False
            with patched_panel_methods(("_check_updates", fake_check)):
                btn.click()
                APP.processEvents()
            eq(calls["check"], 1,
               "§2 补充规则 2：up_to_date 复位后再点击必须触发重新检查")
            eq(calls["prepare"], 1,
               "§2 补充规则 2：up_to_date 复位后再点击不得再次触发 prepare")
    finally:
        close(widget)


if __name__ == "__main__":
    sys.exit(run(globals(), "ADS 面板 · 顶栏按键版本号（B 独立）"))
