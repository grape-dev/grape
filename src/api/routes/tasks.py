"""Tasks 路由：个人任务中心（notify / close / mine）

- notify: issue agent 分配/换人后调用 → 建任务 + 换人刷新 + Web conversation + 后台 chat-agent + 飞书通知
- close:  issue agent 关闭 Issue 后调用 → 该 issue 全部 active 任务完成（issue_closed）
- mine:   任务中心 → 当前登录用户的待处理/已完成任务
"""
import asyncio
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import WORKSPACE_DIR, log
from src.session_manager import (
    db, db_lock, get_chat_config, create_conversation, add_message,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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
    """换人：将同 issue 其他 assignee 的 active 任务置为 completed（reassigned）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='reassigned', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status='active' AND assignee<>?",
            (repo_key, issue_number, keep_assignee)
        )
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
    return build_chat_prompt(ctx, repo_key, [], ["chat"], account=repo_key.split("/", 1)[0] if "/" in repo_key else "")


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

        add_message(conv_id, "user", task_description)
        if final_text:
            add_message(conv_id, "agent", final_text)

        # 飞书仅作通知（D3）
        if open_id and api and ctx:
            notify_text = (f"📋 你有新的任务\n\n"
                           f"仓库: {repo_key}\nIssue: #{issue_number}\n\n"
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
    from src.agent.executor import consume_stream_async
    backend = cfg.get("backend", "opencode")
    async for evt_type, data in consume_stream_async(
        prompt, cwd, session_id="",
        backend_name=backend, account=repo_key.split("/", 1)[0] if "/" in repo_key else "",
        user_id=user_id, user_token=user_token,
        agent_name="Chat Agent", agent_label=f"[Agent:task|repo:{repo_key}]",
    ):
        yield evt_type, data


@router.post("/notify")
async def notify_task(body: TaskNotifyBody):
    """issue agent 分配/换人后调用：建任务 + 换人刷新 + Web conversation + 后台 chat-agent + 飞书通知"""
    if not body.assignee or not body.repo_key or not body.issue_number:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "assignee / repo_key / issue_number 必填"})

    # repo 已托管
    from src.user.db import get_all_managed_repos
    managed = {r["full_name"] for r in get_all_managed_repos()}
    if body.repo_key not in managed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未托管"})

    # 责任人必须是"人"而非 bot 账号（避免给 gitcode_bot 建任务）
    from src.user.db import get_all_bot_usernames
    if body.assignee in get_all_bot_usernames():
        return {"ok": False, "code": 400, "message": f"责任人 {body.assignee} 是 bot 账号，不创建任务"}

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
            conv_id = create_conversation(user_id, body.repo_key, title=f"Issue #{body.issue_number}")
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE tasks SET conversation_id=? WHERE id=?", (conv_id, task_id))
            db.commit()
        # 立即写入任务上下文消息（不等后台 agent，保证会话非空、去对话可见上下文）
        try:
            ctx_msg = _build_task_context_message(title, body.task_description, body.repo_key, body.issue_number)
            add_message(conv_id, "user", ctx_msg)
        except Exception as e:
            log.warning(f"Task notify write context message failed: {e}")
    except Exception as e:
        log.warning(f"Task notify conversation create failed: {e}")

    # 后台执行 chat-agent（立即返回）
    open_id = _get_feishu_open_id(body.assignee)
    cfg = get_chat_config(user_id) if user_id else {"backend": "opencode"}
    from src.api import main as api_main
    bm = api_main.bot_manager
    ctx = next((b for b in bm.get_all_bots().values() if getattr(b, "bot_type", "") == "chat_bot"), None) if bm else None
    api = bm.api if bm else None
    asyncio.create_task(_run_notify(ctx, api, user_id, conv_id, open_id,
                                    body.task_description, body.repo_key, body.issue_number, cfg))

    return {"ok": True, "task_id": task_id, "conversation_id": conv_id,
            "message": "任务已创建并通知责任人"}


@router.post("/close")
async def close_tasks(body: TaskCloseBody):
    """issue agent 关闭 Issue 后调用：该 issue 全部 active 任务 → completed（issue_closed）"""
    if not body.repo_key or not body.issue_number:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo_key / issue_number 必填"})
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='issue_closed', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status='active'",
            (body.repo_key, body.issue_number)
        )
        db.commit()
        closed = c.rowcount
    return {"ok": True, "closed": closed}


@router.get("/mine")
async def my_tasks(status: str = Query("", pattern="^(active|completed|)?$"),
                   user: dict = Depends(get_current_user)):
    """任务中心：当前登录用户的待处理/已完成任务"""
    clauses = ["user_id=?"]
    params = [user["id"]]
    if status:
        clauses.append("status=?")
        params.append(status)
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
        "id": r[0], "assignee": r[1], "repo_key": r[2], "issue_number": r[3],
        "title": r[4] or "", "task_description": r[5] or "",
        "conversation_id": r[6] or "", "status": r[7],
        "completed_reason": r[8] or "", "created_at": r[9], "completed_at": r[10],
    } for r in rows]}
