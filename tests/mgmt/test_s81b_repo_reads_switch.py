"""S81b 用例（D57）：managed_repos 单列查询助手直接切 Repository
（用户拍板：不加双路径开关，直接切）。

覆盖：managed_repo_exists（Issue #671 自愈链路判据）与
get_host_user_id（Issue #372 路径 B 降级链判据，17 调用点）。
"""
import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


@pytest.fixture(autouse=True)
def _gitee_card_and_clean():
    import json as _json
    from src.api.db_setup import _ensure_platform_storage_columns
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (_json.dumps({"gitee": {"name": "Gitee", "dialect": "github", "host": "gitee.com",
                                    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
                                    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
                                    "webhook_verify": "token_compare", "clone_auth": "basic_password"}}),))
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE '%s81b%'")
        db.commit()
    invalidate_platform_card_cache()
    _ensure_platform_storage_columns()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE '%s81b%'")
        db.commit()
    invalidate_platform_card_cache()


def _insert(repo_key, host_user_id=0):
    with db_lock:
        o, r = repo_key.split(":", 1)[1].split("/", 1)
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, "
            "full_name, host_user_id) VALUES (?, ?, ?, ?, ?)",
            (repo_key, o, r, f"{o}/{r}", host_user_id))
        db.commit()


def test_s81b_managed_repo_exists():
    from src.domains.mgmt.user.db import managed_repo_exists
    _insert("gitee:s81b:o/r")
    assert managed_repo_exists("gitee:s81b:o/r") is True
    assert managed_repo_exists("gitee:s81b:o/none") is False
    assert managed_repo_exists("") is False


def test_s81b_get_host_user_id():
    from src.domains.mgmt.user.db import get_host_user_id
    _insert("gitee:s81b:o2/r2", host_user_id=24)
    _insert("gitee:s81b:o3/r3", host_user_id=0)
    assert get_host_user_id("gitee:s81b:o2/r2") == 24   # 路径 B
    assert get_host_user_id("gitee:s81b:o3/r3") == 0    # 路径 A/未配置
    assert get_host_user_id("gitee:s81b:o9/none") == 0  # 仓库不存在
    assert get_host_user_id("") == 0
