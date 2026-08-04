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
            stages.append({"agent": agent, "latest_event": None})
            stage_statuses.append(0)
            continue

        latest = agent_events[-1]
        status = STAGE_STATUS_MAP.get(latest.get("status", "pending"), 0)
        # 最新一条 event 明细（卡片展示用）
        latest_event = {
            "seq": latest.get("seq", 0),
            "title": latest.get("title", "") or latest.get("event_type", ""),
            "event_type": latest.get("event_type", ""),
            "status": latest.get("status", ""),
            "actor": latest.get("actor", ""),
        }
        stages.append({"agent": agent, "latest_event": latest_event})
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

    # 当前阶段责任人：取最后一个非 pending 阶段（最新处理/正在处理的 agent）的最新 event actor
    current_owner = ""
    for i in range(len(stage_statuses) - 1, -1, -1):
        if stage_statuses[i] > 0:
            agent = AGENT_ORDER[i]
            latest = stages[i].get("latest_event") or {}
            actor = latest.get("actor", "")
            if actor:
                from src.user.db import get_all_bot_usernames
                if actor in get_all_bot_usernames():
                    from src.api.event_pipeline import get_agent_display_name
                    current_owner = get_agent_display_name(agent)
                else:
                    current_owner = actor
            break

    return {
        "issue_id": issue_id,
        "title": title,
        "stages": stages,
        "statuses": stage_statuses,
        "result": result,
        "agents": involved_agents,
        "current_owner": current_owner,
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
                "SELECT seq, issue_id, event_type, agent_type, title, summary, status, created_at, actor FROM events WHERE repo_key=? AND issue_id != '' ORDER BY seq ASC",
                (repo_key,)
            )
            rows = c.fetchall()
        except Exception:
            rows = []

    events = [
        {"seq": r[0], "issue_id": r[1], "event_type": r[2], "agent_type": r[3] or "", "title": r[4], "status": r[5], "created_at": r[6], "actor": r[7] or ""}
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
