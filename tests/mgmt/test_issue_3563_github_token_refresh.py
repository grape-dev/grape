"""Issue #3563 A 侧：GitHub OAuth 预刷新链单测（D100 已落地，本单只补测）。

覆盖验收两条：
1. 预刷新成功链：external_identities.token_expires_at=now+60s 时，
   get_user_github_token_with_refresh 触发 refresh 恰一次、返回新 token、
   行被更新（access_token 前移、token_expires_at 前移）。
2. 预刷新失败链：refresh 失败 → mark_identity_expired（status=4）且旧 token 返回；
   再经 with_oauth_token 包装必 401 的调用 → HTTPException 401 +
   detail.action=reauth_required + provider（Spec v2：不新增 error_code）。

另含 grep 断言：src/api/deps.py 预刷新判定链不再读写 user_credentials.<p>_oauth
（deps.py 内 github_oauth 命中数 = 0）。

刷新链路（D100，不重写）：get_user_token_for → token_expires_at_any（<600s）→
_try_refresh_user_token（provider_link 注册表）→ _try_refresh_github_token
（分布式锁 oauth_refresh:{user_id}:github）→ 失败 mark_identity_expired（status=4）。
"""

import os
import time
import uuid

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from fastapi import HTTPException

from src.kernel.session_manager import db, db_lock


def _mk_github_oauth_user(expires_in: int = 60):
    """真实 grape_accounts + external_identities 行（GitHub，带 refresh_token）。"""
    from src.domains.mgmt.auth.grape_account_db import (
        create_external_identity, create_grape_account)
    uname = "s3563a_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@test.local", password="pw123")
    uid = str(u["id"])
    ok = create_external_identity(
        grape_user_id=uid, provider="github",
        external_id="gh3563a_" + uuid.uuid4().hex[:6],
        external_login=uname, access_token="old_tok_3563", refresh_token="rt_3563",
        expires_in=expires_in)
    assert ok
    return uid, uname


def _cleanup(uid: str, uname: str, provider: str = "github"):
    with db_lock:
        db.execute("DELETE FROM external_identities WHERE grape_user_id=? AND provider=?", (uid, provider))
        db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.commit()


class TestGithubTokenPreRefresh:
    """预刷新成功/失败两链（生产代码 get_user_token_for，仅 mock 刷新派发点）。"""

    def test_pre_refresh_success_refresh_once_new_token_row_updated(self, monkeypatch):
        """expires=now+60s → refresh 恰一次、返回新 token、行已更新。"""
        from src.domains.mgmt.auth.grape_account_db import update_external_identity_tokens
        uid, uname = _mk_github_oauth_user(expires_in=60)
        try:
            calls = []

            def _fake_refresh(user_id: str, provider: str):
                calls.append((user_id, provider))
                # 模拟真实刷新成功回写：新 token + 前移到期时间（生产链 refresh_oauth_token 同口径）
                update_external_identity_tokens(user_id, provider,
                                                 access_token="new_tok_3563",
                                                 refresh_token="rt_3563_2",
                                                 expires_in=28800)
                return "new_tok_3563"

            monkeypatch.setattr("src.api.deps._try_refresh_user_token", _fake_refresh)
            from src.api.deps import get_user_github_token_with_refresh
            token = get_user_github_token_with_refresh(uname, user_id=uid)

            assert token == "new_tok_3563"
            assert len(calls) == 1, f"refresh 应恰被调用一次，实际 {len(calls)}"
            assert calls[0] == (uid, "github")
            # 行被更新：加密存储回读解密 + 到期时间前移
            from src.domains.mgmt.public import stored_access_token_any, token_expires_at_any
            from src.domains.mgmt.auth.credential_crypto import decrypt
            assert decrypt(stored_access_token_any(uid, "github")) == "new_tok_3563"
            _exp = token_expires_at_any(uid, "github")
            assert _exp, "token_expires_at 应被回写"
            from src.api.deps import _parse_utc_timestamp
            assert _parse_utc_timestamp(_exp) > int(time.time()) + 60
        finally:
            _cleanup(uid, uname)

    def test_pre_refresh_failure_marks_expired_and_401_reauth(self, monkeypatch):
        """refresh 失败 → status=4 + 旧 token 返回；后续 401 调用 → 401 reauth_required。"""
        uid, uname = _mk_github_oauth_user(expires_in=60)
        try:
            monkeypatch.setattr("src.api.deps._try_refresh_user_token",
                                lambda user_id, provider: None)
            from src.api.deps import get_user_github_token_with_refresh
            token = get_user_github_token_with_refresh(uname, user_id=uid)
            assert token == "old_tok_3563", "刷新失败应返回旧 token（不清除）"

            from src.domains.mgmt.auth.grape_account_db import is_identity_expired
            assert is_identity_expired(uid, "github") is True, "mark_identity_expired 应落 status=4"

            # 过期 token 走接口链：上游 401 → with_oauth_token 刷新重试 → 仍失败 → 401 reauth_required
            monkeypatch.setattr("src.domains.mgmt.user.db.refresh_oauth_token",
                                lambda user_id, provider: None)
            from src.api.oauth_token import with_oauth_token

            def _api(tok):
                raise RuntimeError("GitHub API GET /user failed (401): Bad credentials")

            with pytest.raises(HTTPException) as ei:
                with_oauth_token({"id": uid, "username": uname}, "github", _api)
            assert ei.value.status_code == 401
            detail = ei.value.detail
            assert isinstance(detail, dict)
            assert detail.get("action") == "reauth_required"
            assert detail.get("provider") == "github"
            assert detail.get("code") == 401
            assert detail.get("message") == "GitHub 授权已过期，请重新登录授权"
            assert "error_code" not in detail, "Spec v2：不新增 error_code（用户裁定）"
            assert "api.github.com" not in str(detail)
        finally:
            _cleanup(uid, uname)


class TestDepsPreRefreshChainGrep:
    """grep 断言：预刷新判定链不再读写 user_credentials.<p>_oauth。"""

    def test_deps_py_has_no_legacy_user_credentials_oauth_snapshot(self):
        """src/api/deps.py 内 github_oauth 命中数 = 0。

        D100 改道后预刷新读 external_identities.token_expires_at（权威存储），
        不再摸 user_credentials.github_oauth 快照。
        （注：try_refresh_gitcode_token 的实现名 refresh_gitcode_oauth_token
        含 "gitcode_oauth" 子串属函数名非快照字段，不在断言范围。）
        """
        deps_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "src", "api", "deps.py")
        with open(deps_path, "r", encoding="utf-8") as f:
            src = f.read()
        assert "github_oauth" not in src, "deps.py 不应再引用 user_credentials.github_oauth"
