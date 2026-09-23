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
    """admin 库连接配置：ADMIN_DB_* 环境变量优先，其次复用 DEVOPS_DB_* 或 configs/db-backend.json。"""
    import json as _json
    # 先从 configs/db-backend.json 读基础配置
    _cfg_host = "127.0.0.1"
    _cfg_port = 3306
    _cfg_user = "root"
    _cfg_password = ""
    try:
        _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "db-backend.json")
        with open(_cfg_path) as f:
            _db_cfg = _json.load(f)
            _mysql = _db_cfg.get("mysql", {})
            _cfg_host = _mysql.get("host", _cfg_host)
            _cfg_port = _mysql.get("port", _cfg_port)
            _cfg_user = _mysql.get("user", _cfg_user)
            _cfg_password = _mysql.get("password", _cfg_password)
    except Exception:
        pass
    # 环境变量优先覆盖
    host = os.environ.get("ADMIN_DB_HOST", os.environ.get("DEVOPS_DB_HOST", _cfg_host))
    port = int(os.environ.get("ADMIN_DB_PORT", os.environ.get("DEVOPS_DB_PORT", str(_cfg_port))))
    user = os.environ.get("ADMIN_DB_USER", os.environ.get("DEVOPS_DB_USER", _cfg_user))
    password = os.environ.get("ADMIN_DB_PASSWORD", os.environ.get("DEVOPS_DB_PASSWORD", _cfg_password))
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
