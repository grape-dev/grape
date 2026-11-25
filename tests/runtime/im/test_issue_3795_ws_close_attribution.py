"""Issue #3795：飞书 WS 关闭码（3003 ping timeout 等）不得记为 unexpected crash。

现场（生产 2026-11-23 00:19–00:50 CST，grape-mgmt，33 条/小时、25 个实例）：

    [im-engine] channel crashed (unexpected): inst=<uuid> type=feishu
    websockets.exceptions.ConnectionClosedError: received 3003 (registered) ping timeout

契约：
  1. 适配器层把带关闭码的 WS 关闭归一为 ``ChannelWsClosed``（ChannelError 子类），
     携带 ``close_code``，``str()`` 含 ``ws closed code=<code>``；
  2. Supervisor 对 ``ChannelWsClosed`` / 泄漏的 duck-typed ``ConnectionClosed``
     记 WARNING 单行 + ``ws_transport`` + ``ws_close_<code>`` 归因，绝不写
     ``channel crashed (unexpected)`` 全栈 ERROR；
  3. 归因键有界（4000+ 应用自定义码 → ``ws_close_4xxx``，无码 → ``ws_close_unknown``）；
  4. 真 bug（AttributeError 等）仍按 unexpected ERROR 暴露（不得被降级吞掉）。
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from src.domains.runtime.im.channel import (
    CancelContext, Channel, ChannelConfig, ChannelError, ChannelWsClosed,
    OutboundMessage, SendResult, Type,
)
from src.domains.runtime.im.channel.registry import ChannelRegistry as Registry
from src.domains.runtime.im.adapters.feishu.channel import FeishuChannel
from src.domains.runtime.im.adapters.feishu.outbound import FeishuSender
from src.domains.runtime.im.engine import supervisor as supervisor_mod
from src.domains.runtime.im.engine.metrics import EngineMetrics, ws_close_cause
from src.domains.runtime.im.engine.supervisor import Supervisor, ws_close_code_of


def _closed_error(code: int, reason: str = "registered ping timeout") -> ConnectionClosedError:
    frame = Close(code, reason)
    return ConnectionClosedError(frame, frame, rcvd_then_sent=True)


# ── 适配器层：关闭码归一 ─────────────────────────────────

class FailingWS:
    """接收循环抛指定异常的 ws 替身（ticket 现场 3003 由服务端主动关闭）。"""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc

    async def send(self, data):
        return None

    async def close(self):
        self.closed = True


def _feishu(ws) -> FeishuChannel:
    cfg = ChannelConfig(
        type=Type.FEISHU,
        raw={"app_id": "app1", "app_secret": "sec"},
        installation_id="i1",
    )

    async def _endpoint(app_id, app_secret):
        return "wss://example.com/ws?fpid=1", 0

    return FeishuChannel(
        cfg, FeishuSender(reply_text=None, reply_card=None),
        endpoint_fetcher=_endpoint,
        ws_connect=lambda url, **kw: ws,
        seen_loader=lambda app: set(),
        seen_persist=lambda app, mid: None,
        seen_cleanup=lambda app: None,
        ping_interval=9999,
    )


async def test_feishu_3003_normalized_to_channel_ws_closed():
    ch = _feishu(FailingWS(_closed_error(3003)))
    with pytest.raises(ChannelWsClosed) as ei:
        await ch.connect(CancelContext())
    err = ei.value
    assert err.close_code == 3003
    assert isinstance(err, ChannelError)
    assert isinstance(err.__cause__, ConnectionClosedError)   # 原始原因保留
    assert "ws closed code=3003" in str(err)


async def test_feishu_close_without_code_stays_plain_channel_error():
    """无关闭码的传输异常（BrokenPipe）不虚构关闭码，仍是普通 ChannelError。"""
    ch = _feishu(FailingWS(BrokenPipeError(32, "Broken pipe")))
    with pytest.raises(ChannelError) as ei:
        await ch.connect(CancelContext())
    assert not isinstance(ei.value, ChannelWsClosed)


# ── Supervisor 层：WARNING 归因 ──────────────────────────

class WsClosedChannel(Channel):
    attempts = 0

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        type(self).attempts += 1
        raise ChannelWsClosed("feishu ws failed: app=app-1", close_code=3003)

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=False)


class LeakedCloseChannel(Channel):
    """适配器没归一 → 裸 websockets ConnectionClosedError 泄漏到 Supervisor。"""

    attempts = 0

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        type(self).attempts += 1
        raise _closed_error(1006, "connection closed abnormally")

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=False)


class BugChannel(Channel):
    attempts = 0

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        type(self).attempts += 1
        raise AttributeError("payload shape changed")

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=False)


class _LeaseStore:
    def try_acquire(self, key, steal_after=60, instance_timeout=45.0):
        return f"tok:{key}"

    def renew(self, key, token):
        return True

    def release(self, key, token, **kw):
        return None

    def batch_renew(self, held):
        return {}

    def list_expired_or_unheld(self, cursor="", limit=100):
        return []


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self, level=logging.DEBUG):
        return "\n".join(r.getMessage() for r in self.records if r.levelno >= level)


@pytest.fixture()
def captured():
    handler = Capture()
    logger = supervisor_mod.log
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture()
def delays(monkeypatch):
    recorded = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(supervisor_mod, "_backoff_sleep", _fake_sleep)
    return recorded


def _supervisor(channel_cls, metrics):
    registry = Registry()
    registry.register(Type.FEISHU, channel_cls)

    async def provider():
        return [{"installation_id": "app-1", "channel_type": "feishu", "scope": "user",
                 "config": {"app_id": "app-1", "app_secret": "sec"}}]

    sup = Supervisor(registry=registry, instance_id="r1", lease_store=_LeaseStore(),
                     installation_provider=provider, metrics=metrics)
    sup._running = True
    sup.instances = ["r1"]
    return sup


async def _run(channel_cls, attempts, metrics, timeout=3.0):
    sup = _supervisor(channel_cls, metrics)
    await sup._scan_once()

    async def _poll():
        while channel_cls.attempts < attempts:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_poll(), timeout=timeout)
    finally:
        sup._running = False
        for task in list(sup._run_tasks.values()):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sup._run_tasks.clear()


async def test_supervisor_ws_closed_is_warning_with_close_attribution(captured, delays):
    WsClosedChannel.attempts = 0
    metrics = EngineMetrics()

    await _run(WsClosedChannel, attempts=3, metrics=metrics)

    assert "channel crashed (unexpected)" not in captured.text(logging.DEBUG)
    warnings = [r for r in captured.records
                if r.levelno == logging.WARNING and "channel ws closed" in r.getMessage()]
    assert warnings, "WS 关闭必须记 WARNING 单行"
    assert "ws closed code=3003" in warnings[0].getMessage()
    assert "inst=app-1" in warnings[0].getMessage()
    assert warnings[0].exc_info is None   # 不带全栈（非 log.exception）

    assert metrics.reconnects == 3
    assert metrics.flap_by_cause["ws_transport"] == 3    # 看板既有口径不断档
    assert metrics.flap_by_cause["ws_close_3003"] == 3   # 关闭码维度归因
    assert max(delays) <= supervisor_mod.BACKOFF_MAX


async def test_supervisor_leaked_connection_closed_is_not_unexpected_crash(captured, delays):
    """适配器未归一的裸 ConnectionClosedError（非 OSError 族）→ 同样 WARNING + 关闭码。"""
    LeakedCloseChannel.attempts = 0
    metrics = EngineMetrics()

    await _run(LeakedCloseChannel, attempts=2, metrics=metrics)

    assert "channel crashed (unexpected)" not in captured.text(logging.DEBUG)
    assert "ws closed code=1006" in captured.text(logging.WARNING)
    assert metrics.flap_by_cause["ws_close_1006"] == 2
    assert metrics.reconnects == 2


async def test_true_bug_still_reported_as_unexpected_crash(captured, delays):
    """真 bug（AttributeError）不得被关闭码归因吞成 WARNING。"""
    BugChannel.attempts = 0

    await _run(BugChannel, attempts=1, metrics=EngineMetrics())

    errs = [r for r in captured.records
            if r.levelno >= logging.ERROR and "channel crashed (unexpected)" in r.getMessage()]
    assert errs


# ── 归因键有界 + duck-typing 取码 ────────────────────────

def test_ws_close_cause_is_bounded():
    assert ws_close_cause(3003) == "ws_close_3003"
    assert ws_close_cause(1006) == "ws_close_1006"
    assert ws_close_cause(4001) == "ws_close_4xxx"   # 应用自定义码分桶（键有界）
    assert ws_close_cause(None) == "ws_close_unknown"
    assert ws_close_cause("3003") == "ws_close_unknown"
    assert ws_close_cause(True) == "ws_close_unknown"


def test_ws_close_code_of_ducktyped_close_frame():
    class _Frame:
        code = 3003
        reason = "registered ping timeout"

    class _Closed(Exception):
        rcvd = _Frame()
        sent = None

    assert ws_close_code_of(_Closed()) == 3003
    # 普通业务异常的 code 属性（HTTP/业务码）不得被误认为 WS 关闭码
    err = RuntimeError("boom")
    err.code = 500
    assert ws_close_code_of(err) is None