"""微信渠道路由/身份/typing/媒体解析（Issue #1728）。

关键解析（与 Telegram 同款思路）：
  - `context_token` 必须随入站消息缓存、出站回传（微信按会话上下文投递）；
  - `chat_id` 就是 `from_user_id`（形如 `xxx@im.wechat`），回复直接发回该用户；
  - typing_ticket 按用户缓存（TTL 1h），由 `getconfig` 获取后 `sendtyping` 使用；
  - 入站媒体 `storage_key` 是媒体描述 JSON，媒体下载在 AdapterApi 两跳完成。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from .media import WechatMediaUploader
from .protocol import as_dict, as_str


class WechatRouteRegistry:
    """chat_id → context_token / message_id → chat_id 台账（进程内有界）。"""

    def __init__(self, max_entries: int = 4000) -> None:
        self._max = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._context: dict[str, str] = {}
        self._routes: dict[str, str] = {}

    def remember(self, chat_id: str, context_token: str = "",
                 message_id: str = "") -> None:
        chat = as_str(chat_id)
        if not chat:
            return
        with self._lock:
            token = as_str(context_token)
            if token:
                self._context[chat] = token
            if message_id:
                self._routes[as_str(message_id)] = chat
            self._trim()

    def _trim(self) -> None:
        while len(self._routes) > self._max:
            self._routes.pop(next(iter(self._routes)), None)
        while len(self._context) > self._max:
            self._context.pop(next(iter(self._context)), None)

    def context_token(self, chat_id: str) -> str:
        with self._lock:
            return self._context.get(as_str(chat_id), "")

    def channel_for(self, message_id: str) -> str:
        with self._lock:
            return self._routes.get(as_str(message_id), "")


class WechatUserResolver:
    """平台用户显示名缓存（getconfig 未给名字时沿用最后已知值）。"""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def remember(self, user_id: str, name: str) -> None:
        if as_str(user_id) and as_str(name):
            self._names[as_str(user_id)] = as_str(name)

    def name(self, user_id: str) -> str:
        return self._names.get(as_str(user_id), "")


class WechatTypingCache:
    """typing_ticket 缓存（按用户，TTL 1h）。"""

    def __init__(self, ttl: float = 3600.0, now: Callable[[], float] = time.monotonic) -> None:
        self._ttl = float(ttl)
        self._now = now
        self._tickets: dict[str, tuple[str, float]] = {}

    def put(self, user_id: str, ticket: str) -> None:
        if as_str(user_id) and as_str(ticket):
            self._tickets[as_str(user_id)] = (as_str(ticket), self._now())

    def get(self, user_id: str) -> str:
        hit = self._tickets.get(as_str(user_id))
        if hit is None:
            return ""
        ticket, ts = hit
        if self._now() - ts > self._ttl:
            self._tickets.pop(as_str(user_id), None)
            return ""
        return ticket


def encode_media_key(media: dict) -> str:
    """入站媒体 → storage_key（JSON 描述，AdapterApi 下载时还原）。"""
    data = {k: media.get(k) for k in ("url", "aes_key", "filename", "type", "msg_id")}
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def decode_media_key(key: str) -> dict:
    try:
        data = json.loads(as_str(key))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class WechatMediaFetcher:
    """AdapterApi 两跳媒体下载：解析 storage_key → CDN 下载 → AES 解密。"""

    def __init__(self, client, uploader: WechatMediaUploader | None = None) -> None:
        self._uploader = uploader or WechatMediaUploader(client)

    async def fetch(self, storage_key: str) -> bytes:
        media = decode_media_key(storage_key)
        url = as_str(media.get("url"))
        aes_key = as_str(media.get("aes_key"))
        if not url or not aes_key:
            return b""
        return await self._uploader.download(url, aes_key)

    async def fetch_media(self, media: dict) -> bytes:
        return await self.fetch(encode_media_key(as_dict(media)))


__all__ = [
    "WechatMediaFetcher",
    "WechatRouteRegistry",
    "WechatTypingCache",
    "WechatUserResolver",
    "decode_media_key",
    "encode_media_key",
]