"""测试运行器负例：没有收集到用例时必须失败，零执行不算通过。"""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import eq, contains, run


def test_zero_collected_tests_fail():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = run({"not_a_test": lambda: None}, title="零执行守卫")
    eq(code, 1, "没有 test_* 函数必须返回失败")
    contains(output.getvalue(), "零执行不通过")


if __name__ == "__main__":
    raise SystemExit(run(globals(), "测试运行器零执行守卫"))
