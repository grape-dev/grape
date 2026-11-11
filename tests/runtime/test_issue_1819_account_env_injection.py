"""Issue #1819: $ACCOUNT 注入值必须是个人账号，禁止仓库全名（owner/repo）。

覆盖：
- 账号形态校验：含 '/' 的仓库全名被拒绝；
- resolve_actor_account：非法候选值回退 actor / Issue creator，仍不可得留空并告警；
- run_pipeline 不再以仓库 owner 段兜底（静态契约）；
- _create_failed_task 失败快照与 env_extra 的 ACCOUNT 均不含 '/'；
- build_task_env（重派 / 对话入队）对非法 account 回退 Issue creator；
- autopilot（scheduler）与事件对话路由不再用 repository 充当 ACCOUNT；
- fetch_issue_via_vcs 能正常解析 repo_key（回归：函数内 split_owner_repo 导入缺失
  会抛 NameError 并被调用方吞掉，VCS Issue 核实与 creator 兜底静默降级）。
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.session_manager import db, db_lock
from src.domains.runtime.pipeline.event_pipeline import (
    _normalize_account,
    _create_failed_task,
    build_task_env,
    fetch_issue_via_vcs,
    resolve_actor_account,
)

_REPO_KEY = "gitcode:owner/repo"
_REPO_LIKE = "gitcode:owner/repo%"


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
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE ?", (_REPO_LIKE,))
        c.execute("DELETE FROM events WHERE repo_key LIKE ?", (_REPO_LIKE,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE ?", (_REPO_LIKE,))
        try:
            c.execute("DELETE FROM issue WHERE repo_key LIKE ?", (_REPO_LIKE,))
        except Exception:
            pass
        db.commit()


@pytest.fixture(autouse=True)
def _isolate_repo_rows():
    _clean()
    yield
    _clean()


def _a2a_evt(account="owner/repo", actor="", issue_number="5", repository="owner/repo"):
    payload = {"repository": repository, "issue_number": issue_number}
    if account is not None:
        payload["account"] = account
    return {"type": "a2a_event", "actor": actor, "payload": payload,
            "repository": repository}


class TestAccountShape:
    def test_repo_full_name_is_invalid(self):
        assert _normalize_account("owner/repo") == ""
        assert _normalize_account("gitcode:owner/repo") == ""
        assert _normalize_account("") == ""
        assert _normalize_account(None) == ""
        assert _normalize_account("a b") == ""

    def test_personal_account_is_valid(self):
        assert _normalize_account("XOcto") == "XOcto"
        assert _normalize_account("  alice  ") == "alice"


class TestResolveActorAccountRejectsRepoFullName:
    def test_a2a_payload_repo_full_name_falls_back_to_actor(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="owner/repo", actor="actor_user")
        assert resolve_actor_account(evt, _REPO_KEY) == "actor_user"

    def test_a2a_payload_repo_full_name_falls_back_to_issue_creator(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="owner/repo", actor="")
        assert resolve_actor_account(evt, _REPO_KEY) == "creator_user"

    def test_non_a2a_actor_repo_full_name_falls_back_to_issue_creator(self):
        _seed_issue(created_by="creator_user")
        evt = {"type": "issue_opened", "actor": "owner/repo",
               "payload": {"repository": "owner/repo", "issue_number": "5"}}
        assert resolve_actor_account(evt, _REPO_KEY) == "creator_user"

    def test_invalid_candidates_warn_and_stay_empty(self, caplog):
        _seed_issue(created_by="")
        evt = _a2a_evt(account="owner/repo", actor="owner/repo")
        with caplog.at_level("WARNING"):
            account = resolve_actor_account(evt, _REPO_KEY)
        assert account == ""
        warning = "\n".join(r.getMessage() for r in caplog.records if "ACCOUNT_" in r.getMessage())
        assert "ACCOUNT_INVALID" in warning, "非法账号形态必须输出告警"
        assert "ACCOUNT_MISSING" in warning, "解析为空必须输出结构化告警"


class TestFailedTaskSnapshotAccount:
    def test_env_and_task_account_have_no_slash(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="owner/repo", actor="", issue_number="5")
        evt["account"] = "owner/repo"
        with patch("src.prompt_builder.build_devops_agent_prompt", return_value=""), \
             patch("src.api.event_pipeline._get_task_repo_token", return_value=""):
            _create_failed_task(_REPO_KEY, 0, "qa", "opencode", "qa-skill", "", 0, evt,
                                issue_id="#5")
        with db_lock:
            row = db.cursor().execute(
                "SELECT account, env_extra FROM agent_tasks WHERE repo_key=? "
                "ORDER BY created_at DESC LIMIT 1", (_REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == "creator_user"
        assert "/" not in (row[0] or "")
        assert json.loads(row[1]).get("ACCOUNT") == "creator_user"


class TestBuildTaskEnvAccount:
    def test_invalid_account_falls_back_to_issue_creator(self):
        _seed_issue(created_by="creator_user")
        env = build_task_env(_REPO_KEY, "qa", "#5", account="owner/repo",
                             issue_number="5")
        assert env["ACCOUNT"] == "creator_user"
        assert "/" not in env["ACCOUNT"]

    def test_invalid_account_without_creator_stays_empty(self):
        _seed_issue(created_by="")
        env = build_task_env(_REPO_KEY, "qa", "#5", account="owner/repo",
                             issue_number="5")
        assert env["ACCOUNT"] == ""

    def test_valid_account_is_kept(self):
        _seed_issue(created_by="creator_user")
        env = build_task_env(_REPO_KEY, "qa", "#5", account="alice",
                             issue_number="5")
        assert env["ACCOUNT"] == "alice"

    def test_repo_env_vars_unchanged(self):
        _seed_issue(created_by="creator_user")
        env = build_task_env(_REPO_KEY, "qa", "#5", account="owner/repo",
                             issue_number="5")
        assert env["REPO_FULL_NAME"] == "owner/repo"
        assert env["REPO_OWNER"] == "owner"
        assert env["REPO_NAME"] == "repo"


class TestInjectionSourcesNoLongerUseRepository:
    """注入侧源码契约：禁止把 repository（owner/repo）写入 ACCOUNT。"""

    def test_run_pipeline_uses_resolver_not_owner_fallback(self):
        import inspect
        import src.domains.runtime.pipeline.event_pipeline as ep
        src = inspect.getsource(ep.run_pipeline)
        assert 'split_owner_repo(evt["repository"])' not in src
        assert "resolve_actor_account(evt, repo_key)" in src

    def test_scheduler_autopilot_account_uses_task_creator(self):
        """偏离原 AC（原口径：autopilot 恒空 $ACCOUNT）。

        现口径：按 autopilot_tasks.created_by 注入任务创建者账号，查不到（created_by=0 /
        用户已删）才留空并告警。变更理由：恒空使定时巡检任务在任何消费端都没有身份，
        署名与认领只能由下游硬编码。
        """
        import inspect
        import src.domains.runtime.engine.scheduler as sch
        src = inspect.getsource(sch._execute_autopilot_agent)
        assert '"ACCOUNT": repository' not in src
        assert '"ACCOUNT": _autopilot_account' in src
        assert "task_creator_account" in src
        assert "ACCOUNT_MISSING" in src

    def test_event_conversation_routes_use_resolver(self):
        import inspect
        import src.domains.runtime.routes.events as ev
        src = inspect.getsource(ev)
        assert "account=repository" not in src
        assert "resolve_actor_account(evt, repo_key)" in src


class TestFetchIssueViaVcsImportRegression:
    """回归：fetch_issue_via_vcs 内部必须导入 split_owner_repo（Issue #1819 QA 阻断项）。

    导入缺失时会在函数体内抛 NameError，被上层调用方 try/except 吞掉后 VCS Issue
    核实与 creator 兜底静默降级为 unknown，故此处做行为级断言。
    """

    def test_fetch_issue_via_vcs_resolves_repo_key(self, monkeypatch):
        import urllib.request
        import src.domains.mgmt.user.db as user_db

        monkeypatch.setattr(user_db, "get_repo_token_with_refresh", lambda repo_key: "tok")
        captured = {}

        class _Resp:
            def read(self):
                return json.dumps({"title": "issue title", "number": 5}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _fake_urlopen(req, timeout=15):
            captured["url"] = getattr(req, "full_url", "")
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        state, data = fetch_issue_via_vcs("gitcode:owner/repo", "#5")
        assert state == "exists"
        assert data.get("title") == "issue title"
        assert "/repos/owner/repo/issues/5" in captured["url"]

    def test_fetch_issue_via_vcs_unknown_without_token(self, monkeypatch):
        import src.domains.mgmt.user.db as user_db

        monkeypatch.setattr(user_db, "get_repo_token_with_refresh", lambda repo_key: "")
        assert fetch_issue_via_vcs("gitcode:owner/repo", "5") == ("unknown", {})


class TestSharedAccountChain:
    def test_resolver_and_build_task_env_agree(self):
        _seed_issue(created_by="creator_user")
        evt = _a2a_evt(account="owner/repo", actor="")
        assert resolve_actor_account(evt, _REPO_KEY) == "creator_user"
        env = build_task_env(_REPO_KEY, "qa", "#5", account="owner/repo",
                             issue_number="5")
        assert env["ACCOUNT"] == "creator_user"

    def test_autopilot_missing_is_logged_at_info(self, caplog):
        from src.domains.runtime.pipeline.account_env import log_account_alert

        with caplog.at_level("INFO"):
            log_account_alert("ACCOUNT_MISSING", level="info", event_type="autopilot")
        records = [r for r in caplog.records if "ACCOUNT_MISSING" in r.getMessage()]
        assert records
        assert records[-1].levelname == "INFO"
