"""Issue #3097 / #3054 QA：BusinessDataProbe 的「缺表」与「读不成」必须分开。

定稿语义：
- 缺表（MySQL 1146 / SQLite no such table）= 无数据（三库拓扑正常态）；
- 表存在但读不成（连接/超时/权限/通道不可用）= 不可判定 → 保守当「有数据」，
  不自动合并（一次抖动不得把有数据的占位账号判成无数据并自动合并）。
"""
from contextlib import contextmanager

import pytest

from src.domains.mgmt.auth import business_data as bd
from src.domains.mgmt.auth.business_data import BusinessDataProbe


def _cursor_factory(exc=None, count=0):
    @contextmanager
    def _cm():
        class _C:
            def execute(self, sql, params=()):
                if exc is not None:
                    raise exc
                class _R:
                    def fetchone(self_inner):
                        return (count,)
                return _R()
        yield _C()
    return _cm


def _probe(exc=None, count=0, runtime=None, warnings=None):
    return BusinessDataProbe(
        cursor_factory=_cursor_factory(exc, count),
        runtime_counts=(lambda uid: runtime) if runtime is not None else (lambda uid: {}),
        warn_sink=(warnings.append if warnings is not None else (lambda p: None)),
    )


def test_no_data_when_all_counts_zero():
    v = _probe(count=0).probe("u1")
    assert v.has_data is False and v.indeterminate is False and v.reason == "no-data"


def test_has_data_when_any_count_positive():
    v = _probe(count=1).probe("u1")
    assert v.has_data is True and v.indeterminate is False and v.reason == "has-data"


def test_missing_table_counts_as_no_data():
    """缺表 = 拓扑正常态 → 无数据（不能因此让占位账号合并永久失效）。"""
    warnings = []
    v = _probe(exc=RuntimeError("no such table: user_feedback"), warnings=warnings).probe("u1")
    assert v.has_data is False
    assert v.indeterminate is False
    assert any(w["action"] == "missing-table->no-data" for w in warnings)


def test_transient_read_error_is_indeterminate_not_no_data():
    """连接/超时类错误 → 不可判定 → 保守当有数据（不自动合并）。"""
    warnings = []
    v = _probe(exc=RuntimeError("connection refused (2003)"), warnings=warnings).probe("u1")
    assert v.has_data is True
    assert v.indeterminate is True
    assert v.reason == "indeterminate"
    assert any(w["action"] == "unreadable->indeterminate" for w in warnings)


def test_runtime_channel_failure_is_indeterminate():
    """runtime 契约通道整体不可用（跨进程抖动）→ 不可判定，不得判无数据。"""
    def boom(uid):
        raise RuntimeError("runtime service unavailable")

    v = BusinessDataProbe(cursor_factory=_cursor_factory(None, 0),
                          runtime_counts=boom,
                          warn_sink=lambda p: None).probe("u1")
    assert v.has_data is True and v.indeterminate is True


def test_runtime_single_table_missing_vs_unreadable():
    ok_missing = _probe(count=0, runtime={
        "agent_tasks": {"count": None, "missing_table": True, "error": "1146"},
    }).probe("u1")
    assert ok_missing.has_data is False and ok_missing.indeterminate is False

    transient = _probe(count=0, runtime={
        "agent_tasks": {"count": None, "missing_table": False, "error": "timeout"},
    }).probe("u1")
    assert transient.has_data is True and transient.indeterminate is True
