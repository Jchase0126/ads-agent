"""Loopback smoke test for the ADS Agent tool server.

Runs the toolserver inside the ADS bundled Python (automation mode, no GUI),
pumps the job queue with a plain thread (instead of the Qt timer), then
calls tools over HTTP exactly the way backend/tools.py does.

Usage (from anywhere):
  "E:\\ADS\\Programfile\\ADS2027\\tools\\python\\python.exe" tests\\test_loopback.py
"""

import json
import sys
import threading
import time
import urllib.request

ADDON_DIR = __file__.rsplit(os_sep := __import__("os").sep, 2)[0] + os_sep + "addon" + os_sep + "ads_agent"
sys.path.insert(0, ADDON_DIR)

import toolserver  # noqa: E402


class _DummyTimer:
    """Standalone stand-in for the Qt pump timer."""

    def start(self, ms):
        pass


def main() -> int:
    failures = 0

    toolserver._pump_timer = _DummyTimer()
    base = toolserver.ensure_started()
    print(f"toolserver at {base}")

    threading.Thread(
        target=lambda: [toolserver.pump() or time.sleep(0.02) for _ in iter(int, 1)],
        daemon=True,
    ).start()

    parser_token = "ads-agent-local-token"
    try:
        import configparser

        parser = configparser.ConfigParser()
        cfg = ADDON_DIR.rsplit(os_sep, 2)[0] + os_sep + "config.ini"
        if __import__("os").path.exists(cfg):
            parser.read(cfg, encoding="utf-8")
            parser_token = parser.get("ads", "token", fallback=parser_token)
    except Exception:
        pass

    def call_tool(name, args, token=parser_token, expect_error=False):
        req = urllib.request.Request(
            base + "/execute",
            data=json.dumps({"name": name, "args": args}).encode(),
            headers={"Content-Type": "application/json", "X-Ads-Agent-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    import urllib.error  # noqa: E402

    # 1. health
    with urllib.request.urlopen(base + "/health", timeout=10) as r:
        print("health:", r.status, r.read().decode())
        assert r.status == 200

    # 2. get_workspace_info — must answer even with no workspace open
    status, body = call_tool("get_workspace_info", {})
    print("get_workspace_info:", status, json.dumps(body, ensure_ascii=False)[:200])
    if status == 200 and body.get("workspace_open") in (True, False):
        print("  ✅ workspace info OK (open={})".format(body.get("workspace_open")))
    else:
        print("  ❌ unexpected response")
        failures += 1

    # 3. unknown tool -> clean error
    status, body = call_tool("no_such_tool", {})
    if status >= 400 and "error" in body:
        print("  ✅ unknown tool rejected:", body["error"][:80])
    else:
        print("  ❌ unknown tool not rejected:", status, body)
        failures += 1

    # 4. bad token -> 401
    status, body = call_tool("get_workspace_info", {}, token="wrong")
    if status == 401:
        print("  ✅ bad token rejected")
    else:
        print("  ❌ bad token accepted:", status, body)
        failures += 1

    # 5. run_python escape hatch
    status, body = call_tool("run_python", {"code": "print('hello from ADS python', 6*7)"})
    if status == 200 and body.get("ok") and "42" in body.get("stdout", ""):
        print("  ✅ run_python OK:", body["stdout"].strip())
    else:
        print("  ❌ run_python failed:", status, body)
        failures += 1

    print()
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    __import__("os").environ.setdefault("HPEESOF_DIR", r"E:\ADS\Programfile\ADS2027")
    sys.exit(main())
