"""钉钉身份/媒体翻译（Issue #936 P2，设计文档 §2.2 resolvers.py）。

附件两跳（P4 修正）：适配器只把 downloadCode 翻译成 MediaRef 引用；
下载（第一跳）经可注入的下载钩子在 worker 侧执行，理解（VLM，第二跳）
同样在 worker——绝不内联 handler 协程（test_media_vlm_not_in_handler 守卫）。
"""
from __future__ import annotations

from ...channel.message import InboundMessage


class DingtalkIdentityResolver:
    """senderStaffId → grape 用户（grape_t_channel_sender_identity 的钉钉视图，§5.1）。"""

    def __init__(self, lookup=None) -> None:
        self._lookup = lookup  # Callable[[staff_id], Optional[str user_id]]

    async def resolve(self, inst, msg: InboundMessage) -> str | None:
        if self._lookup is None:
            return None
        return self._lookup(msg.source.sender_id)


class DingtalkMediaResolver:
    """MediaRef 两跳解析：不下载、不调 VLM；download_hook 由 worker 侧注入。"""

    def __init__(self, download_hook=None) -> None:
        self._download_hook = download_hook  # Callable[[storage_key], Awaitable[None]]

    async def resolve_media(self, msg: InboundMessage) -> list:
        return list(msg.media_refs)

    async def download(self, storage_key: str) -> None:
        """第一跳（下载）：默认 no-op，由 worker 注入真实实现。"""
        if self._download_hook is not None:
            await self._download_hook(storage_key)


__all__ = ["DingtalkIdentityResolver", "DingtalkMediaResolver"]
