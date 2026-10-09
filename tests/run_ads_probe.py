"""通过 toolserver /execute 在 ADS 进程内执行只读探测脚本。

用法:
    python tests/run_ads_probe.py [探针文件名] [--timeout 120]

默认跑 ``Z3_dist_paths_in_ads.py``（可分发改造的 ADS 进程内核实）。
探针脚本约定：暴露一个模块级 ``CODE`` 字符串（其余内容不执行）。

注意：必须先在 ADS 里把插件加载起来（工具服务在 config.ini 的 [ads] port 上
监听），否则这里会直接报连不上 —— 本脚本刻意**不**代启动 ADS。
"""

import argparse
import configparser
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config.ini")
PROBES = os.path.join(ROOT, "tests", "probes")
DEFAULT_PROBE = "Z3_dist_paths_in_ads.py"


def _cfg():
    p = configparser.ConfigParser()
    p.read(CFG, encoding="utf-8")
    return p


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_code(probe):
    """读出探针脚本里的 CODE 字符串。"""
    name = probe if probe.lower().endswith(".py") else f"{probe}.py"
    src = os.path.join(PROBES, name)
    if not os.path.isfile(src):
        raise SystemExit(f"找不到探针: {src}")
    ns = {}
    exec(compile(open(src, encoding="utf-8").read(), src, "exec"), ns)
    if "CODE" not in ns:
        raise SystemExit(f"{src} 没有定义 CODE")
    return src, ns["CODE"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("probe", nargs="?", default=DEFAULT_PROBE)
    ap.add_argument("--timeout", type=int, default=180, help="HTTP 等待秒数")
    ap.add_argument("--tool-timeout", type=int, default=120, help="ADS 侧执行上限秒数")
    args = ap.parse_args()

    src, code = load_code(args.probe)
    print(f"探针: {src}")

    p = _cfg()
    host = p.get("ads", "host", fallback="127.0.0.1")
    port = p.get("ads", "port", fallback="8763")
    token = p.get("ads", "token", fallback="").strip()
    if not token:
        raise SystemExit("config.ini 没有 [ads] token")

    url = f"http://{host}:{port}/execute"
    print(f"目标: {url}")

    body = json.dumps(
        {"name": "run_python", "args": {"code": code}, "timeout": args.tool_timeout}
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Ads-Agent-Token": token},
        method="POST",
    )
    try:
        with _opener().open(req, timeout=args.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"连不上工具服务（ADS 是否已启动并加载插件？）: {e}")
        return 1

    # toolserver 的 /execute 直接把 run_python 的结果返回：{"stdout":..., "ok":...}
    print(json.dumps(result, ensure_ascii=False, indent=1)[:20000])
    if isinstance(result, dict) and result.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
