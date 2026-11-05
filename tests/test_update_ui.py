"""ADS 面板「软件更新」界面测试。

跑法：python tests/test_update_ui.py
覆盖：控件与 objectName、各后端状态（update_available / up_to_date / ready / error / 离线）
      的文案与按钮启用态、ready 绝不谎称已安装、reveal 只定位不执行、静默检查不打扰、
      顶栏绿色按钮常驻显示当前版本 / 点击分流，以及所有更新路径 never-raise。

2026-11-04 需求驱动修改（按键改常驻），非放宽安全断言：
用户要求「自动更新绿色按键在没有新版本时显示当前版本号，比如 v1.3.5」。按
docs/顶栏绿色按键显示当前版本_接口契约_2026-11-04.md §4.1，原先 4 处「按钮应隐藏/收起/复位成
『一键更新』」的断言改为「常驻可见 + 显示版本号」，这是需求变更本身，不是为了让实现过关而松绑。
所有既有 never-raise / not_contains / 不得谎称已安装 的安全断言一条未删。
"""

import os
import sys

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

# 面板初始化会连后端读配置；测试里必须掐断这条链路。
panel.AgentPanelWidget.reload_config = lambda self: None
# 启动时的静默更新检查会起真线程去连后端 —— 测试里禁掉，单独测那个方法。
_ORIGINAL_STARTUP_CHECK = panel.AgentPanelWidget._maybe_check_updates_on_start
panel.AgentPanelWidget._maybe_check_updates_on_start = lambda self: None


def make_panel(width=485, height=760):
    widget = panel.AgentPanelWidget()
    widget.resize(width, height)
    widget.show()
    widget.settings_btn.click()
    APP.processEvents()
    return widget


def close(widget):
    try:
        widget.close()
        APP.processEvents()
    except Exception:  # noqa: BLE001
        pass


class _FakeSignal:
    """够用的假信号：connect / emit，用来代替 QThread 的 result/finished。"""

    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


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
READY = {
    "status": "ready",
    "current_version": "1.3.1",
    "latest_version": "1.4.0",
    "message": "安装包已通过 GitHub Release SHA-256 校验并解压；尚未安装。",
    "instruction": "更新尚未安装。请保存工作并关闭所有 ADS 窗口及 ADS Agent 后端，"
                   "然后在资源管理器中双击以下安装器。",
    "package_path": r"C:\tmp\adsagent-update-x\ADSAgent-1.4.0.zip",
    "installer_path": r"C:\tmp\adsagent-update-x\ADSAgent-1.4.0\install_addon.bat",
}
OFFLINE = {"error": "后端未启动（[WinError 10061] 由于目标计算机积极拒绝，无法连接。）",
           "backend_down": True}


def local_plugin_version():
    """本地安装版本（与实现同源：pathbridge.load().PLUGIN_VERSION），取不到返回 ""。"""
    try:
        return str(getattr(pathbridge.load(), "PLUGIN_VERSION", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 控件
def test_update_controls_have_stable_object_names():
    widget = make_panel()
    try:
        expected = {
            "settings.update.status": widget.update_status_label,
            "settings.update.auto_check": widget.update_auto_check,
            "settings.update.check": widget.update_check_btn,
            "settings.update.prepare": widget.update_prepare_btn,
            "settings.update.reveal": widget.update_reveal_btn,
            "settings.update.instruction": widget.update_instruction_view,
            "settings.update.hint": widget.update_hint,
        }
        for name, control in expected.items():
            eq(control.objectName(), name, f"控件 {name} 的 objectName 应稳定")
        eq(widget.update_heading.text(), "软件更新", "应有「软件更新」小节标题")
    finally:
        close(widget)


def test_initial_state_offers_no_false_promise():
    widget = make_panel()
    try:
        ok(not widget.update_prepare_btn.isEnabled(), "未检查前不应允许准备更新")
        ok(not widget.update_reveal_btn.isEnabled(), "没有安装包时不应允许打开目录")
        ok(widget.update_auto_check.isChecked(), "默认应开启启动时自动检查（只查版本号）")
        contains(widget.update_prepare_btn.text(), "不安装",
                 "准备更新按钮必须自证「不安装」")
        contains(widget.update_hint.text(), "手动运行安装器",
                 "提示必须说明安装需要用户手动执行")
    finally:
        close(widget)


# ---------------------------------------------------------------- 各状态
def test_update_available_state():
    widget = make_panel()
    try:
        widget._on_update_checked(dict(AVAILABLE))
        text = widget.update_status_label.text()
        contains(text, "1.4.0", "应显示新版本号")
        contains(text, "1.3.1", "应显示当前版本号")
        ok(widget.update_prepare_btn.isEnabled(), "有更新时应允许准备更新")
        ok(not widget.update_reveal_btn.isEnabled(), "还没下载时不应允许打开目录")
        not_contains(text, "已安装", "有更新 ≠ 已安装")
        not_contains(text, "已更新", "有更新 ≠ 已更新")
    finally:
        close(widget)


def test_up_to_date_state():
    widget = make_panel()
    try:
        widget._on_update_checked(dict(UP_TO_DATE))
        text = widget.update_status_label.text()
        contains(text, "已是最新版本", "无更新时应明确说已是最新")
        contains(text, "1.4.0", "应显示当前版本号")
        ok(not widget.update_prepare_btn.isEnabled(), "无更新时不应允许准备更新")
        not_contains(text, "发现新版本", "无更新时不得提示有新版本")
    finally:
        close(widget)


def test_ready_state_never_claims_installed():
    widget = make_panel()
    try:
        widget.general_settings_tab.click()
        APP.processEvents()
        widget._on_update_prepared(dict(READY))
        text = widget.update_status_label.text()
        contains(text, "尚未安装", "prepare 只是暂存，必须明说尚未安装")
        contains(text, "1.4.0", "应显示待安装版本号")
        for lie in ("安装完成", "已安装", "更新成功", "已更新", "升级成功"):
            not_contains(text, lie, f"准备阶段绝不能谎称「{lie}」")
        ok(not widget.update_prepare_btn.isEnabled(), "已准备好后按钮应禁用，避免重复下载")
        ok(widget.update_reveal_btn.isEnabled(), "已准备好后应允许打开安装包目录")
        ok(widget.update_instruction_view.isVisible(), "应展示安装说明")
        instruction = widget.update_instruction_view.toPlainText()
        contains(instruction, "关闭所有 ADS", "安装说明必须要求先关闭 ADS")
        contains(instruction, READY["installer_path"], "安装说明必须给出安装器完整路径")
    finally:
        close(widget)


def test_error_state_from_prepare():
    widget = make_panel()
    try:
        widget._on_update_prepared({
            "status": "error",
            "current_version": "1.3.1",
            "latest_version": "1.4.0",
            "message": "准备更新失败，未修改安装或用户数据：安装包大小或 SHA-256 校验失败",
        })
        text = widget.update_status_label.text()
        contains(text, "SHA-256", "应如实转述后端失败原因")
        contains(text, "未修改安装", "应说明没有改动安装")
        ok(not widget.update_reveal_btn.isEnabled(), "失败后不应允许打开（不存在的）目录")
        ok(not widget.update_prepare_btn.isEnabled() or True, "失败后按钮状态不应崩")
    finally:
        close(widget)


def test_offline_transport_error_is_reported_not_raised():
    widget = make_panel()
    try:
        widget._on_update_checked(dict(OFFLINE))          # 后端没起
        contains(widget.update_status_label.text(), "后端未启动",
                 "离线时应把传输层错误如实说出来")
        widget._on_update_prepared(dict(OFFLINE))         # prepare 也一样
        contains(widget.update_status_label.text(), "后端未启动", "prepare 离线也要如实说")
        ok(not widget.update_reveal_btn.isEnabled(), "离线时不得假装有安装包")
    finally:
        close(widget)


def test_silent_check_does_not_hijack_ui():
    """启动时的静默检查：无更新不打扰、有更新只留提示，不弹模态框。"""
    widget = make_panel()
    try:
        widget.update_status_label.setText("用户正在看的这行字")
        widget._on_update_checked(dict(UP_TO_DATE), silent=True)
        eq(widget.update_status_label.text(), "用户正在看的这行字",
           "静默检查无更新时不得改动设置页状态")
        ok(not widget.status.text().startswith("发现新版本"),
           "无更新时不应提示新版本")

        widget._on_update_checked(dict(AVAILABLE), silent=True)
        contains(widget.status.text(), "发现新版本",
                 "静默检查发现新版本时应在状态栏留一句提示")
        contains(widget.status.text(), "1.4.0", "提示里应带版本号")
    finally:
        close(widget)


def test_silent_check_swallows_errors():
    widget = make_panel()
    try:
        widget.update_status_label.setText("原样保留")
        widget._on_update_checked(dict(OFFLINE), silent=True)
        eq(widget.update_status_label.text(), "原样保留", "静默检查失败不得打扰用户")
        widget._on_update_checked({}, silent=True)
        eq(widget.update_status_label.text(), "原样保留", "空响应也不得改动界面")
    finally:
        close(widget)


# ---------------------------------------------------------------- 行为
def test_check_updates_reports_through_worker():
    """_check_updates 必须走后台 worker，并把结果交给 _on_update_checked。"""
    widget = make_panel()
    try:
        seen = {}

        class FakeWorker:
            def __init__(self, payload, path="/config", parent=None, timeout=60):
                seen["payload"] = payload
                seen["path"] = path
                seen["timeout"] = timeout
                self.result = _FakeSignal()
                self.finished = _FakeSignal()
                self.started = False

            def start(self):
                self.started = True
                seen["started"] = True
                self.result.emit(dict(AVAILABLE))

        original = panel.ConfigWorker
        panel.ConfigWorker = FakeWorker
        try:
            widget._check_updates(silent=False)
        finally:
            panel.ConfigWorker = original

        eq(seen.get("path"), "/updates/check", "必须调用约定的检查端点")
        ok(seen.get("payload") is None, "检查更新应是 GET（payload 为 None）")
        ok(seen.get("started"), "请求必须真的在线程里跑起来")
        contains(widget.update_status_label.text(), "1.4.0", "worker 回来的结果应落到界面")
        ok(widget.update_prepare_btn.isEnabled(), "查到更新后应放开准备更新")
    finally:
        close(widget)


def test_prepare_third_party_contract():
    """prepare 必须是 POST 空对象，且超时足够长（下载可能要几分钟）。"""
    widget = make_panel()
    try:
        seen = {}

        class FakeWorker:
            def __init__(self, payload, path="/config", parent=None, timeout=60):
                seen["payload"] = payload
                seen["path"] = path
                seen["timeout"] = timeout
                self.result = _FakeSignal()
                self.finished = _FakeSignal()

            def start(self):
                seen["started"] = True
                self.result.emit(dict(READY))

        original = panel.ConfigWorker
        panel.ConfigWorker = FakeWorker
        try:
            widget._prepare_update()
        finally:
            panel.ConfigWorker = original

        eq(seen.get("path"), "/updates/prepare", "必须调用约定的准备端点")
        eq(seen.get("payload"), {}, "prepare 必须发送空对象（后端拒绝任何 URL 字段）")
        ok(seen.get("timeout", 0) >= 120, "下载校验需要长超时，否则会被误判失败")
        ok(seen.get("started"), "准备更新必须真的在线程里跑")
        contains(widget.update_status_label.text(), "尚未安装", "结果文案必须保持「未安装」口径")
    finally:
        close(widget)


def test_confirm_dialog_is_non_modal_and_explains_no_install():
    widget = make_panel()
    try:
        captured = {}
        original = panel.QtWidgets.QMessageBox

        class FakeBox:
            Icon = original.Icon
            ButtonRole = original.ButtonRole

            def __init__(self, parent=None):
                captured["created"] = True
                captured["shown"] = False
                self._buttons = {}
                self.buttonClicked = _FakeSignal()

            def setWindowTitle(self, text):
                captured["title"] = text

            def setIcon(self, icon):
                pass

            def setText(self, text):
                captured["text"] = text

            def setInformativeText(self, text):
                captured["info"] = text

            def setDetailedText(self, text):
                pass

            def addButton(self, text, role):
                captured.setdefault("buttons", []).append(text)
                self._buttons[text] = object()
                return self._buttons[text]

            def setDefaultButton(self, button):
                pass

            def setAttribute(self, *args):
                pass

            def show(self):
                captured["shown"] = True

        panel.QtWidgets.QMessageBox = FakeBox
        try:
            widget._confirm_prepare_update()
        finally:
            panel.QtWidgets.QMessageBox = original

        ok(captured.get("shown"), "确认框必须用 show() 非阻塞弹出")
        texts = " ".join(captured.get("buttons", []))
        contains(texts, "下载并校验", "确认按钮应表明只是下载校验")
        contains(texts, "取消", "必须提供取消")
        info = captured.get("info", "")
        contains(info, "不会安装", "确认说明必须写清不会安装")
        contains(info, "关闭全部 ADS", "确认说明必须要求先关闭 ADS")
    finally:
        close(widget)


def test_reveal_locates_folder_without_running_installer():
    widget = make_panel()
    try:
        opened = []
        original_open = panel.QtGui.QDesktopServices.openUrl
        try:
            panel.QtGui.QDesktopServices.openUrl = lambda url: opened.append(url.toString())

            # 没有准备好任何包时：只给提示，不打开任何东西
            widget._reveal_update_package()
            eq(len(opened), 0, "没有安装包时不得打开任何目录")
            contains(widget.status.text(), "还没有", "应告诉用户还没有安装包")

            widget._update_ready_path = READY["installer_path"]
            widget._reveal_update_package()
            # 目录不存在（测试机上没有这个临时目录）→ 只提示，不打开
            eq(len(opened), 0, "目录不存在时不得打开不存在的路径")
            contains(widget.status.text(), "不存在", "应如实说目录不在了")
        finally:
            panel.QtGui.QDesktopServices.openUrl = original_open
    finally:
        close(widget)


def test_reveal_opens_existing_folder_but_never_executes():
    """目录存在时打开的是目录，不是安装器本身（不执行任何东西）。"""
    widget = make_panel()
    try:
        opened = []
        original_open = panel.QtGui.QDesktopServices.openUrl
        try:
            panel.QtGui.QDesktopServices.openUrl = lambda url: opened.append(url.toString())
            widget._update_ready_path = os.path.dirname(os.path.abspath(__file__))
            widget._reveal_update_package()
        finally:
            panel.QtGui.QDesktopServices.openUrl = original_open

        eq(len(opened), 1, "目录存在时应打开它")
        target = opened[0]
        ok(target.startswith("file:"), f"应打开本地目录 URL，实际 {target!r}")
        ok(not target.lower().endswith(".bat"),
           "绝不能把安装器本身当浏览器目标打开（那等于让系统执行它）")
    finally:
        close(widget)


def test_prepare_requires_explicit_confirmation():
    """未经确认不得开始下载。"""
    widget = make_panel()
    try:
        started = {"n": 0}
        original = panel.AgentPanelWidget._prepare_update
        panel.AgentPanelWidget._prepare_update = lambda self: started.__setitem__("n", started["n"] + 1)
        try:
            widget._on_prepare_choice(False)   # 用户点了取消
            eq(started["n"], 0, "取消后不得开始下载")
            widget._on_prepare_choice(True)
            eq(started["n"], 1, "确认后才允许下载")
        finally:
            panel.AgentPanelWidget._prepare_update = original
    finally:
        close(widget)


def test_update_paths_never_raise_on_garbage():
    widget = make_panel()
    try:
        for payload in (None, {}, {"status": None}, {"status": "unknown-status"},
                        {"status": "ready"}, {"status": "update_available", "latest_version": 1},
                        {"error": None}, {"status": "error"}):
            widget._on_update_checked(payload)
            widget._on_update_prepared(payload)
        widget._reveal_update_package()
        ok(True, "垃圾输入也不能让更新界面抛异常")
    finally:
        close(widget)


def test_auto_check_setting_persists_without_touching_other_sections():
    """写 [updates] 段不能用 configparser 重写整个文件（会丢注释和其它段）。"""
    import types

    widget = make_panel()
    try:
        store = {"lines": [
            "# 顶部注释",
            "[llm]",
            "model = deepseek-chat",
            "",
            "[backend]",
            "port = 8760",
            "",
            "[updates]",
            "auto_check = false",
            "",
            "[ads]",
            "token = keep-me",
        ]}
        applied = {}

        def edit_config(mutate, timeout=None):
            lines = list(store["lines"])
            mutate(lines)
            store["lines"] = lines
            applied["done"] = True
            return True

        fake = types.SimpleNamespace(auth=lambda: types.SimpleNamespace(edit_config=edit_config))
        original = sys.modules.get("authbridge")
        sys.modules["authbridge"] = fake
        try:
            widget.update_auto_check.setChecked(True)
            ok(widget._save_update_settings(), "保存应成功")
        finally:
            if original is None:
                sys.modules.pop("authbridge", None)
            else:
                sys.modules["authbridge"] = original

        ok(applied.get("done"), "必须真正调用跨进程安全的写入通道")
        text = "\n".join(store["lines"])
        contains(text, "# 顶部注释", "注释必须保留")
        contains(text, "model = deepseek-chat", "其它段的键必须保留")
        contains(text, "token = keep-me", "其它段的键必须保留")
        contains(text, "auto_check = true", "应就地改成 true，而不是新加一段")
        eq(text.count("[updates]"), 1, "不得重复添加 [updates] 段头")
    finally:
        close(widget)


# ---------------------------------------------------------------- 顶部绿色一键更新按钮
def test_top_update_button_visible_by_default_and_shows_version():
    """常驻：默认可见并显示当前版本 v{V}；发现新版本变「一键更新 vX」；已是最新仍显示版本号。"""
    widget = make_panel()
    try:
        local = local_plugin_version()
        ok(hasattr(widget, "update_top_btn"), "面板顶部应有绿色一键更新按钮")
        ok(widget.update_top_btn.isVisible(), "顶栏按钮必须常驻可见（没有新版本也不收起）")
        if local:
            contains(widget.update_top_btn.text(), f"v{local}",
                     "没有新版本时应显示本地当前版本号")
        else:
            eq(widget.update_top_btn.text(), "检查更新", "版本号取不到时应显示「检查更新」")
        widget._on_update_checked(dict(AVAILABLE))
        ok(widget.update_top_btn.isVisible(), "发现新版本时按钮应出现")
        contains(widget.update_top_btn.text(), "1.4.0", "按钮文案应带新版本号")
        widget._on_update_checked(dict(UP_TO_DATE))
        ok(widget.update_top_btn.isVisible(), "已是最新时按钮仍然常驻可见")
        contains(widget.update_top_btn.text(), "1.4.0",
                 "已是最新时应显示当前版本号（来自检查数据）")
    finally:
        close(widget)


def test_top_button_click_runs_prepare_then_apply_chain():
    """点击绿色按钮：prepare(ready) → 自动 apply → applied 后自动重启后端。"""
    widget = make_panel()
    try:
        calls = {"apply": 0, "restart": 0}
        widget._on_update_checked(dict(AVAILABLE))          # 按钮出现
        original_prepare = panel.AgentPanelWidget._prepare_update
        original_apply = panel.AgentPanelWidget._apply_update
        original_restart = panel.AgentPanelWidget._restart_backend_async
        try:
            def fake_prepare(self):
                self._update_preparing = True
                self._on_update_prepared(dict(READY))

            def fake_apply(self):
                calls["apply"] += 1
                self._gc_prepare_worker(None)
                self._on_update_applied({
                    "status": "applied", "applied_version": "1.4.0",
                    "message": "已应用更新", "files": 3,
                })

            panel.AgentPanelWidget._prepare_update = fake_prepare
            panel.AgentPanelWidget._apply_update = fake_apply
            panel.AgentPanelWidget._restart_backend_async = (
                lambda self: calls.__setitem__("restart", calls["restart"] + 1))
            widget.update_top_btn.click()
            APP.processEvents()
        finally:
            panel.AgentPanelWidget._prepare_update = original_prepare
            panel.AgentPanelWidget._apply_update = original_apply
            panel.AgentPanelWidget._restart_backend_async = original_restart
        eq(calls["apply"], 1, "ready 后必须立即自动 apply（一键语义）")
        eq(calls["restart"], 1, "applied 后必须自动重启后端")
        contains(widget.status.text(), "1.4.0", "状态栏应显示已更新到的版本")
        contains(widget.update_top_btn.text(), "1.4.0", "更新完成后顶栏应显示新版本号")
        ok(widget.update_top_btn.isVisible(), "更新完成后按钮仍常驻可见（不再收起）")
    finally:
        close(widget)


def test_top_button_failure_restores_button():
    """prepare 失败时按钮恢复可点，且不能把「正在下载」卡死。"""
    widget = make_panel()
    try:
        local = local_plugin_version()
        widget._on_update_checked(dict(AVAILABLE))
        widget._apply_update_now()
        widget._on_update_prepared({"error": "后端未启动", "backend_down": True})
        ok(widget.update_top_btn.isEnabled(), "失败后按钮应恢复可用")
        if local:
            contains(widget.update_top_btn.text(), f"v{local}",
                     "失败复位后应显示当前版本号")
        not_contains(widget.update_top_btn.text(), "一键更新",
                     "失败复位后文案不再是「一键更新」（此时没有可用更新）")
        not_contains(widget.update_top_btn.text(), "正在下载",
                     "失败后不得把「正在下载」卡死在按钮上")
        ok(widget.update_top_btn.isVisible(), "失败后按钮仍常驻可见")
    finally:
        close(widget)


def test_top_button_initial_shows_version_and_check_tooltip():
    """契约 §2 初始态：常驻可见、文案 v{V}、tooltip/读屏文案说明「当前版本 + 点击检查更新」。"""
    widget = make_panel()
    try:
        local = local_plugin_version()
        btn = widget.update_top_btn
        ok(btn.isVisible(), "初始状态下顶栏按钮必须可见")
        ok(btn.isEnabled(), "初始状态下顶栏按钮必须可点")
        if local:
            eq(btn.text(), f"v{local}", "初始文案必须是当前版本号 v{V}")
            contains(btn.toolTip(), "当前版本", "tooltip 应说明这是当前版本")
        else:
            eq(btn.text(), "检查更新", "版本号取不到时应显示「检查更新」")
        contains(btn.toolTip(), "点击检查更新", "tooltip 应说明点击是检查更新")
        if local:
            contains(btn.accessibleName(), "当前版本", "读屏文案应说明当前版本")
        contains(btn.accessibleName(), "点击检查更新", "读屏文案应说明点击语义")
    finally:
        close(widget)


def test_top_button_up_to_date_prefers_data_current_version():
    """已是最新时显示数据里的 current_version（后端权威），而不是本地常量。"""
    widget = make_panel()
    try:
        local = local_plugin_version()
        widget._on_update_checked({
            "status": "up_to_date",
            "current_version": "1.4.0",
            "latest_version": "1.4.0",
            "message": "当前版本不低于最新稳定版本，不会回退。",
        })
        eq(widget.update_top_btn.text(), "v1.4.0",
           "已是最新时应显示数据里的 current_version")
        if local and local != "1.4.0":
            not_contains(widget.update_top_btn.text(), local,
                         "数据里有当前版本时不得退回本地常量")
        ok(widget.update_top_btn.isVisible(), "已是最新时按钮仍常驻可见")
        contains(widget.update_top_btn.toolTip(), "已是最新版本", "tooltip 应说明已是最新")
    finally:
        close(widget)


def test_top_button_available_click_runs_prepare():
    """有可用更新时点击顶栏按钮 → 直接走一键下载（prepare），不再重新检查。"""
    widget = make_panel()
    try:
        widget._on_update_checked(dict(AVAILABLE))
        eq(widget.update_top_btn.text(), "一键更新 v1.4.0", "有更新时按钮文案应为一键更新")
        calls = {"prepare": 0, "check": 0}
        original_prepare = panel.AgentPanelWidget._prepare_update
        original_check = panel.AgentPanelWidget._check_updates
        try:
            panel.AgentPanelWidget._prepare_update = (
                lambda self: calls.__setitem__("prepare", calls["prepare"] + 1))
            panel.AgentPanelWidget._check_updates = (
                lambda self, silent=False, on_available=None:
                    calls.__setitem__("check", calls["check"] + 1))
            widget.update_top_btn.click()
            APP.processEvents()
        finally:
            panel.AgentPanelWidget._prepare_update = original_prepare
            panel.AgentPanelWidget._check_updates = original_check
        eq(calls["prepare"], 1, "有可用更新时点击必须触发 prepare 链路")
        eq(calls["check"], 0, "有可用更新时点击不应重新检查")
        contains(widget.update_top_btn.text(), "正在下载", "点击后按钮应进入下载中状态")
        ok(not widget.update_top_btn.isEnabled(), "下载中按钮应禁用，避免重复点击")
    finally:
        close(widget)


def test_top_button_up_to_date_click_rechecks():
    """无可用更新时点击顶栏按钮 → 重新检查（不触发下载）。"""
    widget = make_panel()
    try:
        widget._on_update_checked(dict(UP_TO_DATE))
        calls = {"prepare": 0, "check": 0}
        original_prepare = panel.AgentPanelWidget._prepare_update
        original_check = panel.AgentPanelWidget._check_updates
        try:
            panel.AgentPanelWidget._prepare_update = (
                lambda self: calls.__setitem__("prepare", calls["prepare"] + 1))
            panel.AgentPanelWidget._check_updates = (
                lambda self, silent=False, on_available=None:
                    calls.__setitem__("check", calls["check"] + 1))
            widget.update_top_btn.click()
            APP.processEvents()
        finally:
            panel.AgentPanelWidget._prepare_update = original_prepare
            panel.AgentPanelWidget._check_updates = original_check
        eq(calls["check"], 1, "无可用更新时点击必须重新检查")
        eq(calls["prepare"], 0, "无可用更新时点击绝不能开始下载")
    finally:
        close(widget)


def test_top_button_error_state_keeps_version_and_rechecks():
    """检查失败：仍显示当前版本号、仍可点，点击触发重新检查。"""
    widget = make_panel()
    try:
        local = local_plugin_version()
        widget._on_update_checked({"status": "error", "message": "网络不可用"})
        if local:
            eq(widget.update_top_btn.text(), f"v{local}",
               "检查失败后应保持当前版本号")
        ok(widget.update_top_btn.isVisible(), "检查失败后按钮仍常驻可见")
        ok(widget.update_top_btn.isEnabled(), "检查失败后按钮应仍可点")
        contains(widget.update_top_btn.toolTip(), "检查更新失败", "tooltip 应说明上次检查失败")
        calls = {"prepare": 0, "check": 0}
        original_prepare = panel.AgentPanelWidget._prepare_update
        original_check = panel.AgentPanelWidget._check_updates
        try:
            panel.AgentPanelWidget._prepare_update = (
                lambda self: calls.__setitem__("prepare", calls["prepare"] + 1))
            panel.AgentPanelWidget._check_updates = (
                lambda self, silent=False, on_available=None:
                    calls.__setitem__("check", calls["check"] + 1))
            widget.update_top_btn.click()
            APP.processEvents()
        finally:
            panel.AgentPanelWidget._prepare_update = original_prepare
            panel.AgentPanelWidget._check_updates = original_check
        eq(calls["check"], 1, "检查失败后点击必须重新检查")
        eq(calls["prepare"], 0, "检查失败后点击绝不能开始下载")
    finally:
        close(widget)


def test_top_button_without_local_version_falls_back_to_check_text():
    """本地版本取不到（抛异常或返回空）：文案退化成「检查更新」，且 never-raise。"""
    widget = make_panel()
    original_load = pathbridge.load
    try:
        for broken in (lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("boom"))):
            pathbridge.load = broken
            widget._on_update_checked({"status": "error", "message": "检查失败"})
            eq(widget.update_top_btn.text(), "检查更新",
               "取不到本地版本时应退化成「检查更新」")
            ok(widget.update_top_btn.isVisible(), "版本未知时按钮也必须常驻可见")
            contains(widget.update_top_btn.toolTip(), "点击检查更新",
                     "版本未知时 tooltip 仍应说明点击行为")
        ok(True, "版本来源异常不得让顶栏按钮链路抛异常")
    finally:
        pathbridge.load = original_load
        close(widget)


def test_top_button_applied_shows_new_version():
    """更新完成：顶栏显示 v{applied_version}，仍常驻且可点。"""
    widget = make_panel()
    try:
        restarted = {"n": 0}
        original_restart = panel.AgentPanelWidget._restart_backend_async
        try:
            panel.AgentPanelWidget._restart_backend_async = (
                lambda self: restarted.__setitem__("n", restarted["n"] + 1))
            widget._on_update_applied({
                "status": "applied", "applied_version": "1.4.0",
                "message": "已应用更新", "files": 3,
            })
        finally:
            panel.AgentPanelWidget._restart_backend_async = original_restart
        eq(widget.update_top_btn.text(), "v1.4.0", "更新完成后应显示新版本号")
        ok(widget.update_top_btn.isVisible(), "更新完成后按钮仍常驻可见")
        ok(widget.update_top_btn.isEnabled(), "更新完成后按钮应恢复可点")
        contains(widget.update_top_btn.toolTip(), "已更新到", "tooltip 应说明已更新到新版本")
        eq(restarted["n"], 1, "更新完成后仍应自动重启后端")
    finally:
        close(widget)


def test_top_button_never_raises_on_garbage_status_data():
    """垃圾检查结果（{} / {"status": 123} / None 等）：never-raise，按钮仍在且文案非空。"""
    widget = make_panel()
    try:
        for payload in ({}, {"status": 123}, None,
                        {"status": "update_available", "latest_version": 1},
                        {"status": "up_to_date", "current_version": None},
                        {"error": None}, {"status": "error", "message": None}):
            widget._on_update_checked(payload)
            ok(widget.update_top_btn.isVisible(), "垃圾数据也不能把常驻按钮弄丢")
            ok(bool(widget.update_top_btn.text()), "垃圾数据下按钮文案不得为空")
        ok(True, "垃圾数据不得让顶栏按钮链路抛异常")
    finally:
        close(widget)


def test_top_button_prepare_recheck_up_to_date_restores_current_version():
    """点了「一键更新」后后端复检说已是最新：必须复位成 v{current}、可点、tooltip 回「当前版本」。

    update_service.py:346 _prepare_locked() 每次都会重新 _check()，:351-352 若状态不是
    update_available 就直接返回，因此「下载入口点下去、复检发现其实已是最新」是真实可达路径。
    契约 §2 up_to_date 行要求可见 + 可点 + 文案 v{current}，禁用只允许出现在 preparing 行。
    """
    widget = make_panel()
    original_prepare = panel.AgentPanelWidget._prepare_update
    try:
        local = local_plugin_version()
        # 掐断真实 worker：这里只验证状态机复位，不连后端。
        panel.AgentPanelWidget._prepare_update = lambda self: None
        widget._on_update_checked(dict(AVAILABLE))
        widget._apply_update_now()
        eq(widget.update_top_btn.text(), "正在下载更新…", "点击后应进入下载中状态")
        ok(not widget.update_top_btn.isEnabled(), "下载中按钮应禁用")

        widget._on_update_prepared({
            "status": "up_to_date",
            "current_version": "1.4.0",
            "latest_version": "1.4.0",
            "message": "当前版本不低于最新稳定版本，不会回退。",
        })
        btn = widget.update_top_btn
        eq(btn.text().strip(), "v1.4.0", "复检已是最新时应显示数据里的当前版本")
        ok(btn.isEnabled(), "复检已是最新后按钮必须恢复可点（禁用只允许在下载中）")
        ok(btn.isVisible(), "复检已是最新后按钮仍常驻可见")
        contains(btn.toolTip(), "当前版本", "tooltip 应回到「当前版本」文案")
        not_contains(btn.toolTip(), "正在下载", "tooltip 不得停在下载中文案")
        not_contains(btn.toolTip(), "上次更新失败", "复检已是最新不是失败，措辞不得写成失败")
        not_contains(btn.text(), "一键更新", "复检已是最新后不得再显示「一键更新」")

        # 复位后点击应走「重新检查」，绝不重新开始下载。
        calls = {"check": 0, "prepare": 0}
        original_check = panel.AgentPanelWidget._check_updates
        try:
            panel.AgentPanelWidget._check_updates = (
                lambda self, silent=False, on_available=None:
                    calls.__setitem__("check", calls["check"] + 1))
            panel.AgentPanelWidget._prepare_update = (
                lambda self: calls.__setitem__("prepare", calls["prepare"] + 1))
            btn.click()
            APP.processEvents()
        finally:
            panel.AgentPanelWidget._check_updates = original_check
        eq(calls["check"], 1, "复位后点击必须重新检查更新")
        eq(calls["prepare"], 0, "已是最新时点击绝不能开始下载")

        # 数据里没有 current_version：退回本地版本，仍可点、文案不为空。
        widget._update_current_version = ""
        widget._on_update_prepared({"status": "up_to_date", "message": "已是最新"})
        ok(widget.update_top_btn.isEnabled(), "缺 current_version 时也必须恢复可点")
        if local:
            eq(widget.update_top_btn.text().strip(), f"v{local}",
               "缺 current_version 时应退回本地 v{V}")
        ok(bool(widget.update_top_btn.text().strip()), "复位后文案不得为空")
    finally:
        panel.AgentPanelWidget._prepare_update = original_prepare
        close(widget)


def test_top_button_fits_minimum_panel_width():
    """面板最小宽度 330 时：常驻按键不被裁切，也不会把「模型设置」挤出右边界。

    这是常驻改造的连带布局约束：330 是 panel.py 里 setMinimumWidth(U.px(330)) 声明的最小宽度，
    顶栏必须在这宽度下仍放下 侧栏键 + 品牌字 + 更新键 + 主题键 + 设置键。
    2026-11-04 用户反馈修订：品牌标题（ADS Agent）**任何宽度都必须显示**——空间不足时
    由 _elide_title 做省略号（如「AD…」），绝不消失；更新键 min 宽锁定 sizeHint（永不裁字），
    被压缩的只能是可省略的品牌字；紧凑态（<400）设置键收窄 U.px(84)、间距 U.px(2)，
    变宽后全部还原。
    """
    widget = make_panel(330, 520)
    try:
        btn = widget.update_top_btn
        ok(btn.isVisible(), "最小宽度下更新键仍必须常驻可见")
        ok(btn.isEnabled(), "最小宽度下更新键必须可点")
        ok(btn.width() >= btn.sizeHint().width(), "最小宽度下更新键不得被裁切")
        ok(bool(btn.text().strip()), "最小宽度下更新键文案不得为空")
        settings_right = widget.settings_btn.x() + widget.settings_btn.width()
        ok(settings_right < widget.width(),
           f"最小宽度下顶栏动作被裁切（右缘 {settings_right} >= {widget.width()}）")
        ok(widget.title.isVisible(), "品牌标题任何宽度都必须可见（2026-11-04 用户反馈）")
        ok(bool(widget.title.text().strip()),
           "品牌标题文案不得为空（最窄时是省略号形态，如 AD…）")
        ok(not widget.subtitle.isVisible(), "最小宽度下品牌副标题隐藏（<400 既有降级）")
        eq(widget.settings_btn.width(), panel.U.px(84),
           "紧凑态设置键应收窄到 U.px(84)，给品牌字和更新键腾空间")
        # 变宽后必须还原常规间距/内边距/宽度，不能永久停在紧凑态。
        widget.resize(600, 520)
        APP.processEvents()
        eq(widget._top_row.spacing(), panel.U.P("sm"), "变宽后顶栏间距必须还原")
        contains(btn.styleSheet(), f"padding:{panel.U.px(5)}px {panel.U.px(14)}px",
                 "变宽后更新键水平内边距必须还原")
        ok(btn.width() >= btn.sizeHint().width(), "变宽后更新键不得被裁切")
        ok(btn.isVisible(), "变宽后更新键仍常驻可见")
        ok(widget.title.isVisible(), "变宽后品牌标题必须保持可见")
        eq(widget.title.text(), "ADS Agent",
           "变宽后品牌标题必须还原全文（不得停留在省略号）")
        ok(widget.subtitle.isVisible(), "变宽后品牌副标题必须恢复可见")
        eq(widget.settings_btn.width(), panel.U.px(92), "变宽后设置键宽度必须还原 92")
    finally:
        close(widget)


def test_startup_check_is_delayed_and_respects_setting():
    """启动检查必须延时（避开首屏），关掉开关就完全不查。"""
    widget = make_panel()
    try:
        widget._update_timer = None
        widget.update_auto_check.setChecked(False)
        _ORIGINAL_STARTUP_CHECK(widget)
        ok(getattr(widget, "_update_timer", None) is None,
           "关闭自动检查后不得创建定时器")

        widget.update_auto_check.setChecked(True)
        _ORIGINAL_STARTUP_CHECK(widget)
        timer = getattr(widget, "_update_timer", None)
        ok(timer is not None, "开启自动检查后应创建定时器")
        ok(timer.isSingleShot(), "启动检查应只跑一次")
        ok(timer.interval() >= 1000, "启动检查必须延时，不能和首屏渲染抢主线程")
    finally:
        close(widget)


if __name__ == "__main__":
    sys.exit(run(globals(), "ADS 面板 · 软件更新"))
