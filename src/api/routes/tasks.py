"""Tasks 路由：个人任务中心（notify / close / mine）

- notify: issue agent 分配/换人后调用 → 建任务 + 换人刷新 + Web conversation + 静态模板消息 + 飞书通知
          （§十：不得触发后台 ChatAgent / 不得注入用户个人 token 执行 opencode）
- close:  issue agent 关闭 Issue 后调用 → 该 issue 全部 active 任务完成（issue_closed）
- mine:   任务中心 → 当前登录用户的待处理/已完成任务
"""
import asyncio
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from src.api.deps import get_current_user, get_user_token
from src.config import log
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
            "INSERT INTO tasks (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number) "
            "VALUES (?,?,?,?,?,?,'active',?,?)",
            (user_id, assignee, repo_key, issue_number, title, task_description,
             _get_issue_subtask_mode(repo_key, issue_number), _get_issue_parent(repo_key, issue_number))
        )
        db.commit()
        return c.lastrowid


def _ensure_task_conversation(user_id: int, repo_key: str, issue_number: int,
                              title: str = "", description: str = "",
                              write_context_on_reuse: bool = False) -> str:
    """为任务查找/复用「Issue #N」对话（任务中心「去对话」入口）。

    - 复用：同用户已有该 Issue 对话 → 补 conv_type/issue_number 标记；
      write_context_on_reuse=True 时再次写入上下文消息（notify 语义：每次分配都刷上下文）
    - 新建：create_conversation(conv_type="task") 并写入任务上下文消息。
    - 失败返回空串（不抛异常，旁路不影响任务建档）。
    """
    from src.session_manager import get_user_conversations, create_conversation, update_conversation, add_message
    conv_id = ""
    try:
        convs = get_user_conversations(user_id, repo_key=repo_key, limit=50)
        for conv in convs:
            if conv.get("title") == f"Issue #{issue_number}":
                conv_id = conv["id"]
                break
        if conv_id:
            update_conversation(conv_id, conv_type="task", issue_number=issue_number)
            if write_context_on_reuse:
                ctx_msg = _build_task_context_message(title, description, repo_key, issue_number)
                add_message(conv_id, "agent", ctx_msg)
        else:
            conv_id = create_conversation(user_id, repo_key, title=f"Issue #{issue_number}",
                                          conv_type="task", issue_number=issue_number)
            try:
                ctx_msg = _build_task_context_message(title, description, repo_key, issue_number)
                add_message(conv_id, "agent", ctx_msg)
            except Exception as e:
                log.warning(f"Task conversation write context message failed: {e}")
    except Exception as e:
        log.warning(f"Task conversation ensure failed: {e}")
        return ""
    return conv_id


def ensure_issue_task(repo_key: str, issue_number: int, title: str = "",
                      description: str = "", assignee: str = "",
                      event_type: str = "",
                      subtask_mode: int = -1) -> int | None:
    """Issue 事件幂等建档（任务中心全覆盖，task-center-issue-coverage-design.md §3-A）。

    - assignee 优先 → 对应平台 user_id；assignee 为空 → 仓库 owner
    - assignee/owner 未注册平台（无 unified_user_mapping）→ user_id=0 落库；
      任务中心 mine 查询 `user_id=? OR user_id=0` 展示（P2 拍板修订：展示，不隐藏）
    - 幂等：同一 issue 已存在 active/processing 任务则复用（2026-09-03 修复#15：不再按 assignee 区分，
      防止 webhook 回声导致同一 issue 产生两个不同 assignee 的任务）
    """
    from src.repo_key import to_repo_key, split_owner_repo
    repo_key = to_repo_key(repo_key)
    try:
        owner = split_owner_repo(repo_key)[0] or ""
    except Exception:
        owner = ""
    target = assignee or owner or ""
    user_id = _get_user_id_by_gitcode(target) if target else 0
    if user_id is None:
        user_id = 0
    with db_lock:
        c = db.cursor()
        # 幂等：同一 issue 任何 assignee 的 active/processing 任务都复用
        # （#15 修复：防止 webhook 回声导致不同 assignee 创建多个任务）
        c.execute(
            "SELECT id, conversation_id FROM tasks WHERE repo_key=? AND issue_number=? AND status IN ('active','processing') LIMIT 1",
            (repo_key, issue_number)
        )
        row = c.fetchone()
        if row:
            task_id = row[0]
            linked = row[1]
            if title:
                c.execute("UPDATE tasks SET title=? WHERE id=?", (title, task_id))
            if subtask_mode >= 0:
                c.execute("UPDATE tasks SET is_subtask_mode=? WHERE id=?", (subtask_mode, task_id))
            # 如果传入的 assignee 与现有不同，更新 assignee 并关闭旧 assignee 的任务
            # （#15 修复：复用已有任务时同步 assignee，防止 assignee 与新建 Issue 不匹配）
            _existing_assignee = ""
            try:
                c.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,))
                _r2 = c.fetchone()
                if _r2:
                    _existing_assignee = _r2[0] or ""
            except Exception:
                pass
            if target and _existing_assignee and target != _existing_assignee:
                c.execute("UPDATE tasks SET assignee=?, user_id=? WHERE id=?", (target, user_id, task_id))
                try:
                    _close_other_assignee_tasks(repo_key, issue_number, keep_assignee=target)
                except Exception:
                    pass
            db.commit()
        else:
            if not title:
                title = _get_issue_title(repo_key, issue_number)
            if not description:
                description = f"{event_type or 'Issue'}：请在任务中心查看并处理。"
            _subtask_val = subtask_mode if subtask_mode >= 0 else _get_issue_subtask_mode(repo_key, issue_number)
            _parent_val = _get_issue_parent(repo_key, issue_number)
            c.execute(
                "INSERT INTO tasks (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number) "
                "VALUES (?,?,?,?,?,?,'active',?,?)",
                (user_id, target, repo_key, issue_number, title, description,
                 _subtask_val, _parent_val)
            )
            db.commit()
            task_id = c.lastrowid
            linked = None
    # 「去对话」配套：user_id>0 且未关联对话时创建/复用 Issue 会话
    if user_id > 0 and not linked:
        try:
            conv_id = _ensure_task_conversation(user_id, repo_key, issue_number, title, description)
            if conv_id:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE tasks SET conversation_id=? WHERE id=?", (conv_id, task_id))
                    db.commit()
        except Exception as e:
            log.warning(f"ensure_issue_task conversation link failed: {e}")
    return task_id


def close_issue_tasks(repo_key: str, issue_number: int) -> int:
    """webhook 兜底（P5）：Issue closed → 该 issue 所有 active/processing 任务完成。"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE tasks SET status='completed', completed_reason='issue_closed', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (repo_key, issue_number)
        )
        db.commit()
        return c.rowcount


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


def _get_issue_subtask_mode(repo_key: str, issue_number: int) -> int:
    """从 issue_state 读取 is_subtask_mode（任务创建时同步）。
    子任务（有 parent_issue_id）也算多任务模式。"""
    from src.api.session_store import get_issue_stage
    issue_id = f"#{issue_number}"
    state = get_issue_stage(repo_key, issue_id) or {}
    if state.get("is_subtask_mode"):
        return 1
    # 有 parent_issue_id 的子任务也算多任务模式
    parent = state.get("parent_issue_id", "") or ""
    if parent and str(parent).replace("#", "").strip():
        return 1
    return 0


def _get_issue_parent(repo_key: str, issue_number: int) -> int:
    """从 issue_state 读取 parent_issue_number"""
    from src.api.session_store import get_issue_stage
    issue_id = f"#{issue_number}"
    state = get_issue_stage(repo_key, issue_id) or {}
    parent = state.get("parent_issue_id", "") or ""
    parent_num = str(parent).replace("#", "").strip()
    return int(parent_num) if parent_num.isdigit() else 0


def _get_issue_title(repo_key: str, issue_number: int) -> str:
    """从 events 表取该 issue 最新事件的标题（零 API 调用）。
    
    跳过 title 为事件类型名（如 issue_opened/issue_updated）或 A2A 占位标题的行，
    这些不是 Issue 真实标题。回退到 issue_opened 事件的 payload 中提取标题。
    """
    _EVENT_TYPE_NAMES = {
        "issue_opened", "issue_updated", "issue_comment", "issue_labeled",
        "issue_closed", "push", "tag_created", "release_created",
        "pull_request_opened", "pull_request_updated", "pull_request_merged",
        "note_hook_pr", "vod_signal", "a2a_event", "build_start", "build_failure",
        "deploy_start", "deploy_failure", "deploy_finish",
    }
    def _is_placeholder(t):
        return not t or t in _EVENT_TYPE_NAMES or t.startswith("A2A:") or t.startswith("A2A (订阅):")
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT title FROM events WHERE repo_key=? AND issue_id=? AND title!='' "
                "ORDER BY seq DESC LIMIT 1",
                (repo_key, f"#{issue_number}")
            )
            row = c.fetchone()
            if row:
                t = row[0] or ""
                if not _is_placeholder(t):
                    return t
            # payload 兜底：从 issue_opened 事件的 payload 中提取真实标题
            c.execute(
                "SELECT payload FROM events WHERE repo_key=? AND issue_id=? AND event_type='issue_opened' "
                "ORDER BY seq ASC LIMIT 1",
                (repo_key, f"#{issue_number}")
            )
            prow = c.fetchone()
            if prow:
                import json as _json
                try:
                    _pl = _json.loads(prow[0] or "{}")
                except Exception:
                    _pl = {}
                _obj = _pl.get("object_attributes", {})
                _iss = _pl.get("issue", {})
                _pt = ""
                if isinstance(_obj, dict):
                    _pt = _obj.get("title", "") or ""
                if not _pt and isinstance(_iss, dict):
                    _pt = _iss.get("title", "") or ""
                if _pt and not _is_placeholder(_pt):
                    return _pt
            return ""
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


async def _run_notify(ctx, api, user_id: int, conv_id: str, open_id: str,
                      task_description: str, repo_key: str, issue_number: int, cfg: dict):
    """任务分配通知：仅静态模板消息 + 飞书通知，不执行任何后台 LLM/ChatAgent。

    §十 原则：ChatAgent 只在用户真实回复消息时执行；任务分配/通知不得触发 opencode/stream，
    也不注入用户个人 token 用于 GitCode 写操作（见 agent-tasks-direct-create-binding-fix-design.md §10.4）。
    """
    try:
        # 静态模板 agent 消息（不生成 LLM 回复，提示用户去任务中心『去对话』）。
        # 任务上下文/描述已由 _ensure_task_conversation 预写入，此处仅追加指针消息，不重复内容。
        if conv_id:
            try:
                from src.repo_key import from_repo_key
                _tpl = (f"📋 任务已分配 · Issue #{issue_number}\n\n"
                        f"仓库: {from_repo_key(repo_key)}\n\n"
                        f"请到任务中心『去对话』查看并处理。你的第一条回复将创建会话。")
                add_message(conv_id, "agent", _tpl)
                log.info(f"Task notify: static template written to conv={conv_id} (no chat-agent)")
            except Exception as e:
                log.warning(f"Task notify: static template write failed: {e}")

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


@router.post("/notify")
async def notify_task(body: TaskNotifyBody, request: Request):
    """issue agent 分配/换人后调用：建任务 + 换人刷新 + Web conversation + 静态模板通知 + 飞书通知（不触发 ChatAgent）"""
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

    # 创建/复用 Web conversation（不带 im_channel_id，Chat Agent 页可见）——
    # 复用 _ensure_task_conversation（与 ensure_issue_task 同源，notify 语义：每次分配重刷上下文）
    conv_id = _ensure_task_conversation(user_id, body.repo_key, body.issue_number,
                                        title, body.task_description, write_context_on_reuse=True)
    if conv_id:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE tasks SET conversation_id=? WHERE id=?", (conv_id, task_id))
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


class TaskCreateBody(BaseModel):
    repo_key: str = ""
    title: str = ""
    description: str = ""
    assignee: str = ""
    scene_id: str = ""
    is_subtask_mode: int = 0


class TaskStatusBody(BaseModel):
    status: str = ""


# 任务状态迁移规则（任务中心新建任务支持的状态流）
_TASK_STATUS_FLOWS = {
    "active": ("processing", "cancelled"),
    "processing": ("completed", "cancelled", "active"),
    "completed": (),
    "cancelled": ("active",),
}


def _find_open_issue_by_title(api_token: str, owner: str, repo: str, title: str) -> int | None:
    """幂等查找：同仓库已存在同名 open Issue → 返回其 number（避免重复建 issue）。"""
    from src.user.repo_manager import _req
    try:
        data = _req("GET",
                    f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?state=all&per_page=50&page=1",
                    api_token)
    except Exception:
        return None
    if isinstance(data, dict):
        _cand = data.get("data") if isinstance(data.get("data"), list) else (data.get("items") or data.get("records") or [])
        items = _cand or []
    else:
        items = data if isinstance(data, list) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("title") or "").strip() == title.strip():
            st = str(it.get("state") or "").lower()
            if st in ("open", "opened", "active", "1", "todo"):
                n = it.get("number") or it.get("iid") or it.get("id")
                if n:
                    return int(n)
    return None


def _post_gitcode_issue(api_token: str, owner: str, repo: str, title: str,
                        body_text: str = "", assignee: str = "") -> dict:
    """创建 GitCode Issue（v5 实测：必须 access_token query + form-encoded，勿用 Bearer+JSON body）。

    Gitee 系 API 特点：POST /repos/{owner}/{repo}/issues?access_token=xxx
    body 为 application/x-www-form-urlencoded 的 title/body/assignee；
    JSON body 会返回 400 "body不能为空"（2026-08-21 实测）。
    """
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur
    import urllib.error as _uerr
    params = {"title": title}
    if body_text:
        params["body"] = body_text
    if assignee:
        params["assignee"] = assignee
    url = f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?access_token={api_token}"
    req = _ur.Request(url, data=_up.urlencode(params).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"}, method="POST")
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return _json.loads(raw) if raw.strip() else {}
    except _uerr.HTTPError as e:
        raise RuntimeError(f"GitCode API POST issues failed ({e.code}): {e.read().decode(errors='replace')[:300]}")
    except _uerr.URLError as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")


def _patch_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（追加多任务模式标记）。"""
    import urllib.request as _ur, urllib.error as _uerr, json as _json
    url = f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{issue_num}?access_token={api_token}"
    data = _json.dumps({"body": body_text}).encode()
    req = _ur.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="PATCH")
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read().decode()) if resp.read else {}
    except Exception:
        raise


@router.post("/create")
async def create_task_via_issue(body: TaskCreateBody, user: dict = Depends(get_current_user)):
    """Task Center 新建任务：同步在 GitCode 仓库创建 Issue，并按既有流程建档。

    - issue 责任人 = 系统 General Settings 的 Account（settings.credentials.gitcode.account，
      即当前登录用户 gitcode 账号；也可显式传 assignee 覆盖）
    - 幂等：同仓库同标题的 open issue 存在 → 复用（不重复创建）；
      同一 issue 的任务 id 不重复（ensure_issue_task 幂等）
    - 状态迁移：task 支持 active/processing/completed/cancelled（PUT /api/tasks/{id}/status）
    """
    from src.repo_key import to_repo_key, split_owner_repo
    if not body.repo_key or not body.title.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库和标题必填"})
    if not (body.description or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "任务描述必填"})
    repo_key = to_repo_key(body.repo_key)
    owner, rname = split_owner_repo(repo_key)
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})
    assignee = (body.assignee or "").strip() or user["username"]  # General Settings Account：登录用户 gitcode 账号
    # 1) 幂等：同 title open issue 已存在 → 提示用户任务已存在
    issue_num = _find_open_issue_by_title(api_token, owner, rname, body.title.strip())
    created = False
    if issue_num is not None:
        raise HTTPException(status_code=409, detail={"code": 409,
                                                     "message": f"任务已存在（Issue #{issue_num}），请输入新的任务标题",
                                                     "issue_number": issue_num})
    if issue_num is None:
        resp = _post_gitcode_issue(api_token, owner, rname, body.title.strip(),
                                   (body.description or "").strip(), assignee=assignee)
        n = None
        if isinstance(resp, dict):
            n = resp.get("number") or resp.get("iid") or resp.get("id")
        if not n:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"创建 GitCode Issue 失败（响应无 issue number）: {str(resp)[:200]}"})
        issue_num = int(n)
        created = True
    # 2) 自动检测多任务模式：根据场景的 issue skill 是否为 issue-multitask
    _auto_subtask_mode = 0
    if body.scene_id:
        try:
            from src.api.pipeline_config import _query_scene_by_id
            from src.config import log as _log
            _scene_obj = _query_scene_by_id(body.scene_id, repo_key=repo_key, user_id=user.get("id", 0))
            if _scene_obj:
                _nodes = _scene_obj.get("nodes", []) or []
                _issue_skill = next((n.get("skill", "") for n in _nodes if n.get("id") == "issue"), "")
                if _issue_skill == "issue-multitask":
                    _auto_subtask_mode = 1
                    _log.info(f"Auto multi-task mode: scene={body.scene_id} issue skill=issue-multitask")
        except Exception as _e:
            from src.config import log as _log
            _log.warning(f"Auto multi-task detection failed: {_e}")

    # 3) 按既有流程幂等建档（同 issue 任务 id 复用）
    task_id = ensure_issue_task(repo_key, issue_num, body.title.strip(), body.description or "", assignee=assignee,
                                subtask_mode=_auto_subtask_mode)
    if not task_id:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "任务建档失败"})
    # 4) 如指定了开发场景 → 写入 issue_state.scene_id + scene_user_id
    if body.scene_id:
        try:
            from src.api.session_store import set_issue_scene
            scene_uid = 0
            try:
                from src.api.pipeline_config import _query_scene_by_id
                scene_obj = _query_scene_by_id(body.scene_id, repo_key=repo_key, user_id=user.get("id", 0))
                if scene_obj and scene_obj.get("scope") == "user":
                    scene_uid = user.get("id", 0)
            except Exception:
                pass
            set_issue_scene(repo_key, f"#{issue_num}", body.scene_id, scene_user_id=scene_uid)
        except Exception as _e:
            from src.config import log
            log.warning(f"create_task: set_issue_scene failed: {_e}")
    # 5) 多任务模式（自动检测）→ Issue body 追加标记 + issue_state 标记
    if _auto_subtask_mode:
        try:
            from src.api.session_store import set_subtask_mode
            set_subtask_mode(repo_key, f"#{issue_num}", True)
        except Exception as _e:
            from src.config import log
            log.warning(f"create_task: set_subtask_mode failed: {_e}")
        try:
            _marker = "\n\n---\n[multi-task-mode] 本任务启用多任务模式，将由 Issue Agent 自动判断是否拆分为子任务执行。"
            _patch_issue_body(api_token, owner, rname, issue_num, (body.description or "") + _marker)
        except Exception as _e:
            from src.config import log
            log.warning(f"create_task: patch issue body for multi-task-mode failed: {_e}")
    return {"ok": True, "task_id": task_id, "issue_number": issue_num, "created": created,
            "assignee": assignee, "is_subtask_mode": _auto_subtask_mode,
            "message": "任务已创建，Issue 已同步" if created else "任务已创建（复用已有 Issue）"}


@router.put("/{task_id}/status")
async def change_task_status(task_id: int, body: TaskStatusBody, user: dict = Depends(get_current_user)):
    """任务状态迁移（active → processing → completed / cancelled；cancelled → active 可恢复）。

    终端状态 completed 不可改；同一任务只能单向流转（按 _TASK_STATUS_FLOWS 校验）。
    """
    ns = (body.status or "").strip()
    if ns not in _TASK_STATUS_FLOWS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"非法状态: {ns}"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, status FROM tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
        if row[0] not in (user["id"], 0):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该任务"})
        cur = row[1]
        if cur != ns and ns not in _TASK_STATUS_FLOWS.get(cur, ()):
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"不允许从 {cur} 迁移到 {ns}"})
        c.execute(
            "UPDATE tasks SET status=?, "
            "completed_at=CASE WHEN ? IN ('completed','cancelled') THEN datetime('now') ELSE completed_at END, "
            "completed_reason=CASE WHEN ?='completed' THEN 'manual' WHEN ?='cancelled' THEN 'cancelled' ELSE completed_reason END "
            "WHERE id=?",
            (ns, ns, ns, ns, task_id)
        )
        db.commit()
    return {"ok": True, "task_id": task_id, "status": ns}


class TaskClarifyBody(BaseModel):
    message: str = ""


def _get_task_issue_info(task_id: int) -> tuple[str, int, str] | None:
    """从 tasks 表取 repo_key + issue_number + conversation_id（关联 agent_tasks 的键）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number, conversation_id FROM tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return None
    return row[0], row[1], row[2] or ""


def _find_agent_task_ids(repo_key: str, issue_number: int) -> list[str]:
    """按 repo_key + issue_id 关联 agent_tasks（agent 执行产生的 task_messages 以此为外键）。"""
    from src.repo_key import to_repo_key
    rk = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM agent_tasks WHERE repo_key=? AND issue_id=? ORDER BY created_at DESC",
            (rk, issue_number))
        return [r[0] for r in c.fetchall()]


@router.get("/{task_id}/clarifications")
async def get_task_clarifications(task_id: int, user: dict = Depends(get_current_user)):
    """拉取 agent 在该 Issue 上的澄清消息（task_messages 中 text/tool_use 类型）。

    关联链：tasks.issue_number + repo_key → agent_tasks.id → task_messages.task_id。
    只返回 text / reasoning / error 类型（agent 向用户发出的消息），不含 step_start/finish 噪音。
    """
    info = _get_task_issue_info(task_id)
    if not info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    repo_key, issue_number, conv_id = info
    agent_task_ids = _find_agent_task_ids(repo_key, issue_number)
    messages = []
    if agent_task_ids:
        with db_lock:
            c = db.cursor()
            # 取最近 50 条 agent 澄清消息（text/reasoning/error/tool_use）
            placeholders = ",".join("?" * len(agent_task_ids))
            c.execute(
                f"SELECT task_id, seq, type, tool, content, input_json, created_at "
                f"FROM task_messages WHERE task_id IN ({placeholders}) "
                f"AND type IN ('text','reasoning','error','tool_use') "
                f"ORDER BY created_at DESC LIMIT 50",
                agent_task_ids)
            rows = c.fetchall()
        import json as _json
        for r in reversed(rows):
            msg = {"task_id": r[0], "seq": r[1], "type": r[2], "tool": r[3] or "",
                   "content": r[4] or "", "created_at": normalize_event_time(r[6])}
            if r[5]:
                try:
                    msg["input"] = _json.loads(r[5])
                except Exception:
                    msg["input"] = r[5]
            messages.append(msg)
    # 也拉 task conversation 里的历史消息（用户与 agent 的对话）
    conv_messages = []
    if conv_id:
        from src.session_manager import get_messages
        conv_messages = get_messages(conv_id, limit=30)
    return {"messages": messages, "conversation_id": conv_id,
            "issue_url": f"https://gitcode.com/{repo_key}/issues/{issue_number}"}


@router.post("/{task_id}/clarify")
async def reply_to_clarification(task_id: int, body: TaskClarifyBody,
                                 user: dict = Depends(get_current_user)):
    """开发者在 Task Center 回复 agent 澄清：同步发 GitCode Issue 评论 + 写入任务会话。

    - Issue 评论：以「💬 开发者澄清」前缀发到 GitCode（agent 通过 webhook issue_comment 事件继续处理）
    - 任务会话：写入 chat_messages（user 角色），后续「去对话」可看到完整上下文
    """
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "消息不能为空"})
    info = _get_task_issue_info(task_id)
    if not info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
    repo_key, issue_number, conv_id = info
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    # 1) 发 GitCode Issue 评论（form + access_token，同 _post_gitcode_issue 的实测结论）
    from src.repo_key import split_owner_repo
    import urllib.parse as _up
    import urllib.request as _ur
    import urllib.error as _uerr
    owner, rname = split_owner_repo(repo_key)
    comment_body = f"💬 **开发者澄清**\n\n{msg}"
    url = f"https://api.gitcode.com/api/v5/repos/{owner}/{rname}/issues/{issue_number}/comments?access_token={api_token}"
    req = _ur.Request(url, data=_up.urlencode({"body": comment_body}).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"}, method="POST")
    try:
        with _ur.urlopen(req, timeout=30) as resp:
            resp.read()
        log.info(f"Task clarify: posted comment to {repo_key}/issues/{issue_number} by {user['username']}")
    except _uerr.HTTPError as e:
        raise HTTPException(status_code=502, detail={"code": 502,
                                                     "message": f"GitCode 评论失败({e.code}): {e.read().decode(errors='replace')[:200]}"})
    except _uerr.URLError as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"无法连接 GitCode: {e.reason}"})

    # 2) 写入任务会话（如有 conv_id）
    if conv_id:
        try:
            from src.session_manager import add_message
            add_message(conv_id, "user", msg)
        except Exception as e:
            log.warning(f"Task clarify: write conversation message failed: {e}")

    return {"ok": True, "message": "澄清已发送到 Issue 并同步到任务会话"}


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
    clauses = ["(user_id=? OR user_id=0)"]
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
                f"status, completed_reason, created_at, completed_at, is_subtask_mode, parent_issue_number "
                f"FROM tasks WHERE {where} "
                f"ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, created_at DESC",
                params
            )
            rows = c.fetchall()
        except Exception:
            rows = []
    # 查 issue_state 中的 scene_id（按 repo_key + issue_number 批量查询）
    from src.api.session_store import get_issue_scene
    from src.api.pipeline_config import get_pipeline_config
    from src.api.session_store import get_issue_stage
    tasks_out = []
    for r in rows:
        _rk = to_repo_key(from_repo_key(r[2]))
        _issue_id = f"#{r[3]}"
        # 区分场景来源：issue_state.scene_id（VOD 识别）vs 仓库默认场景
        _st = get_issue_stage(_rk, _issue_id)
        _issue_scene_id = (_st.get("scene_id") or "") if _st else ""
        if _issue_scene_id:
            _scene_id = _issue_scene_id
            _scene_source = "vod"  # VOD 识别
        else:
            # 回退到仓库默认场景
            _scene_id = (get_issue_scene(_rk, _issue_id) or ("default", 0))[0]
            _scene_source = "repo_default"
        _scene_name = ""
        try:
            _cfg = get_pipeline_config(_scene_id, repo_key=_rk)
            if _cfg:
                _scene_name = _cfg.get("name", _scene_id)
        except Exception:
            pass
        tasks_out.append({
            "id": r[0], "assignee": r[1], "repo_key": from_repo_key(r[2]), "issue_number": r[3],
            "title": r[4] or "", "task_description": r[5] or "",
            "conversation_id": r[6] or "", "status": r[7],
            "completed_reason": r[8] or "", "created_at": normalize_event_time(r[9]), "completed_at": normalize_event_time(r[10]),
            "is_subtask_mode": r[11] or 0,
            "parent_issue_number": r[12] or 0,
            "scene_id": _scene_id,
            "scene_name": _scene_name,
            "scene_source": _scene_source,
        })
    return {"tasks": tasks_out}
