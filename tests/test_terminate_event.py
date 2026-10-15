"""terminate_event 测试（Issue #136）。

覆盖：
- worker 模式终止 → task 状态=cancelled（worker 心跳 WHERE status=cancelled 可匹配）
- direct 模式终止 → task 状态=failed（保留原语义）
- worker 心跳查询 cancelled_tasks 匹配验证
- 事件状态回写 failed
"""
import time
from datetime import datetime, timezone

import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()


def _insert_event(seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status) VALUES (?, ?, ?, ?)",
            (seq, "gitcode:test/repo", "issue_opened", status),
        )
        db.commit()


def _insert_remote_runtime():
    """创建远端 worker runtime（credential_id > 0, online, fresh last_seen_at）。"""
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, status, last_seen_at) "
            "VALUES (?, ?, 'active', ?)",
            ("hash-test-cred", "test-worker", now),
        )
        cred_id = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, last_seen_at, healthy) "
            "VALUES (?, ?, 'online', ?, 1)",
            (cred_id, "opencode", now),
        )
        rt_id = c.lastrowid
        db.commit()
    return rt_id, cred_id


def _insert_local_runtime():
    """创建本机引擎 runtime（credential_id=0）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy) "
            "VALUES (0, 'opencode', 'offline', 1)",
        )
        rt_id = c.lastrowid
        db.commit()
    return rt_id


def _insert_task(task_id, event_seq, runtime_id, status="dispatched"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "sess-1", runtime_id, event_seq, "#136",
             "gitcode:test/repo", "coding", "test prompt", status,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


_TEST_USER = {"id": 1, "username": "test", "auth_type": "session_token"}


class TestTerminateWorkerMode:
    """worker 模式终止 → cancel_task（status=cancelled）。"""

    def test_worker_task_cancelled(self):
        from src.api.routes.events import terminate_event

        _insert_event(seq=100, status="running")
        rt_id, _ = _insert_remote_runtime()
        _insert_task("task-worker-1", event_seq=100, runtime_id=rt_id)

        result = terminate_event(seq=100, user=_TEST_USER)

        assert result["cancelled"] is True
        task = service.get_task("task-worker-1")
        assert task["status"] == "cancelled", (
            f"worker 模式 task 应为 cancelled, 实际={task['status']}"
        )

    def test_worker_event_status_failed(self):
        from src.api.routes.events import terminate_event

        _insert_event(seq=101, status="running")
        rt_id, _ = _insert_remote_runtime()
        _insert_task("task-worker-2", event_seq=101, runtime_id=rt_id)

        terminate_event(seq=101, user=_TEST_USER)

        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM events WHERE seq=?", (101,))
            row = c.fetchone()
        assert row and row[0] == "failed", (
            f"事件状态应为 failed, 实际={row[0] if row else 'None'}"
        )

    def test_heartbeat_matches_cancelled_task(self):
        """worker 心跳查询 WHERE status=cancelled 能匹配到新取消的任务。"""
        from src.api.routes.events import terminate_event

        _insert_event(seq=102, status="running")
        rt_id, cred_id = _insert_remote_runtime()
        _insert_task("task-worker-3", event_seq=102, runtime_id=rt_id)

        terminate_event(seq=102, user=_TEST_USER)

        # 模拟 worker 心跳查询 cancelled_tasks（runtime.py 中的查询逻辑）
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT id FROM agent_tasks WHERE runtime_id IN (?) AND status='cancelled'",
                (rt_id,),
            ).fetchall()
        cancelled_tasks = [r[0] for r in rows]
        assert "task-worker-3" in cancelled_tasks, (
            f"心跳查询应匹配到 cancelled task, got={cancelled_tasks}"
        )


class TestTerminateDirectMode:
    """direct 模式终止 → fail_task（status=failed，保留原语义）。"""

    def test_direct_task_failed(self):
        from src.api.routes.events import terminate_event

        _insert_event(seq=200, status="running")
        # runtime_id=0 → direct 模式
        _insert_task("task-direct-1", event_seq=200, runtime_id=0)

        result = terminate_event(seq=200, user=_TEST_USER)

        assert result["cancelled"] is True
        task = service.get_task("task-direct-1")
        assert task["status"] == "failed", (
            f"direct 模式 task 应为 failed, 实际={task['status']}"
        )

    def test_direct_event_status_failed(self):
        from src.api.routes.events import terminate_event

        _insert_event(seq=201, status="running")
        _insert_task("task-direct-2", event_seq=201, runtime_id=0)

        terminate_event(seq=201, user=_TEST_USER)

        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM events WHERE seq=?", (201,))
            row = c.fetchone()
        assert row and row[0] == "failed"

    def test_local_engine_task_failed(self):
        """本机引擎（credential_id=0）也走 fail_task。"""
        from src.api.routes.events import terminate_event

        _insert_event(seq=202, status="running")
        rt_id = _insert_local_runtime()
        _insert_task("task-local-1", event_seq=202, runtime_id=rt_id)

        terminate_event(seq=202, user=_TEST_USER)

        task = service.get_task("task-local-1")
        assert task["status"] == "failed", (
            f"本机引擎 task 应为 failed, 实际={task['status']}"
        )


class TestTerminateEdgeCases:
    """边界情况。"""

    def test_event_not_found(self):
        from src.api.routes.events import terminate_event
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            terminate_event(seq=999, user=_TEST_USER)
        assert exc.value.status_code == 404

    def test_event_already_completed(self):
        from src.api.routes.events import terminate_event
        from fastapi import HTTPException

        _insert_event(seq=300, status="completed")
        with pytest.raises(HTTPException) as exc:
            terminate_event(seq=300, user=_TEST_USER)
        assert exc.value.status_code == 400

    def test_no_task_id_still_works(self):
        """事件无关联 task → 仅 cancel_direct_task + 事件状态回写。"""
        from src.api.routes.events import terminate_event

        _insert_event(seq=400, status="running")
        result = terminate_event(seq=400, user=_TEST_USER)
        # cancel_direct_task 无 future → False，但事件状态仍回写 failed
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM events WHERE seq=?", (400,))
            row = c.fetchone()
        assert row and row[0] == "failed"
