# 上下文用量 · 输入 / 输出 / 缓存 用量契约（冻结稿 2026-11-04）

> 本文件是并行开发的**唯一接口契约**。五个工作流各自只改自己的文件，
> 一律按本文件里的字段名、类型、语义实现；不要自作主张改名或加"差不多"的别名。
> 契约之外的新增字段必须先回本文件登记，再由 Lead 统一放行。

## 0. 背景与目标

现有上下文用量环（`addon/ads_agent/panel.py` 的 `ContextUsageIndicator`）只显示
"已用 / 模型容量"和接口上报的输入、输出 tokens。实际对话里用户还需要看到：

1. **输入**里有多少走**缓存命中**、多少是**未命中**（DeepSeek 的
   `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，OpenAI/GLM/StepFun 的
   `prompt_tokens_details.cached_tokens`）—— 命中率直接决定单价和首字延迟；
2. **输出**里有多少是**推理（thinking）** tokens；
3. **本回合**与**本次会话**的累计（请求数、输入、输出、缓存命中、未命中、命中率），
   而不是每次都只看"最近一次请求"。

约束（全部工作流共同遵守）：

- 服务端没上报的字段一律 `None`，界面**不得**用 0 冒充实测；估算路径必须保留
  `estimated=True` 与 `basis="token_estimate"`。
- 不改现有字段的含义与取值口径：`used` 仍表示"最近一次请求的输入 + 输出"，
  `max` 仍是模型上下文容量，`unit` 仍是 `"tokens"`（面板侧另有 `"chars"` 分支）。
- 全链路向后兼容：老面板连新后端、新面板连老后端都必须正常工作，不得 KeyError、
  不得把缺字段渲染成 `None`/`nan`。
- 只做纯本地计算，**不许**为了测试或演示真的去请求 LLM 接口。

## 1. `backend/model_context.py` —— 规范化与累加

### 1.1 `normalize_reported(reported) -> dict`

输入 `reported` 是 `llm.chat_stream()` 返回的 `usage` 原始字典，可能是 `None`、
不是 dict、字段缺失或类型不对。**绝不抛异常**。

输出 dict，键固定为下列 7 个，值可能是 `None`：

| 键 | 来源（按顺序取第一个有效值） |
| --- | --- |
| `prompt_tokens` | `prompt_tokens` |
| `completion_tokens` | `completion_tokens` |
| `total_tokens` | `total_tokens`，缺失时用 `prompt+completion`（两者都有效才算） |
| `cache_hit_tokens` | `prompt_cache_hit_tokens` → `cache_hit_tokens` → `cached_tokens` → `prompt_tokens_details.cached_tokens` → `prompt_tokens_details.cache_hit_tokens` |
| `cache_miss_tokens` | `prompt_cache_miss_tokens` → `cache_miss_tokens` → 由 `prompt_tokens - cache_hit_tokens` 推导（两者都有效且差 ≥ 0 才算） |
| `cached_tokens` | 与 `cache_hit_tokens` 同值（镜像，方便只认 `cached` 语义的界面统一读一个键） |
| `reasoning_tokens` | `completion_tokens_details.reasoning_tokens` → `reasoning_tokens` |

"有效值" = `int` 且非 `bool` 且 `>= 0`（复用现有 `valid_tokens()`）。
`prompt_tokens_details` / `completion_tokens_details` 必须是 dict，否则跳过。
provider 上报的 `cache_hit_tokens + cache_miss_tokens` 与 `prompt_tokens` 不一致时
**如实保留**，不做修正。

### 1.2 `usage(...)` 增补

`usage(cfg, messages, tools=None, reported=None, generated_chars=0)` 现有返回
（`model/max/unit/source/source_url/connection/used/estimated/basis/prompt_tokens/completion_tokens`）
**保持不变**，新增：

```
total_tokens, cache_hit_tokens, cache_miss_tokens, reasoning_tokens, cache_hit_rate
```

- `cache_hit_rate = cache_hit_tokens / (cache_hit_tokens + cache_miss_tokens)`，
  取值 `0.0`~`1.0` 的 float；分母缺失或为 0 → `None`。
- `reported` 不可用（走估算）时，上述 5 个新增字段**全部** `None`，
  并保持 `estimated=True`、`basis="token_estimate"`。

### 1.3 `add_usage(total, reported) -> dict`

累加器。`total` 为 `None`/`{}`/上一次 `add_usage` 的结果，`reported` 为 provider
原始 usage（内部先过 `normalize_reported`）。**返回新 dict，不修改入参**。

返回键固定：

```
requests            # 调用次数（无论有没有 usage 都 +1）
measured_requests   # prompt_tokens 与 completion_tokens 都有效的次数
estimated_requests  # = requests - measured_requests
prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens,
reasoning_tokens, total_tokens        # 有效整数累加，缺失按 0
cache_hit_rate      # 由累加后的 hit/(hit+miss) 派生，分母为 0 → None
```

## 2. `backend/llm.py` —— 把缓存字段真正拿到手

流式请求默认不返回 usage。修复点：

- `_build_body(..., stream=True)` 时附加 `"stream_options": {"include_usage": True}`；
- 若服务端因该字段返回 HTTP 400，**去掉 `stream_options` 重试一次**（与现有
  `thinking` 字段的 400 降级同一套路），不得让功能因网关不认字段而整体失败；
- 单个流里 usage 可能分散在多块（有的服务先发 `total_tokens` 再发 details），
  取 usage 时用"**合并**"而不是"整体替换"：dict 深一层合并（`prompt_tokens_details`
  之类嵌套 dict 也合并），非空值覆盖空值/旧值；
- 返回的 `usage` 保持 provider 原始结构（规范化交给 `model_context`），不要在这里
  改名成自定义字段。

## 3. `backend/agent.py` —— 回合累计与事件字段

### 3.1 回合累计

`Agent._turn_usage` 现有 `{"completion", "prompt", "llm_s", "known"}` 扩为：

```
{"completion": int, "prompt": int, "cache_hit": int, "cache_miss": int,
 "reasoning": int, "requests": int, "llm_s": float, "known": bool}
```

`_accum_usage(usage, elapsed_s)`：每次 LLM 调用 `requests += 1`；用
`model_context.normalize_reported` 累加 4 个 token 字段；`known` 语义不变
（至少一次拿到有效 prompt+completion）。

### 3.2 `context_usage` 事件

`_emit_context_usage(...)` 在现有字段之外增加（**顶层**平铺，取值来自
`model_context.usage()` 的结果，未实测为 `None`）：

```
cache_hit_tokens, cache_miss_tokens, cache_hit_rate, reasoning_tokens, total_tokens,
turn: {"requests", "prompt_tokens", "completion_tokens", "cache_hit_tokens",
       "cache_miss_tokens", "reasoning_tokens", "measured_requests", "llm_seconds"}
```

- `phase` 仍为 `"request"` / `"streaming"` / `"response"`；
- `"request"` / `"streaming"` 两阶段缓存字段为 `None`（还没有实测值），`turn` 为
  截至此刻的回合累计；
- `"response"` 阶段带 provider 实测值，`turn` 含本次调用。

### 3.3 `done` 事件 stats

`_stats_payload(model)` 增加：

```
cache_hit_tokens, cache_miss_tokens, reasoning_tokens, requests
```

取值来自回合累计；服务不返回 usage 时保持 `None`（`known=False`），不要伪造 0。

## 4. `addon/ads_agent/panel.py` —— 展示

### 4.1 事件归一化

`_on_event` 的 `context_usage` 分支：把新字段按"是 `int` 且非 `bool` 且 `>= 0`"
校验后写进 `self._context_usage[(project, model)]`，非法值置 `None`；
`cache_hit_rate` 校验为 `0.0~1.0` 的 `float`；`turn` 必须是 dict，逐字段校验。
**不得因为新字段缺失而丢弃整条事件**（老后端兼容）。

### 4.2 本次会话累计（面板侧）

面板自己累计，不落盘（重启清零，文案必须写"本次会话"）：

- 每收到一条 `phase == "response"` 且带实测 `model_context` 的事件，
  `session = model_context.add_usage(session, 实测字段)`；
- 会话累计存在 `self._context_usage[(project, model)]["session"]` 里，
  与上下文用量同 key —— 切换项目/模型各算各的；
- 估算事件（`basis != "provider_usage"`）不计入实测累计。

### 4.3 指示器展示

`ContextUsageIndicator` 仍是一个 32px 的 `QToolButton`，行为要求：

- **主环语义不变**：环弧长 = 上下文占用比例，≥80% 琥珀、≥95% 红；
- 悬浮 / 键盘聚焦 / 点击 → 打开**详情浮层**（不要只依赖超长 tooltip：
  现有 tooltip 已经 8 行以上，真机上会被屏幕边缘裁切）。浮层内容分组：

  ```
  上下文   12.3%  12,345 / 1,000,000 tokens      ← 模型名 + 容量来源
  本回合   输入 8,120 · 输出 1,234 · 缓存命中 6,000 (74%) · 未命中 2,120 · 推理 320
  本次会话 12 次请求 · 输入 92,310 · 输出 14,020 · 缓存命中率 71.2%
  ```

  没有的字段整行/整段省略，不显示 `None`、不显示"0 tokens"冒充；
- **tooltip 保留**（无障碍 `setAccessibleDescription` 同样更新），内容与浮层一致但更短；
- 缓存命中率建议用环内小弧或浮层内色条表达，**不得**替换或混淆上下文占用主环；
- 估算值前缀 `≈`；`basis == "local_history"` 时如实注明"未含后端系统提示和完整工具结果"。

### 4.4 每回合用量行

`_format_stats(stats, reply_text)` 在能拿到 `cache_hit_tokens + cache_miss_tokens > 0`
时追加 `缓存命中 62%`（`requests > 1` 时可追加 `· 3 次调用`），格式保持
` · ` 分隔，不改变现有顺序（Tokens · Token/秒 · 模型）。

## 5. 测试分工（互不重叠）

| 文件 | 归属 | 覆盖 |
| --- | --- | --- |
| `tests/test_usage_capture.py` | 工作流 1 | 流式 usage 捕获/合并、`stream_options` 400 降级 |
| `tests/test_model_context_cache.py` | 工作流 2 | 规范化（三种 provider 形状 + 脏数据）、`usage()` 新字段、`add_usage` 幂等与不变量 |
| `tests/test_context_usage_ui.py` | 工作流 4 | Qt offscreen：指示器浮层/tooltip 文案、缺字段降级、会话累计 |
| `tests/test_context_usage_flow.py` | 工作流 5 | 后端 → 事件 → 面板归一化的端到端（假 emit，不联网） |

`tests/run_tests.py` 只由工作流 5 修改（登记新文件、分层与说明）。
所有新测试必须能在 `python tests/run_tests.py --stdlib`（逻辑层）或无头 Qt 层跑通，
不得依赖 ADS 实机、网络、真实 API Key。

## 6. 验收口径

- `python tests/run_tests.py --stdlib` 全绿（跳过要写清楚原因）；
- Qt 层相关文件（`test_context_usage_ui.py` 等）在 PySide6 下 offscreen 跑通；
- `git diff` 只落在上表 + 各工作流声明的文件范围内；
- 新增字段在"没有缓存字段的服务端"上不产生噪音（tooltip 无空行、无 `None`）。
