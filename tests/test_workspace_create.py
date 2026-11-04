"""create_workspace / 工作区切换闸门 / 最近记录 的离线回归（契约 5.1–5.6）。

全部用例离线：`de` 用 _FakeDe 打桩，每个用例一个独立的临时数据根
（ADS_AGENT_DATA_DIR），绝不连接真实 ADS，也不触碰真实磁盘目录。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, contains, eq, ok, raises, run  # noqa: E402

add_path("addon", "ads_agent")
import ads_ops  # noqa: E402
from unittest.mock import patch  # noqa: E402


class _FakeWS:
    def __init__(self, path, libraries=()):
        self.path = path
        self.library_names = list(libraries)
        self.writable_library_names = list(self.library_names)
        self.libraries = []


class _FakeDe:
    """最小 de 打桩：只实现本工具用到的接口，调用全部记账。"""

    def __init__(self):
        self.is_open = False
        self.ws_obj = None
        self.reads = []          # 只读查询（不算副作用）
        self.create_calls = []
        self.open_calls = []     # [(path, force)]
        self.library_calls = []
        self._libs = {}

    def workspace_is_open(self):
        self.reads.append("workspace_is_open")
        return self.is_open

    def active_workspace(self):
        self.reads.append("active_workspace")
        return self.ws_obj

    def directory_is_workspace(self, path):
        return os.path.isfile(os.path.join(path, "lib.defs"))

    def create_workspace(self, path):
        self.create_calls.append(path)
        os.makedirs(path)
        with open(os.path.join(path, "lib.defs"), "w", encoding="utf-8") as f:
            f.write("VERSION 1.0\n")

    def create_new_library(self, name, path):
        self.library_calls.append((name, path))
        self._libs.setdefault(os.path.normcase(os.path.normpath(path)), []).append(name)

    def open_workspace(self, path, force=False):
        self.open_calls.append((path, force))
        self.ws_obj = _FakeWS(
            path, self._libs.get(os.path.normcase(os.path.normpath(path)), []))
        self.is_open = True
        return self.ws_obj

    def set_active(self, path):
        self.ws_obj = _FakeWS(path)
        self.is_open = True


class _NoCreateDe(_FakeDe):
    """模拟老版本 ADS：SDK 没有 create_workspace。"""

    create_workspace = None


class _BrokenOpenDe(_FakeDe):
    """建目录成功但打开失败：必须如实报 created=true / workspace_open=false。"""

    def open_workspace(self, path, force=False):
        self.open_calls.append((path, force))
        raise RuntimeError("boom: workspace cannot be opened")


class _Env:
    """每个用例独立的数据根 + 临时父目录，互不串读。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="ads_mkws_")
        self.data = os.path.join(self.tmp, "data")
        self.root = os.path.join(self.tmp, "wsroot")
        os.makedirs(self.root)
        self._old_data = os.environ.get("ADS_AGENT_DATA_DIR")
        self._old_root = os.environ.pop("ADS_AGENT_WORKSPACE_ROOT", None)
        os.environ["ADS_AGENT_DATA_DIR"] = self.data

    def last_txt(self):
        fp = os.path.join(self.data, "last_workspace.txt")
        if not os.path.isfile(fp):
            return None
        with open(fp, encoding="utf-8") as f:
            return f.read()

    def recent_json(self):
        with open(os.path.join(self.data, "recent_workspaces.json"), encoding="utf-8") as f:
            return json.load(f)

    def close(self):
        if self._old_data is None:
            os.environ.pop("ADS_AGENT_DATA_DIR", None)
        else:
            os.environ["ADS_AGENT_DATA_DIR"] = self._old_data
        if self._old_root is not None:
            os.environ["ADS_AGENT_WORKSPACE_ROOT"] = self._old_root
        shutil.rmtree(self.tmp, ignore_errors=True)


def _snapshot(path):
    """目录快照（相对路径 -> 内容），用于证明“已存在目录零改动”。"""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = f.read()
    return out


def test_create_default_root_creates_opens_and_records():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "X_lib"})
        want = os.path.join(env.data, "workspaces", "X_lib")
        eq(r.get("created"), True, f"应创建成功: {r}")
        eq(r.get("reason"), None, "成功结果不带 reason")
        eq(os.path.normcase(r["path"]), os.path.normcase(want), "默认根是 <数据根>/workspaces")
        eq(r["name"], "X_lib")
        eq(r["workspace_open"], True, "默认 open=true 会立即打开")
        eq(fake.create_calls, [want], "de.create_workspace 按目标路径调用")
        eq(fake.open_calls, [(want, False)], "打开用关键字 force=False")
        ok(os.path.isfile(os.path.join(want, "lib.defs")), "工作区目录含 lib.defs")
        eq(r["libraries"], [], "未指定 library 时没有库")
        eq(r["writable_libraries"], [])
        eq(r["library_created"], "")
        eq(r["previous_workspace"], "", "创建前没有打开工作区")
        eq(r["force_closed"], False)
        eq(r["notes"], [])
        eq(env.last_txt(), os.path.normpath(want), "last_workspace.txt 同步更新")
        rec = env.recent_json()
        eq(rec["version"], 1)
        eq(rec["items"][0]["path"], os.path.normpath(want), "recent 首条是刚打开的工作区")
    finally:
        env.close()


def test_create_with_root_library_and_open_false():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace(
                {"name": "Y_lib", "root": env.root, "library": "Y_lib_wrk", "open": False})
        want = os.path.join(env.root, "Y_lib")
        eq(r.get("created"), True)
        eq(os.path.normcase(r["path"]), os.path.normcase(want))
        eq(r["workspace_open"], False, "open=false 不打开")
        eq(fake.open_calls, [], "open=false 不调 de.open_workspace")
        eq(fake.library_calls, [("Y_lib_wrk", want)], "创建后建首个可写库")
        eq(r["library_created"], "Y_lib_wrk")
        eq(r["libraries"], [])
        eq(env.last_txt(), None, "没打开就不该写最近记录")
    finally:
        env.close()


def test_create_refuses_existing_without_touching_it():
    env = _Env()
    fake = _FakeDe()
    try:
        target = os.path.join(env.root, "KeepMe")
        os.makedirs(target)
        with open(os.path.join(target, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("不要动我\n")
        before = _snapshot(target)
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "KeepMe", "root": env.root})
        eq(r.get("created"), False)
        eq(r.get("reason"), "exists")
        eq(os.path.normcase(r["path"]), os.path.normcase(target))
        eq(_snapshot(target), before, "已存在目录内容零改动")
        eq(fake.create_calls, [], "拒绝时不创建")
        eq(fake.open_calls, [])
        eq(fake.reads, [], "exists 拒绝发生在任何 de 调用之前")
    finally:
        env.close()


def test_create_invalid_names_are_refused_early():
    env = _Env()
    fake = _FakeDe()
    try:
        bad_names = ["", ".", "..", "a/b", "a:b", "a*b", 'a"b', "x" * 121]
        with patch.object(ads_ops, "_de", return_value=fake):
            for bad in bad_names:
                args = {"name": bad, "root": env.root} if bad else {"root": env.root}
                r = ads_ops.create_workspace(args)
                eq(r.get("created"), False, f"{bad!r} 应被拒绝")
                eq(r.get("reason"), "invalid_name", f"{bad!r} 的拒绝原因是 invalid_name")
        eq(fake.create_calls, [], "非法名称零副作用")
        eq(fake.reads, [], "非法名称在查询 de 之前就被拒绝")
        eq(os.listdir(env.root), [], "非法名称不落盘")
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "a:b", "root": env.root})
        eq(r.get("reason"), "invalid_name")
    finally:
        env.close()


def test_create_needs_force_then_force_switches():
    env = _Env()
    fake = _FakeDe()
    try:
        old_ws = os.path.join(env.tmp, "Old")
        os.makedirs(old_ws)
        with open(os.path.join(old_ws, "lib.defs"), "w", encoding="utf-8") as f:
            f.write("VERSION 1.0\n")
        fake.set_active(old_ws)
        fresh_root = os.path.join(env.tmp, "not_yet")  # 父目录尚不存在
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "NewWs", "root": fresh_root})
        eq(r.get("created"), False)
        eq(r.get("reason"), "needs_force")
        eq(os.path.normcase(r["current_workspace"]), os.path.normcase(old_ws))
        eq(fake.create_calls, [], "needs_force 拒绝时目录都没建")
        ok(not os.path.exists(os.path.join(fresh_root, "NewWs")), "拒绝时不落盘")
        ok(not os.path.exists(fresh_root), "拒绝时连父目录都不该建（零磁盘副作用）")

        with patch.object(ads_ops, "_de", return_value=fake):
            r2 = ads_ops.create_workspace(
                {"name": "NewWs", "root": fresh_root, "force": True})
        new_ws = os.path.join(fresh_root, "NewWs")
        eq(r2.get("created"), True)
        eq(os.path.normcase(r2["previous_workspace"]), os.path.normcase(old_ws))
        eq(r2["force_closed"], True, "force=true 确实关掉了旧工作区")
        eq(r2["workspace_open"], True)
        eq(fake.open_calls, [(new_ws, True)], "force 透传给 SDK")
    finally:
        env.close()


def test_create_reports_created_but_not_opened():
    env = _Env()
    fake = _BrokenOpenDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "Half", "root": env.root})
        eq(r.get("created"), True, "目录建成了就必须如实报 created=true")
        eq(r["workspace_open"], False, "打开失败不得报成已打开")
        eq(r["path"], os.path.join(env.root, "Half"))
        ok(os.path.isdir(os.path.join(env.root, "Half")), "目录确实存在")
        ok(any("自动打开失败" in n for n in r["notes"]), f"notes 要说明打开失败: {r['notes']}")
        eq(fake.open_calls, [(os.path.join(env.root, "Half"), False)])
    finally:
        env.close()


def test_create_sdk_unsupported():
    env = _Env()
    fake = _NoCreateDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "NoSdk", "root": env.root})
        eq(r.get("created"), False)
        eq(r.get("reason"), "sdk_unsupported")
        eq(fake.create_calls, [])
        eq(fake.reads, [], "SDK 不支持时不做无谓查询")
        ok(not os.path.exists(os.path.join(env.root, "NoSdk")))
    finally:
        env.close()


def test_create_invalid_path_and_path_wins_over_name():
    env = _Env()
    fake = _FakeDe()
    try:
        # 父路径是个文件 -> invalid_path
        blocker = os.path.join(env.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"path": os.path.join(blocker, "child")})
        eq(r.get("created"), False)
        eq(r.get("reason"), "invalid_path")
        eq(fake.create_calls, [])

        # path 含非法目录名 -> invalid_name
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"path": os.path.join(env.root, "a*b")})
        eq(r.get("reason"), "invalid_name")

        # name 与 path 都给：以 path 为准
        target = os.path.join(env.root, "FromPath")
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.create_workspace({"name": "bad/name", "path": target})
        eq(r.get("created"), True, "path 优先，name 不参与")
        eq(os.path.normcase(r["path"]), os.path.normcase(target))
        eq(r["name"], "FromPath")
    finally:
        env.close()


def test_dispatch_and_run_python_helpers_registered():
    ok("create_workspace" in ads_ops.DISPATCH, "create_workspace 必须进 DISPATCH")
    ok("list_workspaces" in ads_ops.DISPATCH, "list_workspaces 必须进 DISPATCH")
    eq(ads_ops.DISPATCH["create_workspace"], ads_ops.create_workspace)
    eq(ads_ops.DISPATCH["list_workspaces"], ads_ops.list_workspaces)

    env = _Env()
    fake = _FakeDe()
    try:
        py_env = {}
        ads_ops.install_env_helpers(py_env)
        ok(callable(py_env.get("mkws")), "run_python 环境要有 mkws 助手")
        ok(callable(py_env.get("lsws")), "run_python 环境要有 lsws 助手")
        buf = io.StringIO()
        with patch.object(ads_ops, "_de", return_value=fake), \
                contextlib.redirect_stdout(buf):
            r = py_env["mkws"]("M_lib", root=env.root)
        eq(r.get("created"), True)
        contains(buf.getvalue(), "[workspace] created:", "mkws 打印创建结果")
        buf2 = io.StringIO()
        with patch.object(ads_ops, "_de", return_value=fake), \
                contextlib.redirect_stdout(buf2):
            listed = py_env["lsws"]("M_lib")
        ok(any(os.path.normcase(w["path"]) == os.path.normcase(r["path"])
               for w in listed.get("workspaces") or []), "lsws 能找到刚建的工作区")
        contains(buf2.getvalue(), "M_lib", "lsws 打印命中路径")
    finally:
        env.close()


if __name__ == "__main__":
    raise SystemExit(run(globals()))
