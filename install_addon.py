"""Register / unregister the ADS Agent addon in the ADS 2027 eesof_addons.xml.

Usage:
  python install_addon.py            # install (idempotent)
  python install_addon.py --remove   # uninstall
  python install_addon.py --status   # show registration state
"""

import argparse
import os
import re
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ADDON_INIT = os.path.join(PROJECT_ROOT, "addon", "ads_agent", "__init__.py")
ADDON_NAME = "ADS Agent"

DEFAULT_HPEESOF_DIR = r"E:\ADS\Programfile\ADS2027"
BACKUP_SUFFIX = ".bak_adsagent"


def find_xml() -> str:
    hpeesof = os.environ.get("HPEESOF_DIR", DEFAULT_HPEESOF_DIR)
    for candidate in (
        os.path.join(hpeesof, "config", "eesof_addons.xml"),
        r"C:\Program Files\Keysight\ADS2027\config\eesof_addons.xml",
    ):
        if os.path.exists(candidate):
            return candidate
    raise SystemExit(
        f"找不到 eesof_addons.xml。请设置 HPEESOF_DIR 环境变量指向 ADS 2027 安装目录\n"
        f"（当前尝试：{hpeesof}）"
    )


def read_entries(xml_path: str):
    text = open(xml_path, encoding="utf-8", errors="replace").read()
    entries = {}
    for m in re.finditer(r'<Addon\s+Name="([^"]+)"\s+FilePath="([^"]+)"[^>]*>', text):
        entries[m.group(1)] = (m.group(2), m)
    return text, entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    xml_path = find_xml()
    text, entries = read_entries(xml_path)

    if args.status:
        if ADDON_NAME in entries:
            path, _ = entries[ADDON_NAME]
            print(f"已注册: {ADDON_NAME} -> {path}")
        else:
            print("未注册")
        print(f"配置文件: {xml_path}")
        return

    backup = xml_path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        shutil.copy2(xml_path, backup)
        print(f"已备份原配置 -> {backup}")

    if args.remove:
        if ADDON_NAME not in entries:
            print("未注册，无需卸载")
            return
        _, match = entries[ADDON_NAME]
        text = text.replace(match.group(0), "")
        open(xml_path, "w", encoding="utf-8").write(text)
        print(f"已卸载 {ADDON_NAME}。重启 ADS 生效。")
        return

    if ADDON_NAME in entries:
        path, _ = entries[ADDON_NAME]
        print(f"已注册过: {path}")
        if os.path.normpath(path) != os.path.normpath(ADDON_INIT):
            # stale path from an old location — refresh it
            old_entry = f'<Addon Name="{ADDON_NAME}" FilePath="{path}"'
            new_entry = f'<Addon Name="{ADDON_NAME}" FilePath="{ADDON_INIT}"'
            text = text.replace(old_entry, new_entry)
            open(xml_path, "w", encoding="utf-8").write(text)
            print(f"路径已更新 -> {ADDON_INIT}")
        return

    entry = f'    <Addon Name="{ADDON_NAME}" FilePath="{ADDON_INIT}" Enabled="1" />\n'
    if "</EESof_Addons>" in text:
        text = text.replace("</EESof_Addons>", entry + "</EESof_Addons>")
    else:
        text += f"\n<EESof_Addons>\n{entry}</EESof_Addons>\n"
    open(xml_path, "w", encoding="utf-8").write(text)
    print(f"已注册 {ADDON_NAME} -> {ADDON_INIT}")
    print("重启 ADS 后生效：菜单 Tools > ADS Agent > 打开对话面板…")
    if not os.path.exists(os.path.join(PROJECT_ROOT, "config.ini")):
        print("提示：请先复制 config.example.ini 为 config.ini 并填写 LLM API Key")


if __name__ == "__main__":
    main()
