"""扫雷⑦ 批B1（中危四项）用例。

1. credential_registry 平台实锚卡驱动（补 gitlab——configs/platforms/gitlab.json 在册）；
2. GLOBAL_BOT_USERNAME_SOURCES 补 gitee 单值源（get_global_gitee_bot_username，
   与 token 链 global_bot_gitee_token 同构；未配置回落 gitcode pool 存量语义）；
3. repos.py _supported_providers 兜底 except 名单与主路径对称（含 gitlab）；
4. webhook 跨平台同名仓串仓隔离（provider 维度收窄查找链）。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _env():
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                            "('global_bot_usernames','[\"gc-bot-a\",\"gc-bot-b\"]')")
        db.cursor().execute("DELETE FROM meta WHERE key='global_bot_gitee_username'")
        for t in ("managed_repos", "grape_t_repo_credentials"):
            db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 'b1%' OR repo_key LIKE '%:b1o/b1r'")
        db.commit()
    from src.domains.mgmt.user.db import _invalidate_global_bot_cache
    _invalidate_global_bot_cache()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key IN "
                            "('global_bot_usernames','global_bot_gitee_username')")
        for t in ("managed_repos", "grape_t_repo_credentials"):
            db.cursor().execute(f"DELETE FROM {t} WHERE repo_key LIKE 'b1%' OR repo_key LIKE '%:b1o/b1r'")
        db.commit()
    _invalidate_global_bot_cache()


# ──────────── ① credential_registry：实锚补 gitlab ────────────

def test_b1_registry_gitlab_pat_registered():
    """gitlab 平台实锚登记：gitlab_pat / gitlab_oauth 均在注册表（原三平台硬编码漏）。"""
    from src.domains.mgmt.user.credential_registry import lookup, validate_fields
    spec = lookup("gitlab_pat")
    assert spec is not None and spec.scope == "user"
    ok, missing = validate_fields("gitlab_pat", {"token": "tok"})
    assert ok and not missing
    assert not validate_fields("gitlab_pat", {})[0]
    # OAuth 快照副本段同源（deprecated 语义保持）
    ospec = lookup("gitlab_oauth")
    assert ospec is not None and ospec.deprecated is True


def test_b1_registry_known_platforms_card_driven():
    """_known_platforms 卡驱动：卡在册四平台全部登记，兜底含 gitlab。"""
    from src.domains.mgmt.user.credential_registry import _known_platforms, _PAT_SPECS
    known = _known_platforms()
    for p in ("gitcode", "github", "gitee", "gitlab"):
        assert p in known, p
    pat_keys = {s.key for s in _PAT_SPECS}
    assert {"gitcode_pat", "github_pat", "gitee_pat", "gitlab_pat"} <= pat_keys


def test_b1_registry_dynamic_pat_fallback_covers_gitlab():
    """_is_dynamic_pat 异常兜底分支同样含 gitlab（名单不对称回潮防线）。"""
    from src.domains.mgmt.user.credential_registry import _is_dynamic_pat
    import src.domains.mgmt.user.credential_registry as cr
    orig = cr.get_platform_card if hasattr(cr, "get_platform_card") else None
    try:
        # 强制卡体系异常路径：卡导入失败 → 兜底名单判定
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **k):
            if "platform_card" in name:
                raise RuntimeError("card system down")
            return real_import(name, *a, **k)
        builtins.__import__ = _boom
        assert _is_dynamic_pat("gitlab_pat") is True
        assert _is_dynamic_pat("nosuch_pat") is False
    finally:
        builtins.__import__ = real_import


# ──────────── ② gitee bot 身份链 ────────────

def test_b1_gitee_bot_username_source_single_value():
    """GLOBAL_BOT_USERNAME_SOURCES 补 gitee：单值源（meta global_bot_gitee_username）。"""
    from src.domains.mgmt.user.db import GLOBAL_BOT_USERNAME_SOURCES, _invalidate_global_bot_cache
    with db_lock:
        db.cursor().execute("INSERT OR REPLACE INTO meta (key,value) VALUES "
                            "('global_bot_gitee_username','gt-bot-x')")
        db.commit()
    _invalidate_global_bot_cache()
    try:
        assert "gitee" in GLOBAL_BOT_USERNAME_SOURCES
        assert GLOBAL_BOT_USERNAME_SOURCES["gitee"]() == ["gt-bot-x"]
        from src.domains.mgmt.user.repo_manager import resolve_bot_username
        assert resolve_bot_username("gitee") == "gt-bot-x"
        assert get_usernames_for_gitee() == ["gt-bot-x"]
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='global_bot_gitee_username'")
            db.commit()
        _invalidate_global_bot_cache()


def get_usernames_for_gitee():
    from src.domains.mgmt.user.db import get_global_bot_usernames_for
    return get_global_bot_usernames_for("gitee")


def test_b1_gitee_bot_username_unset_falls_back_to_gitcode_pool():
    """未配置 gitee 专属单值：回落 gitcode pool（存量 config-only 同源语义锁保持）。"""
    assert get_usernames_for_gitee() == ["gc-bot-a", "gc-bot-b"]
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    # 存量语义锁（test_s88）：专属单值未配置时 gitee 仅走卡（卡无 bot_identity → 空）
    assert resolve_bot_username("gitee") == ""


# ──────────── ③ repos.py 兜底名单对称 ────────────

def test_b1_supported_providers_fallback_symmetric():
    """卡体系异常时兜底名单与主路径对称（含 gitlab，勿再漏）。"""
    from src.domains.mgmt.routes import repos as repos_mod
    from src.domains.runtime.vcs import platform_card as pc
    orig = pc.list_vcs_platforms

    def _boom():
        raise RuntimeError("card system down")
    try:
        # patch 模块内 from-import 绑定的符号（_supported_providers 局部 from-import）
        import src.domains.runtime.vcs.platform_card
        prev = src.domains.runtime.vcs.platform_card.list_vcs_platforms
        src.domains.runtime.vcs.platform_card.list_vcs_platforms = _boom
        # _supported_providers 内部 `from src.domains.runtime.vcs.platform_card import list_vcs_platforms`
        # 取的是模块属性，赋值即生效
        fb = repos_mod._supported_providers()
        main = set(prev()) | {"gitcode", "github"}
        assert {"gitcode", "github", "gitlab", "gitee"} <= fb
        # 对称性：兜底 ⊇ 主路径卡平台
        assert main <= fb
    finally:
        import src.domains.runtime.vcs.platform_card
        src.domains.runtime.vcs.platform_card.list_vcs_platforms = prev


# ──────────── ④ webhook 跨平台同名串仓隔离 ────────────

def _seed_webhook_row(repo_key: str, secret: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, "
            "full_name, webhook_id, webhook_secret) VALUES (?, 'b1o', 'b1r', 'b1o/b1r', 1, ?)",
            (repo_key, secret))
        db.commit()


def test_b1_webhook_token_provider_isolated():
    """跨平台同名仓（github:o/r 与 gitlab:o/r）不互串预期 webhook token。

    串仓风险判定为真实：get_webhook_secret_by_repo_key_any 原实现按
    '%:full_name' 任意前缀遍历，gitlab webhook 端点会拿到 github 同名仓的
    secret（误拒合法事件/或两仓同 secret 时误收）。provider 维度收窄后：
    每个端点只命中本平台行。
    """
    from src.domains.mgmt.user.db import get_webhook_token_by_repo
    _seed_webhook_row("github:b1o/b1r", "sec-gh")
    _seed_webhook_row("gitlab:b1o/b1r", "sec-gl")
    assert get_webhook_token_by_repo("b1o/b1r", provider="gitlab") == "sec-gl"
    assert get_webhook_token_by_repo("b1o/b1r", provider="github") == "sec-gh"
    # 未托管平台同名仓：不再借其他平台的 secret（隔离语义）
    assert get_webhook_token_by_repo("b1o/b1r", provider="gitee") == ""


def test_b1_webhook_secret_lookup_provider_scoped():
    from src.domains.mgmt.user.db import get_webhook_secret_by_repo_key_any
    _seed_webhook_row("github:b1o/b1r", "sec-gh")
    _seed_webhook_row("gitlab:b1o/b1r", "sec-gl")
    assert get_webhook_secret_by_repo_key_any("b1o/b1r", provider="gitlab") == "sec-gl"
    assert get_webhook_secret_by_repo_key_any("b1o/b1r", provider="github") == "sec-gh"
    # provider 为空：存量任意 provider 遍历语义保持
    assert get_webhook_secret_by_repo_key_any("b1o/b1r") in ("sec-gh", "sec-gl")


def test_b1_webhook_token_provider_scoped_rc_fallback():
    """provider 隔离链 ②：rc 凭据表按本平台 repo_key 回落（不串平台）。"""
    from src.domains.mgmt.user.db import add_managed_repo, get_webhook_token_by_repo
    add_managed_repo("gitee:b1o/b1r", "b1o", "b1r", webhook_id=1,
                     **{"gitee_webhook_token": "wt-rc-gitee"})
    assert get_webhook_token_by_repo("b1o/b1r", provider="gitee") == "wt-rc-gitee"
    assert get_webhook_token_by_repo("b1o/b1r", provider="github") == ""
