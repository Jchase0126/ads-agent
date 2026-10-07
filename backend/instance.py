"""本地服务实例登记与身份校验。

改造前 ``backend_launcher`` 判断"后端在不在"只看 ``/health`` 是不是 200 ——
只要那个端口上坐着**任何一个** HTTP 服务（旧版本后端、另一个 ADS Agent、甚至是
完全无关的本地程序）都会被当成"本插件的后端已在运行"，然后面板连过去，
表现为"明明连上了却一直 401"，或者更糟：把指令发给了别人的服务。

现在探测必须同时满足三件事才算"可以复用"：

1. **身份** —— ``service`` 必须是本服务的名字（``ads_agent_backend`` /
   ``ads_agent_toolserver``），且 ``protocol`` 与本端一致（协议变更即视为不同服务）；
2. **归属** —— ``identity.install_id`` 必须是**这份安装**的 ID（存在数据根目录里）。
   另一份安装（别的目录/别的机器拷来的）即使端口相同也不接管；
3. **存活** —— 登记文件里的 pid 必须还活着。

任何一条不满足都**不会**被当成"已在运行"，而是给出可执行的错误提示
（端口被谁占、是不是旧版本、要不要先停掉那一个），不会闷头连错实例。

登记文件写在 ``<数据根>/runtime/<kind>.json``，进程正常退出时删除；
异常崩溃留下的陈旧登记会被下一次探测按 pid 存活自动清掉。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.request

try:
    import paths
except Exception:  # pragma: no cover — 被按文件路径加载时 backend/ 不在 sys.path 上
    import importlib.util as _ilu
    import sys as _sys

    _paths = _sys.modules.get("ads_agent_shared_paths")
    if _paths is None:
        _target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paths.py")
        _spec = _ilu.spec_from_file_location("ads_agent_shared_paths", _target)
        if _spec is None or _spec.loader is None:
            raise ImportError(f"无法加载共享路径模块: {_target}")
        _paths = _ilu.module_from_spec(_spec)
        _sys.modules["ads_agent_shared_paths"] = _paths
        _spec.loader.exec_module(_paths)
    paths = _paths  # type: ignore

SERVICE_BACKEND = "ads_agent_backend"
SERVICE_TOOLSERVER = "ads_agent_toolserver"

_KNOWN_SERVICES = {SERVICE_BACKEND, SERVICE_TOOLSERVER}


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------

def identity(extra: dict | None = None) -> dict:
    """本进程的身份。放进 ``/health`` 供对端校验。

    只放**不含机密**的定位信息：安装 ID 是随机 UUID 的前 16 位，
    不是令牌，泄漏出去也驱动不了任何接口（接口另有令牌鉴权）。
    """
    data_root = paths.data_root()
    info = {
        "install_id": paths.install_id(),
        "plugin_version": paths.PLUGIN_VERSION,
        "protocol": paths.PROTOCOL_VERSION,
        "app_root": paths.app_root(),
        "data_root": data_root,
        "root_fingerprint": hashlib.sha256(
            os.path.normcase(data_root).encode("utf-8")
        ).hexdigest()[:12],
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if extra:
        info.update(extra)
    return info


# ---------------------------------------------------------------------------
# 登记文件
# ---------------------------------------------------------------------------

def _opener():
    """回环请求绕过代理 —— 否则设了 HTTP_PROXY 的环境会把探测结果搞乱。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def write_instance(kind: str, port: int, host: str = "127.0.0.1",
                   extra: dict | None = None) -> str:
    """写下本进程的实例登记，返回文件路径。"""
    path = paths.instance_path(kind, str(port))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(identity(extra))
    payload.update({"kind": kind, "host": host, "port": port})
    tmp = f"{path}.tmp_{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def list_instances(kind: str | None = None, alive_only: bool = True) -> list:
    """runtime 目录下的实例登记（带上登记文件路径）。

    ``alive_only=True`` 时先按 pid 过滤掉已退出的进程。
    """
    runtime = paths.runtime_dir()
    found = []
    if not os.path.isdir(runtime):
        return found
    prefix = f"{kind}_" if kind else None
    for name in sorted(os.listdir(runtime)):
        if not name.endswith(".json"):
            continue
        if prefix and not name.startswith(prefix):
            continue
        try:
            with open(os.path.join(runtime, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            alive = pid_alive(int(data.get("pid") or -1))
        except (TypeError, ValueError):
            alive = False
        if alive_only and not alive:
            continue
        data["_file"] = os.path.join(runtime, name)
        data["_alive"] = alive
        found.append(data)
    return found


def read_instance(kind: str, port: int | None = None) -> dict | None:
    """读自己的登记；``port`` 为空时取该 kind 里最新的一个存活登记。"""
    if port:
        return _read_one(paths.instance_path(kind, str(port)))
    entries = list_instances(kind, alive_only=True)
    if not entries:
        return None
    entries.sort(key=lambda d: str(d.get("started_at") or ""))
    return entries[-1]


def _read_one(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_instance(kind: str, port: int | None = None) -> bool:
    """删除**自己**的登记（别人的别动）。"""
    path = paths.instance_path(kind, str(port) if port else "")
    targets = [path] if port else [
        e["_file"] for e in list_instances(kind, alive_only=False)
        if int(e.get("pid") or -1) == os.getpid()
    ]
    ok = False
    for target in targets:
        try:
            os.unlink(target)
            ok = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return ok


def pid_alive(pid: int) -> bool:
    """pid 是否还活着（跨平台，Windows 用 OpenProcess）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cleanup_stale_instances() -> list:
    """清掉 pid 已死还赖着的登记文件，返回被清理的 kind 列表。"""
    cleaned = []
    runtime = paths.runtime_dir()
    if not os.path.isdir(runtime):
        return cleaned
    for name in sorted(os.listdir(runtime)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(runtime, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, UnicodeError):
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            pid = -1
        if not pid_alive(pid):
            try:
                os.unlink(path)
                cleaned.append(name[:-5])
            except OSError:
                pass
    return cleaned


def same_install_conflicts(kind: str, port: int) -> list:
    """同一份安装在**别的端口**上还跑着的同类服务 —— 真正的"多开冲突"。

    首版只支持单实例：同一 install_id 却出现两个不同端口的实例，说明用户
    起了两次（或上一次没退干净），必须报出来 —— 否则面板连到 A、工具连到 B，
    症状是"工具调用偶发失败 / 随机 401"，极难排查。

    同一 install_id、同一端口的多个登记是**正常**的（多个 ADS 窗口共享同一个后端）。
    """
    mine = paths.install_id()
    conflicts = []
    for item in list_instances(kind, alive_only=True):
        if str(item.get("install_id") or "") != mine:
            continue
        try:
            their_port = int(item.get("port") or -1)
        except (TypeError, ValueError):
            continue
        if their_port > 0 and their_port != port:
            conflicts.append(item)
    return conflicts


def foreign_install_instances(kind: str) -> list:
    """还在跑的、**别的安装**（install_id 不同）的同类服务。

    典型来源：旧目录里那份没卸干净还在跑，或者是别人拷来的副本。
    连过去会变成"明明通着却做不了事"，所以单独识别、单独提示。
    """
    mine = paths.install_id()
    return [
        item for item in list_instances(kind, alive_only=True)
        if str(item.get("install_id") or "") != mine
    ]


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def probe(url: str, timeout: float = 2.0) -> dict:
    """取一次 ``/health``。返回 ``{"reachable", "payload", "error"}``。

    只做 HTTP GET，不做任何判断 —— 判断在 :func:`evaluate` 里。
    """
    try:
        with _opener().open(url.rstrip("/") + "/health", timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {"reachable": True, "payload": None,
                    "error": "响应不是 JSON 对象"}
        return {"reachable": True, "payload": payload, "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "payload": None,
                "error": f"{type(e).__name__}: {e}"}


def evaluate(probed: dict, kind: str) -> dict:
    """判定一个已探测到的服务能不能当作"本插件的实例"复用。

    返回 ``{"usable", "reason", "detail", "identity", "conflict"}``。
    """
    expected_service = SERVICE_BACKEND if kind == "backend" else SERVICE_TOOLSERVER
    expected_protocol = paths.PROTOCOL_VERSION
    mine = paths.install_id()
    out = {"usable": False, "reason": "", "detail": "", "identity": None,
           "conflict": False}

    if not probed.get("reachable"):
        out["reason"] = "unreachable"
        out["detail"] = probed.get("error") or "无法连接"
        return out

    payload = probed.get("payload")
    if not isinstance(payload, dict):
        out["reason"] = "bad_payload"
        out["detail"] = probed.get("error") or "响应格式不对"
        return out

    # service 是规范字段；server 是 ADS 端工具服务用过的历史键名，一起认。
    # 只认一个键名会出这种事：/health 手测一切正常，而自动探测一律判成
    # "端口被其它程序占用"（2026-10-07 实机验收抓到）。
    service = str(payload.get("service") or payload.get("server") or "")
    protocol = payload.get("protocol")
    ident = payload.get("identity") if isinstance(payload.get("identity"), dict) else None
    out["identity"] = ident

    # 1) 是不是本插件的服务
    if service in _KNOWN_SERVICES and service != expected_service:
        out["reason"] = "wrong_service"
        out["conflict"] = True
        out["detail"] = (f"端口上是 ADS Agent 的{_cn(service)}，"
                         f"而这里要的是{_cn(expected_service)}（两者端口配错或重复了）")
        return out
    if service != expected_service:
        out["reason"] = "foreign_service"
        out["conflict"] = True
        out["detail"] = f"端口被**其它程序**占用（对方自报 {service or '未知服务'}）"
        return out

    # 2) 协议版本 —— 旧版本后端也算冲突，不会闷头复用
    try:
        proto_int = int(protocol)
    except (TypeError, ValueError):
        proto_int = -1
    if proto_int != expected_protocol:
        out["reason"] = "protocol_mismatch"
        out["conflict"] = True
        out["detail"] = (f"协议版本不一致：对方是 v{proto_int if proto_int>=0 else '未知（更旧的版本）'}，"
                         f"本插件是 v{expected_protocol}。请先停掉旧的 ADS Agent 后端再启动")
        return out

    # 3) 归属（install_id）—— 没有身份信息的版本：保守起见也当冲突，
    #    因为无法证明它属于这份安装（可能连的是别的机器拷来的实例）
    if ident is None:
        out["reason"] = "no_identity"
        out["conflict"] = True
        out["detail"] = ("该后端没有上报身份（比 "
                         f"{paths.PLUGIN_VERSION} 更旧的版本）。无法确定它属于这份安装，"
                         "已按冲突处理 —— 请先停止它再启动新版")
        return out

    theirs = str(ident.get("install_id") or "")
    if not theirs:
        out["reason"] = "no_install_id"
        out["conflict"] = True
        out["detail"] = "该后端没有上报安装 ID，无法确定归属"
        return out
    if theirs != mine:
        out["reason"] = "foreign_install"
        out["conflict"] = True
        out["detail"] = (f"端口上跑的是**另一个 ADS Agent 安装**（数据目录 "
                         f"{ident.get('data_root') or '未知'}）。两个实例会互相抢同一个端口，"
                         f"请先停掉那个，或给本插件换一个端口")
        return out

    # 3.5) 同一份安装、但挂在不同 ADS 版本下 —— 跨版本实例冲突。
    # 同一份插件安装（同 install_id）被 ADS 2024–2027 共用时，同一时刻只能
    # 服务一个 ADS 版本：工具服务与 DE 数据库是一一对应的，连错版本会把指令
    # 发到另一个 ADS 里去。两侧都上报了 ads_dir 且不同 → 明确拒绝，不误连。
    their_ads_dir = os.path.normcase(os.path.normpath(str(ident.get("ads_dir") or "")))
    my_ads_dir = os.path.normcase(os.path.normpath(
        (os.environ.get(paths.ENV_ADS_DIR) or "").strip().strip('"')
    ))
    if their_ads_dir and my_ads_dir and their_ads_dir != my_ads_dir:
        out["reason"] = "cross_version_conflict"
        out["conflict"] = True
        out["detail"] = (f"该服务属于另一个 ADS 版本（对方 ADS 目录 {ident.get('ads_dir')}，"
                         f"本实例 {my_ads_dir}）。同一份插件安装同时只能服务一个 ADS 版本，"
                         f"请先退出另一个 ADS，或给本版本换一个端口")
        return out

    # 4) 同一份安装，但程序目录不同 —— 典型的"升级后换了安装位置"，可复用但要说清楚
    note = ""
    if ident.get("app_root") and os.path.normcase(str(ident["app_root"])) != \
            os.path.normcase(paths.app_root()):
        note = f"（该实例来自 {ident.get('app_root')}，与本实例的 {paths.app_root()} 不同 —— 通常是刚升级过）"

    out["usable"] = True
    out["reason"] = "ok"
    out["detail"] = (f"复用了已运行的{_cn(expected_service)} pid="
                     f"{ident.get('pid') or '?'} v{ident.get('plugin_version') or '?'}{note}")
    return out


def _cn(service: str) -> str:
    return {"ads_agent_backend": "后端", "ads_agent_toolserver": "工具服务"}.get(service, service)


def port_in_use(host: str, port: int) -> bool:
    """端口是否被占（不管被谁占）。用于"连不上但端口也起不来"的报错。"""
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.close()
        return False
    except OSError:
        return True
