"""系统健康检查端点——分两层：免鉴权基本检查 + 需登录完整状态。"""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends

from src.api.deps import get_current_user
from src.config import OBS_ENABLED, OBS_BUCKET, log

router = APIRouter()


def _check_db() -> dict:
    """SELECT 1 检查数据库连通性。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1")
            c.fetchone()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


def _check_obs() -> dict:
    """检查 OBS 配置和 Storage 实例。"""
    if not OBS_ENABLED:
        return {"enabled": False, "reachable": False}
    try:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            return {"enabled": True, "reachable": True, "bucket": OBS_BUCKET}
        return {"enabled": True, "reachable": False, "error": "storage instance is None"}
    except Exception as e:
        return {"enabled": True, "reachable": False, "error": str(e)[:200]}


def _check_workers(user: dict, repo_key: str = "") -> dict:
    """查 agent_runtimes 表统计 Worker——按三级分类（全局/项目/个人）。

    user: 当前登录用户
    repo_key: 当前项目 repo_key（前端传入）
    """
    try:
        from src.session_manager import db, db_lock
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

        # 全局 Worker：scope=global OR scope=admin
        global_stats = _stats("scope IN ('global', 'admin')", [])

        # 项目 Worker：scope=repo AND repo_key=当前项目
        project_stats = _stats("scope='repo' AND repo_key=%s", [repo_key]) if repo_key else \
            {"total": 0, "online": 0, "offline": 0, "busy_slots": 0, "max_concurrent": 0}

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
    except Exception as e:
        return {
            "global": {"total": 0, "online": 0, "offline": 0, "busy_slots": 0, "max_concurrent": 0},
            "project": {"total": 0, "online": 0, "offline": 0, "busy_slots": 0, "max_concurrent": 0},
            "personal": {"total": 0, "online": 0, "offline": 0, "busy_slots": 0, "max_concurrent": 0},
            "error": str(e)[:200],
        }


@router.get("/api/health")
async def health():
    """免鉴权健康端点——只返回服务是否在线。

    供 K8s livenessProbe / nginx 健康检查 / 外部监控工具使用。
    不查数据库、不暴露系统规模。
    """
    return {"server": "ok"}


@router.get("/api/health/detail")
async def health_detail(repo_key: str = "", user: dict = Depends(get_current_user)):
    """需登录健康端点——返回完整系统状态。

    供前端系统监控页面使用。包含数据库、OBS、Worker 详情。
    Worker 按三级分类：全局/项目/个人。
    """
    from src.repo_key import to_repo_key
    full_repo_key = to_repo_key(repo_key) if repo_key and ":" not in repo_key else repo_key
    return {
        "server": "ok",
        "database": _check_db(),
        "obs": _check_obs(),
        "workers": _check_workers(user, full_repo_key),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
