"""Issue #3469 复审修复的定点用例（kid 列宽 / 二次因子顺序 / D 补齐 / F 降级）。

复审提出的四处问题，每处一条用例；全部走真实实现，只有外部通知与 runtime 契约用
假端口/替身（领域服务本来就只依赖端口协议）。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.auth.email_change import (
    EmailChangeCommand,
    EmailChangeError,
    EmailChangeRequest,
    EmailChangeService,
    EmailFactor,
)

USER_ID = "u-review"


# ── 1) I：kid 非空时哈希长度超出 VARCHAR(64) ─────────────────────────
def test_stored_hash_with_kid_exceeds_legacy_width(monkeypatch):
    """kid 启用后 hash 形如 'k1:<64hex>' = 67 字符，旧列宽 64 会截断/1406。"""
    from src.kernel.auth_sdk import jwt_utils

    monkeypatch.setattr(jwt_utils, "_SIGNING_SECRET", "s", raising=False)
    monkeypatch.setattr(jwt_utils, "_SIGNING_KID", "k1", raising=False)

    from src.domains.mgmt.auth.verification_code import VerificationCode

    stored = VerificationCode("123456", "a@b.com").stored_hash
    assert stored.startswith("k1:")
    assert len(stored) == 67
    # 落库列宽必须装得下（schema_ddl 口径）
    from src.domains.mgmt.schema_ddl import _VERIFICATION_CODES_DDL

    assert "VARCHAR(191)" in _VERIFICATION_CODES_DDL
    assert "code_hash" in _VERIFICATION_CODES_DDL


def test_kid_hash_roundtrip_verifies(monkeypatch):
    """带 kid 的 hash 能按 kid 取密钥验过（I 的正向闭环）。"""
    from src.kernel.auth_sdk import jwt_utils

    monkeypatch.setattr(jwt_utils, "_SIGNING_SECRET", "s", raising=False)
    monkeypatch.setattr(jwt_utils, "_SIGNING_KID", "k1", raising=False)
    monkeypatch.setattr(jwt_utils, "_SECRET_KEYS", {"k1": "s"}, raising=False)

    from src.domains.mgmt.auth.verification_code import VerificationCode

    vc = VerificationCode("123456", "a@b.com")
    assert vc.matches(vc.stored_hash, "123456", "a@b.com") is True
    assert vc.matches(vc.stored_hash, "000000", "a@b.com") is False


def test_hash_width_migration_sqlite_noop(monkeypatch):
    """SQLite 无长度语义：迁移返回 after=191 且不动表（幂等）。"""
    import sqlite3

    from src.domains.mgmt.migrations.verification_hash_width import (
        VerificationHashWidthMigration,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE verification_codes (id INTEGER PRIMARY KEY, code_hash VARCHAR(64) NOT NULL)")
    monkeypatch.setattr("src.api.db_setup._mysql_backend", lambda: False)
    stats = VerificationHashWidthMigration()(conn=conn)
    assert stats["after"] == 191 and stats["altered"] is False
    # 二次执行仍幂等
    stats2 = VerificationHashWidthMigration()(conn=conn)
    assert stats2["after"] == 191 and stats2["altered"] is False


# ── 2) 二次因子必须先于验证码消费 ────────────────────────────────────
class _Accounts:
    def __init__(self, account):
        self.account = dict(account)
        self.updates = []

    def get(self, user_id):
        return dict(self.account)

    def update_email(self, user_id, new_email):
        self.updates.append(new_email)
        self.account["email"] = new_email
        return True

    def is_email_registered(self, email):
        return False


class _Verification:
    """模拟真实存储语义：consume=False 只判不烧，consume=True 才把码标记已用。"""

    def __init__(self):
        self.valid_codes = {"123456"}
        self.used = set()
        self.confirm_codes = {"654321"}
        self.confirm_used = set()
        self.count = 0
        self.rl_calls = []
        self.rl_verdict = {"ok": True, "message": "OK"}

    def _check(self, codes, used, code):
        if code in used:
            return {"ok": False, "error_code": "CODE_USED", "message": "验证码无效或已过期"}
        if code not in codes:
            return {"ok": False, "error_code": "CODE_INVALID", "message": "验证码无效或已过期"}
        return {"ok": True}

    def verify_code(self, key, scene, code, target="", consume=True):
        if scene == "change_email_confirm":
            r = self._check(self.confirm_codes, self.confirm_used, code)
            if r["ok"] and consume:
                self.confirm_used.add(code)
            return r
        r = self._check(self.valid_codes, self.used, code)
        if r["ok"] and consume:
            self.used.add(code)
        return r

    def send_code(self, email, scene, ip="", rate_limit_key="", target=""):
        return {"ok": True, "message": "验证码已发送", "require_factor": "password"}

    def count_sends(self, key, scene, window_seconds=600):
        return self.count

    def check_rate_limit(self, rl_key, scene, ip=""):
        self.rl_calls.append((rl_key, scene, ip))
        return self.rl_verdict


class _Notifier:
    def __init__(self):
        self.emails = []
        self.inapp = []

    def send(self, to, subject, body):
        self.emails.append((to, subject))
        return True

    def notify_user(self, user_id, subject, body):
        self.inapp.append((user_id, subject))
        return True


def test_wrong_second_factor_does_not_burn_code(monkeypatch):
    """复审实测的回归：密码错一次之后，同一个新邮箱码必须仍可用。"""
    account = {"id": USER_ID, "email": "old@example.com", "password_hash": "hash"}
    accounts, verification, notifier = _Accounts(account), _Verification(), _Notifier()
    svc = EmailChangeService(accounts=accounts, verification=verification, notifier=notifier)

    monkeypatch.setattr("src.domains.mgmt.auth.password.verify_password",
                        lambda raw, hashed: raw == "correct", raising=False)

    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id=USER_ID, new_email="new@example.com",
                                      code="123456", password="wrong"))
    assert ei.value.error_code == "PASSWORD_INVALID"
    assert "123456" not in verification.used, "密码失败不得消耗新邮箱验证码"

    # 同一个码 + 正确密码 → 必须成功
    result = svc.commit(EmailChangeCommand(user_id=USER_ID, new_email="new@example.com",
                                           code="123456", password="correct"))
    assert result.require_factor is EmailFactor.PASSWORD
    assert accounts.updates == ["new@example.com"]
    assert "123456" in verification.used


# ── 3) D：注销发码也要按可投递判定 ──────────────────────────────────
def test_delete_account_send_code_rejects_undeliverable(client):
    from unittest.mock import patch

    account = {"id": "u1", "email": "1+noreply@noreply.gitcode.com", "password_hash": "h"}
    with patch("src.domains.mgmt.routes.grape_auth.get_grape_account_by_id", return_value=account), \
         patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=True):
        r = client.post("/api/auth/delete-account/send-code")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "EMAIL_UNBOUND"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.api.deps import require_jwt_only

    app.dependency_overrides[require_jwt_only] = lambda: {"id": "u1", "username": "t", "token_type": "jwt"}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── 4) F：旧邮箱不可投递时改走站内信/IM ─────────────────────────────
def test_undeliverable_old_email_falls_back_to_inapp(monkeypatch):
    account = {"id": USER_ID, "email": "1+noreply@noreply.gitcode.com", "password_hash": None}
    accounts, verification, notifier = _Accounts(account), _Verification(), _Notifier()
    svc = EmailChangeService(accounts=accounts, verification=verification, notifier=notifier)

    svc.commit(EmailChangeCommand(user_id=USER_ID, new_email="real@example.com", code="123456"))
    # 占位/私有中转邮箱不发邮件，但必须落一条站内信/IM
    assert (USER_ID, "邮箱已变更") in notifier.inapp
    assert all(to != account["email"] for to, _ in notifier.emails)


def test_failed_change_with_deliverable_old_email_notifies_failure():
    account = {"id": USER_ID, "email": "old@example.com", "password_hash": None}
    accounts = _Accounts(account)
    accounts.update_email = lambda user_id, new_email: "conflict"
    notifier = _Notifier()
    svc = EmailChangeService(accounts=accounts, verification=_Verification(), notifier=notifier)

    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id=USER_ID, new_email="new@example.com",
                                      code="123456", old_email_code="654321"))
    assert ei.value.error_code == "EMAIL_TAKEN"
    assert ("old@example.com", "邮箱变更失败") in notifier.emails
    assert all(subject != "邮箱已变更" for _, subject in notifier.emails)

def test_target_address_rate_limited():
    """E 复审补齐：新地址维度独立限流；60s 间隔命中回 RATE_LIMITED + retry_after。"""
    account = {"id": USER_ID, "email": "old@example.com", "password_hash": "hash"}
    verification = _Verification()
    verification.rl_verdict = {"ok": False, "error_code": "RATE_LIMITED", "retry_after": 42,
                               "message": "请 42 秒后再试"}
    svc = EmailChangeService(accounts=_Accounts(account), verification=verification,
                             notifier=_Notifier())

    with pytest.raises(EmailChangeError) as ei:
        svc.request_code(EmailChangeRequest(user_id=USER_ID,
                                           new_email="brand-new@example.com", ip="1.2.3.4"))
    assert ei.value.status == 429 and ei.value.error_code == "RATE_LIMITED"
    assert ei.value.retry_after == 42
    # 限流检查打在目标地址维度上
    assert verification.rl_calls and verification.rl_calls[0][0] == "brand-new@example.com"
    assert "brand-new@example.com" not in verification.used


def test_register_send_code_uses_identity_email_format(client):
    """L 复审补齐：注册侧与改绑侧同一形态判据，a@b 不再放行。"""
    from unittest.mock import patch

    with patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=True), \
         patch("src.domains.mgmt.routes.grape_auth.is_username_taken", return_value=False):
        r = client.post("/api/auth/register/send-code",
                        json={"username": "okname", "email": "a@b", "password": "TestPass123!"})
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "EMAIL_INVALID"


def test_unlock_send_code_returns_hint(client):
    """N3：账号不存在/邮箱不可投递时回 200 + 通用提示，不再静默。"""
    from unittest.mock import patch

    with patch("src.domains.mgmt.routes.grape_auth.get_grape_account_by_identifier", return_value=None), \
         patch("src.domains.mgmt.routes.grape_auth.auth_verification.check_rate_limit",
               return_value={"ok": True, "message": "OK"}), \
         patch("src.domains.mgmt.auth.email_service.is_email_configured", return_value=True):
        r = client.post("/api/auth/unlock/send-code", json={"identifier": "ghost@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "验证码已发送"
    assert "绑定" in body.get("hint", "")


def test_profile_exposes_email_deliverable():
    """N4：/auth/profile 与 /auth/me 同源提供 email_deliverable（壳前端判据来源）。"""
    from unittest.mock import patch

    from src.domains.mgmt.routes.auth import get_profile

    user = {"id": 42, "username": "tester"}
    with patch("src.domains.mgmt.routes.auth.get_grape_account_by_username",
               return_value={"nickname": "n", "email": "1+u@noreply.gitcode.com", "email_verified": 0}), \
         patch("src.domains.mgmt.routes.auth.user_db.get_managed_repos", return_value=[]), \
         patch("src.domains.mgmt.auth.grape_account_db.get_user_identities", return_value=[]), \
         patch("src.domains.mgmt.auth.grape_account_db.has_password", return_value=False), \
         patch("src.domains.mgmt.routes.auth._links_payload", return_value={}):
        profile = get_profile(user=user)
    assert profile["email_deliverable"] is False


@pytest.fixture(autouse=True)
def noop_login_throttle(monkeypatch):
    """领域服务单测里不落 login_failures（专项节流用例单独覆盖）。"""
    from src.domains.mgmt.auth import login_throttle

    monkeypatch.setattr(login_throttle, "check_throttle", lambda *a, **k: {"allowed": True}, raising=False)
    monkeypatch.setattr(login_throttle, "record_failure", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(login_throttle, "clear_failures", lambda *a, **k: None, raising=False)


# ── 复审第二轮：不可投递目标 / 密码因子节流 / IP 超限码 / 通知不再谎报 ──

def test_undeliverable_target_rejected():
    """P2：把 noreply.* 绑成账号邮箱必须拒绝（否则又造出「有 email 收不到信」形态）。"""
    account = {"id": USER_ID, "email": "old@example.com", "password_hash": "hash"}
    svc = EmailChangeService(accounts=_Accounts(account), verification=_Verification(),
                             notifier=_Notifier())
    with pytest.raises(EmailChangeError) as ei:
        svc.request_code(EmailChangeRequest(user_id=USER_ID,
                                            new_email="1+u@noreply.github.com"))
    assert ei.value.status == 400 and ei.value.error_code == "EMAIL_UNDELIVERABLE"


def test_password_factor_uses_login_throttle(monkeypatch):
    """P2：密码因子失败要计数（本 PR 新开的密码判定面不能被无节流爆破）。"""
    account = {"id": USER_ID, "email": "old@example.com", "password_hash": "hash"}
    from src.domains.mgmt.auth import login_throttle

    calls = {"check": 0, "record": 0, "clear": 0}
    monkeypatch.setattr(login_throttle, "check_throttle",
                        lambda *a, **k: calls.__setitem__("check", calls["check"] + 1) or {"allowed": True})
    monkeypatch.setattr(login_throttle, "record_failure",
                        lambda *a, **k: calls.__setitem__("record", calls["record"] + 1))
    monkeypatch.setattr(login_throttle, "clear_failures",
                        lambda *a, **k: calls.__setitem__("clear", calls["clear"] + 1))
    monkeypatch.setattr("src.domains.mgmt.auth.password.verify_password",
                        lambda raw, hashed: raw == "right")

    svc = EmailChangeService(accounts=_Accounts(account), verification=_Verification(),
                             notifier=_Notifier())
    with pytest.raises(EmailChangeError):
        svc.commit(EmailChangeCommand(user_id=USER_ID, new_email="new@example.com",
                                      code="123456", password="wrong"))
    assert calls["record"] == 1 and calls["clear"] == 0

    svc.commit(EmailChangeCommand(user_id=USER_ID, new_email="new@example.com",
                                  code="123456", password="right"))
    assert calls["clear"] == 1


def test_ip_limit_reports_ip_limited(monkeypatch):
    """P2：IP 维度超限要有独立 error_code，不能冒充目标邮箱被限流。"""
    from src.domains.mgmt.auth import verification as v

    monkeypatch.setattr(v, "_count_recent_ip_requests", lambda ip, window=600: v.MAX_IP_PER_WINDOW)
    r = v.check_rate_limit("user:x", "change_email", "10.0.0.9")
    assert r["ok"] is False and r["error_code"] == "IP_LIMITED"


def test_ip_count_ignores_change_email_target_shadow(monkeypatch):
    """P2：change_email 的 target 影子行不再放大 IP 计数（一次改绑 = 一次计数）。"""
    from src.domains.mgmt.auth.verification_store import VerificationCodeStore
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE verification_codes (id INTEGER PRIMARY KEY, email TEXT, code_hash TEXT,"
                 " scene TEXT, ip TEXT, created_at TEXT)")
    now = "2099-01-01 00:00:00"
    rows = [("new@x", "", "change_email", "1.1.1.1"),      # target 影子行
            ("user:u", "k1:abc", "change_email", "1.1.1.1"),  # 真码
            ("e@x", "", "register", "1.1.1.1")]               # H 的影子路径
    for email, h, scene, ip in rows:
        conn.execute("INSERT INTO verification_codes (email, code_hash, scene, ip, created_at)"
                     " VALUES (?,?,?,?,?)", (email, h, scene, ip, now))
    import threading
    store = VerificationCodeStore(lambda: (conn, threading.RLock()))
    assert store.count_ip_since("1.1.1.1", "2000-01-01 00:00:00") == 2


def test_notification_adapter_does_not_coerce_id_and_reports_failure(monkeypatch):
    """P1：UUID 不 int() 强转；runtime 返回 False 时不得对外报成功。"""
    from src.domains.mgmt.auth.email_change import _EmailNotificationAdapter
    from src.domains.runtime import public as runtime_public

    seen = {}

    def _fake_fail(uid, kind, text):
        seen["uid"] = uid
        return False

    monkeypatch.setattr(runtime_public, "schedule_notify", _fake_fail, raising=False)
    assert _EmailNotificationAdapter().notify_user("2ec8bad0-uuid", "x", "y") is False
    assert seen["uid"] == "2ec8bad0-uuid"

    monkeypatch.setattr(runtime_public, "schedule_notify",
                        lambda uid, kind, text: True, raising=False)
    assert _EmailNotificationAdapter().notify_user("2ec8bad0-uuid", "x", "y") is True


def test_sync_enqueue_when_no_loop(monkeypatch):
    """P1：没有事件循环时 schedule_notify 同步入队，而不是静默 skip 报成功。"""
    from src.domains.runtime.im import notify as im_notify

    monkeypatch.setattr(im_notify, "active_targets",
                        lambda uid: [{"installation_id": "inst-1", "channel_type": "feishu"}], raising=False)
    monkeypatch.setattr(im_notify, "p2p_chat_id", lambda inst: "chat-1", raising=False)
    calls = []
    import src.domains.runtime.im.outbox as OB
    monkeypatch.setattr(OB, "enqueue",
                        lambda **kw: calls.append(kw) or True, raising=False)
    monkeypatch.setattr(OB, "new_expiry", lambda: "2099-01-01 00:00:00", raising=False)
    monkeypatch.setattr(OB, "out_track_id",
                        lambda *a: "track-1", raising=False)

    assert im_notify.schedule_notify("2ec8bad0-uuid", "email_change", "hi") is True
    assert calls and calls[0]["installation_id"] == "inst-1"

    # 没有任何安装 → 入队失败必须如实返回 False
    monkeypatch.setattr(im_notify, "active_targets", lambda uid: [], raising=False)
    assert im_notify.schedule_notify("2ec8bad0-uuid", "email_change", "hi") is False


def test_send_interval_not_reported_as_send_failure():
    """复审小瑕疵：60s 间隔被拒不得冒充"验证码发送失败"（CODE_SEND_FAILED）。

    真源在 verification.check_rate_limit：它必须给出结构化 RATE_LIMITED + retry_after，
    否则 email_change 的 `sent.get("error_code") or "CODE_SEND_FAILED"` 会兜成发信失败。
    """
    from src.domains.mgmt.auth import verification as v

    r = v.check_rate_limit.__wrapped__ if hasattr(v.check_rate_limit, "__wrapped__") else v.check_rate_limit
    verdict = v._rate_limit_rejection("interval@example.com", "change_email", "")
    assert verdict is None            # 无记录 → 放行

    # 手工造一条"刚刚发过"的影子/真码行，再判一次
    from src.domains.mgmt.auth.verification_store import VerificationCodeStore
    import sqlite3, threading
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE verification_codes (id INTEGER PRIMARY KEY, email TEXT, code_hash TEXT,"
                 " scene TEXT, ip TEXT, created_at TEXT, expires_at TEXT, used INTEGER DEFAULT 0,"
                 " attempts INTEGER DEFAULT 0)")
    import time as _t
    conn.execute("INSERT INTO verification_codes (email, code_hash, scene, ip, created_at, expires_at)"
                 " VALUES (?,?,?,?,?,?)",
                 ("interval@example.com", "k1:abc", "change_email", "",
                  _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime()), "2099-01-01 00:00:00"))
    store = VerificationCodeStore(lambda: (conn, threading.RLock()))
    v._store = store
    try:
        out = v._rate_limit_rejection("interval@example.com", "change_email", "")
    finally:
        v._store = VerificationCodeStore()
    assert out and out["ok"] is False
    assert out["error_code"] == "RATE_LIMITED", out
    assert 0 < out["retry_after"] <= 60, out


def test_email_lookup_is_deterministic():
    """复审 P1：同 email 多行时读侧必须优先活跃/冷静期行，不能靠 LIMIT 1 撞运气。"""
    import inspect
    from src.domains.mgmt.auth import grape_account_db

    src = inspect.getsource(grape_account_db.get_grape_account_by_email)
    assert "ORDER BY CASE WHEN status IN (1, 3) THEN 0 ELSE 1 END" in src
    assert "updated_at DESC" in src


def test_disabled_email_counts_as_registered():
    """复审 P1 配套：冲突判定要把 status=0 也算"已注册"（与唯一键同口径）。"""
    from src.domains.mgmt.auth import grape_account_db as gad

    import inspect

    src = inspect.getsource(gad.is_email_registered)
    assert "status IN (0, 1, 3)" in src
