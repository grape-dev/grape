"""「创建项目」目标上下文（空间清单 + 个人登录名）缓存用例。

口径（需求方拍板）：默认 1800s；meta > env > 默认；配置 0 = 完全不缓存；
缓存键含 token 指纹（换 token/账号必须 miss）；返回体深拷贝。
结构：缓存是应用服务（vcs.namespaces）的横切能力，provider 只表达平台事实。
"""
import pytest

from src.domains.mgmt.vcs import namespaces as N


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    N.invalidate()
    N._ttl_memo = (0.0, N.DEFAULT_TTL_SECONDS)
    monkeypatch.delenv("GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.setattr(N, "_meta_ttl", lambda: "")
    yield
    N.invalidate()


@pytest.fixture()
def spy(monkeypatch):
    """替换实时采集，计数调用次数。"""
    calls = {"n": 0}

    def _fake(provider, token):
        calls["n"] += 1
        return {"provider": provider, "personal_login": "account-a",
                "spaces": [{"path": "grp/sub", "name": "sub", "kind": "group"}]}

    monkeypatch.setattr(N, "_collect", _fake)
    return calls


def _call(provider="gitlab", token="tok-a"):
    return N.create_targets(provider, token)


def test_default_ttl():
    assert N._resolve_ttl() == 1800


def test_env_override(monkeypatch):
    monkeypatch.setenv("GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS", "60")
    assert N._resolve_ttl() == 60


def test_meta_wins_over_env(monkeypatch):
    monkeypatch.setenv("GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS", "60")
    monkeypatch.setattr(N, "_meta_ttl", lambda: "120")
    assert N._resolve_ttl() == 120


def test_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS", "abc")
    assert N._resolve_ttl() == N.DEFAULT_TTL_SECONDS


def test_zero_disables_cache(monkeypatch, spy):
    monkeypatch.setenv("GRAPE_CREATE_SPACES_CACHE_TTL_SECONDS", "0")
    _call()
    _call()
    assert spy["n"] == 2
    assert N.stats()["entries"] == 0


def test_second_call_hits(spy):
    a = _call()
    b = _call()
    assert spy["n"] == 1
    assert a == b and a is not b          # 深拷贝：调用方改不坏缓存
    assert N.stats()["hits"] == 1


def test_token_change_misses(spy):
    _call(token="tok-a")
    _call(token="tok-b")
    assert spy["n"] == 2


def test_provider_change_misses(spy):
    _call(provider="gitlab")
    _call(provider="gitcode")
    assert spy["n"] == 2


def test_expiry_refetches(spy):
    import time as _t

    _call()
    _base = _t.time()
    N._CACHE._clock = lambda: _base + 3600   # 时间前进 1h > 默认 30min → 过期
    _call()
    assert spy["n"] == 2


def test_invalidate_forces_refetch(spy):
    _call()
    assert N.invalidate("gitlab") == 1
    _call()
    assert spy["n"] == 2


def test_invalidate_all(spy):
    _call("gitlab", "t1")
    _call("gitcode", "t2")
    assert N.invalidate() == 2
    assert N.stats()["entries"] == 0