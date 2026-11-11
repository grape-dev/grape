"""Issue #699 Phase 2 — kernel/db 分 Schema 与 SQL 改写测试（TC-2.1/2.3/2.6/2.7 行为面）。"""
import importlib
import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ===== TC-2.1：table()/t_*() 映射契约 =====

def test_t_mgmt_sqlite_prefix(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
    from src.kernel import db as kdb
    importlib.reload(kdb)
    assert kdb.t_mgmt("managed_repos") == "mgmt_managed_repos"
    assert kdb.t_runtime("events") == "rt_events"
    assert kdb.t_insight("obs_log") == "ins_obs_log"


def test_t_mgmt_mysql_schema_qualified(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
    from src.kernel import db as kdb
    importlib.reload(kdb)
    assert kdb.t_mgmt("managed_repos") == "grape_mgmt.managed_repos"
    assert kdb.t_runtime("events") == "grape_runtime.events"
    assert kdb.t_insight("obs_log") == "grape_insight.obs_log"
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")


def test_t_bare_name_when_schema_mode_off(monkeypatch):
    """t_*() returns bare table name when schema mode is off (graceful degradation)."""
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("GRAPE_SCHEMA_MODE", raising=False)
    from src.kernel import db as kdb
    importlib.reload(kdb)
    assert kdb.t_mgmt("managed_repos") == "managed_repos"
    assert kdb.t_runtime("events") == "events"
    assert kdb.t_insight("obs_log") == "obs_log"
    # _qualified_table always returns prefixed name regardless of schema mode
    assert kdb._qualified_table(kdb.SCHEMA_RUNTIME, "events") == "rt_events"


# ===== 注册表完整性 =====

def test_table_registry_coverage():
    from src.kernel.db import TABLE_REGISTRY, SCHEMA_MGMT, SCHEMA_RUNTIME, SCHEMA_INSIGHT
    assert len(TABLE_REGISTRY) >= 85
    counts = {SCHEMA_MGMT: 0, SCHEMA_RUNTIME: 0, SCHEMA_INSIGHT: 0}
    for name, schema in TABLE_REGISTRY.items():
        assert schema in counts
        counts[schema] += 1
    assert counts[SCHEMA_MGMT] >= 48
    assert counts[SCHEMA_RUNTIME] >= 22
    assert counts[SCHEMA_INSIGHT] >= 11
    # 设计文档锚点表抽查
    assert TABLE_REGISTRY["managed_repos"] == SCHEMA_MGMT
    assert TABLE_REGISTRY["events"] == SCHEMA_RUNTIME
    assert TABLE_REGISTRY["obs_llm_usage"] == SCHEMA_INSIGHT
    assert TABLE_REGISTRY["distributed_locks"] == SCHEMA_RUNTIME
    assert TABLE_REGISTRY["session_api_tokens"] == SCHEMA_MGMT


def test_schema_mode_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GRAPE_SCHEMA_MODE", raising=False)
    from src.kernel import db as kdb
    importlib.reload(kdb)
    assert kdb.schema_mode_enabled() is False
    sql = "SELECT * FROM managed_repos WHERE repo_key=?"
    assert kdb.qualify_sql(sql) == sql


# ===== qualify_sql 改写安全面 =====

@pytest.fixture()
def schema_mode(monkeypatch):
    monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    from src.kernel import db as kdb
    importlib.reload(kdb)
    return kdb


def test_qualify_sql_basic(schema_mode):
    kdb = schema_mode
    assert kdb.qualify_sql(
        "SELECT * FROM managed_repos WHERE repo_key=?"
    ) == "SELECT * FROM mgmt_managed_repos WHERE repo_key=?"
    assert kdb.qualify_sql(
        "INSERT INTO events (seq, status) VALUES (?, ?)"
    ) == "INSERT INTO rt_events (seq, status) VALUES (?, ?)"
    assert kdb.qualify_sql(
        "UPDATE agent_tasks SET status=? WHERE id=?"
    ) == "UPDATE rt_agent_tasks SET status=? WHERE id=?"
    assert kdb.qualify_sql(
        "DELETE FROM obs_log WHERE ts<?"
    ) == "DELETE FROM ins_obs_log WHERE ts<?"
    assert kdb.qualify_sql(
        "CREATE TABLE IF NOT EXISTS events (seq INTEGER)"
    ) == "CREATE TABLE IF NOT EXISTS rt_events (seq INTEGER)"
    assert kdb.qualify_sql(
        "SELECT * FROM events e JOIN managed_repos r ON e.repo_key=r.repo_key"
    ) == "SELECT * FROM rt_events e JOIN mgmt_managed_repos r ON e.repo_key=r.repo_key"


def test_qualify_sql_skips_literals_and_columns(schema_mode):
    kdb = schema_mode
    # 数据值不被改写
    assert kdb.qualify_sql(
        "INSERT INTO meta (key, value) VALUES ('events', '1')"
    ) == "INSERT INTO rt_meta (key, value) VALUES ('events', '1')"
    # 列名与表同名（agent_type 列）不改写
    sql = "SELECT runtime_id FROM devops_agent_session WHERE agent_type=?"
    assert kdb.qualify_sql(sql) == (
        "SELECT runtime_id FROM mgmt_devops_agent_session WHERE agent_type=?"
    )
    # 未注册表名不改写
    assert kdb.qualify_sql("SELECT x FROM sqlite_master_seq") == "SELECT x FROM sqlite_master_seq"
    # sqlite_master 表名清单 → 逻辑名（剥前缀），业务侧 _tables 成员判断语义不变
    assert kdb.qualify_sql("SELECT name FROM sqlite_master WHERE type='table'") == (
        "SELECT " + kdb._SQLITE_MASTER_STRIP + " FROM sqlite_master WHERE type='table'"
    )
    # PRAGMA table_info(逻辑名) → 物理前缀名
    assert kdb.qualify_sql("PRAGMA table_info(agent_type)") == "PRAGMA table_info(rt_agent_type)"
    assert kdb.qualify_sql("PRAGMA table_info(not_a_registry_table)") == (
        "PRAGMA table_info(not_a_registry_table)"
    )
    # 已前缀表名不重复改写
    assert kdb.qualify_sql("SELECT * FROM rt_events") == "SELECT * FROM rt_events"


def test_sqlite_master_strip_anchored_prefix(schema_mode):
    """Regression: obs_alert_rules contains 'rt_' substring — must not be stripped.

    The old REPLACE(name,'rt_','') approach corrupted obs_alert_rules → obs_alerules.
    The new CASE+LIKE 'prefix%' anchored approach only strips actual prefixes.
    """
    kdb = schema_mode
    conn = sqlite3.connect(":memory:")
    wrapped = kdb.SchemaSQLiteDB(conn)
    # Create prefixed insight tables (obs_alert_rules / obs_alert_events both contain 'rt_')
    wrapped.execute("CREATE TABLE IF NOT EXISTS obs_alert_rules (id INTEGER)")
    wrapped.execute("CREATE TABLE IF NOT EXISTS obs_alert_events (id INTEGER)")
    wrapped.execute("CREATE TABLE IF NOT EXISTS obs_llm_usage (id INTEGER)")
    wrapped.commit()
    # Query via wrapped connection: qualify_sql rewrites SELECT name FROM sqlite_master
    # to use _SQLITE_MASTER_STRIP which must strip ins_ prefix correctly
    tables = {r[0] for r in wrapped.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    # Logical names (prefix stripped) must be the full registered names
    assert "obs_alert_rules" in tables, f"got {tables}"
    assert "obs_alert_events" in tables, f"got {tables}"
    assert "obs_llm_usage" in tables, f"got {tables}"
    # Must NOT contain corrupted names
    assert "obs_alerules" not in tables
    assert "obs_aleevents" not in tables
    conn.close()


# ===== SQLite 连接代理（TC-2.3 行为面）=====

def test_schema_sqlite_proxy_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    from src.kernel import db as kdb
    importlib.reload(kdb)
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    wrapped = kdb.SchemaSQLiteDB(conn)
    wrapped.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, status TEXT)")
    wrapped.execute("INSERT INTO events (status) VALUES ('pending')")
    wrapped.commit()
    # 裸表名经代理自动改写为前缀表名
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "rt_events" in tables and "events" not in tables
    row = wrapped.execute("SELECT status FROM events WHERE seq=1").fetchone()
    assert row[0] == "pending"
    # 迁移台账 + 幂等 RENAME
    plan = kdb.migrate_sqlite_prefixes(conn, dry_run=True)
    assert plan == []  # 新库建表已带前缀，无需迁移


def test_migrate_sqlite_prefixes_renames_with_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("GRAPE_SCHEMA_MODE", raising=False)
    from src.kernel import db as kdb
    importlib.reload(kdb)
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO events (status) VALUES ('running')")
    conn.commit()
    plan = kdb.migrate_sqlite_prefixes(conn, dry_run=True)
    assert ("events", "rt_events") in plan
    kdb.migrate_sqlite_prefixes(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "rt_events" in tables and "events" not in tables
    assert conn.execute("SELECT status FROM rt_events WHERE seq=1").fetchone()[0] == "running"
    # 幂等：二次迁移为空
    assert kdb.migrate_sqlite_prefixes(conn) == []
    conn.close()


# ===== TC-2.6：contracts 空壳接口 =====

def test_contracts_import():
    from src.domains.contracts.mgmt_service import MgmtService
    from src.domains.contracts.runtime_service import RuntimeService
    from src.domains.contracts.insight_service import InsightService
    for cls in (MgmtService, RuntimeService, InsightService):
        assert cls.__abstractmethods__  # 空壳接口：方法未实现


# ===== TC-2.3 e2e：session_manager 启用 schema mode 建前缀表 =====

def test_session_manager_schema_mode_e2e(tmp_path):
    code = textwrap.dedent("""
        import os
        os.environ["DEVOPS_DB_PATH"] = {db_path!r}
        os.environ["GRAPE_SCHEMA_MODE"] = "1"
        os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
        from src.kernel import session_manager  # noqa: F401  建表走 SchemaSQLiteDB 代理
        from src.api import db_setup
        db_setup.init_database()  # 全量建表（TC-2.3）
        import sqlite3
        raw = sqlite3.connect({db_path!r})
        created = [r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print("\\n".join(sorted(created)))
        raw.close()
    """).format(db_path=str(tmp_path / "e2e.db"))
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=REPO_ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    tables = set(r.stdout.split())
    assert any(t.startswith("mgmt_") for t in tables), tables
    assert any(t.startswith("rt_") for t in tables), tables
    assert any(t.startswith("ins_") for t in tables), tables


# ===== TC-2.7：运行时改写后无裸表名泄漏（行为等价验证）=====

def test_codemod_tool_check_mode(tmp_path):
    tool = os.path.join(REPO_ROOT, "scripts", "replace_table_refs.py")
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import sqlite3\n\n"
        "def q():\n"
        "    return db.execute(\"SELECT * FROM events WHERE status=?\", (s,))\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, tool, "--check", str(sample)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "would rewrite 1 ref(s)" in r.stdout
    assert sample.read_text(encoding="utf-8").count("FROM events") == 1  # check 不写回
    r2 = subprocess.run(
        [sys.executable, tool, str(sample)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
    )
    assert r2.returncode == 0, r2.stderr
    content = sample.read_text(encoding="utf-8")
    assert "FROM {t_runtime('events')}" in content
    assert "from src.kernel.db import t_runtime" in content
