"""问题反馈路由（Issue #174 + feedback-openapi-expose-design weekly-08）。

数据存储在 grape_db 的 user_feedback 表中，由工作台直接读写。
Admin 管理台通过 grapeJdbc 直接查同一张表。

weekly-08 对外改造：
- POST /api/feedback（submit）对外 = dual（`tk_` Agent 可调，自动反馈闭环）；
- mine/context/delete/upload = user_only（`require_no_session_token`）；
- list/stats/status 为管理端点 = require_admin（堵住原本无鉴权漏洞）；
- tk_ 提交由服务端从 token 权威解析仓库/任务/agent/技能（不信任请求体）。
"""
from fastapi import APIRouter, Depends, Query, UploadFile, File, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
import os, uuid
from src.api.deps import (
    get_current_user_or_api_key,
    require_no_session_token,
    require_admin,
)
from src.config import log
from src.session_manager import db, db_lock

# 每个 Agent 任务（tk_ / session_key）最大反馈条数（feedback-openapi-expose-design.md §四.9）
FEEDBACK_MAX_PER_AGENT_TASK = 2

router = APIRouter(prefix='/api', tags=['feedback'])


class FeedbackBody(BaseModel):
    category: str
    title: str
    description: str
    page_url: str = ''
    repo_key: str = ''
    attachment_url: str = ''
    attachment_name: str = ''
    event_seq: int = 0
    feedback_kind: str = ''


def _check_context_access(user: dict, repo_key: str) -> None:
    """反馈上下文仓库归属校验。

    session_token（tk_）→ 跳过（token 已绑定仓库，作用域天然受限）；
    jwt / api_key → 走真实 managed_repos 校验（api_key 也按 uid 查，防跨仓库读取
    他人事件上下文，feedback-openapi 注 7）。
    """
    if user.get("auth_type") == "session_token":
        return
    from src.api.deps import check_repo_access
    # api_key 也强制真实校验：把 auth_type 伪装成 jwt，让 check_repo_access 走 managed_repos
    check_repo_access({**user, "auth_type": "jwt"}, repo_key)


def _resolve_skill(skill_name: str):
    """技能名 → (skill_id, skill_name, scope)；official 优先，取活跃技能。"""
    if not skill_name:
        return None, '', ''
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, skill_name, scope FROM skill WHERE skill_name=? AND is_active=1 "
                "ORDER BY CASE scope WHEN 'official' THEN 0 WHEN 'repo' THEN 1 ELSE 2 END LIMIT 1",
                (skill_name,))
            r = c.fetchone()
        if r:
            return r[0], r[1] or '', r[2] or ''
        return None, skill_name, ''
    except Exception:
        return None, skill_name, ''


def _collect_feedback_context(event_seq: int, user: dict):
    """按 event_seq 服务端现查任务上下文（feedback-task-context-design.md §4.1）。

    权威信息全部取自 DB（events/agent_tasks/agent_runtimes），不信任前端拼装：
    - 仓库/issue/agent → events 表
    - 任务 → agent_tasks（event_seq 关联最新一条）
    - 技能 → agent_tasks.skill → skill 表（id/scope，weekly-08 新增）
    - 引擎 → 与 events 详情同源的多级归因（runtime_id → 场景 → session）
    返回 dict（含 repo_key/task/issue/engine/skill/fail_summary），失败返回 None。
    """
    import re as _re
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, repo_key, issue_id, agent_type, status, runtime, result "
                "FROM events WHERE seq=?",
                (event_seq,))
            row = c.fetchone()
        if not row:
            return None
        repo_key = row[1] or ''
        # 权限：与 events 详情一致（无权访问该仓库的事件不能带上下文）
        try:
            _check_context_access(user, repo_key)
        except Exception:
            return None
        # issue_id 形如 "379" 或 "#pr_12_3"：拆内部 id 与展示号
        raw_issue = str(row[2] or '')
        issue_number = ''
        m = _re.search(r"(\d+)", raw_issue)
        if m:
            issue_number = m.group(1)
        # 内部整型 card id（grape issue 表主键）
        issue_id_int = None
        try:
            with db_lock:
                c2 = db.cursor()
                c2.execute("SELECT id FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                           (repo_key, issue_number))
                ir = c2.fetchone()
                if ir:
                    issue_id_int = ir[0]
        except Exception:
            pass
        # 关联任务（最新一条）
        task = None
        engine = None
        task_result_text = ''
        skill_id = None
        skill_name = ''
        skill_scope = ''
        try:
            with db_lock:
                c3 = db.cursor()
                c3.execute(
                    "SELECT id, status, runtime_id, exit_code, started_at, completed_at, "
                    "result_text, skill FROM agent_tasks WHERE event_seq=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (event_seq,))
                tr = c3.fetchone()
            if tr:
                task = {
                    "task_id": tr[0], "task_status": tr[1] or '', "runtime_id": tr[2] or 0,
                    "exit_code": tr[3], "started_at": tr[4] or '', "completed_at": tr[5] or '',
                }
                task_result_text = str(tr[6] or '')
                skill_id, skill_name, skill_scope = _resolve_skill(tr[7] or '')
        except Exception:
            task = None
        # 引擎归因（与 events 详情同源：runtime_id → 场景 → session 绑定）
        runtime_id = (task or {}).get("runtime_id") or 0
        if not runtime_id:
            try:
                with db_lock:
                    c4 = db.cursor()
                    c4.execute(
                        "SELECT runtime_id FROM devops_agent_session "
                        "WHERE repo_key=? AND agent_type=? AND runtime_id>0 LIMIT 1",
                        (repo_key, row[3] or ''))
                    sr = c4.fetchone()
                if sr:
                    runtime_id = sr[0]
            except Exception:
                pass
        if runtime_id:
            try:
                with db_lock:
                    c5 = db.cursor()
                    c5.execute(
                        "SELECT ar.name, ar.credential_id, COALESCE(rc.device_name,''), "
                        "COALESCE(rc.last_ip,''), COALESCE(ar.provider,'') "
                        "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc "
                        "ON rc.id=ar.credential_id WHERE ar.id=?",
                        (runtime_id,))
                    ar = c5.fetchone()
                if ar:
                    from src.runtime.instances import instance_name
                    engine = {
                        "runtime": row[5] or ar[4] or '',
                        "engine_instance": instance_name(ar[0] or "engine", ar[2] or "", ar[1] or 0),
                        "engine_device": ar[2] or '',
                        "engine_ip": ar[3] or '',
                        "runtime_id": runtime_id,
                    }
            except Exception:
                engine = None
        if engine and runtime_id:
            try:
                from src.runtime.health_scorer import get_health_score
                hd = get_health_score(runtime_id)
                engine["health_score"] = hd.get("health_score")
            except Exception:
                pass
        # 失败摘要：result_text 优先，回退 events.result
        summary_src = task_result_text
        if not summary_src:
            summary_src = str(row[6] or '')
        fail_summary = (summary_src or '')[:500]
        return {
            "event_seq": event_seq,
            "repo_key": repo_key,
            "issue_id": issue_id_int,
            "issue_number": issue_number,
            "agent_type": row[3] or '',
            "event_status": row[4] or '',
            "task": task,
            "engine": engine,
            "skill": {"skill_id": skill_id, "skill_name": skill_name, "skill_scope": skill_scope},
            "fail_summary": fail_summary,
        }
    except Exception as e:
        log.warning(f"feedback context collect failed: {e}")
        return None


def _resolve_token_task_context(user: dict) -> dict:
    """session_token（tk_）分支：从 token 权威解析仓库/任务/agent/技能，不信任请求体。

    repo_key/agent_type ← token 绑定字段；
    task_id ← token.session_key → agent_tasks 反查 event_seq/skill/runtime_id。
    """
    repo_key = user.get("repo_key") or ''
    task_id = user.get("session_key") or ''
    agent_type = user.get("agent_type") or ''
    skill_name = ''
    event_seq = None
    runtime_id = 0
    if task_id:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT event_seq, agent_type, skill, runtime_id, repo_key "
                    "FROM agent_tasks WHERE id=? LIMIT 1",
                    (task_id,))
                r = c.fetchone()
            if r:
                event_seq = r[0]
                agent_type = agent_type or (r[1] or '')
                skill_name = r[2] or ''
                runtime_id = r[3] or 0
                repo_key = repo_key or (r[4] or '')
        except Exception:
            pass
    skill_id, skill_name, skill_scope = _resolve_skill(skill_name)
    return {
        "repo_key": repo_key,
        "task_id": task_id,
        "agent_type": agent_type,
        "skill_id": skill_id,
        "skill_name": skill_name,
        "skill_scope": skill_scope,
        "event_seq": event_seq,
        "runtime_id": runtime_id,
    }


def _agent_task_feedback_count(task_id: str) -> int:
    """统计同一任务（tk_ session_key）已提交的 agent 反馈条数。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM user_feedback WHERE source='agent' AND task_id=? AND is_deleted=0",
                (task_id,))
            return c.fetchone()[0]
    except Exception:
        return 0


@router.get('/feedback/context/{event_seq}', operation_id="feedback_context",
            summary="Get feedback context", description="Task-failure feedback context (server-collected, repo access checked), user-only (tk_ -> 403).",
            tags=["feedback"])
def get_feedback_context(event_seq: int, user: dict = Depends(require_no_session_token)):
    """任务失败反馈上下文（服务端权威搜集，前端只传 seq）。"""
    ctx = _collect_feedback_context(event_seq, user)
    if ctx is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "事件未找到或无权限"})
    return ctx


@router.post('/feedback', operation_id="feedback_submit",
             summary="Submit feedback", description="Submits user feedback (FeedbackBody). Agent session token (tk_) auto-resolves repo/task/agent/skill from the token; JWT/API Key fall back to event_seq server lookup or body repo_key.",
             tags=["feedback"])
def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user_or_api_key)):
    """提交问题反馈（写入 grape_db user_feedback 表）。

    - session_token（tk_，Agent 自动反馈）：服务端从 token 权威解析上下文，
      忽略请求体的 repo_key/event_seq/skill；每任务 ≤ FEEDBACK_MAX_PER_AGENT_TASK 条。
    - JWT / API Key：带 event_seq 时服务端现查上下文落库，repo_key 仅在无 seq 时生效。
    """
    auth_type = user.get("auth_type", "jwt")
    source = "agent" if auth_type == "session_token" else "user"

    if auth_type == "session_token":
        # Agent 自动反馈：token 权威解析（不信任 body）
        token_ctx = _resolve_token_task_context(user)
        repo_key = token_ctx["repo_key"]
        task_id = token_ctx["task_id"] or ''
        # 每任务限流：≤2 条
        if task_id and _agent_task_feedback_count(task_id) >= FEEDBACK_MAX_PER_AGENT_TASK:
            raise HTTPException(status_code=429, detail={
                "code": 429, "message": f"每个任务最多提交 {FEEDBACK_MAX_PER_AGENT_TASK} 条反馈"})
        event_seq = token_ctx.get("event_seq")
        ctx = None
        if event_seq:
            ctx = _collect_feedback_context(event_seq, user)
        if ctx is None:
            # token 已绑定仓库，直接可用；任务上下文尽力而为
            ctx = {
                "repo_key": repo_key,
                "event_seq": event_seq,
                "issue_id": None, "issue_number": '',
                "agent_type": token_ctx["agent_type"],
                "task": {"task_id": task_id} if task_id else None,
                "skill": {"skill_id": token_ctx["skill_id"],
                          "skill_name": token_ctx["skill_name"],
                          "skill_scope": token_ctx["skill_scope"]},
                "fail_summary": '',
            }
        issue_number = (ctx or {}).get("issue_number") or ''
        issue_id = (ctx or {}).get("issue_id")
        agent_type = token_ctx["agent_type"] or (ctx or {}).get("agent_type") or ''
        skill = (ctx or {}).get("skill") or {}
        fail_summary = (ctx or {}).get("fail_summary") or ''
    else:
        # 用户反馈：带 event_seq 服务端现查上下文
        ctx = _collect_feedback_context(body.event_seq, user) if body.event_seq else None
        repo_key = (ctx and ctx.get("repo_key")) or (body.repo_key or '')
        task_id = (ctx and (ctx.get("task") or {}).get("task_id")) or ''
        issue_number = (ctx and ctx.get("issue_number")) or ''
        issue_id = (ctx and ctx.get("issue_id"))
        agent_type = (ctx and ctx.get("agent_type")) or ''
        fail_summary = (ctx and ctx.get("fail_summary")) or ''
        skill = (ctx and ctx.get("skill")) or {}
        event_seq = body.event_seq or None
    skill_id = skill.get("skill_id")
    skill_name = skill.get("skill_name") or ''
    skill_scope = skill.get("skill_scope") or ''

    engine_info = ''
    if ctx and ctx.get("engine"):
        import json as _json
        try:
            engine_info = _json.dumps(ctx["engine"], ensure_ascii=False)
        except Exception:
            engine_info = ''
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO user_feedback "
                "(user_id, username, category, title, description, page_url, repo_key, "
                "attachment_url, attachment_name, status, event_seq, task_id, issue_id, "
                "issue_number, agent_type, engine_info, fail_summary, source, "
                "skill_id, skill_name, skill_scope, feedback_kind) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?,?,?)",
                (user.get('id') or 0, user.get('username') or '',
                 body.category, body.title, body.description,
                 body.page_url or '', repo_key,
                 body.attachment_url or '', body.attachment_name or '',
                 event_seq, task_id, issue_id, issue_number, agent_type, engine_info,
                 fail_summary, source, skill_id, skill_name, skill_scope, body.feedback_kind or ''))
            db.commit()
            fid = c.lastrowid
        log.info(f"feedback: submitted id={fid} by={user.get('username') or auth_type} "
                 f"source={source} category={body.category} event_seq={event_seq or 0}")
        return {"id": fid, "status": "open", "message": "反馈已提交"}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"feedback submit failed: {e}")
        return {"error": "反馈提交失败，请稍后重试"}


@router.get('/feedback/list', operation_id="feedback_list", summary="List feedback (admin)",
            description="Admin feedback list with status/category/search/source filters (admin-only).",
            tags=["feedback"])
def list_feedback(
    status: str = Query('', description='筛选状态'),
    category: str = Query('', description='筛选类型'),
    search: str = Query('', description='搜索标题/描述'),
    source: str = Query('', description='筛选来源 user/agent'),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_admin),
):
    """反馈列表（仅供 admin 管理台调用，role=admin JWT）。"""
    try:
        with db_lock:
            c = db.cursor()
            conditions = ["is_deleted=0"]
            params = []
            if status:
                conditions.append("status=?")
                params.append(status)
            if category:
                conditions.append("category=?")
                params.append(category)
            if search:
                conditions.append("(title LIKE ? OR description LIKE ?)")
                params.extend([f"%{search}%", f"%{search}%"])
            if source:
                conditions.append("source=?")
                params.append(source)
            where = " WHERE " + " AND ".join(conditions)

            c.execute(f"SELECT COUNT(*) FROM user_feedback{where}", tuple(params))
            total = c.fetchone()[0]

            c.execute(
                f"SELECT id, user_id, username, category, title, description, "
                f"page_url, repo_key, attachment_url, attachment_name, status, priority, "
                f"handler_name, handler_remark, handled_at, created_at, updated_at, "
                f"source, skill_id, skill_name, skill_scope, issue_url, issue_number "
                f"FROM user_feedback{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params + [size, page * size]))
            rows = c.fetchall()

        items = []
        for r in rows:
            items.append({
                "id": r[0], "user_id": r[1], "username": r[2],
                "category": r[3], "title": r[4], "description": r[5],
                "page_url": r[6] or '', "repo_key": r[7] or '',
                "attachment_url": r[8] or '', "attachment_name": r[9] or '',
                "status": r[10], "priority": r[11],
                "handler_name": r[12] or '', "handler_remark": r[13] or '',
                "handled_at": r[14] or '', "created_at": r[15] or '',
                "updated_at": r[16] or '',
                "source": r[17] or 'user', "skill_id": r[18], "skill_name": r[19] or '',
                "skill_scope": r[20] or '', "issue_url": r[21] or '', "issue_number": r[22] or '',
            })

        # 统计
        stats = _get_stats()

        return {"items": items, "total": total, "page": page, "stats": stats}
    except Exception as e:
        log.warning(f"feedback list failed: {e}")
        return {"items": [], "total": 0, "error": str(e)}


@router.put('/feedback/{fid}/status', operation_id="feedback_status",
            summary="Update feedback status (admin)", description="Updates feedback status/priority/handler (admin-only).",
            tags=["feedback"])
def update_feedback_status(fid: int, body: dict, user: dict = Depends(require_admin)):
    """更新反馈状态（仅 admin 管理台调用）。

    handler_name 以 admin JWT 的 username 为权威值（不信任请求体任意值）。
    """
    new_status = body.get("status", "")
    priority = body.get("priority")
    handler_name = user.get("username") or body.get("handler_name", "")
    handler_remark = body.get("handler_remark", "")
    if new_status not in ("open", "in_progress", "resolved", "closed"):
        return {"error": "无效状态"}
    try:
        with db_lock:
            c = db.cursor()
            sets = ["status=?", "handler_name=?", "handler_remark=?"]
            params = [new_status, handler_name, handler_remark]
            if priority is not None:
                sets.append("priority=?")
                params.append(int(priority))
            if new_status in ("resolved", "closed"):
                sets.append("handled_at=?")
                params.append(datetime.now(timezone.utc).isoformat())
            params.append(fid)
            c.execute(
                f"UPDATE user_feedback SET {', '.join(sets)} WHERE id=? AND is_deleted=0",
                tuple(params))
            db.commit()
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "反馈记录不存在"})
        log.info(f"feedback: updated id={fid} status={new_status} by={handler_name}")
        return {"success": True, "message": "状态已更新"}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"feedback update failed: {e}")
        return {"error": "更新失败"}


@router.delete('/feedback/{fid}', operation_id="feedback_delete",
               summary="Delete feedback (own)", description="Soft-deletes own feedback (user-only, ownership 403).",
               tags=["feedback"])
def delete_feedback(fid: int, user: dict = Depends(require_no_session_token)):
    """删除反馈（软删除，仅本人可操作）。

    校验 user_id 归属：非本人返回 403。
    """
    uid = user.get('id') or 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT user_id FROM user_feedback WHERE id=? AND is_deleted=0",
                (fid,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail={"code": 404, "message": "反馈记录不存在"})
            if row[0] != uid:
                raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除他人反馈"})
            c.execute(
                "UPDATE user_feedback SET is_deleted=1, updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), fid))
            db.commit()
        log.info(f"feedback: deleted id={fid} by user={user.get('username')} (uid={uid})")
        return {"success": True, "message": "反馈已删除"}
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"feedback delete failed: {e}")
        return {"error": "删除失败，请稍后重试"}


@router.get('/feedback/stats', operation_id="feedback_stats", summary="Feedback stats (admin)",
            description="Feedback statistics by status/category (admin-only).",
            tags=["feedback"])
def feedback_stats(user: dict = Depends(require_admin)):
    """反馈统计（仅供 admin 管理台调用）。"""
    return _get_stats()


@router.get('/feedback/mine', operation_id="feedback_mine", summary="My feedback list",
            description="Paged feedback submitted by the current user (user-only, tk_ -> 403).",
            tags=["feedback"])
def my_feedback(
    user: dict = Depends(require_no_session_token),
    page: int = Query(0, ge=0),
    size: int = Query(10, ge=1, le=100),
):
    """查看当前用户提交的反馈列表（分页）。"""
    uid = user.get('id') or 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM user_feedback WHERE user_id=? AND is_deleted=0", (uid,))
            total = c.fetchone()[0]
            c.execute(
                "SELECT id, category, title, description, attachment_url, attachment_name, "
                "status, priority, handler_name, handler_remark, created_at, updated_at, "
                "source, skill_name, issue_url, issue_number "
                "FROM user_feedback WHERE user_id=? AND is_deleted=0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (uid, size, page * size))
            rows = c.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0], "category": r[1], "title": r[2], "description": r[3],
                "attachment_url": r[4] or '', "attachment_name": r[5] or '',
                "status": r[6], "priority": r[7],
                "handler_name": r[8] or '', "handler_remark": r[9] or '',
                "created_at": r[10] or '', "updated_at": r[11] or '',
                "source": r[12] or 'user', "skill_name": r[13] or '',
                "issue_url": r[14] or '', "issue_number": r[15] or '',
            })
        return {"items": items, "total": total, "page": page,
                "total_pages": max(1, (total + size - 1) // size)}
    except Exception as e:
        log.warning(f"feedback mine failed: {e}")
        return {"items": [], "total": 0, "error": str(e)}


def _get_stats():
    """查询反馈统计数据。"""
    try:
        with db_lock:
            c = db.cursor()
            result = {"byStatus": {}, "byCategory": {}}
            for st in ("open", "in_progress", "resolved", "closed"):
                c.execute("SELECT COUNT(*) FROM user_feedback WHERE status=? AND is_deleted=0", (st,))
                result["byStatus"][st] = c.fetchone()[0]
            result["byStatus"]["total"] = sum(result["byStatus"].values())
            for cat in ("bug", "suggestion", "question", "other"):
                c.execute("SELECT COUNT(*) FROM user_feedback WHERE category=? AND is_deleted=0", (cat,))
                result["byCategory"][cat] = c.fetchone()[0]
            result["byCategory"]["total"] = sum(result["byCategory"].values())
            # Agent 来源统计（weekly-08：devops agent 意见反馈可见性）
            try:
                c.execute("SELECT COUNT(*) FROM user_feedback WHERE source='agent' AND is_deleted=0")
                result["bySource"] = {"agent": c.fetchone()[0], "user": 0}
                c.execute("SELECT COUNT(*) FROM user_feedback WHERE source='user' AND is_deleted=0")
                result["bySource"]["user"] = c.fetchone()[0]
            except Exception:
                result["bySource"] = {}
            return result
    except Exception as e:
        log.warning(f"feedback stats failed: {e}")
        return {"byStatus": {}, "byCategory": {}}


@router.post('/feedback/upload', operation_id="feedback_upload",
             summary="Upload feedback attachment", description="Uploads a feedback attachment (image or zip, <=5MB), user-only.",
             tags=["feedback"])
async def upload_feedback_attachment(file: UploadFile = File(...),
                                     user: dict = Depends(require_no_session_token)):
    """上传反馈附件（图片或 zip 文件）。

    OBS 模式下存到 OBS 桶（feedback/ 目录），返回签名 URL。
    非 OBS 模式下存到本地 static/uploads/feedback/，返回本地路径。
    """
    # 校验文件类型：图片 + zip
    allowed_types = {
        "image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml",
        "application/zip", "application/x-zip-compressed", "application/octet-stream",
    }
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".zip"}
    content_type = file.content_type or ""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持 jpg/png/webp/gif/svg/zip 格式"})
    if not ext:
        ext = ".png"

    # 读取并校验大小（5MB）
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "文件不能超过 5MB"})

    # 生成唯一 key
    file_key = f"feedback/{uuid.uuid4().hex[:16]}{ext}"

    from src.storage import get_storage
    storage = get_storage()
    if storage:
        # OBS 模式：存到 OBS 桶
        storage.save_bytes(file_key, content)
        url = storage.temporary_url(file_key, expires=3600 * 24 * 7)  # 7 天有效
        log.info(f"feedback upload: OBS key={file_key} size={len(content)} by={user.get('username')}")
    else:
        # 非 OBS 模式：存到本地
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "feedback")
        os.makedirs(static_dir, exist_ok=True)
        local_name = file_key.replace("feedback/", "")
        local_path = os.path.join(static_dir, local_name)
        with open(local_path, "wb") as f:
            f.write(content)
        url = f"/static/uploads/feedback/{local_name}"
        log.info(f"feedback upload: local path={url} size={len(content)} by={user.get('username')}")

    return {"url": url, "file_key": file_key, "is_zip": ext == ".zip", "original_name": filename}