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


#: normalize_reported() 的固定 7 键（docs/context-usage-spec.md §1.1）。
REPORTED_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens",
                 "cache_hit_tokens", "cache_miss_tokens", "cached_tokens",
                 "reasoning_tokens")


def _first_valid(*values):
    """按顺序返回第一个"有效值"（int、非 bool、>= 0），否则 None。"""
    for value in values:
        if valid_tokens(value):
            return value
    return None


def _details(reported, key):
    """取嵌套 details dict；不是 dict（含 None）时返回空 dict。"""
    value = reported.get(key) if isinstance(reported, dict) else None
    return value if isinstance(value, dict) else {}


def _hit_rate(hit, miss):
    """命中率 = hit / (hit + miss)；分母缺失或为 0 → None（不得用 0 冒充）。"""
    if valid_tokens(hit) and valid_tokens(miss):
        denominator = hit + miss
        if denominator > 0:
            return hit / denominator
    return None


def normalize_reported(reported):
    """把 provider 原始 usage 规范成固定 7 键 dict；**绝不抛异常**。

    兼容三种形状：DeepSeek 的 prompt_cache_hit_tokens / prompt_cache_miss_tokens、
    OpenAI/GLM/StepFun 的 prompt_tokens_details.cached_tokens、以及通用
    cached_tokens / cache_hit_tokens / cache_miss_tokens / reasoning_tokens。
    无效值（bool、负数、字符串、None、非 dict）一律 None；provider 上报的
    hit + miss 与 prompt 不一致时**如实保留**，不做修正。
    """
    result = dict.fromkeys(REPORTED_KEYS)
    if not isinstance(reported, dict):
        return result
    try:
        prompt = _first_valid(reported.get("prompt_tokens"))
        completion = _first_valid(reported.get("completion_tokens"))
        total = _first_valid(reported.get("total_tokens"))
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        prompt_details = _details(reported, "prompt_tokens_details")
        completion_details = _details(reported, "completion_tokens_details")
        hit = _first_valid(
            reported.get("prompt_cache_hit_tokens"),
            reported.get("cache_hit_tokens"),
            reported.get("cached_tokens"),
            prompt_details.get("cached_tokens"),
            prompt_details.get("cache_hit_tokens"),
        )
        miss = _first_valid(reported.get("prompt_cache_miss_tokens"),
                            reported.get("cache_miss_tokens"))
        if miss is None and prompt is not None and hit is not None and prompt - hit >= 0:
            miss = prompt - hit
        reasoning = _first_valid(completion_details.get("reasoning_tokens"),
                                reported.get("reasoning_tokens"))
        result.update(prompt_tokens=prompt, completion_tokens=completion,
                      total_tokens=total, cache_hit_tokens=hit,
                      cache_miss_tokens=miss, cached_tokens=hit,
                      reasoning_tokens=reasoning)
    except Exception:  # noqa: BLE001 —— 契约要求绝不向调用方抛异常，脏数据照实降级
        pass
    return result


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
    """上下文用量快照。

    老字段（model/max/unit/source/source_url/connection/used/estimated/basis/
    prompt_tokens/completion_tokens）语义与取值口径**一律不变**；新增
    total_tokens / cache_hit_tokens / cache_miss_tokens / reasoning_tokens /
    cache_hit_rate，走估算路径时这 5 个全部 None（见 spec §1.2）。
    """
    result = capacity(cfg)
    normalized = normalize_reported(reported)
    prompt = normalized["prompt_tokens"]
    completion = normalized["completion_tokens"]
    measured = valid_tokens(prompt) and valid_tokens(completion)
    result.update(used=prompt + completion if measured else
                  estimate_request(messages, tools) + math.ceil(generated_chars / 2),
                  estimated=not measured,
                  basis="provider_usage" if measured else "token_estimate",
                  prompt_tokens=prompt if valid_tokens(prompt) else None,
                  completion_tokens=completion if valid_tokens(completion) else None)
    if measured:
        result.update(total_tokens=normalized["total_tokens"],
                      cache_hit_tokens=normalized["cache_hit_tokens"],
                      cache_miss_tokens=normalized["cache_miss_tokens"],
                      reasoning_tokens=normalized["reasoning_tokens"],
                      cache_hit_rate=_hit_rate(normalized["cache_hit_tokens"],
                                               normalized["cache_miss_tokens"]))
    else:
        result.update(total_tokens=None, cache_hit_tokens=None,
                      cache_miss_tokens=None, reasoning_tokens=None,
                      cache_hit_rate=None)
    return result


#: add_usage() 里按整数求和的 token 字段（spec §1.3）。
SUM_KEYS = ("prompt_tokens", "completion_tokens", "cache_hit_tokens",
            "cache_miss_tokens", "reasoning_tokens", "total_tokens")


def _count(value):
    """累加器里的计数：无效值（None/bool/负数/字符串）按 0 处理。"""
    return value if valid_tokens(value) else 0


def add_usage(total, reported):
    """把一次 provider 原始 usage 累加进会话累计；**纯函数，不修改入参**。

    `total` 可以是 None / {} / 上一次 add_usage 的返回。返回键固定：
    requests / measured_requests / estimated_requests / 6 个 token 累计 /
    cache_hit_rate（由累加后的 hit/(hit+miss) 派生，分母为 0 → None）。
    """
    current = total if isinstance(total, dict) else {}
    normalized = normalize_reported(reported)
    measured = (valid_tokens(normalized["prompt_tokens"])
                and valid_tokens(normalized["completion_tokens"]))
    requests = _count(current.get("requests")) + 1
    measured_requests = _count(current.get("measured_requests")) + (1 if measured else 0)
    result = {
        "requests": requests,
        "measured_requests": measured_requests,
        "estimated_requests": requests - measured_requests,
    }
    for key in SUM_KEYS:
        result[key] = _count(current.get(key)) + _count(normalized[key])
    result["cache_hit_rate"] = _hit_rate(result["cache_hit_tokens"],
                                         result["cache_miss_tokens"])
    return result
