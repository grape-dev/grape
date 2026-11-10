"""Telegram 适配器（Issue #1613，设计文档 docs/weekly-09/im-telegram-adapter-design.md）。

依赖铁律（docstring 声明即契约）：
  1. 只 import im_channel，永不 import im_engine 内部；
  2. 平台语义（getUpdates / InlineKeyboardMarkup / callback_data 64B / HTML parse_mode）
     只在本包出现；
  3. 核心不读 raw——本包负责把 raw 翻译成强类型信封。
"""
from .cards import (
    MAX_BUTTONS,
    MAX_BUTTONS_PER_ROW,
    MAX_CALLBACK_BYTES,
    TelegramCardLedger,
    decode_callback_data,
    default_ledger,
    encode_callback_data,
)
from .channel import TelegramChannel
from .inbound import callback_query_to_inbound, update_to_inbound
from .markdown import TELEGRAM_TEXT_LIMIT, format_html, split_text
from .outbound import (
    ALLOWED_UPDATES,
    TELEGRAM_CAPABILITIES,
    TelegramAdapterApi,
    TelegramApiError,
    TelegramClient,
    TelegramSender,
)

__all__ = [
    "ALLOWED_UPDATES",
    "MAX_BUTTONS",
    "MAX_BUTTONS_PER_ROW",
    "MAX_CALLBACK_BYTES",
    "TELEGRAM_CAPABILITIES",
    "TELEGRAM_TEXT_LIMIT",
    "TelegramAdapterApi",
    "TelegramApiError",
    "TelegramCardLedger",
    "TelegramChannel",
    "TelegramClient",
    "TelegramSender",
    "callback_query_to_inbound",
    "decode_callback_data",
    "default_ledger",
    "encode_callback_data",
    "format_html",
    "split_text",
    "update_to_inbound",
]