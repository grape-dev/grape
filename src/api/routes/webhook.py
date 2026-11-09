"""Webhook & A2A 端点 — 集成事件管道"""
import json, os, time
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from src.config import log
from src.api.deps import get_user_or_none
from src.api.event_pipeline import run_pipeline, resolve_actor_account
from src.api.a2a_dispatch import _on_handoff_dispatch, run_a2a_dispatch

router = APIRouter(tags=["webhook"])


# Issue #340：仓库删除 webhook 事件名（GitCode/GitHub，大小写不敏感匹配）
_REPO_DELETE_EVENT_HEADERS = {
    "repo.destroyed", "repository.destroyed", "repo.deleted", "repository.deleted",
    "repository destroyed", "repository deleted", "project.destroyed",
}
_REPO_DELETE_ACTION_EVENTS = {"delete", "deleted", "destroyed"}


def _is_repo_delete_event(event_header: str, payload: dict) -> bool:
    """识别 webhook 事件是否为仓库删除（Issue #340 触发路径二）。

    - GitCode：X-Gitcode-Event 头（如 repo.destroyed）或 body action；
    - GitHub：X-GitHub-Event=repository 且 action=deleted；
    - 兼容部分平台把 action 放在 body 顶层或 event 字段。
    """
    header = (event_header or "").strip().lower()
    if header in _REPO_DELETE_EVENT_HEADERS:
        return True
    # 头名含 repo/repository 且动词为删除/销毁（宽松匹配，防平台命名差异）
    if ("repo" in header or "project" in header) and (
            "delet" in header or "destroy" in header or "remove" in header):
        return True
    action = ""
    if isinstance(payload, dict):
        action = str(payload.get("action", "") or payload.get("event", "") or "").strip().lower()
    if header in ("repository", "repo", "project") and action in _REPO_DELETE_ACTION_EVENTS:
        return True
    return False


def _handle_repo_deleted(repo: str, provider: str, trigger_detail: str) -> dict:
    """仓库删除事件处理：级联清空平台内全部关联数据（Issue #340）。

    与平台侧"解除托管"等价：cascade_cleanup_repo_data + 移除 managed_repos。
    """
    from src.repo_key import to_repo_key
    from src.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
    from src.user import db as user_db
    from src.session_manager import db as _db, db_lock

    repo_key = to_repo_key(repo, provider=provider)
    owner, _, repo_name = repo.partition("/")

    # 读取托管记录（清理前取 owner/repo 名，删除后即失效）
    _owner, _repo_name = owner, repo_name
    try:
        with db_lock:
            c = _db.cursor()
            c.execute("SELECT repo_owner, repo_name FROM managed_repos WHERE repo_key=?",
                      (repo_key,))
            row = c.fetchone()
            if row and row[0] and row[1]:
                _owner, _repo_name = row[0], row[1]
    except Exception:
        pass

    try:
        stats = cascade_cleanup_repo_data(repo_key, _owner, _repo_name,
                                          trigger="webhook")
        log.info(f"Webhook repo-deleted: cascade cleanup {repo_key} done ({trigger_detail}): {stats}")
    except RepoCleanupError as e:
        # 事务已回滚；保留 managed 记录，探活轮询兜底重试
        log.warning(f"Webhook repo-deleted: cascade cleanup {repo_key} failed (rolled back, "
                    f"polling will retry): {e}")
        return {"status": "cleanup_failed_retryable", "message": str(e)}

    try:
        user_db.remove_managed_repo(repo_key)
    except Exception as e:
        log.warning(f"Webhook repo-deleted: remove managed_repos {repo_key} failed: {e}")
    return {"status": "repo_deleted", "message": "repository deleted, cascade cleanup done"}


from src.api.a2a_context import a2a_context_key as _a2a_context_key
from src.api.a2a_context import a2a_event_context_key as _a2a_event_context_key


def _has_active_a2a_event(repo_key: str, issue_id: str, agent_type: str,
                          scene_id: str | None = None, context_key: str = "") -> int:
    """Issue #587：A2A 去重判定键 = 「事件活跃(running/dispatched)」。

    仅同 issue + 同 agent_type 且事件仍处于活跃态才视为重复（返回该事件 seq）；
    事件已终态（completed/failed/cancelled）不阻断新交接（返回 0）。
    scene_id 提供时限定跨流水线目标场景。

    Issue #1667：额外引入 context_key（context/idempotency_key 维度）——
    - 未提供 context_key（旧调用方）→ 保持 #587 原语义，任何活跃事件都算重复；
    - 提供 context_key → 仅活跃事件上下文键相同（或历史行无上下文键）才判重复，
      不同上下文（不同 idempotency_key/context）放行，不再静默丢弃。
    """
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            if scene_id:
                c.execute(
                    "SELECT seq FROM events WHERE repo_key=? AND issue_id=? "
                    "AND event_type='a2a_event' AND status IN ('running','dispatched') "
                    "AND scene_id=? ORDER BY seq DESC LIMIT 1",
                    (repo_key, issue_id, scene_id))
                row = c.fetchone()
                return (row[0] if row else 0)
            c.execute(
                "SELECT seq, idempotency_key, payload FROM events "
                "WHERE repo_key=? AND issue_id=? AND agent_type=? "
                "AND event_type='a2a_event' AND status IN ('running','dispatched') "
                "ORDER BY seq DESC LIMIT 50",
                (repo_key, issue_id, agent_type))
            rows = c.fetchall() or []
        if not context_key:
            return (rows[0][0] if rows else 0)
        for _seq, _ik, _payload in rows:
            _row_key = _a2a_event_context_key(_ik or "", _payload or "")
            if not _row_key or _row_key == context_key:
                # 历史活跃事件无上下文键 → 无法区分，按旧语义保守判重复
                return _seq
        return 0
    except Exception as _de:
        log.warning(f"A2A: active-event dedup check failed: {_de}")
        return 0


def _queued_a2a_events(repo_key: str, issue_id: str, agent_type: str) -> list:
    """Issue #1667：列出同 issue+agent 的排队 A2A 事件（status='queued'）。

    返回 [{"seq", "payload", "queue": {...}}]，供 queue 幂等复用与 force 取代使用。
    """
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, payload FROM events WHERE repo_key=? AND issue_id=? "
                "AND agent_type=? AND event_type='a2a_event' AND status='queued' "
                "ORDER BY seq ASC",
                (repo_key, issue_id, agent_type))
            rows = c.fetchall() or []
    except Exception as _qe:
        log.warning(f"A2A: queued event lookup failed: {_qe}")
        return []
    out = []
    for _seq, _raw in rows:
        _p = {}
        try:
            _p = json.loads(_raw or "{}")
        except Exception:
            _p = {}
        if not isinstance(_p, dict):
            _p = {}
        out.append({"seq": _seq, "payload": _p, "queue": _p.get("_a2a_queue") or {}})
    return out


def _cancel_superseded_queued_a2a(repo_key: str, issue_id: str, agent_type: str,
                                  context_key: str, reason: str) -> list:
    """Issue #1667：force 下发后取消同上下文的旧排队记录，避免终态后重复派发。"""
    cancelled = []
    if not context_key:
        return cancelled
    for _item in _queued_a2a_events(repo_key, issue_id, agent_type):
        _p = _item["payload"]
        _row_key = _a2a_context_key(
            str(_p.get("idempotency_key") or ""), str(_p.get("context") or ""))
        if _row_key != context_key:
            continue
        try:
            from src.api.event_store import update_event_complete
            update_event_complete(_item["seq"], "cancelled",
                                  result=f"superseded by {reason}", exit_code=None)
            cancelled.append(_item["seq"])
            log.info(f"A2A: queued event cancelled (superseded by {reason}) seq={_item['seq']}")
        except Exception as _ce:
            log.warning(f"A2A: cancel superseded queued event failed seq={_item['seq']}: {_ce}")
    return cancelled


def _duplicate_a2a_original_refs(repo_key: str, existing_seq: int) -> dict:
    """Issue #729：duplicate_a2a 拦截时回查原事件的任务产物引用。

    返回 original_event 结构（seq / agent / status / issue_id / created_at +
    task_ref / result_ref），随拦截响应透传给调用方，下游可凭引用定位并续接
    原任务产物（task_id + query_path 轮询 / result 摘要）：
    - 原任务在途（queued/dispatched/running）→ result_ref.status=pending，
      仅 task_ref 可用于轮询（Spec：result pending 边界）；
    - 无关联任务 / 查询失败 → unavailable 显式标注，不阻断拦截响应本身。
    全部为新增可选字段，向后兼容。
    """
    from src.api.pipeline_status import (
        _UNAVAILABLE_RESULT_REF,
        _UNAVAILABLE_TASK_REF,
        load_segment_task_refs,
    )
    evt = {"seq": existing_seq, "agent": "", "status": "",
           "issue_id": "", "created_at": ""}
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT agent_type, status, issue_id, created_at FROM events "
                "WHERE repo_key=? AND seq=? LIMIT 1",
                (repo_key, existing_seq))
            row = c.fetchone()
        if row:
            evt = {"seq": existing_seq, "agent": row[0] or "",
                   "status": row[1] or "", "issue_id": row[2] or "",
                   "created_at": row[3] or ""}
    except Exception as _re:
        log.warning(f"A2A #729: load original event failed seq={existing_seq}: {_re}")
    pair = (load_segment_task_refs([repo_key], [existing_seq]) or {}).get(existing_seq)
    if pair:
        evt["task_ref"] = pair[0]
        evt["result_ref"] = pair[1]
    else:
        evt["task_ref"] = dict(_UNAVAILABLE_TASK_REF)
        evt["result_ref"] = dict(_UNAVAILABLE_RESULT_REF)
    return evt


def _duplicate_a2a_skip_response(agent_name: str, repo_key: str, existing_seq: int,
                                 reason: str = "active_a2a_event"):
    """Issue #729：同 issue 同 agent 重复 A2A 的拦截响应体（含原任务产物引用）。

    Issue #1667：补充 queued=False / reason，明确与「排队」响应区分；
    existing_seq 恒指向被去重/排队的那个前序活跃事件 seq。
    """
    return JSONResponse(status_code=200, content={
        "status": "ok", "seq": 0, "agent": agent_name,
        "skipped": "duplicate_a2a", "queued": False,
        "reason": reason, "existing_seq": existing_seq,
        "message": "same issue+agent already has an active a2a event (existing_seq); "
                   "retry with queue=true to queue it, or force=true to dispatch immediately",
        "original_event": _duplicate_a2a_original_refs(repo_key, existing_seq),
    })


def _a2a_event_payload(seq, status, agent, issue_id, created_at=None) -> dict:
    """Issue #1691：统一 A2A 事件字段口径（seq/status/agent/issue_id[/created_at]），
    避免幂等、排队与并发回查等响应的 event 字面量再次漂移。"""
    payload = {
        "seq": seq,
        "status": status,
        "agent": agent,
        "issue_id": issue_id,
    }
    if created_at is not None:
        payload["created_at"] = created_at
    return payload


def _augment_a2a_dispatch_response(result, agent_name: str, issue_number) -> dict:
    """Issue #1691：成功派发响应补 `event`（seq/status/agent/issue_id）。

    与幂等/排队响应口径一致，CLI 可直接读取下游派发状态，无需再猜 seq=0/ok 的语义。
    门槛以派发结果自身的 `seq>0` 为准：`run_a2a_dispatch` 在 auto-select 无引擎 /
    异常时会先 `_fail_event(seq)` 把已建事件置 failed，再返回 `{"status":"ok","seq":0}`，
    此时不得补 `event.status=running`，须保持与事件库真实状态一致。
    """
    if isinstance(result, dict) and result.get("status") == "ok":
        dispatch_seq = result.get("seq")
        if isinstance(dispatch_seq, int) and dispatch_seq > 0:
            result.setdefault("event", _a2a_event_payload(
                dispatch_seq, "running", agent_name,
                f"#{issue_number}" if issue_number else ""))
    return result


def _a2a_source_event_seq(session_info) -> int:
    """取 A2A 调用方（源事件）seq：会话 token 的 session_key=调用方任务 id → agent_tasks.event_seq。

    Issue #476：A2A 失败诊断需要"引用 seq"（触发本次交办的源事件 seq）。
    无 session / 无任务关联时返回 0（不阻断诊断写入）。
    """
    try:
        if session_info and session_info.get("session_key"):
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT event_seq FROM agent_tasks WHERE id=? LIMIT 1",
                          (session_info["session_key"],))
                row = c.fetchone()
            if row and row[0]:
                return int(row[0])
    except Exception as e:
        log.warning(f"A2A: resolve source event seq failed: {e}")
    return 0


# Issue #514：非 POST 方法显式拒绝（405），防止落入 SPA catch-all 兜底返回 200 HTML，
# 误导健康探针/监控把 webhook 入口误判为成功。仅 POST 是合法的 webhook 入口方法。
_WEBHOOK_REJECTED_METHODS = ["GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]


@router.api_route("/webhook/gitcode", methods=_WEBHOOK_REJECTED_METHODS,
                  include_in_schema=False, name="gitcode_webhook_method_reject")
async def gitcode_webhook_method_reject():
    """非 POST 方法访问 /webhook/gitcode → 405 Method Not Allowed（Issue #514）。"""
    return JSONResponse(
        status_code=405,
        content={"code": 405, "message": "method not allowed: webhook endpoint accepts POST only"},
    )


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
             f"event={event_header}, body_size={len(body)}, token={'present' if token else 'absent'}")

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

    # 4. token 对比验证（Issue #860：hmac.compare_digest 恒定时间比较，防时序侧信道）
    import hmac
    if not hmac.compare_digest(str(token), str(expected)):
        log.warning(f"Webhook rejected: token mismatch for repo={repo}")
        return JSONResponse(status_code=403, content={"code": 403, "message": "invalid webhook token"})

    log.info(f"Webhook: token OK, repo={repo}, event={event_header}")

    # Issue #340 触发路径二：仓库删除事件 → 级联清空平台内全部关联数据
    if _is_repo_delete_event(event_header, payload):
        result = _handle_repo_deleted(repo, "gitcode", f"event={event_header}")
        status_code = 200 if result.get("status") == "repo_deleted" else 500
        return JSONResponse(status_code=status_code, content=result)

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

    # GitHub webhook 验证（安全加固）：至少提供一种凭据且校验通过才放行。
    # - X-Hub-Signature-256 存在 → 校验 HMAC-SHA256（失败 403）
    # - 否则 X-GitHub-Token 必须存在且与仓库密钥一致（失败 403）
    # - 两者皆缺 → 401 拒绝，绝不进入流水线调度
    import hashlib, hmac
    signature = headers.get("X-Hub-Signature-256", "")
    token_header = headers.get("X-GitHub-Token", "")
    if not signature and not token_header:
        log.warning(f"GitHub Webhook rejected: missing signature & token for repo={repo}")
        return JSONResponse(status_code=401, content={"code": 401, "message": "missing signature or token"})
    if signature:
        computed = hmac.new(expected.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(f"sha256={computed}", signature):
            log.warning(f"GitHub Webhook rejected: signature mismatch for repo={repo}, body_size={len(body)}")
            return JSONResponse(status_code=403, content={"code": 403, "message": "invalid signature"})
    else:
        if not hmac.compare_digest(str(token_header), str(expected)):
            log.warning(f"GitHub Webhook rejected: token mismatch for repo={repo}")
            return JSONResponse(status_code=403, content={"code": 403, "message": "invalid token"})

    log.info(f"GitHub Webhook: verified, repo={repo}, event={event_header}")

    # Issue #340 触发路径二：仓库删除事件（X-GitHub-Event=repository + action=deleted）
    try:
        _gh_payload = json.loads(body)
    except Exception:
        _gh_payload = {}
    if _is_repo_delete_event(event_header, _gh_payload):
        result = _handle_repo_deleted(repo, "github", f"event={event_header}")
        status_code = 200 if result.get("status") == "repo_deleted" else 500
        return JSONResponse(status_code=status_code, content=result)

    import asyncio as _aio
    _aio.ensure_future(_aio.to_thread(run_pipeline, body, dict(headers),
                                     account=repo, repo_key_override=f"github:{repo}"))
    log.info(f"GitHub Webhook: pipeline scheduled async for repo={repo}, event={event_header}")
    return {"status": "ok", "seq": 0, "event_type": event_header, "message": "pipeline scheduled"}


def _resolve_a2a_repo_key(repository: str) -> str:
    """A2A 端点 repo_key 统一解析（Issue #544 审查修正②）。

    - 已带 provider 前缀（github:/gitcode:）→ 幂等返回；
    - 无前缀 → 查 managed_repos 反查实际 provider（github: / gitcode:）；
    - 未命中/查表异常 → 默认 gitcode。
    POST /api/a2a 与 GET /api/a2a/status 共用，保证两端点指纹/幂等键的
    repo_key 维度一致（否则 github 托管仓无前缀传入时核对接口假阴性）。
    """
    from src.repo_key import to_repo_key
    if not repository:
        return ""
    if ":" in repository:
        return to_repo_key(repository)
    try:
        from src.session_manager import db as _pdb, db_lock as _pdb_lock
        with _pdb_lock:
            _pc = _pdb.cursor()
            _pc.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?)",
                        (f"github:{repository}", f"gitcode:{repository}"))
            _pr = _pc.fetchone()
        if _pr:
            return _pr[0]
    except Exception:
        pass
    return to_repo_key(repository)


def _authorize_a2a(request: Request, repo_key: str) -> dict | None:
    """A2A 端点仓库级鉴权：bot token 或会话级临时 token（repo 作用域）二选一通过。

    - bot token：仓库级（managed_repos / devops_agent_session），支持远端 worker；
    - 会话 token（GRAPE_SESSION_TOKEN，M2）：校验 repo_key 匹配——该仓库执行期 agent 凭据。
    仅当仓库已托管（有 bot token）才严格校验 bot；未托管仓库允许会话 token 或交由后续校验。

    返回 session token info（含 session_key），供调用方反查调用方身份；
    bot token 通道返回 None（无身份信息）。

    关联仓库信任域（Issue #15 修复）：会话 token 绑定的仓库与目标仓库若属于同一 Issue
    场景的关联仓库集合（GRAPE_LINKED_REPOS），或两仓 bot token 相同（同一运维主体），
    则放行——打通 QA→coding(关联仓库) 的审查-修复闭环。信任域放行走 bot 通道语义
    （返回 None，调用方无法反查身份，A2A 拦截对其放行）。
    """
    _auth = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not _auth:
        _auth = request.headers.get("X-Bot-Token", "") or ""
    if not repo_key:
        log.warning("A2A: missing repository, reject")
        raise HTTPException(status_code=400, detail={"code": 400, "message": "missing repository in payload"})
    from src.user.db import get_repo_bot_token_no_global
    from src.api.auth_token import verify_session_token, verify_session_token_for_repo
    _expected = get_repo_bot_token_no_global(repo_key) or ""
    _bot_ok = bool(_expected) and _auth == _expected
    # 严格匹配：会话 token 绑定仓库 == 目标仓库 → 返回 info（可反查调用方身份）
    _session_info = verify_session_token_for_repo(_auth, repo_key)
    _session_ok = bool(_session_info)
    if _bot_ok or _session_ok:
        log.info(f"A2A: token OK (bot={_bot_ok}, session={_session_ok}) repo={repo_key}")
        return _session_info if _session_ok else None
    # 关联仓库信任域：会话 token 的仓库与目标仓库属于同一场景关联集合 → 放行
    _raw_session_info = verify_session_token(_auth)
    if _raw_session_info and _raw_session_info.get("repo_key"):
        _session_repo = _raw_session_info["repo_key"]
        if _session_repo != repo_key:
            # 检查 1: 两仓 bot token 相同（同一运维主体）
            _session_bot_token = get_repo_bot_token_no_global(_session_repo) or ""
            if _session_bot_token and _session_bot_token == _expected:
                log.info(f"A2A: trust domain (same bot token) session_repo={_session_repo} → target_repo={repo_key}")
                return None
            # Issue #372: 检查 1b: 两仓 host_user_id 相同（路径 B 用户 token 托管场景）
            try:
                from src.user.db import get_host_user_id
                _session_host = get_host_user_id(_session_repo)
                _target_host = get_host_user_id(repo_key)
                if _session_host > 0 and _session_host == _target_host:
                    log.info(f"A2A: trust domain (same host_user_id={_session_host}) session_repo={_session_repo} → target_repo={repo_key}")
                    return None
            except Exception:
                pass
            # 检查 2: 目标仓库是会话仓库的关联仓库（查场景 linked_repos）
            try:
                from src.session_manager import db, db_lock
                import json as _json
                with db_lock:
                    c = db.cursor()
                    # 查会话仓库的所有场景的 linked_repos
                    c.execute(
                        "SELECT linked_repos, advanced_options FROM devops_pipeline_scene "
                        "WHERE repo_key=? AND is_active=1", (_session_repo,))
                    for row in c.fetchall():
                        _lr = row[0] or ""
                        _adv = row[1] or ""
                        for _lr_str in (_lr, _json.loads(_adv).get("linked_repos", "[]") if _adv else "[]"):
                            if not _lr_str:
                                continue
                            try:
                                _lr_list = _json.loads(_lr_str) if isinstance(_lr_str, str) else _lr_str
                            except Exception:
                                continue
                            for _lr_item in (_lr_list or []):
                                if isinstance(_lr_item, dict) and _lr_item.get("repo_key") == repo_key:
                                    log.info(f"A2A: trust domain (linked repo) session_repo={_session_repo} → target_repo={repo_key}")
                                    return None
            except Exception as _le:
                log.warning(f"A2A: linked repo trust check failed: {_le}")
    if not _expected:
        log.warning(f"A2A: repo not managed (no bot token) & no matching session token, defer to downstream: {repo_key}")
        return None
    log.warning(f"A2A: unauthorized (token mismatch) for repo={repo_key}")
    raise HTTPException(status_code=403, detail={"code": 403, "message": "invalid token"})


class A2AHandoffBody(BaseModel):
    agent: str = ""
    payload: dict = {}
    # Issue #544 L2：显式幂等键（客户端由来源 task_id + 目标角色派生）。
    # 同仓同键的重复请求返回已有事件，不创建新事件；缺省 payload.idempotency_key 兜底。
    idempotency_key: str = ""
    # Issue #1667：命中同 issue+agent 活跃事件时的处置方式。
    # queue=true → 排队，前序事件终态后自动派发；force=true → 立即新建事件（跳过活跃去重）。
    # 两者均可在 payload 中兜底传入；默认保持 #587 duplicate 语义。
    queue: bool = False
    force: bool = False


def _scene_node_check(repo_key: str, issue_number, agent_name: str) -> dict:
    """目标 agent 是否在当前 Issue 场景的 pipeline nodes 内（nodes=单一事实源）。

    Issue #568: 节点来源统一走 `get_pipeline_config`（与 `scenes detail` 同源：
    DB 项目场景行 → 市场官方模板 → pipelines.json → default，含进程级缓存），
    保证「同一 token + 同一 scene_id」在 scenes detail 与 a2a handoff 两路
    返回一致节点集合（修复 404 vs 409 nodes 口径分歧）。
    解析优先级：issue 绑定场景（get_issue_scene，与 GRAPE_SCENE_ID 注入同源）
    → 空时回退 default。
    返回 {"scene_id", "nodes", "in_scene"}。
    """
    from src.api.pipeline_config import get_pipeline_config
    from src.api.session_store import get_issue_scene
    _scene_id = "default"
    try:
        _sid, _ = get_issue_scene(repo_key, f"#{issue_number}")
        _scene_id = _sid or "default"
    except Exception:
        _scene_id = "default"
    try:
        _cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
        nodes = (_cfg or {}).get("nodes") or []
    except Exception:
        nodes = []
    return {
        "scene_id": _scene_id,
        "nodes": nodes,
        "in_scene": any(n.get("id") == agent_name for n in nodes),
    }


def _refresh_issue_scene_after_handoff(repo_key: str, issue_number: str, target_scene_id: str) -> str:
    """Issue #568: 跨流水线移交后刷新 issue 绑定场景为目标场景有效 ID。

    校验目标场景在 `devops_pipeline_scene` 存在（is_active=1），命中时将 issue
    绑定场景刷新为 DB 行 scene_id（有效 ID 归一）；后续 agent 任务经
    `_resolve_scene_id` → `get_issue_scene` 注入的 GRAPE_SCENE_ID 即为该有效 ID，
    且可通过 `scenes detail` 查询且节点有效。

    返回刷新后的有效 scene_id；目标场景 DB 行不存在时返回空串（软失败——
    移交主流程已通过 get_pipeline_config 兜底口径校验节点，这里仅收紧
    DB 作用域的绑定刷新，不阻断移交）。
    """
    from src.config import log
    effective_scene_id = ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (target_scene_id, repo_key))
            row = c.fetchone()
        if row and row[0]:
            effective_scene_id = row[0]
    except Exception as _e:
        log.warning(f"A2A #568: target scene existence check failed: {_e}")
        return ""
    if not effective_scene_id:
        log.info(f"A2A #568: target scene '{target_scene_id}' has no active DB row under repo={repo_key}, skip scene refresh")
        return ""
    try:
        from src.api.session_store import set_issue_scene
        set_issue_scene(repo_key, f"#{issue_number}", effective_scene_id)
        log.info(f"A2A #568: issue scene refreshed to effective scene={effective_scene_id} (issue=#{issue_number})")
    except Exception as _e:
        log.warning(f"A2A #568: issue scene refresh failed: {_e}")
    return effective_scene_id


def _cross_pipeline_handoff(
    repo_key: str, issue_number: str, target_scene_id: str, target_node: str,
    carried_context: dict, target_assignee: str, engine_config, context: str,
    repository: str, account: str,
) -> dict:
    """Issue #329: Cross-pipeline handoff — redirect A2A to target scene's vod node.

    This function:
    1. Validates that the target scene exists and contains the target_node (vod).
    2. Checks for duplicate handoff (non-terminal event chain for same Issue in target scene).
    3. Persists carried_context + target_assignee + engine_config on the issue_state.
    4. Creates a cross_scene_handoff event for audit trail.
    5. If target_assignee is provided, records assignee_changed event (old_assignee captured in step 3).
    6. Dispatches an A2A event to the target scene's target_node.
    7. The target_node (vod) receives the carried_context via the a2a context field and proceeds.

    Returns {"ok": True, "response": <response dict>} on success,
    {"ok": False, "error": "<skip reason>"} on interception.
    """
    from src.api.pipeline_config import get_pipeline_config
    from src.config import log

    # 1. Validate target scene exists and contains the target_node
    target_cfg = get_pipeline_config(target_scene_id, repo_key=repo_key)
    if not target_cfg:
        # Issue #568: 目标场景不存在（DB/市场/官方兜底均未命中）→ 显式拦截，
        # 防止移交后注入 GRAPE_SCENE_ID 指向失效场景
        log.info(f"A2A #568: target scene '{target_scene_id}' not found under repo={repo_key}, handoff rejected")
        return {"ok": False, "error": "target_scene_not_found"}
    _target_scene_name = target_cfg.get("display_name", target_scene_id) if target_cfg else target_scene_id
    target_nodes = (target_cfg or {}).get("nodes") or []
    target_node_ids = [n.get("id") or n.get("tag") for n in target_nodes]
    if target_node not in target_node_ids:
        log.info(f"A2A #329: target scene '{target_scene_id}' does not contain node '{target_node}', nodes={target_node_ids}")
        return {"ok": False, "error": "target_scene_missing_node"}

    # 1.5 Issue #755 方案A：跨仓单号回填——转交 payload 沿用源仓单号时（目标仓 VCS 404），
    #     解析真实落地单号（carried_context.target_issue_number / DB 反查 source_issue_number）
    #     并替换 issue_number；后续去重检查 / issue_state / 事件 / 派发 payload 全部使用真实单号，
    #     下游任务注入的 ISSUE_NUMBER 即命中目标仓真实 Issue。未命中不改写（行为不变）。
    try:
        _cc_pre = carried_context if isinstance(carried_context, dict) else {}
        _resolved_num, _resolved_via = _resolve_cross_repo_issue_number(repo_key, issue_number, _cc_pre)
        if _resolved_via in ("target_field", "db_mapping") and _resolved_num != str(issue_number).strip().lstrip("#"):
            log.info(f"A2A #755: cross-pipeline issue_number backfilled {issue_number} -> {_resolved_num} "
                     f"(via {_resolved_via}, repo={repo_key})")
            issue_number = _resolved_num
            if isinstance(carried_context, dict):
                carried_context["target_issue_number"] = int(_resolved_num)
    except Exception as _755_e:
        log.warning(f"A2A #755: cross-pipeline issue_number backfill failed, keep original: {_755_e}")

    # 2. Duplicate handoff check: same Issue already has a non-terminal event in target scene
    #    Issue #587：去重判定键 = 事件活跃(running/dispatched)；已终态事件不阻断新交接
    _existing_seq = _has_active_a2a_event(repo_key, f"#{issue_number}", "", scene_id=target_scene_id)
    if _existing_seq:
        log.info(f"A2A #329: duplicate handoff skip — issue=#{issue_number} already has running event in scene={target_scene_id} seq={_existing_seq}")
        # Issue #729：拦截时携带原事件的任务产物引用（task_ref/result_ref），
        # 跨流水线下游凭引用可定位并续接原任务产物
        return {
            "ok": False, "error": "duplicate_handoff",
            "original_event": _duplicate_a2a_original_refs(repo_key, _existing_seq),
        }

    # 3. Persist carried_context + engine_config on issue_state
    #    Also capture old_assignee BEFORE the UPDATE so the audit trail in step 5
    #    records the original owner rather than an empty string.
    _old_assignee = ""
    try:
        from src.api.session_store import set_issue_scene, set_issue_stage
        from src.session_manager import db, db_lock
        _issue_key = f"#{issue_number}"
        _issue_num = int(issue_number)
        # Read current assignee BEFORE writing (audit trail needs the old value)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT assignee FROM issue WHERE repo_key=? AND issue_number=?",
                (repo_key, _issue_num))
            _row = c.fetchone()
        if _row:
            _old_assignee = _row[0] or ""
        # Issue #568: 刷新 issue 绑定场景为目标场景有效 ID（校验 DB 行存在 + scene_id 归一），
        # 后续注入 GRAPE_SCENE_ID 即有效且可通过 scenes detail 查询；DB 行缺失时回落原值
        _effective_sid = _refresh_issue_scene_after_handoff(repo_key, issue_number, target_scene_id)
        if not _effective_sid:
            set_issue_scene(repo_key, _issue_key, target_scene_id)
        # Update stage to target_node (e.g. vod) to reflect cross-pipeline continuation.
        # owner_agent uses target_node as the responsible agent tag (matches event_pipeline.py pattern).
        set_issue_stage(repo_key, _issue_key, target_node, target_node,
                        assignee=target_assignee)
        log.info(f"A2A #329: issue_state updated scene={target_scene_id} stage={target_node} old_assignee={_old_assignee}")
    except Exception as _se:
        log.warning(f"A2A #329: issue_state update failed: {_se}")

    # 4. Persist carried_context metadata (source_issue_number, spec_ref, etc.)
    _cc = carried_context if isinstance(carried_context, dict) else {}
    # Issue #755：target_issue_number 与 source_issue_number 配对持久化——
    # 后续 A2A 入口按该字段做源仓单号 → 真实落地单号映射兜底（方案B）。
    try:
        _tgt_num_755 = str(issue_number).strip().lstrip("#")
        if _tgt_num_755.isdigit():
            _cc["target_issue_number"] = int(_tgt_num_755)
    except Exception:
        pass
    if _cc:
        try:
            from src.session_manager import db as _sdb, db_lock as _sdl
            _cc_json = json.dumps(_cc)
            with _sdl:
                _sc = _sdb.cursor()
                _sc.execute(
                    "UPDATE issue SET carried_context=? WHERE repo_key=? AND issue_number=?",
                    (_cc_json, repo_key, int(issue_number)))
                _sdb.commit()
        except Exception:
            # carried_context column may not exist yet — store in issue_state metadata
            log.info("A2A #329: carried_context column not available, skipping persist")

    # 5. Assignee change audit event (assignee already written by set_issue_stage in step 3;
    #    only record the assignee_changed event with old_assignee captured in step 3)
    if target_assignee:
        try:
            from src.api.event_store import append_event
            # Record assignee_changed event for audit trail
            # (the actual assignee UPDATE was done by set_issue_stage in step 3;
            #  we only append the audit event here to avoid a duplicate write + race window)
            append_event(
                repo_key=repo_key,
                event_type="assignee_changed",
                title=f"Assignee changed to {target_assignee}",
                issue_id=f"#{issue_number}",
                agent_type="vod",
                source="a2a",
                actor="",
                summary=f"Cross-pipeline handoff: assignee changed from {_old_assignee or '(none)'} to {target_assignee}",
                payload={
                    "repository": repository,
                    "issue_number": issue_number,
                    "old_assignee": _old_assignee,
                    "new_assignee": target_assignee,
                    "target_scene_id": target_scene_id,
                    "source_issue_number": _cc.get("source_issue_number", ""),
                },
                status="completed",
                scene_id=target_scene_id,
            )
            log.info(f"A2A #329: assignee changed from {_old_assignee or '(none)'} to {target_assignee} for issue=#{issue_number}")
        except Exception as _ae:
            log.warning(f"A2A #329: assignee change event failed: {_ae}")

    # 6. Create cross_scene_handoff audit event
    try:
        from src.api.event_store import append_event
        append_event(
            repo_key=repo_key,
            event_type="cross_scene_handoff",
            title=f"Cross-pipeline handoff → {_target_scene_name}/{target_node}",
            issue_id=f"#{issue_number}",
            agent_type=target_node,
            source="a2a",
            actor=account,
            summary=f"Handoff to scene={_target_scene_name} node={target_node}",
            payload={
                "repository": repository,
                "issue_number": issue_number,
                "target_scene_id": target_scene_id,
                "target_node": target_node,
                "carried_context": _cc,
                "target_assignee": target_assignee,
                "engine_config": engine_config,
                "context": context[:500],
            },
            status="completed",
            scene_id=target_scene_id,
        )
    except Exception as _ce:
        log.warning(f"A2A #329: cross_scene_handoff event record failed: {_ce}")

    # 7. Dispatch A2A to target_node in target scene (normal A2A flow, but with scene override)
    # The target_node (vod) will receive the carried_context via the a2a context field.
    _handoff_context = context
    if not _handoff_context and _cc:
        _handoff_context = f"跨流水线接续：来源 Issue #{_cc.get('source_issue_number', issue_number)}，目标场景 {target_scene_id}，评级 {_cc.get('rating', 'N/A')}。Spec 参考: {_cc.get('spec_ref', 'N/A')}"

    try:
        from src.api.event_store import append_event
        _a2a_title = f"Cross-pipeline A2A → {target_node}"
        try:
            from src.api.issues_center import _get_issue_title
            _a2a_title = _get_issue_title(repo_key, int(issue_number)) or _a2a_title
        except Exception:
            pass
        seq = append_event(
            repo_key=repo_key,
            event_type="a2a_event",
            title=_a2a_title,
            issue_id=f"#{issue_number}",
            source="a2a",
            agent_type=target_node,
            actor=account,
            summary=f"A2A: → {target_node} (cross-pipeline, scene={_target_scene_name})",
            payload={
                "repository": repository,
                "account": account,
                "issue_number": issue_number,
                "pr_number": "",
                "context": _handoff_context[:500],
                "target_scene_id": target_scene_id,
                "carried_context": _cc,
                "engine_config": engine_config,
            },
            status="running",
            scene_id=target_scene_id,
        )
        log.info(f"A2A #329: cross-pipeline a2a_event created seq={seq}")
        # Trigger pipeline execution for the new event (same as normal A2A path)
        try:
            from src.api.event_bus import get_event_bus, GrapeEvent, EventDomain
            import asyncio as _aio
            handoff_event = GrapeEvent(
                domain=EventDomain.AGENT,
                type="agent/handoff",
                repo_key=repo_key,
                issue_id=f"#{issue_number}",
                agent_type=target_node,
                seq=seq,
                payload={
                    "target_agent": target_node,
                    "node_id": target_node,
                    "scene_id": target_scene_id,
                    "runtime_id": 0,
                    "evt": {
                        "type": "a2a_event",
                        "repository": repository,
                        "source": "a2a",
                        "actor": account,
                        "account": account,
                        "issue_id": str(issue_number) if issue_number else "",
                        "_a2a_context": context,
                        "payload": {
                            "repository": repository,
                            "account": account,
                            "issue_number": issue_number,
                            "pr_number": None,
                            "context": context,
                            "carried_context": carried_context or {},
                            "engine_config": engine_config,
                            "target_scene_id": target_scene_id,
                        },
                    },
                },
            )
            _bus = get_event_bus()
            if not _bus.has_listeners("agent/handoff"):
                _bus.on_sync("agent/handoff", _on_handoff_dispatch)
            _loop = _aio.get_event_loop()
            _loop.create_task(_bus.emit(handoff_event))
            log.info(f"A2A #329: cross-pipeline event bus dispatch seq={seq}")
        except Exception as _de2:
            log.warning(f"A2A #329: event bus dispatch failed, falling back to direct: {_de2}")
            try:
                import json as _pj
                _raw = _pj.dumps({"action": "a2a_event", "seq": seq, "repository": repository, "issue_number": issue_number}).encode()
                run_pipeline(_raw, {"x-grape-token": "internal"})
                log.info(f"A2A #329: cross-pipeline direct run_pipeline seq={seq}")
            except Exception as _de3:
                log.warning(f"A2A #329: direct run_pipeline also failed: {_de3}")
        return {"ok": True, "response": {"status": "ok", "seq": seq, "agent": target_node, "scene": target_scene_id, "cross_pipeline": True}}
    except Exception as _de:
        log.warning(f"A2A #329: dispatch failed: {_de}")
        return {"ok": False, "error": "dispatch_failed"}


@router.get("/api/a2a/status", operation_id="a2a_status", tags=["a2a"],
            summary="Verify whether an A2A handoff event exists (Issue #544 L3)",
            description="Idempotency verification endpoint: query by idempotency_key (exact) "
                        "and/or request fingerprint (repository+issue_number+agent). Use after a "
                        "timed-out POST /api/a2a to check whether the handoff event was already "
                        "created before retrying. Accepts JWT/session token/API key (public) "
                        "plus internal bot-token.")
async def a2a_status_endpoint(request: Request,
                              repository: str = "",
                              issue_number: str = "",
                              agent: str = "",
                              idempotency_key: str = "",
                              user: dict | None = Depends(get_user_or_none)):
    """A2A 交接核对接口（Issue #544 L3）。

    - `idempotency_key` 非空 → 按 (repo_key, key) 精确查已有事件；
    - 否则按请求指纹（sha256(repo_key|issue|agent)[:16]，与 POST 留痕实现一致，不含 pr）查 payload 命中（含失败留痕）；
    - `agent` 为空时按指纹退化为 repo+issue 维度查全部 A2A 事件。
    """
    if not repository or repository.count("/") != 1:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repository is required in 'owner/repo' format"})
    if not issue_number or not str(issue_number).isdigit():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "issue_number is required (plain number)"})

    # repo_key 解析：与 POST /api/a2a 同口径（Issue #544 审查修正②：
    # 无前缀时经 managed_repos 反查 provider，github 托管仓不再假阴性）
    repo_key = _resolve_a2a_repo_key(repository)

    # 鉴权：与 POST /api/a2a 同口径（用户级身份校验托管仓库 / 内部 bot、会话 token）
    if user and user.get("auth_type") in ("jwt", "api_key"):
        from src.user.db import get_managed_repos
        managed = {r.get("repo_key") for r in get_managed_repos(user.get("id") or 0)}
        if repo_key not in managed:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权查询该仓库的 A2A 交接"})
    else:
        _authorize_a2a(request, repo_key)

    import hashlib as _hashlib
    import json as _json
    from src.session_manager import db, db_lock
    events: list = []
    try:
        with db_lock:
            c = db.cursor()
            if idempotency_key:
                c.execute(
                    "SELECT seq, issue_id, agent_type, status, title, created_at, payload"
                    " FROM events WHERE repo_key=? AND idempotency_key=? ORDER BY seq DESC LIMIT 20",
                    (repo_key, idempotency_key))
            else:
                _fp = _hashlib.sha256(
                    f"{repo_key}|{issue_number}|{agent}".encode()).hexdigest()[:16]
                c.execute(
                    "SELECT seq, issue_id, agent_type, status, title, created_at, payload"
                    " FROM events WHERE repo_key=? AND issue_id=? AND event_type='a2a_event'"
                    " AND payload LIKE ? ORDER BY seq DESC LIMIT 20",
                    (repo_key, f"#{issue_number}", f'%"fingerprint": "{_fp}"%'))
            for row in c.fetchall():
                try:
                    _p = _json.loads(row[6] or "{}")
                except Exception:
                    _p = {}
                _qinfo = _p.get("_a2a_queue") if isinstance(_p.get("_a2a_queue"), dict) else {}
                events.append({"seq": row[0], "issue_id": row[1], "agent": row[2],
                               "status": row[3], "title": row[4], "created_at": row[5],
                               "a2a_trace": bool(_p.get("a2a_trace")),
                               "fingerprint": _p.get("fingerprint", ""),
                               "idempotency_key": _p.get("idempotency_key", ""),
                               "queued": bool(_qinfo) and row[3] == "queued",
                               "existing_seq": int(_qinfo.get("existing_seq") or 0),
                               "queue_seq": row[0] if (_qinfo and row[3] == "queued") else 0})
    except Exception as e:
        log.warning(f"A2A status lookup failed: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "a2a status lookup failed"})

    _queued = [e for e in events if e.get("queued")]
    return {"found": bool(events), "repository": repository,
            "issue_number": issue_number, "agent": agent or "",
            "idempotency_key": idempotency_key or "", "events": events,
            "queued": _queued, "queued_count": len(_queued)}


def _a2a_repo_fallback_check(repo_key: str, user_hint: dict | None = None) -> str:
    """Issue #671：A2A 归属校验回退——GitCode/GitHub 可达性校验（与 git-repo 透传端点同源口径）。

    managed_repos 未命中时调用（仅认证调用方）。token 解析与 git_repo_api._repo_token
    口径对齐：TokenResolver 策略链（会话/仓库 bot/全局 bot/host 用户）→ 会话用户 token
    （user_hint 提供 user_id/username 时按用户身份取）。

    返回 verdict：
    - "ok"            仓库存在且 token 可达（放行 + 幂等补录）
    - "not_found"     上游 404（仓库不存在/拼写错误）
    - "no_permission" 上游 403（仓库存在但无权限）
    - "unreachable"   网络/上游异常（无法判定）
    - "no_token"      无可用平台 token（无法判定）
    - "unsupported"   repo_key 前缀不支持
    """
    from src.repo_key import parse_repo_key
    _provider, _owner, _name = parse_repo_key(repo_key)
    if _provider not in ("gitcode", "github") or not _owner or not _name:
        return "unsupported"

    token = ""
    try:
        from src.token_resolver import TokenResolver, create_provider_from_repo_key
        token = TokenResolver(create_provider_from_repo_key(repo_key)).get_token(repo_key) or ""
    except Exception as _te:
        log.warning(f"A2A fallback: token resolver failed for {repo_key}: {_te}")

    if not token and user_hint:
        try:
            _uid = int(user_hint.get("id") or user_hint.get("user_id") or 0)
            _username = user_hint.get("username") or ""
            if not _username and _uid > 0:
                from src.auth.grape_account_db import get_grape_account_by_id
                _username = (get_grape_account_by_id(_uid) or {}).get("username", "")
            if _username or _uid > 0:
                from src.token_resolver import create_provider_from_repo_key
                token = create_provider_from_repo_key(repo_key).fetch_user_token(_username, _uid) or ""
        except Exception as _ue:
            log.warning(f"A2A fallback: user token fetch failed for {repo_key}: {_ue}")

    if not token:
        return "no_token"

    try:
        from src.vcs.api_client import VcsApiClient
        _info = VcsApiClient(_provider, token, _owner, _name).get_repo_info() or {}
    except Exception as _ve:
        log.warning(f"A2A fallback: repo info probe failed for {repo_key}: {_ve}")
        return "unreachable"
    _err = str(_info.get("error", "") or "")
    if not _err:
        return "ok"
    _status = _info.get("status_code") or 0
    if _status == 404 or "404" in _err:
        return "not_found"
    if _status == 403 or "403" in _err:
        return "no_permission"
    return "unreachable"


def _issue_exists_in_db(repo_key: str, issue_number) -> bool:
    """Issue #755：目标仓 issue 表存在性快查（平台已知 Issue 直接放行，避免每条 A2A 外呼 VCS）。"""
    try:
        _num = int(str(issue_number).strip().lstrip("#"))
    except (TypeError, ValueError):
        return False
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1 FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                      (repo_key, _num))
            return bool(c.fetchone())
    except Exception as _e:
        log.warning(f"A2A #755: issue existence db check failed for {repo_key}: {_e}")
        return False


def _resolve_cross_repo_issue_number(repo_key: str, issue_number, carried_context) -> tuple:
    """Issue #755 方案A：把跨仓 A2A 沿用的源仓单号解析/回填为目标仓真实落地单号。

    背景：跨仓转交时 payload.issue_number 可能是源仓单号（目标仓 VCS 404，下游任务
    注入 ISSUE_NUMBER 后全链路 404）。解析优先级：
    1. 目标仓 issue 表已有该编号行 → 原样返回（正常同仓 A2A，零行为变化）；
    2. carried_context.target_issue_number（转交建单后显式回填）且目标仓存在 → 采用；
    3. 目标仓 issue 表反查 carried_context.source_issue_number == 传入编号 → 命中
       （多命中取最大编号 = 最新落地单，与 _max_issue_from_issues 口径一致）；
    4. 均未命中 → 原样返回（unresolved，行为不变，交由方案 B 入口校验拦截）。

    返回 (resolved_number_str, via)，via ∈ {"kept","target_field","db_mapping","unresolved"}。
    """
    _num = str(issue_number or "").strip().lstrip("#")
    if not _num.isdigit():
        return _num, "unresolved"
    if _issue_exists_in_db(repo_key, _num):
        return _num, "kept"
    _cc = carried_context if isinstance(carried_context, dict) else {}
    _tgt = str(_cc.get("target_issue_number", "") or "").strip().lstrip("#")
    if _tgt.isdigit() and _tgt != _num and _issue_exists_in_db(repo_key, _tgt):
        return _tgt, "target_field"
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT issue_number, carried_context FROM issue WHERE repo_key=?",
                      (repo_key,))
            rows = c.fetchall() or []
    except Exception as _e:
        log.warning(f"A2A #755: source mapping db scan failed for {repo_key}: {_e}")
        rows = []
    _hits = []
    for _n, _raw in rows:
        if _n is None:
            continue
        try:
            _ctx = json.loads(_raw) if isinstance(_raw, str) and _raw.strip() else (_raw or {})
        except Exception:
            continue
        if isinstance(_ctx, dict) and str(_ctx.get("source_issue_number", "") or "").strip().lstrip("#") == _num:
            _hits.append(int(_n))
    if _hits:
        return str(max(_hits)), "db_mapping"
    return _num, "unresolved"


def _a2a_issue_vcs_check(repo_key: str, issue_number) -> str:
    """Issue #755 方案B：issue_number 在目标仓 VCS 的存在性校验（DB 未命中时兜底外呼）。

    token 口径与 _a2a_repo_fallback_check 对齐（TokenResolver 策略链，仅认证后调用）。
    返回 verdict：
    - "exists"    VCS 命中真实 Issue（放行）
    - "not_found" 上游 404（入口 400 拒绝）
    - "unknown"   无 token / 网络 / 其他错误（不阻断，防误杀正常链路）
    """
    from src.repo_key import parse_repo_key
    _provider, _owner, _name = parse_repo_key(repo_key)
    if _provider not in ("gitcode", "github") or not _owner or not _name:
        return "unknown"
    try:
        _num = int(str(issue_number).strip().lstrip("#"))
    except (TypeError, ValueError):
        return "not_found"
    token = ""
    try:
        from src.token_resolver import TokenResolver, create_provider_from_repo_key
        token = TokenResolver(create_provider_from_repo_key(repo_key)).get_token(repo_key) or ""
    except Exception as _te:
        log.warning(f"A2A #755: token resolver failed for {repo_key}: {_te}")
    if not token:
        return "unknown"
    try:
        from src.vcs.api_client import VcsApiClient
        _info = VcsApiClient(_provider, token, _owner, _name).get_issue(_num)
    except Exception as _ve:
        log.warning(f"A2A #755: issue probe failed for {repo_key} #{_num}: {_ve}")
        return "unknown"
    if isinstance(_info, dict) and not _info.get("error"):
        return "exists"
    _err = str(_info.get("error", "") or "") if isinstance(_info, dict) else ""
    _status = _info.get("status_code") if isinstance(_info, dict) else 0
    if _status == 404 or "404" in _err:
        return "not_found"
    return "unknown"


def _a2a_register_repo(repo_key: str) -> bool:
    """Issue #671：回退放行后幂等补录 managed_repos（最小行，INSERT OR IGNORE，不覆盖既有配置）。"""
    from src.repo_key import parse_repo_key
    _provider, _owner, _name = parse_repo_key(repo_key)
    try:
        from src.session_manager import db as _reg_db, db_lock as _reg_lock
        with _reg_lock:
            _reg_c = _reg_db.cursor()
            _reg_c.execute(
                "INSERT OR IGNORE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES (?,?,?,?)",
                (repo_key, _owner, _name, f"{_owner}/{_name}"))
            _reg_db.commit()
        log.info(f"A2A: fallback registered managed_repos row: {repo_key}")
        return True
    except Exception as _e:
        log.warning(f"A2A: fallback registration failed for {repo_key}: {_e}")
        return False


@router.post("/api/a2a", operation_id="a2a_handoff", tags=["a2a"], summary="Hand off to another agent (A2A)", description="Agent-to-Agent handoff: dispatches a new event to the target agent. Accepts JWT/session token/API key (public) plus internal bot-token.")
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

    # ── A2A 入口有效性校验（a2a-input-validation-fix）：内容错误必须同步 400 拒绝，
    # 不做"存在性检查"式的放通——否则 agent 拿到 ok 后直接结束任务，错误在异步派发
    # 内部爆发，任务死机（2026-10-14 实际事故）。
    # Issue #382/#395: repository 必须是字符串——str() 字符串化会让 dict 绕过格式校验，
    # 下游 repository.startswith 对 dict 抛 AttributeError → 500。
    if not isinstance(payload.get("repository"), str):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "invalid repository: must be a string in 'owner/repo' format"})
    repository_early = payload.get("repository") or ""
    issue_number_early = str(payload.get("issue_number") or "")
    # 1. agent 名必须在平台白名单内（agent 写错目标名时立刻暴露）
    if agent_name not in ("vod", "issue", "coding", "qa", "sre"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"invalid agent '{agent_name}': must be one of vod/issue/coding/qa/sre"})
    # 2. issue_number 必须是纯数字（防止传 "#1"、issue 标题、随机文本）
    if not issue_number_early.isdigit():
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"invalid issue_number '{issue_number_early[:50]}': must be a plain number like '1' (no '#', no text)"})
    # 3. repository 必须是 owner/repo 二段格式
    if repository_early.count("/") != 1 or not all(p.strip() for p in repository_early.split("/")):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"invalid repository '{repository_early[:100]}': must be full 'owner/repo' format, bare repo name is not accepted"})

    repository = repository_early  # Issue #382/#395: 已校验为 str，直接复用，不再取原始对象
    # Issue #1668：不再用 repository（仓库属主）兜底 account；解析放到 repo_key/单号回填后。
    account = str(payload.get("account", "") or "").strip()
    context = payload.get("context", "")
    issue_number = payload.get("issue_number", "")
    pr_number = payload.get("pr_number", "")

    # repo_key 解析：带前缀按前缀；无前缀查 managed_repos 反查实际 provider
    # （Issue #544 审查修正②：与 GET /api/a2a/status 统一走 _resolve_a2a_repo_key）
    repo_key = _resolve_a2a_repo_key(repository)
    log.info(f"A2A: agent={agent_name}, repo={repository}")

    # ── Issue #755 方案A入口：跨仓单号回填 ──
    # 源仓单号在目标仓 issue 表不存在时，按 carried_context.target_issue_number /
    # DB 反查 source_issue_number 解析真实落地单号并回填 payload.issue_number；
    # 下游 evt / 事件 / 任务注入 ISSUE_NUMBER 全链路使用回填值（resolve_issue_id
    # 从 payload.issue_number 提取，回填后自然生效）。未命中不改写（行为不变）。
    try:
        _cc_755 = payload.get("carried_context") if isinstance(payload.get("carried_context"), dict) else {}
        _mapped_num, _mapped_via = _resolve_cross_repo_issue_number(repo_key, issue_number_early, _cc_755)
        if _mapped_via in ("target_field", "db_mapping") and _mapped_num != issue_number_early:
            payload["issue_number"] = _mapped_num
            issue_number_early = _mapped_num
            issue_number = _mapped_num
            if isinstance(payload.get("carried_context"), dict):
                payload["carried_context"]["target_issue_number"] = int(_mapped_num)
            log.info(f"A2A #755: issue_number backfilled to {_mapped_num} (via {_mapped_via}, repo={repo_key})")
    except Exception as _755_e:
        log.warning(f"A2A #755: entry issue_number backfill failed, keep original: {_755_e}")

    # Issue #1668：a2a_event 触发人账号解析（payload.account → 已认证调用方登录名 → Issue creator，空则告警）。
    # 禁止回退仓库属主：解析结果同步写入 payload.account / 事件 actor / 任务 ACCOUNT。
    _caller_login = ""
    if isinstance(user, dict):
        _caller_login = str(
            user.get("username") or user.get("login") or user.get("name") or ""
        ).strip()
    account = resolve_actor_account(
        {"type": "a2a_event", "actor": _caller_login, "payload": payload,
         "issue_id": f"#{issue_number}" if issue_number else "",
         "repository": repository},
        repo_key,
    )
    payload["account"] = account

    # ── 仓库真实性校验（a2a-input-validation-fix 第4条，防假仓库）：
    # Issue #671 方案 B：注册表优先 + GitCode 可达性回退。
    # repo_key 在 managed_repos（本平台真实托管）→ 快路径放行；未命中 → 不再直接
    # 400，标记 _repo_unregistered，待鉴权通过后走 _a2a_repo_fallback_check
    # （GitCode/GitHub 可达性校验 + 幂等补录），保证同一 repo_key 与 git-repo
    # 命令两链路判定一致。查表异常时跳过（依赖下游场景节点检查兜底），
    # 避免表结构问题阻断正常 A2A。
    _repo_unregistered = False
    try:
        from src.session_manager import db as _mr_db, db_lock as _mr_lock
        with _mr_lock:
            _mr_c = _mr_db.cursor()
            _mr_c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            if not _mr_c.fetchone():
                _repo_unregistered = True
                log.info(f"A2A: repo not in managed_repos, defer to GitCode reachability fallback: {repo_key}")
    except Exception as _mr_e:
        log.warning(f"A2A: managed_repos existence check failed, skip: {_mr_e}")

    # ── Issue #544 L2：幂等键（来源 task_id + 目标角色派生）──
    # 同仓同键的重复 A2A 请求直接返回已有事件（不创建新事件、不重复派发），
    # 供客户端在超时/失败后带键安全重试。键格式：1-128 位 [A-Za-z0-9_:.-]。
    # ⚠️ 审查修正①：键格式校验可前置（纯 400 不泄露数据），但幂等命中查询必须在
    # 鉴权（_authorize_a2a / 用户级托管校验）之后执行——否则未认证调用方可凭
    # 已有键探测并获取事件 seq/status/issue_id。命中查询见下方鉴权块之后。
    import re as _re_idem
    _idem_key = str(body.idempotency_key or payload.get("idempotency_key", "") or "").strip()
    if _idem_key and not _re_idem.fullmatch(r"[A-Za-z0-9_:.\-]{1,128}", _idem_key):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "invalid idempotency_key: 1-128 chars of [A-Za-z0-9_:.-] expected"
                       " (derive it from source task_id + target agent, e.g. 'task-42:qa')"})


    # ── Issue #544 L3：失败留痕 ──
    # 请求指纹（幂等键缺省时核对接口按它查询）；处理异常且尚未建事件 → 落失败留痕事件
    import hashlib as _hashlib
    _fingerprint = _hashlib.sha256(
        f"{repo_key}|{issue_number_early}|{agent_name}".encode()).hexdigest()[:16]
    try:
        # Issue #329: cross-pipeline handoff — extract carried context fields.
        # When target_scene_id is specified, redirect the A2A to the target scene's
        # target_node (default vod). This enables cross-pipeline continuation without
        # creating a duplicate Issue. The dedup guard (acd07be2) naturally prevents
        # duplicate issue_opened events for the same Issue in the target scene.
        target_scene_id = payload.get("target_scene_id", "")
        target_node = payload.get("target_node", "")
        carried_context = payload.get("carried_context") or {}
        target_assignee = payload.get("target_assignee", "")
        engine_config = payload.get("engine_config")

        if target_scene_id and issue_number:
            _redirect = _cross_pipeline_handoff(
                repo_key, issue_number, target_scene_id, target_node or "vod",
                carried_context, target_assignee, engine_config, context, repository, account,
            )
            if _redirect.get("ok"):
                log.info(f"A2A #329: cross-pipeline handoff redirected to scene={target_scene_id} node={target_node or 'vod'}")
                return _redirect["response"]
            if _redirect.get("error"):
                log.info(f"A2A #329: cross-pipeline handoff skipped: {_redirect['error']}")
                # Issue #729：拦截响应透传 original_event（含 task_ref/result_ref）
                _skip_content = {
                    "status": "ok", "seq": 0, "agent": target_node or "vod",
                    "skipped": _redirect["error"],
                }
                if _redirect.get("original_event"):
                    _skip_content["original_event"] = _redirect["original_event"]
                return JSONResponse(status_code=200, content=_skip_content)

        # Issue #51: cancelled 任务的 A2A 信号一律拒绝（防止取消后流水线继续流转）
        if issue_number and repo_key:
            try:
                from src.session_manager import db as _ck_db, db_lock as _ck_lock
                with _ck_lock:
                    _ck_c = _ck_db.cursor()
                    _ck_c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=? AND status='cancelled' LIMIT 1",
                                  (repo_key, int(issue_number)))
                    if _ck_c.fetchone():
                        log.info(f"A2A: rejected — task cancelled, issue=#{issue_number}, agent={agent_name}")
                        return {"ok": False, "skipped": "task_cancelled"}
            except Exception as _ck_e:
                log.warning(f"A2A: cancelled check failed: {_ck_e}")
        seq = 0

        # 鉴权：用户级身份（JWT/API Key，校验托管仓库）或内部 bot/会话 token（_authorize_a2a）
        _a2a_session_info = None
        if user and user.get("auth_type") in ("jwt", "api_key"):
            from src.user.db import get_managed_repos
            managed = {r.get("repo_key") for r in get_managed_repos(user.get("id") or 0)}
            if repo_key not in managed:
                raise HTTPException(status_code=403, detail={"code": 403, "message": "无权对该仓库发起 A2A"})
        else:
            # 会话 token（repo 作用域）或 仓库 bot token（兼容存量 worker）
            _a2a_session_info = _authorize_a2a(request, repo_key)

        # Issue #671：注册表未命中 → GitCode 可达性回退（鉴权之后执行，
        # 未认证调用方不触发外呼，维持原 400 not hosted 拒绝语义）。
        # 仅以下认证形态触发回退：JWT/API Key 用户、或会话 token 有效
        # （_authorize_a2a 返回 session info）。bot token / 未认证 defer 形态不回退。
        if _repo_unregistered:
            _fb_hint = None
            if user and user.get("auth_type") in ("jwt", "api_key"):
                _fb_hint = user
            elif _a2a_session_info:
                _fb_hint = {"user_id": _a2a_session_info.get("user_id") or 0,
                            "username": _a2a_session_info.get("username") or ""}
            if _fb_hint is not None:
                _verdict = _a2a_repo_fallback_check(repo_key, _fb_hint)
                if _verdict == "ok":
                    # 放行 + 幂等补录（自愈注册缺口，重复调用不产生重复行）
                    _a2a_register_repo(repo_key)
                elif _verdict == "not_found":
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"repository '{repository}' not found on the Git platform (repo_key={repo_key}): 仓库不存在或 owner/repo 拼写错误，请校正后重试"})
                elif _verdict == "no_permission":
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"repository '{repository}' exists but the server has no access to it (repo_key={repo_key}): 请检查仓库权限或先在平台上托管该仓库"})
                else:
                    # no_token / unreachable / unsupported：无法判定可达性 → 维持「未托管」安全默认
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"repository '{repository}' is not hosted on this Grape server (repo_key={repo_key}): 请检查 owner/repo 拼写，或先在平台上托管该仓库"})
            else:
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"repository '{repository}' is not hosted on this Grape server (repo_key={repo_key}): 请检查 owner/repo 拼写，或先在平台上托管该仓库"})

        # ── Issue #755 方案B：issue_number 目标仓存在性校验（鉴权后，未认证不外呼）──
        # 跨仓 A2A 沿用源仓单号（目标仓 VCS 404）会污染异步链路：入口同步 400 拒绝
        # 并提示转交建单状态（与 a2a-input-validation-fix 同口径，错误不流入异步派发）。
        # DB 快查命中（平台已知 Issue）→ 放行不外呼；VCS 明确 404 → 400；
        # 网络/无 token（unknown）→ 放行，不误杀正常链路。
        if issue_number and not _issue_exists_in_db(repo_key, issue_number):
            _issue_verdict = _a2a_issue_vcs_check(repo_key, issue_number)
            if _issue_verdict == "not_found":
                log.info(f"A2A #755: rejected — issue #{issue_number} not found in target repo {repo_key}")
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"issue_number '{issue_number}' not found in repository '{repository}' (repo_key={repo_key}): "
                               f"跨仓转交请先在目标仓完成建单，再用真实落地单号发起 A2A"})

        # Issue #544 审查修正①：幂等命中查询在鉴权之后——未认证调用方不得
        # 凭已有幂等键探测/获取事件信息（seq/status/issue_id）。
        if _idem_key:
            from src.api.event_store import find_event_by_idempotency_key
            _existing = find_event_by_idempotency_key(repo_key, _idem_key)
            if _existing:
                log.info(f"A2A: idempotent hit key={_idem_key} seq={_existing['seq']}, return existing event")
                return JSONResponse(status_code=200, content={
                    "status": "ok", "seq": _existing["seq"], "agent": _existing["agent_type"] or agent_name,
                    "idempotent": True,
                    "message": "duplicate idempotency_key, existing event returned",
                    "event": _a2a_event_payload(
                        _existing["seq"], _existing["status"],
                        _existing["agent_type"], _existing["issue_id"],
                        _existing["created_at"]),
                })

        # PR 订阅模式拦截：PR 订阅触发的 QA 不允许 A2A（只做审查，不流转流水线）
        # 通过 session token 反查调用方 agent_tasks.issue_id：
        #   PR订阅QA 的 issue_id 以 "#pr_" 开头（伪 ID），流水线 QA 的 issue_id 是真实 Issue 号
        #   session token 通道（agent CLI 调用）可反查；bot token 通道（服务端 resume/redispatch）无身份，放行
        if _a2a_session_info and _a2a_session_info.get("session_key"):
            try:
                from src.session_manager import db as _prsub_db, db_lock as _prsub_lock
                _caller_task_id = _a2a_session_info["session_key"]
                with _prsub_lock:
                    _prsub_c = _prsub_db.cursor()
                    _prsub_c.execute("SELECT issue_id FROM agent_tasks WHERE id=?", (_caller_task_id,))
                    _prsub_row = _prsub_c.fetchone()
                if _prsub_row and _prsub_row[0] and _prsub_row[0].startswith("#pr_"):
                    log.info(f"A2A: rejected — PR subscription QA (task={_caller_task_id}, issue={_prsub_row[0]}), A2A disabled")
                    return {"ok": False, "skipped": "pr_subscription_a2a_disabled"}
            except Exception as _prsub_e:
                log.warning(f"A2A: PR subscription check failed: {_prsub_e}")

        # Agent 启用检查（仓库级 enable，见 agent-enable-toggle-design.md）
        from src.api.session_store import is_agent_enabled
        if not is_agent_enabled(repo_key, agent_name):
            log.warning(f"A2A: agent '{agent_name}' disabled for repo={repo_key}, rejected")
            return JSONResponse(status_code=403, content={"code": 403, "message": f"agent {agent_name} disabled"})

        # 场景节点校验：目标 agent 不在当前场景的 pipeline nodes 里 → 拦截（不创建事件）
        # 防止 agent skill 硬编码 A2A 目标（如 coding→qa）与自定义场景 pipeline 冲突
        # （项目场景 nodes=单一事实源；空 nodes → 任何 agent 都不在场景 → 拦截；见 a2a-scene-node-check-fix-design.md）
        if issue_number:
            _scene_ck = _scene_node_check(repo_key, issue_number, agent_name)
            if not _scene_ck.get("in_scene"):
                log.info(f"A2A: agent '{agent_name}' not in scene '{_scene_ck.get('scene_id')}' for issue=#{issue_number}, skipped (nodes={[n.get('id') for n in _scene_ck.get('nodes') or []]})")
                return JSONResponse(status_code=409, content={"status": "skipped", "seq": 0, "agent": agent_name, "skipped": "not_in_scene", "scene": _scene_ck.get("scene_id"), "message": f"agent '{agent_name}' is not in the current scene pipeline (nodes={[n.get('id') for n in _scene_ck.get('nodes') or []]})"})

        # 启动后台执行目标 Agent（由 _execute_agent 判定产出后存储事件）
    except HTTPException:
        raise
    except Exception as _a2a_e:
        # Issue #544 L3：应用层处理失败 → 事件表留可查的失败记录（含请求指纹），
        # 供核对接口/技能超时后自动核对使用；响应 500 带指纹。
        log.error(f"A2A: processing failed before event creation: {_a2a_e}")
        try:
            from src.api.event_store import append_event as _append_trace
            _append_trace(
                repo_key=repo_key, event_type="a2a_event",
                title=f"A2A 交接失败留痕: {agent_name}",
                issue_id=f"#{issue_number}" if issue_number else "#0",
                source="a2a", agent_type=agent_name, actor=account,
                summary=f"A2A failed before event creation: {str(_a2a_e)[:150]}",
                payload={"a2a_trace": True, "fingerprint": _fingerprint,
                         "idempotency_key": _idem_key, "repository": repository,
                         "account": account, "issue_number": issue_number,
                         "pr_number": pr_number, "context": context[:500],
                         "error": str(_a2a_e)[:300]},
                status="failed",
                result=f"A2A 请求处理异常（未派发）：{str(_a2a_e)[:200]}")
        except Exception as _te:
            log.warning(f"A2A: trace write failed: {_te}")
        return JSONResponse(status_code=500, content={
            "code": 500, "message": f"a2a processing failed: {str(_a2a_e)[:150]}",
            "fingerprint": _fingerprint,
            "hint": "retry with the same idempotency_key, or verify via GET /api/a2a/status",
        })
    try:
        from src.api.event_pipeline import _execute_agent
        from src.session_manager import db, db_lock
        backend = ""
        # payload.runtime_id 优先（接管场景：指定路由到本机 worker）
        runtime_id = int(payload.get("runtime_id", 0) or 0)
        skill_name = ""
        skill_id = 0
        skill_path = ""
        repo_name = repository.split("/", 1)[-1] if "/" in repository else repository
        try:
            with db_lock:
                c = db.cursor()
                # payload 未指定 runtime_id 时，从 session 表查绑定的引擎
                if not runtime_id:
                    c.execute("SELECT runtime_id, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_name))
                    row = c.fetchone()
                else:
                    # payload 指定了 runtime_id：只查 skill/skill_id
                    c.execute("SELECT 0, skill, skill_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_name))
                    row = c.fetchone()
                if row:
                    # payload 指定了 runtime_id 时不覆盖；否则用 session 表的值
                    if not runtime_id:
                        runtime_id = row[0] or 0
                    backend = ""
                    if row[0]:
                        c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
                        brow = c.fetchone()
                        if brow and brow[0]:
                            backend = brow[0]
                    # payload 指定 runtime_id 时查该 runtime 的 backend 名
                    if runtime_id and not backend:
                        c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
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
                if is_plugin_mode() and _a2a_issue_scene:
                    # 2026-10-10：去掉 != "default" —— 与 event_pipeline 主路径口径一致，
                    # default 场景 skill 改动在 a2a 路径同样生效
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
                            # Issue #657: skill 解析为空（仅 user scope 可用，scope 不匹配）
                            # → 立即落 skill_resolve_failed 错误事件并返回 failed，不再派发
                            from src.api.event_pipeline import (
                                _skill_scope_mismatch, _skill_scope_mismatch_reason,
                                _fail_skill_resolve_event)
                            _mm, _mm_info = _skill_scope_mismatch(_pipe_skill, repo_key)
                            if _mm:
                                _reason = _skill_scope_mismatch_reason(
                                    _pipe_skill, _mm_info, _a2a_issue_scene, agent_name)
                                _fr = _fail_skill_resolve_event(
                                    repo_key, _pipe_skill, _a2a_issue_scene, agent_name,
                                    _reason,
                                    issue_id=f"#{issue_number}" if issue_number else "")
                                return JSONResponse(
                                    status_code=400,
                                    content={"code": 400, "status": "failed",
                                             "seq": _fr.get("seq", 0),
                                             "agent": agent_name,
                                             "skill": _pipe_skill,
                                             "skill_resolve_failed": True,
                                             "message": _reason})
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
                # Issue #329: pass through cross-pipeline fields for downstream agents
                "carried_context": carried_context if (target_scene_id or carried_context) else {},
                "engine_config": engine_config if (target_scene_id or engine_config) else None,
                # Issue #627: production release binding context pass-through
            },
        }

        # Issue 生命周期阶段推进（D9/D10，见 vod-skill-upgrade-plan.md §3.0.2）
        # A2A 交办 → issue_state 归属转移到目标 agent（允许回退，如 qa 回 coding）
        # 不做跳步修正、不做回退取消：stage-owner 机制保证最新归属的 agent 负责处理
        #（issue-comment-stage-owner-routing-design.md）；旧的 A2A 发起方早已完成其交办。
        if issue_number:
            try:
                from src.api.session_store import get_issue_stage
                _stage_info = get_issue_stage(repo_key, f"#{issue_number}")
                _current_stage = _stage_info["stage"] if _stage_info else ""
                _pipeline_next = {}
                try:
                    import os as _os, json as _json
                    from src.api.session_store import get_issue_scene
                    _scene_id, _ = get_issue_scene(repo_key, f"#{issue_number}") or ("default", 0)
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
            except Exception as _pe:
                log.warning(f"A2A: pipeline order check failed: {_pe}")

            try:
                from src.api.session_store import set_issue_stage
                # Issue #51: 最后节点完成后不再自动 done，改为 pending_review 等待开发者审核
                _expected_next = _pipeline_next.get(_current_stage, "") if _pipeline_next else ""
                if agent_name == "done" or (_expected_next == "done" and agent_name not in (_stage_order or [])):
                    _stage = "pending_review"
                    log.info(f"A2A: last node completed → pending_review issue=#{issue_number} (awaiting user review)")
                else:
                    # 不做跳步修正——agent 有权 A2A 到任意目标 agent（含回滚），
                    # pipeline 顺序由 skill 逻辑保证，A2A 层不干预。
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
        # Issue #587：去重判定键 = 事件活跃(running/dispatched)；已终态事件不阻断新交接
        # Issue #1667：去重键增加 context/idempotency_key 维度；命中活跃事件时支持
        #   queue=true（排队，前序终态后自动派发）/ force=true（立即新建事件）。
        _force = bool(getattr(body, "force", False) or payload.get("force"))
        _queue = bool(getattr(body, "queue", False) or payload.get("queue"))
        _a2a_ctx_key = _a2a_context_key(_idem_key, context)
        if issue_number:
            _existing_seq = _has_active_a2a_event(
                repo_key, f"#{issue_number}", agent_name, context_key=_a2a_ctx_key)
            if _existing_seq and _force:
                log.info(f"A2A: force=true — bypass active-event dedup (issue=#{issue_number} "
                         f"agent={agent_name} existing_seq={_existing_seq})")
                _cancel_superseded_queued_a2a(
                    repo_key, f"#{issue_number}", agent_name, _a2a_ctx_key, "force=true")
            elif _existing_seq and _queue:
                # 审查修复②：排队落库改为「查重 + 插入」同一 db_lock/事务内原子完成，
                # 无 idempotency_key 的并发同上下文请求不会再产生多条排队行。
                from src.api.event_store import append_queued_a2a_event
                from datetime import datetime, timezone
                _a2a_ref_seq_q = _a2a_source_event_seq(_a2a_session_info)
                _a2a_title_q = ""
                try:
                    from src.api.issues_center import _get_issue_title
                    _a2a_title_q = _get_issue_title(repo_key, int(issue_number)) or ""
                except Exception:
                    pass
                if not _a2a_title_q:
                    _a2a_title_q = f"A2A: {agent_name}"
                _q_payload = {
                    "repository": repository, "account": account,
                    "issue_number": issue_number, "pr_number": pr_number,
                    "context": context[:500],
                    "carried_context": carried_context if (target_scene_id or carried_context) else {},
                    "engine_config": engine_config if (target_scene_id or engine_config) else None,
                    "ref_seq": _a2a_ref_seq_q,
                    "idempotency_key": _idem_key,
                    "fingerprint": _fingerprint,
                    "queued": True,
                    "_a2a_queue": {
                        "existing_seq": _existing_seq,
                        "enqueued_at": datetime.now(timezone.utc).isoformat(),
                        "dispatch": {
                            "runtime_id": runtime_id or 0,
                            "backend": backend,
                            "skill_name": skill_name,
                            "skill_id": skill_id,
                            "skill_path": skill_path,
                            "evt": evt,
                        },
                    },
                }
                _qseq = append_queued_a2a_event(
                    repo_key, issue_id=f"#{issue_number}", agent_type=agent_name,
                    title=_a2a_title_q, summary=f"A2A: → {agent_name} (queued)",
                    payload=_q_payload, context_key=_a2a_ctx_key,
                    idempotency_key=_idem_key)
                if _qseq < 0:
                    _reused_seq = -_qseq
                    log.info(f"A2A: queue idempotent hit — reuse queued seq={_reused_seq}")
                    return JSONResponse(status_code=200, content={
                        "status": "ok", "seq": 0, "agent": agent_name,
                        "skipped": "queued", "queued": True, "idempotent": True,
                        "reason": "active_a2a_event",
                        "existing_seq": _existing_seq, "queue_seq": _reused_seq,
                        "message": "same context already queued; existing queued event returned",
                        "event": _a2a_event_payload(
                            _reused_seq, "queued", agent_name,
                            f"#{issue_number}" if issue_number else ""),
                    })
                if not _qseq:
                    log.error(f"A2A: queue event store failed for agent={agent_name}, issue=#{issue_number}")
                    return JSONResponse(status_code=500, content={"code": 500, "message": "event store failed, retry A2A"})
                # 审查修复①：覆盖「活跃判定 → 排队落库」竞态窗口——若前序在间隙内已终态，
                # 终态钩子已在本次写入前执行完，排队行将无人派发；此处立即对账兜底。
                try:
                    from src.api.event_pipeline import reconcile_queued_a2a
                    reconcile_queued_a2a(repo_key)
                except Exception as _re:
                    log.warning(f"A2A: queue reconcile after enqueue failed: {_re}")
                log.info(f"A2A: queued (issue=#{issue_number} agent={agent_name} "
                         f"queue_seq={_qseq} existing_seq={_existing_seq})")
                return JSONResponse(status_code=200, content={
                    "status": "ok", "seq": 0, "agent": agent_name,
                    "skipped": "queued", "queued": True,
                    "reason": "active_a2a_event", "existing_seq": _existing_seq,
                    "queue_seq": _qseq,
                    "message": "same issue+agent already has an active a2a event (existing_seq); "
                               "this handoff is queued and will be dispatched automatically "
                               "after that event reaches a terminal state",
                    "event": _a2a_event_payload(
                        _qseq, "queued", agent_name,
                        f"#{issue_number}" if issue_number else ""),
                })
            elif _existing_seq:
                log.info(f"A2A: duplicate skip — issue=#{issue_number} agent={agent_name} already has running event seq={_existing_seq}")
                # Issue #729：拦截响应携带原任务产物引用（task_ref/result_ref），
                # 下游凭引用可续接原任务产物，无需重新定位
                return _duplicate_a2a_skip_response(agent_name, repo_key, _existing_seq)

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
                from src.api.issues_center import _get_issue_title
                _a2a_title = _get_issue_title(repo_key, int(issue_number)) or ""
            except Exception:
                pass
        if not _a2a_title:
            _a2a_title = f"A2A: {agent_name}"
        # Issue #476：记录源事件 seq（触发本次交办的调用方事件；无 session 时为 0）
        _a2a_ref_seq = _a2a_source_event_seq(_a2a_session_info)
        seq = append_event(
            repo_key=repo_key,
            event_type="a2a_event",
            title=_a2a_title,
            issue_id=f"#{issue_number}" if issue_number else "#0",
            source="a2a",
            agent_type=agent_name,
            actor=account,
            summary=f"A2A: → {agent_name}",
            payload={"repository": repository, "account": account, "issue_number": issue_number, "pr_number": pr_number, "context": context[:500], "is_subtask_mode": _is_subtask_mode, "parent_issue": _parent_issue, "carried_context": carried_context if (target_scene_id or carried_context) else {}, "engine_config": engine_config if (target_scene_id or engine_config) else None, "ref_seq": _a2a_ref_seq, "idempotency_key": _idem_key, "fingerprint": _fingerprint},
            status="running",
            idempotency_key=_idem_key,
        )
        if seq < 0:
            # Issue #544 L2：并发窗口内撞唯一索引 → 返回已有事件（与前置查询同语义）
            _dup = find_event_by_idempotency_key(repo_key, _idem_key)
            log.info(f"A2A: idempotent race hit key={_idem_key} seq={-seq}, return existing event")
            return JSONResponse(status_code=200, content={
                "status": "ok", "seq": -seq, "agent": (_dup or {}).get("agent_type") or agent_name,
                "idempotent": True,
                "message": "duplicate idempotency_key, existing event returned",
                "event": _a2a_event_payload(
                    -seq, (_dup or {}).get("status", ""),
                    (_dup or {}).get("agent_type", ""), (_dup or {}).get("issue_id", ""),
                    (_dup or {}).get("created_at", "")),
            })
        if not seq:
            log.error(f"A2A: append_event returned 0, cannot create event for agent={agent_name}, issue=#{issue_number}")
            return JSONResponse(status_code=500, content={"code": 500, "message": "event store failed, retry A2A"})
        log.info(f"A2A: event created, seq={seq}")

        _dispatch_result = run_a2a_dispatch(
            repo_key, seq, agent_name, evt,
            skill_name=skill_name, skill_id=skill_id, skill_path=skill_path,
            runtime_id=runtime_id or 0, backend=backend,
            issue_number=issue_number, repository=repository,
            ref_seq=_a2a_ref_seq,
        )
        return _augment_a2a_dispatch_response(
            _dispatch_result, agent_name, issue_number)
    except Exception as e:
        log.warning(f"A2A: failed to start agent execution: {e}")
        # 事件已创建（running）但执行启动失败 → 立即置 failed，防卡 running 僵尸
        # Issue #418：try 兜底早退统一走 _fail_event（原裸 update_event_status 无诊断字段）
        if seq:
            try:
                from src.api.event_pipeline import _fail_event
                _fail_event(seq, f"A2A 派发启动异常: {str(e)[:150]}")
            except Exception:
                pass
        seq = 0

    return {"status": "ok", "seq": seq, "agent": agent_name}


# ── 跨流水线移交开放接口（开放契约 / CLI `grape pipeline handoff`）──
# 语义化别名：把 /api/a2a 的 target_scene_id 移交路径暴露成独立 operation，
# 让 CLI 按 operation_id 前缀渲染成 `grape pipeline handoff`（契约→CLI 约定
# <tag>_<动词>，见 docs/weekly-03/operation-id-explicit-naming-design.md）。
# 注：路由级 tags=["webhook"] 会和端点 tags 合并（与 /api/a2a 的
# ['webhook','a2a'] 同形），CLI 分组取 operation_id 前缀而非 tags[0]，故无影响。
# 与 /api/a2a 的差异只有一处：必须携带 payload.target_scene_id，缺则同步 400，
# 避免命令名承诺「跨流水线」却静默退化成普通场内派发。
@router.post(
    "/api/pipeline/handoff", operation_id="pipeline_handoff", tags=["pipeline"],
    summary="Hand off an issue to another pipeline scene",
    description=(
        "Cross-pipeline handoff: same contract as POST /api/a2a, but "
        "payload.target_scene_id is required. Redirects the event to the target "
        "scene's target_node (default vod) without creating a duplicate issue. "
        "Accepts JWT/session token/API key (public) plus internal bot-token."
    ),
)
async def pipeline_handoff_endpoint(request: Request, body: A2AHandoffBody,
                                    user: dict | None = Depends(get_user_or_none)):
    """跨流水线移交：强制携带 payload.target_scene_id，其余语义全部委托 a2a_endpoint。"""
    payload = body.payload or {}
    target_scene_id = str(payload.get("target_scene_id") or "").strip()
    if not target_scene_id:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "missing payload.target_scene_id: pipeline handoff must name the target scene"
                       " (use POST /api/a2a for in-scene dispatch)",
        })
    return await a2a_endpoint(request, body, user)
