"""Issue #2514 复审补测：direct_executor 基线阻断分支 + agent 巡检接线（§7）。"""
import asyncio

from unittest.mock import AsyncMock

from src.domains.runtime.engine import agent as ra
from src.domains.runtime.engine import direct_executor as de
from src.domains.runtime.engine.repogit import WorkspaceBaselineError


def _baseline_error():
    return WorkspaceBaselineError("2514", "/tmp/wt", "weekly-mys",
                                  "feat/issue-2514@abc123", 1, 2, "diverged", "main")


class TestDirectExecutorBaselineBlock:
    """direct_executor: WorkspaceBaselineError → event/task failed（Agent 不启动）。"""

    def _patch_side_effects(self, monkeypatch):
        calls = {}

        def _event(seq, status, exit_code=None, **kw):
            calls["event"] = {"seq": seq, "status": status,
                              "exit_code": exit_code, **kw}

        def _task(tid, status, **kw):
            calls["task"] = {"task_id": tid, "status": status, **kw}

        def _direct(seq, status, **kw):
            calls["direct"] = {"seq": seq, "status": status, **kw}

        monkeypatch.setattr("src.api.event_store.update_event_complete", _event)
        monkeypatch.setattr("src.domains.runtime.engine.service.update_task_status", _task)
        monkeypatch.setattr("src.domains.runtime.pipeline.event_pipeline._update_direct_task",
                            _direct)
        monkeypatch.setattr("src.domains.runtime.pipeline.event_pipeline._cleanup_direct_workdir",
                            lambda *a, **k: calls.setdefault("cleanup", (a, k)))
        monkeypatch.setattr("src.kernel.auth_sdk.auth_token.revoke_session_token",
                            lambda *a, **k: None)
        return calls

    @staticmethod
    def _raise_baseline(*_a, **_k):
        raise _baseline_error()

    def test_baseline_error_marks_task_and_event_failed(self, monkeypatch):
        calls = self._patch_side_effects(monkeypatch)
        monkeypatch.setattr(de, "_execute_agent_inner", self._raise_baseline)
        seq = 987001
        de._DIRECT_FUTURES[seq] = object()
        de._execute_agent("gitcode:owner/repo", seq, "coding", "backend", "skill",
                          {}, task_id="t-block")
        assert calls["task"]["task_id"] == "t-block"
        assert calls["task"]["status"] == "failed"
        assert calls["task"]["exit_code"] == 1
        assert "workspace baseline mismatch" in calls["task"]["result_text"]
        assert "expected_baseline=weekly-mys" in calls["task"]["result_text"]
        assert calls["event"]["seq"] == seq
        assert calls["event"]["status"] == "failed"
        assert calls["event"]["exit_code"] == 1
        assert "direct" not in calls, "有 task_id 时不应再走 _update_direct_task"
        assert seq not in de._DIRECT_FUTURES, "finally 必须清理 Future 登记"

    def test_baseline_error_without_task_id_updates_direct_task(self, monkeypatch):
        calls = self._patch_side_effects(monkeypatch)
        monkeypatch.setattr(de, "_execute_agent_inner", self._raise_baseline)
        seq = 987002
        de._execute_agent("gitcode:owner/repo", seq, "coding", "backend", "skill", {})
        assert calls["direct"]["seq"] == seq
        assert calls["direct"]["status"] == "failed"
        assert "reason=diverged" in calls["direct"]["result_text"]
        assert "task" not in calls, "无 task_id 时不应写任务状态"


class TestAgentPatrolWiring:
    """agent: 启动巡检 + 派发前巡检接线可执行且有单测覆盖。"""

    @staticmethod
    async def _drive(loop_coro, duration=0.2):
        t = asyncio.ensure_future(loop_coro)
        await asyncio.sleep(duration)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    def test_startup_patrol_calls_empty_active(self, monkeypatch):
        calls = []
        monkeypatch.setattr("src.domains.runtime.engine.repogit.patrol_workspaces",
                            lambda active=None, **k: calls.append(active))
        asyncio.run(ra._patrol_workspaces_startup())
        assert calls == [set()], "启动巡检应以空 active_dirs 执行一轮"

    def test_dispatch_patrol_passes_active_cwds(self, monkeypatch):
        calls = []
        monkeypatch.setattr("src.domains.runtime.engine.repogit.patrol_workspaces",
                            lambda active=None, **k: calls.append(active))
        asyncio.run(ra._patrol_workspaces_active(
            {"t1": {"cwd": "/tmp/live"}, "t2": {"cwd": ""}}))
        assert calls == [{"/tmp/live", ""}], "派发前巡检应带上在跑任务 cwd 白名单"

    def test_run_loop_periodic_patrol_invokes_dispatch_helper(self, monkeypatch):
        monkeypatch.setattr(ra, "_workspace_patrol_interval", lambda: 0.0)
        patrol = AsyncMock()
        monkeypatch.setattr(ra, "_patrol_workspaces_active", patrol)
        client = AsyncMock()
        client.get_tags.return_value = []
        client.claim_by_tag.return_value = None
        client.claim.return_value = None
        client.heartbeat.return_value = {"status": "ok", "cancelled_tasks": []}
        active = {"t1": {"cwd": "/tmp/live"}}
        with monkeypatch.context() as m:
            m.setattr(ra, "_start_worker_probe", lambda: None)
            asyncio.run(self._drive(ra._run_loop(
                client, [1], poll_interval=0.01, heartbeat_interval=0.05,
                max_concurrent=4, _test_active_task_objs=active)))
        patrol.assert_awaited()
        assert patrol.await_args.args[0] == active
