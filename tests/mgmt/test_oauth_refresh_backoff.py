"""OAuth 刷新退避 + 刷新后被上游拒绝的收口 S1–S4（评审补强④）。

问题：refresh 成功但上游仍 401 时，既不落 status=4 也不退避；
GitHub 每次刷新都会轮换 refresh_token → 逐请求刷新 = 风暴式轮换。
口径：
  · 60s 内重复要求刷新 → 跳过网络调用，返回当前 access_token（不轮换）
  · 刷新成功但重试仍 401 → 身份打 status=4（引导重新授权）
"""
from __future__ import annotations

import time

import pytest

from src.domains.mgmt.user import db as mdb


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(mdb, "_REFRESH_LAST", {}, raising=False)
    monkeypatch.setattr(mdb, "_read_refresh_token_from_identity", lambda u, p: "rt-old")
    monkeypatch.setattr(mdb, "_read_refresh_token_from_credentials", lambda u, p: "")
    yield


def test_s1_backoff_skips_the_network_and_keeps_the_token(monkeypatch):
    monkeypatch.setattr(mdb, "_read_access_token_from_identity", lambda u, p: "tok-cached")
    mdb._REFRESH_LAST[(1, "github")] = time.monotonic()

    called = {"n": 0}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: called.__setitem__("n", 1))
    assert mdb.refresh_oauth_token(1, "github") == "tok-cached"
    assert called["n"] == 0, "退避期内不得调用 OAuth 端点"


def test_s2_backoff_expires(monkeypatch):
    mdb._REFRESH_LAST[(1, "github")] = time.monotonic() - 120
    from src.domains.mgmt.auth import providers as prov

    monkeypatch.setattr(prov, "get_client_credentials", lambda p: ("cid", "secret"))
    monkeypatch.setattr(prov, "get_provider", lambda p: {"token_url": "https://oauth/token"})

    class _R:
        def read(self):
            return b'{"access_token":"new","refresh_token":"rt-new","expires_in":3600}'

    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _R())
    from src.domains.mgmt.auth import grape_account_db as gadb

    monkeypatch.setattr(gadb, "update_external_identity_tokens",
                        lambda *a, **k: called.append(k))
    assert mdb.refresh_oauth_token(1, "github") == "new"
    assert (1, "github") in mdb._REFRESH_LAST, "成功后必须记录退避时间戳"
    assert called and called[0]["refresh_token"] == "rt-new"


def test_s3_mark_helper_sets_status_4(monkeypatch):
    seen = {}

    class _C:
        def execute(self, sql, params):
            seen["sql"], seen["params"] = sql, params

    class _Proxy:
        def cursor(self, *a, **k):
            return _C()

        def commit(self):
            seen["committed"] = True

    class _Conn(_Proxy):
        def execute(self, sql, params):
            seen["sql"], seen["params"] = sql, params

    monkeypatch.setattr(mdb, "_mg_db", _Proxy(), raising=False)
    monkeypatch.setattr(mdb, "db", _Conn(), raising=False)
    mdb.mark_identity_reauth_required(7, "github", "post_refresh_401")
    assert "status=4" in seen["sql"] and seen["params"] == (7, "github")
    assert seen.get("committed") is True


def test_s4_post_refresh_401_marks_reauth(monkeypatch):
    import src.api.oauth_token as ot
    import src.domains.mgmt.user.db as udb

    marked = []
    monkeypatch.setattr(udb, "mark_identity_reauth_required",
                        lambda u, p, r="": marked.append((u, p, r)), raising=False)
    monkeypatch.setattr(udb, "refresh_oauth_token", lambda u, p: "tok-new")
    monkeypatch.setattr(ot, "get_user_token_for", lambda *a, **k: "tok-old", raising=False)

    class _Err(Exception):
        pass

    def _api(token):
        raise _Err("401 Unauthorized")

    monkeypatch.setattr(ot, "_is_token_error", lambda e: True, raising=False)
    monkeypatch.setattr(ot, "_is_business_forbidden", lambda e: False, raising=False)

    with pytest.raises(Exception):
        ot.with_oauth_token({"id": 7, "username": "u"}, "github", _api)
    assert marked and marked[0][:2] == (7, "github") and marked[0][2] == "post_refresh_401"
