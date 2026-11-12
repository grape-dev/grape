"""巡检测试共享 fake DB（Issue #2050 审查：复用而非各自复制）。

``test_patrol_unit.py`` 与 ``test_issue_2050_structured_handoff_stall.py``
共用同一份脚本化 cursor 替身，避免假库实现漂移（如未定义的
``self._last_sql`` 死代码）。
"""
from __future__ import annotations


class _FakeCursor:
    def __init__(self, fake_db):
        self._db = fake_db
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self._db.executed.append((sql, tuple(params) if not isinstance(params, tuple) else params))
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = self._db._take(sql, params)
        else:                       # INSERT/UPDATE/DELETE：只记 rowcount
            rc = self._db._rowcounts
            self.rowcount = rc.pop(0) if rc else 1
        return self                 # 支持链式 execute().fetchall()

    def executemany(self, sql, seq_of_params=()):
        params = tuple(seq_of_params)
        self._db.executed.append((sql, params))
        rc = self._db._rowcounts
        self.rowcount = rc.pop(0) if rc else len(params)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDb:
    """脚本化 db 替身：[(sql 片段, rows), ...] 按序消费；未命中 SELECT 返回空。"""

    def __init__(self, results=(), rowcounts=()):
        self._results = [(frag, rows) for frag, rows in results]
        self._rowcounts = list(rowcounts)   # 非 SELECT 语句逐次取值（耗尽回退 1）
        self.cursors = []
        self.executed = []                   # [(sql, params)] 审计轨迹（S34 断言用）
        self.commits = 0                     # S48：commit 计数（批量写对账）

    def cursor(self):
        c = _FakeCursor(self)
        self.cursors.append(c)
        return c

    def commit(self):
        self.commits += 1

    def _take(self, sql, params=()):
        # 首个未消费过标记的匹配片段（同片段可重复命中——同一脚本行服务多轮调用，
        # 如 confirm 窗收紧前后的两轮 detect）
        for i, (frag, rows) in enumerate(self._results):
            if frag in sql:
                # S49 keyset 分页感知：候选分页查询（id > ? ORDER BY id LIMIT ?）
                # 按游标过滤脚本行——否则同一脚本行被每页重放，分页循环不终止
                if "ORDER BY id" in sql and "id > ?" in sql and len(params) >= 2:
                    cursor = params[-2]
                    page = [r for r in rows if str(r[0]) > str(cursor)]
                    return page[:params[-1]]      # LIMIT 同步感知（真库语义）
                return rows
        return []
