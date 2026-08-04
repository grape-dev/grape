"""Webhook & A2A 端点 — 集成事件管道"""
import json, time
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

    # 3. 未托管仓库拒绝：不在 managed_repos 的仓库不接收事件（防止幽灵 webhook / 无主事件 / 绕过校验）
    if not expected:
        log.warning(f"Webhook rejected: unmanaged repo={repo}")
        return JSONResponse(status_code=404, content={"code": 404, "message": "repository not managed"})

    # 4. token 对比验证
    if token != expected:
        log.warning(f"Webhook rejected: token mismatch for repo={repo}")
        return JSONResponse(status_code=403, content={"code": 403, "message": "invalid webhook token"})

    log.info(f"Webhook: token OK, repo={repo}, event={event_header}")

    # 5. 处理事件
    log.info(f"Webhook: calling run_pipeline for repo={repo}")
    result = run_pipeline(body, dict(request.headers), account=repo, repo_key_override=f"gitcode:{repo}")
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
    if not payload.get("issue_number"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing issue_number in payload: 所有 A2A 事件必须关联一个 Issue"})

    client_host = request.client.host if request.client else "unknown"
    if client_host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        log.warning(f"A2A rejected from external IP: {client_host}")
        raise HTTPException(status_code=403, detail={"code": 403, "message": "A2A endpoint is internal only"})

    repository = payload.get("repository", "")
    account = payload.get("account", "") or repository
    context = payload.get("context", "")
    issue_number = payload.get("issue_number", "")
    pr_number = payload.get("pr_number", "")

    repo_key = f"gitcode:{repository}"
    # 统一 repo_key 格式（与 run_pipeline 一致：去掉 "source:" 前缀，所有查询都用 "owner/repo"）
    if ":" in repo_key:
        repo_key = repo_key.split(":", 1)[1]
    log.info(f"A2A: agent={agent_name}, repo={repository}")
    seq = 0

    # 启动后台执行目标 Agent（由 _execute_agent 判定产出后存储事件）
    try:
        from src.api.event_pipeline import _execute_agent
        from src.session_manager import db, db_lock
        backend = "opencode"
        skill_name = ""
        skill_path = ""
        repo_name = repository.split("/", 1)[-1] if "/" in repository else repository
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT backend_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repository, agent_name))
                row = c.fetchone()
                if row:
                    backend = "opencode"
                    if row[0]:
                        c.execute("SELECT name FROM backend_agents WHERE id=?", (row[0],))
                        brow = c.fetchone()
                        if brow and brow[0]:
                            backend = brow[0]
                    skill_name = row[1] or ""
                    if row[2]:
                        from src.user.db import get_skill_by_id
                        _sk = get_skill_by_id(row[2])
                        if _sk:
                            skill_name = _sk.get("skill_name") or skill_name
                            skill_path = _sk.get("skill_path") or skill_name
        except Exception:
            pass

        evt = {
            "type": "a2a_event",
            "repository": repository,
            "source": "a2a",
            "actor": "",
            "account": account,
            "issue_id": str(issue_number) if issue_number else "",
            "_a2a_context": context,
            "_skill_path": skill_path,
            "payload": {
                "repository": repository,
                "account": account,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "context": context,
            },
        }

        # 立即创建 event（status=running），Agent 执行完再更新
        from src.api.event_store import append_event
        seq = append_event(
            repo_key=repo_key,
            event_type="a2a_event",
            title=f"A2A: {agent_name}",
            issue_id=f"#{issue_number}" if issue_number else "#0",
            source="a2a",
            agent_type=agent_name,
            actor="",
            summary=f"A2A: → {agent_name}",
            payload={"repository": repository, "account": account, "issue_number": issue_number, "pr_number": pr_number, "context": context[:500]},
            status="running",
        )
        log.info(f"A2A: event created, seq={seq}")

        import threading
        threading.Thread(target=_execute_agent, args=(
            repo_key, seq, agent_name, backend, skill_name or agent_name, evt,
        ), daemon=True, name=f"a2a-{agent_name}-{seq}").start()
        log.info(f"A2A: started agent execution for {agent_name}")
    except Exception as e:
        log.warning(f"A2A: failed to start agent execution: {e}")
        seq = 0

    return {"status": "ok", "seq": seq, "agent": agent_name}
