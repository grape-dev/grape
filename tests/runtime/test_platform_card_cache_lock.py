"""平台卡缓存锁序守卫（2026-11-19 ABBA 死锁回归）。

背景：`_load_cards` 原先在 `_cache_lock` 内部读库（meta 配置 / repo_platforms 表），
而读库路径会拿 `session_manager.db_lock`；`mgmt.routes.scenes._resolve_repo_key`
又在持 db_lock 时回调 `list_vcs_platforms()` → `_load_cards()` 要 `_cache_lock`。
两把锁反序 → 整个进程事件循环卡死（当日两次全站不可用，thread dump 实证：
一边卡 platform_card._load_cards 等 _cache_lock，另一边卡 runtime.public._any_cursor 等 db_lock）。

不变量：**构建平台卡期间不得持有 `_cache_lock`**——锁只在「换缓存」那一瞬持有，
期间只做 dict 操作，绝不碰 DB。
"""
import threading

import pytest

import src.domains.runtime.vcs.platform_card as pc


@pytest.fixture(autouse=True)
def _restore_cache_state():
    """用例会强制重建缓存，结束后还原，避免污染同批其它平台卡用例。"""
    snap = dict(pc._CACHE)
    loaded, ts = pc._loaded, pc._cache_ts
    yield
    pc._CACHE.clear()
    pc._CACHE.update(snap)
    pc._loaded = loaded
    pc._cache_ts = ts


def test_build_runs_without_holding_the_cache_lock(monkeypatch):
    """构建阶段若仍持有 _cache_lock，这里就探测得到（回退成死锁的那一版必红）。"""
    seen = {}

    def fake_build():
        got = pc._cache_lock.acquire(blocking=False)
        seen["cache_lock_held_during_build"] = not got
        if got:
            pc._cache_lock.release()
        return {}

    monkeypatch.setattr(pc, "_build_cards", fake_build)
    monkeypatch.setattr(pc, "_loaded", False)
    pc._load_cards()

    assert seen["cache_lock_held_during_build"] is False


def test_cache_still_swapped_and_ttl_honoured(monkeypatch):
    """拆锁不能把「换缓存 + TTL」拆坏：首次构建后写入，TTL 内不再重建。"""
    calls = {"n": 0}

    def fake_build():
        calls["n"] += 1
        return {"demo": "card"}

    monkeypatch.setattr(pc, "_build_cards", fake_build)
    monkeypatch.setattr(pc, "_loaded", False)
    pc._CACHE.clear()

    pc._load_cards()
    assert pc._CACHE.get("demo") == "card"
    assert pc._loaded is True

    pc._load_cards()  # TTL 内
    assert calls["n"] == 1


def test_build_and_db_lock_holder_do_not_deadlock(monkeypatch):
    """持 db_lock 的线程与触发重建的线程并发，两边都必须在超时内返回。"""
    from src.kernel.session_manager import db_lock

    monkeypatch.setattr(pc, "_build_cards", lambda: {})
    monkeypatch.setattr(pc, "_loaded", False)
    pc._CACHE.clear()

    def holder():
        with db_lock:
            # 真实调用方就是这样：持 db_lock 时读平台卡
            pc.list_vcs_platforms()

    t1 = threading.Thread(target=holder, daemon=True)
    t2 = threading.Thread(target=pc.list_vcs_platforms, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive(), "平台卡加载与 db_lock 持有者并发时死锁"