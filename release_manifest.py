"""发布清单 —— **整个分发链路只有这一份白名单**。

两个使用者：

* ``tools/build_release.py`` —— 按它决定 ZIP 里放哪些文件；
* ``install_addon.py`` 的 deploy 步骤 —— 按它把程序文件复制到
  ``%LOCALAPPDATA%\\Programs\\ADSAgent``。

之所以要白名单而不是"压缩整个目录"：这个仓库里有真东西 —— `config.ini`
里写着真实的 API Key 和回环令牌，``projects.json`` 是真实会话，``logs/``
是运行日志，``tests/`` 里有实机探针和一堆一次性脚本。整目录打包等于把
密钥和开发现场一起发出去。

**反过来也成立**：没在这份清单里的文件不会被安装，所以加了新模块忘了登记，
会用"ImportError"立刻暴露出来，而不是在用户机器上才炸。
"""

from __future__ import annotations

import os

#: 控制台 .bat 入口的安装/卸载名 -> 目标名字（ZIP 里用中文名，用户一眼认得）
ENTRY_LABELS = {
    "install": "安装 ADS Agent.bat",
    "uninstall": "卸载 ADS Agent.bat",
    "selfcheck": "环境自检.bat",
}


# ---------------------------------------------------------------------------
# 运行期文件（缺一不可）
# ---------------------------------------------------------------------------

#: 后端：LLM 对话循环 + 设计任务 + 本地服务。只用标准库。
BACKEND_FILES = [
    "adscompat.py",      # 跨版本兼容档案 + 门禁策略（版本/能力唯一事实来源）
    "adslocate.py",      # ADS 目录 / 解释器定位（安装器与启动器共用）
    "ads_auth.py",       # 回环令牌 + 配置文件唯一写入通道
    "adslog.py",         # 结构化文件日志
    "agent.py",          # LLM 对话循环 + 工具编排
    "config.py",         # config.ini 读取
    "design_job.py",     # 设计任务状态机与持久化
    "design_metrics.py", # 确定性指标评估
    "design_service.py", # 仿真→读数→评估编排
    "instance.py",       # 实例登记与身份校验
    "model_gate.py",     # 仿真结果模型依赖复用门禁
    "model_orchestration.py", # 模型包检查与导入编排
    "model_store.py",    # 模型包资产库与安全解压
    "model_tools.py",    # 模型库 LLM 工具编排
    "model_validation.py", # ADS 模型导入验证
    "llm.py",            # LLM 客户端
    "paths.py",          # **统一路径解析**（数据/程序双根）
    "server.py",         # HTTP/SSE 服务
    "shared_models.py",  # 跨工作区共享模型库
    "tools.py",          # 工具派发 + ADS 端 RPC
]

#: ADS 端插件：面板、工具服务、建图实现。跑在 ADS 进程里。
ADDON_FILES = [
    "__init__.py",       # 菜单与自动启动（ADS addon 契约）
    "ads_ops.py",        # 建图/改图/仿真的真实实现
    "authbridge.py",     # 按文件路径加载共享令牌模块
    "backend_launcher.py",  # 拉起并监护后端进程
    "capability.py",     # 运行时能力检测 + 工具门禁（只读探测，三态）
    "mdplain.py",        # 面板用的极简 Markdown
    "model_attachments.py", # 模型 ZIP 附件上传与卡片
    "model_deps.py",     # 仿真结果模型依赖指纹
    "model_ops.py",      # ADS 侧模型库挂接与查询
    "netlist_check.py",  # 网表等价性核对
    "panel.py",          # 聊天面板（Qt 绑定经 qtcompat，PySide2/6 自适应）
    "pathbridge.py",     # 按文件路径加载共享路径模块
    "qtcompat.py",       # Qt 绑定适配（PySide6/PySide2 二选一，绝不混用）
    "registration.py",   # 注册状态查询（ADS 进程内的官方 API）
    "project_store.py",  # 会话原子保存
    "result_page.py",    # 设计结果页
    "rf_audit.py",       # 射频/Layout 审查
    "toolserver.py",     # ADS 进程内工具服务
    "uiscale.py",        # 自适应缩放与设计令牌
]

#: 根目录
ROOT_FILES = [
    "config.example.ini",   # **只有模板**：不含任何真实密钥
    "install_addon.py",     # 安装/卸载/状态（不带 --remove 即安装）
    "release_manifest.py",  # 本清单（deploy 与打包共用）
    "check_env.py",         # 环境自检
]

#: 可选：存在才打包（缺失不影响运行）
OPTIONAL_ROOT_FILES = [
    "README.md",
    "THIRD-PARTY-NOTICES.txt",
    "LICENSE.txt",
    "CHANGELOG.md",
]

DOCS_FILES = [
    "Layout 审查设计.md",
    "原理图建图与仿真经验总结.md",
    "兼容性矩阵.md",
    "版本证据报告.md",
]

#: 放到包**根目录**的文档（源在 docs/ 下，平铺到根方便用户一眼看到）
ROOT_DOC_MAP = {
    "docs/安装说明.md": "安装说明.md",
}

#: 生成的 bat 入口（由 build_release.py / 就地安装时写出）
BAT_FILES = [
    "install_addon.bat",
    "uninstall_addon.bat",
    "selfcheck.bat",
]


# ---------------------------------------------------------------------------
# 必须排除
# ---------------------------------------------------------------------------

#: 文件名/目录名命中即排除（大小写不敏感）
EXCLUDE_NAMES = {
    "__pycache__", ".git", ".github", ".idea", ".vscode", ".pytest_cache",
    ".mypy_cache", ".workbuddy", ".workbuddy-ai", ".claude", ".cursor",
    "node_modules", ".venv", "venv", "env", "dist", "build",
    "config.ini",              # 真实配置：含 API Key 与回环令牌
    "projects.json",           # 真实会话
    "projects.json.bak",
    "design_jobs",             # 真实设计任务
    "logs",                    # 运行日志
    "tests",                   # 测试与实机探针
    "tools",                   # 打包等构建工具
    "attic",                   # 归档的过期实现
    "probes",
    "preview",
    "oneoff",
    ".DS_Store",
    "Thumbs.db",
    "de_sim.cfg", "hpeesofsim.cfg",   # 本机仿真配置，不属于分发内容
    "findings.md", "progress.md", "task_plan.md",  # 开发过程笔记
}

#: 文件名后缀
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".log",
                    ".tmp", ".bak", ".orig", ".swp")

#: 内容命中即判定为"含真实密钥"，打包前的最后一道闸
SECRET_MARKERS = (
    "sk-",
    "token = ",
    "api_key = ",
)


def is_excluded(name: str) -> bool:
    lower = name.lower()
    if lower in {n.lower() for n in EXCLUDE_NAMES}:
        return True
    return lower.endswith(EXCLUDE_SUFFIXES)


def _iter_allowed(app_root: str):
    """按白名单产出 ``(相对于 app_root 的相对路径, 绝对路径)``。"""
    groups = [
        (BACKEND_FILES, os.path.join("backend")),
        (ADDON_FILES, os.path.join("addon", "ads_agent")),
        (ROOT_FILES + OPTIONAL_ROOT_FILES + BAT_FILES, ""),
        (DOCS_FILES, "docs"),
    ]
    for names, sub in groups:
        for n in names:
            rel = os.path.join(sub, n) if sub else n
            abs_path = os.path.join(app_root, rel)
            if os.path.isfile(abs_path):
                yield rel, abs_path


def collect(app_root: str, strict: bool = False) -> tuple[list, list, list]:
    """收集发布/部署用的文件。

    返回 ``(files, missing, violations)``：
      * ``files`` —— ``[(相对路径, 绝对路径)]``
      * ``missing`` —— 白名单里登记了但磁盘上没有的（``strict`` 时视为错误）
      * ``violations`` —— 命中排除规则的登记项（清单写错了）
    """
    files: list = []
    missing: list = []
    violations: list = []
    seen = set()

    for names, sub in [
        (BACKEND_FILES, os.path.join("backend")),
        (ADDON_FILES, os.path.join("addon", "ads_agent")),
        (ROOT_FILES, ""),
    ]:
        for n in names:
            rel = os.path.join(sub, n) if sub else n
            seen.add(os.path.normcase(rel))
            abs_path = os.path.join(app_root, rel)
            if not os.path.isfile(abs_path):
                missing.append(rel)
                continue
            if is_excluded(n):
                violations.append(rel)
                continue
            files.append((rel, abs_path))

    for names, sub in [
        (OPTIONAL_ROOT_FILES + BAT_FILES, ""),
        (DOCS_FILES, "docs"),
    ]:
        for n in names:
            rel = os.path.join(sub, n) if sub else n
            seen.add(os.path.normcase(rel))
            abs_path = os.path.join(app_root, rel)
            if not os.path.isfile(abs_path):
                continue
            if is_excluded(n):
                violations.append(rel)
                continue
            files.append((rel, abs_path))

    for src_rel, dst_rel in ROOT_DOC_MAP.items():
        abs_path = os.path.join(app_root, src_rel)
        if os.path.isfile(abs_path):
            files.append((dst_rel, abs_path))

    return files, missing, violations


def scan_for_leaks(app_root: str) -> list:
    """粗查：遍历**整个** app_root，列出白名单之外且像"用户数据/产物"的东西。

    打包脚本据此报警。它不替你判断，只是把可能被打包进去的敏感内容摊开。
    """
    allowed = {os.path.normcase(r) for r, _ in _iter_allowed(app_root)}
    leaks = []
    for dirpath, dirnames, filenames in os.walk(app_root):
        dirnames[:] = [d for d in dirnames if d.lower() not in
                       {n.lower() for n in EXCLUDE_NAMES}]
        for fn in filenames:
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, app_root)
            if os.path.normcase(rel) in allowed:
                continue
            if is_excluded(fn):
                leaks.append(rel)
    return sorted(set(leaks))
