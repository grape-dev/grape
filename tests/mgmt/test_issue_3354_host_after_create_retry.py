"""Issue #3354：建仓后自动托管对 VCS 就绪时序错误做有限退避重试。

覆盖：
- mock VCS 前 3 次 404（仓库刚建未就绪）→ 3 次重试（5s/15s/45s）后成功，
  终态 webhook_id>0 且 host_status 清理为完整（空串）；
- 4 次均 404 → 终态失败，host_error 含具体原因 + 已重试 3 次，host_status=webhook_missing；
- 权限类错误（403）不重试，快速失败，host_error 含重试 0 次。

测试 seam：create_new_repo 公开返回值 + managed_repos 落库字段 + 注入的 sleep 记录。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.api.db_setup import init_database  # noqa: E402

REPO_KEY = "gitcode:owner/newrepo3354"


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    init_database()


class TestHostAfterCreateRetry:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
            try:
                c.execute("DELETE FROM user_repos WHERE repo_key=?", (REPO_KEY,))
            except Exception:
                pass
            db.commit()

    def _patches(self, monkeypatch, find_fn, setup_fn=None):
        from src.domains.mgmt.routes import repos as repos_mod

        monkeypatch.setattr(
            "src.domains.mgmt.routes.repos.get_user_token_for",
            lambda username, user_id=0, provider="": "tok-user")

        class _StubProv:
            def create_repo(self, api_token="", name="", description="", private=False, **kw):
                return {"full_name": "owner/newrepo3354", "owner": "owner", "name": name}

        monkeypatch.setattr("src.domains.mgmt.vcs.create_provider",
                            lambda provider="": _StubProv())
        monkeypatch.setattr("src.domains.mgmt.user.db.get_webhook_url",
                            lambda: "http://localhost:8080/webhook/gitcode")
        monkeypatch.setattr("src.domains.mgmt.routes.repos._fetch_and_cache",
                            lambda *a, **kw: [])
        monkeypatch.setattr("src.api.session_store.ensure_default_agent_sessions",
                            lambda *a, **kw: None)

        cfg = {
            "bot_token": "",
            "bot_username": "grape_bot",
            "find_webhook": find_fn,
            "setup_webhook": setup_fn or (lambda *a, **kw: 87693),
            "remove_webhook": lambda *a, **kw: None,
            "webhook_url_suffix": "/webhook/gitcode",
            "db_token_field": "gitcode_webhook_token",
            "db_bot_token_field": "bot_gitcode_token",
            "setup_webhook_kwargs": lambda secret, token, path_b: {},
        }
        monkeypatch.setattr(
            "src.domains.mgmt.user.repo_manager.get_hosting_provider_config",
            lambda provider: cfg)

        slept = []
        monkeypatch.setattr(repos_mod, "_host_after_create_sleep", lambda s: slept.append(s))
        return slept

    def _call(self):
        from src.domains.mgmt.routes.repos import create_new_repo, CreateRepoBody
        user = {"id": 1, "username": "u3354", "auth_type": "jwt"}
        body = CreateRepoBody(name="newrepo3354", provider="gitcode", host_after_create=True)
        return create_new_repo(body, user)

    def _row(self):
        with db_lock:
            return db.cursor().execute(
                "SELECT webhook_id, host_status, host_step, host_error FROM managed_repos "
                "WHERE repo_key=?", (REPO_KEY,)).fetchone()

    def test_retry_then_success(self, monkeypatch):
        self._clean()
        calls = {"n": 0}

        def find_fn(token, owner, repo, url):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("GitCode API POST /repos/owner/newrepo3354/hooks failed (404)")
            return None

        slept = self._patches(monkeypatch, find_fn)
        out = self._call()

        assert out.get("hosted") is True, out
        assert out.get("webhook_id") == 87693, out
        assert calls["n"] == 4, "初次 + 3 次重试 = 4 次尝试"
        assert slept == [5, 15, 45], f"退避间隔应为 5s/15s/45s，实际 {slept}"
        row = self._row()
        assert row is not None, "托管成功应落 managed_repos"
        assert row[0] == 87693 and row[0] > 0
        assert row[1] == "", "重试成功后 host_status 应为完整（空串）"
        assert row[3] == "", "成功后不应残留失败原因"
        self._clean()

    def test_retries_exhausted_terminal_failure(self, monkeypatch):
        self._clean()

        def find_fn(token, owner, repo, url):
            raise RuntimeError("GitCode API POST /repos/owner/newrepo3354/hooks failed (404)")

        slept = self._patches(monkeypatch, find_fn)
        out = self._call()

        assert out.get("hosted") is False, out
        assert out.get("retry_count") == 3, out
        assert slept == [5, 15, 45], f"退避间隔应为 5s/15s/45s，实际 {slept}"
        assert out.get("host_status") == "webhook_missing", out
        err = out.get("host_error") or ""
        assert "仓库不存在或无权访问" in err, err
        assert "已重试 3 次" in err, err
        row = self._row()
        assert row is not None, "失败也应兜底注册 managed_repos（自愈链路）"
        assert row[0] == 0, "终态失败 webhook_id 应为 0"
        assert row[1] == "webhook_missing"
        assert "已重试 3 次" in (row[3] or ""), row[3]
        self._clean()

    def test_permission_error_not_retried(self, monkeypatch):
        self._clean()

        def find_fn(token, owner, repo, url):
            raise RuntimeError("GitCode API POST /repos/owner/newrepo3354/hooks failed (403)")

        slept = self._patches(monkeypatch, find_fn)
        out = self._call()

        assert out.get("hosted") is False, out
        assert out.get("retry_count") == 0, out
        assert slept == [], "权限类错误不应重试"
        err = out.get("host_error") or ""
        assert "权限不足" in err, err
        assert "已重试 0 次" in err, err
        self._clean()