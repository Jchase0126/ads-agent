"""Model capacity and explicitly approximate token accounting, without network I/O."""
import json
import math
from urllib.parse import urlsplit

# Verified against official provider docs on 2026-10-09. Exact host and ID only;
# gateways and renamed/local models require an explicit per-model override.
OFFICIAL = {
    ("api.deepseek.com", "deepseek-flash"): (1_000_000, "https://api-docs.deepseek.com/quick_start/pricing/"),
    ("api.deepseek.com", "deepseek-v4-pro"): (1_000_000, "https://api-docs.deepseek.com/quick_start/pricing/"),
    ("api.stepfun.com", "step-3.7-flash"): (256_000, "https://platform.stepfun.ai/docs/en/guides/models/step-3.7-flash.md"),
    ("api.stepfun.ai", "step-3.7-flash"): (256_000, "https://platform.stepfun.ai/docs/en/guides/models/step-3.7-flash.md"),
    ("api.stepfun.com", "step-5-preview"): (1_000_000, "https://platform.stepfun.ai/docs/en/guides/models/step-5-preview.md"),
    ("api.stepfun.ai", "step-5-preview"): (1_000_000, "https://platform.stepfun.ai/docs/en/guides/models/step-5-preview.md"),
}


def valid_tokens(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def capacity(cfg, model=None):
    model = model or cfg.get("llm_model") or ""
    profile = (cfg.get("llm_profiles") or {}).get(model) or {}
    base = profile.get("base_url", cfg.get("llm_base_url", ""))
    result = {"model": model, "max": None, "unit": "tokens", "source": "unknown", "source_url": "",
              "connection": base.strip().rstrip("/")}
    override = profile.get("context_window_tokens")
    if valid_tokens(override) and override > 0:
        result.update(max=override, source="configured")
    else:
        known = OFFICIAL.get((urlsplit(base).hostname, model))
        if known:
            result.update(max=known[0], source="official", source_url=known[1])
    return result


def capacities(cfg):
    return {name: capacity(cfg, name) for name in cfg.get("llm_models", [])}


def estimate_text(text):
    # Heuristic only: mixed Chinese/code varies by tokenizer. Never report as
    # measured usage or use it to enforce a hard model request limit.
    return math.ceil(len(str(text or "").encode("utf-8")) / 3)


def estimate_request(messages, tools=None):
    total = sum(estimate_text(json.dumps(m, ensure_ascii=False, separators=(",", ":")))
                + 8 for m in messages)
    if tools:
        total += estimate_text(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return total


def usage(cfg, messages, tools=None, reported=None, generated_chars=0):
    result = capacity(cfg)
    prompt = (reported or {}).get("prompt_tokens")
    completion = (reported or {}).get("completion_tokens")
    measured = valid_tokens(prompt) and valid_tokens(completion)
    result.update(used=prompt + completion if measured else
                  estimate_request(messages, tools) + math.ceil(generated_chars / 2),
                  estimated=not measured,
                  basis="provider_usage" if measured else "token_estimate",
                  prompt_tokens=prompt if valid_tokens(prompt) else None,
                  completion_tokens=completion if valid_tokens(completion) else None)
    return result
