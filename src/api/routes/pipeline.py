"""Pipeline 路由：Issue 工作流聚合展示"""
from fastapi import APIRouter, Depends
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

AGENT_ORDER = ["vod", "issue", "coding", "qa", "sre"]
AGENT_LABELS = ["VOD", "Issue", "Coding", "QA", "SRE"]

STAGE_STATUS_MAP = {
    "pending": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
}


def _get_issue_pipeline(repo_key: str, issue_id: str, events: list) -> dict:
    """根据 issue_id 下的事件列表构建单个 Pipeline"""
    issue_events = [e for e in events if e.get("issue_id") == issue_id]
    if not issue_events:
        return None

    title = issue_events[0].get("title", "") or issue_id
    stages = []
    stage_statuses = []

    for agent in AGENT_ORDER:
        agent_events = [e for e in issue_events if e.get("agent_type") == agent]
        if not agent_events:
            stages.append(["—", "—"])
            stage_statuses.append(0)
            continue

        latest = agent_events[-1]
        status = STAGE_STATUS_MAP.get(latest.get("status", "pending"), 0)
        first_event_type = agent_events[0].get("event_type", "")
        stage_desc1 = f"{first_event_type} → {issue_id}" if first_event_type else issue_id

        has_more = len(agent_events) > 1
        stage_desc2 = latest.get("title", "") or latest.get("event_type", "")
        if has_more:
            stage_desc2 += f" ({len(agent_events)} evts)"

        stages.append([stage_desc1, stage_desc2])
        stage_statuses.append(status)

    all_done = all(s == 2 for s in stage_statuses)
    any_failed = any(s == 3 for s in stage_statuses)
    any_running = any(s == 1 for s in stage_statuses)

    if all_done:
        result = "Released"
    elif any_failed:
        result = "Failed"
    elif any_running:
        result = "Processing"
    else:
        result = "Pending"

    involved_agents = [AGENT_LABELS[i] for i, s in enumerate(stage_statuses) if s > 0]

    return {
        "issue_id": issue_id,
        "title": title,
        "stages": stages,
        "statuses": stage_statuses,
        "result": result,
        "agents": involved_agents,
        "event_count": len(issue_events),
    }


@router.get("/{owner}/{repo}")
async def get_pipeline(owner: str, repo: str, user: dict = Depends(get_current_user)):
    repo_key = f"{owner}/{repo}"

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT seq, issue_id, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key=? AND issue_id != '' ORDER BY seq ASC",
                (repo_key,)
            )
            rows = c.fetchall()
        except Exception:
            rows = []

    events = [
        {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "", "title": r[4], "status": r[5], "created_at": r[6]}
        for r in rows
    ]

    issue_ids = list(dict.fromkeys(e["issue_id"] for e in events if e["issue_id"]))
    pipelines = [_get_issue_pipeline(repo_key, iid, events) for iid in issue_ids]
    pipelines = [p for p in pipelines if p is not None]

    active = pipelines[0] if pipelines else None
    history = pipelines[1:] if len(pipelines) > 1 else []

    return {
        "repo": repo_key,
        "active": active,
        "history": history,
        "active_count": 1 if active and active["result"] in ("Processing", "Pending") else 0,
        "today_count": len(pipelines),
    }
