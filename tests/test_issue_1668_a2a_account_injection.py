"""Issue #1668: a2a_event 触发时 ACCOUNT/actor 注入与缺失告警。

覆盖：
- a2a_event 解析链：actor → payload.account → Issue creator；
- 解析为空时注入空串并输出结构化 ACCOUNT_MISSING 告警（event_type / issue_number）；
- 禁止回退仓库属主（REPO_OWNER / repository）；
- issue_opened / PR 事件 ACCOUNT 仍取 webhook actor（回归）；
- /api/a2a 事件记录 actor 与 payload.account 同步补齐。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.session_manager import db, db_lock
from src.api.event_pipeline import resolve_actor_account

_REPO_KEY = "gitcode:owner/repo"


def _a2a_evt(account=None, actor="", issue_number="5", company="owner/repo"):
    payload = {"repository": company, "issue_number": issue_number}
    if account is not None:
        payload["account"] = account
    return {"type": "a2a_event", "actor": actor, "payload": payload, "repository": company}


def _seed_issue(issue_number=5, created_by="creator_user"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?",
                  (_REPO_KEY, issue_number))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, created_by) "
            "VALUES (0, '', ?, ?, 'test issue', ?)",
            (_REPO_KEY, issue_number, created_by))
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        except Exception:
            pass
        db.commit()


class TestResolveActorAccount:
    def test_a2a_payload_account_preferred(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="payload_user", actor="actor_user")
        assert resolve_actor_account(evt, _REPO_KEY) == "payload_user"

    def test_a2a_actor_fallback_when_payload_account_empty(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(actor="actor_user")
        assert resolve_actor_account(evt, _REPO_KEY) == "actor_user"

    def test_a2a_falls_back_to_issue_creator_not_owner(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="")
        assert resolve_actor_account(evt, _REPO_KEY) == "creator_user"
        assert resolve_actor_account(evt, _REPO_KEY) != "owner"

    def test_a2a_missing_everything_warns_and_stays_empty(self, caplog):
        _seed_issue(created_by="")
        evt = _a2a_evt(account="")
        with caplog.at_level("WARNING"):
            account = resolve_actor_account(evt, _REPO_KEY)
        assert account == ""
        warning = "\n".join(r.getMessage() for r in caplog.records if "ACCOUNT_MISSING" in r.getMessage())
        assert warning, "缺失 ACCOUNT 必须输出结构化告警"
        assert "a2a_event" in warning
        assert '"issue_number": "5"' in warning
        assert "owner" not in account

    def test_issue_opened_keeps_webhook_actor(self):
        _seed_issue(created_by="creator_user")
        evt = {"type": "issue_opened", "actor": "webhook_user",
               "payload": {"repository": "owner/repo", "issue_number": "5"}}
        assert resolve_actor_account(evt, _REPO_KEY) == "webhook_user"

    def test_pr_event_keeps_webhook_actor(self):
        evt = {"type": "pull_request_opened", "actor": "pr_user",
               "payload": {"repository": "owner/repo", "pr_number": "7"}}
        assert resolve_actor_account(evt, _REPO_KEY) == "pr_user"


class TestA2AEndpointActorSync:
    def _seed_repo(self):
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo')")
                db.commit()
            except Exception:
                pass

    def _post(self, body):
        from fastapi.testclient import TestClient
        from src.api.main import app
        return TestClient(app).post("/api/a2a", json=body)

    def _body(self, account=None):
        payload = {"repository": "owner/repo", "issue_number": "5",
                   "pr_number": "", "context": "Issue #1668 actor sync"}
        if account is not None:
            payload["account"] = account
        return {"agent": "coding", "payload": payload}

    def test_event_actor_uses_issue_creator_when_payload_account_empty(self):
        _clean()
        self._seed_repo()
        _seed_issue(created_by="creator_user")
        r = self._post(self._body(account=""))
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT actor, payload FROM events WHERE repo_key=? AND event_type='a2a_event' "
                "ORDER BY seq DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == "creator_user"
        assert json.loads(row[1]).get("account") == "creator_user"

    def test_event_actor_never_falls_back_to_repo_owner(self):
        _clean()
        self._seed_repo()
        _seed_issue(created_by="")
        r = self._post(self._body(account=""))
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT actor, payload FROM events WHERE repo_key=? AND event_type='a2a_event' "
                "ORDER BY seq DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == ""
        assert json.loads(row[1]).get("account") == ""
        assert row[0] != "owner"
