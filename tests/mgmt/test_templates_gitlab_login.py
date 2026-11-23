"""一键创建在 GitLab 上的身份取值（2026-11-22 实锤）。

`create_from_template` 取当前用户登录名时只认 `login` 字段；GitLab 的 `/user`
只有 `username` → 恒 502「无法获取当前 gitlab 用户身份」，一键创建在 GitLab 上不可用。
本用例锁：verify_token 只回 username 时也必须能拿到身份（继续走到建仓步骤），
并且 github/gitcode 的 login 语义不回归。
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402
import src.domains.mgmt.vcs as _vcs_mod  # create_provider 现居此模块
from fastapi import HTTPException  # noqa: E402

from src.domains.mgmt.routes import templates as T  # noqa: E402


class _Adapter:
    def __init__(self, info):
        self._info = info
        self.repo_calls = []

    def verify_token(self, token):
        return dict(self._info)

    def create_repo(self, token, name, description="", **kwargs):
        raise RuntimeError("PROBE-REACHED-CREATE-REPO")


def _run(monkeypatch, info, provider="gitlab"):
    monkeypatch.setattr(T, "_find_template",
                        lambda tid: {"template": {"id": "tpl", "default_repo_name": "demo-x",
                                                  "description": ""}})
    monkeypatch.setattr(T, "get_user_token_for", lambda *a, **k: "tok")
    import src.api.oauth_token as OT
    monkeypatch.setattr(OT, "with_oauth_token", lambda user, prov, fn: fn("tok"))
    import src.kernel.token_resolver as TR
    adapter = _Adapter(info)
    monkeypatch.setattr(_vcs_mod, "create_provider", lambda p: adapter)
    body = types.SimpleNamespace(template_id="tpl", provider=provider,
                                 repo_name="demo-x", dev_scene="",
                                 owner="", private=False)
    user = {"id": "33", "username": "account-a", "login_provider": provider}
    return adapter, asyncio.run(T.create_from_template(body, user=user))


def test_gitlab_username_only_is_accepted(monkeypatch):
    adapter, _ = None, None
    with pytest.raises(HTTPException) as ei:
        _run(monkeypatch, {"id": 10000001, "username": "account-a"})   # GitLab: 无 login
    detail = str(ei.value.detail)
    assert "无法获取当前 gitlab 用户身份" not in detail
    # 身份取到后应推进到建仓步骤（我们的探针在这里抛错）
    assert "PROBE-REACHED-CREATE-REPO" in detail or "创建仓库失败" in detail


def test_github_login_still_works(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _run(monkeypatch, {"id": 7, "login": "octo"}, provider="github")
    detail = str(ei.value.detail)
    assert "无法获取当前 github 用户身份" not in detail
    assert "PROBE-REACHED-CREATE-REPO" in detail or "创建仓库失败" in detail


def test_empty_identity_still_errors(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _run(monkeypatch, {"id": 1})            # 两个名字字段都没有
    assert "无法获取当前 gitlab 用户身份" in str(ei.value.detail)