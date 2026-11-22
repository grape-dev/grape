"""平台卡取卡通道的失败语义（S1–S7）。2026-11-21

背景（代码核对）：

  · 服务端读 mgmt 表失败时回 `{"cards": {}, "error": "platform_cards_unavailable"}`，
    且**绝不打 5xx**（为了让 worker 拿到"表读不了"这层信息）；
  · 但客户端原先只取 `cards`，把 `error` 丢弃 → 空 cards 被当"没有卡" → 装配层回落
    代码默认卡（不含 gitee）→ clone 端报 `platform_not_registered`；
  · 结果：**取卡系统坏了，任务侧看到的是「平台没注册」**，排查方向被带偏。

本用例锁三件事：
  1. 通道故障（error / 网络 / 超时 / 5xx）抛 `PlatformCardUnavailableError`，可重试；
     配置错（401/403/404）抛 `PlatformCardContractError`，不重试；
  2. 装配层把降级原因交出来（`ServerContractCardSource.degraded` /
     `PlatformCardAssembler.last_degraded`）；
  3. clone 端据此分码：`platform_card_contract_unavailable`（修链路）vs
     `platform_not_registered`（配卡）。
"""
from __future__ import annotations

import os
import sys
import urllib.error

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, SRC)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC, "data", "test_sessions.db"))

from src.domains.runtime.vcs import card_contract as CC
from src.domains.runtime.vcs import clone_endpoint as CE
from src.domains.runtime.vcs import platform_card as PC
from src.domains.runtime.vcs.card_sources import (
    PlatformCardAssembler,
    PlatformCardSource,
    ServerContractCardSource,
)


class _Resp:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client_with(responses):
    """构造注入了假 opener 的客户端；returns (client, calls)。"""
    calls: list[str] = []
    queue = list(responses)

    def _open(req, timeout=None):
        calls.append(req.full_url)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    c = CC.PlatformCardContractClient(base_url="http://probe.local", token="***",
                                      opener=_open)
    return c, calls


# ---------------------------------------------------------------- S1 服务端 error
def test_s1_server_error_becomes_unavailable():
    """服务端明确回 error → 必须抛 Unavailable，不能当"没有卡"。"""
    body = '{"cards": {}, "error": "platform_cards_unavailable"}'
    c, calls = _client_with([_Resp(200, body), _Resp(200, body)])
    with pytest.raises(CC.PlatformCardUnavailableError) as ei:
        c.fetch_cards()
    assert "platform_cards_unavailable" in str(ei.value)
    assert len(calls) == CC._RETRY_ATTEMPTS, "通道故障应重试"


# ---------------------------------------------------------------- S2 网络失败重试
def test_s2_network_error_retries_then_raises():
    c, calls = _client_with([urllib.error.URLError("boom"), urllib.error.URLError("boom again")])
    with pytest.raises(CC.PlatformCardUnavailableError):
        c.fetch_cards()
    assert len(calls) == CC._RETRY_ATTEMPTS


def test_s2b_success_on_retry():
    """第一次超时、第二次成功 → 不应抛。"""
    c, calls = _client_with([urllib.error.URLError("transient"), _Resp(200, '{"cards": {"gitee": {"host": "gitee.com"}}}')])
    cards = c.fetch_cards()
    assert cards["gitee"]["host"] == "gitee.com"
    assert len(calls) == 2


# ---------------------------------------------------------------- S3 配置错不重试
def test_s3_unauthorized_is_not_retried():
    """401 是凭据/配置错，重试不会变对 → 只调一次，抛非 Unavailable 的 ContractError。"""
    c, calls = _client_with([_Resp(401, "{}"), _Resp(401, "{}")])
    with pytest.raises(CC.PlatformCardContractError) as ei:
        c.fetch_cards()
    assert not isinstance(ei.value, CC.PlatformCardUnavailableError)
    assert len(calls) == 1, "4xx 不得重试"


# ---------------------------------------------------------------- S4 装配降级可见
class _FakeSource(PlatformCardSource):
    name = "fake"

    def __init__(self, raws, available=True):
        self._raws = raws
        self._available = available
        self.degraded = ""

    def available(self):
        return self._available

    def raws(self):
        return dict(self._raws)


def test_s4_assembler_reports_degraded_reason():
    class _BrokenClient:
        def configured(self):
            return True

        def fetch_cards(self):
            raise CC.PlatformCardUnavailableError("http://probe.local: boom")

    src = ServerContractCardSource(client=_BrokenClient())
    assembler = PlatformCardAssembler(
        meta=_FakeSource({}),
        file_source=_FakeSource({}, available=False),
        builtin=_FakeSource({"gitcode": {"host": "gitcode.com"}}),
        fallback_sources=[src],
    )
    raws = assembler.assemble_raws()
    assert "gitcode" in raws
    assert src.degraded == "contract_unavailable"
    assert assembler.last_degraded == "contract_unavailable"


def test_s4b_empty_contract_is_not_channel_failure():
    """服务端正常回空表（0 行 enabled）→ contract_empty，不算通道故障。"""
    class _EmptyClient:
        def configured(self):
            return True

        def fetch_cards(self):
            return {}

    src = ServerContractCardSource(client=_EmptyClient())
    assembler = PlatformCardAssembler(
        meta=_FakeSource({}),
        file_source=_FakeSource({}, available=False),
        builtin=_FakeSource({"gitcode": {"host": "gitcode.com"}}),
        fallback_sources=[src],
    )
    assembler.assemble_raws()
    assert src.degraded == "contract_empty"
    assert assembler.last_degraded == "", "空表属配置问题，不算通道降级"


# ---------------------------------------------------------------- S5/S6 分码
def test_s5_clone_host_reports_contract_unavailable(monkeypatch):
    monkeypatch.setattr(CE, "_card", lambda provider: None)
    monkeypatch.setattr(PC, "platform_card_degraded_reason", lambda: "contract_unavailable")
    with pytest.raises(CE.CloneEndpointError) as ei:
        CE.clone_host("gitee")
    assert ei.value.code == "platform_card_contract_unavailable"
    assert "RUNTIME_SERVER_URL" in str(ei.value)


def test_s6_clone_host_reports_platform_not_registered(monkeypatch):
    monkeypatch.setattr(CE, "_card", lambda provider: None)
    monkeypatch.setattr(PC, "platform_card_degraded_reason", lambda: "")
    with pytest.raises(CE.CloneEndpointError) as ei:
        CE.clone_host("nosuchplat")
    assert ei.value.code == "platform_not_registered"


# ---------------------------------------------------------------- S7 超时
def test_s7_default_timeout_is_ten_seconds():
    """默认 10s（2026-11-21 由 2.0s 两次上调）：取卡只在缓存未命中时发生，
    宁可多等也不要把可用主控判成不可达。"""
    assert CC._DEFAULT_CARD_TIMEOUT == 10.0
    assert CC.resolve_card_timeout() >= 10.0 or "GRAPE_PLATFORM_CARD_TIMEOUT" in os.environ