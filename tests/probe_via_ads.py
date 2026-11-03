"""通过 ADS 端工具服务，在 ADS 进程内部执行只读探测代码。

工作区只在 ADS 进程里是打开的，独立 python 进程里 `de.workspace_is_open()`
为 False，所以想看到活的对象必须走这条路。

令牌取自共享配置（backend/ads_auth.py），不再用写死的默认值 —— 旧的
`ads-agent-local-token` 现在会被拒绝。

用法:
  python tests/probe_via_ads.py tests/probes/A_design.py
  python tests/probe_via_ads.py -c "print(1+1)"
"""

import json
import os
import sys
import urllib.error
import urllib.request

BACKEND = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ads_auth  # noqa: E402

import config as _cfg  # noqa: E402

URL = __import__("config").ads_base_url(_cfg.load()) + "/execute"
# 回环请求绕过 HTTP 代理，否则设了 HTTP_PROXY 的环境会连不上本机服务
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(code: str) -> str:
    req = urllib.request.Request(
        URL,
        data=json.dumps({"name": "run_python", "args": {"code": code}}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            ads_auth.TOKEN_HEADER: ads_auth.ensure_token(),
        },
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return ("ADS 端拒绝了本次请求（回环令牌不匹配）。\n"
                    "重启后端与 ADS；仍不行就 python backend/ads_auth.py --rotate 后再重启两端。")
        return f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
    except urllib.error.URLError as e:
        return (f"无法连接 ADS 端工具服务（{e.reason}）。\n"
                f"确认 ADS 2027 已启动、插件已启用、并打开过一次对话面板。")
    return body.get("stdout", "") or str(body)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] != "-c":
        code = open(sys.argv[1], encoding="utf-8").read()
    else:
        code = sys.argv[2] if len(sys.argv) > 2 else "print('hello')"
    print(run(code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
