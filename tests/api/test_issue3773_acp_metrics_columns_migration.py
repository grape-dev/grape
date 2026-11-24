"""Issue #3773 部署缺口回归：ACP state.db 治理指标 4 列必须走注册式迁移。

背景（部署验证实测缺口）
------------------------
`runtime_credentials` 的 ``acp_state_db_bytes`` / ``acp_state_db_cleanup_counts`` /
``acp_state_db_bytes_freed_total`` / ``acp_resume_skipped_total`` 原先只登记在
`src/domains/runtime/migrations_ddl.py::ALTERS_ENGINE_CREDENTIAL_COLUMNS` 与
`src/api/db_setup.py::_migrate_tables()` 的 ``_ensure_column`` 段，两个消费点都在
**冻结老轨**：

- 存量库已盖 legacy-stamp（202610300001），启动时老轨整段不执行 → 4 列永远不存在；
- worker ``/api/runtime/heartbeat`` 的 UPDATE 把这 4 列写在 SET 列表里 → 每条心跳
  1054 ``Unknown column 'acp_state_db_bytes'``，心跳与 ACP 指标全断（异常被吞）。

修复：4 列改走注册式迁移 ``202611240005``（kind=safe + post_check）。本文件把
「已盖章存量库补列」钉住。

覆盖（生产同款执行器 `_run_migrations_with_conn`，sqlite 内存库即可）：
1. 迁移已注册、kind=safe、domains=("runtime",)、有 post_check，且 4 列都不再出现在
   冻结老轨常量里；
2. 已盖章存量库跑一遍 → 4 列建成并盖一行章；
3. 再跑一遍 → 幂等，不重复盖章；
4. 只补上一半列 → post_check 拦住，不盖章（禁止「版本号盖死、列缺着」）；
5. 表不在本库（非 owner 域）→ 跳过且不盖章。
"""
import sqlite3

import pytest

from src.api import db_setup as ds

MIG_VERSION = "202611240005"
MIG_NAME = "issue3773-acp-state-db-metrics-columns"
TABLE = "runtime_credentials"
COLUMNS = (
    "acp_state_db_bytes",
    "acp_state_db_cleanup_counts",
    "acp_state_db_bytes_freed_total",
    "acp_resume_skipped_total",
)


@pytest.fixture
def stamped_db(monkeypatch):
    """存量库模拟：版本表只有 legacy 盖帽，runtime_credentials 没有 4 个 ACP 列。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE runtime_credentials ("
        "id INTEGER PRIMARY KEY, token_hash TEXT, name TEXT, cpu_percent REAL DEFAULT 0)")
    conn.execute(
        "INSERT INTO runtime_credentials (token_hash, name) VALUES ('h', 'worker-1')")
    conn.execute(
        "CREATE TABLE grape_t_schema_migrations ("
        "version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT, "
        "duration_ms INTEGER DEFAULT 0, checksum TEXT DEFAULT '')")
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version,name,kind,applied_at) "
        "VALUES ('202610300001','legacy-run-all-migrations','legacy-stamp','2026-10-30 00:00:00')")
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


def test_registered_safe_and_not_on_frozen_track():
    """① 注册式迁移在位，且 4 列都已从冻结老轨常量撤出。"""
    mig = _mig()
    assert mig.name == MIG_NAME
    assert mig.kind == "safe"
    assert callable(mig.fn)
    assert callable(mig.post_check), "4 列存在性可判定，必须带 post_check（Issue #3648）"
    assert tuple(mig.domains) == ("runtime",), "runtime_credentials 归 runtime"

    from src.domains.runtime import migrations_ddl as runtime_mig

    frozen = " ".join(runtime_mig.ALTERS_ENGINE_CREDENTIAL_COLUMNS)
    for col in COLUMNS:
        assert col not in frozen, (
            f"{col} 又回到了冻结老轨 ALTERS_ENGINE_CREDENTIAL_COLUMNS：该常量只被 "
            "_migrate_tables() 消费，已盖章存量库永不执行 → 心跳 UPDATE 1054 Unknown"
            " column。新增列请只登记 register_migration()。"
        )


def test_stamped_db_gets_columns_and_is_stamped(stamped_db):
    """② 已盖章存量库：老轨不执行也能补出 4 列，并盖一行章。"""
    assert not (set(COLUMNS) & _columns(stamped_db))
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert set(COLUMNS) <= _columns(stamped_db), (
        "已盖章存量库未补列 → 心跳 UPDATE 将 1054 Unknown column")
    assert _stamped(stamped_db) == 1
    row = stamped_db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM {TABLE}").fetchone()
    assert row[0] in (0, 0.0) and row[2] in (0, 0.0) and row[3] in (0, 0)


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
    assert _stamped(stamped_db) == 0, "4 列没补齐却盖章 = 版本号盖死、永不重试"


def test_non_owner_skips_without_stamping(monkeypatch):
    """⑤ 表不在本库（非 owner 域）→ 跳过不盖章，由 runtime 库收敛。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE grape_t_schema_migrations ("
        "version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT, "
        "duration_ms INTEGER DEFAULT 0, checksum TEXT DEFAULT '')")
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version,name,kind,applied_at) "
        "VALUES ('202610300001','legacy-run-all-migrations','legacy-stamp','2026-10-30 00:00:00')")
    conn.commit()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    ds._run_migrations_with_conn(conn, registry=[_mig()])
    assert conn.execute(
        "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?",
        (MIG_VERSION,)).fetchone()[0] == 0
    conn.close()