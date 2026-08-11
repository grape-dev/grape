"""Issue 生命周期事件路由测试（见 vod-skill-upgrade-plan.md L1~L10 归属矩阵）"""
import json, os
import pytest

from src.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.api import session_store as ss
from src.api.event_pipeline import _resolve_issue_stage_agent


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state")
        db.cursor().execute("DELETE FROM events")
        db.cursor().execute("DELETE FROM event_skipped")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state")
        db.cursor().execute("DELETE FROM events")
        db.cursor().execute("DELETE FROM event_skipped")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()


RK = "gitcode:test/repo"
ISSUE_PAYLOAD = {"issue": {"number": 5, "title": "T"}, "object_attributes": {"id": 5, "title": "T", "iid": 5}}
PR_PAYLOAD = {"pull_request": {"number": 10, "title": "feat: fix #5"}, "object_attributes": {"id": 10, "iid": 10, "title": "feat: fix #5"}}


class TestStageRoutingHelper:
    def test_issue_opened_to_vod(self):
        assert _resolve_issue_stage_agent(RK, "issue_opened", ISSUE_PAYLOAD, "issue") == "vod"

    def test_issue_comment_no_state_default_vod(self):
        # 无 issue_state 记录 → Issue 类事件默认 vod
        assert _resolve_issue_stage_agent(RK, "issue_comment", ISSUE_PAYLOAD, "issue") == "vod"

    def test_issue_comment_stage_issue_to_issue(self):
        ss.set_issue_stage(RK, "#5", "issue", "issue")
        assert _resolve_issue_stage_agent(RK, "issue_comment", ISSUE_PAYLOAD, "issue") == "issue"

    def test_issue_updated_stage_coding_to_issue(self):
        # L4：stage=coding 时 issue_updated → issue agent（coding 不直接处理 Issue 事件）
        ss.set_issue_stage(RK, "#5", "coding", "coding")
        assert _resolve_issue_stage_agent(RK, "issue_updated", ISSUE_PAYLOAD, "coding") == "issue"

    def test_pr_stage_vod_to_issue(self):
        # L6：stage=vod 时 PR 事件 → issue agent 审视（不路由 coding/qa）
        ss.set_issue_stage(RK, "#5", "vod", "vod")
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", PR_PAYLOAD, "coding") == "issue"

    def test_pr_stage_coding_to_coding(self):
        # L7：stage=coding 时 PR → coding
        ss.set_issue_stage(RK, "#5", "coding", "coding")
        assert _resolve_issue_stage_agent(RK, "pull_request_updated", PR_PAYLOAD, "issue") == "coding"

    def test_pr_stage_qa_to_qa(self):
        # L5：stage=qa 时 PR → qa（未合入持续接单）
        ss.set_issue_stage(RK, "#5", "qa", "qa")
        assert _resolve_issue_stage_agent(RK, "note_hook_pr", PR_PAYLOAD, "issue") == "qa"

    def test_pr_stage_issue_to_issue(self):
        ss.set_issue_stage(RK, "#5", "issue", "issue")
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", PR_PAYLOAD, "qa") == "issue"

    def test_done_no_dispatch(self):
        ss.set_issue_stage(RK, "#5", "done", "done")
        assert _resolve_issue_stage_agent(RK, "issue_comment", ISSUE_PAYLOAD, "issue") == ""
        assert _resolve_issue_stage_agent(RK, "pull_request_updated", PR_PAYLOAD, "qa") == ""


class TestRunPipelineStage:
    def test_issue_opened_creates_state_and_routes_vod(self):
        # 预置路由：issue 订阅 issue_opened + issue_updated（阶段矩阵二次分发）
        with db_lock:
            db.cursor().execute("INSERT OR REPLACE INTO event_route_config (repo_key, event_type, skill) VALUES ('','issue_opened','issue')")
            db.cursor().execute("INSERT OR REPLACE INTO agent_type (agent_type, display_name, enable, order_num, subscribe_events) VALUES ('issue','Issue Agent',1,2,'[\"issue_opened\",\"issue_updated\",\"issue_comment\"]')")
            db.commit()
        raw = json.dumps({"object_kind": "issue", "project": {"path_with_namespace": "owner/repo"},
                          "object_attributes": {"id": 5, "title": "T", "iid": 5, "state": "opened"},
                          "user": {"username": "alice"}}).encode()
        r = ep.run_pipeline(raw, {"X-GitCode-Event": "Issue Hook"}, account="owner/repo", repo_key_override="test/repo")
        # issue_opened（归一化为 issue_updated）→ 建 stage=vod（无 session 时默认 vod）
        st = ss.get_issue_stage(RK, "#5")
        assert st and st["stage"] == "vod" and st["owner_agent"] == "vod"


class TestNoIssueNoWork:
    """no issue, no work：PR 事件无关联 Issue / 无 issue_state 时不得触发任何 agent。"""

    def test_pr_without_issue_not_dispatched(self):
        """PR 事件无关联 Issue（payload 无 issue 号）→ 不路由到 QA 等任何 agent。"""
        # payload 不含关联 Issue 号
        pr_no_issue = {"pull_request": {"number": 99, "title": "docs: fix typo"}, "object_attributes": {"id": 99, "iid": 99, "title": "docs: fix typo"}}
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", pr_no_issue, "") == ""

    def test_pr_with_issue_but_no_stage_not_dispatched(self):
        """PR 有关联 Issue 但 issue_state 未建立（非 grape 开发链）→ 不触发任何 agent。"""
        pr_with_issue = {"pull_request": {"number": 10, "title": "feat: fix #5"}, "object_attributes": {"id": 10, "iid": 10, "title": "feat: fix #5"}}
        # 未 set_issue_stage（#5 无记录）→ default_agent="" → 不触发
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", pr_with_issue, "") == ""

    def test_pr_with_stage_qa_dispatched(self):
        """PR 有关联 Issue 且 issue_state=qa（grape 开发链推进）→ 正常路由 QA。"""
        pr_with_issue = {"pull_request": {"number": 10, "title": "feat: fix #5"}, "object_attributes": {"id": 10, "iid": 10, "title": "feat: fix #5"}}
        ss.set_issue_stage(RK, "#5", "qa", "qa")
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", pr_with_issue, "") == "qa"


class TestIssueAssigneeAndStage:
    """Issue 责任人（assignee）落库/读取 + pipeline 阶段列（见 user-workflow-overview）。"""

    def test_set_and_get_assignee(self):
        ss.set_issue_stage(RK, "#5", "coding", "coding", assignee="grape_bot")
        st = ss.get_issue_stage(RK, "#5")
        assert st["assignee"] == "grape_bot"
        assert ss.get_issue_assignee(RK, "#5") == "grape_bot"

    def test_assignee_default_empty(self):
        ss.set_issue_stage(RK, "#5", "vod", "vod")
        st = ss.get_issue_stage(RK, "#5")
        assert st["assignee"] == ""

    def test_get_issue_assignee_no_record(self):
        assert ss.get_issue_assignee(RK, "#999") == ""

    def test_pipeline_stage_field(self):
        """_get_issue_pipeline 返回 stage（来自 issue_state.stage）。"""
        from src.api.routes.pipeline import _get_issue_pipeline
        ss.set_issue_stage(RK, "#5", "qa", "qa", assignee="grape_bot")
        events = [
            {"seq": 1, "issue_id": "#5", "event_type": "issue_opened", "agent_type": "vod", "title": "T", "status": "completed", "actor": "alice"},
            {"seq": 2, "issue_id": "#5", "event_type": "a2a_event", "agent_type": "coding", "title": "T", "status": "completed", "actor": ""},
        ]
        p = _get_issue_pipeline(RK, "#5", events)
        assert p is not None
        assert p["stage"] == "qa"
        assert p["current_owner"] == "grape_bot"  # 优先 issue_state.assignee

    def test_pipeline_stage_default_vod(self):
        """无 issue_state 记录时 stage 默认 vod。"""
        from src.api.routes.pipeline import _get_issue_pipeline
        events = [{"seq": 1, "issue_id": "#5", "event_type": "issue_opened", "agent_type": "vod", "title": "T", "status": "completed", "actor": "alice"}]
        p = _get_issue_pipeline(RK, "#5", events)
        assert p is not None
        assert p["stage"] == "vod"
