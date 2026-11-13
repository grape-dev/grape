"""钉钉 markdown 分块（Issue #936 P2，设计文档 §4.4）。

钉钉 markdown 消息正文上限约 15000 字节（参考 multica dingtalk_markdown.go L10-33）：
超长内容按字节分块发送，且每块必须**代码围栏闭合**——块内 "```" 出现次数为奇数时
在块尾补闭合围栏、下一块以开栏续接，否则钉钉渲染会把后续块吞进代码块。
"""
from __future__ import annotations


DINGTALK_MARKDOWN_LIMIT = 15000  # bytes
_FENCE = "```"


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _fence_count(chunk: str) -> int:
    return chunk.count(_FENCE)


def _close_fence(chunk: str) -> str:
    """围栏不闭合（奇数次 ```）→ 块尾补闭合，返回值标记续接需要开栏。"""
    if _fence_count(chunk) % 2 == 1:
        return chunk + "\n" + _FENCE
    return chunk


def _reopen_fence(chunk: str, reopen: bool) -> str:
    if reopen:
        return _FENCE + "\n" + chunk
    return chunk


def split_markdown(text: str, limit: int = DINGTALK_MARKDOWN_LIMIT) -> list[str]:
    """markdown → ≤limit 字节的块序列（围栏逐块闭合）。

    按行切优先（不截断多字节字符）；单行超限再按字符硬切（UTF-8 边界安全）。
    围栏续接可能让块超限（补 "```" 开/闭），超限块二次切分后再平衡，
    直至每块 ≤limit 且围栏闭合。空文本返回空列表。
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
                rest = encoded[len(piece.encode("utf-8")):] \
                    .decode("utf-8", errors="ignore")
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
    # 围栏续接开销可能让块超限：超限块二次切分再平衡（有限轮次收敛）
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
    """钉钉 markdown 兼容标准语法，正文不改写（分块见 split_markdown）。"""
    return text


__all__ = ["DINGTALK_MARKDOWN_LIMIT", "render_markdown", "split_markdown"]
