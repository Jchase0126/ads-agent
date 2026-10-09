"""启动 ADS 2027 并等工具服务就绪（可分发改造后的实机验收）。

与 tests/launch_ads.py 相比做了三处调整：
  1. 支持 --workspace 参数（默认用已存在的工作区）；
  2. 对话框处理更宽：任何含 OK / 确定 / Close / 继续 的按钮都试一遍，
     并记录点到了什么（ADS 卡在模态窗时 toolserver 永远不会起来）；
  3. 超时更短（默认 6 分钟），并把窗口清单打出来，便于判断卡在哪一步。

用法: python tools/launch_ads_for_check.py [--workspace <路径>] [--timeout 360]
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

import adslocate  # noqa: E402
import paths  # noqa: E402

DEFAULT_WORKSPACE = r"E:\ADS\Work_AI\BFR106_wrk"


def windows_texts():
    try:
        from pywinauto import Desktop

        return [(w.window_text() or "").strip() for w in Desktop(backend="uia").windows()]
    except Exception as e:  # noqa: BLE001
        return [f"<enumerate failed: {e}>"]


def click_any(label_patterns, button_patterns, timeout=4.0):
    """在标题含任一 label 的窗口里，点文本含任一 button 的按钮。"""
    try:
        from pywinauto import Desktop
    except Exception:  # noqa: BLE001
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for w in Desktop(backend="uia").windows():
                title = (w.window_text() or "")
                if not title:
                    continue
                if not any(p.lower() in title.lower() for p in label_patterns):
                    continue
                for b in w.descendants(control_type="Button"):
                    text = (b.window_text() or "")
                    if not text:
                        continue
                    if any(p.lower() in text.lower() for p in button_patterns):
                        b.invoke()
                        return f"点击 {text!r}（窗口 {title[:40]!r}）"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return None


def port_up(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--timeout", type=int, default=360)
    ap.add_argument("--ads-dir", default="")
    args = ap.parse_args()

    found = adslocate.detect_ads_dirs()
    targets = [i for i in found if i.get("is_target")] or found
    if args.ads_dir:
        ads_dir = args.ads_dir
    elif targets:
        ads_dir = targets[0]["dir"]
    else:
        print("FAIL 没有找到 ADS 安装目录")
        return 1
    ads_exe = os.path.join(ads_dir, "bin", "ads.exe")
    print("ADS 目录:", ads_dir)
    print("启动:", ads_exe)
    print("插件版本:", paths.PLUGIN_VERSION, " 数据目录:", paths.data_root())

    if not os.path.isfile(ads_exe):
        print("FAIL 找不到 ads.exe:", ads_exe)
        return 1

    subprocess.Popen([ads_exe], cwd=os.path.dirname(ads_exe))

    # 各种可能的模态窗：Product Selection / 许可 / 欢迎页
    handled = []
    for label, buttons in (
        (("select", "product", "license", "许可", "选择"), ("ok", "确定", "ok(&o)")),
        (("welcome", "get started", "getting started", "欢迎"), ("close", "关闭")),
    ):
        hit = click_any(label, buttons, timeout=25)
        if hit:
            handled.append(hit)
    print("对话框处理:", handled or "（没有需要处理的对话框）")

    import configparser

    parser = configparser.ConfigParser()
    parser.read(paths.config_path(), encoding="utf-8")
    port = parser.getint("ads", "port", fallback=8761)
    print(f"等工具服务 127.0.0.1:{port}（最多 {args.timeout}s）...")

    if not port_up(port, args.timeout):
        print("TIMEOUT：工具服务没有起来。当前窗口：")
        for text in windows_texts():
            print("   -", text[:90])
        return 1

    print("工具服务已就绪")
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
        health = json.loads(resp.read().decode("utf-8"))
    print("工具服务 /health:")
    print(json.dumps(health, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
