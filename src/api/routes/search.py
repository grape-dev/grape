"""Search 路由：全局搜索"""
from fastapi import APIRouter, Depends, Query
from src.api.deps import get_current_user
from src.user import db as user_db

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/")
async def global_search(q: str = Query("", min_length=1),
                        type: str = Query("all", pattern="^(all|event|repo|agent)$"),
                        user: dict = Depends(get_current_user)):
    results = {"events": [], "repos": [], "agents": []}
    q_lower = q.lower()

    if type in ("all", "event"):
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "SELECT seq, repo_key, event_type, title, status FROM events WHERE LOWER(title) LIKE ? OR LOWER(event_type) LIKE ? OR LOWER(repo_key) LIKE ? ORDER BY seq DESC LIMIT 10",
                    (f"%{q_lower}%", f"%{q_lower}%", f"%{q_lower}%")
                )
                results["events"] = [
                    {"seq": r[0], "repo_key": r[1], "event_type": r[2], "title": r[3] or r[2], "status": r[4]}
                    for r in c.fetchall()
                ]
            except Exception:
                pass

    if type in ("all", "repo"):
        managed = user_db.get_managed_repos(user.get("id", 0)) if hasattr(user, "id") else []
        for r in managed:
            if q_lower in r.get("full_name", "").lower():
                results["repos"].append({
                    "full_name": r["full_name"],
                    "role": "Admin" if r.get("is_admin") else "Member",
                })

    if type in ("all", "agent"):
        from src.api.routes.agents import AGENT_META
        for name, meta in AGENT_META.items():
            if q_lower in name or q_lower in meta["display_name"].lower():
                results["agents"].append({
                    "name": name,
                    "display_name": meta["display_name"],
                    "icon": meta["icon"],
                })

    return results
