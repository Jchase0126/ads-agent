"""模型包工具的**后端编排层** —— LLM 侧看到的 6 个模型工具都从这里落地。

为什么要有这一层，而不是让 LLM 直接调 ADS 端工具
--------------------------------------------------
1. **工作区绑定**：目标 Workspace 必须来自**可信应用上下文**（ADS 当前打开的
   工作区），不能接受 LLM 传入的任意路径。否则模型一句「导入到 D:\\...」就能
   把文件写到工作区之外 —— 这是真实的越权面，不是理论风险。
   所以每个涉及写入的模型工具都先调 ``get_workspace_info`` 拿当前工作区，
   再把它作为参数注入，LLM 侧只认 package_id / model_id。
2. **清单是唯一事实源**：包的状态、识别结果、型号索引都在 Workspace 的
   model_store 清单里。list / inspect 只需要读清单，不必把 ADS 叫起来，
   也不该让模型拿到文件内容。
3. **导入是复合动作**：解压（后端本地）→ 建索引（后端本地）→ 挂接（ADS 端）
   → 验证（ADS 端）。拆成两次模型调用会留下"解压完但没挂接"的中间态，
   所以在一次调用里编排完，并如实报告每一步的真实结果。

状态诚实性
----------
本模块**绝不**因为"解压成功"就报"模型就绪"。ready 只能由
``validate_model_import`` 通过后显式写入 —— 见 :func:`validate_model_import`。
"""

import json
import os
import threading

import adslog
import model_store
import tools as tools_mod

log = adslog.get("backend.model_tools")

# 包状态 → 中文说明（给界面与 LLM 看的同一份口径，别处不要另写一套）
STATE_LABELS = {
    model_store.STATE_SAVED: "已保存",
    model_store.STATE_INSPECTING: "检查中",
    model_store.STATE_PENDING_IMPORT: "待导入",
    model_store.STATE_IMPORTING: "导入中",
    model_store.STATE_PENDING_VERIFY: "待验证",
    model_store.STATE_READY: "已就绪",
    model_store.STATE_FAILED: "失败",
    model_store.STATE_CANCELLED: "已取消",
    model_store.STATE_AWAITING_USER: "等待用户操作",
}

KIND_LABELS = {
    "touchstone": "Touchstone 模型文件包",
    "design_kit": "ADS Design Kit",
    "mixed": "混合包（含模型文件与库结构）",
    "unknown": "未识别",
}


class ModelToolError(RuntimeError):
    """模型工具的可预期错误（会如实转成工具结果里的 error，不冒充成功）。"""


# ---------------------------------------------------------------------------
# 工作区绑定
# ---------------------------------------------------------------------------

def current_workspace(cfg: dict) -> str:
    """当前 ADS 打开的工作区路径。

    **没有打开工作区就明确报错**，绝不隐式落到某个默认工程 —— 模型资产的
    归属必须由用户的真实工作区决定。
    """
    try:
        info = tools_mod.call(cfg, "get_workspace_info", {})
    except Exception as e:  # noqa: BLE001
        raise ModelToolError(
            f"无法确认当前 ADS 工作区：{type(e).__name__}: {e}。"
            f"请确认 ADS 2027 已启动且工具服务可用。"
        ) from e
    if not isinstance(info, dict) or not info.get("workspace_open"):
        raise ModelToolError(
            "当前没有打开 ADS 工作区，无法管理模型压缩包。"
            "请先在 ADS 中打开或新建一个 Workspace —— "
            "模型资产归属于工作区，不会被存到某个隐式默认位置。"
        )
    path = str(info.get("path") or "").strip()
    if not path:
        raise ModelToolError("ADS 报告工作区已打开，但没有给出工作区路径，无法定位模型资产。")
    return path


def _lookup(root: str, package_id: str) -> dict | None:
    """取一条清单记录，不存在时返回 None（不抛）。

    model_store.get_package 对"记录不存在"是抛 ModelStoreError 还是返回
    None，取决于它内部把"包没找到"当成错误还是空结果。编排层两种都要能接：
    直接透传异常会让 HTTP handler 在发出响应前中断，连接不收尾，客户端
    看到的是 RemoteDisconnected（像服务器崩了），而真实原因只是"没这个包"。
    """
    try:
        return model_store.get_package(root, package_id)
    except model_store.ModelStoreError:
        return None


def _record_view(rec: dict, include_models: bool = True,
                 max_models: int = 200) -> dict:
    """把清单记录整理成给 LLM / 界面看的形状。

    刻意**不返回**文件绝对路径以外的任何文件内容：LLM 只需要标识与元数据。
    """
    state = rec.get("state") or model_store.STATE_SAVED
    out = {
        "package_id": rec.get("package_id"),
        # 清单里的字段名是 original_filename（强调"原始"），对外统一用
        # filename —— 面板卡片与 LLM 都读这一个键，避免两套叫法。
        "filename": rec.get("original_filename") or rec.get("stored_filename"),
        "size_bytes": rec.get("size_bytes"),
        "sha256": rec.get("sha256"),
        "package_kind": rec.get("package_kind"),
        "package_kind_label": KIND_LABELS.get(rec.get("package_kind"), "未识别"),
        "vendor": rec.get("vendor"),
        "version": rec.get("version"),
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "uploaded_at": rec.get("uploaded_at"),
        "ref_count": rec.get("ref_count", 1),
        "extracted": bool(rec.get("extract_relpath")),
        "library_attach": rec.get("library_attach"),
        "model_count": len(rec.get("models") or []),
    }
    if rec.get("last_error"):
        out["last_error"] = rec["last_error"]
    if include_models:
        models = rec.get("models") or []
        out["models"] = models[:max(0, int(max_models))]
        if len(models) > len(out["models"]):
            out["models_truncated"] = len(models) - len(out["models"])
    return out


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def list_model_packages(cfg: dict, args: dict) -> dict:
    """列出当前工作区的模型包资产（只读清单，不解压不加载）。"""
    workspace = current_workspace(cfg)
    root = model_store.store_root(workspace)
    records = model_store.list_packages(root)
    kind = str(args.get("package_kind") or "").strip()
    state = str(args.get("state") or "").strip()
    if kind:
        records = [r for r in records if r.get("package_kind") == kind]
    if state:
        records = [r for r in records if r.get("state") == state]
    try:
        limit = max(1, int(args.get("max_items") or 50))
    except (TypeError, ValueError):
        limit = 50
    total = len(records)
    records = records[:limit]
    return {
        "workspace": workspace,
        "total": total,
        "returned": len(records),
        "packages": [_record_view(r, include_models=False) for r in records],
        **({"note": f"共 {total} 个包，已按上限返回前 {len(records)} 个"}
           if total > len(records) else {}),
    }


def inspect_model_package(cfg: dict, args: dict) -> dict:
    """查看包详情：识别依据、候选套件根、文件统计、型号索引。"""
    package_id = str(args.get("package_id") or "").strip()
    if not package_id:
        raise ModelToolError("缺少 package_id")
    workspace = current_workspace(cfg)
    root = model_store.store_root(workspace)
    rec = _lookup(root, package_id)
    if rec is None:
        raise ModelToolError(
            f"当前工作区里没有模型包 {package_id}。"
            f"先用 list_model_packages 看已有的包；"
            f"包归属于上传时所在的工作区，换工程后不会自动带过来。"
        )
    include = args.get("include_models", True)
    try:
        max_models = int(args.get("max_models") or 200)
    except (TypeError, ValueError):
        max_models = 200
    view = _record_view(rec, include_models=bool(include), max_models=max_models)
    # 识别依据要露出来：让用户/模型知道"为什么这么判型"，而不是只给结论。
    # 清单里这些是**平铺字段**（kind_evidence / library_attach / …），
    # 不是嵌套的 detection 对象 —— 按平铺读，别照着别的模块的形状猜。
    attach = rec.get("library_attach") or {}
    scan = (rec.get("validation") or {}).get("scan") or {}
    view["detection"] = {
        "evidence": rec.get("kind_evidence") or [],
        "confidence": rec.get("kind_confidence"),
        "kit_root": attach.get("kit_root"),
        "kit_roots": attach.get("kit_root_candidates") or [],
        "kit_root_ambiguous": bool(attach.get("kit_root_ambiguous")),
        "candidates": rec.get("kind_candidates") or [],
        "defined_libraries": attach.get("defined_libraries") or [],
        "vendor_evidence": rec.get("vendor_evidence") or [],
        "version_evidence": rec.get("version_evidence") or [],
        "file_types": scan.get("ext_histogram") or {},
        "entry_count": scan.get("entry_count"),
        "notes": rec.get("notes") or [],
    }
    view["validation"] = rec.get("validation")
    view["archive_relpath"] = rec.get("archive_relpath")
    view["extract_relpath"] = rec.get("extract_relpath")
    view["workspace"] = workspace
    return {"package": view}


def import_model_package(cfg: dict, args: dict, cancel_event=None) -> dict:
    """按需导入：解压 → 建索引 → 挂接 ADS 工作区。

    幂等：同一包重复导入不会重复挂接（挂接前会核对工作区里是否已有同名同路径库）。
    """
    package_id = str(args.get("package_id") or "").strip()
    if not package_id:
        raise ModelToolError("缺少 package_id")
    workspace = current_workspace(cfg)
    root = model_store.store_root(workspace)

    steps: list = []

    def _step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": bool(ok), "detail": detail})

    rec = _lookup(root, package_id)
    if rec is None:
        raise ModelToolError(f"当前工作区里没有模型包 {package_id}")

    # --- 0. 只读扫描（不解压）：识别类型与套件根 -----------------------------
    try:
        rec = model_store.scan_archive(root, package_id)
        _step("scan", True, rec.get("package_kind") or "")
    except Exception as e:  # noqa: BLE001
        model_store.set_state(root, package_id, model_store.STATE_FAILED, error=str(e))
        _step("scan", False, f"{type(e).__name__}: {e}")
        raise ModelToolError(f"扫描模型包失败：{type(e).__name__}: {e}") from e

    # 有歧义时不武断：让用户/模型选套件根
    attach = rec.get("library_attach") or {}
    candidates = attach.get("kit_root_candidates") or []
    kit_root = str(args.get("kit_root") or "").strip()
    if not kit_root and len(candidates) > 1:
        model_store.set_state(root, package_id, model_store.STATE_AWAITING_USER)
        raise ModelToolError(
            f"包 {package_id} 里识别出多个候选套件根目录：{candidates}。"
            f"无法确定要导入哪一个 —— 请用 inspect_model_package 查看结构后，"
            f"在 kit_root 参数里明确指定其中一个。"
        )
    if not kit_root and candidates:
        kit_root = candidates[0]

    if cancel_event is not None and cancel_event.is_set():
        model_store.set_state(root, package_id, model_store.STATE_CANCELLED,
                              error="用户取消")
        return {"package_id": package_id, "cancelled": True,
                "message": "已在解压前取消，未执行任何 ADS 操作。", "steps": steps}

    # --- 1. 安全解压（临时目录 → 检查 → 提交） ------------------------------
    # 状态流转**交给 extract_package 自己管**（它内部会 inspecting →
    # pending_import/pending_verify）。这里如果抢先把状态置成 importing，
    # 反而会让 extract 内部的 inspecting 流转失败 —— 状态机不是给上层
    # 跳着玩的。上层只负责"从哪个状态起步是合法的"。
    try:
        cur = (_lookup(root, package_id) or {}).get("state")
        if cur == model_store.STATE_SAVED:
            model_store.set_state(root, package_id, model_store.STATE_INSPECTING)
    except model_store.IllegalStateTransition as e:
        raise ModelToolError(
            f"包 {package_id} 当前状态是「{cur}」，不能开始导入：{e}") from e
    try:
        rec = model_store.extract_package(root, package_id)
        _step("extract", True, rec.get("extract_relpath") or "")
    except model_store.UnsafeArchive as e:
        model_store.set_state(root, package_id, model_store.STATE_FAILED, error=str(e))
        _step("extract", False, "安全校验未通过")
        raise ModelToolError(
            f"解压被安全校验拒绝：{e}。原始 ZIP 已保留在 archives/ 下，"
            f"未向 ADS 写入任何内容。"
        ) from e
    except Exception as e:  # noqa: BLE001
        model_store.set_state(root, package_id, model_store.STATE_FAILED, error=str(e))
        _step("extract", False, f"{type(e).__name__}: {e}")
        raise ModelToolError(f"解压失败：{type(e).__name__}: {e}") from e

    # --- 2. 建型号索引 -------------------------------------------------------
    try:
        models = model_store.index_models(root, package_id)
        _step("index", True, f"{len(models)} 个型号")
    except Exception as e:  # noqa: BLE001
        model_store.set_state(root, package_id, model_store.STATE_FAILED, error=str(e))
        _step("index", False, f"{type(e).__name__}: {e}")
        raise ModelToolError(f"建立型号索引失败：{type(e).__name__}: {e}") from e

    # --- 3. 挂接到当前工作区（ADS 端） --------------------------------------
    kind = rec.get("package_kind")

    def _mark_importing() -> None:
        """置"导入中"（挂接阶段可能有几十秒，界面需要有可见的进行态）。

        重复导入时状态可能已经是 importing/pending_verify，那就不用再置。
        """
        try:
            model_store.set_state(root, package_id, model_store.STATE_IMPORTING)
        except model_store.IllegalStateTransition:
            pass

    if kind == "touchstone":
        # Touchstone 文件包不按 Design Kit 安装：靠 ADS 的 S 参数元件引用文件路径。
        # 这里只登记路径，不去改工作区的库定义。
        _mark_importing()
        _attach_update(
            root, package_id,
            {
                "mode": "touchstone_reference",
                "note": "Touchstone 模型通过 S 参数元件的文件路径引用，不需要挂接库；"
                        "放置元件时用 validate_model_import 核对元件与参数。",
                # 索引里模型文件的字段叫 relpath（相对工作区），不是 file_relpath
                "model_files": [m.get("relpath") for m in models
                                if m.get("relpath")][:200],
            },
        )
        _step("attach", True, "Touchstone：登记模型文件路径（不挂接库）")
        model_store.set_state(root, package_id, model_store.STATE_PENDING_VERIFY)
        return {
            "package_id": package_id,
            "state": model_store.STATE_PENDING_VERIFY,
            "state_label": STATE_LABELS[model_store.STATE_PENDING_VERIFY],
            "kind": kind,
            "workspace": workspace,
            "model_count": len(models),
            "steps": steps,
            "message": "已安全解压并建立索引。Touchstone 模型通过 S 参数元件引用使用，"
                       "还没有验证过元件能否真正放置 —— 请用 validate_model_import 验证。",
        }

    if kind not in ("design_kit", "mixed"):
        _mark_importing()
        model_store.set_state(root, package_id, model_store.STATE_PENDING_VERIFY)
        return {
            "package_id": package_id,
            "state": model_store.STATE_PENDING_VERIFY,
            "state_label": STATE_LABELS[model_store.STATE_PENDING_VERIFY],
            "kind": kind,
            "workspace": workspace,
            "steps": steps,
            "supported": False,
            "message": f"该包被识别为 {KIND_LABELS.get(kind, kind)}，"
                       f"不属于当前支持自动导入的范围（Touchstone 模型包 / ADS Design Kit）。"
                       f"内容已安全解压并可浏览，但没有挂接为 ADS 库。",
        }

    extract_abs = _resolve_extract_abs(workspace, rec)
    if cancel_event is not None and cancel_event.is_set():
        model_store.set_state(root, package_id, model_store.STATE_CANCELLED,
                              error="用户取消")
        return {"package_id": package_id, "cancelled": True,
                "message": "已在挂接前取消，工作区未做任何挂接改动。", "steps": steps}

    kit_root_abs = _join_kit_root(extract_abs, kit_root)
    _mark_importing()
    try:
        attach = tools_mod.call(cfg, "attach_design_kit", {
            "package_id": package_id,
            "kit_root": kit_root_abs,
            "kit_root_rel": kit_root,
            "workspace": workspace,          # 可信上下文，后端注入
            "library_names": _lib_names(rec, args.get("vendor_filter")),
        })
    except tools_mod.AdsToolError as e:
        model_store.set_state(root, package_id, model_store.STATE_FAILED, error=str(e))
        _step("attach", False, str(e))
        raise ModelToolError(
            f"挂接到工作区失败：{e}。解压产物已保留在 extracted/ 下，未向 ADS 写入库定义。"
        ) from e

    attach_info = {
        "mode": "workspace_read_only",
        "libraries": attach.get("libraries") or [],
        "already_attached": bool(attach.get("already_attached")),
        "attached_at": attach.get("attached_at"),
        "kit_root": kit_root,
    }
    _attach_update(root, package_id, attach_info)
    _step("attach", True,
          f"{len(attach_info['libraries'])} 个库"
          + ("（此前已挂接，未重复添加）" if attach_info["already_attached"] else ""))

    # 解压结束 ≠ 模型就绪：挂接完成后必须经过 ADS 侧验证
    try:
        model_store.set_state(root, package_id, model_store.STATE_IMPORTING)
    except model_store.IllegalStateTransition:
        pass          # 已经是 importing（重复导入）时无需再置
    model_store.set_state(root, package_id, model_store.STATE_PENDING_VERIFY)
    return {
        "package_id": package_id,
        "state": model_store.STATE_PENDING_VERIFY,
        "state_label": STATE_LABELS[model_store.STATE_PENDING_VERIFY],
        "kind": kind,
        "workspace": workspace,
        "model_count": len(models),
        "library_attach": attach_info,
        "steps": steps,
        "message": "已解压、建索引并挂接到当前工作区（只读）。"
                   "**挂接成功不等于模型可用** —— 请用 list_vendor_models 找元件、"
                   "再用 validate_model_import 验证后才能放进电路仿真。",
    }


def list_vendor_models(cfg: dict, args: dict) -> dict:
    """在已挂接库（含只读原厂库）里检索元件。目标工作区由后端绑定。"""
    workspace = current_workspace(cfg)
    params = {"workspace": workspace}
    for key in ("library", "name_prefix", "max_items", "include_params"):
        if args.get(key) is not None:
            params[key] = args[key]
    return tools_mod.call(cfg, "list_vendor_models", params)


def _index_entries_for(workspace: str, library: str, cell: str) -> dict:
    """在工作区已导入的模型包索引里检索这个元件。

    为什么要接：索引里有**真实从文件解析出来**的频率范围、参考阻抗、端口数、
    偏置条件，而 ADS 端的 `model_def` 只有参数定义（本版本 `ModelParam` 连
    min/max 都没有）。所以「参考阻抗 50Ω / 频段 0.1–6 GHz / VCE 3V」这类信息
    **只有索引层有** —— 工具描述承诺了它，就得在这儿接上。

    检索交给 `model_store.find_models`：它是数据的产出方，型号/库归一化规则在
    那里只有一份（不做前缀匹配，`BFP181` 不会混淆 `BFP181W`）。

    只采信**已导入**的包（pending_verify / ready）：没解压的包索引不可信。
    """
    root = model_store.store_root(workspace)
    live: list = []
    for rec in model_store.list_packages(root):
        if rec.get("state") in (model_store.STATE_READY,
                                model_store.STATE_PENDING_VERIFY):
            live.append(rec)
    if not live:
        return {"found": False, "count": 0, "hits": [],
                "indexed_entries_scanned": 0,
                "notes": ["当前工作区没有已导入的模型包（导入后才会建立型号索引）"]}
    scanned = sum(len(r.get("models") or []) for r in live)
    found = model_store.find_models(root, part=cell, library=library,
                                    models=[m for r in live
                                            for m in (r.get("models") or [])])
    # 索引可能被 max_index_entries 截断（一个 Design Kit 动辄几千条），
    # 把"已比对多少 / 共多少"如实带出去，否则模型会以为该型号真的不存在。
    total = sum(int((r.get("validation") or {}).get("index", {}).get("total")
                    or len(r.get("models") or [])) for r in live)
    found["indexed_entries_total"] = total
    found["index_truncated"] = bool(total > scanned)
    return found


def get_vendor_model_info(cfg: dict, args: dict) -> dict:
    """单个元件详情：ADS 端 model_def 参数定义 + 索引里的模型元数据。

    两边信息互补、都只放真实读到的：
      * ADS 端 `model_def` —— 参数名/类型/单位/默认值（厂家在 AEL 里定义的那些）；
      * 后端模型索引  —— 频率范围、参考阻抗、端口数、偏置（从模型文件头解析）。

    ADS 端拿不到频率范围（本版本 `ModelParam` 没有 min/max 字段），所以这部分
    必须由索引补上，否则工具描述就是空承诺。

    **一个型号可能有多个偏置变体**（BFP181 有 VCE_0.5V_IC_1mA、_2V_IC_10mA …），
    所以 `model_index.hits` 是列表而不是单条 —— 用户指定了偏置时应该在
    列表里选对应那条，而不是让工具随便挑一个。
    """
    workspace = current_workspace(cfg)
    library = str(args.get("library") or "").strip()
    cell = str(args.get("cell") or "").strip()
    result = tools_mod.call(cfg, "get_vendor_model_info", {
        "library": args.get("library"),
        "cell": args.get("cell"),
        "view": args.get("view") or "schematic",
        "workspace": workspace,
    })

    found = _index_entries_for(workspace, library, cell)
    if not found.get("found"):
        # 如实说明"没有可引用的索引数据"，而不是静默留空让人以为"没有限制"
        result["model_index"] = {
            "available": False,
            "reason": ("当前工作区的已导入模型包索引里没有这个型号。"
                       "若它来自手工挂接的原厂库（不是通过附件导入的包），"
                       "索引里就没有它的记录 —— 频率范围与参考阻抗需以仿真实测为准。"),
            "notes": found.get("notes") or [],
            "indexed_entries_scanned": found.get("indexed_entries_scanned", 0),
            "indexed_entries_total": found.get("indexed_entries_total", 0),
            "index_truncated": bool(found.get("index_truncated")),
            "hits": [],
            # 顶层字段恒存在（值可能为 None）—— 调用方要能区分"没查到"
            # 与"查到了但该项未知"，不能靠字段在不在来判断。
            "part": None,
            "vendor": None,
            "model_type": None,
            "ports": None,
            "freq_start_hz": None,
            "freq_stop_hz": None,
            "frequency_unit": None,
            "data_format": None,
            "reference_impedance_ohm": None,
            "reference_impedance_source": None,
            "library": None,
            "file_relpath": None,
            "bias": {},
            "notes": [],
        }
        if found.get("index_truncated"):
            result["model_index"]["reason"] += (
                f"（注意：索引共 {found.get('indexed_entries_total')} 条，"
                f"只索引了前 {found.get('indexed_entries_scanned')} 条，"
                f"该型号可能落在未索引的部分）")
        return result

    hits = found.get("hits") or []
    first = hits[0]
    result["model_index"] = {
        "available": True,
        "count": len(hits),
        # 同一型号可能有多条偏置变体（BFP181 有多种 VCE/IC 组合），必须给列表：
        # 挑一条当"这个型号的参数"是不对的，不同偏置下的模型是不同的东西。
        "hits": hits,
        "indexed_entries_scanned": found.get("indexed_entries_scanned", 0),
        "indexed_entries_total": found.get("indexed_entries_total", 0),
        "index_truncated": bool(found.get("index_truncated")),
        # 顶层字段取第一条作为**概览**（方便不关心变体的调用方），
        # 但上面 hits 才是完整信息，notes 里也点明了这层关系。
        "kind": first.get("kind"),
        "part": first.get("part"),
        "vendor": first.get("vendor"),
        "model_type": first.get("model_type"),
        "ports": first.get("ports"),
        "freq_start_hz": first.get("freq_start_hz"),
        "freq_stop_hz": first.get("freq_stop_hz"),
        "frequency_unit": first.get("frequency_unit"),
        "data_format": first.get("data_format"),
        "reference_impedance_ohm": first.get("reference_impedance_ohm"),
        "reference_impedance_source": first.get("reference_impedance_source"),
        "library": first.get("library"),
        "file_relpath": first.get("relpath"),
        "bias": first.get("bias") or {},
        "notes": list(first.get("notes") or []),
    }
    if len(hits) > 1:
        variants = []
        for h in hits[:10]:
            bias = ", ".join(f"{k}={v}" for k, v in (h.get("bias") or {}).items())
            variants.append(f"{h.get('relpath', '').rsplit('/', 1)[-1]}"
                            + (f" ({bias})" if bias else ""))
        result["model_index"]["notes"].append(
            f"该型号在索引里有 {len(hits)} 个偏置变体；顶层字段是第一条的概览，"
            f"选型时请在 hits 里按偏置条件挑对应那条。示例：{'; '.join(variants[:5])}")
    # 覆盖目标频段要提醒：规格要求"不静默外推"
    if first.get("freq_start_hz") and first.get("freq_stop_hz"):
        result["model_index"]["frequency_coverage_note"] = (
            f"（以第一条变体为例）该模型的频率范围是 "
            f"{first['freq_start_hz']:.6g}–{first['freq_stop_hz']:.6g} Hz。"
            f"**目标频段超出这个范围时不能直接外推并报告达标** —— "
            f"应改选覆盖该频段的型号，或用仿真实测确认。"
        )
    return result


def validate_model_import(cfg: dict, args: dict) -> dict:
    """验证模型可用性 —— 这是把状态标成 ready 的**唯一**入口。

    只有本工具明确通过，才写 ``STATE_READY``。解压成功、索引建成、
    库已挂接都只是"前置条件齐了"，不是"模型可用"。
    """
    workspace = current_workspace(cfg)
    library = str(args.get("library") or "").strip()
    cell = str(args.get("cell") or "").strip()
    if not library or not cell:
        raise ModelToolError("需要 library 与 cell")

    result = tools_mod.call(cfg, "validate_model_import", {
        "library": library,
        "cell": cell,
        "parameters": args.get("parameters") or {},
        "run_smoke_sim": bool(args.get("run_smoke_sim")),
        "workspace": workspace,
    })

    ok = bool(result.get("ok"))
    # 找出这个元件属于哪个包，把验证结果回写到对应记录上
    root = model_store.store_root(workspace)
    touched: list = []
    if ok:
        for rec in model_store.list_packages(root):
            attach = rec.get("library_attach") or {}
            names = {str(x.get("name") or "") for x in (attach.get("libraries") or [])
                     if isinstance(x, dict)}
            if library in names:
                validation = dict(rec.get("validation") or {})
                validation[library + ":" + cell] = {
                    "ok": True,
                    "validated_at": result.get("validated_at"),
                    "smoke_sim": bool(args.get("run_smoke_sim")),
                    "dataset_path": result.get("dataset_path"),
                    # 元件**定义**指纹（库+cell+参数定义）。
                    # 注意它与 ads_ops 里同名的 model_fingerprint 不是一回事：
                    # 那个是"设计引用到的模型文件内容哈希"dict，用于缓存失效。
                    "component_def_fingerprint":
                        result.get("component_def_fingerprint"),
                }
                try:
                    # 验证结果与「就绪」状态**分两步**写：
                    # update_package 改普通字段，set_state 走状态机校验。
                    # 混在一起写会绕过流转检查（或者被 update_package 直接拒掉
                    # state 参数），那"只有验证通过才能就绪"这条门禁就没了。
                    model_store.update_package(
                        root, rec["package_id"], validation=validation)
                    cur = (_lookup(root, rec["package_id"]) or {}).get("state")
                    if cur != model_store.STATE_PENDING_VERIFY:
                        # 例如包已被重新上传/取消过 —— 不硬闯，如实说明
                        log.warning("包 %s 当前状态 %s 不可直接置 ready",
                                    rec.get("package_id"), cur)
                    else:
                        model_store.set_state(
                            root, rec["package_id"], model_store.STATE_READY)
                        touched.append(rec["package_id"])
                except Exception as e:  # noqa: BLE001
                    log.warning("回写验证结果失败 %s: %s", rec.get("package_id"), e)

    result["workspace"] = workspace
    result["packages_marked_ready"] = touched
    if ok:
        result["message"] = ("验证通过，该元件模型可用于仿真。"
                             + ("" if args.get("run_smoke_sim")
                                else "（本次为静态校验，未跑仿真实证）"))
    else:
        result["message"] = ("验证未通过，模型**不可用于仿真**。"
                             "相关包状态保持待验证，不会被标成就绪。")
    return result


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

def _attach_update(root: str, package_id: str, attach_info: dict) -> dict:
    """把挂接结果**并进** library_attach，而不是整体替换。

    整体替换是有代价的：扫描阶段已经把 lib.defs 解析进 ``library_attach``
    （kit_root_candidates / defined_libraries / kit_root_ambiguous），
    那些是后续"多候选套件根判定"和"按供应商筛库"要用的。直接覆盖会把它们
    冲成空 —— 表现为：界面看不到套件根候选、vendor_filter 永远筛不出东西。
    所以这里只补挂接相关的键。
    """
    rec = _lookup(root, package_id) or {}
    current = dict(rec.get("library_attach") or {})
    current.update(attach_info)
    return model_store.update_package(root, package_id, library_attach=current)


def _resolve_extract_abs(workspace: str, rec: dict) -> str:
    """把清单里的相对解压路径解析成绝对路径，并核对它确实在解压目录内。

    工程迁移后相对路径可能失效 —— 这里核对不通过就报错，绝不返回一个
    猜出来的路径让后续往未知位置写东西。
    """
    rel = str(rec.get("extract_relpath") or "").strip()
    if not rel:
        raise ModelToolError(f"包 {rec.get('package_id')} 尚未解压，无法挂接")
    root = model_store.store_root(workspace)
    abs_path = os.path.normpath(os.path.join(workspace, rel))
    norm_root = os.path.normpath(root)
    if not (abs_path == norm_root or abs_path.startswith(norm_root + os.sep)):
        raise ModelToolError(
            f"解压目录 {rel} 不在模型资产根目录内，已阻止继续（可能是工程迁移后"
            f"清单里的相对路径被改坏了）。请重新上传该压缩包。"
        )
    if not os.path.isdir(abs_path):
        raise ModelToolError(
            f"解压目录不存在：{rel}。工程可能已被移动或删除 —— "
            f"请重新上传该压缩包，或用 relocate 重新定位资产。"
        )
    return abs_path


def _join_kit_root(extract_abs: str, kit_root: str) -> str:
    """在解压目录内拼套件根，并挡住越界。"""
    if not kit_root:
        return extract_abs
    joined = os.path.normpath(os.path.join(extract_abs, kit_root))
    if not joined.startswith(os.path.normpath(extract_abs) + os.sep):
        raise ModelToolError(f"套件根路径 {kit_root} 越出了解压目录，已阻止")
    if not os.path.isdir(joined):
        raise ModelToolError(f"套件根目录不存在：{kit_root}")
    return joined


def _lib_names(rec: dict, vendor_filter: str = "") -> list:
    """从包里挑出要挂接的库名。

    优先用 Design Kit 自己的 lib.defs 里 DEFINE 的库名（那是原厂声明的权威
    集合，清单扫描时已解析进 ``library_attach.defined_libraries``）；挑不出
    就留空，交给 ADS 端按 lib.defs 自己解析。
    """
    defined = (rec.get("library_attach") or {}).get("defined_libraries") or []
    names = [str(x.get("name")) for x in defined
             if isinstance(x, dict) and x.get("name")]
    if not names:
        return []
    vendor = str(vendor_filter or "").strip().lower()
    if not vendor:
        return names
    picked = [n for n in names if vendor in n.lower()]
    # 挑不出就**如实报空**而不是悄悄全挂 —— 用户明确说了只要某供应商的
    return picked


# ---------------------------------------------------------------------------
# 派发表（agent.py 按名字取）
# ---------------------------------------------------------------------------

LOCAL_HANDLERS = {
    "list_model_packages": list_model_packages,
    "inspect_model_package": inspect_model_package,
}
ADS_HANDLERS = {
    "import_model_package": import_model_package,
    "list_vendor_models": list_vendor_models,
    "get_vendor_model_info": get_vendor_model_info,
    "validate_model_import": validate_model_import,
}


def is_model_tool(name: str) -> bool:
    return name in LOCAL_HANDLERS or name in ADS_HANDLERS