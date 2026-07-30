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
                "SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key=? ORDER BY seq DESC LIMIT 5",
                (repo_key,)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": r[6]}
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
                "status": "",
                "last_active_at": "",
            })

    # 计算每个 Agent 的事件统计
    agent_event_counts = {}
    for ev in recent_events:
        at = ev["agent_type"]
        if at:
            agent_event_counts[at] = agent_event_counts.get(at, 0) + 1

    for atype in all_types:
        if atype not in agent_event_counts:
            agent_event_counts[atype] = 0

    # 获取用户角色
    role = "Member"
    try:
        repos = user_db.get_managed_repos(user["id"])
        for r in repos:
            if r["repo_owner"] == owner and r["repo_name"] == repo:
                role = "Maintainer" if r.get("is_admin") else "Member"
                break
    except Exception:
        pass

    event_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for ev in recent_events:
        s = ev["status"]
        if s in event_counts:
            event_counts[s] += 1

    return {
        "repo": repo_key,
        "role": role,
        "metrics": {
            "total_events": len(recent_events),
            "active_agents": sum(1 for a in agent_sessions if a["status"] == "running"),
            "pending_events": event_counts["pending"],
            "failed_events": event_counts["failed"],
        },
        "agents": agent_sessions,
        "agent_event_counts": agent_event_counts,
        "activity": recent_events,
    }
