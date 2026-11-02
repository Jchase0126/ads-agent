"""极简测试骨架 —— 本工程刻意不依赖 pytest（后端只用标准库）。

用法::

    from _harness import run, eq, ok, raises, skip

    def test_something():
        eq(1 + 1, 2, "算术")

    if __name__ == "__main__":
        raise SystemExit(run(globals()))

``run()`` 会按**定义顺序**执行命名空间里所有 ``test_*`` 可调用对象，
每条断言都带一行说明，失败时打印断言消息 + 堆栈，最后给出汇总。

这些文件同样能被 pytest 直接收集（函数名就是 ``test_*``）。
"""

import atexit
import os
import shutil
import sys
import tempfile
import threading
import traceback

# ---------------------------------------------------------------------------
# 后台线程异常收集（2026-10-30）：以前 threading.excepthook 默认只把
# 后台线程的未捕获异常打到 stderr，run() 只统计主线程 —— 容量用例的后台
# 提交线程抛 TimeoutError、测试却照常打印"全部通过"，就是这么发生的
# （见 tests/test_gap_audit_2026-10-30.md §2）。现在 run() 一开始就装上
# 收集钩子，收尾时把后台异常并入退出码：有后台异常 = FAIL。
# ---------------------------------------------------------------------------
_THREAD_ERRORS: list = []
_THREAD_ERR_LOCK = threading.Lock()


def _record_thread_exc(args) -> None:
    thread_name = getattr(getattr(args, "thread", None), "name", "") or "?"
    text = "".join(traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback)).strip()
    with _THREAD_ERR_LOCK:
        _THREAD_ERRORS.append({"thread": thread_name, "text": text})
    # 保留默认行为：照样打到 stderr，便于与旧日志对照
    sys.stderr.write(f"\n[后台线程异常] {thread_name}:\n{text}\n")


def background_errors() -> list:
    """本进程目前已收集到的后台线程未捕获异常（快照）。

    需要断言"整个测试期间后台零异常"的用例可以直接调它。
    """
    with _THREAD_ERR_LOCK:
        return list(_THREAD_ERRORS)


# 导入即接管（run() 里还会再装一次，幂等）：测试文件在 import 之后、
# run() 之前起的线程，异常同样要被收集。
threading.excepthook = _record_thread_exc

# ---------------------------------------------------------------------------
# Windows 编码：测试输出一律按 UTF-8 写
# ---------------------------------------------------------------------------
# 中文 Windows 的控制台/管道默认代码页是 GBK，测试里却到处是 emoji 与中文
# 断言文案（📦 / Ω / 工作区…）。子进程按 UTF-8 写、父进程按 GBK 读就会满屏
# 乱码，甚至中途 UnicodeEncodeError。这里统一把本进程的 stdout/stderr 切成
# UTF-8（errors=replace 兜住个别无法编码的字符），父进程也按 UTF-8 解码
# （见 run_tests.py），两边口径一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

PASSED: list = []
FAILED: list = []

# ---------------------------------------------------------------------------
# 测试隔离：用户数据根目录绝不能是真实的 %LOCALAPPDATA%\ADSAgent
# ---------------------------------------------------------------------------
# 程序文件与用户数据分离之后，凡是通过 backend/paths.py 取路径的代码
# （design_job 的保存、config.PROJECT_ROOT、日志……）默认都会落到真实数据
# 根目录。测试若不管它，就会把假会话写进用户的地方 —— 而用户那边的数据
# 一旦被"已存在的有效文件"挡住，真正的迁移就再也写不进去了。
#
# 所以：没有一个测试显式指定时，一律指到一个进程级临时目录，退出时删掉。
# 需要自己的目录的测试照旧设置 ADS_AGENT_DATA_DIR，本默认值会被覆盖。
_TEST_DATA_DIR: str | None = None


def _isolate_user_data() -> None:
    global _TEST_DATA_DIR
    if os.environ.get("ADS_AGENT_DATA_DIR"):
        return  # 测试自己指定了位置，尊重它
    _TEST_DATA_DIR = tempfile.mkdtemp(prefix="ads_agent 测试数据 ")
    os.environ["ADS_AGENT_DATA_DIR"] = _TEST_DATA_DIR


def _cleanup_user_data() -> None:
    if _TEST_DATA_DIR and os.path.isdir(_TEST_DATA_DIR):
        try:
            shutil.rmtree(_TEST_DATA_DIR)
        except OSError:
            pass


_isolate_user_data()
atexit.register(_cleanup_user_data)


class Skip(Exception):
    """环境不满足（例如缺 PySide6）——不算失败。"""


def ok(cond, msg="") -> None:
    if not cond:
        raise AssertionError(msg or "断言失败")


def eq(got, want, msg="") -> None:
    if got != want:
        raise AssertionError(f"{msg or '值不相等'}\n    实际: {got!r}\n    期望: {want!r}")


def ne(got, unwanted, msg="") -> None:
    if got == unwanted:
        raise AssertionError(f"{msg or '值不应相等'}\n    实际: {got!r}")


def contains(haystack, needle, msg="") -> None:
    if needle not in haystack:
        raise AssertionError(
            f"{msg or '未找到期望内容'}\n    查找: {needle!r}\n    内容: {str(haystack)[:400]!r}"
        )


def not_contains(haystack, needle, msg="") -> None:
    if needle in haystack:
        raise AssertionError(
            f"{msg or '出现了不该出现的内容'}\n    不该有: {needle!r}\n    内容: {str(haystack)[:400]!r}"
        )


def raises(exc_type, fn, msg="") -> Exception:
    try:
        fn()
    except exc_type as e:
        return e
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"{msg or '异常类型不符'}\n    期望: {exc_type.__name__}\n    实际: {type(e).__name__}: {e}"
        ) from e
    raise AssertionError(f"{msg or '期望抛出异常'}: {exc_type.__name__}，但没有抛")


def skip(reason: str) -> None:
    raise Skip(reason)


def run(namespace: dict, title: str = "") -> int:
    """执行 namespace 里所有 test_* 函数，返回进程退出码。"""
    tests = [
        (name, fn)
        for name, fn in namespace.items()
        if name.startswith("test_") and callable(fn)
    ]
    if not tests:
        # 零执行不得计为通过：测试文件被重命名/收集条件写错时，
        # "退出码 0 但一项都没跑"必须按失败暴露，不能汇总成 PASS。
        print(f"\n{'=' * 72}")
        print(f"{title or '测试'}   共 0 项   Python {sys.version.split()[0]}")
        print("=" * 72)
        print("结果: FAIL  （该文件没有收集到任何 test_* 用例 —— 零执行不通过）")
        return 1
    width = max((len(n) for n, _ in tests), default=10)
    threading.excepthook = _record_thread_exc  # 后台异常也要算账（见上）
    print(f"\n{'=' * 72}")
    print(f"{title or '测试'}   共 {len(tests)} 项   Python {sys.version.split()[0]}")
    print("=" * 72)

    skipped: list = []
    for name, fn in tests:
        try:
            fn()
        except Skip as e:
            print(f"  SKIP  {name:<{width}}  {e}")
            skipped.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name:<{width}}  {type(e).__name__}: {e}")
            traceback.print_exc()
            FAILED.append(name)
        else:
            print(f"  ok    {name:<{width}}")
            PASSED.append(name)

    print("-" * 72)
    # 后台线程未捕获异常并入结果（2026-10-30）：后台异常不再是"只打
    # stderr 不影响 PASS"。必须先取出快照再判定，避免打印中被并发追加。
    bg = background_errors()
    if FAILED:
        print(f"结果: FAIL  ({len(FAILED)} 项失败 / 共 {len(tests)} 项)")
        for n in FAILED:
            print(f"       - {n}")
        if skipped:
            # 失败与跳过同时出现时，失败清单不能盖掉跳过数：
            # 少跑的用例和跑挂的用例同样是"没被验证"。
            print(f"另有 {len(skipped)} 项跳过（未被验证，不计入通过）:")
            for n in skipped:
                print(f"       - {n}")
        if bg:
            _print_bg_errors(bg)
        return 1
    if bg:
        # 全部用例通过但后台线程抛过异常 —— 按 FAIL 处理并如实列出，
        # "N 项 PASS"不构成后台无异常的证据。
        print(f"结果: FAIL  （{len(tests)} 项用例全部通过，"
              f"但后台线程出现 {len(bg)} 个未捕获异常）")
        _print_bg_errors(bg)
        if skipped:
            print(f"另有 {len(skipped)} 项跳过（未被验证，不计入通过）")
        return 1
    if skipped:
        # 跳过的用例**不算通过**。曾因缺 numpy 把
        # test_negative_wrong_dataset_provenance 等负例静默跳过后仍打印
        # "28 项全部通过"，让人误以为来源门禁已被回归覆盖。
        print(f"结果: PASS  ({len(PASSED)} 项通过 / 共 {len(tests)} 项；"
              f"**跳过 {len(skipped)} 项未验证**)")
        for n in skipped:
            print(f"       跳过 - {n}")
        print("结论: 有跳过不等于完整验收通过；请补齐依赖后重跑。")
        return 1
    print(f"结果: PASS  ({len(tests)} 项全部通过)")
    return 0


def _print_bg_errors(bg: list) -> None:
    print(f"后台线程异常（{len(bg)} 个）—— 未捕获即失败:")
    for item in bg:
        head = item["text"].splitlines()[-1] if item["text"] else "?"
        print(f"       - [{item['thread']}] {head}")
        for line in item["text"].splitlines()[-4:-1]:
            print(f"         {line}")


def add_path(*parts: str) -> str:
    """把项目内的目录加进 sys.path（去重），返回该绝对路径。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", *parts))
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
