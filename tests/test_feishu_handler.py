"""Tests for FeishuAsync API client (new async implementation).

替换原旧同步 feishu.py 的测试：token 缓存、消息发送、群成员分页等
在新版 FeishuAsync（aiohttp 实现）上的等价覆盖。
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _make_ctx(app_id="cli_feishu"):
    from src.im.feishu_async import BotContext
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="feishu-test")


def _make_api_with_session():
    """构造 FeishuAsync + mock aiohttp session。

    session 用 MagicMock（非 AsyncMock）：其 .post/.get 调用返回普通对象，
    可被 `async with` 正常处理；响应对象由 _http_response 提供 async CM。
    """
    from src.im.feishu_async import FeishuAsync
    api = FeishuAsync()
    session = MagicMock()
    api._get_session = AsyncMock(return_value=session)
    return api, session


def _http_response(payload):
    """构造支持 `async with` 的响应对象：__aenter__ 返回自身，json() 返回 payload。"""
    resp = MagicMock()
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestGetTenantToken:
    def test_fetches_token_with_cache(self):
        """首次获取 token，二次命中 ctx.token 缓存。"""
        from src.im.feishu_async import FEISHU_BASE
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        session.post.return_value = _http_response(
            {"tenant_access_token": "tok_abc", "expire": 7200}
        )

        async def run():
            t1 = await api.get_tenant_token(ctx)
            t2 = await api.get_tenant_token(ctx)
            return t1, t2

        t1, t2 = asyncio.run(run())
        assert t1 == "tok_abc"
        assert t2 == "tok_abc"
        assert session.post.call_count == 1  # 缓存命中
        url = session.post.call_args[0][0]
        assert url == f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"

    def test_token_error_returns_empty(self):
        """token API 异常返回空串。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        session.post.return_value = _http_response({"code": 10003})

        async def run():
            return await api.get_tenant_token(ctx)

        assert asyncio.run(run()) == ""


class TestReplyCard:
    def test_sends_card(self):
        """reply_card 发送 interactive 卡片。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        session.post.return_value = _http_response(
            {"data": {"message_id": "msg_test_123"}}
        )

        async def run():
            return await api.reply_card(ctx, "msg_orig", "Test card")

        msg_id = asyncio.run(run())
        assert msg_id == "msg_test_123"
        url = session.post.call_args[0][0]
        assert "im/v1/messages/msg_orig/reply" in url
        payload = session.post.call_args.kwargs["json"]
        assert payload["msg_type"] == "interactive"
        card = json.loads(payload["content"])
        assert card["header"]["template"] == "blue"

    def test_no_token_returns_empty(self):
        """无 token 时不发送。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="")

        async def run():
            return await api.reply_card(ctx, "msg_orig", "text")

        assert asyncio.run(run()) == ""
        session.post.assert_not_called()


class TestSendCardToUser:
    def test_sends_to_user(self):
        """send_card_to_user 主动发送给 open_id。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        session.post.return_value = _http_response(
            {"data": {"message_id": "msg_user_456"}}
        )

        async def run():
            return await api.send_card_to_user(ctx, "ou_user", "Hello")

        msg_id = asyncio.run(run())
        assert msg_id == "msg_user_456"
        url = session.post.call_args[0][0]
        assert "receive_id_type=open_id" in url
        payload = session.post.call_args.kwargs["json"]
        assert payload["receive_id"] == "ou_user"


class TestReplyText:
    def test_reply_text(self):
        """reply_text 发送纯文本。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        session.post.return_value = _http_response(
            {"data": {"message_id": "msg_rt"}}
        )

        async def run():
            return await api.reply_text(ctx, "msg_orig", "hi")

        assert asyncio.run(run()) == "msg_rt"
        payload = session.post.call_args.kwargs["json"]
        assert payload["msg_type"] == "text"


class TestGetChatMembers:
    def test_returns_normalized_members(self):
        """返回规范化 [{name, open_id}]，member_id → open_id。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        session.get.return_value = _http_response({
            "code": 0,
            "data": {
                "items": [
                    {"name": "张三", "member_id": "ou_zhang"},
                    {"name": "李四", "member_id": "ou_li"},
                ],
                "has_more": False, "page_token": "",
            },
        })

        async def run():
            return await api.get_chat_members(ctx, "chat_123")

        members = asyncio.run(run())
        assert len(members) == 2
        assert members[0] == {"name": "张三", "open_id": "ou_zhang"}
        assert members[1] == {"name": "李四", "open_id": "ou_li"}
        headers = session.get.call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer tok_test"

    def test_handles_pagination(self):
        """has_more=true 时带 page_token 翻页。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        calls = {"n": 0}

        async def _json():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"code": 0, "data": {"items": [{"name": "A", "member_id": "ou_a"}],
                                            "has_more": True, "page_token": "pt_next"}}
            return {"code": 0, "data": {"items": [{"name": "B", "member_id": "ou_b"}],
                                        "has_more": False, "page_token": ""}}

        resp = MagicMock()
        resp.json = _json
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session.get.return_value = resp

        async def run():
            return await api.get_chat_members(ctx, "chat_123")

        members = asyncio.run(run())
        assert len(members) == 2
        assert session.get.call_count == 2
        params2 = session.get.call_args_list[1].kwargs.get("params", {})
        assert params2.get("page_token") == "pt_next"

    def test_no_token_returns_empty(self):
        """无 token 返回空，不发请求。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="")

        async def run():
            return await api.get_chat_members(ctx, "chat_123")

        assert asyncio.run(run()) == []
        session.get.assert_not_called()

    def test_exception_isolation(self):
        """异常时返回空列表不抛。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        resp = MagicMock()
        resp.json = AsyncMock(side_effect=RuntimeError("boom"))
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session.get.return_value = resp

        async def run():
            return await api.get_chat_members(ctx, "chat_123")

        assert asyncio.run(run()) == []


class TestGetUserNickname:
    def test_get_user_name(self):
        """get_user_name 返回昵称。"""
        api, session = _make_api_with_session()
        ctx = _make_ctx()
        api._ensure_token = AsyncMock(return_value="tok_test")
        session.get.return_value = _http_response(
            {"data": {"user": {"name": "测试昵称"}}}
        )

        async def run():
            return await api.get_user_name(ctx, "ou_user")

        assert asyncio.run(run()) == "测试昵称"
