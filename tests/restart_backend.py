"""一次性运维脚本：重启 ADS Agent 后端（以隐藏进程方式）并写日志。

用法: python tests/_restart_backend.py
"""

import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "backend.log")
PYTHON = sys.executable
HEALTH = "http://127.0.0.1:8760/health"


def _listening_pid(port: int) -> int | None:
    res = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = res.stdout or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _kill(pid: int):
    try:
        os.kill(pid, 15)
    except Exception:
        return
    for _ in range(25):  # 最多等 2.5s 优雅退出
        time.sleep(0.1)
        if _listening_pid(8760) is None:
            return
    try:  # 仍活着则强杀
        os.kill(pid, 9)
    except Exception:
        pass
    time.sleep(0.5)


def alive() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    pid = _listening_pid(8760)
    if pid:
        print(f"[-] 停止旧后端 PID {pid}")
        _kill(pid)
    else:
        print("[-] 端口 8760 上没有监听")
    if alive():
        print("[!] 旧进程仍未退出，放弃")
        return 1

    print(f"[+] 启动新后端  python={PYTHON}")
    # 关键：脱离父进程所在的作业对象，否则调用方（这里往往是一个短暂存在的
    # shell/沙箱）退出时，子进程会被一起回收。
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0x8)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    # start_new_session 在 Windows 上无意义，POSIX 下同样起到脱离的作用
    logf = open(LOG, "ab")  # 进程会一直持有这个句柄
    devnull = open(os.devnull, "rb")
    subprocess.Popen(
        [PYTHON, os.path.join(ROOT, "backend", "server.py")],
        cwd=ROOT, stdin=devnull, stdout=logf, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        creationflags=flags, start_new_session=True,
        close_fds=False,
    )
    for _ in range(40):
        time.sleep(0.25)
        if alive():
            break
    if not alive():
        print("[!] 后端未起来，看 logs/backend.log")
        return 1
    with urllib.request.urlopen(HEALTH, timeout=3) as r:
        print("[OK] 后端已就绪:", r.read().decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
