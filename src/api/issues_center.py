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
    # Issue #667：卡片 id 缺行时的兜底解析——GitCode 侧 Issue number（会话仓库作用域内），
    # 服务端据此复用/补建内部需求卡行，避免同步缺口导致 404 阻断澄清流程。
    issue_number: int = 0


class TaskPendingReviewBody(BaseModel):
    review_points: str = ""
    references: str = ""


class TaskCloseBody(BaseModel):
    repo_key: str = ""
    issue_number: int = 0


def _get_user_id_by_gitcode(gitcode_user: str, repo_key: str = "") -> int | None:
    """按平台用户名查 user_id（unified_user_mapping.user_id）。

    查找失败（未注册或异常）时记录 warning 日志，便于追溯（Issue #178）。
    优先返回「登录账号（grape_accounts）id」——unified_user_mapping 可能存在新旧双 id 映射
    （低 id 遗留 + grape_accounts id），若取到非登录 id 会导致 issue.user_id 与请求用户不符、
    「我的需求」按 user_id 过滤为空（issue-244 关联事故）。无登录账号时回退任意映射。

    FR-11.2: 按 repo_key 的 provider 分查对应列（gitcode_user / github_login），
    避免 OR 扫两列导致空串交叉命中。repo_key 为空时降级为 gitcode_user 查询（向后兼容）。
    """
    if not gitcode_user:
        return None
    with db_lock:
        c = db.cursor()
        try:
            # FR-CQ-1: 提取为 um_column_for_repo 工具函数
            from src.repo_key import um_column_for_repo
            _col = um_column_for_repo(repo_key)
            row = c.execute(
                f"SELECT um.user_id FROM unified_user_mapping um "
                f"WHERE um.{_col}=? AND um.user_id>0 "
                f"AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id=um.user_id AND ga.status=1) "
                f"ORDER BY um.user_id LIMIT 1", (gitcode_user,)).fetchone()
            if not row:
                row = c.execute(
                    f"SELECT user_id FROM unified_user_mapping WHERE {_col}=? AND user_id>0 LIMIT 1",
                    (gitcode_user,)).fetchone()
            if row:
                return row[0]
            log.warning(f"_get_user_id_by_gitcode: gitcode_user='{gitcode_user}' not found in unified_user_mapping (user_id will be 0)")
            return None
        except Exception as e:
            log.warning(f"_get_user_id_by_gitcode: query failed for '{gitcode_user}': {e}")
            return None


def _get_feishu_open_id(gitcode_user: str, repo_key: str = "") -> str:
    """按平台用户名查飞书 open_id。

    FR-17.2: 有 repo_key 时按 provider 分查对应列；无则只查 gitcode_user（调用方从 grape_accounts.username 取值）。
    """
    if not gitcode_user:
        return ""
    # FR-CQ-1: 提取为 um_column_for_repo 工具函数
    from src.repo_key import um_column_for_repo
    _col = um_column_for_repo(repo_key)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT feishu_open_id FROM unified_user_mapping WHERE {_col}=? AND feishu_open_id!='' LIMIT 1", (gitcode_user,))
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


def _get_gitcode_user_by_user_id(user_id: int) -> str:
    """按 user_id 反查平台用户名（gitcode_user 优先，github_login 兜底）。

    Issue #1337: 建 issue 卡片时回填 created_by 用；查不到返回空串（归入「未归属」）。
    """
    if not user_id:
        return ""
    with db_lock:
        c = db.cursor()
        try:
            row = c.execute(
                "SELECT COALESCE(NULLIF(gitcode_user,''), NULLIF(github_login,''))"
                " FROM unified_user_mapping WHERE user_id=? AND user_id>0 LIMIT 1",
                (user_id,)).fetchone()
            return (row[0] or "") if row else ""
        except Exception as e:
            log.warning(f"_get_gitcode_user_by_user_id({user_id}) failed: {e}")
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
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id, created_by) "
            "VALUES (?,?,?,?,?,?,'active',?,?,?,?)",
            (user_id, assignee, repo_key, issue_number, title, task_description,
             _get_issue_subtask_mode(repo_key, issue_number), _get_issue_parent(repo_key, issue_number),
             _get_issue_parent_id(repo_key, issue_number), _get_gitcode_user_by_user_id(user_id))
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


def _resolve_issue_org_id(repo_key: str, explicit_org: int = 0) -> int:
    """解析需求归属组织（issue-244-org-attribution-fix-design.md 方案 B）。

    - repo 关联需求（repo_key 非空）：org = `managed_repos.org_id`（仓库权威，单一来源），
      忽略调用方/请求上下文 org——requests 的 user.org_id 只作「查看过滤」，绝不作写库归属。
    - 组织级需求（未来，repo_key 为空且有 explicit_org）：org = 显式选定的组织，
      须由调用方先完成归属性校验（org_member/创建权限），本函数不代校验。
    - 无托管记录 / 无显式 org → 0（个人视图豁免 org 过滤，创建者仍可见）。
    """
    if repo_key:
        from src.user.org_db import get_org_id_by_repo_key
        return get_org_id_by_repo_key(repo_key) or 0
    return explicit_org or 0


def ensure_issue_task(repo_key: str, issue_number: int, title: str = "",
                      description: str = "", assignee: str = "",
                      event_type: str = "",
                      subtask_mode: int = -1,
                      sender: str = "",
                      org_id: int = 0,
                      owner_user_id: int = 0,
                      creator: str = "") -> int | None:
    """Issue 事件幂等建档（任务中心全覆盖，task-center-issue-coverage-design.md §3-A）。

    - assignee 优先 → 对应平台 user_id
    - assignee 为空 → issue_state 已存 assignee
    - 仍为空 → webhook 操作用户（sender，Issue #178：创建人应为操作用户而非 owner）
    - 最后降级 → 仓库 owner（+ warning 日志）
    - assignee/owner 未注册平台（无 unified_user_mapping）→ user_id=0 落库；
      任务中心 mine 查询 `user_id=? OR user_id=0` 展示（P2 拍板修订：展示，不隐藏）
    - 幂等：同一 issue 已存在 active/processing 任务则复用（2026-09-03 修复#15：不再按 assignee 区分，
      防止 webhook 回声导致同一 issue 产生两个不同 assignee 的任务）
    - Issue #805：creator（创建人 login）落库 created_by；复用路径仅空值回填（幂等不覆盖），
      assignee 流转不改变创建人。
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
    # 显式创建身份优先 > assignee/sender/owner 反查（issue-card-owner-attribution-fix-design.md）
    user_id = owner_user_id if owner_user_id > 0 else (_get_user_id_by_gitcode(target, repo_key) if target else 0)
    if user_id is None:
        # 未注册平台用户 → user_id=0（保留原始 assignee 名字，不 fallback 到 owner）
        user_id = 0
    # org 归属 = 仓库托管组织（repo 关联需求的唯一权威，issue-244-org-attribution-fix-design.md 方案 B）。
    # 不再信任调用方传入的任意 org_id（如请求上下文 user.org_id），杜绝 #244 式串货漂移；
    # 调用方传入的 org_id 仅与仓库值比对，不一致时告警并校准为仓库值。
    from src.user.org_db import get_org_id_by_repo_key
    _repo_org = get_org_id_by_repo_key(repo_key) or 0
    if org_id and org_id != _repo_org:
        log.warning(f"ensure_issue_task: org drift rejected repo={repo_key} issue=#{issue_number} "
                    f"passed_org={org_id} → repo_org={_repo_org}")
    org_id = _repo_org
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
            # 复用路径：已有行 org 与仓库托管 org 不一致（0/缺失/旧错误值）一律校准为仓库 org
            #（issue-244-org-attribution-fix-design.md：org 唯一权威 = managed_repos.org_id）
            if org_id:
                c.execute(
                    "UPDATE issue SET org_id=? WHERE id=? AND (org_id IS NULL OR org_id != ?)",
                    (org_id, task_id, org_id))
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
            # 归属校准：现有值为 0 或「默认/承载账号」且与解析值不一致 → 校准为解析值
            # （issue-card-owner-attribution-fix-design.md：只修 0 修不掉非 0 错误归属）
            _DEFAULT_OWNER_UID = 1  # 默认/承载账号 id（可调；与历史「空 assignee 命中默认账号」现场一致）
            if (user_id > 0 and _existing_user_id not in (None,)
                    and _existing_user_id in (0, _DEFAULT_OWNER_UID)
                    and _existing_user_id != user_id):
                c.execute("UPDATE issue SET user_id=? WHERE id=?", (user_id, task_id))
                log.info(f"ensure_issue_task: ownership calibrated issue #{issue_number} "
                         f"user_id {_existing_user_id}->{user_id}")
            if target and target != _existing_assignee:
                c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (target, user_id, task_id))
                try:
                    _close_other_assignee_tasks(repo_key, issue_number, keep_assignee=target)
                except Exception:
                    pass
            # Issue #805: 创建人幂等补写——仅 created_by 为空时回填，绝不覆盖已有创建人
            _creator_val = (creator or "").strip()
            if _creator_val:
                c.execute(
                    "UPDATE issue SET created_by=? WHERE id=? AND (created_by IS NULL OR created_by='')",
                    (_creator_val, task_id))
            db.commit()
        else:
            if not title:
                title = _get_issue_title(repo_key, issue_number)
            if not description:
                description = f"{event_type or 'Issue'}：请在任务中心查看并处理。"
            _subtask_val = subtask_mode if subtask_mode >= 0 else _get_issue_subtask_mode(repo_key, issue_number)
            _parent_val = _get_issue_parent(repo_key, issue_number)
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, is_subtask_mode, parent_issue_number, parent_issue_id, org_id, created_by) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)",
                (user_id, target, repo_key, issue_number, title, description,
                 _subtask_val, _parent_val, _get_issue_parent_id(repo_key, issue_number), org_id,
                 (creator or "").strip())
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


# 进入 pending_review 时可能写入的哨兵 owner_agent（非真实流水线节点）；
# resume 回落 stage 时不能直接采用，避免阶段取值非法。
_RESUME_STAGE_SENTINELS = ("", "bot_takeover", "issue_closed", "pending_review")


def _resume_stage(owner_agent: str) -> str:
    """pending_review 恢复到哪个阶段：真实节点用 owner_agent，哨兵回落到默认首节点 issue。"""
    _owner = (owner_agent or "").strip()
    return "issue" if _owner in _RESUME_STAGE_SENTINELS else _owner


def _resume_issue_waiting(repo_key: str, issue_number: int) -> bool:
    """「等用户」双态恢复（幂等）：`awaiting_input / pending_review → processing`。

    供两条路径复用（issue-explicit-awaiting-input-api-design.md §3.6 / issue-pending-review-explicit-design.md §3.5）：
    - webhook `issue_comment` 自动恢复（非 bot 用户评论）；
    - `issues clarify` / Agent 显式续接。
    仅当 issue 正处于 `awaiting_input` 或 `pending_review` 时翻回，返回是否真正翻转（幂等、可观测）。

    Issue #1599（审核态漂移收敛）：翻转 status 时同步把 `stage=pending_review` 回落到
    `owner_agent`，避免库内 `status=processing` 但 `stage` 仍停在 pending_review，
    导致展示态继续显示「待审核」而 review-reject 必然 noop。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT stage, owner_agent FROM issue WHERE repo_key=? AND issue_number=?",
                (repo_key, issue_number))
            _row = c.fetchone()
            _stage = ((_row[0] if _row else "") or "").strip()
            _owner = ((_row[1] if _row else "") or "").strip()
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status IN ('awaiting_input','pending_review')",
                (repo_key, issue_number))
            _changed = c.rowcount > 0
            if _changed and _stage == "pending_review":
                c.execute(
                    "UPDATE issue SET stage=? WHERE repo_key=? AND issue_number=?",
                    (_resume_stage(_owner), repo_key, issue_number))
            db.commit()
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


# Issue #1599：审核动作未命中原因分类所依赖的终态集合
_ISSUE_TERMINAL_STATUSES = ("completed", "cancelled")


def _read_issue_status(repo_key: str, issue_number: int) -> str:
    """回读 issue 表真实 status（空串 = 无行或回读失败，由调用方兜底）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                (repo_key, issue_number))
            _r = c.fetchone()
        return ((_r[0] if _r else "") or "").strip()
    except Exception as _e:
        log.warning(f"read issue status failed {repo_key}/#{issue_number}: {_e}")
        return ""


def _review_noop_reason(repo_key: str, issue_number: int, target_status: str) -> str:
    """审核动作未命中时回读真实 status 并给出机器可读原因码。

    - `noop:already_target`：已处于目标态（幂等，无需变更）；
    - `noop:terminal`：已完成/已取消，不可再变更；
    - `noop:not_pending_review`：前置态不符（如 awaiting_input/active）；
    - `noop:error`：回读失败（无行/DB 异常）。
    """
    _cur = _read_issue_status(repo_key, issue_number)
    if not _cur:
        return "noop:error"
    if _cur == target_status:
        return "noop:already_target"
    if _cur in _ISSUE_TERMINAL_STATUSES:
        return "noop:terminal"
    return "noop:not_pending_review"


# Issue #656: 自动结单完成原因（区别于 review_passed/user_confirmed 人工确认路径）
_AUTO_FINALIZED_REASON = "auto_finalized"


def _pipeline_last_node_check(repo_key: str, issue_number: int, agent_type: str) -> bool:
    """完成的 agent 是否为该 issue 流水线末节点（next=='done' 或 next 无下游节点）。"""
    try:
        from src.api.session_store import get_issue_stage, get_issue_scene
        from src.api.pipeline_config import get_pipeline_config
        _issue_id = f"#{issue_number}"
        _st = get_issue_stage(repo_key, _issue_id) or {}
        _cur = (_st.get("stage") or "").strip()
        if _cur != agent_type:
            return False
        _scene = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _cfg = get_pipeline_config(_scene_id, repo_key=repo_key) or {}
        _nodes = _cfg.get("nodes") or []
        _ids = {(_n.get("id") or _n.get("tag") or "").strip() for _n in _nodes}
        for _n in _nodes:
            _nid = (_n.get("id") or _n.get("tag") or "").strip()
            if _nid == agent_type:
                _next = (_n.get("next") or "").strip()
                if _next == "done" or _next == "":
                    return True
                if _next not in _ids:
                    # 配置笔误（next 指向不存在节点）不视为末节点，防误触发自动结单
                    log.warning(f"pipeline last-node check: next={_next} not in nodes "
                                f"(misconfig?) {repo_key}/#{issue_number} agent={agent_type}")
                return False
        return False
    except Exception as _e:
        log.warning(f"pipeline last-node check failed {repo_key}/#{issue_number}: {_e}")
        return False


def _find_github_mirror_repo_key(repo_key: str) -> str:
    """查找 GitCode 仓库绑定的 GitHub 镜像 repo_key（confirm-done 同款逻辑，#656 复用）。

    managed_repos 表中同一 owner/repo 可能同时存在 gitcode: 和 github: 两条记录，
    通过 full_name 匹配。找不到返回空串。
    """
    try:
        from src.repo_key import split_owner_repo
        from src.session_manager import db, db_lock
        owner, repo = split_owner_repo(repo_key)
        if not owner or not repo:
            return ""
        gh_key = f"github:{owner}/{repo}"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key FROM managed_repos WHERE repo_key=? LIMIT 1", (gh_key,))
            row = c.fetchone()
        return row[0] if row else ""
    except Exception as e:
        log.warning(f"find github mirror for {repo_key} failed: {e}")
        return ""


def _close_vcs_one_auto(repo_key: str, issue_number: int) -> str:
    """对单个 repo_key 幂等关闭 VCS Issue（bot token，降级不阻断）。"""
    try:
        from src.repo_key import parse_repo_key
        provider, owner, repo = parse_repo_key(repo_key)
        if not owner or not repo:
            return "skipped"
        token = ""
        if provider == "github":
            from src.user.db import get_github_token as _get_gh
            token = _get_gh(repo_key=repo_key) or ""
        else:
            from src.user.db import get_repo_bot_token as _get_gc
            token = _get_gc(repo_key=repo_key) or ""
        if not token:
            log.info(f"auto-finalize: no bot token for {repo_key}, skip VCS close")
            return "skipped"
        from src.vcs.api_client import VcsApiClient
        client = VcsApiClient(provider, token, owner, repo)
        try:
            if client.get_issue_state(issue_number) == "closed":
                return "skipped"
        except Exception as _se:
            log.warning(f"auto-finalize: get_issue_state {repo_key}#{issue_number} failed: {_se}")
        resp = client.close_issue(issue_number)
        if isinstance(resp, dict) and "error" not in resp:
            log.info(f"auto-finalize: closed VCS issue {repo_key}#{issue_number}")
            return "success"
        log.warning(f"auto-finalize: close_issue {repo_key}#{issue_number} -> {resp}")
        return "failed"
    except Exception as _e:
        log.warning(f"auto-finalize: close VCS issue {repo_key}#{issue_number} failed: {_e}")
        return "failed"


def _close_vcs_issue_auto(repo_key: str, issue_number: int) -> str:
    """Issue #656: 服务端自动结单后联动关闭 VCS Issue（bot token，降级不阻断）。

    与 confirm-done 人工路径语义一致：GitCode 仓库同时尝试关闭绑定的 GitHub 镜像 Issue
    （镜像 repo_key 解析失败按降级语义 skip）；github: 仓库仅关闭 GitHub 侧。

    幂等：VCS 侧已 closed → skipped。返回 "success" / "skipped" / "failed"（聚合：
    任一侧 success 即 success，否则任一侧 failed 即 failed，全 skipped 即 skipped；
    失败仅记日志，不影响本地已完成状态，与 confirm-done 的降级语义一致）。
    """
    if not issue_number:
        return "skipped"
    _results = {}
    try:
        _results[repo_key] = _close_vcs_one_auto(repo_key, issue_number)
        if not repo_key.startswith("github:"):
            _gh_rk = _find_github_mirror_repo_key(repo_key)
            if _gh_rk:
                _results[_gh_rk] = _close_vcs_one_auto(_gh_rk, issue_number)
    except Exception as _e:
        log.warning(f"auto-finalize: close VCS issue {repo_key}#{issue_number} failed: {_e}")
        return "failed"
    _vals = list(_results.values())
    if "success" in _vals:
        return "success"
    if "failed" in _vals:
        return "failed"
    return "skipped"


def _issue_latest_event_status_by_agent(repo_key: str, issue_id_tag: str) -> dict:
    """该 issue 每个 agent_type 的最新事件状态（seq 最大者），用于「全部节点 completed」检测。

    兼容 events.issue_id 带/不带 `#` 两种历史形态（同一 issue 归并后取全局最新）。
    """
    out = {}
    _num_s = (issue_id_tag or "").strip().lstrip("#")
    if not _num_s:
        return out
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT agent_type, status, seq FROM events e WHERE repo_key=? "
                "AND issue_id IN (?, ?) "
                "AND seq = (SELECT MAX(seq) FROM events e2 "
                "WHERE e2.repo_key=e.repo_key AND e2.issue_id IN (?, ?) "
                "AND e2.agent_type=e.agent_type)",
                (repo_key, f"#{_num_s}", _num_s, f"#{_num_s}", _num_s))
            for _ag, _st, _seq in c.fetchall():
                if _ag:
                    _st = _st or ""
                    _prev = out.get(_ag)
                    # 极端双形态并存时按 seq 最大者取
                    if _prev is None or _seq >= _prev[1]:
                        out[_ag] = (_st, _seq)
        return {k: v[0] for k, v in out.items()}
    except Exception as _e:
        log.warning(f"auto-finalize: latest event status query failed {repo_key}/{issue_id_tag}: {_e}")
        return out


def auto_finalize_issue_on_last_node(repo_key: str, issue_id: str, agent_type: str,
                                     source: str = "pipeline") -> str:
    """Issue #656: 末节点完成后 issue 级自动收尾迁移（自动结单）。

    触发条件（全部满足才自动结单）：
    - 完成的 agent == issue 当前 stage，且该节点为流水线末节点（next=='done' 或无下游）；
    - 该 issue 全部流水线节点最新事件均 completed（无 running/dispatched），且无 failed 阻断；
    - issue.status 非 pending_review（显式待人工确认场景不自动结单，保留 confirm done 人工路径）。

    迁移：status→completed、completed_reason='auto_finalized'、completed_at 落库（#368）、
    stage→done，并联动关闭 VCS（GitCode/GitHub）Issue（bot token，降级不阻断，幂等）。

    有 failed 阻断时回退 stage=pending_review（保留 Issue #51 人工确认语义）。

    返回："auto_done" / "pending_review_fallback" / "noop:<reason>"（幂等可重入）。
    """
    try:
        _num_s = (issue_id or "").strip().lstrip("#")
        if not _num_s.isdigit() or _num_s == "0":
            return "noop:not_issue"          # PR 订阅伪 issue_id（#pr_...）等非真实 Issue
        _num = int(_num_s)
        _tag = f"#{_num}"

        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                      (repo_key, _num))
            _row = c.fetchone()
        _status = (_row[0] if _row else "") or ""
        if not _row:
            return "noop:no_issue_row"
        if _status in ("completed", "cancelled"):
            # 补偿对齐：status=completed 但 stage 未到 done（历史 set_issue_stage 失败残留）
            # → 读 status 反向对齐 stage（幂等，失败仅记日志，不改变 noop 语义）
            if _status == "completed":
                try:
                    from src.api.session_store import get_issue_stage_name, set_issue_stage
                    if (get_issue_stage_name(repo_key, _tag) or "") != "done":
                        log.info(f"Issue #656: compensate stage=done for completed "
                                 f"{repo_key}/{_tag}")
                        set_issue_stage(repo_key, _tag, "done", "agent")
                except Exception as _se:
                    log.warning(f"auto-finalize compensate stage=done failed "
                                f"{repo_key}/{_tag}: {_se}")
            return "noop:terminal"
        if _status == "pending_review":
            return "noop:pending_review"      # 显式待人工确认（confirm done 路径），不自动结单

        if not _pipeline_last_node_check(repo_key, _num, agent_type):
            return "noop:not_last_node"

        # 「全部节点 completed + 无阻断」检测：每个流水线节点的最新事件
        from src.api.session_store import get_issue_scene
        from src.api.pipeline_config import get_pipeline_config
        _scene = get_issue_scene(repo_key, _tag)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _nodes = (get_pipeline_config(_scene_id, repo_key=repo_key) or {}).get("nodes") or []
        _node_ids = [(_n.get("id") or _n.get("tag") or "").strip() for _n in _nodes]
        _node_ids = [n for n in _node_ids if n]
        _latest = _issue_latest_event_status_by_agent(repo_key, _tag)
        for _nid in _node_ids:
            _st = _latest.get(_nid)
            if _st in ("running", "dispatched", "pending"):
                return f"noop:node_running:{_nid}"
            if _st == "failed":
                # 阻断 → 回退 pending_review，等待人工确认（Issue #51 语义保留给异常场景）
                from src.api.session_store import set_issue_stage
                set_issue_stage(repo_key, _tag, "pending_review", agent_type)
                log.info(f"Issue #656: node {_nid} failed → pending_review "
                         f"(manual confirm path) {repo_key}/{_tag}")
                return "pending_review_fallback"
            if _st != "completed":
                return f"noop:node_pending:{_nid}"

        # 条件齐备 → issue 级收尾迁移（幂等：仅可变状态放行）
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE issue SET status='completed', completed_reason=?, completed_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND status IN ('active','processing','awaiting_input')",
                (_AUTO_FINALIZED_REASON, repo_key, _num))
            db.commit()
            _changed = c.rowcount > 0
        if not _changed:
            return "noop:not_mutable"
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, _tag, "done", source or "pipeline")
        except Exception as _se:
            # UPDATE completed 与 set_issue_stage 非原子 → 立即补偿重试一次，仍失败仅记日志
            log.warning(f"auto-finalize stage=done failed {repo_key}/{_tag}: {_se}, retry once")
            try:
                set_issue_stage(repo_key, _tag, "done", source or "pipeline")
            except Exception as _se2:
                log.warning(f"auto-finalize stage=done retry failed {repo_key}/{_tag}: {_se2} "
                            f"(will be compensated on next event)")
        _closed = _close_vcs_issue_auto(repo_key, _num)
        log.info(f"Issue #656: auto-finalized {repo_key}/{_tag} by {source} "
                 f"(agent={agent_type}, vcs_close={_closed})")
        return "auto_done"
    except Exception as _e:
        log.warning(f"auto-finalize failed {repo_key}/{issue_id}: {_e}")
        return "noop:error"


def _review_pass(repo_key: str, issue_number: int, target: str = "processing",
                 evidence: str = "") -> str:
    """Agent 判定用户通过审核（幂等）。

    - target=done：pending_review → completed+done（合入/收尾）；
      **末节点放宽（#2266）**：若当前 stage 为流水线末节点（next=='done'），status 处于
      active/processing/awaiting_input 也放行 done（CLI 流水线可从未进入 pending_review，
      如 PR 已合入但需求卡 active → 原路径 noop）。防中间节点误终态：非末节点仍仅 pending_review 可退出。
    - target=processing：仅 pending_review → processing（继续流水线）；末节点触达则归一 done
      （review-pass-last-node-force-done-design.md，无下游可续）。
    - 未命中 → `noop:already_target|terminal|not_pending_review|error`（Issue #1599：
      回读真实 status 后分类，调用方据此给出 changed=false + 真实 status + 原因码）。
    """
    try:
        if target == "done":
            if _is_last_stage_node(repo_key, issue_number):
                # 末节点：兼容 pending_review + 放宽 active/processing/awaiting_input（方向 A）
                if _complete_issue_done(repo_key, issue_number, evidence,
                                        ("pending_review", "active", "processing", "awaiting_input")):
                    return "passed_done"
                return _review_noop_reason(repo_key, issue_number, "completed")
            # 非末节点：保持原契约（仅 pending_review），防中间节点误终态
            if _complete_issue_done(repo_key, issue_number, evidence, ("pending_review",)):
                return "passed_done"
            return _review_noop_reason(repo_key, issue_number, "completed")
        if _is_last_stage_node(repo_key, issue_number):
            # 末节点 + processing → 归一 done（无下游可续）
            log.info(f"Issue review-pass normalized last-node target=processing→done: "
                     f"{repo_key}/#{issue_number} evidence={evidence[:60]}")
            if _complete_issue_done(repo_key, issue_number, evidence,
                                    ("pending_review", "active", "processing", "awaiting_input")):
                return "passed_done"
            return _review_noop_reason(repo_key, issue_number, "completed")
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
        return _review_noop_reason(repo_key, issue_number, "processing")
    except Exception as _e:
        log.warning(f"review_pass failed {repo_key}/#{issue_number}: {_e}")
        return "noop:error"


def _review_reject(repo_key: str, issue_number: int, target: str = "processing",
                   reason: str = "") -> str:
    """Agent 判定用户未通过审核（幂等）：`pending_review → processing|awaiting_input` 打回/转澄清。

    返回 `rejected_processing|rejected_awaiting_input`（命中）；未命中返回
    `noop:already_target|terminal|not_pending_review|error`（Issue #1599：回读真实 status 分类，
    不再以 `noop` 冒充 `status=pending_review`）。
    """
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
        return _review_noop_reason(repo_key, issue_number, _t)
    except Exception as _e:
        log.warning(f"review_reject failed {repo_key}/#{issue_number}: {_e}")
        return "noop:error"


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


def _vcs_issue_url(repo_key: str, issue_number: int) -> str:
    """repo_key → VCS Issue 公开页 URL（gitcode/github/gitlab 分流）。

    无编号 / 坏仓库（缺 owner 或 repo）→ ''。未知 provider 前缀 → 默认 GitCode。
    """
    from src.repo_key import split_owner_repo
    if not issue_number:
        return ""
    owner, repo = split_owner_repo(repo_key)
    if not owner or not repo:
        return ""
    _p = str(repo_key).split(":", 1)[0]
    if _p == "github":
        return f"https://github.com/{owner}/{repo}/issues/{issue_number}"
    if _p == "gitlab":
        return f"https://gitlab.com/{owner}/{repo}/-/issues/{issue_number}"
    return f"https://gitcode.com/{owner}/{repo}/issues/{issue_number}"


def _build_notify_actions(repo_key: str, issue_number: int, conv_id: str) -> list:
    """通知卡快捷按钮：进入任务会话（cmd 直达）+ 查看 Issue（VCS 公开页只读）。

    设计：docs/weekly-05/feishu-notification-card-quick-actions-design.md。
    """
    _iss = f"#{issue_number}" if issue_number else ""
    actions = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "💬 进入任务会话"},
        "value": {"cmd": "/task-open", "repo_key": repo_key,
                  "issue_number": _iss, "conv_id": conv_id or ""},
    }]
    _url = _vcs_issue_url(repo_key, issue_number)
    if _url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看 Issue ↗"},
            "url": _url,
        })
    return actions


_IM_NOTIFY_TEMPLATES = {
    "task": "blue",
    "awaiting": "orange",
    "pending_review": "red",
}
_IM_NOTIFY_TITLES = {
    "task": "📋 信息知会",
    "awaiting": "❓ 需求待澄清",
    "pending_review": "🔎 需求待审核",
}


def _build_notify_fields(repo_key: str, issue_number: int) -> list:
    """通知卡结构化键值对字段：仓库 / Issue（带 VCS 链接）/ 需求标题（替代纯文本行，移动端对齐更好）。"""
    from src.repo_key import from_repo_key
    _iss = f"#{issue_number}" if issue_number else ""
    _iss_link = ""
    if _iss:
        _url = _vcs_issue_url(repo_key, issue_number)
        if _url:
            _iss_link = f"[{_iss}]({_url})"
    _title = _get_issue_title(repo_key, issue_number) or "—"
    return [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**📦 仓库**\n{from_repo_key(repo_key) or '—'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**🎯 Issue**\n{_iss_link or '—'}"}},
        {"is_short": False, "text": {"tag": "lark_md", "content": f"**📌 需求标题**\n{_title}"}},
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
    """删除指定 issue 的所有附件（含用户手工上传）。**破坏性**：整目录清理，
    仅供明确丢弃场景使用；issue 关闭走 `_cleanup_auto_transferred_attachments`。"""
    try:
        from src.repo_key import split_owner_repo
        _owner, _rname = split_owner_repo(repo_key)
        _attach_cleanup_dir(_owner, _rname, str(issue_number))
    except Exception as _e:
        log.warning(f"Attachment cleanup failed: {_e}")


# Issue #1748 补充：平台自动转存媒体的文件名形态——#315 = user-image-<n>.<ext>；
# #1748 = <uuid32>.<媒体扩展名>（UUID 命名由 spec 约定）。只回收这两类，
# 用户手工上传的附件一律保留。
_AUTO_TRANSFER_PREFIXES = ("user-image-", "user-attachment-")
_AUTO_TRANSFER_MEDIA_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi",
                             ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _is_auto_transferred_attachment(name: str) -> bool:
    """判断附件名是否由平台自动转存（评论媒体/编辑器图片）而来。"""
    if not name:
        return False
    if name.startswith(_AUTO_TRANSFER_PREFIXES):
        return True
    stem, ext = os.path.splitext(name)
    if ext.lower() not in _AUTO_TRANSFER_MEDIA_EXTS or len(stem) != 32:
        return False
    return all(ch in "0123456789abcdef" for ch in stem.lower())


def _cleanup_auto_transferred_attachments(repo_key: str, issue_number) -> int:
    """Issue #1748：回收平台自动转存的评论媒体/编辑器图片，保留用户手工附件。

    issue 确认完成/取消时调用。单条评论的自动转存副本对已关闭 issue 无复用价值，
    但用户手工上传的附件（可能是交付依据/证据）不能删，故不做整目录清理。
    异常一律吞掉，清理失败不影响关闭流程；返回删除数量。
    """
    removed = 0
    try:
        from src.repo_key import split_owner_repo
        _owner, _rname = split_owner_repo(repo_key)
        if not _owner or not _rname:
            return 0
        _num = str(issue_number)
        from src.storage import get_storage
        storage = get_storage()
        if storage:
            # Issue #1748 复审：OBS 模式下 list_keys + 每对象 delete_object 都是网络往返
            # （N 个文件 ≈ N+1 RTT），不能挂在「关闭/取消 issue」的同步链路上 →
            # 丢给后台线程执行，接口立即返回（清理失败只丢日志，不影响业务）。
            def _reap_obs() -> None:
                _n = 0
                try:
                    for _key in storage.list_keys(_attach_obs_key(_owner, _rname, _num, "")):
                        if _is_auto_transferred_attachment(os.path.basename(str(_key))):
                            if storage.delete_object(_key):
                                _n += 1
                except Exception as _e:
                    log.warning(f"Attachment cleanup (obs, async) failed: {_e}")
                if _n:
                    log.info(f"Attachment cleanup (auto-transferred): {_n} object(s) "
                             f"for {_owner}/{_rname}/{_num}")
            import threading
            threading.Thread(target=_reap_obs, name="attach-reap", daemon=True).start()
            return 0
        _dir = _attach_base_dir(_owner, _rname, _num)
        if os.path.isdir(_dir):
            for _name in os.listdir(_dir):
                if not _is_auto_transferred_attachment(_name):
                    continue
                try:
                    os.remove(os.path.join(_dir, _name))
                    removed += 1
                except OSError as _e:
                    log.warning(f"Attachment cleanup: remove {_name} failed: {_e}")
            if removed:
                log.info(f"Attachment cleanup (auto-transferred): {removed} file(s) in {_dir}")
    except Exception as _e:
        log.warning(f"Auto-transferred attachment cleanup failed: {_e}")
    return removed


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
    # Issue #1748 补充：关闭后回收平台自动转存的评论媒体/编辑器图片（保留用户手工附件）
    _cleanup_auto_transferred_attachments(repo_key, issue_number)
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
    # Issue #1748 补充：取消后同样回收自动转存媒体（保留用户手工附件）
    _cleanup_auto_transferred_attachments(repo_key, issue_number)
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


# ===== Issue #627: 已完成需求的生产续接（发布）子单（方案 C：复用现有子任务机制）=====
# 发布子单 = issue 行自身（parent_issue_id + is_subtask_mode=1 + release_* 绑定/结果字段），
# 创建后 stage=sre 直通标准 a2a 派发；子单终态 release_result 即发布结果三分支。
# 零平行表、不穿透 _TASK_STATUS_FLOWS 终态语义。

class ProductionReleaseError(Exception):
    """发布校验链拒绝（detail 携带 code/message）。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


_PRODUCTION_RELEASE_APPROVAL_TTL = int(
    os.environ.get("PRODUCTION_RELEASE_APPROVAL_TTL") or 24 * 3600)

_SHA40_RE = None  # lazy import re


def _is_full_sha(candidate_sha: str) -> bool:
    """候选提交须为完整 40 位十六进制 SHA（短 SHA 不足以钉住候选版本）。"""
    global _SHA40_RE
    if _SHA40_RE is None:
        import re
        _SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
    return bool(_SHA40_RE.match(candidate_sha or ""))


def _latest_sre_acceptance(repo_key: str, issue_number: int) -> dict | None:
    """取该需求最近一条已完成 sre 事件（验收证据锚点）。

    返回 {seq, payload}；无已完成 sre 事件返回 None（未验收）。
    """
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, payload FROM events WHERE repo_key=? AND issue_id=? "
                "AND agent_type='sre' AND status='completed' ORDER BY seq DESC LIMIT 1",
                (repo_key, f"#{int(issue_number)}"))
            row = c.fetchone()
        if not row:
            return None
        try:
            payload = _json.loads(row[1] or "{}")
        except Exception:
            payload = {}
        return {"seq": row[0], "payload": payload if isinstance(payload, dict) else {}}
    except Exception as _e:
        log.warning(f"_latest_sre_acceptance failed: {_e}")
        return None


def is_release_subissue(repo_key: str, issue_number) -> bool:
    """判断 issue 是否为发布子单（release_target_env 非空即发布子单，普通 issue 恒 False）。

    PR #818 审查修复（Optional-3 回声竞态兜底）：
    - 兼容 "#N" 形态的 issue_id（webhook 回声 resolve_issue_id 返回带 # 前缀）；
    - 绑定字段落库前的竞态窗口内，标题前缀 `发布子单：` 作为兜底识别
      （该前缀为本入口专属命名，普通 issue 不会命中）。
    """
    try:
        _num = str(issue_number or "").lstrip("#").strip()
        _num = int(_num)
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT 1 FROM issue WHERE repo_key=? AND issue_number=? "
                "AND (release_target_env != '' OR title LIKE '发布子单：%') LIMIT 1",
                (repo_key, _num))
            return c.fetchone() is not None
    except Exception:
        return False


def _release_subissue_title(issue_number: int, target_env: str, candidate_sha: str) -> str:
    """发布子单标题：`发布子单：需求 #{N} → {env} @ {sha12}`（平台 Issue 标题，可辨识）。"""
    _sha_short = (candidate_sha or "")[:12]
    _env = (target_env or "").strip()
    _title = f"发布子单：需求 #{issue_number} → {_env} @ {_sha_short}"
    return _title[:200]


def _release_subissue_body(repo_key: str, issue_number: int, candidate_sha: str,
                           target_env: str, evidence_ref: str,
                           approved_by: int, approved_at: str) -> str:
    """发布子单 body：`## 父任务`（现有子任务识别机制）+ `## 发布绑定`（绑定要素落平台）。"""
    from src.repo_key import from_repo_key
    _repository = from_repo_key(repo_key) or repo_key.split(":", 1)[-1]
    _lines = [
        f"## 父任务",
        f"- 关联父任务：{_repository}#{int(issue_number)}",
        "",
        "## 发布绑定",
        f"- 源需求：{int(issue_number)}",
        f"- 候选提交：{candidate_sha}",
        f"- 目标环境：{target_env}",
        f"- 验收证据：{evidence_ref or '（见源需求 SRE 验收事件）'}",
        f"- 批准来源：user_id={int(approved_by or 0)}（{approved_at or ''}）",
    ]
    return "\n".join(_lines)


def _sync_release_fields(repo_key: str, issue_number: int, parent_issue_number: int,
                         candidate_sha: str, target_env: str, evidence_ref: str,
                         approved_by: int, approved_at: str) -> None:
    """将父任务关联（parent_issue_id/parent_issue_number/is_subtask_mode）+ 发布绑定要素
    写回发布子单 issue 行（创建时仅此一次；拒绝路径零写入）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET parent_issue_id=?, parent_issue_number=?, is_subtask_mode=1, "
            "release_candidate_sha=?, release_target_env=?, release_evidence_ref=?, "
            "release_approved_by=?, release_approved_at=?, updated_at=datetime('now') "
            "WHERE repo_key=? AND issue_number=?",
            (f"#{int(parent_issue_number)}", int(parent_issue_number),
             candidate_sha, target_env, evidence_ref or "", int(approved_by or 0),
             approved_at or "", repo_key, int(issue_number)))
        db.commit()


def create_release_subissue(repo_key: str, issue_number: int, candidate_sha: str,
                            target_env: str, evidence_ref: str = "",
                            approved_by: int = 0, approved_at: str = "",
                            api_token: str = "", dispatch: bool = True) -> dict:
    """为 done/completed 终态需求创建「发布子单」（方案 C：复用现有子任务机制）。

    校验链（按序，任一失败抛 ProductionReleaseError，**拒绝路径零写入**——
    不建平台 Issue、不动 issue 行、不增删 events，原验收证据天然保留）：
    1. 终态前置：源需求 issue.status 必须为 completed（非终态走正常流水线，拒绝）；
    2. SRE 验收前置：无已完成 sre 事件 → 拒绝（未验收不能发布）；
    3. 候选一致性：candidate_sha 须为完整 40 位 SHA 且与 sre 验收 payload 中的
       candidate_sha（如有记录）一致；
    4. 目标环境：非空且 ≤64 字符；
    5. 批准时效：approved_at 距今 ≤ TTL（默认 24h）；
    6. 唯一性：同（源需求+候选SHA+目标环境）已存在发布子单（任意终态）→ 409 duplicate。

    成功路径：平台创建真实子任务 Issue（标题/body 含 `## 父任务` 链接，复用现有子任务
    识别机制）→ issue 行建档（is_subtask_mode=1 + parent_issue_id=源需求）+ release_*
    绑定字段 → stage=sre（直通 sre，不经 vod/issue/coding/qa）→ dispatch=True 时服务内
    直调 a2a_endpoint 派发 sre 执行部署。子单终态 release_result 即发布结果三分支。
    """
    from src.repo_key import to_repo_key, split_owner_repo, parse_repo_key
    repo_key = to_repo_key(repo_key)
    # PR #799 R1 语义保留：落库前统一小写——唯一绑定与验收记录对照口径一致，
    # 大小写变体不得绕过唯一性重复创建发布子单
    candidate_sha = (candidate_sha or "").strip().lower()
    target_env = (target_env or "").strip()
    evidence_ref = (evidence_ref or "").strip()

    # 1) 终态前置
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM issue WHERE repo_key=? AND issue_number=?",
                  (repo_key, int(issue_number)))
        _row = c.fetchone()
    if not _row:
        raise ProductionReleaseError("issue_not_found", "需求不存在", status=404)
    if (_row[0] or "") != "completed":
        raise ProductionReleaseError(
            "not_terminal",
            f"需求当前状态为 {_row[0] or '(空)'}，非 completed 终态：生产续接仅适用于已完成需求"
            f"（进行中需求请走正常流水线，不使用发布入口）")

    # 2) SRE 验收前置
    _sre_ev = _latest_sre_acceptance(repo_key, issue_number)
    if not _sre_ev:
        raise ProductionReleaseError(
            "no_sre_acceptance",
            "该需求无已完成的 SRE 验收事件：缺少验收证据，拒绝创建发布子单")

    # 3) 候选一致性（格式 + 与验收记录对照）
    if not _is_full_sha(candidate_sha):
        raise ProductionReleaseError(
            "invalid_candidate_sha",
            "candidate_sha 必须为完整 40 位十六进制提交 SHA（短 SHA 不足以钉住候选版本）")
    _accepted_sha = str(_sre_ev["payload"].get("candidate_sha") or "").strip()
    if _accepted_sha and _accepted_sha.lower() != candidate_sha.lower():
        raise ProductionReleaseError(
            "candidate_mismatch",
            f"候选提交与 SRE 验收记录不一致：验收记录 candidate_sha={_accepted_sha}，"
            f"请求 candidate_sha={candidate_sha}（候选变更拒绝发布）")

    # 4) 目标环境
    if not target_env or len(target_env) > 64:
        raise ProductionReleaseError(
            "invalid_target", "target_env 必填且不超过 64 字符（指定生产环境标识）")

    # 5) 批准时效
    from datetime import datetime, timezone as _tz
    _approved_at = (approved_at or "").strip()
    _parsed = None
    if _approved_at:
        try:
            _parsed = datetime.fromisoformat(_approved_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            _parsed = None
    if not _parsed:
        _parsed = datetime.now(_tz.utc)
        _approved_at = _parsed.isoformat()
    if _parsed.tzinfo is None:
        _parsed = _parsed.replace(tzinfo=_tz.utc)
    _age = (datetime.now(_tz.utc) - _parsed).total_seconds()
    if _age > _PRODUCTION_RELEASE_APPROVAL_TTL or _age < -_PRODUCTION_RELEASE_APPROVAL_TTL:
        raise ProductionReleaseError(
            "approval_expired",
            f"批准已过期：approved_at={_approved_at} 距今 {_age / 3600:.1f}h，"
            f"超过有效期 {_PRODUCTION_RELEASE_APPROVAL_TTL / 3600:.0f}h")

    # 6) 唯一性（显式查询前置；拒绝路径发生在平台建单之前——零外部写入）
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, issue_number, release_result FROM issue "
            "WHERE repo_key=? AND parent_issue_number=? AND release_target_env != '' "
            "AND release_candidate_sha=? AND release_target_env=? ORDER BY id DESC LIMIT 1",
            (repo_key, int(issue_number), candidate_sha, target_env))
        _dup = c.fetchone()
    if _dup:
        raise ProductionReleaseError(
            "duplicate",
            f"该绑定已存在发布子单（sub_issue_number={_dup[1]}，result={_dup[2] or '-'}）："
            f"同（需求+候选SHA+目标环境）禁止重复提交", status=409)

    # 平台建单：真实子任务 Issue（复用现有子任务机制——body 含 `## 父任务` 链接）
    _owner, _rname = split_owner_repo(repo_key)
    _provider, _, _ = parse_repo_key(repo_key)
    _title = _release_subissue_title(issue_number, target_env, candidate_sha)
    _body_txt = _release_subissue_body(repo_key, issue_number, candidate_sha,
                                       target_env, evidence_ref, approved_by, _approved_at)
    _sub_num = 0
    try:
        if _provider == "github":
            _resp = _post_github_issue(api_token or "", _owner, _rname, _title,
                                       _body_txt, assignee="")
        else:
            _resp = _post_gitcode_issue(api_token or "", _owner, _rname, _title,
                                        _body_txt, assignee="")
        if isinstance(_resp, dict):
            _sub_num = int(_resp.get("number") or _resp.get("iid") or 0)
    except Exception as _e:
        raise ProductionReleaseError(
            "release_create_failed",
            f"创建发布子单 Issue 失败：{str(_e)[:200]}", status=502)
    if not _sub_num:
        raise ProductionReleaseError(
            "release_create_failed",
            "创建发布子单 Issue 失败（平台响应无 issue number）", status=502)
    # Issue #453 代发台账：建单入账（webhook 回声对账）
    try:
        from src.outbound_ledger import record_outbound, content_hash_md5
        record_outbound(_provider, repo_key, "issue", str(_sub_num),
                        issue_number=str(_sub_num),
                        content_hash=content_hash_md5(_body_txt))
    except Exception as _le:
        log.warning(f"outbound_ledger record (release subissue) failed: {_le}")

    # issue 行建档（复用现有子任务机制：is_subtask_mode=1 + parent_issue_id=源需求）
    try:
        # 建档时即声明子任务模式（ensure_issue_task 写入 is_subtask_mode）
        _task_id = ensure_issue_task(
            repo_key, _sub_num, title=_title, description=_body_txt,
            assignee="", event_type="production_release",
            subtask_mode=1, owner_user_id=int(approved_by or 0))
    except Exception as _e:
        log.warning(f"release subissue ensure_issue_task failed: {_e}")
        raise ProductionReleaseError(
            "release_archive_failed", f"发布子单建档失败：{str(_e)[:200]}", status=500)
    if not _task_id:
        raise ProductionReleaseError("release_archive_failed", "发布子单建档失败", status=500)
    # 父任务关联 + 绑定要素落库（parent_issue_id/number + is_subtask_mode + release_* 一次写入）
    # PR #818 审查修复（R2 部分失败显式上报）：绑定/置阶段失败不再静默——
    # 子单缺 release 绑定会导致 webhook 回声进 vod 二次派发、回填钩子不识别、
    # 唯一性查询不计数；失败即抛 release_archive_failed（与建档失败路径一致）。
    # 唯一索引冲突（并发双提交绕过前置查询的兜底命中）映射 409 duplicate。
    try:
        _sync_release_fields(repo_key, _sub_num, int(issue_number), candidate_sha, target_env,
                             evidence_ref, approved_by, _approved_at)
    except Exception as _e:
        _err_txt = str(_e)
        if "unique" in _err_txt.lower() or "1062" in _err_txt:
            raise ProductionReleaseError(
                "duplicate",
                f"该绑定已存在发布子单（并发提交命中唯一约束，sub_issue_number={_sub_num}）",
                status=409)
        raise ProductionReleaseError(
            "release_archive_failed",
            f"发布子单绑定要素落库失败（sub_issue_number={_sub_num}，平台 Issue 已创建，"
            f"请人工核对）：{str(_e)[:200]}", status=500)
    # 直通 sre：设 stage=sre（跨过 vod/issue/coding/qa）
    try:
        from src.api.session_store import set_issue_stage
        set_issue_stage(repo_key, f"#{_sub_num}", "sre", "sre")
    except Exception as _e:
        raise ProductionReleaseError(
            "release_archive_failed",
            f"发布子单置 stage=sre 失败（sub_issue_number={_sub_num}，绑定要素已落库，"
            f"请人工核对阶段路由）：{str(_e)[:200]}", status=500)
    log.info(f"release subissue created: sub=#{_sub_num} parent=#{int(issue_number)} "
             f"repo={repo_key} sha={candidate_sha[:12]} env={target_env} approved_by={approved_by}")

    out = {"ok": True, "sub_issue_number": _sub_num, "issue_id": _task_id,
           "repo_key": repo_key, "parent_issue_number": int(issue_number),
           "candidate_sha": candidate_sha, "target_env": target_env,
           "evidence_ref": evidence_ref or "", "status": "sre", "seq": 0}

    if not dispatch:
        return out

    # 标准 A2A 派发 sre（服务内直调 a2a_endpoint，与 handoff_issue 同模式：
    # jwt 分支内部再校验 managed_repos，双保险；绑定上下文随 context/events.payload 持久化）
    _seq = _dispatch_release_sre(
        repo_key, int(issue_number), _sub_num, candidate_sha,
        target_env, evidence_ref or "", approved_by=int(approved_by or 0))
    if _seq:
        out["seq"] = _seq
    else:
        out["dispatch_error"] = "sre 派发未成功（事件未创建或被拦截），发布子单保留 stage=sre，可人工处理"
        log.warning(f"release subissue #{_sub_num}: sre dispatch returned no seq")
    return out


def _dispatch_release_sre(repo_key: str, issue_number: int, sub_issue_number: int,
                          candidate_sha: str, target_env: str, evidence_ref: str,
                          approved_by: int = 0) -> int:
    """为发布子单派发携带发布上下文的 sre a2a_event，返回事件 seq（失败返回 0）。

    复用标准 /api/a2a 链路（鉴权、场景节点校验、去重、事件落库、引擎选择全保留）。
    事件挂子单 issue 时间线（issue_id=#{sub_issue_number}），payload 携带
    parent_issue/is_subtask_mode/context（不再需要旁路 production_release 锚点——
    回填按 task.issue_id 反查 issue 行即可）。
    """
    import json as _json
    from src.repo_key import from_repo_key
    from src.api.routes.webhook import a2a_endpoint, A2AHandoffBody
    _repository = from_repo_key(repo_key) or repo_key.split(":", 1)[-1]
    _account = _repository.split("/", 1)[0] if "/" in _repository else ""
    _context = (f"发布子单 #{sub_issue_number}：源需求 #{issue_number} 已完成 SRE 验收并经用户批准"
                f"发布到指定环境。候选提交 {candidate_sha}，目标环境 {target_env}，"
                f"验收证据 {evidence_ref or '（见源需求 SRE 验收事件）'}。"
                f"请按生产发布流程执行部署，并在完成后回填实际部署结果"
                f"（部署成功/失败/未执行三分支）。")
    _a2a_body = A2AHandoffBody(agent="sre", payload={
        "repository": _repository,
        "account": _account,
        "issue_number": sub_issue_number,
        "pr_number": "",
        "context": _context,
        "is_subtask_mode": True,
        "parent_issue": f"#{int(issue_number)}",
    })
    _user = {"id": int(approved_by or 0), "username": "", "auth_type": "jwt"}
    # a2a_endpoint 为 async：创建端点为同步 def（线程池执行，无运行中 loop），直接
    # asyncio.run；若未来出现 async 调用方，用独立线程跑新 loop 兜底，避免嵌套 loop 报错。
    _res = None
    try:
        import asyncio as _aio2
        try:
            _loop = _aio2.get_running_loop()
        except RuntimeError:
            _loop = None
        if _loop and _loop.is_running():
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                _res = _ex.submit(lambda: _aio2.run(a2a_endpoint(None, _a2a_body, _user))).result(
                    timeout=60)
        else:
            _res = _aio2.run(a2a_endpoint(None, _a2a_body, _user))
    except Exception as _e:
        log.warning(f"_dispatch_release_sre failed: {_e}")
        return 0
    if isinstance(_res, dict):
        _seq = int(_res.get("seq") or 0)
        if _seq > 0:
            return _seq
    _body_txt = ""
    try:
        _raw = getattr(_res, "body", b"") or b""
        if isinstance(_raw, memoryview):
            _raw = _raw.tobytes()
        _parsed = _json.loads(bytes(_raw).decode("utf-8"))
        _body_txt = str(_parsed)
    except Exception:
        pass
    log.warning(f"_dispatch_release_sre: a2a returned no seq (res={_body_txt[:200] or _res})")
    return 0


def _has_deploy_url(result_text: str) -> bool:
    """从 sre 结果文本提取部署证据（与 _extract_deploy_url 同口径：artifacts/issue_summary 含 deploy_url）。"""
    import re as _re
    _txt = (result_text or "").strip()
    if not _txt:
        return False
    try:
        import json as _json
        _parsed = _json.loads(_txt)
        if isinstance(_parsed, dict):
            for _key in ("artifacts", "issue_summary"):
                _val = _parsed.get(_key)
                _cands = [str(x) for x in _val] if isinstance(_val, list) else (
                    [_val] if isinstance(_val, str) else [])
                if any(_re.search(r"deploy_url:\s*https?://\S+", _c) for _c in _cands):
                    return True
    except (ValueError, TypeError):
        pass
    return bool(_re.search(r"deploy_url:\s*https?://\S+", _txt))


def _backfill_release_subissue_result(repo_key: str, issue_number: int,
                                      exit_code: int, result_text: str = "") -> bool:
    """发布子单终态回填（结果三分支，幂等）。

    - exit_code != 0 → deploy_failed（release_failure_reason 取 result_text 摘要）；
    - exit_code == 0 且结构化产出携带 deploy_url 证据 → deployed_success；
    - exit_code == 0 但无部署证据 → not_executed（阶段 completed 不视为发布成功）。
    已回填（release_result 非空）不再覆盖；非发布子单（release_target_env 空）零影响。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT release_target_env, release_result FROM issue "
                "WHERE repo_key=? AND issue_number=?",
                (repo_key, int(issue_number)))
            row = c.fetchone()
        if not row or not (row[0] or ""):
            return False   # 非发布子单
        if row[1]:
            return True    # 幂等：已回填
    except Exception as _e:
        log.warning(f"_backfill_release_subissue_result lookup failed: {_e}")
        return False
    if exit_code != 0:
        _result = "deploy_failed"
        _reason = ((result_text or "").strip() or "sre 任务失败（exit_code != 0）")[:500]
    elif _has_deploy_url(result_text or ""):
        _result = "deployed_success"
        _reason = ""
    else:
        _result = "not_executed"
        _reason = ""
    # B1 SQL 双模式：'release_' || ? 的 || 拼接不被 mysql_compat 翻译层覆盖（仅
    # 'gitcode:' || col 一种模式），MySQL 后端会把 || 当逻辑或 → 写入 0。
    # 改为应用层拼接，双后端语义一致。
    _completed_reason = f"release_{_result}"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE issue SET release_result=?, release_failure_reason=?, "
                "status='completed', completed_reason=?, "
                "stage='done', owner_agent='sre', completed_at=datetime('now'), "
                "updated_at=datetime('now') "
                "WHERE repo_key=? AND issue_number=? AND (release_result IS NULL OR release_result='')",
                (_result, _reason, _completed_reason, repo_key, int(issue_number)))
            _changed = c.rowcount > 0
            db.commit()
        if _changed:
            log.info(f"release subissue #{issue_number} backfilled: result={_result} "
                     f"repo={repo_key} exit={exit_code}")
        return _changed
    except Exception as _e:
        log.warning(f"_backfill_release_subissue_result write failed: {_e}")
        return False


def _find_open_issue_by_title(api_token: str, owner: str, repo: str, title: str,
                             provider: str = "gitcode") -> int | None:
    """幂等查找：同仓库已存在同名 open Issue → 返回其 number（避免重复建 issue）。

    Issue #343: 分页循环查全部 open issue，不只看前 50 条。
    Issue #343: 查重失败时抛异常而非静默返回 None，防止 token 失效时重复建单。
    Issue #344: GitCode 分支取消 10 页上限（while True 靠末页不足 50 条终止）。
    Issue #344: GitHub 分支改用 Search API 精确匹配（in:title + repo:scope）。
    返回 issue number（找到）或 None（真没找到）。token 失效时抛 RuntimeError。
    """
    import urllib.parse as _ul
    if provider == "github":
        # Issue #344: 改用 GitHub Search API 精确匹配 title，不再分页遍历
        from src.user.repo_manager import _github_req
        try:
            _q = _ul.quote(f'{title} in:title repo:{owner}/{repo} is:issue is:open')
            data = _github_req("GET",
                               f"https://api.github.com/search/issues?q={_q}&per_page=5",
                               api_token)
        except Exception as e:
            raise RuntimeError(f"幂等查重失败（token 可能失效）: {e}")
        # GitHub Search API returns {"items": [...]} on success;
        # error responses like {"message": "rate limit exceeded"} have no "items" key
        if not isinstance(data, dict) or "items" not in data:
            _msg = data.get("message", "") if isinstance(data, dict) else str(data)[:200]
            if _msg:
                raise RuntimeError(f"幂等查重失败（GitHub API 异常）: {_msg}")
            return None
        items = data.get("items") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("title") or "").strip() == title.strip():
                n = it.get("number")
                if n:
                    return int(n)
        return None
    # GitCode — Issue #344: 取消 10 页上限，while True 靠末页终止
    # 安全阀：最多遍历 1000 页（50000 条），防止 API 异常时无限循环
    from src.user.repo_manager import _req
    page = 1
    _max_pages = 1000
    while page <= _max_pages:
        try:
            data = _req("GET",
                        f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues?state=open&per_page=50&page={page}",
                        api_token)
        except Exception as e:
            raise RuntimeError(f"幂等查重失败（token 可能失效）: {e}")
        if isinstance(data, dict):
            _cand = data.get("data") if isinstance(data.get("data"), list) else (data.get("items") or data.get("records") or [])
            items = _cand or []
        else:
            items = data if isinstance(data, list) else []
        if not items:
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("title") or "").strip() == title.strip():
                st = str(it.get("state") or "").lower()
                if st in ("open", "opened", "active", "1", "todo"):
                    n = it.get("number") or it.get("iid") or it.get("id")
                    if n:
                        return int(n)
        if len(items) < 50:
            break  # 最后一页
        page += 1
    return None


def _gitcode_assignee_member_check(api_token: str, owner: str, repo: str, username: str) -> str:
    """Issue #649: 校验 assignee 是否为 GitCode 仓库成员（GET /collaborators/{u}/permission）。

    返回三态：
    - "member"     确认是成员（有协作者权限记录，可被指派）
    - "not_member" 确认非成员（上游 404）
    - "unknown"    无法判定（403/429/网络等）——放行交给上游 create 最终裁决，
                   避免「查询接口本身无权限」被误判成非成员而阻断合法请求。
    """
    import urllib.parse as _up
    import urllib.request as _ur
    import urllib.error as _uerr
    if not username or not api_token:
        return "unknown"
    _url = (f"https://gitcode.com/api/v5/repos/{owner}/{_up.quote(repo, safe='')}"
            f"/collaborators/{_up.quote(username, safe='')}/permission?access_token={api_token}")
    _req = _ur.Request(_url, headers={"Accept": "application/json"}, method="GET")
    try:
        with _ur.urlopen(_req, timeout=15) as _resp:
            _raw = _resp.read().decode(errors="replace")
        import json as _json
        _data = _json.loads(_raw) if _raw.strip() else {}
        # Gitee 系返回 {access_level, permission, role_name...}；access_level<=10 视为无实质权限
        _lvl = int(_data.get("access_level") or 0)
        return "member" if _lvl >= 20 else "not_member"
    except _uerr.HTTPError as e:
        if e.code == 404:
            return "not_member"
        log.warning(f"assignee member check undetermined (HTTP {e.code}): {username} @ {owner}/{repo}")
        return "unknown"
    except Exception as e:
        log.warning(f"assignee member check failed: {e}")
        return "unknown"


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


def _patch_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（追加多任务模式标记）。GitCode 版。"""
    import urllib.request as _ur, urllib.error as _uerr, json as _json
    url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/issues/{issue_num}?access_token={api_token}"
    data = _json.dumps({"body": body_text}).encode()
    req = _ur.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="PATCH")
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            _out = _json.loads(resp.read().decode()) if resp.read else {}
    except Exception:
        raise
    # Issue #453 台账（S21，第六轮补漏）：多任务模式 patch issue body → issue_edit 记账，
    # 编辑回声对账（原先 marker 检测在 GitCode 必 miss）
    try:
        from src.outbound_ledger import record_outbound, content_hash_md5
        from src.repo_key import to_repo_key
        record_outbound("gitcode", to_repo_key(f"{owner}/{repo}", "gitcode"), "issue_edit",
                        str(issue_num), issue_number=str(issue_num),
                        content_hash=content_hash_md5(body_text))
    except Exception as _le:
        from src.config import log as _log
        _log.warning(f"outbound_ledger record (_patch_issue_body) failed: {_le}")
    return _out


def _patch_github_issue_body(api_token: str, owner: str, repo: str, issue_num: int, body_text: str):
    """PATCH Issue body（GitHub 版）。"""
    from src.user.repo_manager import _github_req
    _out = _github_req("PATCH", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}",
                       api_token, {"body": body_text})
    # Issue #453 台账（S21，第六轮补漏）：GitHub 多任务模式 patch issue body → issue_edit 记账
    try:
        from src.outbound_ledger import record_outbound, content_hash_md5
        from src.repo_key import to_repo_key
        record_outbound("github", to_repo_key(f"{owner}/{repo}", "github"), "issue_edit",
                        str(issue_num), issue_number=str(issue_num),
                        content_hash=content_hash_md5(body_text))
    except Exception as _le:
        from src.config import log as _log
        _log.warning(f"outbound_ledger record (_patch_github_issue_body) failed: {_le}")
    return _out


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
    # Issue #344: 查重失败时返回明确错误，不抛异常中断 onboarding 流程
    try:
        issue_num = _find_open_issue_by_title(bot_token, owner, rname, title.strip())
    except RuntimeError as e:
        return {"ok": False, "code": 502, "message": f"查重失败，Bot Token 可能失效: {str(e)[:200]}"}
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
        # Issue #453 代发台账（写点6/7）：建任务主通道入账
        try:
            from src.outbound_ledger import record_outbound, content_hash_md5
            record_outbound(_provider, repo_key, "issue", str(issue_num), issue_number=str(issue_num),
                            content_hash=content_hash_md5((description or "").strip()))
        except Exception as _le:
            log.warning(f"outbound_ledger record (create_standard_task) failed: {_le}")
        created = True
    task_id = ensure_issue_task(repo_key, issue_num, title.strip(), description or "",
                                assignee=assignee, event_type="onboarding")
    if not task_id:
        return {"ok": False, "code": 500, "message": "任务建档失败"}
    # task_id 语义 = 需求卡片 issue.id（issues_center P2：主字段 issue_id，兼容保留 task_id）
    return {"ok": True, "issue_id": task_id, "task_id": task_id,
            "issue_number": issue_num, "created": created,
            "message": "任务已创建（Issue 已同步）" if created else "任务已创建（复用已有 Issue）"}


def _refresh_user_gitcode_token(user: dict) -> str:
    """用户自己的 GitCode token 401 时主动刷新一次（带锁，防并发）。

    返回刷新后的新 token；无 refresh_token / 刷新失败返回空串（不抛异常，
    由调用方决定最后提示「重新绑定/登录 GitCode」）。
    """
    try:
        from src.api.deps import try_refresh_gitcode_token
        return try_refresh_gitcode_token(user.get("id") or 0) or ""
    except Exception as _e:
        from src.config import log as _log
        _log.warning(f"create_task: refresh user gitcode token failed: {_e}")
        return ""


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
        # Issue #343: GitHub 写操作同样走仓库 bot token
        from src.user.db import get_github_token
        api_token = get_github_token(repo_key=repo_key)
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 GitHub Bot Token 未配置"})
    else:
        # 用户建单必须用用户自己的 GitCode token：assign 需用户身份（bot token 无 assign 权限，
        # GitCode 403）。token 缺失/失效 ≠ 回落 bot——明确引导用户重新绑定/登录 GitCode。
        from src.api.deps import get_user_token_with_refresh
        api_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
        if not api_token:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": "GitCode Token 未配置或已失效，请重新绑定/登录 GitCode 后重试",
            })
    # 是否使用「用户自己的 GitCode token」：用于 401 时区分「用户登录失效」（引导重新绑定）
    # 与「仓库 bot token 失效」（提示检查仓库 Bot Token 配置）两套文案。
    _using_user_token = (not _preauth_token) and (not _is_github)
    # Issue #649: assignee 不再默认填创建者——非成员默认指派会触发上游 403，
    # 误导排查方向。不传 assignee 时留空，由仓库方后续分派；显式传入时走下方成员资格预校验。
    assignee = (body.assignee or "").strip()
    # 内部流程（场景归属 issue_creator / pipeline 事件创建人）仍需要创建者的平台登录名，
    # 单独解析 _creator_login，绝不回填到 assignee（Issue #343: external_login 优先）。
    _creator_login = user["username"]
    try:
        from src.auth.grape_account_db import get_user_identities
        _provider_name = "github" if _is_github else "gitcode"
        for ident in get_user_identities(user["id"]):
            if ident.get("provider") == _provider_name and ident.get("external_login"):
                _creator_login = ident["external_login"]
                break
    except Exception:
        pass  # 查不到就用 grape username fallback
    # Issue #649: 本次建单请求级 trace_id（错误响应附带，便于排查定位）
    import uuid as _uuid
    _trace_id = f"tr_{_uuid.uuid4().hex[:16]}"
    # 1) 幂等：同 title open issue 已存在 → 提示用户任务已存在
    # Issue #343: 查重失败（token 失效）时给明确提示，不静默放行导致重复建单
    try:
        issue_num = _find_open_issue_by_title(api_token, owner, rname, body.title.strip(),
                                              provider="github" if _is_github else "gitcode")
    except RuntimeError as _dedup_err:
        from src.vcs.token_error import is_auth_error_exc, BOT_TOKEN_INVALID_MSG, GITCODE_USER_TOKEN_INVALID_MSG
        if is_auth_error_exc(_dedup_err) and _using_user_token:
            # 用户自己的 GitCode token 401 → 主动刷新一次并重试；仍失败给「重新登录」引导
            _renewed = _refresh_user_gitcode_token(user)
            if _renewed:
                api_token = _renewed
                try:
                    issue_num = _find_open_issue_by_title(api_token, owner, rname, body.title.strip(),
                                                          provider="gitcode")
                except RuntimeError as _dedup_err2:
                    _dedup_err = _dedup_err2
                else:
                    _dedup_err = None
            if _dedup_err is not None:
                # 用户自己的 GitCode token 确认失效（刷新失败或重试仍 401）
                # → 401 + reauth_required：前端 handleResp 收到后自动跳 GitCode OAuth 授权页，强制重新授权
                raise HTTPException(status_code=401, detail={
                    "code": 401,
                    "message": f"查重失败，{GITCODE_USER_TOKEN_INVALID_MSG}",
                    "action": "reauth_required",
                })
        elif is_auth_error_exc(_dedup_err):
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"查重失败，{BOT_TOKEN_INVALID_MSG}: {str(_dedup_err)[:200]}"})
        else:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"查重失败: {str(_dedup_err)[:200]}"})
    created = False
    if issue_num is not None:
        raise HTTPException(status_code=409, detail={"code": 409,
                                                     "message": f"任务已存在（Issue #{issue_num}），请输入新的任务标题",
                                                     "issue_number": issue_num})
    if issue_num is None:
        # Issue #649: 显式 assignee + GitCode → 上游创建前预校验成员资格；
        # 非成员直接给出「非成员」语义错误（含 trace_id），不再无谓打上游 403 再误报。
        if (not _is_github) and assignee:
            _member_state = _gitcode_assignee_member_check(api_token, owner, rname, assignee)
            if _member_state == "not_member":
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": f"创建 Issue 失败：assignee {assignee} 不是仓库 {owner}/{rname} 的成员，"
                               f"无法被指派。可不传 assignee（留空由仓库方分派），或改填该仓库成员。",
                    "trace_id": _trace_id,
                    "upstream_detail": f"member precheck: collaborator permission lookup returned 404 "
                                       f"for assignee '{assignee}' on {owner}/{rname}",
                })
        # Issue #1245: 建单前场景可用性预校验（查重 + assignee 预校验之后、GitCode 建单之前）。
        # 静态只读校验，不通过返回 400 + problems[] + trace_id；此时尚未产生任何 VCS/DB 副作用。
        # 显式场景直接校验；智能推荐（vod-auto）/留空时回落到仓库默认场景一并校验，
        # 与后续写入 issue 的 scene_id 口径一致，避免默认/推荐路径产生死单。
        _scene_for_precheck = body.scene_id if (body.scene_id and body.scene_id != "vod-auto") else ""
        if not _scene_for_precheck:
            try:
                from src.user.db import get_repo_scene
                _scene_for_precheck = get_repo_scene(repo_key) or ""
            except Exception as _rs_err:
                log.warning(f"create_task: get_repo_scene for precheck failed: {_rs_err}")
                _scene_for_precheck = ""
        if _scene_for_precheck:
            from src.api.scene_precheck import precheck_scene, summarize_problems
            _precheck = precheck_scene(_scene_for_precheck, repo_key, user)
            if not _precheck.get("ok"):
                _pc_problems = _precheck.get("problems") or []
                _pc_summary = summarize_problems(_pc_problems)
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"所选场景当前不可执行，已阻止建单：{_pc_summary}",
                    "problems": _pc_problems,
                    "trace_id": _precheck.get("trace_id") or _trace_id,
                })
        # 拼接 Task Center 上传的附件到 issue body（Issue #648：拼装逻辑抽为可复用函数）
        _desc_with_attach = (body.description or "").strip()
        _pending_attachments = _attach_list(owner, rname, "pending")
        _attach_md = _attachments_markdown_block(_pending_attachments)
        if _attach_md:
            _desc_with_attach += _attach_md
        # Issue #343 问题十一修复: 捕获建单 API 403/422，转成友好报错而非 500
        try:
            if _is_github:
                resp = _post_github_issue(api_token, owner, rname, body.title.strip(),
                                          _desc_with_attach, assignee=assignee)
            else:
                # Issue #343: GitCode 写操作 token（用户建单恒为用户 token）
                resp = _post_gitcode_issue(api_token, owner, rname, body.title.strip(),
                                           _desc_with_attach, assignee=assignee)
        except Exception as _create_err:
            _err_msg = str(_create_err)
            # 从 RuntimeError 消息提取 HTTP code（格式: "failed (403): ..."）
            import re as _re
            _codes = _re.findall(r'\((\d{3})\)', _err_msg)
            _http_code = int(_codes[0]) if _codes else 0
            if _http_code == 401 and _using_user_token:
                # 用户自己的 GitCode token 401 → 刷新一次再重试；仍失败走下方 401 分支给「重新登录」引导
                _renewed = _refresh_user_gitcode_token(user)
                if _renewed:
                    api_token = _renewed
                    try:
                        resp = _post_gitcode_issue(api_token, owner, rname, body.title.strip(),
                                                   _desc_with_attach, assignee=assignee)
                    except Exception as _create_err2:
                        _create_err = _create_err2
                        _err_msg = str(_create_err2)
                        _codes = _re.findall(r'\((\d{3})\)', _err_msg)
                        _http_code = int(_codes[0]) if _codes else 0
                    else:
                        # 刷新重试成功：交由下方统一「响应无 issue number」校验提取 number
                        _create_err = None
            if _create_err is None:
                pass  # 刷新重试成功，跳过异常分支
            elif _http_code == 403:
                # Issue #523: 403 不再仅凭状态码断言 assignee 无效——提取上游响应正文（脱敏+截断）按语义分流
                _up_403_parts = _re.split(r'\(403\):\s*', _err_msg, maxsplit=1)
                _up_403 = _up_403_parts[1].strip() if len(_up_403_parts) > 1 else ''
                # 基础脱敏：token/凭据类键值不回显
                _up_403 = _re.sub(
                    r'(?i)(access_token|refresh_token|token|password|authorization|api[_-]?key|secret[_-]?key|ak|sk)[=:]\s*\S+',
                    r'\1=***', _up_403)[:200]
                _up_403_low = _up_403.lower()
                if 'prohibited for model repository and dataset repository' in _up_403_low:
                    # GitCode Space/模型仓/数据集仓能力限制 → 仓库类型不支持建单
                    raise HTTPException(status_code=403, detail={
                        "code": 403,
                        "message": "创建 Issue 失败：当前仓库类型（如 GitCode Space/模型仓/数据集仓）"
                                   "不支持创建 Issue，请改用普通代码仓发起协作"
                                   + (f"（上游响应: {_up_403}）" if _up_403 else "")
                    })
                if _up_403 and any(kw in _up_403_low for kw in (
                        'user not found', 'user is not found', 'permission', 'forbidden',
                        'member', 'privilege', 'unauthorized', 'not authorized')):
                    # Issue #649: 上游 403 权限/成员类 → 归类为「非成员/无权指派」语义错误，
                    # 主文案指向真实原因（成员资格），不再误报「assignee 用户名无效」误导排查；
                    # trace_id + 上游脱敏原文作为辅助字段（detail，非主文案）保留可追溯。
                    if assignee:
                        raise HTTPException(status_code=403, detail={
                            "code": 403,
                            "message": f"创建 Issue 失败：无权指派 assignee（assignee={assignee}，"
                                       f"非仓库 {owner}/{rname} 成员或无指派权限）。"
                                       f"可不传 assignee（留空由仓库方分派）后重试。",
                            "trace_id": _trace_id,
                            "upstream_detail": _up_403,
                        })
                    # assignee 为空的权限类 403（创建者自身无权限等）→ 通用提示附上游脱敏正文
                    raise HTTPException(status_code=403, detail={
                        "code": 403,
                        "message": f"创建 Issue 失败（上游 403）: {_up_403}"
                    })
                # 无法识别的 403 → 通用提示附上游脱敏正文，不猜测归因
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": f"创建 Issue 失败（上游 403）: {_up_403 or _err_msg[:200]}"
                })
            elif _http_code == 422:
                raise HTTPException(status_code=422, detail={
                    "code": 422,
                    "message": f"创建 Issue 失败：参数错误: {_err_msg[:200]}"
                })
            elif _http_code == 401:
                # Issue #345: 区分用户 token / bot token 两套 401 提示
                if _using_user_token:
                    # 用户自己的 GitCode token 401 且刷新失败 → 401 + reauth_required，
                    # 前端自动跳 GitCode OAuth 授权页强制重新授权（用户登录凭据已失效）
                    from src.vcs.token_error import GITCODE_USER_TOKEN_INVALID_MSG
                    raise HTTPException(status_code=401, detail={
                        "code": 401,
                        "message": f"创建 Issue 失败：{GITCODE_USER_TOKEN_INVALID_MSG}",
                        "action": "reauth_required",
                    })
                from src.vcs.token_error import BOT_TOKEN_INVALID_MSG
                raise HTTPException(status_code=502, detail={
                    "code": 502,
                    "message": f"创建 Issue 失败：{BOT_TOKEN_INVALID_MSG}",
                })
            else:
                raise HTTPException(status_code=502, detail={
                    "code": 502,
                    "message": f"创建 Issue 失败: {_err_msg[:200]}"
                })
        n = None
        if isinstance(resp, dict):
            n = resp.get("number") or resp.get("iid") or resp.get("id")
        if not n:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"创建 GitCode Issue 失败（响应无 issue number）: {str(resp)[:200]}"})
        issue_num = int(n)
        # Issue #453 代发台账（写点6/7，前端建单入口）：入账
        try:
            from src.outbound_ledger import record_outbound, content_hash_md5
            from src.repo_key import parse_repo_key
            _prov, _, _ = parse_repo_key(repo_key)
            record_outbound(_prov, repo_key, "issue", str(issue_num), issue_number=str(issue_num),
                            content_hash=content_hash_md5(_desc_with_attach))
        except Exception as _le:
            log.warning(f"outbound_ledger record (create issue) failed: {_le}")
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
    #    选智能推荐（vod-auto）或留空 → 仓库无默认场景时不写 scene_id → issue_opened 走智能推荐
    #    Issue #415: 无显式场景且仓库配置默认场景（managed_repos.dev_scene）→ 回退写入，
    #    保证对话通道创建的 Issue 详情页「流水线场景」不显示 --。
    try:
        from src.api.session_store import set_issue_scene
        from src.runtime.engine_select import _repo_user_id
        _scene_to_write = ""
        if body.scene_id and body.scene_id != "vod-auto":
            _scene_to_write = body.scene_id
        else:
            # 无显式场景 → 仓库默认场景兜底（未配置则保持空，走智能推荐）
            try:
                from src.user.db import get_repo_scene
                _scene_to_write = get_repo_scene(repo_key) or ""
            except Exception as _re:
                log.warning(f"create_task: get_repo_scene failed: {_re}")
                _scene_to_write = ""
        if _scene_to_write:
            # Issue #343 问题十一修复: issue_creator 必须是 GitCode/GitHub 登录名（external_login），
            # 不能用平台名 user["username"]——_repo_user_id 查的是 unified_user_mapping.gitcode_user
            scene_uid = _repo_user_id(repo_key, issue_creator=_creator_login) or 0
            set_issue_scene(repo_key, f"#{issue_num}", _scene_to_write, scene_user_id=scene_uid)
            log.info(f"create_task: scene={_scene_to_write} set for #{issue_num}"
                     + ("" if (body.scene_id and body.scene_id != "vod-auto") else " (repo default fallback)"))
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

    # 4) 按既有流程幂等建档（同 issue 任务 id 复用）；org 归属由 ensure_issue_task 统一以仓库为准
    task_id = ensure_issue_task(repo_key, issue_num, body.title.strip(), body.description or "", assignee=assignee,
                                subtask_mode=_auto_subtask_mode,
                                owner_user_id=int(user.get("id") or 0))
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
                # Issue #343: GitHub 写操作走 bot token
                _patch_github_issue_body(api_token, owner, rname, issue_num, (body.description or "") + _marker)
            else:
                # Issue #343: GitCode 写操作走 bot token
                _patch_issue_body(api_token, owner, rname, issue_num, (body.description or "") + _marker)
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
            # 方案 D（duplicate-issue-opened-event-fix-design.md）：按平台发原生 payload，
            # 使补发事件与对应平台真实 webhook 形态一致（可读性/排查 + 去重指纹一致），
            # 不再对 GitCode 也发 GitHub 格式合成 payload 造成「GitHub 事件」误判。
            import json as _pipe_json
            # Issue #343/#649: pipeline 创建人 = 创建者 external_login（_creator_login），
            # 与 assignee 解耦——不传 assignee 时创建人字段仍正确，不再回填 assignee
            _assign = _creator_login or ""
            if _is_github:
                _pipe_payload = {
                    "action": "opened",
                    "issue": {"number": int(issue_num), "title": body.title.strip(),
                              "user": {"login": _assign} if _assign else {}},
                    "repository": {"full_name": f"{owner}/{rname}"},
                    "sender": {"login": _assign} if _assign else {},
                }
                _pipe_headers = {"Content-Type": "application/json", "X-GitHub-Event": "issues"}
                # Issue #453：内部同步触发标记（同 GitCode 分支，跳过台账判定）
                _pipe_payload["_grape_internal_trigger"] = True
            else:
                _pipe_payload = {
                    "object_kind": "issue",
                    "object_attributes": {"iid": int(issue_num), "title": body.title.strip(),
                                          "action": "open"},
                    "project": {"path_with_namespace": f"{owner}/{rname}"},
                    "user": {"username": _assign} if _assign else {},
                }
                _pipe_headers = {"Content-Type": "application/json", "X-Gitcode-Event": "issue_opened"}
            # Issue #453：建单直发同步触发——payload 为内部构造，非 webhook 回流。
            # 该 issue 已入台账（建单入账），台账检查会命中并拦掉这次触发；
            # _grape_internal_trigger 标记让读侧跳过台账判定（同步触发是我们自己要开工，
            # 与 webhook 回流去重语义相反）。
            # 审查 P0-1 修正：run_pipeline 调用移回 if/else 之外（两平台共用）——
            # 原版本误缩进进 else（GitCode）分支，GitHub 建单后不再同步触发流水线。
            _pipe_payload["_grape_internal_trigger"] = True
            from src.api.event_pipeline import run_pipeline
            _pipe_result = run_pipeline(_pipe_json.dumps(_pipe_payload).encode(), _pipe_headers,
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

_ATTACH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
# Issue #648：GitCode 同步评论是永久评论，不能写 7 天签名 URL（到期失效）；
# 同步时重新签发长效链接（10 年）作为永久可访问 URL 的过渡方案
_ATTACH_SYNC_URL_EXPIRES = 10 * 365 * 24 * 3600
_ATTACH_DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".xml", ".html"}
_ATTACH_CODE_EXTS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".sh", ".sql", ".tf", ".dockerfile"}
_ATTACH_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z"}
_ATTACH_ALL_EXTS = _ATTACH_IMAGE_EXTS | _ATTACH_DOC_EXTS | _ATTACH_CODE_EXTS | _ATTACH_ARCHIVE_EXTS
_ATTACH_MAX_SIZE = (int(os.environ.get("ATTACH_MAX_SIZE_MB", "200")) * 1024 * 1024
                    )  # 单个附件上限：默认 200MB，可用 ATTACH_MAX_SIZE_MB 覆盖（需同步 nginx client_max_body_size）


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
    from src.user.db import get_browser_callback_url as _bcb
    _base_url = (_bcb() or "").rstrip("/")
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


def _attach_count(owner: str, repo: str, issue_number) -> int:
    """Issue #926：仅统计附件数量（分页列表瘦身）。

    与 _attach_list 同存储口径，但跳过 URL 预签名与元数据 dict 构造。
    """
    from src.storage import get_storage
    storage = get_storage()
    if storage:
        prefix_key = _attach_obs_key(owner, repo, issue_number, "")
        items, _ = storage.list_objects_with_meta(prefix_key)
        return sum(1 for item in items if item["key"].rsplit("/", 1)[-1])
    attach_dir = _attach_base_dir(owner, repo, issue_number)
    if os.path.isdir(attach_dir):
        return sum(1 for f in os.listdir(attach_dir)
                   if os.path.isfile(os.path.join(attach_dir, f)))
    return 0


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
    # Issue #471 E1：附件 URL 是浏览器面地址——走 browser_callback_url
    from src.user.db import get_browser_callback_url as _bcb
    _base_url = (_bcb() or "").rstrip("/")
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


def _attachments_markdown_block(attachments) -> str:
    """Issue #648：把附件清单拼为「## 附件」markdown 链接清单（GitCode 同步过渡方案）。

    图片用 ![]()（可直接预览），其余用 []()（下载链接）；
    空清单/无效条目返回空串（调用方按需追加）。
    """
    lines = []
    for _att in attachments or []:
        if not isinstance(_att, dict):
            continue
        _fname = str(_att.get("filename") or "").strip()
        _url = str(_att.get("url") or "").strip()
        if not _fname or not _url:
            continue
        if _att.get("is_image"):
            lines.append(f"![{_fname}]({_url})")
        else:
            lines.append(f"[{_fname}]({_url})")
    if not lines:
        return ""
    return "\n\n## 附件\n" + "\n".join(lines)


def sync_issue_attachment_comment(repo_key: str, issue_number, attachment: dict) -> bool:
    """Issue #648 过渡方案：附件上传后向 VCS issue 同步「## 附件」链接清单评论。

    best-effort：token 获取 / VCS 调用失败仅记日志并返回 False，不影响上传主流程。
    仅支持 gitcode（GitHub 附件同步不在本次范围）。
    """
    try:
        from src.repo_key import parse_repo_key
        _prov, owner, rname = parse_repo_key(repo_key)
        if _prov != "gitcode":
            return False
        _att = dict(attachment) if isinstance(attachment, dict) else {}
        _url = str(_att.get("url") or "")
        # Issue #648：传入 url 是 OBS 7 天签名 URL，GitCode 评论永久存在会到期失效；
        # OBS 后端启用时按对象键重新签发长效链接（本地降级路径 URL 本身永久，无需处理）
        if _url:
            try:
                from src.storage import get_storage
                _storage = get_storage()
                if _storage:
                    _safe_name = _url.rsplit("/", 1)[-1].split("?")[0]
                    if _safe_name:
                        _att["url"] = _storage.temporary_url(
                            _attach_obs_key(owner, rname, issue_number, _safe_name),
                            expires=_ATTACH_SYNC_URL_EXPIRES)
            except Exception as e:
                log.warning(f"attachment sync comment long-url resign failed "
                            f"repo={repo_key} #{issue_number}: {e}")
        _md = _attachments_markdown_block([_att])
        if not _md:
            return False
        from src.user.db import get_gitcode_token
        token = get_gitcode_token(repo_key=repo_key) or ""
        if not token:
            log.warning(f"attachment sync comment skipped: no repo token repo={repo_key}")
            return False
        from src.vcs.api_client import VcsApiClient
        client = VcsApiClient("gitcode", token, owner, rname)
        client.post_issue_comment(int(issue_number), _md.strip())
        log.info(f"attachment sync comment posted: repo={repo_key} issue=#{issue_number}"
                 f" file={(attachment or {}).get('filename')}")
        return True
    except Exception as e:
        log.warning(f"attachment sync comment failed repo={repo_key} #{issue_number}: {e}")
        return False


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


# === Issue #315: GitCode 编辑器上传图片（user-images）服务端鉴权下载 ===

def _user_image_ext(url: str, content_type: str = "") -> str:
    """从 URL 末段/Content-Type 推断图片扩展名（默认 .png）。"""
    last = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    ext = os.path.splitext(last)[1].lower()
    if ext in _ATTACH_IMAGE_EXTS:
        return ext
    ct_map = {"image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
              "image/svg+xml": ".svg", "image/bmp": ".bmp"}
    return ct_map.get(content_type, ".png")


def _collect_authed_attachments(owner: str, repo: str, issue_number, texts, repo_key: str, *,
                                extract_urls, download_method: str, max_count: int,
                                classify, filename_prefix: str, use_uuid: bool = False,
                                prefilter=None, analyzer=None) -> dict:
    """Issue #315/#1748 共用采集编排：提取链接 → 鉴权下载 → 白名单校验 → 转存平台附件。

    参数化差异：链接提取函数、下载方法、类型/扩展名判定、文件名策略、预过滤、分析器。
    token 仅服务端构造请求头使用，不写入日志/注入上下文/返回值。
    顶层异常不外抛（返回带 error 字段的降级结果），绝不阻塞 prompt 组装主流程。
    """
    result: dict = {"ok": [], "failed": []}
    try:
        urls: list[str] = []
        for t in texts or []:
            if not t:
                continue
            for u in extract_urls(str(t)):
                if u not in urls:
                    urls.append(u)
        if not urls:
            return result
        # 数量上限：超出部分降级说明（不静默丢弃）
        for u in urls[max_count:]:
            result["failed"].append({"url": u, "reason": "exceeds count limit"})
        urls = urls[:max_count]

        repo_key = repo_key or f"gitcode:{owner}/{repo}"
        token = ""
        try:
            from src.user.db import get_gitcode_token
            token = get_gitcode_token(repo_key=repo_key) or ""
        except Exception:
            token = ""
        if not token:
            for u in urls:
                result["failed"].append({"url": u, "reason": "no repo token"})
            return result

        import uuid
        from src.vcs.api_client import create_client
        client = create_client(repo_key, token)
        seq = 0
        for u in urls:
            if prefilter is not None and not prefilter(u):
                log.info(f"{filename_prefix} ignored (non-media): {owner}/{repo}/{issue_number}")
                continue
            dl = getattr(client, download_method)(u)
            if "error" in dl:
                result["failed"].append({"url": u, "reason": str(dl["error"])})
                continue
            content_type = str(dl.get("content_type") or "")
            info = classify(u, content_type)
            if not info:
                log.info(f"{filename_prefix} ignored (unsupported type): "
                         f"{owner}/{repo}/{issue_number} ct={content_type or 'unknown'}")
                continue
            kind, ext = info
            if use_uuid:
                filename = f"{uuid.uuid4().hex}{ext}"
            else:
                seq += 1
                filename = f"{filename_prefix}-{seq}{ext}"
            try:
                url = _attach_save(owner, repo, issue_number, filename, dl["content"])
                item = {"filename": filename, "url": url,
                        "size": dl.get("size", len(dl["content"]))}
                analysis = ""
                if analyzer is not None and kind in ("image", "video"):
                    analysis = analyzer(kind, dl["content"], ext)
                if kind is not None:
                    item["kind"] = kind
                    item["source_url"] = u
                if analyzer is not None:
                    item["analysis"] = analysis
                result["ok"].append(item)
                log.info(f"{filename_prefix} saved: {owner}/{repo}/{issue_number}/{filename} "
                         f"({dl.get('size', 0)} bytes, kind={kind}, analyzed={bool(analysis)})")
            except Exception as e:
                result["failed"].append({"url": u, "reason": f"save failed: {e}"})
    except Exception as e:
        result["error"] = str(e)
        log.warning(f"_collect_authed_attachments failed: {owner}/{repo}/{issue_number} -> {e}")
    return result


# === Issue #315: GitCode 编辑器上传图片（user-images）服务端鉴权下载 ===

def _collect_issue_user_images(owner: str, repo: str, issue_number, texts, repo_key: str = "") -> dict:
    """提取 Issue 正文/评论中 raw.gitcode.com/user-images 图片链接并鉴权下载转存为平台附件。

    复用 _attach_save 存储语义（OBS 签名 URL 或 /static/issue_attachments/）。
    返回值 {"ok": [{"filename", "url", "size"}], "failed": [{"url", "reason"}]}，
    顶层异常不外抛（返回带 error 字段的降级结果）。
    """
    from src.vcs.api_client import (extract_gitcode_user_image_urls, USER_IMAGE_MAX_COUNT)

    def _classify(u: str, content_type: str):
        return (None, _user_image_ext(u, content_type))

    return _collect_authed_attachments(
        owner, repo, issue_number, texts, repo_key,
        extract_urls=extract_gitcode_user_image_urls,
        download_method="download_user_image", max_count=USER_IMAGE_MAX_COUNT,
        classify=_classify, filename_prefix="user-image")


# === Issue #1748: 评论媒体附件（gitcode.com/user-attachments，视频/图片）服务端鉴权下载 ===

def _analyze_media_attachment(kind: str, content: bytes, ext: str = "") -> str:
    """Issue #1748：复用现有图片/视频理解能力分析已下载的评论媒体附件。

    返回带「## 用户上传的图片/视频分析」标题的注入文本；分析不可用时返回空串。
    分析模块缺失/异常一律降级为空串，由上层注入明确原因，不阻塞主流程。
    """
    try:
        if kind == "image":
            from src.agent.image_understanding import understand_image_from_bytes
            return understand_image_from_bytes(content, ext or ".png")
        if kind == "video":
            from src.agent.image_understanding import understand_video_from_bytes
            text = understand_video_from_bytes(content, ext or ".mp4")
            if text and "## 用户上传的视频分析" not in text:
                text = f"## 用户上传的视频分析\n{text}\n\n## 请基于以上视频信息回答用户问题"
            return text
    except Exception as e:
        log.warning(f"_analyze_media_attachment failed ({kind}): {e}")
    return ""


def _collect_issue_media_attachments(owner: str, repo: str, issue_number, texts,
                                     repo_key: str = "", analyze: bool = True) -> dict:
    """Issue #1748：提取评论中的 user-attachments 媒体附件并鉴权下载转存为平台附件。

    评论附件直链需登录态（匿名 401），且事件注入原本不含媒体分析，形成双重断链。
    本函数在事件构建期用服务端凭证下载，经 `_attach_save` 转存为平台附件，
    返回无 401 的读取通道 URL，供上层注入媒体分析段落。

    Spec 限定 media-only：非媒体扩展名预过滤、未知类型下载后按 Content-Type 再判，
    非图片/视频一律不落盘不注入；扩展名仅接受媒体白名单并以 UUID 命名。
    返回值顶层异常不外抛（带 error 字段），绝不阻塞 prompt 组装主流程。
    """
    from src.vcs.api_client import (
        extract_gitcode_user_attachment_urls, USER_ATTACHMENT_MAX_COUNT,
        attachment_kind, _attachment_ext, is_media_attachment_url,
    )

    def _classify(u: str, content_type: str):
        kind = attachment_kind(u, content_type)
        ext = _attachment_ext(u, content_type, kind)
        if kind not in ("image", "video") or not ext:
            return None
        return (kind, ext)

    return _collect_authed_attachments(
        owner, repo, issue_number, texts, repo_key,
        extract_urls=extract_gitcode_user_attachment_urls,
        download_method="download_user_attachment", max_count=USER_ATTACHMENT_MAX_COUNT,
        classify=_classify, filename_prefix="user-attachment", use_uuid=True,
        prefilter=is_media_attachment_url,
        analyzer=_analyze_media_attachment if analyze else None)


def _get_task_issue_info(task_id: int) -> tuple[str, int, str] | None:
    """从 tasks 表取 repo_key + issue_number + conversation_id（关联 agent_tasks 的键）。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number, conversation_id FROM issue WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return None
    return row[0], row[1], row[2] or ""


def _find_agent_task_ids(repo_key: str, issue_number) -> list[str]:
    """按 repo_key + issue_id 关联 agent_tasks（agent 执行产生的 task_messages 以此为外键）。

    issue_number 兼容 int / str：
    - int 56 或 '56' → 规范化为 '#56'（agent_tasks.issue_id 统一带 # 前缀存储）
    - '#56' → 原样使用
    """
    from src.repo_key import to_repo_key
    rk = to_repo_key(repo_key)
    iid = str(issue_number).strip()
    if not iid.startswith("#"):
        iid = "#" + iid
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM agent_tasks WHERE repo_key=? AND issue_id=? ORDER BY created_at DESC",
            (rk, iid))
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

def _build_creator_filter(creators: str, clauses: list, params: list):
    """Issue #805：将逗号分隔的 creators 转成 created_by SQL 过滤（单值 =? / 多值 IN）。

    与 _build_assignee_filter 并列：assignees 仍按责任人过滤（向后兼容），
    creators 按创建人过滤（与 GitCode issue 作者口径一致）。
    """
    _list = [a.strip() for a in (creators or "").split(",") if a.strip()]
    if len(_list) == 1:
        clauses.append("created_by=?")
        params.append(_list[0])
    elif len(_list) > 1:
        clauses.append(f"created_by IN ({','.join('?' * len(_list))})")
        params.extend(_list)

def _batch_fetch_pr_issue_keys(keys: list) -> set:
    """批量查 events 表，返回 issue 本身是 PR 的 (repo_key, issue_id) 集合。

    跨仓库 A2A 场景中，agent 在目标仓库创建 PR（而非 Issue），
    issue 表存的 issue_number 实际是 PR 编号，前端链接应用 /pull/ 而非 /issues/。

    判定收紧（需求详情页标签误判修复）：PR 类事件挂 #N 有两种含义——
    ① 跨仓 A2A 建的"PR 形态需求"（issue_number 即 PR 号，无 issue 生命周期事件）；
    ② 普通真实 Issue 的交付关联（PR resolved/Closes #N，PR 事件只是来交付这个 issue 的，
    issue 侧同时存在 issue_opened 等生命周期事件）。
    ② 不算 PR——否则需求流转到待审核/已完成时详情页 Issue 行错标成 PR。
    只有"存在 PR 事件且从无 issue 生命周期事件"的编号才判为 ①。
    """
    from src.vcs.event_types import ALL_PR_EVENTS, ALL_ISSUE_EVENTS
    _pr_types = tuple(ALL_PR_EVENTS)
    _issue_types = tuple(ALL_ISSUE_EVENTS)
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
                _pr_hits = {(row[0], row[1]) for row in c.fetchall()}
                if not _pr_hits:
                    continue
                # 排除②：同编号存在 issue 生命周期事件 → 真实 Issue，PR 只是交付关联
                _ph2 = ",".join("?" * len(_issue_types))
                _ors2 = " OR ".join(["(repo_key=? AND issue_id=?)"] * len(_chunk))
                c.execute(
                    f"SELECT DISTINCT repo_key, issue_id FROM events "
                    f"WHERE ({_ors2}) AND event_type IN ({_ph2})",
                    _flat + list(_issue_types))
                _issue_hits = {(row[0], row[1]) for row in c.fetchall()}
                out |= (_pr_hits - _issue_hits)
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
    """批量查"正在运行"的 agent_tasks，返回 {(repo_key, issue_id)}（防 N+1，Issue #85 雷达动画依据）。

    口径（local-engine-busy-slots-zombie-fix-design.md §8）：与流水线点亮一致——只认
    `status='running'`（任务真正被认领执行；事件此刻同步 running）。`queued`（排队未认领/
    事件 pending）与 `dispatched`（历史 pull 语义、事件未同步 running）都不算，避免
    "任务仅排队中但需求中心雷达已在扫描、流水线未点亮" 的不一致。
    """
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
                    "WHERE status = 'running' AND (" + _ors + ")", _flat)
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


def get_handoff_targets(repo_key: str, issue_number: int, current_scene_id: str = "") -> dict:
    """Issue #337: 计算某需求「可移交目标流水线」列表（列表模式详情移交入口数据源）。

    - 数据源：`get_scenes(repo_key)`（与 /scenes/with-pipeline 同源；仓库作用域即权限过滤，
      调用方仍需先 `check_repo_access`）。
    - 过滤：仅保留含 vod 节点的场景（与 _cross_pipeline_handoff 目标节点约定一致）。
      Issue #450：不再排除当前流水线，目标列表包含当前场景自身（支持同流水线内移交接续）。
    - 返回 {targets: [{id, name, display_name, description, work_branch, scene_type}],
            current_scene_id}。空列表照常返回（前端展示「无可用目标」，非静默失败）。
    """
    from src.api.pipeline_config import get_scenes
    _cur = (current_scene_id or "").strip() or "default"
    targets = []
    try:
        for s in get_scenes(repo_key=repo_key):
            _sid = s.get("id") or ""
            if not _sid:
                continue
            _nodes = s.get("nodes") or []
            _has_vod = any((n.get("id") or n.get("tag")) == "vod" for n in _nodes)
            if not _has_vod:
                continue
            targets.append({
                "id": _sid,
                "name": s.get("name") or _sid,
                "display_name": s.get("display_name") or s.get("name") or _sid,
                "description": s.get("description") or "",
                "work_branch": s.get("work_branch") or "main",
                "scene_type": s.get("scene_type") or "standard",
            })
    except Exception as _e:
        log.warning(f"get_handoff_targets failed repo={repo_key} issue=#{issue_number}: {_e}")
        targets = []
    return {"targets": targets, "current_scene_id": _cur}


def _query_tasks(where: str, params: list, order_by: str, include_user_id: bool = False,
                 limit: int = None, offset: int = 0) -> list[dict]:
    """查询 tasks 并富化（scene / stage / is_running / is_pr）为对外 dict（my_tasks / repo_all_tasks 共享）。

    - where / params / order_by 由调用方构造（列与排序方向经白名单，值参数化，防注入）
    - include_user_id: 结果是否包含 user_id（repo_all 需要）
    - limit/offset（Issue #542）：可选真分页（None=不限，返回全部）
    - issue_state / agent_tasks 均批量查询，避免逐任务 N+1
    - is_pr: 批量查 events 表 event_type 判断是否为 PR 事件（跨仓库 A2A 场景 issue_number 实际是 PR 编号）
    """
    _select = (
        "SELECT id, assignee, repo_key, issue_number, title, task_description, conversation_id, "
        "status, completed_reason, created_at, completed_at, is_subtask_mode, parent_issue_number, parent_issue_id, created_by"
        + (", user_id" if include_user_id else "")
        + ", vcs_exists, vcs_checked_at"
    )
    _vcs_idx = 15 + (1 if include_user_id else 0)
    _limit_sql = ""
    if limit is not None and int(limit) > 0:
        _limit_sql = f" LIMIT {int(limit)} OFFSET {int(offset)}"
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"{_select} FROM issue WHERE {where} ORDER BY {order_by}{_limit_sql}", params)
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

    # Issue #415: 仓库默认场景缓存（无 Issue 级场景时展示回退用，managed_repos.dev_scene）
    _repo_default_scene: dict[str, str] = {}
    for idx, r in enumerate(rows):
        _rk = _keys[idx][0]
        if _rk in _repo_default_scene:
            continue
        try:
            from src.user.db import get_repo_scene
            _repo_default_scene[_rk] = get_repo_scene(_rk) or ""
        except Exception:
            _repo_default_scene[_rk] = ""

    # P1-B: Pre-resolve scene names in batch (loop-out).
    # Collect unique (scene_id, repo_key) pairs and resolve once, instead of
    # calling get_pipeline_config per-card (348× → 2-5× with cache).
    _scene_name_cache: dict[tuple, str] = {}
    for idx, r in enumerate(rows):
        _rk = _keys[idx][0]
        _st = _stage_map.get((_rk, _keys[idx][1]))
        _issue_scene_id = (_st.get("scene_id") or "") if _st else ""
        if not _issue_scene_id:
            _issue_scene_id = _repo_default_scene.get(_rk, "")  # 展示回退：仓库默认场景
        _scene_key = (_issue_scene_id, _rk)
        if _scene_key not in _scene_name_cache:
            _scene_name = ""
            try:
                _cfg = get_pipeline_config(_issue_scene_id, repo_key=_rk)
                if _cfg:
                    _scene_name = _cfg.get("display_name") or _cfg.get("name", _issue_scene_id)
            except Exception:
                pass
            _scene_name_cache[_scene_key] = _scene_name

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
            # 无 Issue 级场景 → 仓库默认场景（Issue #415：回退展示，避免详情页显示 --）
            _scene_id = _repo_default_scene.get(_rk, "")
            _scene_source = "repo_default"
        _scene_name = _scene_name_cache.get((_scene_id, _rk), "")
        _has_running_agent = bool(r[3]) and _stage in ("vod", "issue", "coding", "qa", "sre") and ((_rk, _issue_id) in _running)
        item = {
            "id": r[0], "assignee": r[1], "repo_key": r[2], "issue_number": r[3],
            "title": r[4] or "", "task_description": r[5] or "",
            "conversation_id": r[6] or "", "status": r[7],
            "completed_reason": r[8] or "", "created_at": normalize_event_time(r[9]), "completed_at": normalize_event_time(r[10]),
            "is_subtask_mode": r[11] or 0,
            "parent_issue_number": r[12] or 0,
            "parent_issue_id": r[13] or "",
            # Issue #805: 创建人（issue.created_by，建档固化，不随 assignee 流转变化）
            "creator": r[14] or "",
            "scene_id": _scene_id,
            "scene_name": _scene_name,
            "scene_source": _scene_source,
            "stage": _stage,
            "is_running": _has_running_agent,
            "agent_statuses": _agent_status_map.get((_rk, _issue_id), []),
            "is_pr": (_rk, _issue_id) in _pr_issue_keys,
            # Issue #995：VCS 存在性（默认 1=未知/存在；0=VCS 侧已删除）；未知列值按存在处理。
            "vcs_exists": bool(r[_vcs_idx]) if r[_vcs_idx] is not None else True,
            "vcs_checked_at": r[_vcs_idx + 1] or "",
        }
        if include_user_id:
            item["user_id"] = r[15] or 0
        tasks_out.append(item)
    return tasks_out
