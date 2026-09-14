"""Webhook & A2A 端点 — 集成事件管道"""
import json, os, time
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from src.config import log
from src.api.deps import get_user_or_none
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
    scene_user_id = payload.get("scene_user_id", 0)
    node_id = payload.get("node_id", target_agent)
    evt = payload.get("evt", {})

    log.info(f"agent/handoff listener: dispatching {target_agent} (node={node_id}, scene={scene_id}, uid={scene_user_id})")

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
            user_id=scene_user_id,
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
    """接收 GitCode Webhook 事件（也兼容误入的 GitHub webhook）。"""
    body = await request.body()

    # GitHub webhook 误入 /webhook/gitcode 时直接处理（不转发，body 已消费）
    gh_event = request.headers.get("X-GitHub-Event", "")
    if gh_event:
        log.info(f"GitCode webhook: GitHub event detected ({gh_event}), processing as GitHub webhook")
        return await _process_github_webhook(body, request.headers)

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


@router.post("/webhook/github")
async def github_webhook(request: Request):
    """接收 GitHub Webhook 事件"""
    body = await request.body()
    return await _process_github_webhook(body, request.headers)


async def _process_github_webhook(body: bytes, headers) -> dict:
    """GitHub Webhook 核心处理逻辑（body 已消费，直接传入）。"""
    event_header = headers.get("X-GitHub-Event", "unknown")

    client_host = ""  # 转发场景下无 client 信息
    log.info(f"GitHub Webhook: event={event_header}, body_size={len(body)}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        log.warning("GitHub Webhook: invalid JSON body")
        return JSONResponse(status_code=400, content={"code": 400, "message": "invalid body"})

    repo_obj = payload.get("repository", {})
    repo = repo_obj.get("full_name", "") if isinstance(repo_obj, dict) else ""
    if not repo:
        log.warning("GitHub Webhook: could not extract repo from payload")
        return JSONResponse(status_code=400, content={"code": 400, "message": "missing repository"})

    log.info(f"GitHub Webhook: extracted repo={repo}, event={event_header}")

    from src.user.db import get_webhook_token_by_repo
    expected = get_webhook_token_by_repo(repo)

    if not expected:
        log.warning(f"GitHub Webhook rejected: unmanaged repo={repo}")
        return JSONResponse(status_code=404, content={"code": 404, "message": "repository not managed"})

    # GitHub webhook 验证：X-Hub-Signature-256 HMAC
    import hashlib, hmac
    signature = headers.get("X-Hub-Signature-256", "")
    if signature:
        computed = hmac.new(expected.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(f"sha256={computed}", signature):
            log.warning(f"GitHub Webhook rejected: signature mismatch for repo={repo}, expected_sha256=sha256={computed[:20]}..., got_sha256={signature[:25]}..., body_size={len(body)}, token_prefix={expected[:10]}...")
            return JSONResponse(status_code=403, content={"code": 403, "message": "invalid signature"})
    else:
        token_header = headers.get("X-GitHub-Token", "")
        if token_header and token_header != expected:
            log.warning(f"GitHub Webhook rejected: token mismatch for repo={repo}")
            return JSONResponse(status_code=403, content={"code": 403, "message": "invalid token"})

    log.info(f"GitHub Webhook: verified, repo={repo}, event={event_header}")

    import asyncio as _aio
    _aio.ensure_future(_aio.to_thread(run_pipeline, body, dict(headers),
                                     account=repo, repo_key_override=f"github:{repo}"))
    log.info(f"GitHub Webhook: pipeline scheduled async for repo={repo}, event={event_header}")
    return {"status": "ok", "seq": 0, "event_type": event_header, "message": "pipeline scheduled"}


def _authorize_a2a(request: Request, repo_key: str):
    """A2A 端点仓库级鉴权：bot token 或会话级临时 token（repo 作用域）二选一通过。

    - bot token：仓库级（managed_repos / devops_agent_session），支持远端 worker；
    - 会话 token（GRAPE_SESSION_TOKEN，M2）：校验 repo_key 匹配——该仓库执行期 agent 凭据。
    仅当仓库已托管（有 bot token）才严格校验 bot；未托管仓库允许会话 token 或交由后续校验。
    """
    _auth = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not _auth:
        _auth = request.headers.get("X-Bot-Token", "") or ""
    if not repo_key:
        log.warning("A2A: missing repository, reject")
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing repository in payload"})
    from src.user.db import get_repo_bot_token_no_global
    from src.api.auth_token import verify_session_token_for_repo
    _expected = get_repo_bot_token_no_global(repo_key) or ""
    _bot_ok = bool(_expected) and _auth == _expected
    _session_ok = bool(verify_session_token_for_repo(_auth, repo_key))
    if _bot_ok or _session_ok:
        log.info(f"A2A: token OK (bot={_bot_ok}, session={_session_ok}) repo={repo_key}")
        return
    if not _expected:
        log.warning(f"A2A: repo not managed (no bot token) & no matching session token, defer to downstream: {repo_key}")
        return
    log.warning(f"A2A: unauthorized (token mismatch) for repo={repo_key}")
    raise HTTPException(status_code=403, detail={"code": 403, "message": "invalid token"})


class A2AHandoffBody(BaseModel):
    agent: str = ""
    payload: dict = {}


@router.post("/api/a2a", operation_id="a2a_handoff", tags=["a2a"], summary="Agent 交办（A2A，grape a2a handoff）")
async def a2a_endpoint(request: Request, body: A2AHandoffBody, user: dict | None = Depends(get_user_or_none)):
    """Agent-to-Agent 协议端点：对外安全路由（JWT/会话 token/API Key）+ 内部 bot-token 兼容。"""
    agent_name = body.agent or ""
    payload = body.payload or {}

    if not agent_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing agent field"})
    if not payload.get("repository"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing repository in payload"})
    if not payload.get("issue_number"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing issue_number in payload: 所有 A2A 事件必须关联一个 Issue"})

    repository = payload.get("repository", "")
    account = payload.get("account", "") or repository
    context = payload.get("context", "")
    issue_number = payload.get("issue_number", "")
    pr_number = payload.get("pr_number", "")

    from src.repo_key import to_repo_key
    # 根据 repository 是否带 github: 前缀判断 provider
    if repository.startswith("github:"):
        repo_key = repository
    elif repository.startswith("gitcode:"):
        repo_key = to_repo_key(repository)
    else:
        # 无前缀：检查 managed_repos 确定实际 provider
        try:
            from src.session_manager import db as _pdb, db_lock as _pdb_lock
            with _pdb_lock:
                _pc = _pdb.cursor()
                _pc.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?)", (f"github:{repository}", f"gitcode:{repository}"))
                _pr = _pc.fetchone()
            if _pr:
                repo_key = _pr[0]
            else:
                repo_key = to_repo_key(repository)
        except Exception:
            repo_key = to_repo_key(repository)
    log.info(f"A2A: agent={agent_name}, repo={repository}")

    # Issue #51: cancelled 任务的 A2A 信号一律拒绝（防止取消后流水线继续流转）
    if issue_number and repo_key:
        try:
            from src.session_manager import db as _ck_db, db_lock as _ck_lock
            with _ck_lock:
                _ck_c = _ck_db.cursor()
                _ck_c.execute("SELECT status FROM tasks WHERE repo_key=? AND issue_number=? AND status='cancelled' LIMIT 1",
                              (repo_key, int(issue_number)))
                if _ck_c.fetchone():
                    log.info(f"A2A: rejected — task cancelled, issue=#{issue_number}, agent={agent_name}")
                    return {"ok": False, "skipped": "task_cancelled"}
        except Exception as _ck_e:
            log.warning(f"A2A: cancelled check failed: {_ck_e}")
    seq = 0

    # 鉴权：用户级身份（JWT/API Key，校验托管仓库）或内部 bot/会话 token（_authorize_a2a）
    if user and user.get("auth_type") in ("jwt", "api_key"):
        from src.user.db import get_managed_repos
        managed = {r.get("repo_key") for r in get_managed_repos(user.get("id") or 0)}
        if repo_key not in managed:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权对该仓库发起 A2A"})
    else:
        # 会话 token（repo 作用域）或 仓库 bot token（兼容存量 worker）
        _authorize_a2a(request, repo_key)

    # Agent 启用检查（仓库级 enable，见 agent-enable-toggle-design.md）
    from src.api.session_store import is_agent_enabled
    if not is_agent_enabled(repo_key, agent_name):
        log.warning(f"A2A: agent '{agent_name}' disabled for repo={repo_key}, rejected")
        return JSONResponse(status_code=403, content={"code": 403, "message": f"agent {agent_name} disabled"})

    # 场景节点校验：目标 agent 不在当前场景的 pipeline nodes 里 → 拦截（不创建事件）
    # 防止 agent skill 硬编码 A2A 目标（如 coding→qa）与自定义场景 pipeline 冲突
    if issue_number:
        try:
            from src.session_manager import db, db_lock
            from src.api.session_store import get_issue_scene
            _scene_id_check, _ = get_issue_scene(repo_key, f"#{issue_number}")
            _scene_id_check = _scene_id_check or "default"
            _scene_nodes_check = None
            # 自定义场景从 DB 查
            if _scene_id_check.startswith("custom-"):
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id=? AND is_active=1",
                              (_scene_id_check,))
                    _row = c.fetchone()
                if _row and _row[0]:
                    import json as _json
                    _scene_nodes_check = _json.loads(_row[0])
            # 官方场景从 pipelines.json 查
            if not _scene_nodes_check:
                import os as _os, json as _json
                _pipe_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))),
                                           "configs", "pipelines.json")
                with open(_pipe_path, "r", encoding="utf-8") as _f:
                    _pipes = _json.load(_f)
                _scene_def = _pipes.get("scenes", {}).get(_scene_id_check) or _pipes.get("scenes", {}).get("default") or {}
                _scene_nodes_check = _scene_def.get("nodes") or []
            # 检查目标 agent 是否在场景 nodes 里
            _agent_in_scene = any(n.get("id") == agent_name for n in _scene_nodes_check)
            if not _agent_in_scene:
                log.info(f"A2A: agent '{agent_name}' not in scene '{_scene_id_check}' for issue=#{issue_number}, skipped (nodes={[n.get('id') for n in _scene_nodes_check]})")
                return JSONResponse(status_code=200, content={"status": "ok", "seq": 0, "agent": agent_name, "skipped": "not_in_scene", "scene": _scene_id_check})
        except Exception as _se:
            log.warning(f"A2A: scene node check failed: {_se}")

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

        # Issue 级场景优先：scene 非 default 时，用 issue scene 的 skill 覆盖 session 的 skill
        # 防止场景中心切换场景后污染已选定场景的 issue
        if issue_number:
            try:
                from src.api.session_store import get_issue_scene
                from src.api.event_pipeline import is_plugin_mode, resolve_skill_from_pipeline
                _a2a_issue_scene, _a2a_issue_uid = get_issue_scene(repo_key, f"#{issue_number}")
                _a2a_issue_scene = _a2a_issue_scene or "default"
                if is_plugin_mode() and _a2a_issue_scene and _a2a_issue_scene != "default":
                    _pipe_skill = resolve_skill_from_pipeline(repo_key, agent_name, scene_id=_a2a_issue_scene, user_id=_a2a_issue_uid)
                    if _pipe_skill:
                        skill_name = _pipe_skill
                        from src.user.db import resolve_skill_id, get_skill_by_id
                        _new_sid = resolve_skill_id(_pipe_skill, repo_key=repo_key, user_id=_a2a_issue_uid)
                        if _new_sid:
                            skill_id = _new_sid
                            _sk = get_skill_by_id(_new_sid)
                            skill_path = (_sk.get("skill_path") or _pipe_skill) if _sk else _pipe_skill
                        else:
                            skill_path = _pipe_skill
                            skill_id = 0
                        log.info(f"A2A: issue scene skill override: {agent_name}={_pipe_skill} sid={skill_id} (scene={_a2a_issue_scene})")
            except Exception as _oe:
                log.warning(f"A2A: issue scene skill override failed: {_oe}")

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
        # 回退检测：A2A 目标 agent 比当前 stage 更早时，取消已派发的下游任务。
        # 不做跳步修正——agent 有权 A2A 到任意目标 agent，pipeline 顺序由 skill 逻辑保证。
        if issue_number:
            try:
                from src.api.session_store import get_issue_stage
                _stage_info = get_issue_stage(repo_key, f"#{issue_number}")
                _current_stage = _stage_info["stage"] if _stage_info else ""
                _pipeline_next = {}
                try:
                    import os as _os, json as _json
                    from src.api.session_store import get_issue_scene
                    _scene_id = get_issue_scene(repo_key, f"#{issue_number}") or "default"
                    _pipe_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))),
                                               "configs", "pipelines.json")
                    with open(_pipe_path, "r", encoding="utf-8") as _f:
                        _pipes = _json.load(_f)
                    _scene = _pipes.get("scenes", {}).get(_scene_id) or _pipes.get("scenes", {}).get("default") or {}
                    for _node in (_scene.get("nodes") or []):
                        _pipeline_next[_node.get("id", "")] = _node.get("next", "")
                except Exception:
                    pass
                _stage_order = list(_pipeline_next.keys())
                _cur_idx = _stage_order.index(_current_stage) if _current_stage in _stage_order else -1
                _target_idx = _stage_order.index(agent_name) if agent_name in _stage_order else -1

                # 回退检测 + 取消下游任务
                if _target_idx < _cur_idx and _cur_idx > 0:
                    _cancel_agents = [a for a in _stage_order[_cur_idx:] if a != "done"]
                    log.info(f"A2A: rollback detected — issue=#{issue_number} "
                             f"from={_current_stage} to={agent_name}, cancelling={_cancel_agents}")
                    try:
                        from src.session_manager import db, db_lock
                        from src.runtime.service import cancel_task
                        from src.api.event_pipeline import cancel_direct_task
                        from src.api.event_store import update_event_status

                        with db_lock:
                            c = db.cursor()
                            placeholders = ",".join("?" * len(_cancel_agents))
                            c.execute(
                                f"SELECT id, event_seq, agent_type FROM agent_tasks "
                                f"WHERE repo_key=? AND issue_id=? AND agent_type IN ({placeholders}) "
                                f"AND status IN ('queued','dispatched','running')",
                                (repo_key, f"#{issue_number}", *_cancel_agents))
                            tasks = c.fetchall()

                        _cancelled = 0
                        _errors = []
                        _sre_completed_alert = False

                        for tid, tseq, ttype in tasks:
                            try:
                                cancel_task(tid)
                                _cancelled += 1
                            except Exception as _ce:
                                _errors.append(f"cancel_task({ttype}:{tid}): {_ce}")
                            try:
                                if tseq:
                                    cancel_direct_task(tseq)
                            except Exception as _de:
                                _errors.append(f"cancel_direct_task({ttype}:{tseq}): {_de}")
                            try:
                                if tseq:
                                    update_event_status(tseq, "cancelled")
                            except Exception:
                                pass

                        # SRE 已完成检测：查 events 表（权威状态）
                        if "sre" in _cancel_agents:
                            with db_lock:
                                c2 = db.cursor()
                                c2.execute(
                                    "SELECT seq FROM events WHERE repo_key=? AND issue_id=? "
                                    "AND agent_type='sre' AND status='completed' "
                                    "ORDER BY seq DESC LIMIT 1",
                                    (repo_key, f"#{issue_number}"))
                                _sre_row = c2.fetchone()
                            if _sre_row:
                                _sre_completed_alert = True

                        if _sre_completed_alert:
                            log.warning(f"A2A: SRE already completed for issue=#{issue_number} — "
                                        f"manual review needed, published artifact may need rollback")
                        if _errors:
                            log.warning(f"A2A: cancel errors: {_errors}")
                        log.info(f"A2A: rollback cancel — {_cancelled} tasks cancelled, "
                                 f"sre_alert={_sre_completed_alert}")
                    except Exception as _re:
                        log.warning(f"A2A: rollback cancel failed: {_re} — A2A continues, cancel may not have taken effect")
            except Exception as _pe:
                log.warning(f"A2A: pipeline order check failed: {_pe}")

            try:
                from src.api.session_store import set_issue_stage
                # Issue #51: 最后节点完成后不再自动 done，改为 pending_review 等待开发者审核
                _expected_next = _pipeline_next.get(_current_stage, "") if _pipeline_next else ""
                if agent_name == "done" or (_expected_next == "done" and agent_name not in (_stage_order or [])):
                    _stage = "pending_review"
                    log.info(f"A2A: last node completed → pending_review issue=#{issue_number} (awaiting user review)")
                elif _expected_next and agent_name != _expected_next and agent_name in (_stage_order or []):
                    # 跳步保护：A2A 目标不是 pipeline next → 自动修正为 next
                    log.warning(f"A2A: skip-stage guard — issue=#{issue_number} "
                                f"current={_current_stage} expected_next={_expected_next} but got={agent_name}, "
                                f"correcting to {_expected_next}")
                    _stage = _expected_next
                    agent_name = _expected_next
                else:
                    _stage = agent_name if agent_name in ("vod", "issue", "coding", "qa", "sre", "pending_review", "done") else "issue"
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

        # 多任务模式：VOD A2A 交办时携带 is_subtask_mode → 持久化
        _is_subtask_mode = payload.get("is_subtask_mode", False)
        if _is_subtask_mode and issue_number:
            try:
                from src.api.session_store import set_subtask_mode
                set_subtask_mode(repo_key, f"#{issue_number}", True)
                log.info(f"A2A: subtask_mode enabled for issue=#{issue_number}")
            except Exception as _stm:
                log.warning(f"A2A: set_subtask_mode failed: {_stm}")

        # 子任务快速通道：A2A 携带 parent_issue → 持久化
        _parent_issue = payload.get("parent_issue", "")
        if _parent_issue and issue_number:
            try:
                from src.api.session_store import set_parent_issue
                set_parent_issue(repo_key, f"#{issue_number}", _parent_issue)
                log.info(f"A2A: parent_issue={_parent_issue} set for issue=#{issue_number}")
            except Exception as _pi:
                log.warning(f"A2A: set_parent_issue failed: {_pi}")

        # 立即创建 event（status=running），Agent 执行完再更新
        from src.api.event_store import append_event
        # 从 tasks 表获取 Issue 真实标题（A2A 标题不能用 "A2A: agent" — Pipeline 列表需要显示 Issue 标题）
        _a2a_title = ""
        if issue_number:
            try:
                from src.api.routes.tasks import _get_issue_title
                _a2a_title = _get_issue_title(repo_key, int(issue_number)) or ""
            except Exception:
                pass
        if not _a2a_title:
            _a2a_title = f"A2A: {agent_name}"
        seq = append_event(
            repo_key=repo_key,
            event_type="a2a_event",
            title=_a2a_title,
            issue_id=f"#{issue_number}" if issue_number else "#0",
            source="a2a",
            agent_type=agent_name,
            actor="",
            summary=f"A2A: → {agent_name}",
            payload={"repository": repository, "account": account, "issue_number": issue_number, "pr_number": pr_number, "context": context[:500], "is_subtask_mode": _is_subtask_mode, "parent_issue": _parent_issue},
            status="running",
        )
        if not seq:
            log.error(f"A2A: append_event returned 0, cannot create event for agent={agent_name}, issue=#{issue_number}")
            return JSONResponse(status_code=500, content={"code": 500, "message": "event store failed, retry A2A"})
        log.info(f"A2A: event created, seq={seq}")

        # Plugin mode: emit agent/handoff event → event bus → pipeline_executor dispatches
        # (event-dispatch-redesign-v2.md Phase 4: A2A upgraded to event-driven)
        # 但强制 direct 模式不走 event bus，走 legacy direct dispatch
        try:
            from src.api.event_pipeline import is_plugin_mode
            from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE
            if is_plugin_mode() and get_execution_mode() != EXEC_MODE_DIRECT:
                import asyncio as _aio
                from src.api.event_bus import get_event_bus, GrapeEvent, EventDomain
                from src.api.pipeline_config import get_pipeline_config, get_node_by_tag

                # Determine scene and node config — 用 Issue 级场景，不是仓库级
                scene_id = "default"
                scene_user_id = 0
                try:
                    from src.api.session_store import get_issue_scene
                    scene_id, scene_user_id = get_issue_scene(repo_key, f"#{issue_number}") if issue_number else ("default", 0)
                    scene_id = scene_id or "default"
                except Exception:
                    pass

                node = get_node_by_tag(scene_id, agent_name, repo_key=repo_key, user_id=scene_user_id)
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
                        "scene_user_id": scene_user_id,
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
        from src.runtime.engine_select import pick_engine, engine_name, get_scene_pool_engine_ids
        from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
        # A2A 交办的场景来自 issue_state（智能推荐完成或用户手动选择已持久化）
        _a2a_scene, _a2a_scene_uid = "default", 0
        if issue_number:
            try:
                from src.api.session_store import get_issue_scene
                _a2a_scene, _a2a_scene_uid = get_issue_scene(repo_key, f"#{issue_number}")
                _a2a_scene = _a2a_scene or "default"
            except Exception:
                pass
        engine_id = pick_engine(repo_key, agent_name, fallback_runtime_id=runtime_id or 0,
                                scene_id=_a2a_scene, user_id=_a2a_scene_uid)
        if engine_id is None:
            log.info(f"A2A: no available engine for repo={repo_key} agent={agent_name}, seq={seq}, skip")
            try:
                from src.api.event_store import update_event_status
                update_event_status(seq, "failed")
            except Exception:
                pass
            return {"status": "ok", "seq": 0, "agent": agent_name}
        # Plugin 模式 + engine_id=0：仅 queue_remote 走广播；auto/direct 走本机 direct
        from src.api.event_pipeline import is_plugin_mode as _is_plugin
        from src.runtime.service import get_execution_mode, EXEC_MODE_DIRECT, EXEC_MODE_REMOTE
        if engine_id == 0 and _is_plugin() and get_execution_mode() != EXEC_MODE_DIRECT:
            from src.api.event_pipeline import _enqueue_pipeline_task
            _be = backend or "opencode"
            _pool_eids = get_scene_pool_engine_ids(_a2a_scene, agent_name, repo_key=repo_key, user_id=_a2a_scene_uid)
            _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                                   skill_name or agent_name, evt,
                                   skill_id, skill_path or "", engine_id,
                                   f"#{issue_number}" if issue_number else "",
                                   pool_engine_ids=_pool_eids, user_id=_a2a_scene_uid)
            log.info(f"A2A (plugin broadcast): enqueued pool task for {agent_name}, seq={seq}")
            return {"status": "ok", "seq": seq, "agent": agent_name, "mode": "broadcast"}
        # backend 取已注册引擎的类型名（不沿用字面默认，engine-selection-design.md v2）
        _be = engine_name(engine_id) or backend
        # engine_id=0：auto/direct 兜底 → 自动选本机在线引擎
        if not engine_id:
            try:
                from src.session_manager import db as _edb, db_lock as _edl
                with _edl:
                    _ec = _edb.cursor()
                    _ec.execute("SELECT id, name FROM agent_runtimes WHERE status='online' AND healthy=1 ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END LIMIT 1")
                    _erow = _ec.fetchone()
                if _erow:
                    engine_id = _erow[0]
                    _be = _erow[1] or _be
                    log.info(f"A2A: auto-selected local engine id={engine_id} name={_be}, seq={seq}")
                else:
                    log.info(f"A2A: engine_id=0 no online engine for {agent_name}, seq={seq}, skip")
                    try:
                        from src.api.event_store import update_event_status
                        update_event_status(seq, "failed")
                    except Exception:
                        pass
                    return {"status": "ok", "seq": 0, "agent": agent_name}
            except Exception as _ae:
                log.warning(f"A2A: auto-select engine failed: {_ae}")
                try:
                    from src.api.event_store import update_event_status
                    update_event_status(seq, "failed")
                except Exception:
                    pass
                return {"status": "ok", "seq": 0, "agent": agent_name}
        kind = runtime_execution_kind(engine_id)
        if kind == EXEC_MODE_DIRECT:
            from src.api.event_pipeline import start_direct_agent, _create_direct_task
            _issue_id = f"#{issue_number}" if issue_number else ""
            evt["_skill_id"] = skill_id
            _a2a_task_id = _create_direct_task(repo_key, seq, agent_name, _be,
                                skill_name or agent_name, skill_path or "",
                                engine_id, evt, _issue_id, skill_id=skill_id)
            start_direct_agent(_execute_agent, (
                repo_key, seq, agent_name, _be, skill_name or agent_name, evt, engine_id, _a2a_task_id,
            ), name=f"a2a-{agent_name}-{seq}")
            log.info(f"A2A: started agent execution for {agent_name} (direct, engine={engine_id})")
        else:
            from src.api.event_pipeline import _enqueue_pipeline_task
            _enqueue_pipeline_task(repo_key, seq, agent_name, _be,
                                   skill_name or agent_name, evt,
                                   skill_id, skill_path, engine_id,
                                   f"#{issue_number}" if issue_number else "",
                                   user_id=_a2a_scene_uid)
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
