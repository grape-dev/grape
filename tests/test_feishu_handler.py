"""Tests for feishu_handler module."""
import pytest
import os
import sys
import json
from unittest.mock import Mock, patch, MagicMock
from urllib.request import Request

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Ensure Feishu env is set so ensure_configured() returns True
if "FEISHU_APP_ID" not in os.environ:
    os.environ["FEISHU_APP_ID"] = "cli_test"
if "FEISHU_APP_SECRET" not in os.environ:
    os.environ["FEISHU_APP_SECRET"] = "test_secret"

from src.im.feishu import (
    _build_card_payload,
    get_tenant_token,
    reply_card,
    send_card_to_user,
    get_chat_members,
    get_chat_id_for_repo,
    ensure_configured,
)


class TestBuildCardPayload:
    def test_contains_header(self):
        card = _build_card_payload("Hello World")
        assert card["header"]["template"] == "blue"
        assert card["header"]["title"]["content"] == "DevOps Bot"

    def test_contains_elements(self):
        card = _build_card_payload("Hello World")
        assert len(card["elements"]) == 1
        assert card["elements"][0]["tag"] == "div"
        assert card["elements"][0]["text"]["content"] == "Hello World"

    def test_truncates_long_text(self):
        long_text = "a" * 3000
        card = _build_card_payload(long_text)
        content = card["elements"][0]["text"]["content"]
        assert len(content) == 2000


class TestGetTenantToken:
    def _reset_token(self):
        """Force token to None."""
        from src.im.feishu import _TOKEN, _TOKEN_EXPIRE
        import src.im.feishu as fh
        fh._TOKEN = None
        fh._TOKEN_EXPIRE = 0

    @patch("src.im.feishu.urlopen")
    def test_fetches_token(self, mock_urlopen):
        self._reset_token()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "tenant_access_token": "tok_abc123",
            "expire": 7200
        }).encode()
        mock_urlopen.return_value = mock_response
        token = get_tenant_token()
        assert token == "tok_abc123"

    @patch("src.im.feishu.urlopen")
    def test_uses_cached_token(self, mock_urlopen):
        self._reset_token()
        import src.im.feishu as fh
        import time
        fh._TOKEN = "cached_token"
        fh._TOKEN_EXPIRE = time.time() + 3600
        token = fh.get_tenant_token()
        assert token == "cached_token"
        mock_urlopen.assert_not_called()


class TestReplyCard:
    @patch("src.im.feishu.urlopen")
    def test_sends_card(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "data": {"message_id": "msg_test_123"}
        }).encode()
        mock_urlopen.return_value = mock_response

        with patch("src.im.feishu.get_tenant_token", return_value="tok_test"):
            msg_id = reply_card("msg_original", "Test card")
        assert msg_id == "msg_test_123"

    def test_no_message_id_returns_empty(self):
        assert reply_card("", "test") == ""


class TestSendCardToUser:
    @patch("src.im.feishu.urlopen")
    def test_sends_to_user(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "data": {"message_id": "msg_user_456"}
        }).encode()
        mock_urlopen.return_value = mock_response

        with patch("src.im.feishu.get_tenant_token", return_value="tok_test"):
            msg_id = send_card_to_user("ou_test_user", "Hello user")
        assert msg_id == "msg_user_456"

    def test_empty_open_id(self):
        assert send_card_to_user("", "test") == ""


class TestGetChatMembers:
    @patch("src.im.feishu.urlopen")
    def test_returns_members(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "data": {
                "items": [
                    {"name": "张三", "member_id": "ou_zhang"},
                    {"name": "李四", "member_id": "ou_li"},
                ],
                "has_more": False
            }
        }).encode()
        mock_urlopen.return_value = mock_response

        with patch("src.im.feishu.get_tenant_token", return_value="tok_test"):
            members = get_chat_members("chat_123")
        assert len(members) == 2
        assert members[0]["name"] == "张三"
        assert members[0]["open_id"] == "ou_zhang"

    @patch("src.im.feishu.urlopen")
    def test_handles_pagination(self, mock_urlopen):
        page1 = MagicMock()
        page1.read.return_value = json.dumps({
            "data": {
                "items": [{"name": "A", "member_id": "ou_a"}],
                "has_more": True,
                "page_token": "tok_next"
            }
        }).encode()
        page2 = MagicMock()
        page2.read.return_value = json.dumps({
            "data": {
                "items": [{"name": "B", "member_id": "ou_b"}],
                "has_more": False
            }
        }).encode()
        mock_urlopen.side_effect = [page1, page2]

        with patch("src.im.feishu.get_tenant_token", return_value="tok_test"):
            members = get_chat_members("chat_123")
        assert len(members) == 2
        assert mock_urlopen.call_count == 2

    def test_empty_chat_id(self):
        assert get_chat_members("") == []


class TestGetChatIdForRepo:
    def test_repo_not_found(self):
        with patch("src.session_manager.db") as mock_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = None
            mock_db.cursor.return_value = mock_cursor
            chat_id = get_chat_id_for_repo("gitcode:nonexistent/repo")
        assert chat_id == ""

    def test_found(self):
        with patch("src.session_manager.db") as mock_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = ("chat_found_one",)
            mock_db.cursor.return_value = mock_cursor
            chat_id = get_chat_id_for_repo("gitcode:owner/repo")
        assert chat_id == "chat_found_one"


class TestEnsureConfigured:
    def test_returns_true_when_configured(self):
        assert ensure_configured() is True
