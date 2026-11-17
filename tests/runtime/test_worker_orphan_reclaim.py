"""孤儿引擎回收：代次（run_token）比对重启 + finally 清理 + queued 幽灵 + hermes 计数。

对应线上现象：任务被服务端回炉并重新派发（started_at 刷新），worker 本地旧协程
仍占着同一 tid → 后续 claim 全部 skip → 引擎进程残留、任务空转，直到平台按
attempts 判死（result_text 出现 attempts cap）。
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine import agent as agent_mod


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.setenv("RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF", "0.01")
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT", raising=False)
    monkeypatch.delenv("RUNTIME_TASK_TIMEOUT_RETRIES", raising=False)
    # 引擎杀进程一律打桩（同步阻塞调用，测试里不真杀）
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: None)
    # 凭据吊销 Event 是模块级全局量：上一个用例触发过会残留，导致后续主循环秒退
    agent_mod._credential_revoked.clear()
    yield
    agent_mod._credential_revoked.clear()


class FakeClient:
    """claim 只「预占」不「消费」：任务真正开始执行后才推进到下一轮次。

    真实服务端在 claim 事务里已把任务置 running，所以被 worker skip 掉的任务不会
    自动重投；测试里用「未开始不推进」模拟这一语义，避免同一任务被反复领走后丢失。
    """

    def __init__(self, task_dicts, task_state="running"):
        self.sequence = list(task_dicts)
        self.idx = 0
        self.completed = []
        self.failed = []
        self.requeued = []
        self.task_state = task_state

    def mark_started(self):
        if self.idx < len(self.sequence):
            self.idx += 1

    async def claim(self, rid, **kwargs):
        if self.idx < len(self.sequence):
            return {"task": self.sequence[self.idx], "scan_request": False}
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
        return {"status": "ok"}

    async def complete(self, task_id, text, exit_code=0, **kw):
        self.completed.append((task_id, text, exit_code))
        return {"status": "ok"}

    async def fail(self, task_id, reason, **kw):
        self.failed.append((task_id, reason))
        return {"status": "ok"}

    async def requeue(self, task_id):
        self.requeued.append(task_id)

    async def get_task_state(self, task_id):
        return self.task_state


def _task(tid, started_at, sid="", engine="opencode"):
    return {
        "id": tid,
        "started_at": started_at,
        "issue_session_id": sid or f"isess-{tid}",
        "agent_session_id": f"asess-{tid}",
        "agent_type": "coding",
        "repo_key": "gitcode:test/orphan",
        "skill": "coding-cli-1",
        "prompt": "p",
        "cwd": os.getcwd(),
        "_engine_name": engine,
    }


def _drive(client, execute, timeout=1.2, engine="opencode"):
    """跑主循环若干秒，返回注入的活跃状态容器便于断言。"""
    sessions, tasks, objs, tokens = set(), {}, {}, {}

    async def _run():
        await agent_mod._run_loop(
            client, [4], poll_interval=0.001, heartbeat_interval=999, max_concurrent=20,
            engine_by_rid={4: engine},
            _test_execute=execute,
            _test_active=sessions, _test_active_tasks=tasks,
            _test_active_task_objs=objs, _test_active_run_tokens=tokens)

    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=timeout))
    except (asyncio.TimeoutError, agent_mod.CredentialRevokedError):
        pass
    return {"sessions": sessions, "tasks": tasks, "objs": objs, "tokens": tokens}


def test_redispatch_with_new_run_token_purges_and_restarts(monkeypatch):
    """同一 tid 两次 claim、started_at 变了 → 杀一次旧引擎 + 再执行一轮。"""
    kills = []
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: kills.append(a))
    seen = []

    async def execute(client, task, engine=""):
        client.mark_started()
        seen.append(task.get("started_at"))
        if task.get("started_at") == "2026-11-17 02:48:08":
            await asyncio.sleep(0.4)   # 旧轮次仍在跑，服务端才可能刷新代次
        return {"exit_code": 0, "text": "ok"}

    client = FakeClient([_task("t-orphan", "2026-11-17 02:48:08"),
                         _task("t-orphan", "2026-11-17 03:14:01")])
    state = _drive(client, execute)
    assert seen == ["2026-11-17 02:48:08", "2026-11-17 03:14:01"], seen
    assert len(kills) == 1, f"应杀一次旧引擎，实际 {len(kills)}"
    assert state["tasks"] == {} and state["tokens"] == {}


def test_same_run_token_still_skips(monkeypatch):
    """代次没变 → 维持原语义「已在跑，跳过」，不重启（守住 seq#1477）。"""
    kills = []
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: kills.append(a))
    seen = []

    async def execute(client, task, engine=""):
        client.mark_started()
        seen.append(task.get("started_at"))
        await asyncio.sleep(2.0)   # 观察窗口内保持"在跑"
        return {"exit_code": 0, "text": "ok"}

    client = FakeClient([_task("t-same", "2026-11-17 02:48:08"),
                         _task("t-same", "2026-11-17 02:48:08")])
    _drive(client, execute, timeout=0.6)
    assert seen == ["2026-11-17 02:48:08"], seen
    assert kills == []


def test_restart_cap_stops_after_two_rounds(monkeypatch):
    """坏引擎被反复回炉：第 3 次代次变化不再重启，交给平台判死。"""
    kills = []
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: kills.append(a))
    seen = []

    async def execute(client, task, engine=""):
        client.mark_started()
        seen.append(task.get("started_at"))
        await asyncio.sleep(5.0)       # 坏引擎：每轮都卡着不返回，代次才会连续刷新
        return {"exit_code": 0, "text": "ok"}

    client = FakeClient([_task("t-cap", f"2026-11-17 0{i}:00:00") for i in range(1, 6)])
    _drive(client, execute, timeout=1.0)
    assert len(seen) == 3, f"执行轮次应为 3（首轮 + 2 次重启），实际 {len(seen)}"
    assert len(kills) == 2, kills


def test_early_return_on_task_start_failure_cleans_state(monkeypatch):
    """task_start 三次失败提前 return：活跃状态必须被 finally 清空。"""
    class BrokenStart(FakeClient):
        async def task_start(self, task_id):
            return None

    async def execute(client, task, engine=""):
        client.mark_started()
        raise AssertionError("不应进入执行")

    client = BrokenStart([_task("t-start", "2026-11-17 01:00:00")])
    # task_start 重试 3 次、间隔 1s，需要跑够时间才能观察到 fail 与 finally 清理
    state = _drive(client, execute, timeout=3.5)
    assert client.failed, "task_start 失败应回调 fail"
    assert state["tasks"] == {} and state["sessions"] == set() and state["tokens"] == {}


def test_early_return_on_credential_revoked_cleans_state(monkeypatch):
    """凭据吊销提前 return：活跃状态必须被清空（旧实现会永久占位）。"""
    async def execute(client, task, engine=""):
        client.mark_started()
        raise agent_mod.CredentialRevokedError("revoked")

    client = FakeClient([_task("t-rev", "2026-11-17 01:00:00")])
    state = _drive(client, execute, timeout=0.8)
    assert state["tasks"] == {} and state["sessions"] == set() and state["tokens"] == {}


def test_early_return_on_execution_crash_cleans_state(monkeypatch):
    """执行崩溃提前 return：仍然清空，且回调 fail 带上原因。"""
    async def execute(client, task, engine=""):
        client.mark_started()
        raise RuntimeError("boom")

    client = FakeClient([_task("t-crash", "2026-11-17 01:00:00")])
    state = _drive(client, execute, timeout=0.8)
    assert client.failed and "execution crashed" in client.failed[0][1]
    assert state["tasks"] == {} and state["sessions"] == set()


def test_hermes_slot_not_leaked_by_early_return(monkeypatch):
    """hermes 限并发 1：前一单提前 return 后，下一单 hermes 仍能被接走。"""
    calls = []

    async def execute(client, task, engine=""):
        client.mark_started()
        calls.append(task.get("id"))
        if task.get("id") == "h-1":
            raise RuntimeError("boom")
        return {"exit_code": 0, "text": "ok"}

    client = FakeClient([_task("h-1", "2026-11-17 01:00:00", engine="hermes"),
                         _task("h-2", "2026-11-17 01:05:00", engine="hermes")])
    _drive(client, execute, engine="hermes", timeout=1.2)
    assert calls == ["h-1", "h-2"], calls


def test_stale_round_does_not_report_or_clear_new_round(monkeypatch):
    """被摘掉的旧执行轮：晚些退出时不上报、不清新一轮账本。"""
    kills = []
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: kills.append(a))
    seen = []

    async def execute(client, task, engine=""):
        client.mark_started()
        tok = task.get("started_at")
        seen.append(tok)
        if tok == "2026-11-17 02:48:08":
            await asyncio.sleep(0.9)   # 旧轮次很慢，退出时新一轮已经跑完了
        return {"exit_code": 0, "text": f"done-{tok}"}

    client = FakeClient([_task("t-stale", "2026-11-17 02:48:08"),
                         _task("t-stale", "2026-11-17 03:14:01")])
    state = _drive(client, execute, timeout=1.4)
    assert seen == ["2026-11-17 02:48:08", "2026-11-17 03:14:01"], seen
    assert len(kills) == 1
    # 旧轮次不得把自己的结果再上报一次（只应看到新一轮的 done）
    texts = [c[1] for c in client.completed]
    assert "done-2026-11-17 02:48:08" not in texts, texts
    assert state["tasks"] == {} and state["tokens"] == {}


def test_watchdog_treats_queued_as_ghost(monkeypatch):
    """服务端说 queued（被回炉/被降级回队）→ 本地残留按幽灵清掉并杀引擎。"""
    monkeypatch.setenv("RUNTIME_SID_WATCHDOG_INTERVAL", "0")
    kills = []
    monkeypatch.setattr(agent_mod, "_kill_stale_engine", lambda *a, **k: kills.append(a))

    async def execute(client, task, engine=""):
        client.mark_started()
        await asyncio.sleep(5)   # 一直"在跑"，让看门狗有机会介入

    client = FakeClient([_task("t-queued", "2026-11-17 01:00:00")], task_state="queued")
    state = _drive(client, execute, timeout=0.8)
    assert kills, "queued 幽灵应触发杀引擎"
    assert state["tasks"] == {} and state["tokens"] == {}