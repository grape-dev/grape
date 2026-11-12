"""Router —— 统一入站九步流水线（Issue #742 §4.1，P2 的解法）。

两个巨型 handler（process_chat_async / process_vod_async）的公共骨架
提取为一条有序流水线。顺序契约逐条测试（tests/im/test_router_pipeline.py）。

依赖铁律：只 import im_channel，不 import 适配器；核心（本模块及调用方）
永不读写 InboundMessage.raw——流水线内部状态走引擎侧 context（state dict），
平台载荷字典不承载引擎私有键。

九步：①平台路由 ②事件→安装 ③两阶段去重 ④群过滤先于身份
      ⑤发送者→用户 ⑤b grape 双轨路由 ⑥会话绑定 ⑦持久化+Mark 同事务
      ⑧命令（含 pending selection） ⑨防抖触发 agent。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from ..channel.channel import CancelContext
from ..channel.message import InboundMessage, OutboundMessage
from ..channel.types import ChatType
from .dedup import InboundDedup, Verdict
from .session import SessionBinding, binding_key, resolve_route

DEBOUNCE_WINDOW = 0.5  # 500ms 合并连发消息为一次 agent run（§4.1 防抖）

log = logging.getLogger(__name__)


async def _noop(*args, **kwargs) -> None:
    return None


def _no_command(text: str):
    return None


def _log_task_exception(task: "asyncio.Task") -> None:
    """agent_run 后台任务异常回调：避免失败静默（记日志）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("router agent_run task failed: %r", exc)


@dataclass
class Installation:
    """安装的引擎侧视图（§5.1 grape_t_channel_installation 行）。"""

    installation_id: str
    channel_type: str
    scope: str                    # 'user' | 'repo'
    owner_user_id: str = ""
    repo_key: str = ""
    agent_type: str = ""


@dataclass
class Identity:
    user_id: str = ""
    unbound: bool = True


@dataclass
class RouteDecisionView:
    repo_key: str = ""
    agent_type: str = "chat"


@dataclass
class _PendingRun:
    session_key: tuple
    messages: list = field(default_factory=list)
    handle: Optional[asyncio.TimerHandle] = None
    route: Optional[RouteDecisionView] = None


class Router:
    """流水线编排器。各步组件以协议注入（可测试性：全替身）。"""

    def __init__(
        self,
        *,
        installations: Callable[[InboundMessage], Awaitable[Optional[Installation]]],
        identity: Callable[[Installation, InboundMessage], Awaitable[Identity]],
        identity_bind: Optional[Callable[[Installation, InboundMessage], Awaitable[bool]]] = None,
        bindings,
        dedup: InboundDedup,
        replier,
        append_message: Callable[..., Awaitable[None]] = _noop,
        parse_command: Optional[Callable[[str], Optional[Any]]] = None,
        apply_command: Optional[Callable[..., Awaitable[Optional[Exception]]]] = None,
        agent_run: Optional[Callable[..., Awaitable[None]]] = None,
        add_reaction: Optional[Callable[[InboundMessage], Awaitable[None]]] = None,
        media_worker: Optional[Callable[..., Awaitable[None]]] = None,
        debounce_window: float = DEBOUNCE_WINDOW,
    ) -> None:
        self._installations = installations
        self._identity = identity
        self._identity_bind = identity_bind
        self._bindings = bindings
        self._dedup = dedup
        self._replier = replier
        self._append_message = append_message
        self._parse_command = parse_command or _no_command
        self._apply_command = apply_command
        self._agent_run = agent_run
        self._add_reaction = add_reaction
        self._media_worker = media_worker
        self._debounce_window = debounce_window
        self._pending: dict[tuple, _PendingRun] = {}
        self.calls: list = []  # 测试观察哨：记录关键步骤顺序

    async def handle(self, ctx: CancelContext, msg: InboundMessage) -> Optional[Exception]:
        """返回异常仅代表基础设施失败；产品性丢弃返回 None。"""
        state: dict = {}  # 引擎侧流水线 context：承载安装等内部状态（不进平台载荷字典）
        try:
            return await self._handle(ctx, msg, state)
        except Exception as e:  # 基础设施失败：release claim，重投递是仅剩机会
            inst = state.get("installation")
            if inst is not None:
                self._dedup.release(inst.installation_id, msg)
            return e

    async def _handle(self, ctx: CancelContext, msg: InboundMessage,
                      _state: Optional[dict] = None) -> Optional[Exception]:
        state = _state if _state is not None else {}
        inst = await self._installations(msg)      # ② 事件→安装
        if inst is None:
            self.calls.append(("no_installation", msg.event_id))
            return None
        state["installation"] = inst               # 引擎内部路由用（不进平台载荷字典）

        verdict = self._dedup.try_claim(inst.installation_id, msg)   # ③ 两阶段去重
        if verdict == Verdict.DROP:
            self.calls.append(("dedup_drop", msg.event_id))
            return None

        if msg.source.chat_type == ChatType.GROUP and not msg.addressed_to_bot:
            self.calls.append(("not_addressed", msg.event_id))       # ④ 群过滤先于身份
            self._dedup.mark(inst.installation_id, msg)
            return None

        identity = await self._identity(inst, msg)     # ⑤ 发送者→grape 用户
        if identity.unbound:
            # ⑤' 逃生口：显式绑定指令先于引导卡（否则未绑定用户连命令都发不进来）
            if self._identity_bind is not None and await self._identity_bind(inst, msg):
                self.calls.append(("identity_bound", msg.event_id))
                self._dedup.mark(inst.installation_id, msg)
                return None
            self.calls.append(("binding_card", msg.event_id))
            await self._replier.send_binding_card(msg)
            self._dedup.mark(inst.installation_id, msg)
            return None

        binding = self._bindings.get(inst.installation_id, binding_key(msg.source))  # ⑥前置读
        route = resolve_route(inst, binding, self._explicit_agent(msg))  # ⑤b grape 双轨路由

        session = await self._ensure_session(inst, msg, binding, route)  # ⑥ 会话绑定
        if msg.media_refs and self._media_worker:     # P4：媒体理解不阻塞流水线
            # P1：MediaWorkQueue.enqueue 非阻塞入队，VLM 理解在 worker 侧异步执行
            enqueue = getattr(self._media_worker, "enqueue", None)
            if enqueue is not None:
                enqueue(inst.installation_id, session.binding_key, msg.media_refs)
            self.calls.append(("media_enqueued", msg.event_id))

        await self._append_message(session, msg)      # ⑦ 持久化
        self._dedup.mark(inst.installation_id, msg)   # ⑦ Mark 同事务（见引擎层集成测试）

        command = self._parse_command(msg.command_text or msg.text)  # ⑧ 命令
        if command is not None:
            self.calls.append(("command", str(command)))
            if self._apply_command is not None:
                return await self._apply_command(command, inst, session, msg)
            return None

        if msg.skip_agent_run:
            return None
        if self._add_reaction:                        # OK reaction 在 agent 执行前
            await self._add_reaction(msg)
        await self._debounced_run(session, msg, route)  # ⑨ 防抖触发 agent
        return None

    def _explicit_agent(self, msg: InboundMessage) -> str:
        text = msg.command_text or msg.text
        if text.startswith("/"):
            head = text.split()[0][1:].lower()
            if head in ("vod", "chat", "issue", "coding", "qa", "sre"):
                return head
        return ""

    async def _ensure_session(self, inst: Installation, msg: InboundMessage,
                              binding, route) -> SessionBinding:
        if binding is None:
            binding = SessionBinding(
                installation_id=inst.installation_id,
                binding_key=binding_key(msg.source),
                chat_type=msg.source.chat_type.value,
                scope=inst.scope,
                repo_key=route.repo_key,
                agent_type=route.agent_type,
            )
            return self._bindings.upsert(binding)
        return binding

    async def _debounced_run(self, session: SessionBinding, msg: InboundMessage, route) -> None:
        key = (session.installation_id, session.binding_key)
        loop = asyncio.get_running_loop()
        pending = self._pending.get(key)
        if pending is None:
            pending = _PendingRun(session_key=key, route=route)
            self._pending[key] = pending
        pending.messages.append(msg)
        if pending.handle:
            pending.handle.cancel()
        pending.handle = loop.call_later(
            self._debounce_window,
            lambda: self._spawn_flush(loop, key),
        )

    def _spawn_flush(self, loop: asyncio.AbstractEventLoop, key: tuple) -> None:
        """flush 任务带异常回调：agent_run 失败不得静默（记日志）。"""
        task = loop.create_task(self._flush(key))
        task.add_done_callback(_log_task_exception)

    async def _flush(self, key: tuple) -> None:
        pending = self._pending.pop(key, None)
        if pending is None or self._agent_run is None:
            return
        await self._agent_run(pending.route, pending.messages)


__all__ = ["Router", "Installation", "Identity", "DEBOUNCE_WINDOW"]
