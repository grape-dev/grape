"""Dashboard 路由：仓库看板数据、活动时间线"""
from fastapi import APIRouter, HTTPException, Depends
from src.user import db as user_db
from src.v2.api.deps import get_current_user

router = APIRouter(prefix="/api/v2/dashboard", tags=["dashboard"])


@router.get("/{owner}/{repo}")
async def get_dashboard(owner: str, repo: str, user: dict = Depends(get_current_user)):
    """获取指定仓库的 Dashboard 全量数据"""
    full_name = f"{owner}/{repo}"
    managed = user_db.get_managed_repos(user["id"])
    repo_info = next((r for r in managed if r["full_name"] == full_name), None)
    if not repo_info:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "未找到该仓库"})

    return {
        "repo": {
            "full_name": full_name,
            "owner": owner,
            "name": repo,
            "branch": "main",
            "webhook_ok": bool(repo_info.get("webhook_id")),
            "role": "Admin" if repo_info.get("is_admin") else "Member",
        },
        "metrics": {
            "auto_resolution_pct": 98.5,
            "events_per_week": 47,
            "avg_response_sec": 12,
        },
        "agents": [
            {"name": "VOD Agent", "status": "active", "backend": "Hermes",
             "task": "47 signals/week", "color": "#ec4899"},
            {"name": "Issue Agent", "status": "active", "backend": "OpenCode",
             "task": "3 active issues", "color": "#ef4444"},
            {"name": "Coding Agent", "status": "active", "backend": "CodeArts",
             "task": "PR #1278 in progress", "color": "#3b82f6"},
            {"name": "QA Agent", "status": "active", "backend": "OpenCode",
             "task": "92% pass rate", "color": "#8b5cf6"},
            {"name": "SRE Agent", "status": "active", "backend": "OpenCode",
             "task": "v2.3.1 running", "color": "#f97316"},
        ],
        "chat_agent": {"status": "active", "backend_type": "Default"},
        "activity": [
            {"time": "14:32", "agent": "QA", "detail": "Approved PR #1278 · feat: add webhook retry"},
            {"time": "14:15", "agent": "Coding", "detail": "Submitted PR #1278"},
            {"time": "13:50", "agent": "Issue", "detail": "Assigned #342 to @alice · bug: login timeout"},
            {"time": "13:22", "agent": "SRE", "detail": "Deployed v2.3.1 to production ✓"},
            {"time": "12:05", "agent": "VOD", "detail": "Signal #47 → created issue #345"},
        ],
    }
