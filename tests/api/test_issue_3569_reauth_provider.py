"""Issue #3569 —— 401 reauth_required detail 必须带结构化 provider。

背景：前端按 401 `detail.provider` 选择 OAuth 授权入口（github → /api/auth/login/github，
gitcode → /auth/login/gitcode，缺失则回退 GitCode）。`with_oauth_token` 三条 401 路径若漏带
provider，GitHub 令牌失效会被送去 GitCode 重新授权，形成整页重载闭环。

覆盖：
1. 首次取 token 为空（已过期且无 refresh）
2. 401 → refresh 无新 token
3. refresh 成功但重试仍 401
4. gitcode 入参透出 gitcode（不是硬编码）
5. repos.py 的 401 detail 仍保留 provider（基线已带，防回归删除）

运行：python3 -m pytest tests/api/test_issue_3569_reauth_provider.py -q
"""
import os
import sys

import pytest
from fastapi import HTTPException

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

USER = {"id": "u-3569", "username": "u_3569"}


def _token_error(provider: str) -> RuntimeError:
    return RuntimeError(f"{provider} API /user/repos failed (401): Bad credentials")


def _detail(exc: HTTPException) -> dict:
    assert exc.status_code == 401
    assert isinstance(exc.detail, dict)
    return exc.detail


def test_401_first_path_carries_provider(monkeypatch):
    import src.api.deps as deps
    from src.api.oauth_token import with_oauth_token

    monkeypatch.setattr(deps, "get_user_token_for", lambda *a, **kw: "")
    with pytest.raises(HTTPException) as ei:
        with_oauth_token(USER, "github", lambda token: {"ok": True})
    d = _detail(ei.value)
    assert d["action"] == "reauth_required"
    assert d["provider"] == "github"


def test_401_refresh_empty_carries_provider(monkeypatch):
    import src.api.deps as deps
    import src.domains.mgmt.user.db as user_db
    from src.api.oauth_token import with_oauth_token

    monkeypatch.setattr(deps, "get_user_token_for", lambda *a, **kw: "old-token")
    monkeypatch.setattr(user_db, "refresh_oauth_token", lambda uid, provider: None)

    with pytest.raises(HTTPException) as ei:
        with_oauth_token(USER, "github", lambda token: (_ for _ in ()).throw(_token_error("github")))
    d = _detail(ei.value)
    assert d["action"] == "reauth_required"
    assert d["provider"] == "github"


def test_401_retry_still_unauthorized_carries_provider(monkeypatch):
    import src.api.deps as deps
    import src.domains.mgmt.user.db as user_db
    from src.api.oauth_token import with_oauth_token

    monkeypatch.setattr(deps, "get_user_token_for", lambda *a, **kw: "old-token")
    monkeypatch.setattr(user_db, "refresh_oauth_token", lambda uid, provider: "new-token")

    with pytest.raises(HTTPException) as ei:
        with_oauth_token(USER, "github", lambda token: (_ for _ in ()).throw(_token_error("github")))
    d = _detail(ei.value)
    assert d["provider"] == "github"


def test_provider_is_not_hardcoded_gitcode(monkeypatch):
    import src.api.deps as deps
    from src.api.oauth_token import with_oauth_token

    monkeypatch.setattr(deps, "get_user_token_for", lambda *a, **kw: "")
    with pytest.raises(HTTPException) as ei:
        with_oauth_token(USER, "gitcode", lambda token: {"ok": True})
    assert _detail(ei.value)["provider"] == "gitcode"


def test_repos_401_detail_keeps_provider_field():
    """基线 repos.py 的 401 detail 已带 provider —— 改动时不得丢。"""
    path = os.path.join(ROOT, "src/domains/mgmt/routes/repos.py")
    src = open(path, encoding="utf-8").read()
    idx = src.find('"action": _action,')
    assert idx > 0, "repos.py 401 reauth 分支未找到"
    window = src[idx: idx + 400]
    assert '"provider": _reauth_provider' in window, "repos.py 401 detail 丢失 provider 字段"


def test_frontend_uses_provider_aware_redirect():
    """前端两个 401 分支都走 _redirectToOAuth，不再无条件跳 GitCode。"""
    path = os.path.join(ROOT, "src/api/frontend/shell/js/api.js")
    src = open(path, encoding="utf-8").read()
    assert "_redirectToGitcodeAuth" not in src
    assert src.count("_redirectToOAuth(err?.detail?.provider)") == 2
    assert "'/auth/login/github'" in src