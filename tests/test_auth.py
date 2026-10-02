"""后端接口鉴权测试（不需要 ADS：直接在本进程里拉起真的 server.Handler）。

覆盖评审第 1 项「后端接口鉴权」：

* ``/chat`` ``/config`` ``/tools`` ``/logs`` ``/test_connection`` 无令牌 / 错令牌 -> 401；
* 带上正确令牌 -> 正常 200（鉴权不能把功能弄坏）；
* ``/health`` 保持开放（插件启动探测、check_env.py 靠它）；
* 401 响应体与日志里**都不出现令牌**（既不回显期望值，也不记录对方发来的值）；
* 未授权的 POST 会先把请求体排空，不会把 keep-alive 连接上的下一个请求搞乱；
* 旧公开默认令牌 ``ads-agent-local-token`` 现在必须被拒绝。

测试用临时配置（``ADS_AGENT_CONFIG``）与临时日志目录，**不碰仓库里的
config.ini / logs/**。

运行::

    python tests/test_auth.py
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run, skip  # noqa: E402

BACKEND = add_path("backend")
ADDON = add_path("addon", "ads_agent")

LEGACY = "ads-agent-local-token"
TOKEN = "unit-test-token-" + "Z9xQ" * 8          # 48 字符，不会被判为弱令牌

_TMP = tempfile.mkdtemp(prefix="ads_agent_auth_")
_CFG = os.path.join(_TMP, "config.ini")
_LOG = os.path.join(_TMP, "backend.log")

with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\n"
        "base_url = http://127.0.0.1:1\n"        # 指向死端口：探测立即失败，不联网
        "model = test-model\n"
        "api_key = test-key-not-a-real-secret\n"
        "\n[ads]\n"
        "host = 127.0.0.1\n"
        "port = 8761\n"
        f"token = {TOKEN}\n"
        "\n[agent]\n"
        "sim_timeout = 120\n"
        "sim_off_main_thread = true\n"
    )

# 必须在导入 server 之前：它在 import 时就会 config.load() + adslog.setup()
os.environ["ADS_AGENT_CONFIG"] = _CFG
# 本机回环不走任何 HTTP 代理 —— 某些环境（沙箱/公司网络）会设 HTTP_PROXY，
# 那会把"连接被拒绝"变成"代理返回 502"，让可达性断言失去意义。
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    os.environ[_k] = ("," + _existing) if _existing else "127.0.0.1,localhost"

import adslog  # noqa: E402

adslog.LOG_DIR = _TMP
adslog.LOG_FILE = _LOG
adslog.setup(echo_console=False)   # 只写临时文件，测试输出保持干净

import server  # noqa: E402

import authbridge  # noqa: E402  — 插件侧读令牌的方式

_httpd = None
_HOST = "127.0.0.1"
_PORT = 0


def _ensure_server():
    global _httpd, _PORT
    if _httpd is not None:
        return
    _httpd = ThreadingHTTPServer((_HOST, 0), server.Handler)
    _PORT = _httpd.server_address[1]
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def _teardown():
    if _httpd is not None:
        _httpd.shutdown()
        _httpd.server_close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _call(path, method="GET", token=None, body=None, timeout=20):
    """发一个请求，返回 (status, 原始响应文本)。"""
    _ensure_server()
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers[server.ads_auth.TOKEN_HEADER] = token
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _json(raw):
    return json.loads(raw)


def _cfg_text():
    with open(_CFG, encoding="utf-8") as f:
        return f.read()


def _log_text():
    try:
        with open(_LOG, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


PROTECTED_GETS = ["/config", "/tools", "/logs"]


# ---------------------------------------------------------------------------
# /health 保持开放
# ---------------------------------------------------------------------------

def test_health_is_open_and_reports_auth_required():
    status, raw = _call("/health")
    eq(status, 200, "启动探测接口必须无需令牌即可访问")
    data = _json(raw)
    eq(data.get("status"), "ok")
    eq(data.get("auth_required"), True, "应如实告知接口需要鉴权")
    ok("sim_timeout" in data and "sim_off_main_thread" in data,
       "/health 应报告仿真相关设置，供面板如实描述行为")
    not_contains(raw, TOKEN, "/health 泄露了令牌")


def test_health_does_not_leak_token_hint():
    status, raw = _call("/health")
    eq(status, 200)
    not_contains(raw, server.ads_auth.mask(TOKEN), "/health 不应出现令牌指纹")


# ---------------------------------------------------------------------------
# 无令牌 / 错令牌 -> 401
# ---------------------------------------------------------------------------

def test_protected_gets_reject_missing_token():
    for path in PROTECTED_GETS:
        status, raw = _call(path)
        eq(status, 401, f"{path} 无令牌时必须 401")
        eq(_json(raw).get("error"), "unauthorized", f"{path} 的错误体应保持中性")
        not_contains(raw, TOKEN, f"{path} 的 401 响应回显了令牌")
        not_contains(raw, "expected", f"{path} 的 401 响应泄露了内部字段")


def test_protected_gets_reject_wrong_token():
    for path in PROTECTED_GETS:
        status, raw = _call(path, token="wrong-token")
        eq(status, 401, f"{path} 错令牌时必须 401")
        not_contains(raw, TOKEN)


def test_legacy_public_token_is_now_rejected():
    """旧的公开默认令牌等于人人皆知，必须彻底失效。"""
    for path in PROTECTED_GETS:
        status, raw = _call(path, token=LEGACY)
        eq(status, 401, f"{path} 仍接受公开默认令牌")
        not_contains(raw, TOKEN)


def test_unknown_path_is_gated_too():
    """未知路径也要先鉴权，不能靠 404 探测接口是否存在。"""
    status, _ = _call("/nope")
    eq(status, 401, "未鉴权的未知路径应先返回 401")
    status, raw = _call("/nope", token=TOKEN)
    eq(status, 404, "鉴权通过后未知路径才返回 404")


def test_protected_gets_accept_correct_token():
    status, raw = _call("/tools", token=TOKEN)
    eq(status, 200, "正确令牌必须放行")
    ok(isinstance(_json(raw).get("tools"), list), "/tools 应返回工具名列表")

    status, raw = _call("/config", token=TOKEN)
    eq(status, 200)
    data = _json(raw)
    for key in ("base_url", "model", "models", "has_key", "api_key_hint",
                "sim_off_main_thread", "sim_timeout"):
        ok(key in data, f"/config 缺少字段 {key}")
    not_contains(raw, TOKEN, "/config 不应回显回环令牌")
    not_contains(raw, "test-key-not-a-real-secret", "/config 不应回显明文 API Key")

    status, raw = _call("/logs", token=TOKEN)
    eq(status, 200)
    ok(isinstance(_json(raw).get("lines"), list), "/logs 应返回日志行")


# ---------------------------------------------------------------------------
# POST 接口
# ---------------------------------------------------------------------------

def test_config_post_rejects_missing_token_and_does_not_write():
    before = _cfg_text()
    status, raw = _call("/config", method="POST",
                        body={"model": "evil-model-should-not-apply"})
    eq(status, 401, "未授权的配置写入必须被拒绝")
    eq(_cfg_text(), before, "未授权的 POST 居然改动了配置文件")
    not_contains(raw, TOKEN)


def test_chat_rejects_missing_token():
    status, raw = _call("/chat", method="POST",
                        body={"messages": [{"role": "user", "content": "hi"}],
                              "allow_python": True})
    eq(status, 401, "未授权的 /chat 必须被拒绝（allow_python 等于任意代码执行）")
    not_contains(raw, TOKEN)


def test_chat_with_token_reaches_input_validation():
    """带令牌时应当进入业务逻辑，而不是被鉴权挡住 —— 也证明鉴权不破坏功能。"""
    status, raw = _call("/chat", method="POST", token=TOKEN,
                        body={"messages": [], "allow_python": False})
    eq(status, 400, "空 messages 应返回 400（说明已通过鉴权、未调用 LLM）")
    contains(raw, "messages is empty")
    not_contains(raw, TOKEN)


def test_test_connection_rejects_missing_token():
    status, raw = _call("/test_connection", method="POST",
                        body={"base_url": "http://127.0.0.1:1"})
    eq(status, 401, "未授权的探测接口必须被拒绝")
    not_contains(raw, TOKEN)


def test_test_connection_with_token_works():
    status, raw = _call("/test_connection", method="POST", token=TOKEN,
                        body={"base_url": "http://127.0.0.1:1"}, timeout=30)
    eq(status, 200, "带令牌时应正常执行（返回可达性结果，而不是 401）")
    data = _json(raw)
    # 这里只断言"接口按业务口径回话"（不是 401、且如实报告失败），
    # 不断言 reachable 的具体值 —— 它取决于运行环境有没有代理/监听。
    eq(data.get("ok"), False, "指向不可用的地址时应如实报告失败")
    ok("reachable" in data, "应返回可达性字段")
    ok(bool(data.get("error")), "失败时应给出原因，便于面板提示")
    not_contains(raw, TOKEN, "/test_connection 不应回显回环令牌")


def test_config_post_with_token_is_applied_and_keeps_token():
    """面板保存 LLM 设置时，绝不能把回环令牌一起改掉。"""
    status, raw = _call("/config", method="POST", token=TOKEN,
                        body={"model": "test-model-updated"})
    eq(status, 200, f"带令牌的配置写入应成功: {raw[:200]}")
    eq(_json(raw).get("ok"), True)

    status, raw = _call("/config", token=TOKEN)
    eq(_json(raw).get("model"), "test-model-updated", "配置未生效")

    text = _cfg_text()
    contains(text, f"token = {TOKEN}", "保存 LLM 设置时把回环令牌改掉了")
    ok(server.ads_auth.check_token(TOKEN), "令牌在配置写入后应依然有效")


# ---------------------------------------------------------------------------
# keep-alive 与日志
# ---------------------------------------------------------------------------

def test_unauthorized_post_does_not_desync_keep_alive():
    """401 之前必须排空请求体，否则同一条连接上的下一个请求会解析错位。"""
    _ensure_server()
    conn = http.client.HTTPConnection(_HOST, _PORT, timeout=20)
    try:
        payload = json.dumps({"model": "evil-model-should-not-apply"})
        conn.request("POST", "/config", body=payload, headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(payload.encode("utf-8"))),
            server.ads_auth.TOKEN_HEADER: "wrong-token",
        })
        r1 = conn.getresponse()
        eq(r1.status, 401, "第一条请求应被拒绝")
        r1.read()

        conn.request("GET", "/tools", headers={server.ads_auth.TOKEN_HEADER: TOKEN})
        r2 = conn.getresponse()
        body2 = r2.read().decode("utf-8")
        eq(r2.status, 200, f"keep-alive 连接被未读请求体搞乱了: {body2[:200]}")
        ok(isinstance(json.loads(body2).get("tools"), list))
    finally:
        conn.close()
    not_contains(_cfg_text(), "evil-model-should-not-apply",
                 "被拒绝的请求体居然生效了")


def test_token_never_written_to_logs():
    _call("/config")                       # 制造一条拒绝日志
    _call("/chat", method="POST", body={"messages": [{"role": "user", "content": "x"}]})
    log = _log_text()
    ok(bool(log), "后端应写文件日志")
    not_contains(log, TOKEN, "令牌被写进了日志")
    not_contains(log, LEGACY, "公开默认令牌被写进了日志")
    contains(log, "拒绝未授权请求", "拒绝请求应留痕，方便排查")
    contains(log, "POST /chat", "日志应记录被拒绝的接口")


def test_rejection_log_does_not_echo_the_offered_token():
    _call("/config", token="attacker-supplied-secret-value")
    log = _log_text()
    not_contains(log, "attacker-supplied-secret-value",
                 "日志不应记录对方发来的令牌值")


# ---------------------------------------------------------------------------
# 插件侧请求头与后端校验口径一致
# ---------------------------------------------------------------------------

def test_panel_auth_header_is_accepted_by_backend():
    eq(authbridge.header_name(), server.ads_auth.TOKEN_HEADER,
       "插件与后端的请求头名不一致")
    panel_token = authbridge.token()
    ne(panel_token, "", "插件应能拿到令牌")
    status, raw = _call("/tools", token=panel_token)
    eq(status, 200, "插件用自己读到的令牌应当被后端接受")
    ok(isinstance(_json(raw).get("tools"), list))


def test_loopback_requests_bypass_http_proxy():
    """设了 HTTP_PROXY 也不能把 127.0.0.1 的请求交给代理。

    企业网络/沙箱常设 HTTP_PROXY，代理会把回环请求变成 502，
    于是"工具服务明明在跑"却报"无法连接"。
    """
    _ensure_server()
    import tools

    ok(hasattr(tools, "_LOOPBACK"), "backend/tools.py 应有绕过代理的回环 opener")
    keys = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ[k] = "http://127.0.0.1:1"      # 指向一个死代理
        with tools._LOOPBACK.open(f"http://{_HOST}:{_PORT}/health", timeout=5) as r:
            eq(r.status, 200, "回环请求被 HTTP 代理劫持了")
            ok(b"ok" in r.read(), "/health 应正常返回")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_panel_hint_does_not_contain_the_token():
    """面板给的 401 提示是给人看的，不能把令牌写进去。"""
    try:
        from panel import _unauthorized_hint
    except ImportError as e:  # 没装 PySide6 时跳过（纯后端测试仍应能跑）
        skip(f"未安装 PySide6，跳过面板侧检查：{e}")

    hint = _unauthorized_hint()
    not_contains(hint, TOKEN, "面板提示里出现了令牌")
    not_contains(hint, LEGACY)
    contains(hint, "config.ini")


if __name__ == "__main__":
    try:
        code = run(globals(), "后端接口鉴权")
    finally:
        _teardown()
    sys.exit(code)
