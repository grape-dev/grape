"""fail_task 幂等 / 过期上报丢弃 回归测试。

背景（2026-10-19 补报重放事故）：
- worker 补报队列每 10s 重放同一条 failed 上报（worker 侧 `RuntimeClient.fail` 漏 return
  导致条目永不出队，已在 test_worker_report_queue.TestRuntimeClientAck 侧守住）；
- 服务端 `fail_task` 对 `failed→failed` 不幂等，且不识别「已被重派取代的旧任务上报」，
  于是某次重放恰好落在人工 redispatch（reset_event_terminal 把事件置回 running）之后，
  把事件又写成 failed + 旧失败原因 → 页面显示「重派后仍失败，原因还是上次那个」。

本文件守住三条：
1. 已 failed 的 task 重复 fail → 幂等跳过，不动事件；
2. 同 event_seq 存在更晚创建的任务（重派/回炉重跑）→ 旧任务的 fail 上报被丢弃，
   任务与事件状态都不回写；
3. 正常路径（无更新兄弟任务）仍照旧：task → failed，事件 → failed（不回归）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


def _wipe():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test/%'")
        db.commit()


def _insert_event(seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status) VALUES (?, ?, ?, ?)",
            (seq, "gitcode:test/repo", "issue_opened", status),
        )
        db.commit()


def _insert_task(task_id, event_seq, status="running", created_offset_s=0, repo_key="gitcode:test/repo"):
    """created_offset_s：相对现在的秒偏移（负数=更早创建）。"""
    ts = (datetime.now(timezone.utc) + timedelta(seconds=created_offset_s)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "sess-1", 7, event_seq, "#3", repo_key, "vod", "p", status, ts),
        )
        db.commit()


def _event_status(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
    return row[0] if row else None


def _task_status(tid):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM agent_tasks WHERE id=?", (tid,))
        row = c.fetchone()
    return row[0] if row else None


class TestFailTaskIdempotent:
    def test_already_failed_skips_and_leaves_event_running(self):
        """重放场景：task 已 failed、事件被人工重派 reset 回 running → 不得再写回 failed。"""
        _insert_event(590, status="running")
        _insert_task("t_old", 590, status="failed", created_offset_s=-120)

        service.fail_task("t_old", "engine errors: invalid completion (replayed)", exit_code=-1)

        assert _event_status(590) == "running", "已 failed 任务的重复上报不得覆盖事件状态"
        assert _task_status("t_old") == "failed"

    def test_stale_report_discarded_when_newer_sibling_exists(self):
        """旧任务（已被重派取代，服务端仍认为它在跑）的上报 → 丢弃，不回写任何状态。"""
        _insert_event(590, status="running")
        _insert_task("t_old", 590, status="running", created_offset_s=-120)
        _insert_task("t_new", 590, status="queued", created_offset_s=-1)  # redispatch 产生的新任务

        service.fail_task("t_old", "engine errors: old report", exit_code=-1)

        assert _task_status("t_old") == "running", "过期上报不得把旧任务改成 failed"
        assert _event_status(590) == "running", "过期上报不得回写事件终态（重派后的 running 要保住）"

    def test_normal_fail_still_works(self):
        """不回归：没有更新兄弟任务时，fail 照旧把 task 与事件置 failed。"""
        _insert_event(591, status="running")
        _insert_task("t_only", 591, status="running", created_offset_s=-5)

        service.fail_task("t_only", "engine crashed", exit_code=-1)

        assert _task_status("t_only") == "failed"
        assert _event_status(591) == "failed"

    def test_same_task_twice_is_idempotent(self):
        """连续两次上报同一 task：第二次不得改变已落库的失败次数/状态（幂等）。"""
        _insert_event(592, status="running")
        _insert_task("t_twice", 592, status="running", created_offset_s=-5)

        service.fail_task("t_twice", "first error", exit_code=-1)
        service.fail_task("t_twice", "second error (replay)", exit_code=-1)

        assert _task_status("t_twice") == "failed"
        assert _event_status(592) == "failed"

    def test_no_event_seq_does_not_crash(self):
        """task 没有 event_seq（历史行/NULL）时守卫不得炸。"""
        _insert_task("t_noevt", None, status="running", created_offset_s=-5)
        service.fail_task("t_noevt", "boom", exit_code=-1)
        assert _task_status("t_noevt") == "failed"


class TestTaskCreatedKey:
    """created_at 混写格式下比较键的稳健性（ISO 带时区 vs 本地空格格式）。"""

    def test_iso_with_tz_beats_local_space_format(self):
        iso = service._task_created_key("2026-10-19T09:42:07.122554+00:00")
        loc = service._task_created_key("2026-10-19 09:40:05")
        assert iso > loc

    def test_ordering_within_same_format(self):
        a = service._task_created_key("2026-10-19T09:40:05.576337+00:00")
        b = service._task_created_key("2026-10-19T09:42:07.122554+00:00")
        assert b > a

    def test_empty_and_garbage(self):
        assert service._task_created_key("") == (0, 0.0, "")
        assert service._task_created_key(None)[0] == 0
        assert service._task_created_key("garbage")[0] == 0
