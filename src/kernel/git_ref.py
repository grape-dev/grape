"""Git ref（分支名）规则校验（Issue #3922）。

背景：`POST /api/scenes/branches/{owner}/{repo}` 原先只判空，非法分支名直接打到上游；
上游以 400 拒绝后又被折叠成 502「托管服务不可用」，用户既不知道违反哪条规则也无法自助修正。

本模块是**服务端单一来源**的 ref 规则表（纯函数、无 FastAPI 依赖，前端/其它端点可复用）：

- 规则对齐 `git check-ref-format`：控制字符、空格、`~`、`^`、`:`、`?`、`*`、`[`、`\\` 非法；
  不得以 `/` 或 `.` 开头/结尾、不得含 `..` / `@{` / 连续斜杠 `//`、不得以 `.lock` 结尾、
  不得为单个 `@`、每一级分片不得以 `.` 开头；
- 叠加代码托管平台（GitCode/GitHub/Gitee）实际拒绝的形态：单/双引号，
  以及调用方常误填的完整 ref 形态 `refs/heads/`、`refs/remotes/`（接口只要分支名）；
- 以 `-` 开头的名字一律拒绝：`git checkout` / `git branch` 会把它当选项解析。

返回值是**面向用户的可操作原因**（中文、单句），调用方命中即 400 短路、不再触达上游。
"""

from __future__ import annotations

__all__ = [
    "MAX_BRANCH_NAME_LENGTH",
    "is_valid_branch_name",
    "validate_branch_name",
]

# git 未限制长度，平台（GitCode/GitHub）ref 名上限 255
MAX_BRANCH_NAME_LENGTH = 255

# 完整 ref 形态的前缀：接口只要分支名，带前缀会被上游当成分支名的一部分
_FULL_REF_PREFIXES = ("refs/heads/", "refs/remotes/")

# 平台拒绝、且 git check-ref-format 未覆盖的字符（git 只禁 ~ ^ : ? * [ \ 与空格）
_EXTRA_FORBIDDEN_CHARS = ("'", '"')

_HINT_CHARSET = "仅支持字母、数字、'-'、'_'、'/'、'.'"


def validate_branch_name(name) -> str:
    """校验分支名（git ref 规则 + 平台约束）。

    Args:
        name: 待校验分支名（None/空串视为非法）。

    Returns:
        合法返回空串 ``""``；非法返回**面向用户的可操作原因**（可直接作为 400 message）。
    """
    text = str(name if name is not None else "").strip()
    if not text:
        return "分支名不能为空"
    if len(text) > MAX_BRANCH_NAME_LENGTH:
        return (f"分支名长度不能超过 {MAX_BRANCH_NAME_LENGTH} 个字符"
                f"（当前 {len(text)}）")

    if text == "@":
        return "分支名不能是单个 '@'"
    for prefix in _FULL_REF_PREFIXES:
        if text.startswith(prefix):
            return (f"分支名不应包含 '{prefix}' 前缀，请直接填写分支名"
                    f"（如 feat/my-branch）")

    if text.startswith("-"):
        return "分支名不能以 '-' 开头"
    if text.startswith("/"):
        return "分支名不能以 '/' 开头"
    if text.startswith("."):
        return "分支名不能以 '.' 开头"
    if text.endswith("/"):
        return "分支名不能以 '/' 结尾"
    if text.endswith("."):
        return "分支名不能以 '.' 结尾"
    if text.endswith(".lock"):
        return "分支名不能以 '.lock' 结尾"

    if ".." in text:
        return "分支名不能包含连续的 '..'"
    if "@{" in text:
        return "分支名不能包含 '@{'"
    if "//" in text:
        return "分支名不能包含连续的 '//'"

    for segment in text.split("/"):
        if segment.startswith("."):
            return "分支名的每一级不能以 '.' 开头"

    for char in text:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            return "分支名不能包含控制字符"
    if " " in text:
        return f"分支名不能包含空格，{_HINT_CHARSET}"
    for char in ("~", "^", ":", "?", "*", "[", "\\") + _EXTRA_FORBIDDEN_CHARS:
        if char in text:
            return f"分支名不能包含字符 '{char}'，{_HINT_CHARSET}"
    return ""


def is_valid_branch_name(name) -> bool:
    """分支名是否合法（``validate_branch_name(name) == ""`` 的布尔快捷方式）。"""
    return not validate_branch_name(name)