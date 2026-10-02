# -*- coding: utf-8 -*-
"""布局走线坐标逻辑一键回归：全部拓扑案例的间隙模型 + 结构测试。

用法: python tests/layout_regression.py
退出码非 0 = 存在违规或测试失败。

覆盖（2026-09-29）：
  单级一分二 / 两级级联一分四 / 一分三（直通行）/
  MTEE 威尔金森（通用路径+标注避让）/ 两节耦合线滤波器（CLIN）/ 20 变量压力
"""
import os
import subprocess
import sys

HERE = os_dir = os.path.dirname(__file__)


def run(cmd):
    print("==", " ".join(cmd))
    # 子进程 stdout 是管道时 Python 回退到系统 ANSI 代码页（GBK 控制台即
    # gbk），个别字符（如 ²）会让子脚本直接 UnicodeEncodeError 崩掉，
    # 整个回归跟着误报失败。按当前控制台编码 + replace 传给子进程：
    # 可读性不变、任何字符都崩不了，也不要求用户预设环境变量。
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    child_env = dict(os.environ, PYTHONIOENCODING=f"{enc}:replace")
    r = subprocess.run([sys.executable] + cmd, cwd=HERE, env=child_env)
    return r.returncode


def main():
    bad = run(["iterate_ml_sym_metrics.py"])
    bad += run(["fuzz_layout.py", "20", "1"])
    bad += run(["test_auto_layout.py"])
    bad += run(["test_wire_geometry_gate.py"])
    bad += run(["test_ce_amp_acceptance.py"])
    bad += run(["test_cb_amp_acceptance.py"])
    bad += run(["test_annot_attribution.py"])
    print("布局回归:", "通过" if bad == 0 else f"失败（{bad} 个环节）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
