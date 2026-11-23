"""Issue #3897 C 组（A）：IM 通道传输层断连不得打成 `channel crashed (unexpected)`。

现场（生产 Loki，grape-mgmt，1 小时 11 条）：

    [im-engine] channel crashed (unexpected): inst=<uuid> type=feishu
    BrokenPipeError ... (asyncio selector_events 写 socket 时断连)

Supervisor 的通用 `except Exception` 把传输层断连记成「意外崩溃」全栈 ERROR，
既污染 error 计数（验收要求 1 小时 channel crashed = 0），也不利于按 cause 归因。
适配器内部多数路径已归一为 ChannelError，但仍有 BrokenPipe/ConnectionReset 从
connect() 逃逸（不同适配器/退出路径），必须在 Supervisor 层兜底分类。

契约：
  1. BrokenPipe/ConnectionReset/ConnectionAborted/ConnectionRefused/时序超时 →
     WARNING 单行（inst/type/cause/errno）+ `ws_transport` 归因 + 既有指数退避；
  2. 绝不出现 `channel crashed (unexpected)` 全栈 ERROR；
  3. 真 bug（如 AttributeError）仍按 unexpected ERROR 保留（不能把缺陷吞成抖动）。
"""
import asyncio
import logging

import pytest

from src.domains.runtime.im.channel import (
    CancelContext, Channel, ChannelConfig, OutboundMessage, SendResult, Type,
)
from src.domains.runtime.im.channel.registry import ChannelRegistry as Registry
from src.domains.runtime.im.engine import supervisor as supervisor_mod
from src.domains.runtime.im.engine.metrics import EngineMetrics
from src.domains.runtime.im.engine.supervisor import BACKOFF_MAX, Supervisor


class _LeaseStore:
    def try_acquire(self, key, steal_after=60, instance_timeout=45.0):
        return f"tok:{key}"

    def renew(self, key, token):
        return True

    def release(self, key, token):
        return None

    def batch_renew(self, held):
        return {}

    def list_expired_or_unheld(self, cursor="", limit=100):
        return []


class BrokenPipeChannel(Channel):
    """每次 connect 都以真实传输层错误失败（复现现场）。"""

    attempts = 0

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        type(self).attempts += 1
        raise BrokenPipeError(32, "Broken pipe")

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=False)


class BugChannel(Channel):
    """非传输层异常（真 bug）：必须仍按 unexpected 全栈 ERROR 暴露。"""

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


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self, level=logging.DEBUG):
        return "\n".join(r.getMessage() for r in self.records if r.levelno >= level)


@pytest.fixture
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


@pytest.fixture
def delays(monkeypatch):
    recorded = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(supervisor_mod, "_backoff_sleep", _fake_sleep)
    return recorded


def _supervisor(channel_cls, metrics=None):
    registry = Registry()
    registry.register(Type.FEISHU, channel_cls)

    async def provider():
        return [{"installation_id": "app-1", "channel_type": "feishu", "scope": "user",
                 "config": {"app_id": "app-1", "app_secret": "sec"}}]

    sup = Supervisor(registry=registry, instance_id="r1", lease_store=_LeaseStore(),
                     installation_provider=provider, metrics=metrics or EngineMetrics())
    sup._running = True
    sup.instances = ["r1"]
    return sup


async def _drive(sup, channel_cls, attempts, timeout=3.0):
    await sup._scan_once()

    async def _poll():
        while channel_cls.attempts < attempts:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _cancel(sup):
    sup._running = False
    for task in list(sup._run_tasks.values()):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    sup._run_tasks.clear()


def test_broken_pipe_is_transport_flap_not_crash(captured, delays):
    BrokenPipeChannel.attempts = 0
    metrics = EngineMetrics()
    sup = _supervisor(BrokenPipeChannel, metrics)

    async def scenario():
        try:
            await _drive(sup, BrokenPipeChannel, attempts=3)
        finally:
            await _cancel(sup)

    asyncio.run(scenario())

    text = captured.text(logging.WARNING)
    assert "channel crashed (unexpected)" not in captured.text(logging.DEBUG)
    assert "transport error" in text
    assert "inst=app-1" in text and "type=feishu" in text
    assert "BrokenPipe" in text
    assert metrics.flap_by_cause["ws_transport"] == 3
    assert metrics.reconnects == 3
    # 既有指数退避仍在（2s → 4s → 8s，封顶 15s）
    assert delays[:3] == sorted(delays[:3])
    assert max(delays) <= BACKOFF_MAX


def test_true_bug_still_reported_as_unexpected_crash(captured, delays):
    BugChannel.attempts = 0
    sup = _supervisor(BugChannel)

    async def scenario():
        try:
            await _drive(sup, BugChannel, attempts=1)
        finally:
            await _cancel(sup)

    asyncio.run(scenario())

    errs = [r for r in captured.records
            if r.levelno >= logging.ERROR and "channel crashed (unexpected)" in r.getMessage()]
    assert errs, "非传输层异常（适配器 bug）必须继续以 ERROR 暴露，不能被降级吞掉"
