"""Capacity provenance and isolated per-model context settings."""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, raises, run
add_path("backend")
import model_context as M
import config


def test_official_capacity_is_bound_to_exact_endpoint_and_model():
    cfg = {"llm_model": "deepseek-flash", "llm_base_url": "https://api.deepseek.com"}
    eq(M.capacity(cfg)["max"], 1_000_000)
    eq(M.capacity({**cfg, "llm_base_url": "https://gateway.example"})["max"], None)
    eq(M.capacity({**cfg, "llm_model": "deepseek-flash-other"})["max"], None)
    eq(M.capacity({"llm_model": "step-3.7-flash", "llm_base_url": "https://api.stepfun.com/v1"})["max"], 256_000)


def test_override_and_profile_endpoint_take_precedence():
    cfg = {"llm_model": "a", "llm_base_url": "https://api.deepseek.com",
           "llm_profiles": {"a": {"base_url": "https://local.example", "context_window_tokens": 8192}}}
    eq(M.capacity(cfg)["max"], 8192)
    eq(M.capacity(cfg)["source"], "configured")
    eq(M.capacity({**cfg, "llm_model": "b"})["max"], None)


def test_usage_uses_latest_request_not_accumulated_billing():
    cfg = {"llm_model": "deepseek-flash", "llm_base_url": "https://api.deepseek.com"}
    messages = [{"role": "user", "content": "中文"}]
    first = M.usage(cfg, messages, reported={"prompt_tokens": 100, "completion_tokens": 20})
    second = M.usage(cfg, messages, reported={"prompt_tokens": 150, "completion_tokens": 10})
    eq(first["used"], 120)
    eq(second["used"], 160)
    eq(second["estimated"], False)
    for bad in ({}, {"prompt_tokens": True, "completion_tokens": 10},
                {"prompt_tokens": 10, "completion_tokens": -1}):
        ok(M.usage(cfg, messages, reported=bad)["estimated"])
    ok(M.estimate_request(messages, [{"function": {"description": "x" * 1000}}]) > M.estimate_request(messages))


def test_per_model_limits_survive_group_saves_and_do_not_copy_to_new_model():
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ADS_AGENT_CONFIG": os.path.join(folder, "config.ini")}):
        config.update_llm_settings(base_url="https://custom.example", model="a", models=["a", "b"],
            provider_models=["a", "b"], context_model="a", context_window_tokens=8192)
        config.update_llm_settings(model="b", provider_model="a", provider_models=["a", "b"],
            context_model="b", context_window_tokens=32768)
        eq(config.model_profile("a")["context_window_tokens"], 8192)
        eq(config.model_profile("b")["context_window_tokens"], 32768)
        result = config.update_llm_settings(model="a", provider_model="a", provider_models=["a", "b", "c"])
        eq(result["model_contexts"]["a"]["max"], 8192)
        eq(result["model_contexts"]["b"]["max"], 32768)
        eq(result["model_contexts"]["c"]["max"], None)
        config.update_llm_settings(model="a", context_window_tokens=0)
        eq(config.model_profile("a")["context_window_tokens"], 0)
        eq(config.model_profile("b")["context_window_tokens"], 32768)
        raises(ValueError, lambda: config.update_llm_settings(model="a", context_window_tokens=True))
        raises(ValueError, lambda: config.update_llm_settings(model="a", context_window_tokens=-1))


if __name__ == "__main__":
    raise SystemExit(run(globals()))
