"""ADS 2024–2026 实机验收脚本 —— 取得旧版环境后执行的第一步。

用法（在本插件目录里，用任意 Python 运行）::

    python tools/real_machine_check.py                     # 只读检查
    python tools/real_machine_check.py --ads-dir <目录>    # 显式指定 ADS
    python tools/real_machine_check.py --smoke-toolserver  # 插件已在 ADS 里运行时：
                                                           #   校验 /health 兼容快照
    python tools/real_machine_check.py --write-checks      # 额外准备写入验收
                                                           #   （隔离临时工作区）

原则：
  * **只读优先** —— 不打开设计、不写库、不停任何正在运行的服务；
  * 写入验收**必须显式** --write-checks，且只准备隔离的临时工作区脚手架，
    真正的写入验证通过插件工具在 ADS 进程内执行（见输出的清单）；
  * 结果如实区分"官方文档确认 / 运行时探测确认 / 未确认"。

产出：控制台报告 + 可选 JSON（--json）。这份输出可直接粘进
docs/验证记录-<版本>.md 作为实机验收底稿。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, "backend"))

import adscompat  # noqa: E402
import adslocate  # noqa: E402


def _section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _pick_ads_dir(args) -> str:
    if args.ads_dir:
        info = adslocate.validate_ads_dir(args.ads_dir)
        if not info["valid"]:
            raise SystemExit(f"--ads-dir 不是可用 ADS 安装：{args.ads_dir}（{info['reason']}）")
        return info["dir"]
    found = adslocate.detect_ads_dirs()
    if not found:
        raise SystemExit("未探测到 ADS 安装；请用 --ads-dir 指定。")
    return found[0]["dir"]


def check_version(ads_dir: str) -> dict:
    _section("1) 版本识别（Windows 卸载表权威；buildInfo.xml 仅架构）")
    ver = adslocate.describe_version(ads_dir)
    profile = adscompat.profile_for(ver["year"])
    print(f"目录：{ads_dir}")
    print(f"识别结果：year={ver['year']}  update={ver['update'] or '-'}  "
          f"版本号={ver['build'] or '-'}  来源={ver['source']}  弱证据={ver['weak']}")
    if ver["weak"]:
        print("!! 卸载表未命中，目录名只是弱证据 —— 请与 ADS 关于页人工核对")
    if profile is None:
        print("!! 不在适配档案（2024–2027）→ 插件将按未知版本保守降级（只读）")
    else:
        print(f"适配档案：状态={profile['status']}  Qt={profile['qt_binding']}  "
              f"Python={profile['python_official'] or profile['python_measured'] or '官方未公开'}")
        print(f"官方平台要求：{adscompat.WIN_REQUIREMENT_TEXT.get(ver['year'], '')}")
    return ver


def check_interpreter(ads_dir: str) -> dict:
    _section("2) 自带解释器与模块可导入性（进程外探测，只读）")
    py = os.path.join(ads_dir, "tools", "python", "python.exe")
    if os.name != "nt":
        py = os.path.join(ads_dir, "tools", "python", "bin", "python3")
    out = {"python": py if os.path.isfile(py) else "", "modules": {}}
    if not out["python"]:
        print("!! 未找到 ADS 自带解释器（tools/python/python.exe）")
        return out
    # keysight.ads.de 在导入期要求 HPEESOF_DIR（实机核实）—— 探测时补上
    env = dict(os.environ)
    env.setdefault("HPEESOF_DIR", ads_dir)
    ver = subprocess.run([out["python"], "-V"], capture_output=True, text=True, timeout=60)
    print(f"解释器：{out['python']}  ->  {ver.stdout.strip() or ver.stderr.strip()}")
    out["version"] = (ver.stdout or ver.stderr).strip()
    bits = subprocess.run(
        [out["python"], "-c", "import struct;print(struct.calcsize('P')*8)"],
        capture_output=True, text=True, timeout=60, env=env)
    out["bits"] = bits.stdout.strip()
    print(f"位数：{out['bits']} 位（ADS 2024+ 官方仅 64 位）")
    probes = {
        "keysight.ads.de": "import keysight.ads.de",
        "keysight.ads.de.db_uu.DesignMode": "import keysight.ads.de.db_uu as m; m.DesignMode",
        "keysight.ads.dataset": "import keysight.ads.dataset",
        "keysight.edatoolbox.ads.CircuitSimulator":
            "from keysight.edatoolbox.ads import CircuitSimulator",
        "PySide 绑定": "import importlib;importlib.import_module('PySide6') or importlib.import_module('PySide2')",
    }
    for label, code in probes.items():
        proc = subprocess.run([out["python"], "-c", code],
                              capture_output=True, text=True, timeout=120, env=env)
        status = "OK" if proc.returncode == 0 else \
            f"失败（{(proc.stderr or '').strip().splitlines()[-1][:120] if (proc.stderr or '').strip() else '未知'}）"
        print(f"  {label:<42} {status}")
        out["modules"][label] = proc.returncode == 0
    print("说明：进程外导入成功只证明模块在包里；DE 数据库操作必须在 ADS 进程内。")
    return out


def check_toolserver(base_hint: str = "http://127.0.0.1:8761") -> dict:
    _section("3) 工具服务兼容快照（插件已在 ADS 里运行时可用；只读）")
    try:
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base_hint + "/health", timeout=3) as r:
            payload = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"未探测到运行中的工具服务（{base_hint}）：{type(e).__name__}: {e}")
        print("（这不是错误 —— 请先在 ADS 里打开一次面板，再重跑 --smoke-toolserver）")
        return {}
    compat = payload.get("compat") or {}
    identity = payload.get("identity") or {}
    print(f"服务：{payload.get('service')}  插件={identity.get('plugin_version')}  "
          f"ADS={identity.get('ads_year') or '?'}")
    version = compat.get("ads_version") or {}
    print(f"快照版本识别：year={version.get('year')}  source={version.get('source')}  "
          f"status={version.get('status')}")
    for name, entry in sorted((compat.get("capabilities") or {}).items()):
        print(f"  {entry.get('status'):<12} {name}" +
              (f" — {entry.get('reason')}" if entry.get("reason") else ""))
    print("工具门禁：")
    for name, decision in sorted((compat.get("tools") or {}).items()):
        mark = "允许" if decision.get("allowed") else "拒绝"
        print(f"  [{mark}] {name}" +
              ("" if decision.get("allowed") else f"（{decision.get('code')}）"))
    return compat


def prepare_write_checks(ads_dir: str) -> dict:
    _section("4) 写入验收准备（--write-checks：隔离临时工作区，不动用户设计）")
    import configparser

    try:
        import paths as _paths
        cfg_path = _paths.config_path()
        parser = configparser.ConfigParser()
        if os.path.exists(cfg_path):
            parser.read(cfg_path, encoding="utf-8")
        ver = adscompat.describe_version(ads_dir)
        year = ver.get("year")
        flag = f"experimental_{year}" if year and year != 2027 else None
        if flag:
            enabled = parser.get("compat", flag, fallback="").strip().lower() in \
                ("1", "true", "yes", "on")
            print(f"[compat] {flag} = {parser.get('compat', flag, fallback='(未设置)') or '(未设置)'}"
                  f"  ->  {'已开启' if enabled else '未开启（写工具会被门禁拒绝）'}")
    except Exception as e:  # noqa: BLE001
        print(f"（配置读取失败，跳过开关核对：{type(e).__name__}: {e}）")

    ws = os.path.join(tempfile.mkdtemp(prefix="ads_agent_acceptance_"), "acceptance_wrk")
    print("已准备隔离验收工作区脚手架（目录已建，未连接任何用户工程）：")
    print(f"  {ws}")
    print("\n写入验收清单（在 ADS 内、通过本插件面板逐项执行，全部通过才算实机验收）：")
    steps = [
        "set_design_variables：在验收工作区新建/打开一个含 VAR 的设计，改一个参数并确认保存复核通过，然后撤销不保存；",
        "build_schematic：新建 cell 放置一个电阻+Term 对，确认连通性检查与保存复核通过；",
        "run_simulation：放置最小 AC 控制器，跑一次并 read_traces 读取真实数据；",
        "run_python：print(de.active_workspace().path) 与 DesignMode/Orientation 位置探测；",
        "全部结束后：关闭 ADS（不保存任何用户设计），把本报告粘贴进 docs/验证记录-<版本>.md。",
    ]
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    return {"workspace": ws}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ADS 2024–2026 实机验收（只读优先）")
    ap.add_argument("--ads-dir", help="ADS 安装目录（默认自动探测）")
    ap.add_argument("--smoke-toolserver", action="store_true",
                    help="校验运行中工具服务的 /health 兼容快照（只读）")
    ap.add_argument("--write-checks", action="store_true",
                    help="额外准备写入验收（隔离临时工作区 + 清单）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    ads_dir = _pick_ads_dir(args)
    report = {"ads_dir": ads_dir}
    report["version"] = check_version(ads_dir)
    report["interpreter"] = check_interpreter(ads_dir)
    if args.smoke_toolserver:
        report["compat"] = check_toolserver()
    if args.write_checks:
        report["write_checks"] = prepare_write_checks(ads_dir)

    _section("结论")
    ver = report["version"]
    if ver.get("status") == "known" and ver.get("year") == 2027:
        print("ADS 2027：已实机验收基线（1.0.1/1.1.0 回归覆盖）。")
    elif ver.get("status") == "known":
        print(f"ADS {ver['year']}：实验性适配 —— 本次为实机验收的第一步（环境核查）。"
              "完成第 4 节清单并记录后，才能在 docs/兼容性矩阵.md 把对应行改为实机通过。")
    else:
        print("版本未识别：插件将保守降级（只读）。请核对安装来源与卸载表记录。")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
