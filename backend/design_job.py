"""DesignJob —— 结构化设计任务：明确的状态流转 + 持久化（纯标准库，不依赖 ADS / Qt）。

为什么需要它
------------
"设计一个 2.4 GHz 放大器，增益 ≥15 dB，S11 < −10 dB" 这类要求不能只留在聊天
记录里：它要能被**检查**（指标定义明确）、被**追踪**（跑到哪一步了）、被
**复现**（设计引用 + 产物路径 + 迭代记录），还要能在 ADS 重启后重新打开。

所以把一轮设计固化成 ``DesignJob``：

    requirement   原始要求原文（用户怎么说的，原样留着）
    band          频段（统一换算成 Hz 保存，同时记住用户声明的单位）
    metrics       指标定义（每项：表达式 / 判据 / 目标值 / 单位）—— 可检查
    design        设计引用（工作区 / 库 / cell / view）—— 可再次打开
    stage         执行阶段（见下面的状态机）
    iterations    每次尝试的记录（参数、数据集、指标结果、结论）
    artifacts     产物路径（输出目录 / 网表 / 数据集 / 曲线数据）
    sim           仿真状态（running / done / failed / timeout + 错误原文）

状态流转是**显式**的：非法流转直接抛 ``IllegalTransition``，不会悄悄把任务
从 failed 改成 done。关键约束是"失败也要保留已生成的原理图和仿真结果"——
``fail()`` 只改 stage 和 error，绝不清空 design / artifacts / iterations。

持久化：``design_jobs/<job_id>.json``（临时文件 + ``os.replace`` 原子替换），
与 ``projects.json`` 里那条结果页记录通过 ``job_id`` 关联。
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import threading

SCHEMA_VERSION = 2
# v1 -> v2（2026-10-02）：新增 save_errors / timing / eval_options 字段，
# 任务文件保存前自动留一份 .bak；旧文件读取时由 from_dict 迁移并记录说明。

# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

STAGE_DRAFT = "draft"            # 刚记录要求，还没定指标
STAGE_PLANNED = "planned"        # 指标 / 频段 / 设计引用已确定
STAGE_BUILDING = "building"      # 正在创建或修改原理图
STAGE_SIMULATING = "simulating"  # 仿真进行中
STAGE_EVALUATED = "evaluated"    # 指标已算出（可能有未达标项）
STAGE_DONE = "done"              # 全部指标达标
STAGE_FAILED = "failed"          # 出错中止（产物保留）

STAGES = (
    STAGE_DRAFT, STAGE_PLANNED, STAGE_BUILDING,
    STAGE_SIMULATING, STAGE_EVALUATED, STAGE_DONE, STAGE_FAILED,
)

# 允许的流转。刻意把"未达标 -> 再迭代"写成 evaluated -> building / simulating，
# 这样调参复测是一条正常路径，而不是靠改 stage 硬塞。
# 允许的"回退"（evaluated/done -> planned）是为了"重新规划后重跑"，
# 但仍然拒绝无意义的跳变（例如 draft -> done）。
TRANSITIONS = {
    STAGE_DRAFT: (STAGE_PLANNED, STAGE_FAILED),
    STAGE_PLANNED: (STAGE_BUILDING, STAGE_SIMULATING, STAGE_EVALUATED, STAGE_FAILED),
    STAGE_BUILDING: (STAGE_PLANNED, STAGE_SIMULATING, STAGE_EVALUATED, STAGE_FAILED),
    STAGE_SIMULATING: (STAGE_BUILDING, STAGE_EVALUATED, STAGE_FAILED),
    STAGE_EVALUATED: (STAGE_DONE, STAGE_PLANNED, STAGE_BUILDING,
                      STAGE_SIMULATING, STAGE_FAILED),
    STAGE_DONE: (STAGE_PLANNED, STAGE_BUILDING, STAGE_SIMULATING,
                 STAGE_EVALUATED, STAGE_FAILED),
    STAGE_FAILED: (STAGE_PLANNED, STAGE_BUILDING, STAGE_SIMULATING,
                   STAGE_EVALUATED, STAGE_FAILED),
}

STAGE_LABELS = {
    STAGE_DRAFT: "已记录要求",
    STAGE_PLANNED: "已确定指标",
    STAGE_BUILDING: "正在搭建/修改原理图",
    STAGE_SIMULATING: "仿真进行中",
    STAGE_EVALUATED: "已评估指标",
    STAGE_DONE: "全部指标达标",
    STAGE_FAILED: "失败（产物已保留）",
}

# 已经跑完的状态（可以拿来做结果页）
FINISHED_STAGES = (STAGE_EVALUATED, STAGE_DONE, STAGE_FAILED)


class IllegalTransition(RuntimeError):
    """非法的状态流转。"""


# ---------------------------------------------------------------------------
# 频率单位
# ---------------------------------------------------------------------------

FREQ_UNITS = {
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12,
}
FREQ_ALIASES = {
    "": "hz", "h": "hz", "hz": "hz", "hertz": "hz",
    "k": "khz", "khz": "khz", "kilohertz": "khz",
    "m": "mhz", "mhz": "mhz", "megahertz": "mhz",
    "g": "ghz", "ghz": "ghz", "gigahertz": "ghz",
    "t": "thz", "thz": "thz", "terahertz": "thz",
}


def freq_scale(unit: str):
    """单位 -> 相对 Hz 的倍数；不认识返回 None。"""
    key = FREQ_ALIASES.get(str(unit or "").strip().lower())
    return FREQ_UNITS.get(key) if key else None


def canonical_freq_unit(unit: str, default: str = "") -> str:
    key = FREQ_ALIASES.get(str(unit or "").strip().lower())
    if not key:
        return default
    return {"hz": "Hz", "khz": "kHz", "mhz": "MHz", "ghz": "GHz", "thz": "THz"}[key]


def normalize_band(band) -> dict:
    """把各种写法的频段统一成 ``{start_hz, stop_hz, unit, label}``。

    接受 ``{"start": 2.3, "stop": 2.5, "unit": "GHz"}``，也接受
    ``start_ghz/stop_ghz``、``f_start/f_stop``、``from/to`` 等别名；
    单位缺省按 Hz（ADS 数据集里 freq 的原始单位）。
    """
    if not band:
        return {}
    if isinstance(band, (list, tuple)) and len(band) == 2:
        band = {"start": band[0], "stop": band[1]}
    if not isinstance(band, dict):
        return {}

    unit = canonical_freq_unit(
        band.get("unit") or band.get("freq_unit") or band.get("units") or "Hz", "Hz"
    )
    scale = freq_scale(unit) or 1.0

    def pick(*keys):
        for k in keys:
            if k in band and band[k] is not None and band[k] != "":
                return band[k]
        return None

    def bound(key, *aliases):
        value = pick(key, *aliases)
        if value is not None:
            return value, scale, ""
        for suffix, suffix_unit in (("_ghz", "GHz"), ("_mhz", "MHz"),
                                    ("_khz", "kHz"), ("_hz", "Hz")):
            value = band.get(key + suffix)
            if value is not None:
                return value, freq_scale(suffix_unit), suffix_unit
        return None, scale, ""

    # 字段名带的单位只作用于该端点；start_hz/stop_hz 永远是 Hz。
    start, start_scale, start_unit = bound("start", "f_start", "from", "lo", "min")
    stop, stop_scale, stop_unit = bound("stop", "f_stop", "to", "hi", "max")
    if start is None and band.get("start_hz") is not None:
        start, start_scale = band["start_hz"], 1.0
    if stop is None and band.get("stop_hz") is not None:
        stop, stop_scale = band["stop_hz"], 1.0
    if not any(k in band for k in ("unit", "freq_unit", "units")):
        unit = start_unit or stop_unit or unit
        scale = freq_scale(unit) or 1.0

    try:
        start_hz = float(start) * start_scale if start is not None else None
        stop_hz = float(stop) * stop_scale if stop is not None else None
    except (TypeError, ValueError):
        return {}

    if start_hz is None or stop_hz is None:
        return {}
    if start_hz > stop_hz:
        start_hz, stop_hz = stop_hz, start_hz

    return {
        "start_hz": start_hz,
        "stop_hz": stop_hz,
        "unit": unit,
        "start": start_hz / scale,
        "stop": stop_hz / scale,
        "label": f"{_fmt_num(start_hz / scale)}–{_fmt_num(stop_hz / scale)} {unit}",
    }


def _fmt_num(v) -> str:
    if v is None:
        return "?"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    return f"{f:g}"


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def new_job_id(now=None) -> str:
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"dj_{stamp}_{secrets.token_hex(2)}"


class DesignJob:
    """一次"设计 → 仿真 → 评估"的结构化记录。"""

    def __init__(self, requirement="", band=None, metrics=None, design=None,
                 job_id=None, stage=STAGE_DRAFT, title="", created_at=None,
                 metric_specs=None):
        self.job_id = job_id or new_job_id()
        self.title = title or ""
        self.requirement = requirement or ""
        self.band = normalize_band(band)
        self.metrics = list(metrics or [])
        self.metric_specs = [dict(m) for m in (metric_specs if metric_specs is not None else metrics or [])]
        self.design = dict(design or {})
        self.stage = stage
        self.created_at = created_at or utc_now()
        self.updated_at = self.created_at
        self.iterations = []
        self.artifacts = {}
        self.sim = {}
        self.error = ""
        self.notes = []
        # 保存失败记录（"计算已完成但没存上"必须能被看到，不能只进日志）
        self.save_errors: list = []
        # 各阶段耗时（秒）：sim_s / read_s / normalize_s / evaluate_s
        self.timing: dict = {}
        # 评估口径选项（partial_band 等），reload/resimulate 时沿用
        self.eval_options: dict = {}
        # 仿真时的设计版本（网表指纹）与工作区 —— 结果复用验证与跨工作区防护
        self.design_version: dict = {}
        # 优化记录（只有**用户明确要求**优化时才会有内容，见
        # design_service.run_optimization）：候选 / 每轮的仿真证据与指标 /
        # 最佳已验证结果 / 停止原因。导入模型**不会**自动产生它。
        self.optimization: dict = {}

    # -- 状态 ---------------------------------------------------------------
    def set_optimization(self, **fields) -> "DesignJob":
        """合并写入优化记录（不清空已有轮次）。"""
        for key, value in fields.items():
            self.optimization[key] = value
        self._touch()
        return self

    def add_optimization_iteration(self, record: dict) -> "DesignJob":
        entry = dict(record or {})
        rounds = list(self.optimization.get("iterations") or [])
        entry.setdefault("n", len(rounds) + 1)
        entry.setdefault("at", utc_now())
        rounds.append(entry)
        self.optimization["iterations"] = rounds
        self._touch()
        return self
    def can_transition(self, stage: str) -> bool:
        return stage in TRANSITIONS.get(self.stage, ())

    def transition(self, stage: str, note: str = "") -> "DesignJob":
        if stage not in STAGES:
            raise IllegalTransition(f"未知阶段: {stage}")
        if stage == self.stage:
            self._touch(note)
            return self
        if not self.can_transition(stage):
            raise IllegalTransition(
                f"非法状态流转: {self.stage} -> {stage}"
                f"（允许: {', '.join(TRANSITIONS.get(self.stage, ())) or '无'}）"
            )
        self.stage = stage
        if stage == STAGE_FAILED and note:
            self.error = note
        self._touch(note)
        return self

    def fail(self, error: str, stage: str = STAGE_FAILED) -> "DesignJob":
        """标记失败。**只改状态和错误信息** —— 原理图、数据集、迭代记录全部保留，
        这样"中途失败"仍然能看到已经生成了什么、已经跑出了什么数据。"""
        self.error = str(error or "").strip()
        try:
            self.transition(stage)
        except IllegalTransition:
            # 例如 draft -> failed 之后再失败：允许幂等重设
            self.stage = stage
            self._touch()
        return self

    def is_finished(self) -> bool:
        return self.stage in FINISHED_STAGES

    def _touch(self, note: str = "") -> None:
        self.updated_at = utc_now()
        if note:
            self.notes.append({"at": self.updated_at, "stage": self.stage, "text": note})

    # -- 内容 ---------------------------------------------------------------
    def set_design(self, **ref) -> "DesignJob":
        """记录设计引用（workspace / library / cell / view）。"""
        for key, value in ref.items():
            if value not in (None, ""):
                self.design[key] = str(value)
        self._touch()
        return self

    def set_artifacts(self, **items) -> "DesignJob":
        """记录产物路径（output_dir / netlist_path / dataset_path / ...）。合并，不清空。"""
        for key, value in items.items():
            if value not in (None, ""):
                self.artifacts[key] = value
        self._touch()
        return self

    def set_sim(self, **info) -> "DesignJob":
        for key, value in info.items():
            self.sim[key] = value
        self._touch()
        return self

    def set_metrics(self, results: list) -> "DesignJob":
        """写入**评估器算出的**指标结果（不是模型说的数值）。"""
        self.metrics = list(results or [])
        self._touch()
        return self

    def add_iteration(self, record: dict) -> "DesignJob":
        entry = dict(record or {})
        entry.setdefault("n", len(self.iterations) + 1)
        entry.setdefault("at", utc_now())
        self.iterations.append(entry)
        self._touch()
        return self

    def design_ref(self) -> str:
        """``lib:cell:view`` 形式的设计引用（缺失部分用 ? 占位）。"""
        d = self.design
        lib = d.get("library") or "?"
        cell = d.get("cell") or "?"
        view = d.get("view") or "schematic"
        return f"{lib}:{cell}:{view}"

    def design_label(self) -> str:
        d = self.design
        parts = [d.get("library"), d.get("cell"), d.get("view")]
        return " / ".join(p for p in parts if p) or "(未指定设计)"

    # -- 判定 ---------------------------------------------------------------
    def verdict(self) -> str:
        """pass / partial / fail / unknown —— **只看评估器的结果**。

        规则与 ``design_metrics.summarize`` 保持一致：
        一项都判不出来是 unknown（不是 fail），有一个 True 都没有才是 fail。
        """
        checks = [m for m in self.metrics if isinstance(m, dict) and "pass" in m]
        n_pass = sum(1 for m in checks if m.get("pass") is True)
        n_fail = sum(1 for m in checks if m.get("pass") is False)
        n_unknown = sum(1 for m in checks if m.get("pass") is None)
        if not checks or n_unknown == len(checks):
            return "unknown"
        if n_fail == 0 and n_unknown == 0:
            return "pass"
        if n_pass == 0:
            return "fail"
        return "partial"

    def summary(self) -> dict:
        checks = [m for m in self.metrics if isinstance(m, dict) and "pass" in m]
        return {
            "verdict": self.verdict(),
            "n_metrics": len(checks),
            "n_passed": sum(1 for m in checks if m.get("pass") is True),
            "n_failed": sum(1 for m in checks if m.get("pass") is False),
            "n_unknown": sum(1 for m in checks if m.get("pass") is None),
            "n_iterations": len(self.iterations),
        }

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "title": self.title,
            "requirement": self.requirement,
            "band": self.band,
            "metrics": self.metrics,
            "metric_specs": self.metric_specs,
            "design": self.design,
            "design_ref": self.design_ref(),
            "design_label": self.design_label(),
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "verdict": self.verdict(),
            "summary": self.summary(),
            "iterations": self.iterations,
            "artifacts": self.artifacts,
            "sim": self.sim,
            "error": self.error,
            "notes": self.notes,
            "save_errors": self.save_errors,
            "timing": self.timing,
            "eval_options": self.eval_options,
            "design_version": self.design_version,
            "optimization": self.optimization,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DesignJob":
        job = cls(
            job_id=data.get("job_id") or new_job_id(),
            requirement=data.get("requirement", ""),
            band=data.get("band") or {},
            metrics=data.get("metrics") or [],
            metric_specs=data.get("metric_specs"),
            design=data.get("design") or {},
            stage=data.get("stage") or STAGE_DRAFT,
            title=data.get("title", ""),
            created_at=data.get("created_at") or utc_now(),
        )
        job.iterations = list(data.get("iterations") or [])
        job.artifacts = dict(data.get("artifacts") or {})
        job.sim = dict(data.get("sim") or {})
        job.error = data.get("error", "") or ""
        job.notes = list(data.get("notes") or [])
        job.save_errors = list(data.get("save_errors") or [])
        job.timing = dict(data.get("timing") or {})
        job.eval_options = dict(data.get("eval_options") or {})
        job.design_version = dict(data.get("design_version") or {})
        job.optimization = dict(data.get("optimization") or {})
        job.updated_at = data.get("updated_at") or job.created_at
        if job.stage not in STAGES:
            job.stage = STAGE_DRAFT
        # schema 迁移：v1 任务没有新字段（默认值已兜底），记录一句说明即可
        if int(data.get("schema_version") or 1) < SCHEMA_VERSION:
            job.notes.append({
                "at": utc_now(), "stage": job.stage,
                "text": f"任务文件由 schema v{data.get('schema_version') or 1} 迁移到 "
                        f"v{SCHEMA_VERSION}（2026-10-02），指标与产物保持原样",
            })
        return job


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

_IO_LOCK = threading.Lock()


def job_dir(root: str) -> str:
    return os.path.join(str(root), "design_jobs")


def job_path(root: str, job_id: str) -> str:
    safe = "".join(c for c in str(job_id) if c.isalnum() or c in "_-")
    return os.path.join(job_dir(root), f"{safe}.json")


def save_job(root: str, job: DesignJob | dict) -> str:
    """原子写入 ``design_jobs/<job_id>.json``，返回路径。

    原子替换：结果页读取时永远看不到写了一半的文件（Windows 上
    ``os.replace`` 与 ``open`` 会互相打断，所以带重试）。
    替换成功前把上一版留成 ``<job_id>.json.bak`` —— 主文件损坏时
    ``load_job_ex`` 可以从这里恢复最近一次成功保存的版本。
    """
    data = job.to_dict() if isinstance(job, DesignJob) else dict(job)
    directory = job_dir(root)
    os.makedirs(directory, exist_ok=True)
    path = job_path(root, data.get("job_id", ""))
    tmp = f"{path}.tmp"
    with _IO_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                if os.path.exists(path):
                    try:
                        os.replace(path, f"{path}.bak")
                    except OSError:
                        pass  # .bak 写不上不阻塞主保存
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                import time as _t
                _t.sleep(0.01 * (attempt + 1))
    return path


def load_job(root: str, job_id: str) -> DesignJob | None:
    """读取任务；文件不存在或内容坏了都返回 None（结果页据此提示"已丢失"）。

    需要"损坏原因/是否从备份恢复"时用 ``load_job_ex``。
    """
    job, _err = load_job_ex(root, job_id)
    return job


def load_job_ex(root: str, job_id: str) -> tuple:
    """读取任务，返回 ``(job, error_info)``。

    error_info 为空 dict 表示正常；主文件损坏时自动尝试 ``.bak``：
      {"corrupt": True, "recovered_from_backup": True, "error": "..."}
    两份都读不出来时 job=None，error_info 带上具体损坏原因。
    """
    path = job_path(root, job_id)
    info: dict = {}
    for attempt, p in ((0, path), (1, f"{path}.bak")):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            if attempt == 0:
                continue
            info.setdefault("error", "文件不存在")
            return None, info
        except (OSError, ValueError) as e:
            if attempt == 0:
                info = {"corrupt": True, "recovered_from_backup": False,
                        "error": f"{type(e).__name__}: {e}"}
                continue
            info["recovered_from_backup"] = False
            info["error"] = f"主文件与备份都损坏（{type(e).__name__}: {e}）"
            return None, info
        if not isinstance(data, dict):
            if attempt == 0:
                info = {"corrupt": True, "recovered_from_backup": False,
                        "error": "文件内容不是 JSON 对象"}
                continue
            return None, {**info, "error": "主文件与备份的内容都不是 JSON 对象"}
        job = DesignJob.from_dict(data)
        if attempt == 1:
            info["corrupt"] = True
            info["recovered_from_backup"] = True
            info["error"] = (f"主任务文件损坏（{info.get('error', '')}），"
                             f"已从最近一次成功保存的备份恢复")
            job.notes.append({
                "at": utc_now(), "stage": job.stage,
                "text": "主任务文件损坏，本内容来自最近一次成功保存的备份（.bak）",
            })
        return job, info
    return None, info


def delete_job(root: str, job_id: str) -> bool:
    try:
        os.unlink(job_path(root, job_id))
        return True
    except OSError:
        return False


def list_job_ids(root: str) -> list:
    try:
        names = os.listdir(job_dir(root))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))
