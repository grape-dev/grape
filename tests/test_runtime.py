"""RuntimeService 任务队列测试（见 acceptance-plan M1-R1~R7 / concurrency CC-3）"""
import json, threading
import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean_tasks():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()


def _enqueue(session_id="sess-1", runtime_id=1, prompt="p", seq=1, issue_session_id="", issue_id="#5"):
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt=prompt,
        event_seq=seq, issue_id=issue_id, repo_key="gitcode:test/repo", agent_type="issue",
        skill="issue", env_extra={"K": "v"}, issue_session_id=issue_session_id,
    )


class TestEnqueue:
    def test_requires_session_and_runtime(self):
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="", runtime_id=1, prompt="p")
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="s", runtime_id=0, prompt="p")

    def test_snapshot_fields(self):
        tid = _enqueue(seq=7)
        t = service.get_task(tid)
        assert t["agent_session_id"] == "sess-1" and t["runtime_id"] == 1
        assert t["event_seq"] == 7 and t["issue_id"] == "#5"
        assert t["status"] == "queued"
        assert json.loads(t["env_extra"])["K"] == "v"

    def test_packs_skill_files(self):
        import os
        from src.config import SKILLS_DIR
        # 直接指定 skill_path，不依赖 skill 表（避免全量测试顺序污染）
        tid = service.enqueue_task(
            agent_session_id="sess-1", runtime_id=1, prompt="p", event_seq=1,
            issue_id="#5", repo_key="test/repo", agent_type="issue",
            skill="issue", skill_path=os.path.join(SKILLS_DIR, "issue", "SKILL.md"),
            env_extra={"K": "v"},
        )
        t = service.get_task(tid)
        sf = json.loads(t["skill_files"])
        assert "issue" in sf, "skill_files 应含 issue 技能包"
        assert "SKILL.md" in sf["issue"], "SKILL.md 应打包"


class TestClaim:
    def test_claim_routing(self):
        a = _enqueue(session_id="s1", runtime_id=1, seq=1)
        b = _enqueue(session_id="s2", runtime_id=2, seq=2)
        got = service.claim(1)
        assert got and got["id"] == a
        got2 = service.claim(1)
        assert got2 is None, "同 runtime 无更多 queued"
        # runtime 2 的任务不受影响
        got3 = service.claim(2)
        assert got3 and got3["id"] == b

    def test_claim_serial_slot_d9(self):
        """同 agent_session_id 已有 dispatched → 不 claim（严格串行）"""
        _enqueue(session_id="s1", runtime_id=1, seq=1)
        _enqueue(session_id="s1", runtime_id=1, seq=2)
        first = service.claim(1)
        assert first and first["agent_session_id"] == "s1"
        second = service.claim(1)
        assert second is None, "同 session 已有活跃 task，不得 claim（D9）"

    def test_claim_serial_by_issue_session(self):
        """同 issue_session_id 严格串行（M2，见 multi-issue-isolation-design.md §3.3）"""
        isess = "isess-1"
        _enqueue(session_id="s1", runtime_id=1, seq=1, issue_session_id=isess)
        _enqueue(session_id="s1", runtime_id=1, seq=2, issue_session_id=isess)
        first = service.claim(1)
        assert first["issue_session_id"] == isess
        second = service.claim(1)
        assert second is None, "同 Issue 已有活跃 task，不得并行（串行槽）"

    def test_claim_parallel_across_issues(self):
        """不同 issue_session_id 可并行（M2 核心：跨 Issue 不互相阻塞）"""
        a = _enqueue(session_id="s1", runtime_id=1, seq=1, issue_session_id="isess-A", issue_id="#5")
        b = _enqueue(session_id="s1", runtime_id=1, seq=2, issue_session_id="isess-B", issue_id="#6")
        got_a = service.claim(1)
        got_b = service.claim(1)
        assert got_a and got_b, "不同 Issue 的两个 queued 任务都应被 claim（并行）"
        assert {got_a["id"], got_b["id"]} == {a, b}

    def test_claim_fallback_to_agent_session(self):
        """issue_session_id 为空（旧任务/#0）→ 回退按 agent_session_id 串行"""
        _enqueue(session_id="s1", runtime_id=1, seq=1, issue_id="#0")
        _enqueue(session_id="s1", runtime_id=1, seq=2, issue_id="#0")
        first = service.claim(1)
        assert first
        second = service.claim(1)
        assert second is None, "无 issue_session_id 时按 agent_session_id 串行（现状语义）"

    def test_complete_updates_issue_session(self):
        """complete 后新会话续接指针回写 Issue 级会话"""
        from src.api.session_store import get_or_create_issue_session, get_issue_session
        isess = get_or_create_issue_session("gitcode:test/repo", "issue", "#5")
        _enqueue(session_id="s1", runtime_id=1, seq=1, issue_session_id=isess["id"], issue_id="#5")
        tid = service.claim(1)["id"]
        service.complete_task(tid, "done", 0, usage={"session_id": "new_ses"})
        got = get_issue_session("gitcode:test/repo", "issue", "#5")
        assert got["runtime_session_id"] == "new_ses", "续接指针应写回 Issue 级会话"

    def test_claim_order_fifo(self):
        _enqueue(session_id="s1", runtime_id=1, seq=1)
        _enqueue(session_id="s2", runtime_id=1, seq=2)
        first = service.claim(1)
        assert first["event_seq"] == 1
        second = service.claim(1)
        assert second["event_seq"] == 2

    def test_atomic_claim_concurrent(self):
        """并发抢单：同一任务仅一个 worker 能 claim 成功（CC-3）"""
        tid = _enqueue(session_id="s1", runtime_id=1, seq=1)
        results = []
        lock = threading.Lock()

        def _w():
            r = service.claim(1)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_w) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        claimed = [r for r in results if r]
        assert len(claimed) == 1, f"同一任务并发抢单应仅 1 个成功, got {len(claimed)}"
        assert claimed[0]["id"] == tid


class TestTerminal:
    def test_complete_idempotent(self):
        tid = _enqueue(seq=1)
        service.claim(1)
        service.complete_task(tid, "done", 0)
        service.complete_task(tid, "done again", 0)
        t = service.get_task(tid)
        assert t["status"] == "completed"
        assert t["result_text"] == "done"

    def test_fail_sets_failed(self):
        tid = _enqueue(seq=1)
        service.claim(1)
        service.fail_task(tid, "boom", 1)
        t = service.get_task(tid)
        assert t["status"] == "failed"
        assert "boom" in t["errors"]

    def test_cancel(self):
        tid = _enqueue(seq=1)
        service.cancel_task(tid)
        t = service.get_task(tid)
        assert t["status"] == "cancelled"

    def test_unknown_task_noop(self):
        service.complete_task("t_nonexistent", "x", 0)
        service.fail_task("t_nonexistent", "x")
