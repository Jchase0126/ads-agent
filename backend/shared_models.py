"""备份模型包到统一 libraries 目录，并复制到当前 Workspace 后导入。"""

from __future__ import annotations

import os
import hashlib
import json
import posixpath
import re
import threading
import html
from contextlib import contextmanager

import model_store
import paths
import config


class SharedModelError(RuntimeError):
    pass


def library_root(cfg: dict, workspace: str = "") -> str:
    """首次选择工作区同级目录并持久化，此后跨父目录工作区仍复用它。"""
    raw = (os.environ.get("ADS_AGENT_LIBRARY_ROOT", "").strip()
           or str((cfg or {}).get("model_library_root") or "").strip())
    inferred = not raw
    if not raw:
        if not workspace:
            raise SharedModelError("未配置统一模型库目录，也无法从当前 Workspace 推导位置。")
        raw = os.path.join(os.path.dirname(os.path.abspath(workspace)), "libraries")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(expanded):
        raise SharedModelError("统一 libraries 目录必须配置为绝对路径。")
    root = os.path.realpath(os.path.abspath(expanded))
    ads_root = os.environ.get(paths.ENV_ADS_DIR, "").strip()
    if ads_root:
        ads_root = os.path.realpath(os.path.abspath(ads_root))
        try:
            if os.path.commonpath((root, ads_root)) == ads_root:
                raise SharedModelError(
                    "共享模型库目录不能位于 ADS 安装目录内；请配置到可写的用户数据目录。")
        except ValueError:
            pass
    app_root = os.path.realpath(paths.app_root())
    try:
        if os.path.commonpath((root, app_root)) == app_root:
            raise SharedModelError(
                "共享模型库目录不能位于 ADS Agent 程序目录内；请使用独立数据目录。")
    except ValueError:
        pass
    if inferred:
        try:
            selected = config.persist_model_library_root(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SharedModelError("统一模型库目录无法保存到用户配置。") from exc
        # A concurrent first import may already have selected a different root.
        root = library_root({"model_library_root": selected}, workspace)
        if cfg is not None:
            cfg["model_library_root"] = root
    return root


def set_library_root(cfg: dict, root: str) -> dict:
    """显式更改根目录；已有模型资产不搬移、不删除。"""
    if os.environ.get("ADS_AGENT_LIBRARY_ROOT", "").strip():
        raise SharedModelError("ADS_AGENT_LIBRARY_ROOT 环境变量正在覆盖模型库目录，请先更改该变量。")
    selected = library_root({"model_library_root": root})
    config.persist_model_library_root(selected, only_if_empty=False)
    cfg["model_library_root"] = selected
    return {"library_root": selected, "existing_assets_moved": False}


def _archive_path(root: str, record: dict) -> str:
    path = model_store._abs_from_rel(root, record.get("archive_relpath"))
    if not path or not os.path.isfile(path):
        raise SharedModelError("模型包原始 ZIP 缺失，无法备份或导入。")
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    try:
        if os.path.commonpath((real_root, real_path)) != real_root:
            raise SharedModelError("模型包清单中的 ZIP 路径越出了所属模型库目录。")
    except ValueError as e:
        raise SharedModelError("模型包 ZIP 与模型库目录不在同一文件系统路径下。") from e
    path = real_path
    if model_store.sha256_file(path) != str(record.get("sha256") or ""):
        raise SharedModelError("模型包 ZIP 指纹与清单不一致，已停止复制。")
    return path


def _archive_metadata(path: str, record: dict) -> tuple[str, dict]:
    """Hash validated member paths/bytes and read static model declarations.

    Compression, order, dates and directory entries do not affect identity.
    Paths and every file's bytes do: different releases are never guessed equal.
    No extraction or package execution occurs, and the normal ZIP limits apply.
    """
    entries = []
    catalog = []
    seen = set()
    total = 0
    text_budget = 32 * 1024 * 1024
    cap = 50000
    truncated = False
    attach = record.get("library_attach") or {}
    kit_root = attach.get("kit_root") or ""
    libraries = attach.get("defined_libraries") or []
    declared_name = (record.get("native_list") or {}).get("design_kit_name")
    fallback_library = declared_name if declared_name in {
        item.get("name") for item in libraries} else None
    components = {posixpath.splitext(posixpath.basename(member))[0]
                  for member in (record.get("native_list") or {}).get("atf", [])}

    def add(part, cell, member, kind):
        nonlocal truncated
        if not part or part.upper() in ("NULL", "NONE"):
            return
        library = model_store._library_for_path(kit_root, member, libraries) or fallback_library
        key = (part, cell, library)
        if key in seen:
            return
        seen.add(key)
        if len(catalog) >= cap:
            truncated = True
            return
        catalog.append({"part": part, "cell": cell, "kind": kind,
                        "member_path": member,
                        "library": library,
                        "library_source": "lib.defs path or matching declared DESIGN_KIT_NAME",
                        "source": "archive_static_declaration", "validated": False})

    with model_store._open_zip(path) as archive:
        plan = model_store._plan_entries(archive, model_store.DEFAULT_LIMITS)
        for info, member, is_dir in sorted(plan, key=lambda item: item[1]):
            if is_dir:
                continue
            digest = hashlib.sha256()
            read = 0
            chunks = []
            is_text = member.lower().endswith((".ael", ".htm", ".html")) and text_budget > 0
            text_cap = min(1024 * 1024, text_budget) if is_text else 0
            with archive.open(info) as stream:
                while True:
                    chunk = stream.read(128 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    read += len(chunk)
                    total += len(chunk)
                    if read > info.file_size or total > model_store.DEFAULT_LIMITS["max_total_bytes"]:
                        raise SharedModelError("模型库 ZIP 实际内容超过安全限制。")
                    if is_text and read <= text_cap:
                        chunks.append(chunk)
            entries.append([member, read, digest.hexdigest()])
            stem = posixpath.splitext(posixpath.basename(member))[0]
            if member.lower().endswith((".atf", ".ael")) and not stem.endswith("_list"):
                add(stem, stem, member, "component_definition")
            if model_store._TOUCHSTONE_RE.search(member):
                part, _bias = model_store._bias_from_stem(stem)
                add(part or stem, "", member, "touchstone_filename")
            text = b"".join(chunks).decode("utf-8", errors="replace")
            text_budget -= sum(len(c) for c in chunks)
            if text:
                for match in re.finditer(r'create_item\s*\(\s*"([^"\r\n]+)"', text):
                    add(match.group(1), match.group(1), member, "cell_declaration")
                if stem.endswith("_list"):
                    cell = stem[:-5]
                    for match in re.finditer(r'create_constant_form\s*\(\s*"([^"\r\n]+)"', text):
                        add(match.group(1), cell, member, "part_number_option")
                # Dynamic kits often ship compiled ATF only. Their vendor HTML
                # tables explicitly label a PartNumber column; read that data
                # instead of guessing binary ATF strings to be part numbers.
                if member.lower().endswith((".htm", ".html")) and stem in components:
                    for table in re.findall(r"<table\b[^>]*>(.*?)</table\s*>", text, re.I | re.S):
                        column = None
                        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", table, re.I | re.S):
                            fields = [html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
                                      for value in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", row, re.I | re.S)]
                            labels = [re.sub(r"[\s_-]+", "", value).casefold() for value in fields]
                            if "partnumber" in labels:
                                column = labels.index("partnumber")
                            elif column is not None and column < len(fields):
                                part = fields[column]
                                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+/-]{1,127}", part):
                                    add(part, stem, member, "documented_part_number")
            if is_text and read > text_cap:
                truncated = True
        if text_budget <= 0:
            truncated = True
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), {
        "schema": 3, "entries": catalog, "truncated": truncated,
        "indexed": len(catalog), "at": model_store.utc_now(),
        "note": "静态索引只用于定位所属 ZIP，不代表 ADS 已加载或仿真验证通过。"}


def _ensure_catalog(root: str, record: dict) -> dict:
    path = _archive_path(root, record)
    if (record.get("shared_catalog") or {}).get("schema") == 3 and record.get("content_sha256"):
        return record
    identity, catalog = _archive_metadata(path, record)
    return model_store.update_package(root, record["package_id"],
                                      content_sha256=identity, shared_catalog=catalog)


@contextmanager
def _exclusive_catalog(root: str):
    """Fail closed on contention and keep long catalog scans from looking stale."""
    os.makedirs(root, exist_ok=True)
    lock = model_store.manifest_lock(root)
    if not lock.acquire():
        raise SharedModelError("共享模型库正在被其他进程更新，请稍后重试。")
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(2):
            try:
                os.utime(lock.path, None)
            except OSError:
                return

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        lock.release()


def backup_package(cfg: dict, workspace: str, record: dict) -> dict:
    """把当前 Workspace 清单中的原始 ZIP 备份到共享库，内容哈希去重。"""
    source_root = model_store.store_root(workspace)
    source_path = _archive_path(source_root, record)
    root = library_root(cfg, workspace)
    try:
        if os.path.commonpath((root, os.path.realpath(workspace))) == os.path.realpath(workspace):
            raise SharedModelError("统一 libraries 目录不能位于当前 ADS Workspace 内。")
    except ValueError:
        pass
    identity, _ = _archive_metadata(source_path, record)
    # Serialise identity lookup and storage so simultaneous repacks cannot make duplicates.
    skipped = []
    with _exclusive_catalog(root):
        saved = None
        for candidate in model_store.list_packages(root):
            try:
                candidate = _ensure_catalog(root, candidate)
            except (SharedModelError, model_store.ModelStoreError, OSError) as exc:
                skipped.append({"package_id": candidate["package_id"], "error": str(exc)})
                continue
            if candidate.get("content_sha256") == identity:
                _archive_path(root, candidate)  # Never reuse a damaged canonical ZIP.
                alias = {"sha256": record.get("sha256"),
                         "filename": record.get("original_filename"),
                         "workspace": workspace}
                aliases = list(candidate.get("archive_aliases") or [])
                if alias not in aliases:
                    aliases.append(alias)
                saved = model_store.update_package(root, candidate["package_id"],
                                                    archive_aliases=aliases)
                saved["reused"] = True
                break
        if saved is None:
            saved = model_store.save_archive(
                workspace, record.get("original_filename") or record.get("stored_filename") or "model.zip",
                source_path=source_path, source_session=record.get("source_session") or "",
                storage_root=root, source_workspace=workspace)
        shared = model_store.scan_archive(root, saved["package_id"])
        shared = _ensure_catalog(root, shared)
    info = {
        "backed_up": True,
        "package_id": saved["package_id"],
        "sha256": saved.get("sha256") or record.get("sha256"),
        "reused": bool(saved.get("reused")),
        "library_root": root,
        "content_sha256": identity,
        "source_sha256": record.get("sha256"),
        "unavailable_packages": skipped,
    }
    try:
        model_store.update_package(source_root, record["package_id"],
                                  shared_backup={k: v for k, v in info.items()
                                                 if k != "library_root"})
    except model_store.ModelStoreError:
        pass
    return {**info, "package": shared}


def list_packages(cfg: dict, workspace: str, args: dict | None = None) -> dict:
    """列出统一 libraries 目录中的备份包。"""
    root = library_root(cfg, workspace)
    if not os.path.isdir(root):
        return {"library_root": root, "total": 0, "returned": 0, "packages": []}
    records = model_store.list_packages(root)
    args = args or {}
    kind = str(args.get("package_kind") or "").strip()
    query = str(args.get("query") or "").strip().casefold()
    unavailable = []
    if kind:
        records = [r for r in records if r.get("package_kind") == kind]
    if query:
        matched = []
        for record in records:
            try:
                record = _ensure_catalog(root, record)
            except (SharedModelError, model_store.ModelStoreError, OSError) as exc:
                unavailable.append({"package_id": record["package_id"], "error": str(exc)})
                continue
            metadata = " ".join(str(record.get(k) or "") for k in
                                ("original_filename", "vendor", "version", "package_id"))
            entries = (record.get("shared_catalog") or {}).get("entries") or []
            if query in metadata.casefold() or any(_entry_matches(e, query) for e in entries):
                matched.append(record)
        records = matched
    try:
        limit = max(1, min(int(args.get("max_items") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    total = len(records)
    records = records[:limit]
    from model_tools import _record_view
    packages = [_record_view(r, include_models=False) for r in records]
    for package in packages:
        package["shared_backup"] = {"backed_up": True}
    return {"library_root": root, "total": total, "returned": len(records),
            "packages": packages, "unavailable_packages": unavailable}


def _entry_matches(entry: dict, query: str) -> bool:
    return query in " ".join(str(entry.get(k) or "") for k in
                             ("part", "cell", "library", "member_path")).casefold()


def search_models(cfg: dict, workspace: str, args: dict | None = None) -> dict:
    """Find static part/cell declarations and return the owning importable ZIP ID."""
    root = library_root(cfg, workspace)
    args = args or {}
    query = str(args.get("query") or args.get("part") or "").strip().casefold()
    if not query:
        raise SharedModelError("请提供具体料号、元件名或关键词进行共享模型检索。")
    library = str(args.get("library") or "").strip().casefold()
    kind = str(args.get("package_kind") or "").strip()
    try:
        limit = max(1, min(int(args.get("max_items") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    hits = []
    total = 0
    truncated_packages = []
    unavailable = []
    for record in model_store.list_packages(root) if os.path.isdir(root) else []:
        if args.get("package_id") and record["package_id"] != args["package_id"]:
            continue
        if kind and record.get("package_kind") != kind:
            continue
        try:
            record = _ensure_catalog(root, record)
        except (SharedModelError, model_store.ModelStoreError, OSError) as exc:
            unavailable.append({"package_id": record["package_id"], "error": str(exc)})
            continue
        catalog = record.get("shared_catalog") or {}
        if catalog.get("truncated"):
            truncated_packages.append(record["package_id"])
        for entry in catalog.get("entries") or []:
            if query and not _entry_matches(entry, query):
                continue
            if library and library not in str(entry.get("library") or "").casefold():
                continue
            total += 1
            if len(hits) < limit:
                hits.append({**entry, "package_id": record["package_id"],
                             "original_filename": record.get("original_filename"),
                             "vendor": record.get("vendor"), "version": record.get("version"),
                             "requires_workspace_import": True})
    return {"library_root": root, "total": total, "returned": len(hits),
            "models": hits, "truncated_packages": truncated_packages,
            "unavailable_packages": unavailable,
            "search_scope": "static ZIP declarations, not ADS simulation readiness"}


def copy_to_workspace(cfg: dict, workspace: str, package_id: str) -> dict:
    """验证共享 ZIP 指纹后将其存档到当前 Workspace，返回其本地清单记录。"""
    root = library_root(cfg, workspace)
    try:
        record = model_store.get_package(root, package_id)
    except model_store.ModelStoreError as e:
        raise SharedModelError(
            f"统一 libraries 目录中没有模型包 {package_id}；请先查询可用备份。") from e
    source_path = _archive_path(root, record)
    local = model_store.save_archive(
        workspace, record.get("original_filename") or record.get("stored_filename") or "model.zip",
        source_path=source_path, source_session="cross_workspace_import",
        source_workspace=workspace)
    local_root = model_store.store_root(workspace)
    copied = model_store.scan_archive(local_root, local["package_id"])
    model_store.update_package(
        local_root, local["package_id"],
        shared_backup={"backed_up": True, "package_id": package_id,
                       "sha256": record.get("sha256"), "reused": True})
    return copied
