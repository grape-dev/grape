"""`db_registry._CompatCursor` 必须返回游标自身（sqlite3 语义）。

回归背景（Issue：三库 lib 模式后台循环全灭）：域连接（自有 DSN）走的是
`_CompatConn`/`_CompatCursor`，而 `execute()` 原先直接把 pymysql 的返回值
（受影响行数 int）透传出去。调用点普遍写成 `c.execute(sql).fetchone()`
（例如 `src/domains/runtime/public.acquire_distributed_lock` 的锁协议第一步），
于是三库拓扑下每轮都抛 `AttributeError: 'int' object has no attribute 'fetchone'`，
skill sync / feedback reconcile / feedback status sync / table maintenance
四个后台循环全部失败，且只在分库部署暴露（单库走 MySQLCompatCursor，本来返回 self）。

约定（与 `mysql_compat.MySQLCompatCursor` 对齐）：
- `execute()` / `executemany()` → 返回游标自身；
- 需要行数的调用方读 `c.rowcount`（`exec_any` 就是这么写的）。
"""
from src.kernel import db_registry

_EXPECTED_ROW = ("holder", 123)


class _RawCursor:
    """最小 pymysql 游标替身：execute 返回 int（这正是坑的来源）。"""

    def __init__(self):
        self.rowcount = 1
        self.lastrowid = 7
        self.description = ()

    def execute(self, sql, params=None):  # noqa: ARG002 - 故意返回 int
        return self.rowcount

    def executemany(self, sql, seq):
        return len(list(seq))

    def fetchone(self):
        return _EXPECTED_ROW

    def fetchall(self):
        return [_EXPECTED_ROW]

    def close(self):
        pass


class _RawConn:
    def __init__(self):
        self.cur = _RawCursor()

    def cursor(self, *a, **kw):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _compat_cursor():
    return db_registry._CompatConn(_RawConn()).cursor()


def test_execute_returns_cursor_self():
    c = _compat_cursor()
    assert c.execute("SELECT 1") is c


def test_execute_then_fetchone_works():
    """锁协议的真实写法：`c.execute(...).fetchone()` 不再 AttributeError。"""
    c = _compat_cursor()
    row = c.execute("SELECT holder, expires_at FROM distributed_locks WHERE lock_key=?", ("k",)).fetchone()
    assert row == _EXPECTED_ROW


def test_executemany_returns_cursor_self():
    c = _compat_cursor()
    assert c.executemany("INSERT INTO t (a) VALUES (?)", [(1,), (2,)]) is c


def test_rowcount_still_available():
    """返回 self 不影响行数读取（exec_any / 各处 rowcount 判断）。"""
    c = _compat_cursor()
    c.execute("UPDATE t SET a=1 WHERE id=2")
    assert c.rowcount == 1


def test_mysql_dialect_translation_preserved():
    """方言翻译（? → %s / INSERT OR IGNORE）仍走 translate_sql。"""
    from src.kernel.mysql_compat import translate_sql

    c = _compat_cursor()
    assert c.execute("SELECT * FROM t WHERE a=? AND b=?", (1, 2)) is c
    assert "%s" in (translate_sql("SELECT * FROM t WHERE a=?") or "")