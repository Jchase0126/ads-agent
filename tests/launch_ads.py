"""自动启动 ADS 2027 并打开 AI workspace。

套路（来自 2026-09-26 实测）：
  1. start ads.exe（launcher，主进程是 hpeesofde.exe）
  2. UIA 点 Product Selection 的 OK
  3. 等 toolserver 端口（config.ini [ads] port）LISTENING
  4. 走 /execute 让 ADS 进程内 open_workspace
  5. UIA Invoke Get Started 的 Close（模态窗会阻塞 toolserver）

用法: python tests/launch_ads.py
"""
import os
import socket
import subprocess
import sys
import time

ADS_EXE = r"E:\ADS\Programfile\ADS2027\bin\ads.exe"
WS_PATH = "E:/ADS/Work_AI/BFR106_wrk"

BACKEND = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import ads_auth  # noqa: E402
import config as _cfg  # noqa: E402
import urllib.request  # noqa: E402
import urllib.error  # noqa: E402
import json  # noqa: E402

PORT = int(_cfg.load().get("ads_port", 8762))
URL = _cfg.ads_base_url(_cfg.load()) + "/execute"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def list_windows():
    try:
        wins = uia().windows()
        return [(w.window_text() or "")[:60] for w in wins]
    except Exception as e:  # noqa: BLE001
        return [f"<enumerate failed: {e}>"]


def click_button_in(title_pat, btn_pat, timeout=3.0):
    """在标题含 title_pat 的窗口里 Invoke 文本含 btn_pat 的按钮。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            for w in uia().windows():
                t = w.window_text() or ""
                if title_pat.lower() not in t.lower():
                    continue
                for b in w.descendants(control_type="Button"):
                    bt = b.window_text() or ""
                    if btn_pat.lower() in bt.lower():
                        b.invoke()
                        return f"clicked {bt!r} in {t!r}"
        except Exception:
            pass
        time.sleep(0.4)
    return None


def port_up(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(2)
    return False


def execute(code):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"name": "run_python", "args": {"code": code}}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
        method="POST")
    with _OPENER.open(req, timeout=300) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("stdout", "") or str(body)


def main():
    print("start ads.exe ...")
    subprocess.Popen([ADS_EXE], cwd=os.path.dirname(ADS_EXE))

    # 2) Product Selection / license 对话框 → OK
    for _ in range(3):
        r = click_button_in("select", "ok", timeout=20)
        if r:
            print("  ", r)
            break
        r = click_button_in("product", "ok", timeout=10)
        if r:
            print("  ", r)
            break
    else:
        print("  no product-selection dialog handled; windows now:")
        for t in list_windows():
            print("   -", t)

    print(f"waiting toolserver on {PORT} ...")
    if not port_up():
        print("TIMEOUT waiting for toolserver. windows:")
        for t in list_windows():
            print("   -", t)
        return 1
    print("toolserver up.")

    print("open workspace ...")
    out = execute(
        "from keysight.ads import de\n"
        f"de.open_workspace(r'{WS_PATH}')\n"
        "print('ws:', de.active_workspace())")
    print("  ", out.strip()[:200])

    # 5) Get Started 欢迎窗 → Close
    r = click_button_in("get started", "close", timeout=8) or \
        click_button_in("getting started", "close", timeout=5)
    print("  get-started:", r or "not found (ok)")

    # 最终确认 workspace 可用
    out = execute(
        "from keysight.ads import de\n"
        "print('open:', de.workspace_is_open(), de.active_workspace())")
    print("final:", out.strip()[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
