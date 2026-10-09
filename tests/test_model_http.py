"""模型压缩包 HTTP 接口测试（不需要真实 ADS —— 伪造一个工具服务）。

覆盖任务要求的这些点：
  * 上传通道鉴权（无令牌 401，且令牌不落响应体/日志）；
  * 二进制通道：ZIP 内容**不经过 JSON/Base64**；
  * 中文文件名正确还原（不走 Base64 也不乱码）；
  * 非 ZIP / 空体 / 错 Content-Type 的拒绝路径；
  * **工作区绑定**：没有打开工作区时明确 409，不隐式存到默认工程；
  * 上传**不依赖 LLM 服务连通**（LLM 地址指向死端口）、**不自动解压**。

关键设计：伪造一个假的 ADS 工具服务（只实现 get_workspace_info 等少数工具），
这样「后端 → 工具服务 → 模型层」整条链路都能跑，而不必真的启动 ADS。

运行::

    python tests/test_model_http.py
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

add_path("backend")
add_path("addon", "ads_agent")

TOKEN = "model-http-test-token-" + "Q7wE" * 8

_TMP = tempfile.mkdtemp(prefix="ads_agent_model_http_")
_CFG = os.path.join(_TMP, "config.ini")
_LOG = os.path.join(_TMP, "backend.log")

with open(_CFG, "w", encoding="utf-8") as _f:
    _f.write(
        "[llm]\n"
        "base_url = http://127.0.0.1:1\n"       # 死端口：证明上传不依赖 LLM 连通
        "model = test-model\n"
        "api_key = test-key-not-real\n"
        "\n[ads]\n"
        "host = 127.0.0.1\n"
        "port = 1\n"                            # 占位；_ensure_fake_ads 会改写
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

import server  # noqa: E402


# ---------------------------------------------------------------------------
# 假 ADS 工具服务
# ---------------------------------------------------------------------------

FAKE_STATE = {"workspace": None, "calls": []}


class _FakeAdsHandler(http.server.BaseHTTPRequestHandler):
    """只实现模型链路真正用到的那几个工具，其余一律如实报未知。

    刻意不实现 attach/validate 之外的工具：这样"后端调了不该调的工具"
    会立刻暴露成错误，而不是被一个过于宽松的假服务吞掉。
    """

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
        FAKE_STATE["calls"].append(name)
        if name == "get_workspace_info":
            ws = FAKE_STATE["workspace"]
            if not ws:
                self._json({"workspace_open": False, "ads_api": "fake"})
            else:
                self._json({"workspace_open": True, "path": ws,
                            "libraries": ["AI_lib"],
                            "writable_libraries": ["AI_lib"]})
            return
        self._json({"error": f"假工具服务未实现 {name}"}, 400)


_fake_httpd = None


def _ensure_fake_ads():
    """起假 ADS 工具服务，并把 config.ini 的 [ads] port 指向它。

    只改 [ads] 段里的 port —— [backend] 也有同名键，误改会让后端自己
    监听到随机端口，测试就跑不通了。改写走 ads_auth.edit_config，
    与仓库"config.ini 只有一个写入通道"的纪律一致。
    """
    global _fake_httpd
    if _fake_httpd is not None:
        return
    _fake_httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAdsHandler)
    _fake_httpd.daemon_threads = True
    port = _fake_httpd.server_address[1]
    threading.Thread(target=_fake_httpd.serve_forever, daemon=True).start()

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
            h.shutdown()
            h.server_close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _get(path, token=TOKEN):
    _ensure_server()
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}{path}",
        headers={server.ads_auth.TOKEN_HEADER: token or ""})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _upload(zip_bytes, filename="models.zip", token=TOKEN,
            content_type="application/zip", session="proj1"):
    _ensure_server()
    headers = {
        "Content-Type": content_type,
        server.ads_auth.TOKEN_HEADER: token or "",
        # HTTP 头只能放 ASCII：中文走 RFC 5987 编码，同时给一个 latin-1 版本
        "X-Ads-Filename": filename.encode("ascii", "replace").decode("ascii"),
        "X-Ads-Filename-Star": "UTF-8''" + urllib.parse.quote(filename, safe=""),
        "X-Ads-Session": session,
    }
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}/models/upload", data=zip_bytes,
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _make_zip_bytes(entries):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in entries.items():
            z.writestr(name, payload)
    return buf.getvalue()


SP = "# GHz S RI R 50\n1.0 0.5 0.9 0.9 0.5\n"


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

def test_upload_requires_token():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _upload(_make_zip_bytes({"a/b.s2p": SP}), token=None)
        eq(status, 401, "上传接口必须鉴权（它能往工作区写文件）")
        eq(data, {"error": "unauthorized"}, "401 响应体不含任何令牌信息")

        status2, _ = _upload(_make_zip_bytes({"a/b.s2p": SP}), token="wrong-token")
        eq(status2, 401, "错误令牌必须被拒")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_models_endpoints_require_token():
    status, data = _get("/models/packages", token=None)
    eq(status, 401, "/models/packages 必须鉴权")
    eq(data, {"error": "unauthorized"}, "401 不回显令牌")


# ---------------------------------------------------------------------------
# 工作区绑定
# ---------------------------------------------------------------------------

def test_no_workspace_gives_clear_error():
    FAKE_STATE["workspace"] = None
    status, data = _upload(_make_zip_bytes({"a/b.s2p": SP}))
    eq(status, 409, "没有打开工作区时应明确拒绝，而不是存到某个默认工程")
    eq(data.get("kind"), "no_workspace")
    contains(data.get("error", ""), "工作区",
             "错误文案要说清是工作区的问题，并给出下一步")


def test_no_workspace_large_upload_returns_409_without_aborting_connection():
    """大于 1 MiB 时也要读完请求体并返回明确 409，不能让客户端收到 10053。"""
    import model_attachments as MA

    FAKE_STATE["workspace"] = None
    _ensure_server()
    path = os.path.join(_TMP, "large_no_workspace.zip")
    with open(path, "wb") as stream:
        stream.write(b"x" * (2 * 1024 * 1024 + 137))
    # 直接发送流式 POST，保留对不具备上传预检查的旧客户端的后端防护测试。
    with open(path, "rb") as stream:
        request = urllib.request.Request(
            f"http://{_HOST}:{_PORT}/models/upload", data=stream, method="POST",
            headers={server.ads_auth.TOKEN_HEADER: TOKEN,
                     "Content-Type": "application/zip",
                     "Content-Length": str(os.path.getsize(path)),
                     "X-Ads-Filename": "large_no_workspace.zip"})
        try:
            with MA._opener().open(request, timeout=30) as response:
                status, data = response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            status, data = error.code, json.loads(error.read().decode())
    eq(status, 409, "旧客户端也应收到真实工作区错误，不能中途断连")
    eq(data.get("kind"), "no_workspace")
    contains(data.get("error", ""), "工作区")


def test_upload_lands_in_current_workspace():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        payload = _make_zip_bytes({
            "Kit/lib.defs": "DEFINE VendorKit ./VendorKit\n",
            "Kit/VendorKit/circuit/ael/PART1.atf": "atf",
        })
        status, data = _upload(payload, filename="vendor_kit.zip")
        eq(status, 200, f"上传应成功：{data}")
        pkg = data.get("package") or {}
        ok(pkg.get("package_id", "").startswith("pkg_"), "应返回 package_id")
        eq(pkg.get("state"), "saved", "上传后状态是已保存")
        contains(data.get("message", ""), "未解压",
                 "响应要明确说明上传不解压不加载")

        store = os.path.join(ws, "ads_agent_models")
        ok(os.path.isdir(store), f"应在工作区下建立 {store}")
        archives = os.path.join(store, "archives", pkg["package_id"])
        ok(os.path.isdir(archives) and os.listdir(archives),
           "原始 ZIP 应保存到 archives/<package_id>/")
        extracted = os.path.join(store, "extracted", pkg["package_id"])
        ok(not os.path.exists(extracted),
           "上传后不得自动解压（extracted 目录不应已生成）")

        status2, listed = _get("/models/packages")
        eq(status2, 200)
        eq([p["package_id"] for p in listed["packages"]],
           [pkg["package_id"]], "清单接口应列出刚上传的包")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_workspace_switch_keeps_assets_separate():
    """工作区切换：模型资产归属于工作区，不会跟着用户串到别的工程。"""
    ws_a = tempfile.mkdtemp(prefix="wsA_")
    ws_b = tempfile.mkdtemp(prefix="wsB_")
    try:
        payload = _make_zip_bytes({"a/b.s2p": SP})
        FAKE_STATE["workspace"] = ws_a
        _s, d_a = _upload(payload, filename="m.zip")
        FAKE_STATE["workspace"] = ws_b
        _s, listed_b = _get("/models/packages")
        eq(listed_b.get("total"), 0,
           "换到另一个工作区后不应看到上一个工作区的模型资产")

        _s, listed_a = _get("/models/packages")
        # 切回去还能看到（切的是假服务的状态，不是真的删了）
        FAKE_STATE["workspace"] = ws_a
        _s, listed_a = _get("/models/packages")
        eq(listed_a.get("total"), 1, "切回原工作区后资产仍在")
        ok(os.path.isdir(os.path.join(ws_a, "ads_agent_models")),
           "换工作区不会搬走或删除原工作区的资产")
    finally:
        shutil.rmtree(ws_a, ignore_errors=True)
        shutil.rmtree(ws_b, ignore_errors=True)


# ---------------------------------------------------------------------------
# 文件名
# ---------------------------------------------------------------------------

def test_chinese_filename_roundtrip():
    ws = tempfile.mkdtemp(prefix="ws_cn_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _upload(_make_zip_bytes({"a/b.s2p": SP}),
                               filename="村田 模型 包.zip")
        eq(status, 200, f"中文文件名上传应成功：{data}")
        name = (data.get("package") or {}).get("filename")
        eq(name, "村田 模型 包.zip",
           f"中文文件名必须原样还原，实际 {name!r}（HTTP 头只允许 ASCII，"
           f"必须靠 RFC 5987 编码往返）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_filename_with_spaces():
    ws = tempfile.mkdtemp(prefix="ws_sp_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _upload(_make_zip_bytes({"a/b.s2p": SP}),
                               filename="my vendor kit v2.zip")
        eq(status, 200, f"带空格的文件名应可用：{data}")
        eq((data.get("package") or {}).get("filename"), "my vendor kit v2.zip")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def test_reject_non_zip_extension():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _upload(b"whatever", filename="models.rar")
        eq(status, 415, "非 ZIP 扩展名要明确拒绝")
        contains(data.get("error", ""), "ZIP", "错误文案要说明首版只支持 ZIP")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_reject_wrong_content_type():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _upload(_make_zip_bytes({"a/b.s2p": SP}),
                               content_type="application/json")
        eq(status, 415, "Content-Type 必须是 zip/二进制，拒绝把文件塞进 JSON")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_reject_empty_body():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _upload(b"")
        eq(status, 400, "空请求体应被拒")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_binary_not_base64():
    """上传走二进制：ZIP 魔数原样落盘，不是被 JSON/Base64 包过。"""
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        payload = _make_zip_bytes({"a/b.s2p": SP})
        ok(payload.startswith(b"PK\x03\x04"), "样本应是真实 ZIP")
        status, _ = _upload(payload)
        eq(status, 200)
        archives = os.path.join(ws, "ads_agent_models", "archives")
        heads = []
        for dirpath, _dirs, files in os.walk(archives):
            for f in files:
                with open(os.path.join(dirpath, f), "rb") as fh:
                    heads.append(fh.read(4))
        ok(heads and all(h == b"PK\x03\x04" for h in heads),
           f"落盘文件必须保持原始 ZIP 字节（实际首字节 {heads[:1]}）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_duplicate_upload_reuses_asset():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        payload = _make_zip_bytes({"a/b.s2p": SP})
        _s1, d1 = _upload(payload, filename="m.zip", session="projA")
        _s2, d2 = _upload(payload, filename="m.zip", session="projB")
        eq(d1["package"]["package_id"], d2["package"]["package_id"],
           "相同内容重复上传必须复用同一资产")
        ok(d2.get("reused"), "第二次应标记为复用")
        _s, listed = _get("/models/packages")
        eq(listed["total"], 1, "工作区里只应有一个资产")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_list_packages_without_workspace():
    FAKE_STATE["workspace"] = None
    status, data = _get("/models/packages")
    eq(status, 409, "没有工作区时列清单也要明确报 409")


def test_inspect_missing_package():
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        status, data = _get("/models/package?id=pkg_doesnotexist0000")
        eq(status, 404, "查不存在的包应 404")
        contains(data.get("error", ""), "pkg_doesnotexist0000",
                 "错误要带上用户传的 id，否则无法定位")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_upload_does_not_need_llm():
    """LLM 地址指向死端口，上传仍必须成功（附件保存不依赖 LLM 连通）。"""
    ws = tempfile.mkdtemp(prefix="ws_")
    try:
        FAKE_STATE["workspace"] = ws
        import config as config_mod
        eq(config_mod.load()["llm_base_url"], "http://127.0.0.1:1",
           "本测试的 LLM 地址必须不可达，否则这条断言没有意义")
        status, _ = _upload(_make_zip_bytes({"a/b.s2p": SP}))
        eq(status, 200, "LLM 不可达时附件保存仍要能用")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run(globals())
    finally:
        _teardown()
    raise SystemExit(code)
