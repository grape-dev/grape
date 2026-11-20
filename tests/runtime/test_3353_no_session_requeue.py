"""Issue #3353：认领后从未建会话的任务先原地重派，超限才判死。

背景：worker 进程认领任务后被重启/杀掉，任务停在 running（runtime_session_id 为空、
无 task_messages），平台判死后不再重派，流水线永久停在该节点。修复后：
- attempts < CLAIM_NO_SESSION_MAX_RETRIES → status=queued、started_at=NULL、attempts+1、
  事件回 pending、归还 busy_slot（可被原引擎重新认领）；
- 超限 → 保持原逻辑判 failed 并级联事件。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import sweep_no_session as sns

RT = 9401


def _iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_3353%'")
        c.execute("DELETE FROM task_messages WHERE task_id LIKE 't_3353%'")
        c.execute("DELETE FROM events WHERE seq=9433")
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RT,))
        db.commit()


def _seed(attempts: int = 0):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RT,))
        c.execute("INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, "
                  "installed, busy_slots, max_concurrent) VALUES (?,?,0,'online',1,1,1,4)",
                  (RT, "no-session-engine"))
        c.execute("DELETE FROM agent_tasks WHERE id='t_3353a'")
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, repo_key, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, issue_id, "
            "runtime_session_id, started_at, attempts) "
            "VALUES ('t_3353a','sess3353',?,9433,'3353/repo','coding','issue_opened','running',"
            "'x','','{}','{}','#3353','',?,?)",
            (RT, _iso(5), attempts))
        c.execute("INSERT OR REPLACE INTO events (seq, repo_key, event_type, status) "
                  "VALUES (9433, 'gitcode:3353/repo', 'issue_opened', 'running')")
        db.commit()


def _task_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, attempts, started_at FROM agent_tasks WHERE id='t_3353a'")
        return c.fetchone()


def _busy_slots():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (RT,))
        return c.fetchone()[0]


@pytest.fixture(autouse=True)
def _reset():
    _clean()
    yield
    _clean()


def test_first_strike_requeues_instead_of_failing():
    _seed(attempts=0)
    sns.sweep_claim_without_session_once()

    status, attempts, started_at = _task_row()
    assert status == "queued", "首次无会话必须回队重派，而不是直接判死"
    assert int(attempts) == 1
    assert started_at is None, "回队后必须清空 started_at，否则认领窗口判据错乱"
    assert _busy_slots() == 0, "回队必须归还引擎并发槽"
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM events WHERE seq=9433")
        assert c.fetchone()[0] == "pending", "事件必须回到 pending，否则页面仍显示执行中"


def test_exhausted_attempts_fail_as_before():
    _seed(attempts=sns.MAX_NO_SESSION_RETRIES)
    sns.sweep_claim_without_session_once()

    status, _attempts, _started = _task_row()
    assert status == "failed", "重试次数用尽后必须落终态"
    assert _busy_slots() == 0
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM events WHERE seq=9433")
        assert c.fetchone()[0] == "failed"


def test_requeued_task_not_touched_again():
    _seed(attempts=0)
    sns.sweep_claim_without_session_once()
    # 第二次扫描：任务已是 queued，不在 running/dispatched 候选集里，attempts 不应再涨
    sns.sweep_claim_without_session_once()
    status, attempts, _started = _task_row()
    assert status == "queued"
    assert int(attempts) == 1, "已回队的任务不能被重复计数"