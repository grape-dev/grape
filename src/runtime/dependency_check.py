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

    # 3. cwd
    cwd = task.get("cwd") or ""
    if cwd:
        parent = os.path.dirname(cwd)
        if parent and not os.path.isdir(parent) and not os.path.exists(parent):
            missing.append(f"cwd parent dir does not exist: {parent}")

    # 4. Repo token for git agents (coding/qa/sre need git access)
    _GIT_AGENTS = ("vod", "issue", "coding", "qa", "sre")
    if agent_type in _GIT_AGENTS:
        token = task.get("repo_gitcode_token") or ""
        if not token:
            missing.append(f"repo_gitcode_token is empty for git agent {agent_type} (task={tid})")

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
