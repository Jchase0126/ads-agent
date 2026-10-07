"""射频物理审查的纯计算层 —— 不 import ADS，离线可直接构造数据测试。

两种布局的边界（工程约定，见 docs/Layout 审查设计.md 与系统提示）：

* 原理图布局负责信号流、可读性、引脚方向、接地位置。**原理图导线只是
  绘图坐标，其长度绝不是微带线的物理长度**。
* ADS Layout 才承载实际铜皮的宽度、间隙、长度、拐角、接地回流与制造
  约束。审查发现一律带「验证状态」，查不到的就如实写「未完成验证」，
  绝不用示意图或推测冒充真实 Layout / LineCalc 结果。

本模块全部是纯函数：输入是从 ADS 读出的普通 dict（instances / params /
pins / shapes），输出 findings 列表。每条 finding 的形状：

    {"id", "severity"（error/warning/info）, "element", "location",
     "actual", "problem", "suggestion", "verification"}
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# 单位解析
# ---------------------------------------------------------------------------

_LEN_UNITS_MM = {
    "um": 0.001, "µm": 0.001, "μm": 0.001,
    "mil": 0.0254, "thou": 0.0254,
    "mm": 1.0, "cm": 10.0, "in": 25.4, "inch": 25.4,
    "m": 1000.0,
}
_FREQ_UNITS_HZ = {
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12,
}


def parse_len_mm(value, variables=None):
    """解析长度参数 → (mm, note)。返回 note 提示解释口径；解析失败返回 (None, 原因)。

    ADS 参数形如 "1.7 mm" / "17.6 mil"；裸数值按设计长度单位（通常 mm）
    解释，note 里必须写明该假设。``variables`` 是设计里的 VAR 表
    （名字 → 值）：参数引用 VAR 名（如 W="W50"）时解析成 VAR 的真实值——
    这是读取设计数据，不是臆造。
    """
    depth = 0
    while depth < 5:
        depth += 1
        if value is None:
            return None, "参数缺失"
        s = str(value).strip().strip('"').strip()
        if not s:
            return None, "参数为空"
        if s.startswith("(") or any(ch in s for ch in "+*/"):
            return None, "表达式/引用，无法静态求值"
        low = s.lower()
        # 单位按长度降序匹配，避免 "ghz" 被 "hz" 抢先截断一类的后缀冲突
        for unit in sorted(_LEN_UNITS_MM, key=len, reverse=True):
            if low.endswith(unit):
                num = s[: -len(unit)].strip()
                try:
                    return float(num) * _LEN_UNITS_MM[unit], ""
                except ValueError:
                    return None, f"无法解析数值 {s!r}"
        try:
            return float(s) * 1.0, "裸数值按 mm 解释（需与设计长度单位核对）"
        except ValueError:
            pass
        if variables and s in variables:
            value = variables[s]        # VAR 引用 → 解析 VAR 的值
            continue
        return None, f"无法解析 {s!r}（不是数值/单位串，也不在 VAR 表里）"
    return None, "VAR 引用链过深，放弃解析"


def parse_freq_hz(value):
    """解析频率参数 → (Hz, note)。形如 "2.3 GHz"、"5900000000"。"""
    if value is None:
        return None, "参数缺失"
    s = str(value).strip().strip('"').strip()
    if not s:
        return None, "参数为空"
    if any(ch in s for ch in "+*/"):
        return None, "表达式/引用，无法静态求值"
    low = s.lower()
    for unit in sorted(_FREQ_UNITS_HZ, key=len, reverse=True):
        if low.endswith(unit):
            num = s[: -len(unit)].strip()
            try:
                return float(num) * _FREQ_UNITS_HZ[unit], ""
            except ValueError:
                return None, f"无法解析数值 {s!r}"
    try:
        return float(s), "裸数值按 Hz 解释"
    except ValueError:
        return None, f"无法解析 {s!r}"


def _resolve_var(value, variables):
    """VAR 引用一层链式查找（带防环），返回最终字符串；非引用原样返回。"""
    seen = set()
    while variables and isinstance(value, str):
        s = value.strip().strip('"').strip()
        if s in seen or s not in variables:
            break
        seen.add(s)
        value = variables[s]
    return value


def _param_float(params, name, variables=None):
    """取参数并按长度解析；返回 (mm|None, note)。"""
    raw = params.get(name)
    return parse_len_mm(raw, variables)


# ---------------------------------------------------------------------------
# 微带线闭式计算（Hammerstad 1975 经典式，标称精度 ~2%）
# ---------------------------------------------------------------------------

def microstrip_z0(w_mm: float, h_mm: float, er: float) -> tuple:
    """给定线宽/基板 → (Z0 Ω, eps_eff)。**闭式模型，未过 LineCalc 验证**。

    教科书一阶公式（Hammerstad 1975 主项）：0.1 ≤ w/h ≤ 10 标称精度 ~2%，
    很宽的线（w/h > 10）误差会显著变大 —— 结果一律标注「未过 LineCalc 验证」。
    """
    if w_mm <= 0 or h_mm <= 0 or er <= 0:
        raise ValueError(f"非法参数 w={w_mm} h={h_mm} er={er}")
    u = w_mm / h_mm
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 / u) ** -0.5
    if eeff <= 0:
        raise ValueError(f"有效介电常数非正（u={u:.3f}, er={er}），参数不合理")
    if u <= 1:
        z0 = 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    else:
        z0 = 120 * math.pi / (math.sqrt(eeff) * (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z0, eeff


def microstrip_elec_len_deg(l_mm: float, f_hz: float, eeff: float) -> float:
    """给定物理长度/频率/有效介电常数 → 电长度（度）。"""
    if l_mm <= 0 or f_hz <= 0 or eeff <= 0:
        raise ValueError(f"非法参数 l={l_mm} f={f_hz} eeff={eeff}")
    lambda0_mm = 299792458.0 / f_hz * 1000.0
    lambda_g = lambda0_mm / math.sqrt(eeff)
    return l_mm / lambda_g * 360.0


# ---------------------------------------------------------------------------
# MTEE：按旋转后的实际引脚位置定端口角色（不得按屏幕左右猜）
# ---------------------------------------------------------------------------

def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def mtee_pin_roles(pins: list) -> dict | None:
    """由引脚世界坐标判定 MTEE 的贯穿对与分支脚。

    pins: [{"label": "1", "xy": (x, y)}, ...]（世界坐标，已含实例旋转）。
    规则：两两距离最远的一对是贯穿主线的两个端（1、2 的物理角色由几何
    而不是屏幕方位决定），剩余一个是分支脚（3）。
    """
    pts = [(str(p.get("label")), p["xy"]) for p in pins
           if p.get("label") is not None and p.get("xy")]
    if len(pts) < 3:
        return None
    pairs = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            pairs.append((_dist(pts[i][1], pts[j][1]), i, j))
    pairs.sort(reverse=True)
    d, i, j = pairs[0]
    through = [pts[i][0], pts[j][0]]
    branch = next(pts[k][0] for k in range(len(pts)) if k not in (i, j))
    a, b = pts[i][1], pts[j][1]
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    br = next(p[1] for p in pts if p[0] == branch)
    # 分支脚应落在贯穿对的垂直平分线附近 —— 偏离大说明几何上不是标准 T
    axis = (b[0] - a[0], b[1] - a[1])
    norm = math.hypot(*axis) or 1.0
    off = abs((br[0] - mid[0]) * -axis[1] / norm + (br[1] - mid[1]) * axis[0] / norm)
    return {"through": through, "branch": branch,
            "through_len": round(d, 3), "branch_offset": round(off, 3)}


# ---------------------------------------------------------------------------
# 原理图审查
# ---------------------------------------------------------------------------

_TLINE_MASTERS = {"MLIN", "MLSC", "MLEF", "CPWG", "MCTL", "MCFIL"}
_BEND_MASTERS = {"MBEND", "MSBEND", "MBND", "MBNDG", "MARC", "MCURVE"}
_STEP_MASTERS = {"MSTEP", "STEP", "MTRANS", "MGRAD"}
_MTEE_MASTERS = {"MTEE", "MTEES", "MTEEQ"}
_SUBSTRATE_MASTERS = {"MSUB", "MSUBST", "CPWGSubst"}


def _w_of(inst_entry, key="W", variables=None):
    mm, note = _param_float(inst_entry.get("params") or {}, key, variables)
    return mm, note


def check_mtee_widths(mtee: dict, net_widths: dict, findings: list, fid: str,
                      variables=None):
    """MTEE 逐端宽度审查（要求 4）。

    mtee: {"name", "params", "pins", "roles"}
    net_widths: {net: [{"instance", "master", "W_mm", "W_raw", "note"}]}
    variables: 设计 VAR 表（参数引用 VAR 名时解析成真实设计值）
    """
    roles = mtee.get("roles")
    if not roles:
        findings.append({
            "id": f"{fid}-roles", "severity": "warning",
            "element": f"{mtee['name']} 引脚", "location": "schematic",
            "actual": {"pins": mtee.get("pins")},
            "problem": "引脚坐标不足或缺失，无法按几何判定贯穿对/分支脚",
            "suggestion": "读取该实例引脚 snap_point 后重跑；不要按屏幕方位猜端口",
            "verification": "未验证（缺引脚几何）",
        })
        return
    through = set(roles["through"])
    branch = roles["branch"]
    params = mtee.get("params") or {}
    for p in mtee.get("pins", []):
        label = str(p.get("label"))
        net = p.get("net") or ""
        role = "through" if label in through else ("branch" if label == branch else "?")
        wkey = f"W{label}"
        w_raw = params.get(wkey)
        w_mm, w_note = parse_len_mm(w_raw, variables)
        actual = {"pin": label, "role": role, "net": net, wkey: w_raw}
        # 端上连接的传输线宽度
        lines = net_widths.get(net, [])
        for ln in lines:
            actual[f"连接线 {ln['instance']}"] = ln["W_raw"]
        if w_mm is None:
            findings.append({
                "id": f"{fid}-wexpr-{mtee['name']}-{label}", "severity": "warning",
                "element": f"{mtee['name']} pin{label}（{role}）",
                "location": "schematic", "actual": actual,
                "problem": f"{wkey}={w_raw!r}：{w_note}",
                "suggestion": "把宽度解析成具体数值（或由 VAR 给出）后重跑审查",
                "verification": "未验证（参数不可静态求值）",
            })
            continue
        # 1、2 端是同一条连续主线：默认要求 W1 == W2
        if role == "through" and label == sorted(through)[0] and len(through) == 2:
            other = sorted(through - {label})[0]
            other_mm, other_note = parse_len_mm(params.get(f"W{other}"), variables)
            if other_mm is not None and abs(other_mm - w_mm) > 1e-6:
                branch_mm, _ = parse_len_mm(params.get(f"W{branch}"), variables)
                # 分裂/合并结格局：分支脚宽度与某一贯穿端相同（两条同宽臂），
                # 此时 1/2 宽度差往往就是设计意图（如 Wilkinson 的 50Ω 入、
                # 70Ω 臂）——降为 warning 要求确认，不当成硬错误
                split = branch_mm is not None and (
                    abs(branch_mm - other_mm) < 1e-6 or abs(branch_mm - w_mm) < 1e-6)
                findings.append({
                    "id": f"{fid}-step-{mtee['name']}",
                    "severity": "warning" if split else "error",
                    "element": f"{mtee['name']} pin{label}/pin{other}",
                    "location": "schematic",
                    "actual": {f"W{label}": w_raw, f"W{other}": params.get(f"W{other}"),
                               f"W{branch}": params.get(f"W{branch}"), "几何": roles},
                    "problem": ("贯穿对两端宽度不一致（1、2 端是同一条连续主线，"
                                "默认应 W1=W2）"
                                + ("；本结呈分裂/合并格局（分支与一端同宽），"
                                   "宽度差可能是设计意图" if split else "")),
                    "suggestion": ("若无意的突变请改齐；若是功率分配结请确认臂宽；"
                                   "若有意的宽度渐变，必须加入过渡结构（如 MSTEP）"
                                   "并说明理由，不要静默突变"),
                    "verification": "已验证（原理图参数数据）",
                })
        # 与相接线上的传输线宽度比对
        for ln in lines:
            if ln.get("W_mm") is None:
                continue
            if abs(ln["W_mm"] - w_mm) > 1e-6:
                findings.append({
                    "id": f"{fid}-wjoin-{mtee['name']}-{label}-{ln['instance']}",
                    "severity": "error",
                    "element": f"{mtee['name']} pin{label} ↔ {ln['instance']}",
                    "location": "schematic",
                    "actual": actual,
                    "problem": (f"{wkey}={w_mm:.4f} mm 与相接 {ln['master']} "
                                f"W={ln['W_mm']:.4f} mm 不一致"),
                    "suggestion": "把相接传输线宽度改成一致，或给出过渡结构与理由",
                    "verification": "已验证（原理图参数数据）",
                })


def check_tlines(inv: dict, findings: list, fid: str):
    """传输线参数审查（要求 3）：基板/频率/阻抗/电长度的一致性。

    LineCalc 在本环境不可程序化调用（见 audit 报告 linecalc 字段），
    这里用闭式模型做一致性核对，全部标记「未过 LineCalc 验证」。
    """
    substrates = inv.get("substrates") or {}
    variables = inv.get("vars") or {}
    freq = inv.get("freq") or {}
    f_hz = freq.get("center_hz")
    lines = []
    for inst in inv.get("instances", []):
        if inst.get("master") not in _TLINE_MASTERS:
            continue
        lines.append(inst)
    # 先把每条线的 Z0 算出来，供中位数比较
    computed = []
    for ln in lines:
        params = ln.get("params") or {}
        entry = {"instance": ln["name"], "master": ln["master"],
                 "W_raw": params.get("W"), "L_raw": params.get("L"),
                 "Subst": params.get("Subst")}
        w_mm, w_note = parse_len_mm(params.get("W"), variables)
        l_mm, l_note = parse_len_mm(params.get("L"), variables)
        sub_name = str(params.get("Subst") or "").strip().strip('"')
        sub = substrates.get(sub_name)
        if sub is None:
            if sub_name and "(" not in sub_name:
                findings.append({
                    "id": f"{fid}-subst-{ln['name']}", "severity": "error",
                    "element": ln["name"], "location": "schematic",
                    "actual": entry,
                    "problem": f"Subst 指向的基板实例 {sub_name!r} 不存在",
                    "suggestion": "放置 MSUB 并把 Subst 指向它",
                    "verification": "已验证（原理图参数数据）",
                })
            computed.append((ln, entry, None))
            continue
        entry["基板"] = {k: sub.get(k) for k in ("H", "Er", "T", "TanD")}
        h_mm, h_note = parse_len_mm(sub.get("H"), variables)
        er_raw = _resolve_var(sub.get("Er"), variables)
        try:
            er = float(str(er_raw).strip().strip('"'))
        except (TypeError, ValueError):
            er = None
        if w_mm is None or h_mm is None or er is None:
            findings.append({
                "id": f"{fid}-params-{ln['name']}", "severity": "warning",
                "element": ln["name"], "location": "schematic",
                "actual": entry,
                "problem": ("W/L/基板 H、Er 存在无法静态求值的参数"
                            f"（W:{w_note or 'ok'} L:{l_note or 'ok'} H:{h_note or 'ok'} Er:{er is None}）"),
                "suggestion": "补齐可求值参数，或运行 LineCalc 后把结果回填为具体数值",
                "verification": "未验证（参数不可静态求值）",
            })
            computed.append((ln, entry, None))
            continue
        if ln["master"] == "CPWG":
            # 共面波导带地：闭式模型本工程未实现，不臆造数值
            findings.append({
                "id": f"{fid}-cpwg-{ln['name']}", "severity": "info",
                "element": ln["name"], "location": "schematic",
                "actual": entry,
                "problem": "CPWG 的阻抗/电长度需要 LineCalc 或 EM；本环境无法程序化调用 LineCalc",
                "suggestion": ("在 ADS 里对该线用 Tools ▸ LineCalc ▸ Start LineCalc 手工合成，"
                               "把结果回填参数后重跑审查"),
                "verification": "未验证（LineCalc 不可程序化调用）",
            })
            computed.append((ln, entry, None))
            continue
        try:
            z0, eeff = microstrip_z0(w_mm, h_mm, er)
        except ValueError as e:
            findings.append({
                "id": f"{fid}-calc-{ln['name']}", "severity": "warning",
                "element": ln["name"], "location": "schematic",
                "actual": entry,
                "problem": f"闭式模型无法计算：{e}",
                "suggestion": "核对线宽与基板参数量纲（mm/mil）后再审",
                "verification": "未验证（模型无法计算）",
            })
            computed.append((ln, entry, None))
            continue
        theta = None
        if l_mm is not None and f_hz:
            theta = microstrip_elec_len_deg(l_mm, f_hz, eeff)
        entry.update({"Z0_闭式": round(z0, 2), "eps_eff": round(eeff, 4),
                      "W_mm": round(w_mm, 4), "L_mm": None if l_mm is None else round(l_mm, 4),
                      "电长度deg": None if theta is None else round(theta, 1),
                      "频率Hz": f_hz})
        computed.append((ln, entry, z0))
    # 阻抗一致性提示：优先跟端口参考阻抗（Term 的 Z）比；没有参考时退回
    # 中位数比对（同一设计里多个阻抗层级时点名，λ/4 变换段属有意设计）
    ref_ohm = inv.get("reference_ohm")
    z0s = sorted(z for _, _, z in computed if z is not None)
    baseline = ref_ohm
    if baseline is not None:
        base_note = f"端口参考阻抗 {baseline:.1f} Ω"
    elif z0s:
        baseline = z0s[len(z0s) // 2]
        base_note = "本设计各线中位数"
    else:
        baseline, base_note = None, ""
    if baseline and baseline > 0 and f_hz:
        for ln, entry, z0 in computed:
            if z0 is None:
                continue
            dev = abs(z0 - baseline) / baseline * 100
            if dev > 30:
                findings.append({
                    "id": f"{fid}-z0dev-{ln['name']}", "severity": "warning",
                    "element": ln["name"], "location": "schematic",
                    "actual": entry,
                    "problem": (f"闭式 Z0={z0:.1f} Ω 偏离{base_note} {baseline:.1f} Ω "
                                f"达 {dev:.0f}%"),
                    "suggestion": ("确认该线是有意的阻抗变换（如 λ/4 匹配段）还是"
                                   "线宽/基板参数笔误（注意量纲 mm/mil）；"
                                   "精确结论以 LineCalc/EM 为准"),
                    "verification": "未过 LineCalc 验证（闭式模型；宽线误差更大）",
                })
    return computed


def check_schematic_corners(inv: dict, findings: list, fid: str):
    """原理图级的直角连接检查：两条微带线 90° 相接却没有弯折元件。"""
    nets = inv.get("pin_nets") or {}
    by_name = {i["name"]: i for i in inv.get("instances", [])}
    net_members = {}
    for name, inst in by_name.items():
        for p in inst.get("pins", []):
            if p.get("net"):
                net_members.setdefault(p["net"], []).append((name, p))
    for net, members in net_members.items():
        mlin_pairs = [(n, p) for n, p in members
                      if by_name.get(n, {}).get("master") in _TLINE_MASTERS]
        if len(mlin_pairs) != 2:
            continue
        has_bend = any(by_name.get(n, {}).get("master") in _BEND_MASTERS
                       for n, _ in members)
        has_step = any(by_name.get(n, {}).get("master") in _STEP_MASTERS
                       for n, _ in members)
        if has_bend:
            continue
        (na, pa), (nb, pb) = mlin_pairs
        ia, ib = by_name[na], by_name[nb]
        da = _pin_axis(ia, pa)
        dbb = _pin_axis(ib, pb)
        if da is None or dbb is None:
            continue
        dotv = da[0] * dbb[0] + da[1] * dbb[1]
        ang = math.degrees(math.acos(max(-1.0, min(1.0, dotv))))
        # da/dbb 是两线各自指向实例中心的轴向：拐角时接近 90°；
        # 近平行（0°/180°）是同一条直线延续，不是拐角
        dev = min(abs(ang), abs(180 - ang))
        if dev < 75:
            continue
        findings.append({
            "id": f"{fid}-corner-{na}-{nb}", "severity": "warning",
            "element": f"{na} ↔ {nb}（net {net}）", "location": "schematic",
            "actual": {"夹角deg": round(ang, 1), "弯折元件": "无"},
            "problem": "两条微带线直接 90° 相接，没有弯折元件",
            "suggestion": ("加入 MBEND/MSBEND 切角弯折；切角尺寸由线宽、阻抗、"
                           "基板与工艺决定（ADS 的 BendStyle: ADAPTIVE_MITERED 等），"
                           "不要写死统一比例；修正后复核电长度"),
            "verification": "已验证（原理图几何）" + ("；已有过渡结构" if has_step else ""),
        })


def _pin_axis(inst, pin):
    """该引脚指向实例中心的单位向量（线的出线方向）。"""
    xy = pin.get("xy")
    if not xy or not inst.get("origin"):
        return None
    dx = inst["origin"][0] - xy[0]
    dy = inst["origin"][1] - xy[1]
    n = math.hypot(dx, dy) or 1.0
    return (dx / n, dy / n)


def audit_schematic(inv: dict) -> dict:
    """原理图级射频审查入口。inv 由 ads_ops 侧组装（含 vars=设计 VAR 表）。"""
    fid = "rf"
    findings: list = []
    variables = inv.get("vars") or {}
    computed = check_tlines(inv, findings, fid)
    net_widths = {}
    for ln, entry, _z0 in computed:
        for p in ln.get("pins", []):
            if p.get("net") and ln.get("master") in _TLINE_MASTERS:
                w_mm, w_note = parse_len_mm((ln.get("params") or {}).get("W"), variables)
                net_widths.setdefault(p["net"], []).append(
                    {"instance": ln["name"], "master": ln["master"],
                     "W_mm": w_mm, "W_raw": (ln.get("params") or {}).get("W"),
                     "note": w_note})
    for inst in inv.get("instances", []):
        if inst.get("master") in _MTEE_MASTERS:
            roles = mtee_pin_roles(inst.get("pins") or [])
            inst["roles"] = roles
            check_mtee_widths(inst, net_widths, findings, fid, variables)
    check_schematic_corners(inv, findings, fid)
    return {
        "design": inv.get("design"),
        "frequency": inv.get("freq"),
        "tlines": [e for _, e, _z in computed],
        "findings": findings,
        "principles": ("原理图导线长度只是绘图坐标，不等于微带线物理长度；"
                       "物理宽度/长度/拐角/间隙以 ADS Layout 为准"),
    }


# ---------------------------------------------------------------------------
# Layout 审查（真实 Layout 图形；拿不到就如实返回未完成）
# ---------------------------------------------------------------------------

def _polyline_turns(pts: list):
    """顶点序列的转角（度，正值左转）。"""
    turns = []
    for i in range(1, len(pts) - 1):
        v1 = (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        v2 = (pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        n1 = math.hypot(*v1) or 1.0
        n2 = math.hypot(*v2) or 1.0
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        crossv = v1[0] * v2[1] - v1[1] * v2[0]
        turns.append(math.degrees(math.atan2(crossv, cosv)))
    return turns


def _rect_gap(a, b):
    """两个 bbox 的间隙（不相交时的最近距离；相交返回 0）。"""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _rect_overlap_area(a, b):
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return max(ox, 0.0) * max(oy, 0.0)


def analyze_layout(data: dict) -> dict:
    """真实 Layout 图形审查。data 由 ads_ops 侧从 Layout 视图提取：

    {"available": True, "empty": False, "shapes": [...], "instances": [...],
     "terms": [...], "schematic_names": [...]}
    shapes 元素: {"kind": "path|rect|dot|other", "layer": "cond|id",
                  "bbox": [x1,y1,x2,y2], "outline": [[x,y],...], "width": float}
    """
    fid = "lay"
    findings: list = []
    res = {"available": bool(data.get("available"))}
    if not data.get("available"):
        findings.append({
            "id": f"{fid}-noview", "severity": "info",
            "element": "layout 视图", "location": "-",
            "actual": {"layout_view": data.get("reason", "不存在")},
            "problem": "该设计没有可审查的 Layout 视图",
            "suggestion": ("在 ADS GUI 打开原理图后 Edit ▸ Generate/Update Layout "
                           "手动生成（本环境 Python API 无原理图→Layout 同步生成），"
                           "再重跑本审查"),
            "verification": "未完成 Layout 验证",
        })
        res.update({"empty": True, "findings": findings, "verdict": "未完成 Layout 验证"})
        return res
    shapes = data.get("shapes") or []
    if not shapes and not data.get("instances"):
        findings.append({
            "id": f"{fid}-empty", "severity": "info",
            "element": "layout 视图", "location": "-",
            "actual": {"n_shapes": 0, "n_instances": len(data.get("instances") or [])},
            "problem": "Layout 视图存在但没有任何图形/实例",
            "suggestion": "在 ADS GUI 执行 Generate/Update Layout 同步原理图后再审查",
            "verification": "未完成 Layout 验证（空 Layout）",
        })
        res.update({"empty": True, "findings": findings, "verdict": "未完成 Layout 验证"})
        return res

    res["empty"] = False
    # 1) 实例 ↔ 原理图 对应检查（同步一致性）
    lay_names = [i.get("name") for i in data.get("instances") or []]
    sch_names = data.get("schematic_names") or []
    for n in sch_names:
        if n and n not in lay_names:
            findings.append({
                "id": f"{fid}-sync-{n}", "severity": "warning",
                "element": n, "location": "layout",
                "actual": {"layout_instances": lay_names[:20]},
                "problem": "原理图实例在 Layout 中找不到同名实例（同步漂移）",
                "suggestion": "GUI 里执行 Generate/Update Layout 重新同步",
                "verification": "已验证（实例名比对）",
            })
    # 2) 直角弯折（路径类图形）
    for s in shapes:
        if s.get("kind") != "path":
            continue
        pts = s.get("outline") or []
        if len(pts) < 4:
            continue
        turns = _polyline_turns(pts)
        right_idx = [i + 1 for i, t in enumerate(turns)
                     if 70 <= abs(t) <= 110 or 250 <= abs(t) <= 290]
        if right_idx:
            v = pts[right_idx[0]]
            findings.append({
                "id": f"{fid}-rightangle-{round(v[0], 1)}-{round(v[1], 1)}",
                "severity": "warning",
                "element": f"路径图形 (layer {s.get('layer')}, width {s.get('width')})",
                "location": f"layout({v[0]:.2f}, {v[1]:.2f})",
                "actual": {"n_right_angle_vertices": len(right_idx),
                           "example_vertex": [round(v[0], 3), round(v[1], 3)]},
                "problem": "检测到未切角的直角弯折（产生阻抗不连续与反射）",
                "suggestion": ("改用 ADS 弯折/切角（CornerType: ADAPTIVE_MITERED / "
                               "MITERED 等）；切角尺寸由线宽、阻抗、基板与工艺决定，"
                               "不要写死统一比例；修正后复核电长度"),
                "verification": "已验证（Layout 几何）",
            })
    # 3) 同层重叠铜皮
    by_layer = {}
    for s in shapes:
        if s.get("kind") == "dot":
            continue
        by_layer.setdefault(s.get("layer"), []).append(s)
    for layer, group in by_layer.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                area = _rect_overlap_area(a["bbox"], b["bbox"])
                if area > 1e-6:
                    findings.append({
                        "id": f"{fid}-overlap-{i}-{j}", "severity": "warning",
                        "element": f"{a.get('kind')} ↔ {b.get('kind')} (layer {layer})",
                        "location": f"layout({b['bbox'][0]:.2f}, {b['bbox'][1]:.2f})",
                        "actual": {"bbox_a": a["bbox"], "bbox_b": b["bbox"],
                                   "重叠面积": round(area, 4)},
                        "problem": "同层铜皮重叠（若属不同网络即为短路，需人工确认）",
                        "suggestion": "确认是否同一网络；不同网络请移动/切分铜皮",
                        "verification": "已验证（Layout 几何，网络归属未比对）",
                    })
    # 4) 最小间隙（无工艺规则库：只报数值，不臆造限值）
    gaps = []
    for layer, group in by_layer.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ov = _rect_overlap_area(a["bbox"], b["bbox"])
                if ov <= 1e-6:
                    g = _rect_gap(a["bbox"], b["bbox"])
                    if g < 1e9:
                        gaps.append((g, layer, a, b))
    gaps.sort(key=lambda t: t[0])
    for g, layer, a, b in gaps[:3]:
        findings.append({
            "id": f"{fid}-gap-{a.get('kind')}-{b.get('kind')}-{round(g, 3)}",
            "severity": "info",
            "element": f"{a.get('kind')} ↔ {b.get('kind')} (layer {layer})",
            "location": f"layout",
            "actual": {"间隙": round(g, 4)},
            "problem": "本环境没有工艺规则库，间隙限值无法自动判定",
            "suggestion": "对照工艺最小线距规则人工确认这一对间距",
            "verification": "未验证（缺工艺规则）",
        })
    # 5) 接地回流：只有信号层、没有地/过层图形
    layers = {str(s.get("layer")) for s in shapes}
    if len(layers) == 1 and len(shapes) > 0:
        findings.append({
            "id": f"{fid}-ground", "severity": "warning",
            "element": "layout 全部图形", "location": "-",
            "actual": {"layers": sorted(layers)},
            "problem": "只有一层导体图形，未见地/回流层",
            "suggestion": "补接地层与回流路径（过孔、cond2 平面），或确认该设计确实单层",
            "verification": "已验证（层集合比对）",
        })
    # 6) EM
    res.update({
        "findings": findings,
        "verdict": "已审查（几何级）",
        "em": {"available": "emtools.create_empro_view 存在",
               "note": "EM 仿真自动化未实现，电路仿真与 EM 结果对比未完成",
               "verified": False},
    })
    return res
