"""Issue: /repos/member P1-6 软失败分支不落库 → 缓存永不自愈（大厅可见、工作台 403）。

复现前置（三同时）：
1. 用户绑定 gitcode + github 双平台
2. github token 失效（with_oauth_token 抛 401 reauth_required）
3. gitcode 实时列表含旧缓存没有的新仓库

修复后断言：
- partial 分支返回 _partial=True + _reauth_provider=github
- save_user_repos 被调用且写入含新仓库（check_repo_access 查的 user_repos 表能查到）
- 失败平台(github)旧缓存仓库被保留补齐，不被覆盖丢失
- 大厅返回列表 = gitcode 实时 + github 旧缓存合并去重

运行：python3 -m pytest tests/test_member_partial_reauth.py -q
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

UID = 1
USER = {"id": UID, "username": "u_partial", "auth_type": "jwt", "org_id": 0}

# 旧缓存：gitcode 2 个（无新仓库）+ github 1 个
CACHED = [
    {"repo_key": "gitcode:me/old1", "full_name": "me/old1"},
    {"repo_key": "gitcode:me/old2", "full_name": "me/old2"},
    {"repo_key": "github:me/gh1", "full_name": "me/gh1"},
]
# gitcode 实时：新增 new1（即 OPCTeam/HDBlogService 场景）
GITCODE_LIVE = [
    {"repo_key": "gitcode:me/old1", "full_name": "me/old1"},
    {"repo_key": "gitcode:me/old2", "full_name": "me/old2"},
    {"repo_key": "gitcode:me/new1", "full_name": "me/new1"},
]

_saved_repos = {}


@pytest.fixture(scope="module")
def client():
    from src.api.main import app
    from src.api.deps import get_current_user, get_current_user_or_api_key
    from src.api.routes import repos as repos_route
    from src.user import db as user_db

    from src.auth import grape_account_db as acct_db

    def _fake_identities(uid):
        return [{"provider": "gitcode"}, {"provider": "github"}]

    def _fake_get_user_repos(uid):
        return list(CACHED)

    def _fake_get_synced_at(uid):
        return None  # 缓存无时间 → 视为过期 → 走实时拉取

    def _fake_save(uid, repos):
        _saved_repos["repos"] = list(repos)
        return True

    orig_get = user_db.get_user_repos
    orig_synced = user_db.get_user_repos_synced_at
    orig_save = user_db.save_user_repos

    user_db.get_user_repos = _fake_get_user_repos
    user_db.get_user_repos_synced_at = _fake_get_synced_at
    user_db.save_user_repos = _fake_save

    orig_acct_identities = acct_db.get_user_identities
    acct_db.get_user_identities = _fake_identities

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_current_user_or_api_key] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()
    acct_db.get_user_identities = orig_acct_identities
    user_db.get_user_repos = orig_get
    user_db.get_user_repos_synced_at = orig_synced
    user_db.save_user_repos = orig_save
    _saved_repos.clear()


def test_member_partial_reauth_saves_cache(client, monkeypatch):
    from src.api.routes import repos as repos_route
    from src.api.oauth_token import with_oauth_token
    from fastapi import HTTPException

    def _fake_with_oauth_token(user, provider, fn, *a, **kw):
        if provider == "gitcode":
            return list(GITCODE_LIVE)  # 实时成功，含新仓库
        raise HTTPException(status_code=401, detail={"code": 401,
            "message": "github token expired", "action": "reauth_required"})

    # with_oauth_token 在函数体内以模块级导入名使用，patch 模块属性
    monkeypatch.setattr(repos_route, "with_oauth_token", _fake_with_oauth_token)

    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("_partial") is True
    assert body.get("_reauth_provider") == "github"

    keys = {x["repo_key"] for x in body["repos"]}
    # 大厅看到实时 gitcode（含新仓库）+ github 旧缓存补齐
    assert "gitcode:me/new1" in keys
    assert "gitcode:me/old1" in keys
    assert "github:me/gh1" in keys

    # 核心：落库了，且含新仓库和失败平台的旧缓存
    saved = _saved_repos.get("repos")
    assert saved is not None, "partial 分支未调用 save_user_repos（旧bug复现）"
    saved_keys = {x["repo_key"] for x in saved}
    assert "gitcode:me/new1" in saved_keys, "新仓库未落库 → check_repo_access 会 403"
    assert "github:me/gh1" in saved_keys, "失败平台旧缓存被覆盖丢失"
    assert len(saved) == 4, saved_keys  # old1 old2 new1 gh1 去重后 4 个
