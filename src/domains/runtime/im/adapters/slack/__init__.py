"""Slack 适配器（Issue #1309，设计文档 docs/weekly-09/im-slack-adapter-design.md）。

依赖铁律（docstring 声明即契约）：
  1. 只 import im_channel，永不 import im_engine 内部；
  2. 平台语义（Socket Mode envelope / Block Kit / mrkdwn / 38000 字符 / xoxb+xapp）
     只在本包出现；
  3. 核心不读 raw——本包负责把 raw 翻译成强类型信封。
"""
from .cards import MAX_ACTION_ELEMENTS, MAX_BLOCKS, MAX_SECTION_CHARS
from .channel import ACK_DEADLINE_SECS, SlackChannel
from .inbound import block_actions_to_inbound, event_to_inbound
from .markdown import SLACK_TEXT_LIMIT, format_mrkdwn, split_text
from .outbound import SLACK_CAPABILITIES, SlackAdapterApi, SlackClient, SlackSender

__all__ = [
    "ACK_DEADLINE_SECS",
    "MAX_ACTION_ELEMENTS",
    "MAX_BLOCKS",
    "MAX_SECTION_CHARS",
    "SLACK_CAPABILITIES",
    "SLACK_TEXT_LIMIT",
    "SlackAdapterApi",
    "SlackChannel",
    "SlackClient",
    "SlackSender",
    "block_actions_to_inbound",
    "event_to_inbound",
    "format_mrkdwn",
    "split_text",
]
