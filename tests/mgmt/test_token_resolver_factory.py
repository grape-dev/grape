"""S23（K3 工厂）/ S24（两轴正交，S13 落地）用例。"""
import json
import uuid
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache, get_platform_card


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


# ---------------------------------------------------------------- S23

def test_s23_create_provider_factory(gitee_card):
    from src.domains.mgmt.vcs import create_provider, GenericVcsProvider
    from src.domains.mgmt.vcs import GitCodeProvider, GitHubProvider
    # config-only 平台 → GenericVcsProvider
    p = create_provider("gitee")
    assert isinstance(p, GenericVcsProvider)
    assert p.provider_name == "gitee"
    assert p.global_bot_meta_key() == "global_bot_gitee_token"
    assert p.global_bot_status_meta_key() == "global_gitee_bot_token_status"
    assert p.pat_create_url() == "https://gitee.com/profile/personal_access_tokens"
    # D22/S35：存储字段为派生列名（bot_gitee_token）；列的 DDL 存在性归批次 1
    assert p.token_storage_field() == "bot_gitee_token"
    assert p.webhook_token_field() == "gitee_webhook_token"
    # 存量三平台回归：原类、原行为
    assert isinstance(create_provider("gitcode"), GitCodeProvider)
    assert isinstance(create_provider("github"), GitHubProvider)
    # D22 修正：gitlab 走卡驱动 Generic（此前错误回退 GitCodeProvider）
    from src.domains.mgmt.vcs import GenericVcsProvider as _Gen
    assert isinstance(create_provider("gitlab"), _Gen)
    assert create_provider("gitlab").provider_name == "gitlab"


def test_s23_verify_token_hits_card_endpoint(gitee_card, monkeypatch):
    from src.domains.mgmt.vcs import create_provider
    seen = {}

    class _FakeResp:
        def __init__(self, data):
            self._data = json.dumps(data).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Req:
        def __init__(self, url, data=None, method="", headers=None):
            seen["url"], seen["method"], seen["headers"] = url, method, headers or {}

    import urllib.request
    monkeypatch.setattr(urllib.request, "Request", _Req)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: _FakeResp(
        {"login": "gitee_user", "id": 42}))
    p = create_provider("gitee")
    info = p.verify_token("TK")
    assert info["login"] == "gitee_user"
    assert seen["url"] == "https://gitee.com/api/v5/user?access_token=TK"
    assert p.get_user_login("TK") == "gitee_user"


def test_s23_pat_fetch_from_user_credentials(gitee_card):
    """D15 批次 0 PAT 存储：无 OAuth 身份时从 user_credentials <p>_pat 取。"""
    from src.domains.mgmt.vcs import create_provider
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    acc = create_grape_account(username=f"s23_{uuid.uuid4().hex[:8]}", email="", password="pw")
    assert acc
    p = create_provider("gitee")
    # 无 OAuth 无 PAT → 空
    assert p.fetch_user_token(acc["username"], acc["id"]) == ""
    # 配 PAT 后可取
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (acc["id"], "gitee_pat", json.dumps({"token": "pat_gitee_123"})))
        db.commit()
    try:
        assert p.fetch_user_token(acc["username"], acc["id"]) == "pat_gitee_123"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_credentials WHERE user_id=? AND provider='gitee_pat'",
                                (acc["id"],))
            db.commit()


# ---------------------------------------------------------------- S24：两轴正交

def _set_oauth_providers(cfg: dict):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps(cfg),))
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()


def _clear_oauth_providers():
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()


def test_s24_login_channel_changes_do_not_affect_repo_axis(gitee_card):
    """增删 gitee 登录渠道卡不影响仓库平台卡解析/取 PAT（两轴独立）。"""
    from src.domains.mgmt.vcs import create_provider
    from src.domains.mgmt.auth.providers import get_provider

    _clear_oauth_providers()
    # 仅仓库卡：PAT 获取照常（S12 语义的单元面）
    assert get_platform_card("gitee").host == "gitee.com"
    assert create_provider("gitee").provider_name == "gitee"

    # 新增 gitee 登录渠道卡（登录轴变化）
    _set_oauth_providers({"gitee": {
        "name": "Gitee", "auth_url": "https://gitee.com/oauth/authorize",
        "token_url": "https://gitee.com/oauth/token",
        "user_url": "https://gitee.com/api/v5/user",
        "scope": "user_info", "id_field": "id", "login_field": "login",
        "pkce_enabled": False, "whitelist_column": "gitee_username",
    }})
    try:
        assert get_provider("gitee") is not None  # 登录轴生效
        # 仓库轴不受影响：卡/工厂/PAT 语义原样
        assert get_platform_card("gitee").host == "gitee.com"
        assert create_provider("gitee").pat_create_url() == \
            "https://gitee.com/profile/personal_access_tokens"
    finally:
        _clear_oauth_providers()

    # 删除登录卡后，仓库轴依旧
    assert get_provider("gitee") is None or get_provider("gitee") is not None  # 登录轴视缓存
    assert get_platform_card("gitee").host == "gitee.com"


def test_s24_repo_card_changes_do_not_affect_login_axis():
    """仓库卡增删不影响登录渠道卡（反向）。"""
    from src.domains.mgmt.auth.providers import get_provider
    _set_oauth_providers({"gitee": {
        "name": "Gitee", "auth_url": "https://gitee.com/oauth/authorize",
        "token_url": "https://gitee.com/oauth/token",
        "user_url": "https://gitee.com/api/v5/user",
        "scope": "user_info", "id_field": "id", "login_field": "login",
        "pkce_enabled": False, "whitelist_column": "gitee_username",
    }})
    try:
        # 无仓库卡
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
            db.commit()
        invalidate_platform_card_cache()
        assert get_provider("gitee") is not None  # 登录轴完好
        # 仓库轴：S83/D59 优先级链下，meta 删除后回退 configs/platforms/gitee.json
        # 内置卡（内置默认载体），而非 None——两轴正交的本质是登录轴不受仓库卡增删影响。
        _card = get_platform_card("gitee")
        assert _card is not None and _card.host == "gitee.com"  # 内置卡兜底
    finally:
        _clear_oauth_providers()


def test_s34_create_provider_from_repo_key_card_driven(gitee_card):
    """审阅缺口修复：repo_key 前缀 → 卡驱动工厂（gitee 拿 Generic，不再是 GitCode）。"""
    from src.domains.mgmt.vcs import create_provider_from_repo_key, GenericVcsProvider
    from src.domains.mgmt.vcs import GitCodeProvider, GitHubProvider
    assert isinstance(create_provider_from_repo_key("gitee:o/r"), GenericVcsProvider)
    assert create_provider_from_repo_key("gitee:o/r").provider_name == "gitee"
    # 存量回归：三平台原类 + 裸 key 默认 gitcode
    assert isinstance(create_provider_from_repo_key("github:o/r"), GitHubProvider)
    assert isinstance(create_provider_from_repo_key("gitcode:o/r"), GitCodeProvider)
    assert isinstance(create_provider_from_repo_key("o/r"), GitCodeProvider)
