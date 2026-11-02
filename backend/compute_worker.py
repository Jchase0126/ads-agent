# -*- coding: utf-8 -*-
"""独立计算进程 worker —— 后端 ComputeManager 的子进程入口。

为什么是独立进程
----------------
ADS 插件的 run_python 在 ADS GUI 主线程 exec()，长 NumPy/SciPy 计算（真实
日志有 78.6 秒的单次 run_python、2976 次 scipy minimize）会把 ADS 界面整个
卡死。本 worker 由**后端进程**作为子进程拉起（compute_manager.ComputeManager），
完全不经过 ADS GUI。进程隔离只用于**资源隔离与可回收性**（超时/取消后可以
整进程回收），**不是安全沙箱**——用户代码拥有本进程的全部权限（文件、网络、
任意 import），这一点必须如实告知，不得包装成安全边界。

用法::

    python compute_worker.py <job_dir>

协议（job_dir 内）
------------------
request.json（输入，manager 写）::

    {"job_id": ..., "workspace": ..., "code": ...,
     "inputs": ..., "timeout_s": ..., "created_ts": ...}

log.txt         stdout+stderr 直写（行缓冲追加；manager 直接重定向文件句柄，
                无管道 → 无"管道写满死锁"）。
progress.jsonl  progress(note) 追加的阶段记录（manager 读尾部展示 phase）。
stop            manager 写入的取消标志文件；存在 = 请求协作取消。
result.json     最终结果；tmp + os.replace 原子写。

用户代码命名空间::

    inputs / check_cancel() / progress(note) / emit_result(obj)

结果优先取 emit_result 提交（多次调用最后一次生效，与 result 变量赋值语义
一致），否则回退命名空间中的 ``result`` 变量；两者都没有则 result=None 并
附提交途径提示。

净化规则：numpy ndarray / 标量 → tolist()；非 JSON 安全对象 → 截断 repr；
非有限浮点 → 字符串；结果 JSON 超过 256KB 或输出日志超过 256KB 尾部保留量
→ 截断并标注 partial=True。

内部看门狗线程：超过 timeout_s 硬预算后写出 ``{"state": "timed_out"}`` 的
result.json 并 ``os._exit(9)`` —— 纯 C 级热循环（GIL 已释放）也能退出。

退出码：0 = result.json 已写出（succeeded/failed/cancelled 语义都在文件里）；
9 = 内部看门狗超时；2 = 请求不可读/目录不可用；3 = 结果写出失败。
worker 本身 import 阶段只用标准库（numpy/scipy 由用户代码自行 import），
并且**不再 spawn 任何子进程**。
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import traceback

_LOG_TAIL_LIMIT = 256 * 1024      # stdout 尾部保留量（契约：256KB）
_RESULT_LIMIT = 256 * 1024        # 结果 JSON 超过即截断并标 partial
_ERROR_TEXT_LIMIT = 32 * 1024
_MAX_STR = 64 * 1024              # 单个字符串字段截断阈值
_MAX_ITEMS = 500                  # list/dict 元素数截断阈值
_MAX_DEPTH = 8                    # 嵌套净化深度上限
_REPR_LIMIT = 300                 # 未知对象 repr 截断长度
_NOTES_KEPT = 200                 # result.json 里保留的最近阶段记录数
_MAX_PROGRESS_NOTES = 1000        # progress.jsonl 最多写入条数（防刷爆）
_PROGRESS_NOTE_CHARS = 500
_DEFAULT_TIMEOUT_S = 600.0

_MISSING = object()               # emit_result 尚未调用的哨兵


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data) -> None:
    """tmp + os.replace 原子写 JSON（Windows 上 replace 可能被占用，带重试）。"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False)
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


def _try_write(path: str, payload: dict) -> None:
    try:
        _atomic_write_json(path, payload)
    except OSError:
        pass


def _tail_text(path: str, limit: int) -> tuple:
    """读文件尾部 ≤limit 字节，返回 (文本, 是否被截断)。文件不存在返回 ("", False)。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            data = f.read(limit)
        return data.decode("utf-8", "replace"), size > limit
    except OSError:
        return "", False


def _coerce_timeout(value) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S
    if not math.isfinite(t) or t <= 0:
        return _DEFAULT_TIMEOUT_S
    return t


def _short_repr(obj, limit: int = _REPR_LIMIT) -> str:
    try:
        text = repr(obj)
    except Exception as exc:  # noqa: BLE001 - __repr__ 可能任意抛
        text = f"<repr 失败: {type(exc).__name__}>"
    if len(text) > limit:
        return text[:limit] + f"…[截断，原长 {len(text)} 字符]"
    return text


# ---------------------------------------------------------------------------
# 结果净化
# ---------------------------------------------------------------------------

def _sanitize(obj, depth: int = 0) -> tuple:
    """把任意对象转成 JSON 安全值。返回 (值, 是否发生过截断)。绝不抛异常。"""
    if depth > _MAX_DEPTH:
        return _short_repr(obj), True
    # numpy ndarray / numpy 标量：有 tolist 就转（ndarray 与 np.float64 等）
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        try:
            return _sanitize(tolist(), depth + 1)
        except Exception:  # noqa: BLE001
            return _short_repr(obj), True
    if obj is None or isinstance(obj, bool) or isinstance(obj, int):
        return obj, False
    if isinstance(obj, float):
        try:
            if math.isfinite(obj):
                return obj, False
        except Exception:  # noqa: BLE001
            pass
        return str(obj), False  # nan/inf 不是合法 JSON → 字符串
    if isinstance(obj, str):
        if len(obj) > _MAX_STR:
            return obj[:_MAX_STR] + f"…[截断，原长 {len(obj)} 字符]", True
        return obj, False
    if isinstance(obj, (bytes, bytearray)):
        try:
            text = bytes(obj).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = _short_repr(obj)
        return _sanitize(text, depth + 1)
    if isinstance(obj, dict):
        out: dict = {}
        truncated = False
        items = list(obj.items())
        for key, value in items[:_MAX_ITEMS]:
            k, k_trunc = _sanitize(str(key), depth + 1)
            v, v_trunc = _sanitize(value, depth + 1)
            out[k] = v
            truncated = truncated or k_trunc or v_trunc
        if len(items) > _MAX_ITEMS:
            out["__截断__"] = f"另有 {len(items) - _MAX_ITEMS} 项未序列化"
            truncated = True
        return out, truncated
    if isinstance(obj, (list, tuple, set, frozenset)):
        out: list = []
        truncated = False
        seq = list(obj)
        for value in seq[:_MAX_ITEMS]:
            v, v_trunc = _sanitize(value, depth + 1)
            out.append(v)
            truncated = truncated or v_trunc
        if len(seq) > _MAX_ITEMS:
            out.append(f"…[截断，另有 {len(seq) - _MAX_ITEMS} 项]")
            truncated = True
        return out, truncated
    return _short_repr(obj), True


def _payload(job_id, state, *, error_kind=None, error=None, result=None,
             partial=False, tail="", tail_truncated=False, progress=None,
             progress_count=0, progress_suppressed=0, source="", phase="",
             timeout_s=0.0, start_ts=None, pid=None) -> dict:
    progress = progress or []
    return {
        "job_id": job_id,
        "state": state,
        "error_kind": error_kind,
        "error": error,
        "result": result,
        "partial": bool(partial),
        "stdout_tail": tail,
        "stdout_truncated": bool(tail_truncated),
        "progress": progress,
        "progress_count": progress_count,
        "progress_suppressed": progress_suppressed,
        "source": source,
        "phase": phase or state,
        "timeout_s": timeout_s,
        "worker_pid": os.getpid() if pid is None else pid,
        "start_ts": start_ts,
        "end_ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: list) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: compute_worker.py JOB_DIR\n")
        return 2
    job_dir = os.path.abspath(argv[0])
    result_path = os.path.join(job_dir, "result.json")
    log_path = os.path.join(job_dir, "log.txt")
    try:
        # 行缓冲追加写：stdout/stderr 都指向同一文件，无管道死锁
        log_handle = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        sys.stderr.write(f"cannot open log.txt in {job_dir!r}\n")
        return 2
    sys.stdout = log_handle
    sys.stderr = log_handle

    try:
        with open(os.path.join(job_dir, "request.json"), encoding="utf-8") as f:
            req = json.load(f)
        if not isinstance(req, dict):
            raise ValueError("request.json 顶层不是 JSON 对象")
    except Exception as exc:  # noqa: BLE001
        _try_write(result_path, _payload(
            "", "failed", error_kind="request_unreadable",
            error=f"读取 request.json 失败：{type(exc).__name__}: {exc}",
            phase="request_unreadable"))
        return 2

    job_id = str(req.get("job_id") or "")
    code = req.get("code")
    code = "" if code is None else (code if isinstance(code, str) else str(code))
    inputs = req.get("inputs")
    timeout_s = _coerce_timeout(req.get("timeout_s"))
    stop_path = os.path.join(job_dir, "stop")
    progress_path = os.path.join(job_dir, "progress.jsonl")

    if os.path.exists(stop_path):
        # 派发竞态窗口里管理器已取消：一个字节的用户代码都不跑
        _try_write(result_path, _payload(
            job_id, "cancelled", error_kind="cancelled_before_start",
            error="派发前已请求取消，作业未执行", phase="cancelled",
            timeout_s=timeout_s))
        return 0

    main_done = threading.Event()
    finishing = {"done": False}
    notes: list = []
    counters = {"total": 0, "suppressed": 0}
    notes_lock = threading.Lock()
    emitted = {"obj": _MISSING}
    start_ts = time.time()

    def check_cancel() -> bool:
        """协作取消点：每次调用检查 stop 标志文件是否存在。"""
        return os.path.exists(stop_path)

    def progress(note=""):
        if finishing["done"]:
            return
        text = str(note)[:_PROGRESS_NOTE_CHARS]
        with notes_lock:
            if len(notes) < _NOTES_KEPT:
                notes.append(text)
            counters["total"] += 1
            if counters["total"] > _MAX_PROGRESS_NOTES:
                counters["suppressed"] += 1
                return
        try:
            with open(progress_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "note": text},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    def emit_result(obj):
        if finishing["done"]:
            return
        # 多次调用：最后一次生效（与给 result 变量重复赋值语义一致）
        emitted["obj"] = obj

    def watchdog_fire():
        if main_done.is_set():
            return
        try:
            with notes_lock:
                prog = list(notes)
                cnt = counters["total"]
                sup = counters["suppressed"]
            tail, trunc = _tail_text(log_path, _LOG_TAIL_LIMIT)
            _try_write(result_path, _payload(
                job_id, "timed_out", error_kind="timeout",
                error=f"执行超过 {timeout_s:g}s 硬预算，内部看门狗强制退出",
                partial=trunc, tail=tail, tail_truncated=trunc,
                progress=prog, progress_count=cnt, progress_suppressed=sup,
                phase=(prog[-1] if prog else "timed_out"),
                timeout_s=timeout_s, start_ts=start_ts))
        except Exception:  # noqa: BLE001 - 看门狗路径必须最终 os._exit
            pass
        finally:
            try:
                log_handle.flush()
            except Exception:  # noqa: BLE001
                pass
            os._exit(9)

    timer = threading.Timer(timeout_s, watchdog_fire)
    timer.daemon = True
    timer.start()

    state, error_kind, error_text = "succeeded", None, None
    ns = {
        "inputs": inputs,
        "check_cancel": check_cancel,
        "progress": progress,
        "emit_result": emit_result,
    }
    try:
        exec(compile(code, f"<compute:{job_id or 'job'}>", "exec"), ns)  # noqa: S102 - 设计如此
    except BaseException as exc:  # 用户脚本的 SystemExit 也必须变成工具结果
        state = "failed"
        error_kind = type(exc).__name__
        try:
            tb_text = "".join(traceback.format_exception(exc))
        except Exception:  # noqa: BLE001
            tb_text = ""
        error_text = f"{type(exc).__name__}: {exc}" + (f"\n{tb_text}" if tb_text else "")
        if len(error_text) > _ERROR_TEXT_LIMIT:
            error_text = error_text[:_ERROR_TEXT_LIMIT] + "…[截断]"
    finally:
        main_done.set()            # 看门狗看到即不再强制退出
        finishing["done"] = True   # 关闭写入者入口
        timer.cancel()
        try:
            log_handle.flush()
        except Exception:  # noqa: BLE001
            pass

    raw = _MISSING
    source = ""
    if emitted["obj"] is not _MISSING:
        raw, source = emitted["obj"], "emit_result"
    elif "result" in ns:
        raw, source = ns["result"], "result_var"

    # 协作取消已生效：无论脚本有没有留下结果，最终状态都是 cancelled。
    # 2026-10-30 修正：以前只在"没有任何结果"时才标 cancelled，脚本在
    # check_cancel() 后 break 并留下部分 result 时会被报成 succeeded /
    # partial=False —— 部分产物绝不能标成完整成功。现在保留已算出的
    # 结果但显式 partial=True，让调用方知道这是中途停止的半成品。
    stopped = os.path.exists(stop_path)
    if state == "succeeded" and stopped:
        state, error_kind = "cancelled", "cancelled_by_request"
        if raw is not _MISSING:
            source = source or "partial"
        error_text = error_text or "计算在安全边界收到取消请求并停止（结果为部分结果）"

    result_value = None
    result_truncated = False
    # 中途取消也保留已经算出的结果（partial=True），只有 failed 才丢弃 ——
    # 调用方需要区分"跑到一半的有用数据"和"根本没结果"。
    if state in ("succeeded", "cancelled") and raw is not _MISSING:
        result_value, result_truncated = _sanitize(raw)
        if state == "cancelled":
            result_truncated = True   # 部分结果：不完整，不得当成完整成功
        try:
            encoded = json.dumps(result_value, ensure_ascii=False, allow_nan=False)
        except Exception:  # noqa: BLE001
            result_value = {"__序列化失败__": _short_repr(raw)}
            result_truncated = True
        else:
            if len(encoded) > _RESULT_LIMIT:
                result_value = {"__截断__": True, "类型": type(raw).__name__,
                                "预览": encoded[:2048]}
                result_truncated = True

    tail, out_truncated = _tail_text(log_path, _LOG_TAIL_LIMIT)
    with notes_lock:
        prog = list(notes)
        cnt = counters["total"]
        sup = counters["suppressed"]

    payload = _payload(
        job_id, state, error_kind=error_kind, error=error_text,
        result=result_value, partial=(result_truncated or out_truncated),
        tail=tail, tail_truncated=out_truncated,
        progress=prog, progress_count=cnt, progress_suppressed=sup,
        source=source, phase=(prog[-1] if prog else state),
        timeout_s=timeout_s, start_ts=start_ts)
    if state == "succeeded" and raw is _MISSING:
        payload["no_result_hint"] = "未产生结果：可用 emit_result(obj) 或给 result 变量赋值"

    try:
        _atomic_write_json(result_path, payload)
    except OSError:
        return 3
    try:
        log_handle.flush()
    except Exception:  # noqa: BLE001
        pass
    # 用户代码遗留的非 daemon 线程不得拖住 worker；结果已原子落盘，硬退出。
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
