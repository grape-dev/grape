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

    # 记录来源 IP（用于排查 GitCode webhook IP 变更后防火墙/白名单问题）
    client_host = request.client.host if request.client else "unknown"
    xff = request.headers.get("X-Forwarded-For", "")
    xri = request.headers.get("X-Real-IP", "")
    log.info(f"Webhook: from ip={client_host}, xff={xff or 'none'}, xri={xri or 'none'}, "
             f"event={event_header}, body_size={len(body)}, token={token[:8] if token else 'none'}...")

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

    # 仓库级 bot token 鉴权（替代纯内网 IP 校验，支持远端 worker）
    # Agent 调用时带 Authorization: Bearer <BOT_GITCODE_TOKEN>（或 X-Bot-Token header）
    repository = payload.get("repository", "")
    account = payload.get("account", "") or repository
    context = payload.get("context", "")
    issue_number = payload.get("issue_number", "")
    pr_number = payload.get("pr_number", "")

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"gitcode:{repository}") if repository else ""
    log.info(f"A2A: agent={agent_name}, repo={repository}")
    seq = 0

    # 鉴权：Bearer token 对比该 repo 的 bot token（Agent 的 BOT_GITCODE_TOKEN）
    # 复用仓库级 bot token 机制（对齐 webhook 鉴权思路），支持远端 worker 调用。
    # 仅当仓库已托管（库中有 bot token）时严格校验；未托管仓库放行，交由后续 agent 配置查找处理。
    _auth = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not _auth:
        _auth = request.headers.get("X-Bot-Token", "") or ""
    if repo_key:
        from src.user.db import get_repo_bot_token_no_global
        _expected = get_repo_bot_token_no_global(repo_key) or ""
        if _expected and _auth == _expected:
            log.info(f"A2A: bot token OK, agent={agent_name}")
        elif not _expected:
            log.warning(f"A2A: repo not managed (no bot token), defer to downstream: {repo_key}")
        else:
            log.warning(f"A2A: unauthorized (token mismatch) for repo={repo_key}")
            raise HTTPException(status_code=403, detail={"code": 403, "message": "invalid bot token"})
    else:
        log.warning(f"A2A: missing repository, reject")
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing repository in payload"})

    # Agent 启用检查（仓库级 enable，见 agent-enable-toggle-design.md）
    from src.api.session_store import is_agent_enabled
    if not is_agent_enabled(repo_key, agent_name):
        log.warning(f"A2A: agent '{agent_name}' disabled for repo={repo_key}, rejected")
        return JSONResponse(status_code=403, content={"code": 403, "message": f"agent {agent_name} disabled"})

    # 启动后台执行目标 Agent（由 _execute_agent 判定产出后存储事件）
    try:
        from src.api.event_pipeline import _execute_agent
        from src.session_manager import db, db_lock
        backend = "opencode"
        runtime_id = 0
        skill_name = ""
        skill_id = 0
        skill_path = ""
        repo_name = repository.split("/", 1)[-1] if "/" in repository else repository
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_name))
                row = c.fetchone()
                if row:
                    runtime_id = row[0] or 0
                    backend = "opencode"
                    if row[0]:
                        c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
                        brow = c.fetchone()
                        if brow and brow[0]:
                            backend = brow[0]
                    skill_name = row[1] or ""
                    skill_id = row[2] or 0
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

        # Issue 生命周期阶段推进（D9/D10，见 vod-skill-upgrade-plan.md §3.0.2）
        # A2A 交办 → issue_state 归属转移到目标 agent（qa 回 coding 允许回退）
        if issue_number:
            try:
                from src.api.session_store import set_issue_stage
                _stage = agent_name if agent_name in ("vod", "issue", "coding", "qa", "sre", "done") else "issue"
                set_issue_stage(repo_key, f"#{issue_number}", _stage, agent_name, a2a_seq=None)
                log.info(f"A2A: issue_state updated issue=#{issue_number} stage={_stage} owner={agent_name}")
            except Exception as _e:
                log.warning(f"A2A: issue_state update failed: {_e}")

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

        # 执行分派：与 run_pipeline 一致——auto/direct 就地线程；queue 模式入队（A2A 也走 agent_tasks，
        # 使 pipeline executing 派生/串行槽/可观测性与 webhook 事件一致，见 a2a-queue-refactor-design.md）
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        kind = runtime_execution_kind(runtime_id or 0)
        if kind == EXEC_MODE_DIRECT:
            import threading
            threading.Thread(target=_execute_agent, args=(
                repo_key, seq, agent_name, backend, skill_name or agent_name, evt,
            ), daemon=True, name=f"a2a-{agent_name}-{seq}").start()
            log.info(f"A2A: started agent execution for {agent_name} (direct)")
        else:
            from src.api.event_pipeline import _enqueue_pipeline_task
            _enqueue_pipeline_task(repo_key, seq, agent_name, backend,
                                   skill_name or agent_name, evt,
                                   skill_id, skill_path, runtime_id or 0,
                                   f"#{issue_number}" if issue_number else "")
            log.info(f"A2A: enqueued task for {agent_name}, seq={seq}, mode={kind}")
    except Exception as e:
        log.warning(f"A2A: failed to start agent execution: {e}")
        # 事件已创建（running）但执行启动失败 → 立即置 failed，防卡 running 僵尸
        if seq:
            try:
                from src.api.event_store import update_event_status
                update_event_status(seq, "failed")
            except Exception:
                pass
        seq = 0

    return {"status": "ok", "seq": seq, "agent": agent_name}
