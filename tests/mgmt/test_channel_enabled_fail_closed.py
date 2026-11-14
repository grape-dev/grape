"""P2-4 守卫：渠道 enabled=0 必须 fail-closed（直达登录入口不能绕过开关）。

原实现：`_catalog_from_object(enabled_only=True)` 只过滤登录**目录**，
`GET /api/auth/login/<provider>` 直达链接照样发起授权 → 关掉渠道形同虚设。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_disabled_channel_rejected_on_direct_login(monkeypatch):
    from src.domains.mgmt.routes import grape_auth as ga
    from src.domains.mgmt.auth import providers as P

    monkeypatch.setattr(P, "is_channel_enabled", lambda p: False)
    monkeypatch.setattr(P, "get_provider", lambda p: {"name": "Gitee", "provider": p})

    with pytest.raises(HTTPException) as ei:
        ga.oauth_login(provider="gitee", popup=False, request=None, lang="zh")
    e = ei.value
    assert e.status_code == 403, e
    detail = e.detail if isinstance(e.detail, dict) else {}
    assert detail.get("error_code") == "CHANNEL_DISABLED", detail


def test_unknown_provider_still_400(monkeypatch):
    from src.domains.mgmt.routes import grape_auth as ga
    from src.domains.mgmt.auth import providers as P

    monkeypatch.setattr(P, "get_provider", lambda p: None)
    with pytest.raises(HTTPException) as ei:
        ga.oauth_login(provider="nosuch", popup=False, request=None, lang="zh")
    assert ei.value.status_code == 400


def test_enabled_check_uses_table_flag(monkeypatch):
    """真源是表里的 enabled 位：读到 0 → False；读到 1 → True；异常 → fail-open。"""

    class _Row:
        def __init__(self, enabled):
            self.enabled = enabled

    import src.kernel.persistence.repositories_platform_object as repo

    monkeypatch.setattr("src.domains.platform.importer.is_table_backed", lambda: True)
    orig = repo.OAuthChannelRepo.get

    monkeypatch.setattr(repo.OAuthChannelRepo, "get", staticmethod(lambda s, p: _Row(0)))
    assert repo.__name__ and __import__(
        "src.domains.mgmt.auth.providers", fromlist=["is_channel_enabled"]
    ).is_channel_enabled("gitee") is False

    monkeypatch.setattr(repo.OAuthChannelRepo, "get", staticmethod(lambda s, p: _Row(1)))
    assert __import__(
        "src.domains.mgmt.auth.providers", fromlist=["is_channel_enabled"]
    ).is_channel_enabled("gitee") is True

    def _boom(s, p):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo.OAuthChannelRepo, "get", staticmethod(_boom))
    assert __import__(
        "src.domains.mgmt.auth.providers", fromlist=["is_channel_enabled"]
    ).is_channel_enabled("gitee") is True, "DB 抖动时应 fail-open，不能把登录全打挂"

    monkeypatch.setattr(repo.OAuthChannelRepo, "get", orig)