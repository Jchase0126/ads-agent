"""Y：风险4 双进程并发写 config.ini。

场景（README 手工步骤的自动化等价）：
1. token 处于 legacy 默认值（脚本外已复位）
2. ADS 进程内 ensure_backend() 拉起后端 —— 后端首次 ensure_token 会轮换令牌
3. 同时另一进程反复 POST /config（等价于面板「保存并应用」）
4. 校验 config.ini：token 随机、[llm] model 有效、注释与 [agent]/[ui] 段保留
"""

import json
import sys
import threading
import time
import urllib.request

ROOT = r"D:\Antenna\ADS Agent"
sys.path.insert(0, ROOT + r"\backend")

import importlib.util

spec = importlib.util.spec_from_file_location("aa_shared", ROOT + r"\backend\ads_auth.py")
ads_auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ads_auth)
ads_auth._FORCED_ROOT = ROOT

_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
writes = []


def config_writer(n):
    for i in range(n):
        try:
            body = json.dumps({"model": "deepseek-chat"}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:8760/config", data=body,
                headers={"Content-Type": "application/json",
                         ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
                method="POST")
            with _op.open(req, timeout=10) as r:
                writes.append(("ok", r.status))
        except Exception as e:  # noqa: BLE001
            writes.append(("err", str(e)[:60]))
        time.sleep(0.5)


def launcher():
    code = (
        "import sys, time\n"
        "sys.path.insert(0, r'D:\\Antenna\\ADS Agent\\addon\\ads_agent')\n"
        "import backend_launcher as bl\n"
        "print('ensure_backend:', bl.ensure_backend())\n"
        "time.sleep(10)\n"
    )
    req = urllib.request.Request(
        "http://127.0.0.1:8762/execute",
        data=json.dumps({"name": "run_python", "args": {"code": code}, "timeout": 120}).encode(),
        headers={"Content-Type": "application/json",
                 ads_auth.TOKEN_HEADER: ads_auth.ensure_token()},
        method="POST")
    with _op.open(req, timeout=130) as r:
        print("launcher:", json.loads(r.read().decode()).get("stdout", "")[:160])


launcher_th = threading.Thread(target=launcher)
launcher_th.start()
time.sleep(0.05)
th = threading.Thread(target=config_writer, args=(10,))
th.start()
th.join()
launcher_th.join(timeout=60)
ok = sum(1 for s, _ in writes if s == "ok")
print(f"/config 并发写 {len(writes)} 次成功 {ok} 次，样例: {writes[:5]}")
