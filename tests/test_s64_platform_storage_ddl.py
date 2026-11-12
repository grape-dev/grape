"""D44（S64）用例：gitee 托管硬阻塞修复——平台卡存储列 DDL + 动态列写入。

问题定位（问题清单 #3）：managed_repos 无 config-only 平台物理列
（bot_gitee_token/gitee_webhook_token），gitee 仓走仓库级 bot token
（路径 A）时 add_managed_repo(**{列: 值}) 直接 TypeError；此前只在
测试里 ALTER 模拟（批次 1 前的临时形态）。

修复形态（登录白名单列自动 ALTER 同款先例）：
1. db_setup._ensure_platform_storage_columns()：启动时按仓库平台卡
   幂等加列（identifier 白名单校验；列已存在跳过；无卡不加）；
2. add_managed_repo(**extra_cols)：基线 SQL 不变（无 extras 时逐字节
   等价），传 extras 时追加列（identifier 校验，非法列拒绝）。
"""
import json
import uuid

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


def _cols(table="managed_repos"):
    with db_lock:
        cols = [r[1] for r in db.cursor().execute(f"PRAGMA table_info({table})").fetchall()]
        db.commit()
    return set(cols)


def _drop_col(col):
    with db_lock:
        try:
            db.cursor().execute(f"ALTER TABLE managed_repos DROP COLUMN {col}")
        except Exception:
            pass
        db.commit()


def _row(repo_key):
    with db_lock:
        r = db.cursor().execute("SELECT * FROM managed_repos WHERE repo_key=?", (repo_key,)).fetchone()
        cols = [c[1] for c in db.cursor().execute("PRAGMA table_info(managed_repos)").fetchall()]
        db.commit()
        return dict(zip(cols, r)) if r else None


# ------------------------------------------------ S64 幂等列迁移

def test_s64_ensure_columns_adds_gitee(gitee_card):
    from src.api.db_setup import _ensure_platform_storage_columns
    _drop_col("bot_gitee_token")
    _drop_col("gitee_webhook_token")
    assert "bot_gitee_token" not in _cols()
    _ensure_platform_storage_columns()
    assert "bot_gitee_token" in _cols()
    assert "gitee_webhook_token" in _cols()
    # 幂等：二次执行不报错不重复
    _ensure_platform_storage_columns()
    cols = _cols()
    assert "bot_gitee_token" in cols and "gitee_webhook_token" in cols


def test_s64_ensure_columns_no_card_noop():
    from src.api.db_setup import _ensure_platform_storage_columns
    before = _cols()
    _ensure_platform_storage_columns()
    # 无 config-only 卡时不应新增任何列（gitcode/github 列已存在）
    assert "bot_foo_token" not in _cols()
    assert before == _cols()


# ------------------------------------------------ S64 动态列写入

def test_s64_add_managed_repo_extra_columns(gitee_card):
    """S82/F4 现语义：extra 凭据列 kwargs 路由 grape_t_repo_credentials（列冻结为空）。"""
    from src.domains.mgmt.user import db as udb
    from src.domains.mgmt.user.db import get_repo_credential_value
    from src.api.db_setup import _ensure_platform_storage_columns
    _ensure_platform_storage_columns()
    rk = "gitee:acme/s64web%s" % uuid.uuid4().hex[:4]
    try:
        ok = udb.add_managed_repo(rk, "acme", rk.split("/")[-1],
                                  **{"bot_gitee_token": "bt", "gitee_webhook_token": "wt"})
        assert ok is True
        row = _row(rk)
        assert row["bot_gitee_token"] == ""  # S82 列冻结 + F4 轮换置空
        assert row["gitee_webhook_token"] == ""
        assert row["bot_gitcode_token"] == ""  # 不折叠
        assert get_repo_credential_value(rk, "bot") == "bt"
        assert get_repo_credential_value(rk, "webhook") == "wt"
        # upsert 更新路径：换 token（rc 权威，F4 upsert 修复后二次轮换不撞 UNIQUE）
        ok = udb.add_managed_repo(rk, "acme", rk.split("/")[-1],
                                  **{"bot_gitee_token": "bt2"})
        assert ok is True
        assert get_repo_credential_value(rk, "bot") == "bt2"
        assert get_repo_credential_value(rk, "webhook") == "wt"  # 未传保留
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
            db.commit()


def test_s64_add_managed_repo_rejects_illegal_column():
    """S74 现语义：非法列名响亮拒绝（raise，调用方 except 路由兜底自愈）。"""
    from src.domains.mgmt.user import db as udb
    rk = "gitee:acme/bad%s" % uuid.uuid4().hex[:4]
    try:
        with pytest.raises(ValueError):
            udb.add_managed_repo(rk, "acme", "bad", **{"bot_gitee_token; DROP TABLE x": "v"})
        assert _row(rk) is None
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.commit()


def test_s64_add_managed_repo_no_extras_parity():
    """无 extras 时行为不变（存量参数路径；S82/F4 后凭据进 rc、列冻结）。"""
    from src.domains.mgmt.user import db as udb
    from src.domains.mgmt.user.db import get_repo_credential_value
    rk = "gitcode:acme/s64p%s" % uuid.uuid4().hex[:4]
    try:
        assert udb.add_managed_repo(rk, "acme", rk.split("/")[-1],
                                    webhook_id=5, bot_gitcode_token="bt",
                                    gitcode_webhook_token="wt") is True
        row = _row(rk)
        assert row["webhook_id"] == 5
        assert row["bot_gitcode_token"] == ""  # S82 列冻结
        assert get_repo_credential_value(rk, "bot") == "bt"
        assert get_repo_credential_value(rk, "webhook") == "wt"
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
            db.commit()


def test_s64_columns_created_on_every_boot(gitee_card):
    """S70（实弹发现）：init_database 每次启动必须补 config 派生列——
    原 _ensure_platform_storage_columns 只在 legacy 全量迁移段（已盖章库
    跳过），gitee 卡在盖章之后注册 → 列永不创建 → add_managed_repo 静默
    return False → webhook_secret 不落库 → webhook 验签必 404。幂等函数
    应每次启动执行（与盖章一次性迁移解耦）。"""
    import sqlite3
    _drop_col("bot_gitee_token")
    _drop_col("gitee_webhook_token")
    assert "bot_gitee_token" not in _cols()
    from src.api.db_setup import init_database
    init_database()  # 已盖章库启动路径（非直接调 _ensure_platform_storage_columns）
    assert "bot_gitee_token" in _cols(), (
        "gitee 卡注册后 init_database 启动未补派生列（盖章库跳过迁移段）")
    assert "gitee_webhook_token" in _cols()
