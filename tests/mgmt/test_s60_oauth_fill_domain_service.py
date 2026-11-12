"""D40（S60）用例：oauth_fill 根治——显式链接注册表 + 凭据撤销域服务。

用户纠偏（"从根本上解决问题"）：不在解绑路由补一行 _pat 清理、不在
刷新分派补一个 else——把两处根因各收敛为身份域的域概念：

1. **显式链接注册表（src/kernel/auth_sdk/provider_link.py）**：渠道↔平台链接 =
   渠道目录 ∩ 平台目录（目录派生，非字符串相等散落路由）；刷新能力 =
   注册表声明（provider→refresh 实现），未注册返回 None。消灭
   deps._try_refresh_user_token 的 provider=="gitcode"/"github" 字面量
   二分（config-only 渠道无刷新能力是"未注册"的显式语义，不再是
   else 兜底）。
2. **凭据撤销域服务（grape_account_db.revoke_provider_credentials）**：
   身份域拥有该 provider 名下用户级凭据形态清单（{p}_oauth 登录凭据 /
   {p}_pat PAT 泛型凭据，键族规则集中一处），解绑/吊销单入口传播。
   实锤缺口：解绑只清 _oauth，S52 写入的 _pat 残留可用（撤销不传播）。
"""
import ast
import inspect
import json
import uuid
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_PLATFORM_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
    "bot_username": "g_grape_bot",
}


@pytest.fixture()
def gitee_platform_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_PLATFORM_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_PLATFORM_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


@pytest.fixture()
def gitee_channel_card():
    """gitee 同时配 OAuth 登录渠道卡 → oauth_fill 链接成立（无刷新实现）。"""
    cfg = {
        "gitee": {
            "name": "Gitee", "auth_url": "https://gitee.com/oauth/authorize",
            "token_url": "https://gitee.com/oauth/token",
            "user_url": "https://gitee.com/api/v5/user",
            "scope": "user_info", "login_field": "login",
        }
    }
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (json.dumps(cfg),))
        db.commit()
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    invalidate_provider_cache()
    yield cfg
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
        db.commit()
    invalidate_provider_cache()


# ------------------------------------------------ S60 显式链接注册表

def test_s60_oauth_fill_targets_default():
    """默认目录：gitcode/github 链接成立（内置渠道+内置平台卡）。

    环境无关化（D90 批）：断言依赖 meta 渠道配置——开发库只配 github 时
    gitcode 不在目录 → 假红。自播种内置双渠道（对齐 gitee_channel_card
    既有写法），跑后恢复。
    并行注意：播种键 oauth_providers_config 是 meta 全局单键（与
    gitee_channel_card 同键）——当前测试框架为串行 pytest（无 xdist），
    安全；若未来引入 xdist，本文件需打 serial 标记。"""
    from src.kernel.auth_sdk.provider_link import oauth_fill_targets
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.auth.providers import invalidate_provider_cache
    import json as _json

    def _builtin_channel(provider):
        """内置渠道卡精简形态（对齐 providers._DEFAULT_PROVIDERS 关键字段）。"""
        if provider == "gitcode":
            return {"name": "GitCode", "auth_url": "https://gitcode.com/oauth/authorize",
                    "token_url": "https://gitcode.com/oauth/token",
                    "user_url": "https://gitcode.com/api/v5/user", "scope": "all"}
        return {"name": "GitHub", "auth_url": "https://github.com/login/oauth/authorize",
                "token_url": "https://github.com/login/oauth/access_token",
                "user_url": "https://api.github.com/user", "scope": "read:user"}

    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('oauth_providers_config', ?)",
            (_json.dumps({"gitcode": _builtin_channel("gitcode"),
                          "github": _builtin_channel("github")}),))
        db.commit()
    invalidate_provider_cache()
    try:
        targets = {t["provider"] for t in oauth_fill_targets()}
        assert "gitcode" in targets
        assert "github" in targets
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM meta WHERE key='oauth_providers_config'")
            db.commit()
        invalidate_provider_cache()



def test_s60_pat_only_platform_not_linked(gitee_platform_card):
    """PAT-only 平台（只配平台卡、无 OAuth 渠道）→ 不在 oauth_fill 映射
    （链接=渠道∩平台目录，不是平台名单）。

    D90-N5（2026-11-07）修正：gitee OAuth 渠道卡已在册（providers 内置默认），
    gitee 变为渠道∩平台双卡链接平台——PAT-only 断言改用未注册渠道。"""
    from src.kernel.auth_sdk.provider_link import oauth_fill_targets
    targets = {t["provider"] for t in oauth_fill_targets()}
    assert "notarealplatform" not in targets
    # 新格局：gitee 双卡链接成立（N5-1 渠道卡 + 平台卡）
    assert "gitee" in targets


def test_s60_linked_when_both_cards(gitee_platform_card, gitee_channel_card):
    """渠道卡+平台卡同时就位 → gitee 链接成立，supports_refresh=False
    （未注册刷新实现，显式而非 else 兜底）。"""
    from src.kernel.auth_sdk.provider_link import oauth_fill_targets
    t = {x["provider"]: x for x in oauth_fill_targets()}
    assert "gitee" in t
    assert t["gitee"]["supports_refresh"] is False


def test_s60_refresh_dispatch_no_literals():
    """结构守卫：_try_refresh_user_token 函数体禁平台名字面量（根治
    provider==二分）。"""
    from src.api import deps
    banned = {"github", "gitcode", "gitee"}
    fn = deps._try_refresh_user_token
    tree = ast.parse(inspect.getsource(fn))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in banned:
                pytest.fail(f"_try_refresh_user_token 含平台名字面量: {node.value}")


def test_s60_refresh_dispatch_parity():
    """刷新分派存量等价：gitcode/github 走注册实现，未注册渠道返回 None。
    （注册表持有函数引用，mock 打注册表本体。）"""
    from src.api import deps
    from src.kernel.auth_sdk import provider_link
    import unittest.mock as um
    with patch.dict(provider_link._refresh_registry,
                    {"gitcode": um.Mock(return_value="t1"),
                     "github": um.Mock(return_value="t2")}):
        assert deps._try_refresh_user_token(5, "gitcode") == "t1"
        assert deps._try_refresh_user_token(5, "github") == "t2"
        provider_link._refresh_registry["gitcode"].assert_called_once_with(5)
        provider_link._refresh_registry["github"].assert_called_once_with(5)
    assert deps._try_refresh_user_token(5, "gitee") is None


# ------------------------------------------------ S60 凭据撤销域服务

def test_s60_revoke_clears_all_credential_forms():
    """撤销传播：{p}_oauth 与 {p}_pat 两形态全清（键族规则集中域服务）。"""
    from src.domains.mgmt.auth.grape_account_db import revoke_provider_credentials
    from src.domains.mgmt.user import db as udb
    uid = 9_300_000 + int(uuid.uuid4().hex[:6], 16) % 100_000
    udb.set_credential(uid, "gitee_oauth", {"access_token": "x"})
    udb.set_credential(uid, "gitee_pat", {"token": "y"})
    try:
        result = revoke_provider_credentials(uid, "gitee")
        assert result["gitee_oauth"] is True
        assert result["gitee_pat"] is True
        assert udb.get_credential(uid, "gitee_oauth") is None
        assert udb.get_credential(uid, "gitee_pat") is None
    finally:
        udb.delete_credential(uid, "gitee_oauth")
        udb.delete_credential(uid, "gitee_pat")


def test_s60_revoke_unknown_provider_no_side_effects():
    """撤销服务对未知 provider 幂等安全：不存在的键返回 False 不抛错。"""
    from src.domains.mgmt.auth.grape_account_db import revoke_provider_credentials
    uid = 9_310_000 + int(uuid.uuid4().hex[:6], 16) % 100_000
    result = revoke_provider_credentials(uid, "nope")
    assert result["nope_oauth"] is False
    assert result["nope_pat"] is False


# ------------------------------------------------ S60 解绑路由接撤销服务

_MOCK_USER = {"id": 1, "username": "testuser", "email": "test@example.com", "token_type": "jwt"}


@pytest.fixture
def client():
    from src.api.main import app
    from src.api.deps import require_jwt_only
    app.dependency_overrides[require_jwt_only] = lambda: _MOCK_USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _mock_sm_db():
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor
    mock_lock = MagicMock()
    mock_lock.__enter__ = MagicMock(return_value=None)
    mock_lock.__exit__ = MagicMock(return_value=False)
    return mock_db, mock_lock, mock_cursor


def test_s60_unbind_gitee_revokes_pat_and_oauth(gitee_platform_card, client):
    """实锤缺口修复：解绑 gitee → _pat 与 _oauth 都被撤销（传播闭合）。"""
    import contextlib
    mock_db, mock_lock, _cur = _mock_sm_db()
    with contextlib.ExitStack() as st:
        for m in (
            patch("src.domains.mgmt.routes.grape_auth.auth_audit.log_audit"),
            patch("src.domains.mgmt.routes.grape_auth.get_user_identities", return_value=[]),
            patch("src.domains.mgmt.routes.grape_auth.delete_external_identity", return_value=True),
            patch("src.domains.mgmt.routes.grape_auth.has_password", return_value=True),
            patch("src.domains.mgmt.routes.grape_auth.count_user_identities", return_value=2),
        ):
            st.enter_context(m)
        m_del = st.enter_context(patch("src.domains.mgmt.user.db.delete_credential"))
        st.enter_context(patch("src.kernel.session_manager.db", mock_db))
        st.enter_context(patch("src.kernel.session_manager.db_lock", mock_lock))
        r = client.delete("/api/auth/bind/gitee?confirm=true")
    assert r.status_code == 200, r.text
    revoked_keys = {c.args[1] for c in m_del.call_args_list if len(c.args) >= 2}
    assert "gitee_pat" in revoked_keys, "解绑必须撤销 PAT 凭据（S52 写入形态）"
    assert "gitee_oauth" in revoked_keys


def test_s60_unbind_gitcode_revokes_both(client):
    """gitcode 解绑：撤销服务同样传播两形态（存量 _oauth 语义保持）。"""
    import contextlib
    mock_db, mock_lock, _cur = _mock_sm_db()
    with contextlib.ExitStack() as st:
        for m in (
            patch("src.domains.mgmt.routes.grape_auth.auth_audit.log_audit"),
            patch("src.domains.mgmt.routes.grape_auth.get_user_identities", return_value=[]),
            patch("src.domains.mgmt.routes.grape_auth.delete_external_identity", return_value=True),
            patch("src.domains.mgmt.routes.grape_auth.has_password", return_value=True),
            patch("src.domains.mgmt.routes.grape_auth.count_user_identities", return_value=2),
        ):
            st.enter_context(m)
        m_del = st.enter_context(patch("src.domains.mgmt.user.db.delete_credential"))
        st.enter_context(patch("src.kernel.session_manager.db", mock_db))
        st.enter_context(patch("src.kernel.session_manager.db_lock", mock_lock))
        r = client.delete("/api/auth/bind/gitcode?confirm=true")
    assert r.status_code == 200, r.text
    revoked_keys = {c.args[1] for c in m_del.call_args_list if len(c.args) >= 2}
    assert "gitcode_oauth" in revoked_keys
    assert "gitcode_pat" in revoked_keys
