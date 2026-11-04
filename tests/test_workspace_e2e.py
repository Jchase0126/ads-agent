"""工作区创建/发现/打开的**端到端**回归（离线，真链路，不连真实 ADS）。

链路（每一层都是真的，只有 ADS SDK 与传输端点被替换）：

    backend/tools.py  TOOLS schema / TIMEOUTS
        -> tools.call()            # 超时查表、[agent] workspace_root 注入、POST /execute
        -> 127.0.0.1 假执行器       # 真实 HTTP 往返，请求体照原样解出 name/args/timeout
        -> ads_ops.DISPATCH[name]  # 与 toolserver.py:980 同一张表、同一查表口径
        -> addon/ads_agent/ads_ops.py 里 create_workspace / list_workspaces / open_workspace
        -> fake de                 # patch ads_ops._de，离线记录调用参数

被替换的只有两处，都写在这里，不藏在别处：
  1. ``ads_ops._de`` —— fake，照 tests/test_open_workspace.py 的 _FakeWS/_FakeDe 写法扩了
     ``create_workspace`` 与 ``force`` 关键字（真机语义见契约 0 节）。
  2. ``tools._verify_toolserver`` 与 ``ads_auth.ensure_token`` —— 身份校验和令牌属于
     另一条被测链路（tests/test_tool_identity.py），这里固定掉，避免测试读真实数据根。

工作区**目录**一律落在本用例的临时目录里（``ADS_AGENT_DATA_DIR`` +
``ADS_AGENT_WORKSPACE_ROOT``），绝不影响用户数据根，也绝不扫描真实盘符。

运行::

    python tests/test_workspace_e2e.py
"""

import contextlib
import json
import os

import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

PROJECT_ROOT = add_path()
add_path("addon", "ads_agent")
add_path("backend")

# backend/config.py 在 import 时就定下 CONFIG_PATH，所以配置指向必须抢在它前面：
# 用本测试自己的临时 ini，绝不读/写用户真实的 ADSAgent 配置与令牌。
_TMP_CONFIG_DIR = tempfile.mkdtemp(prefix="ads_ws_e2e_cfg_")
_TMP_CONFIG = os.path.join(_TMP_CONFIG_DIR, "config.ini")
with open(_TMP_CONFIG, "w", encoding="utf-8") as _f:
    _f.write("[llm]\nmodel = m\n\n[ads]\nhost = 127.0.0.1\nport = 1\n"
             "token = e2e-test-token\n\n[agent]\nsim_timeout = 900\n")
os.environ["ADS_AGENT_CONFIG"] = _TMP_CONFIG

import ads_ops  # noqa: E402
import tools  # noqa: E402


# ---------------------------------------------------------------------------
# fake de —— 只模拟契约 0 节列出的那几个 SDK 成员
# ---------------------------------------------------------------------------
class _FakeWS:
    def __init__(self, path, libraries=None):
        self.path = path
        names = list(libraries or ["Demo_lib"])
        self.library_names = names
        self.writable_library_names = list(names)
        self.libraries = []


class _FakeDe:
    """记录每一次 open/create 的**入参**，供断言"到底以什么参数调了 SDK"。"""

    def __init__(self, libraries=None):
        self.is_open = False
        self.ws_obj = None
        self.open_calls = []    # [(path, force)]
        self.create_calls = []  # [path]
        self._libraries = libraries

    # --- 状态查询 -------------------------------------------------------
    def workspace_is_open(self):
        return self.is_open

    def active_workspace(self):
        return self.ws_obj

    def directory_is_workspace(self, path):
        return os.path.isfile(os.path.join(path, "lib.defs"))

    def directory_might_be_workspace(self, path):
        return os.path.isdir(path)

    # --- 写操作 ---------------------------------------------------------
    def create_workspace(self, path, **_kw):
        """真机：路径已存在会抛 RuntimeError，且**创建后不会自动打开**。"""
        if os.path.exists(path):
            raise RuntimeError(f"{path} already exists")
        os.makedirs(path)
        with open(os.path.join(path, "lib.defs"), "w", encoding="utf-8") as f:
            f.write("VERSION 1.0\n")
        self.create_calls.append(path)
        return _FakeWS(path, self._libraries)

    def open_workspace(self, path, *args, **kwargs):
        """真机签名是 ``open_workspace(wrk_path, *, force=False)``。

        这里把**实际是怎么传的**一并记下来（位置/关键字、原始值），
        这样断言的是"force 真的透传到了 SDK"，而不是"我以为它传了"。
        """
        force = kwargs.get("force", args[0] if args else False)
        self.open_calls.append({
            "path": path,
            "force": bool(force),
            "force_as_kwarg": "force" in kwargs,
            "args": args,
            "kwargs": dict(kwargs),
        })
        self.ws_obj = _FakeWS(path, self._libraries)
        self.is_open = True
        return self.ws_obj

    def close_workspace(self):
        self.is_open = False
        self.ws_obj = None


# ---------------------------------------------------------------------------
# 假执行器 —— 真实 HTTP 端点，内部按 ads_ops.DISPATCH 查表派发
# ---------------------------------------------------------------------------
def _dispatch(name, args):
    """与 addon/ads_agent/toolserver.py:980 同一口径：未知工具 -> error，已知 -> handler(args)。"""
    handler = ads_ops.DISPATCH.get(name)
    if handler is None:
        return {"error": f"未知工具: {name}"}
    try:
        return handler(args or {}, None)
    except Exception as e:  # noqa: BLE001 — 工具异常按契约转成 error 字段回给调用方
        return {"error": f"{type(e).__name__}: {e}"}


class _Executor(BaseHTTPRequestHandler):
    received = []  # [{name, args, timeout}]，跨用例清空

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler 约定的方法名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        req = json.loads(raw or "{}")
        name = req.get("name") or ""
        args = req.get("args") or {}
        self.received.append({"name": name, "args": args,
                              "timeout": req.get("timeout"),
                              "path": self.path})
        result = _dispatch(name, args)
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):  # 测试输出不要被 HTTP 访问日志淹掉
        return


_SERVER = None
_SERVER_LOCK = threading.Lock()


def _server():
    """一个进程内复用的假执行器（随机端口，只监听 127.0.0.1）。"""
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is None:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Executor)
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, name="ws-e2e-executor",
                             daemon=True).start()
            _SERVER = srv
        return _SERVER


# ---------------------------------------------------------------------------
# 每个用例独立的临时数据根 + 工作区根
# ---------------------------------------------------------------------------
class _Env:
    _KEYS = ("ADS_AGENT_DATA_DIR", "ADS_AGENT_WORKSPACE_ROOT")

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="ads_ws_e2e_")
        self.data = os.path.join(self.tmp, "data")
        self.root = os.path.join(self.tmp, "workspaces")
        os.makedirs(self.root, exist_ok=True)
        self._old = {k: os.environ.get(k) for k in self._KEYS}
        # 数据根 = 最近记录/令牌等的落点；工作区根 = 发现与新建的爸爸目录
        os.environ["ADS_AGENT_DATA_DIR"] = self.data
        os.environ["ADS_AGENT_WORKSPACE_ROOT"] = self.root

    def mkws(self, name, libraries=None):
        """造一个"已存在、含 lib.defs"的工作区目录。"""
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "lib.defs"), "w", encoding="utf-8") as f:
            f.write("VERSION 1.0\n")
        return path

    def last_record(self):
        fp = os.path.join(self.data, "last_workspace.txt")
        if not os.path.exists(fp):
            return None
        with open(fp, encoding="utf-8") as f:
            return f.read().strip()

    def close(self):
        import shutil

        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


@contextlib.contextmanager
def _chain(env, fake, extra_cfg=None):
    """装好 fake de + 固定身份校验/令牌，产出指向假执行器的真实 cfg。"""
    srv = _server()
    _Executor.received.clear()
    cfg = {
        "ads_host": "127.0.0.1",
        "ads_port": srv.server_address[1],
        # [agent] workspace_root 在后端是扁平 key（见 backend/config.py DEFAULTS 的写法）
        "workspace_root": env.root,
        "sim_timeout": 900,
    }
    cfg.update(extra_cfg or {})
    with patch.object(ads_ops, "_de", return_value=fake), \
            patch.object(tools, "_verify_toolserver", lambda cfg: None), \
            patch.object(tools.ads_auth, "ensure_token", return_value="e2e-test-token"):
        yield cfg


def _norm(p):
    return os.path.normcase(os.path.normpath(str(p or "")))


def _tool_names():
    return {t["function"]["name"]: t["function"] for t in tools.TOOLS}


def _panel_phases():
    """从 panel.py 的语法树里取出工具短语 dict（不 import Qt，不引入新依赖）。

    比字符串 grep 强：键值是从 AST 里结构化解出来的，改空格/换行/引号风格都不会误判。
    取"含 run_python 键的那个 Dict 字面量"—— 全模块只有这一处。
    """
    import ast

    panel = os.path.join(PROJECT_ROOT, "addon", "ads_agent", "panel.py")
    with open(panel, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if "run_python" in keys:
            return {k.value: v.value for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}, src
    raise AssertionError("panel.py 里找不到工具短语 dict（含 run_python 键的那个字面量）")


# ---------------------------------------------------------------------------
# 断言 a：两层声明都在，且参数名与契约 1.1 / 1.2 / 1.3 一致
# ---------------------------------------------------------------------------
def test_schemas_timings_and_dispatch_are_wired():
    schemas = _tool_names()
    for name in ("create_workspace", "list_workspaces"):
        ok(name in schemas, f"{name} 必须出现在 backend/tools.py 的 TOOLS 里（契约 2 节）")
        ok(name in tools.TIMEOUTS, f"{name} 必须有后端超时（契约 2 节）")
        ok(callable(ads_ops.DISPATCH.get(name)),
           f"{name} 必须注册进 ads_ops.DISPATCH（toolserver 只认这张表，toolserver.py:980）")

    eq(tools.TIMEOUTS.get("create_workspace"), 180, "create_workspace 后端超时 180s")
    eq(tools.TIMEOUTS.get("list_workspaces"), 120, "list_workspaces 后端超时 120s")
    eq(tools.TIMEOUTS.get("open_workspace"), 120, "open_workspace 保持 120s，不许被顺手改掉")

    props = schemas["create_workspace"]["parameters"]["properties"]
    for key in ("name", "root", "path", "library", "open", "force"):
        ok(key in props, f"create_workspace schema 缺参数 {key}（契约 1.2）")
    props = schemas["list_workspaces"]["parameters"]["properties"]
    for key in ("query", "max", "roots"):
        ok(key in props, f"list_workspaces schema 缺参数 {key}（契约 1.1）")
    props = schemas["open_workspace"]["parameters"]["properties"]
    ok("force" in props, "open_workspace schema 缺 force（契约 1.3）")

    # 描述里必须写清"发现优先 list_workspaces"和"切换会丢弃未保存修改"（契约 2 节）。
    # 口径放宽：契约要求"说清"，不要求字面出现工具名 —— 自指"本工具"同样说清，
    # 且对模型更不容易产生歧义（实际实现用的是自指写法）。
    list_desc = schemas["list_workspaces"]["description"]
    ok(("list_workspaces" in list_desc) or ("本工具" in list_desc),
       "list_workspaces 描述必须自指如何发现（点名工具名或'本工具'）")
    contains(list_desc, "优先", "list_workspaces 描述必须写清'优先调用'（契约 2 节）")
    ok("扫描" in list_desc or "os.walk" in list_desc or "全盘" in list_desc,
       "list_workspaces 描述必须点明禁止全盘扫描")
    ok(("候选" in list_desc) or ("candidates" in list_desc),
       "list_workspaces 描述必须交代多命中时列候选给用户（契约 5.3 决策语义）")
    open_desc = schemas["open_workspace"]["description"]
    ok("force" in open_desc, "open_workspace 描述必须交代 force 语义")
    ok(("丢弃" in open_desc) or ("未保存" in open_desc),
       "open_workspace 描述必须告知切换会丢弃未保存修改")

    # panel.py 是 Qt 模块（离线逻辑层不 import 它），所以从源码语法树里取短语表核对。
    phases, src = _panel_phases()
    eq(phases.get("create_workspace"), "正在新建工作区",
       "panel.py 工具短语表缺 create_workspace -> 正在新建工作区（契约 3 节）")
    eq(phases.get("list_workspaces"), "正在查找工作区",
       "panel.py 工具短语表缺 list_workspaces -> 正在查找工作区（契约 3 节）")
    for keep in ("run_python", "get_workspace_info", "run_simulation"):
        ok(keep in phases, f"既有短语 {keep} 不得被顺手删掉（只做增量）")
    ok(len(phases) >= 12, f"短语表项数不该减少，实际 {len(phases)}")
    contains(src, "phases.get(self._tool_name, '正在执行工具')",
             "既有兜底短语不得被改掉（契约 3 节：未命中仍是 '正在执行工具'）")


# ---------------------------------------------------------------------------
# 断言 b：新建全链路（含 workspace_root 注入、真正调 open_workspace）
# ---------------------------------------------------------------------------
def test_create_workspace_end_to_end_creates_then_opens():
    env = _Env()
    fake = _FakeDe(libraries=["Demo_lib"])
    try:
        with _chain(env, fake) as cfg:
            # 模型没给 root/path -> 后端必须把 [agent] workspace_root 注入成 root
            result = tools.call(cfg, "create_workspace", {"name": "Demo"})

        ok(isinstance(result, dict), f"tools.call 应返回 dict，实际 {type(result).__name__}")
        ok("error" not in result, f"create_workspace 不应报错: {result.get('error')}")
        eq(result.get("created"), True, "create_workspace 必须返回 created=true（契约 1.2）")
        target = os.path.join(env.root, "Demo")
        eq(_norm(result.get("path")), _norm(target), "返回的 path 应是 <workspace_root>/Demo")
        ok(os.path.isdir(target), "目录必须真的建出来")
        ok(os.path.isfile(os.path.join(target, "lib.defs")), "新工作区必须含 lib.defs")

        # 传输层收到的参数：root 注入的证据
        eq(len(_Executor.received), 1, "一次调用只应发一个 /execute 请求")
        sent = _Executor.received[0]
        eq(sent["name"], "create_workspace", "请求体里的工具名")
        eq(sent["path"], "/execute", "POST 到 /execute")
        eq(_norm(sent["args"].get("root")), _norm(env.root),
           "cfg['workspace_root'] 必须被注入为默认 root（契约 2 节）")
        eq(sent["timeout"], 180 + tools.ADS_TIMEOUT_MARGIN,
           "下发给 ADS 端的超时应等于 TIMEOUTS + ADS_TIMEOUT_MARGIN")

        # fake de 收到 create，且**收到 open_workspace**（真机不会自动打开）
        eq([_norm(p) for p in fake.create_calls], [_norm(target)],
           "de.create_workspace 应以目标路径调用一次")
        eq(len(fake.open_calls), 1, "创建后必须调 de.open_workspace（默认 open=true）")
        eq(_norm(fake.open_calls[0]["path"]), _norm(target), "打开的就是刚建的那个工作区")
        eq(fake.open_calls[0]["force"], False, "创建链路不该传 force=True")
        eq(result.get("workspace_open"), True, "打开成功时 workspace_open=true")
        eq(_norm(env.last_record()), _norm(fake.ws_obj.path),
           "最近记录 last_workspace.txt 必须同步更新（契约 1.4）")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 断言：已存在目录 -> exists，且目录内容零改动、不调 open
# ---------------------------------------------------------------------------
def test_create_workspace_existing_dir_is_refused_untouched():
    env = _Env()
    fake = _FakeDe()
    try:
        existing = env.mkws("Demo")
        marker = os.path.join(existing, "keep_me.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("do not touch")

        with _chain(env, fake) as cfg:
            result = tools.call(cfg, "create_workspace", {"name": "Demo"})

        eq(result.get("created"), False, "目录已存在时 created=false")
        eq(result.get("reason"), "exists", "拒绝理由必须是 exists（契约 1.2 纪律）")
        ok(not fake.create_calls, "已存在时不该调 de.create_workspace")
        ok(not fake.open_calls, "被拒绝时不该打开任何工作区")
        with open(marker, encoding="utf-8") as f:
            eq(f.read(), "do not touch", "既有目录内容必须零改动")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 拒绝路径不得有任何副作用（created:false 就必须是"什么都没发生"）
# ---------------------------------------------------------------------------
def test_create_rejections_have_zero_side_effects():
    env = _Env()
    fake = _FakeDe()
    try:
        with _chain(env, fake) as cfg:
            # 1) 非法名称：无 SDK 调用、不建目录
            bad = tools.call(cfg, "create_workspace", {"name": 'bad:name*'})
            eq(bad.get("created"), False, "非法名称时 created=false")
            eq(bad.get("reason"), "invalid_name",
               "含 \\/:*?\"<>| 的名称必须返回 invalid_name（契约 1.2 纪律）")
            eq(bad.get("message") is not None, True, "拒绝时要有给用户看的中文说明")

            # 2) 已有其它工作区且未给 force：needs_force，同样零副作用
            other = env.mkws("Amp_Other")
            fake.is_open = True
            fake.ws_obj = _FakeWS(other)
            target = os.path.join(env.root, "Amp_New")
            gate = tools.call(cfg, "create_workspace", {"name": "Amp_New"})
            eq(gate.get("created"), False, "另一工作区已打开时缺省不许新建")
            eq(gate.get("reason"), "needs_force",
               "新建也要过 force 闸门（契约 1.2 force 语义）")
            eq(_norm(gate.get("current_workspace")), _norm(other),
               "needs_force 必须带当前工作区路径")
            ok(not os.path.exists(target),
               f"created:false 却把目录建出来了，属谎报：{target}")

        eq(fake.create_calls, [], "被拒绝的路径一次 de.create_workspace 都不许调")
        eq(fake.open_calls, [], "被拒绝的路径一次 de.open_workspace 都不许调")
        eq(_norm(fake.ws_obj.path), _norm(other), "被拒绝后当前工作区不得被换掉")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 断言 c：名称片段多命中 -> ambiguous，且一个都没打开（不许替用户选）
# ---------------------------------------------------------------------------
def test_open_workspace_fragment_ambiguous_never_picks_one():
    env = _Env()
    fake = _FakeDe()
    try:
        a = env.mkws("Amp_Match_A")
        b = env.mkws("Amp_Match_B")

        with _chain(env, fake) as cfg:
            result = tools.call(cfg, "open_workspace", {"path": "Amp_Match"})

        ok("error" not in result, f"歧义不是错误，必须结构化返回: {result.get('error')}")
        eq(result.get("opened"), False, "歧义时 opened=false")
        eq(result.get("reason"), "ambiguous", "多命中必须返回 reason=ambiguous（契约 1.3）")
        cands = result.get("candidates") or []
        ok(len(cands) >= 2, f"候选至少两个，实际 {len(cands)}")
        got = {_norm(c.get("path")) for c in cands}
        ok(_norm(a) in got and _norm(b) in got,
           f"候选里必须同时出现 {a} 与 {b}，实际 {sorted(got)}")
        eq(fake.open_calls, [], "歧义时 de.open_workspace 调用次数必须为 0")
        eq(fake.is_open, False, "歧义时不得打开任何工作区")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 断言 d：切换闸门 —— 缺省 needs_force，force=true 才真的调 open(force=True)
# ---------------------------------------------------------------------------
def test_open_workspace_switch_needs_force_then_forces():
    env = _Env()
    fake = _FakeDe()
    try:
        other = env.mkws("Amp_Other")
        target = env.mkws("Amp_Target")
        fake.is_open = True
        fake.ws_obj = _FakeWS(other)

        with _chain(env, fake) as cfg:
            gate = tools.call(cfg, "open_workspace", {"path": target})
            eq(gate.get("opened"), False, "另一工作区已打开时缺省不许切")
            eq(gate.get("reason"), "needs_force",
               "缺省必须返回 reason=needs_force（契约 1.3）")
            eq(_norm(gate.get("current_workspace")), _norm(other),
               "needs_force 必须带当前工作区路径，用户才知道会丢什么")
            eq(fake.open_calls, [], "闸门拦下时一次 open 都不许调")

            forced = tools.call(cfg, "open_workspace", {"path": target, "force": True})

        eq(forced.get("opened"), True, "force=true 才允许切换")
        eq(_norm(forced.get("path")), _norm(target), "切换后的路径")
        eq(len(fake.open_calls), 1, "force=true 应恰好调用一次 de.open_workspace")
        eq(_norm(fake.open_calls[0]["path"]), _norm(target), "调 open 的路径")
        eq(fake.open_calls[0]["force"], True, "force 必须真的透传到 SDK（契约 1.3 第 4 条）")
        eq(fake.open_calls[0]["force_as_kwarg"], True,
           "真机签名是 keyword-only（open_workspace(p, *, force=False)），"
           f"force 必须以关键字传入，实际 kwargs={fake.open_calls[0]['kwargs']}")
        eq(forced.get("switched"), True, "从别的切过来 switched=true")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 发现链路：list_workspaces 有界扫描、过滤、不碰盘符根
# ---------------------------------------------------------------------------
def test_list_workspaces_end_to_end_is_bounded_and_filtered():
    env = _Env()
    fake = _FakeDe()
    try:
        env.mkws("Amp_Match_A")
        env.mkws("Amp_Match_B")
        env.mkws("Filter_Later")

        with _chain(env, fake) as cfg:
            all_hits = tools.call(cfg, "list_workspaces", {})
            by_query = tools.call(cfg, "list_workspaces", {"query": "amp_match"})
            none = tools.call(cfg, "list_workspaces", {"query": " Absolutely_No_Such_WS "})

        found = {_norm(w.get("path")) for w in (all_hits.get("workspaces") or [])}
        eq(len(found), 3, f"三个工作区都应被发现，实际 {sorted(found)}")
        for w in all_hits.get("workspaces") or []:
            ok(w.get("name"), f"每条都必须有 name: {w}")
            eq(w.get("valid"), True, f"含 lib.defs 的工作区 valid=true: {w}")
            ok(w.get("source") in ("recent", "scan"), f"source 只能是 recent|scan: {w}")
        eq(all_hits.get("truncated"), False, "三个目录不该触发上限")

        roots = all_hits.get("roots") or []
        ok(roots, "必须回传实际扫描的根目录（便于排查误扫）")
        for r in roots:
            ok(_norm(r) != _norm(os.path.abspath(os.sep)),
               f"禁止把盘符根当扫描根（契约 1.1 扫描纪律）: {r}")
        ok(isinstance(all_hits.get("scanned"), int), "scanned 必须是整数")

        got_q = {_norm(w.get("path")) for w in (by_query.get("workspaces") or [])}
        eq(got_q,
           {_norm(os.path.join(env.root, "Amp_Match_A")),
            _norm(os.path.join(env.root, "Amp_Match_B"))},
           "query 大小写不敏感，只应命中两个 Amp_Match_*")
        eq(none.get("workspaces"), [], "零命中返回空列表，不抛异常")
    finally:
        env.close()


# ---------------------------------------------------------------------------
# 自检：相邻用例的隔离承诺真的成立（不然前面所有 PASS 都可能是串读出来的）
# ---------------------------------------------------------------------------
def test_isolation_between_cases():
    """两个 _Env 嵌套时，数据根/工作区根与最近记录必须互不污染。

    ``_Env.close()`` 的实现语义是"恢复进入时快照"，**只有 LIFO 嵌套成立**：
    先建 outer、再建 inner，然后先 close inner 再 close outer。本文件所有用例都是
    try/finally 嵌套 = LIFO，所以这里只断言 LIFO。反序关闭（先关 outer 再关 inner）
    会把环境恢复到已被删除的 inner 路径上 —— 那是 _Env 不支持也不承诺的用法，
    故意不作为断言，免得把"已知限制"测成"通过"。
    """
    outer = _Env()
    try:
        eq(os.environ["ADS_AGENT_DATA_DIR"], outer.data, "外层 env 生效")
        eq(os.environ["ADS_AGENT_WORKSPACE_ROOT"], outer.root, "外层工作区根生效")
        outer_target = os.path.join(outer.root, "Outer_WS")
        fake_o = _FakeDe()
        with _chain(outer, fake_o) as cfg_o:
            r = tools.call(cfg_o, "create_workspace", {"name": "Outer_WS"})
        eq(r.get("created"), True, f"外层新建应成功: {r}")
        outer_record = outer.last_record()
        ok(outer_record, "外层必须写下 last_workspace.txt")
        eq(_norm(outer_record), _norm(outer_target), "外层最近记录指向自己的工作区")

        inner = _Env()
        try:
            eq(os.environ["ADS_AGENT_DATA_DIR"], inner.data, "内层 env 覆盖外层")
            eq(os.environ["ADS_AGENT_WORKSPACE_ROOT"], inner.root, "内层工作区根覆盖外层")
            ok(_norm(inner.data) != _norm(outer.data), "两个 env 的数据根必须不同")

            fake_i = _FakeDe()
            with _chain(inner, fake_i) as cfg_i:
                r2 = tools.call(cfg_i, "create_workspace", {"name": "Inner_WS"})
            eq(r2.get("created"), True, f"内层新建应成功: {r2}")

            # 内层的写入只落在内层
            eq(_norm(inner.last_record()),
               _norm(os.path.join(inner.root, "Inner_WS")),
               "内层最近记录指向内层工作区")
            eq(outer.last_record(), outer_record,
               "内层写入不得污染外层 last_workspace.txt")
            ok(os.path.isdir(os.path.join(inner.root, "Inner_WS")),
               "内层工作区落在内层根")
            ok(not os.path.exists(os.path.join(outer.root, "Inner_WS")),
               "内层工作区不得落到外层根")
            ok(not os.path.exists(os.path.join(inner.root, "Outer_WS")),
               "外层工作区不得落到内层根")
        finally:
            inner.close()

        # LIFO 关闭后，外层环境与数据必须原样还在
        eq(os.environ["ADS_AGENT_DATA_DIR"], outer.data,
           "内层 close 后外层 env 仍生效（LIFO 语义）")
        eq(os.environ["ADS_AGENT_WORKSPACE_ROOT"], outer.root,
           "内层 close 后外层工作区根仍生效")
        eq(outer.last_record(), outer_record, "内层 close 后外层记录仍在")
        fake_o2 = _FakeDe()
        with _chain(outer, fake_o2) as cfg_o2:
            # 不给 path：走 recent 记录。若隔离破了，这里会读到内层的工作区
            r3 = tools.call(cfg_o2, "open_workspace", {})
        eq(_norm(r3.get("path")), _norm(outer_target),
           f"外层最近记录仍指向自己的工作区，实际 {r3.get('path')}")
    finally:
        outer.close()


if __name__ == "__main__":
    raise SystemExit(run(globals(), "工作区端到端"))
