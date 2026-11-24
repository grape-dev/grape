"""Issue #3731 部署缺口回归：`convert_fail_count` 必须走注册式迁移。

背景（部署验证实测缺口）
------------------------
该列原先只登记在 `src/domains/mgmt/migrations_ddl.py::ALTERS_USER_FEEDBACK_COLUMNS`，
而该常量只被**冻结老轨** `db_setup._migrate_tables()` 消费：

- 存量库的 ``grape_t_schema_migrations`` 早已被 legacy-stamp 盖帽（202610300001），
  启动时老轨整段不执行 → 库里永远没有这一列；
- 而 `mgmt.public` 的 ``feedback_pending_rows`` 已经把 ``convert_fail_count`` 写进
  SELECT → 转换读侧首轮即 1054 Unknown column，反馈→Issue 转换继续全停。

修复：列改走注册式迁移 ``202611240004``（kind=safe + post_check），本文件把
「已盖章存量库升列」这件事钉住，避免同类缺口再次静默上线。

覆盖（全部走生产同款执行器 `_run_migrations_with_conn`，sqlite 内存库即可）：
1. 迁移已注册、kind=safe、domains=("mgmt",)、有 post_check，且不再是冻结老轨常量；
2. 已盖章存量库跑一遍 → 列建成并盖一行章；
3. 再跑一遍 → 幂等，不重复盖章、不报错；
4. 补列未生效（`_ensure_column` 静默跳过）→ post_check 拦住，不盖章；
5. 表不在本库（非 owner 域）→ 跳过且不盖章（等 mgmt 库收敛）。
"""
import sqlite3

import pytest

from src.api import db_setup as ds

MIG_VERSION = "202611240004"
MIG_NAME = "issue3731-user-feedback-convert-fail-count"
TABLE = "user_feedback"
COLUMN = "convert_fail_count"


@pytest.fixture
def stamped_db(monkeypatch):
    """存量库模拟：版本表只有 legacy 盖帽，user_feedback 没有 convert_fail_count 列。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE user_feedback ("
        "id INTEGER PRIMARY KEY, repo_key TEXT, title TEXT, issue_url TEXT, "
        "is_deleted INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "INSERT INTO user_feedback (repo_key, title, issue_url) "
        "VALUES ('gitcode:grape-dev/grape', 't', '')")
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
    """① 注册式迁移在位，且已从冻结老轨常量（_migrate_tables 消费点）撤出。"""
    mig = _mig()
    assert mig.name == MIG_NAME
    assert mig.kind == "safe"
    assert callable(mig.fn)
    assert callable(mig.post_check), "列存在性可判定，必须带 post_check（Issue #3648）"
    assert tuple(mig.domains) == ("mgmt",), "user_feedback 归 mgmt（三库拓扑下由 mgmt 库补列）"

    from src.domains.mgmt import migrations_ddl as mgmt_mig

    frozen = " ".join(mgmt_mig.ALTERS_USER_FEEDBACK_COLUMNS)
    assert COLUMN not in frozen, (
        f"{COLUMN} 又回到了冻结老轨 ALTERS_USER_FEEDBACK_COLUMNS：该常量只被 "
        "_migrate_tables() 消费，已盖章存量库永远不执行 → 线上 1054 Unknown column。"
        " 新增列请只登记 register_migration()。"
    )


def test_stamped_db_gets_column_and_is_stamped(stamped_db):
    """② 已盖章存量库：老轨不执行也能补列，并盖一行章。"""
    assert COLUMN not in _columns(stamped_db)
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert COLUMN in _columns(stamped_db), "已盖章存量库未补列 → 读侧 SELECT 将 1054"
    assert _stamped(stamped_db) == 1
    # 存量行按语义应为 0（连败计数从零起算）
    assert stamped_db.execute(
        f"SELECT {COLUMN} FROM {TABLE}").fetchone()[0] == 0


def test_second_run_is_idempotent(stamped_db):
    """③ 再跑一遍：0 pending，不重复盖章、不报错。"""
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert _stamped(stamped_db) == 1
    assert COLUMN in _columns(stamped_db)


def test_no_stamp_when_column_still_missing(stamped_db, monkeypatch):
    """④ 补列没生效（静默跳过）→ post_check 拦住，不盖章（下次启动重试）。"""
    monkeypatch.setattr(ds, "_ensure_column", lambda *a, **kw: None)
    ds._run_migrations_with_conn(stamped_db, registry=[_mig()])
    assert COLUMN not in _columns(stamped_db)
    assert _stamped(stamped_db) == 0, "列没建成却盖章 = 版本号盖死、永不重试"


def test_non_owner_skips_without_stamping(monkeypatch):
    """⑤ 表不在本库（非 owner 域）→ 跳过不盖章，由 mgmt 库收敛。"""
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