"""工具服务身份校验 + 端口冲突诊断（不需要 ADS，纯标准库）。

改造前有两处"看端口不看身份"：

**后端侧** —— ``tools.call`` 直接往 ``[ads] port`` 发带令牌的 ``/execute``。
端口上坐着别的程序 / 另一份安装 / 另一个 ADS 实例时，请求照样发出去，
对方回 401 或 404，用户看到的是"无法连接 ADS 端工具服务"—— 指向完全错误的
排查方向。现在派发前先校验 ``/health`` 的 service / protocol / install_id，
不匹配就带着"到底是谁占了端口"直接报错。

**插件侧** —— 工具服务只管 bind，绑不上就抛裸 ``OSError([WinError 10048])``；
也从不登记自己，于是"多开一个 ADS"和"别的软件占了端口"在用户眼里一模一样。
现在：起来了就写 ``runtime/toolserver_<port>.json``，退出时清掉；
绑不上就翻译成人话（另一个 ADS 实例 / 另一份安装 / 别的程序）。

运行::

    python tests/test_tool_identity.py
"""

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")
add_path("backend")

TOKEN = "identity-test-token-" + "Z9y8" * 8
_TMP = tempfile.mkdtemp(prefix="ads_agent_identity_")
_CFG = os.path.join(_TMP, "config.ini")
with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\nmodel = m\n\n"
        "[ads]\nhost = 127.0.0.1\nport = 1\n"
        f"token = {TOKEN}\n\n"
        "[agent]\nsim_off_main_thread = true\nsim_timeout = 900\n"
    )
os.environ["ADS_AGENT_CONFIG"] = _CFG

import config  # noqa: E402
import instance  # noqa: E402
import paths  # noqa: E402
import tools  # noqa: E402

import toolserver  # noqa: E402

toolserver._LOG_DIR = os.path.join(_TMP, "logs")
toolserver._LOG_FILE = os.path.join(toolserver._LOG_DIR, "ads_toolserver.log")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeServer:
    """假的对端服务。``health`` 决定它自报什么身份；``service`` 为 None 时
    返回一个非本插件的 200 响应（模拟"端口被别的程序占了"）。"""

    def __init__(self, health: dict | None):
        self.health = health
        self.port = _free_port()
        self.health_hits = 0
        self.execute_hits = 0
        self.last_token = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: A003 — 关掉 stderr 噪声
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    outer.health_hits += 1
                    if outer.health is None:
                        self._send({"hello": "i am some other program"})
                    else:
                        self._send(outer.health)
                    return
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                outer.execute_hits += 1
                outer.last_token = self.headers.get("X-Ads-Agent-Token")
                self._send({"stdout": "fake 执行成功\n", "ok": True})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def cfg(self) -> dict:
        cfg = dict(config.load())
        cfg["ads_host"] = "127.0.0.1"
        cfg["ads_port"] = self.port
        return cfg

    def close(self):
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _our_health(install_id=None, protocol=None) -> dict:
    return {
        "status": "ok",
        "service": instance.SERVICE_TOOLSERVER,
        "protocol": paths.PROTOCOL_VERSION if protocol is None else protocol,
        "identity": {
            "install_id": paths.install_id() if install_id is None else install_id,
            "plugin_version": paths.PLUGIN_VERSION,
            "protocol": paths.PROTOCOL_VERSION if protocol is None else protocol,
            "pid": os.getpid(),
        },
    }


def _clear_verdict_cache():
    tools._TOOLSERVER_VERDICT.clear()


# ---------------------------------------------------------------------------
# 后端：派发前校验对端身份
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 自报字段与探测方读取的字段必须对得上（真实事故：键名不一致）
# ---------------------------------------------------------------------------

def test_real_toolserver_health_is_recognized():
    """真起一个 toolserver 的 /health，走真实的 probe + evaluate。

    这条用例是为真实事故加的：toolserver 自报的键名（``server``）和探测方
    读取的键名（``service``）不一致 —— ``/health`` 手测一切正常，自动探测却
    一律判成"端口被其它程序占用"，于是"派发前的身份校验"变成了永远拒绝。
    用假 payload 的用例抓不到这种错位，必须拿真 handler 跑一遍。
    """
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), toolserver._Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        probed = instance.probe(f"http://127.0.0.1:{port}", timeout=3)
        ok(probed.get("reachable"), f"真实 /health 应当可达：{probed.get('error')}")
        verdict = instance.evaluate(probed, "toolserver")
        ok(verdict.get("usable"),
           f"真实 /health 必须被认成本插件的工具服务，实得 {verdict}")
        eq(verdict.get("reason"), "ok", "判定原因应当是 ok")
        ident = verdict.get("identity") or {}
        eq(ident.get("install_id"), paths.install_id(), "身份里的 install_id 要对得上")
        eq(ident.get("protocol"), paths.PROTOCOL_VERSION, "协议版本要与本端一致")
        eq(probed["payload"].get("service"), instance.SERVICE_TOOLSERVER,
           "规范字段 service 必须有")
        eq(probed["payload"].get("server"), instance.SERVICE_TOOLSERVER,
           "历史别名 server 要与 service 同值")
    finally:
        httpd.shutdown()


def test_service_name_has_a_single_source():
    """服务名/协议版本不能各写一份 —— 一处改了另一处没改就会静默失配。"""
    eq(toolserver._service_name(), instance.SERVICE_TOOLSERVER,
       "工具服务自报的服务名必须取自 instance.SERVICE_TOOLSERVER")
    eq(toolserver._protocol_version(), paths.PROTOCOL_VERSION,
       "工具服务自报的协议版本必须取自共享常量")


def test_legacy_server_key_is_still_recognized():
    """只带历史键名 ``server`` 的对端（旧版插件）也要能认出来。"""
    verdict = instance.evaluate(
        {"reachable": True, "error": "", "payload": {
            "server": instance.SERVICE_TOOLSERVER,
            "protocol": paths.PROTOCOL_VERSION,
            "identity": {"install_id": paths.install_id(),
                         "protocol": paths.PROTOCOL_VERSION},
        }},
        "toolserver",
    )
    ok(verdict.get("usable"), f"历史键名应当兼容，实得 {verdict}")


def test_own_toolserver_is_dispatched():
    _clear_verdict_cache()
    srv = FakeServer(_our_health())
    try:
        res = tools.call(srv.cfg(), "run_python", {"code": "print(1)"})
        eq(res.get("ok"), True, "身份对得上时应当正常派发")
        eq(srv.execute_hits, 1, "应当确实发出了 /execute")
        eq(srv.last_token, TOKEN, "令牌应当随请求头送达")
    finally:
        srv.close()


def test_foreign_program_on_port_is_refused():
    """端口被别的程序占了：必须拒绝派发，且**不要**把令牌发出去。"""
    _clear_verdict_cache()
    srv = FakeServer(None)
    try:
        try:
            tools.call(srv.cfg(), "run_python", {"code": "print(1)"})
        except tools.AdsToolError as e:
            contains(str(e), "不是本插件", "应当明确指出对端不是本插件")
        else:
            raise AssertionError("外来服务占用端口时不该派发")
        eq(srv.execute_hits, 0, "拒绝时绝不能把带令牌的 /execute 发出去")
    finally:
        srv.close()


def test_other_install_is_refused():
    """端口上是**另一份安装**的工具服务：令牌不属于它，不能连。"""
    _clear_verdict_cache()
    srv = FakeServer(_our_health(install_id="ffffffffffffffff"))
    try:
        try:
            tools.call(srv.cfg(), "run_python", {"code": "print(1)"})
        except tools.AdsToolError as e:
            contains(str(e), "另一份", "应当指出是另一份安装")
            contains(str(e), paths.install_id(), "应当带上本机 install_id 便于区分")
        else:
            raise AssertionError("归属不对时不该派发")
        eq(srv.execute_hits, 0, "拒绝时不应发出 /execute")
    finally:
        srv.close()


def test_other_service_name_is_refused():
    """同一个端口被配成了后端自己的地址（service 对不上）。"""
    _clear_verdict_cache()
    health = _our_health()
    health["service"] = instance.SERVICE_BACKEND
    srv = FakeServer(health)
    try:
        try:
            tools.call(srv.cfg(), "run_python", {"code": "print(1)"})
        except tools.AdsToolError as e:
            contains(str(e), "不是本插件", "服务名对不上要报配置问题")
        else:
            raise AssertionError("service 不匹配时不该派发")
    finally:
        srv.close()


def test_protocol_mismatch_is_refused():
    _clear_verdict_cache()
    srv = FakeServer(_our_health(protocol=paths.PROTOCOL_VERSION + 1))
    try:
        try:
            tools.call(srv.cfg(), "run_python", {"code": "print(1)"})
        except tools.AdsToolError as e:
            contains(str(e), "协议", "协议不一致要单独报出来")
        else:
            raise AssertionError("协议不一致时不该派发")
    finally:
        srv.close()


def test_unreachable_gives_install_hint():
    _clear_verdict_cache()
    port = _free_port()  # 没人监听
    cfg = dict(config.load())
    cfg["ads_host"], cfg["ads_port"] = "127.0.0.1", port
    try:
        tools.call(cfg, "run_python", {"code": "print(1)"})
    except tools.AdsToolError as e:
        contains(str(e), "无法连接", "连不上要保留原来的排查指引")
        contains(str(e), "Tools > ADS Agent", "要提示怎么把插件开起来")
    else:
        raise AssertionError("没人监听时应当报错")


def test_verdict_is_cached_within_ttl():
    """校验结果要缓存：不能每次工具调用都多打一个 /health。"""
    _clear_verdict_cache()
    srv = FakeServer(_our_health())
    try:
        cfg = srv.cfg()
        tools.call(cfg, "run_python", {"code": "print(1)"})
        tools.call(cfg, "run_python", {"code": "print(2)"})
        tools.call(cfg, "run_python", {"code": "print(3)"})
        eq(srv.health_hits, 1, "TTL 内只探测一次")
        eq(srv.execute_hits, 3, "但每次调用都要真的派发")
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# 插件侧：登记与端口冲突诊断
# ---------------------------------------------------------------------------

def test_toolserver_registers_and_clears_instance():
    """起来要留登记、退出要清掉 —— 否则多开冲突全靠 pid 兜底猜。"""
    instance.clear_instance("toolserver")
    ok(instance.read_instance("toolserver") is None, "前置：先清干净")
    try:
        toolserver._register_instance("127.0.0.1", 5999)
        rec = instance.read_instance("toolserver")
        ok(rec is not None, "登记应当写出来")
        eq(rec.get("port"), 5999, "端口要记下来")
        eq(rec.get("kind"), "toolserver", "kind 要标清是工具服务")
        eq(rec.get("install_id"), paths.install_id(), "要带上本次安装的身份")
        eq(rec.get("pid"), os.getpid(), "要带上 pid 供存活判定")
    finally:
        toolserver._clear_instance()
    ok(instance.read_instance("toolserver") is None, "退出后登记应当被清掉")


def test_bind_conflict_says_another_ads_instance():
    """同一个安装的另一个 ADS 实例占着端口 —— 要明说"多开"。"""
    srv = FakeServer(_our_health())
    try:
        msg = toolserver._bind_conflict_detail("127.0.0.1", srv.port,
                                              OSError(10048, "port in use"))
        contains(msg, "另一个 ADS 实例", "多开冲突要认出来")
        contains(msg, "单实例", "要说明本版本的边界")
        contains(msg, "[ads] port", "要给出可执行的下一步")
    finally:
        srv.close()


def test_bind_conflict_says_other_install():
    srv = FakeServer(_our_health(install_id="0123456789abcdef"))
    try:
        msg = toolserver._bind_conflict_detail("127.0.0.1", srv.port,
                                               OSError(10048, "port in use"))
        contains(msg, "另一份 ADS Agent 安装", "归属不对要单独说")
    finally:
        srv.close()


def test_bind_conflict_says_foreign_program():
    srv = FakeServer(None)
    try:
        msg = toolserver._bind_conflict_detail("127.0.0.1", srv.port,
                                               OSError(10048, "port in use"))
        contains(msg, "不是本插件", "外来程序要认出来")
        contains(msg, "空闲端口", "要提示换端口")
    finally:
        srv.close()


def test_bind_conflict_never_leaks_bare_oserror_only():
    """连不上（端口被防火墙/半开占用那种）也要给出人话，而不是只有一个 errno。"""
    msg = toolserver._bind_conflict_detail("127.0.0.1", _free_port(),
                                           OSError(10048, "port in use"))
    contains(msg, "[ads] port", "至少要有可执行的下一步")


if __name__ == "__main__":
    sys.exit(run(globals()))
