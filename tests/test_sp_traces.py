"""S 参数矩阵表达式解析，不依赖 ADS 进程。"""

import math
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "addon", "ads_agent"))

from _harness import eq, ok, run  # noqa: E402
import ads_ops  # noqa: E402


def test_ads_matrix_expression_and_db_value():
    block, col, transform = ads_ops._trace_expression(
        "dB(S(2,1))", ["SP1.SP"])
    eq((block, col, transform), ("SP1.SP", "S[2,1]", "db"))
    eq(round(ads_ops._trace_number(complex(0.5, 0), transform), 6),
       round(20 * math.log10(0.5), 6))


def test_explicit_block_and_other_component():
    eq(ads_ops._trace_expression("SP1.SP.dB(S(3,1))", ["SP1.SP"]),
       ("SP1.SP", "S[3,1]", "db"))
    eq(ads_ops._trace_expression("S(2,3)", ["SP1.SP"]),
       ("SP1.SP", "S[2,3]", "mag"))
    eq(ads_ops._trace_number(3 + 4j, "mag"), 5.0)


def test_multiple_sp_blocks_are_not_guessed():
    try:
        ads_ops._trace_expression("dB(S(1,1))", ["SP1.SP", "SP2.SP"])
    except KeyError:
        return
    ok(False, "多个 S 参数块必须显式指定")


def test_run_python_system_exit_becomes_error():
    original = ads_ops._python_env
    ads_ops._python_env = lambda: {}
    try:
        result = ads_ops.run_python({"code": "raise SystemExit('stop')"})
        eq(result["ok"], False)
        ok("SystemExit: stop" in result["stdout"])
        eq(ads_ops.run_python({"code": "print(42)"})["ok"], True)
    finally:
        ads_ops._python_env = original


def test_read_traces_zero_limit_keeps_all_points():
    class Series:
        attrs = {"unit": "GHz"}
        dtype = None

        def __init__(self, values):
            self.values = values

        def tolist(self):
            return self.values

    class Frame:
        columns = ["freq", "S[2,1]"]

        def reset_index(self):
            return self

        def __getitem__(self, key):
            return Series([2.0, 2.1, 2.2, 2.3] if key == "freq"
                          else [0.1, 0.2, 0.3, 0.4])

    class Block:
        def to_dataframe(self):
            return Frame()

    class Dataset:
        def keys(self):
            return ["SP1.SP"]

        def __getitem__(self, key):
            return Block()

    names = ("keysight", "keysight.ads", "keysight.ads.dataset")
    previous = {name: sys.modules.get(name) for name in names}
    pkg = types.ModuleType("keysight")
    pkg.__path__ = []
    ads = types.ModuleType("keysight.ads")
    ads.__path__ = []
    dataset = types.ModuleType("keysight.ads.dataset")
    dataset.open = lambda path: Dataset()
    pkg.ads = ads
    ads.dataset = dataset
    sys.modules.update(zip(names, (pkg, ads, dataset)))
    try:
        args = {"path": "dummy.ds", "expressions": ["S(2,1)"]}
        full = ads_ops.read_traces(dict(args, max_points=0))["traces"]["S(2,1)"]
        short = ads_ops.read_traces(dict(args, max_points=2))["traces"]["S(2,1)"]
        eq(len(full["x"]), 4)
        eq(full.get("truncated"), None)
        eq(short["truncated"], True)
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


if __name__ == "__main__":
    raise SystemExit(run(globals()))
