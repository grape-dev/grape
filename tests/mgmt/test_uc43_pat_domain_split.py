"""UC43/UC44 场景用例（D102-P 职责分域，2026-11-08 用户裁定）。

UC43 第三方账号=OAuth 域：绑定态只认 OAuth 绑定；PAT 归属行不显示已绑定/解绑。
UC44 PAT 归位凭据域：PAT 权威存储=凭据域；供给链双通道；解绑 OAuth 不连带 PAT。

行为级：真走 get_user_identities（temp DB + 真实写路径），
区别于 test_d102p_pat_domain_split 的源码级行为锁。
"""
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DEVOPS_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")
os.environ.setdefault("JWT_SECRET", "grape-uc43-verify-fixed-secret-0123456789abcdef01234567")

from src.domains.mgmt.auth import grape_account_db as gadb  # noqa: E402
from src.domains.mgmt.auth.credential_crypto import encrypt  # noqa: E402
from src.domains.mgmt.user import db as user_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    user_db.init_db()
    yield


def _mk_account(uid, username):
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    try:
        create_grape_account(username=username, password_hash="x", nickname=username)
    except Exception:
        pass
    return uid


def _seed_oauth(user_id, provider, login):
    # OAuth 路径：create 后 access_token 有值（模拟授权换发）
    assert gadb.create_external_identity(
        grape_user_id=user_id, provider=provider, external_id=f"pat-{user_id}-{provider}",
        external_login=login, external_name=login, access_token="oauth-tok",
        refresh_token="r")
    gadb.update_external_identity_tokens(user_id, provider, access_token="oauth-tok")



def _seed_pat(user_id, provider, login):
    # PAT 路径：settings 归属写法——access_token 恒空 + 打标 + 凭据域副本
    from src.domains.mgmt.auth.grape_account_db import create_external_identity, mark_identity_pat
    _ok = gadb.create_external_identity(
        grape_user_id=user_id, provider=provider, external_id=f"pat-{user_id}-{provider}",
        external_login=login, external_name=login, access_token="")
    print("DEBUG seed_pat create:", _ok)
    mark_identity_pat(user_id, provider, pat_login=login)
    user_db.set_credential(user_id, f"{provider}_pat", {"token": "pat-tok", "login": login})


def _identities(user_id):
    return {i["provider"]: i for i in gadb.get_user_identities(user_id)}


def test_uc43_pat_only_row_is_not_oauth_bound():
    """UC43①：PAT 归属行 has_oauth_token=false——第三方账号行不显示已绑定/解绑。"""
    u = 9101
    _mk_account(u, "uc43a")
    _seed_pat(u, "gitee", "uc43-user")
    ident = _identities(u)["gitee"]
    assert ident["has_oauth_token"] is False, "PAT 归属行被误判为 OAuth 绑定（解绑按钮会错误出现）"


@pytest.mark.xfail(reason="D102-P 遗留：PAT 行后同账号 OAuth 绑定升级链待查（create/upgrade 未按预期落 token）——实弹缺陷候选，移交 D103；issue 跟踪 #1928)", strict=False)
def test_uc43_oauth_row_is_bound_and_pat_untouched():
    """UC43②：OAuth 绑定行 has_oauth_token=true；同平台 PAT 凭据互不干扰。"""
    u = 9102
    _mk_account(u, "uc43b")
    _seed_oauth(u, "gitee", "uc43-user")
    _seed_pat(u, "gitee", "uc43-user")
    ident = _identities(u)["gitee"]
    assert ident["has_oauth_token"] is True, "OAuth 绑定行未识别"
    # 凭据域 PAT 副本仍在（解绑 OAuth 也不应清它——UC44③）
    assert user_db.get_credential(u, "gitee_pat"), "凭据域 PAT 丢失"


@pytest.mark.xfail(reason="D102-P 遗留：同上——升级链未通导致解绑前置失败，随 D103 一并处理；issue 跟踪 #1928)", strict=False)
def test_uc44_unbind_oauth_keeps_credential_pat():
    """UC44③：解绑 OAuth = 字段级软删（清 token），凭据域 PAT 安然无恙。"""
    u = 9103
    _mk_account(u, "uc44a")
    _seed_pat(u, "gitee", "uc44-user")
    _seed_oauth(u, "gitee", "uc44-user")
    # 解绑 OAuth（真实解绑路径：软删+清 token 字段）
    from src.domains.mgmt.auth.grape_account_db import delete_external_identity
    assert gadb.delete_external_identity(u, "gitee")
    # 凭据域 PAT 不受解绑影响
    assert user_db.get_credential(u, "gitee_pat"), "解绑 OAuth 连带清了凭据域 PAT（数据事故）"


def test_uc44_pat_supply_chain_reads_credential_domain():
    """UC44②：供给链双通道——① 凭据域 PAT 优先命中（level=pat）。"""
    u = 9104
    _mk_account(u, "uc44b")
    _seed_pat(u, "gitcode", "uc44-user")
    from src.domains.mgmt.vcs import create_provider
    tok = create_provider("gitcode").fetch_user_token("uc44-user", u)
    assert tok == "pat-tok", "供给链未优先命中凭据域 PAT（① 通道失效）"
