"""钉钉适配器（Issue #936 P2，设计文档 §2.2）。

依赖铁律（docstring 声明即契约）：
  1. 只 import im_channel，永不 import im_engine 内部；
  2. 平台语义（conversationId/senderStaffId/downloadCode/markdown 15000 字节）
     只在本包出现；
  3. 核心不读 raw——本包负责把 raw 翻译成强类型信封。
"""
from .inbound import build_ack_frame, event_to_inbound
from .markdown import DINGTALK_MARKDOWN_LIMIT, split_markdown
from .outbound import DingtalkSender

__all__ = [
    "DINGTALK_MARKDOWN_LIMIT",
    "DingtalkSender",
    "build_ack_frame",
    "event_to_inbound",
    "split_markdown",
]
