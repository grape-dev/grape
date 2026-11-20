"""Issue #3527：grape_t_cloud_sandboxes 只由 runtime 域建表/读取。

三道防线各自回归：
1. `src/api/db_setup.py::_SANDBOX_CONTROL_TABLES` 摘除该表（不再有第三份建表定义），
   且 202611110007 显式声明 mgmt 域、仍保留 `_ensure_column` 列补齐段；
2. `ensure_cloud_sandboxes_table()` 在非 owner 域直接跳过（不筛域形态兜底）；
3. 列表 / kill / provider 计数读路径统一 runtime 优先，历史落库位置仅作 1146 回退；
4. 多库 DSN 已配但 `GRAPE_MIGRATION_OWNER` 为空时输出 ERROR 告警（worker 除外），
   且按入口区分文案：run_pending_migrations=不筛域跑全量 / run_domain_migrations=未执行任何域迁移；
5. kill 的台账写跟随读侧命中的域（0 行回退另一域，两域皆 0 行报可见 500）。
"""
import contextlib

import pytest

from src.api import db_setup
from src.domains.runtime.routes import runtime as rt


class _Cursor:
    def __init__(self, sink):
        self._sink = sink

    def execute(self, sql, params=()):
        self._sink.append((sql, params))
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _DB:
    def __init__(self, sink):
        self._sink = sink
        self.committed = False

    def cursor(self):
        return _Cursor(self._sink)

    def commit(self):
        self.committed = True


def test_control_tables_no_longer_define_cloud_sandboxes():
    """摘表：第三份建表定义消失，同组 mgmt 表仍在。"""
    names = [name for name, _ddl in db_setup._SANDBOX_CONTROL_TABLES]
    assert "grape_t_cloud_sandboxes" not in names
    assert "grape_t_sandbox_templates" in names
    assert "grape_t_sandbox_provider_config" in names


def test_202611110007_declares_mgmt_domain_and_keeps_column_repair():
    """202611110007 显式 mgmt 域；_ensure_column 列补齐段不得被摘表连带删除。"""
    by_version = {m.version: m for m in db_setup._MIGRATIONS}
    assert by_version["202611110007"].domains == ("mgmt",)

    import inspect

    body = inspect.getsource(db_setup._migration_sandbox_control_plane_schema)
    assert '_ensure_column(c, "runtime_credentials", "cred_type"' in body
    assert '_ensure_column(c, "agent_runtimes", "runtime_type"' in body
    # 循环逐表过归属守卫（不筛域形态的兜底）
    assert "_owned_skip(_name" in body


def test_migration_never_creates_cloud_sandboxes_even_without_guard(monkeypatch):
    """守卫全部放行时也不会再建该表 —— 摘表是硬事实，不依赖守卫。"""
    sink = []
    monkeypatch.setattr(db_setup, "db", _DB(sink))
    monkeypatch.setattr(db_setup, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr(db_setup, "_owned_skip", lambda table, what="": False)
    ensured = []
    monkeypatch.setattr(
        db_setup, "_ensure_column",
        lambda c, table, col, ddl, **kw: ensured.append((table, col)))

    db_setup._migration_sandbox_control_plane_schema()

    ddl = [sql for sql, _params in sink if "CREATE TABLE" in sql]
    assert ddl, "同组 mgmt 表仍应建表"
    assert all("grape_t_cloud_sandboxes" not in sql for sql in ddl)
    assert any("grape_t_sandbox_templates" in sql for sql in ddl)
    assert ("runtime_credentials", "cred_type") in ensured
    assert ("agent_runtimes", "runtime_type") in ensured


def test_ensure_cloud_sandboxes_skips_on_non_owner_domain(monkeypatch):
    """非 owner 域（mgmt/insight）调用迁移函数必须直接跳过，不执行任何 DDL。"""
    from src.kernel import table_ownership as own
    from src.kernel import session_manager as sm
    from src.domains.runtime.migrations import ensure_cloud_sandboxes_table as mig

    monkeypatch.setattr(own, "should_skip", lambda table: True)
    sink = []
    monkeypatch.setattr(sm, "db", _DB(sink))
    monkeypatch.setattr(sm, "db_lock", contextlib.nullcontext())

    mig.ensure_cloud_sandboxes_table()

    assert sink == []
    assert mig.MIGRATION["domains"] == ("runtime",)


def test_missing_owner_alerts_only_when_multi_db(monkeypatch):
    """多库 DSN 已配 + owner 为空 → ERROR；worker / 已注入 owner / 单库不告警。

    文案按 call site 区分（review R1）：pending=不筛域跑全量；domain=未执行任何域迁移。
    """
    from src.kernel import db_registry

    monkeypatch.setattr(db_registry, "lib_enabled", lambda: True)
    monkeypatch.delenv("GRAPE_MIGRATION_OWNER", raising=False)
    monkeypatch.delenv("GRAPE_SERVICE_NAME", raising=False)
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    messages = []
    monkeypatch.setattr(db_setup.log, "error", lambda msg, *a, **k: messages.append(msg))

    # pending 入口（run_pending_migrations）：不筛域形态，跑全量登记表
    db_setup._warn_missing_migration_owner_if_multi_db("pending")
    assert messages and "[migration-guard]" in messages[0]
    assert "GRAPE_MIGRATION_OWNER" in messages[0]
    assert "不筛域" in messages[0]

    # domain 入口（run_domain_migrations）：owner 为空 → 未执行任何域迁移
    messages.clear()
    db_setup._warn_missing_migration_owner_if_multi_db("domain")
    assert messages and "[migration-guard]" in messages[0]
    assert "未执行任何域迁移" in messages[0]
    assert "不筛域" not in messages[0]

    # worker：两个入口都不告警（与 event 共库，owner 为空属正常）
    messages.clear()
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
    db_setup._warn_missing_migration_owner_if_multi_db("pending")
    db_setup._warn_missing_migration_owner_if_multi_db("domain")
    assert messages == [], "worker 与 event 共库，owner 为空属正常"

    # 已注入 owner：不告警
    messages.clear()
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "mgmt")
    db_setup._warn_missing_migration_owner_if_multi_db("pending")
    db_setup._warn_missing_migration_owner_if_multi_db("domain")
    assert messages == []

    # 单库：不告警
    messages.clear()
    monkeypatch.delenv("GRAPE_MIGRATION_OWNER", raising=False)
    monkeypatch.setattr(db_registry, "lib_enabled", lambda: False)
    db_setup._warn_missing_migration_owner_if_multi_db("pending")
    db_setup._warn_missing_migration_owner_if_multi_db("domain")
    assert messages == [], "单库部署不筛域，无需告警"


def test_migration_entries_wire_owner_guard_by_call_site(monkeypatch):
    """告警挂点（review R1）：pending 入口与 domain 入口分别按 call site 触发。"""
    calls = []
    monkeypatch.setattr(
        db_setup, "_warn_missing_migration_owner_if_multi_db",
        lambda call_site="pending": calls.append(call_site))
    # run_pending_migrations：告警必须在真正跑全量迁移的入口
    monkeypatch.setattr(db_setup, "_ensure_migration_session_lock_timeout", lambda: None)
    monkeypatch.setattr(db_setup, "_run_migrations_with_conn", lambda conn, **kw: None)
    monkeypatch.setattr(db_setup, "_backfill_missing_migration_reports", lambda: None)
    db_setup.run_pending_migrations()
    assert calls == ["pending"]

    # run_domain_migrations（owner 为空 → 直接 return）：domain 文案入口
    calls.clear()
    monkeypatch.delenv("GRAPE_MIGRATION_OWNER", raising=False)
    db_setup.run_domain_migrations()
    assert calls == ["domain"]


def test_list_endpoint_prefers_runtime_domain(monkeypatch):
    """列表端点域序 runtime 优先：不得先读历史 mgmt 空表。"""
    calls = []

    def fake(sql, params=(), domains=("runtime", "mgmt")):
        calls.append(domains)
        return [(0,)] if "COUNT(*)" in sql else []

    monkeypatch.setattr(rt, "_fetch_across_domains", fake)
    out = rt.runtime_list_cloud_sandboxes(user={"auth_type": "api_key", "id": "1"})

    assert out["total"] == 0
    assert calls, "列表端点必须经域解析读沙箱表"
    assert all(domains == ("runtime", "mgmt") for domains in calls)


def test_kill_reads_across_domains_runtime_first(monkeypatch):
    """kill 查询走域解析（runtime 优先），无行时才 404。"""
    seen = []

    def fake(sql, params=(), domains=("runtime", "mgmt")):
        seen.append(domains)
        return [], domains[0]

    monkeypatch.setattr(rt, "_fetch_across_domains_with_hit", fake)
    with pytest.raises(Exception) as exc:
        rt.runtime_kill_cloud_sandbox("sbx-1", user={"auth_type": "api_key", "id": "1"})

    assert getattr(exc.value, "status_code", None) == 404
    assert seen == [("runtime", "mgmt")]


class _FakeProvider:
    """kill_sandbox 恒成功的 provider 替身（不触真实平台）。"""

    def kill_sandbox(self, sandbox_id):
        return True


def _patch_kill_env(monkeypatch, hit_domain, write_results):
    """组装 kill 端点测试环境。

    读命中 hit_domain 的 (provider, state, repo_key, provider_id) 行；
    _mark_sandbox_killed_on_domain 依序弹出 write_results 作为 rowcount。
    返回记录实际写域序列的 list。
    """
    monkeypatch.setattr(
        rt, "_fetch_across_domains_with_hit",
        lambda sql, params=(), domains=("runtime", "mgmt"):
            ([("prov", "running", "", None)], hit_domain))
    writes = []

    def fake_write(domain, sandbox_id, now_iso):
        writes.append(domain)
        return write_results.pop(0) if write_results else 1

    monkeypatch.setattr(rt, "_mark_sandbox_killed_on_domain", fake_write)
    from src.domains.runtime.agents.sandbox import backends as _bk
    monkeypatch.setattr(
        _bk, "default_registry",
        lambda: {"prov": lambda *a, **kw: _FakeProvider()})
    return writes


@pytest.mark.parametrize("hit", ["runtime", "mgmt"])
def test_kill_write_follows_read_hit_domain(monkeypatch, hit):
    """读/写域一致（review R2）：读命中哪个域，台账就先写哪个域。"""
    writes = _patch_kill_env(monkeypatch, hit, [1])

    out = rt.runtime_kill_cloud_sandbox("sbx-1", user={"auth_type": "api_key", "id": "1"})

    assert out == {"status": "killed"}
    assert writes == [hit], "写侧必须跟随读侧命中的域，而不是固定写进程主库"


def test_kill_write_falls_back_to_other_domain_on_zero_rows(monkeypatch):
    """读命中 mgmt 回退行、该域 0 行更新 → 回退另一域补写，仍返回 killed。"""
    writes = _patch_kill_env(monkeypatch, "mgmt", [0, 1])

    out = rt.runtime_kill_cloud_sandbox("sbx-1", user={"auth_type": "api_key", "id": "1"})

    assert out == {"status": "killed"}
    assert writes == ["mgmt", "runtime"]


def test_kill_reports_visible_error_when_ledger_update_misses(monkeypatch):
    """两域都 0 行（review R2）：不得静默返回 killed，必须报可见 500。"""
    writes = _patch_kill_env(monkeypatch, "runtime", [0, 0])

    with pytest.raises(Exception) as exc:
        rt.runtime_kill_cloud_sandbox("sbx-1", user={"auth_type": "api_key", "id": "1"})

    assert getattr(exc.value, "status_code", None) == 500
    detail = getattr(exc.value, "detail", {}) or {}
    assert "ledger row update failed" in detail.get("message", "")
    assert writes == ["runtime", "mgmt"], "命中域优先，另一域兜底，两者都试过"


class _KillWriteCursor:
    def __init__(self, rowcount=None):
        self.executed = []
        self.closed = False
        if rowcount is not None:
            self.rowcount = rowcount

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def close(self):
        self.closed = True


class _KillWriteConn:
    def __init__(self, rowcount=None):
        self.cur = _KillWriteCursor(rowcount)
        self.closed = False
        self.committed = False

    def commit(self):
        self.committed = True

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def test_kill_write_uses_dedicated_connection_and_closes(monkeypatch):
    """自有 DSN 路径用 new_connection 专用连接，用完即关（review R3）。"""
    from src.kernel import db_registry as dbr

    conn = _KillWriteConn(rowcount=1)
    seen = []

    def fake_new_connection(domain, autocommit=True):
        seen.append((domain, autocommit))
        return conn

    monkeypatch.setattr(dbr, "new_connection", fake_new_connection)

    assert rt._mark_sandbox_killed_on_domain("runtime", "sbx-1", "now") == 1
    assert seen == [("runtime", True)]
    assert conn.closed and conn.cur.closed
    assert "UPDATE grape_t_cloud_sandboxes" in conn.cur.executed[0][0]


def test_kill_write_unknown_rowcount_is_not_success(monkeypatch):
    """游标无 rowcount 时返回 0（未确认），不得让调用方当成功（review R3）。"""
    from src.kernel import db_registry as dbr

    conn = _KillWriteConn(rowcount=None)
    monkeypatch.setattr(dbr, "new_connection", lambda *a, **k: conn)

    assert rt._mark_sandbox_killed_on_domain("runtime", "sbx-1", "now") == 0


def test_kill_write_falls_back_to_process_db_without_own_dsn(monkeypatch):
    """无自有 DSN（new_connection 抛错）→ 回落进程主库并 commit（review R3）。"""
    from src.kernel import db_registry as dbr
    from src.kernel import session_manager as sm

    conn = _KillWriteConn(rowcount=1)

    def no_dsn(*_a, **_kw):
        raise RuntimeError("domain has no own DSN")

    monkeypatch.setattr(dbr, "new_connection", no_dsn)
    monkeypatch.setattr(sm, "db", conn)
    monkeypatch.setattr(sm, "db_lock", contextlib.nullcontext())

    assert rt._mark_sandbox_killed_on_domain("mgmt", "sbx-1", "now") == 1
    assert conn.cur.executed, "必须向进程主库发出 UPDATE"
