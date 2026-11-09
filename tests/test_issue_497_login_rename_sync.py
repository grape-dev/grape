"""Issue #497: GitCode 登录名变更后 OAuth 登录同步 external_login（TDD）。

验收覆盖：
- 改名用户重新 OAuth 登录后四表自动同步为新登录名；
- username UNIQUE 冲突时保留旧 username 且 external_login 仍正确；
- audit_logs 记录 old/new；
- 未改名用户登录零回归（不写库）；
- issues_center assignee fallback 数据源在同步后取到新名（不改取值逻辑）。
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["DEVOPS_DB_PATH"] = "/tmp/test_issue_497_login_rename.db"
if os.path.exists("/tmp/test_issue_497_login_rename.db"):
    os.remove("/tmp/test_issue_497_login_rename.db")

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from src.api.main import app
from src.session_manager import db, db_lock
from src.api.db_setup import _create_tables
from src.auth.grape_account_db import (
    create_grape_account,
    create_external_identity,
    get_external_identity,
    get_grape_account_by_id,
    get_user_identities,
    delete_external_identity,
)

_create_tables()

client = TestClient(app)


class _FakeReqResp:
    """模拟 requests.get/post 的返回值（沿用 test_auth_routes.py 模式）。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self):
        pass


def _gid():
    return str(20_000_000 + uuid.uuid4().int % 100_000_000)


def _save_state(provider="gitcode"):
    from src.user import db as user_db
    state = "test-state-" + uuid.uuid4().hex[:8]
    user_db.save_oauth_state(state, provider, {})
    return state


def _cleanup(username: str, ext_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=? OR github_login=?", (username, ""))
        c.execute("DELETE FROM login_whitelist WHERE gitcode_username=? OR github_username=?", (username, username))
        c.execute("DELETE FROM audit_logs WHERE user_id IN "
                  "(SELECT id FROM grape_accounts WHERE username=?)", (username,))
        c.execute("DELETE FROM external_identities WHERE external_id=? OR external_login=?", (ext_id, username))
        c.execute("DELETE FROM grape_accounts WHERE username=?", (username,))
        db.commit()


def _seed_user(username, ext_id, external_login):
    """创建用户 + gitcode 绑定 + um 行 + 白名单行（模拟历史四表固化）。"""
    account = create_grape_account(username=username, email="", password="pw")
    assert account, "seed account failed"
    ok = create_external_identity(
        grape_user_id=account["id"], provider="gitcode", external_id=ext_id,
        external_login=external_login, external_name="旧名",
        access_token="at", refresh_token="rt",
    )
    assert ok, "seed identity failed"
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id, gitcode_user) VALUES (?, ?)",
                  (account["id"], external_login))
        c.execute("INSERT OR IGNORE INTO login_whitelist (gitcode_username) VALUES (?)", (external_login,))
        db.commit()
    return account


# ===========================================================================
# 同步函数单元测试（sync_external_login_on_rename）
# ===========================================================================

class TestSyncExternalLoginOnRename:
    def test_sync_updates_four_tables_and_audit(self):
        """改名同步：external_identities + grape_accounts.username + um + whitelist + audit。"""
        from src.auth.grape_account_db import sync_external_login_on_rename
        uname = "rn_user_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        old_login = "gcw_OldRenamed"
        new_login = "NewRenamed_" + uuid.uuid4().hex[:4]
        account = _seed_user(uname, ext_id, old_login)
        try:
            got = sync_external_login_on_rename(account["id"], "gitcode", new_login, old_login)
            assert got is True, "rename sync should return True"

            # 1. external_identities.external_login
            binding = get_external_identity("gitcode", ext_id)
            assert binding is not None
            assert binding["external_login"] == new_login

            # 2. grape_accounts.username
            acc = get_grape_account_by_id(account["id"])
            assert acc["username"] == new_login

            # 3. unified_user_mapping.gitcode_user
            with db_lock:
                row = db.cursor().execute(
                    "SELECT gitcode_user FROM unified_user_mapping WHERE user_id=?", (account["id"],)).fetchone()
            assert row and row[0] == new_login

            # 4. login_whitelist.gitcode_username
            with db_lock:
                row = db.cursor().execute(
                    "SELECT gitcode_username FROM login_whitelist WHERE id IN "
                    "(SELECT id FROM login_whitelist WHERE gitcode_username=?)", (new_login,)).fetchone()
            assert row and row[0] == new_login

            # 5. audit_logs login_rename 留痕 old/new
            with db_lock:
                row = db.cursor().execute(
                    "SELECT event_type, detail FROM audit_logs WHERE user_id=? AND event_type='login_rename' "
                    "ORDER BY seq DESC LIMIT 1", (account["id"],)).fetchone()
            assert row is not None
            assert '"old_login": "%s"' % old_login in row[1]
            assert '"new_login": "%s"' % new_login in row[1]
        finally:
            _cleanup(new_login, ext_id)

    def test_username_unique_conflict_keeps_old_username(self):
        """新名已被其他账号占用：保留旧 username，external_login 仍正确更新。"""
        from src.auth.grape_account_db import sync_external_login_on_rename
        uname = "rn_cf_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        conflict_user = "rn_conflict_" + uuid.uuid4().hex[:6]
        old_login = "gcw_OldConflict"
        new_login = "OccupiedName_" + uuid.uuid4().hex[:4]

        account = _seed_user(uname, ext_id, old_login)
        # 另一个账号已占用 new_login 作为 username
        other = create_grape_account(username=new_login, email="", password="pw")
        try:
            got = sync_external_login_on_rename(account["id"], "gitcode", new_login, old_login)
            assert got is True

            # username 冲突 → 保留旧 username
            acc = get_grape_account_by_id(account["id"])
            assert acc["username"] == uname

            # external_login 仍正确更新
            binding = get_external_identity("gitcode", ext_id)
            assert binding["external_login"] == new_login

            # um 同步为新名
            with db_lock:
                row = db.cursor().execute(
                    "SELECT gitcode_user FROM unified_user_mapping WHERE user_id=?", (account["id"],)).fetchone()
            assert row and row[0] == new_login

            # 占用者账号不受影响
            other_acc = get_grape_account_by_id(other["id"])
            assert other_acc["username"] == new_login
        finally:
            _cleanup(uname, ext_id)
            with db_lock:
                db.cursor().execute("DELETE FROM grape_accounts WHERE id=?", (other["id"],))
                db.commit()

    def test_no_change_returns_false_no_write(self):
        """未改名（new == old）：返回 False，不产生额外写/audit。"""
        from src.auth.grape_account_db import sync_external_login_on_rename
        from src.auth.audit import EVENT_TYPES
        uname = "rn_nc_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        old_login = "gcw_NoChange"
        account = _seed_user(uname, ext_id, old_login)
        try:
            with db_lock:
                before = db.cursor().execute(
                    "SELECT external_login FROM external_identities WHERE external_id=?", (ext_id,)).fetchone()[0]
                audit_before = db.cursor().execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE event_type='login_rename'").fetchone()[0]
            got = sync_external_login_on_rename(account["id"], "gitcode", old_login, old_login)
            assert got is False
            with db_lock:
                after = db.cursor().execute(
                    "SELECT external_login FROM external_identities WHERE external_id=?", (ext_id,)).fetchone()[0]
                audit_after = db.cursor().execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE event_type='login_rename'").fetchone()[0]
            assert after == before == old_login
            assert audit_after == audit_before
            assert "login_rename" in EVENT_TYPES
        finally:
            _cleanup(uname, ext_id)

    def test_empty_new_login_returns_false(self):
        """空 new_login：返回 False 不写。"""
        from src.auth.grape_account_db import sync_external_login_on_rename
        uname = "rn_empty_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        account = _seed_user(uname, ext_id, "gcw_Empty")
        try:
            assert sync_external_login_on_rename(account["id"], "gitcode", "", "gcw_Empty") is False
            assert sync_external_login_on_rename(account["id"], "gitcode", None, "gcw_Empty") is False
        finally:
            _cleanup(uname, ext_id)


# ===========================================================================
# 回调接线测试（已绑定分支 + rebind 路径）
# ===========================================================================

class TestOAuthCallbackLoginRename:
    def test_bound_branch_syncs_renamed_login(self):
        """已绑定分支：OAuth 登录返回新登录名 → 四表自动同步。"""
        uname = "rn_cb_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        old_login = "gcw_OldCallback"
        new_login = "NewCallback_" + uuid.uuid4().hex[:4]
        account = _seed_user(uname, ext_id, old_login)
        state = _save_state()
        try:
            with patch("src.user.db.get_meta_value", return_value="x"), \
                 patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
                 patch("requests.get", return_value=_FakeReqResp({"id": ext_id, "login": new_login,
                                                                  "nickname": "新昵称"})):
                r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                               follow_redirects=False)
            assert r.status_code == 302
            # external_login 已同步
            assert get_external_identity("gitcode", ext_id)["external_login"] == new_login
            # username 已同步
            assert get_grape_account_by_id(account["id"])["username"] == new_login
            # audit 留痕
            with db_lock:
                row = db.cursor().execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE user_id=? AND event_type='login_rename'",
                    (account["id"],)).fetchone()
            assert row[0] >= 1
        finally:
            _cleanup(new_login, ext_id)

    def test_unbound_rebind_path_syncs_renamed_login(self):
        """rebind 路径：解绑后重新登录（同 external_id 新登录名）→ 恢复绑定并同步新名。"""
        uname = "rn_rb_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        old_login = "gcw_OldRebind"
        new_login = "NewRebind_" + uuid.uuid4().hex[:4]
        account = _seed_user(uname, ext_id, old_login)
        # 解绑（status=0，软删除）
        assert delete_external_identity(account["id"], "gitcode") is True
        state = _save_state()
        try:
            with patch("src.user.db.get_meta_value", return_value="x"), \
                 patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
                 patch("requests.get", return_value=_FakeReqResp({"id": ext_id, "login": new_login,
                                                                  "nickname": "新昵称"})):
                r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                               follow_redirects=False)
            assert r.status_code == 302
            # 绑定恢复且 external_login 同步为新名
            binding = get_external_identity("gitcode", ext_id)
            assert binding is not None
            assert binding["external_login"] == new_login
            # username 同步
            assert get_grape_account_by_id(account["id"])["username"] == new_login
            # audit 留痕
            with db_lock:
                row = db.cursor().execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE user_id=? AND event_type='login_rename'",
                    (account["id"],)).fetchone()
            assert row[0] >= 1
        finally:
            _cleanup(new_login, ext_id)

    def test_unchanged_login_no_regression(self):
        """未改名用户登录：不产生 login_rename，external_login 不变（零回归）。"""
        uname = "rn_ur_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        old_login = "gcw_Unchanged"
        account = _seed_user(uname, ext_id, old_login)
        state = _save_state()
        try:
            with patch("src.user.db.get_meta_value", return_value="x"), \
                 patch("requests.post", return_value=_FakeReqResp({"access_token": "at", "refresh_token": "rt"})), \
                 patch("requests.get", return_value=_FakeReqResp({"id": ext_id, "login": old_login,
                                                                  "nickname": "旧名"})):
                r = client.get("/api/auth/callback/gitcode", params={"state": state, "code": "c"},
                               follow_redirects=False)
            assert r.status_code == 302
            assert get_external_identity("gitcode", ext_id)["external_login"] == old_login
            with db_lock:
                row = db.cursor().execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE user_id=? AND event_type='login_rename'",
                    (account["id"],)).fetchone()
            assert row[0] == 0
        finally:
            _cleanup(uname, ext_id)


# ===========================================================================
# issues_center assignee fallback 数据源验证（不改取值逻辑）
# ===========================================================================

class TestAssigneeFallbackDataSource:
    def test_user_identities_return_new_login_after_sync(self):
        """同步后 get_user_identities（#343 assignee fallback 的数据源）返回新登录名。

        issues_center 建单链路 assignee 取值：body.assignee 为空时，
        遍历 get_user_identities(user.id) 取 provider 匹配的 external_login。
        本测试验证同步后的数据源已指向新名（不改 issues_center 取值逻辑）。
        """
        from src.auth.grape_account_db import sync_external_login_on_rename
        uname = "rn_asg_" + uuid.uuid4().hex[:6]
        ext_id = _gid()
        old_login = "gcw_OldAssignee"
        new_login = "NewAssignee_" + uuid.uuid4().hex[:4]
        account = _seed_user(uname, ext_id, old_login)
        try:
            sync_external_login_on_rename(account["id"], "gitcode", new_login, old_login)
            # issues_center 的 fallback 恰好取 provider=='gitcode' 且 external_login 非空的绑定
            assignee = ""
            for ident in get_user_identities(account["id"]):
                if ident.get("provider") == "gitcode" and ident.get("external_login"):
                    assignee = ident["external_login"]
                    break
            assert assignee == new_login
            assert assignee != old_login
        finally:
            _cleanup(new_login, ext_id)