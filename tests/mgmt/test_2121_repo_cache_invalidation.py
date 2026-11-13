"""Issue #2121: user_repos 写后不失效 + 体验 host 失败静默。

覆盖：
1. invalidate_user_repos 置空 synced_at，保留缓存行与 is_join。
2. /repos/member 默认命中缓存；refresh=1 跳过缓存强制回源并落库。
3. 体验接口 host 失败时返回 hosted=False + host_error，不再误报成功。

运行：python3 -m pytest tests/test_2121_repo_cache_invalidation.py -q
"""
import asyncio
import os
import sys
from datetime import datetime

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

USER = {"id": 21211, "username": "u_2121", "auth_type": "jwt", "org_id": 0}


def test_invalidate_user_repos_marks_stale_and_preserves_data():
    from src.domains.mgmt.user import db as user_db

    uid = USER["id"]
    user_db.clear_user_repos(uid)
    user_db.save_user_repos(uid, [
        {"full_name": "org/keep-a", "owner": "org", "name": "keep-a",
         "description": "A", "role": "Admin", "is_admin": True},
    ])
    user_db.mark_user_repo_join(uid, "gitcode:org/keep-a", is_join=True, is_admin=True)
    assert user_db.get_user_repos_synced_at(uid) != ""

    user_db.invalidate_user_repos(uid)

    assert user_db.get_user_repos_synced_at(uid) == "", "invalidate 后缓存应视为过期"
    repos = user_db.get_user_repos(uid)
    assert len(repos) == 1, "invalidate 不应删除缓存行"
    assert repos[0]["full_name"] == "org/keep-a"
    assert repos[0]["is_join"] is True, "invalidate 不应丢失 is_join 状态"

    # 再次落库后恢复新鲜
    user_db.save_user_repos(uid, repos)
    assert user_db.get_user_repos_synced_at(uid) != ""
    user_db.clear_user_repos(uid)


def _patch_member_deps(monkeypatch, cached_repos, fresh_repos, synced_at):
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.auth import grape_account_db as acct_db

    calls = {"fetch": 0, "saved": None}

    monkeypatch.setattr(repos_route.user_db, "get_user_repos",
                        lambda uid: list(cached_repos))
    monkeypatch.setattr(repos_route.user_db, "get_user_repos_synced_at",
                        lambda uid: synced_at)
    monkeypatch.setattr(repos_route.user_db, "save_user_repos",
                        lambda uid, repos: calls.__setitem__("saved", list(repos)))
    monkeypatch.setattr(acct_db, "get_user_identities",
                        lambda uid: [{"provider": "gitcode"}])

    def _fetch(user, provider, fn, *a, **kw):
        calls["fetch"] += 1
        return list(fresh_repos)

    monkeypatch.setattr(repos_route, "with_oauth_token", _fetch)
    monkeypatch.setattr(repos_route, "_start_stale_probe_async", lambda *a, **kw: None)
    return calls


def test_member_default_uses_cache(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [{"full_name": "org/cached", "owner": "org", "name": "cached",
               "role": "Admin", "is_admin": True, "repo_key": "gitcode:org/cached"}]
    fresh = [{"full_name": "org/fresh", "owner": "org", "name": "fresh",
              "role": "Admin", "is_admin": True, "repo_key": "gitcode:org/fresh"}]
    fresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calls = _patch_member_deps(monkeypatch, cached, fresh, fresh_ts)

    result = repos_route.list_member_repos(USER, provider="", refresh=0)

    assert result["_cached"] is True
    assert calls["fetch"] == 0, "默认路径不应回源"
    assert [r["full_name"] for r in result["repos"]] == ["org/cached"]


def test_member_refresh_forces_vcs_and_persists(monkeypatch):
    from src.domains.mgmt.routes import repos as repos_route

    cached = [{"full_name": "org/cached", "owner": "org", "name": "cached",
               "role": "Admin", "is_admin": True, "repo_key": "gitcode:org/cached"}]
    fresh = [{"full_name": "org/cached", "owner": "org", "name": "cached",
              "role": "Admin", "is_admin": True, "repo_key": "gitcode:org/cached"},
             {"full_name": "org/new", "owner": "org", "name": "new",
              "role": "Admin", "is_admin": True, "repo_key": "gitcode:org/new"}]
    fresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    calls = _patch_member_deps(monkeypatch, cached, fresh, fresh_ts)

    result = repos_route.list_member_repos(USER, provider="", refresh=1)

    assert result["_cached"] is False, "refresh=1 必须绕过缓存"
    assert calls["fetch"] == 1, "refresh=1 应回源一次"
    assert calls["saved"] is not None, "refresh=1 回源结果应落库"
    assert "org/new" in [r["full_name"] for r in result["repos"]]


def test_experience_reports_host_failure(monkeypatch):
    from src.domains.mgmt.routes import scenes as scenes_route
    from src.domains.mgmt.routes import templates as templates_route
    from src.domains.mgmt.user import db as user_db
    import src.api.oauth_token as oauth_token

    scene = {
        "id": 2121,
        "name": "demo",
        "display_name": "Demo",
        "description": "",
        "recommended_template": "demo-tpl",
        "nodes_json": "[]",
        "onboarding": "",
    }
    monkeypatch.setattr(user_db, "get_scene_market_by_id", lambda mid: dict(scene))
    monkeypatch.setattr(templates_route, "_find_template",
                        lambda tid: {"template": {"default_repo_name": "demo-repo", "id": "demo-tpl"}})
    monkeypatch.setattr(oauth_token, "with_oauth_token",
                        lambda user, provider, fn, *a, **kw: "u_2121")

    async def _fake_create(req, user):
        return {"full_name": "u_2121/demo-repo", "webhook_id": 0,
                "host_warning": "bot not collaborator"}

    monkeypatch.setattr(templates_route, "create_from_template", _fake_create)

    result = asyncio.run(scenes_route.experience_market_scene(2121, dict(USER)))

    assert result["full_name"] == "u_2121/demo-repo"
    assert result["hosted"] is False
    assert result["host_error"], "host 失败原因必须回传"
    assert "托管失败" in result["message"]


def test_experience_host_error_extracts_dict_message(monkeypatch):
    """host_repo 抛 dict detail 时只回传 message，不把原始字典透给用户。"""
    from fastapi import HTTPException
    from src.domains.mgmt.routes import scenes as scenes_route
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.routes import templates as templates_route
    from src.domains.mgmt.user import db as user_db
    import src.api.oauth_token as oauth_token

    scene = {
        "id": 2121,
        "name": "demo",
        "display_name": "Demo",
        "description": "",
        "recommended_template": "demo-tpl",
        "nodes_json": "[]",
        "onboarding": "",
    }
    repo_name = "demohostfail"
    repo_key = "gitcode:u_2121/" + repo_name
    user_db.clear_user_repos(USER["id"])
    user_db.save_user_repos(USER["id"], [{
        "full_name": "u_2121/" + repo_name, "owner": "u_2121", "name": repo_name,
        "role": "Admin", "is_admin": True, "repo_key": repo_key,
    }])

    monkeypatch.setattr(user_db, "get_scene_market_by_id", lambda mid: dict(scene))
    monkeypatch.setattr(templates_route, "_find_template",
                        lambda tid: {"template": {"default_repo_name": repo_name, "id": "demo-tpl"}})
    monkeypatch.setattr(oauth_token, "with_oauth_token",
                        lambda user, provider, fn, *a, **kw: "u_2121")

    def _raise_host(*a, **kw):
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "error_code": "PERMISSION_DENIED",
            "message": "无权限托管该仓库：请使用仓库所有者的凭据",
        })

    monkeypatch.setattr(repos_route, "host_repo", _raise_host)

    try:
        result = asyncio.run(scenes_route.experience_market_scene(2121, dict(USER)))
    finally:
        user_db.clear_user_repos(USER["id"])

    assert result["hosted"] is False
    assert result["host_error"] == "无权限托管该仓库：请使用仓库所有者的凭据"
    assert "{'code'" not in result["host_error"]
    assert "无权限托管该仓库" in result["message"]


def test_experience_avoids_duplicate_host_warning_prefix(monkeypatch):
    """create_from_template 的 host_warning 已含前缀时不再二次包装。"""
    from src.domains.mgmt.routes import scenes as scenes_route
    from src.domains.mgmt.routes import templates as templates_route
    from src.domains.mgmt.user import db as user_db
    import src.api.oauth_token as oauth_token

    scene = {
        "id": 2121,
        "name": "demo",
        "display_name": "Demo",
        "description": "",
        "recommended_template": "demo-tpl",
        "nodes_json": "[]",
        "onboarding": "",
    }
    repo_name = "demoduplicate"
    user_db.clear_user_repos(USER["id"])

    monkeypatch.setattr(user_db, "get_scene_market_by_id", lambda mid: dict(scene))
    monkeypatch.setattr(templates_route, "_find_template",
                        lambda tid: {"template": {"default_repo_name": repo_name, "id": "demo-tpl"}})
    monkeypatch.setattr(oauth_token, "with_oauth_token",
                        lambda user, provider, fn, *a, **kw: "u_2121")

    async def _fake_create(req, user):
        return {"full_name": "u_2121/" + repo_name, "webhook_id": 0,
                "host_warning": "托管配置失败：bot not collaborator"}

    monkeypatch.setattr(templates_route, "create_from_template", _fake_create)
    try:
        result = asyncio.run(scenes_route.experience_market_scene(2121, dict(USER)))
    finally:
        user_db.clear_user_repos(USER["id"])

    assert result["hosted"] is False
    assert "托管配置失败" not in result["message"]
    assert result["message"].count("托管失败") == 1
    assert "bot not collaborator" in result["message"]


@pytest.mark.parametrize("existing_webhook", [None, 7777])
def test_host_repo_github_success_invalidates_cache(monkeypatch, existing_webhook):
    """GitHub 两个成功返回（复用/新建 webhook）都必须失效 user_repos 缓存。"""
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.user import db as user_db
    from src.domains.mgmt.user import repo_manager
    from src.api import onboarding, session_store

    monkeypatch.setattr(repos_route, "get_user_github_token_with_refresh",
                        lambda *a, **kw: "gh-token")
    monkeypatch.setattr(user_db, "get_webhook_url",
                        lambda: "http://localhost/webhook/gitcode")
    monkeypatch.setattr(user_db, "get_global_github_bot_token", lambda: "bot-token")
    monkeypatch.setattr(user_db, "get_global_github_bot_username", lambda: "bot")
    def _fake_with_oauth(user, provider, fn, *a, **kw):
        if getattr(fn, "__name__", "") == "_gh_precheck":
            return {"info": {"permissions": {"admin": True}, "owner": {"login": "2121owner"}},
                    "login": "2121owner", "perms": {"admin": True}}
        return fn("gh-token")

    monkeypatch.setattr(repos_route, "with_oauth_token", _fake_with_oauth)
    monkeypatch.setattr(repo_manager, "find_existing_github_webhook",
                        lambda *a, **kw: existing_webhook)
    monkeypatch.setattr(repo_manager, "setup_github_webhook", lambda *a, **kw: 7778)
    monkeypatch.setattr(repos_route, "_try_github_bot_collaborator",
                        lambda *a, **kw: {"bot_collaborator_needed": False})
    monkeypatch.setattr(onboarding, "cleanup_stale_repo_data", lambda *a, **kw: None)
    monkeypatch.setattr(session_store, "ensure_default_agent_sessions", lambda *a, **kw: None)
    monkeypatch.setattr(user_db, "add_managed_repo", lambda *a, **kw: True)
    monkeypatch.setattr(user_db, "mark_user_repo_join", lambda *a, **kw: None)
    invalidations = []
    monkeypatch.setattr(user_db, "invalidate_user_repos",
                        lambda uid: invalidations.append(uid))

    result = repos_route.host_repo(
        repos_route.RepoAction(full_name="github:2121owner/repo-gh"), dict(USER),
        provider="github")

    assert result["webhook_id"] == (existing_webhook or 7778)
    assert invalidations == [USER["id"]], "GitHub 托管成功后必须失效 user_repos 缓存"


def test_remove_managed_repo_resets_join_and_refreshes_list(monkeypatch):
    """解除托管：复位 is_join、失效缓存，刷新后仓库回到未托管列表。"""
    from src.domains.mgmt.routes import repos as repos_route
    from src.domains.mgmt.user import db as user_db
    from src.domains.mgmt.user import repo_manager
    from src import repo_cleanup

    uid = USER["id"]
    repo_key = "github:2121owner/unhost-demo"
    user_db.clear_user_repos(uid)
    user_db.save_user_repos(uid, [{
        "full_name": "2121owner/unhost-demo", "owner": "2121owner", "name": "unhost-demo",
        "role": "Admin", "is_admin": True, "repo_key": repo_key,
    }])
    user_db.mark_user_repo_join(uid, repo_key, is_join=True, is_admin=True)
    user_db.add_managed_repo(repo_key, "2121owner", "unhost-demo")
    assert user_db.get_user_repos_synced_at(uid) != ""

    monkeypatch.setattr(repos_route, "_check_unmanage_vcs_permission", lambda *a, **kw: None)
    monkeypatch.setattr(repos_route, "get_user_github_token_with_refresh",
                        lambda *a, **kw: "gh-token")
    monkeypatch.setattr(repo_manager, "_get_managed_webhook", lambda *a, **kw: 0)
    monkeypatch.setattr(user_db, "get_managed_repos", lambda *a, **kw: [])
    monkeypatch.setattr(user_db, "get_global_github_bot_username", lambda: "")
    monkeypatch.setattr(user_db, "get_host_user_id", lambda *a, **kw: 1)
    monkeypatch.setattr(repo_cleanup, "cascade_cleanup_repo_data", lambda *a, **kw: {})

    try:
        result = repos_route.remove_managed_repo(
            repos_route.RepoAction(full_name="github:2121owner/unhost-demo"), dict(USER))
        assert result["message"]

        rows = user_db.get_user_repos(uid)
        row = next(r for r in rows if r.get("repo_key") == repo_key)
        assert row["is_join"] is False, "解除托管必须复位 is_join"
        assert user_db.get_user_repos_synced_at(uid) == "", "解除托管必须失效缓存"

        cached = user_db.get_user_repos(uid)
        fresh = [{
            "full_name": "2121owner/unhost-demo", "owner": "2121owner", "name": "unhost-demo",
            "role": "Admin", "is_admin": True, "repo_key": repo_key,
        }]
        _patch_member_deps(monkeypatch, cached, fresh, "")
        member = repos_route.list_member_repos(USER, provider="", refresh=0)
        assert member["_cached"] is False, "缓存已失效，刷新应回源"
        target = next(r for r in member["repos"] if r["repo_key"] == repo_key)
        assert target["is_joined"] is False
        assert target["is_hosted"] is False, "解除托管后应回到未托管列表"
    finally:
        user_db.clear_user_repos(uid)
