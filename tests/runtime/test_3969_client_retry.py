"""Issue #3969：runtime 客户端重试收敛 + 单次/连续失败判定。

漂移形态：`_request` 已有 4s→64s 指数退避，但无抖动（惊群）、无总时长上限；
heartbeat v2 仅 retries=1，且客户端没有「单次 502（被重试覆盖）vs 连续失败」口径，
导致 502 窗口内引擎被误判离线、心跳空转打满。

seam：`RuntimeClient._request` / `heartbeat` 的公开返回值与可观测计数
（`consecutive_failures` / `heartbeat_fail_streak`），以及退避辅助函数。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.runtime.engine import client as client_mod
from src.domains.runtime.engine.client import RuntimeClient


class _FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status = status
        self._body = body
        self._text = text

    async def json(self, *args, **kwargs):
        return self._body

    async def text(self):
        return self._text


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _client(responses):
    c = RuntimeClient("https://srv", "tok", timeout=1)
    c._session = AsyncMock()
    c._session.closed = False
    req = MagicMock()
    if isinstance(responses, list):
        req.side_effect = responses
    else:
        req.return_value = responses
    c._session.request = req
    return c


def _run(coro):
    return asyncio.run(coro)


class TestBackoffJitter:
    def test_jitter_lower_and_upper_bound(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_JITTER", "0.2")
        with patch.object(client_mod.random, "random", return_value=0.0):
            assert client_mod._next_backoff(4, 64) == pytest.approx(3.2)
        with patch.object(client_mod.random, "random", return_value=1.0):
            assert client_mod._next_backoff(4, 64) == pytest.approx(4.8)

    def test_jitter_zero_keeps_base(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_JITTER", "0")
        with patch.object(client_mod.random, "random", return_value=0.5):
            assert client_mod._next_backoff(8, 64) == pytest.approx(8.0)

    def test_backoff_capped(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_JITTER", "0")
        assert client_mod._next_backoff(999, 64) == 64

    def test_jitter_env_clamped(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_JITTER", "5")
        with patch.object(client_mod.random, "random", return_value=1.0):
            # 上限 0.5，base=10 → 15
            assert client_mod._next_backoff(10, 64) == pytest.approx(15.0)


class TestRetryBudget:
    def test_budget_stops_retrying(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "1")
        c = _client([_Ctx(_FakeResp(500))] * 5)
        sleeps = AsyncMock()
        # 起点 0s，之后每次采样都超过 1s 预算
        with patch.object(asyncio, "sleep", new=sleeps), \
             patch.object(client_mod, "_monotonic",
                          side_effect=[0.0] + [10.0] * 10):
            assert _run(c._request("GET", "/api/x", retries=4)) is None
        assert sleeps.await_count == 0

    def test_budget_zero_disables_cap(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = _client([_Ctx(_FakeResp(500))] * 5)
        sleeps = AsyncMock()
        with patch.object(asyncio, "sleep", new=sleeps):
            assert _run(c._request("GET", "/api/x", retries=2)) is None
        assert sleeps.await_count == 2


class TestConsecutiveFailure:
    def test_single_502_is_retried_and_not_counted(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = _client([_Ctx(_FakeResp(502)), _Ctx(_FakeResp(200, {"ok": True}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("POST", "/api/runtime/heartbeat")) == {"ok": True}
        assert c.consecutive_failures == 0

    def test_exhausted_retry_counts_one_call_failure(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = _client([_Ctx(_FakeResp(502))] * 6)
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("POST", "/api/runtime/heartbeat", retries=2)) is None
        assert c.consecutive_failures == 1

    def test_success_resets_failure_counter(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = _client([_Ctx(_FakeResp(502)), _Ctx(_FakeResp(500)),
                     _Ctx(_FakeResp(200, {}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=1)) is None
        assert c.consecutive_failures == 1
        c._session.request = MagicMock(return_value=_Ctx(_FakeResp(200, {})))
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x")) == {}
        assert c.consecutive_failures == 0


class TestHeartbeatBehavior:
    def _heartbeat_client(self, responses):
        c = _client(responses)
        c._heartbeat_session = AsyncMock(return_value=c._session)
        return c

    def test_heartbeat_uses_at_least_three_retries(self):
        c = _client(_Ctx(_FakeResp(200, {"status": "ok"})))
        c._heartbeat_session = AsyncMock(return_value=c._session)
        c._request = AsyncMock(return_value={"status": "ok"})
        _run(c.heartbeat([1]))
        assert c._request.await_args.kwargs["retries"] >= 3

    def test_heartbeat_streak_increments_on_failure(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = self._heartbeat_client([_Ctx(_FakeResp(502))] * 6)
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c.heartbeat([1])) is None
        assert c.heartbeat_fail_streak == 1
        assert c.heartbeat_offline(threshold=3) is False

    def test_heartbeat_streak_reaches_offline_threshold(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = self._heartbeat_client([_Ctx(_FakeResp(502))] * 30)
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            for _ in range(3):
                assert _run(c.heartbeat([1])) is None
        assert c.heartbeat_fail_streak == 3
        assert c.heartbeat_offline(threshold=3) is True

    def test_heartbeat_streak_resets_on_success(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = self._heartbeat_client([_Ctx(_FakeResp(502))] * 6
                                   + [_Ctx(_FakeResp(200, {"status": "ok"}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c.heartbeat([1])) is None
            assert c.heartbeat_fail_streak == 1
            assert _run(c.heartbeat([1])) == {"status": "ok"}
        assert c.heartbeat_fail_streak == 0

class TestRawRequestBackoff:
    """复审 R1：heartbeat-v2 使用的 `_request_raw` 必须有抖动与总时长预算。"""

    def test_raw_retry_delays_have_jitter(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_JITTER", "0.2")
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "0")
        c = _client([_Ctx(_FakeResp(502)), _Ctx(_FakeResp(502)),
                     _Ctx(_FakeResp(200, {"status": "ok"}))])
        slept = []

        async def _sleep(sec):
            slept.append(sec)

        with patch.object(asyncio, "sleep", new=_sleep):
            status, data, _text = _run(
                c._request_raw("POST", "/api/runtime/heartbeat-v2", retries=2,
                               session=c._session))
        assert status == 200 and data == {"status": "ok"}
        assert len(slept) == 2
        # 固定退避为 2s→4s；加 ±20% 抖动后必须落在区间内（不是精确固定值）
        assert 1.6 <= slept[0] <= 2.4
        assert 3.2 <= slept[1] <= 4.8

    def test_raw_retry_stops_when_budget_exceeded(self, monkeypatch):
        monkeypatch.setenv("GRAPE_CLIENT_RETRY_BUDGET_SECONDS", "1")
        c = _client([_Ctx(_FakeResp(503))] * 6)
        sleeps = AsyncMock()
        with patch.object(asyncio, "sleep", new=sleeps), \
             patch.object(client_mod, "_monotonic",
                          side_effect=[0.0] + [10.0] * 10):
            assert _run(c._request_raw("POST", "/api/runtime/heartbeat-v2",
                                       retries=5, session=c._session)) == (0, None, "")
        assert sleeps.await_count == 0
