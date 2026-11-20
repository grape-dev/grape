# -*- coding: utf-8 -*-
"""中间件 meta URL 缓存回归（压测下 UI 卡顿）。

背景：`request_id_and_log_middleware` 对每个带登录态的请求同步查 2~3 次 meta
（webhook_url / server_base_url），这些同步查询跑在事件循环上并共用进程级
db_lock，压测时把整个进程串行化（实测 8 并发带 token 的 /api/health 8.2s）。

契约：
1. 缓存有效期内（默认 60s）只读一次库；
2. 超过 TTL 才重新读库；
3. TTL 可通过 GRAPE_META_URL_CACHE_TTL_S 覆盖。
"""
import asyncio
import importlib

import pytest


def _load_middleware(monkeypatch, ttl="60"):
    monkeypatch.setenv("GRAPE_META_URL_CACHE_TTL_S", ttl)
    import src.api.middleware as mw

    mw = importlib.reload(mw)
    mw._reset_meta_url_cache()
    return mw


def test_cached_meta_urls_hit_db_once_within_ttl(monkeypatch):
    mw = _load_middleware(monkeypatch)
    calls = []

    def _fake_read():
        calls.append(1)
        return ("https://grape.example/webhook/gitcode", "https://grape.example")

    monkeypatch.setattr(mw, "_read_meta_urls_sync", _fake_read)

    async def _run():
        a = await mw._meta_urls_cached()
        b = await mw._meta_urls_cached()
        c = await mw._meta_urls_cached()
        return a, b, c

    a, b, c = asyncio.run(_run())
    assert a == b == c == ("https://grape.example/webhook/gitcode", "https://grape.example")
    assert len(calls) == 1, f"TTL 内应只查一次库，实际 {len(calls)} 次"


def test_cache_expires_after_ttl(monkeypatch):
    mw = _load_middleware(monkeypatch, ttl="0.01")  # 10ms
    calls = []

    def _fake_read():
        calls.append(1)
        return ("", "")

    monkeypatch.setattr(mw, "_read_meta_urls_sync", _fake_read)

    async def _run():
        await mw._meta_urls_cached()
        await asyncio.sleep(0.05)
        await mw._meta_urls_cached()

    asyncio.run(_run())
    assert len(calls) == 2, f"TTL 过期后应重查，实际 {len(calls)} 次"


def test_reset_forces_reread(monkeypatch):
    mw = _load_middleware(monkeypatch)
    calls = []

    def _fake_read():
        calls.append(1)
        return ("x", "y")

    monkeypatch.setattr(mw, "_read_meta_urls_sync", _fake_read)

    async def _run():
        await mw._meta_urls_cached()
        mw._reset_meta_url_cache()
        await mw._meta_urls_cached()

    asyncio.run(_run())
    assert len(calls) == 2


def test_middleware_has_no_sync_meta_lookup_in_hot_path():
    """静态守护：自愈探测不得再直接调用同步 get_meta_value。"""
    import inspect

    import src.api.middleware as mw

    src = inspect.getsource(mw.install_request_middleware)
    assert "get_meta_value(" not in src, "中间件热路径仍有同步 get_meta_value 调用"
    assert "_meta_urls_cached" in src, "中间件未走缓存读"
    assert "to_thread(validate_token" in src or "asyncio.to_thread(validate_token" in src
