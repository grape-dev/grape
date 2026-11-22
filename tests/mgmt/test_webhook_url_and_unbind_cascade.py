"""S20（W3）：webhook 回调 URL 按平台卡派生；S15（E1）：OAuth 解绑级联语义。

S15 批次 0 语义：解绑/吊销 external_identities 即清空 token（既有行为），
D3 已删 K4 跨用户兜底 → 取凭据自然失效；手配 PAT（user_credentials）不受影响。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
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


def test_s20_hosting_config_suffix_from_card(gitee_card):
    from src.domains.mgmt.user.repo_manager import get_hosting_provider_config, _webhook_path_from_card
    # 存量平台：卡缺省值 = 历史硬编码
    assert get_hosting_provider_config("github")["webhook_url_suffix"] == "/webhook/github"
    assert get_hosting_provider_config("gitcode")["webhook_url_suffix"] == "/webhook/gitcode"
    # 新平台：从卡派生
    assert _webhook_path_from_card("gitee", "/webhook/gitcode") == "/webhook/gitee"


def test_s20_rebuild_webhook_url_for_provider(gitee_card):
    from src.domains.mgmt.user.db import _rebuild_webhook_url_for_provider
    base = "http://demo.example.com/webhook/gitcode"
    # 存量回归：github 语义与历史字符串替换一致
    assert _rebuild_webhook_url_for_provider(base, "github") == \
        "http://demo.example.com/webhook/github"
    assert _rebuild_webhook_url_for_provider(base, "gitcode") == base
    # 新平台：按卡重建
    assert _rebuild_webhook_url_for_provider(base, "gitee") == \
        "http://demo.example.com/webhook/gitee"
    # 无卡未知平台：原样返回（不猜）
    assert _rebuild_webhook_url_for_provider(base, "unknown") == base


def test_s20_rebuild_webhook_url_bare_base(gitee_card):
    """裸基址（无 /webhook/* 段，例如隧道基址）必须补出平台路径。

    2026-11-22 线上实锤：create_new_repo / host_repo / repair_host 三处旧写法
    只在基址已含 "/webhook/" 时才替换 → 裸基址原样下发，平台侧 hook 落到 "/"
    → nginx 只放行 /webhook/ → 回调 404（gitcode:org-a/repo-a，
    hook url 曾是 https://<隧道基址> 缺 /webhook/gitcode）。三处已统一走本函数。
    """
    from src.domains.mgmt.user.db import _rebuild_webhook_url_for_provider
    bare = "https://tunnel.example.com"
    assert _rebuild_webhook_url_for_provider(bare, "gitcode") == bare + "/webhook/gitcode"
    assert _rebuild_webhook_url_for_provider(bare + "/", "gitee") == bare + "/webhook/gitee"
    assert _rebuild_webhook_url_for_provider(bare, "github") == bare + "/webhook/github"


# ---------------------------------------------------------------- S15：解绑级联

def _mk_account(prefix="s15"):
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    import uuid
    acc = create_grape_account(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
                               email="", password="pw")
    assert acc
    return acc


def test_s15_unbind_cascades_to_oauth_fill_credential():
    """解绑 external_identity → token 清空 → get_user_token_for 失效（S10/S15 语义）。"""
    from src.domains.mgmt.auth.grape_account_db import create_external_identity, delete_external_identity
    from src.api.deps import get_user_token_for
    acc = _mk_account()
    create_external_identity(acc["id"], "gitcode", "91015",
                             external_login="l91015",
                             access_token="at_before_unbind")
    assert get_user_token_for(acc["username"], acc["id"], "gitcode") == "at_before_unbind"
    # 解绑（既有行为：status=0 + token 清空）
    ok = delete_external_identity(acc["id"], "gitcode")
    assert ok
    assert get_user_token_for(acc["username"], acc["id"], "gitcode") == ""


def test_s15_manual_pat_survives_unbind():
    """手配 PAT（user_credentials）与登录绑定是两个对象，解绑不波及（S11 反向）。"""
    from src.domains.mgmt.auth.grape_account_db import create_external_identity, delete_external_identity
    acc = _mk_account()
    create_external_identity(acc["id"], "gitcode", "91016", access_token="at_oauth")
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json, updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (acc["id"], "gitee_pat", json.dumps({"token": "pat_manual"})))
        db.commit()
    delete_external_identity(acc["id"], "gitcode")
    with db_lock:
        row = db.cursor().execute(
            "SELECT config_json FROM user_credentials WHERE user_id=? AND provider='gitee_pat'",
            (acc["id"],)).fetchone()
    assert row and json.loads(row[0])["token"] == "pat_manual"
    with db_lock:
        db.cursor().execute("DELETE FROM user_credentials WHERE user_id=? AND provider='gitee_pat'",
                            (acc["id"],))
        db.commit()


def test_s15_delete_pat_does_not_touch_login_binding():
    from src.domains.mgmt.auth.grape_account_db import create_external_identity, get_user_identities_with_tokens
    acc = _mk_account()
    create_external_identity(acc["id"], "gitcode", "91017", access_token="at_keep")
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, provider, config_json) "
            "VALUES (?, 'pat_del', '{}')", (acc["id"],))
        db.commit()
        db.cursor().execute("DELETE FROM user_credentials WHERE user_id=? AND provider='pat_del'",
                            (acc["id"],))
        db.commit()
    idents = get_user_identities_with_tokens(acc["id"])
    assert any(i.get("provider") == "gitcode" for i in idents)
