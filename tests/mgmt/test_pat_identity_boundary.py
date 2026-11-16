"""PAT 与第三方身份归属边界（2026-11-13 现网实测 bug 的回归用例）。

现网复现链（0001 实例 12:06~12:08 日志 + 库状态）：
  1. 用户 B 在 settings 用 platform_tokens 配 gitee PAT，该 gitee 身份曾归用户 A
     且 A 已解绑（external_identities.status=0）。
  2. 旧 settings 守卫只查 status=1（get_external_identity），解绑记录漏过 →
     落进 create_external_identity 的复活分支。
  3. 复活分支只把 status 改回 1、**不改 grape_user_id** → 身份仍在 A 名下，
     PAT 凭据写进 B，前端提示「绑定成功」——既没转移也没报错。
  4. /repos/member 只按身份行判定可列平台 → B 拿到「无 VCS 绑定」引导页，
     尽管 B 名下已有 gitee_pat。

本文件锁三件事：
  T1 create_external_identity 不得复活他人解绑行（防御位，即使调用方漏查）
  T2 泛型 platform_tokens 路径对他人解绑行回 409 + need_transfer + 票据，
     且失败时**不写**当前用户凭据（不留半转移态）
  T3 可列仓平台 = 身份行 ∪ 凭据域 <p>_pat（只配 PAT 也能列仓）
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache

GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}
EXT_ID = "13450218"
EXT_LOGIN = "gitee-bob"


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


class _P:
    """create_provider 假实现：verify_token 返回固定 gitee 身份。"""

    def verify_token(self, token):
        assert token == "gitee-pat-1"
        return {"id": EXT_ID, "login": EXT_LOGIN, "name": "Bob"}


def _mk_user(prefix: str) -> dict:
    from src.domains.mgmt.auth import grape_account_db
    uname = f"{prefix}_{uuid.uuid4().hex[:8]}"
    u = grape_account_db.create_grape_account(
        username=uname, email=f"{uname}@test.local", password="pw123")
    assert u, f"用户构造失败: {uname}"
    return u


def _drop_user(uid: str) -> None:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.commit()


def _bind_then_unbind(uid: str) -> None:
    """造「曾绑定后解绑」的 status=0 记录（现网 A 账号的形态）。"""
    from src.domains.mgmt.auth import grape_account_db
    assert grape_account_db.create_external_identity(
        grape_user_id=uid, provider="gitee", external_id=EXT_ID,
        external_login=EXT_LOGIN)
    assert grape_account_db.delete_external_identity(uid, "gitee")


# ───────────────────────── T1 create_external_identity 防御位 ─────────────────────────

def test_t1_refuse_revive_other_user_unbound_identity():
    """他人解绑行不得被静默复活：返回 False，且归属/状态原样不动。"""
    from src.domains.mgmt.auth import grape_account_db
    a = _mk_user("patb_a")
    b = _mk_user("patb_b")
    try:
        _bind_then_unbind(a["id"])
        before = grape_account_db.get_external_identity_any_status("gitee", EXT_ID)
        assert before and str(before["grape_user_id"]) == str(a["id"])
        assert str(before["status"]) == "0"

        ok = grape_account_db.create_external_identity(
            grape_user_id=b["id"], provider="gitee", external_id=EXT_ID,
            external_login=EXT_LOGIN)

        assert ok is False, "他人解绑记录被静默复活"
        after = grape_account_db.get_external_identity_any_status("gitee", EXT_ID)
        assert after and str(after["grape_user_id"]) == str(a["id"]), "归属被改写"
        assert str(after["status"]) == "0", "他人解绑记录被置回活跃"
        # 活跃查询不得命中（否则 B 的 /repos/member 会拿到 A 的身份）
        assert grape_account_db.get_external_identity("gitee", EXT_ID) is None
    finally:
        _drop_user(a["id"])
        _drop_user(b["id"])


def test_t1_same_user_unbound_revive_still_works():
    """特征锁：本人解绑记录仍可恢复（基线行为不回归）。"""
    from src.domains.mgmt.auth import grape_account_db
    a = _mk_user("patb_self")
    try:
        _bind_then_unbind(a["id"])
        ok = grape_account_db.create_external_identity(
            grape_user_id=a["id"], provider="gitee", external_id=EXT_ID,
            external_login=EXT_LOGIN)
        assert ok is True
        ident = grape_account_db.get_external_identity("gitee", EXT_ID)
        assert ident and str(ident["grape_user_id"]) == str(a["id"])
    finally:
        _drop_user(a["id"])


# ───────────────────── T2 泛型 PAT 保存：他人解绑行 → 409 + 票据 ─────────────────────

def test_t2_pat_save_does_not_touch_identity_rows(gitee_card):
    """PAT 保存只落凭据域：不建/不激活身份行，他人解绑记录保持原状。

    A 批对象模型（2026-11-13 用户裁定）：PAT = 仓库执行凭据，不是身份绑定。
    因此这里既没有 409，也没有转移票据——归属行只由 OAuth 绑定产生。
    """
    from unittest.mock import patch
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    from src.domains.mgmt.user.db import get_credential
    from src.domains.mgmt.auth import grape_account_db

    a = _mk_user("patb_t2a")
    b = _mk_user("patb_t2b")
    try:
        _bind_then_unbind(a["id"])
        body = UpdateCredentials(platform_tokens={"gitee": "gitee-pat-1"})
        with patch("src.kernel.token_resolver.create_provider", return_value=_P()):
            update_credentials(body, user={"id": b["id"]})

        # 凭据落 B 名下（PAT 的语义就是这个）
        cred = get_credential(b["id"], "gitee_pat") or {}
        assert cred.get("token") == "gitee-pat-1"
        assert cred.get("login") == EXT_LOGIN

        # 身份行一行都不能被动：仍归 A、仍是解绑态、B 没有任何 gitee 身份
        ident = grape_account_db.get_external_identity_any_status("gitee", EXT_ID)
        assert str(ident["grape_user_id"]) == str(a["id"])
        assert str(ident["status"]) == "0"
        assert [(i["provider"], i.get("source"))
                for i in grape_account_db.list_platform_links(b["id"])] == [("gitee", "pat")]
    finally:
        _drop_user(a["id"])
        _drop_user(b["id"])


# ───────────────── T3 可列仓平台 = 身份行 ∪ 凭据域 <p>_pat ─────────────────

@pytest.fixture()
def client_with_pat():
    """只配 gitee_pat、无任何身份行的账号（现网 B 账号的形态）。"""
    from src.api.main import app
    from src.api.deps import get_current_user, get_current_user_or_api_key
    from src.domains.mgmt.user import db as user_db

    a = _mk_user("patb_t3")
    user_db.set_credential(a["id"], "gitee", {"token": "gitee-pat-1", "login": EXT_LOGIN}, kind="pat")
    saved = {}

    orig = (user_db.get_user_repos, user_db.get_user_repos_synced_at, user_db.save_user_repos)
    user_db.get_user_repos = lambda uid: []
    user_db.get_user_repos_synced_at = lambda uid: None
    # save_user_repos 自 2026-11-14 起带 providers=（部分刷新，只替换指定平台的行）
    user_db.save_user_repos = lambda uid, repos, **kw: (saved.update({"repos": list(repos)}) or True)

    user = {"id": a["id"], "username": "patb_t3", "auth_type": "jwt", "org_id": 0}
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_or_api_key] = lambda: user
    try:
        yield TestClient(app), a
    finally:
        app.dependency_overrides.clear()
        user_db.get_user_repos, user_db.get_user_repos_synced_at, user_db.save_user_repos = orig
        _drop_user(a["id"])


def test_t3_member_lists_with_pat_only_no_identity(gitee_card, client_with_pat, monkeypatch):
    """只配 PAT：不得返回 _no_vcs_binding 引导页，且按 gitee 拉仓。"""
    from src.domains.mgmt.routes import repos as repos_route

    calls = []

    def _fake_with_token(user, provider, fn, *a, **kw):
        calls.append(provider)
        return []

    monkeypatch.setattr(repos_route, "with_oauth_token", _fake_with_token)

    client, _ = client_with_pat
    r = client.get("/api/repos/member")
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body.get("_no_vcs_binding"), f"只配 PAT 仍被判无绑定: {body}"
    assert "gitee" in calls, f"gitee 未进入拉仓平台集合: {calls}"


def test_t3_sync_lists_with_pat_only_no_identity(gitee_card, client_with_pat, monkeypatch):
    """只配 PAT：/repos/sync 同样不得 400「API Token 未配置」。"""
    from src.domains.mgmt.routes import repos as repos_route

    calls = []

    def _fake_with_token(user, provider, fn, *a, **kw):
        calls.append(provider)
        return []

    monkeypatch.setattr(repos_route, "with_oauth_token", _fake_with_token)

    client, _ = client_with_pat
    r = client.post("/api/repos/sync")
    assert r.status_code == 200, r.text
    assert "gitee" in calls, f"gitee 未进入同步平台集合: {calls}"


# ───────────── T4 同平台换账号：显式切换，不撞唯一约束、不静默半成功 ─────────────

def test_t4_switch_to_new_account_succeeds_explicitly():
    """同用户换平台账号：显式切换并留 warning（旧身份被约束替换）。"""
    from src.domains.mgmt.auth import grape_account_db
    a = _mk_user("patb_t4")
    try:
        assert grape_account_db.create_external_identity(
            grape_user_id=a["id"], provider="gitee", external_id="old-ext-1",
            external_login="gitee-old")
        ok = grape_account_db.create_external_identity(
            grape_user_id=a["id"], provider="gitee", external_id="new-ext-2",
            external_login="gitee-new")
        assert ok is True, "同用户换账号不应失败（旧实现可能撞 UNIQUE 后静默 False）"
        ident = grape_account_db.get_external_identity("gitee", "new-ext-2")
        assert ident and str(ident["grape_user_id"]) == str(a["id"])
        assert grape_account_db.get_external_identity("gitee", "old-ext-1") is None
        assert grape_account_db.get_external_identity_any_status("gitee", "old-ext-1") is None
    finally:
        _drop_user(a["id"])


def test_t4_switch_to_account_owned_by_other_is_refused():
    """换到的目标账号已归他人 → 拒绝，当前用户的活跃身份保持不变。"""
    from src.domains.mgmt.auth import grape_account_db
    a = _mk_user("patb_t4a")
    b = _mk_user("patb_t4b")
    try:
        assert grape_account_db.create_external_identity(
            grape_user_id=a["id"], provider="gitee", external_id="a-ext-1",
            external_login="a-login")
        assert grape_account_db.create_external_identity(
            grape_user_id=b["id"], provider="gitee", external_id="b-ext-2",
            external_login="b-login")

        ok = grape_account_db.create_external_identity(
            grape_user_id=a["id"], provider="gitee", external_id="b-ext-2",
            external_login="b-login")
        assert ok is False, "抢绑他人活跃身份未被拒"

        kept = grape_account_db.get_external_identity("gitee", "a-ext-1")
        assert kept and str(kept["grape_user_id"]) == str(a["id"]), "拒绑后原身份被破坏"
        other = grape_account_db.get_external_identity("gitee", "b-ext-2")
        assert other and str(other["grape_user_id"]) == str(b["id"])
    finally:
        _drop_user(a["id"])
        _drop_user(b["id"])


# ───────────── T5 凭据写失败必须 loud + 身份补偿 ─────────────

def test_t5_credential_write_failure_is_loud_and_compensated(gitee_card):
    """set_credential 返回 False：抛 500，且不产生任何身份行（PAT 本就不写归属）。"""
    from unittest.mock import patch
    from fastapi import HTTPException
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    from src.domains.mgmt.auth import grape_account_db

    b = _mk_user("patb_t5")
    try:
        body = UpdateCredentials(platform_tokens={"gitee": "gitee-pat-1"})
        with patch("src.kernel.token_resolver.create_provider", return_value=_P()), \
                patch("src.domains.mgmt.user.db.set_credential", return_value=False):
            with pytest.raises(HTTPException) as ei:
                update_credentials(body, user={"id": b["id"]})
        assert ei.value.status_code == 500, "凭据写失败未 loud"
        assert grape_account_db.get_external_identity("gitee", EXT_ID) is None, \
            "凭据失败后归属行残留（半成功）"
    finally:
        _drop_user(b["id"])


def test_t6_mark_identity_pat_missing_row_logs_warning(caplog):
    """归属行不存在时 mark_identity_pat 必须告警，不再静默空转。"""
    import logging
    from src.domains.mgmt.auth import grape_account_db
    with caplog.at_level(logging.WARNING):
        grape_account_db.mark_identity_pat("no-such-user-id", "gitee", pat_login="x")
    assert any("mark_identity_pat" in r.message for r in caplog.records), \
        f"缺归属行时未告警: {[r.message for r in caplog.records]}"


def test_t7_platform_tokens_change_invalidates_repo_cache(gitee_card):
    """T7：泛型 platform_tokens 变更必须清 user_repos 缓存。

    行为缺口：缓存失效判定只认 `<p>_token` / `<p>_pat`，而卡片驱动的新平台走的是
    `platform_tokens` 复数键 —— 不清缓存时 /repos/member 会在 TTL 内直接早退返回
    旧快照，新平台的仓库要等过期或手动刷新才出现。
    清除路径复用同一个条件语句，所以这条同时守住「清 PAT 后列表残留」。
    """
    from unittest.mock import patch
    from src.domains.mgmt.routes.settings import UpdateCredentials, update_credentials
    from src.domains.mgmt.user import db as user_db

    a = _mk_user("patb_t7")
    try:
        user_db.save_user_repos(a["id"], [{"repo_key": "gitcode:o/r", "owner": "o", "name": "r"}])
        assert user_db.get_user_repos(a["id"]), "前置：缓存应已写入"

        body = UpdateCredentials(platform_tokens={"gitee": "gitee-pat-1"})
        with patch("src.kernel.token_resolver.create_provider", return_value=_P()):
            update_credentials(body, user={"id": a["id"]})

        assert not user_db.get_user_repos(a["id"]), \
            "platform_tokens 变更后 user_repos 缓存未失效（项目页会继续显示旧快照）"
    finally:
        _drop_user(a["id"])