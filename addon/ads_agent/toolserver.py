"""Localhost tool executor for the ADS Agent.

Runs an HTTP server in a background thread. Every request is marshalled onto
the Qt main thread (via a queue pumped by a QTimer) because the
keysight.ads.de API must be used from the main thread of the ADS process.

**主线程只做必须做的事**：处理器可以调用 ``ctx.defer()``，把后续耗时工作
（典型例子：edatoolbox 的电路仿真 —— 它不碰 DE 数据库）交给后台线程，
这样一次 900s 的仿真不会把 ADS 界面冻住。见 JobContext 的说明。

Endpoints:
  POST /execute  {"name": ..., "args": {...}, "timeout": ...}  需请求头 X-Ads-Agent-Token
  GET  /health   开放，供探测使用；报告**所有**仍在运行的作业（含已转后台的仿真）

/health 的忙碌语义：一个作业从**入队**起就计入 ``busy``，直到它真正收尾
（成功 / 失败 / 后台线程结束）。多个作业同时运行时逐个记录，
一个结束不会把其它作业的状态清掉；请求已超时但后台线程还在跑的作业
会继续算作运行中，并额外带 ``timed_out`` 标记 —— 不谎报空闲。
"""

from __future__ import annotations

import configparser
import datetime
import itertools
import json
import os
import queue
import threading
import time
import sys
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import authbridge
import pathbridge

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))


def _identity() -> dict:
    """本实例的身份（放进 /health，详见 backend/instance.py）。

    拿不到共享模块时返回空 dict —— 对端会把"没有身份"当成无法确认归属，
    拒绝复用。**宁可报冲突，也不要闷头连错实例。**

    身份里额外带 ``ads_year`` / ``ads_dir``（来自 buildInfo.xml 的权威识别）：
    同一份插件安装被多个 ADS 版本共用时，对端据此区分"同版本多开"与
    "跨版本实例冲突"，不会误连到另一个版本的工具服务上。
    """
    try:
        inst = pathbridge.load_backend_module("instance.py", "instance")
    except Exception:  # noqa: BLE001
        return {}
    try:
        extra = {}
        import capability

        version = capability.snapshot().get("ads_version") or {}
        if version.get("year") is not None:
            extra["ads_year"] = int(version["year"])
        extra["ads_build"] = str(version.get("build") or "")
        hpeesof = (os.environ.get("HPEESOF_DIR") or "").strip().strip('"')
        if hpeesof:
            extra["ads_dir"] = os.path.normpath(hpeesof)
    except Exception:  # noqa: BLE001 — 版本信息拿不到不影响身份本身
        extra = {}
    try:
        return inst.identity(extra)
    except Exception:  # noqa: BLE001
        return {}


def _instance():
    """共享的 instance 模块（拿不到就返回 None，不影响服务本身）。"""
    try:
        return pathbridge.load_backend_module("instance.py", "instance")
    except Exception:  # noqa: BLE001
        return None


def _service_name() -> str:
    """/health 里自报的服务名。

    必须与 ``backend/instance.SERVICE_TOOLSERVER`` **完全相同** —— 探测方
    （``instance.evaluate``）正是拿这个字段判断"端口上坐的是不是本插件的工具
    服务"。这里曾经写成另一个键名（``server``），结果是：探测全部判成
    "其它程序占用端口"，而 /health 手测又一切正常 —— 2026-10-07 实机验收抓到。
    """
    inst = _instance()
    return getattr(inst, "SERVICE_TOOLSERVER", "ads_agent_toolserver")


def _protocol_version() -> int:
    """共享的协议版本。别写死：版本一变，探测方就该据此判为不一致。"""
    try:
        return int(pathbridge.load().PROTOCOL_VERSION)
    except Exception:  # noqa: BLE001
        return 1


def _gate_tool(name: str) -> dict:
    """单工具门禁决策（pump 主线程内调用；探测结果有缓存）。

    门禁只在**真实 ADS 进程内**生效：运行时能力检测的事实依据（keysight
    模块、Qt 绑定）只有在那里才存在。脱离 ADS 跑 toolserver 属于测试/开发
    配置，handlers 本身会在触碰 keysight API 时自然失败，无需门禁兜底。
    """
    try:
        import capability

        snap = capability.snapshot()
        if not snap.get("inside_ads"):
            return {"allowed": True, "code": "",
                    "reason": "非 ADS 进程（测试/开发模式），兼容门禁旁路"}
        return capability.gate_tool(name)
    except Exception as e:  # noqa: BLE001
        # 门禁本身坏了 = 无法证明允许 → 保守拒绝（fail-closed）
        return {"allowed": False, "code": "gate_error",
                "reason": f"兼容门禁不可用，按保守策略拒绝: {type(e).__name__}: {e}"}


def _compat_snapshot() -> dict:
    """能力快照（/health 用）。探测失败时返回明确的 unavailable 结构。"""
    try:
        import capability

        return capability.snapshot()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "capabilities": {},
                "ads_version": {"status": "unknown"}}


def _register_instance(host: str, port: int) -> None:
    """在 <数据根>/runtime 里留下"这个端口是本实例在监听"的记录。

    为什么工具服务也要登记：面板/后端靠它判断 8763 上坐着的是**哪一个** ADS
    实例。没有这条记录，就只能看到一句 OSError: [WinError 10048] 端口已占用 ——
    用户无法区分"我多开了一个 ADS"、"装了两份插件"和"别的软件占了这个端口"。
    """
    inst = _instance()
    if inst is None:
        return
    try:
        inst.cleanup_stale_instances()
        inst.write_instance("toolserver", int(port), host)
    except Exception as e:  # noqa: BLE001 — 登记失败不该拦住服务
        _log("WARN", f"写实例登记失败（不影响服务运行）: {type(e).__name__}: {e}")


def _clear_instance() -> None:
    inst = _instance()
    if inst is None:
        return
    try:
        inst.clear_instance("toolserver")
    except Exception as e:  # noqa: BLE001
        _log("WARN", f"清实例登记失败: {type(e).__name__}: {e}")


def _bind_conflict_detail(host: str, port: int, err: Exception) -> str:
    """端口绑不上时，把"被谁占了"说清楚，并给出可执行的下一步。

    三种情况要分开（用户看到的提示完全不同）：
      * **同一个安装的另一个 ADS 实例** —— 多开冲突，本版本仅支持单实例；
      * **另一份安装（别的目录/别人拷来的）** —— 归属不对，不能连；
      * **别的程序** 或 一个不回应 /health 的服务 —— 换端口。
    """
    who = f"端口 {host}:{port}"
    inst = _instance()
    if inst is None:
        return (f"{who} 无法监听（{type(err).__name__}: {err}）。"
                f"请在 config.ini 的 [ads] port 换一个空闲端口后重启 ADS。")
    try:
        verdict = inst.evaluate(inst.probe(f"http://{host}:{port}"), "toolserver")
    except Exception:  # noqa: BLE001
        verdict = {}

    ident = verdict.get("identity") or {}
    if verdict.get("reason") == "ok":
        return (
            f"{who} 已被**另一个 ADS 实例**的工具服务占用（pid={ident.get('pid')}）。"
            f"本版本仅支持单实例：请只保留一个 ADS 窗口，或换一个端口"
            f"（config.ini 的 [ads] port）后重启 ADS。"
        )
    if verdict.get("reason") == "cross_version_conflict":
        mine = _identity().get("ads_year") or "未知"
        return (
            f"{who} 已被**另一个 ADS 版本**的工具服务占用（对方 ADS "
            f"{ident.get('ads_year') or '版本未知'}，本实例 ADS {mine}）。"
            f"同一份插件安装同时只能服务一个 ADS 版本：请先退出另一个 ADS，"
            f"或给本版本换一个端口（config.ini 的 [ads] port）。"
        )
    if verdict.get("reason") in ("foreign_install", "no_install_id"):
        return (
            f"{who} 上是**另一份 ADS Agent 安装**的工具服务（对方 install_id="
            f"{ident.get('install_id') or '未知'}），不是当前这份。"
            f"请先退出那一个，或换一个端口。"
        )
    if verdict.get("conflict"):
        return (f"{who} 上的服务不是本插件：{verdict.get('detail')}。"
                f"请在 config.ini 的 [ads] port 换一个空闲端口后重启 ADS。")
    # 说不清是谁：至少把端口占用这件事讲明白
    return (f"{who} 无法监听：{verdict.get('detail') or f'{type(err).__name__}: {err}'}。"
            f"请在 config.ini 的 [ads] port 换一个空闲端口后重启 ADS。")



#: 旧接口：留空表示"用统一路径解析的结果"。测试会覆盖它们写到临时目录，
#: 所以这两个变量必须继续存在且优先于 paths 的计算结果。
_LOG_DIR = ""
_LOG_FILE = ""


def _log_dir() -> str:
    """日志目录每次现取：数据根目录可能被环境变量改掉，也可能还没建出来。"""
    if _LOG_DIR:
        return _LOG_DIR
    try:
        return pathbridge.load().logs_dir()
    except Exception:  # noqa: BLE001
        return os.path.join(_ROOT, "logs")


def _log_file() -> str:
    if _LOG_FILE:
        return _LOG_FILE
    return os.path.join(_log_dir(), "ads_toolserver.log")


def _log(level: str, msg: str) -> None:
    """写工具执行日志到 ``%LOCALAPPDATA%\\ADSAgent\\logs\\ads_toolserver.log``。

    ADS 是 GUI 进程，没有控制台，print/traceback 全都会丢；报错只能靠这里。
    注意：这里记的是工具名/参数/耗时/异常，**不记令牌**。
    """
    try:
        directory = _log_dir()
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {level} {msg}\n"
        if len(line) > 4000:
            line = line[:4000] + " …(截断)\n"
        with open(_log_file(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 — 写日志失败绝不能影响工具执行
        pass


def _redact(text) -> str:
    """日志脱敏：掩掉 Bearer 头与 token/api_key 类键值（不能依赖 backend.adslog）。"""
    import re
    s = str(text or "")
    if not s:
        return s
    s = re.sub(r"(Bearer\s+)\S+", lambda m: m.group(1) + "***", s,
               flags=re.IGNORECASE)
    s = re.sub(r"\b(token|api_key|apikey|authorization|password|secret)\b"
               r"\s*[:=]\s*[\"']?[^\s\"',;}]+",
               lambda m: m.group(1) + "=***", s, flags=re.IGNORECASE)
    return s
    s = re.sub(r"(Bearer\s+)\S+", r"***", s, flags=re.IGNORECASE)
    s = re.sub(r"(token|api_key|apikey|authorization|password|secret)\s*[:=]\s*[\"']?[^\s\"',;}]+",
               r"=***", s, flags=re.IGNORECASE)
    return s


def _load_config() -> configparser.ConfigParser:
    """读共享配置。

    路径与令牌模块保持一致（ads_auth.config_path()，支持 ADS_AGENT_CONFIG），
    保证"令牌"和"其它设置"一定来自同一个文件 —— 否则会出现两端读不同配置、
    令牌对不上的问题。
    """
    parser = configparser.ConfigParser()
    try:
        path = authbridge.auth().config_path()
    except Exception:  # noqa: BLE001
        try:
            path = os.path.join(pathbridge.load().data_root(), "config.ini")
        except Exception:  # noqa: BLE001
            path = os.path.join(_ROOT, "config.ini")
    if os.path.exists(path):
        parser.read(path, encoding="utf-8")
    return parser


def _setting(section, key, default):
    parser = _load_config()
    try:
        raw = parser.get(section, key).strip()
        return raw if raw else default
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def _setting_bool(section, key, default: bool) -> bool:
    raw = str(_setting(section, key, "true" if default else "false")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _sim_off_main_thread() -> bool:
    """是否允许把仿真阶段移出主线程（config.ini [agent] sim_off_main_thread）。"""
    return _setting_bool("agent", "sim_off_main_thread", True)


# ---------------------------------------------------------------------------
# main-thread execution pump
# ---------------------------------------------------------------------------

# 队列容量与单次泵预算（可用 config.ini [agent] 覆盖）：
#   tool_queue_capacity   排队作业数上限，满了直接拒绝（503），不再无限堆积
#   pump_max_jobs         Qt 主线程每次 pump() 最多执行的作业数 —— 防止大量
#                         排队作业一次性占死界面线程
#   pump_time_budget_ms   pump() 软时间预算（毫秒）：超时即停止"启动下一个
#                         作业"（正在执行的处理器不受影响，主线程无法抢占）
#   active_job_limit      活跃（执行中/已转后台，不含排队）作业数上限，
#                         满了同样拒绝（503），防止后台线程被作业淹没
#   gui_heartbeat_ms      GUI 心跳定时器间隔：独立 QTimer 只写时间戳，
#                         /health 以此判断"主线程是否疑似阻塞"
_DEFAULT_QUEUE_CAPACITY = 64
_DEFAULT_PUMP_MAX_JOBS = 16
_DEFAULT_PUMP_TIME_BUDGET_MS = 8.0    # pump 软时间预算（毫秒）
_DEFAULT_ACTIVE_JOB_LIMIT = 4         # 活跃（执行中/已转后台）作业上限
_DEFAULT_GUI_HEARTBEAT_MS = 200       # GUI 心跳定时器间隔


class BusyError(RuntimeError):
    """队列已满：调用方应稍后重试（HTTP 503）。"""


_jobs: "queue.Queue" = queue.Queue()
_server = None
_server_thread = None
_pump_timer = None  # QTimer, created on the Qt main thread

# 正在执行的作业：job_id -> {"name", "started", "timed_out", "client_id", "cancel_requested"}
#
# 为什么是"表"而不是"一个当前作业"：
#   * 后台仿真开始后主线程立刻返回（ctx.defer()），作业其实还在跑 ——
#     单个槽位会在主线程阶段结束时被清空，/health 于是谎报空闲；
#   * 多个作业可以同时在跑（多个后台仿真），一个结束不能把别人的状态一起清掉。
# 所以每个作业有自己的记录，只有它**真正收尾**（成功/失败/后台线程结束）才摘掉。
_active_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_seq = itertools.count(1)

# 已请求取消、还没被 pump 取走的作业（按调用方给的 client_id 登记）。
# 已经开跑的作业无法强杀 —— 只在作业记录上打 cancel_requested 标记，
# 由 /health 如实报告"已请求停止，正在收尾"。
_cancelled_ids: set = set()
_cancel_lock = threading.Lock()

# 入队锁：submit 的"容量检查 → 活跃上限检查 → 登记 → put"必须整体原子，
# 否则并发提交会在检查与 put 之间互相插队，超卖队列名额
_enqueue_lock = threading.Lock()

# 终态作业小历史（最新在尾部）：请求超时返回后，作业"其实执行到什么程度"
# 仍可经 job_snapshot() 查询。字段见 _job_settle。
_finished_jobs: "deque" = deque(maxlen=256)

# GUI 心跳（monotonic 时间戳；0.0 = 从未刷新）。pump() 每次执行刷新兜底，
# ensure_heartbeat_timer() 在有 Qt 事件循环时用独立 QTimer 高频刷新。
# 心跳停跳只说明"主线程疑似阻塞"，不能断言死锁。
_gui_heartbeat: float = 0.0
_heartbeat_timer = None  # QTimer，创建于 Qt 主线程

# 限频：pump 预算截断日志与 set_phase 同名阶段日志各自的最低间隔，防刷屏
_BUDGET_LOG_INTERVAL_S = 5.0
_PHASE_LOG_INTERVAL_S = 5.0
_last_budget_log: float = 0.0


def _queue_capacity() -> int:
    try:
        return max(1, int(_setting("agent", "tool_queue_capacity",
                                   str(_DEFAULT_QUEUE_CAPACITY))))
    except (TypeError, ValueError):
        return _DEFAULT_QUEUE_CAPACITY


def _pump_max_jobs() -> int:
    try:
        return max(1, int(_setting("agent", "pump_max_jobs",
                                   str(_DEFAULT_PUMP_MAX_JOBS))))
    except (TypeError, ValueError):
        return _DEFAULT_PUMP_MAX_JOBS


def _pump_time_budget_ms() -> float:
    """pump() 软时间预算（毫秒）。<=0 视为不限制（不建议）。"""
    try:
        value = float(_setting("agent", "pump_time_budget_ms",
                               str(_DEFAULT_PUMP_TIME_BUDGET_MS)))
    except (TypeError, ValueError):
        return _DEFAULT_PUMP_TIME_BUDGET_MS
    return value if value > 0 else 0.0


def _active_job_limit() -> int:
    """活跃（执行中/已转后台，不含纯排队）作业数上限。"""
    try:
        return max(1, int(_setting("agent", "active_job_limit",
                                   str(_DEFAULT_ACTIVE_JOB_LIMIT))))
    except (TypeError, ValueError):
        return _DEFAULT_ACTIVE_JOB_LIMIT


def _gui_heartbeat_ms() -> int:
    """GUI 心跳定时器间隔（毫秒），下限 20ms 防止误配成 0 打满 CPU。"""
    try:
        return max(20, int(_setting("agent", "gui_heartbeat_ms",
                                    str(_DEFAULT_GUI_HEARTBEAT_MS))))
    except (TypeError, ValueError):
        return _DEFAULT_GUI_HEARTBEAT_MS


def _touch_gui_heartbeat() -> None:
    """刷新 GUI 心跳时间戳（monotonic）。

    任何线程调用都安全：只写一个 float（GIL 下原子）。GUI 线程上的独立
    QTimer 高频刷新；pump() 每次执行也刷新兜底。心跳停跳只说明"主线程
    疑似阻塞"（长时间没回到事件循环），不能断言死锁。
    """
    global _gui_heartbeat
    _gui_heartbeat = time.monotonic()


def ensure_heartbeat_timer():
    """在 Qt 主线程创建独立的 GUI 心跳 QTimer（与 pump 定时器分开）。

    只在 QApplication/QCoreApplication 实例已存在时创建；后端进程与测试
    没有事件循环，返回 None（pump() 的兜底刷新仍然有效）。重复调用复用
    已有定时器（与 ensure_started 的 _pump_timer 同一套防重入风格，模块
    重载时最多泄漏一个空转定时器，与现状一致、无副作用）。
    """
    global _heartbeat_timer
    if _heartbeat_timer is not None:
        return _heartbeat_timer
    try:
        import qtcompat

        QtCore = qtcompat.QtCore()
        app = QtCore.QCoreApplication.instance()
        if app is None:
            return None
        timer = QtCore.QTimer()
        timer.timeout.connect(_touch_gui_heartbeat)
        interval = _gui_heartbeat_ms()
        timer.setInterval(interval)
        timer.start()
        _heartbeat_timer = timer
        _log("INFO", f"GUI 心跳定时器已启动（{interval}ms，独立于 pump）")
        return timer
    except Exception as e:  # noqa: BLE001 — 心跳失败绝不能影响工具服务
        _log("WARN", f"GUI 心跳定时器创建失败（仅影响主线程活性探测）: "
                     f"{type(e).__name__}: {e}")
        return None


def request_cancel(client_ids) -> dict:
    """请求取消作业（排队中的直接跳过；已开跑的只打标记，不强杀）。

    返回 {"cancelled": [...], "running": [...], "unknown": [...]}，调用方
    （后端/面板）据此向用户如实说明每种作业的下场。
    """
    ids = [str(c) for c in (client_ids or []) if c]
    out = {"cancelled": [], "running": [], "unknown": []}
    by_client = {}
    with _jobs_lock:
        for jid, rec in _active_jobs.items():
            if rec.get("client_id"):
                by_client.setdefault(str(rec["client_id"]), []).append((jid, rec))
        for cid in ids:
            hits = by_client.get(cid)
            if hits:
                for _jid, rec in hits:
                    rec["cancel_requested"] = True
                # queued=True 说明还在队列里没开跑 → 能干净跳过；
                # 否则已在执行 → 只能标记，等安全边界收尾
                if hits[0][1].get("queued"):
                    out["cancelled"].append(cid)
                    with _cancel_lock:
                        _cancelled_ids.add(cid)
                else:
                    out["running"].append(cid)
            else:
                _cancelled_ids.add(cid)
                out["cancelled"].append(cid)
    if out["running"]:
        _log("INFO", f"取消请求：已开跑不可中断 {out['running']}（标记 cancel_requested）")
        # 仿真作业是个例外：它的子进程是本插件自己 spawn 的，可以**真的**
        # 终止。这里主动踢一脚，而不是等 _simulate 的等待循环下一轮才
        # 发现取消标志 —— 用户按下停止后 hpeesofsim 应该马上开始收尾。
        _cancel_running_sims(out["running"])
    if out["cancelled"]:
        _log("INFO", f"取消请求：排队中已跳过 {out['cancelled']}")
    return out


def _cancel_running_sims(client_ids) -> None:
    """对正在跑的仿真子进程发起终止（只针对本插件 spawn 的 pid）。

    尽力而为：没有仿真在跑、或 ads_ops 尚未加载时静默跳过。这里**不**把
    结果写进回执 —— 回执里的 "running" 表示"已请求取消"，进程是否真的
    退出由 /health 与 job_snapshot() 的最终状态如实反映。
    """
    try:
        # 直接用已加载的 ads_ops，**不走 _get_ads_ops()**：那个会做热重载，
        # 重载后 _SIM_PROCS 是新的空表，我们就会"取消不到正在跑的进程"。
        ads_ops = sys.modules.get("ads_ops")
        cancel = getattr(ads_ops, "cancel_sim_process", None) if ads_ops else None
        if cancel is None:
            return
        for cid in client_ids:
            try:
                receipt = cancel(None, str(cid))
                if receipt.get("outcome") not in (None, "not_running"):
                    _log("INFO", f"仿真子进程已终止：job={cid} outcome={receipt['outcome']} "
                                 f"pid={receipt.get('pid')}")
            except Exception as e:  # noqa: BLE001 — 取消失败不能连累整个取消回执
                _log("WARN", f"终止仿真子进程失败：job={cid} {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — ads_ops 不可用（未安装/热重载中）
        _log("WARN", f"取消仿真跳过：{type(e).__name__}: {e}")


def _job_begin(name: str, client_id: str = "") -> int:
    jid = next(_job_seq)
    with _jobs_lock:
        _active_jobs[jid] = {"name": name, "started": time.time(), "timed_out": False,
                             "client_id": str(client_id or ""), "cancel_requested": False,
                             "queued": True, "phase": ""}
    return jid


def _job_end(jid: int) -> None:
    """仅摘除忙碌记录（兼容旧调用；正式收尾请用 _job_settle 写终态历史）。"""
    with _jobs_lock:
        _active_jobs.pop(jid, None)


def _job_mark_timeout(jid: int) -> None:
    with _jobs_lock:
        rec = _active_jobs.get(jid)
        if rec is not None:
            rec["timed_out"] = True


def _job_settle(jid: int, state: str, was_queued: bool = None) -> None:
    """终态收尾：从忙碌表摘除，并写一条终态历史（_finished_jobs）。

    state ∈ succeeded | failed | cancelled | timed_out。请求已超时
    （_job_mark_timeout 打过标记）而作业最终又跑完的，如实记为
    timed_out —— 调用方拿到的就是超时结果，不谎报成功。

    was_queued：pump 在"跳过执行"路径上需要显式传 True —— 取到作业的
    那一刻 queued 标志已翻转，不能据此谎称作业已开跑过。
    """
    with _jobs_lock:
        rec = _active_jobs.pop(jid, None)
        if rec is None:
            return
        if rec.get("timed_out") and state == "succeeded":
            state = "timed_out"
        _finished_jobs.append({
            "id": jid,
            "name": rec["name"],
            "state": state,
            "elapsed_s": round(time.time() - rec["started"], 3),
            "cancel_requested": bool(rec.get("cancel_requested")),
            "queued": bool(rec.get("queued")) if was_queued is None else bool(was_queued),
            "client_id": str(rec.get("client_id") or ""),
        })


def _phase_of(rec: dict) -> str:
    """作业阶段：处理器自报的细粒度阶段优先，否则给生命周期粗粒度阶段。

    排队时是 "queued"，开跑后处理器还没 set_phase 之前是 "running" ——
    这样 /health 的 jobs[].phase 既不会空着，也不会把排队作业误报成执行中。
    """
    phase = str(rec.get("phase") or "")
    if phase:
        return phase
    return "queued" if rec.get("queued") else "running"


def job_snapshot(client_id=None) -> list:
    """按调用方作业标识（client_id，即 /execute 的 job_id）查询作业真实状态。

    解决"请求已经超时（调用方拿到 504），作业其实还在执行 / 刚刚完成"
    时的真实状态查询。返回项的 state ∈ {queued, running, succeeded,
    failed, cancelled, timed_out}：活跃作业带 phase 与 timed_out 标记；
    终态记录含 elapsed_s / cancel_requested / queued。client_id 为 None
    时返回全部（活跃在前，终态按最新在前）。
    """
    cid = str(client_id) if client_id else None
    now = time.time()
    out: list = []
    with _jobs_lock:
        for jid, rec in sorted(_active_jobs.items()):
            if cid is not None and rec.get("client_id") != cid:
                continue
            out.append({
                "id": jid,
                "name": rec["name"],
                "state": "queued" if rec.get("queued") else "running",
                "phase": _phase_of(rec),
                "elapsed_s": round(now - rec["started"], 1),
                "timed_out": bool(rec.get("timed_out")),
                "cancel_requested": bool(rec.get("cancel_requested")),
                "queued": bool(rec.get("queued")),
                "client_id": str(rec.get("client_id") or ""),
            })
        for rec in reversed(_finished_jobs):
            if cid is not None and rec.get("client_id") != cid:
                continue
            out.append(dict(rec))
    return out


def active_jobs() -> list:
    """当前仍在执行的作业（含已超时但后台线程还没结束的），按开始顺序。"""
    now = time.time()
    with _jobs_lock:
        return [
            {
                "id": jid,
                "name": rec["name"],
                # state 与 job_snapshot() 同义：排队 / 执行中。phase 是处理器
                # 自报的阶段明细（可能仍是 "running"），两者不要混用。
                "state": "queued" if rec.get("queued") else "running",
                "elapsed_s": round(now - rec["started"], 1),
                "timed_out": bool(rec["timed_out"]),
                "cancel_requested": bool(rec.get("cancel_requested")),
                "queued": bool(rec.get("queued")),
                # 处理器还没自报阶段时按生命周期给出粗粒度阶段，
                # 避免 /health 里排队作业显示成 "running" 或空白
                "phase": _phase_of(rec),
            }
            for jid, rec in sorted(_active_jobs.items())
        ]

_ADS_OPS_PATH = os.path.join(_HERE, "ads_ops.py")
# ads_ops 依赖的本地纯计算模块：ads_ops 热重载前要先重载它们，
# 否则 reload(ads_ops) 里的 import 命中 sys.modules 缓存，改了也不生效
_ADS_OPS_DEPS = ("rf_audit", "model_ops", "netlist_check",
                 "geometry_policy", "schematic_rules")


def _get_ads_ops(starting_job_id=None):
    """取 ads_ops 模块，文件改动过就热重载。

    没有这个机制时，每次改 ads_ops.py 都得重启 ADS（要重新打开 workspace、
    库、原理图），排查一轮的成本极高；而 `import ads_ops` 命中 sys.modules 缓存，
    改了文件也不会生效，很容易出现「明明修好了但还是报老错」。
    """
    import importlib

    import ads_ops

    try:
        mtime = os.path.getmtime(_ADS_OPS_PATH)
    except OSError:
        return ads_ops

    def _fingerprint():
        parts = [mtime]
        for dep in _ADS_OPS_DEPS:
            try:
                parts.append(os.path.getmtime(os.path.join(_HERE, f"{dep}.py")))
            except OSError:
                parts.append(0)
        return "|".join(str(p) for p in parts)

    loaded = ads_ops.__dict__.get("_LOADED_MTIME")
    if loaded is None:  # 首次：记下当前版本即可
        ads_ops._LOADED_MTIME = _fingerprint()
        return ads_ops
    if loaded == _fingerprint():
        return ads_ops
    with _jobs_lock:
        running = [jid for jid, rec in _active_jobs.items()
                   if jid != starting_job_id and not rec.get("queued", False)]
    if running:
        # 有作业正在执行：推迟热重载，防止任务中途切换模块版本
        # （下个 pump 周期空闲了会自动再试）
        _log("INFO", f"ads_ops.py 有新版本，但当前 {len(running)} 个作业在执行，"
                     f"热重载推迟到空闲")
        return ads_ops

    env_backup = ads_ops.__dict__.get("_PY_ENV")
    try:
        # 依赖模块必须先于 ads_ops 重载，见 _ADS_OPS_DEPS
        for dep in _ADS_OPS_DEPS:
            dep_path = os.path.join(_HERE, f"{dep}.py")
            if dep in sys.modules:
                importlib.reload(sys.modules[dep])
                _log("INFO", f"{dep}.py 已热重载 (mtime={os.path.getmtime(dep_path)})")
        importlib.reload(ads_ops)
        if env_backup:
            ads_ops._PY_ENV = env_backup  # 保住 run_python 的常驻环境
            # 常驻环境里的助手闭包还是旧代码 —— 必须重绑，否则旧 connect/
            # wire/save 会绕过新门禁照画斜线（2026-09-28 实测踩过）。
            refresh = getattr(ads_ops, "install_env_helpers", None)
            if callable(refresh):
                refresh(env_backup)
        ads_ops._LOADED_MTIME = _fingerprint()
        _log("INFO", f"ads_ops.py 已热重载 (mtime={mtime})")
    except Exception as e:  # noqa: BLE001 — 重载失败就继续用旧模块
        _log("ERROR", f"ads_ops.py 热重载失败，继续使用旧模块: {type(e).__name__}: {e}")
    return ads_ops


class JobContext:
    """作业上下文：让处理器把耗时阶段交给后台线程。

    处理器契约（ads_ops 侧按鸭子类型使用，不 import 本模块）：

      * ``ctx.defer()``              声明"我稍后才完成"，pump() 不再自己收尾
      * ``ctx.finish(result)``       后台线程结束时回填结果
      * ``ctx.fail(message)``        后台线程失败时回填错误
      * ``ctx.note(message)``        记一条进度到 ads_toolserver.log
      * ``ctx.cancelled()``          是否已请求取消（线程安全，随时可查）
      * ``ctx.set_phase(name)``      更新作业阶段（/health jobs[].phase 可见）
      * ``ctx.sim_off_main_thread``  是否允许把仿真移出主线程

    为什么需要它：keysight.ads.de 必须在 Qt 主线程调用，而 pump() 是主线程里
    同步执行的 —— 处理器一旦阻塞，整个 ADS 界面就冻住。把"必须主线程"的部分
    （打开设计、生成网表）留在主线程，把"不碰 DE 数据库"的仿真交给后台线程，
    主线程就能立刻回到事件循环。

    收尾（无论走哪条路）都会把本作业从 ``_active_jobs`` 摘掉并写一条终态
    历史（``_finished_jobs``）—— /health 的"忙碌"状态因此始终等于"确实
    还在跑的作业集合"，收尾后的真实结局可经 job_snapshot() 查询。
    """

    __slots__ = ("job_id", "name", "box", "done", "deferred", "sim_off_main_thread",
                 "client_id", "_lock", "_settled", "_phase_log")

    def __init__(self, job_id: int, name: str, box: dict, done: threading.Event,
                 sim_off_main_thread: bool = False, client_id: str = ""):
        self.job_id = job_id
        self.name = name
        self.box = box
        self.done = done
        self.deferred = False
        self.sim_off_main_thread = sim_off_main_thread
        self.client_id = str(client_id or "")
        self._lock = threading.Lock()
        self._settled = False
        self._phase_log = ["", 0.0]   # [上次记录的阶段, 上次记录时间]，限频用

    def defer(self) -> None:
        self.deferred = True

    def cancelled(self) -> bool:
        """是否已请求取消（/cancel 对执行中作业只打标记、不强杀）。

        处理器应在安全边界（例如仿真主循环的迭代间隙）自查并尽早收尾；
        返回 True 时由处理器自己决定如何结束 —— 作业的最终状态如实反映
        处理器的实际结局，不会被谎报成"已中断"。线程安全。
        """
        with _jobs_lock:
            rec = _active_jobs.get(self.job_id)
            if rec is not None and rec.get("cancel_requested"):
                return True
        if self.client_id:
            with _cancel_lock:
                return self.client_id in _cancelled_ids
        return False

    def set_phase(self, phase: str) -> None:
        """更新作业阶段（线程安全）；/health 的 jobs[].phase 可见。

        阶段变化立即记一条 INFO 日志；同名阶段重复上报按
        _PHASE_LOG_INTERVAL_S 限频，防止长循环里反复上报刷屏。
        """
        phase = str(phase or "").strip()
        if not phase:
            return
        now = time.monotonic()
        with self._lock:
            should_log = (phase != self._phase_log[0]
                          or now - self._phase_log[1] >= _PHASE_LOG_INTERVAL_S)
            if should_log:
                self._phase_log[0] = phase
                self._phase_log[1] = now
        with _jobs_lock:
            rec = _active_jobs.get(self.job_id)
            if rec is not None:
                rec["phase"] = phase
        if should_log:
            _log("INFO", f"{self.name} 阶段: {phase}")

    def finish(self, result) -> None:
        self._settle("result", result)

    def fail(self, message) -> None:
        self._settle("error", str(message))

    def note(self, message: str) -> None:
        """记一条进度（后台线程也可调用）。"""
        _log("INFO", f"{self.name} {message}")

    def _settle(self, key: str, value) -> None:
        with self._lock:
            if self._settled:      # 只认第一次收尾（例如请求已超时后台才结束）
                return
            self._settled = True
        self.box[key] = value
        # 真正收尾了才从忙碌表摘掉，并写终态历史
        # （请求超时后后台才跑完的，如实记 timed_out，不谎报成功）
        _job_settle(self.job_id, "failed" if key == "error" else "succeeded")
        self.done.set()


def submit(name: str, args: dict, timeout: float = 300.0,
           client_id: str = "") -> dict:
    """Called from the HTTP thread; blocks until the main thread executes.

    作业从**入队那一刻**就登记为"运行中"，直到真正收尾才摘掉 ——
    这中间包括主线程阶段与后台仿真阶段（见 JobContext 的说明）。

    超时语义分两类，错误信息里明确区分：
      * 排队超时 —— 截止时间到了还没轮到执行（主线程忙/前面作业太长），
        pump() 取到时直接跳过，返回"排队超时，未执行"；
      * 执行超时 —— 已开跑但没在 deadline 内完成，请求方拿到 504；
        后台线程无法被强制终止，作业仍留在 ``_active_jobs`` 里并打上
        ``timed_out`` 标记：/health 继续如实报告"还在跑"。

    队列满或活跃作业达到上限时抛 ``BusyError``（HTTP 503），调用方稍后
    重试。容量检查、活跃上限检查、登记与入队在 ``_enqueue_lock`` 内整体
    原子完成 —— 分开的话并发提交会在检查与 put 之间互相插队，超卖名额。
    """
    done = threading.Event()
    box: dict = {}
    # 总预算 deadline = now + timeout；HTTP 等待多给 2s 宽限，让 pump 能在
    # 放弃之前把"排队超时（未执行）"如实回填给调用方，而不是一律 504
    deadline = time.time() + max(1.0, float(timeout))
    cid = str(client_id or "")
    with _enqueue_lock:
        if _jobs.qsize() >= _queue_capacity():
            raise BusyError(
                f"ADS 工具队列已满（{_queue_capacity()}），请稍后重试；"
                f"当前可能有仿真或建图正在执行（见 /health）"
            )
        # 活跃上限：执行中 + 已转后台（deferred）的作业数（不含纯排队）。
        # deferred 作业后台线程可能要跑几分钟，不设上限会把后台线程淹没。
        with _jobs_lock:
            running = sum(1 for rec in _active_jobs.values()
                          if not rec.get("queued"))
            limit = _active_job_limit()
        if running >= limit:
            raise BusyError(
                f"ADS 活跃作业已达上限（{running}/{limit} 个执行中或后台作业），"
                f"请稍后重试；详情见 /health"
            )
        jid = _job_begin(name, cid)
        _jobs.put((jid, name, args, box, done, deadline, cid))
    if not done.wait(float(timeout) + 2.0):
        _job_mark_timeout(jid)
        raise TimeoutError(f"工具 {name} 在 {timeout:.0f}s 内未完成")
    if "error" in box:
        if box.get("kind") == "precondition":
            raise PreconditionFailure(box["error"])
        raise RuntimeError(box["error"])
    if "result" not in box:
        # 处理器既没成功也没报错就收尾了（例如异常处理器自己抛异常、把真实
        # 原因吞掉）。绝不能让它退化成 KeyError：那会把"缺文件/导入失败"
        # 伪装成"服务崩溃"，让人误判方向去重启 ADS。
        raise RuntimeError(
            "工具执行结束但未返回结果（疑似处理器异常被吞，"
            "见 %LOCALAPPDATA%\\ADSAgent\\logs\\ads_toolserver.log）"
        )
    return box["result"]


class PreconditionFailure(RuntimeError):
    """HTTP 层据此返回 400：请求本身没错，但当前状态不满足前置条件。"""


def _ms(since: float) -> str:
    return f"{int((time.perf_counter() - since) * 1000)}ms"


def _log_budget_truncated(remaining: int) -> None:
    """pump 时间预算截断日志（限频，防止长队列每个 tick 刷一条）。"""
    global _last_budget_log
    now = time.monotonic()
    if now - _last_budget_log < _BUDGET_LOG_INTERVAL_S:
        return
    _last_budget_log = now
    _log("INFO", f"pump 时间预算用尽，本 tick 停止启动新作业"
                 f"（队列剩余 {remaining} 个，执行中的处理器不受影响）")


def pump() -> None:
    """Execute queued jobs; must run on the Qt main thread.

    每次最多执行 ``pump_max_jobs`` 个作业（时间预算保护界面线程），
    处理器若调用 ctx.defer()，本函数立刻返回（主线程随即回到事件循环），
    作业由后台线程通过 ctx.finish()/ctx.fail() 收尾 —— 此时**不摘掉**
    忙碌记录，/health 会继续报告该作业在运行。

    取消/过期的作业在取出的那一刻直接跳过执行并收尾：
      * 已取消（client_id 在 _cancelled_ids）—— "已取消，未执行"；
      * 排队超时（now > deadline）—— "排队超时，未执行"。

    软时间预算 ``pump_time_budget_ms``：本 tick 从开始计时，预算用尽就
    停止"启动下一个作业"（剩余留在队列里）。它**绝不会中断正在执行的
    处理器** —— ADS 主线程上的处理器无法被安全抢占；预算只决定要不要
    再拿一个新的。作业数上限 ``pump_max_jobs`` 仍然同时生效。
    """
    _touch_gui_heartbeat()      # pump 在跑 = 主线程活着（心跳兜底刷新）
    executed = 0
    budget_ms = _pump_time_budget_ms()
    tick_t0 = time.perf_counter()
    while executed < _pump_max_jobs():
        # 软时间预算检查（放在取作业之前：只挡"启动下一个"，不碰在跑的）。
        # executed 为 0 时不截断：预算再小也必须让本 tick 至少推进一个作业，
        # 否则队列会永久饿死（misconfig / 极小预算 = 所有请求等到超时）。
        if (executed > 0 and budget_ms > 0
                and (time.perf_counter() - tick_t0) * 1000.0 >= budget_ms):
            remaining = _jobs.qsize()
            if remaining:
                _log_budget_truncated(remaining)
            break
        try:
            jid, name, args, box, done, deadline, client_id = _jobs.get_nowait()
        except queue.Empty:
            return
        # 作业开跑：queued 标志翻转，取消判定从这里起只能"标记"不能"跳过"。
        # 翻转前先快照"取到时还在排队"，跳过路径的终态历史靠它如实记录。
        with _jobs_lock:
            rec = _active_jobs.get(jid)
            was_queued = bool(rec.get("queued")) if rec is not None else True
            if rec is not None:
                rec["queued"] = False
                rec["phase"] = "running"
            was_cancelled = (rec.get("cancel_requested") if rec is not None
                             else False)
        if not was_cancelled and client_id:
            with _cancel_lock:
                was_cancelled = client_id in _cancelled_ids
                if was_cancelled:
                    _cancelled_ids.discard(client_id)
        if was_cancelled:
            box["error"] = f"工具 {name} 已被用户取消（排队中跳过，未执行）"
            _log("INFO", f"{name} 已取消（client_id={client_id}），跳过执行")
            _job_settle(jid, "cancelled", was_queued=was_queued)
            done.set()
            executed += 1
            continue
        if time.time() > deadline:
            box["error"] = (f"工具 {name} 排队超时：截止时间已过仍未开始执行"
                            f"（主线程忙或前面的作业耗时过长），本次未执行")
            _log("WARN", f"{name} 排队超时（client_id={client_id}），跳过执行")
            _job_settle(jid, "timed_out", was_queued=was_queued)
            done.set()
            executed += 1
            continue
        ctx = JobContext(jid, name, box, done,
                         sim_off_main_thread=_sim_off_main_thread(),
                         client_id=client_id)
        _t0 = time.perf_counter()
        # 预绑定：_get_ads_ops() 自己就可能抛（例如安装不完整、依赖模块缺失）。
        # 若不留初值，下面 except 分支里的 getattr(ads_ops, ...) 会抛
        # UnboundLocalError，把真实异常顶掉，box 里既没有 error 也没有 result，
        # 调用方只能看到一个毫无意义的 KeyError。
        ads_ops = None
        try:
            # This request has not entered its handler yet. Queued requests
            # and this starting request must not prevent an idle reload;
            # another running/deferred job still blocks it.
            ads_ops = _get_ads_ops(starting_job_id=jid)

            handler = ads_ops.DISPATCH.get(name)
            if handler is None:
                box["error"] = f"未知工具: {name}"
                _log("WARN", f"{name} 未知工具")
            else:
                # 跨版本门禁：版本证据 + 运行时能力共同裁决（见 capability.py）。
                # 只在确认允许后才进入处理器 —— 不允许时"未执行、未写入"。
                gate = _gate_tool(name)
                if not gate.get("allowed"):
                    box["error"] = (f"工具 {name} 在当前 ADS 版本上不可用"
                                    f"[{gate.get('code') or 'denied'}]：{gate.get('reason')}")
                    _log("WARN", f"{name} 被兼容门禁拒绝 [{gate.get('code')}]: "
                                 f"{gate.get('reason')}")
                    _job_settle(jid, "failed")
                    done.set()
                    executed += 1
                    continue
                result = handler(args or {}, ctx)
                if ctx.deferred:
                    # 主线程已释放，完成时由后台线程收尾；作业仍是"运行中"
                    _log("INFO", f"{name} 主线程阶段结束({_ms(_t0)})，已转后台执行")
                else:
                    box["result"] = result
                    # run_python 内部失败会带 ok=False，属于「模型写错代码」，
                    # 是最需要看到的一类，单独记录
                    if isinstance(result, dict) and result.get("ok") is False:
                        _log("ERROR", f"{name} 执行失败: "
                                      f"{str(result.get('stdout', ''))[:1500]}")
                    else:
                        _log("INFO", f"{name} 完成({_ms(_t0)})")
        except BaseException as e:  # 处理器异常都要收尾，不能让 Qt 主线程泵退出
            _log("ERROR", f"{name} 异常({_ms(_t0)}): {type(e).__name__}: {e}\n"
                          f"{traceback.format_exc()[-2500:]}")
            # 处理器抛异常时它没能接管收尾，必须由 pump 结束这次请求，
            # 否则 HTTP 线程会一直等到超时。fail() 会把作业标记为已收尾，
            # 后台线程稍后再 finish() 也不会覆盖这个错误。
            ctx.deferred = False
            # 前置条件（未打开工作区、最近记录失效）是调用方可直接改正的状态
            # 问题，不是服务崩溃：保留类别，让 HTTP 层回 4xx 而不是 500。
            # ads_ops 为 None（导入就失败）时 getattr 返回 ()，不会误判。
            if isinstance(e, getattr(ads_ops, "PreconditionError", ())):
                box["kind"] = "precondition"
            try:
                ctx.fail(f"{type(e).__name__}: {e}")
            except BaseException:  # fail() 自己再炸也要把错误写进 box
                box["error"] = f"{type(e).__name__}: {e}"
        finally:
            # 只有"本次作业真的结束了"才摘掉忙碌记录；
            # defer 过的作业交给后台线程收尾（_settle 里摘），
            # 否则一个作业结束会把其它仍在跑的作业的状态一起清空。
            if not ctx.deferred:
                # 兜底：既没有 result 也没有 error 说明上面有东西被吞了。
                # 必须补一条 error，否则 submit() 拿到空 box，作业还会被记成
                # succeeded —— 真实原因就此彻底消失。
                if "error" not in box and "result" not in box:
                    box["error"] = (f"工具 {name} 结束但未产生结果或错误"
                                    "（处理器异常被吞，见 ads_toolserver.log）")
                _job_settle(jid, "failed" if "error" in box else "succeeded")
                done.set()
        executed += 1


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            jobs = active_jobs()
            head = jobs[0] if jobs else None
            # GUI 心跳：pump()/独立 QTimer 每次都会刷新时间戳。age 为 None
            # 表示从未刷新（后端进程/测试没有主线程泵）。心跳陈旧只说明
            # "主线程疑似阻塞"（长时间没回到事件循环），不能断言死锁。
            hb = _gui_heartbeat
            hb_interval_ms = _gui_heartbeat_ms()
            hb_age_ms = (round((time.monotonic() - hb) * 1000.0) if hb else None)
            heartbeat_stale = bool(hb_age_ms is not None
                                   and hb_age_ms > max(2000, 4 * hb_interval_ms))
            # 开放接口：只报告"忙不忙"和作业名/时长，不含任何令牌信息
            self._json({
                "status": "ok",
                # service 是**规范字段**（探测方读它）；server 是历史别名，留着
                # 给人和旧脚本看，两者必须同值。
                "service": _service_name(),
                "server": _service_name(),
                "protocol": _protocol_version(),
                "auth_required": True,
                "sim_off_main_thread": _sim_off_main_thread(),
                # busy 在**整个作业期间**都为真（含已转后台的仿真），
                # 只有作业真正收尾（成功/失败/后台线程结束）才回到 null
                "busy": head["name"] if head else None,
                "elapsed_s": head["elapsed_s"] if head else 0,
                "busy_count": len(jobs),
                "jobs": jobs,
                "queued": _jobs.qsize(),
                "queue_capacity": _queue_capacity(),
                # 请求已超时但后台线程仍在跑的作业数（界面据此说明"还在收尾"）
                "timed_out_count": sum(1 for j in jobs if j["timed_out"]),
                "cancel_requested_count": sum(1 for j in jobs if j.get("cancel_requested")),
                # 正在按取消意图收尾的**执行中**作业数（排队待跳过的不算
                # "正在取消"，它们下一拍就会被直接跳过；与
                # cancel_requested_count 的差值即"排队等跳过"的数量）
                "cancelling_count": sum(1 for j in jobs if j.get("cancel_requested")
                                        and not j.get("queued")),
                # 活跃作业上限（与 busy_count 对照即可看出是否接近饱和）
                "active_limit": _active_job_limit(),
                # 终态作业小历史长度：job_snapshot() 可查收尾后的真实结局
                "finished_recent": len(_finished_jobs),
                "gui_heartbeat_age_ms": hb_age_ms,
                "heartbeat_stale": heartbeat_stale,
                # 身份：让探测方能确认"这是不是我这份安装的工具服务"。
                # 面板/后端据此识别"另一个 ADS Agent 安装的 toolserver 占着端口"
                # 的情况，而不是连上去才发现令牌对不上。见 backend/instance.py
                "identity": _identity(),
                # 跨版本兼容快照：版本识别 + 能力检测 + 门禁后的工具可用性。
                # 后端据此过滤工具列表、面板据此显示"未实机验证"横幅。
                "compat": _compat_snapshot(),
            })
        else:
            self._json({"error": "not found"}, 404)

    def _drain_body(self, limit: int = 1_000_000) -> None:
        """丢弃未读请求体，避免同一条 keep-alive 连接的下次读取错位。"""
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

    def do_POST(self):
        # 先鉴权：未知路径也返回 401，避免用 404 探测有哪些接口
        if not authbridge.check(self.headers.get(authbridge.header_name(), "")):
            # 不回显期望值，也不记录对方发来的内容
            _log("WARN", "拒绝未授权请求 POST /execute")
            self._drain_body()
            self._json({"error": "unauthorized"}, 401)
            return
        if self.path not in ("/execute", "/cancel"):
            # 同样要排空请求体，否则同一条 keep-alive 连接的下次读取会错位
            self._drain_body()
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length > 8_000_000:
            # 超大请求：不读半截复用连接 —— 排空可排的部分后直接断开
            self._drain_body(limit=1_000_000)
            self.close_connection = True
            self._json({"error": f"请求体过大（>{8_000_000} 字节），已拒绝"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._drain_body()
            self._json({"error": "bad request"}, 400)
            return
        if not isinstance(body, dict):
            self._drain_body()
            self._json({"error": "request body must be a JSON object"}, 400)
            return

        if self.path == "/cancel":
            # 取消作业：排队中的跳过，已开跑的只打标记（线程无法强杀）
            ids = body.get("job_ids") or ([body.get("job_id")] if body.get("job_id") else [])
            if not isinstance(ids, list):
                self._json({"error": "job_ids 必须是数组"}, 400)
                return
            out = request_cancel(ids)
            self._json({"ok": True, **out})
            return

        name = str(body.get("name", ""))
        args = body.get("args") or {}
        if not isinstance(args, dict):
            self._json({"error": "args 必须是对象"}, 400)
            return
        try:
            timeout = float(body.get("timeout") or 600)
        except (TypeError, ValueError):
            self._json({"error": "timeout 必须是数字"}, 400)
            return
        if not (0 < timeout <= 7200):
            self._json({"error": "timeout 超出允许范围 (0, 7200]s"}, 400)
            return
        client_id = str(body.get("job_id") or "")

        _log("INFO", f"收到请求 {name}  timeout={timeout}s  job_id={client_id or '-'}  "
                     f"参数={_redact(json.dumps(args, ensure_ascii=False, default=str))[:600]}")
        try:
            result = submit(name, args, timeout=timeout, client_id=client_id)
            self._json(result)
        except BusyError as e:
            _log("WARN", f"{name} 拒绝（忙碌）: {e}")
            self._json({"error": str(e), "kind": "busy", "job_id": client_id}, 503)
        except TimeoutError as e:
            _log("ERROR", f"{name} 执行超时 >{timeout}s")
            # job_id 原样带回：调用方可以稍后用 /health 查它是否仍在收尾
            self._json({"error": str(e), "kind": "exec_timeout",
                        "job_id": client_id}, 504)
        except PreconditionFailure as e:
            _log("WARN", f"{name} 前置条件未满足: {e}")
            self._json({"error": str(e), "kind": "precondition"}, 400)
        except RuntimeError as e:
            _log("ERROR", f"{name} 运行错误: {e}")
            self._json({"error": str(e)}, 500)
        except Exception as e:  # noqa: BLE001
            _log("ERROR", f"{name} 未预期异常: {type(e).__name__}: {e}\n"
                          f"{traceback.format_exc()[-2500:]}")
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def ensure_started() -> str:
    """Start the tool server once; also (re)arm the main-thread pump timer."""
    global _server, _server_thread, _pump_timer

    # 确保令牌存在且与后端一致（缺失或仍是公开默认值时在此生成）
    try:
        authbridge.token()
    except Exception as e:  # noqa: BLE001 — 令牌初始化失败也要让服务起来，鉴权会拒绝
        _log("ERROR", f"初始化回环令牌失败: {type(e).__name__}: {e}")

    if _pump_timer is None:
        import qtcompat
        QtCore = qtcompat.QtCore()

        _pump_timer = QtCore.QTimer()
        _pump_timer.timeout.connect(pump)
        _pump_timer.start(50)  # ms

    # GUI 心跳：独立于 pump 的 QTimer（同样在 Qt 主线程创建），只写时间戳。
    # pump 定时器在 toolserver.py 内创建（ensure_started），心跳就近挂接；
    # 无 Qt 事件循环时（后端进程/测试）返回 None，pump 兜底刷新仍有效。
    ensure_heartbeat_timer()

    if _server is None:
        host = _setting("ads", "host", "127.0.0.1")
        port = int(_setting("ads", "port", "8761"))
        try:
            _server = ThreadingHTTPServer((host, port), _Handler)
        except OSError as e:
            # 裸 OSError([WinError 10048]) 对用户毫无信息量：说不出是多开了一个
            # ADS、装了两份插件，还是别的软件占了端口。这里翻译成人话再抛。
            detail = _bind_conflict_detail(host, port, e)
            _log("ERROR", f"工具服务启动失败: {detail}")
            raise RuntimeError(detail) from e
        _server.daemon_threads = True
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()
        _register_instance(host, port)
        _log("INFO", f"工具服务已监听 {host}:{port}")

    return f"http://{_setting('ads', 'host', '127.0.0.1')}:{_setting('ads', 'port', '8761')}"


def shutdown() -> None:
    global _server, _server_thread, _pump_timer, _heartbeat_timer
    if _pump_timer is not None:
        _pump_timer.stop()
        _pump_timer.deleteLater()
        _pump_timer = None
    if _heartbeat_timer is not None:
        try:
            _heartbeat_timer.stop()
            _heartbeat_timer.deleteLater()
        except Exception:  # noqa: BLE001 — 关停路径不抛异常
            pass
        _heartbeat_timer = None
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
        _server_thread = None
        # 正常退出必须清掉登记：否则下一次启动会看到一个 pid 已死的陈旧记录，
        # 只能靠 pid 存活兜底清理（能清，但没必要留这个坑）
        _clear_instance()
        _log("INFO", "工具服务已停止")
