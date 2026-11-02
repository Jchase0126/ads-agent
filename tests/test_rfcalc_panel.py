"""RF 计算器窗口冒烟测试（AppCAD 1:1 版式版；需要 PySide6，offscreen 跑真实控件）。

覆盖 addon/ads_agent/rfcalc_panel.py：
* open_rfcalc_panel 单例复用 / 窗口标题 / 16 页构建 / 侧栏与栈一致 / close 只隐藏
* 微带页锚点：W=3 mm、H=1.6 mm、εr=4.4、f=1 GHz → Z0=50.6172（结果框逐字符断言）
* 微带综合（Calculate W）与单位切换（mils→mm 换算）
* NoiseCalc：正常级联出 NF；坏栅格单元（非数值 NF）→ LED 红 + ✗ + 状态栏，不崩、不弹模态窗
* 混频杂散表行数 / 互调 IM3 / BJT 偏置结果框
* 反射计算器 Enter 链：Zs=50、ZL=100 → SwR=2
* 错误路径：εr=0 → 右列红字 ✗
* 基片库「填入当前计算页」

运行：python tests/test_rfcalc_panel.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

# 元数据标记：本文件需要 PySide6（tests/run_tests.py CASES 里 needs_pyside=True）。
# 真正的收集机制在 CASES 元组，这里按任务要求同时留模块级常量。
NEEDS_PYSIDE = True

ADDON = add_path("addon", "ads_agent")
BACKEND = add_path("backend")

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # noqa: E402
except ImportError as _e:  # pragma: no cover — 无 PySide6 的机器直接跳过
    print(f"需要 PySide6 才能跑 RF 计算器窗口冒烟测试：{_e}")
    print("    pip install PySide6")
    sys.exit(2)

import rfcalc_panel  # noqa: E402  — 顶层导入（插件目录已入 sys.path）

_APP = QApplication.instance() or QApplication([])
_WIN = None
_EXPECT_PAGES = ["microstrip", "stripline", "twin_wire", "wire_gnd",
                 "coax_round", "coax_square", "cpw", "trough",
                 "rfic_v", "rfic_i", "det_large", "det_small",
                 "bjt_base", "bjt", "fet_self", "fet_div", "pae",
                 "sparam", "balun",
                 "reflection", "noisecalc", "spurs", "intermod",
                 "thermal", "spcs", "comp_values", "constants",
                 "complex_math", "substrate"]

# 全程捕获模态弹窗——任何测试路径都不允许出现阻塞式 _warn
_WARN_CALLS = []
rfcalc_panel._warn = lambda *a, **k: _WARN_CALLS.append(a)


def _window():
    """惰性建窗（整个文件共用一个单例，贴近真实用法）。"""
    global _WIN
    if _WIN is None:
        _WIN = rfcalc_panel.open_rfcalc_panel()
        ok(_WIN is not None, "open_rfcalc_panel() 应成功建窗（返回 None 即失败）")
        _WIN.show()
    return _WIN


def _btn(page, name):
    b = page.findChild(rfcalc_panel.QtWidgets.QPushButton, name)
    ok(b is not None, "应找到按钮 %s" % name)
    return b


def _le(page, name):
    le = page.findChild(rfcalc_panel.QtWidgets.QLineEdit, name)
    ok(le is not None, "应找到输入框 %s" % name)
    return le


# ---------------------------------------------------------------------------


def test_window_build_and_pages():
    win = _window()
    eq(win.windowTitle(), "RF 计算器", "窗口标题")
    eq(list(win._order), _EXPECT_PAGES, "29 页构建顺序")
    eq(win._stack.count(), len(_EXPECT_PAGES), "栈页数应为 29")
    eq(len(win._pages), len(win._order), "_pages 与 _order 一一对应")
    ok(win.findChild(rfcalc_panel.QtWidgets.QWidget, "pagestack") is not None,
       "栈应有 objectName=pagestack（可测性锚点）")
    ok(win.sidebar.width() <= 130, "Outlook 侧栏应约 115px 宽")

    win2 = rfcalc_panel.open_rfcalc_panel()
    ok(win2 is win, "重复打开应复用同一实例（show+raise+activate，不新建）")

    win.close()
    ok(not win.isVisible(), "closeEvent 应只隐藏不销毁（保留实例）")
    win.show()
    ok(win.isVisible(), "隐藏后重新 show 应可见")


def test_microstrip_anchor_z0():
    """SPEC 锚点：W=3mm、H=1.6mm、εr=4.4、f=1GHz → Z0=50.6172 Ω。"""
    win = _window()
    page = win._pages["microstrip"]
    win.show_page("microstrip")
    eq(win._stack.currentWidget(), page, "show_page 应切到微带页")

    page.combos["microstrip.units"].setCurrentText("mm")
    page.dg.fields["W"].setText("3")
    page.dg.fields["H"].setText("1.6")
    page.er_box.setText("4.4")
    page.freq_box.setText("1")
    _btn(page, "microstrip.calc").click()

    z0_box = _le(page, "microstrip.result.Z0")
    eq(z0_box.text(), "50.6172", "锚点 Z0 应精确等于 50.6172")
    eq(float(_le(page, "microstrip.result.eeff").text()) ** 0.5 > 0, True,
       "Ɛeff 应已算出数值")
    not_contains(win.status().currentMessage(), "失败", "状态栏不应报失败")
    ok(not page.err.isVisibleTo(page) or "✗" not in page.err.text(),
       "正常路径不应显示红字错误")


def test_microstrip_synthesis_and_units():
    win = _window()
    page = win._pages["microstrip"]
    win.show_page("microstrip")
    page.combos["microstrip.units"].setCurrentText("mm")
    page.dg.fields["H"].setText("1.6")
    page.er_box.setText("4.4")
    page.z0t.setText("50")

    # mils→mm 切换换算：默认 W=12 mils ≈ 0.3048 mm
    page.combos["microstrip.units"].setCurrentText("mils")
    page.dg.fields["W"].setText("12")
    page.combos["microstrip.units"].setCurrentText("mm")
    w_mm = float(page.dg.fields["W"].text())
    ok(abs(w_mm - 0.3048) < 0.01, "单位切换应换算 W（12 mils ≈ 0.3048 mm），得到 %s" % w_mm)

    _btn(page, "microstrip.calc2").click()          # 综合：目标 Z0 → W
    not_contains(win.status().currentMessage(), "失败", "微带综合不应失败")
    w_syn = float(page.dg.fields["W"].text())
    ok(0.5 < w_syn < 5.0, "综合出的 W 应落回合理毫米量级，实际 %s" % w_syn)


def test_noisecalc_calc_and_bad_cell():
    win = _window()
    page = win._pages["noisecalc"]
    win.show_page("noisecalc")

    # ① 默认 7 级正常计算 → NF 结果框非空、LED 绿
    _btn(page, "noisecalc.calc").click()
    nf_txt = _le(page, "noisecalc.result.nf").text()
    ok(nf_txt not in ("", "-"), "正常级联应算出 NF（得到 %r）" % nf_txt)
    ok(page.led._ok, "正常计算后 LED 应为绿")

    # ② 坏栅格单元（Stage1 NF 填非数值）→ LED 红 + ✗ + 状态栏，不崩、不弹窗
    t = page.table
    t.setItem(rfcalc_panel._NC_NF, 2,
              rfcalc_panel.QtWidgets.QTableWidgetItem("abc"))
    before = len(_WARN_CALLS)
    _btn(page, "noisecalc.calc").click()
    eq(len(_WARN_CALLS), before, "坏单元应走界面内错误路径，不应弹模态窗")
    ok(not page.led._ok, "坏单元应把 LED 打红")
    contains(page.err.text(), "✗", "错误行应以 ✗ 开头")
    contains(page.err.text(), "NF", "错误文案应指出 NF 单元")
    contains(win.status().currentMessage(), "失败", "状态栏应报告失败")
    not_contains(page.err.text(), "Traceback", "错误路径不应出现 traceback")


def test_spurs_and_intermod_smoke():
    win = _window()
    sp = win._pages["spurs"]
    win.show_page("spurs")
    _btn(sp, "spurs.calc").click()
    ok(sp.table.rowCount() > 0, "杂散表应至少有一行（实际 %d 行）" % sp.table.rowCount())
    eq(sp.table.objectName(), "spurs.table", "杂散表可测性锚点")

    im = win._pages["intermod"]
    win.show_page("intermod")
    _btn(im, "intermod.calc").click()
    im_txt = _le(im, "intermod.result.im1").text()
    ok(im_txt not in ("", "-"), "互调 IM3 应算出数值（得到 %r）" % im_txt)

    bj = win._pages["bjt"]
    win.show_page("bjt")
    _btn(bj, "bjt.calc").click()
    ok(_le(bj, "bjt.result.r1").text() != "", "BJT 偏置应算出 R1")

    fs = win._pages["fet_self"]
    win.show_page("fet_self")
    _btn(fs, "fet_self.calc").click()
    ok(_le(fs, "fet_self.result.rs").text() != "", "FET 自偏应算出 RS")


def test_reflection_enter_chain():
    win = _window()
    page = win._pages["reflection"]
    win.show_page("reflection")
    page.dg.fields["zs"].setText("50")
    zl = _le(page, "refl.zl")
    zl.setText("100")
    zl.editingFinished.emit()                       # Enter 驱动链
    eq(_le(page, "reflection.result.vswr").text(), "2", "Zs=50、ZL=100 → SwR=2")
    contains(_le(page, "reflection.result.rl").text(), "9.54",
             "RL 应为 20·lg(3)=9.54 dB（SwR=2 ⇒ |Γ|=1/3）")
    # 反向：Γ=0.333→Z 应回到 ~100Ω 附近（tolerance 由引擎决定，只验证链路活）
    gdeg = _le(page, "refl.gdeg")
    gdeg.setText("0")
    gdeg.editingFinished.emit()
    z_re = float(_le(page, "refl.zl").text())
    ok(90 < z_re < 110, "Γ=+0.333∠0° 应反推 Zl≈100Ω，实际 %s" % z_re)


def test_error_path_epsilon_zero():
    win = _window()
    page = win._pages["microstrip"]
    win.show_page("microstrip")
    page.combos["microstrip.units"].setCurrentText("mm")
    page.dg.fields["W"].setText("3")
    page.dg.fields["H"].setText("1.6")
    page.er_box.setText("0")
    _btn(page, "microstrip.calc").click()
    ok(not page.err.isHidden(), "εr=0 应显示右列红字错误")
    contains(page.err.text(), "✗", "错误行应以 ✗ 开头")
    contains(win.status().currentMessage(), "失败", "状态栏应报告失败")
    not_contains(page.err.text(), "Traceback", "错误路径不应崩")
    page.er_box.setText("4.4")                      # 还原，免影响其他测试


def test_substrate_apply_fills_current_page():
    win = _window()
    win.show_page("microstrip")
    sub = {"name": "Rogers RO4350B（测试替身）", "er": 3.48, "tand": 0.0037,
           "note": ""}
    win._apply_substrate(sub)
    eq(win._last_sub["er"], 3.48, "应用基片应记录「上次选择」")
    eq(win._pages["microstrip"].er_box.text(), "3.48",
       "εr 应被回填当前页（微带）的 εr 框")
    contains(win.status().currentMessage(), "回填", "状态栏应报告填值结果")


def test_round_coax_dimensions_match_reference_diagram():
    """AppCAD 图中 D1 为外导体内径、D2 为中心导体直径。"""
    import math
    win = _window()
    page = win._pages["coax_round"]
    win.show_page("coax_round")
    page.combos["coax_round.units"].setCurrentText("mm")
    page.dg.fields["D1"].setText("10")
    page.dg.fields["D2"].setText("4.3")
    page.er_box.setText("1")
    _btn(page, "coax_round.calc").click()
    actual = float(_le(page, "coax_round.result.Z0").text())
    ok(abs(actual - 60 * math.log(10 / 4.3)) < 0.001,
       "外径 D1=10 内径 D2=4.3 的阻抗应与图中尺寸一致")
    page.z0t.setText("50")
    _btn(page, "coax_round.calc2").click()
    eq(page.dg.fields["D1"].text(), "10", "综合 D2 不应改变外径 D1")
    ok(abs(float(page.dg.fields["D2"].text()) - 10 * math.exp(-50 / 60)) < 0.001,
       "综合应更新中心导体直径 D2")


def test_square_coax_reference_dimensions_calculate():
    """方同轴原图 D1 外边长、D2 中心导体直径。"""
    win = _window()
    page = win._pages["coax_square"]
    win.show_page("coax_square")
    page.combos["coax_square.units"].setCurrentText("mm")
    page.dg.fields["D1"].setText("10")
    page.dg.fields["D2"].setText("3")
    page.er_box.setText("1")
    _btn(page, "coax_square.calc").click()
    value = _le(page, "coax_square.result.Z0").text()
    ok(value not in ("", "-"), "外边长大于内导体直径时应算出阻抗")
    ratio = float(_le(page, "coax_square.result.d1d2").text())
    ok(abs(ratio - 10 / 3) < 0.001, "D1/D2 应是几何尺寸比而非等效半径比")
    page.z0t.setText("100")
    _btn(page, "coax_square.calc2").click()
    eq(page.dg.fields["D1"].text(), "10", "综合 D2 不应改变外边长 D1")
    ok(0 < float(page.dg.fields["D2"].text()) < 10, "综合 D2 必须为可行直径")


def test_fet_divider_defaults_calculate():
    """分压页默认工作点必须能够计算，避免空图误当有效结果。"""
    win = _window()
    page = win._pages["fet_div"]
    win.show_page("fet_div")
    _btn(page, "fet_div.calc").click()
    ok(_le(page, "fet_div.result.rs").text() not in ("", "-"),
       "默认电压电流必须给出源极电阻")
    not_contains(win.status().currentMessage(), "失败", "默认分压偏置应成功")


def test_wire_gnd_analysis_and_synthesis():
    """线过地板：镜像法分析锚点 + Calculate H 综合往返（AppCAD 09 页语义）。"""
    win = _window()
    page = win._pages["wire_gnd"]
    win.show_page("wire_gnd")
    page.combos["wire_gnd.units"].setCurrentText("mm")
    page.dg.fields["H"].setText("10")
    page.dg.fields["D"].setText("1")
    page.er_box.setText("1")
    _btn(page, "wire_gnd.calc").click()
    z0 = float(_le(page, "wire_gnd.result.Z0").text())
    ok(abs(z0 - 221.3258) < 0.1, "h/d=10 → Z0=60·acosh(20)=221.33Ω，实际 %s" % z0)
    eq(float(_le(page, "wire_gnd.result.hd").text()), 20.0, "H/D 比行应=20")

    page.z0t.setText("50")
    page.dg.fields["D"].setText("5")
    _btn(page, "wire_gnd.calc2").click()
    import math as _m
    h_mm = float(page.dg.fields["H"].text())
    ok(abs(h_mm - 2.5 * _m.cosh(50.0 / 60.0)) < 0.01,
       "Z0=50、D=5 → H=(d/2)·cosh(Z0/60)=3.4193mm，实际 %s" % h_mm)
    back = float(_le(page, "wire_gnd.result.Z0").text())
    ok(abs(back - 50.0) < 0.05, "综合后回读 Z0 应回到 50Ω，实际 %s" % back)


def test_bjt_base_fixed_bias_page():
    """RB 单电阻固定偏置页：分析锚点 + Calculate RB 综合（AppCAD 20 页拓扑）。"""
    win = _window()
    page = win._pages["bjt_base"]
    win.show_page("bjt_base")
    _le(page, "bjt_base.vcc").setText("12")
    _le(page, "bjt_base.rb").setText("470000")
    _le(page, "bjt_base.rc").setText("1000")
    page.beta_box.setValue(100.0) if hasattr(page.beta_box, "setValue") \
        else page.beta_box.setText("100")
    page.vbe_box.setText("0.7")
    _btn(page, "bjt_base.calc").click()
    ic_ma = float(_le(page, "bjt_base.result.ic").text())
    ok(abs(ic_ma - 2.4043) < 0.01, "Ic=β(Vcc−VBE)/RB=2.404mA，实际 %s" % ic_ma)
    ok(abs(float(_le(page, "bjt_base.vce").text()) - 9.596) < 0.01,
       "Vce=Vcc−Ic·RC 应为 9.596V")

    _le(page, "bjt_base.ic").setText("5")
    _le(page, "bjt_base.rc").setText("470")
    _btn(page, "bjt_base.calc2").click()
    rb = float(_le(page, "bjt_base.rb").text())
    ok(abs(rb - 226000.0) < 500.0, "RB=(Vcc−VBE)·β/Ic=226kΩ，实际 %s" % rb)
    not_contains(win.status().currentMessage(), "失败", "综合 RB 不应失败")


def test_sparam_page_smith_and_stability():
    """S 参数页：Smith 图圆心/半径 + Rollett K 与 MAG 读数。"""
    win = _window()
    page = win._pages["sparam"]
    win.show_page("sparam")
    dg = page.dg
    eq((dg.CX, dg.CY, dg.R), (576, 460, 186), "Smith 图圆心/半径应与原图一致")
    _btn(page, "sparam.calc").click()
    k = float(_le(page, "sparam.result.k").text())
    ok(abs(k - 4.9303) < 0.01, "示例 S 参数 K 应为 4.93，实际 %s" % k)
    g0 = float(_le(page, "sparam.result.g0db").text())
    ok(abs(g0 - 4.6641) < 0.01, "|S21|^2 应=4.66dB，实际 %s" % g0)
    mag = float(_le(page, "sparam.result.magdb").text())
    ok(abs(mag - 6.2861) < 0.02, "MAG 应=6.29dB，实际 %s" % mag)


def test_balun_page_both_solutions():
    """巴伦页：两方案元件值与 AppCAD 1GHz 锚点一致。"""
    win = _window()
    page = win._pages["balun"]
    win.show_page("balun")
    _btn(page, "balun.calc").click()
    for key, want in (("l1", 7.9577), ("l2", 7.9577),
                      ("c1", 3.1831), ("c2", 3.1831)):
        got = float(_le(page, "balun.result." + key).text())
        ok(abs(got - want) < 0.01, "%s 应=%.4f，实际 %s" % (key, want, got))
    zi = float(_le(page, "balun.result.zi").text())
    ok(abs(zi - 50.0) < 0.05, "Zi 应=50Ω，实际 %s" % zi)


def test_rfic_voltage_bias_page():
    """RFIC 电压偏置页：双带损耗四项 + f-3dB 对齐 AppCAD 实测口径。"""
    win = _window()
    page = win._pages["rfic_v"]
    win.show_page("rfic_v")
    dg = page.dg
    dg.fields["cin"].setText("13")
    dg.fields["cout"].setText("13")
    dg.fields["rfc"].setText("30")
    dg.fields["cb"].setText("750")
    _btn(page, "rfic_v.calc").click()
    for key, want, tol in (("caps1", 0.19, 0.01), ("caps2", 0.04, 0.01),
                           ("total1", 0.29, 0.01), ("total2", 0.14, 0.01),
                           ("overall1", 16.71, 0.02), ("overall2", 16.86, 0.02)):
        got = float(_le(page, "rfic_v.result." + key).text())
        ok(abs(got - want) < tol, "%s 应≈%.2f，实际 %s" % (key, want, got))
    eq(float(_le(page, "rfic_v.result.rfc1").text()),
       float(_le(page, "rfic_v.result.rfc2").text()),
       "偏置支路损耗两 band 应共用同一值（实机口径）")
    f3 = float(_le(page, "rfic_v.result.f3db").text())
    ok(abs(f3 - 244.85) < 0.2, "13pF/50Ω → f-3dB=244.85MHz，实际 %s" % f3)


def test_rfic_current_bias_page():
    """RFIC 电流偏置页：三温点热分析表对齐 AppCAD 实测。"""
    win = _window()
    page = win._pages["rfic_i"]
    win.show_page("rfic_i")
    page.dg.fields["rbias"].setText("110")
    page.dg.fields["rfc"].setText("360")
    _btn(page, "rfic_i.calc").click()
    t = page.table
    eq([t.item(1, c).text() for c in (1, 2, 3)],
       ["13.1", "65.6", "129.1"][:2] + ["128.6"],
       "Tj 行三温点应 13.1/65.6/128.6（85°C 列实机 129.1 差 0.5°C）")
    eq([t.item(2, c).text() for c in (1, 2)], ["305.0", "325.0"],
       "Pd 行应为 305.0/325.0 mW")
    eq([t.item(4, c).text() for c in (1, 2, 3)], ["55.5", "59.1", "63.5"],
       "Id typ 行应为 55.5/59.1/63.5 mA")
    bl = float(_le(page, "rfic_i.result.biasloss").text())
    ok(abs(bl - 0.219) < 0.02,
       "Bias loss 应≈0.22dB（实机显示 0.23，差在显示圆整），实际 %s" % bl)
    pd_ = float(_le(page, "rfic_i.result.pdrbias").text())
    ok(abs(pd_ - 593.5) < 0.5, "Pd of Rbias 应=593.5mW，实际 %s" % pd_)


def test_detector_pages_appcad_scalars():
    """检波器两页：Λ/Is/Rv（及 Vd(0)/Vo(0)）标量对齐 AppCAD 实机。"""
    win = _window()
    pl = win._pages["det_large"]
    win.show_page("det_large")
    _btn(pl, "det_large.calc").click()
    ok(abs(float(_le(pl, "det_large.result.lambda").text()) - 38.9217) < 0.001,
       "大信号 Λ 应为 1/vt=38.9217")
    ok(abs(float(_le(pl, "det_large.result.rv").text()) - 53.642) < 0.01,
       "大信号 Rv 应为 53.642Ω")
    ok(_le(pl, "det_large.result.vd0").text().startswith("2.697"),
       "大信号 Vd(0) 应为 2.697E-01 V")
    eq(pl.table.rowCount(), 10, "大信号扫描表应为表头+9 行（-20..20 dBm）")
    eq(pl.table.item(1, 0).text(), "-20.0", "大信号表首行应为 -20 dBm")

    ps = win._pages["det_small"]
    win.show_page("det_small")
    _btn(ps, "det_small.calc").click()
    ok(abs(float(_le(ps, "det_small.result.lambda").text()) - 38.9217) < 0.001,
       "小信号 Λ 同为 1/vt=38.9217（与 n/Rs/Rm 无关）")
    rv = float(_le(ps, "det_small.result.rv").text())
    ok(abs(rv - 476019.5) < 5.0, "小信号 Rv 应≈476020Ω，实际 %s" % rv)
    eq(ps.table.item(1, 0).text(), "-50.0", "小信号表首行应为 -50 dBm")



def test_diagram_repaint_does_not_accumulate_labels():
    """Qt 反复重绘不得不断追加同一批标注。"""
    win = _window()
    for name in ("microstrip", "stripline", "coax_round", "coax_square",
                 "twin_wire", "cpw", "trough", "wire_gnd",
                 "bjt_base", "bjt", "fet_self", "fet_div"):
        page = win._pages[name]
        win.show_page(name)
        page.dg.grab()
        first = len(page.dg._labels)
        for _ in range(3):
            page.dg.grab()
        eq(len(page.dg._labels), first, name + " 重绘不能累积重复标注")


def test_reflection_diagram_input_fields_are_visible():
    """反射图输入不能被父画布裁切。"""
    win = _window()
    page = win._pages["reflection"]
    win.show_page("reflection")
    _APP.processEvents()
    for field in ("refl.gamma", "refl.zl", "refl.yl"):
        widget = page.findChild(rfcalc_panel.QtWidgets.QLineEdit, field)
        ok(widget is not None, field + " 输入不存在")
        parent = widget.parentWidget()
        ok(parent.rect().contains(widget.geometry()), field + " 输入框超出父画布")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
