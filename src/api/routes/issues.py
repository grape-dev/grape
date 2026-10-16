"""Issues 路由：以 Issue 为中心对外整体开放（阶段 + 事件 + pipeline）。

与 /api/tasks、/api/events 不同，本路由按 「Issue 视角」聚合：
- issue 基本信息 + pipeline 阶段（issue_state.stage/owner_agent/scene）+ 任务中心状态（tasks.status）
- 该 Issue 的事件流（events 表，按 issue_id 聚类）
- Issue pipeline 路线图（event.json 结构化链）

认证：`get_current_user_or_api_key`（JWT / tk_ 会话 token / grape_ API Key 三通道）。
- 读类（列表/详情/事件/pipeline/GET status）→ dual 可达。
- 写/操作类（create/start/confirm-done/cancel/resume/PUT status/clarify/attachments 写）→ `require_no_session_token`
  （用户仅限 user_only，`tk_` → 403；避免 Agent 任意改状态干扰 pipeline）。
Design: docs/weekly-02/issues-api-design.md
"""
import os
import re
import asyncio

from fastapi import APIRouter, HTTPException, Depends, Query, File, UploadFile
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key, require_no_session_token, require_org_member
from src.config import log
from src.api.time_utils import normalize_event_time
from src.api.issues_center import (TaskAwaitingInputBody, TaskPendingReviewBody,
                                   _review_pass, _review_reject, _set_issue_pending_review)

router = APIRouter(prefix="/api/issues", tags=["issues"])


# ===================== 辅助 =====================

def _num_from_issue_id(issue_id: str) -> str:
    """从 issue_id 提取编号。

    Issue #252: #pr_ 前缀的 PR 订阅伪 ID 不参与 issue 事件分组
    （它们由 _query_pr_subscription_events 独立处理，不应混入同编号的 issue 分组）。
    """
    _raw = str(issue_id or "")
    if _raw.startswith("#pr_"):
        return ""
    m = re.search(r"(\d+)", _raw)
    return m.group(1) if m else ""


def _enforce_session_repo_scope(user: dict, repo_keys: list[str]):
    """tk_ 会话 token 限定其绑定仓库：请求 repo_keys 含该 repo 才放行（防 agent 跨仓库读事件）。"""
    if user.get("auth_type") == "session_token":
        _tok_repo = user.get("repo_key") or ""
        if _tok_repo and _tok_repo not in repo_keys:
            raise HTTPException(
                status_code=403,
                detail={"code": 403, "message": "会话 token 无权访问该仓库事件"})


def _query_pr_subscription_events(repo_keys: list[str]) -> list[dict]:
    """查 PR 订阅事件（issue_id LIKE '#pr_%'），按 PR 号聚合为伪卡片。"""
    from src.session_manager import db, db_lock
    items = []
    placeholders = ",".join("?" * len(repo_keys))
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, result, scene_id "
                f"FROM events WHERE repo_key IN ({placeholders}) AND issue_id LIKE ? ORDER BY seq DESC",
                (*repo_keys, "#pr_%"))
            rows = c.fetchall()
        except Exception:
            rows = []
    if not rows:
        return items
    # 按 PR 号聚合（取最新一条作为卡片状态，同时保留全量审查历史）
    _pr_map = {}  # pr_num → latest event
    _pr_history = {}  # pr_num → [event, ...]
    for r in rows:
        iid = r[1] or ""
        # #pr_{pr_num}_{scene_id} → 取 pr_num
        parts = iid.replace("#pr_", "").split("_", 1)
        pr_num = int(parts[0]) if parts and parts[0].isdigit() else 0
        if not pr_num:
            continue
        _ev = {
            "seq": r[0], "issue_id": iid, "event_type": r[2],
            "agent_type": r[3] or "", "title": r[4] or "",
            "actor": r[5] or "", "summary": r[6] or "",
            "status": r[7], "created_at": normalize_event_time(r[8]),
            "result": r[9] or "",
            "scene_id": r[10] if len(r) > 10 else "",
        }
        if pr_num not in _pr_map:
            _pr_map[pr_num] = _ev
        _pr_history.setdefault(pr_num, []).append(_ev)
    # 查场景名缓存
    from src.api.pipeline_config import get_pipeline_config
    _scene_name_cache = {}
    def _get_scene_name(sid):
        if not sid:
            return ""
        if sid not in _scene_name_cache:
            cfg = get_pipeline_config(sid, repo_key=repo_keys[0] if repo_keys else "")
            _scene_name_cache[sid] = (cfg or {}).get("display_name") or (cfg or {}).get("name", sid)
        return _scene_name_cache[sid]

    for pr_num, ev in _pr_map.items():
        _status = ev["status"]
        # 映射到需求中心状态
        if _status == "running":
            _task_status = "processing"
        elif _status == "completed":
            _task_status = "completed"
        elif _status == "failed":
            _task_status = "cancelled"
        else:
            _task_status = "pending"
        items.append({
            "id": -pr_num,  # 负数避免跟真实 task id 冲突
            "issue_number": pr_num,
            "title": ev["title"],
            "task_description": "",
            "conversation_id": "",
            "status": _task_status,
            "completed_reason": "",
            "created_at": ev["created_at"],
            "completed_at": "",
            "is_subtask_mode": 0,
            "parent_issue_number": 0,
            "parent_issue_id": "",
            "scene_id": "",
            "scene_name": "",
            "scene_source": "",
            "stage": "",
            "is_running": _status == "running",
            "agent_statuses": [],
            "repo_key": repo_keys[0] if repo_keys else "",
            "is_pr_event": True,
            "pr_seq": ev["seq"],
            "qa_history": [{**qa, "scene_name": _get_scene_name(qa.get("scene_id", ""))} for qa in _pr_history.get(pr_num, [])],
            "events_count": sum(1 for r in rows if r[1] and r[1].replace("#pr_", "").split("_", 1)[0] == str(pr_num)),
            "latest_event": {"seq": ev["seq"], "status": ev["status"], "title": ev["title"], "created_at": ev["created_at"]},
            "assignee": ev["actor"],
        })
    return items


def _repo_key_of(owner: str, repo: str) -> str:
    from src.repo_key import to_repo_key
    return to_repo_key(f"{owner}/{repo}")


def _repo_keys_of(owner: str, repo: str) -> list[str]:
    """跨 provider 双查：返回 [gitcode:owner/repo, github:owner/repo] 两个前缀。

    与 pipeline.py 的双查逻辑一致——用户可能通过 GitHub Webhook 接入仓库，
    DB 中数据存为 github: 前缀，但前端传入的 owner/repo 无 provider 信息，
    _repo_key_of 默认返回 gitcode: 前缀会漏查 GitHub 仓库数据。
    """
    from src.repo_key import to_repo_key
    _owner_repo = f"{owner}/{repo}"
    repo_key = to_repo_key(_owner_repo)
    keys = [repo_key]
    if repo_key.startswith("gitcode:"):
        keys.append(f"github:{_owner_repo}")
    elif repo_key.startswith("github:"):
        keys.append(f"gitcode:{_owner_repo}")
    return keys


def _issue_stage_map(repo_keys: list[str]) -> dict:
    """仓库内 issue number → issue 阶段摘要（stage/owner/scene/assignee/subtask）。

    Phase C1/C3：`issue` 单表为权威（已收编 + 停写 issue_state），直接读 issue 卡片行；
    `issue_state` 仅作停写过渡期兜底。
    """
    from src.session_manager import db, db_lock
    out = {}
    placeholders = ",".join("?" * len(repo_keys))
    # 1. issue 单表（权威）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT issue_number, stage, owner_agent, assignee, a2a_seq, scene_id, "
                f"parent_issue_id, is_subtask_mode, scene_user_id, allow_scene_override "
                f"FROM issue WHERE repo_key IN ({placeholders})",
                repo_keys)
            for r in c.fetchall():
                n = str(r[0])
                if not n:
                    continue
                out[n] = {
                    "issue_id": f"#{n}", "stage": r[1], "owner_agent": r[2],
                    "assignee": r[3] or "", "a2a_seq": r[4],
                    "scene_id": r[5] or "", "parent_issue_id": r[6] or "",
                    "is_subtask_mode": r[7] or 0, "scene_user_id": r[8] or 0,
                    "allow_scene_override": r[9] or 0,
                }
    except Exception as e:
        log.warning(f"issues: load issue table failed: {e}")
    # 2. issue_state 兜底（无 issue 卡片行但仍有 issue_state 的过渡期数据）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT issue_id, stage, owner_agent, assignee, a2a_seq, scene_id, "
                f"parent_issue_id, is_subtask_mode, scene_user_id, allow_scene_override "
                f"FROM issue_state WHERE repo_key IN ({placeholders})",
                repo_keys)
            for r in c.fetchall():
                n = _num_from_issue_id(r[0])
                if not n or n in out:
                    continue
                out[n] = {
                    "issue_id": r[0], "stage": r[1], "owner_agent": r[2],
                    "assignee": r[3] or "", "a2a_seq": r[4],
                    "scene_id": r[5] or "", "parent_issue_id": r[6] or "",
                    "is_subtask_mode": r[7] or 0, "scene_user_id": r[8] or 0,
                    "allow_scene_override": r[9] or 0,
                }
    except Exception as e:
        log.warning(f"issues: load issue_state failed: {e}")
    return out


def _task_status_map(repo_keys: list[str]) -> dict:
    """仓库内 issue_number → tasks.status（任务中心状态）。"""
    from src.session_manager import db, db_lock
    out = {}
    placeholders = ",".join("?" * len(repo_keys))
    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT issue_number, status FROM issue WHERE repo_key IN ({placeholders})", repo_keys)
            for r in c.fetchall():
                out[str(r[0])] = r[1]
        except Exception as e:
            log.warning(f"issues: load tasks status failed: {e}")
    return out


def _group_events_by_issue(repo_keys: list[str]) -> dict:
    """仓库内 issue_id → events 列表（seq 升序），带时间格式化。

    Issue #252: 排除 entity_type='pull_request' 的事件，PR 事件不混入 issue 展示
    （编号相同时 PR 事件与 issue 事件分离，避免关联混淆）。
    向后兼容：旧事件无 entity_type（空串），视为 issue 事件正常分组。
    """
    from src.session_manager import db, db_lock
    out = {}
    placeholders = ",".join("?" * len(repo_keys))
    with db_lock:
        c = db.cursor()
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
            _has_so = "structured_output" in cols
            _sel = (f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill"
                    + (", structured_output, agent_logs" if _has_so else ""))
            c.execute(
                f"SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at, entity_type "
                f"FROM events WHERE repo_key IN ({placeholders}) AND issue_id!='' ORDER BY seq ASC",
                repo_keys)
            rows = c.fetchall()
        except Exception:
            rows = []
    for r in rows:
        iid = r[1]
        _entity_type = r[9] if len(r) > 9 else ""
        # Issue #252: PR 事件不混入 issue 分组
        if _entity_type == "pull_request":
            continue
        n = _num_from_issue_id(iid)
        if not n:
            continue
        item = {
            "seq": r[0], "event_type": r[2], "agent_type": r[3] or "",
            "title": r[4] or r[2], "actor": r[5] or "",
            "summary": r[6] or r[4] or r[2],
            "status": r[7], "created_at": normalize_event_time(r[8]),
            "runtime": r[9] if len(r) > 9 else "",
            "skill": r[10] if len(r) > 10 else "",
        }
        if len(r) > 11:  # structured_output / agent_logs 存在
            raw_so = r[11]
            try:
                item["output"] = _json.loads(raw_so) if raw_so else None
            except Exception:
                item["output"] = None
            try:
                raw_logs = r[12] if len(r) > 12 else None
                item["logs"] = _json.loads(raw_logs) if raw_logs else []
            except Exception:
                item["logs"] = []
        out.setdefault(n, []).append(item)
    return out


def _issue_title_map(repo_keys: list[str]) -> dict:
    """仓库内 issue_number → 标题（从 events 首条带 title 的事件取）。"""
    g = _group_events_by_issue(repo_keys)
    out = {}
    for n, evs in g.items():
        for ev in evs:
            if ev.get("title"):
                out[n] = ev["title"]
                break
    return out


def _assemble_issue(repo_key: str, number: str, stage_map: dict, status_map: dict,
                    group_map: dict, title_map: dict) -> dict:
    """聚合单个 Issue 展示对象（列表条目）。"""
    stage = stage_map.get(number) or {}
    events = group_map.get(number) or []
    latest = events[-1] if events else None
    return {
        "number": int(number),
        "title": title_map.get(number) or stage.get("issue_id") or f"Issue #{number}",
        "stage": stage.get("stage", "vod"),
        "task_status": status_map.get(number, ""),
        "owner_agent": stage.get("owner_agent", "vod"),
        "scene_id": stage.get("scene_id", ""),
        "assignee": stage.get("assignee", ""),
        "is_subtask_mode": stage.get("is_subtask_mode") or 0,
        "parent_issue_id": stage.get("parent_issue_id", ""),
        "events_count": len(events),
        "latest_event": latest,
    }


def _pipeline_from_archive(repo_keys: list[str], number: str) -> dict | None:
    """读 event.json 组装 pipeline 路线图（跨 provider 双查）。"""
    from src.api.event_archive import load_event_json
    for rk in repo_keys:
        try:
            arch = load_event_json(rk, number)
            if not arch:
                continue
            segments = []
            for seg in arch.get("pipeline", []):
                op = seg.get("output", {}) or {}
                segments.append({
                    "seq": seg.get("seq"),
                    "agent": seg.get("agent", ""),
                    "event_type": seg.get("event_type", ""),
                    "status": seg.get("status", ""),
                    "runtime": seg.get("runtime", ""),
                    "skill": seg.get("skill", ""),
                    "output": {
                        "conclusion": op.get("conclusion", ""),
                        "next_step": op.get("next_step", ""),
                        "actions": op.get("actions", []),
                        "artifacts": op.get("artifacts", []),
                        "issue_summary": op.get("issue_summary", ""),
                    },
                })
            return {
                "issue": arch.get("issue", {}),
                "summary": arch.get("summary", ""),
                "status": arch.get("status", ""),
                "segments": segments,
            }
        except Exception as e:
            log.warning(f"issues: load pipeline archive failed repo={rk} #{number}: {e}")
    return None


def _pipeline_from_events(repo_key: str, number: str, events: list[dict]) -> dict:
    """由 events 表重构 pipeline（weekly-05 主路径），output 用 structured_output。"""
    segments = []
    for ev in events:
        seg = {
            "seq": ev["seq"], "agent": ev.get("agent_type", ""),
            "event_type": ev.get("event_type", ""),
            "status": ev.get("status", ""),
            "runtime": ev.get("runtime", ""), "skill": ev.get("skill", ""),
            "output": {
                "conclusion": ev.get("summary", ""),
                "next_step": "",
                "actions": [], "artifacts": [],
                "issue_summary": ev.get("summary", ""),
            },
            "logs": ev.get("logs", []),
        }
        _out = ev.get("output")
        if isinstance(_out, dict):
            seg["output"] = {
                "conclusion": _out.get("conclusion", ev.get("summary", "")),
                "next_step": _out.get("next_step", ""),
                "actions": _out.get("actions", []),
                "artifacts": _out.get("artifacts", []),
                "issue_summary": _out.get("issue_summary", ev.get("summary", "")),
            }
        segments.append(seg)
    return {"issue": {"number": int(number)} if str(number).isdigit() else {"number": number},
            "summary": (segments[-1]["output"]["issue_summary"] if segments else ""),
            "status": (segments[-1]["status"] or "unknown") if segments else "unknown",
            "segments": segments}


# ===================== 端点 =====================

# ===================== B 类操作（tasks-issues-api-rebound-design.md §三）=====================
# 迁移自 /api/tasks/{task_id}/...：`{issue_id}` 即原 `{task_id}`（= issue.id），定位/逻辑零改动。


@router.post("/{issue_id}/start", operation_id="issues_start", summary="Open issue session", description="Opens the Issue conversation entry: validates ownership and returns the current status without flipping it. User-only (front-end navigation preflight).")
async def start_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """打开 Issue 会话（"去对话"）——仅校验归属并返回当前状态，不翻转状态。

    user_only：纯前端导航预检（确认可访问 + 取当前状态），Agent 执行期（tk_ 会话）无此场景；
    与周边操作类端点（confirm-done/cancel/resume/clarify 等）保持一致，收缩暴露面。
    """
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, status FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        if row[0] != user["id"]:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该需求"})
        return {"status": row[1]}


@router.get("/{issue_id}/status", operation_id="issues_status_get", summary="Query issue status (polling)", description="Returns the Issue status with context for CLI 'grape issues wait' polling.")
def get_issue_status(issue_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """返回 Issue 当前状态（含 repo/issue 上下文），供 CLI `grape issues wait` 轮询至终态。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, status, repo_key, issue_number, title, completed_reason, completed_at "
            "FROM issue WHERE id=?",
            (issue_id,),
        )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    return {
        "id": row[0], "status": row[1], "repo_key": row[2], "issue_number": row[3],
        "title": row[4], "completed_reason": row[5] or "", "completed_at": row[6],
    }


class _IssueStatusBody(BaseModel):
    status: str = ""


@router.put("/{issue_id}/status", operation_id="issues_status", summary="Update issue status", description="Transitions Issue status (active/processing/awaiting_input/pending_review/completed/cancelled flow). User-only: user-facing status is mutable by the owner; session token -> 403.")
def change_issue_status(issue_id: int, body: _IssueStatusBody, user: dict = Depends(require_no_session_token)):
    """Issue 状态迁移（active/processing/awaiting_input/pending_review/completed/cancelled 流转规则）。

    user_only：状态是**需求中心视角（用户可读）**，且 awaiting_input/completed/cancelled 允许用户直接
    设置（高优先级覆盖）；若放行 tk_ 会话 token，Agent 执行期可随意改状态，与 pipeline 驱动的 stage
    脱钩→冲突/不一致，干扰任务执行。故仅用户（JWT / grape_）可改；读（GET /status，轮询 wait 用）保持 dual。
    """
    from src.api.issues_center import _TASK_STATUS_FLOWS
    from src.session_manager import db, db_lock
    ns = (body.status or "").strip()
    if ns not in _TASK_STATUS_FLOWS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"非法状态: {ns}"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT user_id, status FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        if row[0] not in (user["id"], 0):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权操作该需求"})
        cur = row[1]
        if cur != ns and ns not in _TASK_STATUS_FLOWS.get(cur, ()):
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"不允许从 {cur} 迁移到 {ns}"})
        c.execute(
            "UPDATE issue SET status=?, "
            "completed_at=CASE WHEN ? IN ('completed','cancelled') THEN datetime('now') ELSE completed_at END, "
            "completed_reason=CASE WHEN ?='completed' THEN 'manual' WHEN ?='cancelled' THEN 'cancelled' ELSE completed_reason END "
            "WHERE id=?",
            (ns, ns, ns, ns, issue_id)
        )
        db.commit()
    return {"ok": True, "issue_id": issue_id, "status": ns}


class _IssueConfirmBody(BaseModel):
    confirmation: str = "user_confirmed"


def _close_vcs_issues(repo_key: str, issue_number, user: dict) -> dict:
    """confirm-done 后同步关闭 GitCode/GitHub Issue（降级不阻断）。

    Returns:
        {"gitcode": "success|failed|skipped|bot_token_failed", "github": "...",
         "message": str}
        — gitcode 始终尝试关闭（无 issue_number 时 skipped）；github 仅当 repo_key
        以 github: 开头时尝试关闭，否则 skipped。
        bot_token_failed: bot token 可能失效，issue 未关闭（方向3 降级提示）。
    """
    result = {"gitcode": "skipped", "github": "skipped", "message": ""}
    if not issue_number:
        return result
    try:
        issue_number = int(issue_number)
    except (TypeError, ValueError):
        return result

    is_github = repo_key.startswith("github:")

    # --- 确定 GitCode 侧的 repo_key 与 issue（GitHub 仓库时 GitCode 侧 skip）---
    if not is_github:
        result["gitcode"] = _try_close_issue_for_repo(repo_key, issue_number, user, is_github=False)

    # --- GitHub 侧：repo_key 本身是 github:，或需要查找镜像 ---
    if is_github:
        result["github"] = _try_close_issue_for_repo(repo_key, issue_number, user, is_github=True)
    else:
        # GitCode 仓库可能绑定了 GitHub 镜像，尝试查找并关闭镜像 Issue
        _gh_rk = _find_github_mirror_repo_key(repo_key)
        if _gh_rk:
            result["github"] = _try_close_issue_for_repo(_gh_rk, issue_number, user, is_github=True)
        # 无镜像 → 保持 skipped

    # Issue #345: bot_token_failed 时设置用户可见提示
    if "bot_token_failed" in (result["gitcode"], result["github"]):
        from src.vcs.token_error import BOT_TOKEN_INVALID_MSG
        result["message"] = f"Issue 关闭失败：{BOT_TOKEN_INVALID_MSG}"

    return result


def _try_close_issue_for_repo(repo_key: str, issue_number: int, user: dict, is_github: bool) -> str:
    """对单个 repo_key 执行幂等关闭 Issue，返回 success/failed/skipped。"""
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    if not owner or not repo:
        return "skipped"

    # 获取 token — Issue #343: 写操作走 bot token 优先，与建单路径对称
    # Issue #343 修复: bot token 失败时 fallback 到用户 token，用户 token 路径走 with_oauth_token
    token = ""
    _use_user_token_fallback = False
    try:
        if is_github:
            # GitHub: bot token 优先，用户 token fallback
            from src.user.db import get_github_token as _get_gh
            token = _get_gh(repo_key=repo_key) or ""
            if not token and user.get("id"):
                # P1-2 修复: 和 GitCode 路径对称——只设 flag 不获取 token，
                # 让 with_oauth_token 内部用 get_user_github_token_with_refresh 获取（有预刷新）
                _use_user_token_fallback = True
        else:
            # Issue #343: GitCode 写操作走仓库 bot token（跳过 agent session token）
            from src.user.db import get_repo_bot_token as _get_gc
            token = _get_gc(repo_key=repo_key) or ""
            if not token:
                # bot token 未配置，fallback 用户 token（走 with_oauth_token 保护）
                _use_user_token_fallback = True
    except Exception as e:
        log.warning(f"confirm-done close_issue: get token for {repo_key} failed: {e}")

    if not token and not _use_user_token_fallback:
        log.info(f"confirm-done close_issue: no token for {repo_key}, skipping")
        return "skipped"

    # Issue #343 修复: 用户 token fallback 路径走 with_oauth_token 保护（预刷新 + 401 重试）
    if _use_user_token_fallback and not token:
        # bot token 未配置，通过 with_oauth_token 获取用户 token 并执行关闭
        try:
            from src.api.oauth_token import with_oauth_token
            from src.vcs.api_client import VcsApiClient
            _provider_name = "github" if is_github else "gitcode"
            def _do_close(token, _provider=provider, _owner=owner, _repo=repo, _num=issue_number):
                c = VcsApiClient(_provider, token, _owner, _repo)
                # 幂等检查
                state = c.get_issue_state(_num)
                if state == "closed":
                    return "skipped"
                resp = c.close_issue(_num)
                if isinstance(resp, dict) and "error" not in resp:
                    return "success"
                return "failed"
            result = with_oauth_token(user, _provider_name, _do_close)
            if result == "success":
                log.info(f"confirm-done: closed {repo_key}#{issue_number} (user token)")
            return result
        except Exception as e:
            log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} user token failed: {e}")
            return "failed"

    # bot token 路径：直接调用
    from src.vcs.api_client import VcsApiClient
    client = VcsApiClient(provider, token, owner, repo)

    # 幂等检查：已 closed 则跳过
    try:
        state = client.get_issue_state(issue_number)
        if state == "closed":
            log.info(f"confirm-done: {repo_key}#{issue_number} already closed, skipping")
            return "skipped"
    except Exception as e:
        log.warning(f"confirm-done: get_issue_state {repo_key}#{issue_number} failed: {e}")
        # 查询失败不阻断关闭尝试

    # 执行关闭
    try:
        resp = client.close_issue(issue_number)
        if isinstance(resp, dict) and "error" not in resp:
            log.info(f"confirm-done: closed {repo_key}#{issue_number}")
            return "success"
        # Issue #345: 识别 401 返回 Bot Token 失效标记
        from src.vcs.token_error import is_auth_error_result
        if is_auth_error_result(resp):
            log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → Bot Token 可能失效")
            return "bot_token_failed"
        log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → {resp}")
        return "failed"
    except Exception as e:
        # Issue #345: 异常路径也检查 401
        from src.vcs.token_error import is_auth_error_exc
        if is_auth_error_exc(e):
            log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → Bot Token 可能失效 (exc)")
            return "bot_token_failed"
        log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} exception: {e}")
        return "failed"


def _find_github_mirror_repo_key(repo_key: str) -> str:
    """查找 GitCode 仓库绑定的 GitHub 镜像 repo_key。

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
        log.warning(f"confirm-done: find github mirror for {repo_key} failed: {e}")
        return ""


@router.post("/{issue_id}/confirm-done", operation_id="issues_confirm_done", summary="Confirm issue done", description="Developer confirms the requirement is done: status -> completed, stage -> done (idempotent in terminal state).")
async def confirm_issue_done(issue_id: int, body: _IssueConfirmBody, user: dict = Depends(require_no_session_token)):
    """开发者确认需求完成：status→completed、stage→done（任意阶段可确认，终态幂等）。"""
    from src.api.session_store import set_issue_stage
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
                  (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        _repo_key, _issue_num, _status = row[0], row[1], row[2]
        if _status == 'completed' or _status == 'done':
            # 幂等：已关闭的 Issue 再次 confirm-done 不重复调用关闭 API
            return {"ok": True, "already_done": True,
                    "issue_closed": {"gitcode": "skipped", "github": "skipped"}}
        if _status == 'cancelled':
            raise HTTPException(status_code=400, detail={"code": 400, "message": "已取消需求不可确认完成"})
        c.execute(
            "UPDATE issue SET status='completed', completed_reason=?, completed_at=datetime('now') WHERE id=?",
            (body.confirmation, issue_id))
        if _issue_num:
            set_issue_stage(_repo_key, f"#{_issue_num}", "done", "user")
        db.commit()
    log.info(f"Issue #{issue_id} confirmed done by {body.confirmation}")
    _issue_closed = _close_vcs_issues(_repo_key, _issue_num, user)
    return {"ok": True, "issue_id": issue_id, "status": "completed", "stage": "done",
            "issue_closed": _issue_closed}


@router.post("/{issue_id}/cancel", operation_id="issues_cancel", summary="Cancel issue (stop pipeline)", description="Developer cancels the requirement from any stage: terminates agents and stops the pipeline.")
async def cancel_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """开发者取消需求：任何阶段可取消，Agent 终止 + 流水线停止。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
                  (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        status, repo_key, issue_number = row[0], row[1], row[2]
        if status == 'cancelled':
            return {"ok": True, "already_cancelled": True}
        if status == 'completed':
            raise HTTPException(status_code=400, detail={"code": 400, "message": "已完成需求不可取消"})
        _issue_id = f"#{issue_number}" if issue_number else ""
        _cancelled_agents = 0
        if _issue_id:
            c.execute(
                "SELECT id, event_seq FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running')",
                (repo_key, _issue_id))
            _agent_rows = c.fetchall()
            for _at_id, _at_seq in _agent_rows:
                try:
                    from src.runtime.service import cancel_task as _cancel_agent
                    _cancel_agent(_at_id)
                except Exception:
                    pass
                try:
                    if _at_seq:
                        from src.api.event_pipeline import cancel_direct_task
                        cancel_direct_task(_at_seq)
                except Exception:
                    pass
                try:
                    if _at_seq:
                        from src.api.event_store import update_event_status
                        update_event_status(_at_seq, "cancelled")
                except Exception:
                    pass
                _cancelled_agents += 1
            c.execute(
                "UPDATE agent_tasks SET status='cancelled' WHERE repo_key=? AND issue_id=? AND status IN ('queued','dispatched','running')",
                (repo_key, _issue_id))
        c.execute(
            "UPDATE issue SET status='cancelled', completed_reason='user_cancelled', completed_at=datetime('now') WHERE id=?",
            (issue_id,))
        db.commit()
    log.info(f"Issue #{issue_id} cancelled by user (agents={_cancelled_agents})")
    return {"ok": True, "issue_id": issue_id, "status": "cancelled", "agents_cancelled": _cancelled_agents}


@router.post("/{issue_id}/resume", operation_id="issues_resume", summary="Resume cancelled issue", description="Resumes a cancelled requirement from its pre-cancel stage.")
async def resume_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """恢复已取消的需求：回到取消前 stage 继续流转。"""
    from src.api.session_store import get_issue_stage
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
                  (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        status, repo_key, issue_number = row[0], row[1], row[2]
        if status != 'cancelled':
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仅已取消需求可恢复"})
        c.execute(
            "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL WHERE id=?",
            (issue_id,))
        db.commit()
    _issue_id = f"#{issue_number}" if issue_number else ""
    if _issue_id:
        from src.api.event_store import get_events
        _events = get_events(repo_key, issue_id=_issue_id, limit=50)
        _last_completed_agent = ""
        for _e in reversed(_events):
            if _e.get("status") == "completed" and _e.get("agent_type"):
                _last_completed_agent = _e["agent_type"]
                break
        from src.api.pipeline_config import get_pipeline_config
        from src.api.session_store import get_issue_scene
        _scene_tuple = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene_tuple[0] if _scene_tuple and _scene_tuple[0] else "") or "default"
        _pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
        _nodes = (_pipe_cfg or {}).get("nodes", [])
        _resume_stage = ""
        if _last_completed_agent:
            for _n in _nodes:
                if _n.get("id") == _last_completed_agent:
                    _resume_stage = _n.get("next", "")
                    break
        if not _resume_stage:
            _resume_stage = _nodes[0].get("id", "vod") if _nodes else "vod"
        if _resume_stage and _resume_stage not in ("done", ""):
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, _issue_id, _resume_stage, "resume")
            try:
                import urllib.request, json as _json
                from src.prompt_builder import _resolve_server_url
                _a2a_url = _resolve_server_url() + "/api/a2a"
                _a2a_payload = _json.dumps({
                    "agent": _resume_stage,
                    "payload": {
                        "repository": repo_key.replace("gitcode:", ""),
                        "issue_number": issue_number,
                        "context": f"resume from cancelled, last_completed={_last_completed_agent}, next={_resume_stage}",
                    }
                }).encode()
                _req = urllib.request.Request(
                    _a2a_url,
                    data=_a2a_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(_req, timeout=5)
                log.info(f"Issue #{issue_id} resumed → re-dispatched to stage={_resume_stage}")
            except Exception as _e:
                log.warning(f"Issue #{issue_id} resume re-dispatch failed: {_e}")
    log.info(f"Issue #{issue_id} resumed by user")
    return {"ok": True, "issue_id": issue_id, "status": "processing"}


@router.get("/{issue_id}/pipeline", operation_id="issues_pipeline_detail", summary="Issue pipeline roadmap (by issue.id)", description="Returns the issue pipeline info and event history for the detail page.")
async def get_issue_pipeline_by_id(issue_id: int, user: dict = Depends(require_no_session_token)):
    """获取需求关联的流水线信息和事件历史（详情页展示）。"""
    from src.api.event_store import get_events
    from src.api.pipeline_config import get_pipeline_config
    from src.api.session_store import get_issue_stage, get_issue_scene
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        # 仓库全部视图下，用户可以看到其他人的 Issue
        # 按 id 查出 Issue，再校验用户是否有该仓库的访问权限
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?",
                  (issue_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        repo_key, issue_number = row[0], row[1]
    # 校验仓库访问权限（与"仓库全部"列表接口一致）
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    issue_key = f"#{issue_number}" if issue_number else ""
    _scene_tuple = get_issue_scene(repo_key, issue_key)
    _scene_id = (_scene_tuple[0] if _scene_tuple and _scene_tuple[0] else "") or "default"
    _stage_info = get_issue_stage(repo_key, issue_key) if issue_key else None
    _current_stage = (_stage_info or {}).get("stage", "") if _stage_info else ""
    pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key)
    nodes = (pipe_cfg or {}).get("nodes", [])
    events = []
    if issue_key:
        events = get_events(repo_key, issue_id=issue_key, limit=30)
    # 统一计算节点状态（与菜单流水线页一致；compute_node_statuses 兼算 latest_event）
    from src.api.pipeline_status import compute_node_statuses
    nodes, _ = compute_node_statuses(nodes, events, _current_stage)
    # Issue #329: extract carried_context for cross-pipeline continuation source annotation
    from src.api.session_store import get_carried_context
    _carried_context = get_carried_context(repo_key, issue_number)
    _source_issue_number = _carried_context.get("source_issue_number", "") if _carried_context else ""

    # Check for cross_scene_handoff events (Issue #329)
    _has_cross_scene = False
    if issue_key:
        try:
            _has_cross_scene = any(e.get("event_type") == "cross_scene_handoff" for e in events)
        except Exception:
            pass

    return {
        "scene_id": _scene_id,
        "scene_name": (pipe_cfg or {}).get("display_name") or (pipe_cfg or {}).get("name", _scene_id),
        "current_stage": _current_stage,
        "nodes": nodes,
        "events": events,
        # Issue #329: cross-pipeline continuation fields
        "carried_context": _carried_context,
        "source_issue_number": _source_issue_number,
        "has_cross_scene_handoff": _has_cross_scene,
    }


# ===== Issue #337: 列表模式详情「移交到其他流水线」（后端代理，复用 #329 handoff 链路）=====

class IssueHandoffBody(BaseModel):
    target_scene_id: str = ""


def _load_issue_repo_scene(issue_id: int) -> tuple[str, int, str]:
    """按 issue.id 取 (repo_key, issue_number, current_scene_id)；不存在 → 404。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number, scene_id FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    _scene_id = (row[2] or "").strip() or "default"
    return row[0], row[1] or 0, _scene_id


@router.get("/{issue_id}/handoff/targets", operation_id="issues_handoff_targets",
            summary="List handoff target pipelines for an issue",
            description="Lists available cross-pipeline handoff targets for the issue-center detail view: repo-scoped scenes (permission-filtered), excluding the issue's current pipeline, vod node required. Empty list is returned as-is (frontend shows explicit empty hint).")
async def targets_issue(issue_id: int, user: dict = Depends(require_no_session_token)):
    """Issue #337: 需求详情「移交到其他流水线」目标流水线列表。"""
    from src.api.deps import check_repo_access
    from src.api.issues_center import get_handoff_targets
    repo_key, issue_number, current_scene_id = _load_issue_repo_scene(issue_id)
    check_repo_access(user, repo_key)
    out = get_handoff_targets(repo_key, issue_number, current_scene_id)
    return {"issue_id": issue_id, "issue_number": issue_number, "repo_key": repo_key,
            "current_scene_id": out["current_scene_id"], "targets": out["targets"]}


@router.post("/{issue_id}/handoff", operation_id="issues_handoff",
             summary="Hand off issue to another pipeline",
             description="Hands the issue off to a target pipeline (Issue #337 list-view entry). Backend proxy: validates repo access + target membership (excludes current pipeline, vod required), 30s idempotency guard, then reuses the standard A2A cross-pipeline handoff path (a2a_endpoint -> _cross_pipeline_handoff). Result (including 'skipped' interceptions like duplicate_handoff) is passed through to the frontend.")
async def handoff_issue(issue_id: int, body: IssueHandoffBody,
                        user: dict = Depends(require_no_session_token)):
    """Issue #337: 移交提交——服务内直调 a2a_endpoint（与 a2a-redo 同模式），复用 #329 链路。"""
    import time as _time
    import json as _rjson
    from src.api.deps import check_repo_access
    from src.api.issues_center import get_handoff_targets

    # 1) 定位需求 + 归属校验
    repo_key, issue_number, current_scene_id = _load_issue_repo_scene(issue_id)
    check_repo_access(user, repo_key)

    # 2) 目标校验：非空 / 非当前场景 / 在可移交列表内（含 vod 过滤，防绕过权限点名任意场景）
    target_scene_id = (body.target_scene_id or "").strip()
    if not target_scene_id:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请选择目标流水线"})
    if target_scene_id == current_scene_id:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "目标流水线不能是当前流水线"})
    _targets = get_handoff_targets(repo_key, issue_number, current_scene_id)
    _target_ids = {t["id"] for t in _targets["targets"]}
    if target_scene_id not in _target_ids:
        raise HTTPException(status_code=404, detail={
            "code": 404, "message": "目标流水线不存在或不可移交（需含 vod 节点且不归属当前仓库）"})

    # 3) 幂等防连点：30s 内同一需求重复提交拒绝（复用 a2a-redo 的 meta 键模式）
    from src.user.db import get_meta_value, set_meta_value
    _mk = f"issue-handoff:{issue_id}"
    _last = get_meta_value(_mk) or ""
    try:
        _recent = _last and (_time.time() - float(_last)) < 30
    except ValueError:
        _recent = False
    if _recent:
        raise HTTPException(status_code=409, detail={"code": 409, "message": "该需求刚发起过移交，请 30s 后再试"})

    # 4) 组装 payload：carried_context 读取历史接续信息并补来源场景
    from src.api.session_store import get_carried_context
    from src.api.pipeline_config import get_pipeline_config
    _carried = get_carried_context(repo_key, issue_number) or {}
    _carried.setdefault("source_issue_number", issue_number)
    _carried.setdefault("source_scene_id", current_scene_id)
    _src_cfg = get_pipeline_config(current_scene_id, repo_key=repo_key) or {}
    _carried.setdefault("source_scene_name", _src_cfg.get("display_name") or _src_cfg.get("name") or current_scene_id)

    _repository = repo_key.split(":", 1)[1] if ":" in repo_key else repo_key
    _account = (user.get("username") or "") or _repository.split("/", 1)[0]
    _context = (f"用户在需求中心发起跨流水线移交（Issue #{issue_number}）："
                f"目标场景 {target_scene_id}，请快速复核后流转至 issue 确认 Spec。")

    # 5) 服务内直调标准 /api/a2a 链路（jwt 分支内部再校验 managed_repos，双保险）
    from src.api.routes.webhook import a2a_endpoint, A2AHandoffBody
    _handoff_user = dict(user or {})
    _handoff_user["auth_type"] = "jwt"
    _a2a_body = A2AHandoffBody(agent="vod", payload={
        "repository": _repository,
        "account": _account,
        "issue_number": issue_number,
        "pr_number": "",
        "context": _context,
        "target_scene_id": target_scene_id,
        "target_node": "vod",
        "carried_context": _carried,
        "target_assignee": "",
        "engine_config": None,
    })
    _res = await a2a_endpoint(None, _a2a_body, _handoff_user)  # type: ignore[arg-type]  # jwt 分支不读 request
    # JSONResponse（拦截类 skipped）原样透传
    if not isinstance(_res, dict):
        _raw = getattr(_res, "body", b"") or b""
        if isinstance(_raw, memoryview):
            _raw = _raw.tobytes()
        try:
            _res = _rjson.loads(bytes(_raw).decode("utf-8"))
        except Exception:
            _res = {"status": "error", "message": "A2A 返回解析失败"}

    # 6) 记录幂等时间戳（真实入队/拦截都记，防连点）
    set_meta_value(_mk, str(_time.time()))
    log.info(f"[issue-handoff] issue_id={issue_id} #{issue_number} → scene={target_scene_id} "
             f"by={user.get('username', '')}: {_res}")
    return _res


@router.get("/{issue_id}/clarifications", operation_id="issues_clarifications", summary="Issue clarification history", description="Fetches the agent's clarification messages for the Issue.")
async def get_issue_clarifications(issue_id: int, user: dict = Depends(require_no_session_token)):
    """拉取 agent 在该 Issue 上的澄清消息。"""
    from src.api.issues_center import _get_task_issue_info, _find_agent_task_ids
    from src.session_manager import db, db_lock
    info = _get_task_issue_info(issue_id)
    if not info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number, conv_id = info
    agent_task_ids = _find_agent_task_ids(repo_key, issue_number)
    messages = []
    if agent_task_ids:
        import json as _json
        with db_lock:
            c = db.cursor()
            placeholders = ",".join("?" * len(agent_task_ids))
            c.execute(
                f"SELECT task_id, seq, type, tool, content, input_json, created_at "
                f"FROM task_messages WHERE task_id IN ({placeholders}) "
                f"AND type IN ('text','reasoning','error','tool_use') "
                f"ORDER BY created_at DESC LIMIT 50",
                agent_task_ids)
            rows = c.fetchall()
        for r in reversed(rows):
            msg = {"task_id": r[0], "seq": r[1], "type": r[2], "tool": r[3] or "",
                   "content": r[4] or "", "created_at": normalize_event_time(r[6])}
            if r[5]:
                try:
                    msg["input"] = _json.loads(r[5])
                except Exception:
                    msg["input"] = r[5]
            messages.append(msg)
    conv_messages = []
    if conv_id:
        from src.session_manager import get_messages
        conv_messages = get_messages(conv_id, limit=30)
    _issue_host = "github.com" if repo_key.startswith("github:") else "gitcode.com"
    return {"messages": messages, "conversation_id": conv_id,
            "issue_url": f"https://{_issue_host}/{repo_key}/issues/{issue_number}"}


@router.post("/{issue_id}/awaiting-input", operation_id="issues_awaiting-input",
             summary="Agent declares issue awaiting-input (待澄清)",
             description="vod/issue agent explicitly declares this issue needs user input: atomically moves active/processing -> awaiting-input, writes the clarification questions to the task conversation and sends a static Feishu card. Session-only (tk_), repo scope must match the issue, agent_type must be vod/issue. Colocated with issues clarify (user reply) / issues clarifications (history).")
async def issue_awaiting_input(issue_id: int, body: TaskAwaitingInputBody,
                               user: dict = Depends(get_current_user_or_api_key)):
    """vod/issue agent 显式声明「待澄清」（澄清三件套之 Agent 发起）。

    - 从 `/api/tasks/awaiting-input` 迁移而来（issue-explicit-awaiting-input-api-design.md）：awaiting-input
      **只改 issue 状态、与任务生命周期正交（C 单信号）**，故归属 /api/issues/{issue_id}。
    - 仅会话通道（tk_）：URL {issue_id} 定位需求 + 会话 `(repo_key, agent_type)` 作用域校验（与 notify 同源信任模型）。
    - 不终态化、不钉住 task；任务随引擎流结束由 finalize_by_stream_end 照常收尾。
    - **不做**：不触发 ChatAgent / 不执行 opencode / 不注入用户个人 token（§十）。
    """
    from src.api.issues_center import (_ensure_task_conversation, _get_feishu_open_id,
                                       _resolve_user_chat_bot, _run_awaiting_notify)
    from src.session_manager import db as _db, db_lock as _dl, get_chat_config

    questions = (body.questions or "").strip()
    if not questions:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "questions 必填（本次需澄清的问题）"})
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅会话 token（tk_）可调用（Agent 执行期声明待澄清）"})
    session_repo = user.get("repo_key") or ""
    session_agent = user.get("agent_type") or ""
    if not session_repo:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 缺少仓库作用域"})
    if session_agent not in ("vod", "issue"):
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"agent_type={session_agent} 不在本接口范围（仅 vod/issue 可声明待澄清）"})

    with _dl:
        c = _db.cursor()
        c.execute("SELECT id, repo_key, issue_number, assignee, user_id, title, status FROM issue WHERE id=?",
                  (issue_id,))
        irow = c.fetchone()
    if not irow:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在",
                                                       "hint": "awaiting-input 需传需求卡片 id（GRAPE_ISSUE_ID，issue 表主键），非 Issue number；可先 grape issues detail --number $ISSUE_NUMBER --json-out 取 id"})
    issue_card_id, repo_key, issue_number = irow[0], irow[1] or "", irow[2] or 0
    assignee, issue_user_id, title, cur_status = irow[3] or "", irow[4] or 0, irow[5] or "", irow[6] or ""
    from src.repo_key import to_repo_key
    if to_repo_key(repo_key) != to_repo_key(session_repo):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仓库作用域不匹配"})
    if cur_status in ("completed", "cancelled"):
        return {"ok": True, "issue_id": issue_card_id, "status": cur_status, "changed": False}

    with _dl:
        c = _db.cursor()
        c.execute(
            "UPDATE issue SET status='awaiting_input' WHERE id=? AND status IN ('active','processing')",
            (issue_card_id,))
        changed = c.rowcount > 0
        _db.commit()
    user_id = issue_user_id

    conv_id = ""
    if user_id > 0 and changed:
        try:
            conv_id = _ensure_task_conversation(user_id, repo_key, issue_number, title, "",
                                                write_context_on_reuse=False)
            if conv_id:
                with _dl:
                    c = _db.cursor()
                    c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, issue_card_id))
                    _db.commit()
        except Exception as e:
            log.warning(f"Issue awaiting-input: conversation ensure failed: {e}")

    log.info(f"Issue awaiting-input: {repo_key}/#{issue_number} -> awaiting_input issue={issue_card_id} "
             f"changed={changed} (agent={session_agent}, questions={questions[:60]})")
    if changed:
        from src.api.issues_center import _get_feishu_open_id_by_user_id
        open_id = _get_feishu_open_id_by_user_id(user_id) or _get_feishu_open_id(assignee)
        from src.api import main as api_main
        bm = api_main.bot_manager
        ctx = _resolve_user_chat_bot(bm, user_id) if bm else None
        api = bm.api if bm else None
        cfg = get_chat_config(user_id) if user_id else {"backend": "opencode"}
        asyncio.create_task(_run_awaiting_notify(ctx, api, user_id, conv_id, open_id,
                                                 questions, repo_key, issue_number, cfg))

    return {"ok": True, "issue_id": issue_card_id, "status": "awaiting_input", "changed": changed}


# ===== 待审核（pending_review）—— Agent 显式阶段审核点，语义见 issue-pending-review-explicit-design.md =====

_DEV_OPS_AGENTS = ("vod", "issue", "coding", "qa", "sre")


def _session_devops_issue(issue_id: int, user: dict) -> dict:
    """待审核系列端点通用鉴权：仅 tk_ 会话；agent_type∈devops；issue.repo_key==token.repo_key；返回 issue 行。"""
    from src.session_manager import db as _db, db_lock as _dl
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仅会话 token（tk_）可调用（Agent 执行期）"})
    _agent = user.get("agent_type") or ""
    if _agent not in _DEV_OPS_AGENTS:
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"agent_type={_agent} 不在本接口范围（devops agent）"})
    _repo = user.get("repo_key") or ""
    if not _repo:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 缺少仓库作用域"})
    with _dl:
        c = _db.cursor()
        c.execute("SELECT id, repo_key, issue_number, assignee, user_id, title, status FROM issue WHERE id=?", (issue_id,))
        _r = c.fetchone()
    if not _r:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    from src.repo_key import to_repo_key
    if _r[1] and to_repo_key(_r[1]) != to_repo_key(_repo):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "仓库作用域不匹配"})
    return {"id": _r[0], "repo_key": _r[1], "issue_number": _r[2], "assignee": _r[3] or "",
            "user_id": _r[4] or 0, "title": _r[5] or "", "status": _r[6] or ""}


@router.post("/{issue_id}/pending-review", operation_id="issues_pending-review",
             summary="Agent declares issue pending_review (阶段审核点)",
             description="任意 devops agent 显式将 issue 置为待审核（阶段性审核点）：active/processing/awaiting_input -> pending_review，不改 stage，审核点写入任务会话。Session-only (tk_)，repo 匹配，agent_type 为 devops agent。与 awaiting-input 对称（等用户双态）。")
async def issue_pending_review(issue_id: int, body: TaskPendingReviewBody,
                               user: dict = Depends(get_current_user_or_api_key)):
    """Agent 显式提交阶段审核点 → status=pending_review（不改 stage）。"""
    from src.api.issues_center import _ensure_task_conversation
    from src.session_manager import db as _db, db_lock as _dl, add_message as _add

    review_points = (body.review_points or "").strip()
    if not review_points:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "review_points 必填（本次需审核的内容）"})
    it = _session_devops_issue(issue_id, user)
    if it["status"] in ("completed", "cancelled"):
        return {"ok": True, "issue_id": it["id"], "status": it["status"], "changed": False}
    _changed = _set_issue_pending_review(it["repo_key"], it["issue_number"])
    conv_id = ""
    if it["user_id"] > 0 and _changed:
        try:
            conv_id = _ensure_task_conversation(it["user_id"], it["repo_key"], it["issue_number"], it["title"], "",
                                                write_context_on_reuse=False)
            if conv_id:
                from src.repo_key import from_repo_key
                _refs = (body.references or "").strip()
                _msg = (f"🔎 需要你审核确认 · Issue #{it['issue_number']}\n\n"
                        f"仓库: {from_repo_key(it['repo_key'])}\n\n"
                        f"{review_points}" + (f"\n\n参考: {_refs}" if _refs else ""))
                _add(conv_id, "agent", _msg)
                with _dl:
                    c = _db.cursor()
                    c.execute("UPDATE issue SET conversation_id=? WHERE id=?", (conv_id, it["id"]))
                    _db.commit()
        except Exception as e:
            log.warning(f"Issue pending-review: conversation write failed: {e}")
    # 飞书静态卡片触达（同 awaiting）：等用户双态的“待审核”必须主动通知用户
    if _changed:
        try:
            from src.api.issues_center import (_get_feishu_open_id, _get_feishu_open_id_by_user_id,
                                               _resolve_user_chat_bot, _run_pending_review_notify)
            from src.session_manager import get_chat_config
            _open_id = _get_feishu_open_id_by_user_id(it["user_id"]) or _get_feishu_open_id(it["assignee"])
            from src.api import main as api_main
            bm = api_main.bot_manager
            _ctx = _resolve_user_chat_bot(bm, it["user_id"]) if bm else None
            _api = bm.api if bm else None
            _cfg = get_chat_config(it["user_id"]) if it["user_id"] else {"backend": "opencode"}
            asyncio.create_task(_run_pending_review_notify(
                _ctx, _api, it["user_id"], conv_id, _open_id, review_points,
                body.references or "", it["repo_key"], it["issue_number"], _cfg))
        except Exception as e:
            log.warning(f"Issue pending-review: notify schedule failed: {e}")
    return {"ok": True, "issue_id": it["id"], "status": "pending_review", "changed": _changed}


class _ReviewPassBody(BaseModel):
    target: str = "processing"   # processing | done
    evidence: str = ""


class _ReviewRejectBody(BaseModel):
    target: str = "processing"   # processing | awaiting_input
    reason: str = ""


@router.post("/{issue_id}/review-pass", operation_id="issues_review-pass",
             summary="Agent decides review passed",
             description="Agent 判定用户已通过审核：pending_review → processing（继续）；target=done → completed+stage=done（合入/收尾）。Session-only (tk_)，repo 匹配，devops agent。")
async def issue_review_pass(issue_id: int, body: _ReviewPassBody,
                            user: dict = Depends(get_current_user_or_api_key)):
    it = _session_devops_issue(issue_id, user)
    _out = _review_pass(it["repo_key"], it["issue_number"],
                        target=body.target or "processing", evidence=body.evidence or "")
    return {"ok": True, "issue_id": it["id"], "status": "completed" if _out == "passed_done" else ("processing" if _out == "passed_processing" else "pending_review"),
            "result": _out}


@router.post("/{issue_id}/review-reject", operation_id="issues_review-reject",
             summary="Agent decides review rejected",
             description="Agent 判定用户未通过审核：pending_review → processing（打回）或 awaiting_input（转澄清）。Session-only (tk_)，repo 匹配，devops agent。")
async def issue_review_reject(issue_id: int, body: _ReviewRejectBody,
                              user: dict = Depends(get_current_user_or_api_key)):
    it = _session_devops_issue(issue_id, user)
    _out = _review_reject(it["repo_key"], it["issue_number"],
                          target=body.target or "processing", reason=body.reason or "")
    _st = "awaiting_input" if _out == "rejected_awaiting_input" else ("processing" if _out == "rejected_processing" else "pending_review")
    return {"ok": True, "issue_id": it["id"], "status": _st, "result": _out}


class _IssueClarifyBody(BaseModel):
    message: str = ""


@router.post("/{issue_id}/clarify", operation_id="issues_clarify", summary="Reply issue clarification", description="Developer replies to an agent clarification: posts a GitCode issue comment and writes to the task conversation.")
async def clarify_issue(issue_id: int, body: _IssueClarifyBody, user: dict = Depends(require_no_session_token)):
    """开发者在需求中心回复 agent 澄清：同步发 GitCode Issue 评论 + 写入任务会话。"""
    from src.api.issues_center import _get_task_issue_info
    from src.session_manager import db, db_lock
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "消息不能为空"})
    info = _get_task_issue_info(issue_id)
    if not info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number, conv_id = info
    _is_github = repo_key.startswith("github:")
    if _is_github:
        # Issue #343: GitHub 写操作走仓库 bot token
        from src.user.db import get_github_token
        from src.repo_key import to_repo_key
        api_token = get_github_token(repo_key=to_repo_key(repo_key, provider="github"))
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 GitHub Bot Token 未配置"})
    else:
        # Issue #343: GitCode 写操作走仓库 bot token（跳过 agent session token）
        from src.user.db import get_repo_bot_token
        from src.repo_key import to_repo_key
        api_token = get_repo_bot_token(repo_key=to_repo_key(repo_key))
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 Bot Token 未配置"})
    from src.repo_key import split_owner_repo
    owner, rname = split_owner_repo(repo_key)
    comment_body = f"💬 **开发者澄清**\n\n{msg}"
    if _is_github:
        from src.user.repo_manager import _github_req
        try:
            _github_req("POST", f"https://api.github.com/repos/{owner}/{rname}/issues/{issue_number}/comments",
                        api_token, {"body": comment_body})
            log.info(f"Issue clarify: posted comment to {repo_key}/issues/{issue_number} by {user['username']}")
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"GitHub 评论失败: {str(e)[:200]}"})
    else:
        def _post_comment(token: str, _owner=owner, _rname=rname, _issue_num=issue_number, _body=comment_body):
            import urllib.parse as _up
            import urllib.request as _ur
            import urllib.error as _uerr
            url = f"https://gitcode.com/api/v5/repos/{_owner}/{_rname}/issues/{_issue_num}/comments?access_token={token}"
            req = _ur.Request(url, data=_up.urlencode({"body": _body}).encode(),
                              headers={"Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "application/json"}, method="POST")
            try:
                with _ur.urlopen(req, timeout=30) as resp:
                    resp.read()
            except _uerr.HTTPError as e:
                raise RuntimeError(f"GitCode comment failed ({e.code}): {e.read().decode(errors='replace')[:300]}")
            except _uerr.URLError as e:
                raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")
        try:
            # Issue #343: bot token 直接调用，不需要 refresh 包装
            _post_comment(api_token)
            log.info(f"Issue clarify: posted comment to {repo_key}/issues/{issue_number} by {user['username']}")
        except HTTPException:
            raise
        except Exception as e:
            # Issue #345: 识别 401 返回 Bot Token 失效提示
            from src.vcs.token_error import is_auth_error_exc, BOT_TOKEN_INVALID_MSG
            if is_auth_error_exc(e):
                raise HTTPException(status_code=502, detail={"code": 502,
                                                             "message": f"评论失败：{BOT_TOKEN_INVALID_MSG}"})
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"GitCode 评论失败: {str(e)[:200]}"})
    if conv_id:
        try:
            from src.session_manager import add_message
            add_message(conv_id, "user", msg)
        except Exception as e:
            log.warning(f"Issue clarify: write conversation message failed: {e}")
    try:
        from src.api.issues_center import _resume_issue_waiting
        if _resume_issue_waiting(repo_key, issue_number):
            log.info(f"Issue clarify: awaiting_input → processing for {repo_key}/#{issue_number}")
    except Exception as e:
        log.warning(f"Issue clarify: restore processing failed: {e}")
    return {"ok": True, "message": "澄清已发送到 Issue 并同步到任务会话"}


@router.post("/{issue_id}/attachments", operation_id="issues_upload_attachment", summary="Upload issue attachment", description="Uploads an attachment into the issue attachment directory.")
async def upload_issue_attachment(issue_id: int, file: UploadFile = File(...),
                                  user: dict = Depends(require_no_session_token)):
    """上传附件到需求关联的附件目录。"""
    from src.api.issues_center import (_attach_save, _ATTACH_ALL_EXTS, _ATTACH_MAX_SIZE)
    from src.repo_key import split_owner_repo
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number = row[0], row[1]
    owner, rname = split_owner_repo(repo_key)
    original_name = file.filename or "unnamed"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in _ATTACH_ALL_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的文件类型: {ext}"})
    content = await file.read()
    if len(content) > _ATTACH_MAX_SIZE:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "文件大小超过 50MB 限制"})
    import uuid
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dir_name = str(issue_number) if issue_number else "pending"
    url = _attach_save(owner, rname, dir_name, safe_name, content)
    log.info(f"Issue attachment uploaded: issue={issue_id} file={original_name} -> {safe_name} ({len(content)} bytes)")
    return {"url": url, "filename": original_name, "size": len(content)}


@router.get("/{issue_id}/attachments", operation_id="issues_list_attachments", summary="List issue attachments", description="Lists attachments of an issue.")
def list_issue_attachments(issue_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """列出需求关联的附件文件列表。"""
    from src.api.issues_center import _attach_list
    from src.repo_key import split_owner_repo
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number = row[0], row[1]
    owner, rname = split_owner_repo(repo_key)
    attachments = []
    for dir_name in (["pending", str(issue_number)] if issue_number else ["pending"]):
        attachments.extend(_attach_list(owner, rname, dir_name))
    return {"attachments": attachments}


@router.delete("/{issue_id}/attachments/{filename}", operation_id="issues_delete_attachment", summary="Delete issue attachment", description="Deletes an attachment file by UUID filename.")
def delete_issue_attachment(issue_id: int, filename: str, user: dict = Depends(require_no_session_token)):
    """删除指定附件文件。filename 为 UUID 文件名。"""
    from src.api.issues_center import _attach_delete
    from src.repo_key import split_owner_repo
    from src.session_manager import db, db_lock
    import re
    if not re.match(r'^[0-9a-f]{32}\.\w+$', filename):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法文件名"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (issue_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
    repo_key, issue_number = row[0], row[1]
    owner, rname = split_owner_repo(repo_key)
    dir_name = str(issue_number) if issue_number else "pending"
    if _attach_delete(owner, rname, dir_name, filename):
        log.info(f"Issue attachment deleted: issue={issue_id} file={filename}")
        return {"ok": True, "message": "已删除"}
    raise HTTPException(status_code=404, detail={"code": 404, "message": "附件不存在"})

# ===== 创建前临时附件（不进 OpenAPI 契约，前端内部调用）=====

@router.post("/attachments/pre-upload", include_in_schema=False)
async def pre_upload_attachment(file: UploadFile = File(...),
                                repo_key: str = "",
                                user: dict = Depends(require_no_session_token)):
    """创建 Issue 前的临时附件上传。文件存到 pending 目录，创建时按 safe_name 列表精确关联。"""
    from src.api.routes.tasks import _attach_save, _ATTACH_ALL_EXTS, _ATTACH_MAX_SIZE
    from src.repo_key import to_repo_key, split_owner_repo
    rk = to_repo_key(repo_key)
    if not rk:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "repo_key 必填"})
    owner, rname = split_owner_repo(rk)
    original_name = file.filename or "unnamed"
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in _ATTACH_ALL_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的文件类型: {ext}"})
    content = await file.read()
    if len(content) > _ATTACH_MAX_SIZE:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "文件大小超过 50MB 限制"})
    import uuid
    safe_name = f"{uuid.uuid4().hex}{ext}"
    url = _attach_save(owner, rname, "pending", safe_name, content)
    log.info(f"Pre-upload attachment: file={original_name} -> {safe_name} ({len(content)} bytes)")
    return {"url": url, "filename": original_name, "safe_name": safe_name, "size": len(content)}


@router.delete("/attachments/pre-upload/{filename}", include_in_schema=False)
def pre_delete_attachment(filename: str, repo_key: str = "",
                          user: dict = Depends(require_no_session_token)):
    """删除 pending 里的临时附件。"""
    from src.api.routes.tasks import _attach_delete
    from src.repo_key import to_repo_key, split_owner_repo
    import re
    if not re.match(r'^[0-9a-f]{32}\.\w+$', filename):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法文件名"})
    rk = to_repo_key(repo_key)
    owner, rname = split_owner_repo(rk)
    if _attach_delete(owner, rname, "pending", filename):
        return {"ok": True, "message": "已删除"}
    raise HTTPException(status_code=404, detail={"code": 404, "message": "附件不存在"})


@router.get("/mine", operation_id="issues_mine", summary="My issues (personal dimension)", description="My issues (issue-center personal dimension), enriched with scene/stage/is_running/agent_statuses.")
async def my_issues(status: str = Query("", pattern="^(active|processing|awaiting_input|pending_review|completed|)?$"),
                    repo_key: str = Query("", description="按仓库过滤"),
                    sort_order: str = Query("desc", pattern="^(desc|asc)$", description="按创建时间排序方向"),
                    mode: str = Query("default", pattern="^(default|time)$", description="default=状态优先；time=纯时间序"),
                    created_by_me: str = Query("", description="仅我创建的任务"),
                    assignees: str = Query("", description="按创建人筛选(逗号分隔)"),
                    user: dict = Depends(require_org_member)):
    """我的需求列表（需求中心·个人维度）。

    返回当前用户的需求（Issue 卡片行，user_id 过滤）+ 富化
    （scene/stage/is_running/agent_statuses）。
    组织隔离：非个人组织只返回 org_id 匹配的 issue。
    """
    from src.api.issues_center import _query_tasks, _build_assignee_filter
    from src.repo_key import to_repo_key
    from src.user.org_db import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal else _org_id
    clauses = []
    params = []
    if repo_key:
        # 跨 provider 双查：前端传入 owner/repo 无 provider 信息，
        # to_repo_key 默认 gitcode: 前缀会漏查 GitHub 仓库数据。
        _rk = to_repo_key(repo_key)
        _rk_alt = ("github:" + repo_key) if _rk.startswith("gitcode:") else ("gitcode:" + repo_key)
        _rk_list = [_rk, _rk_alt]
        _rk_ph = ",".join("?" * len(_rk_list))
        clauses.append(f"repo_key IN ({_rk_ph})")
        params.extend(_rk_list)
    clauses.append("user_id=?")
    params.append(user["id"])
    if status:
        clauses.append("status=?")
        params.append(status)
    # 组织隔离
    if _filter_org_id:
        clauses.append("org_id=?")
        params.append(_filter_org_id)
    _build_assignee_filter(assignees, clauses, params)
    where = " AND ".join(clauses)
    _order_dir = "DESC" if sort_order == "desc" else "ASC"
    _order_by = f"created_at {_order_dir}"
    if mode == "default":
        _order_by = f"CASE WHEN status='active' THEN 0 WHEN status='processing' THEN 1 ELSE 2 END, created_at {_order_dir}"
    issues_out = _query_tasks(where, params, _order_by, include_user_id=False)
    return {"issues": issues_out, "sort_order": sort_order, "mode": mode}


@router.get("/{owner}/{repo}", operation_id="issues_list", summary="Repo issue list (stage/status enriched)", description="Repo issue list (unified all view) enriched with stage/status/events; paged.")
def list_repo_issues(owner: str, repo: str,
                     status: str = Query("", pattern="^(active|processing|awaiting_input|pending_review|completed|cancelled|)$"),
                     sort_order: str = Query("desc", pattern="^(desc|asc)$", description="按创建时间排序方向"),
                     mode: str = Query("default", pattern="^(default|time)$", description="default=状态优先；time=纯时间序"),
                     assignees: str = Query("", description="按创建人筛选(逗号分隔)"),
                     user: dict = Depends(require_org_member)):
    """仓库 Issue 列表（归一：收编原 repo/{owner}/{repo}，复用 _query_tasks 富化 + 事件聚合）。

    - 按 owner/repo 定位仓库，返回全部卡片富化（scene/stage/is_running/agent_statuses）+ events_count/latest_event
    - 排序按 created_at + 状态优先级（mode 可切换纯时间序）
    - 行为对齐原 issues_list（仓库 public 读聚合，无显式仓库权限门禁）
    """
    from src.api.issues_center import _query_tasks, _build_assignee_filter
    from src.user.org_db import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal else _org_id
    repo_keys = _repo_keys_of(owner, repo)
    placeholders = ",".join("?" * len(repo_keys))
    clauses = [f"repo_key IN ({placeholders})"]
    params: list = list(repo_keys)
    if status:
        clauses.append("status=?")
        params.append(status)
    # 组织隔离
    if _filter_org_id:
        clauses.append("org_id=?")
        params.append(_filter_org_id)
    _build_assignee_filter(assignees, clauses, params)
    where = " AND ".join(clauses)
    _order_dir = "DESC" if sort_order == "desc" else "ASC"
    _order_by = f"created_at {_order_dir}"
    if mode == "default":
        _order_by = (f"CASE WHEN status='active' THEN 0 WHEN status='processing' THEN 1 "
                     f"WHEN status='awaiting_input' THEN 2 WHEN status='pending_review' THEN 3 "
                     f"ELSE 4 END, created_at {_order_dir}")
    issues = _query_tasks(where, params, _order_by, include_user_id=True)
    # 事件聚合字段 + number/task_status/owner_agent 等别名（原 issues_list 字段兼容）
    group_map = _group_events_by_issue(repo_keys)
    stage_map = _issue_stage_map(repo_keys)
    for it in issues:
        _num = it.get("issue_number")
        it["number"] = _num or it.get("number")
        it["task_status"] = it["task_status"] if "task_status" in it else it.get("status", "")
        _st = stage_map.get(str(_num or "")) or {}
        it["owner_agent"] = it.get("owner_agent") or _st.get("owner_agent", "vod")
        it.setdefault("assignee", _st.get("assignee", ""))
        it.setdefault("parent_issue_id", _st.get("parent_issue_id", ""))
        it.setdefault("is_subtask_mode", _st.get("is_subtask_mode") or 0)
        evs = group_map.get(str(_num or "")) or []
        it["events_count"] = len(evs)
        # P1-E: Slim latest_event to essential fields (reduce response from ~713KB)
        _ev = evs[-1] if evs else None
        if _ev:
            it["latest_event"] = {
                "seq": _ev.get("seq"),
                "status": _ev.get("status"),
                "title": _ev.get("title"),
                "created_at": _ev.get("created_at"),
            }
        else:
            it["latest_event"] = None
        # P1-E: Remove heavy fields not needed in list view (detail page has /issues/{id}/pipeline)
        it.pop("agent_statuses", None)
        it.pop("task_description", None)

    # PR 订阅事件：从 events 表查 issue_id LIKE '#pr_%' 的记录，构造伪卡片混入列表
    pr_items = _query_pr_subscription_events(repo_keys)
    issues.extend(pr_items)

    return {"issues": issues, "total": len(issues)}


@router.post("/{owner}/{repo}", operation_id="issues_create", summary="Create issue (unified entry)", description="Creates an Issue (unified external entry): creates the GitCode issue, records it and triggers the pipeline. Body: repo_key?, title, description?, assignee?, scene_id?, is_subtask_mode.")
async def create_issue(owner: str, repo: str, body: dict = None,
                       user: dict = Depends(require_org_member)):
    """创建 Issue（对外唯一创建入口）：建 GitCode Issue + 建档 + 触发 pipeline。

    body: {repo_key?, title, description?, assignee?, scene_id?, is_subtask_mode?}
    repo_key 可选（缺省按 {owner}/{repo} 拼）。返回 {ok, issue_number, task_id, created, ...}
    """
    from src.api.issues_center import TaskCreateBody, create_task_via_issue
    _body = body or {}
    b = TaskCreateBody(
        repo_key=_body.get("repo_key") or f"{owner}/{repo}",
        title=_body.get("title", "") or "",
        description=_body.get("description", "") or "",
        assignee=_body.get("assignee", "") or "",
        scene_id=_body.get("scene_id", "") or "",
        is_subtask_mode=int(_body.get("is_subtask_mode") or 0),
        attachments=_body.get("attachments") or [],
    )
    return await create_task_via_issue(b, user)


@router.get("/{owner}/{repo}/{number}", operation_id="issues_detail", summary="Issue detail (stage+events+pipeline)", description="Single issue detail: base fields + events (seq asc) + pipeline segments.")
def get_issue_detail(owner: str, repo: str, number: int,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_keys = _repo_keys_of(owner, repo)
    _enforce_session_repo_scope(user, repo_keys)
    num = str(number)
    stage_map = _issue_stage_map(repo_keys)
    status_map = _task_status_map(repo_keys)
    group_map = _group_events_by_issue(repo_keys)
    title_map = _issue_title_map(repo_keys)
    if num not in stage_map and num not in status_map and num not in group_map:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
    base = _assemble_issue(repo_keys[0], num, stage_map, status_map, group_map, title_map)
    events = group_map.get(num) or []
    pipeline = _pipeline_from_archive(repo_keys, num) or _pipeline_from_events(repo_keys[0], num, events)
    return {**base, "events": events, "pipeline": pipeline}


@router.get("/{owner}/{repo}/{number}/events", operation_id="issues_events", summary="Issue events list (with structured output)", description="Event list of an issue (seq asc), each with structured output/logs when present. `number` accepts `5` or `#5`.")
def get_issue_events(owner: str, repo: str, number: str,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_keys = _repo_keys_of(owner, repo)
    _enforce_session_repo_scope(user, repo_keys)
    group_map = _group_events_by_issue(repo_keys)
    num = _num_from_issue_id(number) or number.replace("#", "")
    if num not in group_map:
        stage_map = _issue_stage_map(repo_keys)
        status_map = _task_status_map(repo_keys)
        if num not in stage_map and num not in status_map:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
        return {"events": [], "segments": [], "total": 0}
    events = group_map[num]
    return {"events": events, "segments": events, "total": len(events)}


@router.get("/{owner}/{repo}/{number}/pipeline", operation_id="issues_pipeline", summary="Issue pipeline roadmap", description="Pipeline roadmap of an issue: issue + summary + status + segments.")
def get_issue_pipeline(owner: str, repo: str, number: int,
                       user: dict = Depends(get_current_user_or_api_key)):
    repo_keys = _repo_keys_of(owner, repo)
    num = str(number)
    group_map = _group_events_by_issue(repo_keys)
    events = group_map.get(num) or []
    # weekly-05：优先 events 表（含结构化 output）；无事件时回退 event.json 档案
    if events:
        pipeline = _pipeline_from_events(repo_keys[0], num, events)
    else:
        pipeline = _pipeline_from_archive(repo_keys, num) or {
            "issue": {"number": number}, "summary": "", "status": "unknown", "segments": []}
    if not pipeline.get("segments") and num not in _issue_stage_map(repo_keys):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
    return pipeline