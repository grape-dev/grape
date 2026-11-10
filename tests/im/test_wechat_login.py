"""微信扫码绑定状态机测试（Issue #1728 M1）——wait→scaned→need_verifycode→confirmed。"""
import asyncio

from src.im.adapters.wechat.login import (
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_NEED_VERIFY,
    STATUS_WAIT,
    WechatLoginManager,
)


class FakeLoginClient:
    def __init__(self, script):
        self.script = list(script)
        self.qr_calls = 0
        self.status_calls = []
        self.base_url = "https://ilinkai.weixin.qq.com"

    def with_base_url(self, base_url):
        self.base_url = base_url
        return self

    async def get_bot_qrcode(self, bot_type=3):
        self.qr_calls += 1
        return {"qrcode": f"qr-{self.qr_calls}",
                "qrcode_img_content": f"img-{self.qr_calls}"}

    async def get_qrcode_status(self, qrcode, verify_code=""):
        self.status_calls.append((qrcode, verify_code))
        return self.script.pop(0)


def _manager(script):
    client = FakeLoginClient(script)
    return WechatLoginManager(client_factory=lambda: client, cache_ttl=0), client


def test_start_returns_login_id_and_qr():
    manager, _ = _manager([])
    result = asyncio.run(manager.start())
    assert result["login_id"]
    assert result["qrcode_img_content"] == "img-1"
    assert result["expires_in"] > 0


def test_wait_to_scaned_to_confirmed_state_machine():
    manager, client = _manager([
        {"status": "wait"},
        {"status": "scaned"},
        {"status": "confirmed", "bot_token": "bt-1", "ilink_bot_id": "ib-1",
         "baseurl": "https://ilink.example", "ilink_user_id": "u-1"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    assert asyncio.run(manager.status(login_id))["status"] == STATUS_WAIT
    assert asyncio.run(manager.status(login_id))["status"] == "scaned"
    result = asyncio.run(manager.status(login_id))
    assert result["status"] == STATUS_CONFIRMED
    assert result["app_id"] == "ib-1"
    creds = result["credentials"]
    assert creds["bot_token"] == "bt-1"
    assert creds["baseurl"] == "https://ilink.example"
    assert creds["ilink_user_id"] == "u-1"


def test_need_verifycode_then_pin_passed_through():
    manager, client = _manager([
        {"status": "need_verifycode"},
        {"status": "confirmed", "bot_token": "bt", "ilink_bot_id": "ib"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    result = asyncio.run(manager.status(login_id))
    assert result["status"] == STATUS_NEED_VERIFY
    assert result["need_verifycode"] is True
    asyncio.run(manager.status(login_id, verify_code="123456"))
    assert client.status_calls[-1] == ("qr-1", "123456")


def test_expired_refreshes_qrcode_within_limit():
    manager, client = _manager([
        {"status": "expired"},
        {"status": "wait"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    result = asyncio.run(manager.status(login_id))
    assert result["status"] == STATUS_WAIT
    assert result.get("refreshed") is True
    assert result["refresh_count"] == 1
    assert client.qr_calls == 2
    assert client.status_calls[0][0] == "qr-1"


def test_refresh_limit_stops_after_three():
    manager, client = _manager([
        {"status": "expired"}, {"status": "expired"}, {"status": "expired"},
        {"status": "expired"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    for _ in range(3):
        asyncio.run(manager.status(login_id))
    assert client.qr_calls == 4  # 初始 + 3 次刷新
    result = asyncio.run(manager.status(login_id))
    assert result["status"] == STATUS_EXPIRED


def test_verify_code_blocked_triggers_refresh_message():
    manager, client = _manager([
        {"status": "verify_code_blocked"},
        {"status": "wait"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    result = asyncio.run(manager.status(login_id))
    assert result.get("refreshed") is True
    assert "验证码" in result.get("message", "")


def test_scaned_but_redirect_keeps_scaned_status():
    manager, _ = _manager([
        {"status": "scaned_but_redirect", "redirect_host": "h2.example"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    result = asyncio.run(manager.status(login_id))
    assert result["status"] == "scaned"


def test_scaned_but_redirect_switches_client_base_url():
    manager, client = _manager([
        {"status": "scaned_but_redirect", "redirect_host": "h2.example"},
        {"status": "scaned"},
    ])
    login_id = asyncio.run(manager.start())["login_id"]
    asyncio.run(manager.status(login_id))
    assert client.base_url == "https://h2.example"
    asyncio.run(manager.status(login_id))
    assert client.base_url == "https://h2.example"


def test_unknown_login_id_is_expired():
    manager, _ = _manager([])
    assert asyncio.run(manager.status("nope"))["status"] == STATUS_EXPIRED


def test_status_cache_avoids_duplicate_upstream_polls():
    class OnceClient(FakeLoginClient):
        pass

    client = OnceClient([{"status": "wait"}, {"status": "wait"}])
    manager = WechatLoginManager(client_factory=lambda: client, cache_ttl=60)
    login_id = asyncio.run(manager.start())["login_id"]
    asyncio.run(manager.status(login_id))
    asyncio.run(manager.status(login_id))
    assert len(client.status_calls) == 1