"""Dashboard API——仓库级 Dashboard + 系统级聚合统计端点（需登录 + 用户隔离）。"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_current_user
from src.user import db as user_db
from src.api.time_utils import normalize_event_time
from src.session_manager import db, db_lock

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_MAX_DAYS = 30


def _days_param(days: int = Query(7, ge=1, le=_MAX_DAYS)) -> int:
    return days


# FastAPI 依赖注入：直接用 Query 作为参数默认值
_days_dep = Query(7, ge=1, le=_MAX_DAYS)


def _check_repo_access(repo_key: str, user_id: int):
    """查 user_repos 确认用户是否有该仓库权限。无权限返回 403。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT 1 FROM user_repos WHERE user_id=? AND repo_key=? AND is_join=1",
            (user_id, repo_key)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该仓库"})


def _since_clause(days: int) -> str:
    """MySQL 时间过滤条件。"""
    return f"created_at > DATE_SUB(NOW(), INTERVAL {days} DAY)"


@router.get("/runtime/status")
async def dashboard_runtime_status(
    repo_key: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """Worker 在线/离线/槽位统计——按三级分类（全局/项目/个人）。"""
    username = user.get("username", "")

    def _stats(where_clause: str, params: list) -> dict:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                f"SELECT COUNT(*), "
                f"SUM(CASE WHEN status='online' THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN status!='online' THEN 1 ELSE 0 END), "
                f"COALESCE(SUM(busy_slots), 0), "
                f"COALESCE(SUM(max_concurrent), 0) "
                f"FROM agent_runtimes WHERE {where_clause}",
                params
            ).fetchone()
        return {
            "total": row[0] or 0,
            "online": row[1] or 0,
            "offline": row[2] or 0,
            "busy_slots": row[3] or 0,
            "max_concurrent": row[4] or 0,
        }

    empty = {"total": 0, "online": 0, "offline": 0, "busy_slots": 0, "max_concurrent": 0}

    # 全局 Worker：scope=global OR scope=admin
    global_stats = _stats("scope IN ('global', 'admin')", [])

    # 项目 Worker：scope=repo AND repo_key=当前项目
    project_stats = _stats("scope='repo' AND repo_key=%s", [repo_key]) if repo_key else empty

    # 个人 Worker：scope=user AND 凭据 owner_name=当前用户 AND repo_key=当前项目
    # agent_runtimes 没有 owner_name——JOIN runtime_credentials 获取
    def _stats_join(where_clause: str, params: list) -> dict:
        with db_lock:
            c = db.cursor()
            row = c.execute(
                f"SELECT COUNT(*), "
                f"SUM(CASE WHEN r.status='online' THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN r.status!='online' THEN 1 ELSE 0 END), "
                f"COALESCE(SUM(r.busy_slots), 0), "
                f"COALESCE(SUM(r.max_concurrent), 0) "
                f"FROM agent_runtimes r "
                f"JOIN runtime_credentials rc ON r.credential_id=rc.id "
                f"WHERE {where_clause}",
                params
            ).fetchone()
        return {
            "total": row[0] or 0,
            "online": row[1] or 0,
            "offline": row[2] or 0,
            "busy_slots": row[3] or 0,
            "max_concurrent": row[4] or 0,
        }

    if repo_key:
        personal_stats = _stats_join("r.scope='user' AND rc.owner_name=%s AND r.repo_key=%s", [username, repo_key])
    else:
        personal_stats = _stats_join("r.scope='user' AND rc.owner_name=%s", [username])

    return {
        "global": global_stats,
        "project": project_stats,
        "personal": personal_stats,
    }


@router.get("/task/daily")
async def dashboard_task_daily(
    days: int = _days_dep,
    repo_key: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """每日任务统计（按 agent_type 分组）。"""
    if repo_key:
        _check_repo_access(repo_key, user["id"])
    where = _since_clause(days)
    if repo_key:
        where += f" AND repo_key='{repo_key}'"
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            f"SELECT DATE(created_at) as date, agent_type, COUNT(*) as total, "
            f"SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed, "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, "
            f"COALESCE(AVG(duration), 0) as avg_duration "
            f"FROM events WHERE {where} "
            f"GROUP BY DATE(created_at), agent_type ORDER BY date"
        ).fetchall()
    return {
        "days": days,
        "data": [
            {
                "date": str(r[0]) if r[0] else "",
                "agent_type": r[1] or "",
                "total": r[2] or 0,
                "completed": r[3] or 0,
                "failed": r[4] or 0,
                "avg_duration": round(r[5] or 0, 1),
            }
            for r in rows
        ],
    }


@router.get("/task/by-agent")
async def dashboard_task_by_agent(
    days: int = _days_dep,
    repo_key: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """按 agent 的任务统计。"""
    if repo_key:
        _check_repo_access(repo_key, user["id"])
    where = _since_clause(days)
    if repo_key:
        where += f" AND repo_key='{repo_key}'"
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            f"SELECT agent_type, COUNT(*) as total, "
            f"SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed, "
            f"SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, "
            f"COALESCE(AVG(duration), 0) as avg_duration "
            f"FROM events WHERE {where} "
            f"GROUP BY agent_type ORDER BY total DESC"
        ).fetchall()
    return {
        "days": days,
        "data": [
            {
                "agent_type": r[0] or "",
                "total": r[1] or 0,
                "completed": r[2] or 0,
                "failed": r[3] or 0,
                "avg_duration": round(r[4] or 0, 1),
            }
            for r in rows
        ],
    }


@router.get("/failures/daily")
async def dashboard_failures_daily(
    days: int = _days_dep,
    repo_key: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """每日失败统计（按 exit_code 分组）。"""
    if repo_key:
        _check_repo_access(repo_key, user["id"])
    where = f"status='failed' AND {_since_clause(days)}"
    if repo_key:
        where += f" AND repo_key='{repo_key}'"
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            f"SELECT DATE(created_at) as date, exit_code, COUNT(*) as total "
            f"FROM events WHERE {where} "
            f"GROUP BY DATE(created_at), exit_code ORDER BY date"
        ).fetchall()
    return {
        "days": days,
        "data": [
            {
                "date": str(r[0]) if r[0] else "",
                "exit_code": r[1] if r[1] is not None else 0,
                "total": r[2] or 0,
            }
            for r in rows
        ],
    }


@router.get("/failures/by-agent")
async def dashboard_failures_by_agent(
    days: int = _days_dep,
    repo_key: str = Query(""),
    user: dict = Depends(get_current_user),
):
    """按 agent 的失败统计（按 exit_code 分组）。"""
    if repo_key:
        _check_repo_access(repo_key, user["id"])
    where = f"status='failed' AND {_since_clause(days)}"
    if repo_key:
        where += f" AND repo_key='{repo_key}'"
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            f"SELECT agent_type, exit_code, COUNT(*) as total "
            f"FROM events WHERE {where} "
            f"GROUP BY agent_type, exit_code ORDER BY total DESC"
        ).fetchall()
    return {
        "days": days,
        "data": [
            {
                "agent_type": r[0] or "",
                "exit_code": r[1] if r[1] is not None else 0,
                "total": r[2] or 0,
            }
            for r in rows
        ],
    }
@router.get("/{owner}/{repo}")
async def get_dashboard(owner: str, repo: str, user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

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
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                for r in c.fetchall()
            ]
        except Exception:
            pass

        agent_sessions = []
        try:
            c.execute(
                "SELECT s.agent_type, r.name, s.status, s.last_active_at, s.enable, COALESCE(rc.status,'') "
                "FROM devops_agent_session s LEFT JOIN agent_runtimes r ON s.runtime_id=r.id "
                "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                "WHERE s.repo_key=? ORDER BY s.agent_type",
                (repo_key,)
            )
            agent_sessions = []
            for r in c.fetchall():
                _backend = r[1] or ""
                # 凭据已吊销的引擎视为不可用（revoke 后旧行保留但实际无法执行）：
                # 本机直连（rc.status=''，无凭据行）与 active 正常显示，revoked 置空走前端红字
                if _backend and r[5] not in ("", "active"):
                    _backend = ""
                agent_sessions.append({
                    "agent_type": r[0], "backend": _backend, "status": r[2], "last_active_at": normalize_event_time(r[3]),
                    "enable": bool(r[4]) if r[4] is not None else True})
        except Exception:
            pass

        # 每个 Agent 的最近 2 个事件（供 Dashboard Agent 卡片展示，替代假数据）
        agent_recent_events = {}
        try:
            for atype in ["vod", "issue", "coding", "qa", "sre"]:
                c.execute(
                    "SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key=? AND agent_type=? ORDER BY seq DESC LIMIT 2",
                    (repo_key, atype)
                )
                agent_recent_events[atype] = [
                    {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                     "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                    for r in c.fetchall()
                ]
        except Exception:
            pass

    agent_types_present = {a["agent_type"] for a in agent_sessions}
    all_types = ["vod", "issue", "coding", "qa", "sre"]
    for atype in all_types:
        if atype not in agent_types_present:
            from src.api.session_store import get_agent_type_enable
            agent_sessions.append({
                "agent_type": atype,
                "backend": "",  # 无绑定/未建会话：不写硬编码默认（engine-selection-design.md v2）
                "status": "",
                "last_active_at": "",
                "enable": bool(get_agent_type_enable(atype)),
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

    # 获取当前场景和场景列表
    dev_scene = "default"
    try:
        with db_lock:
            c.execute("SELECT dev_scene FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                dev_scene = row[0]
    except Exception:
        pass

    scenes = []
    try:
        from src.api.routes.scenes import _load_scenes
        for sid, s in _load_scenes().items():
            scenes.append({"id": sid, "name": s.get("name", sid)})
    except Exception:
        pass

    return {
        "repo": repo_key,
        "role": role,
        "dev_scene": dev_scene,
        "_scenes": scenes,
        "metrics": {
            "total_events": len(recent_events),
            "active_agents": sum(1 for a in agent_sessions if a["status"] == "running"),
            "pending_events": event_counts["pending"],
            "failed_events": event_counts["failed"],
        },
        "agents": agent_sessions,
        "agent_event_counts": agent_event_counts,
        "agent_recent_events": agent_recent_events,
        "activity": recent_events,
    }

