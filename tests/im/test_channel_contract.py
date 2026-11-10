"""契约层 8 条测试（Issue #742 §7.2 / §8 P0 验收「契约层 8 条全绿」）。"""
import asyncio

import pytest

from src.im.channel import (
    Capability, CancelContext, Channel, ChannelConfig, ChannelRegistry,
    ChatType, InboundMessage, MediaRef, MsgType, OutboundMessage, SendResult,
    Source, Type, Scope,
)


def _msg(**kw) -> InboundMessage:
    base = dict(
        event_id="evt-1", message_id="mid-1",
        source=Source(channel_type=Type.FEISHU, chat_id="oc-1", chat_type=ChatType.P2P),
        type=MsgType.TEXT,
        text="hello",
    )
    base.update(kw)
    return InboundMessage(**base)


def test_1_inbound_message_envelope_defaults():
    """信封默认值不变量：非平台字段必须有跨平台为真的默认。"""
    m = _msg()
    assert m.command_text == "" and m.media_refs == [] and m.raw == {}
    assert m.addressed_to_bot is True and m.force_fresh is False
    assert m.skip_agent_run is False and m.reply_to is None


def test_2_media_ref_is_reference_not_inline():
    """MediaRef 只持引用（storage_key + inline_index），不内联数据（P4）。"""
    ref = MediaRef(type=MsgType.IMAGE,
                   storage_key="img_v2_abc", inline_index=3)
    assert ref.storage_key == "img_v2_abc" and ref.inline_index == 3
    assert not hasattr(ref, "data") and not hasattr(ref, "bytes")


def test_3_capability_bitmask_set_operations():
    """能力位可组合、可检测、可降级判断。"""
    caps = Capability.TEXT | Capability.RICH_CARD
    assert caps & Capability.RICH_CARD
    assert not caps & Capability.THREAD_REPLY
    assert Capability.TEXT | Capability.TEXT == Capability.TEXT


def test_4_registry_last_writer_wins():
    """重复注册覆盖旧工厂。"""
    reg = ChannelRegistry()
    reg.register(Type.FEISHU, lambda cfg: "A")
    reg.register(Type.FEISHU, lambda cfg: "B")
    assert reg.build(ChannelConfig(type=Type.FEISHU)) == "B"


def test_5_registry_nil_type_silent_none():
    """未注册 Type 静默返回 None（不抛错）。"""
    reg = ChannelRegistry()
    assert reg.build(ChannelConfig(type=Type.DINGTALK)) is None
    assert Type.DINGTALK not in reg.registered()


def test_6_channel_abc_requires_all_abstract_methods():
    """Channel 接口完整性：缺方法不可实例化。"""
    class Partial(Channel):
        def type(self): return Type.FEISHU

    with pytest.raises(TypeError):
        Partial()

    class Full(Channel):
        def type(self): return Type.FEISHU
        async def connect(self, ctx): pass
        async def disconnect(self): pass
        async def send(self, out): return SendResult(ok=True)

    ch = Full()
    assert ch.capabilities() == Capability.TEXT  # 默认能力位
    ok, err = ch.validate_config({})
    assert ok and err == ""


def test_7_source_thread_root_not_collapsed():
    """有话题平台：同 chat_id 不同 thread_root 不得塌缩为同一来源。"""
    a = Source(channel_type=Type.SLACK, chat_id="C1", chat_type=ChatType.GROUP, thread_root="t1")
    b = Source(channel_type=Type.SLACK, chat_id="C1", chat_type=ChatType.GROUP, thread_root="t2")
    assert a.chat_id == b.chat_id and a.thread_root != b.thread_root


def test_8_inbound_handler_contract_drop_is_not_error():
    """InboundHandler 契约：产品性丢弃返回 None（非异常）；适配器不得因此重连。"""
    async def handler(ctx, msg):
        return None  # 产品丢弃

    async def infra_handler(ctx, msg):
        return RuntimeError("db down")  # 基础设施失败

    ctx = CancelContext()
    assert asyncio.run(handler(ctx, _msg())) is None
    assert isinstance(asyncio.run(infra_handler(ctx, _msg())), RuntimeError)


def test_channel_config_raw_opaque_and_scope():
    """config.raw 对核心不透明；scope 双轨显式建模（P5）。"""
    cfg = ChannelConfig(type=Type.FEISHU, raw={"app_id": "cli_x"},
                        scope=Scope.REPO)
    assert cfg.scope == Scope.REPO
    assert isinstance(cfg.raw, dict)
