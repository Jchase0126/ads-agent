# -*- coding: utf-8 -*-
"""数据手册 PDF 的纯标准库文本抽取器。

为什么不用 pymupdf / pdfplumber：后端刻意只用标准库（工程约定，与
tests/_harness 的"后端零第三方依赖"一致），且任何一侧解释器（后端
Python / ADS 内嵌 Python）都不能假设用户装过 PDF 库。

采用有限的流扫描式文本预览：不解析
xref、不构建对象树，直接在字节流里找 ``stream`` 段，解压 FlateDecode，
提取文本算子（Tj / TJ / ' / "），按出现顺序拼接、遇定位算子换行。
文本顺序和参数表列归属不保证正确，下游必须保留引用并复核语义。

能力边界（如实标注，不臆测）：
  * 简单字体按 latin-1 解码；未解析字体映射可能丢失负号或微单位，
    此类文本可以预览但不能直接保存为建模参数；
  * 扫描版（整页图片）抽不出文本，同样按"无文本层"上报；
  * 本抽取器不做曲线数字化或拟合，也不提供可靠的原 PDF 页码。
"""

from __future__ import annotations

import re
import zlib

#: 抽取文本低于该密度（字符/页）按"无文本层"上报（扫描件/纯图 PDF）
_MIN_CHARS_PER_PAGE = 30
#: 单段流解压后的安全上限（防炸弹流：手册场景正常内容流远小于此）
_MAX_STREAM_BYTES = 8 << 20
_UNSUPPORTED_FONT_RE = re.compile(rb"/(?:Identity-[HV]|ToUnicode|Differences|ObjStm)\b")

_STREAM_RE = re.compile(
    rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_TJ_ARRAY_RE = re.compile(
    r"\(((?:\\.|[^\\()])*)\)\s*(-?\d+(?:\.\d+)?)?\s*", re.S)
_STRING_RE = re.compile(r"\((?:\\.|[^\\()])*\)", re.S)
# 算子匹配必须带 pos 调用（.match(content, i)）：用 content[i:] 切片会把
# 剩余内容整体拷贝一遍，大内容流上是 O(n²)（BAP64 手册实测 35 秒就栽在这）
_OP_RE = re.compile(
    r"\s*(Tj|TJ|Td|TD|Tm|T\*|BT|ET|'|\"|[-\d.\s]+)")
#: 单行"可读字符"占比低于此值视为字体映射/子集编码的乱码行，丢弃
_GARBBLE_THRESHOLD = 0.7


def _looks_garbled(line: str) -> bool:
    """乱码行启发：子集字体/字符映射流抽出的行充满高位字符与控制符。"""
    if not line:
        return False
    readable = sum(1 for c in line
                   if c in "\n\t" or 32 <= ord(c) < 127)
    return readable / len(line) < _GARBBLE_THRESHOLD


def _unescape(raw: str) -> str:
    """PDF 字符串字面量转义：\\( \\) \\\\ \\n \\r \\t \\b \\f \\ddd。"""
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\" or i + 1 >= len(raw):
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in "01234567":
            j = i + 1
            digits = ""
            while j < len(raw) and len(digits) < 3 and raw[j] in "01234567":
                digits += raw[j]
                j += 1
            try:
                out.append(chr(int(digits, 8) & 0xFF))
            except ValueError:
                out.append(nxt)
            i = j
            continue
        out.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(nxt, nxt))
        i += 2
    return "".join(out)


def _decode_str_bytes(raw: str) -> str:
    """字符串字面量 -> 文本。简单字体按 latin-1（覆盖英文手册）。"""
    return raw.encode("latin-1", "replace").decode("latin-1")


def _pdf_whitespace_norm(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def _extract_content_stream(content: str) -> list:
    """一个内容流 -> 文本行列表（按出现顺序，定位算子开新行）。

    只认文本相关算子，其余（图形/颜色）跳过。TJ 数组里的大数值间隔
    视作字间空隙补空格 —— 参数表的列对齐靠它保留可读性。
    """
    lines: list = []
    cur = []

    def flush():
        line = _pdf_whitespace_norm("".join(cur))
        if line:
            lines.append(line)
        cur.clear()

    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == "[":
            # TJ's operand occurs BEFORE its operator. Read the array here,
            # keeping negative spacing adjustments as gaps between columns.
            j = i + 1
            parts = []
            while j < n and content[j] != "]":
                string = _STRING_RE.match(content, j)
                if string:
                    parts.append(_decode_str_bytes(_unescape(string.group(0)[1:-1])))
                    j = string.end()
                    continue
                number = re.match(r"[-+]?\d+(?:\.\d+)?", content[j:j + 64])
                if number:
                    if float(number.group(0)) < -200:
                        parts.append(" ")
                    j += number.end()
                    continue
                j += 1
            operator = re.match(r"\s*TJ\b", content[j + 1:j + 32]) if j < n else None
            if operator:
                cur.append("".join(parts))
                i = j + 1 + operator.end()
            else:
                i = min(j + 1, n)
            continue
        if ch == "(":
            m = _STRING_RE.match(content, i)
            if not m:
                i += 1
                continue
            inner = m.group(0)[1:-1]
            cur.append(_decode_str_bytes(_unescape(inner)))
            i = m.end()
            continue
        # 算子边界：从当前位置读一个 token（标识符或数字）—— 带 pos
        # 匹配，不做切片拷贝
        m = _OP_RE.match(content, i)
        if not m:
            i += 1
            continue
        token = m.group(1)
        if token in ("Tj", "'"):
            if token == "'":
                flush()
            # 前面必然刚拼过一个字符串（上面的 "(" 分支），Tj 直接吃掉
            i = m.end()
            continue
        if token == '"':
            # (..) (..) " ：第二个串已拼入 cur，这里当 Tj 用
            i = m.end()
            continue
        if token == "TJ":
            i = m.end()
            continue
        if token in ("Td", "TD", "T*", "Tm", "BT", "ET"):
            # 定位/文本块边界：手册里几乎每个新单元格都有定位，
            # 统一换行会把同一行拆碎 —— 只在 TD/Tm（行移动）与 BT 换行
            flush()
            i = m.end()
            continue
        i = m.end()
    flush()
    return [ln for ln in lines if not _looks_garbled(ln)]


def extract_text(data: bytes) -> dict:
    """抽取 PDF 文本。返回结构化结果，绝不抛"解析失败"以外的异常。

    返回：
      page_count  —— 含文本块的内容流数（近似页数，只用于密度判断）
      lines       —— 按序文本行
      text        —— 行拼接（\\n 分隔）
      has_text_layer —— 字符密度判断；False = 扫描件/图形页/编码不支持
      note        —— 人类可读的状态说明（进收据与日志）
    """
    page_count = 0
    # Object streams can hide font dictionaries from a raw-byte scan. Until
    # this parser resolves those objects, conservatively block numeric use.
    complex_fonts = bool(_UNSUPPORTED_FONT_RE.search(data))
    lines: list = []
    for m in _STREAM_RE.finditer(data):
        raw = m.group(1)
        try:
            decoder = zlib.decompressobj()
            content_bytes = decoder.decompress(raw, _MAX_STREAM_BYTES + 1)
            # Bound expansion before allocating the full inflated stream.
            if len(content_bytes) > _MAX_STREAM_BYTES or decoder.unconsumed_tail:
                continue
        except zlib.error:
            # 未压缩流（个别生成器）：只要像内容流（含 BT/Tj）也收
            content_bytes = (raw if len(raw) <= _MAX_STREAM_BYTES
                             and (b"BT" in raw or b"Tj" in raw) else b"")
        if not content_bytes or len(content_bytes) > _MAX_STREAM_BYTES:
            continue
        complex_fonts = complex_fonts or bool(_UNSUPPORTED_FONT_RE.search(content_bytes))
        if b"BT" not in content_bytes:
            continue
        page_count += 1
        try:
            content = content_bytes.decode("latin-1")
        except UnicodeDecodeError:
            continue
        page_lines = _extract_content_stream(content)
        if page_lines:
            lines.append("")  # 页分隔（空行）
            lines.extend(page_lines)

    text = "\n".join(lines).strip()
    chars = len(re.sub(r"\s", "", text))
    approx_pages = max(page_count, 1)
    has_text_layer = chars >= _MIN_CHARS_PER_PAGE * approx_pages or chars >= 200
    if not text:
        note = "未抽到任何文本：可能是扫描版（整页图片）或加密 PDF"
    elif not has_text_layer:
        note = (f"文本量过低（{chars} 字符 / 约 {page_count} 页）："
                "疑似扫描件或非拉丁编码手册，参数提取不可靠")
    else:
        note = f"抽取 {chars} 字符 / 约 {page_count} 个文本页"
    # This parser does not apply font encodings/CMaps. Readable ASCII density
    # does not prove numeric signs or micro-unit glyphs survived extraction.
    parameter_extraction_safe = bool(has_text_layer and not complex_fonts)
    if complex_fonts:
        note += "；包含未解析的字体映射，负号/微单位等可能缺失，禁止直接用于建模参数"
    return {
        "page_count": page_count,
        "lines": lines,
        "text": text,
        "chars": chars,
        "has_text_layer": has_text_layer,
        "parameter_extraction_safe": parameter_extraction_safe,
        "encoding_warning": complex_fonts,
        "note": note,
    }


# ---------------------------------------------------------------------------
# 完整解析引擎（页树 / 对象流 / 字体编码）
# ---------------------------------------------------------------------------
#
# 纯标准库流扫描（上面的 extract_text）不解析页树和字体 CMap，只能当受限
# 降级。真正的解析交给 PyMuPDF（MuPDF 绑定）：它读真实页树、解对象流、
# 按字体 ToUnicode/CMap 解码文本 —— 负号（U+2212）、µ、度数等字形才能保住。
#
# 可靠性按**行**标注：字体映射缺失的个别字形（如本样本 IntDutch 字体里的
# µAdc）会解成控制字符 —— 含这种字形的行标记 unreliable，保留原始字形，
# 绝不靠"器件常识"猜回 µ；该行的数值/单位须走 requires_review，其余行
# 不受牵连。这是审查要求的"页面/区域级可靠性"，不是全文一刀切。

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _pymupdf_available() -> bool:
    try:
        import pymupdf  # noqa: F401
    except Exception:  # noqa: BLE001 — 任何导入失败都按不可用处理
        return False
    return True


def _line_reliability(text: str) -> tuple[bool, list]:
    """一行文本是否可信；返回 (reliable, 未知字形码点列表)。"""
    bad = sorted({ord(ch) for ch in _CONTROL_RE.findall(text)})
    return (not bad), bad


def _pymupdf_parse(data: bytes) -> dict:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass:
            if not doc.authenticate(""):
                return {
                    "engine": "pymupdf", "page_count": doc.page_count,
                    "pages": [], "lines": [], "text": "", "chars": 0,
                    "has_text_layer": False, "parameter_extraction_safe": False,
                    "page_numbers_available": True,
                    "note": "PDF 已加密，无法读取文本；请提供未加密的文件",
                }
        pages = []
        lines: list = []
        unknown_glyph_lines = 0
        for index in range(doc.page_count):
            page = doc[index]
            raw = page.get_text("dict")
            page_lines = []
            try:
                n_images = len(page.get_images(full=False))
            except Exception:  # noqa: BLE001 — 图片统计失败不影响文本
                n_images = None
            for block in raw.get("blocks") or []:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines") or []:
                    text = "".join(str(span.get("text") or "")
                                   for span in line.get("spans") or [])
                    if not text.strip():
                        continue
                    reliable, unknown = _line_reliability(text)
                    if unknown:
                        unknown_glyph_lines += 1
                    entry = {"text": text, "bbox": [round(v, 2) for v in line["bbox"]],
                             "reliable": reliable}
                    if unknown:
                        entry["unknown_glyphs"] = [hex(u) for u in unknown]
                    page_lines.append(entry)
            chars = sum(1 for ln in page_lines for ch in ln["text"] if not ch.isspace())
            pages.append({
                "page": index + 1,
                "width": round(float(page.rect.width), 2),
                "height": round(float(page.rect.height), 2),
                "has_text": bool(page_lines),
                "chars": chars,
                "images": n_images,
                "line_count": len(page_lines),
                "lines": page_lines,
            })
            if page_lines:
                lines.append("")  # 页分隔空行，与降级解析的输出风格一致
                lines.extend(ln["text"] for ln in page_lines)
        text = "\n".join(lines).strip()
        chars = len(re.sub(r"\s", "", text))
        has_text = chars >= _MIN_CHARS_PER_PAGE * max(doc.page_count, 1) or chars >= 200
        bad_pages = [p["page"] for p in pages
                     if p["line_count"] and any(not ln["reliable"] for ln in p["lines"])]
        note = (f"完整解析 {doc.page_count} 页（页树），提取 {chars} 字符"
                f"（引擎 pymupdf {pymupdf.__version__}）")
        if unknown_glyph_lines:
            note += (f"；{unknown_glyph_lines} 行含未映射字形（保留原始码点，"
                     f"涉及页 {bad_pages}），这些行需复核后才能作建模依据")
        return {
            "engine": "pymupdf", "page_count": doc.page_count,
            "pages": pages, "lines": lines, "text": text, "chars": chars,
            "has_text_layer": has_text,
            "parameter_extraction_safe": has_text,
            "page_numbers_available": True,
            "unknown_glyph_lines": unknown_glyph_lines,
            "pages_with_unknown_glyphs": bad_pages,
            "note": note,
        }
    finally:
        doc.close()


def extract_document(data: bytes, runtime_python: str = "",
                     pdf_path: str = "") -> dict:
    """解析 PDF：优先进程内 PyMuPDF，其次配置的运行时子进程，最后降级扫描。

    三个引擎的可靠性语义：

    * ``pymupdf`` —— 真实页数 + 字体编码解码 + 行级可靠性；参数保存走
      **逐条**行证据门禁（在 datasheet_models 里）；
    * ``runtime`` —— 同 pymupdf，但在独立解释器里跑（后端环境缺库时）；
    * ``stream_scan`` —— 旧版受限降级，语义不变（复杂字体映射一律禁止
      数值使用）。
    """
    if _pymupdf_available():
        try:
            return _pymupdf_parse(data)
        except Exception as exc:  # noqa: BLE001 — 损坏文件降级而不是崩溃
            fallback = extract_text(data)
            fallback["engine"] = "stream_scan"
            fallback["page_numbers_available"] = False
            fallback["note"] += f"；完整解析引擎失败已降级：{type(exc).__name__}: {exc}"
            return fallback
    if runtime_python and pdf_path:
        result = _runtime_parse(runtime_python, pdf_path)
        if result is not None:
            return result
    fallback = extract_text(data)
    fallback["engine"] = "stream_scan"
    fallback["page_numbers_available"] = False
    return fallback


def _runtime_parse(runtime_python: str, pdf_path: str) -> dict | None:
    """在配置的运行时解释器里跑同一份解析代码（backend/pdf_worker.py）。"""
    import json
    import os
    import subprocess
    import sys

    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_worker.py")
    if not os.path.isfile(worker) or not os.path.isfile(pdf_path):
        return None
    try:
        proc = subprocess.run(
            [runtime_python, worker, "--backend", os.path.dirname(worker), pdf_path],
            capture_output=True, timeout=120,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("engine") != "pymupdf":
        return None
    return payload


def get_page_drawings_summary(pdf_path: str, page_number: int,
                              max_paths: int = 400) -> dict:
    """一页的矢量图形清单（供曲线提取前确认 path 索引）。"""
    if not _pymupdf_available():
        raise RuntimeError("矢量图形提取需要 pymupdf 运行时；当前环境不可用")
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"页码超出范围：1..{doc.page_count}")
        page = doc[page_number - 1]
        drawings = page.get_drawings()
        out = {"page": page_number, "path_count": len(drawings), "paths": []}
        for index, item in enumerate(drawings[:max_paths]):
            rect = item.get("rect")
            n_points = sum(
                len(entry[1:]) if entry[0] in ("l", "c") else 4
                for entry in item.get("items") or [])
            out["paths"].append({
                "index": index, "item_count": len(item.get("items") or []),
                "n_points": n_points,
                "bbox": [round(v, 2) for v in rect] if rect else None,
                "stroke": bool(item.get("color")),
                "width": item.get("width"),
            })
        if len(drawings) > max_paths:
            out["truncated"] = True
            out["note"] = f"仅列出前 {max_paths} 条路径（共 {len(drawings)} 条）"
        return out
    finally:
        doc.close()


def get_page_drawing_paths(pdf_path: str, page_number: int,
                           indices: list) -> dict:
    """取指定 path 索引的折线顶点（贝塞尔按 51 段展平，与样本工具一致）。"""
    if not _pymupdf_available():
        raise RuntimeError("矢量图形提取需要 pymupdf 运行时；当前环境不可用")
    import pymupdf

    want = set()
    for value in indices:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("path 索引必须是非负整数")
        want.add(value)
    doc = pymupdf.open(pdf_path)
    try:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"页码超出范围：1..{doc.page_count}")
        drawings = doc[page_number - 1].get_drawings()
        out = {"page": page_number, "paths": {}}
        for index in sorted(want):
            if index >= len(drawings):
                raise ValueError(f"path 索引 {index} 不存在（该页共 {len(drawings)} 条）")
            points: list = []
            for entry in drawings[index].get("items") or []:
                op = entry[0]
                if op == "l":
                    points.extend([(float(entry[1].x), float(entry[1].y)),
                                   (float(entry[2].x), float(entry[2].y))])
                elif op == "c":
                    ctrl = [(float(p.x), float(p.y)) for p in entry[1:5]]
                    for step in range(51):
                        t = step / 50.0
                        u = 1.0 - t
                        x = (u * u * u * ctrl[0][0] + 3 * u * u * t * ctrl[1][0]
                             + 3 * u * t * t * ctrl[2][0] + t * t * t * ctrl[3][0])
                        y = (u * u * u * ctrl[0][1] + 3 * u * u * t * ctrl[1][1]
                             + 3 * u * t * t * ctrl[2][1] + t * t * t * ctrl[3][1])
                        points.append((x, y))
                elif op == "re":
                    rect = entry[1]
                    points.extend([(rect.x0, rect.y0), (rect.x1, rect.y0),
                                   (rect.x1, rect.y1), (rect.x0, rect.y1),
                                   (rect.x0, rect.y0)])
                elif op == "qu":
                    quad = entry[1]
                    points.extend([(quad.ul.x, quad.ul.y), (quad.ur.x, quad.ur.y),
                                   (quad.lr.x, quad.lr.y), (quad.ll.x, quad.ll.y),
                                   (quad.ul.x, quad.ul.y)])
                else:
                    raise ValueError(f"path {index} 含不支持的绘图算子 {op!r}；"
                                     "请改选其它路径或人工核对")
            if not points:
                raise ValueError(f"path {index} 没有可提取的顶点")
            out["paths"][str(index)] = {
                "points": [[round(x, 3), round(y, 3)] for x, y in points],
                "bbox": [round(v, 2) for v in drawings[index]["rect"]],
            }
        return out
    finally:
        doc.close()


#: 注入对话的抽取文本上限（字符）。手册参数表通常在前几页；
#: 剩余内容由 _apply_context_budget 的总预算再兜底。
EXCERPT_LIMIT = 12000


def make_excerpt(result: dict, limit: int = EXCERPT_LIMIT) -> str:
    """抽取结果 -> 进对话的截断文本（带明确的省略标注）。"""
    text = (result or {}).get("text") or ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，仅保留前 {limit} 字符）"
