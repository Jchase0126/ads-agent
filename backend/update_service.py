"""Read-only release lookup and verified, isolated manual update preparation.

Only the GitHub Release asset digest is trusted; no caller supplied URL, digest,
script or installation path is used. Nothing is installed by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

import paths

REPO = "Jchase0126/ads-agent"
API_URL = "https://api.github.com/repos/" + REPO + "/releases/latest"
MAX_JSON = 1024 * 1024
MAX_ZIP = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILES = 10000
MAX_CENTRAL = 16 * 1024 * 1024
DEADLINE_S = 600
TIMEOUT = 20
# prepare() 单飞：同一时刻只允许一个准备流程；成功目录只保留最新一份。
_PREPARE_LOCK = threading.Lock()
_PREPARE_LAST_DIR: list = []
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_DIGEST = re.compile(r"sha256:([a-fA-F0-9]{64})\Z")
_ALLOWED_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class UpdateError(ValueError):
    pass


def _version(value):
    match = _VERSION.fullmatch(value if isinstance(value, str) else "")
    if not match:
        raise UpdateError("版本标签不是稳定的三段数字版本")
    return tuple(int(x) for x in match.groups())


def _url(url, hosts):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in hosts or
                parsed.username is not None or parsed.password is not None or
                parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError()
    except (ValueError, TypeError):
        raise UpdateError("拒绝不可信的下载地址或重定向") from None
    return parsed


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > 4:
            raise UpdateError("重定向次数过多")
        _url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, hosts, accept):
    _url(url, hosts)
    opener = urllib.request.build_opener(_Redirect(hosts))
    request = urllib.request.Request(url, headers={"User-Agent": "ADS-Agent-Updater", "Accept": accept})
    return opener.open(request, timeout=TIMEOUT)


def _read_limited(response, limit):
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > limit:
                raise UpdateError("响应超过大小限制")
        except ValueError as exc:
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError("响应长度无效") from exc
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("响应超过大小限制")
    return data


def _release():
    with _open(API_URL, {"api.github.com"}, "application/vnd.github+json") as response:
        payload = json.loads(_read_limited(response, MAX_JSON).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise UpdateError("Release 不是已发布的稳定版本")
    tag = payload.get("tag_name")
    version = _version(tag)
    canonical = ".".join(str(n) for n in version)
    if version <= _version(paths.PLUGIN_VERSION):
        return {"version": canonical, "version_tuple": version}
    name = "ADSAgent-" + canonical + ".zip"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("Release 缺少资源列表")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        raise UpdateError("Release 必须包含唯一指定的安装包 " + name)
    asset = matches[0]
    digest = _DIGEST.fullmatch(asset.get("digest") if isinstance(asset.get("digest"), str) else "")
    if not digest:
        raise UpdateError("Release 安装包缺少可信的 SHA-256 digest")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_ZIP or asset.get("state") != "uploaded":
        raise UpdateError("Release 安装包大小或状态无效")
    url = asset.get("browser_download_url")
    parsed = _url(url, {"github.com"})
    expected = "/" + REPO + "/releases/download/" + urllib.parse.quote(tag, safe="") + "/" + name
    if parsed.path != expected or parsed.query:
        raise UpdateError("Release 安装包地址与指定仓库、版本不一致")
    return {"version": canonical, "version_tuple": version, "name": name,
            "digest": digest.group(1).lower(), "size": size, "url": url}


def _result(status, message, latest="", **extra):
    return {"status": status, "current_version": paths.PLUGIN_VERSION,
            "latest_version": latest, "message": message, **extra}


def _check():
    current = _version(paths.PLUGIN_VERSION)
    release = _release()
    if release["version_tuple"] <= current:
        return _result("up_to_date", "当前版本不低于最新稳定版本，不会回退。", release["version"]), release
    return _result("update_available", "发现新的稳定版本。", release["version"], asset_name=release["name"]), release


def check():
    try:
        return _check()[0]
    except Exception as exc:
        return _result("error", "检查更新失败（网络、Release 或校验信息不可用）：" + str(exc))


def _preflight_central(archive):
    """在 ZipFile 解析中央目录之前，先有界读取 EOCD/ZIP64 头核对项数与目录大小。

    ZipFile 构造即解析整个中央目录；恶意 ZIP 可以在通过下载大小限制的同时塞进
    海量目录项，让进程先吃内存再报错。这里只读文件尾部固定窗口，超限即拒。
    """
    size = os.path.getsize(archive)
    if size < 22:
        raise UpdateError("ZIP 文件过小，不是有效的安装包")
    with open(archive, "rb") as fh:
        window = min(size, 65536 + 22)
        fh.seek(size - window)
        tail = fh.read(window)
    # ZIP32 EOCD：PK\x05\x06；注释长度不定，从后往前找最后一个。
    pos = tail.rfind(b"PK\x05\x06")
    count = None
    central = None
    if pos >= 0 and pos + 22 <= len(tail):
        count = struct.unpack_from("<H", tail, pos + 10)[0]
        central = struct.unpack_from("<I", tail, pos + 12)[0]
        if count == 0xFFFF or central == 0xFFFFFFFF:
            # ZIP64：定位 PK\x06\x07 -> PK\x06\x06，取真实 64 位字段。
            loc = tail.rfind(b"PK\x06\x07")
            if loc >= 0 and loc + 20 <= len(tail):
                z64 = struct.unpack_from("<Q", tail, loc + 8)[0]
                if z64 + 56 <= size:
                    with open(archive, "rb") as fh:
                        fh.seek(z64)
                        head = fh.read(56)
                    if head[:4] == b"PK\x06\x06":
                        count = struct.unpack_from("<Q", head, 32)[0]
                        central = struct.unpack_from("<Q", head, 40)[0]
    if count is None:
        raise UpdateError("ZIP 缺少有效的目录结构")
    if count == 0 or count > MAX_FILES or central > MAX_CENTRAL:
        raise UpdateError("ZIP 目录项数或目录大小超过限制")


def _extract(archive, destination, version):
    root = "ADSAgent-" + version
    seen = set()
    regular_files = set()
    total = 0
    _preflight_central(archive)
    with zipfile.ZipFile(archive) as zf:
        entries = zf.infolist()
        if not entries or len(entries) > MAX_FILES:
            raise UpdateError("ZIP 文件数量无效")
        for entry in entries:
            # ZipInfo normalizes backslashes and truncates NUL on Windows; inspect
            # the original central-directory name before using its normalized form.
            name = entry.orig_filename
            parts = name.rstrip("/").split("/")
            if (not name or "\\" in name or name.startswith("/") or parts[0] != root or
                    any(not p or p in (".", "..") or p.endswith((".", " ")) or
                        any(ord(c) < 32 or c in ':*?"<>|' for c in p) or
                        re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", p)
                        for p in parts)):
                raise UpdateError("ZIP 包含不安全路径")
            key = "/".join(parts).casefold()
            if key in seen:
                raise UpdateError("ZIP 包含重复路径")
            seen.add(key)
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                raise UpdateError("ZIP 包含符号链接、特殊文件或加密文件")
            if not entry.is_dir() and kind != stat.S_IFDIR:
                regular_files.add(key)
            total += entry.file_size
            if total > MAX_EXPANDED:
                raise UpdateError("ZIP 解压大小超过限制")
        required = {f"{root}/{n}".casefold() for n in (
            "install_addon.py", "install_addon.bat", "release_manifest.py",
            "backend/paths.py", "backend/server.py", "backend/update_service.py",
            "addon/ads_agent/__init__.py", "addon/ads_agent/panel.py",
            "addon/ads_agent/toolserver.py", "addon/ads_agent/ads_ops.py")}
        if not required.issubset(regular_files):
            raise UpdateError("ZIP 缺少安装器或必要文件")
        for entry in entries:
            target = os.path.join(destination, *entry.filename.rstrip("/").split("/"))
            if entry.is_dir():
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(entry) as source, open(target, "xb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
    return os.path.join(destination, root, "install_addon.bat")


def _cleanup_prev_dir():
    """删除上一次成功准备遗留的临时目录（只保留最新一份）。"""
    while _PREPARE_LAST_DIR:
        stale = _PREPARE_LAST_DIR.pop()
        shutil.rmtree(stale, ignore_errors=True)


def prepare():
    # 单飞：串行化整个准备流程，防止并发/反复请求耗尽磁盘与内存。
    if not _PREPARE_LOCK.acquire(blocking=False):
        return _result("error", "已有一次更新准备在进行中，请等待其完成后再试。")
    try:
        return _prepare_locked()
    finally:
        _PREPARE_LOCK.release()


# ---------------------------------------------------------------------------
# 应用更新：把已通过校验的暂存包覆盖到程序目录（面板"一键更新"用）。
# ---------------------------------------------------------------------------
def apply():
    """把 prepare() 暂存的更新包复制进程序目录。

    安全边界：
    - 只使用本模块自己暂存的目录（``_PREPARE_LAST_DIR``），绝不接受客户端路径；
    - 与 prepare 共用一把单飞锁，准备/应用不会并发交叉；
    - 目标目录是开发检出（含 ``.git``）或缺少安装器时拒绝——保护源码工作区；
    - 永不覆盖用户数据文件（config.ini / config.example.ini / user_data）；
    - 覆盖失败时中止并如实报告（不做部分回滚，安装器本身也是就地覆盖语义）。
    """
    if not _PREPARE_LOCK.acquire(blocking=False):
        return _result("error", "已有一次更新准备或安装在进行中，请稍后再试。")
    try:
        return _apply_locked()
    finally:
        _PREPARE_LOCK.release()


def _apply_locked():
    try:
        if not _PREPARE_LAST_DIR:
            return _result("error", "还没有准备好的更新包，请先检查并准备更新。")
        directory = _PREPARE_LAST_DIR[-1]
        if not os.path.isdir(directory):
            _PREPARE_LAST_DIR.clear()
            return _result("error", "准备好的更新包已丢失，请重新准备更新。")
        # 暂存目录里是单根 ADSAgent-<ver>/ 的解包结果
        roots = [n for n in os.listdir(directory)
                 if os.path.isdir(os.path.join(directory, n))]
        if len(roots) != 1:
            return _result("error", "暂存的更新包结构异常，请重新准备更新。")
        src_root = os.path.join(directory, roots[0])
        if not os.path.isfile(os.path.join(src_root, "install_addon.py")):
            return _result("error", "暂存的更新包缺少安装器，已中止应用。")
        target = paths.app_root()
        if os.path.isdir(os.path.join(target, ".git")):
            return _result("error",
                           "当前程序目录是开发检出（含 .git），自动更新不覆盖源码；"
                           "请按「准备更新」给出的指引手动安装。")
        if not os.path.isfile(os.path.join(target, "install_addon.py")):
            return _result("error",
                           "程序目录不完整（缺 install_addon.py），已中止自动更新；"
                           "请重新解压安装包后手动安装。")
        copied = 0
        _SKIP_NAMES = {"config.ini", "config.example.ini", "ads_toolserver.log"}
        for dirpath, _dirnames, filenames in os.walk(src_root):
            rel = os.path.relpath(dirpath, src_root)
            dst_dir = target if rel == "." else os.path.join(target, rel)
            os.makedirs(dst_dir, exist_ok=True)
            for fn in filenames:
                if fn.lower() in _SKIP_NAMES:
                    continue  # 用户配置与运行日志永不覆盖
                dst = os.path.join(dst_dir, fn)
                try:
                    if os.path.exists(dst):
                        os.chmod(dst, stat.S_IWUSR)
                    shutil.copy2(os.path.join(dirpath, fn), dst)
                    copied += 1
                except PermissionError as exc:
                    return _result(
                        "error",
                        f"更新中断：文件被占用（{os.path.relpath(dst, target)}）。"
                        "请关闭所有 ADS 窗口与 ADS Agent 后端后重试。"
                        f"（{type(exc).__name__}）")
        shutil.rmtree(directory, ignore_errors=True)
        _PREPARE_LAST_DIR.clear()
        # 读出新版本号用于回执（复制后的 backend/paths.py）
        new_version = ""
        try:
            with open(os.path.join(target, "backend", "paths.py"),
                      encoding="utf-8") as fh:
                m = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', fh.read())
            new_version = m.group(1) if m else ""
        except Exception:  # noqa: BLE001 — 版本号读不出来不影响成功回执
            pass
        return _result("applied",
                       f"已应用更新（覆盖 {copied} 个文件，版本 {new_version or '未知'}）；"
                       "后端即将自动重启以加载新版本。",
                       applied_version=new_version, files=copied)
    except Exception as exc:  # noqa: BLE001
        return _result("error", "应用更新失败：" + str(exc))


def _prepare_locked():
    directory = None
    deadline = time.monotonic() + DEADLINE_S
    try:
        result, release = _check()  # Always re-fetch metadata; never trust client state.
        if result["status"] != "update_available":
            return result
        _cleanup_prev_dir()
        directory = tempfile.mkdtemp(prefix="adsagent-update-")
        archive = os.path.join(directory, release["name"])
        digest = hashlib.sha256()
        count = 0
        with _open(release["url"], _ALLOWED_DOWNLOAD_HOSTS, "application/octet-stream") as response, open(archive, "xb") as out:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) != release["size"]):
                raise UpdateError("下载响应长度与 Release 不一致")
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("下载超过总时限（持续慢速响应将被终止）")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > release["size"] or count > MAX_ZIP:
                    raise UpdateError("下载超过声明大小")
                digest.update(chunk)
                out.write(chunk)
        if count != release["size"] or digest.hexdigest() != release["digest"]:
            raise UpdateError("安装包大小或 SHA-256 校验失败")
        installer = _extract(archive, directory, release["version"])
        # 成功目录交给 _PREPARE_LAST_DIR 托管：下次 prepare 前清理，失败路径不再删除它。
        _PREPARE_LAST_DIR.append(directory)
        directory = None
        instruction = ("更新尚未安装。请保存工作并关闭所有 ADS 窗口及 ADS Agent 后端，"
                       "然后在资源管理器中双击以下安装器，按提示完成安装：" + installer +
                       "。完成后重新启动 ADS；请勿在 ADS 运行时安装。"
                       "安装器不是事务更新，安装失败可能需重新运行安装器；请先备份重要数据。"
                       "多个 ADS 版本时请按安装器提示指定目标；开发检出不会被覆盖，安装器可能重新注册到部署目录。")
        return _result("ready", "安装包已通过 GitHub Release SHA-256 校验并解压；尚未安装。",
                       release["version"], instruction=instruction, package_path=archive,
                       installer_path=installer)
    except Exception as exc:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        return _result("error", "准备更新失败，未修改安装或用户数据：" + str(exc))
