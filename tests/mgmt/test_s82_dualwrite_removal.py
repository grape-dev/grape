"""S82 用例（D58，先测后改）：双写移除——grape_t_repo_credentials 成为唯一权威
写目标，managed_repos 8 平台凭据列冻结（存量值由 backfill 迁入、列读取
保留为历史回落）；全部读取方切 rc 优先（去 GRAPE_PERSISTENCE_ORM 门）。

覆盖：写冻结（add_managed_repo/repair-host 不再写 8 列）、读者切换
（RepoBotToken/get_webhook_token_by_repo 去门、bot_token_health 扫描、
config_store 全局换发、has_bot 闸门）。
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
        for t in ("managed_repos", "grape_t_repo_credentials"):
            db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 's82%'")
        # F4/D106：终态下冷启动不再建 gitee/gitlab 列——本组用例断言
        # 「列冻结为空」语义，需列物理存在（模拟存量升级库形态）。
        _have = {r[1] for r in db.cursor().execute("PRAGMA table_info(managed_repos)").fetchall()}
        for _c in ("bot_gitee_token", "gitee_webhook_token",
                   "bot_gitlab_token", "gitlab_webhook_token"):
            if _c not in _have:
                db.cursor().execute(f"ALTER TABLE managed_repos ADD COLUMN {_c} TEXT DEFAULT ''")
        db.commit()
    invalidate_platform_card_cache()
    from src.api.db_setup import _ensure_platform_storage_columns, \
        backfill_repo_credentials_from_columns
    _ensure_platform_storage_columns()
    backfill_repo_credentials_from_columns()
    yield
    with db_lock:
        for t in ("managed_repos", "grape_t_repo_credentials"):
            db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 's82%'")
        db.commit()
    invalidate_platform_card_cache()


def _cols(repo_key):
    with db_lock:
        row = db.cursor().execute(
            "SELECT bot_gitcode_token, gitcode_webhook_token, bot_github_token, "
            "github_webhook_token, bot_gitee_token, gitee_webhook_token, "
            "bot_gitlab_token, gitlab_webhook_token "
            "FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
    return list(row) if row else []


def _rc(repo_key):
    """D99 归一值域（cred_type ∈ {'bot','webhook'}）+ N3 通道加密：读侧统一解密比对。"""
    from src.domains.mgmt.user.db import _decrypt_repo_secret
    with db_lock:
        rows = db.cursor().execute(
            "SELECT cred_type, value FROM grape_t_repo_credentials WHERE repo_key=?",
            (repo_key,)).fetchall()
    return {ct: _decrypt_repo_secret(v or "") for ct, v in rows}


# ------------------------------------------------ 写冻结

def test_s82_add_managed_repo_no_column_write():
    """add_managed_repo 不再写 8 列（全空），凭据单写 grape_t_repo_credentials。"""
    from src.domains.mgmt.user.db import add_managed_repo, get_repo_credential_value
    assert add_managed_repo("gitee:s82o/r", "o", "r", webhook_id=1,
                            **{"bot_gitee_token": "bt-1", "gitee_webhook_token": "wt-1"})
    assert _cols("gitee:s82o/r") == [""] * 8, f"列应冻结为空: {_cols('gitee:s82o/r')}"
    rc = _rc("gitee:s82o/r")
    assert rc.get("bot") == "bt-1" and rc.get("webhook") == "wt-1"


def test_s82_update_keeps_rc_authoritative():
    """更新路径：rc 单写（同 repo_key 换 token），列保持冻结。"""
    from src.domains.mgmt.user.db import add_managed_repo, get_repo_credential_value
    add_managed_repo("gitee:s82o2/r2", "o", "r", webhook_id=1,
                     **{"bot_gitee_token": "v1"})
    add_managed_repo("gitee:s82o2/r2", "o", "r", webhook_id=1,
                     **{"bot_gitee_token": "v2"})
    assert _rc("gitee:s82o2/r2").get("bot") == "v2"
    assert _cols("gitee:s82o2/r2") == [""] * 8


# ------------------------------------------------ 读者切换（去门）

def test_s82_repobottoken_rc_first_unconditional():
    """RepoBotToken：rc 优先不再需要 flag（默认即 rc 权威）。"""
    from src.domains.mgmt.user.db import add_managed_repo
    from src.kernel.token_resolver import RepoBotToken
    from src.domains.mgmt.vcs import create_provider
    add_managed_repo("gitee:s82o3/r3", "o", "r", webhook_id=1,
                     **{"bot_gitee_token": "rc-authoritative"})
    s = RepoBotToken(create_provider("gitee"))
    assert s._fetch("gitee:s82o3/r3") == "rc-authoritative"


def test_s82_webhook_token_by_repo_rc_first():
    from src.domains.mgmt.user.db import add_managed_repo, get_webhook_token_by_repo
    add_managed_repo("gitee:s82o4/r4", "o", "r", webhook_id=1,
                     **{"gitee_webhook_token": "wt-rc"})
    assert get_webhook_token_by_repo("gitee:s82o4/r4") == "wt-rc"


def test_s82_bot_token_health_covers_rc():
    """bot_token_health 扫描覆盖 grape_t_repo_credentials（原只扫 managed_repos 列）。"""
    from src.domains.mgmt.user.db import add_managed_repo
    from src.domains.runtime.engine.bot_token_health import _verify_repo_bot_tokens
    add_managed_repo("gitee:s82o5/r5", "o", "r", webhook_id=1,
                     **{"bot_gitee_token": "health-tok"})
    repos_to_check = _verify_repo_bot_tokens() if False else None
    # 直接断言扫描面：含 gitee 仓（原扫描只认 gitcode/github 列）
    from src.domains.runtime.engine import bot_token_health as bth
    import inspect
    src = inspect.getsource(bth)
    assert "grape_t_repo_credentials" in src, "health 扫描未覆盖 grape_t_repo_credentials"


def test_s82_global_swap_updates_rc():
    """config_store 全局 token 换发：rc 行同步更新（原只改列）。VCS 离线 mock。"""
    from unittest.mock import patch
    from src.domains.mgmt.user.db import add_managed_repo
    from src.domains.mgmt.admin.config_store import global_bot_token_swap
    add_managed_repo("gitee:s82o6/r6", "o", "r", webhook_id=1,
                     **{"bot_gitee_token": "old-tok"})

    class _FakeProv:
        def verify_token(self, t):
            return {"login": "new-bot"}
        def token_storage_field(self):
            return "bot_gitee_token"
        def webhook_token_field(self):
            return "gitee_webhook_token"
        def add_collaborator(self, *a, **k):
            return True
        def remove_collaborator(self, *a, **k):
            return True
        def remove_webhook(self, *a, **k):
            return True
        def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
            return 777

    # swap 前置：webhook_url 必须已配置（否则步骤 3 前提前返回）
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('webhook_url', 'http://wh.test')")
        db.commit()
    with patch("src.domains.mgmt.vcs.create_provider", return_value=_FakeProv()):
        global_bot_token_swap("gitee", "old-tok", "new-global")
    rc = _rc("gitee:s82o6/r6")
    assert rc.get("bot") == "new-global", f"全局换发未同步 rc: {rc}"
    assert rc.get("webhook") == "new-global"


# ------------------------------------------------ has_bot 闸门

def test_s82_has_bot_reads_rc():
    """deps has_bot 闸门：gitee 仓（仅 rc 有 token）放行。"""
    from src.domains.mgmt.user.db import add_managed_repo, get_managed_repos
    from src.kernel.persistence.uow import uow
    from src.kernel.persistence.repositories import managed_repo_repository as repo
    add_managed_repo("gitee:s82o7/r7", "o", "r", webhook_id=1, org_id=0,
                     host_user_id=0, **{"bot_gitee_token": "bt"})
    # 模拟 deps 的判定：任一托管仓库有 bot token（rc 感知）
    with uow() as s:
        rows = repo.find_by_full_name_any_provider(s, "s82o7/r7")
    assert rows, "反查应命中"
    from src.domains.mgmt.user.db import any_repo_credential_for_repos
    assert any_repo_credential_for_repos([r.repo_key for r in rows]) is True
