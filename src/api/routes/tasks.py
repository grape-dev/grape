"""Tasks 路由：个人执行实例动作（notify / complete / pre-upload）

- notify: agent 分配/换人后调用（会话 token 认证）→ 建任务 + 换人刷新 + Web conversation + 静态模板消息 + 飞书通知
          （§十：不得触发后台 ChatAgent / 不得注入用户个人 token 执行 opencode）
- complete: 某次执行实例完成（agent_tasks 收尾）
- pre-upload: 创建需求前临时附件（Web 会话）
- close / mine / 用户操作类均已移除或迁 /api/issues（见 tasks-issues-api-rebound-design.md）
"""
import asyncio
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from src.api.deps import get_current_user_or_api_key, require_no_session_token, get_user_token
from src.config import log, WORKSPACE_DIR
from src.api.time_utils import normalize_event_time
from src.session_manager import (
    db, db_lock, get_chat_config, create_conversation, add_message,
)



# ===== 需求中心领域逻辑已迁移至 src/api/issues_center（re-export 过渡，tasks-module-refactor）=====
from src.api.issues_center import (  # noqa: F401
    TaskCloseBody,
    TaskConfirmBody,
    TaskCreateBody,
    TaskNotifyBody,
    TaskStatusBody,
    _ATTACH_ALL_EXTS,
    _ATTACH_ARCHIVE_EXTS,
    _ATTACH_CODE_EXTS,
    _ATTACH_DOC_EXTS,
    _ATTACH_IMAGE_EXTS,
    _ATTACH_MAX_SIZE,
    _TASK_STATUS_FLOWS,
    _attach_base_dir,
    _attach_cleanup_dir,
    _attach_delete,
    _attach_list,
    _attach_obs_key,
    _attach_save,
    _batch_fetch_agent_statuses,
    _batch_fetch_issue_state,
    _batch_running_agent_tasks,
    _build_assignee_filter,
    _build_task_context_message,
    _cleanup_issue_attachments,
    _close_issue_tasks_bot_takeover,
    _close_other_assignee_tasks,
    _ensure_assignee_task_card,
    _ensure_task_conversation,
    _find_agent_task_ids,
    _find_open_issue_by_title,
    _get_feishu_open_id,
    _get_issue_parent,
    _get_issue_parent_id,
    _get_issue_subtask_mode,
    _get_issue_title,
    _get_task_issue_info,
    _get_user_id_by_gitcode,
    _gitcode_api_with_refresh,
    _is_gitcode_401,
    _patch_github_issue_body,
    _patch_issue_body,
    _post_gitcode_issue,
    _post_github_issue,
    _query_tasks,
    _require_session_repo_token,
    _resolve_user_chat_bot,
    _run_notify,
    cancel_issue_task,
    close_issue_tasks,
    confirm_issue_done,
    create_standard_task,
    create_task_via_issue,
    ensure_issue_task,
    resume_issue_task,
)
# ===================================================================

router = APIRouter(prefix="/api/tasks", tags=["tasks"])































































































































































































































































































































































































































































































@router.post("/notify", operation_id="tasks_notify", summary="Task assign/notify (Agent runtime)", description="Called by issue agents after assign/reassign/takeover: creates a task, refreshes the assignee, writes the conversation and sends static template + Feishu notifications. Session-only (tk_), repo scope must match.")
async def notify_task(body: TaskNotifyBody, request: Request,
                      user: dict = Depends(get_current_user_or_api_key)):
    """issue agent 分配/换人后调用：建任务 + 换人刷新 + Web conversation + 静态模板通知 + 飞书通知（不触发 ChatAgent）

    - **仅会话通道**：`get_current_user_or_api_key` 让本端点进契约（CLI 可见），但体内
      `_require_session_repo_token` 强制 `tk_` + 仓库作用域；`grape_` API Key / JWT → 403。
    - x-token-scope：session_only（见 openapi._SESSION_ONLY_OPERATIONS）。
    """
    from src.repo_key import to_repo_key
    if not body.assignee or not body.repo_key or not body.issue_number:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "assignee / repo_key / issue_number 必填"})
    body.repo_key = to_repo_key(body.repo_key)
    _session = _require_session_repo_token(request, body.repo_key)
    _agent_type = _session.get("agent_type", "") or "issue"

    # repo 已托管（repo_key 带前缀）
    from src.user.db import get_all_managed_repos
    managed = {r["repo_key"] for r in get_all_managed_repos()}
    if body.repo_key not in managed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未托管"})

    # 责任人若是 bot（grape_bot）→ 视为"接管"：关闭该 issue 用户 active/processing 任务（bot_takeover），
    # 不建 bot 任务。见 task-lifecycle-design.md §2.3/§3.1。
    from src.user.db import get_all_bot_usernames
    if body.assignee in get_all_bot_usernames():
        closed = _close_issue_tasks_bot_takeover(body.repo_key, body.issue_number)
        log.info(f"Task notify: bot_takeover assignee={body.assignee} repo={body.repo_key} issue=#{body.issue_number} closed={closed} (task_id=0)")
        return {"ok": True, "issue_id": 0, "task_id": 0, "conversation_id": "",
                "message": f"任务已由 {body.assignee} 接管" + (f"（关闭 {closed} 条待处理任务）" if closed else "（无待处理任务）")}

    # 解析 user_id
    user_id = _get_user_id_by_gitcode(body.assignee)
    if not user_id:
        return {"ok": False, "code": 400, "message": f"责任人 {body.assignee} 未注册平台，无法创建任务"}

    # 换人刷新 + 创建/复用任务
    _close_other_assignee_tasks(body.repo_key, body.issue_number, body.assignee)
    title = _get_issue_title(body.repo_key, body.issue_number)
    task_id = _ensure_assignee_task_card(user_id, body.assignee, body.repo_key, body.issue_number,
                                         title, body.task_description)
    if not task_id:
        return {"ok": False, "code": 500, "message": "任务创建失败"}
    log.info(f"Task notify: assignee={body.assignee} repo={body.repo_key} issue=#{body.issue_number} task_id={task_id}")

    # 创建/复用 Web conversation（不带 im_channel_id，Chat Agent 页可见）——
    # 复用 _ensure_task_conversation（与 ensure_issue_task 同源，notify 语义：每次分配重刷上下文）
    conv_id = _ensure_task_conversation(user_id, body.repo_key, body.issue_number,
                                        title, body.task_description, write_context_on_reuse=True)
    if conv_id:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, task_id))
            db.commit()

    # 后台仅静态通知（不触发 ChatAgent，§十；立即返回）
    open_id = _get_feishu_open_id(body.assignee)
    if not open_id:
        log.warning(f"Task notify: assignee={body.assignee} 无飞书 open_id（unified_user_mapping.feishu_open_id 为空），跳过飞书通知 task_id={task_id} issue=#{body.issue_number}")
    cfg = get_chat_config(user_id) if user_id else {"backend": "opencode"}
    from src.api import main as api_main
    bm = api_main.bot_manager
    # open_id 为 app 级（feishu-app-scoped-openid-fix.md）：必须用**用户本人 chat bot** 发送，
    # 否则命名空间不匹配 → send_card_to_user 静默失败。用户未配置本人 bot → 不通知（不回落其它 bot）。
    ctx = _resolve_user_chat_bot(bm, user_id) if bm else None
    if not ctx:
        log.warning(f"Task notify: assignee={body.assignee} 未配置本人 chat bot，跳过飞书通知 task_id={task_id} issue=#{body.issue_number}")
    api = bm.api if bm else None
    asyncio.create_task(_run_notify(ctx, api, user_id, conv_id, open_id,
                                    body.task_description, body.repo_key, body.issue_number, cfg))

    return {"ok": True, "issue_id": task_id, "task_id": task_id, "conversation_id": conv_id,
            "message": "任务已创建并通知责任人"}




































# 任务状态迁移规则（Issue #85: 增加 awaiting_input + pending_review）










# Issue #51: 任务确认完成 / 取消 / 恢复



















































































































































































































































































































































































































































# ====== 附件管理 ======

# 允许的附件文件扩展名

















































































































class TaskSelfCompleteBody(BaseModel):
    task_id: str = ""
    result_text: str = ""
    next_step: str = ""
    # 结构化产出（可选）：服务端据此组装结构化 result_text，供下游事件对话/结构化解析识别
    actions: list = []
    conclusion: str = ""
    artifacts: list = []
    issue_summary: str = ""


@router.post("/complete", operation_id="tasks_complete", summary="Self-report task complete", description="Agent self-reports completion of an execution instance. With a session token the task id is derived from session_key; JWT/API key requires task_id. Structured fields build a structured result_text.")
def self_complete_task(body: TaskSelfCompleteBody, user: dict = Depends(get_current_user_or_api_key)):
    """Agent 动作收尾后显式自报完成（issue-45-task-complete-endpoint-design.md）。

    - 会话 token（tk_）：任务 id 由 session_key 自动定位（M2 以 task_id 签发），校验 repo 作用域。
    - JWT / API Key（用户级）：body.task_id 必填，校验任务 repo 在该用户托管仓库内。
    - 终态幂等：completed/failed/cancelled 不再覆盖（返回现状）。
    - 完成后任务 + 事件置为成功，不经过 evaluate_agent_output 启发。
    """
    from src.runtime.service import complete_task
    from src.user import db as _user_db

    if user.get("auth_type") == "session_token":
        task_id = user.get("session_key") or ""
    elif body.task_id:
        task_id = str(body.task_id).strip()
    else:
        task_id = ""
    if not task_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "缺少 task_id（会话 token 自动定位或 body.task_id 必填）"})

    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, repo_key, status FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    _repo, _status = row[1], row[2]

    if user.get("auth_type") == "session_token":
        if user.get("session_key") != task_id or user.get("repo_key") != _repo:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权完成该任务（作用域不匹配）"})
    else:
        managed = {r.get("repo_key") for r in _user_db.get_managed_repos(user.get("id") or 0)}
        if _repo not in managed:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权完成该任务（非本人托管仓库）"})

    if _status in ("completed", "failed", "cancelled"):
        return {"ok": True, "task_id": task_id, "status": _status, "already": True}

    result_text = (body.result_text or "").strip() or f"任务自报完成{(' / ' + body.next_step) if body.next_step else ''}"
    # 结构化产出（可选）：有 actions/conclusion/artifacts/issue_summary 时组装为结构化 JSON 文本，
    # 使 complete_task 的 structured output 解析（actions+conclusion）能识别，供事件对话/下游读取。
    if body.conclusion or body.actions or body.artifacts or body.issue_summary:
        import json as _json
        _sd = {
            "actions": body.actions or [],
            "conclusion": body.conclusion or result_text,
            "artifacts": body.artifacts or [],
            "next_step": body.next_step,
            "issue_summary": body.issue_summary or result_text,
        }
        result_text = _json.dumps(_sd, ensure_ascii=False)
    complete_task(task_id, result_text, 0)
    return {"ok": True, "task_id": task_id, "status": "completed"}


@router.get("/current", operation_id="tasks_current", summary="Get current task detail (Agent runtime)", description="Agent runtime introspection: returns the current execution instance (task + issue + events) for this session token. Session-only (tk_); API key/JWT -> 403.")
def get_current_task(user: dict = Depends(get_current_user_or_api_key)):
    """Agent 执行期自省接口：通过本次会话 token（tk_）获取当前执行实例详情。

    - **仅会话通道**：`auth_type` 必须为 `session_token`（执行期注入 GRAPE_SESSION_TOKEN）；
      `grape_` API Key / JWT → 403（本接口属 Agent 执行期内网自省，不由用户侧调用）。
    - 定位：`session_key`（= agent_tasks.id）自动派生，客户端零入参。
    - 返回：task（执行实例）+ issue（关联需求）+ events 计数/最新事件；**不含 prompt**。
    """
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅会话 token（tk_）可调用（Agent 执行期自省）"})
    task_id = user.get("session_key") or ""
    if not task_id:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在（会话未绑定 task）"})
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, repo_key, agent_type, skill, event_seq, issue_id, status, "
            "result_text, errors, exit_code, started_at, completed_at, created_at "
            "FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    _repo = row[1]
    if user.get("repo_key") != _repo:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该任务（仓库作用域不匹配）"})

    task = {
        "id": row[0], "status": row[6], "agent_type": row[2] or "",
        "skill": row[3] or "", "event_seq": row[4],
        "result_text": row[7] or "", "errors": row[8] or "",
        "exit_code": row[9],
        "started_at": row[10], "completed_at": row[11], "created_at": row[12],
        "repo_key": _repo,
    }

    # 关联需求（repo_key + issue_id 去 '#'）
    import re as _re
    _num = _re.sub(r"[^0-9]", "", str(row[5] or ""))
    task["issue"] = None
    task["events_count"] = 0
    task["latest_event"] = None
    if _num:
        try:
            from src.api.routes.issues import _group_events_by_issue
            group = _group_events_by_issue(_repo)
            evs = group.get(_num) or []
            task["events_count"] = len(evs)
            task["latest_event"] = evs[-1] if evs else None
            from src.api.session_store import get_issue_stage
            st = get_issue_stage(_repo, f"#{_num}")
            _host = "github.com" if _repo.startswith("github:") else "gitcode.com"
            from src.repo_key import split_owner_repo
            _owner, _rname = split_owner_repo(_repo)
            with db_lock:
                c2 = db.cursor()
                c2.execute("SELECT id, issue_number, title, assignee, status "
                           "FROM issue WHERE repo_key=? AND issue_number=?", (_repo, int(_num)))
                ir = c2.fetchone()
            task["issue"] = {
                "id": ir[0] if ir else None,
                "issue_number": ir[1] if ir else int(_num),
                "title": ir[2] if ir else "",
                "assignee": ir[3] if ir else (st or {}).get("assignee", ""),
                "stage": (st or {}).get("stage", "vod"),
                "status": ir[4] if ir else "",
                "scene_id": (st or {}).get("scene_id", ""),
                "url": f"https://{_host}/{_owner}/{_rname}/issues/{_num}",
            }
        except Exception as _e:
            from src.config import log
            log.warning(f"tasks/current: enrich issue failed for {task_id}: {_e}")
    return {"task": task}


class TaskClarifyBody(BaseModel):
    message: str = ""



































def _require_repo_access(user: dict, repo_key: str):
    """校验调用方可访问指定仓库（防 IDOR，Issue #113 审查修复）。

    - 用户级（JWT / API Key）：repo_key 必须在该用户托管仓库（get_managed_repos）内
    - 会话 token（tk_）：repo_key 必须匹配 token 作用域（session_key 对应仓库）
    """
    if user.get("auth_type") == "session_token":
        if user.get("repo_key") != repo_key:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该仓库任务（作用域不匹配）"})
        return
    from src.user import db as _user_db
    _managed = {r.get("repo_key") for r in _user_db.get_managed_repos(user.get("id") or 0)}
    if repo_key not in _managed:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该仓库任务"})































































































































































































