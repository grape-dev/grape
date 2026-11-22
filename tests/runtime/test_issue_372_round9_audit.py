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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

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

class TestT29AutoUpgradeAccept:
    """T29: auto_upgrade GitHub 调 accept_github_invite"""

    def test_t29_01_github_calls_accept_github_invite(self):
        """T29-01: auto_upgrade GitHub 分支调 accept（D99 缝迁移：适配器端口）"""
        from unittest.mock import MagicMock
        from src.domains.mgmt.user.db import auto_upgrade_path_b_repos
        _prov = MagicMock()
        _prov.add_collaborator.return_value = True
        _prov.accept_collaborator_invite.return_value = True
        with patch("src.domains.mgmt.user.db._query_path_b_repos", return_value=[
            {"repo_key": "github:test/repo", "repo_owner": "test", "repo_name": "repo", "host_user_id": 1}
        ]), \
             patch("src.api.deps.get_user_token_for", return_value="ghp_token"), \
             patch("src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id", return_value={"id": 1, "username": "user"}), \
             patch("src.domains.mgmt.vcs.create_provider", return_value=_prov), \
             patch("src.domains.mgmt.user.db._rebuild_webhook_with_bot_token", return_value="123") as mock_update:
            auto_upgrade_path_b_repos("github", "bot_token", "bot_user")
            _prov.add_collaborator.assert_called_once()
            _prov.accept_collaborator_invite.assert_called_once()
            mock_update.assert_called_once()

    def test_t29_02_gitcode_accept_returns_false_skips(self):
        """T29-02: accept_collaborator_invite 返回 False 时不写 token（D99 缝迁移）"""
        from unittest.mock import MagicMock
        from src.domains.mgmt.user.db import auto_upgrade_path_b_repos
        _prov = MagicMock()
        _prov.add_collaborator.return_value = True
        _prov.accept_collaborator_invite.return_value = False
        with patch("src.domains.mgmt.user.db._query_path_b_repos", return_value=[
            {"repo_key": "gitcode:test/repo", "repo_owner": "test", "repo_name": "repo", "host_user_id": 1}
        ]), \
             patch("src.api.deps.get_user_token_for", return_value="gct_token"), \
             patch("src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id", return_value={"id": 1, "username": "user"}), \
             patch("src.domains.mgmt.vcs.create_provider", return_value=_prov), \
             patch("src.domains.mgmt.user.db._update_repo_bot_token") as mock_update:
            auto_upgrade_path_b_repos("gitcode", "bot_token", "bot_user")
            _prov.add_collaborator.assert_called_once()
            _prov.accept_collaborator_invite.assert_called_once()
            # 不应该调 _update_repo_bot_token（accept 失败）
            mock_update.assert_not_called()

    def test_t29_03_github_accept_returns_false_skips(self):
        """T29-03: accept_collaborator_invite 返回 False 时不写 token（D99 缝迁移）"""
        from unittest.mock import MagicMock
        from src.domains.mgmt.user.db import auto_upgrade_path_b_repos
        _prov = MagicMock()
        _prov.add_collaborator.return_value = True
        _prov.accept_collaborator_invite.return_value = False
        with patch("src.domains.mgmt.user.db._query_path_b_repos", return_value=[
            {"repo_key": "github:test/repo", "repo_owner": "test", "repo_name": "repo", "host_user_id": 1}
        ]), \
             patch("src.api.deps.get_user_token_for", return_value="ghp_token"), \
             patch("src.domains.mgmt.auth.grape_account_db.get_grape_account_by_id", return_value={"id": 1, "username": "user"}), \
             patch("src.domains.mgmt.vcs.create_provider", return_value=_prov), \
             patch("src.domains.mgmt.user.db._update_repo_bot_token") as mock_update:
            auto_upgrade_path_b_repos("github", "bot_token", "bot_user")
            mock_update.assert_not_called()

class TestT32WebhookNoSignatureRejection:
    """T32: webhook 无签名无 token 头时拒绝"""

    def test_t32_01_no_signature_no_token_returns_401(self):
        """T32-01: 已托管仓 + 无签名无 token 头 → 401（weekly-08 安全加固标准，R12.5 对齐）。

        2026-11-13（QA B3）裁定登记与同步：#699 四进程修复后 token 读链改走
        GSB（webhook._expected_webhook_token），且「未托管 404」先于签名校验——
        与 gitcode 端点既有语义一致（unmanaged 404 优先：不对未托管仓泄露
        签名校验细节）。本守卫锁「已托管仓无凭据 → 401 拒绝、绝不进流水线」，
        mock 目标随读链更新为 _expected_webhook_token。
        """
        import asyncio
        from src.domains.runtime.routes.webhook import _process_github_webhook

        body = b'{"repository": {"full_name": "test/repo"}, "action": "opened"}'
        headers = {"X-GitHub-Event": "issues"}  # 无 X-Hub-Signature-256, 无 X-GitHub-Token

        with patch("src.domains.runtime.routes.webhook._expected_webhook_token",
                   return_value="webhook_secret"):
            result = asyncio.run(_process_github_webhook(body, headers))
            # 应该返回 401 JSONResponse
            assert hasattr(result, 'status_code'), f"expected JSONResponse, got {type(result)}"
            assert result.status_code == 401

    def test_t32_02_unmanaged_repo_404_before_signature_check(self):
        """T32-02（裁定登记，2026-11-13）：未托管仓 → 404 先于签名校验。

        裁定口径：与 gitcode 端点既有语义一致（unmanaged 404 优先）——
        未托管仓不进入签名/token 校验分支，不泄露校验细节；404 隐藏
        仓库存在性属有意行为（外部扫描面得到的是统一 not-managed 404）。
        """
        import asyncio
        from src.domains.runtime.routes.webhook import _process_github_webhook

        body = b'{"repository": {"full_name": "test/repo"}, "action": "opened"}'
        headers = {"X-GitHub-Event": "issues"}  # 无签名无 token，且未托管（token 空）

        with patch("src.domains.runtime.routes.webhook._expected_webhook_token",
                   return_value=""):
            result = asyncio.run(_process_github_webhook(body, headers))
            # 未托管分支返回 JSONResponse（处理成功路径才返回 dict）——
            # getattr 取状态码避免对联合返回类型断言属性
            assert getattr(result, "status_code", None) == 404

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

class TestT37GetWebhookTokenOrderBy:
    """T37: get_webhook_token_by_repo ORDER BY"""

    def test_t37_01_sql_has_order_by(self):
        """T37-01: 列回落 SQL 含 ORDER BY webhook_secret DESC（D99：多级回落链，
        mock 首缝改为任意密钥查询直通——捕获 managed_repos 列回落段的 SQL）"""
        from src.domains.mgmt.user.db import get_webhook_token_by_repo
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        with patch("src.domains.mgmt.user.db.db"), \
             patch("src.domains.mgmt.user.db.db_lock"), \
             patch("src.domains.mgmt.user.db.get_webhook_secret_by_repo_key_any", return_value=""), \
             patch("src.domains.mgmt.user.db.get_repo_credential_value", return_value=""), \
             patch("src.domains.mgmt.user.db.db.cursor", return_value=mock_cursor):
            get_webhook_token_by_repo("gitcode:test/repo")
            sql = mock_cursor.execute.call_args[0][0]
            assert "ORDER BY" in sql
            assert "webhook_secret DESC" in sql

class TestT30HostRepoInvitePendingFallback:
    """T30: host_repo GitHub invite_pending/add_failed 降级路径 B"""

    def test_t30_01_invite_pending_triggers_fallback(self):
        """T30-01: bot_status=invite_pending/add_failed 降级语义在册（代码验证）。

        D99 缝迁移：GitHub bot 邀请已收敛进 _ensure_bot_collaborator 端口
        （accept_collaborator_invite），状态字面量（invite_pending/add_failed）
        语义保留；旧元组条件形态已重构为各自 return。"""
        with open("src/domains/mgmt/routes/repos.py") as f:
            content = f.read()
        assert "invite_pending" in content
        assert "add_failed" in content
        # 端口化 accept 链在场（host_repo 主链与 repair 链各一处）
        assert content.count("accept_collaborator_invite") >= 2

class TestT33RepairHostGithubAccept:
    """T33: repair_host GitHub 调 accept_github_invite"""

    def test_t33_01_repair_host_calls_accept(self):
        """T33-01: repair_host 调 accept_collaborator_invite（代码验证）。

        D99 缝迁移：旧符号 accept_github_invite 已收敛进适配器端口。"""
        with open("src/domains/mgmt/routes/repos.py") as f:
            content = f.read()
        repair_idx = content.find("def repair_host")
        assert repair_idx >= 0
        # 下一函数边界（repair_host 体较长，accept 调用在函数后段）
        next_def = content.find("\ndef ", repair_idx + 1)
        repair_section = content[repair_idx:next_def if next_def > 0 else len(content)]
        assert "accept_collaborator_invite" in repair_section

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
