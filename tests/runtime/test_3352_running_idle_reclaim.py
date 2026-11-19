"""Issue #3352：complete 超时留下的僵尸 running 必须归还并发槽。

现象：complete 回报丢失（网关 504/断网）后任务停在 running，引擎在线，
`_real_active_count` 仍把它算作占槽 → 认领停滞；原实现要等 6h 长超时才回收。
修复：有会话 + 长时间无执行消息 → sweeper 按独立空闲阈值（默认 120min）判死并释放槽位。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import cleanup

RT = 9501
TID = "t_3352a"


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_3352%'")
        c.execute("DELETE FROM task_messages WHERE task_id LIKE 't_3352%'")
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RT,))
        db.commit()


def _seed(started_minutes_ago: float, last_message_minutes_ago: float | None):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO agent_runtimes (id, name, credential_id, status, "
                  "healthy, installed, busy_slots, max_concurrent) "
                  "VALUES (?,?,0,'online',1,1,1,4)", (RT, "idle-engine"))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, "
            "runtime_session_id, started_at) "
            "VALUES (?, 'sess3352', ?, '3352/repo', 'coding', 'issue_opened', 'running', "
            "'x','','{}','{}','#3352','sess-rt',?)",
            (TID, RT, _iso(started_minutes_ago)))
        if last_message_minutes_ago is not None:
            c.execute("INSERT INTO task_messages (task_id, seq, type, content, created_at) "
                      "VALUES (?,1,'text','progress',?)", (TID, _iso(last_message_minutes_ago)))
        db.commit()


def _row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_tasks WHERE id=?", (TID,))
        st = c.fetchone()[0]
        c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (RT,))
        return st, c.fetchone()[0]


@pytest.fixture(autouse=True)
def _reset():
    _clean()
    yield
    _clean()


def test_idle_running_is_reclaimed_and_slot_released():
    _seed(started_minutes_ago=180, last_message_minutes_ago=None)
    cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=180,
                                        dead_runtime_minutes=30,
                                        long_running_minutes=360,
                                        idle_running_minutes=120)
    status, busy = _row()
    assert status == "failed", "有会话但 3h 无消息的 running 必须被回收"
    assert busy == 0, "回收必须归还 busy_slots"


def test_recent_progress_not_killed():
    _seed(started_minutes_ago=180, last_message_minutes_ago=5)
    cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=180,
                                        dead_runtime_minutes=30,
                                        long_running_minutes=360,
                                        idle_running_minutes=120)
    status, busy = _row()
    assert status == "running", "5 分钟前还有执行消息，不能误杀"
    assert busy == 1


def test_below_idle_threshold_untouched():
    _seed(started_minutes_ago=100, last_message_minutes_ago=None)
    cleanup.sweep_stuck_agent_tasks_once(queued_stale_minutes=180,
                                        dead_runtime_minutes=30,
                                        long_running_minutes=360,
                                        idle_running_minutes=120)
    status, _busy = _row()
    assert status == "running", "未到空闲阈值不动（避免误伤慢启动任务）"