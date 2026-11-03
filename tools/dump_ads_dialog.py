"""诊断：启动 ADS 并把 "Product Selection" 对话框的控件树 dump 出来。

只在需要搞清楚"ADS 卡在哪个模态窗"时用；不改用户数据。
用法: <python-with-pywinauto> tools/dump_ads_dialog.py [--wait 60]
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import adslocate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=60)
    args = ap.parse_args()

    found = adslocate.detect_ads_dirs()
    targets = [i for i in found if i.get("is_target")] or found
    ads_dir = targets[0]["dir"]
    exe = os.path.join(ads_dir, "bin", "ads.exe")
    print("启动:", exe)
    subprocess.Popen([exe], cwd=os.path.dirname(exe))

    from pywinauto import Desktop

    deadline = time.time() + args.wait
    dumped = False
    while time.time() < deadline and not dumped:
        time.sleep(3)
        for w in Desktop(backend="uia").windows():
            title = (w.window_text() or "")
            low = title.lower()
            if not title or not any(k in low for k in
                                    ("product", "select", "license", "许可", "选择")):
                continue
            print(f"\n=== 窗口 {title!r}  class={w.class_name()} ===")
            try:
                w.print_control_identifiers(depth=3)
            except Exception as e:  # noqa: BLE001
                print("print_control_identifiers 失败:", e)
            # 简版清单：所有按钮/单选项的文本
            items = []
            try:
                for c in w.descendants():
                    info = c.element_info
                    ctype = info.control_type
                    if ctype in ("Button", "RadioButton", "CheckBox", "ComboBox",
                                 "List", "ListItem", "Edit", "Text", "TabItem"):
                        items.append({
                            "type": ctype,
                            "text": (c.window_text() or "")[:80],
                            "auto_id": info.automation_id,
                            "enabled": bool(c.is_enabled()),
                            "visible": bool(c.is_visible()),
                        })
            except Exception as e:  # noqa: BLE001
                print("枚举子控件失败:", e)
            print(json.dumps(items, ensure_ascii=False, indent=1)[:6000])
            dumped = True
            break

    if not dumped:
        print("没有找到待处理的对话框；当前窗口：")
        for w in Desktop(backend="uia").windows():
            t = (w.window_text() or "").strip()
            if t:
                print("   -", t[:90])
    return 0


if __name__ == "__main__":
    sys.exit(main())
