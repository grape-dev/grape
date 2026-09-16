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

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _require_session_repo_token(request: Request, repo_key: str) -> dict:
    """notify 等 agent 动作端点鉴权：仅接受 `tk_` 会话 token，且仓库作用域必须匹配。

    不复用仓库级 bot token（webhook 同源、不属于三通道体系）——对外只认会话 token（tk_）。
    会话 token 由执行期注入（event_pipeline 按 task_id/repo_key/agent_type 签发），
    仓库作用域经 `verify_session_token_for_repo` 校验（同一仓库执行期的 agent 凭据才能调用）。
    """
    from src.api.auth_token import verify_session_token_for_repo
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "需会话 token（tk_）"})
    info = verify_session_token_for_repo(token, repo_key)
    if not info:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 无效或仓库作用域不匹配"})
    return info


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


def _ensure_assignee_task_card(user_id: int, assignee: str, repo_key: str, issue_number: int,
                               title: str, task_description: str) -> int | None:
    """为「指定 assignee + Issue」建/复用任务中心卡片（active）；区别于移除的 create，仅服务 notify 分配语义。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM issue WHERE repo_key=? AND issue_number=? AND assignee=? AND status='active' LIMIT 1",
            (repo_key, issue_number, assignee)
        )
        row = c.fetchone()
        if row:
            return row[0]
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id) "
            "VALUES (?,?,?,?,?,?,'active',?,?,?)",
            (user_id, assignee, repo_key, issue_number, title, task_description,
             _get_issue_subtask_mode(repo_key, issue_number), _get_issue_parent(repo_key, issue_number),
             _get_issue_parent_id(repo_key, issue_number))
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
    # Issue #49: assignee 为空时，优先从 issue_state 读取 assignee，再 fallback 到 owner
    if not assignee:
        try:
            from src.api.session_store import get_issue_stage
            _st = get_issue_stage(repo_key, f"#{issue_number}")
            if _st and _st.get("assignee"):
                assignee = _st["assignee"]
        except Exception:
            pass
    target = assignee or owner or ""
    user_id = _get_user_id_by_gitcode(target) if target else 0
    if user_id is None:
        # 未注册平台用户 → user_id=0（保留原始 assignee 名字，不 fallback 到 owner）
        user_id = 0
    with db_lock:
        c = db.cursor()
        # 幂等：同一 issue 任何 assignee 的 active/processing 任务都复用
        # （#15 修复：防止 webhook 回声导致不同 assignee 创建多个任务）
        c.execute(
            "SELECT id, conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND status IN ('active','processing') LIMIT 1",
            (repo_key, issue_number)
        )
        row = c.fetchone()
        if row:
            task_id = row[0]
            linked = row[1]
            if title:
                c.execute("UPDATE issue SET title=? WHERE id=?", (title, task_id))
            if subtask_mode >= 0:
                c.execute("UPDATE issue SET is_subtask_mode=? WHERE id=?", (subtask_mode, task_id))
            # 如果传入的 assignee 与现有不同，更新 assignee 并关闭旧 assignee 的任务
            # （#15 修复：复用已有任务时同步 assignee，防止 assignee 与新建 Issue 不匹配）
            _existing_assignee = ""
            try:
                c.execute("SELECT assignee FROM issue WHERE id=?", (task_id,))
                _r2 = c.fetchone()
                if _r2:
                    _existing_assignee = _r2[0] or ""
            except Exception:
                pass
            if target and _existing_assignee and target != _existing_assignee:
                c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (target, user_id, task_id))
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
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?)",
                (user_id, target, repo_key, issue_number, title, description,
                 _subtask_val, _parent_val, _get_issue_parent_id(repo_key, issue_number))
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
                    c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, task_id))
                    db.commit()
        except Exception as e:
            log.warning(f"ensure_issue_task conversation link failed: {e}")
    return task_id


def close_issue_tasks(repo_key: str, issue_number: int) -> int:
    """webhook 兜底（P5）：Issue closed → 该 issue 所有 active/processing 任务进入 pending_review。

    Issue #51 修订：不再直接 completed，而是先设为 pending_review 等待开发者审核。
    只有 stage 已经是 done/pending_review 时才直接关闭。
    """
    from src.repo_key import to_repo_key
    from src.api.session_store import get_issue_stage, set_issue_stage
    repo_key = to_repo_key(repo_key)
    _issue_id = f"#{issue_number}"
    _stage_info = get_issue_stage(repo_key, _issue_id)
    _stage = (_stage_info or {}).get("stage", "") if _stage_info else ""

    with db_lock:
        c = db.cursor()
        # 检查是否有 agent 正在 running
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running') LIMIT 1",
            (repo_key, _issue_id))
        _has_running = c.fetchone() is not None

        if _stage == "done":
            # stage=done 表示开发者已确认 → 直接 completed
            c.execute(
                "UPDATE issue SET status='completed', completed_reason='issue_closed', completed_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing','awaiting_input','pending_review')",
                (repo_key, issue_number)
            )
        elif _stage == "pending_review":
            # Issue #85: stage=pending_review → 保持 pending_review，不自动 completed
            # 只将 active 任务推进到 pending_review（processing/awaiting_input 保持原状）
            c.execute(
                "UPDATE issue SET status='pending_review' "
                "WHERE repo_key=? AND issue_number=? AND status='active'",
                (repo_key, issue_number)
            )
        elif not _has_running:
            # 还在 pipeline 流转中且无 agent 运行 → 设为 pending_review 等待开发者确认
            # Issue #85: 无 issue_state 记录（stage 为空）时也走此路径
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
                (repo_key, issue_number)
            )
            set_issue_stage(repo_key, _issue_id, "pending_review", "issue_closed")
        # else: 有 agent 在运行 → 不修改 stage，等 agent 完成后再判定
        db.commit()
        return c.rowcount


def _close_other_assignee_tasks(repo_key: str, issue_number: int, keep_assignee: str) -> int:
    """换人：将同 issue 其他 assignee 的 active/processing 任务置为 completed（reassigned）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='completed', completed_reason='reassigned', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing') AND assignee<>?",
            (repo_key, issue_number, keep_assignee)
        )
        db.commit()
        return c.rowcount


def _close_issue_tasks_bot_takeover(repo_key: str, issue_number: int) -> int:
    """bot 接管：将同 issue active 任务置 processing（Issue #51：不再直接 completed）。

    见 task-lifecycle-design.md §2.3：issue agent 将 assignee 刷为 grape_bot 表示自己接管。
    Issue #51 修订：不再直接关闭，改为走 pending_review 流程。
    如果有 agent 正在 running，不修改 stage（等 agent 完成后再判定）。
    """
    from src.api.session_store import get_issue_stage, set_issue_stage
    _issue_id = f"#{issue_number}"
    _stage_info = get_issue_stage(repo_key, _issue_id)
    _stage = (_stage_info or {}).get("stage", "") if _stage_info else ""

    # 检查是否有 agent 正在 running
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running') LIMIT 1",
            (repo_key, _issue_id))
        _has_running = c.fetchone() is not None

        if _stage == "done":
            # stage=done → 直接 completed
            c.execute(
                "UPDATE issue SET status='completed', completed_reason='bot_takeover', completed_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing','awaiting_input','pending_review')",
                (repo_key, issue_number))
        elif _stage == "pending_review":
            # Issue #85: stage=pending_review → 保持，不自动 completed
            c.execute(
                "UPDATE issue SET status='pending_review' "
                "WHERE repo_key=? AND issue_number=? AND status='active'",
                (repo_key, issue_number))
        elif not _has_running:
            # 没有 agent 在运行 → 设为 pending_review（Issue #85: 同步 processing 任务）
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
                (repo_key, issue_number))
            set_issue_stage(repo_key, _issue_id, "pending_review", "bot_takeover")
        # else: 有 agent 在运行 → 不修改 stage，等 agent 完成后再判定
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


def _get_issue_parent_id(repo_key: str, issue_number: int) -> str:
    """从 issue_state 读取 parent_issue_id（文本，如 "#20"；空=普通任务）。"""
    from src.api.session_store import get_issue_stage
    issue_id = f"#{issue_number}"
    state = get_issue_stage(repo_key, issue_id) or {}
    return (state.get("parent_issue_id") or "").strip()


def _get_issue_parent(repo_key: str, issue_number: int) -> int:
    """从 issue_state 读取 parent_issue_id 并转为 int（前端兼容 parent_issue_number）。"""
    parent = _get_issue_parent_id(repo_key, issue_number)
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
        return {"ok": True, "task_id": 0, "conversation_id": "",
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


class TaskCreateBody(BaseModel):
    repo_key: str = ""
    title: str = ""
    description: str = ""
    assignee: str = ""
    scene_id: str = ""
    is_subtask_mode: int = 0


class TaskStatusBody(BaseModel):
    status: str = ""


# 任务状态迁移规则（Issue #85: 增加 awaiting_input + pending_review）
_TASK_STATUS_FLOWS = {
    "active": ("processing", "awaiting_input", "cancelled"),
    "processing": ("awaiting_input", "pending_review", "completed", "cancelled", "active"),
    "awaiting_input": ("processing", "cancelled"),
    "pending_review": ("completed", "cancelled"),
    "completed": (),
    "cancelled": ("active",),
}


# Issue #51: 任务确认完成 / 取消 / 恢复
class TaskConfirmBody(BaseModel):
    confirmation: str = "user_confirmed"


def _cleanup_issue_attachments(repo_key: str, issue_number):
    """删除指定 issue 的所有附件。任务完成/取消时调用。"""
    try:
        from src.repo_key import split_owner_repo
        _owner, _rname = split_owner_repo(repo_key)
        _attach_cleanup_dir(_owner, _rname, str(issue_number))
    except Exception as _e:
        log.warning(f"Attachment cleanup failed: {_e}")


def confirm_issue_done(repo_key: str, issue_number: int, confirmed_by: str = "issue_comment"):
    """Issue 评论确认完成 → 关闭该 issue 所有 active 任务 + stage=done"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='completed', completed_reason=?, completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (confirmed_by, repo_key, issue_number))
        affected = c.rowcount
        db.commit()
    from src.api.session_store import set_issue_stage
    set_issue_stage(repo_key, f"#{issue_number}", "done", "user")
    log.info(f"Issue #{issue_number} confirmed done by {confirmed_by} ({affected} tasks closed)")
    return affected


def cancel_issue_task(repo_key: str, issue_number: int):
    """Issue 评论取消任务"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='cancelled', completed_reason='user_cancelled', completed_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')",
            (repo_key, issue_number))
        affected = c.rowcount
        db.commit()
    log.info(f"Issue #{issue_number} cancelled ({affected} tasks)")
    return affected


def resume_issue_task(repo_key: str, issue_number: int):
    """Issue 评论恢复任务"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='active', completed_reason=NULL, completed_at=NULL "
            "WHERE repo_key=? AND issue_number=? AND status='cancelled'",
            (repo_key, issue_number))
        affected = c.rowcount
        db.commit()
    log.info(f"Issue #{issue_number} resumed ({affected} tasks)")
    return affected


def _find_open_issue_by_title(api_token: str, owner: str, repo: str, title: str,
                             provider: str = "gitcode") -> int | None:
    """幂等查找：同仓库已存在同名 open Issue → 返回其 number（避免重复建 issue）。"""
    if provider == "github":
        from src.user.repo_manager import _github_req
        try:
            data = _github_req("GET",
                               f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=50",
                               api_token)
        except Exception:
            return None
        items = data if isinstance(data, list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("title") or "").strip() == title.strip():
                n = it.get("number")
                if n:
                    return int(n)
        return None
    # GitCode
    from src.user.repo_manager import _req
    try:
        data = _req("GET",
                    f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues?state=all&per_page=50&page=1",
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
    url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues?access_token={api_token}"
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


def _post_github_issue(api_token: str, owner: str, repo: str, title: str,
                       body_text: str = "", assignee: str = "") -> dict:
    """创建 GitHub Issue（POST /repos/{owner}/{repo}/issues，JSON body + Bearer token）。"""
    from src.user.repo_manager import _github_req
    body = {"title": title}
    if body_text:
        body["body"] = body_text
    if assignee:
        body["assignees"] = [assignee]
    return _github_req("POST", f"https://api.github.com/repos/{owner}/{repo}/issues", api_token, body)


def _is_gitcode_401(err: Exception) -> bool:
    """判断异常是否为 GitCode API 401 Bad credentials（Issue #52）。"""
    msg = str(err)
    return "401" in msg or "Bad credentials" in msg


def _gitcode_api_with_refresh(user: dict, api_call_fn, *args, **kwargs):
    """包装 GitCode API 调用——401 时自动刷新 OAuth token 并重试一次（Issue #52）。

    user: 当前登录用户 dict（含 id, username）
    api_call_fn: 实际调 GitCode API 的函数，第一个位置参数是 token
    *args, **kwargs: 传给 api_call_fn 的其余参数（owner, repo, title 等）

    返回 api_call_fn 的结果，或抛 HTTPException（刷新失败时）。
    """
    from src.api.deps import get_user_token, try_refresh_gitcode_token

    username = user["username"]
    user_id = user["id"]
    token = get_user_token(username)

    try:
        return api_call_fn(token, *args, **kwargs)
    except RuntimeError as e:
        if not _is_gitcode_401(e):
            raise  # 非 401 错误不处理
        # 401 → 尝试刷新 token
        new_token = try_refresh_gitcode_token(user_id)
        if not new_token:
            raise HTTPException(status_code=401, detail={
                "code": 401,
                "message": "GitCode 授权已过期，请重新登录授权",
                "action": "reauth_required"
            })
        # 刷新成功 → 重试一次
        return api_call_fn(new_token, *args, **kwargs)


def _patch_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（追加多任务模式标记）。GitCode 版。"""
    import urllib.request as _ur, urllib.error as _uerr, json as _json
    url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues/{issue_num}?access_token={api_token}"
    data = _json.dumps({"body": body_text}).encode()
    req = _ur.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="PATCH")
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read().decode()) if resp.read else {}
    except Exception:
        raise


def _patch_github_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（GitHub 版）。"""
    from src.user.repo_manager import _github_req
    return _github_req("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}",
                       api_token, {"body": body_text})


def create_standard_task(repo_key: str, title: str, description: str,
                         bot_token: str, assignee: str = "", labels=None) -> dict:
    """真实 GitCode Issue 创建 + 标准任务建档（onboarding 模块调用，与手动创建任务同一原子路径）。

    - 幂等：同仓库同标题 open Issue 已存在 → 不重复创建（复用 _find_open_issue_by_title）
    - 建档：ensure_issue_task（幂等，天然单条任务上下文消息）
    - token：bot_token 注入（onboarding 无登录用户上下文，用仓库 bot token）
    - 不额外写入任何消息：任务上下文消息只由 ensure_issue_task → _ensure_task_conversation 产生 1 条
    """
    from src.repo_key import to_repo_key, split_owner_repo, parse_repo_key
    repo_key = to_repo_key(repo_key)
    owner, rname = split_owner_repo(repo_key)
    _provider, _, _ = parse_repo_key(repo_key)
    if not title or not title.strip():
        return {"ok": False, "code": 400, "message": "任务标题必填"}
    if not bot_token:
        return {"ok": False, "code": 400, "message": "bot token 缺失"}
    if not assignee:
        assignee = owner or ""
    issue_num = _find_open_issue_by_title(bot_token, owner, rname, title.strip())
    created = False
    if issue_num is not None:
        # 已有同标题 open Issue → 直接建档（幂等：不再重复创建 Issue）
        issue_num = int(issue_num)
    else:
        if _provider == "github":
            resp = _post_github_issue(bot_token, owner, rname, title.strip(),
                                      (description or "").strip(), assignee=assignee)
        else:
            resp = _post_gitcode_issue(bot_token, owner, rname, title.strip(),
                                       (description or "").strip(), assignee=assignee)
        n = None
        if isinstance(resp, dict):
            n = resp.get("number") or resp.get("iid") or resp.get("id")
        if not n:
            return {"ok": False, "code": 502,
                    "message": f"创建 GitCode Issue 失败（响应无 issue number）: {str(resp)[:200]}"}
        issue_num = int(n)
        created = True
    task_id = ensure_issue_task(repo_key, issue_num, title.strip(), description or "",
                                assignee=assignee, event_type="onboarding")
    if not task_id:
        return {"ok": False, "code": 500, "message": "任务建档失败"}
    return {"ok": True, "task_id": task_id, "issue_number": issue_num, "created": created,
            "message": "任务已创建（Issue 已同步）" if created else "任务已创建（复用已有 Issue）"}


async def create_task_via_issue(body: TaskCreateBody, user: dict = Depends(get_current_user_or_api_key)):
    """Task Center 新建任务：同步在 GitCode 仓库创建 Issue，并按既有流程建档。

    端点 `POST /api/tasks/create` 已废弃移除（Phase C2，见 issues-api-execution-plan.md）——
    对外统一入口为 `POST /api/issues/{owner}/{repo}`（issues.py 复用本逻辑）。
    """
    from src.repo_key import to_repo_key, split_owner_repo
    if not body.repo_key or not body.title.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库和标题必填"})
    if not (body.description or "").strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "任务描述必填"})
    repo_key = to_repo_key(body.repo_key)
    owner, rname = split_owner_repo(repo_key)
    # provider 分流：github: → GitHub API，gitcode: → GitCode API
    _is_github = repo_key.startswith("github:")
    if _is_github:
        from src.user.db import get_user_github_token
        api_token = get_user_github_token(user["id"]) or ""
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "GitHub API Token 未配置"})
    else:
        from src.api.deps import get_user_token_with_refresh
        api_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})
    assignee = (body.assignee or "").strip() or user["username"]
    # 1) 幂等：同 title open issue 已存在 → 提示用户任务已存在
    issue_num = _find_open_issue_by_title(api_token, owner, rname, body.title.strip(),
                                          provider="github" if _is_github else "gitcode")
    created = False
    if issue_num is not None:
        raise HTTPException(status_code=409, detail={"code": 409,
                                                     "message": f"任务已存在（Issue #{issue_num}），请输入新的任务标题",
                                                     "issue_number": issue_num})
    if issue_num is None:
        # 拼接 Task Center 上传的附件到 issue body
        _desc_with_attach = (body.description or "").strip()
        _pending_attachments = _attach_list(owner, rname, "pending")
        if _pending_attachments:
            _attachments = []
            for _att in _pending_attachments:
                _ext = os.path.splitext(_att["filename"])[1].lower()
                if _ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                    _attachments.append(f"![{_att['filename']}]({_att['url']})")
                else:
                    _attachments.append(f"[{_att['filename']}]({_att['url']})")
            if _attachments:
                _desc_with_attach += "\n\n## 附件\n" + "\n".join(_attachments)
        if _is_github:
            resp = _post_github_issue(api_token, owner, rname, body.title.strip(),
                                      _desc_with_attach, assignee=assignee)
        else:
            # Issue #52: 401 时自动刷新 OAuth token 并重试
            resp = _gitcode_api_with_refresh(
                user, _post_gitcode_issue, owner, rname, body.title.strip(),
                _desc_with_attach, assignee=assignee
            )
        n = None
        if isinstance(resp, dict):
            n = resp.get("number") or resp.get("iid") or resp.get("id")
        if not n:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"创建 GitCode Issue 失败（响应无 issue number）: {str(resp)[:200]}"})
        issue_num = int(n)
        created = True
        # 附件从 pending 迁移到实际 issue_number（OBS: copy + delete old; 本地: rename）
        from src.storage import get_storage
        _storage = get_storage()
        _pending_atts = _attach_list(owner, rname, "pending")
        if _pending_atts:
            for _att in _pending_atts:
                _fname = _att["filename"]
                if _storage:
                    # OBS: read pending → save to issue_number → delete pending
                    _old_key = _attach_obs_key(owner, rname, "pending", _fname)
                    _new_key = _attach_obs_key(owner, rname, str(issue_num), _fname)
                    _data = _storage.read_bytes(_old_key)
                    if _data:
                        _storage.save_bytes(_new_key, _data)
                        _storage.delete_object(_old_key)
                else:
                    # 本地: move file
                    _old_path = os.path.join(_attach_base_dir(owner, rname, "pending"), _fname)
                    _new_dir = _attach_base_dir(owner, rname, str(issue_num))
                    os.makedirs(_new_dir, exist_ok=True)
                    try:
                        os.rename(_old_path, os.path.join(_new_dir, _fname))
                    except Exception as _e:
                        log.warning(f"attachment file rename failed: {_e}")
    # 2) 场景选择：用户选了具体场景 → 写入 scene_id（issue_opened 读到有值则跳过推荐）
    #    选智能推荐（vod-auto）或留空 → 不写 scene_id → issue_opened 走智能推荐
    try:
        if body.scene_id and body.scene_id != "vod-auto":
            from src.api.session_store import set_issue_scene
            from src.runtime.engine_select import _repo_user_id
            scene_uid = _repo_user_id(repo_key, issue_creator=user["username"]) or 0
            set_issue_scene(repo_key, f"#{issue_num}", body.scene_id, scene_user_id=scene_uid)
    except Exception as _e:
        from src.config import log
        log.warning(f"create_task: set_issue_scene failed: {_e}")
    # 3) 自动检测多任务模式：根据场景的 issue skill 是否为 issue-multitask
    _auto_subtask_mode = 0
    if body.scene_id and body.scene_id != "vod-auto":
        try:
            from src.api.pipeline_config import _query_scene_by_id
            from src.config import log as _log
            _scene_obj = _query_scene_by_id(body.scene_id, repo_key=repo_key)
            if _scene_obj:
                _nodes = _scene_obj.get("nodes", []) or []
                _issue_skill = next((n.get("skill", "") for n in _nodes if n.get("id") == "issue"), "")
                if _issue_skill == "issue-multitask":
                    _auto_subtask_mode = 1
                    _log.info(f"Auto multi-task mode: scene={body.scene_id} issue skill=issue-multitask")
        except Exception as _e:
            from src.config import log as _log
            _log.warning(f"Auto multi-task detection failed: {_e}")

    # 4) 按既有流程幂等建档（同 issue 任务 id 复用）
    task_id = ensure_issue_task(repo_key, issue_num, body.title.strip(), body.description or "", assignee=assignee,
                                subtask_mode=_auto_subtask_mode)
    if not task_id:
        raise HTTPException(status_code=500, detail={"code": 500, "message": "任务建档失败"})
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
            if _is_github:
                # GitHub: 直接用用户 github_api_token
                from src.user.db import get_user_github_token
                _gh_token = get_user_github_token(user["id"]) or ""
                if _gh_token:
                    _patch_github_issue_body(_gh_token, owner, rname, issue_num, (body.description or "") + _marker)
            else:
                # Issue #52: 401 时自动刷新 token 重试
                _gitcode_api_with_refresh(user, _patch_issue_body, owner, rname, issue_num, (body.description or "") + _marker)
        except HTTPException:
            raise  # reauth_required 需要传递给前端
        except Exception as _e:
            from src.config import log
            log.warning(f"create_task: patch issue body for multi-task-mode failed: {_e}")

    # Issue #89: 同步触发 pipeline 事件，不依赖 webhook（webhook 异步可能丢事件）
    # 新建 Issue 时直接创建 issue_opened 事件 → VOD Agent 处理
    # 复用已有 Issue 时创建 a2a_event 事件 → issue Agent 处理
    if created:
        try:
            from src.config import log as _log
            from src.api.session_store import set_issue_stage
            # 初始化 issue_state（vod 阶段）
            set_issue_stage(repo_key, f"#{issue_num}", "vod", "vod", assignee=assignee or "")
            # 同步触发 pipeline（不依赖 webhook，task_center 创建的事件直接入队）
            # run_pipeline 内部会调 append_event 存事件 + 路由 + 入队
            import json as _pipe_json
            _pipe_payload = _pipe_json.dumps({
                "action": "opened",
                "issue": {"number": int(issue_num), "title": body.title.strip(),
                          "user": {"login": assignee or user["username"]}},
                "repository": {"full_name": f"{owner}/{rname}"},
                "sender": {"login": assignee or user["username"]},
            }).encode()
            _pipe_headers = {"Content-Type": "application/json"}
            if _is_github:
                _pipe_headers["X-GitHub-Event"] = "issues"
            else:
                _pipe_headers["X-Gitcode-Event"] = "issue_opened"
            from src.api.event_pipeline import run_pipeline
            _pipe_result = run_pipeline(_pipe_payload, _pipe_headers,
                                        account=repo_key, repo_key_override=repo_key)
            _log.info(f"create_task: triggered pipeline for #{issue_num}, seq={_pipe_result.get('seq', 0)}")
        except Exception as _e:
            from src.config import log as _log
            _log.warning(f"create_task: sync pipeline trigger failed: {_e}")

    return {"ok": True, "task_id": task_id, "issue_number": issue_num, "created": created,
            "assignee": assignee, "is_subtask_mode": _auto_subtask_mode,
            "message": "任务已创建，Issue 已同步" if created else "任务已创建（复用已有 Issue）"}


# ====== 附件管理 ======

# 允许的附件文件扩展名
_ATTACH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_ATTACH_DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".xml", ".html"}
_ATTACH_CODE_EXTS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".sh", ".sql", ".tf", ".dockerfile"}
_ATTACH_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z"}
_ATTACH_ALL_EXTS = _ATTACH_IMAGE_EXTS | _ATTACH_DOC_EXTS | _ATTACH_CODE_EXTS | _ATTACH_ARCHIVE_EXTS
_ATTACH_MAX_SIZE = 50 * 1024 * 1024  # 50MB


def _attach_obs_key(owner: str, repo: str, issue_number, filename: str) -> str:
    """生成 OBS 对象键：{PREFIX}/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"""
    from src.config import OBS_PREFIX
    prefix = (OBS_PREFIX or "").strip("/")
    return f"{prefix}/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"


def _attach_base_dir(owner: str, repo: str, issue_number) -> str:
    """附件本地存储目录（OBS 未启用时使用）：data/issue_attachments/{owner}/{repo}/{issue_number}/"""
    return os.path.join(WORKSPACE_DIR, "..", "data", "issue_attachments", owner, repo, str(issue_number))


def _attach_save(owner: str, repo: str, issue_number, filename: str, content: bytes) -> str:
    """保存附件。OBS 启用时上传到 OBS，否则写本地磁盘。返回可访问 URL。"""
    from src.storage import get_storage
    storage = get_storage()
    if storage:
        key = _attach_obs_key(owner, repo, issue_number, filename)
        storage.save_bytes(key, content)
        url = storage.temporary_url(key, expires=7 * 24 * 3600)  # 7 天签名有效期
        log.info(f"Attachment saved to OBS: {key} ({len(content)} bytes)")
        return url
    # 本地降级
    attach_dir = _attach_base_dir(owner, repo, issue_number)
    os.makedirs(attach_dir, exist_ok=True)
    fpath = os.path.join(attach_dir, filename)
    with open(fpath, "wb") as f:
        f.write(content)
    from src.user.db import get_meta_value as _gmv
    _base_url = (_gmv("server_base_url") or "").rstrip("/")
    return f"{_base_url}/static/issue_attachments/{owner}/{repo}/{issue_number}/{filename}"


def _attach_delete(owner: str, repo: str, issue_number, filename: str) -> bool:
    """删除单个附件。OBS 启用时删 OBS 对象，否则删本地文件。"""
    from src.storage import get_storage
    storage = get_storage()
    if storage:
        key = _attach_obs_key(owner, repo, issue_number, filename)
        return storage.delete_object(key)
    fpath = os.path.join(_attach_base_dir(owner, repo, issue_number), filename)
    if os.path.isfile(fpath):
        os.unlink(fpath)
        return True
    return False


def _attach_list(owner: str, repo: str, issue_number) -> list[dict]:
    """列出附件。返回 [{filename, url, size, is_image}]。"""
    from src.storage import get_storage
    storage = get_storage()
    if storage:
        prefix_key = _attach_obs_key(owner, repo, issue_number, "")
        items, _ = storage.list_objects_with_meta(prefix_key)
        from src.user.db import get_meta_value as _gmv
        result = []
        for item in items:
            fname = item["key"].rsplit("/", 1)[-1]
            if not fname:
                continue
            ext = os.path.splitext(fname)[1].lower()
            result.append({
                "filename": fname,
                "url": storage.temporary_url(item["key"], expires=7 * 24 * 3600),
                "size": item.get("size", 0),
                "is_image": ext in _ATTACH_IMAGE_EXTS,
            })
        return result
    # 本地降级
    attach_dir = _attach_base_dir(owner, repo, issue_number)
    from src.user.db import get_meta_value as _gmv
    _base_url = (_gmv("server_base_url") or "").rstrip("/")
    result = []
    if os.path.isdir(attach_dir):
        for fname in sorted(os.listdir(attach_dir)):
            fpath = os.path.join(attach_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            result.append({
                "filename": fname,
                "url": f"{_base_url}/static/issue_attachments/{owner}/{repo}/{issue_number}/{fname}",
                "size": os.path.getsize(fpath),
                "is_image": ext in _ATTACH_IMAGE_EXTS,
            })
    return result


def _attach_cleanup_dir(owner: str, repo: str, issue_number):
    """删除整个 issue 的附件目录。任务完成/取消时调用。"""
    from src.storage import get_storage
    storage = get_storage()
    if storage:
        prefix_key = _attach_obs_key(owner, repo, issue_number, "")
        count = storage.delete_prefix(prefix_key)
        if count > 0:
            log.info(f"Attachment cleanup: deleted {count} OBS objects for {owner}/{repo}/{issue_number}")
        return
    import shutil
    _attach_dir = _attach_base_dir(owner, repo, issue_number)
    if os.path.isdir(_attach_dir):
        shutil.rmtree(_attach_dir, ignore_errors=True)
        log.info(f"Attachment cleanup: removed {_attach_dir}")


class TaskSelfCompleteBody(BaseModel):
    task_id: str = ""
    result_text: str = ""
    next_step: str = ""
    # 结构化产出（可选）：服务端据此组装结构化 result_text，供下游事件对话/结构化解析识别
    actions: list = []
    conclusion: str = ""
    artifacts: list = []
    issue_summary: str = ""


@router.post("/complete", operation_id="tasks_complete", summary="任务自报完成（grape CLI tasks complete）")
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


@router.get("/current", operation_id="tasks_current", summary="获取当前任务详情（Agent 执行期自省）")
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


def _get_task_issue_info(task_id: int) -> tuple[str, int, str] | None:
    """从 tasks 表取 repo_key + issue_number + conversation_id（关联 agent_tasks 的键）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number, conversation_id FROM issue WHERE id=?", (task_id,))
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

def _build_assignee_filter(assignees: str, clauses: list, params: list):
    """将逗号分隔的 assignees 转成 SQL 过滤（单值 =? / 多值 IN），就地追加到 clauses/params。"""
    _list = [a.strip() for a in (assignees or "").split(",") if a.strip()]
    if len(_list) == 1:
        clauses.append("assignee=?")
        params.append(_list[0])
    elif len(_list) > 1:
        clauses.append(f"assignee IN ({','.join('?' * len(_list))})")
        params.extend(_list)


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


def _batch_fetch_issue_state(keys: list) -> dict:
    """批量查 issue 单表的 stage/scene_id，返回 {(repo_key, issue_id): {"stage":..,"scene_id":..}}（防 N+1）。

    Phase C3：读 issue 卡片行（keys 中 issue_id 为 '#N'，转 issue_number）。
    """
    out: dict = {}
    if not keys:
        return out
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_number=?)"] * len(_chunk))
        _flat = []
        for _k in _chunk:
            _n = str(_k[1]).lstrip("#")
            _flat.extend([_k[0], int(_n) if _n.isdigit() else -1])
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT repo_key, issue_number, stage, scene_id FROM issue WHERE " + _ors, _flat)
                for row in c.fetchall():
                    out[(row[0], f"#{row[1]}")] = {"stage": row[2] or "", "scene_id": row[3] or ""}
        except Exception as e:
            log.warning(f"batch issue fetch failed: {e}")
    return out


def _batch_running_agent_tasks(keys: list) -> set:
    """批量查运行中的 agent_tasks，返回 {(repo_key, issue_id)}（防 N+1，Issue #85 雷达动画依据）。"""
    out: set = set()
    if not keys:
        return out
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
        _flat = [v for k in _chunk for v in k]
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT repo_key, issue_id FROM agent_tasks "
                    "WHERE status IN ('queued','dispatched','running') AND (" + _ors + ")", _flat)
                out.update((row[0], row[1]) for row in c.fetchall())
        except Exception as e:
            log.warning(f"batch agent_tasks fetch failed: {e}")
    return out


def _batch_fetch_agent_statuses(keys: list) -> dict:
    """批量查各 Agent 节点最新事件状态（Issue #126: 与菜单流水线数据源一致）。

    从 events 表查每个 (repo_key, issue_id) 下各 agent_type 的最新事件 status，
    与 pipeline.py 的 _get_issue_pipeline 使用相同的 AGENT_ORDER + STAGE_STATUS_MAP 逻辑。

    Returns:
        {(repo_key, issue_id): [{agent, label, status, status_num, latest_seq, latest_title}, ...]}
    """
    from src.api.routes.pipeline import AGENT_ORDER, AGENT_LABELS, STAGE_STATUS_MAP

    out: dict = {}
    if not keys:
        return out

    # 批量查询 events（分 chunk 防 SQL 过长）
    _all_rows = []
    for i in range(0, len(keys), 200):
        _chunk = keys[i:i + 200]
        _ors = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
        _flat = [v for k in _chunk for v in k]
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT repo_key, issue_id, agent_type, status, seq, title "
                    "FROM events WHERE " + _ors + " ORDER BY seq ASC",
                    _flat
                )
                _all_rows.extend(c.fetchall())
        except Exception as e:
            log.warning(f"batch agent_statuses fetch failed: {e}")

    # 按 (repo_key, issue_id) 分组
    _events_by_key: dict = {}
    for row in _all_rows:
        _key = (row[0], row[1])
        _events_by_key.setdefault(_key, []).append(row)

    # 为每个 key 构建 per-agent 状态列表
    for key in keys:
        _issue_events = _events_by_key.get(key, [])
        _statuses = []
        for i, agent in enumerate(AGENT_ORDER):
            # row: (repo_key, issue_id, agent_type, status, seq, title)
            _agent_events = [e for e in _issue_events if e[2] == agent]
            if not _agent_events:
                _statuses.append({
                    "agent": agent,
                    "label": AGENT_LABELS[i],
                    "status": "pending",
                    "status_num": 0,
                    "latest_seq": None,
                    "latest_title": "",
                })
            else:
                _latest = _agent_events[-1]  # seq ASC → 最后一条即最新
                _status = _latest[3] or "pending"
                _statuses.append({
                    "agent": agent,
                    "label": AGENT_LABELS[i],
                    "status": _status,
                    "status_num": STAGE_STATUS_MAP.get(_status, 0),
                    "latest_seq": _latest[4],
                    "latest_title": _latest[5] or "",
                })
        out[key] = _statuses

    return out


def _query_tasks(where: str, params: list, order_by: str, include_user_id: bool = False) -> list[dict]:
    """查询 tasks 并富化（scene / stage / is_running）为对外 dict（my_tasks / repo_all_tasks 共享）。

    - where / params / order_by 由调用方构造（列与排序方向经白名单，值参数化，防注入）
    - include_user_id: 结果是否包含 user_id（repo_all 需要）
    - issue_state / agent_tasks 均批量查询，避免逐任务 N+1
    """
    _select = (
        "SELECT id, assignee, repo_key, issue_number, title, task_description, conversation_id, "
        "status, completed_reason, created_at, completed_at, is_subtask_mode, parent_issue_number, parent_issue_id"
        + (", user_id" if include_user_id else "")
    )
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"{_select} FROM issue WHERE {where} ORDER BY {order_by}", params)
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"Task query failed: {e}")
            rows = []
    if not rows:
        return []

    from src.repo_key import to_repo_key, from_repo_key
    from src.api.pipeline_config import get_pipeline_config

    _keys = [(r[2] or "", f"#{r[3]}") for r in rows]
    _stage_map = _batch_fetch_issue_state(_keys)
    _running = _batch_running_agent_tasks(_keys)
    _agent_status_map = _batch_fetch_agent_statuses(_keys)

    tasks_out = []
    for idx, r in enumerate(rows):
        _rk, _issue_id = _keys[idx]
        _st = _stage_map.get((_rk, _issue_id))
        _issue_scene_id = (_st.get("scene_id") or "") if _st else ""
        _stage = (_st.get("stage") or "") if _st else ""
        if _issue_scene_id:
            _scene_id = _issue_scene_id
            _scene_source = "vod"  # VOD 识别
        else:
            # 无 Issue 级场景 → 仓库默认场景（原 get_issue_scene 在 scene_id 为空时返回空串，等价）
            _scene_id = ""
            _scene_source = "repo_default"
        _scene_name = ""
        try:
            _cfg = get_pipeline_config(_scene_id, repo_key=_rk)
            if _cfg:
                _scene_name = _cfg.get("name", _scene_id)
        except Exception:
            pass
        _has_running_agent = bool(r[3]) and _stage in ("vod", "issue", "coding", "qa", "sre") and ((_rk, _issue_id) in _running)
        item = {
            "id": r[0], "assignee": r[1], "repo_key": from_repo_key(r[2]), "issue_number": r[3],
            "title": r[4] or "", "task_description": r[5] or "",
            "conversation_id": r[6] or "", "status": r[7],
            "completed_reason": r[8] or "", "created_at": normalize_event_time(r[9]), "completed_at": normalize_event_time(r[10]),
            "is_subtask_mode": r[11] or 0,
            "parent_issue_number": r[12] or 0,
            "parent_issue_id": r[13] or "",
            "scene_id": _scene_id,
            "scene_name": _scene_name,
            "scene_source": _scene_source,
            "stage": _stage,
            "is_running": _has_running_agent,
            "agent_statuses": _agent_status_map.get((_rk, _issue_id), []),
        }
        if include_user_id:
            item["user_id"] = r[14] or 0
        tasks_out.append(item)
    return tasks_out
