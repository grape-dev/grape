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
    process_command,
    detect_clean_intent,
)


@pytest.fixture(scope="session", autouse=True)
def _seed_chat_bot_config():
    """Seed 一个已配置 chat bot 的用户（global_im_bot_config + chat_agent_session），
    使 ensure_configured()/get_tenant_token() 等同步函数可读到配置。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("CREATE TABLE IF NOT EXISTS global_im_bot_config (bot_app_id TEXT PRIMARY KEY, bot_type TEXT, is_active INTEGER DEFAULT 1)")
        except Exception:
            pass
        try:
            c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type, is_active) VALUES ('cli_test','chat_bot',1)")
        except Exception:
            pass
        try:
            c.execute("CREATE TABLE IF NOT EXISTS chat_agent_session (id TEXT PRIMARY KEY, user_id INTEGER DEFAULT 0, repo_key TEXT DEFAULT '', im_app_id TEXT DEFAULT '', im_app_secret TEXT DEFAULT '', im_provider TEXT DEFAULT '')")
        except Exception:
            pass
        try:
            c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) VALUES ('sess-feishu-test',1,'','cli_test','test_secret','feishu')")
        except Exception:
            pass
        try:
            c.execute("CREATE TABLE IF NOT EXISTS repo_im_group_binding (id INTEGER PRIMARY KEY AUTOINCREMENT, im_provider TEXT DEFAULT 'feishu', im_chat_id TEXT NOT NULL, im_channel_name TEXT DEFAULT '', repo_key TEXT NOT NULL, bind_by TEXT DEFAULT '', im_app_id TEXT DEFAULT '', auto_impl_enabled INTEGER DEFAULT 0, notify_enabled INTEGER DEFAULT 0)")
        except Exception:
            pass
        db.commit()
    yield


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


class TestDetectCleanIntent:
    def test_clean_command(self):
        assert detect_clean_intent("/clean") is False  # /clean goes through process_command, not detect_clean_intent

    def test_chinese_clean(self):
        assert detect_clean_intent("清除会话缓存") is True
        assert detect_clean_intent("清理上下文") is True
        assert detect_clean_intent("重置Agent") is True
        assert detect_clean_intent("重来吧") is True
        assert detect_clean_intent("从头开始") is True

    def test_no_match(self):
        assert detect_clean_intent("你好") is False
        assert detect_clean_intent("查询issue") is False


class TestProcessCommandClean:
    def _setup_db(self, tmp_path):
        import sqlite3
        db_path = os.path.join(tmp_path, "test.db")
        db = sqlite3.connect(db_path)
        db.execute("""
            CREATE TABLE IF NOT EXISTS repo_im_group_binding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                im_provider TEXT DEFAULT 'feishu',
                im_chat_id TEXT NOT NULL,
                im_channel_name TEXT DEFAULT '',
                repo_key TEXT NOT NULL,
                bind_by TEXT DEFAULT '',
                im_app_id TEXT DEFAULT '',
                auto_impl_enabled INTEGER DEFAULT 0,
                notify_enabled INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS chat_agent_session (
                id TEXT PRIMARY KEY,
                repo_key TEXT DEFAULT '',
                event_history TEXT DEFAULT '[]',
                backend_session_id TEXT DEFAULT '',
                backend TEXT DEFAULT '',
                created_at TEXT, last_active_at TEXT
            )
        """)
        db.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, im_app_id) VALUES (?,?,?)",
                   ("chat_clean_test", "owner/repo", "default"))
        db.execute("INSERT OR REPLACE INTO chat_agent_session (id, repo_key, event_history, backend_session_id, backend) VALUES (?,?,?,?,?)",
                   ("sess-1", "owner/repo", json.dumps([{"type": "chat", "text": "hello"}]), "ses_opencode_old", "opencode"))
        db.commit()
        return db, db_path

    def test_clean_clears_session_ids(self, tmp_path):
        db, _ = self._setup_db(tmp_path)
        result = process_command("/clean", "chat_clean_test", "tester", db, "default")
        assert "已清除" in result
        c = db.cursor()
        c.execute("SELECT backend_session_id, backend, event_history FROM chat_agent_session WHERE repo_key=?", ("owner/repo",))
        row = c.fetchone()
        assert row[0] == ""  # backend_session_id cleared
        assert row[1] == ""  # backend cleared
        assert "hello" in row[2]  # event_history preserved

    def test_clean_no_binding(self, tmp_path):
        import sqlite3
        db = sqlite3.connect(os.path.join(tmp_path, "empty.db"))
        db.execute("""
            CREATE TABLE IF NOT EXISTS repo_im_group_binding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                im_provider TEXT DEFAULT 'feishu',
                im_chat_id TEXT NOT NULL,
                im_channel_name TEXT DEFAULT '',
                repo_key TEXT NOT NULL,
                bind_by TEXT DEFAULT '',
                im_app_id TEXT DEFAULT '',
                auto_impl_enabled INTEGER DEFAULT 0,
                notify_enabled INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.commit()
        result = process_command("/clean", "chat_nobody", "tester", db, "default")
        assert "未绑定" in result

    def test_clean_also_matches_full_gitcode_key(self, tmp_path):
        db, _ = self._setup_db(tmp_path)
        c = db.cursor()
        # Add session with gitcode: prefix
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, repo_key, backend_session_id) VALUES (?,?,?)",
                  ("sess-2", "gitcode:owner/repo", "ses_another"))
        db.commit()
        result = process_command("/clean", "chat_clean_test", "tester", db, "default")
        assert "已清除" in result
        c = db.cursor()
        c.execute("SELECT backend_session_id FROM chat_agent_session WHERE repo_key='gitcode:owner/repo'")
        assert c.fetchone()[0] == ""
