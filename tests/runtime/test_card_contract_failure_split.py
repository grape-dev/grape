"""取卡通道的失败分类 S1–S4（评审③）。

真实传输里 urllib 对 4xx/5xx 抛 HTTPError：
  · 4xx → PlatformCardContractError（凭据/路径配置错，重试无意义，不重试）
  · 5xx → PlatformCardUnavailableError（主控故障，可重试）
  · URLError/超时 → PlatformCardUnavailableError
旧实现把三者都塞进“通道不可用”，4xx 分支是死代码；用例曾用“返回响应对象”的假 opener 掩盖了这点。
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from src.domains.runtime.vcs import card_contract as cc


def _client(opener):
    return cc.PlatformCardContractClient(base_url="http://h", token="t", timeout=3, opener=opener)


def test_s1_4xx_is_a_contract_error(monkeypatch):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(cc.time, "sleep", lambda *a, **k: None)
    with pytest.raises(cc.PlatformCardContractError) as ei:
        _client(opener).fetch_cards()
    assert not isinstance(ei.value, cc.PlatformCardUnavailableError)
    assert "HTTP 401" in str(ei.value)


def test_s2_5xx_is_unavailable_and_retried_once(monkeypatch):
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))

    monkeypatch.setattr(cc.time, "sleep", lambda *a, **k: None)
    with pytest.raises(cc.PlatformCardUnavailableError):
        _client(opener).fetch_cards()
    assert calls["n"] >= 2, "5xx 应重试"


def test_s3_network_error_is_unavailable(monkeypatch):
    def opener(req, timeout=None):
        raise urllib.error.URLError("name or service not known")

    monkeypatch.setattr(cc.time, "sleep", lambda *a, **k: None)
    with pytest.raises(cc.PlatformCardUnavailableError):
        _client(opener).fetch_cards()


def test_s4_ok_response_still_parses(monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            return b'{"cards": {"gitee": {"clone_auth": "basic_password"}}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert "gitee" in _client(lambda req, timeout=None: _Resp()).fetch_cards()
