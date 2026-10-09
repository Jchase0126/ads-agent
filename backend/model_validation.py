# -*- coding: utf-8 -*-
"""模型验证**证据**：把"验证到哪一步"记成可唯一追溯的一条记录。

为什么单开这个文件（不塞进 model_store / manifest）
--------------------------------------------------
1. **形状不同**：清单里的 ``validation`` 是 ``{库:cell: {ok, ...}}`` 的一层字典，
   它回答不了"哪个模型文件、哪组参数、跑到哪一步"。把阶段与指纹并到那一层里，
   要么字段不断膨胀，要么每次改都重写几 MB 的 manifest.json。
2. **追溯单元不同**：证据的唯一键是
   ``workspace + package_id + model_id + library:cell + 变体 + 参数指纹 + 模型文件指纹``。
   同一型号的不同**变体**（偏压 / 封装 / 温度 / 频段）是不同器件，必须各记一条；
   模型文件内容或参数一变，同一变体的**旧证据即失效**（保留但标 stale，不删）。
3. **不删资产**：本模块只写 ``validation/evidence.json``，从不改动 manifest 与
   archives/extracted —— 迁移旧清单也是**只读翻译**，旧字段一条都不删。

判定纪律（与界面文案同一份口径，别处不要再写一套）
--------------------------------------------------
* ``ok=True`` 的静态检查**不等于** sim 通过；缺 model_def、放不进电路、要求
  仿真却没跑、用户取消，一律不得记为 ``simulated``。
* 单元件通过**不暗示整包已验证**：包级只能报"已验证 N / 共 M 个模型"。
* 没有指纹可比对的东西（旧清单迁移来的）记作 ``unknown`` 而非 ``pass`` ——
  无法核对的证据不能拿来放行。
"""

import json
import os
import threading

import model_store

SCHEMA = 1
EVIDENCE_DIRNAME = "validation"
EVIDENCE_NAME = "evidence.json"
EVIDENCE_BACKUP = "evidence.json.bak"

# 阶段顺序即"验证深度"：后一层通过必须以前一层通过为前提。
# booted / listed 夹在 parsed 与 placed 之间：Design Kit 导入后先"库已加载启动
# 配置"（booted，eesof_lib.cfg → boot.ael → palette.ael 真的被 DE 加载），
# 再"原生元件列表里能看见分类"（listed）。**两者都不是仿真通过** ——
# 没放进电路、没跑过 hpeesofsim，就不能拿它们当"可用"。
STAGES = ("saved", "extracted", "parsed", "booted", "listed", "placed",
          "simulated")
STAGE_LABELS = {
    "saved": "已保存",
    "extracted": "已解压",
    "parsed": "静态可解析",
    "booted": "库已加载启动配置",
    "listed": "原生列表可见",
    "placed": "已放置",
    "simulated": "已仿真",
}

STATUS_NOT_CHECKED = "not_checked"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

STATUS_LABELS = {
    STATUS_NOT_CHECKED: "未检查",
    STATUS_PASS: "通过",
    STATUS_FAIL: "失败",
    STATUS_SKIPPED: "跳过",
    STATUS_CANCELLED: "已取消",
    STATUS_UNKNOWN: "无法核对",
}

OVERALL_LABELS = {
    "not_verified": "未验证",
    "saved": "仅已保存",
    "extracted": "仅已解压",
    "parsed": "仅静态可解析",
    "booted": "仅库已加载启动配置（未仿真）",
    "listed": "仅原生列表可见（未仿真）",
    "placed": "仅已放置（未仿真）",
    "simulated": "已仿真验证",
    "failed": "验证失败",
    "cancelled": "已取消",
}

# 「合成名次」：放在界面上就是一句话，必须与 put_stage 的取值一致，
# 不允许别处另写一句更漂亮的话 —— 文案与判定分家就是误导的开始。
overall_to_text = OVERALL_LABELS

_locks_guard = threading.Lock()
_locks: dict = {}


def _lock_for(path: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _locks[path] = lock
        return lock


class ModelValidationError(RuntimeError):
    """证据存储的可预期错误（如实转成工具结果，不冒充成功）。"""


# ---------------------------------------------------------------------------
# 存储：validation/evidence.json（与 manifest 同一把跨进程锁）
# ---------------------------------------------------------------------------

def evidence_dir(root: str) -> str:
    return os.path.join(str(root), EVIDENCE_DIRNAME)


def evidence_path(root: str) -> str:
    return os.path.join(evidence_dir(root), EVIDENCE_NAME)


def _empty() -> dict:
    return {"schema": SCHEMA, "entries": {}, "updated_at": "", "created_at": ""}


def load_evidence(root: str) -> dict:
    """读证据文件。缺失/损坏返回空结构 —— 证据丢了要重新验证，
    不能让调用方以为"没有证据 = 全部未验证"。
    """
    path = evidence_path(root)
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate, "rb") as stream:
                raw = stream.read()
        except OSError:
            continue
        if not raw.strip():
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            data.setdefault("schema", SCHEMA)
            return data
    return _empty()


def _atomic_write(path: str, content: bytes) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "wb") as stream:
        stream.write(content)
    os.replace(tmp, path)


def _save_evidence(root: str, data: dict) -> dict:
    """原子写证据文件。**用 manifest 同一把锁**：两个进程同时写破坏的不只是
    并发窗口，还有"先写的那份 evdience 被整个覆盖掉"—— 等价于验证记录凭空消失。
    """
    data = dict(data)
    data["schema"] = SCHEMA
    data["updated_at"] = model_store.utc_now()
    data.setdefault("created_at", data["updated_at"])
    path = evidence_path(root)
    lock = model_store.manifest_lock(root)
    got = lock.acquire()
    try:
        try:
            previous = load_evidence(root)
            if previous.get("entries"):
                _atomic_write(path + ".bak",
                              json.dumps(previous, ensure_ascii=False,
                                         indent=1).encode("utf-8"))
        except OSError:
            pass
        _atomic_write(path, json.dumps(data, ensure_ascii=False,
                                       indent=1).encode("utf-8"))
    finally:
        if got:
            lock.release()
    return data


# ---------------------------------------------------------------------------
# 指纹与标识
# ---------------------------------------------------------------------------

def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_id_for(package_id: str, relpath: str) -> str:
    """一条模型索引的稳定 ID。

    用 ``package_id + 相对路径`` 的内容哈希而不是序号：重新解压 / 重新建索引
    后 ID 不变（路径变才是真的换了对象），也不会因为列表重排而串号。
    """
    key = f"{str(package_id or '').strip()}|{str(relpath or '').strip()}"
    return "m" + _sha256_text(key)[:12]


def params_fingerprint(parameters: dict) -> str:
    """参数指纹：键排序后序列化再哈希（dict 顺序不稳定，直接 str() 会漂）。"""
    if not isinstance(parameters, dict) or not parameters:
        return ""
    canonical = json.dumps({str(k): str(v) for k, v in sorted(parameters.items())},
                           ensure_ascii=False, sort_keys=True)
    return _sha256_text(canonical)[:16]


def file_fingerprint(workspace: str, path: str,
                     read_limit: int = 8 * 1024 * 1024) -> dict:
    """模型文件的指纹与可信元数据。

    ``path`` 可以是相对工作区的路径（清单里存的就是这种），也可以是绝对路径。
    ``exists=False`` 时 sha 为空 —— 「不存在的文件的哈希」必须为空，
    不能拿上一次的值顶替。
    """
    out = {"relpath": "", "abs_path": "", "exists": False, "sha256": "",
           "size_bytes": None, "modified_at": "", "error": ""}
    raw = str(path or "").strip()
    if not raw:
        out["error"] = "未给出模型文件路径，无法计算指纹"
        return out
    abs_path = raw if os.path.isabs(raw) else os.path.join(str(workspace or ""), raw)
    abs_path = os.path.normpath(abs_path)
    out["abs_path"] = abs_path
    try:
        rel = os.path.relpath(abs_path, os.path.normpath(str(workspace))).replace(os.sep, "/")
        out["relpath"] = "" if rel.startswith("..") else rel
    except ValueError:      # 不同盘符时 relpath 抛错 —— 记绝对路径即可
        out["relpath"] = ""
    try:
        stat = os.stat(abs_path)
    except OSError as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["exists"] = True
    out["size_bytes"] = int(stat.st_size)
    out["modified_at"] = model_store.utc_now()
    try:
        out["sha256"] = model_store.sha256_file(abs_path)
    except OSError as e:
        out["error"] = f"读取文件失败: {type(e).__name__}: {e}"
    return out


def subject_key(workspace: str, package_id: str, model_id: str,
                model_ref: str, variant: dict) -> str:
    """证据主体（不含内容与参数）—— 同一主体的不同内容/参数版本互相挤掉。"""
    canonical = json.dumps({
        "workspace": _norm_ws(workspace),
        "package_id": str(package_id or ""),
        "model_id": str(model_id or ""),
        "model_ref": str(model_ref or ""),
        "variant": variant_fingerprint(variant),
    }, ensure_ascii=False, sort_keys=True)
    return _sha256_text(canonical)[:16]


def variant_fingerprint(variant: dict) -> str:
    """变体指纹：偏压 / 封装 / 温度等区分键。

    空变体返回 "" —— 没声明变体不等于"通用"，只是没区分；调用方要据此
    提醒用户"同一型号若有多个变体需分别验证"。
    """
    if not isinstance(variant, dict) or not variant:
        return ""
    canonical = json.dumps({str(k): str(v) for k, v in sorted(variant.items())},
                           ensure_ascii=False, sort_keys=True)
    return _sha256_text(canonical)[:12]


def _norm_ws(workspace: str) -> str:
    return os.path.normcase(os.path.normpath(str(workspace or "")))


def evidence_id_for(workspace: str, package_id: str, model_id: str,
                    model_ref: str, variant: dict, params_fp: str,
                    file_fp: str) -> str:
    """一条证据的 ID：主体 + 参数指纹 + 文件指纹三者共同决定。"""
    canonical = json.dumps({
        "subject": subject_key(workspace, package_id, model_id, model_ref, variant),
        "params": params_fp,
        "file": file_fp,
    }, ensure_ascii=False, sort_keys=True)
    return "ev" + _sha256_text(canonical)[:16]


# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------

def blank_stage(status: str = STATUS_NOT_CHECKED, detail: str = "") -> dict:
    return {"status": status, "at": "", "detail": detail, "artifacts": []}


def blank_stages() -> dict:
    return {stage: blank_stage() for stage in STAGES}


def put_stage(stages: dict, stage: str, status: str, detail: str = "",
              artifacts=None, at: str = "") -> dict:
    """写一个阶段。只更新**显式给出**的阶段，不抹掉之前的结果。

    为什么要这么严：`validate_model_import` 不会一次触达所有阶段（静态校验
    不放置、不仿真），整段覆盖会把上一轮的"已仿真"冲成"未检查"。
    """
    if stage not in STAGES:
        raise ModelValidationError(
            f"未知阶段 {stage!r}（可用: {', '.join(STAGES)}）")
    if status not in STATUS_LABELS:
        raise ModelValidationError(
            f"未知阶段状态 {status!r}（可用: {', '.join(STATUS_LABELS)}）")
    entry = dict(stages.get(stage) or blank_stage())
    entry["status"] = status
    if detail:
        entry["detail"] = detail
    if artifacts:
        merged = list(entry.get("artifacts") or [])
        for item in artifacts:
            text = str(item)
            if text not in merged:
                merged.append(text)
        entry["artifacts"] = merged
    entry["at"] = at or model_store.utc_now()
    stages[stage] = entry
    # 前序阶段必须已通过才能声称后序通过 —— 没解压就说"已仿真"是编故事
    if status == STATUS_PASS:
        idx = STAGES.index(stage)
        for prior in STAGES[:idx]:
            prior_entry = dict(stages.get(prior) or blank_stage())
            if prior_entry.get("status") in (STATUS_NOT_CHECKED, STATUS_UNKNOWN):
                prior_entry["status"] = STATUS_UNKNOWN
                prior_entry["detail"] = (
                    prior_entry.get("detail")
                    or f"后序阶段（{STAGE_LABELS[stage]}）已通过，但本阶段没有留下记录 —— 不能反推为通过")
                stages[prior] = prior_entry
    return stages


def overall_from_stages(stages: dict, simulation_required: bool = False) -> dict:
    """从阶段表算出总判定 + 未验证项列表（界面与后端判定共用这一个口径）。"""
    highest = ""
    failed = []
    cancelled = []
    unknown = []
    for stage in STAGES:
        entry = stages.get(stage) or blank_stage()
        status = entry.get("status") or STATUS_NOT_CHECKED
        if status == STATUS_PASS:
            highest = stage
        elif status == STATUS_FAIL:
            failed.append(stage)
        elif status == STATUS_CANCELLED:
            cancelled.append(stage)
        elif status == STATUS_UNKNOWN:
            unknown.append(stage)

    unverified: list = []
    for stage in unknown:
        unverified.append(f"{STAGE_LABELS[stage]}：无记录可核对")
    for stage in failed:
        unverified.append(f"{STAGE_LABELS[stage]}：{stages[stage].get('detail') or '失败'}")

    if failed:
        overall = "failed"
    elif simulation_required and highest != "simulated":
        if cancelled:
            overall = "cancelled"
            unverified.append("要求仿真，但仿真阶段已取消")
        elif stages.get("simulated", {}).get("status") in (STATUS_SKIPPED, STATUS_CANCELLED):
            overall = "cancelled"
            unverified.append("要求仿真，但仿真未执行")
        else:
            overall = highest or "not_verified"
            unverified.append(
                "**本次未到达「已仿真」阶段**：要求仿真的验证不能以静态结论代替")
    else:
        overall = highest or "not_verified"

    return {"overall": overall, "highest_stage": highest, "unverified": unverified}


# ---------------------------------------------------------------------------
# 写入 / 失效
# ---------------------------------------------------------------------------

def record_evidence(root: str, *, workspace: str, package_id: str = "",
                    model_id: str = "", model_ref: str = "", variant: dict = None,
                    parameters: dict = None, file: dict = None,
                    stages: dict = None, artifacts: list = None,
                    simulation_required: bool = False, notes: list = None,
                    source: str = "", extra: dict = None) -> dict:
    """登记/更新一条验证证据，并让同一主体的旧证据失效。

    返回 ``{"entry", "evidence_id", "created", "stale_ids", "outcome"}``。
    同主体（同 workspace/package_id/model_id/model_ref/变体）里，
    **文件指纹或参数指纹不同** 的旧证据一律标 ``stale`` 并写明原因 ——
    模型和参数换了，上一次"验证通过"就不再描述当前的那个东西。
    """
    root = str(root or "")
    if not root:
        raise ModelValidationError("缺少模型资产根目录，无法记录验证证据")

    variant = dict(variant or {})
    parameters = dict(parameters or {})
    file_info = dict(file or {})
    evidence_fp = str(file_info.get("sha256") or "")
    if not file_info.get("path"):
        # 兼容调用方把路径塞在 key 或 relpath 里
        file_info["path"] = file_info.get("relpath") or file_info.get("abs_path") or ""

    evidence_id = evidence_id_for(workspace, package_id, model_id, model_ref,
                                  variant, params_fingerprint(parameters),
                                  evidence_fp)
    subject = subject_key(workspace, package_id, model_id, model_ref, variant)

    data = load_evidence(root)
    entries = data.get("entries") or {}

    merged_stages = blank_stages()
    existing = entries.get(evidence_id)
    if isinstance(existing, dict):
        merged_stages = {stage: dict(existing.get("stages", {}).get(stage)
                                     or blank_stage()) for stage in STAGES}
    created = True
    for stage, payload in (stages or {}).items():
        if isinstance(payload, dict):
            put_stage(merged_stages, stage, payload.get("status", STATUS_UNKNOWN),
                      payload.get("detail", ""), payload.get("artifacts"),
                      payload.get("at", ""))
        else:
            put_stage(merged_stages, stage, STATUS_PASS if payload is True else STATUS_FAIL,
                      "" if payload is True else str(payload))

    summary = overall_from_stages(merged_stages, simulation_required)
    now = model_store.utc_now()
    entry = {
        "evidence_id": evidence_id,
        "subject_key": subject,
        "workspace": str(workspace or ""),
        "package_id": str(package_id or ""),
        "model_id": str(model_id or ""),
        "model_ref": str(model_ref or ""),
        "variant": variant,
        "variant_key": variant_fingerprint(variant),
        "parameters": parameters,
        "params_fingerprint": params_fingerprint(parameters),
        "file": file_info,
        "file_fingerprint": evidence_fp,
        "stages": merged_stages,
        "simulation_required": bool(simulation_required),
        "overall": summary["overall"],
        "highest_stage": summary["highest_stage"],
        "unverified": summary["unverified"],
        "synthesis": synthesis_text(summary["overall"], model_ref, variant, file_info),
        "notes": [str(n) for n in (notes or [])],
        "source": str(source or ""),
        "legacy": False,
        # 有没有文件指纹决定这条证据能不能被"内容变了"这条规则判定为失效
        "content_bound": bool(evidence_fp),
        "status": "active",
        "stale_reason": "",
        "superseded_by": "",
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    if artifacts:
        entry["artifacts"] = [str(a) for a in artifacts]
    if extra:
        entry.update(extra)

    # 同一主体的其它证据全部失效（文件/参数指纹不同的版本）
    stale_ids = []
    for key, prior in entries.items():
        if key == evidence_id or not isinstance(prior, dict):
            continue
        if prior.get("subject_key") != subject:
            continue
        if prior.get("status") == "stale":
            continue
        reasons = []
        if prior.get("file_fingerprint") != evidence_fp:
            reasons.append("模型文件内容已变（指纹不同）")
        if prior.get("params_fingerprint") != entry["params_fingerprint"]:
            reasons.append("验证时的参数已变")
        if not reasons and not prior.get("legacy", False):
            continue        # 完全同一份数据：不该出现，但也不该把它打成 stale
        if prior.get("legacy", False) and not reasons:
            reasons.append("旧清单迁移的证据无法绑定到具体模型文件，"
                           "已被一次带指纹的验证取代")
        prior["status"] = "stale"
        prior["stale_reason"] = "；".join(reasons)
        prior["superseded_by"] = evidence_id
        prior["updated_at"] = now
        prior["synthesis"] = synthesis_text(prior.get("overall", ""),
                                            prior.get("model_ref", ""),
                                            prior.get("variant", {}),
                                            prior.get("file", {}), stale=True)
        stale_ids.append(key)

    entries[evidence_id] = entry
    data["entries"] = entries
    _save_evidence(root, data)
    return {"entry": entry, "evidence_id": evidence_id, "created": created,
            "stale_ids": stale_ids, "outcome": summary}


def synthesis_text(overall: str, model_ref: str, variant: dict,
                   file: dict, stale: bool = False) -> str:
    """一句话结论：界面、工具返回、LLM 摘要共用这一份措辞。"""
    base = OVERALL_LABELS.get(overall, overall or "未验证")
    bits = [f"{model_ref or '未知元件'}：{base}"]
    if variant:
        bits.append("变体 " + ", ".join(f"{k}={v}" for k, v in sorted(variant.items())))
    name = str((file or {}).get("relpath") or (file or {}).get("path") or "")
    if name:
        bits.append(f"文件 {name.rsplit('/', 1)[-1]}")
    if stale:
        bits.append("（已失效：模型文件或参数与当前不一致，不可再用于放行）")
    return "；".join(bits)


# ---------------------------------------------------------------------------
# 查询 / 包级汇总
# ---------------------------------------------------------------------------

def _active_entries(data: dict) -> list:
    return [e for e in (data.get("entries") or {}).values()
            if isinstance(e, dict) and e.get("status") != "stale"]


def list_evidence(root: str, package_id: str = "", model_id: str = "") -> list:
    """列出有效证据（新的在前）。stale 的不主动返回，但也没删 —— 追溯要用。"""
    entries = _active_entries(load_evidence(root))
    if package_id:
        entries = [e for e in entries if str(e.get("package_id") or "") == package_id]
    if model_id:
        entries = [e for e in entries if str(e.get("model_id") or "") == model_id]
    entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
    return entries


def get_evidence(root: str, evidence_id: str) -> dict:
    return (load_evidence(root).get("entries") or {}).get(str(evidence_id or ""))


def subject_evidence(root: str, workspace: str, package_id: str, model_id: str,
                     model_ref: str, variant: dict = None) -> dict:
    """取该主体当前有效的证据（同一变体最新版）。"""
    key = subject_key(workspace, package_id, model_id, model_ref, variant or {})
    hits = [e for e in _active_entries(load_evidence(root))
            if e.get("subject_key") == key]
    if not hits:
        return {}
    hits.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
    return hits[0]


def package_summary(root: str, package_id: str, models: list = None,
                    models_total: int = 0, index_truncated: bool = False) -> dict:
    """包级汇总：**永远不宣称整包已验证**，只报分子分母。

    为什么这么克制：一个 Design Kit 有上千个 cell，「某个元件验证通过」就
    把包说成可用，用户会以为包里所有型号都能拿来仿真。包级能给的只有
    "已验证 N / 共 M，其中仿真验证 K"；M 取不到（未索引/被截断）就如实说。
    """
    entries = [e for e in list_evidence(root, package_id=package_id)
               if bool(e.get("content_bound"))]
    simulated = [e for e in entries if e.get("overall") == "simulated"]
    placed = [e for e in entries if e.get("overall") == "placed"]
    # booted/listed 是"库/列表已就位但没仿真"的一侧：新增阶段若不计入统计，
    # 会掉进"漏桶" —— 现有判定只看 static/placed 是否为空，booted/listed 的
    # 证据既不算静态也不算已放置，就会被误当成"已到仿真"而放行整包。显式
    # 计数并纳入下方 package_verified 条件，堵住这个漏桶。
    booted = [e for e in entries if e.get("overall") == "booted"]
    listed = [e for e in entries if e.get("overall") == "listed"]
    static = [e for e in entries
              if e.get("overall") in ("parsed", "extracted", "saved")]
    coverage_known = models_total > 0 and not index_truncated
    out = {
        "package_id": str(package_id or ""),
        "models_total": models_total,          # 0 = 未知，不猜
        "models_indexed": len(models or []),
        "index_truncated": bool(index_truncated),
        "coverage_known": bool(coverage_known),
        "evidence_count": len(entries),
        "simulated_count": len(simulated),
        "placed_count": len(placed),
        "booted_count": len(booted),
        "listed_count": len(listed),
        "static_only_count": len(static),
        "simulated_ids": sorted({str(e.get("model_id") or "") for e in simulated}),
        "package_verified": False,
        "label": "",
        "note": "",
    }
    if (coverage_known and models_total and len(entries) == models_total
            and len(simulated) == models_total):
        # 全包每个模型都到达了仿真层才算"包已验证"。判据是"到仿真的条数 == 索引
        # 模型数"，而不是"没有别的桶" —— 后者会漏掉 failed/cancelled/未验证等
        # 既不属于 static/placed 也不属于 simulated 的条目。这条分支几乎不会
        # 命中，保留它是为了"能登陆却从不宣称"的规则有终点，而不是为了放行。
        out["package_verified"] = True
    parts = [f"已记录 {len(entries)} 条验证证据"]
    if simulated:
        parts.append(f"其中 {len(simulated)} 条到「已仿真」")
    if placed:
        parts.append(f"{len(placed)} 条只到「已放置」（未仿真）")
    if booted:
        parts.append(f"{len(booted)} 条只到「库已加载启动配置」（未仿真）")
    if listed:
        parts.append(f"{len(listed)} 条只到「原生列表可见」（未仿真）")
    if static:
        parts.append(f"{len(static)} 条只有静态证据")
    if coverage_known and models_total:
        parts.append(f"索引共 {models_total} 个模型，覆盖 {len(entries)}/{models_total}")
    else:
        parts.append("模型总数未知（未建立完整索引或索引被截断），不给出覆盖率")
    out["label"] = "；".join(parts)
    out["note"] = ("**单元件通过不代表整包可用**：未列在上面/未有证据的模型"
                   "没有经过任何验证，不能直接用于仿真。"
                   "「库已加载启动配置」「原生列表可见」也不等于模型可用 —— "
                   "未放进电路并跑过仿真就不能算验证通过。")
    return out


def readiness_for_package(root: str, package_id: str, models_total: int = 0,
                          index_truncated: bool = False,
                          require_simulation: bool = False) -> dict:
    """包能不能进 ready：只有"证据说话"，不看曾经做过哪些步骤。"""
    summary = package_summary(root, package_id, models_total=models_total,
                              index_truncated=index_truncated)
    reasons = []
    entries = [e for e in list_evidence(root, package_id=package_id)
               if bool(e.get("content_bound"))]
    if not entries:
        reasons.append("该包没有任何可追溯到模型文件的验证证据")
        ready = False
    elif require_simulation:
        simulated = [e for e in entries if e.get("overall") == "simulated"]
        if not simulated:
            reasons.append("要求仿真的验证没有任何一条到达「已仿真」")
            ready = False
        else:
            ready = True
    else:
        ready = True
    return {"ready": bool(ready), "reasons": reasons, "summary": summary,
            "note": summary["note"]}


# ---------------------------------------------------------------------------
# 旧清单迁移（只读翻译，不删任何字段）
# ---------------------------------------------------------------------------

LEGACY_NOTE = ("本条由**旧清单 validation 字段迁移**而来：当时只记录了元件层级"
               "的 ok 标记，没有模型文件指纹与参数指纹，无法绑定到具体模型文件，"
               "因此不能作为当前模型的放行依据 —— 请重新验证。")


def migrate_manifest_validation(root: str, records: list = None,
                                workspace: str = "") -> dict:
    """把旧清单里 ``validation`` 字典翻译成证据条目。

    只做三件事：读出旧值、**如实降级**（拿不到指纹的项一律 unknown）、写进
    evidence.json。manifest 里的旧字段**一个都不删**（那是历史记录，也是
    其它老版本客户端唯一认得的形状）。已迁移过的（同 key 已在 evidence 里）
    不重复生成。
    """
    data = load_evidence(root)
    entries = data.get("entries") or {}
    migrated, skipped, rows = [], [], []
    existing_keys = {str(e.get("migrated_from") or "")
                     for e in entries.values() if isinstance(e, dict)}
    for record in (records or []):
        if not isinstance(record, dict):
            continue
        package_id = str(record.get("package_id") or "")
        legacy = record.get("validation")
        if not isinstance(legacy, dict):
            continue
        for key, value in legacy.items():
            if not isinstance(value, dict):
                continue            # index 统计等子对象不是元件验证记录
            migration_key = f"{package_id}|{key}"
            if migration_key in existing_keys:
                skipped.append(migration_key)
                continue
            stages = blank_stages()
            put_stage(stages, "parsed",
                      STATUS_PASS if value.get("ok") else STATUS_UNKNOWN,
                      "旧清单记录的一次元件级校验"
                      + ("" if value.get("ok") else "（当时未通过）"))
            put_stage(stages, "placed", STATUS_UNKNOWN, "旧清单没有放置记录的字段")
            dataset = str(value.get("dataset_path") or "")
            ran = bool(value.get("smoke_sim")) and bool(dataset) and os.path.isfile(dataset)
            put_stage(stages, "simulated",
                      STATUS_PASS if ran else STATUS_UNKNOWN,
                      ("旧记录标注跑过基础仿真且数据集文件仍在"
                       if ran else "旧记录没有可核对的仿真产物")
                      + (f"：{dataset}" if dataset else ""))
            for stage in ("saved", "extracted"):
                put_stage(stages, stage, STATUS_UNKNOWN, "旧清单未记录该阶段")
            summary = overall_from_stages(stages, False)
            rows.append({
                "workspace": str(workspace or ""),
                "package_id": package_id,
                "model_id": "",
                "model_ref": str(key),
                "variant": {},
                "parameters": {},
                "file": {"path": "", "relpath": "", "sha256": ""},
                "stages": stages,
                "notes": [LEGACY_NOTE],
                "source": "legacy_manifest_migration",
                "extra": {"legacy": True, "content_bound": False,
                          "migrated_from": migration_key,
                          "legacy_validated_at": value.get("validated_at") or "",
                          "legacy_component_def_fingerprint":
                              value.get("component_def_fingerprint") or ""},
            })
            migrated.append(migration_key)

    written = []
    for payload in rows:
        result = record_evidence(
            root, workspace=payload["workspace"],
            package_id=payload["package_id"], model_id=payload["model_id"],
            model_ref=payload["model_ref"], stages=payload["stages"],
            file=payload["file"], parameters=payload["parameters"],
            notes=payload["notes"], source=payload["source"],
            extra=payload["extra"])
        written.append(result["evidence_id"])
    return {"migrated": migrated, "skipped_duplicated": skipped,
            "written_ids": written,
            "note": "旧清单 validation 字段保留未动；迁移结果只是**只读翻译**过的证据副本。"}
