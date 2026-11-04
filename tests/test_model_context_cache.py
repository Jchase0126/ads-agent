"""缓存/推理 tokens 规范化与用量累加 —— docs/context-usage-spec.md §1。

全部纯函数，不联网、不依赖 ADS 实机。覆盖三种 provider 形状、脏数据降级、
usage() 向后兼容、add_usage() 的不可变/幂等/如实保留不变量。
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _harness import add_path, eq, ok, run

add_path("backend")
import model_context as M

# 老字段：面板/agent 现在就在读，语义必须原样保留
LEGACY_USAGE_KEYS = ("model", "max", "unit", "source", "source_url", "connection",
                     "used", "estimated", "basis", "prompt_tokens", "completion_tokens")
NEW_USAGE_KEYS = ("total_tokens", "cache_hit_tokens", "cache_miss_tokens",
                  "reasoning_tokens", "cache_hit_rate")
SUM_KEYS = ("prompt_tokens", "completion_tokens", "cache_hit_tokens",
            "cache_miss_tokens", "reasoning_tokens", "total_tokens")
CFG = {"llm_model": "deepseek-flash", "llm_base_url": "https://api.deepseek.com"}
MESSAGES = [{"role": "user", "content": "你好"}]


def test_normalize_always_returns_seven_fixed_keys_and_never_raises():
    d = M.normalize_reported(None)
    eq(sorted(d), sorted(M.REPORTED_KEYS), "7 个固定键")
    eq(set(d.values()), {None}, "缺失输入全部 None")
    for dirty in ({}, [], "prompt_tokens=100", 42, object(), True, 3.5):
        out = M.normalize_reported(dirty)  # 契约：绝不抛异常
        eq(sorted(out), sorted(M.REPORTED_KEYS), f"脏输入 {dirty!r} 仍返回 7 键")
        eq(set(out.values()), {None}, f"脏输入 {dirty!r} 全部 None")


def test_deepseek_shape():
    d = M.normalize_reported({"prompt_tokens": 1000, "completion_tokens": 200,
                              "total_tokens": 1300,
                              "prompt_cache_hit_tokens": 600,
                              "prompt_cache_miss_tokens": 400})
    eq(d["prompt_tokens"], 1000)
    eq(d["completion_tokens"], 200)
    eq(d["total_tokens"], 1300)
    eq(d["cache_hit_tokens"], 600)
    eq(d["cache_miss_tokens"], 400)
    eq(d["cached_tokens"], 600, "cached_tokens 与 hit 同值镜像")
    eq(d["reasoning_tokens"], None, "DeepSeek 不报 reasoning")


def test_openai_glm_stepfun_shape():
    d = M.normalize_reported({"prompt_tokens": 800, "completion_tokens": 100,
                              "total_tokens": 900,
                              "prompt_tokens_details": {"cached_tokens": 500},
                              "completion_tokens_details": {"reasoning_tokens": 40}})
    eq(d["cache_hit_tokens"], 500, "prompt_tokens_details.cached_tokens")
    eq(d["cache_miss_tokens"], 300, "hit 有效且 prompt-hit>=0 时推导未命中")
    eq(d["cached_tokens"], 500)
    eq(d["reasoning_tokens"], 40, "completion_tokens_details.reasoning_tokens")
    eq(d["total_tokens"], 900)


def test_generic_alias_shape():
    d = M.normalize_reported({"prompt_tokens": 300, "completion_tokens": 30,
                              "cache_hit_tokens": 120, "cache_miss_tokens": 180,
                              "reasoning_tokens": 12})
    eq(d["cache_hit_tokens"], 120)
    eq(d["cache_miss_tokens"], 180)
    eq(d["cached_tokens"], 120)
    eq(d["reasoning_tokens"], 12, "顶层 reasoning_tokens 别名")
    eq(d["total_tokens"], 330, "total 缺失时用 prompt+completion")


def test_alias_priority_order():
    d = M.normalize_reported({"prompt_tokens": 100, "completion_tokens": 5,
                              "prompt_cache_hit_tokens": 40,   # 1 优先级
                              "cache_hit_tokens": 30,          # 2
                              "cached_tokens": 20,             # 3
                              "prompt_tokens_details": {"cached_tokens": 10}})
    eq(d["cache_hit_tokens"], 40, "prompt_cache_hit_tokens 优先")
    eq(d["cache_miss_tokens"], 60, "未命中由 prompt-hit 推导")
    d = M.normalize_reported({"prompt_tokens": 100, "completion_tokens": 5,
                              "cached_tokens": 20,
                              "prompt_tokens_details": {"cached_tokens": 10}})
    eq(d["cache_hit_tokens"], 20, "顶层 cached_tokens 优先于 details")
    d = M.normalize_reported({"prompt_tokens": 100, "completion_tokens": 5,
                              "prompt_tokens_details": {"cache_hit_tokens": 7}})
    eq(d["cache_hit_tokens"], 7, "prompt_tokens_details.cache_hit_tokens 兜底")


def test_dirty_values_degrade_to_none():
    d = M.normalize_reported({"prompt_tokens": True, "completion_tokens": -1,
                              "total_tokens": "999",
                              "prompt_cache_hit_tokens": False,
                              "cache_hit_tokens": "600",
                              "prompt_cache_miss_tokens": -5,
                              "prompt_tokens_details": "not-a-dict",
                              "completion_tokens_details": {"reasoning_tokens": True}})
    eq(set(d.values()), {None}, "bool/负数/字符串/非 dict details 全部降级 None")
    d = M.normalize_reported({"prompt_tokens": 10, "completion_tokens": 2,
                              "cache_hit_tokens": 20})
    eq(d["cache_hit_tokens"], 20, "hit 超过 prompt 也如实保留")
    eq(d["cache_miss_tokens"], None, "差为负时不推导未命中（不修正、不造 0）")


def test_hit_miss_inconsistent_with_prompt_is_preserved():
    d = M.normalize_reported({"prompt_tokens": 100, "completion_tokens": 5,
                              "prompt_cache_hit_tokens": 500,
                              "prompt_cache_miss_tokens": 30})
    eq(d["cache_hit_tokens"], 500, "hit+miss 与 prompt 不一致时不修正")
    eq(d["cache_miss_tokens"], 30)
    eq(d["total_tokens"], 105)


def test_usage_keeps_legacy_fields_and_semantics():
    estimated = M.usage(CFG, MESSAGES)
    for key in LEGACY_USAGE_KEYS:
        ok(key in estimated, f"老字段 {key} 仍在")
    eq(estimated["estimated"], True)
    eq(estimated["basis"], "token_estimate")
    eq(estimated["prompt_tokens"], None)
    eq(estimated["completion_tokens"], None)
    eq(estimated["used"], M.estimate_request(MESSAGES))
    for key in NEW_USAGE_KEYS:
        eq(estimated[key], None, f"估算路径 {key} 必须 None")

    measured = M.usage(CFG, MESSAGES, reported={
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40})
    eq(measured["used"], 120, "used 仍是最近一次请求的输入+输出")
    eq(measured["estimated"], False)
    eq(measured["basis"], "provider_usage")
    eq(measured["prompt_tokens"], 100)
    eq(measured["completion_tokens"], 20)
    eq(measured["total_tokens"], 120)
    eq(measured["cache_hit_tokens"], 60)
    eq(measured["cache_miss_tokens"], 40)
    eq(measured["reasoning_tokens"], None)
    eq(measured["cache_hit_rate"], 0.6)

    # 脏 reported 不得改变老行为：仍走估算且不抛异常
    for bad in ({}, {"prompt_tokens": True, "completion_tokens": 10},
                {"prompt_tokens": 10, "completion_tokens": -1}, "junk", [1]):
        snap = M.usage(CFG, MESSAGES, reported=bad)
        eq(snap["estimated"], True, f"脏 reported {bad!r} 仍 estimated")
        eq(snap["used"], M.estimate_request(MESSAGES))


def test_usage_cache_hit_rate_denominator_zero_is_none():
    snap = M.usage(CFG, MESSAGES, reported={"prompt_tokens": 100, "completion_tokens": 10,
                                            "cache_hit_tokens": 0, "cache_miss_tokens": 0})
    eq(snap["cache_hit_rate"], None, "分母 0 → None（不用 0.0 冒充）")
    snap = M.usage(CFG, MESSAGES, reported={"prompt_tokens": 100, "completion_tokens": 10,
                                            "cache_hit_tokens": 0})
    eq(snap["cache_hit_tokens"], 0)
    eq(snap["cache_miss_tokens"], 100, "hit=0 且 prompt 有效 → 未命中推导为全部输入")
    eq(snap["cache_hit_rate"], 0.0)


def test_add_usage_is_pure_and_does_not_mutate_inputs():
    total = {"requests": 2, "measured_requests": 2, "estimated_requests": 0,
             "prompt_tokens": 200, "completion_tokens": 40,
             "cache_hit_tokens": 120, "cache_miss_tokens": 80,
             "reasoning_tokens": 0, "total_tokens": 240, "cache_hit_rate": 0.6}
    reported = {"prompt_tokens": 100, "completion_tokens": 20,
                "prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 70}
    total_before, reported_before = copy.deepcopy(total), copy.deepcopy(reported)
    result = M.add_usage(total, reported)
    eq(total, total_before, "入参 total 不得被修改")
    eq(reported, reported_before, "入参 reported 不得被修改")
    ok(result is not total, "必须返回新 dict")
    eq(result["requests"], 3)
    eq(result["prompt_tokens"], 300)
    eq(result["completion_tokens"], 60)
    eq(result["cache_hit_tokens"], 150)
    eq(result["cache_miss_tokens"], 150)
    eq(result["total_tokens"], 360, "本次 total 缺失时按 prompt+completion 累计")
    eq(result["cache_hit_rate"], 0.5, "命中率由累加后的 hit/(hit+miss) 派生")


def test_add_usage_idempotent_and_fixed_keys():
    frames = ["prompt_tokens", "completion_tokens", "cache_hit_tokens",
              "cache_miss_tokens", "reasoning_tokens", "total_tokens"]
    expected_keys = ("requests", "measured_requests", "estimated_requests",
                     *frames, "cache_hit_rate")
    seed = None
    reported = {"prompt_tokens": 50, "completion_tokens": 10,
                "prompt_cache_hit_tokens": 50}
    one = M.add_usage(seed, reported)
    two = M.add_usage({}, reported)  # None / {} 等价于空累计
    eq(sorted(one), sorted(expected_keys), "键固定且不多不少")
    eq(one, two, "同样的入参得到同样的结果（幂等，不累积隐藏状态）")
    eq(M.add_usage(one, reported), M.add_usage(one, reported), "重复调用结果一致")


def test_add_usage_counts_requests_and_estimated_requests():
    acc = M.add_usage(None, {"prompt_tokens": 100, "completion_tokens": 20})
    eq((acc["requests"], acc["measured_requests"], acc["estimated_requests"]), (1, 1, 0))
    acc = M.add_usage(acc, None)  # 服务没返回 usage
    eq((acc["requests"], acc["measured_requests"], acc["estimated_requests"]), (2, 1, 1),
       "无 usage 也 +1 请求，但计入 estimated")
    acc = M.add_usage(acc, {"prompt_tokens": 300, "completion_tokens": -1})
    eq((acc["requests"], acc["measured_requests"], acc["estimated_requests"]), (3, 1, 2),
       "completion 非法 → 不算实测")
    eq(acc["prompt_tokens"], 400, "逐字段有效即累加：prompt 有效照加，不因 completion 非法丢弃")
    eq(acc["completion_tokens"], 20)
    acc = M.add_usage(acc, {"prompt_tokens": 200, "completion_tokens": 30,
                            "prompt_cache_hit_tokens": 150,
                            "prompt_cache_miss_tokens": 50, "reasoning_tokens": 8})
    eq((acc["requests"], acc["measured_requests"], acc["estimated_requests"]), (4, 2, 2))
    eq(acc["prompt_tokens"], 600)
    eq(acc["completion_tokens"], 50)
    eq(acc["cache_hit_tokens"], 150)
    eq(acc["cache_miss_tokens"], 50)
    eq(acc["reasoning_tokens"], 8)
    eq(acc["total_tokens"], 350, "total 缺失时按 prompt+completion 累计")
    eq(acc["cache_hit_rate"], 0.75)


def test_add_usage_rate_none_without_cache_measurements():
    acc = M.add_usage(None, {"prompt_tokens": 100, "completion_tokens": 5})
    eq(acc["cache_hit_tokens"], 0, "缺失按 0 累计（累加器口径）")
    eq(acc["cache_miss_tokens"], 0)
    eq(acc["cache_hit_rate"], None, "分母 0 → None")
    acc = M.add_usage(acc, {"prompt_tokens": 100, "completion_tokens": 5,
                            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0})
    eq(acc["cache_hit_rate"], None, "provider 明确报 0/0 时依然 None")


def test_add_usage_accepts_usage_snapshot_as_reported():
    # 面板侧会把 usage() 的实测字段直接喂给 add_usage（spec §4.2）
    snap = M.usage(CFG, MESSAGES, reported={"prompt_tokens": 100, "completion_tokens": 10,
                                            "total_tokens": 110,
                                            "prompt_cache_hit_tokens": 70,
                                            "prompt_cache_miss_tokens": 30})
    acc = M.add_usage(None, snap)
    eq(acc["requests"], 1)
    eq(acc["measured_requests"], 1)
    eq(acc["prompt_tokens"], 100)
    eq(acc["completion_tokens"], 10)
    eq(acc["cache_hit_tokens"], 70)
    eq(acc["cache_miss_tokens"], 30)
    eq(acc["total_tokens"], 110)
    eq(round(acc["cache_hit_rate"], 6), round(0.7, 6))


def test_add_usage_survives_foreign_or_dirty_total():
    for dirty_total in (None, {}, [], "junk", {"requests": True, "prompt_tokens": -5,
                                              "measured_requests": "3"}):
        acc = M.add_usage(dirty_total, {"prompt_tokens": 10, "completion_tokens": 2})
        eq(acc["requests"], 1, f"脏累计 {dirty_total!r} 不得计数污染")
        eq(acc["measured_requests"], 1)
        eq(acc["prompt_tokens"], 10)
        eq(acc["completion_tokens"], 2)


if __name__ == "__main__":
    raise SystemExit(run(globals(), "上下文缓存/推理用量（model_context）"))
