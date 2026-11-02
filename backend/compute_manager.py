# -*- coding: utf-8 -*-
"""独立计算作业管理器：子进程生命周期、排队、取消与回收。

设计要点（接口契约 v1；与 lead 预写接入代码对齐）
--------------------------------------------------
- 计算 worker 是**后端进程的子进程**（backend/compute_worker.py），完全不经过
  ADS GUI；进程隔离仅用于资源隔离与可回收性，**不是安全沙箱**。
- 状态机：queued → running → cancel_requested → succeeded | failed |
  cancelled | timed_out。"cancel_requested" = 已请求取消但进程尚未退出
  （不提前写 cancelled；真实退出由 exit_kind 与终态体现）。
  "HTTP 请求结束 ≠ 执行结束"，终态作业仍可 inspect/collect_result。
- submit 幂等：同 job_id 再次提交返回既有 record，不重复建进程。
- 排队容量满 → ComputeQueueFull（锁内原子记账，不超卖）；依赖探测失败 →
  ComputeEnvError（列缺失依赖与所用解释器，绝不静默换解释器）。
- cancel(job_id) 返回 dict（ok/state/requested/reason；dict 真值 == ok）：
  queued 直接标 cancelled（派发前同锁复查，绝不 spawn）；running 写 stop
  标志 → 等 cancel_grace_s → terminate → ``taskkill /T /F /PID <pid>``
  （仅限本插件自己拉起的 worker pid 树，绝不按进程名批量杀）。
- worker 崩溃不影响 manager：watcher/派发线程 try/except 全包，异常把作业
  标 failed。
- 后端重启：init 扫描 base_dir 下无终态标记的旧作业 → 标 failed
  （"后端重启，作业状态未知"），不追杀任何历史进程。
- 单作业的 state.json 一次写入者按状态机顺序唯一（提交→派发→回收各自顺序
  执行），records dict 全程受 _lock 保护。
- stdout/stderr 由 manager 打开 log.txt 文件句柄直接重定向给子进程
  （无管道 → 无"管道写满死锁"），尾部 256KB 进入 record.stdout_tail。
- Windows 子进程带 creationflags=0x08000000（CREATE_NO_WINDOW）。
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

try:
    import paths  # 后端模块平铺导入（与 adslog/config 一致）
except ImportError:  # pragma: no cover - 被当作独立模块加载时的兜底
    paths = None

#: 终态集合（state 只有落进这里才算执行结束）
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "timed_out")
#: 合法状态全集（契约：queued/running/cancel_requested + 四个终态）
VALID_STATES = ("queued", "running", "cancel_requested") + TERMINAL_STATES

_LOG_TAIL_LIMIT = 256 * 1024        # stdout 尾部保留量（契约：256KB）
_WATCH_SLACK_S = 5.0                # worker 看门狗预算之外的管理器兜底余量
_TERMINATE_WAIT_S = 1.5             # terminate() 后等待退出的时间
_FINAL_WAIT_S = 10.0                # taskkill 后的最终回收等待
_CREATE_NO_WINDOW = 0x08000000      # Windows CREATE_NO_WINDOW
_POLL_INTERVAL_S = 0.05             # 有界轮询步长
_REQUIRED_MODULES = ("numpy", "scipy")


class ComputeError(Exception):
    """计算作业管理器的基础异常。"""


class ComputeQueueFull(ComputeError):
    """排队容量已满（原子拒绝，不超卖）。"""


class ComputeBusy(ComputeError):
    """契约保留：当前策略下并发满进排队队列，不抛此异常。"""


class ComputeEnvError(ComputeError):
    """依赖探测失败（列出缺失依赖与所用解释器）。"""


# ---------------------------------------------------------------------------
# 模块级工具（_probe_dependencies 顶层定义，便于测试 monkeypatch）
# ---------------------------------------------------------------------------

def _probe_dependencies(python_executable, required=_REQUIRED_MODULES,
                        timeout_s: float = 60.0) -> list:
    """返回缺失依赖列表（空列表 = 依赖齐全）。绝不静默换解释器。"""
    code = (
        "import importlib.util, json, sys\n"
        "print(json.dumps([m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]))\n"
    )
    try:
        proc = subprocess.run(
            [python_executable, "-c", code, *required],
            capture_output=True, text=True, timeout=timeout_s,
            creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ComputeEnvError(
            f"依赖探测失败（解释器 {python_executable}）：{exc}") from exc
    try:
        missing = [str(m) for m in json.loads(proc.stdout.strip().splitlines()[-1])]
    except Exception as exc:  # noqa: BLE001 - 探测输出必须可解析，否则如实报错
        raise ComputeEnvError(
            f"依赖探测输出无法解析（解释器 {python_executable}，"
            f"rc={proc.returncode}，stderr 尾部={proc.stderr[-200:]!r}）") from exc
    return missing


def _atomic_write_json(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01 * (attempt + 1))


def _tail_text(path: str, limit: int) -> tuple:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            data = f.read(limit)
        return data.decode("utf-8", "replace"), size > limit
    except OSError:
        return "", False


def _safe_name(job_id) -> str:
    safe = "".join(c for c in str(job_id) if c.isalnum() or c in "_-")
    return safe or "job"


# ---------------------------------------------------------------------------
# ComputeManager
# ---------------------------------------------------------------------------

class ComputeManager:
    """后端本地计算作业管理器（worker = 本模块同目录 compute_worker.py）。"""

    def __init__(self, base_dir=None, python_executable=None, max_workers=2,
                 queue_capacity=8, default_timeout_s=600, max_timeout_s=7200,
                 cancel_grace_s=3.0, probe_cache_s=3600):
        self._python_exe = python_executable or sys.executable
        self._max_workers = max(1, int(max_workers))
        self._queue_capacity = max(1, int(queue_capacity))
        self._default_timeout_s = float(default_timeout_s)
        self._max_timeout_s = float(max_timeout_s)
        self._cancel_grace_s = float(cancel_grace_s)
        self._probe_cache_s = float(probe_cache_s)
        self._worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "compute_worker.py")
        if base_dir:
            self._base_dir = os.path.abspath(base_dir)
        else:
            root = paths.data_root() if paths is not None else \
                os.path.dirname(os.path.abspath(__file__))
            self._base_dir = os.path.join(root, "compute_jobs")
        os.makedirs(self._base_dir, exist_ok=True)

        self._lock = threading.RLock()
        self._slot_free = threading.Condition(self._lock)
        self._records = {}          # job_id -> record dict（全部受 _lock 保护）
        self._procs = {}            # job_id -> Popen（running 中）
        self._logs = {}             # job_id -> log.txt 文件句柄
        self._reaped = set()        # 已收尾（终态化）的 job_id，保证只收尾一次
        self._running = 0           # 当前运行中的 worker 数
        self._pending_count = 0     # 已入队尚未开跑的作业数（容量记账）
        self._pending = queue.Queue()   # 无界 FIFO；容量由 _pending_count 锁内强制
        self._shutdown_flag = False
        self._shutdown_done = False
        self._probe_cache = None    # (missing_list, ts)
        self._stopping = set()      # 正处于取消/回收升级流程的 job_id
        self._killed = set()        # 已被管理器强制终止的 job_id（终态归属用）

        self._sweep_stale_jobs()
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop, name="compute-dispatcher", daemon=True)
        self._dispatcher_thread.start()

    # -- 只读属性 -----------------------------------------------------------

    @property
    def base_dir(self) -> str:
        return self._base_dir

    @property
    def python_executable(self) -> str:
        return self._python_exe

    def job_dir(self, job_id) -> str:
        return os.path.join(self._base_dir, _safe_name(job_id))

    # -- 日志 ----------------------------------------------------------------

    def _log(self, message) -> None:
        path = os.path.join(self._base_dir, "manager.log")
        try:
            if os.path.getsize(path) > 1_000_000:  # 有界
                return
        except OSError:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(message) + "\n")
        except OSError:
            pass

    # -- 依赖探测（带缓存） ---------------------------------------------------

    def _probe_env(self) -> list:
        now = time.time()
        with self._lock:
            if self._probe_cache is not None:
                missing, ts = self._probe_cache
                if now - ts < self._probe_cache_s:
                    return missing
        missing = _probe_dependencies(self._python_exe)
        with self._lock:
            self._probe_cache = (missing, now)
        return missing

    def _clamp_timeout(self, timeout_s) -> float:
        try:
            t = float(timeout_s)
        except (TypeError, ValueError):
            return self._default_timeout_s
        if not math.isfinite(t) or t <= 0:
            return self._default_timeout_s
        return min(t, self._max_timeout_s)

    # -- submit ---------------------------------------------------------------

    def submit(self, job_id, code, inputs=None, timeout_s=None, workspace="",
               label=""):
        job_id = str(job_id)
        if not job_id:
            raise ComputeError("job_id 不能为空")
        if not str(code or "").strip():
            raise ComputeError("code 为空")
        with self._lock:
            if self._shutdown_flag:
                raise ComputeError("ComputeManager 已关闭，不再接受新作业")
            existing = self._records.get(job_id)
            if existing is not None:
                return dict(existing)  # 幂等：不重复建进程
            if self._pending_count >= self._queue_capacity:
                raise ComputeQueueFull(
                    f"计算排队已满（容量 {self._queue_capacity}），作业未受理")
            self._pending_count += 1
            rec = {
                "job_id": job_id,
                "state": "queued",
                "phase": "queued",
                "workspace": str(workspace or ""),
                "submit_ts": time.time(),
                "start_ts": None,
                "end_ts": None,
                "timeout_s": self._clamp_timeout(timeout_s),
                "worker_pid": None,
                "exit_kind": None,
                "error_kind": None,
                "stdout_tail": "",
                "result": None,
                "partial": False,
                # 契约外补充字段
                "elapsed_s": None,
                "error": None,
                "cancel_requested": False,
                "label": str(label or ""),
            }
            self._records[job_id] = rec
        req = {
            "job_id": job_id,
            "workspace": rec["workspace"],
            "code": str(code),
            "inputs": inputs,
            "timeout_s": rec["timeout_s"],
            "created_ts": rec["submit_ts"],
        }
        try:
            os.makedirs(self.job_dir(job_id), exist_ok=True)
            _atomic_write_json(os.path.join(self.job_dir(job_id), "request.json"), req)
        except OSError as exc:
            with self._lock:
                self._records.pop(job_id, None)
                self._pending_count = max(0, self._pending_count - 1)
            raise ComputeError(f"写入作业请求失败：{exc}") from exc
        missing = self._probe_env()
        if missing:
            with self._lock:
                self._records.pop(job_id, None)
                self._pending_count = max(0, self._pending_count - 1)
            self._remove_job_dir(job_id)
            raise ComputeEnvError(
                f"计算依赖缺失：{', '.join(missing)}；解释器：{self._python_exe}")
        self._pending.put(job_id)   # 无界队列，不会失败
        return dict(rec)

    # -- 查询 ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """进程级汇总，供 /health 观察（不进入单个作业的昂贵细节）。

        与 ``inspect()`` 的区别：这里只给计数与排队/运行中作业的最小视图，
        不读 progress.jsonl、不算逐作业 elapsed。心跳与容量排查用它，
        不要为每个作业做一次磁盘 IO。
        """
        with self._lock:
            recs = list(self._records.values())
            running = []
            for rec in recs:
                if rec.get("state") not in ("queued", "running", "cancel_requested"):
                    continue
                item = {
                    "job_id": rec.get("job_id"),
                    "state": rec.get("state"),
                    "label": rec.get("label") or "",
                    "timeout_s": rec.get("timeout_s"),
                    "worker_pid": rec.get("worker_pid"),
                }
                if rec.get("start_ts"):
                    item["elapsed_s"] = round(time.time() - rec["start_ts"], 1)
                running.append(item)
            return {
                "max_workers": self._max_workers,
                "queue_capacity": self._queue_capacity,
                "running": self._running,
                "queued": self._pending_count,
                "cancel_requested": sum(
                    1 for r in recs if r.get("state") == "cancel_requested"),
                "jobs": running[:32],
                "python_executable": self._python_exe,
            }

    def inspect(self, job_id):
        with self._lock:
            rec = self._records.get(str(job_id))
            if rec is None:
                return None
            snap = dict(rec)
        if snap["state"] in ("running", "cancel_requested") and snap["start_ts"]:
            snap["elapsed_s"] = round(time.time() - snap["start_ts"], 3)
            live = self._live_phase(self.job_dir(snap["job_id"]))
            if live:
                snap["phase"] = live
        return snap

    def collect_result(self, job_id):
        rec = self.inspect(job_id)
        if rec is None:
            raise KeyError(f"未知计算作业：{job_id}")
        return rec

    def wait(self, job_id, timeout_s, cancel_check=None):
        """阻塞等待作业终态，返回 record（快照）。

        cancel_check 回调返回 True 时触发一次 cancel 并继续等回收
        （额外等待上限 = cancel_grace_s + terminate + final 兜底）。
        超时未到终态时如实返回当前 record（state 仍为非终态）。
        """
        job_id = str(job_id)
        deadline = time.monotonic() + (
            math.inf if timeout_s is None else float(timeout_s))
        cancel_fired = False
        extra = self._cancel_grace_s + _TERMINATE_WAIT_S + _FINAL_WAIT_S + 2.0
        while True:
            rec = self.inspect(job_id)
            if rec is None:
                raise KeyError(f"未知计算作业：{job_id}")
            if rec["state"] in TERMINAL_STATES:
                return rec
            limit = deadline + (extra if cancel_fired else 0.0)
            if time.monotonic() >= limit:
                return rec
            if cancel_check is not None and not cancel_fired and cancel_check():
                self.cancel(job_id)
                cancel_fired = True
                continue
            time.sleep(_POLL_INTERVAL_S)

    # -- cancel -----------------------------------------------------------------

    def cancel(self, job_id):
        """请求取消作业。返回 dict：ok/state/requested/reason（dict 真值==ok）。

        - queued：直接标 cancelled（派发前同锁复查，绝不 spawn）。
        - running/cancel_requested：state 先落为 cancel_requested（不提前写
          cancelled），然后 stop 标志 → 等 cancel_grace_s → terminate →
          taskkill /T /F /PID（仅限自有 worker pid 树），最终回收。
        - 终态/未知：ok=False，不改终态。幂等：重复调用不重复升级。
        """
        job_id = str(job_id)
        proc = None
        with self._lock:
            rec = self._records.get(job_id)
            if rec is None:
                return {"ok": False, "state": None, "requested": False,
                        "reason": "unknown_job"}
            state = rec["state"]
            if state in TERMINAL_STATES:
                return {"ok": False, "state": state, "requested": False,
                        "reason": "already_terminal"}
            rec["cancel_requested"] = True
            if state == "queued":
                # 派发线程在同一把锁内完成"取消检查→spawn"，标成终态后绝不会再 spawn
                rec["state"] = "cancelled"
                rec["phase"] = "cancelled"
                rec["exit_kind"] = "missing"
                rec["end_ts"] = time.time()
                self._reaped.add(job_id)
                self._slot_free.notify_all()
                snapshot = dict(rec)
            elif state == "cancel_requested":
                return {"ok": True, "state": state, "requested": False,
                        "reason": "cancel_already_in_progress"}
            else:  # running
                rec["state"] = "cancel_requested"
                rec["phase"] = rec.get("phase") or "cancel_requested"
                snapshot = dict(rec)
                proc = self._procs.get(job_id)
                self._stopping.add(job_id)
        self._persist(snapshot)
        if state == "queued":
            self._cleanup_job_dir(job_id)
            return {"ok": True, "state": "cancelled", "requested": True,
                    "reason": "queued_cancelled_before_spawn"}
        if proc is None:  # 理论不可达：running 必有 proc；保守处理交给 watcher
            self._log(f"cancel 时未找到进程句柄 job={job_id}，等待 watcher 兜底")
            return {"ok": True, "state": "cancel_requested", "requested": True,
                    "reason": "proc_handle_missing_watchdog_will_reap"}
        if proc.poll() is not None:
            # 已自行退出，watcher 可能尚未收尾：这里直接收尾（payload 状态优先）
            self._finalize(job_id, proc, proc.wait(timeout=_FINAL_WAIT_S),
                           killed_by_manager=False, state_hint="cancelled",
                           error_kind_hint="cancelled_by_manager")
            return {"ok": True, "state": self._state_of(job_id),
                    "requested": True, "reason": "exited_before_stop"}
        # 协作停止 → 宽限 → terminate → taskkill（全部只针对自有 pid）
        try:
            with open(os.path.join(self.job_dir(job_id), "stop"), "w") as f:
                f.write("cancel")
        except OSError:
            self._log(f"写 stop 标志失败 job={job_id}")
        deadline = time.monotonic() + self._cancel_grace_s
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(_POLL_INTERVAL_S)
        killed = False
        if proc.poll() is None:
            killed = True
            # 先登记再杀：watcher 可能在我们 terminate 生效的瞬间先收尾，
            # 靠这张表保证它仍按"管理器强制终止"归类（见 _finalize）。
            with self._lock:
                self._killed.add(job_id)
            self._log(f"取消升级：terminate worker job={job_id} pid={proc.pid}")
            self._terminate_tree(proc)
        try:
            rc = proc.wait(timeout=_FINAL_WAIT_S)
        except subprocess.TimeoutExpired:
            rc = None
            self._log(f"taskkill 后仍未退出 job={job_id} pid={proc.pid}")
        self._finalize(job_id, proc, rc, killed_by_manager=killed,
                       state_hint="cancelled",
                       error_kind_hint="cancelled_by_manager")
        return {"ok": True, "state": self._state_of(job_id), "requested": True,
                "reason": "terminated" if killed else "stopped_gracefully"}

    def _state_of(self, job_id):
        with self._lock:
            rec = self._records.get(str(job_id))
            return rec["state"] if rec else None

    def _terminate_tree(self, proc) -> None:
        try:
            proc.terminate()  # Windows = TerminateProcess（强杀单进程）
        except OSError:
            return
        deadline = time.monotonic() + _TERMINATE_WAIT_S
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(_POLL_INTERVAL_S)
        if proc.poll() is not None:
            return
        # 进程树强杀：仅限本插件自己拉起的 worker pid（绝不按进程名批量杀）
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=15,
                    creationflags=_CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._log(f"taskkill 失败 pid={proc.pid}: {exc!r}")
        else:
            try:
                proc.kill()
            except OSError:
                pass

    # -- 派发 -------------------------------------------------------------------

    def _dispatch_loop(self):
        while True:
            job_id = None
            try:
                if self._shutdown_flag:
                    return
                try:
                    job_id = self._pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                if job_id is None:
                    return
                self._dispatch_one(job_id)
            except Exception as exc:  # noqa: BLE001 - 派发线程绝不允许死
                self._log(f"派发线程异常：{type(exc).__name__}: {exc}")
                if job_id is not None:
                    self._fail_dispatch(job_id, f"派发线程异常：{type(exc).__name__}: {exc}")

    def _dispatch_one(self, job_id) -> bool:
        spawn_error = False
        snapshot = None
        proc = None
        with self._lock:
            rec = self._records.get(job_id)
            if rec is None or rec["state"] != "queued" or self._shutdown_flag:
                self._pending_count = max(0, self._pending_count - 1)
                return False
            while self._running >= self._max_workers and not self._shutdown_flag:
                self._slot_free.wait(0.2)
            self._pending_count = max(0, self._pending_count - 1)
            if self._shutdown_flag or rec["state"] != "queued":
                return False  # 排队期间被取消：绝不 spawn
            try:
                proc = self._spawn(job_id)
            except Exception as exc:  # noqa: BLE001 - spawn 失败 → 作业标 failed
                rec["state"] = "failed"
                rec["phase"] = "spawn_error"
                rec["error_kind"] = "spawn_error"
                rec["error"] = f"{type(exc).__name__}: {exc}"
                rec["exit_kind"] = "missing"
                rec["end_ts"] = time.time()
                self._reaped.add(job_id)
                snapshot = dict(rec)
                spawn_error = True
            else:
                self._running += 1
                rec["state"] = "running"
                rec["phase"] = "running"
                rec["start_ts"] = time.time()
                rec["worker_pid"] = proc.pid
                snapshot = dict(rec)
            self._slot_free.notify_all()
        self._persist(snapshot)
        if spawn_error:
            self._cleanup_job_dir(job_id)
            return False
        threading.Thread(target=self._watch, args=(job_id, proc),
                         name=f"compute-watch-{job_id[:8]}", daemon=True).start()
        return True

    def _fail_dispatch(self, job_id, message):
        with self._lock:
            rec = self._records.get(job_id)
            if rec is None or job_id in self._reaped:
                return
            self._reaped.add(job_id)
            rec["state"] = "failed"
            rec["phase"] = "dispatch_error"
            rec["error_kind"] = "dispatch_error"
            rec["error"] = message
            rec["end_ts"] = time.time()
            snapshot = dict(rec)
            self._slot_free.notify_all()
        self._persist(snapshot)
        self._cleanup_job_dir(job_id)

    def _spawn(self, job_id):
        jdir = self.job_dir(job_id)
        handle = open(os.path.join(jdir, "log.txt"), "ab")
        kwargs = {"stdout": handle, "stderr": handle,
                  "stdin": subprocess.DEVNULL, "cwd": jdir}
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                [self._python_exe, self._worker_path, jdir], **kwargs)
        except Exception:
            handle.close()
            raise
        with self._lock:
            self._procs[job_id] = proc
            self._logs[job_id] = handle
        return proc

    # -- watcher（每个 running 作业一个线程） ------------------------------------

    def _watch(self, job_id, proc):
        try:
            with self._lock:
                rec = self._records.get(job_id)
                budget = (rec["timeout_s"] if rec else 0) + _WATCH_SLACK_S
            try:
                rc = proc.wait(timeout=budget)
                self._finalize(job_id, proc, rc, killed_by_manager=False)
            except subprocess.TimeoutExpired:
                # worker 看门狗兜底失效 → 管理器强制回收（超时）
                self._log(f"预算到期未退出，管理器强制回收 job={job_id} pid={proc.pid}")
                self._terminate_tree(proc)
                try:
                    rc = proc.wait(timeout=_FINAL_WAIT_S)
                except subprocess.TimeoutExpired:
                    rc = None
                self._finalize(job_id, proc, rc, killed_by_manager=True,
                               state_hint="timed_out",
                               error_kind_hint="timeout_manager_backstop")
        except Exception as exc:  # noqa: BLE001 - watcher 全包，异常→failed
            self._fail_from_thread(job_id, f"watcher 异常：{type(exc).__name__}: {exc}")

    def _fail_from_thread(self, job_id, message):
        with self._lock:
            rec = self._records.get(job_id)
            if rec is None or job_id in self._reaped:
                return
            self._reaped.add(job_id)
            rec["state"] = "failed"
            rec["phase"] = "manager_error"
            rec["error_kind"] = "manager_watch_error"
            rec["error"] = message
            rec["end_ts"] = time.time()
            handle = self._logs.pop(job_id, None)
            self._procs.pop(job_id, None)
            self._running = max(0, self._running - 1)
            snapshot = dict(rec)
            self._slot_free.notify_all()
        if handle:
            try:
                handle.close()
            except OSError:
                pass
        self._persist(snapshot)
        self._cleanup_job_dir(job_id)

    # -- 收尾 -------------------------------------------------------------------

    def _finalize(self, job_id, proc, rc, killed_by_manager=False,
                  state_hint=None, error_kind_hint=None):
        """把退出进程收敛为终态 record（幂等；先到者收尾）。"""
        with self._lock:
            if job_id in self._reaped:
                return
            self._reaped.add(job_id)
            # 强制终止的归属由本表决定，而不是由"谁先收尾"决定：cancel() 在
            # 杀掉进程之前登记，watcher 可能在 terminate 生效的瞬间先跑完
            # proc.wait 并抢先收尾 —— 若此时仍按 killed_by_manager=False 分
            # 类，一次真实的管理器强制终止会被误报成 crashed/failed
            # （2026-10-30 集成测试发现）。
            killed_by_manager = bool(killed_by_manager or job_id in self._killed)
            self._killed.discard(job_id)
            rec = self._records.get(job_id)
            handle = self._logs.pop(job_id, None)
            self._procs.pop(job_id, None)
            self._stopping.discard(job_id)
            if rec is None:  # 不该发生；保守清理
                self._running = max(0, self._running - 1)
                self._slot_free.notify_all()
                snapshot = None
            else:
                jdir = self.job_dir(job_id)
                payload = self._read_result_payload(jdir)
                tail, trunc = _tail_text(os.path.join(jdir, "log.txt"), _LOG_TAIL_LIMIT)
                wstate = payload.get("state")
                if wstate in TERMINAL_STATES:
                    state = wstate
                    error_kind = payload.get("error_kind")
                    error_text = payload.get("error")
                elif state_hint is not None or killed_by_manager:
                    # 管理器强制终止时 watcher 不带 state_hint（它只看到进程
                    # 没了）。此时绝不能落到下面的 rc 分支被当成 crashed：
                    # 是"被取消/被回收"，不是"worker 自己崩了"。
                    state = state_hint or "cancelled"
                    error_kind = error_kind_hint or f"{state}_by_manager"
                    error_text = {
                        "timed_out": "执行预算到期，管理器强制回收（worker 未按时自行退出）",
                        "cancelled": "作业被取消，进程被管理器强制回收",
                    }.get(state, f"作业终态：{state}")
                elif rc == 9:
                    state, error_kind = "timed_out", "timeout"
                    error_text = "worker 看门狗超时退出（未写出结果文件）"
                elif rc == 0:
                    state, error_kind = "failed", "no_result"
                    error_text = "worker 正常退出但未写出 result.json"
                else:
                    state = "failed"
                    error_kind = f"crashed_exit_{rc}"
                    error_text = f"worker 异常退出（rc={rc}，未写出结果文件）"
                rec["state"] = state
                rec["phase"] = str(payload.get("phase") or "") or state
                rec["end_ts"] = time.time()
                rec["elapsed_s"] = (
                    round(rec["end_ts"] - rec["start_ts"], 3)
                    if rec["start_ts"] else None)
                rec["exit_kind"] = self._exit_kind(payload, rc,
                                                   killed_by_manager)
                rec["error_kind"] = error_kind
                rec["error"] = error_text
                rec["result"] = payload.get("result")
                rec["partial"] = bool(payload.get("partial")) or trunc
                rec["stdout_tail"] = tail if tail else str(payload.get("stdout_tail") or "")
                snapshot = dict(rec)
                self._running = max(0, self._running - 1)
                self._slot_free.notify_all()
        if handle:
            try:
                handle.close()
            except OSError:
                pass
        if snapshot is not None:
            self._persist(snapshot)
            self._cleanup_job_dir(job_id)

    @staticmethod
    def _exit_kind(payload, rc, killed_by_manager):
        if killed_by_manager:
            return "terminated"
        if payload.get("state") in TERMINAL_STATES:
            return "natural"          # worker 自己写完结果退出（含看门狗 os._exit(9)）
        if rc is None:
            return "missing"
        if rc == 0 or rc == 9:
            return "natural"
        return "crashed"

    def _read_result_payload(self, jdir) -> dict:
        try:
            with open(os.path.join(jdir, "result.json"), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _live_phase(self, jdir) -> str:
        path = os.path.join(jdir, "progress.jsonl")
        try:
            size = os.path.getsize(path)
            if size <= 0:
                return ""
            with open(path, "rb") as f:
                f.seek(max(0, size - 4096))
                data = f.read()
            lines = [ln for ln in data.decode("utf-8", "replace").splitlines()
                     if ln.strip()]
            if not lines:
                return ""
            note = json.loads(lines[-1]).get("note")
            return str(note) if note is not None else ""
        except (OSError, ValueError):
            return ""

    # -- 持久化与清理 ------------------------------------------------------------

    def _persist(self, rec) -> None:
        """state.json：终态标记 + 可查询摘要。单作业一次写入者按状态机顺序唯一。"""
        jdir = self.job_dir(rec["job_id"])
        try:
            os.makedirs(jdir, exist_ok=True)
            _atomic_write_json(os.path.join(jdir, "state.json"), rec)
        except OSError as exc:
            self._log(f"state.json 写入失败 job={rec.get('job_id')}: {exc!r}")

    def _cleanup_job_dir(self, job_id) -> None:
        """作业终态后清理临时产物；保留 state.json（终态标记 + result 摘要）。"""
        jdir = self.job_dir(job_id)
        keep = {"state.json"}
        try:
            names = os.listdir(jdir)
        except OSError:
            return
        for name in names:
            if name in keep:
                continue
            path = os.path.join(jdir, name)
            for attempt in range(10):
                try:
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.remove(path)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    if attempt == 9:
                        self._log(f"临时文件清理失败（保留现场）job={job_id}: {path}")
                    else:
                        time.sleep(0.01 * (attempt + 1))

    def _remove_job_dir(self, job_id) -> None:
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    # -- 启动清扫 ------------------------------------------------------------------

    def _sweep_stale_jobs(self):
        """init：扫描 base_dir，无终态标记的旧作业标 failed（后端重启，状态未知）。

        绝不追杀历史进程；终态作业加载回内存以便事后查询。
        """
        swept = 0
        try:
            names = sorted(os.listdir(self._base_dir))
        except OSError:
            return
        now = time.time()
        for name in names:
            jdir = os.path.join(self._base_dir, name)
            if not os.path.isdir(jdir):
                continue
            data = self._read_json(os.path.join(jdir, "state.json"))
            if isinstance(data, dict) and data.get("state") in TERMINAL_STATES \
                    and data.get("job_id"):
                job_id = str(data["job_id"])
                self._records[job_id] = dict(data)
                self._reaped.add(job_id)
                self._cleanup_job_dir(job_id)  # 清理中途退出残留的临时文件
                continue
            req = self._read_json(os.path.join(jdir, "request.json"))
            if not isinstance(req, dict) and not isinstance(data, dict):
                # 既无终态标记也无请求：上次异常残留，直接清走，不造假记录
                shutil.rmtree(jdir, ignore_errors=True)
                continue
            job_id = ""
            if isinstance(data, dict):
                job_id = str(data.get("job_id") or "")
            if not job_id and isinstance(req, dict):
                job_id = str(req.get("job_id") or "")
            if not job_id:
                job_id = name
            timeout_s = self._default_timeout_s
            for src in (req, data):
                if isinstance(src, dict):
                    try:
                        t = float(src.get("timeout_s"))
                        if math.isfinite(t) and t > 0:
                            timeout_s = t
                    except (TypeError, ValueError):
                        pass
            submit_ts = now
            if isinstance(req, dict):
                try:
                    submit_ts = float(req.get("created_ts"))
                except (TypeError, ValueError):
                    pass
            rec = {
                "job_id": job_id,
                "state": "failed",
                "phase": "backend_restart",
                "workspace": str((req or {}).get("workspace", "")
                                 if isinstance(req, dict) else ""),
                "submit_ts": submit_ts,
                "start_ts": None,
                "end_ts": now,
                "timeout_s": timeout_s,
                "worker_pid": None,
                "exit_kind": "missing",
                "error_kind": "backend_restart",
                "stdout_tail": str(data.get("stdout_tail") or "")
                if isinstance(data, dict) else "",
                "result": None,
                "partial": False,
                "elapsed_s": None,
                "error": "后端重启，作业状态未知",
                "cancel_requested": False,
                "label": "",
            }
            self._records[job_id] = rec
            self._reaped.add(job_id)
            self._persist(rec)
            self._cleanup_job_dir(job_id)
            swept += 1
        if swept:
            self._log(f"启动清扫：{swept} 个遗留作业标记 failed（后端重启，作业状态未知）")

    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # -- 关停 ----------------------------------------------------------------------

    def shutdown_all(self, grace_s=None) -> None:
        """关停：排队作业标 cancelled；running 走 stop→宽限→terminate→taskkill。"""
        grace = self._cancel_grace_s if grace_s is None else max(0.0, float(grace_s))
        with self._lock:
            if self._shutdown_done:
                return
            self._shutdown_flag = True
            self._slot_free.notify_all()
            queued_ids = [jid for jid, r in self._records.items()
                          if r["state"] == "queued" and jid not in self._reaped]
            running = list(self._procs.items())
        for jid in queued_ids:
            try:
                self.cancel(jid)
            except Exception as exc:  # noqa: BLE001
                self._log(f"shutdown 取消排队作业失败 job={jid}: {exc!r}")
        for jid, _proc in running:
            try:
                with open(os.path.join(self.job_dir(jid), "stop"), "w") as f:
                    f.write("shutdown")
            except OSError:
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self._lock:
                alive = [p for _j, p in running if p.poll() is None]
            if not alive:
                break
            time.sleep(_POLL_INTERVAL_S)
        for jid, proc in running:
            if proc.poll() is None:
                self._log(f"shutdown 强制回收 job={jid} pid={proc.pid}")
                self._terminate_tree(proc)
        for jid, proc in running:
            try:
                rc = proc.wait(timeout=_FINAL_WAIT_S)
            except subprocess.TimeoutExpired:
                rc = None
            try:
                self._finalize(jid, proc, rc, killed_by_manager=rc is None,
                               state_hint="cancelled",
                               error_kind_hint="backend_shutdown")
            except Exception as exc:  # noqa: BLE001
                self._log(f"shutdown 收尾失败 job={jid}: {exc!r}")
        try:
            self._pending.put_nowait(None)
        except queue.Full:
            pass
        self._dispatcher_thread.join(timeout=3.0)
        with self._lock:
            self._shutdown_done = True
