"""Issue #393: 仓库 admin 成员确认需求完成（人工终审）测试。

覆盖（对照 Spec Acceptance）：
- admin（owner / permission 判定）确认 → status=completed、stage=done、审计留痕
- 非 admin（access_level=30 developer）→ 403
- 未绑定 GitCode 的用户 → 403（fail-closed）
- VCS permission 查询异常 → 403（fail-closed，不越权放行）
- 重复确认幂等（already_done，不重复审计、不重复关 VCS）
- cancelled 需求 → 400
- tk_ 会话 token → 403（user-only）
- 流水线同步：agent_tasks 未终态任务 → cancelled；events running → completed
- 审计记录字段（操作人 / 时间 / 目标需求 repo_key/issue_id/issue_number）
- 管理员可确认他人创建的需求（不受 user_id 归属限制，但需仓库访问权）
- 仓库外用户（managed_repos 无该仓库）→ 403
"""
import json
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.auth import grape_account_db

client = TestClient(app)

REPO_KEY = "gitcode:org/issue393"
OTHER_USER_ID = 999999  # 需求归属：非确认人本人（验证 admin 跨归属确认）

_uid = None
_other_uid = None
_headers = {}


def _jwt_access_token(user_id, username):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def setup_module():
    global _uid, _other_uid, _headers
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username IN ('test393_admin', 'test393_owner')")
        c.execute("DELETE FROM external_identities WHERE grape_user_id IN "
                  "(SELECT id FROM grape_accounts WHERE username IN ('test393_admin', 'test393_owner'))")
        c.execute("DELETE FROM external_identities WHERE external_login IN ('issue393_admin', 'org')")
        db.commit()
    # 确认人：绑定 gitcode（external_login=issue393_admin）
    _uid = grape_account_db.create_grape_account("test393_admin", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test393_admin")
    assert _uid, "create test account failed"
    _other_uid = _uid["id"] + 100000  # 他人归属（不真实存在，仅用于 user_id 字段）
    _bind_identity(_uid["id"], "gitcode", "issue393_admin")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}
    with db_lock:
        c = db.cursor()
        # 托管仓库（check_repo_access 需要 managed_repos + user_repos 可见性）
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id) "
            "VALUES (?,?,?,?,?)",
            (REPO_KEY, "org", "issue393", "org/issue393", 1))
        c.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
            "VALUES (?,?,?,?,?,?)",
            (_uid["id"], REPO_KEY, "org", "issue393", 0, 1))
        db.commit()


def _bind_identity(user_id: int, provider: str, login: str):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, external_name, "
            "external_avatar, access_token, refresh_token, token_expires_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, provider, f"ext-{login}", login, login, "", "", "", "", 1))
        db.commit()


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM audit_logs WHERE detail LIKE '%issue393%'")
        c.execute("DELETE FROM user_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM external_identities WHERE external_login IN ('issue393_admin', 'org')")
        c.execute("DELETE FROM grape_accounts WHERE username IN ('test393_admin', 'test393_owner')")
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM audit_logs WHERE detail LIKE '%issue393%'")
        db.commit()


def _insert_task(status='processing', issue_number=500):
    """插入**他人创建**的需求（user_id 非 0 且非确认人），返回 issue_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (OTHER_USER_ID, "someone_else", REPO_KEY, issue_number,
             "Test #393-" + str(issue_number), "desc", status))
        db.commit()
        return c.lastrowid


def _insert_agent_task(issue_number, status='running'):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, issue_id, repo_key, "
            "agent_type, prompt, status) VALUES (?,?,?,?,?,?,?,?)",
            (f"at393-{issue_number}-{status}", "sess393", 1, f"#{issue_number}", REPO_KEY,
             "qa", "prompt", status))
        db.commit()
        return f"at393-{issue_number}-{status}"


def _insert_event(issue_number, status='running'):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (issue_id, repo_key, event_type, agent_type, title, status) "
            "VALUES (?,?,?,?,?,?)",
            (f"#{issue_number}", REPO_KEY, "a2a_event", "qa", "t", status))
        db.commit()
        return c.lastrowid


def _audit_count():
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type='issue_admin_confirmed' "
            "AND detail LIKE '%issue393%'").fetchone()
    return row[0]


def _perm_payload(access_level=50, permission="admin"):
    return {"access_level": access_level, "permission": permission,
            "role_name": "Admin" if access_level >= 50 else "Developer",
            "permissions": {"admin": access_level >= 50}}


def _oauth_pass_through():
    """patch with_oauth_token：测试用户无真实 OAuth token，直通把 token 传给回调。"""
    return patch("src.api.oauth_token.with_oauth_token",
                 side_effect=lambda user, provider, fn, *a, **k: fn("fake-token", *a, **k))


class TestRepoRolesHelper:
    """src/auth/repo_roles.py 角色判定单元测试。"""

    def test_owner_is_admin_without_api_call(self):
        from src.auth.repo_roles import get_repo_admin_identity
        # 用独立账号绑 owner 身份，避免 UNIQUE(grape_user_id, provider) 约束
        # REPLACE 掉主账号的 issue393_admin 绑定（污染后续用例）。
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE username='test393_owner'")
            db.commit()
        _owner_uid = grape_account_db.create_grape_account("test393_owner", email="", password="test123")
        assert _owner_uid, "create owner account failed"
        _bind_identity(_owner_uid["id"], "gitcode", "org")
        try:
            ident = get_repo_admin_identity({"id": _owner_uid["id"]}, REPO_KEY)
            assert ident == {"username": "org", "source": "owner"}
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_owner_uid["id"],))
                c.execute("DELETE FROM grape_accounts WHERE id=?", (_owner_uid["id"],))
                db.commit()

    def test_unbound_user_is_not_admin(self):
        from src.auth.repo_roles import get_repo_admin_identity
        # user id 不存在 → 无绑定 → None（fail-closed，不发起 VCS 查询）
        assert get_repo_admin_identity({"id": 123456789}, REPO_KEY) is None

    def test_maintainer_40_is_not_admin(self):
        """#393 终审口径：Maintainer(40) 不是 admin（区别于托管口径 >=40）。"""
        from src.user import repo_manager
        with patch.object(repo_manager, "_req",
                          return_value=_perm_payload(access_level=40, permission="write")):
            from src.auth.repo_roles import _check_gitcode_admin as _chk
            assert _chk("t", "org", "repo", "u") is False
        with patch.object(repo_manager, "_req",
                          return_value=_perm_payload(access_level=50, permission="admin")):
            from src.auth.repo_roles import _check_gitcode_admin as _chk2
            assert _chk2("t", "org", "repo", "u") is True


class TestAdminConfirmDone:
    """POST /api/issues/{issue_id}/admin-confirm-done 集成测试。"""

    def test_issue_not_found(self):
        r = client.post("/api/issues/999999/admin-confirm-done", headers=_headers)
        assert r.status_code == 404

    def test_admin_confirm_success(self):
        """admin（access_level=50）确认他人需求 → completed + done + 审计。"""
        _clean()
        tid = _insert_task(issue_number=501)
        with _oauth_pass_through(), \
             patch("src.auth.repo_roles._check_gitcode_admin", return_value=True), \
             patch("src.api.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "success", "github": "skipped"}) as m_close:
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "completed"
        assert data["stage"] == "done"
        assert data["confirmed_by"] == "admin"
        m_close.assert_called_once()
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status, completed_reason FROM issue WHERE id=?", (tid,)).fetchone()
        assert row[0] == "completed"
        assert row[1] == "admin_confirmed"
        assert _audit_count() == 1

    def test_owner_confirm_success(self):
        """仓库 owner 本人（external_login == owner）→ 快速路径 admin（端点级）。"""
        _clean()
        tid = _insert_task(issue_number=502)
        # 独立 owner 账号（user_repos 已有可见性 + owner 绑定），不动主账号绑定
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE username='test393_owner2'")
            db.commit()
        _owner_uid = grape_account_db.create_grape_account("test393_owner2", email="", password="test123")
        assert _owner_uid, "create owner2 account failed"
        _bind_identity(_owner_uid["id"], "gitcode", "org")
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
                "VALUES (?,?,?,?,?,?)",
                (_owner_uid["id"], REPO_KEY, "org", "issue393", 0, 1))
            db.commit()
        try:
            _tok = _jwt_access_token(_owner_uid["id"], _owner_uid["username"])
            with patch("src.api.routes.issues._close_vcs_issues",
                       return_value={"gitcode": "skipped", "github": "skipped"}):
                r = client.post(f"/api/issues/{tid}/admin-confirm-done",
                                headers={"Authorization": f"Bearer {_tok}"})
            assert r.status_code == 200
            assert r.json()["confirmed_by"] == "admin"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_owner_uid["id"],))
                c.execute("DELETE FROM user_repos WHERE user_id=?", (_owner_uid["id"],))
                c.execute("DELETE FROM grape_accounts WHERE id=?", (_owner_uid["id"],))
                db.commit()

    def test_non_admin_developer_403(self):
        """Developer（access_level=30）→ 403，状态不变，无审计。"""
        _clean()
        tid = _insert_task(issue_number=503)
        with _oauth_pass_through(), patch("src.auth.repo_roles._check_gitcode_admin", return_value=False):
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 403
        assert "管理员" in r.json()["detail"]["message"]
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM issue WHERE id=?", (tid,)).fetchone()
        assert row[0] == "processing"
        assert _audit_count() == 0

    def test_permission_api_error_403_fail_closed(self):
        """VCS 查询异常（网络/403）→ fail-closed 403。"""
        _clean()
        tid = _insert_task(issue_number=504)
        with _oauth_pass_through(), \
             patch("src.auth.repo_roles._check_gitcode_admin",
                   side_effect=RuntimeError("GitCode API GET failed (403): forbidden")):
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 403

    def test_unbound_user_403(self):
        """未绑定 gitcode 的用户 → 403（fail-closed，无 VCS 查询）。"""
        _clean()
        tid = _insert_task(issue_number=505)
        # 构造一个无绑定的用户 JWT
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE username='test393_nobind'")
            db.commit()
        _nobind = grape_account_db.create_grape_account("test393_nobind", email=None, password="test123")
        try:
            tok = _jwt_access_token(_nobind["id"], "test393_nobind")
            r = client.post(f"/api/issues/{tid}/admin-confirm-done",
                            headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 403
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM grape_accounts WHERE username='test393_nobind'")
                db.commit()

    def test_already_done_idempotent(self):
        """已 completed → already_done，不重复审计、不关 VCS。"""
        _clean()
        tid = _insert_task(status='completed', issue_number=506)
        with _oauth_pass_through() as _m_oauth, \
             patch("src.api.routes.issues._close_vcs_issues") as m_close, \
             patch("src.auth.repo_roles._check_gitcode_admin", return_value=True):
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 200
        assert r.json()["already_done"] is True
        m_close.assert_not_called()
        assert _audit_count() == 0

    def test_cancelled_400(self):
        _clean()
        tid = _insert_task(status='cancelled', issue_number=507)
        with _oauth_pass_through(), patch("src.auth.repo_roles._check_gitcode_admin", return_value=True):
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 400

    def test_session_token_rejected(self):
        """tk_ 会话 token → 403（user-only 端点）。"""
        _clean()
        tid = _insert_task(issue_number=508)
        # 签发真实 tk_ 会话 token（get_current_user_or_api_key 可解析 auth_type=session_token）
        from src.api.auth_token import issue_session_token
        _tk = issue_session_token(session_key="sess393-tk", repo_key=REPO_KEY,
                                  agent_type="qa", user_id=0)
        try:
            r = client.post(f"/api/issues/{tid}/admin-confirm-done",
                            headers={"Authorization": f"Bearer {_tk}"})
            assert r.status_code == 403
        finally:
            from src.session_manager import db as _db, db_lock as _dbl
            with _dbl:
                c = _db.cursor()
                c.execute("DELETE FROM session_api_tokens WHERE session_key='sess393-tk'")
                _db.commit()

    def test_pipeline_synced_to_terminal(self):
        """确认后 agent_tasks 未终态 → cancelled；events running → completed。"""
        _clean()
        tid = _insert_task(issue_number=509)
        _at = _insert_agent_task(509, status='running')
        _at2 = _insert_agent_task(509, status='queued')
        _ev = _insert_event(509, status='running')
        with _oauth_pass_through(), \
             patch("src.auth.repo_roles._check_gitcode_admin", return_value=True), \
             patch("src.api.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "skipped", "github": "skipped"}):
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 200
        assert r.json()["synced_tasks"] == 2
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT status FROM agent_tasks WHERE id IN (?,?)", (_at, _at2)).fetchall()
            ev_row = c.execute("SELECT status FROM events WHERE seq=?", (_ev,)).fetchone()
        assert all(s[0] == 'cancelled' for s in rows)
        assert ev_row[0] == 'completed'

    def test_audit_record_fields(self):
        """审计记录含操作人/时间/目标需求（repo_key/issue_id/issue_number）。"""
        _clean()
        tid = _insert_task(issue_number=510)
        with _oauth_pass_through(), \
             patch("src.auth.repo_roles._check_gitcode_admin", return_value=True), \
             patch("src.api.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "skipped", "github": "skipped"}):
            r = client.post(f"/api/issues/{tid}/admin-confirm-done", headers=_headers)
        assert r.status_code == 200
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT event_type, operator_id, detail, created_at FROM audit_logs "
                "WHERE event_type='issue_admin_confirmed' AND detail LIKE '%issue393%' "
                "ORDER BY seq DESC LIMIT 1").fetchone()
        assert row is not None
        assert row[0] == "issue_admin_confirmed"
        assert row[1] == _uid["id"]  # 操作人
        assert row[3]  # 时间
        detail = json.loads(row[2])
        assert detail["repo_key"] == REPO_KEY
        assert detail["issue_id"] == tid
        assert detail["issue_number"] == 510
        assert detail["username"] == "issue393_admin"
        assert detail["role_source"] == "permission"

    def test_no_repo_access_403(self):
        """用户与该仓库无关联（managed_repos/user_repos 均不可见）→ 403。"""
        _clean()
        tid = _insert_task(issue_number=511)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE username='test393_outsider'")
            db.commit()
        _outsider = grape_account_db.create_grape_account("test393_outsider", email=None, password="test123")
        try:
            tok = _jwt_access_token(_outsider["id"], "test393_outsider")
            r = client.post(f"/api/issues/{tid}/admin-confirm-done",
                            headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 403
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM grape_accounts WHERE username='test393_outsider'")
                db.commit()
