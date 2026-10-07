"""Markdown → 纯文本气泡清洗测试（不需要 ADS；最后一段冒烟需要 PySide6）。

覆盖 addon/ads_agent/mdplain.to_plain 的显示层清洗：

* 修饰符号剥离：**加粗** / *斜体* / `代码` / ~~删除线~~ / # 标题
* 结构转换：- 列表 → • 列表、``` 围栏 → 内容保留、表格去分隔行、
  --- 分隔线、> 引用、[链接](url)
* 误伤保护：__init__ / a_b_c 这类下划线标识符不能被当强调剥掉
* 快路径：纯文本原样返回
* 冒烟：真实 BubbleRow（offscreen）里 assistant 文本不再出现 ** 和 `

运行::

    python tests/test_md_plain.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import add_path, contains, eq, not_contains, ok, run  # noqa: E402

ADDON = add_path("addon", "ads_agent")

import mdplain  # noqa: E402

to_plain = mdplain.to_plain


def test_bold_and_code_strip():
    eq(to_plain("**没有打开任何 ADS 工作区**。"), "没有打开任何 ADS 工作区。",
       "粗体符号剥离")
    eq(to_plain("`get_workspace_info` 返回 workspace_open: false"),
       "get_workspace_info 返回 workspace_open: false", "行内代码反引号剥离")
    eq(to_plain("先跑 `save(d)`，再 ~~检查~~ 确认。"), "先跑 save(d)，再 检查 确认。",
       "代码与删除线同段剥离")


def test_heading_hr_quote():
    eq(to_plain("## 需要你做一步"), "需要你做一步", "二级标题只留文字")
    eq(to_plain("# 一级\n正文"), "一级\n正文", "一级标题只留文字")
    eq(to_plain("上面\n\n---\n\n下面"), "上面\n\n下面", "分隔线变空行")
    eq(to_plain("> 引用的话"), "引用的话", "引用符剥离")


def test_lists():
    src = "- 读完工作区路径与已挂载的库\n- 列出其中的设计\n1. 先打开\n2. 再告诉我"
    eq(to_plain(src),
       "• 读完工作区路径与已挂载的库\n• 列出其中的设计\n1. 先打开\n2. 再告诉我",
       "无序列表转 •，有序列表原样")
    eq(to_plain("  - 缩进项"), "  • 缩进项", "缩进保留")


def test_code_fence():
    src = "做法如下：\n```python\nprint(pins(ML1))\n```\n完成。"
    eq(to_plain(src), "做法如下：\nprint(pins(ML1))\n完成。", "围栏行丢弃、内容保留")
    eq(to_plain("```\nraw **text** kept\n```"), "raw **text** kept",
       "围栏内容不做行内清洗")


def test_table():
    src = ("| 指标 | 值 |\n| --- | --- |\n| S11 | -9.91 dB |\n| S21 | -3.25 dB |")
    got = to_plain(src)
    eq(got, "指标 | 值\nS11 | -9.91 dB\nS21 | -3.25 dB", "表格去分隔行与外侧竖线")


def test_link_and_image():
    eq(to_plain("[文档](https://example.com/x) 在这里"),
       "文档 (https://example.com/x) 在这里", "链接转 文字 (url)")
    eq(to_plain("![图](https://example.com/a.png)"), "图", "图片留 alt")
    eq(to_plain("<https://example.com>"), "https://example.com", "尖括号自动链接")


def test_identifier_protection():
    eq(to_plain("用 `__init__` 初始化"), "用 __init__ 初始化",
       "反引号里的 __init__ 原样保留")
    eq(to_plain("调用 __init__ 方法"), "调用 __init__ 方法",
       "裸 __init__ 不被当强调剥掉")
    eq(to_plain("a_b_c 与 get_workspace_info 不变"), "a_b_c 与 get_workspace_info 不变",
       "snake_case 不变")
    eq(to_plain("这一步 __很重要__ 不要漏"), "这一步 很重要 不要漏",
       "含空格的 __强调__ 正常剥离")
    eq(to_plain("这个 __关键__"), "这个 关键", "含中文的 __强调__ 正常剥离")


def test_fast_path_and_cleanup():
    eq(to_plain("普通一句话，没有标记。"), "普通一句话，没有标记。", "纯文本原样返回")
    eq(to_plain("a\n\n\n\n\nb"), "a\n\nb", "连续空行收敛")
    eq(to_plain("\n\n首尾空行\n\n"), "首尾空行", "首尾空行裁掉")
    eq(to_plain(""), "", "空串原样")
    eq(to_plain("尾随空格   \n下一行"), "尾随空格\n下一行", "行尾空格裁掉")


def test_screenshot_sample():
    src = ("**没有打开任何 ADS 工作区**，所以列不出设计。\n\n"
           "- `get_workspace_info` 返回 workspace_open: false\n"
           "- list_designs 被 ADS 拒绝：'当前没有打开的工作区'\n\n"
           "**需要你做一步**：在 ADS 主界面 File → Open → Workspace 打开一个工作区。")
    got = to_plain(src)
    not_contains(got, "**", "截图样例不再出现 **")
    not_contains(got, "`", "截图样例不再出现反引号")
    contains(got, "• get_workspace_info 返回", "列表项转 •")


# ---------------- BubbleRow 冒烟（需要 PySide6） ----------------

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # noqa: E402
except ImportError as _e:  # pragma: no cover
    print(f"需要 PySide6 才能跑 BubbleRow 冒烟：{_e}")
    print("  pip install PySide6")
    sys.exit(2)

import panel  # noqa: E402

_APP = QApplication.instance() or QApplication([])

_PAL = {
    "ai_bubble": "#ffffff", "ai_text": "#111111", "ai_bubble_border": "#dddddd",
    "avatar_ai": "#888888", "avatar_ai_ring": "#aaaaaa",
    "user_bubble": "#ffffff", "user_text": "#111111",
    "user_bubble_border": "#dddddd", "user_bubble_to": None,
    "avatar_user": "#888888", "avatar_user_ring": "#aaaaaa", "avatar_user_to": None,
    "card_bg": "#f5f5f5", "card_border": "#dddddd", "subtle": "#666666",
    "error": "#cc0000", "hover": "#eeeeee", "text": "#111111",
    # ActivityRow 侧边滑块样式要用
    "accent": "#1a73e8", "accent_hover": "#1667d6", "accent_soft": "#e8f0fe",
}


def test_bubblerow_assistant_sanitized():
    row = panel.BubbleRow("assistant", "**粗** 与 `code`\n- 项目一", _PAL)
    text = row.label.text()
    not_contains(text, "**", "气泡不再出现 **")
    not_contains(text, "`", "气泡不再出现反引号")
    contains(text, "• 项目一", "气泡里列表转 •")
    eq(row._text, "粗 与 code\n• 项目一", "气泡文本即清洗后文本")


def test_bubblerow_user_untouched():
    row = panel.BubbleRow("user", "我输入的 **原样** `保留`", _PAL)
    contains(row.label.text(), "**", "用户气泡不做清洗")


def test_activityrow_scroll_kept_through_stream():
    """回归（2026-09-29 用户反馈：流式思考向上翻看会回弹）：setPlainText
    重建文档把内部滚动条归零，refresh/live_append 必须保持用户阅读位置；
    在底部时继续贴底跟随。"""
    from PySide6.QtWidgets import QListWidgetItem

    entry = {"reasoning": [f"第{i}段 " + "思考内容很长需要折行展示。" * 30
                           for i in range(6)],
             "tools": 5, "elapsed": 63}
    row = panel.ActivityRow(entry, _PAL, on_change=lambda: None)
    row.toggle.setChecked(True)          # 展开（触发 _toggle → refresh）
    row.reflow(600, QListWidgetItem())   # 设定宽度与展开高度（上限 300px）
    row.resize(600, 400)
    row.show()
    _APP.processEvents()
    bar = row.details.verticalScrollBar()
    ok(bar.maximum() > 0, f"长思考内容应有内部滚动，maximum={bar.maximum()}")
    # 场景 1：用户翻到中间 —— 流式刷新后位置保持不回弹。QPlainTextEdit
    # 的滚动按块吸附（setValue 会被吸附到段落边界），断言容差一个块高。
    keep = bar.maximum() // 2
    bar.setValue(keep)
    entry["elapsed"] = 64
    row.live_append()
    _APP.processEvents()
    ok(bar.value() > 0, f"流式刷新后不应弹回顶部，value={bar.value()}")
    ok(abs(bar.value() - keep) <= 80,
       f"流式刷新后阅读位置应保持（块吸附容差）：{bar.value()} vs {keep}")
    # 场景 2：用户在底部 —— 继续贴底跟随新内容
    bar.setValue(bar.maximum())
    entry["elapsed"] = 65
    row.live_append()
    _APP.processEvents()
    eq(bar.value(), bar.maximum(), "在底部时流式刷新应继续贴底")
    row.hide()


def test_bubblerow_width_adapts_to_wide_panel():
    """回归：AI 气泡上限要跟随面板宽度（旧实现固定 720px，宽面板里长回复
    被压在半幅以内，右侧浪费一大条空白）；用户气泡保持紧凑。"""
    long_text = "这是一段足够长的回复，会在气泡里折行成很多行。" * 20
    avail = 1300

    ai = panel.BubbleRow("assistant", long_text, _PAL)
    ai.reflow(avail, panel.QListWidgetItem())
    ok(ai.label.width() > panel.U.px(720),
       f"宽面板下 AI 气泡应超过旧上限 720px，实际 {ai.label.width()}")

    user = panel.BubbleRow("user", long_text, _PAL)
    user.reflow(avail, panel.QListWidgetItem())
    ok(user.label.width() <= panel.U.px(720) + panel.U.px(80),
       f"用户气泡保持紧凑上限，实际 {user.label.width()}")

    short = panel.BubbleRow("assistant", "好的，马上开始。", _PAL)
    short.reflow(avail, panel.QListWidgetItem())
    ok(short.label.width() < panel.U.px(300),
       f"短回复气泡按内容收缩，实际 {short.label.width()}")


if __name__ == "__main__":
    raise SystemExit(run(globals()))
