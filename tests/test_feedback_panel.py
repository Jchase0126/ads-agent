"""ADS 面板「崩溃反馈」接线测试。

跑法：python tests/test_feedback_panel.py
覆盖：设置页控件与 objectName、privacy_notice 文案、auto_open_browser 时跳过询问、
      心跳文件名与后端常量一致、后端不可达时 bridge 返回 {} 不抛。
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


# ---------------------------------------------------------------- 设置页控件
def test_settings_controls_have_stable_object_names():
    widget = make_panel()
    try:
        expected = {
            "settings.feedback.enabled": widget.feedback_enabled,
            "settings.feedback.auto_open_browser": widget.feedback_auto_open_browser,
            "settings.feedback.include_logs": widget.feedback_include_logs,
            "settings.feedback.notice": widget.feedback_notice_view,
            "settings.feedback.report": widget.feedback_report_btn,
            "settings.feedback.open_dir": widget.feedback_dir_btn,
            "settings.feedback.hint": widget.feedback_hint,
        }
        for name, control in expected.items():
            eq(control.objectName(), name, f"控件 {name} 的 objectName 应稳定")
        ok(widget.feedback_heading.text() == "崩溃反馈", "应有「崩溃反馈」小节标题")
    finally:
        close(widget)


def test_settings_controls_live_in_scroll_area():
    """新分组必须在滚动卡片里，且不把固定底栏的保存键挤出可见区。"""
    widget = make_panel(330, 400)
    try:
        widget._toggle_settings(True)  # 先切到设置页，否则保存键不在当前可见页上
        APP.processEvents()
        inside = widget.settings_box.isAncestorOf(widget.feedback_report_btn)
        ok(inside, "反馈按钮应位于设置卡片（settingsCard）内，随滚动条滚动")
        ok(widget.save_btn.isVisible(), "保存按钮必须保持可见")
        ok(widget.save_btn.geometry().bottom() < widget.settings_page.height(),
           "保存按钮不能被新分组挤出设置页")
        eq(widget.settings_scroll.horizontalScrollBar().maximum(), 0,
           "新分组不得引入横向滚动")
    finally:
        close(widget)


def test_notice_view_is_readonly_and_filled():
    widget = make_panel()
    try:
        ok(widget.feedback_notice_view.isReadOnly(), "隐私说明应为只读文本区")
        text = widget.feedback_notice_view.toPlainText()
        ok(text.strip() != "", "隐私说明不能为空")
        contains(text, "不包含", "隐私说明必须写清「不包含」什么")
        contains(text, "反馈报告", "隐私说明应说明报告包含什么")
    finally:
        close(widget)


# ---------------------------------------------------------------- 文案与常量
def test_privacy_notice_text():
    notice = feedbackbridge.privacy_notice()
    ok(isinstance(notice, str) and notice.strip(), "privacy_notice() 必须返回非空字符串")
    contains(notice, "不包含", "隐私说明必须写清「不包含」什么")
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


# ---------------------------------------------------------------- 弹窗策略
def test_auto_open_browser_skips_dialog():
    """auto_open_browser 勾上时，不应再弹三按钮询问框。"""
    widget = make_panel()
    try:
        widget.feedback_enabled.setChecked(True)
        widget.feedback_auto_open_browser.setChecked(True)

        calls = {"ask": 0, "open": []}
        original_post = panel.feedbackbridge.post_report
        original_ask = panel.AgentPanelWidget._ask_feedback
        original_open = panel.AgentPanelWidget._open_external
        try:
            panel.feedbackbridge.post_report = lambda kind, error, context: {
                "ok": True, "markdown": "# r", "title": "t",
                "url": "https://example.invalid/issues/new", "paths": [],
            }
            panel.AgentPanelWidget._ask_feedback = (
                lambda self, data, url, error: calls.__setitem__("ask", calls["ask"] + 1))
            panel.AgentPanelWidget._open_external = (
                lambda self, url: calls["open"].append(url))
            widget._maybe_offer_feedback("error", "boom")
        finally:
            panel.feedbackbridge.post_report = original_post
            panel.AgentPanelWidget._ask_feedback = original_ask
            panel.AgentPanelWidget._open_external = original_open

        eq(calls["ask"], 0, "auto_open_browser=true 时不得弹询问框")
        eq(len(calls["open"]), 1, "auto_open_browser=true 时应直接打开浏览器")
        contains(calls["open"][0], "issues", "应打开 issue 填写页")
    finally:
        close(widget)


def test_auto_open_browser_off_asks_once():
    widget = make_panel()
    try:
        widget.feedback_enabled.setChecked(True)
        widget.feedback_auto_open_browser.setChecked(False)

        calls = {"ask": 0, "open": 0}
        original_post = panel.feedbackbridge.post_report
        original_ask = panel.AgentPanelWidget._ask_feedback
        original_open = panel.AgentPanelWidget._open_external
        try:
            panel.feedbackbridge.post_report = lambda kind, error, context: {
                "ok": True, "markdown": "# r", "title": "t",
                "url": "https://example.invalid/issues/new", "paths": [],
            }
            panel.AgentPanelWidget._ask_feedback = (
                lambda self, data, url, error: calls.__setitem__("ask", calls["ask"] + 1))
            panel.AgentPanelWidget._open_external = (
                lambda self, url: calls.__setitem__("open", calls["open"] + 1))
            widget._maybe_offer_feedback("error", "boom")
        finally:
            panel.feedbackbridge.post_report = original_post
            panel.AgentPanelWidget._ask_feedback = original_ask
            panel.AgentPanelWidget._open_external = original_open

        eq(calls["ask"], 1, "关闭自动打开时应询问一次")
        eq(calls["open"], 0, "询问阶段不得抢先打开浏览器")
    finally:
        close(widget)


def test_disabled_feedback_never_reports():
    widget = make_panel()
    try:
        widget.feedback_enabled.setChecked(False)
        calls = {"post": 0}
        original_post = panel.feedbackbridge.post_report
        try:
            panel.feedbackbridge.post_report = (
                lambda kind, error, context: calls.__setitem__("post", calls["post"] + 1) or {})
            widget._maybe_offer_feedback("error", "boom")
        finally:
            panel.feedbackbridge.post_report = original_post
        eq(calls["post"], 0, "关闭崩溃反馈后不应再生成任何报告")
    finally:
        close(widget)


def test_maybe_offer_feedback_never_raises():
    widget = make_panel()
    try:
        widget.feedback_enabled.setChecked(True)
        original_post = panel.feedbackbridge.post_report
        original_snap = panel.feedbackbridge.snapshot_local
        try:
            def boom(*args, **kwargs):
                raise RuntimeError("后端炸了")

            panel.feedbackbridge.post_report = boom
            panel.feedbackbridge.snapshot_local = boom
            widget._maybe_offer_feedback("error", "boom")  # 不得抛出
            widget._maybe_offer_feedback("error", "")       # 空 error 也不得抛
        finally:
            panel.feedbackbridge.post_report = original_post
            panel.feedbackbridge.snapshot_local = original_snap
        ok(True, "诊断路径必须吞掉所有异常")
    finally:
        close(widget)


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


def test_bridge_settings_roundtrip(tmp=None):
    """read_settings 必须给出完整默认值，不能因为 ini 缺段就少键。"""
    values = feedbackbridge.read_settings()
    for key in ("enabled", "auto_open_browser", "include_logs"):
        ok(key in values, f"read_settings() 必须包含 {key}")
        ok(isinstance(values[key], bool), f"{key} 应为布尔值")
    ok(values.get("max_log_lines", 0) > 0, "max_log_lines 应为正数")


if __name__ == "__main__":
    sys.exit(run(globals(), "ADS 面板 · 崩溃反馈"))
