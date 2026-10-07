"""Auto-start / supervise the Agent backend from inside the ADS process.

The backend (backend/server.py) is pure-stdlib Python, so we spawn it as a
hidden child process, logging to the **user data** log directory
(``%LOCALAPPDATA%\\ADSAgent\\logs\\backend.log``) rather than beside the code.

挑选解释器的顺序见 ``backend/adslocate.py``：**优先 ADS 自带 python**。
这个顺序是有讲究的 —— 后端只依赖标准库，用 ADS 自带的那个永远能跑，
也就不存在"还要先另装 Python"这回事。

Qt-free on purpose: usable from tests and from any thread.

复用判断不再是"`/health` 返回 200"：必须经过 ``backend/instance.py`` 的
身份 + 协议 + 归属三重校验。校验不过会**带着可执行的提示直接失败**，
不会闷头连到一个说不清是谁的服务上（详见该模块的说明）。
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import threading
import time

import pathbridge

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_url_cache: str | None = None


# ---------------------------------------------------------------------------
# 位置与解释器
# ---------------------------------------------------------------------------

def app_root() -> str:
    try:
        return pathbridge.load().app_root()
    except Exception:  # noqa: BLE001
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(here, "..", ".."))


def backend_script() -> str:
    return os.path.join(app_root(), "backend", "server.py")


def _log_file() -> str:
    try:
        return pathbridge.load().log_path("backend")
    except Exception:  # noqa: BLE001
        return os.path.join(app_root(), "logs", "backend.log")


def _instance():
    """取 backend/instance.py（实例登记与身份校验）。"""
    return pathbridge.load_backend_module("instance.py", "instance")


def _adslocate():
    """取 backend/adslocate.py（ADS 目录与解释器查找）。"""
    try:
        return pathbridge.load_backend_module("adslocate.py", "adslocate")
    except Exception as e:  # noqa: BLE001
        raise ImportError(f"无法加载 adslocate: {e}")


def pick_python() -> tuple[str, str]:
    """挑一个能跑后端的解释器，返回 ``(exe, 说明)``。

    后端只用标准库，理论上任何 Python 3 都行；首选 ADS 自带的那一个是因为
    **它必然存在**（ADS 自己要用），用户机器上有没有装 Python 都能跑。
    """
    try:
        adslocate = _adslocate()
    except Exception as e:  # noqa: BLE001
        return (sys.executable or ""), f"当前解释器（{type(e).__name__}）"

    if adslocate.is_ads_python(sys.executable or ""):
        return sys.executable, "ADS 自带 Python"

    hpeesof = (os.environ.get("HPEESOF_DIR") or "").strip()
    if hpeesof:
        bundled = adslocate.ads_bundled_python(hpeesof)
        if bundled and os.path.isfile(bundled):
            return bundled, "ADS 自带 Python（按 HPEESOF_DIR 找到）"

    chosen = adslocate.choose_interpreter()
    if chosen.get("exe"):
        return chosen["exe"], chosen.get("source") or "自动选择"
    return sys.executable or "", "当前解释器（未找到 ADS 自带 Python，退回当前进程）"


def _base_url() -> str:
    parser = configparser.ConfigParser()
    path = _config_path()
    if path and os.path.exists(path):
        parser.read(path, encoding="utf-8")
    host = parser.get("backend", "host", fallback="127.0.0.1").strip() or "127.0.0.1"
    port = parser.get("backend", "port", fallback="8760").strip() or "8760"
    return f"http://{host}:{port}"


def _config_path() -> str:
    try:
        return pathbridge.load().config_path()
    except Exception:  # noqa: BLE001
        return os.path.join(app_root(), "config.ini")


def backend_port() -> int:
    try:
        return int(str(_base_url().rsplit(":", 1)[-1]).strip("/"))
    except (TypeError, ValueError):
        return 8760


def backend_host() -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(_base_url()).hostname or "127.0.0.1"
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------

def probe_backend(timeout: float = 2.0) -> dict:
    """探一次并把结论算好返回。

    ``{"reachable", "verdict"}`` —— ``verdict`` 见 ``instance.evaluate``：
    只有 ``usable=True`` 才算"可以复用"。
    """
    inst = _instance()
    probed = inst.probe(_base_url() + "/health", timeout=timeout)
    verdict = inst.evaluate(probed, "backend")
    return {"reachable": probed["reachable"], "verdict": verdict,
            "payload": probed.get("payload")}


def backend_alive(timeout: float = 1.0) -> bool:
    """是否在跑**且是本插件这份安装**的实例。"""
    try:
        return bool(probe_backend(timeout)["verdict"]["usable"])
    except Exception:  # noqa: BLE001
        return False


def _log_tail(n: int = 12) -> str:
    try:
        with open(_log_file(), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    tail = lines[-n:]
    return " | ".join(t.strip() for t in tail if t.strip())[-800:]


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def ensure_backend(wait: float = 20.0) -> tuple[bool, str]:
    """Make sure a backend we can trust is running; spawn it if needed.

    May block for a few seconds. Never returns success on an unverified
    service: either we identified it as *our* instance, or we report failure.
    """
    global _proc
    with _lock:
        try:
            pathbridge.load().ensure_data_dirs()
        except Exception:  # noqa: BLE001 — 目录建不出来后面会再失败一次，这里不抢着报错
            pass

        probed = probe_backend(1.5)
        verdict = probed["verdict"]
        if verdict["usable"]:
            return True, verdict["detail"] or "后端已在运行"
        if verdict["conflict"]:
            inst = _instance()
            extra = ""
            foreigners = inst.foreign_install_instances("backend")
            if foreigners:
                extra = ("；同机另有另一个安装的后端："
                         + "、".join(f"pid={f.get('pid')}" for f in foreigners))
            clashes = inst.same_install_conflicts("backend", backend_port())
            if clashes:
                extra += ("；检测到本安装的重复实例："
                          + "、".join(f"pid={c.get('pid')} 端口={c.get('port')}"
                                      for c in clashes))
            return False, (f"端口上的服务无法确认归属，已拒绝连接 —— {verdict['detail']}"
                           f"{extra}。请结束多余进程后重试（Tools ▸ ADS Agent ▸ 重启后端）。"
                           f"日志：{_log_file()}")

        # 到这里说明端口没人答话（或答的不是我们的服务）。起一个新的。
        try:
            inst = _instance()
        except Exception as e:  # noqa: BLE001
            return False, f"无法加载实例校验模块：{type(e).__name__}: {e}"

        if inst.port_in_use(backend_host(), backend_port()):
            return False, (
                f"端口 {_base_url()} 已被占用但对方答不上话（不是 ADS Agent 后端）。"
                f"请在 config.ini 的 [backend] port 换一个端口，或者停掉占用它的程序。"
            )

        python, source = pick_python()
        if not python:
            return False, (
                "没有任何可用的 Python 解释器 —— 正常情况下 ADS 自带 "
                "\\tools\\python\\python.exe 一定能找到。请检查 ADS 安装是否完整。"
            )

        log_path = _log_file()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # ALL three std streams must be redirected explicitly: the ADS GUI
        # process has no console, so its stdin is an INVALID handle — letting
        # the child inherit it makes CreateProcess fail with
        # "OSError: [WinError 6] 句柄无效".
        try:
            with open(log_path, "ab") as logf, open(os.devnull, "rb") as devnull:
                _proc = subprocess.Popen(
                    [python, backend_script()],
                    cwd=app_root(),
                    stdin=devnull,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=flags,
                )
        except OSError as e:
            return False, (f"后端启动失败（{type(e).__name__}: {e}）。"
                           f"解释器={python}（{source}）  日志={log_path}")

        deadline = time.time() + wait
        last = ""
        while time.time() < deadline:
            check = probe_backend(1.0)
            if check["verdict"]["usable"]:
                return True, f"后端已启动（{source}）"
            if check["verdict"]["conflict"]:
                # 刚拉起来的这段时间里，可能是另一个进程抢先占了这个端口
                return False, (f"端口出现了一个无法确认归属的服务："
                               f"{check['verdict']['detail']}。日志：{log_path}")
            if _proc.poll() is not None:
                tail = _log_tail()
                return False, (
                    f"后端启动失败，进程已退出（返回码 {_proc.returncode}）。"
                    f"解释器={python}（{source}）  "
                    f"日志尾部：{tail or '(空)'}   完整日志：{log_path}"
                )
            last = check["verdict"].get("detail") or ""
            time.sleep(0.4)
        return False, (f"后端启动超时（{wait:.0f}s 内没能通过身份校验: {last}）。"
                       f"完整日志：{log_path}")


def ensure_backend_async(on_done) -> None:
    """ensure_backend in a daemon thread; on_done(ok, message) from that thread."""

    def run():
        try:
            ok, msg = ensure_backend()
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"{type(e).__name__}: {e}"
        try:
            on_done(ok, msg)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def shutdown_backend() -> None:
    """Terminate the child we spawned (orphans from crashed sessions are
    reused by the next ensure_backend, so this is best-effort cleanup)."""
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            try:
                _proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        _proc = None
