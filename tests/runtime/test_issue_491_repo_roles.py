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

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

REPO_KEY = "gitcode:org/issue491"

def _perm(access_level=0, permission="", role_name="", perms_admin=False):
    return {"access_level": access_level, "permission": permission,
            "role_name": role_name,
            "permissions": {"admin": perms_admin, "push": True, "pull": True}}

class TestCheckGitcodeAdminRoleName:
    """_is_admin_permission dict 形态：GitCode admin 判定口径（含 role_name 分支）。"""

    def test_custom_admin_role_name_admin(self):
        """#491 复现形态：自定义管理员角色 role_name=Admin / access_level=20 / custom → admin。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(_perm(access_level=20, permission="custom",
                                           role_name="Admin")) is True

    def test_role_name_admin_with_developer_level(self):
        """role_name=Admin 但 access_level=30 → admin（角色名权威）。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(_perm(access_level=30, permission="write",
                                           role_name="Admin")) is True

    def test_role_name_developer_not_admin(self):
        """role_name=Developer → 非 admin（安全边界）。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(_perm(access_level=30, permission="write",
                                           role_name="Developer")) is False

    def test_role_name_maintainer_not_admin(self):
        """#393 终审口径回归：Maintainer(40) 不是 admin（payload 对齐 #393 先例：permission=write）。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(_perm(access_level=40, permission="write",
                                           role_name="Maintainer")) is False

    def test_role_name_case_insensitive(self):
        """role_name 大小写不敏感："admin" → admin。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(_perm(access_level=20, permission="custom",
                                           role_name="admin")) is True

    def test_no_role_name_fallback_access_level(self):
        """无 role_name（旧形态）→ 回退 access_level>=50 判定（兜底不破坏）。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(_perm(access_level=50, permission="write")) is True
        assert _is_admin_permission(_perm(access_level=20, permission="custom")) is False


class TestGithubPathUnchanged:
    """GitHub 路径回归：role_name==admin 字符串形态既有口径 + generic None fail-closed。"""

    def test_github_role_name_admin(self):
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission("admin") is True
        assert _is_admin_permission("maintain") is False

    def test_generic_none_fail_closed(self):
        """D44/S66：generic 平台权限端口返回 None → fail-closed 非 admin。"""
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        assert _is_admin_permission(None) is False


class TestAdapterPortShapes:
    """D44/S66 行为级锁：适配器端口三形态与判定链端到端接通（mock 端口返回值）。"""

    def test_gitcode_port_dict_shape(self):
        from src.domains.mgmt.vcs import create_provider
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        with patch("src.domains.mgmt.user.repo_manager.get_member_permission",
                   return_value=_perm(access_level=40, permission="write",
                                      role_name="Maintainer")):
            perm = create_provider("gitcode").get_collaborator_permission("t", "org", "repo", "u")
            assert _is_admin_permission(perm) is False
        with patch("src.domains.mgmt.user.repo_manager.get_member_permission",
                   return_value=_perm(access_level=20, permission="custom",
                                      role_name="Admin")):
            perm = create_provider("gitcode").get_collaborator_permission("t", "org", "repo", "u")
            assert _is_admin_permission(perm) is True

    def test_github_port_str_shape(self):
        from src.domains.mgmt.vcs import create_provider
        from src.domains.mgmt.auth.repo_roles import _is_admin_permission
        with patch("src.domains.mgmt.user.repo_manager.get_github_collaborator_permission",
                   return_value="admin"):
            perm = create_provider("github").get_collaborator_permission("t", "org", "repo", "u")
            assert _is_admin_permission(perm) is True

"""Issue #491: repo admin 角色判定补 role_name 口径（单元测试）。

根因（见 docs/issue-491/spec.md）：GitCode 自定义管理员角色在 collaborator-permission
单查端点返回 role_name="Admin" 但 access_level=20 / permission="custom" /
复数 permissions.admin=false，旧 _check_gitcode_admin 三条判定链全部落空 → 403；
而前端数据源（/user/repos 列表 permission.admin=true）判定 Admin → 按钮可点。
两端不一致导致管理员确认完成非本人创建需求时报「需要仓库管理员权限」。

D44/S66 接缝迁移：判定口径收敛为 _is_admin_permission(perm)（形态归一：
GitCode 适配器返回 dict / GitHub 返回 role_name 字符串 / generic 返回 None），
mock 目标从 repo_manager._req 迁移为形态入参直测 + 适配器端口行为锁。

覆盖（对照 Spec 测试计划，口径与迁移前逐条等价）：
1. role_name="Admin" + access_level=20 + permission="custom" → admin（红：修复前 False）
2. role_name="Admin" + access_level=30 → admin（角色名优先于数字等级）
3. role_name="Developer" + access_level=30 → 非 admin（安全边界）
4. role_name="Maintainer" + access_level=40 → 非 admin（#393 终审口径不破坏）
5. 大小写不敏感（role_name="admin"）→ admin
6. GitHub 路径回归（role_name==admin 字符串形态）+ generic None fail-closed
7. 适配器端口形态锁（GitCode dict / GitHub str / generic None）
"""
