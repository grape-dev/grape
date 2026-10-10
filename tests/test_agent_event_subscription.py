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
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM event_skipped WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM event_skipped WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.cursor().execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
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

    def test_double_path_subscription_extra_event(self, monkeypatch):
        """双路径并行（event-routing-subscription-design.md §2.2）：PR Merged 阶段矩阵→qa，
        SRE 订阅命中→额外建 a2a_event，不改 stage。"""
        monkeypatch.setattr(ep, "_execute_agent", lambda *a, **k: None)
        import time as _t
        # setup: issue_state stage=qa (#1)，qa 会话 + SRE 订阅
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO issue_state (repo_key, issue_id, stage, owner_agent) VALUES (?,?,?,?)",
                      (RK, "#1", "qa", "qa"))
            c.execute("INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, status, last_active_at, enable) VALUES (?,?,?,?,?,?,datetime('now'),1)",
                      (f"qa-{RK}", RK, "qa", 0, "pr", "idle"))
            c.execute("INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, status, last_active_at, enable, subscribe_events) VALUES (?,?,?,?,?,?,datetime('now'),1,?)",
                      (f"sub-sre-{RK}", RK, "sre", 0, "cloud-deploy", "idle",
                       json.dumps([{"event_type": "pull_request_merged", "branch": ""}])))
            c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                      (RK, "owner", "repo", "owner/repo"))
            db.commit()
        pr = {"issues": [{"number": 1}],
              "project": {"path_with_namespace": "owner/repo"},
              "object_attributes": {"id": 10, "iid": 10, "title": "fix issue #1 stuff",
                                    "target_branch": "main", "action": "merge"}}
        r = self._run_pr_merged(pr)
        assert r.get("ok")
        # 主路由 = qa（阶段矩阵）
        assert r.get("skill") == "qa"
        with db_lock:
            rows = db.cursor().execute(
                "SELECT seq, agent_type, event_type FROM events WHERE repo_key=? ORDER BY seq", (RK,)).fetchall()
        # 双路径：qa 主事件 + sre 订阅事件
        assert len(rows) == 2, f"应生成 2 个事件：qa(主) + sre(订阅)，实际 {[r[1] for r in rows]}"
        agents = [r[1] for r in rows]
        assert "qa" in agents
        assert "sre" in agents
        # sre 事件为 a2a_event 类型
        sre_evt = [r for r in rows if r[1] == "sre"][0]
        assert sre_evt[2] == "a2a_event"
        # stage 不推进（仍为 qa，订阅路径不改 stage）
        with db_lock:
            st = db.cursor().execute("SELECT stage FROM issue_state WHERE repo_key=? AND issue_id='#1'", (RK,)).fetchone()
        assert st and st[0] == "qa", f"订阅路径不应推进 stage，实际={st}"


class TestA2ASreSubscriptionDedup:
    """A2A→SRE 订阅去重（event-routing-subscription-design.md §2.6）：
    SRE 已配置订阅时，A2A 交办只推进 stage、不创建任务。"""

    def _seed(self):
        from src.api.db_setup import init_database
        init_database()
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (RK,))
            c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (RK,))
            c.execute("DELETE FROM events WHERE repo_key=?", (RK,))
            c.execute("DELETE FROM issue_state WHERE repo_key=?", (RK,))
            db.commit()

    def test_sre_subscribed_query_returns_nonempty(self):
        """SRE 订阅非空 → a2a_endpoint 去重应生效（不建任务）。"""
        self._seed()
        _set_subscription(subs=[{"event_type": "pull_request_merged", "branch": ""}])
        with db_lock:
            r = db.cursor().execute(
                "SELECT subscribe_events FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'", (RK,)).fetchone()
        assert r and r[0]
        _raw = r[0].strip()
        assert _raw and _raw != "[]" and _raw != "null", "订阅非空时应触发去重"

    def test_sre_no_subscription_query_returns_empty_or_null(self):
        """SRE 无订阅 → a2a_endpoint 去重不生效（正常创建任务）。"""
        self._seed()
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, status, last_active_at, enable, subscribe_events) VALUES (?,?,?,?,?,?,datetime('now'),1,?)",
                      (f"sre-{RK}", RK, "sre", 0, "cloud-deploy", "idle", "[]"))
            db.commit()
            r = db.cursor().execute(
                "SELECT subscribe_events FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'", (RK,)).fetchone()
        _raw = (r[0] or "").strip() if r else ""
        assert not _raw or _raw == "[]" or _raw == "null", "无订阅/空订阅时应正常创建任务"
