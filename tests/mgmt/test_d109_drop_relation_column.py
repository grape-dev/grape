"""迁移 202611170011：删 grape_t_channel_repo_link.relation（档位退役）。

两个断言：
  1. 有列 → 迁移后列消失（幂等：再跑一遍不抛），行不丢；
  2. DROP 未实际生效（_destructive_ddl 吞异常的老行为）→ 迁移必须 raise，不得盖章。

隔离方式：把 session_manager 的 db/db_lock 换成临时 sqlite 连接（迁移函数在调用时
才 import 这两个符号，monkeypatch 生效），不依赖共享测试库的用例顺序。
"""
import os
import sqlite3

os.environ.setdefault("OBS_LOG_DB", "0")
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")


def _make_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "mig.db"))
    conn.execute(
        "CREATE TABLE grape_t_channel_repo_link ("
        " id TEXT PRIMARY KEY, oauth_provider TEXT NOT NULL, repo_provider TEXT NOT NULL,"
        " relation TEXT NOT NULL DEFAULT 'identity_only', source TEXT NOT NULL DEFAULT 'derived',"
        " config_hash TEXT NOT NULL DEFAULT '', created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO grape_t_channel_repo_link (id,oauth_provider,repo_provider,source)"
        " VALUES ('l1','gitee','gitee','derived'),('l2','gitee','codearts','manual')"
    )
    conn.commit()
    return conn


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _bind(monkeypatch, conn):
    import src.kernel.session_manager as sm

    monkeypatch.setattr(sm, "db", conn, raising=False)
    monkeypatch.setattr(sm, "db_lock", _NoLock(), raising=False)


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_repo_link)").fetchall()}


def test_drop_relation_column_is_effective_and_idempotent(tmp_path, monkeypatch):
    from src.api import db_setup

    conn = _make_conn(tmp_path)
    _bind(monkeypatch, conn)
    assert "relation" in _cols(conn)

    db_setup._fn_drop_channel_link_relation_column()

    assert "relation" not in _cols(conn), "迁移应删除 relation 列"
    assert conn.execute("SELECT count(*) FROM grape_t_channel_repo_link").fetchone()[0] == 2, "行不能丢"

    db_setup._fn_drop_channel_link_relation_column()  # 幂等：无列直接返回


def test_drop_relation_column_refuses_to_stamp_when_noop(tmp_path, monkeypatch):
    import pytest

    from src.api import db_setup

    conn = _make_conn(tmp_path)
    _bind(monkeypatch, conn)

    # 模拟 _destructive_ddl 吞异常（MySQL 上曾出现"盖章但列仍在"）
    monkeypatch.setattr(db_setup, "_destructive_ddl", lambda *a, **k: True)

    with pytest.raises(RuntimeError):
        db_setup._fn_drop_channel_link_relation_column()