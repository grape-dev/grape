"""两阶段去重测试（Issue #742 §7.3 ③ / A3）。"""
import asyncio

from src.im.adapters.feishu.inbound import parsed_to_inbound
from src.im.channel import InboundMessage, Source, ChatType, MsgType, Type
from src.im.engine.dedup import InMemoryDedupStore, InboundDedup, Verdict


def _msg(mid: str) -> InboundMessage:
    return InboundMessage(
        event_id=f"evt-{mid}", message_id=mid,
        source=Source(channel_type=Type.FEISHU, chat_id="oc", chat_type=ChatType.P2P),
        type=MsgType.TEXT, text="hi",
    )


def test_first_claim_then_drop():
    dedup = InboundDedup(InMemoryDedupStore())
    assert dedup.try_claim("inst1", _msg("m1")) == Verdict.CLAIM
    assert dedup.try_claim("inst1", _msg("m1")) == Verdict.DROP  # 重投丢弃


def test_mark_prevents_reprocess():
    dedup = InboundDedup(InMemoryDedupStore())
    msg = _msg("m2")
    dedup.try_claim("inst1", msg)
    dedup.mark("inst1", msg)  # 与持久化同事务
    assert dedup.try_claim("inst1", _msg("m2")) == Verdict.DROP


def test_infra_error_releases_claim():
    """DB 异常 → claim release（非 mark）：重投递是仅剩机会。"""
    store = InMemoryDedupStore()
    dedup = InboundDedup(store)
    msg = _msg("m3")
    assert dedup.try_claim("inst1", msg) == Verdict.CLAIM
    dedup.release("inst1", msg)
    assert dedup.try_claim("inst1", _msg("m3")) == Verdict.CLAIM  # 重投可再处理


def test_seen_survives_restart():
    """A3：重启后（同 store 持久化）重投不重复消费。"""
    store = InMemoryDedupStore()  # 模拟 SQLite 持久层：新进程复用同 store
    dedup = InboundDedup(store)
    msg = _msg("m4")
    dedup.try_claim("inst1", msg)
    dedup.mark("inst1", msg)
    dedup_after_restart = InboundDedup(store)
    assert dedup_after_restart.try_claim("inst1", _msg("m4")) == Verdict.DROP


def test_installation_isolation():
    dedup = InboundDedup(InMemoryDedupStore())
    assert dedup.try_claim("instA", _msg("m5")) == Verdict.CLAIM
    assert dedup.try_claim("instB", _msg("m5")) == Verdict.CLAIM  # 安装级隔离
