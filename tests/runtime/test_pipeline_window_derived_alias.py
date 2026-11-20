# -*- coding: utf-8 -*-
"""流水线窗口下推在 MySQL 上不得 1248（派生表缺别名 → fallback 全仓 → 4s）。

现象：/api/pipeline/{owner}/{repo} 每次 3.8~4.5s；日志 60 次
``pipeline window pushdown failed, fallback to full load:
(1248, 'Every derived table must have its own alias')``
——`_pipeline_window` 的 COUNT 子查询没给派生表起别名，MySQL 直接拒绝，
调用方 except 捕获后 `_pushdown_ok=False`，退化成全仓加载
（该仓 6381 行 events、1611 个 issue 逐条构建流水线）。
"""
import sqlite3

import pytest


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, issue_id TEXT,
        event_type TEXT, agent_type TEXT, status TEXT, scene_id TEXT)""")
    conn.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
        [(1, "gitcode:o/r", "#1", "issue_opened", "vod", "completed", ""),
         (2, "gitcode:o/r", "#2", "issue_opened", "vod", "failed", ""),
         (3, "gitcode:o/r", "#3", "issue_opened", "qa", "completed", ""),
         (4, "gitcode:o/r", "#4", "admin_action", "sre", "completed", "")])
    conn.commit()
    return conn


def test_pipeline_window_count_sql_has_derived_table_alias():
    """COUNT 子查询必须带别名（MySQL 1248 守护）。"""
    import inspect
    import re

    from src.domains.runtime.routes import pipeline as pl

    src = inspect.getsource(pl._pipeline_window)
    # 派生表必须收口为 ") AS total_window"（MySQL 1248 守护）
    assert "GROUP BY e.issue_id) AS total_window" in src or re.search(
        r"GROUP BY e\.issue_id\)\s+AS\s+\w+", src), (
        "COUNT 派生表缺别名 → MySQL 1248 → fallback 全仓加载（4s）")


def test_pipeline_window_returns_without_exception(monkeypatch):
    from src.domains.runtime.routes import pipeline as pl

    conn = _make_db()

    class _Cur:
        def __init__(self, c): self._c = c
        def execute(self, sql, params=()):
            return self._c.execute(sql, params)
        def fetchall(self): return self._c.fetchall()
        def fetchone(self): return self._c.fetchone()

    class _Db:
        def __init__(self, conn): self.conn = conn
        def cursor(self): return _Cur(self.conn.cursor())
        def commit(self): self.conn.commit()

    monkeypatch.setattr(pl, "_rt_db", _Db(conn))
    monkeypatch.setattr("src.kernel.session_manager.db_lock",
                        __import__("contextlib").nullcontext())

    win, total, failed = pl._pipeline_window(["gitcode:o/r"], "", 10)
    assert sorted(win) == ["#1", "#2", "#3"], f"窗口应含 3 个非审计 issue: {win}"
    assert total == 3
    assert "#2" in failed
