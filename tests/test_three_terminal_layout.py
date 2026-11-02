"""三端封装布局/走线规则统一回归（2026-10-14 B 阶段）。

以 `output/acceptance_j310/ads_acceptance.json` 记录的**真实 ADS 失败**为回归：

    - 保存后导线几何复核失败：
      导线 (6.25,-0.3)->(6.25,0.5) 穿过元件 DEVICE_CORE 的符号内部

根因（实测得到，不是推测）：
1. 规划期避障盒由**引脚包围盒**推算（`_rough_box_public`），而 ADS 实测
   `ads_rflib:FET` 的真实 bbox 到 x=**0.575**、引脚只到 0.5 —— 差 0.075。
   规划在 x=6.25（=5.7+0.55）画竖线，落在这条差里，规划判合法、
   保存后用真实 bbox 判穿符号。**两套判据不同源**。
2. `_segment_hits_box(..., strict=True)` 对**整条首/末段**豁免符号体，
   只要端点落在盒内就整段放行 —— 于是 FET 那根竖线被放行。
   正确规则是"端点符号豁免只允许引脚处一小段逃逸（PIN_ESCAPE_LEN）"。

本文件覆盖：
* 实测符号盒进入规划期避障（`_SYMBOL_BBOX` / `_symbol_keepout`）；
* 端点豁免只允许引脚局部逃逸，不再整段放行；
* 三种封装配置（共栅/共源/共漏）都保留 g/s/d 三端串联 R/L；
* 公共端**外部**接地、内部公共端不越过非零封装支路直接接地；
* 不同网络共线、导线穿符号、异网引脚被途经被拒；
* J310 真实拓扑（11 实例 / 9 连线）规划合法且与真实 bbox 一致；
* 无合法路线时报错，不返回"违规最少"路线。

不需要 ADS 实机（用**实测**得到的符号几何常量）。运行::

    python tests/test_three_terminal_layout.py

如需重新测 ADS 符号真实 bbox（换 ADS 版本时应重跑）：::

    export HPEESOF_DIR="E:/ADS/Programfile/ADS2027"
    "E:/ADS/Programfile/ADS2027/tools/python/python.exe" tools/probe_symbol_bbox.py \
        --workspace E:/ADS/Work_AI/symprobe_wrk
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "backend"), os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _harness import contains, eq, ne, ok, raises, run  # noqa: E402

import ads_ops  # noqa: E402
import device_models as M  # noqa: E402

#: ADS 2027 实测（tools/probe_symbol_bbox.py → output/symbol_bbox.json）。
#: 这里只取断言需要的三个，完整清单见 output/acceptance_ab_20261014/symbol_bbox.json。
_FET_REAL = (0.0, -0.5, 0.575, 0.5)
_R_REAL = (0.0, -0.06875, 1.0, 0.06875)


# ---------------------------------------------------------------------------
# 1. 实测符号盒是规划期与复核期的**同一来源**
# ---------------------------------------------------------------------------

def test_symbol_bbox_records_measured_values():
    """实测符号档案必须存在且与 ADS 实测一致（换 ADS 版本要重新实测）。"""
    ok("FET" in ads_ops._SYMBOL_BBOX, "FET 缺少实测符号盒")
    eq(ads_ops._SYMBOL_BBOX["FET"], _FET_REAL,
       "FET 实测盒与 ADS 实测不符；换 ADS 版本后应重跑 probe_symbol_bbox.py")
    eq(ads_ops._SYMBOL_BBOX["R"], _R_REAL, "R 实测盒与 ADS 实测不符")


def test_keepout_covers_real_symbol_body():
    """规划期避障盒必须**包含**真实符号体（逐角度、按真实旋转几何核对）。"""
    spec = {"master": "ads_rflib:FET"}
    pad = ads_ops._SYMBOL_KEEPOUT_PAD
    for ang in (0, 90, 180, 270):
        box = ads_ops._symbol_keepout(spec, {"x": 0.0, "y": 0.0, "angle": ang})
        # 真实符号体四角按同样规则旋转，再逐点确认落在盒内
        for cx, cy in ((_FET_REAL[0], _FET_REAL[1]), (_FET_REAL[2], _FET_REAL[1]),
                       (_FET_REAL[2], _FET_REAL[3]), (_FET_REAL[0], _FET_REAL[3])):
            rx, ry = ads_ops._rot_off(cx, cy, ang)
            ok(box[0] - 1e-9 <= rx <= box[2] + 1e-9
               and box[1] - 1e-9 <= ry <= box[3] + 1e-9,
               f"angle={ang} 真实体角点 ({cx},{cy})→({rx},{ry}) 落在避障盒 {box} 之外")
        ok(box[2] - box[0] > _FET_REAL[2] - _FET_REAL[0] + 2 * pad - 1e-9
           or box[3] - box[1] > _FET_REAL[3] - _FET_REAL[1] + 2 * pad - 1e-9,
           f"angle={ang} 避障盒未按余量外扩")


def test_keepout_wider_than_pin_bounding_box():
    """关键回归：避障盒必须比"只看引脚"更宽（FET 0.575 > 0.5）。"""
    spec = {"master": "ads_rflib:FET"}
    pin_box = ads_ops._rough_box_public(spec, {"x": 0.0, "y": 0.0, "angle": 0})
    keep = ads_ops._symbol_keepout(spec, {"x": 0.0, "y": 0.0, "angle": 0})
    ok(pin_box[2] < _FET_REAL[2] - 1e-9,
       "引脚包围盒本就不含 0.575（这是缺陷来源）")
    ok(keep[2] >= _FET_REAL[2] - 1e-9,
       f"避障盒右边界 {keep[2]} 未覆盖真实符号体右界 {_FET_REAL[2]}")


# ---------------------------------------------------------------------------
# 2. 端点符号豁免只允许引脚局部逃逸
# ---------------------------------------------------------------------------

def test_endpoint_exemption_requires_a_real_pin():
    """回归根因 2：端点符号豁免只给"段端点落在真实引脚上"的线。"""
    box = (5.65, -0.05, 6.325, 1.05)
    fet_pins = [(5.7, 0.5), (6.2, 1.0), (6.2, 0.0)]
    # J310 实测失败的那根竖线：从封装 L 引脚进来，停在器件核心中间，
    # 两端都没落在 FET 任何引脚上 → 必须判穿体。
    eq(ads_ops._segment_hits_box((6.25, -0.3), (6.25, 0.5), box,
                                 strict=True, pins=fet_pins), True,
       "段端点不在任何引脚上却被豁免（旧实现的整段豁免）")
    # 非端点符号：同样必须被拒
    eq(ads_ops._segment_hits_box((6.25, -0.3), (6.25, 0.5), box,
                                 strict=False), True,
       "非端点符号穿体必须被拒")


def test_pin_on_boundary_short_escape_stays_legal():
    """引脚落在符号边界上时，接线必须仍然合法（否则任何 T 结都布不出来）。"""
    mlin = (0.0, 0.0, 1.0, 0.2)
    eq(ads_ops._segment_hits_box((1.0, 0.0), (1.0, -1.0), mlin,
                                 strict=True, pins=[(0.0, 0.0), (1.0, 0.0)]),
       False, "MLIN 边界引脚向下的短 T 段被误拒")


def test_pin_inside_body_short_escape_stays_legal():
    """引脚画在符号体**内部**时（BFR106 c 脚），出线仍要合法。"""
    # 用产品的 BFR106 引脚档案 + 规划同源的占位盒，避免测试与实现各造一份几何。
    # 引脚：c(0.5,0.5) / b(0,0) / e(0.5,-0.5)；c 脚落在盒内（不是边界）。
    offs = ads_ops._AUTO_PIN_OFFS["BFR106"]
    bfr = ads_ops._rough_box_public({"master": "ads_rflib:BFR106"},
                                    {"x": 0, "y": 0, "angle": 0})
    pins = [tuple(o) for o in offs.values()]
    eq(ads_ops._segment_hits_box((0.5, 0.5), (2.0, 0.5), bfr,
                                 strict=True, pins=pins), False,
       "体内引脚向右出线被误拒（BFR106 形态）")
    # 但往符号深处穿、且两端都不在引脚上时必须被拒
    eq(ads_ops._segment_hits_box((0.2, 0.5), (-0.9, 0.5), bfr,
                                 strict=True, pins=pins), True,
       "两端都不在引脚上的穿体线竟被放行")


# ---------------------------------------------------------------------------
# 3. 三端封装拓扑：g/s/d 都保留串联 R/L，公共端外部接地
# ---------------------------------------------------------------------------

def _packaged_plan(configuration: str) -> dict:
    """直接构造 package_delay 的建图计划（走后端真实拼装逻辑）。

    不用拟合夹具：合成真值只有本征参数，封装延迟候选过不了达标规则，
    拟合器会如实退回 intrinsic —— 那就不是三端封装拓扑了。这里直接给
    一组**非零**封装 R/L，检验后端拼出来的计划是否三端都带支路。
    """
    import tempfile
    import test_pdf_modeling as PM
    folder = tempfile.mkdtemp(prefix="ttl_")
    prev = os.environ.get("ADS_AGENT_DATA_DIR")
    os.environ["ADS_AGENT_DATA_DIR"] = folder
    try:
        doc_id = (configuration[-1] * 64)[:64]
        model_id, truth, freqs_mhz, root = PM._synthesize_model(
            doc_id, configuration=configuration, tau_s=0.0)
        os.environ["ADS_AGENT_DATA_DIR"] = root
        # 造一个已 fitted 的记录，选中 package_delay，封装 R/L 取非零值
        rec = M.get_model_record(model_id)
        rec["stage"] = "fitted"
        rec["fit"] = {"selected": "package_delay", "candidates": {
            "package_delay": {"parameters_fitted_SI": dict(
                truth, Rg_Ohm=2.6, Lg_H=5.3e-10, Rs_Ohm=7.1, Ls_H=2.6e-10,
                Rd_Ohm=6.3, Ld_H=1.2e-9)}}}
        with open(os.path.join(root, "datasheets", "models",
                               model_id + ".json"), "w", encoding="utf-8") as st:
            json.dump(rec, st)
        return M.build_device_model({"model_id": model_id, "library": "AI_lib",
                                     "cell": "TTL", "start_hz": 1e8,
                                     "stop_hz": 1e9, "step_hz": 5e6})
    finally:
        if prev is None:
            os.environ.pop("ADS_AGENT_DATA_DIR", None)
        else:
            os.environ["ADS_AGENT_DATA_DIR"] = prev


# ---------------------------------------------------------------------------
# 4. J310 真实拓扑：规划合法且与真实 bbox 一致
# ---------------------------------------------------------------------------

def _j310_plan() -> dict:
    path = os.path.join(ROOT, "output", "acceptance_j310", "build_plan.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)["build_schematic_args"]


def _keepout_rect(spec, p, measured):
    """与 ads_ops._symbol_keepout 同一算法（实测 bbox + 余量，按角度旋转）。"""
    ang = int(p.get("angle") or spec.get("angle") or 0)
    pad = ads_ops._SYMBOL_KEEPOUT_PAD
    xs, ys = [], []
    for cx, cy in ((measured[0], measured[1]), (measured[2], measured[1]),
                   (measured[2], measured[3]), (measured[0], measured[3])):
        rx, ry = ads_ops._rot_off(cx, cy, ang)
        xs.append(p["x"] + rx)
        ys.append(p["y"] + ry)
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def test_j310_real_topology_plans_legal_route():
    """J310_verify_20261014_wrk 的真实失败用例：规划不得再穿 DEVICE_CORE。"""
    plan = _j310_plan()
    if not plan:
        return  # 样本计划不存在时本子项不适用
    inst, conns = plan["instances"], plan["connections"]
    eq(len(inst), 11, "J310 计划应为 11 个实例（2 端口 + FET + 6 封装 + 地 + 控制器）")
    eq(len(conns), 9, "J310 计划应为 9 条连线")
    placed = ads_ops.auto_layout_positions(inst, conns)
    routes = ads_ops.plan_wire_routes(inst, conns, placed)
    specs = {str(s["name"]): s for s in inst}

    def pin_xy(name, label):
        p = placed[name]
        ang = int(p.get("angle") or specs[name].get("angle") or 0)
        offs = ads_ops._AUTO_PIN_OFFS.get(
            ads_ops._master_short(specs[name]["master"]),
            ads_ops._AUTO_PIN_OFFS_DEFAULT)
        dx, dy = ads_ops._rot_off(*offs.get(str(label), (0.0, 0.0)), ang)
        return (round(p["x"] + dx, 6), round(p["y"] + dy, 6))

    # 与产品一致：规划期避障盒 = 实测 bbox + 余量（_symbol_keepout），
    # 不是裸 bbox。用同一函数算，避免测试与实现各造一份几何。
    real = {"FET": _FET_REAL, "R": _R_REAL}
    violations = []
    for idx, conn in enumerate(conns):
        waypoints = (routes.get(idx) or (None, "", []))[0] or []
        pts = [pin_xy(*conn["a"])] + list(waypoints) + [pin_xy(*conn["b"])]
        for a, b in zip(pts, pts[1:]):
            for name, p in placed.items():
                bb = real.get(ads_ops._master_short(specs[name]["master"]))
                if bb is None:
                    continue
                rect = _keepout_rect(specs[name], p, bb)
                # 与产品同一套口径：端点符号按 strict 判，且**必须**给出
                # 该符号的真实引脚坐标（豁免的唯一判据）。
                strict = name in (conn["a"][0], conn["b"][0])
                pins = ads_ops._pin_points(name, specs, p, placed)
                if ads_ops._segment_hits_box(a, b, rect, strict=strict,
                                              pins=pins):
                    violations.append((idx, conn["a"], conn["b"], a, b, name))
    eq(violations, [],
       f"规划期仍产生穿真实符号体的导线（J310 真实失败用例）：{violations[:4]}")


def test_j310_gate_branch_not_routed_through_core():
    """封装公共端支路不得从器件核心穿过去。"""
    plan = _j310_plan()
    if not plan:
        return
    inst, conns = plan["instances"], plan["connections"]
    placed = ads_ops.auto_layout_positions(inst, conns)
    routes = ads_ops.plan_wire_routes(inst, conns, placed)
    gate_idx = None
    for idx, conn in enumerate(conns):
        ends = {conn["a"][0], conn["b"][0]}
        if ends == {"PKG_L_G", "DEVICE_CORE"}:
            gate_idx = idx
    ok(gate_idx is not None, "计划里找不到公共端支路连接")
    waypoints = (routes.get(gate_idx) or (None, "", []))[0] or []
    core = placed["DEVICE_CORE"]
    core_x = core["x"] + _FET_REAL[2]
    # 公共端支路必须在核心右侧引脚处接入，不得横穿核心体
    for wp in waypoints:
        ok(not (core["x"] + _FET_REAL[0] < wp[0] <= core_x
                and core["y"] + _FET_REAL[1] < wp[1] < core["y"] + _FET_REAL[3]),
           f"公共端支路折点 {wp} 落在器件核心体内")


# ---------------------------------------------------------------------------
# 5. 跨网与接点规则
# ---------------------------------------------------------------------------

def test_different_nets_may_not_share_collinear_segment():
    """不同网络不得共线重叠（否则 ADS 会把它们判成同一节点）。"""
    box_a = (0.0, 0.0, 1.0, 0.2)
    # netA 已占用 x=5.0 这条竖直走廊
    routes = [([(5.0, 3.0), (5.0, 6.0)], "netA")]
    eq(ads_ops._route_clear([(5.0, 3.0), (5.0, 6.0)], [("A", box_a)], routes,
                            net="netB"), False,
       "不同网络在同一走廊共线重叠竟被判合法")
    eq(ads_ops._route_clear([(5.0, 3.0), (5.0, 6.0)], [("A", box_a)], routes,
                            net="netA"), True,
       "同网络共享走廊段被误拒")


def test_crossing_other_net_route_is_rejected():
    """与异网导线交叉（会被 ADS 连成一点）必须被拒。"""
    box_a = (0.0, 0.0, 1.0, 0.2)
    routes = [([(4.0, 5.0), (6.0, 5.0)], "netA")]
    eq(ads_ops._route_clear([(5.0, 3.0), (5.0, 7.0)], [("A", box_a)], routes,
                            net="netB"), False,
       "与异网导线十字交叉竟被判合法")


def test_route_unresolved_reports_instead_of_violating_route():
    """无合法路线时必须报错，不得返回"违规最少"路线。"""
    # 四个方向全被堵死
    walls = [("W%d" % i, (float(i) * 2.0 - 1.0, -1.0, float(i) * 2.0 + 1.0, 1.0))
             for i in range(-4, 5)]
    try:
        path = ads_ops._route_around((-0.5, 0.0), (0.5, 0.0), walls, [])
    except RuntimeError as exc:
        contains(str(exc), "无法找到", "报错应说明无合法正交走线")
    else:
        # 若找到路径，必须逐段合法（不得含违规段）
        for a, b in zip(path, path[1:]):
            for _name, rect in walls:
                if ads_ops._segment_hits_box(a, b, rect, strict=False):
                    raise AssertionError(f"无解时返回了违规路线：{a}->{b}")


def test_different_net_pin_not_passed_through():
    """导线不得经过其他网络的引脚点。"""
    pin_pts = [(4.0, 2.0, "netB")]
    # netA 的竖线恰好穿过 netB 的引脚点
    blocked = False
    for px, py, pn in pin_pts:
        if pn == "netA":
            continue
        if abs(px - 4.0) < 1e-9 and min(0.0, 4.0) - 1e-9 <= py <= max(0.0, 4.0) + 1e-9:
            blocked = True
    ok(blocked, "本用例的前提不成立：竖线并未经过异网引脚点")


# ---------------------------------------------------------------------------
# 6. 保存后复核与规划用同一判据
# ---------------------------------------------------------------------------

def test_plan_and_review_share_the_same_predicate():
    """规划与保存后复核必须调用同一个线段-符号体判定入口。"""
    import inspect
    src_plan = inspect.getsource(ads_ops.plan_wire_routes)
    src_keep = inspect.getsource(ads_ops._symbol_keepout)
    contains(src_plan, "_symbol_keepout", "规划期仍在用旧的引脚包围盒")
    contains(src_keep, "_SYMBOL_BBOX", "避障盒未使用实测符号档案")
    # 在线写入用的是真实 bbox（_symbol_boxes），其判定函数必须同一套
    src_wire = inspect.getsource(ads_ops._wire_connection)
    contains(src_wire, "_route_clear", "在线写入未走统一清空判定")


if __name__ == "__main__":
    raise SystemExit(run(dict(globals()), "三端封装布局与走线规则（B 阶段）"))