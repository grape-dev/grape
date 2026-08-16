"""飞书 lark_md / 卡片适配转换（2026-08-16）。

lark_md 不支持的语法（按纯文本显示）：表格、代码块、标题、分割线。

- `to_lark_md(text)`: 标准 Markdown → lark_md 文本子集（表格→加粗表头+列表，代码块/标题/分割线降级，按行截断）。
- `to_lark_elements(text)`: 文本 → 卡片元素列表；表格(≤5 列) → `column_set` 真正多列对齐，>5 列回退文本；非表格 → `div`。
"""
import re

_MAX_DATA_ROWS = 15


def _split_table_row(line: str) -> list[str]:
    """拆 markdown 表格行 cell（去首尾 |，还原转义 \\|）"""
    body = line.strip().strip("|")
    return [c.strip().replace("\\|", "|") for c in body.split("|")]


def _row_is_sep(cells: list[str]) -> bool:
    """`|---|:---|` 分隔行（cells 全为 --- 样式）"""
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


def _table_rows(block: list[str]) -> list[list[str]]:
    rows = [_split_table_row(b) for b in block]
    return [r for r in rows if not _row_is_sep(r)]


def _table_fallback_lines(block: list[str]) -> list[str]:
    """方案 A：表头加粗单行（无 bullet）+ 数据行 bullet"""
    rows = _table_rows(block)
    if not rows:
        return []
    out = ["**" + " ｜ ".join(rows[0]) + "**"]
    for r in rows[1:]:
        out.append("- " + " ｜ ".join(r))
    return out


def to_lark_md(text: str, max_len: int = 2000) -> str:
    """标准 Markdown → 飞书 lark_md 子集，按行边界截断到 max_len。"""
    lines = (text or "").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    in_code = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(line)
            i += 1
            continue
        if stripped.startswith("|"):
            block, j = [], i
            while j < n and lines[j].strip().startswith("|"):
                block.append(lines[j].strip())
                j += 1
            if len(block) >= 2 and any(_row_is_sep(_split_table_row(b)) for b in block):
                out.extend(_table_fallback_lines(block))
                i = j
                continue
            out.append(line)
            i += 1
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if m:
            out.append("**" + m.group(1).strip() + "**")
            i += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            i += 1
            continue
        out.append(line)
        i += 1

    result = "\n".join(out)
    if max_len and len(result) > max_len:
        cut = result[:max_len]
        pos = cut.rfind("\n")
        result = cut[:pos] if pos > 0 else cut
    return result


def _build_column_set(block: list[str]) -> dict | None:
    """表格 → 飞书 column_set 元素（真正多列对齐）。>5 列或空表返回 None（回退文本）。"""
    rows = _table_rows(block)
    if not rows:
        return None
    header = rows[0]
    data = rows[1:]
    total = len(data)
    ncols = len(header)
    if ncols == 0 or ncols > 5:
        return None
    truncated = total > _MAX_DATA_ROWS
    data = data[:_MAX_DATA_ROWS]
    columns = []
    for ci in range(ncols):
        hcell = header[ci] if ci < len(header) else ""
        lines = ["**" + (hcell or "") + "**"]
        for r in data:
            cell = r[ci] if ci < len(r) else ""
            lines.append((cell.replace("\n", " · ").strip()) or "—")
        if truncated and ci == 0:
            lines.append(f"… 共 {total} 行")
        weight = max(1, min(5, max((len(l) for l in lines), default=1)))
        columns.append({
            "tag": "column",
            "width": "weighted",
            "weight": weight,
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        })
    return {"tag": "column_set", "columns": columns}


def _flush_text(buf: list[str], elements: list, max_len: int):
    if not buf:
        return
    text = to_lark_md("\n".join(buf), max_len)
    if text.strip():
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": text}})
    buf.clear()


def to_lark_elements(text: str, max_len: int = 2000) -> list[dict]:
    """文本 → 飞书卡片元素列表。表格(≤5列) → column_set；否则 → div 文本。"""
    lines = (text or "").split("\n")
    elements: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    i, n = 0, len(lines)
    in_code = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            if buf_len + len(line) > max_len:
                _flush_text(buf, elements, max_len)
                buf_len = 0
            buf.append(line)
            buf_len += len(line)
            i += 1
            continue
        if stripped.startswith("|"):
            block, j = [], i
            while j < n and lines[j].strip().startswith("|"):
                block.append(lines[j].strip())
                j += 1
            if len(block) >= 2 and any(_row_is_sep(_split_table_row(b)) for b in block):
                cs = _build_column_set(block)
                if cs is not None:
                    _flush_text(buf, elements, max_len)
                    buf_len = 0
                    elements.append(cs)
                else:
                    fallback = _table_fallback_lines(block)
                    if buf_len + sum(len(x) for x in fallback) > max_len:
                        _flush_text(buf, elements, max_len)
                        buf_len = 0
                    buf.extend(fallback)
                    buf_len += sum(len(x) for x in fallback)
                i = j
                continue
            buf.append(line)
            buf_len += len(line)
            i += 1
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if m:
            adapted = "**" + m.group(1).strip() + "**"
            if buf_len + len(adapted) > max_len:
                _flush_text(buf, elements, max_len)
                buf_len = 0
            buf.append(adapted)
            buf_len += len(adapted)
            i += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", stripped):
            i += 1
            continue
        if buf_len + len(line) > max_len:
            _flush_text(buf, elements, max_len)
            buf_len = 0
        buf.append(line)
        buf_len += len(line)
        i += 1
    _flush_text(buf, elements, max_len)
    return elements
