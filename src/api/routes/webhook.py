"""Webhook & A2A 端点 — 集成事件管道"""
import json, hashlib, hmac, os
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from src.config import log
from src.api.event_pipeline import run_pipeline

router = APIRouter(tags=["webhook"])


@router.post("/webhook/gitcode")
async def gitcode_webhook(request: Request):
    """接收 GitCode Webhook 事件，经管道处理后返回"""
    body = await request.body()
    token = request.headers.get("X-GitCode-Token", "")

    # Webhook Token 验证：从 managed_repos 查找匹配的 webhook_token
    from src.user.db import resolve_repo_by_webhook_token
    repo_full_name = resolve_repo_by_webhook_token(token)
    if not repo_full_name:
        log.warning(f"Webhook rejected: invalid token")
        return JSONResponse(status_code=403, content={"code": 403, "message": "invalid webhook token"})

    log.info(f"Webhook: token OK, repo={repo_full_name}")
    result = run_pipeline(body, dict(request.headers), repo_key_override=f"gitcode:{repo_full_name}")
    return {"status": "ok", "seq": result.get("seq", 0), "event_type": result.get("event_type", "")}


@router.post("/api/a2a")
async def a2a_endpoint(request: Request):
    """Agent-to-Agent 协议端点：仅限内网调用"""
    body = await request.json()
    agent_name = body.get("agent", "")
    payload = body.get("payload", {})

    if not agent_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing agent field"})
    if not payload.get("repository"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing repository in payload"})

    client_host = request.client.host if request.client else "unknown"
    if client_host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        log.warning(f"A2A rejected from external IP: {client_host}")
        raise HTTPException(status_code=403, detail={"code": 403, "message": "A2A endpoint is internal only"})

    repository = payload.get("repository", "")
    account = payload.get("account", "default")
    context = payload.get("context", "")

    # 构造伪事件触发目标 Agent
    a2a_payload = {
        "repository": repository,
        "account": account,
        "context": context,
        "source_agent": payload.get("source_agent", ""),
        "issue_number": payload.get("issue_number", ""),
    }

    from src.api.event_store import append_event
    repo_key = f"gitcode:{repository}"
    seq = append_event(
        repo_key=repo_key,
        event_type="a2a_event",
        title=f"A2A: {agent_name}",
        issue_id=payload.get("issue_number", ""),
        agent_type=agent_name,
        source="a2a",
        payload=a2a_payload,
        status="running",
    )

    log.info(f"A2A: agent={agent_name}, repo={repository}, seq={seq}, context={context[:100] if context else ''}")

    return {"status": "ok", "seq": seq, "agent": agent_name}
