"""Issue #4445：tmp janitor 心跳计数列必须走注册式迁移。

与 #3773 同型风险：``runtime_credentials`` 的 2 列写在心跳 UPDATE 的 SET 列表里，
列不存在即 1054 Unknown column，整条心跳 UPDATE 失败被吞 → 心跳与全部指标断掉。
"""
import sqlite3

import pytest

from src.api import db_setup as ds

MIG_VERSION = "202611250014"
MIG_NAME = "issue4445-tmp-janitor-metrics-columns"
TABLE = "runtime_credentials"
COLUMNS = (
    "tmp_janitor_removed_items_total",
    "tmp_janitor_removed_bytes_total",
)


@pytest.fixture
def stamped_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE runtime_credentials ("
        "id INTEGER PRIMARY KEY, token_hash TEXT, name TEXT, cpu_percent REAL DEFAULT 0)")
    conn.execute(
        "INSERT INTO runtime_credentials (token_hash, name) VALUES ('h', 'worker-1')")
    conn.execute(
        "CREATE TABLE grape_t_schema_migrations ("
        "version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT, "
        "duration_ms INTEGER DEFAULT 0, checksum TEXT DEFAULT '')")
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version,name,kind,applied_at) "
        "VALUES ('202610300001','legacy-run-all-migrations','legacy-stamp','2026-10-30 00:00:00')")
    conn.commit()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    yield conn
    conn.close()


def _mig():
    return {m.version: m for m in ds._MIGRATIONS}[MIG_VERSION]


def _columns(conn):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}


def _stamped(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?",
        (MIG_VERSION,)).fetchone()[0]


def test_registered_safe_with_post_check():
    mig = _mig()
    assert mig.name == MIG_NAME
    assert mig.kind == "safe"
    assert callable(mig.fn)
    assert callable(mig.post_check)
    assert tuple(mig.domains) == ("runtime",)


def test_stamped_db_gets_columns_and_is_stamped(stamped_db):
    assert not (set(COLUMNS) & _columns(stamped_db))
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert set(COLUMNS) <= _columns(stamped_db)
    assert _stamped(stamped_db) == 1
    row = stamped_db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM {TABLE}").fetchone()
    assert row[0] in (0, 0.0) and row[1] in (0, 0.0)


def test_second_run_is_idempotent(stamped_db):
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert _stamped(stamped_db) == 1
    assert set(COLUMNS) <= _columns(stamped_db)


def test_no_stamp_when_columns_partially_missing(stamped_db, monkeypatch):
    real = ds._ensure_column

    def _only_first(c, tbl, col, ddl, **kw):
        if col == COLUMNS[0]:
            return real(c, tbl, col, ddl, **kw)
        return None

    monkeypatch.setattr(ds, "_ensure_column", _only_first)
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    present = _columns(stamped_db)
    assert COLUMNS[0] in present
    assert COLUMNS[1] not in present
    assert _stamped(stamped_db) == 0