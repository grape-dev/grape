"""Issue #3527：grape_t_cloud_sandboxes 只由 runtime 域建表/读取。

三道防线各自回归：
1. `src/api/db_setup.py::_SANDBOX_CONTROL_TABLES` 摘除该表（不再有第三份建表定义），
   且 202611110007 显式声明 mgmt 域、仍保留 `_ensure_column` 列补齐段；
2. `ensure_cloud_sandboxes_table()` 在非 owner 域直接跳过（不筛域形态兜底）；
3. 列表 / kill / provider 计数读路径统一 runtime 优先，历史落库位置仅作 1146 回退；
4. 多库 DSN 已配但 `GRAPE_MIGRATION_OWNER` 为空时输出 ERROR 告警（worker 除外）。
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
    """多库 DSN 已配 + owner 为空 → ERROR；worker / 已注入 owner / 单库不告警。"""
    from src.kernel import db_registry

    monkeypatch.setattr(db_registry, "lib_enabled", lambda: True)
    monkeypatch.delenv("GRAPE_MIGRATION_OWNER", raising=False)
    monkeypatch.delenv("GRAPE_SERVICE_NAME", raising=False)
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    messages = []
    monkeypatch.setattr(db_setup.log, "error", lambda msg, *a, **k: messages.append(msg))

    db_setup._warn_missing_migration_owner_if_multi_db()
    assert messages and "[migration-guard]" in messages[0]
    assert "GRAPE_MIGRATION_OWNER" in messages[0]

    messages.clear()
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "worker")
    db_setup._warn_missing_migration_owner_if_multi_db()
    assert messages == [], "worker 与 event 共库，owner 为空属正常"

    messages.clear()
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "server")
    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "mgmt")
    db_setup._warn_missing_migration_owner_if_multi_db()
    assert messages == []

    messages.clear()
    monkeypatch.delenv("GRAPE_MIGRATION_OWNER", raising=False)
    monkeypatch.setattr(db_registry, "lib_enabled", lambda: False)
    db_setup._warn_missing_migration_owner_if_multi_db()
    assert messages == [], "单库部署不筛域，无需告警"


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
        return []

    monkeypatch.setattr(rt, "_fetch_across_domains", fake)
    with pytest.raises(Exception) as exc:
        rt.runtime_kill_cloud_sandbox("sbx-1", user={"auth_type": "api_key", "id": "1"})

    assert getattr(exc.value, "status_code", None) == 404
    assert seen == [("runtime", "mgmt")]
