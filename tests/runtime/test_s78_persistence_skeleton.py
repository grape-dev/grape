"""S78 用例（D53）：持久化骨架验收——引擎 PoC / schema 漂移 / 字段值对账 /
DTO 守卫 / repr 脱敏 / UoW 事务边界。

运行时开关 GRAPE_PERSISTENCE_ORM 默认关闭——本文件直接测骨架本体
（不依赖开关；开关在 S79+ 切换调用方时生效）。
"""
import os

import pytest
from sqlalchemy import text


@pytest.fixture()
def orm_env(tmp_path, monkeypatch):
    """隔离 sqlite 库（内存形态走盖章后的最小建表：直接建四张表骨架）。"""
    from src.kernel.persistence.engine import get_engine
    get_engine.cache_clear()
    # 生产同款 onboarding 顺序：注册平台卡 → 补列口子（S64）→ schema 对账
    import json as _json
    from src.kernel.session_manager import db_lock, db
    from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (_json.dumps({"gitee": {"name": "Gitee", "dialect": "github", "host": "gitee.com",
                                    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
                                    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
                                    "webhook_verify": "token_compare", "clone_auth": "basic_password"}}),))
        db.commit()
    invalidate_platform_card_cache()
    from src.api.db_setup import _ensure_platform_storage_columns
    _ensure_platform_storage_columns()
    eng = get_engine()  # StaticPool：挂接 session_manager 同一连接（幽灵 inode 世界）
    yield eng
    get_engine.cache_clear()


# ------------------------------------------------ 引擎 PoC（桥接模式）

def test_s78_engine_poc_sqlite(orm_env):
    """S78 PoC：连接/事务/pool_pre_ping 与裸 SQL 路径同库同文件。"""
    from src.kernel.persistence.engine import get_engine
    eng = get_engine()
    # 引擎 PoC：creator+StaticPool 挂接共享连接（URL 形态 sqlite://），
    # 连通性与裸 SQL 同世界（同库同连接）
    from sqlalchemy import text as _t
    with eng.connect() as _c:
        assert _c.execute(_t("SELECT 1")).scalar() == 1
    with eng.begin() as c:
        v = c.execute(text("SELECT 1")).scalar()
    assert v == 1
    with eng.connect() as _c:  # creator+StaticPool：连通性即桥接 PoC（mysql 走 pool_pre_ping）
        assert _c.execute(_t("SELECT 1")).scalar() == 1


# ------------------------------------------------ schema 对账（防映射漂移）

def test_s78_schema_drift_zero(orm_env):
    """模型 metadata vs 实际表结构：列/可空/归一类型零漂移。"""
    from src.kernel.persistence.reconcile import schema_drift
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms, get_platform_card
    _dynamic = set()
    for _p in (list_vcs_platforms() or []):
        _c = get_platform_card(_p)
        if _c is None:
            continue
        for _col in (_c.token_storage_field, _c.webhook_token_field):
            if _col:
                _dynamic.add(_col)
    drift = schema_drift(orm_env, dynamic_columns=_dynamic)
    assert not drift, f"schema drift: {drift}"


# ------------------------------------------------ 字段值对账（裸 SQL 写 → ORM 读）

def test_s78_field_value_reconciliation(orm_env):
    from sqlalchemy import text
    from src.kernel.persistence.repositories import (account_repository,
                                              identity_repository,
                                              managed_repo_repository,
                                              user_credential_repository)
    from src.kernel.persistence.uow import uow
    eng = orm_env
    with eng.begin() as c:  # 裸 SQL 写（模拟存量路径）
        # D105 后 id 均为应用层生成的 TEXT 主键：裸行必须带显式 id——SQLite 的
        # TEXT PRIMARY KEY 容忍 NULL（方言怪癖）而 SQLAlchemy ORM 会静默丢弃
        # 主键为 NULL 的行；MySQL 的 TEXT 主键隐式 NOT NULL，缺 id 直接拒插。
        c.execute(text("INSERT INTO grape_accounts (id, username, password_hash) "
                       "VALUES ('s78-acc-1', 's78u', 'ph')"))
        c.execute(text("INSERT INTO external_identities (id, grape_user_id, provider, external_id, external_login, access_token) "
                       "VALUES ('s78-ei-1', '1', 'gitee', 'ext1', 'gl', 'tk')"))
        c.execute(text("INSERT INTO user_credentials (id, user_id, provider, config_json) "
                       "VALUES ('s78-uc-1', '1', 'gitee', '{\"k\":\"v\"}')"))
        c.execute(text("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_secret) VALUES ('gitee:o/r', 'o', 'r', 'o/r', 'sec')"))
    with uow() as s:
        a = account_repository.get_by_username(s, "s78u")
        i = identity_repository.get_by_platform(s, "gitee", "ext1")
        cr = user_credential_repository.get(s, 1, "gitee")
        mr = managed_repo_repository.get(s, "gitee:o/r")
    assert a and a.username == "s78u"
    assert i and i.access_token == "tk" and i.provider == "gitee"
    assert cr and cr.config_json == '{"k":"v"}'
    assert mr and mr.webhook_secret == "sec" and mr.full_name == "o/r"


# ------------------------------------------------ UoW 事务边界

def test_s78_uow_commit_and_rollback(orm_env):
    from sqlalchemy import text
    from src.kernel.persistence.uow import uow
    eng = orm_env
    with uow() as s:  # 正常提交
        s.execute(text("INSERT INTO grape_accounts (username, password_hash) VALUES ('u_ok', 'x')"))
    with pytest.raises(RuntimeError), uow() as s:
        s.execute(text("INSERT INTO grape_accounts (username, password_hash) VALUES ('u_bad', 'x')"))
        raise RuntimeError("boom")  # 触发 rollback
    with eng.begin() as c:
        names = [r[0] for r in c.execute(text("SELECT username FROM grape_accounts"))]
    assert "u_ok" in names and "u_bad" not in names


# ------------------------------------------------ DTO 守卫 + repr 脱敏

def test_s78_repo_return_shape_guard():
    """Repository 公开方法返回注解不得含 ORM 实体（lotus-core 先例）。"""
    from src.kernel.persistence.reconcile import return_shape_guard
    assert not return_shape_guard(), "ORM 实体穿透仓储边界"


def test_s78_sensitive_repr_masked(orm_env):
    """token/secret/password 字段 __repr__ 永不出现明文。"""
    from src.kernel.persistence.models import (AccountModel, ExternalIdentityModel,
                                        ManagedRepoModel)
    a = AccountModel(username="u", password_hash="PLAINTEXT_HASH")
    r = repr(a)
    assert "PLAINTEXT_HASH" not in r
    i = ExternalIdentityModel(access_token="SECRET_TOKEN", refresh_token="REFRESH_SECRET")
    assert "SECRET_TOKEN" not in repr(i) and "REFRESH_SECRET" not in repr(i)
    m = ManagedRepoModel(webhook_secret="HOOK_SECRET")
    assert "HOOK_SECRET" not in repr(m)


def test_s78_orm_switch_retired():
    """开关退役锁（S91 收口）：GRAPE_PERSISTENCE_ORM 开关与 orm_enabled 必须不存在，
    ORM 是身份域唯一持久化路径（engine 重绑跟随缺陷已由 S91 修复，退役重新落地）。"""
    import src.kernel.persistence.repositories as _repos
    assert not hasattr(_repos, "orm_enabled"), "GRAPE_PERSISTENCE_ORM 开关必须已退役"
