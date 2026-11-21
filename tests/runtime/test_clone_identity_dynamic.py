"""clone 用户名动态解析（令牌 → 账号 login）+ 本地缓存的行为级用例。

覆盖：
- 命中缓存不二次出网；不同令牌各自缓存；
- TTL 过期重解析；invalidate 生效；
- 令牌被拒 → 显式异常且不写缓存；
- 同一令牌并发首次解析只出网一次（single-flight）；
- 非 basic_password 平台零出网（常量用户名）；
- basic_password 有令牌 → token_owner；令牌不可用 → 回落卡/owner 档位；
- 纯函数 `clone_username_source` 不受影响（不触发网络）。
"""
from __future__ import annotations

import threading
import types

import pytest

from src.domains.runtime.vcs import clone_endpoint as ce
from src.domains.runtime.vcs import clone_identity as ci


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.delenv("GRAPE_CLONE_LOGIN_TTL_S", raising=False)
    ci._cache.clear()
    ci._locks.clear()
    ci._calls = 0
    ce._warned_providers.clear()
    yield
    ci._cache.clear()
    ci._locks.clear()


def _card(bot_username: str = "", scheme: str = "basic_password", host: str = "gitee.com"):
    return types.SimpleNamespace(host=host, clone_auth=scheme, bot_username=bot_username,
                                 token_env="", bot_token_status="unknown")


def _patch_card(monkeypatch, provider="gitee", **kw):
    card = _card(**kw)
    monkeypatch.setattr(ce, "_card", lambda p: card if p == provider else None)
    return card


def test_cache_hit_avoids_second_lookup(monkeypatch):
    calls = []

    def _fake_fetch(provider, token):
        calls.append(token)
        return "yang-aiwu-new"

    monkeypatch.setattr(ci, "_fetch_login", _fake_fetch)
    assert ci.resolve_login("gitee", "tok-a") == "yang-aiwu-new"
    assert ci.resolve_login("gitee", "tok-a") == "yang-aiwu-new"
    assert len(calls) == 1, calls
    assert ci.cache_stats()["entries"] == 1


def test_distinct_tokens_distinct_entries(monkeypatch):
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: f"login-{t}")
    assert ci.resolve_login("gitee", "tok-a") == "login-tok-a"
    assert ci.resolve_login("gitee", "tok-b") == "login-tok-b"
    assert ci.cache_stats()["entries"] == 2


def test_ttl_expiry_refetches(monkeypatch):
    calls = []
    monkeypatch.setenv("GRAPE_CLONE_LOGIN_TTL_S", "1")
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: (calls.append(t), "x")[1])
    ci.resolve_login("gitee", "tok-a")
    monkeypatch.setattr(ci, "time",
                        types.SimpleNamespace(time=lambda: 10 ** 12))
    assert ci.resolve_login("gitee", "tok-a", use_cache=False) == "x"
    assert len(calls) == 2, calls


def test_use_cache_false_bypasses(monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: (calls.append(t), "x")[1])
    ci.resolve_login("gitee", "tok-a")
    ci.resolve_login("gitee", "tok-a", use_cache=False)
    assert len(calls) == 2


def test_invalidate(monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: (calls.append(t), "x")[1])
    ci.resolve_login("gitee", "tok-a")
    ci.invalidate("gitee", "tok-a")
    ci.resolve_login("gitee", "tok-a")
    assert len(calls) == 2


def test_rejected_token_raises_and_not_cached(monkeypatch):
    def _boom(provider, token):
        raise ci.CloneIdentityError(provider, "token_rejected", "401")

    monkeypatch.setattr(ci, "_fetch_login", _boom)
    with pytest.raises(ci.CloneIdentityError):
        ci.resolve_login("gitee", "bad")
    assert ci.cache_stats()["entries"] == 0


def test_empty_token_raises(monkeypatch):
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: "x")
    with pytest.raises(ci.CloneIdentityError) as ei:
        ci.resolve_login("gitee", "  ")
    assert ei.value.reason == "empty_token"


def test_single_flight_concurrent(monkeypatch):
    """8 线程同时首次解析同一令牌 → 只出网一次。"""
    started = threading.Event()
    calls = []

    def _slow(provider, token):
        calls.append(token)
        started.set()
        import time as _t
        _t.sleep(0.05)
        return "yang-aiwu-new"

    monkeypatch.setattr(ci, "_fetch_login", _slow)
    out = []

    def _worker():
        out.append(ci.resolve_login("gitee", "tok-a"))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == ["yang-aiwu-new"] * 8
    assert len(calls) == 1, calls


def test_scheme_const_no_network(monkeypatch):
    """非 basic_password 平台：常量用户名，零出网。"""
    _patch_card(monkeypatch, provider="gitcode", scheme="x_access_token")
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: pytest.fail("不应出网"))
    assert ci.username_for_token("gitcode", "tok", "gitcode:o/r") == ("x-access-token", "scheme_const")


def test_basic_password_uses_token_owner(monkeypatch):
    _patch_card(monkeypatch, bot_username="wrong-name")
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: "yang-aiwu-new")
    assert ci.username_for_token("gitee", "tok", "gitee:yang-aiwu-new/demo") == (
        "yang-aiwu-new", "token_owner")


def test_basic_password_falls_back_to_card_on_error(monkeypatch):
    _patch_card(monkeypatch, bot_username="card-user")
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: (_ for _ in ()).throw(
        ci.CloneIdentityError("gitee", "token_rejected")))
    assert ci.username_for_token("gitee", "bad", "gitee:some-org/repo") == (
        "card-user", "card_bot_username")


def test_basic_password_owner_fallback_when_card_empty(monkeypatch):
    _patch_card(monkeypatch, bot_username="")
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: (_ for _ in ()).throw(
        ci.CloneIdentityError("gitee", "empty_token")))
    assert ci.username_for_token("gitee", "", "gitee:some-org/repo") == (
        "some-org", "owner_fallback")


def test_pure_clone_username_source_never_networks(monkeypatch):
    """纯函数路径保持无网络：clone_username_source 不能触发 _fetch_login。"""
    _patch_card(monkeypatch, bot_username="card-user")
    monkeypatch.setattr(ci, "_fetch_login", lambda p, t: pytest.fail("不应出网"))
    assert ce.clone_username_source("gitee", "gitee:o/r") == ("card-user", "card_bot_username")