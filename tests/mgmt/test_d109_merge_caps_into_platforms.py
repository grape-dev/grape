"""202611170010（凭据能力表并入平台表）的行为守卫。

实锤背景（2026-11-14 线上 MySQL）：该迁移已盖章，但 grape_t_repo_platforms 一个列都没建、
grape_t_repo_credential_caps 也还在；ORM 每次读平台表都抛 Unknown column，管理面静默退回
文件/meta 兜底。本用例在 sqlite 上跑真实 fn，锁三件事：

  1. 5 个存活字段真的进了平台表；
  2. 值从 caps 表回填（不是留空）；
  3. caps 表被删；
  4. 复检不通过时必须抛（否则又会「空转盖章」）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLS = ["pat_create_url", "token_storage_field", "webhook_token_field", "bot_username", "owner_fallback"]


class _FakeDB:
    """把 sqlite 连接包成 db_setup 里 db 的最小接口（cursor/commit）。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()


def _seed(tmp_path):
    conn = sqlite3.connect(tmp_path / "mgmt.db")
    c = conn.cursor()
    c.execute(
        "CREATE TABLE grape_t_repo_platforms (id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
        " name TEXT, enabled INTEGER NOT NULL DEFAULT 1)"
    )
    c.execute("INSERT INTO grape_t_repo_platforms (id, provider, name) VALUES ('1','gitee','Gitee')")
    c.execute(
        "CREATE TABLE grape_t_repo_credential_caps (id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
        " owner_fallback INTEGER DEFAULT 0, pat_create_url TEXT DEFAULT '',"
        " token_storage_field TEXT DEFAULT '', webhook_token_field TEXT DEFAULT '',"
        " bot_username TEXT DEFAULT '')"
    )
    c.execute(
        "INSERT INTO grape_t_repo_credential_caps (id, provider, owner_fallback, pat_create_url,"
        " token_storage_field, webhook_token_field, bot_username) VALUES"
        " ('c1','gitee',0,'https://gitee.com/profile/personal_access_tokens','','','bot-x')"
    )
    conn.commit()
    return conn


@pytest.fixture()
def fake_db(monkeypatch, tmp_path):
    conn = _seed(tmp_path)
    fake = _FakeDB(conn)
    import src.api.db_setup as ds

    monkeypatch.setattr(ds, "get_db_backend", lambda: "sqlite", raising=False)
    import src.kernel.mysql_compat as mc

    monkeypatch.setattr(mc, "get_db_backend", lambda: "sqlite")
    import src.kernel.session_manager as sm

    monkeypatch.setattr(sm, "db", fake, raising=False)
    monkeypatch.setattr(sm, "db_lock", __import__("threading").RLock(), raising=False)
    monkeypatch.setattr(ds, "_destructive_ddl", lambda c, sql, version="": (c.execute(sql), True)[1], raising=False)
    yield conn
    conn.close()


def test_merge_caps_into_platforms_backfills_and_drops(fake_db):
    import src.api.db_setup as ds

    ds._fn_merge_credential_caps_into_platforms()

    c = fake_db.cursor()
    have = {r[1] for r in c.execute("PRAGMA table_info(grape_t_repo_platforms)").fetchall()}
    for col in COLS:
        assert col in have, f"平台表缺列 {col}"
    row = c.execute(
        "SELECT pat_create_url, bot_username, owner_fallback FROM grape_t_repo_platforms WHERE provider='gitee'"
    ).fetchone()
    assert row[0] == "https://gitee.com/profile/personal_access_tokens"
    assert row[1] == "bot-x"
    assert row[2] == 0
    caps = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='grape_t_repo_credential_caps'").fetchone()
    assert not caps, "caps 表未被删除"


def test_merge_caps_into_platforms_raises_when_ddl_noop(fake_db, monkeypatch):
    """DDL 空转（列没真建）时必须抛，不能把「没做」盖成「已做」。"""
    import src.api.db_setup as ds

    real_alter = str

    class _NoopCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("ALTER TABLE"):
                return self._cur.execute("SELECT 1")  # 什么都不做
            return self._cur.execute(sql, *a)

        def fetchall(self):
            return self._cur.fetchall()

        def fetchone(self):
            return self._cur.fetchone()

    class _NoopDB(_FakeDB):
        def cursor(self):
            return _NoopCursor(self._conn.cursor())

    import src.kernel.session_manager as sm

    monkeypatch.setattr(sm, "db", _NoopDB(fake_db), raising=False)
    # DDL 空转 → 要么复检不通过（RuntimeError），要么回填写到不存在的列
    # （OperationalError）；两者都等于"fn 抛错"，执行器不盖章、下次重试。
    with pytest.raises(Exception) as ei:
        ds._fn_merge_credential_caps_into_platforms()
    msg = str(ei.value)
    assert ("复检未通过" in msg) or ("no such column" in msg.lower()), msg