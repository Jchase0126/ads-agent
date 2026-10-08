"""ADS Agent 的**唯一路径来源** —— 后端、面板、工具服务、启动器、鉴权、自检共用。

为什么要有这个模块
------------------
改造前所有数据都躺在"代码所在目录"旁边（``config.ini`` / ``projects.json`` /
``design_jobs/`` / ``logs/``），带来三个装不出去的问题：

1. 程序文件与用户数据混在一起 —— 卸载要么删不掉（留垃圾），要么把用户的
   API Key、会话、设计任务一起删了；升级也得小心翼翼不能覆盖 ``config.ini``；
2. 程序文件常装在只读或有权限要求的目录，运行时写不了日志和配置；
3. 同机上多个位置各放一份代码 → 各读各的 ``config.ini`` → "回环令牌不一致"。

现在分成两个根：

==============================  ==========================================
程序根目录 ``app_root()``       只读的代码。**卸载会删除这里。**
数据根目录 ``data_root()``      配置/令牌/日志/会话/设计任务。**卸载默认保留。**
==============================  ==========================================

默认：``%LOCALAPPDATA%\\Programs\\ADSAgent`` 与 ``%LOCALAPPDATA%\\ADSAgent``
（都不是系统的 Program Files，不需要管理员权限）。

覆盖能力（保留便携/测试用法，优先级从高到低）：

* ``ADS_AGENT_CONFIG``     —— 配置文件路径本身（历史用法，测试与自检都靠它）
* ``ADS_AGENT_DATA_DIR``   —— 数据根目录
* ``ADS_AGENT_APP_DIR``    —— 程序根目录（ADS 里 ``__file__`` 不可靠时用）

本模块必须**自包含**（只用标准库）：ADS 进程会按文件路径加载它，那时
``backend/`` 不在 ``sys.path`` 上。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid

# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

#: 插件发布版本（打进 ZIP 包名、卸载提示、/health 身份里）
PLUGIN_VERSION = "1.1.2"

#: 数据格式版本。仅当 data_root 下的数据结构发生**不兼容**变化时递增，
#: 用于升级时判断要不要做转换。**不是**插件版本。
DATA_VERSION = 1

#: 后端 / ADS 工具服务的线路协议版本。两端握手时校验，避免连到旧版本服务。
PROTOCOL_VERSION = 1

DISPLAY_NAME = "ADS Agent"
#: 加到 ADS 菜单/ Brands 的对外名字；卸载与打包都按它认（改名要同步迁移）
WINDOWS_APP_DIRNAME = "ADSAgent"


# 环境变量（统一在这里列出来，README 与自检都引用这几个名字）
ENV_APP_DIR = "ADS_AGENT_APP_DIR"
ENV_DATA_DIR = "ADS_AGENT_DATA_DIR"
ENV_CONFIG = "ADS_AGENT_CONFIG"
ENV_ADS_DIR = "HPEESOF_DIR"


# ---------------------------------------------------------------------------
# 根解析
# ---------------------------------------------------------------------------

_DEFAULT_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_forced_app_root: str | None = None
_root_lock = threading.Lock()


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'")


def set_app_root(path: str | None) -> None:
    """显式指定程序根目录（ADS 进程里 ``__file__`` 不可靠时用）。

    与旧代码的 ``ads_auth._FORCED_ROOT`` 同义。
    """
    global _forced_app_root
    _forced_app_root = os.path.normpath(path) if path else None


def app_root() -> str:
    """代码所在目录（只读的程序文件）。"""
    if _forced_app_root:
        return _forced_app_root
    override = _clean(os.environ.get(ENV_APP_DIR))
    if override:
        return os.path.normpath(override)
    return os.path.normpath(_DEFAULT_APP_ROOT)


def _local_app_data() -> str:
    if os.name == "nt":
        lad = _clean(os.environ.get("LOCALAPPDATA"))
        if lad:
            return lad
        home = os.path.expanduser("~")
        return os.path.join(home, "AppData", "Local")
    # 非 Windows（单元自测环境）：退回用户目录下的等价位置
    return os.path.join(os.path.expanduser("~"), ".local", "share")


def installed_app_root() -> str:
    """安装布局下的默认程序根目录（不是系统的 Program Files，无需管理员）。

    兼容 1.0.x 的共享根目录；多版本安装（1.1.0 起）请用
    :func:`installed_app_root_for`，按 ADS 年份隔离程序文件。
    """
    if os.name == "nt":
        return os.path.join(_local_app_data(), "Programs", WINDOWS_APP_DIRNAME)
    return os.path.join(_local_app_data(), "ads-agent")


def installed_app_root_for(ads_year) -> str:
    """某个 ADS 版本专用的程序根目录（``.../Programs/ADSAgent/ADS2027``）。

    为什么按版本隔离：不同 ADS 版本可以各装一份插件程序（各自注册、各自
    升级/卸载，互不覆盖）；用户数据（会话/配置/设计任务）仍然**共享**同一
    个数据根目录 —— 升级不丢数据的前提。``ads_year`` 为空时退回共享根
    （兼容旧布局与旧测试）。
    """
    base = installed_app_root()
    year = str(ads_year or "").strip()
    if not year:
        return base
    return os.path.join(base, f"ADS{year}")


def default_data_root() -> str:
    """默认数据根目录 ``%LOCALAPPDATA%\\ADSAgent``。"""
    return os.path.join(_local_app_data(), WINDOWS_APP_DIRNAME)


def data_root(create: bool = False) -> str:
    """用户数据根目录。

    ``ADS_AGENT_DATA_DIR`` 优先（测试、便携模式），否则用
    :func:`default_data_root`。

    ``create=True`` 时顺手建好全部子目录（写文件前调用）。
    """
    root = _clean(os.environ.get(ENV_DATA_DIR)) or default_data_root()
    root = os.path.normpath(root)
    if create:
        ensure_data_dirs(root)
    return root


# ---------------------------------------------------------------------------
# 具体位置
# ---------------------------------------------------------------------------

def logs_dir() -> str:
    return os.path.join(data_root(), "logs")


def log_path(name: str) -> str:
    return os.path.join(logs_dir(), f"{name}.log")


def design_jobs_dir() -> str:
    return os.path.join(data_root(), "design_jobs")


def runtime_dir() -> str:
    """进程实例登记目录（pid / 端口 / 身份），退出时清理自己的那一份。"""
    return os.path.join(data_root(), "runtime")


def sessions_path() -> str:
    return os.path.join(data_root(), "projects.json")


def sessions_backup_path() -> str:
    return sessions_path() + ".bak"


def install_state_path() -> str:
    return os.path.join(data_root(), "install_state.json")


def instance_path(kind: str, key: str = "") -> str:
    """实例登记文件路径。

    ``kind`` = ``backend`` / ``toolserver``；``key`` 用来区分同一台机器上
    同一类服务的多个实例（这里用端口号），这样"两个后端占了不同端口"这种
    多开情况能被登记下来，而不是互相覆盖掉对方的登记。
    """
    safe_kind = re.sub(r"[^A-Za-z0-9_-]", "_", str(kind))
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", str(key))
    name = f"{safe_kind}_{safe_key}" if safe_key else safe_kind
    return os.path.join(runtime_dir(), f"{name}.json")


def default_config_template() -> str:
    return os.path.join(app_root(), "config.example.ini")


def ensure_data_dirs(root: str | None = None) -> dict:
    """建好数据目录下的全部子目录。返回 {名称: 路径}，便于启动日志打印。"""
    base = root or data_root()
    paths = {
        "data": base,
        "logs": os.path.join(base, "logs"),
        "design_jobs": os.path.join(base, "design_jobs"),
        "runtime": os.path.join(base, "runtime"),
    }
    for key, path in paths.items():
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            if key == "data":
                raise
    return paths


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def config_path() -> str:
    """配置文件路径。优先级：``ADS_AGENT_CONFIG`` > 数据目录 > 旧目录（迁移前）。"""
    override = _clean(os.environ.get(ENV_CONFIG))
    if override:
        return os.path.normpath(override)

    candidate = os.path.join(data_root(), "config.ini")
    if os.path.exists(candidate):
        return candidate

    legacy = legacy_root_config_path()
    if legacy and os.path.exists(legacy):
        # 还没迁移：继续读旧位置，行为与改造前一致（不打断正在跑的会话）
        return legacy
    return candidate


def legacy_root() -> str:
    """旧布局的数据位置 —— 就是代码目录本身。"""
    return app_root()


def legacy_root_config_path() -> str:
    return os.path.join(legacy_root(), "config.ini")


# ---------------------------------------------------------------------------
# 首启初始化 / 迁移
# ---------------------------------------------------------------------------

def _valid_ini(path: str) -> bool:
    import configparser

    if not os.path.isfile(path):
        return False
    try:
        parser = configparser.ConfigParser()
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except (OSError, UnicodeDecodeError, configparser.Error):
        return False
    return bool(parser.sections())


def _valid_projects(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(data, dict) and bool(data.get("projects"))


def _valid_job(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(data, dict) and bool(data.get("job_id"))


def init_first_run(force_template: bool = False) -> dict:
    """保证数据目录与配置文件存在。幂等，可在任意进程里安全调用。

    行为：
      * 数据根目录不存在 → 建出来（含 logs / design_jobs / runtime）；
      * 已有 config.ini 且有效 → **原样保留**（升级路径，API 设置不动）；
      * 否则从 ``config.example.ini`` 复制一份**干净模板**（不含任何真实密钥）；
      * 连模板都没有 → 写出最小骨架。

    返回 ``{"created", "config", "source"}``。
    """
    base = ensure_data_dirs()
    target = os.path.join(base["data"], "config.ini")

    if os.path.exists(target) and not force_template:
        return {"created": False, "config": target, "source": "existing"}

    template = default_config_template()
    body = None
    if os.path.isfile(template):
        try:
            with open(template, encoding="utf-8") as f:
                body = f.read()
        except OSError:
            body = None
    if body is None:
        body = _MINIMAL_CONFIG

    _write_if_absent(target, body)
    return {
        "created": True,
        "config": target,
        "source": "template" if os.path.isfile(template) else "minimal",
    }


def migrate_from_legacy(report=None) -> dict:
    """把旧布局的用户数据搬到数据根目录。**非破坏性：源文件一律不动。**

    每条数据只在目标位置**没有有效数据**时才写入 —— 已有的有效配置、
    会话、设计任务永远不会被旧的、可能过期的内容覆盖。

    ``report`` 是可变 list，用于收集人话日志（安装器与自检会打印它）。
    """
    base = ensure_data_dirs()
    src_root = legacy_root()
    dst_root = base["data"]
    out = {
        "skipped": os.path.normcase(os.path.normpath(src_root))
        == os.path.normcase(os.path.normpath(dst_root)),
        "copied": [],
        "kept_existing": [],
        "failed": [],
    }

    def emit(msg: str) -> None:
        if report is not None:
            report.append(msg)

    if out["skipped"]:
        emit("旧目录与数据目录相同，无需迁移")
        return out

    plan = [
        ("config", "config.ini", "config.ini", _valid_ini),
        ("projects", "projects.json", "projects.json", _valid_projects),
        ("projects.bak", "projects.json.bak", "projects.json.bak", _valid_projects),
    ]

    for kind, src_name, dst_name, valid in plan:
        src = os.path.join(src_root, src_name)
        dst = os.path.join(dst_root, dst_name)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst) and valid(dst):
            out["kept_existing"].append(dst_name)
            emit(f"{dst_name}：数据目录已有有效内容，保留不覆盖")
            continue
        try:
            # 目标存在但内容无效（坏 json / 空会话 / 写了一半出错）不算"已有数据"，
            # 这时必须替换 —— 否则用户永远卡在这个坏文件上，且看不出为什么。
            _write_if_absent(dst, _read_text(src),
                             replace_invalid=True, valid=valid)
            out["copied"].append(dst_name)
            emit(f"{dst_name}：已从旧目录迁移")
        except OSError as e:
            out["failed"].append({"name": dst_name, "error": f"{type(e).__name__}: {e}"})
            emit(f"{dst_name}：迁移失败（原文件保持不变）—— {type(e).__name__}: {e}")

    # 设计任务：逐个文件，已存在的同名有效任务不覆盖
    src_jobs = os.path.join(src_root, "design_jobs")
    if os.path.isdir(src_jobs):
        os.makedirs(base["design_jobs"], exist_ok=True)
        for name in sorted(os.listdir(src_jobs)):
            if not name.endswith(".json"):
                continue
            src = os.path.join(src_jobs, name)
            dst = os.path.join(base["design_jobs"], name)
            if os.path.exists(dst) and _valid_job(dst):
                out["kept_existing"].append("design_jobs/" + name)
                continue
            try:
                _write_if_absent(dst, _read_text(src),
                                 replace_invalid=True, valid=_valid_job)
                out["copied"].append("design_jobs/" + name)
            except OSError as e:
                out["failed"].append({"name": "design_jobs/" + name,
                                      "error": f"{type(e).__name__}: {e}"})
        if out["copied"] or out["kept_existing"]:
            n = len([c for c in out["copied"] if c.startswith("design_jobs/")])
            k = len([c for c in out["kept_existing"] if c.startswith("design_jobs/")])
            emit(f"设计任务：迁移 {n} 个，已存在 {k} 个未覆盖")

    return out


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_if_absent(path: str, body: str, replace_invalid: bool = False,
                     valid=None) -> None:
    """原子写入；**默认绝不覆盖已有内容**。

    ``replace_invalid=True`` 时，若目标已存在但 ``valid(path)`` 判定为无效，
    仍然写入 —— "文件存在"不等于"里面有数据"，坏了一半的 json 若被当有效
    内容保住，用户会永远卡在那个坏文件上，还看不出为什么。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        if not replace_invalid:
            return
        if valid is not None and valid(path):
            return
    tmp = f"{path}.tmp_{os.getpid()}_{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 安装状态（版本 / 安装 ID / 升级）
# ---------------------------------------------------------------------------

def load_install_state() -> dict:
    try:
        with open(install_state_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_install_state(**fields) -> dict:
    """合并写入安装状态（保留未知字段，兼容未来版本写入的内容）。"""
    path = install_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _root_lock:
        state = load_install_state()
        state.update(fields)
        state.setdefault("install_id", new_install_id())
        state["plugin_version"] = PLUGIN_VERSION
        state["data_version"] = DATA_VERSION
        state["updated_at"] = _now()
        tmp = f"{path}.tmp_{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dict(state)


def _norm_install_key(ads_dir: str) -> str:
    return os.path.normcase(os.path.normpath(ads_dir or ""))


def record_ads_install(ads_dir: str, year=None, update: str = "", build: str = "",
                       program_dir: str = "", registered_at: str = "") -> dict:
    """把一次安装登记进 ``install_state.json`` 的 ``ads_installs`` 表。

    键是 ADS 安装目录（normcase），值含版本识别结果（来自 buildInfo.xml）与
    本插件的程序目录 —— 多个 ADS 版本各占一条，**互不覆盖**。1.0.x 的旧
    单值字段 ``ads_dir`` 在首次写入时迁移进表里（原字段保留只读兼容）。
    """
    key = _norm_install_key(ads_dir)
    if not key:
        return load_install_state()
    state = load_install_state()
    installs = state.get("ads_installs")
    if not isinstance(installs, dict):
        installs = {}
        legacy = state.get("ads_dir")
        if legacy:
            installs[_norm_install_key(str(legacy))] = {
                "ads_dir": str(legacy),
                "migrated_from": "ads_dir",
            }
    entry = dict(installs.get(key) or {})
    entry.update({
        "ads_dir": os.path.normpath(ads_dir),
        "year": int(year) if year else entry.get("year"),
        "update": update or entry.get("update") or "",
        "build": build or entry.get("build") or "",
        "program_dir": program_dir or entry.get("program_dir") or "",
        "registered_at": registered_at or _now(),
    })
    installs[key] = entry
    return save_install_state(ads_installs=installs)


def remove_ads_install(ads_dir: str) -> dict:
    """从登记表里移除一个 ADS 版本的记录（卸载用）。"""
    key = _norm_install_key(ads_dir)
    state = load_install_state()
    installs = state.get("ads_installs")
    if isinstance(installs, dict) and key in installs:
        installs.pop(key)
        return save_install_state(ads_installs=installs)
    return state


def ads_installs() -> dict:
    """全部已登记的 ADS 安装（键：normcase 目录）。"""
    state = load_install_state()
    installs = state.get("ads_installs")
    return installs if isinstance(installs, dict) else {}


def touch_install_state(**fields) -> dict:
    """记录本次运行的插件版本等信息。

    只有在「版本变了 / 指定字段变了」时才落盘 —— 每次 ADS 启动都重写一次
    既没有必要，也会让文件 mtime 失去诊断意义。
    """
    state = load_install_state()
    changed = False
    for key, value in fields.items():
        if state.get(key) != value:
            state[key] = value
            changed = True
    if state.get("plugin_version") != PLUGIN_VERSION or \
       state.get("data_version") != DATA_VERSION:
        changed = True
    if not changed:
        return dict(state)
    return save_install_state(**state)


def _now() -> str:
    import datetime

    return datetime.datetime.now().replace(microsecond=0).isoformat()


_state_cache: dict = {}
_state_cache_lock = threading.Lock()


def new_install_id() -> str:
    return uuid.uuid4().hex[:16]


def install_id() -> str:
    """本机这份安装的标识（存在数据根目录里，首次使用后不变）。

    同一份安装重复启动 → 同一个 ID → 视为同一个实例家族，允许接管；
    另一份安装（另一个 data_root）→ 不同 ID → 端口冲突时报"另一个实例"，
    而不是傻乎乎连上去，避免连错实例。
    """
    cached = _state_cache.get("install_id")
    if cached:
        return cached
    state = load_install_state()
    ident = str(state.get("install_id") or "").strip()
    if not ident:
        ident = new_install_id()
        try:
            save_install_state(install_id=ident)
        except OSError:
            pass
    with _state_cache_lock:
        _state_cache["install_id"] = ident
    return ident


def summarize() -> dict:
    """诊断用：一次性把关键路径与身份打印出来（自检与启动日志都用它）。"""
    return {
        "plugin_version": PLUGIN_VERSION,
        "data_version": DATA_VERSION,
        "protocol": PROTOCOL_VERSION,
        "app_root": app_root(),
        "data_root": data_root(),
        "install_id": install_id(),
        "config": config_path(),
        "logs_dir": logs_dir(),
        "design_jobs": design_jobs_dir(),
        "runtime": runtime_dir(),
        "sessions": sessions_path(),
        "install_state": install_state_path(),
        "install_state_data": load_install_state(),
    }


_MINIMAL_CONFIG = """; ADS Agent 配置（自动生成的最小骨架）
; 缺少 config.example.ini 时兜底；正常分发包会带模板。

[llm]
base_url = https://open.bigmodel.cn/api/paas/v4
model = glm-4.6
api_key =

[backend]
host = 127.0.0.1
port = 8760

[ads]
host = 127.0.0.1
port = 8761
token =

[agent]
max_tool_steps = 30
sim_timeout = 900
sim_off_main_thread = true

[ui]
auto_open = true
auto_open_delay_ms = 3000
"""


# ---------------------------------------------------------------------------
# 给"按文件路径加载"用的引导：ADS 进程里 backend/ 不在 sys.path 上
# ---------------------------------------------------------------------------

_MODULE_NAME = "ads_agent_shared_paths"


def shared(caller_file: str | None = None):
    """返回同一个 paths 模块实例（供 ads_auth / launcher 在任意上下文中使用）。

    优先正常 import；失败（``backend/`` 不在 sys.path）则按 ``caller_file``
    同目录的 ``paths.py`` 加载，并登记进 ``sys.modules`` —— 保证同进程内
    只有一份实例，不会出现"两处路径解析结果不一致"。
    """
    mod = sys.modules.get(_MODULE_NAME)
    if mod is not None:
        return mod

    try:
        import paths as _plain  # type: ignore

        if all(hasattr(_plain, a) for a in ("data_root", "config_path", "PLUGIN_VERSION")):
            sys.modules.setdefault(_MODULE_NAME, _plain)
            return _plain
    except Exception:
        pass

    import importlib.util

    base = os.path.dirname(os.path.abspath(caller_file or __file__))
    target = os.path.join(base, "paths.py")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载共享路径模块: {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module
