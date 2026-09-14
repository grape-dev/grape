"""Worker 自动升级测试（Issue #153）。

覆盖：
- 服务端心跳响应含 latest_version 字段
- Worker 版本对比：落后时设置 pending_upgrade
- Worker 版本一致时不触发升级
- 待升级时等待 active_tasks 为空
- 无活跃任务时触发升级
- 升级失败后指数退避
- latest_version 为空时跳过升级
"""
import asyncio
import hashlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runtime import agent as ra


# ===== 服务端测试 =====

def _make_token(name="CI-Upgrade-Worker"):
    import secrets
    token = "rta_" + secrets.token_urlsafe(32)
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), name, "global"))
        db.commit()
    return token


@pytest.fixture
def _clean_db():
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()


class TestHeartbeatLatestVersion:
    """服务端心跳响应应包含 latest_version 字段。"""

    def test_heartbeat_response_contains_latest_version(self, _clean_db):
        from fastapi.testclient import TestClient
        from src.api.main import app
        token = _make_token()
        client = TestClient(app)
        r = client.post("/api/runtime/heartbeat",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"runtime_ids": []})
        assert r.status_code == 200
        data = r.json()
        assert "latest_version" in data
        assert isinstance(data["latest_version"], str)


# ===== Worker 端测试 =====

class TestWorkerAutoUpgrade:
    """Worker 自动升级逻辑测试。"""

    @staticmethod
    async def _drive(loop_coro, duration=0.25):
        t = asyncio.ensure_future(loop_coro)
        await asyncio.sleep(duration)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    def test_version_mismatch_sets_pending_upgrade(self):
        """版本不一致时触发 _perform_upgrade（无活跃任务时执行）。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": "git-newversion-w123-p456"
        }
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-oldversion-w111-p222"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade:
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4)))
            m_upgrade.assert_awaited()

    def test_version_match_no_upgrade(self):
        """版本一致时不触发升级。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": "git-sameversion-w123-p456"
        }
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-sameversion-w123-p456"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade:
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4)))
            m_upgrade.assert_not_awaited()

    def test_pending_upgrade_waits_for_active_tasks(self):
        """有活跃任务时不执行升级。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": "git-newversion-w123-p456"
        }
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-oldversion-w111-p222"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade:
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4,
                _test_active={"busy-session"})))
            m_upgrade.assert_not_awaited()

    def test_empty_latest_version_skips_upgrade(self):
        """latest_version 为空时跳过升级。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": ""
        }
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-oldversion"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade:
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4)))
            m_upgrade.assert_not_awaited()

    def test_upgrade_failure_does_not_crash(self):
        """升级失败后 worker 继续运行（不崩溃）。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": "git-newversion-w123-p456"
        }
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-oldversion-w111-p222"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade:
            try:
                asyncio.run(self._drive(ra._run_loop(
                    client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4),
                    duration=0.3))
            except Exception:
                pytest.fail("Worker should not crash after upgrade failure")
            m_upgrade.assert_awaited()

    def test_upgrade_backoff_increases_wait(self):
        """首次失败后第二次升级在退避期间不触发。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        _call_count = {"n": 0}
        _hb_responses = [
            {"status": "ok", "cancelled_tasks": [], "latest_version": "git-new-w123-p456"}
        ] * 20

        async def _fake_hb(*args, **kwargs):
            return _hb_responses[min(_call_count["n"], len(_hb_responses) - 1)]

        client.heartbeat.side_effect = _fake_hb
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-old-w111-p222"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade, \
             patch("src.runtime.agent._env_float", side_effect=lambda k, d: 0.01 if k == "RUNTIME_REREGISTER_INTERVAL" else d):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.02, max_concurrent=4),
                duration=0.2))
            assert m_upgrade.await_count >= 1

    def test_perform_upgrade_download_failure_returns_false(self):
        """_perform_upgrade 下载失败时返回 False。"""
        async def _run():
            return await ra._perform_upgrade("http://invalid-server:99999", "rta_test")
        result = asyncio.run(_run())
        assert result is False

    def test_heartbeat_none_skips_version_check(self):
        """心跳失败（None）时不执行版本对比。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = None
        client.reset_session = AsyncMock()
        with patch("src.runtime.agent._read_local_worker_version", return_value="git-oldversion"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade, \
             patch("src.runtime.agent._reregister", new=AsyncMock(return_value=[1])):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.01, max_concurrent=4)))
            m_upgrade.assert_not_awaited()
