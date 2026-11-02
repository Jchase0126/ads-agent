"""Conservative parser for a small, explicit SPICE schematic subset.

Supported device cards are two-terminal R/L/C and independent V/I sources.
``.subckt``/``.ends`` are recorded as hierarchy declarations; subcircuit
instances (X cards), controlled sources, semiconductor cards, behavioral
expressions, and simulator-specific extensions are deliberately not expanded.
Unsupported or malformed statements produce line diagnostics and are never
interpreted as connectivity.

Public API::

    parse_spice(text: str, *, source_name: str = "<memory>") -> dict
    parse_file(path: str) -> dict

The return value is JSON-serializable and includes ``circuit_ir``,
``diagnostics``, and ``summary``. This module has no ADS dependency.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any


_MAX_TEXT_CHARS = 8_000_000
_MAX_PHYSICAL_LINES = 100_000
_MAX_LOGICAL_CARDS = 50_000
_MAX_CARD_CHARS = 16_384
_MAX_COMPONENTS = 50_000
_MAX_SUBCKT_DEPTH = 64

_NUMERIC = re.compile(
    r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?"
    r"(?:meg|[tgkmunpf])?(?:ohm|ohms|henry|henries|farad|farads|v|volt|volts|a|amp|amps|s|sec|secs)?$",
    re.IGNORECASE,
)
_SUFFIX = re.compile(r"^([+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?)(meg|[tgkmunpf])?(.*)$", re.IGNORECASE)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$:/-]*$")
_NODE_NAME = re.compile(r"^(?:0|[0-9]+|[A-Za-z_][A-Za-z0-9_.$:/-]*)$")
_PARAM = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$")

# SPICE scale factors. SPICE's historical ``m`` is milli; ``meg`` is 10^6.
_SCALE = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
          "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}


def _logical_lines(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join ``+`` continuation cards while retaining physical source lines."""
    logical: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        # SPICE dialects commonly use $ for an inline comment. Do not strip it
        # inside a braced expression/string; this subset rejects such syntax.
        body = raw.split("$", 1)[0].rstrip()
        if not body.strip():
            continue
        if body.lstrip().startswith("+"):
            if current is None:
                diagnostics.append(_diag("error", "orphan_continuation",
                                         "续行没有前置语句，未解析。", [line_no], raw))
                continue
            continuation = body.lstrip()[1:].strip()
            if continuation:
                current["text"] += " " + continuation
            current["source_lines"].append(line_no)
            current["source_text"] += "\n" + raw
            continue
        if current is not None:
            logical.append(current)
        current = {"text": body.strip(), "source_lines": [line_no], "source_text": raw}
    if current is not None:
        logical.append(current)
    return logical, diagnostics


def _diag(severity: str, code: str, message: str, lines: list[int], text: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message,
            "source_lines": lines, "source_text": text}


def _parse_value(token: str) -> dict[str, Any]:
    """Return exact source text and, only for plain scalar literals, SI value."""
    value = token.strip()
    # Uppercase bare H/F are common explicit SPICE unit spellings. Handle them
    # before the case-insensitive suffix regex, where ``F`` would otherwise be
    # consumed as the femto scale factor (lowercase ``f`` remains femto).
    if len(value) > 1 and value[-1] in {"H", "F"}:
        explicit = _SUFFIX.fullmatch(value[:-1])
        if explicit and not explicit.group(3):
            number, scale, _ = explicit.groups()
            try:
                si = float(number) * _SCALE.get((scale or "").lower(), 1.0)
            except (ValueError, OverflowError):
                si = None
            return {"raw": value, "kind": "scalar", "si_value": si,
                    "scale_suffix": scale or "", "unit_suffix": value[-1]}
    if not _NUMERIC.fullmatch(value):
        return {"raw": value, "kind": "expression_or_unsupported", "si_value": None,
                "unit_suffix": None}
    m = _SUFFIX.fullmatch(value)
    if not m:
        return {"raw": value, "kind": "expression_or_unsupported", "si_value": None,
                "unit_suffix": None}
    number, scale, unit = m.groups()
    try:
        si = float(number) * _SCALE.get((scale or "").lower(), 1.0)
    except (ValueError, OverflowError):
        si = None
    return {"raw": value, "kind": "scalar", "si_value": si,
            "scale_suffix": scale or "", "unit_suffix": unit or ""}


def _node_id(scope: tuple[str, ...], name: str) -> str:
    # Ground is global in SPICE; all other names are local to the declaration.
    if name == "0":
        return "0"
    return "/".join((*scope, name)) if scope else name


def _parse_spice_unbounded(text: str, *, source_name: str = "<memory>") -> dict[str, Any]:
    """Parse supported SPICE cards into a conservative, JSON-safe Circuit IR.

    Device parameter values are retained verbatim. A scalar SI conversion is
    supplied for a plain literal; expressions and unsupported forms remain
    explicit and are not evaluated.
    """
    if not isinstance(text, str):
        raise TypeError("text must be str")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    logical, diagnostics = _logical_lines(text)
    components: list[dict[str, Any]] = []
    subcircuits: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    scope: list[str] = []
    seen_subckts: set[tuple[str, ...]] = set()
    seen_instances: set[tuple[tuple[str, ...], str]] = set()
    ended = False
    first_card = True

    for card in logical:
        raw = card["text"]
        tokens = raw.split()
        if not tokens:
            continue
        head = tokens[0]
        lower = head.lower()
        lines = card["source_lines"]

        # Conventional SPICE files start with a human-readable title line.
        # Skip only an unmistakable non-card first line; never consume a valid
        # R/L/C/V/I card just because the title is optional in some dialects.
        if first_card:
            first_card = False
            if (not head.startswith(".") and head[0].upper() not in "RLCVI"
                    and len(tokens) < 4):
                diagnostics.append(_diag("info", "title_line",
                                         "首行按 SPICE 标题行保留为说明，未作为电路语句解析。",
                                         lines, card["source_text"]))
                continue

        if lower == ".subckt":
            if len(tokens) < 2 or not _NAME.fullmatch(tokens[1]):
                diagnostics.append(_diag("error", "invalid_subckt", ".subckt 缺少有效名称。", lines, card["source_text"]))
                continue
            name = tokens[1]
            path = tuple(scope + [name])
            if path in seen_subckts:
                diagnostics.append(_diag("error", "duplicate_subckt", f"层次声明重复：{'/'.join(path)}。", lines, card["source_text"]))
            seen_subckts.add(path)
            if scope:
                diagnostics.append(_diag("warning", "nested_subckt", "检测到嵌套 .subckt 声明；已记录层次，但未展开实例。", lines, card["source_text"]))
            ports: list[str] = []
            params: list[dict[str, str]] = []
            in_params = False
            for token in tokens[2:]:
                if token.lower() == "params:":
                    in_params = True
                    continue
                if in_params:
                    pm = _PARAM.fullmatch(token)
                    if pm:
                        params.append({"name": pm.group(1), "raw_value": pm.group(2)})
                    else:
                        diagnostics.append(_diag("warning", "unsupported_subckt_parameter", f"无法解析子电路参数 {token!r}，按原文保留。", lines, card["source_text"]))
                        params.append({"name": "", "raw_value": token})
                else:
                    ports.append(token)
                    port_scope = tuple(scope + [name])
                    nodes.setdefault(_node_id(port_scope, token), {"id": _node_id(port_scope, token), "name": token, "scope": list(port_scope), "source_lines": []})
            entry = {"name": name, "scope": list(scope), "path": list(path), "ports": ports,
                     "parameters": params, "source_lines": lines, "source_text": card["source_text"]}
            subcircuits.append(entry)
            if len(scope) >= _MAX_SUBCKT_DEPTH:
                diagnostics.append(_diag("error", "subckt_depth_limit", f"子电路嵌套超过上限 {_MAX_SUBCKT_DEPTH}。", lines, card["source_text"]))
                continue
            scope.append(name)
            continue

        if lower == ".ends":
            if not scope:
                diagnostics.append(_diag("error", "unmatched_ends", ".ends 没有对应的 .subckt。", lines, card["source_text"]))
            else:
                expected = scope[-1]
                supplied = tokens[1] if len(tokens) > 1 else expected
                if supplied.lower() != expected.lower():
                    diagnostics.append(_diag("error", "ends_name_mismatch", f".ends 名称 {supplied!r} 与当前层次 {expected!r} 不一致。", lines, card["source_text"]))
                scope.pop()
            continue

        if lower == ".end":
            ended = True
            continue

        if head.startswith("."):
            diagnostics.append(_diag("warning", "unsupported_directive", f"未解析 SPICE 指令 {head}；不会据此推断电气连接。", lines, card["source_text"]))
            continue

        prefix = head[0].upper()
        kinds = {"R": ("resistor", 2), "L": ("inductor", 2), "C": ("capacitor", 2),
                 "V": ("voltage_source", 2), "I": ("current_source", 2)}
        if prefix not in kinds:
            diagnostics.append(_diag("warning", "unsupported_device", f"不支持器件卡 {head}（器件前缀 {prefix!r}）；未解析其节点。", lines, card["source_text"]))
            continue
        kind, pin_count = kinds[prefix]
        if len(tokens) < 3 + 1:
            diagnostics.append(_diag("error", "incomplete_device", f"{head} 缺少节点或参数。", lines, card["source_text"]))
            continue
        if not _NAME.fullmatch(head):
            diagnostics.append(_diag("error", "invalid_instance_name", f"器件名 {head!r} 不符合支持的名称格式。", lines, card["source_text"]))
            continue
        pin_names = ["1", "2"]
        pin_nodes = tokens[1:1 + pin_count]
        value_tokens = tokens[1 + pin_count:]
        if len(pin_nodes) != pin_count or not value_tokens:
            diagnostics.append(_diag("error", "incomplete_device", f"{head} 缺少完整节点或参数。", lines, card["source_text"]))
            continue
        if any(not _NODE_NAME.fullmatch(n) for n in pin_nodes):
            diagnostics.append(_diag("error", "invalid_node", f"{head} 含不支持的节点名；未创建该器件。", lines, card["source_text"]))
            continue
        instance_key = (tuple(scope), head.casefold())
        if instance_key in seen_instances:
            diagnostics.append(_diag("error", "duplicate_instance",
                                     f"当前层次内器件名重复（SPICE 名称不区分大小写）：{head}。",
                                     lines, card["source_text"]))
            continue
        seen_instances.add(instance_key)

        # R/L/C accepts one positional value and optional KEY=VALUE fields.
        # Independent sources accept only the explicit DC/AC forms below.
        if prefix in "RLC":
            positional = [t for t in value_tokens if "=" not in t]
            extras = [t for t in value_tokens if "=" in t]
            if len(positional) != 1 or any(not _PARAM.fullmatch(t) for t in extras):
                diagnostics.append(_diag("warning", "unsupported_device_syntax", f"{head} 不是支持的单值 R/L/C 语法；保留诊断但不猜参数。", lines, card["source_text"]))
                continue
            params = [{"name": "value", "raw_value": positional[0], "parsed": _parse_value(positional[0])}]
            if params[0]["parsed"]["kind"] != "scalar":
                diagnostics.append(_diag("warning", "nonliteral_parameter", f"{head} 参数不是简单数值字面量；已保留原文，未求值。", lines, card["source_text"]))
            for token in extras:
                pm = _PARAM.fullmatch(token)
                assert pm
                params.append({"name": pm.group(1), "raw_value": pm.group(2), "parsed": _parse_value(pm.group(2))})
        else:
            params, err = _parse_independent_source(value_tokens)
            if err:
                diagnostics.append(_diag("warning", "unsupported_source_syntax", f"{head}: {err} 未解析该源卡。", lines, card["source_text"]))
                continue

        conn = []
        for pin, node_name in zip(pin_names, pin_nodes):
            nid = _node_id(tuple(scope), node_name)
            node = nodes.setdefault(nid, {"id": nid, "name": node_name, "scope": list(scope), "source_lines": []})
            node["source_lines"].extend(lines)
            conn.append({"pin": pin, "node": nid, "source_node": node_name})
        components.append({"id": "/".join((*scope, head)) if scope else head,
                           "reference": head, "kind": kind, "prefix": prefix,
                           "scope": list(scope), "pins": conn, "parameters": params,
                           "source_lines": lines, "source_text": card["source_text"]})
        if len(components) >= _MAX_COMPONENTS:
            diagnostics.append(_diag("error", "component_limit", f"器件数达到解析上限 {_MAX_COMPONENTS}，后续语句停止解析。", lines, card["source_text"]))
            break

    if scope:
        diagnostics.append(_diag("error", "unterminated_subckt", f"以下 .subckt 缺少 .ends：{', '.join(scope)}。", [], ""))
    if not ended:
        diagnostics.append(_diag("warning", "missing_end", "未发现 .end；输入可能被截断，不能作为完整电路建图或仿真。", [], ""))
    if subcircuits:
        diagnostics.append(_diag("warning", "subckt_not_flattened",
                                 "检测到 .subckt 定义；当前解析器不会展开 X 实例或将层次定义伪装成顶层电路。",
                                 [], ""))

    for node in nodes.values():
        node["source_lines"] = sorted(set(node["source_lines"]))
    ir = {
        "schema": "ads-agent-circuit-ir",
        "schema_version": "1.0",
        "source": {"name": source_name, "format": "spice-subset", "sha256": digest},
        "components": components,
        "nodes": list(nodes.values()),
        "nets": [{"id": n["id"], "name": n["name"], "scope": n["scope"],
                  "members": [{"component": c["id"], "pin": p["pin"]}
                              for c in components for p in c["pins"] if p["node"] == n["id"]]}
                 for n in nodes.values()],
        "hierarchy": subcircuits,
    }
    counts = {kind: sum(1 for c in components if c["kind"] == kind)
              for kind in ("resistor", "inductor", "capacitor", "voltage_source", "current_source")}
    return {"circuit_ir": ir, "diagnostics": diagnostics,
            "summary": {"source_sha256": digest, "component_count": len(components),
                        "counts": counts, "node_count": len(nodes),
                        "subcircuit_count": len(subcircuits),
                        "error_count": sum(d["severity"] == "error" for d in diagnostics),
                        "warning_count": sum(d["severity"] == "warning" for d in diagnostics)}}


def _parse_independent_source(tokens: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    """Parse a deliberately small independent source syntax without evaluation."""
    if not tokens:
        return [], "缺少源参数"
    mode = "dc"
    idx = 0
    if tokens[0].lower() in {"dc", "ac"}:
        mode = tokens[0].lower()
        idx = 1
    if idx >= len(tokens):
        return [], "缺少源值"
    primary = tokens[idx]
    if primary.startswith("{") or "(" in primary or ")" in primary:
        return [], "行为表达式不在支持范围内"
    parsed = _parse_value(primary)
    if parsed["kind"] != "scalar":
        return [], f"源值 {primary!r} 不是支持的数值字面量"
    result = [{"name": mode, "raw_value": primary, "parsed": parsed}]
    idx += 1
    if mode == "ac":
        # SPICE AC magnitude and optional phase; phase is kept as raw degrees.
        if idx < len(tokens) and "=" not in tokens[idx]:
            phase = tokens[idx]
            try:
                float(phase)
            except ValueError:
                return [], "AC 相位不是数值"
            result.append({"name": "phase_deg", "raw_value": phase})
            idx += 1
    elif idx < len(tokens) and tokens[idx].lower() == "ac":
        if idx + 1 >= len(tokens):
            return [], "AC 子句缺少幅度"
        amp = _parse_value(tokens[idx + 1])
        if amp["kind"] != "scalar":
            return [], "AC 幅度不是支持的数值字面量"
        result.append({"name": "ac_magnitude", "raw_value": tokens[idx + 1], "parsed": amp})
        if idx + 2 < len(tokens):
            try:
                float(tokens[idx + 2])
            except ValueError:
                return [], "AC 相位不是数值"
            result.append({"name": "ac_phase_deg", "raw_value": tokens[idx + 2]})
            idx += 1
        idx += 2
    # Allow plain KEY=VALUE source attributes; reject waveform syntax.
    for token in tokens[idx:]:
        pm = _PARAM.fullmatch(token)
        if not pm:
            return [], f"额外语法 {token!r} 未明确支持（如 PULSE/SIN/PWL）"
        result.append({"name": pm.group(1), "raw_value": pm.group(2), "parsed": _parse_value(pm.group(2))})
    return result, None


def _failure(filename: str, code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "circuit_ir": None,
            "diagnostics": [_diag("error", code, message, [], "")],
            "summary": {"filename": filename, "component_count": 0, "node_count": 0,
                        "subcircuit_count": 0, "error_count": 1, "warning_count": 0}}


def parse_spice_text(text: str, filename: str = "input.cir") -> dict[str, Any]:
    """Stable service entry point; returns ``ok`` and bounded diagnostics.

    Limits are checked before parsing to keep uploaded input bounded. ``ok``
    means the supported subset was parsed without errors; warnings may remain.
    Unsupported cards can therefore produce ``ok=True`` only when they are
    non-structural warnings. Any unsupported device card makes the result
    incomplete and is represented by a warning; callers must inspect the
    diagnostics before treating the IR as a complete circuit.
    """
    if not isinstance(text, str):
        return _failure(str(filename), "invalid_input_type", "输入必须是文本。")
    if len(text) > _MAX_TEXT_CHARS:
        return _failure(str(filename), "text_limit", f"输入超过 {_MAX_TEXT_CHARS} 个字符上限。")
    physical_lines = text.splitlines()
    if len(physical_lines) > _MAX_PHYSICAL_LINES:
        return _failure(str(filename), "line_limit", f"输入超过 {_MAX_PHYSICAL_LINES} 行上限。")
    if any(len(line) > _MAX_CARD_CHARS for line in physical_lines):
        return _failure(str(filename), "card_length_limit", f"单行超过 {_MAX_CARD_CHARS} 字符上限。")
    logical, pre_diagnostics = _logical_lines(text)
    if len(logical) > _MAX_LOGICAL_CARDS:
        return _failure(str(filename), "card_count_limit", f"有效语句超过 {_MAX_LOGICAL_CARDS} 条上限。")
    try:
        result = _parse_spice_unbounded(text, source_name=str(filename))
    except (ValueError, OverflowError, RecursionError) as exc:
        return _failure(str(filename), "parse_failure", f"解析异常已安全拦截：{type(exc).__name__}: {exc}")
    result["diagnostics"] = pre_diagnostics + result["diagnostics"]
    result["summary"]["filename"] = str(filename)
    result["summary"]["error_count"] = sum(d["severity"] == "error" for d in result["diagnostics"])
    result["summary"]["warning_count"] = sum(d["severity"] == "warning" for d in result["diagnostics"])
    incomplete_codes = {"unsupported_device", "unsupported_device_syntax",
                        "unsupported_source_syntax", "unsupported_directive",
                        "nonliteral_parameter", "nested_subckt", "missing_end",
                        "subckt_not_flattened"}
    result["ok"] = not any(
        d["severity"] == "error" or d["code"] in incomplete_codes
        for d in result["diagnostics"]
    )
    return result


def parse_spice(text: str, *, source_name: str = "<memory>") -> dict[str, Any]:
    """Compatibility alias for :func:`parse_spice_text`."""
    return parse_spice_text(text, filename=source_name)


def parse_file(path: str) -> dict[str, Any]:
    """Read UTF-8/UTF-8-BOM SPICE text and parse it, retaining the input path."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        text = f.read()
    return parse_spice_text(text, filename=os.path.abspath(path))


__all__ = ["parse_spice_text", "parse_spice", "parse_file"]
