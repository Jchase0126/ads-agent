"""跑一遍所有**不需要 ADS 实机**的测试。

    python tests/run_tests.py              # 全部（逻辑 + HTTP 集成 + Qt）
    python tests/run_tests.py --stdlib     # 只跑纯逻辑层（任何解释器都能跑）
    python tests/run_tests.py --list       # 只列出分层清单，不执行

分四层（2026-10-02 起）：
    logic  纯逻辑测试（不需要 PySide6 / 网络）
    http   HTTP 集成测试（起真实本地服务，不依赖 ADS）
    qt     Qt 界面测试（需要 PySide6，offscreen 跑真实控件）
    ads    ADS 实机验收（本脚本不跑，需 ADS 2027 在运行状态）：
           tests/test_loopback.py  —— toolserver 回环冒烟（用 ADS 内置解释器跑）
           tests/probe_via_ads.py  —— ADS 进程内探针入口
           tests/layout_regression.py —— 布局回归编排

报告明确区分 通过 / 失败 / 跳过 / 超时 —— 有跳过时结论写明
"跳过 N 个"，绝不把"跑过的都过了"说成"完整验收通过"。
每个测试文件有独立超时，单个文件卡住不会拖死整个套件。

退出码：0 = 没有失败（跳过不算失败）；1 = 有失败或超时。
"""

import locale
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (文件, 说明, 层级, 需要PySide6)
CASES = [
    # ---------------- logic ----------------
    ("test_token_migration.py", "令牌生成 / 迁移 / 两端一致 / 并发收敛", "logic", False),
    ("test_config_write_race.py", "config.ini 并发写入协调 / 不丢设置与令牌", "logic", False),
    ("test_auth.py", "后端接口鉴权 / 令牌不落日志 / keep-alive", "http", False),
    ("test_sim_output_dir.py", "仿真输出目录唯一性 / 不覆盖旧结果", "logic", False),
    ("test_build_gate.py", "写入备份 / 保存后复核 / 仿真门禁 / 日志语义 / 每轮汇总", "logic", False),
    ("test_auto_layout.py", "原理图自动布局与正交走线", "logic", False),
    ("test_wire_geometry_gate.py", "保存后导线几何门禁", "logic", False),
    ("test_cb_amp_acceptance.py", "CB 放大器布局走线验收", "logic", False),
    ("test_ce_amp_acceptance.py", "CE 放大器布局验收 + 新规则矩阵", "logic", False),
    ("test_annot_attribution.py", "标注归属：区模型实测常数 / 悬空拦截 / 跨度让位", "logic", False),
    ("test_netlist_check.py", "网表级电气等价校验：解析/划分/破坏实验", "logic", False),
    ("test_rf_audit.py", "射频物理审查：单位/闭式阻抗/MTEE 角色/宽度/Layout 几何/诚实标注", "logic", False),
    ("test_sp_traces.py", "S 参数矩阵表达式与 dB 曲线", "logic", False),
    ("test_toolserver_busy.py", "toolserver 忙碌状态 / 多作业互不干扰", "http", False),
    ("test_cancel_chain.py", "取消链路：排队取消 / 过期跳过 / 队列容量 / pump 预算", "logic", False),
    ("test_design_metrics.py", "设计指标：数据解析 / 指标计算 / 不编造数值", "logic", False),
    ("test_design_metrics_edge.py", "指标判定边界：覆盖不足 / 单位不可比 / 越界取点 / 带宽口径", "logic", False),
    ("test_design_job.py", "设计任务：状态机 / 持久化 / 编排与失败保留产物", "logic", False),
    ("test_design_persist.py", "持久化：保存失败上账 / 检查点 / 幂等 / 并发串行 / 迁移", "logic", False),
    ("test_design_workspace.py", "工作区绑定：跨工作区拦截 / 网表指纹复用验证", "logic", False),
    ("test_llm_integrity.py", "LLM：流完整性 / 残缺参数拦截 / 结果摘要 / 上下文预算 / 免密", "logic", False),
    ("test_project_store.py", "会话原子保存 / 备份恢复", "logic", False),
    # ---- 可分发改造（2026-10-07 新增）----
    ("test_paths.py", "统一路径解析：双根 / 中文空格路径 / 首启模板", "logic", False),
    ("test_migration.py", "旧目录迁移：不覆盖有效数据 / 失败保留原文件", "logic", False),
    ("test_registration.py", "安装注册：幂等 / 路径更新 / 保留其它插件 / 回滚", "logic", False),
    ("test_upgrade.py", "升级保留设置与数据 / 卸载默认保留用户数据", "logic", False),
    ("test_instance.py", "实例身份校验：不以外来服务当自己人 / 多开检测", "logic", False),
    ("test_tool_identity.py", "工具服务身份校验 / 端口冲突人话化 / 退出清登记", "logic", False),
    ("test_packaging.py", "打包白名单 / 不含密钥 / 解出来的包能独立运行", "logic", False),
    # ---- 跨版本兼容（2026-10-07 新增）----
    ("test_adscompat.py", "版本识别（注册表权威） / 官方档案 / 门禁矩阵 / 位数判定", "logic", False),
    ("test_capability.py", "运行时能力检测：假 keysight 接口树 / 三态 / fail-closed", "logic", False),
    ("test_qtcompat.py", "Qt 绑定适配：已加载优先 / 终身缓存 / Qt5-Qt6 shim", "logic", False),
    ("test_install_multiversion.py", "多版本安装：分别注册 / 登记表 / 卸载隔离 / 中文空格路径", "logic", False),
    # ---------------- qt ----------------
    ("test_startup_health.py", "真实 Qt 启动 / 健康探测 / 端口释放", "qt", True),
    ("test_ui_navigation.py", "模型管理 / 供应商隔离 / 输入栏选择与响应式布局", "qt", True),
    ("test_project_isolation.py", "项目会话隔离 / 如实报告仿真行为", "qt", True),
    ("test_design_isolation.py", "设计结果页：项目隔离与重启恢复", "qt", True),
    ("test_result_page_labeling.py", "结果页数据标识：显示采样点 vs 完整数据 / 完整导出", "qt", True),
    ("test_md_plain.py", "Markdown 气泡清洗：修饰符剥离 / 结构转换 / 标识符保护", "qt", True),
    ("test_thinking.py", "深度思考链路：thinking 门控与降级 / reasoning 下发 / 折叠行", "qt", True),
]

DEFAULT_TIMEOUT_S = 240


def main() -> int:
    stdlib_only = "--stdlib" in sys.argv
    if "--list" in sys.argv:
        for filename, label, layer, _p in CASES:
            print(f"  {layer:<6} {filename:<32} {label}")
        return 0

    cases = [c for c in CASES if not (stdlib_only and c[3])]
    print(f"解释器: {sys.executable}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"范围: {'纯逻辑层（--stdlib）' if stdlib_only else 'logic + http + qt'}，共 {len(cases)} 个文件")
    print("=" * 72)

    failed, skipped, timed_out, passed = [], [], [], []
    for filename, label, layer, needs_pyside in cases:
        path = os.path.join(HERE, filename)
        child_env = dict(os.environ)
        child_env.pop("PYTHONIOENCODING", None)  # 子测试里的再下一层 subprocess 用系统编码
        try:
            proc = subprocess.run(
                [sys.executable, path], capture_output=True, text=True,
                encoding=locale.getpreferredencoding(False), errors="replace",
                env=child_env, timeout=DEFAULT_TIMEOUT_S,
            )
            rc, text = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            print(f"  超时  {filename:<28} 超过 {DEFAULT_TIMEOUT_S}s，按失败计")
            timed_out.append(filename)
            continue

        if rc == 2 and "PySide6" in text:
            print(f"  跳过  {filename:<28} 需要 PySide6（pip install PySide6）")
            skipped.append(filename)
            continue

        for line in text.splitlines():
            if line.startswith("结果:") or line.strip().startswith(("ok ", "FAIL", "SKIP")):
                print(f"        {line.rstrip()}")

        if rc == 0:
            print(f"  通过  {filename:<28} [{layer:<5}]  {label}")
            passed.append(filename)
        else:
            print(f"  失败  {filename:<28} [{layer:<5}]  {label}")
            print("-" * 72)
            print(text[-3000:])
            print("-" * 72)
            failed.append(filename)

    print("=" * 72)
    broken = failed + timed_out
    summary = (f"通过 {len(passed)} / 失败 {len(failed)}"
               f" / 超时 {len(timed_out)} / 跳过 {len(skipped)}"
               f"（共 {len(cases)} 个）")
    if broken:
        print(f"结果: FAIL —— {summary}")
        print(f"未通过: {', '.join(broken)}")
        if skipped:
            print(f"跳过（未执行，不计入通过）: {', '.join(skipped)}")
        return 1
    print(f"结果: PASS —— {summary}")
    if skipped:
        # 跳过必须如实列出：这部分**没有执行**，不能宣称完整验收
        print(f"注意: 有 {len(skipped)} 个文件因缺 PySide6 跳过，"
              f"未执行（用带 PySide6 的解释器运行可补齐）: {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
