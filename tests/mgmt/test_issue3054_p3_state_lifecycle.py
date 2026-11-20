"""Issue #3054 P3：状态机与账号生命周期（7 子项）回归用例。

覆盖：
1. 解绑「至少保留一种登录方式」原子化——并发解绑至少一端失败；
2. associate 接入同口径限流——连续失败锁定后正确密码也不可绕过；
3. 注销级联失效身份行/清 token，错误码区分冷静期/禁用/已删除；
4. nonce 多槽——双标签页一成一可重试，mismatch 不清整条 cookie；
5. state 消费「先比 provider 再 DELETE」+ 与失败重试解耦；
6. OAuth 注册与密码注册共用注册策略；
7. /callback /associate /register 复查渠道 enabled。

不连真实 IdP、不起服务进程；DB 走 tests/conftest.py 的隔离测试库。
"""
import threading

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from src.domains.mgmt.auth import grape_account_db as gad
from src.domains.mgmt.auth import login_throttle
from src.domains.mgmt.user import db as user_db
from src.kernel.session_manager import db, db_lock


class _FakeRequest:
    """最小 Request 替身：cookies/headers/client。"""

    def __init__(self, cookies=None, headers=None, client_host="127.0.0.1"):
        self.cookies = cookies or {}
        self.headers = headers or {"host": "127.0.0.1:8000"}
        self.client = type("C", (), {"host": client_host})()


def _uname(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _ext() -> str:
    import uuid
    return f"ext_{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """清理本文件产生的限流/state/账号；渠道开关判定默认「启用」。"""
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM login_failures",
                    "DELETE FROM oauth_states",
                    "DELETE FROM external_identities WHERE external_id LIKE 'ext_%'",
                    "DELETE FROM grape_accounts WHERE username LIKE 'p3_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()

    from src.domains.mgmt.auth import auth_guards

    monkeypatch.setattr(auth_guards.ChannelGate, "is_disabled",
                        staticmethod(lambda provider: False))
    yield
    with db_lock:
        c = db.cursor()
        for sql in ("DELETE FROM login_failures",
                    "DELETE FROM external_identities WHERE external_id LIKE 'ext_%'",
                    "DELETE FROM grape_accounts WHERE username LIKE 'p3_%'"):
            try:
                c.execute(sql)
            except Exception:
                pass
        db.commit()


def _no_password_account(prefix: str):
    """无密码账号（纯 OAuth 账号形态）——解绑并发用例需要它。"""
    u = gad.create_grape_account(_uname(prefix), "", "temp-pass")
    assert u, "account create failed"
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_accounts SET password_hash='' WHERE id=?", (u["id"],))
        db.commit()
    return u


def _save_state(provider: str = "gitcode", data: dict | None = None) -> str:
    """落一个 state；默认带 expires（关联/注册路由按 state 内 expires 判过期）。"""
    import hashlib
    import os
    import time as _t
    state = hashlib.sha256(os.urandom(32)).hexdigest()
    payload = dict(data or {})
    payload.setdefault("expires", _t.time() + 600)
    user_db.save_oauth_state(state, provider, payload)
    return state


# ───────────────────────────────────────────── P3-1 解绑原子性

def test_p3_unbind_concurrent_at_least_one_fails():
    u = _no_password_account("p3_conc")
    e1, e2 = _ext(), _ext()
    assert gad.create_external_identity(u["id"], "gitcode", e1, "gc-user", "gc")
    assert gad.create_external_identity(u["id"], "github", e2, "gh-user", "gh")
    assert gad.count_user_identities(u["id"]) == 2

    results = {}
    barrier = threading.Barrier(2)

    def _worker(provider):
        barrier.wait()
        results[provider] = gad.delete_external_identity(u["id"], provider)

    threads = [threading.Thread(target=_worker, args=(p,)) for p in ("gitcode", "github")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(bool(v) for v in results.values()) == [False, True], results
    # 账号仍保留一种可用登录方式
    assert gad.count_user_identities(u["id"]) == 1


def test_p3_unbind_keep_one_is_one_conditional_statement(monkeypatch):
    """回归锁：剩余登录方式判据必须在**同一条 UPDATE** 内求值（读-判-写会并发双过）。"""
    u = _no_password_account("p3_sql")
    assert gad.create_external_identity(u["id"], "gitcode", _ext(), "gc", "gc")
    assert gad.create_external_identity(u["id"], "github", _ext(), "gh", "gh")

    captured = []
    real_cursor = gad._mg_db.cursor

    class _Cur:
        def __init__(self, cur):
            self._c = cur

        def __getattr__(self, item):
            return getattr(self._c, item)

        def execute(self, sql, params=None):
            if str(sql).lstrip().upper().startswith("UPDATE EXTERNAL_IDENTITIES"):
                captured.append((sql, params))
            return self._c.execute(sql, params) if params is not None else self._c.execute(sql)

    monkeypatch.setattr(gad._mg_db, "cursor", lambda *a, **k: _Cur(real_cursor(*a, **k)))
    assert gad.delete_external_identity(u["id"], "gitcode") is True

    assert captured, "解绑未走单条条件 UPDATE（疑似回退成读-判-写）"
    sql, params = captured[0]
    low = " ".join(str(sql).lower().split())
    assert "update external_identities" in low
    assert "status=1" in low, sql            # 目标行限定活跃态
    assert "password_hash" in low, sql       # 密码判据在同一语句内
    assert "status in (1,4)" in low, sql     # 剩余绑定计数判据在同一语句内
    assert u["id"] in [str(p) for p in params]


def test_p3_unbind_with_password_can_drop_last_identity():
    u = gad.create_grape_account(_uname("p3_pw"), "p3_pw@example.com", "Str0ng!pass1")
    e = _ext()
    assert gad.create_external_identity(u["id"], "gitcode", e, "gc-user", "gc")
    assert gad.delete_external_identity(u["id"], "gitcode") is True
    assert gad.count_user_identities(u["id"]) == 0
    assert gad.has_password(u["id"]) is True


# ───────────────────────────────────────────── P3-2 associate 限流

def test_p3_associate_throttle_cannot_be_bypassed(monkeypatch):
    import src.domains.mgmt.routes.grape_auth as ga

    username = _uname("p3_lock")
    u = gad.create_grape_account(username, "p3_lock@example.com", "Str0ng!pass1")
    assert u
    state = _save_state("gitcode", {
        "external_id": _ext(), "external_login": "gc-lock",
        "external_name": "lock", "external_avatar": "",
    })
    body = ga.OAuthAssociateBody(state=state, identifier=username, password="wrong-pass")
    req = _FakeRequest()

    for _ in range(login_throttle.ACCOUNT_LOCK_THRESHOLD):
        with pytest.raises(HTTPException) as ei:
            ga.oauth_associate("gitcode", body, req, JSONResponse({}))
        assert ei.value.status_code == 401
    # 失败计入与 /login 同一维度
    assert login_throttle.get_failure_count(username) == login_throttle.ACCOUNT_LOCK_THRESHOLD

    # 达到阈值后（锁定态），正确密码走 associate 也必须被拦住——原实现可绕过
    from src.domains.mgmt.auth import login_throttle as _lt
    monkeypatch.setattr(_lt, "check_throttle", lambda ident, ip: {
        "allowed": False, "locked": True, "error_code": "ACCOUNT_LOCKED",
        "message": "出于安全保护，账号已临时锁定。", "retry_after": 600,
    })
    good = ga.OAuthAssociateBody(state=state, identifier=username, password="Str0ng!pass1")
    with pytest.raises(HTTPException) as ei:
        ga.oauth_associate("gitcode", good, req, JSONResponse({}))
    assert ei.value.status_code == 429
    assert ei.value.detail.get("error_code") == "ACCOUNT_LOCKED"


# ───────────────────────────────────────────── P3-3 注销级联 + 错误码

def test_p3_soft_delete_cascades_identity_and_tokens():
    u = gad.create_grape_account(_uname("p3_del"), "p3_del@example.com", "Str0ng!pass1")
    e = _ext()
    assert gad.create_external_identity(u["id"], "gitcode", e, "gc-del", "del",
                                        access_token="at-secret", refresh_token="rt-secret")
    assert gad.get_external_identity("gitcode", e) is not None  # 活跃可登录

    assert gad.soft_delete_account(u["id"]) is True

    # Issue #3054：注销必须**释放** UNIQUE(provider, external_id)（旧值改 unbound:<行 id>
    # 并写墓碑）——否则该第三方账号在 30 天冷静期内对所有人不可绑。因此按用户查行。
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, external_id, access_token, refresh_token "
                  "FROM external_identities WHERE grape_user_id=?", (u["id"],))
        row = c.fetchone()
    assert row is not None and int(row[0]) == 0                       # 级联失效
    assert str(row[1]).startswith("unbound:"), "external_id 未释放（唯一键仍被占）"
    assert not (row[2] or ""), "access_token 未清空"
    assert not (row[3] or ""), "refresh_token 未清空"
    assert gad.get_external_identity("gitcode", e) is None   # 不再可登录
    # 墓碑可反查原归属（冷静期内恢复登录走这条）
    tomb = gad.find_identity_by_tombstone("gitcode", e)
    assert tomb is not None and str(tomb["grape_user_id"]) == str(u["id"])


def test_p3_account_status_error_codes_are_distinct():
    from src.domains.mgmt.auth.auth_guards import AccountDeactivationCascade as ADC

    assert ADC.oauth_error_code(3) == "account_deactivating"
    assert ADC.oauth_error_code(0) == "account_disabled"
    assert ADC.oauth_error_code(2) == "account_deleted"
    assert ADC.http_error(3).detail["error_code"] == "ACCOUNT_DEACTIVATING"
    assert ADC.http_error(0).detail["error_code"] == "ACCOUNT_DEACTIVATED"
    assert ADC.http_error(2).detail["error_code"] == "ACCOUNT_DELETED"
    assert ADC.http_error(1).status_code == 403


def test_p3_oauth_login_bound_branch_reports_cooldown():
    """已绑定但账号处于注销冷静期 → account_deactivating（原统一报 account_disabled）。"""
    import src.domains.mgmt.routes.grape_auth as ga

    u = gad.create_grape_account(_uname("p3_cool"), "p3_cool@example.com", "Str0ng!pass1")
    e = _ext()
    assert gad.create_external_identity(u["id"], "gitcode", e, "gc-cool", "cool")
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_accounts SET status=3 WHERE id=?", (u["id"],))
        db.commit()

    resp = ga._handle_oauth_login(
        _FakeRequest(), "gitcode", e,
        {"external_id": e, "external_login": "gc-cool", "external_name": "cool"},
        "at", "rt", popup=False, state_data={"lang": "zh"}, expires_in=0)
    assert isinstance(resp, RedirectResponse)
    assert "account_deactivating" in resp.headers.get("location", "")


# ───────────────────────────────────────────── P3-4 nonce 多槽

def _set_cookie_values(resp):
    return [v.decode() for k, v in resp.raw_headers if k.decode().lower() == "set-cookie"]


def test_p3_double_tab_nonce_multislot_both_work():
    import src.domains.mgmt.routes.grape_auth as ga

    # 标签页 1 发起登录 → cookie 只有 n1
    from src.domains.mgmt.auth.auth_guards import OAuthStateStore

    r1 = JSONResponse({})
    OAuthStateStore.add(r1, _FakeRequest(cookies={}), "n1")
    v1 = [c for c in _set_cookie_values(r1) if c.startswith("oauth_nonce=")][0]
    assert v1.split(";")[0] == "oauth_nonce=n1"

    # 标签页 2 发起登录（带上 1 的 cookie）→ 追加 n2，不覆盖
    r2 = JSONResponse({})
    OAuthStateStore.add(r2, _FakeRequest(cookies={"oauth_nonce": "n1"}), "n2")
    v2 = [c for c in _set_cookie_values(r2) if c.startswith("oauth_nonce=")][0]
    assert v2.split(";")[0] == "oauth_nonce=n1.n2"

    state1 = _save_state("gitcode", {"login_flow": True, "session_nonce": "n1"})
    state2 = _save_state("gitcode", {"login_flow": True, "session_nonce": "n2"})
    req = _FakeRequest(cookies={"oauth_nonce": "n1.n2"})

    sd2, _popup, is_login2, err2 = ga._validate_channel_callback(req, state2, "gitcode")
    assert err2 is None and sd2 is not None and is_login2 is True
    sd1, _popup, is_login1, err1 = ga._validate_channel_callback(req, state1, "gitcode")
    assert err1 is None and sd1 is not None and is_login1 is True

    # 消费一侧只摘掉自己的槽
    consumed = RedirectResponse("/")
    OAuthStateStore.consume(consumed, req, "n1")
    vals = [c for c in _set_cookie_values(consumed) if c.startswith("oauth_nonce=")]
    assert vals and vals[0].split(";")[0] == "oauth_nonce=n2"


def test_p3_nonce_mismatch_does_not_wipe_other_tabs():
    import src.domains.mgmt.routes.grape_auth as ga

    state = _save_state("gitcode", {"login_flow": True, "session_nonce": "n9"})
    req = _FakeRequest(cookies={"oauth_nonce": "n1.n2"})
    sd, _popup, _login, err = ga._validate_channel_callback(req, state, "gitcode")
    assert sd is None and err is not None
    assert "state_mismatch" in err.headers.get("location", "")
    # 不得清整条 cookie（否则另一标签页正在进行的登录被打挂）
    setc = _set_cookie_values(err)
    assert not any(c.startswith("oauth_nonce=") for c in setc), setc


# ───────────────────────────────────────────── P3-5 state 消费顺序/解耦

def test_p3_state_provider_mismatch_does_not_consume():
    state = _save_state("gitcode", {"login_flow": True})
    assert user_db.validate_oauth_state(state, "github") is None      # 平台不符 → 拒绝
    assert user_db.validate_oauth_state(state, "gitcode") is not None  # 但未被烧掉


def test_p3_peek_does_not_consume():
    state = _save_state("gitcode", {"external_id": "x"})
    assert user_db.validate_oauth_state(state, "gitcode", consume=False) is not None
    assert user_db.validate_oauth_state(state, "gitcode", consume=False) is not None
    assert user_db.validate_oauth_state(state, "gitcode") is not None
    assert user_db.validate_oauth_state(state, "gitcode") is None      # 真正的消费只有一次


def test_p3_associate_wrong_password_keeps_session_retryable():
    import src.domains.mgmt.routes.grape_auth as ga

    username = _uname("p3_retry")
    assert gad.create_grape_account(username, "p3_retry@example.com", "Str0ng!pass1")
    ext = _ext()
    state = _save_state("gitcode", {
        "external_id": ext, "external_login": "gc-retry",
        "external_name": "retry", "external_avatar": "",
        "access_token": "at",
    })

    bad = ga.OAuthAssociateBody(state=state, identifier=username, password="nope")
    with pytest.raises(HTTPException) as ei:
        ga.oauth_associate("gitcode", bad, _FakeRequest(), JSONResponse({}))
    assert ei.value.status_code == 401

    # 原会话可原地重试（state 没被烧掉）
    assert user_db.validate_oauth_state(state, "gitcode", consume=False) is not None
    good = ga.OAuthAssociateBody(state=state, identifier=username, password="Str0ng!pass1")
    resp = ga.oauth_associate("gitcode", good, _FakeRequest(), JSONResponse({}))
    assert resp.get("token")
    assert gad.get_external_identity("gitcode", ext) is not None


# ───────────────────────────────────────────── P3-6 OAuth 注册共用策略

def _register_body(state, username, email=""):
    import src.domains.mgmt.routes.grape_auth as ga
    return ga.OAuthRegisterBody(state=state, username=username, email=email)


def test_p3_oauth_register_obeys_signup_policy(monkeypatch):
    import src.domains.mgmt.routes.grape_auth as ga

    monkeypatch.setattr(ga, "_check_whitelist", lambda provider, login: True)
    monkeypatch.setattr(ga.signup_policy, "check_signup_allowed", lambda email: False)

    state = _save_state("gitcode", {"external_id": _ext(), "external_login": "gc-reg",
                                    "email": "p3_blocked@example.com"})
    body = _register_body(state, _uname("p3_reg"))
    with pytest.raises(HTTPException) as ei:
        ga.oauth_register("gitcode", body, _FakeRequest(), JSONResponse({}))
    assert ei.value.status_code == 403
    assert ei.value.detail["error_code"] == "EMAIL_NOT_ALLOWED"


def test_p3_oauth_register_rejects_registered_email(monkeypatch):
    import src.domains.mgmt.routes.grape_auth as ga

    taken = "p3_taken@example.com"
    assert gad.create_grape_account(_uname("p3_taken"), taken, "Str0ng!pass1")
    monkeypatch.setattr(ga, "_check_whitelist", lambda provider, login: True)
    monkeypatch.setattr(ga.signup_policy, "check_signup_allowed", lambda email: True)

    state = _save_state("gitcode", {"external_id": _ext(), "external_login": "gc-reg2",
                                    "email": taken})
    body = _register_body(state, _uname("p3_reg"), email=taken)
    with pytest.raises(HTTPException) as ei:
        ga.oauth_register("gitcode", body, _FakeRequest(), JSONResponse({}))
    assert ei.value.status_code == 409
    assert ei.value.detail["error_code"] == "EMAIL_ALREADY_REGISTERED"


# ───────────────────────────────────────────── P3-7 渠道开关复查

def test_p3_disabled_channel_rejected_on_callback_associate_register(monkeypatch):
    import src.domains.mgmt.routes.grape_auth as ga

    from src.domains.mgmt.auth.auth_guards import ChannelGate
    monkeypatch.setattr(ChannelGate, "is_disabled", staticmethod(lambda provider: True))
    monkeypatch.setattr(ga, "_check_whitelist", lambda provider, login: True)

    # /callback：state 有效也不得完成登录
    state = _save_state("gitcode", {"login_flow": True, "session_nonce": "n1"})
    req = _FakeRequest(cookies={"oauth_nonce": "n1"})
    resp = ga.oauth_callback(req, "gitcode", code="c", state=state, error="")
    assert isinstance(resp, RedirectResponse)
    assert "channel_disabled" in resp.headers.get("location", "")

    # /associate
    state2 = _save_state("gitcode", {"external_id": _ext()})
    body = ga.OAuthAssociateBody(state=state2, identifier="x", password="y")
    with pytest.raises(HTTPException) as ei:
        ga.oauth_associate("gitcode", body, _FakeRequest(), JSONResponse({}))
    assert ei.value.status_code == 403
    assert ei.value.detail["error_code"] == "CHANNEL_DISABLED"

    # /register
    state3 = _save_state("gitcode", {"external_id": _ext(), "external_login": "gc"})
    with pytest.raises(HTTPException) as ei:
        ga.oauth_register("gitcode", _register_body(state3, _uname("p3_reg")),
                          _FakeRequest(), JSONResponse({}))
    assert ei.value.status_code == 403
    assert ei.value.detail["error_code"] == "CHANNEL_DISABLED"