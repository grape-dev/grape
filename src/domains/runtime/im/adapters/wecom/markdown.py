"""企微 markdown 分块（Issue #1254）。

官方文档口径：`markdown.content` 最长 **20480 字节**（utf-8），流式 `stream.content`
同一上限。超长内容必须分块发送，且每块代码围栏闭合——块内 "```" 出现次数为奇数时
在块尾补闭合围栏、下一块以开栏续接，否则客户端会把后续块吞进代码块
（钉钉 #936 同款教训，企微渲染器同样按围栏状态机处理）。

企微 markdown 子集：标题 1~3 级（#~###，更深降级为粗体行）、粗体/斜体、有序/无序
列表、引用、链接、图片、分割线、行内代码、代码块、表格。`render_markdown` 只做
「无损归一」，不改写正文（表格与链接相邻在子集内，无需降级）。
"""
from __future__ import annotations

WECOM_MARKDOWN_LIMIT = 20480  # bytes（官方 markdown.content 上限）
_FENCE = "```"


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _fence_count(chunk: str) -> int:
    return chunk.count(_FENCE)


def _close_fence(chunk: str) -> str:
    if _fence_count(chunk) % 2 == 1:
        return chunk + "\n" + _FENCE
    return chunk


def _reopen_fence(chunk: str, reopen: bool) -> str:
    if reopen:
        return _FENCE + "\n" + chunk
    return chunk


def split_markdown(text: str, limit: int = WECOM_MARKDOWN_LIMIT) -> list[str]:
    """markdown → 每块 ≤limit 字节（围栏逐块闭合；空文本返回空列表）。

    按行切优先（不截断多字节字符）；单行超限再按 utf-8 边界硬切。
    围栏续接可能让块超限：二次切分后再平衡，有限轮次收敛。
    """
    if not text:
        return []

    def _chunk_by_bytes(src: str, byte_limit: int) -> list[str]:
        lines = src.splitlines(keepends=True)
        out: list[str] = []
        buf = ""
        for line in lines:
            while _byte_len(line) > byte_limit:
                if buf:
                    out.append(buf)
                    buf = ""
                encoded = line.encode("utf-8")
                piece = encoded[:byte_limit].decode("utf-8", errors="ignore")
                rest = encoded[len(piece.encode("utf-8")):].decode(
                    "utf-8", errors="ignore")
                out.append(piece)
                line = rest
            if _byte_len(buf) + _byte_len(line) > byte_limit and buf:
                out.append(buf)
                buf = ""
            buf += line
        if buf:
            out.append(buf)
        return out

    def _balance(chunks: list[str]) -> list[str]:
        result: list[str] = []
        reopen = False
        for chunk in chunks:
            chunk = _reopen_fence(chunk, reopen)
            closed = _close_fence(chunk)
            reopen = _fence_count(chunk) % 2 == 1 and closed != chunk
            result.append(closed)
        return result

    chunks = _balance(_chunk_by_bytes(text, limit))
    for _ in range(8):
        oversized = [c for c in chunks if _byte_len(c) > limit]
        if not oversized:
            break
        next_chunks: list[str] = []
        for c in chunks:
            if _byte_len(c) > limit:
                next_chunks.extend(_chunk_by_bytes(c, limit))
            else:
                next_chunks.append(c)
        chunks = _balance(next_chunks)
    return chunks


def render_markdown(text: str) -> str:
    """企微 markdown 归一：4 级以上标题降为粗体行（子集只到 ###），其余原样。

    只做「不给客户端喂未知语法」的最小归一，不重写正文语义。
    """
    if not text:
        return ""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        hashes = len(stripped) - len(stripped.lstrip("#"))
        if 4 <= hashes <= 6 and stripped[hashes:hashes + 1] == " ":
            out.append(f"**{stripped[hashes:].strip()}**")
        else:
            out.append(line)
    return "\n".join(out)


__all__ = ["WECOM_MARKDOWN_LIMIT", "render_markdown", "split_markdown"]