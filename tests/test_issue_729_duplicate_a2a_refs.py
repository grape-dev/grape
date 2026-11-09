"""Issue #729 测试：duplicate_a2a 拦截响应携带原任务产物引用。

覆盖验收标准：
- 同 issue 同 agent 重复 A2A 拦截响应含 original_event.task_ref / result_ref
- 跨流水线 duplicate_handoff 拦截返回值含 original_event 引用
- result pending 边界：原任务在途 → result_ref.status=pending（仅 task_ref 可轮询）
- result available：原任务有结论 → result_ref.status=available + 脱敏摘要
- 无关联任务 / 事件缺失 → unavailable 显式标注，不阻断拦截响应
"""
import json
from datetime import datetime, timezone

import pytest

from src.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test729/%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test729/%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test729/%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test729/%'")
        db.commit()


REPO = "gitcode:test729/repo"


def _insert_event(seq, status="running", event_type="a2a_event", agent_type="coding",
                  issue_id="#729", scene_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, scene_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, REPO, event_type, status, agent_type, issue_id, scene_id,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def _insert_task(task_id, event_seq, status="running", result_text="", created_at=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at, result_text) "
            "VALUES (?, ?, 0, ?, '#729', ?, 'coding', 'p', ?, ?, ?)",
            (task_id, "sess-729", event_seq, REPO, status,
             created_at or datetime.now(timezone.utc).isoformat(), result_text),
        )
        db.commit()


class TestOriginalRefsHelper:
    """_duplicate_a2a_original_refs：原事件 + 任务引用回查。"""

    def test_pending_boundary_running_task(self):
        """result pending 边界：原任务在途 → result_ref.status=pending，仅 task_ref 可轮询。"""
        from src.api.routes.webhook import _duplicate_a2a_original_refs
        _insert_event(7301, status="running", agent_type="coding")
        _insert_task("task-729-run", 7301, status="running", result_text="")
        refs = _duplicate_a2a_original_refs(REPO, 7301)
        assert refs["seq"] == 7301
        assert refs["agent"] == "coding"
        assert refs["status"] == "running"
        assert refs["issue_id"] == "#729"
        assert refs["created_at"]
        tr = refs["task_ref"]
        assert tr["task_id"] == "task-729-run"
        assert tr["task_status"] == "running"
        assert tr["query_path"] == "/api/runtime/tasks/task-729-run/status"
        rr = refs["result_ref"]
        assert rr["status"] == "pending"
        assert "summary" not in rr, "pending 时不应携带摘要"

    def test_available_with_summary(self):
        """原任务有结论 → available + 脱敏摘要。"""
        from src.api.routes.webhook import _duplicate_a2a_original_refs
        _insert_event(7302, status="running", agent_type="qa")
        _insert_task("task-729-done", 7302, status="completed",
                     result_text=json.dumps({"conclusion": "审查通过，可合并", "actions": []}))
        refs = _duplicate_a2a_original_refs(REPO, 7302)
        assert refs["task_ref"]["task_id"] == "task-729-done"
        assert refs["result_ref"]["status"] == "available"
        assert "审查通过" in refs["result_ref"]["summary"]

    def test_unavailable_when_no_task(self):
        """无关联任务 → task_ref/result_ref 显式 unavailable。"""
        from src.api.routes.webhook import _duplicate_a2a_original_refs
        _insert_event(7303, status="running")
        refs = _duplicate_a2a_original_refs(REPO, 7303)
        assert refs["seq"] == 7303
        assert refs["task_ref"]["task_status"] == "unavailable"
        assert refs["task_ref"]["query_path"] == ""
        assert refs["result_ref"]["status"] == "unavailable"

    def test_event_missing_still_returns_unavailable(self):
        """事件缺失 → 不阻断，seq 保留 + unavailable 引用。"""
        from src.api.routes.webhook import _duplicate_a2a_original_refs
        refs = _duplicate_a2a_original_refs(REPO, 7399)
        assert refs["seq"] == 7399
        assert refs["task_ref"]["task_status"] == "unavailable"
        assert refs["result_ref"]["status"] == "unavailable"

    def test_sensitive_token_redacted_in_summary(self):
        """摘要脱敏：token 类模式不出现在 result_ref.summary。"""
        from src.api.routes.webhook import _duplicate_a2a_original_refs
        _insert_event(7304, status="running")
        _insert_task("task-729-leak", 7304, status="completed",
                     result_text="done with token tk_abcdef1234567890abcd")
        refs = _duplicate_a2a_original_refs(REPO, 7304)
        summary = refs["result_ref"].get("summary", "")
        assert "tk_abcdef1234567890" not in summary
        assert "REDACTED" in summary


class TestSameAgentDedupResponse:
    """同 agent duplicate_a2a 拦截响应体：original_event 透传（向后兼容）。"""

    def test_skip_response_contains_original_event(self):
        from src.api.routes.webhook import _duplicate_a2a_skip_response
        _insert_event(7305, status="running", agent_type="sre")
        _insert_task("task-729-sre", 7305, status="dispatched")
        resp = _duplicate_a2a_skip_response("sre", REPO, 7305)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["skipped"] == "duplicate_a2a"
        assert body["existing_seq"] == 7305
        assert body["agent"] == "sre"
        oe = body["original_event"]
        assert oe["seq"] == 7305
        assert oe["task_ref"]["task_id"] == "task-729-sre"
        assert oe["task_ref"]["query_path"] == "/api/runtime/tasks/task-729-sre/status"
        assert oe["result_ref"]["status"] == "pending"


class TestCrossPipelineDedup:
    """跨流水线 duplicate_handoff 拦截：返回值携带 original_event。"""

    def test_duplicate_handoff_returns_original_event(self):
        from src.api.routes.webhook import _cross_pipeline_handoff
        _insert_event(7306, status="running", agent_type="issue", scene_id="default")
        _insert_task("task-729-cross", 7306, status="completed",
                     result_text=json.dumps({"conclusion": "Spec 已确认，交办 coding"}))
        res = _cross_pipeline_handoff(
            REPO, "729", "default", "vod",
            {}, "", None, "ctx", "test729/repo", "actor")
        assert res["ok"] is False
        assert res["error"] == "duplicate_handoff"
        oe = res["original_event"]
        assert oe["seq"] == 7306
        assert oe["agent"] == "issue"
        assert oe["task_ref"]["task_id"] == "task-729-cross"
        assert oe["result_ref"]["status"] == "available"
        assert "Spec" in oe["result_ref"]["summary"]

    def test_non_duplicate_handoff_unaffected(self):
        """无活跃事件 → 不走 duplicate 拦截分支（不携带 original_event 字段）。"""
        from src.api.routes.webhook import _cross_pipeline_handoff
        _insert_event(7307, status="completed", agent_type="issue", scene_id="default")
        res = _cross_pipeline_handoff(
            REPO, "729", "default", "vod",
            {}, "", None, "ctx", "test729/repo", "actor")
        # 该分支语义保持 Issue #329 原行为（成功移交或其它 skip 原因），
        # 断言仅约束：非 duplicate 拦截不伪造 original_event
        if res.get("ok") is False:
            assert res.get("error") != "duplicate_handoff"
            assert "original_event" not in res
