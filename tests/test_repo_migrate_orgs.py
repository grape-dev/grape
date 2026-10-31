"""Issue #543 / #593：迁移目标组织下拉框 — 组织列表端点测试。

覆盖：
- GET /api/repos/migrate-orgs：未鉴权 401、鉴权后返回 orgs 数组（来自 Grape organization 表）、上游失败 502。
- 数据源从 Grape 平台 organization + org_member 表查询（不再调 GitCode API）。
- Issue #593：orgs 列表过滤 is_personal 组织（个人组织已由 personal 字段独立返回，避免下拉重复）。
"""
import os
import sys

import pytest

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.auth.password import hash_password
from src.api.jwt_utils import create_access_token

client = TestClient(app)


def _jwt(user_id, username):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    ver = row[0] if row else 0
    return create_access_token(user_id, username, ver)


def _ensure_user(username="test_migrate_orgs"):
    """创建测试用户。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username=?", (username,))
        row = c.fetchone()
        if row:
            uid = row[0]
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                (username, None, hash_password("t123"), username))
            db.commit()
            uid = c.lastrowid
    return uid, username


# ---------------------------------------------------------------------------
# 1. GET /api/repos/migrate-orgs — 端点鉴权与返回结构
# ---------------------------------------------------------------------------

def test_migrate_orgs_requires_auth():
    """未带 JWT → 401。"""
    r = client.get("/api/repos/migrate-orgs")
    assert r.status_code == 401


def test_migrate_orgs_returns_structure():
    """鉴权后返回 {"orgs": [...], "personal": "..."}，数据来自 Grape organization 表。"""
    uid, username = _ensure_user()
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "orgs" in body and isinstance(body["orgs"], list)
    assert "personal" in body
    assert body["personal"] == username
    # Issue #593：orgs 列表不应含 is_personal=true 条目（个人组织由 personal 字段独立返回）
    personal_in_orgs = [o for o in body["orgs"] if o.get("is_personal")]
    assert not personal_in_orgs, (
        f"orgs should not contain is_personal entries, got {personal_in_orgs}")
    # 若存在团队组织，字段结构应完整
    if body["orgs"]:
        assert "login" in body["orgs"][0]
        assert "name" in body["orgs"][0]


def _ensure_personal_org(uid, username):
    """确保用户有个人组织 + org_member 记录（模拟注册时自动创建）。返回 org_id。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,))
        row = c.fetchone()
        if row:
            return row[0]
        c.execute(
            "INSERT INTO organization (name, slug, owner_id, is_personal) "
            "VALUES (?, ?, ?, 1)",
            (f"{username}'s Workspace", f"{username}-personal", uid))
        org_id = c.lastrowid
        c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                  (org_id, uid))
        db.commit()
    return org_id


def _ensure_team_org(uid, name="Migrate Team Org", slug=None):
    """确保用户有一个团队组织（is_personal=0）+ org_member 记录。返回 org_id。"""
    slug = slug or f"team-{uid}-{abs(hash(name)) % 100000}"
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM organization WHERE slug=?", (slug,))
        row = c.fetchone()
        if row:
            org_id = row[0]
            c.execute("SELECT 1 FROM org_member WHERE org_id=? AND user_id=?",
                      (org_id, uid))
            if not c.fetchone():
                c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                          (org_id, uid))
                db.commit()
            return org_id
        c.execute(
            "INSERT INTO organization (name, slug, owner_id, is_personal) "
            "VALUES (?, ?, ?, 0)",
            (name, slug, uid))
        org_id = c.lastrowid
        c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                  (org_id, uid))
        db.commit()
    return org_id


def test_migrate_orgs_personal_org_excluded_from_orgs():
    """Issue #593：用户有个人组织时，orgs 列表应过滤掉 is_personal 条目（personal 字段已覆盖）。"""
    uid, username = _ensure_user()
    _ensure_personal_org(uid, username)
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    personal_orgs = [o for o in body["orgs"] if o.get("is_personal")]
    assert not personal_orgs, (
        f"orgs should NOT contain is_personal entries (covered by personal field), "
        f"got {personal_orgs}")


def test_migrate_orgs_personal_field_still_returned():
    """Issue #593：orgs 去重后 personal 字段仍应返回当前用户登录名。"""
    uid, username = _ensure_user()
    _ensure_personal_org(uid, username)
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["personal"] == username, (
        f"personal field should still be the username, got {body.get('personal')!r}")


def test_migrate_orgs_team_orgs_still_listed():
    """Issue #593：同时存在个人组织 + 团队组织时，orgs 仅含团队组织。"""
    uid, username = _ensure_user()
    _ensure_personal_org(uid, username)
    team_org_id = _ensure_team_org(uid, name="Issue593 Team", slug=f"issue593-team-{uid}")
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    # 团队组织应在 orgs 中
    team_orgs = [o for o in body["orgs"] if not o.get("is_personal")]
    assert any(o.get("login") == f"issue593-team-{uid}" for o in team_orgs), (
        f"team org should be listed in orgs, got {body['orgs']}")
    # 个人组织不应在 orgs 中
    personal_orgs = [o for o in body["orgs"] if o.get("is_personal")]
    assert not personal_orgs, (
        f"personal org should be filtered out, got {personal_orgs}")


def test_migrate_orgs_upstream_failure_502(monkeypatch):
    """DB 查询故障 → 502 + 友好提示。"""
    uid, username = _ensure_user()
    token = _jwt(uid, username)

    # Patch session_manager.db_lock to raise
    from src import session_manager as sm

    class _BoomLock:
        def __enter__(self):
            raise RuntimeError("DB connection lost")
        def __exit__(self, *a):
            pass

    original = sm.db_lock
    sm.db_lock = _BoomLock()
    try:
        r = client.get("/api/repos/migrate-orgs",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 502, r.text
        detail = r.json().get("detail") or r.json()
        assert detail.get("code") == 502
    finally:
        sm.db_lock = original
