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


def _check_workers() -> dict:
    """查 agent_runtimes 表统计 Worker 在线/离线/槽位。"""
    try:
        from src.session_manager import db, db_lock
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
        total = row[0] or 0
        online = row[1] or 0
        offline = row[2] or 0
        busy = row[3] or 0
        max_conc = row[4] or 0
        return {
            "total": total,
            "online": online,
            "offline": offline,
            "busy_slots": busy,
            "max_concurrent": max_conc,
        }
    except Exception as e:
        return {"total": 0, "online": 0, "offline": 0, "busy_slots": 0, "max_concurrent": 0, "error": str(e)[:200]}


@router.get("/api/health")
async def health():
    """免鉴权健康端点——只返回服务是否在线。

    供 K8s livenessProbe / nginx 健康检查 / 外部监控工具使用。
    不查数据库、不暴露系统规模。
    """
    return {"server": "ok"}


@router.get("/api/health/detail")
async def health_detail(user: dict = Depends(get_current_user)):
    """需登录健康端点——返回完整系统状态。

    供前端系统监控页面使用。包含数据库、OBS、Worker 详情。
    """
    return {
        "server": "ok",
        "database": _check_db(),
        "obs": _check_obs(),
        "workers": _check_workers(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
