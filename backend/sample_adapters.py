"""SHA 锁定的样本适配器 —— 人工复核过的 SMMBFJ310L 曲线映射（回归数据）。

审查文档明确允许：通用模块不得写死本机临时路径、具体 SHA 或 PDF drawing
索引；**SHA 锁定的样本映射可以作为独立适配器与回归数据**。本文件就是那个
独立适配器：

* 只有当上传 PDF 的 SHA-256 与已复核样本完全一致时才可用；
* 校准框（PDF 点坐标）与 drawing 索引来自 2026-10-13 的人工图形复核
  （tools/extract_j310_curves.py 的同源数据）；
* 适配器只产出通用引擎能消费的 axes/series 结构，不含任何器件专属数学；
* 换任何其它 PDF（包括同系列其它手册）都会被 SHA 门禁拒绝 —— 那些必须走
  通用校准流程，不能套用本映射。

page 4 图 4-7：公共栅 Y（图4 幅 / 图6 相）与 S（图5 幅 / 图7 相），
VDS=10 V、ID=10 mA、TA=25°C，频率 100-1000 MHz（对数轴）。
"""

from __future__ import annotations

PRESETS = {
    # SHA-256 of the reviewed ON Semi SMMBFJ309L/J309L/J310L/SMMBFJ310L datasheet
    "smmbfj310l_v1": {
        "sha256": "113d3cb19b47f74879741cf408983f03ab89b0123f1e1beecc3bd8a2442871b8",
        "page": 4,
        "conditions": {"vds_v": 10.0, "id_a": 0.01, "temperature_c": 25.0},
        "configuration": "common_gate",
        "port_order": {"port1": "source", "port2": "drain", "common": "gate"},
        "z0": {"ohm": 50.0, "status": "assumed"},
        "sampling": {"mode": "log10_spaced", "count": 91, "start": 100.0, "stop": 1000.0},
        # 人工复核的图框（PDF 点：x0, y0=顶, x1, y1=底）与轴端值
        "boxes": {
            "Ymag": (100.460, 69.165, 241.285, 213.619),
            "Smag": (361.757, 68.824, 502.469, 213.278),
            "Yphase": (100.573, 302.570, 241.285, 445.946),
            "Sphase": (361.701, 302.570, 502.696, 446.853),
        },
        # drawing 索引 -> (框, 底值, 顶值)
        "curves": {
            "Y11mag": (72, "Ymag", 0, 30), "Y21mag": (74, "Ymag", 0, 30),
            "Y22mag": (76, "Ymag", 0, 30), "Y12mag": (78, "Ymag", 0, 3),
            "S11mag": (83, "Smag", 0.15, 0.45), "S12mag": (84, "Smag", 0, 0.06),
            "S21mag": (85, "Smag", 0.55, 0.85), "S22mag": (86, "Smag", 0.90, 1),
            "Y11phase": (115, "Yphase", 0, 50), "Y12phase": (116, "Yphase", -200, 0),
            "Y22phase": (117, "Yphase", 82, 87), "Y21phase": (118, "Yphase", 130, 180),
            "S11phase": (126, "Sphase", -120, -20), "S21phase": (127, "Sphase", -100, 0),
            "S22phase": (128, "Sphase", -100, 0), "S12phase": (129, "Sphase", 20, 120),
        },
        "figures": {
            ("Y", "mag"): "Figures 4 (|Y|, common gate)",
            ("S", "mag"): "Figures 5 (|S|, common gate)",
            ("Y", "phase"): "Figures 6 (angle Y, common gate)",
            ("S", "phase"): "Figures 7 (angle S, common gate)",
        },
        "mag_units": {"Y": "mS", "S": "1"},
    },
}


def preset_for_sha(sha256: str) -> str:
    """按文件 SHA 找已复核适配器；找不到返回空串（绝不猜测型号）。

    确定性选择入口：同一份已复核手册不必再让聊天手填校准证据，也不能让
    同系列其它手册借型号相似套用本映射。
    """
    digest = str(sha256 or "").strip().lower()
    if not digest:
        return ""
    for name, entry in PRESETS.items():
        if entry["sha256"] == digest:
            return name
    return ""


def build_specs(preset: str, document: dict) -> tuple[list, dict]:
    """把复核映射展开成通用 save_curve 的 spec 列表。

    每条曲线**单独一条记录**：复核样本给每条曲线各自的 y 量程（同一图框里
    Y11 用 0–30 mS、Y12 用 0–3 mS 等），共用一条记录会让不同量程互相污染。
    x 轴（频率）四条图共用同一对数校准。

    SHA 不匹配直接拒绝 —— 这是适配器的硬门禁，不提供任何绕过。
    """
    entry = PRESETS.get(preset)
    if entry is None:
        raise ValueError(f"未知样本适配器 {preset}；可用：{sorted(PRESETS)}")
    if str(document.get("sha256") or "") != entry["sha256"]:
        raise ValueError("样本适配器与当前 PDF 的 SHA-256 不匹配；"
                         "通用 PDF 必须走显式校准流程，不能套用样本映射")
    specs = []
    for label, (index, box_name, low, high) in entry["curves"].items():
        matrix_kind = label[0]
        component = "mag" if label.endswith("mag") else "phase"
        x0, y0, x1, y1 = entry["boxes"][box_name]
        specs.append({
            "page": entry["page"],
            "figure": entry["figures"][(matrix_kind, component)],
            "matrix_kind": matrix_kind,
            "component": component,
            "phase_unit": "deg" if component == "phase" else None,
            "configuration": entry["configuration"],
            "port_order": entry["port_order"],
            "z0": entry["z0"],
            "conditions": entry["conditions"],
            "sampling": entry["sampling"],
            "axes": {
                "x": {"unit": "MHz", "scale": "log10",
                      "cal_points": [{"at": x0, "value": 100.0},
                                     {"at": x1, "value": 1000.0}]},
                "y": {"unit": entry["mag_units"][matrix_kind], "scale": "linear",
                      "cal_points": [{"at": y0, "value": high},
                                     {"at": y1, "value": low}]},
            },
            "series": [{"label": label, "path_index": index}],
        })
    # 顺序按 label 排一次，便于复现与对照
    specs.sort(key=lambda s: s["series"][0]["label"])
    origin = {"method": "sample_adapter", "preset": preset,
              "sha256": entry["sha256"],
              "note": "人工复核过的样本映射（SHA 锁定），非通用自动识别；"
                      "每条曲线各自量程"}
    return specs, origin
