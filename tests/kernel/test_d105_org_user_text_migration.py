"""D105 第四批用户引用列 TEXT 化迁移锁（202611150015）。

背景：org_context.owner_id/created_by/updated_by、org_dispatch.created_by、
agent_shared_knowledge.owner_id、managed_repos.linked_by 均存 user id；D105 后
用户主键为 uuid7 字符串，INTEGER 亲和会混型不命中（SQLite）/严格模式写崩（MySQL）。

口径：
- 存量 int 值 CAST 保值（5 → '5'）；
- 哨兵 0 → ''（''=未指定，前端显示 —）；
- 二次执行零改动（幂等）；表/列不存在逐项跳过。
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.api.db_setup import (  # noqa: E402
    _ORG_USER_TEXT_COLS,
    _normalize_org_user_text_sqlite,
)


def _decl(c, table: str, col: str) -> str:
    return next((r[2] or "" for r in c.execute(f"PRAGMA table_info({table})")
                 if r[1] == col), "")


def _legacy_db(tmp_path):
    db = sqlite3.connect(str(tmp_path / "legacy_org_user.db"))
    c = db.cursor()
    c.execute("""
        CREATE TABLE grape_t_org_context (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'brief',
            ctx_key TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            lifecycle_status TEXT NOT NULL DEFAULT 'draft',
            version INTEGER NOT NULL DEFAULT 1,
            owner_id INTEGER NOT NULL DEFAULT 0,
            valid_until TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL DEFAULT 0,
            updated_by INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )""")
    c.execute("""
        CREATE TABLE grape_t_org_dispatch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL DEFAULT 0
        )""")
    c.execute("""
        CREATE TABLE agent_shared_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT NOT NULL,
            owner_id INTEGER NOT NULL DEFAULT 0
        )""")
    c.execute("""
        CREATE TABLE managed_repos (
            repo_key TEXT PRIMARY KEY,
            linked_by INTEGER DEFAULT 0
        )""")
    c.execute("INSERT INTO grape_t_org_context (owner_id, created_by, updated_by) "
              "VALUES (5, 5, 0)")
    c.execute("INSERT INTO grape_t_org_dispatch (created_by) VALUES ('01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67')")
    c.execute("INSERT INTO agent_shared_knowledge (repo_key, owner_id) VALUES ('r', 0)")
    c.execute("INSERT INTO managed_repos (repo_key, linked_by) VALUES ('r', 7)")
    db.commit()
    return db, c


def test_rebuilds_legacy_integer_user_columns(tmp_path):
    db, c = _legacy_db(tmp_path)
    assert _normalize_org_user_text_sqlite(db) == -1
    db.commit()

    for table, col in _ORG_USER_TEXT_COLS:
        assert _decl(c, table, col).upper() == "TEXT", f"{table}.{col}"

    ctx = c.execute("SELECT owner_id, created_by, updated_by FROM grape_t_org_context").fetchone()
    assert ctx == ("5", "5", "")
    assert c.execute("SELECT created_by FROM grape_t_org_dispatch").fetchone()[0] == \
        "01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67"
    assert c.execute("SELECT owner_id FROM agent_shared_knowledge").fetchone()[0] == ""
    assert c.execute("SELECT linked_by FROM managed_repos").fetchone()[0] == "7"


def test_idempotent_second_run_zero_change(tmp_path):
    db, _c = _legacy_db(tmp_path)
    _normalize_org_user_text_sqlite(db)
    db.commit()
    assert _normalize_org_user_text_sqlite(db) == 0
    db.commit()


def test_missing_tables_skipped(tmp_path):
    db = sqlite3.connect(str(tmp_path / "empty.db"))
    assert _normalize_org_user_text_sqlite(db) == 0