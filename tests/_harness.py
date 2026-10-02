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

import os
import sys
import traceback

PASSED: list = []
FAILED: list = []


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
