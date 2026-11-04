"""工作区工具的后端接线：TOOLS schema / TIMEOUTS / [agent] workspace_root 注入。

对应 docs/工作区对话式创建与打开_接口契约_2026-11-04.md 第 2 节。
不连 ADS：call() 的执行层（对端身份校验、令牌、回环 HTTP）全部换成替身，
只观察真正要发给 ADS 端 /execute 的请求体。

运行::

    python tests/test_workspace_tools.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

BACKEND = add_path("backend")

# 必须在 import config / tools 之前把配置指到临时文件：config.py 在 import 时
# 就会算 CONFIG_PATH，指向真实 config.ini 会读写用户配置。
_TMP = tempfile.mkdtemp(prefix="ads_agent_ws_tools_")
_CONFIG = os.path.join(_TMP, "config.ini")
_CONFIG_TEXT = (
    "[llm]\nmodel = test-model\napi_key = test-key\n\n"
    "[ads]\nhost = 127.0.0.1\nport = 1\n"
    "token = test-token-not-the-public-default\n\n"
    "[agent]\nworkspace_root =\n"
)
with open(_CONFIG, "w", encoding="utf-8") as _f:
    _f.write(_CONFIG_TEXT)
os.environ["ADS_AGENT_CONFIG"] = _CONFIG

from unittest.mock import patch  # noqa: E402

import config as config_mod  # noqa: E402
import tools  # noqa: E402


def _function(name: str) -> dict:
    for item in tools.TOOLS:
        if item["function"]["name"] == name:
            return item["function"]
    raise AssertionError(f"TOOLS 里没有 {name}")


def _props(name: str) -> dict:
    return _function(name)["parameters"]["properties"]


# ---------------------------------------------------------------------------
# schema / TIMEOUTS / 派发路径
# ---------------------------------------------------------------------------

def test_list_workspaces_schema():
    fn = _function("list_workspaces")
    props = _props("list_workspaces")
    eq(set(props), {"query", "max", "roots"}, "list_workspaces 参数名必须逐字一致")
    eq(props["query"]["type"], "string", "query 必须是 string")
    eq(props["max"]["type"], "integer", "max 必须是 integer")
    eq(props["roots"]["type"], "array", "roots 必须是数组")
    eq(props["roots"]["items"]["type"], "string", "roots 元素必须是 string")
    ok(not fn.get("required"), "list_workspaces 不应有必填参数")
    desc = fn["description"]
    contains(desc, "优先调用本工具", "描述必须说明发现工作区优先用它")
    contains(desc, "禁止全盘扫描", "描述必须禁止全盘扫描")
    contains(desc, "列给用户选择", "多命中时交给用户选择")


def test_create_workspace_schema():
    fn = _function("create_workspace")
    props = _props("create_workspace")
    eq(set(props), {"name", "root", "path", "library", "open", "force"},
       "create_workspace 参数名必须逐字一致")
    for key in ("name", "root", "path", "library"):
        eq(props[key]["type"], "string", f"{key} 必须是 string")
    eq(props["open"]["type"], "boolean", "open 必须是 boolean")
    eq(props["force"]["type"], "boolean", "force 必须是 boolean")
    ok(not fn.get("required"),
       "create_workspace 无必填项（name/path 至少一个由工具侧校验）")
    desc = fn["description"]
    contains(desc, "至少给一个", "描述必须写明 name 与 path 至少给一个")
    contains(desc, "绝不覆盖", "描述必须写明目标已存在时绝不覆盖")
    contains(desc, "force=true", "描述必须写明已有工作区时要用户确认后传 force=true")
    contains(desc, "丢弃", "描述必须写明切换会丢弃未保存修改")


def test_open_workspace_force_schema():
    fn = _function("open_workspace")
    props = _props("open_workspace")
    eq(set(props), {"path", "force"}, "open_workspace 参数集合（path + 新增 force）")
    eq(props["path"]["type"], "string", "path 必须是 string")
    eq(props["force"]["type"], "boolean", "force 必须是 boolean")
    ok(not fn.get("required"), "open_workspace 的 path 保持可选，不新增必填")
    desc = fn["description"]
    contains(desc, "force=true", "描述必须写明用户确认后才传 force=true")
    contains(desc, "未保存修改", "描述必须写明切换会丢弃当前未保存修改")
    contains(desc, "ambiguous", "描述必须写明名称片段多命中返回 ambiguous")
    contains(desc, "禁止自行挑一个", "描述必须禁止模型自行挑一个候选")


def test_timeouts():
    eq(tools.TIMEOUTS.get("list_workspaces"), 120, "list_workspaces 超时必须是 120s")
    eq(tools.TIMEOUTS.get("create_workspace"), 180, "create_workspace 超时必须是 180s")
    eq(tools.TIMEOUTS.get("open_workspace"), 120, "open_workspace 超时保持 120s")
    eq(tools.timeout_for({}, "list_workspaces"), 120, "timeout_for 应返回 120")
    eq(tools.timeout_for({}, "create_workspace"), 180, "timeout_for 应返回 180")


def test_same_dispatch_path_as_get_workspace_info():
    """契约要求：新工具与 get_workspace_info 走同一条派发路径（发往 ADS 端）。

    tools.call 的两道闸门决定了这一点：LOCAL_TOOLS 里的名字会被就地拒绝；
    不在 TIMEOUTS 里的名字会被判成"未知工具"。因此新工具必须**不在**
    LOCAL_TOOLS、**在** TIMEOUTS 里。
    """
    for name in ("list_workspaces", "create_workspace"):
        ok(not tools.is_local(name), f"{name} 必须派发到 ADS 端（不能进 LOCAL_TOOLS）")
        ok(name in tools.TIMEOUTS, f"{name} 不在 TIMEOUTS 会被 call() 判成未知工具")
    ok(not tools.is_local("get_workspace_info"), "get_workspace_info 走 ADS 派发路径")
    ok(not tools.is_local("open_workspace"), "open_workspace 走 ADS 派发路径")


# ---------------------------------------------------------------------------
# [agent] workspace_root
# ---------------------------------------------------------------------------

def test_config_default_has_workspace_root():
    ok("workspace_root" in config_mod.DEFAULTS, "DEFAULTS 缺少 workspace_root")
    eq(config_mod.DEFAULTS["workspace_root"], "", "workspace_root 默认必须是空串")
    cfg = config_mod.load()
    ok("workspace_root" in cfg, "load() 的结果里缺少 workspace_root")
    eq(cfg["workspace_root"], "", "config.ini 里留空时应回落到空串默认值")


def test_config_loads_workspace_root_from_ini():
    text = _CONFIG_TEXT.replace("workspace_root =\n",
                                "workspace_root = D:/ADS_Workspaces\n")
    try:
        with open(_CONFIG, "w", encoding="utf-8") as f:
            f.write(text)
        eq(config_mod.load()["workspace_root"], "D:/ADS_Workspaces",
           "load() 应读取 [agent] workspace_root")
    finally:
        with open(_CONFIG, "w", encoding="utf-8") as f:
            f.write(_CONFIG_TEXT)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """替身回环 opener：记录每次 /execute 的请求体，不碰网络。"""

    def __init__(self):
        self.bodies: list = []

    def open(self, req, timeout=None):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse({"ok": True})


_BASE_CFG = {"ads_host": "127.0.0.1", "ads_port": 8761, "workspace_root": ""}


def _dispatch(cfg: dict, name: str, args: dict) -> tuple:
    """走真实的 tools.call，只把真正执行 ADS 的那一层换成替身。"""
    opener = _FakeOpener()
    with patch.object(tools, "_verify_toolserver", lambda _cfg: None), \
            patch.object(tools, "_LOOPBACK", opener), \
            patch.object(tools.ads_auth, "ensure_token", lambda: "ws-tools-test-token"):
        result = tools.call(cfg, name, args, job_id="ws-tools-test-job")
    eq(len(opener.bodies), 1, "一次 call() 应只发一个 /execute 请求")
    body = opener.bodies[0]
    eq(body.get("name"), name, "/execute 请求体里的工具名")
    ok(isinstance(body.get("args"), dict), "/execute 请求体里必须有 args 字典")
    return body, result


def test_inject_default_root_when_missing():
    cfg = dict(_BASE_CFG, workspace_root="D:/默认工作区根")
    args = {"name": "Demo"}
    body, result = _dispatch(cfg, "create_workspace", args)
    eq(body["args"].get("root"), "D:/默认工作区根",
       "create_workspace 缺 root/path 时必须注入 workspace_root")
    eq(args, {"name": "Demo"}, "注入不应就地改写调用方传入的 args")
    eq(result, {"ok": True}, "替身执行层的结果应原样返回")


def test_no_inject_when_root_given():
    cfg = dict(_BASE_CFG, workspace_root="D:/默认工作区根")
    body, _ = _dispatch(cfg, "create_workspace",
                        {"name": "Demo", "root": "E:/用户指定根"})
    eq(body["args"], {"name": "Demo", "root": "E:/用户指定根"},
       "模型给了 root 时不得覆盖、不得追加其它参数")


def test_no_inject_when_path_given():
    cfg = dict(_BASE_CFG, workspace_root="D:/默认工作区根")
    body, _ = _dispatch(cfg, "create_workspace", {"path": "E:/target/Demo"})
    eq(body["args"], {"path": "E:/target/Demo"},
       "给了完整 path 时不得再注入 root")


def test_no_inject_when_config_empty():
    body, _ = _dispatch(dict(_BASE_CFG), "create_workspace", {"name": "Demo"})
    eq(body["args"], {"name": "Demo"},
       "workspace_root 为空时不得给 create_workspace 注入 root")


def test_other_tools_never_receive_root():
    cfg = dict(_BASE_CFG, workspace_root="D:/默认工作区根")
    body, _ = _dispatch(cfg, "open_workspace", {})
    eq(body["args"], {}, "open_workspace 的参数不得被 workspace_root 污染")
    body, _ = _dispatch(cfg, "list_workspaces", {"query": "demo"})
    eq(body["args"], {"query": "demo"},
       "list_workspaces 的参数不得被 workspace_root 污染")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "工作区工具后端接线"))
