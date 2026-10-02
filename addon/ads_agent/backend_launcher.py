"""Auto-start / supervise the Agent backend from inside the ADS process.

The backend (backend/server.py) is pure-stdlib Python, so we simply spawn it
with ADS's own bundled interpreter (sys.executable inside the addon) as a
hidden child process, logging to <project>/logs/backend.log. If an orphan
from a previous session is still serving, we reuse it instead of spawning.

Qt-free on purpose: usable from tests and from any thread.
"""

import configparser
import os
import subprocess
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_SERVER = os.path.join(_ROOT, "backend", "server.py")
_LOG_DIR = os.path.join(_ROOT, "logs")
_LOG = os.path.join(_LOG_DIR, "backend.log")
_CONFIG = os.path.join(_ROOT, "config.ini")

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def _base_url() -> str:
    parser = configparser.ConfigParser()
    if os.path.exists(_CONFIG):
        parser.read(_CONFIG, encoding="utf-8")
    host = parser.get("backend", "host", fallback="127.0.0.1").strip() or "127.0.0.1"
    port = parser.get("backend", "port", fallback="8760").strip() or "8760"
    return f"http://{host}:{port}"


def backend_alive(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(_base_url() + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_backend(wait: float = 15.0) -> tuple[bool, str]:
    """Make sure the backend is running; spawn it if needed. May block ~seconds."""
    global _proc
    with _lock:
        if backend_alive():
            return True, "后端已在运行"

        os.makedirs(_LOG_DIR, exist_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # ALL three std streams must be redirected explicitly: the ADS GUI
        # process has no console, so its stdin is an INVALID handle — letting
        # the child inherit it makes CreateProcess fail with
        # "OSError: [WinError 6] 句柄无效".
        with open(_LOG, "ab") as logf, open(os.devnull, "rb") as devnull:
            _proc = subprocess.Popen(
                [sys.executable, _SERVER],
                cwd=_ROOT,
                stdin=devnull,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=flags,
            )

        deadline = time.time() + wait
        while time.time() < deadline:
            if backend_alive(1.0):
                return True, "后端已自动启动"
            if _proc.poll() is not None:
                return False, (
                    f"后端启动失败（退出码 {_proc.returncode}），详情见 logs\\backend.log"
                )
            time.sleep(0.4)
        return False, "后端启动超时，详情见 logs\\backend.log"


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
            except Exception:
                pass
        _proc = None
