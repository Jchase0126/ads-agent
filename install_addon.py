"""ADS Agent 的安装 / 卸载 / 状态查询（支持 ADS 2024–2027 多版本共存）。

用法::

    python install_addon.py                      # 安装（幂等，可重复执行）
    python install_addon.py --detect             # 只列出探测到的 ADS 候选
    python install_addon.py --status             # 详细注册状态
    python install_addon.py --remove             # 卸载注册（默认保留用户数据）
    python install_addon.py --remove --purge-data  # 连 %LOCALAPPDATA%\\ADSAgent 一起删
    python install_addon.py --ads-dir <ADS目录>  # 显式指定 ADS 安装目录（可重复传，
                                                 # 一个版本一条注册，互不覆盖）
    python install_addon.py --all                # 安装到探测到的全部 ADS 版本
    python install_addon.py --mode inplace       # 就地注册（不改程序文件位置）

## 多版本共存

* 每个 ADS 安装目录各写各的 ``config\\eesof_addons.xml`` 注册（互不影响）；
* 程序文件按版本隔离部署（``...\\Programs\\ADSAgent\\ADS<年份>``），升级/卸载
  只动选定版本；用户数据（配置/会话/设计任务）**共享**同一数据根目录；
* ``install_state.json`` 的 ``ads_installs`` 表按 ADS 目录记录每个版本的
  识别结果（来自 buildInfo.xml）与注册位置 —— 不互相覆盖；
* "可多版本安装" **不等于** "可多实例并行运行"：同一时刻只有一个 ADS 版本
  的实例能持有工具服务端口（跨版本冲突会被明确识别并提示，见 backend/instance.py）。

## 注册是怎么写进去的

ADS 启动时会读 ``<ADS安装目录>\\config\\eesof_addons.xml``，里面每一行
``<Addon Name= FilePath= Enabled= />`` 是一个插件。本插件往里加一行。

**为什么不用官方的 ``add_user_addon``**：那是 ``keysight.ads.de.app`` 里的接口，
底层依赖编译进 ADS 主程序的 ``_pde_app`` 模块 —— 实测在 ADS 进程外调用必然失败
（详见 ``addon/ads_agent/registration.py`` 的注释与 ``tests/probes/Z_user_addon_api.py``
的实机输出）。安装器必须能在"ADS 没运行"的时候工作，所以注册只能是写文件。
官方接口则在**条件成立的地方**照用：ADS 菜单 ▸ Tools ▸ ADS Agent ▸ 注册状态…
（``addon/ads_agent/registration.py``）。

## 写文件的规矩

注册文件里还有 ADS 自己的十几个插件，写坏了用户整个 ADS 都用不了。所以：

1. **结构化解析** —— ``xml.etree`` + 保留注释/处理指令，不用正则替换；
2. **备份** —— 内容真的会变才备份（带时间戳，最多留 5 份）；
3. **原子写** —— 同目录临时文件 → fsync → ``os.replace``；
4. **失败回滚** —— 写完**重新解析**并逐条比对"其它插件是否被写坏"，
   任何一项对不上就还原备份并报错，绝不留下半截文件；
5. **幂等** —— 已注册且路径一致则什么都不做；路径变了只改那一条。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from xml.etree import ElementTree

ADDON_NAME = "ADS Agent"
PLUGIN_ENTRY = os.path.join("addon", "ads_agent", "__init__.py")
BACKUP_PREFIX = ".bak_adsagent_"
MAX_BACKUPS = 5

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_shared():
    """加载 backend/ 下的共享模块（路径解析 + ADS 定位 + 兼容档案）。

    这些模块是纯标准库且**自包含**，不在 sys.path 里长期留 backend/
    也没关系；这里临时加进来只为了这一句 import。
    """
    backend = os.path.join(_HERE, "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    try:
        import adscompat  # type: ignore
        import adslocate  # type: ignore
        import paths  # type: ignore

        return paths, adslocate, adscompat
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"无法加载共享模块（backend/paths.py, backend/adslocate.py, "
            f"backend/adscompat.py）：{type(e).__name__}: {e}\n"
            f"请确认安装包完整，或在解压目录里运行本脚本。"
        )


def plugin_version() -> str:
    paths, _adslocate, _adscompat = _load_shared()
    return paths.PLUGIN_VERSION


# ---------------------------------------------------------------------------
# ADS 注册 XML：结构化读写
# ---------------------------------------------------------------------------

def parse_entries(xml_path: str) -> tuple[ElementTree.ElementTree | None, dict, str]:
    """解析注册文件。

    返回 ``(tree, entries, error)``。``entries`` 是 ``{Name: (FilePath, element)}``。
    文件不存在时返回 ``(None, {}, "")`` —— 由调用方决定要不要新建。
    解析失败时 ``error`` 非空，**调用方必须中止**，不能新建覆盖。
    """
    if not os.path.isfile(xml_path):
        return None, {}, ""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    try:
        tree = ET.parse(xml_path, parser=parser)
    except ET.ParseError as e:
        return None, {}, f"注册文件不是合法 XML：{e}"
    except OSError as e:
        return None, {}, f"读取注册文件失败：{e}"

    root = tree.getroot()
    entries: dict = {}
    for child in list(root):
        tag = _local(child.tag)
        if tag != "Addon":
            continue
        name = (child.get("Name") or "").strip()
        if name:
            entries[name] = (child.get("FilePath") or "", child)
    return tree, entries, ""


def _local(tag) -> str:
    """去掉 XML 命名空间。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def entry_snapshot(entries: dict) -> dict:
    """把"其它插件"的属性做成快照，写完后逐条比对用。"""
    out = {}
    for name, (filepath, element) in entries.items():
        out[name] = dict(sorted((element.attrib or {}).items()))
    return out


def _indent(tree: ElementTree.ElementTree) -> None:
    try:
        ET.indent(tree, space="    ", level=0)   # Python 3.9+
    except AttributeError:  # pragma: no cover
        pass


def render(tree: ElementTree.ElementTree) -> str:
    """序列化（与 ADS 自带文件的风格保持一致：4 空格缩进 + 声明）。"""
    import io

    _indent(tree)
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    body = buf.getvalue().decode("utf-8").strip()
    return '<?xml version="1.0" ?>\n' + body + "\n"


def atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".eesof_addons_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows 上目标被占用会让 replace 失败：短退避重试
        last = None
        for _ in range(60):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:  # noqa: PERF203
                last = e
                import time

                time.sleep(0.02)
        if last:
            raise last
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_file(path: str, note: list | None = None) -> str:
    """内容会变的写入之前备份一份；返回备份路径（没做备份则返回空串）。"""
    if not os.path.isfile(path):
        return ""
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(
        os.path.dirname(path), f"{os.path.basename(path)}{BACKUP_PREFIX}{stamp}"
    )
    shutil.copy2(path, target)
    if note is not None:
        note.append(f"已备份原注册文件 -> {target}")
    _trim_backups(path)
    return target


def _trim_backups(path: str) -> None:
    directory = os.path.dirname(path)
    base = os.path.basename(path)
    try:
        names = sorted(
            n for n in os.listdir(directory)
            if n.startswith(base + BACKUP_PREFIX)
        )
    except OSError:
        return
    for old in names[:-MAX_BACKUPS]:
        try:
            os.unlink(os.path.join(directory, old))
        except OSError:
            pass


def restore_from(backup: str, target: str, note: list | None = None) -> None:
    if not backup or not os.path.isfile(backup):
        return
    shutil.copy2(backup, target)
    if note is not None:
        note.append(f"已从备份还原 -> {target}")


def verify_entries(xml_path: str, before: dict, exclude: str | None = None) -> str:
    """写完之后复核：其它插件一条都不能被写坏。返回错误串（空=没问题）。"""
    _tree, aftermap, err = parse_entries(xml_path)
    if err:
        return err
    before_keys = {k for k in before if k != exclude}
    after_keys = {k for k in aftermap if k != exclude}
    lost = sorted(before_keys - after_keys)
    if lost:
        return f"写入后丢失了其它插件条目：{', '.join(lost)}"

    after_snapshot = entry_snapshot(aftermap)
    changed = []
    for name in sorted(before_keys):
        if after_snapshot.get(name) != before[name]:
            changed.append(name)
    if changed:
        return f"写入后其它插件的条目被改写：{', '.join(changed)}"
    return ""


# ---------------------------------------------------------------------------
# 注册 / 注销
# ---------------------------------------------------------------------------

class InstallError(RuntimeError):
    """用户可读的安装失败（不含 traceback 也能看懂的那种）。"""


def register(xml_path: str, target_init: str, note: list | None = None,
             enabled: bool = True) -> tuple[str, str]:
    """写入/更新注册条目。返回 ``(动作, 说明)``。

    动作是 ``unchanged`` / ``updated`` / ``created`` 之一。
    """
    tree, entries, err = parse_entries(xml_path)
    if err:
        raise InstallError(err)

    before_snapshot = entry_snapshot(entries)
    existing = entries.get(ADDON_NAME)

    if existing is not None and _same_path(existing[0], target_init):
        return "unchanged", f"已注册且路径正确：{target_init}"

    if tree is None:
        root = ET.Element("EESof_Addons")
        tree = ElementTree.ElementTree(root)

    root = tree.getroot()
    action = "updated" if existing is not None else "created"
    element = existing[1] if existing is not None else ET.SubElement(root, "Addon")
    if existing is not None:
        detail_old = existing[0]
    else:
        detail_old = ""

    now = {ADDON_NAME: None}
    element.set("Name", ADDON_NAME)
    element.set("FilePath", target_init)
    element.set("Enabled", "1" if enabled else "0")
    del now, detail_old

    backup = backup_file(xml_path, note)
    text = render(tree)
    try:
        atomic_write(xml_path, text)
        problem = verify_entries(xml_path, before_snapshot, exclude=ADDON_NAME)
    except Exception as e:  # noqa: BLE001
        restore_from(backup, xml_path, note)
        raise InstallError(
            f"写入注册文件失败，已还原备份：{type(e).__name__}: {e}"
        )
    if problem:
        restore_from(backup, xml_path, note)
        raise InstallError(
            f"写入后校验失败，已还原备份（ADS 原有配置未受影响）：{problem}"
        )

    verb = "已更新为" if action == "updated" else "已注册"
    return action, f"{verb} {ADDON_NAME} -> {target_init}"


def unregister(xml_path: str, note: list | None = None) -> tuple[str, str]:
    """移除本插件的注册条目。**其它插件一个都不动。**"""
    tree, entries, err = parse_entries(xml_path)
    if err:
        raise InstallError(err)
    if tree is None:
        return "absent", "注册文件不存在，无需卸载"
    existing = entries.get(ADDON_NAME)
    if existing is None:
        return "absent", f"注册文件里没有 {ADDON_NAME}，无需卸载"

    before_snapshot = entry_snapshot(entries)
    tree.getroot().remove(existing[1])

    backup = backup_file(xml_path, note)
    try:
        atomic_write(xml_path, render(tree))
        problem = verify_entries(xml_path, before_snapshot, exclude=ADDON_NAME)
    except Exception as e:  # noqa: BLE001
        restore_from(backup, xml_path, note)
        raise InstallError(f"写入注册文件失败，已还原备份：{type(e).__name__}: {e}")
    if problem:
        restore_from(backup, xml_path, note)
        raise InstallError(f"写入后校验失败，已还原备份：{problem}")

    return "removed", f"已移除 {ADDON_NAME} 的注册（其它插件条目保持原样）"


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a or "")) == \
               os.path.normcase(os.path.normpath(b or ""))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 目标 XML 位置
# ---------------------------------------------------------------------------

def user_level_xml() -> str:
    """用户级注册文件的推测路径（ ``--user-level`` 用）。

    **未经实机验证**：ADS 内部的配置搜索顺序里 ``$HOME/hpeesof/config`` 排在
    ``$HPEESOF_DIR/config`` 之前（该顺序见于 ADS 自带的 bin\\pde_private.dll 中的
    字符串表），据此推断用户级 addons 文件也在这里；但 ADS 是否真的会读它，
    需要重启一次 ADS 才能确认。默认走安装级就是因为那条路**已经在实机上通了**。
    """
    home = os.path.expanduser("~")
    return os.path.join(home, "hpeesof", "config", "eesof_addons.xml")


def resolve_target_xml(ads_dir: str, user_level: bool) -> tuple[str, str]:
    """返回 ``(xml_path, 说明)``。"""
    if user_level:
        path = user_level_xml()
        return path, "用户级（推测路径，未经实机重启验证）"
    return os.path.join(ads_dir, "config", "eesof_addons.xml"), "安装级"


# ---------------------------------------------------------------------------
# 程序文件部署
# ---------------------------------------------------------------------------

def looks_like_source_checkout(path: str) -> bool:
    """是否是开发检出（而不是待安装的发布包）。"""
    for marker in (".git", "tests", "tools", ".workbuddy"):
        if os.path.exists(os.path.join(path, marker)):
            return True
    return False


def deploy(source: str, target: str, note: list | None = None) -> tuple[bool, list]:
    """按白名单把程序文件复制到安装目录（**不复制任何用户数据**）。"""
    import release_manifest as manifest

    files, missing, violations = manifest.collect(source)
    if missing:
        raise InstallError(
            f"源目录缺少必需文件（安装包不完整？）：{', '.join(missing)}"
        )
    if violations:
        raise InstallError(f"发布清单配置有误：{', '.join(violations)}")

    copied = []
    for rel, src in files:
        dst = os.path.join(target, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _copy_newer(src, dst)
        copied.append(rel)
    if note is not None:
        note.append(f"程序文件已同步 {len(copied)} 个 -> {target}")
    return True, copied


def _copy_newer(src: str, dst: str) -> None:
    try:
        if os.path.exists(dst) and \
                abs(os.path.getmtime(dst) - os.path.getmtime(src)) < 0.01 and \
                os.path.getsize(dst) == os.path.getsize(src):
            return
    except OSError:
        pass
    for _ in range(20):
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError:
            import time

            time.sleep(0.05)
    raise InstallError(f"复制失败（目标被占用）：{dst}")


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_detect(args, note) -> int:
    paths, adslocate, adscompat = _load_shared()
    remembered = _remembered_dirs(paths)
    found = adslocate.detect_ads_dirs(remembered=remembered, scan=not args.no_scan)
    if not found:
        note.append("没有自动探测到 ADS 2024–2027 安装。请用 --ads-dir 显式指定，"
                    "或先确认 ADS 已安装。")
        return 1
    note.append(f"探测到 {len(found)} 个候选：")
    for item in found:
        info = item["info"]
        ver = adslocate.describe_version(item["dir"])
        profile = adscompat.profile_for(ver["year"])
        if ver["status"] == "known":
            tag = ("已实机验证基线" if ver["year"] == 2027
                   else "实验性适配（未实机验证）")
        else:
            tag = "未知版本（保守降级）"
        mark = "（首选版本）" if info["is_target"] else ""
        note.append(
            f"  - {item['dir']}  年份={ver['year'] or '?'}"
            f"  Update={ver['update'] or '?'}  build={ver['build'] or '?'}"
            f"  [{tag}]  来源={item['source']}{mark}\n"
            f"      自带 Python: {info['python'] or '未找到'}"
            f"   安装树可写: {'是' if info['writable_tree'] else '否（可能需要管理员权限）'}"
        )
        if profile:
            win = profile["windows"]
            note.append(f"      官方平台要求: {adscompat.WIN_REQUIREMENT_TEXT.get(ver['year'], '')}"
                        f"（Win10={win.get('win10')} Win11={win.get('win11')}，仅 64 位）")
    return 0


def _remembered_dirs(paths) -> list:
    """上次安装记录（旧版单值字段 + 新版多安装表），供探测排序。"""
    remembered = []
    state = paths.load_install_state()
    if state.get("ads_dir"):
        remembered.append(state["ads_dir"])
    for key, entry in (paths.ads_installs() or {}).items():
        d = (entry or {}).get("ads_dir") or key
        if d and d not in remembered:
            remembered.append(d)
    return remembered


def pick_ads_dirs(args, paths, adslocate) -> list:
    """选出安装目标（一个或多个）。返回 ``[(ads_dir, info), ...]``。

    优先级：``--ads-dir``（可重复）> ``--all``（全部候选）> 自动探测。
    多候选且未明确指定时报错让用户选择 —— **绝不**替用户闷头选。
    """
    explicit = [d for d in (args.ads_dir or []) if d and d.strip()]
    if explicit:
        out = []
        seen = set()
        for d in explicit:
            info = adslocate.validate_ads_dir(d)
            if not info["valid"]:
                raise InstallError(
                    f"--ads-dir 指向的目录不是可用的 ADS 安装：{d}\n"
                    f"  原因：{info['reason']}"
                )
            key = os.path.normcase(os.path.normpath(info["dir"]))
            if key not in seen:
                seen.add(key)
                out.append((info["dir"], info))
        return out

    found = adslocate.detect_ads_dirs(remembered=_remembered_dirs(paths),
                                      scan=not args.no_scan)
    if not found:
        raise InstallError(
            "没有找到 ADS 2024–2027。请用 --ads-dir <ADS安装目录> 显式指定；\n"
            "  例如：python install_addon.py --ads-dir \"C:\\Program Files\\Keysight\\ADS2027\"\n"
            "  可以先用 python install_addon.py --detect 看看探测到了什么。"
        )
    if args.all:
        return [(i["dir"], i["info"]) for i in found]
    best = found[0]
    if len(found) > 1 and not args.accept_default:
        others = ", ".join(f"{i['dir']}({i['year'] or '?'})" for i in found[1:])
        raise InstallError(
            f"探测到多个 ADS 安装，已选 {best['dir']}（{best['year'] or '?'}）。\n"
            f"  其它候选：{others}\n"
            f"  要安装到多个版本：重复传 --ads-dir（每个版本一次）或使用 --all；\n"
            f"  确认只装第一个则加 --accept-default。"
        )
    return [(best["dir"], best["info"])]


def _print_install_state(state: dict, note) -> None:
    if not state:
        note.append("尚无安装记录（install_state.json 不存在）")
        return
    note.append(
        f"安装记录：插件版本={state.get('plugin_version') or '?'}  "
        f"数据版本={state.get('data_version') or '?'}  "
        f"安装ID={state.get('install_id') or '?'}"
    )
    if state.get("app_root"):
        note.append(f"  程序目录：{state['app_root']}")
    installs = state.get("ads_installs")
    if isinstance(installs, dict) and installs:
        note.append(f"  已登记 {len(installs)} 个 ADS 安装：")
        for key, entry in sorted(installs.items()):
            note.append(
                f"    - ADS {entry.get('year') or '?'}"
                f"（Update={entry.get('update') or '?'} build={entry.get('build') or '?'}）"
                f"  {entry.get('ads_dir') or key}"
            )
    elif state.get("ads_dir"):
        note.append(f"  ADS 目录：{state['ads_dir']}")
    if state.get("updated_at"):
        note.append(f"  记录时间：{state['updated_at']}")


def _arch_gate(adscompat, note) -> None:
    """位数检查：32 位 Windows 直接拒绝；32 位启动器在 64 位系统上仅提示。

    ADS 2024–2027 官方只发 64 位（官方支持平台表），32 位环境无法承载。
    """
    arch = adscompat.arch_report()
    if not arch["os_64bit"]:
        raise InstallError(
            f"检测到 32 位 Windows（{arch['machine']}）。"
            "ADS 2024–2027 官方仅提供 64 位版本，本插件不支持 32 位 Windows。"
        )
    if arch["python_bitness"] != 64:
        note.append(f"  说明：当前安装器进程是 {arch['python_bitness']} 位 Python"
                    "（可能是 32 位启动器）。系统是 64 位、ADS 自带解释器为 64 位，"
                    "安装不受影响；运行时一律使用 ADS 自带解释器。")


def cmd_install(args, note) -> int:
    paths, adslocate, adscompat = _load_shared()
    source_root = os.path.normpath(os.path.abspath(_HERE))

    _arch_gate(adscompat, note)
    targets = pick_ads_dirs(args, paths, adslocate)

    source_has_entry = os.path.isfile(os.path.join(source_root, PLUGIN_ENTRY))
    if not source_has_entry and not os.path.isfile(
        os.path.join(source_root, PLUGIN_ENTRY)
    ):
        raise InstallError(f"当前目录下没有 {PLUGIN_ENTRY}，请确认安装包完整")

    for idx, (ads_dir, ads_info) in enumerate(targets):
        if idx:
            note.append("")
        _install_one(args, paths, adslocate, adscompat, note,
                     source_root, ads_dir, ads_info)
    return 0


def _install_one(args, paths, adslocate, adscompat, note,
                 source_root: str, ads_dir: str, ads_info: dict) -> None:
    """对单个 ADS 安装执行：识别 → 部署 → 注册 → 记录。"""
    ver = adslocate.describe_version(ads_dir)
    year = ver["year"]
    note.append(f"ADS 安装目录：{ads_dir}")
    note.append(f"  版本识别（buildInfo.xml）：年份={year or '未识别'}"
                f"  Update={ver['update'] or '?'}  build={ver['build'] or '?'}")
    if ver["status"] == "known":
        tag = ("已实机验证基线（2027）" if year == 2027
               else f"实验性适配（ADS {year}：已完成文档与离线验证，未实机验证）")
        note.append(f"  适配状态：{tag}")
        if year != 2027:
            note.append(f"  注意：{adscompat.WIN_REQUIREMENT_TEXT.get(year, '')}；"
                        f"写/建图/仿真需在 config.ini [compat] experimental_{year} "
                        f"显式开启。")
    else:
        note.append("  适配状态：未知版本 —— 将按保守策略降级（仅只读能力，"
                    "写操作需 allow_unknown_version 显式开启）。")
    if ver["source"] == "registry":
        note.append(f"  识别来源：Windows 卸载表（{ver.get('display_name') or 'DisplayName'}）")
    elif ver["source"] == "dir_name":
        note.append("  识别来源：目录名（弱证据 —— 卸载表里没有该安装的记录；"
                    "如目录曾被改名，请人工核对版本）")
    if ver.get("weak"):
        note.append("  提示：目录名只是弱证据，实际版本以 ADS 关于页为准。")

    # 决定从哪里注册：就地 vs 复制到安装目录
    target_root = source_root
    if args.mode == "inplace":
        pass
    elif args.mode == "deploy":
        target_root = args.install_dir or paths.installed_app_root_for(year)
    else:  # auto
        if _same_path(source_root, paths.installed_app_root_for(year)) or \
                _same_path(source_root, paths.installed_app_root()):
            target_root = source_root
        elif looks_like_source_checkout(source_root):
            # 开发检出：默认就地注册，不要把重装影响到开发目录的语义复杂化
            target_root = source_root
            note.append("  识别为开发检出 —— 就地注册（改用 --mode deploy 可复制到安装目录）")
        else:
            target_root = args.install_dir or paths.installed_app_root_for(year)

    if not _same_path(target_root, source_root):
        deploy(source_root, target_root, note)
    target_init = os.path.join(target_root, PLUGIN_ENTRY)

    xml_path, scope = resolve_target_xml(ads_dir, args.user_level)
    note.append(f"  注册文件：{xml_path}（{scope}）")
    if args.user_level:
        note.append("    注意：用户级路径是根据 ADS 的配置搜索顺序推断的，"
                    "未经 ADS 重启实机验证；装完请重启 ADS 确认菜单出现，"
                    "不生效就改用默认的安装级注册。")
    if not args.user_level and not ads_info["writable_tree"]:
        note.append("    警告：ADS 安装目录当前用户不可写 —— 写入可能失败"
                    "（失败时会回滚并提示改用管理员或 --user-level）")

    action, detail = register(xml_path, target_init, note,
                             enabled=not args.disabled)
    note.append("  " + detail)

    # 数据目录：建好 + 迁移旧布局 + 首启配置（全部版本共享同一份）
    paths.ensure_data_dirs()
    migration = paths.migrate_from_legacy()
    first = paths.init_first_run()
    if first["created"]:
        note.append(f"  首次使用：已从干净模板生成配置 {first['config']}"
                    f"（不含任何真实密钥，请在面板 ⚙设置 里填 API）")
    if migration.get("copied"):
        note.append(f"  已从旧目录迁移用户数据：{', '.join(migration['copied'])}"
                    f"（旧文件原样保留，未删除）")
    if migration.get("kept_existing"):
        note.append(f"  数据目录已有有效的 {len(migration['kept_existing'])} 项，未覆盖")
    if migration.get("failed"):
        note.append(f"  迁移失败 {len(migration['failed'])} 项（旧文件保留未动）："
                    + "; ".join(f"{f['name']}: {f['error']}" for f in migration["failed"]))
    note.append(f"  用户数据目录（各版本共享）：{paths.data_root()}")

    previous = paths.load_install_state()
    old_version = previous.get("plugin_version")
    paths.record_ads_install(
        ads_dir, year=year, update=ver["update"], build=ver["build"],
        program_dir=target_root,
    )
    paths.touch_install_state(
        app_root=target_root,
        ads_dir=ads_dir,  # 旧字段：保留为"最近安装的一个"，新表才是权威
        program_files_root=target_root,
        registration_scope=("user" if args.user_level else "installation"),
        registration_file=xml_path,
    )
    if old_version and old_version != paths.PLUGIN_VERSION:
        note.append(f"  升级：{old_version} → {paths.PLUGIN_VERSION}"
                    f"（API 设置、会话与设计任务均已保留）")

    if action == "created":
        note.append("  完成。请**重启 ADS**，菜单 Tools ▸ ADS Agent 即可使用。")
    elif action == "updated":
        note.append("  完成（更新了注册路径）。请重启 ADS 生效。")
    else:
        note.append("  完成（已经是最新状态，无需变动）。")
    note.append("  装完后可跑『环境自检.bat』确认：解释器、注册状态、令牌一致性等。")


def cmd_remove(args, note) -> int:
    paths, adslocate, _adscompat = _load_shared()

    # 卸载目标：显式 --ads-dir（可重复）> --all（全部已登记）> 上次安装记录
    explicit = [d for d in (args.ads_dir or []) if d and d.strip()]
    registered = paths.ads_installs()
    targets: list = []
    if explicit:
        for d in explicit:
            info = adslocate.validate_ads_dir(d)
            targets.append(info["dir"] if info["valid"] else os.path.normpath(d))
    elif args.all:
        targets = [(entry or {}).get("ads_dir") or key
                   for key, entry in sorted(registered.items())]
        if not targets:
            raise InstallError(
                "安装记录里没有任何 ADS 版本（ads_installs 为空）。"
                "请用 --ads-dir 显式指定要卸载哪个 ADS 的注册。"
            )
    else:
        ads_dir = None
        try:
            picked = pick_ads_dirs(args, paths, adslocate)
            ads_dir = picked[0][0] if picked else None
        except InstallError as e:
            state = paths.load_install_state()
            if state.get("ads_dir"):
                ads_dir = state["ads_dir"]
            if not ads_dir:
                raise
            note.append(f"（未能自动探测 ADS，改用上次安装记录：{ads_dir}）")
        if ads_dir:
            targets = [ads_dir]

    if len(targets) > 1:
        note.append(f"将对 {len(targets)} 个 ADS 安装执行卸载（其它版本不受影响）：")

    for ads_dir in targets:
        note.append(f"ADS 安装目录：{ads_dir}")
        xml_path, scope = resolve_target_xml(ads_dir, args.user_level)
        note.append(f"  注册文件：{xml_path}（{scope}）")
        action, detail = unregister(xml_path, note)
        note.append("  " + detail)
        paths.remove_ads_install(ads_dir)
    note.append("请**重启对应版本的 ADS** 让卸载生效。")

    # 程序文件：只删"不再被任何已登记安装引用"的程序目录
    remaining = paths.ads_installs()
    still_used = {os.path.normcase(os.path.normpath(str((e or {}).get("program_dir") or "")))
                  for e in remaining.values()}
    default_target = args.install_dir or paths.load_install_state().get("program_files_root") \
        or paths.installed_app_root()
    candidates = {default_target}
    for ads_dir in targets:
        entry = registered.get(os.path.normcase(os.path.normpath(ads_dir))) or {}
        if entry.get("program_dir"):
            candidates.add(entry["program_dir"])
    for target in sorted(candidates):
        if not target or not os.path.isdir(target):
            continue
        if os.path.normcase(os.path.normpath(target)) in still_used \
                and not _same_path(target, _HERE):
            note.append(f"程序文件保留在 {target}（仍被其它已登记 ADS 版本引用）")
            continue
        if args.remove_files:
            if _same_path(target, _HERE):
                note.append("当前目录就是安装目录 —— 为安全起见不删除，请手动删除该目录。")
            else:
                try:
                    shutil.rmtree(target)
                    note.append(f"已删除程序文件目录：{target}")
                except OSError as e:
                    note.append(f"删除程序文件目录失败（可手动删除）：{target} —— {e}")
        else:
            note.append(f"程序文件保留在 {target}（加 --remove-files 才会删）")

    if args.purge_data:
        root = paths.data_root()
        if os.path.isdir(root):
            try:
                shutil.rmtree(root)
                note.append(f"已删除用户数据目录：{root}")
            except OSError as e:
                note.append(f"删除用户数据失败（可手动删）：{root} —— {e}")
    else:
        note.append(f"用户数据已保留在 {paths.data_root()}"
                    f"（配置、会话、设计任务都在里面；要清除请加 --purge-data）")
    return 0


def cmd_status(args, note) -> int:
    paths, adslocate, _adscompat = _load_shared()
    note.append(f"插件版本：{paths.PLUGIN_VERSION}   数据格式版本：{paths.DATA_VERSION}")
    note.append(f"程序目录：{paths.app_root()}")
    note.append(f"数据目录：{paths.data_root()}")
    note.append(f"配置文件：{paths.config_path()}"
                f"{'（存在）' if os.path.exists(paths.config_path()) else '（不存在）'}")
    note.append(f"设计任务：{paths.design_jobs_dir()}")
    note.append(f"会话文件：{sessions_state(paths)}")

    state = paths.load_install_state()
    _print_install_state(state, note)

    remembered = _remembered_dirs(paths)
    ads_override = None
    if args.ads_dir:
        ads_override = args.ads_dir
    elif os.environ.get("HPEESOF_DIR"):
        remembered.insert(0, os.environ["HPEESOF_DIR"])

    if ads_override:
        infos = [adslocate.validate_ads_dir(ads_override)]
        dirs = [infos[0]["dir"]]
    else:
        found = adslocate.detect_ads_dirs(remembered=remembered, scan=not args.no_scan)
        dirs = [i["dir"] for i in found]
        infos = [i["info"] for i in found]

    if not dirs:
        note.append("未探测到 ADS 安装（可用 --ads-dir 指定）")
        return 1

    registered_anywhere = False
    for ads_dir, info in zip(dirs, infos):
        if not info["valid"]:
            continue
        xml_path = os.path.join(ads_dir, "config", "eesof_addons.xml")
        _tree, entries, err = parse_entries(xml_path)
        if err:
            note.append(f"[{ads_dir}] 注册文件解析失败：{err}")
            continue
        record = entries.get(ADDON_NAME)
        note.append(f"[{ads_dir}] 注册文件：{xml_path}")
        note.append(f"  共 {len(entries)} 个插件条目")
        if record is None:
            note.append(f"  未注册 {ADDON_NAME}")
            continue
        registered_anywhere = True
        registered_init = os.path.join(paths.app_root(), PLUGIN_ENTRY)
        status = "路径一致" if _same_path(record[0], registered_init) else \
            f"**路径不一致** -> 实际注册到 {record[0]}"
        enabled = (record[1].get("Enabled") or "").strip()
        note.append(f"  已注册 Enabled={enabled or '?'}  {status}")
        if not _same_path(record[0], registered_init):
            note.append("  处理办法：在本目录重新运行一次 install_addon.py，"
                        "它只改这一条并把路径更新到当前程序目录。")

    ul = user_level_xml()
    if os.path.isfile(ul):
        _t, entries, err = parse_entries(ul)
        if not err and entries.get(ADDON_NAME):
            registered_anywhere = True
            note.append(f"[用户级] {ul} 里也有一条 {ADDON_NAME} —— "
                        "安装级与用户级同时存在时 ADS 可能加载两次，建议只保留一处")

    note.append(f"结论：{'已注册' if registered_anywhere else '未注册'}")
    return 0 if registered_anywhere else 1


def sessions_state(paths) -> str:
    p = paths.sessions_path()
    if not os.path.exists(p):
        return f"{p}（不存在，首次对话时会自动生成）"
    try:
        size = os.path.getsize(p)
    except OSError:
        size = -1
    return f"{p}（{size} 字节）"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="install_addon.py",
        description="ADS Agent 安装 / 卸载 / 状态查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--remove", action="store_true", help="卸载注册")
    ap.add_argument("--status", action="store_true", help="查询注册状态")
    ap.add_argument("--detect", action="store_true", help="只列出探测到的 ADS 候选")
    ap.add_argument("--ads-dir", action="append", dest="ads_dir",
                    help="显式指定 ADS 安装目录（可重复传，安装/卸载多个版本互不影响）")
    ap.add_argument("--all", action="store_true",
                    help="安装到全部探测到的 ADS（或卸载全部已登记的 ADS）")
    ap.add_argument("--install-dir", help="程序文件安装目录（配合 --mode deploy）")
    ap.add_argument("--mode", choices=("auto", "inplace", "deploy"), default="auto",
                    help="auto=自动判断；inplace=就地注册；deploy=复制到安装目录后注册")
    ap.add_argument("--user-level", action="store_true",
                    help="写用户级注册文件（推测路径，未经实机重启验证）")
    ap.add_argument("--accept-default", action="store_true",
                    help="探测到多个 ADS 时不报错，直接用第一个")
    ap.add_argument("--no-scan", action="store_true", help="不做磁盘扫描，只用环境/注册表")
    ap.add_argument("--remove-files", action="store_true",
                    help="卸载时一并删除程序文件目录")
    ap.add_argument("--purge-data", action="store_true",
                    help="卸载时一并删除 %%LOCALAPPDATA%%\\ADSAgent（**用户数据不可恢复**）")
    ap.add_argument("--disabled", action="store_true", help="注册但先不启用")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    note: list = []
    code = 0
    try:
        if args.detect:
            code = cmd_detect(args, note)
        elif args.remove:
            code = cmd_remove(args, note)
        elif args.status:
            code = cmd_status(args, note)
        else:
            code = cmd_install(args, note)
    except InstallError as e:
        note.append(f"错误：{e}")
        code = 2
    except PermissionError as e:
        note.append(
            f"错误：没有写入权限 —— {e}\n"
            f"  ADS 装在 Program Files 之类的受保护目录时需要**以管理员身份**运行安装；\n"
            f"  或者试试 --user-level（注意：该路径未经实机验证）。\n"
            f"  注册文件没有被改动。"
        )
        code = 2
    except OSError as e:
        note.append(f"错误：{type(e).__name__}: {e}")
        code = 2

    if args.json:
        print(json.dumps({"code": code, "lines": note}, ensure_ascii=False, indent=1))
    else:
        for line in note:
            print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
