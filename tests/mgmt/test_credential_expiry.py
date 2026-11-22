"""凭据有效期值对象（CredentialExpiry）：状态判定的唯一处，三处消费方共用。"""
from datetime import date

from src.domains.mgmt.user.credential_expiry import (
    SOON_DAYS,
    STATE_EXPIRED,
    STATE_NONE,
    STATE_OK,
    STATE_SOON,
    STATE_UNKNOWN,
    CredentialExpiry,
)

TODAY = date(2026, 11, 20)


def test_empty_expiry_is_none_state():
    e = CredentialExpiry.parse("", today=TODAY)
    assert e.state == STATE_NONE and e.days_left is None and e.at == ""
    assert e.as_payload() == {"expires_at": "", "expires_state": STATE_NONE}
    assert e.is_alert is False


def test_future_far_is_ok():
    e = CredentialExpiry.parse("2027-01-20", today=TODAY)
    assert e.state == STATE_OK and e.days_left == 61
    assert e.is_alert is False
    assert e.as_payload()["expires_in_days"] == 61


def test_within_threshold_is_soon():
    e = CredentialExpiry.parse("2026-11-30", today=TODAY)
    assert e.state == STATE_SOON and e.days_left == 10 and e.is_alert


def test_boundary_exactly_soon_days():
    due = date.fromordinal(TODAY.toordinal() + SOON_DAYS)
    e = CredentialExpiry.parse(due.isoformat(), today=TODAY)
    assert e.state == STATE_SOON and e.days_left == SOON_DAYS
    e2 = CredentialExpiry.parse(date.fromordinal(TODAY.toordinal() + SOON_DAYS + 1).isoformat(), today=TODAY)
    assert e2.state == STATE_OK


def test_past_is_expired():
    e = CredentialExpiry.parse("2026-11-19", today=TODAY)
    assert e.state == STATE_EXPIRED and e.days_left == -1 and e.is_alert


def test_same_day_is_soon_not_expired():
    """到期当天仍可用（GitLab 到期日在当天结束才失效），不能提前判死。"""
    e = CredentialExpiry.parse("2026-11-20", today=TODAY)
    assert e.state == STATE_SOON and e.days_left == 0


def test_unparseable_is_unknown_without_days():
    e = CredentialExpiry.parse("not-a-date", today=TODAY)
    assert e.state == STATE_UNKNOWN and e.days_left is None
    assert "expires_in_days" not in e.as_payload()


def test_iso_timestamp_is_truncated_to_date():
    e = CredentialExpiry.parse("2026-12-31T23:59:59Z", today=TODAY)
    assert e.state == STATE_OK and e.days_left == 41