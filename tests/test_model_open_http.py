"""``POST /models/open`` 的 **HTTP 层回归**（起真实本地后端 + 假 ADS 工具服务）。

盯的是契约 §3.2（`docs/原生列表_集群实施契约_2026-10-09.md`）：面板按钮
「在 ADS 元件列表中打开」直达的那条路由。它和 import 一样有**越权面** ——
工作区与套件根目录必须由后端**可信上下文**解析并注入，请求体里塞的
路径参数一律不得透传。这里全部按真实 HTTP 状态码与响应体断言，并核对
**假 ADS 实际收到的请求参数**（不是源码子串断言）。

沿用 ``test_model_import_flow.py`` 的骨架：真后端 ``server.Handler`` +
一个假 ADS 工具服务（``_FakeAdsHandler`` / ``_ensure_server``，用
``ads_auth.edit_config`` 把假 ADS 端口写回配置），数据隔离走 ``_harness``。

覆盖场景：
1. 无工作区              -> 409（``kind == "no_workspace"``）；
2. 缺 package_id         -> 400；
3. package 不存在         -> 409，文案可读；
4. ADS 不可达            -> 502（``kind == "ads_unreachable"``），两例；
5. 成功路径              -> 200，断言注入的可信参数、伪造参数未透传、
                            result 关键键原样保留、``verified is False``；
6. 非法 body             -> 400（非 JSON / 非对象 / 空 body），不是 500 崩溃。

运行::

    python tests/test_model_open_http.py
"""

import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, ok, run  # noqa: E402

add_path("backend")
add_path("addon", "ads_agent")

TOKEN = "model-open-http-token-" + "Q7wL" * 8

_TMP = tempfile.mkdtemp(prefix="ads_agent_model_open_")
_CFG = os.path.join(_TMP, "config.ini")
_LOG = os.path.join(_TMP, "backend.log")

with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\n"
        "base_url = http://127.0.0.1:1\n"
        "model = test-model\n"
        "api_key = test-key-not-real\n"
        "\n[ads]\n"
        "host = 127.0.0.1\n"
        "port = 1\n"
        f"token = {TOKEN}\n"
        "\n[agent]\n"
        "sim_timeout = 60\n"
    )

os.environ["ADS_AGENT_CONFIG"] = _CFG
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    os.environ[_k] = ("," + _existing) if _existing else "127.0.0.1,localhost"

import adslog  # noqa: E402

adslog.LOG_DIR = _TMP
adslog.LOG_FILE = _LOG
adslog.setup(echo_console=False)

import model_store  # noqa: E402
import model_tools  # noqa: E402
import server  # noqa: E402


# ---------------------------------------------------------------------------
# 假 ADS 工具服务
# ---------------------------------------------------------------------------

#: ``workspace`` 由用例切换；``open_calls`` 记录假 ADS **实际收到**的
#: open_vendor_palette 参数（越权断言靠它）。
FAKE_STATE = {
    "workspace": None,
    "open_calls": [],
}

#: 假 ADS 的 open_vendor_palette 返回模式：``located``（正常）/
#: ``transport_error``（work 已解析、但打开调用在传输层炸 —— 用于打 502 分支）。
FAKE_STATE["open_mode"] = "located"


def _open_payload(args: dict) -> dict:
    """按契约 §3.1 造一个 ``outcome == "located"`` 的返回。

    这是 TDK 这类正常包的主路径：已在原生列表注册并定位到分类，但无法
    程序化打开窗口。关键键 ``outcome`` / ``boot`` / ``native`` / ``limits`` /
    ``verified`` 都要原样穿过后端回到面板。
    """
    return {
        "ok": True,
        "outcome": "located",
        "workspace": args.get("workspace") or "",
        "package_id": args.get("package_id") or "",
        "kit_root": args.get("kit_root") or "",
        "library": args.get("library") or "VendorKit",
        "library_attached": True,
        "boot": {
            "eesof_lib_cfg": {"path": "eesof_lib.cfg", "exists": True},
            "boot_ael": {"path": "de/ael/boot.ael", "exists": True,
                         "atf_path": "de/ael/boot.atf", "atf_exists": True},
            "loaded": True,
            "registered_components": 3,
            "palette_groups": [{
                "library": "VendorKit", "window": "schematic",
                "design_type": "", "name": "Amplifiers", "label": "Amplifiers",
                "items": 2, "owner": "boot", "source": "boot.ael"}],
            "control_files": {"lib_browser_ctl": {"path": "", "exists": False},
                              "records": []},
            "evidence": ["dm_find_item_definition 命中 3 个 cell"],
        },
        "native": {
            "component_library": {"opened": False, "method": "",
                                  "detail": "无程序化打开 API"},
            "palette": {"opened": False, "method": "",
                        "detail": "无程序化打开 API"},
            "located": {"library": "VendorKit", "category": "Amplifiers",
                        "found": True,
                        "via": "deitem_get_visible_palette_name",
                        "window": "schematic"},
        },
        "palette_api": {"create_schematic_palette": True,
                        "create_layout_palette": True},
        "limits": ["本机 ADS 2027 未提供程序化打开 Palette / Component "
                   "Library 窗口的 API（仅可定位已注册分组）"],
        "verified": False,
        "record_path": "",
        "steps": [{"step": "locate", "ok": True, "detail": "命中分类 Amplifiers"}],
        "error": "",
    }


class _FakeAdsHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/health":
            self._json({"error": "not found"}, 404)
            return
        import instance
        import paths
        self._json({
            "status": "ok", "service": instance.SERVICE_TOOLSERVER,
            "protocol": paths.PROTOCOL_VERSION,
            "identity": {"install_id": paths.install_id(),
                         "plugin_version": paths.PLUGIN_VERSION,
                         "protocol": paths.PROTOCOL_VERSION, "pid": os.getpid()},
        })

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._json({"error": "bad body"}, 400)
            return
        if self.path != "/execute":
            self._json({"error": "not found"}, 404)
            return
        name = body.get("name")
        if name == "get_workspace_info":
            ws = FAKE_STATE["workspace"]
            if not ws:
                self._json({"workspace_open": False, "ads_api": "fake"})
            else:
                self._json({"workspace_open": True, "path": ws,
                            "libraries": ["VendorKit"],
                            "writable_libraries": ["VendorKit"]})
            return
        if name == "open_vendor_palette":
            args = body.get("args") or {}
            FAKE_STATE["open_calls"].append(args)
            if FAKE_STATE["open_mode"] == "transport_error":
                # 工作区已解析、但打开调用在 ADS 传输层失败：让后端走 502。
                self._json({"error": "ADS 端打开 Palette 炸了"}, 500)
                return
            self._json(_open_payload(args))
            return
        self._json({"error": f"假工具服务未实现 {name}"}, 400)


_fake_httpd = None
_fake_port = 0


def _ensure_fake_ads():
    global _fake_httpd, _fake_port
    if _fake_httpd is not None:
        return
    _fake_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAdsHandler)
    _fake_httpd.daemon_threads = True
    _fake_port = _fake_httpd.server_address[1]
    threading.Thread(target=_fake_httpd.serve_forever, daemon=True).start()

    import ads_auth

    def _mutate(lines: list) -> None:
        section = None
        for i, raw in enumerate(lines):
            s = raw.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1].strip().lower()
            elif section == "ads" and s.lower().startswith("port"):
                lines[i] = f"port = {_fake_port}"
                return

    ads_auth.edit_config(_mutate)


_httpd = None
_HOST = "127.0.0.1"
_PORT = 0


def _ensure_server():
    global _httpd, _PORT
    _ensure_fake_ads()
    import config as config_mod
    server.CFG.update(config_mod.load())
    if _httpd is None:
        _httpd = ThreadingHTTPServer((_HOST, 0), server.Handler)
        _PORT = _httpd.server_address[1]
        threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def _teardown():
    for h in (_httpd, _fake_httpd):
        if h is not None:
            try:
                h.shutdown()
                h.server_close()
            except Exception:  # noqa: BLE001
                pass
    shutil.rmtree(_TMP, ignore_errors=True)


def _post(path, payload, token=TOKEN):
    _ensure_server()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}{path}", data=data,
        headers={"Content-Type": "application/json",
                 server.ads_auth.TOKEN_HEADER: token or ""},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _post_raw(path, raw, content_type="application/json", token=TOKEN,
              timeout=5.0):
    """发一个**不经过 json.dumps** 的原始 body（用于非法 JSON / 非对象断言）。

    返回 ``(status, data)``；客户端超时返回 ``(None, {})`` —— 万一后端对非法
    body 不响应（历史上有过挂起缺陷），也不让单个用例把整个套件拖死；这种情况
    下断言会以 ``None != 400`` 明确失败，而不是静默跳过。
    """
    _ensure_server()
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}{path}", data=raw,
        headers={"Content-Type": content_type,
                 server.ads_auth.TOKEN_HEADER: token or ""},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except (socket.timeout, TimeoutError):
        return None, {}


def _dead_port() -> int:
    """拿一个**当前没人监听**的本机端口（绑定后立刻释放）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _edit_ads_port(port: int) -> None:
    """把后端配置里的 ADS 端口改掉（写盘上的 config.ini）。

    必须写盘：``_ensure_server`` 每次请求前都会 ``config.load()`` 重新读配置，
    只改内存里的 ``server.CFG`` 会在下一次 ``_post`` 时被覆盖回原值。
    """
    import ads_auth

    def _mutate(lines: list) -> None:
        section = None
        for i, raw in enumerate(lines):
            s = raw.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1].strip().lower()
            elif section == "ads" and s.lower().startswith("port"):
                lines[i] = f"port = {port}"
                return

    ads_auth.edit_config(_mutate)


def _new_workspace():
    ws = tempfile.mkdtemp(prefix="ws_open_")
    FAKE_STATE["workspace"] = ws
    _ensure_server()
    return ws


KIT_ENTRIES = {
    "Kit/lib.defs": "DEFINE VendorKit ./VendorKit\n",
    "Kit/VendorKit/circuit/ael/PART1.atf": "atf",
}


def _make_zip_bytes(entries):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in entries.items():
            z.writestr(name, payload)
    return buf.getvalue()


def _write_zip(payload: bytes) -> str:
    path = os.path.join(_TMP, f"src_{threading.get_ident()}_{os.urandom(4).hex()}.zip")
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def _make_package(workspace):
    """在给定工作区里落一个真实的 Design Kit 包，并写回挂接结果。

    返回 ``(package_id, kit_root)`` —— kit_root 是后端能解析出的可信套件根
    （解压目录下的 ``Kit``），用来断言"由后端注入、而非请求里给的"。
    """
    root = model_store.store_root(workspace)
    package_id = model_store.save_archive(
        workspace, "kit.zip",
        source_path=_write_zip(_make_zip_bytes(KIT_ENTRIES)))["package_id"]
    model_store.scan_archive(root, package_id)
    model_store.extract_package(root, package_id)
    # 挂接结果由导入流水线写；这里直接写回，让后端能解析出 kit_root。
    model_store.update_package(
        root, package_id,
        package_kind="design_kit",
        library_attach={"kit_root": "Kit",
                        "libraries": [{"name": "VendorKit", "path": "Kit/VendorKit"}]})
    rec = model_store.get_package(root, package_id)
    rel = str(rec.get("extract_relpath") or "")
    kit_root = os.path.normpath(os.path.join(workspace, rel, "Kit"))
    return package_id, kit_root


# ---------------------------------------------------------------------------
# 1. 无工作区 / 缺参 / 包不存在
# ---------------------------------------------------------------------------

def test_open_without_workspace_is_409():
    FAKE_STATE["workspace"] = None
    status, data = _post("/models/open", {"package_id": "pkg_" + "0" * 16})
    eq(status, 409, "没有打开工作区时必须明确拒绝，不能受理")
    eq(data.get("kind"), "no_workspace")


def test_open_requires_package_id():
    ws = _new_workspace()
    try:
        status, _data = _post("/models/open", {})
        eq(status, 400, "缺 package_id 应 400")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_open_unknown_package_is_409_readable():
    ws = _new_workspace()
    try:
        status, data = _post("/models/open", {"package_id": "pkg_" + "0" * 16})
        eq(status, 409, "工作区里没有这个包时应明确拒绝")
        ok(data.get("error"), f"必须给出可读原因: {data}")
        contains(data.get("error", ""), "pkg_",
                 "错误文案里应带上查不到的 package_id，便于排查")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. ADS 不可达
# ---------------------------------------------------------------------------

def test_open_ads_unreachable_open_call_is_502():
    """工作区能解析、但 ADS 端打开调用在传输层失败 -> 502 ads_unreachable。

    （假 ADS 让 _verify_toolserver 与 get_workspace_info 正常，只让
    open_vendor_palette 的 /execute 返回 500，从而命中 `_handle_model_open`
    里 ``except tools_mod.AdsToolError`` 的 502 分支。）
    """
    ws = _new_workspace()
    package_id, _kit_root = _make_package(ws)
    FAKE_STATE["open_mode"] = "transport_error"
    try:
        status, data = _post("/models/open", {"package_id": package_id})
        eq(status, 502, f"ADS 打开调用失败必须如实报 502: {data}")
        eq(data.get("kind"), "ads_unreachable")
    finally:
        FAKE_STATE["open_mode"] = "located"
        shutil.rmtree(ws, ignore_errors=True)


def test_open_with_ads_port_down_is_502():
    """把假 ADS 端口指到一个没人监听的端口：ADS **完全不可达**。

    后端已在解析工作区时就识别出这是传输层故障（``current_workspace`` 抛
    ``AdsUnreachableError``），``_handle_model_open`` 据此如实回 **502 /
    kind=ads_unreachable** —— 而不是与「无工作区」同形的 409。契约 §3.2 要求
    如实区分，这里锁定它。
    """
    ws = _new_workspace()
    dead = _dead_port()
    _edit_ads_port(dead)
    try:
        status, data = _post("/models/open", {"package_id": "pkg_" + "0" * 16})
        eq(status, 502, f"ADS 端口不可达必须如实报 502: {data}")
        eq(data.get("kind"), "ads_unreachable")
        contains(data.get("error", ""), "ADS")
    finally:
        _edit_ads_port(_fake_port)
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 成功路径（含越权断言）
# ---------------------------------------------------------------------------

def test_open_success_injects_trusted_context():
    ws = _new_workspace()
    package_id, kit_root = _make_package(ws)
    FAKE_STATE["open_calls"] = []
    forged = "D:/evil-forged-path"
    try:
        status, data = _post("/models/open", {
            "package_id": package_id,
            "library": "VendorKit",
            "category": "Amplifiers",
            "view": "schematic",
            # 伪造的路径参数：越权面，必须被后端丢弃
            "workspace": forged,
            "kit_root": forged,
            "path": forged,
            "root": forged,
        })
        eq(status, 200, f"成功路径应 200: {data}")
        eq(data.get("ok"), True)
        ok("result" in data and isinstance(data["result"], dict), f"应含 result: {data}")
        ok("package" in data and isinstance(data["package"], dict), f"应含 package: {data}")
        result = data["result"]

        # (a) result 里的关键键原样保留
        eq(result.get("outcome"), "located", "outcome 必须原样穿过")
        eq((result.get("boot") or {}).get("loaded"), True, "boot.loaded 原样保留")
        eq((result.get("native") or {}).get("located", {}).get("found"), True,
           "native.located.found 原样保留")
        ok(result.get("limits"), "limits 必须非空并原样保留")
        eq(result.get("verified"), False, "verified 必须是 False（未真正打开窗口）")

        # (b) 假 ADS **实际收到**的参数
        ok(FAKE_STATE["open_calls"], "假 ADS 应实际收到 open_vendor_palette 调用")
        got = FAKE_STATE["open_calls"][-1]

        # 工作区由后端注入 = 可信当前工作区，绝不是请求里给的伪造值
        eq(got.get("workspace"), ws, "workspace 必须是后端注入的可信工作区")
        ne(got.get("workspace"), forged, "伪造的 workspace 绝不能被透传")

        # kit_root 由后端从清单解析（解压目录内的真实路径），非请求里的伪造值
        eq(got.get("kit_root"), kit_root,
           "kit_root 必须是后端解析出的可信路径（解压目录内）")
        ne(got.get("kit_root"), forged, "伪造的 kit_root 绝不能被透传")

        # 请求里塞的任意路径参数都不得出现在透传参数里
        for bad in ("path", "root"):
            eq(bad in got, False, f"请求里的伪造 '{bad}' 不应被透传: {got}")

        # 允许透传的白名单参数照常生效（library / category / view）
        eq(got.get("package_id"), package_id)
        eq(got.get("library"), "VendorKit", "library 是白名单参数，应透传")
        eq(got.get("category"), "Amplifiers", "category 是白名单参数，应透传")
        eq(got.get("view"), "schematic", "view 是白名单参数，应透传")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. 非法 body：可预期错误码，不是 500 崩溃
# ---------------------------------------------------------------------------

def test_open_empty_body_is_400():
    """空 body -> ``_read_json`` 返回 {}，落到「缺 package_id」的 400。"""
    status, data = _post_raw("/models/open", b"")
    eq(status, 400, f"空 body 应 400: {data}")
    ok(data.get("error"), f"应给出可读错误: {data}")


def test_open_bad_json_is_400():
    status, data = _post_raw("/models/open", b"this is not json {")
    eq(status, 400, f"非法 JSON 应 400 而不是 500（更不能挂起）: {data}")
    ok(data.get("error"), f"应给出可读错误: {data}")


def test_open_non_object_body_is_400():
    status, data = _post_raw("/models/open", b"[1, 2, 3]")
    eq(status, 400, f"非 JSON 对象应 400 而不是 500: {data}")
    ok(data.get("error"), f"应给出可读错误: {data}")


if __name__ == "__main__":
    try:
        code = run(globals())
    finally:
        _teardown()
    raise SystemExit(code)
