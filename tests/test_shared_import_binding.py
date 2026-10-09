"""Shared imports retain the trusted ADS target even if the user switches Workspace."""
import os
import threading
from unittest.mock import Mock, patch

from _harness import add_path, eq, ok, raises, run

add_path("backend")
import model_tools
import model_orchestration


def _op():
    return {"package_id": "pkg_local", "state": model_orchestration.OP_SUCCEEDED,
            "result": {"imported": True}}


def test_fixed_target_and_ignored_caller_workspace():
    target = os.path.abspath("trusted_wrk")
    orchestrator = Mock()
    orchestrator.run_sync.return_value = _op()
    with patch.object(model_tools, "current_workspace", side_effect=[target, target]) as current, \
            patch.object(model_tools.shared_models, "copy_to_workspace",
                         return_value={"package_id": "pkg_local"}) as copy, \
            patch.object(model_tools.model_orchestration, "get_orchestrator",
                         return_value=orchestrator):
        result = model_tools.import_shared_model_package(
            {}, {"package_id": "pkg_shared", "workspace": "untrusted_wrk"})
    eq(current.call_count, 2, "Only capture and recheck active Workspace")
    eq(copy.call_args.args[1], target, "Copy uses trusted target")
    eq(orchestrator.run_sync.call_args.args[1], target, "Pipeline keeps same target")
    eq(result["shared_source"]["copied_to_workspace"], target)


def test_workspace_switch_stops_import():
    with patch.object(model_tools, "current_workspace", side_effect=["first_wrk", "second_wrk"]), \
            patch.object(model_tools.shared_models, "copy_to_workspace",
                         return_value={"package_id": "pkg_local"}), \
            patch.object(model_tools.model_orchestration, "get_orchestrator") as orchestrator:
        raises(model_tools.ModelToolError, lambda: model_tools.import_shared_model_package(
            {}, {"package_id": "pkg_shared"}))
        orchestrator.assert_not_called()


def test_cancel_before_copy_has_no_side_effect():
    cancelled = threading.Event()
    cancelled.set()
    with patch.object(model_tools, "current_workspace") as current, \
            patch.object(model_tools.shared_models, "copy_to_workspace") as copy:
        result = model_tools.import_shared_model_package(
            {}, {"package_id": "pkg_shared"}, cancelled)
        current.assert_not_called()
        copy.assert_not_called()
    ok(result["cancelled"])
    eq(result["shared_source"]["copied"], False)


def test_cancel_during_copy_preserves_archive_without_import():
    cancelled = threading.Event()

    def copy(*args):
        cancelled.set()
        return {"package_id": "pkg_local"}

    with patch.object(model_tools, "current_workspace", return_value="first_wrk"), \
            patch.object(model_tools.shared_models, "copy_to_workspace", side_effect=copy), \
            patch.object(model_tools.model_orchestration, "get_orchestrator") as orchestrator:
        result = model_tools.import_shared_model_package(
            {}, {"package_id": "pkg_shared"}, cancelled)
        orchestrator.assert_not_called()
    ok(result["cancelled"])
    eq(result["shared_source"]["copied_to_workspace"], "first_wrk")
    eq(result["shared_source"]["copied"], True)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
