"""直接调 ADS toolserver 的具名工具（build_schematic 等），不经 backend。

用法: python tests/call_tool.py <tool_name> <json_args_file> [timeout]
"""
import json
import os
import sys
import urllib.request

BACKEND = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ads_auth  # noqa: E402
import config as _cfg  # noqa: E402

URL = _cfg.ads_base_url(_cfg.load()) + "/execute"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(name: str, args: dict, timeout: float = 300.0) -> dict:
    req = urllib.request.Request(
        URL,
        data=json.dumps({"name": name, "args": args, "timeout": timeout}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
        method="POST")
    with _OPENER.open(req, timeout=timeout + 60) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    tool = sys.argv[1]
    args = json.loads(open(sys.argv[2], encoding="utf-8").read()) if len(sys.argv) > 2 else {}
    tmo = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
    body = call(tool, args, tmo)
    print(json.dumps(body, ensure_ascii=False, indent=1, default=str)[:8000])
