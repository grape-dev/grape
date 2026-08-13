"""RuntimeAgent 客户端并发槽 + 任务回滚测试（seq#622 卡死回归看护）。

背景：seq#622 卡死根因——客户端 _run_loop 的 active_sessions 用 agent_session_id 做并发 key，
与服务端 claim 的 issue_session_id 粒度不一致：不同 Issue（不同 issue_session_id）的任务被服务端放行，
客户端却因相同 agent_session_id 误判冲突 skip → 任务 dispatched 悬空、无人执行。
修复：① 客户端 key 改为 issue_session_id（回退 agent_session_id）；② skip 时 requeue 回滚。
"""
import asyncio
import pytest

from src.session_manager import db, db_lock
from src.runtime import service
from src.runtime import agent as agent_mod


@pytest.fixture(autouse=True)
def _clean_tasks():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()


def _enqueue(session_id="vod:gitcode:test/repo", runtime_id=4, seq=1,
             issue_session_id="", issue_id="#1"):
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt="p",
        event_seq=seq, issue_id=issue_id, repo_key="gitcode:test/repo", agent_type="vod",
        skill="repo-onboarding", issue_session_id=issue_session_id,
    )


# ===================== 服务端 requeue_task =====================

class TestRequeueTask:
    def test_requeue_dispatched_back_to_queued(self):
        tid = _enqueue(seq=1, issue_session_id="isess-A")
        claimed = service.claim(4)
        assert claimed and claimed["id"] == tid
        assert service.get_task(tid)["status"] == "dispatched"
        ok = service.requeue_task(tid)
        assert ok
        t = service.get_task(tid)
        assert t["status"] == "queued"
        assert t["started_at"] is None
        again = service.claim(4)
        assert again and again["id"] == tid

    def test_requeue_only_recycles_active(self):
        tid = _enqueue(seq=1, issue_session_id="isess-A")
        assert service.requeue_task(tid) is False, "queued 任务无需回滚"
        service.claim(4)
        service.complete_task(tid, "done", 0)
        assert service.requeue_task(tid) is False, "已完成任务不可回滚"


# ===================== 客户端 _run_loop 并发槽 key =====================

class FakeClient:
    """模拟 RuntimeClient：记录 claim 出的任务与 requeue 调用。"""

    def __init__(self, task_dicts):
        self.queue = list(task_dicts)
        self.requeued = []

    async def claim(self, rid):
        if self.queue:
            return self.queue.pop(0)
        return None

    async def requeue(self, task_id):
        self.requeued.append(task_id)

    async def heartbeat(self, runtime_ids):
        pass


def _drive_loop(client, execute_fn, active_prefill=set()):
    """单线程驱动 _run_loop 至 queue 耗尽（用 wait_for 超时兜底）。"""

    async def _run():
        await agent_mod._run_loop(client, [4], poll_interval=0.001,
                                  heartbeat_interval=999, max_concurrent=20,
                                  _test_execute=execute_fn, _test_active=active_prefill)
    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=0.8))
    except asyncio.TimeoutError:
        pass


def test_cross_issue_parallel_same_agent_session(monkeypatch):
    """跨 Issue（不同 issue_session_id）+ 相同 agent_session_id → 客户端都执行，不误判冲突不 requeue。

    回归看护 seq#622：修复前客户端用 agent_session_id 会误判两个不同 Issue 任务冲突并 skip。
    """
    a = _enqueue(seq=1, issue_session_id="isess-A", issue_id="#1")
    b = _enqueue(seq=2, issue_session_id="isess-B", issue_id="#2")
    executed = []

    async def _fake_execute(client, task, engine_name=""):
        executed.append(task.get("id"))

    client = FakeClient([service.get_task(a), service.get_task(b)])
    _drive_loop(client, _fake_execute)
    assert sorted(executed) == sorted([a, b]), "跨 Issue 两个任务都应执行"
    assert client.requeued == [], "无并发冲突，不应 requeue"


def test_same_agent_session_different_issue_no_conflict(monkeypatch):
    """同 agent_session_id + 不同 issue_session_id → 并发 key 不同，任务不互相阻塞。"""
    a = _enqueue(seq=1, issue_session_id="isess-A", issue_id="#1")
    b = _enqueue(seq=2, issue_session_id="isess-B", issue_id="#2")
    executed = []

    async def _fake_execute(client, task, engine_name=""):
        executed.append(task.get("id"))

    client = FakeClient([service.get_task(a), service.get_task(b)])
    _drive_loop(client, _fake_execute)
    assert len(executed) == 2
    assert client.requeued == []


def test_same_issue_conflict_requeued():
    """同 issue_session_id（同 Issue）已有活跃任务 → 冲突分支 requeue 回滚，不悬空。"""
    tid = _enqueue(seq=1, issue_session_id="isess-A", issue_id="#1")
    task = service.get_task(tid)

    async def _fake_execute(client, task, engine_name=""):
        await asyncio.sleep(1)  # 保持活跃，使同 key 冲突分支可触发

    # 预置 active_sessions 已含 isess-A（模拟同 Issue 已有任务在跑）
    client = FakeClient([task])
    _drive_loop(client, _fake_execute, active_prefill={"isess-A"})
    assert client.requeued == [tid], "同 Issue 冲突应 requeue 回滚，避免 dispatched 悬空"
