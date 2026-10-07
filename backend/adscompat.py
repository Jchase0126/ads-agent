"""ADS 2024–2027 跨版本兼容层 —— 版本识别、官方证据档案与功能门禁策略。

这个模块是**唯一事实来源**：后端与 ADS 进程内的插件（经 ``pathbridge`` 按
文件路径加载）都从这里拿版本档案与门禁决策，保证两端口径一致。

设计约束（对应 docs/版本证据报告.md 的结论）：

* **版本识别以 Windows 卸载表为权威。** 官方安装器在卸载表里写
  ``DisplayName``（含年份/Update）、``DisplayVersion`` 与 ``InstallLocation``
  （实机核实：ADS 2027 → "Advanced Design System 2027" / 6.5.0.0）。
  ``buildInfo.xml`` 实机核实只含 ``<buildtype>x64</buildtype>``（架构），
  **没有**版本信息。目录名年份只作弱证据（显式标 ``weak``）；两者都拿不到
  时按"未知版本"保守处理。
* **不凭枚举名/相似名称推断接口语义。** 档案里只记录"官方文档确认"与
  "2027 实机确认"两类事实；其余一律标 ``unknown``，由运行时能力检测决定。
* **未知版本保守降级。** 未知年份的安装：只读探测可用就开读，所有写操作
  默认拒绝，除非用户显式配置 ``allow_unknown_version``。
* **实验性版本逐功能门禁。** ADS 2024–2026 没有实机环境：写/建图/仿真按
  工具逐个判断，且要求用户显式开启对应年份的实验性开关；开关不代表验证
  通过，界面必须继续显示"未实机验证"。

本模块必须只用标准库（安装器与后端共用，addon 侧按路径加载时同样成立）。
"""

from __future__ import annotations

import os
import re
import struct
import sys

# ---------------------------------------------------------------------------
# 版本识别
# ---------------------------------------------------------------------------

_BUILDINFO_CANDIDATES = (
    os.path.join("config", "buildInfo.xml"),
    "buildInfo.xml",
)

_YEAR_RE = re.compile(r"(20\d{2})")


def parse_build_info(ads_dir: str) -> dict:
    """解析 ADS 安装目录的 ``buildInfo.xml``。

    **实机事实（2026-10-07，ADS 2027 / 2022 真实安装）**：该文件只有一行
    ``<buildtype>x64</buildtype>`` —— 只承载**架构**信息，**没有**年份/Update/
    build。因此它只能用来确认 64 位，不能用来识别版本（旧设计曾把它当年份
    来源，是错的）。返回 ``{"year": None, "update": "", "build": "",
    "arch": "x64", "source": path|None}``。
    """
    out = {"year": None, "update": "", "build": "", "arch": "", "source": None}
    root = (ads_dir or "").strip().strip('"')
    if not root or not os.path.isdir(root):
        return out
    for rel in _BUILDINFO_CANDIDATES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        out["source"] = path
        m = re.search(r"<buildtype>([^<]+)</buildtype>", text, re.IGNORECASE)
        if m:
            out["arch"] = m.group(1).strip()
        break
    return out


#: Windows 卸载表里 Keysight ADS 的显示名模式（官方安装器写入）
_REG_DISPLAY_RE = re.compile(
    r"Advanced\s+Design\s+System\s+(20\d{2})(?:\s+Update\s+(\S+))?", re.IGNORECASE
)


def parse_registry(ads_dir: str) -> dict:
    """从 Windows 卸载表识别 ADS 版本（**权威来源**）。

    官方安装器会写 ``DisplayName``（含年份与 Update，如
    "Advanced Design System 2022 Update 2"）、``DisplayVersion``（如 6.5.0.0）
    与 ``InstallLocation``。用 InstallLocation 与目标目录精确匹配。

    返回 ``{"matched": bool, "year": int|None, "update": str,
    "display_version": str, "display_name": str}``。非 Windows 或读不到时
    ``matched=False`` —— 调用方退回弱证据。
    """
    out = {"matched": False, "year": None, "update": "",
           "display_version": "", "display_name": ""}
    if os.name != "nt":
        return out
    target = os.path.normcase(os.path.normpath((ads_dir or "").strip().strip('"')))
    if not target or not os.path.isdir(target):
        return out
    try:
        import winreg
    except ImportError:
        return out

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    flags = winreg.KEY_READ
    try:
        flags |= winreg.KEY_WOW64_64KEY
    except Exception:
        pass

    for root_key, sub in roots:
        try:
            handle = winreg.OpenKey(root_key, sub, 0, flags)
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
                    if "Advanced Design System" not in display and \
                            "ADS" not in display:
                        continue
                    loc = ""
                    try:
                        loc = str(winreg.QueryValueEx(kh, "InstallLocation")[0])
                    except OSError:
                        pass
                    loc = loc.strip().strip('"')
                    if not loc or os.path.normcase(os.path.normpath(loc)) != target:
                        continue
                    m = _REG_DISPLAY_RE.search(display)
                    if not m:
                        continue
                    try:
                        version = str(winreg.QueryValueEx(kh, "DisplayVersion")[0])
                    except OSError:
                        version = ""
                    return {
                        "matched": True,
                        "year": int(m.group(1)),
                        "update": (m.group(2) or "").strip(),
                        "display_version": version,
                        "display_name": display,
                    }
            except OSError:
                continue
        try:
            winreg.CloseKey(handle)
        except OSError:
            pass
    return out


def detect_version(ads_dir: str) -> dict:
    """权威版本识别。优先级：

    1. **Windows 卸载表**（官方安装器写入的 DisplayName/DisplayVersion，
       按 InstallLocation 精确匹配）—— ``source="registry"``；
    2. 目录名年份（官方安装器的目录命名约定）—— ``source="dir_name"``，
       带弱证据标记 ``weak=True``（用户手动改名/移动的目录可能误导）；
    3. 都没有 → ``status="unknown"``（year=None），门禁按未知版本保守处理。

    返回 dict：``{year, update, build(=DisplayVersion), source, weak, known,
    status, profile, arch}``。``buildInfo.xml`` 只提供架构（见其 docstring）。
    """
    reg = parse_registry(ads_dir)
    if reg.get("matched"):
        year = reg.get("year")
        profile = KNOWN_YEAR_PROFILES.get(year) if isinstance(year, int) else None
        return {
            "year": year,
            "update": reg.get("update") or "",
            "build": reg.get("display_version") or "",
            "source": "registry",
            "weak": False,
            "display_name": reg.get("display_name") or "",
            "known": profile is not None,
            "status": "known" if profile is not None else "unknown",
            "profile": profile,
            "arch": parse_build_info(ads_dir).get("arch") or "",
        }

    hint = ""
    m = _YEAR_RE.search(os.path.basename(os.path.normpath(ads_dir or "")))
    if m:
        hint = m.group(1)
    year = int(hint) if hint else None
    profile = KNOWN_YEAR_PROFILES.get(year) if isinstance(year, int) else None
    if year is not None and profile is not None:
        return {
            "year": year, "update": "", "build": "",
            "source": "dir_name", "weak": True,
            "known": True, "status": "known", "profile": profile,
            "arch": parse_build_info(ads_dir).get("arch") or "",
        }
    return {
        "year": None, "update": "", "build": "",
        "source": None, "weak": False,
        "known": False, "status": "unknown", "profile": None,
        "arch": parse_build_info(ads_dir).get("arch") or "",
    }


# ---------------------------------------------------------------------------
# 官方版本档案（证据编码，详见 docs/版本证据报告.md）
# ---------------------------------------------------------------------------

#: 每个年份一条档案。字段语义：
#:   windows        —— 官方支持的平台表（docs.keysight.com/display/support/ADS+Supported+Platforms）
#:   python_official —— 官方发行说明明示的 DE Python 版本；None = 官方未公开
#:   python_measured —— 实机核实过的解释器版本（标注来源）
#:   qt_binding     —— 官方发行说明确认的 PySide 绑定
#:   addon_contract —— 插件加载契约的证据等级
#:   automation_api —— 自动化模式查询 API（官方发行说明记录的更名）
#:   status         —— "verified"（有实机回归基线）/ "experimental"（仅文档+离线）
KNOWN_YEAR_PROFILES = {
    2024: {
        "year": 2024,
        "status": "experimental",
        "windows": {"win10": True, "win11": True, "arch": "x64-only"},
        "python_official": None,          # 发行说明未公开 DE Python 版本号（Beta 阶段）
        "python_measured": None,
        "python_console": "beta",          # 2024 起 Python 控制台为 Beta，需 `ads -python` 启动参数
        "qt_binding": "PySide2",           # 2024 U2.0 发行说明：DDS Python Addon 支持 PySide2 API
        "addon_contract": "unverified",    # eesof_addons.xml 的 Python 插件加载未证实
        "automation_api": "running_automation",   # 2025 发行说明：2024 U2 及更早用此函数
        "notes": (
            "DE Python 模块为 Beta（U1.0 起）；dds 与 de 模块不能在同一 Python 进程导入"
            "（2024 U2.0 发行说明）。写操作无实机证据，默认关闭。"
        ),
    },
    2025: {
        "year": 2025,
        "status": "experimental",
        "windows": {"win10": True, "win11": True, "arch": "x64-only"},
        "python_official": (3, 12),        # "Python version 3.12 is included in ADS installation"
        "python_measured": None,
        "python_console": "production",    # 2025 起 DE Python 首个 production release
        "qt_binding": "PySide2",           # 2025 发行说明：DDS Python Addon 使用 PySide2
        "addon_contract": "documented",    # 2025 U1.0：Python/AEL addon 的 menu generator 有官方记载
        "automation_api": "is_pde_app",    # 2025 发行说明：改用 is_pde_app()
        "notes": (
            "2025 U2.0 起官方支持用户代码使用 python threading API。"
            "写操作按实验性处理（未实机验证）。"
        ),
    },
    2026: {
        "year": 2026,
        "status": "experimental",
        "windows": {"win10": "u0.1-only", "win11": True, "arch": "x64-only"},
        "python_official": None,           # DE Python 版本号未在发行说明明示（EM 子工具为 3.13）
        "python_measured": None,
        "python_console": "production",
        "qt_binding": "PySide6",           # 2026 发行说明：PySide2 已升级为 PySide6
        "addon_contract": "documented",
        "automation_api": "is_pde_app",
        "notes": (
            "从 2026 起 PySide2 → PySide6；U1.0 起 Windows 10 不再受支持"
            "（官方支持平台表 + U1.2 发行说明）。写操作按实验性处理（未实机验证）。"
        ),
    },
    2027: {
        "year": 2027,
        "status": "verified",
        "windows": {"win10": False, "win11": True, "arch": "x64-only"},
        "python_official": None,           # 发行说明未写版本号；实机核实见 python_measured
        "python_measured": (3, 14, 6),     # docs/验证记录-1.0.1.md：ADS 2027 自带 Python 3.14.6
        "python_console": "production",
        "qt_binding": "PySide6",
        "addon_contract": "verified",      # 1.0.1 本机实机验收通过
        "automation_api": "is_pde_app",
        "notes": "当前唯一实机回归基线（build 650 实测，docs/Layout 审查设计.md 能力矩阵）。",
    },
}

#: 有实机回归基线的年份。只有它不做"实验性"标记。
VERIFIED_YEARS = frozenset(
    y for y, p in KNOWN_YEAR_PROFILES.items() if p["status"] == "verified"
)
#: 文档+离线验证、未实机验证的年份（实验性）。
EXPERIMENTAL_YEARS = frozenset(
    y for y, p in KNOWN_YEAR_PROFILES.items() if p["status"] == "experimental"
)

#: 各版本要求的 Windows（用于安装期/启动期提示，不做硬断言——以官方表为准）
WIN_REQUIREMENT_TEXT = {
    2024: "Windows 10/11 x64（官方支持平台表）",
    2025: "Windows 10/11 x64（官方支持平台表）",
    2026: "Windows 11 x64（U1.0 起 Windows 10 已停止支持）；U0.1 仍支持 Windows 10",
    2027: "Windows 11 x64（官方支持平台表，Windows 10 不支持）",
}


def profile_for(year) -> dict | None:
    """年份 → 官方档案；未知年份返回 None（调用方必须按 unknown 处理）。"""
    if isinstance(year, str) and year.isdigit():
        year = int(year)
    if not isinstance(year, int):
        return None
    return KNOWN_YEAR_PROFILES.get(year)


def expected_qt_binding(year) -> str | None:
    """官方证据指向的 PySide 绑定名；未知年份返回 None。"""
    p = profile_for(year)
    return p.get("qt_binding") if p else None


# ---------------------------------------------------------------------------
# 工具分类与门禁
# ---------------------------------------------------------------------------

#: 读工具：只读 ADS 数据库/数据集，不修改设计。
READ_TOOLS = frozenset({
    "get_workspace_info", "list_designs", "get_design_variables",
    "check_connections", "audit_rf", "read_dataset", "read_traces",
    "open_schematic", "design_fingerprint",
})
#: 写/执行工具：可能修改设计或触发仿真。**逐个**门禁，不存在"一个年份开关
#: 全部开放"的说法。
WRITE_TOOLS = frozenset({
    "set_design_variables", "build_schematic", "run_simulation", "run_python",
})
#: 风险分级（错误信息与文档里用）：
#:   variables —— 改 VAR 参数并保存；schematic —— 建图并保存；
#:   simulation —— 起仿真进程；arbitrary —— 任意 Python 代码。
TOOL_RISK = {
    "set_design_variables": "variables",
    "build_schematic": "schematic",
    "run_simulation": "simulation",
    "run_python": "arbitrary",
}
#: 门禁拒绝码。
DENY_UNKNOWN_VERSION = "unknown_version"
DENY_EXPERIMENTAL_OFF = "experimental_disabled"
DENY_CAPABILITY = "capability_missing"
DENY_UNSUPPORTED = "unsupported"


def gating_decision(tool: str, year_status: str, year, capability_ok: bool,
                    experimental_enabled: bool, allow_unknown: bool) -> dict:
    """单个工具的开放决策。**这是两端共用的唯一裁决点。**

    参数：
      tool                 —— 工具名（含 design_fingerprint 这类内部工具）
      year_status          —— ``"known"`` / ``"unknown"``（adscompat.detect_version）
      year                 —— int 年份或 None
      capability_ok        —— 运行时能力检测确认该工具依赖的接口可用
      experimental_enabled —— 对应年份的实验性开关是否已开启（2027 恒 True）
      allow_unknown        —— 未知版本放行开关（config [compat] allow_unknown_version）

    返回 ``{"allowed": bool, "code": str, "reason": str}``。
    """
    year_int = None
    if isinstance(year, str) and year.isdigit():
        year_int = int(year)
    elif isinstance(year, int):
        year_int = year

    # 规则 0：能力检测说了算 —— 接口探测不到，什么年份都不开。
    if not capability_ok:
        return {
            "allowed": False, "code": DENY_CAPABILITY,
            "reason": f"当前 ADS 进程未检测到工具 {tool} 依赖的接口（运行时能力检测未通过）",
        }

    # 规则 1：未知版本 —— 只读工具按运行时能力放行（能力已由规则 0 把关）；
    # 写/执行工具必须用户显式开启 allow_unknown_version，否则拒绝。
    if year_status != "known" or year_int is None:
        if tool in READ_TOOLS:
            return {"allowed": True, "code": "",
                    "reason": "未知版本：仅凭运行时能力检测开放只读工具"}
        if allow_unknown:
            return {"allowed": True, "code": "",
                    "reason": "未知版本且用户已显式开启 allow_unknown_version（无官方证据，风险自担）"}
        return {
            "allowed": False, "code": DENY_UNKNOWN_VERSION,
            "reason": ("无法从 buildInfo.xml 确认 ADS 版本，且没有该版本的兼容证据；"
                       "写/建图/仿真已禁用。可在 config.ini [compat] allow_unknown_version "
                       "显式开启（不受支持）"),
        }

    # 规则 2：有实机回归基线的年份（2027）—— 直接放行。
    if year_int in VERIFIED_YEARS:
        return {"allowed": True, "code": "", "reason": ""}

    # 规则 3：实验性年份（2024–2026）—— 只读工具按能力放行（规则 0 已把关）；
    # 写/执行工具逐个要求对应年份的显式开关，**不存在**一个年份开关全开的做法。
    if tool not in READ_TOOLS and not experimental_enabled:
        return {
            "allowed": False, "code": DENY_EXPERIMENTAL_OFF,
            "reason": (f"ADS {year_int} 为实验性适配（仅文档与离线验证，未实机验证）；"
                       f"工具 {tool} 需在 config.ini [compat] experimental_{year_int} = true "
                       f"显式开启后才会开放。开启不代表已验证通过"),
        }
    if tool in WRITE_TOOLS:
        return {"allowed": True, "code": "",
                "reason": f"实验性开放（ADS {year_int} 未实机验证）"}
    return {"allowed": True, "code": "", "reason": ""}


def experimental_flag_key(year) -> str:
    """config.ini [compat] 里对应年份的实验性开关键名。"""
    return f"experimental_{year}"


# ---------------------------------------------------------------------------
# 位数判定
# ---------------------------------------------------------------------------

def python_bits() -> int:
    """当前解释器位数（区分"系统是 64 位"和"这个进程是 64 位"）。"""
    return struct.calcsize("P") * 8


def _machine() -> str:
    """机器架构（优先环境变量，规避 32 位进程里 platform.machine() 被 WOW64 
    报成 x86 的问题 —— Windows 会提供 PROCESSOR_ARCHITEW6432）。"""
    if os.name == "nt":
        wow = os.environ.get("PROCESSOR_ARCHITEW6432")
        if wow:
            return wow
    import platform

    return platform.machine() or ""


def arch_report() -> dict:
    """位数与架构自检。**只描述，不承诺**：32 位 Windows 明确不支持。

    返回：
      machine          —— 系统架构（x86_64/AMD64/ARM64/x86…）
      os_64bit         —— 操作系统是否 64 位
      python_bitness   —— 当前解释器位数（32 位启动器在 64 位系统上运行时为 32）
      supported        —— 当前解释器可否承载本插件（x64 才行）
      note             —— 人话说明（区分安装器进程与 ADS 解释器）
    """
    machine = _machine().upper()
    os_64 = machine in ("AMD64", "X86_64", "ARM64") or (
        os.name == "nt" and bool(os.environ.get("PROCESSOR_ARCHITEW6432"))
    )
    bits = python_bits()
    # ADS 2024+ 官方只发 64 位（官方支持平台表："supported 64-bit platforms"）。
    supported = os_64 and bits == 64
    note = ""
    if not os_64:
        note = "检测到 32 位 Windows：官方 ADS 2024–2027 仅提供 64 位版本，不支持此环境"
    elif bits != 64:
        note = ("当前是 32 位 Python 进程（可能是 32 位启动器）。系统本身是 64 位；"
                "ADS 自带解释器为 64 位，功能不受影响，但本进程不能作为插件宿主")
    return {
        "machine": machine,
        "os_64bit": bool(os_64),
        "python_bitness": bits,
        "supported": supported,
        "note": note,
    }
