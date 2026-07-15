"""Tests for webhook_handler module."""
import pytest
import os
import sys
import json
from unittest.mock import Mock, patch, MagicMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Must set env BEFORE importing Handler
os.environ["GITCODE_API_TOKEN"] = "test_token"
os.environ["GITCODE_WEBHOOK_TOKEN"] = "test_webhook_secret"

# Force config to reload
if "src.config" in sys.modules:
    import importlib
    importlib.reload(sys.modules["src.config"])

from src.webhook_handler import Handler


class MockHandler(Handler):
    """Helper to instantiate Handler without socket."""
    def __init__(self, method="GET", path="/health", body=b"", headers=None):
        self.command = method
        self.path = path
        self.rfile = MagicMock()
        self.rfile.read.return_value = body
        self.headers = MagicMock()
        self.headers.get.side_effect = lambda k, d=None: {"X-GitCode-Token": "test_webhook_secret", "Content-Length": str(len(body))}.get(k, d)
        self.client_address = ("127.0.0.1", 12345)
        self.wfile = MagicMock()
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()
        self._json_response = MagicMock()


class TestHealthEndpoint:
    def test_health_returns_200(self):
        h = MockHandler(path="/health")
        h.do_GET()
        h.send_response.assert_called_with(200)

    def test_health_contains_skills(self):
        h = MockHandler(path="/health")
        h.do_GET()
        called_data = h.wfile.write.call_args[0][0]
        assert b"skills" in called_data
        assert b"opencode" in called_data


class TestApiSessions:
    def test_sessions_returns_json(self, tmp_path):
        h = MockHandler(path="/api/sessions")
        # Mock DB to return empty
        with patch("src.session_manager.db") as mock_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = []
            mock_db.cursor.return_value = mock_cursor
            h.do_GET()
        h.send_response.assert_called_with(200)


class TestApiUserMapping:
    def test_mapping_missing_params_400(self):
        h = MockHandler(path="/api/user-mapping")
        h._json_response = MagicMock()
        h.do_GET()
        h._json_response.assert_called_with(400, {"error": "missing login or repo_key"})

    def test_mapping_found(self, tmp_path):
        h = MockHandler(path="/api/user-mapping?login=user1&repo_key=gitcode:owner/repo")
        h._json_response = MagicMock()
        with patch("src.session_manager.db") as mock_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = ("ou_12345", "张三")
            mock_db.cursor.return_value = mock_cursor
            h.do_GET()
        h._json_response.assert_called_with(200, {
            "login": "user1", "open_id": "ou_12345", "nick_name": "张三"
        })

    def test_mapping_not_found(self, tmp_path):
        h = MockHandler(path="/api/user-mapping?login=nobody&repo_key=gitcode:owner/repo")
        h._json_response = MagicMock()
        with patch("src.session_manager.db") as mock_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.return_value = None
            mock_db.cursor.return_value = mock_cursor
            with patch("src.im.feishu.get_chat_id_for_repo", return_value=None):
                h.do_GET()
        h._json_response.assert_called_with(404, {"error": "not_found", "login": "nobody"})

    def test_mapping_sync_on_miss(self, tmp_path):
        """When not in DB, should sync then retry."""
        h = MockHandler(path="/api/user-mapping?login=newuser&repo_key=gitcode:owner/repo")
        h._json_response = MagicMock()
        call_count = [0]
        def fetchone_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return ("ou_new", "新用户")
        with patch("src.session_manager.db") as mock_db:
            mock_cursor = MagicMock()
            mock_cursor.fetchone.side_effect = fetchone_side_effect
            mock_db.cursor.return_value = mock_cursor
            with patch("src.im.feishu.get_chat_id_for_repo", return_value="chat_1"):
                with patch("src.im.feishu.sync_repo_feishu_mapping", return_value=1):
                    h.do_GET()
        h._json_response.assert_called_with(200, {
            "login": "newuser", "open_id": "ou_new", "nick_name": "新用户"
        })
        assert call_count[0] == 2


class TestWebhookToken:
    def test_valid_token(self):
        h = MockHandler(method="POST", path="/webhook/gitcode", body=b'{}')
        h.headers.get.side_effect = lambda k, d=None: "test_webhook_secret" if k == "X-GitCode-Token" else d
        with patch("src.webhook_handler._submit_event"):
            h.do_POST()
        h.send_response.assert_called_with(200)

    def test_invalid_token_403(self):
        h = MockHandler(method="POST", path="/webhook/gitcode", body=b'{}')
        h.headers.get.side_effect = lambda k, d=None: "wrong_token" if k == "X-GitCode-Token" else d
        h.do_POST()
        h.send_response.assert_called_with(403)


class TestNotFound:
    def test_unknown_path_404(self):
        h = MockHandler(path="/nonexistent")
        h.do_GET()
        h.send_response.assert_called_with(404)
