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

设计闭环的实测值与达标结论一律由 design_metrics 从真实数据算出，
LLM 只能提供设计与指标定义 —— 见 design_service 的说明。

鉴权说明：/chat 配合 allow_python 等于"在 ADS 进程里执行任意代码"，所以除
/health 之外的接口一律要求令牌。令牌由 ads_auth.py 统一生成/读取，校验失败只
返回一句 {"error": "unauthorized"}，绝不回显或记录令牌本身。

日志始终写入 <project>/logs/backend.log（不依赖 stdout 重定向）。

Run:  python backend/server.py
"""

from __future__ import annotations

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
