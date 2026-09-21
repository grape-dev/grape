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


def ensure_issue_task(repo_key: str, issue_number: int, title: str = "",
                      description: str = "", assignee: str = "",
                      event_type: str = "",
                      subtask_mode: int = -1,
                      sender: str = "") -> int | None:
    """Issue 事件幂等建档（任务中心全覆盖，task-center-issue-coverage-design.md §3-A）。

    - assignee 优先 → 对应平台 user_id
    - assignee 为空 → issue_state 已存 assignee
    - 仍为空 → webhook 操作用户（sender，Issue #178：创建人应为操作用户而非 owner）
    - 最后降级 → 仓库 owner（+ warning 日志）
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
    # Issue #49: assignee 为空时，优先从 issue_state 读取 assignee
    if not assignee:
        try:
            from src.api.session_store import get_issue_stage
            _st = get_issue_stage(repo_key, f"#{issue_number}")
            if _st and _st.get("assignee"):
                assignee = _st["assignee"]
        except Exception:
            pass
    # Issue #178: assignee 仍为空 → 使用 webhook 操作用户（sender），最后降级 owner
    if not assignee:
        if sender:
            assignee = sender
        else:
            assignee = owner or ""
            if owner:
                log.warning(f"ensure_issue_task: no assignee/sender for #{issue_number} repo={repo_key}, fallback to owner '{owner}'")
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
            if target and target != _existing_assignee:
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


class TaskCreateBody(BaseModel):
    repo_key: str = ""
    title: str = ""
    description: str = ""
    assignee: str = ""
    scene_id: str = ""
    is_subtask_mode: int = 0
    attachments: list[str] = []  # pre-upload 返回的 safe_name 列表，创建时按名单精确关联 pending 附件


# 任务状态迁移规则（Issue #85: 增加 awaiting_input + pending_review）


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
    # _preauth_token: autopilot 等内部调用（auth_type=api_key/internal）预注入 bot token
    # JWT 登录用户的 token 是 session JWT 不是 GitCode API token，不能走此路径
    _preauth_token = ""
    if user.get("auth_type") in ("api_key", "internal") and user.get("token"):
        _preauth_token = user["token"]
    if _preauth_token:
        api_token = _preauth_token
    elif _is_github:
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
    # Issue #202: 不再强制阻断同名需求——允许用户提交同名需求。
    # 去除 409 幂等拦截：无论是否已存在同名 open Issue，都继续创建新 Issue。
    issue_num = None
    created = False
    # 拼接 Task Center 上传的附件到 issue body（按 attachments 名单精确读取，避免并发串附件）
    _desc_with_attach = (body.description or "").strip()
    _pending_attachments = _attach_list(owner, rname, "pending")
    if body.attachments:
        _want = set(body.attachments)
        _pending_attachments = [a for a in _pending_attachments if a["filename"] in _want]
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
    elif _preauth_token:
        resp = _post_gitcode_issue(api_token, owner, rname, body.title.strip(),
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
    if body.attachments:
        _want = set(body.attachments)
        _pending_atts = [a for a in _pending_atts if a["filename"] in _want]
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
            elif _preauth_token:
                _patch_issue_body(api_token, owner, rname, issue_num, (body.description or "") + _marker)
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

