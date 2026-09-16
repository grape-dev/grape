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

from fastapi import APIRouter, HTTPException, Depends, Query, File, UploadFile
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.config import log
from src.api.time_utils import normalize_event_time

router = APIRouter(prefix="/api/issues", tags=["issues"])


# ===================== 辅助 =====================

def _num_from_issue_id(issue_id: str) -> str:
    m = re.search(r"(\d+)", str(issue_id or ""))
    return m.group(1) if m else ""


def _repo_key_of(owner: str, repo: str) -> str:
    from src.repo_key import to_repo_key
    return to_repo_key(f"{owner}/{repo}")


def _issue_stage_map(repo_key: str) -> dict:
    """仓库内 issue number → issue 阶段摘要（stage/owner/scene/assignee/subtask）。

    Phase C1/C3：`issue` 单表为权威（已收编 + 停写 issue_state），直接读 issue 卡片行；
    `issue_state` 仅作停写过渡期兜底。
    """
    from src.session_manager import db, db_lock
    out = {}
    # 1. issue 单表（权威）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT issue_number, stage, owner_agent, assignee, a2a_seq, scene_id, "
                "parent_issue_id, is_subtask_mode, scene_user_id, allow_scene_override "
                "FROM issue WHERE repo_key=?",
                (repo_key,))
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
                "SELECT issue_id, stage, owner_agent, assignee, a2a_seq, scene_id, "
                "parent_issue_id, is_subtask_mode, scene_user_id, allow_scene_override "
                "FROM issue_state WHERE repo_key=?",
                (repo_key,))
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


def _task_status_map(repo_key: str) -> dict:
    """仓库内 issue_number → tasks.status（任务中心状态）。"""
    from src.session_manager import db, db_lock
    out = {}
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT issue_number, status FROM issue WHERE repo_key=?", (repo_key,))
            for r in c.fetchall():
                out[str(r[0])] = r[1]
        except Exception as e:
            log.warning(f"issues: load tasks status failed: {e}")
    return out


def _group_events_by_issue(repo_key: str) -> dict:
    """仓库内 issue_id → events 列表（seq 升序），带时间格式化。"""
    from src.session_manager import db, db_lock
    out = {}
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, actor, summary, status, created_at "
                "FROM events WHERE repo_key=? AND issue_id!='' ORDER BY seq ASC",
                (repo_key,))
            rows = c.fetchall()
        except Exception:
            rows = []
    for r in rows:
        iid = r[1]
        n = _num_from_issue_id(iid)
        if not n:
            continue
        out.setdefault(n, []).append({
            "seq": r[0], "event_type": r[2], "agent_type": r[3] or "",
            "title": r[4] or r[2], "actor": r[5] or "",
            "summary": r[6] or r[4] or r[2],
            "status": r[7], "created_at": normalize_event_time(r[8]),
        })
    return out


def _issue_title_map(repo_key: str) -> dict:
    """仓库内 issue_number → 标题（从 events 首条带 title 的事件取）。"""
    g = _group_events_by_issue(repo_key)
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


def _pipeline_from_archive(repo_key: str, number: str) -> dict | None:
    """读 event.json 组装 pipeline 路线图。"""
    try:
        from src.api.event_archive import load_event_json
        arch = load_event_json(repo_key, number)
        if not arch:
            return None
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
        log.warning(f"issues: load pipeline archive failed repo={repo_key} #{number}: {e}")
        return None


def _pipeline_from_events(repo_key: str, number: str, events: list[dict]) -> dict:
    """无 event.json 时，由 events 表降级重构 pipeline（分号 view）。"""
    segments = [{
        "seq": ev["seq"], "agent": ev.get("agent_type", ""),
        "event_type": ev.get("event_type", ""),
        "status": ev.get("status", ""),
        "output": {"conclusion": ev.get("summary", ""), "next_step": "", "actions": [], "artifacts": []},
    } for ev in events]
    return {"issue": {"number": int(number)}, "summary": "", "status": "unknown", "segments": segments}


# ===================== 端点 =====================

# ===================== B 类操作（tasks-issues-api-rebound-design.md §三）=====================
# 迁移自 /api/tasks/{task_id}/...：`{issue_id}` 即原 `{task_id}`（= issue.id），定位/逻辑零改动。


@router.post("/{issue_id}/start", operation_id="issues_start", summary="打开 Issue 会话")
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


@router.get("/{issue_id}/status", operation_id="issues_status_get", summary="查询 Issue 状态（轮询 wait 用）")
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


@router.put("/{issue_id}/status", operation_id="issues_status", summary="更新 Issue 状态")
def change_issue_status(issue_id: int, body: _IssueStatusBody, user: dict = Depends(require_no_session_token)):
    """Issue 状态迁移（active/processing/awaiting_input/pending_review/completed/cancelled 流转规则）。

    user_only：状态是**需求中心视角（用户可读）**，且 awaiting_input/completed/cancelled 允许用户直接
    设置（高优先级覆盖）；若放行 tk_ 会话 token，Agent 执行期可随意改状态，与 pipeline 驱动的 stage
    脱钩→冲突/不一致，干扰任务执行。故仅用户（JWT / grape_）可改；读（GET /status，轮询 wait 用）保持 dual。
    """
    from src.api.routes.tasks import _TASK_STATUS_FLOWS
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


@router.post("/{issue_id}/confirm-done", operation_id="issues_confirm_done", summary="确认 Issue 完成")
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
            return {"ok": True, "already_done": True}
        if _status == 'cancelled':
            raise HTTPException(status_code=400, detail={"code": 400, "message": "已取消需求不可确认完成"})
        c.execute(
            "UPDATE issue SET status='completed', completed_reason=?, completed_at=datetime('now') WHERE id=?",
            (body.confirmation, issue_id))
        if _issue_num:
            set_issue_stage(_repo_key, f"#{_issue_num}", "done", "user")
        db.commit()
    log.info(f"Issue #{issue_id} confirmed done by {body.confirmation}")
    return {"ok": True, "issue_id": issue_id, "status": "completed", "stage": "done"}


@router.post("/{issue_id}/cancel", operation_id="issues_cancel", summary="取消 Issue（停流水线）")
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


@router.post("/{issue_id}/resume", operation_id="issues_resume", summary="恢复已取消的 Issue")
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
        _scene_id = get_issue_scene(repo_key, _issue_id) or "default"
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


@router.get("/{issue_id}/pipeline", operation_id="issues_pipeline_detail", summary="Issue pipeline 路线图（按 issue.id）")
async def get_issue_pipeline_by_id(issue_id: int, user: dict = Depends(require_no_session_token)):
    """获取需求关联的流水线信息和事件历史（详情页展示）。"""
    from src.api.event_store import get_events
    from src.api.pipeline_config import get_pipeline_config
    from src.api.session_store import get_issue_stage, get_issue_scene
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
                  (issue_id, user["id"]))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        repo_key, issue_number = row[0], row[1]
    issue_key = f"#{issue_number}" if issue_number else ""
    _scene_id = get_issue_scene(repo_key, issue_key) or "default"
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
    return {
        "scene_id": _scene_id,
        "scene_name": (pipe_cfg or {}).get("name", _scene_id),
        "current_stage": _current_stage,
        "nodes": nodes,
        "events": events,
    }


@router.get("/{issue_id}/clarifications", operation_id="issues_clarifications", summary="Issue 澄清历史")
async def get_issue_clarifications(issue_id: int, user: dict = Depends(require_no_session_token)):
    """拉取 agent 在该 Issue 上的澄清消息。"""
    from src.api.routes.tasks import _get_task_issue_info, _find_agent_task_ids
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


class _IssueClarifyBody(BaseModel):
    message: str = ""


@router.post("/{issue_id}/clarify", operation_id="issues_clarify", summary="回复 Issue 澄清")
async def clarify_issue(issue_id: int, body: _IssueClarifyBody, user: dict = Depends(require_no_session_token)):
    """开发者在需求中心回复 agent 澄清：同步发 GitCode Issue 评论 + 写入任务会话。"""
    from src.api.routes.tasks import _get_task_issue_info, _gitcode_api_with_refresh, _is_gitcode_401
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
        from src.user.db import get_user_github_token
        api_token = get_user_github_token(user["id"]) or ""
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "GitHub API Token 未配置"})
    else:
        from src.api.deps import get_user_token_with_refresh
        api_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})
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
            _gitcode_api_with_refresh(user, _post_comment)
            log.info(f"Issue clarify: posted comment to {repo_key}/issues/{issue_number} by {user['username']}")
        except HTTPException:
            raise
        except Exception as e:
            if _is_gitcode_401(e):
                raise HTTPException(status_code=401, detail={
                    "code": 401, "message": "GitCode 授权已过期，请重新登录授权",
                    "action": "reauth_required"
                })
            raise HTTPException(status_code=502, detail={"code": 502,
                                                         "message": f"GitCode 评论失败: {str(e)[:200]}"})
    if conv_id:
        try:
            from src.session_manager import add_message
            add_message(conv_id, "user", msg)
        except Exception as e:
            log.warning(f"Issue clarify: write conversation message failed: {e}")
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE issue SET status='processing' "
                "WHERE repo_key=? AND issue_number=? AND status='awaiting_input'",
                (repo_key, issue_number))
            db.commit()
            if c.rowcount > 0:
                log.info(f"Issue clarify: awaiting_input → processing for {repo_key}/#{issue_number}")
    except Exception as e:
        log.warning(f"Issue clarify: restore processing failed: {e}")
    return {"ok": True, "message": "澄清已发送到 Issue 并同步到任务会话"}


@router.post("/{issue_id}/attachments", operation_id="issues_upload_attachment", summary="上传 Issue 附件")
async def upload_issue_attachment(issue_id: int, file: UploadFile = File(...),
                                  user: dict = Depends(require_no_session_token)):
    """上传附件到需求关联的附件目录。"""
    from src.api.routes.tasks import (_attach_save, _ATTACH_ALL_EXTS, _ATTACH_MAX_SIZE)
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


@router.get("/{issue_id}/attachments", operation_id="issues_list_attachments", summary="列出 Issue 附件")
def list_issue_attachments(issue_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """列出需求关联的附件文件列表。"""
    from src.api.routes.tasks import _attach_list
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


@router.delete("/{issue_id}/attachments/{filename}", operation_id="issues_delete_attachment", summary="删除 Issue 附件")
def delete_issue_attachment(issue_id: int, filename: str, user: dict = Depends(require_no_session_token)):
    """删除指定附件文件。filename 为 UUID 文件名。"""
    from src.api.routes.tasks import _attach_delete
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

@router.get("/mine", operation_id="issues_mine", summary="我的 Issue 列表（需求中心·个人维度）")
async def my_issues(status: str = Query("", pattern="^(active|processing|awaiting_input|pending_review|completed|)?$"),
                    repo_key: str = Query("", description="按仓库过滤"),
                    sort_order: str = Query("desc", pattern="^(desc|asc)$", description="按创建时间排序方向"),
                    mode: str = Query("default", pattern="^(default|time)$", description="default=状态优先；time=纯时间序"),
                    created_by_me: str = Query("", description="仅我创建的任务"),
                    assignees: str = Query("", description="按创建人筛选(逗号分隔)"),
                    user: dict = Depends(require_no_session_token)):
    """我的需求列表（需求中心·个人维度）。

    返回当前用户的需求（Issue 卡片行，user_id 过滤）+ 富化
    （scene/stage/is_running/agent_statuses）。
    """
    from src.api.routes.tasks import _query_tasks, _build_assignee_filter
    from src.repo_key import to_repo_key
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
    _build_assignee_filter(assignees, clauses, params)
    where = " AND ".join(clauses)
    _order_dir = "DESC" if sort_order == "desc" else "ASC"
    _order_by = f"created_at {_order_dir}"
    if mode == "default":
        _order_by = f"CASE WHEN status='active' THEN 0 WHEN status='processing' THEN 1 ELSE 2 END, created_at {_order_dir}"
    issues_out = _query_tasks(where, params, _order_by, include_user_id=False)
    return {"issues": issues_out, "sort_order": sort_order, "mode": mode}


@router.get("/{owner}/{repo}", operation_id="issues_list", summary="仓库 Issue 列表（含阶段/状态/富化聚合）")
def list_repo_issues(owner: str, repo: str,
                     status: str = Query("", pattern="^(active|processing|awaiting_input|pending_review|completed|cancelled|)$"),
                     sort_order: str = Query("desc", pattern="^(desc|asc)$", description="按创建时间排序方向"),
                     mode: str = Query("default", pattern="^(default|time)$", description="default=状态优先；time=纯时间序"),
                     assignees: str = Query("", description="按创建人筛选(逗号分隔)"),
                     user: dict = Depends(get_current_user_or_api_key)):
    """仓库 Issue 列表（归一：收编原 repo/{owner}/{repo}，复用 _query_tasks 富化 + 事件聚合）。

    - 按 owner/repo 定位仓库，返回全部卡片富化（scene/stage/is_running/agent_statuses）+ events_count/latest_event
    - 排序按 created_at + 状态优先级（mode 可切换纯时间序）
    - 行为对齐原 issues_list（仓库 public 读聚合，无显式仓库权限门禁）
    """
    from src.api.routes.tasks import _query_tasks, _build_assignee_filter
    repo_key = _repo_key_of(owner, repo)
    clauses = ["repo_key=?"]
    params: list = [repo_key]
    if status:
        clauses.append("status=?")
        params.append(status)
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
    group_map = _group_events_by_issue(repo_key)
    stage_map = _issue_stage_map(repo_key)
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
        it["latest_event"] = evs[-1] if evs else None
    return {"issues": issues, "total": len(issues)}


@router.post("/{owner}/{repo}", operation_id="issues_create", summary="创建 Issue（对外唯一创建入口）")
async def create_issue(owner: str, repo: str, body: dict = None,
                       user: dict = Depends(require_no_session_token)):
    """创建 Issue（对外唯一创建入口）：建 GitCode Issue + 建档 + 触发 pipeline。

    body: {repo_key?, title, description?, assignee?, scene_id?, is_subtask_mode?}
    repo_key 可选（缺省按 {owner}/{repo} 拼）。返回 {ok, issue_number, task_id, created, ...}
    """
    from src.api.routes.tasks import TaskCreateBody, create_task_via_issue
    _body = body or {}
    b = TaskCreateBody(
        repo_key=_body.get("repo_key") or f"{owner}/{repo}",
        title=_body.get("title", "") or "",
        description=_body.get("description", "") or "",
        assignee=_body.get("assignee", "") or "",
        scene_id=_body.get("scene_id", "") or "",
        is_subtask_mode=int(_body.get("is_subtask_mode") or 0),
    )
    return await create_task_via_issue(b, user)


@router.get("/{owner}/{repo}/{number}", operation_id="issues_detail", summary="单个 Issue 详情（阶段+事件+pipeline）")
def get_issue_detail(owner: str, repo: str, number: int,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_key = _repo_key_of(owner, repo)
    num = str(number)
    stage_map = _issue_stage_map(repo_key)
    status_map = _task_status_map(repo_key)
    group_map = _group_events_by_issue(repo_key)
    title_map = _issue_title_map(repo_key)
    if num not in stage_map and num not in status_map and num not in group_map:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
    base = _assemble_issue(repo_key, num, stage_map, status_map, group_map, title_map)
    events = group_map.get(num) or []
    pipeline = _pipeline_from_archive(repo_key, num) or _pipeline_from_events(repo_key, num, events)
    return {**base, "events": events, "pipeline": pipeline}


@router.get("/{owner}/{repo}/{number}/events", operation_id="issues_events", summary="该 Issue 的事件列表")
def get_issue_events(owner: str, repo: str, number: int,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_key = _repo_key_of(owner, repo)
    group_map = _group_events_by_issue(repo_key)
    num = str(number)
    if num not in group_map:
        stage_map = _issue_stage_map(repo_key)
        status_map = _task_status_map(repo_key)
        if num not in stage_map and num not in status_map:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
        return {"events": [], "total": 0}
    events = group_map[num]
    return {"events": events, "total": len(events)}


@router.get("/{owner}/{repo}/{number}/pipeline", operation_id="issues_pipeline", summary="Issue pipeline 路线图")
def get_issue_pipeline(owner: str, repo: str, number: int,
                       user: dict = Depends(get_current_user_or_api_key)):
    repo_key = _repo_key_of(owner, repo)
    num = str(number)
    group_map = _group_events_by_issue(repo_key)
    events = group_map.get(num) or []
    pipeline = _pipeline_from_archive(repo_key, num) or _pipeline_from_events(repo_key, num, events)
    if not pipeline.get("segments") and num not in _issue_stage_map(repo_key):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"Issue #{number} 不存在"})
    return pipeline