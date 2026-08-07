"""Tests for chat handler (async version) routing logic."""
import pytest
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


def _make_ctx(app_id="cli_chat"):
    from src.im.feishu_async import BotContext
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-test")


async def _run(text, parsed_extra=None, api=None, ctx=None):
    from src.im.handler_async import process_chat_async
    ctx = ctx or _make_ctx()
    api = api or AsyncMock()
    parsed = {
        "type": "message", "text": text, "chat_id": "chat_1",
        "message_id": "msg_1", "sender_id": "user_1", "sender_open_id": "ou_1",
        "chat_type": "group",
    }
    if parsed_extra:
        parsed.update(parsed_extra)
    await process_chat_async(ctx, api, parsed)
    return api


class TestProcessChatAsync:
    def test_non_message_returns(self):
        """Non-message type should return immediately."""
        import asyncio
        from src.im.handler_async import process_chat_async
        ctx = _make_ctx()
        api = AsyncMock()
        asyncio.run(process_chat_async(ctx, api, {"type": "event"}))
        api.reply_text.assert_not_called()
        api.reply_card.assert_not_called()

    def test_capability_query_returns_skills(self):
        """User asking 'what can you do' should return capabilities."""
        import asyncio
        api = asyncio.run(_run("你有什么能力"))
        api.reply_text.assert_called_once()
        args = api.reply_text.call_args[0]
        assert "能力" in args[2] or "DevOps" in args[2]

    def test_command_routing(self):
        """/command should route to async process_command."""
        import asyncio
        with patch("src.im.handler_async.process_command_async", new=AsyncMock(return_value="/status result")) as mock_pc:
            asyncio.run(_run("/status"))
            mock_pc.assert_called_once()

    def test_unbound_repo_shows_help(self):
        """Message with no bound repo should show binding help."""
        import asyncio
        with patch("src.im.handler_async.resolve_repo", return_value=""):
            with patch("src.im.handler_async.get_user_id_by_app_id", return_value=None):
                api = asyncio.run(_run("check issue"))
        assert api.reply_text.call_count >= 1
        args = api.reply_text.call_args[0]
        assert "绑定" in args[2] or "未绑定" in args[2]

    def test_bind_intent_detected(self):
        """Natural language bind intent should trigger /bind."""
        import asyncio
        with patch("src.im.handler_async.detect_bind_intent", return_value="owner/repo"):
            with patch("src.im.handler_async.process_command_async", new=AsyncMock(return_value="✅ 已绑定")) as mock_pc:
                asyncio.run(_run("绑定到 owner/repo"))
                mock_pc.assert_called_once()

    def test_clean_intent_called(self):
        """'清除会话缓存' should route to /clean command."""
        import asyncio
        with patch("src.im.handler_async.detect_clean_intent", return_value=True):
            with patch("src.im.handler_async.process_command_async", new=AsyncMock(return_value="✅ 已清除")) as mock_pc:
                asyncio.run(_run("清除会话缓存"))
                mock_pc.assert_called_once()
