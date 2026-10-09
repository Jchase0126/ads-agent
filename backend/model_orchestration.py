"""模型导入的**统一编排层** —— 一次导入 = 一条持久化、有界、可取消的操作记录。

为什么单列一层
--------------
导入有两个入口：面板附件按钮（HTTP ``POST /models/import``）和 LLM 工具
``import_model_package``。过去两个入口各走各的：HTTP 入口有工作区串行锁和
取消事件，LLM 入口直接调编排函数、不过那把锁 —— 于是"按钮与 LLM 同时导入"
会并发改同一份 lib.defs，而重复挂接正是用户明确要求避免的（幂等）。这里把
串行锁、取消事件、状态记录收到一处，两个入口共用同一条流水线与同一把锁。

HTTP 生命周期（本层存在的主因）
------------------------------
``POST /models/import`` 只回**一次** 202 + op_id，后台线程**绝不**碰
Handler/socket：过去的写法是后台线程完成后再用同一个 Handler 写一次响应，
于是"一个请求两次响应"，第二次写进的往往是一条已关闭的连接（面板表现为
偶发的 RemoteDisconnected，而真实原因只是"导入完成了"）。现在后台只更新
操作记录与资产状态，最终结果由 ``GET /models/op?id=...`` 查询。

操作状态是**持久化**的，因此后端重启后不会永久停在"运行中"：启动时把
未终结的操作标记为 ``interrupted``（第 8 个状态，见 :data:`OP_INTERRUPTED`
的说明）—— 它不是"失败"，只是"没有证据表明它跑完了"。
"""

import json
import os
import threading
import uuid

import adslog
import model_store
import shared_models
import tools as tools_mod

log = adslog.get("backend.model_orchestration")

# ---------------------------------------------------------------------------
# 操作状态
# ---------------------------------------------------------------------------

OP_QUEUED = "queued"                    # 已受理，尚未拿到工作区锁
OP_RUNNING = "running"                  # 正在跑（see op["phase"] 看具体阶段）
OP_AWAITING_USER = "awaiting_user"      # 需要用户决策（多候选套件根 / 同名库冲突）
OP_SUCCEEDED = "succeeded"              # 完成（**不代表模型可用**，见 pending_verify）
OP_FAILED = "failed"                    # 失败（产物保留）
OP_CANCEL_REQUESTED = "cancel_requested"  # 已收到取消，流水线在下一个边界停下
OP_CANCELLED = "cancelled"              # 已取消（已完成的前置步骤保留）
OP_INTERRUPTED = "interrupted"          # 后端在操作中途退出，重启后标此态

OP_STATES = (
    OP_QUEUED, OP_RUNNING, OP_AWAITING_USER, OP_SUCCEEDED, OP_FAILED,
    OP_CANCEL_REQUESTED, OP_CANCELLED, OP_INTERRUPTED,
)

OP_LABELS = {
    OP_QUEUED: "排队中",
    OP_RUNNING: "进行中",
    OP_AWAITING_USER: "等待你决定",
    OP_SUCCEEDED: "已完成",
    OP_FAILED: "失败",
    OP_CANCEL_REQUESTED: "正在取消",
    OP_CANCELLED: "已取消",
    OP_INTERRUPTED: "上次运行中断",
}

#: 已终结：不会再变。``awaiting_user`` 不在其中 —— 用户在界面上做出选择后
#: 会重新提交一次导入，那条操作才算终结。
OP_TERMINAL = frozenset({OP_SUCCEEDED, OP_FAILED, OP_CANCELLED, OP_INTERRUPTED})
#: 仍在进行：同 (workspace, package_id) 的新请求只会**并入**这些操作，
#: 不会另起一条。
OP_ACTIVE = frozenset({OP_QUEUED, OP_RUNNING, OP_CANCEL_REQUESTED,
                       OP_AWAITING_USER})

# 排队等锁时轮询取消的间隔（秒）。取 0.2 是为了让"排队中的取消"能在
# 人类可感知的时间里生效，又不至于把 CPU 花在自旋上。
_LOCK_POLL = 0.2

# 有界历史：操作记录用来排障与查询，不是审计日志。超过上限先淘汰**已终结**
# 的旧记录（进行中的一条都不能丢 —— 丢了就查不到正在跑的导入了）。
_MAX_OPS = 200

_OPS_DIRNAME = "model_ops"
_OPS_FILENAME = "ops.json"

# 复用资产层的原子写：Windows 上 os.replace 与 open 会互相打断，那段重试
# 逻辑（实测最坏要重试 22 次）不值得在这里重写第二份。
_atomic_write_bytes = model_store._atomic_write_bytes


class OrchestrationError(RuntimeError):
    """编排层的可预期错误（会如实转成接口/工具结果里的 error）。"""


# ---------------------------------------------------------------------------
# 持久化：有界操作记录
# ---------------------------------------------------------------------------

def _ops_file() -> str:
    """操作记录文件的位置（用户数据目录下，与工作区无关）。

    刻意**不**放在工作区里：操作记录要在重启后仍可按 op_id 查到，而后端
    重启时并不知道用户上次打开的是哪个工作区。
    """
    import config as config_mod

    return os.path.join(config_mod.PROJECT_ROOT, _OPS_DIRNAME, _OPS_FILENAME)


def _blank_op(op_id: str, workspace: str, package_id: str, identity: dict,
              source: str, request_key: str) -> dict:
    now = model_store.utc_now()
    return {
        "op_id": op_id,
        "schema": 1,
        "kind": "import",
        "source": source,                  # http（附件按钮）/ llm（模型工具）
        "workspace": workspace,
        "package_id": package_id,
        # 固定**提交那一刻**的资产身份：换工程、包被替换都要能被发现，
        # 不能让一条操作悄悄指向另一个内容不同的包。
        "identity": dict(identity or {}),
        "request_key": request_key,        # 幂等键（客户端重试带同一个就复用）
        "state": OP_QUEUED,
        "state_label": OP_LABELS[OP_QUEUED],
        "phase": "queued",
        "steps": [],
        "message": "",
        "error": "",
        "error_kind": "",
        "partial": False,
        "result": None,
        "ads_job_id": "",
        "cancel_requested_at": "",
        "pid": os.getpid(),
        "created_at": now,
        "started_at": "",
        "updated_at": now,
        "finished_at": "",
    }


class _OpStore:
    """操作记录的读-改-写（进程内一把锁 + 原子落盘 + 容量上限）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ops: dict = {}
        self._loaded = False

    # -- 读写 ---------------------------------------------------------
    def _read_file(self) -> dict:
        path = _ops_file()
        try:
            with open(path, "rb") as stream:
                raw = stream.read()
        except (FileNotFoundError, OSError):
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            log.warning("操作记录文件损坏，已按空记录继续：%s", path)
            return {}
        ops = data.get("ops") if isinstance(data, dict) else None
        return ops if isinstance(ops, dict) else {}

    def _write_file(self) -> None:
        path = _ops_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = json.dumps({"schema": 1, "ops": self._ops},
                             ensure_ascii=False, indent=1).encode("utf-8")
        _atomic_write_bytes(path, payload)

    def _load(self) -> None:
        if self._loaded:
            return
        self._ops = self._read_file()
        self._loaded = True

    def _trim(self) -> None:
        """超上限时淘汰最旧的**已终结**记录。"""
        if len(self._ops) <= _MAX_OPS:
            return
        finished = [(r.get("finished_at") or r.get("updated_at") or "", k)
                    for k, r in self._ops.items()
                    if str(r.get("state")) in OP_TERMINAL]
        finished.sort()
        drop = len(self._ops) - _MAX_OPS
        for _stamp, key in finished[:drop]:
            self._ops.pop(key, None)

    def all(self) -> dict:
        with self._lock:
            self._load()
            return json.loads(json.dumps(self._ops))

    def get(self, op_id: str) -> dict | None:
        with self._lock:
            self._load()
            record = self._ops.get(op_id)
            return json.loads(json.dumps(record)) if record else None

    def put(self, record: dict) -> dict:
        with self._lock:
            self._load()
            self._ops[record["op_id"]] = dict(record)
            self._trim()
            self._write_file()
            return json.loads(json.dumps(record))

    def update(self, op_id: str, **fields) -> dict | None:
        with self._lock:
            self._load()
            record = self._ops.get(op_id)
            if record is None:
                return None
            record.update(fields)
            record["updated_at"] = model_store.utc_now()
            self._write_file()
            return json.loads(json.dumps(record))


def _ws_key(workspace: str) -> str:
    return os.path.normcase(os.path.normpath(str(workspace or "")))


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------

class ImportOrchestrator:
    """同一工作区的导入串行执行 + 一条操作记录 + 统一的取消传播。

    串行是**必须**的：两个导入并发改同一份 lib.defs 会写出重复条目，而
    "重复挂接"是用户明确要求避免的。锁按工作区分（不同工程互不阻塞）。
    """

    def __init__(self) -> None:
        self._store = _OpStore()
        self._guard = threading.Lock()
        self._events: dict = {}          # op_id -> threading.Event（取消）
        self._done: dict = {}            # op_id -> threading.Event（终结）
        self._threads: dict = {}         # op_id -> Thread
        self._ws_locks: dict = {}        # workspace key -> threading.Lock
        self._held_locks: dict = {}      # workspace key -> op_id（持有者，用于排查）

    # -- 锁 -----------------------------------------------------------
    def ws_lock(self, workspace: str) -> threading.Lock:
        key = _ws_key(workspace)
        with self._guard:
            lock = self._ws_locks.get(key)
            if lock is None:
                if len(self._ws_locks) > 64:      # 换过很多工作区也别无限堆积
                    for k in list(self._ws_locks)[:32]:
                        self._ws_locks.pop(k, None)
                lock = threading.Lock()
                self._ws_locks[key] = lock
            return lock

    # -- 查询 ---------------------------------------------------------
    def get_op(self, op_id: str) -> dict | None:
        return self._store.get(op_id)

    def list_ops(self, workspace: str = "", limit: int = 50) -> list:
        ops = list(self._store.all().values())
        if workspace:
            key = _ws_key(workspace)
            ops = [o for o in ops if _ws_key(o.get("workspace")) == key]
        ops.sort(key=lambda o: str(o.get("created_at") or ""), reverse=True)
        return ops[:max(1, int(limit))]

    def _active_for(self, workspace: str, package_id: str) -> dict | None:
        """同一工作区同一包正在进行中的操作（用于并入，不另起一条）。"""
        key = _ws_key(workspace)
        for op in self._store.all().values():
            if (str(op.get("state")) in OP_ACTIVE
                    and _ws_key(op.get("workspace")) == key
                    and str(op.get("package_id")) == str(package_id)):
                return op
        return None

    def _by_request_key(self, request_key: str) -> dict | None:
        if not request_key:
            return None
        for op in self._store.all().values():
            if str(op.get("request_key") or "") == request_key:
                return op
        return None

    # -- 提交 ---------------------------------------------------------
    def submit(self, cfg: dict, workspace: str, package_id: str, *,
               kit_root: str = "", source: str = "http", request_key: str = "",
               identity: dict | None = None, vendor_filter: str = "") -> tuple:
        """受理一次导入并**立刻返回**（后台线程跑流水线）。

        返回 ``(op, replayed)``：``replayed=True`` 表示没有新起操作，而是
        并入了已有的一条（客户端重试 / 按钮与 LLM 同时点）。调用方据此把
        同一个 op_id 回给两边，两边查到的就是同一条进度。
        """
        root = model_store.store_root(workspace)
        record = _lookup_package(root, package_id)
        if record is None:
            raise OrchestrationError(
                f"当前工作区里没有模型包 {package_id}。"
                f"模型资产归属于上传时所在的工作区，换工程后不会自动带过来。")
        identity = identity or {
            "package_id": package_id,
            "sha256": record.get("sha256"),
            "filename": record.get("original_filename"),
        }

        with self._guard:
            # 幂等 1：客户端带了同一个 request_key（重复提交 / 重试）
            existing = self._by_request_key(request_key)
            if existing is not None:
                return existing, True
            # 幂等 2：同工作区同包已有进行中的操作 —— 并入，不并发
            existing = self._active_for(workspace, package_id)
            if existing is not None:
                if str(existing.get("state")) == OP_AWAITING_USER and kit_root:
                    choices = ((existing.get("result") or {}).get(
                        "kit_root_candidates") or [])
                    normalized = os.path.normcase(os.path.normpath(kit_root))
                    if not any(os.path.normcase(os.path.normpath(str(choice))) == normalized
                               for choice in choices):
                        raise OrchestrationError(
                            "所选 kit_root 不在该模型包提供的候选目录中；请先查看包详情并选择候选目录。")
                    # 延续同一条操作记录，并重跑幂等流水线；原包只回到 saved，
                    # 由流水线重新扫描并继续解压/挂接。
                    try:
                        model_store.set_state(root, package_id, model_store.STATE_SAVED)
                    except model_store.ModelStoreError as e:
                        raise OrchestrationError(
                            f"无法恢复模型包的待导入状态，不能继续：{e}") from e
                    args = dict(existing.get("args") or {})
                    args.update({"kit_root": kit_root,
                                 "vendor_filter": vendor_filter or args.get("vendor_filter", "")})
                    steps = list(existing.get("steps") or [])
                    steps.append({"step": "user_choice", "ok": True,
                                  "detail": f"用户选择套件根目录：{kit_root}",
                                  "at": model_store.utc_now()})
                    self._store.update(
                        existing["op_id"], args=args, state=OP_QUEUED,
                        state_label=OP_LABELS[OP_QUEUED], phase="queued",
                        started_at=model_store.utc_now(), finished_at="",
                        error="", error_kind="", result=None,
                        partial=False, steps=steps)
                    self._events[existing["op_id"]] = threading.Event()
                    self._done[existing["op_id"]] = threading.Event()
                    resume_id = existing["op_id"]
                else:
                    return existing, True
            else:
                resume_id = ""

            if not resume_id:
                op_id = uuid.uuid4().hex
                op = _blank_op(op_id, workspace, package_id, identity, source,
                               request_key)
                op["args"] = {"kit_root": kit_root, "vendor_filter": vendor_filter}
                self._events[op_id] = threading.Event()
                self._done[op_id] = threading.Event()
                self._store.put(op)
            else:
                op_id = resume_id

        thread = threading.Thread(target=self._worker,
                                  args=(op_id, cfg), daemon=True,
                                  name=f"model-import-{op_id[:8]}")
        with self._guard:
            self._threads[op_id] = thread
        thread.start()
        return self.get_op(op_id), False

    def run_sync(self, cfg: dict, workspace: str, package_id: str, *,
                 kit_root: str = "", source: str = "llm",
                 cancel_event=None, identity: dict | None = None,
                 vendor_filter: str = "") -> dict:
        """在**当前线程**跑完流水线并返回最终操作记录。

        LLM 工具入口走这里：它必须等结果（工具调用是同步的），但用的是
        与附件按钮**完全相同**的流水线与工作区锁 —— 所以按钮先点的导入
        会先跑完，LLM 这次要么并入它、要么排在它后面。
        """
        root = model_store.store_root(workspace)
        record = _lookup_package(root, package_id)
        if record is None:
            raise OrchestrationError(f"当前工作区里没有模型包 {package_id}")
        identity = identity or {
            "package_id": package_id,
            "sha256": record.get("sha256"),
            "filename": record.get("original_filename"),
        }

        with self._guard:
            existing = self._active_for(workspace, package_id)
            resume_waiting = bool(existing
                                  and existing.get("state") == OP_AWAITING_USER
                                  and kit_root)
            if existing is not None:
                op_id = existing["op_id"]
            else:
                op_id = uuid.uuid4().hex
                op = _blank_op(op_id, workspace, package_id, identity, source, "")
                op["args"] = {"kit_root": kit_root, "vendor_filter": vendor_filter}
                op["state"] = OP_RUNNING
                op["state_label"] = OP_LABELS[OP_RUNNING]
                op["started_at"] = model_store.utc_now()
                self._events[op_id] = threading.Event()
                self._done[op_id] = threading.Event()
                self._store.put(op)
        if resume_waiting:
            resumed, _ = self.submit(
                cfg, workspace, package_id, kit_root=kit_root, source=source,
                identity=identity, vendor_filter=vendor_filter)
            return self._join(resumed["op_id"], cancel_event=cancel_event,
                              timeout=_SYNC_JOIN_TIMEOUT)
        if existing is not None:
            return self._join(op_id, cancel_event=cancel_event,
                              timeout=_SYNC_JOIN_TIMEOUT)

        # 排队等锁（可取消），然后跑流水线
        event = self._events[op_id]
        if cancel_event is not None and cancel_event.is_set():
            event.set()
        try:
            if not self._acquire_lock(op_id, op["workspace"], event,
                                      extra_event=cancel_event):
                return self.get_op(op_id)
            try:
                self._pipeline(op_id, cfg, event, extra_event=cancel_event)
            finally:
                self._release_lock(op_id, op["workspace"])
        finally:
            self._done.get(op_id, threading.Event()).set()
        return self.get_op(op_id)

    # -- 后台线程 -----------------------------------------------------
    def _worker(self, op_id: str, cfg: dict) -> None:
        op = self.get_op(op_id) or {}
        event = self._events.get(op_id) or threading.Event()
        workspace = op.get("workspace", "")
        try:
            if not self._acquire_lock(op_id, workspace, event):
                return
            try:
                self._pipeline(op_id, cfg, event)
            finally:
                self._release_lock(op_id, workspace)
        except Exception as e:  # noqa: BLE001 — 后台线程崩了也要把操作收尾
            log.exception("导入操作 %s 线程异常: %s: %s", op_id,
                          type(e).__name__, e)
            self._finish(op_id, OP_FAILED, phase="done",
                         error=f"导入线程异常：{type(e).__name__}: {e}",
                         error_kind="internal_error")
        finally:
            self._done.get(op_id, threading.Event()).set()

    def _release_lock(self, op_id: str, workspace: str) -> None:
        """释放本操作持有的工作区锁。

        **必须释放**：锁是"同工作区串行"的唯一保证，但漏释放会把串行
        变成死锁 —— 该工作区之后的每一次导入都卡在 waiting_lock 永不
        执行（"第一次能导入，第二次永远转圈"）。异常路径同样要放，
        所以调用点一律放在 finally 里。
        """
        key = _ws_key(workspace)
        with self._guard:
            if key in self._held_locks:
                self._held_locks.pop(key, None)
        lock = self.ws_lock(workspace)
        try:
            lock.release()
        except RuntimeError:
            # 本线程并未持有（理论上不该发生）：不能让它盖掉真正的结果
            log.warning("导入操作 %s 释放工作区锁时该线程并未持有", op_id)

    def _acquire_lock(self, op_id: str, workspace: str, event: threading.Event,
                      extra_event=None) -> bool:
        """等工作区锁；期间**持续检查取消**（排队中的取消必须生效）。"""
        op = self.get_op(op_id) or {}
        root = model_store.store_root(str(op.get("workspace") or ""))
        package_id = str(op.get("package_id") or "")

        def _cancel_while_queued(message: str) -> dict:
            # 排队阶段取消同样要让资产状态跟着走：否则包会永远停在
            # saved / pending_import，界面看不出"这次导入被用户取消了"。
            try:
                if package_id:
                    _set_package_state(root, package_id,
                                       model_store.STATE_CANCELLED)
            except Exception:  # noqa: BLE001 — 状态写不进去也要把操作收尾
                log.warning("排队取消时标记包 %s 状态失败", package_id)
            return self._finish(op_id, OP_CANCELLED, phase="done",
                                message=message)

        self._store.update(op_id, phase="waiting_lock")
        lock = self.ws_lock(workspace)
        while not lock.acquire(timeout=_LOCK_POLL):
            if event.is_set() or (extra_event is not None and extra_event.is_set()):
                event.set()
                return _cancel_while_queued(
                    "已在排队阶段取消，未执行任何导入动作。")
        if event.is_set() or (extra_event is not None and extra_event.is_set()):
            event.set()
            lock.release()
            with self._guard:
                self._held_locks.pop(_ws_key(workspace), None)
            return _cancel_while_queued(
                "已在解压前取消，未执行任何导入动作。")
        self._store.update(op_id, state=OP_RUNNING,
                           state_label=OP_LABELS[OP_RUNNING],
                           started_at=model_store.utc_now())
        with self._guard:
            self._held_locks[_ws_key(workspace)] = op_id
        return True

    # -- 取消 ---------------------------------------------------------
    def request_cancel(self, cfg: dict, op_id: str,
                       extra_event=None) -> dict:
        """请求取消一条操作。

        取消是**协作式**的：这里只置位事件并把请求记进记录，真正的停止
        发生在流水线的下一个边界（文件 / 数据块 / 阶段之间）。同时把
        ADS 端的 job_id 一起取消 —— 挂接一旦开始，本地停手而 ADS 端继续
        写 lib.defs 是最糟的结果。
        """
        op = self.get_op(op_id)
        if op is None:
            return {"ok": False, "known": False, "op_id": op_id,
                    "message": "该导入操作不存在或已被清理"}
        if str(op.get("state")) in OP_TERMINAL:
            return {"ok": False, "known": True, "op_id": op_id,
                    "state": op.get("state"),
                    "message": f"该导入已结束（{op.get('state_label')}），无需取消"}
        event = self._events.get(op_id)
        if event is not None:
            event.set()
        if extra_event is not None:
            extra_event.set()
        self._store.update(
            op_id, state=OP_CANCEL_REQUESTED,
            state_label=OP_LABELS[OP_CANCEL_REQUESTED],
            cancel_requested_at=model_store.utc_now(),
            message="已收到取消请求，正在最近的安全边界停下（已完成的前置步骤保留）。")
        ads_info = {}
        ads_job_id = str(op.get("ads_job_id") or "")
        if ads_job_id:
            ads_info = tools_mod.cancel_ads_jobs(cfg, [ads_job_id])
            log.info("导入操作 %s 取消已传播到 ADS 作业 %s: %s",
                     op_id, ads_job_id, ads_info)
        return {"ok": True, "known": True, "op_id": op_id,
                "state": OP_CANCEL_REQUESTED, "ads": ads_info}

    def _join(self, op_id: str, cancel_event=None, timeout: float = 0.0) -> dict:
        """等一条操作终结（LLM 并入按钮发起的导入时用）。"""
        thread = self._threads.get(op_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.5, timeout))
        op = self.get_op(op_id) or {}
        op = dict(op)
        op["joined"] = True
        if str(op.get("state")) in OP_TERMINAL:
            return op
        # 等不到就如实把"仍在进行"返回去，不假装完成
        op["note"] = ("已并入同一工作区正在进行中的同包导入，本次未另起导入。"
                      "当前进度如下，最终状态请再查一次操作记录。")
        return op

    # -- 流水线 -------------------------------------------------------
    def _pipeline(self, op_id: str, cfg: dict, event: threading.Event,
                  extra_event=None) -> dict:
        """scan → extract → index → attach。每个阶段边界都检查取消。"""
        op = self.get_op(op_id)
        workspace = op.get("workspace") or ""
        package_id = op.get("package_id") or ""
        root = model_store.store_root(workspace)
        args = op.get("args") or {}
        steps: list = []

        def _cancelled() -> bool:
            return event.is_set() or (extra_event is not None
                                      and extra_event.is_set())

        def _step(name: str, ok: bool, detail: str = "") -> None:
            steps.append({"step": name, "ok": bool(ok), "detail": str(detail),
                          "at": model_store.utc_now()})
            self._store.update(op_id, steps=list(steps), phase=name)

        def _stop_cancelled(where: str, message: str) -> dict:
            _step(where, False, "已取消")
            # 取消不是故障：资产状态只标 cancelled，不写 last_error
            # （写了界面就会把它当成错误红字显示，用户明明是自己点的取消）。
            _set_package_state(root, package_id, model_store.STATE_CANCELLED)
            return self._finish(op_id, OP_CANCELLED, phase="done",
                                steps=steps, message=message,
                                result={"package_id": package_id,
                                        "cancelled": True, "steps": steps})

        try:
            self._store.update(op_id, phase="scan", steps=steps)

            # --- 0. 身份核对：工作区还是不是提交时那个，包内容有没有被换掉 ---
            current = _current_workspace(cfg)
            if _ws_key(current) != _ws_key(workspace):
                _step("scan", False, "工作区已切换，已停止")
                return self._fail(
                    op_id, root, package_id, steps,
                    error=("导入目标工作区与 ADS 当前工作区不一致，已停止："
                           f"提交时是 {workspace}，现在是 {current}。"
                           "请在 ADS 里切回原工作区后重新提交导入 —— "
                           "往别的工作区里挂接库会改到不属于本次导入的库定义。"),
                    error_kind="workspace_mismatch")
            record = _lookup_package(root, package_id)
            if record is None:
                return self._fail(op_id, root, package_id, steps,
                                  error=f"工作区里已经没有模型包 {package_id}",
                                  error_kind="package_missing")
            want = op.get("identity") or {}
            if want.get("sha256") and record.get("sha256") and \
                    want["sha256"] != record.get("sha256"):
                return self._fail(
                    op_id, root, package_id, steps,
                    error=(f"模型包 {package_id} 的内容与提交导入时不一致"
                           f"（SHA-256 已变），已停止。请重新提交导入。"),
                    error_kind="identity_mismatch")

            # 覆盖升级前已有的 Workspace 资产和旧上传入口。
            try:
                backup = shared_models.backup_package(cfg, workspace, record)
                _step("library_backup", True,
                      f"统一模型库已保存（复用={bool(backup.get('reused'))}）")
            except Exception as e:  # noqa: BLE001
                _step("library_backup", False, f"{type(e).__name__}: {e}")
                return self._fail(
                    op_id, root, package_id, steps,
                    error=(f"无法备份模型包到统一 libraries 目录，已停止本次导入："
                           f"{type(e).__name__}: {e}。请检查配置路径与磁盘空间后重试。"),
                    error_kind="library_backup_failed")

            # --- 1. 只读扫描（不解压）：识别类型与套件根 -------------------
            try:
                record = model_store.scan_archive(root, package_id)
                _step("scan", True, record.get("package_kind") or "")
            except Exception as e:  # noqa: BLE001
                return self._fail(op_id, root, package_id, steps,
                                  error=f"扫描模型包失败：{type(e).__name__}: {e}",
                                  error_kind="scan_failed",
                                  step=("scan", False, f"{type(e).__name__}: {e}"))

            # --- 2. 多候选套件根：不武断，交回给用户 ----------------------
            attach_info = record.get("library_attach") or {}
            candidates = attach_info.get("kit_root_candidates") or []
            kit_root = str(args.get("kit_root") or "").strip()
            if not kit_root and len(candidates) > 1:
                _step("kit_root", False, f"{len(candidates)} 个候选，需用户指定")
                _set_package_state(root, package_id,
                                   model_store.STATE_AWAITING_USER)
                return self._finish(
                    op_id, OP_AWAITING_USER, phase="done", steps=steps,
                    error=(f"包里有多个候选套件根目录：{candidates}。"
                           f"请用 inspect_model_package 看结构后，"
                           f"在 kit_root 参数里明确指定一个。"),
                    error_kind="kit_root_ambiguous",
                    result={"package_id": package_id,
                            "kit_root_candidates": candidates, "steps": steps})
            if not kit_root and candidates:
                kit_root = candidates[0]

            if _cancelled():
                return _stop_cancelled("extract",
                                       "已在解压前取消，未执行解压与挂接。")

            # --- 3. 解压（文件边界检查取消） ------------------------------
            try:
                _set_state_if(root, package_id, model_store.STATE_INSPECTING,
                              allowed=(model_store.STATE_SAVED,))
                record = model_store.extract_package(root, package_id,
                                                     cancel_event=event)
                _step("extract", True, record.get("extract_relpath") or "")
            except model_store.OperationCancelled:
                return _stop_cancelled("extract",
                                       "已在解压过程中取消；未提交的解压产物已清理。")
            except model_store.UnsafeArchive as e:
                _step("extract", False, "安全校验未通过")
                return self._fail(
                    op_id, root, package_id, steps,
                    error=(f"解压被安全校验拒绝：{e}。原始 ZIP 已保留在 "
                           f"archives/ 下，未向 ADS 写入任何内容。"),
                    error_kind="unsafe_archive")
            except Exception as e:  # noqa: BLE001
                _step("extract", False, f"{type(e).__name__}: {e}")
                return self._fail(op_id, root, package_id, steps,
                                  error=f"解压失败：{type(e).__name__}: {e}",
                                  error_kind="extract_failed")

            # --- 4. 建索引（数据块边界检查取消） --------------------------
            try:
                models = model_store.index_models(root, package_id,
                                                  cancel_event=event)
                _step("index", True, f"{len(models)} 个型号")
            except model_store.OperationCancelled:
                return _stop_cancelled("index",
                                       "已在建立型号索引时取消；解压产物保留，"
                                       "可以稍后重新导入。")
            except Exception as e:  # noqa: BLE001
                _step("index", False, f"{type(e).__name__}: {e}")
                return self._fail(op_id, root, package_id, steps,
                                  error=f"建立型号索引失败：{type(e).__name__}: {e}",
                                  error_kind="index_failed")

            kind = record.get("package_kind")
            if _cancelled():
                return _stop_cancelled("attach",
                                       "已在挂接前取消；解压产物与索引保留，"
                                       "工作区库定义未改动。")

            # --- 5a. Touchstone：不挂接库 --------------------------------
            if kind == "touchstone":
                _set_state_if(root, package_id, model_store.STATE_IMPORTING)
                _attach_update(root, package_id, {
                    "mode": "touchstone_reference",
                    "note": "Touchstone 模型通过 S 参数元件的文件路径引用，"
                            "不需要挂接库；放置元件时用 validate_model_import 核对。",
                    "model_files": [m.get("relpath") for m in models
                                    if m.get("relpath")][:200],
                })
                _step("attach", True, "Touchstone：登记模型文件路径（不挂接库）")
                _set_package_state(root, package_id,
                                   model_store.STATE_PENDING_VERIFY)
                return self._finish(
                    op_id, OP_SUCCEEDED, phase="done", steps=steps,
                    message="已安全解压并建立索引。Touchstone 模型通过 S 参数"
                            "元件引用使用，还没有验证过元件能否真正放置 —— "
                            "请用 validate_model_import 验证。",
                    result=_result_body(op, kind, len(models), steps,
                                        model_store.STATE_PENDING_VERIFY))

            # --- 5b. 认不出的包：如实说不支持，不硬套 Design Kit ----------
            if kind not in ("design_kit", "mixed"):
                _set_state_if(root, package_id, model_store.STATE_IMPORTING)
                _set_package_state(root, package_id,
                                   model_store.STATE_PENDING_VERIFY)
                _step("attach", True, "未识别类型：不挂接库")
                return self._finish(
                    op_id, OP_SUCCEEDED, phase="done", steps=steps,
                    message=f"该包被识别为 {kind or '未识别'}，不属于当前支持"
                            f"自动导入的范围。内容已安全解压并可浏览，"
                            f"但没有挂接为 ADS 库。",
                    result={**_result_body(op, kind, len(models), steps,
                                           model_store.STATE_PENDING_VERIFY),
                            "supported": False})

            # --- 5c. Design Kit：挂接前再核对一次工作区 -------------------
            extract_abs = _resolve_extract_abs(workspace, record)
            current = _current_workspace(cfg)
            if _ws_key(current) != _ws_key(workspace):
                _step("attach", False, "挂接前发现工作区已切换")
                _set_package_state(root, package_id, model_store.STATE_FAILED,
                                   error="挂接前工作区已切换，已停止")
                return self._fail(
                    op_id, root, package_id, steps,
                    error=("挂接前核对发现 ADS 当前工作区已切换（提交时 "
                           f"{workspace}，现在 {current}），已停止挂接。"
                           "解压产物保留；未向任何工作区的库定义写入内容。"),
                    error_kind="workspace_mismatch")
            kit_root_abs = _join_kit_root(extract_abs, kit_root)
            _set_state_if(root, package_id, model_store.STATE_IMPORTING)

            # --- 5d. 挂接（ADS 端） --------------------------------------
            ads_job_id = f"{op_id}-attach"
            self._store.update(op_id, ads_job_id=ads_job_id)
            try:
                attach = tools_mod.call(cfg, "attach_design_kit", {
                    "package_id": package_id,
                    "kit_root": kit_root_abs,
                    "kit_root_rel": kit_root,
                    "workspace": workspace,           # 可信上下文，后端注入
                    "library_names": _lib_names(record, args.get("vendor_filter")),
                }, job_id=ads_job_id)
            except tools_mod.AdsToolError as e:
                _step("attach", False, str(e))
                return self._fail(
                    op_id, root, package_id, steps,
                    error=(f"挂接到工作区失败：{e}。解压产物已保留在 "
                           f"extracted/ 下。"),
                    error_kind="ads_unreachable")
            except Exception as e:  # noqa: BLE001
                _step("attach", False, f"{type(e).__name__}: {e}")
                return self._fail(
                    op_id, root, package_id, steps,
                    error=f"挂接失败：{type(e).__name__}: {e}",
                    error_kind="attach_failed")

            return self._interpret_attach(op_id, root, package_id, steps, kind,
                                          models, attach, kit_root)

        finally:
            self._done.get(op_id, threading.Event()).set()

    # -- ADS 挂接结果判定 ---------------------------------------------
    def _interpret_attach(self, op_id, root, package_id, steps, kind, models,
                          attach: dict, kit_root: str) -> dict:
        """按 ADS 返回的**业务结果**判定，而不只是"没抛异常"。

        ``tools.call`` 会把 ADS 端 JSON 原样返回：``ok=False`` 不一定抛异常。
        过去编排层只看有没有异常，于是 ADS 返回 ``ok=False,
        kind=workspace_mismatch`` 时后端仍记录 ``attach ok=True, 0 个库``
        并把包推进 pending_verify —— 界面上看起来"导入成功了"。
        """
        op = self.get_op(op_id) or {}
        if not isinstance(attach, dict):
            _step_fail = ("attach", False, "ADS 返回了非对象结果")
            steps.append({"step": _step_fail[0], "ok": False,
                          "detail": _step_fail[1], "at": model_store.utc_now()})
            return self._fail(op_id, root, package_id, steps,
                              error=f"挂接返回了无法解析的结果：{attach!r}",
                              error_kind="bad_result")

        attached = [x for x in (attach.get("libraries") or [])
                    if isinstance(x, dict)]
        conflicts = attach.get("conflicts") or []
        failed_libs = attach.get("failed") or []
        cancelled = bool(attach.get("cancelled"))
        error_kind = str(attach.get("kind") or "")
        touched_lib_defs = bool(attach.get("lib_defs_fallback"))

        # 取消：已完成的部分挂接**保留**（删别人的库引用是破坏性的），
        # 只如实说明"停在哪、留下了什么"。
        if cancelled:
            steps.append({"step": "attach", "ok": False, "detail": "已取消",
                          "at": model_store.utc_now()})
            _set_package_state(root, package_id, model_store.STATE_CANCELLED)
            return self._finish(
                op_id, OP_CANCELLED, phase="done", steps=steps, partial=True,
                message=("已在挂接过程中取消。"
                         + (f"已挂接的 {len(attached)} 个库保留（{', '.join(_lib_names_of(attached))}），"
                            if attached else "")
                         + "未完成的库不再继续挂接。"),
                result={"package_id": package_id, "cancelled": True,
                        "attached_libraries": attached, "steps": steps})

        # 工作区不一致：立刻停，不写任何东西
        if error_kind == "workspace_mismatch" or (
                not attached and not conflicts and not failed_libs
                and str(attach.get("error") or "").find("工作区不一致") >= 0):
            steps.append({"step": "attach", "ok": False,
                          "detail": "工作区不一致", "at": model_store.utc_now()})
            _set_package_state(root, package_id, model_store.STATE_FAILED,
                               error=str(attach.get("error") or "工作区不一致"))
            return self._fail(
                op_id, root, package_id, steps,
                error=str(attach.get("error") or
                          "ADS 报告工作区不一致，已停止挂接。"),
                error_kind="workspace_mismatch")

        attach_info = {
            "mode": "workspace_read_only",
            "attached": bool(attached),
            "libraries": attached,
            "already_attached": bool(attach.get("already_attached")),
            "attached_at": attach.get("attached_at") or "",
            "kit_root": kit_root,
            "conflicts": conflicts,
            "failed": failed_libs,
        }
        _attach_update(root, package_id, attach_info)

        # 零成功不算完成：一个库都没挂上时绝不给 succeeded
        if not attached:
            steps.append({"step": "attach", "ok": False,
                          "detail": "0 个库", "at": model_store.utc_now()})
            reason = str(attach.get("error") or "")
            detail = []
            if failed_libs:
                detail.append("失败: " + "; ".join(
                    f"{f.get('name')}: {f.get('reason')}" for f in failed_libs))
            if conflicts:
                detail.append("冲突: " + "; ".join(
                    f"{c.get('name')}（已存在 {c.get('existing_path')}）"
                    for c in conflicts))
            # **不能**无条件声称"未修改库定义"：走了 lib.defs 退路时
            # 工作区的 lib.defs 确实被改过 —— 那是要在报告里说清的副作用。
            unchanged = "，工作区库定义未被修改" if not touched_lib_defs else \
                        "；注意：本次走了直接改写工作区 lib.defs 的退路，" \
                        "该文件可能已被修改"
            return self._fail(
                op_id, root, package_id, steps,
                error=("没有成功挂接任何库，导入未完成。"
                       + (f"（{'；'.join(detail)}）" if detail else
                          (f"（{reason}）" if reason else ""))
                       + unchanged + "。解压产物保留在 extracted/ 下。"),
                error_kind="conflicts" if conflicts and not failed_libs
                           else "attach_failed")

        steps.append({"step": "attach", "ok": not failed_libs,
                      "detail": (f"{len(attached)} 个库"
                                 + ("（此前已挂接，未重复添加）"
                                    if attach_info["already_attached"] else "")),
                      "at": model_store.utc_now()})

        # 同名库冲突：已挂上一部分，剩下的要用户决定 —— 不替用户选
        if conflicts:
            _set_package_state(root, package_id, model_store.STATE_AWAITING_USER,
                               error="存在同名库冲突，等待用户决定")
            return self._finish(
                op_id, OP_AWAITING_USER, phase="done", steps=steps, partial=True,
                error=(f"已挂接 {len(attached)} 个库，但有 "
                       f"{len(conflicts)} 个同名库指向不同路径，已保留原有引用"
                       f"未替换：{', '.join(str(c.get('name')) for c in conflicts)}。"
                       f"两个套件可能有同名库，需要你决定用哪个。"),
                error_kind="conflicts",
                result={"package_id": package_id, "attached_libraries": attached,
                        "conflicts": conflicts, "steps": steps})

        # 部分失败：保留真实记录，说清哪部分没成
        if failed_libs:
            _set_package_state(
                root, package_id, model_store.STATE_FAILED,
                error="部分库挂接失败：" + "; ".join(
                    f"{f.get('name')}: {f.get('reason')}" for f in failed_libs))
            return self._finish(
                op_id, OP_FAILED, phase="done", steps=steps, partial=True,
                error=(f"{len(attached)} 个库已挂接，{len(failed_libs)} 个失败："
                       + "; ".join(f"{f.get('name')}: {f.get('reason')}"
                                   for f in failed_libs)),
                error_kind="partial",
                result={"package_id": package_id, "attached_libraries": attached,
                        "failed": failed_libs, "steps": steps})

        _set_package_state(root, package_id, model_store.STATE_PENDING_VERIFY)
        # 挂接成功后追加一条**可执行**的原生列表提示：面板据此渲染"在 ADS
        # 元件列表中打开"按钮，LLM 也可据此调 open_vendor_palette。这只是一条
        # "可以去看"的入口，**不代表模型可用** —— pending_verify 语义与此无关，
        # 仍需 validate_model_import 才能进电路。
        first_lib = _lib_names_of(attached)
        native_hint = (
            "库已挂接。可打开 ADS 原生元件列表查看该包分类："
            "面板点「在 ADS 元件列表中打开」，或调用 "
            f"open_vendor_palette(package_id=\"{package_id}\""
            + (f", library=\"{first_lib[0]}\"" if first_lib else "")
            + ")。打开原生列表只证明『能看见』，不等于模型可用。")
        steps.append({"step": "native_list", "ok": True, "detail": native_hint,
                      "at": model_store.utc_now()})
        return self._finish(
            op_id, OP_SUCCEEDED, phase="done", steps=steps,
            message="已解压、建索引并挂接到当前工作区（只读）。"
                    "**挂接成功不等于模型可用** —— 请用 list_vendor_models 找元件、"
                    "再用 validate_model_import 验证后才能放进电路仿真。"
                    "若想在 ADS 原生元件列表里查看该包分类，可调用 "
                    "open_vendor_palette。",
            result={**_result_body(op, kind, len(models), steps,
                                   model_store.STATE_PENDING_VERIFY),
                    "library_attach": attach_info,
                    "native_list": {
                        "available": True,
                        "tool": "open_vendor_palette",
                        "package_id": package_id,
                        "first_library": first_lib[0] if first_lib else "",
                        "hint": native_hint,
                        "note": ("打开原生元件列表只代表库/分类可见，"
                                 "**不代表模型可用**；仍需 validate_model_import。"),
                    }})

    # -- 收尾 ---------------------------------------------------------
    def _fail(self, op_id, root, package_id, steps, error, error_kind,
              step=None):
        if step:
            steps.append({"step": step[0], "ok": step[1], "detail": step[2],
                          "at": model_store.utc_now()})
        try:
            _set_package_state(root, package_id, model_store.STATE_FAILED,
                               error=str(error))
        except Exception:  # noqa: BLE001 — 资产状态写不进去也要把操作收尾
            log.warning("标记包 %s 失败时出错（操作仍会记为失败）", package_id)
        return self._finish(op_id, OP_FAILED, phase="done", steps=steps,
                            error=str(error), error_kind=error_kind,
                            result={"package_id": package_id, "steps": steps})

    def _finish(self, op_id, state, *, phase="", steps=None, message="",
                error="", error_kind="", partial=False, result=None) -> dict:
        fields = {"state": state, "state_label": OP_LABELS[state],
                  "finished_at": model_store.utc_now(), "partial": bool(partial)}
        if phase:
            fields["phase"] = phase
        if steps is not None:
            fields["steps"] = steps
        if message:
            fields["message"] = message
        if error:
            fields["error"] = str(error)
        if error_kind:
            fields["error_kind"] = error_kind
        if result is not None:
            fields["result"] = result
        record = self._store.update(op_id, **fields)
        log.info("导入操作 %s -> %s%s", op_id, state,
                 f"（{error_kind}）" if error_kind else "")
        return record or {}

    # -- 等待 ---------------------------------------------------------
    def wait(self, op_id: str, timeout: float) -> dict | None:
        """等操作终结（供调用方/测试等待）。返回最终记录或 None（超时）。"""
        thread = self._threads.get(op_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        return self.get_op(op_id)


# LLM 同步调用并入已有操作时最多等这么久（工具调用不能无限阻塞对话）。
_SYNC_JOIN_TIMEOUT = 600.0


# ---------------------------------------------------------------------------
# 小工具（与 model_tools 共用同一套判定，避免两处规则分叉）
# ---------------------------------------------------------------------------

def _lookup_package(root: str, package_id: str) -> dict | None:
    """取一条清单记录，不存在返回 None（不抛）。"""
    try:
        return model_store.get_package(root, package_id)
    except model_store.ModelStoreError:
        return None


def _current_workspace(cfg: dict) -> str:
    """当前 ADS 打开的工作区路径（与 model_tools.current_workspace 同一实现）。

    这里**延迟**导入 model_tools：它在模块级导入本模块（LLM 入口要用
    编排器），立刻互相导入会形成循环。
    """
    import model_tools

    return model_tools.current_workspace(cfg)


def _set_package_state(root, package_id, state, error: str = "") -> None:
    """写资产状态；状态机不允许时记录而不抛出（操作记录不能因此断掉）。"""
    try:
        model_store.set_state(root, package_id, state, error=error)
    except model_store.ModelStoreError as e:
        log.warning("包 %s 置为 %s 失败（资产状态机的流转限制）: %s",
                    package_id, state, e)


def _set_state_if(root, package_id, state, allowed: tuple = ()) -> None:
    """只在当前状态属于 ``allowed`` 时流转（重复导入时状态早已在路上）。"""
    record = _lookup_package(root, package_id)
    if record is None:
        return
    if allowed and str(record.get("state")) not in allowed:
        return
    _set_package_state(root, package_id, state)


def _attach_update(root: str, package_id: str, attach_info: dict) -> dict | None:
    """把挂接结果**并进** library_attach（整体替换会冲掉扫描阶段的候选套件根）。"""
    record = _lookup_package(root, package_id)
    if record is None:
        return None
    current = dict(record.get("library_attach") or {})
    current.update(attach_info)
    try:
        return model_store.update_package(root, package_id,
                                          library_attach=current)
    except model_store.ModelStoreError as e:
        log.warning("回写挂接结果失败 %s: %s", package_id, e)
        return None


def _resolve_extract_abs(workspace: str, rec: dict) -> str:
    rel = str(rec.get("extract_relpath") or "").strip()
    if not rel:
        raise OrchestrationError(f"包 {rec.get('package_id')} 尚未解压，无法挂接")
    root = model_store.store_root(workspace)
    abs_path = os.path.normpath(os.path.join(workspace, rel))
    norm_root = os.path.normpath(root)
    if not (abs_path == norm_root or abs_path.startswith(norm_root + os.sep)):
        raise OrchestrationError(
            f"解压目录 {rel} 不在模型资产根目录内，已阻止继续。请重新上传该压缩包。")
    if not os.path.isdir(abs_path):
        raise OrchestrationError(
            f"解压目录不存在：{rel}。工程可能已被移动或删除 —— 请重新上传该压缩包。")
    return abs_path


def _join_kit_root(extract_abs: str, kit_root: str) -> str:
    if not kit_root:
        return extract_abs
    joined = os.path.normpath(os.path.join(extract_abs, kit_root))
    if not joined.startswith(os.path.normpath(extract_abs) + os.sep):
        raise OrchestrationError(f"套件根路径 {kit_root} 越出了解压目录，已阻止")
    if not os.path.isdir(joined):
        raise OrchestrationError(f"套件根目录不存在：{kit_root}")
    return joined


def _lib_names(rec: dict, vendor_filter: str = "") -> list:
    """从包里挑要挂接的库名（优先 lib.defs 里 DEFINE 的权威集合）。"""
    defined = (rec.get("library_attach") or {}).get("defined_libraries") or []
    names = [str(x.get("name")) for x in defined
             if isinstance(x, dict) and x.get("name")]
    if not names:
        return []
    vendor = str(vendor_filter or "").strip().lower()
    if not vendor:
        return names
    return [n for n in names if vendor in n.lower()]


def _lib_names_of(libraries: list) -> list:
    return [str(x.get("name")) for x in libraries if isinstance(x, dict)
            and x.get("name")]


def _result_body(op: dict, kind, model_count: int, steps: list,
                 package_state: str) -> dict:
    from model_tools import STATE_LABELS  # 延迟导入，见 _current_workspace 的说明

    return {
        "package_id": op.get("package_id"),
        "state": package_state,
        "state_label": STATE_LABELS.get(package_state, package_state),
        "kind": kind,
        "workspace": op.get("workspace"),
        "model_count": model_count,
        "steps": steps,
        "op_id": op.get("op_id"),
        "op_state": op.get("state"),
    }


# ---------------------------------------------------------------------------
# 启动恢复
# ---------------------------------------------------------------------------

def recover_interrupted_ops() -> list:
    """把上次没跑完的操作标记为 ``interrupted``。

    后端可能在解压/挂接中途退出，磁盘上的记录还停在 running。没有证据表明
    它跑完了，所以**不能**留成 running（界面会永远转圈），也**不能**标成
    failed（那等于断言"导入失败了"，而实际是"不知道"）。产物原样保留，
    由用户决定重新提交还是放弃。
    """
    store = get_orchestrator()._store
    ops = store.all()
    recovered = []
    # awaiting_user **不**恢复成 interrupted：那是"等你决定"的状态，重启后
    # 用户的决定依然有效（候选套件根、同名库冲突都还在），不该被抹掉。
    unfinished = (OP_QUEUED, OP_RUNNING, OP_CANCEL_REQUESTED)
    for op_id, op in ops.items():
        if str(op.get("state")) not in unfinished:
            continue
        workspace = str(op.get("workspace") or "")
        if workspace:
            _reconcile_package(workspace, str(op.get("package_id") or ""))
        store.update(
            op_id, state=OP_INTERRUPTED, state_label=OP_LABELS[OP_INTERRUPTED],
            phase="done",
            error="后端在执行中途退出，本次导入未能确认完成。解压产物与已挂接的"
                  "库（如果有）都已保留，请查看后重新提交导入 —— "
                  "不能默认视为已完成。",
            error_kind="interrupted",
            message="上次运行中断，待确认")
        recovered.append(op_id)
    if recovered:
        log.warning("启动恢复：%d 个模型导入操作中断在未完成状态 %s",
                    len(recovered), recovered)
    return recovered


def _reconcile_package(workspace: str, package_id: str) -> None:
    """中断后把资产状态从"进行中"拉回可重新导入的状态。

    卡在 inspecting / importing 的包，界面会一直显示"导入中"。状态机里
    failed 可达 inspecting/importing，且 failed 允许重新走 inspecting，
    所以标成 failed（附原因）是既诚实又可恢复的那一个。
    """
    if not package_id:
        return
    try:
        root = model_store.store_root(workspace)
        record = _lookup_package(root, package_id)
    except model_store.ModelStoreError:
        return
    if record is None:
        return
    state = str(record.get("state") or "")
    if state in (model_store.STATE_INSPECTING, model_store.STATE_IMPORTING):
        _set_package_state(root, package_id, model_store.STATE_FAILED,
                           error="后端在导入过程中退出，本次导入未能确认完成；"
                                 "产物已保留，可重新提交导入。")


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------

_ORCHESTRATOR = None
_ORCH_LOCK = threading.Lock()


def get_orchestrator() -> ImportOrchestrator:
    """进程内唯一的编排器（两个入口必须共用同一把锁与同一份记录）。"""
    global _ORCHESTRATOR
    with _ORCH_LOCK:
        if _ORCHESTRATOR is None:
            _ORCHESTRATOR = ImportOrchestrator()
        return _ORCHESTRATOR


def reset_orchestrator() -> None:
    """仅供测试：丢弃进程内单例（换数据目录后不该沿用旧的登记表）。"""
    global _ORCHESTRATOR
    with _ORCH_LOCK:
        _ORCHESTRATOR = None
