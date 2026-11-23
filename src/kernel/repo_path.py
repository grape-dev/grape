"""仓库全路径的段级校验（对齐 PR #1854 的 validate_project，覆盖多级命名空间）。

为什么要单独一层：GitLab 的仓库路径是 `<组>/<子组>/.../<仓>`，来自 **webhook payload**
（外部可控），随后会进 repo_key、托管查表、工作目录与上游 URL。不校验就会把
`..`、空段、`a//b`、带控制字符的路径带进文件系统与 URL —— 这类输入只需要一次就够。

与 PR #1854 的差别：那版用 ASCII 白名单 `[\w.-]+`，会把 GitLab 合法但非 ASCII 的
组名（中文/日文等）判死。这里反过来用**禁止集**（各平台本身就禁止的字符）+
段语义校验，既能挡路径穿越，也不误杀 Unicode 仓库名。
"""
from __future__ import annotations

import re

#: 单段（owner 或 repo）长度上限。来源=平台厂商文档：GitHub 仓库名上限 100 字符；
#: GitLab/Gitee/GitCode 的同类限制不低于该值但不公开承诺更宽，故取四平台**最严**
#: 的 100 作为通用上限（宁可拒掉超长名，也不向上游送出必然失败的路径）。
#: 见 docs/issue-3580/measurements.md「常量来源 · 仓库路径长度上限」。
MAX_SEGMENT_CHARS = 100
#: 完整路径（owner/repo，含子组斜杠）长度上限。来源=平台厂商文档：GitLab 对项目
#: 完整路径的校验上限是 255 字符；其余平台的 owner/repo 合计远低于该量级，
#: 故 255 是四平台交集内的上界。超出即视为非法输入（RepoPathError）。
MAX_PATH_CHARS = 255
# 各平台（gitlab/github/gitee/gitcode）自身就禁止出现在 owner/repo 段里的字符：
# 由它们决定 URL 与文件系统语义，出现即视为非法输入。
_FORBIDDEN_CHARS = frozenset('\\:*?"<>|\x00')
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RepoPathError(ValueError):
    """仓库路径非法（不是"不存在"，是形如注入的输入）。"""


def validate_repo_path(path: str, *, min_segments: int = 2) -> str:
    """校验并返回规范化路径（去掉首尾斜杠/空白）；非法抛 RepoPathError。"""
    raw = str(path or "").strip()
    if not raw:
        raise RepoPathError("仓库路径为空")
    if len(raw) > MAX_PATH_CHARS:
        raise RepoPathError(f"仓库路径过长（>{MAX_PATH_CHARS}）")
    normalized = raw.strip("/")
    if not normalized:
        raise RepoPathError("仓库路径只有分隔符")
    if "//" in normalized:
        raise RepoPathError("仓库路径含空段")
    if _CONTROL_RE.search(normalized):
        raise RepoPathError("仓库路径含控制字符")
    segments = normalized.split("/")
    if len(segments) < min_segments:
        raise RepoPathError(f"仓库路径至少需要 {min_segments} 段")
    for seg in segments:
        if not seg or seg in (".", ".."):
            raise RepoPathError(f"仓库路径含非法段: {seg!r}")
        if seg != seg.strip():
            raise RepoPathError(f"仓库路径段含首尾空白: {seg!r}")
        if any(ch.isspace() for ch in seg):
            # 各平台的 owner/repo 段都不允许空白（URL/文件系统语义歧义），
            # 内部空白也必须拒：'re po' 在不同工具里会被解释成两个参数。
            raise RepoPathError(f"仓库路径段含空白: {seg!r}")
        if len(seg) > MAX_SEGMENT_CHARS:
            raise RepoPathError(f"仓库路径段过长（>{MAX_SEGMENT_CHARS}）")
        if _FORBIDDEN_CHARS & set(seg):
            raise RepoPathError(f"仓库路径段含非法字符: {seg!r}")
    return normalized


def is_valid_repo_path(path: str, *, min_segments: int = 2) -> bool:
    try:
        validate_repo_path(path, min_segments=min_segments)
    except RepoPathError:
        return False
    return True