# -*- coding: utf-8 -*-
"""read_dataset 的表达式解析（纯逻辑，不需要 ADS 进程）。

背景（2026-11-05 实机踩到）：S 参数数据集的**顶层变量是数据块名**
（`SP1.SP`），块里的列才是 `S[1,1]`。工具说明里的示例却写成
`AC1.S(1,1)`，直接拿去取会报"数据集中不存在变量"，看起来像数据集坏了。
这里锁住三件事：

  * `块名.列名` 能拆到块上，并把 `S(1,1)` / `S[1,1]` 当同一列；
  * 只给块名时列整块统计量（不是报错）；
  * 块内没有那一列时报错并**提示**正确写法，而不是静默给空。

**这里测不到**：真实 `keysight.ads.dataset` 的读取语义 —— 用例注入 fake
模块，按实机取到的形状（keys=`['SP1.SP']`、`to_dataframe()` 列名
`S[1,1]`）构造。

运行: python tests/test_dataset_expr.py
"""

import os
import shutil
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "addon", "ads_agent")):
    if _p not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

# read_dataset 会把每列写成 csv（放在数据集同目录）：这里用临时目录，
# 免得跑测试时往仓库里掉 SP1.SP.csv 之类的文件。
_TMPDIR = tempfile.mkdtemp(prefix="dataset_expr_")
_FAKE_DS = os.path.join(_TMPDIR, "fake.ds")

import ads_ops  # noqa: E402

FAILS = []
_COUNT = [0]


def check(name, got, want):
    _COUNT[0] += 1
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")
        print(f"  FAIL {name}: got {got!r} want {want!r}")


def check_in(name, haystack, needle):
    _COUNT[0] += 1
    if needle not in str(haystack):
        FAILS.append(f"{name}: {needle!r} not in {haystack!r}")
        print(f"  FAIL {name}: {needle!r} not in {haystack!r}")


# ---------------------------------------------------------------------------
# fake 数据集（形状照实机：块名 SP1.SP，列 freq/S[1,1]/S[2,1]）
# ---------------------------------------------------------------------------


class FakeRow:
    def __init__(self, data):
        self._d = data

    def __getitem__(self, key):
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)


class FakeSeries:
    def __init__(self, values):
        self._v = list(values)

    def min(self):
        return min(self._v)

    def max(self):
        return max(self._v)

    def __len__(self):
        return len(self._v)

    def __getattr__(self, name):
        if name == "iloc":
            outer = self

            class _At:
                def __getitem__(self, i):
                    return outer._v[i]
            return _At()
        raise AttributeError(name)


class FakeFrame:
    def __init__(self, columns, rows):
        self.columns = list(columns)
        self._rows = [list(r) for r in rows]

    def __getitem__(self, col):
        i = self.columns.index(col)
        return FakeSeries([r[i] for r in self._rows])

    def reset_index(self):
        return self

    def head(self, n):
        return FakeFrame(self.columns, self._rows[:n])

    def to_csv(self, path, index=False):
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(",".join(self.columns) + "\n")

    class _Iloc:
        def __init__(self, frame):
            self._f = frame

        def __getitem__(self, i):
            return FakeRow(dict(zip(self._f.columns, self._f._rows[i])))

    def __getattr__(self, name):
        if name == "iloc":
            return FakeFrame._Iloc(self)
        raise AttributeError(name)

    def __len__(self):
        return len(self._rows)


class FakeBlock:
    def __init__(self, frame):
        self._frame = frame

    def to_dataframe(self):
        return self._frame

    @property
    def data(self):
        return self._frame


class FakeDataset:
    def __init__(self, blocks):
        self._blocks = blocks

    def keys(self):
        return list(self._blocks)

    def __getitem__(self, key):
        return self._blocks[key]


def install_fake_dataset(dataset):
    """注入 keysight.ads.dataset.open —— 真机不需要，这里只是让函数跑起来。"""
    keysight = types.ModuleType("keysight")
    ads = types.ModuleType("keysight.ads")
    ads_dataset = types.ModuleType("keysight.ads.dataset")
    ads_dataset.open = lambda path: dataset
    ads.dataset = ads_dataset
    keysight.ads = ads
    saved = {n: sys.modules.get(n) for n in ("keysight", "keysight.ads", "keysight.ads.dataset")}
    sys.modules["keysight"] = keysight
    sys.modules["keysight.ads"] = ads
    sys.modules["keysight.ads.dataset"] = ads_dataset
    return saved


def restore(saved):
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def sample_dataset():
    frame = FakeFrame(["freq", "S[1,1]", "S[2,1]"],
                      [[1e8, 0.686, 0.314],
                       [1.5e8, 0.5, 0.5],
                       [3e8, 0.045, 0.955]])
    return FakeDataset({"SP1.SP": FakeBlock(frame)})


# ---------------------------------------------------------------------------
# 表达式解析
# ---------------------------------------------------------------------------


def test_block_name_and_column():
    names = ["SP1.SP"]
    check("块名本身", ads_ops._resolve_dataset_expr("SP1.SP", names), ("SP1.SP", ""))
    check("圆括号列", ads_ops._resolve_dataset_expr("SP1.SP.S(1,1)", names),
          ("SP1.SP", "S(1,1)"))
    check("方括号列", ads_ops._resolve_dataset_expr("SP1.SP.S[2,1]", names),
          ("SP1.SP", "S[2,1]"))
    check("未知块", ads_ops._resolve_dataset_expr("XX.YY.S(1,1)", names), ("", ""))
    check("空串", ads_ops._resolve_dataset_expr("", names), ("", ""))


def test_column_key_equivalence():
    eq = ads_ops._column_key
    check("S(1,1) 同 S[1,1]", eq("S(1, 1)") == eq("S[1,1]"), True)
    check("大小写无关", eq("s[2,1]") == eq("S[2,1]"), True)
    check("不同列不相等", eq("S[1,1]") == eq("S[2,1]"), False)
    check("_same_column 命中", ads_ops._same_column("S[1,1]", "S(1,1)"), True)
    check("_same_column 空列", ads_ops._same_column("S[1,1]", ""), False)


# ---------------------------------------------------------------------------
# read_dataset
# ---------------------------------------------------------------------------


def test_read_column_by_expression():
    saved = install_fake_dataset(sample_dataset())
    try:
        out = ads_ops.read_dataset({"path": _FAKE_DS,
                                    "expressions": ["SP1.SP.S(1,1)"],
                                    "max_rows": 2})
    finally:
        restore(saved)
    entry = out["expressions"]["SP1.SP.S(1,1)"]
    check("块名回填", entry.get("block"), "SP1.SP")
    check("命中真实列", entry.get("columns"), ["S[1,1]"])
    check("行数", entry.get("n_rows"), 3)
    check("最小值", round(entry["stats"]["S[1,1]"]["min"], 6), 0.045)
    check("最大值", round(entry["stats"]["S[1,1]"]["max"], 6), 0.686)
    check("样例行保留 freq", entry["sample_rows"][0].get("freq") is not None, True)
    check("样例值", round(entry["sample_rows"][0]["S[1,1]"], 3), 0.686)


def test_read_block_only_gives_all_columns():
    saved = install_fake_dataset(sample_dataset())
    try:
        out = ads_ops.read_dataset({"path": _FAKE_DS,
                                    "expressions": ["SP1.SP"], "max_rows": 1})
    finally:
        restore(saved)
    entry = out["expressions"]["SP1.SP"]
    check("整块列", entry.get("columns"), ["freq", "S[1,1]", "S[2,1]"])
    check("整块统计含 S21", "S[2,1]" in entry["stats"], True)


def test_bogus_column_reports_and_hints():
    saved = install_fake_dataset(sample_dataset())
    try:
        out = ads_ops.read_dataset({"path": _FAKE_DS,
                                    "expressions": ["SP1.SP.S(3,3)"], "max_rows": 1})
    finally:
        restore(saved)
    entry = out["expressions"]["SP1.SP.S(3,3)"]
    check_in("报错", entry.get("error"), "不存在")
    check_in("提示写法", entry.get("hint"), "SP1.SP.S[1,1]")


def test_bogus_block_reports_available_names():
    saved = install_fake_dataset(sample_dataset())
    try:
        out = ads_ops.read_dataset({"path": _FAKE_DS,
                                    "expressions": ["NOPE"], "max_rows": 1})
    finally:
        restore(saved)
    entry = out["expressions"]["NOPE"]
    check_in("报错", entry.get("error"), "不存在")
    check_in("可用变量", entry.get("error"), "SP1.SP")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"- {fn.__name__}")
        fn()
    if FAILS:
        print(f"\nFAILED {len(FAILS)} of {_COUNT[0]}")
        return 1
    print(f"\nPASS {_COUNT[0]} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
