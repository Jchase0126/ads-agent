"""模型导入的**生命周期与编排**回归（起真实本地服务 + 假 ADS 工具服务）。

这个文件盯的是 2026-10-08 审查里那几条"实现看着有、实际不成立"的缺陷：

1. ``POST /models/import`` 一个请求**两次响应**（后台线程复用同一个 Handler
   写 socket）。这里用**裸 socket** 验证：读完第一个响应后继续读，第二个
   响应**永远不该出现**；
2. ADS 返回 ``ok=False`` / ``cancelled`` / ``conflicts`` / ``partial`` /
   ``workspace_mismatch`` 时，编排层过去只捕获异常、不检查业务结果，于是
   "挂接 0 个库"也会被记成 ``attach ok=True`` 并推进到待验证；
3. 附件按钮与 LLM 工具过去各走各的（锁与状态都不共享）；
4. 后端重启后未完成的导入会永久停在"进行中"。

运行::

    python tests/test_model_import_flow.py
"""

import http.client
import http.server
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

add_path("backend")
add_path("addon", "ads_agent")

TOKEN = "model-import-flow-token-" + "Z3xQ" * 8

_TMP = tempfile.mkdtemp(prefix="ads_agent_model_import_")
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

import model_orchestration  # noqa: E402
import model_store  # noqa: E402
import model_tools  # noqa: E402
import server  # noqa: E402


# ---------------------------------------------------------------------------
# 假 ADS 工具服务
# ---------------------------------------------------------------------------

#: attach 的行为由测试按用例切换；``slow`` 用 entered/release 两个事件把
#: 挂接卡住，用来制造"排队""挂接前切换工作区"这类竞态。
FAKE_STATE = {
    "workspace": None,
    "attach": "ok",
    "entered": None,
    "release": None,
    "jobs": [],
    "cancel_calls": [],
    "attach_calls": 0,
}

SP = "# GHz S RI R 50\n1.0 0.5 0.9 0.9 0.5\n"


def _attach_payload(kind: str, package_id: str, workspace: str) -> dict:
    lib = {"name": "VendorKit", "path": "VendorKit", "already_attached": False}
    base = {"package_id": package_id, "workspace": workspace,
            "kit_root": "Kit", "mode": "READ_ONLY", "attached_at": "now",
            "libraries": [], "already_attached": False, "conflicts": [],
            "failed": [], "notes": []}
    if kind == "ok":
        return {**base, "ok": True, "libraries": [lib], "attached_at": "now"}
    if kind == "zero":
        return {**base, "ok": False, "error": "没有成功挂接任何库",
                "failed": [{"name": "VendorKit", "path": "x",
                            "reason": "库目录不存在"}]}
    if kind == "partial":
        return {**base, "ok": False, "libraries": [lib],
                "failed": [{"name": "OtherKit", "path": "y",
                            "reason": "add_library 抛异常"}]}
    if kind == "conflicts":
        return {**base, "ok": False, "libraries": [lib],
                "conflicts": [{"name": "ClashKit", "wanted_path": "a",
                               "existing_path": "b", "reason": "同名不同路径"}]}
    if kind == "cancelled":
        return {**base, "ok": False, "cancelled": True, "libraries": [lib]}
    if kind == "workspace_mismatch":
        return {"ok": False, "kind": "workspace_mismatch",
                "error": "工作区不一致，已停止挂接（目标与当前不是同一个）",
                "package_id": package_id, "workspace": workspace}
    if kind == "raise":
        return None
    raise AssertionError(f"未知的 attach 行为 {kind}")


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
        if self.path == "/cancel":
            FAKE_STATE["cancel_calls"].append(list(body.get("job_ids") or []))
            self._json({"cancelled": list(body.get("job_ids") or []),
                        "running": [], "unknown": []})
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
                            "libraries": ["AI_lib"],
                            "writable_libraries": ["AI_lib"]})
            return
        if name == "attach_design_kit":
            FAKE_STATE["attach_calls"] += 1
            FAKE_STATE["jobs"].append(str(body.get("job_id") or ""))
            mode = FAKE_STATE["attach"]
            if mode == "slow":
                FAKE_STATE["entered"].set()
                FAKE_STATE["release"].wait(15)
                mode = "ok"
            payload = _attach_payload(mode, body.get("package_id") or "",
                                      body.get("workspace") or "")
            if payload is None:
                self._json({"error": "ADS 端挂接炸了"}, 500)
                return
            self._json(payload)
            return
        self._json({"error": f"假工具服务未实现 {name}"}, 400)


_fake_httpd = None


def _ensure_fake_ads():
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
            try:
                h.shutdown()
                h.server_close()
            except Exception:  # noqa: BLE001
                pass
    shutil.rmtree(_TMP, ignore_errors=True)


def _get(path, token=TOKEN):
    _ensure_server()
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}{path}",
        headers={server.ads_auth.TOKEN_HEADER: token or ""})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


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


def _make_zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, payload in entries.items():
            z.writestr(name, payload)
    return buf.getvalue()


def _upload(entries, filename="kit.zip", workspace=None):
    """上传一个包并返回 package_id。"""
    if workspace is not None:
        FAKE_STATE["workspace"] = workspace
    _ensure_server()
    payload = _make_zip_bytes(entries)
    headers = {
        "Content-Type": "application/zip",
        server.ads_auth.TOKEN_HEADER: TOKEN,
        "X-Ads-Filename": filename,
        "X-Ads-Filename-Star": "UTF-8''" + filename,
        "X-Ads-Session": "proj1",
    }
    req = urllib.request.Request(
        f"http://{_HOST}:{_PORT}/models/upload", data=payload,
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data.get("package") or {}).get("package_id")


KIT_ENTRIES = {
    "Kit/lib.defs": "DEFINE VendorKit ./VendorKit\n",
    "Kit/VendorKit/circuit/ael/PART1.atf": "atf",
}
SP_ENTRIES = {"a/b.s2p": SP}


def _reset_attach_count():
    """把挂接调用计数清零。

    ``attach_calls`` 是假 ADS 服务上的**全局**计数器，跨用例累加。
    断言"并入后只挂接了 1 次""取消后一次都没挂接"时必须先清零 ——
    否则断言读到的是前面用例留下的历史值（实测 8 / 11），测的其实
    是"到目前为止一共挂了几次"，与本用例要验证的行为无关。
    """
    FAKE_STATE["attach_calls"] = 0


def _new_workspace():
    """新建一个工作区临时目录并切成当前工作区。"""
    ws = tempfile.mkdtemp(prefix="ws_")
    FAKE_STATE["workspace"] = ws
    _ensure_server()
    return ws


def _wait_op(op_id, timeout=30.0, want=()):
    """轮询直到操作终结（或到达期望状态）。返回最终记录。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _s, data = _get(f"/models/op?id={op_id}")
        last = data
        if not data.get("ok"):
            time.sleep(0.05)
            continue
        state = data.get("state")
        if want and state in want:
            return data
        if state in ("succeeded", "failed", "cancelled", "interrupted",
                     "awaiting_user"):
            return data
        time.sleep(0.05)
    return last or {}


def _package(workspace, package_id):
    root = model_store.store_root(workspace)
    try:
        return model_store.get_package(root, package_id)
    except model_store.ModelStoreError:
        return None


# ---------------------------------------------------------------------------
# 1. HTTP 生命周期：一个请求只能有一次响应
# ---------------------------------------------------------------------------

def test_import_returns_exactly_one_response():
    """裸 socket 验证：202 之后**再也读不到第二个响应**。

    旧实现里后台线程会拿同一个 Handler 再写一次 —— 那一次往往写进已关闭
    或正被复用的 socket。这里把挂接卡住（15s），留足时间让"第二次响应"出现。
    """
    ws = _new_workspace()
    FAKE_STATE["attach"] = "slow"
    FAKE_STATE["entered"] = threading.Event()
    FAKE_STATE["release"] = threading.Event()
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _ensure_server()
        body = json.dumps({"package_id": package_id}).encode("utf-8")
        conn = http.client.HTTPConnection(_HOST, _PORT, timeout=30)
        try:
            conn.putrequest("POST", "/models/import", skip_host=False)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader(server.ads_auth.TOKEN_HEADER, TOKEN)
            conn.endheaders()
            conn.send(body)
            resp = conn.getresponse()
            raw = resp.read()
            eq(resp.status, 202, "导入受理必须回 202（不是 200，也不是先 200 后补一次）")
            data = json.loads(raw.decode("utf-8"))
            ok(data.get("op_id"), "202 里必须带 op_id，否则无法查询进度")
            eq(data.get("accepted"), True)

            # 关键：后台还卡在挂接上，此刻**绝不能**再有第二个响应
            sock = conn.sock
            sock.settimeout(2.0)
            try:
                extra = sock.recv(256)
            except (socket.timeout, TimeoutError):
                extra = b""          # keep-alive 上一直没再来数据 = 没有第二次响应
            eq(extra, b"", "一个导入请求只能有一次响应；后台结果必须走 /models/op 查询")
        finally:
            conn.close()

        op = _wait_op(data["op_id"], timeout=20, want=("running", "queued"))
        eq(op.get("state"), "running", f"后台应仍在执行: {op}")
    finally:
        FAKE_STATE["release"].set()
        FAKE_STATE["attach"] = "ok"


def test_op_query_reports_terminal_result():
    """Touchstone 包：流水线跑完 -> op succeeded，资产停在 pending_verify。"""
    ws = _new_workspace()
    try:
        package_id = _upload(SP_ENTRIES, workspace=ws)
        status, data = _post("/models/import", {"package_id": package_id})
        eq(status, 202, f"受理应返回 202: {data}")
        op = _wait_op(data["op_id"])
        eq(op.get("state"), "succeeded", f"导入应成功: {op}")
        names = [s.get("step") for s in (op.get("steps") or [])]
        for want in ("scan", "extract", "index", "attach"):
            ok(want in names, f"步骤里应有 {want}，实际 {names}")
        ok(all(s.get("ok") for s in op["steps"]),
           f"成功的导入里不能有失败步骤: {op['steps']}")
        rec = _package(ws, package_id)
        eq(rec.get("state"), "pending_verify",
           "Touchstone 解压完成也只能是待验证，绝不能直接就绪")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_query_unknown_op_id_gives_404():
    status, data = _get("/models/op?id=op_does_not_exist")
    eq(status, 404, "查不存在的 op_id 必须 404，不能返回 200 + 空")
    eq(data.get("known"), False)


def test_query_op_requires_id():
    status, _data = _get("/models/op")
    eq(status, 400, "缺 id 应 400")


def test_import_requires_package_id():
    ws = _new_workspace()
    try:
        status, _data = _post("/models/import", {})
        eq(status, 400)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_import_without_workspace_is_409():
    FAKE_STATE["workspace"] = None
    status, data = _post("/models/import", {"package_id": "pkg_" + "0" * 16})
    eq(status, 409, "没有打开工作区时必须明确拒绝")
    eq(data.get("kind"), "no_workspace")


def test_import_unknown_package_is_404():
    ws = _new_workspace()
    try:
        status, data = _post("/models/import",
                             {"package_id": "pkg_" + "0" * 16})
        eq(status, 404, "工作区里没有这个包时应 404，而不是受理一条必然失败的操作")
        contains(data.get("error", ""), "pkg_")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. ADS 业务结果判定（ok=False / cancelled / conflicts / partial / mismatch）
# ---------------------------------------------------------------------------

def _run_attach_case(kind: str):
    ws = _new_workspace()
    FAKE_STATE["attach"] = kind
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        eq(_s, 202, f"受理失败: {data}")
        op = _wait_op(data["op_id"])
        return ws, package_id, op
    finally:
        FAKE_STATE["attach"] = "ok"


def test_attach_workspace_mismatch_is_not_success():
    ws, package_id, op = _run_attach_case("workspace_mismatch")
    try:
        eq(op.get("state"), "failed", "工作区不一致必须判定为失败")
        eq(op.get("error_kind"), "workspace_mismatch")
        contains(op.get("error", ""), "工作区")
        steps = {s.get("step"): s.get("ok") for s in (op.get("steps") or [])}
        ne_attach_ok = steps.get("attach")
        eq(ne_attach_ok, False,
           "审查里的真实缺陷：ADS 返回 ok=False 时后端仍记 attach ok=True —— "
           "这里绝不能再出现")
        eq(_package(ws, package_id).get("state"), "failed")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_zero_attached_libraries_is_not_success():
    """零成功不算完成：一个库都没挂上时不能 succeeded，也不能进待验证。"""
    ws, package_id, op = _run_attach_case("zero")
    try:
        eq(op.get("state"), "failed", "0 个库挂接成功不能算导入完成")
        contains(op.get("error", ""), "没有成功挂接任何库")
        eq(_package(ws, package_id).get("state"), "failed",
           "资产不能停在 pending_verify（那等于宣称已挂接可用）")
        contains(op.get("error", ""), "工作区库定义未被修改",
                 "没走 lib.defs 退路时才能这么说，不能无条件声称")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_partial_attach_keeps_real_record():
    ws, package_id, op = _run_attach_case("partial")
    try:
        eq(op.get("state"), "failed", "部分挂接不是成功")
        eq(op.get("partial"), True)
        result = op.get("result") or {}
        eq(len(result.get("attached_libraries") or []), 1,
           "已挂上的库必须留在记录里（用户看得到真实副作用）")
        eq(len(result.get("failed") or []), 1, "失败的部分要说清")
        contains(op.get("error", ""), "OtherKit")
        eq(_package(ws, package_id).get("state"), "failed")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_conflicts_wait_for_user():
    ws, package_id, op = _run_attach_case("conflicts")
    try:
        eq(op.get("state"), "awaiting_user",
           "同名库冲突要交回用户决定，不能替用户选")
        eq(op.get("partial"), True)
        contains(op.get("error", ""), "ClashKit")
        eq(_package(ws, package_id).get("state"), "awaiting_user")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_ads_cancelled_mid_attach_is_cancelled():
    ws, package_id, op = _run_attach_case("cancelled")
    try:
        eq(op.get("state"), "cancelled", "ADS 端报告取消就必须记为取消，不是失败")
        eq(_package(ws, package_id).get("state"), "cancelled")
        eq(len((op.get("result") or {}).get("attached_libraries") or []), 1,
           "取消前已挂上的库要如实保留（不做回滚，删别人的库引用是破坏性的）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_ads_error_is_failure_not_success():
    ws, package_id, op = _run_attach_case("raise")
    try:
        eq(op.get("state"), "failed")
        eq(op.get("error_kind"), "ads_unreachable")
        eq(_package(ws, package_id).get("state"), "failed")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 幂等与并入
# ---------------------------------------------------------------------------

def test_same_request_id_is_idempotent():
    ws = _new_workspace()
    try:
        package_id = _upload(SP_ENTRIES, workspace=ws)
        _s1, d1 = _post("/models/import",
                        {"package_id": package_id, "request_id": "req-abc"})
        _s2, d2 = _post("/models/import",
                        {"package_id": package_id, "request_id": "req-abc"})
        eq(_s2, 202)
        eq(d2.get("op_id"), d1.get("op_id"), "同一 request_id 必须复用同一条操作")
        eq(d2.get("idempotent_replayed"), True)
        op = _wait_op(d1["op_id"])
        eq(op.get("state"), "succeeded")
        _s3, listed = _get("/models/ops?limit=50")
        same = [o for o in listed["ops"]
                if str(o.get("request_key")) == "req-abc"]
        eq(len(same), 1, "重试不得产生第二条操作记录")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_second_submit_joins_running_op():
    """按钮连点两次 / 按钮与 LLM 同时发起：并入同一条，不并发。"""
    ws = _new_workspace()
    _reset_attach_count()
    FAKE_STATE["attach"] = "slow"
    FAKE_STATE["entered"] = threading.Event()
    FAKE_STATE["release"] = threading.Event()
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s1, d1 = _post("/models/import", {"package_id": package_id})
        ok(FAKE_STATE["entered"].wait(10), "第一次导入应已进入挂接")
        _s2, d2 = _post("/models/import", {"package_id": package_id})
        eq(d2.get("op_id"), d1.get("op_id"),
           "同一工作区同一包已有进行中的导入时，新请求必须并入而不是另起一条")
        eq(d2.get("idempotent_replayed"), True)
        eq(FAKE_STATE["attach_calls"], 1, "并入不得产生第二次挂接（幂等的硬要求）")
    finally:
        FAKE_STATE["release"].set()
        FAKE_STATE["attach"] = "ok"
        _wait_op(d1["op_id"], timeout=20)
        shutil.rmtree(ws, ignore_errors=True)


def test_llm_tool_shares_the_same_op():
    """LLM 入口与附件按钮共用同一条操作（同一把锁、同一份记录）。"""
    ws = _new_workspace()
    _reset_attach_count()
    FAKE_STATE["attach"] = "slow"
    FAKE_STATE["entered"] = threading.Event()
    FAKE_STATE["release"] = threading.Event()
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        ok(FAKE_STATE["entered"].wait(10), "按钮发起的导入应已进入挂接")

        def _release_later():
            time.sleep(0.4)
            FAKE_STATE["release"].set()

        threading.Thread(target=_release_later, daemon=True).start()
        result = model_tools.import_model_package(
            server.CFG, {"package_id": package_id})
        eq(result.get("op_id"), data["op_id"],
           "LLM 调用必须并入按钮已在跑的那条导入，不能另起流程")
        eq(FAKE_STATE["attach_calls"], 1, "LLM 不得再挂一次（重复挂接）")
        eq(result.get("joined_existing_op"), True)
    finally:
        FAKE_STATE["release"].set()
        FAKE_STATE["attach"] = "ok"
        shutil.rmtree(ws, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. 取消：排队 / 解压 / 索引 / 挂接
# ---------------------------------------------------------------------------

def test_cancel_while_queued_never_runs():
    """排队等锁时取消：必须立刻生效，且这条操作**从未执行**。"""
    ws = _new_workspace()
    _reset_attach_count()
    FAKE_STATE["attach"] = "slow"
    FAKE_STATE["entered"] = threading.Event()
    FAKE_STATE["release"] = threading.Event()
    try:
        # 两个包必须**内容不同**：package_id 是按内容 SHA-256 生成的，
        # 内容相同就是同一个包，B 会并入 A，压根排不到队（等于没测）。
        pkg_a = _upload(KIT_ENTRIES, filename="a.zip", workspace=ws)
        pkg_b = _upload({**KIT_ENTRIES, "Kit/extra.atf": "b"},
                        filename="b.zip", workspace=ws)
        ok(pkg_a != pkg_b, f"两个用例包应是不同的包（实际都是 {pkg_a}）")
        _s, d_a = _post("/models/import", {"package_id": pkg_a})
        ok(FAKE_STATE["entered"].wait(10), "A 应已进入挂接")
        _s, d_b = _post("/models/import", {"package_id": pkg_b})
        op_b = _wait_op(d_b["op_id"], timeout=10, want=("queued", "running"))
        eq(op_b.get("state"), "queued", f"B 应排在 A 后面: {op_b}")

        _s, res = _post("/models/cancel", {"op_id": d_b["op_id"]})
        eq(res.get("ok"), True, f"取消应被受理: {res}")
        eq(res.get("state"), "cancel_requested")
        op_b = _wait_op(d_b["op_id"], timeout=15)
        eq(op_b.get("state"), "cancelled", f"排队中的取消必须生效: {op_b}")
        eq(op_b.get("steps") or [], [], "被取消的排队操作绝不能留下已执行的步骤")
        eq(_package(ws, pkg_b).get("state"), "cancelled")
        eq(FAKE_STATE["attach_calls"], 1, "B 被取消后不应发生挂接")
    finally:
        FAKE_STATE["release"].set()
        FAKE_STATE["attach"] = "ok"
        shutil.rmtree(ws, ignore_errors=True)


def test_cancel_propagates_ads_job_id():
    """取消要传到 ADS 端：本地停手而 ADS 还在写 lib.defs 是最糟的结果。"""
    ws = _new_workspace()
    FAKE_STATE["attach"] = "slow"
    FAKE_STATE["entered"] = threading.Event()
    FAKE_STATE["release"] = threading.Event()
    FAKE_STATE["cancel_calls"] = []
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        ok(FAKE_STATE["entered"].wait(10), "应已进入挂接")
        op = _wait_op(data["op_id"], timeout=10, want=("running",))
        job_id = str(op.get("ads_job_id") or "")
        ok(job_id, f"挂接阶段必须把 ADS 作业 id 记进操作记录: {op}")
        _s, res = _post("/models/cancel", {"op_id": data["op_id"]})
        eq(res.get("ok"), True)
        deadline = time.time() + 5
        while time.time() < deadline and not FAKE_STATE["cancel_calls"]:
            time.sleep(0.05)
        ok(FAKE_STATE["cancel_calls"], "取消应传播到 ADS 端 /cancel")
        eq(FAKE_STATE["cancel_calls"][0], [job_id],
           "传播过去的必须是本操作的 ADS 作业 id")
    finally:
        FAKE_STATE["release"].set()
        FAKE_STATE["attach"] = "ok"
        shutil.rmtree(ws, ignore_errors=True)


def test_cancel_unknown_op_is_404():
    _s, data = _post("/models/cancel", {"op_id": "nope"})
    eq(_s, 404)
    eq(data.get("known"), False)


def test_cancel_finished_op_says_so():
    ws = _new_workspace()
    try:
        package_id = _upload(SP_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        _wait_op(data["op_id"])
        _s, res = _post("/models/cancel", {"op_id": data["op_id"]})
        eq(_s, 200, "已结束的操作取消不掉，但也不是错误")
        eq(res.get("known"), True)
        contains(res.get("message", ""), "已结束")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_cancel_during_extract():
    """解压阶段被取消：操作记为 cancelled，且不留半套解压产物。"""
    ws = _new_workspace()
    _reset_attach_count()
    original = model_store.extract_package
    entered = threading.Event()
    release = threading.Event()

    def _patched(root, package_id, limits=None, cancel_event=None):
        entered.set()
        release.wait(15)
        if cancel_event is not None and cancel_event.is_set():
            raise model_store.OperationCancelled("解压被取消")
        return original(root, package_id, limits=limits,
                        cancel_event=cancel_event)

    model_store.extract_package = _patched
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        ok(entered.wait(10), "应已进入解压")
        _s, res = _post("/models/cancel", {"op_id": data["op_id"]})
        eq(res.get("ok"), True)
        release.set()
        op = _wait_op(data["op_id"], timeout=20)
        eq(op.get("state"), "cancelled", f"解压中取消应记为 cancelled: {op}")
        eq(FAKE_STATE["attach_calls"], 0, "取消后不得继续挂接")
        eq(_package(ws, package_id).get("state"), "cancelled")
    finally:
        model_store.extract_package = original
        shutil.rmtree(ws, ignore_errors=True)


def test_extract_checks_cancel_between_files():
    """真实的取消检查点在**文件之间**（不是只在开头查一次）。"""
    ws = tempfile.mkdtemp(prefix="ws_cancel_")
    try:
        entries = {f"Kit/file{i:03d}.txt": "x" * 32 for i in range(60)}
        entries["Kit/lib.defs"] = "DEFINE VendorKit ./VendorKit\n"
        root = model_store.store_root(ws)
        package_id = model_store.save_archive(
            ws, "many_files.zip",
            source_path=_write_zip(_make_zip_bytes(entries)))["package_id"]
        model_store.scan_archive(root, package_id)

        cancel_event = threading.Event()
        calls = {"n": 0}
        original_one = model_store._extract_one

        def _patched_one(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                cancel_event.set()
            return original_one(*a, **kw)

        model_store._extract_one = _patched_one
        try:
            try:
                model_store.extract_package(root, package_id,
                                            cancel_event=cancel_event)
            except model_store.OperationCancelled:
                pass
            else:
                raise AssertionError("取消事件置位后解压应抛 OperationCancelled")
        finally:
            model_store._extract_one = original_one
        ok(calls["n"] < 60, f"取消后不应继续解完所有文件（已解 {calls['n']} 个）")
        extracted = os.path.join(root, "extracted", package_id)
        ok(not os.path.isdir(extracted),
           "取消后不得留下半套解压产物（索引会建出一半、库会挂到缺文件的目录）")
        eq(_package(ws, package_id).get("state"), "inspecting",
           "取消不应把资产标成 failed（用户是自己点的取消，不是包坏了）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_index_checks_cancel_between_blocks():
    ws = tempfile.mkdtemp(prefix="ws_index_")
    try:
        entries = {f"m{i:02d}.s2p": SP for i in range(20)}
        root = model_store.store_root(ws)
        package_id = model_store.save_archive(
            ws, "sp.zip",
            source_path=_write_zip(_make_zip_bytes(entries)))["package_id"]
        model_store.extract_package(root, package_id)

        cancel_event = threading.Event()
        calls = {"n": 0}
        original = model_store.parse_touchstone_header

        def _patched(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                cancel_event.set()
            return original(*a, **kw)

        model_store.parse_touchstone_header = _patched
        try:
            try:
                model_store.index_models(root, package_id,
                                         cancel_event=cancel_event)
            except model_store.OperationCancelled:
                pass
            else:
                raise AssertionError("取消事件置位后建索引应抛 OperationCancelled")
        finally:
            model_store.parse_touchstone_header = original
        ok(calls["n"] < 20, f"取消后不应把全部文件都读完（已读 {calls['n']} 个）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _write_zip(payload: bytes) -> str:
    path = os.path.join(_TMP, f"src_{threading.get_ident()}_{time.time_ns()}.zip")
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def test_workspace_switch_before_attach_stops():
    """挂接前核对工作区：用户中途切工程时立刻停，不往别的工作区挂库。"""
    ws = _new_workspace()
    other = tempfile.mkdtemp(prefix="ws_other_")
    original = model_store.extract_package
    entered = threading.Event()
    release = threading.Event()

    def _patched(root, package_id, limits=None, cancel_event=None):
        entered.set()
        release.wait(15)
        return original(root, package_id, limits=limits,
                        cancel_event=cancel_event)

    model_store.extract_package = _patched
    FAKE_STATE["attach_calls"] = 0
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        ok(entered.wait(10), "应已进入解压")
        FAKE_STATE["workspace"] = other        # 用户切了工程
        release.set()
        op = _wait_op(data["op_id"], timeout=20)
        eq(op.get("state"), "failed", f"工作区已切换时必须停止: {op}")
        eq(op.get("error_kind"), "workspace_mismatch")
        eq(FAKE_STATE["attach_calls"], 0,
           "工作区不一致时一次挂接都不许发生（会改到不属于本次导入的库定义）")
    finally:
        model_store.extract_package = original
        FAKE_STATE["attach_calls"] = 0
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. 重启恢复与有界历史
# ---------------------------------------------------------------------------

def test_restart_marks_ops_interrupted():
    """后端重启：没跑完的操作不能永久停在 running。"""
    ws = _new_workspace()
    FAKE_STATE["attach"] = "slow"
    FAKE_STATE["entered"] = threading.Event()
    FAKE_STATE["release"] = threading.Event()
    op_id = ""
    try:
        package_id = _upload(KIT_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        op_id = data["op_id"]
        ok(FAKE_STATE["entered"].wait(10), "应已进入挂接")
        op = _wait_op(op_id, timeout=10, want=("running",))
        eq(op.get("state"), "running")

        # 模拟后端进程在这里退出后重新启动
        recovered = model_orchestration.recover_interrupted_ops()
        ok(op_id in recovered, f"重启恢复应处理这条未完成的操作: {recovered}")
        _s, after = _get(f"/models/op?id={op_id}")
        eq(after.get("state"), "interrupted",
           "中断必须显式标出来 —— 既不是 succeeded，也不是 failed")
        contains(after.get("error", ""), "未能确认完成")
        pkg = _package(ws, package_id)
        ok(pkg.get("state") != "importing",
           f"资产状态也不能永远停在导入中（实际 {pkg.get('state')}）")
        # 重复执行一次不应报错（恢复是幂等的）
        model_orchestration.recover_interrupted_ops()
    finally:
        FAKE_STATE["release"].set()
        FAKE_STATE["attach"] = "ok"
        shutil.rmtree(ws, ignore_errors=True)


def test_history_is_bounded():
    """历史记录有上限：操作记录用来排障，不是无限审计日志。"""
    ws = _new_workspace()
    original_max = model_orchestration._MAX_OPS
    model_orchestration._MAX_OPS = 4
    try:
        for i in range(6):
            package_id = _upload(SP_ENTRIES, filename=f"sp{i}.zip", workspace=ws)
            _s, data = _post("/models/import", {"package_id": package_id})
            eq(_s, 202, f"第 {i} 次受理失败: {data}")
            op = _wait_op(data["op_id"], timeout=30)
            eq(op.get("state"), "succeeded", f"第 {i} 次导入失败: {op}")
        _s, listed = _get("/models/ops?limit=200")
        ok(listed.get("total", 0) <= 4,
           f"历史记录必须有界（上限 4，实际 {listed.get('total')}）")
    finally:
        model_orchestration._MAX_OPS = original_max
        shutil.rmtree(ws, ignore_errors=True)


def test_ops_query_returns_records():
    ws = _new_workspace()
    try:
        package_id = _upload(SP_ENTRIES, workspace=ws)
        _s, data = _post("/models/import", {"package_id": package_id})
        _wait_op(data["op_id"])
        _s, listed = _get("/models/ops?limit=10")
        eq(_s, 200)
        ok(listed.get("total", 0) >= 1, "应能列出历史操作")
        found = [o for o in listed["ops"] if o.get("op_id") == data["op_id"]]
        eq(len(found), 1)
        eq(found[0].get("package_id"), package_id)
        eq(found[0].get("workspace"), ws, "操作记录要固定住提交时的工作区")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = run(globals())
    finally:
        _teardown()
    raise SystemExit(code)
