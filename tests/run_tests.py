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

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (文件, 说明, 层级, 需要PySide6)
CASES = [
    ("test_datasheet_models.py", "手册证据、单位换算、参数持久化和 RLC 模板", "logic", False),
    ("test_parameter_semantics.py", "参数结构化条件语义绑定 / 表格证据 / semantic_verified", "logic", False),
    ("test_datasheet_curves.py", "曲线矩阵组装硬化 / 复核门禁 / 幅相配对", "logic", False),
    ("test_device_library.py", "器件模型库：身份分层 / 原子发布 / 检索 / 导入 / 放置 / 版本去重", "logic", False),
    ("test_harness_zero_guard.py", "测试运行器：零执行不得计为通过", "logic", False),
    ("test_component_symbol.py", "器件封装：层次引脚锚点 / symbol API 契约", "logic", False),
    ("test_pdf_modeling.py", "PDF 资产 / 曲线记录 / 有源建模 / 验证分账与负例", "logic", False),
    # 2026-10-14 A 阶段：仿真来源绑定（必须真跑；缺 numpy/scipy 会跳过，
    # 而这套负例历史上曾被静默跳过后仍报"全部通过"）
    ("test_sim_provenance.py", "仿真来源可信执行记录 / 占位指纹 / 独立网表状态机", "logic", False),
    # 2026-10-14 B 阶段：实测符号盒 + 引脚感知的三端封装布局走线
    ("test_three_terminal_layout.py", "三端封装布局走线 / 端点豁免 / 实测符号盒", "logic", False),
    ("test_model_context.py", "模型上下文容量来源 / 独立上限 / 实测与估算", "logic", False),
    # ---- 2026-11-04 上下文用量环补全（输入/输出/缓存命中/会话累计）----
    ("test_usage_capture.py", "流式 usage 捕获：stream_options 注入 / 400 降级 / 多块合并", "logic", False),
    ("test_model_context_cache.py", "缓存用量规范化 / usage() 新字段 / 会话累加不变量", "logic", False),
    ("test_context_usage_flow.py", "上下文用量全链路：后端 usage → 事件 → 面板归一化", "logic", False),
    ("test_context_usage_ui.py", "用量环详情浮层 / tooltip 缺字段降级 / 会话累计", "qt", True),
    # ---- 2026-11-04 工作区对话式创建与打开（离线 fake de，不需要 ADS 实机）----
    ("test_workspace_create.py", "工作区新建：目录/首个库创建与拒绝原因", "logic", False),
    ("test_workspace_discovery.py", "工作区发现：有界扫描/最近记录/根目录解析", "logic", False),
    ("test_workspace_tools.py", "工作区工具：schema 参数/超时/默认根注入", "logic", False),
    ("test_workspace_e2e.py", "工作区对话链路端到端（若改用 Qt 需改为 qt/True）", "logic", False),
    ("test_python_helpers.py", "跨调用脚本助手恢复 / 用户变量保留", "logic", False),
    # ---------------- logic ----------------
    ("test_token_migration.py", "令牌生成 / 迁移 / 两端一致 / 并发收敛", "logic", False),
    ("test_config_write_race.py", "config.ini 并发写入协调 / 不丢设置与令牌", "logic", False),
    ("test_provider_remove.py", "删除供应商 provider_remove 语义", "logic", False),
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
    ("test_rfcalc.py", "RF 计算引擎：传输线/噪声/杂散/偏置公式", "logic", False),
    ("test_toolserver_busy.py", "toolserver 忙碌状态 / 多作业互不干扰", "http", False),
    ("test_toolserver_lifecycle.py", "工具服务线程恢复 / 独占端口 / 配置变化", "http", False),
    ("test_turn_budget.py", "轮次预算收尾 / 重复失败保护 / 总结取消", "logic", False),
    ("test_cancel_chain.py", "取消链路：排队取消 / 过期跳过 / 队列容量 / pump 预算", "logic", False),
    # ---- 2026-10-30 ADS 未响应修复（run_compute / 取消链贯通）----
    ("test_compute_worker.py", "计算 worker：有界计算 / 协作取消 / 超时看门狗 / 输出洪泛", "logic", False),
    ("test_compute_manager.py", "计算管理器：队列容量 / 并发上限 / 崩溃回收 / 幂等 / 重启清扫", "logic", False),
    ("test_pump_budget.py", "pump 时间预算 / 原子入队 / 活跃上限 / 心跳 / health", "http", False),
    ("test_chat_cancel_http.py", "真实 HTTP 取消链：聊天停止→/cancel 作业身份贯穿", "http", False),
    ("test_submit_timeout_state.py", "submit 等待超时后作业仍执行的真实状态", "http", False),
    ("test_compute_turn_cancel.py", "跨模块集成：轮次取消传播到计算作业并回收", "logic", False),
    ("test_run_python_boundary.py", "run_python 取消边界 / 主线程占用提示 / 阶段上报", "logic", False),
    ("test_panel_stop_latency.py", "面板停止：读线程及时退出 / turn_id 传播 / 取消两阶段文案", "qt", True),
    ("test_layout_golden.py", "布局/走线 golden 基线锁（固定 PYTHONHASHSEED 子进程）", "logic", False),
    ("test_save_throttle.py", "会话保存合并写 / 活动行就地刷新 / 关闭前落定", "qt", True),
    ("test_health_compute.py", "后端 /health 的计算作业视图（running/queued/降级不 500）", "http", False),
    ("test_sim_cancel.py", "仿真取消：进程树回收 / 执行预算 / 输出洪泛 / 无孤儿", "logic", False),
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
    ("test_model_store.py", "模型包资产：保存去重 / 中文路径 / 识别 / 越界拒绝 / 并发 / 迁移", "logic", False),
    ("test_shared_models.py", "共享库：目录持久化 / 跨工作区复制 / 型号索引 / 重打包去重", "logic", False),
    ("test_shared_import_binding.py", "共享导入：固定目标工作区 / 复制前后取消 / 切换拦截", "logic", False),
    ("test_workspace_model_dependencies.py", "模型仿真依赖：工作区映射隔离 / Include / 枚举回调", "logic", False),
    ("test_model_http.py", "模型包 HTTP：二进制上传 / 鉴权 / 中文名 / 工作区绑定 / 幂等", "http", True),
    ("test_model_ops.py", "模型包 ADS 侧纯逻辑：lib.defs 解析 / 路径解析 / 工具名齐全", "logic", False),
    ("test_model_ops_flow.py", "模型包 ADS 侧流程：挂接幂等 / 只读约束 / 卸载不越界 / 验证门禁", "logic", False),
    ("test_model_part_choices.py", "料号选项表：物理量解析 / 枚举 / 选值核对 / 检索与容差", "logic", False),
    ("test_model_dataset_report.py", "冒烟数据集核对：块名/列名 / NaN / 常数 / 端口数", "logic", False),    ("test_dataset_expr.py", "数据集读数：块.列表达式 / 列名等价 / 缺失列提示", "logic", False),
    ("test_model_includes.py", "模型 Include：空表补齐 / 相对路径 / 不重复包含", "logic", False),
    ("test_model_import_flow.py", "模型包导入编排：操作恢复 / 用户选择 / 取消 / 幂等", "logic", False),
    ("test_model_open_http.py", "原生列表打开路由：可信上下文注入 / 越权拦截 / 失败如实报码", "http", False),
    ("test_model_deps_gate.py", "模型依赖指纹与仿真缓存/条件门禁", "logic", False),
    # ---------------- qt ----------------
    ("test_project_isolation.py", "项目会话隔离 / 如实报告仿真行为", "qt", True),
    ("test_design_isolation.py", "设计结果页：项目隔离与重启恢复", "qt", True),
    ("test_result_page_labeling.py", "结果页数据标识：显示采样点 vs 完整数据 / 完整导出", "qt", True),
    ("test_md_plain.py", "Markdown 气泡清洗：修饰符剥离 / 结构转换 / 标识符保护", "qt", True),
    ("test_thinking.py", "深度思考链路：thinking 门控与降级 / reasoning 下发 / 折叠行", "qt", True),
    ("test_ui_navigation.py", "设置返回 / 草稿保留 / 小窗滚动 / 侧栏恢复 / 关闭面板", "qt", True),
    ("test_model_attachments.py", "模型包附件：拖放 / 上传 / 卡片状态 / 项目隔离与恢复", "qt", True),
    ("test_rfcalc_panel.py", "RF 计算器窗口：页面树/表单栈/冒烟计算/基片填值", "qt", True),
    # ---- 2026-11-04 顶栏更新按键常驻（无新版本时显示当前版本号；本目录既有的
    # test_update_ui.py / test_update_service.py 未登记，故接在 qt 组末尾）----
    ("test_update_top_btn_version.py", "顶栏更新按键：常驻显示当前版本号 / 点击分流 / never-raise", "qt", True),
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
        # 子进程的中文/emoji 输出一律按 UTF-8 写（_harness 里也会 reconfigure，
        # 这里保证"没走 _harness 的早期输出"也一致，且父进程按同一编码解码）
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.run(
                [sys.executable, path], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=child_env, timeout=DEFAULT_TIMEOUT_S,
            )
            rc, text = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            print(f"  超时  {filename:<28} 超过 {DEFAULT_TIMEOUT_S}s，按失败计")
            timed_out.append(filename)
            continue

        # 无 PySide6 时按"跳过"处理（未执行，不计入失败）：
        #   * 多数 qt 文件用 sys.exit(2) 守护 → rc==2 且文案含 PySide6；
        #   * 但有的文件缺守卫：test_ui_navigation.py 顶层 import PySide6、
        #     test_model_http.py 在用例体内 import 触发（间接依赖 PySide6），
        #     二者都会在 _harness 里抛 ModuleNotFoundError → 整文件 rc==1。
        #     它们已登记 needs_pyside=True，据"确实缺 PySide6 模块"判为跳过，
        #     否则会被误计为失败（历史上正是这个漏标）。
        pyside_absent = "No module named 'PySide6'" in text
        if rc != 0 and ((rc == 2 and "PySide6" in text)
                        or (needs_pyside and pyside_absent)):
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
