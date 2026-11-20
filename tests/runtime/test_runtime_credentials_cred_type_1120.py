# -*- coding: utf-8 -*-
"""runtime_credentials 缺 cred_type / platform 的回归（生产 1054）。

沙箱移植让凭据生成写这两列，但 schema_ddl 与迁移都没补，
旧库生成凭据报 (1054, "Unknown column 'cred_type' in 'field list'")。
"""
import sqlite3

_LEGACY_DDL = """
CREATE TABLE runtime_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    device_name TEXT DEFAULT '',
    scope TEXT DEFAULT 'user',
    owner_id TEXT DEFAULT '',
    owner_name TEXT DEFAULT '',
    repo_key TEXT DEFAULT NULL,
    credential_uid VARCHAR(36) DEFAULT NULL,
    status TEXT DEFAULT 'active',
    last_seen_at TEXT DEFAULT NULL,
    last_ip TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _legacy_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(_LEGACY_DDL)
    conn.execute("INSERT INTO runtime_credentials (token_hash,name) VALUES ('h','old')")
    conn.commit()
    return conn


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(runtime_credentials)")}


def test_migration_adds_cred_type_and_platform(monkeypatch):
    from src.api import db_setup

    monkeypatch.setattr(db_setup, "_owned_skip", lambda *a, **k: False)
    monkeypatch.setattr(db_setup, "db_lock", __import__("contextlib").nullcontext())
    conn = _legacy_conn()
    assert "cred_type" not in _cols(conn)

    db_setup._fn_runtime_cred_type_platform(conn=conn)

    cols = _cols(conn)
    assert "cred_type" in cols and "platform" in cols
    # 存量行按默认值落位
    row = conn.execute(
        "SELECT cred_type, platform FROM runtime_credentials WHERE name='old'").fetchone()
    assert row == ("worker", "")


def test_migration_is_idempotent(monkeypatch):
    from src.api import db_setup

    monkeypatch.setattr(db_setup, "_owned_skip", lambda *a, **k: False)
    monkeypatch.setattr(db_setup, "db_lock", __import__("contextlib").nullcontext())
    conn = _legacy_conn()
    db_setup._fn_runtime_cred_type_platform(conn=conn)
    db_setup._fn_runtime_cred_type_platform(conn=conn)   # 二次执行不得报错
    assert {"cred_type", "platform"} <= _cols(conn)


def test_fresh_schema_declares_both_columns():
    from src.domains.runtime import schema_ddl as sd

    ddl = "\n".join(v for v in vars(sd).values() if isinstance(v, str))
    start = ddl.index("CREATE TABLE IF NOT EXISTS runtime_credentials (")
    block = ddl[start:ddl.index("\n)", start)]
    assert "cred_type" in block and "platform" in block


def test_migration_raises_instead_of_stamping_when_ensure_column_fails(monkeypatch):
    """C1 回归：_ensure_column 失败必须抛给执行器（不盖章、下次启动重试）。

    历史事故同型：fn 吞异常 → 版本被钉死 → 列永不补齐 → 线上 1054 永久化。
    """
    import contextlib

    import pytest as _pytest

    from src.api import db_setup

    monkeypatch.setattr(db_setup, "_owned_skip", lambda *a, **k: False)
    monkeypatch.setattr(db_setup, "db_lock", contextlib.nullcontext())

    def _boom(*args, **kwargs):
        raise RuntimeError("MDL wait timeout (1205)")

    monkeypatch.setattr(db_setup, "_ensure_column", _boom)
    conn = _legacy_conn()
    with _pytest.raises(RuntimeError):
        db_setup._fn_runtime_cred_type_platform(conn=conn)
    assert "cred_type" not in _cols(conn)


def test_migration_passes_strict_to_ensure_column(monkeypatch):
    """降级到 COPY/人工清单时必须 raise（strict=True），不能静默返回。"""
    import contextlib

    from src.api import db_setup

    monkeypatch.setattr(db_setup, "_owned_skip", lambda *a, **k: False)
    monkeypatch.setattr(db_setup, "db_lock", contextlib.nullcontext())
    seen = []

    real = db_setup._ensure_column

    def _spy(c, tbl, col, ddl, **kw):
        seen.append((tbl, col, kw.get("strict")))
        return real(c, tbl, col, ddl, **kw)

    monkeypatch.setattr(db_setup, "_ensure_column", _spy)
    conn = _legacy_conn()
    db_setup._fn_runtime_cred_type_platform(conn=conn)
    assert seen == [("runtime_credentials", "cred_type", True),
                    ("runtime_credentials", "platform", True)], seen


def test_migration_raises_non_owner_object_when_table_absent(monkeypatch):
    """C1 第 2 轮：非 owner 库（表不存在）必须抛 _NonOwnerObject 跳过不盖章。

    否则 fn 正常返回 → 执行器在非 owner 库盖死版本号，而列并未建成。
    """
    import contextlib
    import sqlite3

    import pytest as _pytest

    from src.api import db_setup

    monkeypatch.setattr(db_setup, "_owned_skip", lambda *a, **k: False)
    monkeypatch.setattr(db_setup, "db_lock", contextlib.nullcontext())
    conn = sqlite3.connect(":memory:")          # 空的非 owner 库：没有 runtime_credentials
    with _pytest.raises(db_setup._NonOwnerObject):
        db_setup._fn_runtime_cred_type_platform(conn=conn)
