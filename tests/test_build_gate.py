"""设计写入安全 + 仿真门禁测试（不需要 ADS —— keysight 导入都是延迟的）。

覆盖日志复盘定出的 P0/P1 优化项：

* P0-1  写入前自动备份：_esc_oa/_unescape_oa 转义规则、_backup_from_path
        的复制 / 保留份数 / 大小上限 / 新 cell 无可备份；
* P0-2  仿真门禁 _gate_problems：空设计、缺控制器、基板引用断裂、
        有控制器没端口、全部通过 -> 空列表；悬空引脚进 warnings 不拦截；
* P1-1  固定操作 build_schematic 的参数校验（在碰工作区之前就拦截）、
        _check_connection_list / _verify_params 的判定规则；
* P1-2  tools.describe_result 三态（失败 / 请求成功脚本执行失败 / 成功），
        agent.Turn 的每轮汇总计数。

运行::

    python tests/test_build_gate.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, os.path.join(ROOT, "backend"), os.path.join(ROOT, "addon", "ads_agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

from _harness import add_path, contains, eq, ne, ok, raises, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

import ads_ops  # noqa: E402


# ---------------------------------------------------------------------------
# P0-1 备份
# ---------------------------------------------------------------------------

def test_oa_escape_rules():
    # 两个名字都来自 AI_lib 磁盘实测（tests/probes/S*.py）
    eq(ads_ops._esc_oa("Wilkinson_2G4"), "%Wilkinson_2%G4", "实测转义：Wilkinson_2G4")
    eq(ads_ops._esc_oa("AI_probe_k"), "%A%I_probe_k", "实测转义：AI_probe_k")
    eq(ads_ops._unescape_oa("%Wilkinson_2%G4"), "Wilkinson_2G4", "反解")
    eq(ads_ops._unescape_oa("%A%I_probe_k"), "AI_probe_k", "反解")


def test_backup_happy_path():
    with tempfile.TemporaryDirectory() as tmp:
        cell = os.path.join(tmp, "AI_lib", "%My_Cell")
        view = os.path.join(cell, "schematic")
        os.makedirs(view)
        with open(os.path.join(view, "sch.oa"), "w", encoding="utf-8") as f:
            f.write("fake-oa-content")
        ws_path = os.path.join(tmp, "wrk")
        os.makedirs(ws_path)

        info = ads_ops._backup_from_path(cell, "AI_lib", "My_Cell", ws_path)
        ok(info["backed_up"], f"备份应成功: {info}")
        ok(os.path.isdir(info["path"]), "备份目录存在")
        copied = os.path.join(info["path"], "schematic", "sch.oa")
        ok(os.path.isfile(copied), "sch.oa 被复制")
        with open(copied, encoding="utf-8") as f:
            eq(f.read(), "fake-oa-content", "内容一致")
        ok(info["path"].startswith(os.path.join(ws_path, ads_ops.BACKUP_ROOT)),
           "备份落在 workspace 的 ads_agent_backups 下")
        contains(info["path"], "AI_lib__My_Cell__", "目录名带 lib 与 cell")


def test_backup_prunes_old():
    with tempfile.TemporaryDirectory() as tmp:
        cell = os.path.join(tmp, "lib", "%C")
        os.makedirs(os.path.join(cell, "v"))
        with open(os.path.join(cell, "v", "sch.oa"), "w") as f:
            f.write("x")
        ws_path = os.path.join(tmp, "wrk")
        os.makedirs(ws_path)

        for _ in range(ads_ops.KEEP_BACKUPS + 3):
            info = ads_ops._backup_from_path(cell, "lib", "C", ws_path)
            ok(info["backed_up"], "每次都应成功")
        parent = os.path.join(ws_path, ads_ops.BACKUP_ROOT)
        kept = [d for d in os.listdir(parent) if d.startswith("lib__C__")]
        eq(len(kept), ads_ops.KEEP_BACKUPS, f"只保留最近 {ads_ops.KEEP_BACKUPS} 份")


def test_backup_size_cap_and_missing():
    with tempfile.TemporaryDirectory() as tmp:
        # 超大：用 seek 制造稀疏文件，不真占磁盘
        big = os.path.join(tmp, "lib", "%Big", "v")
        os.makedirs(big)
        with open(os.path.join(big, "sch.oa"), "wb") as f:
            f.seek(ads_ops._BACKUP_SIZE_CAP + 1)
            f.write(b"\0")
        ws_path = os.path.join(tmp, "wrk")
        os.makedirs(ws_path)
        info = ads_ops._backup_from_path(os.path.dirname(big), "lib", "Big", ws_path)
        ne(info["backed_up"], True, "超大目录跳过备份")
        contains(info["note"], "跳过", "跳过原因写清楚")

        info2 = ads_ops._backup_from_path("", "lib", "Ghost", ws_path)
        ne(info2["backed_up"], True, "不存在的 cell 没有可备份内容")
        contains(info2["note"], "没有可备份", "原因区分于复制失败")


# ---------------------------------------------------------------------------
# P0-2 门禁
# ---------------------------------------------------------------------------

def _audit(**over):
    base = {
        "n_instances": 7,
        "controllers": [{"name": "SP1", "master": "S_Param"}],
        "ports": [{"name": "Term1", "master": "Term"}],
        "others": [],
        "instances": [],
        "n_nets": 5,
        "nets": ["N1", "N2"],
        "pin_nets": {"N1": 2, "N2": 2},
        "floating_pins": [],
        "broken_substrate_refs": [],
    }
    base.update(over)
    return base


def test_gate_clean_design_passes():
    eq(ads_ops._gate_problems(_audit(), "L:C:schematic"), [], "齐备的设计无阻断")
    ok(ads_ops._gate_warnings(_audit()) == [], "无悬空即无警告")


def test_gate_empty_design_blocked():
    problems = ads_ops._gate_problems(_audit(n_instances=0, controllers=[]),
                                      "L:C:schematic")
    ok(len(problems) >= 2, "空设计 + 缺控制器都要点名")
    contains(problems[0], "是空的", "空设计排第一")


def test_gate_missing_controller_blocked():
    problems = ads_ops._gate_problems(_audit(controllers=[]), "L:C:schematic")
    contains("\n".join(problems), "没有仿真控制器", "点名缺控制器")
    contains("\n".join(problems), "No Simulation Component", "带上底层报错原文")


def test_gate_broken_substrate_blocked():
    audit = _audit(broken_substrate_refs=[
        {"instance": "ML1", "master": "MLIN", "ref": "MSUB1"}])
    problems = ads_ops._gate_problems(audit, "L:C:schematic")
    contains("\n".join(problems), "基板引用缺失", "点名基板问题")
    contains("\n".join(problems), "ML1", "点名哪个实例")
    contains("\n".join(problems), "ads_tlines:MSUB", "给出补法")


def test_gate_controller_without_ports_blocked():
    audit = _audit(ports=[])  # S_Param 需要端口
    problems = ads_ops._gate_problems(audit, "L:C:schematic")
    contains("\n".join(problems), "没有任何端口", "S_Param 无端口要拦")
    contains("\n".join(problems), "connect(d, t1, 2,", "默认 Term 信号脚应为 2")


def test_gate_blocks_shorted_term():
    problems = ads_ops._gate_problems(
        _audit(shorted_ports=[{"instance": "Term1", "net": "N1"}]), "L:C:schematic")
    contains("\n".join(problems), "Term1(N1)")
    contains("\n".join(problems), "同一网络")


def test_audit_detects_shorted_term_pins():
    from types import SimpleNamespace

    net = SimpleNamespace(name="N1")
    pins = [SimpleNamespace(net=net, inst_term=SimpleNamespace(
        is_numbered=True, term_number=n)) for n in (1, 2)]
    term = SimpleNamespace(inst_name="Term1", master_name="Term",
                           parameters=[], inst_pins=pins)
    design = SimpleNamespace(instances=[term], nets=[net])
    audit = ads_ops._design_audit(design)
    eq(audit["shorted_ports"], [{"instance": "Term1", "net": "N1"}])


def test_gate_dc_only_needs_no_port():
    audit = _audit(controllers=[{"name": "DC1", "master": "DC"}], ports=[])
    eq(ads_ops._gate_problems(audit, "L:C:schematic"), [], "DC 单独跑不需要端口")


def test_gate_floating_pins_are_warnings_only():
    audit = _audit(floating_pins=[{"instance": "ML1", "master": "MLIN", "pin": 2}])
    eq(ads_ops._gate_problems(audit, "L:C:schematic"), [], "悬空引脚不拦截")
    warns = ads_ops._gate_warnings(audit)
    ok(warns, "悬空引脚出现在 warnings")
    contains(warns[0], "ML1", "警告点名实例")


# ---------------------------------------------------------------------------
# P1-1 build_schematic / check_connections 的纯逻辑部分
# ---------------------------------------------------------------------------

def test_build_schematic_arg_validation():
    # 这些都在碰工作区之前就会被拦截
    raises(RuntimeError, lambda: ads_ops.build_schematic({"library": "", "cell": "C"}),
           "缺 library")
    raises(RuntimeError, lambda: ads_ops.build_schematic({"library": "L", "cell": "C"}),
           "instances 与 var 都为空")
    raises(RuntimeError, lambda: ads_ops.build_schematic(
        {"library": "L", "cell": "C",
         "instances": [{"master": "ads_tlines:MLIN", "name": "ML1", "x": 0}]}),
        "缺 y 坐标")
    raises(RuntimeError, lambda: ads_ops.build_schematic(
        {"library": "L", "cell": "C", "var": {"values": {}}}),
        "var.values 为空")


class _FakePin:
    def __init__(self, net, label=1, master_pin="p1"):
        self._net = net
        self.master_pin = master_pin
        self.inst_term = _FakeTerm(label)

    @property
    def net(self):
        return self._net


class _FakeTerm:
    def __init__(self, label):
        self._label = label
        self.is_numbered = isinstance(label, int)

    def __getattr__(self, item):
        # term_number / term_name 按引脚类型二选一有值（与实测行为一致）
        if item == "term_number":
            return self._label if self.is_numbered else None
        if item == "term_name":
            return None if self.is_numbered else self._label
        raise AttributeError(item)


class _FakeInst:
    def __init__(self, name, pins):
        self.inst_name = name
        self.inst_pins = pins


def _netobj(name):
    class N:
        pass
    n = N()
    n.name = name
    return n


def test_check_connection_list_verdicts():
    ia = _FakeInst("ML1", [_FakePin(_netobj("N_IN"), 1), _FakePin(_netobj("N_OUT"), 2)])
    ib = _FakeInst("T1", [_FakePin(_netobj("N_IN"), 1)])
    ic = _FakeInst("T2", [_FakePin(None, 1)])          # 悬空
    by_name = {"ML1": ia, "T1": ib, "T2": ic}

    res = ads_ops._check_connection_list(by_name, [
        {"a": ["ML1", 1], "b": ["T1", 1]},             # 同网络 -> ok
        {"a": ["ML1", 2], "b": ["T2", 1]},             # 一端悬空 -> failed
        {"a": ["ML1", 1], "b": ["Ghost", 1]},          # 实例不存在 -> failed
        {"a": ["ML1", 9], "b": ["T1", 1]},             # 引脚不存在 -> failed
    ])
    eq(res[0]["status"], "ok", "同网络判 ok")
    eq(res[0]["net"], "N_IN", "给出网络名")
    eq(res[1]["status"], "failed", "一端悬空判失败")
    eq(res[2]["status"], "failed", "实例缺失判失败")
    eq(res[3]["status"], "failed", "引脚缺失判失败")
    contains(res[3]["reason"], "找不到引脚", "失败原因可操作")


def test_verify_params_unquote_compare():
    class P:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class I:
        inst_name = "ML1"
        parameters = [P("Subst", '"MSUB1"'), P("W", "W50")]

    by_name = {"ML1": I()}
    bad = ads_ops._verify_params(by_name, [
        {"name": "ML1", "params": {"Subst": "MSUB1", "W": "W50"}},   # 引号差异不算不一致
        {"name": "ML1", "params": {"L": "L50"}},                     # 缺参数
        {"name": "ML1", "params": {"W": "W70"}},                     # 值不一致
    ])
    eq([b["param"] for b in bad], ["L", "W"], "只报真正的差异")


def test_net_label_parsing():
    eq(ads_ops._net_label(None), "", "空网络")
    eq(ads_ops._net_label(_netobj("N_A")), "N_A", "有 name 属性直接取")
    eq(ads_ops._net_label('<ScalarNet "N_A">'), "N_A", "从 str 形式抠名字")


def test_bind_pins_merges_entire_existing_nets():
    class Net:
        def __init__(self, name):
            self.name = name

    class Pin:
        def __init__(self, net):
            self.net = net

    class Inst:
        def __init__(self, *pins):
            self.inst_pins = pins

    class Design:
        def __init__(self, *instances):
            self.instances = instances

    left, right = Net("LEFT"), Net("RIGHT")
    a, a_peer = Pin(left), Pin(left)
    b, b_peer = Pin(right), Pin(right)
    design = Design(Inst(a, a_peer), Inst(b, b_peer))
    ads_ops._bind_pins(design, a, b)
    ok(all(p.net is right for p in (a, a_peer, b, b_peer)),
       "合并已有网络时，旧网络的其它引脚也必须迁移")


def test_layout_scale_blocks_invisible_symbols():
    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class Pin:
        def __init__(self, x):
            self.snap_point = Point(x, 0)
            self.inst_term = type("Term", (), {"term_number": 1, "is_numbered": True})()

    class Inst:
        def __init__(self, name, x):
            self.name = name
            self.inst_pins = [Pin(x)]
            self.bbox = type("Box", (), {"lower_left": Point(x, 0),
                                           "upper_right": Point(x + 1, 1)})()

    class Design:
        def __init__(self, distance):
            self.instances = [Inst("A", 0), Inst("B", distance)]

    conn = [{"a": ["A", 1], "b": ["B", 1]}]
    eq(ads_ops._layout_scale_issue(Design(5), conn), "", "正常符号间距通过")
    contains(ads_ops._layout_scale_issue(Design(1000), conn), "符号",
             "千单位间距应被拦截，避免只看见线")


def test_verify_var_values_after_build():
    class Var:
        vars = {"f0": "2.4 GHz", "Riso": "100 Ohm"}

    spec = {"name": "VAR1", "values": {"f0": "2.4 GHz", "Riso": "100 Ohm"}}
    eq(ads_ops._verify_var_values({"VAR1": Var()}, spec), [], "变量落盘一致")
    bad = ads_ops._verify_var_values({"VAR1": Var()},
                                     {"name": "VAR1", "values": {"f0": "2.5 GHz"}})
    eq(bad[0]["param"], "f0", "变量不一致要指出变量名")
    eq(bad[0]["on_disk"], "2.4 GHz", "显示磁盘实际值")


# ---------------------------------------------------------------------------
# R17（2026-09-30）建图验收可信度：网表没拿到 / 核对崩溃 不得返回成功
# ---------------------------------------------------------------------------

_GK_LIB, _GK_CELL = "AI_t", "NetGateCase"
_NETLIST_OK = "R:R1  N__1 N__2 R=50 Ohm\n"
_GK_PARAMS = {"R": "50 Ohm"}
_GK_INST = [{"master": "ads_rflib:R", "name": "R1",
             "x": 0, "y": 0, "params": _GK_PARAMS}]


class _GateParam:
    def __init__(self, name, value):
        self.name, self.value = name, value


class _GateInst:
    def __init__(self, name, params):
        self.inst_name = name
        self.inst_pins = []
        self.parameters = [_GateParam(k, v) for k, v in params.items()]


class _GateWriteDesign:
    def __init__(self):
        self.instances = []

    def add_instance(self, master, xy, **kw):
        # 真实 ADS 实例的参数表在放置时已按主控预存在 —— 假件同样预置
        inst = _GateInst(str(kw.get("name")), dict(_GK_PARAMS))
        self.instances.append(inst)
        return inst

    def save_design(self):
        pass


def _run_gate_build(netlist_fn):
    """把 build_schematic 的 ADS 依赖全部换成本地假件，完整跑一遍流程。

    网表文本由 ``netlist_fn()`` 提供（可抛异常 / 返回空）——其余复核
    （实例、参数、几何）都安排成通过，隔离出"网表核对失败是否放行"
    这一个变量。
    """
    from unittest import mock

    write_design = _GateWriteDesign()
    ro_design = type("RO", (), {"instances": write_design.instances,
                                "generate_netlist": staticmethod(netlist_fn)})()
    audit = {"error": None, "n_instances": 1, "n_nets": 2}
    geo = {"n_segments": 0, "problems": [], "warnings": [],
           "unverified": [], "metrics": {}}
    args = {"library": _GK_LIB, "cell": _GK_CELL, "view": "schematic",
            "instances": _GK_INST, "connections": []}
    with tempfile.TemporaryDirectory() as tmp:
        patches = [
            mock.patch.object(ads_ops, "_require_workspace",
                              lambda: type("WS", (), {"path": tmp})()),
            mock.patch.object(ads_ops, "_ensure_cell_view",
                              lambda ws, l, c, v: {"created": False}),
            mock.patch.object(ads_ops, "_backup_design",
                              lambda ws, l, c: {"backed_up": True, "path": tmp}),
            mock.patch.object(ads_ops, "_db_uu", lambda: None),
            mock.patch.object(ads_ops, "_open_design",
                              lambda l, c, v, write=True:
                              write_design if write else ro_design),
            mock.patch.object(ads_ops, "_close_design", lambda d: None),
            mock.patch.object(ads_ops, "_design_audit", lambda d: dict(audit)),
            mock.patch.object(ads_ops, "_geometry_report", lambda d: dict(geo)),
            mock.patch.object(ads_ops, "_gate_problems", lambda a, n: []),
            mock.patch.object(ads_ops, "_gate_warnings", lambda a: []),
        ]
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return ads_ops.build_schematic(args)


def test_netlist_gate_semantics():
    backup = {"backed_up": True, "path": "E:/bk/AI_t__NetGateCase__t"}
    ref = f"{_GK_LIB}:{_GK_CELL}:schematic"

    # 一致网表：不拦截，ok=True
    problems, equiv = ads_ops._netlist_gate(_NETLIST_OK, _GK_INST, [], ref, backup)
    eq(problems, [], "一致网表不拦截")
    eq(equiv["ok"], True, "一致网表 ok=True")

    # 网表生成失败（异常）：必须拦，错误带原因/设计引用/备份位置
    problems, equiv = ads_ops._netlist_gate(
        "", _GK_INST, [], ref, backup, gen_error="网表生成失败: boom")
    eq(equiv["ok"], None, "未完成核对 ok=None")
    contains(problems[0], "boom", "错误含原因")
    contains(problems[0], ref, "错误含设计引用")
    contains(problems[0], "E:/bk/AI_t__NetGateCase__t", "错误含写入前备份位置")

    # 网表为空（未抛异常）：同样拦
    problems, equiv = ads_ops._netlist_gate("", _GK_INST, [], ref, backup)
    ok(problems, "空网表必须拦截")
    contains(problems[0], "空网表", "错误说明网表为空")
    contains(problems[0], ref, "错误含设计引用")
    eq(equiv["ok"], None, "空网表 ok=None")

    # 核对过程本身崩溃：拦，不能静默当通过
    from unittest import mock
    import netlist_check as nc_mod
    with mock.patch.object(nc_mod, "check_netlist_equivalence",
                           side_effect=ValueError("解析崩溃")):
        problems, equiv = ads_ops._netlist_gate(
            _NETLIST_OK, _GK_INST, [], ref, backup)
    ok(problems, "核对崩溃必须拦截")
    contains(problems[0], "解析崩溃", "错误含异常原因")
    contains(problems[0], ref, "错误含设计引用")
    eq(equiv["ok"], None, "核对崩溃 ok=None")


def test_netlist_gate_reports_diffs():
    backup = {"backed_up": False, "note": "没有可备份的内容（新建 cell）"}
    ref = f"{_GK_LIB}:{_GK_CELL}:schematic"
    # 计划侧把 R1.1-R1.2 连成一个网络，网表侧却分成两个节点 —— 划分不一致必须拦
    conns = [{"a": ["R1", "1"], "b": ["R1", "2"]}]
    problems, equiv = ads_ops._netlist_gate(
        "R:R1  N__1 N__3 R=50 Ohm\n", _GK_INST, conns, ref, backup)
    ok(problems, "划分不一致必须拦截")
    eq(equiv["ok"], False, "差异 ok=False")
    contains(problems[0], "缺失网络", "差异报告点名缺失的网络")
    contains(problems[0], "没有可备份", "无可备份时错误给出说明而非空路径")


def test_build_netlist_generation_error_blocks_success():
    def boom():
        raise RuntimeError("hpeesofsim 不可用")

    err = raises(RuntimeError, lambda: _run_gate_build(boom),
                 "网表生成失败不得返回成功")
    contains(str(err), "hpeesofsim 不可用", "错误含原因")
    contains(str(err), f"{_GK_LIB}:{_GK_CELL}:schematic", "错误含设计引用")
    contains(str(err), "写入前副本", "错误含写入前备份位置")


def test_build_empty_netlist_blocks_success():
    err = raises(RuntimeError, lambda: _run_gate_build(lambda: ""),
                 "空网表不得返回成功")
    contains(str(err), "空网表", "错误说明网表为空")
    contains(str(err), f"{_GK_LIB}:{_GK_CELL}:schematic", "错误含设计引用")
    contains(str(err), "写入前副本", "错误含写入前备份位置")


def test_build_netlist_check_crash_blocks_success():
    import netlist_check as nc_mod
    from unittest import mock

    def crash():
        with mock.patch.object(nc_mod, "check_netlist_equivalence",
                               side_effect=ValueError("解析崩溃")):
            return _run_gate_build(lambda: _NETLIST_OK)

    err = raises(RuntimeError, crash, "核对崩溃不得返回成功")
    contains(str(err), "解析崩溃", "错误含异常原因")
    contains(str(err), f"{_GK_LIB}:{_GK_CELL}:schematic", "错误含设计引用")


def test_build_netlist_ok_passes_with_equivalence():
    result = _run_gate_build(lambda: _NETLIST_OK)
    eq(result["netlist_equivalence"]["ok"], True, "一致网表 ok=True")
    eq(result["parameters_ok"], True, "参数复核通过")
    eq(result["backup"]["backed_up"], True, "报告写入前备份")
    ok("problems" not in result, "无问题时不带 problems 字段")


# ---------------------------------------------------------------------------
# P1-2 日志语义 + 每轮汇总
# ---------------------------------------------------------------------------

def test_describe_result_states():
    import tools

    eq(tools.describe_result("run_python", {"ok": False, "stdout": "x"}),
       ("warning", "请求成功、脚本执行失败"), "HTTP 200 + ok=false 必须说清是脚本失败")
    eq(tools.describe_result("run_python", {"error": "boom"}), ("warning", "失败"))
    eq(tools.describe_result("run_python", {"ok": True, "stdout": ""}), ("info", "成功"))
    eq(tools.describe_result("check_connections", {"design": "x"}), ("info", "成功"))


def test_turn_stats_and_summary():
    import agent

    calls = {"n": 0}

    def fake_chat_stream(cfg, messages, tools=None, timeout=240,
                         on_reasoning=None, on_content=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"tool_calls": [{"id": "1", "function": {
                "name": "run_python",
                "arguments": "{\"code\": \"print(1)\"}"}}]},
                {"prompt_tokens": 10, "completion_tokens": 5})
        return {"content": "结论"}, {"prompt_tokens": 20, "completion_tokens": 8}

    def fake_call(cfg, name, args):
        if name == "run_python":
            return {"ok": False, "stdout": "[异常] TypeError: x"}   # 请求成功、脚本失败
        return {}

    real_chat, real_call = agent.llm.chat_stream, agent.tools_mod.call
    agent.llm.chat_stream, agent.tools_mod.call = fake_chat_stream, fake_call
    try:
        turn = agent.Turn({"max_tool_steps": 3}, [{"role": "user", "content": "hi"}])
        events = []
        turn.run(events.append)
        st = turn.stats
        eq(st["calls"], 1, "一次工具调用")
        eq(st["failed"], 1, "脚本失败计入失败")
        eq(st["script_failed"], 1, "脚本失败单列")
        eq(st["sim_ok"], 0, "无仿真")
        kinds = [e["type"] for e in events]
        ok("done" in kinds, "轮次正常结束")
        done = [e for e in events if e["type"] == "done"][0]
        eq(done["stats"]["completion_tokens"], 13, "done 携带累计 completion tokens")
        eq(done["stats"]["prompt_tokens"], 30, "done 携带累计 prompt tokens")
        ok(done["stats"]["llm_seconds"] >= 0, "done 携带 LLM 耗时")
    finally:
        agent.llm.chat_stream, agent.tools_mod.call = real_chat, real_call


def test_turn_sim_stats():
    import agent
    import tools as tools_mod

    calls = {"n": 0}

    def fake_chat_stream(cfg, messages, tools=None, timeout=240,
                         on_reasoning=None, on_content=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"tool_calls": [{"id": "1", "function": {
                "name": "run_simulation",
                "arguments": "{\"library\": \"L\", \"cell\": \"C\"}"}}]}, None)
        return {"content": "结论"}, None

    real_chat, real_call = agent.llm.chat_stream, agent.tools_mod.call
    agent.llm.chat_stream = fake_chat_stream
    agent.tools_mod.call = lambda cfg, name, args: (_ for _ in ()).throw(
        tools_mod.AdsToolError("仿真前检查未通过"))
    try:
        turn = agent.Turn({"max_tool_steps": 3}, [{"role": "user", "content": "sim"}])
        events = []
        turn.run(events.append)
        st = turn.stats
        eq(st["calls"], 1, "仿真调用计入")
        eq(st["failed"], 1, "仿真失败计入")
        eq(st["sim_failed"], 1, "仿真失败单列")
        # 工具失败在 _run_tool_call 内收敛为 tool_result(ok=false)，轮次正常结束
        tr = [e for e in events if e["type"] == "tool_result"]
        ok(tr and tr[0]["ok"] is False, "仿真失败如实出现在 tool_result")
        ok(any(e["type"] == "done" for e in events), "轮次正常收敛")
    finally:
        agent.llm.chat_stream, agent.tools_mod.call = real_chat, real_call


def test_dialog_reuses_only_current_successful_simulation():
    import agent

    turn = agent.Turn({"max_tool_steps": 3}, [])
    request = {"design": {"library": "L", "cell": "C"}, "metrics": []}
    eq(turn._reuse_recent_simulation(request), request, "尚无仿真不复用")

    turn._last_sim = {"library": "L", "cell": "C", "view": "schematic",
                      "dataset_path": "new.ds"}
    reused = turn._reuse_recent_simulation(request)
    eq(reused["dataset_path"], "new.ds", "同一设计复用新数据集")
    eq(reused["reuse_dataset"], True)
    eq(turn._reuse_recent_simulation({**request, "simulate": True}),
       {**request, "simulate": True}, "显式要求重新仿真要保留")
    eq(turn._reuse_recent_simulation({"design": {"library": "L", "cell": "Other"}}),
       {"design": {"library": "L", "cell": "Other"}}, "不能跨设计复用")


def test_publish_empty_error_is_not_failure():
    import agent

    turn = agent.Turn({"max_tool_steps": 3}, [])
    turn._run_local_tool = lambda name, args, emit: {"verdict": "pass", "error": ""}
    events = []
    turn._run_tool_call([], {"id": "1", "function": {
        "name": "publish_design_result", "arguments": "{}"}}, events.append)
    result = [event for event in events if event["type"] == "tool_result"][0]
    eq(result["ok"], True, "空错误字段不能把成功的发布标成失败")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
