"""PDF 原文件资产层 —— 按**内容 SHA-256** 持久化上传的原始字节。

为什么要有这一层
----------------
过去上传手册只保留抽取文本：解析器升级、字体修复、曲线数字化想要"重新
读一遍原始页面"时无能为力，document_id 只能取到当时的文本快照。

现在原始字节落在统一数据根目录：

    <data_root>/datasheets/files/<sha256[:2]>/<sha256>.pdf

规则：

* 内容寻址 —— 同一 PDF 重复上传**去重复用**，不产生第二份字节；
* 存储路径由服务生成，用户文件名只用于显示（记录在 documents 记录里）；
* 写入是原子的（临时文件 + ``os.replace``），并发上传同一文件不会写坏；
* 旧版纯文本记录没有对应资产 —— 查询时如实报告 ``source_unavailable``，
  绝不把文本摘要当成原始 PDF。

本模块只用标准库（后端零第三方依赖约定不变）。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading

import paths

#: 资产格式版本。目录结构或命名规则变化时递增。
ASSET_VERSION = 1

_SHA = 64
_LOCK = threading.Lock()

#: 单个 PDF 资产的大小上限（与服务端上传上限一致，双保险）。
MAX_ASSET_BYTES = 32 << 20


class AssetError(ValueError):
    pass


def _valid_sha(value) -> bool:
    if not isinstance(value, str) or len(value) != _SHA:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def assets_root() -> str:
    return os.path.join(paths.data_root(), "datasheets", "files")


def relative_path(sha256: str) -> str:
    """资产在数据根目录下的相对路径（存进记录，随数据根一起搬移）。"""
    if not _valid_sha(sha256):
        raise AssetError("无效的资产 SHA-256；请使用工具返回的 document_id")
    return os.path.join("datasheets", "files", sha256[:2], sha256 + ".pdf")


def absolute_path(sha256: str) -> str:
    return os.path.join(paths.data_root(), relative_path(sha256))


def store(data: bytes, expect_sha: str | None = None) -> dict:
    """原子写入并去重。``expect_sha`` 用于校验调用方已算过的哈希。"""
    if not isinstance(data, bytes) or not data:
        raise AssetError("PDF 原始字节为空")
    if len(data) > MAX_ASSET_BYTES:
        raise AssetError(f"PDF 超过资产大小上限（{MAX_ASSET_BYTES} 字节）")
    sha256 = hashlib.sha256(data).hexdigest()
    if expect_sha is not None and expect_sha != sha256:
        raise AssetError("调用方哈希与实际内容不一致；拒绝保存")
    target = absolute_path(sha256)
    with _LOCK:
        if os.path.exists(target):
            reused = True
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
            reused = False
    stat = os.stat(target)
    return {
        "sha256": sha256,
        "relative_path": relative_path(sha256),
        "absolute_path": target,
        "size_bytes": stat.st_size,
        "reused": reused,
        "asset_version": ASSET_VERSION,
    }


def asset_status(sha256: str) -> dict:
    """查询资产是否存在。旧记录返回 source_unavailable，不假装有原文件。"""
    if not _valid_sha(sha256):
        raise AssetError("无效的资产 SHA-256")
    target = absolute_path(sha256)
    if not os.path.isfile(target):
        return {"has_original": False, "source_status": "source_unavailable",
                "relative_path": relative_path(sha256),
                "reattach_hint": "原始 PDF 未存档（旧版上传只保留了文本）。"
                                 "请在聊天里重新发送同一 PDF 文件，上传后会自动补齐原文件资产。"}
    return {"has_original": True, "source_status": "available",
            "relative_path": relative_path(sha256),
            "absolute_path": target,
            "size_bytes": os.path.getsize(target),
            "asset_version": ASSET_VERSION}


def read_bytes(sha256: str) -> bytes:
    """按 document_id 重新读取原始字节（解析器升级 / 曲线提取都从这里拿）。"""
    status = asset_status(sha256)
    if not status.get("has_original"):
        raise AssetError(status.get("reattach_hint") or "原始 PDF 不可用")
    with open(status["absolute_path"], "rb") as stream:
        data = stream.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AssetError("资产内容与 SHA-256 不符（文件可能被外部改动）；请重新上传手册")
    return data
