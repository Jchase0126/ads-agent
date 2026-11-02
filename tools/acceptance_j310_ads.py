"""SMMBFJ310L ADS 实机验收：建图 → 仿真 → 验证（在 ADS 解释器里运行）。

前置：tools/acceptance_j310_pipeline.py 已产出 manifest.json。
本脚本用与聊天工具**相同**的 ads_ops.build_schematic / run_simulation /
read_traces 与 device_models.validate_device_model，不绕过任何门禁。

用法（ADS 捆绑 Python）：
    E:\\ADS\\Programfile\\ADS2027\\tools\\python\\python.exe tools\\acceptance_j310_ads.py \
        --manifest output/acceptance_j310/manifest.json \
        --workspace E:\\ADS\\Work_AI\\J310_model_wrk
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "addon" / "ads_agent"))

ADDON = ROOT / "addon" / "ads_agent"
if str(ADDON) not in sys.path:
    sys.path.insert(0, str(ADDON))

#: 手册精度阈值（人工声明，origin=user_rule）。依据：2026-10-13 同一手册数字化
#: 曲线的拟合误差量级（S 复数 RMSE 约 7e-2、Y 约 1.5e-3 S）留出余量。
#: S 无量纲、Y 为西门子，必须分维度给，不能共用一个阈值。
ACCEPT_THRESHOLDS = {"max_complex_rmse_S": 0.12, "max_complex_rmse_Y": 6e-3}


def _parse_netlist(path: str) -> dict:
    """解析 ADS 生成的网表行，取出 FET 三端节点与 R/L 支路参数。"""
    fet = None
    branches = {}
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            text = line.strip()
            if text.startswith("FET:"):
                # ADS 网表里 FET 行**冒号后不带空格**：`FET:DEVICE_CORE N__1 ...`。
                # 直接 text.split() 会把 parts[1] 当成实例名，实际拿到的是
                # 第一个节点 —— 实测曾因此报 "fet_nodes=['N__2','N__3','G=...']"
                # 并把所有 PKG 支路的 key 认成节点号。必须先按第一个空白切。
                head, _, rest = text.partition(" ")
                parts = rest.split()
                fet = {"name": head.split(":", 1)[-1],
                       "nodes": parts[:3], "params": text}
            elif text.startswith(("R:", "L:")) and "PKG_" in text:
                head, _, rest = text.partition(" ")
                parts = rest.split()
                key = head.split(":", 1)[-1]
                for token in parts[2:]:
                    if "=" in token:
                        name, value = token.split("=", 1)
                        branches.setdefault(key, {})[name] = value
                branches[key]["_nodes"] = parts[:2]
    return {"fet": fet, "branches": branches}


def _expect_close(actual_text: str, expected: float, unit_factor: float,
                  rel: float = 1e-6) -> bool:
    try:
        value = float(str(actual_text).split()[0]) * unit_factor
    except (ValueError, IndexError):
        return False
    return abs(value - expected) <= max(abs(expected) * rel, 1e-15)


_S_PARAM_BLOCK = """S_Param:SP1 CalcS=yes CalcY=yes CalcZ=no GroupDelayAperture=1e-4 \
FreqConversion=no FreqConversionPort=1 StatusLevel=2 CalcNoise=no SortNoise=0 \
BandwidthForNoise=1.0 Hz DevOpPtLevel=0 SPSS_WSP=yes \\
SweepVar="freq" SweepPlan="SP1_stim" OutputPlan="SP1_Output" 

SweepPlan: SP1_stim Start={start} Hz Stop={stop} Hz Step={step} Hz 

OutputPlan:SP1_Output \\
      Type="Output" \\
      UseEquationNestLevel=yes \\
      EquationNestLevel=2 \\
      UseSavedEquationNestLevel=yes \\
      SavedEquationNestLevel=2
"""


def _plan_netlist(plan: dict, z0: float, freq: dict) -> str:
    """把建图计划变成等效网表文本（真实 hpeesofsim 等价核对用）。

    与 DE 建图走**同一条计划**，但不经原理图布局/几何门禁：只验证模型拓扑与
    参数在真实 ADS 求解器下的数值等价性。
    """
    pins = {}
    for inst in plan["instances"]:
        for pin in (1, 2, 3):
            pins[(inst["name"], pin)] = (inst["name"], pin)
    parent = {k: k for k in pins}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for conn in plan["connections"]:
        union((conn["a"][0], int(conn["a"][1])), (conn["b"][0], int(conn["b"][1])))
    ground_roots = {find((i["name"], p)) for i in plan["instances"]
                    if i["master"] == "ads_rflib:GROUND" for p in (1, 2)}
    names, counter = {}, [0]

    def node(inst_name, pin):
        root = find((inst_name, pin))
        if root in ground_roots:
            return "0"
        if root not in names:
            counter[0] += 1
            names[root] = f"N__{counter[0]}"
        return names[root]

    lines = ['; Equivalent netlist generated from the build plan '
             '(common-end package branches included)']
    for inst in plan["instances"]:
        master, name, params = inst["master"], inst["name"], inst.get("params") or {}
        if master == "ads_simulation:TermG":
            lines.append(f"Port:{name} {node(name, 1)} 0 Num={params['Num']} "
                         f"Z={params['Z']} Noise=yes ")
        elif master == "ads_rflib:R":
            lines.append(f"R:{name} {node(name, 1)} {node(name, 2)} R={params['R']} "
                         "Noise=no ")
        elif master == "ads_rflib:L":
            lines.append(f"L:{name} {node(name, 1)} {node(name, 2)} L={params['L']} "
                         "Noise=no ")
        elif master == "ads_rflib:FET":
            lines.append('#uselib "ckt" , "FET"')
            lines.append(
                f"FET:{name} {node(name, 1)} {node(name, 2)} {node(name, 3)} "
                + " ".join(f"{k}={v}" for k, v in params.items()) + " ")
    lines.append(_S_PARAM_BLOCK.format(start=f"{freq['start']:.15g}",
                                       stop=f"{freq['stop']:.15g}",
                                       step=f"{freq['step']:.15g}"))
    return "\n".join(lines) + "\n"


def run_netlist_equivalence(plan: dict, record: dict, workspace: Path,
                            results: dict, stage) -> dict:
    """真实 hpeesofsim 跑计划网表 → 与解析正演做 ADS 等价核对。"""
    from keysight.edatoolbox import ads as eda
    import ads_ops

    z0 = float(record["build"]["z0"])
    freq = record["build"]["s_parameters"]
    freq_in = {"start": freq["start_hz"], "stop": freq["stop_hz"],
               "step": freq["step_hz"]}
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = workspace / "ads_agent_sim" / f"pkg_netlist_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    netlist = _plan_netlist(plan, z0, freq_in)
    netlist_path = out_dir / "netlist.ckt"
    netlist_path.write_text(netlist, encoding="utf-8")
    exit_note = ""
    try:
        eda.CircuitSimulator().run_netlist(netlist, output_dir=str(out_dir),
                                           dataset_name="pkg_check")
    except Exception as exc:  # noqa: BLE001
        # hpeesofsim 可能"算完数据后进程退出码异常"：先看有没有产出 .ds，
        # 有数据就继续核对，同时把异常如实记下来（不掩盖）。
        exit_note = f"{type(exc).__name__}: {exc}"
    dataset = out_dir / "pkg_check.ds"
    if not dataset.is_file():
        candidates = sorted(p for p in out_dir.glob("*.ds"))
        dataset = candidates[0] if candidates else dataset
    stage("netlist_simulation", dataset.is_file(), dataset=str(dataset),
          netlist=str(netlist_path),
          **({"hpeesofsim_exit": exit_note} if exit_note else {}))
    if not dataset.is_file():
        return {}
    exprs = [f"{op}(S({i},{j}))" for i, j in ((1, 1), (2, 1), (1, 2), (2, 2))
             for op in ("real", "imag")]
    traces = ads_ops.read_traces({"path": str(dataset), "expressions": exprs,
                                  "max_points": 0})
    import device_models as M

    validation = M.validate_device_model({
        "model_id": record["model_id"], "dataset_path": str(dataset),
        "traces": traces.get("traces"),
        # 独立网表正演是**辅助证据**：没有走 build_schematic 的几何门禁、
        # 没有原理图连通性核对、没有 run_simulation 可信执行记录。
        # 2026-10-14 起必须显式声明 netlist_equivalent —— 结果只能记
        # numeric_spec_validated，**不得**升格 validated。此前这里自填
        # netlist_sha="netlist_only" 占位，等于用假来源让验收"看起来通过"。
        "validation_mode": "netlist_equivalent",
        "thresholds": dict(ACCEPT_THRESHOLDS)})
    sub = validation.get("sub_results") or {}
    numeric = sub.get("numeric_equivalence") or {}
    elements = numeric.get("elements") or {}
    stage("netlist_equivalence",
          (numeric.get("state") == "pass"
           and (numeric.get("plan_topology_check") or {}).get("state") == "pass"),
          verdict=validation["verdict"],
          numeric=numeric.get("state"),
          S_limit=round(elements.get("S", {}).get("limit", 0), 12),
          S_worst=elements.get("S", {}).get("worst_complex_rmse"),
          Y_limit=round(elements.get("Y", {}).get("limit", 0), 12),
          Y_worst=elements.get("Y", {}).get("worst_complex_rmse"),
          plan_topology=(numeric.get("plan_topology_check") or {}).get("state"),
          reasons=numeric.get("reasons"))
    results["netlist_dataset"] = str(dataset)
    results["netlist_validation"] = validation
    return validation


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--library", default="J310_model_lib")
    parser.add_argument("--recreate", action="store_true",
                        help="重建同名 cell（仅当确认要覆盖时才加）")
    args = parser.parse_args()
    # ADS 打开工作区时可能改变进程 CWD：所有路径一开始就解析成绝对路径
    args.manifest = args.manifest.resolve()
    args.workspace = args.workspace.resolve()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    pipeline_dir = args.manifest.parent
    os.environ.setdefault("ADS_AGENT_DATA_DIR", str(pipeline_dir / "data"))

    import device_models as M

    model_id = manifest["model_id"]
    record = M.get_model_record(model_id)
    if record.get("stage") != "build_planned":
        print(f"[FAIL] 模型 stage={record.get('stage')}，不是 build_planned；先跑离线链路")
        return 2

    from keysight.ads import de
    import ads_ops

    workspace = args.workspace.resolve()
    library = args.library
    if de.workspace_is_open():
        print("[FAIL] 已有工作区打开；请在隔离进程里运行（不要与 GUI 会话并用）")
        return 2

    results = {"started_at": datetime.datetime.now().isoformat(timespec="seconds"),
               "workspace": str(workspace), "library": library,
               "model_id": model_id, "stages": {}}

    def stage(name, ok, **fields):
        results["stages"][name] = {"ok": bool(ok), **fields}
        print(f"[{'ok ' if ok else 'FAIL'}] {name} " +
              " ".join(f"{k}={v}" for k, v in fields.items()), flush=True)

    if workspace.exists():
        w = de.open_workspace(str(workspace))
        names = [l.name for l in w.libraries]
        if library not in names:
            lib_path = workspace / library
            de.create_new_library(library, str(lib_path))
            w.add_library(library, str(lib_path), de.LibraryMode.NON_SHARED)
    else:
        w = de.create_workspace(str(workspace))
        w.open()
        lib_path = workspace / library
        de.create_new_library(library, str(lib_path))
        w.add_library(library, str(lib_path), de.LibraryMode.NON_SHARED)
    stage("workspace", True, path=str(workspace), library=library)

    # 建图：沿用离线链路产出的完整 build_schematic_args（只改库名/目标工作区）
    plan = json.loads((pipeline_dir / "build_plan.json").read_text(encoding="utf-8"))
    spec = dict(plan["build_schematic_args"])
    spec["library"] = library
    spec["cell"] = spec["cell"]
    spec["recreate"] = bool(args.recreate)
    stage("plan_loaded", True, cell=spec["cell"],
          instances=len(spec["instances"]), connections=len(spec["connections"]))

    try:
        built = ads_ops.build_schematic(spec)
    except Exception as exc:  # noqa: BLE001
        built = None
        stage("build", False, error=f"{type(exc).__name__}: {exc}")
        results["failed_at"] = "build"
    if built is not None:
        equivalence = built.get("netlist_equivalence") or {}
        geometry = built.get("geometry") or {}
        stage("build", bool(equivalence.get("ok")),
              cell=spec["cell"],
              n_instances=built.get("n_instances"), n_nets=built.get("n_nets"),
              netlist_diffs=len(equivalence.get("diffs") or []),
              geometry_problems=len(geometry.get("problems") or []),
              parameters_ok=built.get("parameters_ok"))

    sim = None
    dataset_path = None
    if built is not None:
        # 仿真
        try:
            sim = ads_ops.run_simulation({"library": library, "cell": spec["cell"]})
        except Exception as exc:  # noqa: BLE001
            stage("simulation", False, error=f"{type(exc).__name__}: {exc}")
            results["failed_at"] = "simulation"
    if sim is not None:
        dataset_path = sim["dataset_path"]
        stage("simulation", True, dataset=dataset_path, status=sim.get("status"))

        # ---- 可信执行记录（与后端 run_simulation 同一条路径）------------
        # 来源绑定只认这条记录；自填 library/cell/netlist_sha 等于自带数据
        # 冒充仿真结果，正是审查 P1-1 的缺陷。
        import sim_records
        sim_id = ""
        try:
            _rec = M.get_model_record(model_id)
            _build = _rec.get("build") or {}
            sim_id = sim_records.begin({
                "workspace": (sim.get("workspace") or {}).get("path", ""),
                "library": library, "cell": spec["cell"], "view": "schematic",
                "build_fingerprint": _build.get("fingerprint"),
                "netlist_sha": (sim.get("design_version") or {}).get("netlist_sha", ""),
            })
            sim_records.complete(
                sim_id, dataset_path=dataset_path,
                netlist_sha=(sim.get("design_version") or {}).get("netlist_sha", ""),
                process_exit=sim.get("process_exit", 0),
                dataset_ok=bool(dataset_path),
                controller_trace_ok=sim.get("status") == "done",
                workspace=(sim.get("workspace") or {}).get("path", ""))
        except Exception as exc:  # noqa: BLE001
            stage("sim_record", False, error=f"{type(exc).__name__}: {exc}")
        else:
            stage("sim_record", True, simulation_id=sim_id)

        # ---- 网表级拓扑核对：三端封装支路（含公共端）--------------------
        record = M.get_model_record(model_id)
        build = record.get("build") or {}
        selected = (record.get("fit") or {}).get("selected")
        netlist_path = sim.get("netlist_path") or ""
        if netlist_path and os.path.isfile(netlist_path):
            parsed = _parse_netlist(netlist_path)
            fet = parsed["fet"] or {}
            branches = parsed["branches"]
            problems = []
            if not fet:
                problems.append("网表里找不到 FET 器件行")
            elif fet["nodes"] and fet["nodes"][0] == "0":
                problems.append("FET 公共端（gate）直接接地：漏掉公共端封装支路")
            if build.get("topology") == "packaged_all_three_terminals":
                params = record["fit"]["candidates"][selected]["parameters_fitted_SI"]
                for terminal in ("G", "S", "D"):
                    key_r = f"PKG_R_{terminal}"
                    key_l = f"PKG_L_{terminal}"
                    if key_r not in branches or key_l not in branches:
                        problems.append(f"缺少 {key_r}/{key_l} 支路")
                        continue
                    low = terminal.lower()
                    if not _expect_close(branches[key_r]["R"], params[f"R{low}_Ohm"], 1.0):
                        problems.append(f"{key_r} 阻值与拟合值不符：{branches[key_r]['R']}")
                    if not _expect_close(branches[key_l]["L"], params[f"L{low}_H"], 1e-9):
                        problems.append(f"{key_l} 电感与拟合值不符：{branches[key_l]['L']}")
            else:
                for key in branches:
                    problems.append(f"非封装模型却出现封装支路 {key}")
            stage("netlist_common_branch", not problems,
                  topology=build.get("topology"), branches=sorted(branches),
                  fet_nodes=fet.get("nodes"), problems=problems)
            results["netlist_path"] = netlist_path
            results["netlist_check"] = {"topology": build.get("topology"),
                                        "branches": sorted(branches),
                                        "fet_nodes": fet.get("nodes"),
                                        "problems": problems}
        else:
            stage("netlist_common_branch", False, error="未拿到网表文件路径")

        # 读回真实数据集（与聊天工具同一实现），交给验证引擎
        exprs = [f"{op}(S({i},{j}))" for i, j in ((1, 1), (2, 1), (1, 2), (2, 2))
                 for op in ("real", "imag")]
        traces = ads_ops.read_traces({"path": dataset_path, "expressions": exprs,
                                      "max_points": 0})
        n_points = 0
        for name, payload in (traces.get("traces") or {}).items():
            if isinstance(payload, dict) and payload.get("x"):
                n_points = max(n_points, len(payload["x"]))
        stage("dataset_read", n_points > 0, expressions=len(exprs), points=n_points)

        validation = M.validate_device_model({
            "model_id": model_id, "dataset_path": dataset_path,
            "traces": traces.get("traces"),
            # 来源走**可信执行记录**：run_simulation 的真实返回值在这里落一条
            # sim_records（后端同一条路径），验证只传 simulation_id 引用它。
            # 不再自填 library/cell/netlist_sha/workspace —— 那些是结论，不是输入。
            "simulation_id": sim_id,
            "thresholds": dict(ACCEPT_THRESHOLDS)})
        sub = validation.get("sub_results") or {}
        stage("validate", validation["verdict"] == "validated",
              verdict=validation["verdict"],
              independent=bool(validation.get("independent_numeric")),
              provenance=(validation.get("provenance") or {}).get("state"),
              import_consistency=(sub.get("import_consistency") or {}).get("state"),
              numeric_equivalence=(sub.get("numeric_equivalence") or {}).get("state"),
              datasheet_accuracy=(sub.get("datasheet_accuracy") or {}).get("state"),
              scope_coverage=(sub.get("scope_coverage") or {}).get("state"),
              thresholds=ACCEPT_THRESHOLDS,
              curve_error_max=validation.get("curve_error_max_complex_rmse"),
              validation_scope_hz=validation.get("validation_scope_hz"),
              reasons=validation.get("verdict_reasons"),
              unvalidated=validation.get("unvalidated_scope"))
        results["dataset_path"] = dataset_path
        results["cell"] = spec["cell"]
        results["validation"] = validation

    # ---- ADS 网表等价核对（真实 hpeesofsim，不经 DE 布局/几何门禁）--------
    # 验证的是**模型拓扑与参数的数值等价**，与 DE 建图走同一条计划。
    record = M.get_model_record(model_id)
    if record.get("stage") == "build_planned" or (record.get("fit") or {}).get("selected"):
        run_netlist_equivalence(spec, record, args.workspace, results, stage)

    results["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    (pipeline_dir / "ads_acceptance.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    de.close_workspace()
    executed = {k: v for k, v in results["stages"].items()}
    all_ok = all(s["ok"] for s in executed.values())
    print(f"\nALL_STAGES {'PASS' if all_ok else 'FAIL'} "
          f"({sum(1 for s in executed.values() if s['ok'])}/{len(executed)})", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
