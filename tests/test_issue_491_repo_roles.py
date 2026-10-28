"""Issue #491: repo admin 角色判定补 role_name 口径（单元测试）。

根因（见 docs/issue-491/spec.md）：GitCode 自定义管理员角色在 collaborator-permission
单查端点返回 role_name="Admin" 但 access_level=20 / permission="custom" /
复数 permissions.admin=false，_check_gitcode_admin 三条判定链全部落空 → 403；
而前端数据源（/user/repos 列表 permission.admin=true）判定 Admin → 按钮可点。
两端不一致导致管理员确认完成非本人创建需求时报「需要仓库管理员权限」。

覆盖（对照 Spec 测试计划）：
1. role_name="Admin" + access_level=20 + permission="custom" → admin（红：修复前 False）
2. role_name="Admin" + access_level=30 → admin（角色名优先于数字等级）
3. role_name="Developer" + access_level=30 → 非 admin（安全边界）
4. role_name="Maintainer" + access_level=40 → 非 admin（#393 终审口径不破坏）
5. 大小写不敏感（role_name="admin"）→ admin
6. get_repo_admin_identity GitHub 路径回归（role_name==admin 既有口径）
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

REPO_KEY = "gitcode:org/issue491"


def _perm(access_level=0, permission="", role_name="", perms_admin=False):
    return {"access_level": access_level, "permission": permission,
            "role_name": role_name,
            "permissions": {"admin": perms_admin, "push": True, "pull": True}}


class TestCheckGitcodeAdminRoleName:
    """_check_gitcode_admin：GitCode admin 判定口径（含 role_name 分支）。"""

    def test_custom_admin_role_name_admin(self):
        """#491 复现形态：自定义管理员角色 role_name=Admin / access_level=20 / custom → admin。"""
        from src.user import repo_manager
        from src.auth.repo_roles import _check_gitcode_admin
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=20, permission="custom",
                                             role_name="Admin")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is True

    def test_role_name_admin_with_developer_level(self):
        """role_name=Admin 但 access_level=30 → admin（角色名权威）。"""
        from src.user import repo_manager
        from src.auth.repo_roles import _check_gitcode_admin
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=30, permission="write",
                                             role_name="Admin")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is True

    def test_role_name_developer_not_admin(self):
        """role_name=Developer → 非 admin（安全边界）。"""
        from src.user import repo_manager
        from src.auth.repo_roles import _check_gitcode_admin
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=30, permission="write",
                                             role_name="Developer")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is False

    def test_role_name_maintainer_not_admin(self):
        """#393 终审口径回归：Maintainer(40) 不是 admin。"""
        from src.user import repo_manager
        from src.auth.repo_roles import _check_gitcode_admin
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=40, permission="admin",
                                             role_name="Maintainer")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is False

    def test_role_name_case_insensitive(self):
        """role_name 大小写不敏感："admin" → admin。"""
        from src.user import repo_manager
        from src.auth.repo_roles import _check_gitcode_admin
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=20, permission="custom",
                                             role_name="admin")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is True

    def test_no_role_name_fallback_access_level(self):
        """无 role_name（旧形态）→ 回退 access_level>=50 判定（兜底不破坏）。"""
        from src.user import repo_manager
        from src.auth.repo_roles import _check_gitcode_admin
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=50, permission="admin")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is True
        with patch.object(repo_manager, "_req",
                          return_value=_perm(access_level=20, permission="custom")):
            assert _check_gitcode_admin("t", "org", "repo", "u") is False


class TestGithubPathUnchanged:
    """GitHub 路径回归：role_name==admin 既有口径不受影响。"""

    def test_github_role_name_admin(self):
        from src.user import repo_manager
        from src.auth.repo_roles import _check_github_admin
        with patch.object(repo_manager, "get_github_collaborator_permission",
                          return_value="admin"):
            assert _check_github_admin("t", "org", "repo", "u") is True
        with patch.object(repo_manager, "get_github_collaborator_permission",
                          return_value="maintain"):
            assert _check_github_admin("t", "org", "repo", "u") is False
