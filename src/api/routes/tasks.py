"""Tasks 路由：个人任务中心（notify / close / mine）

- notify: issue agent 分配/换人后调用 → 建任务 + 换人刷新 + Web conversation + 后台 chat-agent + 飞书通知
- close:  issue agent 关闭 Issue 后调用 → 该 issue 全部 active 任务完成（issue_closed）
- mine:   任务中心 → 当前登录用户的待处理/已完成任务
"""
import asyncio
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import WORKSPACE_DIR, log
from src.api.time_utils import normalize_event_time
from src.session_manager import (
    db, db_lock, get_chat_config, create_conversation, add_message,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _require_bot_token(request: Request, repo_key: str):
    """仓库级 bot token 鉴权（Agent 调用时带 Authorization: Bearer <BOT_GITCODE_TOKEN>）。

    复用仓库级 bot token 机制（对齐 webhook 鉴权思路），支持远端 worker 调用。
    仅当仓库配置了 bot token（managed_repos.bot_gitcode_token 或 devops_agent_session.repo_gitcode_token，
    不含全局兜底）时严格校验；未配置 token 的仓库放行，交由后续托管校验返回 404。
    """
    from src.user.db import get_repo_bot_token_no_global
    _expected = get_repo_bot_token_no_global(repo_key)
    if not _expected:
        return  # 仓库未配置 bot token → 交给后续校验
    _auth = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not _auth:
        _auth = request.headers.get("X-Bot-Token", "") or ""
    if _auth != _expected:
        log.warning(f"Tasks {request.url.path}: unauthorized (token mismatch) for repo={repo_key}")
        raise HTTPException(status_code=403, detail={"code": 403, "message": "invalid bot token"})


class TaskNotifyBody(BaseModel):
    assignee: str = ""
    repo_key: str = ""
    issue_number: int = 0
    task_description: str = ""


class TaskCloseBody(BaseModel):
    repo_key: str = ""
    issue_number: int = 0


def _get_user_id_by_gitcode(gitcode_user: str) -> int | None:
    """按 GitCode 用户名查平台 user_id（unified_user_mapping.user_id）"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT user_id FROM unified_user_mapping WHERE gitcode_user=? AND user_id>0 LIMIT 1", (gitcode_user,))
            row = c.fetchone()
            return row[0] if row else None
        except Exception:
            return None


def _get_feishu_open_id(gitcode_user: str) -> str:
    """按 GitCode 用户名查飞书 open_id"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=? AND feishu_open_id!='' LIMIT 1", (gitcode_user,))
            row = c.fetchone()
            return row[0] if row else ""
        except Exception:
            return ""


def _create_task(user_id: int, assignee: str, repo_key: str, issue_number: int,
                 title: str, task_description: str) -> int | None:
    """创建 active 任务；同 assignee 同 issue 已有 active 则复用（返回其 id）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM tasks WHERE repo_key=? AND issue_number=? AND assignee=? AND status='active' LIMIT 1",
            (repo_key, issue_number, assignee)
        )
        row = c.fetchone()
        if row:
            return row[0]
        c.execute(
            "INSERT INTO tasks (user_id, assignee, repo_key, issue_number, title, task_description, status) VALUES (?,?,?,?,?,?,'active')",
            (user_id, assignee, repo_key, issue_number, title, task_description)
        )
        db.commit()
        return c.lastrowid


def _close_other_assignee_tasks(repo_key: str, issue_number: int, keep_assignee: str) -> int:
    """换人：将同 issue 其他 assignee 的 active/processing 任务置为 completed（reassigned）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='reassigned', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing') AND assignee<>?",
            (repo_key, issue_number, keep_assignee)
        )
        db.commit()
        return c.rowcount


def _close_issue_tasks_bot_takeover(repo_key: str, issue_number: int) -> int:
    """bot 接管：将同 issue 所有 active/processing 任务置 completed（bot_takeover）。

    见 task-lifecycle-design.md §2.3：issue agent 将 assignee 刷为 grape_bot 表示自己接管，
    用户任务无需再处理，全部关闭。不建 bot 任务。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='bot_takeover', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (repo_key, issue_number))
        db.commit()
        return c.rowcount


def _get_issue_title(repo_key: str, issue_number: int) -> str:
    """从 events 表取该 issue 最新事件的标题（零 API 调用）"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT title FROM events WHERE repo_key=? AND issue_id=? AND title!='' "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, f"#{issue_number}")
            )
            row = c.fetchone()
            return row[0] if row else ""
        except Exception:
            return ""


def _build_task_context_message(title: str, task_description: str, repo_key: str, issue_number: int) -> str:
    """构建写入会话的任务上下文消息（用户侧可见）"""
    lines = [f"📋 任务分配 · Issue #{issue_number}"]
    lines.append(f"仓库: {repo_key}")
    if title:
        lines.append(f"Issue 标题: {title}")
    lines.append("")
    lines.append(f"任务描述: {task_description}")
    lines.append("")
    lines.append("请在下方与我沟通处理进度，或提出疑问。")
    return "\n".join(lines)


def _build_task_prompt(task_description: str, repo_key: str, issue_number: int, user_id: int) -> str:
    """构建任务 prompt：issue 上下文 + 任务描述"""
    from src.prompt_builder import build_chat_prompt
    title = _get_issue_title(repo_key, issue_number)
    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key)
    except Exception:
        issue_summary = ""
    ctx = f"## 任务 Issue\n仓库: {repo_key}\nIssue: #{issue_number}"
    if title:
        ctx += f"\n标题: {title}"
    if issue_summary:
        ctx += f"\n\n{issue_summary}"
    ctx += f"\n\n## 任务描述\n{task_description}"
    from src.repo_key import split_owner_repo
    _owner, _repo = split_owner_repo(repo_key)
    return build_chat_prompt(ctx, repo_key, [], ["chat"], account=_owner)


async def _run_notify(ctx, api, user_id: int, conv_id: str, open_id: str,
                      task_description: str, repo_key: str, issue_number: int, cfg: dict):
    """后台执行 chat-agent：构建 prompt → consume_stream_async → 记录消息 → 飞书通知"""
    try:
        user_token = ""
        try:
            from src.user.db import get_user_gitcode_token
            user_token = get_user_gitcode_token(user_id) or ""
        except Exception:
            pass
        prompt = _build_task_prompt(task_description, repo_key, issue_number, user_id)
        cwd = os.path.join(WORKSPACE_DIR, "chat", str(user_id), conv_id)
        os.makedirs(cwd, exist_ok=True)

        final_text = ""
        async for evt_type, data in _consume(prompt, cwd, cfg, user_id, user_token, repo_key):
            if evt_type == "done":
                final_text = data.get("text", "") if isinstance(data, dict) else ""

        # 预写入已含完整任务上下文（含 task_description），此处仅追加 agent 回复，
        # 不再重复写 user 任务描述（见 task-conversation-role-fix.md §3.2）
        if final_text:
            add_message(conv_id, "agent", final_text)

        # 飞书仅作通知（D3）
        if open_id and api and ctx:
            from src.repo_key import from_repo_key
            notify_text = (f"📋 你有新的任务\n\n"
                           f"仓库: {from_repo_key(repo_key)}\nIssue: #{issue_number}\n\n"
                           f"{task_description}\n\n"
                           f"请在任务中心查看并处理。")
            try:
                await api.send_card_to_user(ctx, open_id, notify_text, title="任务通知")
            except Exception as e:
                log.warning(f"Task notify feishu failed: {e}")
    except Exception as e:
        log.warning(f"Task notify _run_notify error: {e}")


async def _consume(prompt: str, cwd: str, cfg: dict, user_id: int, user_token: str, repo_key: str):
    """async 桥接 consume_stream_async（在线程池消费 stream_agent，不阻塞事件循环）"""
    from src.runtime.executor import consume_stream_async
    from src.repo_key import split_owner_repo
    backend = cfg.get("backend", "opencode")
    _owner, _repo = split_owner_repo(repo_key)
    async for evt_type, data in consume_stream_async(
        prompt, cwd, session_id="",
        backend_name=backend, account=_owner,
        user_id=user_id, user_token=user_token,
        agent_name="Chat Agent", agent_label=f"[Agent:task|repo:{repo_key}]",
    ):
        yield evt_type, data


@router.post("/notify")
async def notify_task(body: TaskNotifyBody, request: Request):
    """issue agent 分配/换人后调用：建任务 + 换人刷新 + Web conversation + 后台 chat-agent + 飞书通知"""
    from src.repo_key import to_repo_key
    if not body.assignee or not body.repo_key or not body.issue_number:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "assignee / repo_key / issue_number 必填"})
    body.repo_key = to_repo_key(body.repo_key)
    _require_bot_token(request, body.repo_key)

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
        return {"ok": True, "task_id": 0, "conversation_id": "",
                "message": f"任务已由 {body.assignee} 接管" + (f"（关闭 {closed} 条待处理任务）" if closed else "（无待处理任务）")}

    # 解析 user_id
    user_id = _get_user_id_by_gitcode(body.assignee)
    if not user_id:
        return {"ok": False, "code": 400, "message": f"责任人 {body.assignee} 未注册平台，无法创建任务"}

    # 换人刷新 + 创建/复用任务
    _close_other_assignee_tasks(body.repo_key, body.issue_number, body.assignee)
    title = _get_issue_title(body.repo_key, body.issue_number)
    task_id = _create_task(user_id, body.assignee, body.repo_key, body.issue_number,
                           title, body.task_description)
    if not task_id:
        return {"ok": False, "code": 500, "message": "任务创建失败"}
    log.info(f"Task notify: assignee={body.assignee} repo={body.repo_key} issue=#{body.issue_number} task_id={task_id}")

    # 创建/复用 Web conversation（不带 im_channel_id，Chat Agent 页可见）
    from src.session_manager import get_user_conversations
    conv_id = ""
    try:
        convs = get_user_conversations(user_id, repo_key=body.repo_key, limit=50)
        for c in convs:
            if c.get("title") == f"Issue #{body.issue_number}":
                conv_id = c["id"]
                break
        if not conv_id:
            conv_id = create_conversation(user_id, body.repo_key, title=f"Issue #{body.issue_number}",
                                          conv_type="task", issue_number=body.issue_number)
        else:
            # 复用存量「Issue #N」会话时补任务标记（conv_type/issue_number）
            from src.session_manager import update_conversation
            update_conversation(conv_id, conv_type="task", issue_number=body.issue_number)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE tasks SET conversation_id=? WHERE id=?", (conv_id, task_id))
            db.commit()
        # 立即写入任务上下文消息（不等后台 agent，保证会话非空、去对话可见上下文）
        # role=agent：任务由 grape_bot 下发，UI 显示在 agent 侧，等待用户答复确认
        #（见 task-conversation-role-fix.md §3.1）
        try:
            ctx_msg = _build_task_context_message(title, body.task_description, body.repo_key, body.issue_number)
            add_message(conv_id, "agent", ctx_msg)
        except Exception as e:
            log.warning(f"Task notify write context message failed: {e}")
    except Exception as e:
        log.warning(f"Task notify conversation create failed: {e}")

    # 后台执行 chat-agent（立即返回）
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

    return {"ok": True, "task_id": task_id, "conversation_id": conv_id,
            "message": "任务已创建并通知责任人"}


def _resolve_user_chat_bot(bm, user_id: int):
    """按用户 chat_agent_session.im_app_id 取其本人 chat bot（open_id 为 app 级，须用本人 bot 发送）。

    用户未配置本人 chat bot → 返回 None（**不通知**，不回落其它 bot，避免用他人 app 发送静默失败）。
    """
    if not bm:
        return None
    try:
        bots = bm.get_all_bots()
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT im_app_id FROM chat_agent_session WHERE user_id=? AND im_app_id!='' LIMIT 1",
                (user_id,)).fetchone()
        if row and row[0] in bots:
            return bots[row[0]]
    except Exception:
        pass
    return None


@router.post("/{task_id}/start")
async def start_task(task_id: int, user: dict = Depends(get_current_user)):
    """打开任务会话（"去对话"）——不再翻转状态。

    task-status-processing-trigger-fix.md §3.3：状态变更以"任务会话收到真实用户消息"为准
    （mark_task_processing_by_conversation），本端点仅校验归属并返回当前状态。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, status FROM tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
        if row[0] != user["id"]:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该任务"})
        return {"status": row[1]}


@router.post("/close")
async def close_tasks(body: TaskCloseBody, request: Request):
    """issue agent 关闭 Issue 后调用：该 issue 全部 active 任务 → completed（issue_closed）"""
    from src.repo_key import to_repo_key
    if not body.repo_key or not body.issue_number:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo_key / issue_number 必填"})
    body.repo_key = to_repo_key(body.repo_key)
    _require_bot_token(request, body.repo_key)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='issue_closed', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (body.repo_key, body.issue_number)
        )
        db.commit()
        closed = c.rowcount
    return {"ok": True, "closed": closed}


@router.get("/mine")
async def my_tasks(status: str = Query("", pattern="^(active|completed|)?$"),
                   repo_key: str = Query("", description="按仓库过滤"),
                   user: dict = Depends(get_current_user)):
    """任务中心：当前登录用户的待处理/已完成任务（可按 repo 过滤）"""
    from src.repo_key import to_repo_key, from_repo_key
    if repo_key:
        repo_key = to_repo_key(repo_key)
    clauses = ["user_id=?"]
    params = [user["id"]]
    if status:
        clauses.append("status=?")
        params.append(status)
    if repo_key:
        clauses.append("repo_key=?")
        params.append(repo_key)
    where = " AND ".join(clauses)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT id, assignee, repo_key, issue_number, title, task_description, conversation_id, "
                f"status, completed_reason, created_at, completed_at FROM tasks WHERE {where} "
                f"ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, created_at DESC",
                params
            )
            rows = c.fetchall()
        except Exception:
            rows = []
    return {"tasks": [{
        "id": r[0], "assignee": r[1], "repo_key": from_repo_key(r[2]), "issue_number": r[3],
        "title": r[4] or "", "task_description": r[5] or "",
        "conversation_id": r[6] or "", "status": r[7],
        "completed_reason": r[8] or "", "created_at": normalize_event_time(r[9]), "completed_at": normalize_event_time(r[10]),
    } for r in rows]}
