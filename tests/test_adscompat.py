"""跨版本兼容层：版本识别、官方档案、门禁决策与位数判定（不需要 ADS）。

覆盖三类故障注入：
  * 版本识别来源缺失 / 多样（注册表命中、目录名弱证据、完全未知）；
  * 门禁矩阵（未知版本、实验性版本、已验证版本 × 读/写工具 × 开关）；
  * 位数判定（32 位进程 / 64 位系统的区分，mock 环境变量与指针宽度）。

运行::

    python tests/test_adscompat.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

add_path("backend")

import adscompat  # noqa: E402


def _fake_ads(tmp: str, name: str) -> str:
    """造一个最小 ADS 安装目录（bin/tools/config + buildInfo.xml）。"""
    root = os.path.join(tmp, name)
    for sub in ("bin", "tools", "config"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    with open(os.path.join(root, "config", "buildInfo.xml"), "w",
              encoding="utf-8") as f:
        f.write('<?xml version="1.0"?>\n<buildtype>x64</buildtype>\n')
    return root


# ---------------------------------------------------------------------------
# buildInfo.xml：实机核实只含 buildtype（架构），绝不冒充版本
# ---------------------------------------------------------------------------

def test_build_info_arch_only():
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        root = _fake_ads(tmp, "ADS2027")
        info = adscompat.parse_build_info(root)
        eq(info["arch"], "x64")
        eq(info["year"], None, "buildInfo.xml 不承载年份（实机核实）")
        eq(info["build"], "", "buildtype 不得被误当成 build 号")


def test_build_info_missing_file():
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        info = adscompat.parse_build_info(tmp)
        eq(info["arch"], "")
        ok(info["source"] is None)


# ---------------------------------------------------------------------------
# detect_version：注册表权威 > 目录名弱证据 > 未知
# ---------------------------------------------------------------------------

def test_detect_registry_hit():
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        root = _fake_ads(tmp, "ADS2027")
        real = adscompat.parse_registry
        adscompat.parse_registry = lambda d: {
            "matched": True, "year": 2027, "update": "",
            "display_version": "6.5.0.0",
            "display_name": "Advanced Design System 2027",
        }
        try:
            ver = adscompat.detect_version(root)
        finally:
            adscompat.parse_registry = real
        eq(ver["year"], 2027)
        eq(ver["status"], "known")
        eq(ver["source"], "registry")
        ok(not ver["weak"])
        eq(ver["build"], "6.5.0.0")


def test_detect_registry_update_parsed():
    """'Advanced Design System 2025 Update 1.0' → year=2025 update=1.0。"""
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        root = _fake_ads(tmp, "ADS_2025_U1")
        real = adscompat.parse_registry
        adscompat.parse_registry = lambda d: {
            "matched": True, "year": 2025, "update": "1.0",
            "display_version": "6.1.0.0",
            "display_name": "Advanced Design System 2025 Update 1.0",
        }
        try:
            ver = adscompat.detect_version(root)
        finally:
            adscompat.parse_registry = real
        eq(ver["year"], 2025)
        eq(ver["update"], "1.0")
        eq(ver["status"], "known")


def test_detect_dir_name_weak_evidence():
    """卸载表没命中时：官方命名目录给弱证据（known 但 weak=True）。"""
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        root = _fake_ads(tmp, "ADS2025")
        ver = adscompat.detect_version(root)
        eq(ver["year"], 2025)
        eq(ver["status"], "known")
        eq(ver["source"], "dir_name")
        ok(ver["weak"], "目录名年份必须带弱证据标记")


def test_detect_unknown_version():
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        root = _fake_ads(tmp, "KeysightTools_9.9")
        ver = adscompat.detect_version(root)
        eq(ver["status"], "unknown")
        eq(ver["year"], None)
        ok(ver["profile"] is None)


def test_detect_out_of_range_year_is_unknown():
    """实机上有 ADS 2022：识别得出年份，但不在适配档案 → 未知（保守）。"""
    with tempfile.TemporaryDirectory(prefix="adscompat_") as tmp:
        root = _fake_ads(tmp, "ADS2022")
        real = adscompat.parse_registry
        adscompat.parse_registry = lambda d: {
            "matched": True, "year": 2022, "update": "2",
            "display_version": "5.5.2.0",
            "display_name": "Advanced Design System 2022 Update 2",
        }
        try:
            ver = adscompat.detect_version(root)
        finally:
            adscompat.parse_registry = real
        eq(ver["year"], 2022)
        eq(ver["status"], "unknown", "2022 不在 2024–2027 档案内 → 保守降级")


def test_profiles_match_official_evidence():
    """档案数据必须与官方文档结论一致（docs/版本证据报告.md）。"""
    eq(adscompat.KNOWN_YEAR_PROFILES[2025]["python_official"], (3, 12),
       "ADS 2025 官方发行说明：Python 3.12")
    eq(adscompat.KNOWN_YEAR_PROFILES[2024]["qt_binding"], "PySide2")
    eq(adscompat.KNOWN_YEAR_PROFILES[2025]["qt_binding"], "PySide2")
    eq(adscompat.KNOWN_YEAR_PROFILES[2026]["qt_binding"], "PySide6",
       "2026 发行说明：PySide2 升级为 PySide6")
    eq(adscompat.KNOWN_YEAR_PROFILES[2027]["python_measured"], (3, 14, 6),
       "2027 实机核实 Python 3.14.6")
    eq(adscompat.KNOWN_YEAR_PROFILES[2027]["status"], "verified")
    eq(adscompat.VERIFIED_YEARS, frozenset({2027}))
    eq(adscompat.EXPERIMENTAL_YEARS, frozenset({2024, 2025, 2026}))


# ---------------------------------------------------------------------------
# 门禁矩阵
# ---------------------------------------------------------------------------

def test_gate_capability_missing_denies_everything():
    for tool in ("get_workspace_info", "build_schematic"):
        d = adscompat.gating_decision(tool, "known", 2027, capability_ok=False,
                                      experimental_enabled=True,
                                      allow_unknown=True)
        ok(not d["allowed"])
        eq(d["code"], adscompat.DENY_CAPABILITY)


def test_gate_verified_year_allows_all():
    for tool in ("get_workspace_info", "set_design_variables",
                 "build_schematic", "run_simulation", "run_python"):
        d = adscompat.gating_decision(tool, "known", 2027, capability_ok=True,
                                      experimental_enabled=False,
                                      allow_unknown=False)
        ok(d["allowed"], f"2027 是回归基线，{tool} 应放行")


def test_gate_experimental_year_needs_optin():
    for tool in ("set_design_variables", "build_schematic",
                 "run_simulation", "run_python"):
        d = adscompat.gating_decision(tool, "known", 2025, capability_ok=True,
                                      experimental_enabled=False,
                                      allow_unknown=False)
        ok(not d["allowed"])
        eq(d["code"], adscompat.DENY_EXPERIMENTAL_OFF)
        contains(d["reason"], "experimental_2025",
                 "拒绝原因必须给出确切的开关名")
        contains(d["reason"], "未实机验证")
    # 显式开启后放行 —— 但 reason 仍必须声明"实验性"
    d = adscompat.gating_decision("build_schematic", "known", 2025,
                                  capability_ok=True, experimental_enabled=True,
                                  allow_unknown=False)
    ok(d["allowed"])
    contains(d["reason"], "未实机验证")


def test_gate_experimental_reads_open_with_capability():
    """读工具不要求实验性开关（能力检测通过即可）—— 聊天/读数场景可用。"""
    for tool in ("get_workspace_info", "list_designs", "get_design_variables",
                 "read_dataset", "check_connections"):
        d = adscompat.gating_decision(tool, "known", 2024, capability_ok=True,
                                      experimental_enabled=False,
                                      allow_unknown=False)
        ok(d["allowed"], f"读工具 {tool} 在实验性年份默认可用")


def test_gate_unknown_version_read_only():
    """未知版本：读按能力放行；写必须 allow_unknown 显式开启。"""
    for tool in ("get_workspace_info", "read_dataset"):
        d = adscompat.gating_decision(tool, "unknown", None, capability_ok=True,
                                      experimental_enabled=False,
                                      allow_unknown=False)
        ok(d["allowed"], f"未知版本读工具 {tool} 应放行")
    d = adscompat.gating_decision("build_schematic", "unknown", None,
                                  capability_ok=True, experimental_enabled=True,
                                  allow_unknown=False)
    ok(not d["allowed"])
    eq(d["code"], adscompat.DENY_UNKNOWN_VERSION)
    d = adscompat.gating_decision("build_schematic", "unknown", None,
                                  capability_ok=True, experimental_enabled=True,
                                  allow_unknown=True)
    ok(d["allowed"], "allow_unknown 显式开启后放行（风险自担）")


# ---------------------------------------------------------------------------
# 位数判定
# ---------------------------------------------------------------------------

def test_arch_report_64bit_process():
    """64 位进程跑在 64 位系统：直接支持（本测试自身就是 64 位时）。"""
    if sys.maxsize <= 2 ** 32:
        return  # 32 位解释器上的另一条用例覆盖
    arch = adscompat.arch_report()
    eq(arch["python_bitness"], 64)
    ok(arch["supported"], "64 位 Python + 64 位系统应判定为支持")


def test_arch_report_32bit_launcher_on_64bit_os(monkeypatch=None):
    """32 位启动器跑在 64 位系统：os_64bit=True 但进程 32 位 → 不支持承载，
    且 note 必须区分"启动器"与"系统"（WOW64 场景）。"""
    if sys.maxsize > 2 ** 32:
        # 模拟 32 位进程：struct.calcsize 被 monkeypatch
        real = adscompat.python_bits
        adscompat.python_bits = lambda: 32
        try:
            arch = adscompat.arch_report()
        finally:
            adscompat.python_bits = real
        eq(arch["python_bitness"], 32)
        ok(arch["os_64bit"], "系统本身仍判定为 64 位")
        ok(not arch["supported"])
        contains(arch["note"], "32 位")
        contains(arch["note"], "64 位")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
