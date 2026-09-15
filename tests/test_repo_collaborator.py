"""托管/解除托管时自动管理 GitCode 项目维护者（全局 bot + repo bot）的测试"""
import os
import sys
from unittest.mock import patch, MagicMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")

import asyncio
from fastapi import HTTPException


def _run(coro):
    import inspect
    if inspect.iscoroutine(coro):
        return asyncio.run(coro)
    return coro  # sync function returns directly


# ===================== repo_manager.add_collaborator =====================

def test_add_collaborator_success():
    """GitCode PUT body 用单数 permission 字段，maintainer 映射为 admin（=仓库维护者）"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = add_collaborator("tok", "owner", "repo", "hwc-dev-bot", "maintainer")
        assert ok is True
        url = mock_req.call_args[0][1]
        assert "/repos/owner/repo/collaborators/hwc-dev-bot" in url
        body = mock_req.call_args[0][3]
        assert body.get("permission") == "admin"  # maintainer → admin
        assert "permissions" not in body


def test_add_collaborator_developer_maps_push():
    """developer → push（GitCode 枚举）"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = add_collaborator("tok", "owner", "repo", "bot", "developer")
        assert ok is True
        assert mock_req.call_args[0][3].get("permission") == "push"


def test_add_collaborator_no_fallback():
    """maintainer 邀请失败时不再降级 developer，直接返回 False（由调用方拒绝托管）"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("fail")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert mock_req.call_count == 1  # 只试一次，不降级
        assert ok is False


def test_add_collaborator_already_member():
    """已是成员（400）视为幂等成功"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("用户已经是项目中的成员")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert ok is True


def test_add_collaborator_already_invited():
    """已被邀请（400 用户已被邀请）视为幂等成功"""
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("用户已被邀请")
        ok = add_collaborator("tok", "owner", "repo", "bot", "maintainer")
        assert ok is True


def test_add_collaborator_no_username():
    from src.user.repo_manager import add_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        ok = add_collaborator("tok", "owner", "repo", "", "maintainer")
        assert ok is False
        mock_req.assert_not_called()


# ===================== repo_manager.remove_collaborator =====================

def test_remove_collaborator_success():
    from src.user.repo_manager import remove_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.return_value = {}
        ok = remove_collaborator("tok", "owner", "repo", "hwc-dev-bot")
        assert ok is True
        url = mock_req.call_args[0][1]
        assert "/repos/owner/repo/collaborators/hwc-dev-bot" in url
        assert mock_req.call_args[0][0] == "DELETE"


def test_remove_collaborator_404_ok():
    """用户本就不是协作者（404）视为成功"""
    from src.user.repo_manager import remove_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("GitCode API DELETE failed (404): not found")
        ok = remove_collaborator("tok", "owner", "repo", "bot")
        assert ok is True


def test_remove_collaborator_error_false():
    """其他错误返回 False"""
    from src.user.repo_manager import remove_collaborator
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.side_effect = RuntimeError("GitCode API DELETE failed (500): server error")
        ok = remove_collaborator("tok", "owner", "repo", "bot")
        assert ok is False


# ===================== repo_manager invite 系列（GitCode 定制接口） =====================

def test_list_user_invites():
    from src.user.repo_manager import list_user_invites, _req_query
    with patch("src.user.repo_manager._req_query") as mock_q:
        mock_q.return_value = {"content": [
            {"source_name": "owner/repo", "invitation_token": "tok1", "user_role": "开发者"}
        ]}
        invites = list_user_invites("bot_tok")
        assert len(invites) == 1
        assert invites[0]["invitation_token"] == "tok1"
        args = mock_q.call_args[0]
        assert args[0] == "GET"
        assert "/user/invite" in args[1]
        # token 作为第 3 个位置参数传入，走 _req_query（内部拼 query access_token）而非 Bearer
        assert args[2] == "bot_tok"
        assert args[3] == {"page": 1, "per_page": 100}


def test_accept_invite_for_repo():
    """按 owner/repo 匹配邀请并接受"""
    from src.user.repo_manager import accept_invite_for_repo
    with patch("src.user.repo_manager.list_user_invites", return_value=[
        {"source_name": "other/repo", "invitation_token": "x"},
        {"source_name": "owner/repo", "invitation_token": "match_tok"},
    ]), patch("src.user.repo_manager.accept_project_invite", return_value=True) as mock_acc:
        ok = accept_invite_for_repo("bot_tok", "owner", "repo")
        assert ok is True
        mock_acc.assert_called_once_with("bot_tok", "match_tok")


def test_accept_invite_for_repo_no_match():
    from src.user.repo_manager import accept_invite_for_repo
    with patch("src.user.repo_manager.list_user_invites", return_value=[
        {"source_name": "other/repo", "invitation_token": "x"},
    ]), patch("src.user.repo_manager.accept_project_invite") as mock_acc:
        ok = accept_invite_for_repo("bot_tok", "owner", "repo")
        assert ok is False
        mock_acc.assert_not_called()


def test_get_member_permission():
    from src.user.repo_manager import get_member_permission
    with patch("src.user.repo_manager._req") as mock_req:
        mock_req.return_value = {"access_level": 40, "role_name": "Maintainer", "permission": "admin"}
        data = get_member_permission("tok", "owner", "repo", "grape_bot")
        assert data["access_level"] == 40
        url = mock_req.call_args[0][1]
        assert "/collaborators/grape_bot/permission" in url


# ===================== host_repo：添加全局 bot 失败拒绝托管 =====================

def test_host_repo_global_bot_fail_rejects():
    """全局 bot 添加协作者失败 → 拒绝托管（抛 502）"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.user.repo_manager.add_collaborator", return_value=False) as mock_add, \
         patch("src.user.repo_manager.find_existing_webhook", return_value=None), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        try:
            _run(repos_mod.host_repo(FakeBody(), fake_user))
            assert False, "should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 502
            assert "全局 Bot" in e.detail["message"]
        mock_add.assert_called_once()


def test_host_repo_global_bot_success():
    """全局 bot 添加协作者成功 + 自动接受邀请 + 已是维护者 → 托管成功"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.user.repo_manager.add_collaborator", return_value=True), \
         patch("src.user.repo_manager.accept_invite_for_repo", return_value=True) as mock_acc, \
         patch("src.user.repo_manager.get_member_permission", return_value={"access_level": 40, "role_name": "Maintainer"}) as mock_perm, \
         patch("src.user.repo_manager.find_existing_webhook", return_value=None), \
         patch.object(repos_mod, "setup_webhook", return_value=1), \
         patch.object(repos_mod, "user_db") as mock_udb, \
         patch("src.api.session_store.ensure_default_agent_sessions"), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        r = _run(repos_mod.host_repo(FakeBody(), fake_user))
        assert "托管成功" in r["message"]
        mock_udb.add_managed_repo.assert_called_once()
        mock_acc.assert_called_once()
        mock_perm.assert_called_once()


def test_host_repo_global_bot_not_maintainer_rejects():
    """bot 已加入但非维护者（access_level<40）→ 拒绝托管"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch("src.user.db.get_meta_value", return_value="http://webhook"), \
         patch("src.user.repo_manager._check_collaborator_admin", return_value=True), \
         patch("src.user.repo_manager._req", return_value={"login": "tester"}), \
         patch("src.user.repo_manager.add_collaborator", return_value=True), \
         patch("src.user.repo_manager.accept_invite_for_repo", return_value=True), \
         patch("src.user.repo_manager.get_member_permission", return_value={"access_level": 30, "role_name": "Developer"}), \
         patch("src.user.repo_manager.find_existing_webhook", return_value=None), \
         patch.object(repos_mod, "setup_webhook", return_value=1), \
         patch.object(repos_mod, "user_db") as mock_udb, \
         patch("src.api.session_store.ensure_default_agent_sessions"), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        try:
            _run(repos_mod.host_repo(FakeBody(), fake_user))
            assert False, "should have raised HTTPException"
        except HTTPException as e:
            assert e.status_code == 502
            assert "维护者" in e.detail["message"]
        mock_udb.add_managed_repo.assert_not_called()

# ===================== set_repo_bot_token：自动更新 bot_username + 添加 repo bot =====================

def test_set_repo_bot_token_updates_username():
    """配置 repo bot token 后，自动更新 bot_username 并添加维护者"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"
        bot_token = "repo_bot_tok"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

    with patch.object(repos_mod, "update_managed_repo_bot_token", return_value=True) as mock_upd, \
         patch("src.user.repo_manager._req", return_value={"login": "repo-bot-login"}), \
         patch.object(repos_mod, "get_user_token", return_value="owner_tok"), \
         patch.object(repos_mod, "update_managed_repo_bot_username") as mock_upd_username, \
         patch("src.user.repo_manager.add_collaborator", return_value=True) as mock_add:
        _run(repos_mod.set_repo_bot_token(FakeBody(), fake_user))
        mock_upd.assert_called_once()
        mock_upd_username.assert_called_once()
        args = mock_add.call_args[0]
        # 用 owner token 添加 repo bot 为维护者
        assert args[0] == "owner_tok"
        assert args[3] == "repo-bot-login"


# ===================== remove_managed_repo：移除 bot 协作者 =====================

def test_remove_managed_repo_removes_collaborators():
    """解除托管时移除全局 bot + repo bot 协作者"""
    import src.api.routes.repos as repos_mod

    class FakeBody:
        full_name = "owner/repo"

    fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

    def fake_managed(user_id):
        return [{"repo_key": "gitcode:owner/repo", "full_name": "owner/repo",
                 "bot_gitcode_token": "repo_tok", "bot_username": "repo-bot-login"}]

    gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

    with patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
         patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
         patch("src.user.repo_manager._get_managed_webhook", return_value=None), \
         patch("src.user.repo_manager.remove_collaborator") as mock_rm, \
         patch.object(repos_mod, "user_db") as mock_udb, \
         patch("src.api.session_store.remove_agent_sessions"), \
         patch("builtins.open", MagicMock()) as mock_open:
        mock_udb.get_managed_repos.side_effect = fake_managed
        mock_udb.remove_managed_repo.return_value = {"full_name": "owner/repo", "webhook_id": None}
        mock_f = MagicMock()
        mock_f.__enter__.return_value.read.return_value = gbt_json
        mock_open.return_value = mock_f
        _run(repos_mod.remove_managed_repo(FakeBody(), fake_user))
        assert mock_rm.call_count == 2
        usernames = [c.args[3] for c in mock_rm.call_args_list]
        assert "hwc-dev-bot" in usernames
        assert "repo-bot-login" in usernames


# ===================== repo_manager.fetch_repos_with_role (N+1 优化) =====================

def test_fetch_repos_uses_permission_field_no_nplus1():
    """列表响应含 permission 字段时直接判定 is_admin，不再对每个非 owner 仓库调 collaborator API"""
    from src.user.repo_manager import fetch_repos_with_role

    repos_payload = [
        {"full_name": "me/own-repo", "name": "own-repo",
         "owner": {"login": "me"}, "permission": {"pull": True, "push": True, "admin": True}},
        {"full_name": "org/repo-admin", "name": "repo-admin",
         "owner": {"login": "org"}, "permission": {"pull": True, "push": True, "admin": True}},
        {"full_name": "org/repo-member", "name": "repo-member",
         "owner": {"login": "org"}, "permission": {"pull": True, "push": False, "admin": False}},
    ]

    calls = []
    def fake_req(method, url, token="", body=None):
        calls.append(url)
        if url.endswith("/user"):
            return {"login": "me"}
        if "/user/repos" in url:
            return repos_payload
        return {}

    with patch("src.user.repo_manager._req", side_effect=fake_req) as mock_req:
        result = fetch_repos_with_role("tok")

    # 只应有 2 次请求：/user + /user/repos（无 collaborator permission N+1）
    assert len(calls) == 2, f"应只有 2 次 GitCode 请求，实际 {len(calls)}: {calls}"
    assert not any("collaborators" in u for u in calls), "不应调用 collaborator API"

    by_name = {r["full_name"]: r["is_admin"] for r in result}
    assert by_name["me/own-repo"] is True        # owner -> admin
    assert by_name["org/repo-admin"] is True     # permission.admin -> admin
    assert by_name["org/repo-member"] is False   # permission.admin=false -> member
