"""引擎池（Engine Pool）集群调度测试（agent-cluster-design.md §4.1/§4.2 / T1-T3/T6-T7）。"""
import json, threading
import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean_cluster_tables():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_type_runtime_pool")
        c.execute("DELETE FROM agent_runtimes")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM agent_type_runtime_pool")
        c.execute("DELETE FROM agent_runtimes")
        db.commit()


_RUNTIME_SEQ = [0]


def _mk_runtime(name=None, *, status="online", healthy=1, credential_id=None,
                busy_slots=0, max_concurrent=4, last_seen=None) -> int:
    """模拟引擎实例行（agent_runtimes），返回 id。每行 (credential_id, name) 唯一（多实例键）。"""
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


def _enqueue_pool(agent_type="coding", session_id="sess-1", issue_session_id="", affinity=0, seq=1):
    """入队池任务（runtime_id=0 + pool_agent_type）。"""
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=0, pool_agent_type=agent_type,
        affinity_runtime_id=affinity, prompt="pool task", event_seq=seq,
        issue_id="#5", repo_key="gitcode:test/repo", agent_type=agent_type,
        skill="", env_extra={"K": "v"}, issue_session_id=issue_session_id,
    )


def _enqueue_own(runtime_id, session_id="sess-x", seq=10):
    """入队确定路由任务（绑死引擎）。"""
    return service.enqueue_task(
        agent_session_id=session_id, runtime_id=runtime_id, prompt="own task",
        event_seq=seq, issue_id="#9", repo_key="gitcode:test/repo",
        agent_type="issue", skill="", env_extra={"K": "v"},
    )


# ===== T1 引擎池绑定 =====

class TestPoolBinding:
    def test_set_and_get_pool(self):
        a = _mk_runtime(); b = _mk_runtime()
        service.set_agent_pool("coding", [a, b], weight=2, priority=3)
        pool = service.get_agent_pool("coding")
        assert len(pool) == 2
        ids = {p["runtime_id"] for p in pool}
        assert ids == {a, b}
        assert all(p["weight"] == 2 and p["priority"] == 3 for p in pool)
        assert all(p["status"] == "online" for p in pool)

    def test_set_pool_replaces(self):
        a = _mk_runtime(); b = _mk_runtime()
        service.set_agent_pool("coding", [a])
        service.set_agent_pool("coding", [b])
        pool = service.get_agent_pool("coding")
        assert [p["runtime_id"] for p in pool] == [b]

    def test_clear_pool(self):
        a = _mk_runtime()
        service.set_agent_pool("coding", [a])
        service.set_agent_pool("coding", [])
        assert service.get_agent_pool("coding") == []
        assert service.pool_runtime_ids("coding") == []

    def test_pool_runtime_ids_only_enabled(self):
        a = _mk_runtime(); b = _mk_runtime()
        service.set_agent_pool("qa", [a, b])
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_type_runtime_pool SET enabled=0 WHERE runtime_id=?", (b,))
            db.commit()
        assert service.pool_runtime_ids("qa") == [a]

    def test_pool_isolated_by_agent_type(self):
        a = _mk_runtime(); b = _mk_runtime()
        service.set_agent_pool("coding", [a])
        service.set_agent_pool("qa", [b])
        assert service.pool_runtime_ids("coding") == [a]
        assert service.pool_runtime_ids("qa") == [b]


# ===== T2 空闲调度（池 claim）=====

class TestPoolClaim:
    def test_claim_pool_task_by_member(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_pool()
        got = service.claim(eng)
        assert got and got["id"] == tid
        # 池任务被领取后 runtime_id 定为实际引擎
        assert service.get_task(tid)["runtime_id"] == eng

    def test_claim_pool_task_skipped_by_non_member(self):
        eng_a = _mk_runtime(); eng_b = _mk_runtime()
        service.set_agent_pool("coding", [eng_a])
        tid = _enqueue_pool()
        # 非池内引擎 claim → 拿不到池任务
        assert service.claim(eng_b) is None
        # 池内引擎 claim → 拿到
        assert service.claim(eng_a)["id"] == tid

    def test_claim_own_task_still_works_for_member(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_own(eng)
        got = service.claim(eng)
        assert got and got["id"] == tid

    def test_claim_without_pool_unchanged(self):
        """无池引擎：只拿自己队列（回归锚点 T7.1）。"""
        eng = _mk_runtime()
        tid = _enqueue_own(eng)
        got = service.claim(eng)
        assert got and got["id"] == tid
        # 无池引擎拿不到任何池任务
        _enqueue_pool(agent_type="coding")
        assert service.claim(eng) is None

    def test_claim_any_idle_member_gets_pool_task(self):
        """池内任一引擎都能领取池任务（谁先 claim 谁拿，pull 模型）。"""
        eng_a = _mk_runtime(); eng_b = _mk_runtime()
        service.set_agent_pool("coding", [eng_a, eng_b])
        tid = _enqueue_pool()
        got = service.claim(eng_b)   # B 先来
        assert got and got["id"] == tid
        assert service.claim(eng_a) is None   # A 再来已无任务

    def test_claim_affinity_hit(self):
        """会话亲和：任务 affinity=B，B claim 时命中（affinity 软优先，B 能拿到）。"""
        eng_a = _mk_runtime(); eng_b = _mk_runtime()
        service.set_agent_pool("coding", [eng_a, eng_b])
        tid = _enqueue_pool(affinity=eng_b)
        got = service.claim(eng_b)
        assert got and got["id"] == tid and got["runtime_id"] == eng_b

    def test_claim_affinity_offline_falls_back(self):
        """亲和引擎离线 → 池内其他引擎降级领取（换机新会话可执行）。"""
        eng_a = _mk_runtime()
        eng_b = _mk_runtime(status="offline")
        service.set_agent_pool("coding", [eng_a, eng_b])
        tid = _enqueue_pool(affinity=eng_b)
        got = service.claim(eng_a)
        assert got and got["id"] == tid

    def test_claim_serial_across_engines(self):
        """同 issue_session_id 已有活跃任务 → 池内其他引擎也不得领取（跨引擎串行 T2.9）。"""
        eng_a = _mk_runtime(); eng_b = _mk_runtime()
        service.set_agent_pool("coding", [eng_a, eng_b])
        t1 = _enqueue_pool(issue_session_id="is-1", seq=1)
        t2 = _enqueue_pool(issue_session_id="is-1", seq=2)
        assert service.claim(eng_a)["id"] == t1
        # 同 Issue 第二个任务：B 空闲也不得领取
        assert service.claim(eng_b) is None

    def test_claim_parallel_across_issues(self):
        eng_a = _mk_runtime(); eng_b = _mk_runtime()
        service.set_agent_pool("coding", [eng_a, eng_b])
        t1 = _enqueue_pool(issue_session_id="is-1", seq=1)
        t2 = _enqueue_pool(issue_session_id="is-2", seq=2)
        got_a = service.claim(eng_a)
        got_b = service.claim(eng_b)
        assert {got_a["id"], got_b["id"]} == {t1, t2}

    def test_claim_skips_disabled_pool_binding(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_type_runtime_pool SET enabled=0 WHERE runtime_id=?", (eng,))
            db.commit()
        _enqueue_pool()
        assert service.claim(eng) is None

    def test_all_busy_pool_keeps_queued(self):
        """池满（busy_slots >= max_concurrent）不硬限流：仍可 claim（边端并发控制），任务不丢。"""
        eng = _mk_runtime(busy_slots=4, max_concurrent=4)
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_pool()
        got = service.claim(eng)
        assert got and got["id"] == tid   # 任务被领取而非丢失/失败


# ===== T3 槽位计数 =====

class TestBusySlots:
    def test_increment_on_claim(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        _enqueue_pool()
        service.claim(eng)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 1

    def test_decrement_on_complete(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_pool()
        service.claim(eng)
        service.complete_task(tid, "done", exit_code=0)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 0

    def test_decrement_on_fail(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_pool()
        service.claim(eng)
        service.fail_task(tid, "boom")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 0

    def test_never_negative(self):
        eng = _mk_runtime(busy_slots=0)
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_pool()
        service.claim(eng)
        # 重复 complete（终态幂等）不会重复扣减
        service.complete_task(tid, "done", exit_code=0)
        service.complete_task(tid, "done", exit_code=0)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (eng,))
            assert c.fetchone()[0] == 0


# ===== T6 兜底链 / T7 兼容 =====

class TestFallbackCompat:
    def test_enqueue_requires_session(self):
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="", runtime_id=0, pool_agent_type="coding", prompt="p")

    def test_enqueue_requires_runtime_or_pool(self):
        with pytest.raises(ValueError):
            service.enqueue_task(agent_session_id="s", runtime_id=0, pool_agent_type="", prompt="p")

    def test_pool_task_snapshot_fields(self):
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        tid = _enqueue_pool(affinity=eng)
        t = service.get_task(tid)
        assert t["runtime_id"] == 0 and t["pool_agent_type"] == "coding"
        assert t["affinity_runtime_id"] == eng
        assert t["status"] == "queued"

    def test_pool_clear_falls_back_to_own_queue(self):
        """池被清空后：池任务不可再被领取（等待/兜底），普通任务不受影响。"""
        eng = _mk_runtime()
        service.set_agent_pool("coding", [eng])
        _enqueue_pool()
        service.set_agent_pool("coding", [])   # 清池
        assert service.claim(eng) is None      # 池任务不再被领取
        tid = _enqueue_own(eng)                # 自己的任务照常
        assert service.claim(eng)["id"] == tid


# ===== C1 并发原子性 =====

class TestConcurrentClaim:
    def test_concurrent_claim_no_dup_no_loss(self):
        """10 线程同时 claim 池内 10 个任务 → 每任务恰被领取一次。"""
        engs = [_mk_runtime() for _ in range(2)]
        service.set_agent_pool("coding", engs)
        tids = set()
        for i in range(10):
            tids.add(_enqueue_pool(session_id=f"sess-{i}", seq=i))
        got_ids = []
        lock = threading.Lock()

        def _worker(eid):
            while True:
                t = service.claim(eid)
                if not t:
                    return
                with lock:
                    got_ids.append(t["id"])

        threads = [threading.Thread(target=_worker, args=(eid,)) for eid in engs * 5]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert set(got_ids) == tids, "每个任务恰被领取一次（无重复、无丢失）"
        assert len(got_ids) == 10
