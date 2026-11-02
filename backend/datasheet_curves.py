"""曲线记录：矢量图数字化、轴校准、采样与持久化（标准库实现）。

记录内容按审查要求显式保存：图号、页码、轴名称/单位、线性或对数刻度、
刻度校准点、采样定义、幅/相定义、偏置与温度条件、公共端、端口顺序、
参考阻抗及其"是否手册声明"状态、提取方法与可靠性。

通用引擎不认识任何具体器件：校准、path 索引、条件全部来自调用方或
**SHA 锁定的样本适配器**（sample_adapters.py）——人工复核过的映射作为
回归数据保留，通用模块不写死本机路径或 drawing 索引。

插值用纯标准库（二分 + 线性），曲线数字化本身不依赖 numpy；
拟合/验证的矩阵运算在 device_models（numpy 可选，缺失时如实报错）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re

import datasheet_models
import doc_assets


class CurveError(ValueError):
    pass


_ID = re.compile(r"[0-9a-f]{64}\Z")
_SCALES = ("linear", "log10")
_COMPONENTS = ("mag", "phase", "real", "imag")
_CONFIGURATIONS = ("common_gate", "common_source", "common_emitter",
                   "common_base", "common_drain", "common_collector", "unspecified")
_MAX_SERIES = 24
_MAX_POINTS = 5000
_ID_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")

#: Y 幅值单位 → 西门子换算。手册里 "mmhos" 与 "mS" 同义（老式写法），
#: 通用适配器可能给出任一种写法；未知单位返回 None（调用方必须报错，
#: 绝不按 1.0 静默处理）。
_MAG_SI = {
    "S": 1.0, "mS": 1e-3, "mmhos": 1e-3, "mmho": 1e-3,
    "uS": 1e-6, "umhos": 1e-6, "umho": 1e-6, "µS": 1e-6, "μS": 1e-6,
    "": 1.0, "1": 1.0,
}

#: 复核矩阵的合法下标（两端口）
_MATRIX_LABEL = re.compile(r"([SY])([12])([12])\Z")


def mag_scale_si(unit) -> float | None:
    """Y 幅值单位 → 西门子换算系数；未知单位返回 None（调用方须报错）。"""
    if not isinstance(unit, str):
        return None
    return _MAG_SI.get(unit.strip())


def curve_evidence_state(record: dict) -> str:
    """曲线证据等级：preset_adapter（SHA 锁定样本）> reviewed > unreviewed。

    样本适配器是**人工复核过**的映射（有 SHA 门禁与来源），可作为可信回归
    适配器；通用路径提取的曲线必须经 review_datasheet_curves 显式复核。
    """
    origin = record.get("origin") or {}
    if origin.get("preset"):
        return "preset_adapter"
    if record.get("status") == "digitized_reviewed":
        return "reviewed"
    return "unreviewed"


# ---------------------------------------------------------------------------
# 基础数学（纯标准库）
# ---------------------------------------------------------------------------

def _interp(xs: list, ys: list, x: float) -> float:
    """线性插值；xs 升序。越界按端点截断（调用方先做覆盖检查）。"""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    if span <= 0:
        return ys[lo]
    frac = (x - xs[lo]) / span
    return ys[lo] + frac * (ys[hi] - ys[lo])


def _polyline(points: list) -> tuple[list, list]:
    """顶点按 x 排序去重（与样本工具 flatten 的行为一致）。"""
    ordered = sorted(points, key=lambda p: (p[0], p[1]))
    xs, ys = [], []
    for x, y in ordered:
        if xs and abs(x - xs[-1]) < 1e-9:
            continue
        xs.append(x)
        ys.append(y)
    return xs, ys


class AxisCal:
    """一根轴的校准：pdf 坐标 <-> 轴值（log10 刻度在对数域做线性插值）。"""

    def __init__(self, spec: dict, name: str):
        if not isinstance(spec, dict):
            raise CurveError(f"{name} 轴配置必须是对象")
        self.scale = spec.get("scale", "linear")
        if self.scale not in _SCALES:
            raise CurveError(f"{name} 轴刻度只能是 linear/log10")
        self.unit = spec.get("unit", "")
        if not isinstance(self.unit, str) or len(self.unit) > 32:
            raise CurveError(f"{name} 轴单位须是短文本")
        cal = spec.get("cal_points")
        if not isinstance(cal, list) or len(cal) < 2:
            raise CurveError(f"{name} 轴需要至少 2 个校准点 cal_points=[{at,value},..]")
        parsed = []
        for point in cal:
            if not isinstance(point, dict):
                raise CurveError(f"{name} 轴校准点格式为 {{at: pdf坐标, value: 轴值}}")
            at_raw, value_raw = point.get("at"), point.get("value")
            for label, raw in (("at", at_raw), ("value", value_raw)):
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                        or not math.isfinite(float(raw)):
                    raise CurveError(f"{name} 轴校准点 {label} 必须是有限数值")
            at = float(at_raw)
            value = float(value_raw)
            if not (math.isfinite(at) and math.isfinite(value)):
                raise CurveError(f"{name} 轴校准点必须是有限数值")
            if self.scale == "log10" and value <= 0:
                raise CurveError(f"{name} 轴为 log10 刻度时校准值必须为正")
            parsed.append((at, value))
        parsed.sort(key=lambda p: p[0])
        if len({p[0] for p in parsed}) != len(parsed):
            raise CurveError(f"{name} 轴校准点的 pdf 坐标不能重复")
        self._at = [p[0] for p in parsed]
        if self.scale == "log10":
            self._val = [math.log10(p[1]) for p in parsed]
        else:
            self._val = [p[1] for p in parsed]
        self.min_value = min(p[1] for p in parsed)
        self.max_value = max(p[1] for p in parsed)

    def to_pdf(self, value: float) -> float:
        if not math.isfinite(value):
            raise CurveError("采样值必须是有限数值")
        if self.scale == "log10":
            if value <= 0:
                raise CurveError("log10 轴不接受非正值")
            value = math.log10(value)
        return _interp(self._val, self._at, value)

    def require_in_domain(self, value: float) -> None:
        """采样目标超出校准域 → 拒绝（轴本身不允许外推）。"""
        if value < self.min_value or value > self.max_value:
            raise CurveError(f"采样目标 {value:g} 超出该轴校准域 "
                             f"[{self.min_value:g}, {self.max_value:g}]；不做外推")

    def to_value(self, at: float) -> float:
        v = _interp(self._at, self._val, at)
        return 10.0 ** v if self.scale == "log10" else v

    def view(self) -> dict:
        return {"unit": self.unit, "scale": self.scale,
                "cal_points": [{"at": a, "value": 10.0 ** v if self.scale == "log10" else v}
                               for a, v in zip(self._at, self._val)]}


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------

def _sample_targets(spec: dict, xcal: AxisCal) -> list:
    mode = spec.get("mode", "values")
    if mode == "values":
        values = spec.get("values")
        if not isinstance(values, list) or not 1 <= len(values) <= _MAX_POINTS:
            raise CurveError(f"sampling.values 须含 1..{_MAX_POINTS} 个目标值")
        out = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(float(value)):
                raise CurveError("sampling.values 必须是有限数值")
            out.append(float(value))
        return out
    if mode in ("log10_spaced", "linear"):
        count = spec.get("count", 101)
        start, stop = spec.get("start"), spec.get("stop")
        for label, value in (("start", start), ("stop", stop)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(float(value)):
                raise CurveError(f"sampling.{label} 必须是有限数值")
        start, stop = float(start), float(stop)
        if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= _MAX_POINTS:
            raise CurveError(f"sampling.count 须在 2..{_MAX_POINTS}")
        if start >= stop or (mode == "log10_spaced" and start <= 0):
            raise CurveError("采样区间须满足 start < stop（log10 还要求 start>0）")
        if mode == "log10_spaced":
            lo, hi = math.log10(start), math.log10(stop)
            return [10.0 ** (lo + (hi - lo) * i / (count - 1)) for i in range(count)]
        return [start + (stop - start) * i / (count - 1) for i in range(count)]
    raise CurveError("sampling.mode 只支持 log10_spaced / linear / values")


def digitize_series(pdf_points: list, xcal: AxisCal, ycal: AxisCal,
                    targets: list, margin: float = 1.5) -> list:
    """把一条折线按目标 x 值采样成 (x_value, y_value) 列表。

    margin：折线两端允许的 PDF 点级缺口（与人工复核过的样本工具一致）；
    超出缺口视为曲线不覆盖请求域，直接报错而不是悄悄外推。
    """
    xs, ys = _polyline(pdf_points)
    for value in targets:
        xcal.require_in_domain(value)
        at = xcal.to_pdf(value)
        if at < xs[0] - margin or at > xs[-1] + margin:
            raise CurveError(f"曲线未覆盖目标 x={value:g}（折线域 "
                             f"[{xcal.to_value(xs[0]):g}, {xcal.to_value(xs[-1]):g}]，"
                             "不做外推）")
    out = []
    for value in targets:
        at = xcal.to_pdf(value)
        at_clamped = min(max(at, xs[0]), xs[-1])
        py = _interp(xs, ys, at_clamped)
        out.append([value, ycal.to_value(py)])
    return out


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------

def _root() -> str:
    return os.path.join(datasheet_models._root(), "curves")


def _path(record_id: str) -> str:
    if not isinstance(record_id, str) or not _ID.fullmatch(record_id):
        raise CurveError("无效的曲线记录 ID；请使用工具返回的 curve_id")
    return os.path.join(_root(), record_id + ".json")


def _write(record_id: str, record: dict) -> None:
    target = _path(record_id)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    import tempfile

    fd, temp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def get_curve(record_id: str) -> dict:
    try:
        with open(_path(record_id), encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as exc:
        raise CurveError("曲线记录不存在；请先用 extract_datasheet_curves 提取") from exc


def save_curve(args: dict) -> dict:
    """从矢量路径提取曲线并持久化（含完整数字化证据）。

    preset 给定时（SHA 锁定样本适配器）一次提取该适配器的全部图；
    否则按调用方提供的校准与 path 索引提取单条记录。
    """
    document = datasheet_models._read("documents", args.get("document_id"))
    asset = doc_assets.asset_status(document["document_id"])
    if not asset.get("has_original"):
        raise CurveError(asset.get("reattach_hint") or "原始 PDF 不可用，无法提取曲线")
    preset = args.get("preset")
    if not preset:
        # 已复核样本按文件 SHA 确定性匹配：同一份手册不再要求聊天手填
        # 校准框与 16 条 path 索引；SHA 不匹配的文档继续走通用校准。
        import sample_adapters

        preset = sample_adapters.preset_for_sha(document.get("sha256")) or None
    if preset:
        import sample_adapters

        specs, origin = sample_adapters.build_specs(str(preset), document)
    else:
        specs = [args]
        origin = {"method": "vector_path_calibration",
                  "note": "校准点与 path 索引由调用方提供；须人工或证据复核"}
    missing = _required_spec_fields(specs)
    if missing:
        raise CurveError(
            "曲线提取缺少必填项：" + "、".join(missing) +
            "。单条提取需要 page/figure/matrix_kind/axes.x/axes.y/series，"
            "且每条 series 要 path_index；port_order 须含 port1/port2/common，"
            "z0 须为 {ohm, status: declared|assumed}。"
            "若这是已复核手册（SHA 与样本适配器一致），请传 preset。")
    saved = []
    for spec in specs:
        saved.append(_extract_and_save(document, spec, origin,
                                       via_preset=bool(preset)))
    if preset:
        return {"ok": True, "preset": preset, "curves": saved,
                "curve_ids": [s["curve_id"] for s in saved],
                "note": "样本适配器已按复核校准提取 " + str(len(saved)) +
                        " 组曲线（digitized_unreviewed）；使用前须交叉核对。"}
    return {**saved[0], "ok": True}


def _required_spec_fields(specs: list) -> list:
    """必填项预检：先一次性列出缺什么，避免逐条提取时才报同一个错。"""
    problems = []
    for n, spec in enumerate(specs, 1):
        tag = f"第 {n} 条"
        axes_spec = spec.get("axes")
        if not isinstance(axes_spec, dict) or "x" not in axes_spec or "y" not in axes_spec:
            problems.append(f"{tag} axes.x/axes.y")
        if spec.get("port_order") is None:
            problems.append(f"{tag} port_order")
        if not isinstance(spec.get("z0"), dict) or spec["z0"].get("ohm") is None:
            problems.append(f"{tag} z0.ohm")
    return problems


def _extract_and_save(document: dict, spec: dict, origin: dict,
                      via_preset: bool) -> dict:
    page = spec.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise CurveError("page 必须是正整数页码")
    figure = str(spec.get("figure") or "")
    axes_spec = spec.get("axes")
    if not isinstance(axes_spec, dict) or "x" not in axes_spec or "y" not in axes_spec:
        raise CurveError("axes 必须含 x 与 y 轴配置")
    xcal = AxisCal(axes_spec["x"], "x")
    ycal = AxisCal(axes_spec["y"], "y")
    matrix_kind = str(spec.get("matrix_kind") or "").upper()
    if matrix_kind not in ("S", "Y"):
        raise CurveError("matrix_kind 须为 S 或 Y")
    component = str(spec.get("component") or "mag").lower()
    if component not in _COMPONENTS:
        raise CurveError(f"component 只支持 {_COMPONENTS}")
    phase_unit = spec.get("phase_unit")
    if component == "phase":
        if phase_unit not in ("deg", "rad"):
            raise CurveError("phase 曲线必须声明 phase_unit=deg|rad")
    elif phase_unit:
        raise CurveError("phase_unit 仅 phase 曲线适用")
    configuration = str(spec.get("configuration") or "unspecified")
    if configuration not in _CONFIGURATIONS:
        raise CurveError(f"configuration 只支持 {_CONFIGURATIONS}")
    port_order = spec.get("port_order")
    if not isinstance(port_order, dict) or \
            set(port_order) - {"port1", "port2", "common"} or \
            not all(isinstance(v, str) and v for v in port_order.values()):
        raise CurveError("port_order 须说明 port1/port2/common 对应的器件端")
    z0 = spec.get("z0")
    if not isinstance(z0, dict) or z0.get("ohm") is None:
        raise CurveError("z0 须为 {ohm: 数值, status: declared|assumed}")
    z0_ohm = float(z0["ohm"])
    if not math.isfinite(z0_ohm) or z0_ohm <= 0:
        raise CurveError("z0.ohm 必须是正数")
    z0_status = str(z0.get("status") or "")
    if z0_status not in ("declared", "assumed"):
        raise CurveError("z0.status 必须显式写 declared（手册声明）或 assumed（假设）")
    series_spec = spec.get("series")
    if not isinstance(series_spec, list) or not 1 <= len(series_spec) <= _MAX_SERIES:
        raise CurveError(f"series 须含 1..{_MAX_SERIES} 条")
    indices = []
    labels = []
    for item in series_spec:
        if not isinstance(item, dict):
            raise CurveError("series 每条须为对象")
        label = item.get("label")
        if not isinstance(label, str) or not _ID_NAME.fullmatch(label):
            raise CurveError("series.label 须是短标识（字母开头）")
        if label in labels:
            raise CurveError(f"series.label 重复：{label}")
        labels.append(label)
        index = item.get("path_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise CurveError(f"series {label} 需要 path_index（见 read_datasheet_page "
                             "include_drawings=true 的路径清单）")
        indices.append(index)
    targets = _sample_targets(spec.get("sampling") or {}, xcal)
    import pdf_extract

    asset = doc_assets.asset_status(document["document_id"])
    drawings = pdf_extract.get_page_drawing_paths(asset["absolute_path"], page, indices)
    series_out = []
    for item, index in zip(series_spec, indices):
        path = drawings["paths"].get(str(index))
        if path is None:
            raise CurveError(f"path {index} 未返回数据")
        values = digitize_series(path["points"], xcal, ycal, targets)
        series_out.append({"label": item["label"], "path_index": index,
                           "path_bbox": path["bbox"], "values": values})
    conditions = datasheet_models.validate_conditions(spec.get("conditions"))
    payload = {
        "document_id": document["document_id"],
        "sha256": document["sha256"],
        "page": page, "figure": figure,
        "axes": {"x": xcal.view(), "y": ycal.view()},
        "matrix_kind": matrix_kind, "component": component,
        "phase_unit": phase_unit if component == "phase" else None,
        "configuration": configuration,
        "port_order": port_order, "z0": {"ohm": z0_ohm, "status": z0_status},
        "conditions": conditions,
        "sampling": {"mode": (spec.get("sampling") or {}).get("mode"),
                     "count": len(targets),
                     "start": targets[0], "stop": targets[-1]},
        "series": [{"label": s["label"], "path_index": s["path_index"],
                    "values": s["values"]} for s in series_out],
    }
    record_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           allow_nan=False).encode("utf-8")).hexdigest()
    record = {**payload, "curve_id": record_id, "origin": origin,
              "status": "digitized_unreviewed",
              "created_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
              "reliability": {
                  "calibration": "reviewed_sample_adapter" if via_preset else "provided",
                  "note": "数字化误差未量化；与另一幅图交叉核对不等于实物测量精度。"}
              }
    _write(record_id, record)
    summary = {s["label"]: f"{len(s['values'])} 点" for s in series_out}
    return {"curve_id": record_id, "page": page, "figure": figure,
            "matrix_kind": matrix_kind, "component": component,
            "configuration": configuration, "z0": record["z0"],
            "conditions": conditions, "series": summary}


def list_curves(args: dict) -> dict:
    """列出某手册的全部曲线记录摘要（按时间倒序）。"""
    document_id = args.get("document_id")
    if not isinstance(document_id, str) or not _ID.fullmatch(document_id or ""):
        raise CurveError("document_id 无效")
    root = _root()
    out = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8") as stream:
                    record = json.load(stream)
            except (OSError, ValueError):
                continue
            if record.get("document_id") != document_id:
                continue
            out.append({
                "curve_id": record["curve_id"], "page": record.get("page"),
                "figure": record.get("figure"), "matrix_kind": record.get("matrix_kind"),
                "component": record.get("component"),
                "configuration": record.get("configuration"),
                "z0": record.get("z0"), "conditions": record.get("conditions"),
                "sampling": record.get("sampling"),
                "series": [s["label"] for s in record.get("series") or []],
                "status": record.get("status"),
                "created_at": record.get("created_at"),
            })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"ok": True, "document_id": document_id, "curves": out,
            "count": len(out)}


def assemble_matrix(curves: list, matrix_kind: str, component_pair: tuple) -> tuple:
    """把 (mag 曲线, phase 曲线) 组装成复数矩阵序列。

    返回 (freqs, matrices, mag_unit)。严格校验（阶段二要求）：

    * 同一 (键, 分量) 只能出现一次 —— 重复标签直接拒绝，不静默覆盖；
    * 所有幅值记录的 y 轴单位必须一致，返回的 mag_unit 就是该唯一单位；
    * 相位单位取该键 phase 记录声明的值，**与记录遍历顺序无关**；
    * 标签去后缀后必须是 (S|Y)(1|2)(1|2)，前缀必须等于 matrix_kind，
      非法下标（S13）与错配前缀（S 里混 Y）都拒绝；
    * 必须完整给出 2×2 全部元素；
    * 每个元素的 mag/phase 采样频点必须逐点相同，否则拒绝（不插值掩盖）。
    """
    if matrix_kind not in ("S", "Y"):
        raise CurveError("matrix_kind 须为 S 或 Y")
    pair = tuple(component_pair)
    if len(pair) != 2 or set(pair) - {"mag", "phase", "real", "imag"}:
        raise CurveError("component_pair 必须是两个分量名")
    entries: dict = {}
    mag_unit = None
    for record in curves:
        if record.get("matrix_kind") != matrix_kind:
            continue
        comp = record.get("component")
        if comp not in pair:
            continue
        unit = (record.get("axes") or {}).get("y", {}).get("unit")
        if comp == pair[0]:
            if mag_unit is None:
                mag_unit = unit
            elif unit != mag_unit:
                raise CurveError(
                    f"幅值曲线的 y 轴单位不一致：{mag_unit!r} vs {unit!r}；"
                    "请统一单位后再组装，禁止混合量纲")
        for series in record.get("series") or []:
            label = series.get("label")
            values = series.get("values")
            if not isinstance(label, str) or not isinstance(values, list) or not values:
                raise CurveError("曲线 series 缺少 label 或采样值")
            key = label
            for suffix in pair:
                if key.endswith(suffix):
                    key = key[: -len(suffix)]
                    break
            match = _MATRIX_LABEL.fullmatch(key)
            if not match:
                raise CurveError(f"曲线标签 {label!r} 无法解析为两端口 {matrix_kind} 参数"
                                 "（须形如 S11mag / Y21phase，下标 1/2）")
            if match.group(1) != matrix_kind:
                raise CurveError(f"曲线标签 {label!r} 的矩阵类型与本次组装 "
                                 f"{matrix_kind} 不一致")
            entry = entries.setdefault(key, {})
            if comp in entry:
                raise CurveError(f"{key} 的 {comp} 曲线重复（标签 {label!r}）；"
                                 "同一参数同一分量只允许一条记录，拒绝静默覆盖")
            entry[comp] = values
            if comp != pair[0]:
                declared = record.get("phase_unit") or "deg"
                if declared not in ("deg", "rad"):
                    raise CurveError(f"{key} 相位单位非法：{declared!r}（deg|rad）")
                if "_phase_unit" in entry and entry["_phase_unit"] != declared:
                    raise CurveError(f"{key} 相位单位不一致："
                                     f"{entry['_phase_unit']!r} vs {declared!r}")
                entry["_phase_unit"] = declared
    if not entries:
        raise CurveError(f"没有可用的 {matrix_kind} 曲线记录")
    freqs = None
    for key, entry in entries.items():
        missing = [c for c in pair if c not in entry]
        if missing:
            raise CurveError(f"{key} 缺少 {'/'.join(missing)}；"
                             "幅值与相位曲线必须成对")
        if "_phase_unit" not in entry:
            raise CurveError(f"{key} 缺少相位曲线，无法确定相位单位")
        xs = [v[0] for v in entry[pair[0]]]
        for pair_comp in pair:
            other = [v[0] for v in entry[pair_comp]]
            if len(other) != len(xs) or any(a != b for a, b in zip(other, xs)):
                raise CurveError(f"{key} 的 {pair_comp} 采样频点与 {pair[0]} 不一致；"
                                 "幅相必须同频点（不插值掩盖）")
        for pair_comp in pair:
            for point in entry[pair_comp]:
                if len(point) != 2 or not all(
                        isinstance(v, (int, float)) and not isinstance(v, bool)
                        and math.isfinite(float(v)) for v in point):
                    raise CurveError(f"{key} 的 {pair_comp} 采样点非法（须为有限数值对）")
        if freqs is None:
            freqs = xs
        elif freqs != xs:
            raise CurveError("各曲线的采样频点不一致；请用同一 sampling 重新提取")
    if not freqs:
        raise CurveError("曲线采样频点为空")
    complete = {(1, 1), (1, 2), (2, 1), (2, 2)}
    present = set()
    for key in entries:
        m = _MATRIX_LABEL.fullmatch(key)
        present.add((int(m.group(2)), int(m.group(3))))
    lack = sorted(complete - present)
    if lack:
        raise CurveError("曲线未完整给出 2×2 参数，缺少：" +
                         ", ".join(f"{matrix_kind}{i}{j}" for i, j in lack))
    matrix = []
    for i in range(len(freqs)):
        cell = {}
        for key, entry in entries.items():
            m = _MATRIX_LABEL.fullmatch(key)
            mag = entry[pair[0]][i][1]
            phase = entry[pair[1]][i][1]
            angle = phase if entry["_phase_unit"] == "rad" else math.radians(phase)
            cell[(int(m.group(2)), int(m.group(3)))] = mag * complex(
                math.cos(angle), math.sin(angle))
        matrix.append(cell)
    return freqs, matrix, (mag_unit if mag_unit is not None else "")



# ---------------------------------------------------------------------------
# 曲线复核
# ---------------------------------------------------------------------------

_REVIEW_EVIDENCE_KEYS = ("axes", "legend", "path", "bias", "ports")


def review_datasheet_curves(args: dict) -> dict:
    """把已提取曲线的状态从 digitized_unreviewed 提升为 digitized_reviewed。

    复核是**证据声明 + 结构完整性核对**，不是"点一下就算复核"：

    * 每条曲线必须有完整轴校准（>=2 点）、图例标签、矢量路径索引、
      偏置条件、端口顺序与参考阻抗；
    * SHA 锁定的样本适配器曲线要重新核验适配器与文档 SHA 仍然匹配；
    * 调用方必须逐项声明已核对轴 / 图例 / 路径 / 偏置 / 端口证据，
      缺任一项的曲线保持原状态并如实列出缺失项。

    只有复核过的曲线（或 SHA 锁定的样本适配器曲线）才允许进入建模拼装。
    """
    ids = args.get("curve_ids")
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        raise CurveError("curve_ids 须含 1..200 个曲线记录 ID")
    evidence = args.get("evidence")
    if not isinstance(evidence, dict):
        raise CurveError("evidence 须显式声明 {axes, legend, path, bias, ports}")
    missing_decl = [k for k in _REVIEW_EVIDENCE_KEYS if evidence.get(k) is not True]
    note = str(args.get("note") or "")
    reviewed, unchanged, failures = [], [], []
    for curve_id in ids:
        record = get_curve(curve_id)
        problems = _curve_structure_problems(record)
        origin = record.get("origin") or {}
        preset = origin.get("preset")
        if preset:
            try:
                import sample_adapters

                document = datasheet_models._read("documents", record.get("document_id"))
                sample_adapters.build_specs(str(preset), document)
            except Exception as exc:  # noqa: BLE001 — 适配器复核失败即不可信
                problems.append(f"样本适配器 {preset} 复核失败：{exc}")
        if missing_decl:
            problems.extend(f"未声明已核对 {k}" for k in missing_decl)
        if problems:
            failures.append({"curve_id": curve_id, "problems": problems})
            continue
        if record.get("status") == "digitized_reviewed":
            unchanged.append(curve_id)
            continue
        updated = dict(record)
        updated["status"] = "digitized_reviewed"
        updated["reviewed_at"] = datetime.datetime.now().replace(
            microsecond=0).isoformat()
        updated["reviewed_by"] = "review_datasheet_curves"
        updated["review_evidence"] = {**{k: True for k in _REVIEW_EVIDENCE_KEYS},
                                      "note": note}
        _write(curve_id, updated)
        reviewed.append(curve_id)
    return {"ok": True, "reviewed": reviewed, "unchanged": unchanged,
            "failures": failures,
            "note": (f"已复核 {len(reviewed)} 条；{len(unchanged)} 条此前已复核；"
                     f"{len(failures)} 条证据不足保持未复核。只有复核过的曲线"
                     "（或 SHA 锁定的样本适配器曲线）可用于建模。")}


def _curve_structure_problems(record: dict) -> list:
    """逐条核对曲线记录的数字化证据是否完整（复核的最低门槛）。"""
    problems = []
    axes = record.get("axes") or {}
    for axis in ("x", "y"):
        spec = axes.get(axis) or {}
        points = spec.get("cal_points")
        if not isinstance(points, list) or len(points) < 2:
            problems.append(f"{axis} 轴缺少 >=2 个校准点")
        if spec.get("scale") not in _SCALES:
            problems.append(f"{axis} 轴刻度非法")
    series = record.get("series")
    if not isinstance(series, list) or not series:
        problems.append("缺少曲线序列")
    else:
        for item in series:
            if not _ID_NAME.fullmatch(str(item.get("label") or "")):
                problems.append(f"图例标签非法：{item.get('label')!r}")
            index = item.get("path_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                problems.append(f"缺少矢量路径索引（{item.get('label')!r}）")
    if not record.get("configuration"):
        problems.append("缺少公共端配置")
    port_order = record.get("port_order")
    if not isinstance(port_order, dict) or not port_order:
        problems.append("缺少端口顺序说明")
    z0 = record.get("z0") or {}
    if z0.get("status") not in ("declared", "assumed"):
        problems.append("参考阻抗缺少 declared/assumed 状态")
    if not record.get("conditions"):
        problems.append("缺少偏置/温度条件")
    if record.get("component") == "phase" and record.get("phase_unit") not in ("deg", "rad"):
        problems.append("相位曲线未声明 phase_unit")
    return problems


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------

HANDLERS = {"extract_datasheet_curves": save_curve,
            "list_datasheet_curves": list_curves,
            "review_datasheet_curves": review_datasheet_curves}


def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOL_SCHEMAS = [
    _tool("extract_datasheet_curves", "从原 PDF 的矢量图提取曲线并存档（图号、页码、轴"
          "单位/刻度/校准点、采样、幅相定义、偏置温度、公共端、端口顺序、z0 及其 "
          "declared/assumed 状态）。通用路径需先用 read_datasheet_page("
          "include_drawings=true) 拿 path 索引，再给 axes 校准点与 series；已复核样本"
          "可用 preset（SHA 锁定）。axes 的格式：{x:{unit,scale,cal_points:[{at,value},..]},"
          "y:{...}}；sampling：{mode: log10_spaced|linear|values, count, start, stop} 或 "
          "{mode:'values', values:[..]}；series：[{label, path_index}]。不做外推；"
          "扫描图不支持。", {
        "document_id": {"type": "string"}, "page": {"type": "integer"},
        "preset": {"type": "string", "description": "已复核样本适配器名（SHA 锁定）"},
        "figure": {"type": "string"},
        "axes": {"type": "object", "description": "{x:{unit,scale,cal_points:[{at,value},..]}, y:{...}}"},
        "series": {"type": "array", "description": "[{label, path_index}]，索引来自页面矢量路径清单"},
        "sampling": {"type": "object", "description": "{mode, count, start, stop} 或 {mode:'values', values:[..]}"},
        "matrix_kind": {"type": "string", "enum": ["S", "Y"]},
        "component": {"type": "string", "enum": ["mag", "phase"]},
        "phase_unit": {"type": "string", "enum": ["deg", "rad"]},
        "configuration": {"type": "string"},
        "port_order": {"type": "object"},
        "z0": {"type": "object", "description": "{ohm: 数值, status: declared|assumed}"},
        "conditions": {"type": "object", "description": "结构化偏置条件，如 vds_v/id_a/temperature_c"},
    }, ["document_id"]),
    _tool("list_datasheet_curves", "列出某手册已提取的曲线记录摘要（图号/页码/类型/条件/"
          "采样/状态），供模型选择与拟合引用 curve_id。", {
        "document_id": {"type": "string"},
    }, ["document_id"]),
    _tool("review_datasheet_curves", "把曲线从 digitized_unreviewed 复核为 "
          "digitized_reviewed：逐条核对轴校准（>=2 点）、图例标签、矢量路径索引、"
          "偏置条件、端口顺序与参考阻抗，并显式声明已核对轴/图例/路径/偏置/端口证据；"
          "样本适配器曲线会重新核验 SHA 锁。**只有复核过的曲线（或 SHA 锁定的样本"
          "适配器曲线）才能用于建模**。证据缺项的曲线保持未复核并如实列出。", {
        "curve_ids": {"type": "array", "description": "要复核的 curve_id 列表"},
        "evidence": {"type": "object",
                     "description": "{axes, legend, path, bias, ports: 全部为 true 才算声明齐全}"},
        "note": {"type": "string"},
    }, ["curve_ids", "evidence"]),
]
