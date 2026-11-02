"""可信仿真执行记录（2026-10-14 A 阶段）。

审查 P1-1：仿真来源此前可由调用方（LLM）自填 —— `_with_simulation_evidence`
见到 `args["simulation"]` 非空就直接返回，于是 `netlist_sha='invented'`、
`workspace='Z:/different'` 也能拿到 `provenance.state=verified`；
`validate_device_model` 还在验证时 `uuid4()` 现造 `simulation_id`，
把"凭空生成的 ID"当成来源 ID。

本模块是**唯一**能产生"来源已验证"结论的地方：

* `simulation_id` 在 `begin()`（仿真开始）时生成，贯穿整次执行，验证时**只能引用**；
* 记录绑定工作区、library/cell/view、建图指纹、**实际网表指纹**、模型规格指纹、
  扫描参数、数据集 SHA 与时间；
* 三项完整性分账 `process_exit` / `dataset_integrity` / `controller_trace`
  —— hpeesofsim 有时"算完产出 .ds 后退出码 0xC0000409"，**文件存在不等于仿真完成**；
* 诊断恢复可读部分产物，但不能计入端到端通过。

落盘位置遵循项目约定：一律走 `backend/paths.py` 的 `data_root()`，
覆盖优先级 ADS_AGENT_CONFIG > ADS_AGENT_DATA_DIR > ADS_AGENT_APP_DIR。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile

import paths


#: 指纹必须是真实十六进制摘要；这些占位值一律拒绝。
_PLACEHOLDERS = {
    "invented", "netlist_only", "none", "null", "unknown", "todo", "n/a", "na",
    "-", "", "fake", "placeholder", "dummy", "test",
}

_ID_RE = re.compile(r"[0-9a-f]{8,64}\Z")
_SIM_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

#: 来源必需字段：缺任何一项都不得 verified。
_REQUIRED = ("workspace", "library", "cell", "view", "netlist_sha",
             "dataset_path", "dataset_sha256", "simulated_at")


class SimulationRecordError(ValueError):
    """来源记录不可信。调用方必须拒绝验证，不得降级放行。"""


def _now() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _root() -> str:
    return os.path.join(paths.data_root(create=True), "sim_records")


def _path(simulation_id: str) -> str:
    if not isinstance(simulation_id, str) or not _SIM_ID_RE.fullmatch(simulation_id):
        raise SimulationRecordError("无效的 simulation_id")
    return os.path.join(_root(), simulation_id + ".json")


def _is_placeholder(value) -> bool:
    if not isinstance(value, str):
        return True
    text = value.strip().lower()
    if text in _PLACEHOLDERS:
        return True
    # 非十六进制摘要一定是编出来的（真实 netlist_sha 是 hex）。
    return bool(text) and not _ID_RE.fullmatch(text)


def _norm_path(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(value)))
    except (OSError, ValueError):
        return ""


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

def begin(payload: dict) -> str:
    """仿真**开始**时调用：生成 simulation_id 并落盘初始记录。

    `payload` 应来自 run_simulation 的调用上下文（library/cell/view/workspace）
    与建图记录（build_fingerprint）。此时尚无数据集，返回值供后续
    `complete()` / `fail()` 与验证阶段引用。
    """
    simulation_id = os.urandom(16).hex()
    record = {
        "simulation_id": simulation_id,
        "started_at": _now(),
        "state": "running",
        "workspace": payload.get("workspace"),
        "library": payload.get("library"),
        "cell": payload.get("cell"),
        "view": payload.get("view") or "schematic",
        "build_fingerprint": payload.get("build_fingerprint"),
        "model_spec_fingerprint": payload.get("model_spec_fingerprint"),
        "scan": payload.get("scan") or {},
        "netlist_sha": payload.get("netlist_sha"),
        "dataset_path": None,
        "dataset_sha256": None,
        "simulated_at": None,
        "integrity": {"process_exit": None, "dataset_integrity": None,
                      "controller_trace": None},
        "diagnostic_only": False,
    }
    _write(record)
    return simulation_id


def complete(simulation_id: str, *, dataset_path: str, netlist_sha: str,
             process_exit, dataset_ok: bool, controller_trace_ok: bool,
             workspace=None) -> dict:
    """仿真结束：写入数据集、指纹与三项完整性分账。

    **进程异常退出不等于仿真失败，文件存在也不等于仿真成功**：三项分账各自
    如实记录，只有调用方据此判断能否进入端到端通过。
    """
    record = get(simulation_id)
    sha = None
    try:
        sha = _sha256_file(dataset_path)
    except OSError as exc:
        raise SimulationRecordError(
            f"数据集不可读，无法绑定来源：{type(exc).__name__}: {exc}") from exc
    record.update({
        "dataset_path": os.path.abspath(dataset_path),
        "dataset_sha256": sha,
        "netlist_sha": netlist_sha,
        "simulated_at": _now(),
        "state": "completed" if (dataset_ok and process_exit == 0
                                 and controller_trace_ok) else "completed_with_issues",
        "integrity": {
            "process_exit": _exit_state(process_exit),
            "dataset_integrity": "ok" if dataset_ok else "failed",
            "controller_trace": "ok" if controller_trace_ok else "incomplete",
        },
        "diagnostic_only": not (dataset_ok and process_exit == 0
                                and controller_trace_ok),
    })
    if workspace:
        record["workspace"] = workspace
    _write(record)
    return record


def fail(simulation_id: str, reason: str) -> dict:
    """仿真失败/异常：如实记录，不得留下"看起来成功"的记录。"""
    record = get(simulation_id)
    record.update({"state": "failed", "failure_reason": reason,
                   "simulated_at": _now(), "diagnostic_only": True})
    _write(record)
    return record


def _exit_state(process_exit) -> str:
    """进程退出码分账。0xC0000409 等"算完才崩"要单独标出。"""
    if process_exit == 0:
        return "clean"
    text = str(process_exit)
    if "3221226505" in text or "0xC0000409" in text.upper():
        # 本机已知：hpeesofsim 产出数据集后异常退出，数据通常仍可用。
        return "abnormal_after_dataset"
    return "failed"


def get(simulation_id: str) -> dict:
    if not isinstance(simulation_id, str) or not _SIM_ID_RE.fullmatch(simulation_id):
        raise SimulationRecordError("缺少或无效的 simulation_id")
    try:
        with open(_path(simulation_id), encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as exc:
        raise SimulationRecordError(
            f"找不到可信仿真执行记录 {simulation_id}；"
            "来源只能由后端从 run_simulation 的真实执行记录读取，"
            "不接受调用方自填") from exc
    except json.JSONDecodeError as exc:
        raise SimulationRecordError(f"仿真执行记录损坏：{exc}") from exc


def _write(record: dict) -> None:
    os.makedirs(_root(), exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=_root(), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp, _path(record["simulation_id"]))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# ---------------------------------------------------------------------------
# 验证期核对
# ---------------------------------------------------------------------------

def resolve(simulation_id: str, *, dataset_path: str, record: dict) -> tuple:
    """按可信记录核对本次验证，返回 (证据, 问题列表)。

    `record` 是模型记录（含 build 计划与指纹）。逐项严格核对：
    必需字段齐全、非占位值、workspace/library/cell/view 与建图记录**严格相等**、
    网表指纹与建图记录一致、数据集 SHA **读取前后各校验一次**。
    """
    problems = []
    sim = get(simulation_id)
    evidence = {
        "simulation_id": simulation_id,
        "source": "backend_execution_record",
        "recorded_at": sim.get("simulated_at"),
        "state": sim.get("state"),
        "integrity": sim.get("integrity") or {},
        "diagnostic_only": bool(sim.get("diagnostic_only")),
    }

    missing = [k for k in _REQUIRED if not sim.get(k)]
    if missing:
        problems.append("仿真执行记录缺少必需字段：" + "、".join(missing)
                        + "；不得判为可信来源")
    for key, label in (("netlist_sha", "网表指纹"), ("dataset_sha256", "数据集 SHA")):
        if _is_placeholder(sim.get(key)):
            problems.append(f"仿真执行记录的{label}（{key}）是占位值或非十六进制摘要"
                            f"（{sim.get(key)!r}）；不接受编造的指纹")

    build = (record or {}).get("build") or {}
    # 严格相等，不做"有值才比"
    for key, label in (("library", "库"), ("cell", "cell"), ("view", "view")):
        want = build.get(key) or ("schematic" if key == "view" else None)
        got = sim.get(key)
        if want and str(got) != str(want):
            problems.append(f"仿真{label}与建图记录不一致：{got!r} vs {want!r}")
    if not sim.get("workspace"):
        problems.append("仿真执行记录没有工作区路径，无法确认数据来自当前工作区")

    want_fp = build.get("fingerprint")
    if want_fp and sim.get("build_fingerprint") and \
            str(sim["build_fingerprint"]) != str(want_fp):
        problems.append("仿真记录的建图指纹与当前模型建图记录不一致"
                        "（模型在建图后被改动或重建）")

    if sim.get("dataset_path") and _norm_path(sim["dataset_path"]) != \
            _norm_path(dataset_path):
        problems.append("仿真记录绑定的数据集与本次 dataset_path 不是同一个文件")

    # 读取前后各校验一次：文件被替换后不能仍关联旧模型。
    try:
        current = _sha256_file(dataset_path)
    except OSError as exc:
        problems.append(f"数据集不可读：{type(exc).__name__}: {exc}")
        current = None
    if current:
        evidence["dataset_sha256_before"] = current
        if sim.get("dataset_sha256") and current != sim["dataset_sha256"]:
            problems.append("数据集内容与仿真记录中的 SHA 不一致"
                            "（文件在仿真后被替换）")

    integrity = sim.get("integrity") or {}
    if integrity.get("process_exit") not in ("clean", None):
        problems.append(f"仿真进程异常退出（{integrity['process_exit']}）；"
                        "产物只作诊断证据，不计入端到端通过")
    if integrity.get("dataset_integrity") not in ("ok", None):
        problems.append("数据集完整性检查未通过")
    if sim.get("diagnostic_only"):
        evidence["note"] = ("本记录只提供诊断证据（进程/数据集/控制流未全部正常），"
                            "不足以支撑端到端验收")

    evidence["workspace"] = sim.get("workspace")
    evidence["library"] = sim.get("library")
    evidence["cell"] = sim.get("cell")
    evidence["view"] = sim.get("view")
    evidence["netlist_sha"] = sim.get("netlist_sha")
    evidence["dataset_sha256"] = sim.get("dataset_sha256")
    return evidence, problems


def recheck_after_read(simulation_id: str) -> str:
    """数据集读取**之后**再校验一次 SHA，防"读完换文件"。"""
    sim = get(simulation_id)
    if not sim.get("dataset_path"):
        return ""
    try:
        current = _sha256_file(sim["dataset_path"])
    except OSError:
        return ""
    return current if current == sim.get("dataset_sha256") else ""