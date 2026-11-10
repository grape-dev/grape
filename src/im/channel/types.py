"""im_channel 契约层 —— 纯抽象，零平台依赖（Issue #742 §2.1 依赖铁律）。

本包禁止：
  1. import 任何平台 SDK（lark-oapi / dingtalk / slack-sdk ...）；
  2. import im_engine 或任何适配器。
只有枚举与值类型，可被任意层安全引用。
"""
from __future__ import annotations

import enum


class Type(str, enum.Enum):
    """渠道类型（= grape_t_channel_installation.channel_type 取值域）。"""

    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    SLACK = "slack"
    WECOM = "wecom"
    TELEGRAM = "telegram"
    WECHAT = "wechat"


class ChatType(str, enum.Enum):
    P2P = "p2p"
    GROUP = "group"


class Scope(str, enum.Enum):
    """grape 双轨安装作用域（§5.1）：user 轨 = chat_agent_session；repo 轨 = devops_agent_session。"""

    USER = "user"
    REPO = "repo"


class MsgType(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    MEDIA = "media"
    CARD_ACTION = "card_action"
    EVENT = "event"
    UNKNOWN = "unknown"


__all__ = ["Type", "ChatType", "Scope", "MsgType"]
