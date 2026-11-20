"""Issue #3394-A 回归：无 Grape 密码账号清 PAT 不得被「至少保留一种登录方式」误拦。

复现形态（beta 实测）：
  - 账号无 password_hash，只有 1 条身份行 + 1 个平台 PAT；
  - 设置页清该平台 PAT → 400「至少保留一种登录方式，请先设置 Grape 密码」，
    而同一请求里凭据已被清空（接口报错、数据已变）。

口径：清 PAT 不是登录方式变更——
  - 有 OAuth：身份行原样保留（只清凭据域），登录方式一条没少；
  - 无 OAuth：删的是 PAT 归属行（access_token 恒空），同样不是登录凭据。
真正的登录保底在身份解绑路径（grape_auth.unbind_provider → UnbindGuard /
KeepOnePredicate 原子裁决），本用例不改那条语义。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from src.api.main import app  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

client = TestClient(app)


@pytest.fixture()
def no_password_user():
    """无 Grape 密码账号 + JWT 头（复现 #3394 的真实账号形态）。"""
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    from src.kernel.auth_sdk.jwt_utils import create_access_token

    username = f"it3394_{uuid.uuid4().hex[:8]}"
    acc = create_grape_account(username=username, email="", password="tmp-pass")
    assert acc and acc.get("id"), "create_grape_account 未返回 uuid 主键"
    uid = acc["id"]
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_accounts SET password_hash='' WHERE id=?", (uid,))
        db.commit()
        row = c.execute("SELECT token_version FROM grape_accounts WHERE id=?",
                        (uid,)).fetchone()
    ver = (row[0] if row else 0) or 0
    headers = {"Authorization": "Bearer " + create_access_token(str(uid), username, ver)}
    yield {"id": uid, "headers": headers}
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        db.commit()


def _clear_pat(user: dict, provider: str = "gitcode"):
    return client.put("/api/settings/credentials",
                      json={"platform_tokens": {provider: ""}},
                      headers=user["headers"])


def test_clear_pat_keeps_oauth_identity_and_clears_credential(no_password_user):
    """有 OAuth + 无密码：200，身份行保留（OAuth token 不动），凭据域清空。"""
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, get_user_identities)
    from src.domains.mgmt.user.db import get_credentials, set_credential

    uid = no_password_user["id"]
    assert create_external_identity(uid, "gitcode", f"ext{uuid.uuid4().hex[:6]}",
                                    access_token="oauth-access-token")
    assert set_credential(uid, "gitcode", {"token": "pat-value", "login": "gclogin"},
                          kind="pat")
    # OAuth 刷新 fallback（kind='oauth' 凭据行）不得被清 PAT 连带撤掉
    assert set_credential(uid, "gitcode", {"access_token": "oauth-cred"},
                          kind="oauth")

    r = _clear_pat(no_password_user)
    assert r.status_code == 200, r.text

    idents = {i["provider"]: i for i in get_user_identities(uid)}
    assert "gitcode" in idents, "清 PAT 不得删 OAuth 身份行"
    assert idents["gitcode"]["has_oauth_token"] is True, "OAuth token 必须原样保留"
    creds = {(c["provider"], c.get("kind", "")): (c.get("config") or {})
             for c in get_credentials(uid)}
    assert not (creds.get(("gitcode", "pat")) or {}).get("token"), "PAT 凭据必须已清空"
    assert (creds.get(("gitcode", "oauth")) or {}).get("access_token") == "oauth-cred", \
        "清 PAT 只撤 pat 凭据，kind='oauth' 行必须保留（刷新 fallback）"


def test_clear_pat_without_oauth_removes_pat_owner_row(no_password_user):
    """无 OAuth + 无密码 + 仅一条 PAT 归属行：200，且归属行被软删（status=0）。

    keep-one 判据会把这一行本身数成最后一种登录方式而恒拒绝（原子 UPDATE
    rowcount=0 → 静默不删），所以该分支必须显式 require_keep_one=False。
    """
    from src.domains.mgmt.auth.grape_account_db import create_external_identity
    from src.domains.mgmt.user.db import set_credential

    uid = no_password_user["id"]
    assert create_external_identity(uid, "gitcode", f"ext{uuid.uuid4().hex[:6]}",
                                    access_token="")
    assert set_credential(uid, "gitcode", {"token": "pat-only", "login": "gclogin"},
                          kind="pat")

    r = _clear_pat(no_password_user)
    assert r.status_code == 200, r.text

    with db_lock:
        row = db.cursor().execute(
            "SELECT status FROM external_identities "
            "WHERE grape_user_id=? AND provider='gitcode'", (uid,)).fetchone()
    assert row is not None, "归属行应保留为软删行（供票据/回登识别），不是物理删除"
    assert int(row[0]) == 0, "无 OAuth 清 PAT 必须软删 PAT 归属行"


def test_clear_pat_aborts_when_identity_read_fails(no_password_user, monkeypatch):
    """身份读失败（degraded read）时不得删除身份行——「没读到」≠「没有」。

    非 strict 读会把异常吞成空列表 → has_oauth 判假 → 误删真 OAuth 行并清 token，
    无密码账号变成 0 种登录方式而接口仍报成功。破坏性写前必须 strict 读并整体放弃。
    """
    from src.domains.mgmt.auth.grape_account_db import create_external_identity
    from src.domains.mgmt.user.db import set_credential
    from src.kernel.persistence.repositories import identity_admin_repository as iar

    uid = no_password_user["id"]
    assert create_external_identity(uid, "gitcode", f"ext{uuid.uuid4().hex[:6]}",
                                    access_token="oauth-token-live")
    assert set_credential(uid, "gitcode", {"token": "pat-live", "login": "acme"},
                          kind="pat")

    def _boom(*a, **k):
        raise RuntimeError("simulated identity read failure")

    monkeypatch.setattr(iar, "list_by_user_statuses", _boom)

    r = _clear_pat(no_password_user)
    assert r.status_code == 500, r.text
    assert r.json()["detail"]["code"] == 500

    with db_lock:
        row = db.cursor().execute(
            "SELECT status, length(access_token) FROM external_identities "
            "WHERE grape_user_id=? AND provider='gitcode'", (uid,)).fetchone()
    assert row is not None, "读失败时不得软删身份行"
    assert int(row[0]) == 1, "读失败时身份行必须保持活跃"
    assert int(row[1]) > 0, "读失败时 OAuth token 必须原样保留"


def test_clear_pat_does_not_unlock_identity_unbind(no_password_user):
    """登录保底未被放开：真实身份解绑路径仍按 keep-one 拒绝最后一种方式。"""
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, delete_external_identity)

    uid = no_password_user["id"]
    assert create_external_identity(uid, "gitcode", f"ext{uuid.uuid4().hex[:6]}",
                                    access_token="oauth-only")
    assert delete_external_identity(uid, "gitcode") is False, \
        "无密码且仅一条 OAuth 绑定时解绑必须被 keep-one 拒绝"