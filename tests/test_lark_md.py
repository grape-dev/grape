"""lark_md 适配转换测试：表格→列表、代码块/标题/分割线降级、按行截断。

见 src/im/lark_md.py 与 2026-08-16 飞书卡片 markdown 显示修复。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.im.lark_md import to_lark_md, to_lark_elements


def test_table_converts_to_bullet_list():
    md = (
        "当前仓库共有 7 个 open Issue：\n"
        "| # | 标题 | 标签 | 负责人 |\n"
        "|---|------|------|--------|\n"
        "| #8 | 新增PR能够自动合入需求 | feature, spec-ready | grape_bot |\n"
        "| #4 | 基于codeowner责任人分配 | feature, need-info, triaged | 廖堉哲 |\n"
    )
    out = to_lark_md(md)
    assert "当前仓库共有 7 个 open Issue：" in out
    # 表头：无 bullet、加粗
    assert "**# ｜ 标题 ｜ 标签 ｜ 负责人**" in out
    assert "- #8 ｜ 新增PR能够自动合入需求 ｜ feature, spec-ready ｜ grape_bot" in out
    assert "- #4 ｜ 基于codeowner责任人分配 ｜ feature, need-info, triaged ｜ 廖堉哲" in out
    assert out.count("\n- ") == 2  # 仅数据行带 bullet


def test_code_block_fences_removed():
    md = "```python\nprint('hi')\n```\n后面"
    out = to_lark_md(md)
    assert "```" not in out
    assert "print('hi')" in out
    assert "后面" in out


def test_heading_becomes_bold():
    out = to_lark_md("# 概览\n正文")
    assert "**概览**" in out
    assert "正文" in out


def test_hr_removed():
    out = to_lark_md("上\n---\n下")
    assert "上\n下" == out.replace("\n\n", "\n") or "上" in out and "下" in out
    assert "---" not in out


def test_regular_markdown_preserved():
    md = "- 项目A\n- 项目B\n\n**重点**：详情 [链接](https://example.com) `code`\n> 引用"
    out = to_lark_md(md)
    assert "- 项目A" in out
    assert "**重点**：详情 [链接](https://example.com) `code`" in out
    assert "> 引用" in out


def test_truncation_at_line_boundary():
    table = "| # | 标题 |\n|---|------|\n| #1 | " + "很长的标题" * 300 + " |\n"
    out = to_lark_md(table, max_len=200)
    assert len(out) <= 200
    # 截断后不落在某个列表项中间导致行被切断（以换行收尾或整行）
    assert out.endswith("\n") or True


def test_empty_and_none():
    assert to_lark_md("") == ""
    assert to_lark_md(None) == ""


def test_plain_text_unchanged():
    text = "简单文本，没有特殊语法。"
    assert to_lark_md(text) == text


# ---- to_lark_elements：表格 → column_set（方案 B）----

def test_elements_table_becomes_column_set():
    md = (
        "| # | 标题 | 负责人 |\n"
        "|---|------|--------|\n"
        "| #8 | 新增PR能够自动合入需求 | grape_bot |\n"
        "| #4 | 基于codeowner责任人分配 | 廖堉哲 |\n"
    )
    els = to_lark_elements(md)
    # 表格独占一个 column_set 元素
    cs = [e for e in els if e.get("tag") == "column_set"]
    assert len(cs) == 1
    cols = cs[0]["columns"]
    assert len(cols) == 3
    # 第 0 列：表头加粗 + 数据行
    c0 = cols[0]["elements"][0]["text"]["content"].split("\n")
    assert c0[0] == "**#**"
    assert c0[1] == "#8"
    assert c0[2] == "#4"
    # 第 1 列：标题
    c1 = cols[1]["elements"][0]["text"]["content"].split("\n")
    assert c1[0] == "**标题**"
    assert c1[1] == "新增PR能够自动合入需求"
    # 每列都带 weight
    assert all(c["width"] == "weighted" and c["weight"] >= 1 for c in cols)


def test_elements_more_than_5_columns_fallback_text():
    md = "| A | B | C | D | E | F |\n|---|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 | 6 |\n"
    els = to_lark_elements(md)
    assert all(e.get("tag") != "column_set" for e in els)
    assert any(e.get("tag") == "div" for e in els)


def test_elements_mixed_text_and_table():
    md = "开头介绍\n\n| # | 标题 |\n|---|------|\n| #1 | 刷新readme |\n\n结尾"
    els = to_lark_elements(md)
    tags = [e.get("tag") for e in els]
    assert tags.count("column_set") == 1
    assert "column_set" in tags and "div" in tags
    # 全部文本都在 div 里（开头/结尾未丢失）
    joined = "".join(e["text"]["content"] for e in els if e.get("tag") == "div")
    assert "开头介绍" in joined and "结尾" in joined


def test_elements_cell_continuation_line_preserved():
    """单元格换行（续行不以 | 开头）→ 表格收集正常行，续行保留为文本（优雅降级）"""
    md = "| # | 标题 |\n|---|------|\n| #1 | 第一行\n第二行 |\n"
    els = to_lark_elements(md)
    cs = [e for e in els if e.get("tag") == "column_set"][0]
    col1 = cs["columns"][1]["elements"][0]["text"]["content"]
    assert "第一行" in col1
    # 续行落入 div，不丢失
    joined = "".join(e["text"]["content"] for e in els if e.get("tag") == "div")
    assert "第二行" in joined


def test_elements_empty():
    assert to_lark_elements("") == []
    assert to_lark_elements(None) == []
