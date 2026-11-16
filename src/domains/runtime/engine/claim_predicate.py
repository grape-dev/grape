"""Claim 前置校验——可插拔 Predicate 链。

独立模块，不依赖 cloud_worker/cloud_base/executor。
Predicate 纯函数式：输入 task + runtime_id，输出 ClaimDecision，不改 DB。
回退逻辑由 claim 端点统一处理。

用法：
    chain = ClaimPredicateChain([LlmCredentialPredicate()])
    decision = chain.check(task, runtime_id)
    if not decision.allowed:
        # 端点统一回退 queued + 标记 claim_skip_cloud
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimDecision:
    """Predicate 校验结果。纯数据，不含副作用。"""
    allowed: bool
    reason: str = ""


class ClaimPredicate(Protocol):
    """Claim 前置校验策略。输入 task + runtime_id，输出决策。不改 DB、不改 task 状态。"""

    def check(self, task: dict, runtime_id: int) -> ClaimDecision:
        ...


class ClaimPredicateChain:
    """有序 Predicate 链。短路逻辑：任一拒绝则拒绝。

    后续加校验只需 chain.add(NewPredicate()) 或在初始化列表加一项。
    每个 Predicate 可独立测试。
    """

    def __init__(self, predicates: list[ClaimPredicate] | None = None):
        self._predicates = predicates or []

    def check(self, task: dict, runtime_id: int) -> ClaimDecision:
        for p in self._predicates:
            try:
                decision = p.check(task, runtime_id)
            except Exception as e:
                log.error(f"predicate {type(p).__name__} raised (fail-open): {e}")
                return ClaimDecision(allowed=True)
            if not decision.allowed:
                return decision  # 短路——第一个拒绝的就返回
        return ClaimDecision(allowed=True)

    def add(self, predicate: ClaimPredicate):
        self._predicates.append(predicate)


class LlmCredentialPredicate:
    """云沙箱任务必须有 LLM Key。

    只校验 runtime_type='cloud' 的任务（cloud_worker claim 的）。
    本机 worker（runtime_type='local'）/ 用户注册 worker（runtime_type='user'）→ 直接通过。
    DB 查询失败 → fail-open（不拦，避免 DB 瞬断卡死全部）。
    用户确实没配 Key → fail-closed（拒绝）。

    依赖注入 credential_reader：默认用 SandboxRepository，测试时可替换为 mock。
    不直接写 SQL。
    """

    def __init__(self, credential_reader=None):
        """Args:
            credential_reader: 可选，实现 query_llm_env(user_id, engine_type) -> dict 的对象。
                默认用 SandboxRepository。
        """
        self._reader = credential_reader

    def _get_reader(self):
        if self._reader is None:
            from src.agent.sandbox.repository import SandboxRepository
            self._reader = SandboxRepository()
        return self._reader

    def check(self, task: dict, runtime_id: int) -> ClaimDecision:
        runtime_type = task.get("runtime_type", "")
        if runtime_type != "cloud":
            return ClaimDecision(allowed=True)  # 非云沙箱不校验

        user_id = task.get("user_id", 0) or 0
        if not user_id:
            return ClaimDecision(allowed=True)  # 查不到 user_id 不拦

        try:
            reader = self._get_reader()
            # engine_type 从 task dict 的 runtime_info 提取（claim → get_task JOIN agent_runtimes）
            import json as _json
            _ri = _json.loads(task.get("runtime_info") or "{}")
            engine_type = _ri.get("engine_type", "") or ""
            llm_env = reader.query_llm_env(user_id, engine_type)
            if llm_env and llm_env.get("_LLM_API_KEY"):
                return ClaimDecision(allowed=True)  # 有 LLM Key
        except Exception as e:
            log.error(f"LlmCredentialPredicate: query failed (fail-open): {e}")
            return ClaimDecision(allowed=True)  # DB 异常 fail-open

        return ClaimDecision(
            allowed=False,
            reason=f"user {user_id} has no LLM credentials, cloud sandbox skipped"
        )
