"""三库模式下的迁移归属（Route A）：本域进程只跑本域登记的迁移。

背景：configs/table_ownership.json 把 agent_runtimes / runtime_credentials /
agent_tasks / task_messages / *_agent_session 判给 runtime 域，所以 mgmt 进程的
DDL 被 `_owned_skip` 跳过；若预检仍按本库判定「加列未完成」，迁移永远不盖章、
每次启动报 ERROR，只能靠手工补库 + 手工盖章。本文件锁三件事：

1. 预检尊重归属：外来表的缺列不计入 missing（由属主域进程负责）。
2. 注册式迁移可标 domains；run_domain_migrations 只跑本域登记项。
3. 域进程的台账天然是空的，绝不能因此触发 legacy 全量迁移。
"""
import pytest

from src.api import db_setup


class _EmptyCursor:
    def execute(self, sql, params=()):
        self.last = sql
        return self

    def fetchall(self):
        return []


def test_register_migration_defaults_to_mgmt_and_accepts_domains():
    mg = db_setup.register_migration("299901010001", "t-default", "safe", lambda: None)
    rt = db_setup.register_migration(
        "299901010002", "t-runtime", "safe", lambda: None, domains=("runtime",))
    try:
        assert mg.domains == ("mgmt",)
        assert rt.domains == ("runtime",)
    finally:
        db_setup._MIGRATIONS[:] = [
            m for m in db_setup._MIGRATIONS if m.version not in ("299901010001", "299901010002")]


def test_issue_646_precheck_treats_foreign_tables_as_satisfied(monkeypatch):
    # 全部表都判给别的域 → 本进程没有一条该由自己建，missing 必须为空
    monkeypatch.setattr(db_setup, "_owned_skip", lambda tbl, what="": True)
    assert db_setup._issue_646_columns_missing(_EmptyCursor()) == []

    # 归属守卫放行（本域表）且列不存在 → 照旧报缺失，不能把真实缺列放过去
    monkeypatch.setattr(db_setup, "_owned_skip", lambda tbl, what="": False)
    missing = db_setup._issue_646_columns_missing(_EmptyCursor())
    assert "agent_runtimes.engine_uid" in missing


def test_issue_868_precheck_treats_foreign_tables_as_satisfied(monkeypatch):
    monkeypatch.setattr(db_setup, "_owned_skip", lambda tbl, what="": True)
    assert db_setup._issue_868_session_columns_missing(_EmptyCursor()) == []
    monkeypatch.setattr(db_setup, "_owned_skip", lambda tbl, what="": False)
    assert "devops_agent_session.engine_uid" in db_setup._issue_868_session_columns_missing(_EmptyCursor())


def test_run_domain_migrations_filters_by_domain_and_never_runs_legacy(monkeypatch):
    mg = db_setup.register_migration("299901010003", "t-default", "safe", lambda: None)
    rt = db_setup.register_migration(
        "299901010004", "t-runtime", "safe", lambda: None, domains=("runtime",))
    captured = {}

    def _fake_runner(conn, registry=None, mdl_holders=None, allow_legacy=True):
        captured["versions"] = [m.version for m in (registry or [])]
        captured["allow_legacy"] = allow_legacy

    monkeypatch.setattr(db_setup, "_run_migrations_with_conn", _fake_runner)
    monkeypatch.setattr(db_setup, "_ensure_migrations_ledger", lambda conn: None)
    monkeypatch.setattr(db_setup, "_ensure_migration_session_lock_timeout", lambda: None)
    try:
        monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "runtime")
        db_setup.run_domain_migrations()
        # 只跑 runtime 域登记的条目（真实仓库里是 #646/#868/#1011 那 7 条 + 本测试的 1 条）
        assert "299901010004" in captured["versions"]
        assert "299901010003" not in captured["versions"]
        # 每条都必须带 runtime 域标签（mgmt-only 条目一条都不许混进来）
        assert all("runtime" in m.domains for m in db_setup._MIGRATIONS
                   if m.version in captured["versions"])
        assert captured["allow_legacy"] is False

        # worker 与 event 共库：不设 owner → 一条都不跑；mgmt 走 init_database 常规路径
        for owner in ("", "mgmt"):
            captured.clear()
            monkeypatch.setenv("GRAPE_MIGRATION_OWNER", owner)
            db_setup.run_domain_migrations()
            assert captured == {}, owner
    finally:
        db_setup._MIGRATIONS[:] = [
            m for m in db_setup._MIGRATIONS
            if m.version not in (mg.version, rt.version)]


def test_empty_domain_ledger_does_not_trigger_legacy_full_migration(monkeypatch):
    """域库台账为空（新建）→ 只跑登记项，绝不 legacy 全量。"""
    called = {"legacy": 0}

    def _boom():
        called["legacy"] += 1

    monkeypatch.setattr(db_setup, "_run_all_migrations", _boom)

    class _Conn:
        def cursor(self):
            return _EmptyCursor()

        def commit(self):
            pass

    class _M:
        version = "299901010005"
        name = "t-empty"
        kind = "safe"
        domains = ("runtime",)
        checksum = ""

        def fn(self):
            pass

    db_setup._run_migrations_with_conn(_Conn(), registry=[_M()], allow_legacy=False)
    assert called["legacy"] == 0