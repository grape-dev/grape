"""按派发身份取个人平台令牌（Issue #21 第 2 步）。

从 `user.db` 摘出来的原因：`db.py` 已经很大，取令牌这一段带平台分支 + 日志，
放在这里可以单测，也让 `get_task_token` 只留一行调用。

语义边界：
  · 只有 `GRAPE_DISPATCH_USER_ID=on` 才走本模块（shadow/off 返回空 → 调用方保持旧行为）；
  · 只有「有按 id 取个人令牌」能力的平台（github / gitcode）参与；gitee/gitlab 不猜；
  · 取不到令牌**不抛错**：调用方回落原链（仓库 bot → 平台 bot → 归属者）。
"""
from __future__ import annotations

from src.kernel.config import log
from src.domains.mgmt.auth.dispatch_identity import DispatchMode, dispatch_identity


def provider_of(repo_key: str) -> str:
    """repo_key 前缀即平台；裸键按 gitcode（与 kernel.repo_key 同口径）。"""
    return repo_key.split(":", 1)[0].lower() if ":" in repo_key else "gitcode"


def user_token_by_id(repo_key: str, user_id) -> str:
    """按 user_id 取个人令牌；平台不支持/无令牌返回空串（不猜、不抛）。"""
    provider = provider_of(repo_key)
    try:
        from src.domains.mgmt.user.db import (
            get_user_github_token_by_id, get_user_gitcode_token)

        if provider == "github":
            return get_user_github_token_by_id(user_id) or ""
        if provider == "gitcode":
            return get_user_gitcode_token(user_id) or ""
    except Exception as e:  # noqa: BLE001 - 取令牌失败不得影响任务
        log.warning(f"user_token_by_id({provider}, {user_id}) failed: {e}")
    return ""


def user_identity_token(task: dict, repo_key: str) -> "tuple[str, str]":
    """on 模式下按派发身份取令牌，返回 `(token, source)`；不适用时 `("", "")`。"""
    if not repo_key:
        return "", ""
    ident = dispatch_identity(task_user_id=(task or {}).get("user_id"),
                              account_name=(task or {}).get("account") or "")
    if ident.mode is not DispatchMode.ON or not ident.applies:
        return "", ""
    token = user_token_by_id(repo_key, ident.user_id)
    if token:
        log.info(f"get_task_token: dispatch identity user_id={ident.user_id} "
                 f"source={ident.source.value} provider={provider_of(repo_key)}")
        return token, ident.source.value
    log.info(f"get_task_token: dispatch identity user_id={ident.user_id} "
             f"source={ident.source.value} has no usable token for {repo_key}; "
             f"falling back to the repo chain")
    return "", ident.source.value