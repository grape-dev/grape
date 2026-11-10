"""飞书身份/媒体翻译（Issue #742 §2.2 resolvers.py）。

P4 修正：媒体（image_key/file_key）只翻译为 MediaRef 引用，
下载在适配器、理解（VLM）在 worker 侧 —— 不内联 handler 协程。
"""
from __future__ import annotations

from typing import Optional

from ...channel.message import InboundMessage, Source


class FeishuIdentityResolver:
    """sender_open_id → grape 用户（external_identities 的 IM 视图，§5.1）。"""

    def __init__(self, lookup=None) -> None:
        self._lookup = lookup  # Callable[[open_id], Optional[str user_id]]

    async def resolve(self, inst, msg: InboundMessage) -> Optional[str]:
        if self._lookup is None:
            return None
        return self._lookup(msg.source.sender_id)


class FeishuMediaResolver:
    """MediaRef 翻译：不下载、不调用 VLM（P4 回归守卫 test_media_vlm_not_in_handler）。"""

    async def resolve_media(self, msg: InboundMessage) -> list:
        return list(msg.media_refs)


__all__ = ["FeishuIdentityResolver", "FeishuMediaResolver"]
