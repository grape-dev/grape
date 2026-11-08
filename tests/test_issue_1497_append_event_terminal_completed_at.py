"""Issue #1497：终态事件写入入口缺 completed_at 补齐。

#1105 只覆盖了 update_event_status / update_event_complete / 级联收敛 / 存量回填，
但 append_event 直接以终态 status INSERT 的事件（a2a 派发、跨流水线交接、
订阅、admin 审计、技能解析失败等）从未写 completed_at → status=completed 且
completed_at 为空。本文件锁定：
- append_event：终态 status INSERT 同事务写 completed_at（非空且 ≥ created_at）
- 非终态 status 不写 completed_at
- 调用方显式传 completed_at 时优先使用
- #1497 存量回填迁移：COALESCE(completed_at, updated_at?, created_at) 幂等回填
"""
import sqlite3
import os
import tempfile

import pytest


@pytest.fixture
def test_db(monkeypatch):
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    _conn = sqlite3.connect(_tmp.name, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("""
        CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT NOT NULL, issue_id TEXT, event_type TEXT, source TEXT,
            agent_type TEXT, title TEXT, actor TEXT, summary TEXT,
            payload TEXT, status TEXT, result TEXT,
            scene_id TEXT DEFAULT '', org_id INTEGER DEFAULT 0,
            entity_type TEXT DEFAULT '', idempotency_key TEXT DEFAULT NULL,
            duration REAL, agent_logs TEXT,
            started_at TEXT, completed_at TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    _conn.commit()
    monkeypatch.setattr("src.session_manager.db", _conn)
    monkeypatch.setattr("src.api.event_store.db", _conn)
    from src.session_manager import db_lock
    monkeypatch.setattr("src.api.event_store.db_lock", db_lock)
    try:
        import src.user.org_db as _org_db
        monkeypatch.setattr(_org_db, "get_org_id_by_repo_key", lambda _rk: 0)
    except Exception:
        pass
    yield _conn
    _conn.close()
    os.unlink(_tmp.name)


def _row(conn, seq):
    return conn.execute(
        "SELECT status, completed_at, created_at FROM events WHERE seq=?", (seq,)
    ).fetchone()


class TestAppendEventTerminalCompletedAt:
    """append_event 终态 status 必须在 INSERT 同事务写 completed_at。"""

    def test_completed_status_writes_completed_at(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="a2a_event",
                           agent_type="coding", title="t", issue_id="#1")
        row = _row(test_db, seq)
        assert row[0] == "completed"
        assert row[1], "#1497: append_event 默认终态必须落 completed_at"

    def test_failed_status_writes_completed_at(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="skill_resolve_failed",
                           agent_type="coding", title="t", issue_id="#1",
                           status="failed")
        row = _row(test_db, seq)
        assert row[0] == "failed"
        assert row[1], "#1497: failed 终态必须落 completed_at"

    def test_cancelled_status_writes_completed_at(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="a2a_event",
                           agent_type="coding", title="t", issue_id="#1",
                           status="cancelled")
        assert _row(test_db, seq)[1], "#1497: cancelled 终态必须落 completed_at"

    def test_timeout_status_writes_completed_at(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="a2a_event",
                           agent_type="coding", title="t", issue_id="#1",
                           status="timeout")
        assert _row(test_db, seq)[1], "#1497: timeout 终态必须落 completed_at"

    def test_pending_status_keeps_completed_at_empty(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="a2a_event",
                           agent_type="qa", title="t", issue_id="#1",
                           status="pending")
        row = _row(test_db, seq)
        assert row[0] == "pending"
        assert row[1] in ("", None), "非终态不写 completed_at"

    def test_running_status_keeps_completed_at_empty(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="a2a_event",
                           agent_type="qa", title="t", issue_id="#1",
                           status="running")
        row = _row(test_db, seq)
        assert row[1] in ("", None), "非终态不写 completed_at"

    def test_completed_at_not_earlier_than_created_at(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="a2a_event",
                           agent_type="coding", title="t", issue_id="#1")
        _, completed_at, created_at = _row(test_db, seq)
        assert completed_at and created_at
        assert completed_at >= created_at, "#1497: completed_at 必须 ≥ created_at"

    def test_explicit_completed_at_kept(self, test_db):
        from src.api.event_store import append_event
        seq = append_event(repo_key="r", event_type="vod_signal",
                           agent_type="vod", title="t", issue_id="#1",
                           status="completed", completed_at="2026-11-08T09:00:00+00:00")
        assert _row(test_db, seq)[1] == "2026-11-08T09:00:00+00:00"


class TestBackfillMigration1497:
    """#1497 存量回填：COALESCE(updated_at, created_at) 兜底，幂等，含 timeout。"""

    def _run_migration(self, conn, with_updated_at=False):
        from src.api.db_setup import _fn_backfill_events_terminal_completed_at_1497
        _ddl = """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_key TEXT, issue_id TEXT, event_type TEXT, status TEXT,
                created_at TEXT, {extra}completed_at TEXT DEFAULT ''
            )
        """
        conn.execute("DROP TABLE IF EXISTS events")
        conn.execute(_ddl.format(extra="updated_at TEXT, " if with_updated_at else ""))
        _rows = [
            (1401, "r", "completed", "2026-11-01 10:00:00", "2026-11-01 10:05:00", ""),
            (1402, "r", "failed", "2026-11-01 11:00:00", "2026-11-01 11:07:00", None),
            (1403, "r", "cancelled", "2026-11-01 12:00:00", "2026-11-01 12:03:00", ""),
            (1404, "r", "timeout", "2026-11-01 13:00:00", "2026-11-01 13:09:00", ""),
            (1405, "r", "running", "2026-11-01 14:00:00", "2026-11-01 14:01:00", ""),
            (1406, "r", "completed", "2026-11-01 15:00:00", "2026-11-01 15:02:00",
             "2026-11-05 09:00:00"),
        ]
        if with_updated_at:
            conn.executemany(
                "INSERT INTO events (seq, repo_key, status, created_at, updated_at, completed_at) "
                "VALUES (?,?,?,?,?,?)", _rows)
        else:
            conn.executemany(
                "INSERT INTO events (seq, repo_key, status, created_at, completed_at) "
                "VALUES (?,?,?,?,?)", [r[:4] + (r[5],) for r in _rows])
        conn.commit()
        _fn_backfill_events_terminal_completed_at_1497(conn)
        conn.commit()

    def test_backfill_prefers_updated_at(self, test_db):
        self._run_migration(test_db, with_updated_at=True)
        got = {seq: _row(test_db, seq)[1] for seq in range(1401, 1407)}
        assert got[1401] == "2026-11-01 10:05:00", "updated_at 存在时优先取 updated_at"
        assert got[1402] == "2026-11-01 11:07:00"
        assert got[1403] == "2026-11-01 12:03:00"
        assert got[1404] == "2026-11-01 13:09:00", "timeout 也是终态，须回填"
        assert got[1405] in ("", None), "非终态不回填"
        assert got[1406] == "2026-11-05 09:00:00", "已非空不覆盖"

    def test_backfill_falls_back_to_created_at_without_updated_at(self, test_db):
        self._run_migration(test_db, with_updated_at=False)
        got = {seq: _row(test_db, seq)[1] for seq in range(1401, 1405)}
        assert got[1401] == "2026-11-01 10:00:00", "无 updated_at 列时按 created_at 口径"
        assert got[1404] == "2026-11-01 13:00:00"

    def test_backfill_idempotent(self, test_db):
        self._run_migration(test_db, with_updated_at=True)
        before = {seq: _row(test_db, seq)[1] for seq in range(1401, 1407)}
        from src.api.db_setup import _fn_backfill_events_terminal_completed_at_1497
        _fn_backfill_events_terminal_completed_at_1497(test_db)
        test_db.commit()
        after = {seq: _row(test_db, seq)[1] for seq in range(1401, 1407)}
        assert before == after, "#1497: 回填迁移必须幂等可重放"
