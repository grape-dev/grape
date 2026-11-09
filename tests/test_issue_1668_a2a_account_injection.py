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
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.session_manager import db, db_lock
from src.api.event_pipeline import resolve_actor_account

_REPO_KEY = "gitcode:owner/repo"
_REPO_LIKE = "gitcode:owner/repo%"


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
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE ? OR repo_key LIKE 'owner/repo%'",
                  (_REPO_LIKE,))
        c.execute("DELETE FROM events WHERE repo_key LIKE ? OR repo_key LIKE 'owner/repo%'",
                  (_REPO_LIKE,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE ? OR repo_key LIKE 'owner/repo%'",
                  (_REPO_LIKE,))
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        except Exception:
            pass
        try:
            c.execute("DELETE FROM issue WHERE repo_key LIKE ? OR repo_key LIKE 'owner/repo%'",
                      (_REPO_LIKE,))
        except Exception:
            pass
        db.commit()


@pytest.fixture(autouse=True)
def _isolate_repo_rows():
    _clean()
    yield
    _clean()


class TestResolveActorAccount:
    def test_a2a_payload_account_preferred(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="payload_user", actor="actor_user")
        assert resolve_actor_account(evt, _REPO_KEY) == "payload_user"

    def test_a2a_actor_fallback_when_payload_account_empty(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(actor="actor_user")
        assert resolve_actor_account(evt, _REPO_KEY) == "actor_user"

    def test_a2a_falls_back_to_issue_creator(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="")
        assert resolve_actor_account(evt, _REPO_KEY) == "creator_user"

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
        assert '"fallback": "payload.account->actor->issue_creator"' in warning

    def test_a2a_empty_result_is_not_cached_when_issue_created_later(self):
        _seed_issue(created_by="")
        evt = _a2a_evt(account="")
        assert resolve_actor_account(evt, _REPO_KEY) == ""
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET created_by=? WHERE repo_key=? AND issue_number=?",
                      ("later_user", _REPO_KEY, 5))
            db.commit()
        assert resolve_actor_account(evt, _REPO_KEY) == "later_user"

    def test_non_a2a_missing_warns_with_actor_fallback_label(self, caplog):
        evt = {"type": "issue_opened", "actor": "",
               "payload": {"repository": "owner/repo", "issue_number": "5"}}
        with caplog.at_level("WARNING"):
            account = resolve_actor_account(evt, _REPO_KEY)
        assert account == ""
        warning = "\n".join(r.getMessage() for r in caplog.records if "ACCOUNT_MISSING" in r.getMessage())
        assert '"fallback": "actor->payload.account"' in warning

    def test_non_a2a_ignores_legacy_evt_account(self):
        evt = {"type": "issue_opened", "actor": "", "account": "legacy_account",
               "payload": {"repository": "owner/repo", "issue_number": "5"}}
        assert resolve_actor_account(evt, _REPO_KEY) == ""

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

    def test_authenticated_caller_login_used_as_actor(self):
        from src.api.deps import get_user_or_none
        from src.api.main import app
        self._seed_repo()
        _seed_issue(created_by="creator_user")
        app.dependency_overrides[get_user_or_none] = lambda: {
            "id": 1, "username": "caller_login", "auth_type": "jwt"}
        try:
            with patch("src.user.db.get_managed_repos",
                       return_value=[{"repo_key": _REPO_KEY}]):
                r = self._post(self._body(account=""))
        finally:
            app.dependency_overrides.pop(get_user_or_none, None)
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT actor, payload FROM events WHERE repo_key=? AND event_type='a2a_event' "
                "ORDER BY seq DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == "caller_login"
        assert json.loads(row[1]).get("account") == "caller_login"


class TestDerivedA2AActorBackfill:
    def _seed_dispatch_repo(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO managed_repos "
                "(repo_key, repo_owner, repo_name, full_name, webhook_id, dev_scene, "
                "bot_gitcode_token, gitcode_webhook_token, bot_username) "
                "VALUES (?, 'owner', 'repo', 'owner/repo', 0, 'default', ?, 'wt', 'grape_bot')",
                (_REPO_KEY, "bot-token"))
            c.execute(
                "INSERT OR REPLACE INTO devops_agent_session "
                "(id, repo_key, agent_type, runtime_id, status, enable, skill, repo_gitcode_token) "
                "VALUES (?, ?, 'sre', 0, 'idle', 1, 'sre-skill', ?)",
                ("sess-1668-sre", _REPO_KEY, "bot-token"))
            db.commit()

    def test_subscription_actor_and_payload_backfill(self):
        import src.api.event_pipeline as ep
        self._seed_dispatch_repo()
        _seed_issue(created_by="creator_user")
        evt = {"repo_key": _REPO_KEY, "issue_id": "#5",
               "payload": {"issue_number": "5"},
               "actor": "", "repository": "owner/repo", "source": "gitcode",
               "type": "issues_opened"}
        with patch("src.runtime.engine_select.pick_engine", return_value=None):
            ep._dispatch_subscription_agent(_REPO_KEY, evt, "issues_opened", "sre", "qa")
        with db_lock:
            row = db.cursor().execute(
                "SELECT actor, payload FROM events WHERE repo_key=? AND agent_type='sre' "
                "ORDER BY seq DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == "creator_user"
        assert json.loads(row[1]).get("account") == "creator_user"

    def test_pr_subscription_actor_and_payload_backfill(self):
        import src.api.event_pipeline as ep
        _seed_issue(created_by="creator_user")
        evt = {"repo_key": _REPO_KEY, "actor": "pr_actor",
               "payload": {"repository": "owner/repo", "issue_number": "5",
                           "pull_request": {"number": 7}},
               "repository": "owner/repo", "type": "pull_request_opened"}
        with patch("src.runtime.engine_select.pick_engine", return_value=None), \
             patch.object(ep, "_fail_event"):
            ep._dispatch_pr_subscription_extra(
                _REPO_KEY, evt, "pull_request_opened",
                ("scene-1668", "qa-skill", [], "sre-skill", []))
        with db_lock:
            row = db.cursor().execute(
                "SELECT actor, payload FROM events WHERE repo_key=? AND agent_type='qa' "
                "ORDER BY seq DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == "pr_actor"
        assert json.loads(row[1]).get("account") == "pr_actor"

    def test_vod_redispatch_actor_and_payload_backfill(self):
        import src.api.event_pipeline as ep
        _seed_issue(created_by="creator_user")
        captured = {}
        evt = {"repo_key": _REPO_KEY, "actor": "",
               "payload": {"repository": "owner/repo", "issue_number": "5"},
               "repository": "owner/repo", "type": "issue_opened"}

        def _capture_append(*args, **kwargs):
            captured.update(kwargs)
            return 1

        with patch("src.api.event_store.append_event", side_effect=_capture_append), \
             patch("src.api.session_store.get_issue_scene", return_value=("default", 1)), \
             patch.object(ep, "_skill_scope_mismatch", return_value=(False, {})), \
             patch("src.runtime.engine_select.pick_engine", return_value=None), \
             patch.object(ep, "_fail_event"):
            ep._redispatch_vod_after_recommend(_REPO_KEY, "#5", "vod", evt, 0)
        assert captured.get("actor") == "creator_user"
        assert captured.get("payload", {}).get("account") == "creator_user"

    def test_failed_task_env_account_uses_resolved_actor(self):
        import src.api.event_pipeline as ep
        _seed_issue(created_by="creator_user")
        evt = {"type": "a2a_event", "actor": "",
               "payload": {"repository": "owner/repo", "issue_number": "5", "account": ""},
               "repository": "owner/repo"}
        with patch("src.prompt_builder.build_devops_agent_prompt", return_value=""), \
             patch.object(ep, "_get_task_repo_token", return_value=""):
            ep._create_failed_task(_REPO_KEY, 0, "qa", "opencode", "qa-skill", "", 0, evt,
                                   issue_id="#5")
        with db_lock:
            row = db.cursor().execute(
                "SELECT env_extra FROM agent_tasks WHERE repo_key=? "
                "ORDER BY created_at DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert json.loads(row[0]).get("ACCOUNT") == "creator_user"
