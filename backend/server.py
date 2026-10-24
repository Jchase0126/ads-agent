"""ADS Agent backend HTTP server (stdlib only, no pip install needed).

Endpoints (除 /health 外都需要请求头 X-Ads-Agent-Token，见 ads_auth.py):
  GET  /health  -> {"status": "ok", ...}   ← 开放，供启动探测使用
  GET  /logs    -> {"lines": [...]}  最近 N 行日志（?n=200），排查用
  GET  /config  -> 当前 LLM 配置（脱敏）
  GET  /tools   -> 可用工具名
  GET  /design/job?id=<job_id> -> 设计任务（结果页）的完整记录
  POST /config  -> 保存 LLM 设置
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
import llm as llm_mod
import model_store
import model_tools
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
# 取消事件：import_op -> threading.Event（导入结束即移除）
_MODEL_OP_CANCELS: dict = {}
_MODEL_OP_LOCK = threading.Lock()
# 同一工作区的导入必须串行：并发挂接会产生重复 lib.defs 条目
_MODEL_IMPORT_LOCKS: dict = {}
_MODEL_IMPORT_LOCKS_GUARD = threading.Lock()


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


def _workspace_import_lock(workspace: str) -> threading.Lock:
    """取某个工作区的导入锁。

    同一工作区的挂接操作必须串行：两个导入并发改同一份 lib.defs 会写出
    重复条目，而重复挂接是用户明确要求避免的（幂等）。
    """
    key = os.path.normcase(os.path.normpath(workspace))
    with _MODEL_IMPORT_LOCKS_GUARD:
        lock = _MODEL_IMPORT_LOCKS.get(key)
        if lock is None:
            if len(_MODEL_IMPORT_LOCKS) > 64:      # 防御：换过很多工作区也别无限堆积
                for k in list(_MODEL_IMPORT_LOCKS)[:32]:
                    _MODEL_IMPORT_LOCKS.pop(k, None)
            lock = threading.Lock()
            _MODEL_IMPORT_LOCKS[key] = lock
        return lock


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


def _config_status() -> dict:
    return {
        "base_url": CFG["llm_base_url"],
        "model": CFG["llm_model"],
        "models": CFG["llm_models"],
        "has_key": bool(CFG.get("llm_api_key")),
        "api_key_hint": config_mod.key_hint(CFG.get("llm_api_key", "")),
        # 面板用它在状态栏如实描述"仿真期间界面会不会卡"
        "sim_off_main_thread": CFG["sim_off_main_thread"],
        "sim_timeout": CFG["sim_timeout"],
    }


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
            self._drain_body()
            if reply:
                self._send_json({"error": "bad request body"}, 400)
            return None
        if not isinstance(data, dict):
            self._drain_body()
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
            body = self._read_json() or {}
            package_id = str(body.get("package_id") or "").strip()
            if not package_id:
                self._send_json({"error": "缺少 package_id"}, 400)
                return
            import uuid as _uuid
            op_id = _uuid.uuid4().hex
            cancel_event = threading.Event()
            with _MODEL_OP_LOCK:
                _MODEL_OP_CANCELS[op_id] = cancel_event

            # 导入是长任务（解压 + 挂接 + 建索引，可能几十秒到几分钟），
            # 不能占住这个 HTTP 线程去做 —— 交给后台线程，响应由它直接写。
            # 面板的取消走 /models/cancel（按 op_id 找 cancel_event）。
            def _worker():
                try:
                    workspace = model_tools.current_workspace(CFG)
                    # 同一工作区串行导入，避免并发挂接写出重复条目
                    with _workspace_import_lock(workspace):
                        res = model_tools.import_model_package(
                            CFG, body, cancel_event=cancel_event)
                    if cancel_event.is_set() and not res.get("cancelled"):
                        res = {**res, "cancelled": True,
                               "message": "已请求取消；已完成的前置步骤保留，"
                                          "取消后不会继续进入仿真。"}
                    self._send_json({"ok": True, "op_id": op_id, **res})
                except model_tools.ModelToolError as e:
                    self._send_json({"error": str(e), "op_id": op_id}, 400)
                except tools_mod.AdsToolError as e:
                    self._send_json({"error": str(e), "op_id": op_id}, 502)
                except Exception as e:  # noqa: BLE001
                    log.exception("模型包导入失败: %s: %s", type(e).__name__, e)
                    self._send_json({"error": f"{type(e).__name__}: {e}",
                                     "op_id": op_id}, 500)
                finally:
                    with _MODEL_OP_LOCK:
                        _MODEL_OP_CANCELS.pop(op_id, None)

            threading.Thread(target=_worker, daemon=True).start()
            self.close_connection = True
            self._send_json({"ok": True, "op_id": op_id, "accepted": True,
                             "message": "导入已开始（后台执行）。"
                                        "可通过 /models/cancel 取消，"
                                        "或用 /models/packages 查看进度状态。"})
            return

        if method == "POST" and action == "cancel":
            body = self._read_json() or {}
            op_id = str(body.get("op_id") or "").strip()
            if not op_id:
                self._send_json({"error": "缺少 op_id"}, 400)
                return
            with _MODEL_OP_LOCK:
                event = _MODEL_OP_CANCELS.get(op_id)
            if event is None:
                self._send_json({"ok": False, "known": False,
                                 "message": "该导入操作已结束或不存在"})
                return
            event.set()
            self._send_json({"ok": True, "known": True})
            return

        self._send_json({"error": f"未知的模型接口 {method} /models/{action}"}, 404)

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
            self._drain_body()
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

            self._send_json({
                "ok": True,
                "workspace": workspace,
                "package": model_tools._record_view(record, include_models=False),
                "reused": was_reused,
                "scan_note": scan_note,
                "message": ("已保存到当前工作区并完成包结构检查。"
                            "**未解压、未加载套件、未执行包内脚本** —— "
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
        else:
            self._send_json({"error": "not found"}, 404)

    # ------------------------------------------------------------------
    def do_POST(self):
        # 所有 POST 都是可操作接口（改配置 / 驱动 Agent），一律先鉴权
        if not self._authorized():
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
            log.info("轮次 %s 收到取消请求", turn_id)
            self._send_json({"ok": True, "known": True})
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
                )
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"保存配置失败: {type(e).__name__}: {e}"}, 500)
                return
            CFG.update(config_mod.load())  # apply immediately
            self._send_json({"ok": True, **status})
            return

        if self.path == "/test_connection":
            body = self._read_json(limit=200_000)
            if body is None:
                return
            base_url = (body.get("base_url") or CFG["llm_base_url"]).strip()
            api_key = (body.get("api_key") or CFG.get("llm_api_key") or "").strip()
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

        turn_id = str(body.get("turn_id") or "").strip() or _new_turn_id()
        events: queue.Queue = queue.Queue()
        turn = agent.Turn(CFG, messages, allow_python=allow_python, model=model,
                          turn_id=turn_id)
        _register_turn(turn)
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
    import datetime
    import os as _os
    _PID = _os.getpid()
    _STARTED_AT = datetime.datetime.now().replace(microsecond=0).isoformat()

    # 启动恢复：上次中断在执行中途的任务标记为"待确认"，不宣称完成、
    # 不自动重跑 —— 产物与备份原样保留，等用户决定
    try:
        recovered = design_svc.recover_interrupted(config_mod.PROJECT_ROOT)
        if recovered:
            log.warning("启动恢复：%d 个中断任务待确认: %s", len(recovered), recovered)
    except Exception as e:  # noqa: BLE001 — 恢复失败不能挡住后端启动
        log.exception("启动恢复失败: %s: %s", type(e).__name__, e)

    host, port = CFG["backend_host"], CFG["backend_port"]
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[ADS Agent] backend listening on http://{host}:{port}")
    print(f"[ADS Agent] model: {CFG['llm_model']}  api_key: {'已配置' if CFG.get('llm_api_key') else '未配置!'}")
    print(f"[ADS Agent] ADS tool server: http://{CFG['ads_host']}:{CFG['ads_port']}")
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ADS Agent] bye")


if __name__ == "__main__":
    main()
