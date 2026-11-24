"""Issue #3396 改动④：PUT /runtimes/{id}/tags 写权限收紧（A5 权限矩阵）。

矩阵（用户裁定）：
- 引擎凭据所有者本人（owner_name 匹配）→ 200
- 凭据归属组织 owner/admin → 200
- 平台管理员（admin_accounts 身份）→ 200（含本机直连 credential_id=0）
- 其他登录用户 / 仓库语境管理员（与机器无归属关系）→ 403
- 本机直连（credential_id=0）非平台管理员 → 403
- session_token（tk_）→ 403
- 读接口 GET /runtimes/{id}/role-tags 保持全员可读 → 200
"""
import hashlib
import json
import secrets

import pytest

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import _resolve_user_or_platform_admin
from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM agent_runtimes WHERE credential_id IN "
            "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396w-%')")
        c.execute(
            "DELETE FROM engine_capabilities WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE credential_id IN "
            "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396w-%'))")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'b3396w-%'")
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=0 AND lower(name)='b3396w-local'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes)")
        c.execute("DELETE FROM admin_accounts WHERE username='b3396w-admin'")
        c.execute("DELETE FROM audit_logs WHERE event_type='engine_tag_change'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM agent_runtimes WHERE credential_id IN "
            "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396w-%')")
        c.execute(
            "DELETE FROM engine_capabilities WHERE runtime_id IN "
            "(SELECT id FROM agent_runtimes WHERE credential_id IN "
            "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396w-%'))")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'b3396w-%'")
        c.execute("DELETE FROM agent_runtimes WHERE credential_id=0 AND lower(name)='b3396w-local'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes)")
        c.execute("DELETE FROM admin_accounts WHERE username='b3396w-admin'")
        c.execute("DELETE FROM audit_logs WHERE event_type='engine_tag_change'")
        db.commit()
    app.dependency_overrides.pop(_resolve_user_or_platform_admin, None)


def _mk_engine(owner_name="owner_alice", scope="user", owner_id="44", org_id="",
               credential=False, cap="all"):
    """建引擎行；credential=True 时建真实凭据行（带 owner_name/org_id）。"""
    with db_lock:
        c = db.cursor()
        cid = 0
        if credential:
            token = "rta_" + secrets.token_urlsafe(16)
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status, org_id) "
                "VALUES (?,?,?,?,?,?, 'active', ?)",
                (hashlib.sha256(token.encode()).hexdigest(), "b3396w-cred-" + secrets.token_hex(4),
                 "dev-b3396w", scope, owner_id, owner_name, org_id))
            cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, scope, owner_id, org_id, agent_tags) "
            "VALUES (?,?,?, 'online', 1, 1, ?, ?, ?, ?)",
            ("b3396w-local" if not credential else "opencode", "b3396w", cid, scope, owner_id, org_id,
             '["chat"]' if cap == "chat" else '["all"]'))
        rid = c.lastrowid
        if cap:
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, cap))
        db.commit()
        return rid


def _client_as(user: dict):
    app.dependency_overrides[_resolve_user_or_platform_admin] = lambda: user
    return TestClient(app)


def _owner_user():
    return {"id": 4001, "username": "owner_alice", "auth_type": "jwt", "is_platform_admin": False}


def _other_user():
    return {"id": 4002, "username": "mallory", "auth_type": "jwt", "is_platform_admin": False}


def _admin_user():
    return {"id": 4003, "username": "b3396w-admin", "auth_type": "jwt", "is_platform_admin": True}


def _put_tags(client, rid, tags=("chat",)):
    return client.put(f"/api/runtime/runtimes/{rid}/tags", json={"tags": list(tags)})


class TestPutTagsPermissionMatrix:
    def test_owner_can_write(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _put_tags(_client_as(_owner_user()), rid)
        assert r.status_code == 200, r.text
        assert r.json()["tags"] == ["chat"]

    def test_other_user_forbidden(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _put_tags(_client_as(_other_user()), rid)
        assert r.status_code == 403, r.text

    def test_platform_admin_can_write(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _put_tags(_client_as(_admin_user()), rid)
        assert r.status_code == 200, r.text

    def test_org_admin_can_write(self):
        # 引擎凭据归属 org，用户 4002 是 org admin
        from src.domains.mgmt.user.org_db import create_org, add_org_member
        org = create_org("b3396w-org-" + secrets.token_hex(3), "b3396w-org-" + secrets.token_hex(3),
                         "4001", description="")
        assert org and org.get("id"), f"create_org failed: {org}"
        # D105 第三批：org.id 为 uuid str（TEXT 主键），不再 int 归一
        org_id = str(org["id"])
        add_org_member(org_id, 4002, role="admin")
        rid = _mk_engine(owner_name="owner_alice", credential=True, org_id=org_id)
        r = _put_tags(_client_as(_other_user()), rid)
        assert r.status_code == 200, r.text

    def test_local_direct_non_admin_forbidden(self):
        rid = _mk_engine(credential=False)
        r = _put_tags(_client_as(_owner_user()), rid)
        assert r.status_code == 403, r.text

    def test_local_direct_platform_admin_allowed(self):
        rid = _mk_engine(credential=False)
        r = _put_tags(_client_as(_admin_user()), rid)
        assert r.status_code == 200, r.text

    def test_session_token_forbidden(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _put_tags(_client_as({"id": 4001, "username": "",
                                  "auth_type": "session_token", "is_platform_admin": False}), rid)
        assert r.status_code == 403, r.text

    def test_missing_runtime_404(self):
        r = _put_tags(_client_as(_owner_user()), 999999999)
        assert r.status_code == 404, r.text

    def test_role_tags_read_stays_public(self):
        """读接口保持全员可读（A5：仅收紧写权限）。"""
        from src.api.deps import get_current_user_or_api_key
        rid = _mk_engine(owner_name="owner_alice", credential=True, cap="chat")
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {"id": 4002, "username": "mallory"}
        try:
            client = TestClient(app)
            r = client.get(f"/api/runtime/runtimes/{rid}/role-tags")
            assert r.status_code == 200, r.text
            assert "chat" in (r.json().get("tags") or [])
        finally:
            app.dependency_overrides.pop(get_current_user_or_api_key, None)


class TestAdminJwtFastPath:
    def test_platform_admin_jwt_bypasses_grape_account_lookup(self):
        """平台管理员持 admin JWT（admin_accounts 独立身份）→ 直接放行（不经 grape_accounts 校验）。"""
        from src.api.jwt_utils import create_access_token
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM admin_accounts WHERE username='b3396w-admin'")
            c.execute("INSERT INTO admin_accounts (username, password_hash, is_super, token_version) "
                      "VALUES ('b3396w-admin', 'x', 1, 7)")
            admin_id = c.execute("SELECT id FROM admin_accounts WHERE username='b3396w-admin'").fetchone()[0]
            db.commit()
        token = create_access_token(admin_id, "b3396w-admin", token_version=7, role="admin")
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        client = TestClient(app)  # 无 dependency override，走真实鉴权链
        r = client.put(f"/api/runtime/runtimes/{rid}/tags",
                       headers={"Authorization": f"Bearer {token}"}, json={"tags": ["chat"]})
        assert r.status_code == 200, r.text

    def test_stale_admin_token_version_rejected(self):
        """admin token_version 与 admin_accounts 不一致 → 不走管理员通道，回落用户通道 → 401。"""
        from src.api.jwt_utils import create_access_token
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM admin_accounts WHERE username='b3396w-admin'")
            c.execute("INSERT INTO admin_accounts (username, password_hash, is_super, token_version) "
                      "VALUES ('b3396w-admin', 'x', 1, 9)")
            admin_id = c.execute("SELECT id FROM admin_accounts WHERE username='b3396w-admin'").fetchone()[0]
            db.commit()
        token = create_access_token(admin_id, "b3396w-admin", token_version=7, role="admin")
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        client = TestClient(app)
        r = client.put(f"/api/runtime/runtimes/{rid}/tags",
                       headers={"Authorization": f"Bearer {token}"}, json={"tags": ["chat"]})
        assert r.status_code in (401, 403), r.text


# ── F1/F2（架构守护复审）：settings 三个同源写端点同权限矩阵 + 标签写入审计 ──────────


def _settings_tags(client, method, rid, tag=None):
    if method == "post":
        return client.post(f"/api/settings/agent-runtimes/{rid}/tags",
                           json={"tag": tag, "priority": 50})
    if method == "put":
        return client.put(f"/api/settings/agent-runtimes/{rid}/tags/{tag}",
                          json={"priority": 50, "enabled": 1})
    return client.delete(f"/api/settings/agent-runtimes/{rid}/tags/{tag}")


def _engine_tags(rid):
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT tag, enabled FROM engine_capabilities WHERE runtime_id=? ORDER BY tag",
            (rid,)).fetchall()
    return [(r[0], int(r[1] or 0)) for r in rows]


def _tag_audits(rid):
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT detail FROM audit_logs WHERE event_type='engine_tag_change' "
            "ORDER BY seq DESC LIMIT 50").fetchall()
    out = []
    for (detail,) in rows:
        try:
            d = json.loads(detail or "{}")
        except Exception:
            continue
        if int(d.get("runtime_id") or 0) == int(rid):
            out.append(d)
    return out


class TestSettingsTagWritePermissionMatrix:
    """F1（A9）：settings 三个写 engine_capabilities 的端点必须同 runtime 端点一套权限矩阵。"""

    def test_owner_can_add_via_settings(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _settings_tags(_client_as(_owner_user()), "post", rid, tag="chat")
        assert r.status_code == 200, r.text
        assert ("chat", 1) in _engine_tags(rid)
        audits = _tag_audits(rid)
        assert any(a.get("action") == "add" and a.get("tag") == "chat"
                   and a.get("operator") == "owner_alice" for a in audits), audits

    def test_other_user_forbidden_on_settings_writes(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        client = _client_as(_other_user())
        before = _engine_tags(rid)
        assert _settings_tags(client, "post", rid, tag="chat").status_code == 403
        assert _settings_tags(client, "put", rid, tag="all").status_code == 403
        assert _settings_tags(client, "delete", rid, tag="all").status_code == 403
        assert _engine_tags(rid) == before
        assert _tag_audits(rid) == []

    def test_platform_admin_can_write_via_settings(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _settings_tags(_client_as(_admin_user()), "post", rid, tag="chat")
        assert r.status_code == 200, r.text

    def test_session_token_forbidden_on_settings(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        user = {"id": 4001, "username": "", "auth_type": "session_token",
                "is_platform_admin": False}
        assert _settings_tags(_client_as(user), "post", rid, tag="chat").status_code == 403

    def test_local_direct_non_admin_forbidden_on_settings(self):
        rid = _mk_engine(credential=False)
        assert _settings_tags(_client_as(_owner_user()), "post", rid, tag="chat").status_code == 403

    def test_owner_update_and_delete_via_settings_audited(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        client = _client_as(_owner_user())
        assert _settings_tags(client, "put", rid, tag="all").status_code == 200
        assert _settings_tags(client, "delete", rid, tag="all").status_code == 200
        actions = [a.get("action") for a in _tag_audits(rid)]
        assert "update" in actions and "delete" in actions, actions

    def test_missing_runtime_404_on_settings(self):
        assert _settings_tags(_client_as(_owner_user()), "post", 999999999,
                              tag="chat").status_code == 404


class TestRuntimeSetTagsAudited:
    """F2（C3）：runtime PUT tags 写入口必须有审计留痕。"""

    def test_set_tags_writes_audit(self):
        rid = _mk_engine(owner_name="owner_alice", credential=True)
        r = _put_tags(_client_as(_owner_user()), rid, tags=("chat",))
        assert r.status_code == 200, r.text
        audits = _tag_audits(rid)
        assert any(a.get("action") == "set" and a.get("tags") == ["chat"]
                   and a.get("source") == "runtime_tags_set" for a in audits), audits
