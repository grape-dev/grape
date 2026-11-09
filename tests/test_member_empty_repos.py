"""Issue #538: 绑定正常、拉取成功、账号下无仓库的合法空结果被误判为失败抛 502。

复现前置：
1. 用户已绑定 gitcode（token 有效）
2. with_oauth_token 拉取成功但返回 []（账号下确无任何仓库）

修复后断言：
- GET /api/repos/member 返回 200 + {"repos": [], "_cached": false}，不抛 502
- POST /api/repos/sync 返回 200 + {"repos": [], "message": "同步成功"}，不抛 502
- save_user_repos 被调用且写入空列表（空仓库用户不再每次穿透缓存直连 VCS）

失败分支回归：
- 拉取异常（failed_providers 非空）且无缓存 → 仍 502

运行：python3 -m pytest tests/test_member_empty_repos.py -q
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

UID = 1
USER = {"id": UID, "username": "u_empty", "auth_type": "jwt", "org_id": 0}

_saved_repos = {}


@pytest.fixture()
def client():
    from src.api.main import app
    from src.api.deps import get_current_user, get_current_user_or_api_key
    from src.user import db as user_db
    from src.auth import grape_account_db as acct_db

    def _fake_identities(uid):
        return [{"provider": "gitcode"}]

    def _fake_get_user_repos(uid):
        return []  # 无旧缓存

    def _fake_get_synced_at(uid):
        return None

    def _fake_save(uid, repos):
        _saved_repos["repos"] = list(repos)
        return True

    orig = (user_db.get_user_repos, user_db.get_user_repos_synced_at, user_db.save_user_repos)
    user_db.get_user_repos = _fake_get_user_repos
    user_db.get_user_repos_synced_at = _fake_get_synced_at
    user_db.save_user_repos = _fake_save

    orig_acct = acct_db.get_user_identities
    acct_db.get_user_identities = _fake_identities

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_current_user_or_api_key] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()
    acct_db.get_user_identities = orig_acct
    user_db.get_user_repos, user_db.get_user_repos_synced_at, user_db.save_user_repos = orig
    _saved_repos.clear()


def test_member_empty_repos_returns_200(client, monkeypatch):
    from src.api.routes import repos as repos_route

    monkeypatch.setattr(repos_route, "with_oauth_token",
                        lambda user, provider, fn, *a, **kw: [])

    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repos"] == []
    assert body.get("_cached") is False

    saved = _saved_repos.get("repos")
    assert saved is not None, "合法空结果未落库（每次请求都会穿透缓存直连 VCS）"
    assert saved == []


def test_sync_empty_repos_returns_200(client, monkeypatch):
    from src.api.routes import repos as repos_route

    monkeypatch.setattr(repos_route, "with_oauth_token",
                        lambda user, provider, fn, *a, **kw: [])

    r = client.post("/api/repos/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repos"] == []
    assert body.get("message") == "同步成功"

    saved = _saved_repos.get("repos")
    assert saved is not None, "合法空结果未落库"
    assert saved == []


def test_member_fetch_failure_still_502(client, monkeypatch):
    from src.api.routes import repos as repos_route

    def _boom(user, provider, fn, *a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(repos_route, "with_oauth_token", _boom)

    r = client.get("/api/repos/member")
    assert r.status_code == 502, r.text


def test_sync_fetch_failure_still_502(client, monkeypatch):
    from src.api.routes import repos as repos_route

    def _boom(user, provider, fn, *a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(repos_route, "with_oauth_token", _boom)

    r = client.post("/api/repos/sync")
    assert r.status_code == 502, r.text
