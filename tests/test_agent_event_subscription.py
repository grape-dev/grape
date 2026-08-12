"""仓库级事件订阅（agent-event-subscription-design.md）测试

覆盖：_match_repo_subscription 匹配逻辑 + run_pipeline 兜底路由 +
agents API 订阅字段读写往返。
"""
import json
import pytest

from src.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.api.event_pipeline import (
    _match_repo_subscription,
    resolve_pr_target_branch,
    run_pipeline,
)


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state")
        db.cursor().execute("DELETE FROM events")
        db.cursor().execute("DELETE FROM event_skipped")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM devops_agent_session")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state")
        db.cursor().execute("DELETE FROM events")
        db.cursor().execute("DELETE FROM event_skipped")
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM devops_agent_session")
        db.commit()


RK = "gitcode:test/repo"


def _set_subscription(agent_type="sre", subs=None):
    """插入仓库级会话 + 订阅配置（enable=1，跳过 enable 拦截）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, status, last_active_at, enable, subscribe_events) VALUES (?,?,?,?,?,?,datetime('now'),1,?)",
            (f"sub-{agent_type}-{RK}", RK, agent_type, 0, "cloud-deploy", "idle",
             json.dumps(subs or [], ensure_ascii=False))
        )
        db.commit()


class TestMatchRepoSubscription:
    def test_no_subscription_returns_empty(self):
        assert _match_repo_subscription(RK, "pull_request_merged", "main") == ""

    def test_event_mismatch_returns_empty(self):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": "main"}])
        assert _match_repo_subscription(RK, "pull_request_opened", "main") == ""

    def test_branch_mismatch_returns_empty(self):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": "main"}])
        assert _match_repo_subscription(RK, "pull_request_merged", "dev") == ""

    def test_branch_match_returns_sre(self):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": "main"}])
        assert _match_repo_subscription(RK, "pull_request_merged", "main") == "sre"

    def test_empty_branch_means_any(self):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": ""}])
        assert _match_repo_subscription(RK, "pull_request_merged", "dev") == "sre"

    def test_comma_branches_match_any(self):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": "main, dev"}])
        assert _match_repo_subscription(RK, "pull_request_merged", "dev") == "sre"
        assert _match_repo_subscription(RK, "pull_request_merged", "main") == "sre"
        assert _match_repo_subscription(RK, "pull_request_merged", "release") == ""


class TestResolvePrTargetBranch:
    def test_gitcode_target_branch(self):
        evt = {"branch": "", "payload": {"object_attributes": {"target_branch": "main"}}}
        assert resolve_pr_target_branch(evt) == "main"

    def test_github_base_ref(self):
        evt = {"branch": "", "payload": {"pull_request": {"base": {"ref": "master"}}}}
        assert resolve_pr_target_branch(evt) == "master"

    def test_push_branch_precedence(self):
        evt = {"branch": "feat/x", "payload": {"object_attributes": {"target_branch": "main"}}}
        assert resolve_pr_target_branch(evt) == "feat/x"

    def test_no_branch_empty(self):
        assert resolve_pr_target_branch({"branch": "", "payload": {}}) == ""


class TestRunPipelineSubscription:
    """run_pipeline：无关联 Issue 的 PR 合入，命中仓库订阅 → 路由 sre 并建 event。"""

    def _run_pr_merged(self, payload):
        raw = json.dumps(payload).encode()
        return run_pipeline(raw, {"X-GitCode-Event": "Merge Request Hook"},
                            account="owner/repo", repo_key_override="test/repo")

    def test_no_subscription_pr_merged_skipped(self):
        pr = {"project": {"path_with_namespace": "owner/repo"},
              "object_attributes": {"id": 10, "iid": 10, "title": "merge to main",
                                    "target_branch": "main", "action": "merge"}}
        r = self._run_pr_merged(pr)
        assert not r.get("ok")
        with db_lock:
            row = db.cursor().execute("SELECT COUNT(*) FROM events").fetchone()
            assert row[0] == 0

    def test_subscription_match_routes_sre(self, monkeypatch):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": "main"}])
        monkeypatch.setattr(ep, "_execute_agent", lambda *a, **k: None)
        pr = {"project": {"path_with_namespace": "owner/repo"},
              "object_attributes": {"id": 10, "iid": 10, "title": "merge to main",
                                    "target_branch": "main", "action": "merge"}}
        r = self._run_pr_merged(pr)
        assert r.get("ok")
        assert r.get("event_type") == "pull_request_merged"
        assert r.get("skill") == "sre"
        with db_lock:
            row = db.cursor().execute(
                "SELECT agent_type, issue_id FROM events WHERE repo_key=? ORDER BY seq DESC LIMIT 1", (RK,)
            ).fetchone()
            assert row[0] == "sre"
            assert row[1] == "#0"

    def test_subscription_branch_mismatch_skipped(self):
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": "main"}])
        pr = {"project": {"path_with_namespace": "owner/repo"},
              "object_attributes": {"id": 10, "iid": 10, "title": "merge to dev",
                                    "target_branch": "dev", "action": "merge"}}
        r = self._run_pr_merged(pr)
        assert not r.get("ok")
        with db_lock:
            row = db.cursor().execute("SELECT COUNT(*) FROM events").fetchone()
            assert row[0] == 0

    def test_subscription_disabled_agent_skipped(self):
        # enable=0 的订阅 agent：命中订阅但被 enable 拦截（agent_disabled）
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, status, last_active_at, enable, subscribe_events) VALUES (?,?,?,?,?,?,datetime('now'),0,?)",
                (f"sub-sre-{RK}", RK, "sre", 0, "cloud-deploy", "idle",
                 json.dumps([{"event_type": "pull_request_merged", "branch": "main"}]))
            )
            db.commit()
        pr = {"project": {"path_with_namespace": "owner/repo"},
              "object_attributes": {"id": 10, "iid": 10, "title": "merge to main",
                                    "target_branch": "main", "action": "merge"}}
        r = self._run_pr_merged(pr)
        assert not r.get("ok")
        with db_lock:
            skip = db.cursor().execute(
                "SELECT COUNT(*) FROM event_skipped WHERE repo_key=? AND reason='agent_disabled'", (RK,)
            ).fetchone()
            assert skip[0] == 1
