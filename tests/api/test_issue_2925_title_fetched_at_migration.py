"""Issue #2925 迁移 202611170043 行为锁：`issue.title_fetched_at` 补列。

口径：
- 创建：列不存在时补出 `title_fetched_at`（pipeline 标题兜底的「已尝试」标记）；
- 幂等：二次执行不重复加列、不报错；
- strict：补列失败必须抛出（迁移执行器据此**不盖章**），列不得被假绿。
"""
import contextlib
import sqlite3

import pytest

import src.api.db_setup as db_setup


def _cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(issue)").fetchall()]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "issue_2925_migration.db"))
    c.execute(
        "CREATE TABLE issue ("
        "id INTEGER PRIMARY KEY, repo_key TEXT, issue_number INTEGER, title TEXT)")
    c.commit()
    monkeypatch.setattr(db_setup, "db", c)
    monkeypatch.setattr(db_setup, "db_lock", contextlib.nullcontext())
    monkeypatch.setattr(db_setup, "_owned_skip", lambda *a, **k: False)
    yield c
    c.close()


def test_migration_creates_title_fetched_at(conn):
    assert "title_fetched_at" not in _cols(conn)
    db_setup._fn_add_issue_title_fetched_at()
    assert "title_fetched_at" in _cols(conn)


def test_migration_is_idempotent(conn):
    db_setup._fn_add_issue_title_fetched_at()
    db_setup._fn_add_issue_title_fetched_at()
    assert _cols(conn).count("title_fetched_at") == 1


def test_migration_strict_failure_raises_and_does_not_stamp(conn, monkeypatch):
    """strict 补列失败 → 异常向上抛出（执行器不盖章），列保持缺失。"""
    calls = []

    def _boom(c, tbl, col, ddl, **kw):
        calls.append((tbl, col, kw.get("strict")))
        raise RuntimeError("ensure column failed")

    monkeypatch.setattr(db_setup, "_ensure_column", _boom)
    with pytest.raises(RuntimeError):
        db_setup._fn_add_issue_title_fetched_at()

    assert calls == [("issue", "title_fetched_at", True)]
    assert "title_fetched_at" not in _cols(conn)