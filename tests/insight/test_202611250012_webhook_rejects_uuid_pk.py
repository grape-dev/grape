"""迁移 202611250012（grape_t_obs_webhook_rejects UUID 主键）回归。

PR #2245 复审 SUP-1：迁移在「DROP 被清理门槛拦截 → raise 不盖章 → 下次启动重试」
路径上必须幂等——主键由旧行确定性派生，重试只能落回同一 PK，不能把台账行搬成两倍。
本文件把四条不变量固定下来：建新表、确定性搬行、被拦时不盖章、重试不重复。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.insight.migrations import webhook_rejects_uuid_pk as mig  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:issue2245/sup1"


def _reset_tables():
    """清场：只动本用例的旧表/新表与测试行（repo_key 前缀 issue2245 之外一律不碰）。"""
    with db_lock:
        c = db.cursor()
        c.execute(f"DROP TABLE IF EXISTS {mig.NEW_TABLE}")
        c.execute(f"DROP TABLE IF EXISTS {mig.OLD_TABLE}")
        c.execute(f"""CREATE TABLE {mig.OLD_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_key TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            token_fp TEXT NOT NULL DEFAULT '',
            expected_fp TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )""")
        db.commit()


def _seed(n: int = 3):
    with db_lock:
        c = db.cursor()
        for i in range(n):
            c.execute(
                f"INSERT INTO {mig.OLD_TABLE} (repo_key, provider, event, ip, token_fp,"
                " expected_fp, reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (RK, "gitcode", f"Push-{i}", "203.0.113.9", "sha256:aaaa", "sha256:bbbb",
                 "mismatch", f"2026-11-25T00:0{i}:00+00:00"))
        db.commit()


def _count(table: str) -> int:
    with db_lock:
        return int(db.cursor().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _ids(table: str):
    with db_lock:
        return [r[0] for r in db.cursor().execute(f"SELECT id FROM {table} ORDER BY id").fetchall()]


@pytest.fixture(autouse=True)
def _clean():
    _reset_tables()
    yield
    _reset_tables()


def test_creates_new_table_and_moves_rows_deterministically():
    _seed(3)
    moved = mig.migrate()
    assert moved == 3
    assert _count(mig.NEW_TABLE) == 3
    with db_lock:
        present = {r[0] for r in db.cursor().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert mig.NEW_TABLE in present
    assert mig.OLD_TABLE not in present, "搬完必须删掉旧表"

    # 主键由 (旧 id, 固定命名空间) 确定性派生
    import uuid
    expected = {uuid.uuid5(mig._ID_NAMESPACE, f"{mig.OLD_TABLE}:{i}").hex for i in (1, 2, 3)}
    assert set(_ids(mig.NEW_TABLE)) == expected


def test_no_old_table_is_noop():
    assert mig.migrate() == 0
    with db_lock:
        present = {r[0] for r in db.cursor().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert mig.NEW_TABLE in present, "即使没有旧表也要把新表建好（存量库路径）"
    assert _count(mig.NEW_TABLE) == 0


def test_drop_blocked_raises_and_retry_does_not_duplicate(monkeypatch):
    _seed(3)
    from src.api import db_setup as ds

    monkeypatch.setattr(ds, "_destructive_ddl", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        mig.migrate()
    # 被拦时新表已有行，但旧表仍在（不盖章 → 下次启动重试）
    assert _count(mig.NEW_TABLE) == 3
    with db_lock:
        present = {r[0] for r in db.cursor().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert mig.OLD_TABLE in present

    # 重试（门槛放行）→ 同一批行落回同一主键，行数不翻倍，旧表被删
    monkeypatch.undo()
    moved = mig.migrate()
    assert _count(mig.NEW_TABLE) == 3, "重试不得产生重复台账行（SUP-1）"
    assert moved == 0, "全部命中既有主键时 rowcount 为 0"
    with db_lock:
        present = {r[0] for r in db.cursor().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert mig.OLD_TABLE not in present


def test_converged_reports_missing_objects():
    mig.migrate()
    with db_lock:
        c = db.cursor()
        c.execute(f"DROP TABLE IF EXISTS {mig.NEW_TABLE}")
        db.commit()
    with pytest.raises(RuntimeError):
        mig.converged()