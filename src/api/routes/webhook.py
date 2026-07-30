"""Webhook & A2A 端点 — 集成事件管道"""
import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from src.config import log
from src.api.event_pipeline import run_pipeline

router = APIRouter(tags=["webhook"])


@router.post("/webhook/gitcode")
async def gitcode_webhook(request: Request):
    """接收 GitCode Webhook 事件"""
    body = await request.body()
    token = request.headers.get("X-GitCode-Token", "")
    event_header = request.headers.get("X-Gitcode-Event", "unknown")

    log.info(f"Webhook: received event={event_header}, body_size={len(body)}, token={token[:8] if token else 'none'}...")

    # 1. 解析事件体获取 repo 名
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        log.warning("Webhook: invalid JSON body")
        return JSONResponse(status_code=400, content={"code": 400, "message": "invalid body"})

    repo = ""
    if "project" in payload and isinstance(payload["project"], dict):
        repo = payload["project"].get("path_with_namespace", "")
    if not repo and "repository" in payload and isinstance(payload["repository"], dict):
        repo = payload["repository"].get("full_name", "")

    if not repo:
        log.warning("Webhook: could not extract repo from payload")
        return JSONResponse(status_code=400, content={"code": 400, "message": "missing repository"})

    log.info(f"Webhook: extracted repo={repo}")

    # 2. 用 repo 查 managed_repos 的预期 token
    from src.user.db import get_webhook_token_by_repo
    expected = get_webhook_token_by_repo(repo)
    log.info(f"Webhook: expected_token={'found' if expected else 'none (unmanaged repo)'}")

    # 3. 对比验证（仅托管仓库需要验证）
    if expected and token != expected:
        log.warning(f"Webhook rejected: token mismatch for repo={repo}")
        return JSONResponse(status_code=403, content={"code": 403, "message": "invalid webhook token"})

    log.info(f"Webhook: token OK, repo={repo}, event={event_header}")

    # 4. 处理事件
    log.info(f"Webhook: calling run_pipeline for repo={repo}")
    result = run_pipeline(body, dict(request.headers), repo_key_override=f"gitcode:{repo}")
    log.info(f"Webhook: pipeline done, seq={result.get('seq')}, event_type={result.get('event_type')}, ok={result.get('ok')}")
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
    issue_number = payload.get("issue_number", "")

    from src.api.event_store import append_event
    repo_key = f"gitcode:{repository}"
    seq = append_event(
        repo_key=repo_key,
        event_type="a2a_event",
        title=f"A2A: {agent_name}",
        issue_id=str(issue_number),
        agent_type=agent_name,
        source="a2a",
        payload=payload,
        status="running",
    )

    log.info(f"A2A: agent={agent_name}, repo={repository}, seq={seq}")
    return {"status": "ok", "seq": seq, "agent": agent_name}
