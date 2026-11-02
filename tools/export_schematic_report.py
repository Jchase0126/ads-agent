"""导出已建原理图的网表与连接摘要（只读，供验收归档）。

截图需要 GUI 会话，脚本方式拿不到；这里导出**可核查的文本证据**：
实例清单、导线几何、网络划分、网表文本 —— 电气正确性用网表核对，
可读性留给交互式截图，两者不互相替代。

跑法（ADS 捆绑解释器 + HPEESOF_DIR）::

    export HPEESOF_DIR="E:/ADS/Programfile/ADS2027"
    "E:/ADS/Programfile/ADS2027/tools/python/python.exe" tools/export_schematic_report.py \
        --workspace E:/ADS/Work_AI/J310_ab_20261014_wrk \
        --library AI_lib --cell J310_ACC_f42ca2ef \
        --out output/acceptance_ab_20261014/schematic_report.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "addon" / "ads_agent"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--view", default="schematic")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    args.out = args.out.resolve()

    from keysight.ads import de
    import ads_ops

    if de.workspace_is_open():
        print("[FAIL] 已有工作区打开；请在隔离进程里运行")
        return 2
    de.open_workspace(str(workspace))

    lines = []
    design = ads_ops._open_design(args.library, args.cell, args.view, write=False)
    try:
        report = ads_ops._design_audit(design)
        lines.append("== 实例 ==")
        for inst in sorted(design.instances,
                           key=lambda i: ads_ops._inst_name(i)):
            name = ads_ops._inst_name(inst)
            pins = []
            for pin in list(getattr(inst, "inst_pins", []) or []):
                snap = getattr(pin, "snap_point", None)
                if snap is None:
                    continue
                x, y = ads_ops._xy_point(snap)
                net = ads_ops._net_label(getattr(pin, "net", None))
                pins.append(f"p{ads_ops._pin_label(pin)}({x:.3f},{y:.3f})"
                            f"{'@' + net if net else ''}")
            lines.append(f"  {name:<16} {type(inst).__name__:<20} {' '.join(pins)}")

        lines.append("")
        lines.append("== 导线 ==")
        n_seg = 0
        # 用与几何门禁同一个提取器（_shape_polyline），保证报告里的
        # 段数/坐标与 problems 判定完全同源，不另造一套读法。
        for shape in list(design.shapes):
            pts, exact = ads_ops._shape_polyline(shape)
            if not pts:
                if not exact:
                    lines.append("  [读不出顶点] 该图形几何未验证")
                continue
            for a, b in zip(pts, pts[1:]):
                if abs(a[0] - b[0]) < 1e-8 and abs(a[1] - b[1]) < 1e-8:
                    continue
                n_seg += 1
                lines.append(f"  ({a[0]:.4g},{a[1]:.4g}) -> ({b[0]:.4g},{b[1]:.4g})")
        lines.append(f"  共 {n_seg} 段")

        geo = ads_ops._geometry_report(design)
        lines.append("")
        lines.append("== 几何复核 ==")
        lines.append(f"  段数 {geo['n_segments']}  问题 {len(geo['problems'])}"
                     f"  警告 {len(geo['warnings'])}")
        for problem in geo["problems"]:
            lines.append(f"  [问题] {problem}")
        for warning in geo["warnings"]:
            lines.append(f"  [警告] {warning}")

        netlist = ""
        try:
            netlist = design.generate_netlist()
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  [网表生成失败] {type(exc).__name__}: {exc}")
    finally:
        ads_ops._close_design(design)
        de.close_workspace()

    lines.append("")
    lines.append(f"== 网表（{len(netlist)} 字符）==")
    lines.append(netlist)
    lines.append("")
    lines.append("== 连接清单 ==")
    lines.append(json.dumps({"audit": report}, ensure_ascii=False, indent=1))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"写入 {args.out}")
    print(f"实例 {report.get('n_instances')} 网络 {report.get('n_nets')} "
          f"导线段 {n_seg} 几何问题 {len(geo['problems'])}")
    return 0 if not geo["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())