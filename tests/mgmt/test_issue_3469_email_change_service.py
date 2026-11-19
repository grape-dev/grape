"""Issue #3469 邮箱变更领域服务单测（无 DB / 无邮件服务）。

设计口径：领域服务只依赖端口协议（AccountPort / VerificationPort / NotificationPort），
所以这里全部用假实现，验证的是**领域规则**，不是 SQL：

- G：二次因子"有则要求、无则放行"，占位/私有中转邮箱不得被加固锁死（验收 11）。
- P：失败原因结构化（NEED_SECOND_FACTOR / PASSWORD_INVALID / CODE_EXPIRED / EMAIL_*）。
- Q：验证码存储键 = ``user:<id>``，``new_email`` 作 target 入盐，校验时必须同 target。
- F：通知只在库写成功后发；失败只通知原邮箱。
"""
from __future__ import annotations

import pytest

from src.domains.mgmt.auth.email_change import (
    EmailChangeCommand,
    EmailChangeError,
    EmailChangeRequest,
    EmailChangeService,
    EmailFactor,
    EmailChangePolicy,
)


class FakeAccounts:
    """AccountPort 假实现（记录调用 + 可编排冲突）。"""

    def __init__(self, account=None, registered=None, update_result: bool | str = True):
        self.account = account
        self.registered = set(registered or [])
        self.update_result = update_result
        self.updates = []

    def get(self, user_id):
        return self.account

    def update_email(self, user_id, new_email):
        self.updates.append((user_id, new_email))
        if self.update_result != True:  # noqa: E712 - 保留 True/"conflict"/False 三态
            return self.update_result
        self.account = dict(self.account or {}, email=new_email)
        return True

    def is_email_registered(self, email):
        return email in self.registered


class FakeVerification:
    """VerificationPort 假实现：`verdicts` 按 (scene) 编排校验结果。"""

    def __init__(self, count=0, verdicts=None, send_ok=True):
        self.count = count
        self.verdicts = verdicts or {}
        self.send_ok = send_ok
        self.sends = []
        self.verifies = []

    def send_code(self, email, scene, ip="", rate_limit_key="", target=""):
        self.sends.append(dict(email=email, scene=scene, ip=ip,
                               rate_limit_key=rate_limit_key, target=target))
        return {"ok": True, "message": "验证码已发送"} if self.send_ok else {
            "ok": False, "message": "验证码发送失败", "error_code": "CODE_SEND_FAILED"}

    def verify_code(self, key, scene, code, target="", consume=True):
        self.verifies.append(dict(key=key, scene=scene, code=code, target=target,
                                  consume=consume))
        result = self.verdicts.get(scene, {"ok": True})
        return dict(result)

    def count_sends(self, key, scene, window_seconds=600):
        return self.count

    def check_rate_limit(self, rl_key, scene, ip=""):
        # Issue #3469 E：新地址维度限流（默认放行，专项用例在 review_fixes 里覆盖）
        return {"ok": True, "message": "OK"}


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.inapp = []

    def send(self, to, subject, body):
        self.sent.append((to, subject))
        return True

    def notify_user(self, user_id, subject, body):
        # Issue #3469 F：不可投递邮箱的降级通道
        self.inapp.append((user_id, subject))
        return True


_ACCOUNT_WITH_PWD = {"id": "u1", "email": "old@example.com", "password_hash": "bcrypt$hash"}
_ACCOUNT_NO_PWD = {"id": "u2", "email": "old@example.com", "password_hash": None}
_ACCOUNT_PLACEHOLDER = {"id": "u3", "email": None, "password_hash": None}
_ACCOUNT_NOREPLY = {"id": "u4", "email": "123+noreply@noreply.gitcode.com", "password_hash": None}


def _service(account, verification=None, notifier=None, update_result=True):
    return EmailChangeService(
        accounts=FakeAccounts(account=account, update_result=update_result),
        verification=verification or FakeVerification(),
        notifier=notifier or FakeNotifier(),
    )


# ── 策略判定（纯函数） ────────────────────────────────────────────────
def test_required_factor_matrix():
    assert EmailChangePolicy.required_factor(_ACCOUNT_WITH_PWD) is EmailFactor.PASSWORD
    assert EmailChangePolicy.required_factor(_ACCOUNT_NO_PWD) is EmailFactor.OLD_EMAIL_CODE
    assert EmailChangePolicy.required_factor(_ACCOUNT_PLACEHOLDER) is EmailFactor.NONE
    assert EmailChangePolicy.required_factor(_ACCOUNT_NOREPLY) is EmailFactor.NONE


def test_deliverable_rejects_private_relay_and_literals():
    assert EmailChangePolicy.is_deliverable({"email": "a@b.com"}) is True
    assert EmailChangePolicy.is_deliverable({"email": "1+noreply@noreply.gitcode.com"}) is False
    assert EmailChangePolicy.is_deliverable({"email": "未公开邮箱"}) is False
    assert EmailChangePolicy.is_deliverable({}) is False


# ── Q：key=user:<id> + target 入盐 ──────────────────────────────────
def test_request_code_uses_user_key_and_binds_target():
    v = FakeVerification()
    svc = _service(_ACCOUNT_WITH_PWD, verification=v)
    result = svc.request_code(EmailChangeRequest(user_id="u1", new_email="New@Example.com", ip="1.1.1.1"))

    sent = v.sends[0]
    assert sent["rate_limit_key"] == "user:u1"
    assert sent["target"] == "new@example.com"      # 小写归一
    assert sent["scene"] == "change_email"
    assert result.require_factor is EmailFactor.PASSWORD


def test_request_code_sends_confirm_code_to_old_mailbox_when_no_password():
    v = FakeVerification()
    svc = _service(_ACCOUNT_NO_PWD, verification=v)
    svc.request_code(EmailChangeRequest(user_id="u2", new_email="new@example.com"))

    scenes = {s["scene"]: s for s in v.sends}
    assert scenes["change_email"]["target"] == "new@example.com"
    assert scenes["change_email_confirm"]["email"] == "old@example.com"
    assert scenes["change_email_confirm"]["target"] == "old@example.com"


def test_commit_verifies_with_target_and_user_key():
    v = FakeVerification()
    svc = _service(_ACCOUNT_NO_PWD, verification=v)
    svc.commit(EmailChangeCommand(user_id="u2", new_email="new@example.com", code="123456",
                                  old_email_code="654321"))
    change = [c for c in v.verifies if c["scene"] == "change_email"][0]
    confirm = [c for c in v.verifies if c["scene"] == "change_email_confirm"][0]
    assert change["key"] == "user:u2" and change["target"] == "new@example.com"
    assert confirm["key"] == "user:u2" and confirm["target"] == "old@example.com"


# ── P：结构化失败原因 ─────────────────────────────────────────────
@pytest.mark.parametrize("verdict,expected", [
    ({"ok": False, "error_code": "CODE_EXPIRED", "message": "验证码无效或已过期"}, "CODE_EXPIRED"),
    ({"ok": False, "error_code": "CODE_INVALID", "message": "验证码无效或已过期"}, "CODE_INVALID"),
    ({"ok": False, "message": "验证码无效或已过期"}, "CODE_INVALID"),
])
def test_commit_passes_structured_verify_error(verdict, expected):
    v = FakeVerification(verdicts={"change_email": verdict})
    svc = _service(_ACCOUNT_WITH_PWD, verification=v)
    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id="u1", new_email="new@example.com", code="000000"))
    assert ei.value.status == 400 and ei.value.error_code == expected


# ── G：二次因子 ─────────────────────────────────────────────────
def test_password_factor_required_then_validated():
    svc = _service(_ACCOUNT_WITH_PWD)
    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id="u1", new_email="new@example.com", code="123456"))
    assert ei.value.error_code == "NEED_SECOND_FACTOR"
    assert ei.value.require_factor is EmailFactor.PASSWORD


def test_password_factor_invalid(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.auth.password.verify_password",
                        lambda raw, hashed: False, raising=False)
    svc = _service(_ACCOUNT_WITH_PWD)
    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id="u1", new_email="new@example.com", code="123456",
                                      password="wrong"))
    assert ei.value.error_code == "PASSWORD_INVALID"


def test_old_email_code_required(monkeypatch):
    svc = _service(_ACCOUNT_NO_PWD)
    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id="u2", new_email="new@example.com", code="123456"))
    assert ei.value.error_code == "NEED_SECOND_FACTOR"
    assert ei.value.require_factor is EmailFactor.OLD_EMAIL_CODE


def test_escape_hatch_for_undeliverable_mailbox():
    """验收 11：无密码 + 原邮箱不可投递 → 不得要求二次因子，改绑必须能走通。"""
    notifier = FakeNotifier()
    accounts = FakeAccounts(account=dict(_ACCOUNT_NOREPLY))
    svc = EmailChangeService(accounts=accounts, verification=FakeVerification(), notifier=notifier)

    result = svc.commit(EmailChangeCommand(user_id="u4", new_email="real@example.com", code="123456"))
    assert result.require_factor is EmailFactor.NONE
    assert accounts.updates == [("u4", "real@example.com")]
    # F：成功后只给可投递的新邮箱发通知，不再向 noreply 地址发
    assert [to for to, _ in notifier.sent] == ["real@example.com"]


# ── E：每用户每小时上限 ─────────────────────────────────────────
def test_hourly_cap_rejects_before_sending():
    v = FakeVerification(count=5)
    svc = _service(_ACCOUNT_WITH_PWD, verification=v)
    with pytest.raises(EmailChangeError) as ei:
        svc.request_code(EmailChangeRequest(user_id="u1", new_email="new@example.com"))
    assert ei.value.status == 429 and ei.value.error_code == "CHANGE_EMAIL_TOO_OFTEN"
    assert v.sends == []


# ── 校验前置：格式 / 同址 / 已占用 ───────────────────────────────
def test_request_code_format_same_and_taken():
    svc = _service(dict(_ACCOUNT_WITH_PWD))
    for new_email, expected in [("a@b", "EMAIL_FORMAT"),
                                ("old@example.com", "EMAIL_SAME")]:
        with pytest.raises(EmailChangeError) as ei:
            svc.request_code(EmailChangeRequest(user_id="u1", new_email=new_email))
        assert ei.value.error_code == expected

    svc2 = EmailChangeService(accounts=FakeAccounts(account=dict(_ACCOUNT_WITH_PWD),
                                                    registered={"taken@example.com"}),
                              verification=FakeVerification(), notifier=FakeNotifier())
    with pytest.raises(EmailChangeError) as ei:
        svc2.request_code(EmailChangeRequest(user_id="u1", new_email="taken@example.com"))
    assert ei.value.error_code == "EMAIL_TAKEN"


# ── F：通知时机 ─────────────────────────────────────────────────
def test_no_success_notification_when_update_conflicts(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.auth.password.verify_password",
                        lambda raw, hashed: True, raising=False)
    notifier = FakeNotifier()
    svc = EmailChangeService(accounts=FakeAccounts(account=dict(_ACCOUNT_WITH_PWD),
                                                   update_result="conflict"),
                             verification=FakeVerification(), notifier=notifier)
    with pytest.raises(EmailChangeError) as ei:
        svc.commit(EmailChangeCommand(user_id="u1", new_email="new@example.com", code="123456",
                                      password="pw"))
    assert ei.value.status == 409 and ei.value.error_code == "EMAIL_TAKEN"
    subjects = [subject for _, subject in notifier.sent]
    assert "邮箱已变更" not in subjects
    assert "邮箱变更失败" in subjects


def test_success_notifies_old_and_new(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.auth.password.verify_password",
                        lambda raw, hashed: True, raising=False)
    notifier = FakeNotifier()
    svc = EmailChangeService(accounts=FakeAccounts(account=dict(_ACCOUNT_WITH_PWD)),
                             verification=FakeVerification(), notifier=notifier)
    svc.commit(EmailChangeCommand(user_id="u1", new_email="new@example.com", code="123456",
                                  password="pw"))
    assert [to for to, _ in notifier.sent] == ["old@example.com", "new@example.com"]

@pytest.fixture(autouse=True)
def noop_login_throttle(monkeypatch):
    """领域服务单测里不落 login_failures（专项节流用例单独覆盖）。"""
    from src.domains.mgmt.auth import login_throttle

    monkeypatch.setattr(login_throttle, "check_throttle", lambda *a, **k: {"allowed": True} if False else {"allowed": True}, raising=False)
    monkeypatch.setattr(login_throttle, "record_failure", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(login_throttle, "clear_failures", lambda *a, **k: None, raising=False)
