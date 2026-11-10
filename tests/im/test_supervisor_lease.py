"""Supervisor 租约键契约测试（Issue #742 §4.2 / PR #857 审查修复项）。

租约获取/释放必须统一走 lease_key(channel_type, installation_id)，
且 release 的 token 由 Supervisor 自持（不探测 LeaseStore 实现私有属性）。
"""
import asyncio

from src.im.channel import (
    CancelContext, Channel, ChannelConfig, OutboundMessage,
    PermanentChannelError, SendResult, Type,
)
from src.im.engine.lease import lease_key
from src.im.channel.registry import ChannelRegistry as Registry
from src.im.engine.supervisor import Supervisor


class FakeLeaseStore:
    """记录调用的租约替身（无 _held 私有属性可探测）。"""

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
        return []

    def list_expired_or_unheld(self, cursor="", limit=100):
        return []


class FakeChannel(Channel):
    def __init__(self, cfg: ChannelConfig):
        self.cfg = cfg

    def type(self) -> Type:
        return Type.FEISHU

    async def connect(self, ctx: CancelContext) -> None:
        raise PermanentChannelError("invalid credential")

    async def disconnect(self) -> None:
        return None

    async def send(self, out: OutboundMessage) -> SendResult:
        return SendResult(ok=False)


def _supervisor(leases):
    registry = Registry()
    registry.register(Type.FEISHU, FakeChannel)

    async def provider():
        return [{
            "installation_id": "app-1",
            "channel_type": "feishu",
            "scope": "user",
            "config": {"app_id": "app-1", "app_secret": "x"},
        }]

    return Supervisor(
        registry=registry, instance_id="r1", lease_store=leases,
        installation_provider=provider,
    )


def test_lease_acquire_uses_lease_key_contract():
    leases = FakeLeaseStore()
    sup = _supervisor(leases)
    sup._running = True

    async def scenario():
        await sup._scan_once()
        await asyncio.sleep(0.05)  # 允许 _run 任务走到 PermanentChannelError

    asyncio.run(scenario())
    expected_key = lease_key("app-1", "feishu")
    assert leases.acquired == [expected_key]         # 获取走 lease_key()
    assert leases.released == [(expected_key, f"tok:{expected_key}")]  # 释放同键 + 自持 token


def test_graceful_yield_releases_lease():
    """环让渡（不再归属）→ 拆连接并按 lease_key 释放。"""
    leases = FakeLeaseStore()
    sup = _supervisor(leases)
    sup._running = True

    async def scenario():
        await sup._scan_once()
        await asyncio.sleep(0.05)
        sup.instances = ["other"]                    # 环迁移：不再归属
        sup._fingerprints.clear()                    # 允许重扫重建判定
        await sup._scan_once()

    asyncio.run(scenario())
    expected_key = lease_key("app-1", "feishu")
    assert leases.released == [(expected_key, f"tok:{expected_key}")]


class _RecordingChannel(Channel):
    """记录 cfg.type 的替身渠道（connect 立即返回，不进入退避循环）。"""

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


def test_channel_type_change_rebuilds_with_new_adapter():
    """安装行 channel_type 被纠正（如回填把 wechat 落成 feishu）时，
    config 指纹不变也必须拆旧重建，否则错适配器会一直挂着跑（2026-11-10 实障）。"""
    built: list = []
    registry = Registry()

    def factory(cfg: ChannelConfig):
        built.append(cfg.type)
        return _RecordingChannel(cfg)

    registry.register(Type.FEISHU, factory)
    registry.register(Type.WECHAT, factory)
    state = {"type": "feishu"}

    async def provider():
        return [{
            "installation_id": "inst-1",
            "channel_type": state["type"],
            "scope": "user",
            "config": {"app_id": "inst-1", "app_secret": "t"},
        }]

    sup = Supervisor(registry=registry, instance_id="r1",
                     lease_store=FakeLeaseStore(), installation_provider=provider)
    sup._running = True

    async def scenario():
        await sup._scan_once()
        await asyncio.sleep(0.02)
        assert sup._channels["inst-1"][0].cfg.type == Type.FEISHU
        state["type"] = "wechat"          # 只改类型，config 完全不变
        await sup._scan_once()
        await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert built == [Type.FEISHU, Type.WECHAT]
    assert sup._channels["inst-1"][0].cfg.type == Type.WECHAT


def test_fingerprint_ignores_runtime_cursor():
    """游标等运行期状态不进指纹：微信 ilink 每轮推进 cursor 都会写 config，
    若参与指纹就会每轮热重建渠道（channels 反复归零，2026-11-10 实障）。"""
    from src.im.engine.supervisor import fingerprint

    base = {"app_id": "a", "app_secret": "s", "cursor": "c1"}
    next_cursor = {"app_id": "a", "app_secret": "s", "cursor": "c2"}
    rotated = {"app_id": "a", "app_secret": "s2", "cursor": "c2"}
    assert fingerprint(base) == fingerprint(next_cursor)
    assert fingerprint(base) != fingerprint(rotated)
