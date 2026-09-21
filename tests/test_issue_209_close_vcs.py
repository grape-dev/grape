"""Issue #209: confirm-done 同步关闭 GitCode/GitHub Issue 测试。

覆盖：
- VcsApiClient.close_issue 三平台分支（gitcode/github/gitlab）
- VcsApiClient.get_issue_state
- confirm_issue_done 响应含 issue_closed 字段
- 降级：VCS 关闭失败不阻断本地完成
- 幂等：已 completed 的 Issue 再次 confirm-done 不调用关闭 API
- 幂等：VCS Issue 已 closed 时 close 被跳过（skipped）
"""
import os
import sys
from unittest.mock import patch, MagicMock

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.vcs.api_client import VcsApiClient

client = TestClient(app)

_uid = None
_headers = {}


def _jwt_access_token(user_id, username="test209"):
    from src.api.jwt_utils import create_access_token
    from src.user.db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def setup_module():
    global _uid, _headers
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='test209_user'")
        db.commit()
    _uid = user_db.create_user("test209_user", "test123")
    if not _uid:
        _uid = user_db.get_user_by_username("test209_user")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id) "
            "VALUES (?,?,?,?,?)",
            ("gitcode:org/issue209", "org", "issue209", "org/issue209", 1))
        db.commit()


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='test209_user'")
        c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:org/issue209'")
        c.execute("DELETE FROM managed_repos WHERE repo_key='github:org/issue209'")
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue")
        c.execute("DELETE FROM issue_state WHERE repo_key='gitcode:org/issue209'")
        db.commit()


def _insert_task(status='processing', issue_number=300, repo_key="gitcode:org/issue209"):
    """插入测试任务，返回 task_id"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "test209_user", repo_key, issue_number,
             "Test #209-" + str(issue_number), "desc", status))
        db.commit()
        return c.lastrowid


# ===================== VcsApiClient.close_issue 单元测试 =====================

class TestVcsApiClientCloseIssue:
    """VcsApiClient.close_issue / get_issue_state 三平台测试"""

    def test_close_issue_gitcode(self):
        """GitCode 平台：PATCH /repos/{owner}/{repo}/issues/{n}"""
        c = VcsApiClient("gitcode", "fake-token", "org", "repo")
        with patch.object(c, "_gitcode_request", return_value={"state": "closed", "number": 5}) as m:
            resp = c.close_issue(5)
        m.assert_called_once_with("PATCH", "/repos/org/repo/issues/5", {"state": "closed"})
        assert resp["state"] == "closed"

    def test_close_issue_github(self):
        """GitHub 平台：PATCH /repos/{owner}/{repo}/issues/{n}"""
        c = VcsApiClient("github", "fake-token", "org", "repo")
        with patch.object(c, "_github_request", return_value={"state": "closed", "number": 5}) as m:
            resp = c.close_issue(5)
        m.assert_called_once_with("PATCH", "/repos/org/repo/issues/5", {"state": "closed"})
        assert resp["state"] == "closed"

    def test_close_issue_gitlab(self):
        """GitLab 平台：PUT /projects/{id}/issues/{n} with state_event"""
        c = VcsApiClient("gitlab", "fake-token", "org", "repo")
        with patch.object(c, "_gitlab_request", return_value={"state": "closed"}) as m:
            resp = c.close_issue(5)
        m.assert_called_once()
        path = m.call_args[0][1]
        assert "/projects/org%2Frepo/issues/5" in path
        assert m.call_args[0][2] == {"state_event": "close"}
        assert resp["state"] == "closed"

    def test_close_issue_unsupported_provider(self):
        c = VcsApiClient("bitbucket", "fake-token", "org", "repo")
        resp = c.close_issue(5)
        assert "error" in resp

    def test_get_issue_state_open(self):
        c = VcsApiClient("gitcode", "fake-token", "org", "repo")
        with patch.object(c, "get_issue", return_value={"state": "open"}):
            assert c.get_issue_state(5) == "open"

    def test_get_issue_state_closed(self):
        c = VcsApiClient("gitcode", "fake-token", "org", "repo")
        with patch.object(c, "get_issue", return_value={"state": "closed"}):
            assert c.get_issue_state(5) == "closed"

    def test_get_issue_state_error(self):
        c = VcsApiClient("gitcode", "fake-token", "org", "repo")
        with patch.object(c, "get_issue", return_value={"error": "HTTP 404"}):
            assert c.get_issue_state(5) == ""


# ===================== confirm_issue_done 集成测试 =====================

class TestConfirmDoneCloseIssues:
    """confirm-done 同步关闭 VCS Issue 测试"""

    def test_confirm_done_response_has_issue_closed(self):
        """confirm-done 响应含 issue_closed 字段"""
        _clean()
        tid = _insert_task(status='processing', issue_number=301)
        with patch("src.api.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "success", "github": "skipped"}) as m:
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "completed"
        assert data["stage"] == "done"
        assert "issue_closed" in data
        assert data["issue_closed"]["gitcode"] == "success"
        assert data["issue_closed"]["github"] == "skipped"
        m.assert_called_once()

    def test_confirm_done_vcs_failure_does_not_block_local(self):
        """VCS 关闭失败时本地状态仍 completed"""
        _clean()
        tid = _insert_task(status='processing', issue_number=302)
        with patch("src.api.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "failed", "github": "failed"}):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert r.json()["issue_closed"]["gitcode"] == "failed"
        # DB 确认本地完成
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM issue WHERE id=?", (tid,)).fetchone()
        assert row[0] == "completed"

    def test_already_done_idempotent_no_close_call(self):
        """已 completed 的 Issue 再次 confirm-done 不调用关闭 API"""
        _clean()
        tid = _insert_task(status='completed', issue_number=303)
        with patch("src.api.routes.issues._close_vcs_issues") as m:
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["already_done"] is True
        assert r.json()["issue_closed"]["gitcode"] == "skipped"
        assert r.json()["issue_closed"]["github"] == "skipped"
        m.assert_not_called()

    def test_close_vcs_issues_no_issue_number(self):
        """无 issue_number 时两侧均 skipped"""
        from src.api.routes.issues import _close_vcs_issues
        result = _close_vcs_issues("gitcode:org/repo", None, {"id": 1, "username": "u"})
        assert result == {"gitcode": "skipped", "github": "skipped"}

    def test_close_vcs_issues_no_token_skipped(self):
        """无 token 时返回 skipped（不报错）"""
        _clean()
        tid = _insert_task(status='processing', issue_number=304)
        # patch token 获取返回空
        with patch("src.api.deps.get_user_token_with_refresh", return_value=""):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["issue_closed"]["gitcode"] == "skipped"

    def test_close_vcs_issues_already_closed_skips_api(self):
        """VCS Issue 已 closed → get_issue_state 返回 closed → close_issue 不被调用"""
        _clean()
        tid = _insert_task(status='processing', issue_number=305)
        mock_client = MagicMock()
        mock_client.get_issue_state.return_value = "closed"
        with patch("src.api.deps.get_user_token_with_refresh", return_value="fake-token"), \
             patch("src.vcs.api_client.VcsApiClient", return_value=mock_client):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["issue_closed"]["gitcode"] == "skipped"
        mock_client.get_issue_state.assert_called_once_with(305)
        mock_client.close_issue.assert_not_called()

    def test_close_vcs_issues_success_calls_close(self):
        """VCS Issue open → get_issue_state 返回 open → close_issue 被调用且成功"""
        _clean()
        tid = _insert_task(status='processing', issue_number=306)
        mock_client = MagicMock()
        mock_client.get_issue_state.return_value = "open"
        mock_client.close_issue.return_value = {"state": "closed", "number": 306}
        with patch("src.api.deps.get_user_token_with_refresh", return_value="fake-token"), \
             patch("src.vcs.api_client.VcsApiClient", return_value=mock_client):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["issue_closed"]["gitcode"] == "success"
        mock_client.close_issue.assert_called_once_with(306)

    def test_close_vcs_issues_api_error_returns_failed(self):
        """close_issue 返回 error → 状态 failed"""
        _clean()
        tid = _insert_task(status='processing', issue_number=307)
        mock_client = MagicMock()
        mock_client.get_issue_state.return_value = "open"
        mock_client.close_issue.return_value = {"error": "HTTP 403"}
        with patch("src.api.deps.get_user_token_with_refresh", return_value="fake-token"), \
             patch("src.vcs.api_client.VcsApiClient", return_value=mock_client):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["issue_closed"]["gitcode"] == "failed"
