"""Environment self-check for ADS Agent.

  python check_env.py          # full check (may take a few seconds)
  python check_env.py --quick  # skip ADS-side imports
"""

import configparser
import json
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OK, WARN, FAIL = "[OK]", "[WARN]", "[FAIL]"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((OK, name, detail or ""))
    except Exception as e:
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))


def warn_check(name, fn):
    try:
        detail = fn()
        results.append((OK, name, detail or ""))
    except Exception as e:
        results.append((WARN, name, f"{type(e).__name__}: {e}"))


HPEESOF = os.environ.get("HPEESOF_DIR", r"E:\ADS\Programfile\ADS2027")
ADS_PYTHON = os.path.join(HPEESOF, "tools", "python", "python.exe")

# 1. config
def cfg_check():
    path = os.path.join(PROJECT_ROOT, "config.ini")
    if not os.path.exists(path):
        raise RuntimeError("config.ini 不存在（可从 config.example.ini 复制）")
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    key = parser.get("llm", "api_key", fallback="").strip()
    if not key and not os.environ.get("ADS_AGENT_API_KEY"):
        raise RuntimeError("config.ini 的 [llm] api_key 未填写，且未设置 ADS_AGENT_API_KEY")
    return f"model={parser.get('llm', 'model', fallback='?')} base={parser.get('llm', 'base_url', fallback='?')}"


check("配置文件 config.ini + LLM Key", cfg_check)

# 2. backend modules compile
def backend_check():
    """编译**整个** backend/ 目录，而不是一份硬编码的文件名清单。

    硬编码清单有个隐蔽的失效方式：新加的模块（比如模型包层的
    model_store.py / model_tools.py）如果忘了登记进来，环境自检照样
    报「语法 OK」，而这个模块在发布包里是坏的 —— 自检通过了，问题却要
    等到用户上传压缩包时才暴露。扫目录让新增文件自动进入检查范围。
    """
    import py_compile

    backend_dir = os.path.join(PROJECT_ROOT, "backend")
    files = sorted(f for f in os.listdir(backend_dir) if f.endswith(".py"))
    if not files:
        raise RuntimeError("backend/ 下没有 .py 文件，路径不对？")
    for f in files:
        py_compile.compile(os.path.join(backend_dir, f), doraise=True)
    return f"backend/*.py 语法 OK（{len(files)} 个模块）"


check("后端代码语法", backend_check)

# 3. addon modules compile (with system python; PySide6 import deferred)
def addon_check():
    """同 backend_check：扫目录，新模块自动纳入（model_ops 等）。"""
    import py_compile

    addon_dir = os.path.join(PROJECT_ROOT, "addon", "ads_agent")
    files = sorted(f for f in os.listdir(addon_dir) if f.endswith(".py"))
    if not files:
        raise RuntimeError("addon/ads_agent/ 下没有 .py 文件，路径不对？")
    for f in files:
        py_compile.compile(os.path.join(addon_dir, f), doraise=True)
    return f"addon/ads_agent/*.py 语法 OK（{len(files)} 个模块）"


check("插件代码语法", addon_check)

# 3b. 回环令牌：两端必须指向同一个配置文件，且不能停在公开默认值
def token_check():
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "addon", "ads_agent"))
    import ads_auth
    import authbridge

    backend_path = os.path.normcase(ads_auth.config_path())
    addon_path = os.path.normcase(authbridge.auth().config_path())
    if backend_path != addon_path:
        raise RuntimeError(f"两端读的不是同一个配置文件：{backend_path} != {addon_path}")

    info = ads_auth.status()
    if not info["configured"]:
        raise RuntimeError("未配置 [ads] token —— 运行 python backend/ads_auth.py 生成")
    if info["legacy"]:
        raise RuntimeError("仍是公开默认令牌 ads-agent-local-token —— 运行 "
                           "python backend/ads_auth.py --rotate 后重启后端与 ADS")
    if info["weak"]:
        return f"已配置但偏短，建议 --rotate  指纹={info['hint']}"
    return f"已配置，两端一致  指纹={info['hint']}"


check("回环令牌状态（后端 / 插件共用）", token_check)

# 3b-2. 配置写入锁：残留锁会让保存变慢（超时后才会退化写入）
def lock_check():
    import time

    sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))
    import ads_auth

    lock = ads_auth.config_lock()
    if not os.path.exists(lock.path):
        return "无残留锁文件"
    age = time.time() - os.path.getmtime(lock.path)
    if age > ads_auth._LOCK_STALE:
        raise RuntimeError(
            f"残留锁文件 {os.path.basename(lock.path)}（{age:.0f}s 未释放）——"
            f"下次写入会自动清理，也可直接删除"
        )
    return f"有锁文件（{age:.1f}s 前创建，可能正在写入）"


warn_check("配置写入锁状态", lock_check)

# 3c. 无需 ADS 的回归测试（鉴权 / 令牌迁移 / 会话隔离）
def regression_tests():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "tests", "run_tests.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    summary = next((line for line in reversed(output.splitlines())
                    if line.startswith("结果:")), "未返回测试汇总")
    if proc.returncode != 0:
        tail = " / ".join(output.strip().splitlines()[-8:])
        raise RuntimeError(f"回归测试未通过：{tail}")
    return summary


check("回归测试（无需 ADS）", regression_tests)

# 4. backend reachable
def _loopback_get(url, timeout=3):
    """回环请求绕过 HTTP 代理 —— 否则设了 HTTP_PROXY 的环境会误报"服务未启动"。"""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def backend_alive():
    parser = configparser.ConfigParser()
    parser.read(os.path.join(PROJECT_ROOT, "config.ini"), encoding="utf-8")
    host = parser.get("backend", "host", fallback="127.0.0.1")
    port = parser.get("backend", "port", fallback="8760")
    data = _loopback_get(f"http://{host}:{port}/health")
    if not data.get("api_key_configured"):
        raise RuntimeError("后端在运行但 API Key 未配置")
    return f"{data['model']} @ {host}:{port}"


warn_check("后端是否已启动", backend_alive)

# 5. ADS toolserver reachable
def ads_alive():
    parser = configparser.ConfigParser()
    parser.read(os.path.join(PROJECT_ROOT, "config.ini"), encoding="utf-8")
    host = parser.get("ads", "host", fallback="127.0.0.1")
    port = parser.get("ads", "port", fallback="8761")
    data = _loopback_get(f"http://{host}:{port}/health")
    busy = data.get("busy")
    return f"{host}:{port}  仿真后台线程={data.get('sim_off_main_thread')}" + (
        f"  正在执行 {busy}" if busy else "")


warn_check("ADS 工具服务是否已启动（需 ADS 已开且启用插件）", ads_alive)

# 6. ADS-side deep check with bundled python
if "--quick" not in sys.argv:
    def ads_python():
        if not os.path.exists(ADS_PYTHON):
            raise RuntimeError(f"未找到 {ADS_PYTHON}")
        return ADS_PYTHON

    check("ADS 捆绑 Python 存在", ads_python)

    def ads_imports():
        code = (
            "import keysight.ads.de, keysight.ads.dataset, PySide6; "
            "from keysight.edatoolbox import ads; print('imports ok')"
        )
        env = dict(os.environ, HPEESOF_DIR=HPEESOF)
        proc = subprocess.run(
            [ADS_PYTHON, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, env=env
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-300:])
        return proc.stdout.strip()

    check("ADS 端关键模块导入（de/dataset/edatoolbox/PySide6）", ads_imports)

    def addon_registered():
        xml = os.path.join(HPEESOF, "config", "eesof_addons.xml")
        if not os.path.exists(xml):
            raise RuntimeError(f"未找到 {xml}")
        text = open(xml, encoding="utf-8", errors="replace").read()
        if 'Name="ADS Agent"' in text:
            return "已在 eesof_addons.xml 注册"
        raise RuntimeError("尚未注册，请运行 install_addon.bat")

    warn_check("插件注册状态", addon_registered)

# report
print()
for mark, name, detail in results:
    print(f"{mark} {name}" + (f"  — {detail}" if detail else ""))
bad = sum(1 for m, *_ in results if m == FAIL)
print()
print("核心项全部 [OK] 即可按 README 步骤使用；[WARN] 项请查看具体提示。")
sys.exit(1 if bad else 0)
