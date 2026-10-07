"""定位 ADS 安装目录与可用的解释器 —— 安装器、启动器、自检共用。

为什么要自己找
--------------
改造前安装脚本里写死了作者机器上 ADS 的路径与解释器的路径 —— 换台机器就废，
而且要求用户另外装某个 Python 发行版（ADS 自带就有 Python，完全不必）。

现在的顺序（先准后广，命中一个可信的就用）：
    1. ``--ads-dir`` / ``HPEESOF_DIR`` 显式指定
    2. 记得的上一次安装位置（``install_state.json``）
    3. Windows 卸载表（``winreg``，含 Keysight / Agilent 的历史键名）
    4. 常见安装盘符的有限深度扫描
永远返回"候选 + 来源"，由调用方决定要不要让用户在候选里挑，不替用户闷头选。

解释器同理：**首选 ADS 自带 python** —— 用户机器上有没有装 Python 都能跑，
这也是"全程不要求用户另装 Python/PySide6"的前提
（PySide6 与 keysight.* 都只在 ADS 自带解释器里有）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

#: 目录必须同时有这些才算一个像样的 ADS 安装
_REQUIRED = ("bin", "tools")
#: 随便给的目录也能被认出来的强特征之一（缺一不可时至少有一个）
_SIGNATURE = ("config", "circuit", "tools")

ADS_DIR_ENV = "HPEESOF_DIR"

#: 本插件面向的 ADS 版本。其它年份不是不能用，但要明确提示（API 契约
#: ``setup_addon`` / ``generate_menu`` 早在 2024 版就有了，跨版本并不必然坏，
#: 不过只有 2027 是实测过的）。
TARGET_YEAR = "2027"
_YEAR_RE = re.compile(r"(20\d{2})")


def detect_year(install_dir: str) -> str:
    """从目录名 / buildInfo.xml 推断 ADS 年份；推断不出返回空串。"""
    name = os.path.basename(os.path.normpath(install_dir or ""))
    m = _YEAR_RE.search(name)
    if m:
        return m.group(1)
    for candidate in ("buildInfo.xml", os.path.join("config", "buildInfo.xml")):
        path = os.path.join(install_dir or "", candidate)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                m = _YEAR_RE.search(f.read())
        except OSError:
            continue
        if m:
            return m.group(1)
    return ""


def validate_ads_dir(path: str) -> dict:
    """判断一个目录是不是可用的 ADS 2027 安装根目录。"""
    p = (path or "").strip().strip('"')
    info = {
        "dir": p,
        "valid": False,
        "reason": "",
        "python": "",
        "addons_xml": "",
        "addons_xml_exists": False,
        "writable_tree": False,
        "year": "",
        "is_target": False,
    }
    if not p:
        info["reason"] = "未指定目录"
        return info
    if not os.path.isdir(p):
        info["reason"] = "目录不存在"
        return info

    missing = [d for d in _REQUIRED if not os.path.isdir(os.path.join(p, d))]
    sig = sum(1 for d in _SIGNATURE if os.path.isdir(os.path.join(p, d)))
    if missing or sig == 0:
        info["reason"] = f"不像 ADS 安装目录（缺少 {', '.join(missing) or '特征目录'}）"
        return info

    py = os.path.join(p, "tools", "python", "python.exe")
    if os.name != "nt":
        py = os.path.join(p, "tools", "python", "bin", "python3")
    info["python"] = py if os.path.isfile(py) else ""
    info["addons_xml"] = os.path.join(p, "config", "eesof_addons.xml")
    info["addons_xml_exists"] = os.path.isfile(info["addons_xml"])
    try:
        info["writable_tree"] = os.access(p, os.W_OK)
    except Exception:
        info["writable_tree"] = False
    info["year"] = detect_year(p)
    info["is_target"] = info["year"] == TARGET_YEAR
    info["valid"] = bool(info["addons_xml_exists"])
    if not info["valid"]:
        info["reason"] = "没有 config\\eesof_addons.xml（不是 ADS 或安装不完整）"
    return info


def _from_env() -> list:
    val = (os.environ.get(ADS_DIR_ENV) or "").strip().strip('"')
    return [val] if val else []


def _from_registry() -> list:
    """Windows 卸载表里找 Keysight ADS。读不到就算了，不算失败。"""
    if os.name != "nt":
        return []
    out: list = []
    try:
        import winreg
    except Exception:
        return out

    roots = [
        (getattr(winreg, "HKEY_LOCAL_MACHINE", None), r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (getattr(winreg, "HKEY_LOCAL_MACHINE", None), r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (getattr(winreg, "HKEY_CURRENT_USER", None), r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    flags = winreg.KEY_READ
    try:
        flags |= winreg.KEY_WOW64_64KEY
    except Exception:
        pass

    for root, sub in roots:
        if root is None:
            continue
        try:
            handle = winreg.OpenKey(root, sub, 0, flags)
        except OSError:
            continue
        idx = 0
        while True:
            try:
                name = winreg.EnumKey(handle, idx)
            except OSError:
                break
            idx += 1
            try:
                with winreg.OpenKey(handle, name, 0, flags) as kh:
                    try:
                        display = str(winreg.QueryValueEx(kh, "DisplayName")[0])
                    except OSError:
                        continue
                    if "ADS" not in display and "Advanced Design System" not in display:
                        continue
                    loc = ""
                    for value in ("InstallLocation", "UninstallString"):
                        try:
                            loc = str(winreg.QueryValueEx(kh, value)[0])
                        except OSError:
                            continue
                        if loc:
                            break
                    loc = loc.strip().strip('"')
                    # UninstallString 通常是 "...\Uninstall\setup.exe"，往上取根
                    if loc and os.path.basename(loc).lower().endswith(".exe"):
                        loc = os.path.dirname(os.path.dirname(loc))
                    if loc:
                        out.append(loc)
            except OSError:
                continue
        try:
            winreg.CloseKey(handle)
        except OSError:
            pass
    return out


def _from_scan(max_depth: int = 4) -> list:
    """有限深度扫描常见安装盘符。只找 ``ADS20xx`` 这类名字，不做全盘遍历。"""
    pattern = re.compile(r"^ADS[ _-]?20\d{2}", re.IGNORECASE)
    roots: list = []
    env_home = (os.environ.get("SYSTEMDRIVE") or "C:").strip()
    candidates = [env_home + "\\", "D:\\", "E:\\"]
    for drive in "CDEFG":
        candidates.append(f"{drive}:\\")

    seen_roots = set()
    for root in candidates:
        norm = os.path.normpath(root)
        if norm in seen_roots or not os.path.isdir(norm):
            continue
        seen_roots.add(norm)
        stack = [(norm, 0)]
        while stack:
            current, depth = stack.pop(0)
            if depth > max_depth:
                continue
            try:
                entries = sorted(os.listdir(current))
            except OSError:
                continue
            for entry in entries:
                full = os.path.join(current, entry)
                if not os.path.isdir(full):
                    continue
                name_up = entry.upper()
                # 名字像 ADS2027 / ADS_2027 / ADS 2027：先当候选，进一层确认
                if pattern.match(entry) or name_up in ("ADS", "KEYSIGHT", "PROGRAMFILE"):
                    for sub in ("",):
                        probe_dir = os.path.join(full, sub) if sub else full
                        if os.path.isdir(os.path.join(probe_dir, "config")):
                            roots.append(probe_dir)
                # 常见二级结构：Keysight\ADS2027、Program Files\Keysight\ADS2027
                if depth < max_depth and name_up in ("KEYSIGHT", "AGILENT", "PROGRAMFILE"):
                    stack.append((full, depth + 1))
    return roots


def _dedup(paths: list) -> list:
    out: list = []
    seen = set()
    for p in paths:
        if not p:
            continue
        norm = os.path.normpath(p)
        key = os.path.normcase(norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def detect_ads_dirs( remembered: list | None = None, scan: bool = True) -> list:
    """返回候选列表：``[{dir, source, info}]``，按可信度排序。"""
    out: list = []
    seen: dict = {}

    def push(path: str, source: str, confidence: int) -> None:
        norm = os.path.normpath(path)
        key = os.path.normcase(norm)
        if key in seen:
            return
        info = validate_ads_dir(norm)
        if not info["valid"]:
            return
        seen[key] = True
        try:
            year = int(info["year"] or 0)
        except ValueError:
            year = 0
        # 目标年份优先；同一年份内部按 credibility 排序
        bonus = 200 if info["is_target"] else 0
        out.append({"dir": norm, "source": source,
                    "confidence": confidence + bonus + year,
                    "year": info["year"], "is_target": info["is_target"],
                    "info": info})

    for p in (remembered or []):
        push(p, "上次安装记录", 100)
    for p in _from_env():
        push(p, f"环境变量 {ADS_DIR_ENV}", 90)
    for p in _from_registry():
        push(p, "Windows 卸载表", 70)
    if scan:
        for p in _from_scan():
            push(p, "磁盘扫描", 30)

    out.sort(key=lambda item: -item["confidence"])
    return out


# ---------------------------------------------------------------------------
# 解释器
# ---------------------------------------------------------------------------

def _run_version(exe: str) -> str:
    try:
        proc = subprocess.run([exe, "-V"], capture_output=True, text=True,
                              timeout=20, encoding="utf-8", errors="replace")
        return (proc.stdout or proc.stderr).strip().splitlines()[0] if proc.returncode == 0 else ""
    except Exception:
        return ""


def is_ads_python(exe: str) -> bool:
    """判断某个解释器是不是 ADS 自带的（``tools/python`` 下的那个）。"""
    norm = os.path.normcase(os.path.normpath(exe or ""))
    return os.path.normcase(os.sep + os.path.join("tools", "python") + os.sep) in norm


def interpreter_candidates(ads_dirs: list | None = None) -> list:
    """可用解释器候选：``[{exe, source, note, ads}]``，ADS 自带的排最前。"""
    out: list = []
    seen: dict = {}

    def push(exe: str, source: str, note: str, ads: bool) -> None:
        if not exe or not os.path.isfile(exe):
            return
        norm = os.path.normcase(os.path.normpath(exe))
        if norm in seen:
            return
        seen[norm] = True
        out.append({"exe": norm, "source": source, "note": note, "ads": ads,
                    "version": ""})

    for item in (ads_dirs or []):
        info = validate_ads_dir(item) if isinstance(item, str) else item.get("info", {})
        py = info.get("python") if isinstance(info, dict) else ""
        if py:
            push(py, "ADS 自带 Python", "随 ADS 安装，无需另外准备 Python", True)

    if os.name == "nt":
        for exe in ("py", "python", "python3"):
            try:
                proc = subprocess.run(
                    ["where" if exe != "py" else "where", exe],
                    capture_output=True, text=True, timeout=15,
                    encoding="utf-8", errors="replace", shell=True,
                )
            except Exception:
                continue
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if not line or is_ads_python(line):
                    continue
                push(line, "系统 Python", "本机已安装的 Python（非 ADS 自带）", False)
                break

    return out


def choose_interpreter(ads_dirs: list | None = None, verify: bool = False) -> dict:
    """挑一个能用的解释器。

    返回 ``{"exe", "source", "note", "ads", "version", "candidates"}``；
    一个都没有时 ``exe`` 为空 —— 调用方据此给出明确的"该装什么"提示，
    不要偷偷退回到某个可能存在但跑不了的 python。
    """
    ads_dirs = ads_dirs if ads_dirs is not None else [i["dir"] for i in detect_ads_dirs()]
    cands = interpreter_candidates(ads_dirs)
    if verify:
        for c in cands:
            c["version"] = _run_version(c["exe"])
        cands = [c for c in cands if c["version"]]
    chosen = cands[0] if cands else None
    result = chosen or {"exe": "", "source": "", "note": "", "ads": False, "version": ""}
    result = dict(result)
    result["candidates"] = cands
    return result


def ads_bundled_python(ads_dir: str) -> str | None:
    """ADS 自带解释器路径；存在才返回。"""
    info = validate_ads_dir(ads_dir)
    return info["python"] or None


def inside_ads() -> bool:
    """当前进程是不是跑在 ADS 里面（面板/工具服务/启动器都是）。"""
    try:
        import keysight  # type: ignore  # noqa: F401
    except Exception:
        return False
    return is_ads_python(sys.executable or "")
