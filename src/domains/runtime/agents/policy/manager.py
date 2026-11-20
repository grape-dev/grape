"""PolicyManager——策略管理器，屏蔽 detector / guard 内部结构 + 直接写审计。

不持有 sandbox 依赖。audit 从 common.audit 导入。
线程安全：check_input / check_command 无共享可变状态（不加锁）。
"""
from __future__ import annotations
import logging

from src.domains.runtime.agents.common.audit import AuditListener, NullAuditLogger, AuditEvent, AuditKind, now_iso
from .guard.prompt_injection import PromptInjectionDetector, NullPromptInjectionDetector, InjectionResult
from .guard.command_guard import CommandGuard, NullCommandGuard, GuardResult
from .types import Decision, InputDecision

log = logging.getLogger("devops-bot")


class PolicyManager:
    """策略管理器——屏蔽 detector / guard 内部结构 + 直接写审计。

    不持有 sandbox 依赖。audit 从 common.audit 导入。
    线程安全：无共享可变状态。
    """

    def __init__(self, *,
                 detector: PromptInjectionDetector = NullPromptInjectionDetector(),
                 guard: CommandGuard = NullCommandGuard(),
                 audit: AuditListener = NullAuditLogger()):
        self._detector = detector
        self._guard = guard
        self._audit = audit

    def check_input(self, user_input: str, task_id: str = "") -> InjectionResult:
        """任务级检测。返回 InjectionResult（含 decision + score）。

        内部写审计：DENY 时 log POLICY_DECISION + score + detail + task_id。
        P0-12 修复：detector 抛异常时 fail-closed（was fail-open 返回 ALLOW）。
        检测器异常意味着无法判断安全性——拒绝比放行更安全。
        """
        try:
            result = self._detector.check(user_input)
        except Exception as e:
            log.error(f"prompt injection detector error, fail-closed: {e}")
            result = InjectionResult(decision=InputDecision.DENY, score=-1.0,
                                     detail=f"detector error (fail-closed): {e}")
            self._audit.log(AuditEvent(
                ts=now_iso(), task_id=task_id,
                kind=AuditKind.POLICY_DECISION, outcome="deny",
                detail={"check": "prompt_injection", "score": -1.0,
                        "detail": f"detector error: {e}"}))
        if result.decision is InputDecision.DENY:
            self._audit.log(AuditEvent(
                ts=now_iso(), task_id=task_id,
                kind=AuditKind.POLICY_DECISION, outcome="deny",
                detail={"check": "prompt_injection", "score": result.score,
                        "detail": result.detail}))
        return result

    def check_command(self, cmd: list[str], task_id: str = "") -> GuardResult:
        """命令级检测。

        内部写审计：DENY 时 log POLICY_DECISION + reason + task_id。
        P0-12 修复：guard 抛异常时 fail-closed（was fail-open 返回 ALLOW）。
        守卫异常意味着无法判断命令安全性——拒绝比放行更安全。
        """
        try:
            result = self._guard.check(cmd)
        except Exception as e:
            log.error(f"command guard error, fail-closed: {e}")
            result = GuardResult(decision=Decision.DENY, cmd=[], reason=f"guard error (fail-closed): {e}")
            self._audit.log(AuditEvent(
                ts=now_iso(), task_id=task_id,
                kind=AuditKind.POLICY_DECISION, outcome="deny",
                detail={"check": "command_guard", "reason": f"guard error: {e}"}))
        if result.decision is Decision.DENY:
            self._audit.log(AuditEvent(
                ts=now_iso(), task_id=task_id,
                kind=AuditKind.POLICY_DECISION, outcome="deny",
                detail={"check": "command_guard", "reason": result.reason}))
        return result

    @property
    def is_null(self) -> bool:
        """是否运行在 Null 模式。启动自检用。"""
        return (isinstance(self._detector, NullPromptInjectionDetector)
                and isinstance(self._guard, NullCommandGuard))
