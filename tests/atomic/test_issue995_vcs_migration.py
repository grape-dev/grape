"""Issue #995 补列迁移（202611060004）的回归用例（QA 审查 M2）。

覆盖四条执行语义，全部走生产同款执行器 `_run_migrations_with_conn`，
用 sqlite 内存库模拟「存量库已盖 legacy 帽、issue 表无 vcs 列」，不需要 MySQL：

1. 迁移已注册且 kind=safe；
2. 存量态跑一遍 → 两列建成 + 盖一行章；
3. 再跑一遍 → 幂等，不重复盖章；
4. 补列失败（含 QA M1 的「_ensure_column 静默返回」场景）→ 抛错且不盖章。
"""
import sqlite3

import pytest

from src.api import db_setup as ds

MIG_VERSION = "202611060004"


@pytest.fixture
def legacy_stamped_db(monkeypatch):
    """存量库模拟：版本表只有 legacy 盖帽，issue 表没有 vcs_* 两列。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY, repo_key TEXT, issue_number INTEGER)")
    conn.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('gitcode:x/y', 1)")
    conn.execute(
        "CREATE TABLE grape_t_schema_migrations ("
        "version TEXT PRIMARY KEY, name TEXT, kind TEXT, applied_at TEXT, "
        "duration_ms INTEGER DEFAULT 0, checksum TEXT DEFAULT '')")
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version,name,kind,applied_at) "
        "VALUES ('202610300001','legacy-run-all-migrations','legacy-stamp','2026-10-30 00:00:00')")
    conn.commit()

    import src.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    yield conn
    conn.close()


def _mig():
    return {m.version: m for m in ds._MIGRATIONS}[MIG_VERSION]


def _columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(issue)").fetchall()}


def _stamped(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?", (MIG_VERSION,)).fetchone()[0]


def test_registered_safe():
    """① 迁移已注册、kind=safe、与老轨分开。"""
    mig = _mig()
    assert mig.name == "issue995_issue_vcs_exists_columns"
    assert mig.kind == "safe"
    assert callable(mig.fn)
    assert "_migrate_issue_vcs_exists" not in ds.LEGACY_TRACK_FROZEN


def test_adds_columns_on_legacy_stamped_db(legacy_stamped_db):
    """② 存量态跑一遍：两列建成并盖章（老轨不执行也不影响）。"""
    assert _columns(legacy_stamped_db) == {"id", "repo_key", "issue_number"}
    ds._run_migrations_with_conn(legacy_stamped_db, registry=[_mig()])
    assert {"vcs_exists", "vcs_checked_at"} <= _columns(legacy_stamped_db)
    assert _stamped(legacy_stamped_db) == 1
    # 存量行默认值按语义应为「存在」
    assert legacy_stamped_db.execute("SELECT vcs_exists FROM issue").fetchone()[0] == 1


def test_second_run_is_idempotent(legacy_stamped_db):
    """③ 再跑一遍：0 pending，不重复盖章、不报错。"""
    ds._run_migrations_with_conn(legacy_stamped_db, registry=[_mig()])
    ds._run_migrations_with_conn(legacy_stamped_db, registry=[_mig()])
    assert _stamped(legacy_stamped_db) == 1


def test_no_stamp_when_ensure_column_raises(legacy_stamped_db, monkeypatch):
    """④a 补列直接抛错（strict 分支）→ 不盖章，下次启动重试。"""
    def _boom(*a, **kw):
        raise RuntimeError("simulated ALTER failure")

    monkeypatch.setattr(ds, "_ensure_column", _boom)
    # 执行器吞掉 fn 异常（跳过不盖章），因此这里不期望异常外泄，只断言「没盖章」
    ds._run_migrations_with_conn(legacy_stamped_db, registry=[_mig()])
    assert _stamped(legacy_stamped_db) == 0


def test_no_stamp_when_ensure_column_silently_skips(legacy_stamped_db, monkeypatch):
    """④b QA M1 场景：_ensure_column 只记人工清单、正常返回 → 回验兜住，仍不盖章。"""
    monkeypatch.setattr(ds, "_ensure_column", lambda *a, **kw: None)
    ds._run_migrations_with_conn(legacy_stamped_db, registry=[_mig()])
    assert _stamped(legacy_stamped_db) == 0
    assert "vcs_exists" not in _columns(legacy_stamped_db)