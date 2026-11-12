"""S81 用例（D56，先测后改）：仓库域查询族接 ManagedRepoRepository
（GRAPE_PERSISTENCE_ORM 灰度，flag off=存量 raw SQL 语义）。

收口点：get_webhook_secret_by_repo_key_any（W3 通用面 + S77 repair-host
provider 反查的底层）。
"""
import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


@pytest.fixture(autouse=True)
def _gitee_card_and_columns():
    """生产同款 onboarding：注册卡 → S64 补列（ORM 模型含派生列）。"""
    import json as _json
    from src.api.db_setup import _ensure_platform_storage_columns
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (_json.dumps({"gitee": {"name": "Gitee", "dialect": "github", "host": "gitee.com",
                                    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
                                    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
                                    "webhook_verify": "token_compare", "clone_auth": "basic_password"}}),))
        db.commit()
    invalidate_platform_card_cache()
    _ensure_platform_storage_columns()
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE '%s81%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE '%s81%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE '%s81%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE '%s81%'")
        db.commit()
    invalidate_platform_card_cache()


def _insert(repo_key, secret):
    with db_lock:
        o, r = repo_key.split(":", 1)[1].split("/", 1)
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, "
            "full_name, webhook_secret) VALUES (?, ?, ?, ?, ?)",
            (repo_key, o, r, f"{o}/{r}", secret))
        db.commit()


def test_s81_webhook_secret_any_flag_on_orm():
    """flag on：走 Repository（同库同值——行为等价锁）。"""
    import os
    _insert("gitee:s81:o/r", "sec-1")
    from src.domains.mgmt.user.db import get_webhook_secret_by_repo_key_any
    os.environ["GRAPE_PERSISTENCE_ORM"] = "1"
    try:
        assert get_webhook_secret_by_repo_key_any("s81:o/r") == "sec-1"
    finally:
        os.environ.pop("GRAPE_PERSISTENCE_ORM", None)


def test_s81_webhook_secret_any_flag_off_parity():
    """flag off：存量 raw SQL 路径语义不变（回归锁）。"""
    _insert("gitcode:s81:o2/r2", "sec-2")
    from src.domains.mgmt.user.db import get_webhook_secret_by_repo_key_any
    assert get_webhook_secret_by_repo_key_any("s81:o2/r2") == "sec-2"


def test_s81_repository_find_by_full_name_any_provider():
    from src.kernel.persistence.uow import uow
    from src.kernel.persistence.repositories import managed_repo_repository as repo
    _insert("gitee:s81:o3/r3", "sec-3")
    _insert("gitcode:s81:o3/r3", "sec-4")
    with uow() as s:
        rows = repo.find_by_full_name_any_provider(s, "s81:o3/r3")
    secrets = {r.repo_key: r.webhook_secret for r in rows}
    assert secrets == {"gitee:s81:o3/r3": "sec-3", "gitcode:s81:o3/r3": "sec-4"}


def test_s81_repo_exists_and_webhook_secret():
    from src.kernel.persistence.uow import uow
    from src.kernel.persistence.repositories import managed_repo_repository as repo
    _insert("gitee:s81:o4/r4", "sec-5")
    with uow() as s:
        assert repo.exists(s, "gitee:s81:o4/r4") is True
        assert repo.exists(s, "gitee:s81:o4/none") is False
        assert repo.get_webhook_secret(s, "gitee:s81:o4/r4") == "sec-5"
