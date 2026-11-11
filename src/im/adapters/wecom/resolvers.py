"""企微身份/媒体解析（Issue #1254，对标 dingtalk/resolvers.py 的两跳纪律）。

媒体两跳（P4 修正，钉钉同款）：
  第一跳「下载」在**适配器收帧时**完成 —— 企微给的 COS 直链只有 5 分钟有效期，
  过期即 403，所以不能延后到 worker；下载后按 AES-256-CBC 解密（每条 URL 独立
  aeskey，PKCS#7 填充至 32 字节，IV=aeskey 前 16 字节）落内存缓存。
  第二跳「理解」（VLM）仍在 worker 侧，绝不内联 handler 协程。

aeskey 不落库：随帧携带、只在进程内用于解密，用完即弃（官方口径：每条 URL 一个 key）。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...channel.message import InboundMessage

log = logging.getLogger(__name__)

MEDIA_URL_TTL = 5 * 60.0          # 官方：COS 直链 5 分钟内有效
DEFAULT_FETCH_TIMEOUT = 15.0    # 单次媒体抓取上限：企微 COS 直链 5 分钟内有效，
                                # 15s 取 95 分位 3s 的 5x 余量，超时即降级为「下载失败」指标。
CACHE_DEFAULT_TTL = 2 * 3600.0    # handler/worker 拉取窗口


class WecomMediaError(RuntimeError):
    """媒体下载/解密失败（显式失败，不返回空串冒充成功）。"""


def decode_aeskey(aeskey: str) -> bytes:
    """aeskey → 32 字节密钥（base64 优先，回落 utf-8 截断，长度不足即失败）。"""
    raw = (aeskey or "").strip()
    if not raw:
        raise WecomMediaError("empty aeskey")
    for decoder in (base64.b64decode,):
        try:
            key = decoder(raw + "=" * (-len(raw) % 4))
            if len(key) >= 32:
                return key[:32]
        except Exception:
            pass
    try:
        key = raw.encode("utf-8")
    except Exception as e:  # pragma: no cover - str 必然可编码
        raise WecomMediaError(f"aeskey not decodable: {e!r}") from e
    if len(key) < 32:
        raise WecomMediaError(f"aeskey too short ({len(key)} bytes)")
    return key[:32]


def decrypt_media(data: bytes, aeskey: str) -> bytes:
    """AES-256-CBC 解密 + PKCS#7（32 字节块）去填充。"""
    if not data:
        raise WecomMediaError("empty media payload")
    key = decode_aeskey(aeskey)
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception as e:  # pragma: no cover - 依赖由 requirements 提供
        raise WecomMediaError(f"cryptography unavailable: {e!r}") from e
    if len(data) % 16:
        # CBC 输入必须是 16 字节整数倍（PKCS#7 至 32 字节仍按 16 分块对齐）
        raise WecomMediaError(f"ciphertext not block aligned ({len(data)} bytes)")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    if not plain:
        return b""
    pad = plain[-1]
    if 1 <= pad <= 32 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain


def media_storage_key(msg_id: str, index: int, url: str = "") -> str:
    return "wecom:" + hashlib.sha1(
        f"{msg_id}|{index}|{url}".encode("utf-8")).hexdigest()[:20]


class WecomMediaCache:
    """进程内媒体缓存（下载后字节，按 storage_key 取回），有界 + TTL。"""

    def __init__(self, ttl: float = CACHE_DEFAULT_TTL, max_entries: int = 512,
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


async def _default_http_get(url: str, timeout: float = DEFAULT_FETCH_TIMEOUT) -> bytes:
    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise WecomMediaError(f"media download HTTP {resp.status_code}")
    return resp.content


class WecomMediaResolver:
    """收帧即下载 + 解密 + 落缓存。httpx / 时钟 / 解密均可注入（测试不触网）。"""

    def __init__(self, *, http_get: Callable[[str], Awaitable[bytes]] | None = None,
                 cache: WecomMediaCache | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._http_get = http_get or _default_http_get
        self._cache = cache if cache is not None else WecomMediaCache(clock=clock)
        self._clock = clock
        self.downloaded = 0
        self.failed = 0

    @property
    def cache(self) -> WecomMediaCache:
        return self._cache

    async def fetch(self, items: list[Any], msg_id: str = "") -> list[str]:
        """媒体引用 → storage_key 列表（失败项留空串，**不移位**后续索引）。"""
        keys: list[str] = []
        for index, item in enumerate(items or []):
            url = str(getattr(item, "url", "") or "")
            aeskey = str(getattr(item, "aeskey", "") or "")
            key = media_storage_key(msg_id, index, url)
            try:
                data = await self._http_get(url)
                if not isinstance(data, (bytes, bytearray)):
                    raise WecomMediaError("downloader returned non-bytes")
                plain = decrypt_media(bytes(data), aeskey) if aeskey else bytes(data)
                self._cache.put(key, plain)
                self.downloaded += 1
                keys.append(key)
            except Exception as e:
                # 单附件失败不扩散（multica 陷阱 7），但必须留痕且不移位
                self.failed += 1
                log.warning("[wecom] media fetch failed (idx=%s type=%s): %r",
                            index, getattr(item, "type", ""), e)
                keys.append("")
        return keys


class WecomIdentityResolver:
    """from.userid → grape 用户（grape_t_channel_sender_identity 的企微视图）。

    企微 userid 在企业主体内可能为加密串（智能机器人创建者非超管时）→ 明文映射
    需自建应用对接，v1 只做同名直查；查不到由 Router 走 identity_bind 逃生口。
    """

    def __init__(self, lookup: Callable[[str], str | None] | None = None) -> None:
        self._lookup = lookup

    async def resolve(self, inst: Any, msg: InboundMessage) -> str | None:
        if self._lookup is None:
            return None
        try:
            return self._lookup(msg.source.sender_id)
        except Exception as e:
            log.warning("[wecom] identity resolve failed: %r", e)
            return None


__all__ = [
    "CACHE_DEFAULT_TTL",
    "MEDIA_URL_TTL",
    "WecomIdentityResolver",
    "WecomMediaCache",
    "WecomMediaError",
    "WecomMediaResolver",
    "decode_aeskey",
    "decrypt_media",
    "media_storage_key",
]