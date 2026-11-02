"""有源器件建模引擎：模型选择、受约束拟合、建图计划与验证。

三类数值严格分账（审查要求）：

* 提取值（datasheet_text）—— 参数记录，带页码/行证据；
* 拟合值（fitted）—— 本模块 least_squares 的结果，带目标函数、边界、
  初值、收敛、边界命中与留出验证；
* 工程假设（assumed）—— 显式声明（如 50Ω 参考阻抗未在手册标注）。

通用引擎不写死本机路径、SHA 或 drawing 索引；J310 样本映射在
sample_adapters（SHA 锁定适配器）。

依赖策略：numpy/scipy 缺失时拟合与验证工具**如实报错**（含缺失清单），
不做无数值库的降级拟合；ADS 侧建图/仿真仍由既有工具完成，门禁不绕过。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
import uuid

import datasheet_models
import datasheet_curves


class ModelError(ValueError):
    pass


_NUM = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
#: 模型库稳定身份（mdl_ + 十六进制）。与建模记录 ID 是不同层级，不能混用。
_LIBRARY_ID = re.compile(r"mdl_[0-9a-fA-F]+\Z")

#: 终端索引与受支持配置（端子顺序与样本工具一致：g, s, d）
_TERMINALS = ("g", "s", "d")
_CONFIGURATIONS = {
    "common_gate": {"port1": "s", "port2": "d", "common": "g"},
    "common_source": {"port1": "g", "port2": "d", "common": "s"},
    "common_drain": {"port1": "g", "port2": "s", "common": "d"},
}
#: ads_rflib:FET 原生符号的引脚映射（运行时实测：gate=1, drain=2, source=3）；
#: 键用终端短名 g/d/s，与 _CONFIGURATIONS 一致
_ADS_FET_PINS = {"g": 1, "d": 2, "s": 3}
#: 物理封装脚号（SOT-143 类 J310 手册：drain=1, source=2, gate=3）——与符号脚号
#: 是两张独立表，建图计划必须分别说明。
_PACKAGE_PINS_J310 = {"drain": 1, "source": 2, "gate": 3}

_UNSUPPORTED_ALWAYS = ["dc_operating_point", "nonlinear_compression", "noise",
                       "temperature_drift", "large_signal_harmonics"]

DEFAULT_RULE = {
    "heldout_max_nrmse": 0.10,
    "gm_min_mS": 8.0, "gm_max_mS": 18.0, "gds_max_uS": 250.0,
    "origin": "exploratory_default",
    "note": "探索性工程规则（留出加权复数 NRMSE 与 gm/gds 合理区间），"
            "不是厂家精度承诺，也不是用户验收指标。",
}


def _np():
    try:
        import numpy
    except Exception as exc:  # noqa: BLE001
        raise ModelError("数值库 numpy 不可用：拟合与验证需要 numpy/scipy。"
                         f"请在后端解释器安装后重试（{type(exc).__name__}: {exc}）")
    return numpy


def _scipy():
    try:
        from scipy.optimize import least_squares
    except Exception as exc:  # noqa: BLE001
        raise ModelError("数值库 scipy 不可用：受约束拟合需要 scipy.optimize。"
                         f"请在后端解释器安装后重试（{type(exc).__name__}: {exc}）")
    return least_squares


# ---------------------------------------------------------------------------
# 小信号 JFET 模型（3×3 不定导纳 -> 配置对应二端口）
# ---------------------------------------------------------------------------

def intrinsic_y3(freqs_hz, p):
    """三端小信号模型（g, s, d）：Cgs/Cgd/Cds + gds + gm(+tau 延迟）。单位 SI。"""
    np = _np()
    jw = 2j * np.pi * np.asarray(freqs_hz, dtype=float)
    a = np.zeros((len(jw), 3, 3), dtype=complex)
    for i, j, name in ((0, 1, "Cgs_F"), (0, 2, "Cgd_F"), (1, 2, "Cds_F")):
        v = jw * p[name]
        a[:, i, i] += v
        a[:, j, j] += v
        a[:, i, j] -= v
        a[:, j, i] -= v
    v = p["gds_S"]
    a[:, 1, 1] += v
    a[:, 2, 2] += v
    a[:, 1, 2] -= v
    a[:, 2, 1] -= v
    gm = p["gm_S"] * np.exp(-jw * p.get("tau_s", 0.0))
    a[:, 2, 0] += gm
    a[:, 2, 1] -= gm
    a[:, 1, 0] -= gm
    a[:, 1, 1] += gm
    return a


def _config_indices(configuration: str) -> tuple[int, int, int]:
    cfg = _CONFIGURATIONS.get(configuration)
    if cfg is None:
        raise ModelError(f"拟合配置只支持 {sorted(_CONFIGURATIONS)}")
    return (_TERMINALS.index(cfg["port1"]), _TERMINALS.index(cfg["port2"]),
            _TERMINALS.index(cfg["common"]))


def circuit_y2p(freqs_hz, p, configuration: str, packaged: bool):
    """模型在测量配置下的 2×3 端 Y 矩阵序列（list[complex]，2x2）。"""
    np = _np()
    y3 = intrinsic_y3(freqs_hz, p)
    p1, p2, _common = _config_indices(configuration)
    if not packaged:
        return y3[:, [p1, p2], :][:, :, [p1, p2]]
    jw = 2j * np.pi * np.asarray(freqs_hz, dtype=float)
    branches = np.stack([1.0 / (p[f"R{t}_Ohm"] + jw * p[f"L{t}_H"])
                         for t in _TERMINALS], axis=1)
    m = y3.copy()
    for i in range(3):
        m[:, i, i] += branches[:, i]
    b = np.zeros((len(jw), 3, 2), dtype=complex)
    b[:, p1, 0] = branches[:, p1]
    b[:, p2, 1] = branches[:, p2]
    ext = np.zeros((len(jw), 2, 2), dtype=complex)
    ext[:, 0, 0] = branches[:, p1]
    ext[:, 1, 1] = branches[:, p2]
    eliminated = ext - np.swapaxes(b, 1, 2) @ np.linalg.solve(m, b)
    return eliminated


def _cg_to_cs(y):
    """公共栅 2 端口 -> 公共源 2 端口（[Vs-Vg,Vd-Vg]=B[Vg-Vs,Vd-Vs] 变换）。"""
    return _to_common_source(y, "common_gate")


def _config_basis(configuration: str) -> "np.ndarray":
    """配置基变换 M：测量端口电压 v = M u（u=(Vg-Vs, Vd-Vs) 为公共源参考）。

    于是 Y_cs = M^T Y_cfg M（v=M u ⇒ Y_u = Mᵀ Y_v M）。三种配置：

    * common_gate  (p1=s,p2=d,c=g)：v=(-v1..., ...) -> [[-1,0],[-1,1]]
    * common_source(p1=g,p2=d,c=s)：v=u             -> I
    * common_drain (p1=g,p2=s,c=d)：v=(u1-u2, -u2)  -> [[1,-1],[0,-1]]
    """
    np = _np()
    if configuration == "common_gate":
        return np.array([[-1.0, 0.0], [-1.0, 1.0]])
    if configuration == "common_source":
        return np.eye(2)
    if configuration == "common_drain":
        return np.array([[1.0, -1.0], [0.0, -1.0]])
    raise ModelError(f"gm/gds 外推不支持配置 {configuration}")


def _to_common_source(y, configuration: str):
    """把任意支持配置下的 2 端口 Y 变换到公共源参考（对不定导纳矩阵精确）。"""
    np = _np()
    m = _config_basis(configuration)
    arr = np.asarray(y)
    single = arr.ndim == 2
    if single:
        arr = arr[None]
    out = np.einsum("ab,nbc,cd->nad", m.T, arr, m)
    return out[0] if single else out


def effective_gm_gds(y2p_dc, configuration: str) -> tuple[float, float]:
    """1 kHz 外推的公共源 gm/gds（一致性检查，不是直流工作点测量）。"""
    if configuration not in _CONFIGURATIONS:
        raise ModelError(f"gm/gds 外推不支持配置 {configuration}")
    y = _to_common_source(y2p_dc, configuration)
    return float(y[1, 0].real), float(y[1, 1].real)


def y_to_s(y, z0: float):
    np = _np()
    eye = np.eye(2)
    return np.array([(eye - z0 * m) @ np.linalg.inv(eye + z0 * m) for m in y])


def s_to_y(s, z0: float):
    np = _np()
    eye = np.eye(2)
    return np.array([(eye - m) @ np.linalg.inv(eye + m) / z0 for m in s])


def matrix_metrics(actual, target, phase_amp_floor=None) -> dict:
    """逐 S/Y 参数的幅值/相位/复数误差（幅差绝对、相位差绝对分开标注）。

    * 相位用**环绕角差** angle(a*conj(b))（±180°，不会出现 359° 假误差）；
    * ``phase_amp_floor`` 给定时，两端幅值都低于该下限的点不参与相位统计
      （低幅值点的相位不可信），并如实报告被排除的点数与相位是否可评估。
    """
    np = _np()
    out = {}
    for i, j in ((1, 1), (1, 2), (2, 1), (2, 2)):
        a = actual[:, i - 1, j - 1]
        b = target[:, i - 1, j - 1]
        phase = np.rad2deg(np.angle(a * np.conj(b)))
        if phase_amp_floor is None:
            mask = np.ones(len(a), dtype=bool)
        else:
            mask = np.maximum(np.abs(a), np.abs(b)) >= phase_amp_floor
        excluded = int(len(a) - int(np.count_nonzero(mask)))
        if np.count_nonzero(mask):
            phase_rmse = float(np.sqrt(np.mean(phase[mask] ** 2)))
            phase_max = float(np.max(np.abs(phase[mask])))
            phase_state = "evaluated"
        else:
            phase_rmse, phase_max, phase_state = None, None, "not_evaluated"
        out[f"{i}{j}"] = {
            "magnitude_rmse": float(np.sqrt(np.mean((abs(a) - abs(b)) ** 2))),
            "magnitude_max_abs": float(np.max(np.abs(abs(a) - abs(b)))),
            "phase_rmse_deg": phase_rmse,
            "phase_max_abs_deg": phase_max,
            "phase_state": phase_state,
            "phase_points": int(np.count_nonzero(mask)),
            "phase_low_amp_points": excluded,
            "complex_rmse": float(np.sqrt(np.mean(np.abs(a - b) ** 2))),
            "complex_max_abs": float(np.max(np.abs(a - b))),
        }
    return out


def _element_pass(metrics: dict, tolerance: dict, peak: float) -> tuple:
    """逐元素判定：complex_rmse <= max(绝对下限, 相对容差 × 峰值)。

    归一化误差在真值趋近 0 的元素上会失去意义（相位/幅度都不可比），
    所以必须同时给一个**绝对误差下限**；两者取大是标准的"相对+绝对"容差。
    """
    limit = max(float(tolerance["max_complex_abs"]), float(tolerance["max_nrmse"]) * peak)
    worst = max(metrics, key=lambda k: metrics[k]["complex_rmse"])
    return (metrics[worst]["complex_rmse"] <= limit), limit, worst



def weighted_nrmse(y, target, scale) -> float:
    np = _np()
    return float(np.sqrt(np.mean(np.abs((y - target) / scale) ** 2)))


def matrix_stack(cells) -> "np.ndarray":
    """assemble_matrix 的 dict 序列 -> (N,2,2) 复数数组。"""
    np = _np()
    return np.asarray([[[cell[(i, j)] for j in (1, 2)] for i in (1, 2)]
                       for cell in cells], dtype=complex)


# ---------------------------------------------------------------------------
# 曲线证据组装
# ---------------------------------------------------------------------------

def _expand_ids(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    raise ModelError("curve_ids 的值必须是曲线 ID 或 ID 列表")


def _load_curves(record: dict, kinds: tuple) -> dict:
    """从模型记录的 curve_ids 装载曲线记录，逐项核对归属与证据等级。

    阶段二门禁（任何一项不满足即拒绝拼接，不做"尽力而为"）：

    * 每条曲线的 document_id 必须等于模型记录的 document_id；
    * 曲线证据等级必须是 SHA 锁定样本适配器或已显式复核（reviewed），
      未复核曲线拒绝（不得用未复核数字化结果直接建模）；
    * 配置、端口顺序、参考阻抗（数值 + declared/assumed）、测试条件
      必须彼此兼容（同一批曲线必须来自同一测量条件）。
    """
    ids = record.get("curve_ids") or {}
    document_id = record.get("document_id")
    out = {}
    unreviewed = []
    for kind in kinds:
        pair = ids.get(kind) or {}
        records = []
        for comp in ("mag", "phase"):
            for curve_id in _expand_ids(pair.get(comp)):
                record_c = datasheet_curves.get_curve(curve_id)
                if record_c.get("document_id") != document_id:
                    raise ModelError(
                        f"曲线 {curve_id[:8]} 属于另一个手册（document_id 不一致）；"
                        "不得跨手册拼接曲线")
                state = datasheet_curves.curve_evidence_state(record_c)
                if state == "unreviewed":
                    unreviewed.append(curve_id)
                records.append(record_c)
        if records:
            out[kind] = records
    if unreviewed:
        raise ModelError(
            "以下曲线尚未复核，不能用于建模（请先 review_datasheet_curves，"
            "或使用 SHA 锁定的样本适配器）：" + ", ".join(c[:8] for c in unreviewed))
    _curve_binding(out)
    return out


def _curve_binding(curves: dict) -> dict:
    """返回所有曲线共享的绑定信息；任一项不兼容直接拒绝。"""
    anchor = None
    for kind in ("S", "Y"):
        for rec in curves.get(kind) or []:
            if anchor is None:
                anchor = rec
                continue
            if rec.get("configuration") != anchor.get("configuration"):
                raise ModelError(
                    f"曲线配置不一致：{anchor.get('configuration')} vs "
                    f"{rec.get('configuration')}；不得把不同配置的曲线拼在一起")
            if (rec.get("port_order") or {}) != (anchor.get("port_order") or {}):
                raise ModelError("曲线的端口顺序说明不一致；不得跨端口约定拼接")
            z_a = anchor.get("z0") or {}
            z_b = rec.get("z0") or {}
            if (z_a.get("ohm"), z_a.get("status")) != (z_b.get("ohm"), z_b.get("status")):
                raise ModelError(
                    f"曲线参考阻抗不一致：{z_a} vs {z_b}；非 50Ω 与 50Ω 数据不得混用")
            if dict(rec.get("conditions") or {}) != dict(anchor.get("conditions") or {}):
                raise ModelError(
                    f"曲线测试条件不一致：{anchor.get('conditions')} vs "
                    f"{rec.get('conditions')}；不得把不同偏置/温度的曲线拼在一起")
    if anchor is None:
        raise ModelError("模型记录没有可用的曲线")
    return {
        "configuration": anchor.get("configuration"),
        "port_order": dict(anchor.get("port_order") or {}),
        "z0": dict(anchor.get("z0") or {}),
        "conditions": dict(anchor.get("conditions") or {}),
        "document_id": anchor.get("document_id"),
        "evidence": [datasheet_curves.curve_evidence_state(r)
                     for kind in ("S", "Y") for r in (curves.get(kind) or [])],
    }


def _require_pair(curves: dict, kind: str) -> tuple:
    records = curves.get(kind) or []
    comps = {r.get("component") for r in records}
    if not {"mag", "phase"} <= comps:
        raise ModelError(f"{kind} 曲线必须有幅值与相位成对记录；当前：{sorted(comps)}")
    freqs, matrix, mag_unit = datasheet_curves.assemble_matrix(records, kind, ("mag", "phase"))
    return freqs, matrix, mag_unit, records


# ---------------------------------------------------------------------------
# 模型记录
# ---------------------------------------------------------------------------

def _root() -> str:
    return os.path.join(datasheet_models._root(), "models")


def _path(record_id: str) -> str:
    if isinstance(record_id, str) and _LIBRARY_ID.fullmatch(record_id.strip()):
        raise ModelError(
            f"{record_id.strip()} 是模型库身份（library_model_id），不是建模记录 ID。"
            "建模记录 ID 是 64 位十六进制（model_record_id）：可用 get_model_revision "
            "返回的 source_record_id 获取；要新记录请先 prepare_device_model。")
    if not isinstance(record_id, str) or not _ID.fullmatch(record_id):
        raise ModelError("无效的模型记录 ID；请使用工具返回的 model_record_id"
                         "（模型库身份 library_model_id 不能用于这里）")
    return os.path.join(_root(), record_id + ".json")


def _record_id(args: dict) -> str:
    """解析建模记录身份：优先 model_record_id，兼容旧字段 model_id。

    区别不在命名风格而在层级：model_record_id 指向建模记录文件，
    library_model_id（mdl_…）指向已发布/候选修订。混用由 _path() 拦下并
    给出下一步，而不是只报一句笼统的"无效 ID"。
    """
    value = args.get("model_record_id") or args.get("model_id")
    if not isinstance(value, str) or not value.strip():
        raise ModelError("缺少 model_record_id（建模记录 ID，64 位十六进制）；"
                         "库身份请用 library_model_id，不要放进 model_record_id")
    return value.strip()


def _save(record: dict) -> None:
    import tempfile

    os.makedirs(_root(), exist_ok=True)
    record["updated_at"] = datetime.datetime.now().replace(microsecond=0).isoformat()
    fd, temp = tempfile.mkstemp(dir=_root(), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp, _path(record["model_id"]))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _set_task(record: dict, state: str, note: str = "") -> None:
    """维护任务块：task_id + 状态机历史（规范状态见 device_library.TASK_STATES）。

    任务在候选创建时就生成身份并持久化（需求第十节）；此后每个阶段落一条
    历史（有界），重启后按记录真实阶段恢复。
    """
    task = record.get("task") or {}
    if not task.get("task_id"):
        task["task_id"] = "task_" + uuid.uuid4().hex[:12]
    history = task.get("history") or []
    entry = {"state": state, "at": datetime.datetime.now()
             .replace(microsecond=0).isoformat()}
    if note:
        entry["note"] = note[:300]
    if not history or history[-1].get("state") != state:
        history.append(entry)
    else:
        history[-1] = entry
    task["history"] = history[-40:]
    task["state"] = state
    task["updated_at"] = entry["at"]
    record["task"] = task


def _archive_validation(record: dict) -> None:
    """建图/参数变化时归档旧验证结论（保留证据，不静默丢弃）。"""
    previous = record.get("validation")
    if not previous:
        return
    history = record.setdefault("validation_history", [])
    history.append({
        "archived_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "build_fingerprint": previous.get("build_fingerprint"),
        "verdict": previous.get("verdict"),
        "validation_mode": previous.get("validation_mode"),
        "validated_at": previous.get("validated_at"),
    })
    record["validation_history"] = history[-10:]


def get_model_record(model_id: str) -> dict:
    try:
        with open(_path(model_id), encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as exc:
        raise ModelError("模型记录不存在；请先用 prepare_device_model 创建") from exc


def get_device_model(args: dict) -> dict:
    record = get_model_record(_record_id(args))
    return {"ok": True, "model_record_id": record.get("model_id"), **record}


def _norm_part(text: str) -> str:
    """型号归一化：忽略大小写、空格与常见分隔符差异。

    只用于"是否同一个型号"的比较，不用于放宽**跨手册/跨型号**的归属检查
    —— 那是 document_id + 显式共用声明负责的。
    """
    return re.sub(r"[\s_\-/]+", "", str(text or "")).upper()


def _check_parameter_attribution(parameter_set_id, document_id, part_number):
    """参数集归属核对（审查 P1/P2）。

    状态够"新"不代表是**这本手册这个型号**的参数 —— 此前只查 status，
    于是别的手册/别的型号的参数集也能被引用。缺证据时明确拒绝，
    跨手册一律拒绝；跨型号需有可核实的同族共用声明。
    """
    params = datasheet_models._read("parameters", parameter_set_id)
    review = params.get("status")
    if review not in ("evidence_checked", "semantic_verified", "reviewed"):
        raise ModelError(
            f"参数集状态为 {review!r}；必须先通过 review_parameter_set"
            "（evidence_checked）或 verify_parameter_semantics（semantic_verified）"
            "才能用于建模；requires_review / extracted_unreviewed 一律拒绝")
    param_doc = params.get("document_id")
    if param_doc and str(param_doc) != str(document_id):
        raise ModelError(
            f"参数集 {str(parameter_set_id)[:12]}… 来自另一份资料"
            f"（document_id {str(param_doc)[:12]}… ≠ 目标 "
            f"{str(document_id)[:12]}…）；"
            "不得用其它手册的参数为该型号建模")
    param_pn = params.get("part_number")
    attribution = "exact"
    if param_pn and _norm_part(str(param_pn)) != _norm_part(str(part_number)):
        # 同族共用只有具备**可核实的声明证据**才允许。
        shared = params.get("shared_range_quote") or params.get("shared_with")
        evidence = None
        if isinstance(shared, str) and shared.strip():
            evidence = shared.strip()
        elif isinstance(shared, list):
            evidence = "; ".join(str(s).strip() for s in shared if str(s).strip())
        if not evidence:
            raise ModelError(
                f"参数集型号 {param_pn!r} 与目标型号 {part_number!r} 不一致，"
                "且没有可核实的同族共用声明证据（shared_range_quote）；"
                "不得凭参数集状态就引用其它型号的记录")
        attribution = "shared_declared"
    return review, {
        "parameter_set_id": parameter_set_id,
        "document_id_match": not param_doc or str(param_doc) == str(document_id),
        "part_number": param_pn,
        "part_number_match": bool(param_pn)
        and _norm_part(str(param_pn)) == _norm_part(str(part_number)),
        "attribution": attribution,
    }


def prepare_device_model(args: dict) -> dict:
    """模型类型选择与候选记录：证据不足时明确列出缺什么，不假装就绪。"""
    document = datasheet_models._read("documents", args.get("document_id"))
    part_number = args.get("part_number")
    if not isinstance(part_number, str) or not part_number.strip():
        raise ModelError("请指定具体型号")
    # 归属核对放在**曲线可用性之前**：先确认"这些参数确实属于这本手册这个
    # 型号"，再谈有没有曲线。否则跨手册参数集会被"没有可用曲线"这条
    # 无关错误挡掉，归属缺陷就被掩盖了。
    parameter_set_id = args.get("parameter_set_id")
    parameter_review = None
    parameter_attribution = None
    if parameter_set_id:
        parameter_review, parameter_attribution = _check_parameter_attribution(
            parameter_set_id, document["document_id"], part_number)
    device_kind = args.get("device_kind")
    model_kind = args.get("model_kind", "auto")
    if model_kind not in ("auto", "vendor_package", "two_port_data", "jfet_small_signal"):
        raise ModelError(f"model_kind 只支持 auto/vendor_package/two_port_data/jfet_small_signal")
    if device_kind not in ("jfet", "mosfet", "bjt", "passive_equivalent", "unknown"):
        raise ModelError("device_kind 不支持")
    if device_kind == "jfet" and model_kind == "auto":
        model_kind = "jfet_small_signal"
    if model_kind == "auto":
        raise ModelError("无法从器件类型自动选型；请明确 model_kind"
                         "（vendor_package/two_port_data/jfet_small_signal）")
    if model_kind == "vendor_package":
        return {"ok": True, "model_kind": "vendor_package",
                "note": "原厂模型包走既有模型工具链：list_model_packages / "
                        "search_shared_models 找包，import_model_package 导入，"
                        "validate_model_import 验证。本工具不重复实现导入。",
                "pointers": ["list_model_packages", "import_model_package",
                             "validate_model_import"]}
    if model_kind == "jfet_small_signal" and device_kind != "jfet":
        raise ModelError("jfet_small_signal 只适用于 device_kind=jfet；"
                         "不得用无源 RLC 或错误类型代替")
    curve_ids = args.get("curve_ids")
    if not isinstance(curve_ids, dict):
        curve_ids = {}
    missing = []
    curves = _load_curves({"curve_ids": curve_ids,
                           "document_id": document["document_id"]}, ("Y", "S"))
    binding = _curve_binding(curves)
    # 型号必须在手册文本中有证据（曲线/参数都属于该型号）
    datasheet_models._part_number_evidence(part_number, document)

    def pair_comps(kind: str) -> set:
        pair = curve_ids.get(kind) or {}
        comps = set()
        for comp in ("mag", "phase"):
            for curve_id in _expand_ids(pair.get(comp)):
                comps.add(datasheet_curves.get_curve(curve_id).get("component"))
        return comps

    if model_kind == "jfet_small_signal":
        comps = pair_comps("Y")
        for comp in ("mag", "phase"):
            if comp not in comps:
                missing.append(f"Y_{comp}_curve")  # 拟合目标，必配
        if pair_comps("S") != {"mag", "phase"}:
            note_extra = "S 曲线缺失：拟合仍可进行，但独立交叉核对不可用。"
        else:
            note_extra = ""
    else:  # two_port_data：任一类型幅相成对即可（Y 会在构建时换算为 S2P）
        have = [k for k in ("S", "Y") if pair_comps(k) == {"mag", "phase"}]
        if not have:
            missing.extend(["S_or_Y_mag_phase_pair"])
        note_extra = ("" if "S" in have else
                      "仅有 Y 曲线：构建阶段会用记录 z0 做 Y→S 换算写 S2P，"
                      "并记录换算方法。")
    configuration = binding["configuration"]
    payload = {
        "document_id": document["document_id"], "part_number": part_number,
        "device_kind": device_kind, "model_kind": model_kind,
        "curve_ids": curve_ids, "parameter_set_id": parameter_set_id or None,
        "configuration": configuration,
        "curve_binding": {"z0": binding["z0"], "conditions": binding["conditions"],
                          "port_order": binding["port_order"],
                          "evidence": sorted(set(binding["evidence"]))},
        "parameter_review": parameter_review,
        "parameter_attribution": parameter_attribution,
    }
    record_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           allow_nan=False).encode("utf-8")).hexdigest()
    # 同一输入重试不能重置 fitted/build/validation/task 历史。model_id 是输入内容
    # 派生稳定键：命中已有记录就复用其真实阶段；输入不同会产生新 model_id。
    existing_path = _path(record_id)
    if os.path.isfile(existing_path):
        existing = get_model_record(record_id)
        return {"ok": True, **existing, "reused": True,
                "note": "相同 document/model/conditions/curve 输入已存在，复用原任务记录；"
                        "未重置拟合、建图或验证状态。"}
    record = {**payload, "model_id": record_id,
              "stage": "blocked_missing_evidence" if missing else "candidate_ready",
              "missing": missing,
              "unsupported": list(_UNSUPPORTED_ALWAYS),
              "fit": None, "build": None, "validation": None,
              "created_at": datetime.datetime.now().replace(microsecond=0).isoformat()}
    # 任务身份与来源链接（需求第三节/第十节）：task_id 在任务创建时生成并
    # 持久化；attachment_instance_id / 显示别名由面板或 LLM 透传，仅作记录，
    # 不改变型号身份判定。
    task = record.setdefault("task", {})
    task["task_id"] = "task_" + uuid.uuid4().hex[:12]
    for key in ("task_id_external", "attachment_instance_id", "display_name",
                "alias", "vendor"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            task[key] = value.strip()[:120]
    _set_task(record, "blocked" if missing else "target_resolved",
              note="目标型号 " + part_number)
    _save(record)
    note = ("候选模型记录已建立（candidate_ready）。" if not missing else
            "证据不足，记录为 blocked_missing_evidence：" + "、".join(missing) + "。")
    if note_extra:
        note += " " + note_extra
    if parameter_review:
        note += (f" 参数集复核级别：{parameter_review}"
                 + ("（语义级）" if parameter_review == "semantic_verified" else
                    "（字段证据级，非语义级）"))
    return {"ok": True, **record,
            "note": note + " 拟合用 fit_device_model；建图用 build_device_model。"
                    "数据二端口/固定偏置小信号模型之外的能力（DC/非线性/噪声/温度）"
                    "一律未支持。"}


# ---------------------------------------------------------------------------
# 拟合
# ---------------------------------------------------------------------------

_PARAM_SPEC = {
    # name: (scale, min, max, seed)
    "gm_S": ("S", 1e-4, 0.1, 0.014),
    "gds_S": ("S", 1e-7, 2.5e-4, 7e-5),
    "Cgs_F": ("F", 1e-13, 1.5e-11, 5e-12),
    "Cgd_F": ("F", 1e-13, 1e-11, 1.8e-12),
    "Cds_F": ("F", 0.0, 3e-12, 5e-14),
    "Rg_Ohm": ("Ohm", 1e-3, 20.0, 1.0), "Rs_Ohm": ("Ohm", 1e-3, 20.0, 1.0),
    "Rd_Ohm": ("Ohm", 1e-3, 20.0, 1.0),
    "Lg_H": ("H", 1e-12, 4e-9, 5e-10), "Ls_H": ("H", 1e-12, 4e-9, 5e-10),
    "Ld_H": ("H", 1e-12, 4e-9, 5e-10),
    "tau_s": ("s", 0.0, 1.5e-10, 3e-11),
}
_CANDIDATES = {
    "intrinsic": ["gm_S", "gds_S", "Cgs_F", "Cgd_F", "Cds_F"],
    "intrinsic_delay": ["gm_S", "gds_S", "Cgs_F", "Cgd_F", "Cds_F", "tau_s"],
    "package_delay": ["gm_S", "gds_S", "Cgs_F", "Cgd_F", "Cds_F",
                      "Rg_Ohm", "Rs_Ohm", "Rd_Ohm", "Lg_H", "Ls_H", "Ld_H", "tau_s"],
}


def fit_device_model(args: dict) -> dict:
    """受约束小信号拟合：目标=固定偏置 Y 曲线；S 曲线只作独立交叉核对。"""
    record = get_model_record(_record_id(args))
    if record.get("model_kind") != "jfet_small_signal":
        raise ModelError("fit_device_model 目前只支持 jfet_small_signal 候选")
    configuration = record.get("configuration")
    if configuration not in _CONFIGURATIONS:
        raise ModelError("模型记录缺少一致的测量配置；请先补齐曲线证据")
    curves = _load_curves(record, ("Y", "S"))
    freqs, y_target, y_unit, _records = _require_pair(curves, "Y")
    scale_factor = datasheet_curves.mag_scale_si(y_unit)
    if scale_factor is None:
        raise ModelError(f"Y 幅值轴单位 {y_unit!r} 无法换算为 S；请用 S/mS/uS 轴重新提取")
    np = _np()
    y_target = matrix_stack(y_target) * scale_factor
    freqs_hz = np.asarray([f * 1e6 for f in freqs], dtype=float)
    holdout = args.get("holdout_mhz")
    if holdout is not None:
        if not isinstance(holdout, list) or len(holdout) < 2:
            raise ModelError("holdout_mhz 须为至少 2 个频点（MHz）的列表")
        held_mask = np.isin(np.asarray(freqs, dtype=float), np.asarray(holdout, dtype=float))
        if held_mask.sum() < 2 or (~held_mask).sum() < 4:
            raise ModelError("留出集/训练集划分后点数不足（训练>=4，留出>=2）")
    else:
        # 缺省留出：中频段每隔一个点（确定性划分，与样本工具同思路）
        held_mask = np.zeros(len(freqs), dtype=bool)
        held_mask[len(freqs) // 3: 2 * len(freqs) // 3: 2] = True
        if held_mask.sum() < 2:
            raise ModelError("缺省留出集划分失败；请显式给 holdout_mhz")
    train_mask = ~held_mask
    y_train = y_target[train_mask]
    f_train = freqs_hz[train_mask]
    y_held = y_target[held_mask]
    f_held = freqs_hz[held_mask]
    scale = np.maximum(np.max(np.abs(y_train), axis=0), 1e-9)
    candidates_wanted = args.get("candidates")
    if candidates_wanted is None:
        candidates_wanted = list(_CANDIDATES)
    if not isinstance(candidates_wanted, list) or \
            any(c not in _CANDIDATES for c in candidates_wanted):
        raise ModelError(f"candidates 只能取 {list(_CANDIDATES)}")
    least_squares = _scipy()
    report_candidates = {}
    rng_seed = int(args.get("seed", 310))
    for kind in candidates_wanted:
        names = _CANDIDATES[kind]
        lo = np.array([_PARAM_SPEC[n][1] for n in names])
        hi = np.array([_PARAM_SPEC[n][2] for n in names])
        seed = np.array([_PARAM_SPEC[n][3] for n in names])
        rng = np.random.default_rng(rng_seed)

        def unpack(x):
            return dict(zip(names, map(float, x)))

        def residual(x):
            p = unpack(x)
            r = (circuit_y2p(f_train, p, configuration, kind == "package_delay")
                 - y_train) / scale
            return np.concatenate((r.real.ravel(), r.imag.ravel()))

        n_starts = 8 if kind == "package_delay" else 3
        solutions = []
        for n in range(n_starts):
            x0 = seed if n == 0 else np.clip(
                seed * np.exp(rng.normal(0, 0.55, len(seed))), lo + 1e-12, hi - 1e-12)
            solutions.append(least_squares(residual, x0, bounds=(lo, hi),
                                           x_scale="jac", max_nfev=1500,
                                           ftol=1e-10, xtol=1e-10, gtol=1e-10))
        best = min(solutions, key=lambda s: s.cost)
        p = unpack(best.x)
        y_all = circuit_y2p(freqs_hz, p, configuration, kind == "package_delay")
        y_dc = circuit_y2p(np.array([1000.0]), p, configuration,
                           kind == "package_delay")[0]
        gm_eff, gds_eff = effective_gm_gds(np.asarray(y_dc), configuration)
        held_score = weighted_nrmse(
            circuit_y2p(f_held, p, configuration, kind == "package_delay"),
            y_held, scale)
        train_score = weighted_nrmse(
            circuit_y2p(f_train, p, configuration, kind == "package_delay"),
            y_train, scale)
        s_model = y_to_s(y_all, record_z0(record))
        near = [s for s in solutions if s.cost <= best.cost * 1.05 + 1e-12]
        entry = {
            "parameters_fitted_SI": p,
            "parameter_count": len(names),
            "fit_success": bool(best.success),
            "train_weighted_complex_nrmse": train_score,
            "heldout_weighted_complex_nrmse": held_score,
            "effective_CS_gm_mS_1kHz_extrapolated": gm_eff * 1e3,
            "effective_CS_gds_uS_1kHz_extrapolated": gds_eff * 1e6,
            "parameter_bound_hits": [names[i] for i in range(len(names))
                                     if min(best.x[i] - lo[i], hi[i] - best.x[i])
                                     / (hi[i] - lo[i]) < 1e-3],
            "scaled_jacobian_condition": None,
            "near_best_multistart_ranges": {
                names[i]: [min(s.x[i] for s in near), max(s.x[i] for s in near)]
                for i in range(len(names))},
            "multistart_costs": [float(s.cost) for s in solutions],
        }
        try:
            jac = best.jac * (hi - lo)[None, :]
            sv = np.linalg.svd(jac, compute_uv=False)
            entry["scaled_jacobian_condition"] = float(sv[0] / max(sv[-1], 1e-30))
        except Exception:  # noqa: BLE001 — 条件数只是诊断
            pass
        if "S" in curves:
            try:
                s_freqs, s_target, _u, _rs = _require_pair(curves, "S")
                s_target = matrix_stack(s_target)
                s_freqs_hz = np.asarray([f * 1e6 for f in s_freqs], dtype=float)
                overlap = _overlap_mask(freqs_hz, s_freqs_hz)
                if overlap is None:
                    entry["vs_S_curves_independent_error"] = \
                        "S 曲线频段与拟合频段没有交集"
                else:
                    mask = overlap["mask"]
                    s_on_grid = _interp_on_overlap(s_target, s_freqs_hz, freqs_hz, mask)
                    entry["vs_S_curves_independent"] = matrix_metrics(
                        s_model[mask], s_on_grid, _PHASE_AMP_FLOOR["S"])
                    entry["vs_S_curves_independent_scope"] = {
                        "overlap_band_hz": overlap["overlap_hz"],
                        "compared_points": overlap["grid_points"],
                        "curve_coverage": overlap["target_coverage"],
                        "note": "只在 S 曲线与拟合频段的交集内比较，不做外推填充"}
            except datasheet_curves.CurveError as exc:
                entry["vs_S_curves_independent_error"] = str(exc)
        report_candidates[kind] = entry
    rule = dict(DEFAULT_RULE)
    user_rule = args.get("thresholds") or {}
    if user_rule:
        for key in ("heldout_max_nrmse", "gm_min_mS", "gm_max_mS", "gds_max_uS"):
            if key in user_rule:
                rule[key] = float(user_rule[key])
        rule["origin"] = "user_rule"
    eligible = [k for k, c in report_candidates.items()
                if c["heldout_weighted_complex_nrmse"] <= rule["heldout_max_nrmse"]
                and rule["gm_min_mS"] <= c["effective_CS_gm_mS_1kHz_extrapolated"] <= rule["gm_max_mS"]
                and c["effective_CS_gds_uS_1kHz_extrapolated"] <= rule["gds_max_uS"]]
    selected = min(eligible, key=lambda k: report_candidates[k][
        "heldout_weighted_complex_nrmse"]) if eligible else None
    record["fit"] = {
        "candidates": report_candidates,
        "selected": selected,
        "rule": rule,
        "train_frequencies_mhz": [round(f / 1e6, 6) for f in f_train],
        "heldout_frequencies_mhz": [round(f / 1e6, 6) for f in f_held],
        "objective": "加权复数 NRMSE（按 |Y| 峰值归一，实部虚部联合）",
        "fit_target": f"Y 曲线（{configuration}，单位 {y_unit}）",
        "target_conditions": (record.get("curve_binding") or {}).get("conditions") or {},
        "target_z0_Ohm": (record.get("curve_binding") or {}).get("z0") or {},
        "independent_check": "S 曲线未参与拟合，仅作交叉核对；跨图一致不是实物测量精度",
    }
    if selected:
        record["stage"] = "fitted"
        record["fit"]["status"] = "fitted"
        _set_task(record, "model_ready", note="候选 " + selected)
    else:
        record["stage"] = "fit_failed_no_candidate_meets_rule"
        record["fit"]["status"] = "no_candidate_meets_rule"
        _set_task(record, "failed", note="没有候选通过声明规则；阈值未放宽")
    _save(record)
    return {"ok": True, "model_id": record["model_id"], "stage": record["stage"],
            "selected": selected, "rule": rule,
            "candidates": {k: {"heldout": v["heldout_weighted_complex_nrmse"],
                               "gm_mS": v["effective_CS_gm_mS_1kHz_extrapolated"],
                               "gds_uS": v["effective_CS_gds_uS_1kHz_extrapolated"],
                               "bound_hits": v["parameter_bound_hits"]}
                           for k, v in report_candidates.items()},
            "note": ("拟合完成且候选通过声明规则（阈值 origin=" + rule["origin"] + "）。"
                     if selected else
                     "没有候选通过声明规则；未放宽阈值，可用 thresholds 显式给出用户规则后重试。")
            + " 拟合参数是 fitted 值，不是厂家原文。1 kHz gm/gds 是外推一致性检查。"}


def record_z0(record: dict) -> float:
    """曲线记录里共享的参考阻抗；不一致直接拒绝（非 50Ω 与 50Ω 不得混用）。"""
    values = set()
    for kind in ("Y", "S"):
        pair = (record.get("curve_ids") or {}).get(kind) or {}
        for comp in ("mag", "phase"):
            for curve_id in _expand_ids(pair.get(comp)):
                curve = datasheet_curves.get_curve(curve_id)
                z0 = curve.get("z0") or {}
                values.add((float(z0.get("ohm")), str(z0.get("status"))))
    if not values:
        raise ModelError("曲线记录缺失，无法确定参考阻抗")
    if len(values) > 1:
        raise ModelError(f"曲线记录的参考阻抗不一致：{sorted(values)}；"
                         "不得把不同 z0 的数据混在一个模型里")
    return values.pop()[0]


# ---------------------------------------------------------------------------
# 建图计划
# ---------------------------------------------------------------------------

def _valid_lib_cell(args: dict) -> tuple:
    library, cell = args.get("library"), args.get("cell")
    if not isinstance(library, str) or not _NUM.fullmatch(library):
        raise ModelError("library 必须是现有可写库的合法名称")
    if not isinstance(cell, str) or not _NUM.fullmatch(cell):
        raise ModelError("cell 必须是合法名称；请为测试电路用新 cell")
    return library, cell


def _freq_args(args: dict) -> dict:
    start = datasheet_models._finite(args.get("start_hz"), "start_hz")
    stop = datasheet_models._finite(args.get("stop_hz"), "stop_hz")
    step = datasheet_models._finite(args.get("step_hz"), "step_hz")
    if not 0 < start < stop or not 0 < step <= stop - start \
            or (stop - start) / step > 10000:
        raise ModelError("频率须满足 0 < start < stop、0 < step <= stop-start，最多 10001 个点")
    return {"start": start, "stop": stop, "step": step}


def _fmt(value: float, unit: str) -> str:
    return f"{value:.12g} {unit}"


def _build_fingerprint(record: dict, candidate: str, configuration: str, z0: float,
                       params: dict, freq: dict, topology: str, plan: dict) -> str:
    """建图/参数/拓扑的联合指纹：数据集只能绑定到产生它的那一版构建。"""
    payload = {
        "model_kind": record.get("model_kind"),
        "model_id": record.get("model_id"),
        "candidate": candidate,
        "configuration": configuration,
        "z0_Ohm": float(z0),
        "topology": topology,
        "parameters_fitted_SI": {k: float(v) for k, v in sorted(params.items())},
        "frequency": freq,
        "instances": [[i["name"], i["master"],
                       sorted((i.get("params") or {}).items())]
                      for i in plan["instances"]],
        "connections": [[c["a"], c["b"]] for c in plan["connections"]],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


#: 建图计划里出现的工程单位 → SI（独立求解器用，与建图代码分开维护）
_PLAN_UNITS = {"Ohm": 1.0, "nH": 1e-9, "pF": 1e-12, "psec": 1e-12, "mS": 1e-3,
               "S": 1.0, "Hz": 1.0, "GHz": 1e9}


def _plan_value(text) -> float:
    parts = str(text).split()
    if not parts:
        raise ModelError("建图计划里的参数值缺失")
    number = float(parts[0])
    if len(parts) > 1:
        factor = _PLAN_UNITS.get(parts[1])
        if factor is None:
            raise ModelError(f"建图计划里出现未知单位 {parts[1]!r}")
        number *= factor
    return number


def solve_plan_y2p(plan: dict, configuration: str, freqs_hz) -> "np.ndarray":
    """把建图计划的网表当成独立电路求解 2 端口 Y（拓扑核对用）。

    这是**独立于建图代码路径**的检查：解析计划里的 R/L/FET/TermG/GROUND
    实例与连线，按标称元件值搭节点导纳方程，对端口做内部节点消元。漏建支路
    （例如公共端封装 R/L）、端口互换、参数串位都会在这里和 circuit_y2p 的
    解析正演对不上。返回 (N,2,2) 复数数组，端口顺序为 Num=1、Num=2。
    """
    np = _np()
    cfg = _CONFIGURATIONS.get(configuration)
    if cfg is None:
        raise ModelError(f"不支持的配置 {configuration}")
    pins_of, masters, params_of = {}, {}, {}
    ground_nodes = []
    ports = {}
    for inst in plan.get("instances") or []:
        name, master = inst["name"], inst["master"]
        masters[name] = master
        params_of[name] = inst.get("params") or {}
        for pin in range(1, 5):
            pins_of[(name, pin)] = (name, pin)
        if master == "ads_rflib:GROUND":
            ground_nodes.append(name)
        if master == "ads_simulation:TermG":
            ports[str(params_of[name].get("Num"))] = name
    if set(ports) != {"1", "2"}:
        raise ModelError("建图计划必须恰好有 Num=1 与 Num=2 两个端口")
    if not ground_nodes:
        raise ModelError("建图计划缺少 GROUND 参考节点")

    # union-find：连线把 (实例, 引脚) 合并成同一节点；地引脚统一到节点 0
    parent = {key: key for key in pins_of}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for conn in plan.get("connections") or []:
        a, b = conn["a"], conn["b"]
        union((a[0], int(a[1])), (b[0], int(b[1])))
    ground_root = {find((g, 1)) for g in ground_nodes}
    ground_root |= {find((g, 2)) for g in ground_nodes}
    node_of = {}

    def node_id(key):
        root = find(key)
        if root in ground_root:
            return 0
        if root not in node_of:
            node_of[root] = len(node_of) + 1
        return node_of[root]

    # 端口节点（TermG 引脚 1 是信号端，引脚 2 接地）
    port_nodes = {num: node_id((name, 1)) for num, name in ports.items()}
    if port_nodes["1"] == port_nodes["2"] or port_nodes["1"] == 0 or port_nodes["2"] == 0:
        raise ModelError("端口节点无效（端口短接或接地）")

    # 元件列表（引脚 -> 全局节点）
    elements = []
    fet_insts = []
    for name, master in masters.items():
        params = params_of[name]
        if master == "ads_rflib:FET":
            fet_insts.append(name)
        elif master in ("ads_rflib:R", "ads_rflib:L"):
            elements.append((master, node_id((name, 1)), node_id((name, 2)), params))
        elif master in ("ads_rflib:GROUND", "ads_simulation:TermG",
                        "ads_simulation:S_Param"):
            continue
        else:
            raise ModelError(f"独立求解器不认识建图计划里的元件 {master}")

    freqs = np.asarray(freqs_hz, dtype=float)
    n_nodes = len(node_of) + 1        # 含 0 号地
    jw = 2j * np.pi * freqs
    out = np.zeros((len(freqs), 2, 2), dtype=complex)
    for k in range(len(freqs)):
        y = np.zeros((n_nodes, n_nodes), dtype=complex)

        def stamp(a, b, val):
            if a:
                y[a, a] += val
            if b:
                y[b, b] += val
            if a and b:
                y[a, b] -= val
                y[b, a] -= val

        for master, a, b, params in elements:
            if master == "ads_rflib:R":
                stamp(a, b, 1.0 / _plan_value(params["R"]))
            else:
                stamp(a, b, 1.0 / (jw[k] * _plan_value(params["L"])))
        for name in fet_insts:
            params = params_of[name]
            core = {"gm_S": _plan_value(params["G"]),
                    "gds_S": 1.0 / _plan_value(params["Rds"]),
                    "Cgs_F": _plan_value(params["Cgs"]),
                    "Cgd_F": _plan_value(params["Cdg"]),
                    "Cds_F": _plan_value(params["Cds"]),
                    "tau_s": _plan_value(params["T"])}
            y3 = intrinsic_y3(np.array([freqs[k]]), core)[0]
            # _ADS_FET_PINS: g=1, d=2, s=3；intrinsic_y3 顺序 (g, s, d)
            nodes = [node_id((name, 1)), node_id((name, 3)), node_id((name, 2))]
            for i in range(3):
                for j in range(3):
                    if nodes[i] and nodes[j]:
                        y[nodes[i], nodes[j]] += y3[i, j]
        # 端口节点做内部节点消元：Y_pp - Y_pi inv(Y_ii) Y_ip
        port_ids = [port_nodes["1"], port_nodes["2"]]
        internal = [n for n in range(1, n_nodes) if n not in port_ids]
        ypp = y[np.ix_(port_ids, port_ids)]
        if internal:
            ypi = y[np.ix_(port_ids, internal)]
            yip = y[np.ix_(internal, port_ids)]
            yii = y[np.ix_(internal, internal)]
            try:
                out[k] = ypp - ypi @ np.linalg.solve(yii, yip)
            except np.linalg.LinAlgError as exc:
                raise ModelError(
                    "建图计划网表奇异（存在悬空节点或短路支路），无法求解："
                    f"{type(exc).__name__}") from exc
        else:
            out[k] = ypp
    return out


def build_device_model(args: dict) -> dict:
    record = get_model_record(_record_id(args))
    library, cell = _valid_lib_cell(args)
    freq = _freq_args(args)
    curve_ids = record.get("curve_ids") or {}
    if record["model_kind"] == "two_port_data":
        return _build_two_port(record, library, cell, freq)
    if record["model_kind"] != "jfet_small_signal":
        raise ModelError(f"暂不支持的建图类型 {record['model_kind']}")
    if not (record.get("fit") or {}).get("selected") or record.get("stage") not in (
            "fitted", "build_planned", "schematic_built", "simulation_completed",
            "validated", "validation_failed", "validation_incomplete",
            "numeric_spec_validated"):
        raise ModelError("模型尚未完成通过规则的拟合（stage=fitted）；不能建图。"
                         "先 fit_device_model，或如实报告拟合失败。")
    kind = record["fit"]["selected"]
    params = record["fit"]["candidates"][kind]["parameters_fitted_SI"]
    configuration = record["configuration"]
    cfg = _CONFIGURATIONS[configuration]
    packaged = kind == "package_delay"
    # 幂等 cell 命名：后缀取自建图指纹（不含 cell 名），同一模型同一参数
    # 的重复 build 请求得到同一个 cell 名 —— 不再每次偷偷生成随机 cell
    # （随机 cell 会破坏幂等与历史证据对账）。
    port1_t, port2_t, common_t = cfg["port1"], cfg["port2"], cfg["common"]
    z0 = record_z0(record)
    instances = [
        {"master": "ads_simulation:TermG", "name": "P1_" + port1_t.upper(),
         "params": {"Num": "1", "Z": _fmt(z0, "Ohm")}},
        {"master": "ads_rflib:FET", "name": "DEVICE_CORE", "params": {
            "G": _fmt(params["gm_S"] * 1e3, "mS"),
            "T": _fmt(params.get("tau_s", 0.0) * 1e12, "psec"),
            "F": "0 GHz",
            "Cgs": _fmt(params["Cgs_F"] * 1e12, "pF"),
            "Cdg": _fmt(params["Cgd_F"] * 1e12, "pF"),
            "Cds": _fmt(params["Cds_F"] * 1e12, "pF"),
            "Cdc": "0 pF", "Ggs": "0 S", "Ri": "0 Ohm",
            "Rds": _fmt(1.0 / params["gds_S"] if params["gds_S"] > 0 else 1e12, "Ohm"),
        }},
        {"master": "ads_simulation:TermG", "name": "P2_" + port2_t.upper(),
         "params": {"Num": "2", "Z": _fmt(z0, "Ohm")}},
        {"master": "ads_rflib:GROUND", "name": "COMMON_" + common_t.upper()},
        {"master": "ads_simulation:S_Param", "name": "SP1", "params": {
            "Start": _fmt(freq["start"], "Hz"), "Stop": _fmt(freq["stop"], "Hz"),
            "Step": _fmt(freq["step"], "Hz"),
            "CalcS": "yes", "CalcY": "yes", "CalcNoise": "No"}},
    ]
    # 三端一致处理：外部端 -> R -> L -> 内部 FET 引脚。公共端的外部端就是
    # 参考地（GROUND 引脚），**内部公共端不得越过非零封装支路直接接地** ——
    # 否则数值正演（circuit_y2p 对 g/s/d 全部加 R/L 后消元）与 ADS 网表拓扑
    # 不一致，共栅测试会漏掉 Rg/Lg（历史缺陷）。
    connections = []
    for terminal, endpoint in (
            (port1_t, ["P1_" + port1_t.upper(), 1]),
            (port2_t, ["P2_" + port2_t.upper(), 1]),
            (common_t, ["COMMON_" + common_t.upper(), 1])):
        core_pin = ["DEVICE_CORE", _ADS_FET_PINS[terminal]]
        if packaged:
            r_name = f"PKG_R_{terminal.upper()}"
            l_name = f"PKG_L_{terminal.upper()}"
            instances.extend([
                {"master": "ads_rflib:R", "name": r_name,
                 "params": {"R": _fmt(params[f"R{terminal}_Ohm"], "Ohm"), "Noise": "No"}},
                {"master": "ads_rflib:L", "name": l_name,
                 "params": {"L": _fmt(params[f"L{terminal}_H"] * 1e9, "nH"), "Noise": "No"}},
            ])
            connections.extend([{"a": endpoint, "b": [r_name, 1]},
                                {"a": [r_name, 2], "b": [l_name, 1]},
                                {"a": [l_name, 2], "b": core_pin}])
        else:
            connections.append({"a": endpoint, "b": core_pin})
    plan = {
        "library": library, "cell": cell, "layout": "auto",
        "recreate": False, "instances": instances, "connections": connections,
    }
    topology = ("packaged_all_three_terminals" if packaged
                else "intrinsic_no_package")
    fingerprint = _build_fingerprint(record, kind, configuration, z0, params,
                                     freq, topology, plan)
    # 指纹不含 cell 名（payload 只取实例与连线），因此可以放心用它做后缀：
    # 同一模型同一参数 → 同一指纹 → 同一 cell 名（幂等）；参数/拓扑一变 →
    # 新指纹 → 新 cell（历史不被覆盖）。
    cell = f"{cell[:32]}_{fingerprint[:8]}"
    plan["cell"] = cell
    record["build"] = {
        "kind": "jfet_small_signal", "candidate": kind,
        "library": library, "cell": cell, "view": "schematic",
        "topology": topology,
        "z0": z0,
        "conditions": (record.get("curve_binding") or {}).get("conditions") or {},
        "fingerprint": fingerprint,
        "plan": plan,
        "pin_mapping": {
            "physical_package": dict(_PACKAGE_PINS_J310),
            "physical_package_note": "J310/SOT-143 样本脚号（drain=1/source=2/gate=3）；"
                                     "其它封装须按实际数据手册填写，不得照搬",
            "ads_native_symbol": {"ads_rflib:FET":
                                  {"gate": _ADS_FET_PINS["g"],
                                   "drain": _ADS_FET_PINS["d"],
                                   "source": _ADS_FET_PINS["s"]}},
            "ports": {"port1": f"{port1_t}（{cfg['port1']}）",
                      "port2": f"{port2_t}（{cfg['port2']}）",
                      "common": f"{common_t}（外部端接地）"},
            "common_branch": ("公共端经 PKG_R/PKG_L 串联支路接地（外部公共端接地，"
                              "内部公共端不直接接地）" if packaged
                              else "内部公共端直接接地（无封装支路）"),
            "note": "物理封装脚号与 ADS 原生符号脚号是两张独立映射表，已分别给出。",
        },
        "parameters_source": {"kind": "fitted", "model_id": record["model_id"],
                              "candidate": kind,
                              "note": "全部为拟合值（fitted），不是厂家原文数值"},
        "s_parameters": {"start_hz": freq["start"], "stop_hz": freq["stop"],
                         "step_hz": freq["step"]},
    }
    record["stage"] = "build_planned"
    _archive_validation(record)          # 建图/参数变了，旧验证结论作废归档
    record["validation"] = None
    _set_task(record, "packaged", note="建图计划就绪（cell=" + cell + "）")
    _save(record)
    bias_conditions = (record.get("curve_binding") or {}).get("conditions") or {}
    return {"ok": True, "model_id": record["model_id"], "stage": record["stage"],
            "build_schematic_args": plan,
            "simulation_args": {"library": library, "cell": cell},
            "pin_mapping": record["build"]["pin_mapping"],
            "topology": topology,
            "build_fingerprint": fingerprint,
            "next_steps": "调用 build_schematic 执行建图（含网表/几何门禁），"
                          "再 run_simulation，最后 validate_device_model("
                          "model_record_id=..., dataset_path=run_simulation 返回的 .ds)；"
                          "来源 simulation_id 由会话按同一数据集自动注入，不要自填。"
                          "仿真完成不等于验证通过。",
            "applicability": {
                "configuration": configuration,
                "bias": bias_conditions,
                "small_signal_only": True,
                "unsupported": record["unsupported"],
            }}


def _build_two_port(record: dict, library: str, cell: str, freq: dict) -> dict:
    """固定偏置数据二端口：从曲线记录写出 s2p，规划 S2P 组件测试电路。

    参考阻抗用曲线记录的**实际 z0**（不再是写死的 50Ω）；只有 Y 曲线时用
    该 z0 做 Y→S 换算，并把换算方法与前后样例显式记录。
    """
    curves = _load_curves(record, ("Y", "S"))
    if not curves:
        raise ModelError("缺少 S 或 Y 幅相曲线对，无法构建数据二端口")
    kind = "S" if "S" in curves else "Y"
    s_freqs, matrix, mag_unit, recs = _require_pair(curves, kind)
    z0 = record_z0(record)
    transformation = None
    if kind == "Y":
        scale = datasheet_curves.mag_scale_si(mag_unit)
        if scale is None:
            raise ModelError(f"Y 幅值轴单位 {mag_unit!r} 无法换算为 S；"
                             "请用 S/mS/uS 轴重新提取")
        y_si = matrix_stack(matrix) * scale
        s_from_y = y_to_s(y_si, z0)
        transformation = {
            "method": "Y_to_S_by_curve_z0",
            "z0_Ohm": z0,
            "mag_unit": mag_unit, "scale_to_SI": scale,
            "example": {
                "freq_hz": float(s_freqs[0]) * 1e6,
                "Y_before_SI": [[y_si[0][0, 0].real, y_si[0][0, 0].imag],
                                [y_si[0][1, 0].real, y_si[0][1, 0].imag]],
                "S_after": [[s_from_y[0][0, 0].real, s_from_y[0][0, 0].imag],
                            [s_from_y[0][1, 0].real, s_from_y[0][1, 0].imag]],
            },
            "note": "曲线记录的 Y 用记录 z0 换算为 S 后写入 S2P；换算方法与样例已记录。",
        }
        series = s_from_y
    else:
        np = _np()
        series = matrix_stack(matrix)
    model_dir = os.path.join(_root(), record["model_id"])
    os.makedirs(model_dir, exist_ok=True)
    tag = "Y2S" if kind == "Y" else "S"
    s2p_name = f"{record['part_number'].replace(' ', '_')}_{tag}_twoport.s2p"
    s2p_path = os.path.join(model_dir, s2p_name)
    origin = recs[0].get("origin") or {}
    with open(s2p_path, "w", encoding="ascii") as stream:
        stream.write("! Digitized datasheet curves (NOT a manufacturer model)\n")
        stream.write(f"! origin: {origin.get('preset', origin.get('method'))}; "
                     f"document {record['document_id']}\n")
        stream.write(f"! configuration={record['configuration']} "
                     f"z0={z0:g} (curve record z0.status)\n")
        if transformation:
            stream.write("! Y->S conversion applied with curve z0\n")
        # 参考阻抗用实际 z0（阶段二：不再写死 50）
        stream.write(f"# MHz S RI R {z0:g}\n")
        for f_mhz, m in zip(s_freqs, series):
            values = [m[0, 0], m[1, 0], m[0, 1], m[1, 1]]
            stream.write(f"{f_mhz:.9g} " +
                         " ".join(f"{v.real:.12g} {v.imag:.12g}" for v in values) + "\n")
    import hashlib as _hashlib

    with open(s2p_path, "rb") as stream:
        file_sha = _hashlib.sha256(stream.read()).hexdigest()
    instances = [
        {"master": "ads_simulation:TermG", "name": "P1", "params": {"Num": "1", "Z": f"{z0:g} Ohm"}},
        {"master": "ads_datacmps:S2P", "name": "DEVICE_DATA",
         "params": {"File": f'"{s2p_path}"', "InterpMode": "Mode0",
                    "InterpDom": "ID1", "Temp": "25",
                    "CheckPassivity": "y_n0", "EnforcePassivity": "no"}},
        {"master": "ads_simulation:TermG", "name": "P2", "params": {"Num": "2", "Z": f"{z0:g} Ohm"}},
        {"master": "ads_rflib:GROUND", "name": "COMMON_REF"},
        {"master": "ads_simulation:S_Param", "name": "SP1", "params": {
            "Start": _fmt(freq["start"], "Hz"), "Stop": _fmt(freq["stop"], "Hz"),
            "Step": _fmt(freq["step"], "Hz"),
            "CalcS": "yes", "CalcY": "yes", "CalcNoise": "No"}},
    ]
    port_order = recs[0].get("port_order")
    connections = [{"a": ["P1", 1], "b": ["DEVICE_DATA", 1]},
                   {"a": ["DEVICE_DATA", 2], "b": ["P2", 1]},
                   {"a": ["DEVICE_DATA", 3], "b": ["COMMON_REF", 1]}]
    plan = {"library": library, "cell": cell, "layout": "auto",
            "recreate": False, "instances": instances, "connections": connections}
    fingerprint = hashlib.sha256(json.dumps({
        "model_kind": "two_port_data", "model_id": record["model_id"],
        "s2p_sha256": file_sha, "z0_Ohm": z0, "source_kind": kind,
        "transformation": transformation, "frequency": freq,
        "instances": [[i["name"], i["master"], sorted((i.get("params") or {}).items())]
                      for i in instances],
        "connections": [[c["a"], c["b"]] for c in connections],
    }, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
    cell = f"{cell[:32]}_{fingerprint[:8]}"     # 幂等命名（同 JFET 路径）
    plan["cell"] = cell
    record["build"] = {
        "kind": "two_port_data", "library": library, "cell": cell, "view": "schematic",
        "s2p_path": s2p_path, "s2p_sha256": file_sha,
        "z0": z0, "source_kind": kind, "transformation": transformation,
        "port_order": port_order,
        "conditions": (record.get("curve_binding") or {}).get("conditions") or {},
        "fingerprint": fingerprint, "plan": plan,
        "s_parameters": {"start_hz": freq["start"], "stop_hz": freq["stop"],
                         "step_hz": freq["step"]},
        "parameters_source": {"kind": "digitized_curves",
                              "note": "来自曲线记录的数字化数据，不是厂家模型"},
    }
    record["stage"] = "build_planned"
    _archive_validation(record)
    record["validation"] = None
    _set_task(record, "packaged", note="建图计划就绪（cell=" + cell + "）")
    _save(record)
    return {"ok": True, "model_id": record["model_id"], "stage": record["stage"],
            "build_schematic_args": plan,
            "simulation_args": {"library": library, "cell": cell},
            "s2p": {"path": s2p_path, "sha256": file_sha, "z0": z0},
            "transformation": transformation,
            "build_fingerprint": fingerprint,
            "next_steps": "build_schematic -> run_simulation -> "
                          "validate_device_model(model_record_id=..., "
                          "dataset_path=run_simulation 返回的 .ds)；simulation_id 由会话注入。"}


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

_S_EXPRS = [f"{op}(S({i},{j}))"
            for i, j in ((1, 1), (2, 1), (1, 2), (2, 2))
            for op in ("real", "imag")]


#: 数值等价容差（**工程容差**，不是手册精度承诺）。S 无量纲、Y 是西门子，
#: 两个量纲分别判定，判据 = max(该量纲绝对下限, 相对容差 × 该量纲峰值)；
#: 绝对下限是必要的 —— 归一化误差在真值趋近 0 的元素上没有意义。
_NUMERIC_TOLERANCE = {
    "origin": "engineering_tolerance_v1",
    "note": "解析正演与 ADS 求解器的一致性工程容差；不代表手册精度。",
    "S": {"max_complex_abs": 1e-4, "max_nrmse": 1e-4},
    "Y": {"max_complex_abs": 1e-6, "max_nrmse": 1e-4},
    "plan": {"max_complex_abs": 1e-9},
}
#: 相位可信度下限：两端幅值都低于此值的频点不参与相位统计（低幅值相位不可信）
_PHASE_AMP_FLOOR = {"S": 1e-3, "Y": 1e-6}
_PLAYBACK_TOL = 1e-6
_TRACE_MAX_BYTES = 128 * 1024 * 1024


def _finite_positive(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(float(value)) or float(value) <= 0:
        raise ModelError(f"{label} 必须是正的有限数值（阈值来源须可核对）")
    return float(value)


def _overlap_mask(grid_hz, target_freqs_hz):
    """仿真网格与目标频段的交集掩码；无交集返回 None。

    只允许在**交集内**插值 —— 越界部分绝不 np.interp 两端常数填充，
    那会把"没有数据"伪装成"误差很小"。
    """
    np = _np()
    grid = np.asarray(grid_hz, dtype=float)
    target = np.asarray(target_freqs_hz, dtype=float)
    if len(grid) == 0 or len(target) == 0:
        return None
    lo = max(float(grid[0]), float(target[0]))
    hi = min(float(grid[-1]), float(target[-1]))
    tol = 1e-9 * max(abs(lo), abs(hi), 1.0)
    if hi < lo - tol:
        return None
    mask = (grid >= lo - tol) & (grid <= hi + tol)
    if not np.any(mask):
        return None
    in_target = (target >= lo - tol) & (target <= hi + tol)
    return {"mask": mask, "overlap_hz": [lo, hi],
            "grid_points": int(np.count_nonzero(mask)),
            "target_coverage": float(np.mean(in_target)),
            "grid_outside": int(len(grid) - int(np.count_nonzero(mask)))}


def _interp_on_overlap(target, target_freqs_hz, grid_hz, mask):
    """把目标曲线插值到交集内的网格点上（真插值，不外推）。"""
    np = _np()
    g = np.asarray(grid_hz, dtype=float)[mask]
    out = np.zeros((len(g), 2, 2), dtype=complex)
    for i in range(2):
        for j in range(2):
            out[:, i, j] = (
                np.interp(g, target_freqs_hz, target[:, i, j].real)
                + 1j * np.interp(g, target_freqs_hz, target[:, i, j].imag))
    return out


def _read_traces_strict(traces: dict):
    """读取 real/imag S 参数并做数据完整性硬校验。

    拒绝：缺失表达式、NaN/Inf、空数据、x/y 长度不一致、频率轴乱序/重复、
    各参数频率轴不一致（禁止静默截断或混频）。
    """
    np = _np()
    freqs = None
    s_actual = None
    for i, j in ((1, 1), (2, 1), (1, 2), (2, 2)):
        real_e = traces.get(f"real(S({i},{j}))") or {}
        imag_e = traces.get(f"imag(S({i},{j}))") or {}
        if not real_e or not imag_e:
            raise ModelError(f"数据集缺少 S({i},{j}) 的 real/imag 曲线")
        if real_e.get("error") or imag_e.get("error"):
            raise ModelError(f"数据集读取失败 S({i},{j})："
                             f"{real_e.get('error') or imag_e.get('error')}")
        xs = np.asarray(real_e.get("x") or [], dtype=float)
        xi = np.asarray(imag_e.get("x") or [], dtype=float)
        yr = np.asarray(real_e.get("y") or [], dtype=float)
        yi = np.asarray(imag_e.get("y") or [], dtype=float)
        if len(xs) == 0:
            raise ModelError(f"S({i},{j}) 数据为空")
        if not (len(xs) == len(xi) == len(yr) == len(yi)):
            raise ModelError(f"S({i},{j}) 的 x/y 长度不一致（禁止静默截断）")
        if len(xs) < 2:
            raise ModelError(f"S({i},{j}) 频点少于 2 个，无法验证")
        if not (np.all(np.isfinite(xs)) and np.all(np.isfinite(yr))
                and np.all(np.isfinite(yi))):
            raise ModelError(f"S({i},{j}) 含 NaN/Inf")
        if not np.all(np.diff(xs) > 0):
            raise ModelError(f"S({i},{j}) 的频率轴不是严格单调递增（重复/乱序频点）")
        if not np.array_equal(xi, xs):
            raise ModelError(f"S({i},{j}) 的 real/imag 频率轴不一致")
        if freqs is None:
            freqs = xs
            s_actual = np.zeros((len(xs), 2, 2), dtype=complex)
        elif not np.array_equal(xs, freqs):
            raise ModelError(f"S({i},{j}) 的频率轴与其它参数不一致（禁止混频）")
        s_actual[:, i - 1, j - 1] = yr + 1j * yi
    return freqs, s_actual


def _dataset_provenance(record: dict, dataset_path: str, simulation_id,
                        freqs) -> dict:
    """数据集来源绑定：确认数据由本模型本次建图/仿真产生。

    审查 P1-1 修复：来源**只能**来自后端可信执行记录（backend/sim_records.py，
    由 run_simulation 落盘）。此前 `simulation` 是公开参数，调用方可自填，
    而本函数对 library/cell/dataset_path 只"有值才比"、网表指纹只查非空 ——
    于是 netlist_sha='invented'、workspace='Z:/different' 也能 verified。
    现在：调用方给不出可信 simulation_id，或记录缺必需字段、含占位指纹、
    与建图记录不一致，一律 unverified。
    """
    import sim_records

    build = record.get("build") or {}
    problems = []
    evidence = {"dataset_path": dataset_path,
                "requested_simulation_id": simulation_id or None}

    # 数据集自身指纹（读取前后由 resolve/校验各算一次）
    try:
        stat = os.stat(dataset_path)
        evidence["file_size"] = int(stat.st_size)
        evidence["file_mtime"] = datetime.datetime.fromtimestamp(
            stat.st_mtime).replace(microsecond=0).isoformat()
    except OSError as exc:
        evidence["file_size"] = None
        problems.append(f"数据集文件不可读：{type(exc).__name__}: {exc}")

    # ---- 来源：只认可信执行记录 ---------------------------------------
    try:
        sim_evidence, sim_problems = sim_records.resolve(
            simulation_id, dataset_path=dataset_path, record=record)
        evidence["simulation"] = sim_evidence
        problems.extend(sim_problems)
    except sim_records.SimulationRecordError as exc:
        evidence["simulation"] = {"state": "absent", "reason": str(exc)}
        problems.append(str(exc))

    # ---- 频段必须与建图扫描一致（此前只看数值区间）-------------------
    spec = build.get("s_parameters") or {}
    try:
        band = [float(freqs[0]), float(freqs[-1])]
    except Exception:  # noqa: BLE001
        band = None
    evidence["expected_band_hz"] = [spec.get("start_hz"), spec.get("stop_hz")]
    evidence["dataset_band_hz"] = band
    if band and spec.get("start_hz") and spec.get("stop_hz"):
        tol = 1e-6 * max(abs(float(spec["stop_hz"])), 1.0)
        if abs(band[0] - float(spec["start_hz"])) > tol or \
                abs(band[1] - float(spec["stop_hz"])) > tol:
            problems.append(f"数据集频段 {band} 与建图扫描 "
                            f"{spec['start_hz']}..{spec['stop_hz']} 不一致")
    previous = (record.get("validation") or {}).get("build_fingerprint")
    if previous and previous != build.get("fingerprint"):
        problems.append("模型在建图后被重建/改动，之前的验证结论不适用于当前版本")
    return {"state": "verified" if not problems else "unverified",
            "problems": problems, "evidence": evidence,
            "build_fingerprint": build.get("fingerprint")}


def validate_device_model(args: dict, fetch_traces=None, record: dict | None = None) -> dict:
    """读真实数据集并给出**分账**验证结论（四个子结果，各自 evaluated/pass/fail）。

    子结果：import_consistency（输入回放/端口映射）、numeric_equivalence
    （解析正演 vs ADS，S 与 Y 分维度容差）、datasheet_accuracy（在手册曲线与
    仿真的交集内比误差，超域标 extrapolated）、scope_coverage（频段/条件覆盖）。

    判定纪律：
    * 任何必须的检查**失败** → validation_failed；
    * 缺少必须的检查，或**没有手册精度阈值**（准确度未评估）→ validation_incomplete，
      **不得**整体标 validated；
    * 工具执行成功（ok=true）不等于模型验收成功（verdict）。

    ``record`` 供"已封装修订在新工作区复验"注入由可信导入/放置回执重建的记录；
    普通调用不传，按 model_record_id 从磁盘读。
    """
    if record is None:
        record = get_model_record(_record_id(args))
    if record.get("stage") not in ("build_planned", "schematic_built",
                                   "simulation_completed", "validated",
                                   "validation_failed", "validation_incomplete",
                                   "numeric_spec_validated"):
        raise ModelError("模型还没有建图计划；先 build_device_model 并完成建图与仿真")
    build = record.get("build") or {}
    if not build.get("fingerprint"):
        raise ModelError("模型记录缺少建图指纹；请重新 build_device_model 后再验证")
    dataset_path = args.get("dataset_path")
    if not isinstance(dataset_path, str) or not dataset_path.strip():
        raise ModelError("需要 run_simulation 返回的真实 dataset_path")
    # 来源只能是**后端可信执行记录**：调用方自填的 simulation 一律丢弃
    # （审查 P1-1）。这里只接受一个 simulation_id 去引用那条记录。
    simulation_id = args.get("simulation_id")
    netlist_mode = bool(args.get("validation_mode") == "netlist_equivalent")
    if args.get("simulation"):
        raise ModelError(
            "simulation 来源字段不再接受自填：来源必须由后端从 run_simulation "
            "的可信执行记录读取。请只传 simulation_id（或让会话自动注入）。")
    if fetch_traces is None and isinstance(args.get("traces"), dict):
        raw = {"traces": args["traces"]}
    else:
        if fetch_traces is None:
            raise ModelError("validate_device_model 需要数据集读取通道；"
                             "请通过聊天工具调用（后端注入 read_traces）")
        raw = fetch_traces(dataset_path, _S_EXPRS)
    freqs, s_actual = _read_traces_strict(raw.get("traces") or {})
    np = _np()
    z0 = record_z0(record)
    y_actual = s_to_y(s_actual, z0)
    model_kind = record.get("model_kind")
    fit = record.get("fit") or {}
    candidate = fit.get("selected")
    curves = _load_curves(record, ("Y", "S"))
    binding = _curve_binding(curves)
    result = {
        "model_id": record["model_id"], "model_record_id": record["model_id"],
        "model_kind": model_kind, "dataset_path": dataset_path,
        # 来源 ID 取自可信执行记录，**不再在这里现造** uuid —— 现造 ID
        # 不是来源，验证时生成再回头引用等于自己证明自己。
        "simulation_id": simulation_id,
        "validated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "z0_ohm": z0, "frequency_points": int(len(freqs)),
        "freq_range_hz": [float(freqs[0]), float(freqs[-1])],
        "build_fingerprint": build.get("fingerprint"),
        "sub_results": {},
        "unvalidated_scope": list(record.get("unsupported") or _UNSUPPORTED_ALWAYS),
        "conditions": binding["conditions"],
        "curve_evidence": sorted(set(binding["evidence"])),
    }
    result["provenance"] = _dataset_provenance(record, dataset_path,
                                               simulation_id, freqs)
    # 数据集读取**之后**再核一次 SHA：防止"读完换文件"仍关联旧模型。
    if simulation_id:
        try:
            import sim_records
            if not sim_records.recheck_after_read(simulation_id):
                result["provenance"]["state"] = "unverified"
                result["provenance"]["problems"].append(
                    "数据集在验证读取前后内容不一致（文件被替换）")
        except Exception as exc:  # noqa: BLE001
            result["provenance"]["state"] = "unverified"
            result["provenance"]["problems"].append(
                f"读取后复校来源失败：{type(exc).__name__}: {exc}")

    # ---- 1. 导入一致性 ---------------------------------------------------
    import_sub = {"state": "not_evaluated", "evaluated": False, "metrics": {},
                  "threshold": {"origin": "tool_constant",
                                "max_complex_abs_error": _PLAYBACK_TOL},
                  "scope": {"kind": build.get("kind")}, "reasons": []}
    if build.get("kind") == "two_port_data" and build.get("s2p_path"):
        try:
            rows = []
            with open(build["s2p_path"], encoding="ascii") as stream:
                for line in stream:
                    text = line.strip()
                    if not text or text.startswith(("!", "#")):
                        continue
                    parts = text.split()
                    if len(parts) >= 9:
                        rows.append([float(v) for v in parts[:9]])
        except (OSError, ValueError) as exc:
            import_sub["state"] = "fail"
            import_sub["reasons"].append(f"S2P 文件不可读：{type(exc).__name__}: {exc}")
            rows = []
        if rows:
            arr = np.asarray(rows, dtype=float)
            s2p_hz = arr[:, 0] * 1e6
            tol = 1e-9 * max(abs(s2p_hz[0]), abs(s2p_hz[-1]), 1.0)
            in_band = (freqs >= s2p_hz[0] - tol) & (freqs <= s2p_hz[-1] + tol)
            if not np.any(in_band):
                import_sub["reasons"].append("仿真频段与 S2P 数据频段没有交集，无法回放核对")
            else:
                expected = np.zeros((int(np.count_nonzero(in_band)), 2, 2), dtype=complex)
                g = freqs[in_band]
                for n, (i, j) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
                    z = arr[:, 1 + 2 * n] + 1j * arr[:, 2 + 2 * n]
                    expected[:, i, j] = (np.interp(g, s2p_hz, z.real)
                                         + 1j * np.interp(g, s2p_hz, z.imag))
                err = float(np.max(np.abs(s_actual[in_band] - expected)))
                passed = err <= _PLAYBACK_TOL
                import_sub.update({
                    "state": "pass" if passed else "fail", "evaluated": True,
                    "metrics": {"max_complex_abs_error": err,
                                "compared_points": int(np.count_nonzero(in_band)),
                                "grid_points_outside_s2p_band":
                                    int(len(freqs) - int(np.count_nonzero(in_band)))},
                    "scope": {"s2p_band_hz": [float(s2p_hz[0]), float(s2p_hz[-1])],
                              "compared_band_hz": [float(g[0]), float(g[-1])]},
                })
                if not passed:
                    import_sub["reasons"].append(
                        "ADS 加载的 S2P 与写出的数据不一致（端口映射或文件内容问题）")
                if import_sub["metrics"]["grid_points_outside_s2p_band"]:
                    import_sub["reasons"].append(
                        "部分仿真频点在 S2P 频段之外（ADS 外推），未参与回放核对")
    else:
        import_sub["reasons"].append(
            "JFET 三端模型的导入一致性由 build_schematic 的网表等价门禁保证；"
            "本工具不重复判定（provenance 另行绑定数据集来源）")

    # ---- 2. 数值等价（解析正演 vs ADS） ----------------------------------
    numeric = {"state": "not_evaluated", "evaluated": False, "metrics": {},
               "threshold": {"origin": _NUMERIC_TOLERANCE["origin"],
                             "S": dict(_NUMERIC_TOLERANCE["S"]),
                             "Y": dict(_NUMERIC_TOLERANCE["Y"]),
                             "plan": dict(_NUMERIC_TOLERANCE["plan"]),
                             "note": _NUMERIC_TOLERANCE["note"]},
               "scope": {"what": "同一拟合参数的解析正演 vs ADS 仿真"}, "reasons": []}
    if model_kind == "jfet_small_signal" and candidate:
        params = fit["candidates"][candidate]["parameters_fitted_SI"]
        packaged = candidate == "package_delay"
        y_model = circuit_y2p(freqs, params, record["configuration"], packaged)
        s_model = y_to_s(y_model, z0)
        metrics_s = matrix_metrics(s_actual, s_model, _PHASE_AMP_FLOOR["S"])
        metrics_y = matrix_metrics(y_actual, y_model, _PHASE_AMP_FLOOR["Y"])
        elements = {}
        for dim, metrics, target in (("S", metrics_s, s_model), ("Y", metrics_y, y_model)):
            tol = _NUMERIC_TOLERANCE[dim]
            peak = float(np.max(np.abs(target)))
            ok_dim, limit, worst = _element_pass(metrics, tol, peak)
            elements[dim] = {"pass": bool(ok_dim), "limit": limit, "peak": peak,
                             "worst_element": worst,
                             "worst_complex_rmse": metrics[worst]["complex_rmse"]}
        # 建图计划拓扑核对（独立网表求解，能抓漏支路/端口互换）
        plan_check = {"state": "not_evaluated", "reasons": []}
        plan = build.get("plan")
        if isinstance(plan, dict) and plan.get("instances"):
            try:
                y_plan = solve_plan_y2p(plan, record["configuration"], freqs)
                delta = float(np.max(np.abs(y_plan - y_model)))
                plan_check = {
                    "state": "pass" if delta <= _NUMERIC_TOLERANCE["plan"]["max_complex_abs"]
                             else "fail",
                    "max_complex_abs_vs_forward": delta,
                    "tolerance": _NUMERIC_TOLERANCE["plan"]["max_complex_abs"],
                    "reasons": [],
                }
                if plan_check["state"] == "fail":
                    plan_check["reasons"].append(
                        "建图计划的网表与解析正演拓扑不一致（如公共端封装支路缺失/"
                        "端口互换/参数串位）")
            except ModelError as exc:
                plan_check = {"state": "fail", "reasons": [f"建图计划无法求解：{exc}"]}
        else:
            plan_check["reasons"].append("模型记录缺建图计划，无法做拓扑核对")
        all_zero = float(np.max(np.abs(s_actual))) <= 1e-12
        dim_pass = all(v["pass"] for v in elements.values())
        state = "pass" if (dim_pass and plan_check["state"] == "pass") else "fail"
        numeric = {"state": state, "evaluated": True,
                   "metrics": {"S": metrics_s, "Y": metrics_y},
                   "elements": elements, "plan_topology_check": plan_check,
                   "threshold": numeric["threshold"],
                   "scope": {"what": "同一拟合参数的解析正演 vs ADS 仿真",
                             "candidate": candidate, "packaged": packaged},
                   "reasons": []}
        if all_zero:
            numeric["reasons"].append("数据集 S 参数全为零：ADS 未得到有效结果或数据集损坏")
        for dim, info in elements.items():
            if not info["pass"]:
                numeric["reasons"].append(
                    f"{dim} 量纲不一致：最大元素 {info['worst_element']} 复数 RMSE "
                    f"{info['worst_complex_rmse']:.3e} > 容差 {info['limit']:.3e}")
        numeric["reasons"].extend(plan_check["reasons"])
    elif model_kind == "two_port_data":
        numeric["reasons"].append(
            "数据二端口的独立正演即 S2P 输入回放（见 import_consistency），不重复计算")
    else:
        numeric["reasons"].append("模型未完成通过规则的拟合（stage 不是 fitted/selected 为空）")

    # ---- 3. 手册曲线准确度（交集内比较） ---------------------------------
    accuracy_inputs = {}
    if "S" in curves:
        s_freqs, s_target, s_unit, _rs = _require_pair(curves, "S")
        accuracy_inputs["S"] = (matrix_stack(s_target),
                                np.asarray([f * 1e6 for f in s_freqs], dtype=float),
                                s_unit)
    if "Y" in curves:
        y_freqs, y_target, y_unit, _ry = _require_pair(curves, "Y")
        scale = datasheet_curves.mag_scale_si(y_unit)
        if scale is None:
            accuracy_inputs["Y_unit_error"] = y_unit
        else:
            accuracy_inputs["Y"] = (matrix_stack(y_target) * scale,
                                    np.asarray([f * 1e6 for f in y_freqs], dtype=float),
                                    y_unit)
    raw_th = args.get("thresholds") or {}
    per_dim = {}
    legacy_note = None
    for dim in ("S", "Y"):
        key = f"max_complex_rmse_{dim}"
        if key in raw_th:
            per_dim[dim] = _finite_positive(raw_th[key], key)
    if raw_th.get("max_complex_rmse") is not None:
        present = [d for d in ("S", "Y") if d in accuracy_inputs]
        if len(present) == 1:
            per_dim.setdefault(present[0],
                               _finite_positive(raw_th["max_complex_rmse"], "max_complex_rmse"))
            legacy_note = ("单维旧键 max_complex_rmse 应用于唯一的 "
                           f"{present[0]} 量纲")
        elif len(present) > 1:
            raise ModelError(
                "S 无量纲、Y 是西门子，量纲不同不能共用一个 max_complex_rmse；"
                "请分别给 max_complex_rmse_S / max_complex_rmse_Y")
    accuracy = {"state": "not_evaluated", "evaluated": False, "metrics": {},
                "thresholds": {"origin": "none", "per_dimension": per_dim},
                "scope": {"curve_kinds": sorted(k for k in ("S", "Y")
                                                if k in accuracy_inputs)},
                "reasons": []}
    if accuracy_inputs.get("Y_unit_error"):
        accuracy["reasons"].append(
            f"Y 曲线单位 {accuracy_inputs['Y_unit_error']!r} 无法换 SI，Y 准确度未评估")
    available = [d for d in ("S", "Y") if d in accuracy_inputs]
    if not available:
        accuracy["reasons"].append("模型没有可用的幅相曲线对，准确度未评估")
    else:
        compared = {}
        extrapolated = {}
        for dim in available:
            target, target_hz, _unit = accuracy_inputs[dim]
            overlap = _overlap_mask(freqs, target_hz)
            if overlap is None:
                extrapolated[dim] = {"state": "no_overlap",
                                     "curve_band_hz": [float(target_hz[0]), float(target_hz[-1])],
                                     "sim_band_hz": [float(freqs[0]), float(freqs[-1])]}
                continue
            mask = overlap["mask"]
            if dim == "S":
                grid_values = s_actual[mask]
            else:
                grid_values = y_actual[mask]
            target_on_grid = _interp_on_overlap(target, target_hz, freqs, mask)
            compared[dim] = {
                "metrics": matrix_metrics(grid_values, target_on_grid,
                                          _PHASE_AMP_FLOOR[dim]),
                "overlap_band_hz": overlap["overlap_hz"],
                "compared_points": overlap["grid_points"],
                "curve_coverage": overlap["target_coverage"],
                "grid_points_outside_curve_band": overlap["grid_outside"],
                "target_band_hz": [float(target_hz[0]), float(target_hz[-1])],
            }
        accuracy["metrics"] = {d: v["metrics"] for d, v in compared.items()}
        accuracy["scope"].update(
            {d: {k: v for k, v in compared[d].items() if k != "metrics"}
             for d in compared})
        accuracy["extrapolated"] = extrapolated
        missing_th = [d for d in compared if d not in per_dim]
        if not compared:
            accuracy["reasons"].append("仿真频段与曲线频段没有交集，准确度无法评估")
        elif missing_th:
            accuracy["reasons"].append(
                "未给手册精度阈值：" + "、".join(f"max_complex_rmse_{d}" for d in missing_th) +
                "；准确度只报告误差，不判达标（缺阈值不得整体 validated）")
        else:
            accuracy["evaluated"] = True
            accuracy["thresholds"] = {"origin": "user_rule", "per_dimension": per_dim}
            if legacy_note:
                accuracy["thresholds"]["note"] = legacy_note
            fails = []
            for dim, data in compared.items():
                worst = max(data["metrics"], key=lambda k: data["metrics"][k]["complex_rmse"])
                value = data["metrics"][worst]["complex_rmse"]
                if value > per_dim[dim]:
                    fails.append(f"{dim}{worst} 复数 RMSE {value:.3e} > 阈值 {per_dim[dim]:.3e}")
                data["verdict"] = "pass" if value <= per_dim[dim] else "fail"
                data["worst_element"] = worst
                data["worst_complex_rmse"] = value
            accuracy["state"] = "fail" if fails else "pass"
            if fails:
                accuracy["reasons"].extend(fails)
            if extrapolated:
                accuracy["reasons"].append(
                    "以下量纲的曲线频段与仿真没有交集，标 extrapolated/未验证：" +
                    "、".join(sorted(extrapolated)))

    # ---- 4. 范围覆盖 -----------------------------------------------------
    scope = {"state": "not_evaluated", "evaluated": False,
             "sim_band_hz": [float(freqs[0]), float(freqs[-1])],
             "conditions": binding["conditions"], "configuration": binding["configuration"],
             "z0_ohm": z0, "reasons": []}
    if not curves:
        scope["reasons"].append("模型没有曲线证据，无法评估覆盖范围")
    else:
        scope["evaluated"] = True
        scope["state"] = "pass"
        scope["curve_bands_hz"] = {
            d: [float(accuracy_inputs[d][1][0]), float(accuracy_inputs[d][1][-1])]
            for d in available}
        overlap_any = False
        extrapolated_bands = {}
        for dim in available:
            target_hz = accuracy_inputs[dim][1]
            ov = _overlap_mask(freqs, target_hz)
            if ov is None:
                extrapolated_bands[dim] = {"curve_band_hz":
                                           [float(target_hz[0]), float(target_hz[-1])],
                                           "state": "not_validated"}
            else:
                overlap_any = True
                scope[f"validated_band_hz_{dim}"] = ov["overlap_hz"]
        scope["overlap_any"] = overlap_any
        scope["extrapolated"] = extrapolated_bands
        if not overlap_any:
            scope["reasons"].append(
                "仿真频段与所有曲线频段都没有交集：模型只在与曲线重叠的范围内有效，"
                "本次全部属于外推（extrapolated/未验证）")
        if extrapolated_bands:
            scope["reasons"].append("部分曲线频段在仿真范围之外，标 extrapolated")
        if not binding["conditions"]:
            scope["reasons"].append("曲线缺少偏置/温度条件，适用范围无法界定")

    sub_results = {"import_consistency": import_sub, "numeric_equivalence": numeric,
                   "datasheet_accuracy": accuracy, "scope_coverage": scope}
    result["sub_results"] = sub_results

    # ---- 总判定 ---------------------------------------------------------
    failed = [name for name, sub in sub_results.items() if sub["state"] == "fail"]
    incomplete = []
    if result["provenance"]["state"] != "verified":
        failed.append("dataset_provenance")
    if model_kind == "jfet_small_signal":
        if numeric["state"] == "not_evaluated":
            incomplete.append("numeric_equivalence 未评估（缺少拟合/正演）")
    elif model_kind == "two_port_data":
        if import_sub["state"] == "not_evaluated":
            incomplete.append("import_consistency 未评估（缺少可回放的 S2P 交集）")
    if accuracy["state"] == "not_evaluated":
        incomplete.append("datasheet_accuracy 未评估（缺少手册精度阈值或无可比曲线）")
    scope_missing = not scope["evaluated"] or not scope.get("overlap_any", False)
    if scope_missing:
        incomplete.append("scope_coverage 未评估（没有与仿真重叠的可比频段）")
    if failed:
        verdict = "validation_failed"
    elif incomplete:
        verdict = "validation_incomplete"
    else:
        verdict = "validated"
    result["verdict"] = verdict
    result["verdict_reasons"] = failed + incomplete
    result["validation_scope_hz"] = {d: scope.get(f"validated_band_hz_{d}")
                                     for d in ("S", "Y")
                                     if scope.get(f"validated_band_hz_{d}")}
    # 独立网表正演只是**辅助证据**：没有走 build_schematic 的几何门禁、
    # 没有原理图连通性核对、没有来源绑定，因此**不得**升格为 validated。
    # （审查 P1-3：脚本曾用 netlist_sha="netlist_only" 占位 + 几何失败，
    #  最终仍把模型记成 validated。）
    if netlist_mode:
        numeric_state = numeric.get("state")
        if numeric_state == "fail":
            verdict = "validation_failed"
            result["verdict_reasons"] = failed + [
                "独立网表正演数值等价失败"]
        else:
            verdict = "numeric_spec_validated"
            result["verdict_reasons"] = [
                "独立网表正演仅核对模型拓扑与参数的数值等价；"
                "未走 build_schematic 几何门禁、未核对原理图连通性、"
                "无仿真来源绑定，因此不是端到端验收。"]
    result["verdict"] = verdict
    result["validation_mode"] = "netlist_equivalent" if netlist_mode else "schematic"
    if netlist_mode:
        result["provenance"]["state"] = "unverified"
        result["provenance"]["problems"].append(
            "独立网表验证没有可信仿真执行记录（未经 run_simulation 正常路径）")
    if verdict == "validated":
        result["verdict_note"] = ("导入/数值等价/准确度/覆盖四项均通过；"
                                  "validated 仅限上面给出的频段与条件。")
    elif verdict == "validation_failed":
        result["verdict_note"] = "存在失败的必要检查（见 verdict_reasons），不得称验证通过。"
    elif verdict == "numeric_spec_validated":
        result["verdict_note"] = (
            "仅独立网表正演的数值等价通过（辅助证据）。原理图建图、几何门禁、"
            "连通性与仿真来源绑定均未核验；不得据此声称模型验收通过或交互完成。")
    else:
        result["verdict_note"] = ("必要检查未全部评估：" +
                                  "；".join(incomplete) +
                                  "。状态为 validation_incomplete，不得整体称 validated。")
    _archive_validation(record)      # 先归档旧结论（此刻还在 record["validation"]）
    record["validation"] = result
    record["stage"] = verdict
    _set_task(record, "validation_completed",
              note="verdict=" + verdict)
    _save(record)
    return {"ok": True, "model_id": record["model_id"], "stage": record["stage"],
            "verdict": verdict, "verdict_reasons": result["verdict_reasons"],
            "validation_mode": result["validation_mode"],
            "sub_results": sub_results, "provenance": result["provenance"],
            "simulation_id": result["simulation_id"],
            "build_fingerprint": result["build_fingerprint"],
            "validation_scope_hz": result["validation_scope_hz"],
            "curve_error_max_complex_rmse": {
                d: max(m["complex_rmse"] for m in accuracy["metrics"][d].values())
                for d in accuracy["metrics"]},
            "independent_numeric": bool(numeric["evaluated"]),
            "unvalidated_scope": result["unvalidated_scope"],
            "note": "验证分账报告：导入一致性 / 数值等价（S、Y 分维度容差）/ "
                    "手册曲线准确度（仅在交集内、超域标 extrapolated）/ 范围覆盖；"
                    "工具执行成功（ok）与模型验收成功（validated）是两件事，"
                    "缺阈值或缺必要检查时为 validation_incomplete。"
                    "来源只认后端可信执行记录（simulation_id），"
                    "独立网表正演只记 numeric_spec_validated，不等于端到端验收。"}



# ---------------------------------------------------------------------------
# 封装元件的修订级复验（新工作区使用电路，复用手册曲线与数值判据）
# ---------------------------------------------------------------------------

def _revision_build(record: dict, *, library: str, cell: str,
                    band_hz) -> dict:
    """把修订自带的建图记录改成**使用电路**视角的建图记录。

    源记录的建图目标是验证电路（TermG 平铺、AI_lib:J310_ACC_…）；新工作区
    仿真的是封装元件放进使用电路后的 cell。两者拓扑不同、扫描步长也可能
    不同，因此不能把源指纹直接套过去 —— 这里重建一份指向真实使用电路的
    build 块，指纹按新的库/cell/频段重算，并显式标注它来自哪个修订。
    """
    source_build = record.get("build") or {}
    if not source_build.get("plan") and not source_build.get("pin_mapping"):
        raise ModelError("修订归档的建模记录缺少建图计划，无法建立使用电路复验绑定")
    if not band_hz or len(band_hz) != 2:
        raise ModelError("修订复验需要显式频段 [start_hz, stop_hz]")
    start, stop = float(band_hz[0]), float(band_hz[1])
    if not (start < stop):
        raise ModelError("频段必须 start_hz < stop_hz")
    build = dict(source_build)
    build.update({"library": library, "cell": cell, "view": "schematic",
                  "kind": "packaged_usage_circuit",
                  "s_parameters": {"start_hz": start, "stop_hz": stop,
                                   "step_hz": source_build.get(
                                       "s_parameters", {}).get("step_hz")},
                  "source_build": {"library": source_build.get("library"),
                                   "cell": source_build.get("cell"),
                                   "fingerprint": source_build.get("fingerprint")}})
    build["fingerprint"] = _usage_fingerprint(
        record, build, library=library, cell=cell, band_hz=[start, stop])
    return build


def _usage_fingerprint(record: dict, build: dict, *, library: str, cell: str,
                       band_hz: list) -> str:
    """使用电路的建图指纹。

    与验证电路指纹分开算：两者的网表本来不同（TermG 平铺 vs 封装元件放在
    使用电路里），共用一个指纹会让"换到新工作区"看起来像"模型被改动"。
    这里绑定的是同一份修订内容 + 这次实际导入的库/cell + 频段，因此参数被
    改动、换个 cell、或换频段都会得到不同指纹。
    """
    source = (build.get("source_build") or {})
    payload = {
        "scope": "packaged_usage_circuit",
        "model_id": record.get("model_id"),
        "revision_source_fingerprint": source.get("fingerprint"),
        "candidate": (record.get("fit") or {}).get("selected"),
        "configuration": record.get("configuration"),
        "z0_Ohm": float(record_z0(record)),
        "library": library, "cell": cell,
        "band_hz": [float(band_hz[0]), float(band_hz[1])],
        "parameters": sorted((((record.get("fit") or {}).get("candidates") or {})
                              .get((record.get("fit") or {}).get("selected")) or {})
                             .get("parameters_fitted_SI") or {}),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def validate_device_revision(args: dict, fetch_traces=None) -> dict:
    """已封装修订在新工作区的复验：真实使用电路 + 修订自带证据。

    与 validate_device_model 的分工：
    * 源模型验证核对"验证电路"与建图记录；
    * 本函数核对"导入后的封装元件 + 使用电路"，revision/placement 必须与
      真实回执一致，数据集必须绑到这次使用电路的网表指纹。

    复验材料全部来自修订归档（模型记录/曲线/参数快照），因此不依赖源数据根。
    """
    import device_library
    import sim_records

    revision_id = str(args.get("revision_id") or "").strip()
    placement = args.get("placement") if isinstance(args.get("placement"), dict) else {}
    root = str(args.get("library_root") or "").strip()
    if not revision_id or not root:
        raise ModelError("需要 revision_id 与 library_root（搜索模型库返回的库根）")
    if args.get("simulation"):
        raise ModelError(
            "simulation 来源字段不再接受自填：来源必须是 run_simulation 的"
            "可信执行记录，请只传 simulation_id")
    simulation_id = str(args.get("simulation_id") or "").strip()
    if not simulation_id:
        raise ModelError("缺少 simulation_id：须用标准 run_simulation 运行使用电路后引用其记录")
    dataset_path = str(args.get("dataset_path") or "").strip()
    if not dataset_path:
        raise ModelError("缺少 dataset_path（run_simulation 返回的 .ds）")

    workspace = str(args.get("workspace") or placement.get("workspace") or "").strip()
    if not workspace:
        raise ModelError("缺少 workspace：需与导入/放置登记的工作区一致")

    imports = device_library.load_imports(workspace)
    record_import = (imports.get("revisions") or {}).get(revision_id)
    if not record_import:
        raise ModelError(f"修订 {revision_id} 未导入该工作区；请先 import_device_revision")
    component_cell = str(record_import.get("cell") or "")
    component_library = str(record_import.get("library") or "")
    if not component_cell or not component_library:
        raise ModelError("导入登记缺少实际元件库/cell；请重新导入后再复验")

    # placement 指的是使用电路回执：它的 cell 是 usage cell，不是元件 cell。
    # 两者是不同层（元件子电路 vs 放元件的测试图），必须分别核对。
    usage_cell = str(placement.get("cell") or "").strip()
    usage_library = str(placement.get("library") or component_library).strip()
    recorded_cells = [str(p.get("cell") or "") for p in
                      (record_import.get("placements") or [])]
    if usage_cell:
        if usage_cell not in recorded_cells:
            raise ModelError(
                f"使用电路 {usage_cell} 不在该修订的放置记录里；"
                "请先 place_device_component 并引用其回执")
    elif recorded_cells:
        usage_cell = recorded_cells[-1]
    else:
        raise ModelError("该修订尚未放置到使用电路；请先 place_device_component")

    manifest = device_library.manifest_by_revision(root, revision_id)
    problems = device_library.verify_revision_files(root, manifest)
    if problems:
        raise ModelError("修订归档不完整，不能复验：" + "；".join(problems))
    base = device_library._revision_dir(
        root, (manifest.get("identity") or {}).get("device_id"),
        (manifest.get("identity") or {}).get("model_id"), revision_id)
    snapshot, snapshot_problems = device_library._load_snapshot(base, manifest)
    if snapshot_problems:
        raise ModelError("修订快照不可用：" + "；".join(snapshot_problems))
    source_record = snapshot.get("model_record") or {}
    if not source_record.get("model_id"):
        raise ModelError("修订快照缺少建模记录，无法复验")

    band = placement.get("band_hz") or (args.get("band_hz")
                                       if isinstance(args.get("band_hz"), list)
                                       else None)
    recorded_band = None
    for item in record_import.get("placements") or []:
        if str(item.get("cell") or "") == usage_cell:
            recorded_band = item.get("band_hz")
    if not band:
        band = recorded_band
    if band and recorded_band and [float(x) for x in band] != \
            [float(x) for x in recorded_band]:
        raise ModelError(
            f"复验频段 {band} 与放置登记的 {recorded_band} 不一致；"
            "换频段请重新放置，不能用旧使用电路的仿真结论顶替")
    build = _revision_build(source_record, library=usage_library,
                            cell=usage_cell, band_hz=band)

    # 使用电路的曲线证据来自修订快照，不用源数据根
    record = dict(source_record)
    record["curve_ids"] = source_record.get("curve_ids") or {}
    record["build"] = build

    # 仿真来源必须同时绑到"这次修订 + 这个使用电路 cell"
    sim_evidence, sim_problems = sim_records.resolve(
        simulation_id, dataset_path=dataset_path, record=record)
    binding_problems = list(sim_problems)
    sim = sim_records.get(simulation_id)
    if str(sim.get("library") or "") != usage_library:
        binding_problems.append(
            f"仿真记录库 {sim.get('library')!r} 与使用电路库 {usage_library!r} 不一致")
    if str(sim.get("cell") or "") != usage_cell:
        binding_problems.append(
            f"仿真记录 cell {sim.get('cell')!r} 与使用电路 {usage_cell!r} 不一致")
    if not sim.get("netlist_sha"):
        binding_problems.append("仿真记录缺少使用电路网表指纹")

    result = validate_device_model(args, fetch_traces=fetch_traces, record=record)
    result["binding"] = {
        "scope": "packaged_usage_circuit",
        "revision_id": revision_id,
        "workspace": workspace,
        "library": usage_library, "cell": usage_cell,
        "component": {"library": component_library, "cell": component_cell},
        "band_hz": band,
        "validation_status": (manifest.get("validation") or {}).get("verdict"),
        "archive_version": (manifest.get("archive") or {}).get("archive_version"),
        "simulation": sim_evidence,
        "problems": binding_problems,
    }
    if binding_problems:
        # 绑定不成立时不能把使用电路的数值结论当成该修订已复验
        result["verdict"] = "validation_failed"
        result["stage"] = "validation_failed"
        result["verdict_reasons"] = list(result.get("verdict_reasons") or []) + \
            binding_problems
        result["provenance"]["state"] = "unverified"
        result["provenance"]["problems"] = list(
            result["provenance"].get("problems") or []) + binding_problems
    return result


# ---------------------------------------------------------------------------
# 可放置元件封装（发布到器件模型库的产物；验证电路之外的复用形态）
# ---------------------------------------------------------------------------

def _tech_cell_name(part_number: str) -> str:
    """型号 → 合法 ADS cell 名：字母开头，仅字母数字下划线，≤64 字符。

    非法字符一律转换成下划线（原名保留在清单的 part_number 字段，符合
    「不合法字符转换后保存原名映射」的要求）。
    """
    stem = re.sub(r"[^A-Za-z0-9_]", "_", str(part_number or "").strip())
    stem = re.sub(r"_+", "_", stem).strip("_") or "DEVICE"
    if not stem[0].isalpha():
        stem = "DEF_" + stem
    return stem[:64]


def build_component_package(record: dict, cell_name: str = "") -> dict:
    """从模型记录派生**可放置元件**的子电路计划与依赖清单。

    与验证电路（TermG 顶层测试图）的区别：元件暴露**逻辑端口引脚**（1/2/3 =
    port1/port2/common），公共端作为引脚引出而不是内部接地 —— 用户在 自己的
    电路里决定公共端接哪里。内部拓扑与已验证计划完全一致（同一批 R/L/FET
    串联支路），引脚角色表写进 pin_order。

    依赖文件（two_port_data 的 S2P）以 ``{MODEL_DIR}`` 占位符引用，导入时
    替换为工作区依赖目录的绝对路径。
    """
    build = record.get("build") or {}
    if not build:
        raise ModelError("模型记录没有建图计划；请先 build_device_model")
    tech = cell_name or _tech_cell_name(record.get("part_number"))
    if not _NUM.fullmatch(tech):
        raise ModelError(f"元件名 {tech!r} 不合法（字母开头，字母数字下划线）")
    model_kind = record.get("model_kind")
    z0 = build.get("z0")
    if model_kind == "two_port_data":
        s2p_path = build.get("s2p_path")
        if not s2p_path or not os.path.isfile(s2p_path):
            raise ModelError("S2P 模型文件不存在，无法封装数据二端口元件")
        basename = os.path.basename(s2p_path)
        import hashlib as _hashlib
        with open(s2p_path, "rb") as stream:
            sha = _hashlib.sha256(stream.read()).hexdigest()
        instances = [
            {"master": "ads_datacmps:S2P", "name": "DEVICE_DATA",
             "params": {"File": f'"{{MODEL_DIR}}/{basename}"', "InterpMode": "Mode0",
                        "InterpDom": "ID1", "Temp": "25",
                        "CheckPassivity": "y_n0", "EnforcePassivity": "no"}},
        ]
        # 元件 cell 用**层次引脚（Term）**暴露逻辑端口，不用仿真 Port：
        # Port 只用于测试电路，放进子电路会在用户设计里引入额外的端口实例。
        term_anchors = [
            {"name": "port1", "number": 1, "instance": "DEVICE_DATA", "pin": 1},
            {"name": "port2", "number": 2, "instance": "DEVICE_DATA", "pin": 2},
            {"name": "common", "number": 3, "instance": "DEVICE_DATA", "pin": 3},
        ]
        plan = {"library": "", "cell": tech, "layout": "auto", "recreate": False,
                "instances": instances, "connections": [],
                "term_anchors": term_anchors}
        pin_order = [{"num": 1, "terminal": "port1", "role": "port1"},
                     {"num": 2, "terminal": "port2", "role": "port2"},
                     {"num": 3, "terminal": "common", "role": "common"}]
        deps = [{"relpath": "model/" + basename, "sha256": sha,
                 "source_path": s2p_path}]
        return {"component_plan": plan, "deps": deps, "pin_order": pin_order,
                "term_anchors": term_anchors, "tech_cell_name": tech,
                "note": "数据二端口元件：引脚 1/2 = 端口，引脚 3 = 公共参考"
                        "（S2P 组件第三脚）；固定偏置，不可用于直流求解。"}
    if model_kind != "jfet_small_signal":
        raise ModelError(f"暂不支持为 {model_kind} 生成可放置元件")
    fit = record.get("fit") or {}
    kind = fit.get("selected")
    if not kind or record.get("stage") not in (
            "fitted", "build_planned", "schematic_built", "simulation_completed",
            "validated", "validation_failed", "validation_incomplete",
            "numeric_spec_validated"):
        raise ModelError("模型尚未完成通过规则的拟合（stage=fitted）；不能封装元件")
    params = fit["candidates"][kind]["parameters_fitted_SI"]
    configuration = record.get("configuration")
    cfg = _CONFIGURATIONS.get(configuration)
    if cfg is None:
        raise ModelError(f"不支持的配置 {configuration}")
    packaged = kind == "package_delay"
    port1_t, port2_t, common_t = cfg["port1"], cfg["port2"], cfg["common"]
    terminals = (port1_t, port2_t, common_t)
    instances = [
        {"master": "ads_rflib:FET", "name": "DEVICE_CORE", "params": {
            "G": _fmt(params["gm_S"] * 1e3, "mS"),
            "T": _fmt(params.get("tau_s", 0.0) * 1e12, "psec"),
            "F": "0 GHz",
            "Cgs": _fmt(params["Cgs_F"] * 1e12, "pF"),
            "Cdg": _fmt(params["Cgd_F"] * 1e12, "pF"),
            "Cds": _fmt(params["Cds_F"] * 1e12, "pF"),
            "Cdc": "0 pF", "Ggs": "0 S", "Ri": "0 Ohm",
            "Rds": _fmt(1.0 / params["gds_S"] if params["gds_S"] > 0 else 1e12, "Ohm"),
        }},
    ]
    connections = []
    term_anchors = []
    for num, terminal in enumerate(terminals, start=1):
        core_pin = ["DEVICE_CORE", _ADS_FET_PINS[terminal]]
        if packaged:
            r_name = f"PKG_R_{terminal.upper()}"
            l_name = f"PKG_L_{terminal.upper()}"
            instances.extend([
                {"master": "ads_rflib:R", "name": r_name,
                 "params": {"R": _fmt(params[f"R{terminal}_Ohm"], "Ohm"), "Noise": "No"}},
                {"master": "ads_rflib:L", "name": l_name,
                 "params": {"L": _fmt(params[f"L{terminal}_H"] * 1e9, "nH"),
                            "Noise": "No"}},
            ])
            connections.extend([{"a": [r_name, 2], "b": [l_name, 1]},
                                {"a": [l_name, 2], "b": core_pin}])
            anchor = {"name": terminal, "number": num,
                      "instance": r_name, "pin": 1}
        else:
            anchor = {"name": terminal, "number": num,
                      "instance": "DEVICE_CORE", "pin": _ADS_FET_PINS[terminal]}
        term_anchors.append(anchor)
    plan = {"library": "", "cell": tech, "layout": "auto", "recreate": False,
            "instances": instances, "connections": connections,
            "term_anchors": term_anchors}
    pin_order = [
        {"num": 1, "terminal": port1_t, "role": "port1"},
        {"num": 2, "terminal": port2_t, "role": "port2"},
        {"num": 3, "terminal": common_t, "role": "common"},
    ]
    plan["pin_order"] = pin_order
    return {"component_plan": plan, "deps": [], "pin_order": pin_order,
            "term_anchors": term_anchors, "tech_cell_name": tech,
            "topology": ("packaged_all_three_terminals" if packaged
                         else "intrinsic_no_package"),
            "note": ("三端元件：三个引脚各经封装 R/L 到内部 FET（与验证计划同拓扑）；"
                     "公共端引出为引脚 3。" if packaged else
                     "本征模型无封装支路：三个引脚直连内部 FET。")
                   + f" 配置 {configuration}：引脚1={port1_t} 引脚2={port2_t} "
                     f"引脚3(公共)={common_t}。"}


def verify_component_plan(plan: dict, record: dict) -> dict:
    """元件计划的结构核对：端口→支路→FET 的链条与角色表一致，无内部接地。

    抓的负例：公共端被直接接地（跳过封装支路）、端口互换、漏建某端支路、
    FET 参数与拟合值不一致。与验证电路的 solve_plan_y2p 数值核对互补：
    元件是三端口（数值 2 端口求解不适用），这里做**结构等价**核对。
    """
    problems = []
    instances = {i["name"]: i for i in plan.get("instances") or []}
    connections = plan.get("connections") or []
    core = instances.get("DEVICE_CORE")
    if not core or core.get("master") != "ads_rflib:FET":
        return {"ok": False, "problems": ["缺少 DEVICE_CORE（ads_rflib:FET）"]}
    # 引脚级连通表：把连接对变成 (实例, 引脚) 邻接
    adj = {}

    def key(name, pin):
        return f"{name}:{pin}"

    for conn in connections:
        a, b = conn["a"], conn["b"]
        adj.setdefault(key(*a), []).append(key(*b))
        adj.setdefault(key(*b), []).append(key(*a))
    # R/L 的两个引脚在元件内部导通 —— 链条要穿过元件，必须补上这条内边；
    # FET 的三个引脚互相独立（内部是小信号导纳，不是短路），不补。
    for name, inst in instances.items():
        if inst.get("master") in ("ads_rflib:R", "ads_rflib:L"):
            adj.setdefault(key(name, 1), []).append(key(name, 2))
            adj.setdefault(key(name, 2), []).append(key(name, 1))
    fit = record.get("fit") or {}
    kind = fit.get("selected") or ""
    packaged = kind == "package_delay"
    cfg = _CONFIGURATIONS.get(record.get("configuration") or "")
    terminals = (cfg["port1"], cfg["port2"], cfg["common"]) if cfg else ()
    anchors = {a.get("name"): a for a in (plan.get("term_anchors") or [])}
    if not anchors:
        problems.append("元件计划缺少层次引脚定义（term_anchors）")
    for num, terminal in enumerate(terminals, start=1):
        anchor = anchors.get(terminal)
        if not anchor:
            problems.append(f"端子 {terminal} 缺少层次引脚")
            continue
        if int(anchor.get("number") or 0) != num:
            problems.append(f"端子 {terminal} 的引脚号 {anchor.get('number')} != {num}")
        start_inst = str(anchor.get("instance") or "")
        start_pin = int(anchor.get("pin") or 0)
        if not start_inst or start_inst not in instances:
            problems.append(f"端子 {terminal} 的起点实例 {start_inst} 不存在")
            continue
        chain = [key(start_inst, start_pin)]
        seen = set()
        reached_core = False
        while chain:
            node = chain.pop(0)
            if node in seen:
                continue
            seen.add(node)
            inst = node.split(":")[0]
            if inst == "DEVICE_CORE":
                reached_core = True
                break
            chain.extend(adj.get(node) or [])
        if not reached_core:
            problems.append(f"端子 {terminal} 的支路没有连到 DEVICE_CORE")
        if packaged:
            r_name, l_name = f"PKG_R_{terminal.upper()}", f"PKG_L_{terminal.upper()}"
            r, l = instances.get(r_name), instances.get(l_name)
            if not r or r.get("master") != "ads_rflib:R":
                problems.append(f"缺少封装电阻 {r_name}")
            if not l or l.get("master") != "ads_rflib:L":
                problems.append(f"缺少封装电感 {l_name}")
            else:
                expect = fit["candidates"][kind]["parameters_fitted_SI"]
                want_l = _fmt(expect[f"L{terminal}_H"] * 1e9, "nH")
                if str((l.get("params") or {}).get("L")) != want_l:
                    problems.append(
                        f"{l_name} 电感值 {(l.get('params') or {}).get('L')!r} "
                        f"与拟合值 {want_l!r} 不一致")
    # 公共端不得越过封装支路直接接地（元件里根本没有 GROUND 实例）
    grounds = [n for n, i in instances.items() if i.get("master") == "ads_rflib:GROUND"]
    if grounds:
        problems.append("元件子电路不应包含 GROUND 实例（公共端必须引出为引脚）："
                        + ", ".join(grounds))
    # 端口互换核对：起点实例必须就是该端子的 FET 引脚（无封装）或该端 R（有封装）
    if cfg:
        for num, terminal in enumerate(terminals, start=1):
            anchor = anchors.get(terminal) or {}
            want_inst = (f"PKG_R_{terminal.upper()}" if packaged else "DEVICE_CORE")
            if anchor.get("instance") != want_inst:
                problems.append(f"端子 {terminal} 的层次引脚挂错了实例："
                                f"{anchor.get('instance')} != {want_inst}（疑似端口互换）")
            if not packaged and int(anchor.get("pin") or 0) != _ADS_FET_PINS[terminal]:
                problems.append(f"端子 {terminal} 未连到对应的 FET 引脚（疑似端口互换）")
    return {"ok": not problems, "problems": problems,
            "checked": {"packaged": packaged, "terminals": list(terminals),
                        "instances": len(instances),
                        "connections": len(connections)}}


HANDLERS = {
    "prepare_device_model": prepare_device_model,
    "fit_device_model": fit_device_model,
    "build_device_model": build_device_model,
    "get_device_model": get_device_model,
    "validate_device_revision": validate_device_revision,
}


def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOL_SCHEMAS = [
    _tool("prepare_device_model", "按资料选择能成立的模型类型并建立候选记录："
          "vendor_package（走既有模型包工具）、two_port_data（固定偏置 S/Y 数据二端口）、"
          "jfet_small_signal（JFET 小信号 gm/gds/Cgs/Cgd/Cds，可选延迟与封装 R/L）。"
          "需要曲线记录（extract_datasheet_curves 的产物）作证据；证据不足时明确列出"
          "缺失项，不假装就绪。有源器件禁止用纯 RLC 代替。", {
        "document_id": {"type": "string"}, "part_number": {"type": "string"},
        "device_kind": {"type": "string", "enum": ["jfet", "mosfet", "bjt", "passive_equivalent", "unknown"]},
        "model_kind": {"type": "string", "enum": ["auto", "vendor_package", "two_port_data", "jfet_small_signal"]},
        "curve_ids": {"type": "object", "description": "如 {\"Y\": {\"mag\": id, \"phase\": id}, \"S\": {...}}",
                      "properties": {"Y": {"type": "object", "properties": {
                          "mag": {"type": "string"}, "phase": {"type": "string"}}},
                          "S": {"type": "object", "properties": {
                              "mag": {"type": "string"}, "phase": {"type": "string"}}}}},
        "parameter_set_id": {"type": "string"},
    }, ["document_id", "part_number", "device_kind", "model_kind"]),
    _tool("fit_device_model", "受约束小信号拟合：目标=固定偏置 Y 幅相曲线，S 曲线只作"
          "独立交叉核对。多候选（intrinsic/intrinsic_delay/package_delay）多初值拟合；"
          "阈值显式声明（缺省为探索性规则并标注 origin）；无一候选达标时不放宽阈值，"
          "如实报告。产出 fitted 参数（不是厂家原文）。", {
        "model_record_id": {"type": "string", "description": "建模记录 ID（64 位十六进制）；"
                            "若只有模型库身份 library_model_id（mdl_…），先用 "
                            "get_model_revision 取 source_record_id"},
        "model_id": {"type": "string", "description": "旧字段，仅兼容；等同 model_record_id"},
        "holdout_mhz": {"type": "array", "items": {"type": "number"},
                        "description": "留出频点（MHz）；不给则用确定性中频段划分"},
        "candidates": {"type": "array", "items": {"type": "string",
                       "enum": ["intrinsic", "intrinsic_delay", "package_delay"]}},
        "thresholds": {"type": "object", "properties": {
            "heldout_max_nrmse": {"type": "number"}, "gm_min_mS": {"type": "number"},
            "gm_max_mS": {"type": "number"}, "gds_max_uS": {"type": "number"}},
            "description": "用户声明的达标规则（origin=user_rule）"},
        "seed": {"type": "integer"},
    }, ["model_record_id"]),
    _tool("build_device_model", "从拟合结果或曲线记录生成建图计划（build_schematic_args）："
          "JFET 三端小信号（ads_rflib:FET 原生符号 gate=1/drain=2/source=3）或数据二端口"
          "（ads_datacmps:S2P，参考阻抗用曲线记录的实际 z0）。**含封装 R/L 时 g/s/d 三个"
          "端子都建串联支路：外部公共端接地、内部公共端不得越过非零封装支路直接接地**"
          "（支持 common_gate/common_source/common_drain）。只有 Y 曲线时用记录 z0 做 Y→S "
          "换算并记录方法。返回端口映射、拓扑与建图指纹（数据集必须绑定到该指纹）；"
          "随后调用 build_schematic、run_simulation、validate_device_model。本工具不写 ADS、不仿真。", {
        "model_record_id": {"type": "string", "description": "建模记录 ID（64 位十六进制）"},
        "model_id": {"type": "string", "description": "旧字段，仅兼容；等同 model_record_id"},
        "library": {"type": "string"},
        "cell": {"type": "string"}, "start_hz": {"type": "number"},
        "stop_hz": {"type": "number"}, "step_hz": {"type": "number"},
    }, ["model_record_id", "library", "cell", "start_hz", "stop_hz", "step_hz"]),
    _tool("validate_device_model", "模型验证：读取 run_simulation 返回的真实 .ds 数据集，"
          "分四个子结果各自给 evaluated/pass/fail/not_evaluated：1) 导入一致性（S2P 回放/"
          "端口映射）；2) 数值等价（同一参数的解析正演 vs ADS，S 与 Y **分别**用各自量纲"
          "的工程容差 + 绝对误差下限；并做建图计划网表拓扑核对）；3) 手册曲线准确度"
          "（只在与仿真的交集内比误差，超域标 extrapolated，相位按环绕角差、低幅值点标"
          "不可信）；4) 范围覆盖（频段/偏置条件）。同时绑定数据集来源（工作区、库/cell、"
          "网表指纹、数据集 SHA、频段——只凭 dataset_path 不足以关联模型）。"
          "阈值必须分维度显式给出：max_complex_rmse_S（无量纲）与 max_complex_rmse_Y（西门子）；"
          "**没有手册精度阈值时准确度只报误差并标 not_evaluated，整体 verdict 为 "
          "validation_incomplete，绝不标 validated**。verdict=validation_failed/incomplete/"
          "validated 三者语义严格区分；ok=true 只表示工具执行成功。"
          "来源只认可信 simulation_id（后端从 run_simulation 执行记录读取；"
          "会话会为同一数据集自动注入）；**不接受自填 simulation 对象**。", {
        "model_record_id": {"type": "string", "description": "建模记录 ID（64 位十六进制）"},
        "model_id": {"type": "string", "description": "旧字段，仅兼容；等同 model_record_id"},
        "dataset_path": {"type": "string", "description": "run_simulation 返回的 .ds 路径"},
        "simulation_id": {"type": "string", "description": "可信执行记录 ID（run_simulation "
                          "返回）；缺省时会话按同一 dataset_path 自动注入"},
        "thresholds": {"type": "object", "properties": {
            "max_complex_rmse_S": {"type": "number"},
            "max_complex_rmse_Y": {"type": "number"}},
            "description": "手册精度阈值；S 与 Y 量纲不同必须分开给，不共用一个阈值"},
    }, ["model_record_id", "dataset_path"]),
    _tool("get_device_model", "查询模型记录的当前状态（stage）、拟合/建图/验证证据与"
          "缺失清单。stage 流转：candidate_ready -> fitted -> build_planned -> "
          "validated / validation_failed / validation_incomplete。", {
        "model_record_id": {"type": "string", "description": "建模记录 ID（64 位十六进制）"},
        "model_id": {"type": "string", "description": "旧字段，仅兼容；等同 model_record_id"},
    }, ["model_record_id"]),
    _tool("validate_device_revision",
          "**已封装修订在新工作区的复验**：核对真实使用电路（导入后的封装元件）"
          "与修订自带证据，不依赖源数据根。与 validate_device_model 分工：那个验"
          "源模型的验证电路，这个验跨工作区复用后的使用电路。"
          "revision_id 来自 search_model_library；library_root 用其返回的库根；"
          "workspace/cell/library 必须与 import_device_revision/place_device_component "
          "的真实回执一致。来源只认 run_simulation 的可信 simulation_id。"
          "绑定不成立（换 cell、改参数、换数据集、频段不符）时 verdict 直接"
          "validation_failed，不会把数值吻合当成复验通过。", {
        "revision_id": {"type": "string"},
        "library_root": {"type": "string", "description": "模型库根（search_model_library 返回）"},
        "workspace": {"type": "string", "description": "导入/放置登记的工作区路径"},
        "simulation_id": {"type": "string", "description": "run_simulation 的可信执行记录 ID"},
        "dataset_path": {"type": "string", "description": "run_simulation 返回的 .ds 路径"},
        "placement": {"type": "object", "description": "place_device_component 的回执"
                      "（library/cell/band_hz），用于与导入登记核对"},
        "band_hz": {"type": "array", "items": {"type": "number"},
                    "description": "使用电路扫描频段 [start_hz, stop_hz]"},
        "thresholds": {"type": "object", "properties": {
            "max_complex_rmse_S": {"type": "number"},
            "max_complex_rmse_Y": {"type": "number"}},
            "description": "手册精度阈值；S 与 Y 量纲不同必须分开给"},
    }, ["revision_id", "library_root", "workspace", "simulation_id", "dataset_path"]),
]
