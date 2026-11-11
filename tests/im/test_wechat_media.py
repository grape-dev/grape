"""微信媒体射测试（Issue #1728 M2）——AES-128-ECB + PKCS#7 + CDN 上传。"""
import base64
import os

import pytest

from src.im.adapters.wechat.media import (
    WechatMediaUploader,
    aes_decrypt,
    aes_encrypt,
    md5_hex,
    media_type_for_name,
    normalize_aes_key,
    normalize_aes_key_b64,
    pkcs7_pad,
    pkcs7_unpad,
)
from src.im.adapters.wechat.protocol import WechatApiError


def test_key_accepts_hex32_and_base64():
    raw = bytes(range(16))
    assert normalize_aes_key(raw.hex()) == raw
    assert normalize_aes_key(base64.b64encode(raw).decode()) == raw


def test_key_rejects_invalid():
    assert normalize_aes_key("") is None
    assert normalize_aes_key("zz" * 16) is None
    assert normalize_aes_key(base64.b64encode(b"short").decode()) is None


def test_normalize_key_b64_always_16_bytes():
    value = normalize_aes_key_b64("00112233445566778899aabbccddeeff")
    assert len(base64.b64decode(value)) == 16


def test_pkcs7_pad_unpad_roundtrip():
    for size in (0, 1, 15, 16, 17, 100):
        data = os.urandom(size)
        padded = pkcs7_pad(data)
        assert len(padded) % 16 == 0
        assert pkcs7_unpad(padded) == data


def test_aes_roundtrip_hex_key():
    key = "00112233445566778899aabbccddeeff"
    data = b"grape wechat media payload" * 3
    encrypted = aes_encrypt(data, key)
    assert encrypted != data
    assert aes_decrypt(encrypted, key) == data


def test_aes_roundtrip_base64_key():
    key = base64.b64encode(bytes(range(16))).decode()
    data = b"hello"
    assert aes_decrypt(aes_encrypt(data, key), key) == data


def test_aes_invalid_key_raises():
    with pytest.raises(WechatApiError):
        aes_encrypt(b"x", "not-a-key")


def test_media_type_for_name():
    assert media_type_for_name("a.PNG") == "image"
    assert media_type_for_name("a.mp4") == "video"
    assert media_type_for_name("a.pdf") == "file"
    assert media_type_for_name("", "image/jpeg") == "image"


class FakeUploadClient:
    def __init__(self, headers, status=200):
        self.headers = headers
        self.status = status
        self.uploaded = b""
        self.upload_calls = 0
        self.last_payload = {}

    async def get_upload_url(self, payload):
        self.last_payload = payload
        return {"upload_full_url": "https://cdn/upload"}

    async def post_upload_bytes(self, url, data):
        self.upload_calls += 1
        self.uploaded = data
        return self.status, dict(self.headers)

    async def download_bytes(self, url, timeout=None):
        return self.uploaded


def test_upload_retries_until_encrypted_param():
    class FlakyUploadClient(FakeUploadClient):
        async def post_upload_bytes(self, url, data):
            self.upload_calls += 1
            self.uploaded = data
            if self.upload_calls < 3:
                return 200, {}
            return 200, {"x-encrypted-param": "enc-ok"}

    client = FlakyUploadClient({})
    uploader = WechatMediaUploader(client, max_retries=2)
    data = b"payload-bytes"
    ref = __import__("asyncio").run(uploader.upload(data, filename="a.txt", media_type="file"))
    assert client.upload_calls == 3
    assert ref["encrypt_query_param"]
    assert ref["encrypt_type"] == 1
    assert client.last_payload["rawsize"] == len(data)
    assert client.last_payload["rawfilemd5"] == md5_hex(data)
    assert aes_decrypt(client.uploaded, ref["aes_key"]) == data


def test_upload_success_first_try():
    client = FakeUploadClient({"x-encrypted-param": "enc-1"})
    uploader = WechatMediaUploader(client)
    ref = __import__("asyncio").run(uploader.upload(b"abc", filename="a.png", media_type="image"))
    assert client.upload_calls == 1
    assert ref["encrypt_query_param"] == "enc-1"


def test_upload_missing_param_exhausts_and_raises():
    client = FakeUploadClient({})
    uploader = WechatMediaUploader(client, max_retries=1)
    with pytest.raises(WechatApiError):
        __import__("asyncio").run(uploader.upload(b"abc", filename="a.bin"))


def test_upload_empty_payload_raises():
    with pytest.raises(WechatApiError):
        __import__("asyncio").run(WechatMediaUploader(FakeUploadClient({})).upload(b""))


def test_download_decrypts_roundtrip():
    async def scenario():
        key = base64.b64encode(os.urandom(16)).decode()
        cipher = aes_encrypt(b"file-content", key)
        client = FakeUploadClient({})
        client.uploaded = cipher
        uploader = WechatMediaUploader(client)
        return await uploader.download("https://cdn/x", key)
    assert __import__("asyncio").run(scenario()) == b"file-content"