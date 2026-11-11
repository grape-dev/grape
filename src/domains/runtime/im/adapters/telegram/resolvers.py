"""Telegram 身份/媒体解析 + 消息→会话台账（Issue #1613 §4 横切）。

媒体两跳（与企微/钉钉/Slack 同纪律）：
  第一跳「下载」在 **TelegramAdapterApi**（handler/worker 侧真正用到时）完成：
  MediaRef 只存 `file_id` 引用，下载时先 `getFile` 换 `file_path`，再走
  `https://api.telegram.org/file/bot<token>/<file_path>`；Bot API 下载上限 20MB。
  第二跳「理解」（VLM）仍由引擎 worker 侧负责，绝不内联 ack。

路由台账：`editMessageText` / `setMessageReaction` 只给 message_id，需要 chat_id，
入站时把 `message_id → chat_id` 记进 `TelegramRouteRegistry`，出站侧反查（有界 + TTL）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

CACHE_DEFAULT_TTL = 2 * 3600.0
DEFAULT_FETCH_TIMEOUT = 20.0
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024      # Bot API getFile 下载硬上限 20MB


class TelegramMediaError(RuntimeError):
    """媒体下载失败（显式失败，不返回空串冒充成功）。"""


class TelegramMediaCache:
    """进程内媒体缓存（下载后字节，按 file_id 取回），有界 + TTL。"""

    def __init__(self, ttl: float = CACHE_DEFAULT_TTL, max_entries: int = 256,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._map: dict[str, tuple[bytes, float]] = {}

    def put(self, key: str, data: bytes) -> None:
        if not key:
            return
        now = self._clock()
        if key not in self._map and len(self._map) >= self._max:
            oldest = min(self._map, key=lambda k: self._map[k][1])
            self._map.pop(oldest, None)
        self._map[key] = (bytes(data or b""), now)

    def get(self, key: str) -> bytes:
        entry = self._map.get(key or "")
        if entry is None:
            return b""
        data, at = entry
        if self._clock() - at > self._ttl:
            self._map.pop(key, None)
            return b""
        return data

    def __len__(self) -> int:
        return len(self._map)


class TelegramRouteRegistry:
    """message_id → chat_id（editMessageText / reaction 用），有界 + TTL。"""

    def __init__(self, ttl: float = CACHE_DEFAULT_TTL, max_entries: int = 4096,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._map: dict[str, tuple[str, float]] = {}

    def remember(self, message_id: str, chat_id: str) -> None:
        if not message_id or not chat_id:
            return
        now = self._clock()
        if message_id not in self._map and len(self._map) >= self._max:
            oldest = min(self._map, key=lambda k: self._map[k][1])
            self._map.pop(oldest, None)
        self._map[str(message_id)] = (str(chat_id), now)

    def channel_for(self, message_id: str) -> str:
        entry = self._map.get(str(message_id or ""))
        if entry is None:
            return ""
        chat_id, at = entry
        if self._clock() - at > self._ttl:
            self._map.pop(str(message_id), None)
            return ""
        return chat_id

    def __len__(self) -> int:
        return len(self._map)


class TelegramUserResolver:
    """sender_id → 显示名（入站已带 first_name，无需额外网络）。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def remember(self, user_id: str, name: str) -> None:
        if user_id and name:
            self._cache[str(user_id)] = str(name)

    def name(self, user_id: str) -> str:
        return self._cache.get(str(user_id or ""), "")


class TelegramMediaFetcher:
    """file_id → bytes：getFile 换 path → 下载，20MB 上限 + 进程内缓存。"""

    def __init__(self, client: Any, *,
                 max_bytes: int = MAX_DOWNLOAD_BYTES,
                 cache: TelegramMediaCache | None = None) -> None:
        self._client = client
        self._max_bytes = int(max_bytes)
        self._cache = cache if cache is not None else TelegramMediaCache()

    @property
    def cache(self) -> TelegramMediaCache:
        return self._cache

    async def fetch(self, file_id: str) -> bytes:
        key = str(file_id or "")
        if not key:
            return b""
        cached = self._cache.get(key)
        if cached:
            return cached
        try:
            info = await self._client.get_file(key)
        except Exception as e:
            raise TelegramMediaError(str(e)) from e
        file_path = str((info or {}).get("file_path") or "")
        file_size = int((info or {}).get("file_size") or 0)
        if file_size and file_size > self._max_bytes:
            raise TelegramMediaError(
                f"file too large: {file_size} > {self._max_bytes} bytes")
        if not file_path:
            raise TelegramMediaError("getFile returned no file_path")
        try:
            data = await self._client.download_file(file_path)
        except Exception as e:
            raise TelegramMediaError(str(e)) from e
        if len(data) > self._max_bytes:
            raise TelegramMediaError("downloaded file exceeds 20MB")
        self._cache.put(key, data)
        return data


__all__ = [
    "CACHE_DEFAULT_TTL",
    "MAX_DOWNLOAD_BYTES",
    "TelegramMediaCache",
    "TelegramMediaError",
    "TelegramMediaFetcher",
    "TelegramRouteRegistry",
    "TelegramUserResolver",
]