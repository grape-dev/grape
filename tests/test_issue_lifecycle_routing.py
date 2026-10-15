"""Issue 生命周期事件路由测试（见 vod-skill-upgrade-plan.md L1~L10 归属矩阵）"""
import json, os
import pytest
from unittest.mock import patch as _p

from src.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.api import session_store as ss
from src.api.event_pipeline import _resolve_issue_stage_agent


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM issue WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM event_skipped")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM issue WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
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
        # L7：stage=coding 时 PR opened → coding（首次提交路由到 coding）
        ss.set_issue_stage(RK, "#5", "coding", "coding")
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", PR_PAYLOAD, "issue") == "coding"

    def test_pr_updated_stage_coding_to_qa(self):
        # Issue #46 修复：stage=coding 时 PR updated → qa（QA↔coding 回流闭环）
        # weekly-02 表驱动：pull_request_updated + coding 恒路由到 qa 复审
        ss.set_issue_stage(RK, "#5", "coding", "coding")
        assert _resolve_issue_stage_agent(RK, "pull_request_updated", PR_PAYLOAD, "issue") == "qa"

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

    # Issue #51: pending_review 阶段测试
    def test_pending_review_no_dispatch(self):
        # TC-29: pending_review 阶段 issue_comment 路由到 issue agent，其余事件不分发
        ss.set_issue_stage(RK, "#5", "pending_review", "sre")
        assert _resolve_issue_stage_agent(RK, "issue_comment", ISSUE_PAYLOAD, "issue") == "issue"
        assert _resolve_issue_stage_agent(RK, "pull_request_updated", PR_PAYLOAD, "qa") == ""
        assert _resolve_issue_stage_agent(RK, "issue_updated", ISSUE_PAYLOAD, "issue") == ""

    def test_pr_stage_pending_review_empty(self):
        # TC-31: PR updated + pending_review → 空
        ss.set_issue_stage(RK, "#5", "pending_review", "sre")
        assert _resolve_issue_stage_agent(RK, "pull_request_updated", PR_PAYLOAD, "issue") == ""
        assert _resolve_issue_stage_agent(RK, "pull_request_opened", PR_PAYLOAD, "issue") == ""
        assert _resolve_issue_stage_agent(RK, "pull_request_merged", PR_PAYLOAD, "issue") == ""


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


class TestOnboardingWebhookPipeline:
    """§11 v1.5：onboarding 模拟 GitCode issue_opened webhook → run_pipeline（广播模式不需绑引擎）。"""

    def test_onboarding_ctx_applied_and_broadcast_dispatches(self):
        """模拟 webhook：issue_opened 路由 vod；_onboarding 上下文注入技能/环境变量；plugin 广播入队（engine_id=0）。"""
        import json as _j
        # 构造与 onboarding._submit_onboarding_webhook 一致的 webhook 体
        raw = _j.dumps({
            "object_kind": "issue",
            "project": {"path_with_namespace": "owner/repo", "name": "repo"},
            "repository": {"full_name": "owner/repo", "name": "repo"},
            "object_attributes": {"action": "open", "iid": 1, "number": 1,
                                  "title": "引导·快速部署体验", "state": "opened"},
            "user": {"username": "alice"},
            "user_name": "alice",
            "_onboarding": {"target": "experience", "skill": "repo-onboarding", "skill_id": 0,
                            "scene": "default", "result_path": "/tmp/onboarding-result-experience.json"},
        }).encode()

        # 广播（plugin）模式：is_plugin_mode→True、pick_engine→0、enqueue_task 记 pool_tag
        captured = {"enqueued": []}
        with _p("src.api.event_pipeline.is_plugin_mode", return_value=True), \
             _p("src.runtime.engine_select.pick_engine", return_value=0), \
             _p("src.runtime.service.get_execution_mode", return_value="queue_remote"), \
             _p("src.runtime.service.enqueue_task",
                side_effect=lambda **kw: captured["enqueued"].append(kw) or "t_onb_demo"):
            r = ep.run_pipeline(raw, {"X-GitCode-Event": "Issue Hook"},
                                account="owner/repo", repo_key_override="test/repo")
        assert r.get("ok") is True and r.get("event_type") == "issue_opened"
        assert captured["enqueued"], "广播模式应入队"
        task = captured["enqueued"][0]
        assert task.get("agent_type") == "vod"
        assert task.get("pool_tag") == "vod" or task.get("pool_agent_type") == "vod", "pool 认领依赖 tag"
        assert "ONBOARDING_RESULT" in task.get("env_extra", {}), "onboarding 上下文应注入 ONBOARDING_* 环境变量"
        assert task.get("skill") == "repo-onboarding", "技能应被引导技能覆盖"

    def test_onboarding_ctx_legacy_no_engine_fails_event(self):
        """legacy 未绑定引擎：与普通事件一致 engine_unavailable（不隐式执行）。"""
        import json as _j
        raw = _j.dumps({
            "object_kind": "issue",
            "project": {"path_with_namespace": "owner/repo"},
            "object_attributes": {"action": "open", "iid": 2, "number": 2,
                                  "title": "引导·探索方向", "state": "opened"},
            "user": {"username": "alice"},
            "user_name": "alice",
            "_onboarding": {"target": "explore", "skill": "repo-onboarding", "skill_id": 0,
                            "scene": "default", "result_path": "/tmp/onboarding-result-explore.json"},
        }).encode()
        with _p("src.api.event_pipeline.is_plugin_mode", return_value=False), \
             _p("src.runtime.engine_select.pick_engine", return_value=None):
            r = ep.run_pipeline(raw, {"X-GitCode-Event": "Issue Hook"},
                                account="owner/repo", repo_key_override="test/repo")
        with db_lock:
            row = db.cursor().execute(
                "SELECT status FROM events WHERE repo_key=? ORDER BY seq DESC LIMIT 1", (RK,)).fetchone()
        assert row and row[0] == "failed"
