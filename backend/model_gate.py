# -*- coding: utf-8 -*-
"""模型门禁：① 结果复用的模型侧核对 ② 模型条件（频段/阻抗/偏压/温度）覆盖门禁。

纯标准库 + 本地清单，不依赖 ADS / Qt，可离线测试。

为什么这两件事必须做成**门禁**而不是提示
----------------------------------------
提示会被忽略，门禁不会。把"模型频率范围"写成一句给 LLM 的说明文字，
LLM 照样会看着 ADS 外推出来的漂亮曲线说"增益达标"—— 而那条曲线在
模型有效频段之外是**没有依据**的。所以覆盖不足 / 条件未知时，这里直接
把指标判定为 ``pass=None``（未知），并给出机器可读的原因，而不是放行。

两条规则（保守方向，宁可多跑、宁可说不知道）
--------------------------------------------
1. **复用核对**：拿不到当前模型证据 / 旧结果没有模型证据 / 依赖无法完整
   解析 —— 一律不复用，重新仿真。
2. **条件覆盖**：模型有效频段不覆盖目标频段、或频段/条件本身未知 ——
   指标一律不判达标（unknown），并在结果里写明"需核实"。
"""

import os
import re

import model_store

# 与 addon/ads_agent/model_deps.py 保持一致（两边是同一套语义，改要一起改）
STATE_NONE = "none"
STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_MISSING = "missing"

# 频段比较的相对容差：模型标称 6 GHz、目标 6 GHz 视为覆盖（浮点表示误差级）
_FREQ_TOL = 1e-6
# 阻抗比较的相对容差：50 与 50.0000001 视为相同
_Z_TOL = 0.01


# ---------------------------------------------------------------------------
# 证据归一化（兼容"旧结果只有 model_fingerprint 字典"的形态）
# ---------------------------------------------------------------------------

def normalize_evidence(raw):
    """把各种历史形态的模型证据归一成统一结构；认不出来返回 None。

    兼容路径：2026-10-08 之前的结果只有 ``model_fingerprint``（``{路径: 哈希}``
    字典）。它记录的是"当时确实解析出文件并算了哈希"，所以按
    ``complete`` 处理是**保守且正确**的（少算只会多跑一次）。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if raw.get("state"):
        return {
            "state": str(raw.get("state")),
            "fingerprint": str(raw.get("fingerprint") or ""),
            "n_deps": int(raw.get("n_deps") or 0),
            "deps": list(raw.get("deps") or []),
            "unresolved": list(raw.get("unresolved") or []),
            "missing": list(raw.get("missing") or []),
            "kits": list(raw.get("kits") or []),
            "notes": list(raw.get("notes") or []),
            "model_fingerprint": dict(raw.get("model_fingerprint") or {}),
        }
    # 旧形态：{路径: 哈希}
    pairs = {str(k): str(v) for k, v in raw.items()
             if isinstance(v, str) and str(k)}
    if not pairs:
        return None
    return {
        "state": STATE_COMPLETE,
        "fingerprint": "",
        "n_deps": len(pairs),
        "deps": [{"path": k, "sha256": v} for k, v in pairs.items()],
        "unresolved": [], "missing": [], "kits": [], "notes": [],
        "model_fingerprint": pairs,
    }


def _hash_map(evidence: dict) -> dict:
    """证据里"路径 → 内容哈希"的映射（含套件级指纹）。"""
    ev = evidence or {}
    out = {}
    for dep in ev.get("deps") or []:
        path, sha = str(dep.get("path") or ""), str(dep.get("sha256") or "")
        if path and sha:
            out[path] = sha
    for kit in ev.get("kits") or []:
        root, fp = str(kit.get("root") or ""), str(kit.get("fingerprint") or "")
        if root and fp:
            out[f"kit:{root}"] = fp
    if not out and ev.get("model_fingerprint"):
        out.update({str(k): str(v) for k, v in ev["model_fingerprint"].items()})
    return out


# ---------------------------------------------------------------------------
# ① 复用核对
# ---------------------------------------------------------------------------

def compare_model_evidence(old, current) -> dict:
    """复用前核对模型依赖，决定这份旧结果还能不能用。

    ``old``      : 当初仿真时记下的模型证据（None = 旧结果没有指纹）
    ``current``  : 现在重新扫出来的证据（None = 这次拿不到）

    返回 ``{"reuse": bool, "state": str, "reason": str, "changed": [...],
    "note": str}``。``reuse=False`` 时一定带一句人话原因 —— 用户有权知道
    "为什么又重新仿真了"。
    """
    cur = normalize_evidence(current)
    old_ev = normalize_evidence(old)

    if cur is None:
        return {
            "reuse": False, "state": "", "reason": "model_evidence_unavailable",
            "changed": [],
            "note": "无法确认该设计引用的模型文件未发生变化（本次拿不到模型依赖证据），"
                    "按保守策略放弃复用旧数据集，将重新仿真。",
        }

    if cur["state"] == STATE_NONE:
        # 设计不引用外部模型 —— 网表指纹已经覆盖全部输入，模型侧无需核对
        if old_ev and old_ev.get("state") not in (STATE_NONE, ""):
            return {"reuse": False, "state": cur["state"],
                    "reason": "model_dependency_set_changed", "changed": [],
                    "note": "当前设计已不再报告旧结果中的模型依赖，按保守策略重新仿真。"}
        return {"reuse": True, "state": cur["state"], "reason": "no_external_model",
                "changed": [], "note": ""}

    if old_ev is None:
        return {
            "reuse": False, "state": cur["state"],
            "reason": "old_result_has_no_model_evidence", "changed": [],
            "note": "旧结果里没有模型依赖记录（无法确认当时的模型内容），"
                    "按保守策略不复用，将重新仿真。",
        }

    if cur["state"] == STATE_MISSING or (cur.get("missing") and not cur.get("deps")):
        names = ", ".join(str(m.get("raw") or m.get("expanded") or "?")
                          for m in (cur.get("missing") or [])[:3])
        return {
            "reuse": False, "state": cur["state"], "reason": "model_dependency_missing",
            "changed": [],
            "note": f"模型依赖缺失（{names}），旧结果不能代表当前状态，将重新仿真。",
        }

    old_map, cur_map = _hash_map(old_ev), _hash_map(cur)
    changed = sorted(k for k in (set(old_map) | set(cur_map))
                     if old_map.get(k) != cur_map.get(k))
    if changed:
        return {
            "reuse": False, "state": cur["state"], "reason": "model_content_changed",
            "changed": changed,
            "note": (f"检测到模型内容已变化（{', '.join(_short(c) for c in changed[:3])}"
                     f"{' 等' if len(changed) > 3 else ''}），"
                     f"已放弃复用旧数据集，将重新仿真。"),
        }

    if cur["state"] == STATE_INCOMPLETE or old_ev["state"] == STATE_INCOMPLETE:
        # 不完整状态可能包含无法定位的文件/变量引用。整体指纹只覆盖已经
        # 解析的文件，除非未知部分有明确覆盖，否则相同指纹不能证明模型未变。
        return {
            "reuse": False, "state": cur["state"], "reason": "model_deps_unresolved",
            "changed": [],
            "note": "模型依赖无法完整解析（相对路径/VAR/层次引用或套件级指纹变化），"
                    "不能确认所有模型内容未变，按保守策略重新仿真。",
        }

    # 两边都是 complete：已解析文件逐项一致 -> 可以复用
    return {"reuse": True, "state": cur["state"], "reason": "model_deps_match",
            "changed": [], "note": ""}


def _short(path: str, keep: int = 2) -> str:
    """路径太长时只留末尾几段（展示用，不用于判定）。"""
    parts = str(path or "").replace("\\", "/").split("/")
    return "/".join(parts[-keep:]) if len(parts) > keep else str(path)


# ---------------------------------------------------------------------------
# ② 模型条件（频段 / 阻抗 / 偏压 / 温度）
# ---------------------------------------------------------------------------

def _match_index_entry(dep_path: str, entries: list):
    """用**文件路径**在模型索引里定位条目。

    只认"路径精确落在某条索引记录上"的匹配。型号相近不算（``BFP181``
    与 ``BFP181W`` 是两个型号），靠名字猜出来的一条比明说查不到更危险。
    """
    norm = str(dep_path or "").replace("\\", "/").lower()
    if not norm:
        return None
    fallback = None
    for entry in entries:
        rel = str(entry.get("relpath") or "").replace("\\", "/")
        if not rel:
            continue
        if not norm.endswith("/" + rel.lower()):
            continue
        pid = str(entry.get("package_id") or "")
        if pid and ("/" + pid + "/") in norm:
            return entry        # 连包都对得上 —— 最可靠
        if fallback is None:
            fallback = entry
    return fallback


def resolve_conditions(workspace: str, evidence: dict,
                       required: dict | None = None) -> list:
    """把"这次仿真用了哪些模型"翻译成可判定的有效条件清单。

    每项只有**索引里真实写着**的字段（来自模型文件头解析），查不到就是
    ``available=False`` + 原因 —— 不拿别的元件/别的变体的数据凑。

    ``required`` 可给 ``{"bias": {...}, "temperature_c": 数值}``：用户/指标
    要求的工作点。给了就会核对变体是否匹配；没给则只如实记录。
    """
    ev = normalize_evidence(evidence) or {}
    workspace = str(workspace or "").strip()
    entries: list = []
    index_available = False
    if workspace:
        try:
            root = model_store.store_root(workspace)
            live = [r for r in model_store.list_packages(root)
                    if r.get("state") in (model_store.STATE_READY,
                                          model_store.STATE_PENDING_VERIFY)]
            models = [m for r in live for m in (r.get("models") or [])
                      if isinstance(m, dict)]
            if models:
                index_available = True
                found = model_store.find_models(root, models=models)
                entries = list(found.get("hits") or [])
        except Exception:  # noqa: BLE001 — 索引读不了就如实标"查不到"
            entries = []

    conditions: list = []
    for dep in ev.get("deps") or []:
        path = str(dep.get("path") or "")
        entry = _match_index_entry(path, entries) if entries else None
        if entry is None:
            conditions.append({
                "source": "unresolved",
                "path": path,
                "variant": str(dep.get("variant") or ""),
                "package_id": str(dep.get("package_id") or ""),
                "available": False,
                "ports": int(dep["ports"]) if dep.get("ports") else None,
                "freq_start_hz": None, "freq_stop_hz": None,
                "reference_impedance_ohm": None, "bias": {}, "temperature_c": None,
                "reason": ("当前工作区的已导入模型包索引里没有这个文件"
                           if index_available else
                           "当前工作区没有已导入的模型包，无法核对模型条件"),
            })
            continue
        conditions.append({
            "source": f"model_index:{entry.get('package_id') or '?'}",
            "path": path,
            "variant": str(entry.get("relpath") or dep.get("variant") or ""),
            "part": entry.get("part"),
            "vendor": entry.get("vendor"),
            "package_id": entry.get("package_id"),
            "available": True,
            "ports": entry.get("ports"),
            "freq_start_hz": entry.get("freq_start_hz"),
            "freq_stop_hz": entry.get("freq_stop_hz"),
            "reference_impedance_ohm": entry.get("reference_impedance_ohm"),
            "reference_impedance_source": entry.get("reference_impedance_source"),
            "bias": dict(entry.get("bias") or {}),
            "temperature_c": None,
            "notes": list(entry.get("notes") or []),
        })

    # 套件级保守指纹：只知道"用了某个套件的元件"，不知道具体型号与变体
    for kit in ev.get("kits") or []:
        conditions.append({
            "source": f"design_kit:{kit.get('library') or '?'}",
            "path": str(kit.get("root") or ""),
            "variant": "", "part": None, "package_id": None,
            "available": False,
            "ports": None, "freq_start_hz": None, "freq_stop_hz": None,
            "reference_impedance_ohm": None, "bias": {}, "temperature_c": None,
            "reason": ("该元件来自 Design Kit，本次只能给出套件级保守指纹，"
                       "定位不到具体型号与有效条件"),
        })
    # 缺失/未解析的依赖同样是"条件未知"
    for item in (ev.get("missing") or []) + (ev.get("unresolved") or []):
        conditions.append({
            "source": "unresolved",
            "path": str(item.get("expanded") or item.get("raw") or ""),
            "variant": "", "part": None, "package_id": None,
            "available": False,
            "ports": None, "freq_start_hz": None, "freq_stop_hz": None,
            "reference_impedance_ohm": None, "bias": {}, "temperature_c": None,
            "reason": str(item.get("reason") or "模型依赖无法解析"),
        })

    if required:
        for cond in conditions:
            cond["required"] = dict(required)
    return conditions


def evaluate_coverage(band: dict, conditions: list,
                      reference_ohm=None, required: dict | None = None) -> dict:
    """模型条件覆盖门禁。

    返回 ``{"state": str, "block": bool, "reasons": [...], "per_model": [...],
    "message": str}``。

    ``state``：
        none          没有模型参与（普通理想元件设计）—— 门禁不生效；
        covered       所有模型的有效频段都覆盖目标频段；
        out_of_range  至少一个模型的有效频段**明确不覆盖**目标频段 ——
                      ADS 在该区间给出的是插值/外推结果，不能据此报达标；
        unknown       频段或条件查不到 —— 同样不能报达标。

    ``block=True`` 时调用方必须把指标判成 unknown（pass=None）。
    """
    conditions = list(conditions or [])
    if not conditions:
        return {"state": "none", "block": False, "reasons": [], "per_model": [],
                "message": "本次仿真没有引用外部模型，频段覆盖由数据本身决定。"}

    band = band or {}
    lo = _to_float(band.get("start_hz"))
    hi = _to_float(band.get("stop_hz"))

    per_model = []
    reasons = []
    worst = "covered"
    _RANK = {"covered": 0, "unknown": 1, "impedance_mismatch": 2,
             "condition_mismatch": 3, "out_of_range": 4}

    for cond in conditions:
        state = "covered"
        detail = []
        if not cond.get("available"):
            state = "unknown"
            detail.append(str(cond.get("reason") or "模型条件未知，无法核对"))
        else:
            f_start = _to_float(cond.get("freq_start_hz"))
            f_stop = _to_float(cond.get("freq_stop_hz"))
            if f_start is None or f_stop is None:
                state = "unknown"
                detail.append("索引里没有该模型的频率范围（无法核对覆盖）")
            elif lo is None or hi is None:
                state = "unknown"
                detail.append("任务没有给出目标频段，无法核对覆盖")
            else:
                span = max(abs(f_stop - f_start), abs(hi - lo), 1.0)
                tol = span * _FREQ_TOL
                if lo < f_start - tol or hi > f_stop + tol:
                    state = "out_of_range"
                    detail.append(
                        f"模型有效频段 {_hz(f_start)}–{_hz(f_stop)} 不覆盖目标频段 "
                        f"{_hz(lo)}–{_hz(hi)}；超出部分是插值/外推结果，不能据此报达标")
            # 参考阻抗：两边都明确知道才比（不知道就不制造假的失配）
            z_model = _to_float(cond.get("reference_impedance_ohm"))
            z_design = _to_float(reference_ohm)
            if z_model and z_design and abs(z_model - z_design) > z_design * _Z_TOL:
                if _RANK["impedance_mismatch"] > _RANK[state]:
                    state = "impedance_mismatch"
                detail.append(f"模型参考阻抗 {z_model:g}Ω 与设计参考阻抗 {z_design:g}Ω 不一致")
            # 偏压 / 温度：给了要求才核对
            req_bias = dict((required or {}).get("bias") or {})
            if req_bias:
                bias = dict(cond.get("bias") or {})
                if bias and not _bias_matches(bias, req_bias):
                    if _RANK["condition_mismatch"] > _RANK[state]:
                        state = "condition_mismatch"
                    detail.append(
                        f"模型偏压条件 {_fmt_bias(bias)} 与要求的 "
                        f"{_fmt_bias(req_bias)} 不一致")
                elif not bias:
                    state = "unknown" if state == "covered" else state
                    detail.append("模型偏压条件未知，无法核对工作点")
            temp = _to_float((required or {}).get("temperature_c"))
            if temp is not None and cond.get("temperature_c") is None \
                    and cond.get("available"):
                state = "unknown" if state == "covered" else state
                detail.append(f"要求 {temp:g}℃，但模型索引里没有温度条件")

        per_model.append({
            "source": cond.get("source") or "",
            "part": cond.get("part"),
            "variant": cond.get("variant") or "",
            "package_id": cond.get("package_id"),
            "state": state,
            "freq_start_hz": cond.get("freq_start_hz"),
            "freq_stop_hz": cond.get("freq_stop_hz"),
            "reference_impedance_ohm": cond.get("reference_impedance_ohm"),
            "bias": dict(cond.get("bias") or {}),
            "detail": detail,
        })
        if _RANK[state] > _RANK[worst]:
            worst = state
        if state != "covered":
            reasons.append({"model": cond.get("part") or cond.get("variant")
                            or cond.get("source") or "?", "state": state,
                            "detail": "; ".join(detail)})

    block = worst != "covered"
    if worst == "out_of_range":
        message = ("有模型的有效频段不覆盖目标频段 —— 超出部分是 ADS 的插值/外推，"
                   "本次指标一律不判达标，需改用覆盖该频段的型号或用实测确认。")
    elif worst == "condition_mismatch":
        message = "模型的工作条件（偏压/温度）与要求不一致，指标不判达标。"
    elif worst == "impedance_mismatch":
        message = "模型的参考阻抗与设计参考阻抗不一致，指标不判达标。"
    elif worst == "unknown":
        message = ("模型的有效频段或工作条件无法确认（索引里没有可引用的原厂数据），"
                   "本次指标一律判为「未知 / 需核实」，不报达标。")
    else:
        message = "所有模型的有效频段都覆盖目标频段。"

    return {"state": worst, "block": block, "reasons": reasons,
            "per_model": per_model, "message": message}


def _bias_matches(bias: dict, required: dict) -> bool:
    """偏压是否匹配。型号名相近不算依据 —— 只比**索引里真实解析出的**数值。

    要求的键在模型里查不到 -> 视为不匹配（保守）。
    """
    for key, want in (required or {}).items():
        got = None
        for k, v in (bias or {}).items():
            if str(k).upper() == str(key).upper():
                got = v
                break
        if got is None:
            return False
        if not _value_close(got, want):
            return False
    return True


def _value_close(got, want) -> bool:
    a, unit_a = _parse_quantity(got)
    b, unit_b = _parse_quantity(want)
    if a is None or b is None:
        return str(got).strip().lower() == str(want).strip().lower()
    if unit_a != unit_b:
        # 不允许把 10 mA 与 10 A 当成相同，也不猜无单位数字的物理量纲。
        if not unit_a or not unit_b:
            return False
        converted = _unit_scale(unit_a)
        wanted = _unit_scale(unit_b)
        if converted is None or wanted is None or converted[0] != wanted[0]:
            return False
        a *= converted[1]
        b *= wanted[1]
    return abs(a - b) <= max(abs(b), 1e-12) * 1e-3


_QUANTITY_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-zµμΩ°]+)?\s*$")
_UNIT_PREFIX = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6,
                "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3,
                "K": 1e3, "M": 1e6, "G": 1e9}
_UNIT_BASES = {"A": "current", "V": "voltage", "W": "power",
               "Hz": "frequency", "Ohm": "resistance", "ohm": "resistance",
               "Ω": "resistance", "F": "capacitance", "H": "inductance"}


def _parse_quantity(value):
    if isinstance(value, (int, float)):
        return float(value), ""
    match = _QUANTITY_RE.match(str(value or ""))
    if not match:
        return None, ""
    try:
        return float(match.group(1)), match.group(2) or ""
    except ValueError:
        return None, ""


def _unit_scale(unit):
    if unit in _UNIT_BASES:
        return _UNIT_BASES[unit], 1.0
    if len(unit) > 1 and unit[0] in _UNIT_PREFIX and unit[1:] in _UNIT_BASES:
        return _UNIT_BASES[unit[1:]], _UNIT_PREFIX[unit[0]]
    return None


def _num_of(value):
    if isinstance(value, (int, float)):
        return value
    text = str(value or "")
    for token in text.replace(",", " ").split():
        num = _to_float(token)
        if num is not None:
            return num
    return None


def _to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _hz(value) -> str:
    v = _to_float(value)
    if v is None:
        return "?"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(v) >= scale:
            return f"{v / scale:g}{unit}"
    return f"{v:g}Hz"


def _fmt_bias(bias: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in dict(bias or {}).items()) or "(未标注)"


def describe_conditions(conditions: list, gate: dict | None = None) -> str:
    """结果页展示用：模型来源与有效条件（一行一个模型，人话）。"""
    lines = []
    for cond in conditions or []:
        part = cond.get("part") or cond.get("variant") or "（未识别型号）"
        source = cond.get("source") or "来源未知"
        if not cond.get("available"):
            lines.append(f"{part}：{source} —— 有效条件未知（{cond.get('reason') or '索引里没有可引用数据'}）")
            continue
        bits = []
        if cond.get("freq_start_hz") and cond.get("freq_stop_hz"):
            bits.append(f"频段 {_hz(cond['freq_start_hz'])}–{_hz(cond['freq_stop_hz'])}")
        else:
            bits.append("频段未知")
        if cond.get("reference_impedance_ohm"):
            bits.append(f"Z0 {cond['reference_impedance_ohm']:g}Ω")
        if cond.get("bias"):
            bits.append("偏压 " + _fmt_bias(cond["bias"]))
        if cond.get("package_id"):
            bits.append(f"包 {cond['package_id']}")
        lines.append(f"{part}：{source} —— " + "，".join(bits))
    if gate and gate.get("message"):
        lines.append(gate["message"])
    return "\n".join(lines)
