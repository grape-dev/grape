"""Issue #3877 回归：events 级联补偿五列必须走**登记式**迁移（存量 runtime 库）。

背景（生产证据，Issue #3877）
-----------------------------
分库（三库）拓扑下**存量** runtime 库的 ``events`` 表永久缺 ``cascade_reason`` /
``report_missing`` / ``triggered_by`` / ``cascade_task_status`` /
``cascade_prev_event_status`` → ``_cascade_event_terminal()`` 的终态收敛 UPDATE 每条抛
``1054 Unknown column 'cascade_reason'``（72 小时 54 次），事件终态不收敛、守卫只能等 TTL。

五列此前只登记在两处，**对存量库都不生效**：

- ``runtime/schema_ddl.py::_EVENTS_TABLE_DDL`` —— 只服务新装库的 ``CREATE TABLE``；
- ``runtime/migrations_ddl.py::ALTERS_SESSION_EVENT_COLUMNS`` —— 只被
  ``db_setup._migrate_tables()`` 消费，而它属**已冻结的 legacy 轨道**
  ``_run_all_migrations()``：已盖 legacy-stamp（202610300001）的存量库启动时整段跳过。

修复：五列改走注册式迁移 ``202611250001``（kind=safe + post_check，五列在才盖章）。

覆盖（生产同款执行器 ``_run_migrations_with_conn``，sqlite 内存库即可）：
1. 迁移已注册、kind=safe、domains=("runtime",)、有 post_check，且列定义与 schema_ddl 一致；
2. 已盖章存量库跑一遍 → 五列建成并盖一行章，且 legacy 全量轨道**没有**被执行；
3. 再跑一遍 → 幂等，不重复盖章；
4. 只补上一半列 → post_check 拦住，不盖章（禁止「版本号盖死、列缺着」）；
5. 表不在本库（非 owner 域）→ 跳过且不盖章；
6. 负向对照：不注册该迁移（registry=[]）时存量库不会因「老轨不跑」而意外补出五列。
"""
import sqlite3

import pytest

from src.api import db_setup as ds

MIG_VERSION = "202611250001"
MIG_NAME = "events-cascade-compensation-columns"
TABLE = "events"
COLUMNS = (
    "cascade_reason",
    "report_missing",
    "triggered_by",
    "cascade_task_status",
    "cascade_prev_event_status",
)


def _events_ddl(with_cascade: bool = False) -> str:
    """存量库形态的 events 表（cascade 五列缺失时可复现 1054）。"""
    base = (
        "CREATE TABLE events ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, repo_key TEXT NOT NULL, "
        "event_type TEXT NOT NULL, issue_id TEXT DEFAULT '', agent_type TEXT DEFAULT '', "
        "scene_id TEXT DEFAULT '', status TEXT DEFAULT 'pending', "
        "exit_code INTEGER DEFAULT NULL, completed_at TEXT DEFAULT '', "
        "result TEXT DEFAULT '', structured_output TEXT DEFAULT ''")
    if with_cascade:
        base += (
            ", cascade_reason TEXT DEFAULT '', report_missing INTEGER DEFAULT 0, "
            "triggered_by TEXT DEFAULT '', cascade_task_status TEXT DEFAULT '', "
            "cascade_prev_event_status TEXT DEFAULT ''")
    return base + ")"


def _seed_ledger(conn) -> None:
    conn.execute(
        "CREATE TABLE grape_t_schema_migrations ("
        "version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT, "
        "duration_ms INTEGER DEFAULT 0, checksum TEXT DEFAULT '')")
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version,name,kind,applied_at) "
        "VALUES ('202610300001','legacy-run-all-migrations','legacy-stamp','2026-10-30 00:00:00')")


@pytest.fixture
def stamped_db(monkeypatch):
    """存量库模拟：版本表只有 legacy 盖帽，events 没有 cascade 五列。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(_events_ddl())
    conn.execute("INSERT INTO events (repo_key, event_type) VALUES ('gitcode:o/r', 'a2a_event')")
    _seed_ledger(conn)
    conn.commit()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    yield conn
    conn.close()


def _mig():
    return {m.version: m for m in ds._MIGRATIONS}[MIG_VERSION]


def _columns(conn):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}


def _stamped(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?",
        (MIG_VERSION,)).fetchone()[0]


def test_registered_safe_owner_domain_and_has_post_check():
    """① 注册式迁移在位：kind=safe、domains=("runtime",)、post_check 可判定。"""
    mig = _mig()
    assert mig.name == MIG_NAME
    assert mig.kind == "safe"
    assert callable(mig.fn)
    assert callable(mig.post_check), "五列存在性可判定，必须带 post_check（Issue #3648）"
    assert tuple(mig.domains) == ("runtime",), "events 归 runtime（configs/table_ownership.json）"

    # 列定义必须与 schema_ddl（新装库形态）逐字一致，否则新库/存量库口径分叉
    from src.domains.runtime import schema_ddl
    from src.domains.runtime.migrations import events_cascade_columns as mig_mod

    ddl_text = schema_ddl._EVENTS_TABLE_DDL
    for col, col_ddl in mig_mod.COLUMNS:
        assert col in COLUMNS
        assert f"{col} {col_ddl}" in ddl_text, (
            f"{col} 的列定义 {col_ddl!r} 与 schema_ddl._EVENTS_TABLE_DDL 不一致")


def test_stamped_db_gets_columns_and_is_stamped(stamped_db, monkeypatch):
    """② 已盖章存量库：老轨不执行也能补出五列，并盖一行章。"""
    called = {"legacy": 0}

    def _boom():
        called["legacy"] += 1

    monkeypatch.setattr(ds, "_run_all_migrations", _boom)

    assert not (set(COLUMNS) & _columns(stamped_db))
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert set(COLUMNS) <= _columns(stamped_db), (
        "已盖章存量库未补列 → cascade 终态 UPDATE 将 1054 Unknown column")
    assert _stamped(stamped_db) == 1
    assert called["legacy"] == 0, "已盖章存量库不得回落到 legacy 全量轨道"
    # 补列后历史行取默认值（幂等补列不破坏既有数据）
    row = stamped_db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM events WHERE seq=1").fetchone()
    assert row[0] == "" and int(row[1]) == 0 and row[2] == ""


def test_second_run_is_idempotent(stamped_db):
    """③ 再跑一遍：0 pending，不重复盖章、不报错。"""
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert _stamped(stamped_db) == 1
    assert set(COLUMNS) <= _columns(stamped_db)


def test_no_stamp_when_columns_partially_missing(stamped_db, monkeypatch):
    """④ 只补上一半列 → post_check 拦住，不盖章（下次启动重试）。"""
    real = ds._ensure_column

    def _only_first(c, tbl, col, ddl, **kw):
        if col == COLUMNS[0]:
            return real(c, tbl, col, ddl, **kw)
        return None

    monkeypatch.setattr(ds, "_ensure_column", _only_first)
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    present = _columns(stamped_db)
    assert COLUMNS[0] in present
    assert COLUMNS[2] not in present
    assert _stamped(stamped_db) == 0, "五列没补齐却盖章 = 版本号盖死、永不重试"


def test_non_owner_skips_without_stamping(monkeypatch):
    """⑤ 表不在本库（非 owner 库只看到跨库只读视图）→ 跳过不盖章，由 runtime 库收敛。"""
    conn = sqlite3.connect(":memory:")
    _seed_ledger(conn)
    conn.commit()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    ds._run_migrations_with_conn(conn, registry=[_mig()])
    assert conn.execute(
        "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?",
        (MIG_VERSION,)).fetchone()[0] == 0
    conn.close()


def test_unregistered_migration_does_not_touch_stamped_db(stamped_db):
    """⑥ 负向对照：不注册该迁移 → 存量库（老轨不跑）不会意外补出五列。

    这条证明「五列到位」只可能来自 202611250001，而不是某个被动兜底。
    """
    ds._run_migrations_with_conn(stamped_db, registry=[])
    assert not (set(COLUMNS) & _columns(stamped_db))
    assert _stamped(stamped_db) == 0