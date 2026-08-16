"""飞书 lark_md 适配转换：把 agent 的标准 Markdown 答复转换为飞书卡片 lark_md 子集。

lark_md 不支持的语法（会按纯文本显示）：表格、代码块、标题、分割线。
转换策略（2026-08-16）：
- 表格 → 每行一个列表项（分隔符 ｜），跳过 `|---|` 分隔行；
- 代码块 → 降级为纯文本（去掉围栏，保留内容）；
- `# 标题` → 转加粗；
- `---` 分割线 → 删除；
- 按行边界截断到 max_len，避免切断表格/行。
"""
import re


def _split_table_row(line: str) -> list[str]:
    """拆 markdown 表格行 cell（去首尾 |，还原转义 \\|）"""
    body = line.strip().strip("|")
    cells = []
    for c in body.split("|"):
        cells.append(c.strip().replace("\\|", "|"))
    return cells


def _is_table_sep(line: str) -> bool:
    """`| --- | :-- | ---: |` 分隔行"""
    cells = [c for c in _split_table_row(line)]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


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
            # 收集连续表格块
            block, j = [], i
            while j < n and lines[j].strip().startswith("|"):
                block.append(lines[j].strip())
                j += 1
            if len(block) >= 2 and any(_is_table_sep(b) for b in block):
                for b in block:
                    if _is_table_sep(b):
                        continue
                    out.append("- " + " ｜ ".join(_split_table_row(b)))
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
        if pos > 0:
            result = cut[:pos]
        else:
            result = cut
    return result
