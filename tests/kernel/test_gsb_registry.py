"""GSB 服务注册表单元测试（Issue #699 Phase 5 / docs/weekly-09 §7.8.3）.

覆盖：注册/发现/心跳超时/注销/全量视图/过期收敛。
测试库由 tests/conftest.py 强制切到 SQLite 临时库，不触碰生产 MySQL。
"""
from __future__ import annotations

import time

import pytest

from src.kernel import session_manager
from src.kernel.gsb import registry


def _reset_registry() -> None:
    """清空注册表，保证用例间互不影响。"""
    registry._ensure_table()
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute("DELETE FROM gsb_service_registry")
        session_manager.db.commit()


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_registry()
    yield
    _reset_registry()


def test_register_then_discover_roundtrip():
    registry.register_service(
        "unit-mgmt",
        "http://127.0.0.1:38081",
        health_url="/api/ping",
        metadata={"role": "mgmt", "pid": 123},
        version="2.2.0",
    )

    svc = registry.discover_service("unit-mgmt")
    assert svc["base_url"] == "http://127.0.0.1:38081"
    assert svc["health_url"] == "/api/ping"
    assert svc["version"] == "2.2.0"
    assert svc["status"] == "online"
    # metadata 以 JSON 落库
    assert '"role": "mgmt"' in svc["metadata"]

    assert [s["service_name"] for s in registry.list_services()] == ["unit-mgmt"]


def test_register_is_idempotent_replace():
    registry.register_service("unit-runtime", "http://127.0.0.1:38083")
    registry.register_service("unit-runtime", "http://127.0.0.1:39083")

    rows = registry.list_all_services()
    assert len(rows) == 1
    assert rows[0]["base_url"] == "http://127.0.0.1:39083"


def test_heartbeat_extends_liveness(monkeypatch):
    registry.register_service("unit-event", "http://127.0.0.1:38082")

    # 人为把心跳推到 100s 前（超过 90s 超时）
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET last_heartbeat=? WHERE service_name=?",
            (time.time() - 100, "unit-event"),
        )
        session_manager.db.commit()

    assert registry.discover_service("unit-event") == {}
    assert registry.list_services() == []

    # 心跳后重新可见
    registry.heartbeat_service("unit-event")
    assert registry.discover_service("unit-event")["service_name"] == "unit-event"
    assert len(registry.list_services()) == 1


def test_list_all_services_marks_stale_rows_not_alive():
    registry.register_service("unit-insight", "http://127.0.0.1:38084")
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET last_heartbeat=? WHERE service_name=?",
            (time.time() - 500, "unit-insight"),
        )
        session_manager.db.commit()

    rows = registry.list_all_services()
    assert len(rows) == 1
    assert rows[0]["alive"] is False
    assert rows[0]["age_seconds"] >= 500
    assert rows[0]["status"] == "online"  # 未收敛前仍标记 online，由 purge 处理


def test_purge_stale_marks_offline():
    registry.register_service("unit-old", "http://127.0.0.1:38999")
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET last_heartbeat=? WHERE service_name=?",
            (time.time() - 999, "unit-old"),
        )
        session_manager.db.commit()

    assert registry.purge_stale() == 1
    rows = registry.list_all_services()
    assert rows[0]["status"] == "offline"
    assert rows[0]["alive"] is False
    assert registry.discover_service("unit-old") == {}


def test_deregister_marks_offline_but_keeps_row():
    registry.register_service("unit-worker", "http://127.0.0.1:38083")
    registry.deregister_service("unit-worker")

    assert registry.discover_service("unit-worker") == {}
    rows = registry.list_all_services()
    assert len(rows) == 1
    assert rows[0]["status"] == "offline"
    assert rows[0]["base_url"] == "http://127.0.0.1:38083"


def test_discover_unknown_service_returns_empty():
    assert registry.discover_service("nope") == {}
    assert registry.list_services() == []

def test_ensure_table_adds_version_column_to_legacy_table():
    """旧库升级：早期建表没有 version 列，_ensure_table 必须补列后注册才不失败。

    回归背景：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，而 register_service
    的 INSERT 显式写 version → 旧库上注册会一直被 try/except 吞掉重试。
    """
    with session_manager.db_lock:
        c = session_manager.db.cursor()
        c.execute("DROP TABLE IF EXISTS gsb_service_registry")
        c.execute(
            """
            CREATE TABLE gsb_service_registry (
                service_name   TEXT PRIMARY KEY,
                base_url       TEXT NOT NULL,
                health_url     TEXT,
                status         TEXT DEFAULT 'online',
                last_heartbeat REAL DEFAULT 0,
                metadata       TEXT DEFAULT '{}'
            )
            """
        )
        session_manager.db.commit()

    registry.register_service(
        "unit-legacy", "http://127.0.0.1:38081", version="2.2.0"
    )

    svc = registry.discover_service("unit-legacy")
    assert svc["version"] == "2.2.0"
    assert svc["base_url"] == "http://127.0.0.1:38081"
