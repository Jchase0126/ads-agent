"""Shared configuration loader for the ADS Agent backend and install tools.

Reads config.ini at the project root; environment variables override:
  ADS_AGENT_API_KEY      -> [llm] api_key
  ADS_AGENT_BASE_URL     -> [llm] base_url
  ADS_AGENT_MODEL        -> [llm] model
  ADS_AGENT_CONFIG       -> 配置文件路径（默认 <项目根>/config.ini，测试/便携安装用）

回环令牌不在这里定义默认值：它由 backend/ads_auth.py 统一负责生成与读取，
后端和 ADS 端工具服务必须拿到同一个值（见该模块的说明）。
"""

import configparser
import json
import model_context
import os
import re
from urllib.parse import urlparse

import ads_auth
import paths

#: 代码所在目录（只读的程序文件）
APP_ROOT = paths.app_root()

#: **用户数据根目录** —— 设计任务等数据存在这里，不在代码边上。
#: 旧变量名保留是为了少改调用点，但语义已经变了（见 backend/paths.py）。
PROJECT_ROOT = paths.data_root()

CONFIG_PATH = ads_auth.config_path()

DEFAULTS = {
    "llm_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "llm_model": "glm-4.6",
    "llm_api_key": "",
    # 深度思考：auto=智谱官方接口自动开启 / on=始终请求（服务不支持自动降级）/ off=关闭
    "llm_thinking": "auto",
    "backend_host": "127.0.0.1",
    "backend_port": 8760,
    "ads_host": "127.0.0.1",
    "ads_port": 8761,
    # 单轮对话允许的 LLM 工具调用轮数。太小时，稍微需要探索的任务
    # （建视图 / 放元件 / 调参数再复测）会在半路被掐断。
    "max_tool_steps": 30,
    # 长会话上下文预算（字符数）：超过后压缩较早历史，保留系统提示、
    # 原始需求与最近消息。0 = 不压缩。
    "context_budget_chars": 120000,
    # 仿真类工具的等待上限（秒）。会被下发到 ADS 端，两端用同一个值。
    "sim_timeout": 900,
    # 把耗时仿真放到后台线程执行（不占用 ADS 主线程）。
    # 网表生成等必须访问 DE 数据库的步骤始终留在主线程；见 ads_ops.run_simulation。
    "sim_off_main_thread": True,
    # 取消仿真时给 hpeesofsim 的收尾宽限（秒）：先 terminate，超时才
    # taskkill /T 整棵**自己 spawn 的**进程树（绝不按进程名批量杀）。
    "sim_cancel_grace_s": 3.0,
    # 统一模型库：ZIP 备份和索引放在此目录，各 ADS Workspace 按需导入副本。
    "model_library_root": "",
    # PDF 解析运行时：后端解释器缺 pymupdf 时，指向一个装了 pymupdf 的
    # 解释器，解析在子进程里跑同一份代码（backend/pdf_worker.py）。
    # 留空 = 只用进程内引擎 + 受限降级扫描。
    "datasheet_runtime_python": "",
    # create_workspace 未给 root/path 时的默认父目录（[agent] workspace_root）。
    # 只由 backend/tools.py 的 call() 注入 —— ADS 进程拿不到后端配置，
    # 所以不能在 ADS 端读这个键。留空 = 交给 ADS 端按自己的根目录规则解析。
    "workspace_root": "",
    # ---- 独立计算 worker（run_compute，2026-10-30）------------------------
    # NumPy/SciPy 优化、拟合、大规模数据处理在后端计算子进程里执行，
    # 不占 ADS 主线程。以下键都在 [agent] 段。
    # 同时运行的计算作业上限。
    "compute_max_workers": 2,
    # 排队容量：队列满时 run_compute 立即返回 busy（可预测拒绝，不无限积压）。
    "compute_queue_capacity": 8,
    # 单个计算作业默认执行预算（秒）；代码可按作业覆盖，硬上限见下一项。
    "compute_timeout_s": 600,
    # 执行预算硬上限（秒）：超过这个值的 timeout_s 请求会被拒绝/截断。
    "compute_max_timeout_s": 7200,
    # 取消宽限期（秒）：请求停止后给协作退出的时间，超时才 terminate。
    "compute_cancel_grace_s": 3.0,
    # 计算子进程解释器：留空用当前后端解释器；需要独立 NumPy/SciPy 环境
    # 时指向其 python.exe（依赖探测失败会明确报错，不会静默换解释器）。
    "compute_python": "",
    # 计算作业目录（request/result/log 与进程记录）：留空用数据根下 compute_jobs。
    "compute_base_dir": "",
    # ---- 崩溃反馈（2026-11-03）------------------------------------------
    # 这些键都在 [feedback] 段。**刻意**用 feedback_ 前缀：backend/feedback.py
    # 的 settings() 按**短键名**（enabled/auto_open_browser/...）合并调用方
    # 传进来的 dict，若这里也叫 enabled，CFG 被整体传进去时就会互相顶掉。
    # 后端的做法是 server.py 里显式映射成短键名再交给 feedback.settings()。
    # 总开关：关掉后不装异常钩子、不写报告（心跳仍写，用于卡死判定）。
    "feedback_enabled": True,
    # 崩溃时自动用浏览器打开预填好的 GitHub issue 页（默认关：不打扰用户）。
    "feedback_auto_open_browser": False,
    # 报告里附上最近日志摘录（默认开：没有日志的崩溃报告基本没法看）。
    "feedback_include_logs": True,
    # 附多少行日志（feedback._tail_lines 内部再夹到 1..2000）。
    "feedback_max_log_lines": 120,
    # 卡死判定阈值（秒）：ADS 端心跳超过这么久没更新就认为卡住了。0 = 关闭判定。
    "feedback_hang_seconds": 180,
    # 联网时自动把脱敏报告直接上传到 GitHub issue（2026-11-03，默认开）。
    "feedback_auto_upload": True,
    # GitHub PAT（只允许往插件仓库开 issue 的最小权限令牌）。留空=不上传。
    # 也可用环境变量 ADS_AGENT_FEEDBACK_TOKEN；该值不出现在任何 UI。
    "feedback_upload_token": "",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    config_path = ads_auth.config_path()
    parser = configparser.ConfigParser(interpolation=None)
    if os.path.exists(config_path):
        parser.read(config_path, encoding="utf-8")

    def get(section, key, out_key, cast=str):
        if parser.has_option(section, key):
            raw = parser.get(section, key).strip()
            if raw != "":
                cfg[out_key] = cast(raw)

    def get_bool(section, key, out_key):
        if parser.has_option(section, key):
            raw = parser.get(section, key).strip().lower()
            if raw:
                cfg[out_key] = raw in ("1", "true", "yes", "on")

    gi = int
    gf = float
    get("llm", "base_url", "llm_base_url")
    get("llm", "model", "llm_model")
    get("llm", "api_key", "llm_api_key")
    get("llm", "thinking", "llm_thinking")
    get("backend", "host", "backend_host")
    get("backend", "port", "backend_port", gi)
    get("ads", "host", "ads_host")
    get("ads", "port", "ads_port", gi)
    get("agent", "max_tool_steps", "max_tool_steps", gi)
    get("agent", "sim_timeout", "sim_timeout", gi)
    get("agent", "context_budget_chars", "context_budget_chars", gi)
    get_bool("agent", "sim_off_main_thread", "sim_off_main_thread")
    get("agent", "sim_cancel_grace_s", "sim_cancel_grace_s", gf)
    # create_workspace 未给 root/path 时作为默认父目录（见 backend/tools.py）
    get("agent", "workspace_root", "workspace_root")
    # 独立计算 worker（run_compute，2026-10-30）
    get("agent", "compute_max_workers", "compute_max_workers", gi)
    get("agent", "compute_queue_capacity", "compute_queue_capacity", gi)
    get("agent", "compute_timeout_s", "compute_timeout_s", gi)
    get("agent", "compute_max_timeout_s", "compute_max_timeout_s", gi)
    get("agent", "compute_cancel_grace_s", "compute_cancel_grace_s", gf)
    get("agent", "compute_python", "compute_python")
    get("agent", "compute_base_dir", "compute_base_dir")
    get("models", "library_root", "model_library_root")
    get("datasheet", "runtime_python", "datasheet_runtime_python")
    # 崩溃反馈（[feedback] 段，2026-11-03）
    get_bool("feedback", "enabled", "feedback_enabled")
    get_bool("feedback", "auto_open_browser", "feedback_auto_open_browser")
    get_bool("feedback", "include_logs", "feedback_include_logs")
    get("feedback", "max_log_lines", "feedback_max_log_lines", gi)
    get("feedback", "hang_seconds", "feedback_hang_seconds", gi)
    get_bool("feedback", "auto_upload", "feedback_auto_upload")
    get("feedback", "upload_token", "feedback_upload_token")
    # 留空时由共享库模块按当前 ADS Workspace 的父目录解析为同级 libraries。
    cfg["model_library_root"] = (
        os.environ.get("ADS_AGENT_LIBRARY_ROOT", "").strip()
        or cfg["model_library_root"])

    cfg["llm_api_key"] = os.environ.get("ADS_AGENT_API_KEY", cfg["llm_api_key"])
    cfg["llm_base_url"] = os.environ.get("ADS_AGENT_BASE_URL", cfg["llm_base_url"])
    cfg["llm_model"] = os.environ.get("ADS_AGENT_MODEL", cfg["llm_model"])

    # 显式删空与旧配置缺省不同，不能用默认值或环境变量复活供应商。
    empty_providers = parser.getboolean("llm", "providers_empty", fallback=False)
    if empty_providers:
        cfg["llm_model"] = cfg["llm_base_url"] = cfg["llm_api_key"] = ""

    # 令牌的唯一来源：缺失或仍是公开默认值时，这里会生成并写回 config.ini。
    # 不用 get("ads","token",...) 读原始值——那样会把公开默认值当成有效配置。
    cfg["ads_token"] = ads_auth.ensure_token()

    models = []
    if parser.has_option("llm", "models"):
        raw = parser.get("llm", "models").strip()
        models = [m.strip() for m in raw.split(",") if m.strip()]
    if empty_providers:
        models = []
    elif cfg["llm_model"] and cfg["llm_model"] not in models:
        models.insert(0, cfg["llm_model"])
    cfg["llm_models"] = models
    cfg["llm_profiles"] = _read_profiles(parser)
    return cfg


def _read_profiles(parser) -> dict:
    """Read independent connections; old model lists inherit the legacy connection."""
    raw = parser.get("llm", "profiles", fallback="{}", raw=True)
    profiles = json.loads(raw)
    if not isinstance(profiles, dict):
        raise ValueError("模型连接配置必须为对象")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not isinstance(profile, dict):
            raise ValueError("模型连接配置格式无效")
        if not all(isinstance(profile.get(k, ""), str) for k in ("base_url", "api_key", "provider_name")):
            raise ValueError("模型连接参数必须为字符串")
        if not isinstance(profile.get("enabled", True), bool):
            raise ValueError("供应商启用状态必须为布尔值")
        tokens = profile.get("context_window_tokens", 0)
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("模型上下文上限必须为非负整数 tokens")
    if parser.getboolean("llm", "providers_empty", fallback=False):
        return {}
    connection = {
        "base_url": parser.get("llm", "base_url", fallback=DEFAULTS["llm_base_url"], raw=True),
        "api_key": parser.get("llm", "api_key", fallback="", raw=True),
    }
    names = parser.get("llm", "models", fallback="", raw=True).split(",")
    names.append(parser.get("llm", "model", fallback=DEFAULTS["llm_model"], raw=True))
    for name in names:
        if name.strip():
            profiles.setdefault(name.strip(), dict(connection))
    return profiles


def model_profile(name: str) -> dict | None:
    """Full credential for the authenticated editor only, never config status/logs."""
    cfg = load()
    profile = cfg["llm_profiles"].get(name)
    if profile is None:
        return None
    return {"model": name, "base_url": profile.get("base_url", ""),
            "api_key": profile.get("api_key", ""), "provider_name": provider_label(profile),
            "enabled": profile.get("enabled", True),
            "context_window_tokens": profile.get("context_window_tokens", 0),
            "models": next((g['models'] for g in provider_groups(cfg) if name in g['models']), [name])}


def _connection_identity(profile):
    return (profile.get('base_url', '').strip().rstrip('/'), profile.get('api_key', ''), provider_label(profile))


def provider_groups(cfg):
    """Public provider memberships; credentials are used only internally for grouping."""
    grouped = {}
    for name in cfg['llm_models']:
        profile = cfg['llm_profiles'].get(name)
        if profile is None:
            continue
        identity = _connection_identity(profile)
        group = grouped.setdefault(identity, dict(model=name, provider_name=provider_label(profile), enabled=profile.get("enabled", True), models=[]))
        group['enabled'] = group['enabled'] and profile.get("enabled", True)
        group['models'].append(name)
    return list(grouped.values())


def provider_label(profile):
    if profile.get("provider_name", "").strip():
        return profile["provider_name"].strip()
    host = urlparse(profile.get("base_url", "")).hostname or ""
    return {"api.deepseek.com": "DeepSeek", "open.bigmodel.cn": "智谱 GLM",
            "api.openai.com": "OpenAI", "127.0.0.1": "本地服务",
            "localhost": "本地服务"}.get(host, host or "自定义供应商")


def profile_labels(cfg):
    return {name: provider_label(profile) for name, profile in cfg["llm_profiles"].items()}


def ads_base_url(cfg: dict) -> str:
    return f"http://{cfg['ads_host']}:{cfg['ads_port']}"


def persist_model_library_root(root: str, only_if_empty: bool = True) -> str:
    """Select the first shared root under the configuration lock, preserving other settings.

    Unlike interactive LLM settings, a failed lock must not fall back to an
    unlocked write: two first imports must not choose different shared roots.
    Explicit edits pass only_if_empty=False; the environment override is never written.
    """
    root = str(root).strip()
    if not os.path.isabs(root) or "\n" in root or "\r" in root:
        raise ValueError("模型库目录必须为绝对路径")
    lock = ads_auth.config_lock()
    if not lock.acquire():
        raise RuntimeError("无法锁定模型库配置，请稍后重试")
    try:
        lines = ads_auth._read_lines()
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string("\n".join(lines))
        previous = parser.get("models", "library_root", fallback="").strip()
        if only_if_empty and previous:
            return previous
        start = next((i for i, line in enumerate(lines)
                      if line.strip().lower() == "[models]"), None)
        if start is None:
            lines.extend(["", "[models]"])
            start = len(lines) - 1
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip().startswith("[")), len(lines))
        index = next((i for i in range(start + 1, end)
                      if re.match(r"\s*library_root\s*=", lines[i], re.I)), None)
        if index is None:
            lines.insert(end, "library_root = " + root)
        else:
            lines[index] = "library_root = " + root
        ads_auth._atomic_write_lines(lines)
        return root
    finally:
        lock.release()


def key_hint(key: str) -> str:
    """Masked representation of an API key, safe to show in the panel."""
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:4] + "****" + key[-4:]


def update_llm_settings(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    models: list | None = None,
    provider_name: str | None = None,
    provider_models: list | None = None,
    provider_model: str | None = None,
    provider_enabled: bool | None = None,
    context_model: str | None = None,
    context_window_tokens: int | None = None,
    provider_remove: bool | None = None,
) -> dict:
    """Persist [llm] settings into config.ini, preserving comments & other keys.

    Only provided values are written. Returns the effective settings after
    the update; the in-memory CFG in server.py must be refreshed separately.

    provider_remove=True 时删除 provider_model 所属的整个供应商（同一连接
    身份的全部模型 profile），活动模型落在被删组内时会切到剩余模型。

    写入走 ads_auth.edit_config —— 与令牌生成/轮换**共用同一个跨进程锁**，
    否则"保存 LLM 设置"和"轮换令牌"会各自按旧快照整文件写回，
    把对方刚写的字段覆盖掉（典型：刚轮换的令牌被还原成公开默认值）。
    """
    if provider_enabled is not None and not isinstance(provider_enabled, bool):
        raise ValueError("供应商启用状态必须为布尔值")
    if provider_remove is not None and not isinstance(provider_remove, bool):
        raise ValueError("供应商删除标记必须为布尔值")
    if context_window_tokens is not None and (isinstance(context_window_tokens, bool)
            or not isinstance(context_window_tokens, int) or context_window_tokens < 0):
        raise ValueError("模型上下文上限必须为非负整数 tokens")
    updates: dict[str, str] = {}
    deleted_models: list[str] = []
    if base_url is not None and base_url.strip():
        updates["base_url"] = base_url.strip()
    if model is not None and model.strip():
        updates["model"] = model.strip()
    if models:
        updates["models"] = ", ".join(models)
    if api_key is not None:  # empty string clears the key on purpose
        updates["api_key"] = api_key.strip()

    def mutate(lines: list) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string("\n".join(lines))
        profiles = _read_profiles(parser)
        active = parser.get("llm", "model", fallback=DEFAULTS["llm_model"])
        target = model.strip() if model is not None and model.strip() else active
        origin = profiles.get(provider_model) if provider_model else None
        if provider_model and origin is None:
            raise ValueError('供应商配置不存在，请重新载入')
        previous_members = [n for n, p in profiles.items() if origin is not None
                            and _connection_identity(p) == _connection_identity(origin)]
        connection = dict(profiles.get(target, profiles.get(active, {
            "base_url": DEFAULTS["llm_base_url"], "api_key": ""})))
        connection.pop("context_window_tokens", None)
        if base_url is not None and base_url.strip():
            connection["base_url"] = base_url.strip()
        if api_key is not None:
            connection["api_key"] = api_key.strip()
        if provider_name is not None:
            connection["provider_name"] = provider_name.strip()
        if provider_enabled is not None:
            connection["enabled"] = provider_enabled
        if provider_remove:
            if origin is None:
                raise ValueError('请指定要删除的供应商')
            env_model = os.environ.get("ADS_AGENT_MODEL", "").strip()
            if env_model and env_model in previous_members:
                raise ValueError('该模型由环境变量 ADS_AGENT_MODEL 指定，请先移除环境变量再删除供应商')
            deleted_models.extend(previous_members)
            for name in previous_members:
                profiles.pop(name)
            saved = models if models is not None else parser.get(
                'llm', 'models', fallback=active).split(',')
            remaining = list(dict.fromkeys(n.strip() for n in saved
                                           if n.strip() and n.strip() not in previous_members))
            remaining = [n for n in remaining if n in profiles]
            remaining.extend(n for n in profiles if n not in remaining)
            if active not in profiles:
                active = next((n for n in remaining if profiles[n].get('enabled', True)),
                              remaining[0] if remaining else "")
            updates["model"] = active
            updates["models"] = ", ".join(remaining)
            updates["providers_empty"] = "false" if profiles else "true"
            # legacy 顶层连接键跟随新的活动模型，避免残留指向已删供应商
            conn = dict(profiles.get(active, {"base_url": "", "api_key": ""}))
            conn.pop("context_window_tokens", None)
            updates["base_url"] = conn.get("base_url", "")
            updates["api_key"] = conn.get("api_key", "")
        elif provider_models is not None:
            if not isinstance(provider_models, list) or not provider_models or not all(
                    isinstance(n, str) and n.strip() for n in provider_models):
                raise ValueError('请至少选择一个有效模型')
            members = list(dict.fromkeys(n.strip() for n in provider_models))
            if target not in members:
                raise ValueError('当前模型必须属于该供应商')
            for name in members:
                if name in profiles and name not in previous_members and _connection_identity(profiles[name]) != _connection_identity(connection):
                    raise ValueError('模型已属于其他供应商：' + name)
            for name in previous_members:
                if name not in members:
                    profiles.pop(name)
            for name in members:
                old_limit = profiles.get(name, {}).get("context_window_tokens", 0)
                profiles[name] = dict(connection)
                if old_limit:
                    profiles[name]["context_window_tokens"] = old_limit
            saved = models if models is not None else parser.get('llm', 'models', fallback=active).split(',')
            updates['models'] = ', '.join(dict.fromkeys([n.strip() for n in saved
                if n.strip() and (n.strip() not in previous_members or n.strip() in members)] + members))
        elif target:
            old_limit = profiles.get(target, {}).get("context_window_tokens", 0)
            profiles[target] = connection
            if old_limit:
                profiles[target]["context_window_tokens"] = old_limit
        if context_window_tokens is not None and not provider_remove:
            limit_model = (context_model or target).strip()
            if limit_model not in (members if provider_models is not None else [target]):
                raise ValueError("上下文设置的模型必须在当前供应商选中的模型列表中")
            if context_window_tokens:
                profiles[limit_model]["context_window_tokens"] = context_window_tokens
            else:
                profiles[limit_model].pop("context_window_tokens", None)
        if not provider_remove and profiles:
            updates["providers_empty"] = "false"
            updates["base_url"] = connection.get("base_url", "")
            updates["api_key"] = connection.get("api_key", "")
        updates["profiles"] = json.dumps(profiles, ensure_ascii=False)
        start = next((i for i, l in enumerate(lines) if l.strip().lower() == "[llm]"), None)
        if start is None:
            lines.insert(0, "[llm]")
            start = 0
        end = len(lines)  # section ends at the next [section] header or EOF
        for i in range(start + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith("[") and s.endswith("]"):
                end = i
                break

        for key, value in updates.items():
            idx = next(
                (i for i in range(start + 1, end)
                 if re.match(rf"{re.escape(key)}\s*=", lines[i])),
                None,
            )
            if idx is not None:
                lines[idx] = f"{key} = {value}"
            else:
                lines.insert(end, f"{key} = {value}")
                end += 1

    locked = ads_auth.edit_config(mutate)

    cfg = load()
    return {
        "base_url": cfg["llm_base_url"],
        "model": cfg["llm_model"],
        "models": cfg["llm_models"],
        "has_key": bool(cfg["llm_api_key"]),
        "api_key_hint": key_hint(cfg["llm_api_key"]),
        "profile_labels": profile_labels(cfg),
        "provider_groups": provider_groups(cfg),
        "model_contexts": model_context.capacities(cfg),
        # 是否在跨进程锁内完成（正常恒为 True；False 说明当时锁被占住，
        # 本次写入退化为无锁，极小概率丢更新 —— 面板/日志据此排查）
        "config_locked": locked,
        **({"deleted_models": deleted_models} if provider_remove else {}),
    }


#: [feedback] 段各键的 ini 键名 -> (出参名, 中文说明)
_FEEDBACK_KEYS = {
    "enabled": "feedback_enabled",
    "auto_open_browser": "feedback_auto_open_browser",
    "include_logs": "feedback_include_logs",
    "max_log_lines": "feedback_max_log_lines",
    "hang_seconds": "feedback_hang_seconds",
    "auto_upload": "feedback_auto_upload",
    "upload_token": "feedback_upload_token",
}


def update_feedback_settings(
    enabled: bool | None = None,
    auto_open_browser: bool | None = None,
    include_logs: bool | None = None,
    max_log_lines: int | None = None,
    hang_seconds: int | None = None,
) -> dict:
    """把崩溃反馈的几个开关写进 config.ini 的 ``[feedback]`` 段。

    只写**显式给出**的键：面板从不发这几个字段，所以缺省必须"不改"，
    否则面板每次保存 LLM 设置都会把反馈开关重置回默认值。

    写入同样走 ads_auth.edit_config —— 与令牌轮换、LLM 设置共用同一把
    跨进程锁，避免整文件写回时互相覆盖。返回写入后的**实际生效值**
    （长键名，与 load() 的 DEFAULTS 同名），server.py 据此刷新内存 CFG。
    """
    # 类型校验先做完再动文件：写一半才发现参数非法，会留下半截配置。
    for name, value in (("enabled", enabled),
                        ("auto_open_browser", auto_open_browser),
                        ("include_logs", include_logs)):
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"反馈开关 {name} 必须为布尔值")
    if max_log_lines is not None and (isinstance(max_log_lines, bool)
                                      or not isinstance(max_log_lines, int)
                                      or max_log_lines < 0):
        raise ValueError("反馈日志行数必须为非负整数")
    if hang_seconds is not None and (isinstance(hang_seconds, bool)
                                     or not isinstance(hang_seconds, int)
                                     or hang_seconds < 0):
        raise ValueError("卡死判定秒数必须为非负整数")

    updates: dict[str, str] = {}
    if enabled is not None:
        updates["enabled"] = "true" if enabled else "false"
    if auto_open_browser is not None:
        updates["auto_open_browser"] = "true" if auto_open_browser else "false"
    if include_logs is not None:
        updates["include_logs"] = "true" if include_logs else "false"
    if max_log_lines is not None:
        # feedback._tail_lines 自己会夹到 1..2000，这里只保证不写负数。
        updates["max_log_lines"] = str(int(max_log_lines))
    if hang_seconds is not None:
        updates["hang_seconds"] = str(int(hang_seconds))

    if updates:
        def mutate(lines: list) -> None:
            start = next((i for i, l in enumerate(lines)
                          if l.strip().lower() == "[feedback]"), None)
            if start is None:
                if lines and lines[-1].strip():
                    lines.append("")  # 段之间留空行，和手写的文件保持一致
                lines.append("[feedback]")
                start = len(lines) - 1
            end = len(lines)  # 段落到下一个 [section] 头或文件末尾
            for i in range(start + 1, len(lines)):
                s = lines[i].strip()
                if s.startswith("[") and s.endswith("]"):
                    end = i
                    break
            for key, value in updates.items():
                idx = next(
                    (i for i in range(start + 1, end)
                     if re.match(rf"{re.escape(key)}\s*=", lines[i])),
                    None,
                )
                if idx is not None:
                    lines[idx] = f"{key} = {value}"
                else:
                    lines.insert(end, f"{key} = {value}")
                    end += 1

        ads_auth.edit_config(mutate)

    cfg = load()
    return {out_key: cfg[out_key] for out_key in _FEEDBACK_KEYS.values()}
