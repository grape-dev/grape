"""Telegram 文本方言 —— 标准 Markdown → HTML + 4096/3900 分块。

选 HTML 而非 MarkdownV2（设计 §3.5）：
  - MarkdownV2 需要转义 `_ * [ ] ( ) ~ \\` > # + - = | { } . !`，用户内容极易打崩整条消息；
  - HTML 只需转义 `&` `<` `>`，容错面小（parse_mode=HTML）。

本模块只在出站边界把标准 Markdown 转成 Telegram 支持的 HTML 子集：
  `**粗**` / `__粗__` → `<b>`、`_斜_` → `<i>`、`~~删~~` → `<s>`、
  `[文字](url)` → `<a href="url">文字</a>`、行内代码 → `<code>`、围栏 → `<pre>`。

分块：Telegram 单条 text 硬上限 4096 字符（按实体解析后），按 **3900** 切块留余量，
优先行边界，围栏跨块时补闭合/续接，Python str 按码位切分（中文安全）。
"""
from __future__ import annotations

import re

TELEGRAM_TEXT_LIMIT = 3900
TELEGRAM_HARD_LIMIT = 4096
_FENCE = "```"

_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\((\S+?)\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((\S+?)\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+?)__")
_ITALIC_RE = re.compile(r"(?<![\w*])_([^_\n]+?)_(?![\w*])")
_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")

_TOKEN_RE = re.compile(r"\x00(\d+)\x00")


def _escape(text: str) -> str:
    """HTML 文本节点转义（幂等：已有实体不二次转义）。"""
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+);)", "&amp;", text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _convert_segment(segment: str) -> str:
    """非代码段：占位保护 → 转义 → 语法改写 → 还原（单趟）。"""
    tokens: list[str] = []

    def _stash(value: str) -> str:
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    # 1) 行内代码 / 图片 / 链接：内容需要转义，URL 不转义
    segment = _INLINE_CODE_RE.sub(
        lambda m: _stash("<code>" + _escape(m.group(1)) + "</code>"), segment)
    segment = _IMAGE_RE.sub(
        lambda m: _stash(f'<a href="{_escape(m.group(2))}">'
                         f'{_escape(m.group(1)) or _escape(m.group(2))}</a>'), segment)
    segment = _LINK_RE.sub(
        lambda m: _stash(f'<a href="{_escape(m.group(2))}">'
                         f'{_escape(m.group(1))}</a>'), segment)
    # 2) 普通文本转义
    segment = _escape(segment)
    # 3) 语法改写（结果占位，避免被后续规则二次命中）
    segment = _HEADING_RE.sub(lambda m: _stash("<b>" + m.group(1) + "</b>"), segment)
    segment = _BOLD_RE.sub(lambda m: _stash("<b>" + m.group(1) + "</b>"), segment)
    segment = _BOLD_UNDER_RE.sub(lambda m: _stash("<b>" + m.group(1) + "</b>"), segment)
    segment = _ITALIC_RE.sub(lambda m: _stash("<i>" + m.group(1) + "</i>"), segment)
    segment = _STRIKE_RE.sub(lambda m: _stash("<s>" + m.group(1) + "</s>"), segment)

    def _restore(match: re.Match) -> str:
        index = int(match.group(1))
        return tokens[index] if index < len(tokens) else ""

    return _TOKEN_RE.sub(_restore, segment)


def format_html(text: str) -> str:
    """标准 Markdown → Telegram HTML（代码围栏原样转义）。"""
    if not text:
        return ""
    parts = text.split(_FENCE)
    for i in range(0, len(parts), 2):
        parts[i] = _convert_segment(parts[i])
    for i in range(1, len(parts), 2):
        parts[i] = "<pre>" + _escape(parts[i]) + "</pre>"
    return _FENCE.join(parts)


def _char_chunks(src: str, limit: int) -> list[str]:
    """按行切 + 单行硬切，保证每块 ≤limit 个码位（不切多字节字符）。"""
    lines = src.splitlines(keepends=True)
    out: list[str] = []
    buf = ""
    for line in lines:
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + len(line) > limit:
            out.append(buf)
            buf = ""
        buf += line
    if buf:
        out.append(buf)
    return out


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """文本 → ≤limit 字符块序列（代码围栏逐块闭合）。

    空文本返回空列表；单块不超限原样返回。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    budget = limit - 8 if (_FENCE in text and limit > 16) else limit
    chunks = _char_chunks(text, budget)
    result: list[str] = []
    reopen = False
    for chunk in chunks:
        if reopen:
            chunk = _FENCE + "\n" + chunk
        if chunk.count(_FENCE) % 2 == 1:
            chunk = chunk + "\n" + _FENCE
            reopen = True
        else:
            reopen = False
        result.append(chunk)
    return result


def render_markdown(text: str) -> str:
    """出站文本统一入口：HTML 转换（分块由 split_text 负责）。"""
    return format_html(text)


__all__ = [
    "TELEGRAM_HARD_LIMIT",
    "TELEGRAM_TEXT_LIMIT",
    "format_html",
    "render_markdown",
    "split_text",
]