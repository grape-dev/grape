"""Dashboard 路由：仓库概览、指标、Agent 状态"""
from fastapi import APIRouter, Depends
from src.user import db as user_db
from src.api.deps import get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/{owner}/{repo}")
async def get_dashboard(owner: str, repo: str, user: dict = Depends(get_current_user)):
    repo_key = f"{owner}/{repo}"

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()

        recent_events = []
        try:
            c.execute(
                "SELECT seq, event_type, agent_type, title, status, created_at FROM events WHERE repo_key=? ORDER BY seq DESC LIMIT 5",
                (repo_key,)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1], "status": r[4], "created_at": r[5]}
                for r in c.fetchall()
            ]
        except Exception:
            pass

        agent_sessions = []
        try:
            c.execute(
                "SELECT agent_type, backend, status, last_active_at FROM agent_sessions WHERE repo_key=? ORDER BY agent_type",
                (repo_key,)
            )
            agent_sessions = [
                {"agent_type": r[0], "backend": r[1], "status": r[2], "last_active_at": r[3]}
                for r in c.fetchall()
            ]
        except Exception:
            pass

    agent_types_present = {a["agent_type"] for a in agent_sessions}
    all_types = ["vod", "issue", "coding", "qa", "sre"]
    for atype in all_types:
        if atype not in agent_types_present:
            agent_sessions.append({
                "agent_type": atype,
                "backend": "opencode",
                "status": "idle",
                "last_active_at": "",
            })

    event_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for ev in recent_events:
        s = ev["status"]
        if s in event_counts:
            event_counts[s] += 1

    return {
        "repo": repo_key,
        "metrics": {
            "total_events": len(recent_events),
            "active_agents": sum(1 for a in agent_sessions if a["status"] == "running"),
            "pending_events": event_counts["pending"],
            "failed_events": event_counts["failed"],
        },
        "agents": agent_sessions,
        "activity": recent_events,
    }
