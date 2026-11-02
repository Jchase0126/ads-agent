"""run_python 的取消边界与阶段上报（2026-10-30 卡死修复）。

背景：run_python 在 ADS 主线程里同步 ``exec``，一旦进入就无法安全中断
（脚本可能正握着 ADS 数据库写句柄，跨线程注入异常会留下半写的 design）。
历史日志里有 78.6 秒的 run_python、也有脚本里 2976 次 SciPy minimize 的记录。
修复不是"假装能杀"，而是明确三件事：

1. 还没开始执行时收到取消 —— 一行用户代码都不跑（``before_exec``）。
2. 已经在 exec 里 —— 绝不强杀，但结果如实标注 ``after_exec``。
3. 真跑了很久 —— 结果里写明主线程被占用多久，并指向 run_compute。

这些用例全部在纯逻辑层跑（不需要 ADS），断言的是**行为**而不是源码字符串。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, contains, eq, not_contains, ok, run

add_path("addon", "ads_agent")
import ads_ops


class _Ctx:
    """JobContext 的最小替身：只实现 run_python 真正会用到的两个方法。"""

    def __init__(self, cancelled_seq=None):
        self.phases = []
        self._seq = list(cancelled_seq or [])
        self._last = False

    def cancelled(self):
        if self._seq:
            self._last = bool(self._seq.pop(0))
        return self._last

    def set_phase(self, phase):
        self.phases.append(str(phase))


def _fresh_env():
    """每个用例都在干净的常驻环境上跑，互不污染。"""
    previous = ads_ops._PY_ENV
    ads_ops._PY_ENV = None
    return previous


def test_cancel_before_exec_runs_no_user_code():
    """派发窗口里就收到取消：脚本一个字节都不跑，也不碰 ADS 数据库。"""
    previous = _fresh_env()
    marker = os.path.join(tempfile.mkdtemp(prefix="rp_before_"), "ran.txt")
    try:
        ctx = _Ctx(cancelled_seq=[True])
        code = f"open({marker!r}, 'w').write('x')"
        result = ads_ops.run_python({"code": code}, ctx)
        eq(result.get("ok"), False, "未执行必须如实报失败，不能报成功")
        eq(result.get("cancelled"), True, "应标明这是被取消的")
        eq(result.get("cancel_boundary"), "before_exec",
           "取消边界应说明'执行前'——处理器根本没进去")
        eq(os.path.exists(marker), False, "取消后不得执行任何用户代码（副作用为零）")
        eq(ctx.phases, [], "未执行的脚本不应上报执行阶段")
        contains(result.get("error", ""), "未开始执行")
    finally:
        ads_ops._PY_ENV = previous


def test_normal_run_has_no_cancel_marker_and_reports_elapsed():
    """没有取消时结果形态不变（兼容旧调用方），并带上耗时。"""
    previous = _fresh_env()
    try:
        result = ads_ops.run_python({"code": "print(sum(range(10)))"}, _Ctx())
        eq(result.get("ok"), True)
        eq(result["stdout"].strip(), "45")
        eq("cancelled" in result, False,
           "没有取消请求时不应凭空多出 cancelled 字段")
        ok(isinstance(result.get("elapsed_s"), float), "应如实报告本次耗时")
    finally:
        ads_ops._PY_ENV = previous


def test_cancel_during_exec_is_reported_as_after_exec_boundary():
    """执行中才到取消：不强杀，但结果必须如实标注边界（不是"已停止"）。"""
    previous = _fresh_env()
    try:
        # cancelled() 第一次问（执行前）返回 False，第二次（执行后）返回 True
        ctx = _Ctx(cancelled_seq=[False, True])
        result = ads_ops.run_python({"code": "print('写完了')"}, ctx)
        eq(result.get("ok"), True, "脚本本身跑完了，不能谎报成失败")
        eq(result.get("cancelled"), True, "应告诉调用方取消请求确实到过")
        eq(result.get("cancel_boundary"), "after_exec",
           "取消边界必须说明'执行之后'，不能暗示脚本被中断")
        contains(result["stdout"], "写完了", "用户输出必须保留")
        contains(result["stdout"], "取消边界",
                 "结果里应明确写出'无法中断、跑完才停'")
        contains(result["stdout"], "此前的写入已发生",
                 "必须提醒调用方前面的写入已经生效，不能当成没发生")
    finally:
        ads_ops._PY_ENV = previous


def test_slow_run_warns_main_thread_occupancy_and_points_to_run_compute():
    """执行超过阈值时明确提示主线程被占用，并指向 run_compute。"""
    previous = _fresh_env()
    previous_hint = ads_ops._RUN_PYTHON_SLOW_HINT_S
    ads_ops._RUN_PYTHON_SLOW_HINT_S = 0.0   # 任何一次调用都算"慢"，无需真的等 5 秒
    try:
        result = ads_ops.run_python({"code": "x = 1"}, _Ctx())
        eq(result.get("ok"), True)
        contains(result["stdout"], "[主线程占用]")
        contains(result["stdout"], "run_compute",
                 "慢脚本提示必须指向正确的工具，否则模型还会继续用 run_python")
        contains(result["stdout"], "无法响应",
                 "要说明代价：这段时间界面是卡住的")
    finally:
        ads_ops._RUN_PYTHON_SLOW_HINT_S = previous_hint
        ads_ops._PY_ENV = previous


def test_fast_run_does_not_emit_slow_warning():
    """正常短脚本不能被慢速提示刷屏（否则模型会学会忽略它）。"""
    previous = _fresh_env()
    try:
        result = ads_ops.run_python({"code": "y = 2"}, _Ctx())
        eq(result.get("ok"), True)
        not_contains(result["stdout"], "[主线程占用]")
    finally:
        ads_ops._PY_ENV = previous


def test_phases_are_reported_for_health_visibility():
    """/health 的 jobs[].phase 要靠 set_phase：进 exec 前后都要有阶段。"""
    previous = _fresh_env()
    try:
        ctx = _Ctx()
        ads_ops.run_python({"code": "pass"}, ctx)
        ok("run_python:exec" in ctx.phases,
           f"执行阶段应可见，实际: {ctx.phases}")
        ok("run_python:done" in ctx.phases,
           f"收尾阶段应可见，实际: {ctx.phases}")
    finally:
        ads_ops._PY_ENV = previous


def test_script_error_still_reports_after_exec_cancel_boundary():
    """脚本抛异常 + 取消请求：既要保留 traceback，也要如实标注边界。"""
    previous = _fresh_env()
    try:
        ctx = _Ctx(cancelled_seq=[False, True])
        result = ads_ops.run_python({"code": "raise ValueError('探针')"}, ctx)
        eq(result.get("ok"), False, "脚本异常必须报失败")
        contains(result["stdout"], "ValueError")
        contains(result["stdout"], "探针")
        eq(result.get("cancelled"), True)
        eq(result.get("cancel_boundary"), "after_exec")
    finally:
        ads_ops._PY_ENV = previous


def test_persistent_env_and_helpers_survive_ctx_plumbing():
    """原有关键行为不能因取消/阶段接线而退化：常驻变量与助手仍可用。"""
    previous = _fresh_env()
    try:
        ads_ops.run_python({"code": "kept = 41"}, _Ctx())
        result = ads_ops.run_python({"code": "print(kept + 1, callable(ws))"}, _Ctx())
        eq(result["stdout"].strip(), "42 True",
           "常驻变量与预置助手必须跨调用保留（否则老脚本静默失效）")
    finally:
        ads_ops._PY_ENV = previous


def test_ctx_none_and_broken_ctx_never_break_the_tool():
    """ctx 缺失或实现不全（旧版 JobContext / 测试替身）时必须照常工作。"""
    previous = _fresh_env()
    try:
        eq(ads_ops.run_python({"code": "print('ok')"}, None)["stdout"].strip(), "ok")

        class _Broken:
            def cancelled(self):
                raise RuntimeError("取消状态查询炸了")

            def set_phase(self, phase):
                raise RuntimeError("阶段上报炸了")

        result = ads_ops.run_python({"code": "print('still ok')"}, _Broken())
        eq(result.get("ok"), True, "取消/阶段查询失败不能影响工具本身")
        eq(result["stdout"].strip(), "still ok")
    finally:
        ads_ops._PY_ENV = previous


if __name__ == "__main__":
    raise SystemExit(run(globals(), "run_python 取消边界与阶段上报"))
