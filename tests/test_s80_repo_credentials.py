"""S80 用例（D55，先测后改）：凭据域 grape_t_repo_credentials 单表 + 双写对账。

范围裁定（D53 ADR）："分表"=managed_repos 泛型平台列家族（bot_*_token/
*_webhook_token ×4 平台共 8 列）的列式等价收口——单表 grape_t_repo_credentials
（repo_key, cred_type, value），非水平分片。策略：**双写**（managed_repos
列保留，存量读取方零改动；grape_t_repo_credentials 由 add_managed_repo 同事务
写入）+ 存量数据 backfill（幂等，只填空）。

读取方切换（TokenResolver/managed repo 查询族）留 S80b/批次 1——本批
只落表+写路径+对账（评审定稿：分表映射 PoC 在骨架阶段解决）。
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
def _gitee_card_and_clean():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE 's80%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 's80%'")
        db.commit()
    invalidate_platform_card_cache()
    from src.api.db_setup import _ensure_platform_storage_columns
    _ensure_platform_storage_columns()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key LIKE 's80%'")
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key LIKE 's80%'")
        db.commit()
    invalidate_platform_card_cache()


def _rc_rows(repo_key):
    with db_lock:
        return db.cursor().execute(
            "SELECT cred_type, value FROM grape_t_repo_credentials WHERE repo_key=? ORDER BY cred_type",
            (repo_key,)).fetchall()


def test_s80_repo_credentials_table_exists():
    """表由盖章迁移创建（patrol 先例：safe 迁移补建存量库）。"""
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(grape_t_repo_credentials)")]
    assert {"repo_key", "cred_type", "value", "updated_at"} <= set(cols), cols


def test_s80_add_managed_repo_dual_write():
    """add_managed_repo 写平台列时同事务双写 grape_t_repo_credentials 行。"""
    from src.domains.mgmt.user.db import add_managed_repo
    assert add_managed_repo("s80:o/r", "o", "r", webhook_id=1,
                            **{"bot_gitee_token": "bt-1", "gitee_webhook_token": "wt-1"})
    rows = dict(_rc_rows("s80:o/r"))
    # D99 归一：列名→cred_type 值域 {'bot','webhook'}；N3：值通道加密，读侧解密回读
    from src.domains.mgmt.auth.credential_crypto import decrypt
    assert decrypt(rows.get("bot") or "") == "bt-1"
    assert decrypt(rows.get("webhook") or "") == "wt-1"


def test_s80_dual_write_non_platform_columns_no_rows():
    """非凭据列（webhook_secret/dev_scene 等）不进 grape_t_repo_credentials（职责边界）。"""
    from src.domains.mgmt.user.db import add_managed_repo
    add_managed_repo("s80:o2/r", "o", "r", webhook_id=1, webhook_secret="ws",
                     dev_scene="scene-a")
    rows = dict(_rc_rows("s80:o2/r"))
    assert rows == {}, f"非凭据列不应双写: {rows}"


def test_s80_repo_credential_repository_crud():
    from src.persistence.uow import uow
    from src.persistence.repositories import repo_credential_repository as repo
    with uow() as s:
        repo.set(s, "s80:o3/r", "bot", "v1")  # D99 值域归一
        repo.set(s, "s80:o3/r", "webhook", "v2")  # D99 值域归一
    with uow() as s:
        assert repo.get(s, "s80:o3/r", "bot") == "v1"
        pairs = {r.cred_type: r.value for r in repo.list_by_repo(s, "s80:o3/r")}
    assert pairs == {"bot": "v1", "webhook": "v2"}
    with uow() as s:
        repo.delete(s, "s80:o3/r", "bot")
    with uow() as s:
        assert repo.get(s, "s80:o3/r", "bot") is None  # D99 值域归一


def test_s80_backfill_from_columns_idempotent():
    """存量库 backfill：列有值、rc 无行（S82 前的存量形态，raw SQL 造）→
    幂等回填；rc 已有值不覆盖。"""
    from src.api.db_setup import backfill_repo_credentials_from_columns
    with db_lock:  # 直接造存量形态（列有值；S82 起列冻结不再由代码写）
        # F4b/D106 适配：终态卡下冷启动不再建 gitee/gitlab 列——本用例模拟
        # 存量升级库形态，需先补出这两列（与 test_s82 fixture 同款）。
        _have = {r[1] for r in db.cursor().execute(
            "PRAGMA table_info(managed_repos)").fetchall()}
        for _c in ("bot_gitee_token", "gitee_webhook_token",
                   "bot_gitlab_token", "gitlab_webhook_token"):
            if _c not in _have:
                db.cursor().execute(
                    f"ALTER TABLE managed_repos ADD COLUMN {_c} TEXT DEFAULT ''")
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
            "bot_gitee_token, gitee_webhook_token) VALUES "
            "('s80:o4/r', 'o', 'r', 'o/r', 'bt-old', 'wt-old')")
        import uuid as _uuid
        db.cursor().execute(
            "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
            "VALUES (?, 's80:o4/r', 'webhook', 'preset')", (str(_uuid.uuid4()),))  # D99 目标态
        db.commit()
    backfill_repo_credentials_from_columns()
    rows = dict(_rc_rows("s80:o4/r"))
    assert rows.get("bot") == "bt-old"       # 从列回填（D99 归一 bot 值域）
    assert rows.get("webhook") == "preset"   # 已有值不被覆盖
    backfill_repo_credentials_from_columns()             # 幂等
    assert dict(_rc_rows("s80:o4/r")) == rows
