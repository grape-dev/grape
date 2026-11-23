"""Issue #2423 M2b：管理端 OV 凭据格子（user_id×org，vault 入库不落明文文件）。

Seam（公开接口）：
- org 管理员 PUT/GET/DELETE /api/orgs/{org_id}/ov-credentials/{user_id}；
- 明文只落 grape_t_plugin_credential_vault（全系统唯一持明文处），格子行只存 vault_ref；
- 吊销 = 格子 + vault 行同删（无副本缓存 → 不复活）；
- resolve_cell(user_id) 供 exec 端点 tk_ 兑换（两用户身份隔离）。
"""
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.main import app
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db
from src.api.jwt_utils import create_access_token

client = TestClient(app)

_marker = uuid.uuid4().hex[:8]
_admin, _member, _outsider = {}, {}, {}
_org_id = 0


def _mk_user(prefix: str):
    return grape_account_db.create_grape_account(f"{prefix}_{_marker}",
                                                 email=None, password="pw")


def _headers(user: dict):
    return {"Authorization": "Bearer " + create_access_token(
        user["id"], user["username"], user.get("token_version") or 0)}


def _create_org(owner_id, name: str):
    """arch11 D105 适配：organization.id 为应用层 UUIDv7（裸插 lastrowid=NULL）——
    显式生成 id 后返回；org_id 保持调用方 int/str 兼容。"""
    from src.kernel.id_gen import uuid7_str
    with db_lock:
        c = db.cursor()
        _oid = uuid7_str()
        c.execute(
            "INSERT INTO organization (id, name, slug, owner_id, is_personal, description) "
            "VALUES (?,?,?,?,0,'issue2423 test')",
            (_oid, name, f"{name}-{uuid.uuid4().hex[:6]}", str(owner_id)))
        c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?,?, 'owner')",
                  (_oid, str(owner_id)))
        db.commit()
    return _oid


def _add_org_member(org_id, user_id, role: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO org_member (org_id, user_id, role) VALUES (?,?,?)",
            (org_id, str(user_id), role))
        db.commit()


def _cell_row(user_id: int, org_id: int):
    with db_lock:
        return db.cursor().execute(
            "SELECT id, base_url, vault_ref FROM grape_t_ov_credential_cells "
            "WHERE user_id=? AND org_id=?", (user_id, org_id)).fetchone()


def _vault_row_by_ref(ref: str):
    with db_lock:
        return db.cursor().execute(
            "SELECT secret, plugin_id, name, owner_id FROM grape_t_plugin_credential_vault "
            "WHERE ref=?", (ref,)).fetchone()


def _cleanup():
    uids = [u["id"] for u in (_admin, _member, _outsider) if u]
    if not uids:
        return
    uid_marks = ",".join("?" * len(uids))
    with db_lock:
        c = db.cursor()
        c.execute(f"DELETE FROM grape_t_ov_credential_cells WHERE user_id IN ({uid_marks})",
                  tuple(uids))
        c.execute("DELETE FROM grape_t_ov_credential_cells WHERE org_id=?", (_org_id,))
        c.execute(f"DELETE FROM grape_t_ov_credential_cell_revocations WHERE user_id IN ({uid_marks})",
                  tuple(uids))
        c.execute("DELETE FROM grape_t_ov_credential_cell_revocations WHERE org_id=?", (_org_id,))
        c.execute(f"DELETE FROM grape_t_plugin_credential_vault WHERE owner_id IN ({uid_marks})",
                  tuple(str(u) for u in uids))
        c.execute("DELETE FROM org_member WHERE org_id=?", (_org_id,))
        c.execute("DELETE FROM organization WHERE id=?", (_org_id,))
        for u in uids:
            c.execute("DELETE FROM grape_accounts WHERE id=?", (u,))
        db.commit()


def setup_module():
    global _admin, _member, _outsider, _org_id
    _admin = _mk_user("celladm")
    _member = _mk_user("cellmem")
    _outsider = _mk_user("cellout")
    _org_id = _create_org(_admin["id"], "ov-cell-org")
    _add_org_member(_org_id, _member["id"], "member")


def teardown_module():
    _cleanup()


# ---------- 模块级单元：格子存取/吊销/隔离 ----------

class TestCredentialCellUnit:
    def test_put_and_resolve_roundtrip(self):
        from src.domains.mgmt.auth.credential_cell import put_cell, resolve_cell, revoke_cell
        put_cell(_admin["id"], _member["id"], _org_id, "ov-key-member-1",
                 base_url="http://ov.test")
        cell = resolve_cell(_member["id"])
        assert cell and cell["secret"] == "ov-key-member-1"
        assert cell["base_url"] == "http://ov.test"
        # 明文只落 vault；格子行无明文
        row = _cell_row(_member["id"], _org_id)
        assert row and "ov-key-member-1" not in str(row)
        vrow = _vault_row_by_ref(row[2])
        assert vrow and vrow[0] == "ov-key-member-1"
        revoke_cell(_member["id"], _org_id)

    def test_revoke_deletes_vault_and_cell_no_resurrect(self):
        from src.domains.mgmt.auth.credential_cell import put_cell, resolve_cell, revoke_cell
        put_cell(_admin["id"], _member["id"], _org_id, "ov-key-tmp")
        row = _cell_row(_member["id"], _org_id)
        ref = row[2]
        assert revoke_cell(_member["id"], _org_id) is True
        assert _cell_row(_member["id"], _org_id) is None
        assert _vault_row_by_ref(ref) is None          # 吊销不复活（vault 同删）
        assert resolve_cell(_member["id"]) is None

    def test_two_users_identity_isolation(self):
        """两用户各自格子 → resolve 只回本人凭据（OV 落账身份隔离）。"""
        from src.domains.mgmt.auth.credential_cell import put_cell, resolve_cell, revoke_cell
        put_cell(_admin["id"], _admin["id"], _org_id, "ov-key-admin")
        put_cell(_admin["id"], _member["id"], _org_id, "ov-key-member-2")
        assert resolve_cell(_admin["id"])["secret"] == "ov-key-admin"
        assert resolve_cell(_member["id"])["secret"] == "ov-key-member-2"
        revoke_cell(_admin["id"], _org_id)
        revoke_cell(_member["id"], _org_id)

    def test_registration_invariants(self):
        """新表双注册：_NEW_TABLES（全新库）+ kind=safe 迁移（存量库启动补建）。"""
        from src.api import db_setup
        names = [n for n, _ in db_setup._NEW_TABLES]
        assert "grape_t_ov_credential_cells" in names
        migs = {m.version: m for m in db_setup._MIGRATIONS}
        # 撞号让位：原 202611180002 改号 202611220004（runtime 域 ensure-runtime-type-info-columns 占用 002）
        assert "202611220004" in migs
        assert migs["202611220004"].kind == "safe"
        assert migs["202611220004"].name == "issue2423-create-ov-credential-cells"
        assert "create table if not exists" in dict(db_setup._NEW_TABLES)["grape_t_ov_credential_cells"].lower()


# ---------- 管理端 API（org 路径型管理员鉴权） ----------

def _put(uid, key, headers=None, base_url="http://ov.test"):
    return client.put(f"/api/orgs/{_org_id}/ov-credentials/{uid}",
                      json={"ov_api_key": key, "base_url": base_url},
                      headers=headers or _headers(_admin))


class TestOVCredentialCellRoutes:
    def test_admin_put_for_member_and_self(self):
        r = _put(_member["id"], "ov-key-route-member")
        assert r.status_code == 200, r.text
        r2 = _put(_admin["id"], "ov-key-route-admin")       # 管理员给本人配置
        assert r2.status_code == 200, r.text

    def test_list_masked_no_plaintext(self):
        _put(_member["id"], "ov-key-mask-1")
        r = client.get(f"/api/orgs/{_org_id}/ov-credentials",
                       headers=_headers(_admin))
        assert r.status_code == 200, r.text
        entries = r.json()
        assert isinstance(entries, list) and entries
        text = r.text
        assert "ov-key-mask-1" not in text
        item = next(e for e in entries if e.get("user_id") == _member["id"])
        assert item.get("base_url") == "http://ov.test"
        assert item.get("has_secret") is True

    def test_delete_revokes(self):
        _put(_member["id"], "ov-key-del-1")
        r = client.delete(f"/api/orgs/{_org_id}/ov-credentials/{_member['id']}",
                          headers=_headers(_admin))
        assert r.status_code == 200, r.text
        assert _cell_row(_member["id"], _org_id) is None

    def test_delete_missing_404(self):
        r = client.delete(f"/api/orgs/{_org_id}/ov-credentials/{_member['id']}",
                          headers=_headers(_admin))
        assert r.status_code == 404

    def test_non_admin_member_403(self):
        r = client.put(f"/api/orgs/{_org_id}/ov-credentials/{_member['id']}",
                       json={"ov_api_key": "x"},
                       headers=_headers(_member))
        assert r.status_code == 403

    def test_outsider_403(self):
        r = client.put(f"/api/orgs/{_org_id}/ov-credentials/{_member['id']}",
                       json={"ov_api_key": "x"},
                       headers=_headers(_outsider))
        assert r.status_code == 403

    def test_target_not_member_404(self):
        r = _put(_outsider["id"], "ov-key-x")   # 目标用户不是本组织成员
        assert r.status_code == 404

    def test_empty_key_rejected_422(self):
        r = client.put(f"/api/orgs/{_org_id}/ov-credentials/{_member['id']}",
                       json={"ov_api_key": ""},
                       headers=_headers(_admin))
        assert r.status_code == 422
