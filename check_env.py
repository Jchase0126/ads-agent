"""环境自检 —— 安装 / 升级 / 排障的第一步。

  python check_env.py           # 完整自检（含回归测试）
  python check_env.py --quick   # 跳过 ADS 自带解释器的深度检查
  python check_env.py --no-tests  # 不跑回归测试（快）

它按这个顺序回答"这台机器能不能跑起来、哪里不对"：

1. 运行环境 —— 用哪个解释器（**优先 ADS 自带**）、ADS 装在哪
2. 路径 —— 程序目录 / 数据目录是不是分开了（卸载安全的前提）
3. 配置与令牌 —— 配置文件在哪、令牌是不是独立随机值、两端是否一致
4. 注册 —— ADS 里有没有这一条、指的路径对不对、其它插件有没有被碰过
5. 实例 —— 端口上是不是本插件的服务（**身份校验，不看 200**）
6. 回归测试 —— 不需要 ADS 的那些
"""

import configparser
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "backend"))

OK, WARN, FAIL = "[OK]", "[WARN]", "[FAIL]"
results = []
QUICK = "--quick" in sys.argv


def check(name, fn):
    try:
        detail = fn()
        results.append((OK, name, detail or ""))
    except Exception as e:  # noqa: BLE001
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))


def warn_check(name, fn):
    try:
        detail = fn()
        results.append((OK, name, detail or ""))
    except Exception as e:  # noqa: BLE001
        results.append((WARN, name, f"{type(e).__name__}: {e}"))


def _loopback_get(url, timeout=3):
    """回环请求绕过 HTTP 代理 —— 否则设了 HTTP_PROXY 的环境会误报"服务未启动"。"""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


try:
    import adslocate  # noqa: E402
    import paths  # noqa: E402
except Exception as e:  # noqa: BLE001
    print(f"[FAIL] 无法加载共享模块 backend/paths.py / adslocate.py：{e}")
    print("       请在**解压出来的完整安装包**里运行本脚本。")
    raise SystemExit(1)


# --------------------------------------------------------------------------
# 1. 运行环境
# --------------------------------------------------------------------------

def interpreter_check():
    chosen = adslocate.choose_interpreter()
    if not chosen.get("exe"):
        raise RuntimeError(
            "没有找到任何可用的 Python 解释器。正常情况下 ADS 自带 "
            "<ADS>\\tools\\python\\python.exe —— 请检查 ADS 是否安装完整。"
        )
    note = "ADS 自带" if chosen.get("ads") else "非 ADS 自带"
    return f"{chosen['exe']}（{chosen.get('source') or note}）"


check("可用解释器（优先 ADS 自带）", interpreter_check)


def ads_dir_check():
    found = adslocate.detect_ads_dirs(scan=not QUICK)
    targets = [i for i in found if i.get("info", {}).get("is_target")]
    if not found:
        raise RuntimeError(
            "没有探测到 ADS 安装。可用 python install_addon.py --ads-dir <目录> 指定。"
        )
    best = targets[0] if targets else found[0]
    if not targets:
        raise RuntimeError(
            f"只找到 {best['dir']}（年份 {best['year'] or '?'}），没有找到 ADS "
            f"{adslocate.TARGET_YEAR}。请用 install_addon.py --ads-dir 显式指定。"
        )
    return (f"{best['dir']}  来源={best['source']}  自带 Python="
            f"{'有' if best['info']['python'] else '**没有**'}")


check(f"ADS {adslocate.TARGET_YEAR} 安装目录", ads_dir_check)


# --------------------------------------------------------------------------
# 2. 路径：程序目录 vs 数据目录
# --------------------------------------------------------------------------

def path_check():
    app_root = paths.app_root()
    data_root = paths.data_root()
    if os.path.normcase(app_root) == os.path.normcase(data_root):
        raise RuntimeError("程序目录与数据目录相同 —— 卸载会连带删掉用户数据")
    missing = [d for d in (paths.logs_dir(), paths.design_jobs_dir(),
                           paths.runtime_dir()) if not os.path.isdir(d)]
    if missing:
        raise RuntimeError(f"数据目录下缺子目录（跑一次 install_addon.py 即可）：{missing}")
    return f"程序={app_root}  数据={data_root}"


check("程序/数据目录已分离", path_check)


def install_state_check():
    state = paths.load_install_state()
    if not state:
        raise RuntimeError("没有安装记录 —— 请先运行 install_addon.py")
    return (f"插件版本={state.get('plugin_version') or '?'}  "
            f"数据版本={state.get('data_version') or '?'}  "
            f"安装ID={state.get('install_id') or '?'}")


check("安装版本记录", install_state_check)


# --------------------------------------------------------------------------
# 3. 配置与令牌
# --------------------------------------------------------------------------

def cfg_check():
    path = paths.config_path()
    if not os.path.exists(path):
        raise RuntimeError(
            f"配置文件不存在：{path} —— 请先运行 install_addon.py（首启会从干净模板生成）。"
        )
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    key = parser.get("llm", "api_key", fallback="").strip()
    if not key and not os.environ.get("ADS_AGENT_API_KEY"):
        raise RuntimeError(
            f"{path} 的 [llm] api_key 未填写，且未设置 ADS_AGENT_API_KEY。"
            f"推荐在面板 ⚙设置 里填（会自动写回这个文件）。"
        )
    return f"{path}  model={parser.get('llm', 'model', fallback='?')}"


check("配置文件与 LLM Key", cfg_check)


def backend_check():
    import py_compile

    for f in ("ads_auth.py", "adslog.py", "config.py", "llm.py", "tools.py",
              "agent.py", "server.py", "paths.py", "instance.py", "adslocate.py"):
        py_compile.compile(os.path.join(HERE, "backend", f), doraise=True)
    return "backend/*.py 语法 OK"


check("后端代码语法", backend_check)


def addon_check():
    import py_compile

    for f in ("__init__.py", "ads_ops.py", "toolserver.py", "panel.py",
              "authbridge.py", "uiscale.py", "backend_launcher.py",
              "pathbridge.py", "registration.py"):
        target = os.path.join(HERE, "addon", "ads_agent", f)
        if not os.path.isfile(target):
            raise RuntimeError(f"缺少插件文件：{target}（安装包不完整？）")
        py_compile.compile(target, doraise=True)
    return "addon/ads_agent/*.py 语法 OK"


check("插件代码语法", addon_check)


def token_check():
    sys.path.insert(0, os.path.join(HERE, "addon", "ads_agent"))
    import ads_auth  # noqa: E402
    import authbridge  # noqa: E402

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


def lock_check():
    import ads_auth  # noqa: E402

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


# --------------------------------------------------------------------------
# 4. 注册
# --------------------------------------------------------------------------

def registration_check():
    import install_addon  # noqa: E402

    state = paths.load_install_state()
    candidates = []
    if state.get("ads_dir"):
        candidates.append(state["ads_dir"])
    env_dir = (os.environ.get("HPEESOF_DIR") or "").strip()
    if env_dir:
        candidates.append(env_dir)
    candidates += [i["dir"] for i in adslocate.detect_ads_dirs(scan=not QUICK)]

    seen, registered = set(), None
    for ads_dir in candidates:
        norm = os.path.normcase(os.path.normpath(ads_dir))
        if norm in seen:
            continue
        seen.add(norm)
        xml = os.path.join(ads_dir, "config", "eesof_addons.xml")
        if not os.path.isfile(xml):
            continue
        _t, entries, err = install_addon.parse_entries(xml)
        if err:
            continue
        record = entries.get(install_addon.ADDON_NAME)
        if not record:
            continue
        registered = (ads_dir, record)
        expected = os.path.join(paths.app_root(), install_addon.PLUGIN_ENTRY)
        if install_addon._same_path(record[0], expected):
            return f"已注册到 {ads_dir}，路径与本程序目录一致"
        raise RuntimeError(
            f"已注册到 {ads_dir}，但路径不一致：注册的是 {record[0]}，"
            f"而本程序目录是 {expected}。在本程序目录重新运行一次 "
            f"install_addon.py 即可（它只改这一条）。"
        )
    if not registered:
        raise RuntimeError("ADS 里没有注册本插件 —— 请运行 install_addon.py 并重启 ADS")
    return ""


check("ADS 注册状态", registration_check)


def other_addons_check():
    """自己能写，也要确认没把别人的写坏 —— 每次自检顺手核一遍。"""
    import install_addon  # noqa: E402

    state = paths.load_install_state()
    ads_dir = state.get("ads_dir") or (os.environ.get("HPEESOF_DIR") or "")
    if not ads_dir:
        raise RuntimeError("没有安装记录也没有 HPEESOF_DIR，无法核对")
    xml = os.path.join(ads_dir, "config", "eesof_addons.xml")
    _t, entries, err = install_addon.parse_entries(xml)
    if err:
        raise RuntimeError(err)
    official = [n for n in entries if n != install_addon.ADDON_NAME]
    return f"注册文件里另有 {len(official)} 个插件条目，均可正常解析"


warn_check("其它 ADS 插件完好", other_addons_check)


# --------------------------------------------------------------------------
# 5. 服务实例（身份校验，不看 200）
# --------------------------------------------------------------------------

def _cfg():
    parser = configparser.ConfigParser()
    parser.read(paths.config_path(), encoding="utf-8")
    return parser


def _service_check(kind: str, kind_cn: str, section: str, port_fallback: int):
    import instance  # noqa: E402

    parser = _cfg()
    host = parser.get(section, "host", fallback="127.0.0.1")
    port = parser.getint(section, "port", fallback=port_fallback)
    probed = instance.probe(f"http://{host}:{port}", timeout=2.0)
    verdict = instance.evaluate(probed, kind)
    if not probed["reachable"]:
        hint = ("面板会用 ADS 自带 Python 自动拉起它" if kind == "backend"
                else "打开一次对话面板，或菜单 Tools ▸ ADS Agent ▸ 启动/重启工具服务")
        raise RuntimeError(f"{host}:{port} 没有响应 —— {hint}")
    if not verdict["usable"]:
        raise RuntimeError(f"{verdict['detail']}（{verdict['reason']}）")
    ident = (probed.get("payload") or {}).get("identity") or {}
    return (f"{kind_cn} {host}:{port}  pid={ident.get('pid') or '?'}  "
            f"v{ident.get('plugin_version') or '?'}  身份校验通过")


warn_check("后端实例（身份校验）",
           lambda: _service_check("backend", "后端", "backend", 8760))
warn_check("ADS 工具服务实例（身份校验）",
           lambda: _service_check("toolserver", "工具服务", "ads", 8761))


def multi_instance_check():
    import instance  # noqa: E402

    instance.cleanup_stale_instances()
    port = _cfg().getint("backend", "port", fallback=8760)
    clashes = instance.same_install_conflicts("backend", port)
    foreigners = instance.foreign_install_instances("backend")
    notes = []
    if clashes:
        notes.append("同一安装的重复后端：" +
                     "、".join(f"pid={c.get('pid')} 端口={c.get('port')}" for c in clashes))
    if foreigners:
        notes.append("另一个安装的后端：" +
                     "、".join(f"pid={f.get('pid')}" for f in foreigners))
    if notes:
        raise RuntimeError("；".join(notes) +
                           " —— 多个后端会各持一份内存配置，界面上的设置会互相覆盖")
    return "没有检测到重复实例"


warn_check("后端多开检测", multi_instance_check)


# --------------------------------------------------------------------------
# 6. 回归测试
# --------------------------------------------------------------------------

def regression_tests():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "tests", "run_tests.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    summary = next((line for line in reversed(output.splitlines())
                    if line.startswith("结果:")), "未返回测试汇总")
    if proc.returncode != 0:
        tail = " / ".join(output.strip().splitlines()[-8:])
        raise RuntimeError(f"回归测试未通过：{tail}")
    return summary


if "--no-tests" not in sys.argv:
    check("回归测试（无需 ADS）", regression_tests)


# --------------------------------------------------------------------------
# ADS 侧深度检查
# --------------------------------------------------------------------------

if not QUICK:
    def ads_python_check():
        chosen = adslocate.choose_interpreter(verify=True)
        if not chosen.get("exe"):
            raise RuntimeError("找不到任何可用的解释器")
        return (f"{chosen['exe']}  {chosen.get('version') or '（版本未知）'}"
                f"{'（ADS 自带）' if chosen.get('ads') else ''}")

    check("ADS 自带 Python", ads_python_check)

    def ads_imports():
        chosen = adslocate.choose_interpreter()
        code = ("import keysight.ads.de, keysight.ads.dataset; "
                "from keysight.edatoolbox import ads; print('基础模块 ok')")
        proc = subprocess.run(
            [chosen["exe"], "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
            env=dict(os.environ),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-300:]
            # PySide6 只在 ADS 自己的进程里有，外部解释器缺它是正常的
            if "PySide6" in tail:
                return "基础模块可用（PySide6 仅随 ADS 进程提供，非 ADS 解释器缺它属正常）"
            raise RuntimeError(tail)
        return (proc.stdout or "").strip()

    warn_check("ADS 端模块可用性", ads_imports)


# report
print()
for mark, name, detail in results:
    print(f"{mark} {name}" + (f"  — {detail}" if detail else ""))
bad = sum(1 for m, *_ in results if m == FAIL)
print()
if bad:
    print(f"有 {bad} 项未通过 —— 按上面的提示处理后再看 ADS 面板。")
else:
    print("核心项全部 [OK]；[WARN] 通常是「服务还没起来」，按提示处理即可。")
print("用户数据位置：" + paths.data_root())
sys.exit(1 if bad else 0)
