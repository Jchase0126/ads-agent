# -*- coding: utf-8 -*-
"""把 schematic_rules.py 里 35 条规则的 ``code`` 定位串刷新为**当前**行号。

为什么需要它
============
规则表的 ``code`` 字段是人类可读的定位串（形如
``ads_ops.py:_route_clear:1184 / _wire_connection:1369``）。源码一改，
行号就漂。手抄行号既慢又必然出错，所以这里用**锚点子串**在源码里现场
查行号：锚点是一段确定的代码片段（函数名或特征语句），行号由搜索得到，
写回前还会把结果打印出来供核对。

用法::

    python tools/refresh_rule_locations.py            # 只打印
    python tools/refresh_rule_locations.py --write    # 写回 schematic_rules.py
"""

from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
AGENT = os.path.join(ROOT, "addon", "ads_agent")
OPS = os.path.join(AGENT, "ads_ops.py")
POLICY = os.path.join(AGENT, "geometry_policy.py")
RULES = os.path.join(AGENT, "schematic_rules.py")


def load(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read().split("\n")


OPS_LINES = load(OPS)
POLICY_LINES = load(POLICY)
RULES_LINES = load(RULES)
SOURCES = {"ads_ops.py": OPS_LINES, "geometry_policy.py": POLICY_LINES,
           "schematic_rules.py": RULES_LINES}


def line_of(filename, anchor, nth=0):
    """anchor 首次（或第 nth 次）出现的 1-based 行号。"""
    lines = SOURCES[filename]
    seen = -1
    for i, text in enumerate(lines, 1):
        if anchor in text:
            seen += 1
            if seen == nth:
                return i
    raise LookupError(f"{filename} 里找不到锚点 {anchor!r}（第 {nth} 次出现）")


def span(filename, start_anchor, end_anchor, nth_start=0):
    a = line_of(filename, start_anchor, nth_start)
    b = line_of(filename, end_anchor, nth_start)
    return a, max(a, b)


def ref(filename, start_anchor, end_anchor=None, nth_start=0):
    if end_anchor is None:
        return f"{filename}:{line_of(filename, start_anchor, nth_start)}"
    a, b = span(filename, start_anchor, end_anchor, nth_start)
    return f"{filename}:{a}-{b}"


def refs(*parts):
    return " / ".join(p if p.startswith(("ads_ops", "geometry", "schematic"))
                      else p for p in parts)


# ---------------------------------------------------------------------------
# 每条规则：锚点 -> 定位串。锚点改动时这里会抛 LookupError，比静默漂行号好。
# ---------------------------------------------------------------------------
SPEC = {
    "R-01": lambda: " / ".join([
        ref("ads_ops.py", "abs(a[0] - b[0]) > 1e-8 and abs(a[1] - b[1]) > 1e-8",
            nth_start=0),                                   # _route_clear 斜段
        ref("ads_ops.py", "走线包含斜段", nth_start=0),       # _wire_connection
        ref("ads_ops.py", "走线包含斜段", nth_start=1),       # connect_impl
        ref("ads_ops.py", "wire 折点含斜段"),                 # wire_impl
        ref("ads_ops.py", "段导线是斜线"),                    # _geometry_report ①
    ]),
    "R-02": lambda: " / ".join([
        ref("geometry_policy.py", "def segment_hits_box"),
        ref("ads_ops.py", "def _through_sym_pin"),
        ref("ads_ops.py", "的符号内部", nth_start=0),          # _geometry_report ④
    ]),
    "R-03": lambda: ref("ads_ops.py", "for (px, py, pn) in pin_pts:"),
    "R-04": lambda: ref("geometry_policy.py", "EDGE_RIDE_MAX:", nth_start=0),
    "R-05": lambda: ref("ads_ops.py", 'elif rid == "R-05":'),
    "R-06": lambda: ref("ads_ops.py", 'elif rid == "R-06":'),
    "R-07": lambda: ref("ads_ops.py", 'if rid == "R-07":'),
    "R-08": lambda: ref("ads_ops.py", 'elif rid == "R-08":'),
    "R-09": lambda: ref("ads_ops.py", 'elif rid == "R-09":'),
    "R-10": lambda: ref("ads_ops.py", 'elif rid == "R-10":'),
    "R-11": lambda: ref("ads_ops.py", 'elif rid == "R-11":'),
    "R-12": lambda: ref("ads_ops.py", 'elif rid == "R-12":'),
    "R-13": lambda: ref("ads_ops.py", "重复重合线段："),
    "R-14": lambda: ref("ads_ops.py", "平行同向线段"),
    "R-15": lambda: ref("ads_ops.py", "同轴折返（方向反转）"),
    "R-16": lambda: ref("ads_ops.py", '"R-16",'),
    "R-17": lambda: ref("ads_ops.py", "读图无法判定归属"),
    "R-18": lambda: ref("ads_ops.py", "的标注文字互压"),
    "R-19": lambda: ref("ads_ops.py", "def _layout_scale_issue"),
    "R-20": lambda: ref("ads_ops.py", "只支持全新铺图"),
    "R-21": lambda: " / ".join([
        ref("ads_ops.py", "def _backup_design"),
        ref("ads_ops.py", "会清空原设计，但写入前备份失败"),
    ]),
    "R-22": lambda: " / ".join([
        ref("ads_ops.py", "def _verify_params"),
        ref("ads_ops.py", "def _verify_var_values"),
    ]),
    "R-23": lambda: " / ".join([
        ref("ads_ops.py", "def _netlist_gate"),
        ref("ads_ops.py", "保存后审计失败"),
    ]),
    "R-24": lambda: " / ".join([
        ref("ads_ops.py", "def _check_connection_list"),
        ref("ads_ops.py", "连线复核失败"),
    ]),
    "R-25": lambda: " / ".join([
        ref("ads_ops.py", "def _gate_problems"),
        ref("ads_ops.py", "没有仿真控制器"),
    ]),
    "R-26": lambda: ref("ads_ops.py", "基板引用缺失"),
    "R-27": lambda: " / ".join([
        ref("ads_ops.py", "shorted_ports = "),
        ref("ads_ops.py", "信号脚与地脚接在同一网络"),
    ]),
    "R-28": lambda: " / ".join([
        ref("ads_ops.py", "port_ground_issues = "),
        ref("ads_ops.py", "端口地脚没有接地参考"),
    ]),
    "R-29": lambda: ref("ads_ops.py", "def _gate_warnings"),
    "R-30": lambda: " / ".join([
        ref("ads_ops.py", "cand.sort("),
        ref("ads_ops.py", "main = cand[0][2]"),
    ]),
    "R-31": lambda: " / ".join([
        ref("ads_ops.py", "def _normalize_port_angle"),
        ref("ads_ops.py", "def _gnd_normalized_angles"),
    ]),
    "R-32": lambda: ref("ads_ops.py", "y_low = min(ys)"),
    "R-33": lambda: ref("ads_ops.py", "def annot_y"),
    "R-34": lambda: " / ".join([
        ref("ads_ops.py", "max(_ROW_GAP_MIN,"),
        ref("ads_ops.py", "def _row_depth"),
    ]),
    "R-35": lambda: " / ".join([
        ref("ads_ops.py", "def _match_sym_split"),
        ref("ads_ops.py", "def _sym_rows"),
    ]),
}

# 规则号 -> code 字段的补充说明（行号之外的人话）
NOTES = {
    "R-02": "（strict+pins 时端点命中引脚即整段豁免，复核期由 _through_sym_pin 用体内深度 ≤0.5 兜住）",
    "R-04": "（非 strict 分支，贴边滑行 > EDGE_RIDE_MAX=0.25 格判违规）",
    "R-11": "（交叉与共线重叠共用本规则，措辞按关系分）",
    "R-12": "（kind==t 且 at_pin 为假：端点落在导线中部、该点无引脚）",
    "R-19": "（>200 × 符号中位长边；无有效 bbox 时放行）",
    "R-21": "（recreate 且备份失败则拒写；note 含「没有可备份」视为可放行）",
    "R-30": "（枚举全部简单路径后按 (长度, 端口名对, 路径) 排序取首条）",
    "R-32": "（共享地需 y 差 ≤0.45 且 x 跨度 ≤6.0 才合并）",
    "R-33": "（annot_y(n) = 1.6 + 0.62*(n-1)）",
    "R-34": "（max(2.6, 上一行深度+0.25)；docstring 仍写 5，已过期）",
    "R-35": "（k=5 直接 raise）",
}


def find_code_arg(src, rid):
    """返回 _R("R-xx", ...) 第 7 个位置参数（code）的 [start, end) 区间。

    ``code`` 在文件里是**隐式拼接的多段字符串字面量**（跨行），所以这里
    按"逗号 + 括号深度"定位整个参数区间，而不是只找第一个字面量。
    """
    key = f'_R("{rid}"'
    at = src.index(key)
    i = src.index("(", at)          # _R( 的左括号
    depth = 0
    commas = 0
    arg_start = None
    in_str = None
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            commas += 1
            if commas == 6:
                arg_start = i + 1
            elif commas == 7:
                return arg_start, i
        i += 1
    raise LookupError(f"{rid} 的 _R(...) 解析失败")


def _wrap_literal(text, indent):
    """把定位串写成缩进对齐的多段隐式拼接字面量。

    每行以 ``" / "`` 结尾、下一行接着写 —— 这样隐式拼接后的字符串与单行
    写法完全一致，不会因为折行丢掉分隔符。
    """
    parts = text.split(" / ")
    lines = []
    cur = ""
    for part in parts:
        piece = " / " + part if cur else part
        if cur and len(cur) + len(piece) > 70:
            lines.append(cur)
            cur = part
        else:
            cur += piece
    if cur:
        lines.append(cur)
    pad = " " * indent
    if len(lines) == 1:
        return '"' + lines[0] + '"'
    out = []
    for idx, ln in enumerate(lines):
        tail = " / " if idx < len(lines) - 1 else ""
        out.append(pad + '"' + ln + tail + '"')
    return chr(10).join(out)


def main():
    write = "--write" in sys.argv
    with io.open(RULES, encoding="utf-8") as f:
        src = f.read()
    out = []
    for rid in sorted(SPEC):
        code = SPEC[rid]() + NOTES.get(rid, "")
        out.append((rid, code))
    if not write:
        for rid, code in out:
            print(rid, "::", code)
        return 0
    for rid, code in out:
        a, b = find_code_arg(src, rid)
        raw = src[a:b]
        body = raw.split("\n")
        indent = len(body[-1]) - len(body[-1].lstrip(" ")) if len(body) > 1 else 8
        src = src[:a] + "\n" + _wrap_literal(code, indent) + src[b:]
    with io.open(RULES, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    print(f"已刷新 {len(out)} 条规则的 code 字段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
