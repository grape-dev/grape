"""GitLab 登录名归一：/user 返回 username，跨平台调用方读 login。

回归背景：身份识别只看 login → GitLab 得到空登录名 → 解除托管 502
「无法获取当前用户 GitLab 身份」，仓主自己也无法解除托管。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.vcs.gitlab import GitLabProvider


class _Card:
    api_base = "https://gitlab.example.com"
    host = "gitlab.example.com"


def _provider(monkeypatch, payload):
    p = GitLabProvider.__new__(GitLabProvider)
    p._card = _Card()
    p.provider = "gitlab"
    monkeypatch.setattr(GitLabProvider, "_http", lambda self, *a, **k: dict(payload))
    return p


def test_verify_token_exposes_login_from_username(monkeypatch):
    p = _provider(monkeypatch, {"username": "account-a", "name": "A", "id": 1})
    info = p.verify_token("tok")
    assert info["login"] == "account-a"
    assert info["username"] == "account-a"


def test_verify_token_keeps_explicit_login(monkeypatch):
    p = _provider(monkeypatch, {"login": "explicit-a", "username": "ignored-a"})
    assert p.verify_token("tok")["login"] == "explicit-a"


def test_vcs_user_login_reads_gitlab_identity(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    class _P:
        def verify_token(self, token):
            return {"username": "account-a"}

    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider", lambda provider: _P(), raising=False)
    # 兼容层：即使适配器没归一，login 缺失也要回落到 username
    assert repo_manager.vcs_user_login("gitlab", "tok") == "account-a"


def test_vcs_user_login_empty_when_nothing_present(monkeypatch):
    from src.domains.mgmt.user import repo_manager

    class _P:
        def verify_token(self, token):
            return {}

    monkeypatch.setattr("src.domains.mgmt.vcs.create_provider", lambda provider: _P(), raising=False)
    assert repo_manager.vcs_user_login("gitlab", "tok") == ""