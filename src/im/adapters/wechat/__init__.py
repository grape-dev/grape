"""微信（ilink Bot 通道 / ClawBot）适配器（Issue #1728）。

依赖铁律（docstring 声明即契约）：
  1. 只 import `im_channel`，永不 import `im_engine` 内部；
  2. 平台语义（ilink 端点 / Bearer 鉴权 / AES-128-ECB / getupdates 游标）
     只在本包出现；
  3. 核心不读 raw —— 本包负责把 raw 翻译成强类型信封。

三段契约：
  - 接入：`WechatChannel.connect()` 长轮询 getupdates（游标持久化）；
  - 发送：`WechatSender`（4000 分块 + client_id 幂等 + context_token 回传 + 媒体上传）；
  - 卡片：`channel/renderers/wechat.py` 纯文本「标题 + 正文 + 编号」降级。
"""
from .channel import WechatChannel
from .inbound import dedup_key, message_to_inbound, messages_to_inbound
from .login import WechatLoginManager, default_login_manager
from .media import (
    WechatMediaUploader,
    aes_decrypt,
    aes_encrypt,
    media_type_for_name,
    normalize_aes_key,
    normalize_aes_key_b64,
)
from .outbound import WECHAT_CAPABILITIES, WechatAdapterApi, WechatSender, split_text
from .protocol import (
    DEFAULT_BASE_URL,
    RET_SESSION_PAUSED,
    SESSION_PAUSE_SECONDS,
    TEXT_LIMIT,
    WechatApiError,
    WechatClient,
)
from .resolvers import (
    WechatMediaFetcher,
    WechatRouteRegistry,
    WechatTypingCache,
    WechatUserResolver,
    decode_media_key,
    encode_media_key,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "RET_SESSION_PAUSED",
    "SESSION_PAUSE_SECONDS",
    "TEXT_LIMIT",
    "WECHAT_CAPABILITIES",
    "WechatAdapterApi",
    "WechatApiError",
    "WechatChannel",
    "WechatClient",
    "WechatLoginManager",
    "WechatMediaFetcher",
    "WechatMediaUploader",
    "WechatRouteRegistry",
    "WechatSender",
    "WechatTypingCache",
    "WechatUserResolver",
    "aes_decrypt",
    "aes_encrypt",
    "decode_media_key",
    "dedup_key",
    "default_login_manager",
    "encode_media_key",
    "media_type_for_name",
    "message_to_inbound",
    "messages_to_inbound",
    "normalize_aes_key",
    "normalize_aes_key_b64",
    "split_text",
]