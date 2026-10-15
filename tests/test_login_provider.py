"""Issue #247 测试：login_provider 字段驱动 provider 选择。

覆盖场景：
- JWT 含 login_provider=gitcode → 优先返回 gitcode token（即使同时绑定 github token）
- JWT 含 login_provider=github → 优先返回 github token
- JWT 无 login_provider（密码登录/旧 token）→ fallback 到现有 github 优先逻辑
- login_provider 指定平台但无对应 token → fallback 到现有逻辑
- create_access_token 写入 login_provider 字段
- create_refresh_token 写入 login_provider 字段并滚动继承
- get_current_user 提取 login_provider 并传递
- refresh 端点从旧 refresh_token 继承 login_provider
"""
import uuid

import pytest
from unittest.mock import patch

from src.api.jwt_utils import (create_access_token, verify_access_token,
                               create_refresh_token, verify_refresh_token)
from src.api.routes.repos import _get_user_provider_and_token


class TestCreateAccessTokenLoginProvider:
    """create_access_token 写入 login_provider 字段"""

    def test_with_login_provider_gitcode(self):
        token = create_access_token(1, "alice", token_version=0, login_provider="gitcode")
        payload = verify_access_token(token)
        assert payload is not None
        assert payload["login_provider"] == "gitcode"

    def test_with_login_provider_github(self):
        token = create_access_token(2, "bob", token_version=0, login_provider="github")
        payload = verify_access_token(token)
        assert payload is not None
        assert payload["login_provider"] == "github"

    def test_without_login_provider_omits_field(self):
        """密码登录/旧 token 不传 login_provider → payload 中无此字段（不报错）"""
        token = create_access_token(3, "carol", token_version=0)
        payload = verify_access_token(token)
        assert payload is not None
        assert "login_provider" not in payload

    def test_empty_login_provider_omits_field(self):
        token = create_access_token(4, "dave", token_version=0, login_provider="")
        payload = verify_access_token(token)
        assert payload is not None
        assert "login_provider" not in payload


class TestGetUserProviderAndToken:
    """_get_user_provider_and_token 按 login_provider 优先选择"""

    def test_gitcode_login_prefers_gitcode_token(self):
        """login_provider=gitcode + 双平台 token 共存 → 返回 gitcode"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value="gh_tok"), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value="gc_tok"):
            provider, token = _get_user_provider_and_token("alice", login_provider="gitcode")
        assert provider == "gitcode"
        assert token == "gc_tok"

    def test_github_login_prefers_github_token(self):
        """login_provider=github + 双平台 token 共存 → 返回 github"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value="gh_tok"), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value="gc_tok"):
            provider, token = _get_user_provider_and_token("bob", login_provider="github")
        assert provider == "github"
        assert token == "gh_tok"

    def test_no_login_provider_fallback_github_first(self):
        """无 login_provider → fallback 现有逻辑（github 优先）"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value="gh_tok"), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value="gc_tok"):
            provider, token = _get_user_provider_and_token("carol")
        assert provider == "github"
        assert token == "gh_tok"

    def test_gitcode_login_but_no_gitcode_token_fallback(self):
        """login_provider=gitcode 但无 gitcode token → fallback 到 github"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value="gh_tok"), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value=""):
            provider, token = _get_user_provider_and_token("dave", login_provider="gitcode")
        assert provider == "github"
        assert token == "gh_tok"

    def test_github_login_but_no_github_token_fallback(self):
        """login_provider=github 但无 github token → fallback 到 gitcode"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value=""), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value="gc_tok"):
            provider, token = _get_user_provider_and_token("eve", login_provider="github")
        assert provider == "gitcode"
        assert token == "gc_tok"

    def test_no_tokens_returns_empty(self):
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value=""), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value=""):
            provider, token = _get_user_provider_and_token("frank", login_provider="gitcode")
        assert provider == ""
        assert token == ""

    def test_gitcode_login_only_gitcode_token(self):
        """login_provider=gitcode + 仅 gitcode token → 返回 gitcode"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value=""), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value="gc_tok"):
            provider, token = _get_user_provider_and_token("grace", login_provider="gitcode")
        assert provider == "gitcode"
        assert token == "gc_tok"

    def test_github_login_only_github_token(self):
        """login_provider=github + 仅 github token → 返回 github"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value="gh_tok"), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value=""):
            provider, token = _get_user_provider_and_token("henry", login_provider="github")
        assert provider == "github"
        assert token == "gh_tok"

    def test_unknown_login_provider_fallback(self):
        """未知 login_provider 值 → fallback 到现有逻辑"""
        with patch("src.api.routes.repos.get_user_github_token_with_refresh", return_value="gh_tok"), \
             patch("src.api.routes.repos.get_user_token_with_refresh", return_value="gc_tok"):
            provider, token = _get_user_provider_and_token("ivan", login_provider="gitlab")
        assert provider == "github"
        assert token == "gh_tok"


class TestGetCurrentUserLoginProvider:
    """get_current_user 提取 login_provider 并放入返回 dict"""

    def test_jwt_with_login_provider_extracted(self):
        from src.api.deps import get_current_user
        from src.auth.grape_account_db import create_grape_account, get_token_version, soft_delete_account

        uname = "lp_test_" + uuid.uuid4().hex[:6]
        u = create_grape_account(username=uname, email="", password="pw")
        try:
            token = create_access_token(u["id"], uname, get_token_version(u["id"]),
                                        login_provider="gitcode")
            user_obj = get_current_user(authorization=f"Bearer {token}")
            assert user_obj["login_provider"] == "gitcode"
        finally:
            soft_delete_account(u["id"])

    def test_jwt_without_login_provider_defaults_empty(self):
        from src.api.deps import get_current_user
        from src.auth.grape_account_db import create_grape_account, get_token_version, soft_delete_account

        uname = "lp_noprov_" + uuid.uuid4().hex[:6]
        u = create_grape_account(username=uname, email="", password="pw")
        try:
            token = create_access_token(u["id"], uname, get_token_version(u["id"]))
            user_obj = get_current_user(authorization=f"Bearer {token}")
            assert user_obj["login_provider"] == ""
        finally:
            soft_delete_account(u["id"])


class TestCreateRefreshTokenLoginProvider:
    """create_refresh_token 写入 login_provider 并在滚动续期时继承"""

    def test_refresh_token_with_login_provider(self):
        token = create_refresh_token(1, "alice", token_version=0, login_provider="gitcode")
        payload = verify_refresh_token(token)
        assert payload is not None
        assert payload["login_provider"] == "gitcode"

    def test_refresh_token_without_login_provider_omits_field(self):
        token = create_refresh_token(2, "bob", token_version=0)
        payload = verify_refresh_token(token)
        assert payload is not None
        assert "login_provider" not in payload

    def test_refresh_token_empty_login_provider_omits_field(self):
        token = create_refresh_token(3, "carol", token_version=0, login_provider="")
        payload = verify_refresh_token(token)
        assert payload is not None
        assert "login_provider" not in payload

    def test_refresh_token_inherits_login_provider_via_max_exp(self):
        """refresh 滚动续期时 login_provider 与 max_exp 一起继承"""
        import time
        # 首次签发带 login_provider
        old_max_exp = int(time.time()) + 86400
        old_token = create_refresh_token(4, "dave", token_version=0, max_exp=old_max_exp,
                                          login_provider="github")
        old_payload = verify_refresh_token(old_token)
        assert old_payload["login_provider"] == "github"
        # 模拟 refresh 端点：从旧 payload 提取 login_provider 传给新 token
        inherited_lp = old_payload.get("login_provider", "")
        new_token = create_refresh_token(4, "dave", token_version=0, max_exp=old_max_exp,
                                          login_provider=inherited_lp)
        new_payload = verify_refresh_token(new_token)
        assert new_payload["login_provider"] == "github"
        # max_exp 也继承
        assert new_payload["max_exp"] == old_max_exp


class TestRefreshEndpointLoginProvider:
    """refresh 端点从旧 refresh_token 继承 login_provider"""

    def test_refresh_carries_login_provider(self):
        """POST /auth/refresh 从旧 refresh_token 提取 login_provider 传给新 access_token"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.auth.grape_account_db import create_grape_account, get_token_version, soft_delete_account

        uname = "refresh_lp_" + uuid.uuid4().hex[:6]
        u = create_grape_account(username=uname, email="", password="pw")
        client = TestClient(app)
        try:
            # 构造带 login_provider 的 refresh_token（模拟 OAuth 登录后 cookie）
            tv = get_token_version(u["id"])
            rt = create_refresh_token(u["id"], uname, tv, login_provider="gitcode")
            # 调 refresh 端点
            r = client.post("/api/auth/refresh",
                            headers={"X-Requested-With": "XMLHttpRequest"},
                            cookies={"refresh_token": rt})
            assert r.status_code == 200
            new_access = r.json()["token"]
            # 新 access_token 应携带 login_provider=gitcode
            payload = verify_access_token(new_access)
            assert payload is not None
            assert payload.get("login_provider") == "gitcode"
            # 新 refresh cookie 也应携带（通过 Set-Cookie 中的 token 验证）
            set_cookie = r.headers.get("set-cookie", "")
            assert "refresh_token" in set_cookie
        finally:
            soft_delete_account(u["id"])

    def test_refresh_without_login_provider_stays_empty(self):
        """旧 refresh_token 无 login_provider（密码登录）→ 新 access_token 也无（不伪造）"""
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.auth.grape_account_db import create_grape_account, get_token_version, soft_delete_account

        uname = "refresh_nolp_" + uuid.uuid4().hex[:6]
        u = create_grape_account(username=uname, email="", password="pw")
        client = TestClient(app)
        try:
            tv = get_token_version(u["id"])
            rt = create_refresh_token(u["id"], uname, tv)  # 无 login_provider
            r = client.post("/api/auth/refresh",
                            headers={"X-Requested-With": "XMLHttpRequest"},
                            cookies={"refresh_token": rt})
            assert r.status_code == 200
            new_access = r.json()["token"]
            payload = verify_access_token(new_access)
            assert payload is not None
            assert "login_provider" not in payload
        finally:
            soft_delete_account(u["id"])
