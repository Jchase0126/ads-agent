"""在 ADS 进程内加载共享令牌模块 backend/ads_auth.py。

为什么不能直接 ``import ads_auth``：ADS 插件的 sys.path 里是 addon 目录，
``backend/`` 并不在（也**不应该**把它加进去 —— 那会把 backend/config.py、
backend/tools.py 这类通用名带进 ADS 进程，可能遮蔽 ADS 自己的模块）。

所以这里按**文件路径**加载同一个实现。关键点是"只有一份实现"：
令牌的生成、轮换、校验规则全在 backend/ads_auth.py 里，
后端和 ADS 端不会各自维护默认值而生成出两个不同的令牌。
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_SHARED = os.path.join(_ROOT, "backend", "ads_auth.py")
_MODULE_NAME = "ads_agent_shared_auth"

_mod = None


def auth():
    """返回共享的 ads_auth 模块（进程内单例）。"""
    global _mod
    if _mod is not None:
        return _mod

    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SHARED)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载共享令牌模块: {_SHARED}")
    module = importlib.util.module_from_spec(spec)
    # 先注册再 exec，避免模块内部出现重复导入
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    # ADS 进程里 __file__ 未必可靠，显式把程序根告诉它（这个根同时也是
    # 数据迁移的源目录），并同步给共享路径模块，保证两端解析出的
    # config.ini / 日志目录一致。
    module._FORCED_ROOT = _ROOT
    try:
        module._paths().set_app_root(_ROOT)
    except Exception:
        pass
    _mod = module
    return module


def token() -> str:
    """当前回环令牌（必要时生成并写回 config.ini）。"""
    return auth().ensure_token()


def check(provided: str) -> bool:
    """校验请求携带的令牌。"""
    return auth().check_token(provided)


def app_root() -> str:
    """代码所在目录（只读的程序文件）。"""
    return auth().app_root()


def data_root() -> str:
    """用户数据根目录（日志、会话、设计任务都在它下面）。"""
    return auth().data_root()


def header_name() -> str:
    return auth().TOKEN_HEADER


def mask(value: str) -> str:
    """脱敏展示（日志/界面用），绝不打印明文。"""
    return auth().mask(value)
