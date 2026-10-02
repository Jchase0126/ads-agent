"""令牌迁移 / 生成 / 校验的测试（不需要 ADS，纯标准库）。

覆盖评审第 2 项「移除公开默认令牌」的关键不变量：

* 缺失 / 空 / 旧公开默认值 ``ads-agent-local-token`` -> 自动生成随机令牌并写回；
* 用户自己设的值一律尊重（即使偏短，只提示不篡改）；
* **两端拿到同一个令牌**：后端（``sys.path`` 导入 ``ads_auth``）与
  ADS 端（``authbridge`` 按文件路径加载同一实现）读到的值必须一致 ——
  这正是"只改默认常量、两端各自生成"那个坑的回归测试；
* 并发 / 多进程同时启动时只会收敛到一个令牌（跨进程排他锁 + 双重检查）；
* 校验 fail-closed：没配置令牌时一律拒绝，而不是把接口放开；
* 写盘是原子的，且保留注释与其它段落。

运行::

    python tests/test_token_migration.py
"""

import configparser
import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, ne, not_contains, ok, run  # noqa: E402

BACKEND = add_path("backend")
ADDON = add_path("addon", "ads_agent")

import ads_auth  # noqa: E402  — 后端侧的那一份实现

import authbridge  # noqa: E402  — ADS 端按文件路径加载的同一份实现

LEGACY = "ads-agent-local-token"
_ENV = ads_auth.CONFIG_ENV

_tmpdirs: list = []
_original_env = os.environ.get(_ENV)


def _teardown():
    if _original_env is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = _original_env
    for d in _tmpdirs:
        shutil.rmtree(d, ignore_errors=True)
    _tmpdirs.clear()


def _fresh(body: str = "") -> str:
    """建一个临时配置目录，把 ADS_AGENT_CONFIG 指过去，返回 config.ini 路径。"""
    d = tempfile.mkdtemp(prefix="ads_agent_tok_")
    _tmpdirs.append(d)
    path = os.path.join(d, "config.ini")
    if body:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
    os.environ[_ENV] = path
    ads_auth._invalidate_cache()
    return path


def _raw(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _token_in_file(path: str) -> str:
    p = configparser.ConfigParser()
    p.read(path, encoding="utf-8")
    return (p.get("ads", "token", fallback="") or "").strip()


CONFIG_WITH_LEGACY = """; 顶部注释必须保留
[llm]
base_url = http://127.0.0.1:1
api_key =

[ads]
host = 127.0.0.1
port = 8761
; 令牌说明也必须保留
token = ads-agent-local-token

[agent]
sim_timeout = 900
"""


# ---------------------------------------------------------------------------
# 生成 / 迁移
# ---------------------------------------------------------------------------

def test_generates_when_config_missing():
    path = _fresh()
    eq(os.path.exists(path), False, "前置条件：配置文件不应存在")
    token = ads_auth.ensure_token()
    ok(bool(token), "应当生成令牌")
    ok(not ads_auth.needs_rotation(token), "生成的令牌不应需要轮换")
    ok(len(token) >= ads_auth.MIN_STRONG_LEN, "生成的令牌应当足够长")
    eq(_token_in_file(path), token, "生成后应写回 config.ini")


def test_empty_token_is_rotated():
    path = _fresh("[ads]\ntoken = \n")
    token = ads_auth.ensure_token()
    ok(bool(token) and not ads_auth.is_legacy(token), "空令牌应被替换成随机令牌")
    eq(_token_in_file(path), token)


def test_legacy_token_is_rotated():
    path = _fresh(CONFIG_WITH_LEGACY)
    eq(ads_auth.read_token(), LEGACY, "前置条件：读到旧默认令牌")
    token = ads_auth.ensure_token()
    ne(token, LEGACY, "公开默认令牌必须被轮换掉")
    eq(_token_in_file(path), token, "轮换结果必须落盘")
    eq(ads_auth.check_token(LEGACY), False, "旧默认令牌必须立即失效")


def test_legacy_rotation_is_idempotent():
    path = _fresh(CONFIG_WITH_LEGACY)
    first = ads_auth.ensure_token()
    mtime = os.path.getmtime(path)
    second = ads_auth.ensure_token()
    eq(second, first, "已有强令牌时不应再次轮换")
    eq(os.path.getmtime(path), mtime, "不应重复写盘")


def test_custom_token_is_respected():
    path = _fresh("[ads]\ntoken = " + "my-own-token-" + "a" * 20 + "\n")
    mine = "my-own-token-" + "a" * 20
    eq(ads_auth.ensure_token(), mine, "用户自设的令牌必须原样保留")
    eq(_token_in_file(path), mine)


def test_weak_token_warns_but_is_kept():
    _fresh("[ads]\ntoken = short\n")
    eq(ads_auth.ensure_token(), "short", "偏短的令牌只提示，不擅自改动")
    ok(ads_auth.is_weak("short"), "应当被判定为弱令牌")
    eq(ads_auth.is_weak("x" * ads_auth.MIN_STRONG_LEN), False, "达到长度阈值即不算弱")


def test_write_is_atomic_and_preserves_layout():
    path = _fresh(CONFIG_WITH_LEGACY)
    token = ads_auth.ensure_token()
    text = _raw(path)
    contains(text, "; 顶部注释必须保留", "注释被吞掉了")
    contains(text, "; 令牌说明也必须保留", "section 内注释被吞掉了")
    contains(text, "[llm]", "其它段落被删掉了")
    contains(text, "base_url = http://127.0.0.1:1", "其它键被改掉了")
    contains(text, "[agent]", "后续段落被删掉了")
    contains(text, "sim_timeout = 900", "后续键被改掉了")
    not_contains(text, LEGACY, "旧令牌仍留在文件里")
    eq(text.count("\ntoken = "), 1, "不应出现重复的 token 行")
    contains(text, f"token = {token}")
    eq(_token_in_file(path), token)


def test_write_creates_ads_section_when_absent():
    path = _fresh("[llm]\napi_key = \n")
    token = ads_auth.ensure_token()
    text = _raw(path)
    contains(text, "[ads]", "缺少 [ads] 段时应自动补上")
    eq(_token_in_file(path), token)


# ---------------------------------------------------------------------------
# 两端一致（评审第 2 项的核心回归）
# ---------------------------------------------------------------------------

def test_backend_and_addon_read_the_same_config_path():
    """环境变量未设置时，两端的默认配置路径必须指向同一个文件。"""
    saved = os.environ.pop(_ENV, None)
    try:
        backend_path = ads_auth.config_path()
        addon_path = authbridge.auth().config_path()
        eq(os.path.normcase(addon_path), os.path.normcase(backend_path),
           "两端默认配置路径不一致 —— 这正是会出现两个不同令牌的根因")
    finally:
        if saved is not None:
            os.environ[_ENV] = saved


def test_backend_and_addon_converge_on_one_token():
    """同一份旧配置：后端先启动、ADS 端后启动，必须拿到同一个令牌。"""
    path = _fresh(CONFIG_WITH_LEGACY)
    backend_token = ads_auth.ensure_token()          # 后端启动
    addon_token = authbridge.token()                 # ADS 端启动
    eq(addon_token, backend_token, "两端生成了不同的令牌")
    ne(addon_token, LEGACY, "两端都不应停留在公开默认值")
    eq(_token_in_file(path), backend_token, "落盘值应与两端一致")
    ok(authbridge.check(backend_token), "ADS 端应校验通过后端拿到的令牌")
    ok(ads_auth.check_token(addon_token), "后端应校验通过 ADS 端拿到的令牌")


def test_addon_first_then_backend_also_converges():
    """顺序反过来（ADS 先启动）也必须一致。"""
    _fresh(CONFIG_WITH_LEGACY)
    addon_token = authbridge.token()
    backend_token = ads_auth.ensure_token()
    eq(backend_token, addon_token)
    ok(ads_auth.check_token(addon_token) and authbridge.check(backend_token))


# ---------------------------------------------------------------------------
# 并发
# ---------------------------------------------------------------------------

def test_concurrent_threads_converge():
    """多线程同时 ensure_token（模拟面板与后端同时启动）。"""
    path = _fresh(CONFIG_WITH_LEGACY)
    results: list = []
    errors: list = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=10)
            results.append(ads_auth.ensure_token())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    eq(errors, [], f"并发调用出错: {errors}")
    eq(len(results), 8, "所有线程都应有结果")
    eq(len(set(results)), 1, f"并发生成了多个令牌: {set(results)}")
    eq(_token_in_file(path), results[0], "落盘值应与并发结果一致")


def test_concurrent_processes_converge():
    """多进程同时 ensure_token（模拟后端进程与 ADS 进程同时启动）。"""
    path = _fresh(CONFIG_WITH_LEGACY)
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import ads_auth; sys.stdout.write(ads_auth.ensure_token())" % BACKEND
    )
    env = dict(os.environ)
    env[_ENV] = path
    procs = [
        subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(4)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        eq(p.returncode, 0, f"子进程失败: {err[:400]}")
        outs.append(out.strip())

    eq(len(set(outs)), 1, f"多个进程生成了不同令牌: {set(outs)}")
    ne(outs[0], LEGACY, "不应停留在公开默认值")
    eq(_token_in_file(path), outs[0], "落盘值应与各进程一致")


# ---------------------------------------------------------------------------
# 校验（fail closed）
# ---------------------------------------------------------------------------

def test_check_token_accepts_only_the_exact_value():
    _fresh(CONFIG_WITH_LEGACY)
    token = ads_auth.ensure_token()
    eq(ads_auth.check_token(token), True, "正确令牌应通过")
    eq(ads_auth.check_token(""), False, "空令牌必须被拒绝")
    eq(ads_auth.check_token(None), False, "None 必须被拒绝")
    eq(ads_auth.check_token(token + "x"), False, "多一个字符必须被拒绝")
    eq(ads_auth.check_token(token[:-1]), False, "少一个字符必须被拒绝")
    eq(ads_auth.check_token(token.upper()), False, "大小写不同必须被拒绝")
    eq(ads_auth.check_token(LEGACY), False, "公开默认值必须被拒绝")


def test_check_token_fails_closed_without_config():
    """没配置令牌时一律拒绝 —— 绝不能"没配就放开"。"""
    _fresh()                                   # 文件不存在
    eq(ads_auth.read_token(), "", "前置条件：读不到令牌")
    eq(ads_auth.check_token(""), False)
    eq(ads_auth.check_token("anything"), False)
    eq(ads_auth.check_token(LEGACY), False)

    _fresh("[ads]\ntoken = \n")                # 文件存在但为空
    eq(ads_auth.check_token(""), False, "空配置下连空令牌也必须拒绝")


def test_check_token_is_read_only():
    """校验不能顺手生成/改写配置（否则每次请求都会碰盘）。"""
    path = _fresh(CONFIG_WITH_LEGACY)
    token = ads_auth.ensure_token()          # 先完成一次迁移
    before = _raw(path)
    for _ in range(3):
        eq(ads_auth.check_token(token), True)
        eq(ads_auth.check_token(LEGACY), False)
        eq(ads_auth.check_token("nope"), False)
    eq(_raw(path), before, "check_token 不应改动配置文件")


def test_rotation_takes_effect_without_restart():
    """轮换后正在运行的进程下一次请求就能读到新值（无长期内存缓存）。"""
    _fresh(CONFIG_WITH_LEGACY)
    old = ads_auth.ensure_token()
    ok(ads_auth.check_token(old), "旧令牌此时有效")
    new = ads_auth._persist(ads_auth.generate_token())
    ne(new, old, "轮换应产生新值")
    eq(ads_auth.read_token(), new, "轮换后应立刻读到新值")
    eq(ads_auth.check_token(old), False, "旧令牌应立即失效")
    eq(ads_auth.check_token(new), True, "新令牌应立即生效")


def test_rotate_cli():
    path = _fresh(CONFIG_WITH_LEGACY)
    old = ads_auth.ensure_token()
    env = dict(os.environ)
    env[_ENV] = path
    script = os.path.join(BACKEND, "ads_auth.py")
    p = subprocess.run([sys.executable, script, "--rotate"], capture_output=True,
                       text=True, env=env, timeout=60)
    eq(p.returncode, 0, f"CLI 失败: {p.stderr[:400]}")
    not_contains(p.stdout, old, "CLI 输出不应回显明文令牌")
    new = ads_auth.read_token()
    ne(new, old, "--rotate 应换掉令牌")
    ok(not ads_auth.is_legacy(new), "轮换后不应是公开默认值")


def test_mask_never_leaks_the_whole_token():
    token = "abcdefghijklmnopqrstuvwxyz0123456789"
    masked = ads_auth.mask(token)
    not_contains(masked, token, "脱敏结果不应包含完整令牌")
    ok(len(masked) < len(token), "脱敏结果应比原文短")
    eq(ads_auth.mask(""), "", "空令牌的脱敏结果为空")
    not_contains(ads_auth.mask("abc"), "abc", "短令牌也应脱敏")


def test_status_never_contains_the_plaintext_token():
    _fresh(CONFIG_WITH_LEGACY)
    token = ads_auth.ensure_token()
    info = ads_auth.status()
    eq(info["configured"], True)
    eq(info["legacy"], False)
    not_contains(str(info), token, "status() 泄露了明文令牌")
    contains(str(info), ads_auth.mask(token))


def test_config_env_var_isolates_tests_from_the_real_config():
    """测试用的临时配置绝不能碰到仓库里的 config.ini。"""
    real = os.path.join(add_path(), "config.ini")
    fake = _fresh(CONFIG_WITH_LEGACY)
    ne(os.path.normcase(ads_auth.config_path()), os.path.normcase(real),
       "ADS_AGENT_CONFIG 未生效")
    eq(os.path.normcase(ads_auth.config_path()), os.path.normcase(fake))
    real_before = _raw(real) if os.path.exists(real) else None
    ads_auth.ensure_token()
    real_after = _raw(real) if os.path.exists(real) else None
    eq(real_after, real_before, "临时配置的写入影响到了真实的 config.ini")


if __name__ == "__main__":
    try:
        code = run(globals(), "令牌迁移 / 生成 / 校验")
    finally:
        _teardown()
    sys.exit(code)
