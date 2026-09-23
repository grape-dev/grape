"""精选排序读取：从 admin 管理库的 admin_featured_config 表读取精选场景/技能的展示顺序。

admin 管理台通过 FeaturedService 将精选顺序持久化到 admin_featured_config 表
（type/item_id/sort_order）。grape 业务前端展示精选场景和精选技能时，
应按此排序返回，与管理台保持一致。

连接配置通过环境变量 ADMIN_DB_* 覆盖，默认与业务库同实例（DEVOPS_DB_*）。
连接失败或表不存在时静默降级——调用方回退到原有排序逻辑（like_count / updated_at）。
"""
import os
import threading
import logging

import pymysql

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn = None


def _admin_db_config() -> dict:
    """admin 库连接配置：ADMIN_DB_* 环境变量优先，其次复用 DEVOPS_DB_* 的 host/port/user/password。"""
    # 复用业务库的 host/port/user/password（两库通常在同一实例）
    host = os.environ.get("ADMIN_DB_HOST", os.environ.get("DEVOPS_DB_HOST", "127.0.0.1"))
    port = int(os.environ.get("ADMIN_DB_PORT", os.environ.get("DEVOPS_DB_PORT", "3306")))
    user = os.environ.get("ADMIN_DB_USER", os.environ.get("DEVOPS_DB_USER", "root"))
    password = os.environ.get("ADMIN_DB_PASSWORD", os.environ.get("DEVOPS_DB_PASSWORD", ""))
    database = os.environ.get("ADMIN_DB_NAME", "grape_admin_db")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "connect_timeout": 5,
    }


def _get_conn():
    """获取/复用 admin 库连接（线程安全，惰性创建）。"""
    global _conn
    if _conn is None:
        _conn = pymysql.connect(autocommit=True, **_admin_db_config())
    try:
        _conn.ping(reconnect=True)
    except Exception:
        try:
            _conn = pymysql.connect(autocommit=True, **_admin_db_config())
        except Exception:
            _conn = None
            raise
    return _conn


def get_featured_order(featured_type: str) -> list[int]:
    """读取指定类型的精选 item_id 有序列表（按 sort_order 升序）。

    Args:
        featured_type: 'scene' 或 'skill'

    Returns:
        有序 item_id 列表。连接失败或表不存在时返回空列表（调用方回退到原有排序）。
    """
    try:
        with _lock:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT item_id FROM admin_featured_config WHERE type = %s ORDER BY sort_order ASC",
                (featured_type,),
            )
            return [int(r[0]) for r in cur.fetchall()]
    except Exception as e:
        log.debug("读取精选排序失败 type=%s: %s", featured_type, e)
        return []
