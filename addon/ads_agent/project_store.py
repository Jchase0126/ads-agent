"""项目会话的原子保存与备份恢复（不依赖 Qt）。"""

from __future__ import annotations

import json
import os
import secrets
import time


def _read_valid(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return None
    if not data["projects"]:
        return None
    return data


def load(path: str) -> tuple[dict | None, bool]:
    """返回 (会话, 是否来自备份)。"""
    data = _read_valid(path)
    if data is not None:
        return data, False
    data = _read_valid(path + ".bak")
    return data, data is not None


def _replace(src: str, dst: str) -> None:
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01 * (attempt + 1))


def _atomic_bytes(path: str, content: bytes) -> None:
    tmp = f"{path}.{secrets.token_hex(8)}.tmp"
    try:
        with open(tmp, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(path: str, data: dict) -> None:
    """完整写入后替换主文件；仅将有效旧文件更新为备份。"""
    content = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if _read_valid(path) is not None:
        with open(path, "rb") as stream:
            previous = stream.read()
        _atomic_bytes(path + ".bak", previous)
    _atomic_bytes(path, content)
