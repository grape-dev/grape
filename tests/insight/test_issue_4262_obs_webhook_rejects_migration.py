"""Issue #4262 / PR #2157 复审 C2 + R1：`grape_t_obs_webhook_rejects` 存量库注册式建表。

事故（C2）：本表此前**只**登记在 `db_setup._NEW_TABLES`，而 `_create_tables()` 只在新装库
首跑——存量库启动不跑它，于是 202611250006 的 `CREATE INDEX` 每次 1146（no such table）、
失败不盖章、每次启动重试，写侧只剩 `obs webhook rejects ingest failed` WARNING 丢诊断行。

本文件钉住：
1. 建表迁移（202611240016，insight 域，带 post_check）在册，且版本先于索引迁移
   （202611250006）——否则首次启动建索引仍撞缺表；
2. 索引迁移自己判表在位：表缺失时先建表再建索引（台账已盖章/建表被跳过的库也补齐）；
3. **台账已盖章 + 目标缺失**（R1）：同号同名已在版本表盖章、表与索引都不在的存量库，
   `verify_time_index` 必须判红（启动后置校验据此暴露漂移），补跑幂等 fn 后转绿；
4. `verify_migration_targets` 的 insight 检查项确实接线（漂移进启动失败清单）。
"""
import sqlite3

import pytest

from src.api import db_setup as D
from src.domains.insight.observability import webhook_rejects as wr

TABLE_MIG = "202611240016"
INDEX_MIG = "202611250006"


def _conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "mig.db"))
    conn.execute(D._SCHEMA_MIGRATIONS_DDL)
    return conn


def _stamp(conn, version, name):
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
        "VALUES (?, ?, 'safe', '2026-11-25 00:00:00')",
        (version, name))
    conn.commit()


def _table_exists(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (wr.TABLE,)).fetchone())


def _index_exists(conn):
    return D._index_present(conn.cursor(), wr.TABLE, wr.TIME_INDEX)


# ===== 1. 注册口径 =====

def test_registry_declares_table_migration_before_index_migration():
    reg = {m.version: m for m in D._MIGRATIONS}
    table = reg[TABLE_MIG]
    assert table.name == "create-obs-webhook-rejects-table"
    assert table.domains == ("insight",), "建表 SQL 归 insight 域（跨域守卫 P0-2）"
    assert callable(table.post_check), "建表迁移必须带后置校验（fn 返回≠目标到位）"

    index = reg[INDEX_MIG]
    assert index.name == "obs-webhook-rejects-time-repo-index"
    assert index.domains == ("insight",)
    assert callable(index.post_check)

    assert TABLE_MIG < INDEX_MIG, "建表迁移必须先于索引迁移执行（同批首次启动）"


def test_table_still_registered_in_new_tables_for_fresh_install():
    """新装库路径保持原样：`_NEW_TABLES` 仍然建表（双保险，不做减法）。"""
    from src.api import db_setup as _D
    assert wr.TABLE in {name for name, _ddl in _D._NEW_TABLES}


# ===== 2. 索引迁移自己判表在位（不再 1146）=====

def test_index_migration_creates_missing_table_then_index(tmp_path):
    conn = _conn(tmp_path)
    assert not _table_exists(conn) and not _index_exists(conn)

    wr.ensure_time_index(conn)

    assert _table_exists(conn), "表缺失时索引迁移须先建表（否则 1146 永不盖章）"
    assert _index_exists(conn)
    wr.verify_time_index(conn)          # 不抛 = 表 + 索引都在位


def test_index_migration_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    assert wr.ensure_time_index(conn) is True
    assert wr.ensure_time_index(conn) is False
    assert wr.ensure_table(conn) is False


# ===== 3. R1：台账已盖章 + 目标缺失（存量库路径）=====

def test_stamped_ledger_with_missing_targets_is_red_then_recovers(tmp_path):
    """同号同名已盖章、表与索引却都不在 → 后置校验判红；补跑幂等 fn 后转绿。

    这条覆盖的是「按 version 判 pending」永远修不到的路径：执行器看到版本已盖章就不跑，
    只有 post_check / 启动后置校验能让漂移可见。
    """
    conn = _conn(tmp_path)
    _stamp(conn, TABLE_MIG, "create-obs-webhook-rejects-table")
    _stamp(conn, INDEX_MIG, "obs-webhook-rejects-time-repo-index")
    assert not _table_exists(conn)

    with pytest.raises(RuntimeError, match="缺失"):
        wr.verify_time_index(conn)

    wr.ensure_table(conn)
    wr.verify_table(conn)               # 表到位 → 建表迁移的后置校验转绿
    with pytest.raises(RuntimeError, match=wr.TIME_INDEX):
        wr.verify_time_index(conn)      # 索引仍未建 → 索引迁移后置校验继续判红

    wr.ensure_time_index(conn)
    wr.verify_time_index(conn)          # 两者都补齐才盖章


def test_verify_table_rejects_absent_table(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(RuntimeError, match=TABLE_MIG):
        wr.verify_table(conn)


# ===== 4. 启动后置校验接线 =====

def test_verify_migration_targets_reports_insight_drift(monkeypatch):
    """insight 检查项接进启动失败清单（否则「台账盖章但目标缺失」只剩日志静默）。"""
    def _boom(conn=None):
        raise RuntimeError("grape_t_obs_webhook_rejects 缺失")

    monkeypatch.setattr(wr, "verify_time_index", _boom)
    failures = D.verify_migration_targets(domain="insight")
    assert any("insight grape_t_obs_webhook_rejects" in f for f in failures), failures