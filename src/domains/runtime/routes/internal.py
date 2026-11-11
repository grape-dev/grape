"""Runtime 进程内部端点——仅供其他域 RemoteProvider 调用（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.7.

集群模式下挂载到 Runtime（Event/Worker）进程，提供 /internal/* 端点供
MgmtServiceRemote / InsightServiceRemote 调用。
单机模式不挂载（走 LocalProvider 直接查 DB）。

服务端一律用 Local 实现（get_local_service），否则 remote 模式下会 HTTP 回环自调用。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.kernel.gsb.provider import get_local_service as _get_local

router = APIRouter(prefix="/internal", tags=["internal-runtime"])


@router.get("/tasks")
async def list_tasks(repo_key: str, limit: int = 50) -> list:
    """供 RuntimeServiceRemote.get_tasks() 调用。"""
    return _get_local("runtime").get_tasks(repo_key, limit)


@router.get("/tasks/{task_id}/messages")
async def get_task_messages(task_id: str, after_id: int = 0) -> list:
    """供 RuntimeServiceRemote.get_task_messages() 调用。"""
    return _get_local("runtime").get_task_messages(task_id, after_id)


@router.get("/events")
async def list_events(repo_key: str, limit: int = 50) -> list:
    """供 RuntimeServiceRemote.get_events() 调用。"""
    return _get_local("runtime").get_events(repo_key, limit)


@router.get("/events/{seq}")
async def get_event(seq: int) -> dict:
    """供 RuntimeServiceRemote.get_event_detail() 调用。"""
    return _get_local("runtime").get_event_detail(seq)


@router.get("/runtimes")
async def list_runtimes() -> list:
    """供 RuntimeServiceRemote.get_runtimes() 调用。"""
    return _get_local("runtime").get_runtimes()


@router.get("/issues/{repo_key}/{issue_number}/state")
async def get_issue_state(repo_key: str, issue_number: int) -> dict:
    """供 RuntimeServiceRemote.get_issue_state() 调用。"""
    return _get_local("runtime").get_issue_state(repo_key, issue_number)


@router.post("/issues/{repo_key}/{issue_number}/state")
async def update_issue_state(repo_key: str, issue_number: int, body: dict) -> dict:
    """供 RuntimeServiceRemote.update_issue_state() 调用。"""
    return _get_local("runtime").update_issue_state(repo_key, issue_number, body)


@router.get("/chat/conversations")
async def list_chat_conversations(repo_key: str = "", limit: int = 50) -> list:
    """供 RuntimeServiceRemote.get_chat_conversations() 调用（v2.2：chat 表归运行域）。"""
    return _get_local("runtime").get_chat_conversations(repo_key, limit)


@router.get("/chat/conversations/{conv_id}/messages")
async def get_chat_messages(conv_id: str, limit: int = 100, before: int = 0) -> list:
    """供 RuntimeServiceRemote.get_chat_messages() 调用（v2.2：chat 表归运行域）。"""
    return _get_local("runtime").get_chat_messages(conv_id, limit, before)
