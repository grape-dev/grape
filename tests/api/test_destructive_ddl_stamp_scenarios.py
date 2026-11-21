"""破坏性 DDL 的“盖章”纪律 S1–S4（评审④ 假绿）。

约定：迁移 fn 正常返回 = 执行器盖章。因此 DDL 真失败必须抛出，
否则审计写成 phase=after、下次启动不再重试（session_key 列宽原事故形态）。
"""
from __future__ import annotations

import pytest

from src.api import db_setup as ds


class _Cursor:
    def __init__(self, exc):
        self.exc = exc

    def execute(self, ddl, *a, **k):
        if self.exc:
            raise self.exc


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setattr(ds, "_mysql_backend", lambda: False)   # 跳过版本门槛
    monkeypatch.setattr(ds, "_is_mdl_timeout", lambda e: False)


def test_s1_generic_failure_raises_so_it_is_not_stamped():
    with pytest.raises(Exception) as ei:
        ds._destructive_ddl(_Cursor(RuntimeError("boom")), "ALTER TABLE t DROP COLUMN c",
                            kind_hint="cleanup")
    assert "boom" in str(ei.value)


def test_s2_already_in_target_state_is_success():
    err = Exception("no such column")
    err.args = (1091, "no such column")
    assert ds._destructive_ddl(_Cursor(err), "ALTER TABLE t DROP COLUMN c",
                               kind_hint="cleanup") is True


def test_s3_gate_not_passed_returns_false_without_running():
    import src.api.db_setup as d
    c = _Cursor(None)
    orig = d._mysql_backend
    d._mysql_backend = lambda: True
    d._cleanup_gate_passed = lambda: False
    try:
        assert d._destructive_ddl(c, "DROP TABLE old", kind_hint="cleanup") is False
    finally:
        d._mysql_backend = orig
        d._cleanup_gate_passed = orig if hasattr(d, "_cleanup_gate_passed") else None


def test_s4_kind_hint_is_enforced():
    with pytest.raises(ValueError):
        ds._destructive_ddl(_Cursor(None), "DROP TABLE x", kind_hint="alter")
