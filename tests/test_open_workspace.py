"""open_workspace 工具与「无工作区」统一口径的回归。

场景来自 2026-10-12 实机日志：ADS 重启后工作区未打开，工具报错口径不一致
（list_designs 是友好中文、odesign 透传底层英文、ws() 静默返回 None），
模型只能靠十几次 run_python 探索、外加一次 120 秒的全盘 os.walk 才自己
发现 de.open_workspace。这里验证：恢复路径一步可达、错误信息统一指路、
最近工作区记忆按数据根隔离。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, contains, eq, ok, raises, run  # noqa: E402

add_path("addon", "ads_agent")
import ads_ops  # noqa: E402
from unittest.mock import patch  # noqa: E402


class _FakeWS:
    def __init__(self, path):
        self.path = path
        self.library_names = ["Demo_lib"]
        self.writable_library_names = ["Demo_lib"]
        self.libraries = []


class _FakeDe:
    def __init__(self):
        self.is_open = False
        self.ws_obj = None
        self.opened_paths = []

    def workspace_is_open(self):
        return self.is_open

    def active_workspace(self):
        return self.ws_obj

    def open_workspace(self, path):
        self.opened_paths.append(path)
        self.ws_obj = _FakeWS(path)
        self.is_open = True
        return self.ws_obj


class _Env:
    """每个用例独立的数据根 + 工作区目录，互不串读 last_workspace.txt。"""

    def __init__(self):
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="ads_openws_")
        self.data = os.path.join(self.tmp, "data")
        self.wrk = os.path.join(self.tmp, "wrk")
        os.makedirs(self.wrk, exist_ok=True)
        with open(os.path.join(self.wrk, "lib.defs"), "w", encoding="utf-8") as f:
            f.write("VERSION 1.0\n")
        self._old = os.environ.get("ADS_AGENT_DATA_DIR")
        os.environ["ADS_AGENT_DATA_DIR"] = self.data

    def record_last(self, path):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "last_workspace.txt"), "w", encoding="utf-8") as f:
            f.write(path)

    def last_record(self):
        fp = os.path.join(self.data, "last_workspace.txt")
        return open(fp, encoding="utf-8").read() if os.path.exists(fp) else None

    def close(self):
        import shutil

        if self._old is None:
            os.environ.pop("ADS_AGENT_DATA_DIR", None)
        else:
            os.environ["ADS_AGENT_DATA_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_open_workspace_restores_last_record_without_path():
    env = _Env()
    fake = _FakeDe()
    try:
        env.record_last(env.wrk)
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.open_workspace({})
        eq(r.get("workspace_open"), True)
        eq(os.path.normcase(os.path.normpath(r["path"])),
           os.path.normcase(os.path.normpath(env.wrk)), "打开的正是最近记录的工作区")
        eq(fake.opened_paths, [env.wrk], "de.open_workspace 按记录路径调用")
        eq(env.last_record(), os.path.normpath(env.wrk), "打开后最近记录刷新为规范路径")
    finally:
        env.close()


def test_open_workspace_validates_path():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            missing = os.path.join(env.tmp, "nope")
            err = raises(RuntimeError, lambda: ads_ops.open_workspace({"path": missing}))
            contains(str(err), "工作区目录不存在")

            bare = os.path.join(env.tmp, "bare")
            os.makedirs(bare, exist_ok=True)
            err = raises(RuntimeError, lambda: ads_ops.open_workspace({"path": bare}))
            contains(str(err), "缺少 lib.defs")

            # 无 path 且没有任何最近记录
            err = raises(RuntimeError, lambda: ads_ops.open_workspace({}))
            contains(str(err), "没有可用的最近工作区记录")
        ok(not fake.opened_paths, "非法路径不触发 de.open_workspace")
    finally:
        env.close()


def test_open_workspace_idempotent_when_same_path_already_open():
    env = _Env()
    fake = _FakeDe()
    try:
        env.record_last(env.wrk)
        with patch.object(ads_ops, "_de", return_value=fake):
            ads_ops.open_workspace({})
            again = ads_ops.open_workspace({})
        eq(again.get("already_open"), True, "同路径重复打开返回 already_open")
        eq(len(fake.opened_paths), 1, "没有重复调用底层 open_workspace")
    finally:
        env.close()


def test_require_workspace_error_points_to_recovery():
    env = _Env()
    fake = _FakeDe()  # is_open = False
    try:
        env.record_last(env.wrk)
        with patch.object(ads_ops, "_de", return_value=fake):
            err = raises(ads_ops.PreconditionError,
                         lambda: ads_ops._require_workspace())
        contains(str(err), "open_workspace")
        contains(str(err), env.wrk, "错误里直接给出最近工作区路径")
        ok(isinstance(err, RuntimeError),
           "前置条件错误仍是 RuntimeError 子类，旧调用方不受影响")
    finally:
        env.close()


def test_missing_recent_workspace_is_precondition_not_crash():
    """最近工作区缺失是调用方可改正的状态，不能当成服务 500。"""
    env = _Env()
    try:
        fake = _FakeDe()
        fake.open_workspace = lambda path: setattr(fake, "opened_paths",
                                                   fake.opened_paths + [path]) or None
        with patch.object(ads_ops, "_de", return_value=fake), \
                patch.object(ads_ops, "_last_workspace_path", return_value=""):
            err = raises(ads_ops.PreconditionError,
                         lambda: ads_ops.open_workspace({}))
        contains(str(err), "path", "要给出可执行下一步")
    finally:
        env.close()


def test_run_python_helpers_use_unified_hint():
    env = _Env()
    prev = ads_ops._PY_ENV
    ads_ops._PY_ENV = None
    try:
        # 测试进程没有 keysight.ads.de -> env["de"] 为 None，
        # 与实机「ADS 重启后没开工作区」同一条路径
        r = ads_ops.run_python({"code": "odesign('X_lib', 'Y_cell')"})
        eq(r["ok"], False)
        contains(r["stdout"], "open_workspace", "odesign 报错直接指路恢复")

        r = ads_ops.run_python({"code": "import json; print(json.dumps(ls()))"})
        eq(r["ok"], True)
        contains(r["stdout"], "open_workspace", "ls() 速览的 error 同样指路")
    finally:
        ads_ops._PY_ENV = prev
        env.close()


def test_run_python_open_workspace_helper_restores_session():
    env = _Env()
    fake = _FakeDe()
    prev = ads_ops._PY_ENV
    ads_ops._PY_ENV = None
    try:
        env.record_last(env.wrk)
        real_de = ads_ops._de
        with patch.object(ads_ops, "_de", return_value=fake):
            # 助手 open_workspace 在 env 里绑定的是模块级工具，
            # 工具内部再取 _de() —— patch 生效
            r = ads_ops.run_python({"code": "w = open_workspace(); print(w is not None)"})
        eq(r["ok"], True, r["stdout"])
        contains(r["stdout"], "[workspace] path:")
        ok(fake.opened_paths, "底层 open_workspace 被调用")
    finally:
        ads_ops._PY_ENV = prev
        env.close()


if __name__ == "__main__":
    raise SystemExit(run(globals()))
