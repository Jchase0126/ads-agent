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


def test_read_dataset_materializes_each_block_once():
    """同一表达式重复出现时，只能物化一次 DataFrame。

    2026-10-30 主线程优化：read_traces 早已按块缓存 to_dataframe()，
    read_dataset 没有 —— 模型把同一变量列两次就会整块重新物化一遍，
    数据块一大大就直接顶在主线程上。本用例用调用计数锁住"只转一次"。
    """
    calls = {"n": 0}

    class Series:
        attrs = {"unit": "GHz"}

        def __init__(self, values):
            self.values = values

        def __len__(self):
            return len(self.values)

        def min(self):
            return min(self.values)

        def max(self):
            return max(self.values)

        @property
        def iloc(self):
            values = self.values

            class _I:
                def __getitem__(self, i):
                    return values[i]
            return _I()

    class Frame:
        columns = ["freq", "S[2,1]"]
        _values = {"freq": [1.0, 2.0, 3.0], "S[2,1]": [1.0, 2.0, 3.0]}

        def reset_index(self):
            return self

        def __len__(self):
            return 3

        def __getitem__(self, key):
            return Series([1.0, 2.0, 3.0])

        def head(self, n):
            return self

        @property
        def iloc(self):
            frame = self

            class _Row:
                def __getitem__(self, col):
                    return frame._values[col][frame._row]

            class _ILoc:
                def __getitem__(self, i):
                    row = _Row()
                    row._row = i
                    return row
            return _ILoc()

        def to_csv(self, path, index=False):
            with open(path, "w", encoding="utf-8") as f:
                f.write("freq,S[2,1]\n")

    class Block:
        def to_dataframe(self):
            calls["n"] += 1
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
    import tempfile
    tmp = tempfile.mkdtemp(prefix="read_dataset_")
    sys.modules.update(zip(names, (pkg, ads, dataset)))
    try:
        out = ads_ops.read_dataset({
            "path": os.path.join(tmp, "dummy.ds"),
            "expressions": ["S(2,1)", "S(2,1)"],
            "max_rows": 1,
        })
        eq(calls["n"], 1,
           "同一块被重复物化：read_dataset 未命中按块缓存（主线程会被成倍占用）")
        eq(sorted(out["expressions"]), ["S(2,1)"])
        eq(out["expressions"]["S(2,1)"]["n_rows"], 3)
        eq(out["expressions"]["S(2,1)"]["stats"]["freq"]["min"], 1.0)
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_read_traces_materializes_each_block_once():
    """read_traces 的按块缓存同样要锁住（2026-10-30 已修，防回退）。"""
    calls = {"n": 0}

    class Series:
        attrs = {"unit": "GHz"}
        dtype = None

        def __init__(self, values):
            self.values = values

        def tolist(self):
            return self.values

    class Frame:
        columns = ["freq", "S[2,1]", "S[1,1]"]

        def reset_index(self):
            return self

        def __getitem__(self, key):
            return Series([1.0, 2.0])

    class Block:
        def to_dataframe(self):
            calls["n"] += 1
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
        out = ads_ops.read_traces({"path": "dummy.ds",
                                   "expressions": ["S(2,1)", "S(1,1)"],
                                   "max_points": 0})
        eq(calls["n"], 1, "同一数据块的 S 参数应共用一次 to_dataframe()")
        eq(sorted(out["traces"]), ["S(1,1)", "S(2,1)"])
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


if __name__ == "__main__":
    raise SystemExit(run(globals()))
