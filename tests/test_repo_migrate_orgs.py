"""Issue #543：迁移目标组织下拉框 — 组织列表端点测试。

覆盖：
- GET /api/repos/migrate-orgs：未鉴权 401、鉴权后返回 orgs 数组（来自 Grape organization 表）、上游失败 502。
- 数据源从 Grape 平台 organization + org_member 表查询（不再调 GitCode API）。
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
    # 个人组织应在列表中（注册时自动创建）
    if body["orgs"]:
        assert "login" in body["orgs"][0]
        assert "name" in body["orgs"][0]


def test_migrate_orgs_personal_org_included():
    """用户有个人组织时，应出现在返回列表中。"""
    uid, username = _ensure_user()
    # 手动创建个人组织 + org_member 记录（模拟注册时自动创建）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO organization (name, slug, owner_id, is_personal) "
                "VALUES (?, ?, ?, 1)",
                (f"{username}'s Workspace", f"{username}-personal", uid))
            org_id = c.lastrowid
            c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                      (org_id, uid))
            db.commit()
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    personal_orgs = [o for o in body["orgs"] if o.get("is_personal")]
    assert len(personal_orgs) >= 1, f"Expected at least 1 personal org, got {body['orgs']}"


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
