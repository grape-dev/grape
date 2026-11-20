"""提示注入检测器——任务级，输入是用户消息。

P1 不用 LLM 检测器——延迟预算 P99 < 50ms（正则/规则匹配）。
诚实定位：基于正则必然被绕过——它是纵深防御的一层，不是安全边界。
真正的安全边界是 bwrap + seccomp（内核态）。
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Protocol

from ..types import InputDecision


@dataclass(frozen=True)
class InjectionResult:
    decision: InputDecision
    score: float          # 0.0-1.0
    detail: str = ""


class PromptInjectionDetector(Protocol):
    """提示注入检测器——任务级。"""

    def check(self, user_input: str) -> InjectionResult:
        """检测用户输入是否含提示注入。

        ALLOW → 放行
        DENY → 调用方 raise TaskBlocked
        """
        ...


# 提示注入规则集（P1 硬编码，P2 从 DB 加载 + 60s 缓存）
_INJECTION_PATTERNS = [
    r"ignore\s+(previous|all|above)\s+instructions",
    r"you\s+are\s+(now|actually)\s+a",
    r"system\s*:\s*you\s+can",
    r"reveal\s+(your|the)\s+(system|initial)\s+prompt",
    r"jailbreak|DAN|AIM\s+mode",
    r"<\/?system>|<\/?assistant>",
]


class RuleBasedDetector:
    """基于正则的提示注入检测器。

    诚实定位：基于正则必然被绕过——它是纵深防御的一层，不是安全边界。
    真正的安全边界是 bwrap + seccomp（内核态）。
    """

    def __init__(self, patterns: list[str] | None = None):
        self._patterns = [re.compile(p, re.IGNORECASE) for p in (patterns or _INJECTION_PATTERNS)]

    def check(self, user_input: str) -> InjectionResult:
        matches = []
        for pattern in self._patterns:
            if pattern.search(user_input):
                matches.append(pattern.pattern)
        if not matches:
            return InjectionResult(decision=InputDecision.ALLOW, score=0.0)
        score = min(len(matches) / len(self._patterns), 1.0)
        if score >= 0.3:
            return InjectionResult(decision=InputDecision.DENY, score=score,
                                   detail=f"matched: {matches}")
        return InjectionResult(decision=InputDecision.ALLOW, score=score,
                               detail=f"low score: {matches}")


class NullPromptInjectionDetector:
    """空检测器——不检测，总是 ALLOW。"""

    def check(self, user_input: str) -> InjectionResult:
        return InjectionResult(decision=InputDecision.ALLOW, score=0.0)
