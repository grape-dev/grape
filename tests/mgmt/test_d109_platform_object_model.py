"""D109 平台对象模型：领域层 + Repository 行为锁（第一批）。

覆盖 UC-D109-01..27 的可离线子集：
- 关系表 link/revoke/复活（UC 10-15，2026-11-14 纯关系模型）
- canonical hash 稳定性（§8 启动扫描的判定依据）
- 配置派生 upsert 五语义（inserted/skipped/updated/manual_kept/restored）（UC 01/04/06/07）
- orphaned 软删与「无视 hash 恢复」、manual 不被覆盖（UC 06/07）
- bot：D110 起归平台行，账号目录用例已移出（见 tests/mgmt/test_d110_platform_bot.py）
- user_platform_link：加/减/撤销/复活（UC 10-15）
- channel_repo_link 整渠道替换（UC 16）
- DDL 幂等（连跑两次不报错）
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.kernel.persistence.repositories_platform_object import (
    ChannelRepoLinkRepo,
    OAuthChannelRepo,
    RepoPlatformRepo,
    UserRepoPlatformLinkRepo,
)
from src.domains.platform.canonical import canonical_hash, strip_doc_keys

_RC_DDL = (
    "CREATE TABLE IF NOT EXISTS grape_t_repo_credentials ("
    " id VARCHAR(256) PRIMARY KEY, repo_key TEXT NOT NULL, cred_type TEXT NOT NULL,"
    " value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " created_at TEXT NOT NULL DEFAULT '', derived_from TEXT NOT NULL DEFAULT '',"
    " UNIQUE (repo_key, cred_type))"
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.execute(_RC_DDL)
    raw.commit()
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ---------- 领域层 ----------

def test_user_repo_platform_link_object_is_relation_only():
    """关系对象只有 linked_at / revoked_at / reason —— 不再有 via_bits/status。"""
    from src.domains.platform.domain import UserRepoPlatformLink

    link = UserRepoPlatformLink(user_id="u1", repo_provider="gitee")
    assert link.active and link.linked_at == ""
    link.link(now="2026-11-14T00:00:00Z")
    assert link.active and link.linked_at == "2026-11-14T00:00:00Z"
    link.revoke(now="2026-11-14T01:00:00Z", reason="user_unbind")
    assert not link.active and link.revoked_reason == "user_unbind"
    link.link(now="2026-11-14T02:00:00Z")  # 复活：linked_at 保留首次
    assert link.active and link.linked_at == "2026-11-14T00:00:00Z"


def test_canonical_hash_stable_and_ignores_doc_keys():
    a = {"name": "Gitee", "scope": "user_info", "_readme": ["x"], "_fields": {"name": "y"}}
    b = {"scope": "user_info", "name": "Gitee"}
    assert canonical_hash(a) == canonical_hash(b)
    c = {"name": "Gitee", "scope": "user_info projects"}
    assert canonical_hash(a) != canonical_hash(c)
    assert strip_doc_keys({"a": 1, "_x": 2}) == {"a": 1}


# ---------- 配置派生 upsert 五语义 ----------

def test_import_upsert_semantics(session):
    fields = {"name": "Gitee", "auth_url": "https://gitee.com/oauth/authorize", "enabled": 0}  # enabled 应被保护
    h1 = canonical_hash(fields)
    assert OAuthChannelRepo.upsert_from_config(session, "gitee", fields, h1) == "inserted"
    row = OAuthChannelRepo.get(session, "gitee")
    assert row is not None and row.enabled == 1  # 新建默认启用（enabled 不来自配置）

    # hash 相同 → skip
    assert OAuthChannelRepo.upsert_from_config(session, "gitee", fields, h1) == "skipped"

    # hash 变 → update（定义字段覆盖）
    fields2 = dict(fields, auth_url="https://gitee.org/oauth/authorize")
    h2 = canonical_hash(fields2)
    OAuthChannelRepo.set_enabled(session, "gitee", False)
    assert OAuthChannelRepo.upsert_from_config(session, "gitee", fields2, h2) == "updated"
    row = OAuthChannelRepo.get(session, "gitee")
    assert row.auth_url == "https://gitee.org/oauth/authorize"
    assert row.enabled == 0  # 定义字段更新不碰 enabled
    assert row.config_hash == h2

    # manual 行不被文件覆盖
    row.source = "manual"
    session.flush()
    assert OAuthChannelRepo.upsert_from_config(session, "gitee", {"auth_url": "x"}, "h3") == "manual_kept"
    assert OAuthChannelRepo.get(session, "gitee").auth_url == "https://gitee.org/oauth/authorize"


def test_orphaned_soft_delete_and_restore_without_hash(session):
    h = canonical_hash({"name": "GitLab"})
    assert RepoPlatformRepo.upsert_from_config(session, "gitlab", {"name": "GitLab"}, h) == "inserted"
    # 文件里没有 → 软删
    assert RepoPlatformRepo.mark_orphaned(session, ["gitee"]) == ["gitlab"]
    row = RepoPlatformRepo.get(session, "gitlab")
    assert row.source == "orphaned" and row.enabled == 0
    # 内容未变加回（同 hash）→ 仍能恢复（无视 hash）
    assert RepoPlatformRepo.upsert_from_config(session, "gitlab", {"name": "GitLab"}, h) == "restored"
    row = RepoPlatformRepo.get(session, "gitlab")
    assert row.source == "config" and row.enabled == 1


def test_platform_row_carries_pat_and_bot_columns(session):
    """2026-11-14：原 caps 表的存活字段并入平台行（configs/repo-credentials.json 已删）。"""
    fields = {"name": "Gitee", "pat_create_url": "https://gitee.com/profile/personal_access_tokens",
              "bot_username": "tblbot", "token_storage_field": "", "webhook_token_field": "", "owner_fallback": 0}
    assert RepoPlatformRepo.upsert_from_config(session, "gitee", fields, canonical_hash(fields)) == "inserted"
    row = RepoPlatformRepo.get(session, "gitee")
    assert row.pat_create_url.endswith("/personal_access_tokens")
    assert row.bot_username == "tblbot" and row.owner_fallback == 0


def test_migration_merges_caps_and_drops_table(monkeypatch, tmp_path):
    """202611170010：caps 存活字段回填平台行 → 删 caps 表；幂等可重跑。"""
    import sqlite3

    import src.kernel.session_manager as sm
    from src.api import db_setup

    conn = sqlite3.connect(str(tmp_path / "merge.db"))
    conn.execute(
        "CREATE TABLE grape_t_repo_platforms (id TEXT PRIMARY KEY, provider TEXT, name TEXT,"
        " enabled INTEGER DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE grape_t_repo_credential_caps (provider TEXT PRIMARY KEY,"
        " pat_create_url TEXT, token_storage_field TEXT, webhook_token_field TEXT,"
        " bot_username TEXT, owner_fallback INTEGER)"
    )
    conn.execute("INSERT INTO grape_t_repo_platforms (id,provider,name,enabled) VALUES ('p1','gitee','Gitee',1)")
    conn.execute(
        "INSERT INTO grape_t_repo_credential_caps VALUES"
        " ('gitee','https://gitee.com/profile/personal_access_tokens','','','tblbot',1)"
    )
    conn.commit()
    monkeypatch.setattr(sm, "db", conn, raising=False)

    db_setup._fn_merge_credential_caps_into_platforms()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(grape_t_repo_platforms)")}
    assert {"pat_create_url", "token_storage_field", "webhook_token_field", "bot_username",
            "owner_fallback"} <= cols
    row = conn.execute(
        "SELECT pat_create_url, bot_username, owner_fallback FROM grape_t_repo_platforms"
        " WHERE provider='gitee'"
    ).fetchone()
    assert row[0].endswith("/personal_access_tokens") and row[1] == "tblbot" and row[2] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "grape_t_repo_credential_caps" not in tables
    db_setup._fn_merge_credential_caps_into_platforms()  # 幂等：caps 已删不报错
    conn.close()


# ---------- user_platform_link ----------

def test_user_repo_platform_link_lifecycle(session):
    """纯关系：link 幂等建行 / revoke 写时间与原因 / link 复活。"""
    repo = UserRepoPlatformLinkRepo
    repo.link(session, "u1", "gitee")
    row = repo.get(session, "u1", "gitee")
    assert row is not None and row.revoked_at == "" and row.linked_at
    first = row.linked_at
    repo.link(session, "u1", "gitee")  # 幂等，不改 linked_at
    assert repo.get(session, "u1", "gitee").linked_at == first
    repo.revoke(session, "u1", "gitee", "oauth_unbound")
    row = repo.get(session, "u1", "gitee")
    assert row.revoked_at and row.revoked_reason == "oauth_unbound"
    repo.link(session, "u1", "gitee")  # 复活
    row = repo.get(session, "u1", "gitee")
    assert row.revoked_at == "" and row.linked_at == first


def test_revoke_on_missing_row_is_noop(session):
    UserRepoPlatformLinkRepo.revoke(session, "nobody", "gitee", "user_unbind")
    assert UserRepoPlatformLinkRepo.get(session, "nobody", "gitee") is None


# ---------- channel_repo_link ----------

def test_channel_repo_link_merge_for_oauth(session):
    n = ChannelRepoLinkRepo.merge_for_oauth(
        session,
        "huawei",
        [
            {"repo_provider": "codearts"},
            {"repo_provider": "gitcode"},
        ],
    )
    assert n == 2
    rows = ChannelRepoLinkRepo.list_by_oauth(session, "huawei")
    assert {r.repo_provider for r in rows} == {"codearts", "gitcode"}
    # 再导入（只留一个）→ 整渠道替换
    ChannelRepoLinkRepo.merge_for_oauth(session, "huawei", [{"repo_provider": "codearts"}])
    rows = ChannelRepoLinkRepo.list_by_oauth(session, "huawei")
    assert [r.repo_provider for r in rows] == ["codearts"]


# ---------- DDL 幂等 ----------

def test_ddl_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.executescript(PLATFORM_OBJECT_DDL)
    conn.executescript(PLATFORM_OBJECT_DDL)  # 第二次不报错
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # D110：bot 账号目录退役（bot 归平台行），四张表、无部分唯一索引
    assert len([t for t in tables if t.startswith("grape_t_")]) == 4
    conn.close()

def test_migration_function_creates_tables_and_backfills_columns(monkeypatch, tmp_path):
    """迁移本体（202611170009）：建五表 + rc 补 created_at/derived_from，幂等可重跑。"""
    import sqlite3

    import src.kernel.session_manager as sm
    from src.api import db_setup

    conn = sqlite3.connect(str(tmp_path / "mig.db"))
    conn.execute(_RC_DDL)
    conn.commit()
    monkeypatch.setattr(sm, "db", conn, raising=False)

    db_setup._fn_create_platform_object_tables()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert len([t for t in tables if t.startswith("grape_t_")]) == 5  # 四张新表 + rc（D110 删 bot 表）
    rc_cols = {r[1] for r in conn.execute("PRAGMA table_info(grape_t_repo_credentials)")}
    assert {"created_at", "derived_from"} <= rc_cols
    # 幂等：再跑一次不报错、列不重复
    db_setup._fn_create_platform_object_tables()
    assert len([r for r in conn.execute("PRAGMA table_info(grape_t_repo_credentials)") if r[1] == "derived_from"]) == 1
    conn.close()
