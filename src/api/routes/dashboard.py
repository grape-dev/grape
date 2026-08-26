"""Dashboard API——系统级聚合统计端点（需登录 + 用户隔离）。"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_current_user
from src.session_manager import db, db_lock

router = APIRouter()

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


@router.get("/api/dashboard/runtime/status")
async def dashboard_runtime_status(user: dict = Depends(get_current_user)):
    """Worker 在线/离线/槽位统计。"""
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN status='online' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status!='online' THEN 1 ELSE 0 END), "
            "COALESCE(SUM(busy_slots), 0), "
            "COALESCE(SUM(max_concurrent), 0) "
            "FROM agent_runtimes"
        ).fetchone()
    return {
        "total": row[0] or 0,
        "online": row[1] or 0,
        "offline": row[2] or 0,
        "busy_slots": row[3] or 0,
        "max_concurrent": row[4] or 0,
    }


@router.get("/api/dashboard/task/daily")
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


@router.get("/api/dashboard/task/by-agent")
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


@router.get("/api/dashboard/failures/daily")
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


@router.get("/api/dashboard/failures/by-agent")
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
