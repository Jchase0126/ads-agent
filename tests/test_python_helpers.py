"""Regression for persistent scripts shadowing ADS helper names."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, run

add_path("addon", "ads_agent")
import ads_ops
import toolserver
from unittest.mock import patch


def test_helpers_recover_after_success_and_script_error():
    previous = ads_ops._PY_ENV
    ads_ops._PY_ENV = None
    try:
        result = ads_ops.run_python({"code": "cells = ['table result']; ws = 7; saved_value = 42"})
        eq(result["ok"], True)
        result = ads_ops.run_python({"code": "print(callable(cells), callable(ws), saved_value)"})
        eq(result["stdout"].strip(), "True True 42")
        result = ads_ops.run_python({"code": "pins = []; params = None; raise ValueError('probe')"})
        eq(result["ok"], False)
        result = ads_ops.run_python({"code": "print(callable(pins), callable(params), saved_value)"})
        eq(result["stdout"].strip(), "True True 42")
    finally:
        ads_ops._PY_ENV = previous


def test_user_imports_and_helpers_share_current_output_buffer():
    previous = ads_ops._PY_ENV
    ads_ops._PY_ENV = None
    try:
        ads_ops.run_python({"code": "import fractions; data = {'x': 3}"})
        result = ads_ops.run_python({"code": "print(fractions.Fraction(data['x'], 2)); print(sig(cells))"})
        eq(result["ok"], True)
        ok("3/2" in result["stdout"])
        ok("cells(" in result["stdout"])
    finally:
        ads_ops._PY_ENV = previous


def test_reload_allows_starting_and_queued_jobs_but_blocks_deferred_job():
    previous_jobs = dict(toolserver._active_jobs)
    previous_mtime = getattr(ads_ops, "_LOADED_MTIME", None)
    previous_env = ads_ops._PY_ENV
    ads_ops._LOADED_MTIME = "old-version"
    ads_ops._PY_ENV = None
    try:
        toolserver._active_jobs.clear()
        toolserver._active_jobs.update({1: {"queued": False}, 2: {"queued": True}})
        with patch("importlib.reload", side_effect=lambda module: module) as reload_module, \
                patch.object(toolserver, "_log"):
            toolserver._get_ads_ops(starting_job_id=1)
            ok(reload_module.called, "the starting request must not block reload")
            ads_ops._LOADED_MTIME = "old-version"
            toolserver._active_jobs[3] = {"queued": False, "deferred": True}
            reload_module.reset_mock()
            toolserver._get_ads_ops(starting_job_id=1)
            ok(not reload_module.called, "a deferred job must keep its module version")
    finally:
        toolserver._active_jobs.clear()
        toolserver._active_jobs.update(previous_jobs)
        ads_ops._PY_ENV = previous_env
        if previous_mtime is None:
            del ads_ops._LOADED_MTIME
        else:
            ads_ops._LOADED_MTIME = previous_mtime


if __name__ == "__main__":
    raise SystemExit(run(globals()))
