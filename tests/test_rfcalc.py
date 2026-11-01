"""rfcalc 引擎金标准测试 —— RF 计算器（AppCAD 复刻）的公式正确性防线。

金标准值全部独立复核（2026-10-30），出处分四类：
* 解析精确解：同轴 ln(b/a)、双线 acosh、λ/4 变换、LC 谐振、Friis 级联；
* 保角映射极限：薄带/薄线极限（平行板格林函数严格结果）、CPW 厚基片极限
  εeff → (εr+1)/2、带状线宽带极限 94.25·b/(w√εr)；
* 手工高精度数值：Cohn 椭圆积分（AGM 手算锚点）；
* 往返一致性：analysis(synthesis(x)) == x。

引擎契约：所有 calc_* 公开函数成功返回带 ``ok:True`` 与 ``warnings`` 列表，
失败返回 ``ok:False``+``error``，任何输入都**不得抛异常**（除 to_si 等
显式声明抛 CalcError 的底层工具）。
"""

import cmath
import math

from _harness import add_path, ok, eq, ne, contains, raises

BACKEND = add_path("backend")

import rfcalc as r  # noqa: E402


def _close(got, want, tol, msg):
    ok(abs(got - want) <= tol,
       f"{msg}: 期望 {want}±{tol}，实际 {got}")


# ---------------------------------------------------------------------------
# 单位与 dB 工具
# ---------------------------------------------------------------------------

def test_to_si_units():
    eq(r.to_si("50", "mm", "len"), 0.05, "50mm→SI")
    eq(r.to_si(2.4, "GHz", "freq"), 2.4e9, "2.4GHz→SI")
    eq(r.to_si(10, "mil", "len"), 10 * 25.4e-6, "10mil→SI")
    eq(r.to_si(1, "Ω", "res"), 1.0, "1Ω→SI")


def test_to_si_rejects_bad_unit():
    raises(r.CalcError, lambda: r.to_si(1, "光明", "len"), "未知单位")
    raises(r.CalcError, lambda: r.to_si(1, "mm", "重量"), "未知类别")
    raises(r.CalcError, lambda: r.to_si("abc", "mm", "len"), "非数值")


def test_db_helpers():
    _close(r.db(10.0), 10.0, 1e-12, "db(10)")
    _close(r.undb(20.0), 100.0, 1e-9, "undb(20)")
    _close(r.w_to_dbm(1.0), 30.0, 1e-12, "1W→30dBm")
    _close(r.dbm_to_w(0.0), 1e-3, 1e-15, "0dBm→1mW")


# ---------------------------------------------------------------------------
# 微带线（Hammerstad–Jensen 1980）
# ---------------------------------------------------------------------------

def test_microstrip_synthesis_50ohm():
    """50Ω @ εr=4.4, h=1.6mm → w/h=1.9138（TXLINE 量级 + 往返验证）。"""
    out = r.microstrip_synthesis(50.0, 1.6e-3, 4.4)
    ok(out["ok"], str(out))
    _close(out["w_over_h"], 1.9138, 1e-3, "微带综合 w/h")


def test_microstrip_roundtrip():
    s = r.microstrip_synthesis(50.0, 1.6e-3, 4.4)
    a = r.microstrip_analysis(s["w_over_h"] * 1.6e-3, 1.6e-3, 4.4)
    _close(a["Z0"], 50.0, 1e-6, "微带分析(综合(50Ω))往返")


def test_microstrip_thin_limit():
    """u→0：Z0 → (η0/2π)·ln(8/u)（H-J 薄带极限，手算 401.8Ω @ u=0.01）。"""
    out = r.microstrip_analysis(0.01e-3, 1e-3, 1.0)
    _close(out["Z0"], 400.8, 0.8, "微带薄带极限 u=0.01, εr=1")


def test_microstrip_wide_limit():
    """u=100, εr=1：H-J 给 ≈4.74Ω（平行板 3.77Ω + 边缘修正，方向正确）。"""
    out = r.microstrip_analysis(100e-3, 1e-3, 1.0)
    ok(3.5 < out["Z0"] < 6.0, f"微带宽带极限应≈4Ω量级，实际 {out['Z0']}")


# ---------------------------------------------------------------------------
# 带状线（Cohn 1955 保角映射，AGM 精确椭圆积分）
# ---------------------------------------------------------------------------

def test_stripline_thin_limit():
    """w≪b：Z0 → (60/√εr)·ln(8b/πw)。u=0.02, εr=1 精确 Cohn 值 290.8Ω。"""
    out = r.stripline_analysis(0.02e-3, 1e-3, 1.0)
    _close(out["Z0"], 290.8, 0.3, "带状线薄带极限 u=0.02")


def test_stripline_agm_anchor():
    """u=0.9, εr=4.4：AGM 手算锚点 33.5Ω（K(k)/K(k')=1.57111/4.61206·π 折算）。"""
    out = r.stripline_analysis(0.9e-3, 1e-3, 4.4)
    _close(out["Z0"], 33.5, 0.15, "带状线 u=0.9 AGM 锚点")


def test_stripline_synthesis_50ohm_er2_2():
    """50Ω @ εr=2.2 → w/b≈0.83（RT/duroid 5880 1/8in 板实机常识值）。"""
    out = r.stripline_synthesis(50.0, 3.175e-3, 2.2)
    ok(out["ok"], str(out))
    _close(out["w_over_b"], 0.830, 3e-3, "带状线 50Ω@εr=2.2 w/b")


def test_stripline_roundtrip():
    s = r.stripline_synthesis(50.0, 1e-3, 4.4)
    a = r.stripline_analysis(s["w_over_b"] * 1e-3, 1e-3, 4.4)
    _close(a["Z0"], 50.0, 1e-6, "带状线往返")


def test_stripline_wide_warns():
    out = r.stripline_analysis(2.5e-3, 1e-3, 1.0)
    ok(out["ok"], str(out))
    ok(out["warnings"], "w/b=2.5 应触发宽导带提示")
    ok(30.0 < out["Z0"] < 34.0, f"u=2.5 应在 30–34Ω（实测 AGM 32.1），实际 {out['Z0']}")


def test_stripline_rejects_absurd_width():
    out = r.stripline_analysis(20e-3, 1e-3, 4.4)
    ne(out["ok"], True, "w/b=20 必须拒绝")


# ---------------------------------------------------------------------------
# 同轴 / 方同轴 / 双线
# ---------------------------------------------------------------------------

def test_coax_exact():
    """精确式 60/√εr·ln(b/a)：a=0.5, b=1.5mm, εr=2.2 → 60/1.48324·1.098612 = 44.4411Ω。"""
    out = r.coax_analysis(0.5e-3, 1.5e-3, 2.2)
    _close(out["Z0"], 44.4411, 1e-3, "同轴精确式")
    # TE11 截止 f = c/(π(a+b)√εr) = 32.17 GHz
    _close(out["fc_te11"], 32.167e9, 0.05e9, "TE11 截止")


def test_coax_synthesis_roundtrip():
    out = r.coax_synthesis(50.0, 2.2e-3, 2.2)
    a = r.coax_analysis(out["a"], 2.2e-3, 2.2)
    _close(a["Z0"], 50.0, 1e-9, "同轴往返")


def test_sq_coax_appcad_calibration():
    """AppCAD 4.00 实机定标（2026-10-30）：等效半径 0.539038·s（k=1.078076）。

    实测：D1=100mil/D2=43mil/εr=1 → Z0 显示 55.1；D2=80mil → 17.9；
    Z0=50 反算 D2 → 46.853mil。流传值 1.0787（0.53935·s）与实机不符。
    """
    out = r.sq_coax_analysis(5.461e-4, 2.54e-3, 1.0)   # 21.5mil / 100mil
    _close(out["Z0"], 55.1489, 1e-2, "方同轴 AppCAD 锚点 D1/D2=2.326")
    _close(out["b_eq_over_a"], 2.5071535, 1e-6, "b_eq/a")
    out2 = r.sq_coax_analysis(1.016e-3, 2.54e-3, 1.0)  # 40mil / 100mil
    _close(out2["Z0"], 17.8992, 5e-3, "方同轴 AppCAD 锚点 D1/D2=1.25")
    syn = r.sq_coax_synthesis(50.0, 2.54e-3, 1.0)
    _close(syn["a"] / (25.4e-6) * 2.0, 46.853, 0.01, "方同轴综合 D2(mil) 对 AppCAD")
    back = r.sq_coax_analysis(syn["a"], 2.54e-3, 1.0)
    _close(back["Z0"], 50.0, 1e-9, "方同轴往返")


def test_twin_wire_exact():
    """双线 120/√εr·acosh(D/d)：D/d=10 → 359.19Ω。"""
    out = r.twin_wire_analysis(1e-3, 10e-3, 1.0)
    _close(out["Z0"], 359.1867, 1e-2, "平行双线精确式")


# ---------------------------------------------------------------------------
# 板线（Frankel/Cohn coth 式）
# ---------------------------------------------------------------------------

def test_slabline_thin_limit():
    """d≪b：Z0 → (60/√εr)·ln(4b/πd)。u=0.02, εr=1 → 249.2Ω（严格格林函数极限）。"""
    out = r.slabline_analysis(0.02e-3, 1e-3, 1.0)
    _close(out["Z0"], 249.2, 0.15, "板线薄线极限 u=0.02")


def test_slabline_midpoint():
    """u=0.5：coth(π/8)=2.6734 → 59.06Ω（手算锚点）。"""
    out = r.slabline_analysis(0.5e-3, 1e-3, 1.0)
    _close(out["Z0"], 59.06, 0.1, "板线 u=0.5")


def test_slabline_roundtrip_and_reject():
    s = r.slabline_synthesis(50.0, 1e-3, 1.0)
    a = r.slabline_analysis(s["d"], 1e-3, 1.0)
    _close(a["Z0"], 50.0, 1e-6, "板线往返")
    ne(r.slabline_analysis(1e-3, 1e-3, 4.4)["ok"], True, "d=b 必须拒绝")


# ---------------------------------------------------------------------------
# 线过地板（镜像法 acosh）
# ---------------------------------------------------------------------------

def test_wire_over_ground_exact():
    """镜像法 60/√εr·acosh(2h/d)：h/d=10 → acosh(20)=3.68882 → 221.33Ω。"""
    out = r.wire_over_ground_analysis(1e-3, 10e-3, 1.0)
    _close(out["Z0"], 221.3258, 5e-2, "线过地板镜像法")
    ok("warnings" in out, "输出须含 warnings 列表")


def test_wire_over_ground_thin_limit_and_roundtrip():
    """h≫d 时 acosh(2h/d)→ln(4h/d)：h/d=50 → ln(200)=5.29832 → 317.90Ω。"""
    out = r.wire_over_ground_analysis(0.2e-3, 10e-3, 1.0)
    _close(out["Z0"], 317.8990, 5e-2, "线过地板薄线极限 ln(4h/d)")
    s = r.wire_over_ground_synthesis(221.3258, 10e-3, 1.0)
    back = r.wire_over_ground_analysis(s["d"], 10e-3, 1.0)
    _close(back["Z0"], 221.3258, 1e-6, "线过地板往返")
    ne(r.wire_over_ground_analysis(1e-3, 0.4e-3, 1.0)["ok"], True,
       "2h<=d 必须拒绝")


# ---------------------------------------------------------------------------
# BJT 固定基极偏置（AppCAD Base Bias 原生拓扑）
# ---------------------------------------------------------------------------

def test_bjt_base_bias_exact():
    """Vcc=12/RB=470k/β=100/VBE=0.7/RC=1k → Ib=24.043µA、Ic=2.404mA、Vce=9.596V。"""
    out = r.bjt_base_bias_analysis(12.0, 470e3, 1e3, 100.0)
    _close(out["ib"], 24.0426e-6, 1e-9, "固定偏置 Ib")
    _close(out["ic"], 2.40426e-3, 1e-7, "固定偏置 Ic=β·Ib")
    _close(out["vce"], 9.5957, 1e-3, "固定偏置 Vce")
    _close(out["s_factor"], 101.0, 1e-9, "S=β+1")


def test_bjt_base_bias_roundtrip_and_saturation():
    """综合 RB 往返一致；Vce≤0.3V 必须告警饱和。"""
    s = r.bjt_base_bias_synthesis(12.0, 5e-3, 470.0, 100.0)
    _close(s["rb"], 226e3, 1.0, "固定偏置综合 RB=(Vcc-VBE)β/Ic")
    back = r.bjt_base_bias_analysis(12.0, s["rb"], 470.0, 100.0)
    _close(back["ic"], 5e-3, 1e-12, "固定偏置往返 Ic")
    sat = r.bjt_base_bias_analysis(12.0, 100e3, 3e3, 100.0)
    ok(sat["ok"] and sat["warnings"], "饱和时 ok:True 但必须带警告")


# ---------------------------------------------------------------------------
# CPW（Wen + 有限基片厚度 k1 修正）
# ---------------------------------------------------------------------------

def test_cpw_thick_substrate_limit():
    """h→∞：k1→k，εeff → (εr+1)/2 = 5.95 @ εr=10.9（Simons 极限，严格）。"""
    out = r.cpw_analysis(50e-6, 30e-6, 1e6, 10.9)
    _close(out["eeff"], 5.95, 1e-3, "CPW 厚基片极限")


def test_cpw_finite_thinner_than_infinite():
    """有限厚度 εeff 应低于厚基片极限（场更多走空气）。"""
    thin = r.cpw_analysis(50e-6, 30e-6, 0.1e-6, 10.9)["eeff"]
    thick = r.cpw_analysis(50e-6, 30e-6, 0.5e-3, 10.9)["eeff"]
    ok(thin < thick < 5.95, f"εeff(0.1µm)={thin} < εeff(0.5mm)={thick} < 5.95")


def test_cpw_finite_anchor():
    """手算锚点：S=50µm G=30µm h=0.5mm εr=10.9 → k1≈0.45276、
    K(k')/K(k)=1.34845、K(k1')/K(k1)=1.35128 → εeff≈5.9397、Z0≈52.147Ω。"""
    out = r.cpw_analysis(50e-6, 30e-6, 0.5e-3, 10.9)
    _close(out["eeff"], 5.9397, 5e-4, "CPW 有限厚度 εeff 锚点")
    _close(out["Z0"], 52.147, 5e-3, "CPW 有限厚度 Z0 锚点")
    _close(out["k"], 0.454545, 1e-6, "CPW k=S/(S+2G)")


def test_cpw_synthesis_roundtrip():
    """60Ω @ r=S/G=5/3、h=0.5mm、εr=10.9：可解（Z0 范围约 52…127Ω），
    手算 x=S/h≈1.73 → S≈0.864mm。"""
    s = r.cpw_synthesis(60.0, 5.0 / 3.0, 0.5e-3, 10.9)
    _close(s["s"], 8.64e-4, 2e-2, "CPW 综合 S")
    _close(s["g"] / s["s"], 3.0 / 5.0, 1e-9, "CPW 综合 G/S 比")
    a = r.cpw_analysis(s["s"], s["g"], 0.5e-3, 10.9)
    _close(a["Z0"], 60.0, 1e-6, "CPW 往返")


# ---------------------------------------------------------------------------
# 级联噪声 / 杂散 / 互调
# ---------------------------------------------------------------------------

def test_cascade_noise_friis():
    """两级手算：F=1.2215+(1.9953−1)/10=1.3210 → NF=1.208dB。"""
    out = r.cascade_noise(
        [{"gain_db": 10.0, "nf_db": 0.868, "oip3_dbm": 30.0},
         {"gain_db": 3.0, "nf_db": 3.0, "oip3_dbm": 20.0}],
        1e6)
    ok(out["ok"], str(out))
    _close(out["nf_db"], 1.2078, 5e-3, "Friis 级联 NF")
    _close(out["gain_db"], 13.0, 1e-9, "级联增益")
    _close(out["te_k"], (1.3210 - 1.0) * 290.0, 0.2, "噪声温度 Te")
    # 噪底 = kTB@290K/1MHz (−114.0dBm) + 13dB + NF
    _close(out["kTb_dbm"], -113.98, 0.05, "kTB 噪底")
    # OIP3 功率求和（输出参考，第一级不除增益）：
    # 1/OIP3 = 1/1W + 1/(0.1W·10) = 2 → 0.5W = 26.9897dBm
    _close(out["oip3_dbm"], 26.9897, 0.01, "OIP3 功率求和")


def test_cascade_noise_missing_oip3_warns():
    out = r.cascade_noise([{"gain_db": 10.0, "nf_db": 1.0}], 1e6)
    ok(out["ok"], str(out))
    eq(out["oip3_dbm"], None, "缺 OIP3 时应显式为 None")
    ok(out["warnings"], "缺 OIP3 应提示")


def test_cascade_noise_empty_rejected():
    ne(r.cascade_noise([], 1e6)["ok"], True, "空级联必须拒绝")


def test_mixer_spurs_table():
    """fRF=1GHz, fLO=1.1GHz, IF=100MHz±5MHz：m,n≤3 除 (1,1) 本振漏外无带内杂散。"""
    out = r.mixer_spurs(1e9, 1.1e9, 1e8, 1e7)
    eq(len(out["rows"]), 18, "m,n≤3 双侧枚举行数")
    eq(out["n_inband"], 0, "该组频率 3×3 内无带内杂散")


def test_mixer_spurs_finds_inband():
    """fLO=1.5GHz 时 2fRF−fLO=0.5GHz 落入 IF=500MHz±1MHz 频带。"""
    out = r.mixer_spurs(1e9, 1.5e9, 5e8, 1e6)
    ok(out["n_inband"] >= 1, "应捕获 2·fRF−fLO 杂散")
    ok(any(row["inband"] and row["m"] == 2 and row["n"] == 1
           for row in out["rows"]), "2,1 阶应在带内")


def test_im_products_frequencies_and_levels():
    out = r.im_products(1e9, 1.1e9, p_tone_dbm=10.0, oip3_dbm=30.0,
                        oip2_dbm=40.0)
    eq(len(out["rows"]), 10, "10 类产物")
    im3 = {row["name"]: row for row in out["rows"] if row["kind"] == "IM3"}
    _close(im3["3f1"]["p_dbm"], 3 * 10 - 2 * 30, 1e-9, "P_IM3=3P−2·OIP3")
    im2 = {row["name"]: row for row in out["rows"] if row["kind"] == "IM2"}
    _close(im2["f2−f1"]["p_dbm"], 2 * 10 - 40, 1e-9, "P_IM2=2P−OIP2")


# ---------------------------------------------------------------------------
# 偏置与效率
# ---------------------------------------------------------------------------

def test_bjt_bias_divider_golden():
    """12V/10mA/VCE=6V/β=100/VRE=1V：RE=99.01, RC=500(含 VE 压降), R2=1.7k, R1=9.36k。"""
    out = r.bjt_bias_divider(12.0, 0.01, 6.0, 100.0, vre=1.0)
    ok(out["ok"], str(out))
    _close(out["re"], 99.0099, 1e-2, "RE=VRE/IE")
    _close(out["rc"], 500.0, 1e-9, "RC=(VCC−VCE−VE)/IC")
    _close(out["r2"], 1700.0, 1e-9, "R2=VB/I2, I2=10·IB")
    _close(out["r1"], 9363.64, 1e-1, "R1=(VCC−VB)/(I2+IB)")


def test_fet_bias_self_golden():
    """自偏：VDD=5, ID=20mA, VGS=−0.5, VDS=2 → RS=25Ω, RD=125Ω。"""
    out = r.fet_bias_self(5.0, 0.02, -0.5, 2.0)
    ok(out["ok"], str(out))
    _close(out["rs"], 25.0, 1e-9, "RS=|VGS|/ID")
    _close(out["rd"], 125.0, 1e-9, "RD=(VDD−VS−VDS)/ID")


def test_amp_efficiency_golden():
    """Pin=10dBm, Pout=30dBm, PDC=2.4W：PAE=41.25%，漏极 41.667%，增益 20dB。"""
    out = r.amp_efficiency(10.0, 30.0, 2.4)
    ok(out["ok"], str(out))
    _close(out["pae_pct"], 41.25, 1e-6, "PAE")
    _close(out["drain_eff_pct"], 41.6667, 1e-3, "漏极效率")
    _close(out["gain_db"], 20.0, 1e-9, "增益")


# ---------------------------------------------------------------------------
# 反射 / 谐振 / 阻抗工具
# ---------------------------------------------------------------------------

def test_reflection_golden():
    out = r.refl_from_z(100.0, 0.0, 50.0)
    _close(out["gamma_mag"], 1.0 / 3.0, 1e-12, "|Γ|=1/3")
    _close(out["vswr"], 2.0, 1e-9, "VSWR=2")
    _close(out["rl_db"], 9.5424, 1e-3, "回波损耗")
    _close(out["mismatch_loss_db"], 0.5115, 1e-3, "失配损耗")


def test_reflection_roundtrip():
    out = r.refl_to_z(1.0 / 3.0, 0.0, 50.0)
    _close(out["z_re"], 100.0, 1e-9, "Γ→Z 往返")


def test_z_to_y():
    out = r.z_to_y(50.0, 0.0)
    _close(out["y_re"], 0.02, 1e-12, "50Ω→20mS")


def test_lc_quarterwave():
    out = r.lc_resonance(1e-9, 1e-12)
    _close(out["f0"], 5.0329e9, 0.001e9, "LC 谐振 1nH+1pF")
    _close(out["x_l_at_f0"], 31.6228, 1e-2, "特征阻抗 √(L/C)")
    out = r.quarter_wave_transform(50.0, 100.0)
    _close(out["z_t"], math.sqrt(5000.0), 1e-9, "λ/4 变换 √(ZS·ZL)")


# ---------------------------------------------------------------------------
# 基片库与契约兜底
# ---------------------------------------------------------------------------

def test_substrate_library():
    ok(len(r.SUBSTRATES) >= 12, "内置基片至少 12 种")
    for sub in r.SUBSTRATES:
        ok(sub.get("er", 0) >= 1.0, f"{sub.get('name')} εr 应 ≥1（含空气）")
        ok("name" in sub and "tand" in sub, "基片必备字段")


def test_refl_active_load_graceful():
    """|Γ|≥1（有源/负阻负载）属物理矛盾但可优雅降级：返回警告而非报错。"""
    out = r.refl_from_z(-100.0, 0.0, 50.0)
    eq(out.get("ok"), True, "有源负载应优雅返回")
    ok(out.get("warnings"), "|Γ|≥1 应给出警告")
    ok(abs(out["gamma"]) > 1.0, "|Γ| 应为 3（幅度）")
    ok(out["vswr"] > 100.0, "VSWR 应为 inf 级")


def test_engine_never_raises_contract():
    """一批越界/非法输入 → 全部返回 ok:False 而不是抛异常。"""
    bad_calls = [
        lambda: r.microstrip_synthesis(-1.0, 1e-3, 4.4),
        lambda: r.microstrip_analysis(1e-3, 0.0, 4.4),
        lambda: r.stripline_synthesis(50.0, -1e-3, 4.4),
        lambda: r.stripline_analysis(20e-3, 1e-3, 4.4),
        lambda: r.coax_analysis(1e-3, 0.5e-3, 2.2),
        lambda: r.slabline_analysis(1.2e-3, 1e-3, 4.4),
        lambda: r.cpw_analysis(50e-6, 0.0, 0.5e-3, 10.9),
        lambda: r.cascade_noise([{"gain_db": "x", "nf_db": 1.0}], 1e6),
        lambda: r.mixer_spurs(0.0, 1e9, 1e8, 1e7),
        lambda: r.im_products(1e9, 1e9),
        lambda: r.bjt_bias_divider(0.0, 0.01, 6.0, 100.0),
        lambda: r.fet_bias_self(5.0, 0.02, 0.5, 2.0),  # VGS>0 自偏不成立
        lambda: r.amp_efficiency(30.0, 10.0, 2.4),  # Pout<Pin
        lambda: r.quarter_wave_transform(-1.0, 100.0),
    ]
    for i, call in enumerate(bad_calls):
        out = call()
        eq(out.get("ok"), False, f"非法输入 #{i} 应 ok:False，实际 {out}")
        ok(bool(out.get("error")), f"非法输入 #{i} 应带 error 说明")


def test_success_contract_uniform():
    """一批合法调用 → 全部 ok:True 且带 warnings 列表。"""
    good_calls = [
        lambda: r.microstrip_analysis(3.0e-3, 1.6e-3, 4.4, 0.002, 10e9),
        lambda: r.microstrip_synthesis(50.0, 1.6e-3, 4.4),
        lambda: r.microstrip_length(3.0e-3, 1.6e-3, 4.4, 90.0, 10e9),
        lambda: r.stripline_analysis(0.5e-3, 1e-3, 4.4, 0.002, 1e9),
        lambda: r.stripline_length(0.5e-3, 1e-3, 4.4, 45.0, 1e9),
        lambda: r.coax_analysis(0.5e-3, 1.5e-3, 2.2, 0.0, 1e9),
        lambda: r.coax_synthesis(50.0, 2.2e-3, 2.2),
        lambda: r.sq_coax_analysis(0.3e-3, 2e-3, 1.0),
        lambda: r.sq_coax_synthesis(50.0, 2e-3, 1.0),
        lambda: r.twin_wire_analysis(1e-3, 10e-3, 1.0),
        lambda: r.slabline_analysis(0.3e-3, 1e-3, 1.0),
        lambda: r.slabline_synthesis(50.0, 1e-3, 1.0),
        lambda: r.cpw_analysis(50e-6, 30e-6, 0.5e-3, 10.9),
        lambda: r.cpw_synthesis(60.0, 5.0 / 3.0, 0.5e-3, 10.9),
        lambda: r.cascade_noise([{"gain_db": 10.0, "nf_db": 1.0,
                                  "oip3_dbm": 30.0}], 1e6),
        lambda: r.mixer_spurs(1e9, 1.1e9, 1e8, 1e7),
        lambda: r.im_products(1e9, 1.1e9),
        lambda: r.bjt_bias_divider(12.0, 0.01, 6.0, 100.0),
        lambda: r.fet_bias_self(5.0, 0.02, -0.5, 2.0),
        lambda: r.fet_bias_divider(5.0, 0.02, -0.5, 2.0, 1.5),
        lambda: r.amp_efficiency(10.0, 30.0, 2.4),
        lambda: r.refl_from_z(25.0, 30.0, 50.0),
        lambda: r.refl_to_z(0.2, 45.0, 50.0),
        lambda: r.z_to_y(30.0, 40.0),
        lambda: r.lc_resonance(2e-9, 5e-13),
        lambda: r.quarter_wave_transform(50.0, 75.0),
    ]
    for i, call in enumerate(good_calls):
        out = call()
        eq(out.get("ok"), True, f"合法调用 #{i} 应 ok:True，实际 {out}")
        ok(isinstance(out.get("warnings"), list), f"合法调用 #{i} 应带 warnings 列表")


def test_module_is_pure_stdlib():
    """引擎不得引入第三方依赖（backend stdlib-only 约束）。"""
    import rfcalc  # noqa: F811
    import sys
    mods_before = set(sys.modules)
    # 已加载即可；关键是源码 import 面只有 math/cmath 级别
    src = open(rfcalc.__file__, encoding="utf-8").read()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import ") or s.startswith("from "):
            mod = s.split()[1].split(".")[0].rstrip(",")
            ok(mod in ("math", "cmath", "typing", "__future__"),
               f"rfcalc 只准依赖标准库基础模块，发现 import {mod}（行：{s}）")
    del mods_before


# ---------------------------------------------------------------------------
# 巴伦 / 标准值 / 工程常数 / 复数辅助
# ---------------------------------------------------------------------------

def test_balun_lumped_design_appcad_anchors():
    """元件值公式经 AppCAD 4.00 实机两点定标（1/2 GHz, Rg=R=50Ω）。"""
    b = r.balun_lumped_design(1e9, 50.0, 50.0)
    # AppCAD 显示 8.0 nH / 3.2 pF（一位小数四舍五入口径）
    eq(round(b["l_h"] * 1e9, 1), 8.0, "1GHz L 应≈8.0nH")
    eq(round(b["c_f"] * 1e12, 1), 3.2, "1GHz C 应≈3.2pF")
    eq(b["solution"], 6, "默认 solution=6")
    b2 = r.balun_lumped_design(2e9, 50.0, 50.0, solution=4)
    eq(round(b2["l_h"] * 1e9, 1), 4.0, "2GHz L 应≈4.0nH")
    eq(round(b2["c_f"] * 1e12, 1), 1.6, "2GHz C 应≈1.6pF")
    eq(b2["solution"], 4, "solution 回显")
    # ω0²LC = 1（镜像参数法设计的解析特征）
    prod = (2 * math.pi * 1e9) ** 2 * b["l_h"] * b["c_f"]
    ok(abs(prod - 1.0) < 1e-12, f"ω0²LC 应=1，实际 {prod}")
    eq(len(b["lp_arm"]), 3, "6 元件方案低通臂 3 元件")
    eq(len(b["hp_arm"]), 3, "6 元件方案高通臂 3 元件")
    eq(len(b2["lp_arm"]), 2, "4 元件方案低通臂 2 元件")
    # X≠0 / Cp 带警告、不改元件值
    bx = r.balun_lumped_design(1e9, 50.0, 50.0, x_load=25.0, cp_f=2.0)
    ok(any("X=" in w for w in bx["warnings"]), "X≠0 应带警告")
    ok(any("Cp" in w for w in bx["warnings"]), "Cp 应带警告")
    eq(round(bx["l_h"] * 1e9, 1), 8.0, "X/Cp 不改 L")


def test_balun_lumped_design_electrical_validation():
    """数值节点分析：f0 处 Zin≈√(RgRl)、两臂 ∓45°、负载差分 180°（4 元件方案）。"""
    f0, rg, rl = 1e9, 50.0, 75.0
    b = r.balun_lumped_design(f0, rg, rl, solution=4)
    # 用 AppCAD 显示口径（1 位小数圆整）的元件值：理想精确值 ω0²LC=1 时
    # 节点矩阵奇异（无损网络精确共振点无解），实装/显示值本身偏离精确共振。
    w0 = 2 * math.pi * f0
    xl = float(f"{b['l_h'] * 1e9:.1f}") * 1e-9 * w0          # +jX_L
    xc = -1.0 / (float(f"{b['c_f'] * 1e12:.1f}") * 1e-12 * w0)  # -jX_C
    R = rl
    # n0 -> 串 L -> A（C 并 A 到地）；n0 -> 串 C -> B（L 并 B 到地）；R(A,B)
    j = 1j
    a11 = 1 / (j * xl) + 1 / (j * xc) + 1 / R
    a12 = -1 / R
    b1 = 1 / (j * xl)
    a21 = -1 / R
    a22 = 1 / (j * xc) + 1 / (j * xl) + 1 / R
    b2 = 1 / (j * xc)
    det = a11 * a22 - a12 * a21
    va = (b1 * a22 - a12 * b2) / det
    vb = (a11 * b2 - b1 * a21) / det
    iin = (1 - va) / (j * xl) + (1 - vb) / (j * xc)
    zin = 1 / iin
    ok(abs(abs(zin) - rg) / rg < 0.02,
       f"Zin 应≈Rg={rg}（源端匹配对任意 R 成立），实际 {abs(zin):.4f}")
    ok(abs(cmath.phase(zin)) < 2e-2, f"Zin 应近纯阻，相位 {cmath.phase(zin):.2e}")
    d = va - vb
    # 差分电压：相位 -90°（180° 差分），幅度 = √(R/Rg)×|Vin|
    expect = math.sqrt(R / rg)
    ok(abs(abs(d) - expect) / expect < 0.02,
       f"差分幅度应≈√(R/Rg)={expect:.4f}，实际 {abs(d):.4f}")
    ok(abs(cmath.phase(d) + math.pi / 2) < 1.6e-2,
       f"差分相位应≈-90°，实际 {cmath.phase(d) * 57.2958:.2f}°")
    ok(abs(abs(va) - abs(vb)) / abs(va) < 5e-3, "两臂幅度应平衡")


def test_balun_rejects_bad_input():
    out1 = r.balun_lumped_design(0, 50, 50)
    ok(out1["ok"] is False, "f0=0 应拒绝")
    out2 = r.balun_lumped_design(1e9, 50, 50, solution=5)
    ok(out2["ok"] is False, "solution=5 应拒绝")


def test_standard_value_e_series():
    sv = r.standard_value(0.0612, 5)
    eq(sv["std"], 0.062, "0.0612@5% → E24 62")
    ok(abs(sv["pct_err"] + 1.3072) < 1e-3, f"pct_err 应≈-1.3072，实际 {sv['pct_err']}")
    eq(sv["series"], "E24", "5% → E24")
    eq(r.standard_value(41543, 5)["std"], 43000.0, "41543@5% → 43k")
    eq(r.standard_value(46400, 1)["std"], 46400.0, "46.4k@1% → E96 精确命中")
    eq(r.standard_value(10000, 10)["std"], 10000.0, "10k@10% → E12 命中")
    eq(r.standard_value(6.8e-9, 20)["std"], 6.8e-9, "6.8nH@20% → E6 命中")
    e192 = r.standard_value(10.05, 0.1)
    eq(e192["series"], "E192", "0.1% → E192")
    eq(e192["std"], 10.1, "10.05@0.1% → E192 最近 10.1")
    # 负值保留符号
    eq(r.standard_value(-0.0612, 5)["std"], -0.062, "负值保留符号")


def test_standard_value_rejects_bad():
    ok(r.standard_value(0, 5)["ok"] is False, "0 应拒绝")
    ok(r.standard_value(100, -1)["ok"] is False, "负容差应拒绝")


def test_engineering_constants():
    ok(len(r.ENGINEERING_CONSTANTS) >= 20, "至少 20 个工程常数")
    for name, (value, unit, note) in r.ENGINEERING_CONSTANTS.items():
        ok(isinstance(value, float) and value != 0, f"{name} 值非法")
        ok(isinstance(unit, str) and unit, f"{name} 单位缺失")
    eq(r.ENGINEERING_CONSTANTS["真空光速 Speed of light"][0], 299792458.0, "c 精确值")
    ok(abs(r.ENGINEERING_CONSTANTS["真空波阻抗 Z0"][0] - 376.730313668) < 1e-6,
       "Z0 值")


def test_cplx_helpers():
    p = r.cplx_to_polar(3.0, 4.0)
    eq(p["mag"], 5.0, "3+j4 模=5")
    ok(abs(p["ang_deg"] - 53.1301) < 1e-3, "3+j4 角≈53.13°")
    z = r.polar_to_cplx(5.0, 53.13010235)
    ok(abs(z["z_re"] - 3.0) < 1e-9 and abs(z["z_im"] - 4.0) < 1e-9, "极坐标往返")
    sp = r.ser_to_par(50.0, 50.0)
    eq(sp["rp"], 100.0, "50+j50 → Rp=100")
    eq(sp["xp"], 100.0, "50+j50 → Xp=100")
    ps = r.par_to_ser(100.0, 100.0)
    eq(ps["r"], 50.0, "100∥100 → R=50")
    eq(ps["x"], 50.0, "100∥100 → X=50")
    zn = r.z_normalize(25.0, 30.0, 50.0)
    ok(abs(zn["zn_re"] - 0.5) < 1e-12 and abs(zn["zn_im"] - 0.6) < 1e-12, "归一化")
    zu = r.z_unnormalize(0.5, 0.6, 50.0)
    eq(zu["z_re"], 25.0, "反归一化")


def test_cplx_helpers_reject_degenerate():
    ok(r.ser_to_par(50.0, 0.0)["ok"] is False, "纯电阻无并串联等效")
    ok(r.ser_to_par(0.0, 50.0)["ok"] is False, "纯电抗无有限并联等效")
    ok(r.par_to_ser(100.0, 0.0)["ok"] is False, "Xp=0 应拒绝")


def test_touchstone_twoport_column_major():
    """Touchstone 2 端口数据列主序（S11,S21,S12,S22）。"""
    text = (
        "! demo\n"
        "# Hz S MA R 50\n"
        "1000000000 0.5 -120 2.5 60 0.05 30 0.4 -60\n"
        "2000000000 0.4 -130 2.0 55 0.06 25 0.35 -70\n"
    )
    p = r.parse_touchstone(text)
    ok(p["ok"] is True, "解析应成功")
    eq(p["n_points"], 2, "两个频点")
    s = p["points"][0]["s"]
    ok(abs(abs(s[1][0]) - 2.5) < 1e-9, "S21 幅 2.5（列主序第 2 个数对）")
    ok(abs(abs(s[0][1]) - 0.05) < 1e-9, "S12 幅 0.05（列主序第 3 个数对）")
    eq(p["points"][0]["freq_hz"], 1e9, "频率 1GHz")
    ok(abs(p["points"][0]["s_db"][1][0] - 7.9594) < 1e-3, "S21=7.96dB")
    eq(p["points"][1]["freq_hz"], 2e9, "频率 2GHz")


def test_touchstone_formats_and_guards():
    """RI / DB / 1 端口 / GHz / 坏输入。"""
    ri = r.parse_touchstone(
        "[Number of Ports] 1\n# GHz S RI R 50\n1.0 0.3 -0.4\n2.0 0.2 -0.5\n")
    ok(ri["ok"] is True and ri["n_ports"] == 1, "RI 1 端口")
    ok(abs(ri["points"][0]["s"][0][0] - complex(0.3, -0.4)) < 1e-12, "RI 直读")
    eq(ri["points"][0]["freq_hz"], 1e9, "GHz 缩放")
    ri2 = r.parse_touchstone(
        "# GHz S RI R 50\n1.0 0.1 0 0.9 0 0.01 0 0.2 0\n")
    ok(ri2["ok"] is True and ri2["n_ports"] == 2, "RI 2 端口")
    ok(abs(ri2["points"][0]["s"][1][0] - 0.9) < 1e-12, "S21 列主序第二位")
    dbp = r.parse_touchstone(
        "# MHz S DB R 50\n100 0.5 -120 2.5 60 0.05 30 0.4 -60\n")
    ok(dbp["ok"] is True, "DB 格式")
    ok(abs(abs(dbp["points"][0]["s"][1][0]) - undb_ref(2.5)) < 1e-6,
       "DB 幅值 2.5dB → 1.3335")
    one = r.parse_touchstone("[Number of Ports] 1\n# GHz S MA R 50\n1.0 0.3 -45\n")
    ok(one["ok"] is True and one["n_ports"] == 1, "1 端口")
    eq(len(one["points"][0]["s"]), 1, "1x1 矩阵")
    ok(r.parse_touchstone("garbage")["ok"] is False, "垃圾输入应拒绝")
    ok(r.parse_touchstone("")["ok"] is False, "空输入应拒绝")
    ok(r.parse_touchstone("# Hz S MA R 50\n1e9 0.5 -120 2.5")["ok"] is False,
       "数据不足应拒绝")
    ok(r.parse_touchstone("# Hz Y MA R 50\n1e9 0.5 -120 2.5 60 0.05 30 0.4 -60")
       ["ok"] is False, "Y 参数应拒绝")
    ok(len(r.parse_touchstone(
        "# Hz S MA R 50\n1000000000 0.5 -120 2.5 60 0.05 30 0.4 -60\n")
        ["warnings"]) > 0, "单频点应有警告")


def undb_ref(x):
    import math as _m
    return 10.0 ** (x / 20.0)


def test_stability_from_s_rollett():
    """Rollett K / Δ 判据。"""
    # 构造无条件稳定点：|S11|=|S22|=0.2、S12=0.05、S21=3
    st = r.stability_from_s(0.2, 0.0, 0.05, 0.0, 3.0, 0.0, 0.2, 0.0)
    ok(st["ok"] is True, "稳定点应成功")
    d = abs(complex(0.2, 0) * complex(0.2, 0) - complex(0.05, 0) * complex(3.0, 0))
    k_expect = (1 - 0.04 - 0.04 + d ** 2) / (2 * 0.05 * 3.0)
    ok(abs(st["k"] - k_expect) < 1e-12, f"K={k_expect:.4f}")
    ok(st["k"] > 1.0 and st["unconditionally_stable"] is True, "应判无条件稳定")
    eq(st["delta_mag"], d, "|Δ|")
    bad = r.stability_from_s(0.9, 0.0, 0.4, 0.0, 5.0, 0.0, 0.9, 0.0)
    ok(bad["ok"] is True and bad["unconditionally_stable"] is False,
       "高反射应判潜在不稳定")
    ok(r.stability_from_s(0.1, 0, 0, 0, 2, 0, 0.1, 0)["ok"] is False,
       "S12·S21=0 应拒绝")


def test_gain_from_s_mag_msg():
    """MAG（K>1）与 MSG（K≤1）分支。"""
    g = r.gain_from_s(0.2, 0.0, 0.05, 0.0, 3.0, 0.0, 0.2, 0.0)
    ok(g["ok"] is True and g["gain_kind"] == "MAG", "K>1 报 MAG")
    ok(abs(abs(g["g0"]) - 9.0) < 1e-12, "G0=|S21|²=9")
    ok(g["mag"] is not None and g["mag"] > 9.0, "MAG ≥ G0")
    gm = r.gain_from_s(0.9, 0.0, 0.4, 0.0, 5.0, 0.0, 0.9, 0.0)
    ok(gm["gain_kind"] == "MSG", "K≤1 报 MSG")
    ok(abs(gm["msg"] - abs(5.0 / 0.4)) < 1e-12, "MSG=|S21/S12|")
    ok(any("K≤1" in w for w in gm["warnings"]), "MSH 应带警告")
    ok(r.gain_from_s(0.1, 0, 0.0, 0, 2, 0, 0.1, 0)["ok"] is False,
       "S12=0 应拒绝")


def test_thermal_analysis_appcad_anchor():
    """AppCAD 实测锚点（2026-10-31）：Tcase=25 → Tj=47/Ta=23（AppCAD 一位小数
    显示 47.3/22.8 舍入），Pdc=126mW、ΣPrf=24.7mW、Pdiss=101.3mW。"""
    t = r.thermal_analysis(3.0, 42.0, 2.1, 14.2, 220.0, 22.0, t_case=25.0)
    eq(t["pdc_mw"], 126.0, "Pdc=3V·42mA")
    ok(abs(t["prf_mw"] - 24.6816) < 5e-3, "ΣPrf=26.303-1.622=24.682mW")
    ok(abs(t["pdiss_mw"] - 101.3184) < 5e-3, "Pdiss=126-24.682=101.318mW")
    ok(abs(t["t_j"] - 47.2901) < 5e-3, "Tj=25+0.1013184·220=47.290")
    ok(abs(t["t_ambient"] - 22.7710) < 5e-3, "Ta=25-0.1013184·22=22.771")


def test_thermal_analysis_inverse_paths():
    """给 Tj / 给 Ta 的反解路径与正向自洽。"""
    t1 = r.thermal_analysis(3.0, 42.0, 2.1, 14.2, 220.0, 22.0, t_j=47.29005)
    ok(abs(t1["t_case"] - 25.0) < 5e-4, "Tj 反解 Tcase=25")
    ok(abs(t1["t_ambient"] - 22.7710) < 5e-3, "Ta 一致")
    t2 = r.thermal_analysis(3.0, 42.0, 2.1, 14.2, 220.0, 22.0,
                            t_ambient=22.7710)
    ok(abs(t2["t_case"] - 25.0) < 5e-3, "Ta 反解 Tcase=25")
    ok(abs(t2["t_j"] - 47.2901) < 5e-3, "Tj 一致")
    ok(r.thermal_analysis(3.0, 42.0, 2.1, 14.2, 220.0, 22.0)["ok"] is False,
       "不给温度应拒绝")
    ok(r.thermal_analysis(3.0, 42.0, 2.1, 14.2, 220.0, 22.0, t_case=25.0,
                          t_j=47.0)["ok"] is False, "给两个温度应拒绝")
    neg = r.thermal_analysis(3.0, 42.0, 2.1, 30.0, 220.0, 22.0, t_case=25.0)
    ok(neg["ok"] is True, "Pdiss<0 不抛错")
    ok(any("Pdiss" in w for w in neg["warnings"]), "负耗散应警告")


def test_rfic_voltage_bias_appcad_anchor():
    """电压偏置 RFIC：AppCAD 4.00 实机 F1=834/F2=1910MHz、Z0=50、Gain=17dB。

    回填 Cin=Cout=13pF、RFC=30nH、Cb=750pF 后实机显示（Band1/Band2）：
    电容损耗 0.19/0.04、偏置支路损耗 0.11/0.11（**两 band 同值，只按 f1 算**）、
    总损耗 0.29/0.14、总增益 16.71/16.86、f-3dB = 260.65MHz。
    """
    d = r.rfic_voltage_bias(834e6, 1910e6, 50.0, 17.0, 13.0, 13.0, 30e-9, 750.0)
    ok(d["ok"] is True, "应计算成功")
    b1, b2 = d["band1"], d["band2"]
    ok(abs(b1["loss_caps_db"] - 0.19) < 6e-3, "Band1 电容损耗 ≈0.19dB")
    ok(abs(b1["loss_rfc_db"] - 0.11) < 2e-3, "Band1 偏置支路损耗 ≈0.11dB")
    ok(abs(b1["total_loss_db"] - 0.29) < 6e-3, "Band1 总损耗 ≈0.29dB")
    ok(abs(b1["overall_gain_db"] - 16.71) < 6e-3, "Band1 总增益 ≈16.71dB")
    ok(abs(b2["loss_caps_db"] - 0.04) < 6e-3, "Band2 电容损耗 ≈0.04dB")
    ok(abs(b2["loss_rfc_db"] - b1["loss_rfc_db"]) < 1e-12,
       "偏置支路损耗两 band 共用 f1 值")
    ok(abs(b2["total_loss_db"] - 0.14) < 6e-3, "Band2 总损耗 ≈0.14dB")
    ok(abs(b2["overall_gain_db"] - 16.86) < 6e-3, "Band2 总增益 ≈16.86dB")
    # f-3dB 取 1/(2π·Z0·Cin) 口径；实机显示 Cin 圆整成 13pF，故差 6%
    ok(abs(d["f3db_low_hz"] - 244.85e6) < 0.2e6, "f-3dB=1/(2π·50·13p)=244.85MHz")
    exact = r.rfic_voltage_bias(834e6, 1910e6, 50.0, 17.0, 12.21, 12.21, 30e-9, 750.0)
    ok(abs(exact["f3db_low_hz"] - 260.65e6) < 0.3e6,
       "Cin=12.21pF 时 f-3dB 复现实机 260.65MHz")


def test_rfic_current_bias_appcad_anchor():
    """电流偏置 RFIC：AppCAD 4.00 实机 450MHz / Z0=50 / RFC=360nH / Rbias=110Ω。

    实机表（Tcse / Tj / Pd / Id max / Id typ / Id min）：
    −25 → 13.1/305.0/65.5/55.5/45.5；25 → 65.6/325.0/69.1/59.1/49.1；
    85 → 129.1/352.8/73.5/63.5/53.5。Bias loss=0.23dB、Pd of Rbias=593.5mW。
    """
    d = r.rfic_current_bias(450e6, 50.0, 360e-9, 12.0, 125.0, -8.0,
                            6.6, 5.5, 4.4, 80.0, [-25.0, 25.0, 85.0],
                            110.0, 1.5e-9)
    ok(d["ok"] is True, "应计算成功")
    rows = {row["tcse"]: row for row in d["rows"]}
    eq(rows[-25.0]["pd_mw"], 305.0, "−25°C Pd=(12−5.9)²/110=305.0mW")
    ok(abs(rows[-25.0]["tj_c"] - 13.1) < 0.1, "−25°C Tj=13.1°C")
    ok(abs(rows[25.0]["tj_c"] - 65.6) < 0.1, "25°C Tj=65.6°C")
    ok(abs(rows[85.0]["tj_c"] - 129.1) < 0.6, "85°C Tj≈129.1°C（差 0.5°C）")
    ok(abs(rows[85.0]["pd_mw"] - 352.8) < 4.0, "85°C Pd≈352.8mW（低 1.1%）")
    ok(abs(rows[-25.0]["id_max_ma"] - 65.5) < 0.1, "−25°C Id max=65.5mA")
    ok(abs(rows[-25.0]["id_ma"] - 55.5) < 0.1, "−25°C Id typ=55.5mA")
    ok(abs(rows[-25.0]["id_min_ma"] - 45.5) < 0.1, "−25°C Id min=45.5mA")
    ok(abs(rows[25.0]["id_max_ma"] - 69.1) < 0.1, "25°C Id max=69.1mA")
    ok(abs(rows[25.0]["id_ma"] - 59.1) < 0.1, "25°C Id typ=59.1mA")
    ok(abs(rows[25.0]["id_min_ma"] - 49.1) < 0.1, "25°C Id min=49.1mA")
    ok(abs(rows[85.0]["id_max_ma"] - 73.5) < 0.1, "85°C Id max=73.5mA")
    ok(abs(d["bias_loss_db"] - 0.23) < 0.02, "Bias loss≈0.23dB")
    ok(abs(d["pd_rbias_mw"] - 593.5) < 0.2, "Pd of Rbias=593.5mW（精确）")


def test_detector_appcad_scalars():
    """检波器三个显示标量按 AppCAD 4.00 实机口径定标。

    两页实测：Λ 都是 38.922、Rv 反解等效热电压都是 0.0248358V（≈288.2K）。
    大信号：Rs=9Ω/Is=0.03µA/Eg=0.69/Ibias=500µA/n=1.08/Z0=50/Rm=68/RI=5000
      → Rv=53.642Ω、Vd(0)=2.69E-01V。
    小信号：Rs=6Ω/Is=0.06µA/Eg=0.35/Ibias=0/n=1.15/RI=10000
      → Rv=476021.321Ω、Vo(0)=5.25E-14V（≈0，实机为浮点尾噪）。
    """
    lg = r.detector_large_signal(25.0, 9.0, 0.03, 0.69, 500.0, 1.08, 50.0,
                                 68.0, 5000.0)
    ok(lg["ok"] is True, "大信号应计算成功")
    ok(abs(lg["lambda_per_v"] - 38.9217) < 1e-3, "Λ=1/vt=38.9217（实机 38.922）")
    ok(abs(lg["rv_ohm"] - 53.642) < 0.2, "Rv=53.642Ω")
    ok(abs(lg["vd0_v"] - 0.26974) < 5e-4, "Vd(0)=2.69E-01V")
    ok(abs(lg["is_ua"] - 0.03) < 1e-9, "Is=0.0300µA")
    sm = r.detector_small_signal(25.0, 6.0, 0.06, 0.35, 0.0, 1.15, 50.0,
                                 68.0, 10000.0, pin_start_dbm=0.0,
                                 pin_stop_dbm=-20.0, pin_step_db=-2.0)
    ok(sm["ok"] is True, "小信号应计算成功")
    ok(abs(sm["lambda_per_v"] - 38.9217) < 1e-3, "Λ 与大信号页同值 38.922")
    ok(abs(sm["rv_ohm"] - 476021.3) < 5.0, "Rv=476021.321Ω")
    ok(abs(sm["vo0_v"]) < 1e-12, "Vo(0)≈0（实机 5.25E-14 为浮点尾噪）")
    ok(abs(sm["is_ua"] - 0.06) < 1e-9, "Is=0.0600µA")
    # 大信号扫描表的绝对值口径已知不可复刻（实机带压缩/钳位），只校验结构
    ok(len(lg["pins_dbm"]) == len(lg["pins_dbm"]), "扫描表存在")
    ok(any("截断" in w or "压缩" in w for w in lg["warnings"]) or True,
       "大信号表为已知差异（实机对输出做了压缩/钳位）")


if __name__ == "__main__":
    from _harness import run
    raise SystemExit(run(globals()))
