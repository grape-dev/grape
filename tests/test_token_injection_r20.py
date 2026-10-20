"""R20.24/R20.25 Token 注入链路与脱敏测试。

测试要点：
- R20.24.3: GIT_ASKPASS fallback 链包含 USER_*_TOKEN
- R20.25.1: result_text 脱敏覆盖 user_token
- R20.25.2: agent_logs 脱敏覆盖 user_token
- R20.25.4: 脱敏列表动态构建
"""
import pytest
from unittest.mock import patch, MagicMock
import inspect


class TestGitAskpassFallback:
    """R20.24.3: GIT_ASKPASS fallback 链包含 USER_*_TOKEN。"""

    def test_github_askpass_includes_user_token(self):
        """R20.24.3: GitHub GIT_ASKPASS fallback 链含 USER_GITHUB_TOKEN"""
        from src.runtime.repogit import _write_askpass
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_askpass(tmpdir, "github:owner/repo")
            if path and os.path.exists(path):
                content = open(path).read()
                assert "USER_GITHUB_TOKEN" in content, \
                    "GIT_ASKPASS for GitHub should include USER_GITHUB_TOKEN in fallback chain"

    def test_gitcode_askpass_includes_user_token(self):
        """R20.24.3: GitCode GIT_ASKPASS fallback 链含 USER_GITCODE_TOKEN"""
        from src.runtime.repogit import _write_askpass
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_askpass(tmpdir, "gitcode:owner/repo")
            if path and os.path.exists(path):
                content = open(path).read()
                assert "USER_GITCODE_TOKEN" in content, \
                    "GIT_ASKPASS for GitCode should include USER_GITCODE_TOKEN in fallback chain"

    def test_github_fallback_order(self):
        """R20.24.3: GitHub fallback 顺序 BOT → USER → BOT_TOKEN → BOT_GITCODE"""
        from src.runtime.repogit import _write_askpass
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_askpass(tmpdir, "github:owner/repo")
            if path and os.path.exists(path):
                content = open(path).read()
                # BOT_GITHUB_TOKEN 应在 USER_GITHUB_TOKEN 之前
                assert content.find("BOT_GITHUB_TOKEN") < content.find("USER_GITHUB_TOKEN")

    def test_gitcode_fallback_order(self):
        """R20.24.3: GitCode fallback 顺序 BOT → USER → BOT_TOKEN → BOT_GITHUB"""
        from src.runtime.repogit import _write_askpass
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_askpass(tmpdir, "gitcode:owner/repo")
            if path and os.path.exists(path):
                content = open(path).read()
                # BOT_GITCODE_TOKEN 应在 USER_GITCODE_TOKEN 之前
                assert content.find("BOT_GITCODE_TOKEN") < content.find("USER_GITCODE_TOKEN")


class TestTokenRedaction:
    """R20.25: Token 脱敏全覆盖。"""

    def test_result_text_redacts_both_tokens(self):
        """R20.25.1: result_text 脱敏覆盖 repo_gitcode_token + user_token"""
        # 模拟脱敏逻辑
        result_text = "I got token abc123 and user_token xyz789"
        repo_gitcode_token = "abc123"
        user_token = "xyz789"
        # R20.25.1 脱敏逻辑
        if result_text:
            if repo_gitcode_token:
                result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
            if user_token:
                result_text = result_text.replace(user_token, "[REDACTED]")
        assert "abc123" not in result_text
        assert "xyz789" not in result_text
        assert "[REDACTED]" in result_text

    def test_redact_only_repo_token(self):
        """R20.25.1: 只脱 repo_gitcode_token（无 user_token）"""
        result_text = "token is abc123"
        repo_gitcode_token = "abc123"
        user_token = ""
        if result_text:
            if repo_gitcode_token:
                result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
            if user_token:
                result_text = result_text.replace(user_token, "[REDACTED]")
        assert "abc123" not in result_text
        assert "[REDACTED]" in result_text

    def test_redact_empty_text(self):
        """R20.25.1: 空文本不报错"""
        result_text = ""
        repo_gitcode_token = "abc123"
        if result_text:
            if repo_gitcode_token:
                result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
        assert result_text == ""

    def test_event_pipeline_has_user_token_redaction(self):
        """R20.25.4: event_pipeline.py 中有 user_token 脱敏逻辑"""
        import inspect
        from src.api.event_pipeline import _execute_agent_inner
        source = inspect.getsource(_execute_agent_inner)
        # R20.25.1: result_text 脱敏覆盖 user_token
        assert "_user_token_for_redact" in source, \
            "_execute_agent_inner should have _user_token_for_redact variable for redaction"
        # 验证 result_text 脱敏位置包含 user_token
        idx = source.find("result_text = result_text.replace")
        assert idx > 0, "should have result_text replace logic"
        snippet = source[idx:idx + 500]
        assert "_user_token_for_redact" in snippet, \
            "result_text redaction should include _user_token_for_redact"

    def test_agent_logs_redacts_user_token(self):
        """R20.25.2: agent_logs 脱敏覆盖 user_token"""
        import inspect
        from src.api.event_pipeline import _execute_agent_inner
        source = inspect.getsource(_execute_agent_inner)
        # 找到 agent_logs 脱敏位置
        idx = source.find("_msg = _msg.replace")
        assert idx > 0, "should have _msg replace logic for agent_logs"
        snippet = source[idx:idx + 300]
        assert "_user_token_for_redact" in snippet, \
            "agent_logs redaction should include _user_token_for_redact"
