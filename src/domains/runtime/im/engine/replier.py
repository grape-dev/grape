"""Replier —— 出站（Issue #742 §4.4；#1008 出站收敛）。Markdown 为通用语。

agent 输出 Markdown → 渠道 SendMarkdownCard / 纯文本自动二选一；
系统通知卡（绑定引导/错误/离线）走 Renderer 接口；
继承 A4/A7 纪律：fire-and-forget，不阻塞入站流水线。

Issue #1008 失败/降级语义：
  - 空路由（chat_id 与 msg_id 双空）→ 调用方（HandlerEgress）计 metrics.failed，
    结构化日志，不再静默丢弃（钉钉 deliver skipped 事故修复）；
  - MESSAGE_EDIT 缺失 → edit() 降级为追加消息（内容不丢，计 degraded）；
  - RICH_CARD 缺失 → 纯文本降级（markdown 剥离，计 degraded）。
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..channel.capability import Capability
from ..channel.message import InboundMessage, OutboundMessage, SendResult


class ChannelSender(Protocol):
    async def send(self, out: OutboundMessage) -> SendResult: ...
    def capabilities(self) -> Capability: ...


class Replier:
    """出站统一入口：文本/卡片降级 + 编辑降级 + 绑定引导卡。"""

    def __init__(self, sender: ChannelSender) -> None:
        self._sender = sender

    def caps(self) -> Capability:
        """渠道能力位（Issue #1008：降级决策的唯一依据，声明不协商）。"""
        try:
            return self._sender.capabilities()
        except Exception:
            return Capability.TEXT

    async def reply(self, out: OutboundMessage) -> SendResult:
        caps = self.caps()
        if out.text and not (caps & Capability.RICH_CARD):
            # 渠道无富卡能力 → 降级纯文本（markdown 符号由适配器剥离）
            out = OutboundMessage(
                chat_id=out.chat_id, text=_strip_markdown(out.text),
                message_type=out.message_type, thread_root=out.thread_root,
                reply_to=out.reply_to, ephemeral=out.ephemeral,
                metadata=dict(out.metadata or {}),
            )
            md = dict(out.metadata or {})
            md["degraded"] = "text_fallback"
            out.metadata = md
        return await self._sender.send(out)

    async def edit(self, out: OutboundMessage) -> SendResult:
        """原地更新意图（Issue #1008 §replier MESSAGE_EDIT 降级语义）。

        渠道有 MESSAGE_EDIT（飞书 token 原地更新）→ 透传 patch 意图；
        无 MESSAGE_EDIT（钉钉）→ 降级为追加发送一条新消息（内容不丢）。
        降级标记写进 metadata，由 HandlerEgress 计 degraded。
        """
        caps = self.caps()
        if not (caps & Capability.MESSAGE_EDIT):
            md = dict(out.metadata or {})
            md["degraded"] = "edit_append"
            out = OutboundMessage(
                chat_id=out.chat_id, text=out.text,
                message_type=out.message_type, thread_root=out.thread_root,
                reply_to=out.reply_to, ephemeral=out.ephemeral,
                metadata=md,
            )
            return await self.reply(out)
        return await self._sender.send(out)

    async def send_binding_card(self, msg: InboundMessage) -> None:
        """未绑定身份的引导卡（④ 后 ⑤ 判 unbound 时发出）。"""
        await self._sender.send(OutboundMessage(
            chat_id=msg.source.chat_id,
            text="请先绑定你的账号以使用 grape（回复 /绑定 完成绑定，绑定引导卡）。",
            ephemeral=True,
        ))


def _strip_markdown(text: str) -> str:
    import re
    stripped = re.sub(r"[*_`#>~\[\]]", "", text)
    return stripped


__all__ = ["Replier"]
