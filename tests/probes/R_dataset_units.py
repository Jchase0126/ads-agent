"""R：查清 .ds 数据集到底有没有暴露单位（需要 ADS 环境）。

为什么要查：结果页要"标明横纵轴、单位和数据来源"。数据来源（.ds 路径）和
纵轴单位（由表达式语法推出，如 dB(...) -> dB）都没问题；**横轴单位**目前
拿不到 —— 数据集里 freq 是 Hz 还是 GHz、单位信息存在哪，本机没验证过。

`ads_ops._series_unit` 用的是通用属性探测（pandas Series.attrs / dtype.unit），
不猜 Keysight 的私有 API。这个脚本把能看到的元信息全部打印出来，据此决定：

  * 有明确来源 -> 用真实单位，去掉"单位未声明"的提示；
  * 没有       -> 保持现状：横轴单位由"声明的频段落在数据范围内"反推，
                  并在结果页与指标 note 里**如实标注**是推断出来的。

用法（在仓库根目录）::

    python tests/probe_via_ads.py tests/probes/R_dataset_units.py

把 DATASET 改成你最近一次仿真产出的 .ds 路径（run_simulation 会返回）。
"""

DATASET = r""          # 例：r"C:\ws\ads_agent_sim\Amp24G_20260924_170000\Amp24G.ds"
EXPR = ""              # 留空则只列变量名；填一个表达式（如 dB(S(2,1))）看它的元信息

import os  # noqa: E402

import keysight.ads.dataset as dataset  # noqa: E402

if not DATASET or not os.path.exists(DATASET):
    raise SystemExit(f"请先把 DATASET 改成真实存在的 .ds 路径（当前: {DATASET!r}）")

data = dataset.open(DATASET)
names = [str(k) for k in (data.keys() if hasattr(data, "keys") else data)]
print("=" * 70)
print("数据集对象类型:", type(data).__name__)
print("变量名:", names)
print("data 上的公开成员:", [n for n in dir(data) if not n.startswith("_")][:40])

if not EXPR:
    print()
    print("把 EXPR 设成上面某个变量名再跑一次，就能看到它的元信息。")
    raise SystemExit(0)

block = data[EXPR]
print("=" * 70)
print(f"变量 {EXPR!r} 的块类型:", type(block).__name__)
print("块上的公开成员:", [n for n in dir(block) if not n.startswith("_")][:60])
for attr in ("units", "unit", "x_units", "y_units", "axis", "axes",
             "independent_vars", "sweep_vars", "name", "expr"):
    if hasattr(block, attr):
        try:
            print(f"   block.{attr} =", repr(getattr(block, attr))[:200])
        except Exception as e:  # noqa: BLE001
            print(f"   block.{attr} 读取失败:", type(e).__name__, e)

df = block.to_dataframe().reset_index()
print("-" * 70)
print("DataFrame 列:", [str(c) for c in df.columns])
print("df.attrs =", repr(getattr(df, "attrs", None))[:300])
for col in df.columns:
    series = df[col]
    print(f"   列 {str(col)!r}:")
    print("      dtype =", series.dtype)
    print("      attrs =", repr(getattr(series, "attrs", None))[:200])
    for attr in ("unit", "units"):
        if hasattr(series, attr):
            print(f"      .{attr} =", repr(getattr(series, attr))[:120])
        if hasattr(series.dtype, attr):
            print(f"      dtype.{attr} =", repr(getattr(series.dtype, attr))[:120])
    print("      前 3 个值 =", [repr(v) for v in series.head(3).tolist()])

print()
print("=" * 70)
print("结论怎么用：")
print("  * 若某处明确给出了横轴单位（如 'Hz'/'GHz'）-> 把它接进 ads_ops._series_unit；")
print("  * 若没有任何单位信息 -> 保持现状（按频段反推 + 结果页如实标注'推断'）。")
