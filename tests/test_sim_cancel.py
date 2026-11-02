"""仿真执行路径的取消 / 预算 / 进程回收。

背景：keysight.edatoolbox 的 ``CircuitSimulator.run_netlist`` **没有**
timeout 参数，也没有 cancel 方法 —— 它最终走 ``util.execute`` 里的一句
``subprocess.check_call``，同步阻塞、无法中途打断。所以本插件在
``ads_ops._simulate`` 里把 ``_execute`` 改成自己 ``Popen``，从而：

  * 拿得到 hpeesofsim 的 pid，取消时能只终止**自己 spawn 的那棵树**；
  * 执行预算到期由本插件终止子进程并如实标 ``timed_out``，而不是让 HTTP
    层的 socket 超时"假装"结束了作业（那个只结束等待，进程照样跑到底）；
  * 输出重定向到文件而不是管道，大量 stdout/stderr 不会因写满而卡死。

这里用真的子进程（假 hpeesofsim 脚本）验证，不用 mock 假装"取消成功"。
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

from _harness import add_path, eq, ok, run

add_path("addon", "ads_agent")
add_path("backend")

import ads_ops  # noqa: E402

PYTHON = sys.executable

# 非协作热循环：只能被 terminate/taskkill 打断，模拟"跑飞的 hpeesofsim"
_HOT = """
import sys
n = 0
while True:
    n += sum(range(2000))
    if n % 5000 == 0:
        sys.stdout.write('.')
        sys.stdout.flush()
"""

# 协作退出：收到停止标志（我们这里用"哨兵文件"模拟）后自己收尾
_FLOOD = """
import sys
for i in range(200000):
    sys.stdout.write('line %d ' % i + 'x' * 40 + '\\n')
sys.stderr.write('err ' * 2000 + '\\n')
"""

_FAIL = """
import sys
sys.stdout.write('fatal: license checkout failed\\n')
sys.stdout.flush()
sys.exit(3)
"""


class Ctx:
    """JobContext 的鸭子类型替身：只提供 cancelled() / set_phase() / note()。"""

    def __init__(self, job_id="sim-job-1"):
        self.job_id = job_id
        self.cancel_flag = False
        self.phases = []
        self.notes = []
        self.sim_off_main_thread = True

    def cancelled(self):
        return bool(self.cancel_flag)

    def set_phase(self, phase):
        self.phases.append(phase)

    def note(self, msg):
        self.notes.append(msg)


def _stub_keysight(sim_cls):
    keysight = types.ModuleType("keysight")
    toolbox = types.ModuleType("keysight.edatoolbox")
    toolbox.ads = types.SimpleNamespace(CircuitSimulator=sim_cls)
    adsmod = types.ModuleType("keysight.ads")
    dataset = types.ModuleType("keysight.ads.dataset")
    dataset.open = lambda path: {}
    adsmod.dataset = dataset
    return {"keysight": keysight, "keysight.edatoolbox": toolbox,
            "keysight.ads": adsmod, "keysight.ads.dataset": dataset}


def _make_sim_cls(cmd_builder):
    """造一个与 SDK 同形的 CircuitSimulator：run_netlist 里调 _execute(cmd, ...)。"""

    class FakeSimulator:
        def run_netlist(self, netlist, output_dir, dataset_name):
            cmd = cmd_builder(output_dir)
            out = os.path.join(output_dir, "hpeesofsim.log")
            self._execute(cmd, output_file=out, error_file=out, cwd=None, extra_env={})
            Path(output_dir, dataset_name + ".ds").write_bytes(b"dataset")

    return FakeSimulator


class SimEnv:
    """临时输出目录 + sys.modules 打桩 + 预算覆盖。"""

    def __init__(self, script, budget_s=None):
        self.dir = tempfile.mkdtemp(prefix="ads_agent_simcancel_")
        self.output = os.path.join(self.dir, "sim_output")
        os.makedirs(self.output, exist_ok=True)
        self.script = os.path.join(self.dir, "fake_hpeesofsim.py")
        Path(self.script).write_text(script, encoding="utf-8")
        self.budget_s = budget_s
        self._saved_modules = None
        self._saved_budget = None

    def __enter__(self):
        self._saved_modules = dict(sys.modules)
        sim_cls = _make_sim_cls(lambda out: [PYTHON, self.script, out])
        sys.modules.update(_stub_keysight(sim_cls))
        if self.budget_s is not None:
            self._saved_budget = ads_ops._sim_budget_s
            value = float(self.budget_s)
            ads_ops._sim_budget_s = lambda: value
        return self

    def __exit__(self, *exc):
        if self._saved_budget is not None:
            ads_ops._sim_budget_s = self._saved_budget
        for name, mod in self._saved_modules.items():
            sys.modules[name] = mod
        for name in ("keysight.ads.dataset", "keysight.ads", "keysight.edatoolbox", "keysight"):
            if name not in self._saved_modules:
                sys.modules.pop(name, None)
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def run(self, ctx=None, timeout_s=60):
        audit = {"n_instances": 1, "controllers": 1, "ports": 2, "n_nets": 1,
                 "warnings": [], "_vendor_dependencies": {}, "_workspace": "ws",
                 "_design_version": "v1"}
        return ads_ops._simulate("NETLIST", self.output, "test", "", audit, ctx=ctx)


def _wait_active_sim(job_key="", timeout=15.0):
    """等到 _SIM_PROCS 里出现登记（有界轮询，不用固定 sleep）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with ads_ops._SIM_PROCS_LOCK:
            if job_key in ads_ops._SIM_PROCS or (not job_key and ads_ops._SIM_PROCS):
                return True
        time.sleep(0.02)
    return False


def _sim_procs_empty():
    with ads_ops._SIM_PROCS_LOCK:
        return not ads_ops._SIM_PROCS


def _run_in_thread(fn, timeout_s=60):
    box = {}

    def _worker():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 — 交给断言去判
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_s)
    ok(not t.is_alive(), f"仿真路径必须在 {timeout_s}s 内返回（实际仍在阻塞）")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def test_cancel_terminates_only_our_child_and_reports_cancelled():
    """取消：子进程被终止回收，结果如实标 cancelled + partial，不是成功。"""
    ctx = Ctx()
    with SimEnv(_HOT, budget_s=120) as env:
        box = {}

        def _run():
            box["result"] = env.run(ctx=ctx)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        ok(_wait_active_sim(ctx.job_id), "仿真子进程应已登记（等待有界轮询超时）")
        with ads_ops._SIM_PROCS_LOCK:
            pid = ads_ops._SIM_PROCS[ctx.job_id]["proc"].pid
        ok(ads_ops._pid_alive(pid), "取消前子进程应仍在运行")
        ctx.cancel_flag = True                       # 只置位，模拟参数化取消
        t.join(30)
        ok(not t.is_alive(), "取消后仿真调用必须收工，不能继续挂住")
        result = box["result"]
        eq(result["status"], "cancelled", "取消后状态必须是 cancelled")
        ok(result.get("partial") is True, "取消的结果是部分结果，必须标 partial")
        ok(not ads_ops._pid_alive(pid), "取消后不得留下孤儿仿真进程")
        ok(_sim_procs_empty(), "取消后必须从活跃登记表里摘掉")
        ok(any(p.startswith("sim:") for p in ctx.phases), "必须上报仿真阶段（供排查卡在哪一步）")


def test_execution_budget_marks_timed_out_and_reaps():
    """执行预算到期：终止子进程并标 timed_out，不依赖 HTTP 层超时。"""
    ctx = Ctx("sim-budget")
    with SimEnv(_HOT, budget_s=1.0) as env:
        t0 = time.monotonic()
        result = _run_in_thread(lambda: env.run(ctx=ctx), timeout_s=60)
        elapsed = time.monotonic() - t0
        eq(result["status"], "timed_out", "预算到期必须是 timed_out")
        ok(result.get("partial") is True, "timed_out 的结果是部分结果")
        ok(elapsed < 40, f"应在预算 + 宽限附近返回，实际 {elapsed:.1f}s")
        ok("sim:budget_expired" in ctx.phases or "sim:running" in ctx.phases,
           "必须上报阶段，让用户看到卡在仿真")
        ok(_sim_procs_empty(), "预算到期后不得残留进程登记")


def test_large_stdout_and_stderr_do_not_deadlock():
    """输出洪泛：重定向到文件（不是管道），不得因写满而卡死。"""
    ctx = Ctx("sim-flood")
    with SimEnv(_FLOOD, budget_s=120) as env:
        result = _run_in_thread(lambda: env.run(ctx=ctx), timeout_s=90)
        eq(result["status"], "done", "输出洪泛不应影响仿真完成")
        log_path = os.path.join(env.output, "hpeesofsim.log")
        ok(os.path.getsize(log_path) > 100000, "输出应被写进文件（证明没被截断/卡住）")
        ok(_sim_procs_empty(), "完成后不得残留登记")


def test_child_failure_keeps_execution_error_semantics():
    """子进程非零退出：仍然走"仿真失败"路径，但带上 rc 与真实输出。"""
    ctx = Ctx("sim-fail")
    with SimEnv(_FAIL, budget_s=120) as env:
        try:
            env.run(ctx=ctx)
        except RuntimeError as e:
            text = str(e)
            ok("仿真失败" in text, f"应报仿真失败，实际：{text[:200]}")
            ok("license" in text, "必须带出 hpeesofsim 的真实输出（license 报错）")
        else:
            raise AssertionError("非零退出必须报错，不能被当成成功")


def test_cancel_of_idle_path_reports_not_running():
    """没有在跑仿真时取消：如实说 not_running，不假装"已停止"。"""
    receipt = ads_ops.cancel_sim_process(None, "no-such-job")
    eq(receipt["outcome"], "not_running", "没有在跑的仿真时必须如实报 not_running")
    eq(receipt["pid"], None, "没有进程就没有 pid")


def test_cancel_is_idempotent_after_completion():
    """已经结束之后再取消：不炸、不误报，也不碰别人的进程。"""
    ctx = Ctx("sim-done")
    with SimEnv(_FAIL, budget_s=120) as env:
        try:
            env.run(ctx=ctx)
        except RuntimeError:
            pass
    receipt = ads_ops.cancel_sim_process(None, "sim-done")
    eq(receipt["outcome"], "not_running", "已完成/已清理后取消必须报 not_running")
    ok(_sim_procs_empty(), "取消不应在登记表里留下幽灵条目")


def test_pid_probe_never_kills_the_target():
    """_pid_alive 必须是只读探测：绝不能用 os.kill(pid, 0) 那种会真杀进程的写法。

    Windows 上 os.kill 的第二参数是**退出码**，会直接 TerminateProcess。
    """
    import subprocess

    proc = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(30)"])
    try:
        for _ in range(200):
            if ads_ops._pid_alive(proc.pid):
                break
            time.sleep(0.02)
        ok(ads_ops._pid_alive(proc.pid), "探测活进程必须返回 True")
        ok(proc.poll() is None, "探测本身不得终止目标进程")
    finally:
        proc.kill()
        proc.wait(timeout=10)
    for _ in range(200):
        if not ads_ops._pid_alive(proc.pid):
            break
        time.sleep(0.02)
    ok(not ads_ops._pid_alive(proc.pid), "已退出的进程必须探测为 False")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "仿真取消 / 执行预算 / 进程回收"))
