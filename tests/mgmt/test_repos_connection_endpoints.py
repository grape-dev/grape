"""平台 PAT 连接端点：绑定/清除必须有审计、且不回显 token（复审 C3 落实 + 看护缺口）。

覆盖：
  1. PUT 成功 → 落 user_credentials(kind=pat) 且写审计 repos_connection_bind；
  2. DELETE → 清空凭据且写审计 repos_connection_clear；
  3. GET 只回存在性与登录名，**不回显 token**；
  4. token 校验失败 → 400/502，且不写审计（未绑定就不该留痕）。
"""
import types

import pytest

from src.domains.mgmt.routes import repos as repos_mod


class _FakeUserDb:
    def __init__(self):
        self.creds = {}
        self.invalidated = 0

    def get_credential(self, uid, provider, kind=""):
        return self.creds.get((uid, provider), {})

    def set_credential(self, uid, provider, cfg, kind=""):
        self.creds[(uid, provider)] = dict(cfg or {})
        return True

    def invalidate_user_repos(self, uid):
        self.invalidated += 1


@pytest.fixture()
def wiring(monkeypatch):
    fake = _FakeUserDb()
    audits = []
    monkeypatch.setattr(repos_mod, "user_db", fake, raising=False)
    monkeypatch.setattr(repos_mod, "_provider_card_or_404", lambda p: None, raising=False)
    monkeypatch.setattr(repos_mod, "log_audit",
                        lambda ev, **kw: audits.append((ev, kw)), raising=False)
    return fake, audits


def _headers_user():
    return {"id": "u-1", "username": "account-a"}


def test_put_binds_pat_and_writes_audit(wiring, monkeypatch):
    fake, audits = wiring

    class _Prov:
        def verify_token(self, token):
            assert token == "tk-1"
            return {"id": 7, "login": "account-a"}

    import src.domains.mgmt.vcs as _vcs_mod
    monkeypatch.setattr(_vcs_mod, "create_provider", lambda p: _Prov())
    body = types.SimpleNamespace(token="tk-1", login="")
    out = repos_mod.set_repo_connection("gitlab", body, user=_headers_user())
    assert out["configured"] is True and out["login"] == "account-a"
    assert fake.creds[("u-1", "gitlab")]["token"] == "tk-1"
    assert any(ev == "repos_connection_bind" and kw["detail"]["provider"] == "gitlab"
               for ev, kw in audits)
    # 审计里绝不能出现 token 明文
    assert all("tk-1" not in str(kw) for _, kw in audits)


def test_delete_clears_and_writes_audit(wiring):
    fake, audits = wiring
    fake.creds[("u-1", "gitlab")] = {"token": "tk-1", "login": "account-a"}
    out = repos_mod.delete_repo_connection("gitlab", user=_headers_user())
    assert out["configured"] is False
    assert fake.creds[("u-1", "gitlab")] == {}
    assert any(ev == "repos_connection_clear" for ev, _ in audits)


def test_get_never_returns_the_token(wiring):
    fake, _ = wiring
    fake.creds[("u-1", "gitlab")] = {"token": "tk-1", "login": "account-a"}
    out = repos_mod.get_repo_connection("gitlab", user=_headers_user())
    assert out == {"provider": "gitlab", "configured": True,
                   "login": "account-a", "source": "pat"}
    assert "tk-1" not in str(out)


def test_invalid_token_is_rejected_without_audit(wiring, monkeypatch):
    from fastapi import HTTPException

    fake, audits = wiring

    class _Prov:
        def verify_token(self, token):
            raise RuntimeError("401 Unauthorized")

    import src.domains.mgmt.vcs as _vcs_mod
    monkeypatch.setattr(_vcs_mod, "create_provider", lambda p: _Prov())
    body = types.SimpleNamespace(token="bad", login="")
    with pytest.raises(HTTPException) as ei:
        repos_mod.set_repo_connection("gitlab", body, user=_headers_user())
    assert ei.value.status_code == 502
    assert not [ev for ev, _ in audits if ev.startswith("repos_connection_")]