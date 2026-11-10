"""Issue #870：/health/realtime im 段 legacy 字段对齐（新引擎健康指标补齐）。

覆盖验收项：
  - engine=on/off 两态 im 段字段集合一致（新引擎额外 running/channels 保留）；
  - 一次收发后 received/sent 各 +1（失败路径 failed +1）；
  - 一次重连后 reconnects +1，flap_by_cause 按原因累计；
  - 缺失场景字段值为 0 而非缺键。
"""
import asyncio
import sqlite3

import pytest

from src.im.channel.channel import CancelContext, ChannelError
from src.im.channel.message import OutboundMessage
from src.im.channel.registry import ChannelRegistry
from src.im.engine.metrics import CountingSender, EngineMetrics
from src.im.engine_wiring import ImEngine

from test_engine_wiring import (
    FakeApi, NoopLease, Recorder, _build_engine, _fake_ctx, _msg, db_path,
)


# ---------------------------------------------------------------- 工具

LEGACY_METRIC_KEYS = {
    "total_messages_received", "total_messages_sent", "total_messages_failed",
    "total_reconnects", "flap_by_cause",
}
SPEC_METRIC_KEYS = LEGACY_METRIC_KEYS | {"reconnects"}


class FlakyChannel:
    """connect 第一次抛 ChannelError（链路断开），之后挂起——驱动 Supervisor 重连分支。"""

    def __init__(self, cfg, sender):
        self.cfg = cfg
        self.sender = sender
        self.disconnected = False
        self.connect_calls = 0

    async def connect(self, ctx):
        self.connect_calls += 1
        if self.connect_calls == 1:
            raise ChannelError("ws closed: flaky")
        await asyncio.sleep(3600)

    async def disconnect(self):
        self.disconnected = True


def _build_engine_with_channel(db_path, api, rec, channel_cls):
    return ImEngine(
        db_path,
        registry=ChannelRegistry(),
        lease_store=NoopLease(),
        api_factory=lambda: api,
        ctx_factory=_fake_ctx,
        process_chat=lambda ctx, api_, parsed: rec.calls.append(
            ("chat", ctx.bot_type, parsed["text"])) or asyncio.sleep(0),
        process_vod=lambda ctx, api_, parsed: rec.calls.append(
            ("vod", ctx.bot_type, parsed["text"])) or asyncio.sleep(0),
        process_command=None,
        handle_selection=None,
        get_pending_selection=None,
        channel_builder=channel_cls,
        scan_interval=3600,
    )


async def _drive_msg(engine, msg):
    channels = engine.supervisor._channels
    assert channels, "supervisor should build channel for backfilled installation"
    ch = next(iter(channels.values()))[0]
    handler = ch.cfg.inbound_handler
    return await handler(CancelContext(), msg)


# ---------------------------------------------------------------- 两态字段集合

async def test_health_field_set_matches_legacy(db_path):
    """engine=on 态 im 段：legacy 指标键全量在场（缺失给 0），Spec 键 reconnects 也在场。"""
    rec = Recorder()
    engine = _build_engine(db_path, FakeApi(), rec)
    await engine.start()
    try:
        health = engine.health()
        assert LEGACY_METRIC_KEYS <= set(health)
        assert "reconnects" in health
        # 新引擎额外字段保留
        for key in ("engine", "instance_id", "running", "channels"):
            assert key in health
    finally:
        await engine.stop()


def test_engine_metric_field_set_is_canonical():
    """im 段指标键集固定（原"Issue #870 两态一致性"在旧 IM 下线后收敛为单一口径）。

    legacy BotManager 已退役（A2 M1），不再存在 engine=off 态；
    保留断言：必需指标键在场 + 键集恰好等于约定集合（防悄悄少键/多键）。
    """
    snap = EngineMetrics().snapshot()
    required = LEGACY_METRIC_KEYS | {"total_messages_degraded", "degraded_by_reason",
                                     "reconnects"}
    assert required <= set(snap)
    assert set(snap) == required


# ---------------------------------------------------------------- 缺失给 0

async def test_health_metrics_default_zero(db_path):
    """无任何流量时指标为 0，flap_by_cause 三分类齐全（缺失 ≠ 缺键）。"""
    rec = Recorder()
    engine = _build_engine(db_path, FakeApi(), rec)
    await engine.start()
    try:
        health = engine.health()
        assert health["total_messages_received"] == 0
        assert health["total_messages_sent"] == 0
        assert health["total_messages_failed"] == 0
        assert health["reconnects"] == 0
        assert health["total_reconnects"] == 0
        flap = health["flap_by_cause"]
        assert flap == {"lease": 0, "ws_transport": 0, "shutdown": 0,
                        "ilink_longpoll": 0}
    finally:
        await engine.stop()


# ---------------------------------------------------------------- 一次收发计数

async def test_one_roundtrip_increments_received_and_sent(db_path):
    """未绑定发送者一条消息：received +1，绑定引导卡 sent +1（验收项 2）。"""
    conn = sqlite3.connect(db_path)
    conn.commit()
    conn.close()
    rec = Recorder()
    api = FakeApi()
    engine = _build_engine(db_path, api, rec)
    await engine.start()
    await asyncio.sleep(0.1)
    try:
        before = engine.health()
        exc = await _drive_msg(engine, _msg("om_m1", "hello", sender="ou_unknown"))
        assert exc is None
        await asyncio.sleep(0.3)          # fire-and-forget 发送落地
        after = engine.health()
        assert after["total_messages_received"] == before["total_messages_received"] + 1
        assert after["total_messages_sent"] == before["total_messages_sent"] + 1
        assert after["total_messages_failed"] == before["total_messages_failed"]
    finally:
        await engine.stop()


async def test_send_failure_increments_failed(db_path):
    """发送器结果钩子失败路径：failed +1（验收项 2 失败分支）。"""
    from src.im.engine.metrics import CountingSender

    metrics = EngineMetrics()

    class FailingInner:
        def capabilities(self):
            from src.im.channel.capability import Capability
            return Capability.TEXT

        async def send(self, out):
            raise RuntimeError("api down")

    class OkInner:
        def capabilities(self):
            from src.im.channel.capability import Capability
            return Capability.TEXT

        async def send(self, out):
            from src.im.channel.message import SendResult
            return SendResult(ok=False, error="rejected")

    failing = CountingSender(FailingInner(), metrics)
    ok_false = CountingSender(OkInner(), metrics)
    from src.im.channel.message import OutboundMessage
    out = OutboundMessage(chat_id="oc-1", text="hi")
    with pytest.raises(RuntimeError):
        await failing.send(out)
    await ok_false.send(out)
    snap = metrics.snapshot()
    assert snap["total_messages_failed"] == 2
    assert snap["total_messages_sent"] == 0


# ---------------------------------------------------------------- 重连与抖动

async def test_reconnect_increments_reconnects_and_flap(db_path):
    """链路断开一次 → 退避重连：reconnects +1，flap_by_cause[ws_transport] +1（验收项 3）。"""
    rec = Recorder()
    engine = _build_engine_with_channel(db_path, FakeApi(), rec, FlakyChannel)
    await engine.start()
    try:
        await asyncio.sleep(0.3)          # 首连失败 → BACKOFF_MIN 退避后重连
        health = engine.health()
        assert health["reconnects"] == 1
        assert health["total_reconnects"] == 1
        assert health["flap_by_cause"]["ws_transport"] == 1
        assert health["flap_by_cause"]["lease"] == 0
        assert health["flap_by_cause"]["shutdown"] == 0
    finally:
        await engine.stop()


async def test_shutdown_records_flap_not_reconnect(db_path):
    """停机断开：flap_by_cause[shutdown] 累计，reconnects 不动。"""
    rec = Recorder()
    engine = _build_engine_with_channel(db_path, FakeApi(), rec, FlakyChannel)
    await engine.start()
    await asyncio.sleep(0.3)              # 首连失败一次（reconnects=1）
    try:
        await engine.stop()
        health = engine.health()
        assert health["flap_by_cause"]["shutdown"] == 1
        assert health["reconnects"] == 1  # 停机不产生新重连
    finally:
        await engine.stop()
