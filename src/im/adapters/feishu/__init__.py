"""飞书适配器（Issue #742 §2.2）。

依赖铁律（docstring 声明即契约）：
  1. 只 import im_channel，永不 import im_engine 内部；
  2. 平台语义（open_id/union_id/lark 卡片）只在本包出现；
  3. 核心不读 raw——本包负责把 raw 翻译成强类型信封。
"""
from .inbound import parsed_to_inbound, parse_card_action
from .outbound import FeishuSender
from .markdown import render_markdown

__all__ = ["parsed_to_inbound", "parse_card_action", "FeishuSender", "render_markdown"]
