"""信封定义（Issue #742 §3.2）——所有平台消息翻译成的统一格式。

边界规则：新字段进信封的标准 = 「换一个平台它仍然为真吗」。
平台私有语义一律留在 raw（核心禁读，§2.1 铁律 4）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .types import ChatType, MsgType, Type


@dataclass
class Source:
    """消息来源的强类型描述。"""

    channel_type: Type
    chat_id: str                    # BindingKey 的主体（§5.2）
    chat_type: ChatType
    thread_root: str = ""           # 有话题平台（Slack thread）的线程根；严禁与裸 chat_id 塌缩
    sender_id: str = ""             # 平台会话内用户 ID（如飞书 open_id）
    sender_stable_id: str = ""      # 平台稳定用户 ID（如飞书 union_id）


@dataclass
class MediaRef:
    """媒体引用（P4 修正）：适配器下载落存储，理解（VLM）在 worker 侧，不内联 handler 协程。"""

    type: MsgType
    storage_key: str                # image_key / file_key → 引用键
    inline_index: int = 0           # 附件失败不移位（multica 陷阱 7）


@dataclass
class ReplyCtx:
    """回复上下文（引用回复）。"""

    message_id: str = ""
    root_message_id: str = ""


@dataclass
class InboundMessage:
    """入站信封。grape 的 parsed dict（_parse_event）强类型化。"""

    event_id: str                   # 幂等键（飞书 DATA 帧 headers message_id）
    message_id: str                 # 幂等键
    source: Source
    type: MsgType
    text: str                       # @剥离后的正文
    command_text: str = ""          # 命令富化前原文（防 "/issue" 二次解释）
    media_refs: list[MediaRef] = field(default_factory=list)
    reply_to: Optional[ReplyCtx] = None
    addressed_to_bot: bool = True   # 群内 @ 裁决
    force_fresh: bool = False       # /clear /cleansession 等价物
    skip_agent_run: bool = False
    raw: dict[str, Any] = field(default_factory=dict)  # 平台原始载荷，核心禁读


@dataclass
class OutboundMessage:
    """出站信封。Markdown 为通用语（§4.4），渠道按能力二选一渲染。"""

    chat_id: str
    text: str = ""                  # markdown 优先；渠道无 markdown 能力时降纯文本
    message_type: MsgType = MsgType.TEXT
    thread_root: str = ""
    reply_to: Optional[ReplyCtx] = None
    ephemeral: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
