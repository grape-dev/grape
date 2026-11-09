"""Issue #671：A2A 仓库归属校验——注册表优先 + GitCode 可达性回退 + 幂等补录。

覆盖（见 docs/issue-671/plan.md）：
- 已注册仓：不走回退，行为不变（回归）；
- 未注册 + 回退 ok：放行且 managed_repos 幂等补录（重复调用单行）；
- 未注册 + not_found / no_permission：400 细分错误信息；
- 未注册 + 未认证：维持 400 not hosted（不触发回退外呼）；
- _a2a_repo_fallback_check verdict 判定（404/403/网络错误/无 token）；
- create_new_repo 托管失败兜底注册（注册链路缺口修复）。

测试 seam：/api/a2a HTTP 响应 + managed_repos 行数（公开行为）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402
from src.api.db_setup import init_database  # noqa: E402

REPO_KEY = "gitcode:owner/repo671"


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    init_database()


class TestA2ARepoFallback:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo671%'")
            try:
                c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo671%'")
            except Exception:
                pass
            c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo671%'")
            c.execute("DELETE FROM session_api_tokens WHERE repo_key LIKE 'gitcode:owner/repo671%'")
            c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo671'")
            try:
                c.execute("DELETE FROM user_repos WHERE repo_key='gitcode:owner/repo671'")
            except Exception:
                pass
            try:
                c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:owner/repo671%'")
            except Exception:
                pass
            db.commit()

    def _seed_registered(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES ('gitcode:owner/repo671', 'owner', 'repo671', 'owner/repo671')")
            db.commit()

    def _body(self):
        return {
            "agent": "coding",
            "payload": {
                "repository": "owner/repo671",
                "account": "owner/repo671",
                "issue_number": "5",
                "pr_number": "",
                "context": "测试 A2A 归属回退",
            },
        }

    def _managed_rows(self):
        with db_lock:
            return db.cursor().execute(
                "SELECT COUNT(*) FROM managed_repos WHERE repo_key=?",
                (REPO_KEY,)).fetchone()[0]

    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        return TestClient(app)

    def _session_token(self):
        from src.api.auth_token import issue_session_token
        return issue_session_token(session_key="sess-671", repo_key=REPO_KEY, agent_type="coding")

    # ── 回退 verdict 判定（_a2a_repo_fallback_check） ──────────────────────

    def test_fallback_check_verdicts(self, monkeypatch):
        from src.api.routes.webhook import _a2a_repo_fallback_check
        monkeypatch.setattr("src.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "tok-671")
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {})
        assert _a2a_repo_fallback_check(REPO_KEY) == "ok"
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {"error": "HTTP 404", "status_code": 404})
        assert _a2a_repo_fallback_check(REPO_KEY) == "not_found"
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {"error": "HTTP 403", "status_code": 403})
        assert _a2a_repo_fallback_check(REPO_KEY) == "no_permission"
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {"error": "conn timeout"})
        assert _a2a_repo_fallback_check(REPO_KEY) == "unreachable"
        monkeypatch.setattr("src.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "")
        assert _a2a_repo_fallback_check(REPO_KEY) == "no_token"

    # ── 幂等补录（_a2a_register_repo） ─────────────────────────────────────

    def test_register_repo_idempotent(self):
        from src.api.routes.webhook import _a2a_register_repo
        self._clean()
        assert _a2a_register_repo(REPO_KEY) is True
        assert _a2a_register_repo(REPO_KEY) is True
        assert self._managed_rows() == 1, "重复补录不得产生重复行"
        self._clean()

    # ── 端点行为 ───────────────────────────────────────────────────────────

    def test_registered_repo_no_fallback(self, monkeypatch):
        """已注册仓：不触发回退（回归，行为不变）。"""
        self._clean()
        self._seed_registered()

        def _no_call(*a, **kw):
            raise AssertionError("registered repo must not trigger fallback")
        monkeypatch.setattr("src.api.routes.webhook._a2a_repo_fallback_check", _no_call)
        r = self._client().post("/api/a2a", json=self._body())
        assert r.status_code == 200 and r.json().get("seq", 0) > 0, r.text
        self._clean()

    def test_unregistered_unauth_keeps_400_not_hosted(self, monkeypatch):
        """未注册 + 未认证：维持原 400 not hosted，不触发回退外呼。"""
        self._clean()

        def _no_call(*a, **kw):
            raise AssertionError("unauthenticated caller must not trigger fallback probe")
        monkeypatch.setattr("src.api.routes.webhook._a2a_repo_fallback_check", _no_call)
        r = self._client().post("/api/a2a", json=self._body())
        assert r.status_code == 400, (r.status_code, r.text)
        assert "not hosted" in r.text
        self._clean()

    def test_unregistered_session_fallback_ok_registers(self, monkeypatch):
        """未注册 + 会话 token + GitCode 可达 → 放行 + 幂等补录。"""
        self._clean()
        monkeypatch.setattr("src.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "tok-671")
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {})
        token = self._session_token()
        client = self._client()
        r1 = client.post("/api/a2a", json=self._body(),
                         headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 200 and r1.json().get("seq", 0) > 0, r1.text
        assert self._managed_rows() == 1, "回退放行后应补录 managed_repos"
        # 重复调用：补录幂等（不产生重复行），事件可重复派发（无幂等键）
        r2 = client.post("/api/a2a", json=self._body(),
                         headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200, r2.text
        assert self._managed_rows() == 1, "重复补录不得产生重复行"
        self._clean()

    def test_unregistered_session_fallback_not_found_400(self, monkeypatch):
        self._clean()
        monkeypatch.setattr("src.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "tok-671")
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {"error": "HTTP 404", "status_code": 404})
        token = self._session_token()
        r = self._client().post("/api/a2a", json=self._body(),
                                headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, (r.status_code, r.text)
        assert "not found" in r.text
        assert self._managed_rows() == 0

    def test_unregistered_session_fallback_no_permission_400(self, monkeypatch):
        self._clean()
        monkeypatch.setattr("src.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "tok-671")
        monkeypatch.setattr("src.vcs.api_client.VcsApiClient.get_repo_info",
                            lambda self: {"error": "HTTP 403", "status_code": 403})
        token = self._session_token()
        r = self._client().post("/api/a2a", json=self._body(),
                                headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, (r.status_code, r.text)
        assert "no access" in r.text
        assert self._managed_rows() == 0

    def test_unregistered_session_no_token_keeps_not_hosted(self, monkeypatch):
        """回退无法判定（无 token）→ 维持「未托管」安全默认。"""
        self._clean()
        monkeypatch.setattr("src.token_resolver.TokenResolver.get_token",
                            lambda self, rk: "")
        token = self._session_token()
        r = self._client().post("/api/a2a", json=self._body(),
                                headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400, (r.status_code, r.text)
        assert "not hosted" in r.text
        self._clean()


class TestCreateRepoFallbackRegistration:
    """Issue #671 ②：创建后托管失败/webhook_url 缺失 → 兜底注册最小行。"""

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/newrepo671'")
            try:
                c.execute("DELETE FROM user_repos WHERE repo_key='gitcode:owner/newrepo671'")
            except Exception:
                pass
            db.commit()

    def test_create_host_missing_registers_minimal_row(self, monkeypatch):
        self._clean()
        from src.api.routes.repos import create_new_repo, CreateRepoBody
        monkeypatch.setattr("src.api.routes.repos.get_user_token_with_refresh",
                            lambda username, user_id=0, **kw: "tok-u")
        monkeypatch.setattr("src.api.routes.repos.create_repo",
                            lambda api_token, name, description="", private=False, **kw: {
                                "full_name": "owner/newrepo671", "owner": "owner", "name": name})
        monkeypatch.setattr("src.user.db.get_webhook_url", lambda: "")
        monkeypatch.setattr("src.api.routes.repos._fetch_and_cache", lambda *a, **kw: [])
        user = {"id": 1, "username": "u671", "auth_type": "jwt"}
        body = CreateRepoBody(name="newrepo671", provider="gitcode", host_after_create=True)
        out = create_new_repo(body, user)
        assert out.get("hosted") is False and "webhook_url" in out.get("message", ""), out
        with db_lock:
            row = db.cursor().execute(
                "SELECT repo_owner, repo_name, host_status FROM managed_repos "
                "WHERE repo_key='gitcode:owner/newrepo671'").fetchone()
        assert row is not None, "托管未完成也应进入 managed_repos（注册链路兜底）"
        assert row[0] == "owner" and row[1] == "newrepo671" and row[2] == "webhook_missing"
        from src.user.db import managed_repo_exists
        assert managed_repo_exists("gitcode:owner/newrepo671") is True
        self._clean()

    def test_managed_repo_exists_helper(self):
        from src.user.db import managed_repo_exists
        self._clean()
        assert managed_repo_exists("gitcode:owner/newrepo671") is False
        assert managed_repo_exists("") is False
        self._clean()


class TestRunbookDoc:
    """Issue #671 ③：A2A runbook 含 400 repo not hosted 降级路径。"""

    def test_runbook_contains_degradation_path(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "help", "api", "12-a2a.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        assert "repo not hosted" in text
        assert "重试" in text and "人工交办" in text
