"""渠道族 + repo_credentials 的 DDL 归位守卫（2026-11-15）。

背景：渠道 7 张表的建表语句此前只存在于 `scripts/migrations/001_channel_tables.py`，
每个进程启动都自己建一份 → 三库模式下 mgmt/runtime/insight 各有一张同名基表
（演练库实测 17 个重复副本）。`grape_t_repo_credentials` 的 DDL 则留在
`src/api/db_setup.py` 的迁移函数里，而属主是 insight。

本文件钉住：
- 7 张渠道表的 DDL 在 owner 域（`src/domains/mgmt/schema_ddl.py`），并进入 `_NEW_TABLES`；
- `scripts/migrations/001` 引用同一份常量（不再自带副本，杜绝再次分裂）；
- `repo_credentials` 的 DDL 文本由 insight 域提供；
- 建表收敛闸门 `db_name_domain()`：单库（None）照建，分库只由 owner 建。
"""
import sqlite3

import pytest


CHANNEL_TABLES = (
    "grape_t_channel_installation",
    "grape_t_channel_chat_session_binding",
    "grape_t_channel_inbound_dedup",
    "grape_t_channel_sender_identity",
    "grape_t_channel_outbound_card",
    "grape_t_channel_lease",
    "grape_t_channel_inbound_message",
)


def test_channel_ddl_lives_in_mgmt_domain():
    from src.domains.mgmt.schema_ddl import CHANNEL_TABLE_DDL
    names = [n for n, _ in CHANNEL_TABLE_DDL]
    assert list(names) == list(CHANNEL_TABLES)
    for _name, ddl in CHANNEL_TABLE_DDL:
        assert "CREATE TABLE IF NOT EXISTS" in ddl


def test_channel_tables_registered_in_new_tables():
    from src.api import db_setup
    registered = {n for n, _ in db_setup._NEW_TABLES}
    missing = set(CHANNEL_TABLES) - registered
    assert not missing, f"渠道表未进入 _NEW_TABLES: {missing}"


def test_migration_001_reuses_owner_ddl():
    """001 迁移不得再内联建表语句（否则 owner 域又不是唯一来源）。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "scripts" / "migrations" / "001_channel_tables.py").read_text(encoding="utf-8")
    assert "CHANNEL_TABLE_DDL" in src
    assert "CREATE TABLE IF NOT EXISTS" not in src, "001 仍有内联 DDL"


def test_repo_credentials_ddl_lives_in_insight_domain():
    from src.api import db_setup
    from src.domains.insight.schema_ddl import _REPO_CREDENTIALS_DDL
    assert "grape_t_repo_credentials" in _REPO_CREDENTIALS_DDL
    fn_src = db_setup._fn_create_repo_credentials_table.__doc__ or ""
    assert "D99" in fn_src  # 迁移函数仍保留语义说明
    # 迁移函数体只引用常量，不再内联 CREATE TABLE
    import inspect
    body = inspect.getsource(db_setup._fn_create_repo_credentials_table)
    assert "_REPO_CREDENTIALS_DDL" in body
    assert "CREATE TABLE IF NOT EXISTS grape_t_repo_credentials" not in body


def test_ensure_channel_tables_creates_all_in_sqlite():
    import importlib.util
    import pathlib
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "scripts" / "migrations" / "001_channel_tables.py")
    spec = importlib.util.spec_from_file_location("mig001_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(":memory:")
    mod.ensure_channel_tables(conn)
    got = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert set(CHANNEL_TABLES) <= got
    cols = [r[1] for r in conn.execute("PRAGMA table_info(grape_t_channel_lease)")]
    assert "lease_token" in cols and "lease_expires_at" in cols


@pytest.mark.parametrize("db_name,expected", [
    ("grape_mgmt_db", "mgmt"),
    ("grape_stage_runtime", "runtime"),
    ("grape_stage_insight", "insight"),
    ("grape_ljt_db", None),      # 单库/共享库 → 每个进程都是 owner
    ("", None),
])
def test_db_name_domain(monkeypatch, db_name, expected):
    from src.kernel import table_ownership as to
    monkeypatch.setenv("DEVOPS_DB_NAME", db_name)
    assert to.db_name_domain() == expected