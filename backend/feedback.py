"""崩溃与卡死反馈：本机脱敏取证 + 浏览器预填 GitHub issue。

设计口径（2026-11-03 与用户逐条确认）
------------------------------------
* **通道是「浏览器预填 + 一键复制」，没有任何网络上传代码**。
  本模块只在本机生成报告文件（JSON + Markdown）与剪贴板文本，再由用户
  在 GitHub 页面点 Submit。整个链路不发凭据、不连网、不需要令牌 ——
  因此既不可能泄露 API key，也不会因为公司内网封锁而出故障。
* **隐私边界**：只收集版本 / 环境 / 异常栈 / 脱敏日志 / 工具名。
  用户的设计名、元件名、工程路径、聊天内容、API key 一律不出本机
  （见 :func:`sanitize` 的逐条规则）。
* **触发**：崩溃（进程内未捕获异常）与卡死（心跳超时）都会生成报告。
  ``[feedback] auto_open_browser=true`` 时自动打开预填页（无人值守模式），
  默认 false 则弹窗由用户决定 —— 即「弹窗询问」与「自动上报」同时具备。
* **诊断本身绝不能二次抛异常**：所有公开函数吞掉异常并返回安全值。
  排查工具自己崩掉会把现场弄丢，这比不排查更糟。

为什么放在 backend 而不是 addon
-------------------------------
ADS 进程内的面板不能随便往 ``sys.path`` 塞 ``backend/``（见
``addon/ads_agent/pathbridge.py`` 的说明）。所以取证与脱敏都在后端做，
面板只经 HTTP 索取「生成好的报告文本」，再用 QDesktopServices 打开浏览器。

对外契约
--------
``build_report(kind, error_text, context) -> dict`` 生成报告；
``render_markdown(report) -> str`` 渲染给用户看 / 贴进 issue 的文本；
``issue_url(report) -> str`` 生成 GitHub 预填链接（超长自动降级）；
``record(kind, error_text, context) -> dict | None`` 记录一次崩溃/卡死；
``write_heartbeat(name, payload)`` / ``check_stale(name, seconds)`` 卡死看门狗。
"""

import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
import traceback

import adslog
import paths

# ---------------------------------------------------------------------------
# 目标仓库与用户可见文案
# ---------------------------------------------------------------------------
#: 反馈落在哪个仓库（用户自己维护的上游）。写死是刻意的：报告不该能把
#: 用户带去任意地址（避免被改配置当成钓鱼跳板）。
FEEDBACK_REPO = "Jchase0126/ads-agent"

#: GitHub「新建 issue」入口。预填走 query 参数，不需要任何令牌。
ISSUE_URL_BASE = "https://github.com/%s/issues/new" % FEEDBACK_REPO

#: 浏览器预填 URL 的安全上限。GitHub 对超长 URL 直接 414，且不同浏览器/
#: 代理的实际上限不一致，这里留足余量（约 7KB）。
URL_MAX_CHARS = 7000

#: 隐私声明（会同时出现在设置界面与每份报告里 —— 用户要能一眼核对）。
PRIVACY_NOTICE = (
    "反馈报告只包含：插件与 ADS 版本、操作系统与 Python 版本、异常堆栈、"
    "已脱敏的运行日志、出错时正在执行的工具名。\n"
    "不包含：API 密钥与令牌、你的工程/设计/元件名称、本机文件路径、"
    "与助手的对话内容、任何网络上传（报告只存在本机，由你确认后提交）。"
)

#: 报告里逐条列出"已屏蔽什么"，让用户能自己核对（比空口承诺可信）。
SANITIZE_RULES = (
    "令牌/密钥（Bearer 头、api_key=、token=、password= 等）→ ***",
    "本机绝对路径（C:\\Users\\…、/home/…、\\\\服务器\\共享）→ <本机路径>",
    "超长随机串（疑似密钥/会话 ID，≥40 字符）→ <已屏蔽的长串>",
)

_log = adslog.get("ads_agent.feedback")
_RECORD_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
# 绝对路径：Windows 盘符（含 \\?\ 长路径前缀）、UNC、POSIX。整体掩掉，
# 只在结尾像是"我们自己的源码/配置文件名"时保留 basename —— 例如
# .../addon/ads_agent/ads_ops.py 里的 ads_ops.py 对定位问题有用，
# 而它前面的用户名/目录名对任何人都没用。
_RE_WIN_PATH = re.compile(r"(?:\\\\\?\\|[A-Za-z]:\\|\\\\)[^\s\"'<>|*?\r\n]*")
_RE_POSIX_HOME = re.compile(r"(?:/home|/Users|/root)/[^\s\"'<>|*?\r\n]*")
_RE_POSIX_TMP = re.compile(r"/(?:tmp|var/folders)/[^\s\"'<>|*?\r\n]*")

#: 值得保留的 basename 后缀（插件自己的文件名，不含隐私）。
_KEEP_EXT = (".py", ".ini", ".json", ".log", ".txt", ".ds", ".zip", ".md")

#: 超长随机串：密钥、会话 ID、哈希。保留它没有诊断价值，却可能泄密。
_RE_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _mask_path(match):
    text = match.group(0)
    base = re.split(r"[\\/]", text.rstrip("\\/").strip())[-1]
    if base.lower().endswith(_KEEP_EXT) and len(base) <= 64:
        return "<本机路径>\\" + base
    return "<本机路径>"


def redact_paths(text: str) -> str:
    """把本机绝对路径换成 ``<本机路径>``（保留末尾源码文件名）。"""
    out = _RE_WIN_PATH.sub(_mask_path, text)
    out = _RE_POSIX_HOME.sub(_mask_path, out)
    out = _RE_POSIX_TMP.sub(_mask_path, out)
    # 数据根 / 程序根被写在配置里、日志里，逐个按字面替换（比正则可靠）
    for root in _known_roots():
        if root and len(root) > 3:
            out = out.replace(root, "<本机路径>")
            out = out.replace(root.replace("\\", "/"), "<本机路径>")
    return out


def _known_roots() -> tuple:
    """本包里出现过的、必然含用户名的目录。取不到就算了。"""
    found = []
    for fn in (getattr(paths, "data_root", None),
               getattr(paths, "app_root", None),
               getattr(paths, "installed_app_root", None),
               getattr(paths, "logs_dir", None)):
        if fn is None:
            continue
        try:
            found.append(str(fn()))
        except Exception:  # noqa: BLE001 — 取不到路径不影响脱敏主体
            continue
    return tuple(found)


def sanitize(text, extra_names=()) -> str:
    """统一脱敏入口：令牌 → 路径 → 超长串 → 调用方点名要屏蔽的字符串。

    ``extra_names`` 用于调用方给出**明确的**敏感词（例如当前设计名、
    Workspace 名）。我们无法通用地识别"哪个词是设计名"，但调用方知道，
    所以留这个口子 —— 而不是靠猜测式的启发式规则误伤正常日志。
    """
    try:
        s = adslog.redact(text)          # 令牌/key=value（复用既有实现）
        s = redact_paths(s)
        s = _RE_LONG_TOKEN.sub("<已屏蔽的长串>", s)
        for name in extra_names or ():
            name = str(name or "").strip()
            if len(name) >= 3:
                s = s.replace(name, "<已屏蔽>")
        return s
    except Exception:  # noqa: BLE001 — 脱敏失败就返回一句占位，绝不回吐原文
        return "(脱敏失败，本段内容已丢弃)"


# ---------------------------------------------------------------------------
# 配置（[feedback] 段）
# ---------------------------------------------------------------------------
DEFAULTS = {
    # 总开关：false = 完全不生成报告、不写心跳、不弹窗
    "enabled": True,
    # true = 崩溃/卡死后直接打开浏览器预填页（无人值守）；false = 先弹窗问
    "auto_open_browser": False,
    # 报告里是否附最近日志（关掉只留异常栈）
    "include_logs": True,
    # 附多少行日志
    "max_log_lines": 120,
    # 卡死判定：心跳超过这个秒数没更新即判定卡死（0 = 不检测）
    "hang_seconds": 180,
}

_TRUE = ("1", "true", "yes", "on")

#: 一次性读到的配置缓存（后端进程内有效）。面板改完配置会重启后端或不影响
#: 下一次 record() —— 每次 record 都重新读，避免"改了开关不生效"。
_CFG_CACHE = {"at": 0.0, "value": None}
_CFG_TTL = 2.0


def settings(cfg=None) -> dict:
    """取 ``[feedback]`` 段配置；读不到就用默认值（诊断功能不该因配置损坏失效）。"""
    base = dict(DEFAULTS)
    if cfg is not None:
        base.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
        return base
    now = time.monotonic()
    cached = _CFG_CACHE["value"]
    if cached is not None and now - _CFG_CACHE["at"] < _CFG_TTL:
        return dict(cached)
    try:
        import config as config_mod

        loaded = config_mod.load()
        for key in DEFAULTS:
            if key in loaded:
                base[key] = loaded[key]
    except Exception:  # noqa: BLE001 — 配置读失败就用默认值
        pass
    _CFG_CACHE["value"] = dict(base)
    _CFG_CACHE["at"] = now
    return base


# ---------------------------------------------------------------------------
# 环境信息（只收不识别个人身份的部分）
# ---------------------------------------------------------------------------
def env_info() -> dict:
    """环境快照。刻意**不**采集主机名与用户名。"""
    info = {
        "plugin_version": getattr(paths, "PLUGIN_VERSION", ""),
        "data_version": getattr(paths, "DATA_VERSION", ""),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "python_impl": platform.python_implementation(),
        "is_64bit": sys.maxsize > 2 ** 32,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    try:
        info["install_id"] = str(paths.load_install_state().get("install_id") or "")[:12]
    except Exception:  # noqa: BLE001
        info["install_id"] = ""
    try:
        info["ads_year"] = str(paths.load_install_state().get("ads_year") or "")
    except Exception:  # noqa: BLE001
        pass
    return info


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
_KIND_LABELS = {
    "crash": "进程崩溃",
    "hang": "卡死/无响应",
    "error": "工具报错",
}

_SEVERITY = {"crash": "崩溃", "hang": "卡死", "error": "报错"}


#: 形如 ``KeyError: 'result'`` / ``ModuleNotFoundError: No module named 'x'``
#: 的异常摘要行。用它而不是"最后一行"：日志尾巴、后续多余输出都可能落在
#: 栈之后，取最后一行会把不相干的内容当成崩溃摘要（同理影响指纹稳定性）。
_RE_EXC_LINE = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit|Failure|Timeout)\b.*$")
_RE_FRAME = re.compile(r'^\s*File "', re.MULTILINE)


def _summary_line(text) -> str:
    """挑出最能代表这次失败的摘要：优先异常行，退化为最后一行非空文本。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        if _RE_EXC_LINE.match(line):
            return line
    return lines[-1] if lines else ""


def fingerprint(kind: str, error_text) -> str:
    """同一处崩溃的稳定指纹（用于去重，避免同一个 bug 刷几十份报告）。"""
    head = ""
    try:
        head = _summary_line(error_text)
        # 拼上最后一帧位置：同类异常在不同位置的指纹应当不同。
        # 行号一律换成 N，否则同一处代码挪一行就变成"新 bug"。
        frames = _RE_FRAME.findall(str(error_text or ""))
        if frames:
            last = str(error_text or "").rstrip().splitlines()
            for ln in reversed(last):
                if ln.strip().startswith("File \""):
                    head += "|" + re.sub(r"\d+", "N", ln.strip())
                    break
    except Exception:  # noqa: BLE001
        head = str(error_text or "")[:200]
    return hashlib.sha1(("%s\x00%s" % (kind, head)).encode("utf-8", "replace")).hexdigest()[:16]


def _tail_lines(n: int) -> list:
    try:
        return adslog.tail(max(1, min(int(n), 2000)))
    except Exception:  # noqa: BLE001
        return ["(日志读取失败)"]


def build_report(kind: str, error_text, context=None, log_lines=None,
                 extra_names=()) -> dict:
    """组装一份报告 dict。**纯本机操作**，不写盘、不联网。"""
    cfg = settings()
    kind = str(kind or "error")
    if kind not in _KIND_LABELS:
        kind = "error"
    ctx = dict(context or {})
    if log_lines is None:
        if cfg.get("include_logs", True):
            log_lines = _tail_lines(cfg.get("max_log_lines", 120))
        else:
            log_lines = []

    names = []
    for key in ("design", "workspace", "project", "cell"):
        value = ctx.get(key)
        if value:
            names.append(str(value))
    names.extend(str(x) for x in (extra_names or ()))

    report = {
        "kind": kind,
        "kind_label": _KIND_LABELS[kind],
        "severity": _SEVERITY.get(kind, "报错"),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fingerprint(kind, error_text),
        "error": sanitize(error_text, names).strip()[:12000],
        "context": {k: sanitize(v, names) for k, v in ctx.items()
                    if k not in ("design", "workspace", "project", "cell")},
        "env": env_info(),
        "log_lines": [sanitize(ln, names) for ln in (log_lines or [])],
        "sanitize_rules": list(SANITIZE_RULES),
        "privacy_notice": PRIVACY_NOTICE,
        "repo": FEEDBACK_REPO,
    }
    # 上下文里的敏感键（万一调用方塞进来）直接抹掉，不做"尽量保留"
    for secret_key in ("api_key", "token", "password", "secret", "authorization"):
        for holder in (report["context"], report["env"]):
            if secret_key in holder:
                holder[secret_key] = "***"
    return report


def _md_escape(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def render_markdown(report: dict, include_logs: bool = True) -> str:
    """渲染成 Markdown（既用于剪贴板，也用于 GitHub 预填正文）。"""
    r = report or {}
    env = r.get("env") or {}
    lines = [
        "### 现象",
        "",
        "- 类型：%s" % r.get("kind_label", ""),
        "- 时间：%s" % r.get("created_at", ""),
        "- 插件版本：%s（数据版本 %s）" % (env.get("plugin_version", "?"),
                                          env.get("data_version", "?")),
        "",
        "### 异常/错误",
        "",
        "```text",
        _md_escape(r.get("error", "")).strip() or "(无)",
        "```",
    ]
    ctx = r.get("context") or {}
    if ctx:
        lines += ["", "### 上下文", ""]
        for key, value in ctx.items():
            lines.append("- `%s`: %s" % (key, _md_escape(str(value)).strip()))
    lines += [
        "",
        "### 环境",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
    ]
    for key in ("platform", "python", "python_impl", "is_64bit",
                "plugin_version", "data_version", "ads_year", "install_id"):
        if key in env and env.get(key) not in ("", None):
            lines.append("| %s | %s |" % (key, _md_escape(str(env[key]))))
    logs = r.get("log_lines") or []
    if include_logs and logs:
        lines += ["", "### 最近日志（已脱敏）", "", "```text"]
        lines += [_md_escape(ln) for ln in logs]
        lines += ["```"]
    lines += [
        "",
        "### 已做的脱敏",
        "",
    ]
    lines += ["- %s" % rule for rule in (r.get("sanitize_rules") or [])]
    lines += ["", "> %s" % _md_escape(r.get("privacy_notice", "")).replace("\n", "\n> ")]
    return "\n".join(lines)


def issue_title(report: dict) -> str:
    r = report or {}
    env = r.get("env") or {}
    head = _summary_line(_md_escape(r.get("error", "")))
    head = re.sub(r"\s+", " ", head)[:90]
    return "[%s] %s — v%s" % (r.get("severity", "报错"), head or "(无异常摘要)",
                              env.get("plugin_version", "?"))


def issue_url(report: dict, max_chars: int = URL_MAX_CHARS) -> str:
    """生成 GitHub 预填链接；太长就逐级降级（全量 → 去日志 → 只要摘要）。

    降级而不是截断，是为了保证贴出去的正文永远是一份**完整自洽**的报告，
    不会出现"栈被砍半"的半截内容。
    """
    from urllib.parse import quote

    title = issue_title(report)
    candidates = [
        render_markdown(report, include_logs=True),
        render_markdown(report, include_logs=False),
    ]
    compact = dict(report or {})
    compact["log_lines"] = []
    compact["context"] = {}
    candidates.append(render_markdown(compact, include_logs=False))

    base = ISSUE_URL_BASE + "?labels=bug&template=bug_report.yml&title="
    encoded_title = quote(title, safe="")
    for body in candidates:
        url = base + encoded_title + "&body=" + quote(body, safe="")
        if len(url) <= max_chars:
            return url
    # 极端情况（错误文本本身超长）：只保留异常摘要那一行
    trimmed = dict(compact)
    trimmed["error"] = _summary_line(_md_escape(trimmed.get("error", "")))[:400]
    url = base + encoded_title + "&body=" + quote(
        render_markdown(trimmed, include_logs=False), safe="")
    return url[:max_chars]


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def feedback_dir(create: bool = False) -> str:
    """报告目录：``<数据根>/feedback``（与 logs 同级，便于用户找到）。"""
    root = os.path.join(str(paths.data_root()), "feedback")
    if create:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
    return root


def _atomic_write(path: str, text: str) -> None:
    tmp = "%s.tmp_%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_report(report: dict) -> dict:
    """把报告写成 JSON + Markdown 两个文件，返回路径（失败返回空 dict）。"""
    try:
        root = feedback_dir(create=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stem = "%s_%s_%s" % (stamp, report.get("kind", "error"),
                             report.get("fingerprint", "")[:8])
        json_path = os.path.join(root, stem + ".json")
        md_path = os.path.join(root, stem + ".md")
        _atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=1))
        _atomic_write(md_path, render_markdown(report))
        return {"dir": root, "json": json_path, "md": md_path}
    except Exception as e:  # noqa: BLE001 — 写不下来也要让流程继续
        _log.warning("反馈报告写盘失败: %s", e)
        return {}


def list_reports(limit: int = 20) -> list:
    """最近的报告（按修改时间倒序），供设置页"历史报告"列表用。"""
    try:
        root = feedback_dir()
        if not os.path.isdir(root):
            return []
        items = []
        for name in os.listdir(root):
            if not name.endswith(".json"):
                continue
            full = os.path.join(root, name)
            try:
                items.append({"name": name[:-5] + ".md",
                              "path": os.path.join(root, name[:-5] + ".md"),
                              "mtime": os.path.getmtime(full)})
            except OSError:
                continue
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items[:max(1, int(limit))]
    except Exception:  # noqa: BLE001
        return []


def prune(keep: int = 40) -> int:
    """只留最近 ``keep`` 份报告，返回删除数量（避免无限增长）。"""
    removed = 0
    try:
        items = list_reports(limit=10000)
        for item in items[max(1, int(keep)):]:
            for path in (item["path"], item["path"].replace(".md", ".json")):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    except Exception:  # noqa: BLE001
        pass
    return removed


# ---------------------------------------------------------------------------
# 去重 + 记录
# ---------------------------------------------------------------------------
def _reported_path() -> str:
    return os.path.join(feedback_dir(create=True), "reported.json")


def _load_reported() -> dict:
    try:
        with open(_reported_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(kind: str, error_text, context=None, log_lines=None,
           extra_names=(), dedupe_window_s: float = 600.0) -> dict:
    """生成并保存一份报告。**绝不抛异常**。

    返回 ``{"report", "paths", "duplicate", "fingerprint"}``。
    同一指纹在 ``dedupe_window_s`` 内重复出现时 ``duplicate=True``
    （仍然更新计数，但不重复弹窗 —— 用户不会想被同一个 bug 弹十次）。
    """
    result = {"report": None, "paths": {}, "duplicate": False, "fingerprint": ""}
    try:
        cfg = settings()
        if not cfg.get("enabled", True):
            return result
        report = build_report(kind, error_text, context, log_lines, extra_names)
        fp = report["fingerprint"]
        result["report"] = report
        result["fingerprint"] = fp
        with _RECORD_LOCK:
            reported = _load_reported()
            now = time.time()
            prev = reported.get(fp) or {}
            last = float(prev.get("last") or 0)
            count = int(prev.get("count") or 0) + 1
            reported[fp] = {"last": now, "count": count,
                            "kind": report["kind"],
                            "created_at": report["created_at"]}
            if len(reported) > 300:      # 只留最近的 300 个指纹
                for key in sorted(reported, key=lambda k: reported[k].get("last") or 0)[:100]:
                    reported.pop(key, None)
            try:
                _atomic_write(_reported_path(),
                              json.dumps(reported, ensure_ascii=False, indent=1))
            except OSError:
                pass
            result["duplicate"] = bool(prev) and (now - last) < float(dedupe_window_s)
        if not result["duplicate"]:
            result["paths"] = save_report(report)
            prune(keep=40)
        _log.error("已记录%s报告 %s（指纹 %s）", report["kind_label"],
                   "重复" if result["duplicate"] else "新", fp)
        return result
    except Exception as e:  # noqa: BLE001 — 诊断路径自己出问题也不能炸调用方
        try:
            _log.warning("记录反馈报告失败: %s", e)
        except Exception:  # noqa: BLE001
            pass
        return result


def recent_duplicates(window_s: float = 600.0) -> list:
    """窗口内重复次数 >1 的指纹（用于"这个 bug 又出现了，共 N 次"提示）。"""
    try:
        reported = _load_reported()
        now = time.time()
        return [dict(fingerprint=fp, **info) for fp, info in reported.items()
                if info.get("count", 0) > 1 and now - float(info.get("last") or 0) < window_s]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 卡死看门狗（跨进程心跳文件）
# ---------------------------------------------------------------------------
#: 为什么用心跳文件而不是进程内计时：真正要发现的是"ADS 主线程卡住"，
#: 那时烧在 ADS 进程里的线程自己也不会跑。心跳文件由**另一个进程**读，
#: 才能发现"对面的进程已经不响应了"。


def _heartbeat_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "default"))[:40]
    return os.path.join(str(paths.runtime_dir()), "heartbeat_%s.json" % safe)


def write_heartbeat(name: str, payload=None) -> bool:
    """写一次心跳（原子替换）。写不了就返回 False，不抛。"""
    try:
        path = _heartbeat_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "name": name,
            "at": time.time(),
            "pid": os.getpid(),
            "payload": dict(payload or {}),
        }
        _atomic_write(path, json.dumps(data, ensure_ascii=False))
        return True
    except Exception:  # noqa: BLE001
        return False


def read_heartbeat(name: str) -> dict:
    try:
        with open(_heartbeat_path(name), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        data["age"] = max(0.0, time.time() - float(data.get("at") or 0))
        return data
    except (OSError, ValueError):
        return {}


def check_stale(name: str, seconds, alive_grace: float = 30.0) -> dict:
    """心跳是否超时。返回 ``{"stale", "age", "pid", "alive"}``。

    ``alive_grace``：进程刚启动还没写心跳时不该立刻告警 —— 文件缺失且
    从未有过心跳，视为"尚未启动"，不算卡死。
    """
    cfg = settings()
    limit = float(seconds if seconds is not None else cfg.get("hang_seconds", 180) or 0)
    beat = read_heartbeat(name)
    if not beat:
        return {"stale": False, "age": None, "pid": None, "alive": False,
                "limit": limit}
    age = float(beat.get("age") or 0)
    pid = beat.get("pid")
    alive = True
    if pid and pid != os.getpid():
        try:
            alive = _pid_alive(int(pid))
        except Exception:  # noqa: BLE001
            alive = True
    return {"stale": bool(limit > 0 and age > max(limit, alive_grace)),
            "age": age, "pid": pid, "alive": alive, "limit": limit,
            "payload": beat.get("payload") or {}}


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否还活着（Windows 用 OpenProcess，其它用 kill 0）。"""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 进程级异常钩子
# ---------------------------------------------------------------------------
_HOOK_INSTALLED = {"main": False, "thread": False, "exiti": False}


def _format_exc(exc_type, exc_value, exc_tb) -> str:
    try:
        return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:  # noqa: BLE001
        return "%s: %s" % (getattr(exc_type, "__name__", "?"), exc_value)


def install_hooks(context=None) -> bool:
    """装 ``sys.excepthook`` / ``threading.excepthook``，崩溃时自动留报告。

    刻意**不**接管 ``faulthandler``：C 层硬崩溃（访问违例）写不出 Python
    报告，硬塞只会误导。那种情况靠 ``faulthandler`` 的文本单独排查。
    """
    if not settings().get("enabled", True):
        return False
    ctx = dict(context or {})
    try:
        prev_main = sys.excepthook

        def _main_hook(exc_type, exc_value, exc_tb):
            try:
                record("crash", _format_exc(exc_type, exc_value, exc_tb),
                       dict(ctx, hook="sys.excepthook"))
            except Exception:  # noqa: BLE001
                pass
            try:
                prev_main(exc_type, exc_value, exc_tb)
            except Exception:  # noqa: BLE001
                pass

        sys.excepthook = _main_hook
        _HOOK_INSTALLED["main"] = True

        prev_thread = threading.excepthook

        def _thread_hook(args):
            try:
                record("crash",
                       _format_exc(args.exc_type, args.exc_value, args.exc_traceback),
                       dict(ctx, hook="threading.excepthook",
                            thread=getattr(getattr(args, "thread", None), "name", "")))
            except Exception:  # noqa: BLE001
                pass
            try:
                prev_thread(args)
            except Exception:  # noqa: BLE001
                pass

        threading.excepthook = _thread_hook
        _HOOK_INSTALLED["thread"] = True
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("安装崩溃钩子失败: %s", e)
        return False


#: 卡死看门狗的心跳文件名（ADS 面板进程写、后端进程读）。
#: 面板侧不能 import 本模块（它跑在 ADS 解释器里，见 pathbridge 的说明），
#: 所以这个字符串在 addon/ads_agent/feedbackbridge.py 里有一份同样的常量；
#: 改名字必须两边一起改，且 ``tests/test_feedback.py`` 会断言两处一致。
HEARTBEAT_NAME = "ads_panel"


def snapshot(kind: str, error_text, context=None) -> dict:
    """给 HTTP 层用的一步到位接口：生成 → 落盘 → 出报告文本与预填链接。

    与 :func:`record` 的区别是这里**总是**返回可用的 markdown/url，
    即使去重命中（用户在界面上点"生成报告"时，他想看的就是这一次的内容）。
    """
    result = record(kind, error_text, context)
    report = result.get("report")
    if report is None:
        # record() 被开关关掉或自身出错：退化成"不落盘、只渲染"
        report = build_report(kind, error_text, context)
        result["report"] = report
    return {
        "ok": True,
        "report": report,
        "markdown": render_markdown(report),
        "title": issue_title(report),
        "url": issue_url(report),
        "fingerprint": report.get("fingerprint", ""),
        "duplicate": bool(result.get("duplicate")),
        "paths": result.get("paths") or {},
        "dir": feedback_dir(create=True),
        "privacy_notice": PRIVACY_NOTICE,
        "repo": FEEDBACK_REPO,
    }


def hooks_installed() -> dict:
    return dict(_HOOK_INSTALLED)
