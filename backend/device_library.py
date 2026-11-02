"""器件模型库 —— 统一身份、发布、检索与跨工作区导入。

为什么要有这一层
----------------
PDF 建模产物（device_models 的记录）此前只活在 ``<data_root>/datasheets/models/``：
它有拟合参数与建图计划，却没有「发布」概念 —— 不能按型号检索、不能跨工作区
复用，和原厂 ZIP 模型包（model_store / shared_models）是两套互不相通的系统。

本模块把两者收进**同一个库根目录与同一套接口**：

    <library_root>/                          # 与 ZIP 包共享的统一模型库根
      archives/ extracted/ manifest.json     # ← 原厂 ZIP 包（model_store 原样）
      devices/                               # ← 本模块的器件模型
        index.json                           #    轻量索引（可由清单重建）
        <device_id>/device.json
        <model_id>/model.json
          revisions/<revision_id>/
            manifest.json                    #    完整清单（唯一事实源）
            source/evidence.json             #    资料与证据引用（不复制 PDF 本体）
            model/component_plan.json        #    可放置元件的建图计划
            model/<deps>                     #    依赖文件（S2P 等）
            validation/validation_plan.json  #    验证电路计划（TermG）
            validation/validation.json       #    发布时的最新验证报告

身份分层（六个 ID 在这里的落点）：

* document_id / attachment_instance_id —— 由 doc_assets 与面板各自持有，
  本模块只在 evidence 里**引用**，不重新发明；
* task_id —— 建模任务的持久身份，写在 device_models 记录的 ``task`` 块里；
* device_id —— 「厂商 + 型号」的稳定身份（归一化只去大小写与分隔符；
  BFR106 ≠ BRF106，型号拼写差异绝不自动合并）；
* model_id —— 「器件 + 模型形态（类型/配置/条件/参考阻抗）」的稳定身份；
* revision_id —— **不可变**模型内容（参数 + 元件计划 + 依赖指纹）的版本。
  验证报告不属于模型内容：同一修订补充验证只追加报告，不产生新版本。

发布纪律
--------
* 先写 ``.staging/<task 前缀>/`` 再整目录原子改名入库；半成品不进索引；
* 同内容重复发布 → 同 revision_id → 复用（不新建、不覆盖）；
* 已发布版本的模型内容永不改写；新内容 = 新 revision；
* verdict != validated 的模型只能以 ``candidate`` 状态发布（明确标注），
  不得冒充已验证版本；
* 索引损坏可由各修订的 manifest.json 重建（``rebuild_index``）。

本模块只用标准库；ADS 侧操作通过注入的 ``call_tool(name, args)`` 完成，
便于离线测试。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile

import model_store
import shared_models


class DeviceLibraryError(RuntimeError):
    """模型库的可预期错误（工具层如实转成 error，不冒充成功）。"""


SCHEMA = 1
MANIFEST_SCHEMA = 1
BUILDER_ID = "device_publish_v1"
#: 修订归档格式版本。2 起修订自带建模记录/曲线/参数快照，可在全新数据根复验；
#: 1 的旧修订只能靠源数据根，读取时如实标 incomplete。
ARCHIVE_VERSION = 2

STATUS_CANDIDATE = "candidate"
STATUS_PUBLISHED = "published"
STATUSES = (STATUS_CANDIDATE, STATUS_PUBLISHED)

#: 建模任务的状态机。blocked/failed/cancelled 与成功阶段分开记录。
TASK_STATES = ("received", "parsed", "target_resolved", "evidence_ready",
               "model_ready", "packaged", "validation_completed", "published",
               "blocked", "failed", "cancelled")

_ID_HEX = re.compile(r"[0-9a-f]{16}\Z")
_TECH_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_MAX_REVISION_REPORTS = 10


def _norm_part(text: str) -> str:
    return re.sub(r"[\s_\-/]+", "", str(text or "")).upper()


def _norm_key(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _short_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def device_identity(vendor: str, part_number: str) -> str:
    """器件身份：厂商 + 型号（归一化后）。不同厂商/型号绝不合并。"""
    vendor_n = re.sub(r"\s+", " ", str(vendor or "").strip()).casefold()
    part_n = _norm_part(part_number)
    if not part_n:
        raise DeviceLibraryError("器件型号缺失，无法确定器件身份")
    return "dev_" + _short_hash(_norm_key({"vendor": vendor_n, "part": part_n}))


def _conditions_key(conditions: dict) -> dict:
    out = {}
    for key, value in sorted((conditions or {}).items()):
        try:
            out[str(key)] = round(float(value), 9)
        except (TypeError, ValueError):
            out[str(key)] = str(value)
    return out


def model_identity(device_id: str, model_kind: str, configuration: str,
                   conditions: dict, z0_ohm) -> str:
    """模型身份：同一器件下「模型形态」相同的就是同一个模型。

    形态 = 类型 + 测量配置 + 测试条件 + 参考阻抗 —— 固定偏置小信号模型与
    其它偏置下的模型**不是**同一个模型，不得混用。
    """
    payload = _norm_key({"device": device_id, "kind": model_kind,
                         "config": configuration,
                         "conditions": _conditions_key(conditions),
                         "z0": None if z0_ohm is None else round(float(z0_ohm), 6)})
    return "mdl_" + _short_hash(payload)


def canonical_state(record: dict | None, published: bool = False) -> str:
    """把建模记录的 stage 映射到规范任务状态机（不代表验证结论）。"""
    if published:
        return "published"
    if not record:
        return "received"
    stage = str(record.get("stage") or "")
    if (record.get("task") or {}).get("cancelled"):
        return "cancelled"
    if stage in ("validated", "validation_incomplete", "validation_failed",
                 "numeric_spec_validated"):
        return "validation_completed"
    if stage == "build_planned" or record.get("build"):
        return "packaged"
    if stage == "fitted":
        return "model_ready"
    if stage == "fit_failed_no_candidate_meets_rule":
        return "failed"
    if stage == "blocked_missing_evidence":
        return "blocked"
    if stage == "candidate_ready":
        curves = record.get("curve_ids") or {}
        if any((curves.get(k) or {}) for k in ("S", "Y")):
            return "evidence_ready"
        return "target_resolved"
    return "target_resolved" if stage else "received"


# ---------------------------------------------------------------------------
# 路径与索引
# ---------------------------------------------------------------------------

def devices_root(cfg: dict, workspace: str) -> str:
    """器件库根目录：统一模型库根下的 ``devices/``（与 ZIP 包同根、互不覆盖）。"""
    root = shared_models.library_root(cfg, workspace)
    return os.path.join(root, "devices")


def _revision_dir(root: str, device_id: str, model_id: str, revision_id: str) -> str:
    for value, label in ((device_id, "device_id"), (model_id, "model_id"),
                         (revision_id, "revision_id")):
        if not isinstance(value, str) or not value.startswith(("dev_", "mdl_", "rev_")) \
                or not _ID_HEX.fullmatch(value[4:]):
            raise DeviceLibraryError(f"无效的 {label}；请使用本工具返回的标识")
    return os.path.join(root, device_id, model_id, "revisions", revision_id)


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=1)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass


def _write_bytes(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def manifest_path(root: str, device_id: str, model_id: str, revision_id: str) -> str:
    return os.path.join(_revision_dir(root, device_id, model_id, revision_id),
                        "manifest.json")


def load_manifest(root: str, device_id: str, model_id: str, revision_id: str) -> dict:
    path = manifest_path(root, device_id, model_id, revision_id)
    if not os.path.isfile(path):
        raise DeviceLibraryError(f"器件模型库中没有该修订（{revision_id}）；清单不存在")
    return _read_json(path)


def write_manifest(root: str, manifest: dict) -> None:
    """只写回清单的**可追加**部分（验证报告）；模型内容本身不可变。

    指纹在写入前重算：清单被外部改动过（或调用方拼了半截对象）时，
    指纹不一致会立刻暴露，而不是把一份来历不明的内容当成有效清单。
    """
    identity = manifest.get("identity") or {}
    path = manifest_path(root, identity.get("device_id"), identity.get("model_id"),
                         identity.get("revision_id"))
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        _norm_key(manifest).encode("utf-8")).hexdigest()
    _write_json(path, manifest)


def manifest_by_revision(root: str, revision_id: str) -> dict:
    """只给 revision_id 时经索引反查清单（索引损坏会先由清单重建）。"""
    target = str(revision_id or "").strip()
    if not target:
        raise DeviceLibraryError("缺少 revision_id")
    for entry in load_index(root).get("entries") or []:
        if entry.get("revision_id") == target:
            return load_manifest(root, entry["device_id"], entry["model_id"], target)
    raise DeviceLibraryError(f"模型库索引中没有修订 {target}；请先检索确认型号与版本")


def load_index(root: str) -> dict:
    """读索引；损坏时自动由清单重建（需求：发布索引损坏应能通过清单恢复）。"""
    path = os.path.join(root, "index.json")
    try:
        index = _read_json(path)
        if index.get("schema") == SCHEMA and isinstance(index.get("entries"), list):
            return index
    except (OSError, ValueError):
        pass
    return rebuild_index(root)


def rebuild_index(root: str) -> dict:
    """扫描 devices/*/…/revisions/*/manifest.json 重建轻量索引。"""
    entries = []
    if os.path.isdir(root):
        for device_id in sorted(os.listdir(root)):
            device_dir = os.path.join(root, device_id)
            if not device_id.startswith("dev_") or not os.path.isdir(device_dir):
                continue
            for model_id in sorted(os.listdir(device_dir)):
                rev_root = os.path.join(device_dir, model_id, "revisions")
                if not model_id.startswith("mdl_") or not os.path.isdir(rev_root):
                    continue
                for revision_id in sorted(os.listdir(rev_root)):
                    path = os.path.join(rev_root, revision_id, "manifest.json")
                    if not os.path.isfile(path):
                        continue
                    try:
                        entries.append(_index_entry(_read_json(path)))
                    except (OSError, ValueError):
                        continue
    index = {"schema": SCHEMA, "updated_at": model_store.utc_now(),
             "entries": entries}
    _write_json(os.path.join(root, "index.json"), index)
    return index


def _index_entry(manifest: dict) -> dict:
    device = manifest.get("device") or {}
    model = manifest.get("model") or {}
    identity = manifest.get("identity") or {}
    validation = manifest.get("validation") or {}
    return {
        "device_id": identity.get("device_id"),
        "model_id": identity.get("model_id"),
        "revision_id": identity.get("revision_id"),
        "part": device.get("part_number"),
        "vendor": device.get("vendor"),
        "display_name": device.get("display_name") or device.get("part_number"),
        "aliases": list(device.get("aliases") or []),
        "device_kind": device.get("device_kind"),
        "model_kind": model.get("kind"),
        "configuration": model.get("configuration"),
        "conditions": model.get("conditions") or {},
        "z0_ohm": model.get("z0_ohm"),
        "status": manifest.get("status"),
        "verdict": validation.get("verdict"),
        "validated_band_hz": _validated_band(manifest),
        "capabilities_unsupported": list((manifest.get("capabilities") or {})
                                         .get("unsupported") or []),
        "tech_cell_name": (manifest.get("ads_library") or {}).get("tech_cell_name"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "published_at": manifest.get("published_at"),
    }


def _validated_band(manifest: dict) -> list | None:
    scope = (manifest.get("validation") or {}).get("validation_scope_hz") or {}
    bands = [v for v in scope.values() if isinstance(v, list) and len(v) == 2]
    if not bands:
        return None
    return [min(b[0] for b in bands), max(b[1] for b in bands)]


def _save_index_entry(root: str, manifest: dict) -> None:
    """把一条修订并入索引（在库锁内调用；锁拿不到时跳过并如实标注）。"""
    index = load_index(root)
    entry = _index_entry(manifest)
    key = (entry["device_id"], entry["model_id"], entry["revision_id"])
    index["entries"] = [e for e in index["entries"]
                        if (e.get("device_id"), e.get("model_id"),
                            e.get("revision_id")) != key]
    index["entries"].append(entry)
    index["updated_at"] = model_store.utc_now()
    _write_json(os.path.join(root, "index.json"), index)


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------

def publish_device_model(record: dict, *, vendor: str, part_number: str,
                         display_name: str = "", aliases=None,
                         physical_package: dict | None = None,
                         component_plan: dict | None = None,
                         deps: list | None = None,
                         validation_plan: dict | None = None,
                         allow_unverified: bool = False,
                         task_id: str = "",
                         library_root: str = "") -> dict:
    """在共享库写锁内发布，避免并发同名操作破坏 revision / index 一致性。"""
    if not library_root:
        raise DeviceLibraryError("library_root 未解析")
    os.makedirs(library_root, exist_ok=True)
    lock = model_store.manifest_lock(library_root)
    if not lock.acquire(timeout=30):
        raise DeviceLibraryError("器件模型库正被其他进程更新，请稍后重试；本次未写入")
    try:
        return _publish_device_model_locked(
            record, vendor=vendor, part_number=part_number,
            display_name=display_name, aliases=aliases,
            physical_package=physical_package, component_plan=component_plan,
            deps=deps, validation_plan=validation_plan,
            allow_unverified=allow_unverified, task_id=task_id,
            library_root=library_root)
    finally:
        lock.release()


def _publish_device_model_locked(record: dict, *, vendor: str, part_number: str,
                                 display_name: str = "", aliases=None,
                                 physical_package: dict | None = None,
                                 component_plan: dict | None = None,
                                 deps: list | None = None,
                                 validation_plan: dict | None = None,
                                 allow_unverified: bool = False,
                                 task_id: str = "",
                                 library_root: str = "") -> dict:
    """发布器件模型（调用方必须持有 manifest_lock）。"""
    """把一个已完成建模/验证的模型记录发布到器件模型库。

    ``record`` 是 device_models 的记录；``component_plan``（可放置元件的
    建图计划，含 pin_order 角色表）与 ``deps``（依赖文件清单，含源路径与
    SHA）由调用方（device_models.build_component_package）生成 —— 本模块
    只负责身份、暂存、原子入库、索引与状态判定。
    """
    if not library_root:
        raise DeviceLibraryError("library_root 未解析")
    root = library_root
    validation = record.get("validation") or {}
    verdict = str(validation.get("verdict") or "")
    reasons = []
    if verdict == "validated":
        status = STATUS_PUBLISHED
    elif allow_unverified:
        status = STATUS_CANDIDATE
        reasons.append("模型未达到 validated（verdict=" + (verdict or "无") + "），"
                       "按明确标注的候选模型发布；不得当作已验证版本使用。")
    else:
        raise DeviceLibraryError(
            "模型当前 verdict=" + (verdict or "无") +
            "，不能发布为已验证版本。可在验证通过后重新发布，"
            "或显式 allow_unverified=True 以候选（candidate）状态发布。")
    build = record.get("build") or {}
    if not build:
        raise DeviceLibraryError("模型记录没有建图计划；请先 build_device_model")
    if component_plan is None:
        raise DeviceLibraryError("缺少可放置元件的建图计划（component_plan）")

    device_id = device_identity(vendor, part_number)
    model_kind = str(record.get("model_kind") or "")
    configuration = str(record.get("configuration") or "")
    conditions = (record.get("curve_binding") or {}).get("conditions") or {}
    try:
        z0 = float(build.get("z0"))
    except (TypeError, ValueError):
        z0 = None
    model_id = model_identity(device_id, model_kind, configuration, conditions, z0)

    # ---- 不可变内容 → revision 身份（验证报告不属于模型内容） ----------
    deps_digest = [[str(d.get("relpath") or ""), str(d.get("sha256") or "")]
                   for d in (deps or [])]
    revision_payload = _norm_key({
        "schema": MANIFEST_SCHEMA, "builder": BUILDER_ID,
        "device": {"vendor": re.sub(r"\s+", " ", str(vendor or "").strip()).casefold(),
                   "part": _norm_part(part_number)},
        "model_kind": model_kind, "configuration": configuration,
        "conditions": _conditions_key(conditions), "z0_ohm": z0,
        "source_document_id": str(record.get("document_id") or ""),
        "component_plan": component_plan,
        "deps": deps_digest,
    })
    revision_id = "rev_" + _short_hash(revision_payload)
    final_dir = _revision_dir(root, device_id, model_id, revision_id)
    manifest_rel = os.path.join("devices", device_id, model_id,
                                "revisions", revision_id)

    reused = os.path.isfile(os.path.join(final_dir, "manifest.json"))
    staging_dir = None
    if not reused:
        staging_root = os.path.join(root, ".staging")
        os.makedirs(staging_root, exist_ok=True)
        staging_dir = tempfile.mkdtemp(prefix="pub_", dir=staging_root)
        try:
            manifest = _build_manifest(
                record, device_id, model_id, revision_id, vendor, part_number,
                display_name, aliases, physical_package, model_kind,
                configuration, conditions, z0, build, component_plan, deps,
                validation, status, reasons, task_id, verdict,
                validation_plan=validation_plan)
            _write_staging(staging_dir, manifest, component_plan, deps,
                           validation_plan, record=record)
            # manifest.json 先落进暂存目录，随目录一起原子改名 —— 入库后的
            # 修订目录从第一刻起就是完整的（没有"目录在了但清单没到"的中间态）。
            _write_json(os.path.join(staging_dir, "manifest.json"), manifest)
            os.makedirs(os.path.dirname(final_dir), exist_ok=True)
            if os.path.exists(final_dir):      # 并发发布同内容：已有人入库
                reused = True
            else:
                os.rename(staging_dir, final_dir)   # 同卷目录改名 = 原子入库
                staging_dir = None
        finally:
            if staging_dir and os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)

    manifest = _read_json(os.path.join(final_dir, "manifest.json"))
    # ---- 验证报告只追加，不覆盖（同一修订可补充更完整的验证）----------
    reports = manifest.get("validation_reports") or []
    report_entry = {"verdict": verdict, "mode": validation.get("validation_mode"),
                    "at": validation.get("validated_at"),
                    "simulation_id": validation.get("simulation_id"),
                    "scope_hz": validation.get("validation_scope_hz") or {}}
    if not reports or reports[-1] != report_entry:
        reports.append(report_entry)
        reports = reports[-_MAX_REVISION_REPORTS:]
    manifest["validation_reports"] = reports
    manifest["validation"] = validation
    if status == STATUS_PUBLISHED or manifest["status"] == STATUS_PUBLISHED:
        # 已发布过的修订不因一次候选级复验而降级；内容不可变，报告只追加。
        manifest["status"] = STATUS_PUBLISHED
        manifest["status_reasons"] = []
    else:
        manifest["status"] = status
        manifest["status_reasons"] = reasons
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        _norm_key(manifest).encode("utf-8")).hexdigest()
    manifest["published_at"] = manifest.get("published_at") or model_store.utc_now()
    _write_json(os.path.join(final_dir, "manifest.json"), manifest)
    _sync_model_and_device(root, manifest)

    # 索引更新在库锁内做；拿不到锁不回滚已入库内容（可 rebuild 恢复）
    index_note = ""
    lock = model_store.manifest_lock(root)
    if lock.acquire():
        try:
            _save_index_entry(root, manifest)
        finally:
            lock.release()
    else:
        index_note = "（索引锁忙，本次未更新索引；检索前请重建索引）"

    return {"ok": True, "reused": reused,
            "device_id": device_id, "model_id": model_id,
            "revision_id": revision_id, "status": manifest["status"],
            "status_reasons": manifest["status_reasons"],
            "library_root": os.path.dirname(root), "devices_root": root,
            "manifest_relpath": manifest_rel,
            "verdict": verdict, "tech_cell_name":
                (manifest.get("ads_library") or {}).get("tech_cell_name"),
            "note": ("同内容修订已存在，直接复用（未创建新版本）。" if reused else
                     "新修订已原子发布到器件模型库。") + index_note}


def _build_manifest(record, device_id, model_id, revision_id, vendor,
                    part_number, display_name, aliases, physical_package,
                    model_kind, configuration, conditions, z0, build,
                    component_plan, deps, validation, status, reasons,
                    task_id, verdict, validation_plan=None) -> dict:
    document_id = str(record.get("document_id") or "")
    evidence = {
        "document_id": document_id,
        "pdf_sha256": document_id,
        "parameter_set_id": record.get("parameter_set_id"),
        "parameter_review": record.get("parameter_review"),
        "parameter_attribution": record.get("parameter_attribution"),
        "curve_evidence": (record.get("curve_binding") or {}).get("evidence") or [],
        "curve_ids": record.get("curve_ids") or {},
        "curve_bands_hz": _curve_bands_hz(record),
    }
    try:
        import datasheet_models
        document = datasheet_models._read("documents", document_id)
        evidence["pdf_filename"] = (document.get("filename")
                                    or document.get("original_filename"))
        evidence["parse_engine"] = document.get("parse_engine")
        evidence["pdf_size_bytes"] = document.get("size_bytes")
    except Exception:  # noqa: BLE001 — 证据块的文件元数据缺失不阻断发布
        pass
    return {
        "schema": MANIFEST_SCHEMA,
        "builder": BUILDER_ID,
        "created_at": model_store.utc_now(),
        "identity": {"device_id": device_id, "model_id": model_id,
                     "revision_id": revision_id,
                     "source_record_id": record.get("model_id"),
                     "source_kind": "pdf_datasheet_model",
                     "task_id": str(task_id or "")},
        "device": {
            "vendor": re.sub(r"\s+", " ", str(vendor or "").strip()),
            "part_number": str(part_number or "").strip(),
            "display_name": (display_name or "").strip() or str(part_number).strip(),
            "aliases": [str(a).strip() for a in (aliases or []) if str(a).strip()],
            "device_kind": record.get("device_kind"),
            "physical_package": physical_package,
            "source_filename": evidence.get("pdf_filename"),
        },
        "model": {
            "kind": model_kind,
            "method": ("constrained_fit" if model_kind == "jfet_small_signal"
                       else "digitized_curves"),
            "configuration": configuration,
            "conditions": conditions,
            "z0_ohm": z0,
            "candidate": (record.get("fit") or {}).get("selected"),
            "parameters_source": build.get("parameters_source") or {},
        },
        "capabilities": {
            "supported": ["small_signal_ac_within_validated_band"],
            "unsupported": list(record.get("unsupported") or []),
            "note": "固定偏置小信号模型：只在已验证频段/条件内可信；"
                    "不支持的能力一律以 unsupported 为准，不得自动用于"
                    "直流偏置、噪声、非线性或温度扫描。",
        },
        "dependencies": [dict(d) for d in (deps or [])],
        "ads_library": {
            "tech_cell_name": str(component_plan.get("cell") or ""),
            "pins": [dict(p) for p in (component_plan.get("pin_order") or [])],
            "pin_mapping": build.get("pin_mapping") or {},
            "note": "ADS 符号端口号/终端角色是逻辑映射；物理封装脚号见 "
                    "pin_mapping.physical_package（须有资料依据，未证实为 null）。",
        },
        "evidence": evidence,
        "validation": validation,
        "validation_plan_sha256": (hashlib.sha256(_norm_key(validation_plan)
                                                  .encode("utf-8")).hexdigest()
                                   if validation_plan else None),
        "status": status,
        "status_reasons": reasons,
    }


_AXIS_HZ_FACTORS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}


def _curve_bands_hz(record: dict) -> dict:
    """从曲线记录读各量纲频段（单位换算按轴校准声明；供无验证结论时退回）。"""
    out = {}
    try:
        import datasheet_curves
        for kind in ("S", "Y"):
            pair = (record.get("curve_ids") or {}).get(kind) or {}
            for comp in ("mag", "phase"):
                ids = pair.get(comp)
                ids = ids if isinstance(ids, list) else ([ids] if ids else [])
                for curve_id in ids:
                    curve = datasheet_curves.get_curve(curve_id)
                    unit = str(((curve.get("axes") or {}).get("x") or {})
                               .get("unit") or "")
                    factor = _AXIS_HZ_FACTORS.get(unit)
                    if not factor:
                        continue
                    series = curve.get("series") or []
                    xs = [float(row[0]) for row in
                          (series[0].get("values") or []) if row]
                    if len(xs) < 2:
                        # series 为空时退回采样区间声明（同一轴单位）
                        sampling = curve.get("sampling") or {}
                        try:
                            xs = [float(sampling["start"]), float(sampling["stop"])]
                        except (KeyError, TypeError, ValueError):
                            continue
                    if len(xs) >= 2:
                        out.setdefault(kind, [min(xs) * factor, max(xs) * factor])
                if kind in out:
                    break
    except Exception:  # noqa: BLE001 — 频段缺失时使用电路会显式报缺
        pass
    return out


def _write_staging(staging_dir: str, manifest: dict, component_plan: dict,
                   deps: list, validation_plan: dict | None = None,
                   record: dict | None = None) -> None:
    """把修订内容写进暂存目录（manifest.json 由发布方在指纹后写回根目录）。

    归档目标是**自包含**：删掉上传缓存、迁移用户数据根、或验收数据根不可见
    之后，仅凭这个修订目录就能复算曲线、重建模型记录并做准确度复验。缺任何
    一项都必须当场失败，不能发布一个"看着完整、实际复验不了"的修订。
    """
    _write_json(os.path.join(staging_dir, "source", "evidence.json"),
                manifest.get("evidence") or {})
    # 原始资料随修订一起归档（PDF 按内容 SHA 存储于独立用户数据根；发布时
    # 复制原始字节进入模型库，因此删除上传缓存/迁移用户数据根后修订仍可审计）。
    document_id = str((manifest.get("evidence") or {}).get("document_id") or "")
    if document_id:
        try:
            import doc_assets
            pdf_bytes = doc_assets.read_bytes(document_id)
        except Exception as exc:  # noqa: BLE001 — 无源资料时不能伪称完整归档
            raise DeviceLibraryError(
                f"原始 PDF 资产不可用，无法发布可追溯修订：{type(exc).__name__}: {exc}") from exc
        if hashlib.sha256(pdf_bytes).hexdigest() != document_id:
            raise DeviceLibraryError("原始 PDF SHA-256 与 document_id 不一致，拒绝发布")
        _write_bytes(os.path.join(staging_dir, "source", "datasheet.pdf"), pdf_bytes)
    _write_json(os.path.join(staging_dir, "model", "component_plan.json"),
                {"plan": component_plan,
                 "note": "元件子电路计划：{MODEL_DIR} 占位符在导入时替换为"
                         "工作区依赖目录绝对路径。"})
    if validation_plan:
        _write_json(os.path.join(staging_dir, "validation", "validation_plan.json"),
                    {"plan": validation_plan,
                     "note": "验证电路计划（TermG 端口）；跨工作区复验时使用。"})
    for dep in deps or []:
        rel = str(dep.get("relpath") or "")
        src = str(dep.get("source_path") or "")
        if not rel or not src or not os.path.isfile(src):
            continue
        target = os.path.join(staging_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(src, target)
    _write_model_snapshot(staging_dir, record, manifest)


def _snapshot_json(base_dir: str, rel: str, payload) -> dict:
    """写一份不可变快照并返回它相对修订根的定位与摘要。"""
    path = os.path.join(base_dir, rel)
    _write_json(path, payload)
    return {"relpath": rel.replace("\\", "/"),
            "sha256": model_store.sha256_file(path)}


def _write_model_snapshot(staging_dir: str, record: dict | None,
                          manifest: dict) -> None:
    """归档建模记录、曲线与参数快照 —— 让修订能脱离源数据根复验。

    复验要读建模记录（端口映射/拟合参数/建图指纹）、曲线记录（手册数值）
    与参数记录（原文证据）。只归档 PDF 与元件计划不够：源数据根不可见时，
    这三样都会读不到，验证只能报"模型记录不存在"。
    """
    if not isinstance(record, dict) or not record.get("model_id"):
        # 没有源记录就说明该修订无法独立复验 —— 不能静默发布。
        raise DeviceLibraryError(
            "发布缺少源建模记录，无法归档可复验的修订；请从 device_models "
            "记录重新发布")
    import datasheet_curves
    import datasheet_models

    files = {}
    files["model_record"] = _snapshot_json(
        staging_dir, "snapshot/model_record.json", record)

    # 曲线记录（含数字化点与图框证据）按 curve_ids 全量快照
    curve_ids = record.get("curve_ids") or {}
    curve_files = {}
    for kind, pair in sorted(curve_ids.items()):
        if not isinstance(pair, dict):
            continue
        for component, curve_id in sorted(pair.items()):
            try:
                payload = datasheet_curves.get_curve(str(curve_id))
            except Exception as exc:  # noqa: BLE001 — 缺曲线即无法复验
                raise DeviceLibraryError(
                    f"曲线记录 {curve_id} 不可读，无法归档可复验修订："
                    f"{type(exc).__name__}: {exc}") from exc
            rel = f"snapshot/curves/{curve_id}.json"
            curve_files[f"{kind}.{component}"] = _snapshot_json(
                staging_dir, rel, payload)
    files["curves"] = curve_files

    parameter_set_id = str(record.get("parameter_set_id") or "")
    if parameter_set_id:
        try:
            params = datasheet_models._read("parameters", parameter_set_id)
        except Exception as exc:  # noqa: BLE001
            raise DeviceLibraryError(
                f"参数记录 {parameter_set_id} 不可读，无法归档可复验修订："
                f"{type(exc).__name__}: {exc}") from exc
        files["parameters"] = _snapshot_json(
            staging_dir, "snapshot/parameter_set.json", params)

    fit = record.get("fit") or {}
    if fit:
        files["fit"] = _snapshot_json(
            staging_dir, "snapshot/fit.json",
            {"selected": fit.get("selected"),
             "candidates": fit.get("candidates") or {},
             "rules": fit.get("rules") or {}})

    index = {"archive_version": ARCHIVE_VERSION,
             "source_record_id": record.get("model_id"),
             "document_id": record.get("document_id"),
             "part_number": record.get("part_number"),
             "configuration": record.get("configuration"),
             "files": files}
    _write_json(os.path.join(staging_dir, "snapshot", "snapshot_index.json"), index)
    # 清单里登记快照索引自身的指纹，读取时先核索引再核各文件
    manifest.setdefault("archive", {})
    manifest["archive"] = {
        "archive_version": ARCHIVE_VERSION,
        "snapshot_index_relpath": "snapshot/snapshot_index.json",
        "snapshot_index_sha256": model_store.sha256_file(
            os.path.join(staging_dir, "snapshot", "snapshot_index.json")),
        "complete": True,
    }


def _load_snapshot(base: str, manifest: dict) -> tuple[dict, list]:
    """读取修订自带快照；返回 (快照内容, 问题列表)。旧修订标为不完整。"""
    archive = manifest.get("archive") or {}
    if not archive:
        return {}, ["该修订是归档格式 1（未自带建模记录/曲线快照），"
                    "只能依赖源数据根复验；如需独立复验请发布新修订"]
    rel = str(archive.get("snapshot_index_relpath") or "")
    expect = str(archive.get("snapshot_index_sha256") or "")
    path = os.path.join(base, rel) if rel else ""
    if not rel or not os.path.isfile(path):
        return {}, [f"快照索引缺失：{rel or '(未登记)'}"]
    if expect and model_store.sha256_file(path) != expect:
        return {}, ["快照索引指纹不符（可能被改动）"]
    problems = []
    index = _read_json(path)
    snapshot = {}
    for name, entry in (index.get("files") or {}).items():
        if name == "curves":
            snapshot["curves"] = {}
            for key, item in (entry or {}).items():
                payload, issues = _snapshot_file(base, item)
                problems.extend(issues)
                if payload is not None:
                    snapshot["curves"][key] = payload
            continue
        payload, issues = _snapshot_file(base, entry)
        problems.extend(issues)
        if payload is not None:
            snapshot[name] = payload
    return snapshot, problems


def _snapshot_file(base: str, entry: dict) -> tuple[dict | None, list]:
    rel = str((entry or {}).get("relpath") or "")
    expect = str((entry or {}).get("sha256") or "")
    if not rel:
        return None, ["快照条目缺少 relpath"]
    path = os.path.join(base, rel)
    if not os.path.isfile(path):
        return None, [f"快照文件缺失：{rel}"]
    if expect and model_store.sha256_file(path) != expect:
        return None, [f"快照文件指纹不符（可能被改动）：{rel}"]
    try:
        return _read_json(path), []
    except (OSError, ValueError) as exc:
        return None, [f"快照文件不可解析：{rel}（{exc}）"]


def _sync_model_and_device(root: str, manifest: dict) -> None:
    identity = manifest.get("identity") or {}
    model_dir = os.path.join(root, identity["device_id"], identity["model_id"])
    rev_dir = os.path.join(model_dir, "revisions")
    _write_json(os.path.join(model_dir, "model.json"), {
        "schema": SCHEMA, "model_id": identity["model_id"],
        "device_id": identity["device_id"], "kind": manifest["model"]["kind"],
        "configuration": manifest["model"]["configuration"],
        "conditions": manifest["model"]["conditions"],
        "z0_ohm": manifest["model"]["z0_ohm"],
        "revisions": sorted(os.listdir(rev_dir)) if os.path.isdir(rev_dir) else [],
        "updated_at": model_store.utc_now()})
    _write_json(os.path.join(root, identity["device_id"], "device.json"), {
        "schema": SCHEMA, "device_id": identity["device_id"],
        "vendor": manifest["device"]["vendor"],
        "part_number": manifest["device"]["part_number"],
        "display_name": manifest["device"]["display_name"],
        "aliases": manifest["device"]["aliases"],
        "device_kind": manifest["device"]["device_kind"],
        "updated_at": model_store.utc_now()})


# ---------------------------------------------------------------------------
# 文件完整性
# ---------------------------------------------------------------------------

def verify_revision_files(root: str, manifest: dict) -> list:
    """核对清单里登记的依赖文件与归档快照指纹；返回问题列表（空 = 通过）。"""
    problems = []
    identity = manifest.get("identity") or {}
    base = _revision_dir(root, identity.get("device_id"), identity.get("model_id"),
                         identity.get("revision_id"))
    document_id = str((manifest.get("evidence") or {}).get("document_id") or "")
    if document_id:
        source_pdf = os.path.join(base, "source", "datasheet.pdf")
        if not os.path.isfile(source_pdf):
            problems.append("原始 PDF 归档缺失：source/datasheet.pdf")
        elif model_store.sha256_file(source_pdf) != document_id:
            problems.append("原始 PDF 指纹不符（可能被替换）：source/datasheet.pdf")
    for dep in manifest.get("dependencies") or []:
        rel = str(dep.get("relpath") or "")
        expect = str(dep.get("sha256") or "")
        path = os.path.join(base, rel)
        if not rel or not os.path.isfile(path):
            problems.append(f"依赖文件缺失：{rel}")
            continue
        if expect and model_store.sha256_file(path) != expect:
            problems.append(f"依赖文件指纹不符（可能被改动）：{rel}")
    if (manifest.get("archive") or {}).get("archive_version"):
        _snapshot, issues = _load_snapshot(base, manifest)
        problems.extend(issues)
    return problems


def revision_archive_state(root: str, manifest: dict) -> dict:
    """修订归档的可复验性：能否脱离源数据根重建模型与证据。"""
    identity = manifest.get("identity") or {}
    base = _revision_dir(root, identity.get("device_id"), identity.get("model_id"),
                         identity.get("revision_id"))
    snapshot, problems = _load_snapshot(base, manifest)
    archive = manifest.get("archive") or {}
    return {"archive_version": archive.get("archive_version"),
            "self_contained": bool(snapshot.get("model_record"))
            and not problems,
            "has_model_record": bool(snapshot.get("model_record")),
            "curve_records": sorted((snapshot.get("curves") or {})),
            "has_parameter_set": bool(snapshot.get("parameters")),
            "has_fit": bool(snapshot.get("fit")),
            "problems": problems}


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

def search_registry(root: str, args: dict) -> dict:
    index = load_index(root)
    entries = index.get("entries") or []
    query = str(args.get("query") or "").strip().casefold()
    part = _norm_part(args.get("part") or "")
    vendor = str(args.get("vendor") or "").strip().casefold()
    status = str(args.get("status") or "").strip()
    verdict = str(args.get("verdict") or "").strip()
    kind = str(args.get("device_kind") or "").strip()
    model_kind = str(args.get("model_kind") or "").strip()
    configuration = str(args.get("configuration") or "").strip()
    capability = str(args.get("capability") or "").strip().casefold()
    band_start = args.get("band_start_hz")
    band_stop = args.get("band_stop_hz")

    def band_covers(entry) -> bool:
        span = entry.get("validated_band_hz")
        if not span:
            return False
        try:
            lo, hi = float(span[0]), float(span[1])
        except (TypeError, ValueError):
            return False
        if band_start is not None and float(band_start) < lo - 1e-6:
            return False
        if band_stop is not None and float(band_stop) > hi + 1e-6:
            return False
        return True

    matched = []
    for entry in entries:
        if part and _norm_part(entry.get("part") or "") != part:
            continue
        if vendor and vendor not in str(entry.get("vendor") or "").casefold():
            continue
        if status and entry.get("status") != status:
            continue
        if verdict and entry.get("verdict") != verdict:
            continue
        if kind and entry.get("device_kind") != kind:
            continue
        if model_kind and entry.get("model_kind") != model_kind:
            continue
        if configuration and entry.get("configuration") != configuration:
            continue
        if capability and capability in _norm_key(
                entry.get("capabilities_unsupported") or []).casefold():
            continue     # 明确不支持的能力被点名排除
        if (band_start is not None or band_stop is not None) \
                and not band_covers(entry):
            continue     # 验证频段不覆盖目标频段 → 不命中（不静默外推）
        if query:
            hay = " ".join(str(entry.get(k) or "") for k in
                           ("part", "vendor", "display_name")
                           + tuple(entry.get("aliases") or [])).casefold()
            if query not in hay:
                continue
        matched.append(entry)
    try:
        limit = max(1, min(int(args.get("max_items") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    total = len(matched)
    return {"entries": matched[:limit], "total": total,
            "returned": min(limit, total), "index_updated_at": index.get("updated_at")}


def resolve_revision(root: str, device_id: str, model_id: str,
                     revision_id: str = "") -> dict:
    """选定修订：显式 revision_id 优先；否则 published 优先、同状态取最新。"""
    model_dir = os.path.join(root, device_id, model_id, "revisions")
    if not os.path.isdir(model_dir):
        raise DeviceLibraryError("器件模型库中没有该模型；请先检索确认标识")
    available = {}
    for rev in os.listdir(model_dir):
        path = os.path.join(model_dir, rev, "manifest.json")
        if not os.path.isfile(path):
            continue
        try:
            available[rev] = _read_json(path)
        except (OSError, ValueError):
            continue
    if not available:
        raise DeviceLibraryError("该模型没有任何已发布修订")
    if revision_id:
        manifest = available.get(revision_id)
        if manifest is None:
            raise DeviceLibraryError(
                f"修订 {revision_id} 不存在；可用：{sorted(available)}")
        return manifest
    ranked = sorted(available.items(),
                    key=lambda kv: (kv[1].get("status") == STATUS_PUBLISHED,
                                    kv[1].get("published_at") or ""),
                    reverse=True)
    return ranked[0][1]


# ---------------------------------------------------------------------------
# 跨工作区导入与放置（ADS 侧操作经注入的 call_tool 执行）
# ---------------------------------------------------------------------------

def workspace_import_root(workspace: str) -> str:
    return os.path.join(model_store.store_root(workspace), "devices")


def _import_record_path(workspace: str) -> str:
    return os.path.join(model_store.store_root(workspace), "device_imports.json")


def load_imports(workspace: str) -> dict:
    path = _import_record_path(workspace)
    try:
        data = _read_json(path)
        if isinstance(data.get("revisions"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "revisions": {}}


def save_imports(workspace: str, data: dict) -> None:
    _write_json(_import_record_path(workspace), data)


def substitute_model_dir(plan: dict, model_dir: str) -> dict:
    """把计划里的 ``{MODEL_DIR}`` 占位符替换为工作区依赖目录的绝对路径。"""
    text = json.dumps(plan, ensure_ascii=False)
    text = text.replace("{MODEL_DIR}", model_dir.replace("\\", "\\\\"))
    return json.loads(text)


def _looks_like_exists_error(message: str) -> bool:
    lowered = str(message).casefold()
    return any(k in lowered for k in ("已存在", "exists", "already"))


def import_revision(root: str, workspace: str, manifest: dict, *,
                    call_tool, library: str = "", cell: str = "",
                    force_rebuild: bool = False) -> dict:
    """把指定修订导入当前工作区：复制依赖 → 建元件 cell → 登记。

    * 幂等：同一修订已导入本工作区且清单指纹一致 → 直接复用既有 cell；
    * 中央库升级/移动不影响既有工程：依赖**复制**进工作区；
    * 目标名字被其它内容占用时自动换名重试，绝不覆盖（登记实际名字）。
    """
    identity = manifest.get("identity") or {}
    revision_id = identity["revision_id"]
    problems = verify_revision_files(root, manifest)
    if problems:
        raise DeviceLibraryError("修订文件完整性校验失败：" + "；".join(problems))

    imports = load_imports(workspace)
    existing = imports["revisions"].get(revision_id) or {}
    if existing and existing.get("manifest_sha256") == manifest.get("manifest_sha256") \
            and not force_rebuild:
        return {"ok": True, "reused": True, **existing,
                "note": "该修订已导入当前工作区，直接复用（未重复建 cell）。"}

    # ---- 依赖复制进工作区 ---------------------------------------------
    base = _revision_dir(root, identity["device_id"], identity["model_id"],
                         revision_id)
    dep_dir = os.path.join(workspace_import_root(workspace), revision_id, "deps")
    copied = []
    for dep in manifest.get("dependencies") or []:
        rel = str(dep.get("relpath") or "")
        if not rel:
            continue
        target = os.path.join(dep_dir, os.path.basename(rel))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(base, rel), target)
        copied.append({"relpath": rel, "workspace_path": target})

    # ---- 元件 cell -----------------------------------------------------
    plan_payload = _read_json(os.path.join(base, "model", "component_plan.json"))
    plan = substitute_model_dir(plan_payload.get("plan") or {}, dep_dir)
    tech = str(cell or "").strip() or str(
        (manifest.get("ads_library") or {}).get("tech_cell_name") or "")
    if not _TECH_NAME.fullmatch(tech):
        raise DeviceLibraryError(
            f"元件名 {tech!r} 不是合法 ADS 技术名称"
            "（字母开头，仅字母数字下划线，≤64 字符）")
    target_library = str(library or "").strip()
    if not target_library:
        info = call_tool("get_workspace_info", {})
        writable = [str(x) for x in (info.get("writable_libraries") or [])
                    if str(x).strip()]
        if not writable:
            raise DeviceLibraryError("当前工作区没有可写库；请先打开工作区")
        target_library = writable[0]

    attempts = [tech] + [f"{tech[:56]}_r{i}" for i in range(2, 6)]
    last_error = None
    used_cell = None
    for candidate in attempts:
        args = dict(plan)
        args["library"] = target_library
        args["cell"] = candidate
        args["recreate"] = bool(force_rebuild)
        try:
            result = call_tool("build_schematic", args)
        except Exception as exc:  # noqa: BLE001 — ADS 侧错误统一处理
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if result.get("error"):
            last_error = str(result["error"])
            # 名字被占用（其它内容）→ 换名重试；其它错误直接失败
            if not _looks_like_exists_error(last_error):
                raise DeviceLibraryError("元件 cell 构建失败：" + last_error)
            continue
        used_cell = candidate
        break
    if not used_cell:
        raise DeviceLibraryError("元件 cell 构建失败：" + str(last_error))

    # ---- symbol 视图（真实可放置元件的必要条件）-------------------------
    # 只用 ADS 自己读到的原理图端子生成；生成失败则整次导入失败 ——
    # 没有 symbol 的 cell 只能被层次调用，不能从元件列表放置。
    symbol_result = None
    try:
        symbol_result = call_tool("create_component_symbol", {
            "library": target_library, "cell": used_cell,
            "replace": bool(force_rebuild)})
    except Exception as exc:  # noqa: BLE001 — ADS 侧错误统一处理
        raise DeviceLibraryError(
            f"元件 symbol 生成失败（cell={used_cell}）：{type(exc).__name__}: {exc}")
    if symbol_result.get("error"):
        raise DeviceLibraryError(
            "元件 symbol 生成失败：" + str(symbol_result["error"]))
    if not symbol_result.get("ok"):
        raise DeviceLibraryError(
            "元件 symbol 生成未通过复核：" +
            "；".join(symbol_result.get("problems") or ["未返回成功状态"]))
    symbol_pins = symbol_result.get("pins") or []

    record = {
        "revision_id": revision_id,
        "device_id": identity["device_id"],
        "model_id": identity["model_id"],
        "part": (manifest.get("device") or {}).get("part_number"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "library": target_library, "cell": used_cell,
        "cell_name_requested": tech,
        "symbol": {"view": symbol_result.get("symbol_view"),
                   "pins": symbol_pins,
                   "created": bool(symbol_result.get("symbol_created"))},
        "deps": copied, "imported_at": model_store.utc_now(),
        "workspace": workspace,
        "status": manifest.get("status"),
        "verdict": (manifest.get("validation") or {}).get("verdict"),
    }
    imports["revisions"][revision_id] = record
    save_imports(workspace, imports)
    note = (f"元件已导入：{target_library}:{used_cell}（含 symbol 视图，"
            f"{len(symbol_pins)} 个引脚）")
    if used_cell != tech:
        note = (f"元件名与请求不同（原名被占用，未覆盖）：{tech} → {used_cell}；"
                f"已生成 symbol 视图")
    return {"ok": True, "reused": False, **record, "note": note}


def place_component(root: str, workspace: str, manifest: dict,
                    import_record: dict, *, call_tool, cell: str = "",
                    band_hz=None) -> dict:
    """把已导入的元件放进一张使用电路（TermG + S 参数控制器）。

    仿真必须由聊天编排层随后调用标准 run_simulation；本模块不直接触发仿真，
    以确保可信 simulation_id、工作区/建图指纹与数据集 SHA 正常落盘。
    """
    identity = manifest.get("identity") or {}
    revision_id = identity["revision_id"]
    component_library = import_record.get("library")
    component_cell = import_record.get("cell")
    if not component_library or not component_cell:
        raise DeviceLibraryError("该修订尚未导入当前工作区；请先导入")
    base = _revision_dir(root, identity["device_id"], identity["model_id"],
                         revision_id)
    plan_payload = _read_json(os.path.join(base, "model", "component_plan.json"))
    plan = plan_payload.get("plan") or {}
    usage = usage_plan_from(manifest, component_library, component_cell,
                            band_hz=band_hz)
    band = usage.pop("_band_hz")
    band_note = usage.pop("_band_note")
    usage_cell = str(cell or "").strip() or f"{component_cell[:56]}_use"
    attempts = [usage_cell] + [f"{usage_cell[:54]}_{i}" for i in range(2, 6)]
    last_error = None
    used = None
    for candidate in attempts:
        args = dict(usage)
        args["library"] = component_library
        args["cell"] = candidate
        args["recreate"] = False
        try:
            result = call_tool("build_schematic", args)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if result.get("error"):
            last_error = str(result["error"])
            if not _looks_like_exists_error(last_error):
                raise DeviceLibraryError("使用电路构建失败：" + last_error)
            continue
        used = candidate
        break
    if not used:
        raise DeviceLibraryError("使用电路构建失败：" + str(last_error))

    # 放置历史只追加（保留既往，不覆盖）
    imports = load_imports(workspace)
    record = imports["revisions"].setdefault(revision_id, {})
    history = record.setdefault("placements", [])
    history.append({"cell": used, "at": model_store.utc_now(),
                    "simulated": False,
                    # 频段随放置一起登记：复验时用它核对数据集频段，
                    # 免得事后靠"当前清单"反推当时建的是哪一段。
                    "band_hz": [float(band[0]), float(band[1])],
                    "band_note": band_note,
                    "requested_band_hz": usage.get("requested_band_hz"),
                    "validated_band_hz": usage.get("validated_band_hz"),
                    "validation_status": usage.get("validation_status")})
    record["placements"] = history[-20:]
    save_imports(workspace, imports)
    return {"ok": True, "library": component_library, "cell": used,
            "component": f"{component_library}:{component_cell}",
            "band_hz": band, "band_note": band_note,
            "requested_band_hz": usage.get("requested_band_hz"),
            "validated_band_hz": usage.get("validated_band_hz"),
            "validation_status": usage.get("validation_status"),
            "next_step": "请通过标准 run_simulation 工具运行该 cell；这样仿真来源、"
                         "建图指纹与数据集 SHA 会被可信执行记录绑定。"}


def usage_plan_from(manifest: dict, library: str, component_cell: str,
                    band_hz=None) -> dict:
    """从清单派生使用电路计划（TermG + 被测元件 + S 参数控制器）。"""
    ads = manifest.get("ads_library") or {}
    pins = ads.get("pins") or []
    if not pins:
        raise DeviceLibraryError("清单缺少引脚映射，无法派生使用电路")
    model = manifest.get("model") or {}
    z0 = model.get("z0_ohm") or 50.0
    pin_by_role = {str(p.get("role")): int(p.get("num")) for p in pins}
    port1_pin = pin_by_role.get("port1")
    port2_pin = pin_by_role.get("port2")
    common_pin = pin_by_role.get("common")
    if not port1_pin or not port2_pin:
        raise DeviceLibraryError("清单引脚映射缺少 port1/port2 角色")
    master = f"{library}:{component_cell}"
    fmt = lambda v: f"{float(v):.12g} Ohm"  # noqa: E731
    instances = [
        {"master": "ads_simulation:TermG", "name": "P1",
         "params": {"Num": "1", "Z": fmt(z0)}},
        {"master": master, "name": "DEVICE", "params": {},
         # 层次子电路实例的引脚数必须显式给：netlist_check 只能从 master 名
         # 推断内置符号的脚数，推断不到时默认 2 脚 —— 三端元件会被误判成
         # 少一脚，网表划分核对必然失败。
         "_npins": len(pins)},
        {"master": "ads_simulation:TermG", "name": "P2",
         "params": {"Num": "2", "Z": fmt(z0)}},
    ]
    connections = [{"a": ["P1", 1], "b": ["DEVICE", port1_pin]},
                   {"a": ["DEVICE", port2_pin], "b": ["P2", 1]}]
    if common_pin:
        instances.append({"master": "ads_rflib:GROUND", "name": "COMMONGND"})
        connections.append({"a": ["DEVICE", common_pin], "b": ["COMMONGND", 1]})
    validation = manifest.get("validation") or {}
    validation_status = str(validation.get("verdict") or "unvalidated")
    validated_band = _validated_band(manifest) if validation_status in {
        "validated", "passed", "pass"} else None
    source_curve_band = _curve_band(manifest)
    requested_band = list(band_hz) if band_hz else None
    band = requested_band or validated_band or source_curve_band
    if requested_band:
        band_note = ("使用模型已验证频段" if validated_band and
                     requested_band[0] >= validated_band[0] and
                     requested_band[1] <= validated_band[1]
                     else "请求频段未验证，需按需复验")
    elif validated_band:
        band_note = "使用模型已验证频段"
    else:
        band_note = "使用曲线覆盖频段（未验证，需按需复验）"
    if not band:
        raise DeviceLibraryError("清单里没有可用频段（既无验证范围也无曲线范围）；"
                                 "请显式给 band_hz 后重试")
    step = max((float(band[1]) - float(band[0])) / 200.0, 1.0)
    instances.append({"master": "ads_simulation:S_Param", "name": "SP1", "params": {
        "Start": f"{float(band[0]):.12g} Hz", "Stop": f"{float(band[1]):.12g} Hz",
        "Step": f"{step:.12g} Hz",
        "CalcS": "yes", "CalcY": "yes", "CalcNoise": "No"}})
    return {"library": library, "cell": "", "layout": "auto", "recreate": False,
            "instances": instances, "connections": connections,
            "requested_band_hz": requested_band,
            "source_curve_band_hz": source_curve_band,
            "validated_band_hz": validated_band,
            "validation_status": validation_status,
            "_band_hz": band, "_band_note": band_note}


def _curve_band(manifest: dict):
    bands = ((manifest.get("evidence") or {}).get("curve_bands_hz") or {})
    spans = [v for v in bands.values() if isinstance(v, list) and len(v) == 2]
    if not spans:
        return None
    return [min(b[0] for b in spans), max(b[1] for b in spans)]
