"""后端结构化文件日志。

为什么不能只靠 stdout 重定向：后端可能被三种方式拉起——ADS 插件
（backend_launcher，会把 stdout 重定向到 logs/backend.log）、start_backend.bat
（输出到控制台窗口，窗口一关就没了）、命令行。排查问题时最常用的恰恰是第二种，
所以日志必须由程序自己写文件，而不是依赖启动方式。

用法::

    import adslog
    adslog.setup()                 # 幂等，配好 RotatingFileHandler
    log = adslog.get(__name__)
    log.info("...")
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# 统一走 backend/paths.py：日志属于**用户数据**，写到 %LOCALAPPDATA%\ADSAgent\logs，
# 而不是代码目录旁边（程序文件可能只读，也不该被运行时产物污染）。
import paths

# 模块级常量：测试会在 import server 之前把它们改写到临时目录（见 tests/test_auth.py）
LOG_DIR = paths.logs_dir()
LOG_FILE = paths.log_path("backend")

MAX_BYTES = 2_000_000   # 单文件 2MB
BACKUP_COUNT = 3        # 保留 backend.log.1~3

_FMT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup(level: int = logging.DEBUG, echo_console: bool = True) -> str:
    """配置 root logger 的文件 + 控制台输出。幂等，返回日志文件路径。"""
    global _configured
    if _configured:
        return LOG_FILE

    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if echo_console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    _configured = True
    return LOG_FILE


def get(name: str = "ads_agent") -> logging.Logger:
    """拿一个 logger；顺手确保已配置（避免漏调 setup 时完全没有日志）。"""
    setup()
    return logging.getLogger(name)


def tail(n: int = 200) -> list:
    """返回日志文件最后 n 行（找不到文件返回提示行）。"""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return ["(日志尚未生成: %s)" % LOG_FILE]
    except OSError as e:
        return [f"(读取日志失败: {e})"]
    tail_lines = lines[-n:]
    return tail_lines if tail_lines else ["(日志为空)"]


def _clip(text: str, limit: int = 800) -> str:
    s = str(text)
    return s if len(s) <= limit else s[:limit] + f" …(共{len(s)}字符)"


# ---------------------------------------------------------------------------
# 敏感信息脱敏：工具脚本、参数、上游错误响应里的凭据不能进日志与诊断导出
# ---------------------------------------------------------------------------
import re as _re

_SECRET_KEYS = ("token", "api_key", "apikey", "authorization", "password",
                "secret", "api-key", "x-api-key")
_RE_BEARER = _re.compile(r"(Bearer\s+)\S+", _re.IGNORECASE)
_RE_SECRET_KV = _re.compile(
    r"\b(" + "|".join(_SECRET_KEYS) + r")\b\s*[:=]\s*[\"']?[^\s\"',;}]+",
    _re.IGNORECASE,
)


def redact(text) -> str:
    """掩掉令牌/密钥类内容（Bearer 头与 key=value 形式），其余原样返回。"""
    s = str(text or "")
    if not s:
        return s
    s = _RE_BEARER.sub(r"\1***", s)
    s = _RE_SECRET_KV.sub(r"\1=***", s)
    return s
