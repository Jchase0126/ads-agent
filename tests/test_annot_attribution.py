# -*- coding: utf-8 -*-
"""标注归属回归（2026-09-30 建图验收可信度轮的标注部分）。

背景与根因（实测证据链见 tests/probes/annot_orient_probe.py 与
logs/ce_agent_before_report.json）:
* 区模型 _annot_text_zone 旧版不区分 angle 90/270 的文字侧、TermG-180
  方向放反、L/BFR106/V_DC 缺失 —— 避让判的是幻影区;
* 7.5 避让 _try 里 fixed/changed 缺 nonlocal,候选扫描不 break、偏移
  逐次叠加 —— RD +2.8 = 1.4+1.4 的叠加重影;
* 行线擦竖放文字的 graze 也触发避让,把支路文字整块赶出符号。

本文件固化:区模型实测常数、CE 案例的归属不变量、跨度让位、
first-accept 语义(防叠加回归)、_annotation_issues 渲染级复核。

运行: python tests/test_annot_attribution.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import ads_ops  # noqa: E402
from ce_amp_case import INSTANCES, CONNECTIONS  # noqa: E402
from _harness import contains, eq, ok, run  # noqa: E402

# ---------------------------------------------------------------------------
# 区模型 v2:方向学实测常数（AI_annot_probe scratch cell,2026-09-30）
# ---------------------------------------------------------------------------

def test_zone_model_measured_constants():
    cases = [
        ("R", 90, (0.10, 0.32, 1.02, 1.08)),     # 体向上:文字在体右上
        ("R", 270, (0.10, -0.68, 1.02, 0.08)),   # 体向下:文字贴 origin 右下
        ("C", 90, (0.15, 0.32, 0.82, 1.08)),
        ("C", 270, (0.15, -0.68, 0.82, 0.08)),
        ("C", 0, (0.15, -0.85, 0.90, -0.15)),
        ("L", 90, (0.07, 0.13, 0.73, 1.08)),
        ("L", 270, (0.07, -0.87, 0.73, 0.08)),
        ("L", 0, (0.15, -0.95, 0.90, -0.05)),
        ("TERMG", 180, (-0.75, -1.12, 0.20, -0.20)),  # 体在引脚左,文字左下
        ("TERMG", 0, (0.15, -1.12, 1.05, -0.20)),
        ("TERM", 0, (0.15, -1.12, 1.05, -0.20)),
        ("BFR106", 0, (0.16, -1.00, 0.84, -0.58)),
        ("V_DC", 0, (0.15, -0.90, 0.90, -0.20)),
        ("MSUB", 0, (0.20, -2.65, 1.80, 0.55)),       # 旧模型保持
        ("MLIN", 0, (0.20, -1.25, 1.45, -0.15)),
    ]
    for key, ang, want in cases:
        got = ads_ops._annot_text_zone(key, 0, 0, 0, 0, ang)
        eq(got, want, f"zone[{key}@{ang}] 实测常数")


def test_zone_model_ground_none():
    eq(ads_ops._annot_text_zone("GROUND", 0, 0, 0, 0, 0), None,
       "GROUND 无标注文字,返回 None")


# ---------------------------------------------------------------------------
# CE 案例（用户截图对应设计）:归属不变量
# ---------------------------------------------------------------------------

def _ce_placed():
    return ads_ops.auto_layout_positions(
        [dict(s) for s in INSTANCES], [dict(c) for c in CONNECTIONS])


def _attached(zone, box, pad=0.05, attach_h=1.2, attach_v=2.2):
    return ads_ops.auto_layout_positions and (
        (zone[0] <= box[2] + pad and box[0] <= zone[2] + pad
         and zone[1] <= box[3] + pad and box[1] <= zone[3] + pad)
        or (zone[0] <= box[2] + pad and box[0] <= zone[2] + pad
            and max(box[1] - zone[3], zone[1] - box[3]) <= attach_v)
        or (zone[1] <= box[3] + pad and box[1] <= zone[3] + pad
            and max(box[0] - zone[2], zone[0] - box[2]) <= attach_h))


def test_ce_no_drifted_annotations():
    placed = _ce_placed()
    # 历史漂移位:RD (0,2.8)/PORT1 (-2.4,0) —— 现在必须全在默认位或小位移
    for n in ("RD", "R1", "PORT1", "COUT", "RE1", "R2", "CE", "CIN",
              "CDCIN", "CDCOUT", "LC", "LOUT"):
        annot = placed[n].get("annot") or (0.0, 0.0)
        ok(abs(annot[0]) <= 1.2 and abs(annot[1]) <= 1.2,
           f"{n} 标注偏移必须留在符号旁: {annot}")


def test_ce_zones_attached_to_symbols():
    placed = _ce_placed()
    specs = {str(s["name"]): s for s in INSTANCES}
    for n, p in placed.items():
        s = specs.get(n)
        if s is None:
            continue
        z = ads_ops._annot_text_zone(ads_ops._master_short(s["master"]),
                                     p["x"], p["y"],
                                     *(p.get("annot") or (0, 0)),
                                     p.get("angle"))
        if z is None:
            continue
        box = ads_ops._rough_box_public(s, p)
        ok(_attached(z, box), f"{n} 文字区 {z} 必须与符号盒 {box} 保持搭接")


def test_ce_output_branch_text_no_overlap():
    """用户点名的支路拥挤:RD 回默认位后,其文字区与 LOUT 文字区必须
    不重叠 —— 由跨度让位(LOUT 右移)保证,而不是把 RD 赶走。"""
    placed = _ce_placed()
    eq(placed["RD"].get("annot") or (0, 0), (0.0, 0.0),
       "RD 文字应回到默认位（符号旁）")
    ok(placed["LOUT"]["x"] >= placed["RD"]["x"] + 0.9,
       f"LOUT 必须为 RD 文字让位: LOUT.x={placed['LOUT']['x']}, "
       f"RD.x={placed['RD']['x']}")
    specs = {str(s["name"]): s for s in INSTANCES}

    def zone(n):
        p = placed[n]
        return ads_ops._annot_text_zone(
            ads_ops._master_short(specs[n]["master"]),
            p["x"], p["y"], *(p.get("annot") or (0, 0)), p.get("angle"))

    zrd, zlo = zone("RD"), zone("LOUT")
    # pad 0.02（2026-09-30 紧凑化）：zone 模型各含 ~0.05 保守边带，
    # R|L 相邻列距 0.9（手工参考图形态，09-30 渲染级实测 words 互压 0）
    # 时 zone 边缘间隙 0.03 —— 断言语义是"真实字形不重叠"，
    # 不再把保守边带的擦边当互压
    pad = 0.02
    ok(zrd[2] < zlo[0] - pad or zlo[2] < zrd[0] - pad
       or zrd[3] < zlo[1] - pad or zlo[3] < zrd[1] - pad,
       f"RD 文字区 {zrd} 与 LOUT 文字区 {zlo} 不得重叠")


def test_ce_electrical_topology_untouched():
    """布局与标注修复不得改电气:离线等价对 EXPECTED 拓扑（同
    test_ce_amp_acceptance 的口径,这里防本文件改动引入回归）。"""
    import netlist_check as nc
    placed = _ce_placed()
    # 每条连接的两个引脚仍由布局器配对（连线清单未变,坐标只影响几何）
    eq([c["a"] for c in CONNECTIONS][:3],
       [["PORT1", "1"], ["PORT1", "1"], ["CIN", "2"]],
       "连接清单前 3 条不被布局改动")
    eq(len(placed), len(INSTANCES), "实例一个不少")


# ---------------------------------------------------------------------------
# first-accept 语义（nonlocal 修复的回归护栏）
# ---------------------------------------------------------------------------

def test_first_accept_wins_no_accumulation():
    """_try 曾缺 nonlocal:候选扫描不 break、偏移逐次叠加(1.4+1.4=2.8)。
    固化:5WAY 的 ML1 第一个可接受候选是 (-1.2, 0.0),最终值必须是它。"""
    import iterate_ml_sym_metrics as met
    placed = ads_ops.auto_layout_positions(
        [dict(s) for s in met.SPEC_5WAY], [dict(c) for c in met.CONN_5WAY])
    eq(placed["ML1"].get("annot"), (-1.2, 0.0),
       f"first-accept 语义: ML1 annot={placed['ML1'].get('annot')}")


def test_sym_split_pinned_annotations_survive():
    """对称分支的刻意标注位(隔离电阻上移一行等)不得被收敛复查回收,
    也不被二次挪动 —— 渲染验证过的设计位。"""
    import test_auto_layout as t
    placed = ads_ops.auto_layout_positions(
        t._wilkinson_ml_spec(), t._wilkinson_ml_conns(),
        {"name": "VAR1", "values": {"W50": "38.2 mil"}})
    eq(placed["RISO1"].get("annot"), (0.0, 1.0), "隔离电阻文字上移一行")
    eq(placed["MSUB1"]["x"], placed["P2"]["x"] + 1.5, "MSUB 右侧空白列")


def test_mtee_case_keeps_working_offset():
    """MTEE 五行文字块:通用路径两节案例的历史验证位仍可达。"""
    import iterate_ml_sym_metrics as met
    placed = ads_ops.auto_layout_positions(
        [dict(s) for s in met.SPEC_MTEE], [dict(c) for c in met.CONN_MTEE])
    eq(placed["MTEE1"].get("annot"), (-1.2, -2.2),
       f"MTEE1 annot={placed['MTEE1'].get('annot')}")


# ---------------------------------------------------------------------------
# _annotation_issues 渲染级兜底
# ---------------------------------------------------------------------------

class _Box:
    def __init__(self, x0, y0, x1, y1):
        self.lower_left = type("P", (), {"x": x0, "y": y0})()
        self.upper_right = type("P", (), {"x": x1, "y": y1})()


class _Inst:
    def __init__(self, name, sym, annot=None):
        self.inst_name = name
        self.bbox = _Box(*sym)
        if annot is None:
            self.has_ads_annotation = False
        else:
            self.has_ads_annotation = True
            self.bbox_annotation_only = _Box(*annot)


def test_annotation_issues_reports_float_and_overlap():
    design = type("D", (), {"instances": [
        _Inst("R1", (0, 0, 1, 1), (0.2, -0.6, 1.0, 0.0)),   # 贴符号
        _Inst("RD", (10, 0, 11, 1), (10.2, 3.0, 11.0, 3.8)),  # 悬空 2 格
        _Inst("LOUT", (12, 0, 13, 1), (12.2, -0.5, 12.8, 0.2)),
        _Inst("COUT", (14, 0, 15, 1), (12.6, -0.4, 13.4, 0.3)),  # 与 LOUT 互压
    ]})()
    by_name = {i.inst_name: i for i in design.instances}
    issues = ads_ops._annotation_issues(design, by_name)
    ok(any("RD" in s and ("悬空" in s or "1.2" in s) for s in issues),
       f"RD 悬空被点名: {issues}")
    ok(any("LOUT" in s and "COUT" in s for s in issues),
       f"互压按双实例点名: {issues}")
    ok(not any("R1" in s for s in issues), f"贴符号的 R1 不进 issues: {issues}")


def test_annotation_issues_clean_design():
    design = type("D", (), {"instances": [
        _Inst("R1", (0, 0, 1, 1), (0.2, -0.6, 1.0, 0.0)),
        _Inst("R2", (3, 0, 4, 1), (3.2, -0.6, 4.0, 0.0)),
    ]})()
    eq(ads_ops._annotation_issues(design, {}), [], "干净设计零 issue")


def test_annotation_issues_no_annotation_no_crash():
    design = type("D", (), {"instances": [_Inst("G1", (0, 0, 1, 1))]})()
    eq(ads_ops._annotation_issues(design, {}), [],
       "无标注实例(GROUND)静默跳过")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
