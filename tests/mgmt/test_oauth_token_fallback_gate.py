"""OAuth 与 PAT 两个模式并存：取 token 恒定 ①PAT ②OAuth（2026-11-14 用户口径）。

用户原话：
「gitee/github 本来就要支持 oauth 和 pat 的两个模式」「这个（关系配置）不是判断依据，
它只是用来判断登录成功之后要不要写用户和平台仓库关系的依据」

行为锁：
1. 有 PAT → 永远返回 PAT，与是否有 OAuth 绑定无关；
2. 无 PAT → 回落登录令牌（不再有任何开关/关系门禁）；
3. channel-repo-links.json 的配对不影响取 token（它只服务 hooks 写用户平台关系）；
4. 平台卡 legacy_token_symbol 遗留符号同一口径（PAT 优先，其次 OAuth 早退）。
"""
import pytest

from src.api import deps


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GRAPE_OAUTH_TOKEN_FALLBACK", raising=False)


def _no_pat(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.user.db.get_credential",
                        lambda *a, **k: {}, raising=False)


def _oauth_token(monkeypatch, value="oauth-login-token"):
    monkeypatch.setattr(deps, "_get_oauth_token_from_identity",
                        lambda u, p: value, raising=False)
    monkeypatch.setattr("src.domains.mgmt.auth.grape_account_db._has_refresh_token",
                        lambda uid, p: False, raising=False)


def test_oauth_fallback_is_on_by_default(monkeypatch):
    """无 PAT → 回落登录令牌（两种模式并存，gitee/github 同样适用）。"""
    _no_pat(monkeypatch)
    _oauth_token(monkeypatch)
    assert deps.get_user_token_for("u1", "42", "gitee") == "oauth-login-token"
    assert deps.get_user_token_for("u1", "42", "github") == "oauth-login-token"


def test_relation_config_does_not_gate_token(monkeypatch):
    """关系配置不是取 token 的判据：即使没有任何关系行也照样回落 OAuth。"""
    _no_pat(monkeypatch)
    _oauth_token(monkeypatch)
    import src.domains.platform.hooks as hooks
    assert not hasattr(hooks, "user_oauth_drives"), "关系配置不应参与取 token 判定"
    assert deps.get_user_token_for("u1", "42", "gitee") == "oauth-login-token"


def test_pat_always_wins(monkeypatch):
    """有 PAT 时永远优先 PAT（与 OAuth 模式无关，不受任何开关影响）。"""
    monkeypatch.setattr("src.domains.mgmt.user.db.get_credential",
                        lambda uid, provider, kind=None: {"token": "pat-token"}, raising=False)
    _oauth_token(monkeypatch)
    monkeypatch.setenv("GRAPE_OAUTH_TOKEN_FALLBACK", "0")
    assert deps.get_user_token_for("u1", "42", "gitee") == "pat-token"


def test_legacy_token_symbol_keeps_baseline_semantics(monkeypatch):
    """遗留符号（平台卡 legacy_token_symbol）：保留基线 R2 早退语义——

    已有 OAuth 令牌且无 refresh_token 时直接返回它（仅看请求与令牌是否切割过），
    否则委托 get_user_token_for（①PAT ②OAuth）。
    """
    monkeypatch.setattr("src.domains.mgmt.user.db.get_credential",
                        lambda uid, provider, kind=None: {"token": "pat-token"}, raising=False)
    _oauth_token(monkeypatch)
    resolver = deps._legacy_token_resolver("get_user_token_with_refresh", "gitee")
    assert resolver("u1", 42) == "oauth-login-token"   # 基线早退：OAuth 在场直接给
    monkeypatch.setattr(deps, "_get_oauth_token_from_identity", lambda u, p: "", raising=False)
    assert resolver("u1", 42) == "pat-token"           # 无 OAuth → 委托 → PAT 优先


def test_gitee_scope_is_login_only():
    """Gitee 登录应用只申请身份 scope（仓库权限由该应用 scope 或 PAT 承担，取 token 不预判）。"""
    from src.domains.mgmt.auth.providers import builtin_channels
    scopes = set(str(builtin_channels()["gitee"]["scope"]).split())
    assert scopes == {"user_info"}, scopes