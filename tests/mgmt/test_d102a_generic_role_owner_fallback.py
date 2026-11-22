"""D102-A：generic 适配器 fetch_repos_with_role owner 兜底——行为锁。

根因：Gitee /user/repos 列表响应不带可用 permissions.admin（owner 仓库
也缺），generic 适配器只认该字段 → owner 被标 Member（is_admin=0），
前端 role 显示"成员"。

修复语义（owner 兜底）：仓库 owner.login == 当前用户 login → is_admin=True；
permissions.admin 仍作补充信号（显式 admin=True 也算）——两者取或。
非 owner 仓库维持原判定（permissions 缺失降级 False 不误报权限的
既有原则不变）。
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from unittest.mock import patch


def _mk_resolver():
    from src.domains.mgmt.vcs import create_provider
    return create_provider("gitee")


def _fake_http_gitee_page(login="example-user"):
    """模拟 Gitee /user/repos 响应形态：owner 仓库无 permissions 或 permissions 无 admin。"""
    def _http(method, path, token):
        if path.startswith("/user/repos"):
            return [
                {   # owner 仓库：permissions 缺失（Gitee 列表实测口径）
                    "full_name": f"{login}/my_test",
                    "owner": {"login": login},
                    "name": "my_test",
                    "html_url": f"https://gitee.com/{login}/my_test",
                    # 注意：无 permissions 键
                },
                {   # 他人仓库：permissions 存在且 admin=False
                    "full_name": "other/repo_b",
                    "owner": {"login": "other"},
                    "name": "repo_b",
                    "html_url": "https://gitee.com/other/repo_b",
                    "permissions": {"admin": False, "push": True, "pull": True},
                },
                {   # 他人仓库：permissions admin=True（显式管理员）
                    "full_name": "org1/repo_c",
                    "owner": {"login": "org1"},
                    "name": "repo_c",
                    "html_url": "https://gitee.com/org1/repo_c",
                    "permissions": {"admin": True, "push": True, "pull": True},
                },
            ]
        if path == "/user":
            return {"login": login}
        raise AssertionError(f"unexpected http: {path}")

    return _http


def test_d102a_owner_fallback_is_admin():
    """owner 兜底：owner.login == 当前用户 login → is_admin=True（permissions 缺失也判 Admin）。"""
    r = _mk_resolver()
    with patch.object(r, "_http", side_effect=_fake_http_gitee_page("example-user")):
        repos = r.fetch_repos_with_role("tok")
    by_name = {x["name"]: x for x in repos}
    assert by_name["my_test"]["is_admin"] is True, \
        "owner 仓库（permissions 缺失）应经 owner 兜底判 Admin"


def test_d102a_non_owner_still_permission_based():
    """非 owner 仓库维持原判定：permissions.admin=False → False；admin=True → True。"""
    r = _mk_resolver()
    with patch.object(r, "_http", side_effect=_fake_http_gitee_page("example-user")):
        repos = r.fetch_repos_with_role("tok")
    by_name = {x["name"]: x for x in repos}
    assert by_name["repo_b"]["is_admin"] is False, "非 owner 且显式 admin=False 不应误报"
    assert by_name["repo_c"]["is_admin"] is True, "非 owner 但显式 admin=True 应判 Admin"


def test_d102a_explicit_admin_still_honored():
    """permissions.admin=True 显式信号仍被尊重（owner 兜底是取或非覆盖）。"""
    r = _mk_resolver()
    login = "example-user"

    def _http(method, path, token):
        if path.startswith("/user/repos"):
            return [{"full_name": f"{login}/own_admin",
                     "owner": {"login": login},
                     "name": "own_admin",
                     "permissions": {"admin": True}}]
        if path == "/user":
            return {"login": login}
        raise AssertionError(path)

    with patch.object(r, "_http", side_effect=_http):
        repos = r.fetch_repos_with_role("tok")
    assert repos[0]["is_admin"] is True


def test_d102a_owner_login_fetch_fail_graceful():
    """GET /user 失败（网络/权限）→ 降级为纯 permissions 判定，不炸整个列表拉取。"""
    r = _mk_resolver()
    login = "example-user"

    def _http(method, path, token):
        if path == "/user":
            raise RuntimeError("gitee API /user failed (500)")
        if path.startswith("/user/repos"):
            return [{"full_name": f"{login}/my_test",
                     "owner": {"login": login},
                     "name": "my_test"}]
        raise AssertionError(path)

    with patch.object(r, "_http", side_effect=_http):
        repos = r.fetch_repos_with_role("tok")  # 不应抛异常
    assert len(repos) == 1
    assert repos[0]["is_admin"] is False, "login 拉取失败时降级纯 permissions（缺失=False），列表完整返回"
