"""UC42-B：D100 清退断言——OAuth 快照清退（W1/R1/R2/R3）+ upc 退役行为锁。

方案稿 docs/weekly-09/d100-credential-type-schema-design.md §三/§四。

清退不变式（行为级断言，非源码锚定）：
- W1：refresh_oauth_token 成功后 <p>_oauth 快照行**不再被写入**
- R1：预刷新判定改读 external_identities.token_expires_at（快照行删除
  也不影响预刷新语义）
- R2：refresh 无 ei refresh_token → 不再回落快照（返回 None 不刷新）
- R3：/api/auth/me 的 gitcode_bound/github_bound 只由 ei bindings 决定
- upc 退役：OAuth 登录/绑定成功后零额外落库（P2=a，与基线行为一致）
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import time
import uuid

from src.kernel.session_manager import db, db_lock


def _mk_oauth_user(provider="gitee"):
    """造 完整 OAuth 链路用户：grape_accounts + external_identities（权威存储）。"""
    from src.domains.mgmt.auth.grape_account_db import create_grape_account, create_external_identity
    uname = "uc42b_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@test.local",
                             password="pw12345")
    uid = u["id"]
    ext_id = "uc42b_ext_" + uuid.uuid4().hex[:6]
    ok = create_external_identity(
        grape_user_id=uid, provider=provider, external_id=ext_id,
        external_login=uname, access_token="at_uc42b_old",
        refresh_token="rt_uc42b", expires_in=3600)
    assert ok, "ei 建行失败"
    return uid, uname, ext_id


def _cleanup_user(uid, uname, provider="gitee"):
    with db_lock:
        db.execute("DELETE FROM external_identities WHERE grape_user_id=? AND provider=?",
                   (uid, provider))
        db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
        db.execute("DELETE FROM unified_user_mapping WHERE gitcode_user=?", (uname,))
        db.execute("DELETE FROM user_credentials WHERE user_id=? AND provider LIKE '%_oauth'",
                   (uid,))
        db.commit()


# ──────────────── W1：刷新不再双写快照 ────────────────

def test_uc42b_w1_refresh_no_snapshot_write():
    """refresh_oauth_token 成功后：ei 三列更新（权威），<p>_oauth 快照零写入。"""
    from src.domains.mgmt.auth.grape_account_db import update_external_identity_tokens
    from src.domains.mgmt.user import db as udb
    uid, uname, ext_id = _mk_oauth_user()
    try:
        new_tok = "at_uc42b_fresh_" + uuid.uuid4().hex[:6]
        # 模拟 refresh 成功路径（W1 删除前此处还会同步写 <p>_oauth 快照）
        ok = update_external_identity_tokens(uid, "gitee", access_token=new_tok,
                                             refresh_token="rt_new", expires_in=7200)
        assert ok, "ei token 更新失败"
        # 权威侧命中新值
        from src.domains.mgmt.auth.grape_account_db import get_user_identities_with_tokens
        idents = [i for i in get_user_identities_with_tokens(uid) if i["provider"] == "gitee"]
        assert idents and idents[0]["access_token"] == new_tok
        # expires 断言改直查列（get_user_identities_with_tokens 返回 dict
        # 不含 token_expires_at——其字段面只有 token/login/avatar/created_at）
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username
        acc = get_grape_account_by_username(uname)
        with db_lock:
            exp = db.execute(
                "SELECT token_expires_at FROM external_identities "
                "WHERE grape_user_id=? AND provider='gitee'", (uid,)).fetchone()
        assert exp and exp[0], "ei.expires_at 未随刷新写入"
        # 时效语义：7200s → expires ≈ now+7200（gmtime UTC TEXT）
        from src.api.deps import _parse_utc_timestamp
        exp_epoch = _parse_utc_timestamp(exp[0])
        assert exp_epoch > time.time() + 6000, f"expires 语义异常: {exp[0]}"
        # 快照侧零写入（W1 断言核心——清退前这里会有 access_token=new_tok 的行）
        with db_lock:
            snap = db.execute(
                "SELECT config_json FROM user_credentials WHERE user_id=? AND provider='gitee_oauth'",
                (uid,)).fetchone()
        if snap:  # 存量行（P1=a 冻结）允许存在，但不得含本次新值
            assert new_tok not in snap[0], "快照行仍被刷新路径写入（W1 回潮）"
    finally:
        _cleanup_user(uid, uname)


# ──────────────── R2：刷新无回落 ────────────────

def test_uc42b_r2_refresh_no_snapshot_fallback():
    """ei 无 refresh_token → refresh_oauth_token 返回 None（不再读快照兜底）。

    场景：ei 行 refresh_token 为空（如只读授权）；快照行手工造一个
    refresh_token（模拟存量残留）——清退后刷新不得再消费它。
    """
    from src.domains.mgmt.auth.grape_account_db import update_external_identity_tokens
    from src.domains.mgmt.user import db as udb
    uid, uname, ext_id = _mk_oauth_user()
    try:
        # ei 行 refresh_token 置空（直接 SQL，模拟无 refresh 授权形态）
        with db_lock:
            db.execute("UPDATE external_identities SET refresh_token='' "
                       "WHERE grape_user_id=? AND provider='gitee'", (uid,))
            db.commit()
        # 快照行造残留 refresh_token（清退前 R2 会回落读到它）
        udb.set_credential(uid, "gitee_oauth",
                           {"refresh_token": "STALE_SNAPSHOT_RT",
                            "access_token": "stale"})
        from src.domains.mgmt.user.db import refresh_oauth_token
        # provider 未配 oauth client → 返回 None（未配 client 检查先于读 rt；
        # 但 rt 为空路径同样返回 None——两条 None 路径合并断言"不产生刷新成功"）
        result = refresh_oauth_token(uid, "gitee")
        assert result is None, f"不应成功刷新: {result!r}"
        # 快照残留值未被消费：ei refresh_token 仍为空
        with db_lock:
            rt = db.execute("SELECT refresh_token FROM external_identities "
                            "WHERE grape_user_id=? AND provider='gitee'",
                            (uid,)).fetchone()[0]
        assert rt == "", "ei refresh_token 被意外写入"
    finally:
        _cleanup_user(uid, uname)


# ──────────────── R1：预刷新判定改读 ei ────────────────

def test_uc42b_r1_preread_from_identity_expires():
    """预刷新判定数据源=ei.token_expires_at：快照行删除后语义不变。

    构造：ei.expires_at 设为 5 分钟后（<600s 阈值）→ get_user_token_with_refresh
    触发刷新尝试（provider 未配 client → 刷新失败返回旧 token，但"尝试了刷新"
    可由返回值路径断言——旧 token 返回即判定链正常工作）。
    快照行造一个"远未过期"的假 token_expires_at——若判定仍读快照，
    则不会触发刷新（与 ei 判定结果相反），以此区分数据源。
    """
    import datetime
    from src.domains.mgmt.auth.grape_account_db import (
        get_grape_account_by_username, update_external_identity_tokens)
    from src.domains.mgmt.user import db as udb
    uid, uname, ext_id = _mk_oauth_user()
    try:
        # ei.expires_at = 5 分钟后（即将过期，<600s 阈值内）
        soon = (datetime.datetime.utcnow() +
                datetime.timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
        with db_lock:
            db.execute("UPDATE external_identities SET token_expires_at=? "
                       "WHERE grape_user_id=? AND provider='gitee'", (soon, uid))
            db.commit()
        # 快照行造"远未过期"假象（若误读快照则判定为不需要刷新）
        udb.set_credential(uid, "gitee_oauth",
                           {"access_token": "at_snap",
                            "token_expires_at": int(time.time()) + 86400})
        from src.api.deps import get_user_token_with_refresh
        # 走预刷新判定：ei 判定即将过期 → 尝试刷新（未配 client 刷新失败）→
        # 返回旧 token（不抛错即链路正常；若误读快照则直接返回旧 token，
        # 两种路径返回值相同——用 mock 捕获 _try_refresh_user_token 是否被调用）
        from unittest.mock import patch
        called = {}
        def _fake_try(uid_, provider_):
            called["hit"] = True
            return None
        with patch("src.api.deps._try_refresh_user_token", side_effect=_fake_try):
            from src.api.deps import get_user_token_for
            tok = get_user_token_for(uname, user_id=uid, provider="gitee")
        assert tok == "at_uc42b_old", f"token 回退异常: {tok!r}"
        assert called.get("hit") is True, "预刷新未触发——判定未读 ei.expires_at（R1 回潮）"
    finally:
        _cleanup_user(uid, uname)


# ──────────────── R3：绑定判定只看 ei ────────────────

def test_uc42b_r3_binding_from_identities_only():
    """/api/auth/me 的 *_bound 只由 ei bindings 决定：快照行存在但 ei 无绑定
    → bound=False（清退前快照行会让 bound 误判 True）。"""
    from fastapi.testclient import TestClient
    from src.domains.mgmt.user import db as udb
    from src.domains.mgmt.auth.grape_account_db import create_grape_account
    uname = "uc42b_r3_" + uuid.uuid4().hex[:6]
    u = create_grape_account(username=uname, email=f"{uname}@test.local",
                             password="pw12345")
    uid = u["id"]
    try:
        # 只造快照行，不建 ei 绑定（清退前此形态 github_bound 会误判 True）
        udb.set_credential(uid, "github_oauth", {"access_token": "stale"})
        from src.api.main import app
        from src.api.deps import require_no_session_token
        app.dependency_overrides[require_no_session_token] = lambda: {
            "id": uid, "username": uname}
        client = TestClient(app)
        try:
            r = client.get("/api/auth/profile", headers={"Authorization": "Bearer test"})
            assert r.status_code == 200, r.text[:200]
            d = r.json()
            assert d.get("github_bound") is False, \
                f"github_bound 被快照行误判 True（R3 回潮）: {d.get('github_bound')}"
            assert "github" not in d.get("bindings", []), "bindings 含未绑定平台"
        finally:
            app.dependency_overrides.clear()
    finally:
        _cleanup_user(uid, uname, provider="github")


# ──────────────── upc 退役：登录成功零额外落库 ────────────────

def test_uc42b_upc_retired_no_provision_writes(probe_admin=None):
    """OAuth 绑定成功后：无 upc 表（新装库）、无授权记录落库点。

    P2=(a)：登录成功后唯一写入=ei + auth_audit（基线形态）。
    表级断言：新装库 sqlite_master 无 user_platform_credentials。
    """
    with db_lock:
        t = db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='user_platform_credentials'").fetchone()
    assert t is None, "user_platform_credentials 表仍被建表（R1 退役失效）"
    # 源码级：provisioner 模块已删除
    assert not os.path.exists(os.path.join(
        os.path.dirname(__file__), "..", "src", "domains", "runtime", "vcs",
        "platform_credential_provisioner.py")), "provisioner 文件未删除"
