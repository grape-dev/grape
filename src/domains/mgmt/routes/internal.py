"""Mgmt 进程内部端点——仅供其他域 RemoteProvider 调用（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.7.

集群模式下挂载到 Mgmt 进程，提供 /internal/* 端点供
RuntimeServiceRemote / InsightServiceRemote 调用。
单机模式不挂载（走 LocalProvider 直接查 DB）。

服务端一律用 Local 实现（get_local_service），否则 remote 模式下会 HTTP 回环自调用。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.kernel.gsb.provider import get_local_service as _get_local

router = APIRouter(prefix="/internal", tags=["internal-mgmt"])


@router.get("/repos/{repo_key}/webhook-config")
async def get_repo_webhook_config(repo_key: str) -> dict:
    """供 MgmtServiceRemote.get_repo_webhook_config() 调用。"""
    return _get_local("mgmt").get_repo_webhook_config(repo_key)


@router.get("/sessions/{repo_key}/{agent_type}")
async def get_agent_session(repo_key: str, agent_type: str) -> dict:
    """供 MgmtServiceRemote.get_agent_session() 调用。"""
    return _get_local("mgmt").get_agent_session(repo_key, agent_type)


@router.get("/scenes/{repo_key}")
async def get_scenes(repo_key: str) -> list:
    """供 MgmtServiceRemote.get_scene_config() 调用。"""
    return _get_local("mgmt").get_scene_config(repo_key)


@router.get("/scenes/{scene_id}/engine-ids")
async def get_scene_engine_ids(scene_id: str) -> list:
    """供 MgmtServiceRemote.get_scene_pool_engine_ids() 调用。"""
    return _get_local("mgmt").get_scene_pool_engine_ids(scene_id)


@router.post("/tokens/verify")
async def verify_token(body: dict) -> dict:
    """供 MgmtServiceRemote.verify_session_token() 调用。"""
    return _get_local("mgmt").verify_session_token(body.get("token_hash", ""))


@router.get("/repos/{repo_key}/bot-config")
async def get_bot_config(repo_key: str) -> dict:
    """供 MgmtServiceRemote.get_bot_config() 调用。"""
    return _get_local("mgmt").get_bot_config(repo_key)


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str) -> dict:
    """供 MgmtServiceRemote.get_skill() 调用。"""
    result = _get_local("mgmt").get_skill(skill_id)
    return result if isinstance(result, dict) else {"result": result}
