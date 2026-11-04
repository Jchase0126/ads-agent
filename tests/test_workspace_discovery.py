"""list_workspaces / open_workspace（名称片段·父目录·切换闸门）的离线回归。

覆盖契约 5.3、5.5、5.6：候选发现、歧义/零命中/缺 lib.defs 的决策态、
切换闸门、最近记录（原子写/去重/上限/txt 同步）、发现纪律（有界扫描、
>2000 条目跳过、3s 预算截断、绝不 os.walk 盘符根）。

全部用例离线：`de` 用 _FakeDe 打桩，每个用例独立临时数据根，不碰真实 ADS。
"""
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
    def __init__(self, path):
        self.path = path
        self.library_names = ["Demo_lib"]
        self.writable_library_names = ["Demo_lib"]
        self.libraries = []


class _FakeDe:
    def __init__(self):
        self.is_open = False
        self.ws_obj = None
        self.open_calls = []  # [(path, force)]

    def workspace_is_open(self):
        return self.is_open

    def active_workspace(self):
        return self.ws_obj

    def directory_is_workspace(self, path):
        return os.path.isfile(os.path.join(path, "lib.defs"))

    def open_workspace(self, path, force=False):
        self.open_calls.append((path, force))
        self.ws_obj = _FakeWS(path)
        self.is_open = True
        return self.ws_obj

    def set_active(self, path):
        self.ws_obj = _FakeWS(path)
        self.is_open = True


class _Env:
    """临时磁盘树：

    root_a/ws_one、root_a/ws_two、root_a/plain/nested_ws（第 2 层）
    root_a/ws_one/should_not_scan（工作区内部不再下钻，应扫不到）
    root_b/DemoProj
    """

    def __init__(self, root_env=True):
        self.tmp = tempfile.mkdtemp(prefix="ads_wslist_")
        self.data = os.path.join(self.tmp, "data")
        self.root_a = os.path.join(self.tmp, "root_a")
        self.root_b = os.path.join(self.tmp, "root_b")
        self._saved = {k: os.environ.get(k)
                       for k in ("ADS_AGENT_DATA_DIR", "ADS_AGENT_WORKSPACE_ROOT")}
        os.environ["ADS_AGENT_DATA_DIR"] = self.data
        if root_env:
            os.environ["ADS_AGENT_WORKSPACE_ROOT"] = self.root_a
        else:
            os.environ.pop("ADS_AGENT_WORKSPACE_ROOT", None)
        self.ws_one = self._mkws(os.path.join(self.root_a, "ws_one"))
        self.ws_two = self._mkws(os.path.join(self.root_a, "ws_two"))
        self.nested = self._mkws(os.path.join(self.root_a, "plain", "nested_ws"))
        self.hidden = self._mkws(os.path.join(self.ws_one, "should_not_scan"))
        self.demo = self._mkws(os.path.join(self.root_b, "DemoProj"))

    @staticmethod
    def _mkws(path):
        os.makedirs(path)
        with open(os.path.join(path, "lib.defs"), "w", encoding="utf-8") as f:
            f.write("VERSION 1.0\n")
        return os.path.normpath(path)

    def close(self):
        for key, old in self._saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        shutil.rmtree(self.tmp, ignore_errors=True)


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def test_list_workspaces_bounded_scan_and_fields():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.list_workspaces({})
        eq(r.get("error"), None)
        paths = {_norm(w["path"]) for w in r["workspaces"]}
        eq(paths, {_norm(env.ws_one), _norm(env.ws_two), _norm(env.nested)},
           "只发现已知根的 1–2 层工作区")
        eq(r["roots"], [os.path.normpath(env.root_a)], "roots 解析到 env 指定根")
        ok(r["scanned"] >= 3, f"实际检查目录数应 >3: {r['scanned']}")
        eq(r["truncated"], False)
        eq(r["recent"], [])
        for w in r["workspaces"]:
            eq(set(w), {"name", "path", "valid", "last_used", "source"})
            eq(w["source"], "scan")
            eq(w["valid"], True)
            eq(w["last_used"], "")
    finally:
        env.close()


def test_list_workspaces_query_max_and_truncation():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            one = ads_ops.list_workspaces({"query": "nested"})
            eq([_norm(w["path"]) for w in one["workspaces"]], [_norm(env.nested)],
               "query 按名称/路径片段过滤")
            capped = ads_ops.list_workspaces({"max": 1})
            eq(len(capped["workspaces"]), 1, "max 限制返回条数")
            eq(capped["truncated"], True, "因 max 截断时 truncated=true")
            large = ads_ops.list_workspaces({"max": 1000})
            eq(len(large["workspaces"]), 3, "max 上限 100 不影响真实数量")
            eq(large["truncated"], False)
    finally:
        env.close()


def test_discovery_skips_huge_dir_and_never_walks():
    env = _Env()
    fake = _FakeDe()
    try:
        huge = os.path.join(env.tmp, "huge")
        for i in range(5):
            os.makedirs(os.path.join(huge, "d%d" % i))
        with patch.object(ads_ops, "_de", return_value=fake), \
                patch.object(ads_ops, "_WS_SCAN_MAX_DIR_ENTRIES", 3):
            r = ads_ops.list_workspaces({"roots": [huge]})
        eq(r["workspaces"], [], "条目超限的目录直接跳过，不列完")
        eq(r["truncated"], True)
        ok(any("条目超过" in n for n in r["notes"]), f"notes 要说明跳过原因: {r['notes']}")

        def _boom(*a, **k):
            raise AssertionError("发现流程禁止 os.walk")

        with patch.object(ads_ops, "_de", return_value=fake), \
                patch.object(ads_ops.os, "walk", _boom):
            r2 = ads_ops.list_workspaces({})
        ok(r2["workspaces"], "禁止 os.walk 时仍能靠 scandir 发现工作区")
        ok(all(os.path.isdir(p) for p in r2["roots"]))
    finally:
        env.close()


def test_discovery_time_budget_truncates():
    env = _Env()
    fake = _FakeDe()
    ticks = iter(range(1000))
    try:
        def _clock():
            return 100.0 + next(ticks) * 0.001  # 每次读表都前进，模拟预算已耗尽

        with patch.object(ads_ops, "_de", return_value=fake), \
                patch.object(ads_ops, "_WS_SCAN_BUDGET_S", 0.0), \
                patch.object(ads_ops.time, "monotonic", _clock):
            r = ads_ops.list_workspaces({"roots": [env.root_a]})
        eq(r["truncated"], True, "预算耗尽必须 truncated=true")
        eq(r["scanned"], 0, "超预算后不再检查目录")
        ok(any("预算" in n for n in r["notes"]), f"notes 要说明预算截断: {r['notes']}")
    finally:
        env.close()


def test_discovery_depth_pruning_and_roots_cap():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.list_workspaces({})
        paths = {_norm(w["path"]) for w in r["workspaces"]}
        ok(_norm(env.nested) in paths, "第 1 层非工作区时会下钻 1 层")
        ok(_norm(env.hidden) not in paths, "工作区内部不再下钻（有界扫描）")

            # 显式 roots 超过 10 个：只扫前 10 个并写 note
        many = [os.path.join(env.tmp, "nope_%02d" % i) for i in range(12)]
        with patch.object(ads_ops, "_de", return_value=fake):
            r2 = ads_ops.list_workspaces({"roots": many})
        eq(len(r2["roots"]), ads_ops._WS_SCAN_MAX_ROOTS, "根目录数上限 10")
        ok(any("最多" in n for n in r2["notes"]), f"notes 要说明 roots 截断: {r2['notes']}")
    finally:
        env.close()


def test_open_unique_fragment_opens_target():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.open_workspace({"path": "nested"})
        eq(r.get("opened"), True, f"唯一命中应打开: {r}")
        eq(_norm(r["path"]), _norm(env.nested))
        eq(r["already_open"], False)
        eq(r["switched"], False, "此前没有打开工作区")
        eq(fake.open_calls, [(env.nested, False)], "命中后按完整路径打开且 force=False")
    finally:
        env.close()


def test_open_ambiguous_returns_candidates_without_opening():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.open_workspace({"path": "ws_"})
        eq(r.get("opened"), False)
        eq(r.get("reason"), "ambiguous")
        eq(len(r["candidates"]), 2, f"ws_one/ws_two 两个命中: {r['candidates']}")
        for c in r["candidates"]:
            eq(set(c), {"name", "path", "last_used"}, "决策态候选字段按契约")
        eq(fake.open_calls, [], "歧义时绝不替用户选，也不调 de.open_workspace")
        ok(r["message"], "要带中文说明")
    finally:
        env.close()


def test_open_not_found_and_invalid_path_decision_states():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.open_workspace({"path": "zzz_no_such_ws"})
        eq(r.get("reason"), "not_found")
        eq(r["candidates"], [])
        contains(r["message"], "list_workspaces", "not_found 要指引 list_workspaces")
        eq(fake.open_calls, [])

        bad = os.path.join(env.root_a, "badws")  # 目录存在但缺 lib.defs
        os.makedirs(bad)
        ads_ops._remember_workspace(bad)  # 让片段解析能从 recent 命中它
        with patch.object(ads_ops, "_de", return_value=fake):
            r2 = ads_ops.open_workspace({"path": "badws"})
        eq(r2.get("reason"), "invalid_path")
        eq(r2["candidates"][0]["valid"], False, "无效候选要标 valid=false")
        eq(fake.open_calls, [], "缺 lib.defs 时绝不调 de.open_workspace")

        # 显式完整路径缺 lib.defs -> 保留异常通道（既有测试依赖原文）
        with patch.object(ads_ops, "_de", return_value=fake):
            err = raises(RuntimeError, lambda: ads_ops.open_workspace({"path": bad}))
        contains(str(err), "缺少 lib.defs")
        eq(fake.open_calls, [])
    finally:
        env.close()


def test_open_trailing_separator_parent_mode():
    env = _Env()
    fake = _FakeDe()
    try:
        with patch.object(ads_ops, "_de", return_value=fake):
            amb = ads_ops.open_workspace({"path": env.root_a + os.sep})
        eq(amb.get("reason"), "ambiguous", "父目录下 2 个工作区 -> ambiguous")
        eq(len(amb["candidates"]), 2)
        eq(fake.open_calls, [])

        with patch.object(ads_ops, "_de", return_value=fake):
            one = ads_ops.open_workspace({"path": env.root_b + os.sep})
        eq(one.get("opened"), True, f"父目录下唯一工作区应打开: {one}")
        eq(_norm(one["path"]), _norm(env.demo))
        eq(fake.open_calls, [(env.demo, False)])
    finally:
        env.close()


def test_open_switch_gate_and_already_open():
    env = _Env()
    fake = _FakeDe()
    try:
        fake.set_active(env.ws_one)
        with patch.object(ads_ops, "_de", return_value=fake):
            gate = ads_ops.open_workspace({"path": env.ws_two})
        eq(gate.get("opened"), False)
        eq(gate.get("reason"), "needs_force")
        eq(_norm(gate["current_workspace"]), _norm(env.ws_one))
        eq(fake.open_calls, [], "force 缺省时不得切换（避免丢未保存修改）")

        with patch.object(ads_ops, "_de", return_value=fake):
            sw = ads_ops.open_workspace({"path": env.ws_two, "force": True})
        eq(sw.get("opened"), True)
        eq(sw["switched"], True)
        eq(sw["force_closed"], True)
        eq(fake.open_calls, [(env.ws_two, True)], "force=true 才调 SDK 且透传 force")

        # 同一路径已是当前工作区：already_open，不当成“切换”也不传 force
        fake2 = _FakeDe()
        fake2.set_active(env.ws_one)
        with patch.object(ads_ops, "_de", return_value=fake2):
            same = ads_ops.open_workspace({"path": env.ws_one, "force": True})
        eq(same.get("already_open"), True)
        eq(same["switched"], False)
        eq(fake2.open_calls, [], "already_open 分支不调用 open_workspace")
    finally:
        env.close()


def test_recent_records_atomic_dedupe_cap_and_txt_sync():
    env = _Env()
    try:
        paths = [os.path.join(env.tmp, "r_%02d" % i) for i in range(25)]
        for p in paths:
            ads_ops._remember_workspace(p)
        fp = os.path.join(env.data, "recent_workspaces.json")
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        eq(data["version"], 1)
        eq(len(data["items"]), 20, "最近记录上限 20")
        eq(data["items"][0]["path"], os.path.normpath(paths[-1]), "最近使用在前")
        for it in data["items"]:
            eq(set(it), {"path", "name", "last_used"})
        with open(os.path.join(env.data, "last_workspace.txt"), encoding="utf-8") as f:
            eq(f.read(), os.path.normpath(paths[-1]), "last_workspace.txt 同步更新")

        ads_ops._remember_workspace(paths[-1])  # 重复最前：不新增、不重复
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        eq(len(data["items"]), 20)
        eq(data["items"][0]["path"], os.path.normpath(paths[-1]))

        ads_ops._remember_workspace(paths[10])  # 旧条目提到最前
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        eq(len(data["items"]), 20)
        eq(data["items"][0]["path"], os.path.normpath(paths[10]))
        eq(len({os.path.normcase(i["path"]) for i in data["items"]}), 20, "路径不重复")
        leftovers = [n for n in os.listdir(env.data) if n.startswith(".recent_workspaces.")]
        eq(leftovers, [], "原子写的临时文件不残留")
        with open(os.path.join(env.data, "last_workspace.txt"), encoding="utf-8") as f:
            eq(f.read(), os.path.normpath(paths[10]), "txt 与 json 同步")
    finally:
        env.close()


def test_discovery_roots_fall_back_to_recent_parent():
    env = _Env(root_env=False)
    fake = _FakeDe()
    try:
        ads_ops._remember_workspace(env.demo)
        with patch.object(ads_ops, "_de", return_value=fake):
            r = ads_ops.list_workspaces({})
        ok(any(_norm(p) == _norm(env.root_b) for p in r["roots"]),
           f"没有 env 时用最近工作区的父目录: {r['roots']}")
        ok(any(_norm(w["path"]) == _norm(env.demo) for w in r["workspaces"]))
        eq(_norm(r["recent"][0]["path"]), _norm(env.demo), "recent 字段带最近记录")

        # recent 里失效的路径也要如实报 valid=false（source=recent）
        stale = os.path.join(env.tmp, "gone_ws")
        ads_ops._remember_workspace(stale)
        with patch.object(ads_ops, "_de", return_value=fake):
            r2 = ads_ops.list_workspaces({"roots": [env.root_a]})
        hit = [w for w in r2["workspaces"] if _norm(w["path"]) == _norm(stale)]
        eq(len(hit), 1, "失效的最近记录不静默丢弃")
        eq(hit[0]["valid"], False)
        eq(hit[0]["source"], "recent")
    finally:
        env.close()


if __name__ == "__main__":
    raise SystemExit(run(globals()))
