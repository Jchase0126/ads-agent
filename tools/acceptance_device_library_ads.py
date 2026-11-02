"""器件模型库实机验收：导入 → 层次引脚 → symbol 视图 → 放置 → 网表 → 仿真。

**必须在 ADS 进程内运行** —— `keysight.ads.de` 的工作区状态是进程内的：
另起一个解释器进程时 `workspace_is_open()` 恒为 False，即使 ADS GUI 里
开着工作区。两条可用入口：

* 面板的 `run_python`（推荐，工具服务就在 ADS 进程里）；
* 或把本脚本的 `main()` 通过 toolserver 的 run_python 调用（把参数拼进代码）。

脚本本身不启动/关闭 ADS，不碰用户已有设计：所有 cell 都建在 `--library`
指定的库（默认 `ADS_AGENT_ACCEPT`），结束后只做只读报告，不删除任何东西。

用法（在 ADS 进程内、已打开工作区）::

    acceptance_device_library_ads.main([
        "--revision-manifest", r"E:\\...\\revisions\\rev_xxx\\manifest.json",
        "--library", "DeviceLib", "--cell", "DL_ACCEPT", "--force"])

判据（逐项如实报告，任一项失败即整体非零退出）：

1. 修订清单可读、PDF/依赖指纹核对通过；
2. 元件子电路建图成功（build_schematic + term_anchors），网表等价门禁通过；
3. symbol 视图生成成功，symbol 引脚编号与原理图端子一致且都有 pin 图形；
4. 使用电路（TermG + 元件 + S 参数控制器）建图成功；
5. run_simulation 产出真实 .ds 数据集。

**不** 判定手册精度或模型能力 —— 那些由 validate_device_model 与模型清单负责。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for rel in ("backend", "addon/ads_agent"):
    path = str(ROOT / rel)
    if path not in sys.path:
        sys.path.insert(0, path)

import device_library  # noqa: E402
import device_models  # noqa: E402


def _json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                               default=str), encoding="utf-8")


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="器件模型库 ADS 实机验收")
    parser.add_argument("--revision-manifest", required=True,
                        help="器件模型库某个修订的 manifest.json")
    parser.add_argument("--library", default="ADS_AGENT_ACCEPT",
                        help="验收用可写库（默认 ADS_AGENT_ACCEPT）")
    parser.add_argument("--cell", default="DL_ACCEPT",
                        help="验收元件 cell 名")
    parser.add_argument("--out", default=str(ROOT / "output" / "device_library_ads"),
                        help="报告输出目录")
    parser.add_argument("--force", action="store_true",
                        help="允许覆盖已存在的验收 cell（默认拒绝）")
    args = parser.parse_args(argv)

    out = Path(args.out)
    report = {"started_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "manifest": str(args.revision_manifest), "library": args.library,
              "stages": {}}

    def stage(name: str, ok: bool, **fields) -> None:
        report["stages"][name] = {"ok": bool(ok), **fields}
        print(f"[{'ok ' if ok else 'FAIL'}] {name} " +
              " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)

    # ---- 0) 先确认 ADS 进程内可用 ---------------------------------------
    try:
        import keysight.ads.de as de
    except Exception as exc:  # noqa: BLE001
        stage("ads_runtime", False,
              note=f"必须在 ADS 自带解释器内运行：{type(exc).__name__}: {exc}")
        _json(out / "acceptance.json", report)
        return 1
    if not de.workspace_is_open():
        stage("workspace", False, note="没有打开的工作区；请先在 ADS 打开一个")
        _json(out / "acceptance.json", report)
        return 1
    ws = de.active_workspace()
    ws_path = str(getattr(ws, "path", "") or "")
    stage("workspace", True, path=ws_path)
    report["workspace"] = ws_path

    # ---- 1) 清单与文件完整性 --------------------------------------------
    manifest_path = Path(args.revision_manifest)
    if not manifest_path.is_file():
        stage("manifest", False, note="manifest.json 不存在")
        _json(out / "acceptance.json", report)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = str(manifest_path.parents[4])   # devices/<dev>/<mdl>/revisions/<rev>
    problems = device_library.verify_revision_files(root, manifest)
    stage("source_integrity", not problems,
          present=(not problems), problems=problems)
    report["manifest_sha256"] = manifest.get("manifest_sha256")
    report["identity"] = manifest.get("identity")

    # ---- 2) 元件子电路（含层次引脚）--------------------------------------
    import ads_ops
    plan_payload = json.loads((manifest_path.parent / "model" /
                               "component_plan.json").read_text(encoding="utf-8"))
    plan = dict(plan_payload.get("plan") or {})
    tech = str(plan.get("cell") or args.cell)
    build_args = dict(plan)
    build_args.update({"library": args.library, "cell": args.cell,
                       "recreate": bool(args.force)})
    try:
        built = ads_ops.build_schematic(build_args)
    except Exception as exc:  # noqa: BLE001
        stage("component_build", False,
              note=f"{type(exc).__name__}: {exc}")
        _json(out / "acceptance.json", report)
        return 1
    equivalence = built.get("netlist_equivalence") or {}
    stage("component_build", bool(equivalence.get("ok")),
          design=built.get("design"),
          netlist_equivalent=equivalence.get("ok"),
          diffs=(equivalence.get("diffs") or [])[:4])
    report["component_build"] = {k: built.get(k) for k in
                                 ("design", "placed", "netlist_equivalence",
                                  "backup")}

    # ---- 3) symbol 视图 --------------------------------------------------
    try:
        symbol = ads_ops.create_component_symbol({
            "library": args.library, "cell": args.cell,
            "symbol_view": "symbol", "replace": bool(args.force)})
    except Exception as exc:  # noqa: BLE001
        stage("symbol", False, note=f"{type(exc).__name__}: {exc}")
        _json(out / "acceptance.json", report)
        return 1
    stage("symbol", bool(symbol.get("ok")), pins=symbol.get("pins"),
          problems=symbol.get("problems"))
    report["symbol"] = symbol

    # ---- 4) 使用电路（TermG + 元件 + S 参数控制器）------------------------
    usage = device_library.usage_plan_from(
        manifest, args.library, args.cell,
        band_hz=(manifest.get("validation") or {}).get("validation_scope_hz")
        and None)
    band = usage.pop("_band_hz")
    band_note = usage.pop("_band_note")
    usage.update({"library": args.library, "cell": args.cell + "_USE",
                  "recreate": bool(args.force)})
    try:
        used = ads_ops.build_schematic(usage)
    except Exception as exc:  # noqa: BLE001
        stage("usage_build", False, note=f"{type(exc).__name__}: {exc}")
        _json(out / "acceptance.json", report)
        return 1
    usage_equiv = used.get("netlist_equivalence") or {}
    stage("usage_build", bool(usage_equiv.get("ok")),
          design=used.get("design"), band_hz=band, band_note=band_note)

    # ---- 5) 真实仿真 -----------------------------------------------------
    try:
        sim = ads_ops.run_simulation({"library": args.library,
                                      "cell": args.cell + "_USE",
                                      "view": "schematic"})
    except Exception as exc:  # noqa: BLE001
        stage("simulation", False, note=f"{type(exc).__name__}: {exc}")
        _json(out / "acceptance.json", report)
        return 1
    dataset = sim.get("dataset_path") or ""
    stage("simulation", bool(dataset and sim.get("status") == "done"),
          status=sim.get("status"), dataset=dataset,
          process_exit=sim.get("process_exit"))
    report["simulation"] = {k: sim.get(k) for k in
                            ("status", "dataset_path", "process_exit",
                             "design_version", "workspace")}

    failed = [name for name, item in report["stages"].items() if not item["ok"]]
    report["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    report["overall"] = "pass" if not failed else "fail"
    report["failed_stages"] = failed
    _json(out / "acceptance.json", report)
    print("\n报告: " + str(out / "acceptance.json"))
    print("总判定: " + report["overall"] +
          ("" if not failed else " —— 失败阶段: " + ", ".join(failed)))
    print("注意：本脚本只证明元件可建图/生成 symbol/放置/仿真；"
          "模型能力与手册精度仍以 validate_device_model 与清单为准。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
