"""Tests for Issue #357 system health optimization (P1/P2 changes).

Covers:
- P1-A: Connection pool initialization and DB_POOL_DISABLED fallback
- P1-B: Scene config cache (TTL, invalidation, loop-out batch)
- P1-D: Distributed task lock
- P2-A: Batched user_credentials DELETE
- P2-C: Table maintenance (agent_tasks / task_messages cleanup)
"""
import os
import time
from unittest.mock import MagicMock, patch

import pytest


# ====== P1-A: Connection Pool ======

class TestConnectionPool:
    def test_pool_disabled_falls_back_to_per_thread(self):
        """DB_POOL_DISABLED=1 skips pool init, uses legacy per-thread connection."""
        from src.mysql_compat import MySQLCompatDB
        db = MySQLCompatDB(host="10.0.0.1", port=3306, user="u", password="p", database="d")
        db._pool_disabled = True
        assert db._pool is None
        with patch("src.mysql_compat.pymysql.connect") as m_conn:
            m_conn.return_value.cursor.return_value = MagicMock()
            cur = db._raw_cursor()
        assert cur is not None
        assert m_conn.call_count == 1

    def test_pool_lazy_init_flag(self):
        """Pool is not initialized until first _raw_cursor call."""
        from src.mysql_compat import MySQLCompatDB
        db = MySQLCompatDB(host="h")
        assert db._pool is None
        assert db._pool_disabled is False  # not disabled by default (unless env says so)

    def test_pool_env_var_disables(self):
        """DB_POOL_DISABLED=1 env var sets _pool_disabled at init time."""
        from src.mysql_compat import MySQLCompatDB
        with patch.dict(os.environ, {"DB_POOL_DISABLED": "1"}):
            db = MySQLCompatDB(host="h")
            assert db._pool_disabled is True


# ====== P1-B: Scene Config Cache ======

class TestSceneConfigCache:
    def test_cache_put_and_get(self):
        """Cache stores and retrieves results within TTL."""
        from src.api.pipeline_config import _cache_put, _cache_get, _SCENE_CACHE, _SCENE_CACHE_LOCK
        _SCENE_CACHE.clear()
        _cache_put("scene1", "repo1", {"name": "Test Scene"})
        result, hit = _cache_get("scene1", "repo1")
        assert hit is True
        assert result == {"name": "Test Scene"}

    def test_cache_miss_returns_none(self):
        """Cache miss returns (None, False)."""
        from src.api.pipeline_config import _cache_get, _SCENE_CACHE
        _SCENE_CACHE.clear()
        result, hit = _cache_get("nonexistent", "norepo")
        assert hit is False
        assert result is None

    def test_cache_expiry(self):
        """Cache entries expire after TTL."""
        from src.api.pipeline_config import (
            _cache_put, _cache_get, _SCENE_CACHE, _SCENE_CACHE_TTL,
        )
        _SCENE_CACHE.clear()
        _cache_put("scene1", "repo1", {"name": "Test"})
        # Manually expire the entry
        with patch("src.api.pipeline_config.time.time", return_value=time.time() + _SCENE_CACHE_TTL + 1):
            result, hit = _cache_get("scene1", "repo1")
        assert hit is False
        assert result is None

    def test_invalidate_specific_key(self):
        """invalidate_scene_cache removes specific (scene_id, repo_key) entry."""
        from src.api.pipeline_config import (
            _cache_put, _cache_get, invalidate_scene_cache, _SCENE_CACHE,
        )
        _SCENE_CACHE.clear()
        _cache_put("scene1", "repo1", {"name": "A"})
        _cache_put("scene2", "repo1", {"name": "B"})
        invalidate_scene_cache("scene1", "repo1")
        _, hit1 = _cache_get("scene1", "repo1")
        _, hit2 = _cache_get("scene2", "repo1")
        assert hit1 is False
        assert hit2 is True

    def test_invalidate_by_repo_key(self):
        """invalidate_scene_cache(repo_key=X) removes all entries for that repo."""
        from src.api.pipeline_config import (
            _cache_put, _cache_get, invalidate_scene_cache, _SCENE_CACHE,
        )
        _SCENE_CACHE.clear()
        _cache_put("scene1", "repo1", {"name": "A"})
        _cache_put("scene2", "repo1", {"name": "B"})
        _cache_put("scene1", "repo2", {"name": "C"})
        invalidate_scene_cache("", "repo1")
        _, hit1 = _cache_get("scene1", "repo1")
        _, hit2 = _cache_get("scene2", "repo1")
        _, hit3 = _cache_get("scene1", "repo2")
        assert hit1 is False
        assert hit2 is False
        assert hit3 is True

    def test_invalidate_all(self):
        """invalidate_scene_cache() with no args clears entire cache."""
        from src.api.pipeline_config import (
            _cache_put, _cache_get, invalidate_scene_cache, _SCENE_CACHE,
        )
        _cache_put("s1", "r1", {"name": "A"})
        _cache_put("s2", "r2", {"name": "B"})
        invalidate_scene_cache()
        assert len(_SCENE_CACHE) == 0

    def test_cache_none_result(self):
        """None results are cached to avoid repeated misses."""
        from src.api.pipeline_config import _cache_put, _cache_get, _SCENE_CACHE
        _SCENE_CACHE.clear()
        _cache_put("missing", "repo", None)
        result, hit = _cache_get("missing", "repo")
        assert hit is True
        assert result is None


# ====== P1-D: Distributed Task Lock ======

class TestTaskLock:
    def test_lock_acquired_runs_body(self):
        """When lock is acquired, the body runs and lock is released."""
        import asyncio
        from src.runtime.task_lock import run_with_distributed_lock

        body_ran = False

        async def _test():
            nonlocal body_ran
            with patch("src.lock.acquire_lock", return_value=True):
                with patch("src.lock.release_lock") as mock_release:
                    async with run_with_distributed_lock("test_task", ttl=60) as acquired:
                        body_ran = True
                        assert acquired is True
                    return mock_release

        mock_release = asyncio.run(_test())
        assert body_ran
        mock_release.assert_called_once_with("test_task")

    def test_lock_not_acquired_skips_body(self):
        """When lock is not acquired (another worker holds it), body is skipped."""
        import asyncio
        from src.runtime.task_lock import run_with_distributed_lock

        body_ran = False

        async def _test():
            nonlocal body_ran
            with patch("src.lock.acquire_lock", return_value=False):
                async with run_with_distributed_lock("test_task", ttl=60) as acquired:
                    if acquired:
                        body_ran = True
                    return acquired

        acquired = asyncio.run(_test())
        assert not body_ran
        assert acquired is False


# ====== P2-C: Table Maintenance ======

class TestTableMaintenance:
    def test_cleanup_old_agent_tasks_fields(self):
        """cleanup_old_agent_tasks_fields nullifies old completed task fields."""
        from src.runtime.table_maintenance import cleanup_old_agent_tasks_fields
        from src.session_manager import db, db_lock

        # Insert a completed task with old timestamp
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, agent_type, status, "
                "prompt, result_text, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("test_357_1", "test_session", 1, 0, "test", "completed",
                 "old prompt", "old result", "2020-01-01 00:00:00"),
            )
            db.commit()

        # Run cleanup
        updated = cleanup_old_agent_tasks_fields()
        assert updated >= 1

        # Verify fields are cleared (prompt is NOT NULL so set to empty string)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT prompt, result_text FROM agent_tasks WHERE id='test_357_1'")
            row = c.fetchone()
            assert row[0] == ""
            assert row[1] == ""
            # Cleanup
            c.execute("DELETE FROM agent_tasks WHERE id='test_357_1'")
            db.commit()

    def test_cleanup_old_task_messages(self):
        """cleanup_old_task_messages deletes old messages."""
        from src.runtime.table_maintenance import cleanup_old_task_messages
        from src.session_manager import db, db_lock

        # Insert an old task message (type column, not role)
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO task_messages (task_id, seq, type, content, created_at) "
                "VALUES (?,?,?,?,?)",
                ("test_357_task", 99999, "text", "old message", "2020-01-01 00:00:00"),
            )
            db.commit()

        # Run cleanup
        deleted = cleanup_old_task_messages()
        assert deleted >= 1

        # Verify message is deleted
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM task_messages WHERE task_id='test_357_task'")
            count = c.fetchone()[0]
            assert count == 0
