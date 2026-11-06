"""Issue #1105：终态事件必须落 completed_at（终态⇔completed_at 不变量）。

覆盖：
- update_event_status：completed/cancelled 终态写入 completed_at（此前仅 SET status）
- update_event_complete：调用方传空 completed_at 时兜底赋当前时间
- _sync_issue_pipeline_terminal：admin-confirm 收敛 running→completed 补 completed_at
- 存量回填迁移：终态且 completed_at 为空的行取 created_at 回填，幂等可重跑
"""
import sqlite3
import os
import tempfile

import pytest

from src.session_manager import db as _db, db_lock as _db_lock


@pytest.fixture
def test_db(monkeypatch):
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    _conn = sqlite3.connect(_tmp.name, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("""
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT, issue_id TEXT, event_type TEXT, source TEXT,
            agent_type TEXT, title TEXT, actor TEXT, summary TEXT,
            payload TEXT, status TEXT, result TEXT,
            runtime TEXT, skill TEXT, exit_code INTEGER,
            duration REAL, agent_logs TEXT,
            started_at TEXT, completed_at TEXT, created_at TEXT
        )
    """)
    _conn.commit()
    monkeypatch.setattr("src.session_manager.db", _conn)
    monkeypatch.setattr("src.api.event_store.db", _conn)
    from src.session_manager import db_lock
    monkeypatch.setattr("src.api.event_store.db_lock", db_lock)
    yield _conn
    _conn.close()
    os.unlink(_tmp.name)


def _insert_event(conn, seq, status="running", created_at="2026-11-01 10:00:00"):
    conn.execute(
        "INSERT INTO events (seq, repo_key, issue_id, event_type, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (seq, "test/repo", "#1", "a2a_event", status, created_at)
    )
    conn.commit()


def _completed_at(conn, seq):
    row = conn.execute(
        "SELECT completed_at FROM events WHERE seq=?", (seq,)).fetchone()
    return row[0] if row else None


class TestUpdateEventStatusTerminalCompletedAt:
    """update_event_status 非 failed 终态必须写 completed_at。"""

    def test_running_to_completed_sets_completed_at(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 1101, status="running")
        update_event_status(1101, "completed")
        assert _completed_at(test_db, 1101), "#1105: completed 终态必须落 completed_at"

    def test_running_to_cancelled_sets_completed_at(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 1102, status="running")
        update_event_status(1102, "cancelled")
        assert _completed_at(test_db, 1102), "#1105: cancelled 终态必须落 completed_at"

    def test_existing_completed_at_not_overwritten(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 1103, status="running")
        test_db.execute(
            "UPDATE events SET completed_at='2026-11-01T10:00:00+00:00' WHERE seq=1103")
        test_db.commit()
        update_event_status(1103, "completed")
        assert _completed_at(test_db, 1103) == "2026-11-01T10:00:00+00:00", \
            "#1105: 已有 completed_at 不被覆盖"


class TestUpdateEventCompleteEmptyCompletedAt:
    """update_event_complete 传空 completed_at 时兜底赋值。"""

    def test_empty_completed_at_backfilled(self, test_db):
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 1201, status="running")
        update_event_complete(1201, "completed", exit_code=0, completed_at="", result="ok")
        assert _completed_at(test_db, 1201), "#1105: 空 completed_at 必须兜底赋值"

    def test_none_completed_at_backfilled(self, test_db):
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 1202, status="running")
        update_event_complete(1202, "failed", exit_code=1)  # completed_at 缺省 ""
        assert _completed_at(test_db, 1202), "#1105: 缺省 completed_at 必须兜底赋值"

    def test_explicit_completed_at_kept(self, test_db):
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 1203, status="running")
        update_event_complete(1203, "completed", completed_at="2026-11-02T09:00:00+00:00")
        assert _completed_at(test_db, 1203) == "2026-11-02T09:00:00+00:00"


class TestSyncIssuePipelineTerminalCompletedAt:
    """admin-confirm 收敛 running→completed 必须补 completed_at。"""

    def test_running_event_converged_with_completed_at(self, test_db):
        from src.api.routes.issues import _sync_issue_pipeline_terminal
        _insert_event(test_db, 1301, status="running")
        _sync_issue_pipeline_terminal("test/repo", 1)
        row = test_db.execute(
            "SELECT status, completed_at FROM events WHERE seq=1301").fetchone()
        assert row[0] == "completed"
        assert row[1], "#1105: 收敛为 completed 必须落 completed_at"

    def test_terminal_event_completed_at_untouched(self, test_db):
        from src.api.routes.issues import _sync_issue_pipeline_terminal
        _insert_event(test_db, 1302, status="completed")
        test_db.execute(
            "UPDATE events SET completed_at='2026-11-03T08:00:00' WHERE seq=1302")
        test_db.commit()
        _sync_issue_pipeline_terminal("test/repo", 1)
        assert _completed_at(test_db, 1302) == "2026-11-03T08:00:00"


class TestBackfillMigration:
    """存量回填迁移：终态且 completed_at 为空 → created_at 回填，幂等。"""

    def _run_migration(self, conn):
        from src.api.db_setup import _fn_backfill_events_terminal_completed_at
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_key TEXT, issue_id TEXT, event_type TEXT, status TEXT,
                created_at TEXT, completed_at TEXT DEFAULT ''
            )
        """)
        conn.executemany(
            "INSERT INTO events (seq, repo_key, status, created_at, completed_at) "
            "VALUES (?,?,?,?,?)",
            [
                (1401, "r", "completed", "2026-11-01 10:00:00", ""),
                (1402, "r", "failed", "2026-11-01 11:00:00", None),
                (1403, "r", "cancelled", "2026-11-01 12:00:00", ""),
                (1404, "r", "running", "2026-11-01 13:00:00", ""),
                (1405, "r", "completed", "2026-11-01 14:00:00", "2026-11-05 09:00:00"),
            ])
        conn.commit()
        _fn_backfill_events_terminal_completed_at(conn)
        conn.commit()

    def test_backfill_fills_missing_terminal(self, test_db):
        self._run_migration(test_db)
        got = {seq: _completed_at(test_db, seq) for seq in range(1401, 1406)}
        assert got[1401] == "2026-11-01 10:00:00"
        assert got[1402] == "2026-11-01 11:00:00"
        assert got[1403] == "2026-11-01 12:00:00"
        assert got[1404] in ("", None), "running 非终态不回填"
        assert got[1405] == "2026-11-05 09:00:00", "已非空不覆盖"

    def test_backfill_idempotent(self, test_db):
        self._run_migration(test_db)
        before = {seq: _completed_at(test_db, seq) for seq in range(1401, 1406)}
        from src.api.db_setup import _fn_backfill_events_terminal_completed_at
        _fn_backfill_events_terminal_completed_at(test_db)
        test_db.commit()
        after = {seq: _completed_at(test_db, seq) for seq in range(1401, 1406)}
        assert before == after, "#1105: 回填迁移必须幂等"

    def test_backfill_created_at_empty_falls_back(self, test_db):
        test_db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_key TEXT, status TEXT,
                created_at TEXT, completed_at TEXT DEFAULT ''
            )
        """)
        test_db.execute(
            "INSERT INTO events (seq, repo_key, status, created_at, completed_at) "
            "VALUES (1501, 'r', 'completed', '', '')")
        test_db.commit()
        from src.api.db_setup import _fn_backfill_events_terminal_completed_at
        _fn_backfill_events_terminal_completed_at(test_db)
        test_db.commit()
        assert _completed_at(test_db, 1501), "created_at 为空时兜底当前时间"
