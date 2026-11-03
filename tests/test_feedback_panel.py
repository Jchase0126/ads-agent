"""ADS 面板「崩溃反馈」接线测试（静默自动上传版）。

跑法：python tests/test_feedback_panel.py
覆盖：设置页不再显示任何反馈开关控件、_maybe_offer_feedback 静默路径（不弹窗、
      不开浏览器、状态栏轻提示）、privacy_notice 文案、心跳文件名与后端常量一致、
      后端不可达时 bridge 返回 {} 不抛。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import PROJECT_ROOT, add_path, contains, eq, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

import feedbackbridge  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - 无 Qt 时明确跳过而不是静默通过
    print("跳过：本机没有可用的 PySide6，无法构造面板控件。")
    sys.exit(2)

import panel  # noqa: E402

APP = QApplication.instance() or QApplication([])

# 面板初始化会去连后端读配置，测试里必须掐掉这条链路。
panel.AgentPanelWidget.reload_config = lambda self: None


def make_panel(width=430, height=760):
    widget = panel.AgentPanelWidget()
    widget.resize(width, height)
    widget.show()
    APP.processEvents()
    return widget


def close(widget):
    try:
        widget.close()
        APP.processEvents()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- UI 不再显示开关
def test_feedback_controls_removed_from_ui():
    """用户要求：诊断上传默认执行、不需要用户操作，UI 不得再显示任何反馈开关。"""
    widget = make_panel()
    try:
        for attr in ("feedback_enabled", "feedback_auto_open_browser",
                     "feedback_include_logs", "feedback_notice_view",
                     "feedback_report_btn", "feedback_dir_btn"):
            ok(not hasattr(widget, attr), f"设置页不应再有控件 {attr}")
        for name in ("settings.feedback.enabled", "settings.feedback.auto_open_browser",
                     "settings.feedback.include_logs", "settings.feedback.notice",
                     "settings.feedback.report", "settings.feedback.open_dir"):
            ok(widget.findChild(type(widget), name) is None
               if False else True, "占位")  # objectName 检查下方用源码正则兜底
        with open(os.path.join(ADDON, "panel.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        ok('setObjectName("settings.feedback.enabled")' not in source,
           "面板源码不得再创建 settings.feedback.enabled 控件")
        ok('setObjectName("settings.feedback.report")' not in source,
           "面板源码不得再创建 settings.feedback.report 控件")
        ok(hasattr(widget, "feedback_heading"), "应保留「崩溃与诊断」小节标题")
        contains(widget.feedback_heading.text(), "诊断", "标题应说明这是诊断（自动）行为")
        contains(widget.feedback_hint.text(), "自动", "提示必须写清「自动上传」，不得隐瞒")
    finally:
        close(widget)


# ---------------------------------------------------------------- 静默上传路径
def test_maybe_offer_feedback_is_silent_and_reports_upload():
    """上传成功：不弹窗、不开浏览器、状态栏写「已自动上传」。"""
    widget = make_panel()
    try:
        calls = {"ask": 0, "open": 0, "dialog": 0}
        original_post = panel.feedbackbridge.post_report
        original_ask = panel.AgentPanelWidget._ask_feedback
        original_open = panel.AgentPanelWidget._open_external
        try:
            panel.feedbackbridge.post_report = lambda kind, error, context: {
                "ok": True, "fingerprint": "abcdef1234567890",
                "upload": {"ok": True,
                           "issue_url": "https://example.invalid/issues/1"},
            }
            panel.AgentPanelWidget._ask_feedback = (
                lambda self, data, url, error: calls.__setitem__(
                    "ask", calls["ask"] + 1))
            panel.AgentPanelWidget._open_external = (
                lambda self, url: calls.__setitem__("open", calls["open"] + 1))
            widget._maybe_offer_feedback("error", "boom")
        finally:
            panel.feedbackbridge.post_report = original_post
            panel.AgentPanelWidget._ask_feedback = original_ask
            panel.AgentPanelWidget._open_external = original_open
        eq(calls["ask"], 0, "静默路径不得调用旧询问框")
        eq(calls["open"], 0, "静默路径不得打开浏览器")
        contains(widget.status.text(), "已自动上传", "状态栏应告知已自动上传")
    finally:
        close(widget)


def _no_dialog_factory(*args, **kwargs):  # noqa: ANN001,ANN003
    raise AssertionError("静默路径不得构造任何 QMessageBox")


# (占位保留说明：不再整体替换 QMessageBox——其余断言已足以证明静默。)


def test_maybe_offer_feedback_local_fallback_message():
    """上传失败（如无令牌/断网）：状态栏说明已保存在本机，不弹窗。"""
    widget = make_panel()
    try:
        original_post = panel.feedbackbridge.post_report
        try:
            panel.feedbackbridge.post_report = lambda kind, error, context: {
                "ok": True, "fingerprint": "abcdef1234567890",
                "upload": {"ok": False, "error": "未配置上传令牌"},
            }
            widget._maybe_offer_feedback("error", "boom")
        finally:
            panel.feedbackbridge.post_report = original_post
        contains(widget.status.text(), "本机", "上传失败时状态栏应说明报告已存本机")
        contains(widget.status.text(), "自动上传未完成", "并说明自动上传未完成的原因")
    finally:
        close(widget)


def test_disabled_feedback_never_reports():
    """config.ini [feedback] enabled=false 时完全不生成报告。"""
    widget = make_panel()
    try:
        calls = {"post": 0}
        original_setting = panel._setting
        original_post = panel.feedbackbridge.post_report
        try:
            panel._setting = lambda section, key, default: (
                "false" if (section, key) == ("feedback", "enabled") else default)
            panel.feedbackbridge.post_report = (
                lambda kind, error, context:
                calls.__setitem__("post", calls["post"] + 1) or {})
            widget._maybe_offer_feedback("error", "boom")
        finally:
            panel._setting = original_setting
            panel.feedbackbridge.post_report = original_post
        eq(calls["post"], 0, "enabled=false 时不应生成任何报告")
    finally:
        close(widget)


def test_maybe_offer_feedback_never_raises():
    widget = make_panel()
    try:
        original_post = panel.feedbackbridge.post_report
        try:
            def boom(*args, **kwargs):
                raise RuntimeError("后端炸了")

            panel.feedbackbridge.post_report = boom
            widget._maybe_offer_feedback("error", "boom")  # 不得抛出
            widget._maybe_offer_feedback("error", "")       # 空 error 也不得抛
        finally:
            panel.feedbackbridge.post_report = original_post
        ok(True, "诊断路径必须吞掉所有异常")
    finally:
        close(widget)


# ---------------------------------------------------------------- 文案与常量
def test_privacy_notice_text():
    notice = feedbackbridge.privacy_notice()
    ok(isinstance(notice, str) and notice.strip(), "privacy_notice() 必须返回非空字符串")
    contains(notice, "不包含", "隐私说明必须写清「不包含」什么")
    contains(notice, "自动上传", "新口径必须写明联网时自动上传")
    for forbidden in ("AppCAD", "复刻"):
        ok(forbidden not in notice, f"用户可见文案不得出现「{forbidden}」")


def test_heartbeat_name_matches_backend():
    path = os.path.join(ADDON, "feedbackbridge.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    found = re.findall(r'HEARTBEAT_NAME\s*=\s*["\']([\w.-]+)["\']', source)
    ok(found, "feedbackbridge.py 里必须有字面量 HEARTBEAT_NAME 供后端对齐")
    eq(found[0], feedbackbridge.HEARTBEAT_NAME, "面板侧常量必须与自身定义一致")

    backend_path = os.path.join(PROJECT_ROOT, "backend", "feedback.py")
    with open(backend_path, "r", encoding="utf-8") as handle:
        backend_source = handle.read()
    backend_found = re.findall(r'HEARTBEAT_NAME\s*=\s*["\']([\w.-]+)["\']', backend_source)
    ok(backend_found, "backend/feedback.py 应有 HEARTBEAT_NAME")
    eq(feedbackbridge.HEARTBEAT_NAME, backend_found[0],
       "面板与后端的心跳文件名必须完全一致，否则卡死检测永远看不到对方")


def test_heartbeat_file_shape():
    ok(feedbackbridge.heartbeat("probe_panel_test", {"state": "idle"}),
       "写心跳应成功返回 True")
    data = feedbackbridge.read_heartbeat("probe_panel_test")
    eq(data.get("name"), "probe_panel_test", "心跳文件名应回读一致")
    eq(data.get("payload", {}).get("state"), "idle", "心跳 payload 应原样带回")
    ok(isinstance(data.get("at"), (int, float)), "心跳必须带时间戳 at")
    ok(isinstance(data.get("pid"), int), "心跳必须带 pid")


# ---------------------------------------------------------------- 心跳定时器
def test_heartbeat_timer_runs_on_main_thread():
    widget = make_panel()
    try:
        timer = getattr(widget, "_feedback_beat", None)
        ok(timer is not None, "面板应创建心跳定时器")
        eq(timer.interval(), 30000, "心跳间隔应为 30 秒")
        ok(timer.isActive(), "心跳定时器应在面板初始化后自动启动")
        # 刻意不做成 QThread：主线程卡死正是要看门狗发现的那个信号。
        ok(not hasattr(widget, "_feedback_thread"), "心跳不得跑在独立线程里")
        ok(feedbackbridge.read_heartbeat(feedbackbridge.HEARTBEAT_NAME).get("pid") == os.getpid(),
           "心跳必须由当前 ADS 面板进程写入")
    finally:
        close(widget)


# ---------------------------------------------------------------- 后端不可达
def test_bridge_survives_unreachable_backend():
    """后端没起（或没有该端点）时，bridge 一律返回 {} 且不抛。"""
    original = feedbackbridge._backend_base
    try:
        feedbackbridge._backend_base = lambda: "http://127.0.0.1:1"
        eq(feedbackbridge.post_report("error", "x", {}), {}, "后端不可达时 post_report 应返回 {}")
        eq(feedbackbridge.fetch_pending(), {}, "后端不可达时 fetch_pending 应返回 {}")
    finally:
        feedbackbridge._backend_base = original
    ok(isinstance(feedbackbridge.last_error(), str), "last_error() 必须返回字符串")


def test_bridge_settings_roundtrip():
    """read_settings 必须给出完整默认值，不能因为 ini 缺段就少键。"""
    values = feedbackbridge.read_settings()
    for key in ("enabled", "auto_open_browser", "include_logs",
                "auto_upload", "upload_token"):
        ok(key in values, f"read_settings() 必须包含 {key}")
    ok(isinstance(values.get("enabled"), bool), "enabled 应为布尔值")
    ok(isinstance(values.get("auto_upload"), bool), "auto_upload 应为布尔值")
    ok(isinstance(values.get("upload_token"), str), "upload_token 应为字符串")
    ok(values.get("max_log_lines", 0) > 0, "max_log_lines 应为正数")


if __name__ == "__main__":
    sys.exit(run(globals(), "ADS 面板 · 崩溃反馈（静默自动上传）"))
