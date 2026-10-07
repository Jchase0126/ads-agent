"""Minimal OpenAI-compatible chat-completions client (stdlib only).

Works with GLM (open.bigmodel.cn), DeepSeek, OpenAI, Ollama, vLLM — anything
that speaks the /chat/completions protocol with function calling.

深度思考（reasoning）：请求侧按 [llm] thinking 配置决定是否携带 thinking 参数
（智谱 GLM 官方协议 {"type": "enabled"}；DeepSeek V3.x+/v4 系列无需参数、
默认就流式返回 reasoning_content）。思考内容以增量回调的形式交给调用方
实时转发到面板，不拼进最终 message，也不会回填进对话历史。

传输层走 SSE 流式（stream=True），增量通过回调给出、最终拼装成与
非流式相同的 message 结构；chat() 只是 chat_stream 的便捷封装。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import adslog


class LLMError(RuntimeError):
    pass


class StreamIncomplete(LLMError):
    """流式响应异常中断（连接提前结束 / 没有 [DONE] 结束标志）。"""

    def __init__(self, message: str, partial_message: dict | None = None):
        super().__init__(message)
        self.partial_message = partial_message or {}


# 深度思考参数只在确认支持的官方接口上默认开启；其它网关靠 [llm] thinking=on 显式打开
_THINKING_HOST_SUFFIXES = ("bigmodel.cn", "zhipuai.cn")
# 本机推理服务（Ollama / vLLM / LM Studio 等）通常不需要 API Key：
# 是否需要密钥是服务能力，不是统一强制要求。
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
# 临时故障（限流 / 服务端抖动）的有限重试
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF = 1.5


def local_service(base_url: str) -> bool:
    host = urllib.parse.urlsplit(base_url or "").hostname or ""
    return host in _LOCAL_HOSTS


def thinking_requested(cfg: dict) -> bool:
    """cfg["llm_thinking"]: auto（默认，仅智谱官方接口）/ on / off。"""
    mode = str(cfg.get("llm_thinking", "auto")).strip().lower()
    if mode in ("off", "0", "false", "no"):
        return False
    if mode in ("on", "1", "true", "yes"):
        return True
    host = urllib.parse.urlsplit(cfg.get("llm_base_url", "")).hostname or ""
    return any(host == s or host.endswith("." + s) for s in _THINKING_HOST_SUFFIXES)


def _build_body(cfg: dict, messages: list, tools: list | None, stream: bool) -> dict:
    body = {
        "model": cfg["llm_model"],
        "messages": messages,
        "temperature": 0.3,
        "stream": bool(stream),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if thinking_requested(cfg):
        body["thinking"] = {"type": "enabled"}
        # GLM 官方约束：思考模式下 temperature 用服务端默认值最稳，干脆不带
        body.pop("temperature", None)
    return body


def chat_stream(cfg: dict, messages: list, tools: list | None = None, timeout: int = 240,
                on_reasoning=None, on_content=None) -> tuple:
    """流式请求一次对话补全，返回拼装完成的 (assistant message, usage)。

    on_reasoning / on_content 收到的都是**增量**文本（思考内容 / 正文），
    不关心传 None 即可。返回的 message 与 OpenAI 非流式结构一致：
      {"role": "assistant", "content": str, "tool_calls": [...]?, "complete": bool}
    思考内容只走回调，不拼进 message。``usage`` 取流中最后带 usage 的块
    （DeepSeek / GLM 都在收尾块里带；个别服务没有则为 None，调用方估算兜底）。

    完整性（2026-10-02）：
      * 正常结束 = 收到 [DONE] 结束标志；message["complete"] 如实标注；
      * 连接提前结束（没收到 [DONE]）且带了 tool_calls 时抛
        StreamIncomplete —— 调用方**绝不能执行残缺响应里的工具调用**；
      * 连不上 / 限流 / 5xx 在**收到任何流数据之前**自动重试
        （有限次数、退避）；已经开始吐字后中断则抛 StreamIncomplete。

    API Key：本机服务（127.0.0.1 / localhost 等）不强制要求密钥；
    远程服务缺密钥才报错。
    """
    api_key = cfg.get("llm_api_key", "")
    base_url = cfg["llm_base_url"].rstrip("/")
    if not api_key and not local_service(base_url):
        raise LLMError(
            "未配置 LLM API Key：请在 config.ini 的 [llm] api_key 填写，"
            "或设置环境变量 ADS_AGENT_API_KEY"
        )

    url = base_url + "/chat/completions"
    body = _build_body(cfg, messages, tools, stream=True)
    use_thinking = "thinking" in body

    def _headers() -> dict:
        h = {"Content-Type": "application/json"}
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
        return h

    def _open(b: dict):
        req = urllib.request.Request(
            url, data=json.dumps(b).encode("utf-8"), headers=_headers(), method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def _http_error(e: urllib.error.HTTPError) -> LLMError:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        # 上游错误响应里可能回显请求头/密钥，进日志前脱敏
        return LLMError(f"LLM 接口返回 HTTP {e.code}: {adslog.redact(detail)}")

    def _transient(e) -> bool:
        """限流 / 服务端临时故障 / 连接抖动 —— 流未开始时值得重试。"""
        if isinstance(e, urllib.error.HTTPError):
            return e.code in (408, 429, 500, 502, 503, 504)
        return isinstance(e, (urllib.error.URLError, TimeoutError,
                              ConnectionError, OSError))

    message: dict = {"role": "assistant", "content": "", "complete": False}
    calls: dict = {}
    usage = None
    resp = None
    last_err: Exception | None = None
    for attempt in range(_TRANSIENT_RETRIES + 1):
        message = {"role": "assistant", "content": "", "complete": False}
        calls, usage = {}, None
        try:
            body_try = body
            resp = _open(body_try)
        except urllib.error.HTTPError as e:
            if e.code == 400 and use_thinking:
                # 该模型/网关不认识 thinking 字段：去掉后降级重试一次
                body_try = {k: v for k, v in body.items() if k != "thinking"}
                body_try["temperature"] = 0.3
                try:
                    resp = _open(body_try)
                except urllib.error.HTTPError as e2:
                    raise _http_error(e2) from e2
                except urllib.error.URLError as e2:
                    raise LLMError(f"无法连接 LLM 接口 {url}: {e2.reason}") from e2
            elif _transient(e) and attempt < _TRANSIENT_RETRIES:
                last_err = e
                time.sleep(_TRANSIENT_BACKOFF * (attempt + 1))
                continue
            else:
                raise _http_error(e) from e
        except _transient_error_types() as e:
            if attempt < _TRANSIENT_RETRIES:
                last_err = e
                time.sleep(_TRANSIENT_BACKOFF * (attempt + 1))
                continue
            raise LLMError(f"无法连接 LLM 接口 {url}: {e}") from e

        received_any = False
        done_seen = False
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue  # SSE 注释 / 空行 / 心跳
                received_any = True
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done_seen = True
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                r = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(r, str) and r and on_reasoning:
                    on_reasoning(r)
                c = delta.get("content")
                if isinstance(c, str) and c:
                    message["content"] += c
                    if on_content:
                        on_content(c)
                for i, tcd in enumerate(delta.get("tool_calls") or []):
                    idx = tcd.get("index", i)
                    slot = calls.setdefault(idx, {"id": "", "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                    # 与 message 共享调用对象：后续增量原位补齐，也让中断异常
                    # 保留已收到的调用。只有 complete=True 才能交给 Agent 执行。
                    message["tool_calls"] = [calls[k] for k in sorted(calls)]
                    if tcd.get("id"):
                        slot["id"] = tcd["id"]
                    fn = tcd.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
        except _transient_error_types() as e:
            # 流中途断开：重试会重复已发给回调的内容（思考/正文会重复显示），
            # 所以只要已经收到过数据就不再自动重试，如实报告中断
            raise StreamIncomplete(
                f"模型响应流在传输中中断（已接收 {len(message['content'])} 字正文），"
                f"请重试：{type(e).__name__}: {e}",
                partial_message=message,
            ) from e
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

        message["complete"] = done_seen
        if not done_seen:
            # 没有 [DONE]：流被服务端/网络提前掐断。带工具调用的残缺响应
            # 绝不能执行（参数很可能没拼完）；纯文本残缺响应打上标记交调用方。
            if calls:
                raise StreamIncomplete(
                    "模型响应流异常中断（未收到结束标志），其中的工具调用不完整，"
                    "已阻止执行。请重试。",
                    partial_message=message,
                )
            return message, usage
        return message, usage

    # 重试耗尽（理论上不会到这里，attempt 循环里要么 return 要么 raise）
    raise LLMError(f"无法连接 LLM 接口 {url}: {last_err}")


def _transient_error_types() -> tuple:
    return (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def chat(cfg: dict, messages: list, tools: list | None = None, timeout: int = 240) -> tuple:
    """Send one chat completion request, return (assistant message, usage).

    chat_stream 的便捷封装（内部也是流式，只是不关心增量）。message 结构：
      {"role": "assistant", "content": str|None, "tool_calls": [...]?}
    ``usage`` 是接口返回的 token 统计，个别服务不返回时为 None —— 调用方估算兜底。
    """
    return chat_stream(cfg, messages, tools=tools, timeout=timeout)


class ApiUnreachable(LLMError):
    """DNS/connection/timeout — the endpoint is not reachable at all."""


def list_models(base_url: str, api_key: str = "", timeout: int = 20) -> list[str]:
    """Fetch model ids from an OpenAI-compatible endpoint (GET /models).

    Raises ApiUnreachable when the host cannot be reached, LLMError otherwise
    (bad auth, missing /models endpoint, malformed payload).
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LLMError(f"认证失败（HTTP {e.code}）：API 密钥无效或无权限") from e
        if e.code == 404:
            raise LLMError("地址可达，但该服务未提供 /models 模型列表接口（可手动填写模型名）") from e
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise LLMError(f"服务返回 HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ApiUnreachable(f"无法连接 {url}: {e.reason}") from e
    except TimeoutError as e:
        raise ApiUnreachable(f"连接超时（>{timeout}s）：{url}") from e
    except json.JSONDecodeError as e:
        raise LLMError("响应不是有效的 JSON，可能不是 OpenAI 兼容接口") from e

    items = data.get("data") if isinstance(data, dict) else data
    ids: list[str] = []
    for item in items or []:
        mid = item.get("id") if isinstance(item, dict) else item
        if mid:
            ids.append(str(mid))
    if not ids:
        raise LLMError("接口可达，但未返回任何模型")
    return sorted(set(ids))
