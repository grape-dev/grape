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
        def _ins(evt, act, allow, actor="any", req="", block="", se=""):
            cur.execute(
                "INSERT INTO event_action_filter (event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, priority, enabled) "
                "VALUES (?,?,?,?,?,?,?,0,1)",
                (evt, act, allow, actor, req, block, se))
        # 真实 payload：issue_updated.action 只有 update/close/reopen
        # update：title/description 任一变更 → 放行；纯 assignees/labels 变更 → 过滤
        _ins("issue_updated", "update", 1, actor="user", req='["description","title"]', block='["assignees","labels"]')
        _ins("issue_updated", "close", 2, se="close_issue_tasks")
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
        assert d["decision"] == "no_rule"  # open action 在真实 payload 中不出现（not issue_updated）

    def test_update_assign_blocked(self, monkeypatch):
        """真实 payload：assign 变更 = update + changes.assignees → 过滤。"""
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "update"},
            "changes": {"assignees": [{"username": "b"}], "updated_at": "t"},
        })
        assert d["decision"] == "skip"
        assert "assignees" in d["reason"]

    def test_update_label_blocked(self, monkeypatch):
        """真实 payload：label 变更 = update + changes.labels → 过滤。"""
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "update"},
            "changes": {"labels": ["bug"], "updated_at": "t"},
        })
        assert d["decision"] == "skip"
        assert "labels" in d["reason"]

    def test_update_description_allowed(self, monkeypatch):
        """update + changes.description → 放行（内容编辑/需求补充）。"""
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "update"},
            "changes": {"description": {"from": "a", "to": "b"}, "updated_at": "t"},
        })
        assert d["decision"] == "allow"

    def test_update_title_allowed(self, monkeypatch):
        """update + changes.title → 放行（标题编辑保留）。"""
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "update"},
            "changes": {"title": {"from": "a", "to": "b"}, "updated_at": "t"},
        })
        assert d["decision"] == "allow"

    def test_update_desc_plus_assign_allowed(self, monkeypatch):
        """update 同时改 description 和 assignees → 内容变更优先，放行。"""
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {
            "object_attributes": {"action": "update"},
            "changes": {"description": {"from": "a", "to": "b"}, "assignees": [{"username": "b"}]},
        })
        assert d["decision"] == "allow"

    def test_close_side_effect(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {"object_attributes": {"action": "close"}})
        assert d["decision"] == "side_effect"
        assert d["side_effect"] == "close_issue_tasks"

    def test_edit_no_action_rule(self, monkeypatch):
        """真实 payload 无 action='edit'；fallback 为 no_rule（放行）而非误杀。"""
        from src.api.event_rule import evaluate_event_action
        _monkey_actor(monkeypatch, "user")
        d = evaluate_event_action("issue_updated", {"object_attributes": {"action": "edit"}, "changes": {}})
        assert d["decision"] == "no_rule"

    def test_pr_update_actor_split(self, monkeypatch):
        from src.api.event_rule import evaluate_event_action
        # Issue #49 补充：PR update 需带新 commit 才放行
        payload = {"object_attributes": {"action": "update"}, "commits": [{"id": "x"}]}
        _monkey_actor(monkeypatch, "user")
        d1 = evaluate_event_action("pull_request_opened", payload)
        assert d1["decision"] == "allow"
        _monkey_actor(monkeypatch, "bot")
        d2 = evaluate_event_action("pull_request_opened", payload)
        assert d2["decision"] == "skip"
        assert "actor" in d2["reason"]
        # 无 commit（审查/元数据）→ 无论 user/bot 均 skip
        _monkey_actor(monkeypatch, "user")
        d3 = evaluate_event_action("pull_request_opened", {"object_attributes": {"action": "update"}})
        assert d3["decision"] == "skip" and "commit" in d3["reason"]

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
        # issue 类事件：vod→vod，issue 及之后（coding/qa/sre）→issue，done→不分发
        assert self._resolve("issue_comment", "vod") == "vod"
        assert self._resolve("issue_comment", "issue") == "issue"
        assert self._resolve("issue_comment", "coding") == "issue"
        assert self._resolve("issue_comment", "sre") == "issue"
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

    def test_issue_updated_route(self):
        # issue_updated 非 close 动作（认识 open/edit 已由 L1 放行的部分）在非 done 阶段归 issue
        assert self._resolve("issue_updated", "vod") == "issue"
        assert self._resolve("issue_updated", "issue") == "issue"
        assert self._resolve("issue_updated", "sre") == "issue"
        assert self._resolve("issue_updated", "done") == ""