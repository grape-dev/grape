"""企业微信（WeCom）适配器（Issue #1254）。

依赖铁律（docstring 声明即契约，与 feishu / dingtalk 同款）：
  1. 只 import im_channel，永不 import im_engine 内部；
  2. 平台语义（chattype=string、aeskey、20480 字节、template_card、req_id 信封）
     只在本包出现；
  3. 核心不读 raw——本包负责把 raw 翻译成强类型信封。

长连接协议事实（官方文档 /document/path/101463，2026-10 版）：
  - 端点 wss://openws.work.weixin.qq.com，握手后发 aibot_subscribe{bot_id,secret}，
    10s 内须收 errcode=0 ack；心跳每 30s 一次 ping；同一机器人只允许一条有效连接
    （新连接踢掉旧连接并下发 disconnected_event）。
  - 入站 cmd：aibot_msg_callback / aibot_event_callback（body 可能是 JSON 字符串）。
  - 出站 cmd：aibot_respond_msg（回复，透传回调 req_id）、aibot_send_msg
    （主动推送，**仅接受 markdown / template_card**）、aibot_respond_update_msg
    （更新模板卡片）、aibot_upload_media_init/chunk/finish（分片上传换 media_id）。
"""
from .cards import (
    CardLedger,
    build_template_card,
    decode_action_key,
    default_card_ledger,
)
from .inbound import (
    CMD_EVENT_CALLBACK,
    CMD_MSG_CALLBACK,
    WECOM_WS_URL,
    build_ping_frame,
    build_subscribe_frame,
    event_to_inbound,
    media_items,
    parse_frame,
    template_card_event_to_inbound,
)
from .markdown import WECOM_MARKDOWN_LIMIT, render_markdown, split_markdown
from .outbound import (
    WECOM_CAPABILITIES,
    WecomAdapterApi,
    WecomReplyRegistry,
    WecomSender,
    WecomTransport,
    default_reply_registry,
)

__all__ = [
    "CMD_EVENT_CALLBACK",
    "CMD_MSG_CALLBACK",
    "WECOM_CAPABILITIES",
    "WECOM_MARKDOWN_LIMIT",
    "WECOM_WS_URL",
    "CardLedger",
    "WecomAdapterApi",
    "WecomReplyRegistry",
    "WecomSender",
    "WecomTransport",
    "build_ping_frame",
    "build_subscribe_frame",
    "build_template_card",
    "decode_action_key",
    "default_card_ledger",
    "default_reply_registry",
    "event_to_inbound",
    "media_items",
    "parse_frame",
    "render_markdown",
    "split_markdown",
    "template_card_event_to_inbound",
]