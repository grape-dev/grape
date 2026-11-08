"""Issue #1503：失败终态事件 result 非空（消除静默失败路径）。

背景：seq=55067 实例——exit_code=1 但事件 result 为空，排障需倒查日志。

覆盖（对应 Spec 验收标准 / AT-EVS-11）：
- update_event_complete：终态写入 result 为空 → 兜底「状态默认摘要（含 exit_code）」
  （与 #1105 completed_at 兜底同层收口）
- update_event_status：终态保护修正——后到写入携带非空原因 → 合并进 result（状态不变）；
  纯状态翻转仍 skip（#130 语义不回归）
- 裸 update_event_status(seq, "failed") → #476 守卫回填非空诊断 result
- service.fail_task：error 为空、无 _result_show 时 → 事件 result 落含状态/exit_code 默认摘要
"""
import os
import sys
import tempfile
import sqlite3

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


def _insert_event(conn, seq, status="running", result=""):
    conn.execute(
        "INSERT INTO events (seq, repo_key, issue_id, event_type, status, result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seq, "test/repo", "#1", "a2a_event", status, result, "2026-11-08 10:00:00")
    )
    conn.commit()


def _event(conn, seq):
    row = conn.execute(
        "SELECT status, result, exit_code, completed_at FROM events WHERE seq=?",
        (seq,)).fetchone()
    return tuple(row) if row else None


# ===== AT-EVS-11：终态写入必携带非空 result =====

class TestUpdateEventCompleteResultFallback:
    """update_event_complete：result 兜底与 completed_at 兜底同层收口。"""

    def test_failed_events_are_not_silent(self, test_db):
        """AT-EVS-11：failed + 空 result + exit_code=1 → result 兜底非空且含诊断信息。"""
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 15031, status="running")
        update_event_complete(15031, "failed", exit_code=1, result="")
        status, result, exit_code, completed_at = _event(test_db, 15031)
        assert status == "failed"
        assert result, "#1503: 失败事件 result 不得为空（seq=55067 场景）"
        assert "failed" in result.lower(), f"result 含状态: {result}"
        assert "1" in result, f"result 含 exit_code: {result}"
        assert completed_at, "completed_at 非空（#1105 不变量）"

    def test_failed_none_result_also_backfilled(self, test_db):
        """调用方未传 result（默认空串语义）→ 同样兜底。"""
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 15032, status="running")
        update_event_complete(15032, "failed", exit_code=-1)
        status, result, exit_code, _ = _event(test_db, 15032)
        assert status == "failed"
        assert result, "#1503: result=None/缺省 → 兜底非空"

    def test_completed_empty_result_gets_default_summary(self, test_db):
        """completed + 空 result → 兜底非空摘要（终态事件都须可读）。"""
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 15033, status="running")
        update_event_complete(15033, "completed", exit_code=0, result="")
        status, result, _, _ = _event(test_db, 15033)
        assert status == "completed"
        assert result, "#1503: completed 空 result → 兜底非空"
        assert "completed" in result.lower()

    def test_explicit_result_not_overwritten(self, test_db):
        """调用方显式传非空 result → 保持原语义，不覆盖。"""
        from src.api.event_store import update_event_complete
        _insert_event(test_db, 15034, status="running")
        update_event_complete(15034, "failed", exit_code=1, result="引擎 OOM 崩溃")
        _, result, _, _ = _event(test_db, 15034)
        assert result == "引擎 OOM 崩溃"


class TestUpdateEventStatusLateReasonMerge:
    """终态保护修正：后到带原因写入合并进 result；纯状态翻转仍 skip。"""

    def test_late_reason_merged_into_failed_result(self, test_db):
        """已 completed 事件收到后到 failed 带原因写入 → 状态保持 completed，原因合并进 result。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 15041, status="completed", result="任务完成")
        update_event_status(15041, "failed", result="worker 上报: 进程被 SIGKILL")
        status, result, _, _ = _event(test_db, 15041)
        assert status == "completed", "#130 终态保护：状态不被翻转"
        assert "worker 上报" in result, "#1503: 后到原因不得被静默吞掉"
        assert "任务完成" in result, "原 result 保留（合并非覆盖）"

    def test_late_reason_merged_when_result_empty(self, test_db):
        """终态事件 result 为空 + 后到带原因写入 → result 直接补上原因。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 15042, status="failed", result="")
        update_event_status(15042, "completed", result="迟到结构化产出: 已完成")
        status, result, _, _ = _event(test_db, 15042)
        assert status == "failed"
        assert "迟到结构化产出" in result, "#1503: 空 result 终态补后到原因"

    def test_pure_flip_still_skipped(self, test_db):
        """回归：裸终态翻转（无原因）仍被保护 skip，result 不动。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 15043, status="completed", result="原有结果")
        update_event_status(15043, "failed")
        status, result, _, _ = _event(test_db, 15043)
        assert status == "completed"
        assert result == "原有结果"

    def test_same_terminal_status_failed_fallback_unchanged(self, test_db):
        """回归：failed→failed 裸调用走 #476 空诊断回填（原有语义）。"""
        from src.api.event_store import update_event_status
        _insert_event(test_db, 15044, status="failed", result="")
        update_event_status(15044, "failed")
        _, result, exit_code, _ = _event(test_db, 15044)
        assert result, "#476: 同态 failed 裸调用回填诊断"
        assert exit_code is not None


class TestBareFailedGuard:
    """裸 update_event_status(seq, failed) 调用点守卫（webhook 降级路径）。"""

    def test_bare_failed_on_running_event_backfills_result(self, test_db):
        from src.api.event_store import update_event_status
        _insert_event(test_db, 15051, status="running")
        update_event_status(15051, "failed")
        status, result, exit_code, _ = _event(test_db, 15051)
        assert status == "failed"
        assert result, "#1503: 裸 failed 调用 → result 非空"
        assert exit_code is not None


# ===== service.fail_task 空 result 路径 =====

@pytest.fixture()
def svc_db(monkeypatch):
    """自包含 service 级 fixture（#369 先例模式）：不依赖共享测试库，避免跨文件污染。"""
    import threading
    from src.runtime import service as svc
    test_db = sqlite3.connect(":memory:", check_same_thread=False)
    test_db.execute("PRAGMA foreign_keys=OFF")
    test_db.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, agent_session_id TEXT DEFAULT '', runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER, issue_id TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '', skill TEXT DEFAULT '', skill_id INTEGER DEFAULT 0,
        prompt TEXT DEFAULT '', cwd TEXT DEFAULT '', runtime_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', env_extra TEXT DEFAULT '{}',
        skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '',
        project_info_files TEXT DEFAULT '{}', project_info_meta TEXT DEFAULT '{}',
        knowledge_files TEXT DEFAULT '{}', project_info_file_urls TEXT DEFAULT '{}',
        knowledge_file_urls TEXT DEFAULT '{}', attempts INTEGER DEFAULT 0,
        pool_agent_type TEXT DEFAULT '', affinity_runtime_id INTEGER DEFAULT 0,
        pool_tag TEXT DEFAULT '', pool_engine_ids TEXT DEFAULT '',
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0, self_reported INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '', stderr TEXT DEFAULT '',
        usage_json TEXT DEFAULT '{}', started_at TEXT, completed_at TEXT, created_at TEXT,
        user_gitcode_token TEXT DEFAULT ''
    );
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, event_type TEXT, source TEXT, issue_id TEXT,
        title TEXT, summary TEXT, actor TEXT, payload TEXT DEFAULT '', agent_type TEXT,
        runtime TEXT, skill TEXT, status TEXT, exit_code INTEGER, duration REAL,
        agent_logs TEXT, result TEXT, structured_output TEXT,
        created_at TEXT, started_at TEXT, completed_at TEXT, entity_type TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, installed INTEGER DEFAULT 0, credential_id INTEGER DEFAULT 0,
        last_seen_at TEXT, scope TEXT DEFAULT '', busy_slots INTEGER DEFAULT 0,
        max_concurrent INTEGER DEFAULT 4, probe_status TEXT DEFAULT ''
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at TEXT, content TEXT
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.Lock())
    import src.session_manager as _sm
    import src.api.event_store as _es
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", threading.Lock())
    monkeypatch.setattr(_es, "db", test_db)
    monkeypatch.setattr(_es, "db_lock", threading.Lock())
    yield test_db
    test_db.close()


class TestFailTaskEmptyResultFallback:
    """fail_task：_result_show 与 error 均为空 → 事件 result 落含状态/exit_code 默认摘要。"""

    def test_fail_task_empty_error_writes_default_summary(self, svc_db):
        from datetime import datetime, timezone
        from src.runtime import service
        c = svc_db
        c.execute("INSERT INTO events (seq, repo_key, event_type, status, result) "
                  "VALUES (15061, 'gitcode:test/repo', 'issue_opened', 'running', '')")
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, "
                  "issue_id, repo_key, agent_type, prompt, status, created_at) "
                  "VALUES ('t_15061', 's', 7, 15061, '#3', 'gitcode:test/repo', 'vod', 'p', "
                  "'running', ?)", (datetime.now(timezone.utc).isoformat(),))
        c.commit()

        service.fail_task("t_15061", "", exit_code=1)

        row = c.execute("SELECT status, result FROM events WHERE seq=15061").fetchone()
        assert row[0] == "failed"
        assert row[1], "#1503: error 为空时事件 result 仍非空"
        assert "1" in row[1], f"result 含 exit_code: {row[1]}"
