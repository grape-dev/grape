"""Runtime 确定路由 claim / 兜底回收 / 槽位测试（engine-selection-design.md v2）。

claim-based 引擎池已弃用（agent_type_runtime_pool / pool_agent_type），
本文件仅覆盖仍有效的确定路由（runtime_id>0）行为：claim、超时回收、busy_slots。
"""
import threading
import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean_cluster_tables():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_runtimes")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_runtimes")
        db.commit()


def _mk_runtime(name=None, *, status="online", healthy=1, credential_id=None,
                busy_slots=0, max_concurrent=4, last_seen=None) -> int:
    """模拟引擎实例行（agent_runtimes），返回 id。"""
    import datetime
    _RUNTIME_SEQ[0] += 1
    n = _RUNTIME_SEQ[0]
    name = name or f"opencode-w{n}"
    cid = credential_id if credential_id is not None else n
    ls = last_seen if last_seen is not None else datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (credential_id, name, status, healthy, busy_slots, max_concurrent, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?, 'global', '')",
            (cid, name, status, healthy, busy_slots, max_concurrent, ls))
        rid = c.lastrowid
        db.commit()
    return rid


_RUNTIME_SEQ = [0]


def _enqueue_own(runtime_id, session_id="sess-x", seq=10):
    """入队确定路由任务（绑死引擎）。"""
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt="own task",
        event_seq=seq, issue_id="#9", repo_key="gitcode:test/repo",
        agent_type="issue", skill="", env_extra={"K": "v"},
    )


# ===== M1 兜底回收（dispatched 超时自动重排 + attempts 上限）=====

class TestStaleRecovery:
    """M1 兜底回收：dispatched 超时自动重排 + attempts 上限（防 worker 丢弃死循环）。"""

    @staticmethod
    def _stale_dispatched(attempts=0, age_minutes=5):
        from datetime import datetime, timezone, timedelta
        eng = _mk_runtime()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_stale%'")
            c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, status, prompt, attempts, started_at) VALUES ('t_stale1','s1',?,1,'dispatched','p',?,?)",
                      (eng, attempts, (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()))
            db.commit()
        return eng

    def test_stale_dispatched_recovered_on_claim(self):
        """超时 dispatched（5min 前未 start）→ claim 时回收回 queued，attempts+1，槽位归还。"""
        eng = self._stale_dispatched(attempts=0)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET busy_slots=1, status='online' WHERE id=?", (eng,))
            db.commit()
        service.claim(eng)
        t = service.get_task("t_stale1")
        assert t["status"] == "queued" or t["status"] == "dispatched", t["status"]
        assert t["attempts"] == 1, t["attempts"]
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            bs = c.fetchone()[0]
        # 回收归还槽位(1→0)；若 claim 随即领走回池任务则 +1(→1)，二者均合法
        assert bs in (0, 1), f"busy_slots={bs}"

    def test_stale_dispatched_attempts_limit_fails(self):
        """attempts=2 的超时 dispatched → 回收时直接 failed（回收超限）。"""
        eng = self._stale_dispatched(attempts=2)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET busy_slots=1, status='online' WHERE id=?", (eng,))
            db.commit()
        service.claim(eng)
        t = service.get_task("t_stale1")
        assert t["status"] == "failed", t["status"]
        assert t["attempts"] == 3, t["attempts"]

    def test_fresh_dispatched_not_recovered(self):
        """新鲜 dispatched（10s 前）→ 不被回收。"""
        eng = _mk_runtime()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_stale%'")
            from datetime import datetime, timezone, timedelta
            c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, status, prompt, attempts, started_at) VALUES ('t_stale2','s1',?,1,'dispatched','p',0,?)",
                      (eng, (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()))
            db.commit()
        service.claim(eng)
        t = service.get_task("t_stale2")
        assert t["status"] == "dispatched", t["status"]


# ===== 确定路由 claim =====

class TestDeterministicClaim:
    def test_claim_own_task(self):
        eng = _mk_runtime()
        tid = _enqueue_own(eng)
        got = service.claim(eng)
        assert got and got["id"] == tid
        assert service.get_task(tid)["runtime_id"] == eng

    def test_claim_isolated_per_instance(self):
        eng_a = _mk_runtime(); eng_b = _mk_runtime()
        t1 = _enqueue_own(eng_a, session_id="s-a", seq=1)
        t2 = _enqueue_own(eng_b, session_id="s-b", seq=2)
        assert service.claim(eng_a)["id"] == t1
        assert service.claim(eng_a) is None   # 队列已空
        assert service.claim(eng_b)["id"] == t2

    def test_claim_serial_by_issue_session(self):
        eng = _mk_runtime()
        t1 = service.enqueue_task(
            agent_session_id="s1", runtime_id=eng, prompt="p1", event_seq=1,
            issue_id="#5", repo_key="gitcode:test/repo", agent_type="issue",
            skill="", issue_session_id="is-1")
        t2 = service.enqueue_task(
            agent_session_id="s1", runtime_id=eng, prompt="p2", event_seq=2,
            issue_id="#5", repo_key="gitcode:test/repo", agent_type="issue",
            skill="", issue_session_id="is-1")
        assert service.claim(eng)["id"] == t1
        assert service.claim(eng) is None   # 同 Issue 串行


# ===== T3 槽位计数 =====

class TestBusySlots:
    def test_increment_on_claim(self):
        eng = _mk_runtime()
        _enqueue_own(eng)
        service.claim(eng)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 1

    def test_decrement_on_complete(self):
        eng = _mk_runtime()
        tid = _enqueue_own(eng)
        service.claim(eng)
        service.complete_task(tid, "done", exit_code=0)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 0

    def test_decrement_on_fail(self):
        eng = _mk_runtime()
        tid = _enqueue_own(eng)
        service.claim(eng)
        service.fail_task(tid, "boom")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 0

    def test_never_negative(self):
        eng = _mk_runtime(busy_slots=0)
        tid = _enqueue_own(eng)
        service.claim(eng)
        service.complete_task(tid, "done", exit_code=0)
        service.complete_task(tid, "done", exit_code=0)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 0


# ===== 入队校验 =====

class TestEnqueueValidation:
    def test_enqueue_requires_session(self):
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="", runtime_id=1, prompt="p")

    def test_enqueue_requires_runtime(self):
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="s", runtime_id=0, prompt="p")


# ===== C1 并发原子性（确定路由）=====

class TestConcurrentClaim:
    def test_concurrent_claim_no_dup_no_loss(self):
        """10 线程同时 claim 池内 10 个任务 → 每任务恰被领取一次。"""
        eng = _mk_runtime()
        tids = set()
        for i in range(10):
            tids.add(_enqueue_own(eng, session_id=f"sess-{i}", seq=i))
        got_ids = []
        lock = threading.Lock()

        def _worker():
            while True:
                t = service.claim(eng)
                if not t:
                    return
                with lock:
                    got_ids.append(t["id"])

        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert set(got_ids) == tids, "每个任务恰被领取一次（无重复、无丢失）"
        assert len(got_ids) == 10
