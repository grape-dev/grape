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


def test_issue_646_precheck_skips_only_delegated_foreign_tables(monkeypatch):
    """review M2：只有确认「本进程已注入迁移域」才跳过域外表核验。

    跳过条件由 `_foreign_owner_will_run()` 判定（三库模式 + owner）；单库/角色回落
    时它返回 False，预检照旧报缺失 → 不盖章 + 可见 ERROR。
    """
    monkeypatch.setattr(db_setup, "_foreign_owner_will_run", lambda tbl, **k: True)
    assert db_setup._issue_646_columns_missing(_EmptyCursor()) == []

    monkeypatch.setattr(db_setup, "_foreign_owner_will_run", lambda tbl, **k: False)
    assert "agent_runtimes.engine_uid" in db_setup._issue_646_columns_missing(_EmptyCursor())


def test_issue_868_precheck_skips_only_delegated_foreign_tables(monkeypatch):
    monkeypatch.setattr(db_setup, "_foreign_owner_will_run", lambda tbl, **k: True)
    assert db_setup._issue_868_session_columns_missing(_EmptyCursor()) == []
    monkeypatch.setattr(db_setup, "_foreign_owner_will_run", lambda tbl, **k: False)
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
        monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
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


# ── 第三轮 review（F1/F2/F3/F4）新增守卫 ─────────────────────────────────────

def test_current_domain_requires_three_db_flag(monkeypatch):
    """单库部署即使设了 owner 也不筛域（否则 runtime 迁移在单库里会被跳过）。"""
    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "runtime")
    monkeypatch.delenv("GRAPE_DOMAIN_LIB", raising=False)
    assert db_setup.current_migration_domain() == ""

    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    assert db_setup.current_migration_domain() == "runtime"

    # 只认四个合法域：非法 owner 不再直接判空，而走库名/角色推断兜底，
    # 因此这里必须清掉推断输入（套件内其他用例会残留 DEVOPS_DB_NAME/角色），
    # 才能验证「无任何可推断信息 → 空」这条口径。
    for _k in ("DEVOPS_DB_NAME", "GRAPE_PROCESS_ROLE", "GRAPE_SERVICE_NAME"):
        monkeypatch.delenv(_k, raising=False)
    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "worker")
    assert db_setup.current_migration_domain() == ""  # 只认四个合法域


def test_registry_filter_drops_migrations_of_other_domains(monkeypatch):
    """三库模式下执行器拿到的登记表只含本域条目（域外条目不再「跳过 DDL 却盖章」）。"""
    mg = db_setup.register_migration("299901010006", "t-mgmt-only", "safe", lambda: None)
    rt = db_setup.register_migration(
        "299901010007", "t-runtime-only", "safe", lambda: None, domains=("runtime",))
    try:
        monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
        monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "runtime")
        versions = [m.version for m in db_setup.filter_migration_registry(db_setup._MIGRATIONS)]
        assert "299901010007" in versions
        assert "299901010006" not in versions
        # 真实仓库里带 runtime 标签的 7 条必须在，mgmt-only 的一条都不许在
        assert all("runtime" in m.domains for m in db_setup.filter_migration_registry(
            db_setup._MIGRATIONS))

        # mgmt 进程反向：只保留 mgmt 域条目
        monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "mgmt")
        versions = [m.version for m in db_setup.filter_migration_registry(db_setup._MIGRATIONS)]
        assert "299901010006" in versions and "299901010007" not in versions

        # 单库模式（无 GRAPE_DOMAIN_LIB）：不过滤，行为与历史一致
        monkeypatch.delenv("GRAPE_DOMAIN_LIB", raising=False)
        versions = [m.version for m in db_setup.filter_migration_registry(db_setup._MIGRATIONS)]
        assert "299901010006" in versions and "299901010007" in versions
    finally:
        db_setup._MIGRATIONS[:] = [
            m for m in db_setup._MIGRATIONS
            if m.version not in (mg.version, rt.version)]


def test_anchor_migration_no_longer_touches_the_mgmt_table():
    """F3：autopilot_tasks.engine_runtime_uids 已从 runtime 域迁移拆出到 mgmt 域。"""
    import inspect

    src = inspect.getsource(db_setup._migration_1011_add_engine_anchor_columns)
    body = src.split('"""')[-1]   # 只看代码体，docstring 里会解释为什么要拆出去
    assert '_ensure_column(c, "autopilot_tasks"' not in body, \
        "runtime 域迁移里不能再建 mgmt 表列"

    by_version = {m.version: m for m in db_setup._MIGRATIONS}
    assert "202611170046" in by_version, "缺少拆分后的 mgmt 域迁移"
    assert by_version["202611170046"].domains == ("mgmt",)
    assert "autopilot_tasks" in inspect.getsource(
        db_setup._migration_202611170046_autopilot_engine_runtime_uids)


def test_both_startup_paths_export_the_migration_owner():
    """F2：systemd 与 nohup(deploy3db.sh) 两条启动路径都必须给进程 owner。"""
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sysd = open(os.path.join(root, "scripts/grape-systemd-run.sh"), encoding="utf-8").read()
    db3 = open(os.path.join(root, "deploy/deploy3db.sh"), encoding="utf-8").read()

    # systemd 路径：owner 必须在 `set -a; . "$GRAPE_3DB_ENV"` 之后按角色强制赋值
    src_pos = sysd.index('. "$_3db_env"')
    own_pos = sysd.index('export GRAPE_MIGRATION_OWNER="runtime"')
    assert own_pos > src_pos, "owner 导出必须晚于 DSN 文件 source，否则会被同名变量覆盖"

    # nohup 路径：start_one 里必须按角色传 owner（否则四个进程都早退）
    assert "GRAPE_MIGRATION_OWNER=\"$_mig_owner\"" in db3
    assert '_mig_owner="runtime"' in db3 and '_mig_owner="insight"' in db3
    assert '_mig_owner="mgmt"' in db3


# ── 第 3 轮 review（R1/M1/M2/M3/M4）新增守卫 ────────────────────────────────

def test_migration_versions_are_unique_and_keep_the_shared_numbers():
    """R1：版本号不得复用/顶替 —— 043 属 #2925，pool_selector 用 047。"""
    by_version = {}
    for m in db_setup._MIGRATIONS:
        assert m.version not in by_version, \
            f"版本号 {m.version} 重复登记（{by_version[m.version].name} / {m.name}）"
        by_version[m.version] = m

    assert by_version["202611170043"].name == "issue2925-issue-title-fetched-at"
    assert by_version["202611170047"].name == "agent-tasks-pool-selector-definition"
    # 目标分支上已登记的 #2925 迁移不得被删：既要存在也要有 fn
    assert callable(by_version["202611170043"].fn)


def test_cross_domain_legacy_migrations_are_labelled_by_the_owning_domain():
    """M1：只碰 runtime/insight 表的存量迁移必须按属主域贴标签，否则两侧都不补列。"""
    by_version = {m.version: m for m in db_setup._MIGRATIONS}
    for version, doms in db_setup._LEGACY_DOMAIN_LABELS.items():
        assert version in by_version, f"补正表里的 {version} 不存在（拼错号？）"
        assert by_version[version].domains == doms, \
            f"{version} 的生效域 {by_version[version].domains} != 补正表 {doms}"

    # 本 PR 期间定过编号的几条，域必须钉死
    assert by_version["202611170045"].domains == ("runtime",)   # runtime_credentials
    assert by_version["202611170044"].domains == ("mgmt",)      # autopilot_tasks
    assert by_version["202611170046"].domains == ("mgmt",)      # autopilot_tasks
    assert by_version["202611170047"].domains == ("runtime",)   # agent_tasks


def test_domain_migrations_run_under_the_migrate_lock(monkeypatch):
    """M3：域迁移执行器必须复用 init_database 的 GET_LOCK，不能裸跑（并发 DDL）。"""
    import inspect

    src = inspect.getsource(db_setup.run_domain_migrations)
    assert "_run_with_migrate_lock(" in src, "域迁移执行器绕过了迁移锁"

    locked = {}

    def _fake_lock(runner):
        locked["ran"] = True
        runner()

    monkeypatch.setattr(db_setup, "_run_with_migrate_lock", _fake_lock)
    monkeypatch.setattr(db_setup, "_ensure_migrations_ledger", lambda *a, **k: None)
    monkeypatch.setattr(db_setup, "_run_migrations_with_conn", lambda *a, **k: None)
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setenv("GRAPE_MIGRATION_OWNER", "runtime")
    db_setup.run_domain_migrations()
    assert locked.get("ran") is True


def test_prechecks_do_not_treat_ownership_inference_as_proof():
    """M2：预检不得把「归属推断」当作「已落库」；单库/角色回落时应可见地失败。"""
    import inspect

    for fn in (db_setup._issue_646_columns_missing,
               db_setup._issue_868_session_columns_missing,
               db_setup._migration_1011_backfill_rederive_engine_uids):
        src = inspect.getsource(fn)
        assert "_foreign_owner_will_run(" in src or "PRECHECK" in src, \
            f"{fn.__name__} 仍在用归属推断替代落库实测"

    # 行为断言：三库模式（注入 owner）才允许跳过核验；单库/角色回落必须返回 False
    from src.kernel import table_ownership as _own

    monkeypatch_calls = {"owner": "runtime"}

    def _fake_foreign_owner(table, domain=None):
        return monkeypatch_calls["owner"]

    def _fake_guard_enabled():
        return True

    real_fo, real_ge = _own.foreign_owner, _own.guard_enabled
    _own.foreign_owner, _own.guard_enabled = _fake_foreign_owner, _fake_guard_enabled
    try:
        import os
        old_env = {k: os.environ.get(k) for k in ("GRAPE_DOMAIN_LIB", "GRAPE_MIGRATION_OWNER")}
        os.environ.pop("GRAPE_DOMAIN_LIB", None)
        os.environ["GRAPE_MIGRATION_OWNER"] = "insight"     # 角色回落形态
        assert db_setup._foreign_owner_will_run("agent_runtimes") is False, \
            "单库/角色回落时归属不能替代实测"

        os.environ["GRAPE_DOMAIN_LIB"] = "1"
        os.environ["GRAPE_MIGRATION_OWNER"] = "mgmt"        # 真正注入域的三库形态
        assert db_setup._foreign_owner_will_run("agent_runtimes") is True
    finally:
        _own.foreign_owner, _own.guard_enabled = real_fo, real_ge
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
