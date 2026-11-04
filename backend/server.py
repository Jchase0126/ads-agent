"""ADS Agent backend HTTP server (stdlib only, no pip install needed).

Endpoints (除 /health 外都需要请求头 X-Ads-Agent-Token，见 ads_auth.py):
  GET  /health  -> {"status": "ok", ...}   ← 开放，供启动探测使用
  GET  /logs    -> {"lines": [...]}  最近 N 行日志（?n=200），排查用
  GET  /config  -> 当前 LLM 配置（脱敏）
  GET  /tools   -> 可用工具名
  GET  /design/job?id=<job_id> -> 设计任务（结果页）的完整记录
  POST /config  -> 保存 LLM 设置
  POST /config/model -> 鉴权后读取指定模型的连接参数（含密钥）
  POST /chat    -> SSE event stream; body: {"messages": [...], "allow_python": bool}
  POST /test_connection -> 探测 base_url 可用性并拉取模型列表
  POST /design/run        -> 跑仿真 + 读真实曲线 + 确定性评估，返回结果页数据
  POST /design/resimulate -> 按原指标重新仿真并评估（结果页的"重新仿真"）
  POST /design/reload     -> 复用已有 .ds 重新评估（不重新仿真）
  POST /design/open_schematic -> 在 ADS 中打开指定原理图
  POST /models/upload  -> 二进制上传模型压缩包（**不走 JSON**）
  GET  /models/packages -> 列出当前工作区的模型包
  POST /models/import   -> 按需解压并导入某个包
  POST /models/cancel   -> 请求取消导入

模型压缩包为什么必须走独立的二进制通道
---------------------------------------
真实原厂包很大（本机样本 14~41 MB，大的 Design Kit 上百 MB），而 /chat 等
JSON 接口统一限制 8 MB。把 ZIP 转成 Base64 塞进 JSON 会同时踩三个坑：
体积膨胀 33%、整个文件进内存、以及**模型文件内容会进聊天历史与 LLM 请求**
（等于把原厂模型说明书喂给模型，既浪费又危险）。

所以这里用 ``Content-Type: application/zip`` + 二进制请求体：文件内容全程
不进 JSON、不进对话历史、不进 LLM 请求 —— LLM 只拿 package_id 与元数据。
仍然受同一个 X-Ads-Agent-Token 保护（凡是能驱动 ADS 的接口都必须鉴权）。

设计闭环的实测值与达标结论一律由 design_metrics 从真实数据算出，
LLM 只能提供设计与指标定义 —— 见 design_service 的说明。

鉴权说明：/chat 配合 allow_python 等于"在 ADS 进程里执行任意代码"，所以除
/health 之外的接口一律要求令牌。令牌由 ads_auth.py 统一生成/读取，校验失败只
返回一句 {"error": "unauthorized"}，绝不回显或记录令牌本身。

日志始终写入 <project>/logs/backend.log（不依赖 stdout 重定向）。

Run:  python backend/server.py
"""

import json
import model_context
import pdf_extract
import datasheet_models
import doc_assets
import spice_schematic
import spice_ads_plan
import os
import queue
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ads_auth
import adslog
import agent
import config as config_mod
import design_job as design_job_mod
import design_service as design_svc
import feedback
import llm as llm_mod
import model_orchestration
import model_store
import model_tools
import shared_models
import update_service
import tools as tools_mod
from tools import TOOLS

CFG = config_mod.load()
_LOG_FILE = adslog.setup()
log = adslog.get("backend.server")
log.info("=" * 60)
log.info("后端启动  Python=%s  配置=%s  日志=%s",
         sys.version.split()[0], config_mod.CONFIG_PATH, _LOG_FILE)
log.info("监听 http://%s:%s  模型=%s  max_tool_steps=%s  仿真超时=%ss  仿真脱离主线程=%s",
         CFG["backend_host"], CFG["backend_port"], CFG["llm_model"],
         CFG["max_tool_steps"], CFG["sim_timeout"], CFG["sim_off_main_thread"])
log.info("ADS 工具服务=http://%s:%s  回环令牌=%s（接口鉴权已启用，/health 除外）",
         CFG["ads_host"], CFG["ads_port"], ads_auth.mask(CFG["ads_token"]))

# ---------------------------------------------------------------------------
# 对话轮次注册表：turn_id -> agent.Turn。/chat/cancel 据此找到正在运行的
# 轮次并置位取消事件 —— 取消沿调用链传播（不再发起新请求/新工具派发）。
# ---------------------------------------------------------------------------
_TURNS_LOCK = threading.Lock()
_ACTIVE_TURNS: dict = {}
# 设计任务取消事件注册表：job_id -> threading.Event（任务结束即移除）
_DESIGN_CANCELS: dict = {}
_DESIGN_CANCELS_LOCK = threading.Lock()
# 幂等缓存：request_id -> (时间戳, 响应 dict)。重复点击/客户端重试返回原
# 响应，不会重新执行同一次设计写入。容量与 TTL 双重限制。
_IDEM_CACHE: dict = {}
_IDEM_LOCK = threading.Lock()
_IDEM_TTL = 24 * 3600
_IDEM_MAX = 500

# 模型包上传：走独立二进制通道（见模块 docstring 的说明）。
# 真实原厂包 14~41 MB 起，上百 MB 的 Design Kit 也常见；上限取 2 GiB 的
# Content-Length 校验，实际写入仍按流式分块，不一次性读进内存。
_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024
_DATASHEET_MAX_BYTES = 32 * 1024 * 1024
_SPICE_MAX_BYTES = 4 * 1024 * 1024


def _register_turn(turn) -> None:
    with _TURNS_LOCK:
        # 防御：轮次异常退出没清理时也不至于无限堆积
        if len(_ACTIVE_TURNS) > 200:
            for k in list(_ACTIVE_TURNS)[:100]:
                _ACTIVE_TURNS.pop(k, None)
        _ACTIVE_TURNS[turn.turn_id] = turn


def _unregister_turn(turn_id: str) -> None:
    with _TURNS_LOCK:
        _ACTIVE_TURNS.pop(turn_id, None)
        _TURN_EVENTS.pop(turn_id, None)


# turn_id -> SSE 事件队列（2026-10-30）：/chat/cancel 受理后立即向流里
# 注入 {"type":"cancel_requested"}，让前端马上知道"停止已受理、执行中
# 作业正在收尾"，而不是干等工具到达下一个取消检查点（长工具下最坏是
# 它的 socket 超时）。轮次结束随 _unregister_turn 一并移除。
_TURN_EVENTS: dict = {}


def _set_turn_events(turn_id: str, events) -> None:
    with _TURNS_LOCK:
        _TURN_EVENTS[turn_id] = events


def _push_turn_event(turn_id: str, ev: dict) -> None:
    with _TURNS_LOCK:
        q = _TURN_EVENTS.get(turn_id)
    if q is not None:
        q.put(ev)


def _idem_lookup(request_id: str):
    with _IDEM_LOCK:
        hit = _IDEM_CACHE.get(request_id)
        if hit is None:
            return None
        stamp, resp = hit
        if time.time() - stamp > _IDEM_TTL:
            _IDEM_CACHE.pop(request_id, None)
            return None
        return resp


def _idem_store(request_id: str, resp: dict) -> None:
    with _IDEM_LOCK:
        if len(_IDEM_CACHE) >= _IDEM_MAX:
            for k in sorted(_IDEM_CACHE, key=lambda k: _IDEM_CACHE[k][0])[:100]:
                _IDEM_CACHE.pop(k, None)
        _IDEM_CACHE[request_id] = (time.time(), resp)


def _new_turn_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _decode_upload_filename(headers) -> str:
    """从请求头取上传文件名，正确处理中文与非 ASCII。

    HTTP 头只能放 ASCII，所以中文文件名要么按 RFC 5987 编码
    （``filename*=UTF-8''%E6%9D%91%E7%94%B0.zip``），要么退化成 latin-1
    被 UTF-8 误解码成一串乱码。面板两种都发，这里优先取编码形式，
    并把 latin-1 乱码**还原**回 UTF-8 —— 否则「村田_模型.zip」会被存成
    一串看不出所以然的字符名，用户在附件卡片上根本认不出是自己那个包。
    """
    import re
    import urllib.parse

    raw_star = headers.get("X-Ads-Filename-Star") or headers.get("filename*") or ""
    if raw_star:
        # format: charset'lang'pct-encoded
        parts = raw_star.split("'", 2)
        encoded = parts[2] if len(parts) == 3 else raw_star
        try:
            return urllib.parse.unquote(encoded, encoding=parts[0] or "utf-8",
                                        errors="strict").strip()
        except (UnicodeDecodeError, LookupError):
            pass
    plain = (headers.get("X-Ads-Filename") or headers.get("filename") or "").strip()
    if not plain:
        return ""
    # latin-1 是 HTTP 头的老规矩：中文 UTF-8 字节会被逐字节塞进 latin-1
    if re.search(r"[\x80-\xff]", plain):
        try:
            return plain.encode("latin-1").decode("utf-8").strip()
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    return plain


def workspace_mismatch(current_path: str, target_workspace: str):
    """打开设计前的工作区核对。不一致返回给用户看的说明，一致返回 None。"""
    import os as _os

    def norm(p):
        p = str(p or "").strip()
        if not p:
            return ""
        # 注意 normpath("") == "."，必须先判空
        return _os.path.normcase(_os.path.normpath(p))

    target = norm(target_workspace)
    if not target or not current_path:
        return None
    if norm(current_path) != target:
        return (
            f"工作区不一致，已停止打开：目标设计位于 {target_workspace}，"
            f"而 ADS 当前打开的是 {current_path}。两个工作区可能存在同名的 "
            f"library/cell。请在 ADS 中切换到目标工作区后重试，"
            f"或先核对要打开的是不是当前工作区里的设计。"
        )
    return None


_PID = None  # main() 里赋值；模块被测试导入时不依赖运行状态
_STARTED_AT = ""

# 崩溃反馈的心跳/看守线程（start_feedback_watch 里创建，daemon）
_FEEDBACK_THREAD = None
_FEEDBACK_STOP = None


def _identity() -> dict:
    """本实例的身份（放进 /health，供面板/启动器判断"是不是自己那份安装"）。

    模块被测试导入时 instance 模块可能还没登记过任何状态，这里容忍失败：
    身份缺失只会让对端判定为"无法确认归属"，不会让服务起不来。
    """
    try:
        import instance

        return instance.identity()
    except Exception:  # noqa: BLE001
        return {}


def _config_status() -> dict:
    return {
        "base_url": CFG["llm_base_url"],
        "model": CFG["llm_model"],
        "models": CFG["llm_models"],
        "has_key": bool(CFG.get("llm_api_key")),
        "api_key_hint": config_mod.key_hint(CFG.get("llm_api_key", "")),
        "profile_labels": config_mod.profile_labels(CFG),
        "provider_groups": config_mod.provider_groups(CFG),
        "model_contexts": model_context.capacities(CFG),
        "context_budget_chars": int(120000 if CFG.get("context_budget_chars") is None
                                    else CFG["context_budget_chars"]),
        # 面板用它在状态栏如实描述"仿真期间界面会不会卡"
        "sim_off_main_thread": CFG["sim_off_main_thread"],
        "sim_timeout": CFG["sim_timeout"],
        # 崩溃反馈的开关（[feedback] 段）。面板据此显示"崩溃时自动打开
        # issue 页"之类的状态；写回走 POST /config 的 feedback_* 字段。
        "feedback_enabled": bool(CFG.get("feedback_enabled", True)),
        "feedback_auto_open_browser": bool(CFG.get("feedback_auto_open_browser", False)),
        "feedback_include_logs": bool(CFG.get("feedback_include_logs", True)),
        "feedback_max_log_lines": int(CFG.get("feedback_max_log_lines") or 120),
        "feedback_hang_seconds": int(CFG.get("feedback_hang_seconds") or 0),
    }


# ---------------------------------------------------------------------------
# 崩溃反馈：配置映射 / 心跳线程 / 卡死看守（2026-11-03）
# ---------------------------------------------------------------------------
#: 后端自己写心跳用的名字。**刻意**与面板侧不同：ADS 端写心跳用的名字是
#: feedback.HEARTBEAT_NAME（"ads_panel"），后端写 "backend" —— 各写各的，
#: 后端要判定的是"ADS 主线程还转不转"，不能拿自己的心跳去判。
_BACKEND_HEARTBEAT = "backend"

#: 心跳与看守的间隔（秒）。必须明显小于 hang_seconds，否则发现不及时。
_WATCH_INTERVAL_S = 30.0

#: 报告正文的长度上限（字符）。feedback.build_report 内部还会再截到 12000。
REPORT_MAX_CHARS = 20000

#: 允许的上报类型。未知类型一律 400，不静默改成 error —— 那会让"卡死"
#: 混进"崩溃"，去重指纹也就乱了。
REPORT_KINDS = ("crash", "hang", "error")

#: "这个 bug 又出现了"的统计窗口（秒）。与 feedback.record 的
#: dedupe_window_s 默认值保持一致，两处口径不同会让面板显示自相矛盾。
_DEDUPE_WINDOW_S = 600.0


def _feedback_cfg() -> dict:
    """把 CFG 里的 feedback_* **长键**映射成 feedback.settings() 认的**短键**。

    不能直接把 CFG 交给 feedback.settings()：它只认
    enabled/auto_open_browser/include_logs/max_log_lines/hang_seconds，
    而 config.DEFAULTS 里刻意用的是 feedback_ 前缀（见那里的说明），
    两边名字必须在这里接上，否则面板改了开关后端读到的还是默认值。
    """
    return {
        "enabled": bool(CFG.get("feedback_enabled", True)),
        "auto_open_browser": bool(CFG.get("feedback_auto_open_browser", False)),
        "include_logs": bool(CFG.get("feedback_include_logs", True)),
        "max_log_lines": int(CFG.get("feedback_max_log_lines") or 120),
        "hang_seconds": int(CFG.get("feedback_hang_seconds") or 0),
        "auto_upload": bool(CFG.get("feedback_auto_upload", True)),
        "upload_token": str(CFG.get("feedback_upload_token") or ""),
    }


def _feedback_payload() -> dict:
    """心跳里带一点"后端正在干什么"，卡死时能看出卡在哪类作业上。"""
    payload = {"pid": os.getpid()}
    try:
        with _TURNS_LOCK:
            payload["active_turns"] = len(_ACTIVE_TURNS)
    except Exception:  # noqa: BLE001
        pass
    try:
        compute = agent.compute_health()
        payload["compute"] = {
            "available": bool(compute.get("available")),
            "running": int(compute.get("running") or 0),
            "queued": int(compute.get("queued") or 0),
        }
    except Exception:  # noqa: BLE001
        pass
    return payload


def _check_hang(auto_record: bool = True) -> dict:
    """读一次 ADS 端心跳，判定是否卡死；检出时**自动**生成一份报告。

    ``auto_record`` 为真时，一旦发现"心跳陈旧且进程已经不在"，就调一次
    feedback.record("hang", ...) —— 这就是"无人值守自动上报"的后端半边。
    不在这里自己做去重：feedback.record 内置指纹去重（dedupe_window_s
    默认 600s），重复调用只会返回 duplicate=True，不会刷出第二份报告。

    任何异常都吞掉并降级成 {"stale": False, "error": ...}：看门狗坏掉
    不能反过来把 /feedback/pending 拖成 500。
    """
    cfg = _feedback_cfg()
    out = {
        "heartbeat": feedback.HEARTBEAT_NAME,
        "stale": False,
        "age": None,
        "pid": None,
        "alive": False,
        "limit": float(cfg["hang_seconds"]),
        "payload": {},
        "auto_reported": None,
    }
    try:
        status = feedback.check_stale(feedback.HEARTBEAT_NAME, cfg["hang_seconds"])
        out.update({k: status.get(k) for k in ("stale", "age", "pid", "alive", "limit")})
        out["payload"] = status.get("payload") or {}
    except Exception as e:  # noqa: BLE001
        log.exception("读取卡死心跳失败: %s: %s", type(e).__name__, e)
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if not auto_record or not out["stale"] or out["alive"]:
        return out
    # 心跳陈旧 + 进程已经不在 = 上一次运行是被掐断的（崩溃/强杀/卡死后被关）。
    age = float(out["age"] or 0)
    limit = float(out["limit"] or 0)
    pid = out["pid"]
    # 正文必须**逐字稳定**：fingerprint() 只吃 kind + error_text，把秒数
    # 拼进正文会让每 30s 一次的看守各算出一个新指纹，"靠内置去重不重复
    # 上报"就彻底失效了（实测：同一个卡死，age 3600/3630/3660 给出三个
    # 不同指纹）。变化的数字只放进 context —— 它不参与指纹计算。
    error_text = (
        f"ADS 端疑似卡死：{feedback.HEARTBEAT_NAME} 的心跳长时间未更新，"
        f"进程已不再响应。"
    )
    try:
        result = feedback.record(
            "hang", error_text,
            context={
                "source": "backend_watchdog",
                "heartbeat": feedback.HEARTBEAT_NAME,
                "age_s": round(age, 1),
                "limit_s": round(limit, 1),
                "pid": pid,
            },
            extra_names=(),
        )
        out["auto_reported"] = {
            "duplicate": bool(result.get("duplicate")),
            "fingerprint": result.get("fingerprint"),
            "paths": result.get("paths") or {},
        }
        log.warning("检出 ADS 端卡死（心跳 %.0fs 未更新，进程 %s 不在），已自动生成反馈报告：%s",
                    age, pid, (result.get("paths") or {}).get("json") or "（重复，未新增）")
    except Exception as e:  # noqa: BLE001
        log.exception("自动生成卡死报告失败: %s: %s", type(e).__name__, e)
        out["auto_reported"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _feedback_loop(stop_event: threading.Event) -> None:
    """后台看门狗：写自己的心跳，顺便周期性检查 ADS 端有没有卡死。

    必须是 daemon 且绝不把异常抛出去 —— 这个线程只是"顺带看一眼"，
    它挂了不能影响主服务，更不能让进程退出码变脏。
    """
    while not stop_event.wait(_WATCH_INTERVAL_S):
        try:
            feedback.write_heartbeat(_BACKEND_HEARTBEAT, _feedback_payload())
        except Exception:  # noqa: BLE001
            pass  # write_heartbeat 自己就不抛，这里只是双保险
        try:
            _check_hang(auto_record=True)
        except Exception:  # noqa: BLE001
            pass


def start_feedback_watch() -> bool:
    """启动心跳/看守线程（幂等：重复调用只有第一次生效）。"""
    # 两个名字都要声明 global：_FEEDBACK_THREAD 先读后写，少了声明会被
    # Python 当成局部变量，第一次调用就 UnboundLocalError。
    global _FEEDBACK_THREAD, _FEEDBACK_STOP
    if _FEEDBACK_THREAD is not None and _FEEDBACK_THREAD.is_alive():
        return False
    _FEEDBACK_STOP = threading.Event()
    thread = threading.Thread(target=_feedback_loop, args=(_FEEDBACK_STOP,),
                              name="feedback-watch", daemon=True)
    thread.start()
    _FEEDBACK_THREAD = thread
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ------------------------------------------------------------------
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    def _drain_body(self, limit: int = 1_000_000) -> None:
        """把未读的请求体丢掉。

        拒绝请求时不消费 body 会让同一条 keep-alive 连接的下一次读取从
        body 中间开始解析，所以 401 之前必须先排空。
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return
        remaining = min(length, limit)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _authorized(self) -> bool:
        """校验回环令牌；失败时回一句不含令牌信息的 401 并返回 False。"""
        if ads_auth.check_token(self.headers.get(ads_auth.TOKEN_HEADER, "")):
            return True
        # 只记录"谁被拒绝了"，不记录对方发了什么，也不回显期望值
        log.warning("拒绝未授权请求 %s %s", self.command, urlparse(self.path).path)
        self._drain_body()
        self._send_json({"error": "unauthorized"}, 401)
        return False

    # ------------------------------------------------------------------
    def _read_json(self, limit: int = 8_000_000, reply: bool = True):
        """读并解析 JSON 请求体；统一校验入口。

        规则（2026-10-02 统一）：
          * 请求体超过 limit（默认 8MB）→ 413 并**直接断开连接** ——
            绝不读半截 JSON 继续复用同一条 keep-alive 连接；
          * 非法 JSON / 非 JSON 对象（数组、字符串都不行）→ 400；
          * 失败时已代表调用方发出错误响应（reply=True），返回 None，
            调用方直接 return 即可；reply=False 供需要自定义响应的场景。
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > limit:
            self._drain_body(limit=1_000_000)
            self.close_connection = True
            if reply:
                self._send_json({"error": f"请求体过大（>{limit} 字节），已拒绝"}, 413)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # body 已被 rfile.read(length) **完整消费**，这里绝不能再用
            # _drain_body() 按 Content-Length 二次读取 —— keep-alive 连接上
            # 不会再来字节，会把这个服务线程永久挂住（表现为请求一直无响应）。
            if reply:
                self._send_json({"error": "bad request body"}, 400)
            return None
        if not isinstance(data, dict):
            # 同上：body 已读完，直接回 400，不再 drain。
            if reply:
                self._send_json({"error": "request body must be a JSON object"}, 400)
            return None
        return data

    def _handle_models(self, method: str, path: str) -> None:
        """模型压缩包的 HTTP 接口。

        关键设计点：
          * **目标工作区由可信上下文绑定** —— 所有写操作都先问 ADS 当前打开的
            工作区，请求体里就算带 workspace 也不采信（防越权写到别处）；
          * 上传不依赖 LLM 服务连通：保存与扫描都在本进程完成，LLM 只是后续
            查清单的一方；
          * 上传**不解压不加载**：只落盘 + 只读扫描中央目录 + 记清单。
        """
        action = path[len("/models/"):].strip("/")

        if method == "GET" and action == "packages":
            try:
                result = model_tools.list_model_packages(CFG, {})
            except model_tools.ModelToolError as e:
                self._send_json({"error": str(e), "kind": "no_workspace"}, 409)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("列出模型包失败: %s: %s", type(e).__name__, e)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._send_json({"ok": True, **result})
            return

        if method == "GET" and action == "op":
            self._handle_model_op()
            return

        if method == "GET" and action == "ops":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(int((query.get("limit") or ["50"])[0]), 200))
            except ValueError:
                limit = 50
            ops = model_orchestration.get_orchestrator().list_ops(limit=limit)
            self._send_json({"ok": True, "total": len(ops), "ops": ops})
            return

        if method == "GET" and action == "package":
            query = parse_qs(urlparse(self.path).query)
            package_id = (query.get("id") or [""])[0].strip()
            if not package_id:
                self._send_json({"error": "缺少 id"}, 400)
                return
            try:
                result = model_tools.inspect_model_package(
                    CFG, {"package_id": package_id})
            except model_tools.ModelToolError as e:
                self._send_json({"error": str(e)}, 404)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("查看模型包失败: %s: %s", type(e).__name__, e)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._send_json({"ok": True, **result})
            return

        if method == "POST" and action == "import":
            body = self._read_json()
            if body is None:
                return
            self._handle_model_import(body)
            return

        if method == "POST" and action == "open":
            body = self._read_json()
            if body is None:
                return
            self._handle_model_open(body)
            return

        if method == "POST" and action == "cancel":
            self._handle_model_cancel()
            return

        self._send_json({"error": f"未知的模型接口 {method} /models/{action}"}, 404)

    def _handle_model_import(self, body: dict) -> None:
        """受理一次导入：**只回一次 202 + op_id**。

        过去的写法（一个请求两次响应）是这样坏的：主线程发完 accepted 就关
        连接，后台线程完成后又拿同一个 Handler 的 ``_send_json`` 写一次 ——
        第二次往往写进一条已关闭/正被复用的 socket，面板表现为偶发的
        ``RemoteDisconnected``，而真实原因只是"导入跑完了"。

        现在后台线程**只**更新持久化操作记录与资产状态，不再持有 Handler；
        最终结果由 ``GET /models/op?id=<op_id>`` 查询（面板的轮询与"刷新状态"
        都走它）。
        """
        package_id = str(body.get("package_id") or "").strip()
        if not package_id:
            self._send_json({"error": "缺少 package_id"}, 400)
            return
        request_key = str(body.get("request_id") or "").strip()
        kit_root = str(body.get("kit_root") or "").strip()

        # 目标工作区**只认 ADS 当前打开的**，请求体里带的路径一律不采信；
        # 并且在这里就固定下来 —— 之后用户切换工程会被流水线核对出来并停止，
        # 不会挂到另一个工作区上。
        try:
            workspace = model_tools.current_workspace(CFG)
        except model_tools.ModelToolError as e:
            self._send_json({"error": str(e), "kind": "no_workspace"}, 409)
            return

        try:
            op, replayed = model_orchestration.get_orchestrator().submit(
                CFG, workspace, package_id, kit_root=kit_root,
                source="http", request_key=request_key,
                vendor_filter=str(body.get("vendor_filter") or "").strip())
        except model_orchestration.OrchestrationError as e:
            self._send_json({"error": str(e)}, 404)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("受理模型导入失败: %s: %s", type(e).__name__, e)
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return

        self._send_json({
            "ok": True,
            "accepted": True,
            "op_id": op["op_id"],
            "workspace": workspace,
            "package_id": package_id,
            "state": op["state"],
            "state_label": op.get("state_label"),
            "idempotent_replayed": bool(replayed),
            "message": ("导入已受理（后台执行）。"
                        + ("**没有新起导入**：同一工作区同一包已有进行中的导入，"
                           "本次已并入它。" if replayed else "")
                        + "用 GET /models/op?id=<op_id> 查进度与最终结果，"
                          "POST /models/cancel 取消。"),
        }, 202)

    def _handle_model_open(self, body: dict) -> None:
        """在 ADS 原生元件列表里打开/定位某个已导入的包（面板按钮直达，不经 LLM）。

        与 import 一样：目标工作区**只认 ADS 当前打开的**，请求体里的路径一律
        不采信；后端从清单按 package_id 解析可信套件根，再转给 ADS 端工具。
        返回 ``{"ok":.., "result":<3.1 结构>, "package":<清单视图>}`` 供面板直接渲染。
        """
        package_id = str(body.get("package_id") or "").strip()
        if not package_id:
            self._send_json({"error": "缺少 package_id"}, 400)
            return
        args = {"package_id": package_id}
        for key in ("library", "category", "view"):
            value = str(body.get(key) or "").strip()
            if value:
                args[key] = value
        try:
            result = model_tools.open_vendor_palette(CFG, args)
        except model_tools.AdsUnreachableError as e:
            # ADS 工具服务连不上/超时：与"没打开工作区"分开报，别让用户去开工程。
            self._send_json({"error": str(e), "kind": "ads_unreachable"}, 502)
            return
        except model_tools.ModelToolError as e:
            self._send_json({"error": str(e), "kind": "no_workspace"}, 409)
            return
        except tools_mod.AdsToolError as e:
            # 工作区已解析、仅 open 调用在传输层失败：同样如实报 502。
            self._send_json({"error": str(e), "kind": "ads_unreachable"}, 502)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("打开原生元件列表失败: %s: %s", type(e).__name__, e)
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        if not isinstance(result, dict):
            result = {"ok": False, "outcome": "failed",
                      "error": f"ADS 返回了无法解析的结果：{type(result).__name__}"}
        package = result.pop("package", None)
        self._send_json({
            "ok": bool(result.get("ok", True)),
            "result": result,
            "package": package or {},
        })

    def _handle_model_op(self) -> None:
        """查一条导入操作的状态（后台线程把最终结果写在这里）。"""
        query = parse_qs(urlparse(self.path).query)
        op_id = (query.get("id") or [""])[0].strip()
        if not op_id:
            self._send_json({"error": "缺少 id"}, 400)
            return
        op = model_orchestration.get_orchestrator().get_op(op_id)
        if op is None:
            self._send_json({"error": f"找不到导入操作 {op_id}", "known": False},
                            404)
            return
        self._send_json({"ok": True, **op})

    def _handle_model_cancel(self) -> None:
        """请求取消一条导入操作。

        取消是协作式的：接口在这里就返回，真正停下发生在流水线的下一个安全
        边界（排队 / 文件 / 数据块 / 阶段之间）。响应里的 ``state`` 是"已收到
        取消"，不是"已停止" —— 停止后状态才会变成 ``cancelled``。
        """
        body = self._read_json() or {}
        op_id = str(body.get("op_id") or "").strip()
        if not op_id:
            self._send_json({"error": "缺少 op_id"}, 400)
            return
        result = model_orchestration.get_orchestrator().request_cancel(CFG, op_id)
        if not result.get("ok"):
            self._send_json(result, 200 if result.get("known") else 404)
            return
        self._send_json({**result,
                         "message": "已收到取消请求，正在最近的安全边界停下；"
                                    "已完成的前置步骤保留。"})

    def _handle_upload(self) -> None:
        """接收二进制 ZIP 并保存为模型包资产。

        与 JSON 接口的关键差别：
          * 请求体是**原始字节**，不做 Base64（膨胀 33% 且会让模型文件内容
            进入 JSON / 对话历史 / LLM 请求）；
          * 边读边算 SHA-256、边写临时文件，不把整个包读进内存；
          * 超限时**断开连接**而不是读半截再复用 keep-alive。
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/zip", "application/x-zip-compressed",
                         "application/octet-stream"):
            self._drain_body(limit=1_000_000)
            self.close_connection = True
            self._send_json(
                {"error": f"不支持的 Content-Type: {ctype or '(空)'}；"
                          f"模型包上传必须用 application/zip 或 "
                          f"application/octet-stream 的原始字节流"},
                415)
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._drain_body()
            self._send_json({"error": "请求体为空：没有收到文件内容"}, 400)
            return
        if length > _UPLOAD_MAX_BYTES:
            self._drain_body(limit=1_000_000)
            self.close_connection = True
            self._send_json(
                {"error": f"文件过大（{length} 字节 > 上限 "
                          f"{_UPLOAD_MAX_BYTES} 字节）"}, 413)
            return

        # 文件名放在请求头里（JSON 通道会把它和内容混在一起，且非 ASCII
        # 需要编码）。支持两种编码：RFC 5987 的 filename*=UTF-8''... 优先。
        filename = _decode_upload_filename(self.headers)
        if not filename:
            self._drain_body()
            self._send_json({"error": "缺少文件名（X-Ads-Filename 请求头）"}, 400)
            return
        if not filename.lower().endswith(".zip"):
            self._drain_body()
            self.close_connection = True
            self._send_json(
                {"error": f"首版只支持 ZIP 压缩包，收到的是 {filename}。"
                          f"其它压缩格式（tar / rar / 7z）暂不支持。"}, 415)
            return

        # 目标工作区：**只认 ADS 当前打开的**，请求体/请求头里带的路径不采信
        try:
            workspace = model_tools.current_workspace(CFG)
        except model_tools.ModelToolError as e:
            # Content-Length 已确认在允许的 2 GiB 上限内。客户端会持续发送
            # 请求体；只排空默认的 1 MiB 后就关连接，会在大 ZIP (>1 MiB)
            # 上传到一半时触发 WinError 10053，客户端收不到真正的 409。
            # 把这个有界请求体读完，再返回明确的 no_workspace 错误。
            self._drain_body(limit=length)
            self.close_connection = True
            self._send_json({"error": str(e), "kind": "no_workspace"}, 409)
            return

        source_session = (self.headers.get("X-Ads-Session") or "").strip()

        # 落到临时文件：边收边算哈希，不把整包读进内存
        import hashlib
        import tempfile

        tmp_dir = os.path.join(tempfile.gettempdir(), "ads_agent_uploads")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"up_{os.getpid()}_{threading.get_ident()}.zip")
        digest = hashlib.sha256()
        received = 0
        try:
            with open(tmp_path, "wb") as out:
                while received < length:
                    chunk = self.rfile.read(min(_UPLOAD_CHUNK, length - received))
                    if not chunk:
                        break
                    received += len(chunk)
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if received != length:
                raise OSError(f"文件未传完（收到 {received}/{length} 字节）")

            record = model_store.save_archive(
                workspace, filename, source_path=tmp_path,
                source_session=source_session)
            # reused 是 save_archive 的一次性返回标记（表示这次没有新建
            # 资产、只是加了引用）。后面的 scan_archive 会返回**重新读出
            # 的记录**，那个标记不在清单里 —— 不先存下来就会丢，界面就
            # 分不清"新上传"和"复用已有资产"。
            was_reused = bool(record.get("reused"))

            # 只读扫描中央目录（不解压）：识别包类型与套件根
            root = model_store.store_root(workspace)
            try:
                record = model_store.scan_archive(root, record["package_id"])
                scan_note = ""
            except Exception as e:  # noqa: BLE001 — 扫描失败不该让上传白费
                scan_note = f"包结构扫描失败（文件已保存，可稍后重试检查）：{e}"
                log.warning("上传后扫描失败 %s: %s", record.get("package_id"), e)

            try:
                backup = shared_models.backup_package(CFG, workspace, record)
                backup_summary = {k: backup.get(k) for k in
                                  ("backed_up", "package_id", "sha256", "reused", "library_root")}
                backup_note = ""
            except Exception as e:  # noqa: BLE001 — 本地上传保留，明确报告备份失败
                backup_summary = {"backed_up": False, "error": f"{type(e).__name__}: {e}"}
                backup_note = f"统一模型库备份失败：{type(e).__name__}: {e}"
                log.exception("统一模型库备份失败 %s", record.get("package_id"))

            self._send_json({
                "ok": True,
                "workspace": workspace,
                "package": model_tools._record_view(record, include_models=False),
                "reused": was_reused,
                "shared_library_backup": backup_summary,
                "scan_note": scan_note,
                "backup_note": backup_note,
                "message": ("已保存到当前工作区并完成包结构检查。"
                            + ("已备份到统一 libraries 目录。" if backup_summary.get("backed_up")
                               else "统一模型库备份未完成，请检查备份错误。")
                            + "**未解压、未加载套件、未执行包内脚本** —— "
                            "需要时点附件卡片上的「解压并导入」或明确告诉我。"),
            })
        except model_store.UnsafeArchive as e:
            self._send_json({"error": f"压缩包安全校验未通过：{e}"}, 400)
        except Exception as e:  # noqa: BLE001
            log.exception("模型包上传失败: %s: %s", type(e).__name__, e)
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

    def _handle_datasheet_parse(self) -> None:
        """Extract readable text from an uploaded datasheet PDF for this chat turn."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/pdf", "application/octet-stream"):
            self._drain_body(limit=1_000_000)
            self._send_json({"error": "数据手册上传只接受 PDF 文件"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > _DATASHEET_MAX_BYTES:
            self._drain_body(limit=1_000_000)
            self._send_json({"error": f"PDF 大小必须在 1 字节至 {_DATASHEET_MAX_BYTES} 字节之间"},
                            413 if length > _DATASHEET_MAX_BYTES else 400)
            return
        filename = _decode_upload_filename(self.headers)
        if not filename.lower().endswith(".pdf"):
            self._drain_body(limit=length)
            self._send_json({"error": "文件名必须以 .pdf 结尾"}, 415)
            return
        data = self.rfile.read(length)
        if len(data) != length:
            self.close_connection = True
            self._send_json({"error": "PDF 上传未完成"}, 400)
            return
        if not data.startswith(b"%PDF-"):
            self._send_json({"error": "文件内容不是有效的 PDF 文件"}, 400)
            return
        import hashlib
        digest = hashlib.sha256(data).hexdigest()
        try:
            # 原文件按内容 SHA-256 落盘（原子写 + 去重）——解析器升级、字体
            # 修复、曲线提取都可以按 document_id 重新读取原始页面。
            asset = doc_assets.store(data, expect_sha=digest)
            parsed = pdf_extract.extract_document(
                data, runtime_python=CFG.get("datasheet_runtime_python") or "",
                pdf_path=asset["absolute_path"])
            excerpt = pdf_extract.make_excerpt(parsed)
            document = datasheet_models.save_document(
                filename, digest, parsed, length,
                session=(self.headers.get("X-Ads-Session") or "").strip())
        except (ValueError, OSError) as exc:
            self._send_json({"error": f"手册解析或记录保存失败：{exc}"}, 422)
            return
        real_pages = parsed.get("page_numbers_available", False)
        self._send_json({
            "ok": True, "filename": filename, "size_bytes": length,
            "sha256": digest,
            "document_id": document["document_id"],
            # 旧字段名保留（面板卡片继续可读）；完整解析时值就是真实页数
            "page_count_approx": parsed.get("page_count", 0),
            "page_count_source": document.get("page_count_source"),
            "page_numbers_available": real_pages,
            "parse_engine": document.get("parse_engine"),
            "has_original": document.get("has_original", False),
            "unknown_glyph_lines": document.get("unknown_glyph_lines", 0),
            "chars": parsed.get("chars", 0), "has_text_layer": parsed.get("has_text_layer", False),
            "parameter_extraction_safe": parsed.get("parameter_extraction_safe", False),
            "note": parsed.get("note", ""), "text": excerpt,
            "truncated": len(parsed.get("text", "")) > pdf_extract.EXCERPT_LIMIT,
        })

    def _handle_spice_schematic_parse(self) -> None:
        """Parse an exported SPICE netlist into a bounded, reviewable Circuit IR."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > _SPICE_MAX_BYTES:
            self._drain_body(limit=1_000_000)
            self._send_json({"error": f"SPICE 网表大小必须在 1 字节至 {_SPICE_MAX_BYTES} 字节之间"},
                            413 if length > _SPICE_MAX_BYTES else 400)
            return
        filename = _decode_upload_filename(self.headers)
        if os.path.splitext(filename)[1].lower() not in {".cir", ".sp", ".net", ".spice"}:
            self._drain_body(limit=length)
            self._send_json({"error": "仅支持 .cir、.sp、.net、.spice 导出的 SPICE 网表"}, 415)
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            self._send_json({"error": "SPICE 网表上传未完成"}, 400)
            return
        import hashlib
        digest = hashlib.sha256(raw).hexdigest()
        text = None
        for encoding in ("utf-8-sig", "gb18030", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            self._send_json({"error": "网表编码无法识别；请另存为 UTF-8 文本"}, 422)
            return
        try:
            parsed = spice_schematic.parse_spice_text(text, filename=filename)
        except (ValueError, TypeError) as exc:
            self._send_json({"error": f"SPICE 网表解析失败：{exc}"}, 422)
            return
        if not isinstance(parsed, dict) or not isinstance(parsed.get("circuit_ir"), dict):
            self._send_json({"error": "解析器未返回有效 Circuit IR"}, 422)
            return
        ir_text = json.dumps(parsed["circuit_ir"], ensure_ascii=False,
                             separators=(",", ":"))
        if len(ir_text) > 200_000:
            self._send_json({"error": "解析结果超过聊天上下文安全上限；请仅导出目标电路或目标子电路"}, 413)
            return
        diagnostics = list(parsed.get("diagnostics") or [])
        diagnostics_truncated = len(diagnostics) > 100
        ads_plan = spice_ads_plan.compile_ads_build_plan(parsed)
        self._send_json({"ok": True, "filename": filename, "size_bytes": length,
                         "sha256": digest, "source_format": "SPICE",
                         "parse_ok": bool(parsed.get("ok")),
                         "summary": parsed.get("summary") or {},
                         "ads_build_plan": ads_plan,
                         "circuit": parsed["circuit_ir"],
                         "diagnostics": diagnostics[:100],
                         "diagnostics_truncated": diagnostics_truncated})

    def _handle_design(self, path: str) -> None:
        """设计闭环接口：跑仿真 / 重新评估 / 打开原理图。

        实测值与达标结论一律由 design_service 调 design_metrics 从真实 .ds
        数据算出；请求体里就算带了 actual/pass 也不会被采用。
        """
        action = path[len("/design/"):].strip("/")
        if action == "open_schematic":
            body = self._read_json() or {}
            if not isinstance(body, dict):
                self._send_json({"error": "request body must be a JSON object"}, 400)
                return
            if not body.get("library") or not body.get("cell"):
                self._send_json({"error": "需要 library 与 cell"}, 400)
                return
            # 工作区核对：结果页可能来自另一个工作区，同名设计不能开错
            target_ws = str(body.get("workspace") or "").strip()
            if target_ws:
                try:
                    ws_info = tools_mod.call(CFG, "get_workspace_info", {})
                except tools_mod.AdsToolError as e:
                    self._send_json({"error": str(e)}, 502)
                    return
                except Exception as e:  # noqa: BLE001
                    self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
                    return
                if isinstance(ws_info, dict) and ws_info.get("workspace_open"):
                    mismatch = workspace_mismatch(
                        str(ws_info.get("path") or ""), target_ws)
                    if mismatch:
                        self._send_json({"error": mismatch, "kind": "workspace_mismatch"},
                                        409)
                        return
            try:
                result = tools_mod.call(CFG, "open_schematic", {
                    "library": body.get("library"),
                    "cell": body.get("cell"),
                    "view": body.get("view") or "schematic",
                })
            except tools_mod.AdsToolError as e:
                self._send_json({"error": str(e)}, 502)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("打开原理图失败: %s", e)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._send_json({"ok": True, "result": result})
            return

        if action not in ("run", "resimulate", "reload"):
            self._send_json({"error": f"未知的设计接口 /design/{action}"}, 404)
            return

        body = self._read_json()
        if body is None:
            self._send_json({"error": "bad request body"}, 400)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "request body must be a JSON object"}, 400)
            return

        root = config_mod.PROJECT_ROOT
        # 幂等：带 request_id 的重复请求直接返回原响应（不重复建图/重复仿真）
        request_id = str(body.get("request_id") or "").strip()
        if request_id:
            cached = _idem_lookup(request_id)
            if cached is not None:
                self._send_json({**cached, "idempotent_replayed": True})
                return

        # 取消事件：/design/cancel 可请求停止（阶段边界生效，产物保留）
        import uuid as _uuid
        cancel_event = threading.Event()
        cancel_key = str(body.get("job_id") or "") or _uuid.uuid4().hex
        cancel_event_key = cancel_key
        with _DESIGN_CANCELS_LOCK:
            if len(_DESIGN_CANCELS) > 200:
                for k in list(_DESIGN_CANCELS)[:100]:
                    _DESIGN_CANCELS.pop(k, None)
            _DESIGN_CANCELS[cancel_key] = cancel_event

        try:
            if action == "run":
                spec = design_svc.build_spec(body)
                job = design_svc.run_design(CFG, spec, root,
                                            cancel_event=cancel_event)
            else:
                job_id = str(body.get("job_id") or "").strip()
                if not job_id:
                    self._send_json({"error": "缺少 job_id"}, 400)
                    return
                cancel_event_key = job_id
                with _DESIGN_CANCELS_LOCK:
                    _DESIGN_CANCELS[cancel_event_key] = cancel_event
                # 同一任务串行：并发的 resimulate/reload 基于旧快照互相覆盖
                with design_svc.job_lock(job_id):
                    if action == "resimulate":
                        job = design_svc.resimulate(CFG, root, job_id,
                                                    cancel_event=cancel_event)
                    else:
                        job = design_svc.reload_job(CFG, root, job_id,
                                                    cancel_event=cancel_event)
        except design_svc.DesignError as e:
            self._send_json({"error": str(e)}, 400)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("设计接口 %s 失败: %s: %s", action, type(e).__name__, e)
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        finally:
            with _DESIGN_CANCELS_LOCK:
                _DESIGN_CANCELS.pop(cancel_key, None)
                try:
                    _DESIGN_CANCELS.pop(cancel_event_key, None)
                except Exception:  # noqa: BLE001
                    pass

        resp = {
            "ok": job.stage != design_job_mod.STAGE_FAILED,
            "job": job.to_dict(),
            # "计算已完成"与"结果未成功保存"必须分开报告
            "save_ok": not job.save_errors,
            **({"save_errors": job.save_errors} if job.save_errors else {}),
        }
        if request_id:
            _idem_store(request_id, resp)
        self._send_json(resp)

    def do_GET(self):
        path = urlparse(self.path).path
        # /health 保持开放：插件启动探测、check_env.py 都靠它判断后端是否活着
        if path != "/health" and not self._authorized():
            return
        if path == "/updates/check":
            self._send_json(update_service.check())
            return
        if path.startswith("/models/"):
            self._handle_models("GET", path)
            return
        if path == "/logs":
            # 排查用：直接看最近 N 行日志，省得去翻文件
            query = parse_qs(urlparse(self.path).query)
            try:
                n = int((query.get("n") or ["200"])[0])
            except ValueError:
                n = 200
            self._send_json({"log_file": _LOG_FILE, "lines": adslog.tail(max(1, min(n, 2000)))})
        elif path == "/health":
            has_key = bool(CFG.get("llm_api_key"))
            self._send_json(
                {
                    "status": "ok",
                    "service": "ads_agent_backend",
                    "protocol": 1,
                    "pid": _PID,
                    "started_at": _STARTED_AT,
                    "model": CFG["llm_model"],
                    "api_key_configured": has_key,
                    "max_tool_steps": CFG["max_tool_steps"],
                    "sim_timeout": CFG["sim_timeout"],
                    "sim_off_main_thread": CFG["sim_off_main_thread"],
                    # 只说明"接口需要鉴权"，不回显任何令牌信息
                    "auth_required": True,
                    "ads_tool_server": f"http://{CFG['ads_host']}:{CFG['ads_port']}",
                    # 身份：让探测方能确认"这是不是我那份安装的后端"，
                    # 而不是只看 200 就连上去（详见 backend/instance.py）
                    "identity": _identity(),
                    # 计算作业：排队的 / 执行中的 / 已请求取消的。
                    # 与 ADS 端 /health 的字段同义，但这里是**后端进程自己的**
                    # 计算 worker（run_compute），不经过 ADS GUI。
                    "compute": agent.compute_health(),
                }
            )
        elif path == "/tools":
            self._send_json({"tools": [t["function"]["name"] for t in TOOLS]})
        elif path == "/design/job":
            query = parse_qs(urlparse(self.path).query)
            job_id = (query.get("id") or [""])[0].strip()
            if not job_id:
                self._send_json({"error": "缺少 id"}, 400)
                return
            job, corrupt = design_job_mod.load_job_ex(config_mod.PROJECT_ROOT, job_id)
            if job is None:
                # 损坏原因要带给界面：不能只说"找不到"
                self._send_json({
                    "error": (f"任务文件损坏且无法恢复：{corrupt.get('error', '')}"
                              if corrupt.get("corrupt")
                              else f"找不到设计任务 {job_id}"),
                    "corrupt": bool(corrupt.get("corrupt")),
                    "detail": corrupt,
                }, 404)
                return
            payload = job.to_dict()
            if corrupt:
                payload["corrupt_info"] = corrupt
            self._send_json(payload)
        elif path == "/design/export":
            # 完整数据导出：从原始 .ds 重新读取指定表达式，CSV 直出。
            # 显示点只是画图采样 —— 报告里要用完整数据就走这里，
            # 绝不把 600 点的显示数组冒充原始数据。
            query = parse_qs(urlparse(self.path).query)
            job_id = (query.get("id") or [""])[0].strip()
            expr = (query.get("expr") or [""])[0].strip()
            if not job_id or not expr:
                self._send_json({"error": "需要 id 与 expr"}, 400)
                return
            job = design_job_mod.load_job(config_mod.PROJECT_ROOT, job_id)
            if job is None:
                self._send_json({"error": f"找不到设计任务 {job_id}"}, 404)
                return
            source = (job.artifacts or {}).get("dataset_source") or \
                     (job.artifacts or {}).get("dataset_path") or ""
            if not source or not os.path.exists(source):
                self._send_json({"error": f"数据集不存在或路径未记录：{source or '(无)'}"},
                                404)
                return
            try:
                raw = tools_mod.call(CFG, "read_traces", {
                    "path": source, "expressions": [expr], "max_points": 0,
                })
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"读取数据集失败：{e}"}, 502)
                return
            payload = (raw.get("traces") or {}).get(expr) or {}
            xs, ys = payload.get("x") or [], payload.get("y") or []
            if len(xs) != len(ys) or not xs:
                self._send_json({"error": "该表达式在数据集中没有有效数据"}, 404)
                return
            lines = [f"# {payload.get('y_name') or expr}",
                     f"# 来源: {source}",
                     f"# 完整数据（未降采样），共 {len(xs)} 点",
                     f"{payload.get('x_name') or 'x'}({payload.get('x_unit') or '未声明'}),"
                     f"{payload.get('y_name') or 'y'}({payload.get('y_unit') or '未声明'})"]
            lines += [f"{x!r},{y!r}" for x, y in zip(xs, ys)]
            self._send_json({"ok": True, "expr": expr, "source": source,
                             "n_points": len(xs), "csv": "\n".join(lines)})
        elif path == "/config":
            self._send_json(_config_status())
        elif path == "/feedback/pending":
            # 面板/脚本查询"有没有待上报的崩溃"：窗口内重复出现的指纹
            # + ADS 端心跳是否已经卡死（顺带触发一次自动上报）。
            body = {"ok": True}
            try:
                body["duplicates"] = feedback.recent_duplicates(_DEDUPE_WINDOW_S)
            except Exception as e:  # noqa: BLE001
                log.exception("读取重复指纹失败: %s: %s", type(e).__name__, e)
                body["duplicates"] = []
                body["duplicates_error"] = f"{type(e).__name__}: {e}"
            body["hang"] = _check_hang(auto_record=True)
            self._send_json(body)
        elif path == "/feedback/list":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 200))
            body = {"ok": True, "limit": limit}
            try:
                body["reports"] = feedback.list_reports(limit)
                body["dir"] = feedback.feedback_dir(create=False)
            except Exception as e:  # noqa: BLE001
                log.exception("列出反馈报告失败: %s: %s", type(e).__name__, e)
                body["ok"] = False
                body["reports"] = []
                body["error"] = f"{type(e).__name__}: {e}"
            body["hooks"] = feedback.hooks_installed()
            body["privacy_notice"] = feedback.PRIVACY_NOTICE
            body["repo"] = feedback.FEEDBACK_REPO
            self._send_json(body)
        else:
            self._send_json({"error": "not found"}, 404)

    # ------------------------------------------------------------------
    def do_POST(self):
        # 所有 POST 都是可操作接口（改配置 / 驱动 Agent），一律先鉴权
        if not self._authorized():
            return
        if self.path == "/updates/prepare":
            body = self._read_json()
            if body is None:
                return
            if body:
                self._send_json({"status": "error", "message": "请求体必须是空 JSON 对象"}, 400)
                return
            self._send_json(update_service.prepare())
            return
        if self.path == "/updates/apply":
            body = self._read_json()
            if body is None:
                return
            if body:
                self._send_json({"status": "error", "message": "请求体必须是空 JSON 对象"}, 400)
                return
            # 只应用本服务此前暂存并通过校验的更新包；不接受任何客户端路径。
            self._send_json(update_service.apply())
            return
        if self.path == "/chat/cancel":
            body = self._read_json() or {}
            turn_id = str(body.get("turn_id") or "").strip()
            if not turn_id:
                self._send_json({"error": "缺少 turn_id"}, 400)
                return
            with _TURNS_LOCK:
                turn = _ACTIVE_TURNS.get(turn_id)
            if turn is None:
                # 轮次可能已经结束 —— 如实说明，不报错误
                self._send_json({"ok": False, "known": False,
                                 "message": "该轮次已结束或不存在，无需取消"})
                return
            turn.cancel()
            # 2026-10-30 取消链：除了置轮次取消事件，还要对**已派发**的
            # 作业发出取消 —— ADS 端作业走 toolserver /cancel（排队作业被
            # 跳过不执行，执行中作业打标记），后端计算作业由 compute_manager
            # 按宽限期终止回收。回执里的状态是"已请求取消"，不是"已退出"。
            try:
                cancel_info = turn.propagate_cancel()
            except Exception as e:  # noqa: BLE001 — 取消尽力而为，不回 500
                log.warning("轮次 %s 取消传播失败: %s: %s",
                            turn_id, type(e).__name__, e)
                cancel_info = {"error": str(e)}
            log.info("轮次 %s 收到取消请求", turn_id)
            # 立即向 SSE 流注入受理事件（上面的 propagate_cancel 已把取消
            # 请求送达排队/执行中的作业）——前端不用等下一个取消检查点。
            _push_turn_event(turn_id, {"type": "cancel_requested",
                                       "turn_id": turn_id,
                                       "jobs": cancel_info})
            self._send_json({"ok": True, "known": True,
                             "jobs": cancel_info,
                             "message": "已请求取消：不再发起新请求；"
                                        "执行中的作业会在各自安全边界收尾，"
                                        "最终状态以查询为准。"})
            return

        if self.path == "/design/cancel":
            body = self._read_json() or {}
            job_id = str(body.get("job_id") or "").strip()
            if not job_id:
                self._send_json({"error": "缺少 job_id"}, 400)
                return
            with _DESIGN_CANCELS_LOCK:
                event = _DESIGN_CANCELS.get(job_id)
            if event is None:
                self._send_json({"ok": False, "known": False,
                                 "message": "该任务当前没有正在执行的操作"})
                return
            event.set()
            # 把取消同步给 ADS 端：排队中的仿真作业直接跳过
            ads_job_id = ""
            try:
                _job = design_job_mod.load_job(config_mod.PROJECT_ROOT, job_id)
                if _job is not None:
                    ads_job_id = (_job.sim or {}).get("ads_job_id") or ""
            except Exception:  # noqa: BLE001
                pass
            cancel_info = tools_mod.cancel_ads_jobs(
                CFG, [ads_job_id] if ads_job_id else [])
            log.info("设计任务 %s 收到取消请求（ADS 作业 %s）", job_id,
                     cancel_info)
            self._send_json({"ok": True, "known": True, "ads": cancel_info})
            return

        if self.path == "/config/provider/delete":
            body = self._read_json(limit=200_000)
            if body is None:
                return
            name = body.get("provider_model")
            if not isinstance(name, str) or not name.strip():
                self._send_json({"error": "请选择供应商"}, 400)
                return
            try:
                status = config_mod.update_llm_settings(
                    provider_model=name.strip(), provider_remove=True)
            except ValueError as e:
                # 404 保留给旧版服务未实现此路由，避免误判为兼容性问题。
                self._send_json({"error": str(e)}, 400)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("删除供应商失败")
                self._send_json({"error": f"删除供应商失败: {type(e).__name__}: {e}"}, 500)
                return
            CFG.update(config_mod.load())
            self._send_json({"ok": True, "deleted": True, **_config_status(),
                             "deleted_models": status["deleted_models"],
                             "config_locked": status["config_locked"]})
            return

        if self.path == "/config/model":
            body = self._read_json(limit=200_000)
            if body is None:
                return
            name = body.get("model")
            if not isinstance(name, str) or not name.strip():
                self._send_json({"error": "请选择模型"}, 400)
                return
            profile = config_mod.model_profile(name.strip())
            self._send_json(profile if profile is not None else {"error": "模型配置不存在"},
                            200 if profile is not None else 404)
            return

        if self.path == "/config":
            body = self._read_json(limit=200_000)
            if body is None:
                return
            try:
                status = config_mod.update_llm_settings(
                    base_url=body.get("base_url"),
                    api_key=body.get("api_key"),
                    model=body.get("model"),
                    models=body.get("models"),
                    provider_name=body.get("provider_name"),
                    provider_models=body.get("provider_models"),
                    provider_model=body.get("provider_model"),
                    provider_enabled=body.get("provider_enabled"),
                    context_model=body.get("context_model"),
                    context_window_tokens=body.get("context_window_tokens"),
                    provider_remove=body.get("provider_remove"),
                )
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"保存配置失败: {type(e).__name__}: {e}"}, 500)
                return
            CFG.update(config_mod.load())  # apply immediately
            # 任务书要求在 /config 里也能写这几个键。**只在请求体真的带了
            # 它们时才写** —— 面板保存模型设置时从不带 feedback_* 字段，
            # 若照抄"缺省即默认值"的写法，会把人家的反馈开关静默重置。
            if any(k in body for k in (
                    "feedback_enabled", "feedback_auto_open_browser",
                    "feedback_include_logs", "feedback_max_log_lines",
                    "feedback_hang_seconds")):
                try:
                    status.update(config_mod.update_feedback_settings(
                        enabled=body.get("feedback_enabled"),
                        auto_open_browser=body.get("feedback_auto_open_browser"),
                        include_logs=body.get("feedback_include_logs"),
                        max_log_lines=body.get("feedback_max_log_lines"),
                        hang_seconds=body.get("feedback_hang_seconds"),
                    ))
                    CFG.update(config_mod.load())
                except ValueError as e:
                    self._send_json(
                        {"error": f"保存反馈配置失败: {e}", **status}, 400)
                    return
                except Exception as e:  # noqa: BLE001
                    log.exception("保存反馈配置失败: %s: %s", type(e).__name__, e)
                    self._send_json(
                        {"error": f"保存反馈配置失败: {type(e).__name__}: {e}"}, 500)
                    return
            self._send_json({"ok": True, **status})
            return

        if self.path == "/config/feedback":
            # 崩溃反馈的几个开关单独走一条：面板保存 LLM 设置时从不带这些
            # 字段，混在 /config 里会让"保存模型"顺带把反馈开关写成默认值。
            body = self._read_json(limit=20_000)
            if body is None:
                return
            try:
                status = config_mod.update_feedback_settings(
                    enabled=body.get("feedback_enabled"),
                    auto_open_browser=body.get("feedback_auto_open_browser"),
                    include_logs=body.get("feedback_include_logs"),
                    max_log_lines=body.get("feedback_max_log_lines"),
                    hang_seconds=body.get("feedback_hang_seconds"),
                )
            except ValueError as e:
                # 参数不合法是调用方的问题，按 400 回，别报成 500
                self._send_json({"error": f"保存反馈配置失败: {e}"}, 400)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("保存反馈配置失败: %s: %s", type(e).__name__, e)
                self._send_json({"error": f"保存反馈配置失败: {type(e).__name__}: {e}"}, 500)
                return
            CFG.update(config_mod.load())  # apply immediately
            # 关掉总开关时把钩子卸掉？不做 —— 钩子已装的话继续装，
            # feedback.record 内部会因为 enabled=False 直接返回空结构，
            # 反而比"装一半卸一半"更容易排查。
            self._send_json({"ok": True, **status})
            return

        if self.path == "/feedback/report":
            body = self._read_json(limit=REPORT_MAX_CHARS * 4 + 20_000)
            if body is None:
                return
            kind = str(body.get("kind") or "crash").strip().lower()
            if kind not in REPORT_KINDS:
                self._send_json(
                    {"error": "kind 只接受 crash / hang / error",
                     "kinds": list(REPORT_KINDS)}, 400)
                return
            error_text = body.get("error")
            if not isinstance(error_text, str) or not error_text.strip():
                self._send_json({"error": "缺少 error（崩溃/报错的原文，不能为空）"}, 400)
                return
            truncated = False
            if len(error_text) > REPORT_MAX_CHARS:
                # 超长直接截断而不是拒绝：用户手上的报错文本可能就是一大坨
                # 日志，拒掉等于让人自己去删。截断位置在响应里如实说明。
                error_text = error_text[:REPORT_MAX_CHARS]
                truncated = True
            context = body.get("context")
            if context is not None and not isinstance(context, dict):
                self._send_json({"error": "context 必须是 JSON 对象"}, 400)
                return
            try:
                result = feedback.snapshot(kind, error_text, context or {})
            except Exception as e:  # noqa: BLE001
                log.exception("生成反馈报告失败: %s: %s", type(e).__name__, e)
                self._send_json({"error": f"生成反馈报告失败: {type(e).__name__}: {e}"}, 500)
                return
            if truncated:
                result["truncated"] = True
                result["max_chars"] = REPORT_MAX_CHARS
            self._send_json(result)
            return

        if self.path == "/test_connection":
            body = self._read_json(limit=200_000)
            if body is None:
                return
            base_url = llm_mod.normalize_base_url(body.get("base_url") or CFG["llm_base_url"])
            api_key = (body.get("api_key", CFG.get("llm_api_key")) or "").strip()
            try:
                import time as _time

                t0 = _time.perf_counter()
                models = llm_mod.list_models(base_url, api_key)
                latency = int((_time.perf_counter() - t0) * 1000)
                self._send_json(
                    {"ok": True, "reachable": True, "base_url": base_url,
                     "models": models, "count": len(models), "latency_ms": latency}
                )
            except llm_mod.ApiUnreachable as e:
                self._send_json({"ok": False, "reachable": False, "base_url": base_url,
                                 "models": [], "error": str(e)})
            except llm_mod.LLMError as e:
                self._send_json({"ok": False, "reachable": True, "base_url": base_url,
                                 "models": [], "error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "reachable": False, "base_url": base_url,
                                 "models": [], "error": f"{type(e).__name__}: {e}"})
            return

        if self.path.startswith("/design/"):
            self._handle_design(self.path)
            return

        if self.path == "/models/upload":
            self._handle_upload()
            return

        if self.path == "/datasheets/parse":
            self._handle_datasheet_parse()
            return

        if self.path == "/schematics/parse-spice":
            self._handle_spice_schematic_parse()
            return

        if self.path.startswith("/models/"):
            self._handle_models("POST", self.path)
            return

        if self.path != "/chat":
            self._send_json({"error": "not found"}, 404)
            return

        body = self._read_json(limit=8_000_000)
        if body is None:
            return

        messages = body.get("messages") or []
        allow_python = bool(body.get("allow_python", True))
        model = body.get("model")
        if not isinstance(messages, list):
            self._send_json({"error": "messages 必须是数组"}, 400)
            return
        if not messages:
            self._send_json({"error": "messages is empty"}, 400)
            return

        selected_model = model or CFG["llm_model"]
        if any(not g.get("enabled", True) and selected_model in g["models"]
               for g in config_mod.provider_groups(CFG)):
            self._send_json({"error": "该供应商已停用，请启用供应商或选择其他模型。"}, 400)
            return

        turn_id = str(body.get("turn_id") or "").strip() or _new_turn_id()
        events: queue.Queue = queue.Queue()
        turn = agent.Turn(CFG, messages, allow_python=allow_python, model=model,
                          turn_id=turn_id)
        _register_turn(turn)
        _set_turn_events(turn_id, events)
        events.put({"type": "start", "turn_id": turn_id})

        def worker():
            try:
                turn.run(events.put)
            except BaseException as e:  # noqa: BLE001 — worker 里崩了也要通知前端
                traceback.print_exc()
                log.exception("对话线程崩溃: %s: %s", type(e).__name__, e)
                events.put({"type": "error", "message": f"后端线程异常：{type(e).__name__}: {e}"})
            finally:
                _unregister_turn(turn_id)
            events.put(None)  # sentinel

        threading.Thread(target=worker, daemon=True).start()

        # Stream as SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                try:
                    ev = events.get(timeout=15)
                    if ev is None:
                        break
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keepalive comment
                    self.wfile.flush()
                    continue
                data = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + data + b"\n\n")
                self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # panel closed mid-turn


def main():
    global _PID, _STARTED_AT
    import atexit
    import datetime
    import os as _os

    import instance

    _PID = _os.getpid()
    _STARTED_AT = datetime.datetime.now().replace(microsecond=0).isoformat()

    host, port = CFG["backend_host"], CFG["backend_port"]

    # ---- 实例登记 ----
    # 先查"同一份安装有没有在别的端口上还跑着" —— 首版只支持单实例。
    # 不查的话两个后端会各写一份 config/日志，用户表现为"设置改了不生效 / 随机 401"。
    stale = instance.cleanup_stale_instances()
    if stale:
        log.info("清理了 %d 个已退出进程的实例登记: %s", len(stale), stale)
    clashes = instance.same_install_conflicts("backend", port)
    if clashes:
        detail = "；".join(
            f"pid={c.get('pid')} 端口={c.get('port')} 目录={c.get('app_root') or '?'}"
            for c in clashes
        )
        log.error(
            "检测到同一份安装的后端多开：%s。本实例仍会启动，但请停掉多余的那个 —— "
            "两个实例会各自持有一份内存配置，界面上的设置会互相覆盖。", detail
        )
        print(f"[ADS Agent] 警告：检测到重复的后端实例（{detail}）", file=sys.stderr)
    foreigners = instance.foreign_install_instances("backend")
    if foreigners:
        detail = "；".join(f"pid={c.get('pid')} 数据目录={c.get('data_root') or '?'}"
                           for c in foreigners)
        log.error("检测到**另一个 ADS Agent 安装**的后端仍在运行：%s。", detail)
        print(f"[ADS Agent] 警告：另一个 ADS Agent 安装也在运行（{detail}）",
              file=sys.stderr)

    # ---- 端口冲突：给一条能看懂的话，而不是抛裸 OSError ----
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        who = ""
        probed = instance.probe(f"http://{host}:{port}", timeout=1.5)
        verdict = (instance.evaluate(probed, "backend") if probed["reachable"] else None)
        if verdict and verdict["identity"]:
            who = (f" —— 占用者是另一个 ADS Agent 后端（pid="
                   f"{verdict['identity'].get('pid') or '?'}，数据目录 "
                   f"{verdict['identity'].get('data_root') or '?'}）")
        log.error("后端启动失败：端口 %s:%s 被占用%s  底层错误=%s", host, port, who, e)
        print(
            f"[ADS Agent] 启动失败：端口 {host}:{port} 已被占用{who}。\n"
            f"            请先在任务管理器里结束那个 python.exe，或在 config.ini 的 "
            f"[backend] port 换成别的端口。\n"
            f"            日志：{_LOG_FILE}",
            file=sys.stderr,
        )
        return 1

    # 登记之后再对外服务；atexit 保证 Ctrl+C / 正常退出都会摘掉登记，
    # 崩溃留下来的由下一次启动的 cleanup_stale_instances 回收。
    try:
        state = instance.write_instance(
            "backend", port, host=host, extra={"python": sys.executable}
        )
        log.info("实例登记 -> %s", state)
    except OSError as e:
        log.warning("实例登记失败（不影响启动，但多开检测会失效）: %s", e)

    atexit.register(instance.clear_instance, "backend", port)

    # 启动恢复：上次中断在执行中途的任务标记为"待确认"，不宣称完成、
    # 不自动重跑 —— 产物与备份原样保留，等用户决定
    try:
        recovered = design_svc.recover_interrupted(config_mod.PROJECT_ROOT)
        if recovered:
            log.warning("启动恢复：%d 个中断任务待确认: %s", len(recovered), recovered)
    except Exception as e:  # noqa: BLE001 — 恢复失败不能挡住后端启动
        log.exception("启动恢复失败: %s: %s", type(e).__name__, e)

    # 模型导入同理：上次跑一半的导入不能永远停在"进行中"（界面会一直转圈），
    # 也不能标成"失败"（那等于断言导入失败了，而实际只是不知道）。
    try:
        broken = model_orchestration.recover_interrupted_ops()
        if broken:
            log.warning("启动恢复：%d 个模型导入操作中断在未完成状态: %s",
                        len(broken), broken)
    except Exception as e:  # noqa: BLE001 — 同上
        log.exception("模型导入启动恢复失败: %s: %s", type(e).__name__, e)

    # 崩溃反馈：装异常钩子（未捕获异常/线程崩溃 → 自动落一份报告），
    # 并起心跳/看守线程。都在 serve_forever 之前，否则启动阶段崩了就没人管。
    try:
        if feedback.install_hooks({"source": "backend"}):
            log.info("崩溃反馈钩子已安装（数据目录=%s）", feedback.feedback_dir(create=False))
    except Exception as e:  # noqa: BLE001 — 反馈装不上不能挡住服务启动
        log.exception("安装崩溃反馈钩子失败: %s: %s", type(e).__name__, e)
    try:
        if start_feedback_watch():
            log.info("崩溃反馈看门狗已启动（间隔 %.0fs，卡死阈值 %ss）",
                     _WATCH_INTERVAL_S, CFG.get("feedback_hang_seconds"))
    except Exception as e:  # noqa: BLE001 — 同上
        log.exception("启动崩溃反馈看门狗失败: %s: %s", type(e).__name__, e)

    print(f"[ADS Agent] backend listening on http://{host}:{port}")
    print(f"[ADS Agent] model: {CFG['llm_model']}  api_key: {'已配置' if CFG.get('llm_api_key') else '未配置!'}")
    print(f"[ADS Agent] ADS tool server: http://{CFG['ads_host']}:{CFG['ads_port']}")
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ADS Agent] bye")
    finally:
        instance.clear_instance("backend", port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
