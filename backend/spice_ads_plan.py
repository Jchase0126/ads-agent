"""Create a review-only ADS build plan from the supported SPICE Circuit IR.

This module never writes to ADS. It emits only mappings with workspace examples
as evidence and marks uncertain device/source syntax as blocking diagnostics.
"""
from __future__ import annotations

import re
import math
from typing import Any


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")
_MAP = {
    "resistor": ("ads_rflib:R", "R", "Ohm"),
    "inductor": ("ads_rflib:L", "L", "H"),
    "capacitor": ("ads_rflib:C", "C", "F"),
}


def compile_ads_build_plan(parsed: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic proposal; ``ready`` means mapping-safe, not sim-ready."""
    ir = parsed.get("circuit_ir") if isinstance(parsed, dict) else None
    if not isinstance(ir, dict):
        return {"ready": False, "instances": [], "connections": [],
                "diagnostics": [{"code": "missing_ir", "message": "缺少有效 Circuit IR。"}]}
    diagnostics: list[dict[str, str]] = []
    if not parsed.get("ok"):
        diagnostics.append({"code": "incomplete_source", "message": "源网表解析不完整；禁止从部分 IR 生成建图计划。"})
    if ir.get("hierarchy"):
        diagnostics.append({"code": "hierarchy_not_flattened", "message": "含子电路层级，当前计划器不展开层级。"})
    instances: list[dict[str, Any]] = []
    source_map: dict[str, str] = {}
    component_names: dict[str, str] = {}
    names: set[str] = set()
    for comp in ir.get("components") or []:
        ref = str(comp.get("reference") or comp.get("id") or "")
        kind = comp.get("kind")
        if kind in _MAP:
            master, parameter, unit = _MAP[kind]
            raw = next((p for p in comp.get("parameters", []) if p.get("name") == "value"), {})
            value = raw.get("parsed") or {}
            if any(p.get("name") != "value" for p in comp.get("parameters", [])):
                diagnostics.append({"code": "extra_component_parameters", "message": f"{ref} 含映射器未处理的附加参数。"})
                continue
            if value.get("kind") != "scalar" or value.get("si_value") is None:
                diagnostics.append({"code": "non_scalar_value", "message": f"{ref} 的参数不是可安全映射的标量。"})
                continue
            suffix = str(value.get("unit_suffix") or "").lower()
            expected_units = {"resistor": {"", "ohm", "ohms"},
                              "inductor": {"", "h", "henry", "henries"},
                              "capacitor": {"", "f", "farad", "farads"}}[kind]
            if (suffix not in expected_units or not math.isfinite(float(value["si_value"]))
                    or float(value["si_value"]) <= 0):
                diagnostics.append({"code": "invalid_component_value", "message": f"{ref} 的单位与物理取值不符合 {kind} 映射要求。"})
                continue
            ads_value = f"{float(value['si_value']):.12g} {unit}"
        elif kind == "voltage_source":
            params = comp.get("parameters") or []
            dc = next((p for p in params if p.get("name") == "dc"), None)
            if dc is None or any(p.get("name") not in {"dc"} for p in params):
                diagnostics.append({"code": "source_mapping_unverified", "message": f"{ref} 不是已支持的纯 DC 独立电压源；AC/PULSE 等语法不自动映射。"})
                continue
            val = dc.get("parsed") or {}
            if (val.get("kind") != "scalar" or val.get("si_value") is None
                    or str(val.get("unit_suffix") or "").lower() not in {"", "v", "volt", "volts"}
                    or not math.isfinite(float(val.get("si_value") or 0))):
                diagnostics.append({"code": "non_scalar_value", "message": f"{ref} 的 DC 值不是标量。"})
                continue
            master, parameter, unit = "ads_sources:V_DC", "Vdc", "V"
            ads_value = f"{float(val['si_value']):.12g} {unit}"
            source_map[ref] = "SPICE pin 1=正端、pin 2=负端；ADS 符号的极性需在当前 ADS 版本核对后落图。"
            diagnostics.append({"code": "source_pin_polarity_unverified", "message": f"{ref} 的 ADS 电压源符号引脚极性尚未由当前版本 pins() 实测；保留建议映射，必须人工核验后才能落图。"})
        else:
            diagnostics.append({"code": "unsupported_ads_mapping", "message": f"{ref} ({kind}) 没有经本工作区示例核验的 ADS 元件映射。"})
            continue
        base = _SAFE_NAME.sub("_", ref)
        name = base or "X"
        if not name[0].isalpha() and name[0] != "_":
            name = "X_" + name
        candidate, suffix = name, 2
        while candidate.casefold() in names:
            candidate = f"{name}_{suffix}"
            suffix += 1
        names.add(candidate.casefold())
        component_names[str(comp.get("id"))] = candidate
        if kind in _MAP:
            source_map[ref] = (f"{master} 的两端拓扑和参数 {parameter} 有工作区 ADS 构建示例"
                               f"（tests/ce_amp_case.py、tests/cb_amp_case.py）作映射依据；"
                               "本次导入设计尚未在 ADS 仿真复核。")
        instances.append({"master": master, "name": candidate, "params": {parameter: ads_value}})
    # Build deterministic star connections for each multi-terminal net.
    connections: list[dict[str, list[Any]]] = []
    gnd_needed = any(n.get("id") == "0" and n.get("members") for n in ir.get("nets") or [])
    if gnd_needed:
        instances.append({"master": "ads_rflib:GROUND", "name": "SPICE_GND"})
    for net in ir.get("nets") or []:
        members = net.get("members") or []
        endpoints: list[list[Any]] = []
        for member in members:
            ref = str(member.get("component") or "")
            ads_name = component_names.get(ref)
            if ads_name is None:
                continue
            endpoints.append([ads_name, member.get("pin")])
        if net.get("id") == "0" and gnd_needed:
            endpoints.append(["SPICE_GND", 1])
        for endpoint in endpoints[1:]:
            connections.append({"a": endpoints[0], "b": endpoint})
    return {"ready": not diagnostics, "meaning": "mapping-ready only; no controller, Term/TermG, RF reference plane, or simulation is inferred",
            "instances": instances, "connections": connections,
            "source_crosswalk": source_map, "diagnostics": diagnostics}


__all__ = ["compile_ads_build_plan"]
