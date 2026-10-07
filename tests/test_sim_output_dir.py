"""仿真输出目录唯一性测试（不需要 ADS —— ads_ops 的 keysight 导入都是延迟的）。

覆盖第三轮评审第 2 项「仿真输出目录冲突」：

``run_simulation`` 过去用 ``<cell>_<YYYYmmdd_HHMMSS>`` + ``makedirs(exist_ok=True)``
建输出目录。秒级时间戳在同一秒内必然撞名，而"改个参数再跑一次"正是最常见的用法 ——
第二次的结果会把第一次**就地覆盖**，之前的 .ds / 网表 / 日志全部丢失。

现在改为 ``ads_ops.unique_sim_dir()``：靠 ``os.makedirs(..., exist_ok=False)``
的原子性定胜负，撞名就顺延 ``_2`` / ``_3`` …。本文件验证：

* 同一秒内连续多次调用 → 目录各不相同，且都不会覆盖已有内容；
* 同一秒内**并发**（多线程 / 多进程）调用 → 依然各不相同；
* cell 名里的非法字符不会造出子目录；
* 穷尽后缀时给出可操作的报错。

运行::

    python tests/test_sim_output_dir.py
"""

import datetime
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

import ads_ops  # noqa: E402

_TMPDIRS: list = []
FIXED = datetime.datetime(2026, 9, 24, 16, 30, 5)   # 固定到同一秒，专门制造撞名


def _root() -> str:
    d = tempfile.mkdtemp(prefix="ads_agent_sim_")
    _TMPDIRS.append(d)
    return d


def _teardown():
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def _wait(cond, timeout=30.0, msg=""):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待超时：{msg}")


# ---------------------------------------------------------------------------
# 连续运行
# ---------------------------------------------------------------------------

def test_same_second_runs_get_distinct_dirs():
    root = _root()
    dirs = [ads_ops.unique_sim_dir(root, "Wilkinson", now=FIXED) for _ in range(5)]
    eq(len(set(dirs)), 5, f"同一秒内出现了重名目录: {dirs}")
    for d in dirs:
        ok(os.path.isdir(d), f"目录没有真的建出来: {d}")
        eq(os.path.dirname(d), os.path.join(root, ads_ops.SIM_ROOT))

    names = [os.path.basename(d) for d in dirs]
    eq(names[0], "Wilkinson_20260924_163005")
    eq(names[1], "Wilkinson_20260924_163005_2")
    eq(names[2], "Wilkinson_20260924_163005_3")


def test_previous_result_is_never_overwritten():
    root = _root()
    first = ads_ops.unique_sim_dir(root, "RFamp", now=FIXED)
    netlist = os.path.join(first, "netlist.ckt")
    with open(netlist, "w", encoding="utf-8") as f:
        f.write("第一次的网表")
    dataset = os.path.join(first, "RFamp.ds")
    with open(dataset, "w", encoding="utf-8") as f:
        f.write("第一次的数据集")

    second = ads_ops.unique_sim_dir(root, "RFamp", now=FIXED)
    ne(second, first, "第二次仿真落到了同一个目录")
    with open(netlist, encoding="utf-8") as f:
        eq(f.read(), "第一次的网表", "第一次的网表被覆盖了")
    with open(dataset, encoding="utf-8") as f:
        eq(f.read(), "第一次的数据集", "第一次的数据集被覆盖了")
    eq(os.listdir(second), [], "新目录应当是空的")


def test_different_seconds_still_get_fresh_dirs():
    root = _root()
    a = ads_ops.unique_sim_dir(root, "C", now=FIXED)
    b = ads_ops.unique_sim_dir(root, "C", now=FIXED + datetime.timedelta(seconds=1))
    ne(a, b)
    eq(os.path.basename(b), "C_20260924_163006")


def test_parent_dir_is_created_on_demand():
    root = os.path.join(_root(), "deep", "workspace")
    ok(not os.path.exists(root), "前置条件：工作区目录还不存在")
    d = ads_ops.unique_sim_dir(root, "X", now=FIXED)
    ok(os.path.isdir(d))
    eq(os.path.dirname(d), os.path.join(root, ads_ops.SIM_ROOT))


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------

def test_concurrent_threads_get_distinct_dirs():
    root = _root()
    n = 16
    results: list = []
    errors: list = []
    barrier = threading.Barrier(n)

    def worker():
        try:
            barrier.wait(timeout=15)
            results.append(ads_ops.unique_sim_dir(root, "Same", now=FIXED))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    eq(errors, [], f"并发创建出错: {errors}")
    eq(len(results), n)
    eq(len(set(results)), n, f"并发创建出现了重名目录: {sorted(results)}")
    for d in results:
        ok(os.path.isdir(d), f"目录没有建出来: {d}")
        eq(os.listdir(d), [], "新目录应当是空的")


def test_concurrent_processes_get_distinct_dirs():
    """多个进程同时在同一秒建目录 —— 不靠时间戳精度，也不靠随机数。"""
    root = _root()
    per_proc = 8
    script = os.path.join(root, "_mk.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, r'{ADDON}')\n"
            "import datetime, ads_ops\n"
            "root = sys.argv[1]; n = int(sys.argv[2])\n"
            "fixed = datetime.datetime(2026, 9, 24, 16, 30, 5)\n"
            "for _ in range(n):\n"
            "    print(ads_ops.unique_sim_dir(root, 'Same', now=fixed), flush=True)\n"
        )
    procs = [
        subprocess.Popen([sys.executable, script, root, str(per_proc)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(3)
    ]
    dirs: list = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        eq(p.returncode, 0, f"子进程失败: {err[-400:]}")
        dirs.extend(line.strip() for line in out.splitlines() if line.strip())

    eq(len(dirs), 3 * per_proc, f"目录数不对: {len(dirs)}")
    eq(len(set(dirs)), len(dirs), "跨进程出现了重名目录（会覆盖彼此的仿真结果）")
    for d in dirs:
        ok(os.path.isdir(d), f"目录没有建出来: {d}")


# ---------------------------------------------------------------------------
# 名字安全 / 边界
# ---------------------------------------------------------------------------

def test_cell_name_is_sanitized_for_the_filesystem():
    eq(ads_ops._safe_name("a/b:c*d"), "a_b_c_d")
    eq(ads_ops._safe_name('a<b>c|d?e"f\\g'), "a_b_c_d_e_f_g")
    eq(ads_ops._safe_name("normal_name-2"), "normal_name-2")
    eq(ads_ops._safe_name(""), "design")
    eq(ads_ops._safe_name("   "), "design")
    eq(ads_ops._safe_name("trailing."), "trailing")


def test_illegal_cell_name_does_not_escape_the_sim_root():
    root = _root()
    d = ads_ops.unique_sim_dir(root, "../evil/cell", now=FIXED)
    eq(os.path.dirname(d), os.path.join(root, ads_ops.SIM_ROOT),
       "非法 cell 名让目录跑到了仿真根目录之外")
    ok(os.path.isdir(d))
    not_contains(os.path.basename(d), "..")


def test_exhausted_suffixes_raise_a_clear_error():
    root = _root()
    ads_ops.unique_sim_dir(root, "C", now=FIXED)
    ads_ops.unique_sim_dir(root, "C", now=FIXED)
    try:
        ads_ops.unique_sim_dir(root, "C", now=FIXED, tries=2)
    except RuntimeError as e:
        contains(str(e), "C_20260924_163005", "报错应指出撞名的前缀")
        contains(str(e), ads_ops.SIM_ROOT, "报错应指出要清理哪个目录")
    else:
        raise AssertionError("后缀用尽时应抛出可操作的 RuntimeError")


# ---------------------------------------------------------------------------
# 回归护栏：run_simulation 必须走 unique_sim_dir
# ---------------------------------------------------------------------------

def test_run_simulation_uses_unique_sim_dir():
    """防止有人把实现改回"秒级时间戳 + exist_ok=True"。

    run_simulation 本身要 ADS 才能跑，所以这里做源码级检查 —— 它是这个
    缺陷唯一的"入口"，值得加一道护栏。
    """
    src = inspect.getsource(ads_ops.run_simulation)
    contains(src, "unique_sim_dir", "run_simulation 没有使用 unique_sim_dir")
    not_contains(src, "strftime", "run_simulation 又回到秒级时间戳命名了")
    not_contains(src, "exist_ok=True", "run_simulation 又用了 exist_ok=True 建目录")

    body = inspect.getsource(ads_ops.unique_sim_dir)
    contains(body, "exist_ok=False", "唯一性依赖 makedirs 的原子性，不能改掉")


if __name__ == "__main__":
    try:
        code = run(globals(), "仿真输出目录唯一性")
    finally:
        _teardown()
    sys.exit(code)
