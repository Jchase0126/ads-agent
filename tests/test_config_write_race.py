"""config.ini 并发写入协调的测试（不需要 ADS，纯标准库）。

覆盖第三轮评审第 1 项「配置写入竞争」：

config.ini 有**两个**写入方 —— 令牌生成/轮换（``backend/ads_auth.py``）与
LLM 设置保存（``backend/config.py``）。两者都是"读整个文件 → 改几行 → 写回"，
如果不共用一套协调机制就会互相覆盖。本文件验证：

* 两边走的是**同一把跨进程锁**（``ads_auth.config_lock()``）；
* 一边持锁时另一边会**真的等待**，而不是各自按旧快照写回；
* 线程并发 / 进程并发下，API 设置与令牌**都不丢**；
* 注释、空行、其它段落原样保留；
* 写入是原子的，不留临时文件、不留锁文件；
* 锁可重入（``ensure_token`` 内部要嵌套写令牌，不能自锁）；
* 没持锁就 ``release()`` 不会删掉别人的锁文件。

运行::

    python tests/test_config_write_race.py
"""

import configparser
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")

# 必须在 import config 之前：config.py 在 import 时就会算 CONFIG_PATH
_TMP = tempfile.mkdtemp(prefix="ads_agent_race_")
_CFG = os.path.join(_TMP, "config.ini")
os.environ["ADS_AGENT_CONFIG"] = _CFG

import ads_auth  # noqa: E402

import config as config_mod  # noqa: E402

CONFIG_TEMPLATE = """; 顶部注释必须保留
[llm]
base_url = https://example.invalid/v1
model = initial-model
api_key = sk-not-a-real-key
models = initial-model, other-model

[ads]
host = 127.0.0.1
port = 8761
; 令牌说明必须保留
token = ads-agent-local-token

[agent]
max_tool_steps = 30
sim_timeout = 900
"""


def _reset_lock() -> None:
    """把共享锁恢复成"干净可用"状态（测试之间不互相影响）。"""
    lock = ads_auth.config_lock()
    lock._depth = 0
    if lock.fd is not None:
        try:
            os.close(lock.fd)
        except OSError:
            pass
        lock.fd = None
    try:
        os.unlink(lock.path)
    except OSError:
        pass


def _fresh(body: str = CONFIG_TEMPLATE) -> str:
    _reset_lock()
    with open(_CFG, "w", encoding="utf-8") as f:
        f.write(body)
    ads_auth._invalidate_cache()
    ads_auth.reset_lock_stats()
    return _CFG


def _teardown() -> None:
    _reset_lock()
    shutil.rmtree(_TMP, ignore_errors=True)


def _raw() -> str:
    with open(_CFG, encoding="utf-8") as f:
        return f.read()


def _get(section: str, key: str, default=None):
    parser = configparser.ConfigParser()
    parser.read(_CFG, encoding="utf-8")
    return parser.get(section, key, fallback=default)


def _token() -> str:
    return (_get("ads", "token", "") or "").strip()


def _wait(cond, timeout=10.0, msg=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"等待超时：{msg}")


# ---------------------------------------------------------------------------
# 共用同一把锁
# ---------------------------------------------------------------------------

def test_lock_file_lives_next_to_config():
    _fresh()
    lock = ads_auth.config_lock()
    eq(os.path.dirname(os.path.abspath(lock.path)),
       os.path.dirname(os.path.abspath(_CFG)),
       "锁文件应与 config.ini 同目录（不同安装路径也能各自串行化）")
    contains(os.path.basename(lock.path), "config",
             "锁文件名应体现它保护的是整份配置，而不只是令牌")


def test_same_path_returns_the_same_lock_object():
    _fresh()
    ok(ads_auth.config_lock() is ads_auth.config_lock(),
       "同一路径必须返回同一个锁对象，否则重入计数失效")


def test_lock_is_reentrant():
    _fresh()
    lock = ads_auth.config_lock()
    ok(lock.acquire(timeout=1), "首次加锁应成功")
    ok(lock.acquire(timeout=1), "同线程重入应成功（ensure_token 内部要嵌套写令牌）")
    eq(os.path.exists(lock.path), True)
    lock.release()
    eq(os.path.exists(lock.path), True, "还有一层没释放，锁文件不该消失")
    lock.release()
    eq(os.path.exists(lock.path), False, "完全释放后应清掉锁文件")
    ok(lock.acquire(timeout=1), "释放后应能再次加锁")
    lock.release()


def test_release_without_acquire_does_not_steal_the_lock():
    _fresh()
    holder = ads_auth.config_lock()
    ok(holder.acquire(timeout=1))

    other = ads_auth.ConfigLock(holder.path)   # 模拟另一个进程的锁对象
    eq(other.acquire(timeout=0.2), False, "锁被持有时不应拿到")
    other.release()                            # 没持锁就 release

    eq(os.path.exists(holder.path), True,
       "未持锁的 release() 把别人的锁文件删掉了 —— 会造成锁失效")
    holder.release()
    eq(os.path.exists(holder.path), False)


def test_two_threads_are_serialized_by_the_same_lock():
    _fresh()
    order = []
    lock = ads_auth.config_lock()
    inside = threading.Event()

    def first():
        lock.acquire()
        try:
            order.append("first-in")
            inside.set()                 # 明确告知"已持锁"，不用 sleep 猜
            time.sleep(0.35)
            order.append("first-out")
        finally:
            lock.release()

    t = threading.Thread(target=first)
    t.start()
    ok(inside.wait(10), "第一个线程没能及时拿到锁")

    ok(lock.acquire(timeout=5), "主线程应能拿到锁（不同线程、同一进程）")
    order.append("second-in")
    lock.release()
    t.join(10)
    eq(order, ["first-in", "first-out", "second-in"],
       "两个线程没有被同一把锁串行化")


# ---------------------------------------------------------------------------
# 一边持锁时另一边必须等待（丢更新的确定性复现）
# ---------------------------------------------------------------------------

def test_llm_save_waits_for_token_rotation():
    """令牌轮换持锁期间保存 LLM 设置 —— 顺序必须被强制，两个字段都要保住。

    这就是"丢更新"的确定性复现：如果 config.update_llm_settings 不走同一把锁，
    它会先读（此时还是旧令牌）、再在令牌写完之后写回，把刚轮换的令牌
    还原成公开默认值。
    """
    _fresh()
    order = []
    written = {}
    holding = threading.Event()

    def rotator():
        lock = ads_auth.config_lock()
        lock.acquire()
        try:
            holding.set()                                # 已持锁（不用 sleep 猜）
            ads_auth._read_lines()                       # 读
            time.sleep(0.5)                              # 放大竞争窗口
            written["token"] = ads_auth.generate_token()
            ads_auth._write_token(written["token"])      # 改 + 写（可重入）
            order.append("token-written")
        finally:
            lock.release()

    t = threading.Thread(target=rotator)
    t.start()
    ok(holding.wait(10), "轮换线程没能及时拿到锁")

    result = config_mod.update_llm_settings(model="model-after-race")
    order.append("llm-written")
    t.join(10)

    eq(order, ["token-written", "llm-written"],
       "保存 LLM 设置没有等待令牌写入完成 —— 两边没共用同一把锁")
    eq(result.get("config_locked"), True, "本次写入应在跨进程锁内完成")
    eq(_token(), written["token"], "刚轮换的令牌被 LLM 保存覆盖了（丢更新）")
    eq(_get("llm", "model"), "model-after-race", "LLM 设置没保存成功")
    eq(_get("llm", "base_url"), "https://example.invalid/v1", "其它 LLM 字段被弄丢了")
    eq(_get("llm", "api_key"), "sk-not-a-real-key", "API Key 被弄丢了")
    contains(_raw(), "; 顶部注释必须保留", "注释被吞掉了")
    contains(_raw(), "; 令牌说明必须保留", "段落内注释被吞掉了")
    eq(ads_auth.lock_stats()["fallback"], 0, "出现了无锁写入（锁没起作用）")


def test_token_rotation_waits_for_llm_save():
    """反向：LLM 保存持锁期间轮换令牌，轮换必须等它写完。"""
    _fresh()
    lock = ads_auth.config_lock()
    lock.acquire()

    written = {}
    started = threading.Event()
    done = threading.Event()

    def rotator():
        started.set()
        written["token"] = ads_auth._persist(ads_auth.generate_token())
        done.set()

    t = threading.Thread(target=rotator)
    t.start()
    ok(started.wait(10), "轮换线程没能及时启动")
    time.sleep(0.25)                  # 给它机会去抢锁（抢不到才是对的）
    eq(done.is_set(), False, "令牌写入没有等待持有锁的一方")

    config_mod.update_llm_settings(model="model-while-locked")
    lock.release()

    ok(done.wait(10), "释放锁后令牌写入应立刻完成")
    t.join(5)
    eq(_token(), written["token"], "令牌没有写进去")
    eq(_get("llm", "model"), "model-while-locked", "LLM 设置被令牌写入覆盖了")
    eq(ads_auth.lock_stats()["fallback"], 0)


# ---------------------------------------------------------------------------
# 并发压力：不丢字段
# ---------------------------------------------------------------------------

def test_three_threads_saving_different_fields_lose_nothing():
    """三个线程分别保存 base_url / model / api_key —— 三个字段都必须留下。"""
    _fresh()
    errors = []
    rounds = 30

    def saver(**kwargs):
        def loop():
            try:
                for _ in range(rounds):
                    config_mod.update_llm_settings(**kwargs)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        return loop

    threads = [
        threading.Thread(target=saver(base_url="https://h29.invalid/v1")),
        threading.Thread(target=saver(model="MM29")),
        threading.Thread(target=saver(api_key="sk-key-29")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    eq(errors, [], f"并发保存出错: {errors}")
    eq(_get("llm", "base_url"), "https://h29.invalid/v1", "base_url 被并发写弄丢了")
    eq(_get("llm", "model"), "MM29", "model 被并发写弄丢了")
    eq(_get("llm", "api_key"), "sk-key-29", "api_key 被并发写弄丢了")
    eq(ads_auth.lock_stats()["fallback"], 0, "出现了无锁写入")


def test_thread_stress_keeps_token_and_llm_settings():
    """线程并发：令牌写入与 LLM 保存互相交错，两边都不能丢。

    ``model`` 只有 LLM 写者会写，所以最终值必然是它最后一次写的 ``MT{N-1}``；
    如果写入没有串行化，某次令牌写入会按旧快照把 model 还原成更早的值。
    """
    _fresh()
    errors = []
    n = 60

    def llm_writer():
        try:
            for i in range(n):
                config_mod.update_llm_settings(model=f"MT{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def token_writer():
        try:
            for _ in range(n):
                ads_auth._persist(ads_auth.generate_token())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=llm_writer),
               threading.Thread(target=token_writer),
               threading.Thread(target=llm_writer),
               threading.Thread(target=token_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)

    eq(errors, [], f"并发写入出错: {errors}")
    eq(ads_auth.lock_stats()["fallback"], 0, "出现了无锁写入（锁没起作用）")

    token = _token()
    ok(bool(token), "令牌被写丢了")
    ne(token, ads_auth.LEGACY_DEFAULT_TOKEN, "令牌被还原成公开默认值")
    ok(len(token) >= ads_auth.MIN_STRONG_LEN, "令牌长度异常")
    ok(ads_auth.check_token(token), "落盘的令牌通不过校验")

    eq(_get("llm", "model"), f"MT{n - 1}", "LLM 设置被并发写入弄丢了（丢更新）")
    eq(_get("llm", "base_url"), "https://example.invalid/v1")
    eq(_get("llm", "api_key"), "sk-not-a-real-key")
    contains(_raw(), "; 顶部注释必须保留")
    contains(_raw(), "; 令牌说明必须保留")
    eq(_get("agent", "sim_timeout"), "900", "其它段落被弄丢了")


def test_lock_timeout_outlives_the_stale_threshold():
    """超时必须大于陈旧阈值。

    否则等待者会在"能回收残留锁"之前就先放弃 —— 表现为并发写入随机卡满整个
    超时，然后退化成无锁写入（丢更新的窗口就打开了）。这条是被实测出来的：
    曾经 240 次并发写入里有 4 次白等满 8 秒。
    """
    ok(ads_auth._LOCK_TIMEOUT > ads_auth._LOCK_STALE,
       f"超时 {ads_auth._LOCK_TIMEOUT}s 应大于陈旧阈值 {ads_auth._LOCK_STALE}s")


def test_phantom_lock_is_reclaimed_immediately():
    """release() 删不掉锁文件时，必须把它标记成陈旧，让下一个写入者立刻回收。"""
    _fresh()
    lock = ads_auth.config_lock()
    ok(lock.acquire(timeout=2), "首次加锁应成功")

    handle = open(lock.path, "r+")        # 占住文件 -> unlink 必然失败
    try:
        lock.release()
    finally:
        handle.close()

    eq(os.path.exists(lock.path), True, "前置条件：锁文件没能删掉")
    ok(os.path.getmtime(lock.path) < 1000,
       "残留锁的 mtime 必须被拨到过去，否则等待者要白等到 _LOCK_STALE")

    t0 = time.time()
    ok(lock.acquire(timeout=5), "下一个写入者应能立刻回收残留锁")
    ok(time.time() - t0 < 2.0, "回收残留锁不该等满超时")
    lock.release()


def test_concurrent_readers_do_not_break_writers():
    """读配置的线程与写配置的线程并发时，两边都不能出错。

    Windows 的 ``os.replace`` 与 ``open`` 会互相打断（目标文件被打开时替换会被拒，
    替换的一瞬间读也会被拒）。所以：
    * 写入必须重试到成功（否则用户点"保存"直接报错）；
    * 读**绝不能**返回空令牌 —— 空令牌会让 check_token 一律拒绝，表现为"随机 401"。
    """
    _fresh()
    token = ads_auth.ensure_token()
    stop = threading.Event()
    errors: list = []
    empties = [0]
    reads = [0]

    def reader():
        while not stop.is_set():
            try:
                tok = ads_auth.read_token()
                if not tok:
                    empties[0] += 1
                elif not ads_auth.check_token(tok):
                    errors.append(("check", tok))
                reads[0] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(("read", repr(e)))
                return

    def writer():
        try:
            for i in range(40):
                config_mod.update_llm_settings(model=f"RW{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(("write", repr(e)))

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for r in readers:
        r.start()
    w = threading.Thread(target=writer)
    w.start()
    w.join(90)
    stop.set()
    for r in readers:
        r.join(15)

    eq(errors, [], f"并发读写出错（Windows 文件占用？）: {errors}")
    eq(empties[0], 0, "读到过空令牌 —— 会导致随机 401")
    ok(reads[0] > 0, "读线程应当真的读过配置")
    eq(_get("llm", "model"), "RW39", "并发读者把写入搞丢了")
    eq(_token(), token, "并发读者把令牌搞丢了")


def test_edit_config_propagates_read_failure_without_writing():
    """读不到配置时必须**失败**，而不是按空内容写回（那会把配置整个抹掉）。"""
    _fresh()
    before = _raw()
    real = ads_auth._read_lines

    def broken():
        raise OSError("配置文件被占用")

    ads_auth._read_lines = broken
    try:
        try:
            ads_auth.edit_config(lambda lines: lines.append("[evil]"))
        except OSError:
            pass
        else:
            raise AssertionError("读失败时应向上抛异常")
    finally:
        ads_auth._read_lines = real

    eq(_raw(), before, "读取失败却把配置写坏了（LLM 设置/令牌会被清空）")
    eq(os.path.exists(ads_auth.config_lock().path), False, "异常后锁没释放")


def test_read_lines_never_returns_empty_for_an_existing_file():
    """文件存在但读不到 -> 抛异常；文件真的不存在 -> 返回空列表。"""
    _fresh()
    eq(len(ads_auth._read_lines()), len(CONFIG_TEMPLATE.splitlines()))

    os.unlink(_CFG)
    eq(ads_auth._read_lines(), [], "文件不存在时应返回空列表（首次创建配置）")

    # 把路径变成目录：open() 必定失败，必须抛出来而不是返回 []
    os.makedirs(_CFG, exist_ok=True)
    try:
        try:
            ads_auth._read_lines()
        except OSError:
            pass
        else:
            raise AssertionError("读不到却返回了空列表 —— 会把配置写坏")
    finally:
        os.rmdir(_CFG)


def test_process_stress_keeps_token_and_llm_settings():
    """进程并发：一个进程反复保存 LLM 设置，另一个反复轮换令牌。"""
    _fresh()
    ads_auth.ensure_token()      # 先把公开默认值轮换掉，否则只读进程可能读到它
    n = 25

    llm_script = os.path.join(_TMP, "writer_llm.py")
    token_script = os.path.join(_TMP, "writer_token.py")
    reader_script = os.path.join(_TMP, "reader.py")
    with open(llm_script, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, r'{BACKEND}')\n"
            "import config\n"
            "n = int(sys.argv[1])\n"
            "for i in range(n):\n"
            "    config.update_llm_settings(model=f'MP{i}')\n"
            "print(f'MP{n - 1}', flush=True)\n"
        )
    with open(token_script, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, r'{BACKEND}')\n"
            "import ads_auth\n"
            "n = int(sys.argv[1])\n"
            "last = ''\n"
            "for _ in range(n):\n"
            "    last = ads_auth._persist(ads_auth.generate_token())\n"
            "print(last, flush=True)\n"
        )
    # 第三方进程只读 —— 在 Windows 上它会占用文件，写入必须能扛住，
    # 同时它自己也绝不能读到空令牌或公开默认值（那会表现为随机 401）。
    # 注意：不能断言 check_token(刚读到的值) 为真 —— 令牌写入进程可能刚好
    # 在两次调用之间完成了一次轮换，这是正常行为。
    with open(reader_script, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, r'{BACKEND}')\n"
            "import ads_auth\n"
            "n = int(sys.argv[1])\n"
            "for _ in range(n * 40):\n"
            "    tok = ads_auth.read_token()\n"
            "    assert tok, '读到空令牌'\n"
            "    assert len(tok) >= ads_auth.MIN_STRONG_LEN, '读到弱令牌'\n"
            "    assert not ads_auth.is_legacy(tok), '读到公开默认令牌'\n"
            "print('reader-ok', flush=True)\n"
        )

    env = dict(os.environ)
    env[ads_auth.CONFIG_ENV] = _CFG
    procs = [
        subprocess.Popen([sys.executable, llm_script, str(n)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env),
        subprocess.Popen([sys.executable, token_script, str(n)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env),
        subprocess.Popen([sys.executable, reader_script, str(n)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env),
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=240)
        eq(p.returncode, 0, f"子进程失败: {err[-400:]}")
        outs.append(out.strip().splitlines()[-1])

    llm_last, token_last, reader_last = outs
    eq(reader_last, "reader-ok", "只读进程在读配置时出错了")
    eq(_get("llm", "model"), llm_last, "跨进程并发写把 LLM 设置弄丢了")
    eq(_token(), token_last, "跨进程并发写把令牌弄丢了")
    ne(_token(), ads_auth.LEGACY_DEFAULT_TOKEN)
    ok(ads_auth.check_token(_token()), "落盘的令牌通不过校验")
    eq(_get("llm", "base_url"), "https://example.invalid/v1")
    eq(_get("llm", "api_key"), "sk-not-a-real-key")
    contains(_raw(), "; 顶部注释必须保留")


# ---------------------------------------------------------------------------
# 保注释 / 原子性 / 边界
# ---------------------------------------------------------------------------

def test_llm_save_preserves_comments_and_other_sections():
    _fresh()
    config_mod.update_llm_settings(model="changed", api_key="sk-new-key")
    text = _raw()
    contains(text, "; 顶部注释必须保留")
    contains(text, "; 令牌说明必须保留")
    contains(text, "[agent]")
    contains(text, "sim_timeout = 900")
    contains(text, "max_tool_steps = 30")
    contains(text, "models = initial-model, other-model")
    contains(text, "model = changed")
    contains(text, "api_key = sk-new-key")
    eq(text.count("\nmodel = "), 1, "不应出现重复的键")

    # 令牌字段必须还在，而且不能是公开默认值。
    # （保存 LLM 设置时 load() 会顺带把公开默认值轮换掉 —— 这是期望行为，
    #  这里要验的是"没丢"，不是"没变"。）
    eq(text.count("\ntoken = "), 1, "不应出现重复的 token 行")
    token = _token()
    ok(bool(token), "保存 LLM 设置把令牌弄丢了")
    ne(token, ads_auth.LEGACY_DEFAULT_TOKEN, "令牌仍是公开默认值")
    ok(ads_auth.check_token(token), "保存后落盘的令牌通不过校验")


def test_write_leaves_no_temp_or_lock_files():
    _fresh()
    config_mod.update_llm_settings(model="m")
    ads_auth._persist(ads_auth.generate_token())
    config_mod.update_llm_settings(base_url="https://x.invalid/v1")

    leftovers = [f for f in os.listdir(_TMP) if f.startswith(".ads_agent_config_tmp_")]
    eq(leftovers, [], f"残留了临时文件: {leftovers}")
    eq([f for f in os.listdir(_TMP) if f.endswith(".lock")], [],
       "锁文件没有清理干净")
    eq(ads_auth.lock_stats()["fallback"], 0)


def test_edit_config_creates_the_file_when_missing():
    _fresh()
    os.unlink(_CFG)
    ads_auth._invalidate_cache()
    ok(ads_auth.edit_config(lambda lines: lines.extend(["[new]", "k = v"])),
       "文件不存在时也应在锁内完成写入")
    contains(_raw(), "[new]")
    eq(_get("new", "k"), "v")


def test_edit_config_skips_write_when_mutate_raises():
    _fresh()
    before = _raw()

    def boom(lines):
        raise ValueError("nope")

    try:
        ads_auth.edit_config(boom)
    except ValueError:
        pass
    else:
        raise AssertionError("mutate 抛异常时应继续向上抛")
    eq(_raw(), before, "mutate 失败却把文件改了")
    eq(os.path.exists(ads_auth.config_lock().path), False, "异常后锁没释放")


def test_stale_lock_is_reclaimed():
    _fresh()
    lock = ads_auth.config_lock()
    with open(lock.path, "w", encoding="utf-8") as f:
        f.write("")
    old = time.time() - (ads_auth._LOCK_STALE + 5)
    os.utime(lock.path, (old, old))

    ok(lock.acquire(timeout=3), "陈旧的锁（进程崩溃残留）应被清理并重新获取")
    lock.release()


def test_token_and_llm_settings_survive_a_simulated_restart():
    """一次完整的"轮换 + 保存"之后，两端读到的令牌与 LLM 设置都还在。"""
    _fresh()
    token = ads_auth.ensure_token()
    config_mod.update_llm_settings(model="restart-model")
    ads_auth._invalidate_cache()                 # 模拟重启后重新读盘
    eq(ads_auth.read_token(), token, "重启后令牌变了")
    ok(ads_auth.check_token(token))
    eq(_get("llm", "model"), "restart-model", "重启后 LLM 设置丢了")
    not_contains(_raw(), ads_auth.LEGACY_DEFAULT_TOKEN)


if __name__ == "__main__":
    try:
        code = run(globals(), "config.ini 并发写入协调")
    finally:
        _teardown()
    sys.exit(code)
