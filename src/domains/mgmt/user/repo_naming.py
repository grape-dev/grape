r"""仓库名称校验口径（Issue #4231）：check-name 与 create 共用同一实现。

Python 的 `\w` 默认匹配 Unicode（含中文），直接沿用会让「中文名」通过本地校验、
一路打到上游托管平台才失败。这里显式限定 ASCII 字符集，长度上限取各托管平台的
最小公约数（100），并让返回原因不含任何提示前缀，避免与前端文案叠加。
"""
import re

REPO_NAME_MAX_LENGTH = 100
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_repo_name(name: str, label: str = "仓库名称") -> tuple[bool, str]:
    """校验仓库名称/路径，返回 (是否合法, 裸原因)。"""
    _name = (name or "").strip()
    if not _name:
        return False, f"{label}不能为空"
    if len(_name) > REPO_NAME_MAX_LENGTH:
        return False, f"{label}不能超过 {REPO_NAME_MAX_LENGTH} 个字符"
    if not REPO_NAME_PATTERN.match(_name):
        return False, f"{label}只能包含字母、数字、下划线、连字符和点"
    if _name in (".", ".."):
        return False, f"{label}不能为 . 或 .."
    return True, ""
