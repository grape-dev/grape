"""S87 用例（D63，先测后改）：真二分收口第一批——全局 bot 链统一 + 孪生
token 函数泛化 + OAuth 读回校验全渠道化 + 文件级零比较守卫。

背景（D63 台账）：35 处疑似二分逐条分类后，8 组真二分本批收口；
方言/卡驱动域 5 处合法保留，gitcode 专属子系统 3 处登记 D51/批次。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock


# ---------- 1. 结构守卫：收口文件零平台比较 ----------

def test_s87_no_platform_comparison_token_resolver():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/token_resolver.py").read_text()
    for pat in ('== "github"', '!= "github"', '== "gitcode"', '!= "gitcode"',
                '== "gitee"', '== "gitlab"'):
        assert pat not in src, f"token_resolver 残留平台比较: {pat}"


def test_s87_no_platform_comparison_feedback_converter():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/api/feedback_issue_converter.py").read_text()
    assert '== "github"' not in src and '== "gitcode"' not in src


def test_s87_no_platform_comparison_grape_auth():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/domains/mgmt/routes/grape_auth.py").read_text()
    assert '== "github"' not in src, "grape_auth 残留 github 特判（读回校验应全渠道）"


# ---------- 2. 行为锁：统一 helper ----------

def test_s87_global_bot_usernames_for_provider():
    """github=独立 meta 键单值；其余=gitcode usernames 集合（现状语义保持）。"""
    from src.domains.mgmt.user.db import get_global_bot_usernames_for, _invalidate_global_bot_cache
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                            "('global_bot_github_username','gh-bot-x')")
        db.cursor().execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                            "('global_bot_usernames','[\"gc-bot-a\",\"gc-bot-b\"]')")
        db.commit()
    # 60s TTL 缓存可能持有此前用例的文件 fallback 值（与 test_global_bot_token_db.py 同款清缓存）
    _invalidate_global_bot_cache()
    try:
        assert get_global_bot_usernames_for("github") == ["gh-bot-x"]
        assert get_global_bot_usernames_for("gitcode") == ["gc-bot-a", "gc-bot-b"]
        # config-only 平台与 gitcode 同源集合（存量语义：else 分支）
        assert get_global_bot_usernames_for("gitee") == ["gc-bot-a", "gc-bot-b"]
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key IN "
                                "('global_bot_github_username','global_bot_usernames')")
            db.commit()
        _invalidate_global_bot_cache()


def test_s87_get_user_platform_token_unified(tmp_path=None):
    """孪生函数泛化：get_user_platform_token(user_id, provider) 双平台等值。"""
    from src.domains.mgmt.user import db as udb
    # 造一条 gitcode 身份凭据（get_credentials 走 user_credentials/external_identities 链）
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) "
            "VALUES (9001, 'gitcode', ?, datetime('now'))",
            (json.dumps({"token": "tok-gc-1"}),))
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) "
            "VALUES (9001, 'gitee', ?, datetime('now'))",
            (json.dumps({"token": "tok-gt-1"}),))
        db.commit()
    try:
        assert udb.get_user_platform_token(9001, "gitcode") == "tok-gc-1"
        assert udb.get_user_platform_token(9001, "gitee") == "tok-gt-1"
        # 孪生薄委托等值
        assert udb.get_user_gitcode_token(9001) == "tok-gc-1"
        assert udb.get_user_github_token_by_id(9001) == ""
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_credentials WHERE user_id=9001")
            db.commit()


def test_s87_oauth_readback_all_providers_shape():
    """读回校验代码形状锁：readback 块不再包在 provider 条件内（全渠道）。"""
    import pathlib, re
    src = (pathlib.Path(__file__).resolve().parent.parent / "src/domains/mgmt/routes/grape_auth.py").read_text()
    # 读回调用存在且不在任何 if provider == 判断内（结构：紧跟 account 创建之后）
    assert "get_grape_account_by_id(account[\"id\"])" in src
    assert 'if provider == "github":' not in src
