"""迁移 202611250013（grape_t_autopilot_payload_guard UUID 主键）回归。

与 202611250012 同批（PR #2245 复审 SUP-1）：建新表 → 搬行（uuid5 确定性主键）→
DROP 被清理门槛拦截时 raise 不盖章 → 重试不重复。该表另有 UNIQUE(payload_hash)，
本文件同时断言两条幂等来源都成立。
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.migrations import payload_guard_uuid_pk as mig  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:issue2245/payload"


def _reset_tables():
    with db_lock:
        c = db.cursor()
        c.execute(f"DROP TABLE IF EXISTS {mig.NEW_TABLE}")
        c.execute(f"DROP TABLE IF EXISTS {mig.OLD_TABLE}")
        c.execute(f"""CREATE TABLE {mig.OLD_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload_hash TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            repo_key TEXT NOT NULL DEFAULT '',
            dispatch_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE (payload_hash)
        )""")
        db.commit()


def _seed(n: int = 2):
    with db_lock:
        c = db.cursor()
        for i in range(n):
            c.execute(
                f"INSERT INTO {mig.OLD_TABLE} (payload_hash, task_id, repo_key, dispatch_count,"
                " failure_count, status, reason, first_seen_at, last_seen_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"hash{i}", str(i), RK, 1, 1, "blocked", "same payload",
                 "2026-11-25T00:00:00+00:00", "2026-11-25T00:10:00+00:00",
                 "2026-11-25T00:10:00+00:00"))
        db.commit()


def _count(table: str) -> int:
    with db_lock:
        return int(db.cursor().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _tables():
    with db_lock:
        return {r[0] for r in db.cursor().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


@pytest.fixture(autouse=True)
def _clean():
    _reset_tables()
    yield
    _reset_tables()


def test_creates_table_and_moves_rows():
    _seed(2)
    moved = mig.migrate()
    assert moved == 2
    assert _count(mig.NEW_TABLE) == 2
    assert mig.OLD_TABLE not in _tables()
    import uuid as _uuid
    with db_lock:
        ids = {r[0] for r in db.cursor().execute(
            f"SELECT id FROM {mig.NEW_TABLE}").fetchall()}
    expected = {_uuid.uuid5(mig._ID_NAMESPACE, f"{mig.OLD_TABLE}:{i}").hex for i in (1, 2)}
    assert ids == expected


def test_no_old_table_still_creates_new(monkeypatch):
    assert mig.migrate() == 0
    assert mig.NEW_TABLE in _tables()
    assert _count(mig.NEW_TABLE) == 0


def test_drop_blocked_then_retry_is_idempotent(monkeypatch):
    _seed(2)
    from src.api import db_setup as ds

    monkeypatch.setattr(ds, "_destructive_ddl", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        mig.migrate()
    assert _count(mig.NEW_TABLE) == 2
    assert mig.OLD_TABLE in _tables()

    monkeypatch.undo()
    assert mig.migrate() == 0, "重试全部命中既有主键 → 不搬新行"
    assert _count(mig.NEW_TABLE) == 2
    assert mig.OLD_TABLE not in _tables()


def test_converged_detects_leftover_old_table():
    _seed(1)
    mig.migrate()
    with db_lock:
        c = db.cursor()
        c.execute(f"""CREATE TABLE {mig.OLD_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, payload_hash TEXT NOT NULL DEFAULT '')
        """)
        db.commit()
    with pytest.raises(RuntimeError):
        mig.converged()