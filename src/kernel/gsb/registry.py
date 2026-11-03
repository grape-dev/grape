"""Grape Service Bus — 服务注册表（DB 驱动，零外部依赖）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.3.

每个域进程启动时注册自己到 gsb_service_registry 表，30s 心跳更新，
关闭时注销。其他域通过 discover_service() 发现服务地址。

注册表表名不走 TABLE_REGISTRY（GSB 基础设施表，不参与分 Schema 前缀）。
"""
from __future__ import annotations

import json
import time

from src.kernel import session_manager

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS gsb_service_registry (
    service_name   TEXT PRIMARY KEY,
    base_url       TEXT NOT NULL,
    health_url     TEXT,
    version        TEXT DEFAULT '1',
    status         TEXT DEFAULT 'online',
    last_heartbeat REAL DEFAULT 0,
    metadata       TEXT DEFAULT '{}'
)
"""

# 心跳超时：90s = 3 个心跳周期（30s × 3）
HEARTBEAT_TIMEOUT = 90


def _ensure_table():
    """幂等建表（每次 register 时调用，CREATE IF NOT EXISTS）。"""
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(_REGISTRY_DDL)
        session_manager.db.commit()


def register_service(
    name: str,
    base_url: str,
    health_url: str = "/api/health",
    metadata: dict | None = None,
) -> None:
    """服务启动时注册自己。

    Args:
        name: 服务名（mgmt / runtime / insight / future-service）
        base_url: 服务基础 URL（如 http://localhost:38080）
        health_url: 健康检查路径（默认 /api/health）
        metadata: 额外元数据（description, endpoints, ...）
    """
    _ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO gsb_service_registry "
            "(service_name, base_url, health_url, status, last_heartbeat, metadata) "
            "VALUES (?, ?, ?, 'online', ?, ?)",
            (name, base_url, health_url, time.time(), json.dumps(metadata or {})),
        )
        session_manager.db.commit()


def heartbeat_service(name: str) -> None:
    """定时心跳更新（30s 调用一次）。"""
    _ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET last_heartbeat=?, status='online' "
            "WHERE service_name=?",
            (time.time(), name),
        )
        session_manager.db.commit()


def discover_service(name: str) -> dict:
    """发现服务地址——带健康检查（心跳超时视为离线）。

    Returns:
        服务行 dict（service_name, base_url, health_url, ...）或空 dict。
    """
    _ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "SELECT * FROM gsb_service_registry "
            "WHERE service_name=? AND status='online'",
            (name,),
        )
        row = c.fetchone()
        if not row:
            return {}
        data = dict(row) if hasattr(row, "keys") else {}
        if not data:
            # SQLite Row factory may not be enabled; fall back
            cols = [d[0] for d in c.description] if c.description else []
            data = dict(zip(cols, row)) if row else {}
        # 心跳超时检查
        if time.time() - float(data.get("last_heartbeat", 0)) > HEARTBEAT_TIMEOUT:
            return {}
        return data


def list_services() -> list:
    """列出所有在线服务（心跳未超时）。"""
    _ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "SELECT * FROM gsb_service_registry "
            "WHERE status='online' AND last_heartbeat > ? "
            "ORDER BY service_name",
            (time.time() - HEARTBEAT_TIMEOUT,),
        )
        rows = c.fetchall()
        if rows and hasattr(rows[0], "keys"):
            return [dict(r) for r in rows]
        # Fallback: manually build dicts
        cols = [d[0] for d in c.description] if c.description else []
        return [dict(zip(cols, r)) for r in rows] if cols else []


def deregister_service(name: str) -> None:
    """服务关闭时注销。"""
    _ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET status='offline' WHERE service_name=?",
            (name,),
        )
        session_manager.db.commit()
