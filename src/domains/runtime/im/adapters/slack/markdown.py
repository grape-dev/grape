"""Slack 文本方言 —— 标准 Markdown → mrkdwn + 38000 字符分块（Issue #1309）。

Slack **用 mrkdwn，不是标准 Markdown**（设计 §3.5）：
  - 粗体是 `*粗*`（不是 `**粗**`）、斜体 `_斜_`、删除线 `~删~`；
  - 链接是 `<url|文字>`（不是 `[文字](url)`）；
  - 标题 `#` 与表格不被渲染（标题降级为整行粗体）；
  - `&` / `<` / `>` 必须转义为 `&amp;` / `&lt;` / `&gt;`，否则被当成链接或特殊语法。

分层契约不变：业务层继续写标准 Markdown，本模块只在适配器出站边界做转换。
代码块（``` 围栏）与行内代码按原样保留（Slack 在代码块内按字面渲染）。

分块：`chat.postMessage` 的 text 硬上限约 40k 字符，这里按 **38000 字符** 切
（含余量与 Slac 侧统计差异）；按行切优先，单行超限再硬切——Python str 按码位切分，
不会切在多字节字符中间（中文安全）。围栏跨块时补闭合/续接，避免渲染吞掉后续块。
"""
from __future__ import annotations

import re

SLACK_TEXT_LIMIT = 38000   # chat.postMessage text 软上限（上游 multica 同口径）
_FENCE = "```"

# 链接/图片：先摘出来（占位），避免转义与后续正则破坏 URL
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\((\S+?)\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((\S+?)\)")
# 已是 mrkdwn 的链接/图片（<url|文字>）：二次转换时原样保留，保证幂等
_MRKDWN_LINK_RE = re.compile(r"<([^|>\s]+)\|([^>\n]+)>")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+?)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")

_TOKEN_RE = re.compile(r"\x00(\d+)\x00")


def _escape(text: str) -> str:
    """mrkdwn 必需转义（幂等：已有的 &amp;/&lt;/&gt; 不二次转义）。"""
    text = re.sub(r"&(?!(?:amp|lt|gt);)", "&amp;", text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _convert_segment(segment: str) -> str:
    """非代码段：占位保护 → 转义 → 语法改写 → 还原（单趟，可重复运行不叠加）。

    幂等说明：单星号 `*文字*` 在 mrkdwn 中就是**粗体**，因此不再二次转换为
    `_斜体_`——它既可能是用户直接写的 mrkdwn 粗体，也可能是上一趟 `**粗**` 的输出。
    保留单星号即可保证 `format_mrkdwn` 重复运行结果稳定（标准 Markdown 的
    单星号斜体请改用 `_斜体_` 表达）。
    """
    tokens: list[str] = []

    def _stash(value: str) -> str:
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    # 1) 原样保留：行内代码 / 图片 / 链接（链接内部 label 需要转义，URL 不转义）
    segment = _INLINE_CODE_RE.sub(lambda m: _stash("`" + m.group(1) + "`"), segment)
    segment = _IMAGE_RE.sub(
        lambda m: _stash(f"<{m.group(2)}|{_escape(m.group(1)) or m.group(2)}>"), segment)
    segment = _LINK_RE.sub(
        lambda m: _stash(f"<{m.group(2)}|{_escape(m.group(1))}>"), segment)
    segment = _MRKDWN_LINK_RE.sub(lambda m: _stash(m.group(0)), segment)
    # 2) 普通文本转义
    segment = _escape(segment)
    # 3) 语法改写（结果同样占位，避免被后续斜体正则二次命中）
    segment = _HEADING_RE.sub(lambda m: _stash("*" + m.group(1) + "*"), segment)
    segment = _BOLD_RE.sub(lambda m: _stash("*" + m.group(1) + "*"), segment)
    segment = _BOLD_UNDER_RE.sub(lambda m: _stash("*" + m.group(1) + "*"), segment)
    segment = _STRIKE_RE.sub(lambda m: _stash("~" + m.group(1) + "~"), segment)

    def _restore(match: re.Match) -> str:
        index = int(match.group(1))
        return tokens[index] if index < len(tokens) else ""

    return _TOKEN_RE.sub(_restore, segment)


def format_mrkdwn(text: str) -> str:
    """标准 Markdown → Slack mrkdwn（代码块原样保留）。"""
    if not text:
        return ""
    parts = text.split(_FENCE)
    # 偶数段 = 正文（转换）；奇数段 = 代码块内容（原样）
    for i in range(0, len(parts), 2):
        parts[i] = _convert_segment(parts[i])
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


def _fence_count(chunk: str) -> int:
    return chunk.count(_FENCE)


def split_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    """文本 → ≤limit 字符的块序列（代码围栏逐块闭合，中文边界安全）。

    空文本返回空列表；单块不超限时原样返回（不做无意义改写）。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    # 围栏续接需要额外 "```\n" 前缀 / "\n```" 后缀（各 4 字符）：先预留预算，
    # 保证补围栏后每块仍 ≤limit（否则只能截断，会丢内容）。
    budget = limit - 8 if (_FENCE in text and limit > 16) else limit
    chunks = _char_chunks(text, budget)
    result: list[str] = []
    reopen = False
    for chunk in chunks:
        if reopen:
            chunk = _FENCE + "\n" + chunk
        if _fence_count(chunk) % 2 == 1:
            chunk = chunk + "\n" + _FENCE
            reopen = True
        else:
            reopen = False
        result.append(chunk)
    return result


def render_markdown(text: str) -> str:
    """出站文本统一入口：mrkdwn 转换（分块由 split_text 负责）。"""
    return format_mrkdwn(text)


__all__ = [
    "SLACK_TEXT_LIMIT",
    "format_mrkdwn",
    "render_markdown",
    "split_text",
]
