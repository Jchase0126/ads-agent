"""布局/走线规划的 golden 锁：结果必须与 2026-10-30 基线一致。

为什么要有这个文件：
    2026-10-30 优化 ADS 主线程占用时，曾给 ``auto_layout_positions`` 内部的
    ``_master_short`` / ``kind`` / ``offs`` / ``_multi_pin`` / ``_rough_box``
    加 dict 记忆化（cProfile 显示这些被调几十万次）。A/B 交替实测（chain13/19/
    31）证明那是**负优化**——慢 1.4~1.9 倍，因为单次成本低于缓存查找本身。
    负优化已回滚，但"回滚后用户看到的图有没有变"必须由机器守住，不能靠肉眼。

为什么快照要在子进程里算（重要）：
    实测发现 ``auto_layout_positions`` 对同一输入**不保证跨进程一致**：
    chain07/chain13 的 T01.x 在 PYTHONHASHSEED=0 时是 4.95、seed=3/7 时是 6.35。
    根因是布局里多处遍历 ``set``（``adj_all`` / ``adj_side`` / ``rf_set`` /
    ``node_parent`` 的键）决定摆放顺序，而 str 的哈希随进程随机化。
    这是本次修复**范围之外**的既有缺陷（本次未改布局算法语义），但它会让
    "直接在主进程里比对坐标"变成 flaky 测试 —— 所以本文件用固定
    ``PYTHONHASHSEED=0`` 的子进程产出快照，再与基线比对。基线也是同种子生成的。
    该缺陷已记入 docs/ADS未响应修复实施报告_2026-10-30.md 的"剩余限制"。

基线来源：``tests/layout_golden_2026-10-30.json``（PYTHONHASHSEED=0 下生成，
三个用例、坐标保留 9 位小数）。
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, ok, eq, run  # noqa: E402

add_path("addon", "ads_agent")
add_path("tests")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GOLDEN_PATH = os.path.join(HERE, "layout_golden_2026-10-30.json")
ADDON = os.path.join(ROOT, "addon", "ads_agent")

# 在子进程里把快照算成 JSON 打印出来。故意内联成一个字符串：子进程必须只依赖
# 仓库里的 ads_ops 与本文件里的构造器，不能 import 测试框架（会污染输出）。
_CHILD = r'''
import json, os, sys
ROOT = sys.argv[1]
sys.path.insert(0, os.path.join(ROOT, "addon", "ads_agent"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import ads_ops
import test_layout_golden as T

out = {}
for tag, build in T.CASES:
    insts, conns, var = build()
    pos, wire = T._snapshot(insts, conns, var)
    out[tag] = {"n_inst": len(insts), "n_conn": len(conns),
                "positions": pos, "wire": wire}
print("@@GOLDEN@@" + json.dumps(out, ensure_ascii=False, sort_keys=True))
'''


def chain_spec(n):
    """n 级 MLIN 主干，每 3 级挂一个 MTEE 分支 + R 到地。

    与生成 golden 时用的构造器逐字一致（改了它就必须重新生成基线）。
    """
    insts, conns, stems = [], [], []
    for i in range(n):
        nm = "TL%02d" % (i + 1)
        insts.append({"name": nm, "master": "ads_tlines:MLIN",
                      "params": {"W": 1.0, "L": 2.0}})
        if stems:
            conns.append({"a": [stems[-1], "2"], "b": [nm, "1"]})
        stems.append(nm)
    insts += [{"name": "GND1", "master": "ads_rf:GND"},
              {"name": "PORT1", "master": "ads_simulation:Term"},
              {"name": "PORT2", "master": "ads_simulation:Term"}]
    conns.append({"a": ["PORT1", "1"], "b": [stems[0], "1"]})
    conns.append({"a": [stems[-1], "2"], "b": ["PORT2", "1"]})
    for i in range(1, n - 1, 3):
        tn, rn = "T%02d" % i, "R%02d" % i
        insts.append({"name": tn, "master": "ads_tlines:MTEE"})
        insts.append({"name": rn, "master": "ads_rf:R", "params": {"R": 50.0}})
        conns.append({"a": [stems[i], "2"], "b": [tn, "1"]})
        conns.append({"a": [tn, "2"], "b": [stems[i + 1], "1"]})
        conns.append({"a": [tn, "3"], "b": [rn, "1"]})
        conns.append({"a": [rn, "2"], "b": ["GND1", "0"]})
    return insts, conns, None


def _wilkinson():
    import test_auto_layout
    return test_auto_layout._wilkinson_spec()


CASES = [
    ("wilkinson", _wilkinson),
    ("chain07", lambda: chain_spec(7)),
    ("chain13", lambda: chain_spec(13)),
]


def _snapshot(insts, conns, var):
    import ads_ops
    pos = ads_ops.auto_layout_positions(insts, conns, var)
    wire = ads_ops.plan_wire_routes(insts, conns, pos)
    return (
        {k: [round(v["x"], 9), round(v["y"], 9), v.get("angle")]
         for k, v in sorted(pos.items())},
        {str(k): (None if w[0] is None else
                  [[round(p[0], 9), round(p[1], 9)] for p in w[0]],
                  w[1], sorted(map(str, w[2] or [])))
         for k, w in sorted(wire.items())},
    )


def _load_golden():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def _child_snapshot():
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"          # 见模块 docstring：布局受 set 遍历顺序影响
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "-c", _CHILD, ROOT],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=180)
    ok(proc.returncode == 0,
       f"子进程生成布局快照失败（rc={proc.returncode}）：{proc.stderr[-800:]}")
    for line in (proc.stdout or "").splitlines():
        if line.startswith("@@GOLDEN@@"):
            return json.loads(line[len("@@GOLDEN@@"):])
    raise AssertionError(f"子进程没有输出快照：{proc.stdout[-800:]}")


def _diff(label, got, want):
    """给出人看得懂的第一处差异（整份 JSON 打印出来没人看）。"""
    if isinstance(want, dict):
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        if missing:
            return f"{label}: 缺少键 {missing[:5]}"
        if extra:
            return f"{label}: 多出键 {extra[:5]}"
        for k in sorted(want):
            d = _diff(f"{label}[{k}]", got[k], want[k])
            if d:
                return d
        return ""
    if isinstance(want, list):
        if len(got) != len(want):
            return f"{label}: 长度 {len(got)} != {len(want)}"
        for i, (a, b) in enumerate(zip(got, want)):
            d = _diff(f"{label}[{i}]", a, b)
            if d:
                return d
        return ""
    return "" if got == want else f"{label}: {got!r} != {want!r}"


def test_golden_file_exists_and_matches_case_set():
    ok(os.path.isfile(GOLDEN_PATH), f"golden 基线文件缺失: {GOLDEN_PATH}")
    golden = _load_golden()
    eq(sorted(golden), sorted(tag for tag, _ in CASES), "golden 用例集合应与测试一致")


def test_positions_and_wire_plans_match_golden():
    golden = _load_golden()
    got = _child_snapshot()
    for tag, _build in CASES:
        want = golden[tag]
        cur = got[tag]
        eq(cur["n_inst"], want["n_inst"],
           f"{tag}: 实例数变了，基线需要重新生成")
        eq(cur["n_conn"], want["n_conn"],
           f"{tag}: 连接数变了，基线需要重新生成")
        d = _diff(f"{tag}.positions", cur["positions"], want["positions"])
        ok(not d, f"布局结果与 2026-10-30 基线不一致（会改变用户看到的图）—— {d}")
        d = _diff(f"{tag}.wire", {str(k): v for k, v in cur["wire"].items()},
                  {str(k): v for k, v in want["wire"].items()})
        ok(not d, f"走线计划与 2026-10-30 基线不一致（会改变导线形状）—— {d}")


def test_layout_is_deterministic_within_one_process():
    """同一进程内连算三次必须完全一样（缓存/全局状态泄漏会破坏这点）。"""
    insts, conns, var = chain_spec(13)
    first = _snapshot(insts, conns, var)
    for i in range(2):
        again = _snapshot(insts, conns, var)
        eq(again[0], first[0], f"第 {i + 2} 次布局结果漂移：存在跨调用状态泄漏")
        eq(again[1], first[1], f"第 {i + 2} 次走线计划漂移：存在跨调用状态泄漏")


def test_inputs_are_not_mutated_by_planning():
    """规划只读输入：instances/connections 不得被就地改写。"""
    insts, conns, var = chain_spec(13)
    before_i = json.dumps(insts, sort_keys=True, ensure_ascii=False)
    before_c = json.dumps(conns, sort_keys=True, ensure_ascii=False)
    _snapshot(insts, conns, var)
    eq(json.dumps(insts, sort_keys=True, ensure_ascii=False), before_i,
       "auto_layout_positions 改写了 instances")
    eq(json.dumps(conns, sort_keys=True, ensure_ascii=False), before_c,
       "auto_layout_positions/plan_wire_routes 改写了 connections")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "布局/走线规划 golden 基线"))
