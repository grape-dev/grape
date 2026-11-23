"""Issue #3855 官方技能改名「真实鉴权链路」回归（不 monkeypatch 鉴权判定）。

覆盖：
- 普通用户 PATCH 官方技能 → 403（display_name 不被写入）
- 同名普通用户（username 与管理员相同，sub 为 grape_accounts id）→ 403
- 真实管理员（JWT role=admin，sub=admin_accounts id）→ 200 且写入
- 详情 can_edit_display_name：管理员 True、普通用户 False

整条链路走 TestClient → 真实 JWT（create_access_token）→
get_current_user_or_api_key_or_admin → admin_accounts id 复核，
不使用 dependency_overrides 或 monkeypatch 伪造身份。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.mgmt.admin.db import get_admin_token_version
from src.domains.mgmt.auth import grape_account_db
from src.kernel.auth_sdk.jwt_utils import create_access_token
from src.kernel.session_manager import db, db_lock

_client = TestClient(app)


def _mk_admin():
    name = f"i3855adm{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO admin_accounts (username, password_hash, is_super, token_version) "
            "VALUES (?, '', 1, 0)", (name,))
        db.commit()
        aid = c.lastrowid
    tok = create_access_token(aid, name, get_admin_token_version(aid), role="admin")
    return aid, name, tok


def _mk_user(username=None):
    username = username or f"i3855usr{uuid.uuid4().hex[:8]}"
    acc = grape_account_db.create_grape_account(username, email=None, password="pw")
    if not acc:
        acc = grape_account_db.get_grape_account_by_username(username)
    uid = acc["id"]
    tok = create_access_token(uid, username, grape_account_db.get_token_version(uid))
    return uid, username, tok


def _mk_official_skill(display_name="旧官方名"):
    name = f"i3855-official-{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, scope, owner_id, is_active) VALUES (?,?,?,?,?,'official','',1)",
            (name, display_name, "官方技能", "official", name))
        db.commit()
        sid = c.lastrowid
    return sid, name


def _display(sid):
    with db_lock:
        row = db.cursor().execute(
            "SELECT display_name FROM skill WHERE id=?", (sid,)).fetchone()
    return row[0] if row else None


def _cleanup(sid, admin_id, uids):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE id=?", (sid,))
        if admin_id:
            c.execute("DELETE FROM admin_accounts WHERE id=?", (admin_id,))
        for u in uids:
            c.execute("DELETE FROM grape_accounts WHERE id=?", (u,))
        db.commit()


def _patch(sid, body, token):
    return _client.patch(f"/api/skills/{sid}", json=body,
                         headers={"Authorization": f"Bearer {token}"})


def test_normal_user_cannot_rename_official_skill():
    admin_id, _admin_name, _admin_tok = _mk_admin()
    uid, _uname, utok = _mk_user()
    sid, _name = _mk_official_skill()
    try:
        r = _patch(sid, {"display_name": "普通用户新名"}, utok)
        assert r.status_code == 403, r.text
        assert _display(sid) == "旧官方名"
    finally:
        _cleanup(sid, admin_id, [uid])


def test_same_username_user_cannot_rename_official_skill():
    # grape_accounts 与 admin_accounts 独立：同名普通账号不得凭 username 取得改名权。
    admin_id, admin_name, _admin_tok = _mk_admin()
    uid, uname, utok = _mk_user(username=admin_name)
    assert uname == admin_name
    sid, _name = _mk_official_skill()
    try:
        r = _patch(sid, {"display_name": "同名用户新名"}, utok)
        assert r.status_code == 403, r.text
        assert _display(sid) == "旧官方名"
    finally:
        _cleanup(sid, admin_id, [uid])


def test_real_admin_can_rename_official_skill():
    admin_id, _admin_name, admin_tok = _mk_admin()
    sid, _name = _mk_official_skill()
    try:
        r = _patch(sid, {"display_name": "管理员新名"}, admin_tok)
        assert r.status_code == 200, r.text
        assert _display(sid) == "管理员新名"
        assert r.json().get("display_name") == "管理员新名"
    finally:
        _cleanup(sid, admin_id, [])


def test_detail_can_edit_display_name_by_identity():
    admin_id, _admin_name, admin_tok = _mk_admin()
    uid, _uname, utok = _mk_user()
    sid, _name = _mk_official_skill()
    try:
        r_admin = _client.get(f"/api/skills/by-id/{sid}",
                              headers={"Authorization": f"Bearer {admin_tok}"})
        assert r_admin.status_code == 200, r_admin.text
        assert r_admin.json().get("can_edit_display_name") is True

        r_user = _client.get(f"/api/skills/by-id/{sid}",
                             headers={"Authorization": f"Bearer {utok}"})
        assert r_user.status_code == 200, r_user.text
        assert r_user.json().get("can_edit_display_name") is False
    finally:
        _cleanup(sid, admin_id, [uid])