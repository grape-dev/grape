"""Slack 身份/媒体解析 + 消息→频道台账（Issue #1309 §4.5）。

媒体两跳（与企微/钉钉同纪律）：
  第一跳「下载」在 **SlackAdapterApi**（handler/worker 侧真正用到时）完成 ——
  `url_private_download` 必须带 `Authorization: Bearer xoxb` 才能读，URL 本身长期有效，
  因此 MediaRef 只存引用（storage_key=URL），下载后落进程内缓存（同 URL 二次读取不再打网络）。
  第二跳「理解」（VLM）仍由引擎 worker 侧负责，绝不内联 handler 协程。

身份：
  `users.info` 拿显示名（Tier 4，进程内缓存 + TTL），身份映射本身走引擎既有
  `grape_t_channel_sender_identity`（user 轨 `/bind me` 自助绑定），本模块不重复实现。

路由台账：
  `chat.update` / `reactions.add` / `update_message` 只给 message ts、没有 channel，
  入站时把 `ts → channel` 记进 `SlackRouteRegistry`，出站侧按 ts 反查（有界 + TTL）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

CACHE_DEFAULT_TTL = 2 * 3600.0     # 媒体/路由台账默认保留窗口
USER_CACHE_TTL = 3600.0            # users.info 显示名缓存（Tier 4 限流保护）；
                                   # 取值理由：显示名极少变化 + users.info 属 Tier 4，
                                   # 1h 内复用可把该接口调用压到「每会话每小时一次」量级。
DEFAULT_FETCH_TIMEOUT = 15.0       # 单次媒体抓取上限（设计余量，非实测）：常规文件下载在秒级，
                                   # 留大余量仍低于引擎 30s 扫描窗口，超时即降级不阻塞入站。


class SlackMediaError(RuntimeError):
    """媒体下载失败（显式失败，不返回空串冒充成功）。"""


class SlackMediaCache:
    """进程内媒体缓存（下载后字节，按 storage_key 取回），有界 + TTL。"""

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


class SlackRouteRegistry:
    """message ts → channel（chat.update / reactions / 编辑用），有界 + TTL。"""

    def __init__(self, ttl: float = CACHE_DEFAULT_TTL, max_entries: int = 4096,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._map: dict[str, tuple[str, float]] = {}

    def remember(self, ts: str, channel: str) -> None:
        if not ts or not channel:
            return
        now = self._clock()
        if ts not in self._map and len(self._map) >= self._max:
            oldest = min(self._map, key=lambda k: self._map[k][1])
            self._map.pop(oldest, None)
        self._map[ts] = (channel, now)

    def channel_for(self, ts: str) -> str:
        entry = self._map.get(ts or "")
        if entry is None:
            return ""
        channel, at = entry
        if self._clock() - at > self._ttl:
            self._map.pop(ts, None)
            return ""
        return channel

    def __len__(self) -> int:
        return len(self._map)


class SlackUserResolver:
    """user_id → 显示名（users.info，进程内缓存；失败回落空串，不影响主链路）。"""

    def __init__(self, fetch: Callable[[str], Awaitable[dict]] | None = None, *,
                 ttl: float = USER_CACHE_TTL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[str, float]] = {}

    async def name(self, user_id: str) -> str:
        if not user_id or self._fetch is None:
            return ""
        cached = self._cache.get(user_id)
        if cached and self._clock() - cached[1] <= self._ttl:
            return cached[0]
        try:
            body = await self._fetch(user_id)
        except Exception as e:
            log.debug("[slack] users.info failed (user=%s): %r", user_id[:12], e)
            return ""
        user = (body or {}).get("user") if isinstance(body, dict) else None
        profile = (user or {}).get("profile") if isinstance(user, dict) else None
        name = ""
        if isinstance(profile, dict):
            name = str(profile.get("display_name") or profile.get("real_name") or "")
        if not name and isinstance(user, dict):
            name = str(user.get("name") or "")
        self._cache[user_id] = (name, self._clock())
        return name


async def _default_download(url: str, token: str,
                            timeout: float = DEFAULT_FETCH_TIMEOUT) -> bytes:
    """url_private_download 下载（必须带 xoxb，否则 302 到登录页/HTML）。"""
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise SlackMediaError(f"media download HTTP {resp.status_code}")
    content_type = str(resp.headers.get("content-type") or "")
    if content_type.startswith("text/html"):
        # 未授权时 Slack 会返回登录页 HTML（不是文件）——显式失败，不把网页当图片
        raise SlackMediaError("media download returned html (token/scope missing)")
    return resp.content


class SlackMediaFetcher:
    """媒体下载 + 缓存（HTTP 与缓存均可注入，测试不触网）。"""

    def __init__(self, token: str, *,
                 http_get: Callable[[str, str], Awaitable[bytes]] | None = None,
                 cache: SlackMediaCache | None = None) -> None:
        self._token = str(token or "")
        self._http_get = http_get or _default_download
        self._cache = cache if cache is not None else SlackMediaCache()

    @property
    def cache(self) -> SlackMediaCache:
        return self._cache

    async def fetch(self, url: str) -> bytes:
        key = str(url or "")
        if not key:
            return b""
        cached = self._cache.get(key)
        if cached:
            return cached
        try:
            data = await self._http_get(key, self._token)
        except Exception as e:
            log.warning("[slack] media fetch failed: %r", e)
            raise SlackMediaError(str(e)) from e
        data = bytes(data or b"")
        self._cache.put(key, data)
        return data


__all__ = [
    "CACHE_DEFAULT_TTL",
    "USER_CACHE_TTL",
    "SlackMediaCache",
    "SlackMediaError",
    "SlackMediaFetcher",
    "SlackRouteRegistry",
    "SlackUserResolver",
]
