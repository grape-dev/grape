"""S80b 用例（D55 续，先测后改）：凭据读取方切换——grape_t_repo_credentials 优先，
managed_repos 列回落（双写期两源一致；灰度开关 GRAPE_PERSISTENCE_ORM）。

覆盖两个读取点：
1. TokenResolver.RepoBotToken._fetch（bot token 策略链第 2 级）；
2. get_webhook_token_by_repo（webhook token 回落链，卡平台 {p}_webhook_token）。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


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
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE 's80b%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 's80b%'")
        db.commit()
    invalidate_platform_card_cache()
    from src.api.db_setup import _ensure_platform_storage_columns
    _ensure_platform_storage_columns()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE 's80b%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 's80b%'")
        db.commit()
    invalidate_platform_card_cache()
    import os
    os.environ.pop("GRAPE_PERSISTENCE_ORM", None)


def _setup_repo(repo_key, col_val, rc_val, col="bot_gitee_token", cred="bot"):  # D99: rc 值域归一
    with db_lock:
        o, r = repo_key.split(":", 1)[1].split("/", 1)
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, "
            f"full_name, {col}) VALUES (?, ?, ?, ?, ?)",
            (repo_key, o, r, f"{o}/{r}", col_val))
        if rc_val is not None:
            db.cursor().execute(
                "INSERT OR REPLACE INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
                "VALUES (?, ?, ?, ?)", (str(__import__("uuid").uuid4()), repo_key, cred, rc_val))  # D99: id 主键
        db.commit()


def test_s80b_repobottoken_reads_repo_credentials_first():
    """flag on：grape_t_repo_credentials 有值优先（即使与列不同）；flag off：回落列。"""
    import os
    from src.kernel.token_resolver import RepoBotToken, create_provider
    _setup_repo("s80b:o1/r1", "col-tok", "rc-tok")
    s = RepoBotToken(create_provider("gitee"))
    # S82：rc 唯一权威（无条件，列=历史回落）
    assert s._fetch("s80b:o1/r1") == "rc-tok"


def test_s80b_repobottoken_fallback_to_column_when_rc_empty():
    """grape_t_repo_credentials 无行 → 回落 managed_repos 列（双写期两源兜底）。"""
    import os
    from src.kernel.token_resolver import RepoBotToken, create_provider
    _setup_repo("s80b:o2/r2", "col-tok-2", None)
    s = RepoBotToken(create_provider("gitee"))
    assert s._fetch("s80b:o2/r2") == "col-tok-2"  # rc 空回落列（历史数据）


def test_s80b_webhook_token_card_platform():
    """get_webhook_token_by_repo：gitee 仓（卡平台）读 {p}_webhook_token
    credential 优先，列回落（原实现只认 gitcode/github 两列）。"""
    import os
    from src.domains.mgmt.user.db import get_webhook_token_by_repo
    _setup_repo("gitee:acme/r3", "", "wt-rc", col="gitee_webhook_token",
                cred="webhook")  # D99: rc 值域归一
    # S82：rc 唯一权威（无条件）
    assert get_webhook_token_by_repo("gitee:acme/r3") == "wt-rc"
