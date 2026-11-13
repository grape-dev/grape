"""S84 用例（D60，评审采纳）：评审收口三件——provider 派生统一 + 回落语义锁。

三块：
1. 结构守卫：token_resolver / user/db 无内联 provider 派生
   （`split(":", 1)[0]` 形状清零——S80b 回潮形状，D60 拍板收口）；
2. 行为锁：get_webhook_token_by_repo 裸键（owner/repo）回落 gitcode
   语义（parse_repo_key 单一入口的存量等价性）；
3. 依赖声明锁：rc 空串=无值→回落 managed_repos 列（D60 固化：
   列冻结期正确性依赖此回落，删除回落前必须先删列，顺序不可倒置）。

注：守卫 1 在内联收口代码落地前为 RED（tests-first 纪律；S84 代码
收口随批次 2 落地后转绿）。守卫 2/3 为常态回归锁，应恒绿。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock

GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture(autouse=True)
def _env():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE 's84%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 's84%'")
        db.commit()
    from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
    invalidate_platform_card_cache()
    from src.api.db_setup import _ensure_platform_storage_columns
    _ensure_platform_storage_columns()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE 's84%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 's84%'")
        db.commit()


# ---------- 1. 结构守卫：内联 provider 派生清零 ----------

_INLINE_DERIVATION = '.split(":", 1)[0] if ":" in'


def _module_source(modname: str) -> str:
    import importlib
    import inspect
    return inspect.getsource(importlib.import_module(modname))


def test_s84_no_inline_provider_derivation_token_resolver():
    """D60③：token_resolver 不得内联 split 派生 provider（S80b 回潮形状）。"""
    src = _module_source("src.kernel.token_resolver")
    assert _INLINE_DERIVATION not in src, (
        "token_resolver 存在内联 provider 派生（split(\":\",1)[0]）——"
        "统一走 src.kernel.repo_key.parse_repo_key（S77 语义锚点，D60③）")


def test_s84_no_inline_provider_derivation_user_db():
    """D60③：user/db 同守卫（get_webhook_token_by_repo 等读取点）。"""
    src = _module_source("src.domains.mgmt.user.db")
    assert _INLINE_DERIVATION not in src, (
        "user/db 存在内联 provider 派生——统一走 parse_repo_key（D60③）")


# ---------- 2. 行为锁：裸键回落 gitcode（parse_repo_key 存量等价） ----------

def test_s84_bare_key_gitcode_fallback_semantics():
    """裸键（owner/repo，无前缀）→ gitcode 语义保持（parse_repo_key 默认）。"""
    from src.kernel.repo_key import parse_repo_key
    assert parse_repo_key("owner/repo")[0] == "gitcode"
    assert parse_repo_key("gitee:owner/repo")[0] == "gitee"
    assert parse_repo_key("")[0] == "gitcode"


# ---------- 3. 依赖声明锁：rc 空串回落列（D60 固化顺序） ----------

def test_s84_rc_empty_means_missing_column_fallback():
    """rc 无行/空串=无值：get_repo_credential_value 返回 ''，
    列回落语义依赖此契约（D60④：删回落前先删列）。"""
    from src.domains.mgmt.user.db import get_repo_credential_value
    assert get_repo_credential_value("s84:ghost/repo", "bot") == ""  # D99 值域归一


def test_s84_rc_value_preferred_over_column():
    """rc 有值优先于列（S82 后 rc=唯一权威写目标；列=历史回落）。"""
    from src.domains.mgmt.user.db import get_repo_credential_value
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
            "VALUES ('%s', 's84:p/a', 'bot', 'rc-wins')" % str(__import__("uuid").uuid4()))  # D99: id+值域归一
        db.commit()
    try:
        assert get_repo_credential_value("s84:p/a", "bot") == "rc-wins"  # D99 值域归一
    finally:
        with db_lock:
            db.cursor().execute(
                "DELETE FROM grape_t_repo_credentials WHERE repo_key='s84:p/a'")
            db.commit()
