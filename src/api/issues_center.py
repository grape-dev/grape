"""需求中心（Issues Center）领域逻辑（issues_center）。

从 src/api/routes/tasks.py 拆分（tasks-module-refactor-and-task-id-disambiguation-design.md）：
- issue 建档/状态流转/VCS 创建/附件/任务对话/平台映射/需求列表查询等「需求中心」领域函数集中于此，
- routes/tasks.py 只保留「执行实例」动作（complete/current），其他路由与业务模块均从本模块导入，
- 本模块为叶子：**不得 import routes.tasks / routes.issues**（防环形依赖）。
"""
import asyncio
import os
from datetime import datetime

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key
from src.api.time_utils import normalize_event_time
from src.config import log, WORKSPACE_DIR
from src.session_manager import db, db_lock, get_chat_config, create_conversation, add_message

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


class TaskAwaitingInputBody(BaseModel):
    questions: str = ""
    reason: str = ""


class TaskPendingReviewBody(BaseModel):
    review_points: str = ""
    references: str = ""


class TaskCloseBody(BaseModel):
    repo_key: str = ""
    issue_number: int = 0


def _get_user_id_by_gitcode(gitcode_user: str) -> int | None:
    """按 GitCode 用户名查平台 user_id（unified_user_mapping.user_id）。

    查找失败（未注册或异常）时记录 warning 日志，便于追溯（Issue #178）。
    """
    if not gitcode_user:
        return None
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT user_id FROM unified_user_mapping WHERE gitcode_user=? AND user_id>0 LIMIT 1", (gitcode_user,))
            row = c.fetchone()
            if row:
                return row[0]
            log.warning(f"_get_user_id_by_gitcode: gitcode_user='{gitcode_user}' not found in unified_user_mapping (user_id will be 0)")
            return None
        except Exception as e:
            log.warning(f"_get_user_id_by_gitcode: query failed for '{gitcode_user}': {e}")
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


def _get_feishu_open_id_by_user_id(user_id: int) -> str:
    """按平台 user_id 查飞书 open_id（等用户状态通知的 open_id 定位）。

    A2A/流水线产生的需求卡 `assignee` 常为空（归属人由 user_id 承载），飞书 open_id 应随 user_id 查询，
    而非 GitCode 用户名（assignee），否则 assignee 为空时通知静默丢失（feishu-openid-by-user-id-design.md）。
    返回 open_id（空串=未配置/缺失）。
    """
    if not user_id:
        return ""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=? AND feishu_open_id!='' LIMIT 1",
                (user_id,))
            row = c.fetchone()
            return row[0] if row else ""
        except Exception:
            return ""


def _ensure_assignee_task_card(user_id: int, assignee: str, repo_key: str, issue_number: int,
                               title: str, task_description: str) -> int | None:
    """为「指定 assignee + Issue」建/复用任务中心卡片（active）；区别于移除的 create，仅服务 notify 分配语义。

    2026-10-06：按 (repo_key, issue_number) 去重复用（任何状态），避免 awaiting/completed 卡导致 notify 另建新卡；
    复用卡时同步 assignee/user_id。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, assignee, user_id FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (repo_key, issue_number)
        )
        row = c.fetchone()
        if row:
            card_id, _existing_asg, _existing_uid = row[0], row[1] or "", row[2] or 0
            if _existing_asg != assignee or (_existing_uid or 0) != (user_id or 0):
                c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (assignee, user_id, card_id))
                db.commit()
            return card_id
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id) "
            "VALUES (?,?,?,?,?,?,'active',?,?,?)",
            (user_id, assignee, repo_key, issue_number, title, task_description,
             _get_issue_subtask_mode(repo_key, issue_number), _get_issue_parent(repo_key, issue_number),
             _get_issue_parent_id(repo_key, issue_number))
        )
        db.commit()
        return c.lastrowid


def _build_task_conversation_title(issue_number: int, title: str = "") -> str:
    """生成任务会话标题：`Issue #N · {title}`（title 空回退 `Issue #N`）。

    与需求中心看板卡片标题风格一致；超长截断保护（title 部分上限 80 字符）。
    """
    _MAX_TITLE_LEN = 80
    clean_title = (title or "").strip()
    if clean_title:
        if len(clean_title) > _MAX_TITLE_LEN:
            clean_title = clean_title[:_MAX_TITLE_LEN]
        return f"Issue #{issue_number} · {clean_title}"
    return f"Issue #{issue_number}"


def _ensure_task_conversation(user_id: int, repo_key: str, issue_number: int,
                              title: str = "", description: str = "",
                              write_context_on_reuse: bool = False) -> str:
    """为任务查找/复用「Issue #N · {title}」对话（任务中心「去对话」入口）。

    - 复用：同用户已有该 Issue 对话（按 conv_type='task' + issue_number 列匹配，
      旧格式 `Issue #N` 字符串作回退）→ 补 conv_type/issue_number 标记；
      write_context_on_reuse=True 时再次写入上下文消息（notify 语义：每次分配都刷上下文）
    - 存量升级：复用命中旧标题 `Issue #N` 的会话时刷新为新格式（不覆盖用户自定义标题）
    - 新建：create_conversation(conv_type="task") 并写入任务上下文消息。
    - 失败返回空串（不抛异常，旁路不影响任务建档）。
    """
    from src.session_manager import get_user_conversations, create_conversation, update_conversation, add_message
    conv_id = ""
    new_title = _build_task_conversation_title(issue_number, title)
    old_title = f"Issue #{issue_number}"
    try:
        convs = get_user_conversations(user_id, repo_key=repo_key, limit=50)
        matched = None
        for conv in convs:
            _ct = conv.get("conv_type") or ""
            _in = conv.get("issue_number") or 0
            if _ct == "task" and _in == issue_number:
                matched = conv
                break
        if matched is None:
            for conv in convs:
                if conv.get("title") == old_title:
                    matched = conv
                    break
        if matched is not None:
            conv_id = matched["id"]
            update_conversation(conv_id, conv_type="task", issue_number=issue_number)
            if (matched.get("title") or "") == old_title:
                update_conversation(conv_id, title=new_title)
            if write_context_on_reuse:
                ctx_msg = _build_task_context_message(title, description, repo_key, issue_number)
                add_message(conv_id, "agent", ctx_msg)
        else:
            conv_id = create_conversation(user_id, repo_key, title=new_title,
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
                      subtask_mode: int = -1,
                      sender: str = "",
                      org_id: int = 0) -> int | None:
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
    # org_id 未显式传入时，从 managed_repos 表推断仓库关联的组织
    if not org_id:
        try:
            from src.session_manager import db as _org_db, db_lock as _org_db_lock
            with _org_db_lock:
                _oc = _org_db.cursor()
                _oc.execute("SELECT org_id FROM managed_repos WHERE repo_key=? ORDER BY org_id DESC LIMIT 1", (repo_key,))
                _or = _oc.fetchone()
                if _or and _or[0]:
                    org_id = _or[0]
                else:
                    log.info(f"ensure_issue_task: managed_repos has no org_id for repo_key={repo_key}, org_id stays 0")
        except Exception as _e:
            log.warning(f"ensure_issue_task: failed to infer org_id from managed_repos for {repo_key}: {_e}")
    with db_lock:
        c = db.cursor()
        # 幂等去重：同一平台 issue 唯一一张卡，任何状态都复用（2026-10-06 修复：
        # 此前仅复用 active/processing，awaiting_input/completed/cancelled 时后续事件会新增一行 → 重复卡片）
        c.execute(
            "SELECT id, conversation_id, status FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id LIMIT 1",
            (repo_key, issue_number)
        )
        row = c.fetchone()
        if row:
            task_id = row[0]
            linked = row[1]
            _cur_st = row[2] or ""
            # 复用路径：如果已有行 org_id=0 但推断出 org_id>0，则回补（session_store 先建行的 org_id 为 0）
            if org_id:
                c.execute("UPDATE issue SET org_id=? WHERE id=? AND (org_id=0 OR org_id IS NULL)", (org_id, task_id))
            if _cur_st not in ("active", "processing"):
                # 复用终态/待澄清卡：重新武装为 active（继续在任务中心处理，避免另建新卡）
                c.execute("UPDATE issue SET status='active', completed_reason=NULL, completed_at=NULL WHERE id=?",
                          (task_id,))
            if title:
                c.execute("UPDATE issue SET title=? WHERE id=?", (title, task_id))
            if subtask_mode >= 0:
                c.execute("UPDATE issue SET is_subtask_mode=? WHERE id=?", (subtask_mode, task_id))
            # 如果传入的 assignee 与现有不同，更新 assignee 并关闭旧 assignee 的任务
            # （#15 修复：复用已有任务时同步 assignee，防止 assignee 与新建 Issue 不匹配）
            _existing_assignee = ""
            _existing_user_id = 0
            try:
                c.execute("SELECT assignee, user_id FROM issue WHERE id=?", (task_id,))
                _r2 = c.fetchone()
                if _r2:
                    _existing_assignee = _r2[0] or ""
                    _existing_user_id = _r2[1] or 0
            except Exception:
                pass
            # 归属回补：历史/先行建档行 user_id=0 且 assignee 现可映射 → 修正（issues-mine-user-id-zero-fix-design.md）
            if not _existing_user_id and target:
                _uid_now = _get_user_id_by_gitcode(target) or 0
                if _uid_now:
                    c.execute("UPDATE issue SET user_id=? WHERE id=?", (_uid_now, task_id))
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
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id, org_id) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?,?)",
                (user_id, target, repo_key, issue_number, title, description,
                 _subtask_val, _parent_val, _get_issue_parent_id(repo_key, issue_number), org_id)
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
    """换人：不再把需求标完成（reassigned 语义错位，见 reassign-takeover-no-complete-design.md）。

    单行模型（issue.(repo_key, issue_number) UNIQUE）下 assignee<>keep 基本不命中；
    责任转移由 _ensure_assignee_task_card 更新 assignee 承担。保持 no-op 幂等，不触碰需求状态。
    """
    return 0


def _close_issue_tasks_bot_takeover(repo_key: str, issue_number: int) -> int:
    """bot 接管：不把需求标完成（bot_takeover 语义错位，见 reassign-takeover-no-complete-design.md）。

    接管语义 = bot 自己继续处理，绝不完结需求：
    - stage='done'（本已终态）→ 早退不改状态（已完成的保持 completed，不翻回不重标）；
    - stage='pending_review' → 保持 pending_review；
    - 无 running agent 且活跃 → pending_review 等待开发者确认；
    - 有 agent 在 running → 不修改，等 agent 完成后再判定。
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
            # 本已终态：bot 接管不改变完成状态（防止重复完成 / 翻活已完成需求）
            return 0
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


def _resume_issue_waiting(repo_key: str, issue_number: int) -> bool:
    """「等用户」双态恢复（幂等）：`awaiting_input / pending_review → processing`。

    供两条路径复用（issue-explicit-awaiting-input-api-design.md §3.6 / issue-pending-review-explicit-design.md §3.5）：
    - webhook `issue_comment` 自动恢复（非 bot 用户评论）；
    - `issues clarify` / Agent 显式续接。
    仅当 issue 正处于 `awaiting_input` 或 `pending_review` 时翻回，返回是否真正翻转（幂等、可观测）。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('awaiting_input','pending_review')",
                (repo_key, issue_number))
            db.commit()
            _changed = c.rowcount > 0
        if _changed:
            log.info(f"Issue waiting resume: {repo_key}/#{issue_number} awaiting/pending_review → processing")
        return _changed
    except Exception as _e:
        log.warning(f"resume issue from waiting failed {repo_key}/#{issue_number}: {_e}")
        return False


def _set_issue_pending_review(repo_key: str, issue_number: int) -> bool:
    """Agent 显式设置阶段审核点（幂等）：`active/processing/awaiting_input → pending_review`，不改 stage。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE issue SET status='pending_review', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing','awaiting_input')",
                (repo_key, issue_number))
            db.commit()
            _changed = c.rowcount > 0
        if _changed:
            log.info(f"Issue pending_review set: {repo_key}/#{issue_number}")
        return _changed
    except Exception as _e:
        log.warning(f"set issue pending_review failed {repo_key}/#{issue_number}: {_e}")
        return False


def _is_last_stage_node(repo_key: str, issue_number: int) -> bool:
    """当前 issue.stage 是否为流水线末节点（next=='done'）。

    与 complete_task 末节点判定（service.py:1685-1687）同源复用 `next=='done'` 判据；
    stage 不可推定 / 未知 / 已终态 → 保守返回 False（review-pass 归一兜底用）。
    """
    try:
        from src.api.session_store import get_issue_stage, get_issue_scene
        from src.api.pipeline_config import get_pipeline_config
        _issue_id = f"#{issue_number}"
        _st = get_issue_stage(repo_key, _issue_id) or {}
        _cur = (_st.get("stage") or "").strip()
        if not _cur or _cur in ("done", "pending_review"):
            return False
        _scene = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _cfg = get_pipeline_config(_scene_id, repo_key=repo_key) or {}
        for _n in (_cfg.get("nodes") or []):
            if _n.get("id") == _cur:
                return _n.get("next") == "done"
        return False
    except Exception:
        return False


def _complete_issue_done(repo_key: str, issue_number: int, evidence: str,
                         statuses: tuple = ("pending_review",)) -> bool:
    """把给定 status 集合内的问题置 completed + stage=done（合入/收尾）。返回是否变更。"""
    with db_lock:
        c = db.cursor()
        _ph = ",".join("?" * len(statuses))
        c.execute(
            f"UPDATE issue SET status='completed', completed_reason='review_passed', completed_at=datetime('now') "
            f"WHERE repo_key=? AND issue_number=? AND status IN ({_ph})",
            (repo_key, issue_number, *statuses))
        db.commit()
        _changed = c.rowcount > 0
    if _changed:
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, f"#{issue_number}", "done", "agent")
        except Exception as _se:
            log.warning(f"review_pass stage=done failed {repo_key}/#{issue_number}: {_se}")
        log.info(f"Issue review-pass(done): {repo_key}/#{issue_number} evidence={evidence[:60]}")
    return _changed


def _review_pass(repo_key: str, issue_number: int, target: str = "processing",
                 evidence: str = "") -> str:
    """Agent 判定用户通过审核（幂等）。

    - target=done：pending_review → completed+done（合入/收尾）；
      **末节点放宽（#2266）**：若当前 stage 为流水线末节点（next=='done'），status 处于
      active/processing/awaiting_input 也放行 done（CLI 流水线可从未进入 pending_review，
      如 PR 已合入但需求卡 active → 原路径 noop）。防中间节点误终态：非末节点仍仅 pending_review 可退出。
    - target=processing：仅 pending_review → processing（继续流水线）；末节点触达则归一 done
      （review-pass-last-node-force-done-design.md，无下游可续）。
    - 非命中 → noop。
    """
    try:
        if target == "done":
            if _is_last_stage_node(repo_key, issue_number):
                # 末节点：兼容 pending_review + 放宽 active/processing/awaiting_input（方向 A）
                if _complete_issue_done(repo_key, issue_number, evidence,
                                        ("pending_review", "active", "processing", "awaiting_input")):
                    return "passed_done"
                return "noop"
            # 非末节点：保持原契约（仅 pending_review），防中间节点误终态
            return "passed_done" if _complete_issue_done(repo_key, issue_number, evidence,
                                                          ("pending_review",)) else "noop"
        if _is_last_stage_node(repo_key, issue_number):
            # 末节点 + processing → 归一 done（无下游可续）
            log.info(f"Issue review-pass normalized last-node target=processing→done: "
                     f"{repo_key}/#{issue_number} evidence={evidence[:60]}")
            return "passed_done" if _complete_issue_done(repo_key, issue_number, evidence,
                                                          ("pending_review", "active", "processing", "awaiting_input")) else "noop"
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='pending_review'",
                (repo_key, issue_number))
            db.commit()
            _changed = c.rowcount > 0
        if _changed:
            log.info(f"Issue review-pass(processing): {repo_key}/#{issue_number} evidence={evidence[:60]}")
            return "passed_processing"
        return "noop"
    except Exception as _e:
        log.warning(f"review_pass failed {repo_key}/#{issue_number}: {_e}")
        return "noop"


def _review_reject(repo_key: str, issue_number: int, target: str = "processing",
                   reason: str = "") -> str:
    """Agent 判定用户未通过审核（幂等）：`pending_review → processing|awaiting_input` 打回/转澄清。"""
    try:
        _t = target if target in ("processing", "awaiting_input") else "processing"
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE issue SET status=?, completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='pending_review'",
                (_t, repo_key, issue_number))
            db.commit()
            _changed = c.rowcount > 0
        if _changed:
            log.info(f"Issue review-reject({_t}): {repo_key}/#{issue_number} reason={reason[:60]}")
            return f"rejected_{_t}"
        return "noop"
    except Exception as _e:
        log.warning(f"review_reject failed {repo_key}/#{issue_number}: {_e}")
        return "noop"


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


def _build_notify_actions(repo_key: str, issue_number: int, conv_id: str) -> list:
    """通知卡快捷按钮：进入任务会话（cmd 直达）+ 查看 Issue（VCS 公开页只读）。

    设计：docs/weekly-05/feishu-notification-card-quick-actions-design.md。
    """
    try:
        from src.repo_key import split_owner_repo
        owner, repo = split_owner_repo(repo_key)
    except Exception:
        owner, repo = "", ""
    _iss = f"#{issue_number}" if issue_number else ""
    actions = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "💬 进入任务会话"},
        "value": {"cmd": "/task-open", "repo_key": repo_key,
                  "issue_number": _iss, "conv_id": conv_id or ""},
    }]
    if owner and repo and _iss:
        base = "https://github.com" if str(repo_key).startswith("github:") else "https://gitcode.com"
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看 Issue ↗"},
            "url": f"{base}/{owner}/{repo}/issues/{_iss.lstrip('#')}",
        })
    return actions


_IM_NOTIFY_TEMPLATES = {
    "task": "blue",
    "awaiting": "orange",
    "pending_review": "red",
}
_IM_NOTIFY_TITLES = {
    "task": "📋 任务通知",
    "awaiting": "❓ 需求待澄清",
    "pending_review": "🔎 需求待审核",
}


def _build_notify_fields(repo_key: str, issue_number: int) -> list:
    """通知卡结构化键值对字段：仓库 / Issue（替代纯文本行，移动端对齐更好）。"""
    from src.repo_key import from_repo_key
    _iss = f"#{issue_number}" if issue_number else "—"
    return [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**📦 仓库**\n{from_repo_key(repo_key) or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**🎯 Issue**\n{_iss}"}},
    ]


def _notify_note() -> str:
    """通知卡底部时间戳（灰字注释）。"""
    return f"发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"


_IM_NOTIFY_COUNTERS: dict[str, int] = {}


def _im_notify_counter(key: str) -> None:
    """通知计数器（cluster-im-lease-stability-design.md §3.3）。"""
    _IM_NOTIFY_COUNTERS[key] = _IM_NOTIFY_COUNTERS.get(key, 0) + 1


def get_im_notify_counters() -> dict:
    return dict(_IM_NOTIFY_COUNTERS)


def _im_notify_begin(ntype: str, repo_key, issue_number, user_id, open_id, ctx):
    _im_notify_counter("attempted")
    _ctx_name = getattr(ctx, "name", "") if ctx else ""
    log.info(f"[im-notify] type={ntype} repo={repo_key} issue=#{issue_number} "
             f"user={user_id} open_id={open_id or ''} ctx={_ctx_name or 'none'}")


def _im_notify_skip(ntype: str, issue_number, user_id, ctx, open_id):
    if not ctx:
        _reason = "no_ctx"
    elif not open_id:
        _reason = "no_openid"
    else:
        _reason = "no_api"
    _im_notify_counter(f"skipped_{_reason}")
    log.warning(f"[im-notify] type={ntype} issue=#{issue_number} user={user_id} reason={_reason}")


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
        _im_notify_begin("task", repo_key, issue_number, user_id, open_id, ctx)
        if open_id and api and ctx:
            notify_text = (f"你有一条新任务待处理。\n\n{task_description}"
                           if (task_description or "").strip() else "你有一条新任务待处理。")
            try:
                await api.send_card_to_user(
                    ctx, open_id, notify_text, title=_IM_NOTIFY_TITLES["task"],
                    template=_IM_NOTIFY_TEMPLATES["task"],
                    actions=_build_notify_actions(repo_key, issue_number, conv_id),
                    fields=_build_notify_fields(repo_key, issue_number),
                    note=_notify_note())
                _im_notify_counter("sent_ok")
            except Exception as e:
                _im_notify_counter("sent_failed")
                log.warning(f"Task notify feishu failed: {e}")
        else:
            _im_notify_skip("task", issue_number, user_id, ctx, open_id)
    except Exception as e:
        log.warning(f"Task notify _run_notify error: {e}")


async def _run_awaiting_notify(ctx, api, user_id: int, conv_id: str, open_id: str,
                               questions: str, repo_key: str, issue_number: int, cfg: dict):
    """待澄清通知：仅静态模板消息 + 飞书卡片，不触发 ChatAgent / 不跑 LLM（与 _run_notify 同规约）。

    进入 awaiting_input 的语义就是「等用户输入」，必须主动触达用户；fd：本人 chat bot（open_id 为 app 级）。
    """
    try:
        from src.repo_key import from_repo_key
        if conv_id:
            try:
                _tpl = (f"❓ 需要你补充信息 · Issue #{issue_number}\n\n"
                        f"仓库: {from_repo_key(repo_key)}\n\n"
                        f"{questions}")
                add_message(conv_id, "agent", _tpl)
                log.info(f"Task awaiting: questions written to conv={conv_id} (no chat-agent)")
            except Exception as e:
                log.warning(f"Task awaiting: template write failed: {e}")

        _im_notify_begin("awaiting", repo_key, issue_number, user_id, open_id, ctx)
        if open_id and api and ctx:
            notify_text = (f"Agent 需要你补充以下信息：\n\n{questions}"
                           if (questions or "").strip() else "Agent 需要你补充信息。")
            try:
                await api.send_card_to_user(
                    ctx, open_id, notify_text, title=_IM_NOTIFY_TITLES["awaiting"],
                    template=_IM_NOTIFY_TEMPLATES["awaiting"],
                    actions=_build_notify_actions(repo_key, issue_number, conv_id),
                    fields=_build_notify_fields(repo_key, issue_number),
                    note=_notify_note())
                _im_notify_counter("sent_ok")
            except Exception as e:
                _im_notify_counter("sent_failed")
                log.warning(f"Task awaiting feishu failed: {e}")
        else:
            _im_notify_skip("awaiting", issue_number, user_id, ctx, open_id)
    except Exception as e:
        log.warning(f"Task awaiting _run_awaiting_notify error: {e}")


async def _run_pending_review_notify(ctx, api, user_id: int, conv_id: str, open_id: str,
                                     review_points: str, references: str,
                                     repo_key: str, issue_number: int, cfg: dict):
    """待审核通知：仅静态模板消息 + 飞书卡片，不触发 ChatAgent / 不跑 LLM（与 _run_awaiting_notify 同规约）。

    进入 pending_review 的语义就是「交付审核点请用户拍板」，必须主动触达用户；本机本人 chat bot（open_id 为 app 级）。
    """
    try:
        from src.repo_key import from_repo_key
        _refs_suffix = (f"\n\n参考: {references}" if (references or "").strip() else "")
        if conv_id:
            try:
                _tpl = (f"🔎 需要你审核确认 · Issue #{issue_number}\n\n"
                        f"仓库: {from_repo_key(repo_key)}\n\n"
                        f"{review_points}{_refs_suffix}")
                add_message(conv_id, "agent", _tpl)
                log.info(f"Issue pending-review: review points written to conv={conv_id} (no chat-agent)")
            except Exception as e:
                log.warning(f"Issue pending-review: template write failed: {e}")

        _im_notify_begin("pending_review", repo_key, issue_number, user_id, open_id, ctx)
        if open_id and api and ctx:
            notify_text = (f"请审核确认以下交付审核点：\n\n{review_points}{_refs_suffix}"
                           if (review_points or "").strip() else "请审核确认交付结果。")
            try:
                await api.send_card_to_user(
                    ctx, open_id, notify_text, title=_IM_NOTIFY_TITLES["pending_review"],
                    template=_IM_NOTIFY_TEMPLATES["pending_review"],
                    actions=_build_notify_actions(repo_key, issue_number, conv_id),
                    fields=_build_notify_fields(repo_key, issue_number),
                    note=_notify_note())
                _im_notify_counter("sent_ok")
            except Exception as e:
                _im_notify_counter("sent_failed")
                log.warning(f"Issue pending-review feishu failed: {e}")
        else:
            _im_notify_skip("pending_review", issue_number, user_id, ctx, open_id)
    except Exception as e:
        log.warning(f"Issue pending-review _run_pending_review_notify error: {e}")


def _resolve_user_chat_bot(bm, user_id: int):
    """按用户 chat_agent_session.im_app_id 取其本人 chat bot（open_id 为 app 级，须用本人 bot 发送）。

    用户未配置本人 chat bot → 返回 None（**不通知**，不回落其它 bot，避免用他人 app 发送静默失败）。
    可观测（cluster-im-lease-stability-design.md §3.1）：区分 no_bm / no_session_row /
    bot_not_in_memory（sticky-lease 场景）→ 让"未收到通知"有日志可查。
    """
    if not bm:
        log.info(f"[im-notify] resolve user={user_id} reason=no_bm")
        return None
    try:
        bots = bm.get_all_bots()
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT im_app_id FROM chat_agent_session WHERE user_id=? AND im_app_id!='' LIMIT 1",
                (user_id,)).fetchone()
        if not row or not row[0]:
            log.warning(f"[im-notify] resolve user={user_id} reason=no_session_row")
            return None
        bot = bots.get(row[0])
        if bot:
            log.info(f"[im-notify] resolve user={user_id} bot={row[0]} ok")
        else:
            log.warning(f"[im-notify] resolve user={user_id} bot={row[0]} reason=bot_not_in_memory hint=sticky-lease")
        return bot
    except Exception as e:
        log.warning(f"[im-notify] resolve user={user_id} reason=bot_error err={e}")
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

_TASK_STATUS_FLOWS = {
    "active": ("processing", "awaiting_input", "pending_review", "cancelled"),
    "processing": ("awaiting_input", "pending_review", "completed", "cancelled", "active"),
    "awaiting_input": ("processing", "pending_review", "cancelled"),
    "pending_review": ("processing", "completed", "cancelled"),
    "completed": (),
    "cancelled": ("active",),
}

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
    # task_id 语义 = 需求卡片 issue.id（issues_center P2：主字段 issue_id，兼容保留 task_id）
    return {"ok": True, "issue_id": task_id, "task_id": task_id,
            "issue_number": issue_num, "created": created,
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
    # grape_ 前缀的是用户级 API Key（CLI 用户），同样不是 GitCode token，不能走此路径
    _preauth_token = ""
    if user.get("auth_type") in ("api_key", "internal") and user.get("token"):
        _token_val = user["token"]
        if not _token_val.startswith("grape_"):
            _preauth_token = _token_val
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
    _org_id = user.get("org_id", 0)
    task_id = ensure_issue_task(repo_key, issue_num, body.title.strip(), body.description or "", assignee=assignee,
                                subtask_mode=_auto_subtask_mode, org_id=_org_id)
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

    # task_id 语义 = 需求卡片 issue.id（issues_center P2：主字段 issue_id，兼容保留 task_id）
    return {"ok": True, "issue_id": task_id, "task_id": task_id,
            "issue_number": issue_num, "created": created,
            "assignee": assignee, "is_subtask_mode": _auto_subtask_mode,
            "message": "任务已创建，Issue 已同步" if created else "任务已创建（复用已有 Issue）"}

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

def _batch_fetch_pr_issue_keys(keys: list) -> set:
    """批量查 events 表，返回 issue 关联了 PR 事件的 (repo_key, issue_id) 集合。

    跨仓库 A2A 场景中，agent 在目标仓库创建 PR（而非 Issue），
    issue 表存的 issue_number 实际是 PR 编号，前端链接应用 /pull/ 而非 /issues/。
    """
    from src.vcs.event_types import ALL_PR_EVENTS
    _pr_types = tuple(ALL_PR_EVENTS)
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
                _ph = ",".join("?" * len(_pr_types))
                c.execute(
                    f"SELECT DISTINCT repo_key, issue_id FROM events "
                    f"WHERE ({_ors}) AND event_type IN ({_ph})",
                    _flat + list(_pr_types))
                for row in c.fetchall():
                    out.add((row[0], row[1]))
        except Exception as e:
            log.warning(f"batch pr_issue_keys fetch failed: {e}")
    return out

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
    """查询 tasks 并富化（scene / stage / is_running / is_pr）为对外 dict（my_tasks / repo_all_tasks 共享）。

    - where / params / order_by 由调用方构造（列与排序方向经白名单，值参数化，防注入）
    - include_user_id: 结果是否包含 user_id（repo_all 需要）
    - issue_state / agent_tasks 均批量查询，避免逐任务 N+1
    - is_pr: 批量查 events 表 event_type 判断是否为 PR 事件（跨仓库 A2A 场景 issue_number 实际是 PR 编号）
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
    _pr_issue_keys = _batch_fetch_pr_issue_keys(_keys)

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
                _scene_name = _cfg.get("display_name") or _cfg.get("name", _scene_id)
        except Exception:
            pass
        _has_running_agent = bool(r[3]) and _stage in ("vod", "issue", "coding", "qa", "sre") and ((_rk, _issue_id) in _running)
        item = {
            "id": r[0], "assignee": r[1], "repo_key": r[2], "issue_number": r[3],
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
            "is_pr": (_rk, _issue_id) in _pr_issue_keys,
        }
        if include_user_id:
            item["user_id"] = r[14] or 0
        tasks_out.append(item)
    return tasks_out
