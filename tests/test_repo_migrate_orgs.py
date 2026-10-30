"""Issue #543：迁移目标组织下拉框 — 组织列表端点与 GitCode V5 封装测试。

覆盖：
- fetch_user_orgs：GitCode V5 /user/orgs 返回结构、分页、失败抛错；
- GET /api/repos/migrate-orgs：未鉴权 401、鉴权后返回 orgs 数组、上游失败 502。
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


def _ensure_user(username="test_migrate_orgs", gc_token="gc_tok_543"):
    """创建测试用户并绑定 GitCode token（external_identities）。"""
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
        c.execute(
            "INSERT OR REPLACE INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, access_token) "
            "VALUES (?, 'gitcode', ?, ?, ?)",
            (uid, f"{username}_gc", username, gc_token))
        db.commit()
    return uid, username


# ---------------------------------------------------------------------------
# 1. fetch_user_orgs — GitCode V5 封装
# ---------------------------------------------------------------------------

def test_fetch_user_orgs_returns_list(monkeypatch):
    """正常返回：解析 login/name/avatar_url。"""
    from src.user import repo_manager as rm

    fake = [
        {"login": "orgA", "name": "Org Alpha", "avatar_url": "https://x/a.png"},
        {"login": "orgB", "name": "Org Beta", "avatar_url": ""},
    ]

    def _fake_req(method, url, token="", body=None):
        assert url.startswith(rm.API_BASE + "/user/orgs")
        return fake

    monkeypatch.setattr(rm, "_req", _fake_req)
    orgs = rm.fetch_user_orgs("tok")
    assert len(orgs) == 2
    assert orgs[0]["login"] == "orgA"
    assert orgs[0]["name"] == "Org Alpha"
    assert orgs[1]["avatar_url"] == ""


def test_fetch_user_orgs_dict_wrapper(monkeypatch):
    """GitCode 偶尔返回 {"data": [...]} 包装 → 也能解析。"""
    from src.user import repo_manager as rm

    def _fake_req(method, url, token="", body=None):
        return {"data": [{"login": "wrappedOrg", "username": "wrappedOrg"}]}

    monkeypatch.setattr(rm, "_req", _fake_req)
    orgs = rm.fetch_user_orgs("tok")
    assert len(orgs) == 1
    assert orgs[0]["login"] == "wrappedOrg"


def test_fetch_user_orgs_pagination(monkeypatch):
    """分页：第一页满 50 条 → 继续第二页；第二页不足 → 停止。"""
    from src.user import repo_manager as rm

    pages = {
        1: [{"login": f"org{i}"} for i in range(50)],
        2: [{"login": "org50"}],
    }

    def _fake_req(method, url, token="", body=None):
        page = 1
        if "page=2" in url:
            page = 2
        return pages[page]

    monkeypatch.setattr(rm, "_req", _fake_req)
    orgs = rm.fetch_user_orgs("tok")
    assert len(orgs) == 51


def test_fetch_user_orgs_empty_token():
    """空 token → 抛 RuntimeError（不调网络）。"""
    from src.user.repo_manager import fetch_user_orgs
    with pytest.raises(RuntimeError):
        fetch_user_orgs("")


def test_fetch_user_orgs_failure(monkeypatch):
    """上游 401/500 → RuntimeError 透传。"""
    from src.user import repo_manager as rm

    def _boom(method, url, token="", body=None):
        raise RuntimeError("GitCode API GET failed (401): unauthorized")

    monkeypatch.setattr(rm, "_req", _boom)
    with pytest.raises(RuntimeError):
        rm.fetch_user_orgs("tok")


# ---------------------------------------------------------------------------
# 2. GET /api/repos/migrate-orgs — 端点鉴权与返回结构
# ---------------------------------------------------------------------------

def test_migrate_orgs_requires_auth():
    """未带 JWT → 401。"""
    r = client.get("/api/repos/migrate-orgs")
    assert r.status_code == 401


def test_migrate_orgs_returns_structure(monkeypatch):
    """鉴权后返回 {"orgs": [...], "personal": "..."}。"""
    from src.user import repo_manager as rm

    uid, username = _ensure_user()

    def _fake_req(method, url, token="", body=None):
        if url.endswith("/user") or url == rm.API_BASE + "/user":
            return {"login": username}
        if "/user/orgs" in url:
            return [{"login": "targetOrg", "name": "Target Org", "avatar_url": ""}]
        return {}

    monkeypatch.setattr(rm, "_req", _fake_req)
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "orgs" in body and isinstance(body["orgs"], list)
    assert len(body["orgs"]) == 1
    assert body["orgs"][0]["login"] == "targetOrg"
    assert body["personal"] == username


def test_migrate_orgs_upstream_failure_502(monkeypatch):
    """GitCode 上游故障 → 502 + 友好提示。"""
    from src.user import repo_manager as rm

    uid, username = _ensure_user()

    def _boom(method, url, token="", body=None):
        raise RuntimeError("GitCode API GET failed (500): server error")

    monkeypatch.setattr(rm, "_req", _boom)
    token = _jwt(uid, username)
    r = client.get("/api/repos/migrate-orgs",
                   headers={"Authorization": f"Bearer {token}"})
    # mock 用户 token 有效：with_oauth_token 无 401 刷新重试路径，上游 500 应稳定映射 502
    assert r.status_code == 502, r.text
    detail = r.json().get("detail") or r.json()
    assert detail.get("code") == 502
