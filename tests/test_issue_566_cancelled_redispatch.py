"""Issue #566 — cancelled（用户取消）终态支持重新派发（与 failed 同权）。

覆盖后端口径：
- redispatch 链路复用：reset_event_terminal 对 cancelled 事件同样重置为 running
  并清空 exit_code/completed_at/result/structured_output（允许重跑成功后覆盖）。
- 终态口径对齐 #269/#320：cancelled 不被延迟的 failed 信号误判为失败
  （update_event_status / update_event_complete 双层终态保护）。
- failed 既有行为零回归：reset_event_terminal 对 failed 事件语义不变。
"""
import pytest
import tempfile, os, sqlite3

from src.session_manager import db_lock


@pytest.fixture
def test_db(monkeypatch):
    """创建临时 SQLite DB 并替换全局 db/db_lock（含 structured_output 列）。"""
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
            started_at TEXT, completed_at TEXT, created_at TEXT,
            structured_output TEXT
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


def _insert_event(conn, seq, status="running", result="", structured_output=None,
                  exit_code=None, completed_at=""):
    conn.execute(
        "INSERT INTO events (seq, repo_key, issue_id, event_type, status, result, "
        "structured_output, exit_code, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (seq, "test/repo", "#1", "a2a_event", status, result, structured_output,
         exit_code, completed_at)
    )
    conn.commit()


def _row(conn, seq):
    return conn.execute(
        "SELECT status, result, structured_output, exit_code, completed_at "
        "FROM events WHERE seq=?", (seq,)).fetchone()


class TestCancelledRedispatch:
    """cancelled → 重新派发（reset_event_terminal 复用 failed 同一链路）。"""

    def test_reset_cancelled_event_to_running(self, test_db):
        """cancelled 事件 redispatch 后重置 running 并清产出（允许重跑覆盖）。"""
        from src.api.event_store import reset_event_terminal
        _insert_event(test_db, 5661, status="cancelled", result="用户手动终止",
                      structured_output='{"conclusion": "cancelled"}',
                      exit_code=-1, completed_at="2026-10-30T10:00:00Z")
        reset_event_terminal(5661)
        r = _row(test_db, 5661)
        assert r["status"] == "running"
        assert r["result"] == ""
        assert r["structured_output"] is None
        assert r["exit_code"] is None
        assert r["completed_at"] == ""

    def test_reset_cancelled_then_complete_overwrites(self, test_db):
        """cancelled→redispatch→重跑成功：complete 可写入新终态（执行完成用例）。"""
        from src.api.event_store import reset_event_terminal, update_event_complete
        _insert_event(test_db, 5662, status="cancelled", result="用户手动终止")
        reset_event_terminal(5662)
        update_event_complete(5662, "completed", exit_code=0, result="重跑成功",
                              structured_output={"conclusion": "ok"})
        r = _row(test_db, 5662)
        assert r["status"] == "completed"
        assert r["result"] == "重跑成功"

    def test_failed_reset_unchanged_semantics(self, test_db):
        """零回归：failed 事件 reset 语义与 cancelled 完全一致。"""
        from src.api.event_store import reset_event_terminal
        _insert_event(test_db, 5663, status="failed", result="boom",
                      structured_output='{"conclusion": "failed"}', exit_code=1)
        reset_event_terminal(5663)
        r = _row(test_db, 5663)
        assert r["status"] == "running"
        assert r["result"] == ""
        assert r["exit_code"] is None


class TestCancelledStatusAlignment:
    """cancelled 口径对齐 #269/#320：取消不误判为失败。"""

    def test_cancelled_not_overwritten_by_failed_status(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 5671, status="cancelled")
        update_event_status(5671, "failed")
        assert _row(test_db, 5671)["status"] == "cancelled"

    def test_cancelled_not_overwritten_by_delayed_failed_complete(self, test_db):
        """延迟失败上报（残留进程 fail_task 补报）不得把 cancelled 写成 failed。"""
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 5672, status="cancelled")
        update_event_complete(5672, "failed", exit_code=1, result="延迟失败信号")
        assert _row(test_db, 5672)["status"] == "cancelled"

    def test_running_can_still_be_cancelled(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 5673, status="running")
        update_event_status(5673, "cancelled")
        assert _row(test_db, 5673)["status"] == "cancelled"
