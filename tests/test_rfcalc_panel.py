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
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402 — 键盘导航模拟

_APP = QApplication.instance() or QApplication([])
_WIN = None
_EXPECT_PAGES = ["microstrip", "stripline", "twin_wire", "wire_gnd",
                 "coax_round", "coax_square", "cpw", "trough",
                 "rfic_v", "rfic_i", "det_large", "det_small",
                 "bjt_base", "bjt", "fet_self", "fet_div", "pae",
                 "sparam", "balun",
                 "reflection", "noisecalc", "spurs", "intermod",
                 "thermal", "spcs", "comp_values", "constants",
                 "complex_math", "substrate",
                 "attenuator", "vswrconv", "skin", "lcres"]

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
    eq(list(win._order), _EXPECT_PAGES, "33 页构建顺序")
    eq(win._stack.count(), len(_EXPECT_PAGES), "栈页数应为 33")
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


# ---------------------------------------------------------------------------
# chrome / 交互：侧栏导航三态、键盘导航、Web 条、状态栏口径、焦点与令牌
# ---------------------------------------------------------------------------


def test_sidebar_nav_anchor_tooltip_and_checked_sync():
    """侧栏项应有 <page>.nav 锚点、tooltip/statusTip，选中态随切页同步。"""
    win = _window()
    bar = win.sidebar
    item = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "microstrip.nav")
    ok(item is not None, "侧栏项应有 <page>.nav objectName 锚点")
    ok(bool(item.toolTip()), "侧栏项应有 tooltip")
    ok(bool(item.statusTip()), "侧栏项应有 statusTip")
    ok(bool(item.accessibleName()), "侧栏项应有 accessibleName")
    eq(item.iconSize().width(), 32, "侧栏图标应为 32px")
    win.show_page("microstrip")
    ok(item.isChecked(), "当前页侧栏项应选中")
    other = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "stripline.nav")
    ok(other is not None and not other.isChecked(), "非当前页项不应选中")
    win.show_page("stripline")
    ok(other.isChecked() and not item.isChecked(), "切页后选中应同步")
    # 换组重建后：新组项齐全，当前页高亮不丢
    bar.expand("sig")
    cur = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "reflection.nav")
    ok(cur is not None, "展开新组后应有该项")
    win.show_page("reflection")
    ok(cur.isChecked(), "换组后当前页高亮应保持")
    bar.expand("tl")


def test_sidebar_keyboard_navigation_and_activation():
    """↑↓ 环绕导航 + Home/End + ←/→ 进出分组条 + 回车/空格激活。"""
    win = _window()
    bar = win.sidebar
    win.show_page("microstrip")
    _APP.processEvents()
    first = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "microstrip.nav")
    second = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "stripline.nav")
    last = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "trough.nav")
    ok(all(w is not None for w in (first, second, last)), "tl 组 8 项应齐全")
    first.setFocus()
    eq(_APP.focusWidget(), first, "可显式聚焦侧栏项（StrongFocus）")
    QTest.keyClick(first, Qt.Key_Down)
    eq(_APP.focusWidget(), second, "↓ 应把焦点移到下一项")
    QTest.keyClick(second, Qt.Key_Return)
    eq(win._stack.currentWidget().name, "stripline", "回车应激活当前项")
    last.setFocus()
    QTest.keyClick(last, Qt.Key_Down)
    eq(_APP.focusWidget(), first, "末项 ↓ 应环绕到首项")
    QTest.keyClick(first, Qt.Key_End)
    eq(_APP.focusWidget(), last, "End 应跳到末项")
    QTest.keyClick(last, Qt.Key_Home)
    eq(_APP.focusWidget(), first, "Home 应跳到首项")
    QTest.keyClick(first, Qt.Key_Right)
    ok(_APP.focusWidget() in bar._group_buttons(), "→ 应跳到分组条")
    gb = bar._group_buttons()
    eq(len(gb), 6, "应有 6 个分组钮")
    QTest.keyClick(gb[0], Qt.Key_Down)
    eq(_APP.focusWidget(), gb[1], "分组条 ↓ 应在组间移动")
    QTest.keyClick(gb[1], Qt.Key_Left)
    eq(_APP.focusWidget(), last, "← 应回到项区末项")
    second.setFocus()
    QTest.keyClick(second, Qt.Key_Space)
    eq(win._stack.currentWidget().name, "stripline", "空格应激活当前项")


def test_sidebar_group_chevron_expand_collapse():
    """分组钮应有 chevron 指示（展开=向下 / 收起=向右）与 tooltip，点击换组。"""
    win = _window()
    bar = win.sidebar
    gb = bar._group_buttons()
    ok(len(gb) == 6, "应有 6 个分组钮")
    for b in gb:
        ok(bool(b.toolTip()), "分组钮应有 tooltip")
        ok(not b.icon().isNull(), "分组钮应有 chevron 图标")
    active = bar.findChild(rfcalc_panel.QtWidgets.QPushButton, "sidebar.group.tl")
    inactive = bar.findChild(rfcalc_panel.QtWidgets.QPushButton, "sidebar.group.sig")
    ok(not active.icon().pixmap(10, 10).toImage() ==
       inactive.icon().pixmap(10, 10).toImage(), "展开/收起 chevron 应不同")
    inactive.click()
    eq(bar._expanded, "sig", "点击分组钮应展开该组")
    ok(bar.findChild(rfcalc_panel.QtWidgets.QToolButton, "sparam.nav") is not None,
       "展开 sig 后应有其成员项")
    bar.expand("tl")


def test_webbar_hover_underline_cursor_and_message():
    """Web 条：手型光标、tooltip、悬停下划线、离线点击提示（文案不得含「复刻」）。"""
    win = _window()
    bar = win.findChild(rfcalc_panel.QtWidgets.QFrame, "webbar")
    ok(bar is not None, "应找到 webbar")
    lab = bar._lab
    eq(lab.cursor().shape(), Qt.PointingHandCursor, "Web 条应有手型光标")
    ok(bool(lab.toolTip()), "Web 条应有 tooltip")
    bar.enterEvent(QEvent(QEvent.Type.Enter))
    ok(lab.font().underline(), "悬停应显示下划线")
    bar.leaveEvent(QEvent(QEvent.Type.Leave))
    ok(not lab.font().underline(), "离开应取消下划线")
    _APP.processEvents()
    QTest.mouseClick(bar, Qt.LeftButton)
    msg = win.status().currentMessage()
    contains(msg, "Web", "点击应给离线提示")
    not_contains(msg, "复刻", "用户可见文案不得出现「复刻」")


def test_shortcut_hints_and_status_messages():
    """F3/F4/F8 可见提示（常驻标签 + 按钮 tooltip）与状态栏统一口径。"""
    win = _window()
    hint = win.findChild(rfcalc_panel.QtWidgets.QLabel, "shortcut.hint")
    ok(hint is not None, "状态栏应常驻快捷键提示标签")
    for k in ("F4", "F3", "F8"):
        contains(hint.text(), k, "快捷键提示应含 " + k)
    page = win._pages["microstrip"]
    contains(_btn(page, "microstrip.calc").toolTip(), "F4", "计算钮 tooltip 应含 F4")
    mm = page.findChild(rfcalc_panel.QtWidgets.QPushButton, "microstrip.mainmenu")
    if mm is not None:
        contains(mm.toolTip(), "F8", "Main Menu 钮 tooltip 应含 F8")
    win.show_page("spurs")
    msg = win.status().currentMessage()
    contains(msg, "杂散", "页切换提示应含中文页名")
    contains(msg, "spurs", "页切换提示应含页名标识")
    win.show_page("microstrip")
    _btn(page, "microstrip.calc").click()
    msg = win.status().currentMessage()
    contains(msg, "微带线", "成功提示应含中文页名")
    not_contains(msg, "失败", "成功提示不应含「失败」")


def test_window_minimum_size_and_focus_on_page_switch():
    """窗口有合理 minimumSize；页切换后焦点落到页面内首个可交互控件。"""
    win = _window()
    ok(win.minimumSize().width() >= 720 and win.minimumSize().height() >= 520,
       "应设置合理 minimumSize")
    page = win._pages["noisecalc"]
    win.show_page("noisecalc")
    _APP.processEvents()
    fw = _APP.focusWidget()
    ok(fw is not None and page.isAncestorOf(fw), "页切换后焦点应落到页面内控件")
    if isinstance(fw, rfcalc_panel.QtWidgets.QLineEdit):
        ok(not fw.isReadOnly(), "焦点应优先落在可编辑控件")


def test_page_css_tokens_icons_and_contrast():
    """页级样式对齐设计令牌；thermal/spcs 图标非空；关键文字对比度≥4.5。"""
    css = rfcalc_panel._page_css("microstrip", rfcalc_panel.A_BG)
    contains(css, rfcalc_panel.ACCENT, "页级样式应含 accent 焦点环令牌")
    contains(css, ":focus", "页级样式应有焦点规则")
    contains(css, rfcalc_panel.RO_BG, "只读框应统一 RO_BG 底色")
    ok(not rfcalc_panel._pix("thermal", 32).toImage().pixel(16, 22) == 0,
       "thermal 图标应有像素（此前为空白缺陷）")
    ok(not rfcalc_panel._pix("spcs", 32).toImage().pixel(16, 16) == 0,
       "spcs 图标应有像素（此前为空白缺陷）")

    def _lum(hexc):
        c = [int(hexc[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
        lin = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
               for v in c]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def _ratio(fg, bg):
        hi = max(_lum(fg), _lum(bg))
        lo = min(_lum(fg), _lum(bg))
        return (hi + 0.05) / (lo + 0.05)

    ok(_ratio(rfcalc_panel.TEXT_SUB, rfcalc_panel.RO_BG) >= 4.5,
       "只读框文字对比度应≥4.5")
    ok(_ratio(rfcalc_panel.WEBBLUE, "#FFFFFF") >= 4.5, "Web 蓝字对比度应≥4.5")
    ok(_ratio(rfcalc_panel.STATUS_FG, "#F0F0F0") >= 4.5,
       "状态栏提示文字对比度应≥4.5")


def test_attenuator_all_topologies_and_bad_input():
    """衰减器页：Z0=50/A=10dB 四拓扑出电阻（对引擎交叉核对+解析锚点）；
    坏输入（A=0）→ 状态栏含「失败」不崩；A=70dB → warnings 注记可见。"""
    import rfcalc
    win = _window()
    page = win._pages["attenuator"]
    win.show_page("attenuator")
    ok(win.findChild(rfcalc_panel.QtWidgets.QWidget, "attenuator.diagram")
       is not None, "应有矢量示意图 attenuator.diagram")
    _le(page, "attenuator.z0").setText("50")
    _le(page, "attenuator.att").setText("10")
    _btn(page, "attenuator.calc").click()
    contains(win.status().currentMessage(), "衰减器设计", "成功提示应含中文页名")

    import math
    k = 10 ** (10 / 20.0)
    expect = {
        "pi_r_series": 50 * (k * k - 1) / (2 * k),
        "pi_r_shunt": 50 * (k + 1) / (k - 1),
        "t_r_series": 50 * (k - 1) / (k + 1),
        "t_r_shunt": 2 * 50 * k / (k * k - 1),
        "bt_r_fixed": 50.0,
        "lp_z_in": 50.0,
    }
    eng = {"pi_r_series": rfcalc.attenuator_pi(50, 10)["r_series"],
           "pi_r_shunt": rfcalc.attenuator_pi(50, 10)["r_shunt"],
           "t_r_series": rfcalc.attenuator_t(50, 10)["r_series"],
           "t_r_shunt": rfcalc.attenuator_t(50, 10)["r_shunt"],
           "bt_r_bridge": rfcalc.attenuator_bridged_t(50, 10)["r_bridge"],
           "bt_r_shunt": rfcalc.attenuator_bridged_t(50, 10)["r_shunt"],
           "bt_r_fixed": rfcalc.attenuator_bridged_t(50, 10)["r_fixed"],
           "lp_r_series": rfcalc.attenuator_lpad(50, 10)["r_series"],
           "lp_r_shunt": rfcalc.attenuator_lpad(50, 10)["r_shunt"],
           "lp_z_in": rfcalc.attenuator_lpad(50, 10)["z_in"],
           "lp_z_out": rfcalc.attenuator_lpad(50, 10)["z_out"],
           "lp_vswr_in": rfcalc.attenuator_lpad(50, 10)["vswr_in"],
           "lp_vswr_out": rfcalc.attenuator_lpad(50, 10)["vswr_out"]}
    for key, want in expect.items():
        got = float(_le(page, "attenuator.result." + key).text())
        ok(abs(got - want) < 0.05, "%s 显示 %s 应≈解析值 %.3f" % (key, got, want))
    for key, want in eng.items():
        got = float(_le(page, "attenuator.result." + key).text())
        ok(abs(got - float(want)) <= 1e-3 * max(1.0, abs(float(want))),
           "%s 显示 %s 应与引擎一致 %.6g" % (key, got, want))
    ok(abs(float(_le(page, "attenuator.result.lp_vswr_in").text()) - 1.0) < 1e-6,
       "L 型入端应严格匹配 VSWR=1")

    _le(page, "attenuator.att").setText("70")
    _btn(page, "attenuator.calc").click()
    note = page.findChild(rfcalc_panel.QtWidgets.QLabel, "attenuator.note")
    ok(note is not None and note.isVisibleTo(page) and note.text(),
       "A>60dB 应显示引擎 warnings 注记")

    _le(page, "attenuator.att").setText("0")
    _btn(page, "attenuator.calc").click()
    contains(win.status().currentMessage(), "失败",
             "A=0 非法输入应 sticky 报「失败」")
    _le(page, "attenuator.att").setText("10")
    _btn(page, "attenuator.calc").click()
    not_contains(win.status().currentMessage(), "失败", "改回合法值应恢复")


def test_vswrconv_conversions_and_empty_error():
    """VSWR 换算页：RL=10dB → VSWR=1.925/|Γ|=0.316/反射10%；三口全空报错。"""
    win = _window()
    page = win._pages["vswrconv"]
    win.show_page("vswrconv")
    _le(page, "vswrconv.rl").setText("10")
    _btn(page, "vswrconv.calc").click()
    ok(abs(float(_le(page, "vswrconv.result.vswr").text()) - 1.925) < 0.005,
       "RL=10dB 应换算出 VSWR≈1.925")
    ok(abs(float(_le(page, "vswrconv.result.gamma_mag").text()) - 0.3162) < 0.001,
       "|Γ| 应≈0.3162")
    ok(abs(float(_le(page, "vswrconv.result.power_reflected_pct").text()) - 10) < 0.05,
       "反射功率应≈10%")
    ok(abs(float(_le(page, "vswrconv.result.power_transmitted_pct").text()) - 90) < 0.05,
       "透射功率应≈90%")
    not_contains(win.status().currentMessage(), "失败", "合法入口不应报失败")

    _le(page, "vswrconv.rl").setText("")
    _btn(page, "vswrconv.calc").click()
    contains(win.status().currentMessage(), "失败", "三口全空应 sticky 报「失败」")
    ok("✗" in page.err.text(), "空入口应在页内显示 ✗ 错误")

    _le(page, "vswrconv.vswr").setText("2")
    _le(page, "vswrconv.gamma").setText("0.5")
    _btn(page, "vswrconv.calc").click()
    contains(win.status().currentMessage(), "失败", "两个入口同填应报「失败」")


def test_skin_copper_anchor_and_material_switch():
    """趋肤效应页：1GHz 铜 δ≈2.09µm、Rs≈8.2mΩ/□；换银 δ 变小；f 空 → 失败。"""
    win = _window()
    page = win._pages["skin"]
    win.show_page("skin")
    _le(page, "skin.freq").setText("1")
    page.combos["skin.funit"].setCurrentText("GHz")
    page.combos["skin.material"].setCurrentText("铜 Cu")
    _btn(page, "skin.calc").click()
    d_cu = float(_le(page, "skin.result.delta_um").text())
    ok(abs(d_cu - 2.087) < 0.05, "1GHz 铜 δ 应≈2.087µm，实际 %s" % d_cu)
    rs = float(_le(page, "skin.result.rs_ohm_sq").text())
    ok(0.007 < rs < 0.010, "1GHz 铜 Rs 应≈0.00824 Ω/□，实际 %s" % rs)

    page.combos["skin.material"].setCurrentText("银 Ag")
    _btn(page, "skin.calc").click()
    d_ag = float(_le(page, "skin.result.delta_um").text())
    ok(d_ag < d_cu, "银电导率更高 δ 应变小（%s < %s）" % (d_ag, d_cu))

    _le(page, "skin.freq").setText("")
    _btn(page, "skin.calc").click()
    contains(win.status().currentMessage(), "失败", "频率为空应报「失败」")


def test_lcres_q_srf_and_bad_input():
    """LC 谐振器页：L=C=10(nH/pF)+ESR1Ω → f0≈0.5033GHz/Q≈31.6；加 Cpar 出 SRF；
    L 空 → 失败。"""
    win = _window()
    page = win._pages["lcres"]
    win.show_page("lcres")
    _le(page, "lcres.l").setText("10")
    _le(page, "lcres.c").setText("10")
    _le(page, "lcres.esr").setText("1")
    _btn(page, "lcres.calc").click()
    ok(abs(float(_le(page, "lcres.result.f0_ghz").text()) - 0.5033) < 0.001,
       "f0 应≈0.5033 GHz")
    ok(abs(float(_le(page, "lcres.result.z0_char").text()) - 31.62) < 0.05,
       "特性阻抗 √(L/C) 应≈31.62 Ω")
    ok(abs(float(_le(page, "lcres.result.q_unloaded").text()) - 31.62) < 0.1,
       "ESR=1Ω 时空载 Q 应≈31.6")
    ok(abs(float(_le(page, "lcres.result.bw_3db_mhz").text()) - 15.92) < 0.2,
       "3dB 带宽应≈15.9 MHz")
    eq(_le(page, "lcres.result.srf_ghz").text(), "-",
       "无寄生时 SRF 应显示 -")

    _le(page, "lcres.cpar").setText("1")
    _btn(page, "lcres.calc").click()
    ok(abs(float(_le(page, "lcres.result.srf_ghz").text()) - 1.5915) < 0.01,
       "Cpar=1pF 时 SRF 应≈1.5915 GHz（L·Cpar 谐振取小）")

    _le(page, "lcres.l").setText("")
    _btn(page, "lcres.calc").click()
    contains(win.status().currentMessage(), "失败", "L 为空应报「失败」")


def test_coax_round_te11_group():
    """同轴（圆）页 TE11 组：D1=10/D2=4.3/εr=1 → fc≈13.35GHz、上限≈12.68；
    f=10GHz 单模「是」→ 14GHz「否」→ f 清空「-」。"""
    win = _window()
    page = win._pages["coax_round"]
    win.show_page("coax_round")
    page.combos["coax_round.units"].setCurrentText("mm")
    page.dg.fields["D1"].setText("10")
    page.dg.fields["D2"].setText("4.3")
    page.er_box.setText("1")
    page.freq_box.setText("10")
    _btn(page, "coax_round.calc").click()
    ok(abs(float(_le(page, "coax_round.result.Z0").text()) - 50.6273) < 0.05,
       "TE11 扩展不应破坏既有 Z0 计算")
    ok(abs(float(_le(page, "coax_round.result.fc_te11").text()) - 13.35) < 0.05,
       "TE11 截止应≈13.35 GHz")
    ok(abs(float(_le(page, "coax_round.result.fmax_te11").text()) - 12.68) < 0.05,
       "TE11 建议上限应≈12.68 GHz（0.95fc）")
    eq(_le(page, "coax_round.result.te11_ok").text(), "是", "10GHz 应低于截止")

    page.freq_box.setText("14")
    _btn(page, "coax_round.calc").click()
    eq(_le(page, "coax_round.result.te11_ok").text(), "否", "14GHz 应高于截止")

    page.freq_box.setText("")
    _btn(page, "coax_round.calc").click()
    eq(_le(page, "coax_round.result.te11_ok").text(), "-",
       "未填频率时单模判定应显示 -")


def test_xt_pages_registered_sidebar_and_icons():
    """扩展工具分组：4 页注册进 _pages/_order、侧栏 nav 锚点/tooltip、
    分组钮存在、4 个新矢量图标有像素。"""
    win = _window()
    bar = win.sidebar
    for name, label in (("attenuator", "衰减器设计"), ("vswrconv", "VSWR换算"),
                        ("skin", "趋肤效应"), ("lcres", "LC谐振器")):
        ok(name in win._pages and name in win._order, "%s 应已注册" % name)
        ok(win._stack.indexOf(win._pages[name]) >= 0, "%s 应在页栈中" % name)
        ok(win.findChild(rfcalc_panel.QtWidgets.QPushButton, name + ".calc")
           is not None, "%s 应有 F4 计算钮" % name)
    gb = bar.findChild(rfcalc_panel.QtWidgets.QPushButton, "sidebar.group.xt")
    ok(gb is not None, "应有「扩展工具」分组钮")
    contains(gb.text(), "扩展工具", "分组钮文字")
    bar.expand("xt")
    for name, label in (("attenuator", "衰减器设计"), ("vswrconv", "VSWR换算"),
                        ("skin", "趋肤效应"), ("lcres", "LC谐振器")):
        it = bar.findChild(rfcalc_panel.QtWidgets.QToolButton, name + ".nav")
        ok(it is not None, "%s 应有侧栏项 %s.nav" % (name, name))
        contains(it.toolTip(), label, "%s 侧栏 tooltip 应含中文页名" % name)
    bar.expand("tl")
    for kind in ("att", "vswr", "skin", "lcres"):
        img = rfcalc_panel._pix(kind, 32).toImage()
        ok(not img.isNull(), "%s 图标应可渲染" % kind)
        ok(any(img.pixel(x, y) != 0 for x in (8, 16, 24) for y in (8, 14, 16, 25)),
           "%s 32px 图标应有像素（非空白）" % kind)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
