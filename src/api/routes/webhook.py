"""Webhook & A2A 端点 — 集成事件管道"""
import json, hashlib, hmac, os
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from src.config import log, GITCODE_WEBHOOK_TOKEN, resolve_account_by_webhook_token
from src.api.event_pipeline import run_pipeline

router = APIRouter(tags=["webhook"])


@router.post("/webhook/gitcode")
async def gitcode_webhook(request: Request):
    """接收 GitCode Webhook 事件，经管道处理后返回"""
    body = await request.body()
    token = request.headers.get("X-GitCode-Token", "")

    # Webhook Token 验证（三层）
    if GITCODE_WEBHOOK_TOKEN and token != GITCODE_WEBHOOK_TOKEN:
        resolved_account = resolve_account_by_webhook_token(token)
        if not resolved_account:
            log.warning(f"Webhook rejected: invalid token")
            return JSONResponse(status_code=403, content={"code": 403, "message": "invalid webhook token"})
        account = resolved_account
    else:
        account = resolve_account_by_webhook_token(token) or "default"

    log.info(f"Webhook: token OK, account={account}")

    # 通过事件管道处理
    result = run_pipeline(body, dict(request.headers), account=account)

    return {"status": "ok", "seq": result.get("seq", 0), "event_type": result.get("event_type", "")}


@router.post("/api/a2a")
async def a2a_endpoint(request: Request):
    """Agent-to-Agent 协议端点：仅限内网调用"""
    client_host = request.client.host if request.client else "unknown"
    if client_host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        log.warning(f"A2A rejected from external IP: {client_host}")
        raise HTTPException(status_code=403, detail={"code": 403, "message": "A2A endpoint is internal only"})

    body = await request.json()
    agent_name = body.get("agent", "")
    payload = body.get("payload", {})

    if not agent_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing agent field"})

    repository = payload.get("repository", "")
    account = payload.get("account", "default")
    context = payload.get("context", "")

    if not repository:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing repository in payload"})

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
