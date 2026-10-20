"""Issue #372 R11: 第八轮审计安全修复测试。

覆盖：
- T21: detect_webhook_from_host / _get_trusted_host 安全加固
- T22: _update_repo_bot_token 清空 webhook_secret
- T24: _query_path_b_repos repo_key 前缀过滤
- T25: GitHub PAT _has_refresh_token 守卫
- T26: _get_task_token 公共函数
- T27: 前端版本号一致性

所有 DB 操作用 mock，不连真实数据库。
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===========================================================================
# T21: detect_webhook_from_host / _get_trusted_host 安全加固
# ===========================================================================

class TestT21DetectWebhookFromHost:
    """T21: detect_webhook_from_host TRUSTED_HOSTS 安全加固"""

    def test_t21_01_no_trusted_hosts_ignores_forwarded_host(self):
        """T21-01: TRUSTED_HOSTS 未设时 X-Forwarded-Host 被忽略"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)
            from src.user.db import detect_webhook_from_host
            result = detect_webhook_from_host(
                host="1.2.3.4:3000",
                forwarded_host="evil.com",
                forwarded_proto="http",
            )
            assert "evil.com" not in result
            assert "1.2.3.4" in result

    def test_t21_02_trusted_hosts_set_accepts_forwarded_host(self):
        """T21-02: TRUSTED_HOSTS 设定时 X-Forwarded-Host 在白名单内被接受"""
        with patch.dict(os.environ, {"TRUSTED_HOSTS": "example.com"}, clear=False):
            from src.user.db import detect_webhook_from_host
            result = detect_webhook_from_host(
                host="127.0.0.1:3000",
                forwarded_host="example.com",
                forwarded_proto="https",
            )
            assert "example.com" in result
            assert "127.0.0.1" not in result

    def test_t21_03_trusted_hosts_set_rejects_unlisted_forwarded_host(self):
        """T21-03: TRUSTED_HOSTS 设定时 X-Forwarded-Host 不在白名单被拒绝"""
        with patch.dict(os.environ, {"TRUSTED_HOSTS": "example.com"}, clear=False):
            from src.user.db import detect_webhook_from_host
            result = detect_webhook_from_host(
                host="1.2.3.4:3000",
                forwarded_host="evil.com",
                forwarded_proto="http",
            )
            assert result == ""

    def test_t21_04_rejects_localhost(self):
        """T21-04: detect_webhook_from_host 不接受 localhost / 127.0.0.1"""
        from src.user.db import detect_webhook_from_host
        for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            result = detect_webhook_from_host(host=host, forwarded_host="", forwarded_proto="http")
            assert result == "", f"localhost variant {host} should be rejected"


class TestT21bGetTrustedHost:
    """T21b: _get_trusted_host 安全加固"""

    def test_t21b_01_no_trusted_hosts_ignores_forwarded_host(self):
        """T21b-01: TRUSTED_HOSTS 未设时 _get_trusted_host 不读 X-Forwarded-Host"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)

            class MockRequest:
                headers = {"Host": "1.2.3.4:3000", "X-Forwarded-Host": "evil.com"}

            from src.auth.providers import _get_trusted_host
            result = _get_trusted_host(MockRequest())
            assert "evil.com" not in result
            assert "1.2.3.4" in result


# ===========================================================================
# T22: _update_repo_bot_token 清空 webhook_secret
# ===========================================================================

class TestT22UpdateRepoBotToken:
    """T22: _update_repo_bot_token 升级时清空 webhook_secret"""

    def test_t22_01_gitcode_upgrade_clears_webhook_secret(self):
        """T22-01: GitCode B→A 升级后 webhook_secret 被清空"""
        from src.user.db import _update_repo_bot_token
        mock_cursor = MagicMock()
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor):
            _update_repo_bot_token("gitcode:test/repo", "new_bot_token", provider="gitcode")
            # 验证 SQL 含 webhook_secret=''
            sql_arg = mock_cursor.execute.call_args[0][0]
            assert "webhook_secret=''" in sql_arg
            assert "bot_gitcode_token=?" in sql_arg

    def test_t22_02_github_upgrade_clears_webhook_secret(self):
        """T22-02: GitHub B→A 升级也清空 webhook_secret"""
        from src.user.db import _update_repo_bot_token
        mock_cursor = MagicMock()
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor):
            _update_repo_bot_token("github:test/repo", "new_gh_token", provider="github")
            sql_arg = mock_cursor.execute.call_args[0][0]
            assert "webhook_secret=''" in sql_arg
            assert "bot_github_token=?" in sql_arg


# ===========================================================================
# T24: _query_path_b_repos repo_key 前缀过滤
# ===========================================================================

class TestT24QueryPathBReposPrefixFilter:
    """T24: _query_path_b_repos repo_key 前缀过滤"""

    def test_t24_01_gitcode_query_has_prefix_filter(self):
        """T24-01: gitcode 查询包含 NOT LIKE 'github:%' 过滤"""
        from src.user.db import _query_path_b_repos
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor):
            _query_path_b_repos("gitcode")
            sql_arg = mock_cursor.execute.call_args[0][0]
            assert "NOT LIKE 'github:%'" in sql_arg

    def test_t24_02_github_query_has_prefix_filter(self):
        """T24-02: github 查询包含 LIKE 'github:%' 过滤"""
        from src.user.db import _query_path_b_repos
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor):
            _query_path_b_repos("github")
            sql_arg = mock_cursor.execute.call_args[0][0]
            assert "LIKE 'github:%'" in sql_arg


# ===========================================================================
# T25: GitHub PAT _has_refresh_token 守卫
# ===========================================================================

class TestT25GithubPatGuard:
    """T25: GitHub PAT _has_refresh_token 守卫"""

    def test_t25_01_github_pat_skips_refresh(self):
        """T25-01: GitHub PAT（无 refresh_token）不触发 refresh_oauth_token"""
        from src.api.deps import get_user_github_token_with_refresh
        with patch("src.api.deps._get_oauth_token_from_identity", return_value="ghp_PATTOKEN123"), \
             patch("src.auth.grape_account_db._has_refresh_token", return_value=False), \
             patch("src.api.deps.user_db.get_credential") as mock_get_cred, \
             patch("src.api.deps.user_db.refresh_oauth_token") as mock_refresh:
            token = get_user_github_token_with_refresh("testuser", user_id=1)
            assert token == "ghp_PATTOKEN123"
            # 不应该调 get_credential（因为 _has_refresh_token=False 直接 return）
            mock_get_cred.assert_not_called()
            mock_refresh.assert_not_called()


# ===========================================================================
# T26: _get_task_token 公共函数
# ===========================================================================

class TestT26GetTaskTokenPublic:
    """T26: _get_task_token 公共函数验证"""

    def test_t26_01_returns_repo_gitcode_token_if_present(self):
        """T26-01: task 行有 repo_gitcode_token 时直接返回"""
        from src.user.db import get_task_token
        task = {"repo_gitcode_token": "bot_token_123", "repo_key": "gitcode:test/repo"}
        assert get_task_token(task) == "bot_token_123"

    def test_t26_02_returns_empty_for_empty_task(self):
        """T26-02: 空 task 返回空串"""
        from src.user.db import get_task_token
        assert get_task_token({}) == ""

    def test_t26_03_agent_and_service_import_same_function(self):
        """T26-03: agent.py 和 service.py 都从 src.user.db 导入 get_task_token"""
        import src.runtime.agent as agent_mod
        import src.runtime.service as service_mod
        from src.user.db import get_task_token
        # 两个模块的 _get_task_token 应该指向 user.db.get_task_token
        assert agent_mod._get_task_token is get_task_token
        assert service_mod._get_task_token is get_task_token


# ===========================================================================
# T27: 前端版本号一致性
# ===========================================================================

class TestT27FrontendVersionSync:
    """T27: 前端版本号一致性"""

    def test_t27_01_api_js_version_consistent(self):
        """T27-01: api.js 版本号在所有 HTML 中一致"""
        import re
        base = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")
        versions = set()
        for html_file in ("index.html", "register.html", "forgot.html"):
            path = os.path.join(base, html_file)
            if os.path.exists(path):
                with open(path) as f:
                    content = f.read()
                match = re.search(r'api\.js\?v=(\d+)', content)
                if match:
                    versions.add(match.group(1))
        assert len(versions) == 1, f"api.js versions inconsistent: {versions}"

    def test_t27_02_app_css_version_consistent(self):
        """T27-02: app.css 版本号在所有 HTML 中一致"""
        import re
        base = os.path.join(os.path.dirname(__file__), "..", "src", "api", "static")
        versions = set()
        for html_file in ("index.html", "register.html", "forgot.html"):
            path = os.path.join(base, html_file)
            if os.path.exists(path):
                with open(path) as f:
                    content = f.read()
                match = re.search(r'app\.css\?v=(\d+)', content)
                if match:
                    versions.add(match.group(1))
        assert len(versions) == 1, f"app.css versions inconsistent: {versions}"
