"""实机验收：启动 ADS 2027 → 等插件就绪 → 在 ADS 进程内跑只读探针 → 检查面板。

为什么要写成"一个进程干完所有事"：
    之前分两步做（先 tools/launch_ads_for_check.py，再 tests/run_ads_probe.py），
    ADS 被 Popen 起来后，**父进程一退出，进程树就被收掉**，第二步跑过去只看到
    "connection refused"。这里把启动/等待/探测/收尾放在同一条时间线上，
    中途 ADS 一直活着。

用法（用 ADS 自带的 python 跑，保证不依赖外部 Python）:
    <ADS>/tools/python/python.exe tools/ads_acceptance.py [--timeout 300] [--keep]
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, HERE)

import adslocate  # noqa: E402
import paths  # noqa: E402
from launch_ads_for_check import click_any, windows_texts  # noqa: E402

DEFAULT_WORKSPACE = r"E:\ADS\Work_AI\BFR106_wrk"
PROBE_DIR = os.path.join(ROOT, "tests", "probes")
PROBES = ["Z3_dist_paths_in_ads.py", "Z4_panel_in_ads.py"]


def port_up(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(2)
    return False


def http_json(url, body=None, token="", timeout=180):
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Ads-Agent-Token"] = token
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_probe_code(name):
    path = os.path.join(PROBE_DIR, name)
    ns = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns["CODE"]


def find_panel_window(timeout=25.0):
    """在 ADS 主窗口里找停靠的 Agent 面板（UI 层面的旁证）。

    面板是**停靠**进 ADS 主窗口的 QDockWidget，停靠后不再是顶层窗口，所以
    枚举 Desktop 顶层窗口看不到它 —— 要在 ADS 主窗口的子孙控件里找它的标题。
    "面板到底建起来没有"以 Z4 探针（进程内 ``panel._panel``）为准，这里只是
    再从 UI 侧确认它**可见**地待在主窗口里。

    返回 dict；pywinauto 不可用时给 ``available: False``，不当作失败。
    """
    out = {"available": False}
    try:
        from pywinauto import Desktop
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"pywinauto 不可用: {e}"
        return out

    out["available"] = True
    title_key = "ADS Agent"
    deadline = time.time() + timeout
    seen = []
    main_title = None
    while time.time() < deadline:
        try:
            windows = list(Desktop(backend="uia").windows())
        except Exception:  # noqa: BLE001
            windows = []
        seen = [(w.window_text() or "").strip() for w in windows]
        for w in windows:
            t = (w.window_text() or "")
            low = t.lower()
            if "advanced design system" in low and "main" in low:
                main_title = t
                hits = []
                try:
                    for c in w.descendants():
                        text = (c.window_text() or "")
                        if title_key.lower() in text.lower():
                            hits.append({
                                "type": c.element_info.control_type,
                                "text": text[:80],
                            })
                except Exception as e:  # noqa: BLE001
                    hits = [{"error": f"{type(e).__name__}: {e}"}]
                if hits:
                    out["ads_main_window"] = main_title
                    out["dock_controls"] = hits
                    out["found"] = True
                    return out
                out["ads_main_window"] = main_title
                out["dock_controls"] = []
        time.sleep(1.0)

    out["found"] = False
    out["ads_main_window"] = main_title
    out["windows_sample"] = [t[:70] for t in seen if t][:40]
    return out


def port_closed(port, timeout=30.0):
    """等端口**关掉**（与 port_up 相反，用来验证退出清理）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            return True
        time.sleep(0.5)
    return False


def wait_ready(port, timeout, log):
    """等工具服务起来，**同时**处理 ADS 启动过程中冒出来的模态窗。

    踩过的坑：ADS 的 "Product Selection" 对话框要 40~60s 才出现（产品初始化之后），
    只在前 25s 里点一次是点不到的 —— 结果 ADS 一直卡在模态窗上，工具服务永远
    不启动，验收就误报成"插件没加载"。所以这里把"点对话框"和"探端口"放进同一个
    循环，谁先就绪就走谁。
    """
    phases = (
        # (窗口标题里要含的词, 要点的按钮文本)
        (("product selection", "product", "license", "许可", "选择", "bundle"),
         ("ok", "确定", "ok(&o)")),
        (("welcome", "get started", "getting started", "欢迎"), ("close", "关闭")),
        (("recover", "restore", "恢复"), ("ok", "确定", "no", "否", "cancel")),
    )
    deadline = time.time() + timeout
    handled = []
    while time.time() < deadline:
        for labels, buttons in phases:
            hit = click_any(labels, buttons, timeout=2)
            if hit and hit not in handled:
                handled.append(hit)
                log(f"已处理对话框: {hit}")
        if port_up(port, 2):
            return True, handled
    return False, handled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--ads-dir", default="")
    ap.add_argument("--close-ads", action="store_true",
                    help="验收结束后尝试关闭 ADS 主窗口（可能弹保存提示，慎用）")
    ap.add_argument("--json", default="", help="把完整报告另存为该路径（UTF-8 JSON）")
    ap.add_argument("--no-live-tool", action="store_true",
                    help='跳过"经后端真实派发一次只读工具"的检查')
    ap.add_argument("--verify-shutdown", action="store_true",
                    help="跑完后停掉本会话的 toolserver，验证端口释放与登记清理")
    args = ap.parse_args()

    report = {"steps": []}

    # ---- 0. 定位 ADS ----
    found = adslocate.detect_ads_dirs()
    targets = [i for i in found if i.get("is_target")] or found
    ads_dir = args.ads_dir or (targets[0]["dir"] if targets else "")
    ads_exe = os.path.join(ads_dir, "bin", "ads.exe")
    report["ads_dir"] = ads_dir
    report["ads_exe"] = ads_exe
    report["plugin_version"] = paths.PLUGIN_VERSION
    report["data_root"] = paths.data_root()
    report["target_year"] = adslocate.TARGET_YEAR
    report["ads_candidates"] = [
        {"dir": i["dir"], "year": i.get("year"), "is_target": i.get("is_target")}
        for i in found
    ]
    if not os.path.isfile(ads_exe):
        report["fatal"] = f"找不到 ads.exe: {ads_exe}"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1

    # ---- 1. 启动 ADS ----
    subprocess.Popen([ads_exe], cwd=os.path.dirname(ads_exe))

    # ---- 2. 等工具服务（期间持续处理模态窗）----
    import configparser

    parser = configparser.ConfigParser()
    parser.read(paths.config_path(), encoding="utf-8")
    port = parser.getint("ads", "port", fallback=8763)
    token = parser.get("ads", "token", fallback="").strip()
    report["toolserver_port"] = port

    def log(msg):
        print(msg, flush=True)

    ready, handled = wait_ready(port, args.timeout, log)
    report["dialogs"] = handled
    if not ready:
        report["fatal"] = f"工具服务在 {args.timeout}s 内未启动"
        report["windows"] = [t[:90] for t in windows_texts()]
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 1

    base = f"http://127.0.0.1:{port}"
    report["toolserver_health"] = http_json(f"{base}/health", timeout=15)

    # ---- 3. 面板是否自动打开（UI 旁证）----
    report["panel_ui"] = find_panel_window(timeout=25)

    # ---- 4. ADS 进程内跑只读探针 ----
    # 面板与后端是 QTimer 里延迟拉起的，工具服务刚起来时窗口可能还没完全稳定；
    # 给一个短暂的沉降时间，避免把"正在创建"误判成"没创建"。
    time.sleep(3)
    report["probes"] = {}
    for probe in PROBES:
        entry = {}
        try:
            code = load_probe_code(probe)
            res = http_json(
                f"{base}/execute",
                {"name": "run_python", "args": {"code": code}, "timeout": 120},
                token=token,
                timeout=180,
            )
            entry["ok"] = res.get("ok")
            stdout = res.get("stdout", "")
            try:
                entry["data"] = json.loads(stdout)
            except ValueError:
                entry["raw"] = stdout[:4000]
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {e}"
        report["probes"][probe] = entry

    # ---- 5. 后端身份是否与工具服务同源 ----
    try:
        bh = http_json("http://127.0.0.1:8760/health", timeout=10)
        report["backend_health"] = {
            "pid": bh.get("pid"),
            "identity": bh.get("identity"),
        }
        mine = (report["toolserver_health"] or {}).get("identity", {})
        theirs = (bh.get("identity") or {})
        report["same_install_id"] = (
            bool(mine.get("install_id"))
            and mine.get("install_id") == theirs.get("install_id")
        )
    except Exception as e:  # noqa: BLE001
        report["backend_error"] = f"{type(e).__name__}: {e}"

    # ---- 5b. 工具服务的实例登记（多开冲突判定靠它）----
    try:
        rt = paths.runtime_dir()
        regs = sorted(f for f in os.listdir(rt) if f.startswith("toolserver"))
        report["toolserver_instance_files"] = regs
        if regs:
            with open(os.path.join(rt, regs[0]), encoding="utf-8") as fh:
                rec = json.load(fh)
            report["toolserver_instance"] = {
                "kind": rec.get("kind"),
                "port": rec.get("port"),
                "pid": rec.get("pid"),
                "install_id": rec.get("install_id"),
                "pid_matches_health": rec.get("pid")
                == (report["toolserver_health"] or {}).get("identity", {}).get("pid"),
            }
    except Exception as e:  # noqa: BLE001
        report["instance_file_error"] = f"{type(e).__name__}: {e}"

    # ---- 6. 后端侧身份闸门：真派发一次只读工具 ----
    if not args.no_live_tool:
        try:
            import config as backend_config
            import tools as backend_tools

            cfg = backend_config.load()
            url = backend_tools.ads_base_url(cfg)
            verdict = backend_tools.instance.evaluate(
                backend_tools.instance.probe(url), "toolserver")
            report["live_tool"] = {
                "url": url,
                "verdict_usable": verdict.get("usable"),
                "verdict_reason": verdict.get("reason"),
                "verdict_detail": verdict.get("detail"),
            }
            res = backend_tools.call(cfg, "get_workspace_info", {})
            # 不同工具返回的结构不一样（run_python 是 {stdout, ok}，查询类直接
            # 返回业务字段），所以整包记下来，别假定某个键一定在。
            report["live_tool"]["result_has_error"] = "error" in res
            report["live_tool"]["result_head"] = json.dumps(
                res, ensure_ascii=False, default=str)[:500]
        except Exception as e:  # noqa: BLE001
            report.setdefault("live_tool", {})
            report["live_tool"]["error"] = f"{type(e).__name__}: {e}"

    # ---- 7. 退出清理（可选；会把这个 ADS 会话的 toolserver 停掉）----
    if args.verify_shutdown:
        entry = {}
        rt = paths.runtime_dir()
        try:
            entry["instance_before"] = sorted(
                f for f in os.listdir(rt) if f.startswith("toolserver"))
        except Exception as e:  # noqa: BLE001
            entry["instance_before_error"] = f"{type(e).__name__}: {e}"
        # 用 QTimer 把 shutdown 排到"这次 HTTP 回包之后"，否则请求拿不到结果
        code = (
            "from PySide6.QtCore import QTimer\n"
            "import toolserver\n"
            "QTimer.singleShot(300, toolserver.shutdown)\n"
            "print('shutdown 已排程（300ms 后执行）')\n"
        )
        try:
            res = http_json(
                f"{base}/execute",
                {"name": "run_python", "args": {"code": code}, "timeout": 60},
                token=token,
                timeout=90,
            )
            entry["scheduled"] = res.get("ok")
            entry["stdout"] = str(res.get("stdout", ""))[:200]
        except Exception as e:  # noqa: BLE001
            entry["schedule_error"] = f"{type(e).__name__}: {e}"
        entry["port_released"] = port_closed(port, timeout=30)
        try:
            entry["instance_after"] = sorted(
                f for f in os.listdir(rt) if f.startswith("toolserver"))
        except Exception as e:  # noqa: BLE001
            entry["instance_after_error"] = f"{type(e).__name__}: {e}"
        entry["instance_file_removed"] = not entry.get("instance_after")
        report["shutdown_cleanup"] = entry

    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"报告已写入: {os.path.abspath(args.json)}")

    # 故意**不**自动关 ADS：关闭主窗口可能弹出"是否保存工作区"提示，
    # 盲点一个按钮就等于替用户丢弃未保存的设计 —— 这类不可恢复操作不代替
    # 用户决定。需要关就由用户自己关，或者显式加 --close-ads。
    if args.close_ads:
        try:
            from pywinauto import Desktop

            for w in Desktop(backend="uia").windows():
                t = (w.window_text() or "").lower()
                if "advanced design system" in t and "main" in t:
                    print("正在关闭 ADS 主窗口（若弹出保存提示请手工确认）...")
                    w.close()
                    break
        except Exception as e:  # noqa: BLE001
            print(f"关闭 ADS 失败（请手工关闭）: {type(e).__name__}: {e}")
    else:
        print("ADS 保持运行，可直接看面板；不需要时请手工关闭。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
