"""Issue #49 Webhook 事件 action 过滤 + 分阶段路由单测。

见 issue-49-webhook-action-filter-fix-design.md §四（L1 规则）/§五（L2 阶段模型）。
"""
import json
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


@pytest.fixture(autouse=True)
def _clean_event_rules():
    """清空 event_action_filter 并注入测试规则（避免依赖真实 seed）。"""
    from src.api import db_setup
    db_setup.init_database()
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM event_action_filter")
        cur = c
        def _ins(evt, act, allow, actor="any", req="", se=""):
            cur.execute(
                "INSERT INTO event_action_filter (event_type, action, allow, actor, require_changes, handle_side_effect, priority, enabled) "
                "VALUES (?,?,?,?,?,?,0,1)",
                (evt, act, allow, actor, req, se))
        _ins("issue_updated", "open", 1)
        _ins("issue_updated", "close", 2, se="close_issue_tasks")
        _ins("issue_updated", "edit", 1, actor="user", req='["description"]')
        _ins("issue_updated", "assign", 0)
        _ins("issue_updated", "label", 0)
        _ins("issue_updated", "reopen", 0)
        _ins("pull_request_opened", "open", 1)
        _ins("pull_request_opened", "update", 1, actor="user")
        _ins("pull_request_opened", "close", 0)
        _ins("pull_request_opened", "merge", 1)
        _ins("pull_request_merged", "merge", 1)
        db.commit()
    from src.api import event_rule
    event_rule.load_event_action_rule(force=True)
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM event_action_filter")
        db.commit()
    event_rule.load_event_action_rule(force=True)


def _monkey_actor(monkeypatch, kind):
    """stub _get_actor → 返回指定类型。"""
    from src.api import event_rule as er
    monkeypatch.setattr(er, "_get_actor", lambda payload, **kw: kind)


class TestEvaluateEventAction:
    def test_open_allow(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {"object_attributes": {"action": "open"}})
        assert d["decision"] == "allow"

    def test_assign_filter(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {"object_attributes": {"action": "assign"}})
        assert d["decision"] == "skip"
        assert "assign" in d["reason"]

    def test_close_side_effect(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {"object_attributes": {"action": "close"}})
        assert d["decision"] == "side_effect"
        assert d["side_effect"] == "close_issue_tasks"

    def test_edit_requires_description_change(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        # 无 changes → 过滤
        d1 = evaluate_event_action("issue_updated", {"object_attributes": {"action": "edit"}})
        assert d1["decision"] == "skip"
        # 有 description 变更 → 放行
        d2 = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "edit"},
            "changes": {"description": {"from": "a", "to": "b"}},
        })
        assert d2["decision"] == "allow"
        # 仅 title 变更 → 过滤
        d3 = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "edit"},
            "changes": {"title": {"from": "a", "to": "b"}},
        })
        assert d3["decision"] == "skip"

    def test_pr_update_actor_split(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        # user → allow
        _monkey_actor(monkeypatch, "user")
        d1 = evaluate_event_action("pull_request_opened", {"object_attributes": {"action": "update"}})
        assert d1["decision"] == "allow"
        # bot → skip（actor mismatch）
        _monkey_actor(monkeypatch, "bot")
        d2 = evaluate_event_action("pull_request_opened", {"object_attributes": {"action": "update"}})
        assert d2["decision"] == "skip"
        assert "actor" in d2["reason"]

    def test_no_rule_allows(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("note_hook_pr", {})
        assert d["decision"] == "no_rule"


class TestResolveIssueStageAgent:
    """分阶段路由映射表（L2）回归：覆盖各 stage×event 归属。"""

    def _resolve(self, event_type, stage, issue_id="#5"):
        from src.api import event_pipeline as ep
        from src.api.session_store import set_issue_stage
        set_issue_stage("gitcode:org/test", issue_id, stage, stage)
        try:
            payload = {"number": 5, "object_attributes": {"iid": 5, "number": 5}}
            val = ep._resolve_issue_stage_agent("gitcode:org/test", event_type, payload, "issue")
            return val
        finally:
            # 清理避免污染下一用例
            from src.session_manager import db, db_lock
            with db_lock:
                db.cursor().execute("DELETE FROM issue_state WHERE repo_key='gitcode:org/test' AND issue_id=?", (issue_id,))
                db.commit()

    def test_issue_comment_route(self):
        # issue 类事件：vod→vod，issue→issue，coding/qa/sre→不分发
        assert self._resolve("issue_comment", "vod") == "vod"
        assert self._resolve("issue_comment", "issue") == "issue"
        assert self._resolve("issue_comment", "coding") == ""
        assert self._resolve("issue_comment", "sre") == ""
        assert self._resolve("issue_comment", "done") == ""

    def test_pr_opened_route(self):
        assert self._resolve("pull_request_opened", "vod") == "issue"
        assert self._resolve("pull_request_opened", "issue") == "issue"
        assert self._resolve("pull_request_opened", "coding") == "coding"
        assert self._resolve("pull_request_opened", "qa") == "qa"
        assert self._resolve("pull_request_opened", "sre") == "sre"
        assert self._resolve("pull_request_opened", "done") == ""

    def test_pr_merged_route(self):
        assert self._resolve("pull_request_merged", "coding") == "coding"
        assert self._resolve("pull_request_merged", "qa") == "qa"
        assert self._resolve("pull_request_merged", "sre") == "sre"

    def test_issue_opened_always_vod(self):
        assert self._resolve("issue_opened", "coding") == "vod"

    def test_issue_updated_close_not_routed(self):
        # close 已由 L1 副作用短路；路由层安全兜底不分发
        assert self._resolve("issue_updated", "vod") == ""
        assert self._resolve("issue_updated", "issue") == ""