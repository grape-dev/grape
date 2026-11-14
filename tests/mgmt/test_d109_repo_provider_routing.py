"""托管路由的平台判定守卫（2026-11-14 实锤：gitee 仓库被当成 github 托管 → 上游 404）。

两条断言：
  1. 后端在「无前缀线索」时用本账号 user_repos 缓存反查平台（唯一命中才采用），
     不再直接落到「按 token 可用性轮询」把 gitee 仓库判成 github。
  2. generic provider 拉仓库时，Gitee v5 的 namespace 形态要能补出 owner /
     full_name，否则角色判定（owner == 我的 login ⇒ admin）全部落空、界面显示
     「我明明是管理员」却被读成 Member。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fake_repos(rows):
    return lambda uid: rows


def test_provider_resolved_from_user_repos_cache(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    monkeypatch.setattr(repos_route, "_repo_listable_providers",
                        lambda: {"gitcode", "gitee", "github", "gitlab"})
    from src.domains.mgmt.user import db as user_db

    monkeypatch.setattr(user_db, "get_user_repos",
                        _fake_repos([{"repo_key": "gitee:yang-aiwu-new/livefire-tpl-verify-1010"}]))

    assert repos_route._provider_from_user_repos(
        {"id": "33", "username": "yangaiwu"}, "yang-aiwu-new/livefire-tpl-verify-1010") == "gitee"


def test_provider_cache_hint_requires_unique_hit(monkeypatch):
    """同名仓库出现在两个平台 → 不猜（返回空，交给显式 provider 或人工）"""
    from src.domains.mgmt.routes import repos as repos_route

    monkeypatch.setattr(repos_route, "_repo_listable_providers",
                        lambda: {"gitcode", "gitee"})
    from src.domains.mgmt.user import db as user_db

    monkeypatch.setattr(user_db, "get_user_repos",
                        _fake_repos([{"repo_key": "gitee:o/r"}, {"repo_key": "gitcode:o/r"}]))
    assert repos_route._provider_from_user_repos({"id": "33"}, "o/r") == ""


def test_provider_cache_hint_no_hit(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    monkeypatch.setattr(repos_route, "_repo_listable_providers", lambda: {"gitee"})
    from src.domains.mgmt.user import db as user_db

    monkeypatch.setattr(user_db, "get_user_repos", _fake_repos([{"repo_key": "gitee:o/r"}]))
    assert repos_route._provider_from_user_repos({"id": "33"}, "other/repo") == ""
    assert repos_route._provider_from_user_repos({"id": "33"}, "") == ""


def test_generic_fetch_fills_owner_from_gitee_namespace():
    """Gitee v5 仓库对象用 namespace 表示归属；owner 缺失时不能让角色判定落空。"""
    from src.kernel.token_resolver import GenericVcsProvider

    class _Card:
        api_base = "https://gitee.com/api/v5"
        api_auth = "query_token"
        dialect = "gitee_v5"

    class _Prov(GenericVcsProvider):
        def __init__(self):
            self._card = _Card()

        def _http(self, method, path, token, data=None, form=False):
            if path.startswith("/user/repos"):
                return [
                    {"full_name": "myfuture_1/innovation", "name": "innovation",
                     "namespace": {"path": "myfuture_1"}, "permissions": {}},
                    {"name": "solo", "namespace": {"path": "yang-aiwu-new"}},
                ]
            return {}

        def get_user_login(self, token):
            return "yang-aiwu-new"

    rows = _Prov().fetch_repos_with_role("tok")
    assert rows[0]["owner"] == "myfuture_1"
    assert rows[0]["is_admin"] is False          # 别人的命名空间 → 不是管理员
    assert rows[1]["owner"] == "yang-aiwu-new"
    assert rows[1]["is_admin"] is True            # owner == 我 → 管理员
    assert rows[1]["full_name"] == "yang-aiwu-new/solo"   # 缺 full_name 时用 namespace 补