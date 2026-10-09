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

import os

import adslog
import model_orchestration
import model_store
import model_validation
import shared_models
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


class AdsUnreachableError(ModelToolError):
    """ADS 工具服务**连接层**失败（连不上 / 超时），与"没打开工作区"是两回事。

    为什么要单开一类：``current_workspace`` 过去把 ``tools_mod.call`` 的所有异常
    都吞成 ``ModelToolError``，于是「ADS 完全不可达」与「没有打开工作区」在 API
    层不可区分 —— 面板只会看到 409 no_workspace，用户以为该去开工程，实际是
    ADS/工具服务没起来。本类继承 ``ModelToolError``，既有 ``except ModelToolError``
    调用方行为不变；需要区分的一方（如 /models/open）可先捕获本类回 502。
    """


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
    except tools_mod.AdsToolError as e:
        # 连接层失败：如实标成"ADS 不可达"，别冒充"工作区没打开"。
        raise AdsUnreachableError(
            f"无法连接 ADS 工具服务，无法确认当前工作区：{e}。"
            f"请确认 ADS 2027 已启动且工具服务可用。"
        ) from e
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
        "shared_backup": rec.get("shared_backup") or {"backed_up": False},
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


def list_shared_model_packages(cfg: dict, args: dict) -> dict:
    """查询 ADS Agent 统一 libraries 目录中的模型包备份。"""
    try:
        workspace = current_workspace(cfg)
        return shared_models.list_packages(cfg, workspace, args)
    except shared_models.SharedModelError as e:
        raise ModelToolError(str(e)) from e


def search_shared_models(cfg: dict, args: dict) -> dict:
    """按型号或成员文件名定位共享模型库，返回所属模型包标识。"""
    try:
        workspace = current_workspace(cfg)
        return shared_models.search_models(cfg, workspace, args)
    except shared_models.SharedModelError as e:
        raise ModelToolError(str(e)) from e


def import_shared_model_package(cfg: dict, args: dict, cancel_event=None) -> dict:
    """将共享备份复制到当前工作区，再交给既有导入/挂接流水线。"""
    package_id = str(args.get("package_id") or "").strip()
    if not package_id:
        raise ModelToolError("缺少 package_id")
    if cancel_event is not None and cancel_event.is_set():
        return _shared_import_cancelled(package_id)
    workspace = current_workspace(cfg)
    if cancel_event is not None and cancel_event.is_set():
        return _shared_import_cancelled(package_id, workspace)
    try:
        local = shared_models.copy_to_workspace(cfg, workspace, package_id)
    except shared_models.SharedModelError as e:
        raise ModelToolError(str(e)) from e
    if cancel_event is not None and cancel_event.is_set():
        return _shared_import_cancelled(package_id, workspace, copied=True)
    active_workspace = current_workspace(cfg)
    if (os.path.normcase(os.path.realpath(active_workspace)) !=
            os.path.normcase(os.path.realpath(workspace))):
        raise ModelToolError(
            "共享模型包已复制到原工作区，但 ADS 当前工作区已切换，已停止导入。"
            "请切回原工作区重试；不会导入到切换后的工作区。")
    result = _import_model_package_bound(cfg, {
        "package_id": local["package_id"],
        "kit_root": str(args.get("kit_root") or ""),
        "vendor_filter": str(args.get("vendor_filter") or ""),
    }, workspace, cancel_event=cancel_event)
    result["shared_source"] = {"package_id": package_id,
                               "filename": local.get("original_filename") or
                                           local.get("stored_filename"),
                               "copied_to_workspace": workspace}
    return result


def _shared_import_cancelled(package_id: str, workspace: str = "", *,
                             copied: bool = False) -> dict:
    return {"package_id": package_id, "cancelled": True,
            "op_state": model_orchestration.OP_CANCELLED,
            "op_state_label": model_orchestration.OP_LABELS[
                model_orchestration.OP_CANCELLED],
            "message": "共享模型包导入已取消。", "steps": [],
            "shared_source": {"package_id": package_id,
                              "copied_to_workspace": workspace if copied else "",
                              "copied": copied}}


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

    **与附件按钮走同一条流水线**：本函数只是把 LLM 的调用交给
    :mod:`model_orchestration`（同一把工作区串行锁、同一份持久化操作记录）。
    过去 LLM 入口直接调编排函数、不过那把锁，于是"按钮与 LLM 同时导入"
    会并发改同一份 lib.defs —— 重复挂接正是用户明确要求避免的。

    幂等：同一包重复导入不会重复挂接（挂接前会核对工作区里是否已有同名同路径库）；
    同一工作区同一包已有进行中的导入时，本次**并入**那条而不是另起一条。
    """
    package_id = str(args.get("package_id") or "").strip()
    if not package_id:
        raise ModelToolError("缺少 package_id")
    workspace = current_workspace(cfg)
    return _import_model_package_bound(cfg, args, workspace,
                                       cancel_event=cancel_event)


def _import_model_package_bound(cfg: dict, args: dict, workspace: str,
                                cancel_event=None) -> dict:
    """内部入口：仅接收上游已从 ADS 可信上下文取得的固定目标工作区。"""
    package_id = str(args.get("package_id") or "").strip()
    kit_root = str(args.get("kit_root") or "").strip()
    vendor_filter = str(args.get("vendor_filter") or "").strip()

    try:
        op = model_orchestration.get_orchestrator().run_sync(
            cfg, workspace, package_id, kit_root=kit_root,
            vendor_filter=vendor_filter, source="llm",
            cancel_event=cancel_event)
    except model_orchestration.OrchestrationError as e:
        # 编排层的可预期错误（包不在这个工作区等）如实转成工具结果里的 error，
        # 不冒充成功 —— 见本模块 docstring 的"状态诚实性"。
        raise ModelToolError(str(e)) from e

    return _op_result_view(op)


def _op_result_view(op: dict) -> dict:
    """把一条操作记录整理成工具/接口返回体。

    失败与取消都保留在返回体里（不用异常抛）—— 模型工具的结果要能被 LLM
    读到"到底哪一步没成"，抛异常只会变成一句看不到步骤的 error。
    """
    result = dict(op.get("result") or {})
    result.setdefault("package_id", op.get("package_id"))
    result.setdefault("steps", op.get("steps") or [])
    result["op_id"] = op.get("op_id")
    result["op_state"] = op.get("state")
    result["op_state_label"] = model_orchestration.OP_LABELS.get(
        op.get("state"), op.get("state"))
    result["cancelled"] = op.get("state") in (
        model_orchestration.OP_CANCELLED,
        model_orchestration.OP_CANCEL_REQUESTED)
    if op.get("message"):
        result["message"] = op["message"]
    if op.get("error"):
        result["error"] = op["error"]
    if op.get("error_kind"):
        result["kind"] = op["error_kind"]
    if op.get("partial"):
        result["partial"] = True
    if op.get("joined"):
        #只看有没有并入，**不看记录的 source**：按钮（http）先起的操作，
        # LLM 并进来时记录的 source 仍是 http ——过去这里多判了一个
        # source == "llm"，结果"LLM 并入了已有导入"这件事在返回体里
        # 根本不体现，调用方无法区分"我发起的"与"我蹭了别人的"。
        result["joined_existing_op"] = True
    return result


def list_vendor_models(cfg: dict, args: dict, cancel_event=None) -> dict:
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


def get_vendor_model_info(cfg: dict, args: dict, cancel_event=None) -> dict:
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


def validate_model_import(cfg: dict, args: dict, cancel_event=None) -> dict:
    """验证模型可用性 —— 这是把状态标成 ready 的**唯一**入口。

    只有本工具明确通过，才写 ``STATE_READY``。解压成功、索引建成、
    库已挂接都只是"前置条件齐了"，不是"模型可用"。
    """
    workspace = current_workspace(cfg)
    library = str(args.get("library") or "").strip()
    cell = str(args.get("cell") or "").strip()
    model_file = str(args.get("model_file") or args.get("file")
                     or args.get("path") or "").strip()
    if not model_file and not (library and cell):
        raise ModelToolError("需要 library + cell，或 model_file")
    package_id = str(args.get("package_id") or "").strip()
    if model_file:
        if os.path.isabs(model_file):
            raise ModelToolError("model_file 必须是当前工作区内的相对路径")
        normalized = os.path.normpath(model_file)
        if normalized == ".." or normalized.startswith(".." + os.sep):
            raise ModelToolError("model_file 不能越出当前工作区")
        if package_id:
            package = _lookup(model_store.store_root(workspace), package_id)
            extracted = str((package or {}).get("extract_relpath") or "")
            if not extracted:
                raise ModelToolError("指定模型包尚未解压，不能绑定其模型文件")
            extracted_root = os.path.realpath(os.path.join(workspace, extracted))
            candidate = os.path.realpath(os.path.join(workspace, normalized))
            try:
                if os.path.commonpath((extracted_root, candidate)) != extracted_root:
                    raise ModelToolError("model_file 不属于所指定模型包的解压目录")
            except ValueError as e:
                raise ModelToolError("model_file 路径与模型包目录不一致") from e

    # 只傳入明确定义的字段；工作区永远由 ADS 当前上下文注入。
    allowed = ("master", "library", "cell", "view", "parameters", "variant",
               "bias", "package", "temperature", "package_id", "model_id",
               "component", "smoke_component", "target_band",
               "run_smoke_sim", "require_smoke_sim")
    payload = {key: args[key] for key in allowed if key in args}
    if model_file:
        payload["model_file"] = model_file
    payload["workspace"] = workspace
    result = tools_mod.call(cfg, "validate_model_import", payload)

    verification = result.get("verification") or {}
    if not isinstance(verification, dict) or not verification:
        result["workspace"] = workspace
        result.setdefault("message", "ADS 没有返回分阶段验证证据；本次结果未写入验证台账，也不会改变模型包状态。")
        result["packages_marked_ready"] = []
        return result
    root = model_store.store_root(workspace)
    package_id = str(args.get("package_id") or verification.get("package_id") or "")
    model_ref = str(verification.get("model_ref") or result.get("master")
                    or model_file or f"{library}:{cell}")
    entry = model_validation.record_evidence(
        root, workspace=workspace, package_id=package_id,
        model_id=str(args.get("model_id") or verification.get("model_id") or ""),
        model_ref=model_ref,
        variant=verification.get("variant") or args.get("variant") or {},
        parameters=verification.get("parameters") or args.get("parameters") or {},
        file=verification.get("file") or {},
        stages=verification.get("stages") or {},
        artifacts=verification.get("artifacts") or [],
        simulation_required=bool(verification.get("simulation_required")),
        notes=result.get("warnings") or [], source="validate_model_import")
    result["evidence"] = entry.get("entry")
    result["evidence_id"] = entry.get("evidence_id")
    result["workspace"] = workspace
    result["packages_marked_ready"] = []

    # 一条元件证据不能代表整包。只有包级汇总明确覆盖完整索引且每个模型都
    # 达到仿真验证，才允许将包状态改为 ready。
    if package_id:
        rec = _lookup(root, package_id)
        if rec is not None:
            models = rec.get("models") or []
            summary = model_validation.package_summary(
                root, package_id, models_total=len(models),
                models=models, index_truncated=bool(rec.get("models_truncated")))
            validation = dict(rec.get("validation") or {})
            validation["evidence_summary"] = summary
            model_store.update_package(root, package_id, validation=validation)
            if summary.get("package_verified") and rec.get("state") == model_store.STATE_PENDING_VERIFY:
                model_store.set_state(root, package_id, model_store.STATE_READY)
                result["packages_marked_ready"] = [package_id]

    result["message"] = ("验证证据已保存。"
                          + ("模型达到已仿真验证。" if verification.get("overall") == "simulated"
                             else "当前只确认到「" + str(verification.get("overall") or "未验证")
                             + "」，不能据此宣称模型性能已仿真验证。"))
    return result


def _resolve_native_kit_root(workspace: str, rec: dict) -> str:
    """从清单解析**可信**的套件根绝对路径，供原生元件列表定位用。

    绝不接受任何外部传入的路径：解压目录必须落在模型资产根目录内，套件根
    必须在解压目录内且真实存在，否则一律返回 ""（让 ADS 端从已挂接库回推，
    而不是把可疑路径喂过去）。清单若被改坏指向工作区外，这里直接拒绝。
    """
    rel = str(rec.get("extract_relpath") or "").strip()
    kit = str((rec.get("library_attach") or {}).get("kit_root") or "").strip()
    if not rel or not kit:
        return ""
    store = os.path.normpath(model_store.store_root(workspace))
    extract_abs = os.path.normpath(os.path.join(workspace, rel))
    if not (extract_abs == store or extract_abs.startswith(store + os.sep)):
        return ""
    joined = os.path.normpath(os.path.join(extract_abs, kit))
    if not (joined == extract_abs or joined.startswith(extract_abs + os.sep)):
        return ""
    if not os.path.isdir(joined):
        return ""
    return joined


def _first_attached_library(rec: dict) -> str:
    """该包第一个已挂接库名（清单里挂接结果的第一条），没有则返回 ""。"""
    attach = rec.get("library_attach") or {}
    for item in (attach.get("libraries") or []):
        if isinstance(item, dict) and item.get("name"):
            return str(item["name"])
    return ""


def open_vendor_palette(cfg: dict, args: dict, cancel_event=None) -> dict:
    """在 ADS **原生元件列表**里打开/定位某个已导入的原厂包。

    工作区与套件根目录一律由**可信上下文**解析，绝不采信 LLM 传入的路径：
    LLM 只能给 package_id（以及 library / category / view）。跨工作区/包不存在
    在此给出可读错误；能否真正打开由 ADS 端按机制事实判定，本函数只如实转达。
    """
    workspace = current_workspace(cfg)
    package_id = str(args.get("package_id") or "").strip()
    if not package_id:
        raise ModelToolError("缺少 package_id")
    root = model_store.store_root(workspace)
    rec = _lookup(root, package_id)
    if rec is None:
        raise ModelToolError(
            f"当前工作区里没有模型包 {package_id}。"
            f"包归属于上传时所在的工作区，换工程后不会自动带过来 —— "
            f"请先用 list_model_packages 看当前工作区已有哪些包。")
    kind = str(rec.get("package_kind") or "")

    params = {"package_id": package_id, "workspace": workspace}  # 工作区由后端注入
    if kind:
        # 透传清单识别出的包类型（非 LLM 入参），让 ADS 端把纯 Touchstone 直接
        # 判 unsupported 更硬；不在 LLM schema 白名单里，仅后端内部传递。
        params["package_kind"] = kind
    kit_root = _resolve_native_kit_root(workspace, rec)
    if kit_root:
        params["kit_root"] = kit_root
    library = str(args.get("library") or "").strip() or _first_attached_library(rec)
    if library:
        params["library"] = library
    for key in ("category", "view"):
        value = args.get(key)
        if value:
            params[key] = value

    result = tools_mod.call(cfg, "open_vendor_palette", params)
    if not isinstance(result, dict):
        result = {"ok": False, "outcome": "failed",
                  "error": f"ADS 返回了无法解析的结果类型：{type(result).__name__}",
                  "raw": str(result)[:500]}
    # 结果里带上清单视图供面板渲染；**不改动 ADS 返回的关键字段**。
    view = dict(result)
    view.setdefault("workspace", workspace)
    view.setdefault("package_id", package_id)
    view["package_kind"] = kind
    view["package"] = _record_view(rec, include_models=False)
    return view


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
# 挂接相关的路径/库名判定都搬到了 model_orchestration（导入流水线在那里）。
# 刻意**不在本模块留第二份**：两处规则迟早会分叉，而"哪套规则生效"取决于
# 调用从哪个入口进来 —— 那正是"按钮与 LLM 行为不一致"的来源。


# ---------------------------------------------------------------------------
# 派发表（agent.py 按名字取）
# ---------------------------------------------------------------------------

LOCAL_HANDLERS = {
    "list_model_packages": list_model_packages,
    "list_shared_model_packages": list_shared_model_packages,
    "search_shared_models": search_shared_models,
    "inspect_model_package": inspect_model_package,
}
ADS_HANDLERS = {
    "import_model_package": import_model_package,
    "import_shared_model_package": import_shared_model_package,
    "list_vendor_models": list_vendor_models,
    "get_vendor_model_info": get_vendor_model_info,
    "validate_model_import": validate_model_import,
    "open_vendor_palette": open_vendor_palette,
}


def is_model_tool(name: str) -> bool:
    return name in LOCAL_HANDLERS or name in ADS_HANDLERS
