"""旧布局迁移：搬过去但不覆盖，失败保留原文件。

改造前所有用户数据都堆在代码目录旁边（``config.ini`` / ``projects.json`` /
``design_jobs/``）。搬到 ``%LOCALAPPDATA%\\ADSAgent`` 时必须满足：

1. **非破坏性** —— 源文件一个都不删、一个都不改。迁移失败了原来的还能用；
2. **不覆盖新位置的有效数据** —— 用户已经在数据目录里配好的东西不能被
   旧的、可能过期的内容盖掉（典型场景：先装了新版配好 API，又跑了一次旧目录）；
3. **无效数据不算有效** —— 空的、坏的、缺字段的文件应当被正确地替换，
   而不是因为"文件存在"就跳过。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ok, run  # noqa: E402

add_path("backend")

VALID_CONFIG = "\n".join([
    "; 一份旧的有效配置",
    "[llm]",
    "base_url = https://api.deepseek.com",
    "model = deepseek-chat",
    "api_key = LOCAL_TEST_KEY_NOT_REAL",
    "",
    "[ads]",
    "token = LOCAL_TEST_TOKEN_NOT_REAL",
    "",
])


def _projects(name="旧会话"):
    return {"active": name, "projects": {name: {"entries": [], "history": []}}}


def _job(job_id="job_old_1"):
    return {"job_id": job_id, "state": "done", "spec": {"metrics": []}}


class Workspace:
    """造一个"程序目录里放着旧数据 + 独立的数据目录"的现场。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="迁移测试 ")
        base = os.path.join(self._tmp.name, "旧的程序目录 名称")
        os.makedirs(base)
        self.app = base
        self.data = os.path.join(self._tmp.name, "新的数据目录 ADSAgent")
        os.makedirs(self.data)
        self._old_env = os.environ.get("ADS_AGENT_DATA_DIR")
        self._old_app = os.environ.get("ADS_AGENT_APP_DIR")
        os.environ["ADS_AGENT_DATA_DIR"] = self.data
        os.environ["ADS_AGENT_APP_DIR"] = self.app
        return self

    def __exit__(self, *exc):
        for key, value in (("ADS_AGENT_DATA_DIR", self._old_env),
                           ("ADS_AGENT_APP_DIR", self._old_app)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            self._tmp.cleanup()
        except OSError:
            pass
        return False

    def seed_legacy(self, **files):
        for rel, content in files.items():
            path = os.path.join(self.app, rel)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            yield path

    def read(self, rel: str) -> str:
        with open(os.path.join(self.data, rel), encoding="utf-8") as f:
            return f.read()


def _fresh():
    sys.modules.pop("paths", None)
    sys.modules.pop("ads_agent_shared_paths", None)
    import paths

    paths.set_app_root(None)
    return paths


def test_migration_copies_legacy_data():
    with Workspace() as ws:
        list(ws.seed_legacy(
            **{
                "config.ini": VALID_CONFIG,
                "projects.json": _projects(),
                "design_jobs/job_a.json": _job("job_a"),
            }
        ))
        paths = _fresh()
        report = paths.migrate_from_legacy()

        ok(os.path.isfile(os.path.join(ws.data, "config.ini")), "配置应被迁移")
        ok(os.path.isfile(os.path.join(ws.data, "projects.json")), "会话应被迁移")
        ok(os.path.isfile(os.path.join(ws.data, "design_jobs", "job_a.json")),
           "设计任务应被迁移")
        contains(report["copied"], "config.ini")
        contains(report["copied"], "projects.json")

        # 源文件必须原样还在 —— "失败保留原文件"的前提是先不破坏
        ok(os.path.isfile(os.path.join(ws.app, "config.ini")), "旧配置文件必须还在")
        eq(open(os.path.join(ws.app, "config.ini"), encoding="utf-8").read(),
           VALID_CONFIG, "旧配置文件不能被改写")


def test_migration_never_overwrites_valid_existing_data():
    with Workspace() as ws:
        list(ws.seed_legacy(
            **{"config.ini": VALID_CONFIG, "projects.json": _projects("旧会话")}
        ))
        # 数据目录里已经有一份用户配好的新内容
        mine = dict(_projects("新的会话"), extra="用户在数据目录里做的修改")
        with open(os.path.join(ws.data, "projects.json"), "w", encoding="utf-8") as f:
            json.dump(mine, f, ensure_ascii=False)
        new_cfg = VALID_CONFIG.replace("deepseek-chat", "用户选的新模型")
        with open(os.path.join(ws.data, "config.ini"), "w", encoding="utf-8") as f:
            f.write(new_cfg)

        paths = _fresh()
        report = paths.migrate_from_legacy()

        eq(json.loads(ws.read("projects.json"))["active"], "新的会话",
           "数据目录里的有效会话不能被旧内容覆盖")
        ok(json.loads(ws.read("projects.json")).get("extra"), "用户的新修改要保住")
        contains(ws.read("config.ini"), "用户选的新模型", "已有的配置不能被旧配置覆盖")
        ok("projects.json" in report["kept_existing"])
        ok("config.ini" in report["kept_existing"])


def test_invalid_existing_data_is_replaced():
    """目标位置的"文件"存在但内容是坏的 —— 应当被正确的旧数据替换。"""
    with Workspace() as ws:
        list(ws.seed_legacy(**{"projects.json": _projects("有效旧会话")}))
        with open(os.path.join(ws.data, "projects.json"), "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        paths = _fresh()
        report = paths.migrate_from_legacy()
        eq(json.loads(ws.read("projects.json"))["active"], "有效旧会话",
           "空/坏的目标文件不应挡住迁移")
        ok("projects.json" in report["copied"])

        # 空会话（没有任何项目）同样不算有效：{projects: {}} 会被视为空
        with open(os.path.join(ws.data, "projects.json"), "w", encoding="utf-8") as f:
            json.dump({"projects": {}}, f)
        paths = _fresh()
        paths.migrate_from_legacy()
        eq(json.loads(ws.read("projects.json"))["active"], "有效旧会话")


def test_migration_is_idempotent():
    with Workspace() as ws:
        list(ws.seed_legacy(**{"projects.json": _projects(), "config.ini": VALID_CONFIG}))
        paths = _fresh()
        paths.migrate_from_legacy()
        after_first = ws.read("projects.json")
        second = paths.migrate_from_legacy()
        eq(ws.read("projects.json"), after_first, "第二次迁移不应产生任何变化")
        eq(second["copied"], [], "第二次不该再复制")


def test_migration_failure_keeps_source_intact():
    """目标不可写 —— 源文件必须完好，且要把失败说清楚。"""
    with Workspace() as ws:
        list(ws.seed_legacy(**{"config.ini": VALID_CONFIG}))
        paths = _fresh()
        real = paths._write_if_absent

        def boom(path, body, **kwargs):
            if path.endswith("config.ini"):
                raise OSError("模拟：目标不可写")
            return real(path, body)

        paths._write_if_absent = boom
        try:
            report = paths.migrate_from_legacy()
        finally:
            paths._write_if_absent = real

        ok(len(report["failed"]) == 1, "失败项应被如实记录")
        eq(report["failed"][0]["name"], "config.ini")
        contains(report["failed"][0]["error"], "模拟：目标不可写")
        eq(open(os.path.join(ws.app, "config.ini"), encoding="utf-8").read(),
           VALID_CONFIG, "失败时原文件必须原封不动")


def test_no_migration_when_roots_are_the_same():
    with Workspace() as ws:
        os.environ["ADS_AGENT_DATA_DIR"] = ws.app
        try:
            paths = _fresh()
            report = paths.migrate_from_legacy()
            ok(report["skipped"], "程序目录==数据目录时不应迁移")
            eq(report["copied"], [])
        finally:
            os.environ["ADS_AGENT_DATA_DIR"] = ws.data


def test_migration_preserves_chinese_and_spaces():
    with Workspace() as ws:
        list(ws.seed_legacy(**{"projects.json": _projects("带中文的项目 名称")}))
        paths = _fresh()
        paths.migrate_from_legacy()
        data = json.loads(ws.read("projects.json"))
        eq(data["active"], "带中文的项目 名称", "含中文与空格的内容必须无损")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "旧目录迁移"))
