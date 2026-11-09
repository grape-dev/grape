"""Issue #587 测试：任务终态级联事件收敛 + A2A 去重活跃键 + 回报可观测 + 审计留痕。

覆盖验收标准：
- 任务 completed/failed/cancelled 后关联事件级联收敛（cancel_task / update_task_status 路径）
- 指向已终态事件的同角色新交接不再被 duplicate_a2a 阻断（_has_active_a2a_event）
- 补偿收敛入口 converge_event_to_task / POST /tasks/{id}/converge-event（幂等）
- 回报失败可观测记录（_record_report_failure → agent_tasks.errors）
- 终止/重派操作审计留痕（admin_action 事件含 actor/time/target/动作）
- sweeper 超时探活：终态任务 → 事件映射收敛（cancelled 不再误标 failed）
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test587/%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test587/%'")
        db.commit()


REPO = "gitcode:test587/repo"


def _insert_event(seq, status="running", event_type="a2a_event", agent_type="coding",
                  issue_id="#587", scene_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, scene_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, REPO, event_type, status, agent_type, issue_id, scene_id,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def _get_event_status(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, result, completed_at FROM events WHERE seq=?", (seq,))
        row = c.fetchone()
    return row


def _insert_task(task_id, event_seq, status="dispatched", exit_code=None, created_at=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at, exit_code) "
            "VALUES (?, ?, 0, ?, '#587', ?, 'coding', 'p', ?, ?, ?)",
            (task_id, "sess-587", event_seq, REPO, status,
             created_at or datetime.now(timezone.utc).isoformat(), exit_code),
        )
        db.commit()


class TestTerminalCascade:
    """任务终态 → 事件终态级联收敛（Issue #587 核心修复）。"""

    def test_cancel_task_cascades_event_to_cancelled(self):
        _insert_event(5801, status="running")
        _insert_task("task-cancel-1", 5801, status="dispatched")
        service.cancel_task("task-cancel-1")
        assert service.get_task("task-cancel-1")["status"] == "cancelled"
        row = _get_event_status(5801)
        assert row[0] == "cancelled", f"事件应为 cancelled（级联收敛），实际={row[0]}"
        assert row[1] and "取消" in row[1]
        assert row[2], "级联收敛须写 completed_at"

    def test_update_task_status_failed_cascades_event_to_failed(self):
        _insert_event(5802, status="running")
        _insert_task("task-fail-1", 5802, status="running")
        service.update_task_status("task-fail-1", "failed", exit_code=1)
        row = _get_event_status(5802)
        assert row[0] == "failed", f"事件应为 failed（级联收敛），实际={row[0]}"
        assert row[2], "级联收敛须写 completed_at"

    def test_update_task_status_completed_cascades_event_to_completed(self):
        _insert_event(5803, status="running")
        _insert_task("task-done-1", 5803, status="running")
        service.update_task_status("task-done-1", "completed", exit_code=0)
        row = _get_event_status(5803)
        assert row[0] == "completed"

    def test_pending_event_also_converges(self):
        _insert_event(5804, status="pending")
        _insert_task("task-cancel-2", 5804, status="dispatched")
        service.cancel_task("task-cancel-2")
        assert _get_event_status(5804)[0] == "cancelled"

    def test_terminal_event_not_overwritten_by_cascade(self):
        # 事件已 completed（complete_task 详写路径先行）→ 级联 no-op，不覆盖
        _insert_event(5805, status="completed")
        _insert_task("task-late-1", 5805, status="running")
        service.update_task_status("task-late-1", "failed", exit_code=1)
        assert _get_event_status(5805)[0] == "completed", "已终态事件不得被另一终态覆盖"

    def test_non_terminal_status_no_cascade(self):
        _insert_event(5806, status="pending")
        _insert_task("task-run-1", 5806, status="dispatched")
        service.update_task_status("task-run-1", "running")
        assert _get_event_status(5806)[0] == "running"

    def test_cascade_releases_event_active_guard(self):
        """Issue #587 审查修复：级联收敛须同步释放 event_active_guard（不再靠 TTL 兜底）。"""
        from src.api.event_store import claim_event_guard, update_event_guard_seq
        _insert_event(5807, status="running")
        _insert_task("task-guard-1", 5807, status="dispatched")
        # 模拟派发时占用的活跃守卫（seq=0 占位后回填真实 seq）
        assert claim_event_guard(REPO, "#587", "coding")
        update_event_guard_seq(REPO, "#587", "coding", 5807)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM event_active_guard WHERE seq=?", (5807,))
            assert c.fetchone()[0] == 1, "前置：守卫行存在"
        service.cancel_task("task-guard-1")
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM event_active_guard WHERE seq=?", (5807,))
            assert c.fetchone()[0] == 0, "级联收敛后守卫须即时释放"
        assert _get_event_status(5807)[0] == "cancelled"


class TestConvergeCompensation:
    """补偿收敛入口（converge_event_to_task + 管理端端点）：幂等、可重复调用。"""

    def test_converge_after_cancel(self):
        _insert_event(5811, status="running")
        _insert_task("task-cc-1", 5811, status="cancelled", exit_code=None)
        res = service.converge_event_to_task("task-cc-1")
        assert res["status"] == "converged"
        assert res["event_status_after"] == "cancelled"
        assert _get_event_status(5811)[0] == "cancelled"

    def test_converge_idempotent(self):
        _insert_event(5812, status="running")
        _insert_task("task-cc-2", 5812, status="failed", exit_code=2)
        first = service.converge_event_to_task("task-cc-2")
        second = service.converge_event_to_task("task-cc-2")
        assert first["status"] == "converged"
        assert second["status"] == "noop", "重复补偿调用应幂等 no-op"

    def test_converge_rejects_non_terminal_task(self):
        _insert_event(5813, status="running")
        _insert_task("task-cc-3", 5813, status="running")
        res = service.converge_event_to_task("task-cc-3")
        assert res["status"] == "task_not_terminal"
        assert _get_event_status(5813)[0] == "running"

    def test_endpoint_converge_event(self):
        from src.api.routes.runtime import converge_event
        _insert_event(5814, status="running")
        _insert_task("task-ep-1", 5814, status="cancelled", exit_code=None)
        res = converge_event("task-ep-1", user={"id": 1, "username": "ops", "auth_type": "session_token"})
        assert res["status"] == "converged"
        assert _get_event_status(5814)[0] == "cancelled"

    def test_endpoint_converge_event_writes_audit_trail(self):
        """Issue #587 审查修复：补偿收敛端点与 terminate/redispatch 一致强制审计留痕。"""
        from src.api.routes.runtime import converge_event
        _insert_event(5815, status="running")
        _insert_task("task-ep-2", 5815, status="failed", exit_code=1)
        converge_event("task-ep-2", user={"id": 1, "username": "ops-auditor", "auth_type": "session_token"})
        with db_lock:
            c = db.cursor()
            c.execute("SELECT actor, payload FROM events WHERE repo_key=? AND event_type='admin_action' "
                      "AND json_extract(payload, '$.target_event_seq')=?", (REPO, 5815))
            rows = c.fetchall()
        assert len(rows) == 1, f"补偿收敛须留痕 1 条审计事件，实际 {len(rows)}"
        actor, payload = rows[0]
        assert actor == "ops-auditor"
        p = json.loads(payload)
        assert p["action"] == "converge-event"
        assert p["target_task_id"] == "task-ep-2"

    def test_endpoint_404_unknown_task(self):
        from fastapi import HTTPException
        from src.api.routes.runtime import converge_event
        with pytest.raises(HTTPException) as exc:
            converge_event("no-such-task", user={"id": 1, "username": "ops", "auth_type": "session_token"})
        assert exc.value.status_code == 404


class TestA2ADedupActiveKey:
    """A2A 去重判定键 = 事件活跃(running/dispatched)；已终态事件不阻断新交接。"""

    def test_terminal_event_does_not_block(self):
        from src.api.routes.webhook import _has_active_a2a_event
        for i, st in enumerate(("completed", "failed", "cancelled")):
            _insert_event(5820 + i, status=st)
            assert _has_active_a2a_event(REPO, "#587", "coding") == 0, \
                f"{st} 事件不应阻断新交接"

    def test_running_event_blocks(self):
        from src.api.routes.webhook import _has_active_a2a_event
        _insert_event(5821, status="running")
        assert _has_active_a2a_event(REPO, "#587", "coding") == 5821

    def test_dispatched_event_blocks(self):
        from src.api.routes.webhook import _has_active_a2a_event
        _insert_event(5822, status="dispatched")
        assert _has_active_a2a_event(REPO, "#587", "coding") == 5822

    def test_different_agent_does_not_block(self):
        from src.api.routes.webhook import _has_active_a2a_event
        _insert_event(5823, status="running", agent_type="qa")
        assert _has_active_a2a_event(REPO, "#587", "coding") == 0

    def test_scene_scoped_dedup(self):
        from src.api.routes.webhook import _has_active_a2a_event
        _insert_event(5824, status="running", scene_id="scene-a")
        assert _has_active_a2a_event(REPO, "#587", "", scene_id="scene-a") == 5824
        assert _has_active_a2a_event(REPO, "#587", "", scene_id="scene-b") == 0

    def test_cascaded_cancel_releases_dedup_block(self):
        """端到端语义：卡 running 事件经任务 cancel 级联收敛后不再阻断。"""
        from src.api.routes.webhook import _has_active_a2a_event
        _insert_event(5825, status="running")
        _insert_task("task-e2e-1", 5825, status="dispatched")
        assert _has_active_a2a_event(REPO, "#587", "coding") == 5825
        service.cancel_task("task-e2e-1")
        assert _has_active_a2a_event(REPO, "#587", "coding") == 0, \
            "任务 cancelled 级联收敛后，同角色新交接不得再被阻断"


class TestReportFailureObservability:
    """回报失败可观测记录（task_id/原因/重试次数）。"""

    def test_record_report_failure_writes_errors(self):
        _insert_task("task-rf-1", 5830, status="running")
        service._REPORT_FAILURE_COUNTS.pop("task-rf-1:messages", None)
        service._record_report_failure("task-rf-1", "messages", "db locked")
        task = service.get_task("task-rf-1")
        errs = json.loads(task["errors"])
        entry = next(e for e in errs if isinstance(e, dict)
                     and e.get("error_code") == "REPORT_INGESTION_FAILED")
        assert entry["reason"] == "db locked"
        assert entry["retries"] == 1
        assert entry["task_id"] == "task-rf-1"

    def test_record_report_failure_counts_retries(self):
        _insert_task("task-rf-2", 5831, status="running")
        for k in list(service._REPORT_FAILURE_COUNTS):
            if k.startswith("task-rf-2:"):
                service._REPORT_FAILURE_COUNTS.pop(k)
        service._record_report_failure("task-rf-2", "progress", "boom")
        service._record_report_failure("task-rf-2", "progress", "boom again")
        task = service.get_task("task-rf-2")
        errs = json.loads(task["errors"])
        entry = next(e for e in errs if isinstance(e, dict)
                     and e.get("error_code") == "REPORT_INGESTION_FAILED"
                     and e.get("kind") == "progress")
        assert entry["retries"] == 2, "重试计数须递增"
        assert sum(1 for e in errs if isinstance(e, dict)
                   and e.get("kind") == "progress") == 1, "同类失败只留一条（更新语义）"


class TestAdminActionAudit:
    """管理端终止/重派操作强制审计留痕（actor/time/target_event/动作）。"""

    def _audit_events(self, seq):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT actor, summary, payload, status FROM events "
                      "WHERE repo_key=? AND event_type='admin_action' AND "
                      "json_extract(payload, '$.target_event_seq')=?",
                      (REPO, seq))
            return c.fetchall()

    def test_terminate_writes_audit_trail(self):
        from src.api.routes.events import terminate_event
        _insert_event(5840, status="running", event_type="issue_comment")
        _insert_task("task-term-1", 5840, status="dispatched")
        terminate_event(5840, user={"id": 1, "username": "ops-auditor", "auth_type": "session_token"})
        rows = self._audit_events(5840)
        assert len(rows) == 1, f"终止操作须留痕 1 条审计事件，实际 {len(rows)}"
        actor, summary, payload, status = rows[0]
        assert actor == "ops-auditor"
        assert "terminate" in summary and "5840" in summary
        p = json.loads(payload)
        assert p["action"] == "terminate"
        assert p["target_event_seq"] == 5840
        assert p["time"], "审计须含 time"
        assert status == "completed", "审计事件不参与业务流转"

    def test_redispatch_writes_audit_trail(self):
        from src.api.routes.events import redispatch_event
        _insert_event(5841, status="failed", event_type="issue_comment")
        rows_before = len(self._audit_events(5841))
        try:
            redispatch_event(5841, user={"id": 1, "username": "ops-auditor", "auth_type": "session_token"})
        except Exception:
            pass  # 重派主流程可因环境缺依赖失败，但审计留痕必须发生
        rows = self._audit_events(5841)
        assert len(rows) > rows_before, "重派操作须写审计留痕"
        p = json.loads(rows[-1][2])
        assert p["action"] == "redispatch"


class TestSweeperTerminalMapping:
    """sweeper 超时探活：最近任务已终态 → 事件按任务终态映射收敛。"""

    def test_sweeper_maps_cancelled_task_to_cancelled_event(self):
        from src.runtime.cleanup import sweep_stuck_events_once
        # 事件 running 超时 + 最近任务已 cancelled → 事件收敛 cancelled（非 failed）
        _old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, "
                      "created_at, started_at) VALUES (?, ?, 'a2a_event', 'running', 'coding', '#587', ?, ?)",
                      (5850, REPO, _old, _old))
            db.commit()
        _insert_task("task-sw-1", 5850, status="cancelled", exit_code=None,
                     created_at=_old)
        sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        row = _get_event_status(5850)
        assert row[0] == "cancelled", f"终态任务(cancelled)的超时事件应映射收敛为 cancelled，实际={row[0]}"

    def test_sweeper_failed_task_converges_failed(self):
        from src.runtime.cleanup import sweep_stuck_events_once
        _old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, "
                      "created_at, started_at) VALUES (?, ?, 'a2a_event', 'running', 'coding', '#587', ?, ?)",
                      (5851, REPO, _old, _old))
            db.commit()
        _insert_task("task-sw-2", 5851, status="failed", exit_code=1, created_at=_old)
        sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert _get_event_status(5851)[0] == "failed"
