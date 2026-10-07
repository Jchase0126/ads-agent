"""共享回环令牌 + **配置文件的唯一写入通道** —— 后端 (backend/) 与 ADS 端 (addon/ads_agent/) 共用。

为什么需要这个模块
------------------
1) 令牌过去是写死的 ``ads-agent-local-token``，而且它同时出现在 config.example.ini、
   README、tests/*.py 和代码默认值里 —— 等于公开。任何能访问 127.0.0.1:8760 / :8761
   的本机进程都可以驱动 Agent 在 ADS 进程里执行任意 Python。

   现在改为：首次启动 / 安装时**随机生成**令牌，写进 config.ini 的 ``[ads] token``；
   两端都**只**通过本模块读写这个值，不再各自持有"默认常量"——
   如果两端各自维护默认值，就会出现"一端生成了新令牌、另一端还在用默认值"，
   于是所有请求 401。检测到旧的公开默认值（``LEGACY_DEFAULT_TOKEN``）时自动轮换。

2) config.ini 有**两个**写入方：令牌的生成/轮换（本模块）与 LLM 设置的保存
   （``backend/config.py``）。两者都是"读整个文件 → 改几行 → 写回"，
   如果不共用一套协调机制就会互相覆盖 —— 典型症状是"刚轮换的令牌被一次
   保存 API 设置的操作还原成公开默认值"，或者反过来"刚填的 model 被令牌轮换抹掉"。

   所以本模块同时提供**唯一**的写入原语 :func:`edit_config`：
   跨进程排他锁（O_CREAT|O_EXCL，带残留清理、同线程可重入）+ 原子替换
   （临时文件 + fsync + ``os.replace``）。**所有**写 config.ini 的代码都必须走它。

本模块必须**自包含**（只用标准库）：ADS 进程会通过
``addon/ads_agent/authbridge.py`` 以文件路径方式加载它，那时 ``backend/`` 不在
sys.path 上，所以不能 import 同目录的其它模块。

安全约定：令牌绝不写入日志、异常消息或 HTTP 响应体；需要展示时只用 ``mask()``。
"""

import configparser
import hmac
import os
import random
import re
import secrets
import sys
import threading
import time

# 两端共用的请求头名
TOKEN_HEADER = "X-Ads-Agent-Token"

# 旧版本里公开的默认令牌。检测到它就说明配置是"人人皆知"的状态，必须轮换。
LEGACY_DEFAULT_TOKEN = "ads-agent-local-token"

# secrets.token_urlsafe(32) -> 43 个字符
TOKEN_BYTES = 32
# 低于这个长度视为弱令牌（只警告，不擅自改动用户自己设的值）
MIN_STRONG_LEN = 24

# 允许把配置指到别处（测试、便携安装）。留空则用 %LOCALAPPDATA%\ADSAgent\config.ini
CONFIG_ENV = "ADS_AGENT_CONFIG"

_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 由 authbridge 在 ADS 进程内覆写（那时 __file__ 可能不可靠）
_FORCED_ROOT: str | None = None

# 路径统一走 backend/paths.py —— **不要**在这里另算一遍，否则两端会读到
# 不同的配置文件（表现就是"随机 401"）。本进程里 backend/ 不在 sys.path 上，
# 所以按文件位置加载同一个 paths.py（同进程只有一份实例）。
_PATHS = None


def _paths():
    global _PATHS
    if _PATHS is None:
        import importlib.util

        mod = sys.modules.get("ads_agent_shared_paths")
        if mod is None:
            try:
                import paths as _plain  # type: ignore

                if hasattr(_plain, "config_path"):
                    mod = _plain
            except Exception:
                mod = None
        if mod is None:
            target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paths.py")
            spec = importlib.util.spec_from_file_location("ads_agent_shared_paths", target)
            if spec is None or spec.loader is None:     # pragma: no cover
                raise ImportError(f"无法加载共享路径模块: {target}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["ads_agent_shared_paths"] = mod
            spec.loader.exec_module(mod)
        _PATHS = mod
    try:
        # ADS 进程里会把 _FORCED_ROOT 写进来（见 authbridge），同步给 paths
        if _FORCED_ROOT:
            _PATHS.set_app_root(_FORCED_ROOT)
    except Exception:
        pass
    return _PATHS

# 锁文件：与 config.ini 同目录，名字带上"配置"而不只是"令牌"——
# 它保护的是整个文件的读-改-写。
_LOCK_SUFFIX = ".ads_agent_config.lock"
_LOCK_TIMEOUT = 20.0   # 等别人写完的上限。**必须大于 _LOCK_STALE**：
                       # 否则一个"没人持有但存在"的残留锁会让等待者在清理它之前
                       # 就先放弃，表现为随机卡顿 + 退化成无锁写入。
_LOCK_STALE = 15.0     # 超过这么久还没释放，视为残留，等待者可回收
_LOCK_POLL = 0.01      # 抢锁轮询间隔（临界区只有毫秒级，不需要等太久）
_LOCK_EPOCH = 1.0      # 用来把"残留锁"的 mtime 拨到 1970，让等待者立刻能回收

# Windows 上 os.replace 与 open 会互相打断（见 _replace_with_retry 的说明），
# 所以两边都要重试。实测：3 个线程持续读的情况下，写方平均重试 2 次、
# P90 重试 9 次；但如果读者是**不间断**的紧循环，几十次重试也可能全撞上，
# 所以写方的预算给得很足（200 次 × 约 4ms ≈ 0.8s 上限），
# 读方的预算小一些（读不到还可以退回缓存值，不值得让请求等太久）。
_WRITE_ATTEMPTS = 200
_READ_ATTEMPTS = 60
_RETRY_BASE = 0.002
_RETRY_JITTER = 0.003

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "token": ""}

_locks_guard = threading.Lock()
_locks: dict = {}

# 诊断用计数：没能拿到锁、退化为"无锁写入"的次数。正常应恒为 0。
_stats_lock = threading.Lock()
_stats = {"acquired": 0, "fallback": 0}


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def app_root() -> str:
    """代码所在目录（只读的程序文件）—— 见 backend/paths.py 的双根说明。"""
    return _paths().app_root()


def data_root() -> str:
    """用户数据根目录（配置、令牌、日志、会话、设计任务）。"""
    return _paths().data_root()


def project_root() -> str:
    """兼容旧名。**语义已变为"数据根目录"** —— 以前几个项目数据（design_jobs）
    跟代码放在一起，现在它们在 %LOCALAPPDATA%\\ADSAgent 下。
    """
    return data_root()


def config_path() -> str:
    override = (os.environ.get(CONFIG_ENV) or "").strip()
    if override:
        return override
    return _paths().config_path()


def _lock_path() -> str:
    directory = os.path.dirname(os.path.abspath(config_path())) or "."
    return os.path.join(directory, _LOCK_SUFFIX)


def _bump(key: str) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + 1


def lock_stats() -> dict:
    """写入协调的诊断信息（测试与排错用）。"""
    with _stats_lock:
        return dict(_stats)


def reset_lock_stats() -> None:
    with _stats_lock:
        _stats["acquired"] = 0
        _stats["fallback"] = 0


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def _stat_key(path: str):
    try:
        st = os.stat(path)
        return (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return (path, None, None)


def _retry_sleep() -> None:
    """重试前的短暂退避（带抖动，避免多次重试与读者节奏对齐）。"""
    time.sleep(_RETRY_BASE + random.random() * _RETRY_JITTER)


def _read_token_raw(path: str) -> str:
    """读一次令牌。**自己 open**，好让"文件被占用"这类 OSError 冒出来。

    不能用 ``parser.read(path)``：它内部 ``except OSError: continue``，
    会把"读不到"和"没配置"混成同一个结果 —— 前者被当成后者就会误判成
    "令牌没配"，接口一律 401（随机掉线的 401 就是这么来的）。
    """
    parser = configparser.ConfigParser()
    with open(path, encoding="utf-8") as f:
        parser.read_file(f)
    return (parser.get("ads", "token") or "").strip()


def _read_token_retry(path: str, attempts: int = _READ_ATTEMPTS):
    """带重试地读令牌。返回 None 表示"确实读不到"（不是"没配置"）。"""
    if not os.path.exists(path):
        return ""
    for _ in range(max(1, attempts)):
        try:
            return _read_token_raw(path)
        except FileNotFoundError:
            return ""
        except (configparser.Error, UnicodeDecodeError):
            return ""                        # 内容坏了：当作未配置
        except OSError:
            _retry_sleep()
    return None


def read_token() -> str:
    """当前令牌（读盘）；未配置时返回空串。

    刻意不做长期内存缓存：轮换令牌后，正在运行的进程下一次请求就能读到新值，
    不需要重启。

    但**读不到**时（Windows 上正好撞上 ``os.replace`` 的瞬间）必须退回上次
    成功读到的值 —— 绝不能返回空串，那会让 ``check_token()`` 一律拒绝，
    表现为"随机 401"。
    """
    path = config_path()
    before = _stat_key(path)
    token = _read_token_retry(path)
    after = _stat_key(path)

    if token is None:
        with _cache_lock:
            return _cache["token"]

    if before == after:          # 读的过程中文件没变，才敢缓存
        with _cache_lock:
            _cache["key"] = after
            _cache["token"] = token
    return token


def _invalidate_cache() -> None:
    with _cache_lock:
        _cache["key"] = None
        _cache["token"] = ""


def _read_lines() -> list:
    """整文件读成行列表（保留注释、空行、段落顺序）。

    文件存在但读不到时**抛异常**，绝不返回空列表 —— 调用方拿到空列表会写出
    一个"只有新内容"的配置文件，把用户的 LLM 设置、令牌全部抹掉。
    读不到就失败，比静默清空配置安全得多。
    """
    path = config_path()
    if not os.path.exists(path):
        return []
    for _ in range(max(1, _READ_ATTEMPTS)):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []
        except UnicodeDecodeError:
            return []
        except OSError:
            _retry_sleep()
    raise OSError(
        f"无法读取配置文件 {path}（正被其它进程占用？）。"
        f"为避免把配置写坏，本次修改已放弃。"
    )


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def is_legacy(token: str) -> bool:
    """是否是那个公开的默认令牌。"""
    return bool(token) and token == LEGACY_DEFAULT_TOKEN


def needs_rotation(token: str) -> bool:
    """空 / 公开默认值 -> 必须换掉。用户自己设的值一律尊重（即使偏短）。"""
    return (not token) or is_legacy(token)


def is_weak(token: str) -> bool:
    """长度不足的令牌只提示，不擅自改动。"""
    return bool(token) and len(token) < MIN_STRONG_LEN


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def mask(token: str) -> str:
    """可安全展示的脱敏形式（日志 / 界面用）。"""
    if not token:
        return ""
    if len(token) <= 8:
        return token[:2] + "****"
    return token[:4] + "****" + token[-4:]


# ---------------------------------------------------------------------------
# 跨进程写入协调
# ---------------------------------------------------------------------------

class ConfigLock:
    """config.ini 的写入锁：跨进程排他 + 同线程可重入。

    * **跨进程**：``os.open(O_CREAT|O_EXCL)`` 创建锁文件，抢不到就轮询；
      进程崩溃留下的陈旧锁会被清理（见 ``_LOCK_STALE``）。
    * **同线程可重入**：``ensure_token()`` 持锁时会调 ``_write_token()``，
      后者又要进锁 —— 用线程本地计数避免把自己锁死。
    * 不同线程各自计数，仍由文件锁串行化（线程 B 会真的等待线程 A 释放）。
    """

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        self._local = threading.local()

    # -- 可重入计数（按线程） ------------------------------------------------
    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    def _is_stale(self) -> bool:
        try:
            return (time.time() - os.path.getmtime(self.path)) > _LOCK_STALE
        except OSError:
            return False

    def acquire(self, timeout: float = _LOCK_TIMEOUT) -> bool:
        if self._depth > 0:                  # 本线程已持锁 -> 只加计数
            self._depth += 1
            return True
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self._depth = 1
                return True
            except FileExistsError:
                if self._is_stale():        # 上次崩溃留下的锁，清掉重试
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(_LOCK_POLL)
            except FileNotFoundError:
                return False                # 目录不存在等：交给调用方处理
            except OSError:
                # Windows 上"别人刚好在 unlink 锁文件"会让 O_CREAT|O_EXCL 报
                # EACCES/EPERM。这是瞬时的，**不能**当成"拿不到锁" ——
                # 那会直接退化成无锁写入，把并发保护白白丢掉。
                if time.time() >= deadline:
                    return False
                _retry_sleep()

    def release(self) -> None:
        if self._depth <= 0:
            return                          # 没持锁就 release：绝不能删掉别人的锁文件
        if self._depth > 1:
            self._depth -= 1
            return
        self._depth = 0
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        # 删锁也要重试：删不掉会留下一个"没人持有但存在"的锁文件，
        # 之后的写入会白等到 _LOCK_STALE 才敢清理。
        for _ in range(5):
            try:
                os.unlink(self.path)
                return
            except FileNotFoundError:
                return
            except OSError:
                _retry_sleep()

        # 还是删不掉（Windows 上文件可能被瞬时占用）：把 mtime 拨到 1970，
        # 让**下一个等待者立刻**判定它是陈旧锁并回收。
        # 不做这一步的后果实测过：残留锁会让并发写入随机卡满整个超时，
        # 然后退化成无锁写入（丢更新的窗口就打开了）。
        try:
            os.utime(self.path, (_LOCK_EPOCH, _LOCK_EPOCH))
        except OSError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def config_lock() -> ConfigLock:
    """取当前配置文件的锁（同一路径返回同一个实例，保证可重入计数有效）。"""
    path = _lock_path()
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = ConfigLock(path)
            _locks[path] = lock
        return lock


def _replace_with_retry(tmp: str, path: str, attempts: int = _WRITE_ATTEMPTS) -> None:
    """``os.replace`` 的重试包装（Windows 上必须）。

    Windows 的 ``os.replace``（MoveFileEx + REPLACE_EXISTING）在**目标文件正被
    别人打开**时会直接抛 ``PermissionError`` —— 而 Python 的 ``open()`` 默认
    共享模式不含 FILE_SHARE_DELETE，所以只要有另一个线程/进程正在**读**
    config.ini（令牌校验、面板读配置都会读），替换就可能被拒。

    这不是理论问题：并发保存时能稳定复现（见 tests/test_config_write_race.py）。
    实测 3 个线程持续读的情况下，平均重试 2 次、最坏 22 次即可成功，所以这里
    用带抖动的短退避重试；真的一直失败才抛出去，让调用方如实报错，
    而不是假装保存成功。
    """
    last: OSError | None = None
    for _ in range(max(1, attempts)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:        # 目标被占用：退避重试
            last = e
            _retry_sleep()
    if last is not None:
        raise last


def _atomic_write_lines(lines: list) -> None:
    """把行列表原子写回 config.ini（临时文件 + fsync + os.replace）。

    读方永远看不到写了一半的文件 —— 配置被截断会让整个程序起不来。
    临时文件名带上 pid 与线程号：同一进程里多个线程各自写入时不会互相踩。
    """
    path = config_path()
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(
        directory, f".ads_agent_config_tmp_{os.getpid()}_{threading.get_ident()}"
    )
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _invalidate_cache()


def edit_config(mutate, timeout: float = _LOCK_TIMEOUT) -> bool:
    """在共享锁内对 config.ini 做一次**读-改-写**（原子替换）。

    ``mutate(lines)`` 就地修改行列表；返回是否真的拿到了锁。

    所有写 config.ini 的代码都必须走这里 —— 后端保存 LLM 设置
    （``backend/config.py``）与令牌生成/轮换（本模块）都算。走同一个锁，
    两边就不会出现"一边写的字段被另一边按旧快照覆盖回去"。

    拿不到锁（别的进程持有了很久、或目录异常）时**仍然完成本次写入**：
    原子替换保证文件不会写坏，而"用户点的保存没生效"比极小概率的丢更新更糟。
    这种情况会记进 :func:`lock_stats` 的 ``fallback`` 计数，便于排错。
    """
    lock = config_lock()
    got = lock.acquire(timeout)
    _bump("acquired" if got else "fallback")
    try:
        lines = _read_lines()
        mutate(lines)
        _atomic_write_lines(lines)
    finally:
        if got:
            lock.release()
    return got


# ---------------------------------------------------------------------------
# 令牌写入
# ---------------------------------------------------------------------------

def _write_token(token: str) -> None:
    """把令牌写回 config.ini 的 [ads] token（在共享锁内、原子替换）。"""
    def mutate(lines: list) -> None:
        start = next(
            (i for i, l in enumerate(lines) if l.strip().lower() == "[ads]"), None
        )
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("[ads]")
            start = len(lines) - 1

        end = len(lines)
        for i in range(start + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("[") and s.endswith("]"):
                end = i
                break

        idx = next(
            (i for i in range(start + 1, end) if re.match(r"token\s*=", lines[i], re.I)),
            None,
        )
        if idx is not None:
            lines[idx] = f"token = {token}"
        else:
            lines.insert(end, f"token = {token}")

    edit_config(mutate)


def _persist(token: str) -> str:
    """写盘并回读确认；写不进去时至少保证本进程可用。"""
    try:
        _write_token(token)
    except OSError:
        return token
    return read_token() or token


def ensure_token() -> str:
    """拿到可用令牌；缺失或仍是公开默认值时生成并持久化。

    并发安全：加锁 + 双重检查。两个进程同时启动时，只有一个生成，
    另一个会读到它写下的值 —— 绝不会各自生成不同令牌。
    """
    token = read_token()
    if not needs_rotation(token):
        return token

    lock = config_lock()
    if not lock.acquire():
        # 拿不到锁（别人正在生成，或目录不可写）。再读一次盘；
        # 若仍是旧值就退化生成 —— 因为两端校验时都会重新读盘，
        # 最终仍会收敛到磁盘上的那一个值。
        _bump("fallback")
        token = read_token()
        return token if not needs_rotation(token) else _persist(generate_token())

    try:
        token = read_token()            # 双重检查：别人可能刚写完
        if not needs_rotation(token):
            return token
        return _persist(generate_token())
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def check_token(provided: str) -> bool:
    """请求携带的令牌是否有效。

    只读、不写盘；未配置令牌时**一律拒绝**（fail closed）——
    绝不能因为"没配令牌"就把接口放开。
    使用 hmac.compare_digest 做定时安全比较。
    """
    expected = read_token()
    if not expected:
        return False
    return hmac.compare_digest(str(provided or ""), expected)


def status() -> dict:
    """本地诊断用（含脱敏提示）。不要把它放进对外开放的响应体。"""
    token = read_token()
    return {
        "configured": bool(token),
        "legacy": is_legacy(token),
        "weak": is_weak(token),
        "hint": mask(token),
        "config": config_path(),
    }


def main() -> int:
    """命令行自查：默认只显示脱敏信息，--show 才打印明文。"""
    import argparse

    ap = argparse.ArgumentParser(description="ADS Agent 回环令牌自查")
    ap.add_argument("--show", action="store_true", help="打印明文令牌（谨慎）")
    ap.add_argument("--rotate", action="store_true", help="强制换一个新令牌")
    args = ap.parse_args()

    if args.rotate:
        _persist(generate_token())

    token = ensure_token()
    info = status()
    print(f"配置文件   : {info['config']}")
    print(f"令牌状态   : {'已配置' if info['configured'] else '未配置'}"
          f"{'（仍是公开默认值，已自动轮换）' if info['legacy'] else ''}"
          f"{'（长度偏短，建议轮换）' if info['weak'] else ''}")
    print(f"令牌指纹   : {info['hint']}")
    if args.show:
        print(f"令牌明文   : {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
