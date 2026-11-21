"""Issue #3698：并发注册冲突（飞书 3001 too many connections）的归因 / 退避 / 告警。

现场（Grafana，2026-11-21 12:34:02~12:35:34 UTC）：飞书适配器把 websockets
`ConnectionClosedError(3001)` 直接穿透 connect()，Supervisor 的通用 `except Exception`
把它记成 `channel crashed (unexpected)` 全栈 ERROR，并按 15s 封顶退避无限重试 → 渠道
长期不可用且日志刷屏。

本文件锁三条契约：
  1. 3001 归因为 `ws_register_conflict`（独立 bucket），不再走 "unexpected" 全栈分支；
  2. 该归因的退避突破 BACKOFF_MAX=15s（分段升到 REGISTER_CONFLICT_BACKOFF_MAX），
     连续 REGISTER_CONFLICT_ALERT_AFTER 次后升级为 ERROR 告警；
  3. 同一 app_id 只允许一条注册连接（多安装/僵尸连接不再并发注册撞上限）。
"""
import asyncio
import logging

import pytest

from src.domains.runtime.im.channel import (
    CancelContext, Channel, ChannelConfig, ChannelRegistrationConflict,
    OutboundMessage, SendResult, Type,
)
from src.domains.runtime.im.channel.registry import ChannelRegistry as Registry
from src.domains.runtime.im.engine import supervisor as supervisor_mod
from src.domains.runtime.im.engine.metrics import EngineMetrics
from src.domains.runtime.im.engine.supervisor import (
    BACKOFF_MAX, REGISTER_CONFLICT_ALERT_AFTER, REGISTER_CONFLICT_BACKOFF_MAX,
    Supervisor, next_backoff,
)


# ---------------------------------------------------------------- 替身

class FakeLeaseStore:
    def __init__(self):
        self.acquired = []
        self.released = []

    def try_acquire(self, key, steal_after=60, instance_timeout=45.0):
        self.acquired.append(key)
        return f"tok:{key}"

    def renew(self, key, token):
        return True

    def release(self, key, token):
        self.released.append((key, token))

    def batch_renew(self, held):
        return {}

    def list_expired_or_unheld(self, cursor="", limit=100):
        return []


class ConflictChannel(Channel):
    """每次 connect 都以 3001 并发注册冲突失败（复现现场）。"""

    attempts = 0

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        type(self).attempts += 1
        raise ChannelRegistrationConflict(
            "feishu ws register conflict: close_code=3001 (registered) too many connections")

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=False)


class IdleChannel(Channel):
    """connect 立即返回（模拟建连成功后的让渡/断开），仅用于装配计数。"""

    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return self.cfg.type

    async def connect(self, ctx: CancelContext) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=True)


class Capture(logging.Handler):
    """收集 supervisor 日志（断言"没有全栈 ERROR 刷屏"这类否定式契约）。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self, level=logging.DEBUG):
        return "\n".join(
            r.getMessage() for r in self.records if r.levelno >= level)


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
    """把退避睡眠替身为"记账 + 让出一次事件循环"，用例不必真等 5~300s。"""
    recorded = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(supervisor_mod, "_backoff_sleep", _fake_sleep)
    return recorded


def _supervisor(metrics, installations, channel_cls, leases=None):
    registry = Registry()
    registry.register(Type.FEISHU, channel_cls)

    async def provider():
        return list(installations())

    sup = Supervisor(
        registry=registry, instance_id="r1",
        lease_store=leases if leases is not None else FakeLeaseStore(),
        installation_provider=provider, metrics=metrics,
    )
    sup._running = True
    sup.instances = ["r1"]
    return sup


def _inst(inst_id: str, app_id: str = "app-1"):
    return {
        "installation_id": inst_id,
        "channel_type": "feishu",
        "scope": "user",
        "config": {"app_id": app_id, "app_secret": "sec"},
    }


async def _drive(sup, attempts, timeout=3.0):
    """跑一轮扫描后让 _run 循环推进到指定尝试次数。"""
    await sup._scan_once()

    async def _poll():
        while ConflictChannel.attempts < attempts:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _cancel_runs(sup):
    sup._running = False
    for task in list(sup._run_tasks.values()):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    sup._run_tasks.clear()


# ---------------------------------------------------------------- 1. 归因

def test_register_conflict_attributed_not_unexpected_crash(captured, delays):
    """3001 → 记 ws_register_conflict（WARNING），绝不再打 "crashed (unexpected)" 全栈 ERROR。"""
    ConflictChannel.attempts = 0
    metrics = EngineMetrics()
    sup = _supervisor(metrics, lambda: [_inst("app-1")], ConflictChannel)

    async def scenario():
        try:
            await _drive(sup, attempts=3)
        finally:
            await _cancel_runs(sup)

    asyncio.run(scenario())

    assert metrics.flap_by_cause["ws_register_conflict"] == 3
    assert metrics.flap_by_cause["ws_transport"] == 0    # 不再混入普通链路抖动桶
    assert metrics.reconnects == 3
    text = captured.text(logging.WARNING)
    assert "registration conflict" in text
    assert "crashed (unexpected)" not in captured.text(logging.DEBUG)


def test_register_conflict_escalates_to_error_alert(captured, delays):
    """连续 REGISTER_CONFLICT_ALERT_AFTER 次后升级为 ERROR 告警（可被 Grafana 规则捕获）。"""
    ConflictChannel.attempts = 0
    metrics = EngineMetrics()
    sup = _supervisor(metrics, lambda: [_inst("app-1")], ConflictChannel)

    async def scenario():
        try:
            await _drive(sup, attempts=REGISTER_CONFLICT_ALERT_AFTER)
        finally:
            await _cancel_runs(sup)

    asyncio.run(scenario())

    errors = captured.text(logging.ERROR)
    assert "registration conflict escalated" in errors
    assert f"consecutive={REGISTER_CONFLICT_ALERT_AFTER}" in errors
    # 前几次仍应是 WARNING（降噪）：不是一来就 ERROR
    assert captured.text(logging.ERROR).count("registration conflict escalated") == 1


# ---------------------------------------------------------------- 2. 退避

def test_conflict_backoff_breaks_legacy_15s_cap(captured, delays):
    """注册冲突的退避必须突破 BACKOFF_MAX=15s，否则只会以 15s 间隔反复撞上限。"""
    ConflictChannel.attempts = 0
    sup = _supervisor(EngineMetrics(), lambda: [_inst("app-1")], ConflictChannel)

    async def scenario():
        try:
            await _drive(sup, attempts=4)
        finally:
            await _cancel_runs(sup)

    asyncio.run(scenario())

    assert delays[0] >= supervisor_mod.REGISTER_CONFLICT_BACKOFF_MIN
    assert delays[:4] == sorted(delays[:4])            # 单调递增
    assert max(delays) > BACKOFF_MAX                   # 突破 15s 封顶
    assert max(delays) <= REGISTER_CONFLICT_BACKOFF_MAX


def test_next_backoff_caps_per_cause():
    """退避递推契约：普通抖动 2s→15s 封顶；注册冲突单独 5s→300s 封顶。"""
    assert next_backoff(2.0) == 4.0
    assert next_backoff(BACKOFF_MAX) == BACKOFF_MAX
    assert next_backoff(5.0, register_conflict=True) == 10.0
    assert next_backoff(160.0, register_conflict=True) == REGISTER_CONFLICT_BACKOFF_MAX
    assert next_backoff(REGISTER_CONFLICT_BACKOFF_MAX, register_conflict=True) == \
        REGISTER_CONFLICT_BACKOFF_MAX


# ---------------------------------------------------------------- 3. 同 app_id 单连接

def test_duplicate_app_id_keeps_single_registration(captured):
    """同一 app_id 的两个安装行 → 只允许一条注册连接（否则并发注册撞 3001）。"""
    built = []

    def factory(cfg):
        built.append(cfg.installation_id)
        return IdleChannel(cfg)

    leases = FakeLeaseStore()
    installs = [_inst("inst-a"), _inst("inst-b")]
    sup = _supervisor(EngineMetrics(), lambda: installs, factory, leases=leases)

    async def scenario():
        try:
            await sup._scan_once()
            await asyncio.sleep(0)
        finally:
            await _cancel_runs(sup)

    asyncio.run(scenario())

    assert built == ["inst-a"]              # 第二个安装不建连接
    assert len(sup._channels) == 1
    assert len(leases.acquired) == 1        # 也不占租约（避免"持租约不连接"）
    assert "duplicate app installation" in captured.text(logging.WARNING)
    assert "inst-b" in captured.text(logging.WARNING)   # 显式记录便于运维定位


def test_same_installation_rescan_is_not_treated_as_duplicate(captured):
    """同安装重扫（配置未变）不能被自己的 app_id 去重挡掉。"""
    built = []

    def factory(cfg):
        built.append(cfg.installation_id)
        return IdleChannel(cfg)

    sup = _supervisor(EngineMetrics(), lambda: [_inst("inst-a")], factory)

    async def scenario():
        try:
            await sup._scan_once()
            await sup._scan_once()
            await asyncio.sleep(0)
        finally:
            await _cancel_runs(sup)

    asyncio.run(scenario())

    assert built == ["inst-a"]              # 第二次扫描命中指纹复用，不重建
    assert len(sup._channels) == 1


def test_skipped_duplicate_takes_over_after_owner_released(captured):
    """owner 让渡后，被跳过的安装应在下一轮接上（避免永久饿死）。"""
    built = []

    def factory(cfg):
        built.append(cfg.installation_id)
        return IdleChannel(cfg)

    state = {"installs": [_inst("inst-a"), _inst("inst-b")]}
    sup = _supervisor(EngineMetrics(), lambda: state["installs"], factory)

    async def scenario():
        try:
            await sup._scan_once()          # inst-a 占位，inst-b 被跳
            await asyncio.sleep(0)
            state["installs"] = [_inst("inst-b")]
            await sup._scan_once()          # inst-a 让渡，释放 app 归属
            await sup._scan_once()          # inst-b 接上
            await asyncio.sleep(0)
        finally:
            await _cancel_runs(sup)

    asyncio.run(scenario())

    assert built == ["inst-a", "inst-b"]
    assert list(sup._channels) == ["inst-b"]