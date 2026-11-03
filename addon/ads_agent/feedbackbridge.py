"""在 ADS 进程内加载共享诊断模块 backend/feedback.py，并做面板侧的最小封装。

与 ``pathbridge`` / ``authbridge`` 同一个套路：按**文件路径**加载，而不是
``import feedback`` —— ADS 插件的 ``sys.path`` 里是 addon 目录，``backend/``
不在也不应该在里面（那会把 config / paths / tools 这类通用名带进 ADS 进程，
可能遮蔽 ADS 自己的模块）。

比前两个桥多一步：``backend/feedback.py`` 自己会 ``import adslog`` / ``import paths``
（``backend/adslog.py`` 还会在导入时求值 ``LOG_DIR = paths.logs_dir()``）。这些顶层名
在 ADS 进程里根本不存在，所以这里用 :func:`_scoped` 在加载的**那一瞬间**临时把依赖
登记进 ``sys.modules``，加载完立刻还原 —— 既能让模块正常初始化，又不会把
``adslog`` / ``paths`` 永久塞进 ADS 的解释器。

面板侧对后端 HTTP 的两件事（生成报告、取待处理列表）也放在这里，而不是放进
``panel.py``：``panel`` 里的 ``FeedbackWorker`` 只是把这个函数搬到线程里跑，
网络细节（回环地址、令牌头、超时、异常收敛）只在这一处。

约定：本模块**只读**配置、只写 ``[feedback]`` 段；所有入口都不抛异常 ——
诊断路径失败最多让"生成报告"按钮点不动，绝不能把面板搞崩。
"""

from __future__ import annotations

import configparser
import contextlib
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_BACKEND = os.path.join(_ROOT, "backend")

#: 卡死看门狗的心跳文件名（本进程写、后端进程读）。
#: 面板侧不能 import backend/feedback.py 来拿这个常量（它跑在 ADS 解释器里），
#: 所以这里有一份同样的字面量：改名字必须两边一起改，
#: ``tests/test_feedback.py`` 与 ``tests/test_feedback_panel.py`` 会断言两处一致。
HEARTBEAT_NAME = "ads_panel"

#: ``[feedback]`` 段的键与默认值。必须与 ``backend/feedback.py`` 的 ``DEFAULTS``
#: 保持同名同义：后端读的是同一份 config.ini，键名对不上就等于开关失效。
DEFAULTS = {
    "enabled": True,
    "auto_open_browser": False,
    "include_logs": True,
    "max_log_lines": 120,
    "hang_seconds": 180,
}

_TRUE = ("1", "true", "yes", "on")
_SECTION = "feedback"

_TIMEOUT = 10  # 秒。生成报告要在本机扫日志，比普通接口慢，但不该无限等

#: 回环请求必须绕过 HTTP 代理：企业网络常设 HTTP_PROXY，urllib 会把
#: 127.0.0.1 的请求也发给代理，于是面板报"后端未启动"（其实是通的）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_MODULES: dict = {}
_LOAD_LOCK = threading.RLock()

#: 最近一次 HTTP 失败的说明。``post_report`` 失败一律返回 ``{}``（调用方只需判断
#: 真值），具体原因用 :func:`last_error` 取，界面上据此给一句可操作的提示。
_LAST = {"error": ""}


# ---------------------------------------------------------------------------
# 按文件路径加载共享模块
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _scoped(mapping: dict):
    """临时把若干顶层名登记进 ``sys.modules``，退出时**完整还原**。

    只在 ``exec_module`` 期间有效：``backend/adslog.py`` 导入即求值
    ``paths.logs_dir()``，没有这一步就会 ModuleNotFoundError。
    """
    saved: dict = {}
    added: list = []
    for name, mod in mapping.items():
        if name in sys.modules:
            saved[name] = sys.modules[name]
        else:
            added.append(name)
        sys.modules[name] = mod
    try:
        yield
    finally:
        for name in added:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def _spec_load(module_name: str, path: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载共享模块: {path}")
    module = importlib.util.module_from_spec(spec)
    # 先登记再 exec，避免模块内部二次导入造成两份实例
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _shared(filename: str, module_name: str, probe: tuple):
    """按路径加载 ``backend/<filename>``（进程内单例）。

    ``probe`` 是必须存在的属性：正常 import 得到的是别的同名模块时据此拒绝，
    绝不把不相干的模块当成共享实现用。
    """
    with _LOAD_LOCK:
        cached = _MODULES.get(module_name)
        if cached is not None:
            return cached

        stem = os.path.splitext(filename)[0]
        try:  # 测试/后端进程里 backend 可能本来就在 sys.path 上
            import importlib

            mod = importlib.import_module(stem)
            if all(hasattr(mod, a) for a in probe):
                _MODULES[module_name] = mod
                return mod
        except Exception:  # noqa: BLE001 — 正常路径不通就退回按路径加载
            pass

        target = os.path.join(_BACKEND, filename)
        if not os.path.isfile(target):
            raise ImportError(f"找不到共享模块: {target}")
        mod = _spec_load(module_name, target)
        _MODULES[module_name] = mod
        return mod


def _paths():
    """共享 paths 模块（与 pathbridge 同一个实例）。"""
    with _LOAD_LOCK:
        cached = _MODULES.get("paths")
        if cached is not None:
            return cached
        try:
            import pathbridge  # 同目录；重复实现会让两端读出不同的数据根

            mod = pathbridge.load()
        except Exception:  # noqa: BLE001
            mod = _shared("paths.py", "ads_agent_shared_paths",
                          ("data_root", "config_path", "PLUGIN_VERSION"))
        _MODULES["paths"] = mod
        return mod


def _feedback_module():
    """共享 feedback 模块（进程内单例）。"""
    with _LOAD_LOCK:
        cached = _MODULES.get("feedback")
        if cached is not None:
            return cached
        probe = ("HEARTBEAT_NAME", "write_heartbeat", "read_heartbeat",
                 "feedback_dir", "PRIVACY_NOTICE", "snapshot")
        try:
            import importlib

            mod = importlib.import_module("feedback")
            if all(hasattr(mod, a) for a in probe):
                _MODULES["feedback"] = mod
                return mod
        except Exception:  # noqa: BLE001
            pass

        target = os.path.join(_BACKEND, "feedback.py")
        if not os.path.isfile(target):
            raise ImportError(f"找不到共享模块: {target}")
        paths_mod = _paths()
        # adslog 导入即读 paths；feedback 再用到 adslog / paths 两个顶层名。
        with _scoped({"paths": paths_mod}):
            adslog_mod = _shared("adslog.py", "ads_agent_shared_adslog", ("redact",))
        with _scoped({"paths": paths_mod, "adslog": adslog_mod}):
            mod = _spec_load("ads_agent_shared_feedback", target)
        _MODULES["feedback"] = mod
        return mod


# ---------------------------------------------------------------------------
# 配置（[feedback] 段）
# ---------------------------------------------------------------------------

def _config_path() -> str:
    """config.ini 的真实位置（与后端、工具服务读到的同一份）。"""
    return str(_paths().config_path())


def _read_raw() -> dict:
    parser = configparser.ConfigParser()
    path = _config_path()
    try:
        if os.path.isfile(path):
            parser.read(path, encoding="utf-8")
        if parser.has_section(_SECTION):
            return dict(parser.items(_SECTION))
    except Exception:  # noqa: BLE001 — 配置坏了就用默认值，诊断功能不该因此失效
        pass
    return {}


def read_settings() -> dict:
    """读 ``[feedback]`` 段并合并默认值；读不到就是默认值，**不抛异常**。

    为什么要自己读而不是问后端：``backend/feedback.py`` 的 ``settings()`` 走
    ``config.load()``，而 ``config.load()`` 只解析 llm/backend/ads 等段，拿不到
    ``[feedback]``。面板要显示"用户上次勾了什么"，只能自己读这一份。
    """
    cfg = dict(DEFAULTS)
    raw = _read_raw()
    for key, default in DEFAULTS.items():
        if key not in raw:
            continue
        value = str(raw.get(key) or "").strip()
        if isinstance(default, bool):
            cfg[key] = value.lower() in _TRUE
        else:
            try:
                cfg[key] = int(value or 0) if value else default
            except (TypeError, ValueError):
                cfg[key] = default
    return cfg


def save_settings(values: dict) -> bool:
    """把 ``values`` 写进 config.ini 的 ``[feedback]`` 段（只动这一段）。

    写盘走 ``backend/ads_auth.py`` 的 ``edit_config`` —— 它是 config.ini 的
    **唯一**写入通道（跨进程锁 + 原子替换）；自己 open(...,"w") 会把后端刚存的
    令牌或 API 设置按旧快照覆盖回去。返回是否写成功。
    """
    clean: dict = {}
    for key, default in DEFAULTS.items():
        if key not in values:
            continue
        value = values[key]
        if isinstance(default, bool):
            clean[key] = "true" if value else "false"
        else:
            try:
                clean[key] = str(int(value))
            except (TypeError, ValueError):
                continue
    if not clean:
        return False
    try:
        import authbridge

        auth = authbridge.auth()

        def mutate(lines: list) -> None:
            start = None
            for i, line in enumerate(lines):
                if line.strip().lower() == f"[{_SECTION}]":
                    start = i
                    break
            if start is None:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(f"[{_SECTION}]")
                start = len(lines) - 1

            end = len(lines)
            for i in range(start + 1, len(lines)):
                stripped = lines[i].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    end = i
                    break

            pending = dict(clean)
            for i in range(start + 1, end):
                line = lines[i]
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith(";"):
                    continue
                key = stripped.split("=", 1)[0].strip().lower()
                if key in pending:
                    lines[i] = f"{key} = {pending.pop(key)}"
            insert_at = end
            for key, value in pending.items():
                lines.insert(insert_at, f"{key} = {value}")
                insert_at += 1

        return bool(auth.edit_config(mutate))
    except BaseException as e:  # noqa: BLE001 — 保存失败不能让面板崩
        _LAST["error"] = f"{type(e).__name__}: {e}"
        return False


# ---------------------------------------------------------------------------
# 心跳（卡死看门狗）
# ---------------------------------------------------------------------------

def heartbeat(name: str = HEARTBEAT_NAME, payload: dict | None = None) -> bool:
    """写一次心跳文件 ``<数据根>/runtime/heartbeat_<name>.json``。

    必须由**主线程**的定时器来调：把它放独立线程就永远在跳，"界面卡死但进程
    还活着"这个真正要检测的状态就漏掉了。失败返回 False，不抛。
    """
    try:
        return bool(_feedback_module().write_heartbeat(
            name, dict(payload or {})))
    except BaseException:  # noqa: BLE001
        return False


def read_heartbeat(name: str = HEARTBEAT_NAME) -> dict:
    try:
        return dict(_feedback_module().read_heartbeat(name) or {})
    except BaseException:  # noqa: BLE001
        return {}


def dir(create: bool = False) -> str:
    """反馈报告目录（``<数据根>/feedback``）；模块加载不了时用等价路径兜底。"""
    try:
        return str(_feedback_module().feedback_dir(create=create))
    except BaseException:  # noqa: BLE001
        try:
            root = os.path.join(str(_paths().data_root()), "feedback")
            if create:
                os.makedirs(root, exist_ok=True)
            return root
        except BaseException:  # noqa: BLE001
            return ""


def privacy_notice() -> str:
    """隐私说明全文（唯一来源是 backend/feedback.py 的 PRIVACY_NOTICE）。"""
    try:
        return str(getattr(_feedback_module(), "PRIVACY_NOTICE", "") or "")
    except BaseException:  # noqa: BLE001
        return ""


def snapshot_local(kind: str, error_text: str = "", context: dict | None = None) -> dict:
    """不经过后端进程，直接在本机生成一份报告（后端没起时的兜底）。"""
    try:
        return dict(_feedback_module().snapshot(kind, error_text, context) or {})
    except BaseException as e:  # noqa: BLE001
        _LAST["error"] = f"{type(e).__name__}: {e}"
        return {}


# ---------------------------------------------------------------------------
# 后端 HTTP（只读 + 生成本机报告；面板从不直接连 GitHub）
# ---------------------------------------------------------------------------

def _backend_base() -> str:
    """后端基地址，与 panel._backend_base() 同源同值（同一份 config.ini）。"""
    host, port = "127.0.0.1", "8760"
    parser = configparser.ConfigParser()
    try:
        path = _config_path()
        if os.path.isfile(path):
            parser.read(path, encoding="utf-8")
            host = (parser.get("backend", "host", fallback="") or host).strip() or host
            port = (parser.get("backend", "port", fallback="") or port).strip() or port
    except Exception:  # noqa: BLE001 — 配置读不到就用默认回环地址
        pass
    return f"http://{host}:{port}"


def _auth_header() -> dict:
    try:
        import authbridge

        return {authbridge.header_name(): authbridge.token()}
    except Exception:  # noqa: BLE001 — 取不到就发出去，让后端回 401
        return {}


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return ("后端版本过旧（缺少反馈接口），请关闭后端窗口后重新运行 start_backend.bat")
        if exc.code == 401:
            return "后端拒绝了本次请求（回环令牌不匹配），请重启后端与 ADS"
        return f"后端返回 HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"后端未启动（{exc.reason}）"
    return f"{type(exc).__name__}: {exc}"


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = _backend_base() + path
    headers = {"Accept": "application/json"}
    headers.update(_auth_header())
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    obj = json.loads(raw or "{}")
    return obj if isinstance(obj, dict) else {}


def last_error() -> str:
    """最近一次失败的可读说明（成功时为空串）。"""
    return str(_LAST.get("error") or "")


def post_report(kind: str, error_text: str = "", context: dict | None = None) -> dict:
    """``POST /feedback/report`` → 报告 markdown / 预填 issue 链接。

    成功后返回后端的原样响应（``ok/markdown/title/url/duplicate/paths/dir/
    privacy_notice/repo``）；任何失败都返回 ``{}`` 并把原因记进 :func:`last_error` ——
    调用方只需判断真值，然后退回本机快照。
    """
    _LAST["error"] = ""
    try:
        data = _request("POST", "/feedback/report", {
            "kind": str(kind or "manual"),
            "error": str(error_text or ""),
            "context": dict(context or {}),
        })
        if not data:
            raise ValueError("后端返回了空响应")
        return data
    except BaseException as e:  # noqa: BLE001
        _LAST["error"] = _describe(e)
        return {}


def fetch_pending() -> dict:
    """``GET /feedback/pending`` → ``{ok, duplicates, stale}``；失败返回 ``{}``。"""
    _LAST["error"] = ""
    try:
        return _request("GET", "/feedback/pending")
    except BaseException as e:  # noqa: BLE001
        _LAST["error"] = _describe(e)
        return {}
