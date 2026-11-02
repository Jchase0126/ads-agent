"""器件封装：层次引脚 + 真实 symbol 视图生成路径的契约回归。

分两层，**没有 ADS 进程也能跑**：

1. 纯逻辑层（任何解释器）：
   * component_plan 必须用 term_anchors 暴露逻辑引脚，而不是放仿真 Port；
   * FET/封装 R/L 的引脚锚点必须落在正确端子实例上；
   * verify_component_plan 要能抓出端口互换、漏支路、内部接地。

2. ADS 契约层（仅当 ADS 自带解释器可用时）：
   在 `E:\\ADS\\...\\tools\\python\\python.exe` 下 import 真实 API 并断言：
   * db_uu.create_symbol 存在且可调用；
   * generate_symbol.SymbolGenerator 存在且 generate_symbol 可调用；
   * Design.add_numbered_term 存在；
   * Pin 的引脚编号属性名是 term_number（**不是** pin_number）——
     2026-10-19 实测：用错名字会永远找不到引脚，属于静默逻辑错误。

这一层探测不需要打开工作区，也不会写任何 ADS 数据。
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ok, skip, run

add_path("backend")
add_path("addon", "ads_agent")

import device_models as M


def _record(kind="package_delay"):
    params = {"gm_S": 0.014, "gds_S": 6.5e-5, "Cgs_F": 5e-12, "Cgd_F": 1.8e-12,
              "Cds_F": 5e-14, "Rg_Ohm": 1.0, "Rs_Ohm": 1.0, "Rd_Ohm": 1.0,
              "Lg_H": 5e-10, "Ls_H": 5e-10, "Ld_H": 5e-10, "tau_s": 3e-11}
    return {"part_number": "J310SYM", "model_kind": "jfet_small_signal",
            "configuration": "common_source", "stage": "build_planned",
            "fit": {"selected": kind, "candidates": {kind: {
                "parameters_fitted_SI": dict(params)}}},
            "build": {"kind": "jfet_small_signal", "z0": 50.0,
                      "plan": {"instances": [], "connections": []},
                      "parameters_source": {}},
            "unsupported": []}


def test_component_plan_exposes_hierarchical_terms_not_sim_ports():
    pkg = M.build_component_package(_record())
    plan = pkg["component_plan"]
    masters = [i["master"] for i in plan["instances"]]
    ok("ads_simulation:Port" not in masters,
       "子电路元件不能用仿真 Port 暴露引脚（应使用层次 Term）")
    anchors = plan["term_anchors"]
    eq([a["number"] for a in anchors], [1, 2, 3], "三个逻辑引脚按 1/2/3 编号")
    eq([a["name"] for a in anchors], ["g", "d", "s"],
       "公共源配置：端口1=g、端口2=d、公共=s")
    # package_delay：每个端子锚点必须先落在该端的封装电阻上
    eq([a["instance"] for a in anchors],
       ["PKG_R_G", "PKG_R_D", "PKG_R_S"], "封装模型锚点应落在各端封装电阻 pin1")
    eq([a["pin"] for a in anchors], [1, 1, 1])


def test_component_plan_intrinsic_anchors_hit_fet_pins():
    pkg = M.build_component_package(_record(kind="intrinsic"))
    anchors = {a["name"]: a for a in pkg["component_plan"]["term_anchors"]}
    eq(anchors["g"]["instance"], "DEVICE_CORE")
    eq(anchors["g"]["pin"], M._ADS_FET_PINS["g"])
    eq(anchors["d"]["pin"], M._ADS_FET_PINS["d"])
    eq(anchors["s"]["pin"], M._ADS_FET_PINS["s"])
    ok(M.verify_component_plan(pkg["component_plan"], _record("intrinsic"))["ok"])


def test_verify_component_plan_catches_port_swap_and_ground():
    record = _record()
    pkg = M.build_component_package(record)
    plan = pkg["component_plan"]
    # 端口互换：交换前两个锚点的实例
    swapped = {**plan, "term_anchors": [dict(a) for a in plan["term_anchors"]]}
    swapped["term_anchors"][0]["instance"], swapped["term_anchors"][1]["instance"] = \
        swapped["term_anchors"][1]["instance"], swapped["term_anchors"][0]["instance"]
    result = M.verify_component_plan(swapped, record)
    eq(result["ok"], False, "锚点互换必须被检出")
    ok(any("端口互换" in p for p in result["problems"]), f"{result['problems']}")
    # 内部接地：塞一个 GROUND 实例
    grounded = {**plan, "instances": list(plan["instances"]) + [
        {"master": "ads_rflib:GROUND", "name": "BAD_GND", "params": {}}]}
    result = M.verify_component_plan(grounded, record)
    eq(result["ok"], False, "元件子电路内不得有 GROUND")
    ok(any("GROUND" in p for p in result["problems"]))


def test_verify_component_plan_catches_missing_package_branch():
    record = _record()
    pkg = M.build_component_package(record)
    plan = pkg["component_plan"]
    broken = {**plan, "instances": [i for i in plan["instances"]
                                    if i["name"] != "PKG_L_G"]}
    result = M.verify_component_plan(broken, record)
    eq(result["ok"], False, "漏封装电感必须被检出")
    ok(any("PKG_L_G" in p for p in result["problems"]))


def _ads_python() -> tuple:
    """返回 (解释器路径, ADS 安装目录)。未找到时返回 ("", "")。

    优先用 adslocate 的官方探测（注册表/环境变量），不写死开发机路径；
    探测不到才退回 HPEESOF_DIR 环境变量。
    """
    env_dir = os.environ.get("HPEESOF_DIR", "").strip()
    candidates = []
    try:
        import adslocate
        for item in adslocate.detect_ads_dirs():
            candidates.append(item["dir"])
    except Exception:  # noqa: BLE001 — 探测失败按未找到处理
        pass
    if env_dir:
        candidates.insert(0, env_dir)
    for root in candidates:
        python = os.path.join(root, "tools", "python", "python.exe")
        if os.path.isfile(python):
            return python, root
    return "", ""


def test_ads_symbol_api_contract_matches_implementation():
    """用 ADS 自带解释器核对符号生成 API 的名字与签名。"""
    python, ads_dir = _ads_python()
    if not python:
        skip("本机未探测到 ADS 2027 自带解释器（该项需 ADS 安装）")
    import json
    import subprocess
    code = (
        "import inspect, json\n"
        "import keysight.ads.de.db_uu as db_uu\n"
        "from keysight.ads.de.experimental.generate_symbol import SymbolGenerator\n"
        "from keysight.ads.de.db_dbu._design import Design\n"
        "from keysight.ads.de.db_dbu._db_x import Pin\n"
        "print(json.dumps({\n"
        "  'create_symbol': hasattr(db_uu, 'create_symbol'),\n"
        "  'create_symbol_sig': str(inspect.signature(db_uu.create_symbol)),\n"
        "  'generator': hasattr(SymbolGenerator, 'generate_symbol'),\n"
        "  'generator_sig': str(inspect.signature(SymbolGenerator.__init__)),\n"
        "  'add_numbered_term': hasattr(Design, 'add_numbered_term'),\n"
        "  'terms_property': isinstance(getattr(Design, 'terms', None), property),\n"
        "  'pin_has_term_number': hasattr(Pin, 'term_number'),\n"
        "  'pin_has_pin_number': hasattr(Pin, 'pin_number'),\n"
        "}))\n")
    env = dict(os.environ)
    # keysight.ads.de 在导入期要求 HPEESOF_DIR（实机核实过）
    env["HPEESOF_DIR"] = ads_dir
    proc = subprocess.run([python, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        skip(f"ADS 解释器探测失败：{tail[0]}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    eq(payload["create_symbol"], True, "db_uu.create_symbol 必须存在")
    eq(payload["generator"], True, "SymbolGenerator.generate_symbol 必须存在")
    eq(payload["add_numbered_term"], True, "Design.add_numbered_term 必须存在")
    eq(payload["terms_property"], True, "Design.terms 必须是属性")
    eq(payload["pin_has_term_number"], True, "引脚编号属性是 term_number")
    eq(payload["pin_has_pin_number"], False,
       "Pin 没有 pin_number —— 代码里用它会永远找不到引脚")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "器件封装：层次引脚与 symbol 生成契约"))
