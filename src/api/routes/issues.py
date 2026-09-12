"""Issues 路由：以 Issue 为中心对外整体开放（阶段 + 事件 + pipeline）。

与 /api/tasks、/api/events 不同，本路由按 「Issue 视角」聚合：
- issue 基本信息 + pipeline 阶段（issue_state.stage/owner_agent/scene）+ 任务中心状态（tasks.status）
- 该 Issue 的事件流（events 表，按 issue_id 聚类）
- Issue pipeline 路线图（event.json 结构化链）

认证：get_current_user_or_api_key（JWT / tk_ 会话 token / grape_ API Key 三通道）。
Design: docs/weekly-02/issues-api-design.md
"""
import re

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key
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

    Phase C1 过渡：阶段字段以 `issue_state`（权威阶段机）为准，`issue` 卡片行兜底
    （收编后字段回填/双写期间两表一致；issue_state 停写后自动落在单表）。
    """
    from src.session_manager import db, db_lock
    out = {}
    # 1. issue_state（权威阶段；收编前亦正确）
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
                if not n:
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
    # 2. issue 单表兜底（无 issue_state 记录，但已有卡片行的 issue）
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
                if not n or n in out:
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

@router.get("/{owner}/{repo}", operation_id="issues_list", summary="仓库 Issue 列表（含阶段/状态聚合）")
def list_repo_issues(owner: str, repo: str,
                     user: dict = Depends(get_current_user_or_api_key)):
    repo_key = _repo_key_of(owner, repo)
    stage_map = _issue_stage_map(repo_key)
    status_map = _task_status_map(repo_key)
    group_map = _group_events_by_issue(repo_key)
    title_map = _issue_title_map(repo_key)
    numbers = set(stage_map) | set(status_map) | set(group_map)
    issues = sorted((_assemble_issue(repo_key, n, stage_map, status_map, group_map, title_map)
                     for n in numbers),
                    key=lambda i: i["latest_event"]["created_at"] if i["latest_event"] else "",
                    reverse=True)
    return {"issues": issues, "total": len(issues)}


@router.post("/{owner}/{repo}", operation_id="issues_create", summary="创建 Issue（迁移自 /api/tasks/create）")
async def create_issue(owner: str, repo: str, body: dict = None,
                       user: dict = Depends(get_current_user_or_api_key)):
    """创建 Issue：复用 /api/tasks/create 逻辑（ensure_issue_task + GitCode create + 触发 pipeline）。

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