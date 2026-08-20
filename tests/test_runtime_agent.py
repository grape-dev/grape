"""RuntimeAgent 边端单元测试（src/runtime/agent.py）。

覆盖：env 解析、cwd 路径映射、本机 CLI 发现、超时杀进程、worker 版本读取、
重注册、主循环（claim 去重 / D9 串行冲突 / 并发上限 / 心跳失败自动重注册）。
"""
import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runtime import agent as ra

_is_win = sys.platform == "win32"


class TestEnvHelpers:
    def test_env_float(self, monkeypatch):
        monkeypatch.setenv("X", "2.5")
        assert ra._env_float("X", 1) == 2.5
        assert ra._env_float("MISSING", 1.5) == 1.5

    def test_env_float_invalid_uses_default(self, monkeypatch):
        monkeypatch.setenv("X", "abc")
        assert ra._env_float("X", 3.0) == 3.0

    def test_env_int(self, monkeypatch):
        monkeypatch.setenv("X", "42")
        assert ra._env_int("X", 1) == 42
        assert ra._env_int("MISSING", 7) == 7

    def test_env_int_invalid_uses_default(self, monkeypatch):
        monkeypatch.setenv("X", "notnum")
        assert ra._env_int("X", 9) == 9


class TestRemapCwd:
    def test_no_mapping_default_home(self, monkeypatch):
        monkeypatch.delenv("RUNTIME_CWD_PREFIX_MAP", raising=False)
        with patch("os.path.expanduser", return_value="/home/zhangsan"):
            assert ra._remap_cwd("/home/grape/org/repo") == "/home/zhangsan/org/repo"
        # 非 /home/grape 前缀不改
        with patch("os.path.expanduser", return_value="/home/zhangsan"):
            assert ra._remap_cwd("/data/x") == "/data/x"

    def test_empty_path(self):
        assert ra._remap_cwd("") == ""

    def test_custom_mapping(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_CWD_PREFIX_MAP", "/home/grape=/home/lvjuntao,/opt/data=/data")
        assert ra._remap_cwd("/home/grape/a/b") == "/home/lvjuntao/a/b"
        assert ra._remap_cwd("/opt/data/c") == "/data/c"
        assert ra._remap_cwd("/elsewhere/x") == "/elsewhere/x"


class TestWorkerVersion:
    def test_reads_file(self):
        with patch("builtins.open", return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value="1.2.3\n"))))):
            assert ra._read_local_worker_version() == "1.2.3"

    def test_missing_file_returns_empty(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert ra._read_local_worker_version() == ""


class TestDiscoverLocalRuntimes:
    def test_success(self):
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "opencode", "cli_path": "/usr/bin/opencode", "installed": True}]):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="opencode 1.0\n")):
                out = ra._discover_local_runtimes()
        assert out[0]["name"] == "opencode"
        assert out[0]["healthy"] is True
        assert out[0]["version"] == "opencode 1.0"

    def test_unhealthy(self):
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "opencode", "cli_path": "/usr/bin/opencode", "installed": True}]):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                out = ra._discover_local_runtimes()
        assert out[0]["healthy"] is False
        assert out[0]["version"] == ""

    def test_no_cli_path(self):
        with patch("src.runtime.registry.discover_runtimes", return_value=[
                {"name": "opencode", "cli_path": "", "installed": False}]):
            out = ra._discover_local_runtimes()
        assert out[0]["healthy"] is False


@pytest.mark.skipif(_is_win, reason="POSIX-only: pgrep/readlink/killpg")
class TestKillStaleEngine:
    def test_kills_matching_pid(self):
        task = {"cwd": "/home/grape/org/repo"}
        with patch("os.path.expanduser", return_value="/home/zhangsan"), \
             patch("subprocess.run", return_value=MagicMock(stdout="1234\n")), \
             patch("os.readlink", return_value="/home/zhangsan/org/repo/x"), \
             patch("os.getpgid", return_value=99), \
             patch("os.killpg") as m_kill:
            ra._kill_stale_engine(task)
        m_kill.assert_called_once_with(99, __import__("signal").SIGTERM)

    def test_no_processes(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="")), \
             patch("os.readlink", side_effect=FileNotFoundError), \
             patch("os.getpgid"), patch("os.killpg") as m_kill:
            ra._kill_stale_engine({})
        m_kill.assert_not_called()


class TestReregister:
    async def _client(self, ids):
        c = AsyncMock()
        c.register.return_value = ids
        return c

    def test_success_recovers_orphans(self):
        c = MagicMock()
        c.register = AsyncMock(return_value=([1, 2], "probe"))
        c.recover_orphans = AsyncMock()
        c.reset_session = AsyncMock()
        ids = asyncio.run(ra._reregister(c, [], "dev"))
        assert ids == [1, 2]
        assert c.recover_orphans.await_count == 2

    def test_empty_ids_returns_none(self):
        c = MagicMock()
        c.register = AsyncMock(return_value=[])
        c.reset_session = AsyncMock()
        assert asyncio.run(ra._reregister(c, [], "dev")) is None

    def test_register_exception_returns_none(self):
        c = MagicMock()
        c.register = AsyncMock(side_effect=RuntimeError("boom"))
        c.reset_session = AsyncMock()
        assert asyncio.run(ra._reregister(c, [], "dev")) is None


class TestStreamRuntimeAsync:
    async def _collect(self, agen):
        return [evt async for evt in agen]

    def test_bridges_events_from_producer(self):
        with patch("src.runtime.executor.stream_runtime",
                   return_value=iter([("step_start", "s"), ("text", "hi"), ("done", {"exit_code": 0})])):
            events = asyncio.run(self._collect(ra._stream_runtime_async("prompt", "/w")))
        assert events == [("step_start", "s"), ("text", "hi"), ("done", {"exit_code": 0})]

    def test_producer_error_ends_generator(self):
        def _boom(*a, **k):
            raise RuntimeError("opencode crashed")

        with patch("src.runtime.executor.stream_runtime", side_effect=_boom):
            events = asyncio.run(self._collect(ra._stream_runtime_async("prompt", "/w")))
        assert events == []


class TestExecuteTaskTokenInjection:
    """chat 任务执行必须注入用户级 token（USER_GITCODE_TOKEN 来源）。

    回归：2026-08-19 修复前，队列执行的 chat 任务只传 bot_token（repo_gitcode_token），
    user_token 缺失 → executor 不设 USER_GITCODE_TOKEN → agent 调 GitCode API 401。
    """

    def _chat_task(self, **kw):
        t = {
            "id": "task-chat-1", "agent_type": "chat", "event_type": "chat",
            "user_id": 7, "repo_key": "songspace/shelltest", "issue_id": "58",
            "prompt": "hello", "cwd": "/tmp/cg-token-test",
            "runtime_session_id": "", "env_extra": "{}",
            "skill_files": "{}", "project_info_files": "{}", "knowledge_files": "{}",
            "repo_gitcode_token": "BOT_REPO_TOKEN",
        }
        t.update(kw)
        return t

    def _fake_stream(self):
        """async 生成器：产出 text + done，供 _execute_task 消费。"""
        holder = {"last_kwargs": None}

        async def gen(*args, **kwargs):
            holder["last_kwargs"] = kwargs
            yield "text", {"text": "hi", "type": "text"}
            yield "done", {"text": "ok", "exit_code": 0, "oc_session_id": "ses_x"}
        return gen, holder

    async def _run(self, task):
        client = AsyncMock()
        fake, holder = self._fake_stream()
        with patch("src.runtime.agent._stream_runtime_async", new=fake), \
             patch("src.user.db.get_user_gitcode_token", return_value="USER_TOKEN_ABC") as m_ut:
            await ra._execute_task(client, task, "hermes-worker-1")
        return holder["last_kwargs"], m_ut

    def test_chat_task_injects_user_token(self):
        kwargs, m_ut = asyncio.run(self._run(self._chat_task()))
        assert kwargs["user_token"] == "USER_TOKEN_ABC"
        # bot token 语义保留（repo 级）
        assert kwargs["bot_token"] == "BOT_REPO_TOKEN"
        m_ut.assert_called_once_with(7)

    def test_non_chat_task_no_user_token(self):
        t = self._chat_task(agent_type="coding", event_type="issue")
        kwargs, m_ut = asyncio.run(self._run(t))
        assert kwargs["user_token"] == ""
        assert kwargs["bot_token"] == "BOT_REPO_TOKEN"


class TestRunLoop:
    """用 _test_execute / _test_active 钩子驱动主循环，短暂运行后取消。"""

    @staticmethod
    async def _drive(loop_coro, duration=0.25):
        t = asyncio.ensure_future(loop_coro)
        await asyncio.sleep(duration)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    def test_periodic_reregister(self, monkeypatch):
        monkeypatch.setenv("RUNTIME_REREGISTER_INTERVAL", "0.01")
        client = AsyncMock()
        client.claim.return_value = None
        client.heartbeat.return_value = True
        with patch("src.runtime.agent._reregister", new=AsyncMock(return_value=[1])) as m_rr:
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4)))
        m_rr.assert_awaited()

    def test_claims_and_executes(self):
        client = AsyncMock()
        client.claim.return_value = {"id": "t1", "issue_session_id": "s1", "agent_session_id": "a1"}
        client.heartbeat.return_value = True
        exec_mock = AsyncMock()
        asyncio.run(self._drive(ra._run_loop(
            client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4,
            _test_execute=exec_mock)))
        exec_mock.assert_awaited()
        assert client.claim.await_count > 0

    def test_session_conflict_requeues(self):
        client = AsyncMock()
        client.claim.return_value = {"id": "t1", "issue_session_id": "s_busy"}
        client.heartbeat.return_value = True
        exec_mock = AsyncMock()
        asyncio.run(self._drive(ra._run_loop(
            client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=4,
            _test_execute=exec_mock, _test_active={"s_busy"})))
        client.requeue.assert_awaited()
        exec_mock.assert_not_awaited()

    def test_max_concurrent_skips(self):
        client = AsyncMock()
        client.claim.return_value = {"id": "t1", "issue_session_id": "s2"}
        client.heartbeat.return_value = True
        exec_mock = AsyncMock()
        asyncio.run(self._drive(ra._run_loop(
            client, [1], poll_interval=0.01, heartbeat_interval=0.05, max_concurrent=1,
            _test_execute=exec_mock, _test_active={"occupied"})))
        exec_mock.assert_not_awaited()
        client.requeue.assert_not_awaited()

    def test_heartbeat_failure_triggers_reregister(self):
        client = AsyncMock()
        client.claim.return_value = None
        client.heartbeat.return_value = False
        client.reset_session = AsyncMock()
        with patch("src.runtime.agent._reregister", new=AsyncMock(return_value=[1])):
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.01, max_concurrent=4)))
        client.reset_session.assert_awaited()
        # 连续 3 次失败后触发重注册
        assert client.reset_session.await_count >= 3

    def test_claim_exception_counts_heartbeat_failure(self):
        client = AsyncMock()
        client.claim.side_effect = RuntimeError("net")
        client.heartbeat.return_value = True
        exec_mock = AsyncMock()
        asyncio.run(self._drive(ra._run_loop(
            client, [1], poll_interval=0.01, heartbeat_interval=0.01, max_concurrent=4,
            _test_execute=exec_mock)))
        exec_mock.assert_not_awaited()
