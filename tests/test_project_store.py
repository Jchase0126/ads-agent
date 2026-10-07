"""会话文件写入中断时，主文件和上一份备份仍可恢复。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, eq, ok, raises, run  # noqa: E402

add_path("addon", "ads_agent")
import project_store  # noqa: E402


def _data(name):
    return {"active": name, "projects": {name: {"entries": [], "history": []}}}


def test_atomic_save_and_backup_recovery():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "projects.json")
        project_store.save(path, _data("old"))
        project_store.save(path, _data("new"))
        eq(project_store.load(path), (_data("new"), False))
        eq(project_store.load(path + ".bak"), (_data("old"), False))
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("{broken")
        eq(project_store.load(path), (_data("old"), True),
           "主文件损坏时应恢复上一份有效会话")


def test_failed_replace_preserves_old_session():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "projects.json")
        project_store.save(path, _data("old"))
        original = project_store._replace

        def fail_main(src, dst):
            if dst == path:
                raise PermissionError("locked")
            return original(src, dst)

        project_store._replace = fail_main
        try:
            raises(PermissionError, lambda: project_store.save(path, _data("new")))
        finally:
            project_store._replace = original
        eq(project_store.load(path), (_data("old"), False))
        ok(not any(name.endswith(".tmp") for name in os.listdir(directory)),
           "失败后不应残留临时文件")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
