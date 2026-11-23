"""`<provider>_pat` 投影必须带出 PAT 到期三键（否则前端提醒永不显示）。

回归背景：后端算出 expires_at / expires_state / expires_in_days，响应白名单重建时
丢键 → 前端读 provider+'_pat' 恒空，到期提醒功能等于没上线。
"""
from __future__ import annotations

from src.domains.mgmt.routes.settings import _pat_public_payload


def test_expiry_keys_are_projected():
    st = {
        "exists": True, "login": "account-a", "oauth_bound": False,
        "expires_at": "2026-12-01T00:00:00+00:00", "expires_state": "expiring",
        "expires_in_days": 8,
    }
    out = _pat_public_payload(st)
    assert out["expires_at"] == "2026-12-01T00:00:00+00:00"
    assert out["expires_state"] == "expiring"
    assert out["expires_in_days"] == 8


def test_expiry_keys_omitted_when_absent():
    out = _pat_public_payload({"exists": True, "login": "account-a"})
    assert "expires_at" not in out and "expires_state" not in out and "expires_in_days" not in out


def test_zero_days_left_is_kept():
    """还剩 0 天是有效状态（今天到期），不能被 falsy 判定吞掉。"""
    out = _pat_public_payload({"exists": True, "login": "account-a", "expires_in_days": 0})
    assert out["expires_in_days"] == 0


def test_broken_row_reports_broken_not_empty():
    out = _pat_public_payload({"exists": False, "login": "", "broken": "decrypt failed"})
    assert out["exists"] is False and out["broken"] == "decrypt failed"


def test_absent_and_unbroken_returns_empty_dict():
    assert _pat_public_payload({"exists": False, "login": ""}) == {}


def test_oauth_bound_without_pat_still_projects():
    out = _pat_public_payload({"exists": False, "login": "", "oauth_bound": True,
                               "oauth_login": "account-a"})
    assert out["exists"] is False and out["login"] == ""