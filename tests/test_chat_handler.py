"""Tests for chat_handler module."""
import pytest
import os
import sys
from unittest.mock import Mock, patch, MagicMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Minimal env setup
os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

# Patch feishu_handler at module level
feishu_patches = {
    "reply_text": MagicMock(return_value=""),
    "reply_card": MagicMock(return_value=""),
    "add_reaction": MagicMock(return_value=""),
    "resolve_repo": MagicMock(return_value=""),
    "process_command": MagicMock(return_value=""),
    "detect_bind_intent": MagicMock(return_value=""),
    "detect_clean_intent": MagicMock(return_value=False),
    "get_user_name": MagicMock(return_value="TestUser"),
}


class TestProcessChat:
    """Test process_chat routing logic."""

    def _make_parsed(self, text="hello", chat_id="chat_1", msg_id="msg_1",
                     sender_id="user_1", open_id="ou_1", msg_type="message"):
        return {
            "type": msg_type,
            "text": text,
            "chat_id": chat_id,
            "message_id": msg_id,
            "sender_id": sender_id,
            "sender_open_id": open_id,
        }

    @patch.multiple("src.chat_handler",
                    reply_text=feishu_patches["reply_text"],
                    reply_card=feishu_patches["reply_card"],
                    add_reaction=feishu_patches["add_reaction"],
                    resolve_repo=feishu_patches["resolve_repo"],
                    process_command=feishu_patches["process_command"],
                    detect_bind_intent=feishu_patches["detect_bind_intent"],
                    detect_clean_intent=feishu_patches["detect_clean_intent"],
                    get_user_name=feishu_patches["get_user_name"])
    def test_non_message_returns(self):
        """Non-message type should return immediately."""
        from src.chat_handler import process_chat
        parsed = self._make_parsed(msg_type="event")
        process_chat(parsed)
        feishu_patches["reply_text"].assert_not_called()
        feishu_patches["reply_card"].assert_not_called()

    @patch.multiple("src.chat_handler",
                    reply_text=feishu_patches["reply_text"],
                    reply_card=feishu_patches["reply_card"],
                    add_reaction=feishu_patches["add_reaction"],
                    resolve_repo=feishu_patches["resolve_repo"],
                    detect_bind_intent=feishu_patches["detect_bind_intent"],
                    detect_clean_intent=feishu_patches["detect_clean_intent"],
                    get_user_name=feishu_patches["get_user_name"])
    def test_capability_query_returns_skills(self):
        """User asking 'what can you do' should return capabilities."""
        from src.chat_handler import process_chat
        parsed = self._make_parsed(text="你有什么能力")
        process_chat(parsed)
        feishu_patches["reply_text"].assert_called_once()
        args = feishu_patches["reply_text"].call_args[0]
        assert "能力" in args[1] or "DevOps" in args[1]

    @patch.multiple("src.chat_handler",
                    reply_text=feishu_patches["reply_text"],
                    reply_card=feishu_patches["reply_card"],
                    add_reaction=feishu_patches["add_reaction"],
                    resolve_repo=feishu_patches["resolve_repo"],
                    process_command=feishu_patches["process_command"],
                    detect_bind_intent=feishu_patches["detect_bind_intent"],
                    detect_clean_intent=feishu_patches["detect_clean_intent"],
                    get_user_name=feishu_patches["get_user_name"])
    def test_command_routing(self):
        """/command should route to process_command."""
        from src.chat_handler import process_chat
        feishu_patches["process_command"].reset_mock()
        feishu_patches["process_command"].return_value = "/status result"
        parsed = self._make_parsed(text="/status")
        process_chat(parsed)
        feishu_patches["process_command"].assert_called_once()

    @patch.multiple("src.chat_handler",
                    reply_text=feishu_patches["reply_text"],
                    reply_card=feishu_patches["reply_card"],
                    add_reaction=feishu_patches["add_reaction"],
                    resolve_repo=feishu_patches["resolve_repo"],
                    process_command=feishu_patches["process_command"],
                    detect_bind_intent=feishu_patches["detect_bind_intent"],
                    detect_clean_intent=feishu_patches["detect_clean_intent"],
                    get_user_name=feishu_patches["get_user_name"])
    def test_unbound_repo_shows_help(self):
        """Message with no bound repo should show binding help."""
        from src.chat_handler import process_chat
        feishu_patches["resolve_repo"].reset_mock()
        feishu_patches["resolve_repo"].return_value = ""
        feishu_patches["reply_text"].reset_mock()
        with patch("src.chat_handler.db"):
            parsed = self._make_parsed(text="check issue")
            process_chat(parsed)
        assert feishu_patches["reply_text"].call_count >= 1
        args = feishu_patches["reply_text"].call_args[0]
        assert "绑定" in args[1] or "未绑定" in args[1]

    @patch.multiple("src.chat_handler",
                    reply_text=feishu_patches["reply_text"],
                    reply_card=feishu_patches["reply_card"],
                    add_reaction=feishu_patches["add_reaction"],
                    resolve_repo=feishu_patches["resolve_repo"],
                    process_command=feishu_patches["process_command"],
                    detect_bind_intent=feishu_patches["detect_bind_intent"],
                    detect_clean_intent=feishu_patches["detect_clean_intent"],
                    get_user_name=feishu_patches["get_user_name"])
    def test_bind_intent_detected(self):
        """Natural language bind intent should trigger /bind."""
        from src.chat_handler import process_chat
        feishu_patches["resolve_repo"].reset_mock()
        feishu_patches["resolve_repo"].return_value = ""
        feishu_patches["detect_bind_intent"].return_value = "owner/repo"
        parsed = self._make_parsed(text="绑定到 owner/repo")
        process_chat(parsed)
        feishu_patches["process_command"].assert_called()


class TestDetectCleanIntent:
    """Direct tests for detect_clean_intent patterns."""

    @patch("src.chat_handler.detect_clean_intent")
    def test_clean_intent_called(self, mock_detect):
        from src.chat_handler import process_chat
        parsed = {
            "type": "message", "text": "清除会话缓存",
            "chat_id": "c1", "message_id": "m1",
            "sender_id": "u1", "sender_open_id": "ou_1"
        }
        mock_detect.return_value = True
        with patch.multiple("src.chat_handler",
                           reply_text=MagicMock(),
                           reply_card=MagicMock(),
                           add_reaction=MagicMock(),
                           resolve_repo=MagicMock(),
                           process_command=MagicMock(),
                           detect_bind_intent=MagicMock(),
                           get_user_name=MagicMock()):
            process_chat(parsed)
            mock_detect.assert_called()
