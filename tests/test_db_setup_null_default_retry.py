# -*- coding: utf-8 -*-
"""Issue #1546 修复回归：带 DEFAULT NULL 的 TEXT 列在 MySQL 上无法 INSTANT/INPLACE。

原 _ensure_column 两级降级失败后不抛异常、只记人工清单，迁移仍会盖章 → 列永不创建。
本测试锁定修复后的行为：
1. DEFAULT NULL 被剥离后重试，仍走 INSTANT（不升级到 COPY 锁表）；
2. 非 NULL 默认值（0/'' 等）不被剥离，语义不变；
3. 剥离后仍失败时保留人工清单记录。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.runtime  # noqa: F401  规避环形导入
from src.api import db_setup as d


class _FakeCur:
    """只允许 PRAGMA；ALTER 按 reject 规则抛错，模拟 MySQL 拒绝 INSTANT/INPLACE。"""

    def __init__(self, reject=None):
        self.sqls = []
        self._reject = reject or (lambda sql: True)

    def execute(self, sql, *args):
        self.sqls.append(sql)
        if sql.startswith("PRAGMA"):
            return self
        if self._reject(sql):
            raise Exception("1064 (42000): unsupported for this column type")
        return self

    def fetchall(self):
        return []

    def alters(self):
        return [s for s in self.sqls if not s.startswith("PRAGMA")]


def test_strip_null_default_only_touches_null():
    assert d._strip_null_default("TEXT DEFAULT NULL") == "TEXT"
    assert d._strip_null_default("MEDIUMTEXT DEFAULT null") == "MEDIUMTEXT"
    assert d._strip_null_default("TEXT") == "TEXT"
    assert d._strip_null_default("INTEGER DEFAULT 0") == "INTEGER DEFAULT 0"
    assert d._strip_null_default("VARCHAR(64) NOT NULL DEFAULT ''") == "VARCHAR(64) NOT NULL DEFAULT ''"


def test_default_null_retried_without_default():
    orig = d._mysql_backend
    d._mysql_backend = lambda: True
    try:
        cur = _FakeCur(reject=lambda sql: "DEFAULT NULL" in sql)
        d._ensure_column(cur, "events", "idempotency_key", "TEXT DEFAULT NULL")
    finally:
        d._mysql_backend = orig
    assert cur.alters()[-1].endswith("TEXT, ALGORITHM=INSTANT")
    assert all("DEFAULT NULL" not in sql for sql in cur.alters()[-1:])


def test_non_null_default_kept_and_reported():
    orig = d._mysql_backend
    d._mysql_backend = lambda: True
    d._DDL_MANUAL_REVIEW.clear()
    try:
        cur = _FakeCur()  # 全部 ALTER 都拒绝，且不允许剥离
        d._ensure_column(cur, "t_demo", "col_x", "INTEGER NOT NULL DEFAULT 0")
    finally:
        d._mysql_backend = orig
    assert len(cur.alters()) == 2, "非 NULL 默认值不得触发剥离重试"
    assert any("t_demo.col_x" in e for e in d._DDL_MANUAL_REVIEW)
    d._DDL_MANUAL_REVIEW.clear()