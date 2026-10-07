# -*- coding: utf-8 -*-
"""网表级电气等价校验（离线/在线共用，2026-09-29 用户规则）。

比对基准不是"网络数量"或"每个元件连了几个节点"，而是**每个网络
包含哪些元件引脚**——即引脚按网络划分的集合划分（partition）完全
一致。布局/重排会改变网络编号（N__1 ↔ N__9），划分不受影响。

三个入口：
* parse_netlist(text)        ADS generate_netlist() 文本 -> {元件: (节点..)}
* partition_from_netlist(..) 网表解析结果 -> 引脚划分 {frozenset(引脚)..}
* partition_from_plan(...)   连接清单 + 地/端口隐含语义 -> 期望划分
* compare_partitions(...)    两个划分逐块比对，返回差异清单（人类可读）

引脚元组统一为 (实例名, 引脚号字符串)。地网络（0 / gnd!）与接地
实例的全部引脚并入同一 GND 块。TermG/Term 端口的隐含地参考不产生
引脚成员。
"""

from __future__ import annotations

import re

# 网表行元件引脚顺序（与 generate_netlist 输出一致；缺省 1,2,..n）
# BFR106 实测: c b e（引脚 1/2/3）
_PIN_ORDER = {
    "BFR106": ("1", "2", "3"),
}
_GND_NETS = {"0", "gnd!", "GND"}


def _master_of_line(inst_field):
    # "BFR106:Q1" / "R:R1" / "Port:PORT1" -> (master_short, inst_name)
    if ":" in inst_field:
        m, name = inst_field.split(":", 1)
        return m.strip(), name.strip()
    return "", inst_field.strip()


def parse_netlist(text):
    """netlist 文本 -> {元件名: (节点1, 节点2, ...)}。

    只解析元件行（Key:Name node node ...），忽略选项/控制器/S_Param
    等控制行（含 SweepPlan/OutputPlan/#include/#load 等）。
    """
    out = {}
    # 控制器/注释行 master 黑名单（R16：Eqn/MeasEqn/Optim 等也不进
    # 元件表 —— 实测 Eqn:EQN1 gain="..." 会被误解析成元件）
    _CTRL = {"s_param", "sweepplan", "outputplan", "component",
             "equation", "meas_eqn", "optim", "goal", "nodeset",
             "parameter_sweep", "stim"}
    for raw in (text or "").splitlines():
        line = raw.rstrip("\\").strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("Options") or ":" not in line.split()[0]:
            continue
        head = line.split()[0]
        if ":" not in head:
            continue
        master, name = _master_of_line(head)
        if master.lower() in _CTRL:
            continue
        # 节点 = 行首字段后的裸 token（参数带 = 的不算）
        nodes = []
        for tok in line.split()[1:]:
            if "=" in tok or tok.startswith("\\"):
                break
            nodes.append(tok)
        if nodes:
            out[name] = tuple(nodes)
    return out


def _norm_net(n):
    return "GND" if str(n).lower() in _GND_NETS else str(n)


def partition_from_netlist(parsed, gnd_instances=()):
    """网表解析结果 -> {网络键: frozenset((实例, 引脚))}。

    gnd_instances: 接地实例名集合（GROUND 符号在网表里不出现，其
    引脚并入 GND 块——调用方从 instances 清单给）。
    """
    part = {}
    for name, nodes in parsed.items():
        m = name.split(":")[0] if ":" in name else ""
        for i, net in enumerate(nodes):
            pin = str(i + 1)
            part.setdefault(_norm_net(net), set()).add((name, pin))
    for g in gnd_instances:
        part.setdefault("GND", set()).add((str(g), "1"))
    return {k: frozenset(v) for k, v in part.items()}


def partition_from_plan(instances, connections):
    """连接清单 -> 期望引脚划分。

    * 每条 connection 把两个引脚并入同一网络；
    * 全部接地实例（GROUND/GND）的引脚先并成一块（GND）——与网表
      侧的 0/gnd! 语义一致：接地元件引脚经 connection 落进 GND 块；
    * TermG/Term 端口只贡献其显式引脚（隐含地参考不产生成员）；
    * 完全悬空的引脚各自成块，保证两侧引脚全集一致。
    """
    parent = {}

    def find(k):
        while parent.get(k, k) != k:
            parent[k] = parent.get(parent[k], parent[k])
            k = parent[k]
        return k

    def union(a, b):
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    gnd_pins = []
    for s in instances or []:
        short = str(s.get("master", "")).replace("/", ":").split(":")[-1].upper()
        if short in ("GROUND", "GND"):
            for l in _pin_order_of(short, s):
                gnd_pins.append((str(s.get("name")), l))
    gnd_set = set(gnd_pins)
    # GND 块锚点：所有地引脚并成一块
    if len(gnd_pins) > 1:
        for k in gnd_pins[1:]:
            union(gnd_pins[0], k)

    for conn in connections or []:
        a, b = conn.get("a"), conn.get("b")
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            continue
        ka = (str(a[0]), str(a[1]))
        kb = (str(b[0]), str(b[1]))
        # 涉地的连接：非地引脚也并进 GND 块（网表侧 gnd! 语义）
        if ka in gnd_set or kb in gnd_set:
            anchor = gnd_pins[0] if gnd_pins else ka
            union(anchor, ka)
            union(anchor, kb)
            continue
        union(ka, kb)

    blocks = {}
    for k in list(parent):
        blocks.setdefault(find(k), set()).add(k)
    # 悬空引脚
    for s in instances or []:
        short = str(s.get("master", "")).replace("/", ":").split(":")[-1].upper()
        for l in _pin_order_of(short, s):
            k = (str(s.get("name")), l)
            if not any(k in blk for blk in blocks.values()):
                blocks[("float",) + k] = {k}
    return {repr(sorted(blk)): frozenset(blk) for blk in blocks.values()}


def _pin_order_of(short, spec):
    """实例的全部引脚号。多引脚器件查 ads_ops._AUTO_PIN_OFFS（BFR106
    三脚、MTEE 三脚、CLIN 四脚、TermG 单脚），两脚件缺省 1/2。
    实测建档（CE_FM_Amp 实机 dump）：GROUND 单脚；INC1/S_Param 无脚。"""
    n = spec.get("_npins")
    if n is not None:
        return [str(i + 1) for i in range(int(n))]
    if short in ("GROUND", "GND"):
        return ["1"]
    if short in ("S_PARAM", "VAR", "MSUB", "SWEEP_PLAN", "OPTIM", "GOAL",
                 "OUTPUT_PLAN", "PARAMETER_SWEEP", "EQN", "MEAS_EQN",
                 "NOTE", "TEXT", "NODESET", "DA_", "STIM"):
        return []
    if "INCLUDE" in str(spec.get("master", "")).upper():
        return []
    try:
        import ads_ops
        offs = ads_ops._AUTO_PIN_OFFS.get(short)
        if offs:
            return [str(l) for l in offs]
    except Exception:  # noqa: BLE001 — 离线独立使用时无 ads_ops
        pass
    return ["1", "2"]


def compare_partitions(exp, act, pin_universe=None):
    """逐块比对两个划分（值是 frozenset((inst,pin))，键忽略——网络
    允许重编号）。接受 {键: frozenset} 的划分，也直接接受 frozenset
    的可迭代集合。返回差异描述列表；空列表 = 电气等价。"""
    def _blocks(part):
        vals = part.values() if hasattr(part, "values") else part
        blocks = {}
        for v in vals:
            blocks.setdefault(frozenset(v), []).append("x")
        return blocks

    exp_blocks = _blocks(exp)
    act_blocks = _blocks(act)
    diffs = []
    for blk in sorted(set(exp_blocks) - set(act_blocks),
                      key=lambda b: sorted(map(str, b))):
        diffs.append("缺失网络: {%s}" % ", ".join("%s.%s" % p
                                                  for p in sorted(blk)))
    for blk in sorted(set(act_blocks) - set(exp_blocks),
                      key=lambda b: sorted(map(str, b))):
        diffs.append("多出网络: {%s}" % ", ".join("%s.%s" % p
                                                  for p in sorted(blk)))
    if pin_universe:
        exp_pins = set().union(*exp.values()) if exp else set()
        act_pins = set().union(*act.values()) if act else set()
        for p in sorted(pin_universe - exp_pins):
            diffs.append("期望中缺失引脚 %s.%s" % p)
        for p in sorted(pin_universe - act_pins):
            diffs.append("实际中缺失引脚 %s.%s" % p)
    return diffs


def check_netlist_equivalence(netlist_text, instances, connections):
    """一站式校验：网表文本 vs 连接清单 的引脚级电气等价。

    返回 (ok, diffs, parsed)。接地实例的引脚自动并入 GND 块；
    无引脚实例（S_Param/Include/VAR/MSUB 控制器与注释件）两侧都跳过；
    端口（Port:）网表行的第 2 节点是隐含地参考，并入 GND。
    """
    def _npins(s):
        short = str(s.get("master", "")).replace("/", ":").split(":")[-1].upper()
        return len(_pin_order_of(short, s))

    gnd_instances = [str(s.get("name")) for s in instances or []
                     if str(s.get("master", "")).replace("/", ":")
                     .split(":")[-1].upper() in ("GROUND", "GND")]
    no_pin_names = {str(s.get("name")) for s in instances or []
                    if _npins(s) == 0}
    parsed_all = parse_netlist(netlist_text)
    # 网表侧修剪：无引脚实例的行剔除；端口的第 2 节点=隐含地
    parsed = {}
    for name, nodes in parsed_all.items():
        if name in no_pin_names:
            continue
        is_port = False
        for raw in (netlist_text or "").splitlines():
            if raw.split(":")[-1].split()[0:1] == [name] \
                    and raw.startswith("Port:"):
                is_port = True
                break
        if is_port and len(nodes) >= 2:
            nodes = (nodes[0],)  # 第 2 节点是隐含地参考，不产生引脚成员
        parsed[name] = nodes
    act = partition_from_netlist(parsed, gnd_instances)
    exp = partition_from_plan(instances, connections)
    universe = set()
    for s in instances or []:
        for l in _pin_order_of(
                str(s.get("master", "")).replace("/", ":")
                .split(":")[-1].upper(), s):
            universe.add((str(s.get("name")), l))
    diffs = compare_partitions(exp, act, universe)
    return (not diffs), diffs, parsed


def check_components(netlist_text, instances):
    """元件存在性 + 参数一致性（网表行 vs 请求清单）。

    返回差异清单；空 = 全部一致。参数比对忽略空白差异。无引脚的
    控制器/注释件（S_Param/Include/VAR/MSUB）不在比对范围（网表里
    以非元件行或 #include 形态存在）。"""
    parsed = parse_netlist(netlist_text)
    diffs = []
    for s in instances or []:
        name = str(s.get("name"))
        m = str(s.get("master", "")).replace("/", ":").split(":")[-1]
        short = m.upper()
        if short in ("GROUND", "GND", "VAR", "MSUB", "S_PARAM",
                     "SWEEP_PLAN", "OPTIM", "GOAL") \
                or "INCLUDE" in str(s.get("master", "")).upper():
            continue
        if short == "TERM":
            m = "Port"  # 端口在网表里是 Port:NAME
        if name not in parsed:
            diffs.append("网表缺少元件 %s" % name)
            continue
        for k, v in (s.get("params") or {}).items():
            if _find_param(netlist_text, name, k, str(v)):
                continue
            diffs.append("元件 %s 参数 %s=%s 与网表不符或缺失" % (name, k, v))
    return diffs


def _find_param(netlist_text, inst, key, value):
    """在元件行（可能折行）里找 Key=value，值做空白不敏感比较。

    值可能含空格单位（"50 Ohm"、"5 V"）—— 截断到下一个 Key= 或行尾，
    与请求值整体（空白不敏感）比较；另兼容请求值不含单位、网表值
    带单位的情形（"50" 匹配 "50 Ohm" 前缀）。参数匹配不限定在同名
    元件行内 —— ADS 网表的参数唯一性由 Key+值共同保证，误配概率
    可忽略（R16 再收严到同名元件行）。
    """
    pat = re.compile(r"[\s:]%s\s*=\s*([^\\\n]+)" % re.escape(key))
    for m in pat.finditer(netlist_text or ""):
        seg = m.group(1)
        # 截断到下一个 Key=（后续是别的参数）
        stop = re.search(r"\s\S+\s*=", seg)
        val = seg[:stop.start()] if stop else seg
        if _canon(val) == _canon(value):
            return True
        # 单位容错:请求 "50" vs 网表 "50 Ohm"
        if _canon(value) and _canon(val).startswith(_canon(value)):
            return True
    return False


def _canon(v):
    return re.sub(r"\s+", "", str(v)).lower()
