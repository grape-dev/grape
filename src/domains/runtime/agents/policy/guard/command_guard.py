"""命令预检——命令级，输入是 argv。

诚实定位：Bash 怪异特性（$IFS、${var}、进程替换、heredoc、base64 | sh）
意味着字符串匹配不可靠。它是纵深防御的一层，不是安全边界。
真正的安全边界是 bwrap + seccomp（内核态）。
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Protocol

from ..types import Decision


@dataclass(frozen=True)
class GuardResult:
    decision: Decision
    cmd: list[str] | None    # DENY 时为 None——忘检查会 TypeError
    reason: str = ""


class CommandGuard(Protocol):
    """命令预检——命令级。"""

    def check(self, cmd: list[str]) -> GuardResult:
        """ALLOW → cmd 原样。MODIFY → cmd 修改后。DENY → cmd=None。"""
        ...


# 危险命令规则集（P1 硬编码，P2 从 DB 加载 + 60s 缓存）
_DANGEROUS_PATTERNS = [
    (r"rm\s+-rf\s+/", "rm -rf root"),
    (r"rm\s+-rf\s+~", "rm -rf home"),
    (r"curl\s+.*\|\s*sh", "curl pipe shell"),
    (r"wget\s+.*\|\s*sh", "wget pipe shell"),
    (r"dd\s+if=.*of=/dev/", "dd to device"),
    (r"mkfs\.", "format filesystem"),
    (r":\(\)\{.*\|.*&\};", "fork bomb"),
    (r"chmod\s+-R\s+777\s+/", "chmod root 777"),
]


class RegexCommandGuard:
    """基于正则的危险命令检测器。

    诚实定位：字符串匹配不可靠，是纵深防御的一层。
    """

    def __init__(self, patterns: list[tuple[str, str]] | None = None):
        self._patterns = [(re.compile(p, re.IGNORECASE), reason)
                          for p, reason in (patterns or _DANGEROUS_PATTERNS)]

    def check(self, cmd: list[str]) -> GuardResult:
        cmd_str = " ".join(cmd)
        for pattern, reason in self._patterns:
            if pattern.search(cmd_str):
                return GuardResult(decision=Decision.DENY, cmd=None, reason=reason)
        return GuardResult(decision=Decision.ALLOW, cmd=cmd)


class NullCommandGuard:
    """空命令守卫——不检测，总是 ALLOW。"""

    def check(self, cmd: list[str]) -> GuardResult:
        return GuardResult(decision=Decision.ALLOW, cmd=cmd)
