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
import traceback

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
    width = max((len(n) for n, _ in tests), default=10)
    print(f"\n{'=' * 72}")
    print(f"{title or '测试'}   共 {len(tests)} 项   Python {sys.version.split()[0]}")
    print("=" * 72)

    for name, fn in tests:
        try:
            fn()
        except Skip as e:
            print(f"  SKIP  {name:<{width}}  {e}")
            PASSED.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name:<{width}}  {type(e).__name__}: {e}")
            traceback.print_exc()
            FAILED.append(name)
        else:
            print(f"  ok    {name:<{width}}")
            PASSED.append(name)

    print("-" * 72)
    if FAILED:
        print(f"结果: FAIL  ({len(FAILED)} 项失败 / 共 {len(tests)} 项)")
        for n in FAILED:
            print(f"       - {n}")
        return 1
    print(f"结果: PASS  ({len(tests)} 项全部通过)")
    return 0


def add_path(*parts: str) -> str:
    """把项目内的目录加进 sys.path（去重），返回该绝对路径。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", *parts))
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
