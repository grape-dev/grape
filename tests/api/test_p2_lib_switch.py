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

def test_insight_local_table_and_lock_switch(monkeypatch):
    from src.domains.contracts.local import insight_local as mod

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: True, raising=True)
    assert mod._t("obs_log") == "obs_log"
    svc = mod.InsightServiceLocal()
    own = db_registry.domain_lock("insight")
    assert svc._lock is own

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False, raising=True)
    assert mod._t("obs_log") == mod.t_insight("obs_log")
    import src.kernel.session_manager as sm
    assert svc._lock is sm.db_lock


def test_mgmt_local_table_and_lock_switch(monkeypatch):
    from src.domains.contracts.local import mgmt_local as mod

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: True, raising=True)
    assert mod._t("managed_repos") == "managed_repos"
    svc = mod.MgmtServiceLocal()
    own = db_registry.domain_lock("mgmt")
    assert svc._lock is own

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False, raising=True)
    assert mod._t("managed_repos") == mod.t_mgmt("managed_repos")
    import src.kernel.session_manager as sm
    assert svc._lock is sm.db_lock


def test_compat_cursor_translates_qmark(monkeypatch):
    """自有 DSN 连接必须走方言翻译：? → %s 且字面 % 被转义。"""
    from src.kernel import db_registry as r

    class Raw:
        def __init__(self):
            self.sql = None
            self.args = None

        def execute(self, sql, params=None):
            self.sql = sql
            self.args = params
            return 0

    raw = Raw()
    cur = r._CompatCursor(raw)
    cur.execute("SELECT * FROM t WHERE a=? AND b LIKE 'x:%'", ("v",))
    assert "%s" in raw.sql and "?" not in raw.sql.split("LIKE")[0]
    assert raw.args == ("v",)



def test_available_requires_own_dsn(monkeypatch):
    """F2：未配自有 DSN 时 lib 不可用（sqlite 复用路径不再误判为可用）。"""
    from src.domains.runtime import public as rt
    from src.domains.mgmt import public as mg

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False)
    assert rt.available() is False
    assert mg.available() is False

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: True)
    assert rt.available() is True
    assert mg.available() is True


def test_available_ignores_process_db_reuse(monkeypatch):
    """即使域连接可复用进程库，只要没有自有 DSN 也不应判为可用。"""
    from src.domains.runtime import public as rt

    monkeypatch.setattr(db_registry, "domain_db", lambda d: object())
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False)
    assert rt.available() is False


def test_mgmt_lib_foreign_only_skips_own_domain(monkeypatch):
    """写路径防同域误走 lib：当前进程就是 mgmt 时返回 None（留在本地事务）。

    mgmt 进程也 include 了 runtime 路由（events/chat/pipeline），同一段代码可能
    在 mgmt 进程里执行；此时走 lib 会换成独立连接（autocommit），丢事务回滚能力。
    """
    from src.kernel import domain_read

    sentinel = object()
    monkeypatch.setattr(domain_read, "_lib", lambda d, f: sentinel)
    monkeypatch.setattr(db_registry, "current_domain", lambda: "mgmt")

    assert domain_read.mgmt_lib() is sentinel              # 读侧不受影响
    assert domain_read.mgmt_lib(foreign_only=True) is None  # 写侧同域 → 不委派


def test_mgmt_lib_foreign_only_allows_runtime_process(monkeypatch):
    from src.kernel import domain_read

    sentinel = object()
    monkeypatch.setattr(domain_read, "_lib", lambda d, f: sentinel)
    monkeypatch.setattr(db_registry, "current_domain", lambda: "runtime")

    assert domain_read.mgmt_lib(foreign_only=True) is sentinel
