"""独立计算 worker（backend/compute_worker.py）的行为测试。

为什么单独测 worker
-------------------
worker 是**协议边界**：管理器只跟 job_dir 里的文件打交道（request.json 进、
result.json 出、stop 是协作取消标志）。这条边界出错的后果很具体：

- 脚本在 check_cancel() 后 break 却报 succeeded → 部分产物被当成完整成功；
- 超时看门狗在 C 级热循环里失效 → 进程再也不退出（真机上就是"卡死"）；
- stdout 灌满管道 → worker 阻塞在自己写的日志上；
- 用户代码抛异常 / 硬 exit → 管理器拿到"没有结果文件"，后端不能因此崩。

所以这些用例**直接起真实子进程**跑真实 NumPy/SciPy 计算（不是 sleep 假装），
断言 result.json 的字段语义。纪律：临时目录；事件/有界轮询，不用固定 sleep
制造竞态；后台线程异常必须判失败（_harness 已接管 threading.excepthook）。

运行::

    python tests/test_compute_worker.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run, skip  # noqa: E402

BACKEND = add_path("backend")

WORKER = os.path.join(BACKEND, "compute_worker.py")
PYTHON = sys.executable


def _have_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class JobDir:
    """一个临时 job_dir：写 request.json、读 result.json、放 stop 标志。"""

    def __init__(self, code, inputs=None, timeout_s=60.0, job_id="w-job"):
        self.path = tempfile.mkdtemp(prefix="ads_agent_worker_")
        self.job_id = job_id
        self.request = {
            "job_id": job_id,
            "workspace": "test-ws",
            "code": code,
            "inputs": inputs,
            "timeout_s": timeout_s,
            "created_ts": time.time(),
        }

    def __enter__(self):
        with open(os.path.join(self.path, "request.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.request, f, ensure_ascii=False)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)

    # -- 文件访问 -------------------------------------------------------
    def stop(self) -> str:
        path = os.path.join(self.path, "stop")
        with open(path, "w", encoding="utf-8") as f:
            f.write("cancel")
        return path

    def result(self) -> dict:
        path = os.path.join(self.path, "result.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def log_text(self) -> str:
        try:
            with open(os.path.join(self.path, "log.txt"),
                      encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    # -- 执行 -----------------------------------------------------------
    def run(self, timeout=120.0):
        """跑 worker，返回 (returncode, result dict)。"""
        proc = subprocess.run(
            [PYTHON, WORKER, self.path],
            capture_output=True, text=True, timeout=timeout,
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
        return proc.returncode, self.result()

    def run_with_cancel(self, delay_s: float, timeout=120.0):
        """起 worker，delay_s 后写 stop 标志（协作取消），返回 (rc, result, 用时)。"""
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [PYTHON, WORKER, self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(0x08000000 if os.name == "nt" else 0),
        )
        try:
            deadline = time.monotonic() + delay_s
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.01)
            self.stop()
            rc = proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        return rc, self.result(), time.monotonic() - t0


# ---------------------------------------------------------------------------
# A. 计算隔离：真实计算、结果与同步基线一致
# ---------------------------------------------------------------------------

_BOUNDED_CODE = """
import numpy as np
from scipy.optimize import minimize

n = int(inputs["n"])
rng = np.random.default_rng(20261030)
a = rng.standard_normal((n, n))
b = rng.standard_normal(n)

def f(x):
    r = a @ x - b
    return float(r @ r)

progress("开始 L-BFGS-B 迭代")
res = minimize(f, np.zeros(n), method="L-BFGS-B",
               options={"maxiter": 60})
progress("迭代结束")
result = {"fun": float(res.fun), "n": n, "iters": int(res.nit)}
"""


def _sync_baseline(n: int) -> dict:
    """同参数的同步基线（本进程内算一遍），用来证明 worker 数值没走样。"""
    import numpy as np
    from scipy.optimize import minimize

    rng = np.random.default_rng(20261030)
    a = rng.standard_normal((n, n))
    b = rng.standard_normal(n)

    def f(x):
        r = a @ x - b
        return float(r @ r)

    res = minimize(f, np.zeros(n), method="L-BFGS-B", options={"maxiter": 60})
    return {"fun": float(res.fun), "n": n, "iters": int(res.nit)}


def test_bounded_cpu_computation_matches_sync_baseline():
    if not _have_numpy():
        skip("缺 numpy/scipy，无法做有界 CPU 计算")
    with JobDir(_BOUNDED_CODE, inputs={"n": 40}, timeout_s=120) as job:
        rc, res = job.run(timeout=180)
    eq(rc, 0, "worker 正常退出码")
    eq(res.get("state"), "succeeded", f"有界计算应成功：{res.get('error')}")
    ok(res.get("result"), "必须交出结果对象")
    base = _sync_baseline(40)
    got = res["result"]
    eq(got["n"], base["n"], "输入 n 必须原样传到计算进程")
    ok(abs(got["fun"] - base["fun"]) <= max(1e-6, abs(base["fun"]) * 1e-6),
       f"目标函数值须与同步基线一致：worker={got['fun']} 基线={base['fun']}")
    eq(res.get("partial"), False, "完整算完的结果不得标 partial")
    ok(res.get("worker_pid") and res["worker_pid"] != os.getpid(),
       "worker_pid 必须是子进程自己的 pid，不能是调用方 pid")


def test_inputs_are_explicit_and_isolated_between_jobs():
    """跨作业不得串数据：同一 worker 文件协议、不同 inputs，结果互不影响。"""
    code = "result = {'got': inputs['v'] * 2, 'keys': sorted(inputs)}"
    with JobDir(code, inputs={"v": 21}) as a:
        rc_a, res_a = a.run()
    with JobDir(code, inputs={"v": 4}) as b:
        rc_b, res_b = b.run()
    eq((rc_a, rc_b), (0, 0), "两次作业都应正常退出")
    eq(res_a["result"]["got"], 42, "第一个作业的结果")
    eq(res_b["result"]["got"], 8, "第二个作业的结果（不得被前一次污染）")
    eq(res_b["result"]["keys"], ["v"], "inputs 只含显式传入的键")


def test_ndarray_result_is_sanitized_to_json_list():
    if not _have_numpy():
        skip("缺 numpy，无法验证 ndarray 净化")
    code = ("import numpy as np\n"
            "result = {'arr': np.arange(5), 'scalar': np.float64(1.5),"
            " 'nan': float('nan')}\n")
    with JobDir(code) as job:
        rc, res = job.run()
    eq(rc, 0, "退出码")
    eq(res.get("state"), "succeeded", "应成功")
    eq(res["result"]["arr"], [0, 1, 2, 3, 4], "ndarray 必须转成 JSON 列表")
    eq(res["result"]["scalar"], 1.5, "numpy 标量必须转成普通标量")
    eq(res["result"]["nan"], "nan", "非有限浮点必须转字符串（JSON 不允许 NaN）")


def test_emit_result_wins_over_result_variable():
    code = ("result = {'from': 'var'}\n"
            "emit_result({'from': 'emit'})\n")
    with JobDir(code) as job:
        rc, res = job.run()
    eq(rc, 0, "退出码")
    eq(res["result"], {"from": "emit"}, "emit_result 优先于 result 变量")
    eq(res.get("source"), "emit_result", "必须如实记录结果来源")


def test_missing_result_reports_hint_instead_of_fake_success():
    with JobDir("x = 1 + 1\n") as job:
        rc, res = job.run()
    eq(rc, 0, "无结果也应正常退出（state 里说明）")
    eq(res.get("state"), "succeeded", "脚本本身跑完了")
    eq(res.get("result"), None, "没有结果对象")
    contains(str(res.get("no_result_hint")), "emit_result",
             "必须告诉调用方怎么交结果，而不是假装有结果")


# ---------------------------------------------------------------------------
# B. 协作取消：如实 cancelled + partial，且保留已算出的部分结果
# ---------------------------------------------------------------------------

_CANCEL_CODE = """
import time
import numpy as np
acc = 0
for i in range(20000):
    if check_cancel():
        progress("收到取消，安全边界退出")
        break
    acc += float(np.sqrt(i))
    time.sleep(0.002)
result = {"acc": acc}
"""


def test_cooperative_cancel_is_cancelled_and_partial_not_success():
    if not _have_numpy():
        skip("缺 numpy")
    with JobDir(_CANCEL_CODE, timeout_s=120) as job:
        rc, res, _elapsed = job.run_with_cancel(0.4)
    eq(rc, 0, "协作取消应走正常退出路径（结果文件已写）")
    eq(res.get("state"), "cancelled",
       "check_cancel() 后停止的作业必须是 cancelled，不能报 succeeded")
    eq(res.get("partial"), True, "中途停止的结果必须标 partial")
    ok(res.get("result") is not None, "已算出的部分结果应保留（不是丢弃）")
    contains(str(res.get("error") or ""), "取消", "错误文案要说明是取消")


def test_stop_flag_written_before_start_prevents_execution():
    """派发竞态窗口：stop 已存在时一个字节的用户代码都不许跑。"""
    marker_code = ("open(inputs['marker'], 'w').write('ran')\n"
                   "result = 1\n")
    with tempfile.TemporaryDirectory(prefix="ads_agent_worker_") as tmp:
        marker = os.path.join(tmp, "marker.txt")
        with JobDir(marker_code, inputs={"marker": marker}) as job:
            job.stop()                      # 起进程前先放 stop
            rc, res = job.run()
    eq(rc, 0, "取消前置的作业正常退出")
    eq(res.get("state"), "cancelled", "必须报 cancelled")
    eq(res.get("error_kind"), "cancelled_before_start", "错误类别要能区分")
    ok(not os.path.exists(marker), "用户代码绝不能被执行")


# ---------------------------------------------------------------------------
# C. 超时：看门狗对 C 级热循环也有效
# ---------------------------------------------------------------------------

_HOT_LOOP_CODE = """
# 纯 C 级热循环：GIL 频繁释放，check_cancel() 一次都不调
import numpy as np
a = np.ones((1400, 1400))
b = a.copy()
for _ in range(100000):
    c = a @ b
result = float(c.sum())
"""


def test_timeout_watchdog_kills_hot_loop_and_marks_timed_out():
    if not _have_numpy():
        skip("缺 numpy")
    with JobDir(_HOT_LOOP_CODE, timeout_s=2.0) as job:
        t0 = time.monotonic()
        rc, res = job.run(timeout=120)
        elapsed = time.monotonic() - t0
    eq(rc, 9, "超时由 worker 内部看门狗 os._exit(9)")
    eq(res.get("state"), "timed_out", "超时必须如实标 timed_out")
    eq(res.get("error_kind"), "timeout", "错误类别")
    ok(elapsed < 60, f"看门狗应在预算附近生效，实际 {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# D. 输出洪泛 / 崩溃 / 坏请求：都不能带崩调用方
# ---------------------------------------------------------------------------

def test_output_flood_is_bounded_and_does_not_deadlock():
    code = ("import sys\n"
            "for i in range(60000):\n"
            "    sys.stdout.write('x' * 100 + '\\n')\n"
            "sys.stderr.write('E' * 50000 + '\\n')\n"
            "result = {'done': True}\n")
    with JobDir(code, timeout_s=120) as job:
        rc, res = job.run(timeout=180)
    eq(rc, 0, "大量输出也必须正常跑完（无管道死锁）")
    eq(res.get("state"), "succeeded", "状态")
    eq(res.get("partial"), True, "日志被截断必须如实标 partial")
    ok(len(res.get("stdout_tail") or "") <= 256 * 1024,
       "stdout 尾部保留量不得超过 256KB")


def test_user_exception_becomes_failed_with_traceback():
    code = "def boom():\n    raise ValueError('故意炸一个')\nboom()\n"
    with JobDir(code) as job:
        rc, res = job.run()
    eq(rc, 0, "用户脚本异常不算 worker 崩溃，仍写结果文件")
    eq(res.get("state"), "failed", "必须报 failed")
    eq(res.get("error_kind"), "ValueError", "错误类别用异常类型名")
    contains(str(res.get("error")), "故意炸一个", "错误文案要带上原始异常信息")


def test_hard_exit_leaves_no_result_file_for_manager():
    """os._exit 不带任何结果：管理器据此判 crashed，worker 侧只保证不挂住。"""
    with JobDir("import os\nos._exit(7)\n") as job:
        rc, res = job.run()
    eq(rc, 7, "硬退出码原样返回")
    eq(res, {}, "没有 result.json —— 由管理器如实归为崩溃，不伪造状态")


def test_bad_request_json_exits_with_code_2():
    with tempfile.TemporaryDirectory(prefix="ads_agent_worker_") as tmp:
        with open(os.path.join(tmp, "request.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        proc = subprocess.run([PYTHON, WORKER, tmp], capture_output=True,
                              text=True, timeout=60,
                              creationflags=(0x08000000 if os.name == "nt" else 0))
        rc = proc.returncode
        with open(os.path.join(tmp, "result.json"), encoding="utf-8") as f:
            res = json.load(f)
    eq(rc, 2, "坏请求必须以专用退出码失败")
    eq(res.get("state"), "failed", "坏请求也要写结果文件说明")
    eq(res.get("error_kind"), "request_unreadable", "错误类别")


def test_progress_notes_are_recorded_and_capped():
    code = ("for i in range(1200):\n"
            "    progress('step %d' % i)\n"
            "result = 1\n")
    with JobDir(code) as job:
        rc, res = job.run(timeout=180)
    eq(rc, 0, "退出码")
    eq(res.get("state"), "succeeded", "状态")
    ok(res.get("progress_suppressed", 0) > 0,
       "超过上限的阶段记录要计数抑制，而不是无限写盘")
    ok(len(res.get("progress") or []) <= 200, "结果里只保留最近的一批阶段记录")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "独立计算 worker（子进程协议）"))
