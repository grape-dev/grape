"""Issue #3773: ACP 分流 / v2 心跳落库 / metrics 采样链路回归。"""
import asyncio
import sqlite3
import threading

import pytest

import src.domains.runtime.routes.runtime as rt

UID_A = "11111111-1111-5111-8111-111111111111"


class _Req:
    headers = {}
    client = None


@pytest.fixture()
def rt_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "rt.db"), check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE runtime_credentials (
            id INTEGER PRIMARY KEY,
            last_seen_at TEXT, last_ip TEXT,
            cpu_percent REAL DEFAULT 0, memory_percent REAL DEFAULT 0,
            disk_percent REAL DEFAULT 0, tmp_percent REAL DEFAULT 0,
            bandwidth_percent REAL DEFAULT 0, bandwidth_mbps REAL DEFAULT 0,
            load_avg TEXT DEFAULT '', spec TEXT DEFAULT '',
            acp_state_db_bytes REAL DEFAULT 0,
            acp_state_db_cleanup_counts TEXT DEFAULT '',
            acp_state_db_bytes_freed_total REAL DEFAULT 0,
            acp_resume_skipped_total INTEGER DEFAULT 0
        );
        CREATE TABLE agent_runtimes (
            id INTEGER PRIMARY KEY, credential_id INTEGER, name TEXT,
            engine_uid TEXT, status TEXT DEFAULT 'offline', last_seen_at TEXT,
            busy_slots INTEGER DEFAULT 0, scene_tags TEXT, runtime_info TEXT,
            role_tags TEXT, tags_user_set INTEGER DEFAULT 0
        );
        CREATE TABLE agent_tasks (
            id TEXT PRIMARY KEY, runtime_id INTEGER, status TEXT
        );
        """
    )
    conn.execute("INSERT INTO runtime_credentials (id) VALUES (7)")
    conn.execute(
        "INSERT INTO agent_runtimes (id, credential_id, name, engine_uid, status)"
        " VALUES (1, 7, 'eng-a', ?, 'offline')", (UID_A,))
    conn.commit()
    lock = threading.RLock()
    monkeypatch.setattr(rt, "db", conn, raising=True)
    monkeypatch.setattr(rt, "db_lock", lock, raising=True)
    return conn


def test_v2_heartbeat_persists_acp_metrics(rt_db):
    body = rt.HeartbeatBodyV2(
        runtime_ids=[1], engine_uids=[UID_A],
        acp_state_db_bytes=123456.0,
        acp_state_db_cleanup_counts='{"ok": 2}',
        acp_state_db_bytes_freed_total=999.0,
        acp_resume_skipped_total=3,
    )
    resp = asyncio.run(rt.runtime_heartbeat_v2(body, _Req(), credential_id=7))
    assert resp["engine_uids_resolved"] == 1
    row = rt_db.execute(
        "SELECT acp_state_db_bytes, acp_state_db_cleanup_counts,"
        " acp_state_db_bytes_freed_total, acp_resume_skipped_total"
        " FROM runtime_credentials WHERE id=7").fetchone()
    assert row == (123456.0, '{"ok": 2}', 999.0, 3), \
        "v2 心跳必须把 acp_* 透传到 v1 落库（否则 /metrics 恒 0）"


def test_collect_acp_state_db_metrics_uses_best_row(monkeypatch):
    from src.domains.runtime import public as rt_public
    from src.domains.insight import observability as obs

    rows = [
        ("hostA", "api", 0.0, "{}", 0.0, 0),
        ("hostA", "worker", 2048.0, '{"ok": 2, "forced": 1}', 900.0, 5),
    ]
    monkeypatch.setattr(rt_public, "read_all_any", lambda _name: rows)
    obs._acp_cleanup_results.clear()
    obs.collect_acp_state_db_metrics()
    assert obs.ACP_STATE_DB_BYTES.labels(host="hostA")._value.get() == 2048.0
    assert obs.ACP_STATE_DB_BYTES_FREED_TOTAL.labels(host="hostA")._value.get() == 900.0
    assert obs.ACP_RESUME_SKIPPED_TOTAL.labels(host="hostA")._value.get() == 5.0
    assert obs.ACP_STATE_DB_CLEANUP_TOTAL.labels(
        host="hostA", result="ok")._value.get() == 2.0
    assert obs.ACP_STATE_DB_CLEANUP_TOTAL.labels(
        host="hostA", result="forced")._value.get() == 1.0
