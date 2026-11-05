# -*- coding: utf-8 -*-
"""离散选项表（原厂料号）的纯逻辑测试：解析 / 枚举 / 校验 / 检索。

覆盖（全部不需要 ADS 进程，用 fake formset 与 fake model_def）：
  * 物理量解析：'2.2[nH]' / '+/-0.5[nH]' / '0.85 mm' / '50[MHz]-20[GHz]'
  * 原厂 label 解析：料号 / Type / Value / Tolerance / RatedCurrent / FreqRange
  * form 分类与网表值：常量 form 的 net_format 是序号，'%d' 模板不算值
  * 选项表枚举：真实枚举结果、缓存、limit 截断、读不到 forms 时如实说明
  * 值与选项表核对：form 名 / 料号 / 网表序号三种写法；错误值给最接近的料号
  * search_model_parts 的过滤与容差比较
  * _check_parameters 把选项表核对结果并入 problems/warnings/entry

**这里测不到**：真实 keysight.ads.de 的 Formset/ConstForm 行为 —— 已在
ADS 实机取证（PartNumber.formset 有 101 个 form，net_format 是序号），
用例里的 fake 只是按实测形状构造，不是对 SDK 的替代验证。

运行: python tests/test_model_part_choices.py
"""

import math
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import model_ops as M  # noqa: E402

FAILS = []
_COUNT = [0]


def check(name, got, want):
    _COUNT[0] += 1
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")
        print(f"  FAIL {name}: got {got!r} want {want!r}")
    else:
        print(f"  ok{name}")


def close(name, got, want, tol=1e-12):
    _COUNT[0] += 1
    try:
        ok = got is not None and abs(float(got) - float(want)) <= tol
    except (TypeError, ValueError):
        ok = False
    if not ok:
        FAILS.append(f"{name}: got {got!r} want ~{want!r}")
        print(f"  FAIL {name}: got {got!r} want ~{want!r}")
    else:
        print(f"  ok{name}")


# --- fake SDK 形状（按 2026-11-05 ADS 实测构造，不是猜的）-----------------

class ConstForm:
    """常量 form：name=料号，label 是长标签，net_format 是网表序号。"""

    def __init__(self, name, label="", net_format="", display_format=""):
        self.name = name
        self.label = label or name
        self.net_format = net_format
        self.display_format = display_format
        self.discrete = True

    def is_constant_form(self, form=None):
        return True


class CompoundForm:
    def __init__(self, name, label=""):
        self.name = name
        self.label = label or name


class StringForm:
    def __init__(self, name="string_form", label="text"):
        self.name = name
        self.label = label


class FakeFormset:
    def __init__(self, name, forms):
        self.name = name
        self.forms = list(forms)

    def find_form_by_name(self, value):
        for form in self.forms:
            if str(getattr(form, "name", "")) == str(value):
                return form
        return None


class BrokenFormset:
    """读不到 forms 的 formset（旧测试里的 fake 就长这样）。"""

    def __init__(self, name):
        self.name = name

    def find_form_by_name(self, value):
        return None


PART_LABELS = [
    ("NULL", "NULL", "NULL", "NULL"),
    ("LQW18AN2N2D00",
     "1:LQW18AN2N2D00, Wound, 2.2[nH], +/-0.5[nH], 700[mA], 50[MHz]-20[GHz]:1",
     "1", "LQW18AN2N2D00"),
    ("LQW18AN3N6C00",
     "2:LQW18AN3N6C00, Wound, 3.6[nH], +/-0.2[nH], 850[mA], 50[MHz]-20[GHz]:2",
     "2", "LQW18AN3N6C00"),
    ("LQW18AN18NG00",
     "3:LQW18AN18NG00, Wound, 18[nH], +/-2[nH], 400[mA], 50[MHz]-6[GHz]:3",
     "3", "LQW18AN18NG00"),
]


def part_formset(name="LQW18AN_00_list_opt"):
    return FakeFormset(name, [ConstForm(n, l, net, disp)
                              for n, l, net, disp in PART_LABELS]
                       + [CompoundForm("LQW18AN_00_compound", "compound")])


def part_param(name="PartNumber", formset=None):
    return types.SimpleNamespace(
        name=name, label="Component Part Number",
        param_type=types.SimpleNamespace(value="string"),
        unit_type=types.SimpleNamespace(value=""),
        default_value=None,
        formset=formset if formset is not None else part_formset(),
        is_editable=True, is_netlistable=True,
        is_discrete_value=False, is_optimizable=False, is_constant=False)


def fake_model_def(params):
    return types.SimpleNamespace(parameters=list(params))


# --- 1. 物理量解析 --------------------------------------------------------
print("== 1. 物理量解析 ==")
check("(1) 引号剥离", M._strip_quotes('"18nH"'), "18nH")
check("(2) None 安全", M._strip_quotes(None), "")
check("(3) 空串安全", M._strip_quotes("  "), "")
check("(4) 无单位", M._unit_family(""), ("", 1.0, ""))
fam = M._unit_family("nH")
check("(5) nH 族", fam[0], "inductance")
close("(6) nH 因子", fam[1], 1e-9)
check("(7) 认不出单位原样返回", M._unit_family("Bogus"), ("", 1.0, "Bogus"))
q = M._parse_quantity("2.2[nH]")
check("(8) 方括号单位", q["unit"], "nH")
close("(9) 2.2nH -> SI", q["value"], 2.2e-9)
check("(10) 不是公差", q["plus_minus"], False)
q = M._parse_quantity("+/-0.5[nH]")
check("(11) 公差标记", q["plus_minus"], True)
close("(12) 公差取幅值", q["value"], 0.5e-9)
q = M._parse_quantity("0.85 mm")
check("(13) 空格单位", q["unit"], "mm")
close("(14) 长度族因子", q["value"], 8.5e-4)
q = M._parse_quantity("700[mA]")
check("(15) 电流族", q["family"], "current")
close("(16) mA -> A", q["value"], 0.7)
check("(17) 非数值返回 None", M._parse_quantity("Wound"), None)
check("(18) 范围拆分", M._split_quantity_range("50[MHz]-20[GHz]"),
      ("50[MHz]", "20[GHz]"))
check("(19) 范围拆不出", M._split_quantity_range(""), ("", ""))

# --- 2. 原厂 label 解析 ---------------------------------------------------
print("== 2. 原厂 label 解析 ==")
d = M._parse_choice_label(
    "1:LQW18AN2N2D00, Wound, 2.2[nH], +/-0.5[nH], 700[mA], 50[MHz]-20[GHz]:1")
check("(1) 料号", d["part_number"], "LQW18AN2N2D00")
check("(2) Type", d["part_type"], "Wound")
check("(3) Value", d["value"], "2.2[nH]")
close("(4) Value SI", d["value_si"], 2.2e-9)
check("(5) 族", d["family"], "inductance")
check("(6) Tolerance", d["tolerance"], "+/-0.5[nH]")
close("(7) Tolerance SI", abs(d["tolerance_si"]), 0.5e-9)
check("(8) RatedCurrent", d["rated_current"], "700[mA]")
close("(9) RatedCurrent SI", d["rated_current_si"], 0.7, 1e-12)
check("(10) FreqRange", d["freq_range"], "50[MHz]-20[GHz]")
close("(11) 频率下限", d["freq_start_si"], 50e6)
close("(12) 频率上限", d["freq_stop_si"], 20e9)
check("(13) 序号", d["index"], 1)
check("(14) parsed", d["parsed"], True)
d = M._parse_choice_label("NULL")
check("(15) NULL 不算料号", (d["part_number"], d["parsed"]), ("", False))
check("(16) 空 label 不炸", M._parse_choice_label(None)["parsed"], False)

# --- 3. form 分类与网表值 -------------------------------------------------
print("== 3. form 分类与网表值 ==")
cform = ConstForm("LQW18AN2N2D00", "1:...", "1", "LQW18AN2N2D00")
check("(1) 常量 form", M._form_kind(cform), "constant")
check("(2) 复合 form", M._form_kind(CompoundForm("c")), "compound")
check("(3) 字符串 form", M._form_kind(StringForm()), "string")
check("(4) 网表值取 net_format", M._form_netlist_value(cform), "1")
tmpl = types.SimpleNamespace(name="layer_text_form", net_format="%d",
                             display_format="%v")
check("(5) 模板不做网表值", M._form_netlist_value(tmpl), "")
check("(6) 模板回落到 display_format", M._form_netlist_value(
    types.SimpleNamespace(name="x", net_format="%d", display_format="bond:drawing")),
    "bond:drawing")
check("(7) display 优先 label", M._form_display_value(cform), "LQW18AN2N2D00")

# --- 4. 选项表枚举 --------------------------------------------------------
print("== 4. 选项表枚举 ==")
param = part_param()
ch = M._enumerate_choices(param)
check("(1) 可用", ch["available"], True)
check("(2) 总数", ch["count"], 5)
check("(3) formset 名", ch["formset"], "LQW18AN_00_list_opt")
check("(4) 允许自由文本", ch["free_text_allowed"], False)
check("(5) kind 统计", ch["kinds"], {"constant": 4, "compound": 1})
check("(6) NULL 标占位", ch["items"][0].get("placeholder"), True)
check("(7) 料号项", ch["items"][1]["part_number"], "LQW18AN2N2D00")
check("(8) 料号项网表值", ch["items"][1]["netlist_value"], "1")
ch2 = M._enumerate_choices(param, limit=2)
check("(9) 截断后条数", len(ch2["items"]), 2)
check("(10) 截断计数", ch2["truncated"], 3)
# 缓存：同一 formset 名再枚举，即使 forms 变了也命中缓存
param.formset.forms = [ConstForm("LQW18AN2N2D00")]
ch3 = M._enumerate_choices(param)
check("(11) 命中缓存", ch3["count"], 5)
ch4 = M._enumerate_choices(param, refresh=True)
check("(12) refresh 重读", ch4["count"], 1)
broken = M._enumerate_choices(part_param(formset=BrokenFormset("Broken_x")))
check("(13) 读不到 forms 不伪装", broken["available"], False)
check("(14) 给出原因", "formset.forms" in (broken.get("reason") or ""), True)
nofs = M._enumerate_choices(types.SimpleNamespace(name="NoFormset"))
check("(15) 无 formset", nofs["available"], False)
check("(16) 无 formset 原因", "没有 formset" in (nofs.get("reason") or ""), True)
onlytxt = FakeFormset("StdFormSet_only_text", [StringForm()])
ch5 = M._enumerate_choices(part_param("Note", onlytxt))
check("(17) 全字符串 form", ch5["kinds"], {"string": 1})
check("(18) 字符串 form 允许文本", ch5["free_text_allowed"], True)

# --- 5. 选型参数判定 ------------------------------------------------------
print("== 5. 选型参数判定 ==")
sel = M._param_selection(part_param(formset=part_formset("LQW18AN_00_list_sel")),
                         with_count=True)
check("(1) 选型计数", sel["count"], 4)
check("(2) 可选项（去掉占位）", sel["selectable"], 3)
check("(3) 示例 form", sel["example"], "LQW18AN2N2D00")
check("(4) 不允许自由文本", sel["free_text_allowed"], False)
check("(5) 字符串参数不是选型", M._param_selection(
    part_param("Note", FakeFormset("StdFormSet_txt", [StringForm()])),
    with_count=True), {})
check("(6) 无 formset 参数不是选型", M._param_selection(
    types.SimpleNamespace(name="X")), {})

# --- 6. 值与选项表核对 ----------------------------------------------------
print("== 6. 值与选项表核对 ==")
choices = M._enumerate_choices(part_param(), refresh=True)
m = M._match_choice("PartNumber", "LQW18AN3N6C00", choices)
check("(1) form 名直接命中", m["selected_form"], "LQW18AN3N6C00")
check("(2) 无问题", m["problems"], [])
check("(3) 选了料号没 warning", m["warnings"], [])
m = M._match_choice("PartNumber", "2", choices)
check("(4) 网表序号命中", m["selected_form"], "LQW18AN3N6C00")
check("(5) 序号命中给 warning", len(m["warnings"]), 1)
check("(6) warning 提到网表序号", "网表序号" in m["warnings"][0], True)
m = M._match_choice("PartNumber", '"LQW18AN2N2D00"', choices)
check("(7) 带引号也命中", m["selected_form"], "LQW18AN2N2D00")
m = M._match_choice("PartNumber", "18nH", choices)
check("(8) 自填数值被拒", bool(m["problems"]), True)
check("(9) 没选到 form", m["selected_form"], "")
check("(10) 按值给建议", "LQW18AN18NG00" in (m["hint"].get("by_value") or []), True)
check("(11) 提示检索工具", "search_model_parts" in m["problems"][0], True)
check("(12) 问题里带 formset", "LQW18AN_00_list_opt" in m["problems"][0], True)
m = M._match_choice("PartNumber", "LQW18AN2N2D0", choices)
check("(13) 名字近似建议", "LQW18AN2N2D00" in (m["hint"].get("part_numbers") or []),
      True)
free_choices = dict(choices)
free_choices["free_text_allowed"] = True
m = M._match_choice("PartNumber", "随便写", free_choices)
check("(14) 允许自由文本时不判错", m["problems"], [])

# --- 7. 检索过滤与容差 ----------------------------------------------------
print("== 7. 检索过滤与容差 ==")
items = {i["form"]: i for i in choices["items"] if i.get("kind") == "constant"}
spec = {"value": M._parse_quantity("18nH")}
check("(1) 值命中 18nH", M._search_matches(items["LQW18AN18NG00"], spec), True)
check("(2) 值不命中 2.2nH", M._search_matches(items["LQW18AN2N2D00"], spec), False)
spec = {"value": M._parse_quantity("2.4[nH]"),
        "value_tolerance": M._value_tolerance_spec(
            M._parse_quantity("2.2[nH]"), "10%")}
check("(3) 10% 容差命中 2.2nH", M._search_matches(items["LQW18AN2N2D00"], spec), True)
spec = {"value": M._parse_quantity("2.4[nH]"),
        "value_tolerance": M._value_tolerance_spec(
            M._parse_quantity("2.2[nH]"), "0.1%")}
check("(4) 0.1% 容差不命中", M._search_matches(items["LQW18AN2N2D00"], spec), False)
spec = {"value": M._parse_quantity("2.2[nH]"),
        "value_tolerance": M._value_tolerance_spec(
            M._parse_quantity("2.2[nH]"), "0.5[nH]")}
check("(5) 绝对容差 0.5nH 含 2.2nH", M._search_matches(items["LQW18AN2N2D00"], spec), True)
tgt = M._parse_quantity("2.2[nH]")
check("(6) 绝对容差解析", M._value_tolerance_spec(tgt, "0.5[nH]"),
      (1e-9, 0.5e-9))
rel, _abs = M._value_tolerance_spec(tgt, "5%")
close("(7) 相对容差解析", rel, 0.05, 1e-9)
check("(8) 容差同族才比较", M._search_matches(
    items["LQW18AN2N2D00"], {"tolerance": M._parse_quantity("0.5[nH]")}), True)
check("(9) 容差不同不命中", M._search_matches(
    items["LQW18AN2N2D00"], {"tolerance": M._parse_quantity("0.2[nH]")}), False)
check("(10) 电流下限命中", M._search_matches(
    items["LQW18AN2N2D00"], {"rated_current_min": M._parse_quantity("500[mA]")}),
    True)
check("(11) 电流下限不命中", M._search_matches(
    items["LQW18AN2N2D00"], {"rated_current_min": M._parse_quantity("1[A]")}),
    False)
check("(12) 频率覆盖命中", M._search_matches(
    items["LQW18AN18NG00"], {"freq_min": M._parse_quantity("100[MHz]"),
                             "freq_max": M._parse_quantity("2[GHz]")}), True)
check("(13) 频率不覆盖", M._search_matches(
    items["LQW18AN18NG00"], {"freq_max": M._parse_quantity("10[GHz]")}), False)
check("(14) 占位默认排除", M._search_matches(items["NULL"], {}), False)
check("(15) 显式包含占位", M._search_matches(items["NULL"],
                                          {"include_placeholder": True}), True)
check("(16) 复合 form 不参与检索", M._search_matches(
    {"kind": "compound", "form": "x"}, {}), False)

# --- 8. _check_parameters 接入 -------------------------------------------
print("== 8. 参数校验接入 ==")
md = fake_model_def([part_param(formset=part_formset("LQW18AN_00_list_opt_chk"))])
res = M._check_parameters(md, {"PartNumber": "18nH"})
check("(1) 检查过", res["checked"], True)
check("(2) 选项表核对过", res["choice_checked"], True)
check("(3) 有 problems", len(res["problems"]), 1)
check("(4) entry 带 selection", res["matched"][0]["selection"]["formset"],
      "LQW18AN_00_list_opt_chk")
check("(5) 自填数值没有 selected_form",
      res["matched"][0]["selection"]["selected_form"], "")
check("(6) 带建议", "LQW18AN18NG00" in (
    res["matched"][0].get("suggestions", {}).get("by_value") or []), True)
res = M._check_parameters(md, {"PartNumber": "LQW18AN18NG00"})
check("(7) 正确料号无问题", res["problems"], [])
check("(8) 记下料号", res["matched"][0].get("selected_part_number"), "LQW18AN18NG00")
check("(9) 记下数值", res["matched"][0].get("selected_value"), "18[nH]")
check("(10) 记下 form", res["matched"][0]["selection"]["selected_form"],
      "LQW18AN18NG00")
res = M._check_parameters(md, {"PartNumber": "3"})
check("(11) 序号写法给 warning", len(res["warnings"]), 1)
res = M._check_parameters(fake_model_def([
    types.SimpleNamespace(name="Value", label="Value",
                          param_type=types.SimpleNamespace(value="string"),
                          unit_type=types.SimpleNamespace(value=""),
                          default_value=None,
                          formset=FakeFormset("StdFormSet_chk", [StringForm()]))]),
    {"Value": "18nH"})
check("(12) 自由文本参数不报错", res["problems"], [])
check("(13) 自由文本不算选型", res["choice_checked"], False)
res = M._check_parameters(fake_model_def([part_param(
    formset=part_formset("LQW18AN_00_list_opt_brk"))]), {"NoSuch": "1"})
check("(14) 未知参数仍是问题", len(res["unknown_params"]), 1)

# --- 9. 检索结果条目 ------------------------------------------------------
print("== 9. 检索结果条目 ==")
hit = M._search_hit("muRataLibWeb_L", "LQW18AN_00", "PartNumber",
                    items["LQW18AN18NG00"])
check("(1) master 拼接", hit["master"], "muRataLibWeb_L:LQW18AN_00")
check("(2) 保留料号", hit["part_number"], "LQW18AN18NG00")
check("(3) 保留网表值", hit["netlist_value"], "3")
check("(4) 保留 label", hit["label"].startswith("3:LQW18AN18NG00"), True)
check("(5) 保留频率范围", hit["freq_range"], "50[MHz]-6[GHz]")
check("(6) 数值可比较", hit["value_si"] is not None and
      math.isclose(hit["value_si"], 18e-9), True)

print()
if FAILS:
    print(f"结果: FAIL —— {len(FAILS)} 项失败")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"结果: PASS —— ({_COUNT[0]} 项通过)")
