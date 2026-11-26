"""#4353「拒绝降级」状态机：TTL 滑出与容量淘汰回归（PR #2245 复审 B2 条件）。

背景：`_DEGRADE_REFUSED` 是进程内台账，正常路径由 `clear_worktree_failure` 清除；
异常路径（worker 崩溃、会话未走到清理）只能靠 TTL 与容量淘汰兜底。这两个兜底一旦
失效，#4353 的「冲突 → 拒绝降级、不标只读」互斥保证会静默退化成「上一轮冲突把下一轮
合法只读会话误判成冲突」。本文件把两条兜底固化为可复跑断言。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.engine import repogit  # noqa: E402

RK = "gitcode:issue4353/state"


@pytest.fixture(autouse=True)
def _clean_state():
    repogit._DEGRADE_REFUSED.clear()
    yield
    repogit._DEGRADE_REFUSED.clear()


def test_state_is_visible_after_mark():
    repogit.mark_degrade_refused(RK, 7, "coding", reason="occupied", code="worktree_conflict")
    hit = repogit.degrade_refused(RK, 7, "coding")
    assert hit["code"] == "worktree_conflict"
    assert hit["reason"] == "occupied"
    assert hit["at"] > 0


def test_state_expires_after_ttl(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(repogit.time, "time", lambda: now)
    repogit.mark_degrade_refused(RK, 7, "coding")
    assert repogit.degrade_refused(RK, 7, "coding"), "刚登记应可见"

    # TTL 之内仍可见（边界内侧）
    now += repogit._DEGRADE_REFUSED_TTL - 1
    monkeypatch.setattr(repogit.time, "time", lambda: now)
    assert repogit.degrade_refused(RK, 7, "coding"), "TTL 窗口内不应过期"

    # 越过 TTL → 过期并被就地清除，不再影响下一轮
    now += 2
    monkeypatch.setattr(repogit.time, "time", lambda: now)
    assert repogit.degrade_refused(RK, 7, "coding") == {}
    assert repogit._failure_key(RK, 7, "coding") not in repogit._DEGRADE_REFUSED


def test_capacity_evicts_oldest_and_keeps_active(monkeypatch):
    now = 2_000_000.0
    monkeypatch.setattr(repogit.time, "time", lambda: now)
    cap = repogit._DEGRADE_REFUSED_MAX

    for i in range(cap):
        now += 1
        monkeypatch.setattr(repogit.time, "time", lambda: now)
        repogit.mark_degrade_refused(RK, 1000 + i, "coding")
    assert len(repogit._DEGRADE_REFUSED) == cap

    # 再登记一条 → 总数为 cap（不超过上限），最旧的一条被淘汰
    now += 1
    monkeypatch.setattr(repogit.time, "time", lambda: now)
    repogit.mark_degrade_refused(RK, 9999, "coding")
    assert len(repogit._DEGRADE_REFUSED) <= cap
    assert repogit.degrade_refused(RK, 1000, "coding") == {}, "最旧记录应被淘汰"
    assert repogit.degrade_refused(RK, 9999, "coding"), "最新记录必须保留"
    # 中间与末尾仍在窗口内
    assert repogit.degrade_refused(RK, 1000 + cap - 1, "coding")


def test_clear_only_drops_target_triple(monkeypatch):
    now = 3_000_000.0
    monkeypatch.setattr(repogit.time, "time", lambda: now)
    repogit.mark_degrade_refused(RK, 1, "coding")
    repogit.mark_degrade_refused(RK, 2, "coding")
    repogit.clear_worktree_failure(RK, 1, "coding")
    assert repogit.degrade_refused(RK, 1, "coding") == {}
    assert repogit.degrade_refused(RK, 2, "coding"), "并发任务的记录不得被连带清除"