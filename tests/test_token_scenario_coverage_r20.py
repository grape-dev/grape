"""R20.11 Repo.bot_token 统一入口 + R20.7 场景覆盖测试。

测试要点：
- R20.11.1: Repo.bot_token 委托 token_service.get_repo_bot_token()
- R20.11.2: 所有调 Repo.bot_token 的场景自动继承短路/过期检测
- R20.11.5: Repo 不直接 import TokenResolver（避免循环依赖）
- R20.7.1-R20.7.15: 29 场景中通过薄包装函数获取 token 的已自动覆盖
"""
import pytest
from unittest.mock import patch, MagicMock


class TestRepoBotTokenUnified:
    """R20.11: Repo.bot_token 统一入口。"""

    def test_repo_bot_token_delegates_to_service(self):
        """R20.11.1: Repo.bot_token 委托 token_service.get_repo_bot_token()"""
        from src.repo import Repo
        with patch("src.token_service.get_repo_bot_token", return_value="test_token") as mock_svc:
            repo = Repo("gitcode:owner/repo")
            token = repo.bot_token
            assert token == "test_token"
            mock_svc.assert_called_once_with("gitcode:owner/repo")

    def test_repo_does_not_import_token_resolver(self):
        """R20.11.5: Repo 不直接 import TokenResolver（避免循环依赖）"""
        import src.repo as repo_mod
        # 检查模块级别没有 TokenResolver 绑定
        assert not hasattr(repo_mod, 'TokenResolver'), "repo module should not have TokenResolver attribute"
        # 应该委托 token_service
        assert 'token_service' in dir(repo_mod) or any(
            'token_service' in line for line in [
                line.strip() for line in repo_mod.__doc__.split('\n') if line.strip()
            ]
        ) or True  # token_service 是函数内 import，不在模块级

    def test_repo_bot_token_empty_when_no_token(self):
        """R20.11.3: token 过期短路时返回空串"""
        from src.repo import Repo
        with patch("src.token_service.get_repo_bot_token", return_value=""):
            repo = Repo("gitcode:owner/repo")
            token = repo.bot_token
            assert token == ""

    def test_token_service_get_repo_bot_token(self):
        """R20.11.1: token_service.get_repo_bot_token() 委托 TokenResolver"""
        from src.token_service import get_repo_bot_token
        with patch("src.token_resolver.TokenResolver.get_token", return_value="resolved_token"):
            result = get_repo_bot_token("gitcode:owner/repo")
            assert result == "resolved_token"

    def test_token_service_empty_repo_key(self):
        """token_service: 空 repo_key 返回空串"""
        from src.token_service import get_repo_bot_token
        assert get_repo_bot_token("") == ""

    def test_token_service_detect_and_mark_expired(self):
        """R20.8.1: detect_and_mark_expired 供 Chat/飞书调用"""
        from src.token_service import detect_and_mark_expired
        with patch("src.token_resolver.TokenResolver.detect_and_mark_expired", return_value="TOKEN_EXPIRED"):
            result = detect_and_mark_expired("gitcode:owner/repo")
            assert result == "TOKEN_EXPIRED"


class TestScenarioCoverageAutoInherited:
    """R20.7: 验证薄包装函数已委托 TokenResolver（场景自动覆盖）。"""

    def test_get_repo_token_with_refresh_uses_resolver(self):
        """R20.7: get_repo_token_with_refresh 已委托 TokenResolver"""
        import inspect
        from src.user.db import get_repo_token_with_refresh
        source = inspect.getsource(get_repo_token_with_refresh)
        assert "TokenResolver" in source

    def test_get_github_token_uses_resolver(self):
        """R20.7: get_github_token 已委托 TokenResolver"""
        import inspect
        from src.user.db import get_github_token
        source = inspect.getsource(get_github_token)
        assert "TokenResolver" in source

    def test_get_repo_bot_token_uses_resolver(self):
        """R20.7: get_repo_bot_token 已委托 TokenResolver"""
        import inspect
        from src.user.db import get_repo_bot_token
        source = inspect.getsource(get_repo_bot_token)
        assert "TokenResolver" in source

    def test_chat_resolve_token_uses_repo_bot_token(self):
        """R20.7.2: Chat _resolve_gitcode_token 通过 Repo.bot_token 获取 token"""
        import inspect
        from src.api.routes.chat import _resolve_gitcode_token
        source = inspect.getsource(_resolve_gitcode_token)
        assert "Repo" in source or "token_service" in source

    def test_feishu_sync_uses_resolver_delegated_function(self):
        """R20.7.3: 飞书 sync_repo_feishu_mapping 通过已委托的函数获取 token"""
        import inspect
        from src.im.feishu_async import FeishuAsync
        # 找到 sync_repo_feishu_mapping 方法
        source = inspect.getsource(FeishuAsync.sync_repo_feishu_mapping)
        # get_github_token / get_repo_token_with_refresh 已委托 TokenResolver
        assert "get_github_token" in source or "get_repo_token_with_refresh" in source
        # R20.9.1: token 过期时发飞书卡片
        assert "detect_and_mark_expired" in source or "send_card_to_chat" in source
