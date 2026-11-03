"""Bash 命令「目标路径」语法感知提取（Issue #757）。

背景：Agent 运行环境工具执行层此前的命令路径解析按裸文本扫描，heredoc
（`cat > file <<EOF ... EOF`）正文与 `python3 -c '<script>'` 字符串字面量中的
路径字符串（如 `skills/issue/references/issue-runbook.md`）被误判为命令目标
路径，导致 bash 工具报「从命令中解析目标路径失败」（Agent feedback #610）。

本模块提供单一权威实现，供命令路径解析消费方复用：

- `mask_heredoc_bodies(command)`：heredoc 定界内容块 → 等长空白（保留换行）
- `mask_python_c_literals(command)`：`python -c` 引号脚本字面量 → 等长空白
- `extract_write_targets(command)`：剥离后仅提取命令自身重定向目标
  （`> file` / `>> file` / `&>` / `&>>` / `N>` / `N>>`）

设计原则（对齐 spec 风险表）：仅剥离可静态判定的安全形态（闭合 heredoc、
引号包裹字面量），未闭合/无引号形态保守保留原文——宁可多保留，不可误删
真实重定向。
"""
from __future__ import annotations

import re

# ── heredoc 定界符：<<-? 可选引号包裹 ───────────────────────────────────────
# 形态：<<EOF / <<'EOF' / <<"EOF" / <<-EOF / <<-'EOF' / << "EOF"（含空白）
_HEREDOC_OPENER_RE = re.compile(
    r"<<-?\s*(?P<quote>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_-]*)\s*(?P=quote)"
)


def _mask_range(text: str, start: int, end: int, *, keep_newlines: bool = True) -> str:
    """把 [start, end) 区间替换为等长空白（默认保留换行，保持行结构/偏移）。"""
    if end <= start:
        return text
    piece = text[start:end]
    if keep_newlines:
        masked = "".join(ch if ch == "\n" else " " for ch in piece)
    else:
        masked = " " * len(piece)
    return text[:start] + masked + text[end:]


def mask_heredoc_bodies(command: str) -> str:
    """把闭合 heredoc 的定界内容块替换为等长空白（保留换行）。

    规则：
    - 定界符行（`<<DELIM` / `<<'DELIM'` / `<<"DELIM"` / `<<-DELIM`）之后，
      至「定界符独立行」结束（`<<-` 允许该行前导 tab）。
    - 未闭合（找不到结束行）保守不剥离，原样返回。
    - 多个 heredoc 逐个处理。
    """
    if "<<" not in command:
        return command
    result = command
    for m in _HEREDOC_OPENER_RE.finditer(result):
        # 只处理首次命中后重扫（剥离改变文本，但空白不影响后续匹配语义）
        delim = m.group("delim")
        body_start = m.end()
        # 找到定界符所在行的行尾，正文自下一行开始
        nl = result.find("\n", body_start)
        if nl == -1:
            # 定界符行无换行 → 无正文（如命令尚未写完），不剥离
            continue
        body_start = nl + 1
        # 逐行找定界符独立行（允许前导 tab；正则从定界符行继续）
        close_re = re.compile(r"^[ \t]*" + re.escape(delim) + r"[ \t]*$")
        pos = body_start
        closed = False
        while pos <= len(result):
            line_end = result.find("\n", pos)
            if line_end == -1:
                line = result[pos:]
                next_pos = len(result) + 1
            else:
                line = result[pos:line_end]
                next_pos = line_end + 1
            if close_re.match(line):
                # 剥离 [body_start, pos)（不含结束行）
                result = _mask_range(result, body_start, pos)
                closed = True
                break
            if line_end == -1:
                break
            pos = next_pos
        if not closed:
            # 未闭合 heredoc：保守不剥离（原文可能是未写完的命令）
            continue
    return result


# ── python -c 脚本字面量 ────────────────────────────────────────────────────
# 形态：python / python3 / python3.11（可穿插其他短选项）后 -c '<script>'
_PYTHON_C_RE = re.compile(
    r"\bpython(?:3(?:\.\d+)?)?((?:\s+-{1,2}[\w=-]+)*)\s+-c\s+(?=['\"])"
)


def mask_python_c_literals(command: str) -> str:
    """把 `python[3[.x]] -c '<script>'` / `-c "<script>"` 的字面量内容替换为等长空白。

    仅处理引号包裹的字面量（可静态判定安全）；无引号/拼接形态保守保留。
    字面量内的路径字符串（如 `pathlib.Path('a/b.md')`）随之失效。
    """
    result = command
    for m in _PYTHON_C_RE.finditer(result):
        i = m.end()
        if i >= len(result):
            continue
        quote = result[i]
        if quote not in ("'", '"'):
            continue
        # 找配对结束引号（单引号内无转义；双引号处理 \" 转义）
        j = i + 1
        while j < len(result):
            ch = result[j]
            if quote == "'" and ch == "'":
                break
            if quote == '"':
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    break
            j += 1
        if j >= len(result):
            # 未闭合：保守不剥离
            continue
        # 内容 [i+1, j) 替换为空白，保留引号对
        result = _mask_range(result, i + 1, j, keep_newlines=False)
    return result


# ── 重定向目标提取 ──────────────────────────────────────────────────────────

_PATH_STOP_CHARS = " \t\n;&|<>(){}"


def _read_token(text: str, start: int) -> tuple[str, int]:
    """自 start 读一个 shell token（去引号），返回 (token, next_index)。"""
    token_chars: list[str] = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < len(text) and text[i] != quote:
                if quote == '"' and text[i] == "\\" and i + 1 < len(text):
                    token_chars.append(text[i + 1])
                    i += 2
                    continue
                token_chars.append(text[i])
                i += 1
            i += 1  # 跳过结束引号
            continue
        if ch == "\\" and i + 1 < len(text):
            token_chars.append(text[i + 1])
            i += 2
            continue
        if ch in _PATH_STOP_CHARS:
            break
        token_chars.append(ch)
        i += 1
    return "".join(token_chars), i


def extract_write_targets(command: str) -> list[str]:
    """提取命令自身的文件重定向目标路径（写入路径）。

    语义（Issue #757 Spec）：
    - 先剥离 heredoc 定界内容块与 `python -c` 引号脚本字面量，块/字面量内的
      路径字符串不参与提取；
    - 仅提取 `>` / `>>` / `&>` / `&>>` / `N>` / `N>>`（N 为 fd 号）后的目标
      token（去引号）；
    - fd 复制形态（`2>&1` / `>&2` / `1>&2`）不产生目标；
    - 无重定向返回空列表；结果保序去重。
    """
    if not command:
        return []
    text = mask_heredoc_bodies(command)
    text = mask_python_c_literals(text)

    targets: list[str] = []
    seen: set[str] = set()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            # 引号内的重定向操作符是字面量（如 echo "a > b"），跳过整个引号段
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if quote == '"' and text[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch != ">":
            i += 1
            continue
        # 命中 '>'：判定操作符形态（向后看 >> / & / fd 复制）
        j = i + 1
        if j < n and text[j] == ">":
            j += 1
        # fd 复用：">&N" / ">>&N" —— 不产生目标
        if j < n and text[j] == "&":
            k = j + 1
            while k < n and text[k].isdigit():
                k += 1
            i = max(j + 1, k)
            continue
        # 跳过目标 token 前的空白
        k = j
        while k < n and text[k] in " \t":
            k += 1
        token, nxt = _read_token(text, k)
        if token and token not in seen:
            seen.add(token)
            targets.append(token)
        i = nxt if nxt > k else k + 1
    return targets
