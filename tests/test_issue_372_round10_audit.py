"""Issue #372 R12.12-R12.13: host_user_id guard structural fix tests.

Cover:
- T38: get_bot_token_by_repo guard (single point inheritance)
- T39: call sites switched to get_repo_token_with_refresh
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestT38GetBotTokenByRepoGuard:
    """T38: get_bot_token_by_repo host_user_id guard (single point)"""

    def test_t38_01_path_b_returns_empty(self):
        """T38-01: 路径 B 仓库（host_user_id > 0）调 get_bot_token_by_repo 返回空"""
        from src.user.db import get_bot_token_by_repo
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("bot_token_value",)
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor), \
             patch("src.user.db.get_host_user_id", return_value=42):
            result = get_bot_token_by_repo("owner", "repo")
            # 路径 B 仓库 → 守卫返回空，不查 DB
            assert result == ""
            # 不应该执行 SQL（守卫在 SQL 之前返回）
            mock_cursor.execute.assert_not_called()

    def test_t38_02_path_a_returns_token(self):
        """T38-02: 路径 A 仓库（host_user_id=0）正常返回 bot token"""
        from src.user.db import get_bot_token_by_repo
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("bot_token_value",)
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor), \
             patch("src.user.db.get_host_user_id", return_value=0):
            result = get_bot_token_by_repo("owner", "repo")
            assert result == "bot_token_value"

    def test_t38_03_unmanaged_repo_returns_empty(self):
        """T38-03: 未托管仓库（host_user_id=0, bot_token 空）返回空"""
        from src.user.db import get_bot_token_by_repo
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor), \
             patch("src.user.db.get_host_user_id", return_value=0):
            result = get_bot_token_by_repo("owner", "repo")
            assert result == ""


class TestT38bGetRepoBotTokenNoGlobalGuard:
    """T38b: get_repo_bot_token_no_global devops_agent_session guard"""

    def test_t38b_01_path_b_skips_devops_agent_session(self):
        """T38b-01: 路径 B 仓库跳过 devops_agent_session 层

        R18 改造后 get_repo_bot_token_no_global 委托 TokenResolver.read_stored_token()，
        DevopsAgentSessionToken._fetch 有 host_user_id 守卫（R22.2.4）跳过 session token。
        测试 mock TokenResolver.read_stored_token 返回用户 token。
        """
        from src.user.db import get_repo_bot_token_no_global
        with patch("src.token_resolver.TokenResolver.read_stored_token", return_value="user_token"):
            result = get_repo_bot_token_no_global("gitcode:owner/repo")
            assert result == "user_token"


class TestT39CallSitesSwitched:
    """T39: call sites switched to get_repo_token_with_refresh"""

    def test_t39_01_scenes_py_uses_unified_entry(self):
        """T39-01: scenes.py 不再直接调 get_bot_token_by_repo"""
        with open("src/api/routes/scenes.py") as f:
            content = f.read()
        # 不应该有 get_bot_token_by_repo 调用（import 不算）
        lines = [l for l in content.split('\n') if 'get_bot_token_by_repo' in l and 'import' not in l]
        assert len(lines) == 0, f"scenes.py should not call get_bot_token_by_repo directly: {lines}"
        # 应该调 get_repo_token_with_refresh
        assert "get_repo_token_with_refresh" in content

    def test_t39_02_chat_py_uses_unified_entry(self):
        """T39-02: chat.py 不再直接调 get_bot_token_by_repo"""
        with open("src/api/routes/chat.py") as f:
            content = f.read()
        lines = [l for l in content.split('\n') if 'get_bot_token_by_repo' in l and 'import' not in l]
        assert len(lines) == 0, f"chat.py should not call get_bot_token_by_repo directly: {lines}"

    def test_t39_03_agents_py_uses_unified_entry(self):
        """T39-03: agents.py 不再直接调 get_bot_token_by_repo"""
        with open("src/api/routes/agents.py") as f:
            content = f.read()
        lines = [l for l in content.split('\n') if 'get_bot_token_by_repo' in l and 'import' not in l]
        assert len(lines) == 0, f"agents.py should not call get_bot_token_by_repo directly: {lines}"

    def test_t39_04_session_store_uses_unified_entry(self):
        """T39-04: session_store.py 不再直接调 get_bot_token_by_repo"""
        with open("src/api/session_store.py") as f:
            content = f.read()
        lines = [l for l in content.split('\n') if 'get_bot_token_by_repo' in l and 'import' not in l]
        assert len(lines) == 0, f"session_store.py should not call get_bot_token_by_repo directly: {lines}"

    def test_t39_05_settings_py_uses_unified_entry(self):
        """T39-05: settings.py 不再直接调 get_bot_token_by_repo"""
        with open("src/api/routes/settings.py") as f:
            content = f.read()
        lines = [l for l in content.split('\n') if 'get_bot_token_by_repo' in l and 'import' not in l]
        assert len(lines) == 0, f"settings.py should not call get_bot_token_by_repo directly: {lines}"

    def test_t39_06_deps_py_uses_unified_entry(self):
        """T39-06: deps.py get_bot_token 不再调 get_bot_token_by_repo"""
        with open("src/api/deps.py") as f:
            content = f.read()
        # get_bot_token 函数体不应调 get_bot_token_by_repo
        idx = content.find("def get_bot_token(")
        assert idx >= 0
        func_body = content[idx:idx + 500]
        assert "get_bot_token_by_repo" not in func_body, "deps.py get_bot_token should use get_repo_token_with_refresh"

    def test_t39_07_repo_manager_uses_unified_entry(self):
        """T39-07: repo_manager.py setup_webhook 不再调 get_bot_token_by_repo"""
        with open("src/user/repo_manager.py") as f:
            content = f.read()
        idx = content.find("def setup_webhook(")
        assert idx >= 0
        func_body = content[idx:idx + 500]
        assert "get_bot_token_by_repo" not in func_body, "repo_manager.py setup_webhook should use get_repo_token_with_refresh"

    def test_t39_08_feishu_async_uses_unified_entry(self):
        """T39-08: feishu_async.py 不再调 get_bot_token_by_repo"""
        with open("src/im/feishu_async.py") as f:
            content = f.read()
        lines = [l for l in content.split('\n') if 'get_bot_token_by_repo' in l and 'import' not in l and '-' not in l.split('get_bot_token_by_repo')[0]]
        assert len(lines) == 0, f"feishu_async.py should not call get_bot_token_by_repo directly: {lines}"
