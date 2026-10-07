"""在 ADS 进程内加载共享路径模块 backend/paths.py。

与 ``authbridge`` 同一个套路：按**文件路径**加载，而不是 ``import paths`` ——
ADS 插件的 ``sys.path`` 里是 addon 目录，``backend/`` 不在也不应该在里面
（那会把 config / tools / paths 这类通用名带进 ADS 进程，可能遮蔽 ADS 自己的模块）。

关键在于"只有一份实现"：后端、面板、工具服务、启动器读到的必须是同一套路径，
否则会出现"后端写日志到 A 处、面板读 B 处配置"这种查不出来的问题。
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_SHARED = os.path.join(_ROOT, "backend", "paths.py")

_MODULE_NAME = "ads_agent_shared_paths"
_mod = None


def load():
    """返回共享的 paths 模块（进程内单例）。"""
    global _mod
    if _mod is not None:
        return _mod

    mod = sys.modules.get(_MODULE_NAME)
    if mod is None:
        try:
            import paths as _plain  # type: ignore

            if all(hasattr(_plain, a) for a in
                   ("data_root", "config_path", "PLUGIN_VERSION")):
                _mod = _plain
                return _mod
        except Exception:
            pass

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SHARED)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载共享路径模块: {_SHARED}")
        mod = importlib.util.module_from_spec(spec)
        # 先登记再 exec，避免模块内部二次导入造成两份实例
        sys.modules[_MODULE_NAME] = mod
        spec.loader.exec_module(mod)

    _mod = mod
    return _mod


def paths(create: bool = False):
    """取共享 paths 模块；``create=True`` 时顺手建好数据子目录。"""
    mod = load()
    if create:
        mod.ensure_data_dirs()
    return mod


def load_backend_module(filename: str, module_name: str | None = None):
    """按文件路径加载 ``backend/<filename>``，返回一个共享模块实例。

    与 :func:`load` 同样的理由：**不**把 ``backend/`` 塞进 ``sys.path``。
    已经能被正常 import 时优先走正常路径（本机测试里两者必须是同一对象，
    否则"两个 instance 模块各自维护一份状态"会让多开检测直接失效）。
    """
    key = f"ads_agent_shared_{module_name or os.path.splitext(filename)[0]}"
    mod = sys.modules.get(key)
    if mod is not None:
        return mod

    stem = os.path.splitext(filename)[0]
    try:
        import importlib

        mod = importlib.import_module(stem)
        if all(hasattr(mod, a) for a in (module_name or stem,)):
            sys.modules[key] = mod
            return mod
    except Exception:
        pass

    target = os.path.join(_ROOT, "backend", filename)
    spec = importlib.util.spec_from_file_location(key, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载共享模块: {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def app_root() -> str:
    return load().app_root()


def data_root() -> str:
    return load().data_root()
