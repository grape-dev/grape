"""收件箱主键必须是客户端 UUID（A12），且历史自增表能就地迁移。

回归背景：id 是对外投递句柄（claim/renew/finish 全按它定位，入队响应也返回它），
用 INTEGER AUTOINCREMENT 在搬迁/回绕后不再唯一；归档表已去自增，主表漏项。
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading

import pytest

from src.domains.runtime import webhook_inbox as wi

HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture()
def sqlite_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    monkeypatch.setattr(wi, "db", conn, raising=False)
    monkeypatch.setattr(wi, "db_lock", threading.RLock(), raising=False)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    yield conn
    conn.close()
    os.unlink(path)


def test_ddl_has_no_autoincrement():
    from src.domains.runtime.schema_ddl import (
        _WEBHOOK_INBOX_ARCHIVE_DDL,
        _WEBHOOK_INBOX_DDL,
    )

    for ddl in (_WEBHOOK_INBOX_DDL, _WEBHOOK_INBOX_ARCHIVE_DDL):
        assert "AUTOINCREMENT" not in ddl.upper()
    assert "id VARCHAR(64) PRIMARY KEY" in _WEBHOOK_INBOX_DDL


def test_enqueue_returns_client_generated_uuid(sqlite_db):
    assert wi.ensure_webhook_inbox_table() is True
    res = wi.enqueue_webhook(repo_key="gitlab:group-a/repo-a", provider="gitlab",
                             event_header="Push Hook", body=b"{}",
                             headers={"x-gitlab-event": "Push Hook"}, delivery_id="d-1")
    assert HEX32.match(str(res["id"])), res
    # 第二次入队拿到不同 id（全局唯一，不靠自增）
    res2 = wi.enqueue_webhook(repo_key="gitlab:group-a/repo-b", provider="gitlab",
                              event_header="Push Hook", body=b"{}",
                              headers={"x-gitlab-event": "Push Hook"}, delivery_id="d-2")
    assert res2["id"] != res["id"]
    assert HEX32.match(str(res2["id"]))


def test_claim_and_finish_round_trip_on_uuid_id(sqlite_db):
    wi.ensure_webhook_inbox_table()
    enq = wi.enqueue_webhook(repo_key="gitlab:group-a/repo-a", provider="gitlab",
                             event_header="Push Hook", body=b"{}",
                             headers={"x-gitlab-event": "Push Hook"}, delivery_id="d-3")
    item = wi.claim_next("worker-1")
    assert item and str(item["id"]) == str(enq["id"])
    out = wi.finish_claim(item["id"], "worker-1", ok=True, error="")
    assert out["status"] == "consumed"


def test_duplicate_delivery_returns_existing_uuid(sqlite_db):
    wi.ensure_webhook_inbox_table()
    first = wi.enqueue_webhook(repo_key="gitlab:group-a/repo-a", provider="gitlab",
                               event_header="Push Hook", body=b"{}",
                               headers={"x-gitlab-event": "Push Hook"}, delivery_id="d-dup")
    again = wi.enqueue_webhook(repo_key="gitlab:group-a/repo-a", provider="gitlab",
                               event_header="Push Hook", body=b"{}",
                               headers={"x-gitlab-event": "Push Hook"}, delivery_id="d-dup")
    assert again["duplicate"] is True
    assert str(again["id"]) == str(first["id"])


def test_legacy_autoincrement_table_is_migrated(sqlite_db):
    """早期版本建出的 INTEGER AUTOINCREMENT 表，首次 ensure 时改写成 UUID。"""
    sqlite_db.execute(
        """
        CREATE TABLE grape_t_webhook_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key VARCHAR(255) NOT NULL, provider VARCHAR(64) NOT NULL DEFAULT '',
            delivery_id VARCHAR(128) NOT NULL, event_header VARCHAR(128) NOT NULL DEFAULT '',
            payload MEDIUMTEXT NOT NULL, headers TEXT NOT NULL DEFAULT '{}',
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
            claim_owner VARCHAR(128) NOT NULL DEFAULT '', claimed_at VARCHAR(32) NOT NULL DEFAULT '',
            lease_expires_at VARCHAR(32) NOT NULL DEFAULT '', next_attempt_at VARCHAR(32) NOT NULL DEFAULT '',
            last_error MEDIUMTEXT DEFAULT NULL, created_at TEXT, updated_at TEXT,
            finished_at VARCHAR(32) NOT NULL DEFAULT '', UNIQUE(repo_key, delivery_id)
        )""")
    sqlite_db.execute(
        "INSERT INTO grape_t_webhook_inbox (repo_key, delivery_id, payload, status, attempts,"
        " created_at, updated_at) VALUES ('gitlab:group-a/repo-a','d1','{}','dead',3,'2026-01-01','2026-01-01')")
    sqlite_db.commit()

    assert wi.migrate_inbox_ids_to_uuid() is True
    rows = sqlite_db.execute("SELECT id, delivery_id, status, attempts FROM grape_t_webhook_inbox").fetchall()
    assert len(rows) == 1
    assert HEX32.match(str(rows[0][0]))
    assert rows[0][1] == "d1" and rows[0][2] == "dead" and rows[0][3] == 3
    # 幂等：再跑一次不再改写
    assert wi.migrate_inbox_ids_to_uuid() is False


def test_ensure_on_fresh_table_does_not_migrate(sqlite_db):
    wi.ensure_webhook_inbox_table()
    assert wi.migrate_inbox_ids_to_uuid() is False

def test_claims_follow_fifo_order_not_uuid_order(sqlite_db):
    """UUID 主键不能把队列变成随机序：同仓事件必须按入队时间消费。"""
    wi.ensure_webhook_inbox_table()
    ids = []
    for i in range(6):
        r = wi.enqueue_webhook(repo_key=f"gitlab:group-a/repo-{i}", provider="gitlab",
                               event_header="Push Hook", body=b"{}", headers={},
                               delivery_id=f"fifo-{i}")
        ids.append(str(r["id"]))
        # 拉开 created_at，避免同秒并列导致断言依赖 id
        import time as _t
        _t.sleep(0.01)

    claimed = []
    while True:
        item = wi.claim_next(f"worker-{len(claimed)}")
        if not item:
            break
        claimed.append(str(item["id"]))
        wi.finish_claim(item["id"], f"worker-{len(claimed) - 1}", ok=True)

    assert claimed == ids, f"消费顺序应与入队顺序一致，实际 {claimed} != {ids}"
