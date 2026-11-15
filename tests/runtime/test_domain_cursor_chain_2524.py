# -*- coding: utf-8 -*-
"""三库拓扑下兼容游标的 execute 返回 rowcount(int)，不是游标本身。

`db_registry._CompatCursor.execute` 直接返回 pymysql 的 rowcount，而进程主库用的
`MySQLCompatCursor.execute` 返回 self。lib 原语按后者写成链式
（`c.execute(...).fetchone()` / `.description`），在三库三机部署下走域连接时必炸：

- `[patrol] leader claim failed: 'int' object has no attribute 'fetchone'`（巡检永远抢不到主，自动检测轮不跑）
- `obs rollup: cascade compensation agg failed: 'int' object has no attribute 'description'`（补偿指标桶缺失）
"""


class _RowcountCursor:
    """模拟 _CompatCursor：execute 返回 int，cur.description / fetchone 走游标自身。"""

    def __init__(self, row=None):
        self._row = row
        self.description = [("report_missing",)]
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return 1  # rowcount，不是游标

    def fetchone(self):
        return self._row


def test_table_has_column_tolerates_rowcount_return(monkeypatch):
    from src.domains.runtime import public as p

    c = _RowcountCursor()
    assert p._table_has_column(c, "events", "report_missing") is True
    assert p._table_has_column(c, "events", "no_such_col") is False


def test_claim_meta_lease_reads_through_cursor(monkeypatch):
    """CAS 读现值这一步不能链式取 fetchone，否则巡检主选举抛异常恒 False。"""
    from contextlib import contextmanager
    from src.domains.runtime import public as p

    calls = []

    class _C(_RowcountCursor):
        def execute(self, sql, params=None):
            calls.append(sql.strip())
            self.description = [("value",)]
            return 1

        def fetchone(self):
            return ("someone_else|2026-11-15T12:00:00+00:00",)

    @contextmanager
    def _fake_tx():
        yield _C()

    monkeypatch.setattr(p, "_any_tx", _fake_tx)
    # 现值新鲜且非本主 → 让位返回 False；关键是不抛异常
    assert p.claim_meta_lease("patrol_leader", "me", "me|2026-11-15T12:10:00+00:00",
                              "2026-11-15T12:05:00+00:00") is False
    assert any("SELECT value FROM meta" in s for s in calls)