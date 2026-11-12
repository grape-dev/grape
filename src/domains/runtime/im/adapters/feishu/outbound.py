"""飞书出站 —— OutboundMessage → 飞书发送（Issue #742 §4.4；#1008 出站收敛）。

Markdown 为通用语：reply_card（SendMarkdownCard）与纯文本自动二选一。
⚠️ update_multi: true 坑（multica 陷阱）：流式/多段更新必须置 update_multi，
否则静默 no-op —— 写进注释 + 测试（tests/im/test_feishu_outbound.py）。

Issue #1008：metadata 携带的平台路由参数（msg_id/title/template/raw_card/
target_user/kind）仅在存在时透传给底层回调——回调契约保持向后兼容
（无 metadata 的调用与旧签名完全一致）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ...channel.capability import Capability
from ...channel.message import OutboundMessage, SendResult
from ...channel.types import Type

log = logging.getLogger(__name__)

# Issue #1095：CARD_ACTION —— 飞书卡片按钮回调可回收（钉钉批5 前不申报）；
# RICH_CARD 语义收紧为「能渲染 Card IR」（FeishuCardRenderer）。
FEISHU_CAPABILITIES = (Capability.TEXT | Capability.RICH_CARD | Capability.CARD_ACTION
                       | Capability.QUOTE_REPLY | Capability.ATTACHMENT
                       | Capability.MESSAGE_EDIT | Capability.REACTION)

# OutboundMessage.metadata → 底层回调关键字参数（存在才传，契约向后兼容）
_ROUTE_KEYS = ("msg_id", "title", "template", "raw_card", "target_user", "kind")


def _route_kwargs(out: OutboundMessage) -> dict:
    md = out.metadata or {}
    kw = {}
    for k in _ROUTE_KEYS:
        v = md.get(k)
        if v:
            kw[k] = v
    return kw


async def _maybe_await(value: Any) -> Any:
    """回调兼容同步/协程两种形态（钉钉 webhook 传输为 async）。"""
    if hasattr(value, "__await__"):
        return await value
    return value


class FeishuSender:
    """发送器：注入底层 reply 函数（reply_text / reply_card），便于测试替身。"""

    def __init__(
        self,
        reply_text: Callable[..., Any],
        reply_card: Callable[..., Any],
    ) -> None:
        self._reply_text = reply_text
        self._reply_card = reply_card

    def capabilities(self) -> Capability:
        return FEISHU_CAPABILITIES

    async def send(self, out: OutboundMessage) -> SendResult:
        try:
            kw = _route_kwargs(out)
            kind = (out.metadata or {}).get("kind") or ""
            if "raw_card" in kw or (_wants_card(out) and kind != "text"):
                # update_multi: true —— 卡片多段更新必须显式开启，否则静默 no-op
                text = out.text if "raw_card" not in kw else ""
                result = await _maybe_await(
                    self._reply_card(out.chat_id, text, update_multi=True, **kw))
            else:
                result = await _maybe_await(self._reply_text(out.chat_id, out.text, **kw))
        except Exception as e:
            # C2：失败在适配器层也留 warning（与钉钉/企微等 5 个渠道对齐；
            # 引擎层 egress 仍会计 metrics，这里补一条可 grep 的现场日志）
            log.warning("[feishu] send failed chat=%s kind=%s err=%r",
                        (out.chat_id or "")[:24], (out.metadata or {}).get("kind", ""), e)
            return SendResult(ok=False, error=str(e))
        return SendResult(ok=True, message_id=str(result or ""))


def _wants_card(out: OutboundMessage) -> bool:
    """markdown 富文本（标题/列表/代码块/表格）走卡片，否则纯文本。"""
    text = out.text or ""
    return any(marker in text for marker in ("#", "- ", "* ", "```", "|"))


__all__ = ["FeishuSender", "FEISHU_CAPABILITIES"]
