"""Shared configuration loader for the ADS Agent backend and install tools.

Reads config.ini at the project root; environment variables override:
  ADS_AGENT_API_KEY      -> [llm] api_key
  ADS_AGENT_BASE_URL     -> [llm] base_url
  ADS_AGENT_MODEL        -> [llm] model
  ADS_AGENT_CONFIG       -> 配置文件路径（默认 <项目根>/config.ini，测试/便携安装用）

回环令牌不在这里定义默认值：它由 backend/ads_auth.py 统一负责生成与读取，
后端和 ADS 端工具服务必须拿到同一个值（见该模块的说明）。
"""

from __future__ import annotations

import configparser
import os
import re

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
    # ---- 跨版本兼容（config.ini [compat]）----
    # 未知版本放行（默认 False = 保守拒绝写操作）
    "compat_allow_unknown_version": False,
    # ADS 2024–2026 为实验性适配：写/建图/仿真需按年份显式开启。
    # **开启不代表验证通过**，界面仍会显示"未实机验证"。
    "compat_experimental_2024": False,
    "compat_experimental_2025": False,
    "compat_experimental_2026": False,
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    config_path = ads_auth.config_path()
    parser = configparser.ConfigParser()
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
    get_bool("compat", "allow_unknown_version", "compat_allow_unknown_version")
    get_bool("compat", "experimental_2024", "compat_experimental_2024")
    get_bool("compat", "experimental_2025", "compat_experimental_2025")
    get_bool("compat", "experimental_2026", "compat_experimental_2026")

    cfg["llm_api_key"] = os.environ.get("ADS_AGENT_API_KEY", cfg["llm_api_key"])
    cfg["llm_base_url"] = os.environ.get("ADS_AGENT_BASE_URL", cfg["llm_base_url"])
    cfg["llm_model"] = os.environ.get("ADS_AGENT_MODEL", cfg["llm_model"])

    # 令牌的唯一来源：缺失或仍是公开默认值时，这里会生成并写回 config.ini。
    # 不用 get("ads","token",...) 读原始值——那样会把公开默认值当成有效配置。
    cfg["ads_token"] = ads_auth.ensure_token()

    models = []
    if parser.has_option("llm", "models"):
        raw = parser.get("llm", "models").strip()
        models = [m.strip() for m in raw.split(",") if m.strip()]
    if cfg["llm_model"] not in models:
        models.insert(0, cfg["llm_model"])
    cfg["llm_models"] = models
    return cfg


def ads_base_url(cfg: dict) -> str:
    return f"http://{cfg['ads_host']}:{cfg['ads_port']}"


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
) -> dict:
    """Persist [llm] settings into config.ini, preserving comments & other keys.

    Only provided values are written. Returns the effective settings after
    the update; the in-memory CFG in server.py must be refreshed separately.

    写入走 ads_auth.edit_config —— 与令牌生成/轮换**共用同一个跨进程锁**，
    否则"保存 LLM 设置"和"轮换令牌"会各自按旧快照整文件写回，
    把对方刚写的字段覆盖掉（典型：刚轮换的令牌被还原成公开默认值）。
    """
    updates: dict[str, str] = {}
    if base_url is not None and base_url.strip():
        updates["base_url"] = base_url.strip()
    if model is not None and model.strip():
        updates["model"] = model.strip()
    if models:
        updates["models"] = ", ".join(models)
    if api_key is not None:  # empty string clears the key on purpose
        updates["api_key"] = api_key.strip()

    def mutate(lines: list) -> None:
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
        # 是否在跨进程锁内完成（正常恒为 True；False 说明当时锁被占住，
        # 本次写入退化为无锁，极小概率丢更新 —— 面板/日志据此排查）
        "config_locked": locked,
    }
