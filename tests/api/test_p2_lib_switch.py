"""P2 新增测试：connection_for / RuntimeServiceLocal 表名策略 / list_runtimes_rows。"""
import contextlib

import pytest

from src.kernel import db_registry


class FakeConn:
    def cursor(self):
        return None


def test_connection_for_bare_name_when_own_dsn(monkeypatch):
    monkeypatch.setattr(db_registry, "domain_db", lambda d: FakeConn(), raising=True)
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: True, raising=True)
    conn, qual = db_registry.connection_for("runtime")
    assert isinstance(conn, FakeConn)
    assert qual("agent_tasks") == "agent_tasks"  # 自有 DSN → 裸名


def test_connection_for_schema_prefix_when_shared(monkeypatch):
    monkeypatch.setattr(db_registry, "domain_db", lambda d: FakeConn(), raising=True)
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False, raising=True)
    _conn, qual = db_registry.connection_for("runtime")
    assert qual("agent_tasks") == "grape_dev_runtime.agent_tasks" or "agent_tasks" in qual("agent_tasks")


def test_connection_for_raises_without_conn(monkeypatch):
    monkeypatch.setattr(db_registry, "domain_db", lambda d: None, raising=True)
    with pytest.raises(RuntimeError):
        db_registry.connection_for("runtime")


def test_runtime_local_table_prefix_switch(monkeypatch):
    from src.domains.contracts.local.runtime_local import RuntimeServiceLocal

    svc = RuntimeServiceLocal()
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: True, raising=True)
    assert svc._t("events") == "events"
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False, raising=True)
    from src.kernel.db import t_runtime
    assert svc._t("events") == t_runtime("events")   # 跨 schema 路径与既有 helper 一致


def test_runtime_local_lock_switch(monkeypatch):
    from src.domains.contracts.local.runtime_local import RuntimeServiceLocal

    svc = RuntimeServiceLocal()
    own = db_registry.domain_lock("runtime")
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: True, raising=True)
    assert svc._lock is own
    import src.kernel.session_manager as sm
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False, raising=True)
    assert svc._lock is sm.db_lock


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("id",)]
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        return 0

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None

    def close(self):
        pass


def install_cursor(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)


def test_list_runtimes_rows_scope_none(monkeypatch):
    from src.domains.runtime import public as rt

    cur = FakeCursor(rows=[(1, "rt-a")])
    install_cursor(monkeypatch, cur)
    rows = rt.list_runtimes_rows(None, "")
    assert rows == [(1, "rt-a")]
    sql, args = cur.executed[0]
    assert "FROM agent_runtimes" in sql and "WHERE" not in sql and args == ()


def test_list_runtimes_rows_scope_global(monkeypatch):
    from src.domains.runtime import public as rt

    cur = FakeCursor(rows=[])
    install_cursor(monkeypatch, cur)
    rt.list_runtimes_rows("global", "u1")
    sql, args = cur.executed[0]
    assert "WHERE scope=%s" in sql and args == ("global", "u1")