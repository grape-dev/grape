"""Webhook & A2A 端点 — 集成事件管道"""
import json, os, time
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from src.config import log
from src.api.event_pipeline import run_pipeline

router = APIRouter(tags=["webhook"])


async def _on_handoff_dispatch(event):
    """Event-driven A2A dispatch listener (Phase 4).

    Listens on 'agent/handoff' events emitted by the A2A endpoint.
    Uses pipeline_executor.dispatch_node to route through the configured event mode.
    """
    import asyncio
    from src.api.pipeline_executor import dispatch_node
    from src.config import log

    payload = event.payload or {}
    target_agent = payload.get("target_agent", event.agent_type)
    skill_name = payload.get("skill", "")
    skill_id = payload.get("skill_id", 0)
    skill_path = payload.get("skill_path", "")
    runtime_id = payload.get("runtime_id", 0)
    scene_id = payload.get("scene_id", "default")
    node_id = payload.get("node_id", target_agent)
    evt = payload.get("evt", {})

    log.info(f"agent/handoff listener: dispatching {target_agent} (node={node_id}, scene={scene_id})")

    try:
        result = await dispatch_node(
            scene_id=scene_id,
            node_id=node_id,
            repo_key=event.repo_key,
            issue_id=event.issue_id,
            event_seq=event.seq,
            evt=evt,
            skill_name=skill_name,
            skill_id=skill_id,
            skill_path=skill_path,
            runtime_id=runtime_id,
            pool_tag=target_agent,
        )
        log.info(f"agent/handoff listener: result={result}")
        if result.get("status") == "failed":
            from src.api.event_store import update_event_status
            update_event_status(event.seq, "failed")
    except Exception as e:
        log.warning(f"agent/handoff listener failed: {e}")
        try:
            from src.api.event_store import update_event_status
            update_event_status(event.seq, "failed")
        except Exception:
            pass


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

    # 5. 处理事件 — 异步执行，立即返回 200 避免 GitCode webhook 超时 (503)
    import asyncio as _aio
    loop = _aio.get_event_loop()
    _aio.ensure_future(_aio.to_thread(run_pipeline, body, dict(request.headers),
                                     account=repo, repo_key_override=f"gitcode:{repo}"))
    log.info(f"Webhook: pipeline scheduled async for repo={repo}, event={event_header}")
    return {"status": "ok", "seq": 0, "event_type": event_header, "message": "pipeline scheduled"}


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
        backend = ""
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
                    backend = ""
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

        # 回退：DB 未配置 skill 时从 pipelines.json (plugin) 或 agent-skill-mapping.json (legacy) 获取默认 skill
        if not skill_name:
            # Plugin mode: try pipelines.json first
            try:
                from src.api.event_pipeline import is_plugin_mode, resolve_skill_from_pipeline
                if is_plugin_mode():
                    _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_name)
                    if _pipe_skill:
                        skill_name = _pipe_skill
                        log.info(f"A2A (plugin): skill for {agent_name}={_pipe_skill}")
            except Exception as e:
                log.warning(f"A2A: plugin skill lookup failed: {e}")
            if not skill_name:
                # Legacy fallback: agent-skill-mapping.json
                try:
                    _defaults_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs", "agent-skill-mapping.json")
                    if os.path.exists(_defaults_path):
                        with open(_defaults_path, "r", encoding="utf-8") as _f:
                            _defaults = json.load(_f)
                        _scene = _defaults.get("scenes", {}).get("default", {})
                        try:
                            from src.user.db import get_repo_scene
                            _scene_id = get_repo_scene(repo_key) or "default"
                            _scene = _defaults.get("scenes", {}).get(_scene_id, _defaults.get("scenes", {}).get("default", {}))
                        except Exception:
                            pass
                        _mapping = _scene.get("mapping", {}) if isinstance(_scene, dict) else {}
                        _default_skill = _mapping.get(agent_name, "")
                        if _default_skill:
                            skill_name = _default_skill
                            log.info(f"A2A: fallback skill for {agent_name}={_default_skill}")
                except Exception as e:
                    log.warning(f"A2A: fallback skill lookup failed: {e}")

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

        # 订阅感知 A2A 去重（event-routing-subscription-design.md §2.6）：
        # SRE 已配置订阅 → PR 合并事件触发的 A2A 交办视为重复，只推进 stage、不创建任务。
        # 但无 pr_number 的直接部署请求（Issue A2A 交办、非 PR 场景）不跳过，需正常创建任务。
        if agent_name == "sre" and issue_number and pr_number:
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute(
                        "SELECT subscribe_events FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'",
                        (repo_key,))
                    row = c.fetchone()
                    if row:
                        _raw = (row[0] or "").strip()
                        if _raw and _raw != "[]" and _raw != "null":
                            log.info(f"A2A: sre subscribed + pr_number={pr_number}, skip task creation (stage=sre, issue=#{issue_number})")
                            return JSONResponse(status_code=200, content={"status": "ok", "seq": 0, "agent": agent_name, "skipped": "sre_subscribed"})
            except Exception:
                pass

        # 同 issue + 同 agent_type 的未完成 A2A 去重：
        # PR 合并后 QA 和 Issue Agent 可能同时触发 A2A → sre，导致重复创建部署任务。
        # 检查是否已有 running/dispatched 状态的同 agent A2A 事件，有则跳过。
        if issue_number:
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute(
                        "SELECT seq FROM events WHERE repo_key=? AND issue_id=? AND agent_type=? AND event_type='a2a_event' AND status IN ('running','dispatched') ORDER BY seq DESC LIMIT 1",
                        (repo_key, f"#{issue_number}", agent_name))
                    _existing = c.fetchone()
                    if _existing:
                        log.info(f"A2A: duplicate skip — issue=#{issue_number} agent={agent_name} already has running event seq={_existing[0]}")
                        return JSONResponse(status_code=200, content={"status": "ok", "seq": 0, "agent": agent_name, "skipped": "duplicate_a2a", "existing_seq": _existing[0]})
            except Exception as _de:
                log.warning(f"A2A: duplicate check failed: {_de}")

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

        # Plugin mode: emit agent/handoff event → event bus → pipeline_executor dispatches
        # (event-dispatch-redesign-v2.md Phase 4: A2A upgraded to event-driven)
        try:
            from src.api.event_pipeline import is_plugin_mode
            if is_plugin_mode():
                import asyncio as _aio
                from src.api.event_bus import get_event_bus, GrapeEvent, EventDomain
                from src.api.pipeline_config import get_pipeline_config, get_node_by_tag

                # Determine scene and node config
                scene_id = "default"
                try:
                    from src.user.db import get_repo_scene
                    scene_id = get_repo_scene(repo_key) or "default"
                except Exception:
                    pass

                node = get_node_by_tag(scene_id, agent_name)
                mode = node.get("mode", "emit") if node else "emit"

                handoff_event = GrapeEvent(
                    domain=EventDomain.AGENT,
                    type="agent/handoff",
                    repo_key=repo_key,
                    issue_id=f"#{issue_number}" if issue_number else "",
                    agent_type=agent_name,
                    seq=seq,
                    payload={
                        "target_agent": agent_name,
                        "skill": skill_name,
                        "skill_id": skill_id,
                        "skill_path": skill_path,
                        "runtime_id": runtime_id,
                        "mode": mode,
                        "scene_id": scene_id,
                        "node_id": node["id"] if node else agent_name,
                        "evt": evt,
                    },
                )

                # Register the dispatch listener (idempotent — only once)
                bus = get_event_bus()
                if not bus.has_listeners("agent/handoff"):
                    bus.on_sync("agent/handoff", _on_handoff_dispatch)

                # Emit the handoff event (async, run in background thread for sync endpoint)
                loop = _aio.get_event_loop()
                if loop.is_running():
                    _aio.ensure_future(bus.emit(handoff_event))
                else:
                    _aio.run(bus.emit(handoff_event))

                log.info(f"A2A (plugin): agent/handoff emitted for {agent_name}, seq={seq}, mode={mode}")
                return {"status": "ok", "seq": seq, "agent": agent_name, "mode": "event-driven"}
        except Exception as e:
            log.warning(f"A2A plugin event-driven path failed: {e}, falling back to legacy dispatch")

        # Legacy dispatch (also serves as fallback for plugin mode)
        from src.runtime.engine_select import pick_engine, engine_name
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        engine_id = pick_engine(repo_key, agent_name, fallback_runtime_id=runtime_id or 0)
        if not engine_id:
            log.info(f"A2A: no available engine for repo={repo_key} agent={agent_name}, seq={seq}, skip")
            try:
                from src.api.event_store import update_event_status
                update_event_status(seq, "failed")
            except Exception:
                pass
            return {"status": "ok", "seq": 0, "agent": agent_name}
        # backend 取已注册引擎的类型名（不沿用字面默认，engine-selection-design.md v2）
        _be = engine_name(engine_id) or backend
        kind = runtime_execution_kind(engine_id)
        if kind == EXEC_MODE_DIRECT:
            from src.api.event_pipeline import start_direct_agent, _create_direct_task
            _issue_id = f"#{issue_number}" if issue_number else ""
            evt["_skill_id"] = skill_id
            _create_direct_task(repo_key, seq, agent_name, _be,
                                skill_name or agent_name, skill_path or "",
                                engine_id, evt, _issue_id, skill_id=skill_id)
            start_direct_agent(_execute_agent, (
                repo_key, seq, agent_name, _be, skill_name or agent_name, evt, engine_id,
            ), name=f"a2a-{agent_name}-{seq}")
            log.info(f"A2A: started agent execution for {agent_name} (direct, engine={engine_id})")
        else:
            from src.api.event_pipeline import _enqueue_pipeline_task
            _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                                   skill_name or agent_name, evt,
                                   skill_id, skill_path, engine_id,
                                   f"#{issue_number}" if issue_number else "")
            log.info(f"A2A: enqueued task for {agent_name}, seq={seq}, engine={engine_id}, mode={kind}")
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
