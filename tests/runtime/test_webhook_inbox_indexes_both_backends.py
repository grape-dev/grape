"""收件箱索引必须在两个后端都真的建出来（评审 B1）。

回归背景：索引 SQL 曾只按 MySQL 写（information_schema 探查 + created_at(20) 前缀），
SQLite 下两句都执行不了、异常被降级成 warning，于是单库/SQLite 环境索引永久缺失，
purge 与排障仍全表扫描。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading

import pytest

from src.domains.runtime import webhook_inbox as wi

INDEXES = {
    "grape_t_webhook_inbox": "idx_webhook_inbox_status_created",
    "grape_t_webhook_inbox_archive": "idx_webhook_inbox_archived",
}


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


def _sqlite_indexes(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)).fetchall()
    return {r[0] for r in rows}


def test_sqlite_backend_creates_indexes(sqlite_db):
    assert wi.ensure_webhook_inbox_table() is True
    for table, index in INDEXES.items():
        assert index in _sqlite_indexes(sqlite_db, table), f"{table} 缺索引 {index}"


def test_sqlite_index_creation_is_idempotent(sqlite_db):
    assert wi.ensure_webhook_inbox_table() is True
    assert wi.ensure_webhook_inbox_table() is True


def test_sqlite_index_columns_are_as_declared(sqlite_db):
    wi.ensure_webhook_inbox_table()
    sql = sqlite_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (INDEXES["grape_t_webhook_inbox"],)).fetchone()[0]
    assert "status" in sql and "created_at" in sql


def test_mysql_declaration_keeps_prefix_index():
    """MySQL 侧仍要带前缀长度（TEXT 列），不能因修 SQLite 而退化。"""
    from src.domains.runtime.schema_ddl import _WEBHOOK_INBOX_INDEXES

    by_name = {row[1]: row for row in _WEBHOOK_INBOX_INDEXES}
    assert by_name["idx_webhook_inbox_status_created"][2] == "(status, created_at(20))"
    assert by_name["idx_webhook_inbox_status_created"][3] == "(status, created_at)"