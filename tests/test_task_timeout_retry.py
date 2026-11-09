"""Issue #516 M2：worker 侧超时自动重试（_wrap attempts 循环）集成测试。

通过 _run_loop（_test_execute=None + monkeypatch _execute_task）驱动真实 _wrap 路径：
- 超时 → kill 引擎 → 退避 → 重试（task 带 _grape_resume_attempt 标记）→ 续跑成功不 fail
- 重试耗尽仍超时 → fail 消息保留 "timed out after" 签名（M3 观测依赖）+ attempts 数
- 重试次数可配置禁用（RUNTIME_TASK_TIMEOUT_RETRIES=0 → 仅 1 次尝试）
- 超时上限从 task env_extra 解析（M1 配置化在执行层的接入验证）
"""
import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime import agent as agent_mod


@pytest.fixture(autouse=True)
def _fast_knobs(monkeypatch):
    monkeypatch.setenv("RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF", "0.01")
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT_RETRIES", raising=False)
    monkeypatch.delenv("GRAPE_TASK_TIMEOUT_CONFIG", raising=False)
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: None)
    yield


class FakeClient:
    def __init__(self, task_dicts):
        self.queue = list(task_dicts)
        self.completed = []
        self.failed = []
        self.requeued = []

    async def claim(self, rid, **kwargs):
        if self.queue:
            return {"task": self.queue.pop(0), "scan_request": False}
        return None

    async def get_tags(self, rid):
        return []

    async def claim_by_tag(self, rid, tag, **kwargs):
        return None

    async def heartbeat(self, runtime_ids, **kwargs):
        return {"status": "ok"}

    async def reset_session(self):
        pass

    async def task_start(self, task_id):
        return {"status": "running"}

    async def complete(self, task_id, text, exit_code=0, **kw):
        self.completed.append((task_id, text, exit_code))
        return {"status": "ok"}

    async def fail(self, task_id, reason, **kw):
        self.failed.append((task_id, reason))
        return {"status": "ok"}

    async def requeue(self, task_id):
        self.requeued.append(task_id)

    async def get_task_state(self, task_id):
        return ""


def _task(timeout="0.05", retries="1"):
    return {
        "id": "t-retry-1",
        "issue_session_id": "isess-retry",
        "agent_session_id": "asess-retry",
        "agent_type": "coding",
        "repo_key": "gitcode:test/retry",
        "skill": "coding-cli-1",
        "prompt": "p",
        "cwd": os.getcwd(),
        "env_extra": json.dumps({"RUNTIME_TASK_TIMEOUT": timeout,
                                 "RUNTIME_TASK_TIMEOUT_RETRIES": retries}),
    }


def _drive(client):
    async def _run():
        await agent_mod._run_loop(client, [4], poll_interval=0.001,
                                  heartbeat_interval=999, max_concurrent=20)

    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=1.5))
    except asyncio.TimeoutError:
        pass


def test_timeout_retry_succeeds_no_fail(monkeypatch):
    """第 1 次尝试超时 → 第 2 次带续跑标记重试 → 成功 complete，不 fail。"""
    calls = []

    async def _fake_execute(client, task, engine_name=""):
        calls.append(task.get("_grape_resume_attempt") or 0)
        if len(calls) == 1:
            await asyncio.sleep(2)  # > 0.05s 超时
        return {"exit_code": 0, "text": "done after retry"}

    monkeypatch.setattr(agent_mod, "_execute_task", _fake_execute)
    client = FakeClient([_task()])
    _drive(client)
    assert calls == [0, 2], "第 2 次尝试应带 _grape_resume_attempt=2 续跑标记"
    assert client.completed and client.completed[0][1] == "done after retry"
    assert client.failed == []


def test_timeout_exhausted_fails_with_signature(monkeypatch):
    """重试耗尽仍超时 → fail，消息保留 timed out after 签名并带尝试数。"""
    attempts = []

    async def _fake_execute(client, task, engine_name=""):
        attempts.append(1)
        await asyncio.sleep(2)

    monkeypatch.setattr(agent_mod, "_execute_task", _fake_execute)
    client = FakeClient([_task(retries="1")])
    _drive(client)
    assert len(attempts) == 2, "默认重试 1 次 → 共 2 次尝试"
    assert client.completed == []
    assert len(client.failed) == 1
    tid, reason = client.failed[0]
    assert "task timed out after" in reason
    assert "attempts: 2" in reason


def test_retry_disabled_single_attempt(monkeypatch):
    attempts = []

    async def _fake_execute(client, task, engine_name=""):
        attempts.append(1)
        await asyncio.sleep(2)

    monkeypatch.setattr(agent_mod, "_execute_task", _fake_execute)
    client = FakeClient([_task(retries="0")])
    _drive(client)
    assert len(attempts) == 1, "RUNTIME_TASK_TIMEOUT_RETRIES=0 → 不重试"
    assert len(client.failed) == 1


def test_no_timeout_single_attempt(monkeypatch):
    """未超时（正常完成/正常 _error）→ 不重试。"""
    attempts = []

    async def _fake_execute(client, task, engine_name=""):
        attempts.append(1)
        return {"_error": "dependency missing"}

    monkeypatch.setattr(agent_mod, "_execute_task", _fake_execute)
    client = FakeClient([_task()])
    _drive(client)
    assert len(attempts) == 1
    assert client.failed and "dependency missing" in client.failed[0][1]


def test_coding_default_timeout_resolved_in_wrap(monkeypatch):
    """M1 接入验证：无显式 env 的 coding 任务超时 = 7200（agent 默认），vod=3600。"""
    resolved = []

    async def _fake_execute(client, task, engine_name=""):
        return {"exit_code": 0, "text": "ok"}

    monkeypatch.setattr(agent_mod, "_execute_task", _fake_execute)
    orig_wait_for = asyncio.wait_for

    async def _spy_wait_for(coro, timeout=None, **kw):
        resolved.append(timeout)
        return await orig_wait_for(coro, timeout=timeout, **kw)

    monkeypatch.setattr(asyncio, "wait_for", _spy_wait_for)
    t = _task()
    t["env_extra"] = "{}"  # coding → 内置默认 7200
    client = FakeClient([t])
    _drive(client)
    assert resolved and 7200.0 in resolved, f"coding 默认超时应解析为 7200，实际 {resolved}"
