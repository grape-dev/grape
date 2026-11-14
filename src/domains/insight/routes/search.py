"""Search 路由：全局搜索

事件 / 智能体两类结果分别来自 runtime 域的 `events` / `agent_type` 表，
统一走 runtime public 的原语（`events_search_global` / `agent_types_search_global`），
本文件不再持有任何 SQL —— 三库拆分后进程主库没有这两张表，直发必 1146。
"""
import logging

from fastapi import APIRouter, Depends, Query
from src.api.deps import require_no_session_token
from src.domains.mgmt.user import db as user_db
from src.domains.runtime import public as runtime_public

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", operation_id="search_global", summary="Global search", description="Global search across events/repos/agents within the current user's accessible repos.")
def global_search(q: str = Query("", min_length=1),
                        type: str = Query("all", pattern="^(all|event|repo|agent)$"),
                        user: dict = Depends(require_no_session_token)):
    results = {"events": [], "repos": [], "agents": []}
    q_lower = q.lower()

    # 当前用户有权访问的仓库集合——事件搜索仅限这些仓库
    managed = user_db.get_managed_repos(str(user.get("id") or ""))
    user_repo_keys = {r["repo_key"] for r in managed}

    if type in ("all", "event"):
        try:
            # 原语返回 seq 倒序前 10 行候选；可见仓库过滤仍留在本处：
            # 原 SQL 的 LIMIT 10 先于过滤生效，下推到 SQL 会改变结果集。
            results["events"] = [
                {"seq": r[0], "repo_key": r[1], "event_type": r[2], "title": r[3] or r[2], "status": r[4]}
                for r in runtime_public.events_search_global(q)
                if r[1] in user_repo_keys  # 仅返回当前用户有权访问的仓库事件
            ]
        except Exception as e:
            log.debug("search: events lookup failed: %s", e)

    if type in ("all", "repo"):
        for r in managed:
            if q_lower in r.get("full_name", "").lower():
                results["repos"].append({
                    "full_name": r["full_name"],
                    "role": "Admin" if r.get("is_admin") else "Member",
                })

    if type in ("all", "agent"):
        try:
            for r in runtime_public.agent_types_search_global(q):
                results["agents"].append({
                    "name": r[0], "display_name": r[1] or r[0], "icon": r[2] or "",
                })
        except Exception as e:
            log.debug("search: agent types lookup failed: %s", e)

    return results