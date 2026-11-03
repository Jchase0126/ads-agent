"""只读探测 3：在 ADS 进程内核实"可分发改造"落地情况。

不写任何注册、不改任何用户数据。只回答四个问题：
  1. 共享路径模块能不能在 ADS 里加载、解析出的目录对不对；
  2. 启动器会不会选中 ADS 自带的解释器；
  3. 官方注册接口（add_user_addon 那一族）在 ADS 里是不是真能用；
  4. 实例身份与工具服务身份是否一致（同一份安装）。
"""

CODE = r'''
import json, os, sys

out = {}

# 在 ADS 进程内，插件目录一般已经在 sys.path 上；万一不在（run_python 的
# 执行环境是自定义 globals，没有 __file__），就从已加载的 toolserver 模块
# 反查插件目录 —— toolserver.py 一定是插件目录里的文件。
try:
    import pathbridge  # noqa: F401
except ImportError:
    _ts = sys.modules.get("toolserver")
    _f = getattr(_ts, "__file__", None) if _ts is not None else None
    if _f:
        _d = os.path.dirname(os.path.abspath(_f))
        if _d not in sys.path:
            sys.path.insert(0, _d)


def guard(label, fn):
    try:
        out[label] = fn()
    except Exception as e:
        out[label] = {"error": f"{type(e).__name__}: {e}"}


# ---- 1. 共享路径模块 ----
def paths_info():
    import pathbridge
    p = pathbridge.load()
    return {
        "plugin_version": p.PLUGIN_VERSION,
        "data_version": p.DATA_VERSION,
        "app_root": p.app_root(),
        "data_root": p.data_root(),
        "config_path": p.config_path(),
        "logs_dir": p.logs_dir(),
        "design_jobs": p.design_jobs_dir(),
        "install_id": p.install_id(),
        # 关键：程序目录和数据目录必须分开
        "roots_are_separate": os.path.normcase(p.app_root()) != os.path.normcase(p.data_root()),
        "data_root_is_localappdata": os.path.normcase(p.data_root()).startswith(
            os.path.normcase(os.environ.get("LOCALAPPDATA", "?"))),
    }


guard("paths", paths_info)

# ---- 2. 解释器选择 ----
def launcher_info():
    import backend_launcher as bl
    import pathbridge

    # backend/adslocate.py 故意**不**在 sys.path 上（避免把 backend/ 塞进 ADS），
    # 必须走 pathbridge 按文件加载 —— 产品代码就是这么用的，探针照抄才对。
    al = pathbridge.load_backend_module("adslocate.py", "adslocate")
    exe, source = bl.pick_python()
    found = al.detect_ads_dirs()
    targets = [i for i in found if i.get("is_target")] or found
    return {
        "chosen": exe,
        "source": source,
        "is_ads_python": al.is_ads_python(exe),
        "current_executable": sys.executable,
        "inside_ads": al.inside_ads(),
        "backend_script": bl.backend_script(),
        "backend_script_exists": os.path.isfile(bl.backend_script()),
        "log_file": bl._log_file(),
        "target_year": al.TARGET_YEAR,
        "detected_ads_dir": targets[0]["dir"] if targets else None,
        "detected_count": len(found),
    }


guard("launcher", launcher_info)

# ---- 3. 官方注册接口（在 ADS 内应当可用）----
def reg_info():
    import registration as reg
    info = reg.describe()
    info["locale_help_lines"] = len(reg.locale_help().splitlines())
    return info


guard("registration", reg_info)

# ---- 4. 实例身份 ----
def ident_info():
    import pathbridge

    inst = pathbridge.load_backend_module("instance.py", "instance")
    ident = inst.identity()
    return {
        "install_id": ident["install_id"],
        "plugin_version": ident["plugin_version"],
        "protocol": ident["protocol"],
        "app_root": ident["app_root"],
        "same_as_paths": ident["install_id"] == out.get("paths", {}).get("install_id"),
        "runtime_dir_exists": os.path.isdir(inst.paths.runtime_dir()),
        "service_toolserver": inst.SERVICE_TOOLSERVER,
        "service_backend": inst.SERVICE_BACKEND,
    }


guard("instance", ident_info)

# ---- 5. 数据目录现状（不写）----
def data_state():
    import pathbridge
    p = pathbridge.load()
    return {
        "data_root_exists": os.path.isdir(p.data_root()),
        "logs_exists": os.path.isdir(p.logs_dir()),
        "design_jobs_exists": os.path.isdir(p.design_jobs_dir()),
        "config_exists": os.path.isfile(os.path.join(p.data_root(), "config.ini")),
        "install_state": p.load_install_state(),
    }


guard("data_state", data_state)

print(json.dumps(out, ensure_ascii=False, indent=1))
'''
