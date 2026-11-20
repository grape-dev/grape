"""D109 凭据迁移与回填（UC-D109-23/24）：meta→表、bot 账号、关系回填、对账、derived_from。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.domains.mgmt.auth.credential_crypto import decrypt
from src.kernel.persistence.repositories_platform_object import (
    OAuthChannelRepo,
    RepoPlatformRepo,
    UserRepoPlatformLinkRepo,
)
from src.domains.platform import importer
from src.domains.platform.migration import (
    backfill_user_platform_links,
    estimate_derived_from,
    migrate_platform_bots,
    migrate_oauth_client_credentials,
    reconcile_visible_platforms,
    run_all,
)

_EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_repo_credentials (
    id VARCHAR(256) PRIMARY KEY, repo_key TEXT NOT NULL, cred_type TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT '', derived_from TEXT NOT NULL DEFAULT '',
    UNIQUE (repo_key, cred_type));
CREATE TABLE IF NOT EXISTS user_credentials (
    id VARCHAR(191) NOT NULL DEFAULT '', user_id TEXT NOT NULL, provider TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '', config_json TEXT DEFAULT '{}', updated_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS external_identities (
    id TEXT, grape_user_id TEXT NOT NULL, provider VARCHAR(32) NOT NULL,
    external_id VARCHAR(128) NOT NULL DEFAULT '', external_login VARCHAR(128) DEFAULT '',
    access_token TEXT DEFAULT '', status INTEGER DEFAULT 1, unbound_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')));
"""


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.executescript(_EXTRA_DDL)
    raw.commit()
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def meta_store(monkeypatch):
    store: dict[str, str] = {}
    import src.domains.mgmt.user.db as udb

    monkeypatch.setattr(udb, "get_meta_value", lambda k: store.get(k, ""), raising=False)
    monkeypatch.setattr(udb, "set_meta_value", lambda k, v: store.__setitem__(k, v), raising=False)
    return store


@pytest.fixture()
def seeded(session, tmp_path):
    """两个平台 + 一个纯登录渠道 + oauth 凭据在 meta。"""
    d = tmp_path / "configs"
    (d / "platforms").mkdir(parents=True)
    (d / "oauth-providers.json").write_text(json.dumps({
        "gitee": {"name": "Gitee"},
        "github": {"name": "GitHub"},
        "huawei": {"name": "华为"},
    }), encoding="utf-8")
    for p in ("gitee", "github"):
        (d / "platforms" / f"{p}.json").write_text(json.dumps({"name": p.title(), "dialect": "github"}), encoding="utf-8")
    importer.scan_and_apply(session, config_dir=str(d), apply=True)
    return str(d)


def test_oauth_client_credentials_move_to_encrypted_columns(session, meta_store, seeded):
    meta_store["oauth_gitee_client_id"] = "cid-123"
    meta_store["oauth_gitee_client_secret"] = "csecret-abc"
    n = migrate_oauth_client_credentials(session)
    assert n == 1
    row = OAuthChannelRepo.get(session, "gitee")
    assert row.client_id_enc and row.client_secret_enc
    assert decrypt(row.client_secret_enc) == "csecret-abc"
    assert decrypt(row.client_id_enc) == "cid-123"
    # 幂等：再跑不覆盖
    assert migrate_oauth_client_credentials(session) == 0
    # meta 值保留（停读不删，回滚可用）
    assert meta_store["oauth_gitee_client_secret"] == "csecret-abc"


def test_oauth_credentials_from_card_json_fallback(session, meta_store, seeded):
    meta_store["oauth_providers_config"] = json.dumps({"github": {"client_id": "gh-id", "client_secret": "gh-sec"}})
    migrate_oauth_client_credentials(session)
    row = OAuthChannelRepo.get(session, "github")
    assert decrypt(row.client_secret_enc) == "gh-sec"


def test_platform_bot_migration_writes_empty_slots_only(session, meta_store, seeded):
    meta_store["global_bot_gitee_token"] = "gc-token"
    meta_store["global_bot_gitee_username"] = "bot_a"
    meta_store["github_bot_username"] = "gh_bot"
    meta_store["github_bot_token"] = "gh-token"
    migrate_platform_bots(session)
    gc = RepoPlatformRepo.get(session, "gitee")
    assert gc.bot_username == "bot_a"
    assert decrypt(gc.bot_token_enc) == "gc-token"
    gh = RepoPlatformRepo.get(session, "github")
    assert gh.bot_username == "gh_bot" and decrypt(gh.bot_token_enc) == "gh-token"
    # 幂等：已有值不重写
    assert migrate_platform_bots(session) == 0


def test_backfill_pat_and_oauth_and_no_ghost_for_pure_login(session, meta_store, seeded):
    session.execute(text(
        "INSERT INTO user_credentials (id, user_id, provider, kind, config_json) "
        "VALUES ('c1','u1','gitee','pat','{}'), ('c2','u2','github_pat','','{}')"
    ))
    session.execute(text(
        "INSERT INTO external_identities (id, grape_user_id, provider, status, unbound_at) "
        "VALUES ('e1','u1','gitee',1,NULL), ('e2','u3','huawei',1,NULL), ('e3','u4','github',1,'2026-01-01')"
    ))
    stats = backfill_user_platform_links(session)
    assert stats.pat_links == 2      # 归位行 + 旧复合键行
    assert stats.oauth_links == 1    # 只有 u1 的 gitee（huawei 纯登录无 link → 跳过；u4 已解绑）
    r = UserRepoPlatformLinkRepo.get(session, "u1", "gitee")
    assert r is not None and r.revoked_at == ""  # 纯关系：有 PAT/身份即建行
    assert UserRepoPlatformLinkRepo.get(session, "u3", "huawei") is None  # 无幽灵关系


def test_reconcile_reports_missing(session, meta_store, seeded):
    session.execute(text(
        "INSERT INTO user_credentials (id, user_id, provider, kind, config_json) VALUES ('c1','u1','gitee','pat','{}')"
    ))
    before = reconcile_visible_platforms(session)
    assert before["ok"] is False and ("u1", "gitee") in before["missing"]
    backfill_user_platform_links(session)
    after = reconcile_visible_platforms(session)
    assert after["ok"] is True and after["missing_count"] == 0


def test_estimate_derived_from_matches_default_account_token(session, meta_store, seeded):
    meta_store["global_bot_gitee_token"] = "tok-gitee"
    meta_store["global_bot_gitee_username"] = "gitee-bot"
    migrate_platform_bots(session)
    from src.domains.mgmt.auth.credential_crypto import encrypt
    session.execute(text(
        "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) VALUES "
        "('r1','gitcode:o/a','bot',:v1), ('r2','gitcode:o/b','bot',:v2)"
    ), {"v1": encrypt("tok-gitee"), "v2": encrypt("someone-else")})
    n = estimate_derived_from(session)
    assert n == 1
    rows = dict(session.execute(text("SELECT repo_key, derived_from FROM grape_t_repo_credentials")).fetchall())
    assert rows["gitcode:o/a"] == "gitee:gitee-bot"
    assert rows["gitcode:o/b"] == ""


def test_run_all_end_to_end(session, meta_store, seeded):
    meta_store["oauth_gitee_client_id"] = "cid"
    meta_store["oauth_gitee_client_secret"] = "sec"
    meta_store["global_bot_gitee_token"] = "bot-tok"
    session.execute(text(
        "INSERT INTO user_credentials (id, user_id, provider, kind, config_json) VALUES ('c1','u1','gitee','pat','{}')"
    ))
    out = run_all(session)
    assert out["stats"]["oauth_channels"] == 1
    assert out["stats"]["platform_bots"] >= 1
    assert out["reconcile"]["ok"] is True
