"""D102-E 回归：OIDC 回调兜底门槛必须识别卡内 `type` 键（华为登录修复）。

背景（2026-11-08 实弹）：华为 /userinfo 404，回调路由兜底门槛读
`channel_type` 键，而 oauth_providers_config 卡存的是 `type: oidc`
——门槛永假，兜底死代码，华为登录 user_info_failed 全挂。
旧 test_d102e 只做源码字符串断言（假绿），本文件全部行为断言。
"""
from src.domains.mgmt.auth.providers import is_oidc_channel


def test_huawei_real_card_shape_is_detected_as_oidc():
    """用库中华为卡的真实形状（type=oidc，无 channel_type）判定。"""
    card = {
        "name": "华为账号", "type": "oidc",
        "auth_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/authorize",
        "user_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/userinfo",
        "scope": "openid email profile", "pkce_enabled": True,
    }
    assert is_oidc_channel(card) is True


def test_channel_type_key_still_honored():
    """D102-E 方案稿键 channel_type 兼容。"""
    assert is_oidc_channel({"channel_type": "oidc"}) is True


def test_non_oidc_cards_rejected():
    assert is_oidc_channel({"type": "oauth2"}) is False
    assert is_oidc_channel({}) is False
    assert is_oidc_channel(None) is False
    # oauth2 卡不能被误判（gitee/gitcode/github 走原 user_info_failed 语义）
    assert is_oidc_channel({"name": "Gitee", "type": "oauth2"}) is False


def test_route_gate_uses_helper_not_bare_channel_type_key():
    """门槛必须经 is_oidc_channel 判定，不得退回裸读 channel_type（回归锁）。"""
    from src.domains.mgmt.routes import grape_auth
    import inspect
    src = inspect.getsource(grape_auth)
    assert "is_oidc_channel" in src, "回调兜底门槛未走 is_oidc_channel"
    assert '.get("channel_type") or ""' not in src, "裸读 channel_type 门槛回归"
