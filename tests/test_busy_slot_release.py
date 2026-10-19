"""busy_slots 统一递减回归测试（local-engine-busy-slots-zombie-fix-design.md 方案 B）。

背景（#256）：本机引擎 busy_slots 残留僵尸计数 → local_claimer/claim_by_tag 误判满载 →
队列卡死、流水线节点不亮。修复：所有「任务不再占用引擎槽位」的路径统一收敛到
`release_busy_slot`（防越界），保证终态/requeue/回收后计数恒等于实际 running/dispatched 数。
"""
import pytest

from src.session_manager import db, db_lock
from src.runtime import service as svc

RT = 9001


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "agent_runtimes", "events"):
            try:
                c.execute(f"DELETE FROM {t} WHERE runtime_id={RT} OR id LIKE 't_busy%' "
                          "OR repo_key='busy/test'")
                c.execute(f"DELETE FROM {t} WHERE runtime_id IS NOT NULL AND runtime_id>0 "
                          "AND (repo_key='busy/test' OR id LIKE 't_busy%')")
            except Exception:
                pass
        db.commit()


def _seed_runtime(busy: int, maxc: int = 4):
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RT,))
        if "busy_slots" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN busy_slots INTEGER DEFAULT 0")
        if "max_concurrent" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN max_concurrent INTEGER DEFAULT 4")
        c.execute("INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
                  "busy_slots, max_concurrent) VALUES (?,?,0,'online',1,1,?,?)",
                  (RT, "busy-opencode", busy, maxc))
        db.commit()


def _seed_task(tid: str, status: str, rid: int = RT):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, event_type, "
            "status, prompt, cwd, skill_files, env_extra, issue_id) "
            "VALUES (?,?,?,?, 'vod', 'issue_opened', ?, 'x', '', '{}', '{}', '#0')",
            (tid, "sess-" + tid, rid, "busy/test", status))
        db.commit()


def _seed_running_with_rid(tid: str, rid: int):
    """构造 running 任务绑定指定 runtime（供回收测试用，不受默认 RT 约束）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
            "busy_slots, max_concurrent) VALUES (?,?,0,'offline',1,1,1,4)",
            (rid, f"busy-rid-{rid}"))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, event_type, "
            "status, prompt, cwd, skill_files, env_extra, issue_id, created_at, started_at) "
            "VALUES (?,?,?,?, 'issue', 'a2a_event', 'running', 'x', '', '{}', '{}', '#0', "
            "'2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (tid, "sess-" + tid, rid, "busy/test"))
        db.commit()


def _busy() -> int:
    with db_lock:
        r = db.cursor().execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (RT,)).fetchone()
        return r[0] if r else -1


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


class TestReleaseBusySlot:
    def test_decrements(self):
        _seed_runtime(busy=2)
        svc.release_busy_slot(RT)
        assert _busy() == 1
        svc.release_busy_slot(RT)
        assert _busy() == 0

    def test_never_negative(self):
        _seed_runtime(busy=0)
        svc.release_busy_slot(RT)
        svc.release_busy_slot(RT)
        assert _busy() == 0

    def test_none_rid_noop(self):
        svc.release_busy_slot(None)


class TestTerminalPathsReleaseSlot:
    def test_update_task_status_completed_releases(self):
        _seed_runtime(busy=1)
        _seed_task("t_busy1", "running")
        svc.update_task_status("t_busy1", "completed")
        assert _busy() == 0

    def test_update_task_status_failed_releases(self):
        _seed_runtime(busy=1)
        _seed_task("t_busy2", "running")
        svc.update_task_status("t_busy2", "failed")
        assert _busy() == 0

    def test_requeue_task_releases(self):
        _seed_runtime(busy=1)
        _seed_task("t_busy3", "running")
        assert svc.requeue_task("t_busy3") is True
        assert _busy() == 0
        with db_lock:
            st = db.cursor().execute("SELECT status FROM agent_tasks WHERE id='t_busy3'").fetchone()[0]
        assert st == "queued"

    def test_consecutive_tasks_no_leak(self):
        """UT-06：串行 N 个任务全部终态后 busy_slots 归零（无泄漏）。"""
        _seed_runtime(busy=0)
        for i in range(5):
            tid = f"t_busy_loop{i}"
            _seed_task(tid, "running")
            svc.update_task_status(tid, "completed")
            assert _busy() == 0, f"第 {i+1} 个任务终态后出现泄漏"


class TestAutopilotFinalizeReleasesSlot:
    """UT-07（L1）：autopilot 终态（_finalize_autopilot_task）必须释放槽位。

    修复前：scheduler._finalize_autopilot_task 直接 UPDATE agent_tasks 置终态，
    绕过 update_task_status → 无 release_busy_slot → busy_slots 泄漏。
    """

    def test_autopilot_fail_releases_slot(self):
        from src.runtime.scheduler import _finalize_autopilot_task
        _seed_runtime(busy=1)
        _seed_task("t_busy_auto_fail", "running")
        _finalize_autopilot_task("t_busy_auto_fail", 0, exit_code=1, final_text="boom")
        assert _busy() == 0, "autopilot failed 终态后 busy_slots 应释放"

    def test_autopilot_success_releases_slot(self):
        from src.runtime.scheduler import _finalize_autopilot_task
        _seed_runtime(busy=1)
        _seed_task("t_busy_auto_ok", "running")
        _finalize_autopilot_task("t_busy_auto_ok", 0, exit_code=0, final_text="ok")
        assert _busy() == 0, "autopilot success 终态后 busy_slots 应释放"

    def test_autopilot_finalize_sets_terminal(self):
        from src.runtime.scheduler import _finalize_autopilot_task
        _seed_runtime(busy=1)
        _seed_task("t_busy_auto_t", "running")
        _finalize_autopilot_task("t_busy_auto_t", 0, exit_code=1, final_text="x")
        with db_lock:
            st = db.cursor().execute("SELECT status FROM agent_tasks WHERE id='t_busy_auto_t'").fetchone()[0]
        assert st == "failed"


class TestSweeperRecycleReleasesSlot:
    """UT-08（L2）：sweeper 兜底回收 running/dispatched 任务必须释放槽位。

    修复前：cleanup.sweep_stuck_agent_tasks_once 直接 UPDATE agent_tasks 置 failed，
    对已占槽任务（running/dispatched）无 release_busy_slot → busy_slots 泄漏。
    """

    def test_sweeper_running_recycle_releases(self):
        from src.runtime import cleanup
        rid = 9002
        _seed_running_with_rid("t_busy_swp1", rid)
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET busy_slots=3 WHERE id=?", (rid,))
            db.commit()
        cleanup.sweep_stuck_agent_tasks_once(
            queued_stale_minutes=-1, dead_runtime_minutes=0, long_running_minutes=0)
        with db_lock:
            bs = c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
            st = c.execute("SELECT status FROM agent_tasks WHERE id='t_busy_swp1'").fetchone()[0]
        assert st == "failed"
        assert bs == 2, f"回收 running 任务后 busy_slots 应 -1, got {bs}"

    def test_sweeper_queued_task_no_release(self):
        """queued（runtime_id=0）任务不占槽 → 回收时不得把 0 减成负数/误减。"""
        from src.runtime import cleanup
        rid = 9003
        _seed_runtime(busy=1)  # RT=9001 占槽 1
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id='t_busy_swp_q'")
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, event_type, "
                "status, prompt, cwd, skill_files, env_extra, issue_id, created_at) "
                "VALUES ('t_busy_swp_q','s',0,'busy/test','vod','issue_opened','queued','x','','{}','{}','#0',"
                "'2020-01-01T00:00:00+00:00')")
            db.commit()
        cleanup.sweep_stuck_agent_tasks_once(
            queued_stale_minutes=-1, dead_runtime_minutes=0, long_running_minutes=0)
        assert _busy() == 1, "queued(runtime_id=0) 任务回收不应改变 busy_slots"