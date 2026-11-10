"""微信 ilink 媒体加解密与 CDN 上传（Issue #1728 M2）。

协议事实（`docs/protocol_zh_CN.md` §媒体）：
  - 入站：CDN 密文 → **AES-128-ECB + PKCS#7** 解密；
    key 双形态：`image_item.aeskey` 为 32 位 hex，`media.aes_key` 为 base64 16B；
  - 出站：算 MD5/大小 → `getuploadurl` → 密文 POST 到 `upload_full_url`
    → 响应头 `x-encrypted-param` 非空（缺失重试 ≤3）→ 把
    `{encrypt_query_param, aes_key, encrypt_type:1}` 放进 media 引用发送。

所有失败都收敛为基础异常/`WechatApiError`，由 sender 侧降级计
`media_upload_failed` / `media_download_failed`，绝不静默丢内容。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .protocol import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    ITEM_VOICE,
    WechatApiError,
    as_int,
    as_str,
)

log = logging.getLogger(__name__)

BLOCK_SIZE = 16
KEY_BYTES = 16
UPLOAD_RETRIES = 3
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

_MEDIA_TYPE_TO_ITEM = {
    "image": ITEM_IMAGE,
    "file": ITEM_FILE,
    "voice": ITEM_VOICE,
    "video": ITEM_VIDEO,
}


def normalize_aes_key(value) -> bytes | None:
    """AES key 兼容 `hex32`（32 hex 字符）与 base64（16 字节）两种形态。

    返回 16 字节 key；无法识别/长度不符返回 None（调用方跳过并计降级）。
    """
    if isinstance(value, (bytes, bytearray)) and len(value) == KEY_BYTES:
        return bytes(value)
    raw = as_str(value).strip()
    if not raw:
        return None
    if len(raw) == 32:
        try:
            data = bytes.fromhex(raw)
        except ValueError:
            data = b""
        if len(data) == KEY_BYTES:
            return data
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None
    return data if len(data) == KEY_BYTES else None


def normalize_aes_key_b64(value) -> str:
    """把任意形态 key 归一为 base64（出站协议要求 aes_key 为 base64）。"""
    key = normalize_aes_key(value)
    if key is None:
        key = os.urandom(KEY_BYTES)
    return base64.b64encode(key).decode("ascii")


def pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    data = bytes(data or b"")
    pad = block_size - (len(data) % block_size)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    data = bytes(data or b"")
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > block_size or pad > len(data):
        return data
    if data[-pad:] != bytes([pad]) * pad:
        return data
    return data[:-pad]


def aes_encrypt(data: bytes, key) -> bytes:
    key_bytes = normalize_aes_key(key)
    if key_bytes is None:
        raise WechatApiError("invalid AES key for encrypt")
    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(pkcs7_pad(data)) + encryptor.finalize()


def aes_decrypt(data: bytes, key) -> bytes:
    key_bytes = normalize_aes_key(key)
    if key_bytes is None:
        raise WechatApiError("invalid AES key for decrypt")
    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
    decryptor = cipher.decryptor()
    return pkcs7_unpad(decryptor.update(bytes(data or b"")) + decryptor.finalize())


def md5_hex(data: bytes) -> str:
    return hashlib.md5(bytes(data or b"")).hexdigest()


def media_type_for_name(filename: str = "", mime_type: str = "") -> str:
    mime = as_str(mime_type).lower()
    name = as_str(filename).lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if mime.startswith("image/") or ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
        return "image"
    if mime.startswith("video/") or ext in {"mp4", "mov", "avi", "mkv"}:
        return "video"
    if mime.startswith("audio/") or ext in {"mp3", "wav", "amr", "silk"}:
        return "voice"
    return "file"


class WechatMediaUploader:
    """出站媒体上传：`getuploadurl` → 密文 POST → 返回 media 引用。

    注入 `client`（protocol.WechatClient）后单测可用假 transport。
    """

    def __init__(self, client, *, max_retries: int = UPLOAD_RETRIES) -> None:
        self._client = client
        self._max_retries = max(0, int(max_retries))

    async def upload(self, data: bytes, *, filename: str = "file.bin",
                     media_type: str = "file") -> dict:
        payload = bytes(data or b"")
        if not payload:
            raise WechatApiError("empty media payload")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise WechatApiError("media exceeds upload limit")
        key_b64 = normalize_aes_key_b64(os.urandom(KEY_BYTES))
        key_bytes = base64.b64decode(key_b64)
        encrypted = aes_encrypt(payload, key_bytes)
        upload = await self._client.get_upload_url({
            "filekey": as_str(filename) or "file.bin",
            "media_type": as_int(_MEDIA_TYPE_TO_ITEM.get(media_type, ITEM_FILE), ITEM_FILE),
            "rawsize": len(payload),
            "rawfilemd5": md5_hex(payload),
            "filesize": len(encrypted),
            "no_need_thumb": True,
            "aeskey": key_b64,
        })
        full_url = as_str(upload.get("upload_full_url"))
        if not full_url:
            raise WechatApiError("getuploadurl missing upload_full_url")

        encrypt_param = ""
        last_error = ""
        for attempt in range(self._max_retries + 1):
            status, headers = await self._client.post_upload_bytes(full_url, encrypted)
            encrypt_param = as_str(
                headers.get("x-encrypted-param") or headers.get("X-Encrypted-Param"))
            if 200 <= int(status or 0) < 300 and encrypt_param:
                break
            last_error = f"upload HTTP {status} encrypted_param={bool(encrypt_param)}"
            log.info("[wechat] media upload retry %s: %s", attempt + 1, last_error)
        if not encrypt_param:
            raise WechatApiError(last_error or "media upload missing x-encrypted-param")

        return {
            "encrypt_query_param": encrypt_param,
            "aes_key": key_b64,
            "encrypt_type": 1,
        }

    async def download(self, url: str, aes_key) -> bytes:
        raw = await self._client.download_bytes(url)
        key = normalize_aes_key(aes_key)
        if key is None:
            raise WechatApiError("media download missing aes key")
        return aes_decrypt(raw, key)


__all__ = [
    "BLOCK_SIZE",
    "KEY_BYTES",
    "MAX_UPLOAD_BYTES",
    "UPLOAD_RETRIES",
    "WechatMediaUploader",
    "aes_decrypt",
    "aes_encrypt",
    "md5_hex",
    "media_type_for_name",
    "normalize_aes_key",
    "normalize_aes_key_b64",
    "pkcs7_pad",
    "pkcs7_unpad",
]