"""场景用例：PAT 全部失效/清理后，工作区必须降级成引导页而不是报 401。

实弹背景（2026-11-16）：用户清掉 PAT（平台侧也吊销了），回到工作区看到
「加载失败: gitcode 授权已过期，请重新登录授权」——纯 PAT 账号根本不存在"重新登录授权"
这个动作，页面还整块被 401 打死，没有任何处置入口。

口径（FR-33 R5）：
  - 合并列表请求（默认工作区入口）：全部可列仓平台都 token 失效且无缓存 → 200 +
    `_no_vcs_binding=true` + 来源感知 hint（PAT → 重新配置 PAT；OAuth → 重新授权）；
  - 显式 `?provider=<p>` 的定向请求：仍抛 401（调用方明确要该平台数据）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.deps import require_org_member
from src.api.main import app
from src.domains.mgmt.routes import repos as repos_route


@pytest.fixture()
def client(monkeypatch):
    """登录用户 + 只配了一个 PAT 的平台 + 该 token 被平台判 401 + 无缓存。"""
    from src.domains.mgmt.auth import grape_account_db as gad
    from src.domains.mgmt.user import db as user_db

    user = {"id": "u-reauth-1", "username": "u-reauth", "auth_type": "jwt", "org_id": 0}
    app.dependency_overrides[require_org_member] = lambda: user

    monkeypatch.setattr(repos_route, "_repo_listable_providers", lambda: ["gitcode"])
    monkeypatch.setattr(repos_route, "is_platform_visible", lambda p: True, raising=False)
    monkeypatch.setattr(gad, "list_platform_links",
                        lambda uid: [{"provider": "gitcode", "source": "pat",
                                      "has_pat_token": True, "external_login": "u"}])
    monkeypatch.setattr(gad, "get_user_identities", lambda uid: [])
    monkeypatch.setattr(user_db, "get_user_repos", lambda uid: [])
    monkeypatch.setattr(user_db, "get_user_repos_synced_at", lambda uid: None)

    def _boom(*a, **kw):
        raise HTTPException(status_code=401, detail={
            "code": 401, "message": "gitcode token rejected", "action": "reauth_required"})

    monkeypatch.setattr(repos_route, "with_oauth_token", _boom)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_member_degrades_to_guide_when_all_pat_dead(client):
    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("_no_vcs_binding") is True, body
    assert body.get("_reauth_provider") == "gitcode", body
    # 纯 PAT 账号不得被引导去"重新登录授权"
    assert "PAT" in body.get("hint", ""), body
    assert "重新登录" not in body.get("hint", ""), body


def test_member_explicit_provider_still_401(client):
    r = client.get("/api/repos/member?provider=gitcode")
    assert r.status_code == 401, r.text
    detail = r.json().get("detail") or {}
    assert detail.get("provider") == "gitcode"
    assert detail.get("action") == "credential_invalid"