"""迁移 202611220001（chat_conversations 状态索引）的 MySQL 分支回归。

背景：SQLite 单测覆盖不到这条分支——`_mysql_backend()` 为假时走的是
`CREATE INDEX IF NOT EXISTS`，而该形式在 MySQL 上直接 1064。修掉 1064 之后裸写
`CREATE INDEX ... (status)` 又会踩另一个坑：`status` 在建表 DDL 里是 TEXT，MySQL 的
TEXT 列做索引键必须带前缀长度，否则 1170；仓库里 `mysql_compat` 的自动补 (191) 特判
只匹配 `IF NOT EXISTS` 形式，裸语句在会话游标与域连接游标上都原样透传。

所以这里直接对着 MySQL 分支做断言：语句里必须出现 `status(191)`，且 1061（索引已存在）
视为幂等成功、不抛。
"""
import pytest

from src.api import db_setup


class _FakeCursor:
    """只回答两件事：表存在 + 是基表；其余语句原样记账。"""

    def __init__(self, create_error: str = ""):
        self.statements = []
        self._create_error = create_error

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.strip().upper().startswith("CREATE INDEX") and self._create_error:
            raise RuntimeError(self._create_error)
        return self

    def fetchone(self):
        sql = self.statements[-1][0] if self.statements else ""
        if "information_schema.tables" in sql:
            return ("BASE TABLE",)
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    def created(self):
        return [s for s, _ in self.statements if s.strip().upper().startswith("CREATE INDEX")]


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1


@pytest.fixture
def mysql_branch(monkeypatch):
    def _install(create_error: str = ""):
        cursor = _FakeCursor(create_error=create_error)
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        monkeypatch.setattr(db_setup, "_migration_conn",
                            lambda domain, default=None: conn)
        return conn, cursor

    return _install


def test_mysql_index_uses_text_prefix_length(mysql_branch):
    conn, cursor = mysql_branch()
    db_setup._fn_chat_conversations_status_index()

    created = cursor.created()
    assert created, cursor.statements
    sql = created[0]
    # TEXT 列必须带前缀，否则 MySQL 报 1170
    assert "(status(191))" in sql, sql
    assert "IF NOT EXISTS" not in sql.upper(), sql
    assert conn.committed >= 1


def test_mysql_existing_index_is_idempotent(mysql_branch):
    """1061（Duplicate key name）是「索引已存在」，迁移应正常返回并盖章。"""
    _conn, cursor = mysql_branch(create_error="(1061, \"Duplicate key name 'idx_chat_conv_status'\")")
    db_setup._fn_chat_conversations_status_index()  # 不应抛
    assert cursor.created()


def test_mysql_other_error_still_raises(mysql_branch):
    """1170 之类的真实错误不能被 1061 幂等分支吞掉。"""
    _conn, _cursor = mysql_branch(create_error="(1170, \"BLOB/TEXT column used in key spec\")")
    with pytest.raises(RuntimeError):
        db_setup._fn_chat_conversations_status_index()
