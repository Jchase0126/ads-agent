"""崩溃/卡死反馈：脱敏、指纹、去重、预填链接降级、心跳看门狗。

这个模块的原则是「诊断路径自己绝不炸」——所以这里的用例除了检查功能，
还专门拿畸形输入去撞它，确认任何一条路径都只返回 ValueError/占位文本，
而不把异常抛给调用方（那会在崩溃处理里引发二次崩溃）。

隐私是硬约束：脱敏漏一条就等于把用户路径/密钥贴到公开 issue 上。
下面的用例逐条钉住 ``backend/feedback.py`` 的三道脱敏（令牌 → 路径 →
超长串）与调用方点名屏蔽。
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ROOT = add_path()
add_path("backend")

import feedback  # noqa: E402
import paths  # noqa: E402


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def test_redacts_key_value_and_bearer_tokens():
    text = ("config: api_key=sk-live-abcdef0123456789 token: abc123\n"
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    out = feedback.sanitize(text)
    not_contains(out, "sk-live-abcdef0123456789", "api_key 的值必须被抹掉")
    not_contains(out, "eyJhbGciOiJIUzI1NiJ9", "Bearer 令牌必须被抹掉")
    OK_MARK = "***"
    contains(out, OK_MARK, "脱敏应留下可见的掩码标记")


def test_redacts_windows_and_posix_paths():
    # 插件自身的源码文件名要保留（否则栈无法定位）；用户的设计文件不保留
    text = r"打开 C:\Users\alice\designs\amp.wrk 失败"
    out = feedback.sanitize(text)
    not_contains(out, "alice", "用户名不得出现在报告里")
    contains(out, "<本机路径>", "路径应被占位符替换")
    not_contains(out, "amp.wrk", "用户设计文件名属于隐私，不得保留")

    out_src = feedback.sanitize(r'  File "C:\Users\alice\proj\server.py", line 12')
    not_contains(out_src, "alice", "源码路径里的用户名同样要抹掉")
    contains(out_src, "server.py", "源码文件名保留，否则栈无法定位")

    out2 = feedback.sanitize("在 /home/bob/proj/x.py 与 /tmp/scratch/y.log 中")
    not_contains(out2, "bob", "POSIX home 路径里的用户名必须抹掉")
    not_contains(out2, "/tmp/scratch", "临时目录路径必须抹掉")


def test_redacts_very_long_tokens():
    token = "A1b2C3d4E5" * 8  # 80 字符，像 key/哈希
    out = feedback.sanitize("value=" + token)
    not_contains(out, token, "40 字符以上的长串应整体屏蔽")


def test_extra_names_are_masked():
    out = feedback.sanitize("设计 MySecretDesign 计算失败", extra_names=["MySecretDesign"])
    not_contains(out, "MySecretDesign", "调用方点名的敏感词必须消失")
    contains(out, "<已屏蔽>", "点名屏蔽应留下占位")


def test_sanitize_survives_garbage_input():
    for bad in (None, 12345, b"\x00\xff", ["a", "b"], {"k": "v"}):
        out = feedback.sanitize(bad)
        ok(isinstance(out, str), "畸形的 %r 也必须返回字符串而不抛" % (bad,))


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------
def test_build_report_strips_secrets_and_design_names():
    ctx = {
        "design": "MyTopSecretDesign",
        "workspace": r"C:\Users\alice\ws",
        "tool": "build_gate",
        "api_key": "sk-should-never-appear",
    }
    report = feedback.build_report("crash", "Traceback ...\nKeyError: 'result'",
                                   context=ctx, log_lines=["line api_key=sk-x"])
    blob = json.dumps(report, ensure_ascii=False)
    not_contains(blob, "MyTopSecretDesign", "设计名必须从报告里消失")
    not_contains(blob, "alice", "工作区路径里的用户名必须消失")
    not_contains(blob, "sk-should-never-appear", "上下文里的密钥必须被抹掉")
    eq(report["context"].get("api_key"), "***", "密钥键应替换为 ***")
    eq(report["context"].get("tool"), "build_gate", "非敏感上下文应保留")
    ok(report["fingerprint"], "报告必须带指纹")
    eq(report["kind"], "crash", "类型应保留")
    contains(report["privacy_notice"], "不包含")


def test_unknown_kind_falls_back_to_error():
    report = feedback.build_report("没用过的类型", "boom", log_lines=[])
    eq(report["kind"], "error", "未知类型应退化为 error，而不是写进报告")


# ---------------------------------------------------------------------------
# 指纹与去重
# ---------------------------------------------------------------------------
def test_fingerprint_ignores_line_numbers_but_not_error_identity():
    a = 'Traceback (most recent call last):\n  File "x.py", line 10, in f\nValueError: bad'
    b = 'Traceback (most recent call last):\n  File "x.py", line 99, in f\nValueError: bad'
    c = 'Traceback (most recent call last):\n  File "x.py", line 10, in f\nKeyError: bad'
    eq(feedback.fingerprint("crash", a), feedback.fingerprint("crash", b),
       "同一处代码挪行不应算新 bug")
    ne(feedback.fingerprint("crash", a), feedback.fingerprint("crash", c),
       "不同异常类型必须是不同指纹")
    ne(feedback.fingerprint("crash", a), feedback.fingerprint("hang", a),
       "类型不同指纹也应不同")


def test_records_are_deduped_within_window():
    first = feedback.record("error", "ValueError: 同一条\n" + str(time.time()),
                            log_lines=[], dedupe_window_s=600.0)
    ok(not first["duplicate"], "第一次出现不应判重复")
    ok(first["paths"], "第一次出现应落盘")
    second = feedback.record("error", "ValueError: 同一条\n" + str(time.time()),
                             log_lines=[], dedupe_window_s=600.0)
    ok(second["duplicate"], "窗口内同一指纹应判重复")
    eq(second["paths"], {}, "重复时不再落盘（不刷屏）")
    eq(second["fingerprint"], first["fingerprint"], "指纹应一致")


def test_record_respects_enabled_switch():
    # 直接喂一个关掉开关的配置对象，避免动真实 config.ini
    saved = feedback.settings

    def _off(cfg=None):
        base = dict(feedback.DEFAULTS)
        base["enabled"] = False
        return base

    feedback.settings = _off
    try:
        out = feedback.record("error", "ValueError: x", log_lines=[])
    finally:
        feedback.settings = saved
    eq(out["report"], None, "关闭开关后 record() 不应生成报告")
    eq(out["paths"], {}, "关闭开关后不应落盘")


# ---------------------------------------------------------------------------
# GitHub 预填链接
# ---------------------------------------------------------------------------
def test_issue_url_fits_github_limit_and_degrades():
    long_logs = ["日志行 %d 一些内容" % i for i in range(400)]
    report = feedback.build_report("crash", "RuntimeError: boom", log_lines=long_logs)
    url = feedback.issue_url(report)
    ok(len(url) <= feedback.URL_MAX_CHARS,
       "预填链接必须 <= %d 字符，实际 %d" % (feedback.URL_MAX_CHARS, len(url)))
    contains(url, "github.com/Jchase0126/ads-agent/issues/new", "必须指向本仓库")
    contains(url, "template=bug_report.yml", "必须带 issue 模板")
    contains(url, "title=", "必须预填标题")


def test_issue_title_names_severity_and_version():
    report = feedback.build_report("crash", "ValueError: 炸了", log_lines=[])
    title = feedback.issue_title(report)
    ok("ValueError" in title, "标题应带异常摘要，实际 %r" % title)
    ok(paths.PLUGIN_VERSION in title, "标题应带版本号，实际 %r" % title)


# ---------------------------------------------------------------------------
# 心跳 / 卡死看门狗
# ---------------------------------------------------------------------------
def test_heartbeat_roundtrip_and_stale_detection():
    name = "test_hb_%d" % os.getpid()
    ok(feedback.write_heartbeat(name, {"state": "idle"}), "心跳应写入成功")
    beat = feedback.read_heartbeat(name)
    eq(beat.get("name"), name, "心跳应带上名字")
    eq(beat.get("payload", {}).get("state"), "idle", "负载应原样保存")
    eq(beat.get("pid"), os.getpid(), "心跳应记录 pid")
    ok(beat.get("age", 99) < 5, "刚写的跳龄应接近 0")

    fresh = feedback.check_stale(name, 180)
    eq(fresh["stale"], False, "刚写过心跳不应判卡死")
    eq(fresh["alive"], True, "本进程 pid 必然存活")

    # 把时间戳改老，模拟"对面主线程卡住、心跳停摆"
    path = feedback._heartbeat_path(name)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["at"] = time.time() - 3600
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    stale = feedback.check_stale(name, 180)
    eq(stale["stale"], True, "一小时没心跳必须判卡死")
    ok(stale["age"] > 3000, "跳龄应反映真实间隔")
    os.remove(path)


def test_missing_heartbeat_is_not_stale():
    out = feedback.check_stale("definitely_not_there_%d" % os.getpid(), 60)
    eq(out["stale"], False, "从未有过心跳 → 尚未启动，不算卡死")
    eq(out["age"], None, "无心跳时跳龄应为 None")


def test_heartbeat_name_matches_panel_bridge():
    """两处常量必须一致，否则看门狗永远读不到面板的心跳（静默失效）。"""
    bridge = os.path.join(ROOT, "addon", "ads_agent", "feedbackbridge.py")
    if not os.path.isfile(bridge):
        from _harness import skip
        skip("addon/ads_agent/feedbackbridge.py 尚未创建")
    with open(bridge, encoding="utf-8") as f:
        src = f.read()
    found = re.findall(r'HEARTBEAT_NAME\s*=\s*["\']([\w.-]+)["\']', src)
    ok(found, "feedbackbridge.py 必须定义 HEARTBEAT_NAME")
    eq(found[0], feedback.HEARTBEAT_NAME,
       "面板与后端的心跳名必须一致，否则卡死检测静默失效")


# ---------------------------------------------------------------------------
# snapshot：HTTP 层的一步到位接口
# ---------------------------------------------------------------------------
def test_snapshot_always_returns_renderable_report():
    out = feedback.snapshot("hang", "界面 180 秒无响应", {"tool": "panel"})
    eq(out["ok"], True, "snapshot 应恒返回 ok")
    ok(out["markdown"], "必须给可复制的正文")
    ok(out["url"].startswith("https://"), "必须给可打开的链接")
    ok(out["title"], "必须给标题")
    ok(os.path.isdir(out["dir"]), "报告目录应已创建")
    ok(out["privacy_notice"], "必须附隐私声明原文")


def test_snapshot_never_raises_on_garbage():
    for bad in (None, "", 123, {"a": 1}, ["x"]):
        out = feedback.snapshot("crash", bad)
        eq(out["ok"], True, "畸形输入 %r 也应返回报告而不是抛异常" % (bad,))


def test_hooks_can_install_and_report_state():
    ok(feedback.install_hooks({"source": "test"}), "钩子应能安装")
    state = feedback.hooks_installed()
    ok(state.get("main") is True, "sys.excepthook 应已接管")
    ok(state.get("thread") is True, "threading.excepthook 应已接管")
    # 幂等：再装一次不报错、不重复包装
    ok(feedback.install_hooks(), "重复安装应幂等成功")


def test_privacy_notice_states_what_is_and_is_not_collected():
    notice = feedback.PRIVACY_NOTICE
    for must in ("不包含", "密钥", "路径"):
        contains(notice, must, "隐私声明必须写明边界：%s" % must)
    contains(notice, "自动上传", "新口径必须写明联网时会自动上传")
    ok(len(notice) > 80, "隐私声明要看得懂，不能是一句话敷衍")


# ---------------------------------------------------------------------------
# 自动上传（v1.3.5：联网默认上传，无需用户登录）
# ---------------------------------------------------------------------------
def _fake_report():
    return {"kind": "crash", "error": "TypeError: boom",
            "env": {"plugin_version": "1.3.5"}}


def test_upload_without_token_fails_closed():
    """无令牌：绝不发网络请求，报告只落本机。"""
    calls = {"post": 0}

    def _spy(*args, **kwargs):
        calls["post"] += 1
        return 201, '{"html_url": "https://x"}'

    original = feedback._http_post_json
    try:
        feedback._http_post_json = _spy
        result = feedback.upload(_fake_report(), "# r", "[崩溃] t", token="")
    finally:
        feedback._http_post_json = original
    eq(calls["post"], 0, "无令牌时不得发出任何网络请求")
    ok(not result.get("ok"), "无令牌时上传必须失败（fail-closed）")
    contains(result.get("error") or "", "令牌", "错误信息应说明缺令牌")


def test_upload_with_token_posts_and_returns_issue_url():
    captured = {}

    def _fake(url, payload, headers, timeout=10.0):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return 201, '{"html_url": "https://github.com/Jchase0126/ads-agent/issues/9"}'

    original = feedback._http_post_json
    try:
        feedback._http_post_json = _fake
        result = feedback.upload(_fake_report(), "# 正文", "[崩溃] t",
                                 token="ghp_test")
    finally:
        feedback._http_post_json = original
    ok(result.get("ok"), "带令牌上传应成功")
    eq(result.get("issue_url"), "https://github.com/Jchase0126/ads-agent/issues/9",
       "应带回 GitHub 返回的 issue 链接")
    eq(captured["url"], feedback.UPLOAD_URL, "只能 POST 到固定仓库地址")
    contains(captured["headers"].get("Authorization") or "", "ghp_test",
             "Authorization 应使用调用方令牌")
    ok(captured["payload"].get("title"), "issue 标题不得为空")


def test_snapshot_carries_upload_result():
    """snapshot()（面板手动/静默路径）必须把上传结果放进返回 dict。"""
    original_post = feedback._http_post_json
    original_token = feedback.resolve_token
    try:
        feedback.resolve_token = lambda cfg=None: "ghp_test"
        feedback._http_post_json = (
            lambda url, payload, headers, timeout=10.0: (
                201, '{"html_url": "https://github.com/x/issues/2"}'))
        snap = feedback.snapshot("error", "TypeError: upload-path",
                                 context={"source": "panel-test"})
    finally:
        feedback._http_post_json = original_post
        feedback.resolve_token = original_token
    ok(snap.get("ok"), "snapshot 应成功")
    up = snap.get("upload") or {}
    ok(up.get("ok"), "snapshot 返回值应包含成功的 upload 结果")
    contains(up.get("issue_url") or "", "issues", "upload 结果应带 issue 链接")


def test_auto_upload_disabled_skips_network():
    """auto_upload=false（config 或 env 注入）时不发任何请求。"""
    calls = {"post": 0}

    def _spy(*args, **kwargs):
        calls["post"] += 1
        return 201, "{}"

    original_post = feedback._http_post_json
    original_settings = feedback.settings
    try:
        feedback._http_post_json = _spy
        feedback.settings = lambda cfg=None: {
            "enabled": True, "auto_upload": False, "upload_token": "ghp_test",
            "include_logs": True, "max_log_lines": 120, "hang_seconds": 180,
        }
        out = feedback._maybe_auto_upload(_fake_report(), background=False)
    finally:
        feedback._http_post_json = original_post
        feedback.settings = original_settings
    eq(calls["post"], 0, "auto_upload=false 时不得发请求")
    contains(out.get("error") or "", "未开启", "应说明自动上传未开启")


def test_resolve_token_precedence_env_over_config():
    """env > config.ini > 打包注入。"""
    original_embedded = feedback.EMBEDDED_UPLOAD_TOKEN
    original_settings = feedback.settings
    try:
        feedback.settings = lambda cfg=None: {
            "enabled": True, "auto_upload": True,
            "upload_token": "cfg-token", "include_logs": True,
            "max_log_lines": 120, "hang_seconds": 180,
        }
        os.environ["ADS_AGENT_FEEDBACK_TOKEN"] = "env-token"
        try:
            eq(feedback.resolve_token(), "env-token", "env 必须优先于 config")
        finally:
            os.environ.pop("ADS_AGENT_FEEDBACK_TOKEN", None)
        eq(feedback.resolve_token(), "cfg-token", "config 次于 env")
        feedback.EMBEDDED_UPLOAD_TOKEN = "pkg-token"
        feedback.settings = lambda cfg=None: {
            "enabled": True, "auto_upload": True, "upload_token": "",
            "include_logs": True, "max_log_lines": 120, "hang_seconds": 180,
        }
        eq(feedback.resolve_token(), "pkg-token", "打包注入是最后一级")
    finally:
        feedback.EMBEDDED_UPLOAD_TOKEN = original_embedded
        feedback.settings = original_settings


if __name__ == "__main__":
    raise SystemExit(run(globals(), "崩溃反馈核心"))
