"""Mgmt 进程内部端点——**已停止挂载**（2026-11 架构收敛）。

mgmt 是管理态服务（开发者工作台后端），按约定不被 event/worker/insight
通过 HTTP 反向调用：其它域需要 mgmt 数据时直接读共享库或走进程内契约。
本文件保留仅为历史参考与契约定义，attach_internal_routes 不会再 include 它；
若将来要恢复，必须同时打开 InternalAuthMiddleware 的服务密钥校验并补审计日志。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.kernel.gsb.provider import get_local_service as _get_local

router = APIRouter(prefix="/internal", tags=["internal-mgmt"])


@router.get("/repos/{repo_key:path}/webhook-config")
async def get_repo_webhook_config(repo_key: str) -> dict:
    """供 MgmtServiceRemote.get_repo_webhook_config() 调用。"""
    return _get_local("mgmt").get_repo_webhook_config(repo_key)


@router.get("/sessions/{repo_key:path}/{agent_type}")
async def get_agent_session(repo_key: str, agent_type: str) -> dict:
    """供 MgmtServiceRemote.get_agent_session() 调用。"""
    return _get_local("mgmt").get_agent_session(repo_key, agent_type)


@router.get("/scenes/{repo_key:path}")
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


@router.get("/repos/{repo_key:path}/bot-config")
async def get_bot_config(repo_key: str) -> dict:
    """供 MgmtServiceRemote.get_bot_config() 调用。"""
    return _get_local("mgmt").get_bot_config(repo_key)


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str) -> dict:
    """供 MgmtServiceRemote.get_skill() 调用。"""
    result = _get_local("mgmt").get_skill(skill_id)
    return result if isinstance(result, dict) else {"result": result}
