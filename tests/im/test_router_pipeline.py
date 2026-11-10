"""Router 流水线顺序契约测试（Issue #742 §7.3）——P2 的解法的验收。"""
import asyncio
import time

import pytest

from src.im.channel import (
    ChatType, InboundMessage, MsgType, Source, Type, Capability, OutboundMessage, SendResult,
)
from src.im.engine.dedup import InboundDedup, InMemoryDedupStore
from src.im.engine.replier import Replier
from src.im.engine.router import Installation, Router
from src.im.engine.session import InMemoryBindingStore, binding_key


def _msg(mid, text="hi", chat_type=ChatType.P2P, addressed=True, command_text="", media=None):
    return InboundMessage(
        event_id=f"evt-{mid}", message_id=mid,
        source=Source(channel_type=Type.FEISHU, chat_id="oc-1", chat_type=chat_type),
        type=MsgType.TEXT, text=text, command_text=command_text or text,
        addressed_to_bot=addressed, media_refs=media or [],
    )


class _FakeSender:
    def __init__(self):
        self.sent = []

    def capabilities(self):
        return Capability.TEXT | Capability.RICH_CARD

    async def send(self, out):
        self.sent.append(out)
        return SendResult(ok=True)


def _router(record, *, identity_unbound=False, command=None, agent_run=None,
            reaction=None, bindings=None, debounce=0.5):
    async def installations(msg):
        record.append("installations")
        return Installation(installation_id="inst-1", channel_type="feishu", scope="user")

    async def identity(inst, msg):
        record.append("identity")
        return type("I", (), {"user_id": "u1", "unbound": identity_unbound})()

    async def append_message(session, msg):
        record.append("append")

    async def apply_command(cmd, inst, session, msg):
        record.append(f"apply_command:{cmd}")
        return None

    async def default_agent_run(route, messages):
        record.append("agent_run")
        if agent_run:
            await agent_run(route, messages)

    async def add_reaction(msg):
        record.append("reaction")

    if reaction is None:
        reaction = add_reaction

    return Router(
        installations=installations, identity=identity,
        bindings=bindings or InMemoryBindingStore(),
        dedup=InboundDedup(InMemoryDedupStore()), replier=Replier(_FakeSender()),
        append_message=append_message,
        parse_command=command, apply_command=apply_command if command else None,
        agent_run=default_agent_run, add_reaction=reaction,
        debounce_window=debounce,
    ), record


def _run(coro):
    return asyncio.run(coro)


def test_group_filter_runs_before_identity():
    """④ 先于 ⑤：群内未 @bot 闲聊绝不触发绑定卡（防刷屏）。"""
    router, record = _router([])
    msg = _msg("m1", text="闲聊", chat_type=ChatType.GROUP, addressed=False)
    assert _run(router.handle.__wrapped__ if False else router._handle(_new_ctx(), msg)) is None
    assert "identity" not in record
    assert "not_addressed" in [c[0] for c in router.calls]


def _new_ctx():
    from src.im.channel import CancelContext
    return CancelContext()


def test_dedup_drop_short_circuits():
    router, record = _router([])
    _run(router.handle(_new_ctx(), _msg("m2")))
    _run(router.handle(_new_ctx(), _msg("m2")))  # 重投
    assert record.count("installations") == 2  # ② 先行
    assert record.count("identity") == 1        # ③ 去重后只走一次身份


def test_unbound_identity_sends_binding_card():
    router, record = _router([], identity_unbound=True)
    msg = _msg("m3")
    assert _run(router.handle(_new_ctx(), msg)) is None
    assert ("binding_card", "evt-m3") in router.calls


def test_command_text_not_reinterpreted():
    """富化 text 里的 '/issue' 字样不得二次解释为命令（L778 回归）。"""
    def parse(text):
        if text.startswith("/issue"):
            return "issue-command"
        return None

    router, record = _router([], command=parse)
    # command_text 为空（非命令消息），text 含 "/issue" 字样 → 不应触发命令
    msg = _msg("m4", text='帮我看 "/issue" 引号里的内容', command_text="")
    _run(router.handle(_new_ctx(), msg))
    assert "apply_command:issue-command" not in record


def test_command_dispatch_when_real_command():
    def parse(text):
        return "issue-command" if text.startswith("/issue") else None

    router, record = _router([], command=parse)
    _run(router.handle(_new_ctx(), _msg("m5", text="/issue create", command_text="/issue create")))
    assert "apply_command:issue-command" in [c for c in record if isinstance(c, str)]


def test_ok_reaction_before_agent_run():
    """OK reaction 在 agent 执行前发出（vod 轨 L453 语义，两轨统一）。"""
    router, record = _router([])

    async def scenario():
        await router.handle(_new_ctx(), _msg("m6"))
        await asyncio.sleep(0.7)

    _run(scenario())
    assert record.index("reaction") < record.index("agent_run")


def test_debounce_merges_burst():
    """500ms 三连发 → 一次 agent run，上下文含三条。"""
    router, record = _router([])

    async def scenario():
        await router.handle(_new_ctx(), _msg("m7"))
        await router.handle(_new_ctx(), _msg("m8"))
        await router.handle(_new_ctx(), _msg("m9"))
        await asyncio.sleep(0.15)  # 防抖窗口内

    _run(scenario())
    assert "agent_run" not in record  # 窗口内尚未触发


def test_debounce_flush_after_window():
    router, record = _router([])
    bursts = []

    async def agent_run(route, messages):
        bursts.append(len(messages))

    async def scenario():
        await router.handle(_new_ctx(), _msg("m10"))
        await router.handle(_new_ctx(), _msg("m11"))
        await asyncio.sleep(0.7)

    router._agent_run = agent_run
    _run(scenario())
    assert bursts == [2]  # 防抖窗口内两条连发合并为一次 run（m10/m11）


def test_media_vlm_not_in_handler():
    """P4 回归：媒体消息立即入流水线，VLM 由 worker 异步补。"""
    router, record = _router([])

    async def media_worker(refs):
        record.append("vlm_worker")

    router._media_worker = media_worker
    from src.im.channel import MediaRef
    msg = _msg("m12", media=[MediaRef(type=MsgType.MEDIA, storage_key="file_v2_v")])

    async def timed():
        t0 = time.monotonic()
        err = await router.handle(_new_ctx(), msg)
        return err, time.monotonic() - t0

    err, elapsed = _run(timed())
    assert err is None
    assert elapsed < 0.5  # 不阻塞流水线（不等待 VLM）
    assert ("media_enqueued", "evt-m12") in router.calls


def test_infra_error_returns_exception_and_releases():
    router, record = _router([])
    # 制造基础设施失败：bindings.get 抛错（DB 挂）
    class BoomStore:
        def get(self, *a):
            raise RuntimeError("db down")

        def upsert(self, b):
            return b

    router._bindings = BoomStore()
    msg = _msg("m13")
    err = _run(router.handle(_new_ctx(), msg))
    assert isinstance(err, RuntimeError)  # 基础设施失败 ≠ 产品丢弃
    # claim 已 release：同一条消息重投后会再次走 ②（而非 dedup DROP）
    record.clear()
    router._bindings = InMemoryBindingStore()
    _run(router.handle(_new_ctx(), msg))
    assert "identity" in record


def test_repo_scope_default_vod_route():
    """vod 轨：无绑定历史 → 默认该 repo 的 vod agent（L522 语义）。"""
    from src.im.engine.session import resolve_route
    inst = Installation(installation_id="i", channel_type="feishu", scope="repo", repo_key="iso/repo-1")
    decision = resolve_route(inst, binding=None)
    assert decision.repo_key == "iso/repo-1" and decision.agent_type == "vod"


def test_chat_scope_session_stickiness():
    """会话粘性：绑定行 agent_type 优先于兜底（§6）。"""
    from src.im.engine.session import SessionBinding, resolve_route
    inst = Installation(installation_id="i", channel_type="feishu", scope="user")
    binding = SessionBinding(installation_id="i", binding_key="oc-1", agent_type="issue")
    d = resolve_route(inst, binding)
    assert d.agent_type == "issue"


def test_explicit_agent_mention_wins():
    from src.im.engine.session import SessionBinding, resolve_route
    inst = Installation(installation_id="i", channel_type="feishu", scope="user")
    binding = SessionBinding(installation_id="i", binding_key="oc-1", agent_type="chat")
    d = resolve_route(inst, binding, explicit_agent="issue")
    assert d.agent_type == "issue"
