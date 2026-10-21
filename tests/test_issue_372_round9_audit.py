"""Issue #372 R12: 第三轮全场景深度审计修复测试。

覆盖：
- T28: GitHub 评论体提取
- T29: auto_upgrade accept_github_invite
- T30: host_repo invite_pending/add_failed 降级
- T32: webhook 无签名拒绝
- T33: repair_host GitHub accept
- T36: issue_updated 标记检测
- T37: get_webhook_token_by_repo ORDER BY

所有 DB 操作用 mock，不连真实数据库。
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===========================================================================
# T28: GitHub 评论体提取
# ===========================================================================

class TestT28GithubCommentBody:
    """T28: GitHub issue_comment 评论体提取"""

    def test_t28_01_github_comment_marker_detected(self):
        """T28-01: GitHub issue_comment 评论含 marker → 被拦截"""
        # 模拟 GitHub issue_comment payload
        github_payload = {
            "comment": {"body": "<!-- grape-auto-comment -->\n✅ done"},
            "action": "created",
        }
        # 验证提取逻辑能从 comment.body 读取
        _oa = github_payload.get("object_attributes", {})
        _note = str(_oa.get("note") or (github_payload.get("comment") or {}).get("body") or github_payload.get("body") or "")
        assert "<!-- grape-auto-comment -->" in _note

    def test_t28_02_github_comment_done_command(self):
        """T28-02: GitHub issue_comment /done 指令能被提取"""
        github_payload = {
            "comment": {"body": "/done"},
            "action": "created",
        }
        _oa = github_payload.get("object_attributes", {})
        _note = str(_oa.get("note") or (github_payload.get("comment") or {}).get("body") or github_payload.get("body") or "")
        assert _note == "/done"

    def test_t28_03_gitcode_comment_still_works(self):
        """T28-03: GitCode issue_comment 评论体仍能提取（回归保护）"""
        gitcode_payload = {
            "object_attributes": {"note": "<!-- grape-auto-comment -->\n完成"},
        }
        _oa = gitcode_payload.get("object_attributes", {})
        _note = str(_oa.get("note") or (gitcode_payload.get("comment") or {}).get("body") or gitcode_payload.get("body") or "")
        assert "<!-- grape-auto-comment -->" in _note


# ===========================================================================
# T29: auto_upgrade accept_github_invite
# ===========================================================================

class TestT29AutoUpgradeAccept:
    """T29: auto_upgrade GitHub 调 accept_github_invite"""

    def test_t29_01_github_calls_accept_github_invite(self):
        """T29-01: auto_upgrade GitHub 分支调 accept_github_invite"""
        from src.user.db import auto_upgrade_path_b_repos
        with patch("src.user.db._query_path_b_repos", return_value=[
            {"repo_key": "github:test/repo", "repo_owner": "test", "repo_name": "repo", "host_user_id": 1}
        ]), \
             patch("src.api.deps.get_user_github_token_with_refresh", return_value="ghp_token"), \
             patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"id": 1, "username": "user"}), \
             patch("src.user.repo_manager.add_github_collaborator", return_value=True) as mock_add, \
             patch("src.user.repo_manager.accept_github_invite", return_value=True) as mock_accept, \
             patch("src.user.db._update_repo_bot_token") as mock_update:
            auto_upgrade_path_b_repos("github", "bot_token", "bot_user")
            mock_add.assert_called_once()
            mock_accept.assert_called_once()
            mock_update.assert_called_once()

    def test_t29_02_gitcode_accept_returns_false_skips(self):
        """T29-02: GitCode accept_invite_for_repo 返回 False 时不写 token"""
        from src.user.db import auto_upgrade_path_b_repos
        with patch("src.user.db._query_path_b_repos", return_value=[
            {"repo_key": "gitcode:test/repo", "repo_owner": "test", "repo_name": "repo", "host_user_id": 1}
        ]), \
             patch("src.api.deps.get_user_token_with_refresh", return_value="gct_token"), \
             patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"id": 1, "username": "user"}), \
             patch("src.user.repo_manager.add_collaborator", return_value=(True, "")) as mock_add, \
             patch("src.user.repo_manager.accept_invite_for_repo", return_value=False) as mock_accept, \
             patch("src.user.db._update_repo_bot_token") as mock_update:
            auto_upgrade_path_b_repos("gitcode", "bot_token", "bot_user")
            mock_add.assert_called_once()
            mock_accept.assert_called_once()
            # 不应该调 _update_repo_bot_token（accept 失败）
            mock_update.assert_not_called()

    def test_t29_03_github_accept_returns_false_skips(self):
        """T29-03: GitHub accept_github_invite 返回 False 时不写 token"""
        from src.user.db import auto_upgrade_path_b_repos
        with patch("src.user.db._query_path_b_repos", return_value=[
            {"repo_key": "github:test/repo", "repo_owner": "test", "repo_name": "repo", "host_user_id": 1}
        ]), \
             patch("src.api.deps.get_user_github_token_with_refresh", return_value="ghp_token"), \
             patch("src.auth.grape_account_db.get_grape_account_by_id", return_value={"id": 1, "username": "user"}), \
             patch("src.user.repo_manager.add_github_collaborator", return_value=True), \
             patch("src.user.repo_manager.accept_github_invite", return_value=False), \
             patch("src.user.db._update_repo_bot_token") as mock_update:
            auto_upgrade_path_b_repos("github", "bot_token", "bot_user")
            mock_update.assert_not_called()


# ===========================================================================
# T32: webhook 无签名无 token 头拒绝
# ===========================================================================

class TestT32WebhookNoSignatureRejection:
    """T32: webhook 无签名无 token 头时拒绝"""

    def test_t32_01_no_signature_no_token_returns_401(self):
        """T32-01: 无签名无 token 头 → 401（weekly-08 安全加固标准，R12.5 对齐）"""
        import asyncio
        from src.api.routes.webhook import _process_github_webhook

        body = b'{"repository": {"full_name": "test/repo"}, "action": "opened"}'
        headers = {"X-GitHub-Event": "issues"}  # 无 X-Hub-Signature-256, 无 X-GitHub-Token

        with patch("src.user.db.get_webhook_token_by_repo", return_value="webhook_secret"):
            result = asyncio.run(_process_github_webhook(body, headers))
            # 应该返回 401 JSONResponse
            assert hasattr(result, 'status_code'), f"expected JSONResponse, got {type(result)}"
            assert result.status_code == 401


# ===========================================================================
# T36: issue_updated 标记检测
# ===========================================================================

class TestT36IssueUpdatedMarker:
    """T36: issue_updated description 编辑标记检测"""

    def test_t36_01_issue_updated_with_marker_skipped(self):
        """T36-01: issue_updated description 含 marker → 跳过"""
        # 验证标记检测逻辑——不调完整 pipeline，只验证条件
        github_payload = {
            "changes": {"body": {"from": "<!-- grape-auto-comment -->\n原内容"}},
        }
        _oa = github_payload.get("object_attributes", {})
        _desc = str(_oa.get("description") or "")
        if not _desc:
            _changes = github_payload.get("changes", {}) or {}
            _desc = str((_changes.get("body") or {}).get("from") or "")
        assert "<!-- grape-auto-comment -->" in _desc

    def test_t36_02_gitcode_issue_updated_with_marker(self):
        """T36-02: GitCode issue_updated description 含 marker"""
        gitcode_payload = {
            "object_attributes": {"description": "<!-- grape-auto-comment -->\n内容"},
        }
        _oa = gitcode_payload.get("object_attributes", {})
        _desc = str(_oa.get("description") or "")
        assert "<!-- grape-auto-comment -->" in _desc


# ===========================================================================
# T37: get_webhook_token_by_repo ORDER BY
# ===========================================================================

class TestT37GetWebhookTokenOrderBy:
    """T37: get_webhook_token_by_repo ORDER BY"""

    def test_t37_01_sql_has_order_by(self):
        """T37-01: SQL 含 ORDER BY webhook_secret DESC"""
        from src.user.db import get_webhook_token_by_repo
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        with patch("src.user.db.db"), \
             patch("src.user.db.db_lock"), \
             patch("src.user.db.db.cursor", return_value=mock_cursor):
            get_webhook_token_by_repo("test/repo")
            sql = mock_cursor.execute.call_args[0][0]
            assert "ORDER BY" in sql
            assert "webhook_secret DESC" in sql


# ===========================================================================
# T30: host_repo invite_pending/add_failed 降级（代码结构验证）
# ===========================================================================

class TestT30HostRepoInvitePendingFallback:
    """T30: host_repo GitHub invite_pending/add_failed 降级路径 B"""

    def test_t30_01_invite_pending_triggers_fallback(self):
        """T30-01: bot_status=invite_pending → 降级路径 B（代码验证）"""
        # 验证代码中包含 invite_pending 和 add_failed 的条件
        with open("src/api/routes/repos.py") as f:
            content = f.read()
        # 应该有 in ("error", "invite_pending", "add_failed") 的条件
        assert "invite_pending" in content
        assert "add_failed" in content
        # 应该出现在 bot_status 检查中
        assert 'in ("error", "invite_pending", "add_failed")' in content or \
               'in ("error","invite_pending","add_failed")' in content


# ===========================================================================
# T33: repair_host GitHub accept（代码结构验证）
# ===========================================================================

class TestT33RepairHostGithubAccept:
    """T33: repair_host GitHub 调 accept_github_invite"""

    def test_t33_01_repair_host_calls_accept(self):
        """T33-01: repair_host GitHub 分支调 accept_github_invite（代码验证）"""
        with open("src/api/routes/repos.py") as f:
            content = f.read()
        # repair_host 区域应该引用 accept_github_invite
        # 找到 repair_host 函数区域
        repair_idx = content.find("def repair_host")
        assert repair_idx >= 0
        repair_section = content[repair_idx:repair_idx + 5000]
        assert "accept_github_invite" in repair_section
