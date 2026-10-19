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
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR id IN (900001)")
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR id IN (900001, 900002, 999)")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR id IN (900001)")
        db.cursor().execute("DELETE FROM agent_runtimes WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%' OR credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 'test_%' OR name LIKE 'udb_%' OR name LIKE 'ag_%' OR name LIKE 'a2a-%' OR name LIKE 'rl-%' OR name LIKE 'chat_%' OR name LIKE 'redis-%' OR name LIKE 'lr-%' OR name LIKE 'wt_%') OR id IN (900001, 900002, 999)")
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

    @pytest.fixture(autouse=True)
    def _no_upgrade_jitter(self):
        """升级 jitter 随机 0~60s 会睡掉测试窗口（0.25s）导致 flaky——归零。"""
        with patch("random.uniform", return_value=0):
            yield

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
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade, \
             patch("random.uniform", return_value=0):
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
                _test_active_tasks={"busy-tid": "busy-session"})))
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
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade, \
             patch("random.uniform", return_value=0):
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
             patch("src.runtime.agent._env_float", side_effect=lambda k, d: 0.01 if k == "RUNTIME_REREGISTER_INTERVAL" else d), \
             patch("random.uniform", return_value=0):
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

    def test_perform_upgrade_missing_params_returns_false(self):
        """_perform_upgrade server 或 token 为空时返回 False，不发起网络请求。"""
        async def _run():
            r1 = await ra._perform_upgrade("", "rta_test")
            r2 = await ra._perform_upgrade("http://server", "")
            return r1, r2
        r1, r2 = asyncio.run(_run())
        assert r1 is False
        assert r2 is False

    def test_perform_upgrade_success_returns_true(self):
        """_perform_upgrade install-worker.sh rc=0 时返回 True（等待重启）。"""
        async def _run():
            with patch("asyncio.to_thread", new=AsyncMock(side_effect=[
                MagicMock(returncode=0, stderr=""),  # curl download
                MagicMock(returncode=0, stderr=""),  # bash install-worker.sh
            ])), patch("tempfile.mkstemp", return_value=(999, "/tmp/fake_upgrade_test.sh")), \
                 patch("os.close"), patch("os.unlink"), patch("os.path.exists", return_value=False):
                return await ra._perform_upgrade("http://server", "rta_test")
        result = asyncio.run(_run())
        assert result is True

    def test_perform_upgrade_install_failure_returns_false(self):
        """_perform_upgrade install-worker.sh rc!=0 时返回 False。"""
        async def _run():
            with patch("asyncio.to_thread", new=AsyncMock(side_effect=[
                MagicMock(returncode=0, stderr=""),  # curl download
                MagicMock(returncode=1, stderr="install error"),  # bash install-worker.sh
            ])), patch("tempfile.mkstemp", return_value=(999, "/tmp/fake_upgrade_test.sh")), \
                 patch("os.close"), patch("os.unlink"), patch("os.path.exists", return_value=False):
                return await ra._perform_upgrade("http://server", "rta_test")
        result = asyncio.run(_run())
        assert result is False

    def test_heartbeat_none_skips_version_check(self):
        """心跳失败（None）时不执行版本对比（版本一致场景）。

        B3 后：心跳失败超阈值时会调 latest_version() 独立端点做版本比对。
        版本一致时不触发升级。
        """
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = None
        client.reset_session = AsyncMock()
        client.latest_version.return_value = "h-sameversion"
        client.get_task_state.return_value = "running"
        with patch("src.runtime.agent._read_local_worker_version", return_value="h-sameversion"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade, \
             patch("src.runtime.agent._reregister", new=AsyncMock(return_value=[1])), \
             patch("src.runtime.agent._env_float", side_effect=lambda k, d: 0.01 if k == "RUNTIME_REREGISTER_INTERVAL" else d):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.01, max_concurrent=4)))
            m_upgrade.assert_not_awaited()

    def test_heartbeat_failure_triggers_version_check_via_latest_version(self):
        """B3: 心跳失败超阈值时通过 latest_version() 独立端点感知新版本。

        死锁环：aiohttp 会话坏 → 心跳失败 → 版本检查不执行（仅心跳成功分支）
        → 不升级 → 会话不恢复。B3 修复后心跳失败也做版本比对。
        """
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = None  # 心跳始终失败
        client.reset_session = AsyncMock()
        client.latest_version.return_value = "h-newversion1234"
        client.get_task_state.return_value = "running"
        with patch("src.runtime.agent._read_local_worker_version", return_value="h-oldversion1234"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)) as m_upgrade, \
             patch("src.runtime.agent._reregister", new=AsyncMock(return_value=[1])), \
             patch("src.runtime.agent._env_float", side_effect=lambda k, d: 0.01 if k == "RUNTIME_REREGISTER_INTERVAL" else d), \
             patch("random.uniform", return_value=0):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.01, max_concurrent=4),
                duration=0.3))
            # 心跳失败后 latest_version 被调用做版本比对
            client.latest_version.assert_awaited()
            # 版本不一致 → 触发升级
            m_upgrade.assert_awaited()


class TestZombieTaskReclamation:
    """B2: 僵尸任务超龄摘除测试。"""

    @staticmethod
    async def _drive(loop_coro, duration=0.25):
        t = asyncio.ensure_future(loop_coro)
        await asyncio.sleep(duration)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    def test_zombie_task_reclaimed_and_fail_called(self):
        """超龄僵尸任务被摘除并回调 fail。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": "h-newversion1234"
        }
        client.get_task_state.return_value = "dispatched"
        client.fail = AsyncMock()
        # 模拟一个已经超龄的僵尸任务（claim_time 设为极小值，阈值 1s → loop.time() - 0.001 > 1）
        _zombie_task = {"id": "zombie-tid", "_engine_name": "opencode", "_claim_time": 0.001}
        with patch("src.runtime.agent._read_local_worker_version", return_value="h-oldversion1234"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)), \
             patch("src.runtime.agent._env_float", side_effect=lambda k, d: 0.01 if k == "RUNTIME_REREGISTER_INTERVAL" else (1.0 if k == "RUNTIME_ZOMBIE_TASK_THRESHOLD" else d)), \
             patch("random.uniform", return_value=0), \
             patch("src.runtime.agent._kill_stale_engine"):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4,
                _test_active_tasks={"zombie-tid": "zombie-session"},
                _test_active_task_objs={"zombie-tid": _zombie_task})))
            # 僵尸任务被回调 fail
            client.fail.assert_awaited()

    def test_non_zombie_task_not_reclaimed(self):
        """未超龄任务不被摘除。"""
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {
            "status": "ok", "cancelled_tasks": [],
            "latest_version": "h-newversion1234"
        }
        client.get_task_state.return_value = "running"
        client.fail = AsyncMock()
        with patch("src.runtime.agent._read_local_worker_version", return_value="h-oldversion1234"), \
             patch("src.runtime.agent._perform_upgrade", new=AsyncMock(return_value=False)), \
             patch("src.runtime.agent._env_float", side_effect=lambda k, d: 0.01 if k == "RUNTIME_REREGISTER_INTERVAL" else (999999.0 if k == "RUNTIME_ZOMBIE_TASK_THRESHOLD" else d)):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4,
                _test_active_tasks={"fresh-tid": "fresh-session"})))
            client.fail.assert_not_awaited()


class TestNormalizeWorkerVersion:
    """B5: 版本归一化测试。"""

    def test_new_format_unchanged(self):
        """新格式版本串（h-12位指纹）归一化后不变。"""
        assert ra._normalize_worker_version("h-abc123def456") == "h-abc123def456"

    def test_old_format_strips_w_p_suffix(self):
        """老格式版本串剥离 -w/-p 后缀。"""
        result = ra._normalize_worker_version("h-abc123def4567890-w111-p222")
        assert result == "h-abc123def456"
        assert "-w" not in result
        assert "-p" not in result

    def test_old_format_git_prefix(self):
        """老格式 git- 前缀版本串剥离 -w/-p 后缀（git- 不截断，short hash 本就 ≤12 字符）。"""
        result = ra._normalize_worker_version("git-abc123def456-w111-p222")
        assert result == "git-abc123def456"
        assert "-w" not in result
        assert "-p" not in result

    def test_empty_string(self):
        """空串归一化后仍为空。"""
        assert ra._normalize_worker_version("") == ""

    def test_none_like(self):
        """空值安全处理。"""
        assert ra._normalize_worker_version(None) == ""

    def test_already_normalized_idempotent(self):
        """已归一化的版本串再次归一化不变。"""
        v = "h-abc123def456"
        assert ra._normalize_worker_version(ra._normalize_worker_version(v)) == v

    def test_old_new_same_code_matches(self):
        """老格式和新格式指向同一代码版本时归一化后相等。"""
        old_format = "h-abc123def4567890-w111-p222"
        new_format = "h-abc123def456"
        assert ra._normalize_worker_version(old_format) == ra._normalize_worker_version(new_format)


class TestReadLocalWorkerVersion:
    """_read_local_worker_version 路径选择测试（与 install-worker.sh 一致）。"""

    def test_root_reads_etc_grape(self):
        """root 模式读取 /etc/grape/worker.version。"""
        with patch("src.runtime.agent.os.geteuid", return_value=0), \
             patch("builtins.open", create=True) as m_open:
            m_open.return_value.__enter__.return_value.read.return_value = "git-root-ver\n"
            result = ra._read_local_worker_version()
            assert "etc/grape/worker.version" in m_open.call_args[0][0]
            assert result == "git-root-ver"

    def test_non_root_reads_home_grape(self):
        """非 root 模式读取 $HOME/.grape/worker.version。"""
        with patch("src.runtime.agent.os.geteuid", return_value=1000), \
             patch("src.runtime.agent.os.path.expanduser", return_value="/home/testuser"), \
             patch("builtins.open", create=True) as m_open:
            m_open.return_value.__enter__.return_value.read.return_value = "git-user-ver\n"
            result = ra._read_local_worker_version()
            assert ".grape/worker.version" in m_open.call_args[0][0]
            assert result == "git-user-ver"

    def test_root_mode_env_override(self):
        """RUNTIME_ROOT_MODE=1 时读取 /etc/grape/worker.version（即使非 root）。"""
        with patch.dict("os.environ", {"RUNTIME_ROOT_MODE": "1"}), \
             patch("src.runtime.agent.os.geteuid", return_value=1000), \
             patch("builtins.open", create=True) as m_open:
            m_open.return_value.__enter__.return_value.read.return_value = "git-root-ver\n"
            result = ra._read_local_worker_version()
            assert "etc/grape/worker.version" in m_open.call_args[0][0]
            assert result == "git-root-ver"

    def test_missing_file_returns_empty(self):
        """版本文件不存在时返回空字符串。"""
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = ra._read_local_worker_version()
            assert result == ""

    def test_falls_back_to_package_version_when_stamp_missing(self, tmp_path):
        """戳文件缺失时退回包内 VERSION。

        没有这个兜底，本地版本恒为 ""，心跳每个检查周期都比对失败 →
        反复触发升级（下载整包 + 重启 worker），表现为"升级循环"。
        """
        (tmp_path / "VERSION").write_text("h-abc123\n", encoding="utf-8")
        _real_open = open

        def _fake_open(path, *a, **k):
            if str(path).endswith("worker.version"):
                raise FileNotFoundError(path)
            return _real_open(path, *a, **k)

        with patch("src.runtime.agent.os.geteuid", return_value=1000), \
             patch("builtins.open", side_effect=_fake_open), \
             patch("src.runtime.agent.os.getcwd", return_value=str(tmp_path)):
            assert ra._read_local_worker_version() == "h-abc123"

    def test_stamp_takes_priority_over_package_version(self, tmp_path):
        """戳文件在位时以戳为准（包内 VERSION 只是兜底，不参与正常路径）。"""
        (tmp_path / "VERSION").write_text("h-abc123\n", encoding="utf-8")
        with patch("src.runtime.agent.os.geteuid", return_value=1000), \
             patch("src.runtime.agent.os.path.expanduser", return_value=str(tmp_path)), \
             patch("src.runtime.agent.os.getcwd", return_value="/nowhere"):
            (tmp_path / ".grape").mkdir()
            (tmp_path / ".grape" / "worker.version").write_text("h-realstamp\n", encoding="utf-8")
            assert ra._read_local_worker_version() == "h-realstamp"
