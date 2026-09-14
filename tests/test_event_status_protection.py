"""事件状态终态保护测试（Issue #130）

验证 update_event_status 的幂等保护逻辑：
- 已 completed 的事件不允许被 failed 覆盖（核心修复：防止任务成功后被误标失败）
- 已 failed 的事件不允许被 completed 覆盖
- 已 cancelled 的事件不允许被其他终态覆盖
- 非终态→终态正常更新
- 非终态→非终态正常更新（如 running→running）
- 终态→同状态允许（幂等，不报错）
"""
import pytest
import tempfile, os, sqlite3

from src.session_manager import db as _db, db_lock as _db_lock


@pytest.fixture
def test_db(monkeypatch):
    """创建临时 SQLite DB 并替换全局 db/db_lock。"""
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp.close()
    _conn = sqlite3.connect(_tmp.name, check_same_thread=False)
    _conn.row_factory = sqlite3.Row

    # 创建 events 表（与生产 schema 一致）
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
    # db_lock 保持原样（threading.Lock，可重入性由 with 保证）
    # 但需要确保 src.api.event_store 导入的 db_lock 指向同一对象
    from src.session_manager import db_lock
    monkeypatch.setattr("src.api.event_store.db_lock", db_lock)

    yield _conn

    _conn.close()
    os.unlink(_tmp.name)


def _insert_event(conn, seq, status="running"):
    """插入一条测试事件。"""
    conn.execute(
        "INSERT INTO events (seq, repo_key, issue_id, event_type, status) VALUES (?, ?, ?, ?, ?)",
        (seq, "test/repo", "#1", "a2a_event", status)
    )
    conn.commit()


def _get_status(conn, seq):
    row = conn.execute("SELECT status FROM events WHERE seq=?", (seq,)).fetchone()
    return row[0] if row else None


class TestUpdateEventStatusTerminalProtection:
    """核心：终态保护——已终态事件不被其他终态覆盖。"""

    def test_completed_not_overwritten_by_failed(self, test_db):
        """Issue #130 核心场景：已 completed 的事件不能被 failed 覆盖。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 100, status="completed")
        update_event_status(100, "failed")
        assert _get_status(test_db, 100) == "completed"

    def test_failed_not_overwritten_by_completed(self, test_db):
        """已 failed 的事件不能被 completed 覆盖。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 101, status="failed")
        update_event_status(101, "completed")
        assert _get_status(test_db, 101) == "failed"

    def test_cancelled_not_overwritten_by_failed(self, test_db):
        """已 cancelled 的事件不能被 failed 覆盖。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 102, status="cancelled")
        update_event_status(102, "failed")
        assert _get_status(test_db, 102) == "cancelled"

    def test_cancelled_not_overwritten_by_completed(self, test_db):
        """已 cancelled 的事件不能被 completed 覆盖。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 103, status="cancelled")
        update_event_status(103, "completed")
        assert _get_status(test_db, 103) == "cancelled"

    def test_completed_not_overwritten_by_cancelled(self, test_db):
        """已 completed 的事件不能被 cancelled 覆盖。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 104, status="completed")
        update_event_status(104, "cancelled")
        assert _get_status(test_db, 104) == "completed"


class TestUpdateEventStatusNonTerminalToTerminal:
    """非终态→终态正常更新。"""

    def test_running_to_completed(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 200, status="running")
        update_event_status(200, "completed")
        assert _get_status(test_db, 200) == "completed"

    def test_running_to_failed(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 201, status="running")
        update_event_status(201, "failed")
        assert _get_status(test_db, 201) == "failed"

    def test_dispatched_to_completed(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 202, status="dispatched")
        update_event_status(202, "completed")
        assert _get_status(test_db, 202) == "completed"

    def test_dispatched_to_failed(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 203, status="dispatched")
        update_event_status(203, "failed")
        assert _get_status(test_db, 203) == "failed"


class TestUpdateEventStatusNonTerminalUpdates:
    """非终态→非终态正常更新（如 running→running, dispatched→running）。"""

    def test_running_to_running(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 300, status="running")
        update_event_status(300, "running")
        assert _get_status(test_db, 300) == "running"

    def test_dispatched_to_running(self, test_db):
        """redispatch 场景：dispatched → running 不受终态保护影响。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 301, status="dispatched")
        update_event_status(301, "running")
        assert _get_status(test_db, 301) == "running"

    def test_completed_to_running_allowed(self, test_db):
        """redispatch 场景：completed → running 应允许（用户手动重派已完成事件）。

        running 不是终态，所以终态保护不阻止 completed→running。
        """
        from src.api.event_store import update_event_status
        _insert_event(test_db, 302, status="completed")
        update_event_status(302, "running")
        assert _get_status(test_db, 302) == "running"


class TestUpdateEventStatusIdempotent:
    """终态→同状态允许（幂等，不报错）。"""

    def test_completed_to_completed(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 400, status="completed")
        update_event_status(400, "completed")
        assert _get_status(test_db, 400) == "completed"

    def test_failed_to_failed(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 401, status="failed")
        update_event_status(401, "failed")
        assert _get_status(test_db, 401) == "failed"


class TestIssue130Scenario:
    """Issue #130 复现场景：任务成功后事件被误标失败。

    模拟 Seq #5304 场景：
    1. 事件创建为 running
    2. 任务执行成功 → complete_task → update_event_complete → status=completed
    3. 延迟的 dispatch 失败信号 → update_event_status(seq, "failed")
    4. 修复后：步骤 3 不应覆盖步骤 2 的 completed 状态
    """

    def test_success_then_delayed_fail_stays_completed(self, test_db):
        from src.api.event_store import update_event_status, update_event_complete
        _insert_event(test_db, 5304, status="running")

        # 步骤 2：任务成功完成
        update_event_complete(5304, "completed", exit_code=0, duration=10.5,
                              completed_at="2026-09-12T10:00:00Z", result="构建成功")
        assert _get_status(test_db, 5304) == "completed"

        # 步骤 3：延迟的失败信号（竞态：_on_handoff_dispatch 异常 / sweeper 等）
        update_event_status(5304, "failed")

        # 修复后：状态应保持 completed
        assert _get_status(test_db, 5304) == "completed", \
            "Issue #130: 任务成功后不应被延迟的 failed 信号覆盖"

    def test_success_then_delayed_fail_via_complete_then_status(self, test_db):
        """验证 update_event_complete 的终态保护 + update_event_status 的终态保护协同。"""
        from src.api.event_store import update_event_status, update_event_complete
        _insert_event(test_db, 5305, status="running")

        # 任务成功
        update_event_complete(5305, "completed", exit_code=0, result="PR 已创建")
        assert _get_status(test_db, 5305) == "completed"

        # 多个延迟的 failed 信号
        update_event_status(5305, "failed")
        update_event_status(5305, "failed")
        assert _get_status(test_db, 5305) == "completed"

    def test_fail_then_delayed_complete_stays_failed(self, test_db):
        """反向验证：已 failed 的事件不被延迟的 completed 覆盖。"""
        from src.api.event_store import update_event_status, update_event_complete
        _insert_event(test_db, 5306, status="running")

        # 任务失败
        update_event_complete(5306, "failed", exit_code=1, result="执行失败")
        assert _get_status(test_db, 5306) == "failed"

        # 延迟的完成信号（残留进程跑完后调 complete）
        update_event_status(5306, "completed")
        assert _get_status(test_db, 5306) == "failed"
