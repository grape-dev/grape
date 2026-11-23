"""收件箱队列指标必须反映真实状态词表与 ISO 时间戳（A13 可观测性）。

回归背景：生产者写 processing/failed，指标却查 claimed；created_at 是 ISO 字符串，
却按 float() 解析 —— 结果最老等待时长、租约过期、重试总量长期恒为 0。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from src.domains.runtime import webhook_inbox as wi


@pytest.fixture()
def inbox_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE grape_t_webhook_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT, provider TEXT, delivery_id TEXT UNIQUE,
            event_header TEXT, payload TEXT, headers TEXT,
            status TEXT, attempts INTEGER, max_attempts INTEGER,
            next_attempt_at TEXT, last_error TEXT,
            created_at TEXT, updated_at TEXT,
            claimed_at TEXT, claim_owner TEXT, lease_expires_at TEXT, finished_at TEXT
        )
        """)
    conn.commit()
    monkeypatch.setattr(wi, "db", conn, raising=False)
    monkeypatch.setattr(wi, "db_lock", __import__("threading").RLock(), raising=False)
    yield conn
    conn.close()
    os.unlink(path)


def _iso(days: float = 0, seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, seconds=seconds)).isoformat()


def _insert(conn, did, status, created_at, attempts=0, lease=""):
    now = _iso()
    conn.execute(
        "INSERT INTO grape_t_webhook_inbox (repo_key, provider, delivery_id, event_header,"
        " payload, headers, status, attempts, max_attempts, next_attempt_at, last_error,"
        " created_at, updated_at, claimed_at, claim_owner, lease_expires_at, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("gitlab:group-a/repo-a", "gitlab", did, "Push Hook", "{}", "{}", status,
         attempts, 5, "", "", created_at, now, "", "", lease, ""))
    conn.commit()


def test_metrics_use_real_status_vocabulary(inbox_db):
    _insert(inbox_db, "d1", "pending", _iso(days=2))
    _insert(inbox_db, "d2", "pending", _iso(seconds=30))
    _insert(inbox_db, "d3", "processing", _iso(seconds=60), attempts=1, lease=_iso(seconds=120))
    _insert(inbox_db, "d4", "failed", _iso(seconds=10), attempts=3)
    _insert(inbox_db, "d5", "dead", _iso(days=1), attempts=5)
    _insert(inbox_db, "d6", "consumed", _iso(days=1), attempts=1)

    m = wi.inbox_metrics()
    assert m["pending"] == 2
    assert m["processing"] == 1
    assert m["claimed"] == 1, "claimed 是 processing 的兼容别名"
    assert m["failed"] == 1
    assert m["dead"] == 1
    assert m["consumed"] == 1
    # 打开行 = pending + processing + failed
    assert m["open_rows"] == 4
    assert m["retries_total"] == 1 + 3
    # 已过期租约（lease 在两分钟前）
    assert m["lease_expired"] == 1


def test_oldest_pending_age_parses_iso_timestamp(inbox_db):
    _insert(inbox_db, "d1", "pending", _iso(days=2))
    m = wi.inbox_metrics()
    assert 2 * 86400 - 60 < m["oldest_pending_age_s"] < 2 * 86400 + 60


def test_oldest_pending_age_zero_when_queue_empty(inbox_db):
    assert wi.inbox_metrics()["oldest_pending_age_s"] == 0.0


def test_iso_to_epoch_tolerates_legacy_epoch_and_garbage():
    assert wi._iso_to_epoch(str(1700000000.0)) == 1700000000.0
    assert wi._iso_to_epoch(1700000000) == 1700000000.0
    assert wi._iso_to_epoch("") is None
    assert wi._iso_to_epoch("not-a-time") is None
    assert wi._iso_to_epoch(None) is None