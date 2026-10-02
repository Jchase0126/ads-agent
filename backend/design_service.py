"""设计闭环编排：跑仿真 → 读真实曲线 → 确定性评估 → 组装 DesignJob。

这一层是"模型说"与"数据说"之间的闸门：模型只能提交**设计与指标定义**
（要什么设计、看哪些表达式、判据是什么），实测值与达标结论一律由这里
调用 ``design_metrics`` 从 ``.ds`` 数据算出来。模型拿到的是评估结果，
所以它无法编造数值、也无法宣称达标。

流程（每一步都落到 DesignJob 上，失败时保留已完成的部分）：

    planned  → run_simulation        → artifacts: output_dir / netlist / dataset
    simulating → read_traces         → 真实数组（完整点数）
    evaluated → design_metrics.evaluate → 目标 / 实测 / 判定 / 频点
    done | evaluated                 → 存 design_jobs/<job_id>.json

失败路径：``sim`` 记下 running/failed + 错误原文，``artifacts`` 保留已经
生成的网表与输出目录，job 进 failed —— **不清空任何已有产物**。
"""

import os
import threading

import adslog
import design_job as dj
import design_metrics as dm
import tools as tools_mod

log = adslog.get("backend.design")

# ---------------------------------------------------------------------------
# 并发保护（2026-10-02）：HTTP 服务是多线程的，同一个 job 的 resimulate 和
# reload 并发跑会基于旧快照互相覆盖；不同项目对同一设计的写入/仿真也要互斥。
# ---------------------------------------------------------------------------
_JOB_LOCKS_GUARD = threading.Lock()
_JOB_LOCKS: dict = {}
_DESIGN_LOCKS_GUARD = threading.Lock()
_DESIGN_LOCKS: dict = {}


def job_lock(job_id: str):
    """同一设计任务的串行锁（上下文管理器）。"""
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.setdefault(str(job_id or ""), threading.Lock())
        if len(_JOB_LOCKS) > 500:
            for k in list(_JOB_LOCKS)[:250]:
                _JOB_LOCKS.pop(k, None)
    return lock


def design_ref_lock(design: dict):
    """同一设计引用（workspace+lib+cell+view）的互斥锁，串行化写入与仿真。"""
    d = design or {}
    ref = "|".join(str(d.get(k) or "") for k in
                   ("workspace", "library", "cell", "view"))
    with _DESIGN_LOCKS_GUARD:
        lock = _DESIGN_LOCKS.setdefault(ref, threading.Lock())
        if len(_DESIGN_LOCKS) > 500:
            for k in list(_DESIGN_LOCKS)[:250]:
                _DESIGN_LOCKS.pop(k, None)
    return lock

# 指标评估必须使用完整曲线；只有持久化给结果页的序列才降采样。
# read_traces 对话工具仍保留默认上限，0 是设计闭环专用的完整读取模式。
MAX_TRACE_POINTS = 0
# 存进 DesignJob、供结果页画图与查点的点数
DISPLAY_POINTS = 600

DEFAULT_VIEW = "schematic"

# 指标定义里允许出现的字段（白名单）。
# 实测值 / 判定（actual / pass / note / exact）**不在**这里：它们只能由
# design_metrics 从真实数据算出来，模型填了也会被丢掉。
METRIC_KEYS = ("id", "label", "kind", "expr", "target", "comparator",
               "unit", "at_hz", "at", "at_unit", "threshold")


class DesignError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# spec 解析
# ---------------------------------------------------------------------------

def build_spec(raw: dict) -> dict:
    """把工具/接口传进来的原始参数整理成规范 spec（缺什么就报什么）。"""
    raw = raw or {}
    design = raw.get("design") or {}
    if not design:
        design = {
            "library": raw.get("library"),
            "cell": raw.get("cell"),
            "view": raw.get("view"),
        }
    library = (design.get("library") or "").strip()
    cell = (design.get("cell") or "").strip()
    if not library or not cell:
        raise DesignError("必须给出设计引用：design.library 与 design.cell（如 AI_lib / Amp24G）")

    metrics = []
    for m in (raw.get("metrics") or []):
        if not isinstance(m, dict):
            continue
        # 只接受**定义**字段。模型有时会顺手把 actual/pass 一起填进来 ——
        # 那是编造实测值，必须在这里就丢掉，绝不能流进任务里。
        metric = {k: m[k] for k in METRIC_KEYS if k in m}
        if not metric.get("kind"):
            metric["kind"] = "min_in_band"
        if not metric.get("expr"):
            continue
        metrics.append(metric)
    if not metrics:
        raise DesignError(
            "必须给出至少一项指标定义（metrics）："
            "每项含 kind（min_in_band/max_in_band/mean_in_band/flatness_in_band/"
            "bandwidth_above/value_at）、expr（数据集表达式，如 dB(S(2,1))）、"
            "target（目标值）、unit（单位）"
        )

    # 要画的曲线：显式声明的优先，否则按指标里用到的表达式去重
    traces = []
    seen = set()
    for t in (raw.get("traces") or []):
        if isinstance(t, dict) and t.get("expr") and t["expr"] not in seen:
            seen.add(t["expr"])
            traces.append({"expr": t["expr"], "label": t.get("label") or t["expr"],
                           "unit": t.get("unit") or ""})
    for m in metrics:
        if m["expr"] not in seen:
            seen.add(m["expr"])
            traces.append({"expr": m["expr"], "label": m.get("label") or m["expr"],
                           "unit": m.get("unit") or ""})

    band = dj.normalize_band(raw.get("band") or raw.get("freq_range") or {})
    if not band and any(m["kind"] in dm.NEEDS_BAND for m in metrics):
        raise DesignError(
            "带内指标需要有效频段 band：请使用数值与单位分开的格式，"
            '例如 {"start": 2.3, "stop": 2.5, "unit": "GHz"}'
        )

    return {
        "job_id": raw.get("job_id") or "",
        "title": (raw.get("title") or "").strip(),
        "requirement": (raw.get("requirement") or "").strip(),
        "band": band,
        "metrics": metrics,
        "traces": traces,
        # 评估口径选项：partial_band=True 表示频段覆盖不足时允许只评估已覆盖
        # 部分（结论会标注实际范围并降级 exact）。默认 False = 无法判定。
        "eval_options": {
            "partial_band": bool(raw.get("partial_band", False)),
        },
        "design": {
            "library": library,
            "cell": cell,
            "view": (design.get("view") or DEFAULT_VIEW).strip() or DEFAULT_VIEW,
            "workspace": (design.get("workspace") or "").strip(),
        },
        "simulate": bool(raw.get("simulate", True)),
        "reuse_dataset": bool(raw.get("reuse_dataset", False)),
        "dataset_path": (raw.get("dataset_path") or "").strip(),
        "output_dir": (raw.get("output_dir") or "").strip(),
        "notes": (raw.get("notes") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def _norm_ws_path(p) -> str:
    """工作区路径归一化：Windows 下大小写与分隔符差异不算"换工作区"。

    注意 normpath("") 会返回 "."，空值必须先判掉。
    """
    p = str(p or "").strip()
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


def verify_workspace(cfg: dict, job: dj.DesignJob):
    """核对任务记录的工作区与 ADS 当前打开的工作区。

    任务跨工作区复用时（切换工作区后重新仿真/评估/打开同名设计），不核对
    就会操作**另一个工作区里的同名设计**。返回错误文案（应停止）或 None。
    ADS 不可达时返回 None —— 后续仿真步骤会给出更具体的错误。
    """
    try:
        info = tools_mod.call(cfg, "get_workspace_info", {})
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict) or not info.get("workspace_open"):
        return None
    current = _norm_ws_path(info.get("path"))
    recorded_raw = str((job.design or {}).get("workspace") or "").strip()
    recorded = _norm_ws_path(recorded_raw)
    if not current:
        return None
    if recorded and recorded != current:
        return (
            "工作区不一致，已停止执行：\n"
            f"- 任务记录的工作区：{recorded_raw}\n"
            f"- ADS 当前打开的工作区：{info.get('path')}\n"
            "两个工作区可能存在同名的 library/cell，继续执行会改错设计。"
            "恢复方式：在 ADS 中切回任务记录的工作区后重试；"
            "或明确确认要在当前工作区对同名设计重新执行（旧结果页会保留）。"
        )
    if not recorded:
        # 第一次执行：把当前工作区记为任务的绑定工作区
        job.set_design(workspace=str(info.get("path")))
    return None


def run_design(cfg: dict, spec: dict, project_root: str,
               job: dj.DesignJob | None = None, on_step=None,
               cancel_event=None) -> dj.DesignJob:
    """同一设计引用串行化的入口；实际编排在 _run_design_locked。"""
    ref = dict((spec or {}).get("design") or (job.design if job is not None else {}) or {})
    with design_ref_lock(ref):
        return _run_design_locked(cfg, spec, project_root, job=job,
                                  on_step=on_step, cancel_event=cancel_event)


def _run_design_locked(cfg: dict, spec: dict, project_root: str,
                       job: dj.DesignJob | None = None, on_step=None,
                       cancel_event=None) -> dj.DesignJob:
    """执行一次完整的设计评估，返回（并持久化）DesignJob。

    ``job`` 不为 None 时在其基础上继续（保留原 job_id 与迭代历史）。
    ``on_step(stage, text)`` 用于向前端推进度。
    ``cancel_event`` 置位后：在阶段边界停止（仿真开始前 / 读取前 / 评估前），
    已完成的产物与备份一律保留，绝不自动重复写入设计。

    检查点持久化（2026-10-02）：planned / simulating（含已生成的产物路径）/
    数据读取完成 / evaluated 每个关键阶段都落盘 —— 后端在任何一步之后退出，
    重启都能恢复到真实状态，而不是只剩内存里的一份。
    """
    import time as _time
    import uuid as _uuid

    _timing = {}

    def step(stage, text=""):
        if on_step:
            try:
                on_step(stage, text)
            except Exception:  # noqa: BLE001
                pass

    def cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    def _cancel_stop(reason: str) -> dj.DesignJob:
        """取消收尾：保留一切已完成的东西，只如实标记状态。"""
        job.fail(reason)
        _persist(project_root, job)
        step(job.stage, reason)
        log.info("设计任务 %s 已取消: %s", job.job_id, reason)
        return job

    if job is None:
        job = dj.DesignJob(
            requirement=spec.get("requirement", ""),
            band=spec.get("band"),
            metrics=spec.get("metrics"),
            metric_specs=spec.get("metrics"),
            design=spec.get("design"),
            title=spec.get("title") or _auto_title(spec),
        )
        if job.stage == dj.STAGE_DRAFT:
            job.transition(dj.STAGE_PLANNED, "已确定指标与设计引用")
    else:
        if spec.get("band"):
            job.band = spec["band"]
        if spec.get("metrics"):
            job.metric_specs = [dict(m) for m in spec["metrics"]]
            job.metrics = [dict(m) for m in spec["metrics"]]
        job.set_design(**spec.get("design", {}))
        if job.stage not in (dj.STAGE_PLANNED, dj.STAGE_BUILDING):
            try:
                job.transition(dj.STAGE_PLANNED, "重新执行")
            except dj.IllegalTransition:
                # 极少数起点（例如上一次卡在 simulating）不能直接回 planned：
                # 不改 stage 硬塞，继续按当前阶段往下走即可
                log.warning("任务 %s 从 %s 重跑，未回到 planned", job.job_id, job.stage)

    # 评估口径随任务走（reload/resimulate 时沿用，不丢失 partial_band 等）
    job.eval_options = dict(spec.get("eval_options") or {})

    # ---- 0. 工作区绑定核查（跨工作区同名设计防误操作）--------------------
    ws_error = verify_workspace(cfg, job)
    if ws_error:
        job.fail(ws_error)
        _persist(project_root, job)
        step(job.stage, "工作区不一致，已停止")
        log.error("设计任务 %s 工作区不一致，已停止", job.job_id)
        return job

    # 检查点 1：指标与设计引用已确定
    _persist(project_root, job)

    iteration = {
        "design_ref": job.design_ref(),
        "band": job.band.get("label", ""),
        "metrics_spec": [dict(m) for m in job.metric_specs],
    }

    design = job.design
    expressions = [t["expr"] for t in spec.get("traces", [])]
    dataset_path = spec.get("dataset_path", "")
    output_dir = spec.get("output_dir", "")

    # ---- 1. 仿真 ---------------------------------------------------------
    if spec.get("simulate") and not spec.get("reuse_dataset"):
        if cancelled():
            return _cancel_stop("用户已取消：仿真未开始")
        job.transition(dj.STAGE_SIMULATING, "开始仿真")
        step(dj.STAGE_SIMULATING, "正在生成网表并仿真…")
        sim_job_id = f"sim_{job.job_id}_{_uuid.uuid4().hex[:8]}"
        job.set_sim(status="running", started_at=dj.utc_now(), ads_job_id=sim_job_id)
        _persist(project_root, job)   # 检查点 2：仿真进行中（含作业标识）
        _t_sim = _time.perf_counter()
        try:
            result = tools_mod.call(cfg, "run_simulation", {
                "library": design.get("library"),
                "cell": design.get("cell"),
                "view": design.get("view", DEFAULT_VIEW),
            }, job_id=sim_job_id)
        except Exception as e:  # noqa: BLE001 — 失败也要留住已生成的产物
            message = str(e)
            job.set_sim(status="failed", error=message, finished_at=dj.utc_now())
            _salvage_paths(job, message)
            iteration.update({"sim_status": "failed", "error": message})
            job.add_iteration(iteration)
            job.fail(f"仿真失败：{message}")
            log.error("设计任务 %s 仿真失败: %s", job.job_id, message[:400])
            _persist(project_root, job)
            step(job.stage, "仿真失败（已保留已生成的网表与目录）")
            return job
        _timing["sim_s"] = round(_time.perf_counter() - _t_sim, 3)

        dataset_path = result.get("dataset_path") or ""
        output_dir = result.get("output_dir") or ""
        # 产物路径尽早落盘：仿真刚完成、读取还没开始，路径就已经可恢复
        job.set_artifacts(output_dir=output_dir,
                          netlist_path=result.get("netlist_path") or "",
                          dataset_path=dataset_path)
        job.set_sim(status=result.get("status") or "done",
                    finished_at=dj.utc_now(),
                    variables=result.get("variables") or [],
                    audit=result.get("audit") or {},
                    output_dir=output_dir)
        # 设计版本与工作区：仿真时"是谁、在哪"的证据，供结果复用验证与
        # 跨工作区防误操作使用（结果页也会展示）
        if result.get("design_version"):
            job.design_version = dict(result["design_version"])
        ws_path = (result.get("workspace") or {}).get("path") or ""
        if ws_path:
            job.set_sim(workspace=ws_path)
        _persist(project_root, job)   # 检查点 3：产物路径已记录
        if result.get("status") == "no_dataset" or not dataset_path:
            iteration.update({"sim_status": "no_dataset"})
            job.add_iteration(iteration)
            job.fail("仿真完成但没有产出 .ds 数据集："
                     + (result.get("hint") or "请检查网表与输出目录"))
            _persist(project_root, job)
            step(job.stage, "仿真没有产出数据集")
            return job
        if cancelled():
            return _cancel_stop("用户已取消：仿真已完成、数据集已保留，"
                                "已停止后续读取与评估（可稍后重新评估）")
        job.transition(dj.STAGE_EVALUATED, "仿真完成，开始评估指标")
    else:
        # 复用已有数据集（"刷新数据" / 重启后重新评估）
        dataset_path = dataset_path or job.artifacts.get("dataset_path", "")
        output_dir = output_dir or job.artifacts.get("output_dir", "")
        if not dataset_path:
            job.add_iteration(iteration)
            job.fail("没有可用的数据集路径：请先执行一次仿真，或提供 dataset_path")
            _persist(project_root, job)
            return job
        job.set_artifacts(dataset_path=dataset_path, output_dir=output_dir)
        job.set_sim(status="reused", finished_at=dj.utc_now(), output_dir=output_dir)
        _to_evaluated(job, "复用已有数据集")

    iteration["dataset_path"] = dataset_path
    iteration["output_dir"] = output_dir

    # ---- 2. 读真实曲线 ---------------------------------------------------
    if cancelled():
        return _cancel_stop("用户已取消：已停止读取与评估（数据集保留在原位）")
    step(dj.STAGE_EVALUATED, "正在读取仿真数据…")
    _t_read = _time.perf_counter()
    try:
        raw = tools_mod.call(cfg, "read_traces", {
            "path": dataset_path,
            "expressions": expressions,
            "max_points": MAX_TRACE_POINTS,
        })
    except Exception as e:  # noqa: BLE001
        message = str(e)
        job.set_sim(status="done", read_error=message)
        iteration.update({"read_error": message})
        job.add_iteration(iteration)
        job.fail(f"读取数据集失败：{message}")
        _persist(project_root, job)
        step(job.stage, "读取数据集失败")
        return job
    _timing["read_s"] = round(_time.perf_counter() - _t_read, 3)

    available = raw.get("variables") or []
    payloads = raw.get("traces") or {}
    _t_norm = _time.perf_counter()
    traces = dm.normalize_traces(payloads, max_points=DISPLAY_POINTS)
    _timing["normalize_s"] = round(_time.perf_counter() - _t_norm, 3)
    for name, payload in payloads.items():
        if isinstance(payload, dict) and payload.get("error"):
            log.warning("表达式 %s 读取失败: %s", name, payload["error"])

    # 检查点 4：数据已读取（曲线元信息落盘，完整数组太大只留 display）
    job.set_artifacts(traces=_strip_traces(traces),
                      dataset_source=dataset_path,
                      available_variables=available[:200])
    if cancelled():
        _persist(project_root, job)
        return _cancel_stop("用户已取消：数据已读取并保存，评估未执行"
                            "（可稍后重新评估）")

    # ---- 3. 确定性评估 ---------------------------------------------------
    _t_eval = _time.perf_counter()
    evaluation = dm.evaluate(traces, job.metric_specs, job.band,
                             available=available,
                             options=spec.get("eval_options") or {})
    _timing["evaluate_s"] = round(_time.perf_counter() - _t_eval, 3)
    job.set_metrics(evaluation["results"])
    summary = evaluation["summary"]
    iteration.update({
        "sim_status": job.sim.get("status"),
        "metrics": [
            {"id": r.get("id"), "label": r.get("label"), "actual": r.get("actual"),
             "target": r.get("target"), "unit": r.get("unit"), "pass": r.get("pass"),
             "at": r.get("at"), "note": r.get("note", "")}
            for r in evaluation["results"]
        ],
        "summary": summary,
    })
    job.add_iteration(iteration)
    job.timing = dict(_timing)

    if summary["verdict"] == "pass":
        job.transition(dj.STAGE_DONE, "全部指标达标")
    else:
        job.transition(dj.STAGE_EVALUATED,
                       f"评估完成：{summary['n_passed']} 项达标 / "
                       f"{summary['n_failed']} 项未达标 / {summary['n_unknown']} 项无法判定")
    _persist(project_root, job)   # 检查点 5：评估完成
    step(job.stage, f"评估完成（{summary['verdict']}）")
    log.info("设计任务 %s 评估完成: %s 耗时=%s", job.job_id, summary, _timing)
    return job


def reload_job(cfg: dict, project_root: str, job_id: str, on_step=None,
               cancel_event=None) -> dj.DesignJob:
    """重新读取已记录的数据集并重算指标（不重新仿真）。"""
    job = dj.load_job(project_root, job_id)
    if job is None:
        raise DesignError(f"找不到设计任务 {job_id}（design_jobs/ 下没有该文件）")
    spec = {
        "job_id": job.job_id,
        "band": job.band,
        "metrics": _stored_metric_specs(job),
        "design": job.design,
        "traces": _traces_from_job(job),
        "simulate": False,
        "reuse_dataset": True,
        "eval_options": dict(job.eval_options or {}),
        "dataset_path": job.artifacts.get("dataset_path", ""),
        "output_dir": job.artifacts.get("output_dir", ""),
    }
    return run_design(cfg, spec, project_root, job=job, on_step=on_step,
                      cancel_event=cancel_event)


def resimulate(cfg: dict, project_root: str, job_id: str, on_step=None,
               cancel_event=None) -> dj.DesignJob:
    """按原指标与设计引用**重新仿真**并评估（结果页的"重新仿真"入口）。"""
    job = dj.load_job(project_root, job_id)
    if job is None:
        raise DesignError(f"找不到设计任务 {job_id}（design_jobs/ 下没有该文件）")
    spec = {
        "job_id": job.job_id,
        "band": job.band,
        "metrics": _stored_metric_specs(job),
        "design": job.design,
        "traces": _traces_from_job(job),
        "simulate": True,
        "reuse_dataset": False,
        "eval_options": dict(job.eval_options or {}),
    }
    return run_design(cfg, spec, project_root, job=job, on_step=on_step,
                      cancel_event=cancel_event)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _stored_metric_specs(job: dj.DesignJob) -> list:
    """旧任务没有独立定义时，只恢复能确认的条件。"""
    specs = []
    for metric in job.metric_specs:
        spec = {k: metric[k] for k in METRIC_KEYS if k in metric}
        if "pass" in metric:  # 旧版任务把评估结果直接存进 metrics
            # 原始 value_at 频点和带宽门限已无法从结果中还原。
            if spec.get("kind") == "value_at":
                spec.pop("at_hz", None)
                spec.pop("at", None)
            elif spec.get("kind") == "bandwidth_above":
                spec.pop("threshold", None)
        specs.append(spec)
    return specs

def _to_evaluated(job: dj.DesignJob, note: str) -> None:
    """把任务推进到 evaluated。

    状态机是显式的，个别起点（如 failed）不能直接跳到 evaluated —— 那就先按
    允许的路径回到 planned 再进，而不是硬改 stage。
    """
    if job.stage == dj.STAGE_EVALUATED:
        return
    try:
        job.transition(dj.STAGE_EVALUATED, note)
        return
    except dj.IllegalTransition:
        pass
    if job.can_transition(dj.STAGE_PLANNED):
        job.transition(dj.STAGE_PLANNED, note)
    job.transition(dj.STAGE_EVALUATED, note)


def _traces_from_job(job: dj.DesignJob) -> list:
    stored = (job.artifacts or {}).get("traces") or {}
    if stored:
        return [{"expr": name, "label": t.get("y_name") or name,
                 "unit": t.get("y_unit") or ""}
                for name, t in stored.items()]
    return [{"expr": m.get("expr"), "label": m.get("label") or m.get("expr"),
             "unit": m.get("unit") or ""}
            for m in job.metrics if m.get("expr")]


def _strip_traces(traces: dict) -> dict:
    """结果页持久化用的曲线数据：display 序列 + 完整/显示点的完整标识。

    刻意保留（结果页要如实回答"这是完整数据还是采样点"）：
      n_points / n_points_raw   完整有效点数 / 数据集原始点数
      display_method / n_display 显示层降采样算法与点数（none = 未降采样）
      quality                   丢弃点数与原因、覆盖范围、最大采样间隔
      source                    数据来源（.ds 路径）
    完整数组 x/y 不落盘 —— 指标已经算完，复查从 .ds 重读（见 /design/export）。
    """
    out = {}
    for name, t in (traces or {}).items():
        display = t.get("display") or {"x": t.get("x", []), "y": t.get("y", []),
                                       "method": "none"}
        out[name] = {
            "x_name": t.get("x_name", ""),
            "x_unit": t.get("x_unit", ""),
            "y_name": t.get("y_name", ""),
            "y_unit": t.get("y_unit", ""),
            "n_points": t.get("n_points", 0),
            "n_points_raw": t.get("n_points_raw", 0),
            "truncated": bool(t.get("truncated")),
            "source": t.get("source", ""),
            "n_display": len(display.get("x", []) or []),
            "display_method": display.get("method", "none"),
            "quality": t.get("quality") or {},
            "x": display.get("x", []),
            "y": display.get("y", []),
        }
    return out


def _salvage_paths(job: dj.DesignJob, message: str) -> None:
    """从仿真报错文本里抢救产物路径。

    ``ads_ops._simulate`` 的失败信息里带了
    ``网表已保存到: ...`` 与 ``输出目录: ...`` —— 失败时这两样其实已经存在，
    丢掉它们就等于丢掉"已经生成的原理图与网表"。
    """
    for key, label in (("output_dir", "输出目录"), ("netlist_path", "网表已保存到")):
        for line in str(message).splitlines():
            line = line.strip()
            if line.startswith(label):
                value = line.split(":", 1)[-1].strip()
                if value and value != "(写入失败)":
                    job.set_artifacts(**{key: value})
                    break


def _auto_title(spec: dict) -> str:
    design = spec.get("design") or {}
    band = (spec.get("band") or {}).get("label", "")
    cell = design.get("cell") or "设计"
    return f"{cell} 设计评估" + (f"（{band}）" if band else "")


def _persist(project_root: str, job: dj.DesignJob) -> str:
    """保存任务；失败时把错误记到 job.save_errors 上（不只进日志）。

    接口层据此返回 save_ok=false —— "计算已完成"和"结果未成功保存"必须
    分开报告，绝不能让用户以为重启后还能恢复一份根本没落盘的结果。
    """
    try:
        return dj.save_job(project_root, job)
    except OSError as e:
        message = f"{type(e).__name__}: {e}"
        log.error("保存设计任务失败 %s: %s", job.job_id, message)
        job.save_errors.append({"at": dj.utc_now(), "error": message,
                                "stage": job.stage})
        if len(job.save_errors) > 20:
            del job.save_errors[:-20]
        return ""


def recover_interrupted(project_root: str) -> list:
    """启动时把上次中断的"运行中"任务恢复为真实状态。

    后端可能在仿真/读数中途退出 —— 磁盘上的任务还停在 simulating。
    没有证据表明它跑完了，所以一律标记为"上次运行中断，待确认"：
    产物路径原样保留，不宣称完成，也**绝不自动重新执行写入**。
    返回被处理的 job_id 列表。
    """
    recovered = []
    for job_id in dj.list_job_ids(project_root):
        job, _info = dj.load_job_ex(project_root, job_id)
        if job is None:
            continue
        running = (job.sim.get("status") == "running"
                   or job.stage in (dj.STAGE_BUILDING, dj.STAGE_SIMULATING))
        if not running:
            continue
        job.sim["status"] = "interrupted"
        job.sim["finished_at"] = dj.utc_now()
        job.fail("上次运行中断，待确认：后端在任务执行中退出，"
                 "产物已保留；请查看产物后重新仿真/评估，不能默认视为完成")
        job.notes.append({
            "at": dj.utc_now(), "stage": job.stage,
            "text": "启动恢复：检测到任务中断于执行中途（stage=%s, sim=%s）"
                    % (job.stage, job.sim.get("status")),
        })
        _persist(project_root, job)
        recovered.append(job_id)
    if recovered:
        log.warning("启动恢复：%d 个中断任务已标记为待确认 %s",
                    len(recovered), recovered)
    return recovered


def jobs_root(project_root: str) -> str:
    return dj.job_dir(project_root)


def project_root_of(cfg: dict) -> str:
    import config as config_mod
    return config_mod.PROJECT_ROOT


def job_file(project_root: str, job_id: str) -> str:
    return dj.job_path(project_root, job_id)


def delete_job(project_root: str, job_id: str) -> bool:
    return dj.delete_job(project_root, job_id)


def exists(project_root: str, job_id: str) -> bool:
    return os.path.exists(dj.job_path(project_root, job_id))
