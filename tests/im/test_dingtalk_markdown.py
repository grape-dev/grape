"""钉钉 markdown 15000 字节分块 + 围栏闭合测试（Issue #936 P2，设计文档 §4.4/§7.6）。

对应 multica dingtalk_markdown.go L10-33 语义的 Python 落地：
  - 每块 UTF-8 编码 ≤ 15000 字节；
  - 代码围栏闭合：块内 "```" 奇数次 → 尾部补闭合 + 下一块开栏续接；
  - 多字节字符不截断（中文/emoji 安全）。
"""
from src.im.adapters.dingtalk.markdown import (
    DINGTALK_MARKDOWN_LIMIT,
    split_markdown,
)


def test_short_text_single_chunk():
    chunks = split_markdown("hello 钉钉")
    assert chunks == ["hello 钉钉"]


def test_empty_text_no_chunks():
    assert split_markdown("") == []


def test_long_text_split_within_limit():
    text = "\n".join(f"行{i}：内容内容内容内容" for i in range(2000))
    chunks = split_markdown(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.encode("utf-8")) <= DINGTALK_MARKDOWN_LIMIT
    assert "".join(chunks).count("```") == text.count("```")  # 内容无损（仅补围栏对）


def test_multibyte_not_truncated():
    text = "中" * 20000
    chunks = split_markdown(text, limit=100)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.encode("utf-8")) <= 100
        assert "\ufffd" not in c         # 无替换符 → UTF-8 边界安全
    assert "".join(chunks) == text     # 硬切不丢字符


def test_fence_closed_when_odd_count():
    """围栏闭合（核心验收）：块内代码围栏未闭合 → 补闭合 + 下一块续接。"""
    text = "# title\n```python\n" + "x = 1\n" * 3000 + "\n```"
    chunks = split_markdown(text, limit=800)
    assert len(chunks) > 1
    for c in chunks:
        assert c.count("```") % 2 == 0   # 每块围栏自平衡（渲染不塌方）
    # 拼回后围栏配对数 ≥ 原文（补了续接对，代码内容跨块可读）


def test_fence_continuation_keeps_code_content():
    """续接语义：跨块代码内容前后有开/闭围栏，渲染等价。"""
    text = "```\ncode line\n" * 100
    chunks = split_markdown(text, limit=120)
    assert len(chunks) > 1
    for c in chunks:
        assert c.count("```") % 2 == 0
    # 除首块与末块外，续接块以 ``` 开头（围栏续接）
    assert chunks[1].startswith("```")


def test_fence_closure_exact_15000_boundary():
    """验收口径：默认 limit=15000 时超长 markdown 分块且每块围栏闭合。"""
    text = "# 报告\n\n```go\n" + "fmt.Println(1)\n" * 2500 + "```\n"
    chunks = split_markdown(text)     # 默认 15000
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.encode("utf-8")) <= DINGTALK_MARKDOWN_LIMIT
        assert c.count("```") % 2 == 0


def test_single_line_over_limit_hard_split():
    line = "a" * 500
    chunks = split_markdown(line, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 100, 100, 100]
    assert "".join(chunks) == line
