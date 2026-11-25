"""Issue #3427：凭据读侧失败原因可分辨——回归测试。

覆盖：
1. 单点解析器 resolve_credential_detailed：固定顺序 rc(bot)→global→user、
   命中来源写日志、解密失败/未配置/上游 401 三种失败各自返回不同 error_code；
2. host 入口：无凭据 400 TOKEN_MISSING + host_error 落库；
   同一有效 PAT 在 rc 表时 host 不再误报「未配置或已过期」；
3. PUT /api/repos/{owner}/{repo}/bot-token：写入即校验（GET /user）、
   401 → TOKEN_REJECTED_UPSTREAM、密文落库可解回；
4. a2a 403 与 token 错误解耦（REPO_NOT_BOUND_TO_USER）。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.api.deps import require_no_session_token  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

client = TestClient(app)

RK = "github:i3427/sample"
RK2 = "github:i3427/sample2"
RK_PATH_B = "github:i3427/pathb"
RK_GC = "gitcode:i3427/bot401"


class _StubProvider:
    """探活桩：provider_name / global_bot_status_meta_key / verify_token 可控。"""

    provider_name = "github"

    def __init__(self, verify_result=None, verify_exc=None):
        self._verify_result = verify_result or {"login": "stub-login"}
        self._verify_exc = verify_exc
        self.verify_calls = []

    def global_bot_status_meta_key(self):
        return "global_bot_github_status"

    def token_storage_field(self):
        return "bot_github_token"

    def verify_token(self, token):
        self.verify_calls.append(token)
        if self._verify_exc:
            raise self._verify_exc
        return self._verify_result


def _encrypt(raw: str) -> str:
    if not raw:
        return raw
    from src.domains.mgmt.auth.credential_crypto import encrypt
    return encrypt(raw)


def _upsert_rc(repo_key: str, value: str):
    from src.domains.insight import public as insight_public
    from datetime import datetime
    insight_public.upsert_repo_credential_value(
        repo_key, "bot", value, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _add_managed(repo_key: str, full_name: str, host_user_id: str = "0"):
    o, r = repo_key.split(":", 1)[1].split("/", 1)
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, host_user_id) VALUES (?,?,?,?,?)",
            (repo_key, o, r, full_name, host_user_id))
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        for rk in (RK, RK2, RK_PATH_B, RK_GC):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            c.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        c.execute("DELETE FROM meta WHERE key IN "
                  "('global_bot_github_token','global_bot_github_status',"
                  "'global_bot_gitcode_token','global_bot_gitcode_status',"
                  "'global_github_bot_token_status')")
        db.commit()
    try:
        from src.domains.mgmt.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
    except Exception:
        pass
    yield
    with db_lock:
        c = db.cursor()
        for rk in (RK, RK2, RK_PATH_B, RK_GC):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            c.execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        c.execute("DELETE FROM meta WHERE key IN "
                  "('global_bot_github_token','global_bot_github_status',"
                  "'global_bot_gitcode_token','global_bot_gitcode_status',"
                  "'global_github_bot_token_status')")
        db.commit()
    try:
        from src.domains.mgmt.user.db import _invalidate_global_bot_cache
        _invalidate_global_bot_cache()
    except Exception:
        pass


def _set_global_bot(value: str):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('global_bot_github_token', ?)",
            (value,))
        db.commit()
    from src.domains.mgmt.user.db import _invalidate_global_bot_cache
    _invalidate_global_bot_cache()


# ─────────────────────────── 解析器：固定顺序与来源 ───────────────────────────

def test_resolve_order_rc_wins_over_global_and_user(monkeypatch):
    """顺序固定：rc（仓库级 bot）命中即用，global/user 不再参与。"""
    _add_managed(RK, "i3427/sample")
    _upsert_rc(RK, _encrypt("rc-token"))
    _set_global_bot("global-token")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert res.ok and res.source == "rc" and res.token == "rc-token"
    assert res.level == "repo_bot"


def test_resolve_order_global_when_rc_missing(monkeypatch):
    _add_managed(RK, "i3427/sample")
    _set_global_bot("global-token")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert res.ok and res.source == "global" and res.token == "global-token"


def test_resolve_order_user_identity_fallback(monkeypatch):
    """全 bot 缺席 → 用户身份（external_identities）命中，source=user。"""
    _add_managed(RK, "i3427/sample", host_user_id="u-123")
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"id": str(uid), "username": "u1"})
    monkeypatch.setattr(
        "src.domains.mgmt.public.stored_access_token_any",
        lambda uid, p: _encrypt("user-token"))
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert res.ok and res.source == "user" and res.token == "user-token"


def test_resolve_path_b_skips_rc_and_global(monkeypatch):
    """路径 B（host_user_id 非空）跳过 rc/global，直接用户身份——与执行链一致。"""
    _add_managed(RK_PATH_B, "i3427/pathb", host_user_id="u-123")
    _upsert_rc(RK_PATH_B, _encrypt("rc-token"))
    _set_global_bot("global-token")
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"id": str(uid), "username": "u1"})
    monkeypatch.setattr(
        "src.domains.mgmt.public.stored_access_token_any",
        lambda uid, p: _encrypt("user-token"))
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK_PATH_B)
    assert res.ok and res.source == "user" and res.token == "user-token"


# ─────────────────────────── 解析器：失败原因分层 ───────────────────────────

def test_resolve_all_missing_token_missing():
    """未配置：全链空 → TOKEN_MISSING（区别于解密失败/上游 401）。"""
    _add_managed(RK, "i3427/sample")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert not res.ok and res.error_code == "TOKEN_MISSING"


def test_resolve_decrypt_failed_distinct_code(monkeypatch):
    """解密失败：rc 密文损坏 → TOKEN_DECRYPT_FAILED，不与未配置合并。"""
    _add_managed(RK, "i3427/sample")
    # 形如 key:nonce:ct 但解不开的密文（密钥变更/损坏的存量行）
    _upsert_rc(RK, "k1:Zm9v:YmFkY2lwaGVydGV4dA==")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert not res.ok and res.error_code == "TOKEN_DECRYPT_FAILED"


def test_resolve_user_identity_decrypt_failed(monkeypatch):
    """用户身份密文解不开（Issue 现象：external_identities 失效密文）→ TOKEN_DECRYPT_FAILED。"""
    _add_managed(RK, "i3427/sample", host_user_id="u-123")
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"id": str(uid), "username": "u1"})
    monkeypatch.setattr(
        "src.domains.mgmt.public.stored_access_token_any",
        lambda uid, p: "k1:Zm9v:YmFkY2lwaGVydGV4dA==")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert not res.ok and res.error_code == "TOKEN_DECRYPT_FAILED"


def test_resolve_probe_upstream_401_rejected(monkeypatch):
    """探活一次：解析成功 + 上游 GET /user 401 → TOKEN_REJECTED_UPSTREAM。"""
    _add_managed(RK, "i3427/sample")
    _upsert_rc(RK, _encrypt("stale-token"))

    class _Err(Exception):
        pass

    def _extract(exc):
        return 401

    monkeypatch.setattr("src.domains.runtime.vcs.token_error.extract_http_status_from_exc",
                        _extract)
    stub = _StubProvider(verify_exc=_Err("GitHub API ... 401"))
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK, provider=stub, probe=True)
    assert not res.ok and res.error_code == "TOKEN_REJECTED_UPSTREAM"


def test_resolve_probe_non_401_keeps_token(monkeypatch):
    """探活非 401（超时/5xx）不阻断，仍返回 token（R21.3.5 口径）。"""
    _add_managed(RK, "i3427/sample")
    _upsert_rc(RK, _encrypt("good-token"))

    class _Err(Exception):
        pass

    def _extract(exc):
        return 503

    monkeypatch.setattr("src.domains.runtime.vcs.token_error.extract_http_status_from_exc",
                        _extract)
    stub = _StubProvider(verify_exc=_Err("timeout"))
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK, provider=stub, probe=True)
    assert res.ok and res.token == "good-token" and res.source == "rc"


def test_resolve_probe_401_rc_falls_through_to_global(monkeypatch):
    """rc 探活 401 → 记录降级并回落 global（失效上层不遮蔽有效下层）。"""
    _add_managed(RK, "i3427/sample")
    _upsert_rc(RK, _encrypt("stale-rc"))
    _set_global_bot("global-token")

    class _Err(Exception):
        pass

    class _SelectiveStub(_StubProvider):
        def verify_token(self, token):
            self.verify_calls.append(token)
            if token == "stale-rc":
                raise _Err("401 Bad credentials")
            return {"login": "stub-login"}

    monkeypatch.setattr("src.domains.runtime.vcs.token_error.extract_http_status_from_exc",
                        lambda e: 401)
    stub = _SelectiveStub()
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK, provider=stub, probe=True)
    assert res.ok and res.source == "global" and res.token == "global-token"
    assert "rc:rejected_upstream" in res.degraded
    assert stub.verify_calls == ["stale-rc", "global-token"]


def test_resolve_decrypt_failed_rc_then_lower_layer_hit(monkeypatch):
    """rc 密文损坏但 global 有效：命中 global（不静默吞并，降级可观测）。"""
    _add_managed(RK, "i3427/sample")
    _upsert_rc(RK, "k1:Zm9v:YmFkY2lwaGVydGV4dA==")
    _set_global_bot("global-token")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert res.ok and res.source == "global" and res.token == "global-token"
    assert "rc:decrypt_failed" in res.degraded


def test_three_failure_codes_and_messages_distinct():
    """验收：三种失败情形 error_code 各异且文案可行动（互不相同且含指引）。"""
    from src.kernel.credential_errors import (CREDENTIAL_ERROR_MESSAGES,
                                              CREDENTIAL_ERROR_HTTP_STATUS,
                                              credential_error_detail)
    codes = ["TOKEN_MISSING", "TOKEN_DECRYPT_FAILED", "TOKEN_REJECTED_UPSTREAM",
             "REPO_NOT_BOUND_TO_USER"]
    msgs = [CREDENTIAL_ERROR_MESSAGES[c] for c in codes]
    assert len(set(msgs)) == len(msgs)
    for c, m in zip(codes, msgs):
        assert len(m) > 20, "文案必须可行动而非一句空话"
    assert CREDENTIAL_ERROR_HTTP_STATUS["TOKEN_REJECTED_UPSTREAM"] == 401
    assert CREDENTIAL_ERROR_HTTP_STATUS["REPO_NOT_BOUND_TO_USER"] == 403
    d = credential_error_detail("TOKEN_MISSING", "GitHub")
    assert d["error_code"] == "TOKEN_MISSING" and d["code"] == 400
    assert "bot-token" in d["message"]


# ─────────────────────────── host 入口 ───────────────────────────

def setup_module():
    app.dependency_overrides[require_no_session_token] = lambda: {
        "id": 2, "username": "i3427-user", "login_provider": "gitcode",
        "auth_type": "api_key", "role": "admin"}


def teardown_module():
    app.dependency_overrides.clear()


def _patch_host_network(monkeypatch, rec):
    """打桩 host GitHub 网络缝（计数 + 恒定返回）。"""
    monkeypatch.setattr("src.domains.mgmt.user.db.get_webhook_url",
                        lambda: "http://test.local/webhook/github")
    monkeypatch.setattr("src.domains.mgmt.routes.repos.with_oauth_token",
                        lambda *a, **k: None)
    monkeypatch.setattr("src.domains.mgmt.user.repo_manager.setup_webhook",
                        lambda *a, **k: 1000)
    monkeypatch.setattr("src.domains.mgmt.user.repo_manager.find_existing_webhook",
                        lambda *a, **k: None)
    monkeypatch.setattr(
        "src.domains.mgmt.user.repo_manager._github_req",
        lambda *a, **k: {"id": 999, "permissions": {"admin": True},
                         "owner": {"login": "i3427-user"}})


def test_host_no_credentials_400_token_missing_and_host_error(monkeypatch):
    """无凭据：400 TOKEN_MISSING；managed_repos.host_error 有值（#3427 验收）。"""
    _add_managed(RK2, "i3427/sample2")
    rec = {}
    _patch_host_network(monkeypatch, rec)
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "i3427/sample2"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"].get("error_code") == "TOKEN_MISSING"
    assert "未检测到任何可用凭据" in r.json()["detail"]["message"]
    # host_error 落库可查
    from src.domains.mgmt.user.db import get_host_detail
    d = get_host_detail(RK2)
    assert d.get("error"), "host 失败必须写 managed_repos.host_error"
    assert d.get("status") == "failed" and d.get("step") == "resolve_token"


def test_host_uses_rc_bot_token_no_false_missing(monkeypatch):
    """同一有效 PAT 复现：rc 表有 token 时 host 不再误报「未配置或已过期」。"""
    _add_managed(RK2, "i3427/sample2")
    _upsert_rc(RK2, _encrypt("pat-valid"))
    # 用户 token 缺席（旧实现在此必报「未配置或已过期」）
    monkeypatch.setattr("src.domains.mgmt.routes.repos.get_user_token_for",
                        lambda *a, **k: "")
    rec = {"tokens": []}
    _patch_host_network(monkeypatch, rec)
    stub = _StubProvider()
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "i3427/sample2"})
    assert r.status_code == 200, r.text
    assert stub.verify_calls and set(stub.verify_calls) == {"pat-valid"}, \
        "解析命中后必须探活（rc 命中另取绑定 login，可能再 verify 一次）"
    # Issue #3427（复审 Spec-2）：rc 命中时 bot_username 取绑定/verify login 并持久化
    from src.domains.mgmt.user.db import get_managed_repo_bot_username
    assert get_managed_repo_bot_username(RK2) == "stub-login"
    # Issue #3427（复审 Spec-1）：GitHub 显式绑定 rc 必须走路径 A，不得写回
    # host_user_id 转路径 B（否则后续解析器跳过 rc，绑定失效）。
    from src.domains.mgmt.user.db import get_host_user_id
    assert str(get_host_user_id(RK2) or "") in ("", "0")


def test_host_rc_uses_bound_bot_username_skips_global_invite(monkeypatch):
    """Issue #3427（复审 Spec-2）：source=rc 用绑定持久化的 bot_username，
    不回退全局 bot username，且跳过全局 bot 邀请（_try_github_bot_collaborator
    不得被调用）；落库不覆盖绑定 username 为空。"""
    from src.domains.mgmt.user.db import update_managed_repo_bot_username
    _add_managed(RK2, "i3427/sample2")
    _upsert_rc(RK2, _encrypt("rc-pat"))
    update_managed_repo_bot_username(RK2, "bound-bot")
    # 全局 bot 也配置（旧实现会邀请/落库全局 bot，且 username 为空时提前
    # not_configured）——rc 必须无视全局配置
    _set_global_bot("global-token")
    monkeypatch.setattr("src.domains.mgmt.routes.repos.get_user_token_for",
                        lambda *a, **k: "")
    _patch_host_network(monkeypatch, {})
    stub = _StubProvider()
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)

    def _no_invite(*a, **k):
        raise AssertionError("source=rc 不得走全局 bot 邀请流程")
    monkeypatch.setattr("src.domains.mgmt.routes.repos._try_github_bot_collaborator",
                        _no_invite)
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "i3427/sample2"})
    assert r.status_code == 200, r.text
    from src.domains.mgmt.user.db import get_managed_repo_bot_username, get_host_user_id
    # 落库保留绑定 username（不被覆盖为空），仍是路径 A
    assert get_managed_repo_bot_username(RK2) == "bound-bot"
    assert str(get_host_user_id(RK2) or "") in ("", "0")
    # rc 命中已探活，绑定 username 命中 DB，不再额外 verify
    assert stub.verify_calls == ["rc-pat"]


def test_host_source_user_borrows_no_global_bot_token(monkeypatch):
    """Issue #3427（复审 Spec-3）：source=user（用户身份）不得借全局 bot token
    判路径 A——与解析器首选一致，用户身份直接走路径 B。"""
    # host_user_id=2 与测试用户同 id（幂等重入不 403）
    _add_managed(RK2, "i3427/sample2", host_user_id="2")
    # 路径 B 下解析器跳过 rc/global，但全局 bot 配置仍存在——旧实现会
    # 用全局 bot 判路径 A 并邀请全局 bot
    _set_global_bot("global-token")
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"id": str(uid), "username": "i3427-user"})
    monkeypatch.setattr(
        "src.domains.mgmt.public.stored_access_token_any",
        lambda uid, p: _encrypt("user-token"))
    _patch_host_network(monkeypatch, {})
    stub = _StubProvider()
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)

    def _no_invite(*a, **k):
        raise AssertionError("source=user 不得邀请全局 bot（应走路径 B）")
    monkeypatch.setattr("src.domains.mgmt.routes.repos._try_github_bot_collaborator",
                        _no_invite)
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "i3427/sample2"})
    assert r.status_code == 200, r.text
    # 解析器首选用户身份（探活 user-token），并保持路径 B
    assert "user-token" in stub.verify_calls
    from src.domains.mgmt.user.db import get_host_user_id
    assert str(get_host_user_id(RK2) or "") not in ("", "0")


def test_credential_errors_token_verify_unavailable_layered():
    """Issue #3427（复审 Spec-5）：TOKEN_VERIFY_UNAVAILABLE 纳入 credential_errors
    分层（502 + 可行动文案），bind 端点不再内联裸 dict。"""
    from src.kernel.credential_errors import (CREDENTIAL_ERROR_HTTP_STATUS,
                                               credential_error_detail)
    assert CREDENTIAL_ERROR_HTTP_STATUS["TOKEN_VERIFY_UNAVAILABLE"] == 502
    d = credential_error_detail("TOKEN_VERIFY_UNAVAILABLE", "GitHub")
    assert d["error_code"] == "TOKEN_VERIFY_UNAVAILABLE" and d["code"] == 502
    assert "校验通道" in d["message"]


# ─────────────────────────── PUT bot-token 显式绑定 ───────────────────────────

def test_bind_bot_token_write_then_verify(monkeypatch):
    """写入即校验：校验通过 → 加密写 rc + 清过期标记 + 返回 login。"""
    _add_managed(RK, "i3427/sample")
    stub = _StubProvider(verify_result={"login": "grapedev-bot"})
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)
    r = client.put("/api/repos/i3427/sample/bot-token",
                   json={"bot_token": "ghp_new_pat"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bot_username"] == "grapedev-bot" and body["verified"] is True
    assert stub.verify_calls == ["ghp_new_pat"]
    # 落库为密文且可解回
    from src.domains.insight import public as insight_public
    raw = insight_public.repo_credential_value(RK, "bot")
    assert raw and raw != "ghp_new_pat"
    from src.domains.mgmt.auth.credential_crypto import decrypt
    assert decrypt(raw) == "ghp_new_pat"
    # 立即可被解析器命中（source=rc）
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert res.ok and res.source == "rc" and res.token == "ghp_new_pat"


def test_bind_bot_token_clears_path_b_host_user_id(monkeypatch):
    """路径 B 仓库显式绑定后清 host_user_id → 解析器可命中 rc（Issue 主场景修复）。"""
    _add_managed(RK_PATH_B, "i3427/pathb", host_user_id="u-123")
    stub = _StubProvider()
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)
    r = client.put("/api/repos/i3427/pathb/bot-token",
                   json={"bot_token": "ghp_pathb_pat"})
    assert r.status_code == 200, r.text
    from src.domains.mgmt.user.db import get_host_user_id
    assert str(get_host_user_id(RK_PATH_B) or "") in ("", "0")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK_PATH_B)
    assert res.ok and res.source == "rc" and res.token == "ghp_pathb_pat"


def test_bind_bot_token_rejected_401(monkeypatch):
    """校验 401 → HTTP 401 TOKEN_REJECTED_UPSTREAM（status 取 detail["code"]），且不落库。"""
    _add_managed(RK, "i3427/sample")

    class _Err(Exception):
        pass

    monkeypatch.setattr(
        "src.domains.runtime.vcs.token_error.extract_http_status_from_exc",
        lambda e: 401)
    stub = _StubProvider(verify_exc=_Err("401 Bad credentials"))
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)
    r = client.put("/api/repos/i3427/sample/bot-token",
                   json={"bot_token": "ghp_bad"})
    assert r.status_code == 401, r.text
    assert r.json()["detail"].get("error_code") == "TOKEN_REJECTED_UPSTREAM"
    from src.domains.insight import public as insight_public
    assert insight_public.repo_credential_value(RK, "bot") == ""


def test_bind_bot_token_repo_not_found(monkeypatch):
    r = client.put("/api/repos/i3427/ghost/bot-token",
                   json={"bot_token": "ghp_x"})
    assert r.status_code == 404, r.text


def test_bind_bot_token_empty_body(monkeypatch):
    _add_managed(RK, "i3427/sample")
    r = client.put("/api/repos/i3427/sample/bot-token", json={"bot_token": ""})
    assert r.status_code == 400
    assert r.json()["detail"].get("error_code") == "TOKEN_MISSING"


# ─────────────────────────── 建单入口：分层错误码 ───────────────────────────

def _run_create_task(repo_key: str):
    from fastapi import HTTPException
    from src.domains.runtime.issues_center import create_task_via_issue, TaskCreateBody
    body = TaskCreateBody(repo_key=repo_key, title="issue-3427", description="test")
    user = {"id": 2, "username": "i3427-user", "auth_type": "jwt", "role": "user"}
    try:
        asyncio.run(create_task_via_issue(body, user))
    except HTTPException as e:
        return e
    raise AssertionError("create_task_via_issue should raise HTTPException")


def test_create_task_missing_layered_code(monkeypatch):
    """建单入口无凭据 → TOKEN_MISSING + HTTP 400（分层码）。"""
    _add_managed(RK2, "i3427/sample2")
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo", lambda *a, **k: "")
    e = _run_create_task(RK2)
    assert e.status_code == 400
    assert e.detail.get("error_code") == "TOKEN_MISSING"


def test_create_task_decrypt_failed_layered_code(monkeypatch):
    """建单入口唯一凭据密文损坏 → TOKEN_DECRYPT_FAILED（不与未配置合并）。"""
    _add_managed(RK2, "i3427/sample2")
    _upsert_rc(RK2, "k1:Zm9v:YmFkY2lwaGVydGV4dA==")
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo", lambda *a, **k: "")
    e = _run_create_task(RK2)
    assert e.status_code == 400
    assert e.detail.get("error_code") == "TOKEN_DECRYPT_FAILED"


def test_create_task_rejected_upstream_status_401(monkeypatch):
    """建单入口上游 401 → HTTP 401（status 取 detail["code"]）+ 分层码。"""
    _add_managed(RK2, "i3427/sample2")
    _upsert_rc(RK2, _encrypt("stale-pat"))

    class _Err(Exception):
        pass

    stub = _StubProvider(verify_exc=_Err("401 Bad credentials"))
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)
    monkeypatch.setattr("src.domains.runtime.vcs.token_error.extract_http_status_from_exc",
                        lambda e: 401)
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo", lambda *a, **k: "")
    e = _run_create_task(RK2)
    assert e.status_code == 401
    assert e.detail.get("error_code") == "TOKEN_REJECTED_UPSTREAM"


# ─────────────────────────── a2a 403 解耦 ───────────────────────────

def test_a2a_403_decoupled_from_token_errors():
    """a2a 403 行为断言：未关联仓库 → REPO_NOT_BOUND_TO_USER，文案指向 user_repos。"""
    from src.api.deps import get_user_or_none
    app.dependency_overrides[get_user_or_none] = lambda: {
        "id": 999999, "username": "i3427-unbound", "auth_type": "jwt", "role": "user"}
    try:
        r = client.post("/api/a2a", json={
            "agent": "qa",
            "payload": {"repository": "i3427/sample", "issue_number": 3427,
                        "context": "test"}})
    finally:
        app.dependency_overrides.pop(get_user_or_none, None)
    assert r.status_code == 403, r.text
    d = r.json()["detail"]
    assert d.get("error_code") == "REPO_NOT_BOUND_TO_USER"
    assert "user_repos" in d.get("message", "")
    assert "TOKEN" not in d.get("error_code", "")


# ─────────────── 复审回归（re-review @ 4c1995a22 意见修复） ───────────────

def test_upsert_host_status_never_registers_repo():
    """未注册仓失败诊断不得落 managed_repos 主表（不污染 managed_repo_exists）。"""
    from src.domains.mgmt.user.db import upsert_host_status, managed_repo_exists
    _rk = "github:i3427/phantom"
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (_rk,))
        db.commit()
    try:
        assert managed_repo_exists(_rk) is False
        upsert_host_status(_rk, "failed", "resolve_token", "TOKEN_MISSING")
        # 存在性判定不被诊断污染：repair_host 自证放行 / bind 404 守卫仍生效
        assert managed_repo_exists(_rk) is False
        with db_lock:
            row = db.cursor().execute(
                "SELECT 1 FROM managed_repos WHERE repo_key=?", (_rk,)).fetchone()
        assert row is None
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (_rk,))
            db.commit()


def test_upsert_host_status_updates_registered_repo():
    """注册仓失败诊断仍正常落 host_status（UPDATE 路径不受影响）。"""
    from src.domains.mgmt.user.db import upsert_host_status, get_host_status
    _add_managed(RK, "i3427/sample")
    upsert_host_status(RK, "failed", "resolve_token", "TOKEN_MISSING")
    assert get_host_status(RK) == "failed"
    upsert_host_status(RK, "", "", "")
    assert get_host_status(RK) == ""


def test_credential_error_code_for_converges_switch():
    """`(cred.error_code if cred else "") or "TOKEN_MISSING"` 的单一出口。"""
    from src.kernel.credential_errors import credential_error_code_for
    from src.domains.mgmt.auth.credential_resolver import CredentialResolution
    assert credential_error_code_for(None) == "TOKEN_MISSING"
    assert credential_error_code_for(CredentialResolution(ok=False)) == "TOKEN_MISSING"
    assert credential_error_code_for(
        CredentialResolution(ok=False, error_code="TOKEN_REJECTED_UPSTREAM")
    ) == "TOKEN_REJECTED_UPSTREAM"


def test_create_task_rejected_upstream_beats_legacy_user_token(monkeypatch):
    """建单入口：解析器已判 401 时，非空 legacy 用户 token 不再兜底（401 分层不被吞）。"""
    _add_managed(RK2, "i3427/sample2")
    _upsert_rc(RK2, _encrypt("stale-pat"))

    class _Err(Exception):
        pass

    stub = _StubProvider(verify_exc=_Err("401 Bad credentials"))
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key", lambda rk: stub)
    monkeypatch.setattr("src.domains.runtime.vcs.token_error.extract_http_status_from_exc",
                        lambda e: 401)
    # legacy 用户 token 非空——旧实现会拿它继续；新实现必须直接 401
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo",
                        lambda *a, **k: "legacy-user-token")
    e = _run_create_task(RK2)
    assert e.status_code == 401
    assert e.detail.get("error_code") == "TOKEN_REJECTED_UPSTREAM"


def test_create_task_missing_falls_back_to_legacy_user_token(monkeypatch):
    """建单入口：解析器 TOKEN_MISSING 时 legacy 用户 token 仍可兜底（不回归）。"""
    _add_managed(RK2, "i3427/sample2")
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo",
                        lambda *a, **k: "legacy-user-token")
    captured = {}

    from src.api import issues_center as _ic

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):
        captured["token"] = api_token
        return None

    monkeypatch.setattr(_ic, "_find_open_issue_by_title", _fake_find)

    def _fake_create(self, title, body="", assignee="", **kwargs):
        return {"number": 3427}

    monkeypatch.setattr(
        "src.domains.runtime.vcs.api_client.VcsApiClient.create_issue", _fake_create)
    monkeypatch.setattr(_ic, "ensure_issue_task",
                        lambda *a, **k: 3427)
    from src.domains.runtime.issues_center import create_task_via_issue, TaskCreateBody
    body = TaskCreateBody(repo_key=RK2, title="issue-3427-fallback",
                          description="test", assignee="", scene_id="",
                          is_subtask_mode=0)
    user = {"id": 2, "username": "i3427-user", "auth_type": "jwt", "role": "user"}
    result = asyncio.run(create_task_via_issue(body, user))
    assert result.get("ok") is True, f"create failed: {result}"
    assert captured.get("token") == "legacy-user-token"


# ─────────────── 复审回归（re-review @ 4dabb630e 意见修复） ───────────────

def _patch_gitcode_resolver_ok(monkeypatch):
    """让解析器在 gitcode 仓上探活通过（bot token 本身可过 GET /user）。"""
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key",
        lambda rk: _StubProvider(verify_result={"login": "bot-login"}))


def _patch_no_user_refresh(monkeypatch):
    """用户 token 刷新缝打桩记录调用（本组用例中必须 0 调用）。"""
    calls = []
    monkeypatch.setattr("src.api.deps.try_refresh_gitcode_token",
                        lambda uid: calls.append(uid) or "")
    return calls


def test_create_task_rc_bot_dedup_401_no_user_reauth(monkeypatch):
    """rc bot 命中后查重 401：走 bot-token 文案 + 分层码，不触发用户刷新/OAuth。"""
    _add_managed(RK_GC, "i3427/bot401")
    _upsert_rc(RK_GC, _encrypt("rc-bot-token"))
    _patch_gitcode_resolver_ok(monkeypatch)
    calls = _patch_no_user_refresh(monkeypatch)

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):
        raise RuntimeError("GitCode API 查重 failed (401): bad credentials")

    monkeypatch.setattr("src.domains.runtime.issues_center._find_open_issue_by_title",
                        _fake_find)
    e = _run_create_task(RK_GC)
    # QA 复审（低）：状态码与 credential_errors 映射对齐 → TOKEN_REJECTED_UPSTREAM=401
    assert e.status_code == 401, e.detail
    assert e.detail.get("error_code") == "TOKEN_REJECTED_UPSTREAM"
    assert "Bot Token" in e.detail.get("message", "")
    assert "action" not in e.detail, "bot 来源 401 不得返回 reauth_required 跳 OAuth"
    assert calls == [], "bot 来源 401 不得触发用户 token 刷新"


def test_create_task_global_bot_dedup_401_no_user_reauth(monkeypatch):
    """global bot 命中后查重 401：同 rc 口径（bot-token 文案，无 reauth）。"""
    _add_managed(RK_GC, "i3427/bot401")
    # 探活桩 provider_name=github → global 层查 global_bot_github_token
    _set_global_bot("global-bot-token")
    _patch_gitcode_resolver_ok(monkeypatch)
    calls = _patch_no_user_refresh(monkeypatch)

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):
        assert api_token == "global-bot-token"
        raise RuntimeError("GitCode API 查重 failed (401): bad credentials")

    monkeypatch.setattr("src.domains.runtime.issues_center._find_open_issue_by_title",
                        _fake_find)
    e = _run_create_task(RK_GC)
    # QA 复审（低）：状态码与 credential_errors 映射对齐 → TOKEN_REJECTED_UPSTREAM=401
    assert e.status_code == 401, e.detail
    assert e.detail.get("error_code") == "TOKEN_REJECTED_UPSTREAM"
    assert "Bot Token" in e.detail.get("message", "")
    assert "action" not in e.detail
    assert calls == []


def test_create_task_rc_bot_create_401_no_user_reauth(monkeypatch):
    """rc bot 命中后建单 401：bot-token 文案 + 分层码，不触发用户刷新/OAuth。"""
    _add_managed(RK_GC, "i3427/bot401")
    _upsert_rc(RK_GC, _encrypt("rc-bot-token"))
    _patch_gitcode_resolver_ok(monkeypatch)
    calls = _patch_no_user_refresh(monkeypatch)
    monkeypatch.setattr("src.domains.runtime.issues_center._find_open_issue_by_title",
                        lambda *a, **k: None)

    def _fake_create(*a, **k):
        raise RuntimeError("GitCode API create failed (401): bad credentials")

    monkeypatch.setattr("src.domains.runtime.issues_center._create_issue_unified",
                        _fake_create)
    e = _run_create_task(RK_GC)
    # QA 复审（低）：状态码与 credential_errors 映射对齐 → TOKEN_REJECTED_UPSTREAM=401
    assert e.status_code == 401, e.detail
    assert e.detail.get("error_code") == "TOKEN_REJECTED_UPSTREAM"
    assert "Bot Token" in e.detail.get("message", "")
    assert "action" not in e.detail, "bot 来源 401 不得返回 reauth_required 跳 OAuth"
    assert calls == [], "bot 来源 401 不得触发用户 token 刷新"


def test_create_task_user_source_dedup_401_keeps_reauth(monkeypatch):
    """对照：解析器 source=user 命中后查重 401，仍走用户登录失效引导（不回归）。"""
    _add_managed(RK_GC, "i3427/bot401", host_user_id="u-9")
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"id": str(uid), "username": "i3427-user"})
    monkeypatch.setattr(
        "src.domains.mgmt.public.stored_access_token_any",
        lambda uid, p: _encrypt("user-source-token"))
    _patch_gitcode_resolver_ok(monkeypatch)
    calls = _patch_no_user_refresh(monkeypatch)

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):
        assert api_token == "user-source-token"
        raise RuntimeError("GitCode API 查重 failed (401): bad credentials")

    monkeypatch.setattr("src.domains.runtime.issues_center._find_open_issue_by_title",
                        _fake_find)
    e = _run_create_task(RK_GC)
    assert e.status_code == 401, e.detail
    assert e.detail.get("action") == "reauth_required"
    assert "重新绑定/登录 GitCode" in e.detail.get("message", "")
    assert calls, "用户来源 401 必须尝试刷新一次"


# ─────────────── 复审回归（re-review @ 70d0b7491 意见修复） ───────────────

def _set_legacy_bot(repo_key: str, token: str):
    """写 managed_repos 历史 bot 列（仅 gitcode/github 有物理列）。"""
    with db_lock:
        db.cursor().execute(
            "UPDATE managed_repos SET bot_github_token=? WHERE repo_key=?",
            (_encrypt(token), repo_key))
        db.commit()


def test_resolve_reads_legacy_bot_column_when_rc_missing(monkeypatch):
    """Spec-5：rc 为空时与执行链同口径回落历史 bot 列，不再误判 TOKEN_MISSING。"""
    _add_managed(RK, "i3427/sample")
    _set_legacy_bot(RK, "legacy-bot-token")
    stub = _StubProvider()
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK, provider=stub)
    assert res.ok and res.source == "rc" and res.token == "legacy-bot-token"


def test_host_error_text_token_missing_matches_layered_message(monkeypatch):
    """Standards-3：host_error 的 token_missing 文案与分层错误码文案一致。"""
    from src.domains.mgmt.routes.repos import _host_error_text
    from src.kernel.credential_errors import CREDENTIAL_ERROR_MESSAGES
    assert _host_error_text("token_missing") == \
        CREDENTIAL_ERROR_MESSAGES["TOKEN_MISSING"]


class _NoopAccess:
    def __call__(self, *a, **k):
        return None


def _with_non_admin_user(fn):
    _orig = app.dependency_overrides.get(require_no_session_token)
    app.dependency_overrides[require_no_session_token] = lambda: {
        "id": 77, "username": "intruder", "auth_type": "jwt", "role": "user"}
    try:
        return fn()
    finally:
        if _orig is not None:
            app.dependency_overrides[require_no_session_token] = _orig


def test_bind_bot_token_not_repo_owner_403(monkeypatch):
    """Standards-1：非托管人/非 admin 绑定 → 403 NOT_REPO_OWNER（bind 入口）。"""
    _add_managed(RK, "i3427/sample", host_user_id="u-owner")
    monkeypatch.setattr("src.api.deps.check_repo_access", _NoopAccess())

    def _run():
        return client.put("/api/repos/i3427/sample/bot-token",
                          json={"bot_token": "ghp_x"})
    r = _with_non_admin_user(_run)
    assert r.status_code == 403, r.text
    assert r.json()["detail"].get("error_code") == "NOT_REPO_OWNER"


def test_set_bot_token_not_repo_owner_403(monkeypatch):
    """Standards-1：非托管人/非 admin 配置 → 403 NOT_REPO_OWNER（set 入口）。"""
    _add_managed(RK, "i3427/sample", host_user_id="u-owner")
    monkeypatch.setattr("src.api.deps.check_repo_access", _NoopAccess())

    def _run():
        return client.put("/api/repos/bot-token",
                          json={"full_name": "i3427/sample", "bot_token": "ghp_x"})
    r = _with_non_admin_user(_run)
    assert r.status_code == 403, r.text
    assert r.json()["detail"].get("error_code") == "NOT_REPO_OWNER"


def test_v5_host_precheck_records_fail_record_exits(monkeypatch):
    """Standards-1：_v5_host_precheck 各失败出口按分流码落 host_error。"""
    from fastapi import HTTPException
    from src.domains.mgmt.routes import repos as R

    cases = [
        (HTTPException(status_code=403, detail={"error_code": "PERMISSION_DENIED"}),
         "permission_denied"),
        (RuntimeError("GitCode API GET x failed (404): missing"), "repo_not_found"),
        (RuntimeError("GitCode API GET x failed (401): bad token"), "permission_denied"),
        (RuntimeError("GitCode API GET x failed (403): forbidden"), "permission_denied"),
        (RuntimeError("GitCode API GET x failed (503): busy"), "upstream_unavailable"),
        (ValueError("boom"), "upstream_unavailable"),
    ]
    for exc, expected in cases:
        calls = []

        def _raise(*a, _exc=exc, **k):
            raise _exc

        monkeypatch.setattr(R, "with_oauth_token", _raise)
        try:
            R._v5_host_precheck("gitcode", "o", "r", "tok", {"id": 1},
                                lambda *a: calls.append(a))
        except Exception:
            pass
        assert calls, f"fail_record not called for {expected}"
        assert calls[0][1] == expected, calls


def test_bind_bot_token_provider_construction_structured_502(monkeypatch):
    """Standards-4：provider 构造异常返回结构化 502，不裸 500。"""
    _add_managed(RK, "i3427/sample")
    monkeypatch.setattr(
        "src.kernel.token_resolver.create_provider_from_repo_key",
        lambda rk: (_ for _ in ()).throw(ValueError("bad provider")))
    r = client.put("/api/repos/i3427/sample/bot-token",
                   json={"bot_token": "ghp_x"})
    assert r.status_code == 502, r.text
    assert r.json()["detail"].get("error_code") == "TOKEN_VERIFY_UNAVAILABLE"


def test_set_repo_bot_credential_rolls_back_rc_on_identity_clear_failure(monkeypatch):
    """Spec-5：清 host_user_id 失败时回滚 rc 写入，避免「报失败但已写入」。"""
    _add_managed(RK, "i3427/sample")
    from src.domains.mgmt.user import db as user_db
    from src.domains.insight import public as insight_public

    def _clear_fail(_rk):
        raise RuntimeError("clear identity failed")
    monkeypatch.setattr(user_db, "_clear_repo_path_b_identity", _clear_fail)
    with pytest.raises(RuntimeError):
        user_db.set_repo_bot_credential_encrypted(RK, "ghp_new")
    assert insight_public.repo_credential_value(RK, "bot") == ""


def test_create_task_github_user_source_401_not_bot_502(monkeypatch):
    """Spec-2：GitHub source=user 的 401 必须是 401（非 bot 502），且不跳 GitCode OAuth。"""
    _add_managed(RK2, "i3427/sample2", host_user_id="u-9")
    monkeypatch.setattr(
        "src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id",
        lambda uid: {"id": str(uid), "username": "i3427-user"})
    monkeypatch.setattr(
        "src.domains.mgmt.public.stored_access_token_any",
        lambda uid, p: _encrypt("github-user-token"))
    _patch_gitcode_resolver_ok(monkeypatch)

    def _fake_find(api_token, owner, repo, title, provider="", **kwargs):
        assert api_token == "github-user-token"
        raise RuntimeError("GitHub API 查重 failed (401): bad credentials")

    monkeypatch.setattr("src.domains.runtime.issues_center._find_open_issue_by_title",
                        _fake_find)
    e = _run_create_task(RK2)
    assert e.status_code == 401, e.detail
    assert e.detail.get("error_code") == "TOKEN_REJECTED_UPSTREAM"
    assert "GitHub" in e.detail.get("message", "")
    assert "action" not in e.detail, "GitHub 401 不得返回 GitCode OAuth reauth_required"


# ─────────────── 复审回归（QA 复审 dd11face7：Spec-1 / Spec-3 / R1） ───────────────

def test_resolve_rc_marked_expired_rejected_upstream(monkeypatch):
    """QA 复审 Spec-1：rc 本地已标记过期（bot_token_status='expired'）全链未命中
    → TOKEN_REJECTED_UPSTREAM（过期不得与未配置同码 TOKEN_MISSING）。"""
    _add_managed(RK, "i3427/sample")
    _upsert_rc(RK, _encrypt("expired-rc-token"))
    from src.domains.mgmt.public import set_repo_bot_token_status_any
    set_repo_bot_token_status_any(RK, "expired")
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert not res.ok and res.error_code == "TOKEN_REJECTED_UPSTREAM"
    assert "rc:marked_expired" in res.degraded


def test_resolve_global_marked_expired_rejected_upstream():
    """QA 复审 Spec-1：全局 bot 本地标记过期同口径 → TOKEN_REJECTED_UPSTREAM。"""
    _add_managed(RK, "i3427/sample")
    _set_global_bot("expired-global-token")
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('global_github_bot_token_status', 'expired')")
        db.commit()
    from src.domains.mgmt.user.db import _invalidate_global_bot_cache
    _invalidate_global_bot_cache()
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert not res.ok and res.error_code == "TOKEN_REJECTED_UPSTREAM"
    assert "global:marked_expired" in res.degraded


def test_resolve_global_platform_row_decrypt_failed(monkeypatch):
    """QA 复审 Spec-3：全局 bot 平台行密文损坏（_platform_bot_token 吞成空串）
    → 解析器区分 TOKEN_DECRYPT_FAILED，不再误报 TOKEN_MISSING。"""
    _add_managed(RK, "i3427/sample")
    monkeypatch.setattr(
        "src.domains.mgmt.user.db._platform_bot_row",
        lambda provider: {"token_enc": "k1:Zm9v:YmFkY2lwaGVydGV4dA==",
                          "username": "", "token_status": "unknown",
                          "checked_at": ""})
    from src.domains.mgmt.auth.credential_resolver import resolve_credential_detailed
    res = resolve_credential_detailed(RK)
    assert not res.ok and res.error_code == "TOKEN_DECRYPT_FAILED"
    assert "global:decrypt_failed" in res.degraded


def test_resolver_not_in_kernel_token_resolver():
    """R1 硬约束：解析器已迁出 kernel——kernel.token_resolver 不再暴露该符号。"""
    import src.kernel.token_resolver as _tr
    assert not hasattr(_tr, "resolve_credential_detailed")
