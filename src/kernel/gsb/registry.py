"""Grape Service Bus — 服务注册表（DB 驱动，零外部依赖）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.3.

每个域进程启动时注册自己到 gsb_service_registry 表，30s 心跳更新，
关闭时注销。其他域通过 discover_service() 发现服务地址。

注册表表名不走 TABLE_REGISTRY（GSB 基础设施表，不参与分 Schema 前缀）。

2026-11 补齐：新增 list_all_services()（运维全量视图，含离线/过期行）与
purge_stale()（把心跳超时的 online 行标为 offline），供 /internal/gsb/services
与巡检脚本使用；register/discover 行为保持设计稿语义不变。
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


def _rows_to_dicts(c, rows: list) -> list[dict]:
    """统一把 cursor 结果转 dict（SQLite Row 工厂 / MySQL DictCursor 均可）。"""
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    cols = [d[0] for d in c.description] if c.description else []
    return [dict(zip(cols, r)) for r in rows] if cols else []


def register_service(
    name: str,
    base_url: str,
    health_url: str = "/api/health",
    metadata: dict | None = None,
    version: str | None = None,
) -> None:
    """服务启动时注册自己。

    Args:
        name: 服务名（mgmt / event / runtime / insight / future-service）
        base_url: 服务基础 URL（如 http://127.0.0.1:38081）
        health_url: 健康检查路径（默认 /api/health）
        metadata: 额外元数据（role, port, pid, started_at, ...）
        version: 服务版本（写入 version 列，便于灰度期间比对）
    """
    _ensure_table()
    payload = json.dumps(metadata or {}, ensure_ascii=False)
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO gsb_service_registry "
            "(service_name, base_url, health_url, version, status, last_heartbeat, metadata) "
            "VALUES (?, ?, ?, ?, 'online', ?, ?)",
            (name, base_url, health_url, version or "1", time.time(), payload),
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
        return _rows_to_dicts(c, c.fetchall())


def list_all_services(include_offline: bool = True) -> list:
    """列出注册表全部行（运维视角，含离线与心跳过期行）。

    与 list_services() 的区别：不做心跳过滤，额外返回 age_seconds / alive
    两个派生字段，方便一眼看出"注册了但已经死掉"的服务。
    """
    _ensure_table()
    where = "" if include_offline else " WHERE status='online'"
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "SELECT * FROM gsb_service_registry" + where + " ORDER BY service_name"
        )
        rows = _rows_to_dicts(c, c.fetchall())

    now = time.time()
    for r in rows:
        age = max(0.0, now - float(r.get("last_heartbeat") or 0))
        r["age_seconds"] = round(age, 1)
        r["alive"] = bool(r.get("status") == "online" and age <= HEARTBEAT_TIMEOUT)
    return rows


def purge_stale(ttl: float = HEARTBEAT_TIMEOUT) -> int:
    """把心跳超时的 online 行标记为 offline，返回受影响行数。

    仅做状态收敛，不删除行——保留 base_url / metadata 便于事后排查。
    """
    _ensure_table()
    cutoff = time.time() - ttl
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET status='offline' "
            "WHERE status='online' AND last_heartbeat < ?",
            (cutoff,),
        )
        affected = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        session_manager.db.commit()
    return affected


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