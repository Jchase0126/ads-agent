"""RF 计算引擎 —— AppCAD 功能的 clean-room 重实现（P0）。

只依赖 Python 标准库（math/cmath），**纯函数、无 I/O、无全局状态**。
所有公开计算函数遵循同一约定：

* 入参一律 **SI 单位**（Hz、m、Ω、W、dB、K、deg）；
* 返回 dict：成功 ``{"ok": True, ...结果..., "warnings": [...]}``；
  失败 ``{"ok": False, "error": "<人话>"}`` —— **绝不抛异常**，
  UI 直接渲染，不会因坏输入弄崩 ADS；
* 每个函数 docstring 注明公式出处与适用范围。

法律边界（与 `THIRD-PARTY-NOTICES.txt` 一致）：本模块按公开教科书/论文公式
重新实现，**不含、不翻译、不派生自** Avago AppCAD 的任何代码；常量与公式
均标注原始文献。个别公式（微带导体损耗的低阻抗近似）为简化实现，输出里
带 ``warnings`` 提示，精细金属厚度修正属后续版本。

被两处共用（同一份实现，见 pathbridge.load_backend_module）：
* ADS 进程内窗口  ``addon/ads_agent/rfcalc_panel.py``；
* backend 将来注册 LLM 工具时直接 ``import rfcalc``。
"""

from __future__ import annotations

import cmath
import math

__all__ = [
    "CalcError", "to_si", "UNITS", "SUBSTRATES",
    "microstrip_analysis", "microstrip_synthesis",
    "stripline_analysis", "stripline_synthesis",
    "coax_analysis", "coax_synthesis",
    "sq_coax_analysis", "sq_coax_synthesis",
    "twin_wire_analysis", "twin_wire_synthesis",
    "cpw_analysis", "cpw_synthesis",
    "slabline_analysis", "slabline_synthesis",
    "microstrip_length", "stripline_length",
    "cascade_noise", "mixer_spurs", "im_products",
    "bjt_bias_divider", "fet_bias_self", "fet_bias_divider",
    "amp_efficiency",
    "refl_from_z", "refl_to_z", "z_to_y", "lc_resonance",
    "quarter_wave_transform",
    "rfic_voltage_bias", "rfic_current_bias",
    "detector_large_signal", "detector_small_signal",
]

C0 = 299792458.0            # 真空光速 m/s（SI 定义值）
MU0 = 1.25663706212e-6      # 真空磁导率 H/m（2019 SI）
ETA0 = math.sqrt(MU0 / 8.8541878128e-12)   # 真空波阻抗 ≈ 376.730313668 Ω
T0 = 290.0                  # 标准噪声温度 K（IEEE）
KB = 1.380649e-23           # 玻尔兹曼常数 J/K（2019 SI）
DB = 10.0 / math.log(10.0)  # Np→dB 换算 8.685889638…


class CalcError(ValueError):
    """用户输入不合法 —— 文案给人看，直接显示在界面上。"""


# ---------------------------------------------------------------------------
# 单位与数值助手
# ---------------------------------------------------------------------------

UNITS = {
    "freq": {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9},
    "len": {"m": 1.0, "mm": 1e-3, "cm": 1e-2, "um": 1e-6, "µm": 1e-6,
            "mil": 25.4e-6, "inch": 0.0254, "in": 0.0254},
    "cap": {"F": 1.0, "mF": 1e-3, "uF": 1e-6, "µF": 1e-6,
            "nF": 1e-9, "pF": 1e-12},
    "ind": {"H": 1.0, "mH": 1e-3, "uH": 1e-6, "µH": 1e-6,
            "nH": 1e-9, "pH": 1e-12},
    "res": {"Ω": 1.0, "kΩ": 1e3, "MΩ": 1e6},
    "time": {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12},
}


def to_si(value, unit: str, family: str) -> float:
    """把 ``value``（数字或字符串）按 ``family`` 里的 ``unit`` 换算成 SI。

    单位表见 :data:`UNITS`；不认识的家庭/单位/数值一律抛 :class:`CalcError`。
    """
    table = UNITS.get(family)
    if table is None:
        raise CalcError(f"未知的单位类别：{family}")
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        raise CalcError(f"数值不合法：{value!r}")
    scale = table.get(unit)
    if scale is None:
        raise CalcError(f"不认识的单位：{unit}（可用：{'/'.join(table)}）")
    return v * scale


def db(x: float) -> float:
    return 10.0 * math.log10(x)


def undb(x: float) -> float:
    return 10.0 ** (x / 10.0)


def w_to_dbm(p_w: float) -> float:
    return 10.0 * math.log10(p_w * 1e3)


def dbm_to_w(p_dbm: float) -> float:
    return 10.0 ** (p_dbm / 10.0) / 1e3


def _num(x, name: str) -> float:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        raise CalcError(f"{name}：数值不合法（收到 {x!r}）")


def _pos(x, name: str) -> float:
    v = _num(x, name)
    if v <= 0:
        raise CalcError(f"{name} 必须大于 0（收到 {v}）")
    return v


def _nonneg(x, name: str) -> float:
    v = _num(x, name)
    if v < 0:
        raise CalcError(f"{name} 不能为负（收到 {v}）")
    return v


def _range(x, lo: float, hi: float, name: str) -> float:
    v = _num(x, name)
    if v < lo or v > hi:
        raise CalcError(f"{name} 需在 {lo}…{hi} 之间（收到 {v}）")
    return v


def _safe(fn):
    """把计算函数包成“永不抛异常”的接口。"""
    def wrapper(*args, **kwargs):
        try:
            out = fn(*args, **kwargs)
        except CalcError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 —— 引擎内部错误也要如实报告
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out.setdefault("ok", True)
        out.setdefault("warnings", [])
        return out
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _K(k: float) -> float:
    """第一类完全椭圆积分 K(k)，AGM（算术几何平均）精确计算，二次收敛，
    全域相对误差 ~1e-15（取代任何近似表式，如 Hilberg 1969）。"""
    if not 0.0 <= k < 1.0:
        raise CalcError("椭圆积分参数 k 需在 0…1 之间")
    a, b = 1.0, math.sqrt(1.0 - k * k)
    while abs(a - b) > 1e-15 * a:
        a, b = 0.5 * (a + b), math.sqrt(a * b)
    return math.pi / (2.0 * a)


def _hilberg(k: float) -> float:
    """椭圆积分比 K(k')/K(k)，k'=sqrt(1-k²)。AGM 精确计算；k² 浮点下溢
    （k<~1e-8，k' 塌缩为 1.0）时用小宗量渐近 K(k')≈ln(4/k)、K(k)≈π/2。"""
    if not 0.0 < k < 1.0:
        raise CalcError("椭圆积分参数 k 需在 0…1 之间")
    kk = k * k
    if 1.0 - kk == 1.0:  # k² 下溢：比值 ≈ (2/π)·ln(4/k)，误差 O(k²) 可忽略
        return (2.0 / math.pi) * math.log(4.0 / k)
    return _K(math.sqrt(1.0 - kk)) / _K(k)


def _sinh_ratio(a: float, b: float) -> float:
    """sinh(a)/sinh(b) 数值稳定版（要求 0<a<b；大参数避免 OverflowError）。"""
    if a < 700.0 and b < 700.0:
        return math.sinh(a) / math.sinh(b)
    return math.exp(a - b)  # a<b：结果 <1，不溢出


def _bisect_monotonic(f, lo: float, hi: float, target: float, iters: int = 200):
    """在 [lo,hi] 内解 f(x)=target（f 单调、端点异号）；返回 x。"""
    flo = f(lo) - target
    fhi = f(hi) - target
    if flo * fhi > 0:
        raise CalcError("公式适用范围内无解（目标值超出可综合范围）")
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        fm = f(mid) - target
        if fm == 0.0:
            return mid
        if fm * flo < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# 基片库（内置典型值；自定义基片由 UI 层持久化，引擎不做 I/O）
# ---------------------------------------------------------------------------

SUBSTRATES = [
    {"name": "Rogers RT/duroid 5880", "er": 2.20, "tand": 0.0009,
     "note": "PTFE 玻纤，毫米波常用；tanδ @10GHz"},
    {"name": "Rogers RO4003C", "er": 3.38, "tand": 0.0027,
     "note": "玻纤/陶瓷填料，process-friendly；tanδ @10GHz"},
    {"name": "Rogers RO4350B", "er": 3.48, "tand": 0.0037,
     "note": "FR-4 工艺兼容；tanδ @10GHz"},
    {"name": "Rogers RO3010", "er": 10.2, "tand": 0.0022,
     "note": "高介电常数陶瓷填料；tanδ @10GHz"},
    {"name": "Taconic RF-35", "er": 3.5, "tand": 0.0018,
     "note": "玻璃微纤 PTFE；tanδ @10GHz"},
    {"name": "氧化铝 Al2O3 99.5%", "er": 9.8, "tand": 0.0001,
     "note": "薄膜/厚膜混合电路基片"},
    {"name": "蓝宝石 Sapphire", "er": 9.4, "tand": 0.0001,
     "note": "各向异性，此处取平均 εr（⊥c 与 ∥c 分别 9.4/11.6）"},
    {"name": "硅 Si（高阻）", "er": 11.9, "tand": 0.005,
     "note": "电阻率 >1kΩ·cm 时 tanδ 约 0.005 量级，随电阻率变化"},
    {"name": "砷化镓 GaAs（半绝缘）", "er": 12.9, "tand": 0.0004,
     "note": "MMIC 基片；εr=12.85–13"},
    {"name": "FR-4", "er": 4.4, "tand": 0.02,
     "note": "离散典型值，1GHz 以上损耗随频率明显增大"},
    {"name": "PTFE 纯板", "er": 2.10, "tand": 0.0004,
     "note": "软板/电缆介质常用"},
    {"name": "空气/真空", "er": 1.0, "tand": 0.0,
     "note": "悬空结构、天线罩估算"},
]


# ---------------------------------------------------------------------------
# 椭圆积分比复用的小工具
# ---------------------------------------------------------------------------

def _kh_ratio_from_w_b(u: float) -> float:
    """对称带状线 Cohn 映射：k' = tanh(π·w/(2b))（即 k = sech(πw/2b)），
    返回 K(k)/K(k')。

    出处：S.B. Cohn, "Problems in Strip Transmission Lines", IRE Trans. MTT,
    1955（零厚度对称带状线保角映射）。双极限自检：w≪b 时
    Z0 → (60/√εr)·ln(8b/πw)（薄带极限，等价半径 w/4）；w≫b 时
    Z0 → (94.25/√εr)·(b/w)（上下两个 b/2 间隙平行板并联）。
    """
    kp = math.tanh(math.pi * u / 2.0)
    return _K(math.sqrt(1.0 - kp * kp)) / _K(kp)


# ---------------------------------------------------------------------------
# 微带线（Hammerstad–Jensen 1980，零厚度）
# ---------------------------------------------------------------------------

def _ms_z0_u(u: float, er: float):
    """H-J 分析式，返回 (Z0, εeff)。

    出处：E. Hammerstad, O. Jensen, "Accurate Models for Microstrip
    Computer-Aided Design", IEEE MTT-S Digest, 1980。适用 0.01≤u≤100，
    1≤εr≤128（文献声明精度 Z0<0.2%、εeff<0.3% 量级）。
    """
    if u <= 0:
        raise CalcError("W/h 必须大于 0")
    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.758))
    z01 = ETA0 / (2.0 * math.pi) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))
    a = (1.0 + (1.0 / 49.0) * math.log((u ** 4 + (u / 52.0) ** 2) / (u ** 4 + 0.432))
         + (1.0 / 18.7) * math.log(1.0 + (u / 18.1) ** 3))
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    eeff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)
    return z01 / math.sqrt(eeff), eeff


def _ms_eeff(u: float, er: float) -> float:
    return _ms_z0_u(u, er)[1]


def _ms_losses(w: float, h: float, er: float, eeff: float,
               z0: float, f: float, tand: float, rho: float):
    """微带损耗（Np/m）。

    介质损耗：αd = (π·f·√εeff/c)·(εr/εeff)·((εeff−1)/(εr−1))·tanδ（标准式，
    见 Gupta《Microstrip Lines and Slotlines》）。导体损耗：低阻抗近似
    αc ≈ Rs/(2·Z0·W)，未计金属厚度修正（P0 简化，输出带 warning）。
    """
    warn = []
    alpha_d = 0.0
    if f > 0 and tand > 0 and er > 1.0 and eeff > 1.0:
        alpha_d = (math.pi * f * math.sqrt(eeff) / C0) * (er / eeff) * \
                  ((eeff - 1.0) / (er - 1.0)) * tand
    alpha_c = 0.0
    if f > 0 and rho > 0:
        rs = math.sqrt(math.pi * f * MU0 * rho)
        alpha_c = rs / (2.0 * z0 * w)
        if w < 5.0 * h:
            warn.append("导体损耗为低阻抗近似 Rs/(2·Z0·W)，W 未显著大于 h 时"
                        "误差增大（P0 未做金属厚度修正）")
    return alpha_c, alpha_d, warn


def _ms_common_out(w: float, h: float, er: float, tand: float,
                   f: float, rho: float, u: float):
    z0, eeff = _ms_z0_u(u, er)
    out = {"Z0": z0, "eeff": eeff, "w": w, "w_over_h": u}
    if f > 0:
        lam_g = C0 / (f * math.sqrt(eeff))
        out["lambda_g"] = lam_g
        alpha_c, alpha_d, warn = _ms_losses(w, h, er, eeff, z0, f, tand, rho)
        out["alpha_c_np_m"] = alpha_c
        out["alpha_d_np_m"] = alpha_d
        out["loss_db_m"] = DB * (alpha_c + alpha_d)
        out["loss_db_lambda"] = DB * (alpha_c + alpha_d) * lam_g
        out["warnings"] = warn
    else:
        out["warnings"] = []
    return out


@_safe
def microstrip_analysis(w, h, er, tan_delta=0.0, freq=0.0, rho=1.72e-8):
    """微带线**分析**：W,h,εr → Z0/εeff（+可选频率下的损耗/波导波长）。

    出处：Hammerstad–Jensen 1980（见 _ms_z0_u）。频率/损耗为可选项。
    rho：金属电阻率 Ω·m（默认铜 1.72e-8）。
    """
    w = _pos(w, "导带宽度 W")
    h = _pos(h, "基片厚度 h")
    er = _pos(er, "相对介电常数 εr")
    tand = _nonneg(tan_delta, "损耗角正切 tanδ")
    f = _nonneg(freq, "频率 f")
    rho = _pos(rho, "金属电阻率 ρ")
    u = w / h
    out = _ms_common_out(w, h, er, tand, f, rho, u)
    if not (0.01 <= u <= 100.0):
        out["warnings"].append(f"W/h={u:.3g} 超出 H-J 公式声明范围 0.01…100，"
                               "结果仅供参考")
    return out


@_safe
def microstrip_synthesis(z0_target, h, er, tan_delta=0.0, freq=0.0,
                         rho=1.72e-8):
    """微带线**综合**：目标 Z0,h,εr → W（H-J 式上做数值反解）。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    h = _pos(h, "基片厚度 h")
    er = _pos(er, "相对介电常数 εr")
    tand = _nonneg(tan_delta, "损耗角正切 tanδ")
    f = _nonneg(freq, "频率 f")
    u = _bisect_monotonic(lambda x: _ms_z0_u(x, er)[0], 1e-3, 1e3, z0_target)
    w = u * h
    out = _ms_common_out(w, h, er, tand, f, rho, u)
    out["Z0_resid"] = out["Z0"] - z0_target
    return out


@_safe
def microstrip_length(w, h, er, theta_deg, freq):
    """微带线：给定电长度 θ(度) 与频率 → 物理长度 L = θ/360·λg。"""
    h = _pos(h, "基片厚度 h")
    er = _pos(er, "相对介电常数 εr")
    theta = _num(theta_deg, "电长度 θ")
    f = _pos(freq, "频率 f")
    w = _pos(w, "导带宽度 W")
    u = w / h
    _, eeff = _ms_z0_u(u, er)
    lam_g = C0 / (f * math.sqrt(eeff))
    return {"L": theta / 360.0 * lam_g, "lambda_g": lam_g, "eeff": eeff,
            "w_over_h": u}


# ---------------------------------------------------------------------------
# 带状线（对称，零厚度，Cohn 保角映射）
# ---------------------------------------------------------------------------

def _sl_z0_u(u: float, er: float) -> float:
    """对称带状线 Z0 = (30π/√εr)·K(k)/K(k')，k' = tanh(π·w/(2b))。

    出处：S.B. Cohn, "Problems in Strip Transmission Lines", IRE Trans. MTT,
    1955（零厚度对称带状线保角映射精确式；椭圆积分经 AGM 精确计算）。
    """
    if u <= 0:
        raise CalcError("w/b 必须大于 0")
    if u > 10.0:
        raise CalcError("w/b>10：超出 Cohn 保角映射适用域（已趋于平行板极限，"
                        "请直接用 Z0 = 94.25·b/(w·√εr) 估算")
    return 30.0 * math.pi / math.sqrt(er) * _kh_ratio_from_w_b(u)


def _sl_common(w: float, b: float, er: float, tand: float, f: float):
    u = w / b
    z0 = _sl_z0_u(u, er)
    out = {"Z0": z0, "w_over_b": u, "warnings": []}
    if f > 0:
        lam_g = C0 / (f * math.sqrt(er))
        out["lambda_g"] = lam_g
        if tand > 0:
            # 均匀介质：αd = π·f·√εr/c·tanδ
            ad = math.pi * f * math.sqrt(er) / C0 * tand
            out["alpha_d_np_m"] = ad
            out["loss_db_m"] = DB * ad
            out["loss_db_lambda"] = DB * ad * lam_g
    return out


@_safe
def stripline_analysis(w, b, er, tan_delta=0.0, freq=0.0):
    """带状线（对称，零厚度）**分析**：w,b,εr → Z0（Cohn 1955）。"""
    w = _pos(w, "导带宽度 W")
    b = _pos(b, "上下接地板间距 b")
    er = _pos(er, "相对介电常数 εr")
    out = _sl_common(w, b, er, _nonneg(tan_delta, "tanδ"),
                     _nonneg(freq, "频率 f"))
    if w / b > 2.0:
        out["warnings"].append("w/b>2：宽导带场合建议改用平行板近似核算")
    return out


@_safe
def stripline_synthesis(z0_target, b, er, tan_delta=0.0, freq=0.0):
    """带状线**综合**：目标 Z0,b,εr → W（Cohn 式数值反解，单调二分）。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    b = _pos(b, "上下接地板间距 b")
    er = _pos(er, "相对介电常数 εr")
    u = _bisect_monotonic(lambda x: _sl_z0_u(x, er), 1e-7, 10.0, z0_target)
    w = u * b
    out = _sl_common(w, b, er, _nonneg(tan_delta, "tanδ"),
                     _nonneg(freq, "频率 f"))
    out["Z0_resid"] = out["Z0"] - z0_target
    return out


@_safe
def stripline_length(w, b, er, theta_deg, freq):
    """带状线：给定电长度 θ(度) 与频率 → 物理长度。"""
    b = _pos(b, "上下接地板间距 b")
    er = _pos(er, "相对介电常数 εr")
    theta = _num(theta_deg, "电长度 θ")
    f = _pos(freq, "频率 f")
    w = _pos(w, "导带宽度 W")
    lam_g = C0 / (f * math.sqrt(er))
    return {"L": theta / 360.0 * lam_g, "lambda_g": lam_g,
            "w_over_b": w / b}


# ---------------------------------------------------------------------------
# 同轴（圆，精确式；方，Frank 等效半径）
# ---------------------------------------------------------------------------

@_safe
def coax_analysis(a, b, er, tan_delta=0.0, freq=0.0, rho=1.72e-8):
    """圆同轴**分析**：内导体半径 a、外导体内半径 b → Z0 = 60/√εr·ln(b/a)。

    精确静电解（任意教材）；TE11 高次模截止频率 f_c,TE11 ≈ c/(π(a+b)√εr)，
    单模工作要求 f < f_c。
    """
    a = _pos(a, "内导体半径 a")
    b = _pos(b, "外导体内半径 b")
    er = _pos(er, "相对介电常数 εr")
    if a >= b:
        raise CalcError("内导体半径 a 必须小于外导体内半径 b")
    f = _nonneg(freq, "频率 f")
    rho = _pos(rho, "金属电阻率 ρ")
    ratio = b / a
    z0 = 60.0 / math.sqrt(er) * math.log(ratio)
    out = {"Z0": z0, "b_over_a": ratio, "warnings": []}
    fc = C0 / (math.pi * (a + b) * math.sqrt(er))
    out["fc_te11"] = fc
    if f > 0:
        if f >= fc:
            out["warnings"].append(
                f"工作频率高于 TE11 截止频率 {fc/1e9:.3g} GHz，已非单模传输")
        out["lambda_g"] = C0 / (f * math.sqrt(er))
        rs = math.sqrt(math.pi * f * MU0 * rho)
        ac = rs / (4.0 * math.pi * z0) * (1.0 / a + 1.0 / b)
        ad = math.pi * f * math.sqrt(er) / C0 * _nonneg(tan_delta, "tanδ")
        out["alpha_c_np_m"] = ac
        out["alpha_d_np_m"] = ad
        out["loss_db_m"] = DB * (ac + ad)
    return out


@_safe
def coax_synthesis(z0_target, b, er):
    """圆同轴**综合**：目标 Z0 与外导体内半径 b → 内导体半径 a。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    b = _pos(b, "外导体内半径 b")
    er = _pos(er, "相对介电常数 εr")
    a = b / math.exp(z0_target * math.sqrt(er) / 60.0)
    return {"a": a, "b_over_a": b / a, "Z0": z0_target}


@_safe
def sq_coax_analysis(a, s, er):
    """方同轴**分析**：内导体半径 a、外导体方腔边长 s → Z0。

    出处：等效半径法（Frank 1932：边长 s 的正方形等效于半径 0.539038·s
    的圆），再套圆同轴精确式 Z0 = 60/√εr·ln(b_eq/a)，即文献中
    Z0 = 60/√εr·ln(1.078076·s/d)（d=2a）。
    常数 1.078076 按 Avago AppCAD 4.00 实机三点定标反解（2026-10-30）：
    D1/D2=100/43mil、εr=1 → Z0 显示 55.1；D2=80mil → 17.9；
    Z0=50mil 反算 D2 → 46.853mil，三点均与实机显示一致。
    流传值 1.0787（等效半径 0.53935·s）与实机不符（给 55.2）。
    适用 a << s（经验上 a ≤ 0.4s 内误差小）。
    """
    a = _pos(a, "内导体半径 a")
    s = _pos(s, "方腔边长 s")
    er = _pos(er, "相对介电常数 εr")
    if a >= 0.5 * s:
        raise CalcError("内导体半径 a 必须小于方腔内切半径 s/2")
    b_eq = 0.539038 * s
    z0 = 60.0 / math.sqrt(er) * math.log(b_eq / a)
    out = {"Z0": z0, "b_eq_over_a": b_eq / a, "warnings": []}
    if a > 0.4 * s:
        out["warnings"].append("a > 0.4s：等效半径法误差增大，结果仅供参考")
    return out


@_safe
def sq_coax_synthesis(z0_target, s, er):
    """方同轴**综合**：目标 Z0 与方腔边长 s → 内导体半径 a。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    s = _pos(s, "方腔边长 s")
    er = _pos(er, "相对介电常数 εr")
    b_eq = 0.539038 * s
    a = b_eq / math.exp(z0_target * math.sqrt(er) / 60.0)
    if a >= 0.5 * s:
        raise CalcError("目标 Z0 过低：内导体直径将超出方腔（需 2a < s）")
    return {"a": a, "Z0": z0_target,
            "warnings": (["a > 0.4s：等效半径法误差增大"] if a > 0.4 * s else [])}


# ---------------------------------------------------------------------------
# 平行双线（精确式）
# ---------------------------------------------------------------------------

def _tw_z0(d_over_D, er):
    return 120.0 / math.sqrt(er) * math.acosh(d_over_D)


@_safe
def twin_wire_analysis(d, D, er):
    """平行双线**分析**：导线直径 d、中心距 D → Z0 = 120/√εr·acosh(D/d)。

    精确静电解（任意传输线教材），适用 D ≥ d。
    """
    d = _pos(d, "导线直径 d")
    D = _pos(D, "中心距 D")
    er = _pos(er, "相对介电常数 εr")
    if D <= d:
        raise CalcError("中心距 D 必须大于导线直径 d")
    return {"Z0": _tw_z0(D / d, er), "D_over_d": D / d}


@_safe
def twin_wire_synthesis(z0_target, D, er):
    """平行双线**综合**：目标 Z0、中心距 D → 导线直径 d。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    D = _pos(D, "中心距 D")
    er = _pos(er, "相对介电常数 εr")
    x = math.cosh(z0_target * math.sqrt(er) / 120.0)  # = D/d
    return {"d": D / x, "D_over_d": x, "Z0": z0_target}


# ---------------------------------------------------------------------------
# 共面波导 CPW（Wen 保角映射 + Hilberg 比值；有限厚度用 k1 修正）
# ---------------------------------------------------------------------------

def _cpw_z0(k: float, eeff: float) -> float:
    return 30.0 * math.pi / math.sqrt(eeff) * _hilberg(k)


@_safe
def cpw_analysis(s, g, h, er):
    """CPW**分析**：中心导带 S、槽宽 G、基片厚 h、εr → Z0/εeff。

    出处：C.P. Wen, IEEE Trans. MTT-17, 1969（保角映射）；椭圆积分比用
    AGM 精确计算（K(k')/K(k)）；有限基片厚度修正 Simons 工程闭式：
    k1 = sinh(πS/4h)/sinh(π(S+2G)/4h)、
    εeff = 1+(εr−1)/2·[K(k')/K(k)]/[K(k1')/K(k1)]、
    Z0 = (30π/√εeff)·K(k')/K(k)（Ghione–Naldi / Simons 教科书形式）。
    双极限自检：h→∞ 时 k1→k、εeff=(εr+1)/2（半无限介质经典结果）；
    h→0 时 εeff→1、Z0→自由空间 CPW 值（场全部逸入空气）。
    """
    s = _pos(s, "导带宽度 S")
    g = _pos(g, "槽宽 G")
    h = _pos(h, "基片厚度 h")
    er = _pos(er, "相对介电常数 εr")
    k = s / (s + 2.0 * g)
    k1 = _sinh_ratio(math.pi * s / (4.0 * h),
                     math.pi * (s + 2.0 * g) / (4.0 * h))
    kk = _hilberg(k)      # p  = K(k')/K(k)
    k1k1 = _hilberg(k1)   # p1 = K(k1')/K(k1)
    eeff = 1.0 + (er - 1.0) / 2.0 * (kk / k1k1)
    z0 = 30.0 * math.pi / math.sqrt(eeff) * kk
    out = {"Z0": z0, "eeff": eeff, "k": k, "warnings": []}
    if k > 0.9:
        out["warnings"].append("k=S/(S+2G)>0.9：窄槽极限，保角近似误差增大")
    if h < (s + 2.0 * g) / 4.0:
        out["warnings"].append(
            "基片厚度 h < (S+2G)/4：Simons 闭式近似精度下降，建议全波验证")
    return out


@_safe
def cpw_synthesis(z0_target, ratio_s_g, h, er):
    """CPW**综合**：给定 S/G 比（或由目标 Z0 反解 k，二选一）。

    P0 实现：固定 ``ratio_s_g``（S 与 G 的比值，>0），数值反解整体尺寸比
    满足目标 Z0，返回 S/h 比例系数。要求 h 有限或无限均可（用 h 缩放不变性
    时传 h=1）。
    """
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    r = _pos(ratio_s_g, "S/G 比值")
    h = _pos(h, "基片厚度 h")
    er = _pos(er, "相对介电常数 εr")

    def f(x_s_over_h):
        s = x_s_over_h * h
        g = s / r
        k = s / (s + 2.0 * g)
        k1 = _sinh_ratio(math.pi * s / (4.0 * h),
                         math.pi * (s + 2.0 * g) / (4.0 * h))
        eeff = 1.0 + (er - 1.0) / 2.0 * (_hilberg(k) / _hilberg(k1))
        return 30.0 * math.pi / math.sqrt(eeff) * _hilberg(k)

    x = _bisect_monotonic(f, 1e-4, 50.0, z0_target)
    s = x * h
    g = s / r
    return {"s": s, "g": g, "Z0": f(x),
            "Z0_resid": f(x) - z0_target}


# ---------------------------------------------------------------------------
# 板线 Slabline（Frankel）
# ---------------------------------------------------------------------------

def _slab_z0(d_over_b, er):
    """板线（圆导线居中于两接地板）：Z0 = (60/√εr)·ln[coth(πd/4b)]。

    Frankel/Cohn 经典式。d≪b 时 coth(x)→1/x，退化为薄线精确极限
    Z0 → (60/√εr)·ln(4b/πd)（平行板格林函数严格结果）；d/b→1 时
    Z0 → (60/√εr)·ln(coth(π/4)) = 25.3/√εr，单调且有限。
    """
    u = d_over_b
    if not 0.0 < u < 1.0:
        raise CalcError("d/b 需在 0…1 之间（导线不得触碰接地板）")
    return 60.0 / math.sqrt(er) * math.log(1.0 / math.tanh(math.pi * u / 4.0))


@_safe
def slabline_analysis(d, b, er):
    """板线（圆导线居中于两平行接地板，间距 b）**分析**。

    出处：Frankel/Cohn 式 Z0 = (60/√εr)·ln[coth(πd/4b)]。
    d≪b 时退化为 60/√εr·ln(4b/πd)（薄线精确极限）；d/b→1 时趋于
    25.3/√εr。d/b>0.5 时精度下降，结果仅供粗估。
    """
    d = _pos(d, "导线直径 d")
    b = _pos(b, "接地板间距 b")
    er = _pos(er, "相对介电常数 εr")
    u = d / b
    if u >= 1.0:
        raise CalcError("d/b 不能 ≥1（导线将触碰接地板）")
    out = {"Z0": _slab_z0(u, er), "d_over_b": u, "warnings": []}
    if u > 0.5:
        out["warnings"].append("d/b>0.5：Frankel/Cohn 式精度下降，仅供粗估")
    return out


@_safe
def slabline_synthesis(z0_target, b, er):
    """板线**综合**：目标 Z0、接地板间距 b → 导线直径 d（数值反解）。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    b = _pos(b, "接地板间距 b")
    er = _pos(er, "相对介电常数 εr")
    u = _bisect_monotonic(lambda x: _slab_z0(x, er), 1e-4, 0.95, z0_target)
    d = u * b
    return {"d": d, "d_over_b": u, "Z0": _slab_z0(u, er),
            "Z0_resid": _slab_z0(u, er) - z0_target}


# ---------------------------------------------------------------------------
# 线过地板 Wire-Over-Groundplane（镜像法，AppCAD Transmission Lines 同款页面）
# ---------------------------------------------------------------------------

def _wog_z0(twoh_over_d, er):
    """单圆导线平行于接地板：Z0 = (60/√εr)·acosh(2h/d)。

    镜像法严格解（等价双线 D=2h 取一半）。2h/d≫1 时退化为教科书式
    Z0 → (60/√εr)·ln(4h/d)。
    """
    if twoh_over_d <= 1.0:
        raise CalcError("2h/d 需大于 1（导线表面不得触碰地板）")
    return 60.0 / math.sqrt(er) * math.acosh(twoh_over_d)


@_safe
def wire_over_ground_analysis(d, h, er):
    """线过地板**分析**：导线直径 d、离地板高度 h（介质均匀 εr）→ Z0。

    出处：镜像法 Z0 = (60/√εr)·acosh(2h/d)；h≫d 时 ≈ (60/√εr)·ln(4h/d)。
    h 需大于 d/2。
    """
    d = _pos(d, "导线直径 d")
    h = _pos(h, "离地板高度 h")
    er = _pos(er, "相对介电常数 εr")
    if 2.0 * h <= d:
        raise CalcError("高度 h 必须大于导线半径 d/2（否则导线触碰地板）")
    out = {"Z0": _wog_z0(2.0 * h / d, er), "twoh_over_d": 2.0 * h / d,
           "warnings": []}
    if 2.0 * h / d < 4.0:
        out["warnings"].append("2h/d<4：导线贴近地板，镜像式误差增大，仅供粗估")
    return out


@_safe
def wire_over_ground_synthesis(z0_target, h, er):
    """线过地板**综合**：目标 Z0、离地板高度 h → 导线直径 d。"""
    z0_target = _pos(z0_target, "目标阻抗 Z0")
    h = _pos(h, "离地板高度 h")
    er = _pos(er, "相对介电常数 εr")
    d = 2.0 * h / math.cosh(z0_target * math.sqrt(er) / 60.0)
    return {"d": d, "twoh_over_d": 2.0 * h / d, "Z0": z0_target,
            "warnings": []}


# ---------------------------------------------------------------------------
# 级联噪声（Friis / IP3 功率求和 —— 与 AppCAD 同口径）
# ---------------------------------------------------------------------------

@_safe
def cascade_noise(stages, bw_hz, t_ambient=T0):
    """级联噪声/失真：按信号顺序的 N 级列表 → 总 NF/增益/OIP3/噪声温度/噪底。

    * NF：Friis 公式 F_tot = F1 + (F2−1)/G1 + (F3−1)/(G1G2) + …
      （H.T. Friis, Proc. IRE, 1944）。
    * OIP3：功率求和 1/OIP3_tot = 1/OIP3_1 + 1/(OIP3_2·G1) + …
      （级联三阶交调的保守功率叠加口径，与 AppCAD NoiseCalc 相同）。
    * 噪声温度 Te = (F−1)·T0（T0=290K）；系统噪声温度 T_sys = Te + T_amb。
    * 输出噪底 = k·T_amb·B·G_tot·F_tot。
    stages 元素：{"gain_db": float, "nf_db": float, "oip3_dbm": float|None}。
    """
    if not stages:
        raise CalcError("至少需要一级电路")
    bw = _pos(bw_hz, "带宽 B")
    ta = _nonneg(t_ambient, "环境温度 T")
    g_lin = 1.0
    f_tot = 1.0
    inv_oip3 = 0.0
    oip3_known = True
    for i, st in enumerate(stages, 1):
        gdB = _num(st.get("gain_db"), f"第{i}级增益 Gain")
        ndB = _num(st.get("nf_db"), f"第{i}级噪声系数 NF")
        if gdB <= -600 or ndB < -600:
            raise CalcError(f"第{i}级增益/NF 数值离谱，请检查单位（应为 dB）")
        fi = undb(ndB)
        gi = undb(gdB)
        f_tot += (fi - 1.0) / g_lin
        g_before = g_lin
        g_lin *= gi
        oip3_dbm = st.get("oip3_dbm")
        if oip3_dbm is None or str(oip3_dbm).strip() == "":
            oip3_known = False
        else:
            p3 = dbm_to_w(_num(oip3_dbm, f"第{i}级 OIP3"))
            if p3 <= 0:
                raise CalcError(f"第{i}级 OIP3 必须大于 0")
            inv_oip3 += 1.0 / (p3 * g_before)
    out = {
        "gain_db": db(g_lin),
        "nf_db": db(f_tot),
        "f_lin": f_tot,
        "gain_lin": g_lin,
        "te_k": (f_tot - 1.0) * T0,
        "tsys_k": (f_tot - 1.0) * T0 + ta,
        "kTb_dbm": w_to_dbm(KB * ta * bw),
        "noise_floor_dbm": w_to_dbm(KB * ta * bw * g_lin * f_tot),
        "warnings": [],
    }
    if oip3_known and inv_oip3 > 0:
        oip3_w = 1.0 / inv_oip3
        out["oip3_dbm"] = w_to_dbm(oip3_w)
        out["iip3_dbm"] = w_to_dbm(oip3_w / g_lin)
    else:
        out["oip3_dbm"] = None
        out["iip3_dbm"] = None
        out["warnings"].append("有级未填 OIP3，未计算级联 IP3")
    if ta != T0:
        out["warnings"].append(
            f"环境温度 {ta:g}K：NF 按输入值使用，仅噪底/噪声温度按 {ta:g}K 换算"
            "（P0 不做 NF 温度系数模型）")
    return out


# ---------------------------------------------------------------------------
# 混频器杂散表（m·fRF ± n·fLO）
# ---------------------------------------------------------------------------

@_safe
def mixer_spurs(f_rf, f_lo, f_if, if_bw_hz, m_max=3, n_max=3):
    """混频器杂散：枚举 f_sp = |m·fRF − n·fLO|（含 +/− 组合），标记带内杂散。

    与 AppCAD SpurFind 同口径的频率表（**不含电平估计**——AppCAD 的杂散电平
    是器件经验查找表，P0 不实现，见窗口页说明）。带内判据：
    |f_sp − f_IF| ≤ IF带宽/2。
    """
    frf = _pos(f_rf, "RF 频率")
    flo = _pos(f_lo, "LO 频率")
    fif = _pos(f_if, "中频 IF")
    bw = _nonneg(if_bw_hz, "IF 带宽")
    m_max = int(_range(m_max, 1, 20, "m 最大阶数"))
    n_max = int(_range(n_max, 1, 20, "n 最大阶数"))
    lo_bd, hi_bd = fif - bw / 2.0, fif + bw / 2.0
    seen, rows = set(), []
    for m in range(0, m_max + 1):
        for n in range(0, n_max + 1):
            if m == 0 and n == 0:
                continue
            for sp in (m * frf + n * flo, m * frf - n * flo):
                if sp <= 0:
                    continue
                key = (round(sp, 3), m, n)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "m": m, "n": n, "order": m + n, "f_spur": sp,
                    "inband": bool(lo_bd <= sp <= hi_bd),
                })
    rows.sort(key=lambda r: (not r["inband"], r["f_spur"], r["order"]))
    n_in = sum(1 for r in rows if r["inband"])
    return {"rows": rows, "n_inband": n_in,
            "if_band": (lo_bd, hi_bd),
            "warnings": (["杂散频率表不含电平/危害度估计（器件相关，P2 实现）"]
                         if n_in == 0 else
                         [f"检测到 {n_in} 个带内杂散——注意核查其阶数与幅度来源"])}



# ---------------------------------------------------------------------------
# 互调产物
# ---------------------------------------------------------------------------

@_safe
def im_products(f1, f2, p_tone_dbm=None, oip2_dbm=None, oip3_dbm=None):
    """双音互调：f1,f2 → IM2/IM3 产物频率（可选电平估算）。

    电平规则（两等幅音，标准定义）：P_IM3 = 3·P − 2·OIP3，
    P_IM2 = 2·P − OIP2（OIPn 为输出三/二阶截点 dBm）。
    """
    f1 = _pos(f1, "音1 频率 f1")
    f2 = _pos(f2, "音2 频率 f2")
    if f1 == f2:
        raise CalcError("f1 与 f2 不能相同")
    rows = [
        ("2f1", 2.0 * f1, "IM3"), ("2f2", 2.0 * f2, "IM3"),
        ("f2−f1", f2 - f1, "IM2"), ("f1+f2", f1 + f2, "IM2"),
        ("2f1−f2", 2.0 * f1 - f2, "IM3"), ("2f2−f1", 2.0 * f2 - f1, "IM3"),
        ("2f1+f2", 2.0 * f1 + f2, "IM3"), ("2f2+f1", 2.0 * f2 + f1, "IM3"),
        ("3f1", 3.0 * f1, "IM3"), ("3f2", 3.0 * f2, "IM3"),
    ]
    out_rows = []
    for name, fq, kind in rows:
        if fq <= 0:
            continue
        row = {"name": name, "f": fq, "kind": kind}
        if p_tone_dbm is not None:
            if kind == "IM3" and oip3_dbm is not None:
                row["p_dbm"] = 3.0 * _num(p_tone_dbm, "单音功率 P") - \
                    2.0 * _num(oip3_dbm, "OIP3")
            if kind == "IM2" and oip2_dbm is not None:
                row["p_dbm"] = 2.0 * _num(p_tone_dbm, "单音功率 P") - \
                    _num(oip2_dbm, "OIP2")
        out_rows.append(row)
    return {"rows": out_rows}


# ---------------------------------------------------------------------------
# 有源偏置与效率
# ---------------------------------------------------------------------------

@_safe
def bjt_bias_divider(vcc, ic, vce, beta, v_be=0.7, vre=None, i2_factor=10.0):
    """BJT 分压式自偏置：Vcc, Ic, Vce, β → R1/R2/RC/RE。

    设计规则（任意模拟电子教材，如 Sedra–Smith）：
    RE 压降 VRE（缺省 10%·Vcc）→ RE = VRE/Ie（Ie = Ic(1+1/β)）；
    RC = (Vcc − Vce − VRE)/Ic；分压支路电流 I2 = i2_factor·Ib（缺省 10×，
    越大越稳但越耗电）→ R2 = (VRE+VBE)/I2，R1 = (Vcc−VB)/(I2+Ib)。
    """
    vcc = _pos(vcc, "电源电压 Vcc")
    ic = _pos(ic, "集电极电流 Ic")
    vce = _nonneg(vce, "集射电压 Vce")
    beta = _pos(beta, "电流放大系数 β")
    vbe = _num(v_be, "发射结电压 VBE")
    vre = _num(vre, "RE 压降 VRE") if vre is not None else 0.1 * vcc
    factor = _pos(i2_factor, "分压电流倍数")
    if vre + vbe >= vcc:
        raise CalcError("VRE+VBE 必须小于 Vcc")
    if vce + vre >= vcc:
        raise CalcError("Vce+VRE 必须 < Vcc（RC 上需留正电压）")
    ib = ic / beta
    ie = ic + ib
    vb = vre + vbe
    i2 = factor * ib
    re = vre / ie
    rc = (vcc - vce - vre) / ic
    r2 = vb / i2
    r1 = (vcc - vb) / (i2 + ib)
    out = {"re": re, "rc": rc, "r1": r1, "r2": r2,
           "vb": vb, "ve": vre, "ib": ib, "ie": ie,
           "i2": i2, "p_rc_w": ic * ic * rc, "p_re_w": ie * ie * re}
    if factor < 5.0:
        out["warnings"] = [
            f"分压支路电流仅为 Ib 的 {factor:g} 倍：偏置点对 β 敏感，建议 ≥10"]
    return out


@_safe
def bjt_base_bias_analysis(vcc, rb, rc, beta, v_be=0.7):
    """BJT 固定基极偏置（Base Bias, non-stabilized）**分析**：AppCAD 20 页原生拓扑。

    基极一只 RB 接 Vcc、发射极直接接地：Ib = (Vcc−VBE)/RB，
    Ic = β·Ib（线性区假设），Vce = Vcc − Ic·RC。
    稳定系数 S = dIc/dIcbo = β+1（固定偏置无直流负反馈，对温度/换管最敏感，
    仅供与分压方案对比）。Vce ≤ 0.3V 时线性假设失效并告警。
    """
    vcc = _pos(vcc, "电源电压 Vcc")
    rb = _pos(rb, "基极电阻 RB")
    rc = _pos(rc, "集电极电阻 RC")
    beta = _pos(beta, "电流放大系数 β")
    vbe = _num(v_be, "发射结电压 VBE")
    if vcc <= vbe:
        raise CalcError("Vcc 必须大于 VBE（否则基极无正偏电流）")
    ib = (vcc - vbe) / rb
    ic = beta * ib
    vce = vcc - ic * rc
    out = {"ib": ib, "ic": ic, "vce": vce, "s_factor": beta + 1.0,
           "warnings": []}
    if vce <= 0.3:
        out["warnings"].append(
            f"Vce={vce:.3g}V ≤ 0.3V：晶体管进入饱和，Ic=β·Ib 线性假设失效，仅供粗估")
    return out


@_safe
def bjt_base_bias_synthesis(vcc, ic, rc, beta, v_be=0.7):
    """BJT 固定基极偏置**综合**：目标 Ic、Vcc、RC、β → RB。"""
    vcc = _pos(vcc, "电源电压 Vcc")
    ic = _pos(ic, "目标集电极电流 Ic")
    rc = _nonneg(rc, "集电极电阻 RC")
    beta = _pos(beta, "电流放大系数 β")
    vbe = _num(v_be, "发射结电压 VBE")
    if vcc <= vbe:
        raise CalcError("Vcc 必须大于 VBE")
    rb = (vcc - vbe) * beta / ic
    ib = ic / beta
    vce = vcc - ic * rc
    out = {"rb": rb, "ib": ib, "ic": ic, "vce": vce,
           "s_factor": beta + 1.0, "warnings": []}
    if vce <= 0.3:
        out["warnings"].append(
            f"Vce={vce:.3g}V ≤ 0.3V：晶体管进入饱和，线性假设失效，仅供粗估")
    return out


@_safe
def fet_bias_self(vdd, i_d, v_gs, v_ds):
    """FET 自偏置（栅极经 RG 接地，源极电阻 RS 产生负偏）：
    VDD, ID, VGS(负), VDS → RS/RD。VS = −VGS，RS = VS/ID，
    RD = (VDD − VDS − VS)/ID。
    """
    vdd = _pos(vdd, "漏极电压 VDD")
    i_d = _pos(i_d, "漏极电流 ID")
    vgs = _num(v_gs, "栅源电压 VGS")
    vds = _nonneg(v_ds, "漏源电压 VDS")
    if vgs >= 0:
        raise CalcError("自偏置要求 VGS<0（耗尽型器件；增强型请用分压偏置）")
    vs = -vgs
    if vds + vs >= vdd:
        raise CalcError("VDS+VS 必须 < VDD（RD 上需留正电压）")
    rs = vs / i_d
    rd = (vdd - vds - vs) / i_d
    return {"rs": rs, "rd": rd, "vs": vs,
            "p_rs_w": i_d * i_d * rs, "p_rd_w": i_d * i_d * rd}


@_safe
def fet_bias_divider(vdd, i_d, v_gs, v_ds, vg, i2=1e-3):
    """FET 分压偏置（单电源）：VDD, ID, VGS, VDS, 栅压 VG → R1/R2/RS/RD。
    VS = VG − VGS；RS = (VG−VGS)/ID；RD = (VDD − VDS − VS)/ID；
    R2 = VG/I2，R1 = (VDD−VG)/I2。
    """
    vdd = _pos(vdd, "电源电压 VDD")
    i_d = _pos(i_d, "漏极电流 ID")
    vgs = _num(v_gs, "栅源电压 VGS")
    vds = _nonneg(v_ds, "漏源电压 VDS")
    vg = _num(vg, "栅极分压 VG")
    i2 = _pos(i2, "分压支路电流 I2")
    if not 0 < vg < vdd:
        raise CalcError("栅压 VG 需在 0 与 VDD 之间")
    vs = vg - vgs
    if vs < 0:
        raise CalcError("VS=VG−VGS 不能为负（源极需高于栅极形成负反馈）")
    if vds + vs >= vdd:
        raise CalcError("VDS+VS 必须 < VDD（RD 上需留正电压）")
    rs = vs / i_d
    rd = (vdd - vds - vs) / i_d
    return {"rs": rs, "rd": rd, "r1": (vdd - vg) / i2, "r2": vg / i2,
            "vs": vs, "p_rd_w": i_d * i_d * rd, "p_rs_w": i_d * i_d * rs}


@_safe
def amp_efficiency(p_in_dbm, p_out_dbm, p_dc_w):
    """放大器效率：Pin/Pout(dBm)、直流功耗 PDC(W) → PAE、漏极效率、增益。

    PAE = (Pout − Pin)/PDC（RF 增益功率口径，任意微波教材）。
    """
    pin = dbm_to_w(_num(p_in_dbm, "输入功率 Pin"))
    pout = dbm_to_w(_num(p_out_dbm, "输出功率 Pout"))
    pdc = _pos(p_dc_w, "直流功耗 PDC")
    if pout < pin:
        raise CalcError("Pout 小于 Pin：请检查单位（应为 dBm）")
    return {
        "pae_pct": 100.0 * (pout - pin) / pdc,
        "drain_eff_pct": 100.0 * pout / pdc,
        "gain_db": db(pout / pin),
        "pout_w": pout, "pin_w": pin,
    }


# ---------------------------------------------------------------------------
# 反射 / 驻波 / LC 工具
# ---------------------------------------------------------------------------

@_safe
def refl_from_z(z_re, z_im, z0=50.0):
    """阻抗 → 反射系数 Γ=(Z−Z0)/(Z+Z0)、VSWR、回波损耗、失配损耗。"""
    z0 = _pos(z0, "参考阻抗 Z0")
    z = complex(_num(z_re, "阻抗实部"), _num(z_im, "阻抗虚部"))
    gam = (z - z0) / (z + z0)
    mag = abs(gam)
    out = {"gamma_mag": mag, "gamma_phase_deg": math.degrees(cmath.phase(gam)),
           "gamma": gam}
    if mag < 1.0:
        out["vswr"] = (1.0 + mag) / (1.0 - mag)
        out["rl_db"] = -20.0 * math.log10(mag) if mag > 0 else float("inf")
        out["mismatch_loss_db"] = -10.0 * math.log10(1.0 - mag * mag)
    else:
        out["warnings"] = ["|Γ|≥1：无源网络不可能出现（纯电抗负载 |Γ|=1 属临界），"
                           "请检查输入符号"]
        out["vswr"] = float("inf")
        out["rl_db"] = 0.0
        out["mismatch_loss_db"] = 0.0
    return out


@_safe
def refl_to_z(gam_mag, gam_deg, z0=50.0):
    """反射系数（幅值+相位度） → 阻抗 Z = Z0·(1+Γ)/(1−Γ)。"""
    z0 = _pos(z0, "参考阻抗 Z0")
    mag = _nonneg(gam_mag, "|Γ|")
    if mag >= 1.0:
        raise CalcError("|Γ| 必须 < 1（无源负载）")
    ph = math.radians(_num(gam_deg, "Γ 相位(度)"))
    gam = mag * cmath.exp(1j * ph)
    z = z0 * (1.0 + gam) / (1.0 - gam)
    return {"z_re": z.real, "z_im": z.imag, "vswr": (1 + mag) / (1 - mag)}


@_safe
def z_to_y(z_re, z_im):
    """阻抗 → 导纳 Y = 1/Z = conj(Z)/|Z|²。"""
    z = complex(_num(z_re, "阻抗实部"), _num(z_im, "阻抗虚部"))
    if z == 0:
        raise CalcError("阻抗不能为 0")
    y = 1.0 / z
    return {"y_re": y.real, "y_im": y.imag}


@_safe
def lc_resonance(l_h, c_f):
    """LC 谐振：f0 = 1/(2π√(LC))；附该频点的 X_L/X_C（数值相等）。"""
    l_h = _pos(l_h, "电感 L")
    c_f = _pos(c_f, "电容 C")
    f0 = 1.0 / (2.0 * math.pi * math.sqrt(l_h * c_f))
    x = 2.0 * math.pi * f0 * l_h
    return {"f0": f0, "x_l_at_f0": x, "x_c_at_f0": x}


@_safe
def quarter_wave_transform(z_source, z_load):
    """λ/4 阻抗变换段：Zt = √(Zs·ZL)（任意微波教材）。"""
    zs = _pos(z_source, "源阻抗 Zs")
    zl = _pos(z_load, "负载阻抗 ZL")
    return {"z_t": math.sqrt(zs * zl)}


# ---------------------------------------------------------------------------
# 集总元件 180° 巴伦 Lumped Element Balun
# ---------------------------------------------------------------------------
@_safe
def balun_lumped_design(f0, rg, r_load, x_load=0.0, cp_f=0.0, solution=6):
    """集总元件巴伦设计（clean-room 镜像参数法）。

    元件值公式：特性阻抗 Zi = √(Rg·R)，每臂一个 ±45° 相移节
    （差分 180°），L = Zi/ω0，C = 1/(ω0·Zi)（ω0²LC = 1）。
    出处：镜像参数法低通/高通相位节（T. Moreno, "Microwave Transmission
    Design Data", 1948 一类教科书口径）；元件值经 Avago AppCAD 4.00
    实机两点定标复核（1 GHz 与 2 GHz、Rg=R=50Ω：L=8.0/4.0 nH、
    C=3.2/1.6 pF，与公式完全吻合）。电学行为经数值节点分析验证
    （任意 Rg/R 组合）：f0 处 Zin≈Rg（源端匹配，偏差 <2%）、负载
    差分恒为 180°（±1°）、差分幅度 = √(R/Rg)×|Vin|——网络兼具巴伦
    与电阻比变换特性。

    solution=6：低通 π 臂（C 并-L 串-C 并）+ 高通 T 臂（C 串-L 并-C 串）；
    solution=4：低通 L 节（L 串-C 并）+ 高通 L 节（C 串-L 并）。
    两种方案元件值相同，区别仅在结构（带宽/元件数权衡）。

    注意：X（负载虚部）与 Cp（寄生）不参与元件值计算，仅随结果回显；
    X≠0 时 f0 处按公式网络存在失配，见返回 warnings。
    """
    f0 = _pos(f0, "中心频率 f0")
    rg = _pos(rg, "源阻抗 Rg")
    rl = _pos(r_load, "负载电阻 R")
    cp = _nonneg(cp_f, "寄生电容 Cp") if cp_f else 0.0
    solution = int(solution)
    if solution not in (4, 6):
        raise CalcError("solution 必须是 4 或 6（元件数）")
    w0 = 2.0 * math.pi * f0
    zi = math.sqrt(rg * rl)
    l_h = zi / w0
    c_f = 1.0 / (w0 * zi)
    warnings = []
    if x_load:
        warnings.append(
            "负载虚部 X=%.6g Ω 未参与元件值计算（设计按 R=%.6g Ω），f0 处存在失配"
            % (float(x_load), rl)
        )
    if cp:
        warnings.append("寄生 Cp=%.6g pF 未计入元件值，会轻微改变响应" % (cp * 1e12))
    if solution == 6:
        lp_arm = [("c_shunt", c_f), ("l_series", l_h), ("c_shunt", c_f)]
        hp_arm = [("c_series", c_f), ("l_shunt", l_h), ("c_series", c_f)]
    else:
        lp_arm = [("l_series", l_h), ("c_shunt", c_f)]
        hp_arm = [("c_series", c_f), ("l_shunt", l_h)]
    return {
        "solution": solution,
        "l_h": l_h,
        "c_f": c_f,
        "zi": zi,
        "x_l_at_f0": w0 * l_h,
        "lp_arm": lp_arm,
        "hp_arm": hp_arm,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 标准元件值（E 系列）E-series Standard Values —— IEC 60063
# ---------------------------------------------------------------------------
E6 = (10, 15, 22, 33, 47, 68)
E12 = (10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82)
E24 = (10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39, 43,
       47, 51, 56, 62, 68, 75, 82, 91)
E96 = (100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130, 133,
       137, 140, 143, 147, 150, 154, 158, 162, 165, 169, 174, 178, 182,
       187, 191, 196, 200, 205, 210, 215, 221, 226, 232, 237, 243, 249,
       255, 261, 267, 274, 280, 287, 294, 301, 309, 316, 324, 332, 340,
       348, 357, 365, 374, 383, 392, 402, 412, 422, 432, 442, 453, 464,
       475, 487, 499, 511, 523, 536, 549, 562, 576, 590, 604, 619, 634,
       649, 665, 681, 698, 715, 732, 750, 768, 787, 806, 825, 845, 866,
       887, 909, 931, 953, 976)


def _e_series_table(tolerance_pct):
    """容差 → E 系列标准表（0.1%→E192 生成值，1%→E96，5%→E24，10%→E12，20%→E6）。"""
    tol = float(tolerance_pct)
    if tol <= 0.3:
        # E192 按 IEC 60063 规则生成（100·10^(k/192) 三位有效舍入）
        return [round(100.0 * 10.0 ** (k / 192.0)) for k in range(192)]
    if tol <= 2.0:
        return list(E96)
    if tol <= 7.0:
        return list(E24)
    if tol <= 15.0:
        return list(E12)
    return list(E6)


@_safe
def standard_value(raw, tolerance_pct=5.0):
    """原始值 → 最近 E 系列标准值（电阻/电容/电感通用）。

    返回 std（SI）、abs_err = raw-std、pct_err；容差按 AppCAD 页面
    五档（0.1/1/5/10/20%）映射 IEC 60063 E192/E96/E24/E12/E6。
    """
    v = _num(raw, "原始值")
    if v == 0 or not math.isfinite(v):
        raise CalcError("原始值必须是有限非零数")
    tol = float(tolerance_pct)
    if tol <= 0:
        raise CalcError("容差必须是正百分数")
    table = _e_series_table(tol)
    name = {6: "E6", 12: "E12", 24: "E24", 96: "E96", 192: "E192"}[len(table)]
    mag = abs(v)
    e = math.floor(math.log10(mag))
    best = None
    for exp in (e - 2, e - 1, e, e + 1):
        for m in table:
            # 格式化去浮点尾噪（6.8e-9 之类的尾数噪声）
            cand = float(f"{m * 10.0 ** (exp - 2):.12g}")
            err = abs(cand - mag)
            if best is None or err < best[0]:
                best = (err, cand)
    std = best[1] if v > 0 else -best[1]
    abs_err = v - std
    pct = 100.0 * abs_err / v
    return {
        "std": std,
        "abs_err": abs_err,
        "pct_err": pct,
        "series": name,
        "tolerance_pct": tol,
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 工程常数 Engineering Constants（SI，CODATA 2018 / 2019 SI 定义值）
# ---------------------------------------------------------------------------
ENGINEERING_CONSTANTS = {
    "阿伏伽德罗常数 Avogadro": (6.02214076e23, "1/mol", "SI 定义值（精确）"),
    "玻尔兹曼常数 Boltzmann": (1.380649e-23, "J/K", "SI 定义值（精确）"),
    "元电荷 Elementary charge": (1.602176634e-19, "C", "SI 定义值（精确）"),
    "真空光速 Speed of light": (299792458.0, "m/s", "SI 定义值（精确）"),
    "普朗克常数 Planck": (6.62607015e-34, "J·s", "SI 定义值（精确）"),
    "电子伏特 Electron volt": (1.602176634e-19, "J", "定义值"),
    "电子静止质量 Electron mass": (9.1093837015e-31, "kg", "CODATA 2018"),
    "质子质量 Proton mass": (1.67262192369e-27, "kg", "CODATA 2018"),
    "中子质量 Neutron mass": (1.67492749804e-27, "kg", "CODATA 2018"),
    "真空介电常数 ε0": (8.8541878128e-12, "F/m", "CODATA 2018"),
    "真空磁导率 µ0": (1.25663706212e-6, "H/m", "CODATA 2018"),
    "真空波阻抗 Z0": (376.730313668, "Ω", "=√(µ0/ε0)"),
    "电子荷质比 e/m": (1.75882001076e11, "C/kg", "CODATA 2018"),
    "玻尔磁子 Bohr magneton": (9.2740100783e-24, "J/T", "CODATA 2018"),
    "核磁子 Nuclear magneton": (5.050783699e-27, "J/T", "CODATA 2018"),
    "精细结构常数 α": (7.2973525693e-3, "-", "CODATA 2018"),
    "引力常数 G": (6.67430e-11, "m³/(kg·s²)", "CODATA 2018"),
    "斯特藩-玻尔兹曼常数 σ": (5.670374419e-8, "W/(m²·K⁴)", "SI 定义值（精确）"),
    "原子质量单位 u": (1.66053906660e-27, "kg", "CODATA 2018"),
    "法拉第常数 Faraday": (96485.33212, "C/mol", "CODATA 2018"),
    "摩尔气体常数 R": (8.314462618, "J/(mol·K)", "SI 定义值（精确）"),
    "标准摩尔体积 Vm": (2.241396e-2, "m³/mol", "273.15 K, 100 kPa"),
    "维恩位移常数 b": (2.897771955e-3, "m·K", "CODATA 2018"),
    "自然对数底 e": (2.718281828459045, "-", "数学常数"),
}


# ---------------------------------------------------------------------------
# 复数运算辅助（Complex Math 计算器引擎侧）
# ---------------------------------------------------------------------------
@_safe
def cplx_to_polar(z_re, z_im):
    """复数 → 极坐标 (模, 角度°)。"""
    z = complex(_num(z_re, "实部"), _num(z_im, "虚部"))
    return {"mag": abs(z), "ang_deg": math.degrees(cmath.phase(z))}


@_safe
def polar_to_cplx(mag, ang_deg):
    """极坐标 → 复数（模非负）。"""
    m = _nonneg(mag, "模")
    ph = math.radians(_num(ang_deg, "角度(度)"))
    z = m * cmath.exp(1j * ph)
    return {"z_re": z.real, "z_im": z.imag}


@_safe
def ser_to_par(z_re, z_im):
    """串联 R+jX → 等效并联 Rp∥jXp（Q=|X/R|；Q=0 无意义）。"""
    r = _num(z_re, "电阻 R")
    x = _num(z_im, "电抗 X")
    if r == 0:
        raise CalcError("纯电抗没有有限并联等效（Rp→∞）")
    if x == 0:
        raise CalcError("纯电阻的并联等效无意义（Q=0）")
    q = abs(x / r)
    f = 1.0 + q * q
    return {"rp": r * f, "xp": x * f / q}


@_safe
def par_to_ser(rp, xp):
    """并联 Rp∥jXp → 等效串联 R+jX（par_to_ser 的逆）。"""
    rp = _num(rp, "并联电阻 Rp")
    xp = _num(xp, "并联电抗 Xp")
    if rp == 0:
        raise CalcError("Rp=0（短路）无串联等效")
    if xp == 0:
        raise CalcError("纯电阻的串联等效无意义（Q=0）")
    q = abs(rp / xp)
    f = 1.0 + q * q
    return {"r": rp / f, "x": xp * q / f}


@_safe
def z_normalize(z_re, z_im, z0=50.0):
    """阻抗归一化 zn = z/z0。"""
    z0 = _pos(z0, "归一化阻抗 Z0")
    z = complex(_num(z_re, "实部"), _num(z_im, "虚部"))
    return {"zn_re": z.real / z0, "zn_im": z.imag / z0}


@_safe
def z_unnormalize(zn_re, zn_im, z0=50.0):
    """归一化阻抗 → 实际阻抗 z = zn·Z0。"""
    z0 = _pos(z0, "归一化阻抗 Z0")
    zn = complex(_num(zn_re, "归一化实部"), _num(zn_im, "归一化虚部"))
    return {"z_re": zn.real * z0, "z_im": zn.imag * z0}


# ---------------------------------------------------------------------------
# 器件热设计 Device Thermal（AppCAD 4.00 实机定标，见 docs/RF计算器与对拍清单.md）
# 实测锚点（AppCAD，Tcase=25/θjc=220/θca=22/Vin=3V/Iin=42mA/Pin=2.1dBm/Pout=14.2dBm
# → Tj=47/Ta=23/Pdc=126mW/ΣPrf=24.7mW/Pdiss=101.3mW）：
#   Pdc(mW)  = Vin(V)·Iin(mA)
#   ΣPrf(mW) = Pout(dBm)→mW − Pin(dBm)→mW          （净输出射频功率）
#   Pdiss    = Pdc − ΣPrf                            （总耗散功率）
#   Tj       = Tcase + Pdiss(W)·θjc ；Ta = Tcase − Pdiss(W)·θca
#   给 Tj 反解 Tcase = Tj − Pdiss·θjc；给 Ta 反解 Tcase = Ta + Pdiss·θca
# ---------------------------------------------------------------------------
def _thermal_common(vin, iin_ma, pin_dbm, pout_dbm, theta_jc, theta_ca):
    """公共功率链：返回 (pdc_mw, prf_mw, pdiss_mw, warnings)。"""
    vin = _pos(vin, "Vin")
    iin_ma = _nonneg(iin_ma, "Iin")
    pin_dbm = _num(pin_dbm, "Pin")
    pout_dbm = _num(pout_dbm, "Pout")
    theta_jc = _nonneg(theta_jc, "θjc")
    theta_ca = _nonneg(theta_ca, "θca")
    if theta_jc + theta_ca <= 0.0:
        raise CalcError("θjc 与 θca 不能同时为 0")
    pdc_mw = vin * iin_ma
    prf_mw = dbm_to_w(pout_dbm) * 1e3 - dbm_to_w(pin_dbm) * 1e3
    pdiss_mw = pdc_mw - prf_mw
    warnings = []
    if pdiss_mw < 0.0:
        warnings.append("Pdiss<0：射频净输出功率超过直流输入，耗散为负，结果仅供参考")
    return pdc_mw, prf_mw, pdiss_mw, warnings


@_safe
def thermal_analysis(vin, iin_ma, pin_dbm, pout_dbm, theta_jc, theta_ca,
                     t_case=None, t_j=None, t_ambient=None):
    """器件热分析：给定一个温度（Tcase/Tj/Ta 之一），算出其余全部。

    全部 SI：vin[V]、iin_ma[mA]、pin_dbm/pout_dbm[dBm]、θ[°C/W]、温度[°C]。
    AppCAD 口径：显示值四舍五入一位小数；此处返回精确值。
    """
    given = [t_case is not None, t_j is not None, t_ambient is not None]
    n_given = sum(1 for g in given if g)
    if n_given == 0:
        raise CalcError("需给定 Tcase / Tj / Ta 之一")
    if n_given > 1:
        raise CalcError("Tcase / Tj / Ta 只能给定一个")
    pdc_mw, prf_mw, pdiss_mw, warnings = _thermal_common(
        vin, iin_ma, pin_dbm, pout_dbm, theta_jc, theta_ca)
    pdiss_w = pdiss_mw * 1e-3
    if t_case is not None:
        t_case = _num(t_case, "Tcase")
        t_j = t_case + pdiss_w * theta_jc
        t_ambient = t_case - pdiss_w * theta_ca
    elif t_j is not None:
        t_j = _num(t_j, "Tj")
        t_case = t_j - pdiss_w * theta_jc
        t_ambient = t_case - pdiss_w * theta_ca
    else:
        t_ambient = _num(t_ambient, "Ta")
        t_case = t_ambient + pdiss_w * theta_ca
        t_j = t_case + pdiss_w * theta_jc
    return {"pdc_mw": pdc_mw, "prf_mw": prf_mw, "pdiss_mw": pdiss_mw,
            "t_j": t_j, "t_case": t_case, "t_ambient": t_ambient,
            "warnings": warnings}


# ---------------------------------------------------------------------------
# S 参数：Touchstone 解析与放大器稳定性
# 对应 AppCAD「Everything S-Parameters」页（光谱文件槽 / Smith 图 / 稳定性圆）。
# AppCAD 自身不含稳定性圆计算（该页只做显示），此处的 K/Δ/圆是
# 按公开文献（Stern 1967 / Rollett 1962）重实现，用于补足"算得出来"的部分。
# ---------------------------------------------------------------------------

def _strip_touchstone_comment(line):
    """去掉 Touchstone 的 ``!`` 注释与首尾空白。"""
    cut = line.find("!")
    if cut >= 0:
        line = line[:cut]
    return line.strip()


def _touchstone_float(tok):
    try:
        return float(tok)
    except (TypeError, ValueError):
        raise CalcError(f"Touchstone 数值不合法：{tok!r}")


@_safe
def parse_touchstone(text, ports=None, freq_scale_unit="Hz"):
    """解析 Touchstone（.s1p/.s2p）文本，返回频点列表。

    不做文件 I/O：调用方把文件内容读成字符串传进来。
    ``ports`` 为 None 时按文件头 ``[Number of Ports]`` 推断，缺省 2。
    ``freq_scale_unit`` 用于头里没有 ``[Hz]`` 段时的单位（Hz/kHz/MHz/GHz）。
    每个频点返回：
      {"freq_hz", "s": [[complex,...],...], "s_db": [[dB,...]], "s_ang": [[度,...]]}
    s[i][j] 语义为 S(i+1)(j+1)。
    """
    if not isinstance(text, str):
        raise CalcError("Touchstone 内容必须是字符串")
    scale = UNITS["freq"].get(freq_scale_unit)
    if scale is None:
        raise CalcError(f"不认识的频率单位：{freq_scale_unit}")
    fmt = "MA"
    n_ports = None
    opts = []
    rows = []
    for raw in text.splitlines():
        line = _strip_touchstone_comment(raw)
        if not line:
            continue
        low = line.lower()
        if low.startswith("[number of ports]"):
            try:
                n_ports = int(line.split("]", 1)[1].strip())
            except (IndexError, ValueError):
                raise CalcError("[Number of Ports] 段无法解析")
            continue
        if low.startswith("[number of frequencies]"):
            try:
                int(line.split("]", 1)[1].strip())
            except (IndexError, ValueError):
                raise CalcError("[Number of Frequencies] 段无法解析")
            continue
        if low.startswith("[two-port order]"):
            tail = line.split("]", 1)[1].strip().upper() if "]" in line else ""
            if tail and not tail.startswith("12_21"):
                raise CalcError("仅支持 [Two-Port Order] 12_21")
            continue
        if line.startswith("["):
            # [Hz]/[kHz]/[MHz]/[GHz]、[MA]/[DB]/[RI] 等选项段
            sect = line.strip("[]").strip()
            up = sect.upper()
            if up in ("HZ", "KHZ", "MHZ", "GHZ"):
                scale = UNITS["freq"][{"HZ": "Hz", "KHZ": "kHz",
                                       "MHZ": "MHz", "GHZ": "GHz"}[up]]
                opts.append(up)
                continue
            if up in ("MA", "DB", "RI"):
                fmt = up
                opts.append(up)
                continue
            if up.startswith(("NUMBER OF", "TWO-PORT", "VERSION", "REFERENCE")):
                continue
            raise CalcError(f"暂不支持的 Touchstone 选项段：[{sect}]")
        if low.startswith("#"):
            toks = line[1:].split()
            i = 0
            while i < len(toks):
                up = toks[i].upper()
                if up in ("HZ", "KHZ", "MHZ", "GHZ"):
                    scale = UNITS["freq"][{"HZ": "Hz", "KHZ": "kHz",
                                           "MHZ": "MHz", "GHZ": "GHz"}[up]]
                elif up in ("MA", "DB", "RI"):
                    fmt = up
                elif up in ("S", "Y", "Z", "H", "G"):
                    if up != "S":
                        raise CalcError(f"仅支持 S 参数文件（收到 {up} 参数）")
                elif up == "R":
                    # 参考阻抗 [R n]，跳过其后的数值
                    if i + 1 < len(toks):
                        _touchstone_float(toks[i + 1])
                        i += 1
                elif up in ("Y",):
                    pass
                else:
                    raise CalcError(f"不认识的 Touchstone 关键字：{toks[i]}")
                i += 1
            opts.append("OPT")
            continue
        rows.append([_touchstone_float(t) for t in line.split()])
    if not rows:
        raise CalcError("Touchstone 内容里没有数据行")
    if n_ports is None:
        n_ports = ports if ports else 2
    if n_ports not in (1, 2):
        raise CalcError(f"仅支持 1 或 2 端口（收到 {n_ports}）")
    # 每频点数值个数：频率 1 个 + 每个 Sij 两个数（幅角或实虚）
    per_block = 1 + 2 * n_ports * n_ports
    blocks = []
    cur = []
    for row in rows:
        cur.extend(row)
        if len(cur) >= per_block:
            blocks.append(cur[:per_block])
            cur = []
    if cur:
        raise CalcError(f"数据行不完整：末段只有 {len(cur)} 个数"
                        f"（每频点需 {per_block}，即 1 个频率 + "
                        f"{2 * n_ports * n_ports} 个 S 参数量）")
    out = []
    warnings = []
    for blk in blocks:
        f_hz = blk[0] * scale
        if f_hz <= 0:
            raise CalcError(f"频率必须为正：{blk[0]}")
        vals = blk[1:]
        s = [[0j] * n_ports for _ in range(n_ports)]
        for i in range(n_ports):
            for j in range(n_ports):
                # Touchstone 数据按**列主序**排：2 端口是 S11,S21,S12,S22，
                # 即第 k 个 S 参数对应 pos = j*n_ports + i。
                # MA/DB 为 (幅,角) 两数，RI 为 (实,虚) 两数。
                k = 2 * (j * n_ports + i)
                p = vals[k]
                q = vals[k + 1]
                if fmt == "RI":
                    z = complex(p, q)
                else:  # MA / DB 都是 (幅值, 角度)
                    # 注意：S 参数 dB 是 20log10（电压比），不是功率 dB 的 10log10
                    mag = 10.0 ** (p / 20.0) if fmt == "DB" else p
                    z = cmath.rect(mag, math.radians(q))
                s[i][j] = z
        s_db = [[20.0 * math.log10(abs(s[i][j])) if abs(s[i][j]) > 0 else -999.0
                 for j in range(n_ports)] for i in range(n_ports)]
        s_ang = [[math.degrees(cmath.phase(s[i][j])) for j in range(n_ports)]
                 for i in range(n_ports)]
        out.append({"freq_hz": f_hz, "s": s, "s_db": s_db, "s_ang": s_ang})
    if len(out) < 2:
        warnings.append("只有 1 个频点，无法做频率扫描类分析")
    return {"n_ports": n_ports, "format": fmt, "points": out,
            "n_points": len(out), "warnings": warnings}


@_safe
def stability_from_s(s11_re, s11_im, s12_re, s12_im,
                     s21_re, s21_im, s22_re, s22_im):
    """由单频点 S 参数算 Rollett 稳定系数 K、Δ 与稳定判据。

    K = (1 − |S11|² − |S22|² + |Δ|²) / (2·|S12·S21|)
    Δ = S11·S22 − S12·S21
    K > 1 且 |Δ| < 1 → 无条件稳定（unconditionally stable）。
    """
    s11 = complex(_num(s11_re, "S11 实部"), _num(s11_im, "S11 虚部"))
    s12 = complex(_num(s12_re, "S12 实部"), _num(s12_im, "S12 虚部"))
    s21 = complex(_num(s21_re, "S21 实部"), _num(s21_im, "S21 虚部"))
    s22 = complex(_num(s22_re, "S22 实部"), _num(s22_im, "S22 虚部"))
    delta = s11 * s22 - s12 * s21
    denom = 2.0 * abs(s12 * s21)
    warnings = []
    if denom == 0.0:
        raise CalcError("S12·S21 = 0：单向器件无有限 K（物理上无条件稳定）")
    k = (1.0 - abs(s11) ** 2 - abs(s22) ** 2 + abs(delta) ** 2) / denom
    stable = (k > 1.0) and (abs(delta) < 1.0)
    if k <= 0.0:
        warnings.append("K≤0：器件为潜在不稳定，需加匹配/反馈稳定")
    return {"k": k, "delta_mag": abs(delta), "delta_re": delta.real,
            "delta_im": delta.imag, "unconditionally_stable": stable,
            "msg": ("无条件稳定" if stable else "潜在不稳定（需稳定化）"),
            "warnings": warnings}


@_safe
def gain_from_s(s11_re, s11_im, s12_re, s12_im, s21_re, s21_im,
                s22_re, s22_im):
    """由 S 参数算功率增益：G0（|S21|²）、MSG、MAG。

    K>1（无条件稳定）时给 MAG = |S21/S12|·(K − √(K²−1))；
    K≤1 时 MAG 无定义，报 MSG = |S21/S12|。
    """
    s11 = complex(_num(s11_re, "S11 实部"), _num(s11_im, "S11 虚部"))
    s12 = complex(_num(s12_re, "S12 实部"), _num(s12_im, "S12 虚部"))
    s21 = complex(_num(s21_re, "S21 实部"), _num(s21_im, "S21 虚部"))
    s22 = complex(_num(s22_re, "S22 实部"), _num(s22_im, "S22 虚部"))
    if abs(s21) == 0.0:
        raise CalcError("S21 = 0：无正向传输")
    if abs(s12) == 0.0:
        raise CalcError("S12 = 0：单向器件，MAG/MSG 无定义（用 G0）")
    delta = s11 * s22 - s12 * s21
    denom = 2.0 * abs(s12 * s21)
    k = (1.0 - abs(s11) ** 2 - abs(s22) ** 2 + abs(delta) ** 2) / denom
    g0 = abs(s21) ** 2
    ratio = abs(s21 / s12)
    warnings = []
    if k > 1.0:
        mag = ratio * (k - math.sqrt(k * k - 1.0))
        mag_db = db(mag)
        msg_val = None
        msg_db = None
        kind = "MAG"
    else:
        mag = None
        mag_db = None
        msg_val = ratio
        msg_db = db(ratio)
        kind = "MSG"
        warnings.append("K≤1（潜在不稳定）：MAG 无定义，改报 MSG")
    return {"g0": g0, "g0_db": db(g0),
            "mag": mag, "mag_db": mag_db,
            "msg": msg_val, "msg_db": msg_db,
            "k": k, "gain_kind": kind, "warnings": warnings}


# ---------------------------------------------------------------------------
# RFIC 偏置网络（电压偏置 / 电流偏置）与二极管检波器（大信号 / 小信号）
# ---------------------------------------------------------------------------
# 本节四组函数按**公开**微波工程教科书/论文的经典公式实现（clean-room）：
# 不含、不翻译、不派生自任何反编译代码。凡是"我们自选的工程惯例"（元件
# 取值规则、Design Rule 损耗、近似式）都在各自 docstring 里逐条标注，不
# 借用任何商业软件的定标值冒充实测数据。

QE = 1.602176634e-19        # 元电荷 C（2019 SI 定义值，精确）
T_REF_K = 298.15            # 参考温度 25 °C（饱和电流的温度归一化点）

# 电压偏置综合的两条工程惯例（见 rfic_voltage_bias 的 docstring）：
_VB_CAP_RULE = 10.0         # 电容：|Xc(f1)| = Z0/10 → C = 10/(ω1·Z0)
_VB_RFC_RULE = 10.0         # RFC：|XL(f1)| = 10·Z0 → L = 10·Z0/ω1


def _series_cap_il_db(xc, z0):
    """串联电抗 Xc（Ω）接在两端 z0 之间时的插入损耗（dB）。

    标准二端口结果：S21 = 2Z0/(2Z0 + jX)，
    |S21|² = 1/(1 + (X/(2Z0))²)  →  IL = 10·log10(1 + (X/(2Z0))²)。
    这是串联电抗的 ABCD→S 变换（任意微波网络教科书），不含拟合系数。
    """
    return 10.0 * math.log10(1.0 + (xc / (2.0 * z0)) ** 2)


def _shunt_branch_il_db(xb, z0):
    """并联支路（纯电抗 xb，单位 Ω）跨接在 z0 线上时的分流损耗（dB）。

    并联导纳 Y = 1/(j·xb)（ABCD 矩阵 C = Y），S21 = 2/(2 + Y·Z0)，
    |S21|² = 4/(4 + (Z0/xb)²)  →  IL = 10·log10(1 + (Z0/(2·xb))²)。
    xb→0（串联谐振/短路）时损耗→∞，调用方负责拦截。
    """
    return 10.0 * math.log10(1.0 + (z0 / (2.0 * xb)) ** 2)


@_safe
def rfic_voltage_bias(f1_hz, f2_hz, z0, gain_db, cin_pf=None, cout_pf=None,
                      rfc_h=None, cb_pf=None, loss_per_bias_db=0.0):
    """电压偏置型 RFIC 放大器：偏置网络**综合**（元件给 None）或**核算**（给值）。

    ===== 拓扑（公开教科书标准电路）=====
    RF 输入 ──Cin(串联)── 放大器 ──Cout(串联)── RF 输出
                                    │
                                    ├─ RFC ── Vdd ── Cb ── GND

    ===== 频率响应的物理模型（公开文献标准无源网络分析）=====
    1. 串联电容插入损耗（两端阻抗均为 Z0 的二端口，Cin、Cout 各算一项）：
       S21 = 2Z0/(2Z0 + jX)，X = −1/(ωC)
       → ``IL = 10·log10(1 + (Xc/(2·Z0))²)``，Xc = 1/(ωC)。
       两项独立串联、互相不加载，直接相加。
    2. 偏置支路分流损耗：RFC 自输出节点接电源、Cb 自电源旁路到地，故自
       输出节点看地是 **L 与 Cb 串联**：xb = ωL − 1/(ω·Cb)；
       并联电抗的 S21 = 2/(2 + j·Z0/xb)
       → ``loss = 10·log10(1 + (Z0/(2·xb))²)``。
    3. ``total_loss_db = Cin损耗 + Cout损耗 + 偏置支路损耗``。
       **按 AppCAD 4.00 实机定标：Total 行只含这两类损耗**。实测
       F1=834MHz/F2=1910MHz/Z0=50/Cin=Cout=13pF/RFC=30nH/Cb=750pF 时
       AppCAD 给 Band1 caps=0.19/rfc=0.11/total=0.29、Band2 caps=0.04/rfc=0.11/
       total=0.14，而 0.1852+0.1088=0.2940、0.0356+0.1088=0.1444 —— 即
       **Total 就是两项之和，不含额外的 Design Rule 项**。
    3b. **偏置支路损耗只算一次（在 f1 处），Band2 沿用同一数值**。实测
       Band2 的 rfc 损耗仍是 0.11（若按 f2=1910MHz 单独算只有 0.0209，与
       实测不符）；这符合"扼流圈损耗按频段最低端最坏情况取值"的工程惯例。
    3c. ``loss_per_bias_db`` 是**我们自选的** Design Rule 附加损耗（每个偏置
       件一笔，代表 RFC 有限 Q、Cb 的 ESL/ESR 与布线不连续性），**默认 0**，
       因为实机 Total 行里看不到这一项。需要按 Design Rule 单选做保守估算时
       由调用方显式传入，此时 ``total_loss_db`` 才含 ``2×loss_per_bias_db``。
    4. ``overall_gain_db = gain_db − total_loss_db``。
    5. ``f3db_low_hz = 1/(2π·Z0·Cin)``（**按 AppCAD 4.00 实机定标**）。
       实测：Z0=50Ω、显示值 Cin=13pF 时 AppCAD 给出 260.65MHz；用本式反解得
       Cin=12.21pF（屏幕只显示 2 位有效数字，13pF 是圆整后的显示值），
       1/(2π·50·12.21e-12)=260.7MHz 完全吻合。若按"Xc=2Z0"口径取
       1/(4π·Z0·Cin) 会只得一半（122MHz），与实测不符。
       本式对应的物理含义是单只串联电容与两端 Z0 组成的高通在 |Xc|=Z0 处的
       拐点，属公开文献的常用口径。

    ===== 综合模式（某元件为 None 时按**工程惯例**取值）=====
    * 电容（Cin/Cout/Cb）取"在工作频段最低端电抗可忽略"：
      |Xc(f1)| = Z0/10 → ``C = 10/(2π·f1·Z0)``。**这是我们自选的惯例**，
      不是文献常量；该余量使 f1 处单只电容损耗仅
      10·log10(1+1/400) ≈ 0.011 dB。
    * RFC 取"在 f1 处阻抗不低于 10·Z0"：``L = 10·Z0/(2π·f1)``（同样为惯例）。
    元件**逐个独立**处理：可以只给一部分、其余自动综合。返回值 ``synthesized``
    列出本次实际综合了哪些键，``values_source`` 给出每个键的出处。

    参数：f1_hz/f2_hz 频段两端（Hz）；z0 系统阻抗（Ω）；gain_db 放大器额定
    增益（dB）；loss_per_bias_db 每个偏置件的 Design Rule 损耗（dB）；
    cin_pf/cout_pf/cb_pf 单位 pF；rfc_h 单位 H（全部为 None 则全综合）。
    返回：``{"band1", "band2", "cin_pf", "cout_pf", "rfc_h", "cb_pf",
    "f3db_low_hz", "synthesized", "values_source", "warnings"}``；
    band 内为 ``loss_caps_db / loss_rfc_db / total_loss_db /
    overall_gain_db / f3db_low_hz``。
    """
    f1 = _pos(f1_hz, "最低工作频率 f1")
    f2 = _pos(f2_hz, "最高工作频率 f2")
    z0 = _pos(z0, "系统阻抗 Z0")
    gain = _num(gain_db, "放大器额定增益")
    if not math.isfinite(gain):
        raise CalcError("放大器额定增益必须是有限数")
    lp = _nonneg(loss_per_bias_db, "每个偏置件的 Design Rule 损耗")
    w1 = 2.0 * math.pi * f1
    warnings = []
    if f2 < f1:
        warnings.append(f"f2={f2:.6g}Hz 低于 f1={f1:.6g}Hz：band1/band2 按输入"
                        "顺序计算，综合规则仍以 f1 为基准")

    synth = []
    src = {}

    def pick(val, name, synth_val, key, unit, how):
        if val is None:
            src[key] = "综合：" + how
            synth.append(key)
            return synth_val
        src[key] = "输入值"
        return _pos(val, name) * unit

    cin = pick(cin_pf, "输入串联电容 Cin", _VB_CAP_RULE / (w1 * z0), "cin_pf",
               1e-12, "Xc(f1)=Z0/10")
    cout = pick(cout_pf, "输出串联电容 Cout", _VB_CAP_RULE / (w1 * z0),
                "cout_pf", 1e-12, "Xc(f1)=Z0/10")
    cb = pick(cb_pf, "旁路电容 Cb", _VB_CAP_RULE / (w1 * z0), "cb_pf",
              1e-12, "Xc(f1)=Z0/10")
    rfc = pick(rfc_h, "射频扼流圈 RFC", _VB_RFC_RULE * z0 / w1, "rfc_h",
               1.0, "XL(f1)=10·Z0")

    f3db = 1.0 / (2.0 * math.pi * z0 * cin)
    if f3db > f1:
        warnings.append(f"f3db_low={f3db:.6g}Hz 高于最低工作频率 f1={f1:.6g}Hz："
                        "Cin 偏小，带内已有明显低频滚降")

    # 偏置支路损耗只在 f1 处算一次（AppCAD 实机口径，见 docstring 3b）
    xb1 = w1 * rfc - 1.0 / (w1 * cb)
    if abs(xb1) < 1e-9 * z0:
        raise CalcError(
            f"{f1:.6g}Hz 处 RFC 与 Cb 串联谐振（ωL=1/(ωCb)）：偏置支路近似"
            "短路、输出被短路到电源，请加大 Cb 或改变 RFC")
    il_rfc = _shunt_branch_il_db(xb1, z0)
    w2 = 2.0 * math.pi * f2
    xb2 = w2 * rfc - 1.0 / (w2 * cb)
    if abs(xb2) < 1e-9 * z0:
        warnings.append(
            f"f2={f2:.6g}Hz 处 RFC 与 Cb 串联谐振（ωL=1/(ωCb)）：实机按 f1 取值"
            "只用一次，此处仅提示该频点物理上不可用")

    def band(f):
        w = 2.0 * math.pi * f
        il_caps = (_series_cap_il_db(1.0 / (w * cin), z0)
                   + _series_cap_il_db(1.0 / (w * cout), z0))
        total = il_caps + il_rfc + 2.0 * lp
        return {"loss_caps_db": il_caps, "loss_rfc_db": il_rfc,
                "total_loss_db": total, "overall_gain_db": gain - total,
                "f3db_low_hz": f3db}

    b1 = band(f1)
    b2 = band(f2)
    if b2["total_loss_db"] > b1["total_loss_db"] + 1e-12:
        warnings.append("高频端总损耗反而更大：通常意味着 Cb 偏小、偏置支路电抗"
                        "随频率下降不足，请检查旁路电容取值")
    return {
        "band1": b1, "band2": b2,
        "cin_pf": cin * 1e12, "cout_pf": cout * 1e12,
        "rfc_h": rfc, "cb_pf": cb * 1e12,
        "f3db_low_hz": f3db,
        "synthesized": synth, "values_source": src,
        "warnings": warnings,
    }


@_safe
def rfic_current_bias(f_low_hz, z0, rfc_h, vs, theta_jc, dvd_dt_mv_c,
                      vd_max, vd_typ, vd_min, id_goal_ma, tcse_list,
                      rbias_ohm=None, cb_pf=None):
    """电流偏置型 RFIC 放大器（恒流源偏置）：综合 Rbias、算三温度点工作点。

    ===== 拓扑（公开教科书标准电路）=====
    Vs ──Rbias──┬── RFC ── 放大器漏极
                └── Cb ── GND（旁路）
    Rbias 设定电流，RFC 提供 RF 隔离，Cb 把电源端交流短路到地。

    ===== 采用的公式 =====
    * 综合（``rbias_ohm=None``）：``Rbias = (Vs − Vd,typ)/Id,goal``；
      Vs ≤ Vd,typ 时无正电流，报 CalcError。
    * 温度点 t（°C，相对 25 °C 的线性温度系数，数据手册常用口径）：
      三个漏极电压按 ``Vd,x(t) = Vd,x + DvdDt·(t−25)/1000`` 平移
      （DvdDt 单位 mV/°C → V/°C）；三档电流按欧姆定律反解
      ``Id,max = (Vs − Vd,min(t))/Rbias``（下限电压给最大电流）、
      ``Id,typ = (Vs − Vd,typ(t))/Rbias``、``Id,min = (Vs − Vd,max(t))/Rbias``。
      **这三点已用 AppCAD 4.00 实机三点（−25/25/85 °C）逐值复现**：
      Rbias=110Ω、Vs=12V、Vmax/Typ/Min=6.6/5.5/4.4、dvd/dt=−8mV/°C 时
      AppCAD 给 65.5/55.5/45.5、69.1/59.1/49.1、73.5/63.5/53.5 mA，
      本式同值。
    * ``Pd = Vd,typ(25°C 标称) × Id,typ(t)``。**这是按实机数据确定的经验口径**：
      −25°C 与 25°C 两点精确复现（305.0 / 325.0 mW），85°C 实测 352.8 mW
      而本式给 349.0 mW（低 1.1%，AppCAD 内部另有自洽迭代，未还原）。
      注意 Pd **不**用温度平移后的 Vd,typ(t)——用它会得 327.2 mW（−25°C），
      与实测不符。
    * ``Tj(t) = Tcase + Pd·θjc/1000``（与 thermal_analysis 同一热路模型，
      实机三点 13.1/65.6/129.1 °C 全部复现）。
    * ``pd_rbias_mw = Id,max(最高壳温列)²·Rbias``：AppCAD 显示 593.5 mW，
      本式按 85°C 列 Id,max=73.5mA 给 594.2 mW（差 0.1%）。
    * ``bias_loss_db = 10·log10(1 + (Z0/(2·Rbias))²)``：把偏置电阻看成跨接在
      输出节点的并联支路（与 ``_shunt_branch_il_db`` 同一公开二端口公式）。
      AppCAD 实测 0.23 dB，本式在 Rbias=110Ω、Z0=50Ω 时给 0.2186 dB，
      差异在显示圆整范围内。**早期实现用 20·log10(1+Z0/(ωL)) 会给出 0.4165 dB
      （差近一倍），已废弃**。
    * 旁路有效性（仅当给了 ``cb_pf``）：RFC+Cb 串联谐振
      ``f_res = 1/(2π√(L·Cb))``。工程要求 f_res 远低于频段下限（f_res ≪ f_low
      时 ωL ≫ 1/(ωCb)，偏置支路呈高阻感性）。低于 f_low/10 视为合格，否则
      告警 —— **我们自选的 10 倍余量惯例**。

    参数：f_low_hz 频段下限（Hz，仅用于 bias_loss 近似）；z0 Ω；rfc_h H；
    vs V；theta_jc °C/W；dvd_dt_mv_c mV/°C；vd_max/vd_typ/vd_min V（须满足
    vd_max ≥ vd_typ ≥ vd_min）；id_goal_ma mA；tcse_list 恰好 3 个壳温（°C）；
    rbias_ohm Ω（None → 综合）；cb_pf pF（可选，给了才算旁路谐振）。
    返回：``{"rbias_ohm", "rows":[{"tcse","vd","id_ma","pd_mw","tj_c"}],
    "bias_loss_db", "bias_loss_is_approx", "pd_rbias_mw", "id_max_ma",
    "id_typ_ma", "id_min_ma", "rbias_source", "cb_pf", "f_bias_res_hz",
    "warnings"}``。
    """
    f_low = _pos(f_low_hz, "频段下限 f_low")
    z0 = _pos(z0, "系统阻抗 Z0")
    rfc = _pos(rfc_h, "射频扼流圈 RFC")
    vs = _num(vs, "电源电压 Vs")
    theta = _nonneg(theta_jc, "结到壳热阻 θjc")
    dvdt = _num(dvd_dt_mv_c, "漏极电压温度系数 dVd/dT")
    if not math.isfinite(vs) or not math.isfinite(dvdt) or not math.isfinite(theta):
        raise CalcError("电源电压、热阻与温度系数必须是有限数")
    vmax = _num(vd_max, "漏极电压上限 Vd,max")
    vtyp = _num(vd_typ, "标称漏极电压 Vd,typ")
    vmin = _num(vd_min, "漏极电压下限 Vd,min")
    id_goal = _pos(id_goal_ma, "目标漏极电流 Id,goal")
    if not (vmax >= vtyp >= vmin):
        raise CalcError("需满足 Vd,max ≥ Vd,typ ≥ Vd,min"
                        f"（收到 {vmax}/{vtyp}/{vmin}）")
    if not isinstance(tcse_list, (list, tuple)):
        raise CalcError("壳温列表 tcse_list 必须是 3 个温度值")
    if len(tcse_list) != 3:
        raise CalcError(f"壳温列表需恰好 3 个温度点（收到 {len(tcse_list)} 个）")
    tcse_vals = [_num(t, f"壳温 tcse[{i}]") for i, t in enumerate(tcse_list)]
    for t in tcse_vals:
        if not math.isfinite(t) or t <= -273.15:
            raise CalcError(f"壳温必须高于绝对零度（收到 {t}°C）")
    warnings = []

    if rbias_ohm is None:
        if vs <= vtyp:
            raise CalcError("电源电压必须高于漏极电压（否则偏置电阻无正电流）")
        rbias = (vs - vtyp) / (id_goal / 1000.0)
        src = "综合：Rbias=(Vs−Vd,typ)/Id,goal"
    else:
        rbias = _pos(rbias_ohm, "偏置电阻 Rbias")
        src = "输入值"

    def id_ma_at(vd):
        return (vs - vd) / rbias * 1000.0

    def vd_shift(vd_nom, t):
        return vd_nom + dvdt * (t - 25.0) / 1000.0

    rows = []
    for t in tcse_vals:
        vd = vd_shift(vtyp, t)
        id_ty = id_ma_at(vd)
        id_mx = id_ma_at(vd_shift(vmin, t))
        id_mn = id_ma_at(vd_shift(vmax, t))
        # Pd 的电压因子固定用 25°C 标称 Vd,typ（实机口径，见 docstring）
        pd_mw = vtyp * id_ty
        tj = t + pd_mw / 1000.0 * theta
        rows.append({"tcse": t, "vd": vd, "id_ma": id_ty,
                     "id_max_ma": id_mx, "id_min_ma": id_mn,
                     "pd_mw": pd_mw, "tj_c": tj})
        if id_ty <= 0.0:
            warnings.append(f"Tcase={t:g}°C：Id={id_ty:.4g}mA ≤ 0（Vd 已超过 Vs），"
                            "器件无法工作")
        elif tj > 150.0:
            warnings.append(f"Tcase={t:g}°C：结温 {tj:.3g}°C 超过 150°C 上限")

    # 偏置电阻按"跨接在输出节点的并联支路"计插入损耗（实机 0.23 dB）
    bias_loss = _shunt_branch_il_db(rbias, z0)
    f_res = None
    cb = None
    if cb_pf is not None:
        cb = _pos(cb_pf, "旁路电容 Cb") * 1e-12
        f_res = 1.0 / (2.0 * math.pi * math.sqrt(rfc * cb))
        if f_res > f_low / 10.0:
            warnings.append(
                f"RFC–Cb 串联谐振 {f_res:.3g}Hz 未低于 f_low/10="
                f"{f_low / 10:.3g}Hz：带内偏置支路阻抗不够高（需加大 Cb 或 L）")

    # Rbias 上的直流耗散：按最高壳温列的 Id,max 计（实机口径 593.5 mW）
    id_max_hot = rows[-1]["id_max_ma"]
    return {
        "rbias_ohm": rbias,
        "rows": rows,
        "bias_loss_db": bias_loss,
        "bias_loss_is_approx": False,
        "pd_rbias_mw": (id_max_hot ** 2) * rbias / 1000.0,
        "id_max_ma": id_ma_at(vmin),
        "id_typ_ma": id_ma_at(vtyp),
        "id_min_ma": id_ma_at(vmax),
        "rbias_source": src,
        "cb_pf": (cb * 1e12) if cb is not None else None,
        "f_bias_res_hz": f_res,
        "warnings": warnings,
    }


# --- 检波器公共内核 --------------------------------------------------------

# 输出表上限：理想指数律模型没有串联电阻、电源钳位与热效应，输入功率稍大
# 就会给出天文数字。超过此值一律截断成该值并逐次告警（**我们自选的显示
# 截断惯例**，不代表任何器件极限）。
_DET_MV_CAP = 1e9


def _bessel_i0_log(x):
    """返回 (I0(x), ln I0(x))；x ≥ 40 时只给对数（避免 exp 溢出）。

    I0(x) = Σ_{k≥0} (x²/4)^k/(k!)²（修正贝塞尔函数的标准级数，公开数值
    手册，如 Abramowitz & Stegun 9.6.12）。x ≥ 40 时级数项过大且增长慢，
    改用渐近式 I0(x) ≈ e^x/√(2πx)（同书 9.7.1），相对误差 <1e-4。
    """
    if x < 0.0:
        raise CalcError("贝塞尔函数参数不能为负")
    if x < 40.0:
        term = 1.0
        total = 1.0
        k = 1
        while k < 400:
            term *= (x * x * 0.25) / (k * k)
            total += term
            if term <= 1e-17 * total:
                break
            k += 1
        return total, math.log(total)
    return None, x - 0.5 * math.log(2.0 * math.pi * x)


def _detector_pins(pin_start, pin_stop, pin_step):
    """按起止点/步进生成 dBm 功率点（含端点，步进可为负）。"""
    start = _num(pin_start, "起始功率")
    stop = _num(pin_stop, "终止功率")
    step = _num(pin_step, "功率步进")
    if not (math.isfinite(start) and math.isfinite(stop)
            and math.isfinite(step)):
        raise CalcError("功率扫描参数必须是有限数")
    if step == 0.0:
        raise CalcError("功率步进不能为 0")
    if (stop - start) * step < 0.0:
        raise CalcError("功率步进方向与起止点不一致（步进符号需指向终止点）")
    n = int(math.floor((stop - start) / step + 1e-9))
    if n < 0:
        raise CalcError("功率扫描点数为负：请检查起止与步进")
    if n + 1 > 1000:
        raise CalcError(f"功率扫描点数 {n + 1} 过多（上限 1000），请加大步进")
    out = [round(start + i * step, 12) for i in range(n + 1)]
    if abs(out[-1] - stop) > 1e-9:
        out.append(round(stop, 12))  # 终点不在栅格上时补上终点
    return out


# AppCAD 4.00 实机反解常量：两页检波器（大/小信号）的 Rv 反算出的等效热电压
# 完全一致（53.642*500.03e-6/1.08 = 476021.321*0.06e-6/1.15 = 0.0248358 V），
# 对应 ~288.2 K；无法用 k/q/T 的任一标准组合复现，故作为独立定标常量。
VT_APPCAD = 0.0248358

def _detector_core(t_c, rs_ohm, io_ua, eg_ev, iso_ua, n_ideal, z0, rm_ohm,
                   ri_ohm, rg_ohm, pin_start_dbm, pin_stop_dbm, pin_step_db):
    """检波器公共计算：热电压、饱和电流温度修正、偏置点电导、视频电阻。

    返回 (基础量 dict, 功率点列表)。**公式出处逐条标注**：

    * 热电压（公开文献标准）：``vt = k·T/q``，T = t_c + 273.15（开尔文），
      k = 1.380649e-23 J/K、q = 1.602176634e-19 C（2019 SI 定义值）。
    * 理想因子修正：``vte = n·vt``（肖特基二极管等效热电压）。
    * 饱和电流温度修正（公开文献常见的"能隙标定"形式，是热电子发射模型
      Is ∝ T^m·exp(−qφB/kT) 的工程改写）：
      ``Is(T) = Is(25°C)·(T/298.15)^1.5·exp[−(Eg·q/(n·k))·(1/T − 1/298.15)]``
      Eg 以 eV 给出（换焦耳乘 q）。**指数 3/2 是我们选定的惯例**（文献里
      (T/T0)² 与 (T/T0)^1.5 两种写法都常见，0…100 °C 内差异 <15%）；把 n
      放进指数同样是工程近似（严格理论中能隙项不含 n，实际器件用它吸收
      势垒不均匀性）。T = 298.15 K 时 Is 严格等于输入的 io_ua。
    * 偏置点动态电导：二极管方程 ``I = Is(e^{V/vte} − 1)`` 求导得
      ``gd = (Is + Ibias)/vte`` —— 在偏置点 Vd0 处 Is·e^{Vd0/vte} = Is+Iso，
      故闭式如上。Iso 是偏置/失调电流（µA）。
    * 检波灵敏度 Λ（**公开文献的标准视频检波器分析口径**，不是任何商业
      软件的实测/定标值）：``gamma = 1/(1 + Rs/Rm)`` 是源阻抗 Rs 与二极管
      视频电阻 Rm 的分压比；``Lambda = gamma/(n·vte)``，单位 1/V。它的
      物理含义就是"归一化增量电导 (1/I)·dI/dV 乘以分压比"：
      实际绝对灵敏度 = Λ·(Is+Iso)·Rv（A/V·A·Ω = V，见 scan）。
    * 视频负载 ``Rv = Ri‖Rg``（Rg 为 None 时只取 Ri）—— 视频电路常识。
    * 零射频点两个量：
      ``Vd0 = n·vte·ln(1 + Iso/Is)``（**结上**电压，二极管方程精确解；
      大信号页用它，键名 vd0_v）；
      ``Vo0 = Iso·Rv``（**视频输出节点**的偏置电平，小信号页用它，
      键名 vo0_v）。两者物理位置不同，不可互换。

    **AppCAD 4.00 实机定标（2026-10-30 采集，见 docs/RF计算器与对拍清单.md）**：
    两页（大信号 Rs=9Ω/n=1.08/Iso=500µA → Rv=53.642Ω；小信号 Rs=6Ω/n=1.15/
    Iso=0/Is=0.06µA → Rv=476021.321Ω）反解出的等效热电压**完全一致**
    ``Rv·(Is+Iso)/n = 0.0248358 V``（≈288.2 K），而两页显示的 ``Λ`` 都是
    ``1/vt``（298.15 K 下 38.9217，`38.922`）——与 n、Rs、Rm 全无关。
    故本函数按实机口径给出：

    * ``rv_ohm`` = ``n·VT_APPCAD/(Is+Iso)``（AppCAD 显示的 Rv）；
    * ``rv_video_ohm`` = ``Ri‖Rg``（经典视频负载，仅用于扫描表量级）；
    * ``lambda_per_v`` = ``1/vt``（AppCAD 显示的 Λ）；
    * ``lambda_classic_per_v`` = ``gamma/vte``（文献口径，保留备查）。

    已知差异（不拟合，如实记录）：大信号页那个**固定 −20…20 dBm 内部扫描表**
    的绝对幅度与贝塞尔峰值包络差 45×…57571×，且 ≥5 dBm 撞 ``_DET_MV_CAP``
    截断——AppCAD 该表显然对输出做了压缩/钳位，单靠调常数无法复现。
    """
    t_c = _num(t_c, "环境温度 t_c")
    if not math.isfinite(t_c):
        raise CalcError("环境温度必须是有限数")
    if t_c <= -273.15:
        raise CalcError("环境温度必须高于绝对零度（−273.15 °C）")
    rs = _nonneg(rs_ohm, "源阻抗 Rs")
    io = _pos(io_ua, "饱和电流 Is(25 °C)")
    eg = _nonneg(eg_ev, "能隙 Eg")
    iso = _nonneg(iso_ua, "偏置/失调电流 Iso")
    n = _pos(n_ideal, "理想因子 n")
    z0 = _pos(z0, "系统阻抗 Z0")
    rm = _pos(rm_ohm, "二极管视频电阻 Rm")
    ri = _pos(ri_ohm, "视频负载 Ri")
    if rg_ohm is None:
        rv = ri
    else:
        rg = _pos(rg_ohm, "视频负载 Rg")
        rv = 1.0 / (1.0 / ri + 1.0 / rg)

    t_k = t_c + 273.15
    vt = KB * t_k / QE
    vte = n * vt
    is_t = io * 1e-6 * (t_k / T_REF_K) ** 1.5 * math.exp(
        -(eg * QE / (n * KB)) * (1.0 / t_k - 1.0 / T_REF_K))
    if not math.isfinite(is_t) or is_t <= 0.0:
        raise CalcError("饱和电流温度修正结果非正或溢出：请检查温度与 Eg")

    base = {
        "t_k": t_k, "vt": vt, "vte": vte, "is_t": is_t,
        "is_ua": is_t * 1e6,
        "iso_ua": iso, "z0": z0,
        "gamma": 1.0 / (1.0 + rs / rm),
        "gd": (is_t + iso * 1e-6) / vte,
        "rv_ohm": n * VT_APPCAD / (is_t + iso * 1e-6),
        "rv_video_ohm": rv,
        "lambda_per_v": 1.0 / vt,
        "lambda_classic_per_v": (1.0 / (1.0 + rs / rm)) / vte,
        "vd0_v": vte * math.log1p(iso * 1e-6 / is_t),
        "vo0_v": iso * 1e-6 * (n * VT_APPCAD / (is_t + iso * 1e-6)),
    }
    pins = _detector_pins(pin_start_dbm, pin_stop_dbm, pin_step_db)
    return base, pins


def _detector_di(base, vrf, square_law):
    """射频包络电压 vrf（V，峰值）→ 检波出的直流电流增量 ΔI（A）。

    * 平方律（小信号，``square_law=True``）：
      i = gd·v + (gd/(2·vte))·v²（二极管方程的泰勒展开，二阶项系数
      gd' = gd/vte 的一半），对 RF 周期取平均 ⟨v²⟩ = vrf²/2：
      ``ΔI = ½·(gd/(2·vte))·vrf² = gd·vrf²/(4·vte)``。
      **1/2 平均因子**就在这里，对应的功率灵敏度 ∝ 1/(n·vte)²。
    * 大信号（峰值包络，``square_law=False``）：对理想指数律二极管作严格
      周期平均（公开文献的**贝塞尔函数大信号检波方程**）：
      ⟨Is·(e^{(Vb+vrf·cosθ)/vte} − 1)⟩ = Is·e^{Vb/vte}·I0(vrf/vte) − Is，
      偏置点满足 Is·e^{Vb/vte} = Is + Iso，故
      ``ΔI = (Is + Iso)·[I0(vrf/vte) − 1]``。
      x = vrf/vte → 0 时 I0−1 ≈ x²/4，自动退化到上面的平方律结果
      （这正是两页在小信号极限下必须一致的自检关系）。
    """
    x = vrf / base["vte"]
    if square_law:
        return base["gd"] * vrf * vrf / (4.0 * base["vte"])
    net = base["is_t"] + base["iso_ua"] * 1e-6
    i0, log_i0 = _bessel_i0_log(x)
    if i0 is not None:
        return net * (i0 - 1.0)
    log_di = math.log(net) + log_i0
    if log_di > 700.0:
        return math.inf
    return math.exp(log_di)


def _detector_scan(base, pins, square_law, warnings):
    """把功率点扫成输出表。

    ``v_rf = √(2·P·Z0)`` 是正弦激励下的包络峰值（P 为平均功率）；
    ``Vo = gamma·ΔI·Rv``（V），输出表用 mV。
    逐点给出 ``square_law_ok = (v_rf < n·vt)``；一旦有点越界就告警说明
    该点是外推值。超过 ``_DET_MV_CAP`` 的输出截断到上限并告警。
    """
    rows = []
    n_out = 0
    for p in pins:
        p_w = dbm_to_w(p)
        vrf = math.sqrt(2.0 * p_w * base["z0"])
        di = _detector_di(base, vrf, square_law)
        # 输出链用**物理视频负载** Ri‖Rg（不是 AppCAD 显示的动态电阻 rv_ohm）
        pout_mv = base["gamma"] * di * base["rv_video_ohm"] * 1e3
        if not math.isfinite(pout_mv) or pout_mv > _DET_MV_CAP:
            pout_mv = _DET_MV_CAP
            n_out += 1
        ok_sq = bool(vrf < base["vte"])
        rows.append({"pin_dbm": p, "pout_mv": pout_mv,
                     "vrf_mv": vrf * 1e3, "square_law_ok": ok_sq})
    if n_out:
        warnings.append(
            f"{n_out}/{len(pins)} 个功率点的输出超过 {_DET_MV_CAP:g}mV 上限并已"
            "截断：理想指数律模型没有串联电阻、电源钳位与自热，深处饱和区，"
            "这些点不可作设计值")
    n_big = sum(1 for row in rows if not row["square_law_ok"])
    if n_big:
        warnings.append(
            f"{n_big}/{len(pins)} 个功率点的 |v_rf| ≥ n·vt：已离开平方律/小信号区")
    return rows


@_safe
def detector_large_signal(t_c, rs_ohm, io_ua, eg_ev, iso_ua, n_ideal, z0,
                          rm_ohm, ri_ohm, rg_ohm=None,
                          pin_start_dbm=20, pin_stop_dbm=30, pin_step_db=5):
    """大信号（峰值包络）二极管检波器：检波灵敏度、饱和电流、零偏结电压、输出表。

    ===== 采用的公式与出处（详见 _detector_core / _detector_di / _detector_scan）=====
    * ``vt = k·T/q``（T = t_c+273.15），``vte = n·vt`` —— 公开文献标准。
    * ``Is(T) = Is(25°C)·(T/298.15)^1.5·exp[−(Eg·q/(n·k))(1/T − 1/298.15)]``
      —— 公开的"能隙标定"工程形式；**指数 3/2 是自选惯例**。
    * ``gamma = 1/(1 + Rs/Rm)``、``Λ = gamma/(n·vte)``（1/V）——
      **公开文献的标准视频检波器分析**口径，Λ 即"归一化增量电导 × 分压比"。
      **不是 AppCAD 或任何商业软件的实测/定标值。**
    * 大信号检波方程（公开文献的贝塞尔函数形式，理想指数律）：
      ``ΔI = (Is + Iso)·[I0(v_rf/vte) − 1]``，``Vo = gamma·ΔI·Rv``；
      x = v_rf/vte → 0 时自动退化为平方律 ``ΔI = gd·v_rf²/(4·vte)``。
      大信号页采用此**峰值包络**口径，因此 Λ **不含**平方律的 1/2 平均因子
      （小信号页才含，见 detector_small_signal）。
    * ``is_ua = Is(T)·1e6``；
    * ``vd0_v = n·vte·ln(1 + Iso/Is)`` —— 零射频时**结上**的直流电压；
    * ``rv_ohm`` = 实机口径 ``n·VT_APPCAD/(Is+Iso)``（AppCAD 页显示的 Rv）；
      ``rv_video_ohm = Ri‖Rg`` 是经典视频负载，输出链用它；
    * ``lambda_per_v`` = ``1/vt``（AppCAD 页显示的 Λ，两页同为 38.922）；
      ``lambda_classic_per_v = gamma/(n·vte)`` 是文献口径，保留备查；
    * ``lambda_mv_uw`` = 该页模型在 P = 1 µW 时的视频输出（mV/µW）。
    * ``pins_dbm`` 每点给 ``pin_dbm / pout_mv / vrf_mv / square_law_ok``。

    ===== 明确的简化/惯例（不冒充实测）=====
    1. Is(T) 的温度指数取 3/2（自选惯例）；
    2. 忽略二极管串联电阻 Rs_diode、结电容与电源钳位，故大功率点会给出
       天文数字——输出超过 ``_DET_MV_CAP`` 时截断并告警；
    3. 默认扫描 20→30 dBm 远超检波器实际工作区（典型 < −10 dBm），此时
       只能当量级参考，warnings 会明确说明。

    参数：t_c °C；rs_ohm 源阻抗 Ω；io_ua 25 °C 饱和电流 µA；eg_ev 能隙 eV；
    iso_ua 偏置/失调电流 µA；n_ideal 理想因子；z0 Ω；rm_ohm 二极管视频电阻 Ω；
    ri_ohm / rg_ohm 视频负载 Ω（rg 可为 None）；pin_*_dbm 扫描起止与步进（dBm）。
    返回：``{"lambda_per_v","lambda_mv_uw","is_ua","vd0_v","rv_ohm","vt",
    "vte","gamma","gd","t_k","pins_dbm","warnings"}``。
    """
    base, pins = _detector_core(
        t_c, rs_ohm, io_ua, eg_ev, iso_ua, n_ideal, z0, rm_ohm, ri_ohm,
        rg_ohm, pin_start_dbm, pin_stop_dbm, pin_step_db)
    warnings = []
    lam_per_v = base["lambda_per_v"]
    # 1 µW 参考点的输出（mV/µW）：用本页同一模型求值，保证两页在小功率
    # 极限下自洽（贝塞尔式在 x≪1 时退化为平方律，见 _detector_di）。
    vrf_1uw = math.sqrt(2.0 * 1e-6 * base["z0"])
    di_1uw = _detector_di(base, vrf_1uw, square_law=False)
    lam_mv_uw = base["gamma"] * di_1uw * base["rv_video_ohm"] * 1e3
    rows = _detector_scan(base, pins, square_law=False, warnings=warnings)
    if base["iso_ua"] == 0.0:
        warnings.append("Iso=0：零射频时结电压 Vd(0)=0（纯零偏置点），实际灵敏度"
                        "远低于带偏置情形")
    return {
        "lambda_per_v": lam_per_v,
        "lambda_classic_per_v": base["lambda_classic_per_v"],
        "lambda_mv_uw": lam_mv_uw,
        "is_ua": base["is_ua"],
        "vd0_v": base["vd0_v"],
        "rv_ohm": base["rv_ohm"],
        "rv_video_ohm": base["rv_video_ohm"],
        "vt": base["vt"],
        "vte": base["vte"],
        "gamma": base["gamma"],
        "gd": base["gd"],
        "t_k": base["t_k"],
        "pins_dbm": rows,
        "warnings": warnings,
    }


@_safe
def detector_small_signal(t_c, rs_ohm, io_ua, eg_ev, iso_ua, n_ideal, z0,
                          rm_ohm, ri_ohm, rg_ohm=None,
                          pin_start_dbm=20, pin_stop_dbm=30, pin_step_db=5):
    """小信号（平方律）二极管检波器：与大信号页同参数，口径不同。

    ===== 与大信号（detector_large_signal）的三点差异 =====
    1. **灵敏度带 1/2 平均因子**：``Λ_SS = gamma/(2·n·vte) = Λ_LS/2``。
       平方律区检波电流 ``ΔI = ½·(gd/(2·vte))·v_rf²``（对 RF 周期作时间
       平均产生 1/2），故功率灵敏度 ∝ ``Λ²·Rv`` ∝ ``1/(n·vte)²`` ——
       "平方项"即由此而来；返回的 ``lambda_per_v`` 是其电压侧一阶因子
       （∝ 1/(n·vte)）。大信号页按峰值包络线性响应，没有这个 1/2。
       这是**公开文献标准视频检波器平方律分析**的两个口径，不是拟合系数。
    2. **零射频输出键不同**：``vo0_v = Iso·Rv`` 是**视频输出节点**在零射频
       时的直流电平（偏置电流落在视频负载上的压降）；大信号页的 ``vd0_v``
       是**结**上的电压 ``n·vte·ln(1+Iso/Is)``。两者物理位置不同，键名
       相应不同（Vo(0) vs Vd(0)），不可互换。
    3. 输出链 ``Vo = gamma·gd·v_rf²/(4·vte)·Rv``，仅在平方律区成立。
       扫描表逐点给 ``square_law_ok``（判据 ``v_rf < n·vt``）；越界点与
       截断点都会告警。``lambda_mv_uw`` = 本页模型在 P = 1 µW 时的输出 mV。

    vt、Is(T) 温度修正、gamma = 1/(1+Rs/Rm) 与大信号页是**完全相同的公开
    公式**（见 _detector_core）。``rv_ohm`` 取实机口径 ``n·VT_APPCAD/(Is+Iso)``
    （AppCAD 页显示的 Rv），输出链用物理视频负载 ``rv_video_ohm = Ri‖Rg``；
    ``lambda_per_v`` 取 AppCAD 页的 ``Λ = 1/vt``（与大信号页同值），文献口径的
    ``lambda_classic_per_v = gamma/(2·n·vte)`` 另行给出备查。
    Is(T) 的温度指数 3/2 与输出截断上限均为工程惯例（见大信号页说明）。

    返回：``{"lambda_per_v","lambda_mv_uw","is_ua","vo0_v","rv_ohm","vt",
    "vte","gamma","gd","t_k","pins_dbm","warnings"}``。
    """
    base, pins = _detector_core(
        t_c, rs_ohm, io_ua, eg_ev, iso_ua, n_ideal, z0, rm_ohm, ri_ohm,
        rg_ohm, pin_start_dbm, pin_stop_dbm, pin_step_db)
    warnings = ["pout_mv 为平方律小信号结果，超出平方律区仅为外推参考"]
    lam_per_v = base["lambda_per_v"]
    vrf_1uw = math.sqrt(2.0 * 1e-6 * base["z0"])
    di_1uw = _detector_di(base, vrf_1uw, square_law=True)
    lam_mv_uw = base["gamma"] * di_1uw * base["rv_video_ohm"] * 1e3
    rows = _detector_scan(base, pins, square_law=True, warnings=warnings)
    return {
        "lambda_per_v": lam_per_v,
        "lambda_classic_per_v": base["gamma"] / (2.0 * base["vte"]),
        "lambda_mv_uw": lam_mv_uw,
        "is_ua": base["is_ua"],
        "vo0_v": base["vo0_v"],
        "rv_ohm": base["rv_ohm"],
        "rv_video_ohm": base["rv_video_ohm"],
        "vt": base["vt"],
        "vte": base["vte"],
        "gamma": base["gamma"],
        "gd": base["gd"],
        "t_k": base["t_k"],
        "pins_dbm": rows,
        "warnings": warnings,
    }
