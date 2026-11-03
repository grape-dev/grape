"""PR body Issue 号上下文过滤（Issue #728）。

e2e 自动关联（pulls create/update）从 body 提取 Issue 号时，先剥离非语义语境，
避免代码块（```）、行内反引号、引用块（>）内的 `#N` 被误捕获建立 PR↔Issue 关联。

纯函数、无外部依赖，可单测：
- strip_nonsemantic_context(text)：剥离 fenced code / inline code / blockquote 行
- extract_related_issue_nums(text, strict=False)：
  - 默认模式：剥离后在剩余正文提取全部 `#N`（自然语言引用语义保留）
  - strict 模式：仅识别 `Closes #` / `Related #` / `关联 #` 显式前缀
"""
import re

# fenced code block：``` 开闭围栏（含 ~~~ 围栏），跨行
_FENCED_RE = re.compile(r"(?:```|~~~).*?(?:```|~~~)", re.DOTALL)
# inline code：单/双反引号包裹（不跨行）
_INLINE_RE = re.compile(r"`+[^`\n]*`+")
# 引用块行：行首（允许缩进）以 > 开头
_QUOTE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
# 正文 Issue 引用
_ISSUE_REF_RE = re.compile(r"#(\d+)")
# 显式关联关键字（strict 模式）：Closes # / Related # / 关联 #
_EXPLICIT_REF_RE = re.compile(r"(?:closes|related|关联)\s*#(\d+)", re.IGNORECASE)


def strip_nonsemantic_context(text: str) -> str:
    """剥离 body 中的非语义语境：fenced code、inline code、blockquote 行。"""
    if not text:
        return ""
    cleaned = _FENCED_RE.sub(" ", text)
    cleaned = _QUOTE_RE.sub(" ", cleaned)
    cleaned = _INLINE_RE.sub(" ", cleaned)
    return cleaned


def extract_related_issue_nums(text: str, strict: bool = False) -> list:
    """从 body 提取可建立 PR↔Issue 关联的 Issue 编号（int 列表，去重保序）。

    strict=False（默认）：剥离非语义语境后提取全部 `#N`（兼容自然语言写法）。
    strict=True：仅提取 `Closes #` / `Related #` / `关联 #` 显式关键字前缀的 `#N`
    （剥离同样先做——代码块里的 `Closes #N` 也不算显式语义）。
    编号 0 与重复项被剔除。
    """
    cleaned = strip_nonsemantic_context(text or "")
    if not cleaned:
        return []
    pattern = _EXPLICIT_REF_RE if strict else _ISSUE_REF_RE
    nums = []
    for m in pattern.finditer(cleaned):
        n = int(m.group(1))
        if n != 0 and n not in nums:
            nums.append(n)
    return nums
