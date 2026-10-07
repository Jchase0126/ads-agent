"""Markdown → 纯文本 清洗器（聊天气泡显示用）。

气泡是 PlainText 的 QLabel，LLM 回复里的 **加粗**、`代码`、# 标题会原样
露出来，观感很差。本模块把常见 Markdown 结构清成适合气泡阅读的纯文本：

    **粗体** / `代码` / ~~删除线~~   → 去掉修饰符号，只留内容
    # 标题                          → 只留标题文字
    - / * 列表项                    → • 列表项（有序列表原样保留）
    ``` 代码块 ```                  → 去掉围栏行，内容逐行保留
    表格                            → 去掉分隔行与外侧竖线，保留 "列 | 列"
    [文字](url)                     → 文字 (url)
    --- 分隔线 / > 引用符            → 去掉

只做显示层清洗：历史记录仍存 LLM 原文，供多轮上下文使用。

用法::

    from mdplain import to_plain
    label 显示前: text = to_plain(text)
"""

import re

# 预编译（模块级，气泡逐条渲染时不用反复编译）
_RE_CRLF = re.compile(r"\r\n?")
_RE_TRAIL_SP = re.compile(r"[ \t]+$")
_RE_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*(\S.*)?$")
_RE_HEADING = re.compile(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$")
_RE_HR = re.compile(r"^\s*(?:=+|[-*_]{3,})\s*$")
_RE_QUOTE = re.compile(r"^\s*>+\s?")
_RE_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RE_TASK = re.compile(r"^\[( |x|X)\]\s*")
_RE_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_RE_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_RE_CODE_SPAN = re.compile(r"(`+)([^`]+)\1")
_RE_STRIKE = re.compile(r"~~(?!\s)([^~\n]+?)(?<!\s)~~")
_RE_BOLD3 = re.compile(r"\*\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*\*")
_RE_BOLD_STAR = re.compile(r"\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*")
_RE_ITALIC_STAR = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")
_RE_BOLD_US = re.compile(r"__(?!_)([^_\n]+?)__(?!_)")
_RE_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RE_TABLE_SEP_CELL = re.compile(r"^\s*:?-+:?\s*$")
_RE_PIPE_TRIM = re.compile(r"^\s*\|(.*)\|\s*$")
_RE_PIPE_GAP = re.compile(r"[ \t]{2,}")
_RE_BLANK_RUN = re.compile(r"\n{3,}")

_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _strip_inline(text: str) -> str:
    """行内清洗：先摘出 `代码`（内容原样保护），再处理其余标记。"""
    kept: list[str] = []

    def _keep(m: re.Match) -> str:
        kept.append(m.group(2))
        return f"\x00{len(kept) - 1}\x00"

    text = _RE_CODE_SPAN.sub(_keep, text)
    text = _RE_IMG.sub(lambda m: m.group(1) or "", text)

    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if not label or label == url:
            return url
        return f"{label} ({url})"

    text = _RE_LINK.sub(_link, text)
    text = _RE_AUTOLINK.sub(r"\1", text)
    text = _RE_STRIKE.sub(r"\1", text)
    text = _RE_BOLD3.sub(r"\1", text)
    text = _RE_BOLD_STAR.sub(r"\1", text)
    text = _RE_ITALIC_STAR.sub(r"\1", text)

    def _bold_us(m: re.Match) -> str:
        # __粗体__ 只在内容像强调（含空格或中文）时才去符号，
        # 避免误伤 __init__ 这类双下划线标识符
        inner = m.group(1)
        if " " in inner or _CJK.search(inner):
            return inner
        return m.group(0)

    text = _RE_BOLD_US.sub(_bold_us, text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)
    return text


def _is_table_sep(line: str) -> bool:
    """表格分隔行：至少一个 |，且每个单元格都是 :---: 形态。"""
    if "|" not in line:
        return False
    cells = line.strip().strip("|").split("|")
    return bool(cells) and all(_RE_TABLE_SEP_CELL.match(c) for c in cells)


def _table_row(line: str) -> str:
    m = _RE_PIPE_TRIM.match(line)
    body = m.group(1) if m else line.strip()
    body = _RE_PIPE_GAP.sub(" ", body.replace("\t", " "))
    return body.strip()


def to_plain(text: str) -> str:
    """把一段 Markdown 文本清成适合纯文本气泡显示的排版。"""
    if not text:
        return text
    if not any(ch in text for ch in "*`#_>~[|\r\n-+"):
        return text  # 没有任何 Markdown 特征，原样返回

    lines = _RE_CRLF.sub("\n", text).split("\n")
    out: list[str] = []
    in_fence = False

    for line in lines:
        if in_fence:
            if _RE_FENCE.match(line):  # 围栏结束行
                in_fence = False
            else:
                out.append(_RE_TRAIL_SP.sub("", line))
            continue
        m = _RE_FENCE.match(line)
        if m:  # 围栏开始行（连语言标注一起丢）
            in_fence = True
            continue
        if _RE_HEADING.match(line):
            line = _RE_HEADING.sub(r"\1", line)
        elif _RE_HR.match(line):
            out.append("")
            continue
        line = _RE_QUOTE.sub("", line)
        if _is_table_sep(line):
            continue
        if line.lstrip().startswith("|") or line.count("|") >= 2:
            line = _table_row(line)
        else:
            bm = _RE_BULLET.match(line)
            if bm:
                body = _RE_TASK.sub(
                    lambda t: "☑ " if t.group(1).lower() == "x" else "☐ ", bm.group(2)
                )
                line = f"{bm.group(1)}• {body}"
        out.append(_strip_inline(_RE_TRAIL_SP.sub("", line)))

    result = "\n".join(out)
    result = _RE_BLANK_RUN.sub("\n\n", result)
    return result.strip("\n")
