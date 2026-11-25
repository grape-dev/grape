"""Issue #3356: /api/repos/member stale-while-revalidate + 多 provider 并行回源。

覆盖三条验收路径：
1. 缓存命中（未过期）→ 不回源；
2. 缓存过期 → 立即返回旧缓存 + 后台刷新；
3. refresh=1 → 同步等待回源完成（已发布契约「写后立刻要准」）。

附加：首次无缓存并行回源、后台失败保留旧值、singleflight 防重复回源。
运行：python3 -m pytest tests/mgmt/test_3356_member_cache_swr.py -q
"""
import os
import sys
import threading
from datetime import datetime, timedelta

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

USER = {"id": 33561, "username": "u_3356", "auth_type": "jwt", "org_id": 0}
PROVIDERS = ["gitcode", "github"]


@pytest.fixture(autouse=True)
def _clear_inflight():
    from src.domains.mgmt.routes import repos as repos_route

    with repos_route._REFRESH_GUARD:
        repos_route._REFRESH_INFLIGHT.clear()
    yield
    with repos_route._REFRESH_GUARD:
        repos_route._REFRESH_INFLIGHT.clear()


def _now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _old_ts():
    return (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")


def _install_route_stubs(monkeypatch, cached, synced_at, fetch_impl):
    """Patch member 路由的外部依赖，返回调用记录。

    fetch_impl(user, provider) -> list[dict] 或抛异常；模拟 with_oauth_token 结果。
    """
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.auth import grape_account_db as acct_db
    from src.domains.platform import visibility

    calls = {"fetch": [], "saved": []}

    monkeypatch.setattr(repos_route, "_repo_listable_providers", lambda: set(PROVIDERS))
    monkeypatch.setattr(visibility, "is_platform_visible", lambda p: True)
    monkeypatch.setattr(acct_db, "list_platform_links", lambda uid: [
        {"provider": p, "token_usable": True, "source": "pat"} for p in PROVIDERS
    ])
    # 缓存视图随落库更新：refresh=1 同步等待后重读缓存能看到刚回源的行
    state = {"rows": list(cached)}
    monkeypatch.setattr(repos_route.user_db, "get_user_repos", lambda uid: list(state["rows"]))
    monkeypatch.setattr(repos_route.user_db, "get_user_repos_synced_at", lambda uid: synced_at)
    monkeypatch.setattr(repos_route, "_filter_repos",
                        lambda uid, repos, org_id=0: list(repos))

    def _save(uid, repos, providers=None):
        calls["saved"].append({"repos": list(repos),
                               "providers": list(providers) if providers is not None else None})
        state["rows"] = list(repos)

    monkeypatch.setattr(repos_route.user_db, "save_user_repos", _save)
    monkeypatch.setattr(repos_route, "_start_stale_probe_async", lambda *a, **kw: None)

    def _with_oauth_token(user, provider, fn, *a, **kw):
        calls["fetch"].append(provider)
        return fetch_impl(user, provider)

    monkeypatch.setattr(repos_route, "with_oauth_token", _with_oauth_token)
    return calls


def _wait_refresh(timeout=5.0):
    from src.domains.mgmt.routes import repos as repos_route

    key = repos_route._member_refresh_key(USER["id"], PROVIDERS)
    assert repos_route._wait_member_refresh(key, timeout), "后台刷新未在超时内结束"


def _repo(provider, name):
    return {"repo_key": f"{provider}:me/{name}", "full_name": f"me/{name}",
            "owner": "me", "name": name, "is_admin": True}


def test_member_fresh_cache_fast_path(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "cached")]
    calls = _install_route_stubs(monkeypatch, cached, _now_ts(),
                                 lambda user, provider: pytest.fail("命中缓存不应回源"))

    result = repos_route.list_member_repos(USER, provider="", refresh=0)

    assert result["_cached"] is True
    assert [r["repo_key"] for r in result["repos"]] == ["gitcode:me/cached"]
    assert calls["fetch"] == []


def test_member_expired_cache_returns_stale_then_refreshes(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "old"), _repo("github", "old")]
    gate = threading.Event()
    started_fetch = threading.Event()

    def _fetch(user, provider):
        started_fetch.set()
        gate.wait(5)
        return [_repo(provider, "new")]

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)

    result = repos_route.list_member_repos(USER, provider="", refresh=0)

    # 回源仍被 gate 阻塞时请求就已返回旧缓存，证明未同步等待后台刷新。
    assert started_fetch.wait(5), "后台刷新未启动"
    assert not gate.is_set()
    assert result["_cached"] is True
    assert result["_stale"] is True
    assert {r["repo_key"] for r in result["repos"]} == {"gitcode:me/old", "github:me/old"}

    gate.set()
    _wait_refresh()
    assert sorted(calls["fetch"]) == PROVIDERS
    assert calls["saved"], "后台刷新完成后应落库"
    saved = calls["saved"][-1]
    assert sorted(saved["providers"]) == PROVIDERS
    assert {r["repo_key"] for r in saved["repos"]} == {"gitcode:me/new", "github:me/new"}


def test_member_refresh_one_waits_for_vcs(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "cached")]
    started_fetch = threading.Event()

    def _fetch(user, provider):
        started_fetch.set()
        return [_repo(provider, "fresh")]

    calls = _install_route_stubs(monkeypatch, cached, _now_ts(), _fetch)

    result = repos_route.list_member_repos(USER, provider="", refresh=1)

    # 已发布契约：refresh=1 同步等待回源并落库，返回的是回源后的新列表。
    assert started_fetch.is_set(), "refresh=1 应同步回源"
    assert result["_cached"] is False
    assert result["_refresh_requested"] is True
    assert "_stale" not in result
    assert {r["repo_key"] for r in result["repos"]} == {
        "gitcode:me/fresh", "github:me/fresh"}
    assert sorted(calls["fetch"]) == PROVIDERS
    assert calls["saved"], "refresh=1 回源结果应落库"
    assert {r["repo_key"] for r in calls["saved"][-1]["repos"]} == {
        "gitcode:me/fresh", "github:me/fresh"}


def test_member_first_load_fetches_providers_in_parallel(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    # Barrier：两个平台都必须到达屏障才放行；若未并行，wait 超时炸 BrokenBarrierError
    barrier = threading.Barrier(2, timeout=5)

    def _fetch(user, provider):
        barrier.wait()
        return [_repo(provider, "a")]

    calls = _install_route_stubs(monkeypatch, [], None, _fetch)

    result = repos_route.list_member_repos(USER, provider="", refresh=0)

    assert sorted(calls["fetch"]) == PROVIDERS
    assert result["_cached"] is False
    assert {r["repo_key"] for r in result["repos"]} == {"gitcode:me/a", "github:me/a"}
    assert calls["saved"], "首次回源结果应落库"
    assert sorted(calls["saved"][-1]["providers"]) == PROVIDERS


def test_member_background_failure_keeps_stale_cache(monkeypatch, caplog):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "old"), _repo("github", "old")]

    def _fetch(user, provider):
        raise RuntimeError("network down")

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)

    result = repos_route.list_member_repos(USER, provider="", refresh=0)
    assert result["_cached"] is True
    assert result["_stale"] is True

    _wait_refresh()
    assert calls["saved"] == [], "后台全部失败时不得写库（避免 synced_at 变新鲜）"
    assert "keep stale cache" in caplog.text


def test_member_background_partial_failure_keeps_failed_provider_cache(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "old"), _repo("github", "old")]

    def _fetch(user, provider):
        if provider == "github":
            raise RuntimeError("github down")
        return [_repo("gitcode", "new")]

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)

    result = repos_route.list_member_repos(USER, provider="", refresh=0)
    assert result["_cached"] is True

    _wait_refresh()
    assert calls["saved"], "部分成功也应落库"
    saved = calls["saved"][-1]
    assert saved["providers"] == ["gitcode"]
    keys = {r["repo_key"] for r in saved["repos"]}
    assert "gitcode:me/new" in keys
    assert "github:me/old" in keys, "失败平台旧缓存必须保留"


def test_member_background_non_401_failure_still_saves_healthy_provider(monkeypatch):
    """单个平台非 401 硬错误不得冻结全平台：健康平台照常落库，失败平台保留旧缓存。"""
    from fastapi import HTTPException

    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "old"), _repo("github", "old")]

    def _fetch(user, provider):
        if provider == "github":
            raise HTTPException(status_code=500, detail="github internal error")
        return [_repo("gitcode", "new")]

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)

    result = repos_route.list_member_repos(USER, provider="", refresh=0)
    assert result["_cached"] is True

    _wait_refresh()
    assert calls["saved"], "非 401 单平台失败不应放弃落库健康平台"
    saved = calls["saved"][-1]
    assert saved["providers"] == ["gitcode"]
    keys = {r["repo_key"] for r in saved["repos"]}
    assert "gitcode:me/new" in keys
    assert "github:me/old" in keys, "失败平台旧缓存必须保留"


def test_member_refresh_deadline_treats_hung_provider_as_failed(monkeypatch):
    """整轮回源超时：挂起平台按失败处理，事件释放、健康平台正常落库。"""
    from src.domains.mgmt.routes import repos as repos_route

    gate = threading.Event()
    cached = [_repo("gitcode", "old"), _repo("github", "old")]

    def _fetch(user, provider):
        if provider == "github":
            gate.wait(10)
            return [_repo("github", "late")]
        return [_repo("gitcode", "new")]

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)
    monkeypatch.setattr(repos_route, "MEMBER_REFRESH_DEADLINE", 0.2)

    try:
        result = repos_route.list_member_repos(USER, provider="", refresh=0)
        assert result["_cached"] is True

        _wait_refresh(timeout=3.0)
        assert calls["saved"], "超时平台按失败处理后仍应落库健康平台"
        saved = calls["saved"][-1]
        assert saved["providers"] == ["gitcode"]
        keys = {r["repo_key"] for r in saved["repos"]}
        assert "gitcode:me/new" in keys
        assert "github:me/old" in keys, "超时平台旧缓存必须保留"
    finally:
        gate.set()


def test_member_singleflight_deduplicates_refresh(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [_repo("gitcode", "old")]
    gate = threading.Event()

    def _fetch(user, provider):
        gate.wait(5)
        return [_repo(provider, "new")]

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)

    for _ in range(5):
        result = repos_route.list_member_repos(USER, provider="", refresh=0)
        assert result["_cached"] is True

    gate.set()
    _wait_refresh()
    assert sorted(calls["fetch"]) == PROVIDERS, f"singleflight 失效：{calls['fetch']}"
    assert len(calls["saved"]) == 1


def test_member_refresh_deadline_env_parsing_boundaries():
    """MEMBER_REFRESH_DEADLINE env 解析边界：空/非法回退 20s，<=0 = None（无 deadline）。"""
    from src.domains.mgmt.routes import repos as repos_route

    parse = repos_route._parse_member_refresh_deadline
    default = repos_route._MEMBER_REFRESH_DEADLINE_DEFAULT
    assert parse("") == default
    assert parse("   ") == default
    assert parse(None) == default
    assert parse("abc") == default
    assert parse("1.5x") == default
    assert parse("20") == 20.0
    assert parse("  5 ") == 5.0
    assert parse("0") is None
    assert parse("-3") is None
    assert parse("0.0") is None


def test_member_deadline_all_providers_timeout_skips_save(monkeypatch):
    """整轮回源全平台超时 → 按全失败处理：不写库（synced_at 不提前变新鲜），保留旧缓存。"""
    from src.domains.mgmt.routes import repos as repos_route

    gate = threading.Event()
    cached = [_repo("gitcode", "old"), _repo("github", "old")]

    def _fetch(user, provider):
        gate.wait(10)
        return [_repo(provider, "late")]

    calls = _install_route_stubs(monkeypatch, cached, _old_ts(), _fetch)
    monkeypatch.setattr(repos_route, "MEMBER_REFRESH_DEADLINE", 0.2)

    try:
        result = repos_route.list_member_repos(USER, provider="", refresh=0)
        assert result["_cached"] is True
        assert result["_stale"] is True

        _wait_refresh(timeout=3.0)
        assert calls["saved"] == [], "全平台超时不得写库（避免 synced_at 变新鲜）"
    finally:
        gate.set()


def test_member_first_load_non_401_error_raises(monkeypatch):
    """首次无缓存回源遇到非 401 HTTPException → 保持原语义直接抛出（不吞成空列表）。"""
    from fastapi import HTTPException

    from src.domains.mgmt.routes import repos as repos_route

    def _fetch(user, provider):
        raise HTTPException(status_code=500, detail="provider internal error")

    calls = _install_route_stubs(monkeypatch, [], None, _fetch)

    with pytest.raises(HTTPException) as exc_info:
        repos_route.list_member_repos(USER, provider="", refresh=0)
    assert exc_info.value.status_code == 500
    assert calls["saved"] == []