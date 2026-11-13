"""D32（S52）用例：先测后改——按单测补齐再完成优化（用户纪律：不能把对的改错）。

三件优化各配 RED 断言（新能力）+ 特征锁（旧行为不变）：
1. webhook 头集合透传（方言验签不受路由头过滤截断）
2. GitLabDialect event_map_overrides 生效（原死参数）
3. gitee PAT 绑定入口（settings.platform_tokens，泛型 <p>_token，
   github/gitcode 既有路径零改动）
全部单元测试：fake request + meta 卡 + mock 委托点，不碰真实平台。
"""
import asyncio
import json
from unittest.mock import patch, MagicMock

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


class _FakeWebhookRequest:
    def __init__(self, headers: dict, body: bytes):
        self._headers = {str(k).lower(): v for k, v in headers.items()}
        self._body = body

    @property
    def headers(self):
        return self  # .items() 可用

    def items(self):
        return list(self._headers.items())

    async def body(self):
        return self._body


# ------------------------------------------------------- 1. 头集合透传

def _mock_backend(capture):
    b = MagicMock()
    b.event_header_key = "X-Gitee-Event"
    b.normalize.return_value = {"repository": "acme/web", "type": "issue_opened"}
    b.verify = lambda headers, body, secret: (
        capture.update({str(k).lower(): v for k, v in headers.items()}) or (True, "ok"))
    return b


def test_s52_webhook_headers_passthrough(gitee_card):
    """路由不再截断头集合：backend.verify 收到全部头（含方言外的额外头）。
    旧行为锁：已知头名验签/归一流程不变。"""
    from src.domains.runtime.routes.webhook import generic_vcs_webhook
    capture = {}
    b = _mock_backend(capture)
    body = json.dumps({"repository": {"full_name": "acme/web"}}).encode()
    req = _FakeWebhookRequest(
        {"X-Gitee-Event": "issue_comment", "x-gitee-signature": "sec",
         "X-Forge-Signature": "custom-header-should-pass-through"}, body)
    with patch("src.domains.runtime.vcs.adapter.get_backend", return_value=b), \
         patch("src.domains.mgmt.user.db.get_webhook_token_by_repo", return_value="sec"), \
         patch("src.domains.runtime.routes.webhook.run_pipeline") as rp:
        rp.return_value = {"ok": True}
        resp = asyncio.run(generic_vcs_webhook("gitee", req))
    assert not (hasattr(resp, "status_code") and resp.status_code == 404)
    # 额外头必须透传到方言层（修复前被路由头过滤截断 → RED）
    assert capture.get("x-forge-signature") == "custom-header-should-pass-through"
    # 旧行为锁：方言已认的头仍在
    assert capture.get("x-gitee-signature") == "sec"


def test_s52_webhook_verify_failure_still_rejected(gitee_card):
    """特征锁：验签失败仍 401（透传不放松校验）。"""
    from src.domains.runtime.routes.webhook import generic_vcs_webhook
    b = MagicMock()
    b.event_header_key = "X-Gitee-Event"
    b.normalize.return_value = {"repository": "acme/web"}
    b.verify.return_value = (False, "signature mismatch")
    req = _FakeWebhookRequest({"X-Gitee-Event": "push"}, b"{}")
    with patch("src.domains.runtime.vcs.adapter.get_backend", return_value=b), \
         patch("src.domains.mgmt.user.db.get_webhook_token_by_repo", return_value="sec"):
        resp = asyncio.run(generic_vcs_webhook("gitee", req))
    assert resp.status_code == 403    # 路由实际语义：验签失败 403（特征锁按真实行为）


# ------------------------------------------------------- 2. GitLab 方言 overrides

def test_s52_gitlab_event_map_overrides_honored():
    """卡 event_map_overrides 对 GitLab 方言生效（原死参数 → RED）。"""
    from src.domains.runtime.vcs.dialects import GitLabDialect
    d = GitLabDialect()
    payload = {"object_kind": "wiki_page", "user": {"username": "bob"},
               "project": {"path_with_namespace": "acme/web"}}
    # 基线：未命中 kind → 原样透传
    base = d.normalize(payload, "", "gitlab")
    assert base["type"] == "wiki_page"
    # overrides 命中：映射到 canonical
    out = d.normalize(payload, "", "gitlab",
                      event_map_overrides={"wiki_page": "issue_updated"})
    assert out["type"] == "issue_updated"


def test_s52_gitlab_builtin_kinds_unchanged():
    """特征锁：GitLab 内置 kind 映射不被 overrides 机制破坏。"""
    from src.domains.runtime.vcs.dialects import GitLabDialect
    d = GitLabDialect()
    out = d.normalize({"object_kind": "merge_request",
                       "object_attributes": {"action": "open"},
                       "user": {"username": "bob"},
                       "project": {"path_with_namespace": "a/b"}}, "", "gitlab")
    assert out["type"] == "pull_request_opened"


# ------------------------------------------------------- 3. gitee PAT 绑定入口

def _fake_user(uid=501):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO grape_accounts (id, username, email, status) "
            "VALUES (?, 'pat_user', 'pat_user@t.local', 1)", (uid,))
        db.commit()
    with db_lock:
        db.cursor().execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
        db.cursor().execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        db.commit()
    return {"id": uid, "username": "pat_user"}


def _cleanup_user(uid=501):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.commit()


class _P:
    def verify_token(self, token):
        assert token == "gitee-pat-1"
        return {"id": 70001, "login": "gitee-bob", "name": "Bob"}


def test_s52_platform_tokens_gitee_pat_entry(gitee_card):
    """settings.platform_tokens={"gitee": tok} → 验证后只写凭据域：
    user_credentials gitee_pat（含 login）；**不写** external_identities。

    A 批对象模型（2026-11-13 用户裁定）：PAT 是仓库执行凭据，不是身份绑定。
    原断言为「双写身份行」，随对象模型归位改为「只写凭据 + 身份行零行」。
    """
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    user = _fake_user()
    try:
        body = UpdateCredentials(platform_tokens={"gitee": "gitee-pat-1"})
        with patch("src.kernel.token_resolver.create_provider", return_value=_P()):
            update_credentials(body, user=user)
        from src.domains.mgmt.user.db import get_credential
        cred = get_credential(user["id"], "gitee_pat")
        assert cred and cred.get("token") == "gitee-pat-1" and cred.get("login") == "gitee-bob"
        from src.domains.mgmt.auth.grape_account_db import get_external_identity_any_status
        assert get_external_identity_any_status("gitee", "70001") is None, \
            "PAT 保存不应创建身份行（身份行只由 OAuth 绑定产生）"
    finally:
        _cleanup_user()


def test_s52_platform_tokens_no_identity_conflict(gitee_card):
    """同一 gitee 账号的 PAT 可被多个 grape 账号各自配置，且不建身份行。

    A 批对象模型（原断言为「他人已绑 → 409」）：PAT = 仓库执行凭据，不是身份
    归属——归属行（external_identities）只由 OAuth 绑定产生，因此 PAT 保存不再
    有冲突校验，也不会改动他人已有的活跃身份行。
    """
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    user = _fake_user(502)
    other = _fake_user(503)
    try:
        from src.domains.mgmt.auth.grape_account_db import (
            create_external_identity, get_external_identity)
        create_external_identity(grape_user_id=other["id"], provider="gitee",
                                  external_id="70001", external_login="gitee-bob")
        body = UpdateCredentials(platform_tokens={"gitee": "gitee-pat-1"})
        with patch("src.kernel.token_resolver.create_provider", return_value=_P()):
            update_credentials(body, user=user)
        from src.domains.mgmt.user.db import get_credential
        cred = get_credential(user["id"], "gitee_pat")
        assert cred and cred.get("token") == "gitee-pat-1"
        ident = get_external_identity("gitee", "70001")
        assert ident and str(ident["grape_user_id"]) == str(other["id"]), \
            "PAT 保存篡改了他人身份行归属"
    finally:
        _cleanup_user(502)
        _cleanup_user(503)


def test_s52_platform_tokens_omitted_no_side_effects():
    """特征锁：不传 platform_tokens（存量前端形态）→ 零副作用。"""
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    user = _fake_user(504)
    try:
        body = UpdateCredentials()          # 全空 body（旧形态）
        update_credentials(body, user=user)
        from src.domains.mgmt.user.db import get_credential
        assert get_credential(user["id"], "gitee_pat") in (None, {})
    finally:
        _cleanup_user(504)


def test_s52_platform_tokens_rejects_unregistered_provider():
    """未配卡平台 → 拒绝（fail-closed，不静默）。"""
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    from fastapi import HTTPException
    user = _fake_user(505)
    try:
        body = UpdateCredentials(platform_tokens={"nope": "tok"})
        with pytest.raises(HTTPException) as ei:
            update_credentials(body, user=user)
        assert ei.value.status_code == 400
    finally:
        _cleanup_user(505)
