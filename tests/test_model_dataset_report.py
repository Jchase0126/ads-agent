# -*- coding: utf-8 -*-
"""冒烟仿真数据集核对（_dataset_report）的纯逻辑测试。

背景（2026-11-05 实机踩到的两个坑，这里各留一条回归）：
  * 数据集的 **块名** 由仿真控制器决定（S_Param 实测是 `SP1.SP`），块里的
    列才是 `S[1,1]`（新版）或 `S(1,1)`（旧版）。旧代码拿固定的 `S(1,1)`
    当块名去取，仿真明明成功（数据集 47KB、S 曲线有限）却被判成失败。
  * 曲线退化（NaN/Inf、全常数、端口数不够）也必须算 problem，不能因为
    "仿真进程退出码 0" 就当成模型可用的证据。

**这里测不到**：真实 `keysight.ads.dataset` 的读取行为 —— 用例注入 fake 模块，
按实机取到的形状（块名 keys、`to_dataframe()` 列名 `S[1,1]`）构造。

运行: python tests/test_model_dataset_report.py
"""

import math
import os
import sys
import tempfile
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


def check_in(name, haystack, needle):
    _COUNT[0] += 1
    text = str(haystack)
    if needle not in text:
        FAILS.append(f"{name}: {needle!r} not in {text!r}")
        print(f"  FAIL {name}: {needle!r} not in {text!r}")


class FakeSeries:
    def __init__(self, values):
        self._values = list(values)

    def min(self):
        vals = [v for v in self._values]
        return min(vals) if vals else float("nan")

    def max(self):
        vals = [v for v in self._values]
        return max(vals) if vals else float("nan")


class FakeFrame:
    def __init__(self, columns, series):
        self._columns = list(columns)
        self._series = dict(series)

    @property
    def columns(self):
        return self._columns

    def __len__(self):
        return len(self._series.get(self._columns[0], FakeSeries([]))._values)

    def __getitem__(self, col):
        return self._series[col]

    def reset_index(self):
        return self


class FakeBlock:
    def __init__(self, frame):
        self._frame = frame

    def to_dataframe(self):
        return self._frame


class FakeDataset:
    def __init__(self, blocks):
        self._blocks = dict(blocks)

    def keys(self):
        return list(self._blocks.keys())

    def __getitem__(self, key):
        if key not in self._blocks:
            raise KeyError(key)
        return self._blocks[key]


def _s_matrix_frame(ports=2, nan_col=None, flat_value=None):
    freq = [1e9, 1.5e9, 2e9]
    cols = ["freq"]
    series = {"freq": FakeSeries(freq)}
    for i in range(1, ports + 1):
        for j in range(1, ports + 1):
            col = f"S[{i},{j}]"
            cols.append(col)
            if flat_value is not None:
                series[col] = FakeSeries([flat_value, flat_value, flat_value])
            elif col == nan_col:
                series[col] = FakeSeries([float("nan"), float("nan"), 0.1])
            else:
                series[col] = FakeSeries([0.1 * i, 0.2 + 0.1 * j, 0.05])
    return FakeFrame(cols, series)


class _FakeModuleScope:
    """把 fake keysight.ads.dataset 放进 sys.modules，退出时严格还原。"""

    def __init__(self, open_fn):
        self._open = open_fn
        self._saved = {}

    def __enter__(self):
        pkg = types.ModuleType("keysight")
        pkg.__path__ = []
        ads = types.ModuleType("keysight.ads")
        ads.__path__ = []
        ds = types.ModuleType("keysight.ads.dataset")
        ds.open = self._open
        pkg.ads = ads
        ads.dataset = ds
        for name, mod in (("keysight", pkg), ("keysight.ads", ads),
                          ("keysight.ads.dataset", ds)):
            self._saved[name] = sys.modules.get(name)
            sys.modules[name] = mod
        return ds

    def __exit__(self, *exc):
        for name, old in self._saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        return False


def _temp_file():
    handle = tempfile.NamedTemporaryFile(suffix=".ds", delete=False)
    handle.write(b"fake dataset")
    handle.close()
    return handle.name


def test_missing_file():
    report = M._dataset_report(os.path.join(tempfile.gettempdir(),
                                           "no_such_model_smoke.ds"), 2)
    check("missing_file_ok", report["ok"], False)
    check_in("missing_file_problem", report["problems"], "数据集文件不存在")


def test_open_failure():
    path = _temp_file()
    try:
        def _boom(_path):
            raise RuntimeError("no reader for this ADS build")

        with _FakeModuleScope(_boom):
            report = M._dataset_report(path, 2)
    finally:
        os.unlink(path)
    check("open_failure_ok", report["ok"], False)
    check_in("open_failure_problem", report["problems"], "打开数据集失败")


def test_block_name_with_bracket_columns():
    """实机形状：块名 SP1.SP，块内列 S[1,1]..S[2,2] -> 通过。"""
    path = _temp_file()
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(_s_matrix_frame(ports=2))})):
            report = M._dataset_report(path, 2)
    finally:
        os.unlink(path)
    check("good_ok", report["ok"], True)
    check("good_problems", report["problems"], [])
    check("good_variables", report["variables"], ["SP1.SP"])
    entry = report["curves"]["SP1.SP"]
    check("good_found", entry["found"], True)
    check("good_s_columns", sorted(entry["s_columns"]),
          ["S[1,1]", "S[1,2]", "S[2,1]", "S[2,2]"])
    check("good_rows", entry["n_rows"], 3)


def test_legacy_parenthesis_columns():
    """旧版列名 S(1,1) 同样要认。"""
    path = _temp_file()
    frame = FakeFrame(["freq", "S(1,1)", "S(2,1)", "S(1,2)", "S(2,2)"],
                      {"freq": FakeSeries([1e9, 2e9]),
                       "S(1,1)": FakeSeries([0.1, 0.2]),
                       "S(2,1)": FakeSeries([0.3, 0.4]),
                       "S(1,2)": FakeSeries([0.5, 0.6]),
                       "S(2,2)": FakeSeries([0.7, 0.8])})
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(frame)})):
            report = M._dataset_report(path, 2)
    finally:
        os.unlink(path)
    check("legacy_ok", report["ok"], True)
    check("legacy_columns", len(report["curves"]["SP1.SP"]["s_columns"]), 4)


def test_no_s_columns():
    path = _temp_file()
    frame = FakeFrame(["freq", "PortZ[1,1]"],
                      {"freq": FakeSeries([1e9, 2e9]),
                       "PortZ[1,1]": FakeSeries([50.0, 51.0])})
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(frame)})):
            report = M._dataset_report(path, 2)
    finally:
        os.unlink(path)
    check("no_s_ok", report["ok"], False)
    check_in("no_s_problem", report["problems"], "只找到 0 条 S 曲线")


def test_nan_detected():
    path = _temp_file()
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(_s_matrix_frame(ports=2,
                                                     nan_col="S[1,1]"))})):
            report = M._dataset_report(path, 2)
    finally:
        os.unlink(path)
    check("nan_ok", report["ok"], False)
    check_in("nan_problem", report["problems"], "NaN/Inf")


def test_flat_curve_detected():
    path = _temp_file()
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(_s_matrix_frame(ports=2,
                                                     flat_value=0.5))})):
            report = M._dataset_report(path, 2)
    finally:
        os.unlink(path)
    check("flat_ok", report["ok"], False)
    check_in("flat_problem", report["problems"], "全是常数")


def test_port_count_shortfall():
    path = _temp_file()
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(_s_matrix_frame(ports=2))})):
            report = M._dataset_report(path, 3)
    finally:
        os.unlink(path)
    check("ports_ok", report["ok"], False)
    check_in("ports_problem", report["problems"], "3 端口应有的 9 条")


def test_explicit_expression():
    path = _temp_file()
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(_s_matrix_frame(ports=2))})):
            report = M._dataset_report(path, 2, expressions=["SP1.SP"])
    finally:
        os.unlink(path)
    check("expr_ok", report["ok"], True)
    check("expr_curves", list(report["curves"].keys()), ["SP1.SP"])


def test_explicit_expression_wrong_name():
    """把块名写成 S(1,1) 是旧代码的错法：现在必须如实报"取不到"。"""
    path = _temp_file()
    try:
        with _FakeModuleScope(lambda _p: FakeDataset(
                {"SP1.SP": FakeBlock(_s_matrix_frame(ports=2))})):
            report = M._dataset_report(path, 2, expressions=["S(1,1)"])
    finally:
        os.unlink(path)
    check("wrong_expr_ok", report["ok"], False)
    check_in("wrong_expr_problem", report["problems"], "取不到 S(1,1)")
    check_in("wrong_expr_variables", report["variables"], "SP1.SP")


def main():
    test_missing_file()
    test_open_failure()
    test_block_name_with_bracket_columns()
    test_legacy_parenthesis_columns()
    test_no_s_columns()
    test_nan_detected()
    test_flat_curve_detected()
    test_port_count_shortfall()
    test_explicit_expression()
    test_explicit_expression_wrong_name()
    print(f"checks: {_COUNT[0]}, fails: {len(FAILS)}")
    if FAILS:
        for line in FAILS:
            print("FAIL", line)
        return 1
    print("PASS test_model_dataset_report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
