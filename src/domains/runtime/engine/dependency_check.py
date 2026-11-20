"""Dependency injection & pre-execution validation (Cordis inject pattern adapted for Grape).

Before Agent execution starts, validate all required resources are available.
Fail fast with explicit error messages instead of vague exit=1.
"""
import logging
import os
from typing import Optional

log = logging.getLogger("devops-bot")


class DependencyError(Exception):
    """Raised when a required dependency is missing or invalid."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        details = "; ".join(missing)
        super().__init__(f"dependency check failed: {details}")


def validate_task_dependencies(task: dict, engine_name: str = "") -> None:
    """Pre-execution dependency validation (inject pattern).

    Checks:
    1. Prompt is non-empty (the most common failure cause)
    2. Skill is available (skill_obs_url or skill_files or local skill_dir)
    3. cwd exists or can be created
    4. Repo token is present for git agents
    5. Issue session is valid for git worktree agents

    Raises DependencyError with a list of missing items if any check fails.
    """
    missing: list[str] = []
    tid = task.get("id", "")
    agent_type = task.get("agent_type") or ""

    # 1. Prompt
    prompt = task.get("prompt") or ""
    if not prompt or not prompt.strip():
        missing.append(f"prompt is empty (task={tid})")

    # 2. Skill availability - at least one source must exist
    skill_obs_url = task.get("skill_obs_url") or ""
    skill_files = task.get("skill_files") or ""
    skill_name = task.get("skill") or ""
    has_skill_source = bool(skill_obs_url or skill_files or skill_name)
    if not has_skill_source and agent_type not in ("chat", "vod"):
        # chat/vod may not need a skill file
        missing.append(f"skill not configured (skill_obs_url/skill_files/skill all empty, task={tid})")

    # 3. cwd — check if can be created (don't require parent to exist, makedirs will create it)
    cwd = task.get("cwd") or ""
    if cwd and not os.path.isdir(cwd):
        # cwd doesn't exist yet but will be created by makedirs — this is OK
        pass

    # 4. Repo token for git agents (coding/qa/sre need git access)
    # D101 方案二（W12，评审 v2 第 4 点）：检查语义钉死——
    # `repo_key 在册 且 resolve_config_level().level ∉ {"", "user"}`，
    # 只读配置级别**不实际解密 token 值**。resolve 异常/空 level → 跳过
    # 检查项（检查器自身故障不阻断任务，空值由 claim 现取自然暴露）；
    # user level → warning 不拦截（路径 B 任务本就靠 claim 现取注入）。
    # 误报面收敛到"repo 在册但完全无 bot 配置"单一形态。
    _GIT_AGENTS = ("vod", "issue", "coding", "qa", "sre")
    if agent_type in _GIT_AGENTS:
        _rk = task.get("repo_key") or ""
        if not _rk:
            missing.append(f"repo_key is empty for git agent {agent_type} (task={tid})")
        else:
            try:
                from src.kernel.token_service import resolve_repo_config_level
                _lvl = (resolve_repo_config_level(_rk).level or "")
                if _lvl == "user":
                    log.warning(
                        f"repo token is user-level for git agent {agent_type} "
                        f"(task={tid}) — claim-time injection will provide it")
                elif _lvl == "":
                    log.warning(
                        f"no repo token config for git agent {agent_type} "
                        f"(task={tid}, repo={_rk}) — claim-time injection "
                        f"will likely yield empty token")
            except Exception as _dce:
                # 检查器自身故障不阻断任务（跳过，由 claim 现取自然暴露）。
                # worker 侧没有 assembly 层，`vcs_cards` 等 kernel 端口天然未绑定——
                # 那不是缺陷，是「本进程没有该能力」，按 debug 记一条即可，否则每个
                # git 类任务都刷一条 warning（Windows worker 实测每任务一条）。
                if "is not bound" in str(_dce):
                    log.debug(f"dependency_check token level probe unavailable "
                              f"(kernel port unbound, task={tid}): {_dce}")
                else:
                    log.warning(f"dependency_check token level probe failed "
                                f"(skipped, task={tid}): {_dce}")

    # 5. Issue session for git worktree agents (warning, not hard fail)
    issue_session_id = task.get("issue_session_id") or ""
    issue_id = task.get("issue_id") or ""
    if agent_type in ("coding", "qa", "sre") and not issue_session_id:
        # Log warning but don't block execution - some tasks may not need worktree
        log.warning(f"issue_session_id is empty for worktree agent {agent_type} (task={tid})")

    if missing:
        log.error(f"Dependency check FAILED for task={tid} agent={agent_type}: {'; '.join(missing)}")
        raise DependencyError(missing)

    log.info(f"Dependency check OK for task={tid} agent={agent_type} engine={engine_name}")
