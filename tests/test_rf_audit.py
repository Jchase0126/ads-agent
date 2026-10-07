"""射频物理审查纯计算层测试（不需要 ADS，直接构造数据驱动 rf_audit）。

覆盖：
* 单位解析：mm/mil/裸数值/表达式（表达式必须返回「无法静态求值」，不能编数）
* 微带闭式模型：50Ω 参考点、单调性、电长度 λ/4 往返
* MTEE 引脚角色：未旋转/旋转 90° 后都按几何判定，不按屏幕方位
* MTEE 宽度审查：W1≠W2 报错、与相接线宽不一致报错、表达式参数标「未验证」
* 传输线审查：正常线算出 Z0/电长度、基板引用断裂报错、CPWG 走「LineCalc 待办」、
  阻抗偏离中位数点名
* 原理图直角连接：90° 相接无弯折元件报警，有 MBEND 不报
* Layout 审查：无视图/空视图如实「未完成 Layout 验证」；有图形时查直角弯折、
  同层重叠、间隙、单层无地、实例同步漂移，EM 恒标「未验证」

运行::

    python tests/test_rf_audit.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

import rf_audit  # noqa: E402


# ---------------- 单位解析 ----------------


def test_parse_len():
    eq(rf_audit.parse_len_mm("1.7 mm"), (1.7, ""), "毫米")
    ok(abs(rf_audit.parse_len_mm("17.6 mil")[0] - 17.6 * 0.0254) < 1e-9, "mil")
    got, note = rf_audit.parse_len_mm("3")
    eq(got, 3.0, "裸数值")
    ok(note, "裸数值必须带解释口径")
    got, note = rf_audit.parse_len_mm("(W)")
    eq(got, None, "表达式不出数值")
    contains(note, "表达式", "表达式说明")
    got, note = rf_audit.parse_len_mm("abc")
    eq(got, None, "乱串不出数值")


def test_parse_freq():
    eq(rf_audit.parse_freq_hz("2.3 GHz"), (2.3e9, ""), "GHz")
    eq(rf_audit.parse_freq_hz("5900000000"), (5.9e9, "裸数值按 Hz 解释"), "裸 Hz")


def test_parse_len_resolves_vars():
    """VAR 引用解析成设计真实值 —— 这是读设计数据，不是臆造。"""
    vars = {"W50": "0.46 mm", "Lq": "6.8 mm", "W70": "0.2 mm"}
    got, note = rf_audit.parse_len_mm("W50", vars)
    eq(got, 0.46, "W=VAR 名 → VAR 值")
    eq(note, "", "解析干净无注")
    got, _ = rf_audit.parse_len_mm("Lq", vars)
    eq(got, 6.8, "链式一次引用")
    got, note = rf_audit.parse_len_mm("W_missing", vars)
    eq(got, None, "不在 VAR 表的引用如实失败")
    contains(note, "VAR", "失败说明指向 VAR 表")
    # 引用链成环要有界
    cyc = {"A": "B", "B": "A"}
    got, note = rf_audit.parse_len_mm("A", cyc)
    eq(got, None, "成环不无限递归")


# ---------------- 微带闭式模型 ----------------


def test_microstrip_z0_reference():
    # FR4 经典参考：Er=4.4、H=1.6mm、W≈3.06mm ≈ 50Ω（文献公认值附近）
    z0, eeff = rf_audit.microstrip_z0(3.06, 1.6, 4.4)
    ok(47 <= z0 <= 53, f"FR4 50Ω 参考点，实际 {z0:.2f}")
    ok(3.0 <= eeff <= 3.7, f"eps_eff 合理区间，实际 {eeff:.3f}")
    # 单调性：线越宽阻抗越低；Er 越大阻抗越低
    z_wide, _ = rf_audit.microstrip_z0(4.0, 1.6, 4.4)
    z_thin, _ = rf_audit.microstrip_z0(2.0, 1.6, 4.4)
    ok(z_thin > z0 > z_wide, "线宽单调性")
    z_hialu, _ = rf_audit.microstrip_z0(0.6, 0.635, 9.6)
    ok(z_hialu > z0, "高 Er 基板同宽阻抗更低")


def test_elec_len_roundtrip():
    f = 2.4e9
    z0, eeff = rf_audit.microstrip_z0(3.06, 1.6, 4.4)
    lambda0_mm = 299792458.0 / f * 1000.0
    quarter = lambda0_mm / math.sqrt(eeff) / 4
    theta = rf_audit.microstrip_elec_len_deg(quarter, f, eeff)
    ok(abs(theta - 90) < 1e-6, f"λ/4 往返应得 90°，实际 {theta}")


# ---------------- MTEE 引脚角色 ----------------


def _mtee_pins(angle=0, ox=0.0, oy=0.0):
    """MTEE 引脚模板：1 在原点、2 在 +x、3 在 (0.5,-0.5)，按 ADS 逆时针旋转。"""
    base = {"1": (0.0, 0.0), "2": (1.0, 0.0), "3": (0.5, -0.5)}

    def rot(p):
        x, y = p
        a = angle % 360
        if a == 90:
            x, y = -y, x
        elif a == 180:
            x, y = -x, -y
        elif a == 270:
            x, y = y, -x
        return (x + ox, y + oy)

    return [{"label": k, "xy": rot(v)} for k, v in base.items()]


def test_mtee_roles_unrotated_and_rotated():
    roles = rf_audit.mtee_pin_roles(_mtee_pins(0))
    eq(roles["through"], ["1", "2"], "未旋转：贯穿对 1/2")
    eq(roles["branch"], "3", "分支脚 3")
    roles90 = rf_audit.mtee_pin_roles(_mtee_pins(90))
    eq(roles90["through"], ["1", "2"], "旋转 90° 后贯穿对仍是 1/2（按几何而非屏幕方位）")
    eq(roles90["branch"], "3", "旋转后分支脚仍是 3")
    # 平移到任意位置不应改变判定
    rolesoff = rf_audit.mtee_pin_roles(_mtee_pins(270, 100, -40))
    eq(rolesoff["through"], ["1", "2"], "任意位置判定不变")
    eq(rf_audit.mtee_pin_roles([{"label": "1", "xy": (0, 0)}]), None, "引脚不足返回 None")


# ---------------- MTEE 宽度审查 ----------------


def _mtee(w1, w2, w3, pins=None):
    return {"name": "MTEE1", "master": "MTEE",
            "params": {"W1": w1, "W2": w2, "W3": w3},
            "pins": pins or [
                {"label": "1", "net": "N1", "xy": (0.0, 0.0)},
                {"label": "2", "net": "N2", "xy": (1.0, 0.0)},
                {"label": "3", "net": "N3", "xy": (0.5, -0.5)},
            ],
            "roles": {"through": ["1", "2"], "branch": "3",
                      "through_len": 1.0, "branch_offset": 0.0}}


def test_mtee_width_mismatch_through():
    f = []
    m = _mtee("0.6 mm", "0.8 mm", "0.7 mm")   # 三个宽度全不同：无格局解释
    m["roles"] = rf_audit.mtee_pin_roles(m["pins"])
    rf_audit.check_mtee_widths(m, {}, f, "t")
    errs = [x for x in f if x["severity"] == "error"]
    eq(len(errs), 1, "W1≠W2 且无格局解释报一条错")
    contains(errs[0]["problem"], "W1=W2", "报错点名连续主线规则")
    eq(errs[0]["verification"], "已验证（原理图参数数据）", "验证状态")


def test_mtee_split_pattern_downgraded_to_warning():
    """Wilkinson 类分裂结（分支与一端同宽）：宽度差是疑似设计意图，降为
    warning 仍要求确认 —— 不静默接受，也不误报硬错误。"""
    f = []
    m = _mtee("0.6 mm", "0.8 mm", "0.8 mm")
    m["roles"] = rf_audit.mtee_pin_roles(m["pins"])
    rf_audit.check_mtee_widths(m, {}, f, "t")
    steps = [x for x in f if x["id"].endswith("-step-MTEE1")]
    eq(len(steps), 1, "宽度差仍要点名")
    eq(steps[0]["severity"], "warning", "分裂格局降为 warning")
    contains(steps[0]["problem"], "分裂", "点名分裂/合并格局")


def test_mtee_width_join_mismatch():
    f = []
    m = _mtee("0.6 mm", "0.6 mm", "0.6 mm")
    m["roles"] = rf_audit.mtee_pin_roles(m["pins"])
    net_widths = {"N1": [{"instance": "ML1", "master": "MLIN",
                          "W_mm": 0.5, "W_raw": "0.5 mm", "note": ""}]}
    rf_audit.check_mtee_widths(m, net_widths, f, "t")
    errs = [x for x in f if x["severity"] == "error"]
    ok(any("ML1" in e["element"] for e in errs), "与相接线宽不一致被点名")
    eq(len(errs), 1, "只报相接不一致这一条")


def test_mtee_width_expression_pending():
    f = []
    m = _mtee('"W1var"', "0.6 mm", "0.6 mm")
    m["roles"] = rf_audit.mtee_pin_roles(m["pins"])
    rf_audit.check_mtee_widths(m, {}, f, "t")
    ok(any(x["verification"].startswith("未验证") for x in f),
       "表达式参数标「未验证」，不出臆造数值")


# ---------------- 传输线审查 ----------------


def _inv(mocr=..., extra=None, reference_ohm=None):
    base = {
        "design": "AI_lib:T:schematic",
        "freq": {"center_hz": 2.4e9},
        "reference_ohm": reference_ohm,
        "substrates": {"MSUB1": {"H": "0.635 mm", "Er": "9.6", "T": "0.02 mm"}},
        "instances": [
            {"name": "MSUB1", "master": "MSUB", "params": {"H": "0.635 mm", "Er": "9.6"},
             "pins": [], "origin": None},
            {"name": "ML1", "master": "MLIN",
             "params": {"W": "0.6 mm", "L": "16.6 mm", "Subst": "MSUB1"},
             "pins": [{"label": "1", "net": "N1", "xy": (0, 0)},
                      {"label": "2", "net": "N2", "xy": (2, 0)}],
             "origin": (0, 0)},
        ],
    }
    if extra:
        base["instances"] += extra
    return base


def test_tline_computed():
    f = []
    out = rf_audit.audit_schematic(_inv())
    t = out["tlines"][0]
    eq(t["Z0_闭式"] is not None, True, "算出 Z0")
    # 氧化铝标准组合：Er=9.6、H=0.635mm、W≈0.6mm 就是 50Ω 微带
    ok(45 <= t["Z0_闭式"] <= 55, f"0.6/0.635/9.6 应约 50Ω，实际 {t['Z0_闭式']}")
    ok(t["电长度deg"] > 0, "电长度已算")
    not_contains(str(t), "LineCalc 已完成", "绝不声称完成 LineCalc")
    ok(not [x for x in out["findings"] if x["severity"] == "error"], "正常线无 error")


def test_tline_reference_impedance_deviation():
    """W50=3.06mm 配 10mil 基板这类真实疑点：闭式约 8.6Ω，对端口 50Ω 偏离必须点名。"""
    inv = _inv(reference_ohm=50.0)
    inv["substrates"]["MSub1"] = {"H": "10.0 mil", "Er": "9.6"}
    inv["instances"].append(
        {"name": "ML9", "master": "MLIN",
         "params": {"W": "3.06 mm", "L": "6.96 mm", "Subst": "MSub1"},
         "pins": [], "origin": None})
    out = rf_audit.audit_schematic(inv)
    dev = [x for x in out["findings"] if x["id"] == "rf-z0dev-ML9"]
    eq(len(dev), 1, "偏离端口参考阻抗被点名")
    contains(dev[0]["problem"], "端口参考阻抗", "比对基准是端口参考阻抗")
    ok(not [x for x in out["findings"]
            if x["id"].startswith("rf-z0dev-") and x["element"] == "ML1"],
       "接近参考阻抗的线不点名")


def test_tline_model_error_is_isolated():
    """单条线的非法参数只产生一条 finding，不炸掉整个审查。"""
    inv = _inv()
    inv["instances"].append(
        {"name": "MLX", "master": "MLIN",
         "params": {"W": "-3 mm", "L": "5 mm", "Subst": "MSUB1"},
         "pins": [], "origin": None})
    out = rf_audit.audit_schematic(inv)
    ok(any(x["id"] == "rf-calc-MLX" for x in out["findings"]), "坏参数单独报")
    ok(len(out["tlines"]) == 2, "其余线照常计算")


def test_tline_z0_deviation_flagged():
    inv = _inv()
    inv["instances"].append(
        {"name": "ML2", "master": "MLIN",
         "params": {"W": "0.1 mm", "L": "5 mm", "Subst": "MSUB1"},
         "pins": [], "origin": None})
    inv["instances"].append(
        {"name": "ML3", "master": "MLIN",
         "params": {"W": "0.6 mm", "L": "5 mm", "Subst": "MSUB1"},
         "pins": [], "origin": None})
    out = rf_audit.audit_schematic(inv)
    dev = [x for x in out["findings"] if x["id"].startswith("rf-z0dev")]
    eq(len(dev), 1, "偏离主线中位数的细线被点名")
    ok(dev and "ML2" == dev[0]["element"], "点名的是 0.1mm 细线")


def test_tline_broken_substrate():
    inv = _inv()
    inv["instances"].append(
        {"name": "ML2", "master": "MLIN",
         "params": {"W": "0.6 mm", "L": "5 mm", "Subst": "MSUB9"},
         "pins": [], "origin": None})
    out = rf_audit.audit_schematic(inv)
    errs = [x for x in out["findings"] if x["severity"] == "error"]
    ok(any("MSUB9" in str(x["actual"]) for x in errs), "基板引用断裂点名")


def test_tline_cpwg_needs_linecalc():
    inv = _inv()
    inv["instances"].append(
        {"name": "CP1", "master": "CPWG",
         "params": {"W": "0.6 mm", "L": "5 mm", "Subst": "MSUB1"},
         "pins": [], "origin": None})
    out = rf_audit.audit_schematic(inv)
    ok(any(x["element"] == "CP1" and x["verification"].startswith("未验证")
           for x in out["findings"]), "CPWG 如实标未验证并给 LineCalc 指引")


# ---------------- 原理图直角连接 ----------------


def test_schematic_right_angle_join():
    inv = _inv(extra=[
        {"name": "ML2", "master": "MLIN",
         "params": {"W": "0.6 mm", "L": "5 mm", "Subst": "MSUB1"},
         "pins": [{"label": "1", "net": "N2", "xy": (2, 0)},
                  {"label": "2", "net": "N3", "xy": (2, 2)}],
         "origin": (2, 2)},
    ])
    # ML1 pin2 与 ML2 pin1 同网 N2，两线轴向 90° 相交，无弯折元件
    out = rf_audit.audit_schematic(inv)
    ok(any("corner" in x["id"] for x in out["findings"]), "90° 相接无弯折元件报警")
    inv["instances"].append({"name": "MB1", "master": "MBEND",
                             "params": {}, "pins": [{"label": "1", "net": "N2", "xy": (2, 0)}],
                             "origin": (2, 0)})
    out2 = rf_audit.audit_schematic(inv)
    ok(not [x for x in out2["findings"] if "corner" in x["id"]],
       "有 MBEND 就不报直角")


# ---------------- Layout 审查 ----------------


def test_layout_unavailable_is_honest():
    res = rf_audit.analyze_layout({"available": False, "reason": "没有 layout 视图"})
    eq(res["verdict"], "未完成 Layout 验证", "无视图 → 如实未完成")
    ok(any(x["verification"] == "未完成 Layout 验证" for x in res["findings"]), "finding 带状态")
    res2 = rf_audit.analyze_layout({"available": True, "empty": True, "shapes": [],
                                    "instances": [], "terms": []})
    eq(res2["verdict"], "未完成 Layout 验证", "空 Layout 同样如实")


def test_layout_right_angle_and_overlap():
    shapes = [
        {"kind": "path", "layer": "cond", "width": 2.0,
         "bbox": [0, 49, 51, 101],
         "outline": [[0, 49], [0, 51], [50, 51], [50, 101], [52, 101], [52, 49]]},
        {"kind": "rect", "layer": "cond", "bbox": [0, 0, 100, 10], "outline": None},
        {"kind": "rect", "layer": "cond", "bbox": [90, 5, 150, 20], "outline": None},
        {"kind": "rect", "layer": "cond2", "bbox": [-10, -10, 60, 40], "outline": None},
    ]
    res = rf_audit.analyze_layout({"available": True, "empty": False, "shapes": shapes,
                                   "instances": [{"name": "ML1", "master": "MLIN"}],
                                   "terms": ["P1"], "schematic_names": ["ML1", "ML9"]})
    ok(any("rightangle" in x["id"] for x in res["findings"]), "未切角直角弯折报警")
    ok(any("overlap" in x["id"] for x in res["findings"]), "同层重叠报警")
    ok(not any("ground" in x["id"] for x in res["findings"]), "有 cond2 不报缺地")
    ok(any("sync-ML9" in x["id"] for x in res["findings"]), "实例同步漂移点名")
    eq(res["em"]["verified"], False, "EM 恒标未验证")
    contains(res["verdict"], "已审查", "有图形时判定为已审查（几何级）")


def test_layout_single_layer_no_ground():
    shapes = [{"kind": "rect", "layer": "cond", "bbox": [0, 0, 10, 10], "outline": None}]
    res = rf_audit.analyze_layout({"available": True, "empty": False, "shapes": shapes,
                                   "instances": [], "terms": [], "schematic_names": []})
    ok(any("ground" in x["id"] for x in res["findings"]), "单层导体报接地缺失")


def test_layout_gap_reported_not_judged():
    shapes = [
        {"kind": "rect", "layer": "cond", "bbox": [0, 0, 10, 10], "outline": None},
        {"kind": "rect", "layer": "cond", "bbox": [12, 0, 22, 10], "outline": None},
        {"kind": "rect", "layer": "cond2", "bbox": [0, 0, 30, 30], "outline": None},
    ]
    res = rf_audit.analyze_layout({"available": True, "empty": False, "shapes": shapes,
                                   "instances": [], "terms": [], "schematic_names": []})
    gaps = [x for x in res["findings"] if x["id"].startswith("lay-gap")]
    eq(len(gaps), 1, "报出最近间隙对")
    eq(gaps[0]["actual"]["间隙"], 2.0, "间隙数值正确")
    contains(gaps[0]["verification"], "未验证", "无工艺规则不判罚，只报数值")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
